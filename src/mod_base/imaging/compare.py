"""Pairwise screenshot comparison (MB2): ``compare_screenshots``, AST-identical in both mods.

Both images are converted to RGB and must have equal sizes; an optional normalized region
``[l, t, r, b]`` crops both at ``int(l*w), int(t*h), int(r*w), int(b*h)`` (an empty box raises).
``changed_fraction`` is the share of pixels whose L-difference is >= 8 (rounded to 7 decimals);
``rms_difference`` is the RMS of the L-difference histogram (3 decimals). A changed fraction below
``minimum_changed_fraction`` raises :class:`mod_base.imaging.metrics.ImageError`.

Returned dict (SPEC §3.0 ``CompareMetrics``)::

    {changed_fraction, rms_difference, required_changed_fraction, region?}

``required_changed_fraction`` and ``region`` echo the arguments. The kit adds the input checks
the mods leave to their callers: both images are static PNGs or both static WebPs (sources are
compared as PNG, derivatives re-measured as WebP) within the byte, pixel and dimension bounds,
both formats and byte bounds checked from the signatures before either image is decoded,
``minimum_changed_fraction`` is a finite number in ``[0, 1]`` and ``region`` satisfies the
``CompareMetrics`` region rule ``0 <= l < r <= 1``, ``0 <= t < b <= 1``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mod_base.imaging.metrics import ImageError, _admit, _decode_rgb, _label, _read_source

OWNER = "MB2"
CHANGED_LUMA_THRESHOLD = 8

_FORMATS = ("PNG", "WEBP")


def compare(first: Path | bytes, second: Path | bytes, *, minimum_changed_fraction: float,
            region: Sequence[float] | None = None) -> dict[str, Any]:
    """Return the CompareMetrics of ``first`` -> ``second`` or raise ``ImageError``."""

    if not _is_fraction(minimum_changed_fraction):
        raise ImageError("minimum_changed_fraction must be a finite number in [0, 1]")
    if region is not None:
        _check_region(region)
    first_payload, first_format, first_label = _admitted(first)
    second_payload, second_format, second_label = _admitted(second)
    if first_format != second_format:
        raise ImageError(f"cannot compare a {first_format} image with a {second_format} image")
    first_rgb = _decode_rgb(first_payload, formats=(first_format,), label=first_label)[1]
    second_rgb = _decode_rgb(second_payload, formats=(second_format,), label=second_label)[1]

    from PIL import ImageChops

    if first_rgb.size != second_rgb.size:
        raise ImageError(
            f"screenshots changed dimensions unexpectedly: {first_rgb.size} -> {second_rgb.size}"
        )
    try:
        if region is not None:
            width, height = first_rgb.size
            left, top, right, bottom = region
            box = (int(left * width), int(top * height), int(right * width), int(bottom * height))
            if box[0] >= box[2] or box[1] >= box[3]:
                raise ImageError(f"comparison region {list(region)} is empty at {width}x{height}")
            first_rgb = first_rgb.crop(box)
            second_rgb = second_rgb.crop(box)
        difference = ImageChops.difference(first_rgb, second_rgb).convert("L")
        histogram = difference.histogram()
        pixels = difference.width * difference.height
        changed_fraction = sum(histogram[CHANGED_LUMA_THRESHOLD:]) / pixels
        rms_difference = (sum(value * value * count for value, count in enumerate(histogram)) / pixels) ** 0.5
    except ImageError:
        raise
    except Exception as exc:  # Pillow failures on already decoded images
        raise ImageError(f"cannot compare screenshots: {exc}") from exc

    if changed_fraction < minimum_changed_fraction:
        scope = "in region " + repr(list(region)) if region is not None else "over the frame"
        raise ImageError(
            f"screenshots expected to change did not change enough {scope} "
            f"(changed={changed_fraction:.7f}, required={minimum_changed_fraction:.7f})"
        )
    comparison: dict[str, Any] = {
        "changed_fraction": round(changed_fraction, 7),
        "rms_difference": round(rms_difference, 3),
        "required_changed_fraction": minimum_changed_fraction,
    }
    if region is not None:
        comparison["region"] = list(region)
    return comparison


def _admitted(source: Path | bytes) -> tuple[bytes, str, str]:
    """``(payload, format, label)`` of one side, its format and byte bound checked before either
    image is decoded."""

    label = _label(source, "") if isinstance(source, Path) else "compared image"
    payload = _read_source(source, formats=_FORMATS, label=label)
    return payload, _admit(payload, formats=_FORMATS, label=label), label


def _is_fraction(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 <= value <= 1)


def _check_region(region: Any) -> None:
    if isinstance(region, (str, bytes)) or not isinstance(region, Sequence) or len(region) != 4:
        raise ImageError("comparison region must be a [left, top, right, bottom] box")
    if not all(_is_fraction(coordinate) for coordinate in region):
        raise ImageError("comparison region coordinates must be finite numbers in [0, 1]")
    left, top, right, bottom = region
    if not (left < right and top < bottom):
        raise ImageError("comparison region must satisfy left < right and top < bottom")
