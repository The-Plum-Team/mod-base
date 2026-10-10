"""What a verification leaves and what a job uploads, on real directories and real ZIPs.

Nothing here needs an account: the reports a hook wrote are a directory, sealing them is a copy
with a record built beside it, and the upload directory is a copy of a sealed export with that
verification beside its envelope. The synthetic mod's hooks run as plain processes
(``tests/ci_mod_harness.py``); only the GitHub API is a fake. The account side of the same path
is ``tests/ci_linux_worker.py``.
"""

from __future__ import annotations

import copy
import hashlib
import io
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import adapter, runtime_inputs, transport, validation
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests import ci_mod_harness as h
from tests.helpers import (ci_envelope, ci_run_descriptor, ci_run_producer, ci_runtime_envelope, ci_staged_plan)
from tests.test_ci_transport import World, target_set_world

CONFIG_SHA256 = hashlib.sha256(b"protected Build config").hexdigest()
RECORD, ENVELOPE, LANE_ENVELOPE = (grammar.CI_VALIDATION_NAME, grammar.CI_ENVELOPE_NAME,
                                   grammar.CI_RUNTIME_ENVELOPE_NAME)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write(root: Path, files: dict[str, bytes]) -> None:
    """Write ``files`` (relative path -> bytes) below ``root`` the way a hook with umask 077 does."""

    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, data in files.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)


