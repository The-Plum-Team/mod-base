"""The process-local memo of successful pixel checks (``imaging.metrics._MEMO``, MB2).

A remembered inspection or comparison must equal a fresh measurement of the same bytes with the
same arguments, must be found only under the SHA-256 of the exact bytes the kit has just read, and
must never answer for other bytes, another policy or other arguments. Rejections are never
remembered, callers get copies, and the memo is bounded.
"""

from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base.imaging import compare as compare_module
from mod_base.imaging import metrics
from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_png, inspect_webp
from mod_base.imaging.png import pattern_png
from mod_base.imaging.webp import derive_webp
from mod_base.model import limits

EXACT = SizePolicy.exact(320, 180)


def reencoded(payload: bytes, **params) -> bytes:
    """The same pixels saved again as PNG with other encoder settings (other bytes)."""

    from PIL import Image

    stream = io.BytesIO()
    with Image.open(io.BytesIO(payload)) as image:
        image.save(stream, format="PNG", **params)
    return stream.getvalue()


def blank_png(width: int, height: int) -> bytes:
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (width, height), (40, 40, 40)).save(stream, format="PNG")
    return stream.getvalue()


class MemoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        metrics._MEMO.clear()
        self.addCleanup(metrics._MEMO.clear)

    def decodes(self, module):
        """Count the full decodes ``module`` performs (the work a memo hit skips)."""

        return mock.patch.object(module, "_decode_rgb", wraps=module._decode_rgb)


