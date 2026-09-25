"""Artifact model, bounded listings, verified download and exact-ID deletion (MB1).

Quick Skin ``rotate_artifacts.Artifact`` shape; Block Pops ``download_artifact`` transport
(``_CredentialSafeRedirect``) and Quick Skin ``collect_compatibility._CredentialStrippingRedirect``.
Downloads are by immutable numeric id only: metadata (name, digest, size, owner run, not expired)
is re-read and compared with the caller's expectation before any byte is fetched, the ZIP's
SHA-256 must equal the ``digest``, and extraction goes through :mod:`mod_base.io.bounded_zip`.

The credential-stripping redirect of both lineages is :meth:`mod_base.github.api.GitHubApi.download`:
the API answers the ``/zip`` endpoint with one redirect, which is followed exactly once to an https
URL without credentials and without the ``Authorization`` header.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import actions_time
from mod_base.io.bounded_zip import ExtractionLimits, archive_limit, extract
from mod_base.model import grammar, limits

OWNER = "MB1"

MAX_HEAD_BRANCH_CHARS = 255
#: The largest compressed artifact the kit downloads: ``limits.MAX_ARTIFACT_BYTES`` (the largest
#: expanded bundle plus its archive overhead).
MAX_ARCHIVE_BYTES = limits.MAX_ARTIFACT_BYTES


def _fail(message: str) -> MbError:
    return MbError(message, reason="artifact")


def _positive(value: Any, label: str, maximum: int = limits.MAX_RUN_ID) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise _fail(f"{label} must be a positive integer no larger than {maximum}")
    return value


def _printable(value: Any, label: str, *, max_bytes: int) -> str:
    if (not isinstance(value, str) or not value or len(value.encode("utf-8", "surrogatepass")) > max_bytes
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise _fail(f"{label} must be a printable string of at most {max_bytes} bytes")
    return value


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

        return actions_time(self.created_at, "artifact created_at"), self.id

    @classmethod
    def parse(cls, raw: Any) -> "Artifact":
        """Strictly parse one API record (positive ids/size, ``sha256:`` digest, RFC 3339 time,
        40-hex ``head_sha``, name within ``limits.MAX_ARTIFACT_NAME_BYTES``)."""

        if not isinstance(raw, dict) or not isinstance(raw.get("workflow_run"), dict):
            raise _fail("artifact API record is malformed")
        workflow = raw["workflow_run"]
        if not isinstance(raw.get("expired"), bool):
            raise _fail("artifact expired flag must be a boolean")
        digest = raw.get("digest")
        if not grammar.is_match(grammar.DIGEST, digest):
            raise _fail("artifact API record has no immutable sha256 digest")
        head_sha = workflow.get("head_sha")
        if not grammar.is_match(grammar.SHA1, head_sha):
            raise _fail("artifact workflow_run.head_sha must be a 40-hex commit")
        created_at = raw.get("created_at")
        try:
            actions_time(created_at, "artifact created_at")
        except MbError as exc:
            raise _fail(str(exc)) from exc
        return cls(
            id=_positive(raw.get("id"), "artifact id"),
            name=_printable(raw.get("name"), "artifact name", max_bytes=limits.MAX_ARTIFACT_NAME_BYTES),
            size=_positive(raw.get("size_in_bytes"), "artifact size_in_bytes", 1 << 40),
            expired=raw["expired"],
            created_at=created_at,
            digest=digest,
            run_id=_positive(workflow.get("id"), "artifact workflow_run.id"),
            head_branch=_printable(workflow.get("head_branch"), "artifact workflow_run.head_branch",
                                   max_bytes=MAX_HEAD_BRANCH_CHARS),
            head_sha=head_sha,
        )


def _newest_first(artifacts: list[Artifact]) -> list[Artifact]:
    ids = [artifact.id for artifact in artifacts]
    if len(set(ids)) != len(ids):
        raise _fail("artifact listing repeats an artifact id")
    return sorted(artifacts, key=lambda artifact: artifact.order, reverse=True)


def get_artifact(api: GitHubApi, artifact_id: int) -> Artifact:
    _positive(artifact_id, "artifact id")
    artifact = Artifact.parse(api.get_json(f"/repos/{api.repository}/actions/artifacts/{artifact_id}"))
    if artifact.id != artifact_id:
        raise _fail(f"artifact {artifact_id} response names another artifact")
    return artifact


def list_named(api: GitHubApi, name: str, *, max_items: int = limits.MAX_ARTIFACTS_PER_NAME) -> list[Artifact]:
    """Artifacts with exactly ``name`` (``?name=``), every row re-checked to carry that name."""

    if not grammar.is_kit_artifact_name(name):
        raise _fail(f"refusing to list a name that is not a kit artifact name: {name!r}"[:200])
    rows = api.paginate(f"/repos/{api.repository}/actions/artifacts", field="artifacts", params={"name": name},
                        max_items=max_items)
    artifacts = [Artifact.parse(row) for row in rows]
    if any(artifact.name != name for artifact in artifacts):
        raise _fail(f"artifact listing for {name!r} returned another name")
    return _newest_first(artifacts)


def list_for_run(api: GitHubApi, run_id: int, *, max_items: int = limits.MAX_ARTIFACTS_PER_NAME) -> list[Artifact]:
    """Artifacts of one run; every row's ``run_id`` must equal ``run_id``."""

    _positive(run_id, "run id")
    rows = api.paginate(f"/repos/{api.repository}/actions/runs/{run_id}/artifacts", field="artifacts",
                        max_items=max_items)
    artifacts = [Artifact.parse(row) for row in rows]
    if any(artifact.run_id != run_id for artifact in artifacts):
        raise _fail(f"artifact listing of run {run_id} returned another run's artifact")
    return _newest_first(artifacts)


