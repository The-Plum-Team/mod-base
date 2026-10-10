"""Bounded regular-file walks and descriptor-relative child reads (MB1).

Union of Quick Skin ``evidence._bounded_entries``/``reject_symlinks`` and Block Pops
``_child_file``: every walk refuses symlinks, special files, hard-linked files and paths outside
its :class:`PathRule` (canonical bundle paths, :func:`mod_base.model.grammar.is_bundle_path`,
unless the caller names another), and enforces count and byte bounds before reading any content.

Walks and reads go through directory descriptors opened with ``O_NOFOLLOW``, so a component that
is (or becomes) a symlink is refused instead of followed; every file read is stat-stable (same
device, inode and size before, during and after the read). The root's own ancestors are the
caller's; only the root itself and everything below it are checked.
"""

from __future__ import annotations

import hashlib
import errno
import itertools
import os
import stat
import sys
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.model import grammar, limits

OWNER = "MB1"

#: Entries ``reject_symlinks`` visits (it takes no caller bounds) and the directory depth at which
#: every walk stops.
MAX_WALK_ENTRIES = 1_000_000
MAX_WALK_DEPTH = 64
_READ_CHUNK = 1 << 16
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


class TreeError(MbError):
    """A directory tree is not a bounded tree of regular files (exit 2)."""

    default_reason = "unsafe-tree"


@dataclass(frozen=True)
class PathRule:
    """The entry paths one tree may hold, relative to its root (never a link or a special file)."""

    name: str
    is_safe: Callable[[object], bool]
    #: Whether two entries may differ only in case.
    aliases: bool = False


#: Pages bundles and every other kit-named tree: the default of the nonempty-file functions, whose
#: walks have no case-alias check of their own.
BUNDLE_PATHS = PathRule("bundle", grammar.is_bundle_path, aliases=True)
#: Repository-shaped data without ``.git``: the default of the ``regular_data`` functions.
REPO_PATHS = PathRule("repository", grammar.is_repo_path)
#: Sealed CI exports, which keep the mod's own file names (spaces, ``+``) and may be unpacked where
#: case is folded.
EXPORT_PATHS = PathRule("export", grammar.is_export_path)
#: Gradle seeds: structural safety only. A cache is opaque data that names its entries freely, so
#: neither a name grammar nor a case-alias check applies.
SEED_PATHS = PathRule("seed", grammar.is_seed_path, aliases=True)


def _admission(rule: PathRule) -> Callable[[str], bool]:
    """``admit(relative)`` of one walk: the rule's grammar and, unless the rule tolerates them, no
    entry that differs from an earlier one only in case."""

    spellings: set[str] = set()

    def admit(relative: str) -> bool:
        if not rule.is_safe(relative):
            return False
        if not rule.aliases:
            folded = relative.casefold()
            if folded in spellings:
                return False
            spellings.add(folded)
        return True
    return admit


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
                  suffixes: Collection[str] | None = None, rule: PathRule = BUNDLE_PATHS) -> dict[str, int]:
    """Return ``{relative POSIX path: size}`` for every file under ``root`` (sorted by path).

    ``root`` must be a real directory; every entry must be a directory or a single-link regular
    file whose path ``rule`` admits; empty files are refused; with ``suffixes`` every file must end
    with one of them.
    """

    for value in (max_files, max_total_bytes, max_file_bytes):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TreeError("tree bounds must be non-negative integers")
    allowed = tuple(suffixes) if suffixes is not None else None
    files: dict[str, int] = {}
    totals = {"bytes": 0, "directories": 0}
    admit = _admission(rule)

    def visit(relative: str, info: os.stat_result) -> bool:
        if not admit(relative):
            raise TreeError(f"tree path is not a canonical {rule.name} path or is a case alias: {relative!r}"[:300])
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


def _require_relative(relative: Any, rule: PathRule = BUNDLE_PATHS) -> list[str]:
    if not rule.is_safe(relative):
        raise TreeError(f"path is not a canonical {rule.name} path: {relative!r}"[:300])
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


def stream_child_file(root: Path, relative: str, *, max_bytes: int,
                      consume: Callable[[bytes], None], rule: PathRule = BUNDLE_PATHS) -> int:
    """Stream a bounded single-link child through no-follow directory descriptors.

    The protected consumer receives bounded chunks; no complete file is allocated. Caller owns
    root ancestry and rechecks the complete inventory when a multi-file operation needs it.
    ``relative`` must be a path ``rule`` admits (``EXPORT_PATHS`` for a sealed export's own files).
    """

    parts = _require_relative(relative, rule)
    if type(max_bytes) is not int or max_bytes < 1 or not callable(consume):
        raise TreeError("stream bound must be positive and consumer callable")
    root_fd = _open_root(root, "tree root")
    try:
        parent = _parent_descriptor(root_fd, parts[:-1])
        try:
            before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            size = _stream_regular(parent, parts[-1], max_bytes=max_bytes, allow_empty=False, consume=consume)
            after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
            if (after.st_dev, after.st_ino, after.st_size) != (before.st_dev, before.st_ino, before.st_size) \
                    or after.st_nlink != 1 or not stat.S_ISREG(after.st_mode):
                raise TreeError("streamed child changed identity or link count")
            return size
        finally:
            os.close(parent)
    except OSError as exc:
        raise TreeError(f"cannot stream {relative!r}: {exc.strerror or exc}"[:300]) from exc
    finally:
        os.close(root_fd)


