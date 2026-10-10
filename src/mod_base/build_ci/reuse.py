"""Post-merge reuse: a push to the default branch that its pull request already tested (MB11).

When a pull request merges, the commit that lands on the default branch usually has the very tree
its test merge had. Building and running that tree again proves nothing new, so the push may be
covered by the two gates of the pull request instead. :func:`admit_post_merge_reuse` decides
that, in the plan job of the push and again in its gate job.

The decision has three outcomes, and each means one thing:

* :class:`ReuseAdmitted`: every proof below holds. The run skips its workers and its gate seals a
  reuse reference (``gate.seal_reuse``).
* :class:`FullRunRequired`: an ordinary and safe reason to test again, named by one of
  ``FULL_RUN_REASONS``. Nothing is wrong and nothing is trusted. Any event but a push is one: a
  dispatch, a schedule or a release is a request to build and run in full.
* an error, which stops the job: an API failure, malformed or ambiguous metadata, a digest or graph
  that differs from what an authenticated record says, an original run that has not finished
  (:class:`OriginalPending`: a full run must not race an original whose result is unknown).

What admission requires, in the order it is established:

1. The pushed commit is the final commit of exactly one merged pull request of this repository
   into the default branch, found from the commit itself.
2. The newest Build run and the newest packaged run of that pull request's last head, chosen
   before any result is read, have completed successfully as their latest attempts under the
   kit pin that is executing now. Neither is a draft deferral or itself a reuse run, and neither
   lists a reuse reference: references never chain.
3. The Build record says which test merge it was sealed for. That is a claim, and everything in it
   is checked: the complete tree, the protected policy digest, the kit pin, the inventory, the
   scenario contract and the runtime selection equal those of the push, and the plan hash is the
   hash of the push's own plan under the original identity (``records.original_plan``), so the
   original gates were sealed for exactly the work the push would do.
4. Both runs show exactly the full graph of their caller for that plan, and both tested records
   read and pair coherently, as ``transport.download_merged_gate_pair`` proves it: the merged pull
   request and its history, the ordered parents of the original test merge, the equal trees, each
   run, seal, upload window and timeline, and the packaged gate's Build being exactly the Build
   gate's bundle.
5. Every artifact both records name is still available. One that has expired or is gone is a
   reason for a full run; one that is still there and differs is an error.

A reference covers the merged commit and names the original identity, runs, attempts and
artifacts separately. It renews no retention: the original artifacts expire when they would have.
:func:`download_reuse_reference` reads one back for a consumer that trusts a reused generation: it
authenticates the reuse run, refuses a run that mixes a reference with evidence of its own, and
authenticates the original pair again.
"""

from __future__ import annotations

import functools
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mod_base.build_ci.graph import (_job_window, _step_window, require_graph, run_graph, sealed_upload,
                                     upload_job_name)
from mod_base.build_ci.identity import PROTECTED_EVENTS, PULL_REQUEST_EVENT
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import _REUSE_SEMANTICS, bind_reuse_reference, original_plan
from mod_base.build_ci.selection import newest_run, pending_run, producer_record
from mod_base.build_ci.transport import (OriginalUnavailable, _admit_source, _artifact_state, _authenticate_artifacts,
                                         _authenticate_run, _authenticate_upload, _bound, _complete_jobs,
                                         _descriptor, _download, _merged, _merged_pair, _original_evidence, _plan,
                                         _run)
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.jobs import job_graph
from mod_base.io.bounded_zip import ExtractionLimits, extract
from mod_base.io.tree import read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from mod_base.model.validators import Int, check
from mod_base.workflow import CI_CALLER_WORKFLOWS, CI_SEAL_STEP, ci_producer, find_job

