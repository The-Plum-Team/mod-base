"""Root-private publisher archive handoff and exclusive Python setup (MB11).

The original caller/program/runtime must already be independently enrolled. This never launches
the copied interpreter or an archive program and grants no system-library execution authority.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath

from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.installation import KitInstallation
from mod_base.build_ci.python_archive import _hash_archive, inspect_python_installer
from mod_base.build_ci.python_installation import (
    PYTHON_INSTALL_ROOT, PythonInstallation, _PROFILES, _admit_lock, _manifest,
    _parent as _installation_parent, _selected, _stamp, _verify as _verify_installation,
    install_privileged_python_archive,
)
from mod_base.build_ci.python_transport import _authenticate, download_python_installer
from mod_base.build_ci.worker import WorkerError
from mod_base.io.atomic_directory import atomic_directory
from mod_base.github.api import GitHubApi
from mod_base.model import limits


PYTHON_ARCHIVE_CACHE = PurePosixPath("/home/runner/.mod-base-python")
_ARCHIVE = "installer.tar.gz"


def _private_directory(info: os.stat_result) -> None:
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise WorkerError("Python cache directory is not root-private")


def _parent(*, create: bool) -> int:
    home = _open_directory(("home", "runner"))
    child = None
    try:
        try:
            before = os.stat(PYTHON_ARCHIVE_CACHE.name, dir_fd=home, follow_symlinks=False)
        except FileNotFoundError:
            if not create:
                raise
            os.mkdir(PYTHON_ARCHIVE_CACHE.name, 0o700, dir_fd=home)
            # Reject unexpected inherited metadata, rather than adopting an existing tree.
            before = os.stat(PYTHON_ARCHIVE_CACHE.name, dir_fd=home, follow_symlinks=False)
        _private_directory(before)
        child = os.open(PYTHON_ARCHIVE_CACHE.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0), dir_fd=home)
        if _stamp(os.fstat(child)) != _stamp(before):
            raise WorkerError("Python cache parent changed while opened")
        result, child = child, None
        return result
    finally:
        if child is not None:
            os.close(child)
        os.close(home)


def _write(stage: int, data: bytes) -> None:
    descriptor = os.open(_ARCHIVE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=stage)
    try:
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:offset + 65536])
            if type(written) is not int or not 1 <= written <= min(65536, len(view) - offset):
                raise WorkerError("Python archive cache write made invalid progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(stage)


def _verify(stage: int, size: int, digest: str) -> None:
    original = os.fstat(stage)
    _private_directory(original)
    with os.scandir(stage) as entries:
        first = next(entries, None)
        if first is None or first.name != _ARCHIVE or next(entries, None) is not None:
            raise WorkerError("Python archive cache inventory differs")
    before = os.stat(_ARCHIVE, dir_fd=stage, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size
            or before.st_uid != 0 or before.st_gid != 0 or stat.S_IMODE(before.st_mode) != 0o600):
        raise WorkerError("Python archive cache file metadata is unsafe")
    descriptor = os.open(_ARCHIVE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=stage)
    try:
        if _stamp(os.fstat(descriptor)) != _stamp(before):
            raise WorkerError("Python archive cache file changed while opened")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            _hash_archive(stream, size, digest)
        if (_stamp(os.fstat(descriptor)) != _stamp(before)
                or _stamp(os.stat(_ARCHIVE, dir_fd=stage, follow_symlinks=False)) != _stamp(before)
                or _stamp(os.fstat(stage)) != _stamp(original)):
            raise WorkerError("Python archive cache changed while verified")
    finally:
        os.close(descriptor)


def cache_privileged_python_installer(api: GitHubApi, *, boundary: HostBoundary,
                                      installation: KitInstallation, version: str) -> Path:
    """Publish one locked archive exclusively under the fixed root-private home cache.

    Existing version directories are refused. Rechecks bind the actual parent, root inode,
    private kit lock and publisher metadata; a late rejection grants no use of a published file.
    """

    authenticate_privileged_host_boundary(boundary)
    if type(version) is not str or version not in _PROFILES:
        raise WorkerError("Python cache version is not enrolled")
    parent = published = None
    try:
        _admit_lock(installation, boundary)
        data = download_python_installer(api, version=version)
        authenticate_privileged_host_boundary(boundary)
        _admit_lock(installation, boundary)
        parent = _parent(create=True)
        parent_info = os.fstat(parent)
        destination = Path(str(PYTHON_ARCHIVE_CACHE / version))
        size, digest = _PROFILES[version]
        wanted = "sha256:" + digest

        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            _admit_lock(installation, boundary)
            current = _parent(create=False)
            try:
                info = os.fstat(current)
                held = os.fstat(parent)
                _private_directory(info)
                _private_directory(held)
                if (info.st_dev, info.st_ino) != (parent_info.st_dev, parent_info.st_ino) or (
                        held.st_dev, held.st_ino) != (parent_info.st_dev, parent_info.st_ino):
                    raise WorkerError("Python cache parent was replaced")
            finally:
                os.close(current)

        def fill(stage_path: Path, stage: int) -> tuple[int, int]:
            original = os.fstat(stage)
            os.fchown(stage, 0, 0)
            os.fchmod(stage, 0o700)
            recheck()
            _write(stage, data)
            _verify(stage, size, wanted)
            _authenticate(api, version)
            recheck()
            current = os.fstat(stage)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise WorkerError("Python archive cache stage was replaced")
            return original.st_dev, original.st_ino

        identity = atomic_directory(destination, fill)
        recheck()
        published = _open_directory(tuple(destination.parts[1:]))
        info = os.fstat(published)
        if (info.st_dev, info.st_ino) != identity:
            raise WorkerError("Python archive cache root was replaced")
        _verify(published, size, wanted)
        _authenticate(api, version)
        recheck()
        named = _open_directory(tuple(destination.parts[1:]))
        try:
            if _stamp(os.fstat(named)) != _stamp(os.fstat(published)):
                raise WorkerError("Python archive cache root changed after publication")
            _verify(published, size, wanted)
        finally:
            os.close(named)
        recheck()
        named = _open_directory(tuple(destination.parts[1:]))
        try:
            if _stamp(os.fstat(named)) != _stamp(os.fstat(published)):
                raise WorkerError("Python archive cache name changed during final verification")
        finally:
            os.close(named)
        return destination / _ARCHIVE
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkerError("cannot publish protected Python archive cache") from error
    finally:
        for descriptor in (published, parent):
            if descriptor is not None:
                os.close(descriptor)


def authenticate_privileged_python_installation(proof: PythonInstallation, *, boundary: HostBoundary,
                                                installation: KitInstallation) -> str:
    """Re-derive expected installed bytes from the fixed approved private archive, read-only.

    Constructors and observed host hashes never approve an installation. The return value is
    the fixed executable path, not approval of its interpreter/import/system runtime closure.
    """

    authenticate_privileged_host_boundary(boundary)
    if (type(proof) is not PythonInstallation or type(proof.version) is not str or proof.version not in _PROFILES
            or any(type(value) is not int or not 0 <= value <= limits.MAX_CI_FILE_ID for value in
                   (proof.entries, proof.files, proof.total_bytes, proof.device, proof.inode))):
        raise WorkerError("Python installation receipt has invalid identity fields")
    size, digest = _PROFILES[proof.version]
    wanted = "sha256:" + digest
    if type(proof.archive_digest) is not str or proof.archive_digest != wanted or type(proof.manifest_digest) is not str:
        raise WorkerError("Python installation receipt differs from the approved archive profile")
    cache_parent = cache = descriptor = prefix = installed = None
    try:
        _admit_lock(installation, boundary)
        cache_parent = _parent(create=False)
        cache_parent_info = os.fstat(cache_parent)
        cache = _open_directory((proof.version,), root=cache_parent)
        _verify(cache, size, wanted)
        cache_info = os.fstat(cache)
        before = os.stat(_ARCHIVE, dir_fd=cache, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != size
                or before.st_uid != 0 or before.st_gid != 0 or stat.S_IMODE(before.st_mode) != 0o600):
            raise WorkerError("Python installation source archive metadata is unsafe")
        descriptor = os.open(_ARCHIVE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                             | getattr(os, "O_CLOEXEC", 0), dir_fd=cache)
        if _stamp(os.fstat(descriptor)) != _stamp(before):
            raise WorkerError("Python installation source archive changed while opened")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            members = inspect_python_installer(source, expected_size=size, expected_digest=wanted)
            selected = _selected(members, proof.version)
            prefix = _installation_parent(proof.version, boundary, create=False)
            prefix_info = os.fstat(prefix)
            installed = _open_directory(("x64",), root=prefix)
            initial = os.fstat(installed)
            expected = PythonInstallation(proof.version, wanted, _manifest(selected, proof.version, wanted),
                len(selected), sum(item.kind == "file" for item in selected), sum(item.size for item in selected),
                initial.st_dev, initial.st_ino)
            if proof != expected:
                raise WorkerError("Python installation receipt does not match independently derived source bytes")

            def recheck() -> None:
                authenticate_privileged_host_boundary(boundary)
                _admit_lock(installation, boundary)
                current = _parent(create=False)
                actual = None
                try:
                    actual = _installation_parent(proof.version, boundary, create=False)
                    if ((os.fstat(current).st_dev, os.fstat(current).st_ino) !=
                            (cache_parent_info.st_dev, cache_parent_info.st_ino)
                            or _stamp(os.fstat(cache_parent)) != _stamp(cache_parent_info)
                            or (os.fstat(actual).st_dev, os.fstat(actual).st_ino) !=
                            (prefix_info.st_dev, prefix_info.st_ino)
                            or _stamp(os.fstat(prefix)) != _stamp(prefix_info)
                            or _stamp(os.stat(proof.version, dir_fd=current, follow_symlinks=False)) != _stamp(cache_info)
                            or _stamp(os.fstat(cache)) != _stamp(cache_info)
                            or _stamp(os.stat("x64", dir_fd=actual, follow_symlinks=False)) != _stamp(initial)
                            or _stamp(os.fstat(installed)) != _stamp(initial)
                            or _stamp(os.stat(_ARCHIVE, dir_fd=cache, follow_symlinks=False)) != _stamp(before)
                            or _stamp(os.fstat(descriptor)) != _stamp(before)):
                        raise WorkerError("Python installation/cache/source binding changed")
                finally:
                    if actual is not None:
                        os.close(actual)
                    os.close(current)

            recheck()
            _verify_installation(installed, selected)
            _hash_archive(source, size, wanted)
            _verify(cache, size, wanted)
            recheck()
            executable = f"python{proof.version.rsplit('.', 1)[0]}"
            return str(PYTHON_INSTALL_ROOT / proof.version / "x64" / "bin" / executable)
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkerError("cannot authenticate protected Python installation") from error
    finally:
        for file in (installed, prefix, descriptor, cache, cache_parent):
            if file is not None:
                os.close(file)


def install_privileged_python_from_publisher(api: GitHubApi, *, boundary: HostBoundary,
                                            installation: KitInstallation, version: str) -> PythonInstallation:
    """Admit/cache publisher bytes, then perform the fixed exclusive source-only installation."""

    archive = cache_privileged_python_installer(api, boundary=boundary, installation=installation, version=version)
    result = install_privileged_python_archive(archive, boundary=boundary, installation=installation, version=version)
    _authenticate(api, version)
    authenticate_privileged_python_installation(result, boundary=boundary, installation=installation)
    return result
