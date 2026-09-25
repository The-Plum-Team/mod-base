"""An in-memory GitHub for tests and ``conformance`` (MB1).

``FakeGitHub`` implements the public surface of :class:`mod_base.github.api.GitHubApi` over seeded
state (repository metadata, branches, runs and attempts with ``referenced_workflows``, jobs with
steps, artifacts with ZIP bytes, file contents at refs, commits, trees, blobs, refs and annotated
tags, compare results, plus exact raw GET responses), counts requests exactly like the real
client, and records every mutating call. Every seeder taking ``repository`` defaults to the fake's
own repository; another value seeds a foreign repository (for example ``The-Plum-Team/mod-base``
for pin reachability and tag peeling). It is derived from Quick
Skin ``test_pages_artifact_rotation.FakeApi`` and Block Pops ``test_pages_publication`` fakes.

Served routes (GET unless noted; ``{R}`` is a seeded repository, ``{own}`` the fake's own):

* ``/rate_limit``; ``/repos/{own}`` (``full_name``, ``default_branch``);
* ``/repos/{own}/branches`` (array, sorted by name) and ``/repos/{own}/branches/{name}``;
* ``/repos/{own}/actions/runs`` and ``/actions/workflows/{file or id}/runs`` (filters ``branch``,
  ``head_sha``, ``event``, ``status``; newest first), ``/actions/runs/{id}``,
  ``/actions/runs/{id}/attempts/{n}``, ``/actions/runs/{id}/attempts/{n}/jobs`` and
  ``/actions/runs/{id}/jobs`` (``filter=latest|all``);
* ``/repos/{own}/actions/artifacts`` (``name``), ``/actions/runs/{id}/artifacts`` (``name``),
  ``/actions/artifacts/{id}``, the ``/zip`` download, and ``DELETE /actions/artifacts/{id}``;
* ``POST /repos/{own}/actions/workflows/{file}/dispatches`` (recorded, answers 204);
* ``/repos/{own}/contents/{path}?ref=`` (files over 1 MiB answer ``encoding: none`` and are
  served through ``/git/blobs``, as GitHub does);
* ``/repos/{R}/compare/{base}...{head}``, ``/git/commits/{sha}``, ``/git/trees/{sha}``
  (``recursive``; a seeded commit id lists that commit's root tree, as the API does),
  ``/git/blobs/{oid}``, ``/git/ref/{ref}`` and ``/git/tags/{sha}``.

Listings honour ``per_page`` (default 30, at most 100) and ``page`` like the API. An exact
``add_response`` seed wins over every typed route; anything unseeded answers 404
(:class:`mod_base.github.api.ApiNotFound`), and a parameter a route does not understand answers
422. Every served body is a fresh strict-JSON copy, so callers can never mutate seeded state.

Two seams reproduce GitHub's eventually consistent listings: :meth:`FakeGitHub.skew_listing` serves
the next responses of a listing with a ``total_count`` off by some rows (a sibling job's upload in
flight), and :meth:`FakeGitHub.during_listing` changes the seeded state between two pages of one
read (an upload or deletion landing mid-listing). The consistent-listing retry never really sleeps
on the fake unless a ``sleep`` is given; :attr:`FakeGitHub.sleeps` records every wait it asked for.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import itertools
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from mod_base.github.api import (
    MAX_DOWNLOAD_BYTES,
    ApiError,
    ApiNotFound,
    ReadOnlyViolation,
    RequestBudgetExhausted,
    _positive as _positive_bound,
    _query,
    _validate_path,
    paginate_with,
    rate_limit_counters,
    read_consistently,
)
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, strict_loads

OWNER = "MB1"

#: Files larger than this are omitted by the contents API (``encoding: none``).
CONTENTS_INLINE_LIMIT = 1024 * 1024
RATE_LIMIT = 5000
RATE_LIMIT_RESET = 1_900_000_000
_LIST_PARAMS = frozenset({"per_page", "page"})
_RUN_FILTERS = frozenset({"branch", "head_sha", "event", "status"})
_T = TypeVar("_T")


def _git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()


def _wrapped_base64(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return "".join(encoded[index:index + 60] + "\n" for index in range(0, len(encoded), 60))


def _require(condition: bool, message: str) -> None:
    """Seeding mistakes are programming errors in a test, never evidence."""

    if not condition:
        raise ValueError(message)


def _positive(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0


class FakeGitHub:
    """Duck-typed stand-in for ``GitHubApi``; seed it, then pass it where a client is expected."""

    def __init__(self, *, repository: str, default_branch: str = "master", writable: bool = False,
                 max_requests: int | None = None, sleep: Callable[[float], None] | None = None) -> None:
        self._repository = grammar.require(grammar.REPOSITORY, repository, "repository")
        self._default_branch = grammar.require(grammar.BRANCH, default_branch, "default branch")
        _require(isinstance(writable, bool), "writable must be a boolean")
        _require(max_requests is None or _positive(max_requests), "max_requests must be a positive integer")
        _require(sleep is None or callable(sleep), "sleep must be callable")
        self._writable = writable
        self._max_requests = max_requests
        self._sleep = sleep
        self._sleeps: list[float] = []
        self._skews: dict[str, tuple[int, int]] = {}
        self._page_actions: dict[str, tuple[int, Callable[[], None]]] = {}
        self._request_count = 0
        self._deleted: list[int] = []
        self._mutations: list[tuple[str, str, Any]] = []
        self._branches: dict[str, tuple[str, str]] = {}
        self._runs: dict[int, dict[str, Any]] = {}
        self._attempts: dict[tuple[int, int], dict[str, Any]] = {}
        self._jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._artifacts: dict[int, tuple[dict[str, Any], bytes]] = {}
        self._files: dict[tuple[str, str], bytes] = {}
        self._compares: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._commits: dict[tuple[str, str], dict[str, Any]] = {}
        self._trees: dict[tuple[str, str], tuple[list[dict[str, Any]], bool]] = {}
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._refs: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._tags: dict[tuple[str, str], tuple[str, str]] = {}
        self._responses: dict[tuple[str, frozenset[tuple[str, str]]], Any] = {}
        self._job_ids = itertools.count(9_000_000_001)

    # -- seeding ---------------------------------------------------------------------------------

    def _repo(self, repository: str | None) -> str:
        if repository is None:
            return self._repository
        return grammar.require(grammar.REPOSITORY, repository, "repository")

    def set_branch(self, name: str, commit: str, tree: str) -> None:
        """Seed a live branch head; also seeds ``/git/commits/{commit}`` with ``tree`` when absent."""

        grammar.require(grammar.BRANCH, name, "branch")
        grammar.require_sha1(commit)
        grammar.require_sha1(tree, "tree")
        seeded = self._commits.get((self._repository, commit))
        _require(seeded is None or seeded["tree"] == tree, f"commit {commit} is already seeded with another tree")
        if seeded is None:
            self._commits[(self._repository, commit)] = {"tree": tree, "parents": []}
        self._branches[name] = (commit, tree)

    def add_run(self, run: Mapping[str, Any], *, attempts: list[Mapping[str, Any]] | None = None) -> None:
        """Seed a workflow run as the API reports its latest attempt; ``attempts`` are its earlier
        attempts (served only through ``/attempts/{n}``)."""

        latest = copy.deepcopy(dict(run))
        _require(_positive(latest.get("id")) and _positive(latest.get("run_attempt")),
                 "a seeded run needs positive id and run_attempt")
        _require(isinstance(latest.get("path"), str) and isinstance(latest.get("created_at"), str),
                 "a seeded run needs its workflow path and created_at")
        run_id, newest = latest["id"], latest["run_attempt"]
        for key in [key for key in self._attempts if key[0] == run_id]:
            del self._attempts[key]
        self._runs[run_id] = latest
        self._attempts[(run_id, newest)] = latest
        for attempt in attempts or []:
            record = copy.deepcopy(dict(attempt))
            _require(record.get("id") == run_id and _positive(record.get("run_attempt")),
                     "every seeded attempt must carry the run's id and a positive run_attempt")
            _require(record["run_attempt"] < newest, "the run itself is its newest attempt")
            _require((run_id, record["run_attempt"]) not in self._attempts, "attempts must be unique")
            self._attempts[(run_id, record["run_attempt"])] = record

    def add_jobs(self, run_id: int, run_attempt: int, jobs: list[Mapping[str, Any]]) -> None:
        """Seed the jobs of one attempt; a job missing ``id``, ``run_id`` or ``run_attempt`` gets
        a unique id and this attempt's identity (explicit values are kept, hostile or not)."""

        _require(_positive(run_id) and _positive(run_attempt), "jobs need a positive run id and attempt")
        seeded = []
        for job in jobs:
            record = copy.deepcopy(dict(job))
            record.setdefault("id", next(self._job_ids))
            record.setdefault("run_id", run_id)
            record.setdefault("run_attempt", run_attempt)
            seeded.append(record)
        self._jobs[(run_id, run_attempt)] = seeded

    def add_artifact(self, record: Mapping[str, Any], archive: bytes) -> None:
        """Seed (or replace) an artifact and its ZIP bytes. Absent ``size_in_bytes``/``digest``
        are derived from ``archive`` and absent ``expired`` is ``False``; explicit values are kept
        verbatim so tests can seed metadata that disagrees with the bytes."""

        _require(isinstance(archive, (bytes, bytearray)), "artifact archive must be bytes")
        stored = copy.deepcopy(dict(record))
        _require(_positive(stored.get("id")), "a seeded artifact needs a positive id")
        _require(isinstance(stored.get("workflow_run"), Mapping), "a seeded artifact needs its workflow_run")
        stored.setdefault("size_in_bytes", len(archive))
        stored.setdefault("digest", "sha256:" + hashlib.sha256(archive).hexdigest())
        stored.setdefault("expired", False)
        self._artifacts[stored["id"]] = (stored, bytes(archive))

    def add_file(self, ref: str, path: str, data: bytes) -> None:
        _require(isinstance(ref, str) and bool(ref), "file ref must be a non-empty string")
        _require(grammar.is_repo_path(path), "file path must be a canonical repository path")
        _require(isinstance(data, (bytes, bytearray)), "file data must be bytes")
        self._files[(ref, path)] = bytes(data)

    def add_compare(self, base: str, head: str, result: Mapping[str, Any], *, repository: str | None = None) -> None:
        _require(isinstance(base, str) and isinstance(head, str) and base and head, "compare refs must be text")
        self._compares[(self._repo(repository), base, head)] = copy.deepcopy(dict(result))

    def add_commit(self, sha: str, tree: str, *, parents: Sequence[str] = (), repository: str | None = None) -> None:
        """Seed ``/git/commits/{sha}`` (``contents.commit_tree``) with its tree and parents."""

        grammar.require_sha1(sha)
        grammar.require_sha1(tree, "tree")
        for parent in parents:
            grammar.require_sha1(parent, "parent")
        self._commits[(self._repo(repository), sha)] = {"tree": tree, "parents": list(parents)}

    def add_tree(self, sha: str, entries: Sequence[Mapping[str, Any]], *, truncated: bool = False,
                 repository: str | None = None) -> None:
        """Seed ``/git/trees/{sha}`` (``contents.tree``, ``tools/verify_action_tree.py``): ``entries``
        are the API's ``{path, mode, type, sha, size?}`` rows; ``truncated`` seeds a truncated
        listing, which consumers must refuse."""

        grammar.require_sha1(sha, "tree")
        _require(isinstance(truncated, bool), "truncated must be a boolean")
        rows = [copy.deepcopy(dict(entry)) for entry in entries]
        self._trees[(self._repo(repository), sha)] = (rows, truncated)

    def add_blob(self, data: bytes, *, oid: str | None = None, repository: str | None = None) -> str:
        """Seed ``/git/blobs/{oid}`` (``contents.blob``) and return its oid: the Git blob id of
        ``data`` unless ``oid`` forces another one (a corrupt object the reader must reject)."""

        _require(isinstance(data, (bytes, bytearray)), "blob data must be bytes")
        identity = _git_blob_id(bytes(data)) if oid is None else grammar.require_sha1(oid, "blob")
        self._blobs[(self._repo(repository), identity)] = bytes(data)
        return identity

    def add_ref(self, ref: str, sha: str, *, annotated_tag_sha: str | None = None,
                repository: str | None = None) -> None:
        """Seed ``/git/ref/{ref}`` (``ref`` like ``tags/v1.0.0`` or ``heads/main``). With
        ``annotated_tag_sha`` the ref points at that tag object, which peels to ``sha`` through
        ``/git/tags/{annotated_tag_sha}`` (``pin.verify``'s tag peel)."""

        _require(isinstance(ref, str) and ref.startswith(("tags/", "heads/"))
                 and grammar.is_match(grammar.BRANCH, ref), "ref must look like tags/<name> or heads/<name>")
        grammar.require_sha1(sha)
        owner = self._repo(repository)
        if annotated_tag_sha is not None:
            grammar.require_sha1(annotated_tag_sha, "tag object")
            self._tags[(owner, annotated_tag_sha)] = (ref.split("/", 1)[1], sha)
        self._refs[(owner, ref)] = (sha, annotated_tag_sha)

    def add_response(self, path: str, payload: Any, *, params: Mapping[str, str | int] | None = None) -> None:
        """Seed the exact JSON body of one GET ``path`` (with exactly ``params``) that no typed seeder
        covers; a request for an unseeded path answers 404 like the API."""

        _validate_path(path)
        _query(params)
        canonical_json(payload)  # must be JSON
        self._responses[(path, self._param_key(params))] = copy.deepcopy(payload)

    def skew_listing(self, path: str, *, responses: int, offset: int = 1) -> None:
        """Serve the next ``responses`` responses of the listing ``path`` (any parameters, every page
        counts) with a ``total_count`` ``offset`` rows off the rows it lists (never below zero), as
        GitHub's eventually consistent listing does while a sibling job uploads: the canary's
        ``total_count 6 disagrees with 5 listed rows``. A later call for ``path`` replaces it."""

        _validate_path(path)
        _require(_positive(responses), "responses must be a positive integer")
        _require(not isinstance(offset, bool) and isinstance(offset, int) and offset != 0,
                 "offset must be a non-zero integer")
        self._skews[path] = (responses, offset)

    def during_listing(self, path: str, action: Callable[[], None], *, after_pages: int = 1) -> None:
        """Run ``action`` (typically :meth:`add_artifact`) once, right after the listing ``path`` (any
        parameters) served its ``after_pages``-th response from now: a concurrent upload or deletion
        landing between two pages of one read. A later call for ``path`` replaces it."""

        _validate_path(path)
        _require(_positive(after_pages), "after_pages must be a positive integer")
        _require(callable(action), "action must be callable")
        self._page_actions[path] = (after_pages, action)

    # -- GitHubApi surface -----------------------------------------------------------------------

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def deleted_artifact_ids(self) -> list[int]:
        return list(self._deleted)

    @property
    def sleeps(self) -> list[float]:
        """Every wait the consistent-listing retry asked for, in order (slept only through ``sleep``)."""

        return list(self._sleeps)

    @property
    def mutations(self) -> list[tuple[str, str, Any]]:
        """Every accepted mutating call, in order: ``(method, path, payload or None)``."""

        return copy.deepcopy(self._mutations)

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        _validate_path(path)
        _query(params)
        self._spend()
        key = (path, self._param_key(params))
        if key in self._responses:
            served = self._serve(self._responses[key])
        else:
            served = self._serve(self._route(path, {name: str(value) for name, value in (params or {}).items()}))
        self._skew(path, served)
        if path in self._page_actions:
            remaining, action = self._page_actions.pop(path)
            if remaining > 1:
                self._page_actions[path] = (remaining - 1, action)
            else:
                action()
        return served

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        return paginate_with(self.get_json, path, field=field, params=params, max_items=max_items, sleep=self._wait)

    def read_listing(self, read: Callable[[], _T]) -> _T:
        return read_consistently(read, sleep=self._wait)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        if not self._writable:
            raise ReadOnlyViolation(f"refusing POST {path!r} on a read-only GitHub client"[:200])
        _validate_path(path)
        if not isinstance(payload, Mapping):
            raise MbError("POST payload must be a JSON object", reason="usage")
        canonical_json(dict(payload))
        self._spend()
        repository, rest = self._split(path, "POST")
        if (repository == self._repository and len(rest) == 4 and rest[:2] == ["actions", "workflows"]
                and rest[3] == "dispatches"):
            self._mutations.append(("POST", path, copy.deepcopy(dict(payload))))
            return None
        raise self._not_found("POST", path)

    def delete(self, path: str) -> None:
        if not self._writable:
            raise ReadOnlyViolation(f"refusing DELETE {path!r} on a read-only GitHub client"[:200])
        _validate_path(path)
        self._spend()
        repository, rest = self._split(path, "DELETE")
        if repository == self._repository and len(rest) == 3 and rest[:2] == ["actions", "artifacts"]:
            artifact_id = self._integer(rest[2])
            if artifact_id in self._artifacts:
                del self._artifacts[artifact_id]
                self._deleted.append(artifact_id)
                self._mutations.append(("DELETE", path, None))
                return
        raise self._not_found("DELETE", path)

    def download(self, path: str, *, max_bytes: int) -> bytes:
        _validate_path(path)
        _positive_bound(max_bytes, "max_bytes", MAX_DOWNLOAD_BYTES)
        self._spend()  # the API request answering with the redirect
        repository, rest = self._split(path, "GET")
        if not (repository == self._repository and len(rest) == 4 and rest[:2] == ["actions", "artifacts"]
                and rest[3] == "zip" and self._integer(rest[2]) in self._artifacts):
            raise self._not_found("GET", path)
        record, archive = self._artifacts[self._integer(rest[2])]
        if record.get("expired") is not False:
            raise ApiError(f"GitHub API GET {path} failed: HTTP 410: artifact has expired", status=410, method="GET",
                           path=path)
        self._spend()  # the credential-free storage request
        if len(archive) > max_bytes:
            raise ApiError(f"artifact download for {path} exceeds its {max_bytes}-byte bound", status=200,
                           method="GET", path=path)
        return archive

    def rate_limit_snapshot(self) -> dict[str, int]:
        return rate_limit_counters(self.get_json("/rate_limit"))

    # -- request plumbing ------------------------------------------------------------------------

    def _wait(self, seconds: float) -> None:
        self._sleeps.append(seconds)
        if self._sleep is not None:
            self._sleep(seconds)

    def _skew(self, path: str, served: Any) -> None:
        """Apply a pending :meth:`skew_listing` of ``path`` to the listing body ``served``."""

        count = served.get("total_count") if isinstance(served, dict) else None
        if path not in self._skews or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return
        remaining, offset = self._skews.pop(path)
        if remaining > 1:
            self._skews[path] = (remaining - 1, offset)
        served["total_count"] = max(served["total_count"] + offset, 0)

    def _spend(self) -> None:
        if self._max_requests is not None and self._request_count >= self._max_requests:
            raise RequestBudgetExhausted(f"the client's budget of {self._max_requests} GitHub requests is spent")
        self._request_count += 1

    @staticmethod
    def _param_key(params: Mapping[str, str | int] | None) -> frozenset[tuple[str, str]]:
        return frozenset((name, str(value)) for name, value in (params or {}).items())

    @staticmethod
    def _serve(value: Any) -> Any:
        return strict_loads(canonical_json(value), label="fake GitHub response", max_bytes=1 << 30)

    @staticmethod
    def _not_found(method: str, path: str) -> ApiNotFound:
        return ApiNotFound(f"GitHub API {method} {path} failed: HTTP 404: Not Found", status=404, method=method,
                           path=path)

    @staticmethod
    def _integer(text: str) -> int | None:
        return int(text) if grammar.POSITIVE_DECIMAL.fullmatch(text) else None

    def _split(self, path: str, method: str) -> tuple[str, list[str]]:
        segments = path.split("/")[1:]
        if len(segments) < 3 or segments[0] != "repos":
            raise self._not_found(method, path)
        return f"{segments[1]}/{segments[2]}", segments[3:]

    def _accept(self, path: str, params: dict[str, str], allowed: frozenset[str]) -> None:
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ApiError(f"GitHub API GET {path} failed: HTTP 422: the fake does not support {unknown}",
                           status=422, method="GET", path=path)

    def _page(self, path: str, rows: list[dict[str, Any]], params: dict[str, str]) -> list[dict[str, Any]]:
        per_page = self._integer(params.get("per_page", "30"))
        page = self._integer(params.get("page", "1"))
        if per_page is None or page is None or per_page > 100:
            raise ApiError(f"GitHub API GET {path} failed: HTTP 422: invalid pagination", status=422, method="GET",
                           path=path)
        return rows[(page - 1) * per_page: page * per_page]

    def _route(self, path: str, params: dict[str, str]) -> Any:
        if path == "/rate_limit":
            self._accept(path, params, frozenset())
            core = {"limit": RATE_LIMIT, "used": self._request_count,
                    "remaining": max(RATE_LIMIT - self._request_count, 0), "reset": RATE_LIMIT_RESET}
            return {"resources": {"core": core}, "rate": core}
        repository, rest = self._split(path, "GET")
        if repository == self._repository:
            value = self._own_route(path, rest, params)
            if value is not None:
                return value
        value = self._git_route(path, repository, rest, params)
        if value is not None:
            return value
        raise self._not_found("GET", path)

    def _own_route(self, path: str, rest: list[str], params: dict[str, str]) -> Any:
        if not rest:
            self._accept(path, params, frozenset())
            return {"full_name": self._repository, "default_branch": self._default_branch, "private": False,
                    "visibility": "public"}
        if rest[0] == "branches":
            return self._branch_route(path, rest[1:], params)
        if rest[0] == "contents" and len(rest) > 1:
            self._accept(path, params, frozenset({"ref"}))
            return self._contents(urllib.parse.unquote("/".join(rest[1:])), params.get("ref", self._default_branch))
        if rest[0] == "actions" and len(rest) > 1:
            if rest[1] == "runs" or rest[1] == "workflows":
                return self._run_route(path, rest[1:], params)
            if rest[1] == "artifacts":
                return self._artifact_route(path, rest[2:], params)
        return None

    def _branch_route(self, path: str, rest: list[str], params: dict[str, str]) -> Any:
        if not rest:
            self._accept(path, params, _LIST_PARAMS)
            rows = [{"name": name, "commit": {"sha": commit}, "protected": False}
                    for name, (commit, _) in sorted(self._branches.items())]
            return self._page(path, rows, params)
        self._accept(path, params, frozenset())
        name = urllib.parse.unquote("/".join(rest))
        if name not in self._branches:
            return None
        commit, tree = self._branches[name]
        return {"name": name, "commit": {"sha": commit, "commit": {"tree": {"sha": tree}}}, "protected": False}

    def _listing(self, path: str, runs: list[dict[str, Any]], params: dict[str, str]) -> dict[str, Any]:
        self._accept(path, params, _LIST_PARAMS | _RUN_FILTERS)
        selected = [
            run for run in runs
            if params.get("branch", run.get("head_branch")) == run.get("head_branch")
            and params.get("head_sha", run.get("head_sha")) == run.get("head_sha")
            and params.get("event", run.get("event")) == run.get("event")
            and ("status" not in params or params["status"] in (run.get("status"), run.get("conclusion")))
        ]
        selected.sort(key=lambda run: (str(run.get("created_at")), run["id"]), reverse=True)
        return {"total_count": len(selected), "workflow_runs": self._page(path, selected, params)}

    def _run_route(self, path: str, rest: list[str], params: dict[str, str]) -> Any:
        if rest[0] == "workflows":
            if len(rest) != 3 or rest[2] != "runs":
                return None
            workflow = rest[1]
            runs = [run for run in self._runs.values()
                    if str(run.get("path", "")).rsplit("/", 1)[-1] == workflow or str(run.get("workflow_id")) == workflow]
            return self._listing(path, runs, params)
        if len(rest) == 1:
            return self._listing(path, list(self._runs.values()), params)
        run_id = self._integer(rest[1])
        if run_id is None:
            return None
        if len(rest) == 2:
            self._accept(path, params, frozenset())
            return self._runs.get(run_id)
        if rest[2] == "attempts" and len(rest) in {4, 5}:
            attempt = self._integer(rest[3])
            if attempt is None or (run_id, attempt) not in self._attempts and (run_id, attempt) not in self._jobs:
                return None
            if len(rest) == 4:
                self._accept(path, params, frozenset())
                return self._attempts.get((run_id, attempt))
            if rest[4] != "jobs":
                return None
            self._accept(path, params, _LIST_PARAMS)
            jobs = self._jobs.get((run_id, attempt), [])
            return {"total_count": len(jobs), "jobs": self._page(path, jobs, params)}
        if rest[2:] == ["jobs"] and run_id in self._runs:
            self._accept(path, params, _LIST_PARAMS | {"filter"})
            if params.get("filter", "latest") == "all":
                jobs = [job for (owner, _), rows in sorted(self._jobs.items()) if owner == run_id for job in rows]
            elif params.get("filter", "latest") == "latest":
                jobs = self._jobs.get((run_id, self._runs[run_id]["run_attempt"]), [])
            else:
                raise ApiError(f"GitHub API GET {path} failed: HTTP 422: invalid filter", status=422, method="GET",
                               path=path)
            return {"total_count": len(jobs), "jobs": self._page(path, jobs, params)}
        if rest[2:] == ["artifacts"]:
            return self._artifact_listing(path, params, run_id=run_id)
        return None

    def _artifact_listing(self, path: str, params: dict[str, str], *, run_id: int | None = None) -> dict[str, Any]:
        self._accept(path, params, _LIST_PARAMS | {"name"})
        rows = [record for record, _ in self._artifacts.values()
                if ("name" not in params or record.get("name") == params["name"])
                and (run_id is None or record["workflow_run"].get("id") == run_id)]
        rows.sort(key=lambda record: (str(record.get("created_at")), record["id"]), reverse=True)
        return {"total_count": len(rows), "artifacts": self._page(path, rows, params)}

    def _artifact_route(self, path: str, rest: list[str], params: dict[str, str]) -> Any:
        if not rest:
            return self._artifact_listing(path, params)
        artifact_id = self._integer(rest[0])
        if len(rest) == 1 and artifact_id in self._artifacts:
            self._accept(path, params, frozenset())
            return self._artifacts[artifact_id][0]
        return None

    def _contents(self, file_path: str, ref: str) -> Any:
        data = self._files.get((ref, file_path))
        if data is None:
            return None
        oid = _git_blob_id(data)
        record = {"type": "file", "path": file_path, "name": file_path.rsplit("/", 1)[-1], "size": len(data),
                  "sha": oid, "encoding": "base64", "content": _wrapped_base64(data)}
        if len(data) > CONTENTS_INLINE_LIMIT:
            self._blobs.setdefault((self._repository, oid), data)
            record.update(encoding="none", content="")
        return record

    def _git_route(self, path: str, repository: str, rest: list[str], params: dict[str, str]) -> Any:
        if len(rest) >= 2 and rest[0] == "compare":
            self._accept(path, params, _LIST_PARAMS)  # the API pages the compared commits
            base, separator, head = urllib.parse.unquote("/".join(rest[1:])).partition("...")
            return self._compares.get((repository, base, head)) if separator else None
        if len(rest) < 3 or rest[0] != "git":
            return None
        kind, identity = rest[1], "/".join(rest[2:])
        if kind == "trees" and len(rest) == 3:
            self._accept(path, params, frozenset({"recursive"}))
            commit = self._commits.get((repository, identity))
            tree_sha = commit["tree"] if (repository, identity) not in self._trees and commit is not None else identity
            seeded = self._trees.get((repository, tree_sha))
            if seeded is None:
                return None
            entries, truncated = seeded
            if "recursive" not in params:
                entries = [entry for entry in entries if "/" not in str(entry.get("path", ""))]
            return {"sha": tree_sha, "tree": entries, "truncated": truncated}
        self._accept(path, params, frozenset())
        if kind == "commits" and len(rest) == 3 and (repository, identity) in self._commits:
            commit = self._commits[(repository, identity)]
            return {"sha": identity, "tree": {"sha": commit["tree"]},
                    "parents": [{"sha": parent} for parent in commit["parents"]]}
        if kind == "blobs" and len(rest) == 3 and (repository, identity) in self._blobs:
            data = self._blobs[(repository, identity)]
            return {"sha": identity, "size": len(data), "encoding": "base64", "content": _wrapped_base64(data)}
        if kind == "ref":
            ref = urllib.parse.unquote(identity)
            if (repository, ref) not in self._refs:
                return None
            target, tag_object = self._refs[(repository, ref)]
            obj = {"sha": tag_object, "type": "tag"} if tag_object is not None else {"sha": target, "type": "commit"}
            return {"ref": f"refs/{ref}", "object": obj}
        if kind == "tags" and len(rest) == 3 and (repository, identity) in self._tags:
            name, target = self._tags[(repository, identity)]
            return {"sha": identity, "tag": name, "object": {"sha": target, "type": "commit"}}
        return None
