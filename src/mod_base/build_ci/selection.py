"""Newest protected PR Build selection before success, never a latest-success query (MB11)."""

from __future__ import annotations

import copy
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import authenticate_merged_pr_identity, authenticate_source_identity
from mod_base.build_ci.graph import BuildGraphV1
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.records import validate_descriptor
from mod_base.build_ci.transport import _authenticate, _authenticate_producer, _download_export
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.artifacts import Artifact
from mod_base.github.jobs import job_graph_sha256, require_successful_step
from mod_base.github.runs import get_run, run_order, validate_run, workflow_runs
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import Int, check
from mod_base.workflow import CI_BUILD_CALL, CI_BUILD_JOBS, CI_UPLOAD_STEP, find_job


def select_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], workflow_path: str) -> dict[str, Any] | None:
    """Select the newest exact PR generation, then authenticate its complete Build descriptor.

    None means absent/pending, never permission to compile or succeed. A failed/cancelled newest
    run, unsupported title, missing bundle or API/corruption failure is fatal, with no old fallback.
    Caller enrolls the protected title-producing workflow independently; title is a selection hint.
    Downloaded full tuple/native byte validity, consumption-time newest recheck,
    caller/status authority and non-PR request/nonce routes remain additional required phases.
    """

    raw, retained = _selection_inputs(plan, workflow_path)
    def admit() -> None:
        authenticate_source_identity(api, retained["identity"])
    result = _select_latest_pr_build(api, plan=retained, workflow_path=workflow_path, source_admission=admit)
    _close_selection(plan, retained, raw)
    return result


def _selection_inputs(plan: dict[str, Any], workflow_path: str) -> tuple[bytes, dict[str, Any]]:
    validate_plan(plan)
    check(plan["identity"]["pr_number"] > 0, "$.identity.pr_number", "selection requires an original PR plan")
    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "protected Build workflow")
    raw = canonical_json(plan)
    return raw, strict_loads(raw, label="original Build selection plan", max_bytes=limits.MAX_CI_PLAN_BYTES)


def _close_selection(plan: dict[str, Any], retained: dict[str, Any], raw: bytes) -> None:
    for value in (plan, retained):
        validate_plan(value)
        check(canonical_json(value) == raw, "$.plan", "original Build selection plan changed during admission")


def select_latest_merged_pr_build(api: GitHubApi, *, plan: dict[str, Any], workflow_path: str,
                                  controller_sha: str, merged_sha: str) -> dict[str, Any] | None:
    """Select the newest original PR Build after actual historical source admission.

    Original plan/workflow and current controller/final merge require independent admission.
    None is absence/pending, not reuse approval. Both coherent historical gates, native bytes,
    original/current policy/pin, later consumer chronology and authority remain mandatory.
    No new compiler, fallback, upload, status or candidate execution is admitted.
    """
    raw, retained = _selection_inputs(plan, workflow_path)
    grammar.require_sha1(controller_sha, "current merged Build controller SHA")
    grammar.require_sha1(merged_sha, "final merged Build SHA")
    original = authenticate_merged_pr_identity(api, retained["identity"],
                                               controller_sha=controller_sha, merged_sha=merged_sha)
    def admit() -> None:
        check(authenticate_merged_pr_identity(api, retained["identity"],
                                              controller_sha=controller_sha, merged_sha=merged_sha) == original,
              "$.source", "original merged Build source changed during selection")
    result = _select_latest_pr_build(api, plan=retained, workflow_path=workflow_path, source_admission=admit)
    _close_selection(plan, retained, raw)
    return result


def revalidate_latest_merged_pr_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                                      workflow_path: str, controller_sha: str, merged_sha: str) -> None:
    """Repeat original historical newest/metadata admission around independent consumption.

    Caller snapshots, source and selected descriptor must remain original. This proves neither
    native payloads nor the packaged partner, consumer chronology or final reuse authority.
    """
    validate_descriptor(descriptor)
    raw = canonical_json(descriptor)
    retained = strict_loads(raw, label="original historical Build descriptor", max_bytes=limits.MAX_CI_RECORD_BYTES)
    latest = select_latest_merged_pr_build(api, plan=plan, workflow_path=workflow_path,
                                           controller_sha=controller_sha, merged_sha=merged_sha)
    validate_descriptor(descriptor)
    check(canonical_json(descriptor) == raw, "$.descriptor", "original historical Build descriptor changed")
    check(latest is not None and latest == retained, "$.descriptor",
          "historical Build is no longer the newest exact available producer")


