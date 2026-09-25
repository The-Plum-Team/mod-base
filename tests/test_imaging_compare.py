"""``mod_base.imaging.compare.compare``: the measurement, its echoed arguments and its input checks (MB2).

Parity with both mods' ``compare_screenshots`` is ``test_imaging_parity``; here the arithmetic is
pinned on hand-built images and the kit's stricter argument and format checks are exercised.
"""

from __future__ import annotations

import io
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base.imaging.compare import CHANGED_LUMA_THRESHOLD, compare
from mod_base.imaging.metrics import ImageError
from mod_base.imaging.png import pattern_png
from mod_base.model import limits
from mod_base.model.documents import COMPARE_METRICS

INPUTS = Path(__file__).resolve().parent / "fixtures" / "imaging" / "inputs"


def png(image) -> bytes:
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def shifted_pair() -> tuple[bytes, bytes]:
    """A 10x10 grey image and a copy whose rows 0-1 are 10 brighter and rows 2-4 are 5 brighter."""

    from PIL import Image, ImageDraw

    first = Image.new("RGB", (10, 10), (100, 100, 100))
    second = first.copy()
    draw = ImageDraw.Draw(second)
    draw.rectangle([0, 0, 9, 1], fill=(110, 110, 110))
    draw.rectangle([0, 2, 9, 4], fill=(105, 105, 105))
    return png(first), png(second)


