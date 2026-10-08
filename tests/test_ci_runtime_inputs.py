"""Frozen runtime lane binding; syscall/worker seams do not establish Linux/native validity."""

import copy
import stat
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import inputs, runtime_inputs
from mod_base.build_ci.toolchain import ToolTreeProof
from mod_base.build_ci.worker import WorkerResult
from mod_base.errors import MbError
from tests import test_ci_inputs as input_fixture
from tests.helpers import ci_envelope, ci_plan, ci_runtime_envelope


def fixture():
    runtime = ci_runtime_envelope()
    runtime.update(scope='lane', lane_id='lane-a')
    return ci_plan(), ci_envelope(), runtime


class RuntimeInputTests(unittest.TestCase):
    boundary = input_fixture.InputExecutionTests.boundary
    validator = input_fixture.InputExecutionTests.validator
    candidate = input_fixture.InputExecutionTests.candidate
    identities = ((1, 20), (1, 30), (1, 40))

    def exercise(self, *, documents=None, lane_id='lane-a', run_id=43, run_attempt=2,
                 tools=None, reads=None, mutate=None, failure=None,
                 preflight=False, result=None):
        plan, build, runtime = fixture() if documents is None else documents
        original = copy.deepcopy((plan, build, runtime))
        proof = ToolTreeProof(('/opt/python',), 'a' * 64, 1, 2, 100)
        execution = WorkerResult(0, b'opaque native output', False) if result is None else result
        events = []
        def read(*args):
            events.append('read')
            self.assertEqual(args[2:], original)
            for actual, caller in zip(args[2:], (plan, build, runtime)):
                self.assertIsNot(actual, caller)
            value = (reads or [self.identities, self.identities])[events.count('read') - 1]
            if isinstance(value, BaseException):
                raise value
            return value
        def launch(**kwargs):
            events.append('execute')
            self.assertEqual((kwargs['hook'], kwargs['unit_id']), ('verify_runtime', lane_id))
            self.assertEqual(kwargs['plan'], original[0])
            if mutate is not None:
                mutate(plan, build, runtime)
            if failure is not None:
                raise failure
            return execution
        with ExitStack() as stack:
            stack.enter_context(patch.object(inputs, 'authenticate_worker_account', return_value=self.validator))
            stack.enter_context(patch.object(inputs, 'authenticate_peer_account', return_value=self.candidate))
            checking = stack.enter_context(patch.object(runtime_inputs, '_read_inputs', side_effect=read))
            execute = stack.enter_context(patch.object(runtime_inputs, 'execute_controller_validator', side_effect=launch))
            terminate = stack.enter_context(patch.object(runtime_inputs, 'terminate_worker'))
            try:
                observed = runtime_inputs.execute_frozen_runtime_validator(
                    boundary=self.boundary, validator=self.validator, sources=None,
                    tools=proof if tools is None else tools,
                    plan=plan, build=build, runtime=runtime, lane_id=lane_id,
                    python='/opt/python/bin/python', java_home=None, run_id=run_id, run_attempt=run_attempt)
                self.assertEqual(observed.execution, execution)
                self.assertEqual(observed.input_sha256, runtime_inputs._context(*original,
                    lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)[0])
                self.assertEqual(events, ['read', 'execute', 'read'])
                return observed
            finally:
                terminate.assert_called_once_with(self.validator)
                if preflight:
                    checking.assert_not_called()
                    execute.assert_not_called()

    def test_exact_lane_uses_original_complete_build_from_different_producer(self):
        observed = self.exercise()
        self.assertEqual(len(observed.input_sha256), 64)
        # Execution is data: downstream receipt freezing still requires successful native admission.
        self.exercise(result=WorkerResult(1, b'failed native output', True))

    def test_wrong_scope_owner_lane_attempt_and_unadmitted_tools_fail_before_input_reads(self):
        for change in ('aggregate', 'target', 'owner', 'plan'):
            documents = fixture()
            if change == 'aggregate':
                documents[2].update(scope='complete', lane_id=None)
            elif change == 'target':
                documents[1].update(scope='target', target_id='target-a')
            elif change == 'owner':
                documents[2]['owning_build']['producer']['run_id'] += 1
            else:
                documents[0]['unknown'] = True
            with self.subTest(change=change), self.assertRaises(MbError):
                self.exercise(documents=documents, preflight=True)
        for args in ({'lane_id': 'lane-b'}, {'run_id': 42}, {'run_attempt': True}, {'tools': object()},
                     {'tools': ('/opt/python',)}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(**args, preflight=True)

    def test_every_input_directory_substitution_is_rejected_after_execution(self):
        for index in range(3):
            changed = list(self.identities)
            changed[index] = (1, 99)
            with self.subTest(index=index), self.assertRaises(MbError):
                self.exercise(reads=[self.identities, tuple(changed)])

    def test_caller_mutation_cannot_replace_retained_pre_post_approval(self):
        mutations = [lambda p, b, r: p.update(unknown=True),
                     lambda p, b, r: b['producer'].update(run_id=44),
                     lambda p, b, r: r['files'][0].update(sha256='f' * 64)]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                self.exercise(mutate=mutate)

    def test_read_and_native_errors_always_terminate_admitted_validator(self):
        for args in ({'reads': [MbError('preflight bytes')]},
                     {'reads': [self.identities, MbError('closing bytes')]},
                     {'failure': MbError('native rejection')}, {'failure': OSError('worker failed')}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)

    def test_fixed_roots_require_exact_metadata_and_original_envelopes(self):
        plan, build, runtime = fixture()
        for fault in (None, 'owner', 'mode', 'acl', 'build', 'runtime'):
            info = SimpleNamespace(st_dev=1, st_ino=20, st_uid=2000 if fault == 'owner' else 1001,
                st_gid=2001, st_mode=stat.S_IFDIR | (0o770 if fault == 'mode' else 0o750))
            with self.subTest(fault=fault), ExitStack() as stack:
                stack.enter_context(patch.object(runtime_inputs, 'authenticate_host_boundary'))
                stack.enter_context(patch.object(runtime_inputs, '_layout'))
                stack.enter_context(patch.object(runtime_inputs, '_open_directory', return_value=10))
                stack.enter_context(patch.object(runtime_inputs.os, 'fstat', return_value=info))
                stack.enter_context(patch.object(runtime_inputs.os, 'close'))
                metadata = stack.enter_context(patch.object(runtime_inputs, 'authenticate_tree_read_access',
                    side_effect=MbError('unsafe access') if fault == 'acl' else None))
                stack.enter_context(patch.object(runtime_inputs, 'verify_validation_plan', return_value=plan))
                stack.enter_context(patch.object(runtime_inputs, 'verify_build_export', return_value={} if fault == 'build' else build))
                stack.enter_context(patch.object(runtime_inputs, 'verify_runtime_export', return_value={} if fault == 'runtime' else runtime))
                if fault is not None:
                    with self.assertRaises(MbError):
                        runtime_inputs._read_inputs(self.boundary, self.validator, plan, build, runtime)
                else:
                    self.assertEqual(runtime_inputs._read_inputs(self.boundary, self.validator, plan, build, runtime), ((1, 20),) * 3)
                    self.assertEqual([call.args[0] for call in metadata.call_args_list],
                        [runtime_inputs.VALIDATOR_INPUT_ROOT, runtime_inputs.BUILD_VALIDATION_ROOT,
                         runtime_inputs.RUNTIME_VALIDATION_ROOT])

    def test_context_digest_keeps_whole_owner_descriptor_without_claiming_api_authentication(self):
        plan, build, runtime = fixture()
        initial = runtime_inputs._context(plan, build, runtime, lane_id='lane-a', run_id=43, run_attempt=2)
        # A structurally valid upload-window change still requires independent API admission.
        runtime['owning_build']['producer']['upload_window']['completed_at'] = '2026-10-07T10:02:01Z'
        changed = runtime_inputs._context(plan, build, runtime, lane_id='lane-a', run_id=43, run_attempt=2)
        self.assertNotEqual(initial[0], changed[0])


if __name__ == '__main__':
    unittest.main()
