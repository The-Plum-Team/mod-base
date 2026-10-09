"""Kit-defined system profiles: the image packages a packaged lane needs, installed before the fence.

A packaged lane runs Minecraft clients under Xvfb with Mesa software rendering. Natively both mods
install those packages with ``sudo apt-get`` before their run; in a kit lane the candidate runs as
a disposable account without sudo, so the kit installs them first. A mod's protected Build config
may name one profile (``runtime.system_profile``); it never lists packages. Each profile is one
constant tuple of Ubuntu 24.04 package names here.

:func:`install_system_profile` runs the image's package manager as root through ``sudo -n`` with
fixed arguments and a fixed environment (no token, no variable of the job), each command bounded by
root's own ``timeout`` and by a deadline here, its output bounded. It refuses once a worker account
exists, like the host fence: whatever root installs must be in place before ``ci worker-prepare``
fences the image and admits its tools, and before any account could have written anything root
would read. It makes no API request.
"""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from collections.abc import Callable

from mod_base.build_ci.worker import WORKER_ACCOUNTS, neutral_log_line
from mod_base.errors import MbError, single_line
from mod_base.model import limits

#: Profile name -> the Ubuntu 24.04 packages it installs, sorted. ``xvfb-mesa`` is the union of the
#: two native software-rendering installs (``.github/actions/run-packaged-e2e/action.yml`` of Quick
#: Skin and of Block Pops): Quick Skin's sixteen packages, which Block Pops also installs, and Block
#: Pops' ``libegl1`` and ``libegl-mesa0``.
SYSTEM_PROFILES: dict[str, tuple[str, ...]] = {
    "xvfb-mesa": (
        "libasound2t64", "libegl-mesa0", "libegl1", "libgl1-mesa-dri", "libglx-mesa0", "libopenal1",
        "libx11-6", "libxcursor1", "libxext6", "libxi6", "libxinerama1", "libxrandr2", "libxrender1",
        "libxtst6", "libxxf86vm1", "mesa-utils", "xauth", "xvfb",
    ),
}
#: The one way to root. Tests point it at a recording stand-in.
SUDO = "/usr/bin/sudo"
_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
#: What reaches ``sudo`` and, rebuilt from nothing by ``env -i``, the package manager as root.
_ENVIRONMENT = {"PATH": _PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
_ROOT_ENVIRONMENT = ("DEBIAN_FRONTEND=noninteractive", f"PATH={_PATH}", "LANG=C.UTF-8", "LC_ALL=C.UTF-8")
#: The exit status of ``timeout`` when the command outlived its bound.
_TIMED_OUT = 124


class SystemProfileError(MbError):
    """The system profile cannot be installed, or not now (exit 2)."""

    default_reason = "ci-system-profile"


def system_profile_commands(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The two root commands that install the profile ``name``: refresh the package lists, then
    install exactly its packages without recommendations."""

    try:
        packages = SYSTEM_PROFILES[name]
    except KeyError:
        raise SystemProfileError(f"unknown system profile {name!r}") from None
    root = (SUDO, "-n", "--", "/usr/bin/timeout", f"--kill-after={int(limits.CI_TERMINATION_GRACE_SECONDS)}s",
            f"{limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS}s", "/usr/bin/env", "-i", *_ROOT_ENVIRONMENT,
            "/usr/bin/apt-get", "-q")
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


def _run(name: str, phase: str, command: tuple[str, ...], log: Callable[[str], object]) -> None:
    """Run one root command to its end within the bound; keep the last bytes of its output and
    show them when it fails."""

    started = time.monotonic()
    # Root's timeout stops the command at the bound and kills it after the grace; this process
    # waits one grace longer for that to be reported.
    deadline = started + limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS + 2 * limits.CI_TERMINATION_GRACE_SECONDS
    tail, truncated = bytearray(), False

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

    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=dict(_ENVIRONMENT), cwd="/", close_fds=True)
        if process.stdout is None:
            raise failure("could not start: no output pipe")
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise failure("timed out")
                chunk = os.read(process.stdout.fileno(), limits.CI_PROCESS_READ_BYTES)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES:
                    del tail[:len(tail) - limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES]
                    truncated = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise failure("timed out")
        status = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        raise failure("timed out") from None
    except OSError as error:
        raise failure(f"could not complete: {error.strerror or error}") from error
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=limits.CI_TERMINATION_GRACE_SECONDS)
            except (OSError, subprocess.SubprocessError) as error:
                raise failure("did not stop") from error
            finally:
                if process.stdout is not None:
                    process.stdout.close()
    if status == _TIMED_OUT:
        show()
        raise failure(f"timed out after its bound of {limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS}s")
    if status != 0:
        raise failure(f"exited with status {status}: {show() or 'no output'}")


def install_system_profile(name: str | None, *, log: Callable[[str], object]) -> int:
    """Install the system profile ``name`` as root and return how many packages it names; ``None``
    (the config names no profile) installs nothing and returns 0.

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
    for phase, command in zip(("update", "install"), system_profile_commands(name)):
        _run(name, phase, command, log)
    return len(SYSTEM_PROFILES[name])
