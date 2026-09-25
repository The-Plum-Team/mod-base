"""Handoff/compact validation in a fresh process (MB3), including BP ``validate_raw``.

Every validation re-reads the bundle from disk: exact directory inventory versus ``files``, every
hash and size, strict JSON, ``documents.validate_*`` with the embedded expectation, the reuse rules
that depend on the config (SPEC §3.2 rule 5: ``delegated`` needs the configured
``delegated_reuse_extension`` among the bundle's extensions, ``attested`` a configured
``attestation_job``), every image re-inspected with the kit (source PNGs against
``image_policy.source_size``, derivatives against ``thumbnail_size``), comparisons recomputed, and
for handoffs the adapter ``collect`` re-derivation (R1) plus, unless disabled, the expectation
re-derivation (R2).

Every JSON file of a kit bundle must be exactly its ``canonical_json`` bytes. The handoff
re-derivation needs the handoff run's event (:func:`mod_base.evidence.expectation.
tested_run_projection`), which only the producing run knows without the API: ``rederive=True``
therefore requires ``GITHUB_RUN_ID``/``GITHUB_RUN_ATTEMPT`` to name the handoff run itself (the
``prepare-evidence`` composite's fresh-process check, whose hooks see the subject commit as
``ctx.implementation_sha`` exactly as ``prepare``'s did). Pages jobs validate with
``rederive=False`` and re-derive with the authenticated run records (``compact`` and ``compose``
do).

A compact bundle is validated without any adapter hook (it runs in ``refresh``, where adapter code
is forbidden): structure, the embedded final selection and its binding
(``documents.check_compact_selection``), the inventory and every derivative and derivative
comparison. ``bind_raw`` additionally re-encodes every derivative that came from a raw handoff
(all frames of a ``complete`` bundle, the ``selected`` epoch of a ``composed`` one, which must be
exactly the raw handoff's frames) from that handoff's PNGs and requires byte-identical WebP (Block
Pops ``_bind_compact``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.evidence._common import (
    EXPECTATION,
    EXTENSIONS,
    MANIFEST,
    SELECTION,
    derivative_policy,
    fail,
    producer_invocation,
    read_document,
    real_directory,
    require_equal,
    require_inventory,
    source_policy,
)
from mod_base.evidence.expectation import (
    _check_extensions,
    require_rederived,
    target_for_key,
    tested_run_projection,
)
from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import inspect_png, inspect_webp
from mod_base.imaging.webp import derive_webp
from mod_base.io.tree import read_child_file
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import sha256_hex
from mod_base.model.documents import (
    check_compact_selection,
    validate_compact,
    validate_expectation,
    validate_handoff,
    validate_selection,
)
from mod_base.runtime import Invocation

OWNER = "MB3"
KINDS = ("handoff", "compact", "anchor", "family")

HANDOFF_KIND = "mod-base.evidence.handoff"
COMPACT_KIND = "mod-base.evidence.compact"
#: The largest file of a compact bundle (``expectation.json``; the manifest is smaller).
_COMPACT_FILE_BYTES = max(lim.MAX_EXPECTATION_BYTES, lim.MAX_MANIFEST_BYTES)


@dataclass(frozen=True)
class Bundle:
    """One bundle read strictly from ``root``: its documents and their exact bytes."""

    root: Path
    manifest: dict[str, Any]
    manifest_raw: bytes
    expectation: dict[str, Any]
    expectation_raw: bytes
    extensions: dict[str, dict[str, Any]]
    selection: dict[str, Any] | None = None
    selection_raw: bytes | None = None


def _manifest(root: Path, kind: str) -> tuple[dict[str, Any], bytes]:
    manifest, raw = read_document(root, MANIFEST, max_bytes=lim.MAX_MANIFEST_BYTES, label=MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("kind") != kind:
        raise fail(f"{MANIFEST} is not a {kind} document")
    return manifest, raw


def _expectation(root: Path, policy: Mapping[str, Any] | None) -> tuple[dict[str, Any], bytes]:
    expectation, raw = read_document(root, EXPECTATION, max_bytes=lim.MAX_EXPECTATION_BYTES, label=EXPECTATION)
    return validate_expectation(expectation, image_policy=policy), raw


def _extensions(invocation: Invocation | None, root: Path, manifest: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    record = manifest["extensions"]
    if record is None:
        return {}
    value, _ = read_document(root, EXTENSIONS, max_bytes=lim.MAX_EXTENSIONS_BYTES, label=EXTENSIONS)
    if invocation is not None:
        value = _check_extensions(invocation, value, label=EXTENSIONS)
    if sorted(value) != record["names"]:
        raise fail(f"{EXTENSIONS} names differ from the manifest's extension names")
    return value


def _header(invocation: Invocation, manifest: Mapping[str, Any], *, key: str,
            expected_subject_commit: str | None) -> None:
    grammar.require_key(key)
    if manifest["key"] != key:
        raise fail(f"the bundle belongs to key {manifest['key']!r}, not {key!r}")
    if manifest["repository"] != invocation.repository:
        raise fail("the bundle names another repository than this run")
    if expected_subject_commit is not None:
        grammar.require_sha1(expected_subject_commit, "expected subject commit")
        if manifest["subject"]["commit"] != expected_subject_commit:
            raise fail("the bundle covers another subject commit than expected", reason="stale-subject")


def _check_reuse(invocation: Invocation, manifest: Mapping[str, Any]) -> None:
    """SPEC §3.2 rule 5, the part that depends on the config (``documents`` checks the rest)."""

    reuse, source = manifest["provenance"]["reuse"], invocation.config.source
    names = manifest["extensions"]["names"] if manifest["extensions"] is not None else []
    if reuse == "delegated" and (source["delegated_reuse_extension"] is None
                                 or source["delegated_reuse_extension"] not in names):
        raise fail("delegated reuse needs the configured delegated_reuse_extension among the bundle's extensions",
                   reason="reuse")
    if reuse == "attested" and source["attestation_job"] is None:
        raise fail("attested reuse needs a configured source.attestation_job", reason="reuse")


# -- Handoffs ----------------------------------------------------------------------------------------


def load_handoff(invocation: Invocation, root: Path, *, key: str,
                 expected_subject_commit: str | None = None) -> Bundle:
    """Strict structure, canonical bytes and exact inventory of a handoff (no image decoding, no
    hook)."""

    root = real_directory(root, "handoff directory")
    manifest, manifest_raw = _manifest(root, HANDOFF_KIND)
    expectation, expectation_raw = _expectation(root, invocation.config.image_policy())
    validate_handoff(manifest, expectation=expectation, allowed_extensions=invocation.config.extension_names)
    _header(invocation, manifest, key=key, expected_subject_commit=expected_subject_commit)
    _check_reuse(invocation, manifest)
    require_inventory(root, manifest["files"], max_files=lim.MAX_HANDOFF_FILES, max_total_bytes=lim.MAX_RAW_BUNDLE_BYTES,
                      max_file_bytes=lim.MAX_SOURCE_PNG_BYTES, label="handoff")
    extensions = _extensions(invocation, root, manifest)
    return Bundle(root, manifest, manifest_raw, expectation, expectation_raw, extensions)


def read_source_png(root: Path, source: Mapping[str, Any]) -> bytes:
    """The bytes of one recorded source PNG, re-hashed against its record."""

    data = read_child_file(root, source["path"], max_bytes=lim.MAX_SOURCE_PNG_BYTES)
    if len(data) != source["size"] or sha256_hex(data) != source["sha256"]:
        raise fail(f"{source['path']} changed after it was recorded")
    return data


def check_handoff_pixels(bundle: Bundle) -> None:
    """Every source PNG's PixelMetrics and every source comparison, recomputed by the kit."""

    policy = source_policy(bundle.expectation)
    measured: dict[str, dict[str, Any]] = {}
    paths: dict[str, str] = {}
    for frame in bundle.manifest["frames"]:
        source = frame["source"]
        paths[frame["frame_id"]] = source["path"]
        if source["path"] not in measured:
            measured[source["path"]] = inspect_png(read_source_png(bundle.root, source), policy)
        if measured[source["path"]] != source["pixel"]:
            raise fail(f"the recorded PixelMetrics of {frame['frame_id']} differ from the kit's")
    frames = {frame["frame_id"]: frame for frame in bundle.manifest["frames"]}
    for comparison in bundle.manifest["comparisons"]:
        first = read_source_png(bundle.root, frames[comparison["first_frame_id"]]["source"])
        second = read_source_png(bundle.root, frames[comparison["second_frame_id"]]["source"])
        metrics = compare(first, second, minimum_changed_fraction=comparison["minimum_changed_fraction"],
                          region=comparison.get("region"))
        if metrics != comparison["source"]:
            raise fail(f"the recorded comparison {comparison['comparison_id']} differs from the kit's")


