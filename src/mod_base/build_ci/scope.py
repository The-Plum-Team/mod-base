"""The narrowed runtime scope of a pull request's packaged run (docs/BUILD-RUNTIME-SELECTION.md).

A plan always holds every obligation of every lane, so the Build, the packaged run and the status
evaluation keep deriving one and the same plan. A pull request may still run fewer scenarios in
its lanes: the packaged ``input`` job derives a *scope* once, after it selected its Build
(``ci scope``, :func:`derive_scope`), and seals it in a ``mod-base.ci.scope`` record bound to its
plan and to its own run attempt. Every later job of the attempt receives the record as a job
output, like the selection record: a lane gives its own entry to the protected ``derive_runtime``
and ``verify_runtime`` hooks (:func:`lane_scope_value`), the aggregating job binds the record
into the results index, and the gate binds its digest into the packaged tested record. What reads
that record later knows that the run was narrowed: post-merge reuse never lets a narrowed pull
request cover the push after its merge (``reuse.FULL_RUN_REASONS["original-run-narrowed"]``), and
a narrowed record is never a full baseline.

The selection itself is the mod's domain policy: the protected ``derive_scope`` hook, run as the
validator from the protected adapter copy, without a token, with the plan and the candidate files
of ``validation-input/`` and the kit's input in ``MB_SCOPE_INPUT``. That input
(``changed-paths-v1``) is what protected kit code authenticated here, through the job's read-only
token: every path the tested merge changes against its base, read from the two Git trees, and the
base commit's own full packaged evidence. The hook names the scenarios each lane runs; the kit
checks that they are that lane's obligations, in plan order.

Full scope is the answer whenever anything is unknown, and it needs no record: a run without one
is exactly the run of a kit without this feature. :class:`FullScope` names why, by one of
:data:`FULL_SCOPE_REASONS`: no ``runtime_selection`` in the protected config, a subject that is
not a pull request of this repository, a changed kit pin, a pull request that changes the selection policy or a file
it reads (:func:`selection_inputs`), changes that cannot be listed completely, a base commit
without complete, successful, unnarrowed full packaged evidence of the executing kit pin, or a
selection that narrows nothing or does not fit its record. A malformed API answer, a failed API
request, a failed hook and a malformed selection stop the job instead: they are errors, not unknowns.
"""

from __future__ import annotations

import functools
import hashlib
import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci.adapter import parse_derived_scope, plan_sources
from mod_base.build_ci.config import BUILD_CONFIG_PATH, CONTROL_PATHS, runtime_selection
from mod_base.build_ci.identity import PULL_REQUEST_EVENT
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import bind_scope_record, original_plan, scope_document
from mod_base.build_ci.reuse import _read_record
from mod_base.build_ci.selection import newest_run, pending_run
from mod_base.build_ci.transport import (ArtifactUnavailable, _artifact_state, _availability, _download,
                                         _listed_artifact, _plan, _run)
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from mod_base.model.validators import Int, check, is_evidence_text
from mod_base.workflow import CI_CALLER_WORKFLOWS, ci_producer

