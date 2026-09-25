"""Deterministic WebP derivatives (MB2).

``Image.thumbnail(box, Resampling.LANCZOS)`` of the RGB source, saved as WebP with
``quality``, ``method`` and ``exact=True``; identical source bytes and parameters always produce
identical output bytes (``validate --bind-raw`` relies on it). The derivative size always equals
:func:`mod_base.model.documents.thumbnail_size` of the source size.

This is Quick Skin ``scripts/pages/evidence._encode_webp`` (box 1600x900) and Block Pops
``_encode_webp_uncached`` (box 1280x720), both at ``quality=82, method=6``, with the box and the
encoder settings as parameters (``expectation.image_policy``). The source must be one static PNG
within the byte, pixel and dimension bounds; the output is decoded again and must be one static
WebP of the expected size within ``limits.MAX_DERIVATIVE_BYTES``. WebP's single-frame writer takes
ICC, EXIF and XMP only from explicit save arguments, so the derivative carries no source metadata.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from pathlib import Path

from mod_base.imaging.metrics import (
    ImageError,
    SizePolicy,
    _MAX_BYTES,
    _decode_rgb,
    _is_dimension,
    _label,
    _read_source,
)
from mod_base.model import limits
from mod_base.model.documents import thumbnail_size

OWNER = "MB2"


def derive_webp(source: Path | bytes, *, box: Sequence[int], quality: int, method: int) -> bytes:
    """Return the WebP derivative bytes of the PNG ``source`` (see module docstring)."""

    if (isinstance(box, (str, bytes)) or not isinstance(box, Sequence) or len(box) != 2
            or not all(_is_dimension(side) for side in box)):
        raise ImageError(f"derivative box must be two integer sides in 1..{limits.MAX_IMAGE_DIMENSION}")
    box_size = (box[0], box[1])
    _require_int("webp quality", quality, 1, 100)
    _require_int("webp method", method, 0, 6)
    label = _label(source, "PNG")
    payload = _read_source(source, formats=("PNG",), label=label)
    rendered = _decode_rgb(payload, formats=("PNG",), label=label)[1]
    expected = thumbnail_size(rendered.size, box_size)

    from PIL import Image

    try:
        rendered.thumbnail(box_size, Image.Resampling.LANCZOS)
        output = io.BytesIO()
        rendered.save(output, "WEBP", quality=quality, method=method, exact=True)
        encoded = output.getvalue()
    except Exception as exc:  # an encoder failure is a rejection, never a partial derivative
        raise ImageError(f"cannot create the WebP derivative of {label}: {exc}") from exc
    if rendered.size != expected:
        raise ImageError(f"the WebP derivative of {label} is {rendered.size}, expected {expected}")
    if not encoded or len(encoded) > _MAX_BYTES["WEBP"]:
        raise ImageError(f"the WebP derivative of {label} exceeds its byte bound")
    _decode_rgb(encoded, formats=("WEBP",), label=f"WebP derivative of {label}",
                policy=SizePolicy.exact(*expected))
    return encoded


def _require_int(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ImageError(f"{name} must be an integer in {minimum}..{maximum}")
