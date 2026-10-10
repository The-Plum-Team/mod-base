"""Fixed status intents for the protected gates of one pull request head (MB11, read-only).

The status caller's ``evaluate`` job asks one question for the current head of a pull request:
which state may each protected gate show on that commit? :func:`evaluate_gates` answers with one
document. A caller-owned job with the mod's App credentials publishes it; nothing here writes to
GitHub.

For each gate the newest run of its managed caller under that head is chosen before any result is
read, by the listing rules of :mod:`mod_base.build_ci.selection`. The state is

* ``pending`` while the pull request is a draft, while no run exists yet, while the newest run is
  in progress, or when the newest run is a draft deferral;
* ``success`` only when the newest run is complete, its exact graph for its mode authenticates,
  its tested record downloads by numeric id and binds to the plan this job derived, the packaged
  gate's owning Build is exactly the bundle the Build gate sealed, and the live pull request still
  has the head, base and test merge of that plan and is not a draft;
* ``failure`` otherwise, with one bounded line that says why. A newer failed run is a failure
  whatever an older run proved, and so is a run whose evidence is gone: an artifact it needs that
  has expired, that its run no longer lists or whose numeric id GitHub answers with 404 or 410.

Nothing a candidate supplies reaches a context, a target or a state. The context strings are those
of the protected Build config, the target is the head the API reports for the pull request number,
and a state is decided only by the checks above. Only a description may quote a rejection.

A ``success`` needs the protected plan of the generation: the graph of a run and the units a
receipt must cover are functions of it. The caller passes the plan its job holds; without one no
finished run can be verified, so no gate can succeed. The plan is no trust root here: a success
also requires both tested records, which the protected gate jobs sealed, to carry its exact hash.

Deriving that plan costs a sandbox and a planning hook, and most evaluations do not need it: a
draft, a generation that has not started, one that is still running and one whose newest run
failed are all decided by the pull request and the run listings alone. :func:`settle_gates` is
that evaluation. It answers with the same document when no gate depends on the plan and with
``None`` as soon as one newest run finished successfully, which only :func:`evaluate_gates` with
the plan can verify. It never answers ``success``.

An API failure is never a state: it propagates, the job fails and no intent is produced. The one
exception is GitHub's own answer that an artifact is gone (:func:`~mod_base.build_ci.transport.artifact_gone`):
that is a fact about the evidence, and the gate it decides is a failure.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci.activation import BUILD_CALLER, PACKAGED_CALLER, STATUS_CALLER, managed_callers, managed_mode
from mod_base.build_ci.config import BuildConfig
from mod_base.build_ci.graph import authenticate_graph, run_graph
from mod_base.build_ci.identity import policy_sha256
from mod_base.build_ci.protocol import subject_of
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.selection import describe_artifact, newest_run, pending_run, producer_record
from mod_base.build_ci.transport import _plan, _run_state, artifact_gone, download_gate_receipt
from mod_base.errors import MbError, single_line
from mod_base.github.api import ApiError, GitHubApi, RequestBudgetExhausted
from mod_base.github.jobs import job_graph
from mod_base.model import grammar, limits
from mod_base.model.validators import Int, check

#: What ``shadow`` mode appends to both context strings, so that neither can be a required name.
SHADOW_SUFFIX = " (shadow)"
#: Gate -> the managed caller whose run seals it, the mode of that run for a pull request and
#: what a description calls the run.
_GATES = {"build": ("build", "full", "Build"), "packaged": ("packaged", "pull-request", "packaged E2E")}
#: Gate -> the caller workflow whose management puts the gate under the kit's status caller.
_CALLERS = {"build": BUILD_CALLER, "packaged": PACKAGED_CALLER}


class StatusError(MbError):
    """The evaluation does not apply or its own inputs disagree (exit 2); no intent is produced."""

    default_reason = "ci-status"


@dataclass(frozen=True)
class _Verdict:
    """What one gate may show: its state, why, the run that decides it and, only when that run's
    gate is verified, its receipt. ``unverified`` marks the one state that stands for a check
    nobody made: the newest run finished successfully and the job held no plan to verify it with."""

    state: str
    description: str
    run_id: int | None = None
    receipt: dict[str, Any] | None = None
    unverified: bool = False


def gate_contexts(config: BuildConfig, activation: dict[str, Any] | None) -> dict[str, str]:
    """Gate -> the fixed context its status is published under, for the gates the activation mode
    puts under the kit's status caller.

    The strings are those of the protected Build config; ``shadow`` (and a rollback that leaves
    it) appends :data:`SHADOW_SUFFIX`. ``shared-build`` leaves packaged E2E to the mod, so only
    the Build gate is evaluated. A mode that manages no status caller is a rejection.
    """

    callers = managed_callers(activation)
    if STATUS_CALLER not in callers or BUILD_CALLER not in callers:
        raise StatusError("the activation mode of this mod manages no gate status caller")
    suffix = SHADOW_SUFFIX if managed_mode(activation) == "shadow" else ""
    return {gate: config.data["contexts"][gate] + suffix for gate, caller in _CALLERS.items() if caller in callers}


def _pull_request(reads: CommandReads, number: int) -> dict[str, Any]:
    """One observation of the open same-repository pull request ``number``: its draft state, its
    head and base and the test merge GitHub currently offers."""

    pull = reads.get_json(f"/repos/{reads.repository}/pulls/{number}")
    check(type(pull) is dict and type(pull.get("number")) is int and pull["number"] == number,
          "$.pr.number", "wrong or malformed PR")
    check(pull.get("state") == "open" and type(pull.get("draft")) is bool,
          "$.pr", "requires an open PR with exact readiness state")
    sides = {}
    for side in ("head", "base"):
        value = pull.get(side)
        check(type(value) is dict and type(value.get("repo")) is dict
              and value["repo"].get("full_name") == reads.repository,
              f"$.pr.{side}.repo", "foreign/fork candidate is unsupported")
        sides[side] = {"sha": grammar.require_sha1(value.get("sha"), f"PR {side} SHA"),
                       "ref": grammar.require(grammar.BRANCH, value.get("ref"), f"PR {side} branch")}
    merge = pull.get("merge_commit_sha")
    if merge is not None:
        grammar.require_sha1(merge, "PR merge SHA")
    return {"draft": pull["draft"], "merge_sha": merge, **sides}


def _generation_plan(plan: dict[str, Any], *, config: BuildConfig, controller_sha: str, pr_number: int,
                     pull: dict[str, Any]) -> dict[str, Any]:
    """A validated private copy of the plan this job derived, which must be the plan of exactly
    the generation under evaluation: this repository, profile, pull request and executing
    controller, the protected policy this checkout holds and the pull request as it is now."""

    plan = _plan(plan)
    identity = plan["identity"]
    if ((identity["repository"], plan["profile"], identity["pr_number"], identity["controller_sha"])
            != (config.data["repository"], config.data["profile"], pr_number, controller_sha)
            or identity["policy_sha256"] != policy_sha256(config, subject_of(identity))):
        raise StatusError("the plan of this job is not the protected plan of this pull request and controller")
    if ((identity["head_sha"], identity["head_branch"], identity["base_sha"], identity["base_branch"],
         identity["tested_sha"])
            != (pull["head"]["sha"], pull["head"]["ref"], pull["base"]["sha"], pull["base"]["ref"], pull["merge_sha"])):
        raise StatusError("the pull request moved after this job planned; evaluate again")
    return plan


def _run_verdict(reads: CommandReads, gate: str, newest: dict[str, Any], plan: dict[str, Any] | None,
                 temporary_root: Path | None) -> _Verdict:
    """What the newest run of a gate's caller proves: its latest attempt, then its exact graph and
    its tested record. A rejection of that evidence is raised."""

    producer, mode, label = _GATES[gate]
    run_id = newest["id"]
    waiting = _Verdict("pending", f"waiting: the newest {label} run is in progress", run_id)
    if newest["status"] != "completed":
        pending_run(newest)
        return waiting
    run = _run_state(reads, run_id)
    check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
          and run["run_attempt"] >= newest["run_attempt"], "$.run", "run listing and run disagree")
    if run["status"] != "completed":
        pending_run(run)
        return waiting
    if run["conclusion"] != "success":
        return _Verdict("failure", f"the newest {label} run failed or was cancelled", run_id)
    if plan is None or temporary_root is None:
        return _Verdict("failure", f"the newest {label} run cannot be verified: this job derived no protected plan",
                        run_id, unverified=True)
    attempt = run["run_attempt"]
    jobs = reads.attempt_jobs(run_id, attempt)
    if job_graph(jobs) == run_graph(producer, "deferred").jobs(plan):
        return _Verdict("pending", f"deferred: the newest {label} run was a draft deferral", run_id)
    digest = authenticate_graph(reads, plan=plan, producer=producer, mode=mode, run_id=run_id, run_attempt=attempt)
    record = producer_record(plan, caller=producer, run_id=run_id, run_attempt=attempt, event=newest["event"],
                             graph_sha256=digest)
    descriptor = describe_artifact(reads, plan=plan, producer=record, jobs=jobs, kind="tested", unit_id=gate)
    receipt = download_gate_receipt(reads, descriptor=descriptor, plan=plan, gate=gate, temporary_root=temporary_root)
    return _Verdict("success", f"the newest {label} run is complete and its gate is verified", run_id, receipt)


def _gate(reads: CommandReads, watch: Watch, gate: str, pull: dict[str, Any], plan: dict[str, Any] | None,
          temporary_root: Path | None) -> _Verdict:
    """The verdict of one gate from the newest run of its caller under the pull request's head.
    A rejection of that run's evidence is a failure, and so is an artifact GitHub says is gone;
    every other API failure propagates."""

    producer, _, label = _GATES[gate]
    newest = watch.read((f"newest {label} run",), functools.partial(
        newest_run, reads, producer, head_sha=pull["head"]["sha"], head_branch=pull["head"]["ref"],
        head_repository=reads.repository, events=("pull_request_target",)))
    if newest is None:
        return _Verdict("pending", f"waiting: no {label} run exists for this head yet")
    try:
        return _run_verdict(reads, gate, newest, plan, temporary_root)
    except ApiError as error:
        if not artifact_gone(error):
            raise
        artifact_id = error.path.split("/")[6]
        return _Verdict("failure", f"the newest {label} run was rejected: artifact {artifact_id} it needs is gone "
                                   f"(HTTP {error.status})", newest["id"])
    except RequestBudgetExhausted:
        raise
    except MbError as error:
        return _Verdict("failure", f"the newest {label} run was rejected: {error}", newest["id"])


def _coupled(build: _Verdict, packaged: _Verdict) -> _Verdict:
    """The packaged verdict once it is tied to the Build gate: a verified packaged run counts only
    when the Build it consumed is exactly the bundle the verified Build gate sealed."""

    if packaged.receipt is None:
        return packaged
    if build.receipt is None:
        if build.state == "pending":
            return _Verdict("pending", "waiting: the Build gate of this head is not verified yet", packaged.run_id)
        return _Verdict("failure", "the Build gate of this head did not pass", packaged.run_id)
    if packaged.receipt["owning_build"] != build.receipt["artifacts"][0]:
        return _Verdict("failure", "the packaged E2E run consumed another Build than the one the Build gate sealed",
                        packaged.run_id)
    return packaged


def _evaluate(api: GitHubApi, *, pr_number: int, config: BuildConfig, activation: dict[str, Any] | None,
              controller_sha: str, plan: dict[str, Any] | None,
              temporary_root: Path | None) -> dict[str, Any] | None:
    """The intents document of :func:`evaluate_gates`. Without a directory for tested records
    (:func:`settle_gates`, which holds no plan either) the answer is None as soon as one gate
    cannot be decided without the plan."""

    contexts = gate_contexts(config, activation)
    Int(1, limits.MAX_RUN_ID)(pr_number, "$.pr_number")
    grammar.require_sha1(controller_sha, "executing controller SHA")
    reads, watch = CommandReads.of(api), Watch()
    if reads.repository != config.data["repository"]:
        raise StatusError("the API client serves another repository than the protected Build config")
    pull = watch.read(("pull request",), functools.partial(_pull_request, reads, pr_number))
    if pull["draft"]:
        verdicts = {gate: _Verdict("pending", "deferred: the pull request is a draft") for gate in contexts}
    else:
        if plan is not None:
            plan = _generation_plan(plan, config=config, controller_sha=controller_sha, pr_number=pr_number, pull=pull)
        verdicts = {gate: _gate(reads, watch, gate, pull, plan, temporary_root) for gate in contexts}
        if temporary_root is None and any(verdict.unverified for verdict in verdicts.values()):
            return None
        if "packaged" in verdicts:
            verdicts["packaged"] = _coupled(verdicts["build"], verdicts["packaged"])
    watch.recheck()
    return {"repository": reads.repository, "pr_number": pr_number, "target_sha": pull["head"]["sha"],
            "gates": {gate: {"context": contexts[gate], "state": verdict.state,
                             "description": single_line(verdict.description,
                                                        limit=limits.MAX_CI_STATUS_DESCRIPTION_CHARS),
                             "target_url": None if verdict.run_id is None
                             else grammar.run_url(reads.repository, verdict.run_id)}
                      for gate, verdict in verdicts.items()}}


def evaluate_gates(api: GitHubApi, *, pr_number: int, config: BuildConfig, activation: dict[str, Any] | None,
                   controller_sha: str, plan: dict[str, Any] | None, temporary_root: Path) -> dict[str, Any]:
    """The status intents of the protected gates for the current head of pull request ``pr_number``.

    ``config`` is the protected Build config and ``activation`` the validated activation manifest
    of the checkout that is executing at ``controller_sha``; ``plan`` is the plan this job derived
    for the pull request, or None when it could derive none (no gate can then succeed);
    ``temporary_root`` is a private directory for the tested records. Returns
    ``{repository, pr_number, target_sha, gates}``, where ``gates`` maps each evaluated gate
    (:func:`gate_contexts`) to its ``context``, ``state`` (``success``, ``pending`` or
    ``failure``), one-line ``description`` and ``target_url``: the canonical URL of the run that
    decides the state, or null when no run does.

    The pull request and both run listings are read at the start and again immediately before the
    document is returned; a difference raises instead of returning intents for a state that is
    already gone.
    """

    check(isinstance(temporary_root, Path), "$.temporary_root", "must be a private directory for the tested records")
    document = _evaluate(api, pr_number=pr_number, config=config, activation=activation,
                         controller_sha=controller_sha, plan=plan, temporary_root=temporary_root)
    if document is None:  # only an evaluation without that directory may leave a gate open
        raise StatusError("the evaluation left a gate undecided")
    return document


def settle_gates(api: GitHubApi, *, pr_number: int, config: BuildConfig, activation: dict[str, Any] | None,
                 controller_sha: str) -> dict[str, Any] | None:
    """The status intents of pull request ``pr_number`` when no gate needs the protected plan,
    else None.

    The first step of the status callee's job, before a subject is authenticated or a plan is
    derived. The document is the one :func:`evaluate_gates` returns, and it is complete exactly
    when every gate is decided by the pull request and the run listings alone: the pull request
    is a draft, or each gate's newest run is absent, still in progress, or failed or cancelled.
    As soon as one newest run finished successfully the answer is None: only the plan can tell a
    verified gate from a draft deferral or a foreign graph, so the job derives it and calls
    :func:`evaluate_gates`. No state of a returned document is ``success``.

    A returned document was observed twice like every other one (the pull request and the
    listings are read again before it is returned). None is no observation anybody acts on, so
    nothing is read again for it.
    """

    document = _evaluate(api, pr_number=pr_number, config=config, activation=activation,
                         controller_sha=controller_sha, plan=None, temporary_root=None)
    if document is not None and any(intent["state"] == "success" for intent in document["gates"].values()):
        raise StatusError("an evaluation without the protected plan may never answer success")
    return document
