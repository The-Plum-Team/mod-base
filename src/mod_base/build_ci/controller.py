"""Inert protected-controller import-source admission; never imports candidate modules (MB11)."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import authenticate_source_identity
from mod_base.build_ci.activation import ACTIVATION_PATH, validate_activation
from mod_base.build_ci.config import BUILD_CONFIG_PATH, validate_build_config
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.protocol import check_output_paths, validate_identity, validate_plan
from mod_base.build_ci.source import GitSourceEntry, SourceError, verify_source_copy
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_worker_account, terminate_worker)
from mod_base.build_ci.toolchain import (ToolBytesProof, ToolTreeProof, execute_byte_fenced_worker,
                                         execute_tool_fenced_worker)
from mod_base.github.api import GitHubApi
from mod_base.github.contents import blob, commit_tree, exact_tree
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.secure_json import loads
from mod_base.io.tree import authenticate_tree_read_access, grant_source_read_access
from mod_base.model import grammar, limits


CONTROLLER_VALIDATION_ROOT = WORKER_ROOT / "controller"
VALIDATOR_HOOKS = frozenset({"verify_target", "verify_build", "verify_runtime"})


@dataclass(frozen=True)
class ControllerFile:
    path: str
    mode: str
    git_blob: str
    sha256: str
    data: bytes


@dataclass(frozen=True)
class ControllerSources:
    controller_sha: str
    controller_tree: str
    config: ControllerFile
    files: tuple[ControllerFile, ...]


@dataclass(frozen=True)
class ControllerActivation:
    sources: ControllerSources
    manifest: ControllerFile


def _config(data: bytes) -> dict[str, Any]:
    document = loads(data, label=BUILD_CONFIG_PATH, max_bytes=limits.MAX_CI_CONFIG_BYTES)
    return validate_build_config(document)


def authenticate_controller_sources(api: GitHubApi, *, identity: dict[str, Any],
                                    protected_paths: tuple[str, ...]) -> ControllerSources:
    """Read only approved paths from the protected controller's immutable Git tree/blobs.

    The exact protected path inventory comes from authenticated native policy, not this config
    or candidate data. Live source authentication brackets the complete read. This is source-byte
    admission, not proof of Python import closure completeness, owner approval or native validity.
    """

    if (type(protected_paths) is not tuple or not 1 <= len(protected_paths) <= limits.MAX_CI_ADAPTER_FILES + 1
            or any(not grammar.is_repo_path(path) for path in protected_paths)
            or protected_paths != tuple(sorted(set(protected_paths)))
            or len({path.casefold() for path in protected_paths}) != len(protected_paths)
            or BUILD_CONFIG_PATH not in protected_paths):
        raise SourceError("controller source policy paths are malformed or incomplete")
    authenticate_source_identity(api, identity)
    controller_tree = commit_tree(api, identity["controller_sha"])
    entries = {row["path"]: row for row in exact_tree(api, controller_tree, recursive=True)}

    def metadata(path: str, cap: int) -> dict[str, Any]:
        parts = path.split("/")
        for index in range(1, len(parts)):
            parent = entries.get("/".join(parts[:index]))
            if parent is None or parent["type"] != "tree" or parent["mode"] != "040000":
                raise SourceError("protected controller source has a missing or non-directory ancestor")
        row = entries.get(path)
        if (row is None or row["type"] != "blob" or row["mode"] not in {"100644", "100755"}
                or type(row.get("size")) is not int or not 0 <= row["size"] <= cap):
            raise SourceError("protected controller source is missing, linked or exceeds its cap")
        return row

    def read(path: str, row: dict[str, Any], cap: int) -> ControllerFile:
        data = blob(api, row["sha"], max_bytes=cap)
        if len(data) != row["size"]:
            raise SourceError("controller Git tree and blob sizes disagree")
        return ControllerFile(path, row["mode"], row["sha"], hashlib.sha256(data).hexdigest(), data)

    config_file = read(BUILD_CONFIG_PATH, metadata(BUILD_CONFIG_PATH, limits.MAX_CI_CONFIG_BYTES),
                       limits.MAX_CI_CONFIG_BYTES)
    config = _config(config_file.data)
    if config["repository"] != api.repository:
        raise SourceError("protected Build config names another repository")
    files = config["adapter"]["files"]
    check_output_paths([BUILD_CONFIG_PATH, *[file["path"] for file in files]], "$.controller.sources")
    if any(file["path"].split("/")[0].casefold() == ".git" for file in files):
        raise SourceError("controller import source cannot include Git metadata")
    if not {file["path"] for file in files} <= set(protected_paths):
        raise SourceError("controller import source is outside protected native policy")
    rows = [metadata(file["path"], limits.MAX_CI_ADAPTER_FILE_BYTES) for file in files]
    if len(config_file.data) + sum(row["size"] for row in rows) > limits.MAX_CI_ADAPTER_TREE_BYTES:
        raise SourceError("protected controller import closure exceeds its whole-byte cap")
    sources = []
    for file, row in zip(files, rows):
        observed = read(file["path"], row, limits.MAX_CI_ADAPTER_FILE_BYTES)
        if observed.sha256 != file["sha256"]:
            raise SourceError("protected controller source differs from configured hash")
        sources.append(observed)
    authenticate_source_identity(api, identity)
    return ControllerSources(identity["controller_sha"], controller_tree, config_file, tuple(sources))


def _validate_sources(sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]:
    validate_identity(identity)
    if (type(sources) is not ControllerSources or sources.controller_sha != identity["controller_sha"]
            or not grammar.is_match(grammar.SHA1, sources.controller_tree)
            or type(sources.config) is not ControllerFile or sources.config.path != BUILD_CONFIG_PATH
            or type(sources.files) is not tuple or not 3 <= len(sources.files) <= limits.MAX_CI_ADAPTER_FILES):
        raise SourceError("controller source receipt has invalid protected identity or inventory")
    files = (sources.config, *sources.files)
    total = 0
    for file in files:
        if (type(file) is not ControllerFile or type(file.data) is not bytes
                or type(file.mode) is not str or file.mode not in {"100644", "100755"}
                or not grammar.is_repo_path(file.path)
                or file.path.split("/")[0].casefold() == ".git"):
            raise SourceError("controller source receipt contains a malformed file")
        cap = limits.MAX_CI_CONFIG_BYTES if file.path == BUILD_CONFIG_PATH else limits.MAX_CI_ADAPTER_FILE_BYTES
        total += len(file.data)
        if len(file.data) > cap or total > limits.MAX_CI_ADAPTER_TREE_BYTES:
            raise SourceError("controller source receipt exceeds its byte caps")
        oid = hashlib.sha1(b"blob " + str(len(file.data)).encode("ascii") + b"\0" + file.data).hexdigest()
        if oid != file.git_blob or hashlib.sha256(file.data).hexdigest() != file.sha256:
            raise SourceError("controller source receipt bytes and object hashes disagree")
    config = _config(sources.config.data)
    if (config["repository"] != identity["repository"]
            or [(file.path, file.sha256) for file in sources.files] !=
               [(file["path"], file["sha256"]) for file in config["adapter"]["files"]]):
        raise SourceError("controller source receipt does not match protected config closure")
    check_output_paths([file.path for file in files], "$.controller.sources")
    return config


def _read_controller_activation(api: GitHubApi, controller_tree: str) -> ControllerFile:
    entries = exact_tree(api, controller_tree, recursive=True)
    names = {'site', ACTIVATION_PATH}
    folded_names = {name.casefold() for name in names}
    if any(row['path'].casefold() in folded_names and row['path'] not in names
           for row in entries):
        raise SourceError('controller activation has a case-aliased path or parent')
    rows = {row['path']: row for row in entries}
    parent = rows.get('site')
    row = rows.get(ACTIVATION_PATH)
    if (parent is None or parent['type'] != 'tree' or parent['mode'] != '040000'
            or row is None or row['type'] != 'blob' or row['mode'] != '100644'
            or type(row.get('size')) is not int or not 1 <= row['size'] <= limits.MAX_CI_ACTIVATION_BYTES):
        raise SourceError('controller activation is missing, linked, executable or oversized')
    data = blob(api, row['sha'], max_bytes=limits.MAX_CI_ACTIVATION_BYTES)
    if len(data) != row['size']:
        raise SourceError('controller activation tree/blob sizes disagree')
    return ControllerFile(ACTIVATION_PATH, row['mode'], row['sha'], hashlib.sha256(data).hexdigest(), data)


def authenticate_controller_activation(api: GitHubApi, *, identity: dict[str, Any],
                                       protected_paths: tuple[str, ...]) -> ControllerActivation:
    """Bind fixed activation bytes to API-authenticated original controller/config sources.

    Caller derives protected paths from independently admitted native policy, including the
    manifest. Returned constructible data does not approve a transition, owner, rollback,
    caller rendering, native predecessor or execution. No candidate manifest is consulted.
    """
    if (type(protected_paths) is not tuple or not 2 <= len(protected_paths) <= limits.MAX_CI_ADAPTER_FILES + 2
            or any(not grammar.is_repo_path(path) for path in protected_paths)
            or protected_paths != tuple(sorted(set(protected_paths)))
            or ACTIVATION_PATH not in protected_paths or BUILD_CONFIG_PATH not in protected_paths):
        raise SourceError('activation requires the fixed manifest in protected native policy')
    check_output_paths(list(protected_paths), '$.activation.protected_paths')
    source_paths = tuple(path for path in protected_paths if path != ACTIVATION_PATH)
    sources = authenticate_controller_sources(api, identity=identity, protected_paths=source_paths)
    config = _validate_sources(sources, identity)
    authenticate_source_identity(api, identity)
    if commit_tree(api, identity['controller_sha']) != sources.controller_tree:
        raise SourceError('activation controller tree differs from retained source admission')
    manifest = _read_controller_activation(api, sources.controller_tree)
    document = validate_activation(loads(manifest.data, label=ACTIVATION_PATH,
                                         max_bytes=limits.MAX_CI_ACTIVATION_BYTES))
    if document['repository'] != config['repository'] or document['profile'] != config['profile']:
        raise SourceError('activation repository/profile differs from original protected config')
    if (authenticate_controller_sources(api, identity=identity, protected_paths=source_paths) != sources
            or _read_controller_activation(api, sources.controller_tree) != manifest):
        raise SourceError('activation or original controller sources changed during admission')
    authenticate_source_identity(api, identity)
    return ControllerActivation(sources, manifest)


def verify_controller_source_copy(root: Path, *, sources: ControllerSources,
                                  identity: dict[str, Any]) -> dict[str, Any]:
    """Verify a minimal source-only import copy against protected in-memory API evidence.

    Caller must retain the actual authenticated receipt, protect ancestors and exclude writers.
    Reject undeclared files and Git metadata. This reads bytes/modes; it imports no code and
    cannot establish that all Python/system import roots have been independently protected.
    """

    return _verify_controller_copy(root, sources=sources, identity=identity, read_only=False)


def _verify_controller_copy(root: Path, *, sources: ControllerSources,
                            identity: dict[str, Any], read_only: bool) -> dict[str, Any]:
    config = _validate_sources(sources, identity)
    files = (sources.config, *sources.files)
    try:
        Path(root, ".git").lstat()  # The fixed handoff root is a pure path constant.
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SourceError("cannot inspect controller import metadata exclusion") from error
    else:
        raise SourceError("controller import copy must omit Git metadata")
    inventory = tuple(sorted((GitSourceEntry(file.path, "100644" if read_only else file.mode,
                                             len(file.data), file.git_blob)
                              for file in files), key=lambda entry: entry.path))
    records = verify_source_copy(root, inventory=inventory)
    expected = {file.path: file.sha256 for file in files}
    if any(record["sha256"] != expected[record["path"]] for record in records):
        raise SourceError("controller import copy differs from protected source bytes")
    return config


def _write_controller_files(stage_fd: int, files: tuple[ControllerFile, ...]) -> None:
    """Write only already-admitted immutable bytes into an empty protected private stage."""

    try:
        info = os.fstat(stage_fd)
        if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
                or info.st_uid != os.geteuid() or os.listdir(stage_fd)):
            raise SourceError("controller source stage must be empty, caller-owned and private")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        for file in files:
            parts = file.path.split("/")
            parent = os.dup(stage_fd)
            try:
                for name in parts[:-1]:
                    try:
                        os.mkdir(name, 0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    child = os.open(name, directory_flags, dir_fd=parent)
                    try:
                        node = os.fstat(child)
                        if node.st_uid != info.st_uid or stat.S_IMODE(node.st_mode) != 0o700:
                            raise SourceError("controller source stage directory lost private ownership")
                        os.fsync(parent)
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(parent)
                    parent = child
                descriptor = os.open(parts[-1], file_flags, 0o600, dir_fd=parent)
                try:
                    node = os.fstat(descriptor)
                    if not stat.S_ISREG(node.st_mode) or node.st_nlink != 1 or node.st_uid != info.st_uid:
                        raise SourceError("controller source stage file lost exclusive ownership")
                    pending = memoryview(file.data)
                    while pending:
                        count = os.write(descriptor, pending)
                        if count <= 0:
                            raise SourceError("controller source stage write made no progress")
                        pending = pending[count:]
                    os.fchmod(descriptor, 0o755 if file.mode == "100755" else 0o644)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.fsync(parent)
            finally:
                os.close(parent)
        os.fsync(stage_fd)
    except OSError as error:
        raise SourceError("cannot materialize protected controller source bytes") from error


def materialize_controller_sources(output: Path, *, sources: ControllerSources,
                                   identity: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish a minimal private copy from retained protected API byte evidence.

    Caller protects the destination parent and retains authentic in-memory evidence. Existing
    outputs are never replaced. No import, worker access grant or native approval is performed.
    """

    expected = _validate_sources(sources, identity)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        _write_controller_files(stage_fd, (sources.config, *sources.files))
        observed = verify_controller_source_copy(stage, sources=sources, identity=identity)
        if observed != expected:
            raise SourceError("materialized controller config differs from protected receipt")
        return observed

    return atomic_directory(output, writer)


