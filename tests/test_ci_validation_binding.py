"""Retained execution/input context remains coherent across privileged receipt freezing."""

import copy
import hashlib
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from mod_base.build_ci import inputs
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerAccount, WorkerResult
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_envelope, ci_plan


class BuildValidationBindingTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    validator = WorkerAccount("validator", 2001, 2001, "validator-home")
    candidate = WorkerAccount("candidate", 2000, 2000, "candidate-home")

    def fixture(self, *, target=False):
        plan, envelope = ci_plan(), ci_envelope()
        if target:
            envelope.update(scope="target", target_id="target-a")
        bound = inputs.BuildValidationExecution(WorkerResult(0, b"native hook output", False),
                                                hashlib.sha256(canonical_json(envelope)).hexdigest())
        return plan, envelope, bound

    def exercise(self, fixture, *, reads=None, freezing=None, forbid_freeze=False, **changes):
        plan, envelope, bound = fixture
        with ExitStack() as stack:
            stack.enter_context(patch.object(inputs, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(inputs, "authenticate_worker_account",
                                             side_effect=[self.validator, self.candidate]))
            checking = stack.enter_context(patch.object(inputs, "_inspect_inputs",
                side_effect=reads or [((1, 20), (1, 30)), ((1, 20), (1, 30))]))
            freezing_mock = stack.enter_context(patch.object(inputs, "freeze_validation_export",
                                                              side_effect=freezing, return_value={"delegated": "receipt"}))
            terminate = stack.enter_context(patch.object(inputs, "terminate_worker"))
            arguments = dict(boundary=self.boundary, validator=self.validator, sources=None,
                bound=bound, plan=plan, envelope=envelope, run_id=42, run_attempt=2)
            arguments.update(changes)
            try:
                result = inputs.freeze_frozen_build_validation(**arguments)
            finally:
                self.assertGreaterEqual(terminate.call_count, 1)
                self.assertTrue(all(call.args == (self.validator,) for call in terminate.call_args_list))
                if forbid_freeze:
                    freezing_mock.assert_not_called()
            return result, freezing_mock.call_args.kwargs, checking.call_count

    def test_complete_and_target_hooks_derive_exact_retained_context(self):
        for target in (False, True):
            fixture = self.fixture(target=target)
            with self.subTest(target=target):
                result, args, reads = self.exercise(fixture)
                self.assertEqual(result, {"delegated": "receipt"})
                self.assertEqual((args["hook"], args["unit_id"]),
                                 ("verify_target", "target-a") if target else ("verify_build", None))
                self.assertEqual(args["input_sha256"], fixture[2].input_sha256)
                self.assertIs(args["execution"], fixture[2].execution)
                self.assertEqual((args["run_id"], args["run_attempt"]), (42, 2))
                self.assertEqual(reads, 2)

    def test_other_input_digest_or_constructible_wrong_execution_does_not_freeze(self):
        for bound in (None, inputs.BuildValidationExecution(WorkerResult(0, b"", False), "f" * 64),
                      inputs.BuildValidationExecution(WorkerResult(1, b"native failed", False),
                         self.fixture()[2].input_sha256)):
            with self.subTest(bound=bound):
                with self.assertRaises(MbError):
                    self.exercise(self.fixture(), bound=bound, forbid_freeze=True)

    def test_other_run_attempt_or_changed_envelope_cannot_reuse_execution(self):
        fixture = self.fixture()
        for changes in ({"run_id": 43}, {"run_attempt": 3}, {"run_id": True}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.exercise(fixture, **changes)
        changed = copy.deepcopy(fixture[1])
        changed.update(scope="target", target_id="target-a")
        with self.assertRaisesRegex(MbError, "exact frozen input"):
            self.exercise(fixture, envelope=changed)

    def test_missing_drifted_inputs_or_freeze_failure_cannot_return_success(self):
        for values in ({"reads": [MbError("input missing")]},
                {"reads": [((1, 20), (1, 30)), MbError("input bytes changed")]},
                {"reads": [((1, 20), (1, 30)), ((1, 21), (1, 30))]},
                {"freezing": MbError("receipt mismatch")}):
            with self.subTest(values=values), self.assertRaises(MbError):
                self.exercise(self.fixture(), **values)

    def test_mutating_caller_plan_and_envelope_cannot_switch_retained_freeze_inputs(self):
        fixture = self.fixture()
        expected_plan, expected_envelope = copy.deepcopy(fixture[0]), copy.deepcopy(fixture[1])
        def freezing(**kwargs):
            fixture[0].clear()
            fixture[1].clear()
            return {"delegated": "receipt"}
        _, args, reads = self.exercise(fixture, freezing=freezing)
        self.assertEqual(args["plan"], expected_plan)
        self.assertEqual(args["input_sha256"], hashlib.sha256(canonical_json(expected_envelope)).hexdigest())
        self.assertEqual(reads, 2)

    def test_unprivileged_host_rejection_precedes_account_lookup_or_cleanup(self):
        with patch.object(inputs, "authenticate_privileged_host_boundary", side_effect=MbError("not root")), \
                patch.object(inputs, "authenticate_worker_account") as account, \
                patch.object(inputs, "terminate_worker") as terminate, self.assertRaises(MbError):
            plan, envelope, bound = self.fixture()
            inputs.freeze_frozen_build_validation(boundary=self.boundary, validator=self.validator,
                sources=None, bound=bound, plan=plan, envelope=envelope, run_id=42, run_attempt=2)
        account.assert_not_called()
        terminate.assert_not_called()
