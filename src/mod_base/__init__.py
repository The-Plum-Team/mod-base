"""mod-base: the shared public-evidence and Pages publication kit.

The package is used from a checkout at a pinned commit (``PYTHONPATH=<kit>/src python3 -P -m
mod_base``); it is never pip-installed in CI. Every document kind the kit reads or writes is listed
in :data:`SCHEMA_VERSIONS` with the version this release writes. Readers accept that version and
the previous one (the N/N-1 rule); for ``schema_version`` 1 that is exactly ``{1}``.
"""

from __future__ import annotations

__version__ = "1.1.2"

#: Adapter protocol version this kit calls (SPEC §4.2). Adapters declare ``ADAPTER_API``.
ADAPTER_API = 1

#: Version of the 8-key PixelMetrics algorithm (SPEC §3.0). A change forces regeneration.
PIXEL_METRICS_VERSION = 1

#: The repository every ``KitRef`` names.
KIT_REPOSITORY = "The-Plum-Team/mod-base"

#: Every document kind (SPEC §3 and §4.1) mapped to the schema version this kit writes.
SCHEMA_VERSIONS: dict[str, int] = {
    "mod-base.config": 1,
    "mod-base.evidence.expectation": 1,
    "mod-base.evidence.handoff": 1,
    "mod-base.evidence.compact": 1,
    "mod-base.evidence.anchor": 1,
    "mod-base.family.envelope": 1,
    "mod-base.family.paired": 1,
    "mod-base.selection": 1,
    "mod-base.promotion": 1,
    "mod-base.build": 1,
    "mod-base.site": 1,
    "mod-base.gallery": 1,
    "mod-base.template-manifest": 1,
    "mod-base.kit-stamp": 1,
    "mod-base.build.plan": 1,
    "mod-base.build.config": 1,
    "mod-base.build.envelope": 1,
    "mod-base.ci.selection": 1,
    "mod-base.ci.gate": 1,
    "mod-base.ci.results": 1,
    "mod-base.ci.reuse": 1,
    "mod-base.ci.validation": 1,
    "mod-base.ci.execution": 1,
    "mod-base.ci.root-request": 1,
    "mod-base.ci.activation": 1,
    "mod-base.ci.batch": 1,
    "mod-base.ci.runtime-envelope": 1,
}


def readable_schema_versions(kind: str) -> frozenset[int]:
    """Return the schema versions a reader of ``kind`` accepts: the current one and N-1 (>= 1)."""

    current = SCHEMA_VERSIONS[kind]
    return frozenset(version for version in (current, current - 1) if version >= 1)
