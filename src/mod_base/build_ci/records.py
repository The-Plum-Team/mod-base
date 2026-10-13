"""Strict CI records, with full identity/attempt/byte provenance and exact plan coverage.

These are pure structural checks. Metadata and self-reported native hashes never replace
independent Git/API, frozen-byte, domain-witness and complete-graph authentication.
"""

from __future__ import annotations

import copy
from typing import Any

from mod_base import SCHEMA_VERSIONS, readable_schema_versions
from mod_base.build_ci.config import SCOPE_INPUTS
from mod_base.build_ci.protocol import (OUTPUT_ROLES, PROFILES, check_output_paths, check_output_scope,
                                        plan_sha256, validate_identity, validate_plan)
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.validators import Const, Int, List, Nullable, Obj, Str, check, fail
from mod_base.errors import MbError
from mod_base.workflow import ci_producer

SHA1 = Str(g.SHA1, max_len=40)
SHA256 = Str(g.SHA256, max_len=64)
UNIT = Str(g.CI_UNIT_ID, max_len=80)
RUN = Int(1, lim.MAX_RUN_ID)
ATTEMPT = Int(1, lim.MAX_RUN_ATTEMPT)
PROFILE = Str(choices=PROFILES)
#: Gate -> producer (the managed caller whose run sealed it) -> the modes of that run which end in
#: this gate's receipt. A deferred run seals nothing and a reuse run seals a reuse reference instead.
GATE_MODES = {"build": {"build": ("full",), "packaged": ("rebuilt",)},
              "packaged": {"packaged": ("pull-request", "selected", "rebuilt")}}


def _timestamp(value: Any, path: str) -> str:
    try:
        g.parse_timestamp(value, path)
    except MbError as error:
        raise fail(path, "must be a real UTC timestamp") from error
    return value


def _file_path(value: Any, path: str) -> str:
    """A file of a sealed Build export under the name the mod staged it with (a plan's own rule)."""

    check(g.is_export_path(value) and value != g.CI_ENVELOPE_NAME, path,
          "must be a canonical export path distinct from the outer envelope")
    return value


_WINDOW = Obj({"started_at": _timestamp, "completed_at": _timestamp})
_PRODUCER_FIELDS = {
    "run_id": RUN, "run_attempt": ATTEMPT,
    "workflow_path": Str(g.WORKFLOW_PATH, max_len=130),
    "workflow_ref": Str(g.WORKFLOW_REF, max_len=460),
    "api_head_sha": SHA1,
    "event": Str(choices=("pull_request_target", "push", "workflow_dispatch", "schedule")),
    "graph_sha256": SHA256,
}
_PRODUCER_IDENTITY = Obj(_PRODUCER_FIELDS)
_PRODUCER = Obj({**_PRODUCER_FIELDS, "upload_window": _WINDOW})
_ARTIFACT = Obj({
    "id": RUN, "name": Str(max_len=lim.MAX_ARTIFACT_NAME_BYTES),
    "digest": Str(g.DIGEST, max_len=71), "size": Int(1, lim.MAX_CI_BUNDLE_COMPRESSED_BYTES),
    "created_at": _timestamp, "expires_at": _timestamp,
})


def _header(kind: str) -> dict[str, Any]:
    versions = readable_schema_versions(kind)
    return {"kind": Const(kind), "schema_version": Int(min(versions), max(versions))}


def _binding() -> dict[str, Any]:
    return {"identity": validate_identity, "plan_sha256": SHA256, "profile": PROFILE}


def _producer_binding(producer: dict[str, Any], identity: dict[str, Any], path: str) -> str:
    """Bind a producer run to its subject; return the managed caller (``build``/``packaged``) it ran.

    GitHub records a ``pull_request_target`` run under the pull request's head commit, and a
    protected push or dispatch under the commit it runs from, which is also the commit it tests."""

    try:
        caller = ci_producer(producer["workflow_path"])
    except MbError as error:
        raise fail(f"{path}.workflow_path", "must be a managed Build/E2E producer caller") from error
    ref = g.WORKFLOW_REF.fullmatch(producer["workflow_ref"])
    check(ref is not None and ref["repository"] == identity["repository"]
          and ref["path"] == producer["workflow_path"] and ref["branch"] == identity["base_branch"],
          f"{path}.workflow_ref", "must name the protected repository, producer workflow and default branch")
    if identity["pr_number"]:
        check(producer["event"] == "pull_request_target", f"{path}.event", "PR producers require protected orchestration")
        check(producer["api_head_sha"] == identity["head_sha"], f"{path}.api_head_sha",
              "a pull-request producer run is recorded under the pull request head")
    else:
        check(producer["event"] != "pull_request_target", f"{path}.event", "PR event has no PR subject")
        check(producer["api_head_sha"] == identity["tested_sha"] == identity["controller_sha"],
              f"{path}.api_head_sha", "a protected non-PR producer runs from the commit it tests")
    if "upload_window" in producer:
        window = producer["upload_window"]
        check(window["started_at"] <= window["completed_at"], f"{path}.upload_window", "upload window is reversed")
    return caller


_DESCRIPTOR = Obj({**_binding(), "producer": _PRODUCER, "artifact": _ARTIFACT})


def validate_descriptor(value: Any, path: str = "$") -> dict[str, Any]:
    """A canonical immutable artifact selection, retaining its digest and exact producer attempt."""

    _DESCRIPTOR(value, path)
    producer, artifact = value["producer"], value["artifact"]
    caller = _producer_binding(producer, value["identity"], f"{path}.producer")
    name = g.parse_ci_artifact_name(artifact["name"])
    check(name is not None, f"{path}.artifact.name", "must be a CI artifact name")
    if name.kind in {"runtime", "results"} or (name.kind, name.unit_id) == ("tested", "packaged"):
        check(caller == "packaged", f"{path}.artifact.name", "only the packaged caller produces this artifact")
    else:
        # A pull request never rebuilds inside its packaged run; only a standalone run can.
        check(caller == "build" or not value["identity"]["pr_number"], f"{path}.artifact.name",
              "a pull request's Build artifacts come from its Build caller")
    check((name.run_id, name.run_attempt) == (producer["run_id"], producer["run_attempt"]),
          f"{path}.artifact.name", "does not bind the producer run and attempt")
    window = producer["upload_window"]
    created = g.parse_timestamp(artifact["created_at"])
    started, completed = g.parse_timestamp(window["started_at"]), g.parse_timestamp(window["completed_at"])
    tolerance = lim.CI_ARTIFACT_UPLOAD_SKEW_SECONDS
    check(-tolerance <= (created - started).total_seconds()
          and (created - completed).total_seconds() <= tolerance,
          f"{path}.artifact.created_at", "artifact was not created within its authenticated upload window")
    check(artifact["created_at"] < artifact["expires_at"], f"{path}.artifact.expires_at", "artifact lifetime is empty")
    if name.kind in {"tested", "reuse"}:
        check(artifact["size"] <= lim.MAX_CI_RECORD_BYTES, f"{path}.artifact.size", "sealed record exceeds record cap")
    return value


