"""API and real source-verification logic with explicit authored source-record syscall seams."""

import copy
import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import source
from mod_base.build_ci.batch import verify_batch_patch_bytes
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests import test_ci_batch_binding as bindings
from tests import test_ci_batch_patch as patches


def record(path, data):
    item = patches.entry(path, data)
    return {'path': path, 'mode': item.mode, 'size': item.size, 'git_blob': item.git_blob,
            'sha256': hashlib.sha256(data).hexdigest()}


class BatchBytesTests(unittest.TestCase):
    def exercise(self, change=None, *, expected=None):
        api, prs, controller, common, common_tree, head_tree, endpoint, comparison = bindings.BatchBindingTests().seed()
        originals = {'before': [record('source', b'old'), record('unchanged', b'known')],
                     'after': [record('source', b'new'), record('unchanged', b'known')]}
        counts = {'before': 0, 'after': 0}
        def read(root, **kwargs):
            name = root.name
            counts[name] += 1
            self.assertEqual(kwargs['tracked_paths'], ('source', 'unchanged'))
            self.assertEqual(kwargs['generated_roots'], ())
            rows = copy.deepcopy(originals[name])
            if change is not None: change(api, prs, name, counts[name], rows, common_tree, head_tree)
            return rows
        with patch.object(source, 'source_records', side_effect=read):
            if expected:
                with self.assertRaisesRegex(MbError, expected):
                    verify_batch_patch_bytes(api, Path('before'), Path('after'), controller_sha=controller,
                                             pr_number=7, permitted_paths=('source',))
                return
            result = verify_batch_patch_bytes(api, Path('before'), Path('after'), controller_sha=controller,
                                              pr_number=7, permitted_paths=('source',))
        self.assertEqual(counts, {'before': 2, 'after': 2})
        self.assertEqual(api.mutations, [])
        for name, tree_sha, observed in (('before', common_tree, result.merge_base_bytes_sha256),
                                         ('after', head_tree, result.head_bytes_sha256)):
            digest = hashlib.sha256(canonical_json({'format': 'mod-base.batch-source-bytes-v1',
                                                    'tree_sha': tree_sha}))
            for row in originals[name]: digest.update(canonical_json(row))
            self.assertEqual(observed, digest.hexdigest())
        self.assertEqual(result.patch.member.generation.pr_number, 7)

    def test_complete_source_records_are_tree_bound_and_rechecked(self):
        self.exercise()

    def test_changed_and_unchanged_source_tampering_rejects_during_actual_verification(self):
        for target in (0, 1):
            def change(api, prs, name, count, rows, common_tree, head_tree):
                if name == 'after': rows[target] = record(rows[target]['path'], b'bad')
            with self.subTest(target=target): self.exercise(change, expected='tracked source differs')

    def test_post_inspection_byte_observation_drift_rejects_with_retained_git_metadata(self):
        def change(api, prs, name, count, rows, common_tree, head_tree):
            if name == 'after' and count == 2: rows[1]['sha256'] = 'f'*64
        self.exercise(change, expected='source bytes changed')

    def test_member_change_in_final_source_read_cannot_return_stale_observation(self):
        def change(api, prs, name, count, rows, common_tree, head_tree):
            if name == 'after' and count == 2:
                prs[7]['draft'] = True
                api.add_response(f'/repos/{api.repository}/pulls/7', prs[7])
        self.exercise(change, expected='after final byte inspection')

    def test_inventory_change_during_source_read_does_not_gain_byte_evidence(self):
        def change(api, prs, name, count, rows, common_tree, head_tree):
            if name == 'after' and count == 1:
                api.add_tree(head_tree, bindings.rows(patches.entry('source', b'new'),
                                                     patches.entry('unchanged', b'bad')))
        self.exercise(change, expected='outside native')

    def test_read_failure_propagates_without_git_or_account_operations(self):
        def change(api, prs, name, count, rows, common_tree, head_tree):
            raise MbError('source read unavailable')
        self.exercise(change, expected='source read unavailable')
        def malformed(api, prs, name, count, rows, common_tree, head_tree):
            rows[0]['sha256'] = 'bad'
        self.exercise(malformed, expected='SHA-256')
