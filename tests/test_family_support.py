"""Shared builders for the family tests (MB4); this module holds no test case.

A family fixture mod is the ``qs_like`` fixture (``tests.fixtures.mods``) materialized as a fresh
Git repository whose ``mod-compatibility`` family uses a small 160x90 derivative box and whose
adapter gains :data:`FAMILY_HOOK`, a ``family_validate`` over a tiny synthetic native format: the
native ``manifest.json`` carries the ready-made projection (``projection``), its ``coverage_sha``
and knobs that make the hook return ``superseded``/``unavailable`` (``outcome``), carry forward
(``carry``, ``carried_from``), write elsewhere (``projection_path``), leave extra files
(``extra_files``) or unsafe entries (``specials``: a ``symlink``, ``hardlink`` or ``fifo`` at a path of
its output) or overwrite files of the bundle it validates (``tamper``). Images are real: pattern
PNG sources and their deterministic WebP derivatives.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mod_base.imaging.metrics import SizePolicy, inspect_png, inspect_webp
from mod_base.imaging.png import pattern_png
from mod_base.imaging.webp import derive_webp
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.documents import run_claim_from_environment, thumbnail_size
from mod_base.runtime import Invocation
from tests.fixtures.mods import support

FAMILY = "mod-compatibility"
KEY = "mc1.20.1"
PRODUCER_WORKFLOW = ".github/workflows/mod-compatibility-review.yml"
PRODUCER_RUN_ID = 202
BOX = (160, 90)
SOURCE_SIZE = (192, 108)
IMAGE_POLICY = {"derivative_box": list(BOX), "webp_quality": 80, "webp_method": 6}
NATIVE_KIND = "test-family-native"

FAMILY_HOOK = '''

# -- family_validate (appended by tests/test_family_support.py) --------------------------------------
import os as _family_os
import shutil as _family_shutil


def family_validate(ctx, family, key, bundle_dir, expected_coverage_sha, output_dir):
    root = Path(bundle_dir)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    outcome = manifest.get("outcome", "available")
    if outcome != "available":
        return {"status": outcome, "reason": "fixture " + outcome}
    result = {"status": "available", "reason": "fixture projection",
              "projection_path": manifest.get("projection_path", "paired.json")}
    if manifest["coverage_sha"] != expected_coverage_sha:
        if not manifest.get("carry", False):
            return {"status": "unavailable", "reason": "fixture lineage refusal"}
        result["carried_from"] = manifest.get("carried_from", manifest["coverage_sha"])
    elif "carried_from" in manifest:
        result["carried_from"] = manifest["carried_from"]
    projection = dict(manifest["projection"], coverage_sha=expected_coverage_sha)
    target = Path(output_dir).joinpath(*result["projection_path"].split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(projection, indent=2), encoding="utf-8")
    if (root / "images").is_dir():
        (target.parent / "images").mkdir(exist_ok=True)
        for image in sorted((root / "images").iterdir()):
            _family_shutil.copyfile(image, target.parent / "images" / image.name)
    for relative, text in manifest.get("extra_files", {}).items():
        extra = Path(output_dir) / relative
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text(text, encoding="utf-8")
    for special in manifest.get("specials", []):
        entry = Path(output_dir).joinpath(*special["path"].split("/"))
        entry.parent.mkdir(parents=True, exist_ok=True)
        if special["kind"] == "symlink":
            entry.symlink_to(target)
        elif special["kind"] == "hardlink":
            _family_os.link(target, entry)
        else:
            _family_os.mkfifo(entry)
    for relative in manifest.get("tamper", []):
        (root / relative).write_bytes(b"tampered by the hook")
    return result
'''


def _configure(root: Path, *, carry_forward: bool, handoff_max_bytes: int, hook: bool) -> None:
    config_path = root / "site" / "mod-base.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for family in config["families"]:
        family.update(image_policy=dict(IMAGE_POLICY), carry_forward=carry_forward,
                      handoff_max_bytes=handoff_max_bytes)
    config_path.write_bytes(canonical_json(config))
    if hook:
        adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
        adapter.write_text(adapter.read_text(encoding="utf-8") + FAMILY_HOOK, encoding="utf-8")


def family_mod(destination: Path, *, carry_forward: bool = True, handoff_max_bytes: int = 64 * 1024 * 1024,
               hook: bool = True) -> support.FixtureMod:
    """Materialize the family fixture mod in the new directory ``destination``."""

    return support.materialize("qs_like", destination, mutate=lambda root: _configure(
        root, carry_forward=carry_forward, handoff_max_bytes=handoff_max_bytes, hook=hook))


def advance(mod: support.FixtureMod, name: str, *, branch: str | None = None,
            start: str | None = None) -> support.FixtureMod:
    """Commit one new file on ``branch`` (default: the mod's branch, from its current head; a new
    branch forks from ``start``) and return the new head; the mod's branch stays checked out."""

    if branch is not None:
        support.git(mod.root, "checkout", "-q", "-b", branch, start or mod.commit)
    (mod.root / name).write_text(name + "\n", encoding="utf-8")
    support.git(mod.root, "add", name)
    support.git(mod.root, "commit", "-q", "-m", f"add {name}")
    head = support.FixtureMod(name=mod.name, root=mod.root, commit=support.git(mod.root, "rev-parse", "HEAD"),
                              tree=support.git(mod.root, "rev-parse", "HEAD^{tree}"), branch=branch or mod.branch,
                              repository=mod.repository)
    if branch is not None:
        support.git(mod.root, "checkout", "-q", mod.branch)
    return head


