"""Newest pull-request Build selection before success, never a latest-success query (MB11).

A producer run of a pull-request generation is found by the keys GitHub records it under: the
managed Build caller's workflow file, the ``pull_request_target`` event and the pull request's
head commit, head branch and source repository. The listing carries no status filter. The newest
run by ``(created_at, id)`` and its latest attempt are chosen before any result is read:

* no run, or a run that has not completed: nothing yet, the wait continues;
* a completed run whose exact graph is the draft deferral: not a producer, the wait continues;
* a completed successful run with the exact full graph: its complete bundle is described;
* anything else (a failed or cancelled run, another graph, another controller or kit pin, a
  missing or expired bundle): a rejection. An older run is never considered.

There is no run-title contract. The tested merge is bound by the records inside the artifacts,
which the consumer compares with its own admitted plan.
"""

from __future__ import annotations

import copy
import functools
import math
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.graph import require_graph, run_graph, sealed_upload, upload_job_name
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import validate_descriptor
from mod_base.build_ci.transport import (_admit_source, _artifact_state, _authenticate_artifacts, _authenticate_run,
                                         _descriptor, _download_build, _merged, _plan, _run)
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import job_graph
from mod_base.github.runs import run_order, workflow_runs
from mod_base.model import grammar, limits
from mod_base.model.validators import check
from mod_base.workflow import CI_CALLER_WORKFLOWS, find_job

_PENDING = ("queued", "in_progress", "waiting", "requested", "pending")


def _pr_plan(plan: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(plan)
    check(plan["identity"]["pr_number"] > 0, "$.identity.pr_number", "selection requires an original PR plan")
    return plan


def _newest(reads: CommandReads, plan: dict[str, Any]) -> dict[str, Any] | None:
    """The newest Build run GitHub lists for this pull-request head, whatever its result."""

    head_sha, head_branch, head_repository = run_head(plan["identity"])
    runs = [run for run in workflow_runs(reads, CI_CALLER_WORKFLOWS["build"], branch=head_branch, head_sha=head_sha,
                                         event="pull_request_target", max_items=limits.MAX_CI_BUILD_RUNS)
            if isinstance(run.get("head_repository"), dict)
            and run["head_repository"].get("full_name") == head_repository]
    if not runs:
        return None
    run = max(runs, key=lambda candidate: run_order(candidate)[:2])
    _, run_id, attempt = run_order(run)
    return {"id": run_id, "run_attempt": attempt, "created_at": run["created_at"],
            "status": run.get("status"), "conclusion": run.get("conclusion")}


def _pending(run: dict[str, Any]) -> None:
    check(run["status"] in _PENDING and run["conclusion"] is None, "$.run.status", "malformed pending Build state")


def _select(reads: CommandReads, watch: Watch, plan: dict[str, Any]) -> dict[str, Any] | None:
    """One observation of the newest Build run: its complete bundle's descriptor, or ``None`` while
    there is nothing to consume yet. Everything mutable it reads is left in ``watch``."""

    identity = plan["identity"]
    newest = watch.read(("newest Build run",), functools.partial(_newest, reads, plan))
    if newest is None:
        return None
    if newest["status"] != "completed":
        _pending(newest)
        return None
    run = _run(reads, watch, newest["id"])
    check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
          and run["run_attempt"] >= newest["run_attempt"], "$.run", "run listing and run disagree")
    if run["status"] != "completed":
        _pending(run)
        return None
    check(run["conclusion"] == "success", "$.run.conclusion", "newest exact Build failed or was cancelled")
    attempt = run["run_attempt"]
    jobs = reads.attempt_jobs(newest["id"], attempt)
    if job_graph(jobs) == run_graph("build", "deferred").jobs(plan):
        return None
    path = CI_CALLER_WORKFLOWS["build"]
    producer = {"run_id": newest["id"], "run_attempt": attempt, "workflow_path": path,
                "workflow_ref": grammar.workflow_ref(identity["repository"], path, identity["base_branch"]),
                "api_head_sha": run_head(identity)[0], "event": "pull_request_target",
                "graph_sha256": require_graph(jobs, plan=plan, producer="build", mode="full", run_attempt=attempt)}
    _authenticate_run(reads, watch, producer, plan, mode="full", complete=True)
    started, completed = sealed_upload(find_job(jobs, upload_job_name("build", "build", None), run_attempt=attempt))
    producer["upload_window"] = {"started_at": started, "completed_at": completed}
    name = grammar.ci_artifact_name("build", producer["run_id"], attempt)
    rows = reads.paginate(f"/repos/{reads.repository}/actions/runs/{producer['run_id']}/artifacts",
                          field="artifacts", params={"name": name}, max_items=limits.MAX_ARTIFACTS_PER_NAME)
    check(len(rows) == 1, "$.artifact", "newest exact Build lacks one unique complete bundle; rerun Build")
    state = _artifact_state(rows[0])
    check(state["name"] == name, "$.artifact.name", "artifact listing ignored the exact name filter")
    descriptor = {"identity": copy.deepcopy(identity), "plan_sha256": plan["plan_sha256"],
                  "profile": plan["profile"], "producer": producer,
                  "artifact": {key: state[key] for key in ("id", "name", "digest", "size", "created_at",
                                                           "expires_at")}}
    validate_descriptor(descriptor)
    _authenticate_artifacts(reads, watch, [descriptor], identity)
    return descriptor


