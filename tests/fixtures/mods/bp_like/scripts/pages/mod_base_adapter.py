"""Synthetic Block Pops-like mod-base adapter (test fixture, ``enrolled-branches`` mode).

It mimics Block Pops' shapes on a tiny synthetic contract: a branch is enrolled when its own
``release/release-matrix.json`` (read from inert Git objects through ``ctx.read_blob``) names that
branch as a canonical release branch; the key is the 24-hex ``branch_token``; the expectation's
profile is the projection of the handoff run's event (``schedule`` -> ``scheduled-anchors``,
anything else ``pr-anchors``) and its scope detail the ``aggregate_scope``; the packaged output is
``profiles/<node>--<minecraft>--<scenario>/result.json`` with role reports whose
``steps[].id/capture_id/message/screenshot`` and ``pixel_validation`` mirror Block Pops'
``report.json``; the exact source job graph comes from ``expected_source_jobs``. It defines no
``compose`` (Block Pops never produces selected evidence), so selected evidence fails closed.
"""

from __future__ import annotations

import hashlib
import json

from mod_base.model.canonical import canonical_json

ADAPTER_API = 1

MATRIX = "release/release-matrix.json"
CONTRACT = "e2e/scenario-contract.json"
AGGREGATE_SCOPE = "block-pops.aggregate_scope"
MAX_DOCUMENT = 1 << 20
MAX_RESULT = 4 << 20


def branch_token(branch: str) -> str:
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document(ctx, commit: str, path: str) -> tuple[dict, bytes]:
    data = ctx.read_blob(commit, path, MAX_DOCUMENT)
    return json.loads(data), data


def targets(ctx, branches):
    if branches is None:
        raise ValueError("the bp-like fixture uses enrolled-branches mode")
    enrolled = []
    for head in branches:
        try:
            matrix, matrix_bytes = _document(ctx, head["commit"], MATRIX)
        except Exception:  # a branch without a matrix is simply not enrolled
            continue
        branch = matrix.get("branch", {})
        if (branch.get("name") != head["name"] or branch.get("canonical") != ctx.config.canonical_branch
                or branch.get("role") != "release"):
            continue
        _, contract_bytes = _document(ctx, head["commit"], CONTRACT)
        enrolled.append({"key": branch_token(head["name"]), "label": head["name"],
                         "subject": {"branch": head["name"], "commit": head["commit"], "tree": head["tree"]},
                         "matrix_sha256": _sha256(matrix_bytes), "contract_sha256": _sha256(contract_bytes)})
    return enrolled


def projection(tested_run) -> str:
    return "scheduled-anchors" if tested_run is not None and tested_run["event"] == "schedule" else "pr-anchors"


def aggregate_scope(matrix: dict, contract: dict, profile: str) -> dict:
    nodes = [runtime["artifact_node"] for runtime in matrix["runtimes"]]
    return {"kind": matrix["migration"]["mode"], "selected_nodes": nodes, "target_nodes": nodes,
            "migration_mode": matrix["migration"]["mode"], "partial": False, "projection": profile,
            "scenarios": contract["profiles"]["release"]}


def expectation(ctx, target, tested_run, extensions):
    commit = target["subject"]["commit"]
    matrix, matrix_bytes = _document(ctx, commit, MATRIX)
    contract, contract_bytes = _document(ctx, commit, CONTRACT)
    if _sha256(matrix_bytes) != target["matrix_sha256"] or _sha256(contract_bytes) != target["contract_sha256"]:
        raise ValueError("the matrix or contract changed since targets")
    profile = projection(tested_run)
    by_id = {scenario["id"]: scenario for scenario in contract["scenarios"]}
    scenarios = [by_id[scenario] for scenario in contract["profiles"]["release"]]
    lanes, captures, comparisons = [], [], []
    for runtime in matrix["runtimes"]:
        for scenario in scenarios:
            lane_id = f"{runtime['artifact_node']}/{scenario['id']}"
            lanes.append({"lane_id": lane_id, "artifact_node": runtime["artifact_node"],
                          "minecraft": runtime["minecraft"], "loader": runtime["loader"], "java": runtime["java"],
                          "scenario": scenario["id"], "roles": [role["role"] for role in scenario["roles"]]})
            for role in scenario["roles"]:
                order = 0
                for step in role["steps"]:
                    if "capture" in step:
                        capture = step["capture"]
                        captures.append({"frame_id": f"{lane_id}/{role['role']}/{step['id']}",
                                         "capture_id": capture["capture_id"], "capture_order": order,
                                         "lane_id": lane_id, "role": role["role"], "step": step["id"],
                                         "title": capture["title"], "expectation": capture["expectation"],
                                         "review_tier": capture["review_tier"]})
                        order += 1
                for comparison in role["comparisons"]:
                    record = {"comparison_id": f"{lane_id}/{role['role']}/{comparison['first_step']}+"
                                               f"{comparison['second_step']}",
                              "lane_id": lane_id, "role": role["role"],
                              "first_frame_id": f"{lane_id}/{role['role']}/{comparison['first_step']}",
                              "second_frame_id": f"{lane_id}/{role['role']}/{comparison['second_step']}",
                              "minimum_changed_fraction": comparison["minimum_changed_fraction"]}
                    if "region" in comparison:
                        record["region"] = comparison["region"]
                    comparisons.append(record)
    detail = aggregate_scope(matrix, contract, profile)
    reference = matrix["visual_reference"]
    anchor = None
    if target["subject"]["branch"] == reference["release_branch"]:
        anchor = {"artifact_nodes": [reference["artifact_node"]]}
    return {
        "kind": "mod-base.evidence.expectation", "schema_version": 1, "repository": matrix["repository"],
        "key": target["key"], "label": target["label"], "subject": target["subject"],
        "matrix_sha256": target["matrix_sha256"], "contract_sha256": target["contract_sha256"],
        "contract_path": CONTRACT, "profile": profile,
        "scope": {"kind": "complete", "detail": detail, "detail_sha256": _sha256(canonical_json(detail))},
        "image_policy": ctx.config.image_policy(),
        "scenarios": [{"id": scenario["id"], "title": scenario["title"]} for scenario in scenarios],
        "lanes": lanes, "captures": captures, "comparisons": comparisons, "anchor": anchor,
    }


