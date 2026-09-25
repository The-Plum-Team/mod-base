"""Handoff -> compact bundle (MB3), with the re-encode binding (BP ``authenticate_source._bind_compact``).

``compact_bundle`` is collect step 5 (see ``docs/SCHEMAS.md``, "The collect flow"). Deterministic:
``images/<sha>.webp`` derivatives via ``imaging.webp.derive_webp`` at the expectation's image
policy, derivative PixelMetrics and CompareMetrics measured by the kit, ``files`` listing exactly
the shipped files. The input is a ``complete`` handoff or a cache (re-validated and re-emitted); a
``selected`` handoff is refused here (it must go through ``compose``, R3). ``selection_path`` is
the selection **draft** written by ``authenticate``; this step completes it (``binding``:
``reencode-identical`` for a handoff, ``cache-revalidated`` for a cache, with the frame and
distinct-derivative counts; ``manifest_sha256`` = ``documents.compact_identity_sha256`` of the
written manifest; a composed cache keeps its ``composition``) and embeds the final selection as
``selection.json``, so ``documents.check_compact_selection`` holds. The written manifest's ``kit``
is the executing kit (the same ``KitRef`` as the selection).

The draft must describe exactly the input: its ``source_manifest_sha256`` is the input's
``manifest.json`` hash, and the subject, coverage, expectation hash, run claims, reuse and verified
extension names (R6: every extension the input carries was verified) all agree. A handoff input
is re-validated completely (inventory, every PNG, R1 ``collect`` on its ``runtime/``) and its
expectation re-derived with the authenticated run records (R2: the handoff run's projection, and
the tested run's own when its event differs); a cache is re-validated as a published compact
bundle and re-derived the same way. This runs in the Pages ``collect`` job,
whose hooks are all permitted there.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.evidence._common import (
    EXPECTATION,
    EXTENSIONS,
    MANIFEST,
    SELECTION,
    derivative_policy,
    fail,
    read_json_argument,
)
from mod_base.evidence.expectation import require_rederived_for_runs, target_for_key
from mod_base.evidence.validate import (
    Bundle,
    check_compact_pixels,
    load_compact,
    read_derivative,
    read_source_png,
    validate_compact_dir,
    verify_handoff,
)
from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import inspect_webp
from mod_base.imaging.webp import derive_webp
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.documents import (
    RUN_CLAIM_FIELDS,
    check_compact_selection,
    compact_identity_sha256,
    validate_compact,
    validate_selection,
)
from mod_base.runtime import Invocation

OWNER = "MB3"
KIND = "mod-base.evidence.compact"
_SOURCE_ARTIFACT_FIELDS = ("kind", "id", "name", "digest", "size", "run_id", "run_attempt")
#: A placeholder ``selection.json`` record: the manifest identity excludes it, so the identity can
#: be computed before the final selection (which embeds that identity) exists.
PLACEHOLDER = {"path": SELECTION, "sha256": "0" * 64, "size": 1}


def read_draft(invocation: Invocation, path: Path, *, key: str) -> dict[str, Any]:
    """The selection draft written by ``authenticate`` for ``key`` in this Pages run."""

    draft, _ = read_json_argument(path, max_bytes=lim.MAX_SELECTION_BYTES, label="selection draft")
    validate_selection(draft, draft=True)
    if draft["key"] != grammar.require_key(key):
        raise fail(f"the selection draft belongs to key {draft['key']!r}, not {key!r}")
    if draft["repository"] != invocation.repository:
        raise fail("the selection draft names another repository than this run")
    if draft["kit"] != invocation.kit:
        raise fail("the selection draft was written by another kit than the executing one")
    return draft


def bind_draft(draft: Mapping[str, Any], bundle: Bundle, *, kind: str) -> None:
    """The draft describes exactly the selected bundle (see the module docstring)."""

    manifest = bundle.manifest
    source = draft["source"]
    checks = (
        ("selected artifact kind", draft["selected_artifact"]["kind"] == kind),
        ("source manifest hash", draft["source_manifest_sha256"] == sha256_hex(bundle.manifest_raw)),
        ("expectation hash", draft["expectation_sha256"] == manifest["expectation"]["sha256"]),
        ("subject", draft["subject"] == manifest["subject"]),
        ("coverage", draft["coverage_sha"] == manifest["provenance"]["coverage_sha"]),
        ("handoff run", {field: source["handoff_run"][field] for field in RUN_CLAIM_FIELDS}
         == manifest["provenance"]["handoff"]),
        ("tested run", {field: source["tested_run"][field] for field in RUN_CLAIM_FIELDS}
         == manifest["provenance"]["tested"]),
        ("reuse", source["reuse"] == manifest["provenance"]["reuse"]),
        ("verified extensions (R6)", draft["extensions_verified"]
         == (manifest["extensions"]["names"] if manifest["extensions"] is not None else [])),
    )
    for label, passed in checks:
        if not passed:
            raise fail(f"the selection draft's {label} does not describe the selected bundle", reason="selection-binding")


def rederive(invocation: Invocation, bundle: Bundle, draft: Mapping[str, Any]) -> dict[str, Any]:
    """R2 with the authenticated run records of the draft: the handoff run's projection, and the
    tested run's own when it was started by another event
    (:func:`mod_base.evidence.expectation.require_rederived_for_runs`)."""

    manifest = bundle.manifest
    target = target_for_key(invocation, manifest["key"], subject=manifest["subject"])
    source = draft["source"]
    return require_rederived_for_runs(invocation, bundle.expectation_raw, target=target,
                                      tested=manifest["provenance"]["tested"],
                                      handoff_event=source["handoff_run"]["event"],
                                      tested_event=source["tested_run"]["event"], extensions=bundle.extensions)


def compact_images(bundle: Bundle) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, bytes]]:
    """The compact frames, comparisons and ``images/<sha>.webp`` bytes of a validated handoff."""

    policy = bundle.expectation["image_policy"]
    box = policy["derivative_box"]
    images: dict[str, bytes] = {}
    derived: dict[str, dict[str, Any]] = {}
    frames = []
    total = len(bundle.expectation_raw)
    for frame in bundle.manifest["frames"]:
        source = frame["source"]
        if source["sha256"] not in derived:
            encoded = derive_webp(read_source_png(bundle.root, source), box=box, quality=policy["webp_quality"],
                                  method=policy["webp_method"])
            total += len(encoded)
            if total > lim.MAX_COMPACT_BUNDLE_BYTES:
                raise fail("the derivatives exceed the compact bundle bound")
            digest = sha256_hex(encoded)
            pixel = inspect_webp(encoded, derivative_policy(source["width"], source["height"], box))
            path = f"images/{digest}.webp"
            images.setdefault(path, encoded)
            derived[source["sha256"]] = {"path": path, "sha256": digest, "size": len(encoded),
                                         "width": pixel["width"], "height": pixel["height"], "format": "webp",
                                         "pixel": pixel}
        record = {name: value for name, value in frame.items() if name != "source"}
        record["source"] = {name: value for name, value in source.items() if name != "path"}
        record["derivative"] = derived[source["sha256"]]
        frames.append(record)
    by_id = {frame["frame_id"]: frame for frame in frames}
    comparisons = []
    for comparison in bundle.manifest["comparisons"]:
        first = images[by_id[comparison["first_frame_id"]]["derivative"]["path"]]
        second = images[by_id[comparison["second_frame_id"]]["derivative"]["path"]]
        metrics = compare(first, second, minimum_changed_fraction=comparison["minimum_changed_fraction"],
                          region=comparison.get("region"))
        comparisons.append({**comparison, "derivative": metrics})
    return frames, comparisons, images


def file_record(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": sha256_hex(data), "size": len(data)}


def compact_manifest(invocation: Invocation, handoff: Mapping[str, Any], *, source_artifact: Mapping[str, Any],
                     scope: Mapping[str, Any], frames: list[dict[str, Any]], comparisons: list[dict[str, Any]],
                     files: list[dict[str, Any]]) -> dict[str, Any]:
    """A compact manifest over ``handoff``'s header with a placeholder ``selection`` record."""

    return {
        "kind": KIND,
        "schema_version": 1,
        "repository": handoff["repository"],
        "key": handoff["key"],
        "kit": invocation.kit,
        "subject": handoff["subject"],
        "provenance": handoff["provenance"],
        "expectation": handoff["expectation"],
        "matrix_sha256": handoff["matrix_sha256"],
        "contract_sha256": handoff["contract_sha256"],
        "scope": dict(scope),
        "extensions": handoff["extensions"],
        "source_artifact": {field: source_artifact[field] for field in _SOURCE_ARTIFACT_FIELDS},
        "selection": dict(PLACEHOLDER),
        "lanes": handoff["lanes"],
        "frames": frames,
        "comparisons": comparisons,
        "files": sorted([*files, dict(PLACEHOLDER)], key=lambda record: record["path"]),
    }