def target_from_expectation(expectation: Mapping[str, Any]) -> dict[str, Any]:
    """The ``targets`` entry an expectation was derived from (key, label, subject and hashes)."""

    return {field: expectation[field] for field in ("key", "label", "subject", "matrix_sha256", "contract_sha256")}


def collect_runtime(invocation: Invocation, bundle: Bundle, target: Mapping[str, Any]) -> dict[str, Any]:
    """The adapter ``collect`` result on the handoff's own ``runtime/`` tree."""

    runtime = real_directory(bundle.root / "runtime", "handoff runtime directory")
    return host.call(invocation, "collect", {"runtime_root": str(runtime), "target": dict(target),
                                            "expectation": bundle.expectation})


def check_collect(bundle: Bundle, result: Mapping[str, Any], *, cross_check: bool) -> None:
    """R1: the ``collect`` result names exactly the handoff's runtime files, lanes, frames and
    comparisons; with ``cross_check`` every frame's (mandatory) ``reported_pixel`` and every
    supplied (optional) comparison ``reported`` equal the kit's."""

    manifest = bundle.manifest
    runtime = sorted(record["path"] for record in manifest["files"] if record["path"].startswith("runtime/"))
    if ["runtime/" + name for name in result["runtime_files"]] != runtime:
        raise fail("R1: collect names other runtime files than the handoff ships", reason="collect-drift")
    for lane, observed in zip(manifest["lanes"], result["lanes"]):
        for field in ("lane_id", "java", "profile", "status", "jars"):
            if lane[field] != observed[field]:
                raise fail(f"R1: collect reports another {field} for lane {lane['lane_id']}", reason="collect-drift")
        if lane.get("elapsed_s") != observed.get("elapsed_s"):
            raise fail(f"R1: collect reports another elapsed_s for lane {lane['lane_id']}", reason="collect-drift")
    for frame, observed in zip(manifest["frames"], result["frames"]):
        if (observed["frame_id"] != frame["frame_id"] or "runtime/" + observed["source_path"] != frame["source"]["path"]
                or observed["runtime_evidence"] != frame["runtime_evidence"]):
            raise fail(f"R1: collect reports another source or runtime evidence for {frame['frame_id']}",
                       reason="collect-drift")
        if cross_check and observed.get("reported_pixel") != frame["source"]["pixel"]:
            raise fail(f"the mod's recorded PixelMetrics of {frame['frame_id']} differ from the kit's",
                       reason="metrics-cross-check")
    for comparison, observed in zip(manifest["comparisons"], result["comparisons"]):
        if cross_check and "reported" in observed and observed["reported"] != comparison["source"]:
            raise fail(f"the mod's recorded comparison {comparison['comparison_id']} differs from the kit's",
                       reason="metrics-cross-check")


