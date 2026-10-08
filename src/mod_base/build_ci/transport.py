"""Numeric-ID transport for Build/runtime exports and full tested gates, independent of Pages (MB11)."""

from __future__ import annotations

import hashlib
import itertools
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import authenticate_merged_pr_identity, authenticate_source_identity
from mod_base.build_ci.exports import (_materialize_build_export, materialize_build_export,
                                       validate_target_partitions, verify_build_export)
from mod_base.build_ci.graph import BuildGraphV1, authenticate_gate_timeline, authenticate_graph
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.records import bind_build_envelope, validate_descriptor
from mod_base.build_ci.runtime_exports import _materialize_runtime_export, verify_runtime_export
from mod_base.build_ci.runtime_schema import bind_runtime_envelope
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.artifacts import Artifact
from mod_base.github.jobs import (actions_time, attempt_jobs, job_graph_sha256,
                                  require_successful_step, step_window)
from mod_base.github.runs import get_run, get_run_attempt, referenced_kit_sha, validate_run
from mod_base.io.bounded_zip import ExtractionLimits, extract, extract_build, extract_runtime
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import read_child_file, validate_tree_entries
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.documents import load_document
from mod_base.model.validators import Int, List, check
from mod_base.workflow import (CI_BUILD_CALL, CI_BUILD_JOBS, CI_PACKAGED_CALL, CI_PACKAGED_JOBS,
                               CI_SEAL_STEP, CI_UPLOAD_STEP, find_job)


def _authenticate(api: GitHubApi, descriptor: dict[str, Any], plan: dict[str, Any],
                  target_id: str | None = None, *, source_admission: Callable[[], None] | None = None) -> None:
    jobs = _authenticate_producer(api, descriptor["producer"], plan, complete=target_id is None,
                                  source_admission=source_admission)
    _authenticate_upload(descriptor["producer"], jobs, target_id)
    _authenticate_artifact(api, descriptor, plan["identity"])


def _authenticate_producer(api: GitHubApi, producer: dict[str, Any], plan: dict[str, Any],
                           *, complete: bool, source_admission: Callable[[], None] | None = None) -> list[dict[str, Any]]:
    _authenticate_run(api, producer, plan, complete=complete, source_admission=source_admission)
    if complete:
        digest = authenticate_graph(api, plan=plan, producer="build", run_id=producer["run_id"],
                                    run_attempt=producer["run_attempt"])
    else:
        digest = job_graph_sha256(BuildGraphV1().jobs(plan))
    check(digest == producer["graph_sha256"], "$.producer.graph_sha256", "wrong authenticated Build graph")
    jobs = attempt_jobs(api, producer["run_id"], producer["run_attempt"])
    if not complete:
        names = [job["name"] for job in jobs]
        expected = {entry["name"] for entry in BuildGraphV1().jobs(plan)}
        check(len(names) == len(set(names)) and set(names) <= expected,
              "$.jobs", "partial Build graph has duplicate or unenrolled jobs")
    return jobs


def _authenticate_run(api: GitHubApi, producer: dict[str, Any], plan: dict[str, Any], *, complete: bool,
                       source_admission: Callable[[], None] | None = None) -> None:
    identity = plan["identity"]
    if source_admission is None:
        authenticate_source_identity(api, identity)
    else:
        source_admission()
    latest = get_run(api, producer["run_id"])
    check(type(latest.get("run_attempt")) is int and latest["run_attempt"] == producer["run_attempt"],
          "$.producer.run_attempt", "a newer producer attempt exists or attempt is malformed")
    run = get_run_attempt(api, producer["run_id"], producer["run_attempt"])
    expected_head = identity["controller_sha"]
    if producer["event"] == "push":
        check(identity["tested_sha"] == expected_head, "$.tested_sha", "push subject differs from executing controller")
    check(producer["api_head_sha"] == expected_head, "$.producer.api_head_sha", "wrong protected API head")
    for observed in (latest, run):
        validate_run(observed, repository=api.repository, workflow_path=producer["workflow_path"],
                     events=(producer["event"],), head_branch=identity["base_branch"], head_sha=expected_head,
                     workflow_id=run["workflow_id"], require_success=complete)
        if not complete:
            check((observed.get("status"), observed.get("conclusion")) in
                  (("in_progress", None), ("completed", "success")),
                  "$.producer.status", "target producer is queued, failed or cancelled")
        check(referenced_kit_sha(observed) == identity["kit"]["sha"], "$.producer.kit", "wrong executing kit")


