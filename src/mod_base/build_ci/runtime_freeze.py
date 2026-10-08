"""Root-only independent candidate runtime copying with original source/Build witnesses (MB11)."""

from __future__ import annotations

import os
import stat
from typing import Any

from mod_base.build_ci.exports import CANDIDATE_OUTPUT_ROOT, CANDIDATE_SOURCE_ROOT
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.inputs import _inspect_inputs as _inspect_build_inputs, _plan_bytes
from mod_base.build_ci.records import bind_build_envelope, validate_descriptor
from mod_base.build_ci.runtime_exports import _bounds, _materialize_runtime_export, verify_runtime_export
from mod_base.build_ci.runtime_inputs import RUNTIME_VALIDATION_ROOT, _context
from mod_base.build_ci.source import GitSourceEntry, _validate_inventory, verify_source_copy
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_worker_account, terminate_worker)
from mod_base.io.tree import authenticate_tree_private_access, privatize_regular_data_copy
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import Int, check


def _original_inputs(plan: dict[str, Any], build: dict[str, Any], owning_build: dict[str, Any], *,
                     lane_id: str, run_id: int, run_attempt: int) -> tuple[bytes, ...]:
    plan_raw = _plan_bytes(plan)
    validate_descriptor(owning_build)
    bind_build_envelope(build, descriptor=owning_build, plan=plan)
    check(build['scope'] == 'complete', '$.build', 'runtime freeze requires the complete original owning Build')
    check(type(lane_id) is str and lane_id in [lane['id'] for lane in plan['lanes']],
          '$.lane_id', 'runtime freeze requires the original enrolled lane')
    Int(1, limits.MAX_RUN_ID)(run_id, '$.run_id')
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, '$.run_attempt')
    raw = (plan_raw, canonical_json(build), canonical_json(owning_build))
    check(len(raw[1]) <= limits.MAX_CI_ENVELOPE_BYTES and len(raw[2]) <= limits.MAX_CI_RECORD_BYTES,
          '$.input', 'original runtime freeze input exceeds its bound')
    return raw


