"""Root entry of the Build worker lifecycle: verify the kit checkout, then run one closed operation.

The runner starts this program, and nothing else, as root::

    /usr/bin/sudo -n -- <python> -I -B -S <kit>/tools/ci_privileged_bootstrap.py \
        --operation <name> --kit <kit> --kit-digest sha256:<hex> --nonce <hex>

``<kit>`` is the kit checkout the job prologue verified against the pinned digest literal. Before
importing anything from it this program re-computes kit-digest-v1 of that checkout and compares it
with the digest it is given, so root never imports bytes other than the ones the prologue admitted.
Until then it uses the standard library only; module initialisation imports no kit code. The
operation is one of a closed set and is carried out by ``mod_base.build_ci.root_request_operations``,
which reads everything else from the runner's private request below the worker root.

The constants below deliberately mirror the kit's grammar and limits: importing them would defeat
the guard. ``tests/test_ci_privileged_bootstrap.py`` keeps both sides equal.
"""

from __future__ import annotations

import hashlib
import importlib.util
import itertools
import os
import re
import signal
import stat
import sys
from pathlib import Path
from types import ModuleType


RUNNER_HOME = "/home/runner"
REPOSITORY = "The-Plum-Team/mod-base"
PROGRAM = "tools/ci_privileged_bootstrap.py"
ROOTS = ("src", "site", "requirements")
OPERATIONS = ("host-fence", "freeze-build-validation", "freeze-runtime-validation")
ENTRY_FLAGS = ("--operation", "--kit", "--kit-digest", "--nonce")
MAX_FILES = 20000
MAX_ENTRIES = 40000
MAX_BYTES = 512 * 1024 * 1024
MAX_DEPTH = 64
MAX_UNIX_ID = (1 << 32) - 2
MAX_ARGUMENT_BYTES = 4096
READ_BYTES = 65536
TIMEOUT_SECONDS = 1800
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
NONCE = re.compile(r"[0-9a-f]{64}", re.ASCII)
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", re.ASCII)
NAME = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
_DIRECTORY = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class BootstrapError(Exception):
    """Pre-import rejection; importing MbError before admission is forbidden."""


def _require(condition: bool) -> None:
    if not condition:
        raise BootstrapError("root bootstrap admission failed")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _protected(info: os.stat_result, runner_uid: int, *, directory: bool) -> None:
    """Only root or the runner may own a kit entry, and nobody else may write it."""
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid in (0, runner_uid) and not info.st_mode & 0o022
             and (directory or (info.st_nlink == 1 and not info.st_mode & 0o111)))


def _entry_arguments(arguments: list[str]) -> dict[str, str]:
    _require(type(arguments) is list and len(arguments) == 2 * len(ENTRY_FLAGS))
    for value in arguments:
        _require(type(value) is str and len(value) <= MAX_ARGUMENT_BYTES)
        try:
            _require(len(value.encode("utf-8")) <= MAX_ARGUMENT_BYTES)
        except UnicodeError as error:
            raise BootstrapError("invalid root bootstrap argument") from error
        _require(not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))
    _require(tuple(arguments[::2]) == ENTRY_FLAGS)
    values = dict(zip(ENTRY_FLAGS, arguments[1::2]))
    kit = values["--kit"]
    _require(values["--operation"] in OPERATIONS
             and DIGEST.fullmatch(values["--kit-digest"]) is not None
             and NONCE.fullmatch(values["--nonce"]) is not None
             and kit.startswith(RUNNER_HOME + "/") and "\\" not in kit and ":" not in kit
             and all(part not in ("", ".", "..") for part in kit.split("/")[1:]))
    return values


def _runner_uid() -> int:
    """The passwd owner of the fixed runner home; never an argument."""
    import pwd

    info = os.stat(RUNNER_HOME, follow_symlinks=False)
    _require(stat.S_ISDIR(info.st_mode) and 1 <= info.st_uid <= MAX_UNIX_ID)
    account = pwd.getpwuid(info.st_uid)
    _require((account.pw_uid, account.pw_gid, account.pw_dir) == (info.st_uid, info.st_gid, RUNNER_HOME))
    return info.st_uid


