"""The artifacts an attempt has uploaded so far, described for a job of that same attempt (MB11).

A fan-in or gate job reads artifacts of the run it is itself a job of. That run is still in
progress, so there is nothing to select and no completed graph to require: the job knows its own
run and attempt, and the mode of the run fixes which jobs have finished before it and what each
of them uploaded. This module turns that into canonical descriptors
(``records.validate_descriptor``), with the content a consumer of a finished run gets from
``selection``:

* the attempt's job listing is read once. Every job that must have finished shows the conclusion
  the graph expects (``graph.require_partial_graph``), and every job that uploaded an expected
  artifact sealed before it uploaded; its upload step is the descriptor's window;
* the run's artifact listing is read once. Every expected name (``grammar.ci_artifact_name``)
  must be there exactly once, unexpired, within the size cap of its kind and recorded under this
  run and the subject's head; no other artifact of this attempt may carry an expected kind.

A failed-jobs-only rerun leaves jobs and artifacts of an earlier attempt behind. GitHub lists a
job it did not run again under the new attempt, but with the start time of the attempt that ran
it, before the new attempt started. Both are rejections that say so: the recovery is to rerun all
jobs, never to mix attempts.

Nothing here proves that the remaining jobs succeed, that this is the run's latest attempt or
that the bytes are what the plan expects: the command that calls this authenticates the run and
the source, its transport the bytes, and the reader of the gate the complete graph.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.graph import require_partial_graph, run_graph, sealed_upload, upload_job_name
from mod_base.build_ci.identity import validate_subject_record
from mod_base.build_ci.protocol import subject_of
from mod_base.build_ci.reads import RERUN, CommandReads
from mod_base.build_ci.records import _PRODUCER_IDENTITY, validate_descriptor
from mod_base.build_ci.transport import _artifact_state, _plan
from mod_base.github.api import GitHubApi
from mod_base.model import grammar, limits
from mod_base.model.validators import Int, check, fail
from mod_base.workflow import ci_producer, find_job

#: The artifact kinds that hold one small canonical record; every other kind holds an export.
_RECORD_KINDS = ("results", "tested", "reuse")
#: The gate whose receipt covers the artifact a fan-in job uploads.
_GATES = {"build": "build", "results": "packaged"}


def attempt_producer(record: dict[str, Any], plan: dict[str, Any], *, mode: str, run_id: int,
                     run_attempt: int) -> dict[str, Any]:
    """The producer identity that every record and descriptor of this attempt carries.

    ``record`` is the job's identity record (``identity.read_subject``): the managed caller and
    the event of the run that is executing. ``mode`` is the graph mode the run is authenticated
    in; its digest for ``plan`` becomes ``graph_sha256``. The result has no upload window."""

    validate_subject_record(record)
    plan = _plan(plan)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    identity, path = plan["identity"], record["workflow_path"]
    check(subject_of(identity) == record["subject"], "$.plan.identity", "the plan belongs to another subject")
    return {"run_id": run_id, "run_attempt": run_attempt, "workflow_path": path,
            "workflow_ref": grammar.workflow_ref(identity["repository"], path, identity["base_branch"]),
            "api_head_sha": run_head(identity)[0], "event": record["event"],
            "graph_sha256": run_graph(ci_producer(path), mode).sha256(plan)}


def settled_jobs(producer: str, mode: str, plan: dict[str, Any], kind: str,
                 unit_id: str | None = None) -> list[str]:
    """The jobs of a run of ``producer`` in ``mode`` that have finished when the job that uploads
    the ``kind`` artifact seals it: every job that succeeds in its own call or in a call the caller
    makes earlier, except itself and the gate that follows it, and every job the mode skips.

    ``kind`` is ``build`` (the assembling job), ``results`` (the aggregating job) or ``tested``
    with the gate as ``unit_id``. A gate that seals a reuse reference is the same job."""

    check(kind == "tested" or (kind in _GATES and unit_id is None), "$.kind",
          "only a fan-in job or a gate reads its own attempt")
    graph = run_graph(producer, mode)
    sealing = upload_job_name(producer, kind, unit_id)
    gate = upload_job_name(producer, "tested", unit_id if kind == "tested" else _GATES[kind])
    skipped = [entry["name"] for entry in graph.jobs(plan) if entry["conclusion"] == "skipped"]
    return [*(name for name in graph.prerequisites(plan, gate) if name != sealing), *skipped]


def settled_artifacts(producer: str, mode: str, plan: dict[str, Any], kind: str,
                      unit_id: str | None = None) -> list[tuple[str, str | None]]:
    """``(kind, unit_id)`` of the artifact every sealing job among :func:`settled_jobs` has
    uploaded, in the order the run produces them: targets, the complete Build and its tested
    record, then lanes and the results index."""

    sealed = set(run_graph(producer, mode).sealed_jobs(plan)) & set(settled_jobs(producer, mode, plan, kind, unit_id))
    units: list[tuple[str, str | None]] = [*(("target", target["id"]) for target in plan["targets"]),
                                           ("build", None), ("tested", "build")]
    if producer == "packaged":
        units += [*(("runtime", lane["id"]) for lane in plan["lanes"]), ("results", None), ("tested", "packaged")]
    return [unit for unit in units if upload_job_name(producer, *unit) in sealed]


def attempt_jobs(api: GitHubApi | CommandReads, run_id: int, run_attempt: int) -> list[dict[str, Any]]:
    """Every job of one running attempt, read once (``reads.CommandReads.attempt_jobs``). A job
    the listing places in an earlier attempt, or one that started before this attempt did (GitHub
    lists a job it carried over into a rerun of failed jobs under the new attempt), is what a
    failed-jobs-only rerun leaves behind and is refused as that."""

    return CommandReads.of(api).attempt_jobs(run_id, run_attempt)


def _size_cap(kind: str) -> int:
    return limits.MAX_CI_RECORD_BYTES if kind in _RECORD_KINDS else limits.MAX_CI_BUNDLE_COMPRESSED_BYTES


def _artifacts(reads: CommandReads, producer: dict[str, Any], plan: dict[str, Any],
               expected: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    """The API state of every expected artifact, from one listing of the run's artifacts."""

    run_id, attempt = producer["run_id"], producer["run_attempt"]
    head_sha, head_branch, _ = run_head(plan["identity"])
    wanted = {grammar.ci_artifact_name(kind, run_id, attempt, unit_id): (kind, unit_id) for kind, unit_id in expected}
    rows = reads.paginate(f"/repos/{reads.repository}/actions/runs/{run_id}/artifacts", field="artifacts",
                          max_items=limits.MAX_CI_ARTIFACTS_PER_GATE)
    listed: dict[str, list[dict[str, Any]]] = {}
    names = []
    for row in rows:
        name = grammar.parse_ci_artifact_name(row.get("name") if type(row) is dict else None)
        if name is not None and name.run_id == run_id:
            listed.setdefault(name.name, []).append(row)
            names.append(name)
    kinds = {kind for kind, _ in expected}
    for name in names:
        check(name.run_attempt != attempt or name.kind not in kinds or name.name in wanted, "$.artifacts",
              f"artifact {name.name!r} is not one the plan expects of this attempt")
    states = []
    for wanted_name, unit in wanted.items():
        found = listed.get(wanted_name, [])
        if not found:
            earlier = sorted(name.name for name in names
                             if (name.kind, name.unit_id) == unit and name.run_attempt != attempt)
            if earlier:
                raise fail("$.artifacts", f"artifact {earlier[-1]!r} is not of attempt {attempt}: {RERUN}")
            raise fail("$.artifacts", f"this attempt has no artifact {wanted_name!r}")
        check(len(found) == 1, "$.artifacts", f"artifact {wanted_name!r} is listed more than once")
        state = _artifact_state(found[0])
        check(state["name"] == wanted_name and not state["expired"], "$.artifacts",
              f"artifact {wanted_name!r} has expired")
        check(state["size"] <= _size_cap(unit[0]), "$.artifacts",
              f"artifact {wanted_name!r} exceeds the size cap of its kind")
        check(state["run_id"] == run_id and state["head_sha"] == head_sha and state["head_branch"] == head_branch,
              "$.artifacts", f"artifact {wanted_name!r} is not recorded under this run and the subject's head")
        states.append(state)
    return states


