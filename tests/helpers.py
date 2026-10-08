"""Shared test helpers: a deterministic PNG pattern generator, JSON helpers and coherent sample
documents for every SPEC §3 kind.

``sample_documents()`` is the single source of ``tests/fixtures/documents/valid/*.json``; a test
asserts the committed fixtures equal it, so the fixtures can be regenerated with::

    PYTHONPATH=src python3 -c "from tests.helpers import write_document_fixtures; write_document_fixtures()"

Other units may import these builders to create their own coherent documents.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.model.documents import compact_identity_sha256

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DOCUMENT_FIXTURES = FIXTURES / "documents"

# -- PNG pattern generator -------------------------------------------------------------------------

PALETTE = (
    (214, 61, 61),
    (54, 170, 92),
    (66, 92, 206),
    (228, 196, 70),
    (150, 78, 196),
    (58, 184, 196),
    (236, 136, 58),
    (118, 122, 128),
)


def pattern_png(width: int, height: int, *, seed: int = 0) -> bytes:
    """Deterministic RGB PNG bytes of ``width`` x ``height`` that pass the 8-metric blank checks.

    An 8x4 grid of saturated palette blocks (rotated by ``seed``) is scaled to the full size with
    nearest-neighbour sampling and blended 25% with a diagonal luma gradient, so the 160x90 sample
    always holds many meaningful colours, high entropy and no dominant dark or light share. Sizes
    down to 8x4 work; the same arguments always produce the same bytes. The source-tree
    ``mod_base.imaging.png.pattern_png`` (the conformance ``image_factory``, MB2) must produce the
    same bytes for the same arguments; MB2 pins that parity with a test.
    """

    from PIL import Image

    if width < 8 or height < 4:
        raise ValueError("pattern_png needs at least 8x4 pixels")
    blocks = Image.new("RGB", (8, 4))
    blocks.putdata([PALETTE[(column + 3 * row + seed) % len(PALETTE)] for row in range(4) for column in range(8)])
    base = blocks.resize((width, height), Image.Resampling.NEAREST)
    gradient = Image.linear_gradient("L").rotate(45 + 90 * (seed % 4), resample=Image.Resampling.BILINEAR,
                                                 expand=False).resize((width, height), Image.Resampling.BILINEAR)
    shaded = Image.blend(base, Image.merge("RGB", (gradient, gradient, gradient)), 0.25)
    stream = io.BytesIO()
    shaded.save(stream, format="PNG", optimize=False, compress_level=6)
    return stream.getvalue()


# -- JSON helpers ----------------------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    return canonical_json(value)


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json_bytes(data: bytes) -> Any:
    return strict_loads(data, label="fixture", max_bytes=64 * 1024 * 1024)


def load_fixture(relative: str) -> Any:
    """Strictly decode ``tests/fixtures/documents/<relative>``."""

    return load_json_bytes((DOCUMENT_FIXTURES / relative).read_bytes())


def _pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise ValueError(f"JSON pointer must start with '/': {pointer!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def _container(document: Any, parts: list[str]) -> Any:
    target = document
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target[part]
    return target


def expand_value(value: Any) -> Any:
    """Expand ``{"$repeat": [text, count]}`` placeholders used by mutation fixtures."""

    if isinstance(value, dict) and set(value) == {"$repeat"}:
        text, count = value["$repeat"]
        return text * count
    return value


def apply_mutation(document: Any, mutation: dict[str, Any]) -> Any:
    """Return a mutated deep copy: ``set`` is ``[[pointer, value], ...]``, ``delete`` a list of
    pointers (applied after ``set``)."""

    result = copy.deepcopy(document)
    for pointer, value in mutation.get("set", []):
        parts = _pointer_parts(pointer)
        target = _container(result, parts)
        if isinstance(target, list):
            index = int(parts[-1])
            if index == len(target):
                target.append(expand_value(value))
            else:
                target[index] = expand_value(value)
        else:
            target[parts[-1]] = expand_value(value)
    for pointer in mutation.get("delete", []):
        parts = _pointer_parts(pointer)
        target = _container(result, parts)
        if isinstance(target, list):
            del target[int(parts[-1])]
        else:
            del target[parts[-1]]
    return result


# -- Coherent sample documents ---------------------------------------------------------------------

REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
KEY = "mc1.20.1"
FAMILY_ID = "mod-compatibility"
E2E_WORKFLOW = ".github/workflows/on-demand-e2e.yml"
FAMILY_WORKFLOW = ".github/workflows/mod-compatibility-review.yml"


def h(label: str, length: int = 64) -> str:
    """A deterministic lowercase hex identifier derived from ``label``."""

    return hashlib.sha256(label.encode("utf-8")).hexdigest()[:length]


COMMIT = h("subject-commit", 40)
TREE = h("subject-tree", 40)
KIT_SHA = h("kit-commit", 40)
SUBJECT = {"branch": "master", "commit": COMMIT, "tree": TREE}
KIT = {"repository": "The-Plum-Team/mod-base", "sha": KIT_SHA, "version": "0.9.0"}
PAGES_RUN_ID = 500
HANDOFF_RUN_ID = 101
HANDOFF_ARTIFACT_ID = 9001
CREATED_AT = "2026-09-25T10:00:00Z"
LANES = (
    ("1.20.1-fabric/full", "1.20.1-fabric", "fabric"),
    ("1.20.1-forge/full", "1.20.1-forge", "forge"),
)
STEPS = (
    ("title_screen", "Title screen", "The Quick Skin title-screen button is visible.", "key"),
    ("skin_apply", "Applied local skin", "The plaid local skin renders on the player model.", "all"),
)


def _pixel(label: str, width: int, height: int) -> dict[str, Any]:
    return {
        "width": width,
        "height": height,
        "file_sha256": h(f"file:{label}"),
        "pixel_sha256": h(f"pixels:{label}"),
        "luma_entropy": 5.123,
        "meaningful_colors": 18,
        "dark_fraction": 0.0123,
        "light_fraction": 0.0012,
    }


def run_claim(run_id: int, *, workflow_path: str = E2E_WORKFLOW, commit: str = COMMIT) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "run_attempt": 1,
        "workflow_path": workflow_path,
        "branch": "master",
        "commit": commit,
        "controller_branch": "master",
        "controller_sha": commit,
    }


def run_record(run_id: int, *, workflow_path: str = E2E_WORKFLOW, event: str = "workflow_dispatch") -> dict[str, Any]:
    return {**run_claim(run_id, workflow_path=workflow_path), "event": event, "created_at": CREATED_AT,
            "conclusion": "success", "head_sha": COMMIT}


def expectation() -> dict[str, Any]:
    lanes = [
        {"lane_id": lane_id, "artifact_node": node, "minecraft": "1.20.1", "loader": loader, "java": 17,
         "scenario": "full", "roles": ["client_a"]}
        for lane_id, node, loader in LANES
    ]
    captures = []
    comparisons = []
    for lane_id, _node, _loader in LANES:
        for order, (step, title, text, tier) in enumerate(STEPS):
            captures.append({
                "frame_id": f"{lane_id}/client_a/{step}",
                "capture_id": f"full.client_a.{step}",
                "capture_order": order,
                "lane_id": lane_id,
                "role": "client_a",
                "step": step,
                "title": title,
                "expectation": text,
                "review_tier": tier,
            })
        comparisons.append({
            "comparison_id": f"{lane_id}/client_a/skin_changed",
            "lane_id": lane_id,
            "role": "client_a",
            "first_frame_id": f"{lane_id}/client_a/title_screen",
            "second_frame_id": f"{lane_id}/client_a/skin_apply",
            "minimum_changed_fraction": 0.02,
            "region": [0.25, 0.1, 0.75, 0.9],
        })
    return {
        "kind": "mod-base.evidence.expectation",
        "schema_version": 1,
        "repository": REPOSITORY,
        "key": KEY,
        "label": "Minecraft 1.20.1",
        "subject": dict(SUBJECT),
        "matrix_sha256": h("matrix"),
        "contract_sha256": h("contract"),
        "contract_path": "e2e/scenario-contract.json",
        "profile": "pr",
        "scope": {"kind": "complete"},
        "image_policy": {"source_size": [1920, 1080], "derivative_box": [1600, 900], "webp_quality": 82,
                         "webp_method": 6, "pixel_metrics_version": 1},
        "scenarios": [{"id": "full", "title": "Full suite"}],
        "lanes": lanes,
        "captures": captures,
        "comparisons": comparisons,
        "anchor": {"artifact_nodes": ["1.20.1-fabric", "1.20.1-forge"]},
    }


def _file(path: str, data_label: str, size: int) -> dict[str, Any]:
    return {"path": path, "sha256": h(f"file:{data_label}"), "size": size}


def _evidence_lanes(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {**lane, "profile": "pr", "status": "pass", "elapsed_s": 312.5,
         "jars": {"production_sha256": h(f"jar:{lane['loader']}"), "harness_sha256": h(f"harness:{lane['loader']}")}}
        for lane in document["lanes"]
    ]


def _frame_base(capture: dict[str, Any], lane: dict[str, Any]) -> dict[str, Any]:
    return {
        **capture,
        "artifact_node": lane["artifact_node"],
        "minecraft": lane["minecraft"],
        "loader": lane["loader"],
        "scenario": lane["scenario"],
        "runtime_evidence": f"PASS {capture['step']}: renderer confirmed the expected state on {lane['loader']}",
    }


def handoff() -> dict[str, Any]:
    wanted = expectation()
    expectation_bytes = canonical_json(wanted)
    lanes = {lane["lane_id"]: lane for lane in wanted["lanes"]}
    frames = []
    files = [{"path": "expectation.json", "sha256": sha256_of(expectation_bytes), "size": len(expectation_bytes)}]
    for capture in wanted["captures"]:
        lane = lanes[capture["lane_id"]]
        label = f"source:{capture['frame_id']}"
        path = f"runtime/profiles/{lane['artifact_node']}/client_a/{capture['step']}.png"
        source = {"path": path, "sha256": h(f"file:{label}"), "size": 654321, "width": 1920, "height": 1080,
                  "format": "png", "pixel": _pixel(label, 1920, 1080)}
        frames.append({**_frame_base(capture, lane), "source": source})
        files.append({"path": path, "sha256": source["sha256"], "size": source["size"]})
    for _lane_id, node, _loader in LANES:
        files.append(_file(f"runtime/profiles/{node}/result.json", f"result:{node}", 20480))
    comparisons = [
        {**comparison, "source": {"changed_fraction": 0.1234567, "rms_difference": 12.345,
                                  "required_changed_fraction": comparison["minimum_changed_fraction"],
                                  "region": comparison["region"]}}
        for comparison in wanted["comparisons"]
    ]
    return {
        "kind": "mod-base.evidence.handoff",
        "schema_version": 1,
        "repository": REPOSITORY,
        "key": KEY,
        "kit": dict(KIT),
        "subject": dict(SUBJECT),
        "provenance": {"handoff": run_claim(HANDOFF_RUN_ID), "tested": run_claim(HANDOFF_RUN_ID), "reuse": "none",
                       "coverage_sha": COMMIT},
        "expectation": {"path": "expectation.json", "sha256": sha256_of(expectation_bytes),
                        "size": len(expectation_bytes)},
        "matrix_sha256": wanted["matrix_sha256"],
        "contract_sha256": wanted["contract_sha256"],
        "scope": {"kind": "complete"},
        "extensions": None,
        "lanes": _evidence_lanes(wanted),
        "frames": frames,
        "comparisons": comparisons,
        "files": sorted(files, key=lambda record: record["path"]),
    }


def _derivative(frame_id: str) -> dict[str, Any]:
    digest = h(f"file:derivative:{frame_id}")
    pixel = _pixel(f"derivative:{frame_id}", 1600, 900)
    return {"path": f"images/{digest}.webp", "sha256": digest, "size": 98765, "width": 1600, "height": 900,
            "format": "webp", "pixel": pixel}


BASELINE_ARTIFACT_ID = 8001
BASELINE_COMMIT = h("baseline-commit", 40)
BASELINE_NAME = grammar.baseline_name(KEY, BASELINE_COMMIT, 77)
BASELINE_OWNER_RUN_ID = 450
#: The tested run of a composed fixture's ``epoch: baseline`` frames (the baseline generation's).
BASELINE_RUN = {**run_claim(77, commit=BASELINE_COMMIT), "event": "workflow_dispatch",
                "created_at": "2026-09-20T10:00:00Z", "conclusion": "success", "head_sha": BASELINE_COMMIT}


def _compact_without_selection(*, composed: bool) -> dict[str, Any]:
    """The compact manifest before its selection is embedded (no ``selection`` member and no
    ``selection.json`` record): exactly what ``compact_identity_sha256`` hashes."""

    raw = handoff()
    frames = []
    files = [record for record in raw["files"] if record["path"] == "expectation.json"]
    for position, frame in enumerate(raw["frames"]):
        source = {key: value for key, value in frame["source"].items() if key != "path"}
        derivative = _derivative(frame["frame_id"])
        item = {**{key: value for key, value in frame.items() if key != "source"}, "source": source,
                "derivative": derivative}
        if composed:
            # The Fabric lane was not re-tested (the baseline's), the Forge lane was (the selection's).
            if frame["loader"] == "fabric":
                item["epoch"] = "baseline"
                item["tested"] = {**BASELINE_RUN, "jar_sha256": h(f"jar:{frame['loader']}")}
            else:
                item["epoch"] = "selected"
                item["tested"] = {**run_record(HANDOFF_RUN_ID), "jar_sha256": h(f"jar:{frame['loader']}")}
        frames.append(item)
        files.append({"path": derivative["path"], "sha256": derivative["sha256"], "size": derivative["size"]})
    comparisons = [
        {**comparison, "derivative": {"changed_fraction": 0.1111111, "rms_difference": 11.5,
                                      "required_changed_fraction": comparison["minimum_changed_fraction"],
                                      "region": comparison["region"]}}
        for comparison in raw["comparisons"]
    ]
    document = {
        **{key: value for key, value in raw.items() if key not in {"kind", "frames", "comparisons", "files"}},
        "kind": "mod-base.evidence.compact",
        "source_artifact": {"kind": "handoff", "id": HANDOFF_ARTIFACT_ID, "name": grammar.handoff_name(KEY, 1),
                            "digest": f"sha256:{h('handoff-zip')}", "size": 4_567_890, "run_id": HANDOFF_RUN_ID,
                            "run_attempt": 1},
        "frames": frames,
        "comparisons": comparisons,
        "files": sorted(files, key=lambda record: record["path"]),
    }
    if composed:
        document["scope"] = {
            "kind": "composed",
            "components": {
                "baseline": {"artifact_id": BASELINE_ARTIFACT_ID, "name": BASELINE_NAME,
                             "digest": f"sha256:{h('baseline-zip')}", "manifest_sha256": h("baseline-manifest")},
                "selected_manifest_sha256": h("selected-manifest"),
            },
        }
    return document




def _selection_for(document: dict[str, Any]) -> dict[str, Any]:
    """The final selection embedded in ``document`` (see ``documents.check_compact_selection``)."""

    selection_document = {
        "kind": "mod-base.selection",
        "schema_version": 1,
        "repository": REPOSITORY,
        "key": KEY,
        "kit": dict(KIT),
        "implementation": implementation(),
        "subject": dict(SUBJECT),
        "coverage_sha": COMMIT,
        "selected_artifact": {**document["source_artifact"], "workflow_path": E2E_WORKFLOW, "created_at": CREATED_AT},
        "source": {"handoff_run": run_record(HANDOFF_RUN_ID), "tested_run": run_record(HANDOFF_RUN_ID),
                   "reuse": "none", "kit_binding": {"source": "workflow_file", "sha": KIT_SHA}},
        "expectation_sha256": canonical_sha256(expectation()),
        "source_manifest_sha256": h("source-manifest"),
        "manifest_sha256": compact_identity_sha256({**document, "selection": {}}),
        "binding": {"mode": "reencode-identical", "frames": len(document["frames"]),
                    "derivatives": len({frame["derivative"]["path"] for frame in document["frames"]})},
        "extensions_verified": [],
    }
    if document["scope"]["kind"] == "composed":
        selection_document["composition"] = {
            "baseline_artifact": {"id": BASELINE_ARTIFACT_ID, "name": BASELINE_NAME,
                                  "digest": f"sha256:{h('baseline-zip')}", "owner_run_id": BASELINE_OWNER_RUN_ID},
            "selected_manifest_sha256": h("selected-manifest"),
        }
    return selection_document


def compact(*, composed: bool = False) -> dict[str, Any]:
    document = _compact_without_selection(composed=composed)
    selection_bytes = canonical_json(_selection_for(document))
    record = {"path": "selection.json", "sha256": sha256_of(selection_bytes), "size": len(selection_bytes)}
    document["selection"] = dict(record)
    document["files"] = sorted(document["files"] + [record], key=lambda item: item["path"])
    return document


def embedded_selection(*, composed: bool = False) -> dict[str, Any]:
    """The final selection that :func:`compact` embeds as ``selection.json``."""

    return _selection_for(_compact_without_selection(composed=composed))


def selection_draft() -> dict[str, Any]:
    """The draft ``authenticate`` writes: the embedded selection without its completion fields."""

    return {key: value for key, value in embedded_selection().items()
            if key not in ("manifest_sha256", "binding", "composition")}


def anchor() -> dict[str, Any]:
    raw = handoff()
    frames = []
    files = [record for record in raw["files"] if record["path"] == "expectation.json"]
    for frame in raw["frames"]:
        digest = h(f"file:canonical:{frame['frame_id']}")
        pixel = {**_pixel(f"canonical:{frame['frame_id']}", 1920, 1080), "file_sha256": digest}
        source = {"path": f"images/{digest}.png", "sha256": digest, "pixel_sha256": pixel["pixel_sha256"],
                  "size": 1_234_567, "width": 1920, "height": 1080, "format": "png", "pixel": pixel}
        frames.append({**{key: value for key, value in frame.items() if key != "source"}, "source": source})
        files.append({"path": source["path"], "sha256": digest, "size": source["size"]})
    return {
        "kind": "mod-base.evidence.anchor",
        "schema_version": 1,
        "repository": REPOSITORY,
        "key": KEY,
        "kit": dict(KIT),
        "subject": dict(SUBJECT),
        "provenance": {"handoff": run_claim(HANDOFF_RUN_ID), "tested": run_claim(HANDOFF_RUN_ID)},
        "reference": {"artifact_nodes": ["1.20.1-fabric", "1.20.1-forge"], "minecraft": ["1.20.1"],
                      "loaders": ["fabric", "forge"]},
        "source_artifact": {"id": HANDOFF_ARTIFACT_ID, "name": grammar.handoff_name(KEY, 1),
                            "digest": f"sha256:{h('handoff-zip')}", "run_id": HANDOFF_RUN_ID, "run_attempt": 1},
        "expectation": dict(raw["expectation"]),
        "lanes": raw["lanes"],
        "frames": frames,
        "files": sorted(files, key=lambda record: record["path"]),
    }


def family_envelope() -> dict[str, Any]:
    manifest_sha = h("native-manifest")
    files = [
        {"path": "images/" + h("native-image-1") + ".webp", "sha256": h("native-image-1"), "size": 45678},
        {"path": "images/" + h("native-image-2") + ".webp", "sha256": h("native-image-2"), "size": 45679},
        {"path": "manifest.json", "sha256": manifest_sha, "size": 34567},
    ]
    return {
        "kind": "mod-base.family.envelope",
        "schema_version": 1,
        "repository": REPOSITORY,
        "family": FAMILY_ID,
        "key": KEY,
        "kit": dict(KIT),
        "subject": dict(SUBJECT),
        "coverage_sha": COMMIT,
        "producer": run_claim(202, workflow_path=FAMILY_WORKFLOW),
        "native": {"manifest_path": "manifest.json", "manifest_sha256": manifest_sha,
                   "kind": "quick-skin-public-mod-compatibility", "schema_version": 6},
        "files": sorted(files, key=lambda record: record["path"]),
    }


def _paired_side(label: str, prefix: str) -> dict[str, Any]:
    digest = h(f"file:family:{label}")
    return {
        "image": {"path": f"{prefix}{digest}.webp", "sha256": digest, "size": 56789, "width": 1280, "height": 720,
                  "format": "webp", "pixel": {**_pixel(f"family:{label}", 1280, 720), "file_sha256": digest}},
        "source": {"sha256": h(f"file:family-source:{label}"), "width": 1920, "height": 1080,
                   "pixel": {**_pixel(f"family-source:{label}", 1920, 1080),
                             "file_sha256": h(f"file:family-source:{label}")}},
    }


def paired_lanes(prefix: str) -> list[dict[str, Any]]:
    return [{
        "lane_id": "1.20.1-fabric/ears",
        "artifact_node": "1.20.1-fabric",
        "minecraft": "1.20.1",
        "loader": "fabric",
        "variant": {"id": "ears", "name": "Ears", "version": "1.4.6", "version_id": "Ab12Cd34"},
        "review": {"reviewed_frame_count": 2, "manifest_sha256": h("review-manifest"), "proof_sha256": h("proof"),
                   "report_sha256": h("report")},
        "pairs": [{
            "pair_id": "mod-compatibility.client_a.integration_visual",
            "capture_id": "mod-compatibility.client_a.integration_visual",
            "reference_capture_id": "full.client_a.skin_apply",
            "title": "Ears features",
            "expectation": "The Ears features render beside the plaid skin.",
            "runtime_evidence": "PASS integration_visual: ears active on the local player",
            "verdict": {"runtime_passed": True, "semantic_valid": True, "matches_reference": None, "defect": False},
            "metrics": {"semantic_changed_fraction": 0.0412, "perceptual_delta": 3.25,
                        "candidate_semantic_sha256": h("candidate-semantic"),
                        "reference_semantic_sha256": h("reference-semantic")},
            "reference": _paired_side("reference", prefix),
            "candidate": _paired_side("candidate", prefix),
        }],
    }]


NOT_APPLICABLE = [{"artifact_node": "1.20.1-forge", "minecraft": "1.20.1", "loader": "forge", "variant_id": "ears",
                   "variant_name": "Ears", "reason": "Ears publishes no Forge build for Minecraft 1.20.1."}]
FAMILY_IMAGE_POLICY = {"derivative_box": [1280, 720], "webp_quality": 80, "webp_method": 6}


def family_paired() -> dict[str, Any]:
    return {
        "kind": "mod-base.family.paired",
        "schema_version": 1,
        "family": FAMILY_ID,
        "key": KEY,
        "coverage_sha": COMMIT,
        "subject": dict(SUBJECT),
        "status": "available",
        "provenance": {"producer": run_record(202, workflow_path=FAMILY_WORKFLOW),
                       "links": [{"label": "Compatibility runtime", "run_id": 303},
                                 {"label": "Clean reference", "run_id": HANDOFF_RUN_ID}]},
        "contracts": {"mod-compatibility-contract": h("compat-contract"), "scenario-contract": h("contract")},
        "image_policy": dict(FAMILY_IMAGE_POLICY),
        "lanes": paired_lanes("images/"),
        "not_applicable": copy.deepcopy(NOT_APPLICABLE),
    }


def implementation() -> dict[str, Any]:
    return {"branch": "master", "sha": COMMIT, "workflow_ref": f"{REPOSITORY}/.github/workflows/pages.yml@refs/heads/master",
            "run_id": PAGES_RUN_ID, "run_attempt": 1}


def selection() -> dict[str, Any]:
    return embedded_selection()


def promotion() -> dict[str, Any]:
    return {
        "kind": "mod-base.promotion",
        "schema_version": 1,
        "repository": REPOSITORY,
        "implementation": implementation(),
        "kit": dict(KIT),
        "heads": {"master": COMMIT},
        "bundles": [{"key": KEY, "collected_artifact_id": 7001, "collected_digest": f"sha256:{h('collected')}",
                     "manifest_sha256": h("compact-manifest"), "coverage_sha": COMMIT,
                     "selected_artifact_id": HANDOFF_ARTIFACT_ID}],
        "families": [{"family": FAMILY_ID, "key": KEY, "available": True, "status": "available",
                      "collected_artifact_id": 7002, "collected_digest": f"sha256:{h('collected-family')}",
                      "coverage_sha": COMMIT, "selected_artifact_id": 9101}],
        "site": {"files": 42, "bytes": 1_234_567, "inventory_sha256": h("site-inventory")},
    }


def build_record() -> dict[str, Any]:
    return {
        "kind": "mod-base.build",
        "schema_version": 1,
        "repository": REPOSITORY,
        "implementation": {"sha": COMMIT, "run_id": PAGES_RUN_ID, "run_attempt": 1,
                           "run_url": grammar.run_url(REPOSITORY, PAGES_RUN_ID),
                           "workflow_ref": f"{REPOSITORY}/.github/workflows/pages.yml@refs/heads/master"},
        "kit": dict(KIT),
        "pixel_metrics_version": 1,
        "site_inventory_sha256": h("site-inventory"),
    }


REPOSITORY_URL = f"https://github.com/{REPOSITORY}"


def site_data() -> dict[str, Any]:
    return {
        "kind": "mod-base.site",
        "schema_version": 1,
        "project": {
            "name": "Quick Skin",
            "tagline": "Change your look. Stay in the game.",
            "eyebrow": "Minecraft appearance mod",
            "description": "Change, preview, and synchronize local skins, capes, and CPM models in-game.",
            "license_label": "All Rights Reserved",
            "repository_url": REPOSITORY_URL,
            "issues_url": f"{REPOSITORY_URL}/issues",
            "icon": "assets/icon.png",
            "links": [{"id": "modrinth", "title": "Modrinth", "description": "Download Quick Skin from Modrinth.",
                       "url": "https://modrinth.com/mod/quick-skin"}],
        },
        "gallery_url": "e2e/",
        "releases": [{"key": KEY, "label": "Minecraft 1.20.1", "minecraft": ["1.20.1"], "loaders": ["fabric", "forge"],
                      "loader_names": ["Fabric", "Forge"], "frame_count": 4, "lane_count": 2,
                      "short_sha": COMMIT[:12], "subject_commit": COMMIT,
                      "tested_run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID)}],
        "families": [{"family": FAMILY_ID, "title": "Mod compatibility", "lane_count": 1, "available": True}],
        "copy": {"principles": ["The same staged production jars launch under their exact Minecraft and loader versions."],
                 "evidence_lead": "Every public frame comes from a successful packaged-JAR scenario."},
        "generated": {"implementation_sha": COMMIT, "kit_sha": KIT_SHA, "kit_version": "0.9.0",
                      "pages_run_url": grammar.run_url(REPOSITORY, PAGES_RUN_ID)},
    }


def gallery_data() -> dict[str, Any]:
    compact_document = compact()
    loader_names = {"fabric": "Fabric", "forge": "Forge"}
    lanes = [
        {"lane_id": lane["lane_id"], "key": KEY, "artifact_node": lane["artifact_node"], "minecraft": lane["minecraft"],
         "loader": lane["loader"], "loader_name": loader_names[lane["loader"]], "scenario": lane["scenario"],
         "roles": lane["roles"], "status": "pass", "elapsed_s": lane["elapsed_s"], "jars": lane["jars"]}
        for lane in compact_document["lanes"]
    ]
    frames = []
    for frame in compact_document["frames"]:
        derivative = frame["derivative"]
        frames.append({
            **{field: frame[field] for field in ("frame_id", "capture_id", "capture_order", "title", "expectation",
                                                 "runtime_evidence", "review_tier", "lane_id", "artifact_node",
                                                 "minecraft", "loader", "scenario", "role", "step")},
            "key": KEY,
            "loader_name": loader_names[frame["loader"]],
            "image": f"images/{KEY}/{derivative['sha256']}.webp",
            "width": derivative["width"],
            "height": derivative["height"],
            "alt": f"{frame['title']} on Minecraft {frame['minecraft']} with {loader_names[frame['loader']]}",
            "source": {"width": frame["source"]["width"], "height": frame["source"]["height"],
                       "file_sha256": frame["source"]["sha256"], "pixel": frame["source"]["pixel"]},
            "published": {"file_sha256": derivative["sha256"], "format": "webp", "pixel": derivative["pixel"]},
            "provenance": {"handoff_run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID), "handoff_commit": COMMIT,
                           "tested_run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID), "tested_commit": COMMIT,
                           "tested_created_at": CREATED_AT, "coverage_sha": COMMIT},
        })
    comparisons = [
        {"comparison_id": item["comparison_id"], "key": KEY, "lane_id": item["lane_id"], "role": item["role"],
         "first_frame_id": item["first_frame_id"], "second_frame_id": item["second_frame_id"],
         "source": item["source"], "published": item["derivative"]}
        for item in compact_document["comparisons"]
    ]
    return {
        "kind": "mod-base.gallery",
        "schema_version": 1,
        "project": {"name": "Quick Skin", "repository_url": REPOSITORY_URL,
                    "actions_url": f"{REPOSITORY_URL}/actions/workflows/on-demand-e2e.yml"},
        "labels": {"scenarios": {"full": "Full suite"}, "roles": {"client_a": "Client A"},
                   "tiers": {"all": "Full review", "key": "Key checkpoint"},
                   "loaders": dict(loader_names), "release_prefix": "Minecraft",
                   "search_placeholder": "Cape, skin, title screen…"},
        "copy": {"gallery_lead": "Browse every validated capture by version.",
                 "methodology": ["The required E2E gate validates mod state, screenshot integrity and pixel changes."],
                 "family_notes": {FAMILY_ID: "Each lane shows its contracted pairs (2, 5 or 7)."}},
        "releases": [{
            "key": KEY, "label": "Minecraft 1.20.1", "minecraft": ["1.20.1"], "loaders": ["fabric", "forge"],
            "loader_names": ["Fabric", "Forge"], "frame_count": 4, "lane_count": 2, "scenarios": ["full"],
            "contract_sha256": h("contract"),
            "contract_url": f"{REPOSITORY_URL}/blob/{COMMIT}/e2e/scenario-contract.json",
            "matrix_sha256": h("matrix"), "subject": dict(SUBJECT), "coverage_sha": COMMIT,
            "short_sha": COMMIT[:12],
            "handoff": {"run_id": HANDOFF_RUN_ID, "run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID),
                        "created_at": CREATED_AT},
            "tested": {"run_id": HANDOFF_RUN_ID, "run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID),
                       "created_at": CREATED_AT, "commit": COMMIT},
            "scope": "complete", "reuse": "none",
        }],
        "lanes": lanes,
        "frames": frames,
        "comparisons": comparisons,
        "families": [{
            "family": FAMILY_ID, "title": "Mod compatibility",
            "description": "Clean reference versus mod-installed pairs, published only after a complete clean review.",
            "available": True, "status": "available",
            "releases": [{
                "key": KEY, "available": True, "status": "available", "coverage_sha": COMMIT,
                "contracts": {"mod-compatibility-contract": h("compat-contract"), "scenario-contract": h("contract")},
                "links": [{"label": "Compatibility runtime", "run_url": grammar.run_url(REPOSITORY, 303)},
                          {"label": "Clean reference", "run_url": grammar.run_url(REPOSITORY, HANDOFF_RUN_ID)}],
                "image_policy": dict(FAMILY_IMAGE_POLICY),
            }],
            "lanes": [{**lane, "key": KEY} for lane in paired_lanes(f"families/{FAMILY_ID}/images/")],
            "not_applicable": [{**entry, "key": KEY} for entry in copy.deepcopy(NOT_APPLICABLE)],
        }],
        "build": {"implementation_sha": COMMIT, "kit_sha": KIT_SHA, "kit_version": "0.9.0", "pixel_metrics_version": 1},
    }


def template_manifest() -> dict[str, Any]:
    return {
        "kind": "mod-base.template-manifest",
        "schema_version": 1,
        "files": [
            {"path": ".gitattributes", "class": "managed", "source": "managed/.gitattributes"},
            {"path": ".github/workflows/pages.yml", "class": "managed", "source": "managed/.github/workflows/pages.yml"},
            {"path": "scripts/ci/mod_base_kit.py", "class": "managed", "source": "managed/scripts/ci/mod_base_kit.py"},
            {"path": "docs/ai/shared/REPOSITORY.md", "class": "managed", "source": "managed/docs/ai/shared/REPOSITORY.md"},
            {"path": ".gitignore", "class": "fragment", "source": "seed/.gitignore.base",
             "lines": ["e2e-out/", "out/", "_site/", "public-evidence/"]},
            {"path": ".github/pull_request_template.md", "class": "fragment",
             "source": "seed/.github/pull_request_template.md.tmpl",
             "markers": ["CONTRIBUTING.md", "AGENTS.md", "## Summary", "## Validation", "## AI assistance"]},
            {"path": "AGENTS.md", "class": "fragment", "source": "seed/AGENTS.md.tmpl"},
            {"path": "CONTRIBUTING.md", "class": "seeded", "source": "seed/CONTRIBUTING.md.tmpl"},
        ],
    }


def kit_stamp() -> dict[str, Any]:
    return {"kind": "mod-base.kit-stamp", "schema_version": 1, "sha": KIT_SHA, "version": "0.9.0",
            "tree_digest": f"sha256:{h('kit-tree')}"}


def ci_plan() -> dict[str, Any]:
    """Synthetic protocol fixture; native mod parity has its own immutable fixtures."""

    from mod_base.build_ci.protocol import plan_sha256

    repository = "example/mod"
    document = {
        "kind": "mod-base.build.plan", "schema_version": 1, "build_adapter_api": 1,
        "profile": "block-pops",
        "identity": {
            "repository": repository, "source_repository": repository, "pr_number": 7,
            "head_sha": "1" * 40, "head_branch": "feature/example",
            "base_sha": "2" * 40, "base_branch": "master",
            "controller_sha": "2" * 40, "controller_workflow": ".github/workflows/build-gate.yml",
            "controller_ref": f"{repository}/.github/workflows/build-gate.yml@refs/heads/master",
            "kit": {"repository": "The-Plum-Team/mod-base", "sha": "3" * 40,
                    "version": "1.1.0", "tree_digest": "sha256:" + "4" * 64},
            "tested_sha": "5" * 40, "tested_tree": "6" * 40, "tested_parents": ["2" * 40, "1" * 40],
            "policy_sha256": h("policy"), "inventory_blob": "7" * 40, "inventory_sha256": h("inventory"),
            "scenario_sha256": h("scenarios"), "runtime_selection_sha256": h("full"), "graph_version": 1,
        },
        "targets": [{"id": "target-a", "java": 21, "native_contract_sha256": h("target-contract"),
                     "outputs": [{"path": f"staged/lane-a/{role}.{'jar' if role in ('production', 'harness') else 'json'}",
                                  "lane_id": "lane-a", "role": role}
                                 for role in ("production", "harness", "sbom", "native-report")]}],
        "lanes": [{"id": "lane-a", "target_id": "target-a", "native_contract_sha256": h("lane-contract"),
                   "obligations": ["scenario/example/server/probe"]}],
    }
    document["plan_sha256"] = plan_sha256(document)
    return document


#: The managed producer callers, written out: a rename in ``mod_base.workflow`` must not follow.
CI_WORKFLOWS = {"build": ".github/workflows/mod-base-build.yml",
                "packaged": ".github/workflows/mod-base-packaged-e2e.yml"}
#: The kit workflows each managed caller references, whether or not their calling jobs run.
CI_KIT_CALLEES = {"build": ("build.yml",), "packaged": ("select-build.yml", "build.yml", "packaged-e2e.yml")}
CI_GRAPH_FIXTURES = FIXTURES / "ci_graphs"


def ci_push_plan() -> dict[str, Any]:
    """The fixture plan for a protected push: one default-branch commit is the commit tested, the
    commit executed and the head GitHub records the run under."""

    from mod_base.build_ci.protocol import plan_sha256

    plan = ci_plan()
    plan["identity"].update(pr_number=0, head_branch="master", head_sha="2" * 40, tested_sha="2" * 40,
                            tested_tree="8" * 40, tested_parents=["a" * 40])
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def ci_run_producer(plan: dict[str, Any], producer: str = "build", mode: str | None = None) -> dict[str, Any]:
    """The run of one managed caller for the plan's subject, as GitHub records it: a pull request's
    under its head commit, a push under the commit it runs from. ``mode`` is the run's graph mode
    (by default the full Build and the pull-request packaged run)."""

    from mod_base.build_ci.graph import run_graph

    identity, workflow = plan["identity"], CI_WORKFLOWS[producer]
    mode = mode or ("full" if producer == "build" else "pull-request")
    return {"run_id": 42 if producer == "build" else 43, "run_attempt": 2, "workflow_path": workflow,
            "workflow_ref": f"{identity['repository']}/{workflow}@refs/heads/{identity['base_branch']}",
            "api_head_sha": identity["head_sha"],
            "event": "pull_request_target" if identity["pr_number"] else "push",
            "graph_sha256": run_graph(producer, mode).sha256(plan),
            "upload_window": {"started_at": "2026-10-07T10:01:00Z", "completed_at": "2026-10-07T10:02:00Z"}}


def ci_producer(gate: str = "build") -> dict[str, Any]:
    return ci_run_producer(ci_plan(), gate)


def ci_graph_jobs(name: str) -> list[dict[str, Any]]:
    """The jobs of the literal Jobs API listing ``tests/fixtures/ci_graphs/<name>.json``."""

    listing = load_json_bytes((CI_GRAPH_FIXTURES / f"{name}.json").read_bytes())
    if listing["total_count"] != len(listing["jobs"]):
        raise ValueError(f"{name}: total_count disagrees with the listed jobs")
    return listing["jobs"]


def ci_api_run(plan: dict[str, Any], producer: str = "build", **changes: Any) -> dict[str, Any]:
    """A completed successful run of a managed caller as ``GET /actions/runs/{id}`` reports it.

    A ``pull_request_target`` run carries the pull request's head branch and commit; the commit it
    executed from appears only in ``referenced_workflows``, as the commit of the mod's local guard
    workflow (Block Pops run 37501309423). A protected push carries the commit it runs from."""

    identity, kit = plan["identity"], plan["identity"]["kit"]
    pull_request = bool(identity["pr_number"])
    run = {
        "id": 42 if producer == "build" else 43,
        "name": "Build" if producer == "build" else "Packaged E2E",
        "display_title": "Example change",
        "path": CI_WORKFLOWS[producer],
        "event": "pull_request_target" if pull_request else "push",
        "status": "completed",
        "conclusion": "success",
        "head_branch": identity["head_branch"],
        "head_sha": identity["head_sha"],
        "head_repository": {"full_name": identity["source_repository"]},
        "repository": {"full_name": identity["repository"]},
        "run_attempt": 2,
        "run_number": 246,
        "workflow_id": 331005979 if producer == "build" else 331006079,
        "created_at": "2026-10-07T10:00:00Z",
        "run_started_at": "2026-10-07T10:00:00Z",
        "updated_at": "2026-10-07T10:15:00Z",
        "previous_attempt_url": f"https://api.github.com/repos/{identity['repository']}/actions/runs/42/attempts/1",
        "pull_requests": [],
        "referenced_workflows": [
            {"path": f"{identity['repository']}/.github/workflows/mod-base-guard.yml@{identity['controller_sha']}",
             "sha": identity["controller_sha"], "ref": f"refs/heads/{identity['base_branch']}"},
            *({"path": f"{kit['repository']}/.github/workflows/{name}@{kit['sha']}", "sha": kit["sha"]}
              for name in CI_KIT_CALLEES[producer]),
        ],
    }
    run.update(changes)
    return run


def ci_api_artifact(descriptor: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """The artifact a descriptor selects as the REST API reports it: its ``workflow_run`` carries
    the head branch and commit of the producer run, like the run itself."""

    selected, producer = descriptor["artifact"], descriptor["producer"]
    record = {
        "id": selected["id"],
        "node_id": f"MDg6QXJ0aWZhY3Q{selected['id']}",
        "name": selected["name"],
        "size_in_bytes": selected["size"],
        "url": f"https://api.github.com/repos/{descriptor['identity']['repository']}/actions/artifacts/{selected['id']}",
        "expired": False,
        "digest": selected["digest"],
        "created_at": selected["created_at"],
        "updated_at": selected["created_at"],
        "expires_at": selected["expires_at"],
        "workflow_run": {"id": producer["run_id"], "repository_id": 1082631496, "head_repository_id": 1082631496,
                         "head_branch": descriptor["identity"]["head_branch"], "head_sha": producer["api_head_sha"]},
    }
    record.update(changes)
    return record


def ci_config() -> dict[str, Any]:
    files = ["scripts/ci/mod_base_build_adapter.py", "scripts/ci/mod_base_build_dispatch.py", "scripts/ci/pr_gate.py"]
    return {"kind": "mod-base.build.config", "schema_version": 1, "repository": "example/mod",
            "profile": "block-pops", "build_adapter_api": 1,
            "adapter": {"path": files[0], "dispatcher": files[1], "policy": files[2],
                        "files": [{"path": name, "sha256": h(name)} for name in sorted(files)]},
            "inventory": {"path": "release/release-matrix.json"},
            "scenario_contract": {"path": "e2e/scenario-contract.json"},
            "bundle": {"path": "build/release"},
            "contexts": {"build": "Trusted PR / Build and verify", "packaged": "Trusted PR / Packaged E2E gate"},
            "timeouts": {"policy_seconds": 3600, "target_seconds": 7200,
                         "runtime_seconds": 1800, "validator_seconds": 600}}


def ci_validation(hook: str = "verify_build", unit_id: str | None = None) -> dict[str, Any]:
    """Inert verifier-output fixture; actual native semantics are separate conformance evidence."""
    plan = ci_plan()
    from mod_base.model.canonical import canonical_json
    units = plan["lanes"] if hook == "verify_runtime" else plan["targets"]
    if hook != "verify_build":
        units = [unit for unit in units if unit["id"] == unit_id]
    return {"kind": "mod-base.ci.validation", "schema_version": 1,
            "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
            "hook": hook, "unit_id": unit_id, "run_id": 42, "run_attempt": 2,
            "source_config_sha256": h("configbytes"), "input_sha256": h("inputs"),
            "reports": [{"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
                         "path": f"reports/{unit['id']}.json",
                         "size": len(canonical_json({"fixture_unit": unit["id"]})),
                         "sha256": h(canonical_json({"fixture_unit": unit["id"]}).decode())} for unit in units]}


def ci_run_descriptor(plan: dict[str, Any], producer: str, mode: str | None, kind: str, *,
                      unit_id: str | None = None, artifact_id: int = 100) -> dict[str, Any]:
    """One artifact of a run of ``producer`` in ``mode`` for the plan's subject."""

    record = ci_run_producer(plan, producer, mode)
    return {"identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
            "profile": plan["profile"], "producer": record,
            "artifact": {"id": artifact_id, "name": grammar.ci_artifact_name(kind, record["run_id"], 2, unit_id),
                         "digest": "sha256:" + h(kind + "-zip"), "size": 512,
                         "created_at": "2026-10-07T10:01:30Z", "expires_at": "2026-10-14T10:01:30Z"}}


