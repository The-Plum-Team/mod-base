"""Fixed read-only plan/Build inputs bound to protected verifier execution (MB11).

No schema or success authority is introduced: plan and envelope retain their existing kinds.
Protected admission must retain genuine source/plan/export provenance and exclude other writers.
"""

from __future__ import annotations

import hashlib
import copy
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci.controller import (ControllerFile, ControllerSources, _write_controller_files,
                                          execute_byte_fenced_controller_validator, execute_controller_validator)
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, verify_build_export
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.build_ci.validation import freeze_validation_export
from mod_base.build_ci.toolchain import ToolBytesProof, ToolTreeProof
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_worker_account, terminate_worker)
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import (authenticate_tree_read_access, file_records, grant_tree_read_access,
                              read_child_file, validate_tree_entries)
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Int, check


VALIDATOR_INPUT_ROOT = WORKER_ROOT / "validation-input"


@dataclass(frozen=True)
class BuildValidationExecution:
    execution: WorkerResult
    input_sha256: str


def _plan_bytes(plan: dict[str, Any]) -> bytes:
    validate_plan(plan)
    raw = canonical_json(plan)
    check(len(raw) <= limits.MAX_CI_PLAN_BYTES, "$.plan", "protected plan exceeds byte cap")
    return raw


def verify_validation_plan(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Require the sole canonical existing-kind plan and no undeclared input files."""

    raw = _plan_bytes(plan)
    validate_tree_entries(root, max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES)
    expected = [{"path": grammar.CI_PLAN_NAME, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}]
    observed = file_records(root, max_files=limits.MAX_CI_PLAN_INPUT_FILES, max_total_bytes=limits.MAX_CI_PLAN_BYTES,
                            max_file_bytes=limits.MAX_CI_PLAN_BYTES)
    check(observed == expected, "$.plan", "validator plan inventory differs from protected input")
    check(read_child_file(root, grammar.CI_PLAN_NAME, max_bytes=limits.MAX_CI_PLAN_BYTES) == raw,
          "$.plan", "validator plan bytes differ from retained protected plan")
    return plan


def materialize_validation_plan(output: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Independently write an existing-kind plan into an exclusive private protected stage."""

    raw = _plan_bytes(plan)
    blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()
    file = ControllerFile(grammar.CI_PLAN_NAME, "100644", blob, hashlib.sha256(raw).hexdigest(), raw)
    def writer(stage: Path, descriptor: int) -> dict[str, Any]:
        _write_controller_files(descriptor, (file,))
        return verify_validation_plan(stage, plan=plan)
    return atomic_directory(output, writer)


def _accounts(boundary: HostBoundary, validator: WorkerAccount) -> WorkerAccount:
    if type(boundary) is not HostBoundary:
        raise WorkerError("validation input requires retained host boundary evidence")
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("validation input requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("validation input validator identity changed")
    candidate = authenticate_worker_account("candidate")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("validation input identities are not isolated")
    return candidate


def _layout(boundary: HostBoundary) -> None:
    for path in (WORKER_ROOT.parent, WORKER_ROOT):
        descriptor = _open_directory(tuple(path.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o711):
                raise WorkerError("validation input traversal layout changed")
        finally:
            os.close(descriptor)


def prepare_validation_plan(*, boundary: HostBoundary, validator: WorkerAccount,
                             plan: dict[str, Any]) -> dict[str, Any]:
    """Root-only read grant for the fixed runner-owned independent plan copy."""

    authenticate_privileged_host_boundary(boundary)
    _plan_bytes(plan)
    candidate = _accounts(boundary, validator)
    descriptor = None
    admitted = False
    try:
        terminate_worker(candidate)
        _layout(boundary)
        descriptor = _open_directory(tuple(VALIDATOR_INPUT_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (boundary.uid, boundary.gid, 0o700):
            raise WorkerError("validation plan must be a private runner-owned independent copy")
        admitted = True
        verify_validation_plan(VALIDATOR_INPUT_ROOT, plan=plan)
        grant_tree_read_access(VALIDATOR_INPUT_ROOT, source_owner_uid=boundary.uid,
                               owner_uid=boundary.uid, reader_gid=validator.gid,
                               max_files=limits.MAX_CI_PLAN_INPUT_FILES, max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES,
                               max_total_bytes=limits.MAX_CI_PLAN_BYTES,
                               max_file_bytes=limits.MAX_CI_PLAN_BYTES)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, validator.gid, 0o750)):
            raise WorkerError("validation plan handoff identity or permissions changed")
        authenticate_tree_read_access(VALIDATOR_INPUT_ROOT, owner_uid=boundary.uid,
                                      reader_gid=validator.gid, max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES)
        observed = verify_validation_plan(VALIDATOR_INPUT_ROOT, plan=plan)
        authenticate_privileged_host_boundary(boundary)
        return observed
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("validation plan could not restore private traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot prepare protected validation plan") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                  envelope: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    authenticate_host_boundary(boundary)
    return _inspect_inputs(boundary, validator, plan, envelope)


def _inspect_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                     envelope: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    """Shared metadata/byte checks after the caller authenticates its runner or root role."""

    _layout(boundary)
    identities = []
    for root, cap in ((VALIDATOR_INPUT_ROOT, limits.MAX_CI_PLAN_INPUT_ENTRIES),
                      (BUILD_VALIDATION_ROOT, limits.MAX_CI_EXPORT_ENTRIES)):
        descriptor = _open_directory(tuple(root.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, validator.gid, 0o750):
                raise WorkerError("validation input root ownership or permissions changed")
            identities.append((info.st_dev, info.st_ino))
        finally:
            os.close(descriptor)
        authenticate_tree_read_access(root, owner_uid=boundary.uid, reader_gid=validator.gid, max_entries=cap)
    verify_validation_plan(VALIDATOR_INPUT_ROOT, plan=plan)
    check(verify_build_export(BUILD_VALIDATION_ROOT, plan=plan) == envelope, "$.input",
          "frozen Build differs from retained protected input")
    return tuple(identities)


def execute_frozen_build_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                   sources: ControllerSources, tools: ToolTreeProof,
                                   plan: dict[str, Any], envelope: dict[str, Any],
                                   python: str, java_home: str | None, run_id: int,
                                   run_attempt: int) -> BuildValidationExecution:
    """Bind fixed aggregate verification to read-only plan/Build bytes before and after.

    Complete Build producer verification only; cross-run runtime selection is a separate protocol.
    Native hook reads ci-plan.json in the fixed input root and the fixed sealed Build root.
    Retain actual returned execution/input digest for output freezing; native closed schemas,
    installer/import provenance, source admission and final API authority remain required.
    """

    return _execute_frozen_build_hook(boundary=boundary, validator=validator, sources=sources,
                tools=tools, plan=plan, envelope=envelope, hook="verify_build", unit_id=None,
                python=python, java_home=java_home, run_id=run_id, run_attempt=run_attempt)


def execute_frozen_target_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                    sources: ControllerSources, tools: ToolTreeProof,
                                    plan: dict[str, Any], envelope: dict[str, Any], target_id: str,
                                    python: str, java_home: str | None, run_id: int,
                                    run_attempt: int) -> BuildValidationExecution:
    """Bind fixed target verification to its exact enrolled frozen producer partition.

    Complete bundles cannot substitute for target partitions. Native dispatcher receives only
    the existing closed verify_target hook and protected MB_TARGET_ID. The same fixed read-only
    plan/Build lifecycle and retained execution/input digest apply as aggregate verification.
    Native compiler semantics, protected provenance and final API authority remain required.
    """

    return _execute_frozen_build_hook(boundary=boundary, validator=validator, sources=sources,
                tools=tools, plan=plan, envelope=envelope, hook="verify_target", unit_id=target_id,
                python=python, java_home=java_home, run_id=run_id, run_attempt=run_attempt)



def execute_byte_fenced_build_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                        sources: ControllerSources, tools: ToolBytesProof,
                                        expected_digest: str, plan: dict[str, Any], envelope: dict[str, Any],
                                        python: str, java_home: str | None, run_id: int,
                                        run_attempt: int) -> BuildValidationExecution:
    """Bind complete same-producer frozen inputs to byte-fenced protected Build verification.

    Retain fixed input identities and canonical envelope digest, with no metadata fallback.
    Native semantics and original caller/runtime/source/API provenance remain prerequisites.
    """
    return _execute_frozen_build_hook(boundary=boundary, validator=validator, sources=sources,
        tools=tools, plan=plan, envelope=envelope, hook="verify_build", unit_id=None,
        python=python, java_home=java_home, run_id=run_id, run_attempt=run_attempt,
        byte_fenced=True, expected_digest=expected_digest)


def execute_byte_fenced_target_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                         sources: ControllerSources, tools: ToolBytesProof,
                                         expected_digest: str, plan: dict[str, Any], envelope: dict[str, Any],
                                         target_id: str, python: str, java_home: str | None,
                                         run_id: int, run_attempt: int) -> BuildValidationExecution:
    """Bind the exact same-producer frozen target partition to byte-fenced verification."""
    return _execute_frozen_build_hook(boundary=boundary, validator=validator, sources=sources,
        tools=tools, plan=plan, envelope=envelope, hook="verify_target", unit_id=target_id,
        python=python, java_home=java_home, run_id=run_id, run_attempt=run_attempt,
        byte_fenced=True, expected_digest=expected_digest)