#: Why a push builds and runs in full, by the token ``ci reuse-admit`` outputs as ``reason``.
FULL_RUN_REASONS = {
    "not-a-push": "only a push to the default branch reuses; a dispatch, a schedule and a pull request test in full",
    "no-merged-pull-request": "no merged pull request of this repository ends in the pushed commit",
    "several-merged-pull-requests": "more than one merged pull request ends in the pushed commit",
    "foreign-pull-request": "the merged pull request came from another repository, whose runs are never protected",
    "no-original-run": "the pull request has no run of a managed caller for its last head",
    "original-run-failed": "the newest original run failed or was cancelled",
    "original-run-deferred": "the newest original run deferred a draft and tested nothing",
    "original-run-reused": "the newest original run is itself a reuse run, and references never chain",
    "original-run-of-another-pull-request": "the newest original run of that head tested another pull request",
    "kit-pin-differs": "the original runs executed another kit pin than this push",
    "merged-tree-differs": "the merged commit does not have the tree the pull request was tested with",
    "policy-differs": "the protected Build policy of the push is not the one the pull request was tested under",
    "inventory-differs": "the inventory of the push is not the one the pull request was tested with",
    "scenario-contract-differs": "the scenario contract of the push is not the one the pull request was tested with",
    "runtime-selection-differs": "the runtime selection of the push is not the one the pull request ran",
    "plan-differs": "the original gates were sealed for another plan than the push derives",
    "original-evidence-unavailable": "an original artifact has expired or is gone",
}
#: The ``reason`` of an admitted reuse.
ADMITTED_REASON = "identical-tested-tree"
#: What the push and the original test merge must have in common, as the design lists it, with the
#: reason a difference gives. The graph protocol is part of the policy digest and of the pin, and
#: everything else a plan holds is compared by its hash.
_COMPARED = (
    ("merged-tree-differs", ("tested_tree",)),
    ("policy-differs", ("policy_sha256",)),
    ("kit-pin-differs", ("kit",)),
    ("inventory-differs", ("inventory_blob", "inventory_sha256")),
    ("scenario-contract-differs", ("scenario_sha256",)),
    ("runtime-selection-differs", ("runtime_selection_sha256",)),
)
#: The mode in which a pull request's run of each managed caller ends in its gate.
_GATE_MODE = {"build": "full", "packaged": "pull-request"}


class OriginalPending(MbError):
    """An original run of the merged pull request has not finished (exit 2).

    Reuse is not admitted and a full run is not started either: it would race evidence whose
    result is unknown. The job stops; rerun it when the original run has finished."""

    default_reason = "ci-original-pending"


class ReuseRefused(MbError):
    """A run that skipped its workers for reuse can no longer be covered by the original gates
    (exit 2). Its gate seals nothing; rerunning all jobs decides again and tests in full."""

    default_reason = "ci-reuse-refused"


@dataclass(frozen=True)
class FullRunRequired:
    """The push builds and runs in full. ``reason`` is a key of ``FULL_RUN_REASONS`` and ``detail``
    one line that says it in words."""

    reason: str
    detail: str

    def __post_init__(self) -> None:
        check(self.reason in FULL_RUN_REASONS, "$.reason", "is not a reason for a full run")


@dataclass(frozen=True)
class ReuseAdmitted:
    """The push is covered by both original gates of pull request ``pr_number``.

    ``source`` is the ``source`` of the reuse reference: the original identity, plan hash and
    profile and the descriptors of both tested records. ``watch`` holds everything mutable the
    admission read; whoever acts on the admission calls :meth:`recheck` immediately before."""

    pr_number: int
    source: dict[str, Any]
    watch: Watch = field(repr=False, compare=False)

    def recheck(self) -> None:
        """Observe the pull request, both runs and every artifact again and require no change."""

        self.watch.recheck()


