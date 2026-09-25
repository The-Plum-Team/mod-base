"""The lossless anchor (MB3): Block Pops ``scripts/pages/visual_anchor.py`` generalized.

Eligibility (:func:`eligible_nodes`): the anchor is enabled, the handoff was a direct canonical run
of the subject (SPEC §3.4 "BP's rule": ``handoff.controller_sha == subject.commit`` and
``handoff.branch == subject.branch``, the handoff run being its own controller), its pixels were
not re-published from another run by attestation (``reuse != "attested"``: Block Pops never cuts an
anchor from an ``attest_run_id`` run), and the adapter's ``anchor_selection`` chose nodes. An
adapter without ``anchor_selection`` declines every anchor (ADAPTER.md: "absent means no
anchor"). ``reuse: "none"`` binds the tested run to the subject itself (the documents' same-run
rule); ``delegated`` reuse stays eligible, as Quick Skin's retained reference handoff of a reused
identical-tree generation is today, and the anchor records the tested run its pixels came from. The anchor verbs run
in the ``prepare-evidence`` composite, so their hooks see the subject commit as
``ctx.implementation_sha``.

``create`` cuts the reference lanes out of an uploaded handoff (bound to its artifact id, name and
digest) and re-encodes every PNG canonically to ``images/<sha>.png``. Block Pops
``scripts/visual/curate.py`` imports :func:`artifact_name` and :func:`validate_anchor_dir` from
here.

Anchor rotation (``config.anchor.successor_grace_days``: Quick Skin 0, Block Pops 8) is
rotation's job (``mod_base.pages.rotate``); an anchor records only what it was cut from. Its
``kit`` is the handoff's (the same producing run cuts both). Validation needs no config: the
embedded expectation supplies the exact source size, every image must already be its canonical
re-encoding, its recomputed PixelMetrics must equal the record, and the direct-run shape is
re-checked (a tested run that is the handoff run tested exactly the subject).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.adapter.protocol import HookUnsupported
from mod_base.errors import MbError
from mod_base.evidence._common import (
    EXPECTATION,
    MANIFEST,
    fail,
    producer_invocation,
    read_document,
    real_directory,
    require_inventory,
    source_policy,
)
from mod_base.evidence.validate import check_handoff_pixels, load_handoff, read_source_png
from mod_base.imaging.metrics import inspect_png
from mod_base.imaging.png import canonical_png
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import read_child_file
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.documents import FRAME_FIELDS, validate_anchor, validate_expectation
from mod_base.runtime import Invocation

OWNER = "MB3"
KIND = "mod-base.evidence.anchor"


@dataclass(frozen=True)
class AnchorIdentity:
    eligible: bool
    name: str | None
    artifact_nodes: tuple[str, ...]


def artifact_name(key: str, commit: str, run_id: int, run_attempt: int) -> str:
    """``mb-anchor--{key}--{commit}--{run_id}--a{attempt}`` (delegates to the grammar)."""

    return grammar.anchor_name(key, commit, run_id, run_attempt)


def eligible_nodes(invocation: Invocation, manifest: Mapping[str, Any], expectation: Mapping[str, Any]) -> tuple[str, ...]:
    """The anchor nodes of a validated handoff, or ``()`` (see the module docstring): the anchor is
    enabled, the handoff was a direct canonical run of the subject that is not an attestation, and
    ``anchor_selection`` chose nodes (which the protocol requires to equal ``expectation.anchor``)."""

    if not invocation.config.anchor["enabled"]:
        return ()
    provenance, subject = manifest["provenance"], manifest["subject"]
    handoff = provenance["handoff"]
    if (provenance["reuse"] == "attested" or handoff["controller_sha"] != subject["commit"]
            or handoff["branch"] != subject["branch"]):
        return ()
    try:
        selection = host.call(producer_invocation(invocation, subject["commit"]), "anchor_selection",
                              {"expectation": dict(expectation)})
    except HookUnsupported:
        return ()
    return () if selection is None else tuple(selection["artifact_nodes"])


def anchor_identity(invocation: Invocation, handoff_dir: Path, *, key: str) -> AnchorIdentity:
    """Decide eligibility for a validated handoff directory and name the anchor it would produce."""

    bundle = load_handoff(invocation, handoff_dir, key=key)
    nodes = eligible_nodes(invocation, bundle.manifest, bundle.expectation)
    if not nodes:
        return AnchorIdentity(eligible=False, name=None, artifact_nodes=())
    claim = bundle.manifest["provenance"]["handoff"]
    name = artifact_name(key, bundle.manifest["subject"]["commit"], claim["run_id"], claim["run_attempt"])
    return AnchorIdentity(eligible=True, name=name, artifact_nodes=nodes)


def create_anchor(invocation: Invocation, *, key: str, handoff_dir: Path, raw_artifact_id: int,
                  raw_artifact_name: str, raw_artifact_digest: str, output: Path) -> dict[str, Any]:
    """Write the anchor bundle into the new ``output`` and return its validated manifest."""

    grammar.require_positive_int(raw_artifact_id, "raw artifact id", maximum=lim.MAX_RUN_ID)
    grammar.require(grammar.DIGEST, raw_artifact_digest, "raw artifact digest")
    bundle = load_handoff(invocation, handoff_dir, key=key)
    manifest, expectation = bundle.manifest, bundle.expectation
    claim = manifest["provenance"]["handoff"]
    if raw_artifact_name != grammar.handoff_name(key, claim["run_attempt"]):
        raise fail("the raw artifact name is not this key's handoff of the producing attempt")
    nodes = eligible_nodes(invocation, manifest, expectation)
    if not nodes:
        raise MbError("this handoff is not an eligible anchor source (direct canonical run and adapter selection)",
                      reason="anchor-ineligible")
    check_handoff_pixels(bundle)
    policy = source_policy(expectation)
    lanes = [lane for lane in manifest["lanes"] if lane["artifact_node"] in nodes]
    images: dict[str, bytes] = {}
    frames = []
    total = len(bundle.expectation_raw)
    for frame in manifest["frames"]:
        if frame["artifact_node"] not in nodes:
            continue
        canonical = canonical_png(read_source_png(bundle.root, frame["source"]), policy=policy)
        total += len(canonical)
        if total > lim.MAX_ANCHOR_BUNDLE_BYTES:
            raise fail("the canonical anchor images exceed the anchor bundle bound")
        pixel = inspect_png(canonical, policy)
        if pixel["pixel_sha256"] != frame["source"]["pixel"]["pixel_sha256"]:
            raise fail(f"the canonical re-encoding of {frame['frame_id']} changed its pixels")
        digest = sha256_hex(canonical)
        path = f"images/{digest}.png"
        images.setdefault(path, canonical)
        frames.append({**{field: frame[field] for field in FRAME_FIELDS},
                       "source": {"path": path, "sha256": digest, "pixel_sha256": pixel["pixel_sha256"],
                                  "size": len(canonical), "width": pixel["width"], "height": pixel["height"],
                                  "format": "png", "pixel": pixel}})
    files = [{"path": EXPECTATION, "sha256": sha256_hex(bundle.expectation_raw), "size": len(bundle.expectation_raw)}]
    files.extend({"path": path, "sha256": sha256_hex(data), "size": len(data)} for path, data in images.items())
    source_artifact = {"id": raw_artifact_id, "name": raw_artifact_name, "digest": raw_artifact_digest,
                       "run_id": claim["run_id"], "run_attempt": claim["run_attempt"]}
    document = {
        "kind": KIND,
        "schema_version": 1,
        "repository": manifest["repository"],
        "key": key,
        "kit": manifest["kit"],
        "subject": manifest["subject"],
        "provenance": {"handoff": claim, "tested": manifest["provenance"]["tested"]},
        "reference": {"artifact_nodes": sorted({lane["artifact_node"] for lane in lanes}),
                      "minecraft": sorted({lane["minecraft"] for lane in lanes}),
                      "loaders": sorted({lane["loader"] for lane in lanes})},
        "source_artifact": source_artifact,
        "expectation": manifest["expectation"],
        "lanes": lanes,
        "frames": frames,
        "files": sorted(files, key=lambda record: record["path"]),
    }
    validate_anchor(document, expectation=expectation)

    def writer(stage: Path, descriptor: int) -> dict[str, Any]:
        write_new(descriptor, EXPECTATION, bundle.expectation_raw)
        for path, data in images.items():
            write_new(descriptor, path, data)
        write_new(descriptor, MANIFEST, canonical_json(document))
        return validate_anchor_dir(stage, key=key, expected_subject_commit=manifest["subject"]["commit"],
                                   raw_artifact_id=raw_artifact_id, raw_artifact_name=raw_artifact_name,
                                   raw_artifact_digest=raw_artifact_digest)

    return atomic_directory(Path(output), writer)


def _require_direct_run(manifest: Mapping[str, Any]) -> None:
    """The anchor's handoff run ran on the subject branch, and a tested run that is that same run
    tested exactly the subject (the documents check ``controller_sha == subject.commit``)."""

    subject, handoff, tested = manifest["subject"], manifest["provenance"]["handoff"], manifest["provenance"]["tested"]
    if handoff["branch"] != subject["branch"]:
        raise fail("an anchor requires a handoff run on the subject branch", reason="anchor-source")
    same_run = (tested["run_id"], tested["run_attempt"], tested["workflow_path"]) == (
        handoff["run_id"], handoff["run_attempt"], handoff["workflow_path"])
    if same_run and (tested["branch"], tested["commit"], tested["controller_branch"], tested["controller_sha"]) != (
            subject["branch"], subject["commit"], handoff["branch"], handoff["commit"]):
        raise fail("the anchor's direct run did not test exactly its subject", reason="anchor-source")


def validate_anchor_dir(root: Path, *, key: str | None = None, expected_subject_commit: str | None = None,
                        raw_artifact_id: int | None = None, raw_artifact_name: str | None = None,
                        raw_artifact_digest: str | None = None) -> dict[str, Any]:
    """Validate an anchor directory (inventory, hashes, canonical PNG re-inspection, embedded
    expectation) and, when all three ``raw_artifact_*`` are given, its source artifact binding.
    Needs no config: BP curate calls it with the anchor alone."""

    raw = (raw_artifact_id, raw_artifact_name, raw_artifact_digest)
    if any(value is not None for value in raw) and not all(value is not None for value in raw):
        raise fail("raw_artifact_id, raw_artifact_name and raw_artifact_digest go together", reason="usage")
    root = real_directory(root, "anchor directory")
    manifest, _ = read_document(root, MANIFEST, max_bytes=lim.MAX_MANIFEST_BYTES, label=MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
        raise fail(f"{MANIFEST} is not a {KIND} document")
    expectation, _ = read_document(root, EXPECTATION, max_bytes=lim.MAX_EXPECTATION_BYTES, label=EXPECTATION)
    validate_expectation(expectation)
    validate_anchor(manifest, expectation=expectation)
    if key is not None and manifest["key"] != grammar.require_key(key):
        raise fail(f"the anchor belongs to key {manifest['key']!r}, not {key!r}")
    if expected_subject_commit is not None and manifest["subject"]["commit"] != grammar.require_sha1(
            expected_subject_commit, "expected subject commit"):
        raise fail("the anchor covers another subject commit than expected", reason="stale-subject")
    _require_direct_run(manifest)
    require_inventory(root, manifest["files"], max_files=lim.MAX_ANCHOR_FILES,
                      max_total_bytes=lim.MAX_ANCHOR_BUNDLE_BYTES, max_file_bytes=lim.MAX_SOURCE_PNG_BYTES,
                      label="anchor")
    policy = source_policy(expectation)
    checked: dict[str, dict[str, Any]] = {}
    for frame in manifest["frames"]:
        source = frame["source"]
        if source["path"] not in checked:
            data = read_child_file(root, source["path"], max_bytes=lim.MAX_SOURCE_PNG_BYTES)
            if len(data) != source["size"] or sha256_hex(data) != source["sha256"]:
                raise fail(f"{source['path']} changed after it was recorded")
            if canonical_png(data, policy=policy) != data:
                raise fail(f"{source['path']} is not a canonical metadata-free RGB PNG")
            checked[source["path"]] = inspect_png(data, policy)
        if checked[source["path"]] != source["pixel"]:
            raise fail(f"the recorded PixelMetrics of anchor frame {frame['frame_id']} differ from the kit's")
    if raw_artifact_id is not None:
        artifact = manifest["source_artifact"]
        if (artifact["id"], artifact["name"], artifact["digest"]) != raw:
            raise fail("the anchor was not cut from the named raw handoff artifact", reason="anchor-source")
    return manifest
