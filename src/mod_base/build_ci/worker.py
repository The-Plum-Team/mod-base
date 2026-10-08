"""Disposable accounts of one job, ported from protected Block Pops controller code.

The fixed boundary, the two fixed accounts, their closed environment and one fenced execution that
always ends with the account terminated and locked. ``mod_base.build_ci.lifecycle`` composes them
into the ``ci worker-*`` commands. A locked account still runs what the runner starts for it
through ``sudo``: locking only ends every way in from outside. Environment clearing is not an
isolation proof; the host fence and the tool admission are separate.
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mod_base.build_ci.protocol import subject_of, validate_identity, validate_subject
from mod_base.errors import MbError
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.validators import Int, Str, check


WORKER_ROOT = PurePosixPath("/tmp/mod-base-sandbox-boundary/mod-base-worker")
WORKER_ACCOUNTS = {"candidate": "modbase_candidate", "validator": "modbase_validator"}
_HOST_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
             "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
_DISPLAY_ENV = {
    "GALLIUM_DRIVER": ("llvmpipe",), "LIBGL_ALWAYS_SOFTWARE": ("1", "true"),
    "SDL_VIDEO_FORCE_EGL": ("1",), "__GLX_VENDOR_LIBRARY_NAME": ("mesa",),
}
#: Interpreter arguments that precede a dispatcher's own argv inside the account. ``sudo`` opens a
#: session for the account, and the session sets its login umask (002 on Ubuntu: a private group),
#: whatever the runner's own umask is. The private default therefore has to be set by the account's
#: process itself, which then becomes the dispatcher.
_PRIVATE_UMASK = ("-I", "-S", "-B", "-c", "import os,sys;os.umask(0o077);os.execv(sys.argv[1],sys.argv[1:])")
#: Every state of a process that can still run. A zombie (``Z``) only waits to be collected.
_LIVE_STATES = "DIKPRSTWt"
#: The environment name that gives a candidate hook every JDK home its job installed, in the
#: job's order and joined with ``:`` (a tool path holds none). The first one is ``JAVA_HOME``.
JAVA_HOMES_ENVIRONMENT = "MB_JAVA_HOMES"


class WorkerError(MbError):
    """A disposable account or its requested operation fails closed."""

    default_reason = "ci-worker"


@dataclass(frozen=True)
class WorkerAccount:
    role: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True)
class WorkerResult:
    returncode: int | None
    log: bytes
    truncated: bool


class WorkerExecutionError(WorkerError):
    """Execution failed; the bounded log remains available for protected diagnostic rendering."""

    def __init__(self, message: str, result: WorkerResult) -> None:
        super().__init__(message)
        self.result = result


def _path(value: str, label: str) -> str:
    check(isinstance(value, str) and value.startswith("/") and "\\" not in value and ":" not in value
          and not any(ord(character) < 32 or ord(character) == 127 for character in value),
          label, "must be a canonical absolute POSIX path")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as error:
        raise WorkerError("worker tool path is not UTF-8") from error
    check(size <= lim.MAX_CI_TOOL_PATH_BYTES, label, "worker tool path exceeds cap")
    parts = value.split("/")[1:]
    check(bool(parts) and all(part not in {"", ".", ".."} for part in parts), label,
          "must have no empty or traversing components")
    return value


def execution_subject(identity: Any) -> dict[str, Any]:
    """The subject a hook runs for, from what its caller holds.

    ``derive_plan`` runs before any plan exists and has the subject ``ci subject`` authenticated.
    Every later hook has the complete identity of its plan: that is validated in full, as before,
    and reduced to its subject part. Nothing else is accepted.
    """

    check(type(identity) is dict, "$.identity", "must be a subject or the complete identity of a plan")
    try:
        subject = subject_of(identity)
    except KeyError:
        subject = None
    if subject != identity:
        validate_identity(identity)
        return subject_of(identity)
    return validate_subject(subject)


def worker_environment(*, role: str, python: str, java_home: str | None,
                       identity: dict[str, Any], run_id: int, run_attempt: int,
                       values: Mapping[str, str]) -> list[str]:
    """Build an env-i argument vector solely from explicit protected input, never ambient env.

    ``identity`` is the subject or the complete plan identity (:func:`execution_subject`).
    Fixed MB_* identity names are translated by native protected dispatchers when necessary.
    Candidate installation/.pth/user sites stay under its private home/cache. Validator paths
    are separate and PYTHONSAFEPATH/PYTHONNOUSERSITE protect its initial interpreter import roots;
    its fixed protected Python command must additionally use -I and authenticated import roots.
    """

    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    identity = execution_subject(identity)
    Int(1, lim.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, lim.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    _path(python, "$.python")
    if java_home is not None:
        _path(java_home, "$.java_home")
    home = WORKER_ROOT / f"{role}-home"
    temporary = home / "tmp"
    name = WORKER_ACCOUNTS[role]
    paths = [str(PurePosixPath(python).parent), *_HOST_ENV["PATH"].split(":")]
    if java_home is not None:
        paths.insert(1, str(PurePosixPath(java_home) / "bin"))
    environment = {
        "CI": "true", "HOME": str(home), "LOGNAME": name, "USER": name,
        "PATH": ":".join(paths), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
        "SHELL": "/bin/bash", "TERM": "dumb", "TMPDIR": str(temporary),
        "GRADLE_USER_HOME": str(home / "gradle-home"), "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": str(temporary / "pycache"),
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1", "GIT_TERMINAL_PROMPT": "0",
        "MB_TESTED_SHA": identity["tested_sha"], "MB_TESTED_TREE": identity["tested_tree"],
        "MB_REPOSITORY": identity["repository"], "MB_SOURCE_BRANCH": identity["head_branch"],
        "MB_RUN_ID": str(run_id), "MB_RUN_ATTEMPT": str(run_attempt),
    }
    if java_home is not None:
        environment["JAVA_HOME"] = java_home
    if role == "validator":
        environment.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory",
                            "GIT_CONFIG_VALUE_0": str(WORKER_ROOT / "repository")})
    check(isinstance(values, Mapping), "$.values", "must be an explicit environment mapping")
    allowed = {*_DISPLAY_ENV, "SOURCE_DATE_EPOCH", "MB_TARGET_ID", "MB_LANE_ID", "E2E_ROW_JSON", "E2E_SCENARIOS",
               JAVA_HOMES_ENVIRONMENT}
    for key, value in values.items():
        check(isinstance(key, str) and key in allowed, "$.values", "unsupported worker environment name")
        check(isinstance(value, str) and "\0" not in value, f"$.values.{key}", "must be a NUL-free string")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError as error:
            raise WorkerError("worker environment is not UTF-8") from error
        check(size <= lim.MAX_CI_ENV_VALUE_BYTES, f"$.values.{key}", "exceeds per-value environment cap")
        if key in _DISPLAY_ENV:
            Str(choices=_DISPLAY_ENV[key])(value, f"$.values.{key}")
        elif key in {"MB_TARGET_ID", "MB_LANE_ID"}:
            Str(g.CI_UNIT_ID, max_len=80)(value, f"$.values.{key}")
        elif key == "SOURCE_DATE_EPOCH":
            check(value.isascii() and value.isdigit() and len(value) <= 20, f"$.values.{key}",
                  "must be a bounded decimal epoch")
        elif key == JAVA_HOMES_ENVIRONMENT:
            homes = value.split(":")
            check(homes[0] == java_home and len(homes) <= lim.MAX_CI_TOOL_ROOTS and len(set(homes)) == len(homes),
                  f"$.values.{key}", "must list distinct JDK homes, starting with JAVA_HOME")
            for home in homes:
                _path(home, f"$.values.{key}")
        environment[key] = value
    encoded = [f"{key}={value}" for key, value in sorted(environment.items())]
    check(sum(len(value.encode("utf-8")) for value in encoded) <= lim.MAX_CI_ENV_BYTES,
          "$.values", "exceeds total environment cap")
    return encoded


def authenticate_worker_account(role: str) -> WorkerAccount:
    """Bind one dedicated Linux passwd account to its private home and unprivileged group.

    Creation, no-sudo policy, filesystem/process access and fresh-UID lifecycle must additionally
    be established by the allocator and real Linux adversarial tests, not this passwd check.
    """

    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    if sys.platform != "linux":
        raise WorkerError("disposable workers require Linux")
    import pwd

    try:
        record = pwd.getpwnam(WORKER_ACCOUNTS[role])
        check(record.pw_name == WORKER_ACCOUNTS[role]
              and record.pw_uid >= lim.MIN_CI_WORKER_UID and record.pw_gid >= lim.MIN_CI_WORKER_UID
              and record.pw_uid != os.getuid() and record.pw_gid != os.getgid(),
              "$.account", "worker account must differ from the runner and privileged identities")
        home = str(WORKER_ROOT / f"{role}-home")
        check(record.pw_dir == home, "$.account.home", "worker passwd home changed")
        check(set(os.getgrouplist(record.pw_name, record.pw_gid)) == {record.pw_gid},
              "$.account.groups", "worker has unexpected supplementary groups")
        return WorkerAccount(role, record.pw_uid, record.pw_gid, home)
    except (KeyError, OSError) as error:
        raise WorkerError("cannot authenticate disposable account") from error


def worker_account_exists(role: str) -> bool:
    """Whether the fixed account of ``role`` has a passwd entry, authentic or not."""

    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    if sys.platform != "linux":
        raise WorkerError("disposable workers require Linux")
    import pwd

    try:
        pwd.getpwnam(WORKER_ACCOUNTS[role])
    except KeyError:
        return False
    except OSError as error:
        raise WorkerError("cannot establish whether a disposable account exists") from error
    return True


def authenticate_peer_account(account: WorkerAccount, *, runner_uid: int,
                              runner_gid: int) -> WorkerAccount | None:
    """The other fixed account when the job has one, isolated from ``account`` and the runner.

    A job allocates the validator alone or both accounts (``ci worker-prepare --roles``). Only
    root adds or removes a passwd entry, so a missing peer is a fact about the job and not
    something a worker can arrange. Every account that exists must differ in user and group from
    the other one and from the runner.
    """

    check(type(account) is WorkerAccount and account.role in WORKER_ACCOUNTS,
          "$.account", "must be an authenticated worker account")
    other = next(role for role in WORKER_ACCOUNTS if role != account.role)
    peer = authenticate_worker_account(other) if worker_account_exists(other) else None
    present = (account,) if peer is None else (account, peer)
    if ((peer is not None and (peer.uid == account.uid or peer.gid == account.gid))
            or any(entry.uid == runner_uid or entry.gid == runner_gid for entry in present)):
        raise WorkerError("worker identities are not isolated from the runner and from each other")
    return peer


def prepare_worker_boundary(*, runner_environment: str) -> None:
    """Exclusively create the fixed runner-owned traversal root on an admitted hosted Linux runner.

    The protected caller authenticates the runner_environment value. This creates only the
    dedicated root; it does not restrict access to the rest of the host or admit execution.
    """

    if (sys.platform != "linux" or type(runner_environment) is not str
            or runner_environment != "github-hosted"):
        raise WorkerError("worker boundary requires an admitted GitHub-hosted Linux runner")
    # The worker minimum is not a host-runner GID policy. Bind the actual non-root runner
    # identities; fresh disposable users still require MIN_CI_WORKER_UID for UID and GID.
    if os.getuid() == 0 or os.getgid() == 0:
        raise WorkerError("worker boundary requires an unprivileged runner identity")
    import pwd

    for name in WORKER_ACCOUNTS.values():
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        except OSError as error:
            raise WorkerError("cannot establish fresh worker identities") from error
        raise WorkerError("worker identity already exists; refusing boundary reuse")
    descriptor = None
    try:
        descriptor = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                             | getattr(os, "O_CLOEXEC", 0))
        for name in (WORKER_ROOT.parent.name, WORKER_ROOT.name):
            os.mkdir(name, mode=0o711, dir_fd=descriptor)
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                            | getattr(os, "O_CLOEXEC", 0), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()
                        or opened.st_gid != os.getgid()
                        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                    raise WorkerError("worker boundary creation identity changed")
                os.fchmod(child, 0o711)
                os.fsync(child)
                os.fsync(descriptor)
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
    except OSError as error:
        raise WorkerError("cannot create a fresh private worker boundary") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def allocate_worker_account(role: str) -> WorkerAccount:
    """Create one fresh fixed no-sudo identity and its private home/tmp/cache directories.

    The dedicated traversal boundary must already be runner-owned and privately prepared.
    Existing identities/homes are never reused. Allocation does not establish host filesystem
    isolation, stage source/overlays/caches or authorize execution. Failures forbid execution.
    """

    if sys.platform != "linux":
        raise WorkerError("disposable account allocation requires Linux")
    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    import pwd

    name = WORKER_ACCOUNTS[role]
    try:
        pwd.getpwnam(name)
    except KeyError:
        pass
    except OSError as error:
        raise WorkerError("cannot establish disposable account absence") from error
    else:
        raise WorkerError("disposable account already exists; refusing reuse")
    boundary = Path(str(WORKER_ROOT.parent))
    root = Path(str(WORKER_ROOT))
    home = root / f"{role}-home"
    account = None
    created = False
    try:
        for directory in (boundary, root):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o711):
                raise WorkerError("worker boundary must be a runner-owned traversal directory")
        home.mkdir(mode=0o700)  # Exclusive: do not adopt an existing home, including a link.
        children = (home / "tmp", home / "gradle-home")
        for directory in children:
            directory.mkdir(mode=0o700)
        created = True  # The control command can fail after passwd/group creation.
        _control(("/usr/bin/sudo", "-n", "/usr/sbin/useradd", "--no-create-home", "--home-dir", str(home),
                  "--shell", "/bin/bash", "--user-group", name),
                 timeout=lim.CI_TERMINATION_GRACE_SECONDS, accepted=frozenset({0}))
        account = authenticate_worker_account(role)
        # sudo -l succeeds even for a limited NOPASSWD rule. Reject any admitted sudo policy;
        # never run a privileged probe whose success would itself cross the boundary.
        _control(("/usr/bin/sudo", "-n", "--user", f"#{account.uid}", "--", "/usr/bin/sudo", "-n", "--list"),
                 timeout=lim.CI_TERMINATION_GRACE_SECONDS, accepted=frozenset({1}))
        for directory in (*children, home):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise WorkerError("worker home directory changed during allocation")
            # Fresh fixed children only; never recursively chown candidate-controlled trees.
            _control(("/usr/bin/sudo", "-n", "/usr/bin/chown", "--no-dereference", "--",
                      f"{account.uid}:{account.gid}", str(directory)),
                     timeout=lim.CI_TERMINATION_GRACE_SECONDS, accepted=frozenset({0}))
            after = directory.lstat()
            if (after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_mode) != (
                    info.st_dev, info.st_ino, account.uid, account.gid, info.st_mode):
                raise WorkerError("worker home ownership did not bind the allocated identity")
        check(authenticate_worker_account(role) == account, "$.account", "worker identity changed during allocation")
        return account
    except (MbError, OSError, ValueError):
        if account is not None:
            terminate_worker(account)
        elif created:
            # An unexpected privileged/group identity must never be targeted by a UID kill.
            try:
                pwd.getpwnam(name)
            except KeyError:
                pass
            else:
                lock_worker_account(role)
        raise WorkerError("disposable account allocation failed; execution is forbidden") from None


def lock_worker_account(role: str) -> None:
    """Lock and expire the fixed account of ``role`` by name; no process is signalled.

    For an account whose passwd entry cannot be authenticated: its UID may belong to something
    else, so it must never be the target of a kill.
    """

    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    _control(("/usr/bin/sudo", "-n", "/usr/sbin/usermod", "--lock", "--expiredate", "1970-01-02",
              WORKER_ACCOUNTS[role]), timeout=lim.CI_TERMINATION_GRACE_SECONDS, accepted=frozenset({0}))


def _control(command: tuple[str, ...], *, timeout: float, accepted: frozenset[int],
             cwd: Path | None = None) -> bytes:
    """Only fixed administrative arguments reach this helper; never a candidate command."""

    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=dict(_HOST_ENV),
                                   **({"cwd": cwd} if cwd is not None else {}))
        if process.stdout is None:
            raise WorkerError("disposable-account control pipe unavailable")
        deadline = time.monotonic() + timeout
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("disposable-account control command timed out")
                if not selector.select(remaining):
                    raise WorkerError("disposable-account control command timed out")
                chunk = os.read(process.stdout.fileno(), lim.MAX_CI_CONTROL_OUTPUT_BYTES - len(output) + 1)
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > lim.MAX_CI_CONTROL_OUTPUT_BYTES:
                    raise WorkerError("disposable-account control output exceeds cap")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) not in accepted:
            raise WorkerError("disposable-account control command rejected")
        return bytes(output)
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkerError("disposable-account control command failed") from error
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=lim.CI_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError) as error:
                raise WorkerError("disposable-account control process did not reap") from error
            finally:
                if process.stdout is not None:
                    process.stdout.close()


def terminate_worker(account: WorkerAccount) -> None:
    """Repeat UID kill sweeps, lock/expire the user, then verify post-lock quiescence.

    A surviving process or control failure is fatal and forbids sealing/upload. This preserves
    native BP's double sweep and bounded asynchronous JVM teardown behavior.
    """

    check(isinstance(account, WorkerAccount), "$.account", "must be an authenticated worker account")
    check(authenticate_worker_account(account.role) == account, "$.account", "worker account identity changed")
    uid = str(account.uid)
    deadline = time.monotonic() + lim.CI_TERMINATION_GRACE_SECONDS

    def sweep() -> None:
        while True:
            def control(command: tuple[str, ...]) -> bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("worker termination grace expired")
                return _control(command, timeout=remaining, accepted=frozenset({0, 1}))

            for _ in range(2):
                control(("/usr/bin/sudo", "-n", "/usr/bin/pkill", "-KILL", "-u", uid))
                control(("/usr/bin/sudo", "-n", "/usr/bin/pkill", "-KILL", "-U", uid))
            effective = control(("/usr/bin/pgrep", "-u", uid)).strip()
            real = control(("/usr/bin/pgrep", "-U", uid)).strip()
            if not (effective or real):
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("worker processes survived termination")
            time.sleep(min(lim.CI_TERMINATION_POLL_SECONDS, remaining))

    try:
        sweep()
    finally:
        lock_worker_account(account.role)
    # Locking is not proof of quiescence. A process may have appeared after the last pre-lock
    # observation; kill/check again while retaining the original whole-sweep deadline.
    sweep()


def worker_processes(account: WorkerAccount) -> bool:
    """Whether the account still owns a process that can run, by real or effective user.

    Observation only: nothing is signalled. A zombie does not count; it runs nothing and only
    waits for init to collect it.
    """

    check(type(account) is WorkerAccount, "$.account", "must be an authenticated worker account")
    check(authenticate_worker_account(account.role) == account, "$.account", "worker account identity changed")
    return any(_control(("/usr/bin/pgrep", "--runstates", _LIVE_STATES, selector, str(account.uid)),
                        timeout=lim.CI_TERMINATION_GRACE_SECONDS, accepted=frozenset({0, 1})).strip()
               for selector in ("-u", "-U"))


def _execution_command(command: tuple[str, ...], python: str, role: str) -> Path:
    check(isinstance(command, tuple) and 4 <= len(command) <= lim.MAX_CI_COMMAND_ARGUMENTS,
          "$.command", "requires a bounded fixed Python dispatcher argv")
    total = 0
    for argument in command:
        check(isinstance(argument, str) and "\0" not in argument, "$.command", "argv must contain NUL-free strings")
        try:
            total += len(argument.encode("utf-8")) + 1
        except UnicodeError as error:
            raise WorkerError("worker command is not UTF-8") from error
        check(total <= lim.MAX_CI_COMMAND_BYTES, "$.command", "argv exceeds its byte cap")
    _path(python, "$.python")
    check(command[:3] == (python, "-I", "-B"), "$.command", "dispatcher requires isolated Python without bytecode writes")
    _path(command[3], "$.command.dispatcher")
    logical_cwd = WORKER_ROOT / ("repository" if role == "candidate" else "controller")
    cwd = Path(str(logical_cwd))
    prefix = str(logical_cwd / "scripts" / "ci") + "/"
    check(command[3].startswith(prefix) and command[3].endswith(".py"), "$.command.dispatcher",
          "dispatcher must be inside the role's fixed scripts/ci root")
    return cwd


def execute_worker(account: WorkerAccount, *, command: tuple[str, ...], python: str,
                   java_home: str | None, identity: dict[str, Any], run_id: int, run_attempt: int,
                   values: Mapping[str, str], timeout_seconds: int) -> WorkerResult:
    """Execute one protected-selected dispatcher and always terminate/lock its entire UID.

    Only a zero exit of a dispatcher that left no process behind, after successful account
    quiescence, returns. Failures/timeouts retain a bounded diagnostic result on
    WorkerExecutionError. No process or raw log authorizes export. The dispatcher starts with a
    umask of 077, so what it creates is private unless it decides otherwise.
    Caller must first authenticate/copy the dispatcher/import closure and establish the private
    filesystem boundary.
    """

    check(isinstance(account, WorkerAccount), "$.account", "must be an authenticated worker account")
    check(authenticate_worker_account(account.role) == account, "$.account", "worker identity changed")
    process = None
    previous = {}
    captured = bytearray()
    truncated = False
    code = None
    terminated = False
    abandoned = False

    def interrupted(signum: int, _frame: Any) -> None:
        raise WorkerError(f"worker interrupted by signal {signum}")

    def settle() -> None:
        # The launcher has gone, so whatever the UID still runs was left behind by the hook.
        # Look before the sweep kills it: the contract fails such a hook instead of tidying up.
        nonlocal abandoned, terminated
        abandoned = worker_processes(account)
        terminate_worker(account)
        terminated = True

    try:
        Int(1, lim.MAX_CI_WORKER_TIMEOUT_SECONDS)(timeout_seconds, "$.timeout_seconds")
        cwd = _execution_command(command, python, account.role)
        environment = worker_environment(role=account.role, python=python, java_home=java_home,
                                         identity=identity, run_id=run_id, run_attempt=run_attempt, values=values)
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupted)
        deadline = time.monotonic() + timeout_seconds
        process = subprocess.Popen(
            ("/usr/bin/sudo", "-n", "--user", f"#{account.uid}", "--", "/usr/bin/setpriv",
             "--no-new-privs", "--", "/usr/bin/env", "-i", f"--chdir={cwd.as_posix()}", *environment,
             python, *_PRIVATE_UMASK, *command),
            cwd=Path(str(WORKER_ROOT)), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(_HOST_ENV), close_fds=True)
        if process.stdout is None:
            raise WorkerError("worker output pipe unavailable")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                if deadline - time.monotonic() <= 0:
                    raise WorkerError("worker execution timed out")
                if code is None:
                    code = process.poll()
                    if code is not None:
                        # A detached child can retain stdout after its dispatcher exits. Kill
                        # that UID now rather than waiting for EOF until the workload timeout.
                        settle()
                        deadline = time.monotonic() + lim.CI_TERMINATION_GRACE_SECONDS
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("worker execution timed out")
                if not selector.select(min(remaining, lim.CI_TERMINATION_POLL_SECONDS)):
                    continue
                chunk = os.read(process.stdout.fileno(), lim.CI_PROCESS_READ_BYTES)
                if not chunk:
                    break
                available = lim.MAX_CI_LOG_BYTES - len(captured)
                captured.extend(chunk[:available])
                truncated = truncated or len(chunk) > available
                # Continue draining after the cap; a chatty worker must never block on our pipe.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerError("worker execution timed out")
        code = process.wait(timeout=remaining)
        if not terminated:
            settle()  # The output ended before the exit was observed.
        if code != 0:
            raise WorkerError("worker dispatcher returned failure")
        if abandoned:
            raise WorkerError("worker dispatcher left a process behind")
    except (MbError, OSError, subprocess.SubprocessError, ValueError) as error:
        # Structural validators may quote hostile values. Keep those out of runner command
        # parsing; protected fixed WorkerError diagnostics contain no candidate text.
        message = str(error) if isinstance(error, WorkerError) else "worker execution rejected"
        raise WorkerExecutionError(message, WorkerResult(code, bytes(captured), truncated)) from error
    finally:
        try:
            # Once cleanup starts, repeated cancellation signals cannot interrupt UID locking.
            for signum in previous:
                signal.signal(signum, signal.SIG_IGN)
            # UID sweeps include detached grandchildren and occur on success, error and signals.
            if not terminated:
                terminate_worker(account)
        finally:
            try:
                if process is not None:
                    try:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=lim.CI_TERMINATION_GRACE_SECONDS)
                    except (OSError, subprocess.SubprocessError) as error:
                        raise WorkerError("worker launcher did not reap") from error
                    finally:
                        if process.stdout is not None:
                            process.stdout.close()
            finally:
                for signum, handler in previous.items():
                    signal.signal(signum, handler)
    return WorkerResult(code, bytes(captured), truncated)


def render_worker_log(result: WorkerResult, *, role: str) -> str:
    """Neutralize modern/legacy Actions command markers and terminal controls on every line."""

    check(isinstance(result, WorkerResult) and isinstance(result.log, bytes)
          and len(result.log) <= lim.MAX_CI_LOG_BYTES and type(result.truncated) is bool,
          "$.result", "must contain a bounded worker log")
    check(isinstance(role, str) and role in WORKER_ACCOUNTS, "$.role", "unknown worker role")
    lines = []
    for line in result.log.decode("utf-8", "replace").splitlines():
        safe = "".join(character if character == "\t" or (32 <= ord(character) < 127 or ord(character) >= 160)
                       else "?" for character in line)
        # Legacy ##[ commands are recognized even after a visible prefix. Escape both
        # grammars, including whole colon runs so replacement cannot recreate adjacent ::.
        safe = re.sub(r":{2,}", lambda match: " ".join(match.group()), safe.replace("##[", "# #["))
        lines.append(f"[{role}] {safe}\n")
    if result.truncated:
        lines.append(f"[{role}] output exceeded the bounded capture and was truncated\n")
    return "".join(lines)
