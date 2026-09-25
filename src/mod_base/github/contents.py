"""Repository contents, Git objects and reachability reads (MB1).

Used for the kit binding of handoffs (the pin in ``config.source.workflow`` at the handoff run's
head), live head rechecks, enrolled-branch trees and ``compare/<kit_sha>...main`` reachability.
"""

from __future__ import annotations

from typing import Any

from mod_base.github.api import GitHubApi

OWNER = "MB1"


def default_branch(api: GitHubApi) -> str:
    """``GET /repos/{repo}`` ``.default_branch`` (validated branch grammar)."""

    raise NotImplementedError("owned by MB1")


def branch_head(api: GitHubApi, branch: str) -> tuple[str, str]:
    """``(commit, tree)`` of the live head of ``branch``."""

    raise NotImplementedError("owned by MB1")


def commit_tree(api: GitHubApi, commit: str) -> str:
    """The tree SHA of ``commit`` (``/git/commits/{sha}``)."""

    raise NotImplementedError("owned by MB1")


def file_at(api: GitHubApi, path: str, ref: str, *, max_bytes: int) -> bytes:
    """Bytes of ``path`` at commit ``ref`` (contents API, base64 decoded strictly, size and Git blob
    SHA re-verified)."""

    raise NotImplementedError("owned by MB1")


def tree(api: GitHubApi, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]:
    """Entries of a Git tree; ``truncated`` must be false."""

    raise NotImplementedError("owned by MB1")


def blob(api: GitHubApi, oid: str, *, max_bytes: int) -> bytes:
    """A Git blob by id, base64-decoded, with its Git object id recomputed and compared."""

    raise NotImplementedError("owned by MB1")


def compare(api: GitHubApi, base: str, head: str, *, repository: str | None = None) -> dict[str, Any]:
    """``GET /repos/{repository or api.repository}/compare/{base}...{head}`` projected to
    ``{status, ahead_by, behind_by}`` with validated types."""

    raise NotImplementedError("owned by MB1")