def sha256_file(path: Path, *, max_bytes: int) -> str:
    """SHA-256 hex of one stable regular file of at most ``max_bytes`` (streamed, ``O_NOFOLLOW``)."""

    digest = hashlib.sha256()
    try:
        _stream_regular(None, Path(path), max_bytes=max_bytes, allow_empty=True, consume=digest.update)
    except OSError as exc:
        raise TreeError(f"cannot hash {Path(path).name!r}: {exc.strerror or exc}"[:300]) from exc
    return digest.hexdigest()


def file_records(root: Path, *, exclude: Collection[str] = (), max_files: int, max_total_bytes: int,
                 max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]:
    """Return the exact inventory ``[{path, sha256, size}]`` of ``root`` sorted by path, leaving out
    the relative paths in ``exclude`` (for example ``manifest.json``)."""

    excluded = set()
    for relative in exclude:
        _require_relative(relative, rule)
        excluded.add(relative)
    sizes = regular_files(root, max_files=max_files, max_total_bytes=max_total_bytes,
                          max_file_bytes=max_file_bytes, rule=rule)
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


def regular_data_records(root: Path, *, max_files: int, max_entries: int, max_total_bytes: int,
                         max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]:
    """Bounded regular data inventory, including empty files and empty directories.

    Directories count toward the entry cap but have no content record. Refuse links, special
    entries and paths outside ``rule`` (by default case aliases and noncanonical repository paths)
    before reading any file bytes. ``SEED_PATHS`` checks structure only: a Gradle cache names its
    entries freely. This is data admission, never native artifact validity or executable provenance.
    """
    if any(type(value) is not int or value < 1 for value in
           (max_files, max_entries, max_total_bytes, max_file_bytes)):
        raise TreeError("regular data bounds must be positive integers")
    paths: dict[str, int] = {}
    admit = _admission(rule)
    total = 0
    def visit(relative: str, info: os.stat_result) -> bool:
        nonlocal total
        if not admit(relative):
            raise TreeError("regular data has an unsafe or aliased path")
        if stat.S_ISDIR(info.st_mode):
            return True
        if info.st_nlink != 1 or not 0 <= info.st_size <= max_file_bytes:
            raise TreeError("regular data file has unsafe links or size")
        paths[relative] = info.st_size
        total += info.st_size
        if len(paths) > max_files or total > max_total_bytes:
            raise TreeError("regular data exceeds its file or byte cap")
        return False
    descriptor = _open_root(root, "regular data root")
    try:
        _walk(descriptor, "", depth=0, budget=[max_entries - 1], visit=visit)
        records = []
        for relative, expected_size in sorted(paths.items()):
            parts = relative.split('/')
            parent = _parent_descriptor(descriptor, parts[:-1])
            try:
                digest = hashlib.sha256()
                size = _stream_regular(parent, parts[-1], max_bytes=max_file_bytes,
                                       allow_empty=True, consume=digest.update)
                if size != expected_size:
                    raise TreeError("regular data size changed during inspection")
                records.append({"path": relative, "sha256": digest.hexdigest(), "size": size})
            finally:
                os.close(parent)
        return records
    except OSError as error:
        raise TreeError("cannot inspect regular data") from error
    finally:
        os.close(descriptor)


def _selected_data_bounds(paths: tuple[str, ...], *, max_files: int, max_entries: int,
                           max_total_bytes: int, max_file_bytes: int) -> None:
    if (any(type(value) is not int or value < 1 for value in
            (max_files, max_entries, max_total_bytes, max_file_bytes))
            or type(paths) is not tuple or not 1 <= len(paths) <= max_files
            or any(not grammar.is_repo_path(path) for path in paths)
            or paths != tuple(sorted(set(paths)))
            or len({path.casefold() for path in paths}) != len(paths)):
        raise TreeError("selected data requires bounded canonical sorted unique paths")


def selected_regular_data_records(root: Path, *, paths: tuple[str, ...], max_files: int,
                                  max_entries: int, max_total_bytes: int,
                                  max_file_bytes: int) -> list[dict[str, Any]]:
    """Hash only declared regular data leaves, including empty files.

    Bound the complete no-follow source closure before selected reads; unselected bytes are
    not read. Caller owns selection/provenance, root ancestry and writer exclusion.
    """
    _selected_data_bounds(paths, max_files=max_files, max_entries=max_entries,
                           max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes)
    validate_tree_entries(root, max_entries=max_entries)
    descriptor = _open_root(root, "selected data root")
    records = []
    total = 0
    try:
        for relative in paths:
            parts = relative.split('/')
            parent = _parent_descriptor(descriptor, parts[:-1])
            try:
                before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                        or not 0 <= before.st_size <= max_file_bytes
                        or total + before.st_size > max_total_bytes):
                    raise TreeError("selected data file exceeds type/link/byte admission")
                digest = hashlib.sha256()
                size = _stream_regular(parent, parts[-1], max_bytes=before.st_size,
                                       allow_empty=True, consume=digest.update)
                after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                stamp = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                                      info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
                if stamp(before) != stamp(after) or size != before.st_size:
                    raise TreeError("selected data changed during reading")
                total += size
                records.append({'path': relative, 'size': size, 'sha256': digest.hexdigest()})
            finally:
                os.close(parent)
        return records
    except OSError as error:
        raise TreeError("cannot inspect selected regular data") from error
    finally:
        os.close(descriptor)


def copy_selected_regular_data_files(root: Path, stage_fd: int, *, paths: tuple[str, ...],
                                     max_files: int, max_entries: int, max_total_bytes: int,
                                     max_file_bytes: int) -> list[dict[str, Any]]:
    """Copy only declared data leaves into an empty private stage, without reading other bytes."""
    _selected_data_bounds(paths, max_files=max_files, max_entries=max_entries,
                           max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes)
    return _copy_regular_files(root, stage_fd, paths=paths, max_files=max_files, max_entries=max_entries,
                               max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                               rule=REPO_PATHS, data=True)