def producer_environment(mod: support.FixtureMod, *, sha: str | None = None) -> dict[str, str]:
    """The environment of the producer job (``mod-compatibility-review.yml``)."""

    return support.environment(mod, run_id=PRODUCER_RUN_ID, event="repository_dispatch", sha=sha,
                               job="publish-evidence", workflow=PRODUCER_WORKFLOW)


def family_environment(mod: support.FixtureMod) -> dict[str, str]:
    """The environment of the Pages ``family`` callee job (no token: ``family_validate`` is local)."""

    return support.pages_environment(mod, job="family", token=None)


def producer_claim(mod: support.FixtureMod, *, sha: str | None = None) -> dict[str, Any]:
    return run_claim_from_environment(producer_environment(mod, sha=sha))


def producer_invocation(mod: support.FixtureMod, *, sha: str | None = None) -> Invocation:
    return support.invocation(mod, producer_environment(mod, sha=sha))


def family_invocation(mod: support.FixtureMod) -> Invocation:
    return support.invocation(mod, family_environment(mod))


def _side(seed: int) -> tuple[dict[str, Any], bytes]:
    """One pair side: the image record and the WebP bytes of a pattern source."""

    png = pattern_png(*SOURCE_SIZE, seed)
    webp = derive_webp(png, box=BOX, quality=IMAGE_POLICY["webp_quality"], method=IMAGE_POLICY["webp_method"])
    width, height = thumbnail_size(SOURCE_SIZE, BOX)
    digest = sha256_hex(webp)
    record = {
        "image": {"path": f"images/{digest}.webp", "sha256": digest, "size": len(webp), "width": width,
                  "height": height, "format": "webp", "pixel": inspect_webp(webp, SizePolicy.exact(width, height))},
        "source": {"sha256": sha256_hex(png), "width": SOURCE_SIZE[0], "height": SOURCE_SIZE[1],
                   "pixel": inspect_png(png, SizePolicy.exact(*SOURCE_SIZE))},
    }
    return record, webp