#: The scope record in the state directory of a packaged job whose run attempt narrowed its scope.
SCOPE_NAME = "ci-scope.json"
#: Why a pull request's lanes run every obligation, by the token ``ci scope`` outputs as ``reason``.
FULL_SCOPE_REASONS = {
    "not-enabled": "the protected Build config enables no runtime selection",
    "not-a-pull-request": "only a pull request narrows its scope; a push, a dispatch and a release run in full",
    "foreign-pull-request": "the pull request comes from another repository, whose runs always test in full",
    "candidate-kit": "the pull request changes the kit pin, and a new kit is tested in full",
    "selection-input-changed": "the pull request changes the selection policy or a file it reads",
    "changes-unknown": "the API did not list the base tree or the tested tree completely",
    "changes-too-large": "the changed paths do not fit the input of the selection hook",
    "no-baseline": "the base commit has no packaged run of a push",
    "baseline-pending": "the newest packaged run of the base commit has not finished",
    "baseline-failed": "the newest packaged run of the base commit did not succeed",
    "baseline-kit-differs": "the base commit was tested under another kit pin",
    "baseline-unavailable": "the packaged evidence of the base commit has expired or is gone",
    "baseline-differs": "the base commit was tested under another policy, inventory, scenario contract or plan",
    "baseline-narrowed": "the evidence of the base commit ran a narrowed scope, which is never a full baseline",
    "nothing-narrowed": "the selection hook selected every obligation of every lane",
    "scope-too-large": "the narrowed scope does not fit its record",
}
#: What the base commit's evidence and the pull request's plan must have in common for the base
#: to be a baseline of the same lanes and obligations, with the reason a difference gives.
_COMPARED = ("repository", "base_branch", "controller_workflow", "kit", "policy_sha256", "inventory_sha256",
             "scenario_sha256", "runtime_selection_sha256", "graph_version")
_TREE_MODES = frozenset({"100644", "100755", "120000", "160000", "040000"})
_TREE_TYPES = frozenset({"blob", "tree", "commit"})


@dataclass(frozen=True)
class FullScope:
    """Every lane runs every obligation. ``reason`` is a key of :data:`FULL_SCOPE_REASONS` and
    ``detail`` one line that says it in words."""

    reason: str
    detail: str

    def __post_init__(self) -> None:
        check(self.reason in FULL_SCOPE_REASONS, "$.reason", "is not a reason for a full scope")


class _Full(Exception):
    """Leaves the derivation with a full scope. Raised and caught in this module only."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.outcome = FullScope(reason, FULL_SCOPE_REASONS[reason] + (f" ({detail})" if detail else ""))


def selection_inputs(config: dict[str, Any]) -> frozenset[str]:
    """The repository paths a pull request may not change and still narrow its scope: the
    protected Build config, every source of the adapter closure (the selection policy is the
    ``derive_scope`` hook among them), every candidate file a plan is derived from (what the hook
    reads in ``validation-input/``) and the control files. ``config`` is the validated document."""

    return frozenset({BUILD_CONFIG_PATH, *(item["path"] for item in config["adapter"]["files"]),
                      *plan_sources(config).values(), *CONTROL_PATHS})


def _tree_path(value: Any) -> bool:
    return (type(value) is str and 1 <= len(value) <= 4096 and not value.startswith("/") and "\0" not in value
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def _tree(reads: CommandReads, sha: str) -> dict[str, tuple[str, str]]:
    """``{path: (mode, object id)}`` of every entry of the recursive Git tree ``sha`` that is not
    itself a tree. A truncated or oversized listing is not known completely: a full scope."""

    value = reads.get_json(f"/repos/{reads.repository}/git/trees/{sha}", params={"recursive": 1})
    check(type(value) is dict and value.get("sha") == sha and type(value.get("tree")) is list
          and type(value.get("truncated")) is bool, "$.tree", f"tree {sha} response is malformed")
    if value["truncated"] or len(value["tree"]) > limits.MAX_CI_SCOPE_TREE_ENTRIES:
        raise _Full("changes-unknown", f"tree {sha}")
    entries: dict[str, tuple[str, str]] = {}
    for index, row in enumerate(value["tree"]):
        here = f"$.tree[{index}]"
        check(type(row) is dict and _tree_path(row.get("path")) and row.get("mode") in _TREE_MODES
              and row.get("type") in _TREE_TYPES and grammar.is_match(grammar.SHA1, row.get("sha")), here,
              f"tree {sha} has a malformed entry")
        check(row["path"] not in entries, here, f"tree {sha} repeats a path")
        if row["type"] != "tree":
            entries[row["path"]] = (row["mode"], row["sha"])
    return entries


def _changes(reads: CommandReads, identity: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The ``changes`` of a scope record and the sorted paths whose entry differs between the
    tree of the pull request's base and the tested tree: added, removed, or of another mode or
    content. Every read names an immutable object."""

    base = identity["base_sha"]
    commit = reads.get_json(f"/repos/{reads.repository}/git/commits/{base}")
    tree = commit.get("tree") if type(commit) is dict else None
    check(type(commit) is dict and commit.get("sha") == base and type(tree) is dict
          and grammar.is_match(grammar.SHA1, tree.get("sha")), "$.base", f"commit {base} response is malformed")
    before, after = _tree(reads, tree["sha"]), _tree(reads, identity["tested_tree"])
    paths = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    return {"base_sha": base, "base_tree": tree["sha"], "tested_tree": identity["tested_tree"],
            "paths": len(paths), "paths_sha256": canonical_sha256(paths)}, paths


