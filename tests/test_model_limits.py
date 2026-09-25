from __future__ import annotations

import importlib
import json
import pkgutil
import unittest
from pathlib import Path

import mod_base
from mod_base.errors import MbError
from mod_base.github import api, artifacts
from mod_base.io.bounded_zip import LIMITS_BY_KIND, ExtractionLimits, ZipRejected, archive_limit, artifact_limit
from mod_base.model import limits
from mod_base.pages.select import family_archive_limit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "documents" / "config"


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
        # SPEC §5.5 says 32; a Quick Skin generation supersedes about 35 long-lived artifacts, so the
        # budget is 64 (the rotation amendment in docs/INTERNAL-API.md).
        self.assertEqual(limits.DELETION_BUDGET, 64)
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
        self.assertLessEqual(limits.MAX_ANCHOR_BUNDLE_BYTES, limits.MAX_RAW_BUNDLE_BYTES)
        self.assertEqual(limits.MAX_ARTIFACT_BYTES, limits.MAX_RAW_BUNDLE_BYTES + limits.MAX_ARCHIVE_OVERHEAD_BYTES)
        self.assertEqual(limits.MAX_ARCHIVE_OVERHEAD_BYTES, 32 * limits.MIB)


class ZipCeilingsTest(unittest.TestCase):
    """The per-kind extraction ceilings are named here (amendment: the ``collected-family`` layout)."""

    def test_collected_family_bounds(self) -> None:
        # paired.json + selected.json + images/ (<= MAX_FAMILY_FILES) + source/envelope.json + source/
        # native files (<= MAX_FAMILY_FILES).
        self.assertEqual(limits.MAX_COLLECTED_FAMILY_FILES, 2 * limits.MAX_FAMILY_FILES + 3)
        self.assertEqual(limits.MAX_COLLECTED_FAMILY_FILES, 16387)
        # The projection images and the verbatim family bundle fit one bundle together.
        self.assertEqual(limits.MAX_COLLECTED_FAMILY_BYTES, limits.MAX_RAW_BUNDLE_BYTES)
        self.assertEqual(limits.MAX_COLLECTED_FAMILY_BYTES, 1024 * 1024 * 1024)

    def test_zip_entries_is_the_largest_kind_bound(self) -> None:
        self.assertEqual(limits.MAX_ZIP_ENTRIES, limits.MAX_COLLECTED_FAMILY_FILES)
        for bound in (limits.MAX_HANDOFF_FILES, limits.MAX_COMPACT_FILES, limits.MAX_ANCHOR_FILES,
                      limits.MAX_FAMILY_FILES, limits.MAX_RUNTIME_FILES):
            self.assertLess(bound, limits.MAX_ZIP_ENTRIES)

    def test_every_extraction_kind_fits_the_ceilings(self) -> None:
        for kind, bounds in LIMITS_BY_KIND.items():
            with self.subTest(kind=kind):
                self.assertLessEqual(bounds.max_entries, limits.MAX_ZIP_ENTRIES)
                self.assertLessEqual(bounds.max_ratio, limits.MAX_ZIP_RATIO)
                # No kind expands beyond one bundle uploaded as a single artifact (ArchiveCapTest).
                self.assertLessEqual(bounds.max_total_bytes, limits.MAX_RAW_BUNDLE_BYTES)
        family = LIMITS_BY_KIND["collected-family"]
        self.assertEqual(family.max_entries, limits.MAX_COLLECTED_FAMILY_FILES)
        self.assertLessEqual(family.max_total_bytes, limits.MAX_COLLECTED_FAMILY_BYTES)