class _FullRun(Exception):
    """Leaves the admission with a full-run answer. Raised and caught in this module only."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.outcome = FullRunRequired(reason, FULL_RUN_REASONS[reason] + (f" ({detail})" if detail else ""))


@dataclass(frozen=True)
class _Pull:
    number: int
    head_sha: str
    head_branch: str


@dataclass(frozen=True)
class _Original:
    """The newest run of one managed caller under the last head of a merged pull request, as its
    latest attempt: completed, successful, neither a deferral nor a reuse run. ``listed`` holds
    the artifacts the run lists under kit names of this run, by name."""

    producer: str
    run_id: int
    run_attempt: int
    event: str
    head: tuple[str, str]
    jobs: list[dict[str, Any]]
    listed: dict[str, list[dict[str, Any]]]

    def artifact(self, kind: str, unit_id: str | None = None) -> dict[str, Any]:
        """The state of the one ``kind`` artifact of this attempt. One that is not listed or has
        expired is gone (:class:`~mod_base.build_ci.transport.OriginalUnavailable`); one that is
        listed twice, malformed or recorded under another run or head is a rejection."""

        name = grammar.ci_artifact_name(kind, self.run_id, self.run_attempt, unit_id)
        rows = self.listed.get(name, [])
        if not rows:
            raise OriginalUnavailable(f"run {self.run_id} attempt {self.run_attempt} no longer lists {name}")
        check(len(rows) == 1, "$.artifacts", f"artifact {name!r} is listed more than once")
        state = _artifact_state(rows[0])
        check((state["name"], state["run_id"], state["head_sha"], state["head_branch"])
              == (name, self.run_id, *self.head), "$.artifacts",
              f"artifact {name!r} is not recorded under its run and the pull request's head")
        if state["expired"]:
            raise OriginalUnavailable(f"{name} has expired")
        return state


def _read_record(data: bytes, name: str, kind: str, temporary_root: Path, **context: Any) -> dict[str, Any]:
    """The one canonical record ``name`` of a downloaded record archive, validated as ``kind``."""

    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-reuse-", dir=temporary_root) as temporary:
            root = Path(temporary) / "record"
            bounds = ExtractionLimits(1, limits.MAX_CI_RECORD_BYTES, limits.MAX_CI_RECORD_BYTES,
                                      suffixes=frozenset({".json"}))
            check(extract(data, root, bounds) == [name], "$.record", f"the archive must hold exactly {name}")
            raw = read_child_file(root, name, max_bytes=limits.MAX_CI_RECORD_BYTES)
    except OSError as error:
        raise MbError("cannot read a private CI record", reason="ci-transport") from error
    document = load_document(raw, kind=kind, **context)
    check(raw == canonical_json(document), "$.record", f"{name} must be canonical JSON")
    return document


def _side(value: Any) -> dict[str, Any] | None:
    if type(value) is not dict:
        return None
    repository = value.get("repo")
    return {"sha": value.get("sha"), "ref": value.get("ref"),
            "repository": repository.get("full_name") if type(repository) is dict else None}


def _commit_pulls(reads: CommandReads, sha: str) -> list[dict[str, Any]]:
    """The pull requests GitHub associates with a commit, reduced to what the admission reads (a
    pull request's other fields change without changing the answer)."""

    rows = reads.paginate(f"/repos/{reads.repository}/commits/{sha}/pulls", field=None,
                          max_items=limits.MAX_CI_COMMIT_PULLS)
    return [{"number": row.get("number"), "state": row.get("state"), "merged_at": row.get("merged_at"),
             "merge_commit_sha": row.get("merge_commit_sha"), "head": _side(row.get("head")),
             "base": _side(row.get("base"))} for row in rows]


def _merged_pull(reads: CommandReads, watch: Watch, identity: dict[str, Any]) -> _Pull:
    """The one merged pull request of this repository whose final commit is the pushed commit."""

    merged, repository = identity["tested_sha"], identity["repository"]
    rows = watch.read(("pull requests of the merged commit",), functools.partial(_commit_pulls, reads, merged))
    found = []
    for index, row in enumerate(rows):
        here = f"$.pulls[{index}]"
        final, base = row["merge_commit_sha"], row["base"]
        check(final is None or grammar.is_match(grammar.SHA1, final), f"{here}.merge_commit_sha", "malformed commit")
        check(base is not None and type(base["ref"]) is str, f"{here}.base", "malformed pull request base")
        if final != merged or row["merged_at"] is None:
            continue
        grammar.parse_timestamp(row["merged_at"], "merged pull request timestamp")
        if (base["ref"], base["repository"]) == (identity["base_branch"], repository):
            found.append((here, row))
    if not found:
        raise _FullRun("no-merged-pull-request")
    if len(found) > 1:
        raise _FullRun("several-merged-pull-requests")
    here, row = found[0]
    Int(1, limits.MAX_RUN_ID)(row["number"], f"{here}.number")
    head = row["head"]
    check(row["state"] == "closed" and head is not None, here, "a merged pull request is closed and has a head")
    if head["repository"] != repository:
        raise _FullRun("foreign-pull-request", f"#{row['number']}")
    check(grammar.is_match(grammar.SHA1, head["sha"]) and grammar.is_match(grammar.BRANCH, head["ref"]),
          f"{here}.head", "malformed pull request head")
    return _Pull(row["number"], head["sha"], head["ref"])


def _original_run(reads: CommandReads, watch: Watch, producer: str, pull: _Pull, plan: dict[str, Any]) -> _Original:
    """Select the newest run of ``producer`` under the pull request's last head, whatever its
    result, and require its latest attempt to be a successful tested generation of this kit pin."""

    repository, kit = reads.repository, plan["identity"]["kit"]
    newest = watch.read(("newest original run", producer), functools.partial(
        newest_run, reads, producer, head_sha=pull.head_sha, head_branch=pull.head_branch,
        head_repository=repository, events=(PULL_REQUEST_EVENT,)))
    if newest is None:
        raise _FullRun("no-original-run", f"{producer}, pull request #{pull.number}")
    run = newest
    if newest["status"] == "completed":
        run = _run(reads, watch, newest["id"])
        check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
              and run["run_attempt"] >= newest["run_attempt"], "$.run", "run listing and run disagree")
        check((run["path"], run["event"], run["head_sha"], run["head_branch"], run["head_repository"])
              == (CI_CALLER_WORKFLOWS[producer], PULL_REQUEST_EVENT, pull.head_sha, pull.head_branch, repository),
              "$.run", "the newest original run is not recorded under the pull request's head")
    if run["status"] != "completed":
        pending_run(run)
        raise OriginalPending(f"{producer} run {newest['id']} of pull request #{pull.number} has not finished: "
                              "reuse is not admitted and a full run must not race it")
    where = f"{producer} run {newest['id']} attempt {run['run_attempt']}"
    check(type(run["conclusion"]) is str, "$.run.conclusion", "a completed run has a conclusion")
    if run["conclusion"] != "success":
        raise _FullRun("original-run-failed", where)
    pins = {sha for workflow, sha in run["referenced_workflows"]
            if workflow.startswith(f"{kit['repository']}/.github/workflows/")}
    if pins != {kit["sha"]}:
        raise _FullRun("kit-pin-differs", where)
    jobs = reads.attempt_jobs(newest["id"], run["run_attempt"])
    graph = job_graph(jobs)
    for mode, reason in (("deferred", "original-run-deferred"), ("reuse", "original-run-reused")):
        if graph == run_graph(producer, mode).jobs(plan):
            raise _FullRun(reason, where)
    listed: dict[str, list[dict[str, Any]]] = {}
    for row in reads.paginate(f"/repos/{repository}/actions/runs/{newest['id']}/artifacts", field="artifacts",
                              max_items=limits.MAX_CI_ARTIFACTS_PER_GATE):
        name = grammar.parse_ci_artifact_name(row.get("name"))
        if name is None or name.run_id != newest["id"]:
            continue
        if name.kind == "reuse":
            raise _FullRun("original-run-reused", f"{where} lists {name.name}")
        listed.setdefault(name.name, []).append(row)
    return _Original(producer, newest["id"], run["run_attempt"], newest["event"], (pull.head_sha, pull.head_branch),
                     jobs, listed)


