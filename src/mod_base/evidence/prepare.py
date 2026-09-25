"""The producer (MB3): Quick Skin ``evidence.prepare`` + Block Pops ``evidence.curate``.

Runs in the ``prepare-evidence`` composite with no API token; every hook sees the subject commit
as ``ctx.implementation_sha`` (SPEC §4.2). It derives the target and the expectation, calls
``collect`` on the E2E output, copies the declared runtime subset verbatim into ``runtime/``,
recomputes every PixelMetrics and CompareMetrics with the kit, cross-checks the adapter's reported
values when ``config.images.cross_check_runtime_metrics`` (``reported_pixel`` is then mandatory; a
comparison's optional ``reported`` is compared whenever the adapter supplies it), calls ``collect``
again on the copied ``runtime/`` and requires an equal result (R1), writes ``manifest.json``,
``expectation.json`` and ``extensions.json`` through ``atomic_directory`` and validates the
result as ``mod-base.evidence.handoff``.

Reuse (SPEC §3.2 rule 5) follows from the claims: a tested run that is the handoff run is
``none``; a distinct tested run is ``delegated`` when the extensions carry
``config.source.delegated_reuse_extension``, otherwise ``attested`` when the config names an
``attestation_job``; anything else is refused. The ``expectation`` hook sees the handoff run's
``GITHUB_EVENT_NAME`` (:func:`mod_base.evidence.expectation.tested_run_projection`).

Only the files ``collect`` declares are read from the E2E output, each through ``O_NOFOLLOW``
descriptors (no symlink anywhere on its path) within the runtime JSON/PNG bounds. The output is a
new directory published atomically only after everything, including the anchor decision
(``anchor_selection``), has succeeded, so a failed prepare leaves nothing behind.
"""

from __future__ import annotations

import os
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
    fail,
    producer_invocation,
    read_document,
    real_directory,
    source_policy,
)
from mod_base.evidence.expectation import (
    derive_expectation,
    expectation_bytes,
    read_extensions,
    target_for_key,
    tested_run_projection,
)
from mod_base.evidence.anchor import eligible_nodes
from mod_base.evidence.validate import verify_handoff
from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import inspect_png
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import read_child_file
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.documents import RUN_CLAIM, SUBJECT, validate_handoff
from mod_base.runtime import Invocation

OWNER = "MB3"
KIND = "mod-base.evidence.handoff"


@dataclass(frozen=True)
class PrepareResult:
    manifest: dict[str, Any]
    output: Path
    anchor_eligible: bool


def _reuse(invocation: Invocation, tested: Mapping[str, Any], handoff: Mapping[str, Any],
           extensions: Mapping[str, Any]) -> str:
    if tested["run_id"] == handoff["run_id"]:
        return "none"
    source = invocation.config.source
    if source["delegated_reuse_extension"] is not None and source["delegated_reuse_extension"] in extensions:
        return "delegated"
    if source["attestation_job"] is not None:
        return "attested"
    raise fail("the tested run is not the handoff run and the config allows neither delegated nor attested reuse")


def _check_anchor_output(anchor: str, anchor_output: Path | None, output: Path) -> None:
    if anchor not in ("auto", "off"):
        raise fail("anchor must be 'auto' or 'off'", reason="usage")
    if anchor_output is None:
        return
    if anchor == "off":
        raise fail("an anchor output requires anchor 'auto'", reason="usage")
    reserved = Path(os.path.abspath(anchor_output))
    if os.path.lexists(reserved):
        raise fail("the anchor output must not exist yet (anchor create writes it)")
    if reserved == output or output in reserved.parents:
        raise fail("the anchor output must lie outside the handoff output")