def tree(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


def report(unit_id: str, **fields: object) -> bytes:
    """A native report as a verification hook writes it: its content is the mod's own."""

    return canonical_json({"schema_version": 1, "unit": unit_id, **fields})


def seal_build(root: Path, plan: dict, *, target_id: str | None = None, attempt: int = 2) -> dict:
    """A sealed Build export in ``root``: every planned file of the target (or of all targets) with
    bytes of its own, and the canonical envelope that names them. Returns the envelope."""

    envelope = ci_envelope(plan, target_id=target_id)
    envelope["producer"]["run_attempt"] = attempt
    contents = {}
    for file in envelope["files"]:
        data = f"{file['role']} bytes of {file['path']}\n".encode()
        file.update(size=len(data), sha256=digest(data))
        contents[file["path"]] = data
    write(root, {**contents, ENVELOPE: canonical_json(envelope)})
    return envelope


def seal_lane(root: Path, plan: dict, lane_id: str, *, extra: dict[str, tuple[str, bytes]] | None = None) -> tuple[dict, dict]:
    """A sealed runtime lane in ``root``: a report, an empty log, a screenshot and the canonical
    runtime envelope, owned by the complete Build of the Build run. Returns ``(Build envelope,
    runtime envelope)``."""

    lane = next(lane for lane in plan["lanes"] if lane["id"] == lane_id)
    contents = {f"lanes/{lane_id}/result.json": ("native-report", report(lane_id, checks=3)),
                f"lanes/{lane_id}/logs/client.log": ("runtime-log", b""),
                f"lanes/{lane_id}/screenshots/title screen.png": ("screenshot", b"\x89PNG synthetic pixels"),
                **(extra or {})}
    envelope = ci_runtime_envelope()
    envelope.update(
        identity=copy.deepcopy(plan["identity"]), plan_sha256=plan["plan_sha256"], profile=plan["profile"],
        producer={key: value for key, value in ci_run_producer(plan, "packaged").items() if key != "upload_window"},
        scope="lane", lane_id=lane_id, owning_build=ci_run_descriptor(plan, "build", "full", "build"),
        lanes=[{"id": lane_id, "native_contract_sha256": lane["native_contract_sha256"]}],
        files=[{"path": name, "lane_id": lane_id, "role": role, "size": len(data), "sha256": digest(data)}
               for name, (role, data) in sorted(contents.items())])
    write(root, {**{name: data for name, (_, data) in contents.items()}, LANE_ENVELOPE: canonical_json(envelope)})
    return ci_envelope(plan), envelope


def upload_archive(directory: Path) -> bytes:
    """The artifact ``actions/upload-artifact`` makes of ``directory`` (``include-hidden-files: true``,
    ``compression-level: 6``): one deflated entry per file under its path relative to the
    directory, and an entry per directory."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(directory.rglob("*")):
            name = path.relative_to(directory).as_posix()
            if path.is_dir():
                entry = zipfile.ZipInfo(name + "/", date_time=(2026, 10, 7, 10, 1, 30))
                entry.create_system, entry.external_attr = 3, ((stat.S_IFDIR | 0o755) << 16) | 0x10
                archive.writestr(entry, b"", compress_type=zipfile.ZIP_STORED)
            else:
                entry = zipfile.ZipInfo(name, date_time=(2026, 10, 7, 10, 1, 30))
                entry.create_system, entry.external_attr = 3, (stat.S_IFREG | 0o644) << 16
                archive.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=6)
    return stream.getvalue()


def detach(archive: bytes) -> tuple[bytes, dict[str, bytes]]:
    """What a reader of the artifact must do before it hands the tree to an export verifier.

    ``transport`` verifies an extracted artifact as exactly one export (``verify_build_export``,
    ``verify_runtime_export``) and does not know the verification beside the envelope yet. So this
    takes ``ci-validation.json`` and every report it names (``reports[].path``, relative to the
    root) out of the archive. Returns the archive of the export alone and the detached files.
    """

    detached = {}
    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(stream, "w") as export:
        record = load_document(source.read(RECORD), kind="mod-base.ci.validation")
        names = {RECORD, *(item["path"] for item in record["reports"])}
        for entry in source.infolist():
            data = b"" if entry.is_dir() else source.read(entry)
            if entry.filename in names:
                detached[entry.filename] = data
            else:
                export.writestr(entry, data, compress_type=entry.compress_type)
    return stream.getvalue(), detached


class Case(unittest.TestCase):
    """A temporary directory and one verification: its plan, its context and its reports."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.plan = ci_staged_plan()
        self.reports = self.temporary / "validation"
        self.sealed = self.temporary / "sealed-validation"

    def context(self, hook: str = "verify_build", unit_id: str | None = None, **changes: object) -> dict:
        return {"plan": self.plan, "hook": hook, "unit_id": unit_id, "run_id": 42, "run_attempt": 2,
                "source_config_sha256": CONFIG_SHA256, "input_sha256": digest(b"the inputs the hook was given"),
                **changes}

    def units(self, hook: str, unit_id: str | None) -> list[dict]:
        units = self.plan["lanes" if hook == "verify_runtime" else "targets"]
        return [unit for unit in units if unit_id in (None, unit["id"])]

    def leave(self, hook: str = "verify_build", unit_id: str | None = None) -> dict[str, bytes]:
        """Write what the hook leaves for its units; return the reports by file name."""

        files = {unit["id"] + ".json": report(unit["id"], hook=hook) for unit in self.units(hook, unit_id)}
        write(self.reports, files)
        return files

    def expected(self, files: dict[str, bytes], **context: object) -> dict:
        context = self.context(**context)
        plan = context.pop("plan")
        units = self.units(context["hook"], context["unit_id"])
        return {"kind": "mod-base.ci.validation", "schema_version": 1, "identity": plan["identity"],
                "plan_sha256": plan["plan_sha256"], "profile": plan["profile"], **context,
                "reports": [{"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
                             "path": unit["id"] + ".json", "size": len(files[unit["id"] + ".json"]),
                             "sha256": digest(files[unit["id"] + ".json"])} for unit in units]}


class RecordTests(Case):
    """``build_validation_record``: protected context plus the reports found, and nothing else."""

    def test_the_record_is_the_plan_the_context_and_the_reports_found(self) -> None:
        for hook, unit_id, names in (("verify_build", None, ["target-a.json", "target-c.json"]),
                                     ("verify_target", "target-c", ["target-c.json"]),
                                     ("verify_runtime", "lane-b", ["lane-b.json"])):
            with self.subTest(hook=hook):
                self.reports = self.temporary / hook
                files = self.leave(hook, unit_id)
                self.assertEqual(sorted(files), names)
                record = validation.build_validation_record(self.reports, **self.context(hook, unit_id))
                self.assertEqual(record, self.expected(files, hook=hook, unit_id=unit_id))
                self.assertEqual(load_document(canonical_json(record), kind=record["kind"], plan=self.plan), record)
                self.assertEqual(tree(self.reports), files)  # Reading builds nothing in the hook's directory.

    def test_every_field_but_the_report_bytes_comes_from_the_protected_context(self) -> None:
        # A report that claims another identity, hook, unit or contract changes nothing but its own hash.
        claims = canonical_json({"hook": "verify_build", "unit": "target-c", "plan_sha256": "0" * 64,
                                 "native_contract_sha256": "f" * 64, "run_id": 7, "passed": True})
        write(self.reports, {"target-a.json": claims})
        record = validation.build_validation_record(self.reports, **self.context("verify_target", "target-a"))
        self.assertEqual(record, self.expected({"target-a.json": claims}, hook="verify_target", unit_id="target-a"))
        self.assertEqual(record["reports"][0]["native_contract_sha256"], self.plan["targets"][0]["native_contract_sha256"])

    def test_a_missing_or_an_extra_entry_is_a_rejection(self) -> None:
        mutations = {
            "no report": lambda root: (root / "target-a.json").unlink(),
            "report of one target only": lambda root: (root / "target-c.json").unlink(),
            "another report": lambda root: write(root, {"target-b.json": report("target-b")}),
            "another file": lambda root: write(root, {"unplanned.txt": b"not a report\n"}),
            "a kit document the hook must never write": lambda root: write(root, {RECORD: canonical_json({})}),
            "a derivation output": lambda root: write(root, {adapter.PLAN_OUTPUT: b"{}\n"}),
            "an empty directory": lambda root: (root / "logs").mkdir(),
            "a report in a directory": lambda root: ((root / "reports").mkdir(),
                                                    (root / "target-a.json").rename(root / "reports/target-a.json")),
            "a report that is a directory": lambda root: ((root / "target-a.json").unlink(),
                                                         (root / "target-a.json").mkdir()),
            "another case": lambda root: (root / "target-a.json").rename(root / "Target-A.json"),
        }
        for label, mutate in mutations.items():
            with self.subTest(case=label):
                self.reports = self.temporary / label.replace(" ", "-")
                self.leave()
                mutate(self.reports)
                with self.assertRaises(MbError):
                    validation.build_validation_record(self.reports, **self.context())
        self.reports = self.temporary / "empty"
        self.reports.mkdir()
        for hook, unit_id in (("verify_build", None), ("verify_target", "target-a"), ("verify_runtime", "lane-a")):
            with self.subTest(empty=hook), self.assertRaisesRegex(MbError, "exactly one `<unit id>.json` report"):
                validation.build_validation_record(self.reports, **self.context(hook, unit_id))
        with self.assertRaises(MbError):
            validation.build_validation_record(self.temporary / "absent", **self.context())

    def test_a_report_that_is_not_one_canonical_json_object_is_a_rejection(self) -> None:
        bad = {"not JSON": b"synthetic verify_target: ok\n", "empty": b"", "a list": b"[]\n", "a string": b'"ok"\n',
               "spaced": b'{"unit": "target-a"}\n', "unsorted": b'{"b":1,"a":2}\n', "no final newline": b'{"a":1}',
               "duplicate key": b'{"a":1,"a":2}\n', "not a number": b'{"a":NaN}\n', "a byte order mark": b'\xef\xbb\xbf{}\n',
               "not UTF-8": b'{"a":"\xff"}\n'}
        for label, data in bad.items():
            with self.subTest(case=label):
                self.reports = self.temporary / label.replace(" ", "-")
                write(self.reports, {"target-a.json": data})
                with self.assertRaises(MbError):
                    validation.build_validation_record(self.reports, **self.context("verify_target", "target-a"))

    def test_a_report_is_at_most_the_report_bound(self) -> None:
        padding = limits.MAX_CI_REPORT_BYTES - len(report("target-a", padding=""))
        largest = report("target-a", padding="x" * padding)
        self.assertEqual(len(largest), limits.MAX_CI_REPORT_BYTES)
        write(self.reports, {"target-a.json": largest})
        record = validation.build_validation_record(self.reports, **self.context("verify_target", "target-a"))
        self.assertEqual(record["reports"][0]["size"], limits.MAX_CI_REPORT_BYTES)
        (self.reports / "target-a.json").write_bytes(report("target-a", padding="x" * (padding + 1)))
        with self.assertRaisesRegex(MbError, "size is outside"):
            validation.build_validation_record(self.reports, **self.context("verify_target", "target-a"))

    def test_a_link_or_a_special_file_is_no_report(self) -> None:
        elsewhere = self.temporary / "elsewhere.json"
        elsewhere.write_bytes(report("target-a"))
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                self.reports = self.temporary / kind
                self.reports.mkdir()
                leaf = self.reports / "target-a.json"
                if kind == "symlink":
                    leaf.symlink_to(elsewhere)
                elif kind == "hardlink":
                    os.link(elsewhere, leaf)
                else:
                    os.mkfifo(leaf)
                with self.assertRaises(MbError):
                    validation.build_validation_record(self.reports, **self.context("verify_target", "target-a"))
        link = self.temporary / "linked-directory"
        self.reports = self.temporary / "real"
        self.leave("verify_target", "target-a")
        link.symlink_to(self.reports)
        with self.assertRaises(MbError):
            validation.build_validation_record(link, **self.context("verify_target", "target-a"))

    def test_the_unit_and_the_hook_are_the_plans_and_the_contracts(self) -> None:
        self.leave("verify_target", "target-a")
        wrong = [("verify_target", "target-b"), ("verify_target", "lane-a"), ("verify_target", None),
                 ("verify_target", "target-c"),  # Another unit of the plan: its report is not the one found.
                 ("verify_runtime", "target-a"), ("verify_runtime", None), ("verify_build", "target-a"),
                 ("verify_build", None),  # Every target, and one report was left.
                 ("derive_plan", None), ("derive_runtime", "lane-a"), ("build_target", "target-a"), ("verify", None),
                 ("verify_target", "Target-A"), ("verify_target", "target-a/../target-a"), ("verify_target", 7)]
        for hook, unit_id in wrong:
            with self.subTest(hook=hook, unit_id=unit_id), self.assertRaises(MbError):
                validation.build_validation_record(self.reports, **self.context(hook, unit_id))

    def test_a_context_that_is_not_a_run_or_a_digest_is_a_rejection_before_anything_is_read(self) -> None:
        for changes in ({"run_id": 0}, {"run_id": True}, {"run_id": "42"}, {"run_attempt": 0},
                        {"run_attempt": limits.MAX_RUN_ATTEMPT + 1}, {"source_config_sha256": "A" * 64},
                        {"input_sha256": "a" * 63}, {"input_sha256": None}, {"plan": {**self.plan, "extra": 1}}):
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                validation.build_validation_record(self.temporary / "never-opened",
                                                   **self.context("verify_target", "target-a", **changes))

    def test_the_synthetic_mods_verifiers_leave_exactly_what_the_record_is_built_from(self) -> None:
        sandbox = h.Sandbox(self.temporary / "mod")
        plan = sandbox.derive_plan()
        for target_id in h.TARGETS:
            sandbox.build(target_id)
        cases = [("verify_build", None, list(h.TARGETS)), ("verify_target", h.TARGETS[1], [h.TARGETS[1]])]
        for hook, unit_id, units in cases:
            with self.subTest(hook=hook):
                process = sandbox.run(hook, unit_id=unit_id)
                self.assertEqual(process.returncode, 0, process.stdout)
                files = tree(sandbox.validation)
                self.assertEqual(sorted(files), sorted(unit + ".json" for unit in units))
                record = validation.build_validation_record(
                    sandbox.validation, plan=plan, hook=hook, unit_id=unit_id, run_id=42, run_attempt=1,
                    source_config_sha256=sandbox.config.sha256, input_sha256="b" * 64)
                self.assertEqual(load_document(canonical_json(record), kind=record["kind"], plan=plan), record)
                self.assertEqual([(item["unit_id"], item["path"], item["sha256"]) for item in record["reports"]],
                                 [(unit, unit + ".json", digest(files[unit + ".json"])) for unit in units])
                self.assertEqual((record["hook"], record["unit_id"], record["run_attempt"],
                                  record["source_config_sha256"]), (hook, unit_id, 1, sandbox.config.sha256))
                for path in sandbox.validation.iterdir():
                    path.unlink()
                sandbox.validation.rmdir()

    def test_a_synthetic_verifier_that_drops_or_adds_a_file_gives_no_record(self) -> None:
        for mode in ("missing", "extra"):
            with self.subTest(mode=mode):
                mod = h.materialize(self.temporary / f"mod-{mode}",
                                    faults=[{"hook": "verify_build", "unit": None, "mode": mode}])
                sandbox = h.Sandbox(self.temporary / f"sandbox-{mode}", protected=mod)
                plan = sandbox.derive_plan()
                for target_id in h.TARGETS:
                    sandbox.build(target_id)
                self.assertEqual(sandbox.run("verify_build").returncode, 0)
                with self.assertRaisesRegex(MbError, "exactly one `<unit id>.json` report"):
                    validation.build_validation_record(
                        sandbox.validation, plan=plan, hook="verify_build", unit_id=None, run_id=42, run_attempt=1,
                        source_config_sha256=sandbox.config.sha256, input_sha256="b" * 64)


class SealTests(Case):
    """``materialize_validation_export``: the private copy of the reports with the record beside it."""

    def test_the_sealed_directory_is_the_reports_and_the_record_built_from_them(self) -> None:
        for hook, unit_id in (("verify_build", None), ("verify_target", "target-a"), ("verify_runtime", "lane-c")):
            with self.subTest(hook=hook):
                self.reports, self.sealed = self.temporary / f"{hook}-left", self.temporary / f"{hook}-sealed"
                files = self.leave(hook, unit_id)
                context = self.context(hook, unit_id)
                record = validation.materialize_validation_export(self.reports, self.sealed, **context)
                self.assertEqual(record, self.expected(files, hook=hook, unit_id=unit_id))
                self.assertEqual(tree(self.sealed), {**files, RECORD: canonical_json(record)})
                self.assertEqual(stat.S_IMODE(self.sealed.stat().st_mode), 0o700)
                self.assertEqual(validation.verify_validation_export(self.sealed, **context), record)
                for name in files:  # An independent copy: the hook's own files are not what is sealed.
                    self.assertNotEqual((self.reports / name).stat().st_ino, (self.sealed / name).stat().st_ino)
                    self.assertEqual((self.sealed / name).stat().st_nlink, 1)
                    (self.reports / name).write_bytes(b"changed after the seal")
                self.assertEqual(validation.verify_validation_export(self.sealed, **context), record)
                self.assertEqual(tree(self.reports), dict.fromkeys(files, b"changed after the seal"))

    def test_an_existing_sealed_directory_is_never_replaced(self) -> None:
        files = self.leave()
        record = validation.materialize_validation_export(self.reports, self.sealed, **self.context())
        before = tree(self.sealed)
        (self.reports / "target-a.json").write_bytes(report("target-a", second=True))
        with self.assertRaises(MbError):
            validation.materialize_validation_export(self.reports, self.sealed, **self.context())
        self.assertEqual(tree(self.sealed), before)
        self.assertEqual(before[RECORD], canonical_json(record))
        self.assertEqual(sorted(before), sorted([*files, RECORD]))

    def test_a_record_the_hook_wrote_is_an_extra_file_and_never_the_sealed_one(self) -> None:
        files = self.leave("verify_target", "target-a")
        forged = self.expected(files, hook="verify_target", unit_id="target-a")
        write(self.reports, {RECORD: canonical_json(forged)})  # Even a perfectly valid one.
        with self.assertRaisesRegex(MbError, "exactly one `<unit id>.json` report"):
            validation.materialize_validation_export(self.reports, self.sealed,
                                                     **self.context("verify_target", "target-a"))
        self.assertEqual(sorted(path.name for path in self.temporary.iterdir()), ["validation"])

    def test_nothing_is_published_for_reports_that_cannot_be_sealed(self) -> None:
        cases = {"missing": lambda root: (root / "target-c.json").unlink(),
                 "extra": lambda root: write(root, {"unplanned.txt": b"x"}),
                 "not JSON": lambda root: (root / "target-a.json").write_bytes(b"ok\n"),
                 "oversized": lambda root: (root / "target-a.json").write_bytes(
                     report("target-a", padding="x" * limits.MAX_CI_REPORT_BYTES)),
                 "linked": lambda root: os.link(root / "target-a.json", self.temporary / "second-name")}
        for label, mutate in cases.items():
            with self.subTest(case=label):
                self.reports = self.temporary / f"left-{label.replace(' ', '-')}"
                self.leave()
                mutate(self.reports)
                with self.assertRaises(MbError):
                    validation.materialize_validation_export(self.reports, self.sealed, **self.context())
                self.assertFalse(self.sealed.exists())
                self.assertEqual([path.name for path in self.temporary.iterdir() if path.name.startswith(".")], [])
        for wrong in ({"hook": "derive_plan"}, {"unit_id": "target-a"}, {"run_id": 0}):
            with self.subTest(wrong=wrong), self.assertRaises(MbError):
                validation.materialize_validation_export(self.reports, self.sealed, **self.context(**wrong))
            self.assertFalse(self.sealed.exists())

    def test_a_copy_that_changes_before_it_is_sealed_leaves_no_output_and_no_stage(self) -> None:
        self.leave()
        original = validation.copy_regular_files

        def changing(root, stage_fd, **bounds):
            records = original(root, stage_fd, **bounds)
            descriptor = os.open("target-a.json", os.O_WRONLY | os.O_TRUNC, dir_fd=stage_fd)
            try:
                os.write(descriptor, report("target-a", changed=True))
            finally:
                os.close(descriptor)
            return records

        with patch.object(validation, "copy_regular_files", side_effect=changing), self.assertRaises(MbError):
            validation.materialize_validation_export(self.reports, self.sealed, **self.context())
        self.assertEqual(sorted(path.name for path in self.temporary.iterdir()), ["validation"])

    def test_a_sealed_verification_answers_only_for_its_own_context_and_bytes(self) -> None:
        self.leave("verify_target", "target-a")
        context = self.context("verify_target", "target-a")
        record = validation.materialize_validation_export(self.reports, self.sealed, **context)
        for changes in ({"run_id": 43}, {"run_attempt": 3}, {"source_config_sha256": "a" * 64},
                        {"input_sha256": "b" * 64}, {"hook": "verify_build", "unit_id": None},
                        {"unit_id": "target-c"}, {"hook": "verify_runtime", "unit_id": "lane-a"}):
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                validation.verify_validation_export(self.sealed, **{**context, **changes})
        other = copy.deepcopy(self.plan)
        other["identity"]["tested_sha"] = "9" * 40
        other["plan_sha256"] = plan_sha256(other)
        with self.assertRaises(MbError):
            validation.verify_validation_export(self.sealed, **{**context, "plan": other})
        leaf = self.sealed / "target-a.json"
        original = leaf.read_bytes()
        for label, mutate in {"report byte": lambda: leaf.write_bytes(original.replace(b"1", b"2", 1)),
                              "extra file": lambda: (self.sealed / "extra.json").write_bytes(b"{}\n"),
                              "record byte": lambda: (self.sealed / RECORD).write_bytes(
                                  canonical_json({**record, "run_attempt": 3}))}.items():
            mutate()
            with self.subTest(case=label), self.assertRaises(MbError):
                validation.verify_validation_export(self.sealed, **context)
            leaf.write_bytes(original)
            (self.sealed / "extra.json").unlink(missing_ok=True)
            (self.sealed / RECORD).write_bytes(canonical_json(record))
        self.assertEqual(validation.verify_validation_export(self.sealed, **context), record)


class UploadCase(Case):
    """One verified export: a sealed export, its sealed verification and where the upload goes."""

    def setUp(self) -> None:
        super().setUp()
        self.export = self.temporary / "sealed-export"
        self.upload = self.temporary / "mb-upload"

    def within(self, name: str) -> Path:
        """Give the next verified export directories of its own below ``name``."""

        base = self.temporary / name
        self.export, self.reports = base / "sealed-export", base / "validation"
        self.sealed, self.upload = base / "sealed-validation", base / "mb-upload"
        return base

    def seal_verification(self, hook: str, unit_id: str | None, **context: object) -> dict:
        """Leave the reports of one verification and seal them for ``context``; return the context."""

        self.leave(hook, unit_id)
        context = self.context(hook, unit_id, **context)
        self.record = validation.materialize_validation_export(self.reports, self.sealed, **context)
        return context

    def verified(self, hook: str, unit_id: str | None, *, plan: dict | None = None, **lane: object) -> dict:
        """Seal an export of the plan and its verification; return the arguments of the upload."""

        self.plan = self.plan if plan is None else plan
        if hook == "verify_runtime":
            build, envelope = seal_lane(self.export, self.plan, unit_id, **lane)
            run_id = 43
            inputs = runtime_inputs._context(self.plan, build, envelope, lane_id=unit_id, run_id=run_id,
                                             run_attempt=2)[0]
        else:
            envelope = seal_build(self.export, self.plan, target_id=unit_id)
            run_id, inputs = 42, digest(canonical_json(envelope))
        return {**self.seal_verification(hook, unit_id, run_id=run_id, input_sha256=inputs), "envelope": envelope}

    def write_upload(self, arguments: dict, **changes: object) -> dict:
        return validation.materialize_validated_export(self.export, self.sealed, self.upload,
                                                       **{**arguments, **changes})

    def assert_nothing_written(self) -> None:
        self.assertFalse(self.upload.exists())
        self.assertEqual(sorted(path.name for path in self.temporary.iterdir()),
                         ["sealed-export", "sealed-validation", "validation"])


class UploadTests(UploadCase):
    """``materialize_validated_export``: the export root with its verification beside the envelope."""

    def test_the_upload_is_the_export_its_envelope_the_record_and_the_reports_in_one_root(self) -> None:
        layouts = {
            ("verify_target", "target-c"): {
                ENVELOPE, RECORD, "target-c.json", "files/Example Mod - lane-c.jar",
                "harness/Example Mod E2E - lane-c.jar", "targets/target-c/artifacts.json"},
            ("verify_build", None): {
                ENVELOPE, RECORD, "target-a.json", "target-c.json",
                *(f"files/Example Mod - {lane}.jar" for lane in ("lane-a", "lane-b", "lane-c")),
                *(f"harness/Example Mod E2E - {lane}.jar" for lane in ("lane-a", "lane-b", "lane-c")),
                "targets/target-a/artifacts.json", "targets/target-a/sbom/example.cdx.json",
                "targets/target-c/artifacts.json"},
            ("verify_runtime", "lane-b"): {
                LANE_ENVELOPE, RECORD, "lane-b.json", "lanes/lane-b/result.json", "lanes/lane-b/logs/client.log",
                "lanes/lane-b/screenshots/title screen.png"},
        }
        for (hook, unit_id), layout in layouts.items():
            with self.subTest(hook=hook):
                base = self.within(hook)
                arguments = self.verified(hook, unit_id)
                self.assertEqual(self.write_upload(arguments), self.record)
                uploaded = tree(self.upload)
                self.assertEqual(set(uploaded), layout)
                # Byte for byte the two sealed directories, and nothing of either is renamed.
                self.assertEqual(uploaded, {**tree(self.export), **tree(self.sealed)})
                self.assertEqual(uploaded[RECORD], canonical_json(self.record))
                name = LANE_ENVELOPE if hook == "verify_runtime" else ENVELOPE
                self.assertEqual(uploaded[name], canonical_json(arguments["envelope"]))
                self.assertEqual(stat.S_IMODE(self.upload.stat().st_mode), 0o700)
                for path in self.upload.rglob("*"):
                    self.assertFalse(path.is_symlink())
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o644, path)
                    if path.is_file():
                        self.assertEqual(path.stat().st_nlink, 1)
                self.assertEqual(sorted(path.name for path in base.iterdir()),
                                 ["mb-upload", "sealed-export", "sealed-validation", "validation"])

    def test_an_existing_upload_directory_is_never_replaced_or_filled(self) -> None:
        arguments = self.verified("verify_target", "target-a")
        for existing in ("directory", "file", "dangling link"):
            with self.subTest(existing=existing):
                if existing == "directory":
                    self.upload.mkdir()
                elif existing == "file":
                    self.upload.write_bytes(b"earlier")
                else:
                    self.upload.symlink_to(self.temporary / "nowhere")
                with self.assertRaises(MbError):
                    self.write_upload(arguments)
                if existing == "directory":
                    self.assertEqual(list(self.upload.iterdir()), [])
                    self.upload.rmdir()
                else:
                    self.assertTrue(self.upload.is_symlink() or self.upload.read_bytes() == b"earlier")
                    self.upload.unlink()
        self.write_upload(arguments)
        before = tree(self.upload)
        with self.assertRaises(MbError):
            self.write_upload(arguments)
        self.assertEqual(tree(self.upload), before)

    def test_a_report_or_the_record_never_takes_or_shadows_a_path_of_the_export(self) -> None:
        def staged(name: str) -> dict:
            plan = ci_staged_plan()
            plan["targets"][1]["outputs"].append({"path": name, "lane_id": None, "role": "native-report"})
            plan["plan_sha256"] = plan_sha256(plan)
            return plan

        # A planned output at the path of the record, of the unit's report, in another case, or
        # below a directory named like one of them.
        for name in (RECORD, "target-c.json", "Target-C.json", "CI-VALIDATION.JSON", "target-c.json/manifest.json",
                     f"{RECORD}/manifest.json"):
            with self.subTest(output=name):
                self.within(name.replace("/", "-"))
                arguments = self.verified("verify_target", "target-c", plan=staged(name))
                with self.assertRaisesRegex(MbError, "collides with a path of the sealed export"):
                    self.write_upload(arguments)
                self.assertFalse(self.upload.exists())
        # A lane file at the root that is named like the lane's report.
        self.within("lane")
        self.plan = ci_staged_plan()
        arguments = self.verified("verify_runtime", "lane-a",
                                  extra={"lane-a.json": ("native-report", report("lane-a", native=True))})
        with self.assertRaisesRegex(MbError, "collides with a path of the sealed export"):
            self.write_upload(arguments)
        self.assertFalse(self.upload.exists())

    def test_the_export_must_be_the_envelope_that_was_verified(self) -> None:
        arguments = self.verified("verify_build", None)
        jar = self.export / "files/Example Mod - lane-a.jar"
        original = jar.read_bytes()
        mutations = {"changed byte": lambda: jar.write_bytes(original.replace(b"p", b"q", 1)),
                     "extra file": lambda: (self.export / "files/unplanned.jar").write_bytes(b"x"),
                     "missing file": lambda: jar.unlink(),
                     "other envelope bytes": lambda: (self.export / ENVELOPE).write_bytes(
                         b" " + canonical_json(arguments["envelope"]))}
        for label, mutate in mutations.items():
            mutate()
            with self.subTest(case=label), self.assertRaises(MbError):
                self.write_upload(arguments)
            self.assert_nothing_written()
            jar.write_bytes(original)
            jar.chmod(0o600)
            (self.export / "files/unplanned.jar").unlink(missing_ok=True)
            (self.export / ENVELOPE).write_bytes(canonical_json(arguments["envelope"]))
        other = copy.deepcopy(arguments["envelope"])
        other["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(MbError):
            self.write_upload(arguments, envelope=other)
        self.assert_nothing_written()
        self.assertEqual(self.write_upload(arguments), self.record)

    def test_the_hook_the_unit_and_the_attempt_must_be_those_of_the_export(self) -> None:
        arguments = self.verified("verify_target", "target-a")
        complete = ci_envelope(self.plan)
        wrong = [{"hook": "verify_build", "unit_id": None},  # A partition is not the complete Build.
                 {"unit_id": "target-c"}, {"hook": "verify_runtime", "unit_id": "lane-a"},
                 {"envelope": complete}, {"run_attempt": 3}, {"run_id": 41}, {"input_sha256": "c" * 64},
                 {"source_config_sha256": "c" * 64}, {"hook": "derive_plan"}, {"unit_id": None}]
        for changes in wrong:
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                self.write_upload(arguments, **changes)
            self.assert_nothing_written()
        # A partition another attempt sealed is not this job's, whatever its verification says.
        earlier = self.temporary / "earlier"
        envelope = seal_build(earlier, self.plan, target_id="target-a", attempt=1)
        with self.assertRaisesRegex(MbError, "not sealed by this run attempt"):
            validation.materialize_validated_export(earlier, self.sealed, self.upload,
                                                    **{**arguments, "envelope": envelope})
        self.assertFalse(self.upload.exists())

    def test_a_verification_never_goes_up_with_an_export_of_another_scope(self) -> None:
        # Every digest agrees in these cases; only the scope of the export is not the hook's.
        self.within("build-over-partition")
        partition = seal_build(self.export, self.plan, target_id="target-a")
        context = self.seal_verification("verify_build", None, input_sha256=digest(canonical_json(partition)))
        with self.assertRaisesRegex(MbError, "uploads the complete Build"):
            self.write_upload({**context, "envelope": partition})
        self.assertFalse(self.upload.exists())
        self.within("target-over-build")
        complete = seal_build(self.export, self.plan)
        context = self.seal_verification("verify_target", "target-a", input_sha256=digest(canonical_json(complete)))
        with self.assertRaisesRegex(MbError, "its own partition"):
            self.write_upload({**context, "envelope": complete})
        self.assertFalse(self.upload.exists())
        self.within("target-over-another-partition")
        other = seal_build(self.export, self.plan, target_id="target-c")
        context = self.seal_verification("verify_target", "target-a", input_sha256=digest(canonical_json(other)))
        with self.assertRaisesRegex(MbError, "its own partition"):
            self.write_upload({**context, "envelope": other})
        self.assertFalse(self.upload.exists())
        self.within("lane-over-another-lane")
        _, lane = seal_lane(self.export, self.plan, "lane-a")
        context = self.seal_verification("verify_runtime", "lane-b", run_id=43)
        with self.assertRaisesRegex(MbError, "exactly its own sealed lane"):
            self.write_upload({**context, "envelope": lane})
        self.assertFalse(self.upload.exists())

    def test_reports_go_up_beside_the_envelope_under_the_names_of_the_contract_only(self) -> None:
        # A record may name a report anywhere; what is uploaded is `<unit id>.json` in the root.
        envelope = seal_build(self.export, self.plan, target_id="target-a")
        data = report("target-a")
        context = self.context("verify_target", "target-a", input_sha256=digest(canonical_json(envelope)))
        record = self.expected({"target-a.json": data}, **{key: context[key] for key in context if key != "plan"})
        record["reports"][0]["path"] = "reports/target-a.json"
        write(self.sealed, {"reports/target-a.json": data, RECORD: canonical_json(record)})
        self.assertEqual(validation.verify_validation_export(self.sealed, **context), record)
        with self.assertRaisesRegex(MbError, "a report is not named `<unit id>.json`"):
            self.write_upload({**context, "envelope": envelope})
        self.assertFalse(self.upload.exists())

    def test_a_build_verification_answers_for_the_digest_of_the_envelope_it_goes_up_with(self) -> None:
        envelope = seal_build(self.export, self.plan)
        context = self.seal_verification("verify_build", None)  # Sealed over some other input digest.
        self.assertNotEqual(context["input_sha256"], digest(canonical_json(envelope)))
        with self.assertRaisesRegex(MbError, "answers for exactly the envelope"):
            self.write_upload({**context, "envelope": envelope})
        self.assert_nothing_written()

    def test_a_lane_upload_needs_its_own_lane_and_its_own_verification(self) -> None:
        arguments = self.verified("verify_runtime", "lane-a")
        for changes in ({"unit_id": "lane-b"}, {"hook": "verify_target", "unit_id": "target-a"}, {"run_id": 42},
                        {"input_sha256": digest(canonical_json(arguments["envelope"]))}):
            with self.subTest(changes=list(changes)), self.assertRaises(MbError):
                self.write_upload(arguments, **changes)
            self.assert_nothing_written()
        log = self.export / "lanes/lane-a/logs/client.log"
        log.write_bytes(b"a line the envelope does not know\n")
        with self.assertRaises(MbError):
            self.write_upload(arguments)
        self.assert_nothing_written()
        log.write_bytes(b"")
        self.assertEqual(self.write_upload(arguments), self.record)
        self.assertEqual((self.upload / "lanes/lane-a/logs/client.log").read_bytes(), b"")  # An empty log stays.

    def test_a_source_that_changes_while_it_is_copied_leaves_no_upload(self) -> None:
        arguments = self.verified("verify_build", None)
        original = validation.copy_selected_regular_files

        def changing(root, stage_fd, **bounds):
            (self.sealed / "target-a.json").write_bytes(report("target-a", late=True))
            return original(root, stage_fd, **bounds)

        with patch.object(validation, "copy_selected_regular_files", side_effect=changing), \
                self.assertRaises(MbError):
            self.write_upload(arguments)
        self.assert_nothing_written()


class RoundTripTests(UploadCase):
    """The uploaded artifact as its consumer reads it: a real ZIP through the transport readers.

    Complete Builds include their validation record; bare target copies test export transport.
    """

    def uploaded(self, plan: dict, hook: str, unit_id: str | None) -> tuple[dict, bytes, dict[str, bytes]]:
        """Verify and upload one export of ``plan``; return the upload's arguments, the archive of
        the export alone and the detached verification files."""

        self.within(unit_id or "complete")
        arguments = self.verified(hook, unit_id, plan=plan)
        self.write_upload(arguments)
        archive = upload_archive(self.upload)
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            self.assertEqual(sorted(name for name in package.namelist() if not name.endswith("/")),
                             sorted(tree(self.upload)))
        export, detached = detach(archive)
        self.assertEqual(sorted(detached), sorted([RECORD, *(unit["id"] + ".json"
                                                             for unit in self.units(hook, unit_id))]))
        return arguments, archive if hook == "verify_build" else export, detached

    def assert_detached_verification(self, arguments: dict, detached: dict[str, bytes], record: dict) -> None:
        """What was detached is the sealed verification of that export, verifiable on its own."""

        directory = self.temporary / f"detached-{len(list(self.temporary.iterdir()))}"
        write(directory, detached)
        context = {key: value for key, value in arguments.items() if key != "envelope"}
        self.assertEqual(validation.verify_validation_export(directory, **context), record)
        self.assertEqual(record["input_sha256"], arguments["input_sha256"])

    def test_the_complete_build_reaches_the_reader_of_a_completed_build(self) -> None:
        world = World()
        world.add_run("build", "build-full")
        arguments, export, detached = self.uploaded(world.plan, "verify_build", None)
        record, sealed = self.record, tree(self.export)
        descriptor = world.publish(world.describe("build", "full", "build", export), export)
        output = self.temporary / "downloaded"
        envelope = transport.download_completed_build(world.api, descriptor=descriptor, plan=world.plan, output=output)
        self.assertEqual(envelope, arguments["envelope"])
        self.assertEqual(tree(output), sealed)
        self.assertEqual(verify_build_export(output, plan=world.plan), envelope)
        self.assert_detached_verification(arguments, detached, record)
        # The record names what the reader verified: the digest of that very envelope.
        self.assertEqual(record["input_sha256"], digest(canonical_json(envelope)))

    def test_every_target_partition_reaches_the_reader_of_the_assembling_job(self) -> None:
        world = target_set_world()
        descriptors, expected = [], []
        for index, target in enumerate(world.plan["targets"]):
            arguments, export, detached = self.uploaded(world.plan, "verify_target", target["id"])
            self.assert_detached_verification(arguments, detached, self.record)
            descriptors.append(world.publish(world.describe("build", "full", "target", export, unit_id=target["id"],
                                                            artifact_id=110 + index), export))
            expected.append((arguments["envelope"], tree(self.export)))
        output = self.temporary / "targets"
        partitions = transport.download_target_set(world.api, descriptors=descriptors, plan=world.plan, run_id=42,
                                                   run_attempt=2, output=output)
        self.assertEqual([partition["envelope"] for partition in partitions], [envelope for envelope, _ in expected])
        for index, (_, sealed) in enumerate(expected):
            self.assertEqual(tree(output / f"target-{index}"), sealed)



if __name__ == "__main__":
    unittest.main()