def finalize(manifest: dict[str, Any], draft: Mapping[str, Any], *, mode: str,
             composition: Mapping[str, Any] | None = None,
             extensions_verified: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Complete the draft for ``manifest`` (which carries the placeholder selection record) and
    return ``(manifest, final selection, selection bytes)`` with the real record in place.

    The final selection always names the expectation and the verified extensions of the bundle it
    is embedded in (``check_compact_selection``). Only a composition may change them: the draft
    describes the selected handoff, while the composed bundle embeds the complete expectation and
    the extensions the compose hook emitted (verified again, R6)."""

    if composition is None and (extensions_verified is not None
                                or draft["expectation_sha256"] != manifest["expectation"]["sha256"]):
        raise fail("only a composition may name another expectation or extension set than its draft")
    identity = compact_identity_sha256(manifest)
    selection = dict(draft)
    selection["expectation_sha256"] = manifest["expectation"]["sha256"]
    if extensions_verified is not None:
        selection["extensions_verified"] = list(extensions_verified)
    selection["manifest_sha256"] = identity
    selection["binding"] = {"mode": mode, "frames": len(manifest["frames"]),
                            "derivatives": len({frame["derivative"]["path"] for frame in manifest["frames"]})}
    if composition is not None:
        selection["composition"] = dict(composition)
    validate_selection(selection, draft=False)
    data = canonical_json(selection)
    record = file_record(SELECTION, data)
    manifest = {**manifest, "selection": record,
                "files": sorted([item for item in manifest["files"] if item["path"] != SELECTION] + [record],
                                key=lambda item: item["path"])}
    if compact_identity_sha256(manifest) != identity:
        raise fail("the compact manifest identity changed while embedding its selection")
    check_compact_selection(manifest, selection)
    return manifest, selection, data


def write_compact(invocation: Invocation, output: Path, manifest: Mapping[str, Any], expectation: Mapping[str, Any],
                  files: Mapping[str, bytes], *, key: str) -> dict[str, Any]:
    """Publish ``files`` plus ``manifest.json`` atomically into the new ``output`` and validate the
    written bundle in place (no hook runs)."""

    validate_compact(manifest, expectation=expectation, allowed_extensions=invocation.config.extension_names)
    encoded = canonical_json(manifest)
    if len(encoded) > lim.MAX_MANIFEST_BYTES:
        raise fail(f"the compact manifest exceeds {lim.MAX_MANIFEST_BYTES} bytes")

    def writer(stage: Path, descriptor: int) -> dict[str, Any]:
        for path in sorted(files):
            write_new(descriptor, path, files[path])
        write_new(descriptor, MANIFEST, encoded)
        return validate_compact_dir(invocation, stage, key=key)

    return atomic_directory(Path(output), writer)


def _compact_handoff(invocation: Invocation, *, key: str, input_dir: Path, draft: Mapping[str, Any],
                     output: Path) -> dict[str, Any]:
    bundle, _ = verify_handoff(invocation, input_dir, key=key, expected_subject_commit=draft["subject"]["commit"])
    if bundle.manifest["scope"]["kind"] != "complete":
        raise fail("a selected handoff must be composed with its baseline (compose), never compacted alone",
                   reason="selected-needs-compose")
    bind_draft(draft, bundle, kind="handoff")
    rederive(invocation, bundle, draft)
    frames, comparisons, images = compact_images(bundle)
    files = {EXPECTATION: bundle.expectation_raw, **images}
    if bundle.manifest["extensions"] is not None:
        files[EXTENSIONS] = canonical_json(bundle.extensions)
    scope = {"kind": "complete"}
    if "detail_sha256" in bundle.manifest["scope"]:
        scope["detail_sha256"] = bundle.manifest["scope"]["detail_sha256"]
    manifest = compact_manifest(invocation, bundle.manifest, source_artifact=draft["selected_artifact"], scope=scope,
                                frames=frames, comparisons=comparisons,
                                files=[file_record(path, data) for path, data in files.items()])
    manifest, _, selection_data = finalize(manifest, draft, mode="reencode-identical")
    return write_compact(invocation, output, manifest, bundle.expectation, {**files, SELECTION: selection_data},
                         key=key)


def _reemit_cache(invocation: Invocation, *, key: str, input_dir: Path, draft: Mapping[str, Any],
                  output: Path) -> dict[str, Any]:
    bundle = load_compact(invocation, input_dir, key=key, expected_subject_commit=draft["subject"]["commit"])
    check_compact_pixels(bundle)
    bind_draft(draft, bundle, kind="cache")
    rederive(invocation, bundle, draft)
    old = bundle.manifest
    files = {EXPECTATION: bundle.expectation_raw}
    if old["extensions"] is not None:
        files[EXTENSIONS] = canonical_json(bundle.extensions)
    for frame in old["frames"]:
        files.setdefault(frame["derivative"]["path"], read_derivative(bundle.root, frame["derivative"]))
    manifest = {**old, "kit": invocation.kit,
                "source_artifact": {field: draft["selected_artifact"][field] for field in _SOURCE_ARTIFACT_FIELDS},
                "selection": dict(PLACEHOLDER),
                "files": sorted([file_record(path, data) for path, data in files.items()] + [dict(PLACEHOLDER)],
                                key=lambda record: record["path"])}
    manifest, _, selection_data = finalize(manifest, draft, mode="cache-revalidated",
                                           composition=(bundle.selection or {}).get("composition"))
    return write_compact(invocation, output, manifest, bundle.expectation, {**files, SELECTION: selection_data},
                         key=key)


def compact_bundle(invocation: Invocation, *, key: str, input_dir: Path, selection_path: Path,
                   output: Path) -> dict[str, Any]:
    """Write the compact bundle of ``input_dir`` (a complete handoff or a cache) into the new
    ``output`` with the completed selection embedded (see module docstring) and return its
    validated manifest."""

    draft = read_draft(invocation, selection_path, key=key)
    if draft["selected_artifact"]["kind"] == "handoff":
        return _compact_handoff(invocation, key=key, input_dir=input_dir, draft=draft, output=output)
    return _reemit_cache(invocation, key=key, input_dir=input_dir, draft=draft, output=output)
