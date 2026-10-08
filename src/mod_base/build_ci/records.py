"""Strict CI records, with full identity/attempt/byte provenance and exact plan coverage.

These are pure structural checks. Metadata and self-reported native hashes never replace
independent Git/API, frozen-byte, domain-witness and complete-graph authentication.
"""

from __future__ import annotations

import copy
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import (OUTPUT_ROLES, PROFILES, check_output_paths, check_output_scope,
                                        validate_identity, validate_plan)
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_sha256
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
    check(window["started_at"] <= artifact["created_at"] <= window["completed_at"],
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


_REUSE_SOURCE = Obj({**_binding(), "build_seal": validate_descriptor, "packaged_seal": validate_descriptor})
_REUSE = Obj({**_header("mod-base.ci.reuse"), **_binding(), "producer": _PRODUCER_IDENTITY, "source": _REUSE_SOURCE})


def validate_reuse_reference(document: Any, *, plan: dict[str, Any] | None = None,
                             path: str = "$") -> dict[str, Any]:
    """Direct original PR seals only; full API/tree/artifact reuse admission is separate K6 work."""

    _REUSE(document, path)
    _plan_binding(document, plan, path)
    _producer_binding(document["producer"], document["identity"], f"{path}.producer")
    covered, source = document["identity"], document["source"]
    check(covered["pr_number"] == 0 and source["identity"]["pr_number"] > 0, path,
          "reuse covers a protected non-PR subject from an original PR")
    check(covered["head_branch"] == covered["base_branch"], f"{path}.identity.head_branch",
          "ordinary post-merge reuse covers only the protected default branch")
    for key in ("repository", "source_repository", "base_branch", "controller_workflow", "controller_ref",
                "kit", "tested_tree", "policy_sha256", "inventory_blob",
                "inventory_sha256", "scenario_sha256", "runtime_selection_sha256", "graph_version"):
        check(covered[key] == source["identity"][key], f"{path}.source.identity.{key}", "reuse semantics differ")
    check(document["profile"] == source["profile"], f"{path}.source.profile", "reuse profile differs")
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


def bind_reuse_reference(document: dict[str, Any], *, descriptor: dict[str, Any],
                         plan: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind a direct reuse record and source chronology to its actual selected upload.

    This does not prove when the protected verifier executed: actual source completion before
    verifier start, original API graphs, tree/policy equality and artifact availability must be
    independently authenticated in K6. No self-reported future timestamp supplies that proof.
    """

    validate_reuse_reference(document, plan=plan)
    _bind_record_producer(document, descriptor, kind="reuse", unit=None,
                          inputs=[document["source"][key] for key in ("build_seal", "packaged_seal")])
    return document