def _open_kit(kit: str, runner_uid: int) -> int:
    """Open the checkout through protected directories only, refusing a link at every step."""
    fd = os.open("/", _DIRECTORY)
    try:
        _protected(os.fstat(fd), runner_uid, directory=True)
        for name in kit.split("/")[1:]:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _protected(before, runner_uid, directory=True)
            child = os.open(name, _DIRECTORY, dir_fd=fd)
            try:
                _require(_stamp(os.fstat(child)) == _stamp(before))
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _names(fd: int, cap: int) -> list[str]:
    with os.scandir(fd) as entries:
        names = [entry.name for entry in itertools.islice(entries, cap + 1)]
    _require(len(names) <= cap)
    return sorted(names)


def _file_digest(parent: int, name: str, cap: int, runner_uid: int) -> tuple[str, int]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _protected(before, runner_uid, directory=False)
    _require(0 <= before.st_size <= cap)
    fd = os.open(name, _FILE, dir_fd=parent)
    try:
        _require(_stamp(os.fstat(fd)) == _stamp(before))
        total, digest = 0, hashlib.sha256()
        while True:
            chunk = os.read(fd, min(READ_BYTES, cap - total + 1))
            if not chunk:
                break
            total += len(chunk)
            _require(total <= cap)
            digest.update(chunk)
        _require(total == before.st_size and _stamp(os.fstat(fd)) == _stamp(before)
                 and _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _stamp(before))
        return digest.hexdigest(), total
    finally:
        os.close(fd)


def _kit_digest(kit_fd: int, runner_uid: int) -> str:
    """kit-digest-v1 of the opened checkout: the same listing ``mod_base.pin`` hashes."""
    entries, total, lines = 0, 0, []

    def walk(parent: int, prefix: str, depth: int) -> None:
        nonlocal entries, total
        _require(depth <= MAX_DEPTH)
        initial = os.fstat(parent)
        _protected(initial, runner_uid, directory=True)
        names = _names(parent, MAX_ENTRIES - entries)
        entries += len(names)
        for name in names:
            _require(NAME.fullmatch(name) is not None and name not in {".", "..", "__pycache__"}
                     and not name.endswith((".pyc", ".pyo", ".pth")))
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            path = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _DIRECTORY, dir_fd=parent)
                try:
                    _require(_stamp(os.fstat(child)) == _stamp(info))
                    walk(child, path + "/", depth + 1)
                    _require(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
                             == _stamp(os.fstat(child)))
                finally:
                    os.close(child)
            else:
                _require(len(lines) < MAX_FILES)
                sha, size = _file_digest(parent, name, MAX_BYTES - total, runner_uid)
                total += size
                lines.append(("./" + path, sha))
        _require(_stamp(os.fstat(parent)) == _stamp(initial))

    for top in ROOTS:
        info = os.stat(top, dir_fd=kit_fd, follow_symlinks=False)
        _protected(info, runner_uid, directory=True)
        child = os.open(top, _DIRECTORY, dir_fd=kit_fd)
        try:
            _require(_stamp(os.fstat(child)) == _stamp(info))
            walk(child, top + "/", 0)
        finally:
            os.close(child)
    _require(bool(lines))
    listing = "".join(f"{sha}  {path}\n" for path, sha in sorted(lines)).encode("ascii")
    return "sha256:" + hashlib.sha256(listing).hexdigest()


def _require_isolated_root() -> None:
    _require(sys.platform == "linux" and os.getuid() == 0 and os.geteuid() == 0
             and os.getgid() == 0 and os.getegid() == 0
             and bool(sys.flags.isolated) and bool(sys.flags.no_site) and bool(sys.flags.dont_write_bytecode))