def _record(reads: CommandReads, state: dict[str, Any], name: str, kind: str,
            temporary_root: Path) -> tuple[dict[str, Any], str]:
    """The one canonical record of an artifact (``state`` as ``transport._artifact_state`` projects
    it) and the SHA-256 of its canonical bytes."""

    check(state["size"] <= limits.MAX_CI_RECORD_BYTES, "$.baseline", "the baseline record exceeds the record cap")
    document = _read_record(_download(reads, {"artifact": state}), name, kind, temporary_root)
    return document, hashlib.sha256(canonical_json(document)).hexdigest()


def _artifact(state: dict[str, Any], run_id: int, run_attempt: int) -> dict[str, Any]:
    return {"run_id": run_id, "run_attempt": run_attempt, "artifact_id": state["id"], "artifact_name": state["name"],
            "digest": state["digest"]}


def _same_baseline(evidence: dict[str, Any], plan: dict[str, Any]) -> None:
    """The base commit's evidence tests the base commit of the pull request, as a protected push,
    for the same lanes and obligations under the same policy and kit pin: its plan hash is the
    hash of the pull request's plan under the base commit's identity (``records.original_plan``,
    the one comparison post-merge reuse makes too), so no target, lane or obligation differs."""

    then, now = evidence["identity"], plan["identity"]
    check(then["pr_number"] == 0 and then["tested_sha"] == now["base_sha"]
          and then["head_branch"] == now["base_branch"], "$.baseline.identity",
          "the baseline record does not test the base commit of the pull request")
    differing = sorted(key for key in _COMPARED if then[key] != now[key])
    if differing or evidence["profile"] != plan["profile"]:
        raise _Full("baseline-differs", ", ".join(differing or ["profile"]))
    if original_plan(plan, then)["plan_sha256"] != evidence["plan_sha256"]:
        raise _Full("baseline-differs", "plan")