def ci_descriptor(kind: str = "build", *, gate: str = "build", unit_id: str | None = None,
                  artifact_id: int = 100) -> dict[str, Any]:
    return ci_run_descriptor(ci_plan(), gate, None, kind, unit_id=unit_id, artifact_id=artifact_id)


def ci_envelope() -> dict[str, Any]:
    plan = ci_plan()
    files = sorted(({**output, "sha256": h(output["path"]), "size": 128}
                    for target in plan["targets"] for output in target["outputs"]), key=lambda item: item["path"])
    return {"kind": "mod-base.build.envelope", "schema_version": 1, "identity": plan["identity"],
              "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
              "producer": {key: value for key, value in ci_producer().items() if key != "upload_window"},
            "scope": "complete", "target_id": None, "files": files,
            "native_reports": [item["path"] for item in files if item["role"] == "native-report"]}


def ci_selection() -> dict[str, Any]:
    plan = ci_plan()
    request = ci_producer("packaged")
    return {"kind": "mod-base.ci.selection", "schema_version": 1, "identity": plan["identity"],
            "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
            "request": {key: request[key] for key in ("run_id", "run_attempt", "workflow_path", "workflow_ref")}
                       | {"nonce": h("request-nonce")},
            "build": ci_descriptor(), "envelope_sha256": canonical_sha256(ci_envelope())}


