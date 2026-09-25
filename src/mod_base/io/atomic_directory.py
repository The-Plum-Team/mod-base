"""Descriptor-bound exclusive directory publication (MB1).

Port of Block Pops ``scripts/lib/atomic_directory.py``: a writer fills a private ``0700`` stage
next to ``output`` through a directory descriptor; the stage is published with
``renameat2(RENAME_NOREPLACE)`` (Linux) or ``renameatx_np(RENAME_EXCL)`` (macOS) so an existing
output is never replaced, and every identity (parent, stage) is re-bound before and after the
writer. A failed writer leaves no stage behind. Unsupported hosts fail before creating anything.

Differences from the lineage: the writer receives ``(stage_path, stage_fd)``; every failure of
the kit's own filesystem operations is an :class:`AtomicDirectoryError` (exit 2), while an
exception raised by the writer itself propagates unchanged.
"""

from __future__ import annotations

import ctypes
import os
import stat
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mod_base.errors import MbError

OWNER = "MB1"
T = TypeVar("T")

#: ``sys.platform`` -> (libc symbol, "fail if the destination exists" flag).
EXCLUSIVE_RENAME = {"darwin": ("renameatx_np", 0x4), "linux": ("renameat2", 0x1)}


class AtomicDirectoryError(MbError):
    """An owned output cannot be safely written or published (exit 2)."""

    default_reason = "unsafe-output"


def _real_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise AtomicDirectoryError(f"cannot inspect output parent: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AtomicDirectoryError("output parent must be a real directory")


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value or ":" in value:
        raise AtomicDirectoryError("output path must be a canonical relative path")
    path = Path(value)
    if path.is_absolute() or value != path.as_posix() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise AtomicDirectoryError("output path must be a canonical relative path")
    return value


def _exclusive_directory_rename(platform: str | None = None,
                                loader: Callable[[], Any] | None = None) -> Callable[[int, str, str], None]:
    """Return ``rename(parent_fd, source, destination)`` that never replaces ``destination``.

    ``platform`` and ``loader`` (``ctypes.CDLL(None)``) are seams for testing both host paths.
    """

    required = (os.open, os.mkdir, os.stat, os.unlink, os.rmdir)
    if (not all(call in os.supports_dir_fd for call in required) or os.listdir not in os.supports_fd
            or not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW")):
        raise AtomicDirectoryError("output publication requires descriptor-relative filesystem primitives")
    name, flag = EXCLUSIVE_RENAME.get(sys.platform if platform is None else platform, (None, None))
    if name is None or flag is None:
        raise AtomicDirectoryError("output publication requires atomic exclusive directory rename")
    try:
        library = loader() if loader is not None else ctypes.CDLL(None, use_errno=True)
        function = getattr(library, name, None)
    except OSError:
        function = None
    if function is None:
        raise AtomicDirectoryError("output publication requires atomic exclusive directory rename")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int

    def rename(parent: int, source: str, destination: str) -> None:
        if function(parent, os.fsencode(source), parent, os.fsencode(destination), flag):
            error = ctypes.get_errno()
            raise AtomicDirectoryError(f"exclusive output publication failed: {os.strerror(error)}")

    return rename


def _directory_fd(path: Path, *, root_fd: int | None = None, create: bool = False) -> int:
    """Open ``path`` (absolute, or relative to ``root_fd``) refusing a symlink at every component.

    An ancestor only has to be passed through, and a CI sandbox may make its boundary search-only
    (``0711``) on purpose, so opening it for reading is refused. Linux can hold a directory by path
    alone (``O_PATH``), still refusing a symlink at every step; every component but the last is
    opened that way, and the descriptor handed back stays a readable one.
    """

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    through = (os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
               if hasattr(os, "O_PATH") else flags)
    parts = path.parts if root_fd is not None else path.parts[1:]
    descriptor = os.dup(root_fd) if root_fd is not None else os.open(path.anchor, through if parts else flags)
    try:
        for index, part in enumerate(parts):
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, flags if index == len(parts) - 1 else through, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _directory_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _bound_output_directory(output: Path, parent: int, name: str, stage: int) -> None:
    try:
        current = _directory_fd(output.parent)
        try:
            same_parent = _directory_identity(os.fstat(current)) == _directory_identity(os.fstat(parent))
        finally:
            os.close(current)
        entry = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not same_parent or not stat.S_ISDIR(entry.st_mode)
                or _directory_identity(entry) != _directory_identity(os.fstat(stage))):
            raise AtomicDirectoryError("output parent or stage identity changed")
    except OSError as exc:
        raise AtomicDirectoryError("output parent or stage binding changed") from exc


def _clear_owned_directory(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                try:
                    if _directory_identity(before) != _directory_identity(os.fstat(child)):
                        raise AtomicDirectoryError("output cleanup directory changed")
                    _clear_owned_directory(child)
                finally:
                    os.close(child)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _directory_identity(current) == _directory_identity(before):
                    os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def _output_path(output: Path) -> Path:
    candidate = Path(output).absolute()
    if candidate.name in {"", ".", ".."} or "\x00" in str(candidate):
        raise AtomicDirectoryError("output must name a new directory")
    if candidate.parent.exists() or candidate.parent.is_symlink():
        _real_directory(candidate.parent)
    # Resolve stable host aliases such as macOS /var -> /private/var once, before any descriptor walk.
    return candidate.parent.resolve() / candidate.name


def atomic_directory(output: Path, writer: Callable[[Path, int], T]) -> T:
    """Create ``output`` atomically: call ``writer(stage_path, stage_fd)`` and publish the stage.

    Raises :class:`AtomicDirectoryError` if ``output`` exists, its parent is not a real directory,
    an identity changed, or exclusive rename is unavailable. Returns the writer's result.
    """

    rename = _exclusive_directory_rename()  # Fail before creating directories on unsupported hosts.
    try:
        output = _output_path(output)
        parent = _directory_fd(output.parent, create=True)
    except OSError as exc:
        raise AtomicDirectoryError(f"cannot open the output parent: {exc.strerror or exc}") from exc
    stage: int | None = None
    name = f".{output.name}.building-{uuid.uuid4().hex}"
    published = False
    try:
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AtomicDirectoryError(f"cannot inspect the output: {exc.strerror or exc}") from exc
        else:
            raise AtomicDirectoryError(f"refusing to replace existing output {output.name!r}")
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            stage = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=parent)
        except OSError as exc:
            raise AtomicDirectoryError(f"cannot create the output stage: {exc.strerror or exc}") from exc
        _bound_output_directory(output, parent, name, stage)
        result = writer(output.parent / name, stage)
        _bound_output_directory(output, parent, name, stage)
        rename(parent, name, output.name)
        name = output.name
        _bound_output_directory(output, parent, name, stage)
        published = True
        return result
    finally:
        try:
            if stage is not None and not published:
                try:
                    _clear_owned_directory(stage)
                    if _directory_identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) == _directory_identity(
                            os.fstat(stage)):
                        os.rmdir(name, dir_fd=parent)
                except (OSError, AtomicDirectoryError):
                    pass  # Best effort: an unpublished stage is never an output; keep the original error.
        finally:
            if stage is not None:
                os.close(stage)
            os.close(parent)


