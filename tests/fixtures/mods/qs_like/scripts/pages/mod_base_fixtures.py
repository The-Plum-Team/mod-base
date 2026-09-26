"""Test-only fixtures of the Quick Skin-like mod (never loaded by the Pages host).

``synthesize`` writes the packaged-output tree the qs-like adapter's ``collect`` reads, in Quick
Skin's own ``result.json`` shape: ``profiles/<node>--<version>--<scenario>/result.json`` with one
report per role (every contract step as ``{name, status, message, screenshot}``) and the recorded
``pixel_validation`` (screenshot metrics by step, comparisons by ``first->second``), plus
``<role>/screenshots/<step>.png``. Consecutive captures of a role use different image seeds, so
every contracted comparison changes enough.

The optional ``conformance`` fixtures make every Quick Skin variant run on the unmodified fixture:

* ``delegated_extensions`` proves reuse of a tested pull-request run through
  ``quick-skin.runtime_source``, as Quick Skin does: through ``ctx.api`` (the conformance seeding
  seam) it uploads the tested run's ``tested-source`` seal and the handoff run's ``reused-source``
  descriptor as ZIP artifacts and adds the handoff run's reuse job, and the reference names the
  seal by id and digest (the adapter's ``authenticate_extensions`` downloads and binds both). The
  seal names the tested run's own branch and commit, the reused pull request's, which the kit's
  tested claim carries too;
* ``selected_extensions`` narrows the expectation to one capture of the ``session`` scenario (the
  second client's player list) through ``quick-skin.feature_selection``: the session lanes are
  re-captured only partly, so the adapter's ``compose`` completes them per frame from the named
  ``mb-baseline`` artifact, a mixed-epoch composition. Like Quick Skin's coverage certificate, the
  selection names a ZIP certificate that a seeded successful ``feature-coverage`` run issued for
  that baseline and the selective runtime (``ctx.api.handoff_run``) and, like Quick Skin's
  healthy-baseline certificate, names the retained baseline of every matrix target
  (``ctx.api.retained_baseline``: a stand-in for a key outside ``--keys``); like Quick Skin's Git
  admission, it records the diff from the baseline's commit to the tested head, which the kit's
  ``selected`` head makes :data:`SELECTED_CHANGE`;
* ``family_bundle`` writes the ``mod-compatibility`` family's native ``qs-like.compatibility`` v1
  bundle: per artifact node of the key one Ears lane pairing a clean reference with a modded
  candidate for every capture of the ``full`` scenario (Ears publishes no Forge build, so a Forge
  node is ``not_applicable``). :data:`FAMILY_OUTCOMES` lists what it can produce: ``available``
  (clean pairs of the subject), ``superseded`` (pairs reviewed against another scenario contract)
  and ``unavailable`` (pairs covering another commit than their envelope).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import zipfile
from pathlib import Path

from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import SizePolicy, inspect_png, inspect_webp
from mod_base.imaging.webp import derive_webp
from mod_base.model.documents import thumbnail_size

MATRIX = "release/release-matrix.json"
CONTRACT = "e2e/scenario-contract.json"
MAX_DOCUMENT = 1 << 20
RUNTIME_SOURCE = "quick-skin.runtime_source"
FEATURE_SELECTION = "quick-skin.feature_selection"
FAMILY_KIND = "qs-like.compatibility"
FAMILY_OUTCOMES = ("available", "superseded", "unavailable")
#: The optional mod of every family lane, and the loader it publishes no build for.
VARIANT = {"id": "ears", "name": "Ears", "version": "1.4.6", "version_id": "Ab12Cd34"}
UNSUPPORTED_LOADER = "forge"
#: The scenario whose captures are paired, and the first image seed of the family's pairs.
PAIRED_SCENARIO = "full"
FAMILY_SEED = 100
#: The capture a selective generation re-captures: one of the two ``session`` checkpoints.
SELECTED_CAPTURES = ("session.client_b.player_list",)
#: The new file the conformance ``selected`` head adds, the change that selection re-tests.
SELECTED_CHANGE = "e2e/conformance-selected-change.md"
#: The ZIP artifacts the adapter's network hooks authenticate (see its module docstring).
SEAL_ARTIFACT = "tested-source-e2e"
DESCRIPTOR_ARTIFACT = "reused-source-e2e"
REUSE_JOB = "Reuse the tested source"
CERTIFICATE_WORKFLOW = ".github/workflows/feature-coverage.yml"
CERTIFICATE_JOB = "Certify complete feature coverage"
CERTIFICATE_ARTIFACT = "feature-coverage-certificate"
CERTIFICATE_KIND = "qs-like.coverage-certificate"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def synthesize(ctx, target, expectation, out_root, image_factory):
    contract = json.loads(ctx.read_blob(target["subject"]["commit"], CONTRACT, MAX_DOCUMENT))
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


def _canonical(value) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _zip(name: str, value) -> bytes:
    """A one-document ZIP artifact, as ``actions/upload-artifact`` stores it."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 9, 1, 12, 0, 0)), _canonical(value))
    return stream.getvalue()