def ci_run_gate(plan: dict[str, Any], gate: str, mode: str) -> dict[str, Any]:
    """The receipt of ``gate`` for the plan's subject, sealed by a run in ``mode``: ``full`` is a
    Build run; ``pull-request``, ``selected`` and ``rebuilt`` are packaged runs, and a rebuilt
    packaged run also seals the Build gate of the Build it rebuilt."""

    producer = "build" if mode == "full" else "packaged"
    units = plan["targets"] if gate == "build" else plan["lanes"]
    if gate == "build":
        artifacts, owning = [ci_run_descriptor(plan, producer, mode, "build")], None
    else:
        artifacts = [ci_run_descriptor(plan, producer, mode, "runtime", unit_id=lane["id"], artifact_id=101 + index)
                     for index, lane in enumerate(plan["lanes"])]
        artifacts.append(ci_run_descriptor(plan, producer, mode, "results", artifact_id=101 + len(plan["lanes"])))
        owning = (ci_run_descriptor(plan, "packaged", "rebuilt", "build") if mode == "rebuilt"
                  else ci_run_descriptor(plan, "build", "full", "build"))
    return {"kind": "mod-base.ci.gate", "schema_version": 1, "identity": copy.deepcopy(plan["identity"]),
            "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
            "producer": {key: value for key, value in ci_run_producer(plan, producer, mode).items()
                         if key != "upload_window"},
            "gate": gate, "mode": mode, "artifacts": artifacts, "owning_build": owning,
            "native_receipts": [{"unit_id": unit["id"], "native_contract_sha256": unit["native_contract_sha256"],
                                 "report_sha256": h(unit["id"] + "-native")} for unit in units]}


