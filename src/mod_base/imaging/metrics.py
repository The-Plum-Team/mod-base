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

Every image entering the kit is untrusted. A ``Path`` source is read once as a stable regular file
(``O_NOFOLLOW``, same device/inode/size/mtime while reading) of at most the format's byte bound
(``MAX_SOURCE_PNG_BYTES`` for PNG, ``MAX_DERIVATIVE_BYTES`` for WebP); a ``bytes`` source obeys the
same bound. The format is identified from the file signature before Pillow sees the bytes: only
the expected format passes, its byte bound is applied, and only that Pillow plugin may open it. A
multi-frame (APNG or animated WebP) image is refused, and the dimensions are checked against the
pixel and dimension bounds before any pixel is decoded (a WebP's declared canvas even before
Pillow opens it); Pillow's decompression-bomb warning is an error. Every Pillow failure becomes
an :class:`ImageError`. The private helpers here are shared by the sibling imaging modules.

Containment is the caller's job: these functions take no root, so a ``Path`` must already be a
contained, tree-validated file (``adapter.api.RuntimeTree.path``, ``io.tree``), or the caller
passes ``bytes``. Only the final component is opened without following a symlink; a path with a
``..`` component is refused, and a symlinked ancestor directory cannot be judged without a root.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import warnings
from collections.abc import Collection
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

#: Byte bound of an encoded image per Pillow format (SPEC §3.0 "Source PNG" / "Derivative WebP").
_MAX_BYTES = {"PNG": limits.MAX_SOURCE_PNG_BYTES, "WEBP": limits.MAX_DERIVATIVE_BYTES}
_DARK_LUMA = 8  # histogram[:8]: luma < 8
_LIGHT_LUMA = 248  # histogram[248:]: luma >= 248
#: The PNG file signature (``PngImagePlugin._accept``).
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: The first RIFF chunks Pillow's WebP plugin accepts (``WebPImagePlugin._accept``).
_WEBP_CHUNKS = (b"VP8 ", b"VP8L", b"VP8X")


class ImageError(MbError):
    """An image is corrupt, of the wrong format or size, blank, or differs from its record."""

    default_reason = "image"


@dataclass(frozen=True)
class SizePolicy:
    """``exact``: dimensions must equal ``(width, height)``; ``minimum``: at least that size.

    A policy no image could satisfy (a non-positive or non-integer size, a side above
    ``limits.MAX_IMAGE_DIMENSION`` or an area above :data:`MAX_IMAGE_PIXELS`) raises
    :class:`ImageError` when constructed.
    """

    mode: str
    width: int
    height: int

    def __post_init__(self) -> None:
        if self.mode not in ("exact", "minimum"):
            raise ImageError(f"unknown image size policy mode {self.mode!r}")
        if not _is_dimension(self.width) or not _is_dimension(self.height):
            raise ImageError(f"image size policy needs integer sides in 1..{limits.MAX_IMAGE_DIMENSION}")
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ImageError(f"image size policy {self.width}x{self.height} exceeds the pixel bound")

    @classmethod
    def exact(cls, width: int, height: int) -> "SizePolicy":
        return cls("exact", width, height)

    @classmethod
    def minimum(cls, width: int, height: int) -> "SizePolicy":
        return cls("minimum", width, height)

    def allows(self, width: int, height: int) -> bool:
        """True when an image of ``width`` x ``height`` satisfies this policy and the pixel bound."""

        if not _within_bounds(width, height):
            return False
        if self.mode == "exact":
            return width == self.width and height == self.height
        return width >= self.width and height >= self.height


def inspect_png(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]:
    """Decode a PNG (format must be ``PNG``) and return its PixelMetrics; raise :class:`ImageError`
    when it is not a PNG, violates ``policy`` or is effectively blank."""

    return _inspect(source, policy, "PNG")


def inspect_webp(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]:
    """The same inspection for a WebP derivative (format must be ``WEBP``)."""

    return _inspect(source, policy, "WEBP")


def _inspect(source: Path | bytes, policy: SizePolicy, image_format: str) -> dict[str, Any]:
    if not isinstance(policy, SizePolicy):
        raise ImageError("an image size policy is required")
    label = _label(source, image_format)
    payload = _read_source(source, formats=(image_format,), label=label)
    rgb = _decode_rgb(payload, formats=(image_format,), label=label, policy=policy)[1]
    return _metrics(payload, rgb, label)


