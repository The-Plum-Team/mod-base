"""Adapter ``targets`` orchestration, enrolled-branch listing and inert fetches (MB5).

``default-branch`` mode: the adapter receives ``branches=None`` and every key's subject is the
protected head (``GITHUB_SHA``). ``enrolled-branches`` mode: the core lists at most
``targets.max_branches`` branches (one page, ``per_page=100``), fetches their heads anonymously as
inert objects (``git -C repo fetch --no-tags --depth=<n> origin <sha...>``, never checked out) and
passes ``[{name, commit, tree}]``; the adapter decides enrollment through ``ctx.read_blob``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB5"


def list_enrolled_branches(api: GitHubApi, *, max_branches: int) -> list[dict[str, str]]:
    """``[{name, commit, tree}]`` of the repository's branches (one page; more than
    ``max_branches`` fails closed), each tree read from the commit API."""

    raise NotImplementedError("owned by MB5")


def fetch_inert(repo_root: Path, commits: Sequence[str], *, depth: int = 1) -> None:
    """Fetch ``commits`` into ``repo_root``'s object store without checkout, tags or credentials
    (sanitized ``git`` environment, ``protocol.version=2``, bounded retries)."""

    raise NotImplementedError("owned by MB5")


def discover_targets(invocation: Invocation, *, api: GitHubApi | None) -> list[dict[str, Any]]:
    """Run the adapter ``targets`` hook for the configured mode and return 1..``targets.max``
    validated targets (``api`` is required in enrolled-branches mode)."""

    raise NotImplementedError("owned by MB5")
