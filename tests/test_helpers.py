"""The PNG pattern generator must pass the 8-metric blank checks (QS inspect_screenshot == BP
_screenshot_metrics) at every size the kit uses; the oracle below restates those checks."""

from __future__ import annotations

import io
import unittest

from tests.helpers import apply_mutation, expand_value, pattern_png


def blank_check_metrics(data: bytes) -> dict:
    from PIL import Image, ImageStat

    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        rgb = image.convert("RGB")
    sample = rgb.resize((160, 90), Image.Resampling.BILINEAR)
    luma = sample.convert("L")
    pixels = sample.width * sample.height
    palette = sample.quantize(colors=32).getcolors() or []
    histogram = luma.histogram()
    return {
        "entropy": float(luma.entropy()),
        "stddev": max(float(value) for value in ImageStat.Stat(sample).stddev),
        "colors": sum(count >= max(2, pixels // 1000) for count, _ in palette),
        "dark": sum(histogram[:8]) / pixels,
        "light": sum(histogram[248:]) / pixels,
        "size": rgb.size,
    }


class PatternPngTest(unittest.TestCase):
    SIZES = [(1920, 1080), (1600, 900), (1280, 720), (640, 360), (333, 777), (64, 36), (8, 4)]

    def test_passes_the_blank_checks(self) -> None:
        for width, height in self.SIZES:
            for seed in (0, 1, 5):
                metrics = blank_check_metrics(pattern_png(width, height, seed=seed))
                with self.subTest(size=(width, height), seed=seed):
                    self.assertEqual(metrics["size"], (width, height))
                    self.assertGreaterEqual(metrics["entropy"], 0.75)
                    self.assertGreaterEqual(metrics["stddev"], 2.0)
                    self.assertGreaterEqual(metrics["colors"], 4)
                    self.assertLessEqual(metrics["dark"], 0.98)
                    self.assertLessEqual(metrics["light"], 0.995)

    def test_is_deterministic_and_seeded(self) -> None:
        self.assertEqual(pattern_png(320, 180, seed=2), pattern_png(320, 180, seed=2))
        self.assertNotEqual(pattern_png(320, 180, seed=2), pattern_png(320, 180, seed=3))

    def test_seeds_differ_by_a_material_pixel_fraction(self) -> None:
        from PIL import Image, ImageChops

        first = Image.open(io.BytesIO(pattern_png(320, 180, seed=0))).convert("RGB")
        second = Image.open(io.BytesIO(pattern_png(320, 180, seed=1))).convert("RGB")
        histogram = ImageChops.difference(first, second).convert("L").histogram()
        self.assertGreater(sum(histogram[8:]) / (320 * 180), 0.5)

    def test_rejects_tiny_sizes(self) -> None:
        with self.assertRaises(ValueError):
            pattern_png(7, 4)


class MutationHelperTest(unittest.TestCase):
    def test_set_delete_append_and_repeat(self) -> None:
        document = {"a": [1, 2], "b": {"c": 1}, "d/e": 0}
        result = apply_mutation(document, {"set": [["/a/2", 3], ["/b/c", {"$repeat": ["x", 3]}], ["/d~1e", 1]],
                                           "delete": ["/a/0"]})
        self.assertEqual(result, {"a": [2, 3], "b": {"c": "xxx"}, "d/e": 1})
        self.assertEqual(document["a"], [1, 2])
        self.assertEqual(expand_value({"$repeat": ["ab", 2]}), "abab")


if __name__ == "__main__":
    unittest.main()
