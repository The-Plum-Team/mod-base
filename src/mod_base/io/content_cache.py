"""Bounded in-process memo for work that is a pure function of exact input bytes (MB1).

Port of Block Pops ``scripts/lib/content_cache.py``. Keys are the SHA-256 and length of the bytes
plus every parameter the result depends on; only successes are stored; mutable results are
deep-copied in and out; ``max_bytes`` weighs results with ``size`` and never stores one heavier
than the whole budget. Filesystem checks always stay outside the cached function.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from typing import TypeVar

OWNER = "MB1"
T = TypeVar("T")


class ContentCache:
    """An LRU memo bounded by ``entries`` and optionally by ``max_bytes``."""

    def __init__(self, *, entries: int, max_bytes: int | None = None) -> None:
        raise NotImplementedError("owned by MB1")

    def __len__(self) -> int:
        raise NotImplementedError("owned by MB1")

    @property
    def stored_bytes(self) -> int:
        raise NotImplementedError("owned by MB1")

    def clear(self) -> None:
        raise NotImplementedError("owned by MB1")

    def get_or_compute(self, data: bytes, parameters: tuple[Hashable, ...], compute: Callable[[], T], *,
                       size: Callable[[T], int] = lambda _value: 0) -> T:
        """Return ``compute()`` for these exact bytes and parameters, reusing an earlier success."""

        raise NotImplementedError("owned by MB1")
