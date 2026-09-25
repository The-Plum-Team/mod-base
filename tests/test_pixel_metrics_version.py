"""``pixel_metrics_version`` (SPEC §1.8, §3.0): the metric algorithm is versioned and pinned.

Every consumer requires ``pixel_metrics_version`` to equal the executing kit's
:data:`mod_base.PIXEL_METRICS_VERSION`, so a changed metric algorithm fails closed until every
generation is regenerated. This test pins what version 1 means: the 8-key metrics and the
comparison metrics of fixed synthetic images (decoded-pixel facts only; the encoded PNG bytes may
differ between zlib builds), the version every document and the config accept, and the changelog
entry of the current release. Changing the algorithm without bumping the version, or bumping it
without a changelog entry, fails here.
"""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path
from typing import Any

import mod_base
from mod_base.config import validate_config
from mod_base.errors import MbError
from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import METRIC_KEYS, SizePolicy, inspect_png
from mod_base.imaging.png import pattern_png
from mod_base.model import documents

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "documents"
#: Version 1 of the metrics of ``pattern_png(160, 90, 3)`` (every key but the encoded file hash).
PATTERN_METRICS_V1 = {"width": 160, "height": 90,
                      "pixel_sha256": "80fa107702a1564d32ce8f73f04949af69bf0ccb04e126efd831e24f89c1848b",
                      "luma_entropy": 6.468, "meaningful_colors": 26, "dark_fraction": 0.0, "light_fraction": 0.0}
#: Version 1 of ``compare(pattern_png(160, 90, 3), pattern_png(160, 90, 4))``.
PATTERN_COMPARISON_V1 = {"changed_fraction": 1.0, "rms_difference": 72.844, "required_changed_fraction": 0.0}


def load(relative: str) -> dict[str, Any]:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


class PixelMetricsVersionTest(unittest.TestCase):
    def test_the_current_version_is_one(self) -> None:
        self.assertEqual(mod_base.PIXEL_METRICS_VERSION, 1)
        self.assertEqual(len(METRIC_KEYS), 8)

    def test_version_one_metrics_of_a_fixed_image_are_pinned(self) -> None:
        first, second = pattern_png(160, 90, 3), pattern_png(160, 90, 4)
        metrics = inspect_png(first, SizePolicy.exact(160, 90))
        self.assertEqual(set(metrics), set(METRIC_KEYS))
        self.assertEqual({key: value for key, value in metrics.items() if key != "file_sha256"}, PATTERN_METRICS_V1)
        self.assertEqual(compare(first, second, minimum_changed_fraction=0.0), PATTERN_COMPARISON_V1)

    def test_the_changelog_records_the_version_of_the_current_release(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        start = text.index(f"\n## v{mod_base.__version__}\n")
        following = text.find("\n## v", start + 1)
        release = text[start:following if following != -1 else len(text)]
        self.assertRegex(release, re.compile(rf"`pixel_metrics_version` {mod_base.PIXEL_METRICS_VERSION}\b"))

    def test_the_config_accepts_only_the_current_version(self) -> None:
        for sample in ("config/qs.json", "config/bp.json"):
            config = load(sample)
            validate_config(copy.deepcopy(config))
            config["images"]["pixel_metrics_version"] = mod_base.PIXEL_METRICS_VERSION + 1
            with self.subTest(config=sample), self.assertRaises(MbError):
                validate_config(config)

    def test_documents_accept_only_the_current_version(self) -> None:
        cases = {
            "valid/expectation.json": lambda document: document["image_policy"],
            "valid/build.json": lambda document: document,
        }
        for relative, holder in cases.items():
            document = load(relative)
            self.assertEqual(holder(document)["pixel_metrics_version"], mod_base.PIXEL_METRICS_VERSION)
            documents.validate_document(copy.deepcopy(document))
            holder(document)["pixel_metrics_version"] = mod_base.PIXEL_METRICS_VERSION + 1
            with self.subTest(document=relative), self.assertRaises(MbError):
                documents.validate_document(document)

    def test_the_gallery_publishes_the_current_version(self) -> None:
        gallery = load("valid/gallery.json")
        self.assertEqual(gallery["build"]["pixel_metrics_version"], mod_base.PIXEL_METRICS_VERSION)
        gallery["build"]["pixel_metrics_version"] = mod_base.PIXEL_METRICS_VERSION + 1
        with self.assertRaises(MbError):
            documents.validate_gallery(gallery)


if __name__ == "__main__":
    unittest.main()
