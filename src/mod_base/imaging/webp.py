"""Deterministic WebP derivatives (MB2).

``Image.thumbnail(box, Resampling.LANCZOS)`` of the RGB source, saved as WebP with
``quality``, ``method`` and ``exact=True``; identical source bytes and parameters always produce
identical output bytes (``validate --bind-raw`` relies on it). The derivative size always equals
:func:`mod_base.model.documents.thumbnail_size` of the source size.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

OWNER = "MB2"


def derive_webp(source: Path | bytes, *, box: Sequence[int], quality: int, method: int) -> bytes:
    """Return the WebP derivative bytes of the PNG ``source`` (see module docstring)."""

    raise NotImplementedError("owned by MB2")
