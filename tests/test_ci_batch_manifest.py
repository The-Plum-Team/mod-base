"""Structural batch data and hostile decoding; no native/Linux/Git writer proof."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci import batch_schema
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_batch


def stack():
    document = ci_batch()
    second = copy.deepcopy(document['members'][0])
    second.update(pr_number=2, head_branch='feature/two', head_sha='9'*40, head_tree='a'*40,
                  draft=True, parent_sha=second['squash_sha'], squash_sha='b'*40, result_tree='c'*40)
    document['members'].append(second)
    document['result_tree'] = second['result_tree']
    return document


class BatchManifestTest(unittest.TestCase):
    def reject(self, document):
        with self.assertRaises(MbError):
            batch_schema.validate_batch_manifest(document)

    def test_ordered_stack_retains_drafts_and_original_object(self):
        document = stack()
        self.assertIs(batch_schema.validate_batch_manifest(document), document)
        self.assertEqual(load_document(canonical_json(document), kind=document['kind']), document)
        document['members'].reverse()
        self.reject(document)

    def test_closed_shapes_exact_types_versions_and_hashes(self):
        for target, field, value in (
                ('document', 'approval', True), ('document', 'schema_version', 2),
                ('document', 'policy_sha256', 'x'*64), ('document', 'profile', 'unknown'),
                ('member', 'pr_number', True), ('member', 'draft', 1),
                ('member', 'program', 'evil.py'), ('member', 'head_tree', 'f'*39),
                ('side', 'size', False), ('side', 'mode', '160000'), ('side', 'sha256', 'a'*64)):
            with self.subTest(field=field, value=value):
                document = ci_batch()
                container = document if target == 'document' else document['members'][0]
                if target == 'side':
                    container = container['patch'][0]['after']
                container[field] = value
                self.reject(document)

    def test_namespace_members_parents_results_and_forks(self):
        for target, field, value in (
                ('document', 'branch', 'feature/batch'), ('document', 'branch', 'batch/'),
                ('document', 'result_tree', 'd'*40), ('member', 'pr_number', 1),
                ('member', 'source_repository', 'someone/fork'), ('member', 'head_branch', 'master'),
                ('member', 'head_branch', 'batch/nested'), ('member', 'parent_sha', '1'*40),
                ('member', 'squash_sha', '7'*40), ('member', 'result_tree', '8'*40)):
            with self.subTest(field=field, value=value):
                document = stack()
                (document if target == 'document' else document['members'][1])[field] = value
                self.reject(document)
        document = ci_batch()
        document['members'][0]['squash_sha'] = document['members'][0]['head_sha']
        batch_schema.validate_batch_manifest(document)  # Legitimate identity equality is allowed.

    def test_patch_inventory_paths_order_case_alias_and_noops(self):
        for name in ('../escape', '.git/config', 'a/.GiT/config', '/absolute', 'a//b'):
            document = ci_batch()
            document['members'][0]['patch'][0]['path'] = name
            self.reject(document)
        for names in (('a', 'a'), ('b', 'a'), ('Src/a', 'src/b')):
            document = ci_batch()
            entry = document['members'][0]['patch'][0]
            document['members'][0]['patch'] = [{**copy.deepcopy(entry), 'path': name} for name in names]
            self.reject(document)
        for before, after in ((None, None), ({'mode': '100644', 'size': 0, 'git_blob': '5'*40},)*2):
            document = ci_batch()
            document['members'][0]['patch'][0].update(before=before, after=after)
            self.reject(document)

    def test_literal_links_mode_changes_additions_deletions_and_blob_sizes(self):
        for before, after in ((None, {'mode': '100644', 'size': 0, 'git_blob': 'a'*40}),
                              ({'mode': '100755', 'size': 0, 'git_blob': 'a'*40}, None),
                              ({'mode': '100644', 'size': 3, 'git_blob': 'a'*40},
                               {'mode': '100755', 'size': 3, 'git_blob': 'a'*40}),
                              (None, {'mode': '120000', 'size': 4, 'git_blob': 'a'*40})):
            document = ci_batch()
            document['members'][0]['patch'][0].update(before=before, after=after)
            batch_schema.validate_batch_manifest(document)
        for size in (0, limits.MAX_CI_SOURCE_LINK_BYTES+1):
            document = ci_batch()
            document['members'][0]['patch'][0]['after'].update(mode='120000', size=size)
            self.reject(document)
        document = stack()
        document['members'][1]['patch'][0]['after']['size'] = 4
        self.reject(document)  # Same declared blob cannot have two different sizes.

    def test_collection_preflight_caps_before_nested_validation(self):
        for members in ([], [ci_batch()['members'][0]]*(limits.MAX_CI_BATCH_MEMBERS+1)):
            document = ci_batch()
            document['members'] = members
            self.reject(document)
        document = stack()
        with patch.object(limits, 'MAX_CI_BATCH_PATCH_FILES', 1), patch.object(batch_schema, '_MEMBER') as nested:
            self.reject(document)
            nested.assert_not_called()

    def test_progressive_path_and_byte_caps(self):
        for constant, cap in (('MAX_CI_BATCH_PATCH_ENTRIES', 1), ('MAX_CI_BATCH_PATCH_PATH_BYTES', 1),
                              ('MAX_CI_SOURCE_TREE_BYTES', 2)):
            with self.subTest(constant=constant), patch.object(limits, constant, cap):
                self.reject(ci_batch())

    def test_strict_decoder_duplicate_nan_and_document_limit(self):
        raw = canonical_json(ci_batch())
        for bad in (raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
                    raw.replace(b'"schema_version":1', b'"schema_version":NaN')):
            with self.assertRaises(MbError):
                load_document(bad, kind='mod-base.ci.batch')
        from mod_base.model import documents
        with patch.dict(documents.MAX_DOCUMENT_BYTES, {'mod-base.ci.batch': len(raw)-1}):
            with self.assertRaises(MbError):
                load_document(raw, kind='mod-base.ci.batch')