def delegated_extensions(ctx, target, tested_run):
    matrix = json.loads(ctx.read_blob(target["subject"]["commit"], MATRIX, MAX_DOCUMENT))
    reference = {"repository": matrix["repository"], "run_id": tested_run["id"], "tested_sha": tested_run["head_sha"],
                 "head_branch": tested_run["head_branch"]}
    seal = ctx.api.add_artifact(tested_run["id"], SEAL_ARTIFACT, _zip("tested-source.json", reference))
    reference["seal_artifact"] = {"id": seal["id"], "name": seal["name"], "digest": seal["digest"]}
    handoff = ctx.api.handoff_run
    ctx.api.add_artifact(handoff["id"], DESCRIPTOR_ARTIFACT, _zip("reused-source.json", reference))
    ctx.api.add_jobs(handoff["id"], handoff["run_attempt"], [{"name": REUSE_JOB, "status": "completed",
                                                             "conclusion": "success"}])
    return {RUNTIME_SOURCE: reference}


def _changed_paths(ctx, base: str, head: str) -> list[str]:
    """The paths of the Git diff from ``base`` to ``head`` in the simulated repository."""

    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(ctx.tmpdir), "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0"}
    listed = subprocess.run(["git", "-C", str(ctx.repo_root), "diff", "--name-only", "--no-renames", "-z", base, head,
                             "--"], check=True, capture_output=True, env=environment, timeout=60).stdout
    return sorted(path for path in listed.decode("utf-8").split("\0") if path)


def selected_extensions(ctx, target, baseline):
    subject = target["subject"]
    base = baseline["name"].split("--")[2]
    selection = {"captures": list(SELECTED_CAPTURES), "baseline_artifact_id": baseline["id"], "base_commit": base,
                 "changed_paths": _changed_paths(ctx, base, subject["commit"])}
    owner = ctx.api.add_run({"path": CERTIFICATE_WORKFLOW, "event": "workflow_run", "head_branch": subject["branch"],
                             "head_sha": subject["commit"], "display_title": "Feature coverage"})
    ctx.api.add_jobs(owner["id"], owner["run_attempt"], [{"name": CERTIFICATE_JOB, "status": "completed",
                                                         "conclusion": "success"}])
    matrix = json.loads(ctx.read_blob(subject["commit"], MATRIX, MAX_DOCUMENT))
    public = {}
    for row in matrix["targets"]:
        key = f"mc{row['minecraft']}"
        record = dict(baseline) if key == target["key"] else ctx.api.retained_baseline(key)
        public[key] = {name: record[name] for name in ("id", "name", "digest")}
    certificate = {"kind": CERTIFICATE_KIND, "baseline_artifact": dict(baseline),
                   "runtime_run_id": ctx.api.handoff_run["id"], "public_baselines": public,
                   "selection_sha256": hashlib.sha256(_canonical(selection)).hexdigest()}
    record = ctx.api.add_artifact(owner["id"], CERTIFICATE_ARTIFACT, _zip("certificate.json", certificate))
    return {FEATURE_SELECTION: {**selection, "certificate_artifact_id": record["id"]}}


