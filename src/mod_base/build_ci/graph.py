"""Exact job graphs of the managed Build and packaged-E2E callers, never inferred from observed jobs.

The graph of a run is the exact multiset of the jobs its caller owns and the jobs of every
workflow it calls, each with one expected conclusion. It is a function of the producer (which
managed caller ran), one mode of that producer's closed set and the protected plan. A consumer
decides the mode from what it has itself admitted (the subject, its inputs, a sealed record) and
then requires the run to show exactly that graph; it never picks a graph to fit the jobs it sees.

Build modes: ``full`` (every worker succeeds), ``deferred`` (a draft: the guard and the deferral
job succeed and the call is skipped) and ``reuse`` (a push to the default branch with admitted
reuse: plan and gate succeed, the workers are skipped). Packaged modes: ``pull-request`` (the Build
is a separate run), ``deferred``, ``selected`` (a standalone run that selected an existing Build),
``rebuilt`` (a standalone run that built in the same run) and ``reuse``.

How GitHub names a skipped call, a skipped unexpanded matrix and a skipped caller job, and which
workflows a run lists as referenced, is confirmed only by a hosted canary.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from mod_base.build_ci.protocol import validate_identity, validate_plan
from mod_base.build_ci.reads import CommandReads
from mod_base.build_ci.records import GATE_MODES, bind_gate_receipt
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import job_graph_sha256, require_job_graph, require_successful_step
from mod_base.model import grammar
from mod_base.model import limits
from mod_base.model.validators import Int, check
from mod_base.workflow import (CI_CALLEE_WORKFLOWS, CI_CALLER_JOBS, CI_CALLS, CI_GUARD_WORKFLOW_PATH, CI_SEAL_STEP,
                               CI_UPLOAD_STEP, ci_api_job_name, ci_caller_job_name, ci_producer,
                               ci_skipped_call_job_name, ci_unexpanded_api_job_name, find_job)

BUILD_MODES = ("full", "deferred", "reuse")
PACKAGED_MODES = ("pull-request", "deferred", "selected", "rebuilt", "reuse")
#: Producer -> mode -> caller job -> how it runs: ``None`` when the job itself is skipped, ``"run"``
#: for a job that simply succeeds, otherwise the mode of the kit workflow it calls.
_CALLER = {
    "build": {
        "full": {"guard": "run", "deferred": None, "shared": "full"},
        "deferred": {"guard": "run", "deferred": "run", "shared": None},
        "reuse": {"guard": "run", "deferred": None, "shared": "reuse"},
    },
    "packaged": {
        "pull-request": {"guard": "run", "deferred": None, "select": None, "rebuild": None, "shared": "full"},
        "deferred": {"guard": "run", "deferred": "run", "select": None, "rebuild": None, "shared": None},
        "selected": {"guard": "run", "deferred": None, "select": "run", "rebuild": None, "shared": "full"},
        "rebuilt": {"guard": "run", "deferred": None, "select": "run", "rebuild": "full", "shared": "full"},
        "reuse": {"guard": "run", "deferred": None, "select": "run", "rebuild": None, "shared": "reuse"},
    },
}
#: Called workflow -> its mode -> job key -> conclusion.
_CALLEE = {
    "guard": {"run": {"verify": "success"}},
    "select-build": {"run": {"select": "success"}},
    "build": {
        "full": {"plan": "success", "policy": "success", "target": "success", "assemble": "success",
                 "gate": "success"},
        "reuse": {"plan": "success", "policy": "skipped", "target": "skipped", "assemble": "skipped",
                  "gate": "success"},
    },
    "packaged-e2e": {
        "full": {"input": "success", "lane": "success", "aggregate": "success", "gate": "success"},
        "reuse": {"input": "skipped", "lane": "skipped", "aggregate": "skipped", "gate": "success"},
    },
}
#: Called workflow -> matrix job -> the plan list it has one job per entry of.
_MATRIX = {"build": {"target": "targets"}, "packaged-e2e": {"lane": "lanes"}}
#: Called workflow -> the jobs that seal an export and upload it when they run.
_SEALING = {"build": ("target", "assemble", "gate"), "packaged-e2e": ("lane", "aggregate", "gate")}
#: Artifact kind (and, for a tested record, its gate) -> the called workflow and job that uploads it.
_UPLOADERS = {("target", None): ("build", "target"), ("build", None): ("build", "assemble"),
              ("runtime", None): ("packaged-e2e", "lane"), ("results", None): ("packaged-e2e", "aggregate"),
              ("tested", "build"): ("build", "gate"), ("tested", "packaged"): ("packaged-e2e", "gate")}


@dataclass(frozen=True)
class _Job:
    name: str
    conclusion: str
    call: str
    key: str | None
    sealed: bool


@dataclass(frozen=True)
class _RunGraph:
    mode: str
    producer: ClassVar[str]
    modes: ClassVar[tuple[str, ...]]

    def __post_init__(self) -> None:
        check(type(self.mode) is str and self.mode in self.modes, "$.mode",
              f"is not a mode of the {self.producer} caller")

    def _expected(self, plan: dict[str, Any]) -> list[_Job]:
        validate_plan(plan)
        expected = []
        for call, state in _CALLER[self.producer][self.mode].items():
            callee = CI_CALLS[self.producer].get(call)
            if callee is None or state is None:
                # The caller's own job, or a calling job that was skipped before its callee expanded.
                name = ci_caller_job_name(self.producer, call) if callee is None else \
                    ci_skipped_call_job_name(self.producer, call)
                expected.append(_Job(name, "success" if state else "skipped", call, None, False))
                continue
            for job, conclusion in _CALLEE[callee][state].items():
                sealed = conclusion == "success" and job in _SEALING.get(callee, ())
                units = _MATRIX.get(callee, {}).get(job)
                if units is None:
                    names = [ci_api_job_name(self.producer, call, job)]
                elif conclusion == "skipped":
                    names = [ci_unexpanded_api_job_name(self.producer, call, job)]
                else:
                    names = [ci_api_job_name(self.producer, call, job, id=unit["id"]) for unit in plan[units]]
                expected.extend(_Job(name, conclusion, call, job, sealed) for name in names)
        return expected

    def jobs(self, plan: dict[str, Any]) -> list[dict[str, str]]:
        """The exact ``[{name, conclusion}]`` multiset of a completed run, sorted by name."""

        return sorted(({"name": job.name, "conclusion": job.conclusion} for job in self._expected(plan)),
                      key=lambda entry: entry["name"])

    def sealed_jobs(self, plan: dict[str, Any]) -> list[str]:
        """The jobs that seal an export and upload it in this mode, gates included."""

        return [job.name for job in self._expected(plan) if job.sealed]

    def sha256(self, plan: dict[str, Any]) -> str:
        """The digest a producer record carries as ``graph_sha256`` for this graph."""

        return job_graph_sha256(self.jobs(plan))

    def called(self) -> dict[str, bool]:
        """Every kit callee id the caller references -> whether its calling job runs in this mode."""

        return {callee: _CALLER[self.producer][self.mode][call] is not None
                for call, callee in CI_CALLS[self.producer].items() if callee != "guard"}

    def prerequisites(self, plan: dict[str, Any], gate_job: str) -> list[str]:
        """The jobs that ran and must have finished before the gate job ``gate_job`` validates:
        every successful job of its own call and of every call the caller makes before it."""

        expected = self._expected(plan)
        order = list(CI_CALLER_JOBS[self.producer])
        gate = next((job for job in expected if job.name == gate_job and job.key == "gate" and job.sealed), None)
        check(gate is not None, "$.gate", "is not a gate this graph runs")
        return [job.name for job in expected if job.conclusion == "success" and job.name != gate_job
                and order.index(job.call) <= order.index(gate.call)]


@dataclass(frozen=True)
class BuildGraphV1(_RunGraph):
    """A run of the managed Build caller in one mode of ``BUILD_MODES``."""

    mode: str = "full"
    producer: ClassVar[str] = "build"
    modes: ClassVar[tuple[str, ...]] = BUILD_MODES


@dataclass(frozen=True)
class PackagedGraphV1(_RunGraph):
    """A run of the managed packaged-E2E caller in one mode of ``PACKAGED_MODES``."""

    mode: str = "pull-request"
    producer: ClassVar[str] = "packaged"
    modes: ClassVar[tuple[str, ...]] = PACKAGED_MODES


def run_graph(producer: str, mode: str) -> BuildGraphV1 | PackagedGraphV1:
    """The graph contract of ``producer`` (``build`` or ``packaged``) in ``mode``."""

    check(type(producer) is str and producer in _CALLER, "$.producer", "must be build or packaged")
    return BuildGraphV1(mode) if producer == "build" else PackagedGraphV1(mode)


def job_name(producer: str, callee: str, job: str, unit_id: str | None = None) -> str:
    """The API name of ``job`` of the kit workflow ``callee`` in a ``producer`` run; ``unit_id`` is
    the target or lane of a matrix job. A Build job of a packaged run is a job of the Build it
    rebuilt."""

    check(type(producer) is str and producer in _CALLER, "$.producer", "must be build or packaged")
    call = next((call for call, called in CI_CALLS[producer].items() if called == callee), None)
    check(call is not None, "$.producer", f"the {producer} caller never calls that workflow")
    check((unit_id is not None) == (job in _MATRIX.get(callee, {})), "$.unit_id", "only a matrix job has a unit")
    return ci_api_job_name(producer, call, job, **({} if unit_id is None else {"id": unit_id}))


def upload_job_name(producer: str, kind: str, unit_id: str | None) -> str:
    """The API name of the job that uploads the ``kind`` artifact of ``unit_id`` in a ``producer`` run.

    A reuse reference is uploaded by the gate of the producer's own workflow."""

    if kind == "reuse":
        return job_name(producer, "build" if producer == "build" else "packaged-e2e", "gate")
    key = (kind, unit_id if kind == "tested" else None)
    check(key in _UPLOADERS, "$.artifact.name", "no job uploads this artifact kind")
    callee, job = _UPLOADERS[key]
    return job_name(producer, callee, job, unit_id if job in _MATRIX[callee] else None)


