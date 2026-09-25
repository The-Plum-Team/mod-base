"""The kit pin: parser, verifier, kit resolution and ``kit-digest-v1`` (MB9).

The same parser as the managed bootstrap ``scripts/ci/mod_base_kit.py`` (parity-tested against it):
every ``.github/workflows/*.yml`` and ``.github/actions/*/action.yml`` line matching
:data:`PIN_LINE` contributes ``(sha, version)``; exactly one pair must result and any other
``The-Plum-Team/mod-base`` token in those files is an error. ``kit-digest-v1`` hashes the listing
``"<sha256>  ./<path>\\n"`` (sorted under ``LC_ALL=C``) of every file under ``src/``, ``site/`` and
``requirements/``, refusing symlinks, special files, executable bits and ``__pycache__``; it is
printed as ``sha256:<hex>`` and equals ``tools/kit_digest.sh``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi

OWNER = "MB9"
PIN_LINE = re.compile(
    r"^\s*(?:-\s+)?uses:\s+The-Plum-Team/mod-base/(\S+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$"
)
KIT_TOKEN = "The-Plum-Team/mod-base"
DIGESTED_DIRS = ("src", "site", "requirements")
STAMP_NAME = "MOD_BASE_KIT.json"
OVERLAY_PATH = "out/mod-base-kit"


@dataclass(frozen=True)
class Pin:
    """The single pin of a mod: ``sha`` (40-hex), ``version`` (``vX.Y.Z``) and every referencing
    ``path@line`` location, sorted."""

    sha: str
    version: str
    references: tuple[str, ...]


def parse_pin_files(files: Mapping[str, bytes]) -> Pin:
    """Parse the pin from ``{repo-relative path: bytes}`` of the workflow and action files."""

    raise NotImplementedError("owned by MB9")


def parse_pin(repo: Path) -> Pin:
    """Read (bounded) the mod's workflow and action files and parse its single pin."""

    raise NotImplementedError("owned by MB9")


def verify(repo: Path, *, network: bool, api: GitHubApi | None = None) -> Pin:
    """Pin consistency; with ``network`` also ``compare/<pin>...main`` is ``ahead|identical`` with
    ``behind_by == 0`` and ``git/ref/tags/<version>`` peels to the pin (``api`` required)."""

    raise NotImplementedError("owned by MB9")


def kit_tree_digest(root: Path) -> str:
    """``sha256:<hex>`` kit-digest-v1 of ``root`` (the kit checkout root)."""

    raise NotImplementedError("owned by MB9")


def kit_path(repo: Path, environ: Mapping[str, str]) -> Path:
    """Resolve the kit root for ``repo`` in the bootstrap order (overlay stamp, env, user cache,
    anonymous fetch), verifying each candidate; raise :class:`mod_base.errors.Unavailable`."""

    raise NotImplementedError("owned by MB9")


def read_stamp(directory: Path) -> dict[str, Any]:
    """Read and validate ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) in ``directory``."""

    raise NotImplementedError("owned by MB9")
