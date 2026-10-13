"""Runtime handoffs between the validator, the runner and root (MB11).

``derive_runtime`` answers in the validator's own home; root hands the runner a private copy of
the one file it must write (:func:`take_derived_runtime`), exactly as it does for ``derive_plan``,
and so it does for ``derive_scope`` in a packaged ``input`` job (:func:`take_derived_scope`).
The rest is the runner-to-root execution context of a lane's verifier, on the existing private
channel.
"""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci import adapter
from mod_base.build_ci.controller import ControllerSources, _validate_sources
from mod_base.build_ci.handoff import (_log, _publish_execution, _read_private_handoff,
                                      validate_execution_handoff)
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.inputs import _accounts, _discard, _layout
from mod_base.build_ci.runtime_inputs import (RuntimeValidationExecution, _context as _input_context,
    _inspect_inputs, _read_inputs, _retained, freeze_frozen_runtime_validation)
from mod_base.build_ci.validation import VALIDATOR_OUTPUT_ROOT
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult, terminate_worker
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import authenticate_tree_private_access, copy_regular_files, file_records, privatize_tree_copy
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import check


#: The runner's private copy of what ``derive_runtime`` wrote, as root handed it over.
DERIVED_RUNTIME_ROOT = WORKER_ROOT / "derived-runtime"
#: The runner's private copy of what ``derive_scope`` wrote, as root handed it over.
DERIVED_SCOPE_ROOT = WORKER_ROOT / "derived-scope"


def take_derived_runtime(*, boundary: HostBoundary, validator: WorkerAccount) -> None:
    """Root-only: give the runner a private copy of what ``derive_runtime`` wrote and remove the original.

    The validator is terminated first. Its output directory must be exactly the one file the
    contract names: private, regular, single-linked, within the report cap. Anything else (a
    missing or an extra file, a link, another mode) fails and hands nothing over. The copy has its
    own inodes, in the fixed runner-private :data:`DERIVED_RUNTIME_ROOT`, which must not exist yet:
    a job derives the runtime values of one lane, once. The original is never chowned; it is
    removed, so that the lane's verifier finds no output of this hook.
    """

    _take_derived_output(boundary, validator, hook="derive_runtime", name=adapter.RUNTIME_OUTPUT,
                         output=Path(str(DERIVED_RUNTIME_ROOT)), max_bytes=limits.MAX_CI_REPORT_BYTES)


def take_derived_scope(*, boundary: HostBoundary, validator: WorkerAccount) -> None:
    """Root-only: give the runner a private copy of what ``derive_scope`` wrote and remove the original.

    Exactly :func:`take_derived_runtime` for the one file of ``derive_scope``, within the scope
    record cap, in the fixed runner-private :data:`DERIVED_SCOPE_ROOT`: the packaged ``input`` job
    of a pull request derives its scope once (``build_ci.scope``)."""

    _take_derived_output(boundary, validator, hook="derive_scope", name=adapter.SCOPE_OUTPUT,
                         output=Path(str(DERIVED_SCOPE_ROOT)), max_bytes=limits.MAX_CI_SCOPE_BYTES)


def _take_derived_output(boundary: HostBoundary, validator: WorkerAccount, *, hook: str, name: str, output: Path,
                         max_bytes: int) -> None:
    """Root-only: hand the runner a private copy of the one file ``name`` a derivation hook left in
    the validator's ``validation/`` at the new directory ``output``, and remove the original."""

    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    names = (name,)
    bounds = dict(max_files=len(names), max_entries=len(names) + 1, max_total_bytes=max_bytes,
                  max_file_bytes=max_bytes)
    try:
        terminate_worker(validator)
        _layout(boundary)
        home = _open_directory(tuple(VALIDATOR_OUTPUT_ROOT.parent.parts[1:]))
        try:
            info = os.fstat(home)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (validator.uid, validator.gid, 0o700):
                raise WorkerError(f"validator home identity changed before the output of {hook} was taken")
        finally:
            os.close(home)
        authenticate_tree_private_access(VALIDATOR_OUTPUT_ROOT, owner_uid=validator.uid,
                                         owner_gid=validator.gid, max_entries=bounds["max_entries"])

        def writer(stage: Path, stage_fd: int) -> list[dict[str, Any]]:
            copied = copy_regular_files(VALIDATOR_OUTPUT_ROOT, stage_fd, **bounds)
            check([record["path"] for record in copied] == list(names), "$.derivation",
                  f"{hook} must leave exactly its one output file")
            return copied

        copied = atomic_directory(output, writer)
        privatize_tree_copy(output, source_owner_uid=0, owner_uid=boundary.uid, owner_gid=boundary.gid, **bounds)
        authenticate_tree_private_access(output, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                         max_entries=bounds["max_entries"])
        check(file_records(output, max_files=bounds["max_files"], max_total_bytes=bounds["max_total_bytes"],
                           max_file_bytes=bounds["max_file_bytes"]) == copied,
              "$.derivation", f"the output of {hook} changed during the ownership transfer")
        _discard(VALIDATOR_OUTPUT_ROOT, names)
        authenticate_privileged_host_boundary(boundary)
    except OSError as error:
        raise WorkerError(f"cannot take the output of {hook} from the validator") from error
    finally:
        terminate_worker(validator)