def gate_mode(producer: str, gate: str, plan: dict[str, Any], digest: str) -> str:
    """The mode a gate's producer record was sealed in: the one admissible mode of ``GATE_MODES``
    whose exact graph for ``plan`` has the digest the record carries.

    A pull request admits only the full Build and the pull-request packaged run, a standalone
    subject never the pull-request one. The answer comes from the admitted record and the plan,
    before any job of the run is read."""

    validate_plan(plan)
    pull_request = bool(plan["identity"]["pr_number"])
    modes = [mode for mode in GATE_MODES.get(gate, {}).get(producer, ())
             if (mode == "pull-request") <= pull_request and (mode in ("selected", "rebuilt")) <= (not pull_request)
             and run_graph(producer, mode).sha256(plan) == digest]
    check(len(modes) == 1, "$.producer.graph_sha256", "is not the graph of an admissible gate mode")
    return modes[0]


def authenticate_referenced_workflows(references: Sequence[tuple[str, str]], *, identity: dict[str, Any],
                                      producer: str, mode: str) -> None:
    """Bind a producer run to its controller commit and kit pin through ``referenced_workflows``.

    ``references`` are the run's ``github.runs.referenced_workflows`` pairs. The run must list the
    mod's guard workflow at ``identity["controller_sha"]`` (a ``pull_request_target`` run names its
    controller nowhere else) and every kit callee whose calling job runs in ``mode`` at the pinned
    kit SHA. A kit callee of this caller whose calling job is skipped may be listed too, at the
    same SHA; no other entry and no repeat is accepted."""

    validate_identity(identity)
    graph = run_graph(producer, mode)
    guard = f"{identity['repository']}/{CI_GUARD_WORKFLOW_PATH}"
    expected = {guard: identity["controller_sha"]}
    required = {guard}
    for callee, runs in graph.called().items():
        workflow = f"{identity['kit']['repository']}/{CI_CALLEE_WORKFLOWS[callee]}"
        expected[workflow] = identity["kit"]["sha"]
        if runs:
            required.add(workflow)
    observed = [tuple(reference) for reference in references]
    names = [workflow for workflow, _ in observed]
    check(len(set(names)) == len(names), "$.run.referenced_workflows", "repeats a workflow")
    for workflow, sha in observed:
        check(workflow in expected, "$.run.referenced_workflows", "lists a workflow this caller never calls")
        check(sha == expected[workflow], "$.run.referenced_workflows",
              "the run did not execute the admitted controller commit" if workflow == guard
              else "the run did not execute the pinned kit")
    check(required <= set(names), "$.run.referenced_workflows", "does not list the guard and every called kit workflow")


