"""Complete target-union and frozen-inventory admission, independent of native witnesses."""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import ExitStack

from mod_base.build_ci import exports
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.worker import WorkerAccount, WorkerError
from mod_base.build_ci.exports import materialize_build_export, validate_target_partitions, verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.records import validate_build_envelope
from mod_base.errors import MbError
from mod_base.io.tree import EXPORT_PATHS
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_descriptor, ci_envelope, ci_plan, ci_run_descriptor, ci_run_producer, ci_staged_plan


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
        # Every role has a bound of its own, so the witness is three lanes of full-size JARs per
        # target (1.5 GiB): each partition separately fits 2 GiB and their coherent union exceeds it.
        plan = ci_plan()
        target, lane = plan["targets"][0], plan["lanes"][0]
        plan["targets"], plan["lanes"] = [], []
        for name in ("target-a", "target-b"):
            lanes = [f"{name}-lane-{index}" for index in range(3)]
            plan["lanes"] += [{**copy.deepcopy(lane), "id": item, "target_id": name} for item in lanes]
            plan["targets"].append({**copy.deepcopy(target), "id": name, "outputs": [
                *({"path": f"{item}/{role}.jar", "lane_id": item, "role": role}
                  for item in lanes for role in ("production", "harness")),
                {"path": f"{name}/artifacts.json", "lane_id": None, "role": "native-report"}]})
        plan["plan_sha256"] = plan_sha256(plan)
        partitions = []
        for index, target in enumerate(plan["targets"]):
            envelope = ci_envelope(plan, target_id=target["id"])
            for file in envelope["files"]:
                file["size"] = 128 if file["role"] == "native-report" else limits.MAX_CI_JAR_BYTES
            self.assertLess(sum(file["size"] for file in envelope["files"]), limits.MAX_CI_EXPORT_TREE_BYTES)
            partitions.append({"envelope": envelope, "descriptor": ci_run_descriptor(
                plan, "build", None, "target", unit_id=target["id"], artifact_id=100 + index)})
        with self.assertRaisesRegex(MbError, "whole-tree"):
            validate_target_partitions(partitions, plan=plan)
        # Within the budget the same two partitions are one complete union.
        for file in partitions[1]["envelope"]["files"]:
            file["size"] = 128
        self.assertEqual(len(validate_target_partitions(partitions, plan=plan)), 14)

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
                terminate_error=None, grant_error=None, changed_envelope=False, alone=False):
        def metadata(inode, owner, group, mode):
            return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group,
                                   st_mode=stat.S_IFDIR | mode)
        info = [metadata(20, parent_owner, 121, 0o711), metadata(21, 1001, 121, 0o711),
                metadata(30, initial_owner, 121, 0o700), metadata(final_inode, 1001, 2001, final_mode)]
        events = []
        expected = ci_envelope()
        def verify(*args, **kwargs):
            events.append("verify")
            return {**expected, "profile": "changed"} if changed_envelope and "grant" in events else expected
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
            stack.enter_context(patch.object(exports, "authenticate_worker_account", return_value=self.validator))
            stack.enter_context(patch.object(exports, "authenticate_peer_account",
                                             return_value=None if alone else self.candidate))
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
                self.assertEqual(events, [*([] if alone else ["terminate"]), "verify", "grant", "verify"])
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

    def test_a_job_with_the_validator_alone_hands_over_without_a_candidate_to_stop(self):
        self.handoff(alone=True)
        for args in ({"initial_owner": 2000}, {"final_mode": 0o777}, {"changed_envelope": True}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.handoff(alone=True, **args)

    def test_foreign_layout_owner_kill_grant_identity_and_envelope_failures_cannot_succeed(self):
        for args in ({"parent_owner": 2000}, {"initial_owner": 2000},
                     {"terminate_error": WorkerError("survivor")}, {"grant_error": OSError("chmod failed")},
                     {"final_inode": 31}, {"final_mode": 0o777}, {"changed_envelope": True}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.handoff(**args)

    def test_forged_validator_and_runner_collisions_reject_before_mutation(self):
        from mod_base.build_ci import worker
        for requested, actual, candidate in [(self.candidate, self.validator, self.candidate),
                                             (self.validator, self.candidate, self.candidate),
                                             (self.validator, self.validator,
                                              WorkerAccount("candidate", 1001, 2000, "home")),
                                             (self.validator, self.validator,
                                              WorkerAccount("candidate", 2000, 2001, "home"))]:
            with self.subTest(requested=requested), \
                    patch.object(exports, "authenticate_privileged_host_boundary"), \
                    patch.object(exports, "authenticate_worker_account", return_value=actual), \
                    patch.object(worker, "worker_account_exists", return_value=True), \
                    patch.object(worker, "authenticate_worker_account", return_value=candidate), \
                    patch.object(exports, "_open_directory") as opening, self.assertRaises(MbError):
                exports.prepare_build_validation(boundary=self.boundary, validator=requested, plan=ci_plan())
            opening.assert_not_called()


class TargetSealTests(unittest.TestCase):
    """The envelope protected code writes for one target, from the plan and a real directory.

    The directory is what a target's hook left: the mod's own files and no kit document. The
    account, the ownership handover and the source check around it need root and are exercised by
    ``LinuxCandidateCommandTests`` in ``tests/ci_linux_worker.py``.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root, self.output = self.base / "export", self.base / "sealed"
        self.plan = ci_staged_plan()
        self.producer = {key: value for key, value in ci_run_producer(self.plan).items() if key != "upload_window"}
        self.payloads = self.write("target-a")

    def write(self, target_id):
        """Write every planned output of ``target_id`` below the export root, as its hook does."""
        payloads = {}
        for output in next(target for target in self.plan["targets"] if target["id"] == target_id)["outputs"]:
            data = f"{output['role']} bytes of {output['path']}".encode()
            path = self.root.joinpath(*output["path"].split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            payloads[output["path"]] = data
        return payloads

    def seal(self, target_id="target-a", **changes):
        return exports.seal_target_export(self.root, self.output, plan=self.plan, target_id=target_id,
                                          **{"producer": self.producer, **changes})

    def published(self):
        return sorted(path.name for path in self.base.iterdir())

    def test_the_envelope_is_the_plan_of_the_target_with_the_sizes_and_hashes_of_the_bytes_found(self):
        envelope = self.seal()
        self.assertEqual(validate_build_envelope(copy.deepcopy(envelope), plan=self.plan), envelope)
        target = self.plan["targets"][0]
        self.assertEqual({key: envelope[key] for key in ("kind", "schema_version", "identity", "plan_sha256", "profile",
                                                         "producer", "scope", "target_id")},
                         {"kind": "mod-base.build.envelope", "schema_version": 1, "identity": self.plan["identity"],
                          "plan_sha256": self.plan["plan_sha256"], "profile": self.plan["profile"],
                          "producer": self.producer, "scope": "target", "target_id": "target-a"})
        self.assertEqual(envelope["files"], [
            {**output, "size": len(self.payloads[output["path"]]),
             "sha256": hashlib.sha256(self.payloads[output["path"]]).hexdigest()}
            for output in sorted(target["outputs"], key=lambda output: output["path"])])
        self.assertEqual(envelope["native_reports"], ["targets/target-a/artifacts.json"])
        # The mod's own names survive: spaces, and one file that belongs to the target as a whole.
        self.assertIn("files/Example Mod - lane-a.jar", self.payloads)
        self.assertEqual([file["lane_id"] for file in envelope["files"] if file["role"] == "sbom"], [None])
        # The sealed directory is an independent copy with the one document the kit wrote.
        self.assertEqual(sorted(path.relative_to(self.output).as_posix() for path in self.output.rglob("*")
                                if path.is_file()), sorted([*self.payloads, grammar.CI_ENVELOPE_NAME]))
        self.assertEqual((self.output / grammar.CI_ENVELOPE_NAME).read_bytes(), canonical_json(envelope))
        self.assertEqual(verify_build_export(self.output, plan=self.plan), envelope)
        for name, data in self.payloads.items():
            self.assertEqual((self.output / name).read_bytes(), data)
            self.assertNotEqual(os.stat(self.output / name).st_ino, os.stat(self.root / name).st_ino)
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertFalse((self.root / grammar.CI_ENVELOPE_NAME).exists())
        self.assertEqual(self.published(), ["export", "sealed"])

    def test_the_other_target_of_the_plan_seals_its_own_outputs(self):
        shutil.rmtree(self.root)
        payloads = self.write("target-c")
        envelope = self.seal("target-c")
        self.assertEqual((envelope["target_id"], [file["path"] for file in envelope["files"]]),
                         ("target-c", sorted(payloads)))
        self.assertEqual(verify_build_export(self.output, plan=self.plan), envelope)

    def test_every_difference_between_the_planned_outputs_and_the_files_is_refused_and_nothing_is_published(self):
        jar, manifest = "files/Example Mod - lane-a.jar", "targets/target-a/artifacts.json"

        def fifo():
            (self.root / jar).unlink()
            os.mkfifo(self.root / jar)

        def directory():
            (self.root / manifest).unlink()
            (self.root / manifest).mkdir()
            (self.root / manifest / "artifacts.json").write_bytes(b"{}")

        def linked():
            (self.root / jar).unlink()
            (self.root / jar).symlink_to(self.root / "harness/Example Mod E2E - lane-a.jar")

        mutations = {
            "a planned output is missing": (lambda: (self.root / jar).unlink(), "1 planned and missing"),
            "two planned outputs are missing": (
                lambda: [(self.root / name).unlink() for name in (jar, manifest)], "2 planned and missing"),
            "an extra file": (lambda: (self.root / "files/extra.jar").write_bytes(b"extra"),
                              r"1 not planned \(first 'files/extra.jar'\)"),
            "an extra file in a new directory": (lambda: [(self.root / "notes").mkdir(),
                                                          (self.root / "notes/readme.txt").write_bytes(b"x")],
                                                 "1 not planned"),
            "an output of another target": (lambda: self.write("target-c"), "3 not planned"),
            "a renamed output": (lambda: (self.root / jar).rename(self.root / "files/Example Mod - lane-a.zip"),
                                 "1 planned and missing .*1 not planned"),
            "an output under another case": (lambda: (self.root / manifest).rename(
                self.root / "targets/target-a/Artifacts.json"), "1 planned and missing .*1 not planned"),
            "a kit envelope written by the hook": (
                lambda: (self.root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(ci_envelope())),
                r"1 not planned \(first 'ci-envelope.json'\)"),
            "a runtime envelope written by the hook": (
                lambda: (self.root / grammar.CI_RUNTIME_ENVELOPE_NAME).write_bytes(b"{}"), "1 not planned"),
            "an empty planned output": (lambda: (self.root / manifest).write_bytes(b""), "size is outside"),
            "a planned output that is a symlink": (linked, "symlink"),
            "a planned output with a second name": (lambda: os.link(self.root / jar, self.base / "alias"),
                                                    "hard-linked"),
            "a planned output that is a pipe": (fifo, "special file"),
            "a planned output that is a directory": (directory, "planned and missing"),
            "a hidden file": (lambda: (self.root / "files/.hidden").write_bytes(b"x"), "not a canonical export path"),
            "a name outside the export grammar": (lambda: (self.root / "files/caf\xe9.jar").write_bytes(b"x"),
                                                  "not a canonical export path"),
            "a case alias of a planned directory": (lambda: (self.root / "Files").mkdir(), "case alias"),
        }
        for name, (mutate, message) in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                with self.assertRaisesRegex(MbError, message):
                    self.seal()
                self.assertFalse(self.output.exists())
                self.assertEqual(sorted(set(self.published()) - {"alias"}), ["export"])  # No stage is left behind.

    def test_a_target_outside_the_plan_a_foreign_producer_and_an_existing_output_are_refused(self):
        for changes in ({"target_id": "target-b"}, {"target_id": "lane-a"},
                        {"producer": {**self.producer, "api_head_sha": "e" * 40}},
                        {"producer": {**self.producer, "event": "push"}},
                        {"producer": {**self.producer, "workflow_path": ".github/workflows/other.yml"}},
                        {"producer": {**self.producer, "upload_window": {}}},
                        {"producer": {key: value for key, value in self.producer.items() if key != "graph_sha256"}}):
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                self.seal(**changes)
            self.assertFalse(self.output.exists())
        self.output.mkdir()
        with self.assertRaisesRegex(MbError, "refusing to replace existing output"):
            self.seal()
        self.assertEqual(os.listdir(self.output), [])

    def test_every_role_keeps_its_own_size_bound_and_the_whole_export_its_caps(self):
        for bound, value in (("MAX_CI_SBOM_BYTES", 8), ("MAX_CI_JAR_BYTES", 8), ("MAX_CI_EXPORT_FILE_BYTES", 8),
                             ("MAX_CI_EXPORT_FILES", 5), ("MAX_CI_EXPORT_TREE_BYTES", 64), ("MAX_CI_EXPORT_ENTRIES", 4),
                             ("MAX_CI_ENVELOPE_BYTES", 64)):
            with self.subTest(bound=bound), patch.object(limits, bound, value), self.assertRaises(MbError):
                self.seal()
            self.assertFalse(self.output.exists())
        with patch.dict(limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE, {self.plan["profile"]: 8}), \
                self.assertRaisesRegex(MbError, "role budget"):
            self.seal()
        self.assertEqual(self.seal()["target_id"], "target-a")

    def test_the_envelope_is_a_function_of_the_inventory_alone(self):
        records = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                   for path, data in sorted(self.payloads.items())]
        envelope = exports.target_envelope(records, plan=self.plan, target_id="target-a", producer=self.producer)
        self.assertEqual(envelope, self.seal())
        self.assertEqual(exports.target_envelope(records[::-1], plan=self.plan, target_id="target-a",
                                                 producer=self.producer), envelope)
        self.assertIsNot(envelope["identity"], self.plan["identity"])
        with self.assertRaisesRegex(MbError, "3 planned and missing .*6 not planned"):
            exports.target_envelope(records, plan=self.plan, target_id="target-c", producer=self.producer)
        with self.assertRaisesRegex(MbError, "6 planned and missing"):
            exports.target_envelope([], plan=self.plan, target_id="target-a", producer=self.producer)

    def test_root_only_steps_refuse_an_unprivileged_caller_before_they_look_at_anything(self):
        if os.geteuid() == 0:
            self.skipTest("this case needs an unprivileged user")
        boundary = HostBoundary("/home/runner", os.getuid(), os.getgid(), 1, 10, 0o755)
        candidate = WorkerAccount("candidate", 2000, 2000, "/tmp/mod-base-sandbox-boundary/mod-base-worker/candidate-home")
        calls = (lambda: exports.freeze_build_export(boundary=boundary, candidate=candidate, inventory=(),
                                                     generated_roots=(), plan=self.plan, target_id="target-a",
                                                     producer=self.producer),
                 lambda: exports.verify_candidate_source(boundary=boundary, candidate=candidate, inventory=(),
                                                         generated_roots=()),
                 lambda: exports.stage_build_bundle(boundary=boundary, candidate=candidate, plan=self.plan,
                                                    envelope=ci_envelope(self.plan), path="build/release"))
        for call in calls:
            with patch.object(exports, "authenticate_worker_account") as account, \
                    patch.object(exports, "terminate_worker") as terminate, \
                    self.assertRaisesRegex(MbError, "requires protected root setup"):
                call()
            account.assert_not_called()
            terminate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
