"""Generate the MB2 imaging golden vectors (run once; ``inputs/`` and ``golden.json`` are committed).

    PYTHONPATH=src python3 -P tests/fixtures/imaging/generate_golden.py \\
        --quick-skin <Quick Skin checkout> --block-pops <Block Pops checkout>

The input images are deterministic Pillow constructions written to ``inputs/``. The REAL mod
functions are then run over them in read-only subprocesses (``-B``, ``PYTHONDONTWRITEBYTECODE``,
``PYTHONPATH`` naming only the checkout's import roots, a temporary working directory; nothing is
written inside either checkout):

* Quick Skin: ``e2e/packaged_runtime.py`` ``inspect_screenshot`` and ``compare_screenshots``,
  ``e2e/visual_evidence.py`` ``canonicalize_png_snapshot`` and ``scripts/pages/evidence.py``
  ``_encode_webp`` (box 1600x900, quality 82, method 6);
* Block Pops: ``e2e/packaged_runtime.py`` ``_screenshot_metrics`` and ``compare_screenshots``,
  ``e2e/visual_evidence.py`` ``canonicalize_png`` and ``scripts/pages/evidence.py``
  ``_encode_webp_uncached`` (box 1280x720, quality 82, method 6).

A first oracle pass derives the mods' WebP files, which become WebP inputs; the second pass
records every inspection, comparison, canonicalization and derivative. ``golden.json`` keeps each
outcome verbatim (metrics, output digests, or the exception type and message with the temporary
directory replaced by ``<tmp>`` and object addresses by ``<address>``) plus the provenance of both
checkouts and of this platform; running the generator twice gives identical files.
``tests/test_imaging_parity.py`` checks the kit against it. The same file, run with ``--oracle``,
is the subprocess side and imports nothing from the kit.
"""

from __future__ import annotations

import argparse
import ast
import base64
import bisect
import hashlib
import io
import json
import os
import platform
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
GOLDEN = HERE / "golden.json"
MODS = ("quick-skin", "block-pops")
QS_COMPARE_AT = ("e2e/packaged_runtime.py", "compare_screenshots")
SOURCE_FILES = {
    "quick-skin": ("e2e/packaged_runtime.py", "e2e/visual_evidence.py", "scripts/pages/evidence.py"),
    "block-pops": ("e2e/packaged_runtime.py", "e2e/visual_evidence.py", "scripts/pages/evidence.py"),
}
#: The mod's import roots on the oracle's ``PYTHONPATH`` (Quick Skin's flat modules, Block Pops'
#: package imports); the oracle refuses a module imported from anywhere else.
ORACLE_PATHS = {"quick-skin": ("scripts/pages", "e2e"), "block-pops": (".",)}
FULL = (1600, 900)


# -- Inputs -----------------------------------------------------------------------------------------


def _png(image: Any, **params: Any) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG", **params)
    return stream.getvalue()


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _raw_png(width: int, height: int, bit_depth: int, colour_type: int, rows: list[bytes],
             *, interlace: int = 0) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, bit_depth, colour_type, 0, 0, interlace)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header)
            + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + _chunk(b"IEND", b""))


def _sub_filtered(row: bytes, bpp: int) -> bytes:
    return b"\x01" + bytes((row[i] - (row[i - bpp] if i >= bpp else 0)) & 0xFF for i in range(len(row)))


def _adam7_png(image: Any) -> bytes:
    """An Adam7-interlaced 8-bit RGB PNG of ``image`` (Pillow reads but never writes interlacing)."""

    width, height = image.size
    pixels = image.tobytes()
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
    rows: list[bytes] = []
    for x0, y0, dx, dy in passes:
        for y in range(y0, height, dy):
            row = b"".join(pixels[(y * width + x) * 3:(y * width + x) * 3 + 3] for x in range(x0, width, dx))
            if row:
                rows.append(_sub_filtered(row, 3))
    return _raw_png(width, height, 8, 2, rows, interlace=1)