def _pair_side(png: bytes, policy: dict, source_size: list[int]) -> tuple[dict, bytes]:
    """One pair side: its image record (the WebP derivative) and the derivative's bytes."""

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
    policy = ctx.config.family(family)["image_policy"]
    source_size = expectation["image_policy"]["source_size"]
    root = Path(out_root)
    (root / "images").mkdir()
    lanes, not_applicable, seed = [], [], FAMILY_SEED
    for lane in expectation["lanes"]:
        if lane["scenario"] != PAIRED_SCENARIO:
            continue
        if lane["loader"] == UNSUPPORTED_LOADER:
            not_applicable.append({"artifact_node": lane["artifact_node"], "minecraft": lane["minecraft"],
                                   "loader": lane["loader"], "variant_id": VARIANT["id"], "variant_name": VARIANT["name"],
                                   "reason": f"Ears publishes no Forge build for Minecraft {lane['minecraft']}."})
            continue
        pairs = []
        for capture in (item for item in expectation["captures"] if item["lane_id"] == lane["lane_id"]):
            reference_png = image_factory(source_size[0], source_size[1], seed)
            candidate_png = image_factory(source_size[0], source_size[1], seed + 1)
            seed += 2
            reference, reference_webp = _pair_side(reference_png, policy, source_size)
            candidate, candidate_webp = _pair_side(candidate_png, policy, source_size)
            for record, data in ((reference, reference_webp), (candidate, candidate_webp)):
                (root / record["image"]["path"]).write_bytes(data)
            measured = compare(reference_png, candidate_png, minimum_changed_fraction=0.0)
            pairs.append({
                "pair_id": capture["capture_id"], "capture_id": capture["capture_id"],
                "reference_capture_id": capture["capture_id"], "title": capture["title"],
                "expectation": f"{capture['expectation']} The Ears features render beside it.",
                "runtime_evidence": f"PASS {capture['step']}: Ears active on {lane['artifact_node']}",
                "verdict": {"runtime_passed": True, "semantic_valid": True, "matches_reference": None, "defect": False},
                "metrics": {"semantic_changed_fraction": measured["changed_fraction"],
                            "perceptual_delta": measured["rms_difference"],
                            "candidate_semantic_sha256": candidate["source"]["pixel"]["pixel_sha256"],
                            "reference_semantic_sha256": reference["source"]["pixel"]["pixel_sha256"]},
                "reference": reference, "candidate": candidate,
            })
        digest = _sha256(json.dumps(pairs, sort_keys=True).encode("utf-8"))
        lanes.append({"lane_id": f"{lane['artifact_node']}/{VARIANT['id']}", "artifact_node": lane["artifact_node"],
                      "minecraft": lane["minecraft"], "loader": lane["loader"], "variant": dict(VARIANT),
                      "review": {"reviewed_frame_count": 2 * len(pairs), "manifest_sha256": digest,
                                 "proof_sha256": _sha256(b"proof " + digest.encode("ascii")),
                                 "report_sha256": _sha256(b"report " + digest.encode("ascii"))},
                      "pairs": pairs})
    contract_sha256 = _sha256(ctx.read_blob(commit, CONTRACT, MAX_DOCUMENT))
    manifest = {"kind": FAMILY_KIND, "schema_version": 1, "family": family, "key": key,
                "coverage_sha": commit if outcome != "unavailable" else "0" * 40,
                "contract_sha256": contract_sha256 if outcome != "superseded" else "0" * 64,
                "producer": producer, "lanes": lanes, "not_applicable": not_applicable}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
