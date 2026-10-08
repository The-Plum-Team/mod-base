"""Closed Build adapter API and pure, bounded v1 identity and plan validation.

Structural validity is not admission: protected Git/API authentication, native witnesses,
sealed bytes and a complete graph are independently required before a gate can pass.
"""

from __future__ import annotations

from typing import Any

from mod_base import KIT_REPOSITORY, readable_schema_versions
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_sha256
from mod_base.model.validators import Const, Int, List, Nullable, Obj, Str, check, fail

BUILD_ADAPTER_API = 1
BUILD_GRAPH_VERSION = 1
PACKAGED_GRAPH_VERSION = 1
#: The native profiles a protected Build config may name.
PROFILES = ("quick-skin", "block-pops")
#: The two producers of gate evidence, and the managed caller workflow a mod runs each one from.
PRODUCERS = ("build", "packaged")
CALLER_WORKFLOWS = {"build": ".github/workflows/mod-base-build.yml",
                    "packaged": ".github/workflows/mod-base-packaged-e2e.yml"}
OUTPUT_ROLES = ("production", "harness", "sbom", "native-report", "build-log")
#: The two JARs every lane has exactly once. Neither can stand for a target as a whole.
LANE_OUTPUT_ROLES = ("production", "harness")
#: Roles one lane, or one target as a whole, holds at most once.
SINGLE_OUTPUT_ROLES = (*LANE_OUTPUT_ROLES, "sbom")

SHA1 = Str(g.SHA1, max_len=40)
SHA256 = Str(g.SHA256, max_len=64)
REPO = Str(g.REPOSITORY, max_len=201)
BRANCH = Str(g.BRANCH, max_len=200)
ID = Str(g.CI_UNIT_ID, max_len=80)
WORKFLOW = Str(g.WORKFLOW_PATH, max_len=130)


def repo_path(value: Any, path: str) -> str:
    if not g.is_repo_path(value):
        raise fail(path, "must be a canonical repository-relative path")
    return value


def export_path(value: Any, path: str) -> str:
    if not g.is_export_path(value):
        raise fail(path, "must be a canonical export path")
    return value


#: What ``ci subject`` authenticates: the tested commit, its protected controller and the kit.
_SUBJECT_FIELDS = {
    "repository": REPO,
    "source_repository": REPO,
    "pr_number": Int(0, lim.MAX_RUN_ID),
    "head_sha": SHA1, "head_branch": BRANCH,
    "base_sha": SHA1, "base_branch": BRANCH,
    "controller_sha": SHA1, "controller_workflow": WORKFLOW,
    "controller_ref": Str(g.WORKFLOW_REF, max_len=460),
    "kit": Obj({"repository": Const(KIT_REPOSITORY), "sha": SHA1,
                "version": Str(g.VERSION, max_len=20), "tree_digest": Str(g.DIGEST, max_len=71)}),
    "tested_sha": SHA1, "tested_tree": SHA1,
    "tested_parents": List(SHA1, max_items=2, unique=True),
    "graph_version": Const(BUILD_GRAPH_VERSION),
}
#: What planning binds to the protected policy and to the candidate bytes a plan is derived from.
_PLAN_FIELDS = {
    "policy_sha256": SHA256, "inventory_blob": SHA1, "inventory_sha256": SHA256,
    "scenario_sha256": SHA256, "runtime_selection_sha256": SHA256,
}
_SUBJECT = Obj(_SUBJECT_FIELDS)
_IDENTITY = Obj({**_SUBJECT_FIELDS, **_PLAN_FIELDS})


def _subject_rules(value: dict[str, Any], path: str) -> None:
    ref = g.WORKFLOW_REF.fullmatch(value["controller_ref"])
    check(ref is not None and ref["repository"] == value["repository"]
          and ref["path"] == value["controller_workflow"] and ref["branch"] == value["base_branch"],
          f"{path}.controller_ref", "must name the protected repository, workflow and base branch")
    if value["pr_number"]:
        check(value["tested_parents"] == [value["base_sha"], value["head_sha"]],
              f"{path}.tested_parents", "must equal the ordered base/head parents")
        check(value["tested_sha"] not in value["tested_parents"], f"{path}.tested_sha",
              "must name the synthetic merge, not either parent")
    else:
        check(value["source_repository"] == value["repository"] and value["tested_sha"] == value["head_sha"],
              path, "non-PR subjects must name the exact protected repository and commit")


def validate_subject(value: Any, path: str = "$") -> dict[str, Any]:
    """An identity before planning: every field but the hashes a plan binds."""

    _SUBJECT(value, path)
    _subject_rules(value, path)
    return value


def validate_identity(value: Any, path: str = "$") -> dict[str, Any]:
    """Require distinct protected-controller and exact-tested-subject identities."""

    _IDENTITY(value, path)
    _subject_rules(value, path)
    return value


def subject_of(identity: dict[str, Any]) -> dict[str, Any]:
    """The subject part of a complete identity (what :func:`validate_subject` accepts)."""

    return {key: identity[key] for key in _SUBJECT_FIELDS}


#: ``lane_id`` names the lane an output belongs to, or is null for one that belongs to its target
#: as a whole: a staged manifest, a report, a log or the SBOM of the whole target.
_OUTPUT = Obj({"path": export_path, "lane_id": Nullable(ID),
               "role": Str(choices=OUTPUT_ROLES)})
