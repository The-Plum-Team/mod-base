"""Bounded regular-file walks and descriptor-relative child reads (MB1).

Union of Quick Skin ``evidence._bounded_entries``/``reject_symlinks`` and Block Pops
``_child_file``: every walk refuses symlinks, special files, hard-linked files and paths that are
not canonical bundle paths (:func:`mod_base.model.grammar.is_bundle_path`), and enforces count
and byte bounds before reading any content.

Walks and reads go through directory descriptors opened with ``O_NOFOLLOW``, so a component that
is (or becomes) a symlink is refused instead of followed; every file read is stat-stable (same
device, inode and size before, during and after the read). The root's own ancestors are the
caller's; only the root itself and everything below it are checked.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import stat
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.model import grammar

OWNER = "MB1"

#: ``reject_symlinks`` bounds (it takes no caller bounds): entries visited and directory depth.
MAX_WALK_ENTRIES = 1_000_000
MAX_WALK_DEPTH = 64
_READ_CHUNK = 1 << 16
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class TreeError(MbError):
    """A directory tree is not a bounded tree of regular files (exit 2)."""

    default_reason = "unsafe-tree"


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _open_root(root: Path, label: str) -> int:
    """Open ``root`` (a real directory, not a symlink) and bind the descriptor to its path."""

    try:
        before = Path(root).lstat()
    except OSError as exc:
        raise TreeError(f"cannot inspect {label}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise TreeError(f"{label} must be a real directory")
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise TreeError(f"cannot open {label}: {exc.strerror or exc}") from exc
    try:
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise TreeError(f"{label} changed while it was opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_child_directory(parent: int, name: str, expected: os.stat_result) -> int:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode) or _identity(opened) != _identity(expected):
            raise TreeError("a directory changed while the tree was walked")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _walk(directory: int, parent: str, *, depth: int, budget: list[int],
          visit: Callable[[str, os.stat_result], bool]) -> None:
    """Depth-first walk below ``directory``: ``visit(relative, lstat)`` returns True to descend.

    ``budget[0]`` is the number of entries that may still be listed; the directory listing itself
    is bounded by it, so a hostile directory with millions of names is refused while listing.
    """

    with os.scandir(directory) as entries:
        names = [entry.name for entry in itertools.islice(entries, budget[0] + 1)]
    if len(names) > budget[0]:
        raise TreeError("directory tree exceeds its entry bound")
    budget[0] -= len(names)
    for name in sorted(names):
        relative = f"{parent}/{name}" if parent else name
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise TreeError(f"tree contains a symlink: {relative!r}"[:300])
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise TreeError(f"tree contains a special file: {relative!r}"[:300])
        if visit(relative, info) and stat.S_ISDIR(info.st_mode):
            if depth >= MAX_WALK_DEPTH:
                raise TreeError("directory tree exceeds its depth bound")
            child = _open_child_directory(directory, name, info)
            try:
                _walk(child, relative, depth=depth + 1, budget=budget, visit=visit)
            finally:
                os.close(child)


def regular_files(root: Path, *, max_files: int, max_total_bytes: int, max_file_bytes: int,
                  suffixes: Collection[str] | None = None) -> dict[str, int]:
    """Return ``{relative POSIX path: size}`` for every file under ``root`` (sorted by path).

    ``root`` must be a real directory; every entry must be a directory or a single-link regular
    file; empty files are refused; with ``suffixes`` every file must end with one of them.
    """

    for value in (max_files, max_total_bytes, max_file_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TreeError("tree bounds must be non-negative integers")
    allowed = tuple(suffixes) if suffixes is not None else None
    files: dict[str, int] = {}
    totals = {"bytes": 0, "directories": 0}

    def visit(relative: str, info: os.stat_result) -> bool:
        if not grammar.is_bundle_path(relative):
            raise TreeError(f"tree path is not a canonical bundle path: {relative!r}"[:300])
        if stat.S_ISDIR(info.st_mode):
            totals["directories"] += 1
            if totals["directories"] > max(max_files, 1):
                raise TreeError("tree exceeds its directory bound")
            return True
        if info.st_nlink != 1:
            raise TreeError(f"tree contains a hard-linked file: {relative!r}"[:300])
        if info.st_size <= 0 or info.st_size > max_file_bytes:
            raise TreeError(f"tree file size is outside 1..{max_file_bytes} bytes: {relative!r}"[:300])
        if allowed is not None and not relative.endswith(allowed):
            raise TreeError(f"tree file has an unapproved suffix: {relative!r}"[:300])
        files[relative] = info.st_size
        totals["bytes"] += info.st_size
        if len(files) > max_files or totals["bytes"] > max_total_bytes:
            raise TreeError("tree exceeds its file-count or byte bound")
        return False

    descriptor = _open_root(root, "tree root")
    try:
        _walk(descriptor, "", depth=0, budget=[2 * max(max_files, 1) + 1], visit=visit)
    except OSError as exc:
        raise TreeError(f"cannot walk the tree: {exc.strerror or exc}") from exc
    finally:
        os.close(descriptor)
    return dict(sorted(files.items()))


def reject_symlinks(root: Path) -> None:
    """Raise :class:`TreeError` if any entry at or under ``root`` is a symlink or special file."""

    try:
        info = Path(root).lstat()
    except OSError as exc:
        raise TreeError(f"cannot inspect {Path(root).name!r}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise TreeError("path is a symlink")
    if stat.S_ISREG(info.st_mode):
        return
    if not stat.S_ISDIR(info.st_mode):
        raise TreeError("path is a special file")
    descriptor = _open_root(root, "tree root")
    try:
        _walk(descriptor, "", depth=0, budget=[MAX_WALK_ENTRIES], visit=lambda _relative, _info: True)
    except OSError as exc:
        raise TreeError(f"cannot walk the tree: {exc.strerror or exc}") from exc
    finally:
        os.close(descriptor)


def _parent_descriptor(root_fd: int, parts: list[str]) -> int:
    """Open every directory component below ``root_fd`` with ``O_NOFOLLOW`` and return the last."""

    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise TreeError("path has an unsafe parent component")
            child = _open_child_directory(descriptor, part, before)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stream_regular(directory: int | None, name: str | Path, *, max_bytes: int, allow_empty: bool,
                    consume: Callable[[bytes], None]) -> int:
    """Stream one stat-stable single-link regular file to ``consume`` and return its size."""

    before = os.stat(name, dir_fd=directory, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise TreeError("file must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise TreeError("file must be a regular file")
    if before.st_nlink != 1:
        raise TreeError("file must not be hard-linked")
    if before.st_size > max_bytes or (before.st_size == 0 and not allow_empty):
        raise TreeError(f"file size must be between {0 if allow_empty else 1} and {max_bytes} bytes")
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (
                before.st_dev, before.st_ino, before.st_size):
            raise TreeError("file changed while it was opened")
        read = 0
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            read += len(chunk)
            remaining -= len(chunk)
            consume(chunk)
        after = os.fstat(descriptor)
        if read != opened.st_size or (after.st_dev, after.st_ino, after.st_size) != (
                opened.st_dev, opened.st_ino, opened.st_size):
            raise TreeError("file changed while it was read")
        return read
    finally:
        os.close(descriptor)


def _require_relative(relative: Any) -> list[str]:
    if not grammar.is_bundle_path(relative):
        raise TreeError(f"path is not a canonical bundle path: {relative!r}"[:300])
    return relative.split("/")


def read_child_file(root: Path, relative: str, *, max_bytes: int) -> bytes:
    """Read ``root/relative`` walking every component through ``O_NOFOLLOW`` directory
    descriptors (no symlink anywhere), stat-stable, 1..``max_bytes`` bytes."""

    parts = _require_relative(relative)
    chunks: list[bytes] = []
    root_fd = _open_root(root, "tree root")
    try:
        parent = _parent_descriptor(root_fd, parts[:-1])
        try:
            _stream_regular(parent, parts[-1], max_bytes=max_bytes, allow_empty=False, consume=chunks.append)
        finally:
            os.close(parent)
    except OSError as exc:
        raise TreeError(f"cannot read {relative!r}: {exc.strerror or exc}"[:300]) from exc
    finally:
        os.close(root_fd)
    return b"".join(chunks)


def sha256_file(path: Path, *, max_bytes: int) -> str:
    """SHA-256 hex of one stable regular file of at most ``max_bytes`` (streamed, ``O_NOFOLLOW``)."""

    digest = hashlib.sha256()
    try:
        _stream_regular(None, Path(path), max_bytes=max_bytes, allow_empty=True, consume=digest.update)
    except OSError as exc:
        raise TreeError(f"cannot hash {Path(path).name!r}: {exc.strerror or exc}"[:300]) from exc
    return digest.hexdigest()


def file_records(root: Path, *, exclude: Collection[str] = (), max_files: int, max_total_bytes: int,
                 max_file_bytes: int) -> list[dict[str, Any]]:
    """Return the exact inventory ``[{path, sha256, size}]`` of ``root`` sorted by path, leaving out
    the relative paths in ``exclude`` (for example ``manifest.json``)."""

    excluded = set()
    for relative in exclude:
        _require_relative(relative)
        excluded.add(relative)
    sizes = regular_files(root, max_files=max_files, max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes)
    records: list[dict[str, Any]] = []
    root_fd = _open_root(root, "tree root")
    try:
        for relative, size in sizes.items():
            if relative in excluded:
                continue
            parts = relative.split("/")
            digest = hashlib.sha256()
            parent = _parent_descriptor(root_fd, parts[:-1])
            try:
                read = _stream_regular(parent, parts[-1], max_bytes=max_file_bytes, allow_empty=False,
                                       consume=digest.update)
            finally:
                os.close(parent)
            if read != size:
                raise TreeError(f"tree file changed while it was inventoried: {relative!r}"[:300])
            records.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
    except OSError as exc:
        raise TreeError(f"cannot inventory the tree: {exc.strerror or exc}") from exc
    finally:
        os.close(root_fd)
    return records