def copy_regular_data_files(root: Path, stage_fd: int, *, max_files: int, max_entries: int, max_total_bytes: int,
                            max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]:
    """Copy regular data into an empty private stage, preserving zero-byte files.

    Empty directories are omitted. Caller owns ancestry, writer exclusion and any required
    directory skeleton. Export readers/copy APIs retain their nonempty-file contract.
    """
    return _copy_regular_files(root, stage_fd, paths=None, max_files=max_files, max_entries=max_entries,
                               max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                               rule=rule, data=True)


def validate_tree_entries(root: Path, *, max_entries: int) -> None:
    """Bound the complete no-follow regular-file/directory closure before content reads.

    Includes the root in the budget. File size, hard-link and hash admission are separate.
    """

    if type(max_entries) is not int or max_entries < 1:
        raise TreeError("tree entry cap must be a positive integer")
    source = _open_root(root, "tree root")
    try:
        _walk(source, "", depth=0, budget=[max_entries - 1], visit=lambda _path, _info: True)
    except OSError as error:
        raise TreeError("cannot inspect tree entry closure") from error
    finally:
        os.close(source)


def copy_regular_files(root: Path, stage_fd: int, *, max_files: int, max_entries: int, max_total_bytes: int,
                       max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]:
    """Stream a bounded regular-file tree into an empty caller-owned private stage.

    Creates independent single-link files, never aliases source inodes. Caller must exclude
    source writers and own the output parent; this is not UID termination or native admission.
    Entry counting includes the root and precedes all content reads. Empty directories are
    omitted; the file inventory is exact. Partial failure must remain unpublished.
    """

    return _copy_regular_files(root, stage_fd, paths=None, max_files=max_files, max_entries=max_entries,
                               max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes, rule=rule)


def copy_selected_regular_files(root: Path, stage_fd: int, *, paths: tuple[str, ...],
                                max_files: int, max_entries: int, max_total_bytes: int,
                                max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]:
    """Append declared regular files to a caller-owned private stage without replacing files.

    Inspect/recheck the entire source tree, including unselected files. Caller must protect both
    trees, bound and authenticate the complete destination union and keep partial work unpublished.
    """

    if (type(max_files) is not int or max_files < 1 or type(paths) is not tuple
            or not 1 <= len(paths) <= max_files or any(not rule.is_safe(path) for path in paths)
            or paths != tuple(sorted(set(paths))) or len({path.casefold() for path in paths}) != len(paths)):
        raise TreeError("selected copy requires bounded sorted unique canonical file paths")
    return _copy_regular_files(root, stage_fd, paths=paths, max_files=max_files, max_entries=max_entries,
                               max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes, rule=rule)


def _create_file(stage_fd: int, relative: str) -> int:
    """Create ``relative`` below a private stage (parents ``0700``) and return its write descriptor.

    Only inventory paths their rule admitted come here, so no output-name rule is applied a second
    time: a Gradle seed entry may be named with ``:`` or ``\\``. No component is followed or replaced.
    """

    parts = relative.split("/")
    parent = os.dup(stage_fd)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=parent)
            except FileExistsError:
                pass
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                       0o600, dir_fd=parent)
    finally:
        os.close(parent)