def ci_gate(gate: str = "build") -> dict[str, Any]:
    return ci_run_gate(ci_plan(), gate, "full" if gate == "build" else "pull-request")


def ci_reuse() -> dict[str, Any]:
    from mod_base.build_ci.graph import run_graph

    plan = ci_plan()
    identity = copy.deepcopy(plan["identity"])
    identity.update(pr_number=0, head_sha="8" * 40, head_branch="master", tested_sha="8" * 40,
                    controller_sha="8" * 40, base_sha="8" * 40, tested_parents=["2" * 40])
    producer = ci_producer()
    # The covering run is a push to the default branch: recorded under the commit it runs from.
    producer.update(run_id=44, event="push", api_head_sha="8" * 40,
                    graph_sha256=run_graph("build", "reuse").sha256(plan))
    producer.pop("upload_window")
    return {"kind": "mod-base.ci.reuse", "schema_version": 1, "identity": identity,
            "plan_sha256": h("covered-plan"), "profile": plan["profile"], "producer": producer,
            "source": {"identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                       "build_seal": ci_descriptor("tested", unit_id="build"),
                       "packaged_seal": ci_descriptor("tested", gate="packaged", unit_id="packaged", artifact_id=103)}}


def ci_execution() -> dict[str, Any]:
    """Inert local execution handoff fixture; metadata does not establish physical origin."""

    import base64
    from mod_base.model.canonical import canonical_json
    import hashlib
    return {"kind": "mod-base.ci.execution", "schema_version": 1, "run_id": 42, "run_attempt": 2,
            "nonce": h("execution-nonce"), "plan_sha256": ci_plan()["plan_sha256"],
            "source_config_sha256": h("configbytes"),
            "input_sha256": hashlib.sha256(canonical_json(ci_envelope())).hexdigest(),
            "returncode": 0, "truncated": False, "log_base64": base64.b64encode(b"fixture log").decode("ascii")}


