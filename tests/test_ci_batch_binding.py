"""Inert API batch merge-base/tree binding; no Git, candidate imports, bytes or writer grant."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.batch import authenticate_batch_patch
from mod_base.build_ci.source import read_source_tree_inventory
from mod_base.errors import MbError
from mod_base.github.contents import exact_tree, tree
from mod_base.model import limits
from tests import test_ci_batch as members
from tests import test_ci_batch_patch as patches
from tests import test_ci_controller_activation as activations


def rows(*entries):
    return [{'path': e.path, 'mode': e.mode, 'type': 'blob', 'sha': e.git_blob, 'size': e.size}
            for e in entries]


class BatchBindingTests(unittest.TestCase):
    def seed(self):
        api, prs, controller = members.BatchMembersTests().seed((7,))
        head = prs[7]['head']['sha']
        head_tree = f'{207:040x}'
        common, common_tree = 'b'*40, 'c'*40
        api.add_commit(common, common_tree, parents=[])
        shared = patches.entry('unchanged')
        api.add_tree(common_tree, rows(patches.entry('source', b'old'), shared))
        api.add_tree(head_tree, rows(patches.entry('source', b'new'), shared))
        comparison = {'status': 'diverged', 'ahead_by': 1, 'behind_by': 2,
                      'base_commit': {'sha': controller},
                      'merge_base_commit': {'sha': common, 'commit': {'tree': {'sha': common_tree}}},
                      'files': [{'filename': '../../ignored-partial-API-file', 'patch': 'ignored'}]}
        endpoint = f'/repos/{api.repository}/compare/{controller}...{head}'
        api.add_response(endpoint, comparison, params={'per_page': 1})
        for tip in (controller, head):
            api.add_compare(common, tip, {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0})
        return api, prs, controller, common, common_tree, head_tree, endpoint, comparison

    def run_patch(self, api, controller):
        return authenticate_batch_patch(api, controller_sha=controller, pr_number=7,
                                        permitted_paths=('source',))

    def test_complete_merge_base_patch_ignores_partial_compare_files_and_controller_tree(self):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = self.seed()
        prs[7]['draft'] = True
        api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
        with patch.object(api, 'get_json', wraps=api.get_json) as reads:
            result = self.run_patch(api, controller)
        self.assertEqual((result.merge_base_sha, result.merge_base_tree), (common, common_tree))
        self.assertTrue(result.member.generation.draft)
        self.assertEqual(result.changes[0].before, patches.entry('source', b'old'))
        self.assertEqual(result.changes[0].after, patches.entry('source', b'new'))
        tree_reads = [c.args[0] for c in reads.call_args_list if '/git/trees/' in c.args[0]]
        self.assertEqual(tree_reads, [f'/repos/{api.repository}/git/trees/{sha}'
                                      for sha in (common_tree, head_tree, common_tree, head_tree)])
        self.assertEqual(api.mutations, [])

    def test_malformed_compare_unreachable_merge_base_and_wrong_object_fail_closed(self):
        changes = (lambda c: c.update(status='unknown'), lambda c: c.update(ahead_by=True),
                   lambda c: c['base_commit'].update(sha='f'*40),
                   lambda c: c.update(merge_base_commit=None),
                   lambda c: c['merge_base_commit'].update(sha=True),
                   lambda c: c['merge_base_commit']['commit'].update(tree=None))
        for change in changes:
            api, prs, controller, common, common_tree, head_tree, endpoint, comparison = self.seed()
            change(comparison); api.add_response(endpoint, comparison, params={'per_page': 1})
            with self.subTest(change=change), self.assertRaises(MbError): self.run_patch(api, controller)
        for variant in ('wrong-tree', 'unreachable'):
            api, prs, controller, common, common_tree, head_tree, endpoint, comparison = self.seed()
            if variant == 'wrong-tree': api.add_commit(common, 'f'*40, parents=[])
            else: api.add_compare(common, controller, {'status': 'diverged', 'ahead_by': 1, 'behind_by': 1})
            with self.subTest(variant=variant), self.assertRaises(MbError): self.run_patch(api, controller)

    def test_member_change_during_tree_read_is_not_hidden_by_immutable_objects(self):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = self.seed()
        original = api.get_json
        def read(path, **kwargs):
            value = original(path, **kwargs)
            if path.endswith('/git/trees/'+common_tree):
                prs[7]['draft'] = True
                api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
            return value
        with patch.object(api, 'get_json', side_effect=read), self.assertRaisesRegex(MbError, 'live batch member'):
            self.run_patch(api, controller)

    def test_same_tree_sha_inventory_drift_and_merge_base_drift_reject(self):
        for target in ('tree', 'merge-base'):
            api, prs, controller, common, common_tree, head_tree, endpoint, comparison = self.seed()
            original = api.get_json; count = 0
            def read(path, **kwargs):
                nonlocal count
                value = original(path, **kwargs)
                selected = path.endswith('/git/trees/'+head_tree) if target == 'tree' else path == endpoint
                if selected:
                    count += 1
                    if count == 2:
                        value = copy.deepcopy(value)
                        if target == 'tree': value['tree'][0]['sha'] = 'f'*40
                        else: value['ahead_by'] += 1
                return value
            with self.subTest(target=target), patch.object(api, 'get_json', side_effect=read), self.assertRaises(MbError):
                self.run_patch(api, controller)
            self.assertEqual(count, 2)

    def test_unknown_path_and_invalid_policy_do_not_gain_success(self):
        api, prs, controller, *_ = self.seed()
        with self.assertRaisesRegex(MbError, 'outside native'):
            authenticate_batch_patch(api, controller_sha=controller, pr_number=7, permitted_paths=('other',))
        for policy in ([], (), ('source', 'source'), ('A/x', 'a/y')):
            with self.subTest(policy=policy), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                authenticate_batch_patch(api, controller_sha=controller, pr_number=7, permitted_paths=policy)
            reads.assert_not_called()


class ExactSourceTreeTests(unittest.TestCase):
    def test_original_controller_inventory_refuses_the_same_commit_alias_escape(self):
        plan, api, paths, source_rows, raw = activations.ControllerActivationTests().fixture()
        original_tree = '8'*40
        api.add_commit(original_tree, 'e'*40, parents=[])
        api.add_response(f'/repos/{api.repository}/git/trees/{original_tree}',
                         {'sha': 'e'*40, 'truncated': False, 'tree': source_rows}, params={'recursive': 1})
        with self.assertRaises(MbError):
            activations.controller.authenticate_controller_activation(api, identity=plan['identity'],
                                                                      protected_paths=paths)

    def test_legacy_commit_alias_is_preserved_but_exact_tree_and_source_refuse_it(self):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = BatchBindingTests().seed()
        api.add_commit(head_tree, 'e'*40, parents=[])
        api.add_response(f'/repos/{api.repository}/git/trees/{head_tree}',
                         {'sha': 'e'*40, 'truncated': False, 'tree': rows(patches.entry('source'))}, params={'recursive': 1})
        self.assertEqual(tree(api, head_tree)[0]['path'], 'source')
        for operation in (lambda: exact_tree(api, head_tree),
                          lambda: read_source_tree_inventory(api, tree_sha=head_tree)):
            with patch.object(api, 'get_json', wraps=api.get_json) as reads, self.assertRaises(MbError): operation()
            self.assertEqual(len(reads.call_args_list), 1)

    def test_incomplete_directory_closure_and_truncation_are_fatal(self):
        for truncated in (False, True):
            api, prs, controller, common, common_tree, head_tree, endpoint, comparison = BatchBindingTests().seed()
            api.add_response(f'/repos/{api.repository}/git/trees/{head_tree}',
                             {'sha': head_tree, 'truncated': truncated, 'tree': rows(patches.entry('dir/source'))}, params={'recursive': 1})
            with self.subTest(truncated=truncated), self.assertRaises(MbError):
                read_source_tree_inventory(api, tree_sha=head_tree)

    def test_source_prefix_cap_rejects_before_directory_inference(self):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = BatchBindingTests().seed()
        api.add_tree(head_tree, rows(patches.entry('a/b/c/d')))
        with patch.object(limits, 'MAX_CI_SOURCE_ENTRIES', 4), self.assertRaisesRegex(MbError, 'entry cap'):
            read_source_tree_inventory(api, tree_sha=head_tree)
