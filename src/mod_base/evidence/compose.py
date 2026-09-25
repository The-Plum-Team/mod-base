"""Composition of ``selected`` evidence with its authenticated baseline (MB3, rule R3).

``compose_selected`` is collect step 4 for a ``selected`` handoff (see ``docs/SCHEMAS.md``, "The
collect flow"); it replaces step 5 (``compact``) for that bundle:

1. validate the selected handoff directory and the selection **draft** written by
   ``authenticate`` (``documents.validate_selection(draft=True)``);
2. write the core's own compaction of the selected handoff into a private directory: a compact
   bundle validated with ``documents.validate_compact(intermediate=True)`` (``scope.kind:
   "selected"``, its selected expectation and the draft embedded as ``selection.json``); its
   :func:`mod_base.model.documents.compact_identity_sha256` is the ``selected_manifest_sha256``;
3. call the adapter's network hook ``compose(ctx, key, selected_compact_dir, output_dir)`` with
   that directory and a private output directory (no ``compose`` hook: fail closed);
4. R3: download the named ``mb-baseline--<key>--...`` artifact by id and authenticate it (owner:
   a successful ``pages.yml`` run on the default branch whose attempt's ``Finalize / Refresh
   evidence cache for {key}`` job succeeded, upload inside its "Retain the complete compact
   generation for feature evidence reuse" step window), validate it as a compact bundle, then
   require every ``epoch: baseline`` frame to equal the baseline bytes and metrics, every ``epoch:
   selected`` frame to equal step 2's compaction, the composed frames to equal the complete
   expectation with no duplicate or missing frame, the selected evidence to be used in full (the
   ``epoch: selected`` frames are exactly step 2's frames, and every lane and comparison step 2
   re-tested comes from it, never from the older baseline), ``scope.components`` to name that
   baseline and step 2's identity, and every ``tested`` record to equal its source;
5. complete the selection: ``binding`` (``reencode-identical``, frame and distinct-derivative
   counts), ``composition`` (``baseline_artifact`` with the authenticated ``owner_run_id``, and
   ``selected_manifest_sha256``) and ``manifest_sha256``, then re-emit the verified composed bundle
   into the new ``output`` with that final selection embedded as ``selection.json``
   (``documents.check_compact_selection`` holds).

The hook's composed bundle must be written as the kit writes bundles (``canonical_json`` documents,
``images/<sha>.webp``); its embedded expectation must be exactly what the adapter re-derives from
the composed bundle's own extensions with the authenticated run records (R2, the complete
expectation; :func:`mod_base.evidence.expectation.require_rederived_for_runs`), and any extension
object that differs from what ``authenticate`` already verified for the selected handoff is
verified again through ``authenticate_extensions`` (R6).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.adapter.protocol import HookUnsupported
from mod_base.errors import MbError
from mod_base.evidence._common import (
    EXPECTATION,
    EXTENSIONS,
    MANIFEST,
    SELECTION,
    fail,
    require_equal,
)
from mod_base.evidence.compact import (
    PLACEHOLDER,
    bind_draft,
    compact_images,
    compact_manifest,
    file_record,
    finalize,
    read_draft,
    rederive,
    write_compact,
)
from mod_base.evidence.expectation import require_rederived_for_runs, target_for_key
from mod_base.evidence.validate import (
    Bundle,
    check_compact_pixels,
    check_extensions_verified,
    load_compact,
    read_derivative,
    verify_handoff,
)
from mod_base.github import artifacts as github_artifacts
from mod_base.github import jobs as github_jobs
from mod_base.github import runs as github_runs
from mod_base.github.api import GitHubApi
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.documents import compact_identity_sha256, validate_compact
from mod_base.runtime import Invocation
from mod_base.workflow import PAGES_EVENTS, PAGES_WORKFLOW_PATH, api_job_name, find_job, step_name

OWNER = "MB3"


def _intermediate(invocation: Invocation, bundle: Bundle, draft: Mapping[str, Any], output: Path) -> dict[str, Any]:
    """Step 2: the core's own compaction of the selected handoff, the draft embedded."""

    frames, comparisons, images = compact_images(bundle)
    files = {EXPECTATION: bundle.expectation_raw, **images}
    if bundle.manifest["extensions"] is not None:
        files[EXTENSIONS] = canonical_json(bundle.extensions)
    draft_data = canonical_json(draft)
    files[SELECTION] = draft_data
    manifest = compact_manifest(invocation, bundle.manifest, source_artifact=draft["selected_artifact"],
                                scope={"kind": "selected", "detail_sha256": bundle.manifest["scope"]["detail_sha256"]},
                                frames=frames, comparisons=comparisons,
                                files=[file_record(path, data) for path, data in files.items() if path != SELECTION])
    record = file_record(SELECTION, draft_data)
    manifest["selection"] = record
    manifest["files"] = sorted([item for item in manifest["files"] if item["path"] != SELECTION] + [record],
                               key=lambda item: item["path"])
    validate_compact(manifest, expectation=bundle.expectation, allowed_extensions=invocation.config.extension_names,
                     intermediate=True)

    def writer(stage: Path, descriptor: int) -> dict[str, Any]:
        for path in sorted(files):
            write_new(descriptor, path, files[path])
        write_new(descriptor, MANIFEST, canonical_json(manifest))
        return load_compact(invocation, stage, key=bundle.manifest["key"], intermediate=True,
                            selection="draft").manifest

    return atomic_directory(output, writer)