def freeze_runtime_export(*, boundary: HostBoundary, candidate: WorkerAccount, execution: WorkerResult,
                          inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...],
                          plan: dict[str, Any], build: dict[str, Any], owning_build: dict[str, Any],
                          lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]:
    """Copy a quiesced candidate's exact lane to a new private runner-owned independent root.

    Caller retains genuine original execution, Git/native policy, selected Build/API and complete
    program/runtime provenance; constructible values or local hashes never establish it. Actual
    native second-UID verification/receipt and final graph/API/upload/status admission remain
    mandatory. A failed closing/cleanup check may leave a private copy: never consume it.
    """
    authenticate_privileged_host_boundary(boundary)
    raw = _original_inputs(plan, build, owning_build, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
    _validate_inventory(inventory)
    check(type(generated_roots) is tuple and all(grammar.is_repo_path(path) for path in generated_roots),
          '$.generated_roots', 'runtime freeze requires exact protected generated-root policy')
    check(type(execution) is WorkerResult and type(execution.returncode) is int and execution.returncode == 0
          and type(execution.log) is bytes and len(execution.log) <= limits.MAX_CI_LOG_BYTES
          and type(execution.truncated) is bool,
          '$.execution', 'runtime freeze requires retained successful bounded candidate execution')
    check(type(candidate) is WorkerAccount and candidate.role == 'candidate'
          and authenticate_worker_account('candidate') == candidate,
          '$.candidate', 'runtime freeze requires the original fixed candidate account')
    validator = authenticate_worker_account('validator')
    check(candidate.uid != validator.uid and candidate.gid != validator.gid
          and all(account.uid != boundary.uid and account.gid != boundary.gid for account in (candidate, validator)),
          '$.accounts', 'runtime freeze accounts are not isolated')
    retained = tuple(strict_loads(data, label=label, max_bytes=cap) for data, label, cap in zip(raw,
        ('original runtime plan', 'original runtime owning Build bytes', 'original selected owning Build'),
        (limits.MAX_CI_PLAN_BYTES, limits.MAX_CI_ENVELOPE_BYTES, limits.MAX_CI_RECORD_BYTES)))
    descriptor = None
    admitted = False
    try:
        terminate_worker(candidate)
        for root, owner, group, mode in ((WORKER_ROOT.parent, boundary.uid, boundary.gid, 0o711),
                (WORKER_ROOT, boundary.uid, boundary.gid, 0o711),
                (CANDIDATE_OUTPUT_ROOT.parent, candidate.uid, candidate.gid, 0o700),
                (CANDIDATE_SOURCE_ROOT, candidate.uid, candidate.gid, 0o700)):
            parent = _open_directory(tuple(root.parts[1:]))
            try:
                info = os.fstat(parent)
                check((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (owner, group, mode),
                      '$.root', 'runtime freeze traversal/home/source identity changed')
            finally:
                os.close(parent)
        build_ids = _inspect_build_inputs(boundary, validator, retained[0], retained[1])
        original = verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots)
        authenticate_tree_private_access(CANDIDATE_OUTPUT_ROOT, owner_uid=candidate.uid,
            owner_gid=candidate.gid, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
        observed = verify_runtime_export(CANDIDATE_OUTPUT_ROOT, plan=retained[0])
        _context(retained[0], retained[1], observed, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(observed['owning_build'] == retained[2], '$.owning_build',
              'candidate runtime differs from original independently selected whole owning Build')
        runtime_raw = canonical_json(observed)
        expected = strict_loads(runtime_raw, label='original candidate runtime lane',
                                max_bytes=limits.MAX_CI_RUNTIME_ENVELOPE_BYTES)

        def close_inputs() -> None:
            check(verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots) == original,
                  '$.source', 'tracked source changed during runtime freeze')
            check(_inspect_build_inputs(boundary, validator, retained[0], retained[1]) == build_ids,
                  '$.input', 'original plan/Build roots changed during runtime freeze')
            check(_original_inputs(plan, build, owning_build, lane_id=lane_id,
                    run_id=run_id, run_attempt=run_attempt) == raw,
                  '$.input', 'caller original runtime freeze inputs changed')

        def before_publish() -> None:
            close_inputs()
            check(verify_runtime_export(CANDIDATE_OUTPUT_ROOT, plan=retained[0]) == expected,
                  '$.runtime', 'candidate runtime changed from original lane before publication')

        copied = _materialize_runtime_export(CANDIDATE_OUTPUT_ROOT, RUNTIME_VALIDATION_ROOT,
                                             plan=retained[0], before_publish=before_publish)
        check(copied == expected, '$.runtime', 'independent runtime copy differs from original lane')
        close_inputs()
        descriptor = _open_directory(tuple(RUNTIME_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        check((initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) == (0, 0, 0o700),
              '$.runtime', 'runtime copy is not fresh private Root-owned')
        admitted = True
        privatize_regular_data_copy(RUNTIME_VALIDATION_ROOT, source_owner_uid=0,
            owner_uid=boundary.uid, owner_gid=boundary.gid, **_bounds(expected, len(runtime_raw)))
        final = os.fstat(descriptor)
        check((final.st_dev, final.st_ino) == (initial.st_dev, initial.st_ino)
              and (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) == (boundary.uid, boundary.gid, 0o700),
              '$.runtime', 'runtime copy identity/private ownership changed')
        reopened = _open_directory(tuple(RUNTIME_VALIDATION_ROOT.parts[1:]))
        try:
            info = os.fstat(reopened)
            check((info.st_dev, info.st_ino) == (initial.st_dev, initial.st_ino),
                  '$.runtime', 'named runtime copy was replaced during ownership transfer')
        finally:
            os.close(reopened)
        authenticate_tree_private_access(RUNTIME_VALIDATION_ROOT, owner_uid=boundary.uid,
            owner_gid=boundary.gid, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
        check(verify_runtime_export(RUNTIME_VALIDATION_ROOT, plan=retained[0]) == expected,
              '$.runtime', 'private runtime bytes changed during ownership transfer')
        close_inputs()
        authenticate_privileged_host_boundary(boundary)
        return expected
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError('runtime freeze could not restore private traversal') from cleanup
        if isinstance(error, OSError):
            raise WorkerError('cannot freeze protected runtime export') from error
        raise
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        except OSError as error:
            raise WorkerError('cannot close protected runtime freeze directory') from error
        finally:
            terminate_worker(candidate)