class ArchiveCapTest(unittest.TestCase):
    """What a producer checks predicts what every consumer accepts. Every public expanded bound stays
    SPEC §3.0's (a raw bundle, an anchor, a family handoff or cache and a collected family: 1 GiB),
    and the one cap on admitted, selected and downloaded archives is that bound plus a documented
    overhead margin (``MAX_ARCHIVE_OVERHEAD_BYTES``) that covers what an archive adds to its files
    (``archive_limit``: deflate framing, per-entry headers and slack) for every kind. With the cap at
    1 GiB, a handoff (1,081,084,928 archive bytes) or a collected family (1,093,666,816) passed its
    own bound and was then refused as an artifact."""

    def test_every_kind_archives_within_the_margin_and_the_cap(self) -> None:
        for kind, bounds in LIMITS_BY_KIND.items():
            with self.subTest(kind=kind):
                self.assertLessEqual(bounds.max_total_bytes, limits.MAX_RAW_BUNDLE_BYTES)
                self.assertLessEqual(archive_limit(bounds) - bounds.max_total_bytes, limits.MAX_ARCHIVE_OVERHEAD_BYTES)
                self.assertLessEqual(archive_limit(bounds), limits.MAX_ARTIFACT_BYTES)
                self.assertEqual(artifact_limit(kind), archive_limit(bounds))

    def test_the_widest_kind_keeps_headroom(self) -> None:
        for kind in ("handoff", "anchor", "collected-family"):
            with self.subTest(kind=kind):
                self.assertEqual(LIMITS_BY_KIND[kind].max_total_bytes, limits.MAX_RAW_BUNDLE_BYTES)
        for kind in ("family-handoff", "family-cache"):
            with self.subTest(kind=kind):
                self.assertEqual(LIMITS_BY_KIND[kind].max_total_bytes, limits.MAX_FAMILY_BUNDLE_BYTES)
        widest = max(archive_limit(bounds) for bounds in LIMITS_BY_KIND.values())
        self.assertEqual(widest, archive_limit(LIMITS_BY_KIND["collected-family"]))
        self.assertEqual(widest, 1_093_667_840)  # docs/SCHEMAS.md, "Archive cap and expanded totals"
        self.assertEqual(limits.MAX_ARTIFACT_BYTES, 1_107_296_256)
        self.assertLess(widest, limits.MAX_ARTIFACT_BYTES - 8 * limits.MIB)

    def test_every_configurable_family_handoff_archives_within_the_cap(self) -> None:
        # ``families[].handoff_max_bytes`` may reach MAX_FAMILY_HANDOFF_BYTES (config.py); admission,
        # select and build bound a family handoff or cache (that native bundle plus its envelope.json)
        # by the archive limit of the configured value (``select.family_archive_limit``).
        for configured in (1, 512 * limits.MIB, limits.MAX_FAMILY_HANDOFF_BYTES):
            with self.subTest(configured=configured):
                limit = family_archive_limit({"handoff_max_bytes": configured})
                expanded = configured + limits.MAX_ENVELOPE_BYTES
                self.assertEqual(limit, artifact_limit("family-handoff", max_total_bytes=expanded))
                self.assertGreater(limit, expanded)
                self.assertLessEqual(limit - expanded, limits.MAX_ARCHIVE_OVERHEAD_BYTES)
                self.assertLessEqual(limit, artifact_limit("family-cache"))
                self.assertLessEqual(limit, limits.MAX_ARTIFACT_BYTES)
        for invalid in (0, -1, True, limits.MAX_FAMILY_BUNDLE_BYTES + 1, 1.0):
            with self.subTest(invalid=invalid), self.assertRaises(ZipRejected):
                artifact_limit("family-handoff", max_total_bytes=invalid)  # type: ignore[arg-type]
        with self.assertRaises(ZipRejected):
            artifact_limit("mb-unknown")

    def test_the_download_caps_are_the_artifact_cap(self) -> None:
        self.assertEqual(artifacts.MAX_ARCHIVE_BYTES, limits.MAX_ARTIFACT_BYTES)
        self.assertEqual(api.MAX_DOWNLOAD_BYTES, limits.MAX_ARTIFACT_BYTES)

    def test_an_expanded_total_at_the_cap_would_not_fit(self) -> None:
        # The regression: an expanded total equal to the archive cap leaves no room for the overhead.
        for kind in ("handoff", "collected-family"):
            bounds = LIMITS_BY_KIND[kind]
            at_cap = ExtractionLimits(bounds.max_entries, limits.MAX_ARTIFACT_BYTES, bounds.max_entry_bytes)
            with self.subTest(kind=kind):
                self.assertGreater(archive_limit(at_cap), limits.MAX_ARTIFACT_BYTES)


