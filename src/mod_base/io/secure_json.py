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

OWNER = "MB1"


class SecureJsonError(MbError):
    """JSON bytes or the file containing them are not trustworthy (exit 2)."""

    default_reason = "invalid-json"


def loads(data: bytes, *, label: str, max_bytes: int) -> Any:
    """Decode strict JSON: non-empty bytes of at most ``max_bytes``, UTF-8 without BOM, no duplicate
    object key, no NaN/Infinity. Raises :class:`SecureJsonError` naming ``label``."""

    raise NotImplementedError("owned by MB1")


def read(path: Path, *, label: str, max_bytes: int) -> tuple[Any, bytes]:
    """Read one stable regular file (``O_NOFOLLOW``, same dev/inode/size before, during and after
    the read, 1..``max_bytes`` bytes) and return ``(strictly decoded value, raw bytes)``."""

    raise NotImplementedError("owned by MB1")


def read_json(path: Path, *, label: str, max_bytes: int) -> Any:
    """``read(...)[0]``: the decoded value only."""

    raise NotImplementedError("owned by MB1")


def require_object(value: Any, *, label: str, required: set[str] | frozenset[str],
                   optional: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Return ``value`` when it is a dict with every ``required`` key and no key outside
    ``required | optional``; raise :class:`SecureJsonError` listing missing/unknown keys."""

    raise NotImplementedError("owned by MB1")


def canonical_json(value: Any) -> bytes:
    """Canonical document bytes: sorted keys, ``(",", ":")``, ``ensure_ascii=False``,
    ``allow_nan=False``, UTF-8, trailing newline (== ``model.canonical.canonical_json``)."""

    raise NotImplementedError("owned by MB1")
