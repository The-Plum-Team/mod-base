"""Numeric-ID transport for Build/runtime exports and tested gates, independent of Pages (MB11).

Every route authenticates a producer run the way GitHub records it: a ``pull_request_target`` run
under the pull request's head commit and branch, a protected push or dispatch under the commit it
runs from. The controller commit and the kit pin come from the run's ``referenced_workflows``; the
tested merge is bound by the records inside the artifacts, compared with the caller's own plan.

Each route is one command's worth of reads. Commits and the job list of a completed attempt are
read once (:class:`~mod_base.build_ci.reads.CommandReads`); the source, each producer run's
latest attempt and each artifact's availability are read when first needed and once more
immediately before anything is published or returned (:class:`~mod_base.build_ci.reads.Watch`).
A caller's plan and descriptors are copied on entry and only the copies are used.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import (authenticate_merged_pr_identity, authenticate_source_identity,
                                            run_head)
from mod_base.build_ci.exports import (_materialize_build_export, materialize_build_export,
                                       validate_target_partitions, verify_build_export)
from mod_base.build_ci.graph import (authenticate_gate_timeline, authenticate_referenced_workflows, gate_mode,
                                     job_name, require_graph, require_partial_graph, run_graph, sealed_upload,
                                     upload_job_name)
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import bind_build_envelope, bind_gate_receipt, validate_descriptor
from mod_base.build_ci.runtime_exports import (_materialize_runtime_export, materialize_runtime_export,
                                               verify_runtime_export)
from mod_base.build_ci.runtime_schema import bind_runtime_envelope
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.artifacts import Artifact
from mod_base.github.runs import referenced_workflows
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.bounded_zip import ExtractionLimits, extract, extract_build, extract_runtime
from mod_base.io.tree import read_child_file, validate_tree_entries
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.documents import load_document
from mod_base.model.validators import Int, List, check
from mod_base.workflow import ci_producer, find_job

_RUN_FIELDS = ("id", "run_attempt", "status", "conclusion", "path", "event", "head_sha", "head_branch", "created_at")


def _plan(plan: dict[str, Any]) -> dict[str, Any]:
    """A validated private copy of the caller's plan."""

    validate_plan(plan)
    return strict_loads(canonical_json(plan), label="admitted Build plan", max_bytes=limits.MAX_CI_PLAN_BYTES)


def _descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """A validated private copy of a caller's artifact descriptor."""

    validate_descriptor(descriptor)
    return strict_loads(canonical_json(descriptor), label="selected artifact descriptor",
                        max_bytes=limits.MAX_CI_RECORD_BYTES)


def _bound(descriptor: dict[str, Any], plan: dict[str, Any], kind: str, unit_id: str | None) -> None:
    for key in ("identity", "plan_sha256", "profile"):
        check(descriptor[key] == plan[key], f"$.{key}", "selected artifact differs from the admitted plan")
    name = grammar.parse_ci_artifact_name(descriptor["artifact"]["name"])
    check((name.kind, name.unit_id) == (kind, unit_id), "$.artifact.name",
          "artifact does not name the exact required export or record")


def _admit_source(reads: CommandReads, watch: Watch, identity: dict[str, Any],
                  merged: tuple[str, str] | None = None) -> None:
    """The live subject, or a merged pull request's history, observed now and before the effect."""

    if merged is None:
        watch.read(("source",), functools.partial(authenticate_source_identity, reads, identity))
    else:
        watch.read(("source",), functools.partial(authenticate_merged_pr_identity, reads, identity,
                                                  controller_sha=merged[0], merged_sha=merged[1]))


def _run_state(reads: CommandReads, run_id: int) -> dict[str, Any]:
    run = reads.run(run_id)
    head = run.get("head_repository")
    return {**{key: run.get(key) for key in _RUN_FIELDS},
            "head_repository": head.get("full_name") if isinstance(head, dict) else None,
            "referenced_workflows": sorted(referenced_workflows(run))}


def _run(reads: CommandReads, watch: Watch, run_id: int) -> dict[str, Any]:
    """The producer run as its latest attempt, observed now and before the effect."""

    return watch.read(("producer run", run_id), functools.partial(_run_state, reads, run_id))