class FamilyBudgetTest(unittest.TestCase):
    """A collected family (``family collect``) holds its generation verbatim beside the projection, so
    the family bounds partition ``MAX_COLLECTED_FAMILY_BYTES``: a native bundle a producer's ``family
    envelope`` accepts (``families[].handoff_max_bytes``, config-capped at ``MAX_FAMILY_HANDOFF_BYTES``)
    is always collectable. With the ceiling at 1 GiB, a handoff near its configured bound passed the
    producer and was then refused by ``family collect`` (exit 2), which blocked every publication."""

    def test_the_parts_fill_exactly_one_collected_family(self) -> None:
        parts = (limits.MAX_FAMILY_HANDOFF_BYTES, limits.MAX_ENVELOPE_BYTES, limits.MAX_PAIRED_BYTES,
                 limits.MAX_SELECTED_JSON_BYTES, limits.MAX_FAMILY_PROJECTION_BYTES)
        self.assertEqual(sum(parts), limits.MAX_COLLECTED_FAMILY_BYTES)
        self.assertEqual(limits.MAX_FAMILY_PROJECTION_BYTES, limits.MAX_COMPACT_BUNDLE_BYTES)
        self.assertEqual(limits.MAX_FAMILY_HANDOFF_BYTES, 784_269_312)  # docs/SCHEMAS.md, config table
        self.assertEqual(limits.MAX_FAMILY_BUNDLE_BYTES, limits.MAX_FAMILY_HANDOFF_BYTES + limits.MAX_ENVELOPE_BYTES)
        self.assertEqual(LIMITS_BY_KIND["collected-family"].max_total_bytes, limits.MAX_COLLECTED_FAMILY_BYTES)

    def test_the_config_ceiling_is_the_collectable_native_bound(self) -> None:
        from mod_base import config

        document = json.loads((FIXTURES / "qs.json").read_text(encoding="utf-8"))
        document["families"][0]["handoff_max_bytes"] = limits.MAX_FAMILY_HANDOFF_BYTES
        config.validate_config(document)
        for invalid in (limits.MAX_FAMILY_HANDOFF_BYTES + 1, limits.MAX_RAW_BUNDLE_BYTES):
            document["families"][0]["handoff_max_bytes"] = invalid
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                    MbError, f"must be between 1 and {limits.MAX_FAMILY_HANDOFF_BYTES}"):
                config.validate_config(document)


class UnitBoundsTest(unittest.TestCase):
    """Bounds that used to be local to one unit now live here with the same values."""

    EXPECTED = {
        "MAX_CANDIDATES": 8,
        "MAX_SUBJECT_RUNS": 100,
        "MAX_CANONICAL_RUNS": 300,
        "MAX_FAMILY_LEGS": 256,
        "MAX_WORKFLOW_FILE_BYTES": 1024 * 1024,
        "MAX_SELECTED_JSON_BYTES": 64 * 1024,
        "GENERATION_PROBES": 32,
        "ANCHOR_PROBES": 16,
    }

    def test_values(self) -> None:
        for name, value in self.EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(getattr(limits, name), value)
                self.assertIs(type(getattr(limits, name)), int)

    def test_every_same_named_module_constant_is_an_alias(self) -> None:
        # A module constant named like a bound (``select.MAX_CANDIDATES``, ``rotate.GENERATION_PROBES``,
        # ``imaging.metrics.MAX_IMAGE_PIXELS``...) may only alias it: a drifted copy anywhere in the kit
        # would enforce another bound than the one documented here.
        bounds = {name: value for name, value in vars(limits).items() if name.isupper()}
        aliases = 0
        for info in pkgutil.walk_packages(mod_base.__path__, "mod_base."):
            if info.name == limits.__name__ or info.name.endswith("__main__"):
                continue
            namespace = vars(importlib.import_module(info.name))
            for name in sorted(bounds.keys() & namespace.keys()):
                with self.subTest(module=info.name, name=name):
                    self.assertEqual(namespace[name], bounds[name])
                    self.assertIs(type(namespace[name]), type(bounds[name]))
                    aliases += 1
        self.assertGreater(aliases, 0)


if __name__ == "__main__":
    unittest.main()
