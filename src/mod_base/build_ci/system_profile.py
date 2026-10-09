"""Kit-defined system profiles: the image packages a packaged lane needs, installed before the fence.

A packaged lane runs Minecraft clients under Xvfb with Mesa software rendering. Natively both mods
install those packages with ``sudo apt-get`` before their run; in a kit lane the candidate runs as
a disposable account without sudo, so the kit installs them first. A mod's protected Build config
may name one profile (``runtime.system_profile``); it never lists packages. Each profile is one
constant tuple of Ubuntu 24.04 package names, ``protocol.SYSTEM_PROFILES``.

:func:`install_system_profile` runs the image's package manager as root through ``sudo -n`` with
fixed arguments and a fixed environment (no token, no variable of the job), each command bounded by
root's own ``timeout`` and by a deadline here, its output bounded. No kit code runs as root: the
two command lines are the whole root surface, a narrower one than an operation of the privileged
bootstrap, which would run kit Python as root to start the same package manager. It refuses once a
worker account exists, like the host fence: whatever root installs must be in place before
``ci worker-prepare`` fences the image and admits its tools, and before any account could have
written anything root would read. It makes no API request.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Callable

from mod_base.build_ci.protocol import SYSTEM_PROFILES
from mod_base.build_ci.worker import WORKER_ACCOUNTS, neutral_log_line
from mod_base.errors import MbError, single_line
from mod_base.model import limits

#: The one way to root; ``ci system-profile`` passes it to :func:`install_system_profile`.
SUDO = "/usr/bin/sudo"
_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
#: What reaches ``sudo`` and, rebuilt from nothing by ``env -i``, the package manager as root.
_ENVIRONMENT = {"PATH": _PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
_ROOT_ENVIRONMENT = ("DEBIAN_FRONTEND=noninteractive", f"PATH={_PATH}", "LANG=C.UTF-8", "LC_ALL=C.UTF-8")
#: The exit statuses of root's ``timeout`` when the command outlived its bound: 124 when it ended
#: on TERM, 128 + SIGKILL when it had to be killed after the grace.
_TIMED_OUT = (124, 137)


class SystemProfileError(MbError):
    """The system profile cannot be installed, or not now (exit 2)."""

    default_reason = "ci-system-profile"


def system_profile_commands(name: str, *, sudo: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The two root commands, started through ``sudo``, that install the profile ``name``: refresh
    the package lists, then install exactly its packages without recommendations."""

    try:
        packages = SYSTEM_PROFILES[name]
    except KeyError:
        raise SystemProfileError(f"unknown system profile {name!r}") from None
    root = (sudo, "-n", "--", "/usr/bin/timeout", f"--kill-after={int(limits.CI_TERMINATION_GRACE_SECONDS)}s",
            f"{limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS}s", "/usr/bin/env", "-i", *_ROOT_ENVIRONMENT,
            "/usr/bin/apt-get", "-q", "-o", f"DPkg::Lock::Timeout={limits.CI_SYSTEM_PROFILE_LOCK_WAIT_SECONDS}",
            "-o", f"Acquire::Retries={limits.CI_SYSTEM_PROFILE_FETCH_RETRIES}")
    return (*root, "update"), (*root, "install", "--yes", "--no-install-recommends", *packages)


def existing_worker_accounts() -> tuple[str, ...]:
    """The names of the kit's worker accounts that exist on this host, in role order."""

    import pwd

    found = []
    for name in WORKER_ACCOUNTS.values():
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        found.append(name)
    return tuple(found)


def _collect(process: subprocess.Popen, deadline: float, keep: Callable[[bytes], None]) -> int | None:
    """Read the command's output to its end and return its exit status, or ``None`` when this
    process's deadline passed first."""

    if process.stdout is None:
        raise OSError("no output pipe")
    with selectors.DefaultSelector() as selector:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return None
            chunk = os.read(process.stdout.fileno(), limits.CI_PROCESS_READ_BYTES)
            if not chunk:
                break
            keep(chunk)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return process.poll()
    try:
        return process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        return None


def _stop(process: subprocess.Popen) -> str | None:
    """End a command still running: TERM first, which ``sudo`` relays to root's ``timeout`` and on
    to the package manager, then KILL after the grace. Returns why it did not stop, or ``None``."""

    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
    except OSError as error:
        return error.strerror or str(error)
    except subprocess.SubprocessError as error:
        return str(error)
    finally:
        if process.stdout is not None:
            process.stdout.close()
    return None


def _run(name: str, phase: str, command: tuple[str, ...], log: Callable[[str], object]) -> None:
    """Run one root command to its end within the bound; keep the last bytes of its output and
    show them whenever it fails, a timeout included."""

    started = time.monotonic()
    # Root's timeout stops the command at the bound and kills it after the grace; this process
    # waits one grace longer for that to be reported, then stops it itself.
    deadline = started + limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS + 2 * limits.CI_TERMINATION_GRACE_SECONDS
    tail, truncated = bytearray(), False

    def keep(chunk: bytes) -> None:
        nonlocal truncated
        tail.extend(chunk)
        if len(tail) > limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES:
            del tail[:len(tail) - limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES]
            truncated = True

    def failure(message: str) -> SystemProfileError:
        return SystemProfileError(f"system profile {name}: apt-get {phase} {message} "
                                  f"(elapsed {time.monotonic() - started:.0f}s)")

    def show() -> str:
        lines = tail.decode("utf-8", "replace").splitlines()
        if truncated and lines:
            lines = lines[1:]  # The first kept line may be cut.
            log(f"[apt-get {phase}] ... earlier output not kept\n")
        for line in lines:
            log(f"[apt-get {phase}] {neutral_log_line(line)}\n")
        last = next((line for line in reversed(lines) if line.strip()), "")
        return single_line(neutral_log_line(last), limit=300)

    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=dict(_ENVIRONMENT), cwd="/", close_fds=True)
    except OSError as error:
        raise failure(f"could not start: {error.strerror or error}") from error
    status, problem = None, None
    try:
        status = _collect(process, deadline, keep)
        if status is None:
            problem = "timed out"
    except OSError as error:
        problem = f"could not complete: {error.strerror or error}"
    finally:
        stopped = _stop(process)
    if stopped is not None:
        problem = f"{problem or 'ended'}, and it did not stop: {stopped}"
    if problem is None and status in _TIMED_OUT:
        problem = f"timed out after its bound of {limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS}s"
    if problem is not None:
        show()
        raise failure(problem)
    if status != 0:
        raise failure(f"exited with status {status}: {show() or 'no output'}")


def install_system_profile(name: str | None, *, log: Callable[[str], object], sudo: str) -> int:
    """Install the system profile ``name`` as root through ``sudo`` and return how many packages it
    names; ``None`` (the config names no profile) installs nothing and returns 0.

    Either way it refuses once a worker account exists. Package lists are refreshed first, then
    exactly the profile's packages are installed without recommendations. A command that cannot
    start, exits non-zero or outlives ``CI_SYSTEM_PROFILE_TIMEOUT_SECONDS`` is a rejection that
    names its phase; the last ``MAX_CI_SYSTEM_PROFILE_LOG_BYTES`` of its output go to ``log``,
    each line neutralised.
    """

    existing = existing_worker_accounts()
    if existing:
        raise SystemProfileError("a system profile must be installed before any worker account exists; "
                                 f"found {', '.join(existing)}")
    if name is None:
        return 0
    for phase, command in zip(("update", "install"), system_profile_commands(name, sudo=sudo)):
        _run(name, phase, command, log)
    return len(SYSTEM_PROFILES[name])
