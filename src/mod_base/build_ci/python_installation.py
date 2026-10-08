"""Exclusive root-owned Python installation from the admitted kit's archive lock (MB11).

No downloaded program executes. This admits an archive-derived filesystem copy, not complete
interpreter/system-library provenance or permission to launch a privileged process.
"""

from __future__ import annotations

import gzip
import hashlib
import itertools
import os
import stat
import zlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.installation import KitInstallation, PRIVILEGED_KIT_ROOT, authenticate_privileged_kit
from mod_base.build_ci.python_archive import PythonInstallerMember, _hash_archive, _inspect, _validate_links, inspect_python_installer
from mod_base.build_ci.worker import WorkerError
from mod_base.io.atomic_directory import atomic_directory
from mod_base.model import limits
from mod_base.model.canonical import canonical_json


PYTHON_INSTALL_ROOT = PurePosixPath("/opt/hostedtoolcache/Python")
_PROFILES = {
    "3.11.17": (92586271, "ae713815fe406a06f1691856f697a0b1e58b3e986f44e62f0cd0bca2424f1aac"),
    "3.12.15": (95218570, "fcb672b0437c2d8df5565b7cc250b2b0d7455e2b8d6a4a107cfa6f2efe4f7ed8"),
    "3.13.16": (102730838, "d6f5f504043400592e9dcf368ba262c64d998608646cc22281b5a75beecbc872"),
}


@dataclass(frozen=True)
class PythonInstallation:
    version: str
    archive_digest: str
    manifest_digest: str
    entries: int
    files: int
    total_bytes: int
    device: int
    inode: int


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _admit_lock(installation: KitInstallation, boundary: HostBoundary) -> None:
    authenticate_privileged_kit(installation, boundary=boundary)
    parent = _open_directory(tuple(PRIVILEGED_KIT_ROOT.parts[1:]) + ("requirements",))
    descriptor = None
    try:
        name = "python-ubuntu24-x64.sha256"
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != 0 or before.st_gid != 0
                or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= limits.MAX_CI_PYTHON_LOCK_BYTES):
            raise WorkerError("installed Python archive lock is unsafe")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
        data = bytearray()
        if _stamp(os.fstat(descriptor)) != _stamp(before):
            raise WorkerError("installed Python archive lock changed while opened")
        while len(data) <= before.st_size:
            part = os.read(descriptor, min(65536, before.st_size - len(data) + 1))
            if not part:
                break
            data.extend(part)
        if (len(data) != before.st_size or _stamp(os.fstat(descriptor)) != _stamp(before)
                or _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _stamp(before)):
            raise WorkerError("installed Python archive lock changed while read")
        records = {}
        if not data.endswith(b"\n") or any(byte < 32 and byte != 10 or byte == 127 for byte in data):
            raise WorkerError("installed Python archive lock framing is noncanonical")
        for line in data.decode("ascii").splitlines():
            if line.startswith("#"):
                continue
            parts = line.split("  ")
            if len(parts) != 2 or parts[1] in records:
                raise WorkerError("installed Python archive lock is noncanonical")
            records[parts[1]] = parts[0]
        expected = {f"python-{version}-linux-24.04-x64.tar.gz": digest for version, (_, digest) in _PROFILES.items()}
        if records != expected:
            raise WorkerError("installed Python archive lock differs from the fixed profiles")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _parent(version: str, boundary: HostBoundary, *, create: bool = True) -> int:
    parts = tuple(PYTHON_INSTALL_ROOT.parts[1:]) + (version,)
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        for name in parts:
            made = False
            if create:
                try:
                    os.mkdir(name, 0o755, dir_fd=descriptor)
                    made = True
                except FileExistsError:
                    pass
            child = _open_directory((name,), root=descriptor)
            try:
                if made:
                    os.fchown(child, 0, 0)
                    os.fchmod(child, 0o755)
                info = os.fstat(child)
                if info.st_uid not in (0, boundary.uid) or info.st_mode & 0o022:
                    raise WorkerError("Python installation parent is not protected")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _selected(members: tuple[PythonInstallerMember, ...], version: str) -> tuple[PythonInstallerMember, ...]:
    def keep(path: str) -> bool:
        return path != "setup.sh" and not any(part == "__pycache__" or part.endswith((".pyc", ".pyo", ".pth"))
                                              for part in path.split("/"))
    selected = tuple(member for member in members if keep(member.path))
    records = {member.path: member for member in selected}
    _validate_links(records)
    binary = records.get("bin/python" + ".".join(version.split(".")[:2]))
    if binary is None or binary.kind != "file" or not binary.size or not binary.mode & 0o111:
        raise WorkerError("Python archive has no enrolled executable")
    return selected


def _mode(member: PythonInstallerMember) -> int:
    return 0o755 if member.kind == "directory" or member.mode & 0o111 else 0o644