def producing_run_event(invocation: Invocation, manifest: Mapping[str, Any]) -> str:
    """``GITHUB_EVENT_NAME`` when this process runs in the handoff run itself (same id and
    attempt); anywhere else the handoff run's event must come from the authenticated run record.
    ``manifest`` must already be validated (:func:`load_handoff`)."""

    handoff = manifest["provenance"]["handoff"]
    environ = invocation.environ
    if (environ.get("GITHUB_RUN_ID") != str(handoff["run_id"])
            or environ.get("GITHUB_RUN_ATTEMPT") != str(handoff["run_attempt"])):
        raise fail("R2 re-derivation needs the handoff run's event: validate inside the producing run, or "
                   "re-derive with the authenticated handoff_run.event", reason="usage")
    return grammar.require(grammar.EVENT, environ.get("GITHUB_EVENT_NAME"), "GITHUB_EVENT_NAME")


def check_handoff(invocation: Invocation, bundle: Bundle, *, handoff_event: str | None = None) -> dict[str, Any]:
    """Pixels, R1 and (with ``handoff_event``) R2 of a loaded handoff; returns the ``collect``
    result of R1."""

    manifest = bundle.manifest
    if handoff_event is not None:
        target = target_for_key(invocation, manifest["key"], subject=manifest["subject"])
        require_rederived(invocation, bundle.expectation_raw, target=target,
                          tested_run=tested_run_projection(manifest["provenance"]["tested"], handoff_event),
                          extensions=bundle.extensions)
    else:
        target = target_from_expectation(bundle.expectation)
    check_handoff_pixels(bundle)
    result = collect_runtime(invocation, bundle, target)
    check_collect(bundle, result, cross_check=invocation.config.images["cross_check_runtime_metrics"])
    return result


