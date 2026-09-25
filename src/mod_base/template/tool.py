"""``template check|sync|init`` (MB9, SPEC §8.2).

``check`` reports every drift as a unified diff: managed files byte-identical to
``template/managed/<path>`` (``pages.yml``'s managed region after ``{{PIN}}``/``{{VERSION}}``
substitution plus the extension-region rules of SPEC §5.2), fragment files holding their required
lines/markers, the ``AGENTS.md`` grammar of SPEC §8.3, the managed-docs link rule, and the absence
of ``CLAUDE.md``, ``.claude/CLAUDE.md`` and ``CLAUDE.local.md``; files in ``template.deferred`` may
be absent. ``sync`` rewrites managed files and the caller's managed region (with ``write``),
preserving the pin and the extension region. ``init`` seeds a new repository and never overwrites.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

OWNER = "MB9"
MANIFEST_PATH = "template/manifest.json"


@dataclass(frozen=True)
class Drift:
    """One difference: ``kind`` is ``missing``, ``changed``, ``fragment``, ``agents``, ``links``,
    ``forbidden`` or ``extension``; ``detail`` is a bounded unified diff or message."""

    path: str
    kind: str
    detail: str


def load_manifest(kit_root: Path) -> dict[str, Any]:
    """Read and validate ``template/manifest.json`` (``mod-base.template-manifest`` v1)."""

    raise NotImplementedError("owned by MB9")


def check(repo: Path, *, kit_root: Path) -> list[Drift]:
    raise NotImplementedError("owned by MB9")


def sync(repo: Path, *, kit_root: Path, write: bool) -> list[Drift]:
    raise NotImplementedError("owned by MB9")


def init(repo: Path, *, kit_root: Path, seed: bool, from_config: Path | None) -> list[str]:
    """Seed missing files; return the created paths; refuse to overwrite anything."""

    raise NotImplementedError("owned by MB9")
