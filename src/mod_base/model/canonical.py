"""The single strict JSON decoder, canonical encoder and bounded file reader of the kit.

These are pure primitives the document model needs before any other unit exists. ``mod_base.io``
re-exposes the same semantics for trusted-boundary readers (``io.secure_json``); there is exactly
one definition of "strict JSON" and one definition of "canonical JSON" in the kit:

* strict: bytes, non-empty, bounded, strict UTF-8 without BOM, no duplicate object keys, no
  ``NaN``/``Infinity``/``-Infinity`` literals, no other non-finite number and no key or string
  holding a lone surrogate (a ``\\ud800``-style escape decodes to a code point that is not
  Unicode text and could never be re-encoded as UTF-8);
* canonical: ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  allow_nan=False)`` UTF-8 bytes plus one trailing ``\\n``, of a value whose object keys are all
  ``str``. The SHA-256 of a written document is the SHA-256 of exactly these bytes.

A value accepted by :func:`strict_loads` is therefore always encodable by :func:`canonical_json`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from pathlib import Path
from typing import Any, NoReturn

from mod_base.errors import MbError


class StrictJsonError(MbError):
    """JSON bytes or the file holding them are not trustworthy."""

    default_reason = "invalid-json"


def _reject_constant(value: str) -> NoReturn:
    raise StrictJsonError(f"non-finite JSON number {value!r} is forbidden")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def has_surrogate(text: str) -> bool:
    """True when ``text`` holds a UTF-16 surrogate code point (U+D800..U+DFFF), which is never
    valid Unicode text and cannot be encoded as UTF-8."""

    return any("\ud800" <= character <= "\udfff" for character in text)


def _reject_untrustworthy_values(value: Any, label: str) -> None:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            raise StrictJsonError(f"{label} contains a non-finite number")
        if isinstance(item, str) and has_surrogate(item):
            raise StrictJsonError(f"{label} contains a lone surrogate escape")
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            for key in item:
                if has_surrogate(key):
                    raise StrictJsonError(f"{label} contains a lone surrogate escape in an object key")
            stack.extend(item.values())


def strict_loads(data: bytes, *, label: str, max_bytes: int) -> Any:
    """Decode strict JSON ``data`` (see module docstring) or raise :class:`StrictJsonError`."""

    if not isinstance(data, (bytes, bytearray)):
        raise StrictJsonError(f"{label} must be bytes")
    if not data:
        raise StrictJsonError(f"{label} is empty")
    if len(data) > max_bytes:
        raise StrictJsonError(f"{label} exceeds the {max_bytes}-byte input limit")
    if bytes(data[:3]) == b"\xef\xbb\xbf":
        raise StrictJsonError(f"{label} starts with a byte-order mark")
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StrictJsonError(f"{label} is not valid UTF-8") from exc
    try:
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except StrictJsonError:
        raise
    except (ValueError, RecursionError) as exc:  # JSONDecodeError, oversized integer literals, nesting
        raise StrictJsonError(f"{label} is not valid JSON: {type(exc).__name__}") from exc
    _reject_untrustworthy_values(value, label)
    return value


def _require_string_keys(value: Any) -> None:
    """``json.dumps`` silently turns ``True``/``1``/``None`` keys into strings, which would change
    a document's hash without any error; a canonical document only ever has ``str`` keys."""

    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key in item:
                if not isinstance(key, str):
                    raise MbError(f"value cannot be encoded as canonical JSON: object key {key!r}"[:200]
                                  + " is not a string")
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)


def canonical_json(value: Any) -> bytes:
    """Return the canonical document bytes of ``value`` (sorted, compact, UTF-8, trailing newline).

    Raises :class:`MbError` (never ``TypeError``/``UnicodeEncodeError``) for a value that is not
    canonical JSON: a non-``str`` object key, a non-finite number, a non-JSON type or a string
    holding a lone surrogate.
    """

    _require_string_keys(value)
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return text.encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:  # UnicodeEncodeError is a ValueError
        raise MbError(f"value cannot be encoded as canonical JSON: {type(exc).__name__}: {exc}"[:300]) from exc


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    """SHA-256 of ``canonical_json(value)``: the identity of an embedded JSON object."""

    return sha256_hex(canonical_json(value))


def read_regular_file(path: Path | str, *, label: str, max_bytes: int, allow_empty: bool = False) -> bytes:
    """Read one stable regular file without following a final-component symlink.

    The file must be a regular file before and after opening (same device, inode and size) and no
    larger than ``max_bytes``; ancestors are the caller's responsibility (``io.tree`` walks).
    """

    candidate = Path(path)
    try:
        before = candidate.lstat()
    except OSError as exc:
        raise StrictJsonError(f"cannot stat {label}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(before.st_mode):
        raise StrictJsonError(f"{label} must not be a symlink")
    if not stat.S_ISREG(before.st_mode):
        raise StrictJsonError(f"{label} must be a regular file")
    if before.st_size > max_bytes or (before.st_size == 0 and not allow_empty):
        raise StrictJsonError(f"{label} size must be between 1 and {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise StrictJsonError(f"cannot open {label}: {exc.strerror or exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev, before.st_ino, before.st_size
        ):
            raise StrictJsonError(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1 << 16, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size) != (opened.st_dev, opened.st_ino, opened.st_size) or len(
            raw
        ) != opened.st_size:
            raise StrictJsonError(f"{label} changed while it was read")
    finally:
        os.close(descriptor)
    return raw


def read_json_file(path: Path | str, *, label: str, max_bytes: int) -> tuple[Any, bytes]:
    """Read and strictly decode one JSON document; return ``(value, raw_bytes)``."""

    raw = read_regular_file(path, label=label, max_bytes=max_bytes)
    return strict_loads(raw, label=label, max_bytes=max_bytes), raw