def _step_window(job: dict[str, Any], step: str) -> tuple[str, str]:
    """``(started, completed)`` of the job's one successful ``step``, as whole-second UTC text."""

    record = require_successful_step(job, step)
    started = grammar.normalize_timestamp(record.get("started_at"), f"step {step!r} started_at")
    completed = grammar.normalize_timestamp(record.get("completed_at"), f"step {step!r} completed_at")
    check(started <= completed, "$.jobs.steps", "step completed before it started")
    return started, completed


def sealed_upload(job: dict[str, Any]) -> tuple[str, str]:
    """The upload window of a sealing job: its one successful seal step must finish before its one
    successful upload step starts, and both must lie inside the completed successful job."""

    check(job.get("status") == "completed" and job.get("conclusion") == "success",
          "$.jobs", "sealing job did not succeed")
    seal, upload = _step_window(job, CI_SEAL_STEP), _step_window(job, CI_UPLOAD_STEP)
    check(seal[1] <= upload[0], "$.jobs.steps", "upload started before sealing finished")
    started, completed = _job_window(job)
    check(started <= seal[0] and upload[1] <= completed, "$.jobs.steps", "step lies outside its completed job")
    return upload


def _job_window(job: dict[str, Any]) -> tuple[str, str]:
    started = grammar.normalize_timestamp(job.get("started_at"), "job started_at")
    completed = grammar.normalize_timestamp(job.get("completed_at"), "job completed_at")
    check(started <= completed, "$.jobs", "job completion precedes start")
    return started, completed