def _execute_frozen_build_hook(*, boundary: HostBoundary, validator: WorkerAccount,
                               sources: ControllerSources, tools: ToolTreeProof | ToolBytesProof,
                               plan: dict[str, Any], envelope: dict[str, Any], hook: str,
                               unit_id: str | None, python: str, java_home: str | None,
                               run_id: int, run_attempt: int, byte_fenced: bool = False,
                               expected_digest: str | None = None) -> BuildValidationExecution:
    _accounts(boundary, validator)
    try:
        if byte_fenced:
            grammar.require(grammar.DIGEST, expected_digest, "approved tool byte digest")
            if type(tools) is not ToolBytesProof or tools.digest != expected_digest:
                raise WorkerError("frozen verifier tool receipt differs from approved bytes")
        _plan_bytes(plan)
        validate_build_envelope(envelope, plan=plan)
        Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
        Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
        if hook == "verify_build":
            check(unit_id is None and envelope["scope"] == "complete" and envelope["target_id"] is None,
                  "$.input", "aggregate Build verification requires the complete bundle")
        else:
            check(hook == "verify_target" and type(unit_id) is str
                  and unit_id in {target["id"] for target in plan["targets"]},
                  "$.target_id", "target verification requires its exact protected enrolled target")
            check(envelope["scope"] == "target" and envelope["target_id"] == unit_id, "$.input",
                  "target verification requires exactly its own frozen partition")
        check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
              "$.input", "Build verifier differs from its producing run/attempt")
        initial = _read_inputs(boundary, validator, plan, envelope)
        execute = execute_byte_fenced_controller_validator if byte_fenced else execute_controller_validator
        byte_arguments = {"expected_digest": expected_digest} if byte_fenced else {}
        result = execute(boundary=boundary, validator=validator, sources=sources,
                    tools=tools, **byte_arguments, plan=plan, hook=hook, unit_id=unit_id, python=python,
                    java_home=java_home, run_id=run_id, run_attempt=run_attempt)
        if _read_inputs(boundary, validator, plan, envelope) != initial:
            raise WorkerError("validation input directory identities changed during execution")
        return BuildValidationExecution(result, hashlib.sha256(canonical_json(envelope)).hexdigest())
    except OSError as error:
        raise WorkerError("cannot execute frozen Build verification") from error
    finally:
        terminate_worker(validator)