def _copy_regular_files(root: Path, stage_fd: int, *, paths: tuple[str, ...] | None,
                        max_files: int, max_entries: int, max_total_bytes: int,
                        max_file_bytes: int, rule: PathRule, data: bool = False) -> list[dict[str, Any]]:
    if type(stage_fd) is not int or stage_fd < 0:
        raise TreeError("export copy stage must be a directory descriptor")
    validate_tree_entries(root, max_entries=max_entries)
    def inventory() -> list[dict[str, Any]]:
        if data and paths is not None:
            return selected_regular_data_records(root, paths=paths, max_files=max_files, max_entries=max_entries,
                max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes)
        if data:
            return regular_data_records(root, max_files=max_files, max_entries=max_entries,
                max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes, rule=rule)
        return file_records(root, max_files=max_files, max_total_bytes=max_total_bytes,
                            max_file_bytes=max_file_bytes, rule=rule)
    records = inventory()
    selected = records
    if paths is not None:
        wanted = set(paths)
        selected = [record for record in records if record["path"] in wanted]
        if len(selected) != len(paths):
            raise TreeError("selected copy path is missing from source inventory")
    try:
        info = os.fstat(stage_fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            raise TreeError("export stage must be caller-owned and private")
        if paths is None or data:
            with os.scandir(stage_fd) as entries:
                if next(entries, None) is not None:
                    raise TreeError("export stage must be empty")
        source = _open_root(root, "export root")
        try:
            for record in selected:
                parts = record["path"].split("/")
                parent = _parent_descriptor(source, parts[:-1])
                try:
                    destination = _create_file(stage_fd, record["path"])
                    try:
                        digest = hashlib.sha256()
                        def consume(chunk: bytes) -> None:
                            digest.update(chunk)
                            remaining = memoryview(chunk)
                            while remaining:
                                written = os.write(destination, remaining)
                                if written <= 0:
                                    raise TreeError("export copy made no write progress")
                                remaining = remaining[written:]
                        size = _stream_regular(parent, parts[-1], max_bytes=record["size"],
                                               allow_empty=data, consume=consume)
                        if size != record["size"] or digest.hexdigest() != record["sha256"]:
                            raise TreeError("export bytes changed during copying")
                        os.fchmod(destination, 0o644)
                        os.fsync(destination)
                    finally:
                        os.close(destination)
                finally:
                    os.close(parent)
            os.fsync(stage_fd)
        finally:
            os.close(source)
    except OSError as error:
        raise TreeError("cannot materialize independent export copy") from error
    validate_tree_entries(root, max_entries=max_entries)
    after = inventory()
    if after != records:
        raise TreeError("export changed during materialization")
    return selected


def grant_tree_read_access(root: Path, *, source_owner_uid: int, owner_uid: int, reader_gid: int,
                           max_files: int, max_entries: int, max_total_bytes: int,
                           max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]:
    """Privileged handoff of a fresh independent private tree to one read-only group.

    Run only in protected root setup, never on a candidate-owned original. Caller authenticates
    owner/group identities and protects root ancestors. All entries must start under the one
    protected source owner; exact content is rechecked. Remove inherited POSIX ACLs, use 0640
    files/0750 directories and expose the root last. Failure keeps root traversal private.
    This cannot establish worker termination, native validity or upload authority.
    """

    def records() -> list[dict[str, Any]]:
        return file_records(root, max_files=max_files, max_total_bytes=max_total_bytes,
                            max_file_bytes=max_file_bytes, rule=rule)
    return _grant_read_access(root, source_owner_uid=source_owner_uid, owner_uid=owner_uid,
                              reader_gid=reader_gid, max_entries=max_entries,
                              records=records, path_is_safe=rule.is_safe)


def grant_regular_data_read_access(root: Path, *, source_owner_uid: int, owner_uid: int, reader_gid: int,
                                   max_files: int, max_entries: int, max_total_bytes: int,
                                   max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]:
    """Protected read-only handoff of independent regular data, including empty runtime logs.

    Same Linux Root/owner/ancestor/writer exclusions and root-last ACL/permission transfer as
    grant_tree_read_access. Complete bounded data bytes are rechecked under the one ``rule`` that
    also admits every entry; native roles and validity require separate admission. Existing
    nonempty export and source contracts are unchanged.
    """
    def records() -> list[dict[str, Any]]:
        return regular_data_records(root, max_files=max_files, max_entries=max_entries,
                                    max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes, rule=rule)
    return _grant_read_access(root, source_owner_uid=source_owner_uid, owner_uid=owner_uid,
                              reader_gid=reader_gid, max_entries=max_entries,
                              records=records, path_is_safe=rule.is_safe)


def grant_source_read_access(root: Path, *, tracked_paths: tuple[str, ...], source_owner_uid: int,
                             owner_uid: int, reader_gid: int, max_files: int, max_entries: int,
                             max_total_bytes: int, max_file_bytes: int) -> list[dict[str, Any]]:
    """Read-only regular-source handoff, including empty files and canonical repository paths.

    Exclude Git metadata and undeclared paths. Strip executable modes; retain exact bytes/blob
    hashes across transfer. The same protected-root, owner/ancestor and writer exclusions apply
    as for export handoff. No symlink or special entry is accepted.
    """

    if (type(max_files) is not int or max_files < 1
            or type(tracked_paths) is not tuple or not 1 <= len(tracked_paths) <= max_files
            or any(not grammar.is_repo_path(path) or path.split("/")[0].casefold() == ".git"
                   for path in tracked_paths)
            or tracked_paths != tuple(sorted(set(tracked_paths)))):
        raise TreeError("source handoff requires a canonical declared path inventory")
    def records() -> list[dict[str, Any]]:
        observed = source_records(root, tracked_paths=tracked_paths, generated_roots=(),
                                   max_files=max_files, max_entries=max_entries,
                                   max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                                   max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES)
        return [{key: record[key] for key in ("path", "size", "sha256", "git_blob")} for record in observed]
    return _grant_read_access(root, source_owner_uid=source_owner_uid, owner_uid=owner_uid,
                              reader_gid=reader_gid, max_entries=max_entries,
                              records=records, path_is_safe=lambda path: grammar.is_repo_path(path)
                              and path.split("/")[0].casefold() != ".git")


def privatize_tree_copy(root: Path, *, source_owner_uid: int, owner_uid: int, owner_gid: int,
                        max_files: int, max_entries: int, max_total_bytes: int,
                        max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]:
    """Transfer an independent protected private copy to its final private non-root owner.

    Protected Linux root setup only. Require one protected source owner and exact bytes, remove
    ACLs and assign 0700 directories/0600 files; transfer root last. Never use on a worker original.
    Caller authenticates copy provenance, destination identities, ancestors and excluded writers.
    No entry is Git metadata, whatever ``rule`` admits.
    """
    def records() -> list[dict[str, Any]]:
        return file_records(root, max_files=max_files, max_total_bytes=max_total_bytes,
                            max_file_bytes=max_file_bytes, rule=rule)
    return _grant_read_access(root, source_owner_uid=source_owner_uid, owner_uid=owner_uid,
                              reader_gid=owner_gid, max_entries=max_entries, records=records, private=True,
                              path_is_safe=lambda path: rule.is_safe(path) and ".git" not in path.casefold().split("/"))


def privatize_regular_data_copy(root: Path, *, source_owner_uid: int, owner_uid: int, owner_gid: int,
                                max_files: int, max_entries: int, max_total_bytes: int,
                                max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]:
    """Transfer only an independent private regular-data copy, preserving empty files.

    Same protected Linux Root/owner/ancestor/writer prerequisites as privatize_tree_copy.
    Recheck complete bounded data under the one ``rule`` that also admits every entry, remove ACLs
    and keep 0700 directories/0600 files throughout. Never transfer a candidate original; native
    validity and execution provenance are separate.
    """
    def records() -> list[dict[str, Any]]:
        return regular_data_records(root, max_files=max_files, max_entries=max_entries,
                                    max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes, rule=rule)
    return _grant_read_access(root, source_owner_uid=source_owner_uid, owner_uid=owner_uid,
                              reader_gid=owner_gid, max_entries=max_entries, records=records,
                              path_is_safe=rule.is_safe, private=True)


def _grant_read_access(root: Path, *, source_owner_uid: int, owner_uid: int, reader_gid: int,
                       max_entries: int, records: Callable[[], list[dict[str, Any]]],
                       path_is_safe: Callable[[str], bool], private: bool = False) -> list[dict[str, Any]]:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise TreeError("tree read handoff requires protected Linux root setup")
    if (type(source_owner_uid) is not int or not 0 <= source_owner_uid <= limits.MAX_CI_UNIX_ID
            or type(owner_uid) is not int or not 1 <= owner_uid <= limits.MAX_CI_UNIX_ID
            or type(reader_gid) is not int or not 1 <= reader_gid <= limits.MAX_CI_UNIX_ID):
        raise TreeError("tree handoff identities must be exact nonprivileged integers")
    validate_tree_entries(root, max_entries=max_entries)
    expected = records()
    directory_mode, file_mode = (0o700, 0o600) if private else (0o750, 0o640)
    descriptor = _open_root(root, "private export root")
    exposed = False
    try:
        initial = os.fstat(descriptor)
        if initial.st_uid != source_owner_uid or stat.S_IMODE(initial.st_mode) != 0o700:
            raise TreeError("handoff root must be private and protected-owned")
        def admission(relative: str, info: os.stat_result) -> bool:
            if (not path_is_safe(relative) or info.st_uid != source_owner_uid
                    or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                raise TreeError("handoff contains unsafe or foreign-owned entries")
            return True
        _walk(descriptor, "", depth=0, budget=[max_entries - 1], visit=admission)

        def permissions(fd: int, *, directory: bool) -> None:
            for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
                try:
                    os.removexattr(fd, attribute)
                except OSError as error:
                    if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                        raise
            os.fchown(fd, owner_uid, reader_gid)
            os.fchmod(fd, directory_mode if directory else file_mode)
            os.fsync(fd)
            actual = os.fstat(fd)
            if (actual.st_uid != owner_uid or actual.st_gid != reader_gid
                    or stat.S_IMODE(actual.st_mode) != (directory_mode if directory else file_mode)
                    or (not directory and actual.st_nlink != 1)):
                raise TreeError("handoff permissions or ownership did not bind")

        def change(relative: str, info: os.stat_result) -> bool:
            parts = relative.split("/")
            parent = _parent_descriptor(descriptor, parts[:-1])
            try:
                child = os.open(parts[-1], _DIRECTORY_FLAGS if stat.S_ISDIR(info.st_mode) else _FILE_FLAGS,
                                dir_fd=parent)
                try:
                    opened = os.fstat(child)
                    if (_identity(opened) != _identity(info) or opened.st_uid != source_owner_uid
                            or opened.st_mode != info.st_mode or opened.st_size != info.st_size):
                        raise TreeError("handoff entry changed before permission transfer")
                    permissions(child, directory=stat.S_ISDIR(info.st_mode))
                finally:
                    os.close(child)
            finally:
                os.close(parent)
            return True
        _walk(descriptor, "", depth=0, budget=[max_entries - 1], visit=change)
        after = records()
        if after != expected:
            raise TreeError("handoff bytes changed during permission transfer")
        # Root remains 0700 until all descendants and their exact byte inventory have passed.
        permissions(descriptor, directory=True)
        exposed = True
        return after
    except OSError as error:
        raise TreeError("cannot grant protected tree read access") from error
    finally:
        try:
            if not exposed:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as error:
            raise TreeError("failed handoff could not keep root private") from error
        finally:
            os.close(descriptor)


def authenticate_tree_read_access(root: Path, *, owner_uid: int, reader_gid: int,
                                  max_entries: int) -> None:
    """Recheck an already-granted regular tree before disposable read-only execution.

    Exact 0750 directories/0640 single-link files and absent access/default ACLs are required.
    This is metadata admission; callers separately authenticate ancestors and source byte hashes.
    """

    _authenticate_tree_access(root, owner_uid=owner_uid, group=reader_gid,
                               max_entries=max_entries, private=False)


def authenticate_tree_private_access(root: Path, *, owner_uid: int, owner_gid: int,
                                     max_entries: int) -> None:
    """Require exact private owner/group, 0700 dirs/0600 single-link files and absent ACLs.

    Metadata only; role/copy provenance, ancestor authentication and byte checks remain separate.
    Root ownership is allowed for a freshly protected-created independent stage/copy.
    """
    _authenticate_tree_access(root, owner_uid=owner_uid, group=owner_gid,
                               max_entries=max_entries, private=True)


def _authenticate_tree_access(root: Path, *, owner_uid: int, group: int,
                               max_entries: int, private: bool) -> None:
    if sys.platform != "linux":
        raise TreeError("tree read access authentication requires Linux")
    minimum = 0 if private else 1
    if (type(owner_uid) is not int or not minimum <= owner_uid <= limits.MAX_CI_UNIX_ID
            or type(group) is not int or not minimum <= group <= limits.MAX_CI_UNIX_ID
            or type(max_entries) is not int or max_entries < 1):
        raise TreeError("read access requires bounded exact identities and entry cap")
    descriptor = _open_root(root, "read-only tree root")
    directory_mode, file_mode = (0o700, 0o600) if private else (0o750, 0o640)
    try:
        def check_access(fd: int, expected: os.stat_result | None) -> None:
            info = os.fstat(fd)
            directory = stat.S_ISDIR(info.st_mode)
            if (not (directory or stat.S_ISREG(info.st_mode))
                    or (expected is not None and _identity(info) != _identity(expected))
                    or info.st_uid != owner_uid or info.st_gid != group
                    or stat.S_IMODE(info.st_mode) != (directory_mode if directory else file_mode)
                    or (not directory and info.st_nlink != 1)):
                raise TreeError("read-only tree permissions or identity changed")
            for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
                try:
                    os.getxattr(fd, attribute)
                except OSError as error:
                    if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                        raise
                else:
                    raise TreeError("read-only tree has an unexpected ACL")
        check_access(descriptor, None)
        def visit(relative: str, info: os.stat_result) -> bool:
            parent = _parent_descriptor(descriptor, relative.split("/")[:-1])
            try:
                child = os.open(relative.split("/")[-1],
                                _DIRECTORY_FLAGS if stat.S_ISDIR(info.st_mode) else _FILE_FLAGS,
                                dir_fd=parent)
                try:
                    check_access(child, info)
                finally:
                    os.close(child)
            finally:
                os.close(parent)
            return True
        _walk(descriptor, "", depth=0, budget=[max_entries - 1], visit=visit)
        check_access(descriptor, None)
    except OSError as error:
        raise TreeError("cannot authenticate read-only tree permissions") from error
    finally:
        os.close(descriptor)


def copy_source_files(root: Path, stage_fd: int, *, tracked_paths: Collection[str],
                      max_files: int, max_entries: int, max_total_bytes: int,
                      max_file_bytes: int, max_link_bytes: int) -> list[dict[str, Any]]:
    """Copy declared clean source into an empty private stage, omitting opaque root .git.

    Caller owns the stage and must exclude other writers. Directory-descriptor creation never
    follows source or destination parents. Partial failures leave the stage unpublished.
    """

    if type(stage_fd) is not int or stage_fd < 0:
        raise TreeError("source copy stage must be a directory descriptor")
    records = source_records(root, tracked_paths=tracked_paths, generated_roots=(),
                             max_files=max_files, max_entries=max_entries,
                             max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                             max_link_bytes=max_link_bytes)
    try:
        stage_info = os.fstat(stage_fd)
        if not stat.S_ISDIR(stage_info.st_mode) or stage_info.st_mode & 0o077:
            raise TreeError("source copy stage must be a private directory")
        with os.scandir(stage_fd) as entries:
            if next(entries, None) is not None:
                raise TreeError("source copy stage must be empty")
        source = _open_root(root, "source root")
        try:
            for record in records:
                parts = record["path"].split("/")
                destination = os.dup(stage_fd)
                parent = None
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=destination)
                        except FileExistsError:
                            pass
                        before = os.stat(part, dir_fd=destination, follow_symlinks=False)
                        child = _open_child_directory(destination, part, before)
                        os.close(destination)
                        destination = child
                    parent = _parent_descriptor(source, parts[:-1])
                    digest = hashlib.sha256()
                    if record["mode"] == "120000":
                        target = os.readlink(parts[-1].encode("utf-8"), dir_fd=parent)
                        if not isinstance(target, bytes) or len(target) != record["size"]:
                            raise TreeError("source link changed before copying")
                        digest.update(target)
                        if digest.hexdigest() != record["sha256"]:
                            raise TreeError("source link changed before copying")
                        os.symlink(target, parts[-1].encode("utf-8"), dir_fd=destination)
                    else:
                        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                             | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                                             0o600, dir_fd=destination)
                        try:
                            def consume(chunk: bytes) -> None:
                                digest.update(chunk)
                                remaining = memoryview(chunk)
                                while remaining:
                                    written = os.write(descriptor, remaining)
                                    if written <= 0:
                                        raise TreeError("source copy made no write progress")
                                    remaining = remaining[written:]
                            size = _stream_regular(parent, parts[-1], max_bytes=record["size"],
                                                   allow_empty=True, consume=consume)
                            if size != record["size"] or digest.hexdigest() != record["sha256"]:
                                raise TreeError("source bytes changed while copying")
                            os.fchmod(descriptor, 0o755 if record["mode"] == "100755" else 0o644)
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    os.fsync(destination)
                finally:
                    if parent is not None:
                        os.close(parent)
                    os.close(destination)
            os.fsync(stage_fd)
        finally:
            os.close(source)
    except OSError as error:
        raise TreeError("cannot materialize declared source copy") from error
    after = source_records(root, tracked_paths=tracked_paths, generated_roots=(),
                           max_files=max_files, max_entries=max_entries,
                           max_total_bytes=max_total_bytes, max_file_bytes=max_file_bytes,
                           max_link_bytes=max_link_bytes)
    if after != records:
        raise TreeError("source changed during materialization")
    return records


