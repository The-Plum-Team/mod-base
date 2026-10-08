"""Newest Build selection before success, never a latest-success query (MB11).

A producer run is found by the keys GitHub records it under: the managed Build caller's workflow
file and

* for a pull request, the ``pull_request_target`` event and the pull request's head commit, head
  branch and source repository;
* for a protected subject (a push or dispatch on the default branch), a ``push`` or
  ``workflow_dispatch`` event on that branch at the tested commit.

The listing carries no status filter. The newest run by ``(created_at, id)`` and its latest
attempt are chosen before any result is read:

* no run: nothing to consume. A pull request keeps waiting; a protected subject builds for itself;
* a run that has not completed: a pull request keeps waiting; for a protected subject it is a
  rejection, because a rebuild must never race a Build whose result is unknown;
* a completed run whose exact graph is the draft deferral (a pull request) or the admitted reuse
  (a protected subject): not a producer. The pull request keeps waiting, the protected subject
  builds for itself;
* a completed successful run with the exact full graph: its complete bundle is described;
* anything else (a failed or cancelled run, another graph, another controller or kit pin, a
  missing or expired bundle): a rejection. An older run is never considered.

A standalone packaged run that found nothing builds inside its own run (its ``rebuild`` calling
job); its later jobs read that Build while the run is still in progress.

There is no run-title contract. The tested merge is bound by the records inside the artifacts,
which the consumer compares with its own admitted plan.

:func:`select_build` finds the exact Build of a packaged run and writes its
``mod-base.ci.selection`` record.
"""

from __future__ import annotations

import functools
import math
import os
import secrets
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.graph import (job_name, require_graph, require_partial_graph, run_graph, sealed_upload,
                                     upload_job_name)
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import build_source_selection
from mod_base.build_ci.transport import (_admit_source, _artifact_state, _authenticate_artifacts, _authenticate_run,
                                         _descriptor, _download_build, _materialize_build, _merged, _plan, _run)
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import job_graph
from mod_base.github.runs import run_order, workflow_runs
from mod_base.model import grammar, limits
from mod_base.model.validators import Int, Str, check
from mod_base.workflow import CI_CALLER_WORKFLOWS, ci_producer, find_job

#: The name of the selection record in the state directory of a job that selects or consumes a Build.
SELECTION_NAME = "ci-selection.json"
#: The ``--build-run-id`` of a standalone packaged run whose ``rebuild`` job built in that run.
SAME_RUN = "same-run"
_PENDING = ("queued", "in_progress", "waiting", "requested", "pending")
#: The events that start a Build run of a protected subject.
_PROTECTED_EVENTS = ("push", "workflow_dispatch")
#: Artifact kind -> what a run that lacks it is said to lack.
_ARTIFACTS = {"build": "complete bundle", "tested": "tested record"}