def _claimed_receipt(reads: CommandReads, state: dict[str, Any], temporary_root: Path) -> dict[str, Any]:
    """What the Build record of the newest original run says it was sealed for.

    The record is read by id from a run that is so far only selected, so this is a claim: a
    bounded download and a strict parse, never an admission. It is the one place that names the
    original test merge, and the same bytes are read again as evidence once the plan they must
    bind is known."""

    check(state["size"] <= limits.MAX_CI_RECORD_BYTES, "$.artifacts", "the Build record exceeds the record cap")
    with _original_evidence():
        data = _download(reads, {"artifact": state})
    claim = _read_record(data, grammar.CI_GATE_NAME, "mod-base.ci.gate", temporary_root)
    check(claim["gate"] == "build" and claim["identity"]["pr_number"] > 0, "$.record",
          "is not the Build receipt of a pull request")
    return claim


def _seal_descriptor(original: dict[str, Any], run: _Original) -> dict[str, Any]:
    """The descriptor of a run's tested record under the original plan. The run must show exactly
    the full graph of its caller for that plan; the window is its gate's upload step."""

    digest = require_graph(run.jobs, plan=original, producer=run.producer, mode=_GATE_MODE[run.producer],
                           run_attempt=run.run_attempt)
    producer = producer_record(original, caller=run.producer, run_id=run.run_id, run_attempt=run.run_attempt,
                               event=run.event, graph_sha256=digest)
    gate = find_job(run.jobs, upload_job_name(run.producer, "tested", run.producer), run_attempt=run.run_attempt)
    started, completed = sealed_upload(gate)
    state = run.artifact("tested", run.producer)
    return _descriptor({
        "identity": original["identity"], "plan_sha256": original["plan_sha256"], "profile": original["profile"],
        "producer": {**producer, "upload_window": {"started_at": started, "completed_at": completed}},
        "artifact": {key: state[key] for key in ("id", "name", "digest", "size", "created_at", "expires_at")}})


