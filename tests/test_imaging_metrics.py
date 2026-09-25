"""``mod_base.imaging.metrics``: SizePolicy, the 8-key inspection and its hostile-input bounds (MB2).

Parity of the measurements with both mods is ``test_imaging_parity``; this module pins the policy
semantics and the fail-closed input handling the kit adds around them.
"""

from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import struct
import tempfile
import unittest
import warnings
import zlib
from pathlib import Path
from unittest import mock

from mod_base.errors import MbError
from mod_base.imaging import metrics
from mod_base.imaging.metrics import (
    MAX_IMAGE_PIXELS,
    METRIC_KEYS,
    ImageError,
    SizePolicy,
    inspect_png,
    inspect_webp,
)
from mod_base.imaging.png import pattern_png
from mod_base.model import limits
from mod_base.model.documents import PIXEL_METRICS

INPUTS = Path(__file__).resolve().parent / "fixtures" / "imaging" / "inputs"
LENIENT = SizePolicy.minimum(1, 1)


def encode(image, image_format: str = "PNG", **params) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format=image_format, **params)
    return stream.getvalue()


def png_header(width: int, height: int) -> bytes:
    """A PNG whose IHDR declares ``width`` x ``height`` RGB pixels over one tiny IDAT."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
            + chunk(b"IEND", b""))


def vp8x_bomb(width: int, height: int) -> bytes:
    """A small extended WebP whose VP8X chunk declares a ``width`` x ``height`` canvas."""

    from PIL import Image

    source = bytearray(encode(Image.open(io.BytesIO(pattern_png(64, 36))), "WEBP", lossless=True,
                              icc_profile=b"profile"))
    assert source[12:16] == b"VP8X"
    source[24:27] = (width - 1).to_bytes(3, "little")
    source[27:30] = (height - 1).to_bytes(3, "little")
    return bytes(source)


class SizePolicyTest(unittest.TestCase):
    def test_exact_admits_only_its_size(self) -> None:
        policy = SizePolicy.exact(1600, 900)
        self.assertEqual((policy.mode, policy.width, policy.height), ("exact", 1600, 900))
        self.assertTrue(policy.allows(1600, 900))
        for width, height in ((1599, 900), (1601, 900), (1600, 899), (1600, 901), (900, 1600), (3200, 1800)):
            with self.subTest(size=(width, height)):
                self.assertFalse(policy.allows(width, height))

    def test_minimum_admits_at_least_its_size(self) -> None:
        policy = SizePolicy.minimum(640, 360)
        self.assertEqual((policy.mode, policy.width, policy.height), ("minimum", 640, 360))
        for width, height in ((640, 360), (641, 360), (640, 361), (1920, 1080), (16384, 360)):
            with self.subTest(size=(width, height)):
                self.assertTrue(policy.allows(width, height))
        for width, height in ((639, 360), (640, 359), (360, 640), (1, 1)):
            with self.subTest(size=(width, height)):
                self.assertFalse(policy.allows(width, height))

    def test_the_pixel_and_dimension_bounds_apply_to_every_policy(self) -> None:
        policy = SizePolicy.minimum(1, 1)
        self.assertTrue(policy.allows(5000, 4000))
        self.assertFalse(policy.allows(5000, 4001))  # 20,005,000 > 20,000,000 pixels
        self.assertTrue(policy.allows(limits.MAX_IMAGE_DIMENSION, 1))
        self.assertFalse(policy.allows(limits.MAX_IMAGE_DIMENSION + 1, 1))
        for width, height in ((0, 1), (1, 0), (-1, 5), (True, 1), (1.0, 1), ("1", 1), (None, 1)):
            with self.subTest(size=(width, height)):
                self.assertFalse(policy.allows(width, height))

    def test_unsatisfiable_policies_are_refused(self) -> None:
        for arguments in (("exact", 0, 1), ("exact", 1, 0), ("minimum", -1, 1), ("exact", True, 1),
                          ("exact", 1.0, 1), ("exact", 16385, 1), ("minimum", 5000, 5000), ("between", 1, 1)):
            with self.subTest(arguments=arguments), self.assertRaises(ImageError):
                SizePolicy(*arguments)

    def test_policies_are_frozen_values(self) -> None:
        policy = SizePolicy.exact(1920, 1080)
        self.assertEqual(policy, SizePolicy("exact", 1920, 1080))
        self.assertNotEqual(policy, SizePolicy.minimum(1920, 1080))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            policy.width = 1  # type: ignore[misc]


class InspectionTest(unittest.TestCase):
    def test_returns_exactly_the_eight_pixel_metrics(self) -> None:
        from PIL import Image

        payload = pattern_png(1600, 900, 5)
        metrics = inspect_png(payload, SizePolicy.exact(1600, 900))
        self.assertEqual(tuple(metrics), METRIC_KEYS)
        self.assertEqual(PIXEL_METRICS(metrics, "$"), metrics)
        self.assertEqual((metrics["width"], metrics["height"]), (1600, 900))
        self.assertEqual(metrics["file_sha256"], hashlib.sha256(payload).hexdigest())
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(metrics["pixel_sha256"], hashlib.sha256(image.convert("RGB").tobytes()).hexdigest())
        self.assertEqual(metrics["luma_entropy"], round(metrics["luma_entropy"], 3))
        self.assertEqual(metrics["dark_fraction"], round(metrics["dark_fraction"], 4))
        self.assertIsInstance(metrics["meaningful_colors"], int)

    def test_the_size_policy_gates_the_image(self) -> None:
        payload = pattern_png(640, 360)
        self.assertEqual(inspect_png(payload, SizePolicy.minimum(640, 360))["width"], 640)
        self.assertEqual(inspect_png(payload, SizePolicy.exact(640, 360))["height"], 360)
        for policy in (SizePolicy.exact(1600, 900), SizePolicy.exact(641, 360), SizePolicy.minimum(641, 360)):
            with self.subTest(policy=policy), self.assertRaisesRegex(ImageError, "dimensions 640x360 violate"):
                inspect_png(payload, policy)

    def test_a_policy_is_required(self) -> None:
        for policy in (None, ("exact", 640, 360), (640, 360)):
            with self.subTest(policy=policy), self.assertRaises(ImageError):
                inspect_png(pattern_png(640, 360), policy)  # type: ignore[arg-type]

    def test_effectively_blank_images_are_rejected_by_each_threshold(self) -> None:
        from PIL import Image, ImageDraw

        solid = encode(Image.new("RGB", (320, 180), (90, 140, 200)))
        bands = Image.new("RGB", (160, 90), (200, 40, 40))  # three colours: only the colour count fails
        draw = ImageDraw.Draw(bands)
        draw.rectangle([0, 30, 159, 59], fill=(40, 200, 40))
        draw.rectangle([0, 60, 159, 89], fill=(40, 40, 200))
        low = Image.new("RGB", (320, 180))  # twelve near-identical greys: only the deviation fails
        low_draw = ImageDraw.Draw(low)
        for row in range(3):
            for column in range(4):
                low_draw.rectangle([column * 80, row * 60, column * 80 + 79, row * 60 + 59],
                                   fill=(100 + column, 101 + row, 102 + (column + row) % 3))
        cases = {
            "solid": (solid, "entropy=-0.000, colors=1"),
            "three colours": (encode(bands), "entropy=1.585, colors=3, dark=0.000, light=0.000"),
            "low deviation": (encode(low), "colors="),
            "black": (encode(Image.new("RGB", (320, 180))), "dark=1.000"),
            "white": (encode(Image.new("RGB", (320, 180), (255, 255, 255))), "light=1.000"),
        }
        for name, (payload, detail) in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ImageError, "effectively blank") as caught:
                inspect_png(payload, LENIENT)
            self.assertIn(detail, str(caught.exception))

    def test_the_blank_gate_boundaries(self) -> None:
        # The golden boundary inputs (generate_golden.boundary_inputs) sit on each threshold; the
        # expected numbers are literals so a drifted constant, comparison or cut point fails here
        # as well as in test_imaging_parity.
        frame = SizePolicy.exact(1600, 900)
        accepted = {
            "colours-4.png": {"meaningful_colors": 4},  # the 4th colour has exactly 14 samples
            "entropy-above.png": {"luma_entropy": 0.751},
            "stddev-above.png": {"meaningful_colors": 7},
            "dark-limit.png": {"dark_fraction": 0.98},  # exactly 14112 of 14400 samples
            "light-limit.png": {"light_fraction": 0.995},  # exactly 14328 of 14400 samples
            "luma-edges.png": {"dark_fraction": 0.2556, "light_fraction": 0.2333},
        }
        for name, expected in accepted.items():
            with self.subTest(input=name):
                metrics = inspect_png((INPUTS / name).read_bytes(), frame)
                self.assertEqual({key: metrics[key] for key in expected}, expected)
        rejected = {
            "colours-3.png": "entropy=1.102, colors=3, dark=0.000, light=0.000",
            "entropy-below.png": "entropy=0.749, colors=5, dark=0.000, light=0.000",
            "stddev-below.png": "entropy=1.374, colors=7, dark=0.000, light=0.000",
            "dark-over.png": "entropy=3.103, colors=11, dark=0.980, light=0.000",
            "light-over.png": "entropy=3.037, colors=9, dark=0.000, light=0.995",
        }
        for name, detail in rejected.items():
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, "effectively blank") as caught:
                inspect_png((INPUTS / name).read_bytes(), frame)
            self.assertIn(f"({detail})", str(caught.exception))

    def test_the_deviation_pair_straddles_two_on_one_channel(self) -> None:
        from PIL import Image, ImageStat

        deviations = {}
        for name in ("stddev-above.png", "stddev-below.png"):
            with Image.open(INPUTS / name) as image:
                sample = image.convert("RGB").resize((160, 90), Image.Resampling.BILINEAR)
            deviations[name] = ImageStat.Stat(sample).stddev
        self.assertTrue(2.0 < max(deviations["stddev-above.png"]) < 2.02)
        self.assertEqual(min(deviations["stddev-above.png"]), 0.0)  # the gate takes the largest channel
        self.assertTrue(1.98 < max(deviations["stddev-below.png"]) < 2.0)

    def test_every_colour_mode_is_measured_as_rgb(self) -> None:
        from PIL import Image

        with Image.open(io.BytesIO(pattern_png(800, 450, 1))) as opened:
            base = opened.convert("RGB")
        reference = inspect_png(encode(base), LENIENT)
        opaque = inspect_png(encode(base.convert("RGBA")), LENIENT)  # same RGB pixels, other file
        self.assertNotEqual(opaque["file_sha256"], reference["file_sha256"])
        self.assertEqual({**opaque, "file_sha256": None}, {**reference, "file_sha256": None})
        for mode in ("L", "LA", "P", "1"):
            with self.subTest(mode=mode):
                self.assertEqual(tuple(inspect_png(encode(base.convert(mode)), LENIENT)), METRIC_KEYS)

    def test_paths_and_bytes_are_one_measurement(self) -> None:
        payload = pattern_png(800, 450, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.png"
            path.write_bytes(payload)
            self.assertEqual(inspect_png(path, LENIENT), inspect_png(payload, LENIENT))

    def test_only_regular_files_are_read(self) -> None:
        payload = pattern_png(800, 450, 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frame.png").write_bytes(payload)
            (root / "link.png").symlink_to(root / "frame.png")
            (root / "folder.png").mkdir()
            (root / "empty.png").write_bytes(b"")
            os.mkfifo(root / "pipe.png")
            for name, message in (("link.png", "not a regular file"), ("folder.png", "not a regular file"),
                                  ("pipe.png", "not a regular file"), ("empty.png", "bytes"),
                                  ("missing.png", "cannot read")):
                with self.subTest(name=name), self.assertRaisesRegex(ImageError, message):
                    inspect_png(root / name, LENIENT)

    def test_a_path_must_not_climb(self) -> None:
        payload = pattern_png(800, 450, 2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frames").mkdir()
            (root / "frame.png").write_bytes(payload)
            self.assertEqual(inspect_png(root / "frame.png", LENIENT)["width"], 800)
            for path in (root / "frames" / ".." / "frame.png", Path("..") / "frame.png"):
                with self.subTest(path=path), self.assertRaisesRegex(ImageError, "must not contain a '..' component"):
                    inspect_png(path, LENIENT)

    def test_sources_are_bounded_before_decoding(self) -> None:
        with self.assertRaisesRegex(ImageError, "bytes"):
            inspect_png(b"", LENIENT)
        with self.assertRaisesRegex(ImageError, f"1..{limits.MAX_SOURCE_PNG_BYTES} bytes"):
            inspect_png(b"\x89PNG" + bytes(limits.MAX_SOURCE_PNG_BYTES), LENIENT)
        with self.assertRaisesRegex(ImageError, f"1..{limits.MAX_DERIVATIVE_BYTES} bytes"):
            inspect_webp(b"RIFF" + bytes(limits.MAX_DERIVATIVE_BYTES), LENIENT)
        for source in ("frame.png", bytearray(pattern_png(8, 4)), memoryview(pattern_png(8, 4)), None):
            with self.subTest(source=type(source).__name__), self.assertRaisesRegex(ImageError, "path or bytes"):
                inspect_png(source, LENIENT)  # type: ignore[arg-type]

    def test_each_function_decodes_only_its_format(self) -> None:
        png = pattern_png(800, 450)
        webp = (INPUTS / "qs-pattern-s0.webp").read_bytes()
        self.assertEqual(inspect_webp(webp, SizePolicy.exact(1600, 900))["width"], 1600)
        with self.assertRaisesRegex(ImageError, "is not a PNG image"):
            inspect_png(webp, LENIENT)
        with self.assertRaisesRegex(ImageError, "is not a WebP image"):
            inspect_webp(png, LENIENT)
        for name in ("pattern-s0.jpg.png", "scene.gif.png", "text.png"):
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, "is not a PNG image"):
                inspect_png((INPUTS / name).read_bytes(), LENIENT)

    def test_multi_frame_images_are_refused(self) -> None:
        with self.assertRaisesRegex(ImageError, "one static frame"):
            inspect_png((INPUTS / "apng.png").read_bytes(), LENIENT)
        with self.assertRaisesRegex(ImageError, "one static frame"):
            inspect_webp((INPUTS / "animated.webp").read_bytes(), LENIENT)

    def test_corrupt_images_are_image_errors(self) -> None:
        for name in ("truncated.png", "idat-flipped.png"):
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, "cannot be decoded"):
                inspect_png((INPUTS / name).read_bytes(), LENIENT)
        self.assertTrue(issubclass(ImageError, MbError))


class StableReadTest(unittest.TestCase):
    """A path is one unchanged regular file read once; each race below is refused, never measured."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "frame.png"
        self.payload = pattern_png(800, 450, 2)
        self.path.write_bytes(self.payload)

    def racing_read(self, change) -> None:
        """Run ``change`` once, just before the first read of the opened file."""

        real_read = os.read
        pending = [change]

        def read(descriptor: int, size: int) -> bytes:
            while pending:
                pending.pop()()
            return real_read(descriptor, size)

        with mock.patch.object(metrics.os, "read", new=read):
            with self.assertRaisesRegex(ImageError, "changed while reading"):
                inspect_png(self.path, LENIENT)
        self.assertEqual(pending, [])

    def test_a_file_swapped_before_opening_is_refused(self) -> None:
        other = self.path.with_name("other.png")
        other.write_bytes(self.payload)  # same bytes and size, another inode
        real_open = os.open

        def swapping_open(path: object, flags: int, *args: object) -> int:
            os.replace(other, self.path)
            return real_open(path, flags, *args)

        with mock.patch.object(metrics.os, "open", new=swapping_open):
            with self.assertRaisesRegex(ImageError, "changed while opening"):
                inspect_png(self.path, LENIENT)
        self.assertFalse(other.exists())

    def keep_modification_time(self, change) -> None:
        """Apply ``change`` and restore the modification time, as a filesystem whose timestamps are
        too coarse to show the change would, so only the size checks can notice it."""

        before = os.stat(self.path)
        change()
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_a_file_that_grows_while_reading_is_refused(self) -> None:
        def grow() -> None:
            with open(self.path, "ab") as stream:  # a PNG with trailing bytes still decodes
                stream.write(b"appended")

        self.racing_read(lambda: self.keep_modification_time(grow))

    def test_a_file_that_shrinks_while_reading_is_refused(self) -> None:
        self.racing_read(lambda: self.keep_modification_time(
            lambda: os.truncate(self.path, len(self.payload) // 2)))

    def test_a_file_rewritten_in_place_while_reading_is_refused(self) -> None:
        stamp = os.stat(self.path).st_mtime_ns + 5_000_000_000

        def rewrite() -> None:
            changed = bytearray(self.payload)
            changed[len(changed) // 2] ^= 0xFF
            with open(self.path, "r+b") as stream:  # same size: only the modification time tells
                stream.write(changed)
            os.utime(self.path, ns=(stamp, stamp))

        self.racing_read(rewrite)


class DecompressionBombTest(unittest.TestCase):
    def test_png_dimensions_are_bounded_before_decoding(self) -> None:
        for width, height in ((60000, 60000), (5000, 4001), (16385, 4)):
            with self.subTest(size=(width, height)), self.assertRaisesRegex(ImageError, "dimensions"):
                inspect_png(png_header(width, height), LENIENT)

    def test_a_highly_compressed_png_over_the_pixel_bound_is_refused(self) -> None:
        payload = (INPUTS / "bomb-5000x5000.png").read_bytes()  # 25 M pixels in 32 KiB
        self.assertLess(len(payload), 64 * 1024)
        with self.assertRaisesRegex(ImageError, "dimensions"):
            inspect_png(payload, LENIENT)

    def test_a_webp_canvas_is_bounded_before_pillow_opens_it(self) -> None:
        for width, height in ((16384, 16384), (5000, 5000), (16777216, 1)):
            # The declared canvas is refused by the kit's header check, not later by Pillow.
            with self.subTest(size=(width, height)), \
                    self.assertRaisesRegex(ImageError, f"dimensions {width}x{height} violate"):
                inspect_webp(vp8x_bomb(width, height), LENIENT)
        self.assertEqual(inspect_webp(vp8x_bomb(64, 36), LENIENT)["width"], 64)

    def test_malformed_webp_headers_are_refused(self) -> None:
        webp = (INPUTS / "qs-pattern-s0.webp").read_bytes()
        lossless = bytearray(vp8x_bomb(64, 36))
        cases = {
            "truncated": webp[:24],
            "lossy start code": webp[:23] + b"\x00\x00\x00" + webp[26:],
            "lossless signature": b"RIFF" + lossless[4:12] + b"VP8L" + lossless[16:20] + b"\x00" + lossless[21:],
        }
        for name, payload in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(ImageError, "WebP header"):
                inspect_webp(payload, LENIENT)

    def test_the_bound_holds_whatever_the_process_configured(self) -> None:
        from PIL import Image

        original = Image.MAX_IMAGE_PIXELS
        try:
            Image.MAX_IMAGE_PIXELS = None
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                with self.assertRaisesRegex(ImageError, "dimensions"):
                    inspect_png((INPUTS / "bomb-5000x5000.png").read_bytes(), LENIENT)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, MAX_IMAGE_PIXELS)
        finally:
            Image.MAX_IMAGE_PIXELS = original


if __name__ == "__main__":
    unittest.main()