def _open_new_file(stage: int, relative: str) -> int:
    """Create ``relative`` under ``stage`` (parents ``0700``) and return its write descriptor."""

    path = Path(_relative_path(relative))
    try:
        parent = _directory_fd(path.parent, root_fd=stage, create=True)
        try:
            return os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                           0o600, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError as exc:
        raise AtomicDirectoryError(f"cannot create {relative!r} in the output stage: {exc.strerror or exc}") from exc


def _write_all(descriptor: int, data: bytes | memoryview) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _seal_new_file(descriptor: int) -> None:
    """Publishable mode ``0644`` and durable bytes for a file created by :func:`_open_new_file`."""

    os.fchmod(descriptor, 0o644)
    os.fsync(descriptor)


def write_new(stage: int, relative: str, data: bytes) -> None:
    """Create ``relative`` (canonical POSIX path, parents created ``0700``) under the stage
    descriptor with ``O_CREAT|O_EXCL|O_NOFOLLOW``, write ``data``, fsync, and chmod ``0644``."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise AtomicDirectoryError("output bytes must be bytes")
    descriptor = _open_new_file(stage, relative)
    try:
        _write_all(descriptor, data)
        _seal_new_file(descriptor)
    except OSError as exc:
        raise AtomicDirectoryError(f"cannot write {relative!r} in the output stage: {exc.strerror or exc}") from exc
    finally:
        os.close(descriptor)