def prepare_handoff(invocation: Invocation, *, e2e_root: Path, key: str, output: Path,
                    subject: Mapping[str, str], tested: Mapping[str, Any], handoff: Mapping[str, Any],
                    extensions_path: Path | None = None, anchor: str = "auto",
                    anchor_output: Path | None = None) -> PrepareResult:
    """Produce the handoff bundle for ``key`` in the new directory ``output``.

    ``subject`` is ``{branch, commit, tree}``; ``tested`` and ``handoff`` are ``RunClaim`` dicts
    (the handoff claim comes from ``documents.run_claim_from_environment``). ``anchor`` is
    ``"auto"`` or ``"off"``: with ``"auto"`` the result reports whether the adapter's
    ``anchor_selection`` and the direct-run rule make an anchor eligible
    (:func:`mod_base.evidence.anchor.eligible_nodes`; the anchor itself is cut by ``anchor
    create`` after the handoff upload, because it needs the artifact id).
    """

    grammar.require_key(key)
    output = Path(os.path.abspath(output))
    _check_anchor_output(anchor, anchor_output, output)
    subject = SUBJECT(dict(subject), "$.subject")
    invocation = producer_invocation(invocation, subject["commit"])
    tested = RUN_CLAIM(dict(tested), "$.tested")
    handoff = RUN_CLAIM(dict(handoff), "$.handoff")
    config = invocation.config
    for label, claim in (("handoff", handoff), ("tested", tested)):
        if claim["workflow_path"] != config.source["workflow"]:
            raise fail(f"the {label} run is not a {config.source['workflow']} run")
    e2e = real_directory(e2e_root, "E2E output root")
    extensions = read_extensions(invocation, extensions_path)
    reuse = _reuse(invocation, tested, handoff, extensions)
    event = grammar.require(grammar.EVENT, invocation.environ.get("GITHUB_EVENT_NAME"), "GITHUB_EVENT_NAME")
    target = target_for_key(invocation, key, subject=subject)
    expectation = derive_expectation(invocation, target=target, tested_run=tested_run_projection(tested, event),
                                     extensions=extensions)
    collected = host.call(invocation, "collect", {"runtime_root": str(e2e), "target": target,
                                                  "expectation": expectation})
    expectation_data = expectation_bytes(expectation)
    extensions_data = canonical_json(extensions) if extensions else None
    cross_check = config.images["cross_check_runtime_metrics"]
    policy = source_policy(expectation)

    def writer(stage: Path, descriptor: int) -> tuple[dict[str, Any], bool]:
        records: list[dict[str, Any]] = []
        sources = {frame["source_path"] for frame in collected["frames"]}
        pixels: dict[str, dict[str, Any]] = {}
        total = 0
        for name in collected["runtime_files"]:
            bound = lim.MAX_RUNTIME_JSON_BYTES if name.endswith(".json") else lim.MAX_SOURCE_PNG_BYTES
            data = read_child_file(e2e, name, max_bytes=bound)
            total += len(data)
            if total > lim.MAX_RAW_BUNDLE_BYTES:
                raise fail("the declared runtime files exceed the raw bundle bound")
            write_new(descriptor, "runtime/" + name, data)
            records.append({"path": "runtime/" + name, "sha256": sha256_hex(data), "size": len(data)})
            if name in sources:
                pixels[name] = inspect_png(data, policy)
        write_new(descriptor, EXPECTATION, expectation_data)
        records.append({"path": EXPECTATION, "sha256": sha256_hex(expectation_data), "size": len(expectation_data)})
        extension_record = None
        if extensions_data is not None:
            write_new(descriptor, EXTENSIONS, extensions_data)
            extension_record = {"path": EXTENSIONS, "sha256": sha256_hex(extensions_data), "size": len(extensions_data),
                                "names": sorted(extensions)}
            records.append({field: extension_record[field] for field in ("path", "sha256", "size")})
        manifest = _manifest(invocation, key=key, subject=subject, tested=tested, handoff=handoff, reuse=reuse,
                             expectation=expectation, expectation_data=expectation_data,
                             extension_record=extension_record, collected=collected, pixels=pixels,
                             records=records, stage=stage, cross_check=cross_check)
        validate_handoff(manifest, expectation=expectation, allowed_extensions=config.extension_names)
        write_new(descriptor, MANIFEST, canonical_json(manifest))
        _, rederived = verify_handoff(invocation, stage, key=key, expected_subject_commit=subject["commit"])
        if canonical_json(rederived) != canonical_json(collected):
            raise MbError("R1: collect on the copied runtime/ differs from collect on the E2E output",
                          reason="collect-drift")
        written, _ = read_document(stage, MANIFEST, max_bytes=lim.MAX_MANIFEST_BYTES, label=MANIFEST)
        nodes = eligible_nodes(invocation, written, expectation) if anchor == "auto" else ()
        return written, bool(nodes)

    manifest, anchor_eligible = atomic_directory(output, writer)
    return PrepareResult(manifest=manifest, output=output, anchor_eligible=anchor_eligible)