def verify_handoff(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None,
                   handoff_event: str | None = None) -> tuple[Bundle, dict[str, Any]]:
    """Full handoff validation; returns the bundle and the ``collect`` result of R1. With
    ``handoff_event`` the expectation is re-derived (R2) with that event's projection."""

    bundle = load_handoff(invocation, root, key=key, expected_subject_commit=expected_subject_commit)
    return bundle, check_handoff(invocation, bundle, handoff_event=handoff_event)


def validate_handoff_dir(invocation: Invocation, root: Path, *, key: str,
                         expected_subject_commit: str | None = None, rederive: bool = True) -> dict[str, Any]:
    """Validate a handoff bundle directory and return its manifest (exit 2 on any defect)."""

    if not rederive:
        bundle, _ = verify_handoff(invocation, root, key=key, expected_subject_commit=expected_subject_commit)
        return bundle.manifest
    bundle = load_handoff(invocation, root, key=key, expected_subject_commit=expected_subject_commit)
    event = producing_run_event(invocation, bundle.manifest)
    check_handoff(producer_invocation(invocation, bundle.manifest["subject"]["commit"]), bundle, handoff_event=event)
    return bundle.manifest


# -- Compact bundles ---------------------------------------------------------------------------------


def load_compact(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None,
                 intermediate: bool = False, selection: str = "final",
                 expectation: Mapping[str, Any] | None = None) -> Bundle:
    """Strict structure, canonical bytes and exact inventory of a compact bundle (no decoding).

    ``selection`` says what the embedded ``selection.json`` must be: ``"final"`` (published
    bundles, bound with ``check_compact_selection``), ``"draft"`` (the intermediate selected
    compaction) or ``"any"`` (a compose hook's output, whose selection the core replaces)."""

    root = real_directory(root, "compact directory")
    manifest, manifest_raw = _manifest(root, COMPACT_KIND)
    embedded, expectation_raw = _expectation(root, invocation.config.image_policy())
    if expectation is not None:
        require_equal(embedded, expectation, f"{EXPECTATION} differs from the expected expectation")
    validate_compact(manifest, expectation=embedded, allowed_extensions=invocation.config.extension_names,
                     intermediate=intermediate)
    _header(invocation, manifest, key=key, expected_subject_commit=expected_subject_commit)
    _check_reuse(invocation, manifest)
    require_inventory(root, manifest["files"], max_files=lim.MAX_COMPACT_FILES,
                      max_total_bytes=lim.MAX_COMPACT_BUNDLE_BYTES, max_file_bytes=_COMPACT_FILE_BYTES, label="compact")
    selection_value, selection_raw = read_document(root, SELECTION, max_bytes=lim.MAX_SELECTION_BYTES, label=SELECTION)
    if selection == "final":
        validate_selection(selection_value, draft=False)
        check_compact_selection(manifest, selection_value)
    elif selection == "draft":
        validate_selection(selection_value, draft=True)
    elif selection != "any":
        raise fail(f"unknown selection requirement {selection!r}")
    extensions = _extensions(invocation, root, manifest)
    return Bundle(root, manifest, manifest_raw, embedded, expectation_raw, extensions, selection_value, selection_raw)