def _metrics(payload: bytes, rgb: Any, label: str) -> dict[str, Any]:
    """The PixelMetrics of the decoded RGB image of ``payload`` (the V11 algorithm, verbatim)."""

    from PIL import Image, ImageStat

    try:
        sample = rgb.resize(SAMPLE_SIZE, Image.Resampling.BILINEAR)
        luma = sample.convert("L")
        entropy = float(luma.entropy())
        channel_stddev = [float(value) for value in ImageStat.Stat(sample).stddev]
        palette_counts = sample.quantize(colors=QUANTIZE_COLORS).getcolors() or []
        sample_pixels = sample.width * sample.height
        meaningful_colors = sum(count >= max(2, sample_pixels // 1000) for count, _ in palette_counts)
        luma_histogram = luma.histogram()
        dark_fraction = sum(luma_histogram[:_DARK_LUMA]) / sample_pixels
        light_fraction = sum(luma_histogram[_LIGHT_LUMA:]) / sample_pixels
        pixel_sha256 = hashlib.sha256(rgb.tobytes()).hexdigest()
    except Exception as exc:  # Pillow reports hostile pixel data through many exception types
        raise ImageError(f"{label} cannot be decoded: {exc}") from exc
    if (
        entropy < MIN_LUMA_ENTROPY
        or max(channel_stddev) < MIN_CHANNEL_STDDEV
        or meaningful_colors < MIN_MEANINGFUL_COLORS
        or dark_fraction > MAX_DARK_FRACTION
        or light_fraction > MAX_LIGHT_FRACTION
    ):
        raise ImageError(
            f"{label} is effectively blank (entropy={entropy:.3f}, colors={meaningful_colors}, "
            f"dark={dark_fraction:.3f}, light={light_fraction:.3f})"
        )
    width, height = rgb.size
    return {
        "width": width,
        "height": height,
        "file_sha256": hashlib.sha256(payload).hexdigest(),
        "pixel_sha256": pixel_sha256,
        "luma_entropy": round(entropy, 3),
        "meaningful_colors": meaningful_colors,
        "dark_fraction": round(dark_fraction, 4),
        "light_fraction": round(light_fraction, 4),
    }


# -- Shared private helpers (also used by compare, png and webp) -------------------------------------


def _is_dimension(value: object) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int)
            and 1 <= value <= limits.MAX_IMAGE_DIMENSION)


def _within_bounds(width: Any, height: Any) -> bool:
    """Both sides are integers in ``1..MAX_IMAGE_DIMENSION`` and the area is within the pixel bound."""

    if not (_is_dimension(width) and _is_dimension(height)):
        return False
    return width * height <= MAX_IMAGE_PIXELS


def _label(source: object, image_format: str) -> str:
    kind = "PNG image" if image_format == "PNG" else "WebP image" if image_format == "WEBP" else "image"
    return f"{kind} {source}" if isinstance(source, Path) else kind


