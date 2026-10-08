"""Complete target-union and frozen-inventory admission, independent of native witnesses."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import ExitStack

from mod_base.build_ci import exports
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult
from mod_base.build_ci.exports import materialize_build_export, validate_target_partitions, verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.errors import MbError
from mod_base.io.tree import EXPORT_PATHS
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_descriptor, ci_envelope, ci_plan


def partitions_fixture():
    plan = ci_plan()
    target = copy.deepcopy(plan["targets"][0])
    target["id"] = "target-b"
    for output in target["outputs"]:
        output["path"] = output["path"].replace("lane-a", "lane-b")
        output["lane_id"] = "lane-b"
    lane = copy.deepcopy(plan["lanes"][0])
    lane.update(id="lane-b", target_id="target-b")
    plan["targets"].append(target)
    plan["lanes"].append(lane)
    plan["plan_sha256"] = plan_sha256(plan)
    partitions = []
    for index, target in enumerate(plan["targets"]):
        envelope = ci_envelope()
        envelope.update(plan_sha256=plan["plan_sha256"], scope="target", target_id=target["id"])
        envelope["files"] = sorted([{**output, "size": 128, "sha256": "a" * 64}
                                    for output in target["outputs"]], key=lambda file: file["path"])
        envelope["native_reports"] = [file["path"] for file in envelope["files"] if file["role"] == "native-report"]
        descriptor = ci_descriptor("target", unit_id=target["id"], artifact_id=100 + index)
        descriptor["plan_sha256"] = plan["plan_sha256"]
        partitions.append({"descriptor": descriptor, "envelope": envelope})
    return plan, partitions


class TargetUnionTests(unittest.TestCase):
    def test_complete_union_and_independent_upload_windows(self):
        plan, partitions = partitions_fixture()
        window = {"started_at": "2026-10-07T10:00:00Z", "completed_at": "2026-10-07T10:03:00Z"}
        partitions[1]["descriptor"]["producer"]["upload_window"] = window
        files = validate_target_partitions(partitions, plan=plan)
        self.assertEqual(len(files), 8)
        self.assertEqual([file["path"] for file in files], sorted(output["path"] for target in plan["targets"]
                                                                for output in target["outputs"]))

    def test_partial_extra_reordered_wrong_binding_and_duplicate_artifact_are_rejected(self):
        mutations = [lambda p: p.pop(), lambda p: p.append(copy.deepcopy(p[0])), lambda p: p.reverse(),
                     lambda p: p[1]["descriptor"]["artifact"].update(id=100),
                     lambda p: p[1]["descriptor"]["artifact"].update(name=grammar.ci_artifact_name("build", 42, 2)),
                     lambda p: p[1]["descriptor"].update(plan_sha256="b" * 64),
                     lambda p: p[1]["envelope"].update(scope="complete", target_id=None),
                     lambda p: p[1]["envelope"]["files"].pop(),
                     lambda p: p[1].update(destination="arbitrary")]
        for index, mutate in enumerate(mutations):
            plan, partitions = partitions_fixture()
            mutate(partitions)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_target_partitions(partitions, plan=plan)

    def test_individually_valid_different_attempts_and_producers_cannot_form_a_union(self):
        for change in ({"run_attempt": 3}, {"run_id": 43}, {"api_head_sha": "b" * 40},
                       {"graph_sha256": "c" * 64}):
            plan, partitions = partitions_fixture()
            for key in ("descriptor", "envelope"):
                partitions[1][key]["producer"].update(change)
            producer = partitions[1]["descriptor"]["producer"]
            partitions[1]["descriptor"]["artifact"]["name"] = grammar.ci_artifact_name(
                "target", producer["run_id"], producer["run_attempt"], "target-b")
            with self.subTest(change=change), self.assertRaises(MbError):
                validate_target_partitions(partitions, plan=plan)

    def test_partitioning_does_not_multiply_whole_tree_budget(self):
        plan, partitions = partitions_fixture()
        # Each partition separately fits 2 GiB; their coherent union exceeds it.
        for partition in partitions:
            # SBOM's generic per-file cap permits a 1 GiB witness; other roles stay within their caps.
            for file in partition["envelope"]["files"]:
                file["size"] = limits.MAX_CI_EXPORT_FILE_BYTES if file["role"] == "sbom" else 128
        with self.assertRaisesRegex(MbError, "whole-tree"):
            validate_target_partitions(partitions, plan=plan)

    def test_complete_logical_entry_budget_includes_inferred_directories_and_envelope(self):
        plan, partitions = partitions_fixture()
        files = [file for partition in partitions for file in partition["envelope"]["files"]]
        directories = {"/".join(file["path"].split("/")[:i]) for file in files
                       for i in range(1, len(file["path"].split("/")))}
        bound = len(files) + len(directories) + 2
        with patch.object(limits, "MAX_CI_EXPORT_ENTRIES", bound):
            validate_target_partitions(partitions, plan=plan)
        with patch.object(limits, "MAX_CI_EXPORT_ENTRIES", bound - 1), self.assertRaisesRegex(MbError, "entry cap"):
            validate_target_partitions(partitions, plan=plan)


class FrozenInventoryTests(unittest.TestCase):
    """Exercise admission using MB1 return values; MB1 tests own actual no-follow traversal."""

    def check_export(self, *, envelope=None, observed=None, raw=None, second_raw=None):
        envelope = ci_envelope() if envelope is None else envelope
        raw = canonical_json(envelope) if raw is None else raw
        observed = ([{key: file[key] for key in ("path", "size", "sha256")} for file in envelope["files"]]
                    if observed is None else observed)
        with patch("mod_base.build_ci.exports.validate_tree_entries"), \
             patch("mod_base.build_ci.exports.read_child_file", side_effect=[raw, raw if second_raw is None else second_raw]), \
             patch("mod_base.build_ci.exports.file_records", return_value=observed) as inventory:
            result = verify_build_export(Path("frozen"), plan=ci_plan())
            self.assertEqual(inventory.call_args.kwargs["max_files"], limits.MAX_CI_EXPORT_FILES + 1)
            self.assertEqual(inventory.call_args.kwargs["max_total_bytes"], limits.MAX_CI_EXPORT_TREE_BYTES + len(raw))
            self.assertIs(inventory.call_args.kwargs["rule"], EXPORT_PATHS)
            return result

    def test_exact_inventory_is_accepted(self):
        self.assertEqual(self.check_export(), ci_envelope())

    def test_entry_budget_rejection_precedes_envelope_content_reads(self):
        with patch("mod_base.build_ci.exports.validate_tree_entries", side_effect=MbError("entry cap")), \
                patch("mod_base.build_ci.exports.read_child_file") as content, self.assertRaises(MbError):
            verify_build_export(Path("hostile"), plan=ci_plan())
        content.assert_not_called()

    def test_missing_extra_size_hash_and_changed_envelope_are_rejected(self):
        files = [{key: file[key] for key in ("path", "size", "sha256")} for file in ci_envelope()["files"]]
        for change in (lambda f: f.pop(), lambda f: f.append({"path": "secret.txt", "size": 1, "sha256": "b" * 64}),
                       lambda f: f[0].update(size=129), lambda f: f[0].update(sha256="b" * 64)):
            observed = copy.deepcopy(files)
            change(observed)
            with self.assertRaises(MbError):
                self.check_export(observed=observed)
        with self.assertRaisesRegex(MbError, "changed"):
            self.check_export(second_raw=b"{}")

    def test_noncanonical_duplicate_keys_and_wrong_plan_are_rejected_before_inventory(self):
        raw = canonical_json(ci_envelope())
        envelope = ci_envelope()
        envelope["identity"]["tested_sha"] = "a" * 40
        for invalid in (b" " + raw, raw.replace(b'{', b'{"kind":"duplicate",', 1), canonical_json(envelope)):
            with patch("mod_base.build_ci.exports.file_records") as inventory, self.assertRaises(MbError):
                self.check_export(raw=invalid)
            inventory.assert_not_called()


class ExportMaterializationTests(unittest.TestCase):
    def test_copy_requires_exact_admitted_inventory_and_independent_stage_verification(self):
        expected = ci_envelope()
        records = [{key: file[key] for key in ("path", "size", "sha256")} for file in expected["files"]]
        raw = canonical_json(expected)
        records.append({"path": grammar.CI_ENVELOPE_NAME, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        records.sort(key=lambda file: file["path"])
        def atomic(output, writer):
            self.assertEqual(output, Path("sealed"))
            return writer(Path("private-stage"), 100)
        for copied, staged, success in [(records, expected, True), (records[:-1], expected, False),
                                       (records, {**expected, "profile": "other"}, False)]:
            with self.subTest(success=success), \
                    patch("mod_base.build_ci.exports.atomic_directory", side_effect=atomic), \
                    patch("mod_base.build_ci.exports.verify_build_export", side_effect=[expected, staged]), \
                    patch("mod_base.build_ci.exports.copy_regular_files", return_value=copied) as copying:
                if success:
                    self.assertEqual(materialize_build_export(Path("reclaimed"), Path("sealed"), plan=ci_plan()), expected)
                    self.assertEqual(copying.call_args.kwargs["max_entries"], limits.MAX_CI_EXPORT_ENTRIES)
                    self.assertIs(copying.call_args.kwargs["rule"], EXPORT_PATHS)
                else:
                    with self.assertRaises(MbError):
                        materialize_build_export(Path("reclaimed"), Path("sealed"), plan=ci_plan())

    def test_failed_original_admission_never_allocates_a_copy(self):
        with patch("mod_base.build_ci.exports.verify_build_export", side_effect=MbError("bad source")), \
                patch("mod_base.build_ci.exports.atomic_directory") as atomic, self.assertRaises(MbError):
            materialize_build_export(Path("reclaimed"), Path("sealed"), plan=ci_plan())
        atomic.assert_not_called()


class FrozenExportTreeTests(unittest.TestCase):
    """Real temporary trees through verification, assembly and the atomic private copy (no seams).

    Declared files come from the synthetic plan; the walk holds every entry of the tree, declared
    or not, to the export path rule.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.output = self.base / "export", self.base / "sealed"
        self.plan, self.envelope = ci_plan(), ci_envelope()
        self.payloads = self.write(self.root, self.envelope)

    @staticmethod
    def write(root, envelope):
        """Give every declared file real bytes, then freeze the envelope that now describes them."""
        payloads = {}
        for file in envelope["files"]:
            data = f"bytes of {file['path']}".encode()
            file.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
            payloads[file["path"]] = data
            (root / file["path"]).parent.mkdir(parents=True, exist_ok=True)
            (root / file["path"]).write_bytes(data)
        (root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
        return payloads

    def test_exact_tree_is_verified_and_copied_into_a_new_private_directory(self):
        self.assertEqual(verify_build_export(self.root, plan=self.plan), self.envelope)
        self.assertEqual(materialize_build_export(self.root, self.output, plan=self.plan), self.envelope)
        self.assertEqual(verify_build_export(self.output, plan=self.plan), self.envelope)
        for name, data in self.payloads.items():
            self.assertEqual((self.output / name).read_bytes(), data)
            self.assertNotEqual(os.stat(self.output / name).st_ino, os.stat(self.root / name).st_ino)

    def test_a_directory_named_like_the_mods_files_is_an_export_path(self):
        (self.root / "staged" / "Quick Skin - Fabric - 1.21.4+build.1").mkdir()
        self.assertEqual(verify_build_export(self.root, plan=self.plan), self.envelope)
        self.assertEqual(materialize_build_export(self.root, self.output, plan=self.plan), self.envelope)
        self.assertEqual(sorted(os.listdir(self.output / "staged")), ["lane-a"])  # an empty directory is not copied

    def test_hostile_entries_never_verify_or_publish(self):
        report = Path("staged") / "lane-a" / "native-report.json"
        mutations = {
            "hidden directory": lambda: (self.root / ".gradle").mkdir(),
            "case alias directory": lambda: (self.root / "Staged").mkdir(),
            "case alias file": lambda: (self.root / "staged" / "lane-a" / "SBOM.json").write_bytes(b"{}"),
            "trailing dot": lambda: (self.root / "staged" / "notes.").mkdir(),
            "double space": lambda: (self.root / "staged" / "a  b").mkdir(),
            "non-ASCII": lambda: (self.root / "staged" / "caf\xe9").mkdir(),
            "undeclared file": lambda: (self.root / "staged" / "lane-a" / "extra.jar").write_bytes(b"extra"),
            "empty file": lambda: (self.root / report).write_bytes(b""),
            "symlink": lambda: (self.root / "staged" / "link.jar").symlink_to(self.root / report),
            "hard link": lambda: os.link(self.root / report, self.base / "alias"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                with self.assertRaises(MbError):
                    verify_build_export(self.root, plan=self.plan)
                with self.assertRaises(MbError):
                    materialize_build_export(self.root, self.output, plan=self.plan)
                self.assertFalse(self.output.exists())

    def test_target_inputs_assemble_into_one_complete_export(self):
        plan, partitions = partitions_fixture()
        payloads = {}
        for index, partition in enumerate(partitions):
            payloads.update(self.write(self.base / "inputs" / f"target-{index}", partition["envelope"]))
        complete = exports.assemble_build_export(self.base / "inputs", partitions=partitions, plan=plan,
                                                 run_id=42, run_attempt=2, output=self.output)
        self.assertEqual((complete["scope"], [file["path"] for file in complete["files"]]), ("complete", sorted(payloads)))
        self.assertEqual(verify_build_export(self.output, plan=plan), complete)
        for name, data in payloads.items():
            self.assertEqual((self.output / name).read_bytes(), data)
        (self.base / "inputs" / "target-1" / "Staged").mkdir()
        with self.assertRaises(MbError):
            exports.assemble_build_export(self.base / "inputs", partitions=partitions, plan=plan,
                                          run_id=42, run_attempt=2, output=self.base / "second")
        self.assertFalse((self.base / "second").exists())


class BuildReadHandoffTests(unittest.TestCase):
    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/candidate-home")
    validator = WorkerAccount("validator", 2001, 2001, "/tmp/validator-home")

    def handoff(self, *, parent_owner=1001, initial_owner=1001, final_inode=30, final_mode=0o750,
                terminate_error=None, grant_error=None, changed_envelope=False):
        def metadata(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group,
                                   st_mode=stat.S_IFDIR | mode)
        info = [metadata(20, parent_owner, 121, 0o711), metadata(21, 1001, 121, 0o711),
                metadata(30, initial_owner, 121, 0o700), metadata(final_inode, 1001, 2001, final_mode)]
        events = []
        expected = ci_envelope()
        def verify(*args, **kwargs):
            events.append("verify")
            return {**expected, "profile": "changed"} if changed_envelope and len(events) > 3 else expected
        def grant(*args, **kwargs):
            events.append("grant")
            self.assertEqual(args[0], exports.BUILD_VALIDATION_ROOT)
            self.assertEqual(kwargs["owner_uid"], self.boundary.uid)
            self.assertEqual(kwargs["reader_gid"], self.validator.gid)
            self.assertIs(kwargs["rule"], EXPORT_PATHS)
            if grant_error:
                raise grant_error
        def terminate(account):
            self.assertEqual(account, self.candidate)
            events.append("terminate")
            if terminate_error:
                raise terminate_error
        with ExitStack() as stack:
            stack.enter_context(patch.object(exports, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(exports, "authenticate_worker_account",
                                              side_effect=[self.validator, self.candidate]))
            opening = stack.enter_context(patch.object(exports, "_open_directory", side_effect=[10, 11, 12]))
            stack.enter_context(patch.object(exports.os, "fstat", side_effect=info))
            stack.enter_context(patch.object(exports.os, "close"))
            modes = stack.enter_context(patch.object(exports.os, "fchmod", create=True))
            stack.enter_context(patch.object(exports.os, "fsync"))
            stack.enter_context(patch.object(exports, "terminate_worker", side_effect=terminate))
            stack.enter_context(patch.object(exports, "verify_build_export", side_effect=verify))
            stack.enter_context(patch.object(exports, "grant_tree_read_access", side_effect=grant))
            try:
                result = exports.prepare_build_validation(boundary=self.boundary, validator=self.validator, plan=ci_plan())
                self.assertEqual(result, expected)
                self.assertEqual(events, ["terminate", "verify", "grant", "verify"])
                self.assertEqual(opening.call_args.args[0], tuple(exports.BUILD_VALIDATION_ROOT.parts[1:]))
                modes.assert_not_called()
            except MbError:
                if parent_owner == 1001 and initial_owner == 1001:
                    modes.assert_called_once_with(12, 0o700)
                else:
                    modes.assert_not_called()
                raise

    def test_fixed_host_accounts_copy_and_order_bind_the_read_handoff(self):
        self.handoff()

    def test_foreign_layout_owner_kill_grant_identity_and_envelope_failures_cannot_succeed(self):
        for args in ({"parent_owner": 2000}, {"initial_owner": 2000},
                     {"terminate_error": WorkerError("survivor")}, {"grant_error": OSError("chmod failed")},
                     {"final_inode": 31}, {"final_mode": 0o777}, {"changed_envelope": True}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.handoff(**args)

    def test_forged_validator_and_runner_collisions_reject_before_mutation(self):
        for requested, actual, candidate in [(self.candidate, self.validator, self.candidate),
                                             (self.validator, self.candidate, self.candidate),
                                             (self.validator, self.validator,
                                              WorkerAccount("candidate", 1001, 2000, "home")),
                                             (self.validator, self.validator,
                                              WorkerAccount("candidate", 2000, 2001, "home"))]:
            with self.subTest(requested=requested), \
                    patch.object(exports, "authenticate_privileged_host_boundary"), \
                    patch.object(exports, "authenticate_worker_account", side_effect=[actual, candidate]), \
                    patch.object(exports, "_open_directory") as opening, self.assertRaises(MbError):
                exports.prepare_build_validation(boundary=self.boundary, validator=requested, plan=ci_plan())
            opening.assert_not_called()


class CandidateFreezeTests(unittest.TestCase):
    """Composition checks; required Linux fixtures own actual UID/filesystem evidence."""

    boundary = HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)
    candidate = WorkerAccount("candidate", 2000, 2000, "candidate-home")
    validator = WorkerAccount("validator", 2001, 2001, "validator-home")

    def freeze(self, *, source_error=None, changed_source=False, kill_error=None,
               private_error=None, copy_owner=0, transfer_error=None, final_inode=30,
               changed_export=False):
        def metadata(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group,
                                   st_mode=stat.S_IFDIR | mode)
        info = [metadata(20, 1001, 121, 0o711), metadata(21, 1001, 121, 0o711),
                metadata(22, 2000, 2000, 0o700), metadata(23, 2000, 2000, 0o700),
                metadata(30, copy_owner, 0, 0o700), metadata(final_inode, 1001, 121, 0o700)]
        events = []
        source_calls = 0
        expected = ci_envelope()
        def terminate(account):
            self.assertEqual(account, self.candidate)
            events.append("terminate")
            if kill_error:
                raise kill_error
        def source(root, **kwargs):
            nonlocal source_calls
            source_calls += 1
            events.append("source")
            self.assertEqual(root, exports.CANDIDATE_SOURCE_ROOT)
            self.assertEqual(kwargs, {"inventory": (), "generated_roots": ("build",)})
            if source_error:
                raise source_error
            return ["changed"] if changed_source and source_calls == 2 else ["original"]
        def private(root, **kwargs):
            events.append("private-original" if root == exports.CANDIDATE_OUTPUT_ROOT else "private-copy")
            self.assertEqual(kwargs["owner_uid"], 2000 if root == exports.CANDIDATE_OUTPUT_ROOT else 1001)
            if private_error:
                raise private_error
        def copying(root, output, **kwargs):
            events.append("copy")
            self.assertEqual((root, output), (exports.CANDIDATE_OUTPUT_ROOT, exports.BUILD_VALIDATION_ROOT))
            self.assertEqual(kwargs["plan"], ci_plan())
            return expected
        def transfer(root, **kwargs):
            events.append("transfer")
            self.assertEqual(root, exports.BUILD_VALIDATION_ROOT)
            self.assertEqual((kwargs["source_owner_uid"], kwargs["owner_uid"], kwargs["owner_gid"]), (0, 1001, 121))
            self.assertIs(kwargs["rule"], EXPORT_PATHS)
            if transfer_error:
                raise transfer_error
        def verify(root, **kwargs):
            events.append("verify")
            self.assertEqual(root, exports.BUILD_VALIDATION_ROOT)
            return {**expected, "profile": "changed"} if changed_export else expected
        with ExitStack() as stack:
            stack.enter_context(patch.object(exports, "authenticate_privileged_host_boundary"))
            stack.enter_context(patch.object(exports, "authenticate_worker_account", side_effect=[self.candidate, self.validator]))
            stack.enter_context(patch.object(exports, "_open_directory", side_effect=[10, 11, 12, 13, 14]))
            stack.enter_context(patch.object(exports.os, "fstat", side_effect=info))
            stack.enter_context(patch.object(exports.os, "close"))
            modes = stack.enter_context(patch.object(exports.os, "fchmod", create=True))
            stack.enter_context(patch.object(exports.os, "fsync"))
            stack.enter_context(patch.object(exports, "terminate_worker", side_effect=terminate))
            stack.enter_context(patch.object(exports, "verify_source_copy", side_effect=source))
            stack.enter_context(patch.object(exports, "authenticate_tree_private_access", side_effect=private))
            stack.enter_context(patch.object(exports, "materialize_build_export", side_effect=copying))
            stack.enter_context(patch.object(exports, "privatize_tree_copy", side_effect=transfer))
            stack.enter_context(patch.object(exports, "verify_build_export", side_effect=verify))
            try:
                result = exports.freeze_build_export(boundary=self.boundary, candidate=self.candidate,
                    execution=WorkerResult(0, b"native log", False), inventory=(), generated_roots=("build",), plan=ci_plan())
                self.assertEqual(result, expected)
                self.assertEqual(events, ["terminate", "source", "private-original", "copy", "source",
                                          "transfer", "private-copy", "verify"])
                modes.assert_not_called()
            except MbError:
                if "transfer" in events:
                    modes.assert_called_once_with(14, 0o700)
                else:
                    modes.assert_not_called()
                if source_error or kill_error or private_error:
                    self.assertNotIn("copy", events)
                if changed_source:
                    self.assertIn("copy", events)
                    self.assertNotIn("transfer", events)
                raise

    def test_quiescence_source_checks_and_private_independent_copy_are_ordered(self):
        self.freeze()

    def test_source_survivor_permissions_and_copy_transfer_failures_refuse_receipt(self):
        for args in ({"source_error": MbError("tracked mutation")}, {"changed_source": True},
                     {"kill_error": WorkerError("survivor")}, {"private_error": MbError("ACL")},
                     {"copy_owner": 2000}, {"transfer_error": OSError("ownership")},
                     {"final_inode": 31}, {"changed_export": True}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.freeze(**args)

    def test_unsuccessful_execution_and_unprotected_root_types_reject_before_account_mutation(self):
        for execution, roots in [(WorkerResult(1, b"failed", False), ()),
                                 (WorkerResult(False, b"bool", False), ()),
                                 (WorkerResult(None, b"cancelled", False), ()),
                                 (WorkerResult(0, "wrong", False), ()),
                                 (WorkerResult(0, b"", False), ["build"]),
                                 (WorkerResult(0, b"", False), (".git",)),
                                 (WorkerResult(0, b"", False), ("../build",))]:
            with self.subTest(execution=execution, roots=roots), \
                    patch.object(exports, "authenticate_privileged_host_boundary"), \
                    patch.object(exports, "authenticate_worker_account") as account, \
                    patch.object(exports, "terminate_worker") as terminate, self.assertRaises(MbError):
                exports.freeze_build_export(boundary=self.boundary, candidate=self.candidate, execution=execution,
                                            inventory=(), generated_roots=roots, plan=ci_plan())
            account.assert_not_called()
            terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