def _baseline(reads: CommandReads, watch: Watch, plan: dict[str, Any], temporary_root: Path) -> dict[str, Any]:
    """The ``baseline`` of a scope record: the full packaged evidence of the base commit.

    The newest packaged run of a push of the base commit, chosen before its result is read, must
    have completed successfully as its latest attempt under the executing kit pin and list, for
    that attempt, either its packaged tested record or the reuse reference of a push its pull
    request covered; behind a reference, the original packaged record it names. The record is
    read and must test the base commit for the same lanes, and neither it nor an original may be
    narrowed. Everything mutable joins ``watch``."""

    identity = plan["identity"]
    base, branch, repository, kit = (identity["base_sha"], identity["base_branch"], identity["repository"],
                                     identity["kit"])
    newest = watch.read(("baseline run", base), functools.partial(
        newest_run, reads, "packaged", head_sha=base, head_branch=branch, head_repository=repository,
        events=("push",)))
    if newest is None:
        raise _Full("no-baseline", base)
    run = _run(reads, watch, newest["id"])
    check(run["created_at"] == newest["created_at"] and type(run["run_attempt"]) is int
          and run["run_attempt"] >= newest["run_attempt"], "$.baseline.run", "run listing and run disagree")
    check((run["path"], run["event"], run["head_sha"], run["head_branch"], run["head_repository"])
          == (CI_CALLER_WORKFLOWS["packaged"], "push", base, branch, repository), "$.baseline.run",
          "the newest packaged run of the base commit is not recorded under it")
    where = f"run {newest['id']} attempt {run['run_attempt']}"
    if run["status"] != "completed":
        pending_run(run)
        raise _Full("baseline-pending", where)
    check(type(run["conclusion"]) is str, "$.baseline.run.conclusion", "a completed run has a conclusion")
    if run["conclusion"] != "success":
        raise _Full("baseline-failed", where)
    pins = {sha for workflow, sha in run["referenced_workflows"]
            if workflow.startswith(f"{kit['repository']}/.github/workflows/")}
    if pins != {kit["sha"]}:
        raise _Full("baseline-kit-differs", where)
    run_id, attempt = newest["id"], run["run_attempt"]
    names = {grammar.ci_artifact_name("tested", run_id, attempt, "packaged"): "tested",
             grammar.ci_artifact_name("reuse", run_id, attempt): "reuse"}
    listed = [row for row in reads.paginate(f"/repos/{repository}/actions/runs/{run_id}/artifacts", field="artifacts",
                                            max_items=limits.MAX_CI_ARTIFACTS_PER_GATE)
              if type(row) is dict and row.get("name") in names]
    check(len(listed) <= 1, "$.baseline.artifacts", f"{where} lists both a tested record and a reuse reference, "
                                                    "or one of them twice")
    if not listed:
        raise _Full("baseline-unavailable", f"{where} lists no packaged tested record or reuse reference")
    state = _artifact_state(listed[0])
    check((state["run_id"], state["head_sha"], state["head_branch"]) == (run_id, base, branch), "$.baseline.artifacts",
          "the baseline record is not recorded under its run and the base commit")
    if state["expired"]:
        raise _Full("baseline-unavailable", f"{state['name']} has expired")
    _listed_artifact(reads, watch, run_id, state)
    evidence = names[state["name"]]
    if evidence == "tested":
        record, digest = _record(reads, state, grammar.CI_GATE_NAME, "mod-base.ci.gate", temporary_root)
        check(record["gate"] == "packaged" and (record["producer"]["run_id"], record["producer"]["run_attempt"])
              == (run_id, attempt), "$.baseline.record", "is not the packaged tested record of its run attempt")
        _same_baseline(record, plan)
        return {"evidence": "tested", "record": _artifact(state, run_id, attempt), "original": None,
                "record_sha256": digest, "plan_sha256": record["plan_sha256"]}
    reference, digest = _record(reads, state, grammar.CI_REUSE_NAME, "mod-base.ci.reuse", temporary_root)
    check((reference["producer"]["run_id"], reference["producer"]["run_attempt"]) == (run_id, attempt),
          "$.baseline.record", "is not the reuse reference of its run attempt")
    _same_baseline(reference, plan)
    seal = reference["source"]["packaged_seal"]
    original_run, original_attempt = seal["producer"]["run_id"], seal["producer"]["run_attempt"]
    key, reader = _availability(reads, original_run, (seal["artifact"]["id"],))
    try:
        original = watch.read(key, reader)[seal["artifact"]["id"]]
    except ArtifactUnavailable as error:
        raise _Full("baseline-unavailable", f"{seal['artifact']['name']} is no longer listed") from error
    for field in ("name", "size", "digest", "created_at", "expires_at"):
        check(original[field] == seal["artifact"][field], f"$.baseline.original.{field}",
              "the original packaged record differs from what the reuse reference names")
    check((original["run_id"], original["head_sha"]) == (original_run, seal["producer"]["api_head_sha"]),
          "$.baseline.original", "the original packaged record is not recorded under its run and head")
    if original["expired"]:
        raise _Full("baseline-unavailable", f"{original['name']} has expired")
    record, _ = _record(reads, original, grammar.CI_GATE_NAME, "mod-base.ci.gate", temporary_root)
    producer = {key: value for key, value in seal["producer"].items() if key != "upload_window"}
    check(record["gate"] == "packaged" and record["producer"] == producer and record["identity"] == seal["identity"]
          and record["plan_sha256"] == seal["plan_sha256"], "$.baseline.original",
          "is not the original packaged record the reuse reference names")
    if "scope_sha256" in record:
        raise _Full("baseline-narrowed", original["name"])
    return {"evidence": "reuse", "record": _artifact(state, run_id, attempt),
            "original": _artifact(original, original_run, original_attempt), "record_sha256": digest,
            "plan_sha256": reference["plan_sha256"]}


