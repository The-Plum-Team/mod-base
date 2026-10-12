"""The tested generation that covers a default-branch commit, for a consumer (Q9/B6, KQ9-1).

A push to the default branch is covered by the two managed callers' newest runs for that commit:
either they built and ran it in full, or ``ci reuse-admit`` admitted the gates of the merged pull
request and both sealed a reuse reference instead (``mod_base.build_ci.reuse``). Whoever consumes
the result of a push (a status page, a review, coverage, Pages) must not care which: it needs the
one Build bundle and the one set of runtime results that were tested together, authenticated as
strictly as a gate authenticates them. :func:`select_generation` finds and proves that set and
publishes it privately; ``ci select-generation`` is its command.

Selection comes before any result is read. For each managed caller the newest run that GitHub
lists for the commit (a push or a dispatch on the default branch), as its latest attempt:

* a run of either caller that has not completed: the generation is still being decided
  (:class:`GenerationUnavailable`, ``ci-generation-pending``). A consumer started by the other
  caller's run finds it again when this one completes;
* a newest run of either caller that failed or was cancelled: there is no tested generation
  (``ci-generation-failed``), and none of an older run is ever considered;
* no packaged run at all: nothing covers the commit yet (``ci-generation-absent``).

The packaged run decides the mode, and the Build caller's newest run must agree with it:

* ``reuse``: both runs are reuse runs of the push, both references name the same original pair
  (references never chain: a source is always a tested record), each is read as
  ``reuse.download_reuse_reference`` reads it, and the original pair is authenticated once, under
  its own identity and the original plan (``records.original_plan``);
* ``selected``: the packaged run consumed the bundle of the Build caller's newest run, which is a
  full run; both tested records are read for the commit's own plan;
* ``rebuilt``: the packaged run built for itself and sealed both gates, and the Build caller has
  no run for the commit.

Anything else is a mixed generation and a rejection (``ci-generation-mixed``): a reused Build
beside fresh runtime results, a packaged run that tested another Build, two references that name
different pull requests. Every proof of the readers it uses holds: exact graphs, latest attempts,
the kit pin and controller in ``referenced_workflows``, upload windows, gate timelines, the
packaged gate's Build being exactly the Build gate's bundle, every source artifact's metadata,
owner and availability.

Evidence that is gone (an artifact listed as expired, no longer listed, or answered with 404 or
410 by its numeric id) is not corruption: the generation can no longer be proven, and the
consumer runs a full generation on request instead (``ci-generation-evidence-unavailable``, exit
3). Nothing partial is ever published. Everything mutable is observed again immediately before the
output directory is published, which happens atomically or not at all.

What is published is the ``mod-base.ci.generation`` record and, on request, the bytes a consumer
reads: the complete Build bundle the packaged gate tested and the exports of chosen lanes (their
reports, logs and screenshots, as the adapter wrote them), each verified as the gate's own readers
verify it, and each keeping the identity it was built or run under.
"""

from __future__ import annotations

import contextlib
import functools
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci import reuse
from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.graph import require_graph, run_graph, sealed_upload, upload_job_name
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import generation_record, original_plan
from mod_base.build_ci.selection import newest_run, pending_run, producer_record
from mod_base.build_ci.transport import (ArtifactUnavailable, OriginalUnavailable, _admit_source, _artifact_state,
                                         _descriptor, _listed_artifact, _materialize_build, _merged, _merged_pair,
                                         _plan, _read_gate, _read_results, _read_sealed_lane, _run, artifact_gone)
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError, GitHubApi
from mod_base.github.jobs import job_graph
from mod_base.io.atomic_directory import atomic_directory
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.workflow import CI_CALLER_WORKFLOWS, find_job

#: The file of the published output that holds the ``mod-base.ci.generation`` document.
GENERATION_NAME = "generation.json"
#: The directory of the published output that holds the complete Build bundle, when asked for.
BUILD_DIRECTORY = "build"
#: The directory of the published output that holds the verified export of each lane asked for,
#: one directory per lane id.
LANES_DIRECTORY = "lanes"
#: The events whose runs of a managed caller test a commit of the default branch.
_EVENTS = ("push", "workflow_dispatch")
#: The modes of each managed caller's run that end a push generation, in the order they are tried.
_MODES = {"build": ("full", "reuse"), "packaged": ("selected", "rebuilt", "reuse")}
#: Why no generation is selected, by the token ``ci select-generation`` outputs as ``reason``.
ABSENT_REASONS = {
    "ci-generation-absent": "the packaged caller has no run for this commit yet",
    "ci-generation-pending": "a newest run of a managed caller for this commit has not finished",
    "ci-generation-failed": "a newest run of a managed caller for this commit failed or was cancelled",
    "ci-generation-evidence-unavailable": "an artifact of the generation has expired or is gone; run a full "
                                          "generation on request",
}


