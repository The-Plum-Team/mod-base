"""Inert published-ref and complete source observations; no push/PR/Root/settlement proof."""

import copy
import hashlib
import unittest
from unittest.mock import patch

from mod_base.build_ci import batch, source
from mod_base.errors import MbError
from mod_base.github.api import ApiError, ApiNotFound
from mod_base.model.canonical import canonical_json
from tests import test_ci_batch_manifest_sources as fixtures


class BatchPublicationTests(unittest.TestCase):
    def seed(self, multiple=False):
        api, prs, controller, document, roots, records = fixtures.BatchManifestSourcesTests().seed(multiple)
        endpoint = f"/repos/{api.repository}/git/ref/heads/{document['branch']}"
        response = {'ref': 'refs/heads/'+document['branch'],
                    'object': {'type': 'commit', 'sha': document['members'][-1]['squash_sha']}}
        api.add_response(endpoint, response)
        return api, prs, controller, document, roots, records, endpoint, response

    def run_bound(self, api, controller, document, roots, **overrides):
        arguments = dict(controller_sha=controller, branch='batch/fixture',
                         commit_sha=document['members'][-1]['squash_sha'], profile='block-pops',
                         policy_sha256='1'*64, permitted_paths=('source',), source_roots=roots)
        arguments.update(overrides)
        return batch.authenticate_batch_publication(api, document, **arguments)

    def test_exact_ref_final_commit_tree_and_complete_source_reads(self):
        for multiple in (False, True):
            api, prs, controller, document, roots, records, endpoint, response = self.seed(multiple)
            counts = {name: 0 for name in records}
            def read(root, **kwargs):
                counts[root.name] += 1
                self.assertEqual(kwargs['tracked_paths'], ('source', 'unchanged'))
                self.assertEqual(kwargs['generated_roots'], ())
                return copy.deepcopy(records[root.name])
            with self.subTest(multiple=multiple), patch.object(source, 'source_records', side_effect=read), patch.object(api, 'get_json', wraps=api.get_json) as calls:
                observed = self.run_bound(api, controller, document, roots)
            self.assertEqual(observed.branch, document['branch'])
            self.assertEqual(observed.commit_sha, document['members'][-1]['squash_sha'])
            self.assertEqual(observed.result_tree, document['result_tree'])
            self.assertEqual(observed.sources.manifest_sha256, hashlib.sha256(canonical_json(document)).hexdigest())
            self.assertTrue(all(count == 4 for count in counts.values()))
            self.assertEqual(sum(call.args[0] == endpoint for call in calls.call_args_list), 2)
            self.assertEqual(api.mutations, [])

    def test_independent_original_branch_commit_policy_and_roots_preflight_before_io(self):
        for overrides in ({'branch': 'batch/substitute'}, {'branch': 'batch/.hidden'},
                          {'commit_sha': 'f'*40}, {'profile': 'quick-skin'},
                          {'policy_sha256': '2'*64}, {'source_roots': ()}, {'permitted_paths': ('../escape',)}):
            api, prs, controller, document, roots, records, endpoint, response = self.seed()
            with self.subTest(overrides=overrides), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots, **overrides)
            reads.assert_not_called()

    def test_ref_must_be_exact_commit_not_partial_alias_tag_or_malformed(self):
        for response in (None, [], {}, {'ref': 'refs/heads/batch/fixture'},
                         {'ref': 'refs/heads/batch/fixture-extra', 'object': {'type': 'commit', 'sha': f'{800:040x}'}},
                         {'ref': 'refs/heads/batch/fixture', 'object': {'type': 'tag', 'sha': f'{800:040x}'}},
                         {'ref': 'refs/heads/batch/fixture', 'object': {'type': 'commit', 'sha': 'f'*40}}):
            api, prs, controller, document, roots, records, endpoint, original = self.seed()
            api.add_response(endpoint, response)
            with self.subTest(response=response), patch.object(source, 'source_records') as files, self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)
            files.assert_not_called()

    def test_missing_permission_and_transport_errors_never_become_published(self):
        for error_type, status in ((ApiNotFound, 404), (ApiError, 403), (ApiError, 0), (ApiError, 429)):
            api, prs, controller, document, roots, records, endpoint, response = self.seed()
            original = api.get_json
            error = error_type('ref unavailable', status=status, method='GET', path=endpoint)
            def get(path, **kwargs):
                if path == endpoint:
                    raise error
                return original(path, **kwargs)
            with self.subTest(status=status), patch.object(api, 'get_json', side_effect=get), self.assertRaises(error_type) as caught:
                self.run_bound(api, controller, document, roots)
            self.assertIs(caught.exception, error)

    def test_result_tree_and_actual_single_parent_graph_must_agree(self):
        for tree, parents in (('f'*40, None), (None, []), (None, ['f'*40])):
            api, prs, controller, document, roots, records, endpoint, response = self.seed()
            member = document['members'][-1]
            api.add_commit(member['squash_sha'], tree or member['result_tree'],
                           parents=parents if parents is not None else [member['parent_sha']])
            with self.subTest(tree=tree, parents=parents), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaises(MbError):
                self.run_bound(api, controller, document, roots)

    def test_ref_movement_after_source_byte_pass_rejects(self):
        api, prs, controller, document, roots, records, endpoint, response = self.seed()
        original = api.get_json
        count = 0
        def get(path, **kwargs):
            nonlocal count
            if path == endpoint:
                count += 1
                if count == 2:
                    return {**response, 'object': {'type': 'commit', 'sha': 'f'*40}}
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'published batch ref differs'):
            self.run_bound(api, controller, document, roots)

    def test_member_change_inside_last_ref_read_cannot_return_stale_sources(self):
        api, prs, controller, document, roots, records, endpoint, response = self.seed(True)
        original = api.get_json
        count = 0
        def get(path, **kwargs):
            nonlocal count
            if path == endpoint:
                count += 1
                if count == 2:
                    prs[7]['draft'] = True
                    api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'after final published ref read'):
            self.run_bound(api, controller, document, roots)

    def test_snapshot_rejects_original_document_mutation_during_ref_inspection(self):
        api, prs, controller, document, roots, records, endpoint, response = self.seed()
        original = api.get_json
        def get(path, **kwargs):
            if path == endpoint:
                document['branch'] = 'batch/substituted'
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'caller manifest changed during publication'):
            self.run_bound(api, controller, document, roots)