def _input(*, input_format: str, changes: dict[str, Any], paths: list[str], baseline: dict[str, Any]) -> str:
    """``MB_SCOPE_INPUT`` in the format ``changed-paths-v1``: one line of canonical JSON with the
    base commit and both trees, every changed path, sorted, and the baseline evidence."""

    document = {"format": input_format, "base_sha": changes["base_sha"], "base_tree": changes["base_tree"],
                "tested_tree": changes["tested_tree"], "changed_paths": paths, "baseline": baseline}
    value = canonical_json(document).decode("utf-8").rstrip("\n")
    if len(value.encode("utf-8")) > limits.MAX_CI_SCOPE_BYTES:
        raise _Full("changes-too-large", f"{len(paths)} paths")
    # Git allows a path that JSON keeps as it is but a worker environment value may not hold (a
    # DEL): such a change is not malformed, it only does not fit the hook's input.
    if not is_evidence_text(value, limits.MAX_CI_SCOPE_BYTES):
        raise _Full("changes-too-large", "a changed path holds a character the hook's input cannot carry")
    return value


def _lanes(plan: dict[str, Any], derived: dict[str, Any]) -> list[dict[str, Any]]:
    """The lanes of a scope record from what the hook selected: every planned lane in plan order,
    its scenarios its own obligations in plan order. A lane the hook names no scenario for runs
    in full. A selection of anything else is malformed and stops the job."""

    check([lane["id"] for lane in derived["lanes"]] == [lane["id"] for lane in plan["lanes"]], "$.scope.lanes",
          "derive_scope must name every planned lane and no other, in plan order")
    lanes = []
    for index, (lane, planned) in enumerate(zip(derived["lanes"], plan["lanes"])):
        selected = set(lane["scenarios"])
        check(lane["scenarios"] == [name for name in planned["obligations"] if name in selected],
              f"$.scope.lanes[{index}].scenarios", "derive_scope must select obligations of the lane, in plan order")
        lanes.append({"id": planned["id"], "scenarios": list(lane["scenarios"] or planned["obligations"])})
    if all(len(lane["scenarios"]) == len(planned["obligations"]) for lane, planned in zip(lanes, plan["lanes"])):
        raise _Full("nothing-narrowed")
    return lanes