def ci_stat(*, inode: int = 99, size: int = 0, mode: int = stat.S_IFDIR | 0o755, uid: int = 0, gid: int = 0,
            links: int = 1) -> SimpleNamespace:
    """A synthetic ``os.stat_result`` for tests of metadata rules that need no real file."""
    return SimpleNamespace(st_dev=1, st_ino=inode, st_size=size, st_mode=mode, st_uid=uid,
                           st_gid=gid, st_nlink=links, st_mtime_ns=1, st_ctime_ns=1)


def ci_root_request(operation: str = "freeze-build-validation") -> dict[str, Any]:
    """Inert root-operation request data; no physical source, UID or execution authority."""
    plan = ci_plan()
    sources = {"controller_sha": plan["identity"]["controller_sha"], "controller_tree": "b" * 40,
               "config": {"path": "scripts/ci/mod-base-build.json", "mode": "100644",
                          "git_blob": "c" * 40, "sha256": h("configbytes"), "size": 1000},
               "files": [{"path": file["path"], "mode": "100644", "git_blob": "d" * 40,
                          "sha256": file["sha256"], "size": 1} for file in ci_config()["adapter"]["files"]]}
    validator = {"uid": 2001, "gid": 2001}
    if operation == "freeze-build-validation":
        arguments = {"validator": validator, "sources": sources, "plan": plan, "envelope": ci_envelope(),
                     "run_id": 42, "run_attempt": 2, "execution_nonce": h("execution-nonce")}
    elif operation == "freeze-runtime-validation":
        runtime = ci_runtime_envelope()
        runtime.update(scope="lane", lane_id="lane-a")
        arguments = {"validator": validator, "sources": sources, "plan": plan, "build": ci_envelope(),
                     "runtime": runtime, "lane_id": "lane-a", "run_id": 43, "run_attempt": 2,
                     "execution_nonce": h("execution-nonce")}
    else:
        raise ValueError(f"no sample root request for {operation!r}")
    return {"kind": "mod-base.ci.root-request", "schema_version": 1, "operation": operation,
            "nonce": h("root-request-nonce"),
            "boundary": {"home": "/home/runner", "uid": 1001, "gid": 121,
                         "device": 1, "inode": 10, "original_mode": 0o755},
            "arguments": arguments}


