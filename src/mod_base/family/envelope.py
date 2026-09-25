"""``mod-base.family.envelope`` creation and validation (MB4).

The envelope (``envelope.json``) sits at the root of each family handoff and cache next to the
mod's native bundle, which only the adapter understands. ``files`` is the exact inventory of the
native bundle (every file except ``envelope.json``); ``native.manifest_sha256`` names its
``manifest.json``; ``producer`` is the producing run's claim from the environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB4"
ENVELOPE_NAME = "envelope.json"


def create_envelope(invocation: Invocation, *, family: str, key: str, bundle_dir: Path, coverage_sha: str,
                    subject: Mapping[str, str], producer: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """Copy the native bundle into the new ``output`` and write ``envelope.json`` beside it.

    ``subject`` is ``{branch, commit}``: the envelope's ``subject.tree`` is read from the checked-out
    repository, whose ``HEAD`` must equal ``commit``. The native bundle must be a bounded
    regular-file tree (at most the family's ``handoff_max_bytes``) holding ``manifest.json`` with a
    string ``kind`` and integer ``schema_version``. Returns the validated envelope."""

    raise NotImplementedError("owned by MB4")


def validate_envelope_dir(invocation: Invocation, root: Path, *, family: str | None = None,
                          key: str | None = None) -> dict[str, Any]:
    """Validate ``root/envelope.json`` against the exact directory inventory and the configured
    family (``handoff_max_bytes``); return the envelope."""

    raise NotImplementedError("owned by MB4")
