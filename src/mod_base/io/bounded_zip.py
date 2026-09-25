"""Bounded ZIP extraction (MB1).

Union of Quick Skin ``scripts/ci/bounded_zip.py`` and Block Pops ``download_artifact`` rules:
stored or deflate entries only; no encrypted, symlink, special, absolute, ``..``, backslash or
duplicate entries; directory entries only as parents; per-entry and total expanded-size caps and a
compression ratio of at most 200, all checked from the central directory before any byte is
inflated and again while streaming. Extraction publishes through
:func:`mod_base.io.atomic_directory.atomic_directory`, so ``destination`` must not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model import limits

OWNER = "MB1"


class ZipRejected(MbError):
    """An archive violates the extraction policy (exit 2)."""

    default_reason = "zip-rejected"


@dataclass(frozen=True)
class ExtractionLimits:
    """Bounds for one archive. ``suffixes`` (when set) restricts every file name's extension."""

    max_entries: int
    max_total_bytes: int
    max_entry_bytes: int
    max_ratio: int = limits.MAX_ZIP_RATIO
    suffixes: frozenset[str] | None = None


#: Limits per downloaded artifact kind (see ``model.grammar.ARTIFACT_PREFIXES``).
LIMITS_BY_KIND: dict[str, ExtractionLimits] = {
    "handoff": ExtractionLimits(limits.MAX_HANDOFF_FILES + 1, limits.MAX_RAW_BUNDLE_BYTES,
                                limits.MAX_SOURCE_PNG_BYTES, suffixes=frozenset({".json", ".png"})),
    "anchor": ExtractionLimits(limits.MAX_ANCHOR_FILES + 1, limits.MAX_ANCHOR_BUNDLE_BYTES,
                               limits.MAX_SOURCE_PNG_BYTES, suffixes=frozenset({".json", ".png"})),
    "cache": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                              limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    "collected": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                                  limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    "baseline": ExtractionLimits(limits.MAX_COMPACT_FILES + 1, limits.MAX_COMPACT_BUNDLE_BYTES,
                                 limits.MAX_EXPECTATION_BYTES, suffixes=frozenset({".json", ".webp"})),
    "family-handoff": ExtractionLimits(limits.MAX_FAMILY_FILES + 1, limits.MAX_FAMILY_HANDOFF_BYTES,
                                       limits.MAX_SOURCE_PNG_BYTES),
    "family-cache": ExtractionLimits(limits.MAX_FAMILY_FILES + 1, limits.MAX_FAMILY_HANDOFF_BYTES,
                                     limits.MAX_SOURCE_PNG_BYTES),
    "collected-family": ExtractionLimits(limits.MAX_FAMILY_FILES + 2, limits.MAX_FAMILY_HANDOFF_BYTES,
                                         limits.MAX_PAIRED_BYTES, suffixes=frozenset({".json", ".webp"})),
    "promotion": ExtractionLimits(2, limits.MAX_PROMOTION_BYTES, limits.MAX_PROMOTION_BYTES,
                                  suffixes=frozenset({".json"})),
}


def extract(archive: Path | bytes, destination: Path, limits_: ExtractionLimits) -> list[str]:
    """Validate and extract ``archive`` into the new directory ``destination``.

    Returns the extracted relative file paths, sorted. Raises :class:`ZipRejected` for any
    policy violation; on failure ``destination`` does not exist.
    """

    raise NotImplementedError("owned by MB1")
