"""Runtime physical-channel/context mechanics with explicit syscall/input seams, not Linux proof."""

import base64
import copy
import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import handoff, runtime_handoff, runtime_inputs
from mod_base.build_ci.worker import WorkerResult
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests import test_ci_execution_handoff as build_fixture
from tests import test_ci_runtime_inputs as runtime_fixture


class RuntimeExecutionHandoffTests(unittest.TestCase):
    validator = runtime_fixture.RuntimeInputTests.validator
    identities = runtime_fixture.RuntimeInputTests.identities

    def fixture(self):
        protected = build_fixture.ExecutionHandoffTests().fixture()
        plan, build, runtime = runtime_fixture.fixture()
        context, _ = runtime_handoff._context(protected['sources'], plan, build, runtime,
            lane_id='lane-a', run_id=43, run_attempt=2)
        return dict(boundary=protected['boundary'], validator=self.validator, sources=protected['sources'],
            plan=plan, build=build, runtime=runtime, lane_id='lane-a', run_id=43, run_attempt=2,
            bound=runtime_inputs.RuntimeValidationExecution(WorkerResult(0, b'binary\0\xff\n::opaque::', True), context['input_sha256']))

    def document(self, fixture):
        context, _ = runtime_handoff._context(fixture['sources'], fixture['plan'], fixture['build'], fixture['runtime'],
            lane_id='lane-a', run_id=43, run_attempt=2)
        result = fixture['bound'].execution
        return dict(kind='mod-base.ci.execution', schema_version=1, **context, nonce='0a' * 32,
            returncode=result.returncode, truncated=result.truncated, log_base64=base64.b64encode(result.log).decode('ascii'))

    def publish(self, fixture, *, fault=None):
        captured = {}
        reads = 0
        def read(*args):
            nonlocal reads
            reads += 1
            self.assertEqual(args[2:], (fixture['plan'], fixture['build'], fixture['runtime']))
            if reads == 2:
                if fault == 'caller':
                    fixture['runtime']['files'][0]['sha256'] = 'f' * 64
                elif fault == 'stage':
                    captured['raw'] = b' ' + captured['raw']
                elif fault == 'bytes':
                    raise MbError('original bytes changed')
            return ((1, 20), (1, 30), (1, 99)) if reads == 2 and fault == 'inode' else self.identities
        def atomic(output, writer):
            self.assertEqual(output, Path(str(handoff.EXECUTION_HANDOFF_ROOT)))
            writer(Path('private-stage'), 100)
            captured['published'] = True
        def metadata(fd):
            return SimpleNamespace(st_mode=(stat.S_IFDIR | 0o700) if fd == 100 else (stat.S_IFREG | 0o600),
                st_uid=fixture['boundary'].uid, st_gid=fixture['boundary'].gid, st_nlink=1,
                st_size=len(captured.get('raw', b'x')))
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime_handoff, 'authenticate_host_boundary'))
            stack.enter_context(patch.object(runtime_handoff, '_accounts'))
            stack.enter_context(patch.object(runtime_handoff, '_read_inputs', side_effect=read))
            terminate = stack.enter_context(patch.object(runtime_handoff, 'terminate_worker'))
            stack.enter_context(patch.object(handoff, 'authenticate_host_boundary'))
            stack.enter_context(patch.object(handoff, '_layout'))
            stack.enter_context(patch.object(handoff, 'atomic_directory', side_effect=atomic))
            stack.enter_context(patch.object(handoff, 'write_new', side_effect=lambda fd, name, raw: captured.update(raw=raw)))
            stack.enter_context(patch.object(handoff.os, 'urandom', return_value=b'\x0a' * 32))
            stack.enter_context(patch.object(handoff.os, 'fstat', side_effect=metadata))
            stack.enter_context(patch.object(handoff.os, 'open', return_value=200))
            stack.enter_context(patch.object(handoff.os, 'O_NOFOLLOW', 0, create=True))
            stack.enter_context(patch.object(handoff.os, 'fchmod', create=True))
            stack.enter_context(patch.object(handoff.os, 'fsync'))
            stack.enter_context(patch.object(handoff.os, 'close'))
            stack.enter_context(patch.object(handoff, 'read_child_file', side_effect=lambda *args, **kwargs: captured['raw']))
            try:
                nonce = runtime_handoff.record_runtime_validation_execution(**fixture)
                self.assertEqual(nonce, '0a' * 32)
                self.assertEqual(captured['raw'], canonical_json(self.document(fixture)))
                self.assertEqual(reads, 2)
                self.assertTrue(captured['published'])
            finally:
                self.assertGreaterEqual(terminate.call_count, 1)
                if fault is not None:
                    self.assertNotIn('published', captured)

    def freeze(self, fixture, *, raw=None, closing_raw=None, mutate=None, changed_input=False, forbid=False):
        original = copy.deepcopy((fixture['plan'], fixture['build'], fixture['runtime']))
        raw = canonical_json(self.document(fixture)) if raw is None else raw
        def freezing(**kwargs):
            self.assertEqual(kwargs['bound'], fixture['bound'])
            self.assertEqual((kwargs['plan'], kwargs['build'], kwargs['runtime']), original)
            for retained, caller in zip((kwargs['plan'], kwargs['build'], kwargs['runtime']),
                                        (fixture['plan'], fixture['build'], fixture['runtime'])):
                self.assertIsNot(retained, caller)
            if mutate is not None:
                mutate(fixture)
            return {'delegated': 'private receipt'}
        with ExitStack() as stack:
            stack.enter_context(patch.object(runtime_handoff, 'authenticate_privileged_host_boundary'))
            stack.enter_context(patch.object(runtime_handoff, '_accounts'))
            stack.enter_context(patch.object(runtime_handoff, '_layout'))
            stack.enter_context(patch.object(runtime_handoff, '_inspect_inputs', side_effect=[self.identities,
                ((1, 20), (1, 30), (1, 99)) if changed_input else self.identities]))
            reader = stack.enter_context(patch.object(runtime_handoff, '_read_private_handoff',
                side_effect=[raw, raw if closing_raw is None else closing_raw]))
            freezing_mock = stack.enter_context(patch.object(runtime_handoff, 'freeze_frozen_runtime_validation', side_effect=freezing))
            terminate = stack.enter_context(patch.object(runtime_handoff, 'terminate_worker'))
            arguments = {key: value for key, value in fixture.items() if key != 'bound'}
            try:
                observed = runtime_handoff.freeze_handed_off_runtime_validation(**arguments, nonce='0a' * 32)
                self.assertEqual(observed, {'delegated': 'private receipt'})
                self.assertEqual(reader.call_count, 2)
            finally:
                self.assertGreaterEqual(terminate.call_count, 1)
                if forbid:
                    freezing_mock.assert_not_called()

    def test_runner_uses_original_context_existing_closed_fields_binary_log_and_private_writer(self):
        self.publish(self.fixture())

    def test_writer_input_bytes_inodes_caller_and_stage_drift_cannot_publish(self):
        for fault in ('bytes', 'inode', 'caller', 'stage'):
            with self.subTest(fault=fault), self.assertRaises(MbError):
                self.publish(self.fixture(), fault=fault)

    def test_failed_or_wrong_bound_execution_cannot_reach_publication(self):
        fixture = self.fixture()
        digest = fixture['bound'].input_sha256
        for bound in (None, runtime_inputs.RuntimeValidationExecution(WorkerResult(0, b'', False), 'f' * 64),
                      runtime_inputs.RuntimeValidationExecution(WorkerResult(1, b'failed', False), digest),
                      runtime_inputs.RuntimeValidationExecution(WorkerResult(False, b'fake', False), digest)):
            with patch.object(runtime_handoff, 'authenticate_host_boundary'), patch.object(runtime_handoff, '_accounts'), \
                    patch.object(runtime_handoff, 'terminate_worker'), patch.object(runtime_handoff, '_publish_execution') as publishing, \
                    self.subTest(bound=bound), self.assertRaises(MbError):
                runtime_handoff.record_runtime_validation_execution(**{**fixture, 'bound': bound})
            publishing.assert_not_called()

    def test_root_reconstructs_original_runtime_execution_and_reads_channel_again(self):
        self.freeze(self.fixture())

    def test_wrong_nonce_run_attempt_plan_source_or_build_digest_cannot_freeze(self):
        fixture = self.fixture()
        original = self.document(fixture)
        for key, value in (('nonce', 'b' * 64), ('run_id', 42), ('run_attempt', 3),
                ('plan_sha256', 'b' * 64), ('source_config_sha256', 'b' * 64),
                ('input_sha256', hashlib.sha256(canonical_json(fixture['build'])).hexdigest())):
            with self.subTest(key=key), self.assertRaises(MbError):
                self.freeze(fixture, raw=canonical_json({**original, key: value}), forbid=True)

    def test_noncanonical_unknown_duplicate_and_nonfinite_records_cannot_freeze(self):
        fixture = self.fixture()
        raw = canonical_json(self.document(fixture))
        for data in (b' ' + raw, canonical_json({**self.document(fixture), 'hook': 'unsafe'}),
                     raw.replace(b'"returncode":0', b'"returncode":0,"returncode":0'), b'{"returncode":NaN}'):
            with self.subTest(data=data[:25]), self.assertRaises(MbError):
                self.freeze(fixture, raw=data, forbid=True)

    def test_channel_inputs_and_original_caller_changes_after_freeze_prevent_return(self):
        fixture = self.fixture()
        with self.assertRaises(MbError):
            self.freeze(fixture, closing_raw=b'changed original channel')
        with self.assertRaises(MbError):
            self.freeze(fixture, changed_input=True)
        for key in ('plan', 'build', 'runtime'):
            with self.subTest(key=key), self.assertRaises(MbError):
                self.freeze(self.fixture(), mutate=lambda args: args[key].clear())

    def test_role_denial_precedes_accounts_and_io_failures_quiesce_admitted_validator(self):
        fixture = self.fixture()
        args = {key: value for key, value in fixture.items() if key != 'bound'}
        for operation, call in (('authenticate_host_boundary', lambda: runtime_handoff.record_runtime_validation_execution(**fixture)),
                ('authenticate_privileged_host_boundary', lambda: runtime_handoff.freeze_handed_off_runtime_validation(**args, nonce='0a' * 32))):
            with patch.object(runtime_handoff, operation, side_effect=MbError('wrong role')), \
                    patch.object(runtime_handoff, '_accounts') as accounts, \
                    patch.object(runtime_handoff, 'terminate_worker') as terminating, self.assertRaises(MbError):
                call()
            accounts.assert_not_called()
            terminating.assert_not_called()
        with patch.object(runtime_handoff, 'authenticate_privileged_host_boundary'), \
                patch.object(runtime_handoff, '_accounts'), patch.object(runtime_handoff, '_layout'), \
                patch.object(runtime_handoff, '_inspect_inputs', return_value=self.identities), \
                patch.object(runtime_handoff, '_read_private_handoff', side_effect=OSError('missing channel')), \
                patch.object(runtime_handoff, 'freeze_frozen_runtime_validation') as freezing, \
                patch.object(runtime_handoff, 'terminate_worker') as terminating, self.assertRaises(MbError):
            runtime_handoff.freeze_handed_off_runtime_validation(**args, nonce='0a' * 32)
        freezing.assert_not_called()
        self.assertGreaterEqual(terminating.call_count, 1)


if __name__ == '__main__':
    unittest.main()