def derive_scope(api: GitHubApi | CommandReads, *, plan: dict[str, Any], config: dict[str, Any], event: str,
                 run_id: int, run_attempt: int, workflow_path: str, temporary_root: Path,
                 derive: Callable[[str], bytes]) -> dict[str, Any] | FullScope:
    """The scope record one run attempt of the packaged caller runs, or :class:`FullScope`.

    ``plan`` is the job's protected plan, ``config`` the validated protected Build config, and
    ``event``, ``run_id``, ``run_attempt`` and ``workflow_path`` the run's own, from the job's
    state. ``derive`` runs the protected ``derive_scope`` hook with the kit's input line and
    returns the bytes the hook wrote (``lifecycle.derive_scope``); it runs only once nothing is
    left that answers full. ``temporary_root`` is a private directory for the baseline records,
    which are removed again. No request is sent when the config, the event or the plan already
    answer full. Everything mutable the baseline rests on is observed again immediately before
    the record is returned: the caller writes it at once."""

    plan = _plan(plan)
    identity = plan["identity"]
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    check(ci_producer(workflow_path) == "packaged", "$.workflow_path", "only the packaged caller narrows its scope")
    input_format = runtime_selection(config)
    try:
        if input_format is None:
            raise _Full("not-enabled")
        if event != PULL_REQUEST_EVENT or not identity["pr_number"]:
            raise _Full("not-a-pull-request")
        if identity["source_repository"] != identity["repository"]:
            raise _Full("foreign-pull-request", identity["source_repository"])
        if "candidate_kit" in plan:
            raise _Full("candidate-kit", plan["candidate_kit"]["version"])
        reads, watch = CommandReads.of(api), Watch()
        changes, paths = _changes(reads, identity)
        touched = sorted(selection_inputs(config).intersection(paths))
        if touched:
            raise _Full("selection-input-changed", ", ".join(touched[:3]) + (", ..." if len(touched) > 3 else ""))
        try:
            with tempfile.TemporaryDirectory(prefix="mb-ci-scope-", dir=temporary_root) as temporary:
                baseline = _baseline(reads, watch, plan, Path(temporary))
        except OSError as error:
            raise MbError("cannot hold the private baseline records of a scope", reason="ci-transport") from error
        value = _input(input_format=input_format, changes=changes, paths=paths, baseline=baseline)
        lanes = _lanes(plan, parse_derived_scope(derive(value)))
        request = {"run_id": run_id, "run_attempt": run_attempt, "nonce": secrets.token_hex(32),
                   "workflow_path": workflow_path,
                   "workflow_ref": grammar.workflow_ref(identity["repository"], workflow_path, identity["base_branch"])}
        document = scope_document(plan=plan, request=request, input_format=input_format, changes=changes,
                                  baseline=baseline, lanes=lanes)
        # The one bound a selection of real obligations can still exceed: its record's size.
        size = len(canonical_json(document))
        if size > limits.MAX_CI_SCOPE_BYTES:
            raise _Full("scope-too-large", f"{size} bytes")
    except _Full as full:
        return full.outcome
    record = bind_scope_record(document, plan=plan, run_id=run_id, run_attempt=run_attempt,
                               workflow_path=workflow_path)
    watch.recheck()
    return record


def received_scope(raw: bytes, *, plan: dict[str, Any], run_id: int, run_attempt: int,
                   workflow_path: str) -> dict[str, Any] | None:
    """The scope a job received from the ``input`` job of its run (``--scope FILE``): ``None`` for
    an empty file, which is what a run in full hands over, else the canonical ``mod-base.ci.scope``
    record of ``plan`` requested by this very run attempt (:func:`records.bind_scope_record`)."""

    if raw in (b"", b"\n"):
        return None
    document = load_document(raw, kind="mod-base.ci.scope", label="scope record", plan=plan)
    check(raw == canonical_json(document), "$.scope", "the scope record is not canonical JSON")
    return bind_scope_record(document, plan=plan, run_id=run_id, run_attempt=run_attempt, workflow_path=workflow_path)


def lane_scope_value(scope: dict[str, Any] | None, lane_id: str) -> str | None:
    """``MB_RUNTIME_SCOPE`` of lane ``lane_id`` under ``scope``: one line of canonical JSON with
    the lane, the scenarios it runs and the digest of the whole scope record, or ``None`` when the
    run is not narrowed."""

    if scope is None:
        return None
    lane = next((item for item in scope["lanes"] if item["id"] == lane_id), None)
    check(lane is not None, "$.scope.lanes", f"the scope names no lane {lane_id}")
    value = {"lane_id": lane_id, "scenarios": lane["scenarios"], "scope_sha256": canonical_sha256(scope)}
    return canonical_json(value).decode("utf-8").rstrip("\n")
