"""Root receipt/context binding with explicit host/byte/copy seams, never real Linux proof."""

import copy
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from mod_base.build_ci import inputs, runtime_inputs, validation
from mod_base.build_ci.worker import WorkerResult
from mod_base.errors import MbError
from tests import test_ci_runtime_inputs as runtime_fixture
from tests.helpers import ci_validation


class RuntimeValidationBindingTests(unittest.TestCase):
    boundary = runtime_fixture.RuntimeInputTests.boundary
    validator = runtime_fixture.RuntimeInputTests.validator
    candidate = runtime_fixture.RuntimeInputTests.candidate
    identities = runtime_fixture.RuntimeInputTests.identities

    def fixture(self):
        documents = runtime_fixture.fixture()
        digest = runtime_inputs._context(*documents, lane_id='lane-a', run_id=43, run_attempt=2)[0]
        return documents, runtime_inputs.RuntimeValidationExecution(WorkerResult(0, b'native log', False), digest)

    def exercise(self, *, fixture=None, reads=None, mutate=None, failure=None, preflight=False, **changes):
        documents, bound = self.fixture() if fixture is None else fixture
        original = copy.deepcopy(documents)
        events = []
        def inspect(*args):
            events.append('inspect')
            self.assertEqual(args[2:], original)
            for observed, caller in zip(args[2:], documents):
                self.assertIsNot(observed, caller)
            value = (reads or [self.identities, self.identities])[events.count('inspect') - 1]
            if isinstance(value, BaseException):
                raise value
            return value
        def freeze(**kwargs):
            events.append('freeze')
            self.assertEqual((kwargs['hook'], kwargs['unit_id']), ('verify_runtime', 'lane-a'))
            self.assertEqual((kwargs['run_id'], kwargs['run_attempt']), (43, 2))
            self.assertIs(kwargs['execution'], bound.execution)
            self.assertEqual(kwargs['input_sha256'], bound.input_sha256)
            self.assertEqual(kwargs['plan'], original[0])
            if mutate is not None:
                mutate(*documents)
                self.assertEqual(kwargs['plan'], original[0])
            if failure is not None:
                raise failure
            # Actual generic native-report contract validation; opaque report bytes remain a seam.
            receipt = ci_validation('verify_runtime', 'lane-a')
            receipt.update(run_id=43, input_sha256=kwargs['input_sha256'])
            return validation.validate_validation_receipt(receipt, plan=kwargs['plan'])
        with ExitStack() as stack:
            privilege = stack.enter_context(patch.object(runtime_inputs, 'authenticate_privileged_host_boundary',
                side_effect=lambda boundary: events.append('privilege')))
            stack.enter_context(patch.object(inputs, 'authenticate_worker_account', side_effect=[self.validator, self.candidate]))
            inspecting = stack.enter_context(patch.object(runtime_inputs, '_inspect_inputs', side_effect=inspect))
            freezing = stack.enter_context(patch.object(runtime_inputs, 'freeze_validation_export', side_effect=freeze))
            terminate = stack.enter_context(patch.object(runtime_inputs, 'terminate_worker',
                side_effect=lambda account: events.append('terminate')))
            arguments = dict(boundary=self.boundary, validator=self.validator, sources=None, bound=bound,
                plan=documents[0], build=documents[1], runtime=documents[2], lane_id='lane-a', run_id=43, run_attempt=2)
            arguments.update(changes)
            try:
                observed = runtime_inputs.freeze_frozen_runtime_validation(**arguments)
                self.assertEqual(observed['input_sha256'], bound.input_sha256)
                self.assertEqual(events, ['privilege', 'terminate', 'inspect', 'freeze', 'inspect', 'privilege', 'terminate'])
                self.assertEqual(privilege.call_count, 2)
                return observed
            finally:
                self.assertGreaterEqual(terminate.call_count, 1)
                self.assertTrue(all(call.args == (self.validator,) for call in terminate.call_args_list))
                if preflight:
                    inspecting.assert_not_called()
                    freezing.assert_not_called()

    def test_exact_original_cross_run_build_lane_and_successful_execution_are_bound(self):
        self.exercise()
        documents, bound = self.fixture()
        # Bounded diagnostic truncation preserves native success semantics.
        self.exercise(fixture=(documents, runtime_inputs.RuntimeValidationExecution(
            WorkerResult(0, b'native log', True), bound.input_sha256)))

    def test_wrong_digest_failed_boolean_unbounded_or_missing_execution_cannot_freeze(self):
        documents, bound = self.fixture()
        for changed in (None, runtime_inputs.RuntimeValidationExecution(bound.execution, 'f' * 64),
                        runtime_inputs.RuntimeValidationExecution(WorkerResult(1, b'failed', False), bound.input_sha256),
                        runtime_inputs.RuntimeValidationExecution(WorkerResult(False, b'fake', False), bound.input_sha256),
                        runtime_inputs.RuntimeValidationExecution(WorkerResult(0, b'log', 1), bound.input_sha256),
                        runtime_inputs.RuntimeValidationExecution(WorkerResult(0, bytearray(b'log'), False), bound.input_sha256)):
            with self.subTest(bound=changed), self.assertRaises(MbError):
                self.exercise(fixture=(documents, bound), bound=changed, preflight=True)
        with patch.object(runtime_inputs.limits, 'MAX_CI_LOG_BYTES', 3), self.assertRaises(MbError):
            self.exercise(preflight=True)

    def test_scope_lane_attempt_and_changed_original_context_reject_before_copy(self):
        for change in ({'lane_id': 'lane-b'}, {'run_id': 42}, {'run_attempt': True}):
            with self.subTest(change=change), self.assertRaises(MbError):
                self.exercise(**change, preflight=True)
        for index in range(3):
            documents, bound = self.fixture()
            if index == 0:
                documents[0]['unknown'] = True
            elif index == 1:
                documents[1].update(scope='target', target_id='target-a')
            else:
                documents[2]['files'][0]['sha256'] = 'f' * 64
            with self.subTest(index=index), self.assertRaises(MbError):
                self.exercise(fixture=(documents, bound), preflight=True)

    def test_each_input_root_must_keep_its_identity_across_receipt_freezing(self):
        for index in range(3):
            changed = list(self.identities)
            changed[index] = (1, 99)
            with self.subTest(index=index), self.assertRaises(MbError):
                self.exercise(reads=[self.identities, tuple(changed)])

    def test_input_copy_and_os_errors_cannot_return_receipt(self):
        for args in ({'reads': [MbError('missing bytes')]},
                     {'reads': [self.identities, MbError('changed closing bytes')]},
                     {'failure': MbError('native receipt rejected')}, {'failure': OSError('copy failed')}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)

    def test_original_caller_drift_during_freeze_rejects_despite_retained_checks(self):
        for mutate in (lambda p, b, r: p.clear(), lambda p, b, r: b.clear(),
                       lambda p, b, r: r['files'][0].update(sha256='f' * 64)):
            with self.subTest(mutate=mutate), self.assertRaises(MbError):
                self.exercise(mutate=mutate)

    def test_unprivileged_host_is_rejected_before_account_lookup_or_cleanup(self):
        documents, bound = self.fixture()
        with patch.object(runtime_inputs, 'authenticate_privileged_host_boundary', side_effect=MbError('not Root')), \
                patch.object(inputs, 'authenticate_worker_account') as accounts, \
                patch.object(runtime_inputs, 'terminate_worker') as terminate, \
                patch.object(runtime_inputs, 'freeze_validation_export') as freezing, self.assertRaises(MbError):
            runtime_inputs.freeze_frozen_runtime_validation(boundary=self.boundary, validator=self.validator,
                sources=None, bound=bound, plan=documents[0], build=documents[1], runtime=documents[2],
                lane_id='lane-a', run_id=43, run_attempt=2)
        accounts.assert_not_called()
        terminate.assert_not_called()
        freezing.assert_not_called()


if __name__ == '__main__':
    unittest.main()
