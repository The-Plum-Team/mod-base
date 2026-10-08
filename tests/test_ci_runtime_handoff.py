"""Runtime read grants retain original inputs; UID/ACL/copy operations are explicit seams."""

import copy
import stat
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import inputs, runtime_inputs
from mod_base.errors import MbError
from mod_base.io.tree import EXPORT_PATHS
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests import test_ci_runtime_inputs as runtime_fixture


class RuntimeReadHandoffTests(unittest.TestCase):
    boundary = runtime_fixture.RuntimeInputTests.boundary
    validator = runtime_fixture.RuntimeInputTests.validator
    candidate = runtime_fixture.RuntimeInputTests.candidate

    def exercise(self, *, fault=None, mutate=None, documents=None, preflight=False):
        documents = runtime_fixture.fixture() if documents is None else documents
        original = copy.deepcopy(documents)
        events = []
        def info(owner, group, mode, inode=40):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)
        initial = info(2000 if fault == 'foreign' else 1001, 121, 0o700)
        final = info(1001, 2001, 0o770 if fault == 'mode' else 0o750, 99 if fault == 'inode' else 40)
        def kill(account):
            events.append(('terminate', account))
            if fault == 'survivor' and account == self.candidate:
                raise MbError('candidate survived')
        def grant(root, **kwargs):
            events.append('grant')
            self.assertEqual(root, runtime_inputs.RUNTIME_VALIDATION_ROOT)
            self.assertEqual((kwargs['source_owner_uid'], kwargs['owner_uid'], kwargs['reader_gid']), (1001, 1001, 2001))
            self.assertEqual(kwargs['max_files'], limits.MAX_CI_RUNTIME_FILES + 1)
            self.assertEqual(kwargs['max_total_bytes'], limits.MAX_CI_RUNTIME_BYTES + len(canonical_json(original[2])))
            self.assertEqual(kwargs['max_entries'], limits.MAX_CI_RUNTIME_ENTRIES)
            # The lane keeps the mod's own file names: the handoff walks it with the export rule.
            self.assertIs(kwargs['rule'], EXPORT_PATHS)
            if mutate is not None:
                mutate(*documents)
            if fault in ('grant', 'cleanup'):
                raise OSError('transfer failed')
        def closing(*args):
            events.append('closing')
            self.assertEqual(args[2:], original)
            for retained, caller in zip(args[2:], documents):
                self.assertIsNot(retained, caller)
            if fault == 'closing-bytes':
                raise MbError('bytes changed')
            return ((1, 99 if fault == 'plan-root' else 20), (1, 99 if fault == 'build-root' else 30),
                    (1, 99 if fault == 'runtime-root' else 40))
        with ExitStack() as stack:
            privilege = stack.enter_context(patch.object(runtime_inputs, 'authenticate_privileged_host_boundary',
                side_effect=[None, MbError('Root changed') if fault == 'privilege' else None]))
            stack.enter_context(patch.object(inputs, 'authenticate_worker_account', side_effect=[self.validator, self.candidate]))
            terminate = stack.enter_context(patch.object(runtime_inputs, 'terminate_worker', side_effect=kill))
            build_read = stack.enter_context(patch.object(runtime_inputs, '_inspect_build_inputs',
                return_value=((1, 20), (1, 30)), side_effect=MbError('Build changed') if fault == 'build-bytes' else None))
            opening = stack.enter_context(patch.object(runtime_inputs, '_open_directory', return_value=10))
            stack.enter_context(patch.object(runtime_inputs.os, 'fstat', side_effect=[initial, final]))
            stack.enter_context(patch.object(runtime_inputs.os, 'close',
                side_effect=OSError('close failed') if fault == 'close' else None))
            modes = stack.enter_context(patch.object(runtime_inputs.os, 'fchmod', create=True,
                side_effect=OSError('private traversal cleanup failed') if fault == 'cleanup' else None))
            stack.enter_context(patch.object(runtime_inputs.os, 'fsync'))
            stack.enter_context(patch.object(runtime_inputs, 'authenticate_tree_private_access',
                side_effect=MbError('unsafe private metadata') if fault == 'private' else None))
            reading = stack.enter_context(patch.object(runtime_inputs, 'verify_runtime_export',
                return_value={} if fault == 'runtime-bytes' else original[2]))
            granting = stack.enter_context(patch.object(runtime_inputs, 'grant_regular_data_read_access', side_effect=grant))
            stack.enter_context(patch.object(runtime_inputs, '_inspect_inputs', side_effect=closing))
            try:
                observed = runtime_inputs.prepare_runtime_validation(boundary=self.boundary, validator=self.validator,
                    plan=documents[0], build=documents[1], runtime=documents[2], lane_id='lane-a', run_id=43, run_attempt=2)
                self.assertEqual(observed, original[2])
                self.assertIsNot(observed, documents[2])
                self.assertEqual(events, [('terminate', self.candidate), ('terminate', self.validator),
                                          'grant', 'closing', ('terminate', self.validator)])
                self.assertEqual(privilege.call_count, 2)
                opening.assert_called_once_with(tuple(runtime_inputs.RUNTIME_VALIDATION_ROOT.parts[1:]))
                modes.assert_not_called()
            except MbError:
                if preflight or fault in ('foreign', 'survivor', 'build-bytes', 'close'):
                    modes.assert_not_called()
                else:
                    modes.assert_called_once_with(10, 0o700)
                raise
            finally:
                self.assertEqual(terminate.call_args.args, (self.validator,))
                if preflight:
                    build_read.assert_not_called()
                    opening.assert_not_called()
                    reading.assert_not_called()
                    granting.assert_not_called()
                if fault in ('survivor', 'foreign', 'private', 'build-bytes', 'runtime-bytes'):
                    granting.assert_not_called()

    def test_original_private_lane_uses_exact_scope_caps_and_existing_inputs(self):
        self.exercise()

    def test_candidate_survivor_foreign_private_metadata_and_wrong_bytes_forbid_read_grant(self):
        for fault in ('survivor', 'foreign', 'private', 'build-bytes', 'runtime-bytes'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_transfer_inode_mode_closing_bytes_and_privilege_failures_restore_private_root(self):
        for fault in ('grant', 'inode', 'mode', 'closing-bytes', 'privilege'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_descriptor_close_failure_still_terminates_validator_and_returns_no_input(self):
        with self.assertRaisesRegex(MbError, 'cannot close protected runtime'):
            self.exercise(fault='close')

    def test_private_traversal_cleanup_failure_is_visible_and_still_quiesces_validator(self):
        with self.assertRaisesRegex(MbError, 'could not restore private traversal'):
            self.exercise(fault='cleanup')

    def test_replacement_of_each_original_root_rejects_handoff(self):
        for fault in ('plan-root', 'build-root', 'runtime-root'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_original_caller_mutation_rejects_even_with_retained_closing_checks(self):
        for mutate in (lambda p, b, r: p.clear(), lambda p, b, r: b.clear(),
                       lambda p, b, r: r['files'][0].update(sha256='f' * 64)):
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                self.exercise(mutate=mutate)

    def test_invalid_context_precedes_input_reads_and_permission_mutation(self):
        documents = runtime_fixture.fixture()
        documents[2].update(scope='complete', lane_id=None)
        with self.assertRaises(MbError):
            self.exercise(documents=documents, preflight=True)

    def test_unprivileged_denial_precedes_account_lookup_and_cleanup(self):
        documents = runtime_fixture.fixture()
        with patch.object(runtime_inputs, 'authenticate_privileged_host_boundary', side_effect=MbError('not Root')), \
                patch.object(inputs, 'authenticate_worker_account') as accounts, \
                patch.object(runtime_inputs, 'terminate_worker') as terminate, self.assertRaises(MbError):
            runtime_inputs.prepare_runtime_validation(boundary=self.boundary, validator=self.validator,
                plan=documents[0], build=documents[1], runtime=documents[2], lane_id='lane-a', run_id=43, run_attempt=2)
        accounts.assert_not_called()
        terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
