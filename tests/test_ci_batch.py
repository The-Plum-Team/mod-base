"""Ordered batch membership through inert API observations; no Git or PR mutation."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.batch import authenticate_batch_members
from mod_base.errors import MbError
from tests import test_ci_protocol as fixtures


class BatchMembersTests(unittest.TestCase):
    def seed(self, numbers=(7, 9)):
        plan, api, prototype = fixtures.seeded_pr()
        rows = {}
        for number in numbers:
            pr = copy.deepcopy(prototype)
            pr.update(number=number, draft=False, merge_commit_sha=None)
            pr['head'].update(ref=f'feature/{number}', sha=f'{number+100:040x}')
            tree = f'{number+200:040x}'
            api.add_commit(pr['head']['sha'], tree, parents=[plan['identity']['controller_sha']])
            api.add_response(f'/repos/{api.repository}/pulls/{number}', pr)
            rows[number] = pr
        return api, rows, plan['identity']['controller_sha']

    def test_members_retain_requested_order_ready_source_and_complete_head_tree(self):
        api, rows, controller = self.seed()
        with patch.object(api, 'get_json', wraps=api.get_json) as reads:
            members = authenticate_batch_members(api, controller_sha=controller, pr_numbers=(9, 7))
        self.assertEqual([m.generation.pr_number for m in members], [9, 7])
        self.assertEqual([m.head_tree for m in members], [f'{209:040x}', f'{207:040x}'])
        self.assertTrue(all(m.generation.base_sha == controller and not m.generation.draft for m in members))
        for call in reads.call_args_list:
            path = call.args[0]
            self.assertTrue(path.startswith(f'/repos/{api.repository}'))
            self.assertNotIn('/actions/', path)
            self.assertNotIn('/git/blobs/', path)

    def test_exact_50_member_boundary_and_invalid_inputs_before_api_reads(self):
        api, rows, controller = self.seed(tuple(range(1, 51)))
        self.assertEqual(len(authenticate_batch_members(api, controller_sha=controller,
                                                      pr_numbers=tuple(range(1, 51)))), 50)
        for numbers in ((), [], (True,), (0,), (-1,), ('7',), (7, 7), tuple(range(1, 52))):
            with self.subTest(numbers=numbers), patch.object(api, 'get_json') as reads:
                with self.assertRaises(MbError):
                    authenticate_batch_members(api, controller_sha=controller, pr_numbers=numbers)
                reads.assert_not_called()
        with patch.object(api, 'get_json') as reads:
            with self.assertRaises(MbError):
                authenticate_batch_members(api, controller_sha='bad', pr_numbers=(7,))
            reads.assert_not_called()

    def test_closed_fork_nested_batch_base_branch_wrong_base_and_bad_head_commit_reject(self):
        changes = (lambda p: p.update(state='closed'),
                   lambda p: p['head']['repo'].update(full_name='fork/mod'),
                   lambda p: p['base'].update(sha='f'*40), lambda p: p['base'].update(ref='other'),
                   lambda p: p['head'].update(ref='batch/nested'),
                   lambda p: p['head'].update(ref=p['base']['ref']))
        for change in changes:
            api, rows, controller = self.seed()
            change(rows[7])
            api.add_response(f'/repos/{api.repository}/pulls/7', rows[7])
            with self.subTest(change=change), self.assertRaises(MbError):
                authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))
        api, rows, controller = self.seed()
        api.add_response(f"/repos/{api.repository}/git/commits/{rows[7]['head']['sha']}",
                         {'sha': 'f'*40, 'tree': {'sha': 'e'*40}})
        with self.assertRaises(MbError):
            authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))

    def test_prior_member_movement_during_later_member_read_is_not_hidden(self):
        for mutation in (lambda p: p.update(draft=True), lambda p: p.update(state='closed'),
                         lambda p: p['head'].update(sha='f'*40),
                         lambda p: p['head'].update(ref='renamed')):
            api, rows, controller = self.seed()
            original = api.get_json
            triggered = False
            def read(path, **kwargs):
                nonlocal triggered
                if path.endswith('/pulls/9') and not triggered:
                    triggered = True
                    mutation(rows[7])
                    api.add_response(f'/repos/{api.repository}/pulls/7', rows[7])
                return original(path, **kwargs)
            with self.subTest(mutation=mutation), patch.object(api, 'get_json', side_effect=read):
                with self.assertRaises(MbError):
                    authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))
            self.assertTrue(triggered)

    def test_head_tree_drift_on_recheck_rejects_even_with_same_head_sha(self):
        api, rows, controller = self.seed()
        original = api.get_json
        path = f"/repos/{api.repository}/git/commits/{rows[7]['head']['sha']}"
        count = 0
        def read(endpoint, **kwargs):
            nonlocal count
            result = original(endpoint, **kwargs)
            if endpoint == path:
                count += 1
                if count == 2:
                    result = copy.deepcopy(result)
                    result['tree']['sha'] = 'f'*40
            return result
        with patch.object(api, 'get_json', side_effect=read):
            with self.assertRaisesRegex(MbError, 'source changed'):
                authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))
        self.assertEqual(count, 2)

    def test_mixed_draft_and_ready_members_preserve_source_readiness_without_authorizing_gates(self):
        api, rows, controller = self.seed()
        rows[7]['draft'] = True
        api.add_response(f'/repos/{api.repository}/pulls/7', rows[7])
        members = authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))
        self.assertEqual([member.generation.draft for member in members], [True, False])

    def test_readiness_change_during_head_tree_read_is_rejected(self):
        api, rows, controller = self.seed()
        original = api.get_json
        path = f"/repos/{api.repository}/git/commits/{rows[7]['head']['sha']}"
        def read(endpoint, **kwargs):
            result = original(endpoint, **kwargs)
            if endpoint == path:
                rows[7]['draft'] = True
                api.add_response(f'/repos/{api.repository}/pulls/7', rows[7])
            return result
        with patch.object(api, 'get_json', side_effect=read):
            with self.assertRaisesRegex(MbError, 'changed during head-tree'):
                authenticate_batch_members(api, controller_sha=controller, pr_numbers=(7, 9))