def prepare_controller_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                  sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]:
    """Protected-root-only read handoff of authenticated code to the fixed second account.

    Preserve source bytes/blob identities; access grant intentionally strips executable modes.
    Native Python runs through the protected fixed interpreter, not executable source files.
    This is not import-closure, installer provenance or native execution/receipt authority.
    """

    authenticate_privileged_host_boundary(boundary)
    _validate_sources(sources, identity)
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("controller handoff requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("controller validator identity changed")
    candidate = authenticate_worker_account("candidate")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("controller handoff identities are not isolated from runner and peer")
    descriptor = None
    admitted = False
    try:
        for path in (WORKER_ROOT.parent, WORKER_ROOT):
            parent = _open_directory(tuple(path.parts[1:]))
            try:
                info = os.fstat(parent)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o711):
                    raise WorkerError("controller handoff traversal root is not protected runner-owned")
            finally:
                os.close(parent)
        descriptor = _open_directory(tuple(CONTROLLER_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (boundary.uid, boundary.gid, 0o700):
            raise WorkerError("controller handoff copy must be fresh private runner-owned bytes")
        admitted = True
        terminate_worker(candidate)
        expected = verify_controller_source_copy(CONTROLLER_VALIDATION_ROOT, sources=sources, identity=identity)
        grant_source_read_access(CONTROLLER_VALIDATION_ROOT,
                               tracked_paths=tuple(sorted(file.path for file in (sources.config, *sources.files))),
                               source_owner_uid=boundary.uid,
                               owner_uid=boundary.uid, reader_gid=validator.gid,
                               max_files=limits.MAX_CI_ADAPTER_FILES + 1,
                               max_entries=limits.MAX_CI_SOURCE_ENTRIES,
                               max_total_bytes=limits.MAX_CI_ADAPTER_TREE_BYTES,
                               max_file_bytes=limits.MAX_CI_ADAPTER_FILE_BYTES)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, validator.gid, 0o750)):
            raise WorkerError("controller handoff copy identity or permissions changed")
        observed = _verify_controller_copy(CONTROLLER_VALIDATION_ROOT, sources=sources,
                                           identity=identity, read_only=True)
        if observed != expected:
            raise WorkerError("controller handoff config changed")
        authenticate_privileged_host_boundary(boundary)
        return observed
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("controller handoff could not restore private traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot prepare protected controller read handoff") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _authenticate_controller_read_copy(boundary: HostBoundary, validator: WorkerAccount,
                                      sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]:
    authenticate_host_boundary(boundary)
    for path in (WORKER_ROOT.parent, WORKER_ROOT):
        descriptor = _open_directory(tuple(path.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o711):
                raise WorkerError("controller read traversal layout changed")
        finally:
            os.close(descriptor)
    authenticate_tree_read_access(CONTROLLER_VALIDATION_ROOT, owner_uid=boundary.uid,
                                  reader_gid=validator.gid, max_entries=limits.MAX_CI_SOURCE_ENTRIES)
    return _verify_controller_copy(CONTROLLER_VALIDATION_ROOT, sources=sources,
                                   identity=identity, read_only=True)


def execute_controller_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                 sources: ControllerSources, tools: ToolTreeProof,
                                 plan: dict[str, Any], hook: str, unit_id: str | None,
                                 python: str, java_home: str | None, run_id: int,
                                 run_attempt: int) -> WorkerResult:
    """Run one closed native verification hook from retained protected source evidence.

    Protected caller must finish installer provenance, complete interpreter/import enrollment
    and immutable native inputs first. Fixed hook argv/timeout are derived here; no arbitrary
    command/environment is accepted. Exit zero/logs do not authorize receipts, upload or status.
    """

    return _execute_controller_validator(boundary=boundary, validator=validator, sources=sources,
        tools=tools, plan=plan, hook=hook, unit_id=unit_id, python=python, java_home=java_home,
        run_id=run_id, run_attempt=run_attempt, byte_fenced=False, expected_digest=None)


