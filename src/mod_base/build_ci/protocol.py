"""Closed Build adapter API and pure, bounded v1 plan validation.

Structural validity is not admission: protected Git/API authentication, native witnesses,
sealed bytes and a complete graph are independently required before a gate can pass.
"""

from __future__ import annotations

from typing import Any

from mod_base import KIT_REPOSITORY, readable_schema_versions
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_sha256
from mod_base.model.validators import Const, Int, List, Obj, Str, check, fail

BUILD_ADAPTER_API = 1
BUILD_GRAPH_VERSION = 1
PACKAGED_GRAPH_VERSION = 1
BUILD_HOOKS = frozenset({"derive_plan", "verify_target", "verify_build", "derive_runtime", "verify_runtime"})
OUTPUT_ROLES = ("production", "harness", "sbom", "native-report", "build-log")

SHA1 = Str(g.SHA1, max_len=40)
SHA256 = Str(g.SHA256, max_len=64)
REPO = Str(g.REPOSITORY, max_len=201)
BRANCH = Str(g.BRANCH, max_len=200)
ID = Str(g.CI_UNIT_ID, max_len=80)
WORKFLOW = Str(g.WORKFLOW_PATH, max_len=130)
RUN = Int(1, lim.MAX_RUN_ID)
ATTEMPT = Int(1, lim.MAX_RUN_ATTEMPT)


def repo_path(value: Any, path: str) -> str:
    if not g.is_repo_path(value):
        raise fail(path, "must be a canonical repository-relative path")
    return value


_IDENTITY = Obj({
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
    "policy_sha256": SHA256, "inventory_blob": SHA1, "inventory_sha256": SHA256,
    "scenario_sha256": SHA256, "runtime_selection_sha256": SHA256,
    "graph_version": Const(BUILD_GRAPH_VERSION),
})


def validate_identity(value: Any, path: str = "$") -> dict[str, Any]:
    """Require distinct protected-controller and exact-tested-subject identities."""

    _IDENTITY(value, path)
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
    return value


_OUTPUT = Obj({"path": repo_path, "lane_id": ID,
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


def _header(kind: str) -> dict[str, Any]:
    return {"kind": Const(kind), "schema_version": Int(min(readable_schema_versions(kind)),
                                                     max(readable_schema_versions(kind)))}


_PLAN = Obj({
    **_header("mod-base.build.plan"), "build_adapter_api": Const(BUILD_ADAPTER_API),
    "identity": validate_identity, "profile": Str(choices=("quick-skin", "block-pops")),
    "targets": List(_TARGET, min_items=1, max_items=lim.MAX_CI_TARGETS, unique_by=lambda item: item["id"]),
    "lanes": List(_LANE, min_items=1, max_items=lim.MAX_CI_LANES, unique_by=lambda item: item["id"]),
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
        check(g.is_repo_path(name) and name.casefold() != g.CI_ENVELOPE_NAME.casefold(), path,
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


def validate_plan(document: Any, *, path: str = "$") -> dict[str, Any]:
    _PLAN(document, path)
    check(document["plan_sha256"] == plan_sha256(document), f"{path}.plan_sha256", "does not bind this plan")
    check_output_paths([output["path"] for target in document["targets"] for output in target["outputs"]],
                       f"{path}.targets")
    targets = {item["id"] for item in document["targets"]}
    lanes = {item["id"]: item["target_id"] for item in document["lanes"]}
    check(set(lanes.values()) == targets, f"{path}.lanes", "must cover every target exactly through declared lanes")
    paths: set[str] = set()
    for target in document["targets"]:
        roles: dict[str, set[str]] = {}
        for output in target["outputs"]:
            check(lanes.get(output["lane_id"]) == target["id"], f"{path}.targets", "output names another target's lane")
            check(output["path"] not in paths, f"{path}.targets", "output path is shared across targets")
            paths.add(output["path"])
            lane_roles = roles.setdefault(output["lane_id"], set())
            check(output["role"] in {"native-report", "build-log"} or output["role"] not in lane_roles,
                  f"{path}.targets", "lane repeats a production, harness or SBOM output role")
            lane_roles.add(output["role"])
        for lane, owner in lanes.items():
            if owner == target["id"]:
                check({"production", "harness", "sbom", "native-report"} <= roles.get(lane, set()),
                      f"{path}.targets", "every lane requires separate production, harness, SBOM and native reports")
    return document
