"""The lossless anchor (MB3): Block Pops ``scripts/pages/visual_anchor.py`` generalized.

Eligibility: the adapter's ``anchor_selection`` returns nodes and the handoff was a direct
canonical run (``handoff.controller_sha == subject.commit``). ``create`` cuts the reference lanes
out of an uploaded handoff (bound to its artifact id, name and digest) and re-encodes every PNG
canonically to ``images/<sha>.png``. Block Pops ``scripts/visual/curate.py`` imports
:func:`artifact_name` and :func:`validate_anchor_dir` from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.model import grammar
from mod_base.runtime import Invocation

OWNER = "MB3"


@dataclass(frozen=True)
class AnchorIdentity:
    eligible: bool
    name: str | None
    artifact_nodes: tuple[str, ...]


def artifact_name(key: str, commit: str, run_id: int, run_attempt: int) -> str:
    """``mb-anchor--{key}--{commit}--{run_id}--a{attempt}`` (delegates to the grammar)."""

    return grammar.anchor_name(key, commit, run_id, run_attempt)


def anchor_identity(invocation: Invocation, handoff_dir: Path, *, key: str) -> AnchorIdentity:
    """Decide eligibility for a validated handoff directory and name the anchor it would produce."""

    raise NotImplementedError("owned by MB3")


def create_anchor(invocation: Invocation, *, key: str, handoff_dir: Path, raw_artifact_id: int,
                  raw_artifact_name: str, raw_artifact_digest: str, output: Path) -> dict[str, Any]:
    """Write the anchor bundle into the new ``output`` and return its validated manifest."""

    raise NotImplementedError("owned by MB3")


def validate_anchor_dir(root: Path, *, key: str | None = None, expected_subject_commit: str | None = None,
                        raw_artifact_id: int | None = None, raw_artifact_name: str | None = None,
                        raw_artifact_digest: str | None = None) -> dict[str, Any]:
    """Validate an anchor directory (inventory, hashes, canonical PNG re-inspection, embedded
    expectation) and, when all three ``raw_artifact_*`` are given, its source artifact binding.
    Needs no config: BP curate calls it with the anchor alone."""

    raise NotImplementedError("owned by MB3")