def _select_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], workflow_path: str,
                             source_admission: Callable[[], None]) -> dict[str, Any] | None:
    """Shared exact producer selection; only protected public routes supply source admission."""
    validate_plan(plan)
    identity = plan["identity"]
    check(identity["pr_number"] > 0, "$.identity.pr_number", "this selection route requires an original PR plan")
    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "protected Build workflow")
    title = grammar.ci_pr_build_title(profile=plan["profile"], pr_number=identity["pr_number"],
                                    head_sha=identity["head_sha"], base_sha=identity["base_sha"],
                                    tested_sha=identity["tested_sha"])

    def newest() -> dict[str, Any] | None:
        runs = workflow_runs(api, workflow_path, branch=identity["base_branch"],
                             head_sha=identity["controller_sha"], event="pull_request_target",
                             max_items=limits.MAX_CI_BUILD_RUNS)
        matches = []
        for run in runs:
            marker = grammar.parse_ci_pr_build_title(run.get("display_title"))
            check(marker is not None and marker.profile == plan["profile"],
                  "$.run.display_title", "run is outside the protected PR Build title contract")
            if run["display_title"] == title:
                matches.append(run)
        return max(matches, key=run_order) if matches else None

    source_admission()
    selected = newest()
    if selected is None:
        source_admission()
        return None
    current = get_run(api, selected["id"])
    Int(1, limits.MAX_RUN_ID)(selected.get("workflow_id"), "$.run.workflow_id")
    validate_run(current, repository=api.repository, workflow_path=workflow_path,
                 events=("pull_request_target",), head_branch=identity["base_branch"],
                 head_sha=identity["controller_sha"], workflow_id=selected["workflow_id"],
                 require_success=False, display_title=title)
    check(run_order(current) == run_order(selected), "$.run", "selected run/attempt changed after listing")
    if current.get("status") != "completed":
        check(current.get("status") in ("queued", "in_progress", "waiting", "requested", "pending")
              and current.get("conclusion") is None, "$.run.status", "malformed pending Build state")
        source_admission()
        return None
    check(current.get("conclusion") == "success", "$.run.conclusion", "newest exact Build failed or was cancelled")
    producer = {"run_id": current["id"], "run_attempt": current["run_attempt"],
                "workflow_path": workflow_path,
                "workflow_ref": f"{identity['repository']}/{workflow_path}@refs/heads/{identity['base_branch']}",
                "api_head_sha": identity["controller_sha"], "event": "pull_request_target",
                "graph_sha256": job_graph_sha256(BuildGraphV1().jobs(plan))}
    jobs = _authenticate_producer(api, producer, plan, complete=True, source_admission=source_admission)
    aggregate = find_job(jobs, f"{CI_BUILD_CALL} / {CI_BUILD_JOBS['assemble']}",
                         run_attempt=producer["run_attempt"])
    upload = require_successful_step(aggregate, CI_UPLOAD_STEP)
    producer["upload_window"] = {key: upload[key] for key in ("started_at", "completed_at")}
    name = grammar.ci_artifact_name("build", producer["run_id"], producer["run_attempt"])
    rows = api.paginate(f"/repos/{api.repository}/actions/runs/{producer['run_id']}/artifacts",
                        field="artifacts", params={"name": name}, max_items=limits.MAX_ARTIFACTS_PER_NAME)
    check(len(rows) == 1, "$.artifact", "newest exact Build lacks one unique complete bundle; rerun Build")
    artifact = Artifact.parse(rows[0])
    check(artifact.name == name, "$.artifact.name", "artifact listing ignored the exact name filter")
    descriptor = {"identity": copy.deepcopy(identity), "plan_sha256": plan["plan_sha256"],
                  "profile": plan["profile"], "producer": producer,
                  "artifact": {"id": artifact.id, "name": artifact.name, "digest": artifact.digest,
                               "size": artifact.size, "created_at": artifact.created_at,
                               "expires_at": rows[0].get("expires_at")}}
    validate_descriptor(descriptor)
    _authenticate(api, descriptor, plan, source_admission=source_admission)
    latest = newest()
    check(latest is not None and run_order(latest) == run_order(current)
          and latest.get("workflow_id") == current["workflow_id"],
          "$.run", "newest exact Build changed during artifact selection")
    final = get_run(api, current["id"])
    validate_run(final, repository=api.repository, workflow_path=workflow_path,
                 events=("pull_request_target",), head_branch=identity["base_branch"],
                 head_sha=identity["controller_sha"], workflow_id=current["workflow_id"],
                 display_title=title)
    check(run_order(final) == run_order(current), "$.run", "selected attempt changed before return")
    source_admission()
    return descriptor


