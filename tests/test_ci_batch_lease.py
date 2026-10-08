"""Exact inert API absence and real empty-expect Git semantics in authored temporary repos."""

import dataclasses
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci.batch import (BatchBranchLease, empty_batch_branch_lease,
                                      observe_batch_branch_lease, recheck_batch_branch_lease,
                                      validate_batch_push_receipt)
from mod_base.errors import MbError
from mod_base.github.api import ApiError, ApiNotFound
from mod_base.model import grammar, limits
from tests import test_ci_batch as members


class BatchBranchLeaseTests(unittest.TestCase):
    def seed(self):
        api, prs, controller = members.BatchMembersTests().seed()
        endpoint = f'/repos/{api.repository}/git/ref/heads/batch/fixture'
        return api, prs, controller, endpoint

    def observe(self, api, controller):
        return observe_batch_branch_lease(api, controller_sha=controller, branch='batch/fixture', pr_numbers=(9, 7))

    def test_exact_absence_brackets_ordered_members_and_recheck(self):
        api, prs, controller, endpoint = self.seed()
        original = api.get_json
        reads = []
        def get(path, **kwargs):
            if path == endpoint:
                reads.append(path)
                raise ApiNotFound('absent', status=404, method='GET', path=endpoint)
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get):
            lease = self.observe(api, controller)
            self.assertEqual(len(reads), 2)
            self.assertEqual([member.generation.pr_number for member in lease.members], [9, 7])
            recheck_batch_branch_lease(api, lease, controller_sha=controller, branch='batch/fixture', pr_numbers=(9, 7))
        self.assertEqual(len(reads), 4)
        self.assertEqual(api.mutations, [])

    def test_invalid_branch_and_members_stop_before_api(self):
        api, prs, controller, endpoint = self.seed()
        for branch in ('batch/', 'master', 'batch/a/', 'batch/a.', 'batch/.hidden',
                       'batch/a/.hidden', 'batch/a.lock', 'batch/a.lock/b', 'batch/a..b',
                       'batch/a//b', 'batch/a:b', 'batch/'+'a'*195, None, True):
            with self.subTest(branch=branch), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                observe_batch_branch_lease(api, controller_sha=controller, branch=branch, pr_numbers=(7,))
            reads.assert_not_called()
        for numbers in ((), [], (True,), (7, 7), tuple(range(1, limits.MAX_CI_BATCH_MEMBERS+2))):
            with self.subTest(numbers=numbers), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                observe_batch_branch_lease(api, controller_sha=controller, branch='batch/fixture', pr_numbers=numbers)
            reads.assert_not_called()

    def test_only_exact_ref_get_not_found_is_absence(self):
        for error in (ApiNotFound('wrong endpoint', status=404, method='GET', path='/other'),
                      ApiNotFound('wrong method', status=404, method='POST', path='same'),
                      ApiNotFound('wrong status', status=500, method='GET', path='same'),
                      ApiError('generic 404', status=404, method='GET', path='same'),
                      ApiError('transport', status=0, method='GET', path='same'),
                      ApiError('permission', status=403, method='GET', path='same'),
                      ApiError('rate limited', status=429, method='GET', path='same')):
            api, prs, controller, endpoint = self.seed()
            if error.path == 'same':
                error.path = endpoint
            original = api.get_json
            def get(path, **kwargs):
                if path == endpoint:
                    raise error
                return original(path, **kwargs)
            with self.subTest(error=str(error)), patch.object(api, 'get_json', side_effect=get), self.assertRaises(type(error)) as caught:
                self.observe(api, controller)
            self.assertIs(caught.exception, error)

    def test_existing_and_malformed_ref_responses_reject(self):
        for response in ({'ref': 'refs/heads/batch/fixture', 'object': {'sha': 'f'*40, 'type': 'commit'}},
                         None, [], {}, False, {'message': 'Not Found'}):
            api, prs, controller, endpoint = self.seed()
            api.add_response(endpoint, response)
            with self.subTest(response=response), self.assertRaises(MbError):
                self.observe(api, controller)

    def test_name_appearing_on_closing_read_rejects(self):
        api, prs, controller, endpoint = self.seed()
        original = api.get_json
        count = 0
        def get(path, **kwargs):
            nonlocal count
            if path == endpoint:
                count += 1
                if count == 2:
                    return {'ref': 'refs/heads/batch/fixture'}
                raise ApiNotFound('absent', status=404, method='GET', path=endpoint)
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), self.assertRaisesRegex(MbError, 'already exists'):
            self.observe(api, controller)

    def test_member_or_controller_movement_during_absence_read_rejects(self):
        for move in ('draft', 'head', 'controller'):
            api, prs, controller, endpoint = self.seed()
            original = api.get_json
            def get(path, **kwargs):
                if path == endpoint:
                    if move == 'draft':
                        prs[7]['draft'] = True
                    elif move == 'head':
                        prs[7]['head']['sha'] = 'f'*40
                    else:
                        branch = original(f'/repos/{api.repository}/branches/master')
                        branch['commit']['sha'] = 'f'*40
                        api.add_response(f'/repos/{api.repository}/branches/master', branch)
                    api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
                    raise ApiNotFound('absent', status=404, method='GET', path=endpoint)
                return original(path, **kwargs)
            with self.subTest(move=move), patch.object(api, 'get_json', side_effect=get), self.assertRaises(MbError):
                self.observe(api, controller)

    def test_source_movement_in_last_absence_read_cannot_return_stale_observation(self):
        api, prs, controller, endpoint = self.seed()
        original = api.get_json
        count = 0
        def get(path, **kwargs):
            nonlocal count
            if path == endpoint:
                count += 1
                if count == 2:
                    prs[7]['draft'] = True
                    api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
                raise ApiNotFound('absent', status=404, method='GET', path=endpoint)
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), self.assertRaisesRegex(MbError, 'after final empty branch read'):
            self.observe(api, controller)

    def test_recheck_requires_original_binding_and_current_sources(self):
        api, prs, controller, endpoint = self.seed()
        # Explicit fixture response for the exact ref route, all other admission uses real fake API.
        original = api.get_json
        def get(path, **kwargs):
            if path == endpoint:
                raise ApiNotFound('absent', status=404, method='GET', path=endpoint)
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get):
            lease = self.observe(api, controller)
        for changed in (None, dataclasses.replace(lease, repository='foreign/mod'),
                        dataclasses.replace(lease, branch='batch/substitute'),
                        dataclasses.replace(lease, controller_sha='f'*40),
                        dataclasses.replace(lease, members=tuple(reversed(lease.members))),
                        BatchBranchLease(api.repository, lease.branch, controller, (None,))):
            with self.subTest(changed=changed), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                recheck_batch_branch_lease(api, changed, controller_sha=controller, branch=lease.branch, pr_numbers=(9, 7))
            reads.assert_not_called()
        prs[7]['draft'] = True
        api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
        with patch.object(api, 'get_json', side_effect=get), self.assertRaises(MbError):
            recheck_batch_branch_lease(api, lease, controller_sha=controller, branch=lease.branch, pr_numbers=(9, 7))

    def test_empty_expect_argument_and_branch_boundaries(self):
        for branch in ('batch/x', 'batch/-x', 'batch/a./b', 'batch/a.LOCK', 'batch/'+'a'*194):
            self.assertTrue(grammar.is_batch_branch(branch))
            self.assertEqual(empty_batch_branch_lease(branch), '--force-with-lease=refs/heads/'+branch+':')

    def test_push_receipt_requires_exact_new_branch_exit_and_framing(self):
        branch, commit, remote = 'batch/fixture', 'f'*40, 'https://github.com/example/mod.git'
        raw = f'To {remote}\n*\t{commit}:refs/heads/{branch}\t[new branch]\nDone\n'.encode()
        for data in (raw, raw.replace(b'\n', b'\r\n')):
            validate_batch_push_receipt(data, exit_code=0, remote=remote, branch=branch, commit_sha=commit)
        for data in (b'', raw[:-1], raw+b'Done\n', raw.replace(b'*\t', b'=\t'),
                     raw.replace(b'[new branch]', b'[up to date]'), raw.replace(b'Done', b'unknown'),
                     raw.replace(b'example/mod', b'foreign/mod'), raw.replace(b'f'*40, b'e'*40),
                     raw.replace(b'batch/fixture', b'batch/other'), raw.replace(b'\n', b'\r'),
                     raw+b'\xff', raw+b'\x00', b'x'*(limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES+1)):
            with self.subTest(data=data[:40]), self.assertRaises(MbError):
                validate_batch_push_receipt(data, exit_code=0, remote=remote, branch=branch, commit_sha=commit)
        for status in (False, 1, -1, '0'):
            with self.subTest(status=status), self.assertRaises(MbError):
                validate_batch_push_receipt(raw, exit_code=status, remote=remote, branch=branch, commit_sha=commit)