def _scene(variant: int) -> Any:
    """A screenshot-like frame: sky gradient, textured ground, a player box and a hotbar."""

    from PIL import Image, ImageDraw

    width, height = FULL
    image = Image.new("RGB", FULL)
    draw = ImageDraw.Draw(image)
    horizon = 500
    for y in range(horizon):
        t = y / horizon
        draw.line([(0, y), (width, y)], fill=(int(110 + 80 * t), int(160 + 60 * t), int(230 + 20 * t)))
    rng = random.Random(1)
    tiles = ((96, 150, 60), (84, 138, 52), (120, 86, 52), (104, 74, 44), (128, 128, 128))
    for y in range(horizon, height, 20):
        for x in range(0, width, 20):
            colour = tiles[rng.randrange(2) if y < horizon + 40 else 2 + rng.randrange(3)]
            draw.rectangle([x, y, x + 19, y + 19], fill=colour)
    body = ((60, 60, 200), (200, 40, 40))[variant]
    draw.rectangle([760, 300, 840, 520], fill=body)
    draw.rectangle([770, 240, 830, 300], fill=(222, 184, 150))
    draw.rectangle([520, 830, 1080, 880], fill=(40, 40, 40))
    for slot in range(9):
        draw.rectangle([526 + slot * 62, 836, 574 + slot * 62, 874], outline=(200, 200, 200), width=3)
    for line in range(3):
        for glyph in range(24):
            if rng.random() < 0.8:
                x = 40 + glyph * 14
                draw.rectangle([x, 40 + line * 26, x + 9, 56 + line * 26], fill=(250, 250, 250))
    return image


def _bands(width: int, height: int, colours: list[tuple[int, int, int]], background: tuple[int, int, int],
           coverage: float) -> Any:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    band_height = int(height * coverage / len(colours))
    for index, colour in enumerate(colours):
        top = 60 + index * (band_height + 7)
        draw.rectangle([0, top, width - 1, top + band_height - 1], fill=colour)
    return image


def _equal_luma_colours(count: int) -> list[tuple[int, int, int]]:
    """``count`` saturated colours whose Pillow ``L`` value is identical."""

    from PIL import Image

    found: list[tuple[int, int, int]] = []
    target = 120
    for red in range(0, 256, 17):
        for blue in range(0, 256, 17):
            for green in range(256):
                probe = Image.new("RGB", (1, 1), (red, green, blue)).convert("L").getpixel((0, 0))
                if probe == target:
                    colour = (red, green, blue)
                    if all(sum(abs(a - b) for a, b in zip(colour, other)) > 120 for other in found):
                        found.append(colour)
                    break
            if len(found) == count:
                return found
    raise AssertionError("not enough equal-luma colours")


