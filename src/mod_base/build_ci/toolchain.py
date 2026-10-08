"""Bounded read-only admission of protected host tool/import trees (MB11).

Metadata admission proves filesystem permissions and identity closure of the selected roots: no
foreign owner, no group/other write access and no special file anywhere below them or on the way
to them. It does not prove installer provenance or compiler semantics. Protected setup must
finish first; mutable tool trees are rejected.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from mod_base.build_ci.host import (HostBoundary, _canonical_path, _open_directory,
                                    authenticate_host_boundary, execute_isolated_worker)
from mod_base.build_ci.worker import WorkerAccount, WorkerError, WorkerResult, terminate_worker
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json


TOOL_INSTALL_PREFIXES = ("/opt/hostedtoolcache", "/usr/lib/jvm")
TOOL_LINK_PREFIXES = (*TOOL_INSTALL_PREFIXES, "/usr", "/lib", "/lib64", "/etc")


@dataclass(frozen=True)
class ToolTreeProof:
    roots: tuple[str, ...]
    metadata_sha256: str
    files: int
    entries: int
    total_bytes: int


def _below(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _Scan:
    def __init__(self, runner_uid: int):
        self.trusted = {0, runner_uid}
        self.records: dict[str, dict] = {}
        self.directories: set[tuple[int, int]] = set()
        self.files = 0
        self.total = 0
        self.json_bytes = 0

    def record(self, path: str, info: os.stat_result, *, target: str | None = None) -> None:
        if info.st_uid not in self.trusted:
            raise WorkerError("tool tree contains a foreign-owned entry")
        if not stat.S_ISLNK(info.st_mode) and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            # POSIX access-ACL write grants require a write-capable group-class mask too.
            raise WorkerError("tool tree has group/other write access")
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            raise WorkerError("tool tree contains a special file")
        value = {"path": path, "stamp": list(_stamp(info)), "target": target}
        previous = self.records.get(path)
        if previous is not None:
            if previous != value:
                raise WorkerError("tool entry changed during admission")
            return
        if len(self.records) >= limits.MAX_CI_SOURCE_ENTRIES:
            raise WorkerError("tool closure exceeds its entry cap")
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            cap = limits.MAX_CI_SOURCE_LINK_BYTES if stat.S_ISLNK(info.st_mode) else limits.MAX_CI_SOURCE_FILE_BYTES
            if info.st_size < 0 or info.st_size > cap:
                raise WorkerError("tool file exceeds its byte cap")
            self.files += 1
            self.total += info.st_size
            if self.files > limits.MAX_CI_SOURCE_FILES or self.total > limits.MAX_CI_SOURCE_TREE_BYTES:
                raise WorkerError("tool closure exceeds its file/whole-tree cap")
        self.json_bytes += len(canonical_json(value))
        if self.json_bytes > limits.MAX_CI_SOURCE_LIST_BYTES:
            raise WorkerError("tool metadata closure exceeds its byte cap")
        self.records[path] = value

    def resolve(self, path: str) -> tuple[str, os.stat_result]:
        """Resolve bounded links while authenticating every alias/target ancestor read-only."""
        pending = list(_canonical_path(path).parts[1:])
        resolved: list[str] = []
        hops = 0
        descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.record("/", os.fstat(descriptor))
            while pending:
                part = pending.pop(0)
                absolute = "/" + "/".join((*resolved, part))
                info = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    if info.st_uid not in self.trusted:
                        raise WorkerError("tool link is foreign-owned")
                    if not 1 <= info.st_size <= limits.MAX_CI_SOURCE_LINK_BYTES:
                        raise WorkerError("tool link exceeds its byte cap")
                    raw = os.readlink(part.encode("utf-8"), dir_fd=descriptor)
                    if len(raw) != info.st_size or _stamp(os.stat(part, dir_fd=descriptor, follow_symlinks=False)) != _stamp(info):
                        raise WorkerError("tool link changed while read")
                    try:
                        target = raw.decode("utf-8", "strict")
                    except UnicodeError as error:
                        raise WorkerError("tool link is not strict UTF-8") from error
                    self.record(absolute, info, target=target)
                    hops += 1
                    if hops > limits.MAX_CI_TOOL_SYMLINK_HOPS:
                        raise WorkerError("tool link resolution exceeds its hop cap")
                    parts = [] if target.startswith("/") else list(resolved)
                    for component in target.split("/"):
                        if component in {"", "."}:
                            continue
                        if component == "..":
                            if not parts:
                                raise WorkerError("tool link escapes the filesystem root")
                            parts.pop()
                        else:
                            parts.append(component)
                    destination = "/" + "/".join(parts)
                    _canonical_path(destination)
                    if not _below(destination, TOOL_LINK_PREFIXES):
                        raise WorkerError("tool link leaves the supported tool/system prefixes")
                    pending = parts + pending
                    if len(("/" + "/".join(pending)).encode("utf-8")) > limits.MAX_CI_TOOL_PATH_BYTES:
                        raise WorkerError("resolved tool path exceeds its byte cap")
                    resolved = []
                    os.close(descriptor)
                    descriptor = None
                    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    continue
                self.record(absolute, info)
                if pending:
                    if not stat.S_ISDIR(info.st_mode):
                        raise WorkerError("tool path has a non-directory ancestor")
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise WorkerError("tool directory changed while opened")
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(descriptor)
                    descriptor = child
                resolved.append(part)
            return "/" + "/".join(resolved), info
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def walk(self, path: str, depth: int) -> None:
        if depth > limits.MAX_CI_TOOL_TREE_DEPTH:
            raise WorkerError("tool closure exceeds its depth cap")
        resolved, info = self.resolve(path)
        if not stat.S_ISDIR(info.st_mode):
            return
        identity = (info.st_dev, info.st_ino)
        if identity in self.directories:
            return
        self.directories.add(identity)
        descriptor = _open_directory(tuple(PurePosixPath(resolved).parts[1:]))
        try:
            if _stamp(os.fstat(descriptor)) != _stamp(info):
                raise WorkerError("tool tree root changed while opened")
            remaining = limits.MAX_CI_SOURCE_ENTRIES - len(self.records)
            with os.scandir(descriptor) as entries:
                names = [entry.name for entry in itertools.islice(entries, remaining + 1)]
            if len(names) > remaining:
                raise WorkerError("tool directory listing exceeds its entry cap")
            for name in sorted(names):
                self.walk(resolved + "/" + name, depth + 1)
            if _stamp(os.fstat(descriptor)) != _stamp(info):
                raise WorkerError("tool directory changed during admission")
        finally:
            os.close(descriptor)


def inspect_worker_toolchains(*, boundary: HostBoundary, roots: tuple[str, ...]) -> ToolTreeProof:
    """Inspect the permission/identity closure of the selected roots as the fenced runner."""
    authenticate_host_boundary(boundary)
    if type(roots) is not tuple or not 1 <= len(roots) <= limits.MAX_CI_TOOL_ROOTS:
        raise WorkerError("tool root inventory is empty or exceeds its cap")
    for root in roots:
        _canonical_path(root)
        if not _below(root, TOOL_INSTALL_PREFIXES) or root in TOOL_INSTALL_PREFIXES:
            raise WorkerError("tool root is outside the supported installation prefixes")
    if len(set(roots)) != len(roots):
        raise WorkerError("tool roots are duplicated")
    scanner = _Scan(boundary.uid)
    try:
        for root in roots:
            resolved, info = scanner.resolve(root)
            if not stat.S_ISDIR(info.st_mode) or not _below(resolved, TOOL_INSTALL_PREFIXES):
                raise WorkerError("selected tool root must be a real directory")
            scanner.walk(resolved, 0)
    except OSError as error:
        raise WorkerError("cannot inspect protected host tool closure") from error
    digest = hashlib.sha256()
    for path in sorted(scanner.records):
        digest.update(canonical_json(scanner.records[path]))
    return ToolTreeProof(roots, digest.hexdigest(), scanner.files, len(scanner.records), scanner.total)


def authenticate_toolchains(proof: ToolTreeProof, *, boundary: HostBoundary) -> None:
    """Re-inspect permission/identity closure and reject any tool receipt drift before dispatch."""
    if type(proof) is not ToolTreeProof:
        raise WorkerError("tool closure receipt is invalid")
    if inspect_worker_toolchains(boundary=boundary, roots=proof.roots) != proof:
        raise WorkerError("tool closure changed after protected admission")


def _execution_tool_paths(proof: ToolTreeProof, boundary: HostBoundary,
                          python: str, java_home: str | None) -> None:
    """Bind executable destinations to fully walked roots, not just permitted link prefixes.

    The scanner may inspect a link target's metadata without walking its surrounding import
    tree. Such a target cannot become an execution root unless setup explicitly enrolled it.
    Installer provenance and complete Python/JDK import enrollment remain caller requirements.
    """
    paths = (python, *((java_home,) if java_home is not None else ()))
    for path in paths:
        _canonical_path(path)
        if not _below(path, proof.roots):
            raise WorkerError("worker Python/JDK path is not inside the admitted tool roots")
    scanner = _Scan(boundary.uid)
    try:
        roots = tuple(scanner.resolve(root)[0] for root in proof.roots)
        for index, path in enumerate(paths):
            resolved, info = scanner.resolve(path)
            if not _below(resolved, roots):
                raise WorkerError("worker Python/JDK destination escapes the admitted tool roots")
            if index == 0:
                if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0
                        or not info.st_mode & stat.S_IXOTH):
                    raise WorkerError("worker Python must be a nonempty worker-executable regular file")
            elif not stat.S_ISDIR(info.st_mode) or not info.st_mode & stat.S_IXOTH:
                raise WorkerError("worker JAVA_HOME must be a worker-traversable directory")
    except OSError as error:
        raise WorkerError("cannot bind protected execution tool destinations") from error


def execute_tool_fenced_worker(account: WorkerAccount, *, boundary: HostBoundary,
                               tools: ToolTreeProof, command: tuple[str, ...], python: str,
                               java_home: str | None, identity: dict[str, Any], run_id: int,
                               run_attempt: int, values: Mapping[str, str], timeout_seconds: int) -> WorkerResult:
    """Revalidate host tool closure and bind Python/JDK paths before isolated dispatch.

    Installer provenance, complete import-root enrollment and native observations remain
    protected caller preconditions. A failed tool check terminates the account without launch.
    """
    try:
        authenticate_toolchains(tools, boundary=boundary)
        _execution_tool_paths(tools, boundary, python, java_home)
    except MbError:
        terminate_worker(account)
        raise
    return execute_isolated_worker(account, boundary=boundary, command=command, python=python,
                                   java_home=java_home, identity=identity, run_id=run_id,
                                   run_attempt=run_attempt, values=values, timeout_seconds=timeout_seconds)
