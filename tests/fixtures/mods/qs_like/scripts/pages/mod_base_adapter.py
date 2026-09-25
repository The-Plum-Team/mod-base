"""Synthetic Quick Skin-like mod-base adapter (test fixture, ``default-branch`` mode).

It mimics Quick Skin's shapes on a tiny synthetic contract: ``mc<version>`` keys whose subject is
the protected head, one lane per (loader, scenario) of each matrix target, the packaged
``profiles/<node>--<version>--<scenario>/result.json`` format with embedded role reports
(``steps[].name/message/screenshot`` and ``pixel_validation``), delegated reuse through the
``quick-skin.runtime_source`` extension, ``selected`` evidence through
``quick-skin.feature_selection`` and a ``compose`` hook that completes selected evidence from an
authenticated ``mb-baseline`` archive. Everything is read from inert Git objects of the subject
commit (``ctx.read_blob``) or from the runtime tree the kit hands over.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from mod_base.io.bounded_zip import LIMITS_BY_KIND, extract
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import compact_identity_sha256

ADAPTER_API = 1

MATRIX = "release/release-matrix.json"
CONTRACT = "e2e/scenario-contract.json"
RUNTIME_SOURCE = "quick-skin.runtime_source"
FEATURE_SELECTION = "quick-skin.feature_selection"
PROFILE = "pr"
MAX_DOCUMENT = 1 << 20
MAX_RESULT = 4 << 20


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document(ctx, commit: str, path: str) -> tuple[dict, bytes]:
    data = ctx.read_blob(commit, path, MAX_DOCUMENT)
    return json.loads(data), data


def _tree(ctx, commit: str) -> str:
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(ctx.tmpdir), "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    completed = subprocess.run(["git", "-C", str(ctx.repo_root), "rev-parse", "--verify", f"{commit}^{{tree}}"],
                               check=True, capture_output=True, env=environment, timeout=60)
    return completed.stdout.decode("ascii").strip()


def targets(ctx, branches):
    if branches is not None:
        raise ValueError("the qs-like fixture uses default-branch mode")
    commit = ctx.implementation_sha
    matrix, matrix_bytes = _document(ctx, commit, MATRIX)
    _, contract_bytes = _document(ctx, commit, CONTRACT)
    subject = {"branch": ctx.config.canonical_branch, "commit": commit, "tree": _tree(ctx, commit)}
    prefix = ctx.config.labels["release_prefix"]
    return [{"key": f"mc{target['minecraft']}", "label": f"{prefix} {target['minecraft']}", "subject": subject,
             "matrix_sha256": _sha256(matrix_bytes), "contract_sha256": _sha256(contract_bytes)}
            for target in matrix["targets"]]


def _scenarios(contract: dict, selection: dict | None) -> list[dict]:
    wanted = contract["profiles"][PROFILE]
    if selection is not None:
        wanted = [scenario for scenario in wanted if scenario in selection["scenarios"]]
    by_id = {scenario["id"]: scenario for scenario in contract["scenarios"]}
    return [by_id[scenario] for scenario in wanted]


def expectation(ctx, target, tested_run, extensions):
    commit = target["subject"]["commit"]
    matrix, matrix_bytes = _document(ctx, commit, MATRIX)
    contract, contract_bytes = _document(ctx, commit, CONTRACT)
    if _sha256(matrix_bytes) != target["matrix_sha256"] or _sha256(contract_bytes) != target["contract_sha256"]:
        raise ValueError("the matrix or contract changed since targets")
    version = target["key"][2:]
    row = next(item for item in matrix["targets"] if item["minecraft"] == version)
    selection = extensions.get(FEATURE_SELECTION)
    scenarios = _scenarios(contract, selection)
    lanes, captures, comparisons = [], [], []
    for loader in row["loaders"]:
        node = f"{loader}-{version}"
        for scenario in scenarios:
            lane_id = f"{node}/{scenario['id']}"
            lanes.append({"lane_id": lane_id, "artifact_node": node, "minecraft": version, "loader": loader,
                          "java": row["java"], "scenario": scenario["id"],
                          "roles": [role["role"] for role in scenario["roles"]]})
            for role in scenario["roles"]:
                order = 0
                for step in role["steps"]:
                    if "capture" not in step:
                        continue
                    capture = step["capture"]
                    captures.append({"frame_id": f"{lane_id}/{role['role']}/{step['id']}",
                                     "capture_id": f"{scenario['id']}.{role['role']}.{step['id']}",
                                     "capture_order": order, "lane_id": lane_id, "role": role["role"],
                                     "step": step["id"], "title": capture["title"],
                                     "expectation": capture["expectation"], "review_tier": capture["review_tier"]})
                    order += 1
                for comparison in role["comparisons"]:
                    record = {"comparison_id": f"{lane_id}/{role['role']}/{comparison['id']}", "lane_id": lane_id,
                              "role": role["role"],
                              "first_frame_id": f"{lane_id}/{role['role']}/{comparison['first']}",
                              "second_frame_id": f"{lane_id}/{role['role']}/{comparison['second']}",
                              "minimum_changed_fraction": comparison["minimum_changed_fraction"]}
                    if "region" in comparison:
                        record["region"] = comparison["region"]
                    comparisons.append(record)
    scope = {"kind": "complete"}
    if selection is not None:
        scope = {"kind": "selected", "detail": selection,
                 "detail_sha256": _sha256(canonical_json(selection))}
    anchor = None
    if version == matrix["unit_test_version"]:
        anchor = {"artifact_nodes": sorted(f"{loader}-{version}" for loader in row["loaders"])}
    return {
        "kind": "mod-base.evidence.expectation", "schema_version": 1, "repository": matrix["repository"],
        "key": target["key"], "label": target["label"], "subject": target["subject"],
        "matrix_sha256": target["matrix_sha256"], "contract_sha256": target["contract_sha256"],
        "contract_path": CONTRACT, "profile": PROFILE, "scope": scope, "image_policy": ctx.config.image_policy(),
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
        if (result["status"] != "pass" or result["artifact_node"] != lane["artifact_node"]
                or result["runtime_version"] != lane["minecraft"] or result["loader"] != lane["loader"]
                or result["scenario"] != lane["scenario"] or result["contract_sha256"] != expectation["contract_sha256"]
                or result["profile"] != profile):
            raise ValueError(f"packaged result identity/status mismatch for {lane['lane_id']}")
        files.append(f"{profile}/result.json")
        results[lane["lane_id"]] = result
        lanes.append({"lane_id": lane["lane_id"], "java": lane["java"], "profile": expectation["profile"],
                      "status": "pass", "elapsed_s": result["elapsed_s"],
                      "jars": {"production_sha256": result["jar_sha256"]}})
    frames, by_frame = [], {}
    for capture in expectation["captures"]:
        lane = next(item for item in expectation["lanes"] if item["lane_id"] == capture["lane_id"])
        report = results[capture["lane_id"]]["reports"][capture["role"]]
        step = next(item for item in report["steps"] if item["name"] == capture["step"])
        if step["status"] != "pass" or step["screenshot"] != f"{capture['step']}.png":
            raise ValueError(f"report step mismatch for {capture['frame_id']}")
        source = f"{profile_path(lane)}/{capture['role']}/screenshots/{step['screenshot']}"
        if not tree.exists(source):
            raise ValueError(f"missing screenshot {source}")
        files.append(source)
        by_frame[capture["frame_id"]] = capture
        frames.append({"frame_id": capture["frame_id"], "source_path": source, "runtime_evidence": step["message"],
                       "reported_pixel": report["pixel_validation"]["screenshots"][capture["step"]]})
    comparisons = []
    for comparison in expectation["comparisons"]:
        first, second = by_frame[comparison["first_frame_id"]], by_frame[comparison["second_frame_id"]]
        report = results[comparison["lane_id"]]["reports"][comparison["role"]]
        comparisons.append({"comparison_id": comparison["comparison_id"],
                            "reported": report["pixel_validation"]["comparisons"][f"{first['step']}->{second['step']}"]})
    return {"runtime_files": sorted(set(files)), "lanes": lanes, "frames": frames, "comparisons": comparisons}


def authenticate_extensions(ctx, manifest, extensions):
    reuse_verified = False
    if RUNTIME_SOURCE in extensions:
        reference = extensions[RUNTIME_SOURCE]
        if ctx.api is None:
            raise RuntimeError("the runtime_source reference needs the read-only API")
        run = ctx.api.get_json(f"/repos/{ctx.api.repository}/actions/runs/{reference['run_id']}")
        tested = manifest["provenance"]["tested"]
        if (run.get("head_sha") != reference["tested_sha"] or run.get("conclusion") != "success"
                or tested["run_id"] != reference["run_id"] or tested["commit"] != reference["tested_sha"]):
            raise ValueError("the runtime_source reference is not the tested run")
        reuse_verified = True
    if FEATURE_SELECTION in extensions:
        if manifest["scope"].get("detail_sha256") != _sha256(canonical_json(extensions[FEATURE_SELECTION])):
            raise ValueError("the feature selection is not the manifest's selected scope")
    return {"verified": sorted(extensions), "reuse_verified": reuse_verified}


def _read(root: Path, name: str) -> bytes:
    return (root / name).read_bytes()


def compose(ctx, key, selected_compact_dir, output_dir):
    selected_root, output = Path(selected_compact_dir), Path(output_dir)
    manifest = json.loads(_read(selected_root, "manifest.json"))
    selected_expectation = json.loads(_read(selected_root, "expectation.json"))
    draft = json.loads(_read(selected_root, "selection.json"))
    extensions = json.loads(_read(selected_root, "extensions.json")) if manifest["extensions"] else {}
    baseline_id = extensions[FEATURE_SELECTION]["baseline_artifact_id"]
    record = ctx.api.get_json(f"/repos/{ctx.api.repository}/actions/artifacts/{baseline_id}")
    archive = ctx.api.download(f"/repos/{ctx.api.repository}/actions/artifacts/{baseline_id}/zip",
                               max_bytes=record["size_in_bytes"])
    baseline_root = Path(ctx.tmpdir) / "baseline"
    extract(archive, baseline_root, LIMITS_BY_KIND["baseline"])
    baseline_raw = _read(baseline_root, "manifest.json")
    baseline = json.loads(baseline_raw)
    baseline_selection = json.loads(_read(baseline_root, "selection.json"))
    composed_extensions = {name: value for name, value in extensions.items() if name != FEATURE_SELECTION}
    target = {name: selected_expectation[name]
              for name in ("key", "label", "subject", "matrix_sha256", "contract_sha256")}
    complete = expectation(ctx, target, None, composed_extensions)
    selected_lanes = {lane["lane_id"] for lane in manifest["lanes"]}
    sources = {"selected": (manifest, selected_root, draft), "baseline": (baseline, baseline_root, baseline_selection)}

    def epoch_of(lane_id: str) -> str:
        return "selected" if lane_id in selected_lanes else "baseline"

    def indexed(epoch: str, field: str, id_field: str) -> dict:
        return {item[id_field]: item for item in sources[epoch][0][field]}

    lanes = [indexed(epoch_of(lane["lane_id"]), "lanes", "lane_id")[lane["lane_id"]] for lane in complete["lanes"]]
    frames, images = [], {}
    for capture in complete["captures"]:
        epoch = epoch_of(capture["lane_id"])
        bundle, root, selection = sources[epoch]
        frame = dict(indexed(epoch, "frames", "frame_id")[capture["frame_id"]])
        lane = indexed(epoch, "lanes", "lane_id")[capture["lane_id"]]
        frame["epoch"] = epoch
        frame["tested"] = {**selection["source"]["tested_run"], "jar_sha256": lane["jars"]["production_sha256"]}
        images[frame["derivative"]["path"]] = _read(root, frame["derivative"]["path"])
        frames.append(frame)
    comparisons = [indexed(epoch_of(item["lane_id"]), "comparisons", "comparison_id")[item["comparison_id"]]
                   for item in complete["comparisons"]]
    expectation_bytes = canonical_json(complete)
    selection_bytes = _read(selected_root, "selection.json")
    files = {"expectation.json": expectation_bytes, "selection.json": selection_bytes, **images}
    extensions_record = None
    if composed_extensions:
        extensions_bytes = canonical_json(composed_extensions)
        files["extensions.json"] = extensions_bytes
        extensions_record = {"path": "extensions.json", "sha256": _sha256(extensions_bytes),
                             "size": len(extensions_bytes), "names": sorted(composed_extensions)}
    composed = {
        **manifest,
        "scope": {"kind": "composed", "components": {
            "baseline": {"artifact_id": record["id"], "name": record["name"], "digest": record["digest"],
                         "manifest_sha256": _sha256(baseline_raw)},
            "selected_manifest_sha256": compact_identity_sha256(manifest)}},
        "expectation": {"path": "expectation.json", "sha256": _sha256(expectation_bytes),
                        "size": len(expectation_bytes)},
        "extensions": extensions_record,
        "selection": {"path": "selection.json", "sha256": _sha256(selection_bytes), "size": len(selection_bytes)},
        "lanes": lanes, "frames": frames, "comparisons": comparisons,
        "files": sorted(({"path": path, "sha256": _sha256(data), "size": len(data)} for path, data in files.items()),
                        key=lambda item: item["path"]),
    }
    files["manifest.json"] = canonical_json(composed)
    for path, data in files.items():
        (output / path).parent.mkdir(parents=True, exist_ok=True)
        (output / path).write_bytes(data)
    return {"baseline_artifact": {"id": record["id"], "name": record["name"], "digest": record["digest"]}}


def anchor_selection(ctx, expectation):
    return expectation["anchor"]