def read_derivative(root: Path, derivative: Mapping[str, Any]) -> bytes:
    data = read_child_file(root, derivative["path"], max_bytes=lim.MAX_DERIVATIVE_BYTES)
    if len(data) != derivative["size"] or sha256_hex(data) != derivative["sha256"]:
        raise fail(f"{derivative['path']} changed after it was recorded")
    return data


def check_compact_pixels(bundle: Bundle) -> None:
    """Every derivative re-inspected at exactly ``thumbnail(source, derivative_box)`` and every
    derivative comparison recomputed on the published WebP bytes."""

    box = bundle.expectation["image_policy"]["derivative_box"]
    measured: dict[str, dict[str, Any]] = {}
    frames = {frame["frame_id"]: frame for frame in bundle.manifest["frames"]}
    for frame in bundle.manifest["frames"]:
        derivative, source = frame["derivative"], frame["source"]
        if derivative["path"] not in measured:
            measured[derivative["path"]] = inspect_webp(read_derivative(bundle.root, derivative),
                                                        derivative_policy(source["width"], source["height"], box))
        if measured[derivative["path"]] != derivative["pixel"]:
            raise fail(f"the derivative PixelMetrics of {frame['frame_id']} differ from the kit's")
    for comparison in bundle.manifest["comparisons"]:
        first = read_derivative(bundle.root, frames[comparison["first_frame_id"]]["derivative"])
        second = read_derivative(bundle.root, frames[comparison["second_frame_id"]]["derivative"])
        metrics = compare(first, second, minimum_changed_fraction=comparison["minimum_changed_fraction"],
                          region=comparison.get("region"))
        if metrics != comparison["derivative"]:
            raise fail(f"the derivative comparison {comparison['comparison_id']} differs from the kit's")


def _without(record: Mapping[str, Any], *fields: str) -> dict[str, Any]:
    return {name: value for name, value in record.items() if name not in fields}


def bind_raw_handoff(invocation: Invocation, compact: Bundle, raw_root: Path) -> None:
    """BP ``_bind_compact``: the compact preserves its raw source, and every derivative that came
    from it is byte-identical to the kit's re-encoding of the raw PNG."""

    manifest = compact.manifest
    source_artifact = manifest["source_artifact"]
    if source_artifact["kind"] != "handoff":
        raise fail("--bind-raw needs a compact bundle compacted from a handoff")
    raw = load_handoff(invocation, raw_root, key=manifest["key"])
    check_handoff_pixels(raw)
    claim = raw.manifest["provenance"]["handoff"]
    if ((source_artifact["run_id"], source_artifact["run_attempt"]) != (claim["run_id"], claim["run_attempt"])
            or source_artifact["name"] != grammar.handoff_name(manifest["key"], claim["run_attempt"])):
        raise fail("the compact's source artifact is not the raw handoff's upload")
    for field in ("repository", "key", "subject", "provenance", "matrix_sha256", "contract_sha256"):
        require_equal(manifest[field], raw.manifest[field], f"the compact's {field} differs from its raw handoff")
    raw_frames = {frame["frame_id"]: frame for frame in raw.manifest["frames"]}
    composed = manifest["scope"]["kind"] == "composed"
    if composed:
        bound = [frame for frame in manifest["frames"] if frame["epoch"] == "selected"]
        if {frame["frame_id"] for frame in bound} != set(raw_frames):
            raise fail("the composed bundle's selected frames are not exactly its raw handoff's frames")
    else:
        bound = manifest["frames"]
        if compact.expectation_raw != raw.expectation_raw:
            raise fail("the compact's expectation.json differs from its raw handoff's")
        require_equal(manifest["scope"], raw.manifest["scope"], "the compact's scope differs from its raw handoff")
        require_equal(manifest["extensions"], raw.manifest["extensions"],
                      "the compact's extensions differ from its raw handoff's")
        require_equal(manifest["lanes"], raw.manifest["lanes"], "the compact's lanes differ from its raw handoff")
        require_equal([frame["frame_id"] for frame in manifest["frames"]], list(raw_frames),
                      "the compact's frames differ from its raw handoff's")
        require_equal([_without(item, "derivative") for item in manifest["comparisons"]], raw.manifest["comparisons"],
                      "the compact's comparisons differ from its raw handoff's")
    policy = compact.expectation["image_policy"]
    encoded: dict[str, bytes] = {}
    for frame in bound:
        original = raw_frames.get(frame["frame_id"])
        if original is None:
            raise fail(f"the raw handoff has no frame {frame['frame_id']}")
        wanted = {**_without(original, "source"), "source": _without(original["source"], "path")}
        require_equal(_without(frame, "derivative", "epoch", "tested"), wanted,
                      f"the compact frame {frame['frame_id']} does not preserve its raw source")
        source = original["source"]
        if source["sha256"] not in encoded:
            encoded[source["sha256"]] = derive_webp(read_source_png(raw.root, source), box=policy["derivative_box"],
                                                    quality=policy["webp_quality"], method=policy["webp_method"])
        if read_derivative(compact.root, frame["derivative"]) != encoded[source["sha256"]]:
            raise fail(f"the derivative of {frame['frame_id']} differs from the authenticated raw pixels",
                       reason="reencode-mismatch")


