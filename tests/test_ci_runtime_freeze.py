"""Original candidate runtime freeze binding with explicit source/copy/UID seams."""

import copy
import stat
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import runtime_freeze
from mod_base.build_ci.source import GitSourceEntry
from mod_base.build_ci.worker import WorkerResult
from mod_base.errors import MbError
from mod_base.io.tree import EXPORT_PATHS
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests import test_ci_runtime_inputs as runtime_fixture


class RuntimeFreezeTests(unittest.TestCase):
    boundary = runtime_fixture.RuntimeInputTests.boundary
    candidate = runtime_fixture.RuntimeInputTests.candidate
    validator = runtime_fixture.RuntimeInputTests.validator
    inventory = (GitSourceEntry('src/Main.java', '100644', 3, 'a' * 40),)

    def exercise(self, *, fault=None, mutate=None, runtime_mutate=None, execution=None,
                 lane_id='lane-a', generated_roots=('build',), preflight=False):
        plan, build, runtime = runtime_fixture.fixture()
        owner = copy.deepcopy(runtime['owning_build'])
        original = copy.deepcopy((plan, build, owner))
        if runtime_mutate is not None:
            runtime_mutate(runtime)
        expected = copy.deepcopy(runtime)
        events = []
        reads = {'source': 0, 'build': 0, 'runtime': 0}
        def info(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)
        metadata = [info(20, 1001, 121, 0o711), info(21, 1001, 121, 0o711),
                    info(22, 2000, 2000, 0o700), info(23, 2000, 2000, 0o700),
                    info(40, 2000 if fault == 'foreign' else 0, 0, 0o700),
                    info(99 if fault == 'inode' else 40, 1001, 121, 0o700),
                    info(99 if fault == 'named-root' else 40, 1001, 121, 0o700)]
        def terminate(account):
            self.assertEqual(account, self.candidate)
            events.append('terminate')
            if fault == 'survivor':
                raise MbError('candidate survived')
        def source(root, **kwargs):
            reads['source'] += 1
            events.append('source')
            self.assertEqual(root, runtime_freeze.CANDIDATE_SOURCE_ROOT)
            self.assertEqual(kwargs, dict(inventory=self.inventory, generated_roots=generated_roots))
            if fault == 'source-pre':
                raise MbError('source mutated')
            return ['changed'] if (fault == 'source-copy' and reads['source'] == 2
                or fault == 'source-transfer' and reads['source'] == 4) else ['original witness']
        def build_inputs(boundary, validator, p, b):
            reads['build'] += 1
            events.append('build')
            self.assertEqual((p, b), original[:2])
            self.assertIsNot(p, plan)
            if fault == 'build-bytes':
                raise MbError('Build bytes changed')
            return ((1, 99 if fault == 'build-root' and reads['build'] > 1 else 20), (1, 30))
        def verifying(root, **kwargs):
            reads['runtime'] += 1
            events.append('runtime-original' if root == runtime_freeze.CANDIDATE_OUTPUT_ROOT else 'runtime-copy')
            self.assertEqual(kwargs['plan'], original[0])
            if fault == 'runtime-original' and reads['runtime'] == 2:
                return {**expected, 'files': []}
            if fault == 'runtime-copy' and root == runtime_freeze.RUNTIME_VALIDATION_ROOT:
                return {**expected, 'files': []}
            return copy.deepcopy(expected)
        def copying(root, output, **kwargs):
            events.append('copy')
            self.assertEqual((root, output), (runtime_freeze.CANDIDATE_OUTPUT_ROOT, runtime_freeze.RUNTIME_VALIDATION_ROOT))
            if mutate is not None:
                mutate(plan, build, owner)
            kwargs['before_publish']()
            if fault == 'copy-result':
                return {**expected, 'files': []}
            return copy.deepcopy(expected)
        def transferring(root, **kwargs):
            events.append('transfer')
            self.assertEqual(root, runtime_freeze.RUNTIME_VALIDATION_ROOT)
            self.assertEqual((kwargs['source_owner_uid'], kwargs['owner_uid'], kwargs['owner_gid']), (0, 1001, 121))
            self.assertEqual(kwargs['max_files'], limits.MAX_CI_RUNTIME_FILES + 1)
            self.assertEqual(kwargs['max_total_bytes'], limits.MAX_CI_RUNTIME_BYTES + len(canonical_json(expected)))
            # The copy keeps the mod's own file names: the transfer walks it with the export rule.
            self.assertIs(kwargs['rule'], EXPORT_PATHS)
            if fault in ('transfer', 'cleanup'):
                raise OSError('ownership failed')
        def close(fd):
            if fault == 'close' and fd == 14:
                raise OSError('close failed')
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime_freeze, 'authenticate_privileged_host_boundary'))
            accounts = stack.enter_context(patch.object(runtime_freeze, 'authenticate_worker_account',
                side_effect=[self.candidate, self.validator]))
            stack.enter_context(patch.object(runtime_freeze, '_open_directory', side_effect=[10, 11, 12, 13, 14, 15]))
            stack.enter_context(patch.object(runtime_freeze.os, 'fstat', side_effect=metadata))
            stack.enter_context(patch.object(runtime_freeze.os, 'close', side_effect=close))
            modes = stack.enter_context(patch.object(runtime_freeze.os, 'fchmod', create=True,
                side_effect=OSError('cleanup failed') if fault == 'cleanup' else None))
            stack.enter_context(patch.object(runtime_freeze.os, 'fsync'))
            killing = stack.enter_context(patch.object(runtime_freeze, 'terminate_worker', side_effect=terminate))
            stack.enter_context(patch.object(runtime_freeze, 'verify_source_copy', side_effect=source))
            stack.enter_context(patch.object(runtime_freeze, '_inspect_build_inputs', side_effect=build_inputs))
            stack.enter_context(patch.object(runtime_freeze, 'authenticate_tree_private_access',
                side_effect=MbError('unsafe private tree') if fault == 'private' else None))
            checking = stack.enter_context(patch.object(runtime_freeze, 'verify_runtime_export', side_effect=verifying))
            copying_mock = stack.enter_context(patch.object(runtime_freeze, '_materialize_runtime_export', side_effect=copying))
            transferring_mock = stack.enter_context(patch.object(runtime_freeze, 'privatize_regular_data_copy', side_effect=transferring))
            try:
                observed = runtime_freeze.freeze_runtime_export(boundary=self.boundary, candidate=self.candidate,
                    execution=WorkerResult(0, b'candidate log', False) if execution is None else execution,
                    inventory=self.inventory, generated_roots=generated_roots, plan=plan, build=build,
                    owning_build=owner, lane_id=lane_id, run_id=43, run_attempt=2)
                self.assertEqual(observed, expected)
                self.assertIsNot(observed, runtime)
                self.assertEqual(reads, {'source': 4, 'build': 4, 'runtime': 3})
                self.assertEqual(events[0], 'terminate')
                self.assertEqual(events[-1], 'terminate')
                modes.assert_not_called()
            except MbError:
                if 'transfer' in events and fault != 'close':
                    modes.assert_called_once_with(14, 0o700)
                else:
                    modes.assert_not_called()
                raise
            finally:
                if preflight:
                    accounts.assert_not_called()
                    killing.assert_not_called()
                    checking.assert_not_called()
                else:
                    self.assertEqual(killing.call_args.args, (self.candidate,))
                if fault in ('survivor', 'source-pre', 'private', 'build-bytes') or runtime_mutate is not None:
                    copying_mock.assert_not_called()
                if fault in ('source-copy', 'build-root', 'runtime-original', 'copy-result', 'foreign'):
                    transferring_mock.assert_not_called()

    def test_success_binds_original_source_build_whole_selection_and_private_copy(self):
        self.exercise()

    def test_survivor_original_source_build_and_private_metadata_failures_stop_copy(self):
        for fault in ('survivor', 'source-pre', 'build-bytes', 'private'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_source_build_and_original_runtime_drift_inside_publication_stop_transfer(self):
        for fault in ('source-copy', 'build-root', 'runtime-original', 'copy-result'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_private_owner_inode_named_root_bytes_and_final_source_failures_never_return(self):
        for fault in ('foreign', 'transfer', 'inode', 'named-root', 'runtime-copy', 'source-transfer'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_candidate_cannot_replace_whole_original_owning_build_or_lane_or_attempt(self):
        for mutate in (lambda r: r['owning_build']['artifact'].update(id=999),
                       lambda r: r.update(scope='complete', lane_id=None),
                       lambda r: r['producer'].update(run_id=44)):
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                self.exercise(runtime_mutate=mutate)

    def test_original_caller_drift_is_rejected_inside_private_publication(self):
        for mutate in (lambda p, b, o: p.clear(), lambda p, b, o: b.clear(),
                       lambda p, b, o: o['artifact'].update(id=999)):
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                self.exercise(mutate=mutate)

    def test_bad_execution_lane_and_generated_policy_reject_before_account_mutation(self):
        for args in ({'execution': WorkerResult(1, b'failed', False)},
                     {'execution': WorkerResult(False, b'fake', False)},
                     {'execution': WorkerResult(0, bytearray(b'log'), False)},
                     {'lane_id': 'lane-b'}, {'generated_roots': ['build']}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(**args, preflight=True)

    def test_cleanup_and_close_errors_are_visible_and_still_quiesce_candidate(self):
        for fault in ('cleanup', 'close'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.exercise(fault=fault)

    def test_unprivileged_denial_precedes_account_lookup(self):
        with patch.object(runtime_freeze, 'authenticate_privileged_host_boundary', side_effect=MbError('not Root')), \
                patch.object(runtime_freeze, 'authenticate_worker_account') as account, self.assertRaises(MbError):
            runtime_freeze.freeze_runtime_export(boundary=self.boundary, candidate=self.candidate, execution=None,
                inventory=(), generated_roots=(), plan={}, build={}, owning_build={}, lane_id='lane-a', run_id=43, run_attempt=2)
        account.assert_not_called()


if __name__ == '__main__':
    unittest.main()
