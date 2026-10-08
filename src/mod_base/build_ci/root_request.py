"""Private runner-to-root request channel of the closed root operations (MB11).

Each workflow step runs as the runner. Work that needs root runs in one child process, the kit's
``tools/ci_privileged_bootstrap.py``, started through ``sudo`` from the prologue-verified kit
checkout. Parent and child exchange data only here: the runner publishes one canonical request
per operation in its own private directory below the worker root (0700 directory, 0600 single-link
file) and passes the child nothing but the operation name, the kit checkout, its digest and the
request nonce. Root derives the runner from the host, re-reads the request through the private
record reader and admits the live host fence before any operation code runs.

A request is data from the sudo-capable runner. It selects no program, hook or destination and
proves no provenance; every operation re-admits what it touches. Root never calls the GitHub API.
"""

from __future__ import annotations

import copy
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT, ControllerFile, ControllerSources,
                                          _validate_sources, _verify_controller_copy)
from mod_base.build_ci.handoff import _context, _read_private_record
from mod_base.build_ci.host import (HostBoundary, _canonical_path, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary, privileged_runner_identity)
from mod_base.build_ci.inputs import _accounts, _inspect_inputs, _layout
from mod_base.build_ci.root_request_schema import validate_root_request
from mod_base.build_ci.runtime_handoff import _context as _runtime_context
from mod_base.build_ci.runtime_inputs import _inspect_inputs as _inspect_runtime_inputs, _retained
from mod_base.build_ci.source import GitSourceEntry, validate_source_inventory
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account
from mod_base.errors import single_line
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_private_access, authenticate_tree_read_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.pin import Pin


ROOT_PROGRAM = "tools/ci_privileged_bootstrap.py"
_ROOT_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


def root_request_path(operation: str) -> PurePosixPath:
    """The fixed private directory holding the single request of one closed operation."""
    check(type(operation) is str and operation in grammar.CI_ROOT_OPERATIONS,
          "$.operation", "unknown root operation")
    return WORKER_ROOT / f"root-request-{operation}"


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


def _restore_sources(metadata: dict[str, Any], identity: dict[str, Any]) -> ControllerSources:
    """Rebuild the source receipt from the protected controller copy; a request carries no bytes."""
    def file(row: dict[str, Any]) -> ControllerFile:
        # Empty Python source is preserved. Whole-copy metadata/hash verification must still
        # independently read and authenticate that named empty file; nothing is ignored.
        raw = (read_child_file(Path(str(CONTROLLER_VALIDATION_ROOT)), row["path"], max_bytes=row["size"])
               if row["size"] else b"")
        check(len(raw) == row["size"], "$.sources", "controller source size changed")
        return ControllerFile(row["path"], row["mode"], row["git_blob"], row["sha256"], raw)
    sources = ControllerSources(metadata["controller_sha"], metadata["controller_tree"],
                                file(metadata["config"]), tuple(file(row) for row in metadata["files"]))
    _validate_sources(sources, identity)
    return sources


def _publish(operation: str, boundary: HostBoundary, arguments: dict[str, Any], *,
             before_publish: Callable[[], None] | None = None) -> str:
    """Fixed exclusive channel mechanics; closing checks come only from protected kit code."""
    try:
        authenticate_host_boundary(boundary)
        root = Path(str(root_request_path(operation)))
        nonce = os.urandom(32).hex()
        document = {"kind": "mod-base.ci.root-request",
                    "schema_version": SCHEMA_VERSIONS["mod-base.ci.root-request"],
                    "operation": operation, "nonce": nonce, "boundary": asdict(boundary),
                    "arguments": arguments}
        validate_root_request(document)
        raw = canonical_json(document)

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
            if before_publish is not None:
                before_publish()
            authenticate_host_boundary(boundary)
            _layout(boundary)
            check(read_child_file(stage, grammar.CI_ROOT_REQUEST_NAME,
                                 max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES) == raw,
                  "$.request", "root request bytes changed during publication")

        _layout(boundary)
        atomic_directory(root, fill)
        return nonce
    except OSError as error:
        raise WorkerError("cannot publish private root request") from error


def request_host_fence(*, boundary: HostBoundary) -> str:
    """Runner-only request to fence the host image; return the request nonce.

    Made after the runner closed its own home and before any worker account exists. The
    request carries nothing but the home fence receipt.
    """
    return _publish("host-fence", boundary, {})


