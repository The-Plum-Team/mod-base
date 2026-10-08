"""Protected fixed plan copy and read-only Build input/execution binding."""

import copy
import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import inputs
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.toolchain import ToolTreeProof
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_envelope, ci_plan


class ValidationPlanTests(unittest.TestCase):
    def test_exact_plan_inventory_and_bytes_are_both_required(self):
        plan = ci_plan()
        raw = canonical_json(plan)
        record = {"path": grammar.CI_PLAN_NAME, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        for records, data, success in [([record], raw, True), ([], raw, False),
                ([record, {**record, "path": "extra.json"}], raw, False),
                ([{**record, "size": len(raw) + 1}], raw, False), ([record], b" " + raw, False)]:
            with self.subTest(success=success, data=data[:1]), \
                    patch.object(inputs, "validate_tree_entries"), \
                    patch.object(inputs, "file_records", return_value=records), \
                    patch.object(inputs, "read_child_file", return_value=data):
                if success:
                    self.assertEqual(inputs.verify_validation_plan(Path("input"), plan=plan), plan)
                else:
                    with self.assertRaises(MbError):
                        inputs.verify_validation_plan(Path("input"), plan=plan)

    def test_entry_preflight_precedes_content_and_invalid_plan_precedes_copy(self):
        with patch.object(inputs, "validate_tree_entries", side_effect=MbError("entries")), \
                patch.object(inputs, "file_records") as records, self.assertRaises(MbError):
            inputs.verify_validation_plan(Path("input"), plan=ci_plan())
        records.assert_not_called()
        invalid = {**ci_plan(), "unknown": True}
        with patch.object(inputs, "atomic_directory") as atomic, self.assertRaises(MbError):
            inputs.materialize_validation_plan(Path("input"), plan=invalid)
        atomic.assert_not_called()

    def test_independent_canonical_stage_is_verified_before_publication(self):
        plan = ci_plan()
        def atomic(output, writer):
            self.assertEqual(output, Path("input"))
            return writer(Path("stage"), 17)
        with patch.object(inputs, "atomic_directory", side_effect=atomic), \
                patch.object(inputs, "_write_controller_files") as writing, \
                patch.object(inputs, "verify_validation_plan", return_value=plan) as checking:
            self.assertEqual(inputs.materialize_validation_plan(Path("input"), plan=plan), plan)
        descriptor, files = writing.call_args.args
        self.assertEqual(descriptor, 17)
        self.assertEqual((files[0].path, files[0].data), (grammar.CI_PLAN_NAME, canonical_json(plan)))
        checking.assert_called_once_with(Path("stage"), plan=plan)


class InputExecutionTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    validator = WorkerAccount("validator", 2001, 2001, "validator-home")
    candidate = WorkerAccount("candidate", 2000, 2000, "candidate-home")

    def exercise(self, *, envelope=None, reads=None, execution_error=None):
        envelope = ci_envelope() if envelope is None else envelope
        execution = WorkerResult(0, b"native verified", False)
        identities = ((1, 20), (1, 30))
        with patch.object(inputs, "authenticate_worker_account", side_effect=[self.validator, self.candidate]), \
                patch.object(inputs, "_read_inputs", side_effect=reads or [identities, identities]) as checking, \
                patch.object(inputs, "execute_controller_validator", return_value=execution,
                             side_effect=execution_error) as execute, \
                patch.object(inputs, "terminate_worker") as terminate:
            try:
                result = inputs.execute_frozen_build_validator(boundary=self.boundary, validator=self.validator,
                    sources=None, tools=ToolTreeProof(("/opt/hostedtoolcache/python",), "a" * 64, 1, 2, 100),
                    plan=ci_plan(), envelope=envelope, python="/opt/hostedtoolcache/python/bin/python",
                    java_home=None, run_id=42, run_attempt=2)
                self.assertEqual(result.execution, execution)
                self.assertEqual(result.input_sha256, hashlib.sha256(canonical_json(envelope)).hexdigest())
                self.assertEqual(checking.call_count, 2)
                self.assertEqual((execute.call_args.kwargs["hook"], execute.call_args.kwargs["unit_id"]), ("verify_build", None))
            finally:
                terminate.assert_called_once_with(self.validator)
                if envelope["producer"]["run_id"] != 42 or envelope["scope"] != "complete":
                    execute.assert_not_called()

    def test_retained_input_digest_and_execution_are_bound_to_complete_same_attempt_build(self):
        self.exercise()

    def test_wrong_producer_partial_pre_post_and_identity_changes_cannot_return_receipt(self):
        wrong_run = ci_envelope()
        wrong_run["producer"]["run_id"] = 43
        partial = ci_envelope()
        partial.update(scope="target", target_id="target-a")
        for args in ({"envelope": wrong_run}, {"envelope": partial},
                     {"reads": [MbError("wrong bytes")]},
                     {"reads": [((1, 20), (1, 30)), MbError("post bytes")]},
                     {"reads": [((1, 20), (1, 30)), ((1, 21), (1, 30))]},
                     {"execution_error": WorkerError("native rejected")}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)

    def test_fixed_read_inputs_require_exact_owners_modes_acls_and_inventory(self):
        info = lambda inode, owner=1001: SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner,
                                st_gid=2001, st_mode=stat.S_IFDIR | 0o750)
        with patch.object(inputs, "authenticate_host_boundary"), patch.object(inputs, "_layout"), \
                patch.object(inputs, "_open_directory", side_effect=[10, 11]), \
                patch.object(inputs.os, "fstat", side_effect=[info(20), info(30)]), \
                patch.object(inputs.os, "close"), \
                patch.object(inputs, "authenticate_tree_read_access") as metadata, \
                patch.object(inputs, "verify_validation_plan", return_value=ci_plan()), \
                patch.object(inputs, "verify_build_export", return_value=ci_envelope()):
            self.assertEqual(inputs._read_inputs(self.boundary, self.validator, ci_plan(), ci_envelope()), ((1,20),(1,30)))
            self.assertEqual([call.args[0] for call in metadata.call_args_list],
                             [inputs.VALIDATOR_INPUT_ROOT, inputs.BUILD_VALIDATION_ROOT])
            self.assertEqual(metadata.call_args.kwargs["max_entries"], limits.MAX_CI_EXPORT_ENTRIES)

    def test_foreign_metadata_unsafe_access_and_changed_build_bytes_refuse_inputs(self):
        metadata = SimpleNamespace(st_dev=1, st_ino=20, st_uid=1001, st_gid=2001,
                                   st_mode=stat.S_IFDIR | 0o750)
        for foreign, access_error, changed in [(True, None, False),
                                               (False, MbError("writable or ACL"), False),
                                               (False, None, True)]:
            with self.subTest(foreign=foreign, changed=changed), \
                    patch.object(inputs, "authenticate_host_boundary"), patch.object(inputs, "_layout"), \
                    patch.object(inputs, "_open_directory", return_value=10), \
                    patch.object(inputs.os, "fstat", return_value=SimpleNamespace(**{**vars(metadata), "st_uid": 2000 if foreign else 1001})), \
                    patch.object(inputs.os, "close"), \
                    patch.object(inputs, "authenticate_tree_read_access", side_effect=access_error), \
                    patch.object(inputs, "verify_validation_plan", return_value=ci_plan()), \
                    patch.object(inputs, "verify_build_export", return_value={} if changed else ci_envelope()), self.assertRaises(MbError):
                inputs._read_inputs(self.boundary, self.validator, ci_plan(), ci_envelope())