def ci_activation(mode: str = "disabled", rollback_from: str | None = None) -> dict[str, Any]:
    """The activation manifest of the ``ci_config`` mod in ``mode``; ``rollback_from`` is the mode a
    ``reviewed-rollback`` leaves (``shared-build`` unless given)."""

    if mode == "reviewed-rollback" and rollback_from is None:
        rollback_from = "shared-build"
    return {"kind": "mod-base.ci.activation", "schema_version": 1,
            "repository": ci_config()["repository"], "profile": ci_config()["profile"], "mode": mode,
            "rollback_from": rollback_from}


def ci_batch() -> dict[str, Any]:
    """Synthetic structural fixture; hashes are data, not Git or byte provenance."""
    repository = ci_config()['repository']
    return {'kind': 'mod-base.ci.batch', 'schema_version': 1,
            'repository': repository, 'profile': ci_config()['profile'],
            'base_branch': 'master', 'base_sha': '1'*40, 'base_tree': '2'*40,
            'branch': 'batch/fixture', 'policy_sha256': '1'*64, 'result_tree': '8'*40,
            'members': [{'pr_number': 1, 'source_repository': repository,
                         'head_branch': 'feature/one', 'head_sha': '3'*40, 'head_tree': '4'*40,
                         'draft': False, 'merge_base_sha': '1'*40, 'merge_base_tree': '2'*40,
                         'merge_base_bytes_sha256': '7'*64, 'head_bytes_sha256': '8'*64,
                         'patch': [{'path': 'src/main.java',
                                    'before': {'mode': '100644', 'size': 3, 'git_blob': '5'*40},
                                    'after': {'mode': '100644', 'size': 3, 'git_blob': '6'*40}}],
                         'parent_sha': '1'*40, 'squash_sha': '7'*40, 'result_tree': '8'*40}]}