def authenticate_baseline(invocation: Invocation, api: GitHubApi, *, key: str,
                          baseline: Mapping[str, Any]) -> github_artifacts.Artifact:
    """R3 owner authentication of the ``mb-baseline`` artifact the compose hook named."""

    parsed = grammar.require_artifact_name(baseline["name"], "baseline")
    if parsed.key != key:
        raise fail("the compose hook named another key's baseline", reason="baseline-owner")
    artifact = github_artifacts.get_artifact(api, baseline["id"])
    if artifact.name != baseline["name"] or artifact.digest != baseline["digest"] or artifact.expired:
        raise fail("the baseline artifact differs from the one the compose hook named, or expired",
                   reason="baseline-owner")
    branch = invocation.config.canonical_branch
    run = github_runs.get_run(api, artifact.run_id)
    github_runs.validate_run(run, repository=invocation.repository, workflow_path=PAGES_WORKFLOW_PATH,
                             events=PAGES_EVENTS, head_branch=branch, require_success=True)
    if run.get("head_sha") != artifact.head_sha or artifact.head_branch != branch:
        raise fail("the baseline artifact does not belong to its owner run's head", reason="baseline-owner")
    attempt = grammar.require_positive_int(run.get("run_attempt"), "baseline owner run attempt")
    jobs = github_jobs.attempt_jobs(api, artifact.run_id, attempt)
    job = find_job(jobs, api_job_name("finalize", "refresh", key=key), run_attempt=attempt)
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise fail("the baseline's refresh job did not succeed", reason="baseline-owner")
    upload = step_name("baseline_upload")
    github_jobs.require_successful_step(job, upload)
    started, completed = github_jobs.step_window(job, upload)
    created = github_jobs.actions_time(artifact.created_at, "baseline artifact created_at")
    if not started <= created <= completed:
        raise fail("the baseline was not uploaded inside its refresh job's retention step", reason="baseline-owner")
    return artifact


def _tested_run(bundle: Bundle) -> dict[str, Any]:
    if bundle.selection is None:
        raise fail("a compact bundle without its embedded selection cannot be composed", reason="composition")
    return bundle.selection["source"]["tested_run"]


def _lane_epochs(frames: list[Mapping[str, Any]]) -> dict[str, str]:
    epochs: dict[str, str] = {}
    for frame in frames:
        if epochs.setdefault(frame["lane_id"], frame["epoch"]) != frame["epoch"]:
            raise fail(f"lane {frame['lane_id']} mixes baseline and selected frames", reason="composition")
    return epochs