def describe_attempt(api: GitHubApi | CommandReads, *, producer: dict[str, Any], plan: dict[str, Any], mode: str,
                     expected: Sequence[tuple[str, str | None]], finished: Sequence[str] = ()) -> list[dict[str, Any]]:
    """Describe the ``expected`` artifacts of one running attempt, in the order given.

    ``producer`` is the attempt's producer identity (:func:`attempt_producer`) and ``mode`` the
    graph mode its digest stands for. ``expected`` holds distinct ``(kind, unit_id)`` pairs;
    ``finished`` names further jobs that must have finished as the graph expects, beside the
    jobs that uploaded the expected artifacts (:func:`settled_jobs`). Two requests for a run of
    up to 100 jobs and 100 artifacts, and one more for the attempt's record when ``api`` has not
    yet read the run as this attempt (:meth:`CommandReads.attempt_started`); with nothing
    expected only the jobs are read and required. One observation: a caller that produces an
    effect describes again immediately before it and requires the same answer."""

    reads = CommandReads.of(api)
    plan = _plan(plan)
    producer = copy.deepcopy(_PRODUCER_IDENTITY(producer, "$.producer"))
    caller = ci_producer(producer["workflow_path"])
    check(producer["graph_sha256"] == run_graph(caller, mode).sha256(plan), "$.producer.graph_sha256",
          "is not the graph of this run in that mode")
    expected = [(kind, unit_id) for kind, unit_id in expected]
    check(len(set(expected)) == len(expected), "$.expected", "must name distinct artifacts")
    uploads = [upload_job_name(caller, kind, unit_id) for kind, unit_id in expected]
    run_id, attempt = producer["run_id"], producer["run_attempt"]
    jobs = attempt_jobs(reads, run_id, attempt)
    require_partial_graph(jobs, plan=plan, producer=caller, mode=mode, run_attempt=attempt,
                          finished=list(dict.fromkeys([*finished, *uploads])))
    descriptors = []
    for state, upload in zip(_artifacts(reads, producer, plan, expected) if expected else [], uploads):
        started, completed = sealed_upload(find_job(jobs, upload, run_attempt=attempt))
        descriptors.append(validate_descriptor({
            "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
            "producer": {**producer, "upload_window": {"started_at": started, "completed_at": completed}},
            "artifact": {key: state[key] for key in ("id", "name", "digest", "size", "created_at", "expires_at")},
        }))
    return descriptors
