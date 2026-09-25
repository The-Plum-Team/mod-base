"""Artifact model, bounded listings, verified download and exact-ID deletion (MB1).

Quick Skin ``rotate_artifacts.Artifact`` shape; Block Pops ``download_artifact`` transport
(``_CredentialSafeRedirect``) and Quick Skin ``collect_compatibility._CredentialStrippingRedirect``.
Downloads are by immutable numeric id only: metadata (name, digest, size, owner run, not expired)
is re-read and compared with the caller's expectation before any byte is fetched, the ZIP's
SHA-256 must equal the ``digest``, and extraction goes through :mod:`mod_base.io.bounded_zip`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.io.bounded_zip import ExtractionLimits
from mod_base.model import limits

OWNER = "MB1"


@dataclass(frozen=True)
class Artifact:
    """One Actions artifact as reported by the API (``workflow_run`` flattened)."""

    id: int
    name: str
    size: int
    expired: bool
    created_at: str
    digest: str
    run_id: int
    head_branch: str
    head_sha: str

    @property
    def order(self) -> tuple[datetime, int]:
        """``(created_at, id)``: newest-last ordering."""

        raise NotImplementedError("owned by MB1")

    @classmethod
    def parse(cls, raw: Any) -> "Artifact":
        """Strictly parse one API record (positive ids/size, ``sha256:`` digest, RFC 3339 time,
        40-hex ``head_sha``, name within ``limits.MAX_ARTIFACT_NAME_BYTES``)."""

        raise NotImplementedError("owned by MB1")


def get_artifact(api: GitHubApi, artifact_id: int) -> Artifact:
    raise NotImplementedError("owned by MB1")


def list_named(api: GitHubApi, name: str, *, max_items: int = limits.MAX_ARTIFACTS_PER_NAME) -> list[Artifact]:
    """Artifacts with exactly ``name`` (``?name=``), every row re-checked to carry that name."""

    raise NotImplementedError("owned by MB1")


def list_for_run(api: GitHubApi, run_id: int, *, max_items: int = limits.MAX_ARTIFACTS_PER_NAME) -> list[Artifact]:
    """Artifacts of one run; every row's ``run_id`` must equal ``run_id``."""

    raise NotImplementedError("owned by MB1")


def list_repository(api: GitHubApi, *, max_items: int) -> list[Artifact]:
    """Every repository artifact, newest first, bounded by ``max_items`` (fail closed beyond)."""

    raise NotImplementedError("owned by MB1")


def download(api: GitHubApi, *, artifact_id: int, name: str, digest: str, size: int, run_id: int,
             output: Path, extraction: ExtractionLimits) -> list[str]:
    """Download artifact ``artifact_id`` into the new directory ``output`` after re-reading its
    metadata and requiring exactly ``name``, ``digest``, ``size``, owner ``run_id`` and not expired;
    the ZIP bytes must hash to ``digest``. Returns the extracted relative paths."""

    raise NotImplementedError("owned by MB1")


def delete(api: GitHubApi, artifact_id: int) -> None:
    """``DELETE`` one artifact by id (writable client only; 404 raises, never "already gone")."""

    raise NotImplementedError("owned by MB1")
