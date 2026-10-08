"""Exact plan-derived full-execution graphs, never inferred from observed jobs.

Hosted reusable-workflow name semantics remain a mandatory live canary gate. These contracts
cover full execution only; deferred/reuse admission is unsupported until separately implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.records import bind_gate_receipt
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import actions_time, attempt_jobs, require_job_graph, require_successful_step, step_window
from mod_base.model import grammar
from mod_base.model import limits
from mod_base.model.validators import Int, check
from mod_base.workflow import (CI_BUILD_CALL, CI_BUILD_JOBS, CI_PACKAGED_CALL, CI_PACKAGED_JOBS,
                               CI_SEAL_STEP, CI_UPLOAD_STEP, find_job)


@dataclass(frozen=True)
class BuildGraphV1:
    """Full shared Build; every target and protected policy leg is mandatory."""

    def jobs(self, plan: dict[str, Any]) -> list[dict[str, str]]:
        validate_plan(plan)
        names = [CI_BUILD_JOBS[key] for key in ("plan", "policy", "assemble", "gate")]
        names.extend(CI_BUILD_JOBS["target"].format(id=target["id"]) for target in plan["targets"])
        return sorted(({"name": f"{CI_BUILD_CALL} / {name}", "conclusion": "success"} for name in names),
                      key=lambda entry: entry["name"])

    def sealed_jobs(self, plan: dict[str, Any]) -> list[str]:
        validate_plan(plan)
        return [f"{CI_BUILD_CALL} / {CI_BUILD_JOBS['target'].format(id=target['id'])}"
                for target in plan["targets"]] + [f"{CI_BUILD_CALL} / {CI_BUILD_JOBS['assemble']}"]


@dataclass(frozen=True)
class PackagedGraphV1:
    """Full shared packaged E2E, with exact Build admission and every contracted lane."""

    def jobs(self, plan: dict[str, Any]) -> list[dict[str, str]]:
        validate_plan(plan)
        names = [CI_PACKAGED_JOBS[key] for key in ("input", "aggregate", "gate")]
        names.extend(CI_PACKAGED_JOBS["lane"].format(id=lane["id"]) for lane in plan["lanes"])
        return sorted(({"name": f"{CI_PACKAGED_CALL} / {name}", "conclusion": "success"} for name in names),
                      key=lambda entry: entry["name"])

    def sealed_jobs(self, plan: dict[str, Any]) -> list[str]:
        validate_plan(plan)
        return [f"{CI_PACKAGED_CALL} / {CI_PACKAGED_JOBS['lane'].format(id=lane['id'])}"
                for lane in plan["lanes"]] + [f"{CI_PACKAGED_CALL} / {CI_PACKAGED_JOBS['aggregate']}"]


def authenticate_graph(api: GitHubApi, *, plan: dict[str, Any], producer: str,
                       run_id: int, run_attempt: int) -> str:
    """Verify a complete exact attempt and successful ordered seal/upload steps.

    This is a graph check, not source/artifact admission or authority to publish a status.
    Caller-owned guard/writer/advisory jobs must be authenticated in their own closed caller graph.
    """

    validate_plan(plan)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    check(producer in ("build", "packaged"), "$.producer", "must be build or packaged")
    graph = BuildGraphV1() if producer == "build" else PackagedGraphV1()
    jobs = attempt_jobs(api, run_id, run_attempt)
    digest = require_job_graph(jobs, graph.jobs(plan))
    for name in graph.sealed_jobs(plan):
        job = find_job(jobs, name, run_attempt=run_attempt)
        require_successful_step(job, CI_SEAL_STEP)
        require_successful_step(job, CI_UPLOAD_STEP)
        seal = step_window(job, CI_SEAL_STEP)
        upload = step_window(job, CI_UPLOAD_STEP)
        check(seal[1] <= upload[0], "$.jobs.steps", "upload started before native export validation finished")
    return digest


def authenticate_gate_timeline(api: GitHubApi, *, document: dict[str, Any],
                               descriptor: dict[str, Any], plan: dict[str, Any]) -> str:
    """Bind a full tested record to exact API input/gate execution and upload times.

    This proves job/step chronology only. Protected run/source/pin authority, immutable artifact
    metadata and bytes, native report validity and the closed caller graph remain mandatory.
    Historical reuse and in-progress same-run writers need separately admitted graph routes.
    """

    bind_gate_receipt(document, descriptor=descriptor, plan=plan)
    producer = document["producer"]
    gate = document["gate"]
    graph = BuildGraphV1() if gate == "build" else PackagedGraphV1()
    call = CI_BUILD_CALL if gate == "build" else CI_PACKAGED_CALL
    names = CI_BUILD_JOBS if gate == "build" else CI_PACKAGED_JOBS
    sealed_names = set(graph.sealed_jobs(plan))
    jobs = attempt_jobs(api, producer["run_id"], producer["run_attempt"])
    digest = require_job_graph(jobs, graph.jobs(plan))
    check(digest == producer["graph_sha256"], "$.producer.graph_sha256", "wrong full gate graph")

    def bounded_step(job: dict[str, Any], step: str) -> tuple[datetime, datetime]:
        require_successful_step(job, step)
        window = step_window(job, step)
        start = actions_time(job.get("started_at"), "job started_at")
        end = actions_time(job.get("completed_at"), "job completed_at")
        check(start <= window[0] <= window[1] <= end, "$.jobs.steps", "step lies outside its completed job")
        return window

    def sealed_upload(job: dict[str, Any]) -> tuple[tuple[datetime, datetime], tuple[datetime, datetime]]:
        seal = bounded_step(job, CI_SEAL_STEP)
        upload = bounded_step(job, CI_UPLOAD_STEP)
        check(seal[1] <= upload[0], "$.jobs.steps", "upload started before validation finished")
        return seal, upload

    gate_name = f"{call} / {names['gate']}"
    gate_job = find_job(jobs, gate_name, run_attempt=producer["run_attempt"])
    verifier, upload = sealed_upload(gate_job)
    expected = descriptor["producer"]["upload_window"]
    check(upload == tuple(actions_time(expected[key], key) for key in ("started_at", "completed_at")),
          "$.producer.upload_window", "wrong tested-record upload window")
    for job in jobs:
        start = actions_time(job.get("started_at"), "job started_at")
        end = actions_time(job.get("completed_at"), "job completed_at")
        check(start <= end, "$.jobs", "job completion precedes start")
        if job["name"] != gate_name:
            check(end <= verifier[0], "$.jobs", "gate validation started before its prerequisites completed")
        if job["name"] in sealed_names:
            sealed_upload(job)

    for source in document["artifacts"] + ([document["owning_build"]] if document["owning_build"] else []):
        name = grammar.parse_ci_artifact_name(source["artifact"]["name"])
        source_producer = source["producer"]
        if source is document["owning_build"]:
            source_jobs = attempt_jobs(api, source_producer["run_id"], source_producer["run_attempt"])
            source_digest = require_job_graph(source_jobs, BuildGraphV1().jobs(plan))
            check(source_digest == source_producer["graph_sha256"], "$.owning_build.producer.graph_sha256",
                  "wrong owning Build graph")
            build_sealed_names = set(BuildGraphV1().sealed_jobs(plan))
            for source_entry in source_jobs:
                start = actions_time(source_entry.get("started_at"), "owning Build job started_at")
                end = actions_time(source_entry.get("completed_at"), "owning Build job completed_at")
                check(start <= end <= verifier[0], "$.owning_build.jobs",
                      "owning Build job is reversed or completed after gate validation started")
                if source_entry["name"] in build_sealed_names:
                    sealed_upload(source_entry)
        else:
            source_jobs = jobs
        if name.kind == "build":
            source_name = f"{CI_BUILD_CALL} / {CI_BUILD_JOBS['assemble']}"
        elif name.kind == "results":
            source_name = f"{CI_PACKAGED_CALL} / {CI_PACKAGED_JOBS['aggregate']}"
        else:
            source_name = f"{CI_PACKAGED_CALL} / {CI_PACKAGED_JOBS['lane'].format(id=name.unit_id)}"
        source_job = find_job(source_jobs, source_name, run_attempt=source_producer["run_attempt"])
        _, actual = sealed_upload(source_job)
        window = source_producer["upload_window"]
        check(actual == tuple(actions_time(window[key], key) for key in ("started_at", "completed_at")),
              "$.source.upload_window", "source window differs from actual API upload")
        check(actions_time(source_job.get("completed_at"), "source job completed_at") <= verifier[0],
              "$.source", "source job completed after gate validation started")
    return digest