class GenerationUnavailable(Unavailable):
    """No tested generation can be selected for the commit (exit 3); ``reason`` is a key of
    ``ABSENT_REASONS``. Nothing is wrong and nothing was published."""

    default_reason = "ci-generation-absent"

    def __init__(self, reason: str, detail: str = "") -> None:
        check(reason in ABSENT_REASONS, "$.reason", "is not a reason for an absent generation")
        super().__init__(ABSENT_REASONS[reason] + (f" ({detail})" if detail else ""), reason=reason)


class MixedGeneration(MbError):
    """The two managed callers' newest runs for the commit do not form one tested generation
    (exit 2): neither result may be consumed alone, and a full generation on request replaces it."""

    default_reason = "ci-generation-mixed"


@dataclass(frozen=True)
class _Newest:
    """The newest run of one managed caller for the commit, as its latest attempt. ``state`` is
    ``absent``, ``pending`` or ``failed``, or the mode whose exact graph the completed successful
    attempt shows; ``listed`` holds the artifacts it lists under kit names of this run, by name."""

    producer: str
    state: str
    run_id: int = 0
    run_attempt: int = 0
    event: str = ""
    jobs: tuple[dict[str, Any], ...] = ()
    listed: dict[str, list[dict[str, Any]]] | None = None

    @property
    def where(self) -> str:
        return f"{self.producer} run {self.run_id} attempt {self.run_attempt}"


@contextlib.contextmanager
def _evidence() -> Iterator[None]:
    """Report evidence of the generation that is gone as :class:`GenerationUnavailable`; every
    other failure passes unchanged (``transport._original_evidence`` for the original pair)."""

    try:
        yield
    except (ArtifactUnavailable, OriginalUnavailable) as error:
        raise GenerationUnavailable("ci-generation-evidence-unavailable", str(error)) from error
    except ApiError as error:
        if not artifact_gone(error):
            raise
        raise GenerationUnavailable("ci-generation-evidence-unavailable",
                                    f"GitHub answers {error.status} for {error.path}") from error


def _newest(reads: CommandReads, watch: Watch, plan: dict[str, Any], producer: str) -> _Newest:
    """Select the newest run of ``producer`` for the commit, whatever its result, then classify its
    latest attempt (:class:`_Newest`)."""

    head_sha, head_branch, head_repository = run_head(plan["identity"])
    newest = watch.read(("newest run", producer), functools.partial(
        newest_run, reads, producer, head_sha=head_sha, head_branch=head_branch, head_repository=head_repository,
        events=_EVENTS))
    if newest is None:
        return _Newest(producer, "absent")
    if newest["status"] != "completed":
        pending_run(newest)
        return _Newest(producer, "pending", newest["id"], newest["run_attempt"])
    run = _run(reads, watch, newest["id"])
    check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
          and run["run_attempt"] >= newest["run_attempt"], "$.run", "run listing and run disagree")
    check((run["path"], run["event"], run["head_sha"], run["head_branch"], run["head_repository"])
          == (CI_CALLER_WORKFLOWS[producer], newest["event"], head_sha, head_branch, head_repository),
          "$.run", "the newest run is not recorded under the commit")
    if run["status"] != "completed":
        pending_run(run)
        return _Newest(producer, "pending", newest["id"], run["run_attempt"])
    check(type(run["conclusion"]) is str, "$.run.conclusion", "a completed run has a conclusion")
    if run["conclusion"] != "success":
        return _Newest(producer, "failed", newest["id"], run["run_attempt"])
    jobs = reads.attempt_jobs(newest["id"], run["run_attempt"])
    graph = job_graph(jobs)
    mode = next((mode for mode in _MODES[producer] if graph == run_graph(producer, mode).jobs(plan)), None)
    if mode is None:
        # The exact error of the full graph says what differs.
        require_graph(jobs, plan=plan, producer=producer, mode=_MODES[producer][0], run_attempt=run["run_attempt"])
        check(False, "$.jobs", "the newest run shows no graph that ends a push generation")
    check(mode != "reuse" or newest["event"] == "push", "$.run.event", "only a push reuses")
    listed: dict[str, list[dict[str, Any]]] = {}
    for row in reads.paginate(f"/repos/{reads.repository}/actions/runs/{newest['id']}/artifacts", field="artifacts",
                              max_items=limits.MAX_CI_ARTIFACTS_PER_GATE):
        name = grammar.parse_ci_artifact_name(row.get("name"))
        if name is not None and name.run_id == newest["id"]:
            listed.setdefault(name.name, []).append(row)
    return _Newest(producer, mode, newest["id"], run["run_attempt"], newest["event"], tuple(jobs), listed)