def require_graph(jobs: list[dict[str, Any]], *, plan: dict[str, Any], producer: str, mode: str,
                  run_attempt: int) -> str:
    """Require the jobs of a completed attempt to be exactly the graph of ``producer`` in ``mode``,
    with every sealing job's seal finished before its upload; return the graph digest."""

    graph = run_graph(producer, mode)
    digest = require_job_graph(jobs, graph.jobs(plan))
    for name in graph.sealed_jobs(plan):
        sealed_upload(find_job(jobs, name, run_attempt=run_attempt))
    return digest


def require_partial_graph(jobs: list[dict[str, Any]], *, plan: dict[str, Any], producer: str, mode: str,
                          run_attempt: int, finished: list[str]) -> None:
    """Admit the jobs of a still-running attempt of ``producer`` in ``mode`` for an in-run reader.

    Every job the attempt shows must be one the graph expects, at most once; each job named in
    ``finished`` must have completed with the conclusion the graph expects and, when it seals, a
    seal that finished before its upload. Jobs that have not started are simply absent. This is
    never proof that the remaining jobs succeed: the completed graph is authenticated at the gate."""

    graph = run_graph(producer, mode)
    expected = {entry["name"]: entry["conclusion"] for entry in graph.jobs(plan)}
    names = [job.get("name") for job in jobs]
    check(len(set(names)) == len(names) and set(names) <= set(expected),
          "$.jobs", "running attempt has a duplicate or unenrolled job")
    sealed = set(graph.sealed_jobs(plan))
    for name in finished:
        check(name in expected, "$.jobs", "required job is outside this graph")
        job = find_job(jobs, name, run_attempt=run_attempt)
        check(job.get("status") == "completed" and job.get("conclusion") == expected[name],
              "$.jobs", f"required job {name!r} has not finished as the graph expects"[:200])
        if name in sealed:
            sealed_upload(job)


