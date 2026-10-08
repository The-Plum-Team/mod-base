"""Fixed private runtime Root request channel and sealing composition (MB11).

Caller retains original source/API/execution provenance and excludes trusted writers.
Independent program/interpreter/caller enrollment and hosted/native authority remain required.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.controller import CONTROLLER_VALIDATION_ROOT, ControllerSources
from mod_base.build_ci.handoff import _read_private_record
from mod_base.build_ci.host import HostBoundary, authenticate_host_boundary, authenticate_privileged_host_boundary
from mod_base.build_ci.inputs import _accounts, _layout
from mod_base.build_ci.installation_record import read_privileged_kit_installation
from mod_base.build_ci.root_request import (_inspect_sources, _invocation, _restore_sources,
                                          _source_metadata, _source_root_identity)
from mod_base.build_ci.runtime_handoff import _context, freeze_handed_off_runtime_validation
from mod_base.build_ci.runtime_inputs import _inspect_inputs, _retained
from mod_base.build_ci.runtime_root_request_schema import validate_runtime_root_request
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, terminate_worker
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_private_access, authenticate_tree_read_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.runtime import Invocation


RUNTIME_ROOT_REQUEST_ROOT = WORKER_ROOT / 'runtime-root-request'


@dataclass(frozen=True)
class RuntimeRootFreezeContext:
    sources: ControllerSources
    plan: dict[str, Any]
    build: dict[str, Any]
    runtime: dict[str, Any]
    lane_id: str
    run_id: int
    run_attempt: int
    execution_nonce: str


def _read(boundary: HostBoundary) -> bytes:
    root = Path(str(RUNTIME_ROOT_REQUEST_ROOT))
    authenticate_tree_private_access(root, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                     max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
    return _read_private_record(root, name=grammar.CI_RUNTIME_ROOT_REQUEST_NAME, owner_uid=boundary.uid,
        owner_gid=boundary.gid, max_bytes=limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES, label='runtime Root request')


def record_runtime_root_freeze_request(invocation: Invocation, *, boundary: HostBoundary, validator: WorkerAccount,
                                       sources: ControllerSources, plan: dict[str, Any], build: dict[str, Any],
                                       runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int,
                                       execution_nonce: str) -> str:
    """Runner-only exclusive private publication; original context gets a distinct fresh entry nonce."""
    try:
        authenticate_host_boundary(boundary)
        _accounts(boundary, validator)
        expected, raw_inputs = _context(sources, plan, build, runtime,
            lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        retained = _retained(raw_inputs)
        _invocation(invocation, retained[0]['identity'])
        metadata = _source_metadata(sources)
        nonce = os.urandom(32).hex()
        document = {'kind': 'mod-base.ci.runtime-root-request',
            'schema_version': SCHEMA_VERSIONS['mod-base.ci.runtime-root-request'],
            'nonce': nonce, 'execution_nonce': execution_nonce, 'boundary': asdict(boundary),
            'validator': {'uid': validator.uid, 'gid': validator.gid}, 'sources': metadata,
            'plan': retained[0], 'build': retained[1], 'runtime': retained[2],
            'lane_id': lane_id, 'run_id': run_id, 'run_attempt': run_attempt}
        validate_runtime_root_request(document)
        raw = canonical_json(document)
        initial = (_inspect_sources(boundary, validator, sources, retained[0]),
                   _inspect_inputs(boundary, validator, *retained))

        def fill(stage: Path, descriptor: int) -> None:
            authenticate_tree_private_access(stage, owner_uid=boundary.uid, owner_gid=boundary.gid, max_entries=1)
            write_new(descriptor, grammar.CI_RUNTIME_ROOT_REQUEST_NAME, raw)
            leaf = os.open(grammar.CI_RUNTIME_ROOT_REQUEST_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                os.fchmod(leaf, 0o600)
                os.fsync(leaf)
            finally:
                os.close(leaf)
            authenticate_tree_private_access(stage, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                             max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            check((_inspect_sources(boundary, validator, sources, retained[0]),
                   _inspect_inputs(boundary, validator, *retained)) == initial,
                  '$.request', 'original runtime Root source/input directories changed')
            closing, closing_inputs = _context(sources, plan, build, runtime,
                lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
            check(closing == expected and closing_inputs == raw_inputs and _source_metadata(sources) == metadata,
                  '$.request', 'original caller runtime Root context changed during publication')
            _invocation(invocation, retained[0]['identity'])
            _accounts(boundary, validator)
            authenticate_host_boundary(boundary)
            _layout(boundary)
            check(read_child_file(stage, grammar.CI_RUNTIME_ROOT_REQUEST_NAME,
                                 max_bytes=limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES) == raw,
                  '$.request', 'runtime Root request bytes changed during closing publication')

        _layout(boundary)
        atomic_directory(Path(str(RUNTIME_ROOT_REQUEST_ROOT)), fill)
        return nonce
    except OSError as error:
        raise WorkerError('cannot publish private runtime Root request') from error


def read_runtime_root_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                                     validator: WorkerAccount, nonce: str) -> RuntimeRootFreezeContext:
    """Root-only fixed channel admission and protected-copy reconstruction; import no domain code."""
    try:
        authenticate_privileged_host_boundary(boundary)
        _accounts(boundary, validator)
        grammar.require(grammar.SHA256, nonce, 'runtime Root request nonce')
        _layout(boundary)
        raw = _read(boundary)
        document = loads(raw, label=grammar.CI_RUNTIME_ROOT_REQUEST_NAME,
                         max_bytes=limits.MAX_CI_RUNTIME_ROOT_REQUEST_BYTES)
        validate_runtime_root_request(document)
        check(raw == canonical_json(document) and document['nonce'] == nonce,
              '$.request', 'runtime Root request is noncanonical or has another entry nonce')
        check(document['boundary'] == asdict(boundary)
              and document['validator'] == {'uid': validator.uid, 'gid': validator.gid},
              '$.request', 'runtime Root host/validator identities changed')
        plan, build, runtime = document['plan'], document['build'], document['runtime']
        _invocation(invocation, plan['identity'])
        installed = read_privileged_kit_installation(boundary=boundary)
        check(plan['identity']['kit'] == {'repository': invocation.kit['repository'], 'sha': installed.kit_sha,
            'version': installed.kit_version, 'tree_digest': installed.digest},
            '$.kit', 'runtime Root context differs from admitted kit copy')
        initial = _source_root_identity()
        authenticate_tree_read_access(CONTROLLER_VALIDATION_ROOT, owner_uid=boundary.uid,
                                      reader_gid=validator.gid, max_entries=limits.MAX_CI_SOURCE_ENTRIES)
        sources = _restore_sources(document)
        check(_inspect_sources(boundary, validator, sources, plan) == initial,
              '$.sources', 'runtime controller copy replaced during reconstruction')
        inputs = _inspect_inputs(boundary, validator, plan, build, runtime)
        _context(sources, plan, build, runtime, lane_id=document['lane_id'],
                 run_id=document['run_id'], run_attempt=document['run_attempt'])
        check(_read(boundary) == raw and _inspect_sources(boundary, validator, sources, plan) == initial
              and _inspect_inputs(boundary, validator, plan, build, runtime) == inputs,
              '$.request', 'original runtime Root request/source/input context changed during admission')
        _accounts(boundary, validator)
        authenticate_privileged_host_boundary(boundary)
        return RuntimeRootFreezeContext(sources, plan, build, runtime, document['lane_id'],
            document['run_id'], document['run_attempt'], document['execution_nonce'])
    except OSError as error:
        raise WorkerError('cannot read private runtime Root request') from error


def _signature(context: RuntimeRootFreezeContext) -> tuple[Any, ...]:
    return (context.sources, *(canonical_json(value) for value in (context.plan, context.build, context.runtime)),
            context.lane_id, context.run_id, context.run_attempt, context.execution_nonce)


def freeze_root_requested_runtime_validation(invocation: Invocation, *, boundary: HostBoundary,
                                              validator: WorkerAccount, nonce: str) -> dict[str, Any]:
    """Fixed Root operation with original request re-admission around runtime receipt sealing."""
    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    try:
        context = read_runtime_root_freeze_request(invocation, boundary=boundary, validator=validator, nonce=nonce)
        original = _signature(context)
        receipt = freeze_handed_off_runtime_validation(boundary=boundary, validator=validator, sources=context.sources,
            plan=context.plan, build=context.build, runtime=context.runtime, lane_id=context.lane_id,
            run_id=context.run_id, run_attempt=context.run_attempt, nonce=context.execution_nonce)
        closing = read_runtime_root_freeze_request(invocation, boundary=boundary, validator=validator, nonce=nonce)
        check(_signature(closing) == original and _signature(context) == original,
              '$.request', 'original runtime Root context changed during receipt sealing')
        authenticate_privileged_host_boundary(boundary)
        return receipt
    finally:
        terminate_worker(validator)