def privatize_source_copy(root: Path, *, tracked_paths: tuple[str, ...], source_owner_uid: int,
                           owner_uid: int, owner_gid: int, max_files: int, max_entries: int,
                           max_total_bytes: int, max_file_bytes: int,
                           max_link_bytes: int) -> list[dict[str, Any]]:
    """Transfer a fresh independent tracked-source copy, preserving Git modes/link bytes.

    Protected Linux Root only; caller admits copy origin, accounts/ancestors and excluded
    writers. Never use on a candidate original. Strip ACLs; directories/nonexecutables become
    0700/0600, executable files 0700. Symlink ownership changes without following the target.
    Literal targets confer no privileged filesystem or execution authority. Expose root last.
    """
    if sys.platform != "linux" or os.geteuid() != 0:
        raise TreeError("source private handoff requires protected Linux Root")
    if (type(source_owner_uid) is not int or not 0 <= source_owner_uid <= limits.MAX_CI_UNIX_ID
            or type(owner_uid) is not int or not 1 <= owner_uid <= limits.MAX_CI_UNIX_ID
            or type(owner_gid) is not int or not 1 <= owner_gid <= limits.MAX_CI_UNIX_ID
            or type(tracked_paths) is not tuple or any(not grammar.is_repo_path(path) for path in tracked_paths)
            or tracked_paths != tuple(sorted(set(tracked_paths)))):
        raise TreeError("source private handoff identities or path inventory differ")
    def records() -> list[dict[str, Any]]:
        return source_records(root, tracked_paths=tracked_paths, generated_roots=(),
            max_files=max_files, max_entries=max_entries, max_total_bytes=max_total_bytes,
            max_file_bytes=max_file_bytes, max_link_bytes=max_link_bytes)
    expected = records()
    leaves = {record['path']: record['mode'] for record in expected}
    prefixes = {path.rsplit('/', 1)[0] for path in tracked_paths if '/' in path}
    for path in tuple(prefixes):
        parts = path.split('/')
        prefixes.update('/'.join(parts[:position]) for position in range(1, len(parts)))
    descriptor = _open_root(root, "private source copy")
    exposed = False
    try:
        initial = os.fstat(descriptor)
        if initial.st_uid != source_owner_uid or stat.S_IMODE(initial.st_mode) != 0o700:
            raise TreeError("source copy root must be protected-owned and private")
        def stamp(info: os.stat_result) -> tuple[int, ...]:
            return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
                    info.st_size, info.st_nlink, info.st_mtime_ns, info.st_ctime_ns)
        def permissions(fd: int, mode: int) -> None:
            for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
                try:
                    os.removexattr(fd, attribute)
                except OSError as error:
                    if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                        raise
            os.fchown(fd, owner_uid, owner_gid)
            os.fchmod(fd, mode)
            os.fsync(fd)
        def walk(directory: int, prefix: str, depth: int, budget: list[int], phase: str) -> None:
            if depth > MAX_WALK_DEPTH:
                raise TreeError("private source exceeds depth cap")
            with os.scandir(directory) as listing:
                names = [entry.name for entry in itertools.islice(listing, budget[0] + 1)]
            if len(names) > budget[0]:
                raise TreeError("private source exceeds entry cap")
            budget[0] -= len(names)
            for name in sorted(names):
                path = prefix + name
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                mode = leaves.get(path)
                is_directory = path in prefixes and stat.S_ISDIR(before.st_mode)
                is_link = mode == '120000' and stat.S_ISLNK(before.st_mode)
                is_file = mode in {'100644', '100755'} and stat.S_ISREG(before.st_mode)
                if not (is_directory or is_link or is_file) or (not is_directory and before.st_nlink != 1):
                    raise TreeError("private source contains an undeclared or unsafe entry")
                expected_uid = owner_uid if phase == 'verify' else source_owner_uid
                if before.st_uid != expected_uid or (phase == 'verify' and before.st_gid != owner_gid):
                    raise TreeError("private source entry ownership differs")
                target_mode = 0o700 if is_directory or mode == '100755' else 0o600
                if is_link:
                    if phase == 'change':
                        os.chown(name, owner_uid, owner_gid, dir_fd=directory, follow_symlinks=False)
                        os.fsync(directory)
                    after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if (not stat.S_ISLNK(after.st_mode) or _identity(after) != _identity(before)
                            or after.st_size != before.st_size or after.st_nlink != 1
                            or (phase != 'admit' and (after.st_uid, after.st_gid) != (owner_uid, owner_gid))):
                        raise TreeError("source link changed during no-follow handoff")
                    continue
                child = os.open(name, _DIRECTORY_FLAGS if is_directory else _FILE_FLAGS, dir_fd=directory)
                try:
                    if stamp(os.fstat(child)) != stamp(before):
                        raise TreeError("source entry changed while opened for handoff")
                    if is_directory:
                        walk(child, path + '/', depth + 1, budget, phase)
                    if phase == 'change':
                        permissions(child, target_mode)
                    elif phase == 'verify':
                        if stat.S_IMODE(os.fstat(child).st_mode) != target_mode:
                            raise TreeError("private source permissions differ")
                        for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
                            try:
                                os.getxattr(child, attribute)
                            except OSError as error:
                                if error.errno not in {errno.ENODATA, errno.ENOTSUP}:
                                    raise
                            else:
                                raise TreeError("private source retained an ACL")
                    if stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != stamp(os.fstat(child)):
                        raise TreeError("source handoff entry name changed")
                finally:
                    os.close(child)
        walk(descriptor, '', 0, [max_entries - 1], 'admit')
        walk(descriptor, '', 0, [max_entries - 1], 'change')
        walk(descriptor, '', 0, [max_entries - 1], 'verify')
        if records() != expected:
            raise TreeError("private source bytes or Git modes changed during handoff")
        permissions(descriptor, 0o700)
        final = os.fstat(descriptor)
        named = Path(root).lstat()
        if (stamp(named) != stamp(final) or _identity(final) != _identity(initial)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (owner_uid, owner_gid, 0o700)):
            raise TreeError("private source root handoff did not bind")
        exposed = True
        return expected
    except OSError as error:
        raise TreeError("cannot privatize independent source copy") from error
    finally:
        try:
            if not exposed:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as error:
            raise TreeError("failed source handoff could not keep root private") from error
        finally:
            os.close(descriptor)