class LocalGitEmptyLeaseTest(unittest.TestCase):
    def test_real_git_creates_once_and_refuses_raced_existing_branch(self):
        executable = shutil.which('git')
        if executable is None:
            self.skipTest('git is unavailable')
        with tempfile.TemporaryDirectory(prefix='mod-base-empty-lease-') as directory:
            root = Path(directory)
            local, remote = root/'authored.git', root/'remote.git'
            environment = {'PATH': str(Path(executable).parent), 'HOME': str(root), 'LC_ALL': 'C',
                           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                           'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                           'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid',
                           'GIT_AUTHOR_DATE': '2000-01-01T00:00:00+0000', 'GIT_COMMITTER_DATE': '2000-01-01T00:00:00+0000'}
            if os.name == 'nt':
                environment['SYSTEMROOT'] = os.environ['SYSTEMROOT']
            def run(repo, *arguments, data=None, succeeds=True, receipt=False):
                result = subprocess.run([executable, '--git-dir='+str(repo), *arguments], input=data,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
                                        timeout=20, check=False)
                if succeeds:
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                else:
                    self.assertNotEqual(result.returncode, 0)
                return result if receipt else result.stdout.strip().decode('ascii')
            for repo in (local, remote):
                run(repo, 'init', '--bare', '--template=')
            for accepted in ('batch/x', 'batch/-x', 'batch/a./b', 'batch/a.LOCK'):
                self.assertTrue(grammar.is_batch_branch(accepted))
                run(local, 'check-ref-format', 'refs/heads/'+accepted)
            for refused in ('batch/', 'batch/a/', 'batch/a.', 'batch/.hidden', 'batch/a.lock/b'):
                self.assertFalse(grammar.is_batch_branch(refused))
                run(local, 'check-ref-format', 'refs/heads/'+refused, succeeds=False)
            tree = run(local, 'hash-object', '-t', 'tree', '-w', '--stdin', data=b'')
            first = run(local, 'commit-tree', tree, data=b'first authored fixture\n')
            second = run(local, 'commit-tree', tree, '-p', first, data=b'second authored fixture\n')
            branch = 'batch/fixture'
            ref = 'refs/heads/'+branch
            lease = empty_batch_branch_lease(branch)
            created = run(local, 'push', '--porcelain', '--no-verify', lease, str(remote), first+':'+ref, receipt=True)
            validate_batch_push_receipt(created.stdout, exit_code=created.returncode, remote=str(remote),
                                        branch=branch, commit_sha=first)
            self.assertEqual(run(remote, 'rev-parse', ref), first)
            # Same-SHA no-op returns exit 0 even with empty expectation; it is not our creation.
            duplicate = run(local, 'push', '--porcelain', '--no-verify', lease, str(remote), first+':'+ref, receipt=True)
            self.assertEqual(duplicate.returncode, 0)
            with self.assertRaises(MbError):
                validate_batch_push_receipt(duplicate.stdout, exit_code=duplicate.returncode, remote=str(remote),
                                            branch=branch, commit_sha=first)
            # A name that exists cannot be advanced even by a fast-forward with empty expectation.
            run(local, 'push', '--no-verify', lease, str(remote), second+':'+ref, succeeds=False)
            self.assertEqual(run(remote, 'rev-parse', ref), first)
            child = branch+'/child'
            run(local, 'push', '--no-verify', empty_batch_branch_lease(child), str(remote), second+':refs/heads/'+child,
                succeeds=False)
            self.assertEqual(run(remote, 'rev-parse', ref), first)
