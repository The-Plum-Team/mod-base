"""Private runner-to-root Build execution data handoff, never a candidate success claim.

Physical origin is the protected runner UID behind its host fence. The protected caller still
owns genuine execution/source provenance, installer/import enrollment and the fixed root program.
This data channel cannot establish native validity, workflow/App authority or that provenance.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
from pathlib import Path
from collections.abc import Callable
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.controller import ControllerSources, _validate_sources
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.inputs import (BuildValidationExecution, _accounts, _layout, _plan_bytes,
                                      freeze_frozen_build_validation)
from mod_base.build_ci.records import validate_build_envelope
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult, terminate_worker
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Bool, Const, Int, Obj, Str, check


EXECUTION_HANDOFF_ROOT = WORKER_ROOT / "execution-handoff"
_SHA = Str(grammar.SHA256, max_len=64)
_VERSIONS = readable_schema_versions("mod-base.ci.execution")
_RECORD = Obj({
    "kind": Const("mod-base.ci.execution"), "schema_version": Int(min(_VERSIONS), max(_VERSIONS)),
    "run_id": Int(1, limits.MAX_RUN_ID), "run_attempt": Int(1, limits.MAX_RUN_ATTEMPT),
    "nonce": _SHA, "plan_sha256": _SHA, "source_config_sha256": _SHA, "input_sha256": _SHA,
    "returncode": Const(0), "truncated": Bool(),
    "log_base64": Str(min_len=0, max_len=limits.MAX_CI_EXECUTION_LOG_CHARS),
})


def _log(document: dict[str, Any]) -> bytes:
    try:
        log = base64.b64decode(document["log_base64"].encode("ascii"), validate=True)
    except (UnicodeError, ValueError, binascii.Error) as error:
        raise WorkerError("execution handoff log is not strict base64") from error
    check(len(log) <= limits.MAX_CI_LOG_BYTES
          and base64.b64encode(log).decode("ascii") == document["log_base64"],
          "$.log_base64", "execution handoff log is oversized or noncanonical")
    return log


def validate_execution_handoff(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Closed local v1 data; validation alone proves neither physical origin nor execution."""

    _RECORD(document, path)
    check(type(document["returncode"]) is int, f"{path}.returncode", "requires exact zero exit code")
    _log(document)
    return document


def _context(sources: ControllerSources, plan: dict[str, Any], envelope: dict[str, Any],
             run_id: int, run_attempt: int) -> dict[str, Any]:
    _plan_bytes(plan)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    validate_build_envelope(envelope, plan=plan)
    config = _validate_sources(sources, plan["identity"])
    check(config["profile"] == plan["profile"], "$.profile", "handoff controller profile differs")
    check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
          "$.producer", "execution handoff must belong to this Build job attempt")
    raw = canonical_json(envelope)
    check(len(raw) <= limits.MAX_CI_ENVELOPE_BYTES, "$.input", "execution input exceeds its byte cap")
    return {"run_id": run_id, "run_attempt": run_attempt, "plan_sha256": plan["plan_sha256"],
            "source_config_sha256": sources.config.sha256, "input_sha256": hashlib.sha256(raw).hexdigest()}