def _read_source(source: Path | bytes, *, formats: Collection[str], label: str) -> bytes:
    """The bytes of ``source``: a ``bytes`` value, or one stable regular file read exactly once.

    The bound is the largest byte bound of ``formats``; :func:`_admit` applies the bound of the
    format the bytes actually carry. A path is opened as given (see the module docstring on
    containment) and must not contain a ``..`` component."""

    maximum = max(_MAX_BYTES[image_format] for image_format in formats)
    if isinstance(source, bytes):
        if not 0 < len(source) <= maximum:
            raise ImageError(f"{label} must hold 1..{maximum} bytes")
        return source
    if not isinstance(source, Path):
        raise ImageError(f"{label} source must be a path or bytes, not {type(source).__name__}")
    if ".." in source.parts:
        raise ImageError(f"{label} path must not contain a '..' component")
    try:
        before = os.lstat(source)
    except OSError as exc:
        raise ImageError(f"cannot read {label}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ImageError(f"{label} is not a regular file")
    if not 0 < before.st_size <= maximum:
        raise ImageError(f"{label} must hold 1..{maximum} bytes")
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise ImageError(f"cannot open {label}: {exc.strerror or exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino or opened.st_size != before.st_size):
            raise ImageError(f"{label} changed while opening")
        chunks: list[bytes] = []
        remaining = before.st_size + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise ImageError(f"cannot read {label}: {exc.strerror or exc}") from exc
    finally:
        os.close(descriptor)
    payload = b"".join(chunks)
    if (len(payload) != before.st_size or after.st_size != before.st_size
            or after.st_mtime_ns != opened.st_mtime_ns):
        raise ImageError(f"{label} changed while reading")
    return payload


def _admit(payload: bytes, *, formats: Collection[str], label: str) -> str:
    """The format ``payload``'s signature names, which must be one of ``formats`` and whose byte
    bound ``payload`` must respect; checked before any Pillow code parses the bytes."""

    if payload.startswith(_PNG_SIGNATURE):
        image_format = "PNG"
    elif payload[:4] == b"RIFF" and payload[8:12] == b"WEBP" and payload[12:16] in _WEBP_CHUNKS:
        image_format = "WEBP"
    else:
        image_format = ""
    if image_format not in formats:
        wanted = " or ".join("WebP" if name == "WEBP" else name for name in formats)
        raise ImageError(f"{label} is not a {wanted} image")
    if len(payload) > _MAX_BYTES[image_format]:
        raise ImageError(f"{label} exceeds {_MAX_BYTES[image_format]} bytes")
    return image_format


def _decode_rgb(payload: bytes, *, formats: Collection[str], label: str,
                policy: SizePolicy | None = None) -> tuple[str, Any]:
    """Fully decode one static image of ``formats`` and return ``(format, RGB image)``.

    :func:`_admit` identifies the format first and only that Pillow plugin may open ``payload``;
    the frame count and the dimensions (``policy`` when given, otherwise only the pixel and
    dimension bounds) are checked before the pixel data is decoded."""

    image_format = _admit(payload, formats=formats, label=label)

    from PIL import Image, ImageFile, UnidentifiedImageError

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    wanted = "WebP" if image_format == "WEBP" else image_format
    if image_format == "WEBP":
        width, height = _webp_canvas(payload, label)
        if not _within_bounds(width, height):
            raise ImageError(f"{label} dimensions {width}x{height} violate {_describe(policy)}")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(payload), formats=[image_format]) as image:
                if image.format != image_format:
                    raise ImageError(f"{label} is not a {wanted} image")
                if getattr(image, "n_frames", 1) != 1:
                    raise ImageError(f"{label} must be one static frame")
                width, height = image.size
                allowed = policy.allows(width, height) if policy is not None else _within_bounds(width, height)
                if not allowed:
                    raise ImageError(f"{label} dimensions {width}x{height} violate {_describe(policy)}")
                image.load()
                rgb = image.convert("RGB")
    except ImageError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageError(f"{label} dimensions violate {_describe(policy)}: {exc}") from exc
    except UnidentifiedImageError as exc:
        raise ImageError(f"{label} is not a {wanted} image") from exc
    except Exception as exc:  # OSError, SyntaxError, ValueError, struct.error, ... from hostile bytes
        raise ImageError(f"{label} cannot be decoded: {exc}") from exc
    return image_format, rgb


def _webp_canvas(payload: bytes, label: str) -> tuple[int, int]:
    """The canvas size the RIFF/WebP header of an admitted WebP ``payload`` declares, read before
    Pillow sees the bytes.

    Opening a WebP makes Pillow create libwebp's animation decoder, which allocates the whole
    canvas before Pillow's own decompression-bomb check runs, so the kit bounds the declared canvas
    first; a truncated or malformed header is rejected."""

    chunk, header = payload[12:16], payload[20:30]
    if len(header) != 10:
        raise ImageError(f"{label} has a truncated WebP header")
    if chunk == b"VP8X":  # flags and reserved bytes, then 24-bit canvas width - 1 and height - 1
        return 1 + int.from_bytes(header[4:7], "little"), 1 + int.from_bytes(header[7:10], "little")
    if chunk == b"VP8L":  # signature 0x2f, then 14-bit width - 1 and height - 1
        if header[0] != 0x2F:
            raise ImageError(f"{label} has a malformed lossless WebP header")
        bits = int.from_bytes(header[1:5], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    if header[3:6] != b"\x9d\x01\x2a":  # lossy: frame tag, start code, then 14-bit width and height
        raise ImageError(f"{label} has a malformed lossy WebP header")
    return int.from_bytes(header[6:8], "little") & 0x3FFF, int.from_bytes(header[8:10], "little") & 0x3FFF


def _describe(policy: SizePolicy | None) -> str:
    bound = f"{MAX_IMAGE_PIXELS} pixels and {limits.MAX_IMAGE_DIMENSION} per side"
    if policy is None:
        return f"the bound of {bound}"
    size = f"{policy.width}x{policy.height}"
    return f"{'exactly' if policy.mode == 'exact' else 'at least'} {size} within {bound}"