def _new_file(stage: int, path: str) -> int:
    _canonical_path("/" + path)
    relative = PurePosixPath(path)
    parent = _open_directory(tuple(relative.parent.parts), root=stage)
    try:
        return os.open(relative.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                       | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=parent)
    finally:
        os.close(parent)


def _manifest(members: tuple[PythonInstallerMember, ...], version: str, digest: str) -> str:
    expected = {"format": "mod-base.python-install-v1", "version": version, "archive_digest": digest,
                "members": [{"path": item.path, "kind": item.kind, "mode": 0o777 if item.kind == "link" else _mode(item),
                             "size": item.size, "sha256": item.sha256, "target": item.target} for item in members]}
    return "sha256:" + hashlib.sha256(canonical_json(expected)).hexdigest()


def _verify(root: int, members: tuple[PythonInstallerMember, ...]) -> None:
    expected = {member.path: member for member in members}
    found: set[str] = set()

    def walk(parent: int, prefix: str) -> None:
        initial = _stamp(os.fstat(parent))
        member = expected.get(prefix.rstrip("/"))
        if member is None or member.kind != "directory" or initial[3:5] != (0, 0) or stat.S_IMODE(initial[2]) != 0o755:
            raise WorkerError("Python installation directory metadata differs")
        found.add(member.path)
        with os.scandir(parent) as listing:
            names = [entry.name for entry in itertools.islice(listing, limits.MAX_CI_PYTHON_ARCHIVE_HEADERS + 1)]
        if len(names) > limits.MAX_CI_PYTHON_ARCHIVE_HEADERS:
            raise WorkerError("Python installation contains excessive entries")
        for name in sorted(names):
            path = prefix + name
            item = expected.get(path)
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if item is None or info.st_uid != 0 or info.st_gid != 0:
                raise WorkerError("Python installation contains unexpected or foreign-owned data")
            if item.kind == "directory":
                child = _open_directory((name,), root=parent)
                try:
                    if _stamp(os.fstat(child)) != _stamp(info):
                        raise WorkerError("Python installation directory changed while opened")
                    walk(child, path + "/")
                finally:
                    os.close(child)
            elif item.kind == "link":
                if not stat.S_ISLNK(info.st_mode) or os.readlink(name, dir_fd=parent) != item.target:
                    raise WorkerError("Python installation link differs")
                found.add(path)
            else:
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != item.size
                        or stat.S_IMODE(info.st_mode) != _mode(item)):
                    raise WorkerError("Python installation regular-file metadata differs")
                file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
                try:
                    if _stamp(os.fstat(file)) != _stamp(info):
                        raise WorkerError("Python installation file changed while opened")
                    content, total = hashlib.sha256(), 0
                    while True:
                        chunk = os.read(file, min(65536, item.size - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > item.size:
                            raise WorkerError("Python installation file grew during inspection")
                        content.update(chunk)
                    if total != item.size or content.hexdigest() != item.sha256 or _stamp(os.fstat(file)) != _stamp(info):
                        raise WorkerError("Python installation file bytes differ")
                finally:
                    os.close(file)
                found.add(path)
            if _stamp(os.stat(name, dir_fd=parent, follow_symlinks=False)) != _stamp(info):
                raise WorkerError("Python installation named member changed")
        if _stamp(os.fstat(parent)) != initial:
            raise WorkerError("Python installation directory changed during inspection")

    walk(root, "")
    if found != set(expected):
        raise WorkerError("Python installation inventory is incomplete")


def _copy(source: BinaryIO, stage: int, all_members: tuple[PythonInstallerMember, ...],
          selected: tuple[PythonInstallerMember, ...], size: int) -> None:
    expected = {member.path: member for member in selected}
    for item in sorted(selected, key=lambda member: (member.path.count("/"), member.path)):
        if item.kind == "directory" and item.path:
            parent = _open_directory(tuple(PurePosixPath(item.path).parent.parts), root=stage)
            try:
                os.mkdir(PurePosixPath(item.path).name, 0o700, dir_fd=parent)
            finally:
                os.close(parent)

    @contextmanager
    def sink(path: str, mode: int, length: int):
        item = expected.get(path)
        if item is None:
            yield None
            return
        if item.kind != "file" or (item.mode, item.size) != (mode, length):
            raise WorkerError("Python installer changed before copying")
        file = _new_file(stage, path)
        try:
            def write(data: bytes) -> None:
                view = memoryview(data)
                while view:
                    count = os.write(file, view)
                    if type(count) is not int or not 0 < count <= len(view):
                        raise WorkerError("Python installation write made no progress")
                    view = view[count:]
            yield write
            os.fchmod(file, _mode(item))
            os.fsync(file)
        finally:
            os.close(file)

    source.seek(0)
    with gzip.GzipFile(fileobj=source, mode="rb") as decoded:
        if _inspect(decoded, compressed_size=size, sink=sink) != all_members:
            raise WorkerError("Python installer inventory changed before copying")
    for item in selected:
        if item.kind == "link":
            parent = _open_directory(tuple(PurePosixPath(item.path).parent.parts), root=stage)
            try:
                os.symlink(item.target, PurePosixPath(item.path).name, dir_fd=parent)
            finally:
                os.close(parent)
    for item in sorted(selected, key=lambda member: member.path.count("/"), reverse=True):
        if item.kind == "directory":
            folder = _open_directory(tuple(PurePosixPath(item.path).parts), root=stage)
            try:
                os.fchmod(folder, 0o755)
                os.fsync(folder)
            finally:
                os.close(folder)
    _verify(stage, selected)


def install_privileged_python_archive(archive: Path, *, boundary: HostBoundary,
                                       installation: KitInstallation, version: str) -> PythonInstallation:
    """Install a fixed locked version exclusively at its compiled prefix; never launch it.

    The original protected caller/interpreter must already be independently enrolled. Existing
    destinations are refused. Returned archive-derived bytes do not approve system dependencies.
    """
    authenticate_privileged_host_boundary(boundary)
    if type(version) is not str or version not in _PROFILES:
        raise WorkerError("Python installation version is not enrolled")
    if not isinstance(archive, Path):
        raise WorkerError("Python installer archive path is invalid")
    source_parent = parent = descriptor = None
    try:
        _admit_lock(installation, boundary)
        path = _canonical_path(archive.as_posix())
        if not path.as_posix().startswith(boundary.home + "/"):
            raise WorkerError("Python installer archive is outside the protected home")
        source_parent = _open_directory(tuple(path.parent.parts[1:]))
        initial = os.stat(path.name, dir_fd=source_parent, follow_symlinks=False)
        size, digest = _PROFILES[version]
        wanted = "sha256:" + digest
        if (not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1 or initial.st_size != size
                or initial.st_uid not in (0, boundary.uid) or initial.st_mode & 0o022):
            raise WorkerError("Python installer archive metadata is unsafe")
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=source_parent)
        if _stamp(os.fstat(descriptor)) != _stamp(initial):
            raise WorkerError("Python installer archive changed while opened")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            all_members = inspect_python_installer(source, expected_size=size, expected_digest=wanted)
            selected = _selected(all_members, version)
            parent = _parent(version, boundary)
            destination = Path(str(PYTHON_INSTALL_ROOT / version / "x64"))

            def recheck() -> None:
                if (_stamp(os.fstat(descriptor)) != _stamp(initial)
                        or _stamp(os.stat(path.name, dir_fd=source_parent, follow_symlinks=False)) != _stamp(initial)):
                    raise WorkerError("Python installer source identity changed")
                actual = _parent(version, boundary, create=False)
                try:
                    if (os.fstat(actual).st_dev, os.fstat(actual).st_ino) != (os.fstat(parent).st_dev, os.fstat(parent).st_ino):
                        raise WorkerError("Python installation parent was replaced")
                finally:
                    os.close(actual)
                _admit_lock(installation, boundary)
                authenticate_privileged_host_boundary(boundary)

            def fill(stage_path: Path, stage: int) -> PythonInstallation:
                original = os.fstat(stage)
                os.fchown(stage, 0, 0)
                os.fchmod(stage, 0o700)
                recheck()
                _copy(source, stage, all_members, selected, size)
                _hash_archive(source, size, wanted)
                recheck()
                _verify(stage, selected)
                current = os.fstat(stage)
                if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                    raise WorkerError("Python installation root changed")
                return PythonInstallation(version, wanted, _manifest(selected, version, wanted), len(selected),
                         sum(item.kind == "file" for item in selected), sum(item.size for item in selected),
                         original.st_dev, original.st_ino)

            result = atomic_directory(destination, fill)
            recheck()
            installed = _open_directory(tuple(destination.parts[1:]))
            try:
                info = os.fstat(installed)
                if (info.st_dev, info.st_ino) != (result.device, result.inode):
                    raise WorkerError("published Python installation root changed")
                _verify(installed, selected)
                named = _open_directory(tuple(destination.parts[1:]))
                try:
                    if _stamp(os.fstat(named)) != _stamp(os.fstat(installed)):
                        raise WorkerError("published Python installation root was replaced during inspection")
                finally:
                    os.close(named)
            finally:
                os.close(installed)
            recheck()
            return result
    except (OSError, UnicodeError, ValueError, EOFError, zlib.error) as error:
        raise WorkerError("cannot install protected Python archive") from error
    finally:
        for file in (descriptor, parent, source_parent):
            if file is not None:
                os.close(file)
