"""Host filesystem fence for the initial GitHub-hosted Linux worker profile (MB11).

Explicit protected inputs only. This hides the runner home and its workspace/action/temp
trees; it is not a VM boundary or a proof about files outside the admitted host layout.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from mod_base.build_ci.worker import (WorkerAccount, WorkerError, WorkerResult,
                                      execute_worker, terminate_worker)
from mod_base.errors import MbError
from mod_base.model import limits


HOST_RUNNER_HOME = "/home/runner"


@dataclass(frozen=True)
class HostBoundary:
    home: str
    uid: int
    gid: int
    device: int
    inode: int
    original_mode: int


def _canonical_path(value: str) -> PurePosixPath:
    if type(value) is not str or len(value) > limits.MAX_CI_TOOL_PATH_BYTES:
        raise WorkerError("host layout path exceeds its byte cap or has an invalid type")
    try:
        if len(value.encode("utf-8")) > limits.MAX_CI_TOOL_PATH_BYTES:
            raise WorkerError("host layout path exceeds its byte cap")
    except UnicodeError as error:
        raise WorkerError("host layout path is not strict UTF-8") from error
    if (not value.startswith("/") or "\\" in value or ":" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])):
        raise WorkerError("host layout must use canonical absolute POSIX paths")
    return PurePosixPath(value)


def _open_directory(parts: tuple[str, ...], *, root: int | None = None) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags) if root is None else os.dup(root)
    try:
        for part in parts:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                raise WorkerError("host layout contains a non-directory or symlink")
            child = os.open(part, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (not stat.S_ISDIR(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    raise WorkerError("host directory changed while opened")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def protect_worker_host(*, runner_environment: str, runner_home: str,
                        workspace: str, runner_temp: str) -> HostBoundary:
    """Close traversal into the fixed runner home after authenticating the initial host layout.

    Call only from the protected prologue on a fresh admitted hosted runner, before candidate
    execution. Candidate processes must start with a private cwd and no inherited host fds.
    Toolchains outside the home remain accessible and must independently be protected against
    worker writes/import poisoning; caches are copied before worker admission.
    Failure after chmod deliberately leaves the home private. Never relax it while UIDs live.
    """

    if (sys.platform != "linux" or type(runner_environment) is not str
            or runner_environment != "github-hosted" or runner_home != HOST_RUNNER_HOME):
        raise WorkerError("host fence requires the initial GitHub-hosted Linux layout")
    home = _canonical_path(runner_home)
    paths = (_canonical_path(workspace), _canonical_path(runner_temp))
    for path in paths:
        if home not in path.parents:
            raise WorkerError("host workspace and temp must be below the runner home")
    if paths[0] == paths[1]:
        raise WorkerError("host workspace and temp must be distinct directories")
    if os.getuid() == 0 or os.getgid() == 0:
        raise WorkerError("host fence requires a non-root runner identity")
    import pwd

    descriptor = None
    try:
        account = pwd.getpwuid(os.getuid())
        if account.pw_dir != HOST_RUNNER_HOME:
            raise WorkerError("runner passwd home does not bind the admitted layout")
        descriptor = _open_directory(tuple(home.parts[1:]))
        before = os.fstat(descriptor)
        if before.st_uid != os.getuid():
            raise WorkerError("runner home is not owned by the executing runner")
        for path in paths:
            child = _open_directory(tuple(path.relative_to(home).parts), root=descriptor)
            try:
                if os.fstat(child).st_uid != os.getuid():
                    raise WorkerError("host workspace/temp is not runner-owned")
            finally:
                os.close(child)
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino, after.st_uid, after.st_gid) !=
                (before.st_dev, before.st_ino, before.st_uid, before.st_gid)
                or stat.S_IMODE(after.st_mode) != 0o700):
            raise WorkerError("runner home did not bind the private host fence")
        return HostBoundary(HOST_RUNNER_HOME, after.st_uid, after.st_gid, after.st_dev,
                            after.st_ino, stat.S_IMODE(before.st_mode))
    except (OSError, KeyError) as error:
        raise WorkerError("cannot establish private runner host fence") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _validate_host_receipt(boundary: HostBoundary) -> None:
    if sys.platform != "linux" or type(boundary) is not HostBoundary or boundary.home != HOST_RUNNER_HOME:
        raise WorkerError("host fence receipt is invalid or unsupported")
    if (any(type(value) is not int or value < 0 for value in
            (boundary.uid, boundary.gid, boundary.device, boundary.inode, boundary.original_mode))
            or boundary.original_mode > 0o7777):
        raise WorkerError("host fence receipt has invalid operating-system identity fields")
    if not 1 <= boundary.uid <= limits.MAX_CI_UNIX_ID or not 1 <= boundary.gid <= limits.MAX_CI_UNIX_ID:
        raise WorkerError("host fence must bind a nonprivileged runner identity")


def _authenticate_home_receipt(boundary: HostBoundary) -> None:
    descriptor = None
    try:
        descriptor = _open_directory(("home", "runner"))
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino, info.st_uid, info.st_gid) !=
                (boundary.device, boundary.inode, boundary.uid, boundary.gid)
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise WorkerError("runner host fence identity or private mode changed")
    except OSError as error:
        raise WorkerError("cannot authenticate private runner host fence") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def authenticate_host_boundary(boundary: HostBoundary) -> None:
    """Recheck the exact private runner-home inode before admitting either disposable UID."""

    _validate_host_receipt(boundary)
    if os.getgid() == 0 or boundary.uid != os.getuid():
        raise WorkerError("host fence belongs to a different runner")
    _authenticate_home_receipt(boundary)


def privileged_runner_identity() -> tuple[int, int]:
    """Root-only: the passwd identity that owns the fixed runner home, derived from the host.

    A root operation learns who the runner is from the filesystem and passwd, never from its
    request. The result names an account; it is not a host fence receipt.
    """

    if sys.platform != "linux" or os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise WorkerError("runner identity lookup requires protected root setup")
    import pwd

    descriptor = None
    try:
        descriptor = _open_directory(("home", "runner"))
        info = os.fstat(descriptor)
        account = pwd.getpwuid(info.st_uid)
        if ((account.pw_uid, account.pw_gid, account.pw_dir) != (info.st_uid, info.st_gid, HOST_RUNNER_HOME)
                or not 1 <= info.st_uid <= limits.MAX_CI_UNIX_ID or not 1 <= info.st_gid <= limits.MAX_CI_UNIX_ID):
            raise WorkerError("runner home does not belong to one nonprivileged passwd identity")
        return info.st_uid, info.st_gid
    except (OSError, KeyError) as error:
        raise WorkerError("cannot derive the runner identity from the fixed home") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def authenticate_privileged_host_boundary(boundary: HostBoundary) -> None:
    """Bind the existing runner fence from protected root setup without granting worker authority."""

    _validate_host_receipt(boundary)
    if os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise WorkerError("privileged host recheck requires protected root setup")
    import pwd
    try:
        account = pwd.getpwuid(boundary.uid)
        if (account.pw_uid, account.pw_gid, account.pw_dir) != (boundary.uid, boundary.gid, boundary.home):
            raise WorkerError("privileged host recheck has a changed runner passwd identity")
    except (OSError, KeyError) as error:
        raise WorkerError("cannot bind runner passwd identity from root setup") from error
    _authenticate_home_receipt(boundary)


def execute_isolated_worker(account: WorkerAccount, *, boundary: HostBoundary,
                            command: tuple[str, ...], python: str, java_home: str | None,
                            identity: dict[str, Any], run_id: int, run_attempt: int,
                            values: Mapping[str, str], timeout_seconds: int) -> WorkerResult:
    """Recheck the private host fence before dispatch; a failed fence locks the disposable UID.

    Authenticated source/import/caches and native validation remain caller preconditions. The
    worker's execution path uses a private cwd and closes inherited host descriptors.
    """

    try:
        authenticate_host_boundary(boundary)
    except MbError:
        terminate_worker(account)
        raise
    return execute_worker(account, command=command, python=python, java_home=java_home,
                          identity=identity, run_id=run_id, run_attempt=run_attempt,
                          values=values, timeout_seconds=timeout_seconds)
