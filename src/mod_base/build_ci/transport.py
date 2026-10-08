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

A selected artifact that its run no longer lists, or that has expired, is rejected like every other
inconsistency (:class:`ArtifactUnavailable`, still a document error). The readers of a merged pull
request's original gates say more: evidence that is gone is an ordinary reason to test again, so
they report it as :class:`OriginalUnavailable`, and only what is still there can be corrupt.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterator
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
from mod_base.build_ci.records import (bind_build_envelope, bind_gate_receipt, bind_results_index,
                                       validate_descriptor)
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.build_ci.runtime_schema import bind_runtime_envelope
from mod_base.build_ci.validation import verify_validation_export
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError, GitHubApi
from mod_base.github.artifacts import Artifact
from mod_base.github.runs import referenced_workflows
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.bounded_zip import ExtractionLimits, extract, extract_build, extract_runtime
from mod_base.io.tree import read_child_file, validate_tree_entries
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.model.documents import load_document
from mod_base.model.validators import DocumentError, Int, List, check
from mod_base.workflow import ci_producer, find_job

_RUN_FIELDS = ("id", "run_attempt", "status", "conclusion", "path", "event", "head_sha", "head_branch", "created_at")
#: The two reads of one artifact by its numeric id: its metadata and its archive.
_ARTIFACT_PATH = re.compile(r"^/repos/[^/]+/[^/]+/actions/artifacts/[0-9]+(?:/zip)?$")


class ArtifactUnavailable(DocumentError):
    """A selected artifact is no longer listed for its producer run, or has expired.

    For a route that needs the artifact this is a rejection like every other document error. It
    has a class of its own because nothing about the artifact is wrong: it is gone."""


class OriginalUnavailable(Unavailable):
    """An artifact of a merged pull request's original gates is gone or has expired (exit 3).

    The original evidence can no longer be authenticated. That is not corruption: whoever meant to
    reuse the evidence tests again. Every other failure of the merged readers stays a rejection."""

    default_reason = "ci-original-unavailable"


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
    if sorted(states) != list(ids):
        raise ArtifactUnavailable("$.artifact", "selected artifact is no longer listed for its producer run")
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
            check(state["run_id"] == run_id and state["head_sha"] == head_sha and state["head_branch"] == head_branch,
                  "$.artifact", "expired artifact or wrong protected producer")
            if state["expired"]:
                raise ArtifactUnavailable("$.artifact", "expired artifact or wrong protected producer")


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

    The artifact is what the assembling job uploaded: the export and, beside its envelope, the
    validation record of that export with its reports (:func:`_verify_sealed_build`). The record
    is moved aside and must be the one frozen for exactly these bytes in the descriptor's
    attempt; what is published is the export alone, exactly its inventory. An artifact that holds
    no record is read as the bare export.

    Without ``before_publish`` the copy is a child of the caller's own unpublished stage, and it
    must carry the very envelope that was bound here."""

    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-download-", dir=output.parent) as temporary:
            root = Path(temporary) / "export"
            extract_build(data, root)
            del data
            if os.path.lexists(root / grammar.CI_VALIDATION_NAME):
                envelope, _ = _verify_sealed_build(root, Path(temporary) / "validation", descriptor, plan, None)
            else:
                envelope = verify_build_export(root, plan=plan)
                bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            check((envelope["scope"], envelope["target_id"]) == ("complete", None),
                  "$.envelope.scope", "export is not the complete Build bundle")
            if before_publish is not None:
                return _materialize_build_export(root, output, plan=plan, before_publish=before_publish)
            check(materialize_build_export(root, output, plan=plan) == envelope,
                  "$.envelope", "original Build bytes changed before publication")
            return envelope
    except OSError as error:
        raise MbError("cannot materialize private Build download", reason="ci-transport") from error


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
                        run_id: int, run_attempt: int, output: Path,
                        source_config_sha256: str | None = None) -> list[dict[str, Any]]:
    """Publish all ordered same-attempt target inputs privately, or publish nothing.

    The reader is the assembling job of the run that built the targets: a full run of the Build
    caller, or a standalone packaged run that rebuilds. That run is still in progress, so only its
    plan, policy and target jobs must have finished. Output children are protected ordinal names
    target-0, target-1, etc. The returned descriptor/envelope pairs bind those positions. This
    prepares native aggregate inputs; it does not mint an aggregate envelope, a native validator
    receipt or a successful final graph.

    With ``source_config_sha256``, the digest of the protected Build config the reader loaded,
    every artifact is what a target job uploads: the partition with the validation record of its
    ``verify_target`` run beside the envelope. The record is verified against the partition and
    kept out of the published inputs. Without it every artifact must be the bare partition.
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
            if source_config_sha256 is None:
                envelope = verify_build_export(root, plan=plan)
                bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            else:
                with tempfile.TemporaryDirectory(prefix="mb-ci-receipt-", dir=output.parent) as temporary:
                    envelope, _ = _verify_sealed_build(root, Path(temporary) / "validation", descriptor, plan,
                                                       source_config_sha256)
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