def request_candidate_staging(*, boundary: HostBoundary, candidate: WorkerAccount, repository: str,
                              tested_sha: str, tested_tree: str, inventory: tuple[GitSourceEntry, ...],
                              source: Path, overlay: Path, pin: Pin, expected_digest: str,
                              gradle_seed: Path | None = None) -> str:
    """Runner-only request to stage the candidate's checkout; return the request nonce.

    Made once, after both accounts were allocated and before the candidate ever runs. The caller
    has authenticated the tested commit and tree, holds the tree's complete inventory and has
    established that ``pin`` is a released kit commit: root repeats none of that, because it
    never calls the API. ``source`` is the runner's checkout of the tested commit, detached at it;
    ``overlay`` a staged kit of ``pin`` with its stamp, of digest ``expected_digest``;
    ``gradle_seed`` an optional restored cache. All three lie below the fenced runner home, where
    root reads them and no worker account reaches them.
    """
    try:
        authenticate_host_boundary(boundary)

        def accounts() -> None:
            check(_accounts(boundary, authenticate_worker_account("validator")) == candidate,
                  "$.candidate", "staging requires the live fixed candidate account")

        accounts()
        validate_source_inventory(inventory)
        if (not isinstance(source, Path) or not isinstance(overlay, Path)
                or not (gradle_seed is None or isinstance(gradle_seed, Path))
                or type(pin) is not Pin or type(pin.version) is not str or not pin.version.startswith("v")):
            raise WorkerError("candidate staging requires protected source paths and a kit pin")
        arguments = {"candidate": {"uid": candidate.uid, "gid": candidate.gid}, "repository": repository,
                     "tested_sha": tested_sha, "tested_tree": tested_tree,
                     "inventory": [{"path": entry.path, "mode": entry.mode, "size": entry.size,
                                    "git_blob": entry.git_blob} for entry in inventory],
                     "source": source.as_posix(),
                     "gradle_seed": None if gradle_seed is None else gradle_seed.as_posix(),
                     "overlay": {"path": overlay.as_posix(), "sha": pin.sha, "version": pin.version[1:],
                                 "tree_digest": expected_digest}}
        return _publish("stage-candidate", boundary, arguments, before_publish=accounts)
    except OSError as error:
        raise WorkerError("cannot publish private root request") from error


def request_build_validation_freeze(*, boundary: HostBoundary, validator: WorkerAccount,
                                    sources: ControllerSources, plan: dict[str, Any], envelope: dict[str, Any],
                                    run_id: int, run_attempt: int, execution_nonce: str) -> str:
    """Runner-only request to seal the Build verifier's receipt; return the request nonce.

    The caller retains the genuine source receipt, plan, frozen Build and the nonce of the
    execution record it published. Source and input directories are inspected before and inside
    publication, so the request describes what root will find.
    """
    try:
        authenticate_host_boundary(boundary)
        _accounts(boundary, validator)
        _context(sources, plan, envelope, run_id, run_attempt)
        arguments = {"validator": {"uid": validator.uid, "gid": validator.gid},
                     "sources": _source_metadata(sources), "plan": copy.deepcopy(plan),
                     "envelope": copy.deepcopy(envelope), "run_id": run_id, "run_attempt": run_attempt,
                     "execution_nonce": execution_nonce}
        initial = (_inspect_sources(boundary, validator, sources, plan),
                   _inspect_inputs(boundary, validator, plan, envelope))

        def closing() -> None:
            check((_inspect_sources(boundary, validator, sources, plan),
                   _inspect_inputs(boundary, validator, plan, envelope)) == initial,
                  "$.request", "root request source/input identities changed")
            _accounts(boundary, validator)

        return _publish("freeze-build-validation", boundary, arguments, before_publish=closing)
    except OSError as error:
        raise WorkerError("cannot publish private root request") from error


