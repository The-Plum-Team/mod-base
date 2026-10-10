"""What the sealing jobs of a run prove about their own attempt before they seal a record (MB11).

A gate is the last job of its call and runs inside the run it judges. ``ci seal-gate`` therefore
cannot require a completed graph: the gate itself is still running. It authenticates the attempt
as far as it exists (:func:`authenticate_attempt`) and then seals (:func:`seal_gate`), and the
reader of the record later requires the completed graph and the real chronology
(``graph.authenticate_gate_timeline``). The aggregating job of a packaged run does the same one
step earlier and seals the results index its gate reads (:func:`seal_results`).

The mode. The job's identity record fixes which modes can reach this gate at all
(:func:`admissible_modes`): a pull request has exactly one. A protected run has more, and which
calls it makes was decided by its caller before the gate started; the gate reads that decision
from the job names of its own attempt and then requires exactly the graph of that one mode. It
never takes the mode from an input of its own job.

The attempt, in that mode:

* the live source, and the run as the latest attempt, still in progress, under the subject's head
  and bound to the controller commit and kit pin (``referenced_workflows``);
* every job that the graph says has finished before the gate shows its expected conclusion,
  success or skipped, and every sealing job among them sealed before it uploaded;
* every artifact those sealing jobs uploaded is listed once, unexpired and within its window
  (``describe.describe_attempt``).

Then the content. A Build gate downloads the complete Build of its attempt and verifies the
export against the plan and its validation record against the export; the receipt names that
bundle and the verifier's report of every target. A packaged gate downloads the results index,
requires it to list exactly the lane artifacts this attempt uploaded, and authenticates the
owning Build the index names. That Build must be the one the ``input`` job of this very run
attempt selected (the gate job is given that selection record): the Build this run rebuilt, or
the complete bundle of the newest run of the Build caller for the live subject, observed here
once more. A lane reads the selected bytes without asking whether they are still the newest; the
gate is where that question is asked last. The receipt names every lane, the index and that
Build. Everything mutable, that newest-run listing included, is observed once more immediately
before the receipt is returned.

A run in ``reuse`` mode has no export to verify. Its gate decides the reuse again, exactly as
the plan job of the run did, and seals a reuse reference instead (:func:`seal_reuse`): the
covered merge and, separately, both original tested records of its pull request.

The results index. A packaged run's complete results are an index of its lane artifacts, not a
union of their bytes. The aggregating job authenticates its attempt like a gate, with the lanes
as the artifacts, then reads every lane by numeric id, one at a time: the export against the
plan, the lane's validation record against that export, and both against the Build of the
selection record its ``input`` job handed to it, bound to this run attempt. The Build bundle
itself is not read here; the gate authenticates it.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci import describe
from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.identity import validate_subject_record
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import (GATE_MODES, bind_source_selection, gate_receipt, results_index,
                                       reuse_reference)
from mod_base.build_ci.reuse import FullRunRequired, ReuseRefused, admit_post_merge_reuse
from mod_base.build_ci.selection import revalidate_latest_pr_build, revalidate_protected_build
from mod_base.build_ci.transport import (_admit_source, _authenticate_build, _authenticate_run, _bound, _plan,
                                         _read_results, _read_sealed_build, _read_sealed_lane, _run)
from mod_base.github.api import GitHubApi
from mod_base.model import grammar
from mod_base.model.validators import check
from mod_base.workflow import ci_producer


@dataclass(frozen=True)
class Attempt:
    """A running attempt, authenticated for the sealing step of its ``gate`` job in ``mode``.

    ``reads`` and ``watch`` are the reads of the command so far: whoever seals continues with
    them and calls ``watch.recheck()`` immediately before its effect. ``producer`` is the
    attempt's identity without an upload window and ``descriptors`` the artifacts its settled
    sealing jobs uploaded (``describe.settled_artifacts``), in that order."""

    reads: CommandReads
    watch: Watch
    plan: dict[str, Any]
    producer: dict[str, Any]
    gate: str
    mode: str
    descriptors: list[dict[str, Any]]

    def descriptor(self, kind: str, unit_id: str | None = None) -> dict[str, Any]:
        """The descriptor of the ``kind`` artifact of this attempt."""

        name = grammar.ci_artifact_name(kind, self.producer["run_id"], self.producer["run_attempt"], unit_id)
        found = [descriptor for descriptor in self.descriptors if descriptor["artifact"]["name"] == name]
        check(len(found) == 1, "$.artifacts", f"this attempt did not describe {name!r}")
        return found[0]


def admissible_modes(record: dict[str, Any], gate: str) -> tuple[str, ...]:
    """The modes in which a run of the record's caller reaches ``gate`` for the record's subject.

    A pull request has one: the full Build, or the packaged run of a pull request. A protected
    subject never has the pull-request mode; a push to the default branch may also be a reuse run,
    whose gate is the caller's own."""

    validate_subject_record(record)
    check(type(gate) is str and gate in GATE_MODES and record["producer"] == gate, "$.gate",
          "is not the gate of this job")
    caller, pull_request = ci_producer(record["workflow_path"]), bool(record["subject"]["pr_number"])
    modes = [mode for mode in GATE_MODES[gate].get(caller, ())
             if (mode == "pull-request") <= pull_request and (mode in ("selected", "rebuilt")) <= (not pull_request)]
    if record["event"] == "push" and caller == gate:
        modes.append("reuse")
    return tuple(modes)