class CompareTest(unittest.TestCase):
    def test_the_measurement_is_the_luma_difference_histogram(self) -> None:
        first, second = shifted_pair()
        result = compare(first, second, minimum_changed_fraction=0.2)
        self.assertEqual(CHANGED_LUMA_THRESHOLD, 8)
        # 20 of 100 pixels differ by 10 >= 8; 30 differ by 5 < 8. RMS = sqrt((20*100 + 30*25) / 100).
        self.assertEqual(result, {"changed_fraction": 0.2, "rms_difference": round(math.sqrt(27.5), 3),
                                  "required_changed_fraction": 0.2})
        self.assertEqual(COMPARE_METRICS(result, "$"), result)

    def test_a_region_crops_both_images_at_truncated_pixel_edges(self) -> None:
        first, second = shifted_pair()
        # int(0.26 * 10) = 2 .. int(0.59 * 10) = 5: columns 2-4 of rows 2-4, the +5 pixels only.
        result = compare(first, second, minimum_changed_fraction=0.0, region=(0.26, 0.26, 0.59, 0.59))
        self.assertEqual(result, {"changed_fraction": 0.0, "rms_difference": 5.0, "required_changed_fraction": 0.0,
                                  "region": [0.26, 0.26, 0.59, 0.59]})
        top = compare(first, second, minimum_changed_fraction=1, region=[0, 0, 1, 0.2])
        self.assertEqual(top, {"changed_fraction": 1.0, "rms_difference": 10.0, "required_changed_fraction": 1,
                               "region": [0, 0, 1, 0.2]})
        self.assertEqual(COMPARE_METRICS(top, "$"), top)

    def test_identical_images_change_nothing(self) -> None:
        payload = pattern_png(320, 180, 3)
        self.assertEqual(compare(payload, payload, minimum_changed_fraction=0.0),
                         {"changed_fraction": 0.0, "rms_difference": 0.0, "required_changed_fraction": 0.0})
        with self.assertRaisesRegex(ImageError, r"did not change enough over the frame .*changed=0\.0000000"):
            compare(payload, payload, minimum_changed_fraction=1e-07)

    def test_an_insufficient_change_in_a_region_is_rejected(self) -> None:
        first, second = shifted_pair()
        with self.assertRaisesRegex(ImageError, r"did not change enough in region \[0\.0, 0\.5, 1\.0, 1\.0\]"):
            compare(first, second, minimum_changed_fraction=0.01, region=(0.0, 0.5, 1.0, 1.0))

    def test_an_empty_pixel_box_is_rejected(self) -> None:
        first, second = shifted_pair()
        with self.assertRaisesRegex(ImageError, "is empty at 10x10"):
            compare(first, second, minimum_changed_fraction=0.0, region=[0.5, 0.0, 0.55, 1.0])

    def test_sizes_and_formats_must_match(self) -> None:
        with self.assertRaisesRegex(ImageError, "changed dimensions unexpectedly"):
            compare(pattern_png(320, 180), pattern_png(320, 181), minimum_changed_fraction=0.0)
        png_frame = (INPUTS / "pattern-s0.png").read_bytes()
        webp_frame = (INPUTS / "qs-pattern-s0.webp").read_bytes()
        with self.assertRaisesRegex(ImageError, "cannot compare a PNG image with a WEBP image"):
            compare(png_frame, webp_frame, minimum_changed_fraction=0.0)
        derivatives = compare(webp_frame, (INPUTS / "qs-pattern-s3.webp").read_bytes(), minimum_changed_fraction=0.5)
        self.assertGreater(derivatives["changed_fraction"], 0.5)

    def test_formats_and_byte_bounds_are_checked_before_either_image_is_parsed(self) -> None:
        from PIL import Image

        webp = (INPUTS / "qs-pattern-s0.webp").read_bytes()
        oversized = webp + bytes(limits.MAX_DERIVATIVE_BYTES)  # a WebP beyond its bound, under a PNG's
        png_frame = (INPUTS / "pattern-s0.png").read_bytes()
        cases = (
            ((oversized, oversized), f"compared image exceeds {limits.MAX_DERIVATIVE_BYTES} bytes"),
            ((webp, oversized), f"compared image exceeds {limits.MAX_DERIVATIVE_BYTES} bytes"),
            ((png_frame, webp), "cannot compare a PNG image with a WEBP image"),
            ((webp, png_frame), "cannot compare a WEBP image with a PNG image"),
            ((png_frame, b"GIF89a" + bytes(64)), "compared image is not a PNG or WebP image"),
        )
        for (first, second), message in cases:
            with self.subTest(message=message), mock.patch.object(Image, "open", wraps=Image.open) as opened:
                with self.assertRaisesRegex(ImageError, message):
                    compare(first, second, minimum_changed_fraction=0.0)
                self.assertEqual(opened.call_count, 0)

    def test_a_pillow_failure_while_comparing_is_an_image_error(self) -> None:
        from PIL import ImageChops

        first, second = shifted_pair()
        with mock.patch.object(ImageChops, "difference", side_effect=ValueError("images do not match")):
            with self.assertRaisesRegex(ImageError, "cannot compare screenshots: images do not match"):
                compare(first, second, minimum_changed_fraction=0.0)

    def test_unusable_images_are_rejected(self) -> None:
        good = (INPUTS / "pattern-s0.png").read_bytes()
        for name, message in (("apng.png", "one static frame"), ("truncated.png", "cannot be decoded"),
                              ("pattern-s0.jpg.png", "is not a PNG or WebP image"),
                              ("bomb-5000x5000.png", "dimensions")):
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, message):
                compare(good, (INPUTS / name).read_bytes(), minimum_changed_fraction=0.0)

    def test_the_arguments_are_validated(self) -> None:
        payload = pattern_png(64, 36)
        for minimum in (-0.1, 1.5, math.nan, math.inf, True, "0.5", None):
            with self.subTest(minimum=minimum), self.assertRaisesRegex(ImageError, "minimum_changed_fraction"):
                compare(payload, payload, minimum_changed_fraction=minimum)  # type: ignore[arg-type]
        for region in ([0, 0, 1], "0011", [0, 0, 1, 1, 1], [0.5, 0, 0.5, 1], [0, 0.6, 1, 0.5], [-0.1, 0, 1, 1],
                       [0, 0, 1.1, 1], [0, 0, math.nan, 1], [False, 0, 1, 1], {0: 0, 1: 0, 2: 1, 3: 1}):
            with self.subTest(region=region), self.assertRaisesRegex(ImageError, "region"):
                compare(payload, payload, minimum_changed_fraction=0.0, region=region)  # type: ignore[arg-type]

    def test_paths_and_bytes_compare_alike(self) -> None:
        first, second = shifted_pair()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.png").write_bytes(first)
            (root / "b.png").write_bytes(second)
            self.assertEqual(compare(root / "a.png", root / "b.png", minimum_changed_fraction=0.1),
                             compare(first, second, minimum_changed_fraction=0.1))


if __name__ == "__main__":
    unittest.main()