def verify_composition(invocation: Invocation, *, composed_dir: Path, selected_dir: Path, baseline_dir: Path,
                       expectation: dict[str, Any]) -> None:
    """R3 core re-verification (step 4 of the module docstring); ``selected_dir`` is the
    intermediate selected compaction. Raises :class:`mod_base.errors.MbError`."""

    key = expectation["key"]
    if expectation["scope"]["kind"] != "complete":
        raise fail("a composed bundle must embed the complete expectation", reason="composition")
    composed = load_compact(invocation, composed_dir, key=key, selection="any", expectation=expectation)
    selected = load_compact(invocation, selected_dir, key=key, intermediate=True, selection="draft")
    baseline = load_compact(invocation, baseline_dir, key=key)
    for bundle in (composed, selected, baseline):
        check_compact_pixels(bundle)
    cm, sm, bm = composed.manifest, selected.manifest, baseline.manifest
    if cm["scope"]["kind"] != "composed" or bm["scope"]["kind"] != "complete":
        raise fail("R3 composes a selected compaction with a complete baseline into a composed bundle",
                   reason="composition")
    for field in ("repository", "key", "kit", "subject", "provenance", "matrix_sha256", "contract_sha256",
                  "source_artifact"):
        require_equal(cm[field], sm[field], f"R3: the composed {field} differs from the selected evidence")
    components = cm["scope"]["components"]
    if components["baseline"]["manifest_sha256"] != sha256_hex(baseline.manifest_raw):
        raise fail("R3: scope.components.baseline names another baseline manifest", reason="composition")
    if components["selected_manifest_sha256"] != compact_identity_sha256(sm):
        raise fail("R3: scope.components.selected_manifest_sha256 is not the selected compaction", reason="composition")
    sources = {"selected": (sm, _tested_run(selected)), "baseline": (bm, _tested_run(baseline))}
    indexes = {epoch: ({frame["frame_id"]: frame for frame in manifest["frames"]},
                       {lane["lane_id"]: lane for lane in manifest["lanes"]},
                       {item["comparison_id"]: item for item in manifest["comparisons"]})
               for epoch, (manifest, _) in sources.items()}
    epochs = _lane_epochs(cm["frames"])
    _require_selected_in_full(cm, indexes["selected"], epochs)
    for frame in cm["frames"]:
        epoch = frame["epoch"]
        frames, lanes, _ = indexes[epoch]
        original = frames.get(frame["frame_id"])
        if original is None:
            raise fail(f"R3: the {epoch} evidence has no frame {frame['frame_id']}", reason="composition")
        require_equal({name: value for name, value in frame.items() if name not in ("epoch", "tested")}, original,
                      f"R3: composed frame {frame['frame_id']} differs from its {epoch} source")
        lane = lanes.get(frame["lane_id"])
        if lane is None:
            raise fail(f"R3: the {epoch} evidence has no lane {frame['lane_id']}", reason="composition")
        tested = {**sources[epoch][1], "jar_sha256": lane["jars"]["production_sha256"]}
        require_equal(frame["tested"], tested, f"R3: composed frame {frame['frame_id']} records another tested run")
    for lane in cm["lanes"]:
        epoch = "selected" if lane["lane_id"] in indexes["selected"][1] else "baseline"
        source_lane = indexes[epoch][1].get(lane["lane_id"])
        if source_lane is None or canonical_json(source_lane) != canonical_json(lane):
            raise fail(f"R3: composed lane {lane['lane_id']} differs from its {epoch} source lane", reason="composition")
    by_id = {frame["frame_id"]: frame for frame in cm["frames"]}
    for comparison in cm["comparisons"]:
        first, second = by_id[comparison["first_frame_id"]], by_id[comparison["second_frame_id"]]
        if first["epoch"] != second["epoch"]:
            raise fail(f"R3: comparison {comparison['comparison_id']} spans two epochs", reason="composition")
        original = indexes[first["epoch"]][2].get(comparison["comparison_id"])
        if original is None:
            raise fail(f"R3: the {first['epoch']} evidence has no comparison {comparison['comparison_id']}",
                       reason="composition")
        require_equal(comparison, original, f"R3: composed comparison {comparison['comparison_id']} differs "
                                            f"from its {first['epoch']} source")


def _require_selected_in_full(composed: Mapping[str, Any],
                              selected: tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
                              epochs: Mapping[str, str]) -> None:
    """R3: the composition uses the selected evidence in full. Its ``epoch: selected`` frames are
    exactly the selected compaction's frames, every selected lane is present and none of its frames
    comes from the baseline, and every selected comparison is present; a hook can never publish
    older baseline evidence for what the selection re-tested."""

    frames, lanes, comparisons = selected
    marked = {frame["frame_id"] for frame in composed["frames"] if frame["epoch"] == "selected"}
    if marked != set(frames):
        raise fail("R3: the composed selected frames are not exactly the selected evidence's frames",
                   reason="composition")
    present = {lane["lane_id"] for lane in composed["lanes"]}
    for lane_id in lanes:
        if lane_id not in present or epochs.get(lane_id, "selected") != "selected":
            raise fail(f"R3: selected lane {lane_id} is not composed from the selected evidence", reason="composition")
    if not set(comparisons) <= {item["comparison_id"] for item in composed["comparisons"]}:
        raise fail("R3: the composition drops a comparison of the selected evidence", reason="composition")


def _verified_extensions(invocation: Invocation, composed: Bundle, selected: Bundle,
                         draft: Mapping[str, Any]) -> list[str]:
    """R6 for the composed bundle's extensions: objects already verified for the selected handoff
    are kept; anything else goes through ``authenticate_extensions`` again."""

    manifest = composed.manifest
    names = manifest["extensions"]["names"] if manifest["extensions"] is not None else []
    verified = set(draft["extensions_verified"])
    if all(name in verified and canonical_json(composed.extensions[name]) == canonical_json(
            selected.extensions.get(name)) for name in names):
        return list(names)
    result = host.call(invocation, "authenticate_extensions",
                       {"manifest": manifest, "extensions": composed.extensions}, network=True)
    return check_extensions_verified(manifest, result)