def _shown_mode(reads: CommandReads, record: dict[str, Any], plan: dict[str, Any], modes: tuple[str, ...],
                run_id: int, run_attempt: int) -> str:
    """The one of the admissible ``modes`` whose graph holds every job this attempt lists."""

    check(bool(modes), "$.mode", "no run of this caller reaches that job for this subject")
    if len(modes) == 1:
        return modes[0]
    caller = ci_producer(record["workflow_path"])
    listed = {job["name"] for job in describe.attempt_jobs(reads, run_id, run_attempt)}
    shown = [mode for mode in modes if listed <= {entry["name"] for entry in run_graph(caller, mode).jobs(plan)}]
    check(len(shown) == 1, "$.jobs", f"this attempt lists the jobs of no single mode of {', '.join(modes)}")
    return shown[0]


def authenticate_attempt(api: GitHubApi | CommandReads, *, record: dict[str, Any], plan: dict[str, Any], gate: str,
                         run_id: int, run_attempt: int) -> Attempt:
    """Authenticate the running attempt whose ``gate`` job calls this, in the mode it shows.

    ``record`` is the job's identity record and ``plan`` the plan of that subject. Returns the
    attempt with its settled artifacts described, or raises: nothing is sealed here."""

    plan = _plan(plan)
    reads, watch = CommandReads.of(api), Watch()
    modes = admissible_modes(record, gate)
    caller = ci_producer(record["workflow_path"])
    _admit_source(reads, watch, plan["identity"])
    # The run is observed before its jobs are read: its record says when this attempt started.
    _run(reads, watch, run_id)
    mode = _shown_mode(reads, record, plan, modes, run_id, run_attempt)
    producer = describe.attempt_producer(record, plan, mode=mode, run_id=run_id, run_attempt=run_attempt)
    _authenticate_run(reads, watch, producer, plan, mode=mode, complete=False)
    descriptors = watch.read(("jobs and artifacts of this attempt", run_id, run_attempt), functools.partial(
        describe.describe_attempt, reads, producer=producer, plan=plan, mode=mode,
        expected=describe.settled_artifacts(caller, mode, plan, "tested", gate),
        finished=describe.settled_jobs(caller, mode, plan, "tested", gate)))
    return Attempt(reads, watch, plan, producer, gate, mode, descriptors)