def _pr_plan(plan: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(plan)
    check(plan["identity"]["pr_number"] > 0, "$.identity.pr_number", "selection requires an original PR plan")
    return plan


def _protected_plan(plan: dict[str, Any]) -> dict[str, Any]:
    plan = _plan(plan)
    check(plan["identity"]["pr_number"] == 0, "$.identity.pr_number",
          "a pull request waits for its own Build run; it never selects or rebuilds one")
    return plan


def newest_run(api: GitHubApi, producer: str, *, head_sha: str, head_branch: str, head_repository: str,
               events: tuple[str, ...]) -> dict[str, Any] | None:
    """The newest run of a managed caller that GitHub lists under one head, whatever its result.

    ``events`` are the events that start such a run; a single one is also the listing's filter.
    No status filter is sent and runs are ordered by ``(created_at, id)``. Returns
    ``{id, run_attempt, created_at, status, conclusion, event}``, or ``None`` when no run exists.
    """

    Str(choices=PRODUCERS)(producer, "$.producer")
    runs = [run for run in workflow_runs(api, CI_CALLER_WORKFLOWS[producer], branch=head_branch, head_sha=head_sha,
                                         event=events[0] if len(events) == 1 else None,
                                         max_items=limits.MAX_CI_BUILD_RUNS)
            if run.get("event") in events and isinstance(run.get("head_repository"), dict)
            and run["head_repository"].get("full_name") == head_repository]
    if not runs:
        return None
    run = max(runs, key=lambda candidate: run_order(candidate)[:2])
    _, run_id, attempt = run_order(run)
    return {"id": run_id, "run_attempt": attempt, "created_at": run["created_at"],
            "status": run.get("status"), "conclusion": run.get("conclusion"), "event": run.get("event")}


def _newest(reads: CommandReads, plan: dict[str, Any]) -> dict[str, Any] | None:
    """The newest Build run GitHub lists for the plan's subject, whatever its result."""

    head_sha, head_branch, head_repository = run_head(plan["identity"])
    events = ("pull_request_target",) if plan["identity"]["pr_number"] else _PROTECTED_EVENTS
    return newest_run(reads, "build", head_sha=head_sha, head_branch=head_branch, head_repository=head_repository,
                      events=events)


def pending_run(run: dict[str, Any]) -> None:
    """Require the listed or read state of a run that has not completed to be a real pending one."""

    check(run["status"] in _PENDING and run["conclusion"] is None, "$.run.status", "malformed pending run state")


def producer_record(plan: dict[str, Any], *, caller: str, run_id: int, run_attempt: int, event: str,
                    graph_sha256: str) -> dict[str, Any]:
    """The producer identity a descriptor carries for one run attempt of the managed caller
    ``caller`` for the plan's subject; it has no upload window yet (:func:`describe_artifact`)."""

    identity, path = plan["identity"], CI_CALLER_WORKFLOWS[caller]
    return {"run_id": run_id, "run_attempt": run_attempt, "workflow_path": path,
            "workflow_ref": grammar.workflow_ref(identity["repository"], path, identity["base_branch"]),
            "api_head_sha": run_head(identity)[0], "event": event, "graph_sha256": graph_sha256}


def describe_artifact(api: GitHubApi, *, plan: dict[str, Any], producer: dict[str, Any], jobs: list[dict[str, Any]],
                      kind: str, unit_id: str | None = None) -> dict[str, Any]:
    """Describe the one ``kind`` artifact of a run attempt from API data alone.

    ``producer`` is the attempt's :func:`producer_record` and ``jobs`` its job list, whose graph
    the caller has required. The upload window is the one of the job that uploads the artifact,
    which must have sealed first; the artifact is the only one of its exact name in the run. The
    result is a structurally valid descriptor. Whoever consumes it authenticates its run and the
    artifact's metadata, owner and availability.
    """

    attempt = producer["run_attempt"]
    caller = ci_producer(producer["workflow_path"])
    started, completed = sealed_upload(find_job(jobs, upload_job_name(caller, kind, unit_id), run_attempt=attempt))
    name = grammar.ci_artifact_name(kind, producer["run_id"], attempt, unit_id)
    rows = api.paginate(f"/repos/{api.repository}/actions/runs/{producer['run_id']}/artifacts",
                        field="artifacts", params={"name": name}, max_items=limits.MAX_ARTIFACTS_PER_NAME)
    check(len(rows) == 1, "$.artifact", f"producer run lacks one unique {_ARTIFACTS.get(kind, kind)}; rerun it")
    state = _artifact_state(rows[0])
    check(state["name"] == name, "$.artifact.name", "artifact listing ignored the exact name filter")
    descriptor = {"identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                  "producer": {**producer, "upload_window": {"started_at": started, "completed_at": completed}},
                  "artifact": {key: state[key] for key in ("id", "name", "digest", "size", "created_at",
                                                           "expires_at")}}
    return _descriptor(descriptor)


def _observe(reads: CommandReads, watch: Watch, plan: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """One observation of the newest Build run of the plan's subject.

    ``("bundle", descriptor)`` describes its complete bundle. Otherwise there is nothing to
    consume: ``absent`` (no run), ``pending`` (not completed), ``deferred`` (a pull request's
    draft deferral) or ``reuse`` (a protected subject's admitted reuse, which builds nothing).
    Everything mutable that was read is left in ``watch``."""

    identity = plan["identity"]
    newest = watch.read(("newest Build run",), functools.partial(_newest, reads, plan))
    if newest is None:
        return "absent", None
    if newest["status"] != "completed":
        pending_run(newest)
        return "pending", None
    run = _run(reads, watch, newest["id"])
    check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
          and run["run_attempt"] >= newest["run_attempt"], "$.run", "run listing and run disagree")
    if run["status"] != "completed":
        pending_run(run)
        return "pending", None
    check(run["conclusion"] == "success", "$.run.conclusion", "newest exact Build failed or was cancelled")
    attempt = run["run_attempt"]
    jobs = reads.attempt_jobs(newest["id"], attempt)
    idle = "deferred" if identity["pr_number"] else "reuse"
    if job_graph(jobs) == run_graph("build", idle).jobs(plan):
        return idle, None
    digest = require_graph(jobs, plan=plan, producer="build", mode="full", run_attempt=attempt)
    producer = producer_record(plan, caller="build", run_id=newest["id"], run_attempt=attempt, event=newest["event"],
                               graph_sha256=digest)
    _authenticate_run(reads, watch, producer, plan, mode="full", complete=True)
    descriptor = describe_artifact(reads, plan=plan, producer=producer, jobs=jobs, kind="build")
    _authenticate_artifacts(reads, watch, [descriptor], identity)
    return "bundle", descriptor


def _select(reads: CommandReads, watch: Watch, plan: dict[str, Any]) -> dict[str, Any] | None:
    """The complete bundle of a pull request's newest Build run, or ``None`` while it is absent,
    pending or deferred."""

    return _observe(reads, watch, plan)[1]


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


def _wait(reads: CommandReads, plan: dict[str, Any], wait_seconds: int, monotonic: Callable[[], float],
          sleep: Callable[[float], None]) -> tuple[dict[str, Any], Watch]:
    """Poll the newest Build run until its bundle can be described; return it with what was read."""

    check(callable(monotonic) and callable(sleep), "$.wait", "protected clock/sleep must be callable")
    Int(1, limits.CI_BUILD_WAIT_SECONDS)(wait_seconds, "$.wait_seconds")
    previous: float | None = None

    def now() -> float:
        nonlocal previous
        value = monotonic()
        check((type(value) is int or (type(value) is float and math.isfinite(value)))
              and (previous is None or value >= previous), "$.wait.clock", "invalid monotonic clock")
        previous = value
        return value

    deadline = now() + wait_seconds
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
    raise MbError(f"Build wait exhausted the {wait_seconds}-second/observation budget; "
                  "rerun complete Build and E2E")


def wait_for_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any],
                             wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS,
                             monotonic: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait at most the protected 5400s admission budget, without an independent PR compiler.

    ``wait_seconds`` may shorten the budget, never extend it. The pull request is admitted when
    the wait starts and again when a bundle is returned. In between, a poll reads only the run
    listing: one request while nothing has completed. Only absence, a pending run or a deferral
    waits; API, corruption and failed-producer errors propagate. An in-flight bounded API call may
    finish after the deadline, but its result can never admit evidence then. Sleep/clock hooks
    belong to the protected runtime, never candidate configuration.
    """

    return _wait(CommandReads.of(api), _pr_plan(plan), wait_seconds, monotonic, sleep)[0]


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
                             wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS,
                             monotonic: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait/select, download by immutable ID, and reject supersession inside atomic publication.

    Caller owns a private output parent inaccessible to both worker UIDs. The downloaded tuple and
    canonical bytes are verified; immediately before publication the pull request, the newest run,
    its latest attempt and the bundle's availability are observed again and must be unchanged.
    Native second-account validation and final gate authority remain separate. The waiting budget
    (``wait_seconds``, at most 5400s) does not replace transport/validator timeouts.
    """

    plan = _pr_plan(plan)
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    reads = CommandReads.of(api)
    descriptor, watch = _wait(reads, plan, wait_seconds, monotonic, sleep)
    return {"descriptor": descriptor, "envelope": _download_build(reads, watch, descriptor, plan, output)}