def compose_selected(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selection_path: Path,
                     output: Path) -> dict[str, Any]:
    """The ``compose`` command (``--selected DIR --selection F --output DIR``): steps 1-5 of the
    module docstring; ``selection_path`` is the draft from ``authenticate``. Returns the composed
    compact manifest written to ``output`` (a new directory)."""

    draft = read_draft(invocation, selection_path, key=key)
    if draft["selected_artifact"]["kind"] != "handoff":
        raise fail("compose takes a selected handoff, not a cache", reason="usage")
    selected, _ = verify_handoff(invocation, selected_dir, key=key, expected_subject_commit=draft["subject"]["commit"])
    if selected.manifest["scope"]["kind"] != "selected":
        raise fail("compose takes a selected handoff; compact a complete one", reason="usage")
    bind_draft(draft, selected, kind="handoff")
    rederive(invocation, selected, draft)
    output = Path(os.path.abspath(output))
    with tempfile.TemporaryDirectory(prefix="mb-compose-") as work_text:
        work = Path(work_text).resolve()
        intermediate_dir = work / "selected"
        intermediate = _intermediate(invocation, selected, draft, intermediate_dir)
        hook_dir = work / "composed"
        os.mkdir(hook_dir, 0o700)
        try:
            result = host.call(invocation, "compose", {"key": key, "selected_compact_dir": str(intermediate_dir),
                                                      "output_dir": str(hook_dir)}, network=True)
        except HookUnsupported as exc:
            raise MbError("selected evidence needs the adapter's compose hook; refusing to publish it alone",
                          reason="compose-unsupported") from exc
        artifact = authenticate_baseline(invocation, api, key=key, baseline=result["baseline_artifact"])
        baseline_dir = work / "baseline"
        github_artifacts.download(api, artifact_id=artifact.id, name=artifact.name, digest=artifact.digest,
                                  size=artifact.size, run_id=artifact.run_id, output=baseline_dir,
                                  extraction=LIMITS_BY_KIND["baseline"])
        baseline = load_compact(invocation, baseline_dir, key=key)
        parsed = grammar.require_artifact_name(artifact.name, "baseline")
        if (baseline.manifest["subject"]["commit"], baseline.manifest["provenance"]["tested"]["run_id"]) != (
                parsed.commit, parsed.run_id):
            raise fail("the baseline bundle is not the generation its artifact name records", reason="baseline-owner")
        composed = load_compact(invocation, hook_dir, key=key, selection="any")
        manifest = composed.manifest
        target = target_for_key(invocation, key, subject=manifest["subject"])
        complete = require_rederived_for_runs(invocation, composed.expectation_raw, target=target,
                                              tested=manifest["provenance"]["tested"],
                                              handoff_event=draft["source"]["handoff_run"]["event"],
                                              tested_event=draft["source"]["tested_run"]["event"],
                                              extensions=composed.extensions)
        verify_composition(invocation, composed_dir=hook_dir, selected_dir=intermediate_dir,
                           baseline_dir=baseline_dir, expectation=complete)
        wanted = {"artifact_id": artifact.id, "name": artifact.name, "digest": artifact.digest,
                  "manifest_sha256": sha256_hex(baseline.manifest_raw)}
        require_equal(manifest["scope"]["components"]["baseline"], wanted,
                      "R3: scope.components.baseline is not the authenticated baseline artifact")
        names = _verified_extensions(invocation, composed, selected, draft)
        files = {EXPECTATION: composed.expectation_raw}
        if manifest["extensions"] is not None:
            files[EXTENSIONS] = canonical_json(composed.extensions)
        for frame in manifest["frames"]:
            files.setdefault(frame["derivative"]["path"], read_derivative(composed.root, frame["derivative"]))
        emitted = {**manifest, "selection": dict(PLACEHOLDER),
                   "files": sorted([file_record(path, data) for path, data in files.items()] + [dict(PLACEHOLDER)],
                                   key=lambda record: record["path"])}
        composition = {"baseline_artifact": {"id": artifact.id, "name": artifact.name, "digest": artifact.digest,
                                             "owner_run_id": artifact.run_id},
                       "selected_manifest_sha256": compact_identity_sha256(intermediate)}
        emitted, _, selection_data = finalize(emitted, draft, mode="reencode-identical", composition=composition,
                                              extensions_verified=names)
        return write_compact(invocation, output, emitted, complete, {**files, SELECTION: selection_data}, key=key)