class TargetInputExecutionTests(unittest.TestCase):
    boundary = InputExecutionTests.boundary
    validator = InputExecutionTests.validator
    candidate = InputExecutionTests.candidate

    def exercise(self, *, plan=None, envelope=None, target_id="target-a", reads=None,
                 execution_error=None, reject_before_inputs=False):
        plan = ci_plan() if plan is None else plan
        envelope = ci_envelope() if envelope is None else envelope
        if envelope["scope"] == "complete" and reject_before_inputs is False:
            envelope = {**envelope, "scope": "target", "target_id": target_id}
        identities = ((1, 20), (1, 30))
        events = []
        execution = WorkerResult(0, b"target verified", False)
        def read(*args):
            events.append("read")
            value = (reads or [identities, identities])[events.count("read") - 1]
            if isinstance(value, BaseException):
                raise value
            self.assertEqual(args, (self.boundary, self.validator, plan, envelope))
            return value
        def launch(**kwargs):
            events.append("execute")
            self.assertEqual((kwargs["hook"], kwargs["unit_id"]), ("verify_target", target_id))
            if execution_error:
                raise execution_error
            return execution
        with patch.object(inputs, "authenticate_worker_account", side_effect=[self.validator, self.candidate]), \
                patch.object(inputs, "_read_inputs", side_effect=read) as checking, \
                patch.object(inputs, "execute_controller_validator", side_effect=launch) as execute, \
                patch.object(inputs, "terminate_worker", side_effect=lambda account: events.append("terminate")) as terminate:
            try:
                result = inputs.execute_frozen_target_validator(boundary=self.boundary, validator=self.validator,
                    sources=None, tools=ToolTreeProof(("/opt/hostedtoolcache/python",), "a" * 64, 1, 2, 100),
                    plan=plan, envelope=envelope, target_id=target_id,
                    python="/opt/hostedtoolcache/python/bin/python", java_home=None, run_id=42, run_attempt=2)
                self.assertEqual(result, inputs.BuildValidationExecution(execution, hashlib.sha256(canonical_json(envelope)).hexdigest()))
                self.assertEqual(events, ["read", "execute", "read", "terminate"])
            finally:
                terminate.assert_called_once_with(self.validator)
                if reject_before_inputs:
                    checking.assert_not_called()
                    execute.assert_not_called()

    def test_each_exact_partition_uses_only_its_enrolled_target_and_own_input_digest(self):
        from tests.test_ci_exports import partitions_fixture
        plan, partitions = partitions_fixture()
        for partition in partitions:
            envelope = partition["envelope"]
            with self.subTest(target=envelope["target_id"]):
                self.exercise(plan=plan, envelope=envelope, target_id=envelope["target_id"])

    def test_complete_other_target_unenrolled_malformed_and_mixed_attempt_inputs_never_launch(self):
        from tests.test_ci_exports import partitions_fixture
        plan, partitions = partitions_fixture()
        target = partitions[0]["envelope"]
        with self.assertRaises(MbError):
            self.exercise(envelope=ci_envelope(), reject_before_inputs=True)
        for target_id, envelope in [("target-a", ci_envelope()),
                ("target-b", copy.deepcopy(target)), ("unknown", copy.deepcopy(target)),
                (None, copy.deepcopy(target)), (False, copy.deepcopy(target)), ([], copy.deepcopy(target))]:
            with self.subTest(target=target_id), self.assertRaises(MbError):
                self.exercise(plan=plan, envelope=envelope, target_id=target_id, reject_before_inputs=True)
        for key, value in (("run_id", 43), ("run_attempt", 3)):
            changed = copy.deepcopy(target)
            changed["producer"][key] = value
            with self.subTest(producer=key), self.assertRaises(MbError):
                self.exercise(plan=plan, envelope=changed, reject_before_inputs=True)

    def test_missing_or_extra_target_outputs_never_reach_native_execution(self):
        target = ci_envelope()
        target.update(scope="target", target_id="target-a")
        for mutate in (lambda files: files.pop(), lambda files: files.append({**files[0], "path": "extra.jar"})):
            changed = copy.deepcopy(target)
            mutate(changed["files"])
            with self.assertRaises(MbError):
                self.exercise(envelope=changed, reject_before_inputs=True)

    def test_target_byte_and_identity_changes_or_native_failure_cannot_return_receipt(self):
        for args in ({"reads": [MbError("wrong target bytes")]},
                     {"reads": [((1, 20), (1, 30)), MbError("post target bytes")]},
                     {"reads": [((1, 20), (1, 30)), ((1, 20), (1, 31))]},
                     {"execution_error": WorkerError("native target rejected")}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)