def _same_binding(first: dict[str, Any], second: dict[str, Any], path: str) -> None:
    for key in ("identity", "plan_sha256", "profile"):
        check(first[key] == second[key], f"{path}.{key}", "does not equal the complete admitted binding")


def _plan_binding(document: dict[str, Any], plan: dict[str, Any] | None, path: str) -> None:
    if plan is not None:
        validate_plan(plan)
        _same_binding(document, plan, path)


#: A planned output with the size and hash of its frozen bytes; ``lane_id`` is null for a file of
#: the target as a whole, exactly as in the plan.
_FILE = Obj({"path": _file_path, "size": Int(1, lim.MAX_CI_EXPORT_FILE_BYTES), "sha256": SHA256,
             "lane_id": Nullable(UNIT), "role": Str(choices=OUTPUT_ROLES)})
_ENVELOPE = Obj({
    **_header("mod-base.build.envelope"), **_binding(), "producer": _PRODUCER_IDENTITY,
    "scope": Str(choices=("target", "complete")), "target_id": Nullable(UNIT),
    "files": List(_FILE, min_items=1, max_items=lim.MAX_CI_EXPORT_FILES, unique_by=lambda item: item["path"]),
    "native_reports": List(_file_path, min_items=1, max_items=lim.MAX_CI_EXPORT_FILES, sorted_values=True),
})


def _role_bounds(profile: str) -> dict[str, int]:
    """The largest file of each output role in a Build export of ``profile``. Every role has a
    bound of its own, so no file is held only to the whole-export per-file ceiling."""

    return {"production": lim.MAX_CI_JAR_BYTES, "harness": lim.MAX_CI_JAR_BYTES, "sbom": lim.MAX_CI_SBOM_BYTES,
            "native-report": lim.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE[profile], "build-log": lim.MAX_CI_LOG_BYTES}


def validate_build_envelope(document: Any, *, plan: dict[str, Any] | None = None,
                            path: str = "$") -> dict[str, Any]:
    _ENVELOPE(document, path)
    _producer_binding(document["producer"], document["identity"], f"{path}.producer")
    _plan_binding(document, plan, path)
    check((document["target_id"] is not None) == (document["scope"] == "target"), f"{path}.target_id",
          "only a target partition names a target")
    files = document["files"]
    paths = [file["path"] for file in files]
    check(paths == sorted(paths), f"{path}.files", "file inventory must be sorted")
    check_output_paths(paths, f"{path}.files")
    check(sum(file["size"] for file in files) <= lim.MAX_CI_EXPORT_TREE_BYTES, f"{path}.files",
          "complete logical export exceeds the whole-tree budget")
    check(document["native_reports"] == [file["path"] for file in files if file["role"] == "native-report"],
          f"{path}.native_reports", "must name exactly the inventoried native reports")
    bounds = _role_bounds(document["profile"])
    for index, file in enumerate(files):
        check_output_scope(file, f"{path}.files[{index}]")
        check(file["size"] <= bounds[file["role"]], f"{path}.files[{index}].size",
              "native file exceeds its role budget")
    if plan is not None:
        targets = [target for target in plan["targets"] if document["scope"] == "complete"
                   or target["id"] == document["target_id"]]
        check(bool(targets), f"{path}.target_id", "target is absent from the protected plan")
        expected = sorted((output for target in targets for output in target["outputs"]), key=lambda item: item["path"])
        observed = [{key: file[key] for key in ("path", "lane_id", "role")} for file in files]
        check(observed == expected, f"{path}.files", "does not equal the exact planned output union")
    return document


def bind_build_envelope(envelope: dict[str, Any], *, descriptor: dict[str, Any],
                        plan: dict[str, Any]) -> dict[str, Any]:
    """Bind pre-upload export identity to an independently authenticated selection.

    The descriptor retains actual API upload times and immutable artifact metadata. Neither
    those future times nor artifact IDs/digests are self-reported inside the uploaded envelope.
    Structural binding alone does not authenticate API provenance or native compiler validity.
    """

    validate_descriptor(descriptor)
    validate_build_envelope(envelope, plan=plan)
    _same_binding(descriptor, envelope, "$.descriptor")
    expected = {key: value for key, value in descriptor["producer"].items() if key != "upload_window"}
    check(envelope["producer"] == expected, "$.envelope.producer", "export differs from selected producer identity")
    name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
    check((name.kind, name.unit_id) == (("build", None) if envelope["scope"] == "complete"
                                       else ("target", envelope["target_id"])),
          "$.descriptor.artifact.name", "selected artifact does not bind export scope/target")
    return envelope


_REQUEST = Obj({"run_id": RUN, "run_attempt": ATTEMPT, "nonce": SHA256,
                "workflow_path": Str(g.WORKFLOW_PATH, max_len=130),
                "workflow_ref": Str(g.WORKFLOW_REF, max_len=460)})
_SELECTION = Obj({**_header("mod-base.ci.selection"), **_binding(), "request": _REQUEST,
                  "build": validate_descriptor, "envelope_sha256": SHA256})


def validate_source_selection(document: Any, *, plan: dict[str, Any] | None = None,
                              path: str = "$") -> dict[str, Any]:
    _SELECTION(document, path)
    _plan_binding(document, plan, path)
    _same_binding(document["build"], document, f"{path}.build")
    name = g.parse_ci_artifact_name(document["build"]["artifact"]["name"])
    check(name.kind == "build", f"{path}.build", "runtime input must be a complete Build bundle")
    request = document["request"]
    ref = g.WORKFLOW_REF.fullmatch(request["workflow_ref"])
    check(ref is not None and ref["repository"] == document["identity"]["repository"]
          and ref["path"] == request["workflow_path"] and ref["branch"] == document["identity"]["base_branch"],
          f"{path}.request.workflow_ref", "request must name its protected workflow/default ref")
    return document


