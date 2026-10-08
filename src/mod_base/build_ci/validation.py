"""What a verification leaves: its reports, the record protected code builds, and the upload (MB11).

A ``verify_*`` hook writes one native report per unit (``<unit id>.json``) into the validator's
``validation/`` directory and nothing else; it never writes a kit document. Root copies exactly
those reports into the runner-private ``sealed-validation/`` and writes the ``mod-base.ci.validation``
record beside them, built from the plan, the hook, the unit, the producing attempt, the digest of
the inputs the hook was given and the reports found. The job then uploads one directory: the
sealed export under its own paths with its envelope and, beside the envelope, that record and the
reports.

Native report semantics, real protected execution and immutable API ownership remain separate
admission requirements. A matching record or hash is never status or upload authority.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import os
import stat
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS, readable_schema_versions
from mod_base.build_ci import adapter
from mod_base.build_ci.controller import ControllerSources, _validate_sources
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.protocol import PROFILES, check_output_paths, repo_path, validate_identity, validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.build_ci.runtime_exports import _bounds as _runtime_bounds
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_peer_account, authenticate_worker_account, terminate_worker)
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import (EXPORT_PATHS, authenticate_tree_private_access, copy_regular_data_files,
                              copy_regular_files, copy_selected_regular_files, file_records, privatize_tree_copy,
                              read_child_file, validate_tree_entries)
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Const, Int, List, Nullable, Obj, Str, check


#: The hooks whose reports are sealed: one job ends with each (``ci worker-validate --hook``).
VERIFICATION_HOOKS = ("verify_target", "verify_build", "verify_runtime")
_SHA = Str(grammar.SHA256, max_len=64)
_UNIT = Str(grammar.CI_UNIT_ID, max_len=80)
_REPORT = Obj({"unit_id": _UNIT, "native_contract_sha256": _SHA,
               "path": repo_path,
               "size": Int(1, limits.MAX_CI_REPORT_BYTES), "sha256": _SHA})
_VERSIONS = readable_schema_versions("mod-base.ci.validation")
_RECEIPT = Obj({
    "kind": Const("mod-base.ci.validation"), "schema_version": Int(min(_VERSIONS), max(_VERSIONS)),
    "identity": validate_identity, "plan_sha256": _SHA,
    "profile": Str(choices=PROFILES),
    "hook": Str(choices=VERIFICATION_HOOKS), "unit_id": Nullable(_UNIT),
    "run_id": Int(1, limits.MAX_RUN_ID), "run_attempt": Int(1, limits.MAX_RUN_ATTEMPT),
    "source_config_sha256": _SHA, "input_sha256": _SHA,
    "reports": List(_REPORT, min_items=1, max_items=limits.MAX_CI_LANES,
                    unique_by=lambda item: item["unit_id"]),
})
_REPORT_SET = "the verification must leave exactly one `<unit id>.json` report per unit and nothing else"

VALIDATOR_OUTPUT_ROOT = WORKER_ROOT / "validator-home" / "validation"
SEALED_VALIDATION_ROOT = WORKER_ROOT / "sealed-validation"


def validate_validation_receipt(document: Any, *, plan: dict[str, Any] | None = None,
                                 path: str = "$") -> dict[str, Any]:
    """Validate a new v1 verifier output kind; no success boolean or unknown field is allowed."""

    _RECEIPT(document, path)
    reports = document["reports"]
    paths = [report["path"] for report in reports]
    check_output_paths(paths, f"{path}.reports")
    check(all(name.endswith(".json") for name in paths), f"{path}.reports", "verification reports must be JSON")
    check(all(name.casefold() != grammar.CI_VALIDATION_NAME.casefold() for name in paths),
          f"{path}.reports", "report cannot overwrite the validation record")
    check(sum(report["size"] for report in reports) <= limits.MAX_CI_EXPORT_TREE_BYTES,
          f"{path}.reports", "validation reports exceed the whole-export byte cap")
    if document["hook"] == "verify_build":
        check(document["unit_id"] is None, f"{path}.unit_id", "aggregate Build has no unit")
    else:
        check(document["unit_id"] is not None and len(reports) == 1
              and reports[0]["unit_id"] == document["unit_id"],
              f"{path}.reports", "unit verification requires exactly its own report")
    if plan is not None:
        validate_plan(plan)
        for key in ("identity", "profile", "plan_sha256"):
            check(document[key] == plan[key], f"{path}.{key}", "differs from the protected plan")
        units = plan["lanes"] if document["hook"] == "verify_runtime" else plan["targets"]
        if document["hook"] != "verify_build":
            units = [unit for unit in units if unit["id"] == document["unit_id"]]
        check(bool(units), f"{path}.unit_id", "unit is outside the protected plan")
        expected = [(unit["id"], unit["native_contract_sha256"]) for unit in units]
        check([(report["unit_id"], report["native_contract_sha256"]) for report in reports] == expected,
              f"{path}.reports", "reports do not cover the exact ordered protected native contracts")
    return document


def _units(*, plan: dict[str, Any], hook: str, unit_id: str | None, run_id: int, run_attempt: int,
           source_config_sha256: str, input_sha256: str) -> list[dict[str, Any]]:
    """Require the protected context of one verification; return the plan units it reports on."""

    validate_plan(plan)
    Str(choices=VERIFICATION_HOOKS)(hook, "$.expected.hook")
    Nullable(_UNIT)(unit_id, "$.expected.unit_id")
    Int(1, limits.MAX_RUN_ID)(run_id, "$.expected.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.expected.run_attempt")
    _SHA(source_config_sha256, "$.expected.source_config_sha256")
    _SHA(input_sha256, "$.expected.input_sha256")
    if hook == "verify_build":
        check(unit_id is None, "$.expected.unit_id", "aggregate Build has no unit")
        return list(plan["targets"])
    units = [unit for unit in plan["targets" if hook == "verify_target" else "lanes"] if unit["id"] == unit_id]
    check(len(units) == 1, "$.expected.unit_id", "unit is outside the protected plan")
    return units


def _require_reports(root: Path, names: tuple[str, ...]) -> None:
    """Refuse, before any byte is read, a report directory that is not exactly the files ``names``."""

    try:
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_CLOEXEC", 0))
        try:
            with os.scandir(descriptor) as entries:
                found = sorted(entry.name for entry in itertools.islice(entries, len(names) + 1))
        finally:
            os.close(descriptor)
    except OSError as error:
        raise WorkerError("cannot list the reports of the verification") from error
    check(found == sorted(names), "$.reports", _REPORT_SET)


def _inventory(files: list[dict[str, Any]], name: str | None = None, raw: bytes = b"") -> list[dict[str, Any]]:
    """The byte inventory of ``files`` by path, with the kit document ``name`` that describes them."""

    records = [{key: file[key] for key in ("path", "size", "sha256")} for file in files]
    if name is not None:
        records.append({"path": name, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return sorted(records, key=lambda record: record["path"])


def build_validation_record(root: Path, *, plan: dict[str, Any], hook: str, unit_id: str | None,
                            run_id: int, run_attempt: int, source_config_sha256: str,
                            input_sha256: str) -> dict[str, Any]:
    """The ``mod-base.ci.validation`` record of one verification, built from the reports in ``root``.

    The hook contributes report bytes and nothing else. ``root`` must hold exactly what the
    contract makes it leave (``adapter.hook_outputs``): one ``<unit id>.json`` for the unit, or for
    every target of ``verify_build``, each a canonical JSON object of at most
    ``MAX_CI_REPORT_BYTES`` in a single-linked regular file. A missing or an extra entry, a link,
    a directory, other JSON or other bytes is a rejection. Identity, plan hash, profile, hook,
    unit, run, attempt, the digest of the protected config, the digest of the verified inputs and
    every unit's native contract come from the caller's protected context. Caller excludes
    writers of ``root``; this proves no execution.
    """

    units = _units(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
                   source_config_sha256=source_config_sha256, input_sha256=input_sha256)
    names = adapter.hook_outputs(hook, plan=plan, unit_id=unit_id)
    _require_reports(root, names)
    observed = {record["path"]: record for record in file_records(
        root, max_files=len(names), max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES,
        max_file_bytes=limits.MAX_CI_REPORT_BYTES)}
    check(sorted(observed) == sorted(names), "$.reports", _REPORT_SET)
    reports = []
    for unit, name in zip(units, names):
        data = read_child_file(root, name, max_bytes=limits.MAX_CI_REPORT_BYTES)
        check((len(data), hashlib.sha256(data).hexdigest()) == (observed[name]["size"], observed[name]["sha256"]),
              "$.reports", "verification report changed while it was read")
        parsed = loads(data, label="native verification report", max_bytes=limits.MAX_CI_REPORT_BYTES)
        check(type(parsed) is dict and data == canonical_json(parsed), "$.reports",
              "native verification report must be a canonical JSON object")
        reports.append({"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
                        "path": name, "size": len(data), "sha256": observed[name]["sha256"]})
    record = {"kind": "mod-base.ci.validation", "schema_version": SCHEMA_VERSIONS["mod-base.ci.validation"],
              "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
              "profile": plan["profile"], "hook": hook, "unit_id": unit_id, "run_id": run_id,
              "run_attempt": run_attempt, "source_config_sha256": source_config_sha256,
              "input_sha256": input_sha256, "reports": reports}
    validate_validation_receipt(record, plan=plan)
    check(len(canonical_json(record)) <= limits.MAX_CI_RECORD_BYTES, "$.validation",
          "validation record exceeds its byte cap")
    return record


def verify_validation_export(root: Path, *, plan: dict[str, Any], hook: str, unit_id: str | None,
                              run_id: int, run_attempt: int, source_config_sha256: str,
                              input_sha256: str) -> dict[str, Any]:
    """Check a sealed verification (the record and its reports) against protected context.

    Caller must retain actual protected execution, stop/lock the UID, reclaim readable output,
    exclude writers and authenticate native report semantics. This checks context and bytes only.
    """

    _units(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
           source_config_sha256=source_config_sha256, input_sha256=input_sha256)
    validate_tree_entries(root, max_entries=limits.MAX_CI_EXPORT_ENTRIES)
    raw = read_child_file(root, grammar.CI_VALIDATION_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    document = loads(raw, label=grammar.CI_VALIDATION_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    validate_validation_receipt(document, plan=plan)
    check(raw == canonical_json(document), "$.validation", "validation record must be canonical JSON")
    expected = {"hook": hook, "unit_id": unit_id, "run_id": run_id, "run_attempt": run_attempt,
                "source_config_sha256": source_config_sha256, "input_sha256": input_sha256}
    check(all(document[key] == value for key, value in expected.items()),
          "$.validation", "validation record differs from protected execution/input context")
    observed = file_records(root, exclude=(grammar.CI_VALIDATION_NAME,),
                            max_files=limits.MAX_CI_LANES + 1,
                            max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                            max_file_bytes=limits.MAX_CI_REPORT_BYTES)
    check(observed == _inventory(document["reports"]), "$.reports",
          "native verification reports differ from exact byte inventory")
    for report in document["reports"]:
        data = read_child_file(root, report["path"], max_bytes=limits.MAX_CI_REPORT_BYTES)
        check(len(data) == report["size"] and hashlib.sha256(data).hexdigest() == report["sha256"],
              "$.reports", "verification report changed during strict JSON admission")
        parsed = loads(data, label="native verification report", max_bytes=limits.MAX_CI_REPORT_BYTES)
        check(type(parsed) is dict and data == canonical_json(parsed), "$.reports",
              "native verification report must be a canonical JSON object")
    check(read_child_file(root, grammar.CI_VALIDATION_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES) == raw,
          "$.validation", "validation record changed during byte verification")
    return document


def materialize_validation_export(root: Path, output: Path, *, plan: dict[str, Any], hook: str,
                                  unit_id: str | None, run_id: int, run_attempt: int,
                                  source_config_sha256: str, input_sha256: str) -> dict[str, Any]:
    """Seal one verification: copy its reports from ``root`` into the new private directory
    ``output`` and write beside them the record built from that copy; never replace an output.

    ``root`` is what the hook left and must be exactly its reports (:func:`build_validation_record`).
    The record is built from the independent copy, never from the original, and the finished
    stage is verified as a consumer would before it is published. Source quiescence and
    reclamation, parent protection and native semantics remain caller obligations. No artifact or
    gate is authorized by structural or byte verification alone.
    """

    context = dict(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
                   source_config_sha256=source_config_sha256, input_sha256=input_sha256)
    _units(**context)
    names = adapter.hook_outputs(hook, plan=plan, unit_id=unit_id)
    _require_reports(root, names)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_files(root, stage_fd, max_files=len(names), max_entries=len(names) + 1,
                                    max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES,
                                    max_file_bytes=limits.MAX_CI_REPORT_BYTES)
        record = build_validation_record(stage, **context)
        check(_inventory(record["reports"]) == copied, "$.reports", "the record does not describe the copied reports")
        write_new(stage_fd, grammar.CI_VALIDATION_NAME, canonical_json(record))
        check(verify_validation_export(stage, **context) == record, "$.validation",
              "sealed verification differs from the record built for it")
        return record

    return atomic_directory(output, writer)


def _disjoint(paths: list[str]) -> bool:
    """Whether no two paths are equal, differ only in case, or use one's file as the other's directory."""

    folded = [path.casefold() for path in paths]
    files = set(folded)
    return len(files) == len(folded) and not any(
        "/".join(parts[:count]) in files for parts in (path.split("/") for path in folded)
        for count in range(1, len(parts)))


def materialize_validated_export(export: Path, validation: Path, output: Path, *, plan: dict[str, Any],
                                 envelope: dict[str, Any], hook: str, unit_id: str | None, run_id: int,
                                 run_attempt: int, source_config_sha256: str,
                                 input_sha256: str) -> dict[str, Any]:
    """Write the directory a job uploads; return its validation record. Never replace an output.

    ``output`` becomes the export root as a reader of the artifact finds it: every file of the
    sealed export under its own path, its envelope (``ci-envelope.json``, or
    ``ci-runtime-envelope.json`` for a lane) and, beside the envelope, ``ci-validation.json`` and
    one ``<unit id>.json`` per report. ``export`` must be exactly ``envelope``: one target's
    partition for ``verify_target``, the complete Build for ``verify_build``, the lane's results
    for ``verify_runtime``, sealed by this run attempt. ``validation`` must be the sealed
    verification of exactly this context (:func:`verify_validation_export`); for a Build or a
    target its ``input_sha256`` is the digest of that very envelope (a lane's also covers its
    owning Build, which the caller binds). A report or the record that would take the path of an
    export file, differ from one only in case or be one of its directories is a rejection. Every
    byte is checked against the two inventories while it is copied into a new private stage, each
    source is inventoried again after its copy, and nothing else is written. Caller excludes
    writers of both sources and owns the parent of ``output``.
    """

    context = dict(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
                   source_config_sha256=source_config_sha256, input_sha256=input_sha256)
    _units(**context)
    runtime = hook == "verify_runtime"
    if runtime:
        validate_runtime_envelope(envelope, plan=plan)
        check((envelope["scope"], envelope["lane_id"]) == ("lane", unit_id), "$.envelope",
              "a lane verification uploads exactly its own sealed lane")
        name, cap = grammar.CI_RUNTIME_ENVELOPE_NAME, limits.MAX_CI_RUNTIME_ENVELOPE_BYTES
    else:
        validate_build_envelope(envelope, plan=plan)
        check((envelope["scope"], envelope["target_id"])
              == (("complete", None) if hook == "verify_build" else ("target", unit_id)), "$.envelope",
              "a Build verification uploads the complete Build, a target verification its own partition")
        name, cap = grammar.CI_ENVELOPE_NAME, limits.MAX_CI_ENVELOPE_BYTES
    check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
          "$.envelope.producer", "the export was not sealed by this run attempt")
    raw = canonical_json(envelope)
    check(len(raw) <= cap, "$.envelope", "envelope exceeds its byte cap")
    check(runtime or input_sha256 == hashlib.sha256(raw).hexdigest(), "$.input_sha256",
          "a Build verification answers for exactly the envelope it is uploaded with")
    record = verify_validation_export(validation, **context)
    check([report["path"] for report in record["reports"]]
          == list(adapter.hook_outputs(hook, plan=plan, unit_id=unit_id)), "$.validation.reports",
          "a report is not named `<unit id>.json`")
    sealed = _inventory(envelope["files"], name, raw)
    beside = _inventory(record["reports"], grammar.CI_VALIDATION_NAME, canonical_json(record))
    check(_disjoint([file["path"] for file in (*sealed, *beside)]), "$.upload",
          "a report or the validation record collides with a path of the sealed export")
    if runtime:
        bounds = _runtime_bounds(envelope, len(raw))
    else:
        bounds = dict(max_files=limits.MAX_CI_EXPORT_FILES + 1, max_entries=limits.MAX_CI_EXPORT_ENTRIES,
                      max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                      max_file_bytes=limits.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        # The stage is new and empty, the first copy refuses any other, and the second creates
        # its files exclusively: what the two copies report is all the stage holds.
        copied = (copy_regular_data_files if runtime else copy_regular_files)(export, stage_fd, **bounds)
        check(copied == sealed, "$.export", "the sealed export is not exactly the envelope that was verified")
        added = copy_selected_regular_files(
            validation, stage_fd, paths=tuple(file["path"] for file in beside), max_files=len(beside),
            max_entries=len(beside) + 1, max_total_bytes=sum(file["size"] for file in beside),
            max_file_bytes=max(limits.MAX_CI_REPORT_BYTES, limits.MAX_CI_RECORD_BYTES))
        check(added == beside, "$.validation", "the sealed verification is not exactly its record and reports")
        return record

    return atomic_directory(output, writer)


def freeze_validation_export(*, boundary: HostBoundary, validator: WorkerAccount,
                              sources: ControllerSources, execution: WorkerResult,
                              plan: dict[str, Any], hook: str, unit_id: str | None,
                              run_id: int, run_attempt: int, input_sha256: str) -> dict[str, Any]:
    """Protected-root-only freeze of a verification into the runner-private ``sealed-validation/``.

    The validator is terminated first. Its ``validation/`` must be exactly the reports of this
    hook, private to it; root copies them (never chowning the original), builds the record from
    the copy and hands only that copy to the runner. A job that allocated the validator alone
    has no candidate to isolate from. Retain real protected source and execution receipts in
    memory; constructed objects confer no provenance. Native schemas and semantics, input
    authenticity and final API/gate admission remain required.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    config = _validate_sources(sources, plan["identity"])
    if config["profile"] != plan["profile"]:
        raise WorkerError("validation freeze profile differs from protected plan")
    if (type(execution) is not WorkerResult or type(execution.returncode) is not int or execution.returncode != 0
            or type(execution.log) is not bytes or len(execution.log) > limits.MAX_CI_LOG_BYTES
            or type(execution.truncated) is not bool):
        raise WorkerError("validation freeze requires retained successful protected execution")
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("validation freeze requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("validation freeze validator identity changed")
    authenticate_peer_account(validator, runner_uid=boundary.uid, runner_gid=boundary.gid)
    context = dict(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
                   source_config_sha256=sources.config.sha256, input_sha256=input_sha256)
    descriptor = None
    admitted = False
    try:
        terminate_worker(validator)
        for path, owner, group, mode in ((WORKER_ROOT.parent, boundary.uid, boundary.gid, 0o711),
                                        (WORKER_ROOT, boundary.uid, boundary.gid, 0o711),
                                        (VALIDATOR_OUTPUT_ROOT.parent, validator.uid, validator.gid, 0o700)):
            parent = _open_directory(tuple(path.parts[1:]))
            try:
                info = os.fstat(parent)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (owner, group, mode):
                    raise WorkerError("validation freeze traversal or validator home identity changed")
            finally:
                os.close(parent)
        authenticate_tree_private_access(VALIDATOR_OUTPUT_ROOT, owner_uid=validator.uid,
                                         owner_gid=validator.gid, max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        expected = materialize_validation_export(VALIDATOR_OUTPUT_ROOT, SEALED_VALIDATION_ROOT, **context)
        descriptor = _open_directory(tuple(SEALED_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (0, 0, 0o700):
            raise WorkerError("validation freeze copy is not fresh private protected-root-owned")
        admitted = True
        raw = canonical_json(expected)
        privatize_tree_copy(SEALED_VALIDATION_ROOT, source_owner_uid=0, owner_uid=boundary.uid,
                             owner_gid=boundary.gid, max_files=limits.MAX_CI_LANES + 1,
                             max_entries=limits.MAX_CI_EXPORT_ENTRIES,
                             max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                             max_file_bytes=limits.MAX_CI_REPORT_BYTES)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, boundary.gid, 0o700)):
            raise WorkerError("validation freeze copy identity or private ownership changed")
        authenticate_tree_private_access(SEALED_VALIDATION_ROOT, owner_uid=boundary.uid,
                                         owner_gid=boundary.gid, max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        observed = verify_validation_export(SEALED_VALIDATION_ROOT, **context)
        if observed != expected:
            raise WorkerError("validation freeze record changed during ownership transfer")
        authenticate_privileged_host_boundary(boundary)
        return observed
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("validation freeze could not restore private traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot freeze protected verifier output") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