def authenticate_graph(api: GitHubApi, *, plan: dict[str, Any], producer: str, mode: str,
                       run_id: int, run_attempt: int) -> str:
    """Read one attempt's jobs and require the exact completed graph of ``producer`` in ``mode``.

    This is a graph check, not source, run or artifact admission and not authority to publish a
    status. Returns the graph digest."""

    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    jobs = CommandReads.of(api).attempt_jobs(run_id, run_attempt)
    return require_graph(jobs, plan=plan, producer=producer, mode=mode, run_attempt=run_attempt)


def authenticate_gate_timeline(api: GitHubApi, *, document: dict[str, Any],
                               descriptor: dict[str, Any], plan: dict[str, Any]) -> str:
    """Bind a tested record to the exact graph of its run and to real execution and upload times.

    The record's mode selects the graph. The gate's seal step is where validation happens: every
    prerequisite job must have finished before it starts, the gate's upload window must be the one
    the descriptor carries, and every source artifact's window must be the actual upload step of
    the job that produced it. A Build consumed from another run needs that run's own exact full
    graph, finished before validation started. Times are compared as whole seconds.

    This proves job and step chronology only. Protected run/source/pin authority, immutable artifact
    metadata and bytes and native report validity remain mandatory."""

    bind_gate_receipt(document, descriptor=descriptor, plan=plan)
    reads = CommandReads.of(api)
    producer = document["producer"]
    caller = ci_producer(producer["workflow_path"])
    graph = run_graph(caller, document["mode"])
    attempt = producer["run_attempt"]
    jobs = reads.attempt_jobs(producer["run_id"], attempt)
    digest = require_graph(jobs, plan=plan, producer=caller, mode=document["mode"], run_attempt=attempt)
    check(digest == producer["graph_sha256"], "$.producer.graph_sha256", "wrong gate graph")

    gate_name = upload_job_name(caller, "tested", document["gate"])
    gate_job = find_job(jobs, gate_name, run_attempt=attempt)
    verifier = _step_window(gate_job, CI_SEAL_STEP)[0]
    window = descriptor["producer"]["upload_window"]
    check(sealed_upload(gate_job) == (window["started_at"], window["completed_at"]),
          "$.producer.upload_window", "wrong tested-record upload window")
    for name in graph.prerequisites(plan, gate_name):
        check(_job_window(find_job(jobs, name, run_attempt=attempt))[1] <= verifier,
              "$.jobs", "gate validation started before its prerequisites completed")

    owning = document["owning_build"]
    for source in document["artifacts"] + ([owning] if owning else []):
        name = grammar.parse_ci_artifact_name(source["artifact"]["name"])
        source_producer, source_jobs = source["producer"], jobs
        if source is owning and document["mode"] != "rebuilt":
            source_jobs = reads.attempt_jobs(source_producer["run_id"], source_producer["run_attempt"])
            check(require_graph(source_jobs, plan=plan, producer="build", mode="full",
                                run_attempt=source_producer["run_attempt"]) == source_producer["graph_sha256"],
                  "$.owning_build.producer.graph_sha256", "wrong owning Build graph")
            for entry in source_jobs:
                check(entry["conclusion"] == "skipped" or _job_window(entry)[1] <= verifier,
                      "$.owning_build.jobs", "owning Build job completed after gate validation started")
        source_job = find_job(source_jobs, upload_job_name(ci_producer(source_producer["workflow_path"]),
                                                           name.kind, name.unit_id),
                              run_attempt=source_producer["run_attempt"])
        expected = source_producer["upload_window"]
        check(sealed_upload(source_job) == (expected["started_at"], expected["completed_at"]),
              "$.source.upload_window", "source window differs from actual API upload")
        check(_job_window(source_job)[1] <= verifier, "$.source", "source job completed after gate validation started")
    return digest