def bind_source_selection(document: dict[str, Any], *, plan: dict[str, Any], run_id: int, run_attempt: int,
                          workflow_path: str) -> dict[str, Any]:
    """Bind a selection record to the plan and to the run attempt that consumes it.

    Only the attempt of the packaged caller that requested a selection may consume it, so a rerun
    of failed jobs alone never inherits the Build an earlier attempt selected. A Build rebuilt
    inside a packaged run serves that run attempt only; every other Build is the complete bundle
    of a separate run of the Build caller. This is structural binding: the consumer authenticates
    the Build through the API again before it reads a byte of it.
    """

    validate_source_selection(document, plan=plan)
    check(ci_producer(workflow_path) == "packaged", "$.request.workflow_path",
          "only a run of the packaged caller selects and consumes a Build")
    request, producer = document["request"], document["build"]["producer"]
    check((request["run_id"], request["run_attempt"], request["workflow_path"]) == (run_id, run_attempt, workflow_path),
          "$.request", "selection was requested by another run, attempt or workflow; rerun all jobs")
    if ci_producer(producer["workflow_path"]) == "packaged":
        check((producer["run_id"], producer["run_attempt"], producer["workflow_path"])
              == (run_id, run_attempt, workflow_path), "$.build.producer",
              "a rebuilt Build serves only the run attempt that built it")
    else:
        check(producer["run_id"] != run_id, "$.build.producer", "a Build caller's bundle comes from a separate run")
    return document


def build_source_selection(*, plan: dict[str, Any], request: dict[str, Any], build: dict[str, Any],
                           envelope: dict[str, Any]) -> dict[str, Any]:
    """The ``mod-base.ci.selection`` record of the exact Build one packaged run attempt consumes.

    ``build`` is the authenticated descriptor of the complete bundle and ``envelope`` the envelope
    read from its verified bytes. ``request`` names the requesting run, its attempt and its managed
    caller on the default branch, with a nonce of its own. The record is bound the way its
    consumer binds it (:func:`bind_source_selection`) before it is returned.
    """

    _REQUEST(request, "$.request")
    bind_build_envelope(envelope, descriptor=build, plan=plan)
    kind = "mod-base.ci.selection"
    document = {"kind": kind, "schema_version": max(readable_schema_versions(kind)),
                "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                "profile": plan["profile"], "request": copy.deepcopy(request), "build": copy.deepcopy(build),
                "envelope_sha256": canonical_sha256(envelope)}
    return bind_source_selection(document, plan=plan, run_id=request["run_id"], run_attempt=request["run_attempt"],
                                 workflow_path=request["workflow_path"])


#: The paths the tested merge changes against its base, as ``ci scope`` read them from the two Git
#: trees: the base commit and its tree, the tested tree, how many paths differ and the SHA-256 of
#: their sorted canonical list (the list itself went to ``derive_scope`` only).
_SCOPE_CHANGES = Obj({"base_sha": SHA1, "base_tree": SHA1, "tested_tree": SHA1,
                      "paths": Int(0, 2 * lim.MAX_CI_SCOPE_TREE_ENTRIES), "paths_sha256": SHA256})
_SCOPE_ARTIFACT = Obj({"run_id": RUN, "run_attempt": ATTEMPT, "artifact_id": RUN,
                       "artifact_name": Str(max_len=lim.MAX_ARTIFACT_NAME_BYTES), "digest": Str(g.DIGEST, max_len=71)})
#: The full packaged evidence of the base commit a narrowed scope rests on: the tested record of a
#: push run of the base commit (``evidence: tested``), or the reuse reference of one and the
#: original packaged record it names (``reuse``); the SHA-256 of the canonical record ``ci scope``
#: read, and the plan hash that record was sealed for.
_SCOPE_BASELINE = Obj({"evidence": Str(choices=("tested", "reuse")), "record": _SCOPE_ARTIFACT,
                       "original": Nullable(_SCOPE_ARTIFACT), "record_sha256": SHA256, "plan_sha256": SHA256})
_SCOPE_LANE = Obj({"id": UNIT, "scenarios": List(Str(g.IDENT, max_len=200), min_items=1,
                                                 max_items=lim.MAX_CI_OBLIGATIONS_PER_LANE, unique=True)})
_SCOPE = Obj({**_header("mod-base.ci.scope"), **_binding(), "request": _REQUEST,
              "input": Str(choices=SCOPE_INPUTS), "changes": _SCOPE_CHANGES, "baseline": _SCOPE_BASELINE,
              "lanes": List(_SCOPE_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda item: item["id"])})


def _baseline_artifact(value: dict[str, Any], kind: str, unit: str | None, path: str) -> None:
    name = g.parse_ci_artifact_name(value["artifact_name"])
    check(name is not None and (name.kind, name.unit_id, name.run_id, name.run_attempt)
          == (kind, unit, value["run_id"], value["run_attempt"]), f"{path}.artifact_name",
          f"must name the {kind} artifact of its own run attempt")


