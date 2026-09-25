"""Canonical metadata-free RGB PNG re-encoding (MB2).

Block Pops ``e2e/visual_evidence.canonicalize_png`` + Quick Skin snapshot: decode fully, convert to
RGB, drop every ancillary chunk and write with fixed encoder settings, so identical pixels always
give identical bytes. Used for anchors (``images/<sha>.png``) and the project icon.

:func:`pattern_png` is the conformance ``image_factory`` (``protocol.ImageFactory``): the only
source-tree PNG generator, so ``conformance --repo <mod>`` needs nothing from ``tests/``.
"""

from __future__ import annotations

from pathlib import Path

from mod_base.imaging.metrics import SizePolicy

OWNER = "MB2"


def canonical_png(source: Path | bytes, *, policy: SizePolicy | None = None) -> bytes:
    """Return the canonical PNG bytes of ``source`` (a PNG); ``policy`` optionally gates its size.
    Raises :class:`mod_base.imaging.metrics.ImageError` for anything that is not a valid PNG."""

    raise NotImplementedError("owned by MB2")


def pattern_png(width: int, height: int, seed: int = 0) -> bytes:
    """Deterministic RGB PNG bytes of ``width`` x ``height`` (at least 8x4) that pass the 8-metric
    blank checks at every size, for synthetic evidence (``protocol.ImageFactory``).

    The algorithm is ``tests/helpers.pattern_png``'s, which a parity test pins byte-for-byte: an
    8x4 grid of the eight saturated ``tests.helpers.PALETTE`` colours rotated by ``seed``
    (``PALETTE[(column + 3 * row + seed) % 8]``), scaled with ``NEAREST`` and blended 25% with
    ``Image.linear_gradient("L")`` rotated by ``45 + 90 * (seed % 4)`` degrees (``BILINEAR``,
    ``expand=False``) and resized with ``BILINEAR``; saved as PNG with ``optimize=False`` and
    ``compress_level=6``. Raises ``ValueError`` below 8x4."""

    raise NotImplementedError("owned by MB2")