class PlanHandoffTests(unittest.TestCase):
    def test_fixed_private_plan_copy_is_checked_before_and_after_group_read_grant(self):
        boundary = InputExecutionTests.boundary
        validator = InputExecutionTests.validator
        candidate = InputExecutionTests.candidate
        def info(owner, group, mode, inode=30):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)
        for foreign, transfer_error, inode in [(False, None, 30), (True, None, 30),
                                             (False, OSError("grant"), 30), (False, None, 31)]:
            with self.subTest(foreign=foreign, inode=inode), ExitStack() as stack:
                stack.enter_context(patch.object(inputs, "authenticate_privileged_host_boundary"))
                stack.enter_context(patch.object(inputs, "authenticate_worker_account", side_effect=[validator, candidate]))
                stack.enter_context(patch.object(inputs, "terminate_worker"))
                stack.enter_context(patch.object(inputs, "_layout"))
                stack.enter_context(patch.object(inputs, "_open_directory", return_value=12))
                stack.enter_context(patch.object(inputs.os, "fstat", side_effect=[info(2000 if foreign else 1001,121,0o700), info(1001,2001,0o750,inode)]))
                stack.enter_context(patch.object(inputs.os, "close"))
                modes = stack.enter_context(patch.object(inputs.os, "fchmod", create=True))
                stack.enter_context(patch.object(inputs.os, "fsync"))
                checking = stack.enter_context(patch.object(inputs, "verify_validation_plan", return_value=ci_plan()))
                grant = stack.enter_context(patch.object(inputs, "grant_tree_read_access", side_effect=transfer_error))
                stack.enter_context(patch.object(inputs, "authenticate_tree_read_access"))
                if not foreign and not transfer_error and inode == 30:
                    self.assertEqual(inputs.prepare_validation_plan(boundary=boundary, validator=validator, plan=ci_plan()), ci_plan())
                    self.assertEqual(checking.call_count, 2)
                    self.assertEqual(grant.call_args.kwargs["reader_gid"], 2001)
                    modes.assert_not_called()
                else:
                    with self.assertRaises(MbError):
                        inputs.prepare_validation_plan(boundary=boundary, validator=validator, plan=ci_plan())
                    if foreign:
                        modes.assert_not_called()
                        grant.assert_not_called()
                    else:
                        modes.assert_called_once_with(12, 0o700)


if __name__ == "__main__":
    unittest.main()
