"""An in-memory GitHub for tests and ``conformance`` (MB1).

``FakeGitHub`` implements the public surface of :class:`mod_base.github.api.GitHubApi` over seeded
state (repository metadata, branches, runs and attempts with ``referenced_workflows``, jobs with
steps, artifacts with ZIP bytes, file contents at refs, commits, trees, blobs, refs and annotated
tags, compare results, plus exact raw GET responses), counts requests exactly like the real
client, and records every mutating call. Every seeder taking ``repository`` defaults to the fake's
own repository; another value seeds a foreign repository (for example ``The-Plum-Team/mod-base``
for pin reachability and tag peeling). It is derived from Quick
Skin ``test_pages_artifact_rotation.FakeApi`` and Block Pops ``test_pages_publication`` fakes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

OWNER = "MB1"


class FakeGitHub:
    """Duck-typed stand-in for ``GitHubApi``; seed it, then pass it where a client is expected."""

    def __init__(self, *, repository: str, default_branch: str = "master", writable: bool = False,
                 max_requests: int | None = None) -> None:
        raise NotImplementedError("owned by MB1")

    # -- seeding ---------------------------------------------------------------------------------
    def set_branch(self, name: str, commit: str, tree: str) -> None:
        raise NotImplementedError("owned by MB1")

    def add_run(self, run: Mapping[str, Any], *, attempts: list[Mapping[str, Any]] | None = None) -> None:
        raise NotImplementedError("owned by MB1")

    def add_jobs(self, run_id: int, run_attempt: int, jobs: list[Mapping[str, Any]]) -> None:
        raise NotImplementedError("owned by MB1")

    def add_artifact(self, record: Mapping[str, Any], archive: bytes) -> None:
        raise NotImplementedError("owned by MB1")

    def add_file(self, ref: str, path: str, data: bytes) -> None:
        raise NotImplementedError("owned by MB1")

    def add_compare(self, base: str, head: str, result: Mapping[str, Any], *, repository: str | None = None) -> None:
        raise NotImplementedError("owned by MB1")

    def add_commit(self, sha: str, tree: str, *, parents: Sequence[str] = (), repository: str | None = None) -> None:
        """Seed ``/git/commits/{sha}`` (``contents.commit_tree``) with its tree and parents."""

        raise NotImplementedError("owned by MB1")

    def add_tree(self, sha: str, entries: Sequence[Mapping[str, Any]], *, truncated: bool = False,
                 repository: str | None = None) -> None:
        """Seed ``/git/trees/{sha}`` (``contents.tree``, ``tools/verify_action_tree.py``): ``entries``
        are the API's ``{path, mode, type, sha, size?}`` rows; ``truncated`` seeds a truncated
        listing, which consumers must refuse."""

        raise NotImplementedError("owned by MB1")

    def add_blob(self, data: bytes, *, oid: str | None = None, repository: str | None = None) -> str:
        """Seed ``/git/blobs/{oid}`` (``contents.blob``) and return its oid: the Git blob id of
        ``data`` unless ``oid`` forces another one (a corrupt object the reader must reject)."""

        raise NotImplementedError("owned by MB1")

    def add_ref(self, ref: str, sha: str, *, annotated_tag_sha: str | None = None,
                repository: str | None = None) -> None:
        """Seed ``/git/ref/{ref}`` (``ref`` like ``tags/v1.0.0`` or ``heads/main``). With
        ``annotated_tag_sha`` the ref points at that tag object, which peels to ``sha`` through
        ``/git/tags/{annotated_tag_sha}`` (``pin.verify``'s tag peel)."""

        raise NotImplementedError("owned by MB1")

    def add_response(self, path: str, payload: Any, *, params: Mapping[str, str | int] | None = None) -> None:
        """Seed the exact JSON body of one GET ``path`` (with exactly ``params``) that no typed seeder
        covers; a request for an unseeded path answers 404 like the API."""

        raise NotImplementedError("owned by MB1")

    # -- GitHubApi surface -----------------------------------------------------------------------
    @property
    def repository(self) -> str:
        raise NotImplementedError("owned by MB1")

    @property
    def writable(self) -> bool:
        raise NotImplementedError("owned by MB1")

    @property
    def request_count(self) -> int:
        raise NotImplementedError("owned by MB1")

    @property
    def deleted_artifact_ids(self) -> list[int]:
        raise NotImplementedError("owned by MB1")

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        raise NotImplementedError("owned by MB1")

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        raise NotImplementedError("owned by MB1")

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        raise NotImplementedError("owned by MB1")

    def delete(self, path: str) -> None:
        raise NotImplementedError("owned by MB1")

    def download(self, path: str, *, max_bytes: int) -> bytes:
        raise NotImplementedError("owned by MB1")

    def rate_limit_snapshot(self) -> dict[str, int]:
        raise NotImplementedError("owned by MB1")
