"""Pure batch patch identities from authored blobs; no native/tree/writer authority is mocked in."""

import dataclasses
import hashlib
import unittest
from unittest.mock import patch

from mod_base.build_ci.batch import derive_batch_patch_inventory
from mod_base.build_ci.source import GitSourceEntry, validate_source_inventory
from mod_base.errors import MbError
from mod_base.model import limits


def entry(path, data=b'known', mode='100644'):
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()
    return GitSourceEntry(path, mode, len(data), blob)


class BatchPatchTests(unittest.TestCase):
    def test_add_delete_modify_and_unchanged_protected_paths_are_exact(self):
        keep = entry('.github/unchanged.yml')
        removed, old = entry('remove', b'gone'), entry('update', b'old')
        added, new = entry('add', b'added'), entry('update', b'new')
        result = derive_batch_patch_inventory(before=(keep, removed, old), after=(keep, added, new),
                                             permitted_paths=('add', 'remove', 'update'))
        self.assertEqual([(row.path, row.before, row.after) for row in result],
                         [('add', None, added), ('remove', removed, None), ('update', old, new)])

    def test_mode_only_changes_and_literal_link_identities_remain_visible(self):
        old = entry('source')
        for new in (dataclasses.replace(old, mode='100755'), entry('source', b'../opaque', '120000')):
            with self.subTest(mode=new.mode):
                rows = derive_batch_patch_inventory(before=(old,), after=(new,), permitted_paths=('source',))
                self.assertEqual((rows[0].before, rows[0].after), (old, new))

    def test_rename_is_explicit_deletion_and_addition_with_both_paths_required(self):
        old = entry('old')
        new = dataclasses.replace(old, path='new')
        rows = derive_batch_patch_inventory(before=(old,), after=(new,), permitted_paths=('new', 'old'))
        self.assertEqual([(row.path, row.before is None, row.after is None) for row in rows],
                         [('new', True, False), ('old', False, True)])
        for policy in (('old',), ('new',)):
            with self.subTest(policy=policy), self.assertRaisesRegex(MbError, 'outside native'):
                derive_batch_patch_inventory(before=(old,), after=(new,), permitted_paths=policy)

    def test_no_op_and_unknown_changed_path_fail_closed(self):
        before, after = (entry('source', b'old'),), (entry('source', b'new'),)
        with self.assertRaisesRegex(MbError, 'no-op'):
            derive_batch_patch_inventory(before=before, after=before, permitted_paths=('source',))
        with self.assertRaisesRegex(MbError, 'outside native'):
            derive_batch_patch_inventory(before=before, after=after, permitted_paths=('other',))

    def test_hostile_policy_tuple_order_paths_and_aliases_are_rejected(self):
        before, after = (entry('source', b'old'),), (entry('source', b'new'),)
        for policy in ([], (), ('source', 'source'), ('z', 'a'), ('../source',), (True,),
                       ('.git/config',), ('Source', 'source'), ('A/x', 'a/y')):
            with self.subTest(policy=policy), self.assertRaises(MbError):
                derive_batch_patch_inventory(before=before, after=after, permitted_paths=policy)
        with patch.object(limits, 'MAX_CI_BATCH_PATCH_FILES', 1):
            with self.assertRaises(MbError):
                derive_batch_patch_inventory(before=before, after=after, permitted_paths=('other', 'source'))
        with patch.object(limits, 'MAX_CI_BATCH_PATCH_PATH_BYTES', 1):
            with self.assertRaisesRegex(MbError, 'byte cap'):
                derive_batch_patch_inventory(before=before, after=after, permitted_paths=('source',))
        with patch.object(limits, 'MAX_CI_BATCH_PATCH_ENTRIES', 2):
            with self.assertRaisesRegex(MbError, 'entry cap'):
                derive_batch_patch_inventory(before=before, after=after, permitted_paths=('a/x', 'b/y'))

    def test_complete_source_inventory_validation_precedes_comparison(self):
        good = (entry('source'),)
        for invalid in ((), [], (None,), (entry('z'), entry('a')), (entry('a'), entry('a')),
                        (entry('A/x'), entry('a/y')), (entry('a'), entry('a/x')),
                        (entry('../escape'),), (dataclasses.replace(good[0], size=True),),
                        (dataclasses.replace(good[0], mode='160000'),)):
            for side in ('before', 'after'):
                arguments = {'before': good, 'after': good, 'permitted_paths': ('source',)}
                arguments[side] = invalid
                with self.subTest(side=side, invalid=invalid), self.assertRaises(MbError):
                    derive_batch_patch_inventory(**arguments)
        self.assertIsNone(validate_source_inventory(good))

    def test_same_blob_size_contradiction_rejects_across_different_paths(self):
        old = entry('old')
        new = dataclasses.replace(old, path='new', size=old.size+1)
        with self.assertRaisesRegex(MbError, 'inconsistent declared sizes'):
            derive_batch_patch_inventory(before=(old,), after=(new,), permitted_paths=('new', 'old'))

    def test_source_entry_cap_stops_before_validating_further_paths(self):
        from mod_base.model import grammar
        inventory = (entry('a/x'), entry('b/y'), entry('z', mode='160000'))
        with patch.object(limits, 'MAX_CI_SOURCE_ENTRIES', 3), \
                patch.object(grammar, 'is_repo_path', wraps=grammar.is_repo_path) as paths:
            self.assertIsNone(validate_source_inventory(inventory[:1]))
            paths.reset_mock()
            with self.assertRaisesRegex(MbError, 'entry cap'):
                validate_source_inventory(inventory)
            self.assertEqual([call.args[0] for call in paths.call_args_list], ['a/x', 'b/y'])