def _admit(reads: CommandReads, watch: Watch, plan: dict[str, Any], temporary_root: Path) -> dict[str, Any]:
    """The ``source`` of the reference that covers the push ``plan`` plans; see the module."""

    now = plan["identity"]
    check(now["repository"] == reads.repository and now["pr_number"] == 0
          and now["tested_sha"] == now["controller_sha"] and now["head_branch"] == now["base_branch"],
          "$.identity", "a reusing push tests the default-branch commit it runs from, in this repository")
    pull = _merged_pull(reads, watch, now)
    build, packaged = (_original_run(reads, watch, producer, pull, plan) for producer in PRODUCERS)

    claim = _claimed_receipt(reads, build.artifact("tested", "build"), temporary_root)
    then = claim["identity"]
    if then["pr_number"] != pull.number:
        raise _FullRun("original-run-of-another-pull-request", f"#{then['pr_number']}, not #{pull.number}")
    compared = {key for _, keys in _COMPARED for key in keys}
    for key in _REUSE_SEMANTICS:
        check(key in compared or then[key] == now[key], f"$.record.identity.{key}",
              "the original Build record is not of this repository's protected caller")
    for reason, keys in _COMPARED:
        if any(then[key] != now[key] for key in keys):
            raise _FullRun(reason, f"pull request #{pull.number}")
    original = original_plan(plan, then)
    if claim["plan_sha256"] != original["plan_sha256"]:
        raise _FullRun("plan-differs", f"pull request #{pull.number}")

    # Everything both records will name, by name: what is gone is known before anything is required.
    packaged.artifact("tested", "packaged")
    build.artifact("build")
    for lane in original["lanes"]:
        packaged.artifact("runtime", lane["id"])
    packaged.artifact("results")

    build_seal, packaged_seal = _seal_descriptor(original, build), _seal_descriptor(original, packaged)
    _merged_pair(reads, watch, build_descriptor=build_seal, packaged_descriptor=packaged_seal, plan=original,
                 merged=_merged(original, now["controller_sha"], now["tested_sha"]), temporary_root=temporary_root)
    return {"identity": original["identity"], "plan_sha256": original["plan_sha256"], "profile": original["profile"],
            "build_seal": build_seal, "packaged_seal": packaged_seal}


def admit_post_merge_reuse(api: GitHubApi | CommandReads, *, plan: dict[str, Any], event: str,
                           temporary_root: Path) -> ReuseAdmitted | FullRunRequired:
    """Decide whether the gates of a merged pull request cover the subject of ``plan``.

    ``plan`` is the protected plan of the job's subject and ``event`` the event of its run, both
    from the job's state; ``temporary_root`` is a private directory in which the two tested
    records are extracted and removed again. Any event but a push answers a full run without a
    request. Returns :class:`ReuseAdmitted` or :class:`FullRunRequired` and raises for everything
    that must stop the job (see the module). Nothing is written: an admission is one observation,
    and its ``recheck`` repeats every mutable read before the caller's effect."""

    plan = _plan(plan)
    check(type(event) is str and event in (PULL_REQUEST_EVENT, *PROTECTED_EVENTS), "$.event",
          "is not an event a managed caller runs for")
    if event != "push":
        return FullRunRequired("not-a-push", FULL_RUN_REASONS["not-a-push"])
    reads, watch = CommandReads.of(api), Watch()
    try:
        source = _admit(reads, watch, plan, temporary_root)
    except _FullRun as full:
        return full.outcome
    except OriginalUnavailable as gone:
        return FullRunRequired("original-evidence-unavailable", str(gone))
    return ReuseAdmitted(source["identity"]["pr_number"], source, watch)


