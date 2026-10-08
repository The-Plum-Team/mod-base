"""Actual inert API/source verification with explicit known-byte filesystem seams."""

import copy
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import batch, source
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests import test_ci_batch_binding as bindings, test_ci_batch_bytes as byte_fixtures
from tests.helpers import ci_batch


class BatchManifestSourcesTests(unittest.TestCase):
    def seed(self, multiple=False):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = bindings.BatchBindingTests().seed()
        if multiple:
            second = copy.deepcopy(prs[7])
            second.update(number=9, draft=True)
            second['head'].update(ref='feature/9', sha=f'{109:040x}')
            prs[9] = second
            api.add_response(f'/repos/{api.repository}/pulls/9', second)
            api.add_commit(second['head']['sha'], head_tree, parents=[controller])
            api.add_response(f"/repos/{api.repository}/compare/{controller}...{second['head']['sha']}",
                             comparison, params={'per_page': 1})
            api.add_compare(common, second['head']['sha'], {'status': 'ahead', 'ahead_by': 1, 'behind_by': 0})
        numbers = (7, 9) if multiple else (7,)
        live = batch.authenticate_batch_members(api, controller_sha=controller, pr_numbers=numbers)
        document = ci_batch()
        document.update(repository=api.repository, base_sha=controller, base_tree=live[0].generation.controller_tree,
                        base_branch=live[0].generation.base_branch)
        before = [byte_fixtures.record('source', b'old'), byte_fixtures.record('unchanged', b'known')]
        after = [byte_fixtures.record('source', b'new'), byte_fixtures.record('unchanged', b'known')]
        def digest(tree, records):
            value = hashlib.sha256(canonical_json({'format': 'mod-base.batch-source-bytes-v1', 'tree_sha': tree}))
            for row in records:
                value.update(canonical_json(row))
            return value.hexdigest()
        prototype = document['members'][0]
        document['members'] = []
        roots, records = [], {}
        parent = controller
        for index, member in enumerate(live):
            declared = copy.deepcopy(prototype)
            squash, result = f'{800+index:040x}', f'{900+index:040x}'
            generation = member.generation
            declared.update(pr_number=generation.pr_number, source_repository=api.repository,
                            head_branch=generation.head_branch, head_sha=generation.head_sha,
                            head_tree=head_tree, draft=generation.draft, merge_base_sha=common,
                            merge_base_tree=common_tree, merge_base_bytes_sha256=digest(common_tree, before),
                            head_bytes_sha256=digest(head_tree, after), parent_sha=parent,
                            squash_sha=squash, result_tree=result)
            declared['patch'] = [{'path': 'source',
                                  'before': {key: before[0][key] for key in ('mode', 'size', 'git_blob')},
                                  'after': {key: after[0][key] for key in ('mode', 'size', 'git_blob')}}]
            document['members'].append(declared)
            api.add_commit(squash, result, parents=[parent])
            api.add_tree(result, [{**{key: row[key] for key in ('path', 'mode', 'size')},
                                   'type': 'blob', 'sha': row['git_blob']} for row in after])
            parent = squash
            pair = (Path(f'before{index}'), Path(f'after{index}'))
            roots.append(pair)
            records[pair[0].name], records[pair[1].name] = before, after
        document['result_tree'] = document['members'][-1]['result_tree']
        return api, prs, controller, document, tuple(roots), records

    def run_bound(self, api, controller, document, roots):
        return batch.verify_batch_manifest_sources(api, document, controller_sha=controller,
                                                   profile='block-pops', policy_sha256='1'*64,
                                                   permitted_paths=('source',), source_roots=roots)

    def test_complete_ordered_sources_bytes_graph_and_draft_observations(self):
        for multiple in (False, True):
            api, prs, controller, document, roots, records = self.seed(multiple)
            counts = {name: 0 for name in records}
            def read(root, **kwargs):
                counts[root.name] += 1
                self.assertEqual(kwargs['tracked_paths'], ('source', 'unchanged'))
                self.assertEqual(kwargs['generated_roots'], ())
                return copy.deepcopy(records[root.name])
            with self.subTest(multiple=multiple), patch.object(source, 'source_records', side_effect=read):
                receipt = self.run_bound(api, controller, document, roots)
            self.assertEqual(receipt.manifest_sha256, hashlib.sha256(canonical_json(document)).hexdigest())
            self.assertEqual([item.patch.member.generation.pr_number for item in receipt.members], list(prs))
            self.assertTrue(all(count == 4 for count in counts.values()))
            self.assertEqual(api.mutations, [])

    def test_preflight_mismatch_and_root_shape_stop_before_api_or_source_reads(self):
        for field, value in (('profile', 'quick-skin'), ('policy_sha256', '2'*64),
                             ('repository', 'foreign/mod'), ('base_sha', 'f'*40), ('program', 'evil.py')):
            api, prs, controller, document, roots, records = self.seed()
            document[field] = value
            with self.subTest(field=field), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)
            reads.assert_not_called()
        for roots in ((), [], ((Path('before'),),), (('before', 'after'),)):
            api, prs, controller, document, _, records = self.seed()
            with self.subTest(roots=roots), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)
            reads.assert_not_called()

    def test_valid_shaped_forged_live_source_tree_patch_and_byte_claims_reject(self):
        for target, field, value in (
                ('top', 'base_branch', 'other'), ('top', 'base_tree', 'f'*40),
                ('member', 'head_branch', 'renamed'), ('member', 'head_sha', 'f'*40),
                ('member', 'head_tree', 'f'*40), ('member', 'draft', True),
                ('member', 'merge_base_sha', 'f'*40), ('member', 'merge_base_tree', 'f'*40),
                ('member', 'head_bytes_sha256', 'f'*64), ('member', 'merge_base_bytes_sha256', 'f'*64),
                ('patch', 'path', 'other')):
            api, prs, controller, document, roots, records = self.seed()
            container = document if target == 'top' else document['members'][0]
            if target == 'patch':
                container = container['patch'][0]
            container[field] = value
            with self.subTest(field=field), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)

    def test_actual_squash_object_tree_and_exact_single_parent_are_required(self):
        for change in (lambda value: value.update(sha='f'*40),
                       lambda value: value.update(tree={'sha': 'f'*40}),
                       lambda value: value.update(parents=[]), lambda value: value.update(parents=None),
                       lambda value: value.update(parents=[{'sha': 'f'*40}]),
                       lambda value: value['parents'].append({'sha': 'f'*40})):
            api, prs, controller, document, roots, records = self.seed()
            member = document['members'][0]
            endpoint = f"/repos/{api.repository}/git/commits/{member['squash_sha']}"
            value = api.get_json(endpoint)
            change(value)
            api.add_response(endpoint, value)
            with self.subTest(change=change), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)

    def test_result_inventory_drift_between_passes_is_not_hidden_by_tree_sha(self):
        api, prs, controller, document, roots, records = self.seed()
        result = document['result_tree']
        original = api.get_json
        count = 0
        def get(path, **kwargs):
            nonlocal count
            if path.endswith('/git/trees/'+result):
                count += 1
                if count == 2:
                    api.add_tree(result, [{'path': 'other', 'mode': '100644', 'type': 'blob', 'size': 1, 'sha': 'f'*40}])
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'result inventories changed'):
            self.run_bound(api, controller, document, roots)

    def test_prior_member_movement_during_later_source_copy_rejects(self):
        api, prs, controller, document, roots, records = self.seed(True)
        def read(root, **kwargs):
            if root.name == 'after1':
                prs[7]['draft'] = True
                api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
            return copy.deepcopy(records[root.name])
        with patch.object(source, 'source_records', side_effect=read), self.assertRaisesRegex(MbError, 'complete manifest membership changed'):
            self.run_bound(api, controller, document, roots)

    def test_caller_manifest_mutation_cannot_change_retained_digest(self):
        api, prs, controller, document, roots, records = self.seed()
        def read(root, **kwargs):
            document['branch'] = 'batch/substituted'
            return copy.deepcopy(records[root.name])
        with patch.object(source, 'source_records', side_effect=read), self.assertRaisesRegex(MbError, 'caller manifest changed'):
            self.run_bound(api, controller, document, roots)

    def test_unchanged_source_tampering_in_second_complete_pass_rejects(self):
        api, prs, controller, document, roots, records = self.seed()
        counts = {}
        def read(root, **kwargs):
            counts[root.name] = counts.get(root.name, 0)+1
            rows = copy.deepcopy(records[root.name])
            if root.name == 'after0' and counts[root.name] == 3:
                rows[1] = byte_fixtures.record('unchanged', b'tampered')
            return rows
        with patch.object(source, 'source_records', side_effect=read), self.assertRaisesRegex(MbError, 'tracked source differs'):
            self.run_bound(api, controller, document, roots)

    def test_mutated_oversized_manifest_is_revalidated_before_encoding(self):
        api, prs, controller, document, roots, records = self.seed()
        def read(root, **kwargs):
            document['branch'] = 'batch/'+'x'*200
            return copy.deepcopy(records[root.name])
        def encode(value):
            if value is document and len(value['branch']) > 200:
                self.fail('oversized mutated manifest reached canonical encoding')
            return canonical_json(value)
        with patch.object(source, 'source_records', side_effect=read), patch.object(batch, 'canonical_json', side_effect=encode), self.assertRaises(MbError):
            self.run_bound(api, controller, document, roots)