def _context(sources: ControllerSources, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], *,
             lane_id: str, run_id: int, run_attempt: int) -> tuple[dict[str, Any], tuple[bytes, ...]]:
    digest, raw = _input_context(plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
    config = _validate_sources(sources, plan['identity'])
    check(config['profile'] == plan['profile'], '$.profile', 'runtime handoff controller profile differs')
    return {'run_id': run_id, 'run_attempt': run_attempt, 'plan_sha256': plan['plan_sha256'],
            'source_config_sha256': sources.config.sha256, 'input_sha256': digest}, raw


def record_runtime_validation_execution(*, boundary: HostBoundary, validator: WorkerAccount,
                                         sources: ControllerSources, bound: RuntimeValidationExecution,
                                         plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any],
                                         lane_id: str, run_id: int, run_attempt: int) -> str:
    """Runner-only exclusive publication of genuinely retained successful runtime execution.

    Caller owns original protected execution/source/API/tool/runtime provenance. Constructible
    bound objects cannot establish it. Preserve existing execution-v1 fields/bounds; no record
    field selects a path/hook/program. Existing channels are never overwritten or reused.
    """
    try:
        authenticate_host_boundary(boundary)
        _accounts(boundary, validator)
    except OSError as error:
        raise WorkerError('cannot authenticate runtime execution handoff runner') from error
    try:
        expected, raw = _context(sources, plan, build, runtime, lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(type(bound) is RuntimeValidationExecution and type(bound.input_sha256) is str
              and bound.input_sha256 == expected['input_sha256'],
              '$.execution', 'runtime execution differs from original protected inputs')
        result = bound.execution
        check(type(result) is WorkerResult and type(result.returncode) is int and result.returncode == 0
              and type(result.log) is bytes and len(result.log) <= limits.MAX_CI_LOG_BYTES
              and type(result.truncated) is bool,
              '$.execution', 'runtime handoff requires successful bounded execution')
        retained = _retained(raw)
        terminate_worker(validator)
        initial = _read_inputs(boundary, validator, *retained)
        nonce = os.urandom(32).hex()
        document = {'kind': 'mod-base.ci.execution', 'schema_version': max(readable_schema_versions('mod-base.ci.execution')),
                    **expected, 'nonce': nonce, 'returncode': result.returncode, 'truncated': result.truncated,
                    'log_base64': base64.b64encode(result.log).decode('ascii')}

        def before_publish() -> None:
            check(_read_inputs(boundary, validator, *retained) == initial,
                  '$.input', 'original runtime handoff input directories changed')
            closing, closing_raw = _context(sources, plan, build, runtime,
                lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
            check(closing == expected and closing_raw == raw,
                  '$.input', 'caller original runtime execution context changed during publication')

        return _publish_execution(boundary, document, before_publish=before_publish)
    except OSError as error:
        raise WorkerError('cannot publish private runtime execution handoff') from error
    finally:
        terminate_worker(validator)


def freeze_handed_off_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                         sources: ControllerSources, plan: dict[str, Any],
                                         build: dict[str, Any], runtime: dict[str, Any], lane_id: str,
                                         run_id: int, run_attempt: int, nonce: str) -> dict[str, Any]:
    """Root-only physical channel/context admission around original runtime receipt freezing.

    The fixed Root program and context require independent protected enrollment. Private runner
    bytes prove no native/execution/source provenance or upload/status authority by themselves.
    Closing failure may leave a private receipt; never consume or upload after failure.
    """
    try:
        authenticate_privileged_host_boundary(boundary)
        _accounts(boundary, validator)
    except OSError as error:
        raise WorkerError('cannot authenticate runtime execution handoff Root context') from error
    try:
        expected, raw_inputs = _context(sources, plan, build, runtime,
            lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        grammar.require(grammar.SHA256, nonce, 'original runtime execution nonce')
        retained = _retained(raw_inputs)
        terminate_worker(validator)
        _layout(boundary)
        initial = _inspect_inputs(boundary, validator, *retained)
        raw = _read_private_handoff(boundary)
        document = strict_loads(raw, label=grammar.CI_EXECUTION_NAME, max_bytes=limits.MAX_CI_EXECUTION_BYTES)
        validate_execution_handoff(document)
        check(raw == canonical_json(document), '$.handoff', 'runtime execution handoff must be canonical JSON')
        check(document['nonce'] == nonce and all(document[key] == value for key, value in expected.items()),
              '$.handoff', 'runtime execution handoff differs from original attempt/source/input context')
        bound = RuntimeValidationExecution(WorkerResult(document['returncode'], _log(document), document['truncated']),
                                            document['input_sha256'])
        authenticate_privileged_host_boundary(boundary)
        receipt = freeze_frozen_runtime_validation(boundary=boundary, validator=validator, sources=sources,
            bound=bound, plan=retained[0], build=retained[1], runtime=retained[2], lane_id=lane_id,
            run_id=run_id, run_attempt=run_attempt)
        check(_read_private_handoff(boundary) == raw,
              '$.handoff', 'original private runtime execution record changed during receipt freeze')
        check(_inspect_inputs(boundary, validator, *retained) == initial,
              '$.input', 'original runtime handoff input directories changed during receipt freeze')
        closing, closing_raw = _context(sources, plan, build, runtime,
            lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        check(closing == expected and closing_raw == raw_inputs,
              '$.input', 'caller original runtime execution context changed during receipt freeze')
        authenticate_privileged_host_boundary(boundary)
        return receipt
    except OSError as error:
        raise WorkerError('cannot read protected runtime execution handoff') from error
    finally:
        terminate_worker(validator)
