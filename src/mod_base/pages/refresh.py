"""Roll the promoted bundles forward as caches (MB6): QS refresh-cache + BP ``refresh_cache``.

``refresh_bundle`` requires that this attempt's caller job ``Deploy GitHub Pages`` and
``Publish / Build atomic static site`` succeeded, then downloads its own inputs by exact id: this
run's single ``mb-promotion`` (uploaded inside the build job's window; validated as a final
promotion), then the collected artifact the promotion names for ``key`` (and ``family``), using the
promotion's ``collected_artifact_id`` and ``collected_digest``. It revalidates that artifact,
requires the live head to equal its ``coverage_sha`` and writes **exactly the bytes to upload**
into ``input_dir``, a new directory (the frozen ``--input`` flag names this upload directory):

* ordinary key: the compact bundle (``mb-collected--<key>`` verbatim), rolled forward as
  ``mb-cache--<key>--<coverage_sha>`` and, for a new complete generation (see ``baseline_name``
  below), also retained as ``mb-baseline--<key>--<commit>--<tested_run_id>``;
* family leg: the collected family artifact's ``source/`` directory (the envelope and native
  bundle, re-collectable by ``family collect``), rolled forward as
  ``mb-family-cache--<family>--<key>--<coverage_sha>``; its selection record
  (``build.FAMILY_SELECTED_NAME``) is checked but never uploaded. A leg the promotion records as not
  available ("nothing collected") returns ``available=False`` with no cache name, writes nothing
  and exits 0.

Details of this port:

* The invocation is this repository's ``pages.yml`` on the canonical branch in the ``refresh``
  (or, for a family, ``refresh-family``) job, and the run and its exact attempt are still the
  ``in_progress`` run of the protected head whose ``referenced_workflows`` resolved the executing
  kit. No adapter hook runs (SPEC §4.3 forbids them in finalize jobs): the compact bundle is
  revalidated structurally with every derivative and derivative comparison re-inspected
  (``evidence.validate``), a family leg with R4 (``validate_projection``) and its envelope.
* The promotion must be exactly ``promotion.json``, canonical, final, and name this run as its
  implementation and the executing kit; the collected artifact must be the one it recorded (id,
  digest, name, this run), its ``manifest.json`` hash, coverage and selected artifact must equal the
  promotion's entry, and its embedded selection must name this Pages attempt. A collected family
  artifact must have the layout ``build`` accepted (:func:`mod_base.pages.build.collected_family_selection`)
  and its selection record must name the promotion's ``selected_artifact_id``.
* The live head: an ordinary bundle's subject branch must still point at its coverage and subject
  tree; a family leg's coverage must still be the head of every branch the promotion published at
  that commit.
* ``baseline_name`` uses the subject commit and the tested run id of the manifest's provenance. It
  is named only for a **new complete generation**, retained once and never extended (Quick Skin:
  once per complete generation): ``baseline_archive.enabled``, the scope is ``complete``, the
  embedded selection's artifact is a ``handoff`` (a ``cache`` republishes a generation an earlier
  publication already promoted, and re-retaining it would also stretch an expired baseline's 90
  days), and no authenticated baseline of that exact name exists yet (the same handoff is selected
  again until rotation retires it, for example by a family wake): at most
  ``limits.MAX_CANDIDATES`` unexpired default-branch uploads of that name owned by another run,
  newest first, go through the consumers' own owner check
  (:func:`mod_base.evidence.compose.authenticate_baseline`, R3: a successful earlier ``pages.yml``
  run whose ``Finalize / Refresh evidence cache for <key>`` job uploaded it in its retention step).
  One that fails that check, or is gone (404), is no retained baseline; every other API failure
  propagates.
* **Exact-name inventory.** Every sibling ``refresh``/``refresh-family`` job of this attempt uploads
  its cache while this one runs, so this run's artifacts are never listed whole: each name the job
  uses (the promotion and its collected artifact) is one exact-name listing of this run
  (``artifacts.list_run_named``), in which a sibling's upload never appears, and every name must
  still be held exactly once. A listing GitHub serves inconsistently anyway (its ``total_count``
  disagreeing with its rows, as it does while the run uploads) is read again within the client's
  budget (``github.api.read_consistently``) and fails closed only after
  ``limits.LISTING_READ_ATTEMPTS`` inconsistent reads.
* **Post-validation recheck** (BP ``refresh_cache`` ``context["recheck"]`` then the cache seal
  recheck): after the upload bytes are written and sealed, and before the upload directory is
  published, this run, its exact attempt, the ``Deploy``/``Build`` jobs (same ids, still
  successful), both used artifacts (same ids, digests, sizes, creation times, not expired) and the
  live head(s) are observed again and must be unchanged; the sealed stage is rechecked last. Any
  drift leaves no upload directory.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.evidence import validate
from mod_base.evidence.compose import authenticate_baseline
from mod_base.family.envelope import ENVELOPE_NAME, MAX_NATIVE_FILE_BYTES, validate_envelope_dir
from mod_base.family.paired import PROJECTION_NAME, SOURCE_DIRECTORY, validate_projection
from mod_base.github import artifacts, contents, jobs, runs
from mod_base.github.api import ApiError, ApiNotFound, GitHubApi, RequestBudgetExhausted
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.io.seal import seal_output
from mod_base.io.tree import read_child_file
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex, strict_loads
from mod_base.pages.build import PROMOTION_FILE, collected_family_selection, current_implementation, require_current_run
from mod_base.runtime import Invocation
from mod_base.workflow import api_job_name, caller_job_name, find_job

OWNER = "MB6"
REFRESH_JOB = "refresh"
REFRESH_FAMILY_JOB = "refresh-family"


@dataclass(frozen=True)
class RefreshResult:
    available: bool
    cache_name: str | None
    baseline_name: str | None = None


def _fail(message: str, reason: str = "refresh") -> MbError:
    return MbError(message[:600], reason=reason)


def _in_window(artifact: artifacts.Artifact, job: Mapping[str, Any]) -> bool:
    try:
        started = jobs.actions_time(job.get("started_at"), "job started_at")
        completed = jobs.actions_time(job.get("completed_at"), "job completed_at")
    except MbError:
        return False
    return started <= artifact.order[0] <= completed


def _successful(attempt_jobs: list[dict[str, Any]], name: str, attempt: int) -> dict[str, Any]:
    job = find_job(attempt_jobs, name, run_attempt=attempt)
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise _fail(f"job {name!r} of this attempt did not succeed", reason="job-graph")
    return job


def _owned(listed: list[artifacts.Artifact], name: str, implementation: Mapping[str, Any],
           branch: str) -> list[artifacts.Artifact]:
    found = [artifact for artifact in listed if artifact.name == name and not artifact.expired]
    for artifact in found:
        if (artifact.head_sha, artifact.head_branch) != (implementation["sha"], branch):
            raise _fail(f"{name} names another head than this run", reason="artifact")
    return found


def _promotion(api: GitHubApi, invocation: Invocation, artifact: artifacts.Artifact, work: Path,
               implementation: Mapping[str, Any]) -> dict[str, Any]:
    output = work / "promotion"
    paths = artifacts.download(api, artifact_id=artifact.id, name=artifact.name, digest=artifact.digest,
                               size=artifact.size, run_id=implementation["run_id"], output=output,
                               extraction=LIMITS_BY_KIND["promotion"])
    if paths != [PROMOTION_FILE]:
        raise _fail(f"{grammar.PROMOTION_NAME} must hold exactly {PROMOTION_FILE}", reason="promotion")
    raw = read_child_file(output, PROMOTION_FILE, max_bytes=lim.MAX_PROMOTION_BYTES)
    promotion = strict_loads(raw, label=PROMOTION_FILE, max_bytes=lim.MAX_PROMOTION_BYTES)
    if canonical_json(promotion) != raw:
        raise _fail(f"{PROMOTION_FILE} is not canonical JSON", reason="promotion")
    documents.validate_promotion(promotion)
    if (promotion["repository"] != invocation.repository or promotion["implementation"] != dict(implementation)
            or promotion["kit"] != invocation.kit):
        raise _fail("the promotion was not written by this Pages attempt and kit", reason="promotion")
    return promotion


@dataclass(frozen=True)
class _Upload:
    """The validated bytes of one cache, the cache ``kind`` they become, the live-head check they
    depend on and the result to report once they are published."""

    files: dict[str, bytes]
    kind: str
    heads: Callable[[], None]
    result: RefreshResult


def _snapshot(api: GitHubApi, invocation: Invocation, implementation: Mapping[str, Any],
              names: tuple[str, ...]) -> tuple[dict[str, Any], dict[str, list[artifacts.Artifact]], bytes]:
    """This run and exact attempt (in progress, executing kit), its successful ``Deploy`` and
    ``Build`` jobs and its artifacts named ``names`` (one exact-name listing each, so the caches
    sibling jobs are uploading never enter it): ``(build job, owned artifacts, observation)``, the
    observation being the canonical record the post-validation recheck compares."""

    run = require_current_run(api, invocation, implementation)
    if runs.referenced_kit_sha(run) != invocation.kit["sha"]:
        raise _fail("this Pages run did not resolve the executing kit", reason="kit-binding")
    run_id, attempt = implementation["run_id"], implementation["run_attempt"]
    attempt_jobs = jobs.attempt_jobs(api, run_id, attempt)
    deploy = _successful(attempt_jobs, caller_job_name("deploy"), attempt)
    build_job = _successful(attempt_jobs, api_job_name("publish", "build"), attempt)
    branch = invocation.config.canonical_branch
    owned = {name: _owned(artifacts.list_run_named(api, run_id, name), name, implementation, branch)
             for name in names}
    observation = canonical_json({
        "run": [run.get("event"), run.get("created_at")],
        "jobs": [[job.get("name"), job.get("id"), job.get("conclusion"), job.get("started_at"),
                  job.get("completed_at")] for job in (deploy, build_job)],
        "artifacts": {name: [[artifact.id, artifact.digest, artifact.size, artifact.created_at]
                             for artifact in found] for name, found in owned.items()},
    })
    return build_job, owned, observation


def _write_upload(input_dir: Path, upload: _Upload, recheck: Callable[[], None]) -> None:
    """Publish exactly ``upload.files`` into the new upload directory, sealed within the extraction
    bounds of the cache it becomes; ``recheck`` and the seal recheck run after sealing and before
    the directory is published."""

    bound = LIMITS_BY_KIND[upload.kind]

    def writer(stage: Path, stage_fd: int) -> None:
        for relative in sorted(upload.files):
            write_new(stage_fd, relative, upload.files[relative])
        rechecks: list[Callable[[], None]] = []
        seal_output(stage_fd, upload.files, max_files=bound.max_entries, max_bytes=bound.max_total_bytes,
                    rechecks=rechecks)
        recheck()
        for sealed in rechecks:
            sealed()

    atomic_directory(Path(input_dir), writer)


def _collected_bytes(root: Path, records: list[Mapping[str, Any]], maximum: int) -> dict[str, bytes]:
    """Every recorded file of ``root``, re-read and re-hashed against its record."""

    files = {}
    for record in records:
        data = read_child_file(root, record["path"], max_bytes=maximum)
        if len(data) != record["size"] or sha256_hex(data) != record["sha256"]:
            raise _fail(f"{record['path']} changed after it was validated", reason="artifact")
        files[record["path"]] = data
    return files


def _baseline_retained(api: GitHubApi, invocation: Invocation, *, key: str, name: str,
                       implementation: Mapping[str, Any]) -> bool:
    """An earlier Pages run already retains the baseline ``name`` of ``key`` (module docstring):
    one of its newest ``limits.MAX_CANDIDATES`` unexpired default-branch uploads by another run
    passes :func:`mod_base.evidence.compose.authenticate_baseline`."""

    default = invocation.config.canonical_branch
    candidates = [artifact for artifact in artifacts.list_named(api, name)
                  if not artifact.expired and artifact.head_branch == default
                  and artifact.run_id != implementation["run_id"]]
    for artifact in candidates[:lim.MAX_CANDIDATES]:
        try:
            authenticate_baseline(invocation, api, key=key,
                                  baseline={"name": artifact.name, "id": artifact.id, "digest": artifact.digest})
        except ApiNotFound:
            continue
        except (ApiError, RequestBudgetExhausted):
            raise
        except MbError:
            continue
        return True
    return False


def _refresh_key(api: GitHubApi, invocation: Invocation, *, key: str, entry: Mapping[str, Any],
                 promotion: Mapping[str, Any], artifact: artifacts.Artifact, work: Path,
                 implementation: Mapping[str, Any]) -> _Upload:
    if (artifact.id, artifact.digest) != (entry["collected_artifact_id"], entry["collected_digest"]):
        raise _fail(f"{artifact.name} is not the collected artifact the promotion recorded", reason="artifact")
    root = work / "collected"
    artifacts.download(api, artifact_id=artifact.id, name=artifact.name, digest=artifact.digest, size=artifact.size,
                       run_id=implementation["run_id"], output=root, extraction=LIMITS_BY_KIND["collected"])
    bundle = validate.load_compact(invocation, root, key=key, expected_subject_commit=entry["coverage_sha"])
    validate.check_compact_pixels(bundle)
    manifest, selection = bundle.manifest, bundle.selection
    assert selection is not None
    if (sha256_hex(bundle.manifest_raw) != entry["manifest_sha256"]
            or manifest["provenance"]["coverage_sha"] != entry["coverage_sha"]
            or manifest["source_artifact"]["id"] != entry["selected_artifact_id"]
            or selection["implementation"] != dict(implementation)):
        raise _fail(f"the collected bundle of {key} is not the one the promotion published", reason="artifact")
    subject = manifest["subject"]
    if promotion["heads"].get(subject["branch"]) != entry["coverage_sha"]:
        raise _fail(f"the promotion published another head of {subject['branch']}", reason="promotion")

    def heads() -> None:
        if contents.branch_head(api, subject["branch"]) != (entry["coverage_sha"], subject["tree"]):
            raise _fail(f"{subject['branch']} advanced past {entry['coverage_sha']}; the cache would be stale",
                        reason="stale-subject")

    heads()
    files = _collected_bytes(root, manifest["files"], max(lim.MAX_EXPECTATION_BYTES, lim.MAX_DERIVATIVE_BYTES))
    files["manifest.json"] = bundle.manifest_raw
    baseline = None
    if (invocation.config.baseline_archive["enabled"] and manifest["scope"]["kind"] == "complete"
            and selection["selected_artifact"]["kind"] == "handoff"):
        name = grammar.baseline_name(key, subject["commit"], manifest["provenance"]["tested"]["run_id"])
        if not _baseline_retained(api, invocation, key=key, name=name, implementation=implementation):
            baseline = name
    return _Upload(files=files, kind="cache", heads=heads, result=RefreshResult(
        available=True, cache_name=grammar.cache_name(key, entry["coverage_sha"]), baseline_name=baseline))


def _refresh_family(api: GitHubApi, invocation: Invocation, *, family: str, key: str, entry: Mapping[str, Any],
                    promotion: Mapping[str, Any], artifact: artifacts.Artifact, work: Path,
                    implementation: Mapping[str, Any]) -> _Upload:
    if (artifact.id, artifact.digest) != (entry["collected_artifact_id"], entry["collected_digest"]):
        raise _fail(f"{artifact.name} is not the collected artifact the promotion recorded", reason="artifact")
    root = work / "collected-family"
    artifacts.download(api, artifact_id=artifact.id, name=artifact.name, digest=artifact.digest, size=artifact.size,
                       run_id=implementation["run_id"], output=root, extraction=LIMITS_BY_KIND["collected-family"])
    selected = collected_family_selection(root, family=family, key=key, reason="artifact")
    if selected.artifact_id != entry["selected_artifact_id"]:
        raise _fail(f"the collected {family} artifact of {key} selected another generation than the promotion "
                    "published", reason="artifact")
    coverage = entry["coverage_sha"]
    source = root / SOURCE_DIRECTORY
    envelope = validate_envelope_dir(invocation, source, family=family, key=key)
    validate_projection(invocation, root / PROJECTION_NAME, images_root=root, family=family, key=key,
                        expected_coverage_sha=coverage)
    branches = sorted(branch for branch, head in promotion["heads"].items() if head == coverage)
    if not branches:
        raise _fail(f"the promotion published no branch at the {family} coverage {coverage}", reason="promotion")

    def heads() -> None:
        for branch in branches:
            if contents.branch_head(api, branch)[0] != coverage:
                raise _fail(f"{branch} advanced past {coverage}; the family cache would be stale",
                            reason="stale-subject")

    heads()
    files = _collected_bytes(source, envelope["files"], MAX_NATIVE_FILE_BYTES)
    files[ENVELOPE_NAME] = read_child_file(source, ENVELOPE_NAME, max_bytes=lim.MAX_ENVELOPE_BYTES)
    if files[ENVELOPE_NAME] != canonical_json(envelope):
        raise _fail(f"{ENVELOPE_NAME} changed after it was validated", reason="artifact")
    return _Upload(files=files, kind="family-cache", heads=heads,
                   result=RefreshResult(available=True, cache_name=grammar.family_cache_name(family, key, coverage)))


def refresh_bundle(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None, input_dir: Path) -> RefreshResult:
    """Download and revalidate one promoted bundle, write its upload bytes into the new
    ``input_dir`` and name its cache (see module docstring). For a family leg with nothing
    collected it returns ``available=False`` (exit 0)."""

    key = grammar.require_key(key)
    if family is not None:
        invocation.config.family(grammar.require_family(family))
    if os.path.lexists(input_dir):
        raise _fail("the upload directory must not exist", reason="output")
    implementation = current_implementation(invocation,
                                            jobs_allowed=(REFRESH_JOB if family is None else REFRESH_FAMILY_JOB,))
    name = grammar.collected_name(key) if family is None else grammar.collected_family_name(family, key)
    names = (grammar.PROMOTION_NAME, name)
    build_job, owned, observation = _snapshot(api, invocation, implementation, names)
    promotions = owned[grammar.PROMOTION_NAME]
    if len(promotions) != 1 or not _in_window(promotions[0], build_job):
        raise _fail(f"this run must hold exactly one {grammar.PROMOTION_NAME} uploaded by its build job",
                    reason="promotion")
    with tempfile.TemporaryDirectory(prefix="mb-refresh-") as directory:
        work = Path(directory).resolve()
        promotion = _promotion(api, invocation, promotions[0], work, implementation)
        if family is not None:
            entry = next((item for item in promotion["families"]
                          if (item["family"], item["key"]) == (family, key)), None)
            if entry is None:
                raise _fail(f"the promotion names no {family} leg for {key}", reason="promotion")
            if not entry["available"]:
                return RefreshResult(available=False, cache_name=None)
        else:
            entry = next((bundle for bundle in promotion["bundles"] if bundle["key"] == key), None)
            if entry is None:
                raise _fail(f"the promotion publishes no bundle for {key}", reason="promotion")
        collected = owned[name]
        if len(collected) != 1:
            raise _fail(f"this run must hold exactly one {name}, found {len(collected)}", reason="artifact")
        if family is not None:
            upload = _refresh_family(api, invocation, family=family, key=key, entry=entry, promotion=promotion,
                                     artifact=collected[0], work=work, implementation=implementation)
        else:
            upload = _refresh_key(api, invocation, key=key, entry=entry, promotion=promotion, artifact=collected[0],
                                  work=work, implementation=implementation)

        def recheck() -> None:
            if _snapshot(api, invocation, implementation, names)[2] != observation:
                raise _fail("this run, its jobs or its artifacts changed while the cache was prepared",
                            reason="stale-input")
            upload.heads()

        _write_upload(input_dir, upload, recheck)
        return upload.result
