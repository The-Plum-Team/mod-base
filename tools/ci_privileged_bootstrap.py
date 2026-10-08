"""Independent pre-import byte guard for the fixed private Build kit.

The protected caller must enroll this program and its interpreter/stdlib before executing it,
and independently authenticate the executing pin and approved digest. Never load this file from
a candidate checkout. Module initialization and byte admission import no mod_base code;
load_fixed_kit imports only after byte admission. The closed process entry seals one fixed request;
it does not install/enroll its own interpreter or authorize its executing pin/controller.
Constants deliberately mirror the kit contract: importing that contract would defeat the guard.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import itertools
import json
import os
import re
import stat
import sys
from pathlib import Path
from types import ModuleType


WORKER_ROOT = "/tmp/mod-base-sandbox-boundary/mod-base-worker"
KIT_ROOT = WORKER_ROOT + "/privileged-kit"
RECORD_ROOT = WORKER_ROOT + "/privileged-kit-record"
RECORD_NAME = "ci-kit-installation.json"
REPOSITORY = "The-Plum-Team/mod-base"
ROOTS = ("src", "site", "requirements")
MAX_RECORD_BYTES = 4096
MAX_FILES = 20000
MAX_ENTRIES = 40000
MAX_BYTES = 512 * 1024 * 1024
MAX_DEPTH = 64
MAX_FILE_ID = (1 << 64) - 1
MAX_UNIX_ID = (1 << 32) - 2
MAX_ARGUMENT_BYTES = 4096
READ_BYTES = 65536
SHA = re.compile(r"[0-9a-f]{40}", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", re.ASCII)
NAME = re.compile(r"[A-Za-z0-9._-]+", re.ASCII)
ENTRY_FLAGS = ("--kit-sha", "--kit-version", "--kit-digest", "--repository", "--controller-sha",
               "--controller-root", "--runner-uid", "--runner-gid", "--home-device",
               "--home-inode", "--home-original-mode", "--nonce")
RUNTIME_OPERATION = "runtime-validation-v1"
RUNTIME_ENTRY_FLAGS = ("--operation", *ENTRY_FLAGS)


class BootstrapError(Exception):
    """Pre-import rejection; importing MbError before admission is forbidden."""


def _require(condition: bool) -> None:
    if not condition:
        raise BootstrapError("private kit bootstrap admission failed")


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _private(fd: int, *, directory: bool) -> tuple[int, ...]:
    info = os.fstat(fd)
    _require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
             and info.st_uid == 0 and info.st_gid == 0
             and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
             and (directory or info.st_nlink == 1))
    for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            os.getxattr(fd, attribute)
        except OSError as error:
            if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                raise
        else:
            raise BootstrapError("private kit bootstrap found an ACL")
    return _stamp(info)


def _open_directory(path: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for name in path.split("/")[1:]:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            _require(stat.S_ISDIR(before.st_mode))
            child = os.open(name, flags, dir_fd=fd)
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


def _file(parent: int, name: str, cap: int, *, collect: bool) -> tuple[bytes | str, int]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    _require(stat.S_ISREG(before.st_mode) and 0 <= before.st_size <= cap)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        initial = _private(fd, directory=False)
        _require(initial == _stamp(before))
        total, chunks, digest = 0, [], hashlib.sha256()
        while True:
            chunk = os.read(fd, min(READ_BYTES, cap - total + 1))
            if not chunk:
                break
            total += len(chunk)
            _require(total <= cap)
            if collect:
                chunks.append(chunk)
            else:
                digest.update(chunk)
        _require(total == before.st_size and _private(fd, directory=False) == initial
                 and _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) == initial)
        return (b"".join(chunks) if collect else digest.hexdigest()), total
    finally:
        os.close(fd)


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _record(raw: bytes, sha: str, version: str, digest: str) -> dict:
    def reject(_value: str) -> None:
        raise BootstrapError("private kit record has a nonfinite number")
    _require(0 < len(raw) <= MAX_RECORD_BYTES)
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_constant=reject)
    _require(type(value) is dict and set(value) == {"kind", "schema_version", "kit",
             "tree_digest", "files", "total_bytes", "device", "inode"})
    _require(value["kind"] == "mod-base.ci.kit-installation"
             and type(value["schema_version"]) is int and value["schema_version"] == 1
             and type(value["kit"]) is dict and value["kit"] == {
                 "repository": REPOSITORY, "sha": sha, "version": version}
             and value["tree_digest"] == digest)
    for name, low, high in (("files", 1, MAX_FILES), ("total_bytes", 0, MAX_BYTES),
                            ("device", 0, MAX_FILE_ID), ("inode", 1, MAX_FILE_ID)):
        _require(type(value[name]) is int and low <= value[name] <= high)
    canonical = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    _require(raw == canonical)
    return value


def _inventory(fd: int) -> tuple[str, int, int]:
    entries, total, lines = 0, 0, []
    _require(_names(fd, len(ROOTS)) == sorted(ROOTS))

    def walk(parent: int, prefix: str, depth: int) -> None:
        nonlocal entries, total
        _require(depth <= MAX_DEPTH)
        initial = _private(parent, directory=True)
        names = _names(parent, MAX_ENTRIES - entries)
        entries += len(names)
        for name in names:
            _require(NAME.fullmatch(name) is not None and name not in {".", "..", "__pycache__"}
                     and not name.endswith((".pyc", ".pyo", ".pth")))
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            path = prefix + name
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=parent)
                try:
                    _require(_stamp(os.fstat(child)) == _stamp(info))
                    # The top-level roots are not included in the native depth cap.
                    walk(child, path + "/", depth if not prefix else depth + 1)
                    _require(_stamp(os.stat(name, dir_fd=parent, follow_symlinks=False))
                             == _stamp(os.fstat(child)))
                finally:
                    os.close(child)
            else:
                _require(len(lines) < MAX_FILES)
                sha, size = _file(parent, name, MAX_BYTES - total, collect=False)
                total += size
                lines.append(("./" + path, sha))
        _require(_private(parent, directory=True) == initial)

    walk(fd, "", 0)
    _require(bool(lines))
    listing = "".join(f"{sha}  {path}\n" for path, sha in sorted(lines)).encode("ascii")
    return "sha256:" + hashlib.sha256(listing).hexdigest(), len(lines), total


def authenticate_fixed_kit(*, expected_sha: str, expected_version: str, expected_digest: str) -> None:
    """Check fixed root record and actual copy before import; approvals are explicit caller inputs."""
    _require(sys.platform == "linux" and os.getuid() == 0 and os.geteuid() == 0
             and os.getgid() == 0 and os.getegid() == 0
             and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode)
    _require(type(expected_sha) is str and SHA.fullmatch(expected_sha) is not None
             and type(expected_version) is str and VERSION.fullmatch(expected_version) is not None
             and len(expected_version) <= 20
             and type(expected_digest) is str and DIGEST.fullmatch(expected_digest) is not None)
    record_fd, kit_fd = None, None
    try:
        record_fd = _open_directory(RECORD_ROOT)
        kit_fd = _open_directory(KIT_ROOT)
        record_stamp = _private(record_fd, directory=True)
        kit_stamp = _private(kit_fd, directory=True)
        _require(_names(record_fd, 1) == [RECORD_NAME])
        raw, _ = _file(record_fd, RECORD_NAME, MAX_RECORD_BYTES, collect=True)
        record = _record(raw, expected_sha, expected_version, expected_digest)
        _require(kit_stamp[:2] == (record["device"], record["inode"]))
        _require(_inventory(kit_fd) == (expected_digest, record["files"], record["total_bytes"]))
        _require(_file(record_fd, RECORD_NAME, MAX_RECORD_BYTES, collect=True)[0] == raw
                 and _names(record_fd, 1) == [RECORD_NAME]
                 and _private(record_fd, directory=True) == record_stamp
                 and _private(kit_fd, directory=True) == kit_stamp)
        for path, expected in ((RECORD_ROOT, record_stamp), (KIT_ROOT, kit_stamp)):
            named = _open_directory(path)
            try:
                _require(_private(named, directory=True) == expected)
            finally:
                os.close(named)
    except (OSError, ValueError, UnicodeError, RecursionError) as error:
        raise BootstrapError("cannot authenticate private kit before import") from error
    finally:
        if kit_fd is not None:
            os.close(kit_fd)
        if record_fd is not None:
            os.close(record_fd)


def load_fixed_kit(*, expected_sha: str, expected_version: str, expected_digest: str) -> ModuleType:
    """Admit bytes, load only the fixed package and recheck; no search-path edits or dispatch."""
    _require(not any(name == "mod_base" or name.startswith("mod_base.") for name in sys.modules))
    expected = dict(expected_sha=expected_sha, expected_version=expected_version, expected_digest=expected_digest)
    original = tuple(sys.path)
    authenticate_fixed_kit(**expected)
    root = Path(KIT_ROOT) / "src/mod_base"
    try:
        spec = importlib.util.spec_from_file_location("mod_base", root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        _require(spec is not None and spec.loader is not None)
        module = importlib.util.module_from_spec(spec)
        sys.modules["mod_base"] = module
        spec.loader.exec_module(module)
        _require(sys.modules.get("mod_base") is module and tuple(sys.path) == original
                 and getattr(module, "__version__", None) == expected_version
                 and getattr(module, "KIT_REPOSITORY", None) == REPOSITORY)
        for name, loaded in tuple(sys.modules.items()):
            if name == "mod_base" or name.startswith("mod_base."):
                origin = getattr(loaded, "__file__", None)
                _require(type(origin) is str and Path(origin).is_relative_to(root))
        authenticate_fixed_kit(**expected)
        return module
    except BaseException as error:
        # Never leave a partly initialized/rejected package available to a retry or caller.
        for name in tuple(sys.modules):
            if name == "mod_base" or name.startswith("mod_base."):
                del sys.modules[name]
        if isinstance(error, (BootstrapError, KeyboardInterrupt, SystemExit)):
            raise
        raise BootstrapError("cannot load admitted private kit") from error


def _entry_arguments(arguments: list[str]) -> dict[str, str]:
    # Preserve legacy Build pairs; only the explicit versioned runtime route adds one pair.
    _require(type(arguments) is list and len(arguments) in (2 * len(ENTRY_FLAGS), 2 * len(RUNTIME_ENTRY_FLAGS)))
    flags = ENTRY_FLAGS if len(arguments) == 2 * len(ENTRY_FLAGS) else RUNTIME_ENTRY_FLAGS
    for value in arguments:
        _require(type(value) is str and len(value) <= MAX_ARGUMENT_BYTES)
        try:
            _require(len(value.encode("utf-8")) <= MAX_ARGUMENT_BYTES)
        except UnicodeError as error:
            raise BootstrapError("invalid private bootstrap argument") from error
        _require(not any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value))
    _require(tuple(arguments[::2]) == flags)
    values = dict(zip(flags, arguments[1::2]))
    if flags == RUNTIME_ENTRY_FLAGS:
        _require(values["--operation"] == RUNTIME_OPERATION)
    for name, minimum, maximum in (("--runner-uid", 1, MAX_UNIX_ID), ("--runner-gid", 1, MAX_UNIX_ID),
            ("--home-device", 0, MAX_FILE_ID), ("--home-inode", 1, MAX_FILE_ID),
            ("--home-original-mode", 0, 0o7777)):
        value = values[name]
        _require(re.fullmatch(r"0|[1-9][0-9]{0,19}", value, re.ASCII) is not None)
        _require(minimum <= int(value) <= maximum)
    _require(re.fullmatch(r"[0-9a-f]{64}", values["--nonce"], re.ASCII) is not None)
    return values


def main(arguments: list[str]) -> int:
    """Closed entry after external program/interpreter/pin enrollment; never read ambient env."""
    try:
        values = _entry_arguments(arguments)
        load_fixed_kit(expected_sha=values["--kit-sha"], expected_version=values["--kit-version"],
                       expected_digest=values["--kit-digest"])
        from mod_base.errors import run_main
    except BootstrapError:
        print("mod-base: private bootstrap rejected", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mod-base: private bootstrap interrupted", file=sys.stderr)
        return 130
    except (Exception, SystemExit):
        print("mod-base: private bootstrap internal error", file=sys.stderr)
        return 1

    # Every kit import is after byte admission. Both targets are fixed protected code;
    # the request never selects one and unsupported CLI operations reject before loading.
    def seal() -> None:
        from mod_base.build_ci.host import HostBoundary, authenticate_privileged_host_boundary
        from mod_base.build_ci.worker import authenticate_worker_account, terminate_worker
        from mod_base.build_ci.root_request import (build_root_freeze_invocation,
                                                    freeze_root_requested_build_validation)
        if values.get("--operation") == RUNTIME_OPERATION:
            from mod_base.build_ci.runtime_root_request import freeze_root_requested_runtime_validation
        boundary = HostBoundary("/home/runner", int(values["--runner-uid"]), int(values["--runner-gid"]),
            int(values["--home-device"]), int(values["--home-inode"]), int(values["--home-original-mode"]))
        authenticate_privileged_host_boundary(boundary)
        validator = authenticate_worker_account("validator")
        try:
            invocation = build_root_freeze_invocation(boundary=boundary, controller_root=values["--controller-root"],
                repository=values["--repository"], controller_sha=values["--controller-sha"], kit_sha=values["--kit-sha"])
        except BaseException:
            terminate_worker(validator)
            raise
        if values.get("--operation") == RUNTIME_OPERATION:
            freeze_root_requested_runtime_validation(invocation, boundary=boundary, validator=validator, nonce=values["--nonce"])
        else:
            freeze_root_requested_build_validation(invocation, boundary=boundary, validator=validator, nonce=values["--nonce"])

    return run_main(seal)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