def ci_runtime_envelope() -> dict[str, Any]:
    """Inert runtime inventory; native output mapping and bytes need independent proof."""
    plan = ci_plan()
    return {'kind': 'mod-base.ci.runtime-envelope', 'schema_version': 1,
            'identity': plan['identity'], 'plan_sha256': plan['plan_sha256'], 'profile': plan['profile'],
            'producer': {key: value for key, value in ci_producer('packaged').items() if key != 'upload_window'},
            'scope': 'complete', 'lane_id': None, 'owning_build': ci_descriptor(),
            'lanes': [{'id': lane['id'], 'native_contract_sha256': lane['native_contract_sha256']}
                      for lane in plan['lanes']],
            'files': [{'path': 'lanes/lane-a/result.json', 'lane_id': 'lane-a',
                       'role': 'native-report', 'size': 128, 'sha256': h('runtime-report')}]}


def sample_documents() -> dict[str, dict[str, Any]]:
    """Fixture name -> a coherent, valid document (see ``VALID_FIXTURE_KINDS``)."""

    return {
        "ci-plan": ci_plan(),
        "ci-config": ci_config(),
        "ci-activation": ci_activation(),
        "ci-batch": ci_batch(),
        "ci-runtime-envelope": ci_runtime_envelope(),
        "ci-envelope": ci_envelope(),
        "ci-selection": ci_selection(),
        "ci-gate-build": ci_gate(),
        "ci-gate-packaged": ci_gate("packaged"),
        "ci-reuse": ci_reuse(),
        "ci-validation": ci_validation(),
        "ci-execution": ci_execution(),
        "ci-root-request": ci_root_request(),
        "ci-root-request-runtime": ci_root_request("freeze-runtime-validation"),
        "expectation": expectation(),
        "handoff": handoff(),
        "compact": compact(),
        "compact-composed": compact(composed=True),
        "anchor": anchor(),
        "family-envelope": family_envelope(),
        "family-paired": family_paired(),
        "selection": selection(),
        "promotion": promotion(),
        "build": build_record(),
        "site": site_data(),
        "gallery": gallery_data(),
        "template-manifest": template_manifest(),
        "kit-stamp": kit_stamp(),
    }


