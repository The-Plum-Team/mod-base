from __future__ import annotations

import unittest

from mod_base.model import limits


class GlobalLimitsTest(unittest.TestCase):
    """SPEC §3.0 "Global limits" table, verbatim."""

    def test_spec_table(self) -> None:
        mib, gib = 1024 * 1024, 1024 * 1024 * 1024
        self.assertEqual(limits.MAX_MANIFEST_BYTES, 10 * mib)
        self.assertEqual(limits.MAX_EXPECTATION_BYTES, 16 * mib)
        self.assertEqual(limits.MAX_SELECTION_BYTES, 1 * mib)
        self.assertEqual(limits.MAX_EXTENSIONS_BYTES, 1 * mib)
        self.assertEqual(limits.MAX_FRAMES, 1000)
        self.assertEqual(limits.MAX_RUNTIME_FILES, 4096)
        self.assertEqual(limits.MAX_RUNTIME_JSON_BYTES, 4 * mib)
        self.assertEqual(limits.MAX_SOURCE_PNG_BYTES, 32 * mib)
        self.assertEqual(limits.MAX_DERIVATIVE_BYTES, 4 * mib)
        self.assertEqual(limits.MAX_RAW_BUNDLE_BYTES, 1 * gib)
        self.assertEqual(limits.MAX_COMPACT_BUNDLE_BYTES, 256 * mib)
        self.assertEqual(limits.MAX_SITE_BYTES, 1 * gib)
        self.assertEqual(limits.MAX_SITE_FILES, 8192)
        self.assertEqual(limits.MAX_IMAGE_PIXELS, 20_000_000)
        self.assertEqual(limits.MAX_ARTIFACTS_PER_NAME, 512)
        self.assertEqual(limits.MAX_PAGES_API_READS, 160)

    def test_other_spec_bounds(self) -> None:
        self.assertEqual(limits.MAX_RUNTIME_EVIDENCE_LENGTH, 4096)
        self.assertEqual(limits.MAX_SCOPE_DETAIL_BYTES, 64 * 1024)
        self.assertEqual(limits.MAX_ADAPTER_RESPONSE_BYTES, 16 * 1024 * 1024)
        self.assertEqual((limits.ADAPTER_TIMEOUT_DEFAULT_SECONDS, limits.ADAPTER_TIMEOUT_MAX_SECONDS), (600, 1800))
        self.assertEqual(limits.MAX_ZIP_RATIO, 200)
        self.assertEqual(limits.DELETION_BUDGET, 32)
        self.assertEqual(limits.MAX_KEYS, 64)
        self.assertEqual(limits.MAX_BRANCHES, 100)
        self.assertEqual((limits.MAX_ICON_BYTES, limits.MAX_ICON_DIMENSION), (512 * 1024, 1024))
        self.assertEqual(limits.MAX_API_RESPONSE_BYTES, 32 * 1024 * 1024)
        self.assertEqual((limits.RUN_POLL_ATTEMPTS, limits.RUN_POLL_INTERVAL_SECONDS), (30, 2.0))

    def test_artifact_retention_map(self) -> None:
        self.assertEqual(limits.RETENTION_DAYS, {
            "handoff": 1, "collected": 1, "collected-family": 1, "promotion": 1, "pages": 1,
            "cache": 90, "family-cache": 90, "baseline": 90,
        })
        self.assertEqual(limits.MAX_ANCHOR_RETENTION_DAYS, 90)
        self.assertEqual(limits.MAX_FAMILY_RETENTION_DAYS, 7)

    def test_derived_bounds_are_consistent(self) -> None:
        self.assertEqual(limits.MAX_HANDOFF_FILES, limits.MAX_RUNTIME_FILES + 2)
        self.assertEqual(limits.MAX_COMPACT_FILES, limits.MAX_FRAMES + 3)
        self.assertLess(limits.MAX_COMPACT_BUNDLE_BYTES, limits.MAX_RAW_BUNDLE_BYTES)
        self.assertLessEqual(limits.MAX_FAMILY_HANDOFF_BYTES, limits.MAX_RAW_BUNDLE_BYTES)


if __name__ == "__main__":
    unittest.main()
