"""Workflow-run reads and exact run validation (MB1).

Block Pops ``_validate_run``/``_historical_run``/``_run_order`` plus Quick Skin owner checks.
Attempts are always read through ``/actions/runs/{id}/attempts/{n}`` when an attempt matters.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any

from mod_base import KIT_REPOSITORY
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi, InconsistentListing
from mod_base.model import documents, grammar, limits
from mod_base.model.validators import DocumentError

OWNER = "MB1"

#: ``status`` filter values the workflow-runs listing accepts (run statuses and conclusions).
RUN_STATUS_FILTERS = frozenset({
    "completed", "action_required", "cancelled", "failure", "neutral", "skipped", "stale", "success",
    "timed_out", "in_progress", "queued", "requested", "waiting", "pending",
})
MAX_REFERENCED_WORKFLOWS = 100
_REFERENCED_PATH = re.compile(r"^(?P<prefix>.+/\.github/workflows/)(?P<file>[A-Za-z0-9._-]{1,100}\.ya?ml)@(?P<ref>[^@]+)$")


def _fail(message: str, reason: str = "run-provenance") -> MbError:
    return MbError(message, reason=reason)


def _positive(value: Any, label: str, maximum: int = limits.MAX_RUN_ID) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise _fail(f"{label} must be a positive integer no larger than {maximum}")
    return value


def get_run(api: GitHubApi, run_id: int) -> dict[str, Any]:
    """``GET /repos/{repo}/actions/runs/{run_id}`` (an object or :class:`ApiError`)."""

    _positive(run_id, "run id")
    run = api.get_json(f"/repos/{api.repository}/actions/runs/{run_id}")
    if not isinstance(run, dict) or run.get("id") != run_id or isinstance(run.get("id"), bool):
        raise _fail(f"workflow run {run_id} response is malformed or names another run")
    return run


def get_run_attempt(api: GitHubApi, run_id: int, run_attempt: int) -> dict[str, Any]:
    """The historical attempt endpoint; its ``run_attempt`` must equal ``run_attempt``."""

    _positive(run_id, "run id")
    _positive(run_attempt, "run attempt", limits.MAX_RUN_ATTEMPT)
    run = api.get_json(f"/repos/{api.repository}/actions/runs/{run_id}/attempts/{run_attempt}")
    if not isinstance(run, dict):
        raise _fail(f"workflow run {run_id} attempt {run_attempt} response is malformed")
    for key in ("id", "run_attempt", "workflow_id"):
        _positive(run.get(key), f"workflow run attempt {key}")
    if run["id"] != run_id or run["run_attempt"] != run_attempt:
        raise _fail(f"workflow run {run_id} attempt {run_attempt} response is stale or names another attempt")
    return run


def validate_run(run: Mapping[str, Any], *, repository: str, workflow_path: str, events: Collection[str],
                 head_branch: str | None = None, head_sha: str | None = None, workflow_id: int | None = None,
                 require_success: bool = True, display_title: str | None = None) -> None:
    """Require exact provenance: ``path``, ``event`` in ``events``, ``head_repository.full_name``,
    and (when given) ``head_branch``, ``head_sha``, ``workflow_id``, ``display_title``; with
    ``require_success`` also ``status == "completed"`` and ``conclusion == "success"``.
    Raises :class:`mod_base.errors.MbError` on any difference."""

    if not isinstance(run, Mapping):
        raise _fail("workflow run must be an object")
    grammar.require(grammar.REPOSITORY, repository, "repository")
    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "workflow path")
    if isinstance(events, str) or not events or any(not isinstance(event, str) for event in events):
        raise _fail("accepted events must be a non-empty collection of names", reason="usage")
    label = f"workflow run {run.get('id')!r}"[:80]
    head_repository = run.get("head_repository")
    checks = (
        ("path", run.get("path") == workflow_path),
        ("event", isinstance(run.get("event"), str) and run.get("event") in set(events)),
        ("head repository", isinstance(head_repository, Mapping) and head_repository.get("full_name") == repository),
        ("head branch", head_branch is None or run.get("head_branch") == head_branch),
        ("head SHA", head_sha is None or (grammar.is_match(grammar.SHA1, head_sha) and run.get("head_sha") == head_sha)),
        ("workflow id", workflow_id is None or (not isinstance(run.get("workflow_id"), bool)
                                                and run.get("workflow_id") == workflow_id)),
        ("display title", display_title is None or run.get("display_title") == display_title),
        ("status", not require_success or run.get("status") == "completed"),
        ("conclusion", not require_success or run.get("conclusion") == "success"),
    )
    for name, passed in checks:
        if not passed:
            raise _fail(f"{label} failed provenance validation: {name} differs")


def run_order(run: Mapping[str, Any]) -> tuple[datetime, int, int]:
    """``(created_at, id, run_attempt)`` after strict shape validation (a total dispatch order)."""

    if not isinstance(run, Mapping):
        raise _fail("workflow run must be an object")
    run_id = _positive(run.get("id"), "workflow run id")
    attempt = _positive(run.get("run_attempt"), "workflow run attempt", limits.MAX_RUN_ATTEMPT)
    try:
        created = grammar.parse_timestamp(run.get("created_at"), "workflow run created_at")
    except MbError as exc:
        raise _fail(str(exc)) from exc
    return created, run_id, attempt


def referenced_kit_sha(run: Mapping[str, Any], *, kit_repository: str = KIT_REPOSITORY) -> str:
    """The single kit SHA a run resolved: every ``referenced_workflows[]`` entry whose ``path``
    starts with ``<kit_repository>/.github/workflows/`` must end in ``@<sha>`` equal to its
    ``sha``, and exactly one distinct 40-hex SHA must result (SPEC §1.2 step 3).

    GitHub resolves repository names case-insensitively, so an entry naming the kit repository in
    another letter case is refused rather than ignored."""

    grammar.require(grammar.REPOSITORY, kit_repository, "kit repository")
    if not isinstance(run, Mapping):
        raise _fail("workflow run must be an object", reason="kit-binding")
    entries = run.get("referenced_workflows")
    if not isinstance(entries, list) or len(entries) > MAX_REFERENCED_WORKFLOWS:
        raise _fail("workflow run has no bounded referenced_workflows list", reason="kit-binding")
    prefix = f"{kit_repository}/.github/workflows/"
    shas: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise _fail(f"referenced_workflows[{index}] is malformed", reason="kit-binding")
        path = entry["path"]
        if not path.casefold().startswith(prefix.casefold()):
            continue
        if not path.startswith(prefix):
            raise _fail(f"referenced_workflows[{index}] names the kit repository in another case",
                        reason="kit-binding")
        sha = entry.get("sha")
        match = _REFERENCED_PATH.fullmatch(path)
        if (match is None or match.group("prefix") != prefix or not grammar.is_match(grammar.SHA1, sha)
                or match.group("ref") != sha):
            raise _fail(f"referenced_workflows[{index}] is not pinned to its own 40-hex kit SHA",
                        reason="kit-binding")
        if "ref" in entry and entry["ref"] is not None and not isinstance(entry["ref"], str):
            raise _fail(f"referenced_workflows[{index}].ref is malformed", reason="kit-binding")
        shas.add(sha)
    if len(shas) != 1:
        raise _fail(f"workflow run references {len(shas)} distinct kit SHAs; exactly one is required",
                    reason="kit-binding")
    return shas.pop()


def workflow_runs(api: GitHubApi, workflow_path: str, *, branch: str | None = None, head_sha: str | None = None,
                  event: str | None = None, status: str | None = None,
                  max_items: int = 1000) -> list[dict[str, Any]]:
    """List runs of ``workflow_path`` (by file name) newest first with the given filters; the
    response ``total_count`` must equal the listed rows when it is at most ``max_items``.

    Beyond ``max_items`` runs the newest ``max_items`` are returned, and a filtered search returns
    at most its ``limits.MAX_FILTERED_RUNS_LISTED`` newest (GitHub lists no more): such a read must
    list exactly that many rows, ending at its last full page. Otherwise only a short page ends the
    read (a full page reaching ``total_count`` is confirmed by the next), and the rows must equal
    ``total_count``. Every row is re-checked against the workflow path and each filter. A snapshot
    whose ``total_count`` changes between pages or disagrees with its rows, or that repeats a run (a
    page race while runs start or settle), is read again from page 1 through ``api.read_listing``
    and fails closed only after ``limits.LISTING_READ_ATTEMPTS`` such reads."""

    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "workflow path")
    _positive(max_items, "max_items", 100_000)
    params: dict[str, str | int] = {}
    if branch is not None:
        params["branch"] = grammar.require(grammar.BRANCH, branch, "branch")
    if head_sha is not None:
        params["head_sha"] = grammar.require_sha1(head_sha, "head SHA")
    if event is not None:
        params["event"] = grammar.require(grammar.EVENT, event, "event")
    if status is not None:
        if status not in RUN_STATUS_FILTERS:
            raise _fail(f"unknown run status filter {status!r}"[:80], reason="usage")
        params["status"] = status
    filename = workflow_path.rsplit("/", 1)[1]
    path = f"/repos/{api.repository}/actions/workflows/{filename}/runs"

    def inconsistent(message: str) -> InconsistentListing:
        return InconsistentListing(message, status=200, method="GET", path=path)

    def read() -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(1, max_items // 100 + 3):
            payload = api.get_json(path, params={**params, "per_page": 100, "page": page})
            rows = payload.get("workflow_runs") if isinstance(payload, dict) else None
            count = payload.get("total_count") if isinstance(payload, dict) else None
            if (not isinstance(rows, list) or len(rows) > 100 or any(not isinstance(row, dict) for row in rows)
                    or isinstance(count, bool) or not isinstance(count, int) or count < 0):
                raise _fail("workflow-runs listing is malformed")
            if total is not None and count != total:
                raise inconsistent("workflow-runs total_count changed between pages")
            total = count
            runs.extend(rows)
            expected = min(total, max_items, limits.MAX_FILTERED_RUNS_LISTED if params else total)
            if expected < total:
                # Only the newest ``expected`` runs are read: a full page reaching them ends the read,
                # and a short page before them is a snapshot missing runs.
                if len(runs) >= expected:
                    break
                if len(rows) < 100:
                    raise inconsistent(f"workflow-runs total_count {total} disagrees with {len(runs)} listed runs")
            elif len(runs) > total or len(rows) < 100:
                # The whole listing: only a short page ends it, so a lagging count hides no run.
                if len(runs) != total:
                    raise inconsistent(f"workflow-runs total_count {total} disagrees with {len(runs)} listed runs")
                break
        else:
            raise _fail("workflow-runs listing did not end within its page bound")
        runs = runs[:max_items]
        # The listing has one row per run (its latest attempt), so a repeated id is a page race.
        seen: set[int] = set()
        for run in runs:
            _, run_id, _ = run_order(run)
            if run_id in seen:
                raise inconsistent("workflow-runs listing repeats a run")
            seen.add(run_id)
            if (run.get("path") != workflow_path
                    or (branch is not None and run.get("head_branch") != branch)
                    or (head_sha is not None and run.get("head_sha") != head_sha)
                    or (event is not None and run.get("event") != event)
                    or (status is not None and run.get("status") != status and run.get("conclusion") != status)):
                raise _fail(f"workflow-runs listing returned run {run_id} outside its filters")
        return sorted(runs, key=run_order, reverse=True)

    return api.read_listing(read)


def wait_for_completion(api: GitHubApi, run_id: int, *, attempts: int = limits.RUN_POLL_ATTEMPTS,
                        interval: float = limits.RUN_POLL_INTERVAL_SECONDS,
                        sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Poll a run until ``status == "completed"`` (at most ``attempts`` reads); raise otherwise."""

    _positive(attempts, "poll attempts", 1000)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0 <= interval <= 60:
        raise _fail("poll interval must be between 0 and 60 seconds", reason="usage")
    for attempt in range(attempts):
        run = get_run(api, run_id)
        if run.get("status") == "completed":
            return run
        if attempt + 1 < attempts:
            sleep(interval)
    raise _fail(f"workflow run {run_id} did not complete within {attempts} observations", reason="run-incomplete")