def _authenticate_upload(producer: dict[str, Any], jobs: list[dict[str, Any]], target_id: str | None) -> None:
    unit = CI_BUILD_JOBS["assemble"] if target_id is None else CI_BUILD_JOBS["target"].format(id=target_id)
    _authenticate_named_upload(producer, jobs, f"{CI_BUILD_CALL} / {unit}")


def _authenticate_named_upload(producer: dict[str, Any], jobs: list[dict[str, Any]], name: str) -> None:
    job = find_job(jobs, name, run_attempt=producer["run_attempt"])
    check(job.get("status") == "completed" and job.get("conclusion") == "success",
          "$.jobs.target", "export job did not succeed")
    require_successful_step(job, CI_SEAL_STEP)
    require_successful_step(job, CI_UPLOAD_STEP)
    check(step_window(job, CI_SEAL_STEP)[1] <= step_window(job, CI_UPLOAD_STEP)[0],
          "$.jobs.steps", "upload started before sealing finished")
    actual_window = step_window(job, CI_UPLOAD_STEP)
    expected_window = tuple(actions_time(producer["upload_window"][key], key)
                            for key in ("started_at", "completed_at"))
    check(actual_window == expected_window, "$.producer.upload_window", "wrong aggregate upload window")


def download_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                          gate: str, workflow_path: str, build_workflow_path: str,
                          temporary_root: Path) -> dict[str, Any]:
    """Read one canonical full tested receipt by numeric ID with bracketed API admission.

    Caller owns a private temporary parent and supplies independently protected plan/workflow
    enrollment. Source artifact metadata/availability and exact execution chronology are checked;
    source payload bytes/native semantics, newest-run selection, caller graph and status authority
    remain additional proofs. This route admits completed full runs, never historical reuse.
    """

    return _download_gate_receipt(api, descriptor=descriptor, plan=plan, gate=gate,
                                  workflow_path=workflow_path, build_workflow_path=build_workflow_path,
                                  temporary_root=temporary_root, merged=None)


def download_merged_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                                 gate: str, workflow_path: str, build_workflow_path: str,
                                 controller_sha: str, merged_sha: str, temporary_root: Path) -> dict[str, Any]:
    """Read an original full PR gate after merge, preserving original producer identities.

    Caller independently admits original plan/workflows, current controller/final merged SHA
    and private temporary parent. Original/current native policy and pin equivalence, both
    coherent gates, source payload bytes/native validity, newest-run selection, actual later
    consumer chronology, caller graph and owner/writer authority remain mandatory. This reads
    one original full tested seal, never a reuse chain, and authorizes no effects or reuse.
    """
    validate_plan(plan)
    validate_descriptor(descriptor)
    grammar.require_sha1(controller_sha, 'current historical gate controller SHA')
    grammar.require_sha1(merged_sha, 'final historical gate merged SHA')
    check(plan['identity']['pr_number'] > 0, '$.identity', 'historical gate requires an original PR plan')
    plan_raw, descriptor_raw = canonical_json(plan), canonical_json(descriptor)
    retained_plan = strict_loads(plan_raw, label='original gate plan', max_bytes=limits.MAX_CI_PLAN_BYTES)
    retained_descriptor = strict_loads(descriptor_raw, label='original gate descriptor', max_bytes=limits.MAX_CI_RECORD_BYTES)
    document = _download_gate_receipt(api, descriptor=retained_descriptor, plan=retained_plan, gate=gate,
                                     workflow_path=workflow_path, build_workflow_path=build_workflow_path,
                                     temporary_root=temporary_root, merged=(controller_sha, merged_sha))
    validate_plan(plan)
    validate_descriptor(descriptor)
    check(canonical_json(plan) == plan_raw and canonical_json(descriptor) == descriptor_raw,
          '$.inputs', 'caller original gate plan/descriptor changed during historical admission')
    return document