#: Fixture name -> the document kind it holds.
VALID_FIXTURE_KINDS = {
    "ci-plan": "mod-base.build.plan",
    "ci-config": "mod-base.build.config",
    "ci-activation": "mod-base.ci.activation",
    "ci-batch": "mod-base.ci.batch",
    "ci-runtime-envelope": "mod-base.ci.runtime-envelope",
    "ci-envelope": "mod-base.build.envelope",
    "ci-selection": "mod-base.ci.selection",
    "ci-gate-build": "mod-base.ci.gate",
    "ci-gate-packaged": "mod-base.ci.gate",
    "ci-reuse": "mod-base.ci.reuse",
    "ci-validation": "mod-base.ci.validation",
    "ci-execution": "mod-base.ci.execution",
    "ci-root-request": "mod-base.ci.root-request",
    "ci-root-request-runtime": "mod-base.ci.root-request",
    "expectation": "mod-base.evidence.expectation",
    "handoff": "mod-base.evidence.handoff",
    "compact": "mod-base.evidence.compact",
    "compact-composed": "mod-base.evidence.compact",
    "anchor": "mod-base.evidence.anchor",
    "family-envelope": "mod-base.family.envelope",
    "family-paired": "mod-base.family.paired",
    "selection": "mod-base.selection",
    "promotion": "mod-base.promotion",
    "build": "mod-base.build",
    "site": "mod-base.site",
    "gallery": "mod-base.gallery",
    "template-manifest": "mod-base.template-manifest",
    "kit-stamp": "mod-base.kit-stamp",
}


def pretty_json(value: Any) -> bytes:
    """Readable, deterministic fixture bytes (sorted keys, two-space indent, trailing newline)."""

    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def write_document_fixtures(root: Path = DOCUMENT_FIXTURES / "valid") -> None:
    """Regenerate ``tests/fixtures/documents/valid/*.json`` from :func:`sample_documents`."""

    root.mkdir(parents=True, exist_ok=True)
    for name, document in sample_documents().items():
        (root / f"{name}.json").write_bytes(pretty_json(document))