def execute_byte_fenced_controller_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                             sources: ControllerSources, tools: ToolBytesProof,
                                             expected_digest: str, plan: dict[str, Any], hook: str,
                                             unit_id: str | None, python: str, java_home: str | None,
                                             run_id: int, run_attempt: int) -> WorkerResult:
    """Bind closed protected verification hooks to independently approved tool bytes.

    Retain the original source/config/account/plan/hook admission and pre/post source checks.
    Full selected tool bytes are checked around execution and whole-UID termination. Original
    caller/runtime, complete import/system enrollment and immutable native inputs remain caller
    prerequisites. Results do not authorize receipts, uploads or statuses.
    """
    return _execute_controller_validator(boundary=boundary, validator=validator, sources=sources,
        tools=tools, plan=plan, hook=hook, unit_id=unit_id, python=python, java_home=java_home,
        run_id=run_id, run_attempt=run_attempt, byte_fenced=True, expected_digest=expected_digest)


def _execute_controller_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                  sources: ControllerSources, tools: ToolTreeProof | ToolBytesProof,
                                  plan: dict[str, Any], hook: str, unit_id: str | None,
                                  python: str, java_home: str | None, run_id: int, run_attempt: int,
                                  byte_fenced: bool, expected_digest: str | None) -> WorkerResult:
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("controller execution requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("controller execution validator identity changed")
    try:
        if byte_fenced:
            grammar.require(grammar.DIGEST, expected_digest, "approved tool byte digest")
            if type(tools) is not ToolBytesProof or tools.digest != expected_digest:
                raise WorkerError("validator tool receipt differs from independently approved bytes")
        validate_plan(plan)
        config = _validate_sources(sources, plan["identity"])
        if config["profile"] != plan["profile"] or type(hook) is not str or hook not in VALIDATOR_HOOKS:
            raise WorkerError("controller verification hook or profile is invalid")
        values = {}
        if hook == "verify_build":
            if unit_id is not None:
                raise WorkerError("aggregate Build verification cannot select a unit")
        else:
            key = "targets" if hook == "verify_target" else "lanes"
            if type(unit_id) is not str or unit_id not in {unit["id"] for unit in plan[key]}:
                raise WorkerError("controller verification unit is outside the protected plan")
            values["MB_TARGET_ID" if hook == "verify_target" else "MB_LANE_ID"] = unit_id
        authenticate_host_boundary(boundary)
        candidate = authenticate_worker_account("candidate")
        if (candidate.uid == validator.uid or candidate.gid == validator.gid
                or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
            raise WorkerError("controller execution identities are not isolated")
        observed = _authenticate_controller_read_copy(boundary, validator, sources, plan["identity"])
        if observed != config:
            raise WorkerError("controller pre-execution config changed")
        dispatcher = str(CONTROLLER_VALIDATION_ROOT / config["adapter"]["dispatcher"])
        execute = execute_byte_fenced_worker if byte_fenced else execute_tool_fenced_worker
        byte_arguments = {"expected_digest": expected_digest} if byte_fenced else {}
        result = execute(validator, boundary=boundary, tools=tools, **byte_arguments,
                    command=(python, "-I", "-B", dispatcher, "--hook", hook), python=python,
                    java_home=java_home, identity=plan["identity"], run_id=run_id,
                    run_attempt=run_attempt, values=values,
                    timeout_seconds=config["timeouts"]["validator_seconds"])
        if _authenticate_controller_read_copy(boundary, validator, sources, plan["identity"]) != config:
            raise WorkerError("controller post-execution config changed")
        return result
    except OSError as error:
        raise WorkerError("cannot execute protected controller verification") from error
    finally:
        # Includes pre-dispatch admission failures and a final independent post-dispatch sweep.
        terminate_worker(validator)
