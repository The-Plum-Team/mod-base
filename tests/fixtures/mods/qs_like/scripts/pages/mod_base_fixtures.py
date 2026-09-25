"""Test-only ``synthesize`` hook of the Quick Skin-like fixture (never loaded by the Pages host).

It writes the packaged-output tree the qs-like adapter's ``collect`` reads, in Quick Skin's own
``result.json`` shape: ``profiles/<node>--<version>--<scenario>/result.json`` with one report per
role (every contract step as ``{name, status, message, screenshot}``) and the recorded
``pixel_validation`` (screenshot metrics by step, comparisons by ``first->second``), plus
``<role>/screenshots/<step>.png``. Consecutive captures of a role use different image seeds, so
every contracted comparison changes enough.
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
        jar = hashlib.sha256(f"jar:{lane['artifact_node']}".encode()).hexdigest()
        reports = {}
        for role in scenarios[lane["scenario"]]["roles"]:
            screenshots = profile / role["role"] / "screenshots"
            screenshots.mkdir(parents=True, exist_ok=True)
            steps, metrics, seed = [], {}, index
            for step in role["steps"]:
                screenshot = None
                if "capture" in step:
                    screenshot = f"{step['id']}.png"
                    path = screenshots / screenshot
                    path.write_bytes(image_factory(width, height, seed))
                    metrics[step["id"]] = ctx.image_metrics(path, ("exact", width, height))
                    seed += 1
                steps.append({"name": step["id"], "status": "pass", "screenshot": screenshot,
                              "message": f"PASS {step['id']}: renderer confirmed the expected state on {lane['loader']}"})
            comparisons = {}
            for comparison in role["comparisons"]:
                comparisons[f"{comparison['first']}->{comparison['second']}"] = compare(
                    screenshots / f"{comparison['first']}.png", screenshots / f"{comparison['second']}.png",
                    minimum_changed_fraction=comparison["minimum_changed_fraction"], region=comparison.get("region"))
            reports[role["role"]] = {"version": lane["minecraft"], "role": role["role"], "scenario": lane["scenario"],
                                     "contract_sha256": expectation["contract_sha256"], "status": "pass",
                                     "steps": steps,
                                     "pixel_validation": {"screenshots": metrics, "comparisons": comparisons}}
        result = {"artifact_node": lane["artifact_node"], "runtime_version": lane["minecraft"], "loader": lane["loader"],
                  "scenario": lane["scenario"], "contract_sha256": expectation["contract_sha256"], "jar_sha256": jar,
                  "installed_quickskin": [{"path": "server/mods/quick-skin.jar", "sha256": jar}], "port": 25565,
                  "status": "pass", "profile": profile_relative, "elapsed_s": 2.5 + index, "reports": reports}
        (profile / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
