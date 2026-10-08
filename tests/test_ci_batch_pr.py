"""Ready batch PR/source/merge binding with inert API and explicit known-byte filesystem seams."""

import copy
import hashlib
import unittest
from unittest.mock import patch

from mod_base.build_ci import batch, source
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests import test_ci_batch_publication as publications
from tests.helpers import ci_plan


class BatchPrTests(unittest.TestCase):
    def seed(self, multiple=False):
        api, prs, controller, document, roots, records, endpoint, response = publications.BatchPublicationTests().seed(multiple)
        identity = ci_plan()['identity']
        head = document['members'][-1]['squash_sha']
        merge = f'{1200:040x}'
        identity.update(pr_number=42, head_sha=head, head_branch=document['branch'],
                        base_sha=controller, base_branch=document['base_branch'], controller_sha=controller,
                        tested_sha=merge, tested_tree=document['result_tree'], tested_parents=[controller, head],
                        policy_sha256=document['policy_sha256'])
        pr = copy.deepcopy(prs[7])
        pr.update(number=42, draft=False, merge_commit_sha=merge)
        pr['head'].update(ref=document['branch'], sha=head)
        api.add_response(f'/repos/{api.repository}/pulls/42', pr)
        api.add_commit(merge, document['result_tree'], parents=[controller, head])
        return api, prs, pr, controller, document, identity, roots, records

    def run_bound(self, api, controller, document, identity, roots):
        return batch.authenticate_batch_pr(api, document, identity, controller_sha=controller,
                                          profile='block-pops', policy_sha256='1'*64,
                                          permitted_paths=('source',), source_roots=roots)

    def test_ready_batch_has_exact_manifest_publication_members_and_merge(self):
        for multiple in (False, True):
            api, prs, pr, controller, document, identity, roots, records = self.seed(multiple)
            counts = {name: 0 for name in records}
            def read(root, **kwargs):
                counts[root.name] += 1
                self.assertEqual(kwargs['tracked_paths'], ('source', 'unchanged'))
                self.assertEqual(kwargs['generated_roots'], ())
                return copy.deepcopy(records[root.name])
            with self.subTest(multiple=multiple), patch.object(source, 'source_records', side_effect=read):
                result = self.run_bound(api, controller, document, identity, roots)
            self.assertEqual(result.generation.pr_number, 42)
            self.assertFalse(result.generation.draft)
            self.assertEqual(result.generation.merge_sha, identity['tested_sha'])
            self.assertEqual(result.identity_sha256, hashlib.sha256(canonical_json(identity)).hexdigest())
            self.assertEqual(result.publication.commit_sha, identity['head_sha'])
            self.assertEqual(result.publication.result_tree, identity['tested_tree'])
            self.assertTrue(all(count == 4 for count in counts.values()))
            self.assertEqual(api.mutations, [])

    def test_identity_manifest_mismatch_and_member_number_fail_before_api(self):
        for field, value in (('pr_number', 7), ('repository', 'foreign/mod'),
                             ('head_branch', 'batch/other'), ('base_branch', 'other'),
                             ('policy_sha256', 'f'*64), ('tested_tree', 'f'*40), ('approval', True)):
            api, prs, pr, controller, document, identity, roots, records = self.seed()
            identity[field] = value
            with self.subTest(field=field), patch.object(api, 'get_json') as reads, self.assertRaises(MbError):
                self.run_bound(api, controller, document, identity, roots)
            reads.assert_not_called()

    def test_draft_closed_fork_wrong_branch_head_base_and_missing_merge_reject(self):
        for change in (lambda pr: pr.update(draft=True), lambda pr: pr.update(state='closed'),
                       lambda pr: pr['head']['repo'].update(full_name='fork/mod'),
                       lambda pr: pr['head'].update(ref='batch/other'), lambda pr: pr['head'].update(sha='f'*40),
                       lambda pr: pr['base'].update(sha='f'*40), lambda pr: pr.update(merge_commit_sha=None)):
            api, prs, pr, controller, document, identity, roots, records = self.seed()
            change(pr)
            api.add_response(f'/repos/{api.repository}/pulls/42', pr)
            with self.subTest(change=change), patch.object(source, 'source_records') as files, self.assertRaises(MbError):
                self.run_bound(api, controller, document, identity, roots)
            files.assert_not_called()

    def test_actual_synthetic_merge_tree_and_exact_ordered_parents(self):
        for tree, parents in (('f'*40, None), (None, []), (None, ['f'*40]), (None, 'reversed')):
            api, prs, pr, controller, document, identity, roots, records = self.seed()
            expected = identity['tested_parents']
            api.add_commit(identity['tested_sha'], tree or identity['tested_tree'],
                           parents=expected if parents is None else list(reversed(expected)) if parents == 'reversed' else parents)
            with self.subTest(tree=tree, parents=parents), patch.object(source, 'source_records') as files, self.assertRaises(MbError):
                self.run_bound(api, controller, document, identity, roots)
            files.assert_not_called()

    def test_batch_readiness_or_merge_movement_during_source_copy_rejects(self):
        for field, value in (('draft', True), ('merge_commit_sha', 'f'*40)):
            api, prs, pr, controller, document, identity, roots, records = self.seed()
            def read(root, **kwargs):
                pr[field] = value
                api.add_response(f'/repos/{api.repository}/pulls/42', pr)
                return copy.deepcopy(records[root.name])
            with self.subTest(field=field), patch.object(source, 'source_records', side_effect=read), self.assertRaises(MbError):
                self.run_bound(api, controller, document, identity, roots)

    def test_source_member_change_during_closing_batch_merge_read_rejects(self):
        api, prs, pr, controller, document, identity, roots, records = self.seed()
        original = api.get_json
        count = 0
        endpoint = f"/repos/{api.repository}/git/commits/{identity['tested_sha']}"
        def get(path, **kwargs):
            nonlocal count
            if path == endpoint:
                count += 1
                if count == 2:
                    prs[7]['draft'] = True
                    api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
            return original(path, **kwargs)
        with patch.object(api, 'get_json', side_effect=get), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'source members changed'):
            self.run_bound(api, controller, document, identity, roots)

    def test_batch_change_during_closing_source_collection_rejects(self):
        api, prs, pr, controller, document, identity, roots, records = self.seed()
        original_members = batch.authenticate_batch_members
        completed_merge_reads = 0
        original_get = api.get_json
        def get(path, **kwargs):
            nonlocal completed_merge_reads
            if path.endswith('/git/commits/'+identity['tested_sha']):
                completed_merge_reads += 1
            return original_get(path, **kwargs)
        def members(*args, **kwargs):
            result = original_members(*args, **kwargs)
            if completed_merge_reads == 2:
                pr['draft'] = True
                api.add_response(f'/repos/{api.repository}/pulls/42', pr)
            return result
        with patch.object(api, 'get_json', side_effect=get), patch.object(batch, 'authenticate_batch_members', side_effect=members), patch.object(source, 'source_records', side_effect=lambda root, **kw: copy.deepcopy(records[root.name])), self.assertRaisesRegex(MbError, 'batch PR readiness/source/merge changed'):
            self.run_bound(api, controller, document, identity, roots)

    def test_caller_identity_or_manifest_mutation_cannot_replace_snapshot(self):
        for target in ('identity', 'manifest'):
            api, prs, pr, controller, document, identity, roots, records = self.seed()
            def read(root, **kwargs):
                if target == 'identity':
                    identity['kit']['sha'] = 'f'*40
                else:
                    document['branch'] = 'batch/substituted'
                return copy.deepcopy(records[root.name])
            with self.subTest(target=target), patch.object(source, 'source_records', side_effect=read), self.assertRaisesRegex(MbError, 'caller manifest/identity changed'):
                self.run_bound(api, controller, document, identity, roots)