def validate_scope_record(document: Any, *, plan: dict[str, Any] | None = None, path: str = "$") -> dict[str, Any]:
    """``mod-base.ci.scope`` v1: the narrowed scope one run attempt of a pull request's packaged
    caller runs (docs/BUILD-RUNTIME-SELECTION.md, D1 b).

    The record binds the plan of the pull request, which keeps every lane's full obligations, and
    the attempt that requested it (``request``, as in a selection record). It says what the scope
    rests on: the input format the protected config named, the changed paths read from the base
    and tested trees, and the base commit's full packaged evidence (``baseline``). ``lanes`` names
    every lane with the scenarios it runs, at least one. Only a pull request of this repository
    under an unchanged kit pin runs one. With a plan, the lanes are exactly the
    planned ones in plan order, each lane's scenarios are its own obligations in plan order, and at
    least one lane runs fewer than all of them: a scope that narrows nothing is no scope. Structure
    only: ``build_ci.scope`` authenticates what the record says before it writes one."""

    _SCOPE(document, path)
    _plan_binding(document, plan, path)
    identity, request = document["identity"], document["request"]
    check(identity["pr_number"] > 0, f"{path}.identity.pr_number", "only a pull request runs a narrowed scope")
    check(identity["source_repository"] == identity["repository"], f"{path}.identity.source_repository",
          "a pull request from another repository never runs a narrowed scope")
    check(plan is None or "candidate_kit" not in plan, f"{path}.plan_sha256",
          "a pull request that changes the kit pin never runs a narrowed scope")
    ref = g.WORKFLOW_REF.fullmatch(request["workflow_ref"])
    check(ref is not None and ref["repository"] == identity["repository"]
          and ref["path"] == request["workflow_path"] and ref["branch"] == identity["base_branch"],
          f"{path}.request.workflow_ref", "request must name its protected workflow/default ref")
    try:
        caller = ci_producer(request["workflow_path"])
    except MbError as error:
        raise fail(f"{path}.request.workflow_path", "must be a managed Build/E2E producer caller") from error
    check(caller == "packaged", f"{path}.request.workflow_path", "only a packaged run narrows its scope")
    changes = document["changes"]
    check((changes["base_sha"], changes["tested_tree"]) == (identity["base_sha"], identity["tested_tree"]),
          f"{path}.changes", "must compare the tested tree with the tree of the pull request's base")
    baseline = document["baseline"]
    if baseline["evidence"] == "tested":
        _baseline_artifact(baseline["record"], "tested", "packaged", f"{path}.baseline.record")
        check(baseline["original"] is None, f"{path}.baseline.original", "a direct record names no original")
    else:
        _baseline_artifact(baseline["record"], "reuse", None, f"{path}.baseline.record")
        check(baseline["original"] is not None, f"{path}.baseline.original", "a reuse reference names its original")
        _baseline_artifact(baseline["original"], "tested", "packaged", f"{path}.baseline.original")
        check(baseline["original"]["run_id"] != baseline["record"]["run_id"], f"{path}.baseline.original",
              "the original record comes from another run than its reference")
    check(len(canonical_json(document)) <= lim.MAX_CI_SCOPE_BYTES, path, "the scope record exceeds its byte cap")
    if plan is not None:
        check([lane["id"] for lane in document["lanes"]] == [lane["id"] for lane in plan["lanes"]],
              f"{path}.lanes", "must name every planned lane and no other, in plan order")
        narrowed = False
        for index, (lane, planned) in enumerate(zip(document["lanes"], plan["lanes"])):
            obligations, selected = planned["obligations"], set(lane["scenarios"])
            check(lane["scenarios"] == [name for name in obligations if name in selected],
                  f"{path}.lanes[{index}].scenarios", "must be obligations of the lane, in plan order")
            narrowed |= len(lane["scenarios"]) < len(obligations)
        check(narrowed, f"{path}.lanes", "narrows no lane: a run of every obligation has no scope record")
    return document


def bind_scope_record(document: dict[str, Any], *, plan: dict[str, Any], run_id: int, run_attempt: int,
                      workflow_path: str) -> dict[str, Any]:
    """Bind a scope record to the plan and to the run attempt that consumes it, exactly as a
    selection record is bound (:func:`bind_source_selection`): only the attempt of the packaged
    caller whose input job requested it may use it, so a rerun of failed jobs alone never inherits
    the scope of an earlier attempt."""

    validate_scope_record(document, plan=plan)
    check(ci_producer(workflow_path) == "packaged", "$.request.workflow_path",
          "only a run of the packaged caller narrows its scope")
    request = document["request"]
    check((request["run_id"], request["run_attempt"], request["workflow_path"]) == (run_id, run_attempt, workflow_path),
          "$.request", "the scope was requested by another run, attempt or workflow; rerun all jobs")
    return document


