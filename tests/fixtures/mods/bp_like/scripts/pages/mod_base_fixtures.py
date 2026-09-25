"""Test-only ``synthesize`` hook of the Block Pops-like fixture (never loaded by the Pages host).

It writes the packaged-output tree the bp-like adapter's ``collect`` reads, in Block Pops' own
shape: ``profiles/<node>--<minecraft>--<scenario>/result.json`` with the production and harness
JAR digests and one embedded ``report.json`` per role (``schema_version``, every contract step as
``{id, status, message, capture_id, screenshot}`` and ``pixel_validation`` keyed by step id and
``first->second``), plus ``<role>/screenshots/<capture_id>.png``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mod_base.imaging.compare import compare

CONTRACT = "e2e/scenario-contract.json"


def synthesize(ctx, target, expectation, out_root, image_factory):
    contract = json.loads(ctx.read_blob(target["subject"]["commit"], CONTRACT, 1 << 20))
    scenarios = {scenario["id"]: scenario for scenario in contract["scenarios"]}
    width, height = expectation["image_policy"]["source_size"]
    root = Path(out_root)
    for index, lane in enumerate(expectation["lanes"]):
        profile_relative = f"profiles/{lane['artifact_node']}--{lane['minecraft']}--{lane['scenario']}"
        profile = root / profile_relative
        reports = {}
        for role in scenarios[lane["scenario"]]["roles"]:
            screenshots = profile / role["role"] / "screenshots"
            screenshots.mkdir(parents=True, exist_ok=True)
            steps, metrics, paths, seed = [], {}, {}, 3 * index
            for step in role["steps"]:
                capture = step.get("capture")
                screenshot = None
                if capture is not None:
                    screenshot = f"{capture['capture_id']}.png"
                    path = screenshots / screenshot
                    path.write_bytes(image_factory(width, height, seed))
                    metrics[step["id"]] = ctx.image_metrics(path, ("exact", width, height))
                    paths[step["id"]] = path
                    seed += 1
                steps.append({"id": step["id"], "status": "pass",
                              "capture_id": None if capture is None else capture["capture_id"],
                              "screenshot": screenshot, "message": f"{step['id']} passed on {lane['artifact_node']}"})
            comparisons = {}
            for comparison in role["comparisons"]:
                comparisons[f"{comparison['first_step']}->{comparison['second_step']}"] = compare(
                    paths[comparison["first_step"]], paths[comparison["second_step"]],
                    minimum_changed_fraction=comparison["minimum_changed_fraction"], region=comparison.get("region"))
            reports[role["role"]] = {"schema_version": 1, "minecraft": lane["minecraft"], "role": role["role"],
                                     "scenario": lane["scenario"], "contract_sha256": expectation["contract_sha256"],
                                     "status": "pass", "steps": steps,
                                     "pixel_validation": {"screenshots": metrics, "comparisons": comparisons}}
        result = {"status": "pass", "error": None, "artifact_node": lane["artifact_node"],
                  "minecraft": lane["minecraft"], "loader": lane["loader"], "scenario": lane["scenario"],
                  "contract_sha256": expectation["contract_sha256"], "profile": profile_relative,
                  "production_jar_sha256": hashlib.sha256(f"prod:{lane['artifact_node']}".encode()).hexdigest(),
                  "harness_jar_sha256": hashlib.sha256(f"harness:{lane['artifact_node']}".encode()).hexdigest(),
                  "reports": reports}
        (profile / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