def _authenticate_run(reads: CommandReads, watch: Watch, producer: dict[str, Any], plan: dict[str, Any],
                      *, mode: str, complete: bool) -> None:
    """The exact latest attempt of a managed caller's run for this subject, in ``mode``."""

    identity = plan["identity"]
    head_sha, head_branch, head_repository = run_head(identity)
    run = _run(reads, watch, producer["run_id"])
    check(type(run["run_attempt"]) is int and run["run_attempt"] == producer["run_attempt"],
          "$.producer.run_attempt", "a newer producer attempt exists or attempt is malformed")
    check(run["path"] == producer["workflow_path"] and run["event"] == producer["event"],
          "$.producer", "run is not the enrolled producer workflow and event")
    check(run["head_sha"] == head_sha == producer["api_head_sha"] and run["head_branch"] == head_branch
          and run["head_repository"] == head_repository,
          "$.producer.api_head_sha", "run is not recorded under this subject's head")
    state = (run["status"], run["conclusion"])
    check(state == ("completed", "success") or (not complete and state == ("in_progress", None)),
          "$.producer.status", "producer run is queued, failed or cancelled" if not complete
          else "producer run did not complete successfully")
    authenticate_referenced_workflows(run["referenced_workflows"], identity=identity,
                                      producer=ci_producer(producer["workflow_path"]), mode=mode)


def _complete_jobs(reads: CommandReads, producer: dict[str, Any], plan: dict[str, Any], *,
                   mode: str) -> list[dict[str, Any]]:
    """The jobs of an authenticated completed attempt: exactly the graph its record carries."""

    jobs = reads.attempt_jobs(producer["run_id"], producer["run_attempt"])
    digest = require_graph(jobs, plan=plan, producer=ci_producer(producer["workflow_path"]), mode=mode,
                           run_attempt=producer["run_attempt"])
    check(digest == producer["graph_sha256"], "$.producer.graph_sha256", "wrong authenticated producer graph")
    return jobs


