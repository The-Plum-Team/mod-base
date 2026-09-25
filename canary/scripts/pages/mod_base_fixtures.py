"""The canary's generator: synthetic demonstration evidence, not a product.

``synthesize`` is the fixtures hook ``conformance`` calls and the canary producer workflow runs in
place of a packaged Minecraft run: it writes the packaged-output tree the canary adapter's
``collect`` reads (``runs/<artifact_node>/<scenario>/report.json`` plus ``<role>/<step>.png``)
with deterministic generated screenshots and the pixel metrics and comparisons the kit recomputes.

``family_bundle`` writes the ``demo-pairs`` family's native ``mod-base-canary.pairs`` v1 bundle: per
artifact node of the key one lane whose pairs join a generated reference and candidate image of
every capture. :data:`FAMILY_OUTCOMES` lists the outcomes it can produce for ``conformance``:
``available`` (the subject's own clean pairs), ``superseded`` (pairs written against another
scenario contract) and ``unavailable`` (pairs covering another commit).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import SizePolicy, inspect_png, inspect_webp
from mod_base.imaging.webp import derive_webp
from mod_base.model.documents import thumbnail_size

CONTRACT = "e2e/scenario-contract.json"
NATIVE_KIND = "mod-base-canary.pairs"
FAMILY_OUTCOMES = ("available", "superseded", "unavailable")
MAX_CONTRACT_BYTES = 1 << 20


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def synthesize(ctx, target, expectation, out_root, image_factory):
    contract = json.loads(ctx.read_blob(target["subject"]["commit"], CONTRACT, MAX_CONTRACT_BYTES))
    scenarios = {scenario["id"]: scenario for scenario in contract["scenarios"]}
    width, height = expectation["image_policy"]["source_size"]
    root = Path(out_root)
    for index, lane in enumerate(expectation["lanes"]):
        base = root / "runs" / lane["artifact_node"] / lane["scenario"]
        roles = {}
        for role in scenarios[lane["scenario"]]["roles"]:
            screenshots = base / role["role"]
            screenshots.mkdir(parents=True, exist_ok=True)
            steps, metrics, seed = [], {}, 10 * index
            for step in role["steps"]:
                screenshot = None
                if "capture" in step:
                    screenshot = f"{role['role']}/{step['id']}.png"
                    path = base / screenshot
                    path.write_bytes(image_factory(width, height, seed))
                    metrics[step["id"]] = ctx.image_metrics(path, ("exact", width, height))
                    seed += 1
                steps.append({"id": step["id"], "status": "pass", "screenshot": screenshot,
                              "message": f"PASS {step['id']}: synthetic checkpoint generated for "
                                         f"{lane['artifact_node']} (not a product)"})
            comparisons = {}
            for comparison in role["comparisons"]:
                comparisons[comparison["id"]] = compare(
                    screenshots / f"{comparison['first']}.png", screenshots / f"{comparison['second']}.png",
                    minimum_changed_fraction=comparison["minimum_changed_fraction"], region=comparison.get("region"))
            roles[role["role"]] = {"steps": steps, "metrics": metrics, "comparisons": comparisons}
        _write_json(base / "report.json", {
            "artifact_node": lane["artifact_node"], "minecraft": lane["minecraft"], "loader": lane["loader"],
            "scenario": lane["scenario"], "contract_sha256": expectation["contract_sha256"], "status": "pass",
            "elapsed_s": 1.5 + index,
            "production_sha256": _sha256(f"synthetic jar {lane['artifact_node']}".encode("utf-8")),
            "roles": roles,
        })


def _side(png: bytes, policy: dict, source_size: list[int]) -> tuple[dict, bytes]:
    webp = derive_webp(png, box=policy["derivative_box"], quality=policy["webp_quality"], method=policy["webp_method"])
    width, height = thumbnail_size(source_size, policy["derivative_box"])
    digest = _sha256(webp)
    record = {
        "image": {"path": f"images/{digest}.webp", "sha256": digest, "size": len(webp), "width": width,
                  "height": height, "format": "webp", "pixel": inspect_webp(webp, SizePolicy.exact(width, height))},
        "source": {"sha256": _sha256(png), "width": source_size[0], "height": source_size[1],
                   "pixel": inspect_png(png, SizePolicy.exact(*source_size))},
    }
    return record, webp


def family_bundle(ctx, family, key, target, expectation, producer, out_root, image_factory, outcome):
    if outcome not in FAMILY_OUTCOMES:
        raise ValueError(f"unknown outcome {outcome!r}")
    commit = target["subject"]["commit"]
    contract_sha256 = _sha256(ctx.read_blob(commit, CONTRACT, MAX_CONTRACT_BYTES))
    policy = ctx.config.family(family)["image_policy"]
    source_size = expectation["image_policy"]["source_size"]
    root = Path(out_root)
    (root / "images").mkdir()
    lanes, seed = [], 100
    for lane in expectation["lanes"]:
        pairs = []
        for capture in (item for item in expectation["captures"] if item["lane_id"] == lane["lane_id"]):
            reference_png = image_factory(source_size[0], source_size[1], seed)
            candidate_png = image_factory(source_size[0], source_size[1], seed + 1)
            seed += 2
            reference, reference_webp = _side(reference_png, policy, source_size)
            candidate, candidate_webp = _side(candidate_png, policy, source_size)
            for record, data in ((reference, reference_webp), (candidate, candidate_webp)):
                (root / record["image"]["path"]).write_bytes(data)
            measured = compare(reference_png, candidate_png, minimum_changed_fraction=0.0)
            pairs.append({
                "pair_id": capture["capture_id"], "capture_id": capture["capture_id"],
                "reference_capture_id": capture["capture_id"], "title": capture["title"],
                "expectation": "Synthetic reference and candidate images of the same checkpoint (not a product).",
                "runtime_evidence": f"PASS {capture['step']}: synthetic pair generated for {lane['artifact_node']}",
                "verdict": {"runtime_passed": True, "semantic_valid": True, "matches_reference": None,
                            "defect": False},
                "metrics": {"semantic_changed_fraction": measured["changed_fraction"],
                            "perceptual_delta": measured["rms_difference"],
                            "candidate_semantic_sha256": candidate["source"]["pixel"]["pixel_sha256"],
                            "reference_semantic_sha256": reference["source"]["pixel"]["pixel_sha256"]},
                "reference": reference, "candidate": candidate,
            })
        digest = _sha256(json.dumps(pairs, sort_keys=True).encode("utf-8"))
        lanes.append({"lane_id": lane["lane_id"], "artifact_node": lane["artifact_node"], "minecraft": lane["minecraft"],
                      "loader": lane["loader"],
                      "variant": {"id": "demo-addon", "name": "Demo add-on", "version": "1.0.0",
                                  "version_id": "synthetic-1"},
                      "review": {"reviewed_frame_count": 2 * len(pairs), "manifest_sha256": digest,
                                 "proof_sha256": _sha256(b"proof " + digest.encode("ascii")),
                                 "report_sha256": _sha256(b"report " + digest.encode("ascii"))},
                      "pairs": pairs})
    coverage = commit if outcome != "unavailable" else "0" * 40
    _write_json(root / "manifest.json", {
        "kind": NATIVE_KIND, "schema_version": 1, "family": family, "key": key, "coverage_sha": coverage,
        "contract_sha256": contract_sha256 if outcome != "superseded" else "0" * 64,
        "producer": producer, "lanes": lanes, "not_applicable": [],
    })