def source_records(root: Path, *, tracked_paths: Collection[str], generated_roots: Collection[str],
                   max_files: int, max_entries: int, max_total_bytes: int,
                   max_file_bytes: int, max_link_bytes: int) -> list[dict[str, Any]]:
    """Inspect every declared source leaf, then reject undeclared paths outside generated roots.

    Unlike export readers, source permits empty tracked files and explicitly tracked symlinks.
    Link bytes are hashed without following them. Git mode/blob identity and SHA-256 are retained.
    The root .git entry is opaque: never execute Git against it before protected replacement.
    Caller must derive paths/generated roots from protected policy and establish UID quiescence.
    """

    for value in (max_files, max_entries, max_total_bytes, max_file_bytes, max_link_bytes):
        if type(value) is not int or value < 1:
            raise TreeError("source bounds must be positive integers")
    if (not isinstance(tracked_paths, Collection) or isinstance(tracked_paths, (str, bytes))
            or not 1 <= len(tracked_paths) <= max_files):
        raise TreeError("source path inventory is empty or exceeds its file cap")
    if (not isinstance(generated_roots, Collection) or isinstance(generated_roots, (str, bytes))
            or len(generated_roots) > max_entries):
        raise TreeError("generated roots exceed their entry cap")
    paths = list(tracked_paths)
    generated = list(generated_roots)
    if any(not grammar.is_repo_path(path) for path in (*paths, *generated)):
        raise TreeError("source paths must be canonical repository paths")
    if any(path.split("/")[0].casefold() == ".git" for path in (*paths, *generated)):
        raise TreeError("source declarations cannot include Git metadata")
    if len(set(paths)) != len(paths) or len(set(generated)) != len(generated):
        raise TreeError("source paths or generated roots are duplicated")
    # Bind every parent spelling as well as leaves; a file cannot be another file's parent.
    spellings: dict[str, str] = {}
    prefixes: set[str] = set()
    for path in (*paths, *generated):
        parts = path.split("/")
        for position in range(1, len(parts) + 1):
            prefix = "/".join(parts[:position])
            folded = prefix.casefold()
            if folded in spellings and spellings[folded] != prefix:
                raise TreeError("source paths have a case alias")
            spellings[folded] = prefix
            if position < len(parts):
                prefixes.add(prefix)
    if set(paths) & prefixes:
        raise TreeError("source file is also a directory")
    if set(paths) & set(generated):
        raise TreeError("generated root is a tracked source leaf")

    records: list[dict[str, Any]] = []
    total = 0
    root_fd = _open_root(root, "source root")
    try:
        for relative in sorted(paths):
            parts = relative.split("/")
            parent = _parent_descriptor(root_fd, parts[:-1])
            try:
                before = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if before.st_nlink != 1 and not stat.S_ISDIR(before.st_mode):
                    raise TreeError("tracked source has multiple hard links")
                if before.st_size < 0 or total + before.st_size > max_total_bytes:
                    raise TreeError("source exceeds its whole-tree byte cap")
                if stat.S_ISLNK(before.st_mode):
                    if not 1 <= before.st_size <= max_link_bytes:
                        raise TreeError("tracked symlink exceeds its byte cap")
                    target = os.readlink(parts[-1].encode("utf-8"), dir_fd=parent)
                    if not isinstance(target, bytes) or len(target) != before.st_size:
                        raise TreeError("tracked symlink changed while read")
                    mode, size = "120000", len(target)
                    digest = hashlib.sha256(target)
                    blob = hashlib.sha1(b"blob " + str(size).encode("ascii") + b"\0" + target)  # Git object identity
                elif stat.S_ISREG(before.st_mode):
                    mode = "100755" if before.st_mode & stat.S_IXUSR else "100644"
                    digest = hashlib.sha256()
                    blob = hashlib.sha1(b"blob " + str(before.st_size).encode("ascii") + b"\0")
                    def consume(chunk: bytes) -> None:
                        digest.update(chunk)
                        blob.update(chunk)
                    size = _stream_regular(parent, parts[-1], max_bytes=max_file_bytes,
                                           allow_empty=True, consume=consume)
                else:
                    raise TreeError("tracked source is not a regular file or declared symlink")
                after = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                stamp = lambda info: (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_nlink,
                                      info.st_mtime_ns, info.st_ctime_ns)
                if stamp(before) != stamp(after) or size != before.st_size:
                    raise TreeError("tracked source changed during inspection")
                total += size
                if total > max_total_bytes:
                    raise TreeError("source exceeds its whole-tree byte cap")
                records.append({"path": relative, "mode": mode, "size": size,
                                "sha256": digest.hexdigest(), "git_blob": blob.hexdigest()})
            finally:
                os.close(parent)

        tracked = set(paths)
        allowed = set(generated)
        budget = [max_entries - 1]  # Include the root itself, as the native boundary does.
        def walk(directory: int, parent: str, depth: int) -> None:
            with os.scandir(directory) as entries:
                names = [entry.name for entry in itertools.islice(entries, budget[0] + 1)]
            if len(names) > budget[0]:
                raise TreeError("source exceeds its entry cap")
            budget[0] -= len(names)
            for name in sorted(names):
                relative = f"{parent}/{name}" if parent else name
                if relative == ".git":
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if relative in tracked:
                    continue  # Every tracked leaf was inspected above, including links.
                if relative not in prefixes and relative not in allowed:
                    raise TreeError("source contains an undeclared path")
                if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    raise TreeError("source parent/generated root must be a real directory")
                if relative in allowed:
                    continue  # Tracked leaves inside it were independently inspected above.
                if depth >= MAX_WALK_DEPTH:
                    raise TreeError("source exceeds its depth cap")
                child = _open_child_directory(directory, name, info)
                try:
                    walk(child, relative, depth + 1)
                finally:
                    os.close(child)
        walk(root_fd, "", 0)
    except OSError as error:
        raise TreeError("cannot inspect declared source tree") from error
    finally:
        os.close(root_fd)
    return records