_TARGET = Obj({
    "id": ID, "java": Int(8, 99), "native_contract_sha256": SHA256,
    "outputs": List(_OUTPUT, min_items=1, max_items=lim.MAX_CI_OUTPUTS_PER_TARGET,
                    unique_by=lambda item: item["path"]),
})
_LANE = Obj({
    "id": ID, "target_id": ID, "native_contract_sha256": SHA256,
    "obligations": List(Str(g.IDENT, max_len=200), min_items=1,
                        max_items=lim.MAX_CI_OBLIGATIONS_PER_LANE, unique=True),
})
_UNIT_FIELDS = {
    "targets": List(_TARGET, min_items=1, max_items=lim.MAX_CI_TARGETS, unique_by=lambda item: item["id"]),
    "lanes": List(_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda item: item["id"]),
}
_UNITS = Obj(_UNIT_FIELDS)


def _header(kind: str) -> dict[str, Any]:
    return {"kind": Const(kind), "schema_version": Int(min(readable_schema_versions(kind)),
                                                     max(readable_schema_versions(kind)))}


#: One extra candidate file the plan was derived from, beside the inventory and the scenario
#: contract its identity binds: the name the protected config stages it under and the SHA-256 of
#: the bytes ``derive_plan`` was given.
_PLAN_INPUT = Obj({"name": ID, "sha256": SHA256})
_PLAN = Obj({
    **_header("mod-base.build.plan"), "build_adapter_api": Const(BUILD_ADAPTER_API),
    "identity": validate_identity, "profile": Str(choices=PROFILES),
    "plan_inputs": List(_PLAN_INPUT, max_items=lim.MAX_CI_PLAN_INPUTS, unique_by=lambda item: item["name"]),
    **_UNIT_FIELDS,
    "plan_sha256": SHA256,
})


def plan_sha256(document: dict[str, Any]) -> str:
    """Hash the complete plan except its own hash; identities are included."""

    return canonical_sha256({key: value for key, value in document.items() if key != "plan_sha256"})


def check_output_paths(paths: list[str], path: str) -> None:
    """The complete logical export has one unambiguous inventory, across all partitions."""

    check(len(paths) <= lim.MAX_CI_EXPORT_FILES, path, "logical export exceeds the whole-tree file count")
    folded: set[str] = set()
    components: dict[str, str] = {}
    for name in paths:
        check((g.is_repo_path(name) or g.is_export_path(name))
              and name.casefold() != g.CI_ENVELOPE_NAME.casefold(), path,
              "unsafe output path or reserved outer envelope name")
        check(name.casefold() not in folded, path, "output inventory has a case-insensitive alias")
        folded.add(name.casefold())
        parts = name.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            previous = components.setdefault(prefix.casefold(), prefix)
            check(previous == prefix, path, "path components have a case-insensitive alias")
    for name in paths:
        parts = name.split("/")
        check(all("/".join(parts[:count]).casefold() not in folded for count in range(1, len(parts))),
              path, "a file is also an inventory directory")


def check_output_scope(output: dict[str, Any], path: str) -> None:
    """An output is one lane's or, with a null ``lane_id``, its target's as a whole. A production
    or harness JAR is always one lane's; a plan and an envelope apply the same rule."""

    check(output["lane_id"] is not None or output["role"] not in LANE_OUTPUT_ROLES, path,
          "a production or harness JAR belongs to one lane")


def _unit_rules(document: dict[str, Any], path: str) -> None:
    """What the mods really stage: every lane has exactly one production and one harness JAR;
    an SBOM is optional, at most one for a lane and one for a target as a whole; every target has
    a native report (its manifest, usually target-scoped); reports and logs may repeat. Paths are
    unique across the whole plan, so files a mod writes once per target carry the target in their
    path."""

    check_output_paths([output["path"] for target in document["targets"] for output in target["outputs"]],
                       f"{path}.targets")
    targets = {item["id"] for item in document["targets"]}
    lanes = {item["id"]: item["target_id"] for item in document["lanes"]}
    check(set(lanes.values()) == targets, f"{path}.lanes", "must cover every target exactly through declared lanes")
    for index, target in enumerate(document["targets"]):
        here = f"{path}.targets[{index}].outputs"
        held: set[tuple[str | None, str]] = set()
        for position, output in enumerate(target["outputs"]):
            lane, role = output["lane_id"], output["role"]
            check(lane is None or lanes.get(lane) == target["id"], f"{here}[{position}].lane_id",
                  "names another target's lane")
            check_output_scope(output, f"{here}[{position}]")
            check(role not in SINGLE_OUTPUT_ROLES or (lane, role) not in held, f"{here}[{position}]",
                  "repeats the production JAR, the harness JAR or the SBOM of one lane or of the target")
            held.add((lane, role))
        check(any(role == "native-report" for _, role in held), here, "every target requires a native report")
        for lane, owner in lanes.items():
            if owner == target["id"]:
                check({(lane, role) for role in LANE_OUTPUT_ROLES} <= held, here,
                      f"lane {lane} requires one production and one harness JAR")


def validate_plan_units(value: Any, path: str = "$") -> dict[str, Any]:
    """The targets and lanes of a plan, as a protected adapter derives them (no identity)."""

    _UNITS(value, path)
    _unit_rules(value, path)
    return value


def validate_plan(document: Any, *, path: str = "$") -> dict[str, Any]:
    _PLAN(document, path)
    check(document["plan_sha256"] == plan_sha256(document), f"{path}.plan_sha256", "does not bind this plan")
    names = [item["name"] for item in document["plan_inputs"]]
    check(names == sorted(names), f"{path}.plan_inputs", "must be sorted by name")
    _unit_rules(document, path)
    return document
