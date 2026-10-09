"""Host filesystem fence for the initial GitHub-hosted Linux worker profile (MB11).

Explicit protected inputs only. Two fences exist. The runner closes its own home, which hides the
workspace, action and temp trees. Root then closes the image itself (``fence_worker_host``): a
hosted ``ubuntu-24.04`` image ships its tool cache, ``/usr/share``, ``/usr/local`` and the JDKs
world-writable, so any local account could replace the interpreter, a JDK or a command on root's
PATH. Neither fence is a VM boundary.
"""

from __future__ import annotations

import json
import os
import selectors
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from mod_base.build_ci.worker import (WORKER_ACCOUNTS, WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      execute_worker, terminate_worker)
from mod_base.errors import MbError, single_line
from mod_base.model import limits


HOST_RUNNER_HOME = "/home/runner"
#: The trees a hosted ``ubuntu-24.04`` image leaves writable by everyone (measured on a runner):
#: the tool cache and the rest of ``/opt``, ``/usr/share``, ``/usr/local`` (the head of root's PATH),
#: the JDKs and one gem. A tree an image does not have is skipped.
HOST_FENCE_TREES = ("/opt", "/usr/share", "/usr/local", "/usr/lib/jvm", "/var/lib/gems")
_FENCE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}


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


def _open_runner_home(runner_environment: str, runner_home: str, workspace: str,
                      runner_temp: str) -> tuple[int, os.stat_result]:
    """Authenticate the initial hosted layout; return a descriptor of the runner home and its metadata.

    Every later check of a receipt compares the home's group with the runner's passwd group and
    with the group of what the runner creates. They are required to agree here, by name, so that a
    host where they differ is refused before anything is changed.
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
        if not before.st_gid == account.pw_gid == os.getgid():
            raise WorkerError("runner home group, the runner's passwd group and its process group differ")
        for path in paths:
            child = _open_directory(tuple(path.relative_to(home).parts), root=descriptor)
            try:
                if os.fstat(child).st_uid != os.getuid():
                    raise WorkerError("host workspace/temp is not runner-owned")
            finally:
                os.close(child)
        return descriptor, before
    except BaseException as error:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(error, (OSError, KeyError)):
            raise WorkerError("cannot establish private runner host fence") from error
        raise


def inspect_worker_host(*, runner_environment: str, runner_home: str,
                        workspace: str, runner_temp: str) -> HostBoundary:
    """The receipt :func:`protect_worker_host` will return, without changing the home.

    ``ci worker-prepare`` records it before it changes anything, so that ``ci worker-finish`` can
    restore the home whatever happens in between.
    """

    descriptor, before = _open_runner_home(runner_environment, runner_home, workspace, runner_temp)
    os.close(descriptor)
    return HostBoundary(HOST_RUNNER_HOME, before.st_uid, before.st_gid, before.st_dev,
                        before.st_ino, stat.S_IMODE(before.st_mode))


def protect_worker_host(*, runner_environment: str, runner_home: str,
                        workspace: str, runner_temp: str) -> HostBoundary:
    """Close traversal into the fixed runner home after authenticating the initial host layout.

    Call only from the protected prologue on a fresh admitted hosted runner, before candidate
    execution. Candidate processes must start with a private cwd and no inherited host fds.
    Toolchains outside the home remain accessible and must independently be protected against
    worker writes/import poisoning; caches are copied before worker admission.
    Failure after chmod deliberately leaves the home private. Never relax it while UIDs live.
    """

    descriptor, before = _open_runner_home(runner_environment, runner_home, workspace, runner_temp)
    try:
        os.fchmod(descriptor, 0o700)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino, after.st_uid, after.st_gid) !=
                (before.st_dev, before.st_ino, before.st_uid, before.st_gid)
                or stat.S_IMODE(after.st_mode) != 0o700):
            raise WorkerError("runner home did not bind the private host fence")
        return HostBoundary(HOST_RUNNER_HOME, after.st_uid, after.st_gid, after.st_dev,
                            after.st_ino, stat.S_IMODE(before.st_mode))
    except OSError as error:
        raise WorkerError("cannot establish private runner host fence") from error
    finally:
        os.close(descriptor)


def restore_worker_host(boundary: HostBoundary) -> None:
    """Give the runner home the mode it had before the fence: the last act of a job.

    Only once no worker account can run any more: both terminated and locked. The home must be
    the recorded directory of the executing runner, still closed or already restored (a second
    call changes nothing).
    """

    _validate_host_receipt(boundary)
    if os.getgid() == 0 or boundary.uid != os.getuid():
        raise WorkerError("host fence belongs to a different runner")
    descriptor = None
    try:
        descriptor = _open_directory(("home", "runner"))
        info = os.fstat(descriptor)
        if ((info.st_dev, info.st_ino, info.st_uid, info.st_gid) !=
                (boundary.device, boundary.inode, boundary.uid, boundary.gid)
                or stat.S_IMODE(info.st_mode) not in {0o700, boundary.original_mode}):
            raise WorkerError("runner home is not the fenced directory this job recorded")
        os.fchmod(descriptor, boundary.original_mode)
        os.fsync(descriptor)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != boundary.original_mode:
            raise WorkerError("runner home did not take its original mode back")
    except OSError as error:
        raise WorkerError("cannot restore the runner home") from error
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


def _fence_command(command: tuple[str, ...], *, phase: str = "administrative",
                   deadline: float | None = None) -> tuple[bytes, bool]:
    """Run one fixed administrative command; return its bounded stdout and whether all of it fit.

    A command that cannot start, exits non-zero or outlives its bound is a rejection. A non-zero
    exit carries the start of what the command, or a program it ran, wrote to stderr. Every
    failure names the fixed phase and its elapsed time, without printing the command arguments.
    """

    process = None
    output, diagnostic = bytearray(), bytearray()
    complete = True
    started = time.monotonic()

    def failure(message: str) -> WorkerError:
        return WorkerError(f"host fence command {message} "
                           f"(phase={phase}; elapsed={time.monotonic() - started:.2f}s)")

    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=dict(_FENCE_ENV), cwd="/", close_fds=True)
        if process.stdout is None or process.stderr is None:
            raise failure("pipes unavailable")
        if deadline is None:
            deadline = time.monotonic() + limits.CI_HOST_FENCE_TIMEOUT_SECONDS
        with selectors.DefaultSelector() as selector:
            for stream, kept in ((process.stdout, output), (process.stderr, diagnostic)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, kept)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                ready = selector.select(remaining) if remaining > 0 else []
                if not ready:
                    raise failure("timed out")
                for key, _ in ready:
                    chunk = os.read(key.fd, limits.CI_PROCESS_READ_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    # Keep the first bounded bytes of each stream and keep draining both.
                    room = limits.MAX_CI_HOST_FENCE_REPORT_BYTES - len(key.data)
                    key.data.extend(chunk[:room])
                    complete = complete and (key.data is diagnostic or len(chunk) <= room)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise failure("timed out")
        if process.wait(timeout=remaining) != 0:
            raise failure("failed: " + single_line(diagnostic.decode("utf-8", "replace"), limit=300))
        return bytes(output), complete
    except (OSError, subprocess.SubprocessError) as error:
        raise failure("could not complete") from error
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError) as error:
                raise failure("did not reap") from error
            finally:
                for stream in (process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()


def _close_writable_trees(trees: tuple[str, ...], *, timings: dict[str, float] | None = None) -> None:
    """One walk: drop group/other write from directories and regular files, and every default ACL.

    Only entries that carry a write bit are changed, so a second run leaves metadata alone. Links
    are never followed or changed, and the walk stays on each tree's own filesystem. A default
    ACL would hand ``other::rwx`` to whatever is created in the tree afterwards.
    """

    deadline = time.monotonic() + limits.CI_HOST_FENCE_TIMEOUT_SECONDS
    for tree in trees:
        started = time.monotonic()
        try:
            _fence_command(("/usr/bin/find", tree, "-xdev", "-ignore_readdir_race",
                            "(", "(", "-type", "d", "-o", "-type", "f", ")", "-perm", "/0022",
                            "-exec", "/usr/bin/chmod", "go-w", "--", "{}", "+", ")", ",",
                            "(", "-type", "d", "-exec", "/usr/bin/setfacl", "-k", "--", "{}", "+", ")"),
                           phase="repair", deadline=deadline)
        finally:
            if timings is not None:
                timings[tree] = round(time.monotonic() - started, 6)


def _reachable_writable_entries(root: str, *, skip: str) -> tuple[list[str], bool]:
    """World-writable non-sticky directories and world-writable regular files on ``root``'s filesystem.

    ``skip`` (the worker boundary) is not entered. Neither is a directory that only its owner can
    search when that owner is an existing account: an account created later can never be that
    owner, so nothing below is reachable for it. Sticky directories are entered but not reported.
    Returns the entries and whether the listing was complete.
    """

    device = str(os.stat(root, follow_symlinks=False).st_dev).encode("ascii")
    output, complete = _fence_command((
        "/usr/bin/find", root, "-xdev", "-ignore_readdir_race", "-path", skip, "-prune", "-o",
        "(", "-type", "d", "!", "-perm", "/0011", "!", "-nouser", "-prune", ")", "-o",
        "(", "(", "-type", "d", "-perm", "-0002", "!", "-perm", "-1000", "-o", "-type", "f", "-perm", "-0002", ")",
        "-printf", "%D %p\\0", ")"), phase="verification")
    records = output.split(b"\0")[:-1]  # A cut-off last record has no terminator and is dropped.
    entries = []
    for record in records:
        number, _, path = record.partition(b" ")
        if number == device:  # A mount point is listed with the device of what is mounted on it.
            entries.append(path.decode("utf-8", "backslashreplace"))
    return entries, complete


def fence_worker_host(*, boundary: HostBoundary) -> None:
    """Root-only, before any worker account exists: close the hosted image to later accounts.

    Removes group/other write permission and default ACLs from the ``HOST_FENCE_TREES`` this host
    has, then proves that no world-writable non-sticky directory and no world-writable regular
    file remains reachable on the root filesystem outside the worker boundary. Anything left is
    a failure, and so is a tree that is not a real directory or a command that fails. Sticky
    directories outside those trees, such as ``/tmp``, stay as they are.
    """

    started = time.monotonic()
    timings: dict[str, Any] = {"repair": {}, "verification": 0.0, "ok": False}
    try:
        _fence_worker_host(boundary, timings)
        timings["ok"] = True
    finally:
        timings["total"] = round(time.monotonic() - started, 6)
        # A bounded informational observation, not an admission receipt. The runner transports
        # it as an audit event; hosted tests report the first real fence even when CLI output is
        # captured. Fixed phase names and image roots never expose candidate-controlled paths.
        print("mod-base host-fence timing: " + json.dumps(timings, separators=(",", ":")),
              file=sys.stderr, flush=True)


def _fence_worker_host(boundary: HostBoundary, timings: dict[str, Any]) -> None:
    authenticate_privileged_host_boundary(boundary)
    import pwd

    try:
        for name in WORKER_ACCOUNTS.values():
            try:
                pwd.getpwnam(name)
            except KeyError:
                continue
            raise WorkerError("host fence must run before any worker account exists")
        trees = []
        for tree in HOST_FENCE_TREES:
            try:
                os.close(_open_directory(tuple(PurePosixPath(tree).parts[1:])))
            except FileNotFoundError:
                continue
            trees.append(tree)
        if trees:
            _close_writable_trees(tuple(trees), timings=timings["repair"])
        started = time.monotonic()
        try:
            entries, complete = _reachable_writable_entries("/", skip=str(WORKER_ROOT.parent))
        finally:
            timings["verification"] = round(time.monotonic() - started, 6)
    except OSError as error:
        raise WorkerError("cannot fence the worker host") from error
    if entries or not complete:
        first = repr(entries[0])[:300] if entries else "an unlisted entry"
        raise WorkerError(f"host fence left {len(entries)}{'' if complete else ' or more'} world-writable "
                          f"entries reachable on the root filesystem, first {first}")
    authenticate_privileged_host_boundary(boundary)


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
