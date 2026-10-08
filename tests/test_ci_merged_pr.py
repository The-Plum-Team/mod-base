"""Historical merged PR observations use actual inert API logic, never full-gate authority."""

import dataclasses
import hashlib
import unittest
from unittest.mock import patch

from mod_base.build_ci import authenticate
from mod_base.build_ci.authenticate import authenticate_merged_pr_identity, authenticate_pr_identity
from mod_base.build_ci.reads import CommandReads
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests.test_ci_protocol import seeded_pr


class MergedPrTests(unittest.TestCase):
    def seed(self, *, current=False, parents=None):
        plan, api, pr = seeded_pr()
        identity = plan['identity']
        merged = 'b'*40
        controller = merged if current else 'c'*40
        pr.update(state='closed', merged=True, merged_at='2026-10-08T10:00:00Z', merge_commit_sha=merged)
        # Post-merge API base observations are not the original synthetic base parent.
        pr['base']['sha'] = controller
        api.add_response(f'/repos/{api.repository}/pulls/7', pr)
        api.add_commit(merged, identity['tested_tree'],
                       parents=[identity['base_sha']] if parents is None else parents)
        api.set_branch('master', controller, identity['tested_tree'] if current else 'd'*40)
        api.add_compare(identity['base_sha'], merged, {'status': 'ahead', 'ahead_by': 2, 'behind_by': 0})
        api.add_compare(merged, controller, {'status': 'identical' if current else 'ahead',
                                            'ahead_by': 0 if current else 3, 'behind_by': 0})
        return api, identity, pr, merged, controller

    def test_final_merge_squash_and_rebase_parents_preserve_exact_tested_merge(self):
        for parents in (['a'*40, 'e'*40], ['a'*40], ['f'*40]):
            for current in (False, True):
                api, identity, pr, merged, controller = self.seed(current=current, parents=parents)
                with self.subTest(parents=parents, current=current):
                    result = authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
                    self.assertEqual(result.merged_parents, tuple(parents))
                    self.assertEqual(result.merged_tree, identity['tested_tree'])
                    self.assertEqual(result.identity_sha256, hashlib.sha256(canonical_json(identity)).hexdigest())
                    self.assertEqual(result.merged_sha, merged)
                    self.assertEqual(result.controller_sha, controller)
                    self.assertEqual(result.repository, api.repository)
                    self.assertEqual(result.pr_number, 7)
                    self.assertEqual(result.merged_at, pr['merged_at'])
                    self.assertEqual(api.mutations, [])
                    with self.assertRaises(dataclasses.FrozenInstanceError):
                        result.merged_sha = 'f'*40
                    with self.assertRaises(MbError):
                        authenticate_pr_identity(api, identity)
        api, identity, pr, _, controller = self.seed()
        merged = identity['tested_sha']
        pr['merge_commit_sha'] = merged
        api.add_response(f'/repos/{api.repository}/pulls/7', pr)
        api.add_compare(identity['base_sha'], merged, {'status': 'ahead', 'ahead_by': 2, 'behind_by': 0})
        api.add_compare(merged, controller, {'status': 'ahead', 'ahead_by': 3, 'behind_by': 0})
        result = authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
        self.assertEqual(result.merged_parents, tuple(identity['tested_parents']))

    def test_invalid_independent_bindings_reject_before_api(self):
        for field, value in (('pr_number', 0), ('source_repository', 'fork/mod'),
                             ('repository', 'foreign/mod'), ('controller_sha', 'f'*40), ('extra', True)):
            api, identity, pr, merged, controller = self.seed()
            identity[field] = value
            with self.subTest(field=field), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
            reads.assert_not_called()
        for controller, merged in ((True, 'b'*40), ('c'*40, None), ('c'*40, '1'*40)):
            api, identity, pr, expected, original_controller = self.seed()
            if merged == '1'*40:
                merged = identity['base_sha']
            with self.subTest(controller=controller, merged=merged), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
            reads.assert_not_called()

    def test_closed_unmerged_draft_fork_malformed_or_moved_records_refuse(self):
        changes = [lambda p: p.update(number=True), lambda p: p.update(number=8),
                   lambda p: p.update(state='open'), lambda p: p.update(merged=False),
                   lambda p: p.update(merged=1), lambda p: p.update(draft=True), lambda p: p.update(draft=0),
                   lambda p: p.update(merged_at=None), lambda p: p.update(merged_at='2026-02-30T10:00:00Z'),
                   lambda p: p.update(merged_at='x'*1000), lambda p: p.update(merge_commit_sha='f'*40),
                   lambda p: p.update(head=None), lambda p: p['head'].update(repo=None),
                   lambda p: p['head']['repo'].update(full_name='fork/mod'),
                   lambda p: p['base']['repo'].update(full_name='foreign/mod'),
                   lambda p: p['head'].update(sha='f'*40), lambda p: p['head'].update(ref='other'),
                   lambda p: p['base'].update(ref='other'), lambda p: p['base'].update(sha=False)]
        for index, change in enumerate(changes):
            api, identity, pr, merged, controller = self.seed()
            change(pr)
            api.add_response(f'/repos/{api.repository}/pulls/7', pr)
            with self.subTest(index=index), self.assertRaises(MbError):
                authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)

    def test_actual_original_tested_and_final_tree_parent_objects_are_required(self):
        for target in ('tested', 'merged'):
            for field, value in (('sha', 'f'*40), ('tree', {'sha': 'f'*40}), ('parents', []),
                                 ('parents', [{'sha': 'a'*40}]*3), ('parents', [{'sha': True}]),
                                 ('parents', 'reversed'), ('parents', 'self'), ('parents', 'duplicate')):
                api, identity, pr, merged, controller = self.seed()
                sha = identity['tested_sha'] if target == 'tested' else merged
                response = api.get_json(f'/repos/{api.repository}/git/commits/{sha}')
                if value == 'reversed':
                    if target == 'merged':
                        continue  # Final parents need not match the original synthetic parents.
                    value = [{'sha': parent} for parent in reversed(identity['tested_parents'])]
                elif value == 'self':
                    value = [{'sha': sha}]
                elif value == 'duplicate':
                    value = [{'sha': 'a'*40}]*2
                response[field] = value
                api.add_response(f'/repos/{api.repository}/git/commits/{sha}', response)
                with self.subTest(target=target, field=field, value=value), self.assertRaises(MbError):
                    authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)

    def test_original_base_and_current_history_are_both_required(self):
        for current in (False, True):
            for target in ('base', 'current'):
                for status, ahead, behind in (('behind', 0, 1), ('diverged', 1, 1), ('ahead', 0, 0),
                                              ('identical', 1, 0), ('ahead', 1, 1)):
                    api, identity, pr, merged, controller = self.seed(current=current)
                    pair = (identity['base_sha'], merged) if target == 'base' else (merged, controller)
                    api.add_compare(*pair, {'status': status, 'ahead_by': ahead, 'behind_by': behind})
                    with self.subTest(current=current, target=target, status=status, ahead=ahead), self.assertRaises(MbError):
                        authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
        api, identity, pr, merged, controller = self.seed()
        with self.assertRaises(MbError):
            authenticate_merged_pr_identity(api, identity, controller_sha='f'*40, merged_sha=merged)
        api, identity, pr, merged, controller = self.seed(current=True)
        endpoint = f'/repos/{api.repository}/branches/master'
        value = api.get_json(endpoint)
        value['commit']['commit']['tree']['sha'] = 'f'*40
        api.add_response(endpoint, value)
        with self.assertRaises(MbError):
            authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)

    def test_a_later_observation_shows_or_refuses_every_record_controller_and_default_change(self):
        def merged_at(api, pr):
            pr['merged_at'] = '2026-10-08T10:01:00Z'
            api.add_response(f'/repos/{api.repository}/pulls/7', pr)

        def head(api, pr):
            pr['head']['sha'] = 'f'*40
            api.add_response(f'/repos/{api.repository}/pulls/7', pr)

        changes = {'merged-at': merged_at, 'head': head,
                   'controller': lambda api, pr: api.set_branch('master', 'f'*40, 'd'*40),
                   'default': lambda api, pr: api.add_response(
                       f'/repos/{api.repository}', {'full_name': api.repository, 'default_branch': 'other'})}
        for name, change in changes.items():
            api, identity, pr, merged, controller = self.seed()
            first = authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
            self.assertEqual(authenticate_merged_pr_identity(api, identity, controller_sha=controller,
                                                             merged_sha=merged), first)
            change(api, pr)
            with self.subTest(change=name):
                if name == 'merged-at':
                    self.assertNotEqual(authenticate_merged_pr_identity(
                        api, identity, controller_sha=controller, merged_sha=merged), first)
                else:
                    with self.assertRaises(MbError):
                        authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)

    def test_one_observation_reads_mutable_state_once_and_git_objects_once_per_command(self):
        api, identity, pr, merged, controller = self.seed()
        authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
        self.assertEqual(api.request_count, 7)  # repository, head, pull request; two commits, two ancestries
        reads = CommandReads.of(api)
        for _ in range(3):
            authenticate_merged_pr_identity(reads, identity, controller_sha=controller, merged_sha=merged)
        self.assertEqual(api.request_count, 7 + 3 * 3 + 4)

    def test_api_failure_is_not_missing_proof_or_full_run_fallback(self):
        for target in ('/pulls/7', '/git/commits/', '/compare/', '/branches/'):
            api, identity, pr, merged, controller = self.seed()
            original = api.get_json
            failure = MbError('unavailable API')
            def get(path, **kwargs):
                if target in path:
                    raise failure
                return original(path, **kwargs)
            with self.subTest(target=target), patch.object(api, 'get_json', side_effect=get), self.assertRaises(MbError) as caught:
                authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
            self.assertIs(caught.exception, failure)

    def test_identity_is_read_on_entry_and_validated_before_it_is_encoded(self):
        api, identity, pr, merged, controller = self.seed()
        expected = hashlib.sha256(canonical_json(identity)).hexdigest()
        original = api.get_json

        def get(path, **kwargs):
            identity['kit']['sha'] = 'f'*40  # the caller's value changes while the API is read
            return original(path, **kwargs)

        with patch.object(api, 'get_json', side_effect=get):
            result = authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
        self.assertEqual(result.identity_sha256, expected)
        api, identity, pr, merged, controller = self.seed()
        identity['head_branch'] = 'x'*1000
        with patch.object(authenticate, 'canonical_json') as encode, patch.object(api, 'get_json') as reads,                 self.assertRaises(MbError):
            authenticate_merged_pr_identity(api, identity, controller_sha=controller, merged_sha=merged)
        encode.assert_not_called()
        reads.assert_not_called()