def request_runtime_validation_freeze(*, boundary: HostBoundary, validator: WorkerAccount,
                                      sources: ControllerSources, plan: dict[str, Any], build: dict[str, Any],
                                      runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int,
                                      execution_nonce: str) -> str:
    """Runner-only request to seal one runtime lane verifier's receipt; return the request nonce.

    The complete owning Build keeps its own cross-run identity. The caller's plan, Build and
    runtime envelopes are canonicalised once, compared again inside publication, and all three
    read-only input roots are inspected before and inside it.
    """
    try:
        authenticate_host_boundary(boundary)
        _accounts(boundary, validator)
        expected, raw_inputs = _runtime_context(sources, plan, build, runtime,
                                                lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
        retained = _retained(raw_inputs)
        metadata = _source_metadata(sources)
        arguments = {"validator": {"uid": validator.uid, "gid": validator.gid}, "sources": metadata,
                     "plan": retained[0], "build": retained[1], "runtime": retained[2],
                     "lane_id": lane_id, "run_id": run_id, "run_attempt": run_attempt,
                     "execution_nonce": execution_nonce}
        initial = (_inspect_sources(boundary, validator, sources, retained[0]),
                   _inspect_runtime_inputs(boundary, validator, *retained))

        def closing() -> None:
            check((_inspect_sources(boundary, validator, sources, retained[0]),
                   _inspect_runtime_inputs(boundary, validator, *retained)) == initial,
                  "$.request", "runtime root request source/input directories changed")
            again, again_inputs = _runtime_context(sources, plan, build, runtime,
                                                   lane_id=lane_id, run_id=run_id, run_attempt=run_attempt)
            check(again == expected and again_inputs == raw_inputs and _source_metadata(sources) == metadata,
                  "$.request", "caller runtime root context changed during publication")
            _accounts(boundary, validator)

        return _publish("freeze-runtime-validation", boundary, arguments, before_publish=closing)
    except OSError as error:
        raise WorkerError("cannot publish private runtime root request") from error


def read_root_request(operation: str, *, nonce: str) -> tuple[HostBoundary, dict[str, Any], bytes]:
    """Root-only: admit the fixed runner-origin request of one operation and the live host fence.

    The runner identity comes from the fixed home and passwd. The request must be the canonical
    private record of exactly this operation and nonce, and its host boundary must be the live
    one. Returns the boundary, the closed arguments and the record bytes for a closing re-read.
    """
    try:
        uid, gid = privileged_runner_identity()
        root = Path(str(root_request_path(operation)))
        grammar.require(grammar.SHA256, nonce, "root request nonce")
        authenticate_tree_private_access(root, owner_uid=uid, owner_gid=gid,
                                         max_entries=limits.MAX_CI_PRIVATE_RECORD_ENTRIES)
        raw = _read_private_record(root, name=grammar.CI_ROOT_REQUEST_NAME, owner_uid=uid, owner_gid=gid,
                                   max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES, label="root request")
        document = loads(raw, label=grammar.CI_ROOT_REQUEST_NAME, max_bytes=limits.MAX_CI_ROOT_REQUEST_BYTES)
        validate_root_request(document)
        check(raw == canonical_json(document) and document["nonce"] == nonce
              and document["operation"] == operation,
              "$.request", "root request is noncanonical or names another operation or entry nonce")
        boundary = HostBoundary(**document["boundary"])
        check((boundary.uid, boundary.gid) == (uid, gid), "$.request.boundary",
              "root request names another runner than the owner of the fixed home")
        authenticate_privileged_host_boundary(boundary)
        _layout(boundary)
        return boundary, document["arguments"], raw
    except OSError as error:
        raise WorkerError("cannot read private root request") from error


def run_root_operation(operation: str, *, python: str, kit_root: Path, kit_digest: str, nonce: str) -> None:
    """Runner-only: run the kit's root bootstrap for one published request and require success.

    The child is ``sudo -n -- <python> -I -B -S <kit>/tools/ci_privileged_bootstrap.py`` with a
    closed argument vector, a fixed environment and no inherited descriptors. The kit checkout
    and digest must be the ones the job prologue verified; the bootstrap re-computes the digest
    before importing anything from it. A non-zero exit, a timeout or a signal is a failure.
    """
    if os.getuid() == 0 or os.geteuid() == 0:
        raise WorkerError("root operations are launched by the unprivileged runner")
    root_request_path(operation)
    _canonical_path(python)
    grammar.require(grammar.DIGEST, kit_digest, "kit digest")
    grammar.require(grammar.SHA256, nonce, "root request nonce")
    if not isinstance(kit_root, Path):
        raise WorkerError("kit checkout must be a protected path")
    kit = _canonical_path(kit_root.as_posix())
    command = ("/usr/bin/sudo", "-n", "--", python, "-I", "-B", "-S", str(kit / ROOT_PROGRAM),
               "--operation", operation, "--kit", str(kit), "--kit-digest", kit_digest, "--nonce", nonce)
    process = None
    diagnostic = bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, env=dict(_ROOT_ENV), cwd=Path(str(WORKER_ROOT)),
                                   close_fds=True)
        if process.stderr is None:
            raise WorkerError("root operation diagnostic pipe unavailable")
        deadline = time.monotonic() + limits.CI_ROOT_OPERATION_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stderr, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorkerError(f"root operation {operation} timed out")
                chunk = os.read(process.stderr.fileno(), limits.CI_PROCESS_READ_BYTES)
                if not chunk:
                    break
                # Keep the first bounded lines and keep draining: the child never blocks on us.
                diagnostic.extend(chunk[:limits.MAX_CI_ROOT_DIAGNOSTIC_BYTES - len(diagnostic)])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerError(f"root operation {operation} timed out")
        code = process.wait(timeout=remaining)
        if code != 0:
            detail = single_line(diagnostic.decode("utf-8", "replace"), limit=600)
            raise WorkerError(f"root operation {operation} failed with exit {code}: {detail}")
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerError(f"root operation {operation} could not complete") from error
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    # sudo relays SIGTERM to the root child; the bootstrap also carries its own alarm.
                    process.send_signal(signal.SIGTERM)
                    try:
                        process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        process.kill()
                process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError) as error:
                raise WorkerError("root operation process did not reap") from error
            finally:
                if process.stderr is not None:
                    process.stderr.close()