def validate_compact_dir(invocation: Invocation, root: Path, *, key: str, bind_raw: Path | None = None,
                         expected_subject_commit: str | None = None) -> dict[str, Any]:
    """Validate a published compact bundle (``complete`` or ``composed``) including its embedded
    final selection (``documents.validate_selection`` and ``documents.check_compact_selection``);
    with ``bind_raw`` (the raw handoff directory) re-encode every derivative from the raw PNGs and
    require byte-identical WebP (BP ``_bind_compact``)."""

    bundle = load_compact(invocation, root, key=key, expected_subject_commit=expected_subject_commit)
    check_compact_pixels(bundle)
    if bind_raw is not None:
        bind_raw_handoff(invocation, bundle, bind_raw)
    return bundle.manifest


# -- R6 ----------------------------------------------------------------------------------------------


def check_extensions_verified(manifest: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    """R6 on an ``authenticate_extensions`` result: every extension the manifest carries is
    verified, and ``delegated`` reuse is proven (``reuse_verified``). Returns the verified names."""

    names = manifest["extensions"]["names"] if manifest["extensions"] is not None else []
    missing = sorted(set(names) - set(result["verified"]))
    if missing:
        raise MbError(f"R6: extensions {missing[:5]} were not verified", reason="extensions-unverified")
    if manifest["provenance"]["reuse"] == "delegated" and result["reuse_verified"] is not True:
        raise MbError("R6: delegated reuse was not verified", reason="extensions-unverified")
    return sorted(names)


# -- Dispatch ----------------------------------------------------------------------------------------


def validate_bundle(invocation: Invocation, kind: str, root: Path, *, key: str, bind_raw: Path | None = None,
                    expected_subject_commit: str | None = None) -> dict[str, Any]:
    """The ``validate`` command: dispatch on ``kind`` (``handoff``, ``compact``, ``anchor`` or
    ``family``, the latter validating only the envelope via :mod:`mod_base.family.envelope`)."""

    if kind not in KINDS:
        raise fail(f"unknown bundle kind {kind!r}"[:80], reason="usage")
    if bind_raw is not None and kind != "compact":
        raise fail("--bind-raw applies only to --kind compact", reason="usage")
    if kind == "handoff":
        return validate_handoff_dir(invocation, root, key=key, expected_subject_commit=expected_subject_commit)
    if kind == "compact":
        return validate_compact_dir(invocation, root, key=key, bind_raw=bind_raw,
                                    expected_subject_commit=expected_subject_commit)
    if kind == "anchor":
        from mod_base.evidence.anchor import validate_anchor_dir

        manifest = validate_anchor_dir(root, key=key, expected_subject_commit=expected_subject_commit)
        if manifest["repository"] != invocation.repository:
            raise fail("the anchor names another repository than this run")
        return manifest
    from mod_base.family.envelope import validate_envelope_dir

    envelope = validate_envelope_dir(invocation, root, key=key)
    if expected_subject_commit is not None and envelope["subject"]["commit"] != expected_subject_commit:
        raise fail("the family envelope covers another subject commit than expected", reason="stale-subject")
    return envelope
