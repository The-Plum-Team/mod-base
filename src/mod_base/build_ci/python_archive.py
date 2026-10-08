"""Hash-first bounded GNU TAR inventory for reviewed Python installers (MB11).

This reads inert bytes only. The protected caller supplies a pre-approved lock, owns stable
no-follow input descriptors and must independently admit installation/runtime provenance.
No archive program executes and no filesystem extraction occurs.
"""

from __future__ import annotations

import gzip
import hashlib
import posixpath
import re
import zlib
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import BinaryIO

from mod_base.build_ci.worker import WorkerError
from mod_base.model import grammar, limits


@dataclass(frozen=True)
class PythonInstallerMember:
    path: str
    kind: str
    mode: int
    size: int
    sha256: str | None
    target: str | None


def _exact(source: BinaryIO, size: int) -> bytes:
    data = source.read(size)
    if type(data) is not bytes or len(data) != size:
        raise WorkerError("Python installer archive is truncated")
    return data


def _hash_archive(source: BinaryIO, size: int, digest: str) -> None:
    source.seek(0)
    actual = hashlib.sha256()
    remaining = size
    while remaining:
        data = source.read(min(65536, remaining))
        if type(data) is not bytes or not data or len(data) > min(65536, remaining):
            raise WorkerError("Python installer compressed size changed")
        remaining -= len(data)
        actual.update(data)
    if source.read(1) != b"" or "sha256:" + actual.hexdigest() != digest:
        raise WorkerError("Python installer bytes differ from the approved lock")
    source.seek(0)


def _number(field: bytes) -> int:
    value = field.strip(b"\0 ")
    if not value or re.fullmatch(rb"[0-7]+", value) is None:
        raise WorkerError("Python installer TAR number is not canonical octal")
    return int(value, 8)


def _text(field: bytes) -> str:
    head, separator, tail = field.partition(b"\0")
    if separator and any(tail):
        raise WorkerError("Python installer TAR text has hidden suffix bytes")
    try:
        result = head.decode("utf-8")
    except UnicodeError as exc:
        raise WorkerError("Python installer TAR text is not UTF-8") from exc
    if (not result or len(head) > limits.MAX_CI_TOOL_PATH_BYTES or "\\" in result
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in result)):
        raise WorkerError("Python installer TAR text is unsafe")
    return result


def _path(name: str, kind: bytes) -> str:
    if kind == b"5" and name.endswith("/"):
        name = name[:-1]
    if name == "." and kind == b"5":
        return ""
    if name.startswith("./"):
        name = name[2:]
    parts = name.split("/")
    if (name.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in parts)
            or len(parts) > limits.MAX_CI_TOOL_TREE_DEPTH):
        raise WorkerError("Python installer member path is unsafe")
    return name


def _validate_links(members: dict[str, PythonInstallerMember]) -> None:
    for member in members.values():
        if member.path:
            parent = posixpath.dirname(member.path)
            if parent not in members or members[parent].kind != "directory":
                raise WorkerError("Python installer member has no explicit directory parent")
        if member.kind != "link":
            continue
        current = member
        seen: set[str] = set()
        for hop in range(limits.MAX_CI_TOOL_SYMLINK_HOPS + 1):
            if current.kind != "link":
                if current.kind != "file":
                    raise WorkerError("Python installer link does not resolve to a regular file")
                break
            if current.path in seen:
                raise WorkerError("Python installer link cycle")
            if hop == limits.MAX_CI_TOOL_SYMLINK_HOPS:
                raise WorkerError("Python installer link chain exceeds its cap")
            seen.add(current.path)
            target = current.target
            if target is None:
                raise WorkerError("Python installer link target is missing")
            destination = posixpath.join(posixpath.dirname(current.path), target)
            if destination not in members:
                raise WorkerError("Python installer link target is absent")
            current = members[destination]
        else:
            raise WorkerError("Python installer link chain exceeds its cap")