class InspectionMemoTest(MemoTestCase):
    def test_a_repeat_returns_the_fresh_result_without_decoding_again(self) -> None:
        payload = pattern_png(320, 180, 3)
        with self.decodes(metrics) as decoded:
            first = inspect_png(payload, EXACT)
            second = inspect_png(payload, EXACT)
        self.assertEqual(decoded.call_count, 1)
        self.assertEqual(second, first)
        self.assertEqual(json.dumps(second), json.dumps(first))
        metrics._MEMO.clear()
        self.assertEqual(json.dumps(inspect_png(payload, EXACT)), json.dumps(first))

    def test_a_path_and_its_bytes_share_one_entry(self) -> None:
        payload = pattern_png(320, 180, 4)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.png"
            path.write_bytes(payload)
            with self.decodes(metrics) as decoded:
                self.assertEqual(inspect_png(path, EXACT), inspect_png(payload, EXACT))
            self.assertEqual(decoded.call_count, 1)

    def test_callers_receive_independent_copies(self) -> None:
        payload = pattern_png(320, 180, 5)
        first = inspect_png(payload, EXACT)
        expected = dict(first)
        first["width"] = 1
        first["file_sha256"] = "0" * 64
        self.assertEqual(inspect_png(payload, EXACT), expected)

    def test_other_bytes_miss_even_with_the_same_pixels(self) -> None:
        payload = pattern_png(320, 180, 6)
        other = reencoded(payload, compress_level=1)
        trailing = payload + b"\x00"  # one more byte after IEND, which Pillow ignores
        self.assertNotEqual(other, payload)
        with self.decodes(metrics) as decoded:
            results = [inspect_png(source, EXACT) for source in (payload, other, trailing)]
        self.assertEqual(decoded.call_count, 3)
        self.assertEqual([result["file_sha256"] for result in results],
                         [hashlib.sha256(source).hexdigest() for source in (payload, other, trailing)])
        self.assertEqual(len({result["pixel_sha256"] for result in results}), 1)

    def test_a_file_rewritten_after_a_success_is_measured_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.png"
            path.write_bytes(pattern_png(320, 180, 7))
            inspect_png(path, EXACT)
            path.write_bytes(blank_png(320, 180))
            with self.assertRaisesRegex(ImageError, "effectively blank"):
                inspect_png(path, EXACT)
            good = pattern_png(320, 180, 7)
            path.write_bytes(good[:len(good) // 2])  # cut inside the image data
            with self.assertRaisesRegex(ImageError, "frame.png cannot be decoded"):
                inspect_png(path, EXACT)

    def test_the_policy_is_part_of_the_key(self) -> None:
        payload = pattern_png(320, 180, 1)
        with self.decodes(metrics) as decoded:
            inspect_png(payload, EXACT)
            inspect_png(payload, SizePolicy.minimum(320, 180))
            inspect_png(payload, SizePolicy.minimum(1, 1))
        self.assertEqual(decoded.call_count, 3)
        for policy in (SizePolicy.exact(321, 180), SizePolicy.minimum(320, 181)):
            with self.subTest(policy=policy), self.assertRaisesRegex(ImageError, "dimensions 320x180 violate"):
                inspect_png(payload, policy)

    def test_the_format_is_part_of_the_key(self) -> None:
        derivative = derive_webp(pattern_png(320, 180, 2), box=(160, 90), quality=80, method=4)
        policy = SizePolicy.exact(160, 90)
        self.assertEqual(inspect_webp(derivative, policy)["width"], 160)
        with self.assertRaisesRegex(ImageError, "is not a PNG image"):
            inspect_png(derivative, policy)

    def test_a_rejection_is_never_remembered(self) -> None:
        payload = blank_png(320, 180)
        with self.decodes(metrics) as decoded:
            for _ in range(2):
                with self.assertRaisesRegex(ImageError, "effectively blank"):
                    inspect_png(payload, EXACT)
        self.assertEqual(decoded.call_count, 2)
        self.assertEqual(len(metrics._MEMO), 0)

    def test_the_admission_checks_still_run_on_a_remembered_payload(self) -> None:
        payload = pattern_png(320, 180, 0)
        inspect_png(payload, EXACT)
        with mock.patch.dict(metrics._MAX_BYTES, {"PNG": len(payload) - 1}):
            with self.assertRaisesRegex(ImageError, f"must hold 1..{len(payload) - 1} bytes"):
                inspect_png(payload, EXACT)


class ComparisonMemoTest(MemoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first, self.second = pattern_png(320, 180, 0), pattern_png(320, 180, 1)

    def test_a_repeat_returns_the_fresh_result_without_decoding_again(self) -> None:
        with self.decodes(compare_module) as decoded:
            first = compare(self.first, self.second, minimum_changed_fraction=0.1, region=[0, 0, 0.5, 0.5])
            second = compare(self.first, self.second, minimum_changed_fraction=0.1, region=(0, 0, 0.5, 0.5))
        self.assertEqual(decoded.call_count, 2)
        self.assertEqual(json.dumps(second), json.dumps(first))
        second["region"].append(1)
        self.assertEqual(compare(self.first, self.second, minimum_changed_fraction=0.1, region=[0, 0, 0.5, 0.5]),
                         first)
        metrics._MEMO.clear()
        fresh = compare(self.first, self.second, minimum_changed_fraction=0.1, region=[0, 0, 0.5, 0.5])
        self.assertEqual(json.dumps(fresh), json.dumps(first))

    def test_every_argument_and_its_exact_type_is_part_of_the_key(self) -> None:
        calls = (
            ((self.first, self.second), {"minimum_changed_fraction": 0}),
            ((self.first, self.second), {"minimum_changed_fraction": 0.0}),
            ((self.first, self.second), {"minimum_changed_fraction": -0.0}),
            ((self.first, self.second), {"minimum_changed_fraction": 0.1}),
            ((self.second, self.first), {"minimum_changed_fraction": 0.1}),
            ((self.first, self.second), {"minimum_changed_fraction": 0.1, "region": [0, 0, 1, 1]}),
            ((self.first, self.second), {"minimum_changed_fraction": 0.1, "region": [0.0, 0, 1, 1]}),
            ((self.first, self.second), {"minimum_changed_fraction": 0.1, "region": [0, 0, 0.5, 1]}),
            ((self.first, reencoded(self.second, compress_level=1)), {"minimum_changed_fraction": 0.1}),
        )
        expected = []
        for arguments, keywords in calls:
            metrics._MEMO.clear()
            expected.append(json.dumps(compare(*arguments, **keywords)))
        metrics._MEMO.clear()
        with self.decodes(compare_module) as decoded:
            observed = [json.dumps(compare(*arguments, **keywords)) for arguments, keywords in calls]
        self.assertEqual(decoded.call_count, 2 * len(calls))
        self.assertEqual(observed, expected)
        self.assertEqual([json.loads(text)["required_changed_fraction"] for text in observed[:3]], [0, 0, 0])
        self.assertEqual([text.count('"required_changed_fraction": 0}') for text in observed[:3]], [1, 0, 0])
        self.assertEqual(observed[2].count('"required_changed_fraction": -0.0}'), 1)

    def test_arguments_of_other_types_are_measured_every_time(self) -> None:
        class Fraction(float):
            pass

        with self.decodes(compare_module) as decoded:
            for _ in range(2):
                compare(self.first, self.second, minimum_changed_fraction=Fraction(0.1))
                compare(self.first, self.second, minimum_changed_fraction=0.1, region=[Fraction(0), 0, 1, 1])
        self.assertEqual(decoded.call_count, 8)

    def test_a_rejected_comparison_is_never_remembered(self) -> None:
        with self.decodes(compare_module) as decoded:
            for _ in range(2):
                with self.assertRaisesRegex(ImageError, "did not change enough"):
                    compare(self.first, self.first, minimum_changed_fraction=0.5)
        self.assertEqual(decoded.call_count, 4)
        self.assertEqual(len(metrics._MEMO), 0)


class BoundTest(unittest.TestCase):
    def test_the_least_recently_used_entry_goes_first(self) -> None:
        memo = metrics._Memo(2)
        memo.put("a", {"value": 1})
        memo.put("b", {"value": 2})
        self.assertEqual(memo.get("a"), {"value": 1})
        memo.put("c", {"value": 3})
        self.assertEqual((memo.get("a"), memo.get("b"), memo.get("c")), ({"value": 1}, None, {"value": 3}))
        self.assertEqual(len(memo), 2)

    def test_the_process_memo_uses_the_kit_bound(self) -> None:
        self.assertEqual(metrics._MEMO._limit, limits.MAX_PIXEL_MEMO_ENTRIES)
        self.assertEqual(limits.MAX_PIXEL_MEMO_ENTRIES, 8192)


if __name__ == "__main__":
    unittest.main()