def _decide(build: _Newest, packaged: _Newest) -> str:
    """The mode of the generation the two newest runs form; see the module."""

    states = (build.state, packaged.state)
    for state, reason in (("pending", "ci-generation-pending"), ("failed", "ci-generation-failed")):
        if state in states:
            where = build if build.state == state else packaged
            raise GenerationUnavailable(reason, where.where)
    if packaged.state == "absent":
        raise GenerationUnavailable("ci-generation-absent")
    expected = {"reuse": "reuse", "selected": "full", "rebuilt": "absent"}[packaged.state]
    if build.state != expected:
        raise MixedGeneration(f"the packaged run {packaged.run_id} is a {packaged.state} run, but the Build caller's "
                              f"newest run for the commit is {build.state} ({build.where}); run a full generation "
                              "on request")
    return packaged.state


def _describe(reads: CommandReads, watch: Watch, plan: dict[str, Any], run: _Newest, mode: str, kind: str,
              unit_id: str | None) -> dict[str, Any]:
    """The descriptor of the one ``kind`` artifact of a selected run's attempt in ``mode``.

    The run must show exactly the graph of that mode; the window is its uploading job's. An
    artifact that is not listed or has expired is gone (``transport.OriginalUnavailable``); one
    listed twice or under another run or head is a rejection. Its availability joins ``watch``
    from the row it was described from."""

    digest = require_graph(list(run.jobs), plan=plan, producer=run.producer, mode=mode, run_attempt=run.run_attempt)
    producer = producer_record(plan, caller=run.producer, run_id=run.run_id, run_attempt=run.run_attempt,
                               event=run.event, graph_sha256=digest)
    name = grammar.ci_artifact_name(kind, run.run_id, run.run_attempt, unit_id)
    rows = (run.listed or {}).get(name, [])
    if not rows:
        raise OriginalUnavailable(f"{run.where} no longer lists {name}")
    check(len(rows) == 1, "$.artifacts", f"artifact {name!r} is listed more than once")
    state = _artifact_state(rows[0])
    head_sha, head_branch, _ = run_head(plan["identity"])
    check((state["name"], state["run_id"], state["head_sha"], state["head_branch"])
          == (name, run.run_id, head_sha, head_branch), "$.artifacts",
          f"artifact {name!r} is not recorded under its run and the commit")
    if state["expired"]:
        raise OriginalUnavailable(f"{name} has expired")
    started, completed = sealed_upload(find_job(list(run.jobs), upload_job_name(run.producer, kind, unit_id),
                                                run_attempt=run.run_attempt))
    _listed_artifact(reads, watch, run.run_id, state)
    return _descriptor({
        "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
        "producer": {**producer, "upload_window": {"started_at": started, "completed_at": completed}},
        "artifact": {key: state[key] for key in ("id", "name", "digest", "size", "created_at", "expires_at")}})