def _inspect(source: BinaryIO, *, compressed_size: int,
             sink: Callable[[str, int, int], AbstractContextManager[Callable[[bytes], None] | None]] | None = None
             ) -> tuple[PythonInstallerMember, ...]:
    members: dict[str, PythonInstallerMember] = {}
    expanded = 0
    pending_name: str | None = None
    ceiling = min(limits.MAX_CI_PYTHON_EXPANDED_BYTES,
                  compressed_size * limits.MAX_CI_PYTHON_COMPRESSION_RATIO)

    def take(size: int) -> bytes:
        nonlocal expanded
        if size > ceiling - expanded:
            raise WorkerError("Python installer expanded bytes exceed their cap")
        data = _exact(source, size)
        expanded += size
        return data

    for index in range(limits.MAX_CI_PYTHON_ARCHIVE_HEADERS + 1):
        header = take(512)
        if header == bytes(512):
            if pending_name is not None or take(512) != bytes(512):
                raise WorkerError("Python installer TAR terminator is incomplete")
            padding = source.read(limits.MAX_CI_PYTHON_TAR_PADDING_BYTES + 1)
            if (type(padding) is not bytes or len(padding) > limits.MAX_CI_PYTHON_TAR_PADDING_BYTES
                    or len(padding) % 512 or any(padding)
                    or expanded + len(padding) > ceiling):
                raise WorkerError("Python installer TAR has excessive or nonzero trailing data")
            if "" not in members or members[""].kind != "directory":
                raise WorkerError("Python installer root directory is absent")
            _validate_links(members)
            return tuple(members[path] for path in sorted(members))
        if index == limits.MAX_CI_PYTHON_ARCHIVE_HEADERS:
            raise WorkerError("Python installer TAR header count exceeds its cap")
        if (header[257:265] != b"ustar  \0"
                or _number(header[148:156]) != sum(header[:148]) + 256 + sum(header[156:])):
            raise WorkerError("Python installer TAR format or checksum is invalid")
        kind = header[156:157]
        if kind not in (b"0", b"2", b"5", b"L"):
            raise WorkerError("Python installer TAR member type is unsupported")
        size, mode = _number(header[124:136]), _number(header[100:108])
        if mode > 0o777 or size > ceiling - expanded:
            raise WorkerError("Python installer TAR member size or special mode is unsafe")
        name = _text(header[:100])
        if kind == b"L":
            if pending_name is not None or name != "././@LongLink" or not 1 < size <= limits.MAX_CI_TOOL_PATH_BYTES:
                raise WorkerError("Python installer GNU long name is invalid")
            data = take(size)
            if not data.endswith(b"\0"):
                raise WorkerError("Python installer GNU long name is unterminated")
            pending_name = _text(data)
            if len(pending_name.encode("utf-8")) <= 100:
                raise WorkerError("Python installer GNU long name is redundant")
        else:
            if pending_name is not None:
                if header[:100].split(b"\0", 1)[0] != pending_name.encode("utf-8")[:100]:
                    raise WorkerError("Python installer GNU long name disagrees with its header")
                name, pending_name = pending_name, None
            path = _path(name, kind)
            if path in members:
                raise WorkerError("Python installer TAR member is duplicated")
            target, digest = None, None
            if kind == b"0":
                content = hashlib.sha256()
                remaining = size
                with (sink(path, mode, size) if sink is not None else nullcontext(None)) as write:
                    while remaining:
                        chunk = take(min(65536, remaining))
                        remaining -= len(chunk)
                        content.update(chunk)
                        if write is not None:
                            write(chunk)
                digest = content.hexdigest()
            else:
                if size:
                    raise WorkerError("Python installer link or directory has a payload")
                if kind == b"2":
                    target = _text(header[157:257])
                    if target.startswith("/") or any(part in ("", ".", "..") or ":" in part for part in target.split("/")):
                        raise WorkerError("Python installer link target escapes its directory")
            members[path] = PythonInstallerMember(path, {b"0": "file", b"2": "link", b"5": "directory"}[kind],
                                                  mode, size, digest, target)
        if any(take((-size) % 512)):
            raise WorkerError("Python installer TAR member padding is nonzero")
    raise WorkerError("Python installer TAR header count exceeds its cap")


def inspect_python_installer(archive: BinaryIO, *, expected_size: int,
                             expected_digest: str) -> tuple[PythonInstallerMember, ...]:
    """Authenticate a pre-approved compressed lock before and after inert bounded inventory.

    The caller owns stable input identity and independent publisher/profile approval. Returning
    members permits neither extraction nor execution, and proves no installed runtime closure.
    """
    if (type(expected_size) is not int or not 1 <= expected_size <= limits.MAX_CI_PYTHON_ARCHIVE_BYTES
            or type(expected_digest) is not str or grammar.DIGEST.fullmatch(expected_digest) is None):
        raise WorkerError("Python installer approved size/digest is invalid")
    try:
        _hash_archive(archive, expected_size, expected_digest)
        with gzip.GzipFile(fileobj=archive, mode="rb") as decoded:
            result = _inspect(decoded, compressed_size=expected_size)
        _hash_archive(archive, expected_size, expected_digest)
        return result
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        raise WorkerError("Python installer archive cannot be read safely") from exc