def _protected(reads: CommandReads, watch: Watch, plan: dict[str, Any]) -> dict[str, Any] | None:
    """The complete bundle of a protected subject's newest Build run, or ``None`` when it has no
    run or its newest run is an admitted reuse. A run that has not completed is a rejection."""

    state, descriptor = _observe(reads, watch, plan)
    check(state != "pending", "$.run.status",
          "newest exact Build of this commit has not completed; a standalone run neither waits nor rebuilds beside it")
    return descriptor


def select_protected_build(api: GitHubApi, *, plan: dict[str, Any]) -> dict[str, Any] | None:
    """Admit the live protected subject, then describe the complete bundle of its newest Build run.

    The run is one the Build caller started by a push or a dispatch on the default branch at the
    tested commit. None means that no such run exists or that the newest one is an admitted reuse,
    which builds nothing: the caller then builds for itself. A newest run that is pending, failed
    or cancelled, another graph, a missing bundle or an API failure is fatal; no older run is
    considered. One observation, like :func:`select_latest_pr_build`.
    """

    plan = _protected_plan(plan)
    reads, watch = CommandReads.of(api), Watch()
    _admit_source(reads, watch, plan["identity"])
    return _protected(reads, watch, plan)


def download_protected_build(api: GitHubApi, *, plan: dict[str, Any], output: Path,
                             run_id: int | None = None) -> dict[str, Any] | None:
    """Select the newest Build run of a protected subject and privately copy its complete bundle.

    With ``run_id`` the newest run must be exactly that run; a missing, superseded or reused one
    is then a rejection. Without it the answer is None when there is nothing to select
    (:func:`select_protected_build`). Immediately before publication the subject, the newest run,
    its latest attempt and the bundle's availability are observed again. Returns the descriptor
    and the envelope.
    """

    plan = _protected_plan(plan)
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    if run_id is not None:
        Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    reads, watch = CommandReads.of(api), Watch()
    _admit_source(reads, watch, plan["identity"])
    descriptor = _protected(reads, watch, plan)
    if descriptor is None and run_id is None:
        return None
    check(descriptor is not None and run_id in (None, descriptor["producer"]["run_id"]), "$.run_id",
          "the named Build run is not the newest exact Build of this subject; rerun the standalone run")
    return {"descriptor": descriptor, "envelope": _download_build(reads, watch, descriptor, plan, output)}