def _full(reads: CommandReads, watch: Watch, plan: dict[str, Any], build: _Newest, packaged: _Newest, mode: str,
          temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """``(source, Build receipt, packaged receipt)`` of a generation the commit's own runs tested."""

    sealer = packaged if mode == "rebuilt" else build
    build_seal = _describe(reads, watch, plan, sealer, "full" if sealer is build else mode, "tested", "build")
    packaged_seal = _describe(reads, watch, plan, packaged, mode, "tested", "packaged")
    build_receipt = _read_gate(reads, watch, build_seal, plan, "build", temporary_root)
    packaged_receipt = _read_gate(reads, watch, packaged_seal, plan, "packaged", temporary_root)
    if packaged_receipt["owning_build"] != build_receipt["artifacts"][0]:
        raise MixedGeneration("the packaged gate of the commit consumed another Build than its Build gate sealed")
    source = {"identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
              "build_seal": build_seal, "packaged_seal": packaged_seal}
    return source, build_receipt, packaged_receipt


def _reused(reads: CommandReads, watch: Watch, originals: Watch, plan: dict[str, Any], build: _Newest,
            packaged: _Newest, temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any],
                                                             dict[str, Any]]:
    """``(references, source, Build receipt, packaged receipt)`` of a generation both callers
    reused: each reference as ``reuse.download_reuse_reference`` reads it, the original pair once."""

    references, verifiers, documents = {}, [], []
    for run in (build, packaged):
        descriptor = _describe(reads, watch, plan, run, "reuse", "reuse", None)
        document, verifier = reuse._read_reference(reads, watch, descriptor, plan, temporary_root)
        references[run.producer] = descriptor
        verifiers.append(verifier)
        documents.append(document)
    source = documents[0]["source"]
    if documents[1]["source"] != source:
        raise MixedGeneration("the reuse runs of the commit name different original generations")
    covered = plan["identity"]
    original = original_plan(plan, source["identity"])
    receipts = _merged_pair(reads, originals, build_descriptor=source["build_seal"],
                            packaged_descriptor=source["packaged_seal"], plan=original,
                            merged=_merged(original, covered["controller_sha"], covered["tested_sha"]),
                            temporary_root=temporary_root)
    check("scope_sha256" not in receipts[1], "$.source.packaged_seal",
          "a reuse reference never covers a push with a narrowed pull request run")
    reuse._originals_finished(reads, source, min(verifiers))
    return references, source, *receipts