def profile_path(lane: dict) -> str:
    return f"profiles/{lane['artifact_node']}--{lane['minecraft']}--{lane['scenario']}"


def collect(ctx, runtime_root, target, expectation):
    tree = ctx.runtime_tree(runtime_root)
    files, lanes, results = [], [], {}
    for lane in expectation["lanes"]:
        profile = profile_path(lane)
        result = tree.read_json(f"{profile}/result.json", max_bytes=MAX_RESULT)
        if (result["status"] != "pass" or result["error"] is not None
                or result["artifact_node"] != lane["artifact_node"] or result["minecraft"] != lane["minecraft"]
                or result["loader"] != lane["loader"] or result["scenario"] != lane["scenario"]
                or result["contract_sha256"] != expectation["contract_sha256"] or result["profile"] != profile):
            raise ValueError(f"packaged result identity/status mismatch for {lane['lane_id']}")
        files.append(f"{profile}/result.json")
        results[lane["lane_id"]] = result
        lanes.append({"lane_id": lane["lane_id"], "java": lane["java"], "profile": expectation["profile"],
                      "status": "pass", "jars": {"production_sha256": result["production_jar_sha256"],
                                                 "harness_sha256": result["harness_jar_sha256"]}})
    frames, steps_by_frame = [], {}
    for capture in expectation["captures"]:
        lane = next(item for item in expectation["lanes"] if item["lane_id"] == capture["lane_id"])
        report = results[capture["lane_id"]]["reports"][capture["role"]]
        step = next(item for item in report["steps"] if item["id"] == capture["step"])
        if (step["status"] != "pass" or step["capture_id"] != capture["capture_id"]
                or step["screenshot"] != f"{capture['capture_id']}.png"):
            raise ValueError(f"report step mismatch for {capture['frame_id']}")
        source = f"{profile_path(lane)}/{capture['role']}/screenshots/{step['screenshot']}"
        files.append(source)
        steps_by_frame[capture["frame_id"]] = step["id"]
        frames.append({"frame_id": capture["frame_id"], "source_path": source, "runtime_evidence": step["message"],
                       "reported_pixel": report["pixel_validation"]["screenshots"][step["id"]]})
    comparisons = []
    for comparison in expectation["comparisons"]:
        report = results[comparison["lane_id"]]["reports"][comparison["role"]]
        pair = f"{steps_by_frame[comparison['first_frame_id']]}->{steps_by_frame[comparison['second_frame_id']]}"
        comparisons.append({"comparison_id": comparison["comparison_id"],
                            "reported": report["pixel_validation"]["comparisons"][pair]})
    return {"runtime_files": sorted(set(files)), "lanes": lanes, "frames": frames, "comparisons": comparisons}


def expected_source_jobs(ctx, expectation, tested_run):
    jobs = [{"name": "Resolve exact source", "conclusion": "success"}]
    jobs += [{"name": f"Packaged E2E / {lane['artifact_node']} / {lane['scenario']}", "conclusion": "success"}
             for lane in expectation["lanes"]]
    jobs.append({"name": "Curate current-head public evidence (advisory)", "conclusion": "success"})
    return jobs


def authenticate_extensions(ctx, manifest, extensions):
    if AGGREGATE_SCOPE in extensions:
        commit = manifest["subject"]["commit"]
        matrix, _ = _document(ctx, commit, MATRIX)
        contract, _ = _document(ctx, commit, CONTRACT)
        detail = extensions[AGGREGATE_SCOPE]
        if detail != aggregate_scope(matrix, contract, detail.get("projection")):
            raise ValueError("the aggregate scope does not match the subject's matrix")
        if manifest["scope"].get("detail_sha256") != _sha256(canonical_json(detail)):
            raise ValueError("the aggregate scope is not the manifest's scope")
    return {"verified": sorted(extensions), "reuse_verified": False}


def anchor_selection(ctx, expectation):
    return expectation["anchor"]