def run_record(run: Mapping[str, Any], claim: Mapping[str, Any], *,
               require_controller_head: bool = True) -> dict[str, Any]:
    """Build the ``RunRecord`` (SPEC §3.0) for an authenticated API ``run`` and its ``RunClaim``:
    the claim's run id/attempt/workflow path must equal the run's; ``head_sha``, ``event``,
    ``created_at``, ``conclusion`` (``success``) and ``display_title`` come from the run. The
    result validates as ``documents.run_record``.

    With ``require_controller_head`` (the default) the run's ``head_sha`` must equal the claim's
    ``controller_sha``: a handoff or family producer run (its own controller, see
    ``documents.own_run_record``) and a ``none``/``attested`` tested run. Pass ``False`` only for
    a ``delegated`` tested run, whose API head legitimately differs (Quick Skin PR reuse tests a
    merge commit that is not the PR run's head); that reuse is proven by the adapter's
    ``authenticate_extensions`` instead (SPEC §4.8 step 4).

    The controller head check also binds ``head_branch`` to the claim's ``controller_branch``."""

    if not isinstance(run, Mapping):
        raise _fail("workflow run must be an object")
    try:
        documents.RUN_CLAIM(claim, "$claim")
    except DocumentError as exc:
        raise _fail(f"run claim is invalid: {exc}") from exc
    _, run_id, attempt = run_order(run)
    if (run_id, attempt) != (claim["run_id"], claim["run_attempt"]):
        raise _fail("run claim names another run or attempt")
    if run.get("path") != claim["workflow_path"]:
        raise _fail("run claim names another workflow")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise _fail(f"workflow run {run_id} attempt {attempt} is not completed/success")
    if require_controller_head and (run.get("head_sha") != claim["controller_sha"]
                                    or run.get("head_branch") != claim["controller_branch"]):
        raise _fail(f"workflow run {run_id} head is not the claimed controller")
    record: dict[str, Any] = {
        **{key: claim[key] for key in documents.RUN_CLAIM_FIELDS},
        "event": run.get("event"),
        "created_at": run.get("created_at"),
        "conclusion": "success",
        "head_sha": run.get("head_sha"),
    }
    if run.get("display_title") is not None:
        record["display_title"] = run["display_title"]
    try:
        return documents.run_record(record, "$run_record")
    except DocumentError as exc:
        raise _fail(f"workflow run {run_id} cannot be recorded: {exc}") from exc