def authenticate_kit(kit: str, expected_digest: str) -> None:
    """Require real root, an isolated interpreter and a protected checkout of exactly this digest.

    The running program must be the checkout's own ``tools/ci_privileged_bootstrap.py``; the kit's
    staged-file lock, verified after import, then binds this file's bytes to the same digest.
    """
    _require_isolated_root()
    _require(type(kit) is str and type(expected_digest) is str and DIGEST.fullmatch(expected_digest) is not None)
    kit_fd = None
    try:
        runner_uid = _runner_uid()
        kit_fd = _open_kit(kit, runner_uid)
        initial = _stamp(os.fstat(kit_fd))
        tools = os.stat("tools", dir_fd=kit_fd, follow_symlinks=False)
        _protected(tools, runner_uid, directory=True)
        tools_fd = os.open("tools", _DIRECTORY, dir_fd=kit_fd)
        try:
            program = os.stat(PROGRAM.split("/")[1], dir_fd=tools_fd, follow_symlinks=False)
            _protected(program, runner_uid, directory=False)
            running = os.stat(__file__, follow_symlinks=False)
            _require((running.st_dev, running.st_ino) == (program.st_dev, program.st_ino)
                     and _stamp(os.fstat(tools_fd)) == _stamp(tools))
        finally:
            os.close(tools_fd)
        _require(_kit_digest(kit_fd, runner_uid) == expected_digest)
        named = _open_kit(kit, runner_uid)
        try:
            _require(_stamp(os.fstat(named)) == initial and _stamp(os.fstat(kit_fd)) == initial)
        finally:
            os.close(named)
    except (OSError, KeyError, ValueError, UnicodeError, RecursionError) as error:
        raise BootstrapError("cannot authenticate the kit checkout before import") from error
    finally:
        if kit_fd is not None:
            os.close(kit_fd)


def load_kit(kit: str, expected_digest: str) -> ModuleType:
    """Admit the checkout's bytes, load only its package, and admit the bytes again.

    No search path is edited: the package is loaded from its exact file location, so no other
    ``mod_base`` and no candidate directory can satisfy an import.
    """
    _require(not any(name == "mod_base" or name.startswith("mod_base.") for name in sys.modules))
    original = tuple(sys.path)
    authenticate_kit(kit, expected_digest)
    root = Path(kit) / "src" / "mod_base"
    try:
        spec = importlib.util.spec_from_file_location("mod_base", root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        _require(spec is not None and spec.loader is not None)
        module = importlib.util.module_from_spec(spec)
        sys.modules["mod_base"] = module
        spec.loader.exec_module(module)
        version = getattr(module, "__version__", None)
        _require(sys.modules.get("mod_base") is module and tuple(sys.path) == original
                 and type(version) is str and VERSION.fullmatch(version) is not None
                 and getattr(module, "KIT_REPOSITORY", None) == REPOSITORY)
        for name, loaded in tuple(sys.modules.items()):
            if name == "mod_base" or name.startswith("mod_base."):
                origin = getattr(loaded, "__file__", None)
                _require(type(origin) is str and Path(origin).is_relative_to(root))
        authenticate_kit(kit, expected_digest)
        return module
    except BaseException as error:
        # Never leave a partly initialized/rejected package available to a retry or caller.
        for name in tuple(sys.modules):
            if name == "mod_base" or name.startswith("mod_base."):
                del sys.modules[name]
        if isinstance(error, (BootstrapError, KeyboardInterrupt, SystemExit)):
            raise
        raise BootstrapError("cannot load the admitted kit checkout") from error


def main(arguments: list[str]) -> int:
    """Closed entry: admit the arguments and the kit, then dispatch one fixed operation."""
    try:
        values = _entry_arguments(arguments)
        _require_isolated_root()
        os.umask(0o077)
        signal.alarm(TIMEOUT_SECONDS)  # A hung root child ends itself whatever happens to its parent.
        load_kit(values["--kit"], values["--kit-digest"])
        from mod_base.errors import run_main
    except BootstrapError:
        print("mod-base: root bootstrap rejected", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mod-base: root bootstrap interrupted", file=sys.stderr)
        return 130
    except (Exception, SystemExit):
        print("mod-base: root bootstrap internal error", file=sys.stderr)
        return 1

    # Every kit import follows byte admission. The operation is fixed protected code selected by
    # the closed argument above; the request it reads never selects code.
    def operate() -> None:
        from mod_base.build_ci.root_request_operations import execute_root_operation
        execute_root_operation(values["--operation"], kit_root=values["--kit"],
                               kit_digest=values["--kit-digest"], nonce=values["--nonce"])

    return run_main(operate)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
