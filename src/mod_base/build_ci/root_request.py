"""Private protected-runner context channel for fixed root Build receipt sealing (MB11).

Caller retains genuine source/plan/execution provenance and excludes competing trusted writers.
Root reading reconstructs bytes only from the fixed protected copy; it imports no domain code.
Independent bootstrap/pin/interpreter enrollment and native/final authority remain prerequisites.
"""

from __future__ import annotations

import copy
import hashlib
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT, ControllerFile, ControllerSources,
                                          _validate_sources, _verify_controller_copy)
from mod_base.build_ci.handoff import _context, _read_private_record, freeze_handed_off_build_validation
from mod_base.build_ci.host import (HostBoundary, _canonical_path, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.inputs import _accounts, _inspect_inputs, _layout
from mod_base.build_ci.installation_record import read_privileged_kit_installation
from mod_base.build_ci.installation import PRIVILEGED_KIT_ROOT
from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, terminate_worker
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_private_access, authenticate_tree_read_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.config import DEFAULT_CONFIG_PATH
from mod_base.runtime import Invocation, build_invocation


ROOT_REQUEST_ROOT = WORKER_ROOT / "root-request"


def build_root_freeze_invocation(*, boundary: HostBoundary, controller_root: str,
                                 repository: str, controller_sha: str, kit_sha: str) -> Invocation:
    """Closed composition root after independent kit loading; controller configuration is data.

    The protected caller independently admits this checkout and executing identities. This
    function does not import its adapters, check out Git or derive approval from filesystem IDs.
    """
    try:
        authenticate_privileged_host_boundary(boundary)
        grammar.require(grammar.REPOSITORY, repository, "controller repository")
        grammar.require_sha1(controller_sha, "controller SHA")
        grammar.require_sha1(kit_sha, "kit SHA")
        path = _canonical_path(controller_root)
        check(_canonical_path(boundary.home) in path.parents, "$.controller",
              "controller configuration must be behind the runner home fence")
        root = Path(str(path))

        def identity() -> tuple[int, int]:
            descriptor = _open_directory(tuple(path.parts[1:]))
            try:
                info = os.fstat(descriptor)
                check(stat.S_ISDIR(info.st_mode) and info.st_uid == boundary.uid,
                      "$.controller", "controller configuration root must be runner-owned")
                return info.st_dev, info.st_ino
            finally:
                os.close(descriptor)

        initial = identity()
        installed = read_privileged_kit_installation(boundary=boundary)
        check(installed.kit_sha == kit_sha, "$.kit", "composition root names another installed kit")
        raw = read_child_file(root, DEFAULT_CONFIG_PATH, max_bytes=limits.MAX_CONFIG_BYTES)
        invocation = build_invocation(root, None,
            {"GITHUB_REPOSITORY": repository, "GITHUB_SHA": controller_sha, "MOD_BASE_KIT_SHA": kit_sha},
            check_repository=False, root=Path(str(PRIVILEGED_KIT_ROOT)))
        check(invocation.config.sha256 == hashlib.sha256(raw).hexdigest()
              and invocation.kit["version"] == installed.kit_version,
              "$.controller", "executing configuration or kit version changed")
        check(read_child_file(root, DEFAULT_CONFIG_PATH, max_bytes=limits.MAX_CONFIG_BYTES) == raw
              and identity() == initial, "$.controller", "controller configuration changed during composition")
        authenticate_privileged_host_boundary(boundary)
        return invocation
    except OSError as error:
        raise WorkerError("cannot compose protected root invocation") from error


@dataclass(frozen=True)
class RootFreezeContext:
    sources: ControllerSources
    plan: dict[str, Any]
    envelope: dict[str, Any]
    run_id: int
    run_attempt: int
    execution_nonce: str


def _invocation(invocation: Invocation, identity: dict[str, Any]) -> None:
    check(type(invocation) is Invocation, "$.invocation", "requires a protected validated invocation")
    check(invocation.repository == identity["repository"]
          and invocation.implementation_sha == identity["controller_sha"]
          and invocation.kit == {key: value for key, value in identity["kit"].items() if key != "tree_digest"},
          "$.invocation", "root request differs from executing controller/kit invocation")


def _source_metadata(sources: ControllerSources) -> dict[str, Any]:
    def row(file: ControllerFile) -> dict[str, Any]:
        return {"path": file.path, "mode": file.mode, "git_blob": file.git_blob,
                "sha256": file.sha256, "size": len(file.data)}
    return {"controller_sha": sources.controller_sha, "controller_tree": sources.controller_tree,
            "config": row(sources.config), "files": [row(file) for file in sources.files]}


def _source_root_identity() -> tuple[int, int]:
    fd = _open_directory(tuple(CONTROLLER_VALIDATION_ROOT.parts[1:]))
    try:
        info = os.fstat(fd)
        return info.st_dev, info.st_ino
    finally:
        os.close(fd)


def _inspect_sources(boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources,
                     plan: dict[str, Any]) -> tuple[int, int]:
    _layout(boundary)
    initial = _source_root_identity()
    authenticate_tree_read_access(CONTROLLER_VALIDATION_ROOT, owner_uid=boundary.uid,
                                  reader_gid=validator.gid, max_entries=limits.MAX_CI_SOURCE_ENTRIES)
    config = _verify_controller_copy(Path(str(CONTROLLER_VALIDATION_ROOT)), sources=sources,
                                     identity=plan["identity"], read_only=True)
    check(config["profile"] == plan["profile"], "$.sources", "root source profile differs")
    check(_source_root_identity() == initial, "$.sources", "controller copy root changed during admission")
    return initial


def _restore_sources(document: dict[str, Any]) -> ControllerSources:
    metadata = document["sources"]
    def file(row: dict[str, Any]) -> ControllerFile:
        # Empty Python source is preserved. Whole-copy metadata/hash verification below must
        # still independently read and authenticate that named empty file; nothing is ignored.
        raw = (read_child_file(Path(str(CONTROLLER_VALIDATION_ROOT)), row["path"], max_bytes=row["size"])
               if row["size"] else b"")
        check(len(raw) == row["size"], "$.sources", "controller source size changed")
        return ControllerFile(row["path"], row["mode"], row["git_blob"], row["sha256"], raw)
    sources = ControllerSources(metadata["controller_sha"], metadata["controller_tree"],
                                file(metadata["config"]), tuple(file(row) for row in metadata["files"]))
    _validate_sources(sources, document["plan"]["identity"])
    return sources


def _read(boundary: HostBoundary) -> bytes:
    root = Path(str(ROOT_REQUEST_ROOT))
    authenticate_tree_private_access(root, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                     max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
    return _read_private_record(root, name=grammar.CI_ROOT_REQUEST_NAME, owner_uid=boundary.uid,
                owner_gid=boundary.gid, max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES, label="root request")


def record_root_freeze_request(invocation: Invocation, *, boundary: HostBoundary, validator: WorkerAccount,
                               sources: ControllerSources, plan: dict[str, Any], envelope: dict[str, Any],
                               run_id: int, run_attempt: int, execution_nonce: str) -> str:
    """Runner-only exclusive publication of genuine retained context; return a separate fresh nonce."""
    try:
        authenticate_host_boundary(boundary)
        _accounts(boundary, validator)
        _context(sources, plan, envelope, run_id, run_attempt)
        _invocation(invocation, plan["identity"])
        nonce = os.urandom(32).hex()
        document = {"kind": "mod-base.ci.root-request", "schema_version": SCHEMA_VERSIONS["mod-base.ci.root-request"],
            "nonce": nonce, "execution_nonce": execution_nonce, "boundary": asdict(boundary),
            "validator": {"uid": validator.uid, "gid": validator.gid}, "sources": _source_metadata(sources),
            "plan": copy.deepcopy(plan), "envelope": copy.deepcopy(envelope), "run_id": run_id, "run_attempt": run_attempt}
        validate_root_request(document)
        raw = canonical_json(document)
        initial = (_inspect_sources(boundary, validator, sources, plan), _inspect_inputs(boundary, validator, plan, envelope))

        def fill(stage: Path, descriptor: int) -> None:
            authenticate_tree_private_access(stage, owner_uid=boundary.uid, owner_gid=boundary.gid, max_entries=1)
            write_new(descriptor, grammar.CI_ROOT_REQUEST_NAME, raw)
            leaf = os.open(grammar.CI_ROOT_REQUEST_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                os.fchmod(leaf, 0o600)
                os.fsync(leaf)
            finally:
                os.close(leaf)
            authenticate_tree_private_access(stage, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                             max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
            check(read_child_file(stage, grammar.CI_ROOT_REQUEST_NAME, max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES) == raw,
                  "$.request", "root request bytes changed during publication")
            check((_inspect_sources(boundary, validator, sources, plan), _inspect_inputs(boundary, validator, plan, envelope)) == initial,
                  "$.request", "root request source/input identities changed")
            _accounts(boundary, validator)
            authenticate_host_boundary(boundary)
            _layout(boundary)

        _layout(boundary)
        atomic_directory(Path(str(ROOT_REQUEST_ROOT)), fill)
        return nonce
    except OSError as error:
        raise WorkerError("cannot publish private root request") from error


def read_root_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                             validator: WorkerAccount, nonce: str) -> RootFreezeContext:
    """Root-only admit fixed runner-origin request and reconstruct actual protected copy bytes."""
    try:
        authenticate_privileged_host_boundary(boundary)
        _accounts(boundary, validator)
        grammar.require(grammar.SHA256, nonce, "root request nonce")
        _layout(boundary)
        raw = _read(boundary)
        document = loads(raw, label=grammar.CI_ROOT_REQUEST_NAME, max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES)
        validate_root_request(document)
        check(raw == canonical_json(document) and document["nonce"] == nonce,
              "$.request", "root request is noncanonical or has another entry nonce")
        check(document["boundary"] == asdict(boundary)
              and document["validator"] == {"uid": validator.uid, "gid": validator.gid},
              "$.request", "root request host/validator identities changed")
        plan, envelope = document["plan"], document["envelope"]
        _invocation(invocation, plan["identity"])
        installed = read_privileged_kit_installation(boundary=boundary)
        check(plan["identity"]["kit"] == {"repository": invocation.kit["repository"], "sha": installed.kit_sha,
            "version": installed.kit_version, "tree_digest": installed.digest}, "$.kit", "root request differs from admitted kit copy")
        initial = _source_root_identity()
        authenticate_tree_read_access(CONTROLLER_VALIDATION_ROOT, owner_uid=boundary.uid,
                                      reader_gid=validator.gid, max_entries=limits.MAX_CI_SOURCE_ENTRIES)
        sources = _restore_sources(document)
        check(_inspect_sources(boundary, validator, sources, plan) == initial,
              "$.sources", "controller copy root replaced during reconstruction")
        inputs = _inspect_inputs(boundary, validator, plan, envelope)
        _context(sources, plan, envelope, document["run_id"], document["run_attempt"])
        check(_read(boundary) == raw and _inspect_sources(boundary, validator, sources, plan) == initial
              and _inspect_inputs(boundary, validator, plan, envelope) == inputs,
              "$.request", "root context changed during physical admission")
        _accounts(boundary, validator)
        authenticate_privileged_host_boundary(boundary)
        return RootFreezeContext(sources, plan, envelope, document["run_id"], document["run_attempt"], document["execution_nonce"])
    except OSError as error:
        raise WorkerError("cannot read private root request") from error


def freeze_root_requested_build_validation(invocation: Invocation, *, boundary: HostBoundary,
                                           validator: WorkerAccount, nonce: str) -> dict[str, Any]:
    """Fixed root operation: admit physical context, seal retained execution and recheck context."""
    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    try:
        context = read_root_freeze_request(invocation, boundary=boundary, validator=validator, nonce=nonce)
        receipt = freeze_handed_off_build_validation(boundary=boundary, validator=validator,
            sources=context.sources, plan=context.plan, envelope=context.envelope,
            run_id=context.run_id, run_attempt=context.run_attempt, nonce=context.execution_nonce)
        check(read_root_freeze_request(invocation, boundary=boundary, validator=validator, nonce=nonce) == context,
              "$.request", "root context changed during receipt sealing")
        return receipt
    finally:
        terminate_worker(validator)