def wait_for_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], workflow_path: str,
                             monotonic: Callable[[], float] = time.monotonic,
                             sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait at most the protected 5400s admission budget, without an independent PR compiler.

    Every observation repeats newest/source authentication; only absence or valid pending state
    waits. API/corruption/failed producer errors propagate. An in-flight bounded API call may
    finish after the deadline, but its result can never admit evidence then. Sleep/clock hooks
    belong to the protected runtime, never candidate configuration.
    """

    validate_plan(plan)
    check(callable(monotonic) and callable(sleep), "$.wait", "protected clock/sleep must be callable")
    plan = copy.deepcopy(plan)
    previous: float | None = None

    def now() -> float:
        nonlocal previous
        value = monotonic()
        check((type(value) is int or (type(value) is float and math.isfinite(value)))
              and (previous is None or value >= previous), "$.wait.clock", "invalid monotonic clock")
        previous = value
        return value

    deadline = now() + limits.CI_BUILD_WAIT_SECONDS
    for _ in range(limits.MAX_CI_BUILD_POLLS):
        if now() >= deadline:
            break
        selected = select_latest_pr_build(api, plan=plan, workflow_path=workflow_path)
        remaining = deadline - now()
        if remaining <= 0:
            break
        if selected is not None:
            return selected
        try:
            sleep(min(limits.CI_BUILD_POLL_SECONDS, remaining))
        except OSError as exc:
            raise MbError("Build wait interrupted; rerun complete Build and E2E") from exc
    raise MbError("Build wait exhausted the 5400-second/observation budget; rerun complete Build and E2E")


def revalidate_latest_pr_build(api: GitHubApi, *, descriptor: dict[str, Any],
                               plan: dict[str, Any], workflow_path: str) -> None:
    """Reject a superseded/unavailable selected descriptor immediately around consumption.

    Call before and after independent byte/native verification. This checks current producer
    selection and metadata, not downloaded bytes, native validity, or caller/status authority.
    """

    validate_descriptor(descriptor)
    latest = select_latest_pr_build(api, plan=plan, workflow_path=workflow_path)
    check(latest is not None and latest == descriptor, "$.descriptor",
          "selected Build is no longer the newest exact available producer; rerun complete Build and E2E")


def download_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], workflow_path: str,
                              output: Path, monotonic: Callable[[], float] = time.monotonic,
                              sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Wait/select, download by immutable ID, and reject supersession inside atomic publication.

    Caller owns a private output parent inaccessible to both worker UIDs. Downloaded full tuple/
    canonical bytes are verified; native second-account validation and final gate authority
    remain separate. The 5400s waiting budget does not replace transport/validator timeouts.
    """

    validate_plan(plan)
    plan = copy.deepcopy(plan)
    descriptor = wait_for_latest_pr_build(api, plan=plan, workflow_path=workflow_path,
                                          monotonic=monotonic, sleep=sleep)
    def revalidate() -> None:
        revalidate_latest_pr_build(api, descriptor=descriptor, plan=plan, workflow_path=workflow_path)
    revalidate()
    envelope = _download_export(api, descriptor=descriptor, plan=plan, workflow_path=workflow_path,
                                output=output, target_id=None, before_publish=revalidate)
    return {"descriptor": descriptor, "envelope": envelope}
