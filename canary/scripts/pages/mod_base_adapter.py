"""The mod-base adapter of the canary: synthetic demonstration evidence, not a product.

The canary is a stand-in mod whose "packaged E2E" is a generator (``mod_base_fixtures.py``). It
mirrors the shapes a real mod has, on a tiny synthetic inventory:

* ``release/release-matrix.json`` lists the release targets (one key ``mc<minecraft>`` each, every
  loader of a target is one artifact node) and names the anchor target;
* ``e2e/scenario-contract.json`` lists the scenarios, their roles, ordered steps, captures and
  comparisons;
* the packaged output is ``runs/<artifact_node>/<scenario>/report.json`` with one report per role
  (``steps[]`` with ``id``, ``status``, ``message`` and ``screenshot``, the recorded pixel ``metrics``
  per captured step and ``comparisons`` per comparison id) and the screenshots beside it under
  ``<role>/<step>.png``;
* the ``demo-pairs`` family's native bundle (``mod-base-canary.pairs`` v1) holds ready-made pair
  records and their WebP images.

Every contract and matrix read goes through ``ctx.read_blob`` (inert Git objects of the subject
commit); nothing here writes outside ``ctx.tmpdir`` or the directory a hook is given.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

ADAPTER_API = 1

MATRIX = "release/release-matrix.json"
CONTRACT = "e2e/scenario-contract.json"
NATIVE_KIND = "mod-base-canary.pairs"
MAX_DOCUMENT_BYTES = 1 << 20
MAX_REPORT_BYTES = 4 << 20
MAX_NATIVE_BYTES = 8 << 20
SHA1_LENGTH = 40


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _document(ctx, commit: str, path: str) -> tuple[dict, bytes]:
    data = ctx.read_blob(commit, path, MAX_DOCUMENT_BYTES)
    return json.loads(data), data


def _tree(ctx, commit: str) -> str:
    """The tree of ``commit`` from the local object store (a read-only ``git rev-parse``)."""

    environment = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(ctx.tmpdir), "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    completed = subprocess.run(["git", "-C", str(ctx.repo_root), "rev-parse", "--verify", f"{commit}^{{tree}}"],
                               check=True, capture_output=True, env=environment, timeout=60)
    tree = completed.stdout.decode("ascii").strip()
    if len(tree) != SHA1_LENGTH:
        raise ValueError(f"unexpected tree id for {commit}")
    return tree


def key_of(target_row: dict) -> str:
    return f"mc{target_row['minecraft']}"


def targets(ctx, branches):
    if branches is not None:
        raise ValueError("the canary uses default-branch mode")
    commit = ctx.implementation_sha
    matrix, matrix_bytes = _document(ctx, commit, MATRIX)
    _, contract_bytes = _document(ctx, commit, CONTRACT)
    subject = {"branch": ctx.config.canonical_branch, "commit": commit, "tree": _tree(ctx, commit)}
    prefix = ctx.config.labels["release_prefix"]
    return [{"key": key_of(row), "label": f"{prefix} {row['minecraft']}", "subject": subject,
             "matrix_sha256": _sha256(matrix_bytes), "contract_sha256": _sha256(contract_bytes)}
            for row in matrix["targets"]]


def _row(matrix: dict, key: str) -> dict:
    rows = [row for row in matrix["targets"] if key_of(row) == key]
    if len(rows) != 1:
        raise ValueError(f"the matrix declares {len(rows)} targets for {key}")
    return rows[0]


def expectation(ctx, target, tested_run, extensions):
    if extensions:
        raise ValueError("the canary declares no extensions")
    commit = target["subject"]["commit"]
    matrix, matrix_bytes = _document(ctx, commit, MATRIX)
    contract, contract_bytes = _document(ctx, commit, CONTRACT)
    if _sha256(matrix_bytes) != target["matrix_sha256"] or _sha256(contract_bytes) != target["contract_sha256"]:
        raise ValueError("the matrix or contract changed since targets")
    row = _row(matrix, target["key"])
    lanes, captures, comparisons = [], [], []
    for loader in row["loaders"]:
        node = f"{loader}-{row['minecraft']}"
        for scenario in contract["scenarios"]:
            lane_id = f"{node}/{scenario['id']}"
            lanes.append({"lane_id": lane_id, "artifact_node": node, "minecraft": row["minecraft"], "loader": loader,
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
                    comparisons.append(record)
    anchor = None
    if row["minecraft"] == matrix["anchor_minecraft"]:
        anchor = {"artifact_nodes": sorted(f"{loader}-{row['minecraft']}" for loader in row["loaders"])}
    return {
        "kind": "mod-base.evidence.expectation", "schema_version": 1, "repository": matrix["repository"],
        "key": target["key"], "label": target["label"], "subject": target["subject"],
        "matrix_sha256": target["matrix_sha256"], "contract_sha256": target["contract_sha256"],
        "contract_path": CONTRACT, "profile": contract["profile"], "scope": {"kind": "complete"},
        "image_policy": ctx.config.image_policy(),
        "scenarios": [{"id": scenario["id"], "title": scenario["title"]} for scenario in contract["scenarios"]],
        "lanes": lanes, "captures": captures, "comparisons": comparisons, "anchor": anchor,
    }


def run_path(lane: dict) -> str:
    return f"runs/{lane['artifact_node']}/{lane['scenario']}"


def collect(ctx, runtime_root, target, expectation):
    tree = ctx.runtime_tree(runtime_root)
    files, lanes, reports = [], [], {}
    for lane in expectation["lanes"]:
        base = run_path(lane)
        report = tree.read_json(f"{base}/report.json", max_bytes=MAX_REPORT_BYTES)
        if (report.get("status") != "pass" or report.get("artifact_node") != lane["artifact_node"]
                or report.get("minecraft") != lane["minecraft"] or report.get("loader") != lane["loader"]
                or report.get("scenario") != lane["scenario"]
                or report.get("contract_sha256") != expectation["contract_sha256"]):
            raise ValueError(f"the report of {lane['lane_id']} is not a passing run of this lane and contract")
        files.append(f"{base}/report.json")
        reports[lane["lane_id"]] = report
        lanes.append({"lane_id": lane["lane_id"], "java": lane["java"], "profile": expectation["profile"],
                      "status": "pass", "elapsed_s": report["elapsed_s"],
                      "jars": {"production_sha256": report["production_sha256"]}})
    frames, steps = [], {}
    for capture in expectation["captures"]:
        lane = next(item for item in expectation["lanes"] if item["lane_id"] == capture["lane_id"])
        role = reports[capture["lane_id"]]["roles"][capture["role"]]
        step = next(item for item in role["steps"] if item["id"] == capture["step"])
        if step["status"] != "pass" or step["screenshot"] != f"{capture['role']}/{capture['step']}.png":
            raise ValueError(f"the report step of {capture['frame_id']} did not pass or names another screenshot")
        source = f"{run_path(lane)}/{step['screenshot']}"
        if not tree.exists(source):
            raise ValueError(f"missing screenshot {source}")
        files.append(source)
        steps[capture["frame_id"]] = capture["step"]
        frames.append({"frame_id": capture["frame_id"], "source_path": source, "runtime_evidence": step["message"],
                       "reported_pixel": role["metrics"][capture["step"]]})
    comparisons = []
    for comparison in expectation["comparisons"]:
        role = reports[comparison["lane_id"]]["roles"][comparison["role"]]
        identifier = comparison["comparison_id"].rsplit("/", 1)[1]
        comparisons.append({"comparison_id": comparison["comparison_id"], "reported": role["comparisons"][identifier]})
    return {"runtime_files": sorted(set(files)), "lanes": lanes, "frames": frames, "comparisons": comparisons}


def anchor_selection(ctx, expectation):
    return expectation["anchor"]


def family_validate(ctx, family, key, bundle_dir, expected_coverage_sha, output_dir):
    """Project the canary's native pairs bundle onto ``mod-base.family.paired``.

    A bundle written against another scenario contract is ``superseded`` (contract drift). One whose
    native coverage is not its envelope's is ``unavailable``. A generation of an earlier commit (the
    envelope's ``coverage_sha``) is carried forward to ``expected_coverage_sha`` when the family's
    ``carry_forward`` allows it and the release matrix is byte-identical at both commits (the
    canary's impact decision: its pairs depend only on the matrix and the contract); otherwise it
    is ``unavailable``. The kit proves the ancestry itself (R5) and re-verifies everything else
    (R4) before it is published."""

    root = Path(bundle_dir)
    envelope = json.loads((root / "envelope.json").read_bytes())
    native_bytes = (root / "manifest.json").read_bytes()
    if len(native_bytes) > MAX_NATIVE_BYTES:
        raise ValueError("the native manifest is too large")
    native = json.loads(native_bytes)
    if native.get("kind") != NATIVE_KIND or native.get("schema_version") != 1:
        raise ValueError(f"the native bundle is not a {NATIVE_KIND} v1 bundle")
    if native["family"] != family or native["key"] != key:
        raise ValueError("the native bundle belongs to another family leg")
    _, contract_bytes = _document(ctx, expected_coverage_sha, CONTRACT)
    if native["contract_sha256"] != _sha256(contract_bytes):
        return {"status": "superseded", "reason": "the pairs were produced for another scenario contract"}
    coverage = envelope["coverage_sha"]
    if native["coverage_sha"] != coverage:
        return {"status": "unavailable", "reason": "the pairs do not cover the commit their envelope names"}
    carried_from = None
    if coverage != expected_coverage_sha:
        if not ctx.config.family(family)["carry_forward"]:
            return {"status": "unavailable", "reason": "the pairs cover an earlier commit (no carry-forward)"}
        if _document(ctx, coverage, MATRIX)[1] != _document(ctx, expected_coverage_sha, MATRIX)[1]:
            return {"status": "unavailable", "reason": "the release matrix changed since the pairs were produced"}
        carried_from = coverage
    producer = native["producer"]
    claim = envelope["producer"]
    if any(producer.get(field) != value for field, value in claim.items()):
        raise ValueError("the native producer record is not the envelope's producer run")
    output = Path(output_dir)
    images = output / "images"
    images.mkdir()
    for lane in native["lanes"]:
        for pair in lane["pairs"]:
            for side in ("reference", "candidate"):
                relative = pair[side]["image"]["path"]
                source = root.joinpath(*relative.split("/"))
                data = source.read_bytes()
                if _sha256(data) != pair[side]["image"]["sha256"] or len(data) != pair[side]["image"]["size"]:
                    raise ValueError(f"{relative} differs from its pair record")
                if not (images / source.name).exists():
                    shutil.copyfile(source, images / source.name)
    projection = {
        "kind": "mod-base.family.paired", "schema_version": 1, "family": family, "key": key,
        "coverage_sha": expected_coverage_sha, "subject": envelope["subject"], "status": "available",
        "provenance": {"producer": producer, "links": [{"label": "Synthetic producer", "run_id": producer["run_id"]}]},
        "contracts": {"scenario-contract": native["contract_sha256"]},
        "image_policy": ctx.config.family(family)["image_policy"],
        "lanes": native["lanes"], "not_applicable": native["not_applicable"],
    }
    (output / "paired.json").write_text(json.dumps(projection, sort_keys=True, indent=1), encoding="utf-8")
    if carried_from is None:
        return {"status": "available", "reason": "synthetic pairs of the expected commit",
                "projection_path": "paired.json"}
    return {"status": "available", "reason": "synthetic pairs carried forward from an unchanged matrix and contract",
            "projection_path": "paired.json", "carried_from": carried_from}
