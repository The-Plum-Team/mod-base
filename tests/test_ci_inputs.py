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
from tests.helpers import ci_envelope, ci_plan, ci_plan_inputs


class ValidationPlanTests(unittest.TestCase):
    """The input root in mocks: order of the checks. ``tests/test_ci_lifecycle.py`` has real trees."""

    def test_exact_plan_inventory_and_bytes_are_both_required(self):
        plan, sources = ci_plan_inputs()
        raw = canonical_json(plan)
        digests = inputs.plan_source_digests(plan)
        self.assertEqual(list(digests), ["inventory", "scenario-contract", "gradle-properties"])
        self.assertEqual(digests, {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()})
        record = {"path": grammar.CI_PLAN_NAME, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        inventory, scenario, extra = ({"path": name, "size": len(sources[name]), "sha256": digests[name]}
                                      for name in digests)
        complete = [record, extra, inventory, scenario]
        for records, data, success in [(complete, raw, True), ([], raw, False), ([record], raw, False),
                ([extra, inventory, scenario], raw, False), ([record, inventory, scenario], raw, False),
                ([*complete, {**record, "path": "extra.json"}], raw, False),
                ([{**record, "sha256": "0" * 64}, extra, inventory, scenario], raw, False),
                ([record, extra, {**inventory, "sha256": digests["scenario-contract"]}, scenario], raw, False),
                ([record, {**extra, "sha256": digests["inventory"]}, inventory, scenario], raw, False),
                ([record, extra, inventory, {**scenario, "path": "scenario"}], raw, False),
                ([record, {**extra, "path": "gradle.properties"}, inventory, scenario], raw, False),
                ([record, extra, {**inventory, "size": limits.MAX_CI_PLAN_SOURCE_BYTES + 1}, scenario], raw, False),
                (complete, b" " + raw, False)]:
            with self.subTest(success=success, data=data[:1], records=[entry["path"] for entry in records]), \
                    patch.object(inputs, "validate_tree_entries") as entries, \
                    patch.object(inputs, "file_records", return_value=records), \
                    patch.object(inputs, "read_child_file", return_value=data):
                if success:
                    self.assertEqual(inputs.verify_validation_plan(Path("input"), plan=plan), plan)
                else:
                    with self.assertRaises(MbError):
                        inputs.verify_validation_plan(Path("input"), plan=plan)
                # Exactly the files of this state and their directory: no room for another entry.
                entries.assert_called_once_with(Path("input"), max_entries=5)

    def test_entry_preflight_precedes_content_and_invalid_plan_precedes_copy(self):
        plan, sources = ci_plan_inputs()
        with patch.object(inputs, "validate_tree_entries", side_effect=MbError("entries")), \
                patch.object(inputs, "file_records") as records, self.assertRaises(MbError):
            inputs.verify_validation_plan(Path("input"), plan=plan)
        records.assert_not_called()
        without_extra = {name: data for name, data in sources.items() if name != "gradle-properties"}
        # A malformed plan, a plan derived from other files, and bytes or names that are no candidate file.
        for arguments in (dict(sources=sources, plan={**plan, "unknown": True}),
                          dict(sources={**sources, "inventory": b"another inventory"}, plan=plan),
                          dict(sources={**sources, "gradle-properties": b"version=2"}, plan=plan),
                          dict(sources=without_extra, plan=plan),
                          dict(sources={**sources, "another-input": b"x"}, plan=plan),
                          dict(sources={**sources, "inventory": b""}),
                          dict(sources={**sources, "scenario-contract": None}),
                          dict(sources={**sources, "inventory": "inventory"}),
                          dict(sources={"inventory": b"inventory"}),
                          dict(sources={**sources, "ci-plan.json": b"{}"}),
                          dict(sources={**sources, "Not A Token": b"x"}),
                          dict(sources={**sources, **{f"extra-{index}": b"x" for index in range(limits.MAX_CI_PLAN_INPUTS)}}),
                          dict(sources=list(sources)),
                          dict(sources={**sources, "inventory": b"x" * (limits.MAX_CI_PLAN_SOURCE_BYTES + 1)})):
            with self.subTest(arguments=str(arguments)[:70]), patch.object(inputs, "atomic_directory") as atomic, \
                    self.assertRaises(MbError):
                inputs.materialize_validation_inputs(Path("input"), **arguments)
            atomic.assert_not_called()

    def test_independent_canonical_stage_is_verified_before_publication(self):
        plan, sources = ci_plan_inputs()
        def atomic(output, writer):
            self.assertEqual(output, Path("input"))
            return writer(Path("stage"), 17)
        for staged_plan in (plan, None):
            with self.subTest(plan=staged_plan is not None), \
                    patch.object(inputs, "atomic_directory", side_effect=atomic), \
                    patch.object(inputs, "_write_controller_files") as writing, \
                    patch.object(inputs, "verify_validation_inputs") as checking:
                inputs.materialize_validation_inputs(Path("input"), sources=sources, plan=staged_plan)
            descriptor, files = writing.call_args.args
            self.assertEqual(descriptor, 17)
            expected = [("gradle-properties", sources["gradle-properties"]), ("inventory", sources["inventory"]),
                        ("scenario-contract", sources["scenario-contract"])]
            if staged_plan is not None:
                expected.insert(0, (grammar.CI_PLAN_NAME, canonical_json(plan)))
            self.assertEqual([(file.path, file.data) for file in files], expected)
            checking.assert_called_once_with(Path("stage"), digests=inputs.plan_source_digests(plan),
                                             plan=staged_plan)


class InputExecutionTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    validator = WorkerAccount("validator", 2001, 2001, "validator-home")
    candidate = WorkerAccount("candidate", 2000, 2000, "candidate-home")

    def exercise(self, *, envelope=None, reads=None, execution_error=None):
        envelope = ci_envelope() if envelope is None else envelope
        execution = WorkerResult(0, b"native verified", False)
        identities = ((1, 20), (1, 30))
        with patch.object(inputs, "authenticate_worker_account", return_value=self.validator), \
                patch.object(inputs, "authenticate_peer_account", return_value=self.candidate), \
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
        with patch.object(inputs, "authenticate_worker_account", return_value=self.validator), \
                patch.object(inputs, "authenticate_peer_account", return_value=self.candidate), \
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
        plan, _ = ci_plan_inputs()
        digests = inputs.plan_source_digests(plan)
        def info(owner, group, mode, inode=30):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)
        cases = [(False, None, 30, candidate, derived) for candidate in (InputExecutionTests.candidate, None)
                 for derived in (False, True)]
        cases += [(True, None, 30, None, False), (False, OSError("grant"), 30, None, False),
                  (False, None, 31, None, False)]
        for foreign, transfer_error, inode, candidate, derived in cases:
            with self.subTest(foreign=foreign, inode=inode, candidate=candidate, derived=derived), \
                    ExitStack() as stack:
                stack.enter_context(patch.object(inputs, "authenticate_privileged_host_boundary"))
                stack.enter_context(patch.object(inputs, "authenticate_worker_account", return_value=validator))
                stack.enter_context(patch.object(inputs, "authenticate_peer_account", return_value=candidate))
                kill = stack.enter_context(patch.object(inputs, "terminate_worker"))
                stack.enter_context(patch.object(inputs, "_layout"))
                stack.enter_context(patch.object(inputs, "_open_directory", return_value=12))
                stack.enter_context(patch.object(inputs.os, "fstat", side_effect=[info(2000 if foreign else 1001,121,0o700), info(1001,2001,0o750,inode)]))
                stack.enter_context(patch.object(inputs.os, "close"))
                modes = stack.enter_context(patch.object(inputs.os, "fchmod", create=True))
                stack.enter_context(patch.object(inputs.os, "fsync"))
                checking = stack.enter_context(patch.object(inputs, "verify_validation_inputs"))
                grant = stack.enter_context(patch.object(inputs, "grant_tree_read_access", side_effect=transfer_error))
                stack.enter_context(patch.object(inputs, "authenticate_tree_read_access"))
                def prepare():
                    if derived:  # Before the plan exists: the candidate files alone.
                        return inputs.prepare_plan_inputs(boundary=boundary, validator=validator, digests=digests)
                    return inputs.prepare_validation_plan(boundary=boundary, validator=validator, plan=plan)
                if not foreign and not transfer_error and inode == 30:
                    self.assertEqual(prepare(), None if derived else plan)
                    expected = unittest.mock.call(inputs.VALIDATOR_INPUT_ROOT, digests=digests,
                                                  plan=None if derived else plan)
                    self.assertEqual(checking.call_args_list, [expected, expected])
                    self.assertEqual(grant.call_args.kwargs["reader_gid"], 2001)
                    self.assertEqual(grant.call_args.kwargs["max_files"], 3 + limits.MAX_CI_PLAN_INPUTS)
                    # A job with the validator alone has no candidate to stop first.
                    self.assertEqual(kill.call_args_list, [] if candidate is None else [unittest.mock.call(candidate)])
                    modes.assert_not_called()
                else:
                    with self.assertRaises(MbError):
                        prepare()
                    if foreign:
                        modes.assert_not_called()
                        grant.assert_not_called()
                    else:
                        modes.assert_called_once_with(12, 0o700)

    def test_digests_that_are_no_digests_or_not_the_plans_reject_before_any_account_is_touched(self):
        boundary, validator = InputExecutionTests.boundary, InputExecutionTests.validator
        plan, _ = ci_plan_inputs()
        good = inputs.plan_source_digests(plan)
        with patch.object(inputs, "authenticate_privileged_host_boundary"), \
                patch.object(inputs, "authenticate_worker_account") as accounts:
            for digests in ({**good, "inventory": "A" * 64}, {**good, "scenario-contract": "b" * 63},
                            {**good, "gradle-properties": None}, {"inventory": "a" * 64},
                            {**good, "ci-plan.json": "c" * 64}, {**good, "Bad Name": "c" * 64}, [], None,
                            {**good, **{f"extra-{index}": "c" * 64 for index in range(limits.MAX_CI_PLAN_INPUTS)}}):
                with self.subTest(digests=str(digests)[:60]), self.assertRaises(MbError):
                    inputs.prepare_plan_inputs(boundary=boundary, validator=validator, digests=digests)
            with self.assertRaises(MbError):
                inputs.prepare_validation_plan(boundary=boundary, validator=validator, plan={**plan, "extra": 1})
        accounts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