def _private(info: os.stat_result, boundary: HostBoundary, *, directory: bool) -> None:
    correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    check(correct_type and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
          == (boundary.uid, boundary.gid, 0o700 if directory else 0o600),
          "$.handoff", "execution channel must be private protected-runner-owned bytes")
    if not directory:
        check(info.st_nlink == 1 and 1 <= info.st_size <= limits.MAX_CI_EXECUTION_BYTES,
              "$.handoff", "execution record is linked, empty or oversized")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read_private_record(root_path: Path, *, name: str, owner_uid: int, owner_gid: int,
                          max_bytes: int, label: str) -> bytes:
    """Internal fixed-layout callers only; no record field selects this path or ownership."""
    def private(info: os.stat_result, *, directory: bool) -> None:
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        check(correct_type and (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
              == (owner_uid, owner_gid, 0o700 if directory else 0o600),
              "$.handoff", f"{label} must have private protected ownership")
        if not directory:
            check(info.st_nlink == 1 and 1 <= info.st_size <= max_bytes,
                  "$.handoff", f"{label} is linked, empty or oversized")

    root = _open_directory(tuple(root_path.parts[1:]))
    try:
        initial = os.fstat(root)
        private(initial, directory=True)
        with os.scandir(root) as entries:
            first = next(entries, None)
            check(first is not None and first.name == name and next(entries, None) is None,
                  "$.handoff", f"unexpected {label} directory entries")
        before = os.stat(name, dir_fd=root, follow_symlinks=False)
        private(before, directory=False)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        try:
            check(_stamp(os.fstat(descriptor)) == _stamp(before), "$.handoff", f"{label} changed before read")
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(limits.CI_PROCESS_READ_BYTES,
                                               max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                check(total <= max_bytes, "$.handoff", f"{label} grew past its cap")
                chunks.append(chunk)
            check(total == before.st_size and _stamp(os.fstat(descriptor)) == _stamp(before)
                  and _stamp(os.stat(name, dir_fd=root, follow_symlinks=False)) == _stamp(before),
                  "$.handoff", f"{label} changed while read")
        finally:
            os.close(descriptor)
        check(_stamp(os.fstat(root)) == _stamp(initial), "$.handoff", f"{label} directory changed during read")
        named = _open_directory(tuple(root_path.parts[1:]))
        try:
            check(_stamp(os.fstat(named)) == _stamp(initial), "$.handoff", f"{label} root was substituted")
        finally:
            os.close(named)
        return b"".join(chunks)
    finally:
        os.close(root)


def _read_private_handoff(boundary: HostBoundary) -> bytes:
    return _read_private_record(Path(str(EXECUTION_HANDOFF_ROOT)), name=grammar.CI_EXECUTION_NAME,
        owner_uid=boundary.uid, owner_gid=boundary.gid, max_bytes=limits.MAX_CI_EXECUTION_BYTES,
        label="execution record")


def _publish_execution(boundary: HostBoundary, document: dict[str, Any], *,
                       before_publish: Callable[[], None] | None = None) -> str:
    """Fixed exclusive channel mechanics; optional closing checks come only from protected code."""
    validate_execution_handoff(document)
    raw = canonical_json(document)
    nonce = document['nonce']
    check(len(raw) <= limits.MAX_CI_EXECUTION_BYTES, '$.handoff', 'execution record exceeds its byte cap')

    def writer(stage: Path, descriptor: int) -> None:
        _private(os.fstat(descriptor), boundary, directory=True)
        write_new(descriptor, grammar.CI_EXECUTION_NAME, raw)
        leaf = os.open(grammar.CI_EXECUTION_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
        try:
            os.fchmod(leaf, 0o600)
            os.fsync(leaf)
            _private(os.fstat(leaf), boundary, directory=False)
        finally:
            os.close(leaf)
        check(read_child_file(stage, grammar.CI_EXECUTION_NAME, max_bytes=limits.MAX_CI_EXECUTION_BYTES) == raw,
              '$.handoff', 'execution record changed during private publication')
        if before_publish is not None:
            before_publish()
        authenticate_host_boundary(boundary)
        check(read_child_file(stage, grammar.CI_EXECUTION_NAME, max_bytes=limits.MAX_CI_EXECUTION_BYTES) == raw,
              '$.handoff', 'execution record changed during closing admission')

    try:
        _layout(boundary)
        atomic_directory(Path(str(EXECUTION_HANDOFF_ROOT)), writer)
    except OSError as error:
        raise WorkerError('cannot publish private execution handoff') from error
    return nonce


def record_build_validation_execution(*, boundary: HostBoundary, sources: ControllerSources,
                                      bound: BuildValidationExecution, plan: dict[str, Any],
                                      envelope: dict[str, Any], run_id: int, run_attempt: int) -> str:
    """Runner-only exclusive local publication of retained successful execution; return nonce.

    Caller supplies the genuine returned bound execution after mandatory UID quiescence.
    A constructible object cannot establish provenance. No path, hook or program is selected
    by the record. An existing channel is never overwritten or reused.
    """

    try:
        authenticate_host_boundary(boundary)
    except OSError as error:
        raise WorkerError("cannot authenticate execution handoff runner") from error
    expected = _context(sources, plan, envelope, run_id, run_attempt)
    check(type(bound) is BuildValidationExecution and bound.input_sha256 == expected["input_sha256"],
          "$.execution", "execution differs from the protected input")
    result = bound.execution
    check(type(result) is WorkerResult and type(result.returncode) is int and result.returncode == 0
          and type(result.log) is bytes and len(result.log) <= limits.MAX_CI_LOG_BYTES
          and type(result.truncated) is bool, "$.execution", "requires retained successful bounded execution")
    try:
        nonce = os.urandom(32).hex()
    except OSError as error:
        raise WorkerError("cannot allocate fresh execution nonce") from error
    document = {"kind": "mod-base.ci.execution", "schema_version": max(_VERSIONS), **expected,
                "nonce": nonce, "returncode": result.returncode, "truncated": result.truncated,
                "log_base64": base64.b64encode(result.log).decode("ascii")}
    return _publish_execution(boundary, document)


def freeze_handed_off_build_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                      sources: ControllerSources, plan: dict[str, Any],
                                      envelope: dict[str, Any], run_id: int, run_attempt: int,
                                      nonce: str) -> dict[str, Any]:
    """Root-only physical-origin/context admission before existing independent receipt freeze.

    The root program and supplied context must themselves come from protected setup. This
    channel retains runner-origin data, not arbitrary candidate programs or native semantics.
    """

    try:
        authenticate_privileged_host_boundary(boundary)
        _accounts(boundary, validator)
    except OSError as error:
        raise WorkerError("cannot authenticate execution handoff root context") from error
    try:
        expected = _context(sources, plan, envelope, run_id, run_attempt)
        _SHA(nonce, "$.nonce")
        _layout(boundary)
        raw = _read_private_handoff(boundary)
        document = loads(raw, label=grammar.CI_EXECUTION_NAME, max_bytes=limits.MAX_CI_EXECUTION_BYTES)
        validate_execution_handoff(document)
        check(raw == canonical_json(document), "$.handoff", "execution handoff must be canonical JSON")
        check(document["nonce"] == nonce and all(document[key] == value for key, value in expected.items()),
              "$.handoff", "execution handoff differs from protected attempt/source/input context")
        bound = BuildValidationExecution(WorkerResult(document["returncode"], _log(document), document["truncated"]),
                                         document["input_sha256"])
        authenticate_privileged_host_boundary(boundary)
        return freeze_frozen_build_validation(boundary=boundary, validator=validator, sources=sources,
                    bound=bound, plan=plan, envelope=envelope, run_id=run_id, run_attempt=run_attempt)
    except OSError as error:
        raise WorkerError("cannot read protected execution handoff") from error
    finally:
        terminate_worker(validator)
