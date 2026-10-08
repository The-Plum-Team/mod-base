"""Verifier output context, strict native JSON and independent byte-copy admission."""

import copy
import hashlib
import unittest
import stat
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import validation
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_plan, ci_validation


class ValidationTests(unittest.TestCase):
    def context(self, document):
        return {"plan": ci_plan(), **{key: document[key] for key in
                ("hook", "unit_id", "run_id", "run_attempt", "source_config_sha256", "input_sha256")}}

    def reader(self, document, *, context=None, data=None, records=None, final=None):
        raw = canonical_json(document)
        native = canonical_json({"fixture_unit": document["reports"][0]["unit_id"]}) if data is None else data
        inventory = [{key: report[key] for key in ("path", "size", "sha256")} for report in document["reports"]]
        with patch.object(validation, "validate_tree_entries"), \
                patch.object(validation, "read_child_file", side_effect=[raw, native, raw if final is None else final]), \
                patch.object(validation, "file_records", return_value=inventory if records is None else records):
            return validation.verify_validation_export(Path("frozen"), **(context or self.context(document)))

    def test_new_kind_and_all_closed_hook_scopes_bind_exact_protected_units(self):
        for hook, unit in (("verify_build", None), ("verify_target", "target-a"), ("verify_runtime", "lane-a")):
            document = ci_validation(hook, unit)
            with self.subTest(hook=hook):
                self.assertEqual(load_document(canonical_json(document), kind=document["kind"], plan=ci_plan()), document)
                self.assertEqual(self.reader(document), document)

    def test_closed_fields_versions_exact_types_and_nonempty_reports(self):
        original = ci_validation()
        for change in (lambda d: d.update(passed=True), lambda d: d.pop("input_sha256"),
                       lambda d: d.update(schema_version=2), lambda d: d.update(run_id=True),
                       lambda d: d.update(run_attempt=0), lambda d: d.update(reports=[]),
                       lambda d: d["reports"][0].update(size=0), lambda d: d["reports"][0].update(extra=1)):
            document = copy.deepcopy(original)
            change(document)
            with self.subTest(change=change), self.assertRaises(MbError):
                validation.validate_validation_receipt(document, plan=ci_plan())

    def test_original_build_report_bound_does_not_expand_validator_outputs(self):
        document = ci_validation()
        document["reports"][0]["size"] = 4 * 1024 * 1024
        validation.validate_validation_receipt(document, plan=ci_plan())
        document["reports"][0]["size"] += 1
        with self.assertRaises(MbError):
            validation.validate_validation_receipt(document, plan=ci_plan())

    def test_plan_contract_unit_identity_and_reserved_or_aliased_reports_reject(self):
        for change in (lambda d: d.update(unit_id="target-a"),
                       lambda d: d.update(profile="quick-skin"),
                       lambda d: d["identity"].update(head_sha="e" * 40),
                       lambda d: d["reports"][0].update(native_contract_sha256="a" * 64),
                       lambda d: d["reports"][0].update(unit_id="foreign"),
                       lambda d: d["reports"][0].update(path=grammar.CI_VALIDATION_NAME.upper()),
                       lambda d: d["reports"][0].update(path="../report.json"),
                       lambda d: d["reports"][0].update(path="reports/report.log"),
                       lambda d: d["reports"].append(copy.deepcopy(d["reports"][0]))):
            document = ci_validation()
            change(document)
            with self.subTest(change=change), self.assertRaises(MbError):
                validation.validate_validation_receipt(document, plan=ci_plan())

    def test_run_attempt_source_config_input_and_hook_context_cannot_be_substituted(self):
        document = ci_validation()
        for changed in ({"run_id": 43}, {"run_attempt": 3}, {"source_config_sha256": "a" * 64},
                        {"input_sha256": "b" * 64}, {"hook": "verify_target", "unit_id": "target-a"}):
            with self.subTest(changed=changed), self.assertRaises(MbError):
                self.reader(document, context={**self.context(document), **changed})

    def test_actual_report_bytes_inventory_and_record_stability_are_required(self):
        document = ci_validation()
        for args in ({"data": b"changed"}, {"records": []}, {"final": b"changed record"}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.reader(document, **args)

    def test_hash_matching_duplicate_keys_nonfinite_or_noncanonical_native_json_rejects(self):
        for data in (b'{"a":1,"a":2}\n', b'{"a":NaN}\n', b' {"a":1}\n', b'[]\n', b'not-json'):
            document = ci_validation()
            document["reports"][0].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            with self.subTest(data=data), self.assertRaises(MbError):
                self.reader(document, data=data)

    def test_expected_context_and_entry_cap_reject_before_content_reads(self):
        document = ci_validation()
        for changes in ({"run_id": True}, {"unit_id": "target-a"}, {"hook": "derive_plan"}):
            with patch.object(validation, "read_child_file") as reading, self.assertRaises(MbError):
                validation.verify_validation_export(Path("unused"), **{**self.context(document), **changes})
            reading.assert_not_called()
        with patch.object(validation, "validate_tree_entries", side_effect=MbError("entry cap")), \
                patch.object(validation, "read_child_file") as reading, self.assertRaises(MbError):
            validation.verify_validation_export(Path("unused"), **self.context(document))
        reading.assert_not_called()

    def test_atomic_copy_requires_exact_bytes_and_independent_stage_verification(self):
        document = ci_validation()
        inventory = [{key: report[key] for key in ("path", "size", "sha256")} for report in document["reports"]]
        raw = canonical_json(document)
        inventory.append({"path": grammar.CI_VALIDATION_NAME, "size": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
        inventory.sort(key=lambda record: record["path"])
        def publication(output, writer):
            return writer(Path("private-stage"), 17)
        with patch.object(validation, "verify_validation_export", side_effect=[document, document]) as checking, \
                patch.object(validation, "copy_regular_files", return_value=inventory), \
                patch.object(validation, "atomic_directory", side_effect=publication):
            self.assertEqual(validation.materialize_validation_export(Path("frozen"), Path("sealed"),
                                                                     **self.context(document)), document)
            self.assertEqual(checking.call_count, 2)
        with patch.object(validation, "verify_validation_export", return_value=document), \
                patch.object(validation, "copy_regular_files", return_value=[]), \
                patch.object(validation, "atomic_directory", side_effect=publication), self.assertRaises(MbError):
            validation.materialize_validation_export(Path("frozen"), Path("sealed"), **self.context(document))

    def test_rejected_original_never_allocates_a_stage(self):
        document = ci_validation()
        with patch.object(validation, "verify_validation_export", side_effect=MbError("bad original")), \
                patch.object(validation, "atomic_directory") as allocating, self.assertRaises(MbError):
            validation.materialize_validation_export(Path("frozen"), Path("sealed"), **self.context(document))
        allocating.assert_not_called()


class ValidationFreezeTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    validator = WorkerAccount("validator", 2001, 2001, "/tmp/validator-home")
    candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/candidate-home")

    def exercise(self, *, foreign=False, final_inode=30, transfer_error=None, changed=False, kill_error=None):
        from tests.test_ci_controller import ControllerSourceTests
        plan, api, _, protected = ControllerSourceTests().fixture()
        from mod_base.build_ci.controller import authenticate_controller_sources
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        expected = ci_validation()
        expected["source_config_sha256"] = sources.config.sha256
        events = []
        def record(name, value):
            def called(*args, **kwargs):
                events.append(name)
                if name == "terminate" and kill_error:
                    raise kill_error
                if name == "transfer" and transfer_error:
                    raise transfer_error
                return value
            return called
        def info(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group,
                                   st_mode=stat.S_IFDIR | mode)
        metadata = [info(20, 1001, 121, 0o711), info(21, 1001, 121, 0o711),
                    info(22, 2001, 2001, 0o700), info(30, 2001 if foreign else 0, 0, 0o700),
                    info(final_inode, 1001, 121, 0o700)]
        with ExitStack() as stack:
            stack.enter_context(patch.object(validation, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(validation, "authenticate_worker_account", side_effect=[self.validator, self.candidate]))
            stack.enter_context(patch.object(validation, "terminate_worker", side_effect=record("terminate", None)))
            stack.enter_context(patch.object(validation, "_open_directory", side_effect=[10, 11, 12, 13]))
            stack.enter_context(patch.object(validation.os, "fstat", side_effect=metadata))
            stack.enter_context(patch.object(validation.os, "close"))
            modes = stack.enter_context(patch.object(validation.os, "fchmod", create=True))
            stack.enter_context(patch.object(validation.os, "fsync"))
            admission = stack.enter_context(patch.object(validation, "authenticate_tree_private_access",
                                                          side_effect=record("metadata", None)))
            copying = stack.enter_context(patch.object(validation, "materialize_validation_export", side_effect=record("copy", expected)))
            transfer = stack.enter_context(patch.object(validation, "privatize_tree_copy", side_effect=record("transfer", [])))
            stack.enter_context(patch.object(validation, "verify_validation_export", side_effect=record("verify", {} if changed else expected)))
            try:
                observed = validation.freeze_validation_export(boundary=self.boundary, validator=self.validator,
                         sources=sources, execution=WorkerResult(0, b"native execution", False), plan=plan,
                         hook="verify_build", unit_id=None, run_id=42, run_attempt=2,
                         input_sha256=expected["input_sha256"])
                self.assertEqual(observed, expected)
                self.assertEqual(events, ["terminate", "metadata", "copy", "transfer", "metadata", "verify"])
                self.assertEqual(copying.call_args.args, (validation.VALIDATOR_OUTPUT_ROOT, validation.SEALED_VALIDATION_ROOT))
                self.assertEqual(copying.call_args.kwargs["source_config_sha256"], sources.config.sha256)
                self.assertEqual(transfer.call_args.kwargs["source_owner_uid"], 0)
                self.assertEqual(transfer.call_args.kwargs["owner_uid"], self.boundary.uid)
                self.assertEqual(admission.call_args.kwargs["owner_gid"], self.boundary.gid)
                modes.assert_not_called()
            except MbError:
                if foreign or kill_error:
                    modes.assert_not_called()
                else:
                    modes.assert_called_once_with(13, 0o700)
                raise

    def test_fixed_output_layout_quiescence_context_and_private_owner_transfer(self):
        self.exercise()

    def test_survivor_foreign_copy_transfer_inode_and_record_failures_never_succeed(self):
        for args in ({"kill_error": WorkerError("survivor")}, {"foreign": True},
                     {"transfer_error": OSError("failed chown")}, {"final_inode": 31}, {"changed": True}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(**args)

    def test_nonzero_boolean_or_missing_execution_cannot_trigger_copy_or_termination(self):
        from tests.test_ci_controller import ControllerSourceTests
        plan, api, _, protected = ControllerSourceTests().fixture()
        from mod_base.build_ci.controller import authenticate_controller_sources
        sources = authenticate_controller_sources(api, identity=plan["identity"], protected_paths=protected)
        for result in (None, WorkerResult(17, b"failed", False), WorkerResult(False, b"forged", False)):
            with patch.object(validation, "authenticate_privileged_host_boundary"), \
                    patch.object(validation, "materialize_validation_export") as copying, \
                    patch.object(validation, "terminate_worker") as killing, self.assertRaises(MbError):
                validation.freeze_validation_export(boundary=self.boundary, validator=self.validator,
                          sources=sources, execution=result, plan=plan, hook="verify_build", unit_id=None,
                          run_id=42, run_attempt=2, input_sha256="a" * 64)
            copying.assert_not_called()
            killing.assert_not_called()