def select_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any]) -> dict[str, Any] | None:
    """Admit the live pull request, then describe the complete bundle of its newest Build run.

    None means absent, pending or deferred, never permission to compile or succeed. A failed or
    cancelled newest run, another graph, a missing bundle or an API failure is fatal, with no old
    fallback. This is one observation: a consumer repeats it immediately before its effect
    (:func:`revalidate_latest_pr_build`). Native byte validity and status authority remain
    additional required phases; a non-PR subject is not selected here.
    """

    plan = _pr_plan(plan)
    reads, watch = CommandReads.of(api), Watch()
    _admit_source(reads, watch, plan["identity"])
    return _select(reads, watch, plan)


def select_latest_merged_pr_build(api: GitHubApi, *, plan: dict[str, Any], controller_sha: str,
                                  merged_sha: str) -> dict[str, Any] | None:
    """Describe the newest original PR Build after actual historical source admission.

    The original plan and the current controller/final merge require independent admission. The
    original runs stay recorded under the pull request's head. None is absence, pending or a
    deferral, not reuse approval. Both coherent historical gates, native bytes, original/current
    policy and pin and authority remain mandatory. No compiler, fallback, upload or status is admitted.
    """

    plan = _pr_plan(plan)
    merged = _merged(plan, controller_sha, merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    _admit_source(reads, watch, plan["identity"], merged)
    return _select(reads, watch, plan)


def revalidate_latest_merged_pr_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                                      controller_sha: str, merged_sha: str) -> None:
    """Repeat the historical newest-run observation around independent consumption.

    The selected descriptor must still describe the newest original Build. This proves neither
    native payloads nor the packaged partner nor final reuse authority.
    """

    descriptor = _descriptor(descriptor)
    latest = select_latest_merged_pr_build(api, plan=plan, controller_sha=controller_sha, merged_sha=merged_sha)
    check(latest is not None and latest == descriptor, "$.descriptor",
          "historical Build is no longer the newest exact available producer")


def _wait(reads: CommandReads, plan: dict[str, Any], monotonic: Callable[[], float],
          sleep: Callable[[float], None]) -> tuple[dict[str, Any], Watch]:
    """Poll the newest Build run until its bundle can be described; return it with what was read."""

    check(callable(monotonic) and callable(sleep), "$.wait", "protected clock/sleep must be callable")
    previous: float | None = None

    def now() -> float:
        nonlocal previous
        value = monotonic()
        check((type(value) is int or (type(value) is float and math.isfinite(value)))
              and (previous is None or value >= previous), "$.wait.clock", "invalid monotonic clock")
        previous = value
        return value

    deadline = now() + limits.CI_BUILD_WAIT_SECONDS
    admitted = False
    for _ in range(limits.MAX_CI_BUILD_POLLS):
        if now() >= deadline:
            break
        watch = Watch()
        if not admitted:
            _admit_source(reads, watch, plan["identity"])
            admitted = True
        selected = _select(reads, watch, plan)
        remaining = deadline - now()
        if remaining <= 0:
            break
        if selected is not None:
            _admit_source(reads, watch, plan["identity"])
            return selected, watch
        try:
            sleep(min(limits.CI_BUILD_POLL_SECONDS, remaining))
        except OSError as exc:
            raise MbError("Build wait interrupted; rerun complete Build and E2E") from exc
    raise MbError("Build wait exhausted the 5400-second/observation budget; rerun complete Build and E2E")


def wait_for_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any],
                             monotonic: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait at most the protected 5400s admission budget, without an independent PR compiler.

    The pull request is admitted when the wait starts and again when a bundle is returned. In
    between, a poll reads only the run listing: one request while nothing has completed. Only
    absence, a pending run or a deferral waits; API, corruption and failed-producer errors
    propagate. An in-flight bounded API call may finish after the deadline, but its result can
    never admit evidence then. Sleep/clock hooks belong to the protected runtime, never candidate
    configuration.
    """

    return _wait(CommandReads.of(api), _pr_plan(plan), monotonic, sleep)[0]


def revalidate_latest_pr_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any]) -> None:
    """Reject a superseded/unavailable selected descriptor immediately around consumption.

    Call before independent byte/native verification consumes the selected Build. This repeats the
    newest-run observation and requires the same descriptor; it checks neither downloaded bytes,
    native validity nor status authority.
    """

    descriptor = _descriptor(descriptor)
    latest = select_latest_pr_build(api, plan=plan)
    check(latest is not None and latest == descriptor, "$.descriptor",
          "selected Build is no longer the newest exact available producer; rerun complete Build and E2E")


def download_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], output: Path,
                             monotonic: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait/select, download by immutable ID, and reject supersession inside atomic publication.

    Caller owns a private output parent inaccessible to both worker UIDs. The downloaded tuple and
    canonical bytes are verified; immediately before publication the pull request, the newest run,
    its latest attempt and the bundle's availability are observed again and must be unchanged.
    Native second-account validation and final gate authority remain separate. The 5400s waiting
    budget does not replace transport/validator timeouts.
    """

    plan = _pr_plan(plan)
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    reads = CommandReads.of(api)
    descriptor, watch = _wait(reads, plan, monotonic, sleep)
    return {"descriptor": descriptor, "envelope": _download_build(reads, watch, descriptor, plan, output)}
