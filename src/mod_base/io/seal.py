"""Seal a generated output tree against its written bytes (MB1).

Port of Block Pops ``build_site._seal_output``: walk the stage through directory descriptors,
require the entry set to equal exactly the written paths, every file to be a regular single-link
file whose bytes hash to the written bytes, and every directory/file stamp (dev, inode, mode,
nlink, size, mtime_ns, ctime_ns) to stay unchanged across a second, stamp-only pass. The
returned ``recheck`` callables can be run again immediately before publication.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping

from mod_base.errors import MbError
from mod_base.model import grammar, limits

OWNER = "MB1"

_Stamp = tuple[int, int, int, int, int, int, int]


class SealError(MbError):
    """The generated tree differs from what was written, or changed while being sealed (exit 2)."""

    default_reason = "seal"


def _stamp(info: os.stat_result) -> _Stamp:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns)


def _children(expected: Mapping[str, bytes], max_files: int, max_bytes: int) -> dict[str, set[str]]:
    """Directory (``""`` is the stage) -> the exact names it must hold, after checking the bounds."""

    if not isinstance(expected, Mapping):
        raise SealError("sealed output must be a mapping of relative paths to bytes")
    count = len(expected)
    if count > max_files:
        raise SealError("generated output exceeds its file-count bound")
    total = 0
    for relative, data in expected.items():
        if not grammar.is_bundle_path(relative):
            raise SealError(f"generated output path is not canonical: {relative!r}"[:200])
        if not isinstance(data, (bytes, bytearray)):
            raise SealError("generated output bytes must be bytes")
        total += len(data)
        if total > max_bytes:
            raise SealError("generated output exceeds its byte bound")
    children: dict[str, set[str]] = {"": set()}
    for relative in expected:
        parent = ""
        for name in relative.split("/"):
            children.setdefault(parent, set()).add(name)
            parent = f"{parent}/{name}" if parent else name
    collision = sorted(set(expected) & set(children))
    if collision:
        raise SealError(f"generated output uses {collision[0]!r} as both a file and a directory")
    return children


def seal_output(stage_fd: int, expected: Mapping[str, bytes], *, max_files: int = limits.MAX_SITE_FILES,
                max_bytes: int = limits.MAX_SITE_BYTES,
                rechecks: list[Callable[[], None]] | None = None) -> tuple[int, int]:
    """Verify the stage behind ``stage_fd`` holds exactly ``expected`` (relative path -> bytes).

    Returns ``(file_count, total_bytes)``; appends a stamp-only recheck to ``rechecks`` when given.
    Raises :class:`SealError` on any extra/missing entry, link, special file, size, byte or stamp
    difference, or when the bounds are exceeded (checked before any hashing).
    """

    children = _children(expected, max_files, max_bytes)
    count, total = len(expected), sum(len(data) for data in expected.values())
    stamps: dict[str, _Stamp] = {}

    def walk(directory: int, parent: str, *, hash_bytes: bool) -> None:
        initial = _stamp(os.fstat(directory))
        if set(os.listdir(directory)) != children[parent]:
            raise SealError("generated output inventory differs from the written paths")
        for name in sorted(children[parent]):
            relative = f"{parent}/{name}" if parent else name
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            is_directory = relative in children
            if not (stat.S_ISDIR(before.st_mode) if is_directory
                    else stat.S_ISREG(before.st_mode) and before.st_nlink == 1):
                raise SealError("generated output contains an unexpected file type or link")
            flags = (os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
                     | (os.O_DIRECTORY if is_directory else os.O_NONBLOCK))
            descriptor = os.open(name, flags, dir_fd=directory)
            try:
                if _stamp(os.fstat(descriptor)) != _stamp(before):
                    raise SealError("generated output entry changed while opening")
                if is_directory:
                    walk(descriptor, relative, hash_bytes=hash_bytes)
                elif hash_bytes:
                    wanted = expected[relative]
                    if before.st_size != len(wanted):
                        raise SealError("generated output file size differs from the written bytes")
                    digest = hashlib.sha256()
                    remaining = len(wanted) + 1
                    while remaining:
                        chunk = os.read(descriptor, min(65536, remaining))
                        if not chunk:
                            break
                        digest.update(chunk)
                        remaining -= len(chunk)
                    if remaining != 1 or digest.digest() != hashlib.sha256(wanted).digest():
                        raise SealError("generated output bytes differ from the written bytes")
                elif stamps[relative] != _stamp(before):
                    raise SealError("generated output file changed after byte verification")
                if (_stamp(os.fstat(descriptor)) != _stamp(before)
                        or _stamp(os.stat(name, dir_fd=directory, follow_symlinks=False)) != _stamp(before)):
                    raise SealError("generated output entry changed during verification")
                if hash_bytes:
                    stamps[relative] = _stamp(before)
            finally:
                os.close(descriptor)
        if _stamp(os.fstat(directory)) != initial or set(os.listdir(directory)) != children[parent]:
            raise SealError("generated output directory changed during verification")
        if hash_bytes:
            stamps[parent] = initial
        elif stamps[parent] != initial:
            raise SealError("generated output directory changed after byte verification")

    def recheck() -> None:
        try:
            walk(stage_fd, "", hash_bytes=False)
        except OSError as exc:
            raise SealError(f"cannot recheck the sealed output: {exc.strerror or exc}") from exc

    try:
        walk(stage_fd, "", hash_bytes=True)
    except OSError as exc:
        raise SealError(f"cannot seal the generated output: {exc.strerror or exc}") from exc
    recheck()
    if rechecks is not None:
        rechecks.append(recheck)
    return count, total