def _manifest(invocation: Invocation, *, key: str, subject: Mapping[str, str], tested: Mapping[str, Any],
              handoff: Mapping[str, Any], reuse: str, expectation: Mapping[str, Any], expectation_data: bytes,
              extension_record: Mapping[str, Any] | None, collected: Mapping[str, Any],
              pixels: Mapping[str, dict[str, Any]], records: list[dict[str, Any]], stage: Path,
              cross_check: bool) -> dict[str, Any]:
    lanes_by_id = {lane["lane_id"]: lane for lane in expectation["lanes"]}
    lanes = []
    for wanted, observed in zip(expectation["lanes"], collected["lanes"]):
        lane = {**wanted, **{field: observed[field] for field in ("profile", "status", "jars")}}
        if "elapsed_s" in observed:
            lane["elapsed_s"] = observed["elapsed_s"]
        lanes.append(lane)
    by_frame = {frame["frame_id"]: frame for frame in collected["frames"]}
    records_by_path = {record["path"]: record for record in records}
    frames = []
    for capture in expectation["captures"]:
        observed = by_frame[capture["frame_id"]]
        lane = lanes_by_id[capture["lane_id"]]
        name = observed["source_path"]
        pixel = pixels[name]
        if cross_check and observed.get("reported_pixel") != pixel:
            raise MbError(f"the mod's recorded PixelMetrics of {capture['frame_id']} differ from the kit's",
                          reason="metrics-cross-check")
        record = records_by_path["runtime/" + name]
        frames.append({
            **capture,
            **{field: lane[field] for field in ("artifact_node", "minecraft", "loader", "scenario")},
            "runtime_evidence": observed["runtime_evidence"],
            "source": {"path": record["path"], "sha256": record["sha256"], "size": record["size"],
                       "width": pixel["width"], "height": pixel["height"], "format": "png", "pixel": pixel},
        })
    frame_paths = {frame["frame_id"]: frame["source"]["path"] for frame in frames}
    reported = {item["comparison_id"]: item.get("reported") for item in collected["comparisons"]}
    comparisons = []
    for wanted in expectation["comparisons"]:
        first = read_child_file(stage, frame_paths[wanted["first_frame_id"]], max_bytes=lim.MAX_SOURCE_PNG_BYTES)
        second = read_child_file(stage, frame_paths[wanted["second_frame_id"]], max_bytes=lim.MAX_SOURCE_PNG_BYTES)
        metrics = compare(first, second, minimum_changed_fraction=wanted["minimum_changed_fraction"],
                          region=wanted.get("region"))
        observed = reported[wanted["comparison_id"]]
        if cross_check and observed is not None and observed != metrics:
            raise MbError(f"the mod's recorded comparison {wanted['comparison_id']} differs from the kit's",
                          reason="metrics-cross-check")
        comparisons.append({**wanted, "source": metrics})
    scope = {"kind": expectation["scope"]["kind"]}
    if "detail_sha256" in expectation["scope"]:
        scope["detail_sha256"] = expectation["scope"]["detail_sha256"]
    return {
        "kind": KIND,
        "schema_version": 1,
        "repository": invocation.repository,
        "key": key,
        "kit": invocation.kit,
        "subject": dict(subject),
        "provenance": {"handoff": dict(handoff), "tested": dict(tested), "reuse": reuse,
                       "coverage_sha": subject["commit"]},
        "expectation": {"path": EXPECTATION, "sha256": sha256_hex(expectation_data), "size": len(expectation_data)},
        "matrix_sha256": expectation["matrix_sha256"],
        "contract_sha256": expectation["contract_sha256"],
        "scope": scope,
        "extensions": None if extension_record is None else dict(extension_record),
        "lanes": lanes,
        "frames": frames,
        "comparisons": comparisons,
        "files": sorted(records, key=lambda record: record["path"]),
    }