# -- A job's reads of its own attempt: sealed exports with their validation record, and the index -----


def _detach_validation(root: Path, receipt: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Split an extracted sealed artifact into its two exactly inventoried trees; return the
    validation record as it was read, before anything it says is verified.

    A sealing job uploads its frozen export together with the validation record of that export:
    ``ci-validation.json`` and the reports it inventories lie beside the outer envelope. They are
    moved from ``root`` into the new private directory ``receipt``, so that the export left in
    ``root`` and the validation export in ``receipt`` can each be verified against its own
    inventory; a file that belongs to neither fails one of the two."""

    raw = read_child_file(root, grammar.CI_VALIDATION_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    document = load_document(raw, kind="mod-base.ci.validation", plan=plan)
    for relative in (grammar.CI_VALIDATION_NAME, *(report["path"] for report in document["reports"])):
        moved = receipt / relative
        moved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.rename(root / relative, moved)
    return document


def _verify_sealed_build(root: Path, receipt: Path, descriptor: dict[str, Any], plan: dict[str, Any],
                         source_config_sha256: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify an extracted sealed Build artifact, a target partition or the complete bundle:
    ``(envelope, validation record)``. The export stays in ``root``, exactly its inventory.

    The export is bound to its descriptor, whose kind fixes the scope. The validation record is
    the one the uploading job froze for exactly these bytes: the ``verify_target`` hook of that
    target, or ``verify_build``, in the descriptor's attempt, under the protected Build config the
    reader itself loaded (``source_config_sha256``), over the canonical envelope.

    A job of the uploading attempt always passes that digest. The reader of a finished Build
    consumes it for another job and passes None: the record then answers for the config digest it
    carries, which the gate of its own run compared with that run's checkout, and the reader
    authenticates that run, its controller commit and its complete graph."""

    recorded = _detach_validation(root, receipt, plan)
    envelope = verify_build_export(root, plan=plan)
    bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
    producer = descriptor["producer"]
    validation = verify_validation_export(
        receipt, plan=plan, hook="verify_build" if envelope["scope"] == "complete" else "verify_target",
        unit_id=envelope["target_id"], run_id=producer["run_id"], run_attempt=producer["run_attempt"],
        source_config_sha256=recorded["source_config_sha256"] if source_config_sha256 is None
        else source_config_sha256,
        input_sha256=hashlib.sha256(canonical_json(envelope)).hexdigest())
    return envelope, validation


def _read_sealed_build(reads: CommandReads, descriptor: dict[str, Any], plan: dict[str, Any], *,
                       source_config_sha256: str, temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Download the complete Build of the reader's own attempt by id and verify it in a private
    temporary directory (:func:`_verify_sealed_build`). The caller authenticates the run, its
    jobs and the artifact."""

    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-sealed-", dir=temporary_root) as temporary:
            root = Path(temporary) / "export"
            extract_build(data, root)
            del data
            return _verify_sealed_build(root, Path(temporary) / "validation", descriptor, plan, source_config_sha256)
    except OSError as error:
        raise MbError("cannot read the sealed Build of this attempt", reason="ci-transport") from error


def _read_sealed_lane(reads: CommandReads, descriptor: dict[str, Any], plan: dict[str, Any], *,
                      owning_build: dict[str, Any], build_envelope_sha256: str, source_config_sha256: str,
                      temporary_root: Path) -> dict[str, str]:
    """Download one lane's results of the reader's own attempt by id and verify them in a private
    temporary directory, one lane at a time: the SHA-256 of its canonical runtime envelope, of its
    canonical validation record and of the lane's verification report.

    The export is the lane's and is bound to its descriptor and to ``owning_build``, the complete
    Build the reader selected. The validation record is the one the lane job froze for exactly
    these bytes: the ``verify_runtime`` hook of that lane in the descriptor's attempt, under the
    protected Build config the reader itself loaded, over the digest of the plan, of the owning
    Build's envelope (``build_envelope_sha256``; the bundle itself is not read) and of this runtime
    envelope. The caller authenticates the run, its jobs and the artifact."""

    data = _download(reads, descriptor)
    producer = descriptor["producer"]
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-sealed-", dir=temporary_root) as temporary:
            root, receipt = Path(temporary) / "export", Path(temporary) / "validation"
            extract_runtime(data, root, scope="lane")
            del data
            _detach_validation(root, receipt, plan)
            envelope = verify_runtime_export(root, plan=plan)
            bind_runtime_envelope(envelope, descriptor=descriptor, owning_build=owning_build, plan=plan)
            envelope_sha256 = canonical_sha256(envelope)
            validation = verify_validation_export(
                receipt, plan=plan, hook="verify_runtime", unit_id=envelope["lane_id"], run_id=producer["run_id"],
                run_attempt=producer["run_attempt"], source_config_sha256=source_config_sha256,
                input_sha256=canonical_sha256({
                    "format": grammar.CI_RUNTIME_INPUT_FORMAT, "plan_sha256": plan["plan_sha256"],
                    "build_envelope_sha256": build_envelope_sha256, "runtime_envelope_sha256": envelope_sha256}))
            return {"envelope_sha256": envelope_sha256, "validation_sha256": canonical_sha256(validation),
                    "report_sha256": validation["reports"][0]["sha256"]}
    except OSError as error:
        raise MbError("cannot read the sealed results of a lane of this attempt", reason="ci-transport") from error


def _read_results(reads: CommandReads, descriptor: dict[str, Any], plan: dict[str, Any],
                  temporary_root: Path) -> dict[str, Any]:
    """Download a results index by id: the canonical ``ci-results.json`` alone, valid for the plan
    and bound to its descriptor. The caller authenticates the run and every artifact it lists."""

    data = _download(reads, descriptor)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-results-", dir=temporary_root) as temporary:
            root = Path(temporary) / "record"
            bounds = ExtractionLimits(1, limits.MAX_CI_RECORD_BYTES, limits.MAX_CI_RECORD_BYTES,
                                      suffixes=frozenset({".json"}))
            check(extract(data, root, bounds) == [grammar.CI_RESULTS_NAME],
                  "$.record", "results index requires exactly its fixed root filename")
            raw = read_child_file(root, grammar.CI_RESULTS_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    except OSError as error:
        raise MbError("cannot read the private results index", reason="ci-transport") from error
    document = load_document(raw, kind="mod-base.ci.results")
    check(raw == canonical_json(document), "$.record", "results index must be canonical JSON")
    return bind_results_index(document, descriptor=descriptor, plan=plan)


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


@contextlib.contextmanager
def _original_evidence() -> Iterator[None]:
    """Report an original artifact that is gone as :class:`OriginalUnavailable`.

    GitHub says so in three ways: it lists the artifact as expired, the run no longer lists it, or
    a read of its numeric id answers 404 or 410. The artifact's immutable metadata and its owner
    are compared first, and every other failure passes unchanged: an API error is never absence."""

    try:
        yield
    except ArtifactUnavailable as error:
        raise OriginalUnavailable(f"original evidence is gone: {error}") from error
    except ApiError as error:
        if error.status not in (404, 410) or error.method != "GET" or _ARTIFACT_PATH.fullmatch(error.path) is None:
            raise
        raise OriginalUnavailable(f"original evidence is gone: GitHub answers {error.status} for "
                                  f"{error.path}") from error


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
    tested seal, never a reuse chain, and authorizes no effects or reuse. A record or source
    artifact that is gone raises :class:`OriginalUnavailable`; one that disappears while the
    receipt is read is a rejection like every other change.
    """

    plan, descriptor = _plan(plan), _descriptor(descriptor)
    merged = _merged(plan, controller_sha, merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    _gate_mode(descriptor, plan, gate)
    _admit_source(reads, watch, plan["identity"], merged)
    with _original_evidence():
        document = _read_gate(reads, watch, descriptor, plan, gate, temporary_root)
    watch.recheck()
    return document


def _merged_pair(reads: CommandReads, watch: Watch, *, build_descriptor: dict[str, Any],
                 packaged_descriptor: dict[str, Any], plan: dict[str, Any], merged: tuple[str, str],
                 temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both original receipts of one merged pull request; the caller rechecks the watch.

    ``watch`` must not already hold the source of another subject: a route that also admits the
    live subject covering this pull request gives the pair a watch of its own. A seal or source
    artifact that is gone raises :class:`OriginalUnavailable`."""

    _gate_mode(build_descriptor, plan, "build")
    _gate_mode(packaged_descriptor, plan, "packaged")
    check(build_descriptor["artifact"]["id"] != packaged_descriptor["artifact"]["id"]
          and build_descriptor["producer"]["run_id"] != packaged_descriptor["producer"]["run_id"],
          "$.pair", "original Build and packaged seals require independent artifacts and runs")
    _admit_source(reads, watch, plan["identity"], merged)
    with _original_evidence():
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
    authority remain required. Missing, corrupt or moved evidence never produces a partial pair:
    an artifact that is gone raises :class:`OriginalUnavailable`, corruption and a change while the
    pair is read are rejections.
    """

    plan, build_descriptor, packaged_descriptor, merged = _pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        controller_sha=controller_sha, merged_sha=merged_sha)
    reads, watch = CommandReads.of(api), Watch()
    documents = _merged_pair(reads, watch, build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor,
                             plan=plan, merged=merged, temporary_root=temporary_root)
    watch.recheck()
    return documents


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
