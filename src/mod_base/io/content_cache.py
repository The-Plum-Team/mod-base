"""Bounded in-process memo for work that is a pure function of exact input bytes (MB1).

Port of Block Pops ``scripts/lib/content_cache.py``. Keys are the SHA-256 and length of the bytes
plus every parameter the result depends on; only successes are stored; mutable results are
deep-copied in and out; ``max_bytes`` weighs results with ``size`` and never stores one heavier
than the whole budget. Filesystem checks always stay outside the cached function.
"""

from __future__ import annotations

import copy
import hashlib
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, TypeVar

OWNER = "MB1"
T = TypeVar("T")


class ContentCache:
    """An LRU memo bounded by ``entries`` and optionally by ``max_bytes``."""

    def __init__(self, *, entries: int, max_bytes: int | None = None) -> None:
        if (isinstance(entries, bool) or not isinstance(entries, int) or entries <= 0
                or (max_bytes is not None and (isinstance(max_bytes, bool) or not isinstance(max_bytes, int)
                                               or max_bytes <= 0))):
            raise ValueError("content cache bounds must be positive integers")
        self._entries = entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._values: OrderedDict[tuple[Hashable, ...], tuple[Any, int]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._values)

    @property
    def stored_bytes(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._values.clear()
        self._bytes = 0

    def get_or_compute(self, data: bytes, parameters: tuple[Hashable, ...], compute: Callable[[], T], *,
                       size: Callable[[T], int] = lambda _value: 0) -> T:
        """Return ``compute()`` for these exact bytes and parameters, reusing an earlier success.

        Mutable results are deep-copied on the way in and out, so no caller can alter what a
        later hit returns. With ``max_bytes``, ``size`` weighs each result against that budget; one
        heavier than the whole budget is returned but never stored. A failing ``compute`` stores
        nothing, so a rejection always runs the real path again with its own caller's label.
        """

        if not isinstance(data, (bytes, bytearray)) or not isinstance(parameters, tuple):
            raise TypeError("content cache keys are exact bytes plus a parameter tuple")
        key = (hashlib.sha256(data).hexdigest(), len(data), *parameters)
        cached = self._values.get(key)
        if cached is not None:
            self._values.move_to_end(key)
            return copy.deepcopy(cached[0])
        value = compute()
        weight = size(value)
        if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
            raise ValueError("content cache result size must be a non-negative integer")
        if self._max_bytes is None or weight <= self._max_bytes:
            self._values[key] = (copy.deepcopy(value), weight)
            self._bytes += weight
            while len(self._values) > self._entries or (self._max_bytes is not None and self._bytes > self._max_bytes):
                _, (_, evicted) = self._values.popitem(last=False)
                self._bytes -= evicted
        return value