def _authenticate_upload(descriptor: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
    """The job that uploads this artifact sealed first and uploaded in the descriptor's window."""

    producer = descriptor["producer"]
    name = grammar.parse_ci_artifact_name(descriptor["artifact"]["name"])
    job = find_job(jobs, upload_job_name(ci_producer(producer["workflow_path"]), name.kind, name.unit_id),
                   run_attempt=producer["run_attempt"])
    window = producer["upload_window"]
    check(sealed_upload(job) == (window["started_at"], window["completed_at"]),
          "$.producer.upload_window", "wrong upload window")


def _artifact_state(raw: Any) -> dict[str, Any]:
    """One API artifact record as the descriptor stores it, with its mutable availability."""

    artifact = Artifact.parse(raw)
    return {"id": artifact.id, "name": artifact.name, "size": artifact.size, "digest": artifact.digest,
            "created_at": grammar.normalize_timestamp(artifact.created_at, "artifact created_at"),
            "expires_at": grammar.normalize_timestamp(raw.get("expires_at"), "artifact expires_at"),
            "expired": artifact.expired, "run_id": artifact.run_id,
            "head_branch": artifact.head_branch, "head_sha": artifact.head_sha}


def _artifact_states(reads: CommandReads, run_id: int, ids: tuple[int, ...]) -> dict[int, dict[str, Any]]:
    """The named artifacts of one run: one read by id for a single artifact, the run's listing for
    several, so the cost does not grow with the number of targets or lanes."""

    if len(ids) == 1:
        rows = [reads.get_json(f"/repos/{reads.repository}/actions/artifacts/{ids[0]}")]
    else:
        rows = reads.paginate(f"/repos/{reads.repository}/actions/runs/{run_id}/artifacts", field="artifacts",
                              max_items=limits.MAX_CI_ARTIFACTS_PER_GATE)
    states = {raw["id"]: _artifact_state(raw) for raw in rows if type(raw) is dict and raw.get("id") in ids}
    check(sorted(states) == list(ids), "$.artifact", "selected artifact is no longer listed for its producer run")
    return states


def _authenticate_artifacts(reads: CommandReads, watch: Watch, descriptors: list[dict[str, Any]],
                            identity: dict[str, Any]) -> None:
    """Immutable metadata, owner run and head, and availability of every selected artifact."""

    head_sha, head_branch, _ = run_head(identity)
    runs: dict[int, list[dict[str, Any]]] = {}
    for descriptor in descriptors:
        runs.setdefault(descriptor["producer"]["run_id"], []).append(descriptor)
    for run_id, group in runs.items():
        ids = tuple(sorted(descriptor["artifact"]["id"] for descriptor in group))
        check(len(set(ids)) == len(ids), "$.artifact.id", "duplicate selected artifact")
        states = watch.read(("artifact availability", run_id, ids),
                            functools.partial(_artifact_states, reads, run_id, ids))
        for descriptor in group:
            selected, state = descriptor["artifact"], states[descriptor["artifact"]["id"]]
            for key in ("id", "name", "size", "digest", "created_at", "expires_at"):
                check(state[key] == selected[key], f"$.artifact.{key}", "immutable selected metadata differs")
            check(not state["expired"] and state["run_id"] == run_id
                  and state["head_sha"] == head_sha and state["head_branch"] == head_branch,
                  "$.artifact", "expired artifact or wrong protected producer")


def _download(reads: CommandReads, descriptor: dict[str, Any]) -> bytes:
    selected = descriptor["artifact"]
    data = reads.download(f"/repos/{reads.repository}/actions/artifacts/{selected['id']}/zip",
                          max_bytes=selected["size"])
    check(len(data) == selected["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == selected["digest"],
          "$.artifact.digest", "download length or SHA-256 differs")
    return data


def _authenticate_build(reads: CommandReads, watch: Watch, descriptor: dict[str, Any],
                        plan: dict[str, Any]) -> None:
    """A complete bundle of a finished full run of the Build caller: its run, exact graph, the
    assembling job's seal and upload window, and the artifact itself. The descriptor is already
    bound to the plan and to the Build caller."""

    producer = descriptor["producer"]
    _authenticate_run(reads, watch, producer, plan, mode="full", complete=True)
    _authenticate_upload(descriptor, _complete_jobs(reads, producer, plan, mode="full"))
    _authenticate_artifacts(reads, watch, [descriptor], plan["identity"])


def _materialize_build(reads: CommandReads, descriptor: dict[str, Any], plan: dict[str, Any], output: Path,
                       before_publish: Callable[[], None] | None) -> dict[str, Any]:
    """Download an authenticated complete bundle by id and publish a verified private copy.

    Without ``before_publish`` the copy is a child of the caller's own unpublished stage, and it
    must carry the very envelope that was bound here."""

    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-download-", dir=output.parent) as temporary:
            root = Path(temporary) / "export"
            extract_build(data, root)
            del data
            envelope = verify_build_export(root, plan=plan)
            check((envelope["scope"], envelope["target_id"]) == ("complete", None),
                  "$.envelope.scope", "export is not the complete Build bundle")
            bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            if before_publish is not None:
                return _materialize_build_export(root, output, plan=plan, before_publish=before_publish)
            check(materialize_build_export(root, output, plan=plan) == envelope,
                  "$.envelope", "original Build bytes changed before publication")
            return envelope
    except OSError as error:
        raise MbError("cannot materialize private Build download", reason="ci-transport") from error


def _materialize_runtime(reads: CommandReads, descriptor: dict[str, Any], owning_build: dict[str, Any],
                         plan: dict[str, Any], output: Path,
                         before_publish: Callable[[], None] | None) -> dict[str, Any]:
    """Download an authenticated complete results aggregate by id and publish a verified private copy.

    What is published is what was bound: with ``before_publish`` the extracted source is verified
    against the bound envelope once more inside the atomic copy; without it the copy is a child of
    the caller's own unpublished stage and must carry the bound envelope."""

    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-runtime-", dir=output.parent) as temporary:
            root = Path(temporary) / "export"
            extract_runtime(data, root, scope="complete")
            del data
            envelope = verify_runtime_export(root, plan=plan)
            bind_runtime_envelope(envelope, descriptor=descriptor, owning_build=owning_build, plan=plan)
            changed = "original runtime bytes changed before publication"
            if before_publish is None:
                check(materialize_runtime_export(root, output, plan=plan) == envelope, "$.envelope", changed)
                return envelope

            def admitted() -> None:
                check(verify_runtime_export(root, plan=plan) == envelope, "$.envelope", changed)
                before_publish()

            return _materialize_runtime_export(root, output, plan=plan, before_publish=admitted)
    except OSError as error:
        raise MbError("cannot materialize private runtime download", reason="ci-transport") from error


def _download_build(reads: CommandReads, watch: Watch, descriptor: dict[str, Any], plan: dict[str, Any],
                    output: Path) -> dict[str, Any]:
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    _authenticate_build(reads, watch, descriptor, plan)
    return _materialize_build(reads, descriptor, plan, output, watch.recheck)


def download_completed_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                             output: Path) -> dict[str, Any]:
    """Authenticate and privately copy one complete bundle by immutable numeric ID.

    The bundle must come from a completed successful full run of the managed Build caller for the
    plan's subject. Caller supplies an independently admitted plan, owns the output parent and
    excludes worker writes. Whether this run is the newest one, native compiler validity and final
    gate authorization are additional obligations. A Build still running in the reader's own run
    is not this route.
    """

    plan, descriptor = _plan(plan), _descriptor(descriptor)
    reads, watch = CommandReads.of(api), Watch()
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    _bound(descriptor, plan, "build", None)
    check(ci_producer(descriptor["producer"]["workflow_path"]) == "build",
          "$.producer.workflow_path", "a Build rebuilt inside a packaged run is read by that run only")
    _admit_source(reads, watch, plan["identity"])
    return _download_build(reads, watch, descriptor, plan, output)


def download_target_set(api: GitHubApi, *, descriptors: list[dict[str, Any]], plan: dict[str, Any],
                        run_id: int, run_attempt: int, output: Path) -> list[dict[str, Any]]:
    """Publish all ordered same-attempt target inputs privately, or publish nothing.

    The reader is the assembling job of the run that built the targets: a full run of the Build
    caller, or a standalone packaged run that rebuilds. That run is still in progress, so only its
    plan, policy and target jobs must have finished. Output children are protected ordinal names
    target-0, target-1, etc. The returned descriptor/envelope pairs bind those positions. This
    prepares native aggregate inputs; it does not mint an aggregate envelope, a native validator
    receipt or a successful final graph.
    """

    plan = _plan(plan)
    List(validate_descriptor, min_items=1, max_items=limits.MAX_CI_TARGETS)(descriptors, "$.descriptors")
    descriptors = [_descriptor(descriptor) for descriptor in descriptors]
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    check(len(descriptors) == len(plan["targets"]), "$.descriptors", "incomplete target input set")
    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    producer = {key: value for key, value in descriptors[0]["producer"].items() if key != "upload_window"}
    check((producer["run_id"], producer["run_attempt"]) == (run_id, run_attempt),
          "$.producer", "targets do not belong to the assembler's exact run and attempt")
    for descriptor, target in zip(descriptors, plan["targets"]):
        _bound(descriptor, plan, "target", target["id"])
        check({key: value for key, value in descriptor["producer"].items() if key != "upload_window"} == producer,
              "$.producer", "mixed target producer")
    check(sum(descriptor["artifact"]["size"] for descriptor in descriptors) <= limits.MAX_CI_TARGET_DOWNLOAD_BYTES,
          "$.descriptors", "target archive set exceeds its additional compressed-byte budget")
    caller = ci_producer(producer["workflow_path"])
    mode = "full" if caller == "build" else "rebuilt"
    check(producer["graph_sha256"] == run_graph(caller, mode).sha256(plan),
          "$.producer.graph_sha256", "targets do not carry the graph of their run")
    targets = [upload_job_name(caller, "target", target["id"]) for target in plan["targets"]]
    finished = [job_name(caller, "build", "plan"), job_name(caller, "build", "policy"), *targets]
    reads, watch = CommandReads.of(api), Watch()

    def uploads() -> list[tuple[str, str]]:
        jobs = reads.attempt_jobs(run_id, run_attempt)
        require_partial_graph(jobs, plan=plan, producer=caller, mode=mode, run_attempt=run_attempt,
                              finished=finished)
        return [sealed_upload(find_job(jobs, name, run_attempt=run_attempt)) for name in targets]

    _admit_source(reads, watch, plan["identity"])
    _authenticate_run(reads, watch, producer, plan, mode=mode, complete=False)
    for descriptor, window in zip(descriptors, watch.read(("producer jobs", run_id, run_attempt), uploads)):
        expected = descriptor["producer"]["upload_window"]
        check(tuple(window) == (expected["started_at"], expected["completed_at"]),
              "$.producer.upload_window", "wrong target upload window")
    _authenticate_artifacts(reads, watch, descriptors, plan["identity"])

    def writer(stage: Path, stage_fd: int) -> list[dict[str, Any]]:
        partitions = []
        files: list[dict[str, Any]] = []
        for index, descriptor in enumerate(descriptors):
            root = stage / f"target-{index}"
            extract_build(_download(reads, descriptor), root)
            envelope = verify_build_export(root, plan=plan)
            bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            partitions.append({"descriptor": descriptor, "envelope": envelope})
            files.extend(envelope["files"])
            check(len(files) <= limits.MAX_CI_EXPORT_FILES
                  and sum(file["size"] for file in files) <= limits.MAX_CI_EXPORT_TREE_BYTES,
                  "$.partitions", "target inputs exceed the original logical export budget")
        validate_target_partitions(partitions, plan=plan)
        validate_tree_entries(stage, max_entries=limits.MAX_CI_TARGET_INPUT_ENTRIES)
        watch.recheck()
        return partitions

    try:
        return atomic_directory(output, writer)
    except OSError as error:
        raise MbError("cannot publish private target inputs", reason="ci-transport") from error


def _gate_mode(descriptor: dict[str, Any], plan: dict[str, Any], gate: str) -> str:
    """The admission of a tested-record descriptor that needs no API: its gate, its binding to the
    plan and the mode of the run that sealed it."""

    check(type(gate) is str and gate in ("build", "packaged"), "$.gate", "wrong protected gate")
    _bound(descriptor, plan, "tested", gate)
    producer = descriptor["producer"]
    return gate_mode(ci_producer(producer["workflow_path"]), gate, plan, producer["graph_sha256"])


def _read_gate(reads: CommandReads, watch: Watch, descriptor: dict[str, Any], plan: dict[str, Any],
               gate: str, temporary_root: Path) -> dict[str, Any]:
    """Authenticate one tested record's run and read its receipt; the caller rechecks the watch.

    The mode of the run is settled from the descriptor's graph digest before any job is read, the
    record is downloaded by id and must be the canonical ``ci-gate.json`` alone, and every source
    artifact it names must still be available. A Build consumed from a separate run is
    authenticated as its own completed full run."""

    mode = _gate_mode(descriptor, plan, gate)
    producer = descriptor["producer"]
    _authenticate_run(reads, watch, producer, plan, mode=mode, complete=True)
    _authenticate_upload(descriptor, _complete_jobs(reads, producer, plan, mode=mode))
    _authenticate_artifacts(reads, watch, [descriptor], plan["identity"])
    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-gate-", dir=temporary_root) as temporary:
            root = Path(temporary) / "record"
            bounds = ExtractionLimits(1, limits.MAX_CI_RECORD_BYTES, limits.MAX_CI_RECORD_BYTES,
                                      suffixes=frozenset({".json"}))
            check(extract(data, root, bounds) == [grammar.CI_GATE_NAME],
                  "$.record", "tested record requires exactly its fixed root filename")
            raw = read_child_file(root, grammar.CI_GATE_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    except OSError as error:
        raise MbError("cannot read private tested gate receipt", reason="ci-transport") from error
    document = load_document(raw, kind="mod-base.ci.gate")
    check(raw == canonical_json(document), "$.record", "tested receipt must be canonical JSON")
    bind_gate_receipt(document, descriptor=descriptor, plan=plan)
    check(document["gate"] == gate and document["mode"] == mode, "$.record", "receipt is another gate or mode")
    owning = document["owning_build"]
    sources = list(document["artifacts"])
    if owning is not None and mode == "rebuilt":
        sources.append(owning)
    elif owning is not None:
        _authenticate_build(reads, watch, owning, plan)
    authenticate_gate_timeline(reads, document=document, descriptor=descriptor, plan=plan)
    _authenticate_artifacts(reads, watch, sources, plan["identity"])
    return document


def download_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                          gate: str, temporary_root: Path) -> dict[str, Any]:
    """Read one canonical tested receipt of the live subject by numeric ID.

    Caller owns a private temporary parent and supplies an independently protected plan. The
    record's run, exact graph for its mode, gate seal and upload, source artifact metadata and
    availability and exact execution chronology are checked, and the mutable part is checked again
    before the receipt is returned. Source payload bytes, native semantics, whether this run is the
    newest one and status authority remain additional proofs. Reuse references are not read here.
    """

    plan, descriptor = _plan(plan), _descriptor(descriptor)
    reads, watch = CommandReads.of(api), Watch()
    _gate_mode(descriptor, plan, gate)
    _admit_source(reads, watch, plan["identity"])
    document = _read_gate(reads, watch, descriptor, plan, gate, temporary_root)
    watch.recheck()
    return document


def _merged(plan: dict[str, Any], controller_sha: str, merged_sha: str) -> tuple[str, str]:
    grammar.require_sha1(controller_sha, "current protected controller SHA")
    grammar.require_sha1(merged_sha, "final merged SHA")
    check(plan["identity"]["pr_number"] > 0, "$.identity", "historical gate requires an original PR plan")
    return controller_sha, merged_sha


def download_merged_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                                 gate: str, controller_sha: str, merged_sha: str,
                                 temporary_root: Path) -> dict[str, Any]:
    """Read an original PR gate receipt after merge, preserving original producer identities.

    Caller independently admits the original plan, the current controller, the final merged SHA
    and a private temporary parent. The original runs stay recorded under the pull request's head.
    Original/current native policy and pin equivalence, both coherent gates, source payload bytes,
    newest-run selection and owner/writer authority remain mandatory. This reads one original
    tested seal, never a reuse chain, and authorizes no effects or reuse.
    """

    plan, descriptor = _plan(plan), _descriptor(descriptor)
    merged = _merged(plan, controller_sha, merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    _gate_mode(descriptor, plan, gate)
    _admit_source(reads, watch, plan["identity"], merged)
    document = _read_gate(reads, watch, descriptor, plan, gate, temporary_root)
    watch.recheck()
    return document


def _merged_pair(reads: CommandReads, watch: Watch, *, build_descriptor: dict[str, Any],
                 packaged_descriptor: dict[str, Any], plan: dict[str, Any], merged: tuple[str, str],
                 temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both original receipts of one merged pull request; the caller rechecks the watch."""

    _gate_mode(build_descriptor, plan, "build")
    _gate_mode(packaged_descriptor, plan, "packaged")
    check(build_descriptor["artifact"]["id"] != packaged_descriptor["artifact"]["id"]
          and build_descriptor["producer"]["run_id"] != packaged_descriptor["producer"]["run_id"],
          "$.pair", "original Build and packaged seals require independent artifacts and runs")
    _admit_source(reads, watch, plan["identity"], merged)
    build = _read_gate(reads, watch, build_descriptor, plan, "build", temporary_root)
    packaged = _read_gate(reads, watch, packaged_descriptor, plan, "packaged", temporary_root)
    check(packaged["owning_build"] == build["artifacts"][0],
          "$.pair.build", "packaged gate consumed a different complete Build bundle")
    return build, packaged


def _pair_inputs(*, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any],
                 plan: dict[str, Any], controller_sha: str,
                 merged_sha: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], tuple[str, str]]:
    plan = _plan(plan)
    return plan, _descriptor(build_descriptor), _descriptor(packaged_descriptor), _merged(plan, controller_sha,
                                                                                         merged_sha)


def download_merged_gate_pair(api: GitHubApi, *, build_descriptor: dict[str, Any],
                              packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                              controller_sha: str, merged_sha: str,
                              temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a coherent pair of original PR seals with the exact consumed Build bundle.

    The original protected plan and the current controller/final SHA require independent
    admission. Each seal is read once; the historical source, both runs' latest attempts and every
    source artifact's availability are observed again before the pair is returned. Native payload
    bytes and semantics, newest eligible run selection, original/current policy and pin and
    authority remain required. Missing, corrupt or moved evidence never produces a partial pair.
    """

    plan, build_descriptor, packaged_descriptor, merged = _pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        controller_sha=controller_sha, merged_sha=merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    documents = _merged_pair(reads, watch, build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor,
                             plan=plan, merged=merged, temporary_root=temporary_root)
    watch.recheck()
    return documents


def _results(packaged: dict[str, Any]) -> dict[str, Any]:
    aggregates = [descriptor for descriptor in packaged["artifacts"]
                  if grammar.parse_ci_artifact_name(descriptor["artifact"]["name"]).kind == "results"]
    check(len(aggregates) == 1, "$.artifacts", "requires exactly one original complete runtime aggregate")
    return aggregates[0]


def download_merged_build(api: GitHubApi, *, build_descriptor: dict[str, Any],
                          packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                          controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]:
    """Copy the exact original complete Build bytes only under coherent historical gate proof.

    Caller owns a private output parent excluding disposable writers and independently admits the
    original plan and the current controller/final SHA. The pair's mutable state is observed again
    inside the atomic copy, before publication. Native Build/runtime validity, complete runtime
    payloads, newest eligible runs, original/current policy and pin and owner/writer authority
    remain required; no native code runs.
    """

    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    plan, build_descriptor, packaged_descriptor, merged = _pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        controller_sha=controller_sha, merged_sha=merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    build, _ = _merged_pair(reads, watch, build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor,
                            plan=plan, merged=merged, temporary_root=output.parent)
    return _materialize_build(reads, build["artifacts"][0], plan, output, watch.recheck)


def download_merged_runtime(api: GitHubApi, *, build_descriptor: dict[str, Any],
                            packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                            controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]:
    """Copy the coherent original pair's complete results bytes, retaining all original identities.

    Independently admit private writer-excluded output ancestry and the original plan. Native
    runtime/Build validation, actual owning Build bytes, newest eligible sources, original/current
    policy and pin and later consumer/writer admission remain mandatory. This reader activates no
    reuse, native success, status or settlement authority.
    """

    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    plan, build_descriptor, packaged_descriptor, merged = _pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        controller_sha=controller_sha, merged_sha=merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    build, packaged = _merged_pair(reads, watch, build_descriptor=build_descriptor,
                                   packaged_descriptor=packaged_descriptor, plan=plan, merged=merged,
                                   temporary_root=output.parent)
    return _materialize_runtime(reads, _results(packaged), build["artifacts"][0], plan, output, watch.recheck)


def download_merged_inputs(api: GitHubApi, *, build_descriptor: dict[str, Any],
                           packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                           controller_sha: str, merged_sha: str,
                           output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish exact original Build/runtime bytes together under one final coherent admission.

    Fixed private children are build and runtime. Neither is published as a caller output if
    either read or the final admission fails. Caller owns private ancestry and excludes writers;
    native domain validity, policy/pin/newest-source/consumer and effect authority remain separate.
    """

    check(isinstance(output, Path) and not os.path.lexists(output), "$.output", "invalid or preexisting output")
    plan, build_descriptor, packaged_descriptor, merged = _pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        controller_sha=controller_sha, merged_sha=merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    build, packaged = _merged_pair(reads, watch, build_descriptor=build_descriptor,
                                   packaged_descriptor=packaged_descriptor, plan=plan, merged=merged,
                                   temporary_root=output.parent)
    bundle, results = build["artifacts"][0], _results(packaged)

    def writer(stage: Path, stage_fd: int) -> tuple[dict[str, Any], dict[str, Any]]:
        build_root, runtime_root = stage / "build", stage / "runtime"
        build_envelope = _materialize_build(reads, bundle, plan, build_root, None)
        runtime_envelope = _materialize_runtime(reads, results, bundle, plan, runtime_root, None)
        watch.recheck()
        validate_tree_entries(stage, max_entries=limits.MAX_CI_ORIGINAL_INPUT_ENTRIES)
        with os.scandir(stage) as entries:
            names = sorted(entry.name for entry in itertools.islice(entries, 3))
        check(names == ["build", "runtime"], "$.inputs", "combined inputs differ from fixed private children")
        check(verify_build_export(build_root, plan=plan) == build_envelope,
              "$.build", "original Build bytes changed before combined publication")
        check(verify_runtime_export(runtime_root, plan=plan) == runtime_envelope,
              "$.runtime", "original runtime bytes changed before combined publication")
        return build_envelope, runtime_envelope

    try:
        return atomic_directory(output, writer)
    except OSError as error:
        raise MbError("cannot publish private original Build/runtime inputs", reason="ci-transport") from error