def scope_document(*, plan: dict[str, Any], request: dict[str, Any], input_format: str, changes: dict[str, Any],
                   baseline: dict[str, Any], lanes: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``mod-base.ci.scope`` document ``ci scope`` writes for one run attempt of the packaged
    caller, not yet validated: the writer binds it the way its consumers do
    (:func:`bind_scope_record`) once it has decided what a document over the byte cap means.
    ``request`` names the requesting run, its attempt and its managed caller on the default branch,
    with a nonce of its own; the other arguments are what the command authenticated and the hook
    selected."""

    _REQUEST(request, "$.request")
    kind = "mod-base.ci.scope"
    return {"kind": kind, "schema_version": max(readable_schema_versions(kind)),
            "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
            "profile": plan["profile"], "request": copy.deepcopy(request), "input": input_format,
            "changes": copy.deepcopy(changes), "baseline": copy.deepcopy(baseline), "lanes": copy.deepcopy(lanes)}


_NATIVE_RECEIPT = Obj({"unit_id": UNIT, "native_contract_sha256": SHA256, "report_sha256": SHA256})
_GATE = Obj({
    **_header("mod-base.ci.gate"), **_binding(), "producer": _PRODUCER_IDENTITY,
    "gate": Str(choices=("build", "packaged")),
    "mode": Str(choices=("full", "pull-request", "selected", "rebuilt")),
    "artifacts": List(validate_descriptor, min_items=1, max_items=lim.MAX_CI_ARTIFACTS_PER_GATE,
                      unique_by=lambda item: item["artifact"]["id"]),
    "owning_build": Nullable(validate_descriptor),
    "native_receipts": List(_NATIVE_RECEIPT, min_items=1, max_items=lim.MAX_CI_LANES,
                            unique_by=lambda item: item["unit_id"]),
}, {
    # Optional within schema 1: the SHA-256 of the canonical scope record (``mod-base.ci.scope``)
    # a pull request's packaged run narrowed its lanes to, as its results index holds it. Absent
    # means every lane ran every obligation.
    "scope_sha256": SHA256,
})


def validate_gate_receipt(document: Any, *, plan: dict[str, Any] | None = None,
                          path: str = "$") -> dict[str, Any]:
    _GATE(document, path)
    _plan_binding(document, plan, path)
    caller = _producer_binding(document["producer"], document["identity"], f"{path}.producer")
    mode, pull_request = document["mode"], bool(document["identity"]["pr_number"])
    check(mode in GATE_MODES[document["gate"]].get(caller, ()), f"{path}.mode",
          "no run of this caller ends in this gate in that mode")
    check(mode not in ("selected", "rebuilt") or not pull_request, f"{path}.mode",
          "a pull request never selects or rebuilds its Build inside the packaged run")
    check(mode != "pull-request" or pull_request, f"{path}.mode", "the pull-request mode needs a pull request")
    check("scope_sha256" not in document or (document["gate"], mode) == ("packaged", "pull-request"),
          f"{path}.scope_sha256", "only the packaged gate of a pull request runs a narrowed scope")
    check("scope_sha256" not in document
          or document["identity"]["source_repository"] == document["identity"]["repository"],
          f"{path}.scope_sha256", "a pull request from another repository never runs a narrowed scope")
    check("scope_sha256" not in document or plan is None or "candidate_kit" not in plan, f"{path}.scope_sha256",
          "a pull request that changes the kit pin never runs a narrowed scope")
    check((document["owning_build"] is not None) == (document["gate"] == "packaged"), f"{path}.owning_build",
          "packaged gate requires an exact owning Build; Build gate has none")
    if document["owning_build"] is not None:
        _same_binding(document["owning_build"], document, f"{path}.owning_build")
        owner = g.parse_ci_artifact_name(document["owning_build"]["artifact"]["name"])
        check(owner.kind == "build", f"{path}.owning_build", "must name a complete Build bundle")
        check(document["owning_build"]["artifact"]["id"] not in {item["artifact"]["id"] for item in document["artifacts"]},
              f"{path}.owning_build", "owning Build id collides with runtime evidence")
        producer, owning = document["producer"], document["owning_build"]["producer"]
        if mode == "rebuilt":
            check(all(owning[key] == producer[key] for key in producer), f"{path}.owning_build.producer",
                  "a rebuilt Build is sealed by the packaged run itself")
        else:
            check(owning["run_id"] != producer["run_id"] and ci_producer(owning["workflow_path"]) == "build",
                  f"{path}.owning_build.producer", "this mode consumes the Build of a separate Build run")
    names = []
    for index, descriptor in enumerate(document["artifacts"]):
        here = f"{path}.artifacts[{index}]"
        _same_binding(descriptor, document, here)
        check(all(descriptor["producer"][key] == document["producer"][key]
                  for key in document["producer"] if key != "upload_window"),
              f"{here}.producer", "mixed producer or attempt")
        names.append(g.parse_ci_artifact_name(descriptor["artifact"]["name"]))
    check(len({name.name for name in names}) == len(names), f"{path}.artifacts", "duplicate artifact name")
    if document["gate"] == "build":
        check(len(names) == 1 and names[0].kind == "build", f"{path}.artifacts", "Build gate requires one complete bundle")
    else:
        check(sum(name.kind == "results" for name in names) == 1
              and all(name.kind in {"results", "runtime"} for name in names), f"{path}.artifacts",
              "packaged gate requires runtime lane artifacts and one complete results aggregate")
        check(sorted(name.unit_id for name in names if name.kind == "runtime")
              == sorted(receipt["unit_id"] for receipt in document["native_receipts"]),
              f"{path}.artifacts", "runtime artifact and native receipt units differ")
        if plan is not None:
            expected_lanes = sorted(lane["id"] for lane in plan["lanes"])
            check(sorted(name.unit_id for name in names if name.kind == "runtime") == expected_lanes,
                  f"{path}.artifacts", "runtime artifacts do not cover every planned lane exactly")
    if plan is not None:
        units = plan["targets"] if document["gate"] == "build" else plan["lanes"]
        expected = [(unit["id"], unit["native_contract_sha256"]) for unit in units]
        observed = [(receipt["unit_id"], receipt["native_contract_sha256"]) for receipt in document["native_receipts"]]
        check(observed == expected, f"{path}.native_receipts", "native receipts do not match the entire ordered plan")
    return document


def gate_receipt(*, plan: dict[str, Any], producer: dict[str, Any], gate: str, mode: str,
                 artifacts: list[dict[str, Any]], owning_build: dict[str, Any] | None,
                 native_receipts: list[dict[str, Any]], scope_sha256: str | None = None) -> dict[str, Any]:
    """The receipt the gate job of ``producer`` (an attempt's identity, without a window) seals
    for ``plan``: a new ``mod-base.ci.gate`` document that is valid for the plan, independent of
    its arguments. ``scope_sha256`` is the digest of the narrowed scope a pull request's packaged
    run ran, and ``None`` for a run in full. Writing one proves nothing: the gate authenticates
    what it names first."""

    validate_plan(plan)
    document = {"kind": "mod-base.ci.gate", "schema_version": SCHEMA_VERSIONS["mod-base.ci.gate"],
                "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                "producer": producer, "gate": gate, "mode": mode, "artifacts": artifacts,
                "owning_build": owning_build, "native_receipts": native_receipts}
    if scope_sha256 is not None:
        document["scope_sha256"] = scope_sha256
    return validate_gate_receipt(copy.deepcopy(document), plan=plan)


#: One lane of a results index: its sealed artifact, the SHA-256 of the canonical runtime envelope
#: and of the canonical validation record inside it, and the SHA-256 of the lane's verification
#: report as that record inventories it.
_RESULT_LANE = Obj({"id": UNIT, "native_contract_sha256": SHA256, "descriptor": validate_descriptor,
                    "envelope_sha256": SHA256, "validation_sha256": SHA256, "report_sha256": SHA256})
_RESULTS = Obj({
    **_header("mod-base.ci.results"), **_binding(), "producer": _PRODUCER_IDENTITY,
    "owning_build": validate_descriptor, "build_envelope_sha256": SHA256,
    "lanes": List(_RESULT_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda item: item["id"]),
}, {
    # Optional within schema 1: the narrowed scope (``mod-base.ci.scope``) every lane of a pull
    # request's packaged run attempt received from its input job. Absent means a run in full.
    "scope": lambda value, path: validate_scope_record(value, path=path),
})


def validate_results_index(document: Any, *, plan: dict[str, Any] | None = None,
                           path: str = "$") -> dict[str, Any]:
    """The complete packaged results of one attempt as an index of its lanes, never a byte union.

    Every lane names the runtime artifact of the same attempt that holds its sealed results. The
    owning Build is the complete bundle every lane ran: the Build the packaged run itself rebuilt,
    or the one of a separate Build run. With a plan, the lanes are exactly the planned ones with
    their native contracts, in plan order. Hashes are recorded, not proven, by this structure."""

    _RESULTS(document, path)
    _plan_binding(document, plan, path)
    producer, owning = document["producer"], document["owning_build"]
    check(_producer_binding(producer, document["identity"], f"{path}.producer") == "packaged",
          f"{path}.producer.workflow_path", "only the packaged caller seals a results index")
    _same_binding(owning, document, f"{path}.owning_build")
    check(g.parse_ci_artifact_name(owning["artifact"]["name"]).kind == "build", f"{path}.owning_build",
          "must name a complete Build bundle")
    if ci_producer(owning["producer"]["workflow_path"]) == "packaged":
        check(all(owning["producer"][key] == producer[key] for key in producer), f"{path}.owning_build.producer",
              "a rebuilt Build is sealed by the packaged run itself")
    else:
        check(owning["producer"]["run_id"] != producer["run_id"], f"{path}.owning_build.producer",
              "a consumed Build comes from a separate Build run")
    artifact_ids = {owning["artifact"]["id"]}
    for index, lane in enumerate(document["lanes"]):
        here = f"{path}.lanes[{index}]"
        descriptor = lane["descriptor"]
        _same_binding(descriptor, document, f"{here}.descriptor")
        check(all(descriptor["producer"][key] == producer[key] for key in producer), f"{here}.descriptor.producer",
              "mixed producer or attempt")
        name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check((name.kind, name.unit_id) == ("runtime", lane["id"]), f"{here}.descriptor.artifact.name",
              "must name the runtime artifact of this lane")
        check(descriptor["artifact"]["id"] not in artifact_ids, f"{here}.descriptor.artifact.id",
              "repeats an artifact id")
        artifact_ids.add(descriptor["artifact"]["id"])
    if "scope" in document:
        scope = document["scope"]
        _same_binding(scope, document, f"{path}.scope")
        check((scope["request"]["run_id"], scope["request"]["run_attempt"], scope["request"]["workflow_path"])
              == (producer["run_id"], producer["run_attempt"], producer["workflow_path"]), f"{path}.scope.request",
              "the scope was requested by another run attempt than the one that indexed its lanes")
        if plan is not None:
            validate_scope_record(scope, plan=plan, path=f"{path}.scope")
    if plan is not None:
        expected = [(lane["id"], lane["native_contract_sha256"]) for lane in plan["lanes"]]
        check([(lane["id"], lane["native_contract_sha256"]) for lane in document["lanes"]] == expected,
              f"{path}.lanes", "does not list every planned lane and no other, in plan order")
    return document


_REUSE_SOURCE = Obj({**_binding(), "build_seal": validate_descriptor, "packaged_seal": validate_descriptor})
_REUSE = Obj({**_header("mod-base.ci.reuse"), **_binding(), "producer": _PRODUCER_IDENTITY, "source": _REUSE_SOURCE})
#: What a covered push and the pull request it reuses have in common: the repository and its
#: protected caller, the kit pin, the complete tested tree and everything a plan binds to the
#: protected policy and to the candidate bytes. The commits, their parents and the controller differ.
_REUSE_SEMANTICS = ("repository", "source_repository", "base_branch", "controller_workflow", "controller_ref",
                    "kit", "tested_tree", "policy_sha256", "inventory_blob", "inventory_sha256", "scenario_sha256",
                    "runtime_selection_sha256", "graph_version")


def original_plan(plan: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """``plan`` under another subject's ``identity``: the same profile, plan inputs, targets and
    lanes, bound by a hash of its own.

    Post-merge reuse plans the merged commit and asks whether the gates of its pull request were
    sealed for the same work. They were exactly when the plan hash their records carry is the hash
    of this plan under the pull request's identity: one comparison covers everything a plan holds."""

    validate_plan(plan)
    original = copy.deepcopy(plan)
    original["identity"] = copy.deepcopy(validate_identity(identity, "$.source.identity"))
    original["plan_sha256"] = plan_sha256(original)
    return validate_plan(original)


def validate_reuse_reference(document: Any, *, plan: dict[str, Any] | None = None,
                             path: str = "$") -> dict[str, Any]:
    """``mod-base.ci.reuse`` v1: a push to the default branch covered by both original gates of the
    merged pull request it came from.

    ``identity``, ``plan_sha256`` and ``producer`` are the covered merge and the reuse run that
    sealed the reference; ``source`` holds the original identity and plan hash and the descriptors
    of both original tested records, each with its run, attempt and artifact. Covered and original
    agree in every field of ``_REUSE_SEMANTICS`` and in the profile. A seal is always a direct
    tested record, never another reference, so references cannot chain. With the covered ``plan``
    the original plan hash must be the hash of that plan under the original identity
    (:func:`original_plan`). Structure only: whoever admits or reads a reference authenticates
    the runs, the merged pull request, the tree and every artifact (``mod_base.build_ci.reuse``)."""

    _REUSE(document, path)
    _plan_binding(document, plan, path)
    _producer_binding(document["producer"], document["identity"], f"{path}.producer")
    check(document["producer"]["event"] == "push", f"{path}.producer.event",
          "only a push to the default branch reuses the gates of its pull request")
    covered, source = document["identity"], document["source"]
    check(covered["pr_number"] == 0 and source["identity"]["pr_number"] > 0, path,
          "reuse covers a protected non-PR subject from an original PR")
    check(covered["head_branch"] == covered["base_branch"], f"{path}.identity.head_branch",
          "ordinary post-merge reuse covers only the protected default branch")
    for key in _REUSE_SEMANTICS:
        check(covered[key] == source["identity"][key], f"{path}.source.identity.{key}", "reuse semantics differ")
    check(document["profile"] == source["profile"], f"{path}.source.profile", "reuse profile differs")
    if plan is not None:
        check(source["plan_sha256"] == original_plan(plan, source["identity"])["plan_sha256"],
              f"{path}.source.plan_sha256", "the original gates were sealed for another plan than the covered one")
    # A final merge can be the exact synthetic commit originally tested. Distinct PR/current
    # bindings retain their provenance even then; SHA inequality is not a reuse requirement.
    for key, producer_kind in (("build_seal", "build"), ("packaged_seal", "packaged")):
        descriptor = source[key]
        _same_binding(descriptor, source, f"{path}.source.{key}")
        name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check(name.kind == "tested" and name.unit_id == producer_kind, f"{path}.source.{key}",
              "must reference an original direct tested seal, never another reuse reference")
    check(source["build_seal"]["artifact"]["id"] != source["packaged_seal"]["artifact"]["id"],
          f"{path}.source", "both original gates cannot share an artifact id")
    check(source["build_seal"]["producer"]["run_id"] != source["packaged_seal"]["producer"]["run_id"],
          f"{path}.source", "original PR Build and packaged seals require independent workflow runs")
    return document


def reuse_reference(*, plan: dict[str, Any], producer: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    """The reference the gate job of ``producer`` (a reuse run's identity, without an upload
    window) seals for the covered ``plan``: a new ``mod-base.ci.reuse`` document that is valid for
    the plan, independent of its arguments. ``source`` is the original binding with both seal
    descriptors, as the admission authenticated it (``reuse.admit_post_merge_reuse``). Writing one
    proves nothing and renews nothing: the original artifacts keep their own retention."""

    validate_plan(plan)
    document = {"kind": "mod-base.ci.reuse", "schema_version": SCHEMA_VERSIONS["mod-base.ci.reuse"],
                "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                "producer": producer, "source": source}
    return validate_reuse_reference(copy.deepcopy(document), plan=plan)


def _bind_record_producer(document: dict[str, Any], descriptor: dict[str, Any],
                          *, kind: str, unit: str | None, inputs: list[dict[str, Any]]) -> None:
    validate_descriptor(descriptor)
    _same_binding(descriptor, document, "$.descriptor")
    expected = {key: value for key, value in descriptor["producer"].items() if key != "upload_window"}
    check(document["producer"] == expected, "$.producer", "record differs from selected writer identity")
    name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
    check((name.kind, name.unit_id) == (kind, unit), "$.descriptor.artifact.name", "wrong selected record kind")
    for source in inputs:
        check(source["artifact"]["id"] != descriptor["artifact"]["id"], "$.descriptor.artifact.id",
              "record artifact collides with its source input")
        check(source["producer"]["upload_window"]["completed_at"]
              <= descriptor["producer"]["upload_window"]["started_at"], "$.source.upload_window",
              "source upload cannot postdate the selected record upload")


def bind_gate_receipt(document: dict[str, Any], *, descriptor: dict[str, Any],
                      plan: dict[str, Any]) -> dict[str, Any]:
    """Bind a pre-upload full gate receipt to its authenticated tested-record selection.

    Require source uploads precede the selected record upload. Actual gate execution, complete
    API graphs, native reports and all artifact metadata remain independent admission proofs.
    """

    validate_gate_receipt(document, plan=plan)
    inputs = list(document["artifacts"])
    if document["owning_build"] is not None:
        inputs.append(document["owning_build"])
    _bind_record_producer(document, descriptor, kind="tested", unit=document["gate"], inputs=inputs)
    return document


def results_index(*, plan: dict[str, Any], producer: dict[str, Any], owning_build: dict[str, Any],
                  build_envelope_sha256: str, lanes: list[dict[str, Any]],
                  scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """The results index the aggregating job of ``producer`` (an attempt's identity, without a
    window) seals for ``plan``: a new ``mod-base.ci.results`` document that is valid for the plan,
    independent of its arguments. ``lanes`` holds, in plan order, ``descriptor``,
    ``envelope_sha256``, ``validation_sha256`` and ``report_sha256`` of every planned lane; the
    lane's id and native contract are the plan's. ``scope`` is the narrowed scope the attempt's
    input job handed to its lanes, or ``None`` for a run in full. The job authenticates each of
    them first."""

    validate_plan(plan)
    check(type(lanes) is list and len(lanes) == len(plan["lanes"]), "$.lanes", "must hold every planned lane")
    document = {"kind": "mod-base.ci.results", "schema_version": SCHEMA_VERSIONS["mod-base.ci.results"],
                "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                "producer": producer, "owning_build": owning_build, "build_envelope_sha256": build_envelope_sha256,
                "lanes": [{"id": planned["id"], "native_contract_sha256": planned["native_contract_sha256"], **lane}
                          for planned, lane in zip(plan["lanes"], lanes)]}
    if scope is not None:
        document["scope"] = scope
    return validate_results_index(copy.deepcopy(document), plan=plan)


def bind_results_index(document: dict[str, Any], *, descriptor: dict[str, Any],
                       plan: dict[str, Any]) -> dict[str, Any]:
    """Bind a results index to the results artifact it was read from: the same attempt sealed
    both, and every lane and the owning Build were uploaded before the index was. The run, the
    graph and every artifact it names remain to be authenticated."""

    validate_results_index(document, plan=plan)
    _bind_record_producer(document, descriptor, kind="results", unit=None,
                          inputs=[*(lane["descriptor"] for lane in document["lanes"]), document["owning_build"]])
    return document


def bind_reuse_reference(document: dict[str, Any], *, descriptor: dict[str, Any],
                         plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind a reuse reference to the reuse artifact it was read from: the same run sealed both,
    and both original seals were uploaded before the reference was.

    Upload times are what the records say. That the original runs had finished before the reuse
    gate started, their graphs, the merged tree, the policy and every artifact's availability are
    proven by whoever reads the reference (``mod_base.build_ci.reuse``)."""

    validate_reuse_reference(document, plan=plan)
    _bind_record_producer(document, descriptor, kind="reuse", unit=None,
                          inputs=[document["source"][key] for key in ("build_seal", "packaged_seal")])
    return document


#: The modes of a generation: the commit's own tested runs, or the merged pull request's.
GENERATION_MODES = ("full", "reuse")
_GENERATION_LANE = Obj({"id": UNIT, "native_contract_sha256": SHA256, "descriptor": validate_descriptor})
_GENERATION = Obj({
    **_header("mod-base.ci.generation"), **_binding(), "mode": Str(choices=GENERATION_MODES),
    "references": Nullable(Obj({"build": validate_descriptor, "packaged": validate_descriptor})),
    "source": _REUSE_SOURCE, "build": validate_descriptor, "results": validate_descriptor,
    "lanes": List(_GENERATION_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda item: item["id"]),
})


def _same_attempt(descriptor: dict[str, Any], producer: dict[str, Any], path: str) -> None:
    check(all(descriptor["producer"][key] == producer[key] for key in producer if key != "upload_window"),
          f"{path}.producer", "is not of the attempt that sealed the packaged gate")


def validate_generation(document: Any, *, plan: dict[str, Any] | None = None, path: str = "$") -> dict[str, Any]:
    """``mod-base.ci.generation`` v1: the tested generation that covers one default-branch commit,
    as ``ci select-generation`` selected and authenticated it for a consumer.

    ``identity``, ``plan_sha256`` and ``profile`` are the covered commit. ``source`` is the
    generation that was tested: the commit itself in a ``full`` generation, the merged pull request
    in a ``reuse`` generation, whose two ``references`` are the reuse references of the commit's
    Build and packaged runs. ``source`` names both tested records; ``build`` is the complete Build
    bundle the packaged gate consumed, ``results`` its results index and ``lanes`` each lane's
    runtime artifact, all of the source binding and of the packaged gate's attempt. With the
    covered ``plan`` the source plan hash must be the plan's own (full) or that of
    :func:`original_plan` (reuse), and the lanes are the planned ones in plan order. Structure
    only: ``generation.select_generation`` authenticates every run, record and artifact first."""

    _GENERATION(document, path)
    _plan_binding(document, plan, path)
    covered, source, references = document["identity"], document["source"], document["references"]
    check(covered["pr_number"] == 0 and covered["head_branch"] == covered["base_branch"], f"{path}.identity",
          "a generation covers a commit of the protected default branch")
    if document["mode"] == "full":
        check(references is None, f"{path}.references", "a full generation has no reuse reference")
        _same_binding(source, document, f"{path}.source")
    else:
        check(references is not None, f"{path}.references", "a reuse generation names both reuse references")
        check(source["identity"]["pr_number"] > 0, f"{path}.source.identity",
              "a reuse generation was tested for a pull request")
        for key in _REUSE_SEMANTICS:
            check(covered[key] == source["identity"][key], f"{path}.source.identity.{key}", "reuse semantics differ")
        check(document["profile"] == source["profile"], f"{path}.source.profile", "reuse profile differs")
        for key in ("build", "packaged"):
            descriptor = references[key]
            _same_binding(descriptor, document, f"{path}.references.{key}")
            name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
            check(name.kind == "reuse" and ci_producer(descriptor["producer"]["workflow_path"]) == key
                  and descriptor["producer"]["event"] == "push", f"{path}.references.{key}",
                  "must be the reuse reference of this caller's push run")
        check(references["build"]["producer"]["run_id"] != references["packaged"]["producer"]["run_id"],
              f"{path}.references", "the two reuse references come from the two callers' own runs")
        if plan is not None:
            check(source["plan_sha256"] == original_plan(plan, source["identity"])["plan_sha256"],
                  f"{path}.source.plan_sha256", "the original gates were sealed for another plan than the covered one")
    for key, gate in (("build_seal", "build"), ("packaged_seal", "packaged")):
        descriptor = source[key]
        _same_binding(descriptor, source, f"{path}.source.{key}")
        name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check((name.kind, name.unit_id) == ("tested", gate), f"{path}.source.{key}",
              "must be the tested record of this gate")
    packaged = source["packaged_seal"]["producer"]
    ids = [source[key]["artifact"]["id"] for key in ("build_seal", "packaged_seal")]
    for key, kind in (("build", "build"), ("results", "results")):
        descriptor = document[key]
        _same_binding(descriptor, source, f"{path}.{key}")
        check(g.parse_ci_artifact_name(descriptor["artifact"]["name"]).kind == kind, f"{path}.{key}",
              f"must name the {kind} artifact")
        ids.append(descriptor["artifact"]["id"])
    _same_attempt(document["results"], packaged, f"{path}.results")
    for index, lane in enumerate(document["lanes"]):
        here = f"{path}.lanes[{index}].descriptor"
        descriptor = lane["descriptor"]
        _same_binding(descriptor, source, here)
        _same_attempt(descriptor, packaged, here)
        name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check((name.kind, name.unit_id) == ("runtime", lane["id"]), f"{here}.artifact.name",
              "must name the runtime artifact of this lane")
        ids.append(descriptor["artifact"]["id"])
    if references is not None:
        ids += [references[key]["artifact"]["id"] for key in ("build", "packaged")]
    check(len(set(ids)) == len(ids), f"{path}.artifacts", "repeats an artifact id")
    if plan is not None:
        check(document["mode"] == "reuse" or source["plan_sha256"] == plan["plan_sha256"],
              f"{path}.source.plan_sha256", "a full generation was tested for the covered plan")
        expected = [(lane["id"], lane["native_contract_sha256"]) for lane in plan["lanes"]]
        check([(lane["id"], lane["native_contract_sha256"]) for lane in document["lanes"]] == expected,
              f"{path}.lanes", "does not list every planned lane and no other, in plan order")
    return document


def generation_record(*, plan: dict[str, Any], mode: str, references: dict[str, Any] | None,
                      source: dict[str, Any], build: dict[str, Any], results: dict[str, Any],
                      lanes: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``mod-base.ci.generation`` document for the covered ``plan``, valid for that plan and
    independent of its arguments. ``lanes`` holds each planned lane's runtime descriptor, in plan
    order; the lane's id and native contract are the plan's. Writing one proves nothing: the
    caller authenticated everything it names first."""

    validate_plan(plan)
    check(type(lanes) is list and len(lanes) == len(plan["lanes"]), "$.lanes", "must hold every planned lane")
    document = {"kind": "mod-base.ci.generation", "schema_version": SCHEMA_VERSIONS["mod-base.ci.generation"],
                "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                "mode": mode, "references": references, "source": source, "build": build, "results": results,
                "lanes": [{"id": planned["id"], "native_contract_sha256": planned["native_contract_sha256"],
                           "descriptor": descriptor} for planned, descriptor in zip(plan["lanes"], lanes)]}
    return validate_generation(copy.deepcopy(document), plan=plan)
