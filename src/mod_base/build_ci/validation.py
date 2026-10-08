"""Closed verifier output records and independent frozen-byte copying (MB11).

Native report semantics, real protected verifier execution and immutable API ownership remain
separate admission requirements. A matching record or hash is never status/upload authority.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import check_output_paths, repo_path, validate_identity, validate_plan
from mod_base.build_ci.controller import ControllerSources, _validate_sources
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_worker_account, terminate_worker)
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.secure_json import loads
from mod_base.io.tree import (authenticate_tree_private_access, copy_regular_files, file_records,
                              privatize_tree_copy, read_child_file, validate_tree_entries)
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Const, Int, List, Nullable, Obj, Str, check


_SHA = Str(grammar.SHA256, max_len=64)
_UNIT = Str(grammar.CI_UNIT_ID, max_len=80)
_REPORT = Obj({"unit_id": _UNIT, "native_contract_sha256": _SHA,
               "path": repo_path,
               "size": Int(1, limits.MAX_CI_REPORT_BYTES), "sha256": _SHA})
_VERSIONS = readable_schema_versions("mod-base.ci.validation")
_RECEIPT = Obj({
    "kind": Const("mod-base.ci.validation"), "schema_version": Int(min(_VERSIONS), max(_VERSIONS)),
    "identity": validate_identity, "plan_sha256": _SHA,
    "profile": Str(choices=("quick-skin", "block-pops")),
    "hook": Str(choices=("verify_target", "verify_build", "verify_runtime")), "unit_id": Nullable(_UNIT),
    "run_id": Int(1, limits.MAX_RUN_ID), "run_attempt": Int(1, limits.MAX_RUN_ATTEMPT),
    "source_config_sha256": _SHA, "input_sha256": _SHA,
    "reports": List(_REPORT, min_items=1, max_items=limits.MAX_CI_LANES,
                    unique_by=lambda item: item["unit_id"]),
})

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


def verify_validation_export(root: Path, *, plan: dict[str, Any], hook: str, unit_id: str | None,
                              run_id: int, run_attempt: int, source_config_sha256: str,
                              input_sha256: str) -> dict[str, Any]:
    """Check canonical frozen verifier output against protected execution/input context.

    Caller must retain actual protected execution, stop/lock the UID, reclaim readable output,
    exclude writers and authenticate native report semantics. This checks context and bytes only.
    """

    validate_plan(plan)
    Str(choices=("verify_target", "verify_build", "verify_runtime"))(hook, "$.expected.hook")
    Nullable(_UNIT)(unit_id, "$.expected.unit_id")
    Int(1, limits.MAX_RUN_ID)(run_id, "$.expected.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.expected.run_attempt")
    _SHA(source_config_sha256, "$.expected.source_config_sha256")
    _SHA(input_sha256, "$.expected.input_sha256")
    if hook == "verify_build":
        check(unit_id is None, "$.expected.unit_id", "aggregate Build has no unit")
    else:
        units = plan["targets"] if hook == "verify_target" else plan["lanes"]
        check(unit_id in {unit["id"] for unit in units}, "$.expected.unit_id", "unit is outside the protected plan")
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
    inventory = sorted(({key: report[key] for key in ("path", "size", "sha256")}
                        for report in document["reports"]), key=lambda report: report["path"])
    check(observed == inventory, "$.reports", "native verification reports differ from exact byte inventory")
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
    """Create independent private frozen verifier bytes; never replace an existing output.

    Source quiescence/reclamation, parent protection and native semantics remain caller obligations.
    No artifact or gate is authorized by structural/byte verification alone.
    """

    context = dict(plan=plan, hook=hook, unit_id=unit_id, run_id=run_id, run_attempt=run_attempt,
                   source_config_sha256=source_config_sha256, input_sha256=input_sha256)
    expected = verify_validation_export(root, **context)
    raw = canonical_json(expected)
    inventory = [{key: report[key] for key in ("path", "size", "sha256")} for report in expected["reports"]]
    inventory.append({"path": grammar.CI_VALIDATION_NAME, "size": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})
    inventory.sort(key=lambda record: record["path"])

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_files(root, stage_fd, max_files=limits.MAX_CI_LANES + 1,
                                    max_entries=limits.MAX_CI_EXPORT_ENTRIES,
                                    max_total_bytes=limits.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                                    max_file_bytes=limits.MAX_CI_REPORT_BYTES)
        check(copied == inventory, "$.reports", "copied validation bytes differ from protected admission")
        observed = verify_validation_export(stage, **context)
        check(observed == expected, "$.validation", "staged validation differs from protected admission")
        return observed

    return atomic_directory(output, writer)


def freeze_validation_export(*, boundary: HostBoundary, validator: WorkerAccount,
                              sources: ControllerSources, execution: WorkerResult,
                              plan: dict[str, Any], hook: str, unit_id: str | None,
                              run_id: int, run_attempt: int, input_sha256: str) -> dict[str, Any]:
    """Protected-root-only freeze of the fixed verifier output into a runner-private copy.

    Retain real protected source/execution receipts in memory; constructed objects confer no
    provenance. Native schemas/semantics, input authenticity and final API/gate admission remain
    required. Never chown the validator original; only the new protected independent copy.
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
    candidate = authenticate_worker_account("candidate")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("validation freeze identities are not isolated")
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