def _merged_pair_inputs(*, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any],
                         plan: dict[str, Any], build_workflow_path: str, packaged_workflow_path: str,
                         controller_sha: str, merged_sha: str) -> tuple[bytes, tuple[bytes, ...], dict[str, Any], tuple[dict[str, Any], ...]]:
    """Bounded original pair argument/snapshot admission before API IO."""
    validate_plan(plan)
    grammar.require_sha1(controller_sha, 'current paired gate controller SHA')
    grammar.require_sha1(merged_sha, 'final paired gate merged SHA')
    for path in (build_workflow_path, packaged_workflow_path):
        grammar.require(grammar.WORKFLOW_PATH, path, 'original paired gate workflow')
    check(plan['identity']['pr_number'] > 0 and build_workflow_path != packaged_workflow_path,
          '$.pair', 'requires independent original PR gate workflows')
    descriptors = (build_descriptor, packaged_descriptor)
    for descriptor, gate, workflow in zip(descriptors, ('build', 'packaged'),
                                           (build_workflow_path, packaged_workflow_path)):
        validate_descriptor(descriptor)
        check(all(descriptor[key] == plan[key] for key in ('identity', 'plan_sha256', 'profile')),
              '$.pair.binding', 'paired gate differs from original admitted plan')
        name = grammar.parse_ci_artifact_name(descriptor['artifact']['name'])
        check((name.kind, name.unit_id) == ('tested', gate)
              and descriptor['producer']['workflow_path'] == workflow,
              '$.pair.gate', 'paired seal differs from original gate/workflow enrollment')
    check(build_descriptor['artifact']['id'] != packaged_descriptor['artifact']['id']
          and build_descriptor['producer']['run_id'] != packaged_descriptor['producer']['run_id'],
          '$.pair', 'original Build and packaged seals require independent artifacts and runs')
    plan_raw = canonical_json(plan)
    descriptor_raw = tuple(canonical_json(descriptor) for descriptor in descriptors)
    retained_plan = strict_loads(plan_raw, label='original paired gate plan', max_bytes=limits.MAX_CI_PLAN_BYTES)
    retained = tuple(strict_loads(raw, label='original paired gate descriptor', max_bytes=limits.MAX_CI_RECORD_BYTES)
                     for raw in descriptor_raw)
    return plan_raw, descriptor_raw, retained_plan, retained