def _rebuilt(reads: CommandReads, watch: Watch, plan: dict[str, Any], *, run_id: int, run_attempt: int,
             event: str) -> dict[str, Any]:
    """The complete bundle a standalone packaged run built for itself, read by a later job of that
    run. The run is still in progress: its guard, its selection and every job of the Build it
    called must have finished the way the rebuilt graph expects."""

    producer = producer_record(plan, caller="packaged", run_id=run_id, run_attempt=run_attempt, event=event,
                               graph_sha256=run_graph("packaged", "rebuilt").sha256(plan))
    _authenticate_run(reads, watch, producer, plan, mode="rebuilt", complete=False)
    finished = [job_name("packaged", "guard", "verify"), job_name("packaged", "select-build", "select"),
                job_name("packaged", "build", "plan"), job_name("packaged", "build", "policy"),
                *(job_name("packaged", "build", "target", target["id"]) for target in plan["targets"]),
                job_name("packaged", "build", "assemble"), job_name("packaged", "build", "gate")]

    def describe() -> dict[str, Any]:
        jobs = reads.attempt_jobs(run_id, run_attempt)
        require_partial_graph(jobs, plan=plan, producer="packaged", mode="rebuilt", run_attempt=run_attempt,
                              finished=finished)
        return describe_artifact(reads, plan=plan, producer=producer, jobs=jobs, kind="build")

    descriptor = watch.read(("rebuilt Build", run_id, run_attempt), describe)
    _authenticate_artifacts(reads, watch, [descriptor], plan["identity"])
    return descriptor