def _lanes(receipt: dict[str, Any], plan: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The results index and each planned lane's runtime descriptor of a packaged receipt."""

    by_unit, results = {}, None
    for descriptor in receipt["artifacts"]:
        name = grammar.parse_ci_artifact_name(descriptor["artifact"]["name"])
        if name.kind == "results":
            results = descriptor
        else:
            by_unit[name.unit_id] = descriptor
    check(results is not None and sorted(by_unit) == sorted(lane["id"] for lane in plan["lanes"]),
          "$.packaged.artifacts", "the packaged receipt does not cover the plan")
    return results, [by_unit[lane["id"]] for lane in plan["lanes"]]


def _publish_lanes(reads: CommandReads, root: Path, temporary_root: Path, *, wanted: tuple[str, ...],
                   tested: dict[str, Any], packaged: dict[str, Any], results: dict[str, Any],
                   lanes: list[dict[str, Any]], bundle: dict[str, Any], source_config_sha256: str) -> None:
    """Verify and keep the exports of the lanes ``wanted`` under ``root``, one directory per lane.

    The results index is read by id and must list exactly the lanes of the packaged receipt, own
    exactly its Build and carry exactly its native receipts. Each lane is then read as the
    aggregating job read it (``transport._read_sealed_lane``): its export bound to its descriptor
    and to the Build, its validation record frozen for those bytes under the protected config,
    and the hashes of its envelope, validation record and report must be the ones the index
    sealed."""

    index = _read_results(reads, results, tested, temporary_root)
    check([lane["descriptor"] for lane in index["lanes"]] == lanes and index["owning_build"] == bundle,
          "$.results", "the results index lists other lanes or another Build than the packaged gate")
    check([(lane["id"], lane["report_sha256"]) for lane in index["lanes"]]
          == [(receipt["unit_id"], receipt["report_sha256"]) for receipt in packaged["native_receipts"]],
          "$.results.lanes", "the results index and the packaged gate disagree on a lane's report")
    root.mkdir(mode=0o700)
    for lane in index["lanes"]:
        if lane["id"] not in wanted:
            continue
        hashes = _read_sealed_lane(reads, lane["descriptor"], tested, owning_build=bundle,
                                   build_envelope_sha256=index["build_envelope_sha256"],
                                   source_config_sha256=source_config_sha256, temporary_root=temporary_root,
                                   keep=root / lane["id"])
        check(hashes == {key: lane[key] for key in hashes}, f"$.lanes.{lane['id']}",
              "the lane's export is not the one its results index sealed")


def select_generation(api: GitHubApi | CommandReads, *, plan: dict[str, Any], output: Path,
                      with_build: bool = False, lanes: tuple[str, ...] = (),
                      source_config_sha256: str | None = None) -> dict[str, Any]:
    """Select, authenticate and privately publish the tested generation that covers the live
    default-branch commit of ``plan`` (see the module); return its ``mod-base.ci.generation``.

    ``plan`` is the consumer's own protected plan of the commit, derived in a protected job
    (``ci subject``, ``ci plan``), and the default branch must still be at that commit. ``output``
    must not exist; its parent is private to the caller. It receives ``generation.json`` and, with
    ``with_build``, the complete Build bundle the packaged gate tested under ``build/``, verified
    against its validation record (``source_config_sha256`` is the digest of the protected Build
    config the caller loaded, which the policy digest binds in both modes). ``lanes`` names planned
    lanes whose verified exports (their files and runtime envelope) are kept under
    ``lanes/<id>/``, each checked against the results index (:func:`_publish_lanes`); it needs
    ``source_config_sha256``. A reused bundle or lane keeps its original identity: nothing is
    rewritten to pretend it was built or run after the merge.

    Raises :class:`GenerationUnavailable` (exit 3) when no generation can be selected or its
    evidence is gone, :class:`MixedGeneration` when the two callers disagree, and the readers'
    rejections for everything that is wrong. Nothing is published unless everything held."""

    plan = _plan(plan)
    check(plan["identity"]["pr_number"] == 0, "$.identity.pr_number",
          "a generation covers a commit of the default branch, never a pull request")
    check(isinstance(output, Path) and not os.path.lexists(output) and output.parent.is_dir(), "$.output",
          "invalid or preexisting output, or no directory to hold it")
    check(type(with_build) is bool, "$.with_build", "must be a boolean")
    planned = [lane["id"] for lane in plan["lanes"]]
    check(type(lanes) is tuple and len(set(lanes)) == len(lanes) and all(lane in planned for lane in lanes),
          "$.lanes", "must name distinct planned lanes")
    check(not lanes or type(source_config_sha256) is str, "$.source_config_sha256",
          "a lane is verified under the protected Build config the caller loaded")
    reads, watch, originals = CommandReads.of(api), Watch(), Watch()
    _admit_source(reads, watch, plan["identity"])
    build, packaged = (_newest(reads, watch, plan, producer) for producer in PRODUCERS)
    mode = _decide(build, packaged)
    with tempfile.TemporaryDirectory(prefix="mb-ci-generation-", dir=output.parent) as temporary:
        with _evidence():
            if mode == "reuse":
                references, source, build_receipt, packaged_receipt = _reused(
                    reads, watch, originals, plan, build, packaged, Path(temporary))
                tested = original_plan(plan, source["identity"])
            else:
                references = None
                source, build_receipt, packaged_receipt = _full(reads, watch, plan, build, packaged, mode,
                                                                Path(temporary))
                tested = plan
    results, runtime = _lanes(packaged_receipt, tested)
    bundle = packaged_receipt["owning_build"]
    document = generation_record(plan=plan, mode="reuse" if mode == "reuse" else "full", references=references,
                                 source=source, build=bundle, results=results, lanes=runtime)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        if with_build:
            with _evidence():
                _materialize_build(reads, bundle, tested, stage / BUILD_DIRECTORY, None, source_config_sha256)
        if lanes:
            with tempfile.TemporaryDirectory(prefix="mb-ci-lanes-", dir=stage) as temporary, _evidence():
                _publish_lanes(reads, stage / LANES_DIRECTORY, Path(temporary), wanted=lanes, tested=tested,
                               packaged=packaged_receipt, results=results, lanes=runtime, bundle=bundle,
                               source_config_sha256=source_config_sha256)
        descriptor = os.open(GENERATION_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=stage_fd)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json(document))
        # Evidence that is gone by now is as unavailable as evidence that was gone at the start.
        with _evidence():
            watch.recheck()
            originals.recheck()
        return document

    try:
        return atomic_directory(output, writer)
    except OSError as error:
        raise MbError("cannot publish the private generation", reason="ci-transport") from error