def _grey_bands(levels: list[int], edges: list[int] | None = None) -> Any:
    """Full-width grey bands of ``levels`` top to bottom: row ``y`` is grey
    ``levels[len(levels) * y // height]``, or ``levels[bisect_right(edges, y)]`` with ``edges``."""

    from PIL import Image

    width, height = FULL

    def level(y: int) -> int:
        return levels[bisect.bisect_right(edges, y) if edges is not None else len(levels) * y // height]

    return Image.frombytes("RGB", FULL, b"".join(bytes((level(y),) * 3) * width for y in range(height)))


def boundary_inputs() -> dict[str, bytes]:
    """Frames on each side of every blank-gate threshold, all 1600x900 so both mods measure them.

    The 160x90 sample is a 10x bilinear reduction: a region edge on a 10-pixel block boundary
    blends into one sample row/column on each side (weights 1/8 and 7/8), an edge in the middle
    of a block into one 50% row, and a sample next to the image border sees only the image. The
    values below (measured by the real mods into ``golden.json``) are:

    * ``colours-4``/``colours-3``: a red/blue split mid-block (red, blue, their 50% blend) plus a
      yellow 8x3-block (80x30) corner patch whose 14 unblended samples are exactly the
      ``max(2, 14400 // 1000)`` a colour needs (4 meaningful colours, accepted), or a 7x3-block
      patch of 12 (3 colours, rejected by the colour count alone);
    * ``entropy-above``/``entropy-below``: a red block over blue, 130 pixels tall and 1520 or 1518
      pixels wide, luma entropy 0.7513 (accepted) or 0.7495 (rejected by entropy alone);
    * ``stddev-above``/``stddev-below``: only red varies, as rows ``120 + 70 * y // 9000`` or
      ``120 + 69 * y // 9000``, so the largest channel deviation is 2.011 (accepted, while green
      and blue deviate by 0) or 1.986 (rejected by the deviation alone);
    * ``dark-limit``/``dark-over``: greys 0..7 in eight bands with a yellow bottom-right patch of
      150x170 or 120x210 pixels, leaving exactly 14112 (0.98, accepted) or 14114 (0.98014,
      rejected) dark samples;
    * ``light-limit``/``light-over``: greys 255..248 with a purple patch of 70x80 or 60x90 pixels,
      exactly 14328 (0.995, accepted) or 14330 (0.99514, rejected) light samples;
    * ``luma-edges``: bands at luma 7, 8, 247 and 248 (dark 0.2556 and light 0.2333, accepted), so
      moving a histogram cut point by one level changes a recorded fraction.
    """

    from PIL import Image, ImageDraw

    red, blue, yellow, purple = (200, 60, 60), (60, 90, 200), (230, 200, 60), (60, 40, 120)
    width, height = FULL
    files: dict[str, bytes] = {}
    for name, patch in (("colours-4.png", (80, 30)), ("colours-3.png", (70, 30))):
        image = Image.new("RGB", FULL, blue)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, width - 1, 454], fill=red)
        draw.rectangle([width - patch[0], height - patch[1], width - 1, height - 1], fill=yellow)
        files[name] = _png(image)
    for name, block_width in (("entropy-above.png", 1520), ("entropy-below.png", 1518)):
        image = Image.new("RGB", FULL, blue)
        draw = ImageDraw.Draw(image)
        draw.rectangle([0, 0, block_width - 1, 129], fill=red)
        draw.rectangle([width - 80, height - 30, width - 1, height - 1], fill=yellow)
        files[name] = _png(image)
    for name, span in (("stddev-above.png", 70), ("stddev-below.png", 69)):
        rows = b"".join(bytes((120 + span * y // 9000, 120, 140)) * width for y in range(height))
        files[name] = _png(Image.frombytes("RGB", FULL, rows))
    for name, levels, colour, patch in (
        ("dark-limit.png", list(range(8)), yellow, (150, 170)),
        ("dark-over.png", list(range(8)), yellow, (120, 210)),
        ("light-limit.png", list(range(255, 247, -1)), purple, (70, 80)),
        ("light-over.png", list(range(255, 247, -1)), purple, (60, 90)),
    ):
        image = _grey_bands(levels)
        ImageDraw.Draw(image).rectangle([width - patch[0], height - patch[1], width - 1, height - 1], fill=colour)
        files[name] = _png(image)
    files["luma-edges.png"] = _png(_grey_bands([7, 8, 247, 248], [230, 455, 690]))
    return files


def build_inputs() -> dict[str, bytes]:
    from PIL import Image, ImageDraw, ImageOps, PngImagePlugin

    from mod_base.imaging.png import pattern_png

    def load(data: bytes) -> Any:
        with Image.open(io.BytesIO(data)) as opened:
            return opened.convert("RGB")

    files: dict[str, bytes] = {}
    s0 = pattern_png(*FULL, 0)
    files["pattern-s0.png"] = s0
    files["pattern-s3.png"] = pattern_png(*FULL, 3)
    edit = load(s0)
    edit.paste(ImageOps.invert(edit.crop((400, 225, 640, 360))), (400, 225))
    files["pattern-edit.png"] = _png(edit, optimize=False, compress_level=6)
    files["pattern-s0-rgba.png"] = _png(load(s0).convert("RGBA"))
    files["pattern-1920x1080.png"] = pattern_png(1920, 1080, 1)
    files["pattern-640x360.png"] = pattern_png(640, 360, 2)
    files["pattern-640x360-s6.png"] = pattern_png(640, 360, 6)
    files["pattern-639x360.png"] = pattern_png(639, 360, 2)
    files["pattern-640x359.png"] = pattern_png(640, 359, 2)
    files["pattern-16x9.png"] = pattern_png(16, 9, 4)
    files["pattern-3200x1800.png"] = pattern_png(3200, 1800, 0)
    scene = _scene(0)
    files["scene.png"] = _png(scene)
    files["scene-red.png"] = _png(_scene(1))
    files["scene-interlaced.png"] = _adam7_png(scene)

    noise = Image.new("RGB", FULL, (40, 40, 40))
    rng = random.Random(7)
    noise.paste(Image.frombytes("RGB", (240, 135), bytes(rng.randrange(256) for _ in range(240 * 135 * 3))),
                (680, 380))
    draw = ImageDraw.Draw(noise)
    for index, colour in enumerate(((200, 60, 60), (60, 200, 60), (60, 60, 200), (220, 220, 60))):
        draw.rectangle([100 + index * 300, 700, 300 + index * 300, 820], fill=colour)
    files["noise.png"] = _png(noise)
    palette = [(214, 61, 61), (54, 170, 92), (66, 92, 206), (228, 196, 70), (150, 78, 196)]
    files["mostly-dark.png"] = _png(_bands(*FULL, palette, (0, 0, 0), 0.16))
    files["mostly-light.png"] = _png(_bands(*FULL, palette, (255, 255, 255), 0.2))
    files["nearly-dark.png"] = _png(_bands(*FULL, palette, (0, 0, 0), 0.03))
    files["nearly-light.png"] = _png(_bands(*FULL, palette, (255, 255, 255), 0.01))
    files["solid-black.png"] = _png(Image.new("RGB", FULL, (0, 0, 0)))
    files["solid-white.png"] = _png(Image.new("RGB", FULL, (255, 255, 255)))
    files["solid-grey.png"] = _png(Image.new("RGB", FULL, (128, 128, 128)))
    equal = _equal_luma_colours(6)
    files["equal-luma.png"] = _png(_bands(*FULL, equal, equal[0], 0.8))
    low = Image.new("RGB", FULL)
    low_draw = ImageDraw.Draw(low)
    for row in range(3):
        for column in range(4):
            shade = (100 + column, 101 + row, 102 + (column + row) % 3)
            low_draw.rectangle([column * 400, row * 300, column * 400 + 399, row * 300 + 299], fill=shade)
    files["low-contrast.png"] = _png(low)
    diagonal = Image.new("RGB", FULL, (40, 90, 200))
    ImageDraw.Draw(diagonal).polygon([(0, 0), (1600, 0), (0, 900)], fill=(230, 200, 60))
    files["two-colour-diagonal.png"] = _png(diagonal)
    three = Image.new("RGB", FULL, (40, 90, 200))
    three_draw = ImageDraw.Draw(three)
    three_draw.polygon([(0, 0), (1100, 0), (0, 700)], fill=(230, 200, 60))
    three_draw.polygon([(1600, 900), (700, 900), (1600, 250)], fill=(200, 40, 40))
    files["three-colour-diagonal.png"] = _png(three)

    base = load(s0)
    files["grey-L.png"] = _png(base.convert("L"))
    alpha = Image.linear_gradient("L").resize(FULL)
    grey_alpha = base.convert("L")
    grey_alpha.putalpha(alpha)
    files["grey-LA.png"] = _png(grey_alpha)
    files["palette-P.png"] = _png(base.quantize(colors=64))
    transparent = base.quantize(colors=64)
    files["palette-P-transparency.png"] = _png(transparent, transparency=3)
    rgba = base.copy()
    rgba.putalpha(alpha)
    files["rgba-alpha.png"] = _png(rgba)
    # 16-bit samples: the high byte is the scene's 8-bit value, the low byte a ramp Pillow drops.
    grey = scene.convert("L").tobytes()
    grey16_rows = [_sub_filtered(b"".join(bytes((value, (value + x) & 0xFF))
                                          for x, value in enumerate(grey[y * FULL[0]:(y + 1) * FULL[0]])), 2)
                   for y in range(FULL[1])]
    files["grey16.png"] = _raw_png(*FULL, 16, 0, grey16_rows)
    rgb = scene.tobytes()
    rgb48_rows = [_sub_filtered(b"".join(bytes((value, (value + x) & 0xFF))
                                         for x, value in enumerate(rgb[y * FULL[0] * 3:(y + 1) * FULL[0] * 3])), 6)
                  for y in range(FULL[1])]
    files["rgb48.png"] = _raw_png(*FULL, 16, 2, rgb48_rows)
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "mod-base golden")
    info.add_text("Comment", "compressed text chunk", zip=True)
    info.add_itxt("Description", "international text", lang="en", tkey="Description")
    files["metadata.png"] = _png(base, pnginfo=info, dpi=(144, 144), icc_profile=b"not-a-real-profile")
    apng = io.BytesIO()
    base.save(apng, format="PNG", save_all=True, append_images=[load(files["pattern-s3.png"])], duration=100)
    files["apng.png"] = apng.getvalue()
    files["truncated.png"] = s0[: len(s0) * 6 // 10]
    idat = s0.index(b"IDAT")
    length = struct.unpack(">I", s0[idat - 4:idat])[0]
    crc = idat + 4 + length
    files["idat-bad-crc.png"] = s0[:crc] + bytes([s0[crc] ^ 0xFF]) + s0[crc + 1:]
    flipped = bytearray(s0[idat + 4:idat + 4 + length])
    flipped[length // 2] ^= 0x5A
    files["idat-flipped.png"] = s0[:idat - 4] + _chunk(b"IDAT", bytes(flipped)) + s0[crc + 4:]
    files["trailing-data.png"] = s0 + b"trailing bytes after IEND"
    jpeg = io.BytesIO()
    base.save(jpeg, format="JPEG", quality=90)
    files["pattern-s0.jpg.png"] = jpeg.getvalue()
    gif = io.BytesIO()
    scene.quantize(colors=64).save(gif, format="GIF")
    files["scene.gif.png"] = gif.getvalue()
    files["text.png"] = b"this is not an image\n"
    files["bomb-5000x5000.png"] = _png(Image.new("L", (5000, 5000), 90), optimize=False, compress_level=9)
    files["bomb-header.png"] = _raw_png(60000, 60000, 8, 2, [b"\x00" + bytes(180000)])
    animated = io.BytesIO()
    base.save(animated, format="WEBP", save_all=True, append_images=[load(files["pattern-s3.png"])],
              quality=60, method=4, duration=100)
    files["animated.webp"] = animated.getvalue()
    files.update(boundary_inputs())
    return files


# -- Plans ------------------------------------------------------------------------------------------

WEBP_SOURCES = {
    "quick-skin": ("pattern-s0.png", "pattern-s3.png", "pattern-edit.png", "pattern-1920x1080.png"),
    "block-pops": ("pattern-s0.png", "pattern-s3.png", "scene.png"),
}
WEBP_NAMES = {"quick-skin": "qs-{stem}.webp", "block-pops": "bp-{stem}.webp"}


def plan(names: list[str]) -> dict[str, list[dict[str, Any]]]:
    pngs = sorted(name for name in names if name.endswith(".png"))
    webps = sorted(name for name in names if name.endswith(".webp"))
    inspect = [{"input": name, "format": "PNG"} for name in pngs]
    inspect += [{"input": name, "format": "WEBP"} for name in webps]
    inspect += [{"input": "pattern-s0.png", "format": "WEBP"}, {"input": "qs-pattern-s0.webp", "format": "PNG"},
                {"input": "text.png", "format": "WEBP"}]
    compare = [
        ("pattern-s0.png", "pattern-s3.png", 0.5, None),
        ("pattern-s0.png", "pattern-s3.png", 0.9999, None),
        ("pattern-s0.png", "pattern-s0.png", 0.0, None),
        ("pattern-s0.png", "pattern-s0.png", 1e-07, None),
        ("pattern-s0.png", "pattern-edit.png", 0.01, None),
        ("pattern-s0.png", "pattern-edit.png", 0.5, [0.25, 0.25, 0.4, 0.4]),
        ("pattern-s0.png", "pattern-edit.png", 0.5, [0.6, 0.6, 0.9, 0.9]),
        ("pattern-s0.png", "pattern-edit.png", 0.0, [0.1, 0.2, 0.3, 0.7]),
        ("pattern-s0.png", "pattern-edit.png", 0.0, [0.5, 0.5, 0.5004, 0.6]),
        ("pattern-s0.png", "pattern-edit.png", 0, [0, 0, 1, 1]),
        ("pattern-s0.png", "pattern-edit.png", 0.002, [0.0, 0.0, 1.0, 1.0]),
        ("pattern-s0.png", "pattern-1920x1080.png", 0.0, None),
        ("pattern-s0.png", "pattern-s0-rgba.png", 0.0, None),
        ("pattern-s0.png", "grey-L.png", 0.1, None),
        ("pattern-s0.png", "palette-P.png", 0.0, [0.125, 0.125, 0.875, 0.875]),
        ("pattern-s0.png", "truncated.png", 0.0, None),
        ("scene.png", "scene-red.png", 0.001, None),
        ("scene.png", "scene-red.png", 0.5, [0.45, 0.25, 0.55, 0.6]),
        ("scene.png", "scene-interlaced.png", 0.0, None),
        ("pattern-640x360.png", "pattern-640x360-s6.png", 0.3, None),
        ("pattern-640x360.png", "pattern-640x360-s6.png", 0.3, [0.333, 0.1, 0.667, 0.9]),
        ("qs-pattern-s0.webp", "qs-pattern-s3.webp", 0.5, None),
        ("qs-pattern-s0.webp", "qs-pattern-edit.webp", 0.001, [0.25, 0.25, 0.4, 0.4]),
        ("qs-pattern-s0.webp", "qs-pattern-1920x1080.webp", 0.0, None),
        ("bp-pattern-s0.webp", "bp-pattern-s3.webp", 0.5, [0.5, 0.0, 1.0, 0.5]),
        ("pattern-s0.png", "qs-pattern-s0.webp", 0.0, None),
    ]
    return {
        "inspect": inspect,
        "compare": [{"first": first, "second": second, "minimum_changed_fraction": minimum, "region": region}
                    for first, second, minimum, region in compare],
        "canonical": [{"input": name} for name in pngs],
        "webp": [{"input": name} for name in pngs],
    }


# -- Oracle (subprocess side; imports only the mod) -------------------------------------------------


def _oracle(mod: str, root: Path, inputs: Path, cases: dict[str, Any]) -> dict[str, Any]:
    import warnings

    warnings.simplefilter("ignore")
    scratch = Path(tempfile.mkdtemp(prefix="mb2-oracle-"))
    try:
        work = scratch / "inputs"
        shutil.copytree(inputs, work)
        if mod == "quick-skin":
            import evidence as pages  # type: ignore[import-not-found]
            import packaged_runtime as runtime  # type: ignore[import-not-found]
            import visual_evidence  # type: ignore[import-not-found]

            def inspect(path: Path, image_format: str) -> Any:
                return runtime.inspect_screenshot(path, expected_format=image_format)

            def canonical(path: Path) -> Any:
                size, source, pixel, canonical_sha, data = visual_evidence.canonicalize_png_snapshot(path)
                return {"size": list(size), "file_sha256": source, "pixel_sha256": pixel, "sha256": canonical_sha,
                        "bytes": len(data)}

            def derive(path: Path) -> bytes:
                destination = scratch / "derived.webp"
                pages._encode_webp(path, destination)
                data = destination.read_bytes()
                destination.unlink()
                return data
        else:
            from e2e import packaged_runtime as runtime  # type: ignore[import-not-found,no-redef]
            from e2e import visual_evidence  # type: ignore[no-redef]
            from scripts.pages import evidence as pages  # type: ignore[import-not-found,no-redef]

            def inspect(path: Path, image_format: str) -> Any:
                from PIL import Image

                Image.MAX_IMAGE_PIXELS = 20_000_000  # set by its caller, _inspect_screenshot
                return runtime._screenshot_metrics(path.read_bytes(), image_format, path)

            def canonical(path: Path) -> Any:
                data = path.read_bytes()
                size = struct.unpack(">II", data[16:24]) if data[12:16] == b"IHDR" else FULL
                width, height, source, pixel, canonical_sha, encoded, _ = visual_evidence.canonicalize_png(
                    path, expected_size=tuple(size))
                return {"size": [width, height], "file_sha256": source, "pixel_sha256": pixel,
                        "sha256": canonical_sha, "bytes": len(encoded), "expected_size": list(size)}

            def derive(path: Path) -> bytes:
                return pages._encode_webp_uncached(path.read_bytes())

        for module in (runtime, visual_evidence, pages):
            if not Path(module.__file__).resolve().is_relative_to(root):
                raise SystemExit(f"{module.__name__} was not imported from {root}: {module.__file__}")

        def outcome(function: Any) -> dict[str, Any]:
            try:
                return {"result": function()}
            except Exception as exc:  # the golden records every rejection verbatim
                message = str(exc).replace(str(work), "<tmp>").replace(str(scratch), "<tmp>")
                message = re.sub(r" at 0x[0-9a-fA-F]+>", " at <address>>", message)
                return {"error": type(exc).__name__, "message": message}

        results: dict[str, list[dict[str, Any]]] = {"inspect": [], "compare": [], "canonical": [], "webp": []}
        for case in cases["inspect"]:
            results["inspect"].append(outcome(lambda: inspect(work / case["input"], case["format"])))
        for case in cases["compare"]:
            region = tuple(case["region"]) if case["region"] is not None else None
            results["compare"].append(outcome(lambda: runtime.compare_screenshots(
                work / case["first"], work / case["second"], case["minimum_changed_fraction"], region)))
        for case in cases["canonical"]:
            results["canonical"].append(outcome(lambda: canonical(work / case["input"])))
        for case in cases["webp"]:
            def encode() -> dict[str, Any]:
                from PIL import Image

                data = derive(work / case["input"])
                with Image.open(io.BytesIO(data)) as image:
                    size = list(image.size)
                return {"sha256": hashlib.sha256(data).hexdigest(), "size": size, "bytes": len(data),
                        "data": base64.b64encode(data).decode("ascii")}
            results["webp"].append(outcome(encode))
        return results
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_oracle(mod: str, root: Path, cases: dict[str, Any]) -> dict[str, Any]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": os.pathsep.join(str(root / entry) for entry in ORACLE_PATHS[mod]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "LC_ALL": "C.UTF-8",
        "HOME": tempfile.gettempdir(),
    }
    with tempfile.TemporaryDirectory(prefix="mb2-golden-") as cwd:
        completed = subprocess.run(
            [sys.executable, "-B", "-P", str(Path(__file__).resolve()), "--oracle", mod, "--root", str(root)],
            input=json.dumps(cases).encode("utf-8"), capture_output=True, cwd=cwd, env=environment,
            check=False, timeout=1800,
        )
    if completed.returncode != 0:
        raise SystemExit(f"{mod} oracle failed:\n{completed.stderr.decode('utf-8', 'replace')}")
    return json.loads(completed.stdout)


# -- Provenance -------------------------------------------------------------------------------------


def _function_ast_sha256(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(nodes) != 1:
        raise SystemExit(f"{path} does not define exactly one {name}")
    return hashlib.sha256(ast.dump(nodes[0], annotate_fields=True).encode("utf-8")).hexdigest()


def provenance(roots: dict[str, Path]) -> dict[str, Any]:
    from PIL import __version__ as pillow, features

    sources: dict[str, Any] = {}
    for mod, root in roots.items():
        commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                check=True).stdout.strip()
        sources[mod] = {
            "commit": commit,
            "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES[mod]},
            "compare_screenshots_ast_sha256": _function_ast_sha256(root / QS_COMPARE_AT[0], QS_COMPARE_AT[1]),
        }
    return {
        "platform": {"system": platform.system(), "machine": platform.machine(),
                     "python": platform.python_version(), "pillow": pillow,
                     "libwebp": features.version("webp"), "zlib": features.version("zlib")},
        "sources": sources,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--quick-skin", type=Path)
    parser.add_argument("--block-pops", type=Path)
    parser.add_argument("--oracle", choices=MODS)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args(argv)
    if args.oracle:
        cases = json.loads(sys.stdin.buffer.read())
        json.dump(_oracle(args.oracle, args.root.resolve(), INPUTS, cases), sys.stdout)
        return 0
    if args.quick_skin is None or args.block_pops is None:
        parser.error("--quick-skin and --block-pops are required")
    roots = {"quick-skin": args.quick_skin.resolve(), "block-pops": args.block_pops.resolve()}
    files = build_inputs()
    if INPUTS.exists():
        shutil.rmtree(INPUTS)
    INPUTS.mkdir()
    for name, data in files.items():
        (INPUTS / name).write_bytes(data)
    for mod, sources in WEBP_SOURCES.items():
        derived = run_oracle(mod, roots[mod], {"inspect": [], "compare": [], "canonical": [],
                                              "webp": [{"input": name} for name in sources]})
        for name, result in zip(sources, derived["webp"]):
            data = base64.b64decode(result["result"]["data"])
            (INPUTS / WEBP_NAMES[mod].format(stem=name[:-4])).write_bytes(data)
    names = sorted(path.name for path in INPUTS.iterdir())
    cases = plan(names)
    outcomes = {mod: run_oracle(mod, root, cases) for mod, root in roots.items()}
    for mod_outcomes in outcomes.values():
        for result in mod_outcomes["webp"]:
            if "result" in result:
                del result["result"]["data"]
    golden = {
        "provenance": provenance(roots),
        "inputs": {name: {"sha256": hashlib.sha256((INPUTS / name).read_bytes()).hexdigest(),
                          "bytes": (INPUTS / name).stat().st_size} for name in names},
        "cases": cases,
        "outcomes": outcomes,
    }
    GOLDEN.write_text(json.dumps(golden, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
