"""``mod_base.imaging.png``: canonical re-encoding and the conformance pattern generator (MB2)."""

from __future__ import annotations

import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from mod_base.imaging import metrics
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_png
from mod_base.imaging.png import canonical_png, pattern_png
from tests import helpers

INPUTS = Path(__file__).resolve().parent / "fixtures" / "imaging" / "inputs"
LENIENT = SizePolicy.minimum(1, 1)


def chunks(payload: bytes) -> list[bytes]:
    """The chunk types of a PNG, after checking every CRC."""

    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    types, offset = [], 8
    while offset < len(payload):
        length = struct.unpack(">I", payload[offset:offset + 4])[0]
        kind = payload[offset + 4:offset + 8]
        body = payload[offset + 8:offset + 8 + length]
        assert struct.unpack(">I", payload[offset + 8 + length:offset + 12 + length])[0] == zlib.crc32(kind + body)
        types.append(kind)
        offset += 12 + length
    return types


def rgb_pixels(payload: bytes) -> tuple[tuple[int, int], bytes]:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        return image.size, image.convert("RGB").tobytes()


class PatternPngTest(unittest.TestCase):
    def test_byte_identical_to_the_test_helper(self) -> None:
        sizes = [(8, 4), (9, 5), (16, 9), (37, 211), (160, 90), (333, 77), (640, 360)]
        for width, height in sizes:
            for seed in (0, 1, 2, 3, 4, 5, 6, 7, 8, 11, -1, -6):
                with self.subTest(size=(width, height), seed=seed):
                    self.assertEqual(pattern_png(width, height, seed), helpers.pattern_png(width, height, seed=seed))
        for width, height in ((1280, 720), (1600, 900), (1920, 1080)):
            for seed in (0, 3):
                with self.subTest(size=(width, height), seed=seed):
                    self.assertEqual(pattern_png(width, height, seed), helpers.pattern_png(width, height, seed=seed))

    def test_deterministic_rgb_and_seeded(self) -> None:
        first = pattern_png(320, 180, 2)
        self.assertEqual(first, pattern_png(320, 180, 2))
        self.assertEqual(first, pattern_png(320, 180, seed=2))
        self.assertEqual(pattern_png(320, 180), pattern_png(320, 180, 0))
        self.assertEqual(len({pattern_png(320, 180, seed) for seed in range(8)}), 8)
        self.assertEqual(rgb_pixels(first)[0], (320, 180))
        self.assertEqual(first[25], 2)  # IHDR colour type 2: 8-bit RGB

    def test_passes_the_blank_checks_at_every_size(self) -> None:
        for width, height in ((8, 4), (9, 5), (8, 2000), (2000, 4), (16384, 8), (160, 90), (161, 91),
                              (640, 360), (1280, 720), (1600, 900), (1920, 1080)):
            for seed in (0, 1, 2, 3, 5):
                with self.subTest(size=(width, height), seed=seed):
                    metrics = inspect_png(pattern_png(width, height, seed), SizePolicy.exact(width, height))
                    self.assertGreaterEqual(metrics["meaningful_colors"], 4)

    def test_refuses_unusable_sizes_and_seeds(self) -> None:
        for arguments in ((7, 4), (8, 3), (0, 0), (-8, 4), (16385, 4), (5000, 4001), (8.0, 4), (True, 4), (8, 4, 1.5),
                          (8, 4, None), ("8", 4)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                pattern_png(*arguments)  # type: ignore[arg-type]


class CanonicalPngTest(unittest.TestCase):
    def test_only_the_critical_chunks_of_an_8_bit_rgb_png(self) -> None:
        source = (INPUTS / "metadata.png").read_bytes()
        self.assertIn(b"iCCP", chunks(source))
        canonical = canonical_png(source)
        self.assertEqual(chunks(canonical), [b"IHDR", b"IDAT", b"IEND"])
        self.assertEqual(canonical[24:29], bytes([8, 2, 0, 0, 0]))  # depth 8, RGB, deflate, no filter, no interlace
        self.assertEqual(rgb_pixels(canonical), rgb_pixels(source))

    def test_identical_pixels_give_identical_bytes(self) -> None:
        plain = (INPUTS / "pattern-s0.png").read_bytes()
        canonical = canonical_png(plain)
        for name in ("metadata.png", "trailing-data.png", "pattern-s0-rgba.png"):
            with self.subTest(input=name):
                self.assertEqual(canonical_png((INPUTS / name).read_bytes()), canonical)
        self.assertEqual(canonical_png(canonical), canonical)
        self.assertEqual(canonical_png(plain), canonical)

    def test_every_colour_mode_keeps_its_rgb_pixels(self) -> None:
        for name in ("grey-L.png", "grey-LA.png", "palette-P.png", "palette-P-transparency.png", "rgba-alpha.png",
                     "grey16.png", "rgb48.png", "scene-interlaced.png"):
            source = (INPUTS / name).read_bytes()
            with self.subTest(input=name):
                canonical = canonical_png(source)
                self.assertEqual(chunks(canonical), [b"IHDR", b"IDAT", b"IEND"])
                self.assertEqual(rgb_pixels(canonical), rgb_pixels(source))

    def test_the_policy_gates_the_source(self) -> None:
        source = pattern_png(640, 360)
        self.assertEqual(canonical_png(source, policy=SizePolicy.exact(640, 360)), canonical_png(source))
        with self.assertRaisesRegex(ImageError, "dimensions 640x360 violate"):
            canonical_png(source, policy=SizePolicy.exact(1600, 900))
        with self.assertRaisesRegex(ImageError, "SizePolicy"):
            canonical_png(source, policy=(640, 360))  # type: ignore[arg-type]

    def test_only_valid_static_pngs_are_canonicalized(self) -> None:
        for name, message in (("apng.png", "one static frame"), ("truncated.png", "cannot be decoded"),
                              ("idat-flipped.png", "cannot be decoded"), ("pattern-s0.jpg.png", "is not a PNG image"),
                              ("scene.gif.png", "is not a PNG image"), ("qs-pattern-s0.webp", "is not a PNG image"),
                              ("text.png", "is not a PNG image"), ("bomb-5000x5000.png", "dimensions")):
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, message):
                canonical_png((INPUTS / name).read_bytes())

    def test_an_untrusted_encoder_result_is_refused(self) -> None:
        # Each check after encoding, driven by a simulated faulty encoder.
        from PIL import Image, ImageOps

        source = pattern_png(96, 54, 1)
        real = Image.frombytes
        faults = (
            ("grey", "is not an 8-bit RGB PNG", lambda *args: real(*args).convert("L")),
            ("inverted", "changed its pixels", lambda *args: ImageOps.invert(real(*args))),
            ("one column short", "changed its pixels", lambda *args: real(*args).crop((0, 0, 95, 54))),
        )
        for name, message, fault in faults:
            with self.subTest(fault=name), mock.patch.object(Image, "frombytes", new=fault):
                with self.assertRaisesRegex(ImageError, f"canonical PNG image {message}"):
                    canonical_png(source)
        with mock.patch.object(Image.Image, "save", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(ImageError, "cannot re-encode PNG image: disk full"):
                canonical_png(source)

    def test_the_canonical_bytes_obey_the_source_bound(self) -> None:
        source = (INPUTS / "grey-L.png").read_bytes()  # one grey sample per pixel re-encodes larger as RGB
        self.assertGreater(len(canonical_png(source)), len(source))
        with mock.patch.dict(metrics._MAX_BYTES, {"PNG": len(source)}):
            with self.assertRaisesRegex(ImageError, "canonical PNG image exceeds its byte bound"):
                canonical_png(source)

    def test_paths_are_read_like_bytes(self) -> None:
        source = pattern_png(96, 54, 1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "icon.png"
            path.write_bytes(source)
            self.assertEqual(canonical_png(path), canonical_png(source))
            (Path(directory) / "link.png").symlink_to(path)
            with self.assertRaisesRegex(ImageError, "not a regular file"):
                canonical_png(Path(directory) / "link.png")


if __name__ == "__main__":
    unittest.main()