def download_merged_gate_pair(api: GitHubApi, *, build_descriptor: dict[str, Any],
                              packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                              build_workflow_path: str, packaged_workflow_path: str,
                              controller_sha: str, merged_sha: str,
                              temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read a coherent pair of original full PR seals with the exact consumed Build bundle.

    Original protected plan/workflows and current controller/final SHA require independent
    admission. Both complete record readers run twice, and historical source observations and
    caller snapshots bracket the entire pair. Native payload bytes/semantics, newest eligible
    run selection, original/current policy/pin, actual consumer chronology and authority remain
    required. Missing/error/corrupt/moved evidence never produces a partial pair or reuse success.
    No effect or candidate execution occurs; repeat admission around future consumption/effects.
    """
    plan_raw, descriptor_raw, retained_plan, retained = _merged_pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        build_workflow_path=build_workflow_path, packaged_workflow_path=packaged_workflow_path,
        controller_sha=controller_sha, merged_sha=merged_sha)
    descriptors = (build_descriptor, packaged_descriptor)
    source = authenticate_merged_pr_identity(api, retained_plan['identity'],
                                             controller_sha=controller_sha, merged_sha=merged_sha)

    def read(index: int) -> dict[str, Any]:
        document = download_merged_gate_receipt(api, descriptor=retained[index], plan=retained_plan,
                                                gate=('build', 'packaged')[index],
                                                workflow_path=(build_workflow_path, packaged_workflow_path)[index],
                                                build_workflow_path=build_workflow_path,
                                                controller_sha=controller_sha, merged_sha=merged_sha,
                                                temporary_root=temporary_root)
        check(authenticate_merged_pr_identity(api, retained_plan['identity'],
                                              controller_sha=controller_sha, merged_sha=merged_sha) == source,
              '$.pair.source', 'historical source changed during paired gate admission')
        return document

    documents = (read(0), read(1))
    check(documents[1]['owning_build'] == documents[0]['artifacts'][0],
          '$.pair.build', 'packaged gate consumed a different complete Build bundle')
    fingerprints = tuple(hashlib.sha256(canonical_json(document)).digest() for document in documents)
    for index in (0, 1):
        check(hashlib.sha256(canonical_json(read(index))).digest() == fingerprints[index],
              '$.pair.record', 'original full gate record changed between paired observations')
    check(authenticate_merged_pr_identity(api, retained_plan['identity'],
                                          controller_sha=controller_sha, merged_sha=merged_sha) == source,
          '$.pair.source', 'historical source changed after closing paired gate reads')
    validate_plan(plan)
    for descriptor in descriptors:
        validate_descriptor(descriptor)
    check(canonical_json(plan) == plan_raw
          and tuple(canonical_json(descriptor) for descriptor in descriptors) == descriptor_raw,
          '$.pair.inputs', 'caller original paired plan/descriptors changed during admission')
    return documents


def _download_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                           gate: str, workflow_path: str, build_workflow_path: str,
                           temporary_root: Path, merged: tuple[str, str] | None) -> dict[str, Any]:
    """Shared full record transport; only fixed internal live/historical admission selects source."""

    validate_plan(plan)
    validate_descriptor(descriptor)
    check(type(gate) is str and gate in ("build", "packaged"), "$.gate", "wrong protected gate")
    for path in (workflow_path, build_workflow_path):
        grammar.require(grammar.WORKFLOW_PATH, path, "protected gate workflow")
    check(gate != "build" or workflow_path == build_workflow_path,
          "$.workflow_path", "Build gate differs from protected Build enrollment")
    check(descriptor["producer"]["workflow_path"] == workflow_path,
          "$.producer.workflow_path", "outside protected gate workflow enrollment")
    for key in ("identity", "plan_sha256", "profile"):
        check(descriptor[key] == plan[key], f"$.{key}", "selected gate differs from admitted plan")
    selected = descriptor["artifact"]
    name = grammar.parse_ci_artifact_name(selected["name"])
    check((name.kind, name.unit_id) == ("tested", gate), "$.artifact.name", "wrong selected tested gate")
    producer = descriptor["producer"]
    call, names = (CI_BUILD_CALL, CI_BUILD_JOBS) if gate == "build" else (CI_PACKAGED_CALL, CI_PACKAGED_JOBS)
    original_merged = None

    def source_admission() -> None:
        nonlocal original_merged
        if merged is None:
            authenticate_source_identity(api, plan['identity'])
        else:
            observed = authenticate_merged_pr_identity(api, plan['identity'], controller_sha=merged[0], merged_sha=merged[1])
            if original_merged is None:
                original_merged = observed
            else:
                check(observed == original_merged, '$.source', 'merged PR observations changed during gate transport')

    def authenticate_record() -> None:
        _authenticate_run(api, producer, plan, complete=True, source_admission=source_admission)
        digest = authenticate_graph(api, plan=plan, producer=gate, run_id=producer["run_id"],
                                    run_attempt=producer["run_attempt"])
        check(digest == producer["graph_sha256"], "$.producer.graph_sha256", "wrong selected gate graph")
        jobs = attempt_jobs(api, producer["run_id"], producer["run_attempt"])
        _authenticate_named_upload(producer, jobs, f"{call} / {names['gate']}")
        _authenticate_artifact(api, descriptor, plan["identity"])

    def authenticate_inputs(document: dict[str, Any]) -> None:
        authenticate_gate_timeline(api, document=document, descriptor=descriptor, plan=plan)
        for source in document["artifacts"]:
            _authenticate_artifact(api, source, plan["identity"])
        owning = document["owning_build"]
        if owning is not None:
            check(owning["producer"]["workflow_path"] == build_workflow_path,
                  "$.owning_build.producer.workflow_path", "outside protected owning Build enrollment")
            _authenticate(api, owning, plan, source_admission=source_admission)

    authenticate_record()
    data = api.download(f"/repos/{api.repository}/actions/artifacts/{selected['id']}/zip", max_bytes=selected["size"])
    check(len(data) == selected["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == selected["digest"],
          "$.artifact.digest", "tested-record ZIP length or digest differs")
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-gate-", dir=temporary_root) as temporary:
            root = Path(temporary) / "record"
            bounds = ExtractionLimits(1, limits.MAX_CI_RECORD_BYTES, limits.MAX_CI_RECORD_BYTES,
                                      suffixes=frozenset({".json"}))
            check(extract(data, root, bounds) == [grammar.CI_GATE_NAME],
                  "$.record", "tested record requires exactly its fixed root filename")
            raw = read_child_file(root, grammar.CI_GATE_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
            document = load_document(raw, kind="mod-base.ci.gate")
            check(raw == canonical_json(document), "$.record", "tested receipt must be canonical JSON")
            authenticate_inputs(document)
            authenticate_record()
            authenticate_inputs(document)
            source_admission()
            return document
    except OSError as error:
        raise MbError("cannot read private tested gate receipt", reason="ci-transport") from error


def _authenticate_artifact(api: GitHubApi, descriptor: dict[str, Any], identity: dict[str, Any]) -> None:
    selected, producer = descriptor["artifact"], descriptor["producer"]
    raw = api.get_json(f"/repos/{api.repository}/actions/artifacts/{selected['id']}")
    artifact = Artifact.parse(raw)
    for key, actual in (("id", artifact.id), ("name", artifact.name), ("size", artifact.size),
                        ("digest", artifact.digest), ("created_at", artifact.created_at),
                        ("expires_at", raw.get("expires_at"))):
        check(actual == selected[key], f"$.artifact.{key}", "immutable selected metadata differs")
    check(not artifact.expired and artifact.run_id == producer["run_id"]
          and artifact.head_sha == identity["controller_sha"] and artifact.head_branch == identity["base_branch"],
          "$.artifact", "expired artifact or wrong protected producer")


def download_completed_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                             workflow_path: str, output: Path) -> dict[str, Any]:
    """Authenticate and privately copy one complete bundle by immutable numeric ID.

    Caller supplies an independently admitted plan and protected producer workflow path, owns
    the output parent and excludes worker writes. Newest-run selection, pin/policy authority,
    native compiler validity and final gate authorization are additional obligations. This
    route requires a completed full Build; in-progress target fan-in uses a separate route.
    """

    return _download_export(api, descriptor=descriptor, plan=plan, workflow_path=workflow_path,
                            output=output, target_id=None)


def download_merged_build(api: GitHubApi, *, build_descriptor: dict[str, Any],
                           packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                           build_workflow_path: str, packaged_workflow_path: str,
                           controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]:
    """Copy the exact original complete Build bytes only under coherent historical gate proof.

    Caller owns an original private output parent excluding disposable writers and independently
    admits original plan/workflows, current controller/final SHA. Native Build/runtime validity,
    complete runtime payloads, newest eligible runs, original/current policy/pin, later consumer
    chronology and owner/writer authority remain required. Fixed internal historical/pair/caller
    rechecks run inside the existing atomic byte copy before publication; no native code runs.
    """
    check(isinstance(output, Path) and not os.path.lexists(output), '$.output', 'invalid or preexisting output')
    plan_raw, descriptor_raw, retained_plan, retained = _merged_pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        build_workflow_path=build_workflow_path, packaged_workflow_path=packaged_workflow_path,
        controller_sha=controller_sha, merged_sha=merged_sha)
    source = authenticate_merged_pr_identity(api, retained_plan['identity'],
                                             controller_sha=controller_sha, merged_sha=merged_sha)

    def source_admission() -> None:
        check(authenticate_merged_pr_identity(api, retained_plan['identity'],
                                              controller_sha=controller_sha, merged_sha=merged_sha) == source,
              '$.source', 'historical source changed during complete Build copy')

    def pair() -> tuple[dict[str, Any], dict[str, Any]]:
        return download_merged_gate_pair(api, build_descriptor=retained[0], packaged_descriptor=retained[1],
                                         plan=retained_plan, build_workflow_path=build_workflow_path,
                                         packaged_workflow_path=packaged_workflow_path,
                                         controller_sha=controller_sha, merged_sha=merged_sha,
                                         temporary_root=output.parent)

    documents = pair()
    source_admission()
    fingerprints = tuple(hashlib.sha256(canonical_json(document)).digest() for document in documents)
    bundle = documents[0]['artifacts'][0]

    def before_publish() -> None:
        source_admission()
        check(tuple(hashlib.sha256(canonical_json(document)).digest() for document in pair()) == fingerprints,
              '$.source', 'original coherent gate pair changed before complete Build publication')
        source_admission()
        validate_plan(plan)
        for descriptor in (build_descriptor, packaged_descriptor):
            validate_descriptor(descriptor)
        check(canonical_json(plan) == plan_raw
              and tuple(canonical_json(descriptor) for descriptor in (build_descriptor, packaged_descriptor)) == descriptor_raw,
              '$.inputs', 'caller original gate plan/descriptors changed before complete Build publication')

    return _download_export(api, descriptor=bundle, plan=retained_plan, workflow_path=build_workflow_path,
                            output=output, target_id=None, before_publish=before_publish,
                            source_admission=source_admission)


def download_merged_runtime(api: GitHubApi, *, build_descriptor: dict[str, Any],
                             packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                             build_workflow_path: str, packaged_workflow_path: str,
                             controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]:
    """Copy the coherent original pair's complete results bytes, retaining all original identities.

    Independently admit private writer-excluded output ancestry and original plan/workflows.
    Native runtime/Build validation, actual owning Build bytes, newest eligible sources,
    original/current policy/pin and later consumer/writer admission remain mandatory.
    This reader activates no reuse, native success, status or settlement authority.
    """
    check(isinstance(output, Path) and not os.path.lexists(output), '$.output', 'invalid or preexisting output')
    plan_raw, descriptor_raw, retained_plan, retained = _merged_pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        build_workflow_path=build_workflow_path, packaged_workflow_path=packaged_workflow_path,
        controller_sha=controller_sha, merged_sha=merged_sha)
    source = authenticate_merged_pr_identity(api, retained_plan['identity'],
                                             controller_sha=controller_sha, merged_sha=merged_sha)

    def source_admission() -> None:
        check(authenticate_merged_pr_identity(api, retained_plan['identity'],
                                              controller_sha=controller_sha, merged_sha=merged_sha) == source,
              '$.source', 'historical source changed during complete runtime copy')

    def pair() -> tuple[dict[str, Any], dict[str, Any]]:
        return download_merged_gate_pair(api, build_descriptor=retained[0], packaged_descriptor=retained[1],
                                         plan=retained_plan, build_workflow_path=build_workflow_path,
                                         packaged_workflow_path=packaged_workflow_path,
                                         controller_sha=controller_sha, merged_sha=merged_sha,
                                         temporary_root=output.parent)

    documents = pair()
    source_admission()
    fingerprints = tuple(hashlib.sha256(canonical_json(document)).digest() for document in documents)
    aggregates = [descriptor for descriptor in documents[1]['artifacts']
                  if grammar.parse_ci_artifact_name(descriptor['artifact']['name']).kind == 'results']
    check(len(aggregates) == 1, '$.artifacts', 'requires exactly one original complete runtime aggregate')
    descriptor = aggregates[0]
    selected = descriptor['artifact']
    _authenticate_artifact(api, descriptor, retained_plan['identity'])
    data = api.download(f"/repos/{api.repository}/actions/artifacts/{selected['id']}/zip", max_bytes=selected['size'])
    check(len(data) == selected['size'] and 'sha256:'+hashlib.sha256(data).hexdigest() == selected['digest'],
          '$.artifact.digest', 'runtime ZIP length or SHA-256 differs')

    def before_publish() -> None:
        source_admission()
        check(tuple(hashlib.sha256(canonical_json(document)).digest() for document in pair()) == fingerprints,
              '$.source', 'original coherent gate pair changed before runtime publication')
        source_admission()
        validate_plan(plan)
        for original in (build_descriptor, packaged_descriptor):
            validate_descriptor(original)
        check(canonical_json(plan) == plan_raw
              and tuple(canonical_json(original) for original in (build_descriptor, packaged_descriptor)) == descriptor_raw,
              '$.inputs', 'caller original gate plan/descriptors changed before runtime publication')
        check(verify_runtime_export(root, plan=retained_plan) == envelope,
              '$.envelope', 'original runtime bytes changed before publication')

    try:
        with tempfile.TemporaryDirectory(prefix='mb-ci-runtime-', dir=output.parent) as temporary:
            root = Path(temporary)/'export'
            extract_runtime(data, root, scope='complete')
            del data
            envelope = verify_runtime_export(root, plan=retained_plan)
            bind_runtime_envelope(envelope, descriptor=descriptor, owning_build=documents[0]['artifacts'][0], plan=retained_plan)
            _authenticate_artifact(api, descriptor, retained_plan['identity'])
            source_admission()
            return _materialize_runtime_export(root, output, plan=retained_plan, before_publish=before_publish)
    except OSError as error:
        raise MbError('cannot materialize private original runtime download', reason='ci-transport') from error


def download_merged_inputs(api: GitHubApi, *, build_descriptor: dict[str, Any],
                            packaged_descriptor: dict[str, Any], plan: dict[str, Any],
                            build_workflow_path: str, packaged_workflow_path: str,
                            controller_sha: str, merged_sha: str, output: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Publish exact original Build/runtime bytes together under one final coherent admission.

    Fixed private children are build and runtime. Neither is published as a caller output if
    either read or final admission fails. Caller owns private ancestry and excludes writers;
    native domain validity, policy/pin/newest-source/consumer and effect authority remain separate.
    """
    check(isinstance(output, Path) and not os.path.lexists(output), '$.output', 'invalid or preexisting output')
    plan_raw, descriptor_raw, retained_plan, retained = _merged_pair_inputs(
        build_descriptor=build_descriptor, packaged_descriptor=packaged_descriptor, plan=plan,
        build_workflow_path=build_workflow_path, packaged_workflow_path=packaged_workflow_path,
        controller_sha=controller_sha, merged_sha=merged_sha)
    source = authenticate_merged_pr_identity(api, retained_plan['identity'],
                                             controller_sha=controller_sha, merged_sha=merged_sha)
    arguments = {'build_descriptor': retained[0], 'packaged_descriptor': retained[1], 'plan': retained_plan,
                 'build_workflow_path': build_workflow_path, 'packaged_workflow_path': packaged_workflow_path,
                 'controller_sha': controller_sha, 'merged_sha': merged_sha}

    def source_admission() -> None:
        check(authenticate_merged_pr_identity(api, retained_plan['identity'],
                                              controller_sha=controller_sha, merged_sha=merged_sha) == source,
              '$.source', 'historical source changed during combined input publication')

    documents = download_merged_gate_pair(api, **arguments, temporary_root=output.parent)
    source_admission()
    fingerprints = tuple(hashlib.sha256(canonical_json(document)).digest() for document in documents)
    build = documents[0]['artifacts'][0]
    aggregates = [descriptor for descriptor in documents[1]['artifacts']
                  if grammar.parse_ci_artifact_name(descriptor['artifact']['name']).kind == 'results']
    check(len(aggregates) == 1, '$.artifacts', 'requires one exact original complete runtime aggregate')

    def writer(stage: Path, stage_fd: int) -> tuple[dict[str, Any], dict[str, Any]]:
        build_root, runtime_root = stage/'build', stage/'runtime'
        build_envelope = download_merged_build(api, **arguments, output=build_root)
        source_admission()
        runtime_envelope = download_merged_runtime(api, **arguments, output=runtime_root)
        source_admission()
        bind_build_envelope(build_envelope, descriptor=build, plan=retained_plan)
        bind_runtime_envelope(runtime_envelope, descriptor=aggregates[0], owning_build=build, plan=retained_plan)
        check(tuple(hashlib.sha256(canonical_json(document)).digest() for document in
                    download_merged_gate_pair(api, **arguments, temporary_root=stage)) == fingerprints,
              '$.source', 'original coherent pair changed before combined input publication')
        source_admission()
        validate_plan(plan)
        for descriptor in (build_descriptor, packaged_descriptor):
            validate_descriptor(descriptor)
        check(canonical_json(plan) == plan_raw
              and tuple(canonical_json(descriptor) for descriptor in (build_descriptor, packaged_descriptor)) == descriptor_raw,
              '$.inputs', 'caller original plan/descriptors changed before combined input publication')
        validate_tree_entries(stage, max_entries=limits.MAX_CI_ORIGINAL_INPUT_ENTRIES)
        with os.scandir(stage) as entries:
            names = sorted(entry.name for entry in itertools.islice(entries, 3))
        check(names == ['build', 'runtime'], '$.inputs', 'combined inputs differ from fixed private children')
        check(verify_build_export(build_root, plan=retained_plan) == build_envelope,
              '$.build', 'original Build bytes changed during complete runtime admission')
        check(verify_runtime_export(runtime_root, plan=retained_plan) == runtime_envelope,
              '$.runtime', 'original runtime bytes changed before combined publication')
        return build_envelope, runtime_envelope

    try:
        return atomic_directory(output, writer)
    except OSError as error:
        raise MbError('cannot publish private original Build/runtime inputs', reason='ci-transport') from error


def download_target_partition(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                              workflow_path: str, run_id: int, run_attempt: int,
                              target_id: str, output: Path) -> dict[str, Any]:
    """Copy one sealed target of the protected assembler's exact run/attempt.

    A partial graph is not a successful full Build. Every target, policy, native union validator
    and final full graph remain mandatory. Caller independently authenticates its own run context.
    """

    validate_plan(plan)
    validate_descriptor(descriptor)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    check(type(target_id) is str and target_id in {target["id"] for target in plan["targets"]},
          "$.target_id", "target is outside protected plan")
    check((descriptor["producer"]["run_id"], descriptor["producer"]["run_attempt"]) == (run_id, run_attempt),
          "$.producer", "target does not belong to the assembler's exact run/attempt")
    return _download_export(api, descriptor=descriptor, plan=plan, workflow_path=workflow_path,
                            output=output, target_id=target_id)


def _download_export(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any],
                      workflow_path: str, output: Path, target_id: str | None,
                      before_publish: Callable[[], None] | None = None,
                      source_admission: Callable[[], None] | None = None) -> dict[str, Any]:
    validate_plan(plan)
    validate_descriptor(descriptor)
    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "protected Build workflow")
    check(descriptor["producer"]["workflow_path"] == workflow_path,
          "$.producer.workflow_path", "outside protected producer enrollment")
    for key in ("identity", "plan_sha256", "profile"):
        check(descriptor[key] == plan[key], f"$.{key}", "selected bundle differs from admitted plan")
    selected = descriptor["artifact"]
    name = grammar.parse_ci_artifact_name(selected["name"])
    check((name.kind, name.unit_id) == (("build", None) if target_id is None else ("target", target_id)),
          "$.artifact.name", "artifact does not name the exact required export")
    check(not os.path.lexists(output), "$.output", "output already exists")
    _authenticate(api, descriptor, plan, target_id, source_admission=source_admission)
    data = api.download(f"/repos/{api.repository}/actions/artifacts/{selected['id']}/zip",
                        max_bytes=selected["size"])
    check(len(data) == selected["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == selected["digest"],
          "$.artifact.digest", "download length or SHA-256 differs")
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-download-", dir=output.parent) as temporary:
            root = Path(temporary) / "export"
            extract_build(data, root)
            envelope = verify_build_export(root, plan=plan)
            check((envelope["scope"], envelope["target_id"]) ==
                  (("complete", None) if target_id is None else ("target", target_id)),
                  "$.envelope.scope", "export does not match the required exact scope")
            bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            _authenticate(api, descriptor, plan, target_id, source_admission=source_admission)
            if before_publish is not None:
                return _materialize_build_export(root, output, plan=plan, before_publish=before_publish)
            return materialize_build_export(root, output, plan=plan)
    except OSError as error:
        raise MbError("cannot materialize private Build download", reason="ci-transport") from error


def download_target_set(api: GitHubApi, *, descriptors: list[dict[str, Any]], plan: dict[str, Any],
                        workflow_path: str, run_id: int, run_attempt: int,
                        output: Path) -> list[dict[str, Any]]:
    """Publish all ordered same-attempt target inputs privately, or publish nothing.

    Output children are protected ordinal names target-0, target-1, etc. The returned retained
    descriptors/envelopes bind those positions. This prepares native aggregate inputs; it does
    not mint an aggregate envelope, native validator receipt or successful final graph.
    """

    validate_plan(plan)
    List(validate_descriptor, min_items=1, max_items=limits.MAX_CI_TARGETS)(descriptors, "$.descriptors")
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    grammar.require(grammar.WORKFLOW_PATH, workflow_path, "protected Build workflow")
    check(len(descriptors) == len(plan["targets"]), "$.descriptors", "incomplete target input set")
    check(not os.path.lexists(output), "$.output", "output already exists")
    producer = {key: value for key, value in descriptors[0]["producer"].items() if key != "upload_window"}
    ids: set[int] = set()
    for descriptor, target in zip(descriptors, plan["targets"]):
        for key in ("identity", "plan_sha256", "profile"):
            check(descriptor[key] == plan[key], f"$.{key}", "target input differs from protected plan")
        current = descriptor["producer"]
        check(current["workflow_path"] == workflow_path
              and (current["run_id"], current["run_attempt"]) == (run_id, run_attempt)
              and {key: value for key, value in current.items() if key != "upload_window"} == producer,
              "$.producer", "mixed or unenrolled target producer")
        name = grammar.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check((name.kind, name.unit_id) == ("target", target["id"]), "$.artifact.name", "wrong ordered target")
        check(descriptor["artifact"]["id"] not in ids, "$.artifact.id", "duplicate target artifact")
        ids.add(descriptor["artifact"]["id"])
    check(sum(descriptor["artifact"]["size"] for descriptor in descriptors) <= limits.MAX_CI_TARGET_DOWNLOAD_BYTES,
          "$.descriptors", "target archive set exceeds its additional compressed-byte budget")

    def authenticate_set() -> None:
        jobs = _authenticate_producer(api, descriptors[0]["producer"], plan, complete=False)
        for key in ("plan", "policy"):
            job = find_job(jobs, f"{CI_BUILD_CALL} / {CI_BUILD_JOBS[key]}", run_attempt=run_attempt)
            check(job.get("status") == "completed" and job.get("conclusion") == "success",
                  "$.jobs.policy", "protected plan/policy did not succeed")
        for descriptor, target in zip(descriptors, plan["targets"]):
            _authenticate_upload(descriptor["producer"], jobs, target["id"])
            _authenticate_artifact(api, descriptor, plan["identity"])

    authenticate_set()

    def writer(stage: Path, stage_fd: int) -> list[dict[str, Any]]:
        partitions = []
        files = []
        for index, descriptor in enumerate(descriptors):
            selected = descriptor["artifact"]
            data = api.download(f"/repos/{api.repository}/actions/artifacts/{selected['id']}/zip", max_bytes=selected["size"])
            check(len(data) == selected["size"] and "sha256:" + hashlib.sha256(data).hexdigest() == selected["digest"],
                  "$.artifact.digest", "target ZIP length or digest differs")
            root = stage / f"target-{index}"
            extract_build(data, root)
            del data
            envelope = verify_build_export(root, plan=plan)
            bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
            partitions.append({"descriptor": descriptor, "envelope": envelope})
            files.extend(envelope["files"])
            check(len(files) <= limits.MAX_CI_EXPORT_FILES
                  and sum(file["size"] for file in files) <= limits.MAX_CI_EXPORT_TREE_BYTES,
                  "$.partitions", "target inputs exceed the original logical export budget")
        validate_target_partitions(partitions, plan=plan)
        validate_tree_entries(stage, max_entries=limits.MAX_CI_TARGET_INPUT_ENTRIES)
        authenticate_set()
        return partitions

    return atomic_directory(output, writer)
