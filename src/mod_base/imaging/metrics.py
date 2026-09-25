"""The 8-key PixelMetrics inspection (MB2), ``pixel_metrics_version`` 1.

Quick Skin ``packaged_runtime.inspect_screenshot`` == Block Pops ``_screenshot_metrics`` (V11):
decode fully (``Image.MAX_IMAGE_PIXELS = 20_000_000``), convert to RGB, bilinear-resample to
160x90, and reject an effectively blank image when ``luma_entropy < 0.75``, the maximum channel
standard deviation ``< 2.0``, fewer than 4 meaningful colours (32-colour quantization, a colour
counts when it covers at least ``max(2, pixels // 1000)`` samples), a dark fraction (luma < 8)
``> 0.98`` or a light fraction (luma >= 248) ``> 0.995``. Entropy is rounded to 3 decimals and the
fractions to 4. Only the size gate differs between mods; it is the :class:`SizePolicy` parameter.

Returned dict (exactly 8 keys, SPEC §3.0 ``PixelMetrics``)::

    {width, height, file_sha256, pixel_sha256, luma_entropy, meaningful_colors,
     dark_fraction, light_fraction}

``pixel_sha256`` is the SHA-256 of the RGB ``tobytes()``; ``file_sha256`` of the encoded bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.model import limits

OWNER = "MB2"

SAMPLE_SIZE = (160, 90)
MIN_LUMA_ENTROPY = 0.75
MIN_CHANNEL_STDDEV = 2.0
MIN_MEANINGFUL_COLORS = 4
MAX_DARK_FRACTION = 0.98
MAX_LIGHT_FRACTION = 0.995
QUANTIZE_COLORS = 32
MAX_IMAGE_PIXELS = limits.MAX_IMAGE_PIXELS
METRIC_KEYS = ("width", "height", "file_sha256", "pixel_sha256", "luma_entropy", "meaningful_colors",
               "dark_fraction", "light_fraction")


class ImageError(MbError):
    """An image is corrupt, of the wrong format or size, blank, or differs from its record."""

    default_reason = "image"


@dataclass(frozen=True)
class SizePolicy:
    """``exact``: dimensions must equal ``(width, height)``; ``minimum``: at least that size."""

    mode: str
    width: int
    height: int

    @classmethod
    def exact(cls, width: int, height: int) -> "SizePolicy":
        return cls("exact", width, height)

    @classmethod
    def minimum(cls, width: int, height: int) -> "SizePolicy":
        return cls("minimum", width, height)

    def allows(self, width: int, height: int) -> bool:
        """True when an image of ``width`` x ``height`` satisfies this policy and the pixel bound."""

        raise NotImplementedError("owned by MB2")


def inspect_png(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]:
    """Decode a PNG (format must be ``PNG``) and return its PixelMetrics; raise :class:`ImageError`
    when it is not a PNG, violates ``policy`` or is effectively blank."""

    raise NotImplementedError("owned by MB2")


def inspect_webp(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]:
    """The same inspection for a WebP derivative (format must be ``WEBP``)."""

    raise NotImplementedError("owned by MB2")
