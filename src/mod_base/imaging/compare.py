"""Pairwise screenshot comparison (MB2): ``compare_screenshots``, AST-identical in both mods.

Both images are converted to RGB and must have equal sizes; an optional normalized region
``[l, t, r, b]`` crops both at ``int(l*w), int(t*h), int(r*w), int(b*h)`` (an empty box raises).
``changed_fraction`` is the share of pixels whose L-difference is >= 8 (rounded to 7 decimals);
``rms_difference`` is the RMS of the L-difference histogram (3 decimals). A changed fraction below
``minimum_changed_fraction`` raises :class:`mod_base.imaging.metrics.ImageError`.

Returned dict (SPEC §3.0 ``CompareMetrics``)::

    {changed_fraction, rms_difference, required_changed_fraction, region?}
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

OWNER = "MB2"
CHANGED_LUMA_THRESHOLD = 8


def compare(first: Path | bytes, second: Path | bytes, *, minimum_changed_fraction: float,
            region: Sequence[float] | None = None) -> dict[str, Any]:
    """Return the CompareMetrics of ``first`` -> ``second`` or raise ``ImageError``."""

    raise NotImplementedError("owned by MB2")