def download_reuse_reference(api: GitHubApi | CommandReads, *, descriptor: dict[str, Any], plan: dict[str, Any],
                             temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read the reuse reference of a finished reuse run and authenticate the generation behind it.

    ``plan`` is the consumer's own protected plan of the live default-branch commit and
    ``descriptor`` selects the ``mb-ci-reuse`` artifact of the run that covers it. Returns
    ``(reference, Build receipt, packaged receipt)``: the two original receipts are read again by
    id and authenticated as a coherent pair of the merged pull request, exactly as at admission.

    The covering run must be the latest attempt of a completed successful push run in exactly the
    reuse graph, and its attempt may list no other evidence of its own: a run that skipped its
    workers and also holds a Build, a lane result or a tested record is a mixed generation and is
    rejected. Its gate must have started sealing after every prerequisite of its own run and every
    job of both original runs had finished. An original artifact that has since expired raises
    ``transport.OriginalUnavailable``: the reference is intact, but its generation can no longer
    be proven. Everything mutable is observed again before return. Whether that run is the newest
    one of its caller for the commit is the consumer's own selection, as for every tested record."""

    plan, descriptor = _plan(plan), _descriptor(descriptor)
    _bound(descriptor, plan, "reuse", None)
    producer, covered = descriptor["producer"], plan["identity"]
    caller = ci_producer(producer["workflow_path"])
    check(producer["event"] == "push" and producer["graph_sha256"] == run_graph(caller, "reuse").sha256(plan),
          "$.producer", "is not a reuse run of a push for this plan")
    reads, watch, originals = CommandReads.of(api), Watch(), Watch()
    _admit_source(reads, watch, covered)
    _authenticate_run(reads, watch, producer, plan, mode="reuse", complete=True)
    jobs = _complete_jobs(reads, producer, plan, mode="reuse")
    _authenticate_upload(descriptor, jobs)
    _authenticate_artifacts(reads, watch, [descriptor], covered)
    run_id, attempt = producer["run_id"], producer["run_attempt"]
    for row in reads.paginate(f"/repos/{reads.repository}/actions/runs/{run_id}/artifacts", field="artifacts",
                              max_items=limits.MAX_CI_ARTIFACTS_PER_GATE):
        name = grammar.parse_ci_artifact_name(row.get("name"))
        check(name is None or (name.run_id, name.run_attempt) != (run_id, attempt)
              or name.name == descriptor["artifact"]["name"], "$.artifacts",
              "a reuse run that lists evidence of its own attempt is a mixed generation")

    document = _read_record(_download(reads, descriptor), grammar.CI_REUSE_NAME, "mod-base.ci.reuse",
                            temporary_root, plan=plan)
    bind_reuse_reference(document, descriptor=descriptor, plan=plan)
    source = document["source"]
    original = original_plan(plan, source["identity"])
    gate_name = upload_job_name(caller, "reuse", None)
    verifier = _step_window(find_job(jobs, gate_name, run_attempt=attempt), CI_SEAL_STEP)[0]
    for name in run_graph(caller, "reuse").prerequisites(plan, gate_name):
        check(_job_window(find_job(jobs, name, run_attempt=attempt))[1] <= verifier, "$.jobs",
              "the reuse gate started before its prerequisites completed")

    receipts = _merged_pair(reads, originals, build_descriptor=source["build_seal"],
                            packaged_descriptor=source["packaged_seal"], plan=original,
                            merged=_merged(original, covered["controller_sha"], covered["tested_sha"]),
                            temporary_root=temporary_root)
    for seal in (source["build_seal"], source["packaged_seal"]):
        for job in reads.attempt_jobs(seal["producer"]["run_id"], seal["producer"]["run_attempt"]):
            check(job["conclusion"] == "skipped" or _job_window(job)[1] <= verifier, "$.source.jobs",
                  "an original job completed after the reuse gate started")
    watch.recheck()
    originals.recheck()
    return document, *receipts