def freeze_frozen_build_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                    sources: ControllerSources, bound: BuildValidationExecution,
                                    plan: dict[str, Any], envelope: dict[str, Any],
                                    run_id: int, run_attempt: int) -> dict[str, Any]:
    """Root-only bind retained Build execution to exact live inputs and independent receipt freeze.

    Hook/unit/input digest come from the retained validated envelope, never a caller's separate
    free-form freeze context. Caller retains genuine execution/source provenance across the
    protected privilege transition. Matching constructible objects confer no authority. Native
    domain verification and final graph/API/upload/status admission remain additional obligations.
    """

    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    try:
        _plan_bytes(plan)
        validate_build_envelope(envelope, plan=plan)
        Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
        Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
        check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
              "$.input", "validation freeze differs from its producing run/attempt")
        raw = canonical_json(envelope)
        check(len(raw) <= limits.MAX_CI_ENVELOPE_BYTES, "$.input", "retained envelope exceeds byte cap")
        check(type(bound) is BuildValidationExecution
              and bound.input_sha256 == hashlib.sha256(raw).hexdigest(),
              "$.execution.input_sha256", "retained execution differs from exact frozen input")
        execution = bound.execution
        check(type(execution) is WorkerResult and type(execution.returncode) is int and execution.returncode == 0
              and type(execution.log) is bytes and len(execution.log) <= limits.MAX_CI_LOG_BYTES
              and type(execution.truncated) is bool,
              "$.execution", "validation freeze requires retained successful bounded execution")
        plan, envelope = copy.deepcopy(plan), copy.deepcopy(envelope)
        hook, unit_id = (("verify_build", None) if envelope["scope"] == "complete"
                         else ("verify_target", envelope["target_id"]))
        terminate_worker(validator)
        initial = _inspect_inputs(boundary, validator, plan, envelope)
        receipt = freeze_validation_export(boundary=boundary, validator=validator, sources=sources,
                    execution=execution, plan=plan, hook=hook, unit_id=unit_id,
                    run_id=run_id, run_attempt=run_attempt, input_sha256=bound.input_sha256)
        if _inspect_inputs(boundary, validator, plan, envelope) != initial:
            raise WorkerError("validation input identities changed during receipt freeze")
        authenticate_privileged_host_boundary(boundary)
        return receipt
    except OSError as exc:
        raise WorkerError("cannot bind frozen Build validation receipt") from exc
    finally:
        terminate_worker(validator)
