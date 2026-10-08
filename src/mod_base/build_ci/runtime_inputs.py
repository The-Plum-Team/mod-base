"""Fixed frozen plan/Build/runtime inputs for byte-fenced native lane validation (MB11)."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from typing import Any

from mod_base.build_ci.controller import ControllerSources, execute_byte_fenced_controller_validator
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, verify_build_export
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                   authenticate_privileged_host_boundary)
from mod_base.build_ci.inputs import (VALIDATOR_INPUT_ROOT, _accounts, _inspect_inputs as _inspect_build_inputs,
                                     _layout, _plan_bytes, verify_validation_plan)
from mod_base.build_ci.records import bind_build_envelope, validate_build_envelope
from mod_base.build_ci.runtime_exports import _bounds, verify_runtime_export
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.build_ci.toolchain import ToolBytesProof
from mod_base.build_ci.validation import freeze_validation_export
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult, terminate_worker
from mod_base.io.tree import (authenticate_tree_private_access, authenticate_tree_read_access,
                             grant_regular_data_read_access)
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.model.validators import Int, check


RUNTIME_VALIDATION_ROOT = WORKER_ROOT / 'sealed-runtime'


@dataclass(frozen=True)
class RuntimeValidationExecution:
    execution: WorkerResult
    input_sha256: str


def _context(plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], *,
              lane_id: str, run_id: int, run_attempt: int) -> tuple[str, tuple[bytes, ...]]:
    plan_raw = _plan_bytes(plan)
    validate_build_envelope(build, plan=plan)
    validate_runtime_envelope(runtime, plan=plan)
    Int(1, limits.MAX_RUN_ID)(run_id, '$.run_id')
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, '$.run_attempt')
    check(type(lane_id) is str and runtime['scope'] == 'lane' and runtime['lane_id'] == lane_id,
          '$.lane_id', 'native runtime validation requires the exact frozen lane')
    check(build['scope'] == 'complete', '$.build', 'runtime validation requires the complete owning Build')
    bind_build_envelope(build, descriptor=runtime['owning_build'], plan=plan)
    check((runtime['producer']['run_id'], runtime['producer']['run_attempt']) == (run_id, run_attempt),
          '$.producer', 'runtime validation differs from its producing run/attempt')
    build_raw, runtime_raw = canonical_json(build), canonical_json(runtime)
    check(len(build_raw) <= limits.MAX_CI_ENVELOPE_BYTES and len(runtime_raw) <= limits.MAX_CI_RUNTIME_ENVELOPE_BYTES,
          '$.input', 'runtime validation envelope exceeds its original bound')
    digest = canonical_sha256({'format': grammar.CI_RUNTIME_INPUT_FORMAT, 'plan_sha256': plan['plan_sha256'],
                              'build_envelope_sha256': hashlib.sha256(build_raw).hexdigest(),
                              'runtime_envelope_sha256': hashlib.sha256(runtime_raw).hexdigest()})
    return digest, (plan_raw, build_raw, runtime_raw)


def _read_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                  build: dict[str, Any], runtime: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    authenticate_host_boundary(boundary)
    return _inspect_inputs(boundary, validator, plan, build, runtime)


def _inspect_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                    build: dict[str, Any], runtime: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    """Inspect original metadata/bytes after the caller authenticates runner or Root identity."""
    _layout(boundary)
    identities = []
    for root, cap in ((VALIDATOR_INPUT_ROOT, limits.MAX_CI_PLAN_INPUT_ENTRIES),
                      (BUILD_VALIDATION_ROOT, limits.MAX_CI_EXPORT_ENTRIES),
                      (RUNTIME_VALIDATION_ROOT, limits.MAX_CI_RUNTIME_ENTRIES)):
        descriptor = _open_directory(tuple(root.parts[1:]))
        try:
            info = os.fstat(descriptor)
            check((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (boundary.uid, validator.gid, 0o750),
                  '$.input', 'runtime validation root ownership/permissions changed')
            identities.append((info.st_dev, info.st_ino))
        finally:
            os.close(descriptor)
        authenticate_tree_read_access(root, owner_uid=boundary.uid, reader_gid=validator.gid, max_entries=cap)
    verify_validation_plan(VALIDATOR_INPUT_ROOT, plan=plan)
    check(verify_build_export(BUILD_VALIDATION_ROOT, plan=plan) == build, '$.build', 'frozen owning Build bytes differ')
    check(verify_runtime_export(RUNTIME_VALIDATION_ROOT, plan=plan) == runtime, '$.runtime', 'frozen lane bytes differ')
    return tuple(identities)


def _retained(raw: tuple[bytes, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(strict_loads(data, label=label, max_bytes=cap) for data, label, cap in zip(raw,
        ('original runtime plan', 'original owning Build', 'original runtime lane'),
        (limits.MAX_CI_PLAN_BYTES, limits.MAX_CI_ENVELOPE_BYTES, limits.MAX_CI_RUNTIME_ENVELOPE_BYTES)))


def prepare_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                               plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any],
                               lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]:
    """Root-only read grant for the original independent runner-private runtime lane copy.

    Plan/complete owning Build are already independently prepared read-only inputs. Caller owns
    original reclaimed-copy/native/API provenance and excludes writers. Never use on a candidate
    original. Failed admission restores admitted runtime root traversal to private; restage before
    retry rather than consuming a partial copy. Late descriptor-close failure can leave a granted
    copy; it still rejects return and quiesces the validator. Never consume a failed handoff.
    """
    authenticate_privileged_host_boundary(boundary)
    candidate = _accounts(boundary, validator)
    descriptor = None
    admitted = False
    try:
        digest, raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        retained = _retained(raw)
        terminate_worker(candidate)
        terminate_worker(validator)
        build_identities = _inspect_build_inputs(boundary, validator, retained[0], retained[1])
        descriptor = _open_directory(tuple(RUNTIME_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        check((initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) == (boundary.uid, boundary.gid, 0o700),
              '$.runtime', 'runtime handoff requires an original independent runner-private copy')
        admitted = True
        authenticate_tree_private_access(RUNTIME_VALIDATION_ROOT, owner_uid=boundary.uid,
            owner_gid=boundary.gid, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
        check(verify_runtime_export(RUNTIME_VALIDATION_ROOT, plan=retained[0]) == retained[2],
              '$.runtime', 'runtime handoff bytes differ from original lane')
        grant_regular_data_read_access(RUNTIME_VALIDATION_ROOT, source_owner_uid=boundary.uid,
            owner_uid=boundary.uid, reader_gid=validator.gid, **_bounds(retained[2], len(raw[2])))
        final = os.fstat(descriptor)
        check((final.st_dev, final.st_ino) == (initial.st_dev, initial.st_ino)
              and (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) == (boundary.uid, validator.gid, 0o750),
              '$.runtime', 'runtime handoff directory identity or permissions changed')
        check(_inspect_inputs(boundary, validator, *retained) == build_identities + ((initial.st_dev, initial.st_ino),),
              '$.input', 'runtime handoff changed an original input directory')
        closing_digest, closing_raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(closing_digest == digest and closing_raw == raw,
              '$.input', 'caller original runtime handoff inputs changed')
        authenticate_privileged_host_boundary(boundary)
        return retained[2]
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError('runtime handoff could not restore private traversal') from cleanup
        if isinstance(error, OSError):
            raise WorkerError('cannot prepare protected runtime read handoff') from error
        raise
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        except OSError as error:
            raise WorkerError('cannot close protected runtime handoff directory') from error
        finally:
            terminate_worker(validator)


def execute_byte_fenced_frozen_runtime_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                                 sources: ControllerSources, tools: ToolBytesProof,
                                                 expected_digest: str, plan: dict[str, Any],
                                                 build: dict[str, Any], runtime: dict[str, Any], lane_id: str,
                                                 python: str, java_home: str | None,
                                                 run_id: int, run_attempt: int) -> RuntimeValidationExecution:
    """Bind the existing protected verify_runtime hook to original frozen lane/owning Build bytes.

    Caller retains actual protected source/plan/API provenance, private reclaimed inputs with
    validator-only reads, independent program/runtime/installer approval and excludes writers.
    Native closed report/capture/JDK/package validation belongs to the enrolled mod dispatcher.
    Returned execution/context data are not a frozen receipt, success or upload/status authority.
    """
    _accounts(boundary, validator)
    try:
        grammar.require(grammar.DIGEST, expected_digest, 'independently approved tool byte digest')
        check(type(tools) is ToolBytesProof and tools.digest == expected_digest,
              '$.tools', 'runtime verifier tool receipt differs from independently approved bytes')
        digest, raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        retained = _retained(raw)
        initial = _read_inputs(boundary, validator, *retained)
        result = execute_byte_fenced_controller_validator(boundary=boundary, validator=validator, sources=sources,
            tools=tools, expected_digest=expected_digest, plan=retained[0], hook='verify_runtime', unit_id=lane_id,
            python=python, java_home=java_home, run_id=run_id, run_attempt=run_attempt)
        check(_read_inputs(boundary, validator, *retained) == initial,
              '$.input', 'runtime validation input directory identities changed during execution')
        closing_digest, closing_raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(closing_digest == digest and closing_raw == raw,
              '$.input', 'caller original runtime validation inputs changed during execution')
        return RuntimeValidationExecution(result, digest)
    except OSError as error:
        raise WorkerError('cannot execute fixed frozen runtime verification') from error
    finally:
        terminate_worker(validator)


def freeze_frozen_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                      sources: ControllerSources, bound: RuntimeValidationExecution,
                                      plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any],
                                      lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]:
    """Root-only freeze tied to genuinely retained lane execution and all original frozen inputs.

    Caller retains original protected execution/source provenance across privilege transition.
    Constructible execution objects or matching hashes never establish that provenance. Native
    validity, original private root lifetime and final API/graph/upload/status admission remain
    mandatory. A failed closing recheck can leave a private copy; never consume it after failure.
    """
    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    try:
        digest, raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(type(bound) is RuntimeValidationExecution and type(bound.input_sha256) is str
              and bound.input_sha256 == digest,
              '$.execution.input_sha256', 'retained runtime execution differs from original frozen inputs')
        execution = bound.execution
        check(type(execution) is WorkerResult and type(execution.returncode) is int and execution.returncode == 0
              and type(execution.log) is bytes and len(execution.log) <= limits.MAX_CI_LOG_BYTES
              and type(execution.truncated) is bool,
              '$.execution', 'runtime freeze requires retained successful bounded execution')
        retained = _retained(raw)
        terminate_worker(validator)
        initial = _inspect_inputs(boundary, validator, *retained)
        receipt = freeze_validation_export(boundary=boundary, validator=validator, sources=sources,
            execution=execution, plan=retained[0], hook='verify_runtime', unit_id=retained[2]['lane_id'],
            run_id=run_id, run_attempt=run_attempt, input_sha256=digest)
        check(_inspect_inputs(boundary, validator, *retained) == initial,
              '$.input', 'runtime validation input directory identities changed during receipt freeze')
        closing_digest, closing_raw = _context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(closing_digest == digest and closing_raw == raw,
              '$.input', 'caller original runtime validation inputs changed during receipt freeze')
        authenticate_privileged_host_boundary(boundary)
        return receipt
    except OSError as error:
        raise WorkerError('cannot bind frozen runtime validation receipt') from error
    finally:
        terminate_worker(validator)
