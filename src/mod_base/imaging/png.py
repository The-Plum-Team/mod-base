"""Canonical metadata-free RGB PNG re-encoding (MB2).

Block Pops ``e2e/visual_evidence.canonicalize_png`` + Quick Skin snapshot: decode fully, convert to
RGB, drop every ancillary chunk and write with fixed encoder settings, so identical pixels always
give identical bytes. Used for anchors (``images/<sha>.png``) and the project icon.

The pixels are re-wrapped with ``Image.frombytes("RGB", size, rgb.tobytes())`` (no ``info``, so
only ``IHDR``/``IDAT``/``IEND`` are written) and saved with ``optimize=False, compress_level=9``,
exactly as both mods do; the result is decoded again and must be a same-size 8-bit RGB PNG of the
same pixels, within ``limits.MAX_SOURCE_PNG_BYTES``.

:func:`pattern_png` is the conformance ``image_factory`` (``protocol.ImageFactory``): the only
source-tree PNG generator, so ``conformance --repo <mod>`` needs nothing from ``tests/``.
"""

from __future__ import annotations

import io
from pathlib import Path

from mod_base.imaging.metrics import (
    ImageError,
    SizePolicy,
    _MAX_BYTES,
    _decode_rgb,
    _label,
    _read_source,
    _within_bounds,
)

OWNER = "MB2"

#: IHDR bit depth 8 and colour type 2 (truecolour): the only encoding :func:`canonical_png` emits.
_RGB8 = bytes((8, 2))

# The eight saturated block colours of the pattern (``tests.helpers.PALETTE``).
_PALETTE = (
    (214, 61, 61),
    (54, 170, 92),
    (66, 92, 206),
    (228, 196, 70),
    (150, 78, 196),
    (58, 184, 196),
    (236, 136, 58),
    (118, 122, 128),
)


def canonical_png(source: Path | bytes, *, policy: SizePolicy | None = None) -> bytes:
    """Return the canonical PNG bytes of ``source`` (a PNG); ``policy`` optionally gates its size.
    Raises :class:`mod_base.imaging.metrics.ImageError` for anything that is not a valid PNG."""

    if policy is not None and not isinstance(policy, SizePolicy):
        raise ImageError("an image size policy must be a SizePolicy")
    label = _label(source, "PNG")
    payload = _read_source(source, formats=("PNG",), label=label)
    rgb = _decode_rgb(payload, formats=("PNG",), label=label, policy=policy)[1]

    from PIL import Image

    try:
        size = rgb.size
        pixels = rgb.tobytes()
        encoded = io.BytesIO()
        Image.frombytes("RGB", size, pixels).save(encoded, format="PNG", optimize=False, compress_level=9)
        canonical = encoded.getvalue()
    except Exception as exc:  # an encoder failure is a rejection, never a partial result
        raise ImageError(f"cannot re-encode {label}: {exc}") from exc
    if not canonical or len(canonical) > _MAX_BYTES["PNG"]:
        raise ImageError(f"canonical {label} exceeds its byte bound")
    if canonical[12:16] != b"IHDR" or canonical[24:26] != _RGB8:
        raise ImageError(f"canonical {label} is not an 8-bit RGB PNG")
    check = _decode_rgb(canonical, formats=("PNG",), label=f"canonical {label}", policy=policy)[1]
    if check.size != size or check.tobytes() != pixels:
        raise ImageError(f"canonical {label} changed its pixels")
    return canonical


def pattern_png(width: int, height: int, seed: int = 0) -> bytes:
    """Deterministic RGB PNG bytes of ``width`` x ``height`` (at least 8x4) that pass the 8-metric
    blank checks at every size, for synthetic evidence (``protocol.ImageFactory``).

    The algorithm is ``tests/helpers.pattern_png``'s, which a parity test pins byte-for-byte: an
    8x4 grid of the eight saturated ``tests.helpers.PALETTE`` colours rotated by ``seed``
    (``PALETTE[(column + 3 * row + seed) % 8]``), scaled with ``NEAREST`` and blended 25% with
    ``Image.linear_gradient("L")`` rotated by ``45 + 90 * (seed % 4)`` degrees (``BILINEAR``,
    ``expand=False``) and resized with ``BILINEAR``; saved as PNG with ``optimize=False`` and
    ``compress_level=6``. Raises ``ValueError`` below 8x4."""

    for name, value in (("width", width), ("height", height), ("seed", seed)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"pattern_png {name} must be an integer")
    if width < 8 or height < 4:
        raise ValueError("pattern_png needs at least 8x4 pixels")
    if not _within_bounds(width, height):
        raise ValueError("pattern_png size exceeds the image pixel or dimension bound")

    from PIL import Image

    blocks = Image.new("RGB", (8, 4))
    blocks.putdata([_PALETTE[(column + 3 * row + seed) % len(_PALETTE)]
                    for row in range(4) for column in range(8)])
    base = blocks.resize((width, height), Image.Resampling.NEAREST)
    gradient = Image.linear_gradient("L").rotate(45 + 90 * (seed % 4), resample=Image.Resampling.BILINEAR,
                                                 expand=False).resize((width, height), Image.Resampling.BILINEAR)
    shaded = Image.blend(base, Image.merge("RGB", (gradient, gradient, gradient)), 0.25)
    stream = io.BytesIO()
    shaded.save(stream, format="PNG", optimize=False, compress_level=6)
    return stream.getvalue()