def _build_gate(attempt: Attempt, config_sha256: str, temporary_root: Path) -> dict[str, Any]:
    build = attempt.descriptor("build")
    _, validation = _read_sealed_build(attempt.reads, build, attempt.plan, source_config_sha256=config_sha256,
                                       temporary_root=temporary_root)
    receipts = [{"unit_id": report["unit_id"], "native_contract_sha256": report["native_contract_sha256"],
                 "report_sha256": report["sha256"]} for report in validation["reports"]]
    return gate_receipt(plan=attempt.plan, producer=attempt.producer, gate="build", mode=attempt.mode,
                        artifacts=[build], owning_build=None, native_receipts=receipts)


def _packaged_gate(attempt: Attempt, selection: dict[str, Any] | None, temporary_root: Path) -> dict[str, Any]:
    plan, producer = attempt.plan, attempt.producer
    check(selection is not None, "$.selection", "a packaged gate judges the Build its input job selected")
    selection = bind_source_selection(selection, plan=plan, run_id=producer["run_id"],
                                      run_attempt=producer["run_attempt"], workflow_path=producer["workflow_path"])
    results = attempt.descriptor("results")
    lanes = [attempt.descriptor("runtime", lane["id"]) for lane in plan["lanes"]]
    index = _read_results(attempt.reads, results, plan, temporary_root)
    check([lane["descriptor"] for lane in index["lanes"]] == lanes, "$.results.lanes",
          "the results index lists other lane artifacts than this attempt uploaded")
    owning = index["owning_build"]
    check((owning, index["build_envelope_sha256"]) == (selection["build"], selection["envelope_sha256"]),
          "$.results.owning_build", "the results index owns another Build than this run attempt selected")
    if attempt.mode == "rebuilt":
        check(owning == attempt.descriptor("build"), "$.results.owning_build",
              "the results index does not own the Build this run rebuilt")
    else:
        _bound(owning, plan, "build", None)
        check(ci_producer(owning["producer"]["workflow_path"]) == "build", "$.results.owning_build",
              "this mode consumes the Build of a separate run of the Build caller")
        # No lane asked whether a newer Build exists. The gate asks, and the listing it reads joins
        # what is observed again before the receipt is returned.
        revalidate = revalidate_latest_pr_build if plan["identity"]["pr_number"] else revalidate_protected_build
        revalidate(attempt.reads, descriptor=owning, plan=plan, watch=attempt.watch)
        _authenticate_build(attempt.reads, attempt.watch, owning, plan)
    receipts = [{"unit_id": lane["id"], "native_contract_sha256": lane["native_contract_sha256"],
                 "report_sha256": lane["report_sha256"]} for lane in index["lanes"]]
    return gate_receipt(plan=plan, producer=attempt.producer, gate="packaged", mode=attempt.mode,
                        artifacts=[*lanes, results], owning_build=owning, native_receipts=receipts)


