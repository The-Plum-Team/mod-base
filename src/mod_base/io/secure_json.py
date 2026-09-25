"""Bounded, race-aware, fail-closed JSON readers used at trust boundaries (MB1).

Port of Block Pops ``scripts/lib/secure_json.py`` (verbatim modulo imports), bound to the kit's
single definitions of strict and canonical JSON in :mod:`mod_base.model.canonical`: ``loads`` and
``canonical_json`` MUST be byte-for-byte equivalent to ``model.canonical.strict_loads`` and
``model.canonical.canonical_json`` (delegating to them is the intended implementation), and
``read`` MUST refuse exactly what ``model.canonical.read_regular_file`` refuses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.model import canonical

OWNER = "MB1"


class SecureJsonError(MbError):
    """JSON bytes or the file containing them are not trustworthy (exit 2)."""

    default_reason = "invalid-json"


def loads(data: bytes, *, label: str, max_bytes: int) -> Any:
    """Decode strict JSON: non-empty bytes of at most ``max_bytes``, UTF-8 without BOM, no duplicate
    object key, no NaN/Infinity. Raises :class:`SecureJsonError` naming ``label``."""

    try:
        return canonical.strict_loads(data, label=label, max_bytes=max_bytes)
    except canonical.StrictJsonError as exc:
        raise SecureJsonError(str(exc)) from exc


def read(path: Path, *, label: str, max_bytes: int) -> tuple[Any, bytes]:
    """Read one stable regular file (``O_NOFOLLOW``, same dev/inode/size before, during and after
    the read, 1..``max_bytes`` bytes) and return ``(strictly decoded value, raw bytes)``."""

    try:
        raw = canonical.read_regular_file(path, label=label, max_bytes=max_bytes)
    except canonical.StrictJsonError as exc:
        raise SecureJsonError(str(exc)) from exc
    return loads(raw, label=label, max_bytes=max_bytes), raw


def read_json(path: Path, *, label: str, max_bytes: int) -> Any:
    """``read(...)[0]``: the decoded value only."""

    return read(path, label=label, max_bytes=max_bytes)[0]


def require_object(value: Any, *, label: str, required: set[str] | frozenset[str],
                   optional: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return ``value`` when it is a dict with every ``required`` key and no key outside
    ``required | optional``; raise :class:`SecureJsonError` listing missing/unknown keys."""

    if not isinstance(value, dict):
        raise SecureJsonError(f"{label} must be an object")
    keys = set(value)
    missing = set(required) - keys
    unknown = keys - set(required) - set(optional)
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing {sorted(missing)}")
        if unknown:
            details.append(f"unknown {sorted(unknown, key=repr)[:20]}")  # bounded: hostile input may hold many keys
        raise SecureJsonError(f"{label} has " + " and ".join(details))
    return value


def canonical_json(value: Any) -> bytes:
    """Canonical document bytes: sorted keys, ``(",", ":")``, ``ensure_ascii=False``,
    ``allow_nan=False``, UTF-8, trailing newline (== ``model.canonical.canonical_json``)."""

    try:
        return canonical.canonical_json(value)
    except MbError as exc:
        raise SecureJsonError(str(exc)) from exc