def projection(*, subject: Mapping[str, str], producer: Mapping[str, Any], coverage_sha: str,
               seeds: tuple[tuple[int, int], ...] = ((1, 2),)) -> tuple[dict[str, Any], dict[str, bytes]]:
    """A valid ``mod-base.family.paired`` projection with one lane of ``len(seeds)`` pairs
    (``(reference seed, candidate seed)`` each) and its images ``{relative path: bytes}``."""

    images: dict[str, bytes] = {}
    pairs = []
    for position, (reference_seed, candidate_seed) in enumerate(seeds):
        reference, reference_bytes = _side(reference_seed)
        candidate, candidate_bytes = _side(candidate_seed)
        images[reference["image"]["path"]] = reference_bytes
        images[candidate["image"]["path"]] = candidate_bytes
        pairs.append({
            "pair_id": f"mod-compatibility.client_a.integration_visual_{position}",
            "capture_id": f"mod-compatibility.client_a.integration_visual_{position}",
            "reference_capture_id": "full.client_a.skin_apply",
            "title": "Ears features",
            "expectation": "The Ears features render beside the plaid skin.",
            "runtime_evidence": "PASS integration_visual: ears active on the local player",
            "verdict": {"runtime_passed": True, "semantic_valid": True, "matches_reference": None, "defect": False},
            "metrics": {"semantic_changed_fraction": 0.0412, "perceptual_delta": 3.25,
                        "candidate_semantic_sha256": "c" * 64, "reference_semantic_sha256": "d" * 64},
            "reference": reference,
            "candidate": candidate,
        })
    document = {
        "kind": "mod-base.family.paired",
        "schema_version": 1,
        "family": FAMILY,
        "key": KEY,
        "coverage_sha": coverage_sha,
        "subject": dict(subject),
        "status": "available",
        "provenance": {"producer": support.run_record(producer, event="repository_dispatch"),
                       "links": [{"label": "Compatibility runtime", "run_id": 303}]},
        "contracts": {"mod-compatibility-contract": "a" * 64, "scenario-contract": "b" * 64},
        "image_policy": dict(IMAGE_POLICY),
        "lanes": [{
            "lane_id": "1.20.1-fabric/ears",
            "artifact_node": "1.20.1-fabric",
            "minecraft": "1.20.1",
            "loader": "fabric",
            "variant": {"id": "ears", "name": "Ears", "version": "1.4.6", "version_id": "Ab12Cd34"},
            "review": {"reviewed_frame_count": 2, "manifest_sha256": "e" * 64, "proof_sha256": "f" * 64,
                       "report_sha256": "0" * 64},
            "pairs": pairs,
        }],
        "not_applicable": [{"artifact_node": "1.20.1-forge", "minecraft": "1.20.1", "loader": "forge",
                            "variant_id": "ears", "variant_name": "Ears",
                            "reason": "Ears publishes no Forge build for Minecraft 1.20.1."}],
    }
    return document, images


def write_tree(root: Path, files: Mapping[str, bytes]) -> Path:
    for relative, data in files.items():
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def native_bundle(root: Path, mod: support.FixtureMod, *, coverage_sha: str | None = None,
                  adjust: Callable[[dict[str, Any]], None] | None = None,
                  projection_adjust: Callable[[dict[str, Any]], None] | None = None,
                  images: Mapping[str, bytes] | None = None, extra: Mapping[str, bytes] | None = None) -> Path:
    """Write the synthetic native bundle of ``mod``'s producer run into the new ``root``: its
    manifest carries a valid projection (subject and producer of the envelope ``family envelope``
    will write) and the projection's images. ``adjust`` edits the manifest, ``projection_adjust``
    the embedded projection; ``images`` replaces the image files; ``extra`` adds native files."""

    coverage = coverage_sha or mod.commit
    document, written = projection(subject=mod.subject, producer=producer_claim(mod), coverage_sha=coverage)
    if projection_adjust is not None:
        projection_adjust(document)
    manifest: dict[str, Any] = {"kind": NATIVE_KIND, "schema_version": 1, "coverage_sha": coverage,
                                "projection": document}
    if adjust is not None:
        adjust(manifest)
    files = dict(written if images is None else images)
    files["manifest.json"] = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    files.update(extra or {})
    root.mkdir(parents=True)
    return write_tree(root, files)


def rewrite_envelope(root: Path, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Rewrite ``root/envelope.json`` canonically after ``change`` (a structurally valid forgery)."""

    path = root / "envelope.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    change(envelope)
    os.chmod(path, 0o644)
    path.write_bytes(canonical_json(envelope))
    return envelope


def copy_tree(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    return destination