def seal_gate(attempt: Attempt, *, config_sha256: str, temporary_root: Path,
              selection: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    """Verify what the gate of an authenticated attempt judges and return its receipt as
    ``(file name, document)``, ready to be written as the one file of the tested record.

    ``config_sha256`` is the digest of the protected Build config this job loaded: the validation
    record of a Build must have been frozen under it. ``selection`` is the selection record the
    ``input`` job of this run attempt handed to a packaged gate job, and None for a Build gate.
    The results index must own exactly the Build of that record and, unless this run rebuilt it,
    that Build must still be the newest exact Build of the live subject. ``temporary_root`` is a
    private directory for the downloads, which are removed again. The attempt's watch is
    rechecked before return."""

    check(attempt.mode != "reuse", "$.mode", "a reuse run seals a reuse reference, not a tested record")
    check(attempt.gate == "packaged" or selection is None, "$.selection", "a Build gate judges no selected Build")
    document = (_build_gate(attempt, config_sha256, temporary_root) if attempt.gate == "build"
                else _packaged_gate(attempt, selection, temporary_root))
    attempt.watch.recheck()
    return grammar.CI_GATE_NAME, document


def seal_reuse(attempt: Attempt, *, temporary_root: Path) -> tuple[str, dict[str, Any]]:
    """Decide the reuse of an attempt authenticated in ``reuse`` mode again and return its reuse
    reference as ``(file name, document)``, ready to be written as the one file of the reference.

    The attempt arrives with its source admitted, its run authenticated as the latest attempt in
    progress and its plan job finished while every worker was skipped. The admission runs once
    more through the attempt's reads (``reuse.admit_post_merge_reuse``), so the gate depends on
    nothing its plan job said: both original tested records are read by id and authenticated, in
    the private directory ``temporary_root``. When the original gates no longer cover this
    subject, the workers that were skipped cannot be made up for here: the gate raises
    :class:`~mod_base.build_ci.reuse.ReuseRefused` and seals nothing. The attempt's watch and the
    admission's are rechecked before return."""

    check(attempt.mode == "reuse", "$.mode", "only a reuse run seals a reuse reference")
    outcome = admit_post_merge_reuse(attempt.reads, plan=attempt.plan, event=attempt.producer["event"],
                                     temporary_root=temporary_root)
    if isinstance(outcome, FullRunRequired):
        raise ReuseRefused(f"this run skipped its workers for reuse, which is no longer admitted "
                           f"({outcome.reason}): {outcome.detail}; rerun all jobs")
    document = reuse_reference(plan=attempt.plan, producer=attempt.producer, source=outcome.source)
    attempt.watch.recheck()
    outcome.recheck()
    return grammar.CI_REUSE_NAME, document


def seal_results(api: GitHubApi | CommandReads, *, record: dict[str, Any], plan: dict[str, Any],
                 selection: dict[str, Any], run_id: int, run_attempt: int, config_sha256: str,
                 temporary_root: Path) -> tuple[str, dict[str, Any]]:
    """Authenticate the lanes of the running attempt whose aggregating job calls this and return
    its results index as ``(file name, document)``, ready to be written as the one file of the
    results artifact.

    ``record`` is the job's identity record, ``plan`` the plan of that subject and ``selection``
    the selection record its ``input`` job handed to this job: the Build every lane ran, selected
    by this very attempt (``records.bind_source_selection``). Every planned lane must have
    uploaded exactly one artifact in this attempt and no other lane may have. ``config_sha256`` is
    the digest of the protected Build config this job loaded; ``temporary_root`` a private
    directory for one lane's download at a time. Everything mutable is observed once more before
    return."""

    plan = _plan(plan)
    reads, watch = CommandReads.of(api), Watch()
    modes = tuple(mode for mode in admissible_modes(record, "packaged") if mode != "reuse")
    selection = bind_source_selection(selection, plan=plan, run_id=run_id, run_attempt=run_attempt,
                                      workflow_path=record["workflow_path"])
    owning = selection["build"]
    _admit_source(reads, watch, plan["identity"])
    # The run is observed before its jobs are read: its record says when this attempt started.
    _run(reads, watch, run_id)
    mode = _shown_mode(reads, record, plan, modes, run_id, run_attempt)
    producer = describe.attempt_producer(record, plan, mode=mode, run_id=run_id, run_attempt=run_attempt)
    _authenticate_run(reads, watch, producer, plan, mode=mode, complete=False)
    descriptors = watch.read(("jobs and lane artifacts of this attempt", run_id, run_attempt), functools.partial(
        describe.describe_attempt, reads, producer=producer, plan=plan, mode=mode,
        expected=[("runtime", lane["id"]) for lane in plan["lanes"]],
        finished=describe.settled_jobs("packaged", mode, plan, "results")))
    lanes = [{"descriptor": descriptor, **_read_sealed_lane(
        reads, descriptor, plan, owning_build=owning, build_envelope_sha256=selection["envelope_sha256"],
        source_config_sha256=config_sha256, temporary_root=temporary_root)} for descriptor in descriptors]
    document = results_index(plan=plan, producer=producer, owning_build=owning,
                             build_envelope_sha256=selection["envelope_sha256"], lanes=lanes)
    watch.recheck()
    return grammar.CI_RESULTS_NAME, document