def list_repository(api: GitHubApi, *, max_items: int) -> list[Artifact]:
    """Every repository artifact, newest first, bounded by ``max_items`` (fail closed beyond)."""

    rows = api.paginate(f"/repos/{api.repository}/actions/artifacts", field="artifacts", max_items=max_items)
    return _newest_first([Artifact.parse(row) for row in rows])


def download(api: GitHubApi, *, artifact_id: int, name: str, digest: str, size: int, run_id: int,
             output: Path, extraction: ExtractionLimits) -> list[str]:
    """Download artifact ``artifact_id`` into the new directory ``output`` after re-reading its
    metadata and requiring exactly ``name``, ``digest``, ``size``, owner ``run_id`` and not expired;
    the ZIP bytes must hash to ``digest``. Returns the extracted relative paths.

    ``size`` must also fit the archive bound of ``extraction`` (:func:`bounded_zip.archive_limit`),
    so an artifact its extractor would refuse is never fetched into memory."""

    _positive(artifact_id, "artifact id")
    if not isinstance(extraction, ExtractionLimits):
        raise _fail("extraction limits are required")
    _positive(size, "artifact size", min(MAX_ARCHIVE_BYTES, archive_limit(extraction)))
    _positive(run_id, "artifact run id")
    if grammar.parse_artifact_name(name) is None:
        raise _fail(f"refusing to download a name that is not a kit artifact name: {name!r}"[:200])
    grammar.require(grammar.DIGEST, digest, "artifact digest")
    if os.path.lexists(output):
        raise _fail("the download output directory must not exist")
    artifact = get_artifact(api, artifact_id)
    for label, observed, wanted in (("name", artifact.name, name), ("digest", artifact.digest, digest),
                                    ("size", artifact.size, size), ("owner run", artifact.run_id, run_id)):
        if observed != wanted:
            raise _fail(f"artifact {artifact_id} {label} differs from the selected artifact")
    if artifact.expired:
        raise _fail(f"artifact {artifact_id} has expired")
    data = api.download(f"/repos/{api.repository}/actions/artifacts/{artifact_id}/zip", max_bytes=size)
    if len(data) != size:
        raise _fail(f"artifact {artifact_id} download is {len(data)} bytes, not {size}")
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise _fail(f"artifact {artifact_id} bytes do not match their sha256 digest")
    return extract(data, output, extraction)


def delete(api: GitHubApi, artifact_id: int) -> None:
    """``DELETE`` one artifact by id (writable client only; 404 raises, never "already gone")."""

    _positive(artifact_id, "artifact id")
    api.delete(f"/repos/{api.repository}/actions/artifacts/{artifact_id}")