def download_rebuilt_build(api: GitHubApi, *, plan: dict[str, Any], run_id: int, run_attempt: int, event: str,
                           output: Path, descriptor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Privately copy the complete bundle that the standalone packaged run ``run_id`` built for
    itself in ``run_attempt``; the reader is a later job of that run.

    The run's latest attempt, controller and kit pin, the finished jobs of its guard, selection
    and Build with their seals, and the bundle's upload window, metadata and availability are
    authenticated from the API, and the mutable part once more immediately before publication.
    With ``descriptor`` (a job that consumes an earlier selection) the bundle must still be exactly
    the selected one. Returns the descriptor and the envelope.
    """

    plan = _protected_plan(plan)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    Str(choices=("push", "workflow_dispatch", "schedule"))(event, "$.event")
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    expected = None if descriptor is None else _descriptor(descriptor)
    reads, watch = CommandReads.of(api), Watch()
    _admit_source(reads, watch, plan["identity"])
    selected = _rebuilt(reads, watch, plan, run_id=run_id, run_attempt=run_attempt, event=event)
    check(expected is None or selected == expected, "$.descriptor",
          "the Build this run rebuilt is no longer the selected one; rerun all jobs")
    return {"descriptor": selected, "envelope": _materialize_build(reads, selected, plan, output, watch.recheck)}


def select_build(api: GitHubApi, *, plan: dict[str, Any], run_id: int, run_attempt: int, workflow_path: str,
                 event: str, temporary_root: Path, build_run_id: int | str | None = None,
                 wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS,
                 monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None:
    """The selection record of the exact Build that run attempt ``run_id``/``run_attempt`` of the
    packaged caller consumes, or None when a protected subject has no Build to select.

    The subject and ``build_run_id`` choose the route. A pull request names no run and waits for
    the newest Build run of its head (:func:`download_latest_pr_build`). A protected subject
    without ``build_run_id`` takes the newest Build run of its commit or answers None, on which
    its caller builds in the same run; with a run id that run must be the newest one
    (:func:`download_protected_build`); with :data:`SAME_RUN` the Build is the one this run built
    (:func:`download_rebuilt_build`, for which ``event`` is the run's event). The bundle is
    downloaded and verified in a private directory below ``temporary_root`` and removed again: the
    record keeps the hash of its envelope. The request binding names the requesting run attempt,
    ``workflow_path`` on the default branch and a fresh nonce.
    """

    plan = _plan(plan)
    identity = plan["identity"]
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    check(ci_producer(workflow_path) == "packaged", "$.workflow_path", "only the packaged caller selects a Build")
    check(build_run_id is None or not identity["pr_number"], "$.build_run_id",
          "a pull request waits for its own Build run and names none")
    request = {"run_id": run_id, "run_attempt": run_attempt, "nonce": secrets.token_hex(32),
               "workflow_path": workflow_path,
               "workflow_ref": grammar.workflow_ref(identity["repository"], workflow_path, identity["base_branch"])}
    reads = CommandReads.of(api)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-select-", dir=temporary_root) as temporary:
            output = Path(temporary) / "build"
            if identity["pr_number"]:
                found = download_latest_pr_build(reads, plan=plan, output=output, wait_seconds=wait_seconds,
                                                 monotonic=monotonic, sleep=sleep)
            elif build_run_id == SAME_RUN:
                found = download_rebuilt_build(reads, plan=plan, run_id=run_id, run_attempt=run_attempt, event=event,
                                               output=output)
            else:
                found = download_protected_build(reads, plan=plan, output=output, run_id=build_run_id)
    except OSError as error:
        raise MbError("cannot hold the private Build copy of a selection", reason="ci-transport") from error
    if found is None:
        return None
    return build_source_selection(plan=plan, request=request, build=found["descriptor"], envelope=found["envelope"])
