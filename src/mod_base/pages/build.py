"""The atomic site renderer (MB6, SPEC §5.3.2): QS ``build_site`` + BP ``_current_pages_inputs``.

``build_site`` downloads its own inputs: SPEC §5.3 build steps 1 ("Download every collected bundle
by immutable ID") and 2 ("Recheck and render the atomic site") are this one kit invocation,
because the download is part of the authentication (§5.3.2 step 4) and must happen in the process
that renders. ``collected_dir`` and ``families_dir`` are therefore **new** work directories that
``build_site`` creates: it lists this run's artifacts (``GITHUB_RUN_ID``, exact attempt), requires
each ``mb-collected--<key>`` exactly once inside its ``Publish / Collect <key>`` job window and
downloads it by id with its digest, size and owner run into ``collected_dir/<key>/``; each
``mb-collected-family--<family>--<key>`` (present exactly for an available leg) likewise into
``families_dir/<family>/<key>/`` (layout: ``mod_base.family.paired`` and its selection record,
:func:`collected_family_selection`).

Steps: invocation checks (``GITHUB_*`` set, workflow ref is ``pages.yml`` on the default branch, no
``GIT_*``, clean checkouts); this run and exact attempt are ``in_progress``; the attempt's jobs by
exact names from :mod:`mod_base.workflow`; the downloads above; live heads (and, for enrolled
branches, tree and matrix blob) equal every bundle subject; each bundle validated against a
re-derived expectation, its embedded selection re-authenticated and bound
(``documents.check_compact_selection``), every derivative re-inspected; families re-run R4;
adapter ``verify_publication`` with the promotion draft (``documents.validate_promotion(draft=
True)``: no ``site`` yet); render into an ``atomic_directory`` stage from the allowlisted kit
``site/`` inventory plus generated files (``site-data.json``, ``e2e/gallery-data.json``,
``assets/theme.css``, images, ``.nojekyll``, optional icon), seal it; recheck steps 2-5; publish to
``output`` (``_site``), write ``_site/build.json`` and the final promotion (with ``site``) as
``promotion_dir/`` :data:`PROMOTION_FILE`, output ``heads`` and ``site_sha256``.

Details of this port:

* **Invocation.** ``build_site`` reads only :class:`~mod_base.runtime.Invocation` facts; the
  facts about its own host (no inherited ``GIT_*`` variable, both checkouts clean at ``GITHUB_SHA``
  and ``MOD_BASE_KIT_SHA``) are :func:`check_checkouts`, which the ``build`` and ``refresh``
  handlers run first. ``--kit-root`` must be the executing kit's own root, so the published front
  end is the digest-verified tree whose code renders it; the executing run's
  ``referenced_workflows`` must name that kit.
* **Expected legs.** The keys are re-derived with the adapter's ``targets`` (as ``admit`` did) and
  the family legs are every configured family for every key; each needs exactly one successful job
  of its exact name, and a ``Publish / Collect ...`` job or collected artifact naming anything else
  fails closed. The one exception is a publication without family legs, whose ``family`` matrix job
  its job-level ``if`` skips before the matrix expands: the jobs API may report it once under its
  unexpanded name (:func:`mod_base.workflow.unexpanded_api_job_name`), and then only
  ``completed/skipped``.
* **Heads.** A branch head is re-read with its tree; the tree binds every blob, the matrix
  included, so no matrix path has to be known by the kit.
* **Selections.** The embedded selection is recomputed without the selected artifact's bytes: the
  selected artifact by id (name, digest, size, owner run, creation time, not expired), its upload
  window (a handoff) or successful Pages owner (a cache; ``pages.yml`` by path and workflow id), the
  kit binding (the pin of ``source.workflow`` at the handoff run's head, or the cache owner's
  ``referenced_workflows``), the handoff and tested run records through their attempt endpoints
  (``source.workflow`` runs by path and workflow id on the canonical controller, started by an
  event admissible for the branch they tested, as ``authenticate`` requires), the attestation job,
  the exact job graph, the extensions (R6, ``authenticate_extensions``) and every counted field, then
  required byte-equal to the embedded ``selection.json``. Only ``source_manifest_sha256`` (a hash
  of bytes this job never downloads) and a composition (bound to the manifest's
  ``scope.components`` by ``check_compact_selection``; R3 proved its baseline owner at collect, and
  a baseline may expire before the composed cache that reuses it) are carried from the collected
  artifact, which this attempt's own collect job uploaded. The newest-run rule is not
  re-applied: a newer source run may start at any time and never invalidates authenticated
  evidence already selected for this publication.
* **Families.** An absent collected artifact means the leg was ``superseded`` or ``unavailable``
  (the collect job's exit 3); the build cannot tell which and records ``unavailable``, which the
  front end renders identically. An available leg is re-run through R4 (and R5 when carried), then
  its generation is authenticated (SPEC §1.8, §5.3.2 step 6; ``family_validate`` has no network):
  the envelope's producer attempt (attempt endpoint) must be a successful
  ``families[].producer.workflow`` run (path and workflow id) of this repository on the default
  branch at ``producer.commit``, started by one of ``producer.events``; the projection's
  ``provenance.producer`` must equal ``runs.run_record`` of that attempt exactly, so its
  ``created_at``, ``display_title`` and ``event`` are the API's (its ``job_graph_sha256``, when
  present, the graph of that attempt's jobs); and ``envelope.kit`` must be the pin of
  ``producer.workflow`` at the producer run's head (``authenticate.kit_binding`` as a
  ``family-handoff`` owned by the producer run, also for a family cache, which is a verbatim copy of
  the producer's bundle). The leg's ``selected_artifact_id`` (used only by rotation) is the
  generation the family job's ``select`` chose, recorded by ``family collect`` as the collected
  artifact's :data:`FAMILY_SELECTED_NAME` (the exact ``Selected`` object of ``select --output``, a
  ``family-handoff`` or ``family-cache`` of this leg) and re-authenticated by id, as a compact
  bundle's embedded selection is: the artifact must be listed, unexpired, by its recorded owner run
  (that run's inventory, read once for every leg it owns) with the recorded name, digest and size,
  within the family's archive limit (``select.family_archive_limit``, as ``select`` bounds both
  kinds). A ``family-handoff`` must be the envelope's producer attempt's own upload, usable as
  ``select`` takes it (at most one upload of that name at the producer's head, else fail closed;
  unexpired and within its archive limit). A ``family-cache`` must be owned by a successful earlier Pages run
  on the default branch at the recorded attempt and named with a commit of the key coverage's
  bounded first-parent history (``select.FamilyGenerations.history``, the only commits ``select``'s
  carry-forward walk probes) at or above the envelope's coverage (R5 again from the envelope's
  coverage to the cache's, which a later publication may have carried it to: ``refresh`` names a
  cache by the promoted coverage while its envelope keeps the producer's). Anything else fails
  closed. The walk itself is never repeated: its cost grows with the commits it probes (one
  exact-name listing per leg and commit), and, like the newest-run rule of ordinary bundles, a newer
  generation appearing after collection never invalidates the authenticated one this publication
  collected.
* **API budget** (``limits.MAX_PAGES_API_READS`` per job): every run, attempt, job list, artifact
  inventory, pin, workflow id and family kit binding is read (and every R5 proof run) at most once
  per invocation (the final recheck re-reads steps 2-5 by design); a selected artifact, ordinary or
  family, is looked up in its owner run's inventory; a collected artifact is downloaded by id from
  the metadata of this attempt's observed inventory (the final recheck observes it again) instead
  of re-reading it per artifact. What remains per leg is its download, however far below the
  coverage its family generation was produced (Quick Skin's 17 keys and family legs, carried up to
  three commits: ``tests/test_build_current_attempt.py`` ``ScaleBudgetTest``).
* **Project text.** ``project.description`` ``{"from_matrix": "a.b"}`` reads
  ``release/release-matrix.json`` (both mods' matrix) at the protected checkout and must be display
  text. The icon keeps its pixels and transparency and is re-encoded without metadata
  (:func:`_icon_png`: ``IHDR``, an indexed icon's ``PLTE``, ``tRNS`` and one ``IDAT`` deflated
  again from exactly the scanlines ``IHDR`` implies); ``pixelated`` rendering is a rule of the
  generated ``theme.css``. The ``theme-color`` meta is the midpoint of ``theme.dark.bg`` and
  ``theme.dark.surface`` (Quick Skin's hand-written ``#111713``); the ``color-scheme`` meta is
  ``dark``, or ``dark light`` when ``theme.light`` is set (``templating.color_scheme``).
* **Bounds.** Before any image is read, the distinct published images named by the validated
  manifests and projections, plus the kit files, must fit the site bounds (``MAX_SITE_FILES``,
  ``MAX_SITE_BYTES``); the seal enforces the final totals.
* **Output.** Every published file is written with ``write_new`` into the stage and sealed
  (``io.seal``); ``build.json`` is written into the stage before sealing and names the inventory of
  every other file (``site_inventory_sha256``, also the ``site_sha256`` output and the promotion's
  ``site``). The promotion is written into the new ``promotion_dir`` after the site is published.
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import struct
import subprocess
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from mod_base import PIXEL_METRICS_VERSION
from mod_base.adapter import host
from mod_base.adapter.protocol import HookUnsupported
from mod_base.errors import MbError
from mod_base.evidence import validate
from mod_base.evidence.compact import rederive
from mod_base.family.envelope import validate_envelope_dir
from mod_base.family.paired import (
    IMAGES_DIRECTORY,
    PROJECTION_NAME,
    SELECTED_NAME,
    SOURCE_DIRECTORY,
    validate_projection,
    verify_carry_forward,
)
from mod_base.github import artifacts, contents, jobs, runs
from mod_base.github.api import GitHubApi
from mod_base.imaging.metrics import ImageError
from mod_base.imaging.png import canonical_png
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.bounded_zip import LIMITS_BY_KIND, archive_limit, extract
from mod_base.io.seal import seal_output
from mod_base.io.tree import read_child_file
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, sha256_hex, strict_loads
from mod_base.model.validators import is_display_text
from mod_base.pages import templating
from mod_base.pages.authenticate import kit_binding
from mod_base.pages.select import FamilyGenerations, Selected, SourceRuns, family_archive_limit
from mod_base.pages.targets import discover_targets
from mod_base.pin import parse_pin_files
from mod_base.runtime import Invocation
from mod_base.workflow import (
    PAGES_EVENTS,
    PAGES_WORKFLOW_PATH,
    api_job_name,
    caller_job_name,
    find_job,
    unexpanded_api_job_name,
)

OWNER = "MB6"
#: The promotion's file name inside ``--promotion DIR`` and inside the ``mb-promotion`` artifact.
PROMOTION_FILE = "promotion.json"
#: The exact kit ``site/`` files copied into every published site (never a directory walk).
SITE_ALLOWLIST = ("index.html", "e2e/index.html", "assets/site.js", "assets/gallery.js", "assets/styles.css")
#: The allowlisted pages rendered through :mod:`mod_base.pages.templating`; the rest are copied.
TEMPLATES = ("index.html", "e2e/index.html")
BUILD_RECORD = "build.json"
SITE_DATA = "site-data.json"
GALLERY_DATA = "e2e/gallery-data.json"
THEME_CSS = "assets/theme.css"
ICON = "assets/icon.png"
NOJEKYLL = ".nojekyll"
#: Bound of one kit ``site/`` source file.
MAX_SITE_SOURCE_BYTES = 1 * lim.MIB
#: The matrix a ``{"from_matrix": ...}`` description is read from (Quick Skin and Block Pops).
MATRIX_PATH = "release/release-matrix.json"
MAX_MATRIX_BYTES = 4 * lim.MIB
MAX_ALT_CHARS = 400
#: The build job's id (``$GITHUB_JOB``), where ``targets``, ``expectation`` and the build hooks run.
BUILD_JOB = "build"

_GIT_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
_GIT_TIMEOUT_SECONDS = 120
_MAX_GIT_OUTPUT = 64 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
#: The only PNG chunks an icon may keep: every critical chunk plus the transparency table, in the
#: order PNG requires them.
_ICON_ORDER = (b"IHDR", b"PLTE", b"tRNS", b"IDAT", b"IEND")
_ICON_CHUNKS = frozenset(_ICON_ORDER)
#: Channels and allowed bit depths per PNG colour type.
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
#: The Adam7 passes ``(x0, y0, dx, dy)``.
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
#: The ``Selected`` record (``select --output``) of the family generation a leg was collected from,
#: written by ``family collect --selected-json`` at the root of ``mb-collected-family--<family>--<key>``
#: beside ``paired.json``, ``images/`` and ``source/`` (``family.paired.SELECTED_NAME``;
#: re-authenticated by ``build``, bound by ``refresh``).
FAMILY_SELECTED_NAME = SELECTED_NAME
#: The entries a collected family artifact's root must hold, and may hold.
_FAMILY_REQUIRED = frozenset({PROJECTION_NAME, SOURCE_DIRECTORY, FAMILY_SELECTED_NAME})
_FAMILY_ROOT = _FAMILY_REQUIRED | {IMAGES_DIRECTORY}
_FAMILY_KINDS = ("family-handoff", "family-cache")
#: The ``family`` job of a publication without family legs (:meth:`_Builder._check_jobs`).
_UNEXPANDED_FAMILY_JOB = unexpanded_api_job_name("publish", "family")


@dataclass(frozen=True)
class BuildResult:
    heads: dict[str, str]
    site_sha256: str
    promotion: dict[str, Any]


class BuildError(MbError):
    """The Pages inputs, this run or the rendered site fail a build check (exit 2)."""

    default_reason = "build"


def _fail(message: str, reason: str = "build") -> BuildError:
    return BuildError(message[:600], reason=reason)


# -- Invocation and checkouts -----------------------------------------------------------------------


def _decimal(invocation: Invocation, name: str, maximum: int) -> int:
    text = invocation.environ.get(name)
    if not grammar.is_match(grammar.POSITIVE_DECIMAL, text) or int(text) > maximum:  # type: ignore[arg-type]
        raise _fail(f"{name} must be a positive decimal", reason="environment")
    return int(text)  # type: ignore[arg-type]


def current_implementation(invocation: Invocation, *, jobs_allowed: Sequence[str]) -> dict[str, Any]:
    """SPEC §5.3.2 step 1: this process runs in this repository's ``pages.yml`` on the canonical
    branch, in one of ``jobs_allowed``; returns the promotion ``implementation`` of this run."""

    environ = invocation.environ
    repository = invocation.repository
    branch = invocation.config.canonical_branch
    if environ.get("GITHUB_REF") != f"refs/heads/{branch}":
        raise _fail(f"GITHUB_REF must be refs/heads/{branch}", reason="environment")
    reference = grammar.workflow_ref(repository, PAGES_WORKFLOW_PATH, branch)
    if environ.get("GITHUB_WORKFLOW_REF") != reference:
        raise _fail(f"GITHUB_WORKFLOW_REF must be {reference}", reason="environment")
    if invocation.github_job not in jobs_allowed:
        raise _fail(f"this command runs only in the job(s) {list(jobs_allowed)}", reason="environment")
    return {"branch": branch, "sha": invocation.implementation_sha, "workflow_ref": reference,
            "run_id": _decimal(invocation, "GITHUB_RUN_ID", lim.MAX_RUN_ID),
            "run_attempt": _decimal(invocation, "GITHUB_RUN_ATTEMPT", lim.MAX_RUN_ATTEMPT)}


def _git(root: Path, *arguments: str) -> str:
    """One read-only ``git`` query with a sanitized environment (no inherited ``GIT_*``, no user or
    system configuration, no fsmonitor, no hooks, no optional locks, no discovery above ``root``)."""

    executable = shutil.which("git", path=_GIT_SEARCH_PATH)
    if not executable:
        raise _fail(f"git is not installed in {_GIT_SEARCH_PATH}", reason="git")
    environment = {"PATH": _GIT_SEARCH_PATH, "HOME": os.devnull, "LANG": "C", "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
                   "GIT_CEILING_DIRECTORIES": str(root.parent)}
    command = [executable, "--no-replace-objects", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}",
               "-C", str(root), *arguments]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=environment, timeout=_GIT_TIMEOUT_SECONDS,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(f"git {arguments[0]} failed: {exc}", reason="git") from exc
    if completed.returncode != 0 or len(completed.stdout) > _MAX_GIT_OUTPUT:
        raise _fail(f"git {arguments[0]} failed in {root.name!r} (exit {completed.returncode})", reason="git")
    return completed.stdout.decode("utf-8", "replace")


def check_checkouts(invocation: Invocation, *, kit_root: Path, environ: Mapping[str, str]) -> None:
    """The host facts of SPEC §5.3.2 step 1: no inherited ``GIT_*`` variable in ``environ`` (the
    process environment, read by the command handler), the mod checkout clean at ``GITHUB_SHA`` and
    the kit checkout ``kit_root`` clean at ``MOD_BASE_KIT_SHA``."""

    inherited = sorted(name for name in environ if name.startswith("GIT_"))
    if inherited:
        raise _fail(f"inherited Git controls {inherited[:3]} must not reach a Pages kit command", reason="environment")
    for root, commit, label in ((Path(os.path.abspath(invocation.repo_root)), invocation.implementation_sha, "mod"),
                                (Path(os.path.abspath(kit_root)), invocation.kit["sha"], "kit")):
        if _git(root, "rev-parse", "--verify", "HEAD^{commit}").strip() != commit:
            raise _fail(f"the {label} checkout is not at {commit}", reason="checkout")
        if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
            raise _fail(f"the {label} checkout is not clean", reason="checkout")


def _new_directory(path: Path, label: str) -> Path:
    target = Path(os.path.abspath(path))
    try:
        os.mkdir(target, 0o700)
    except OSError as exc:
        raise _fail(f"the {label} must be a new directory: {exc.strerror or exc}", reason="output") from exc
    return target


def _timestamp(artifact: artifacts.Artifact) -> str:
    """The artifact's creation time in the ``TIMESTAMP`` form of the selection schema."""

    return artifact.order[0].astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _in_job_window(artifact: artifacts.Artifact, job: Mapping[str, Any]) -> bool:
    try:
        started = jobs.actions_time(job.get("started_at"), "job started_at")
        completed = jobs.actions_time(job.get("completed_at"), "job completed_at")
    except MbError:
        return False
    return started <= artifact.order[0] <= completed


def _is_success(run: Mapping[str, Any]) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") == "success"


def _uploaded_in_step(artifact: artifacts.Artifact, attempt_jobs: list[dict[str, Any]], job_name: str,
                      step_name: str, run_attempt: int) -> bool:
    """``artifact`` was created inside the successful step ``step_name`` of the successful job
    ``job_name`` of ``run_attempt`` (the exact producer upload)."""

    try:
        job = find_job(attempt_jobs, job_name, run_attempt=run_attempt)
        if not _is_success(job):
            return False
        jobs.require_successful_step(job, step_name)
        started, completed = jobs.step_window(job, step_name)
    except MbError:
        return False
    return started <= artifact.order[0] <= completed


def require_current_run(api: GitHubApi, invocation: Invocation, implementation: Mapping[str, Any]) -> dict[str, Any]:
    """SPEC §5.3.2 step 2: the API default branch is the canonical branch and this run and its exact
    attempt are the ``in_progress`` ``pages.yml`` run of the canonical head; returns the run."""

    default = invocation.config.canonical_branch
    if contents.default_branch(api) != default:
        raise _fail(f"config.canonical_branch {default} is not the repository's default branch",
                    reason="canonical-branch")
    run_id, attempt = implementation["run_id"], implementation["run_attempt"]
    run = runs.get_run(api, run_id)
    for record, label in ((run, "run"), (runs.get_run_attempt(api, run_id, attempt), "attempt")):
        head = record.get("head_repository")
        checks = (record.get("id") == run_id, record.get("run_attempt") == attempt,
                  record.get("path") == PAGES_WORKFLOW_PATH, record.get("event") in PAGES_EVENTS,
                  record.get("head_branch") == default, record.get("head_sha") == implementation["sha"],
                  isinstance(head, Mapping) and head.get("full_name") == invocation.repository,
                  record.get("status") == "in_progress", record.get("conclusion") is None)
        if not all(checks):
            raise _fail(f"this Pages {label} {run_id} attempt {attempt} is not the in-progress run of the protected head",
                        reason="current-run")
    return run


# -- Build -------------------------------------------------------------------------------------------


def collected_family_selection(root: Path, *, family: str, key: str, reason: str) -> Selected:
    """The layout of a downloaded ``mb-collected-family--<family>--<key>`` (exactly
    ``paired.json``, ``source/`` and :data:`FAMILY_SELECTED_NAME`, plus ``images/`` when the
    projection has images) and its selection record: the canonical JSON (read without following a
    symlink, at most ``limits.MAX_SELECTED_JSON_BYTES``) of the exact ``Selected`` object of a
    ``family-handoff`` or ``family-cache`` of ``family`` and ``key``. Only the record's form is
    checked here; ``build`` re-authenticates the artifact it names. Failures carry ``reason``."""

    entries = set(os.listdir(root))
    if not _FAMILY_REQUIRED <= entries <= _FAMILY_ROOT:
        raise _fail(f"the collected {family} artifact of {key} holds {sorted(entries)[:5]}", reason=reason)
    try:
        raw = read_child_file(root, FAMILY_SELECTED_NAME, max_bytes=lim.MAX_SELECTED_JSON_BYTES)
        value = strict_loads(raw, label=FAMILY_SELECTED_NAME, max_bytes=lim.MAX_SELECTED_JSON_BYTES)
        selected = Selected.parse(value)
    except MbError as exc:
        raise _fail(f"the collected {family} selection of {key} is malformed: {exc}", reason=reason) from exc
    if canonical_json(value) != raw:
        raise _fail(f"the collected {family} selection of {key} is not canonical JSON", reason=reason)
    parsed = grammar.parse_artifact_name(selected.name)
    if selected.kind not in _FAMILY_KINDS or parsed is None or (parsed.family, parsed.key) != (family, key):
        raise _fail(f"the collected {family} selection of {key} names {selected.name}, not a generation of this leg",
                    reason=reason)
    return selected


@dataclass
class _Leg:
    """One key (``family is None``) or family leg of this publication."""

    key: str
    family: str | None
    job: dict[str, Any]
    artifact: artifacts.Artifact | None = None
    root: Path | None = None


class _Builder:
    """The state of one ``build`` invocation (see the module docstring)."""

    def __init__(self, invocation: Invocation, api: GitHubApi, kit_root: Path) -> None:
        self.invocation = invocation
        self.api = api
        self.config = invocation.config
        self.repository = invocation.repository
        self.default = invocation.config.canonical_branch
        if Path(os.path.abspath(kit_root)).resolve() != Path(os.path.abspath(invocation.kit_root)).resolve():
            raise _fail("--kit-root must be the executing kit's own root", reason="usage")
        self.kit_root = Path(os.path.abspath(kit_root))
        self.implementation = current_implementation(invocation, jobs_allowed=(BUILD_JOB,))
        self.targets: list[dict[str, Any]] = []
        self.legs: list[_Leg] = []
        self.sources = SourceRuns(api, invocation)
        #: Only its memoized ``history`` (the commits ``select``'s walk may probe, read locally) is used:
        #: a collected generation is re-authenticated by id, never found again by walking.
        self.generations = FamilyGenerations(api, invocation, sources=self.sources)
        self._runs: dict[int, dict[str, Any]] = {}
        self._attempts: dict[tuple[int, int], dict[str, Any]] = {}
        self._attempt_jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._run_artifacts: dict[int, list[artifacts.Artifact]] = {}
        self._pins: dict[tuple[str, str], str] = {}
        self._family_kits: set[tuple[str, str, str, str]] = set()
        self._carries: set[tuple[str, str]] = set()

    # -- memoized reads (the Pages API budget is 160 reads per job) ------------------------------

    def run(self, run_id: int) -> dict[str, Any]:
        """The run ``run_id`` at its latest attempt (a cache owner)."""

        if run_id not in self._runs:
            self._runs[run_id] = runs.get_run(self.api, run_id)
        return self._runs[run_id]

    def attempt(self, run_id: int, run_attempt: int) -> dict[str, Any]:
        if (run_id, run_attempt) not in self._attempts:
            self._attempts[(run_id, run_attempt)] = runs.get_run_attempt(self.api, run_id, run_attempt)
        return self._attempts[(run_id, run_attempt)]

    def jobs_of(self, run_id: int, run_attempt: int) -> list[dict[str, Any]]:
        if (run_id, run_attempt) not in self._attempt_jobs:
            self._attempt_jobs[(run_id, run_attempt)] = jobs.attempt_jobs(self.api, run_id, run_attempt)
        return self._attempt_jobs[(run_id, run_attempt)]

    def artifacts_of(self, run_id: int) -> list[artifacts.Artifact]:
        if run_id not in self._run_artifacts:
            self._run_artifacts[run_id] = artifacts.list_for_run(self.api, run_id)
        return self._run_artifacts[run_id]

    def listed(self, run_id: int, artifact_id: int) -> artifacts.Artifact | None:
        """Artifact ``artifact_id`` as the inventory of its owner ``run_id`` lists it (one read for
        every artifact a run owns), or ``None``."""

        found = [artifact for artifact in self.artifacts_of(run_id) if artifact.id == artifact_id]
        return found[0] if len(found) == 1 else None

    def pin_at(self, workflow: str, head: str) -> str:
        if (workflow, head) not in self._pins:
            data = contents.file_at(self.api, workflow, head, max_bytes=lim.MAX_WORKFLOW_FILE_BYTES)
            self._pins[(workflow, head)] = parse_pin_files({workflow: data}).sha
        return self._pins[(workflow, head)]

    def carry_forward(self, carried_from: str, coverage_sha: str) -> None:
        """R5 (:func:`mod_base.family.paired.verify_carry_forward` on the inert objects of the
        protected checkout), proven once per commit pair: every leg carried alike shares it."""

        if (carried_from, coverage_sha) not in self._carries:
            verify_carry_forward(self.invocation.repo_root, carried_from, coverage_sha)
            self._carries.add((carried_from, coverage_sha))

    def workflow_id(self, path: str) -> int:
        """The id of this repository's workflow ``path`` (``GET /actions/workflows/<file>``), read
        once per build."""

        return self.sources.workflow_id(path)

    def of_workflow(self, run: Mapping[str, Any], path: str) -> bool:
        """``run`` belongs to this repository's workflow ``path`` by path and workflow id."""

        identity = run.get("workflow_id")
        return (run.get("path") == path and isinstance(identity, int) and not isinstance(identity, bool)
                and identity == self.workflow_id(path))

    # -- steps 2-5: this run, its jobs, its artifacts and the live heads ---------------------------

    def observe(self) -> str:
        """Steps 2-5 as one canonical observation (compared again by the final recheck)."""

        run_id, attempt = self.implementation["run_id"], self.implementation["run_attempt"]
        run = require_current_run(self.api, self.invocation, self.implementation)
        if runs.referenced_kit_sha(run) != self.invocation.kit["sha"]:
            raise _fail("this Pages run did not resolve the executing kit", reason="kit-binding")
        attempt_jobs = jobs.attempt_jobs(self.api, run_id, attempt)
        observed_jobs = self._check_jobs(attempt_jobs, attempt)
        listed = artifacts.list_for_run(self.api, run_id)
        observed_artifacts = self._check_artifacts(listed)
        heads = {}
        subjects = self.subjects()
        for branch in sorted({*subjects, self.default}):
            commit, tree = contents.branch_head(self.api, branch)
            subject = subjects.get(branch)
            current = commit == self.implementation["sha"] if subject is None else (commit, tree) == (
                subject["commit"], subject["tree"])
            if not current:
                raise _fail(f"{branch} advanced past the published head", reason="stale-subject")
            heads[branch] = [commit, tree]
        return canonical_json({"run": [run["event"], run["created_at"]], "jobs": observed_jobs,
                               "artifacts": observed_artifacts, "heads": heads}).decode("utf-8")

    def subjects(self) -> dict[str, dict[str, str]]:
        """Subject branch -> ``{commit, tree}`` over every key (one head per branch)."""

        found: dict[str, dict[str, str]] = {}
        for target in self.targets:
            subject = target["subject"]
            known = found.setdefault(subject["branch"], {"commit": subject["commit"], "tree": subject["tree"]})
            if known != {"commit": subject["commit"], "tree": subject["tree"]}:
                raise _fail(f"two keys name different heads of {subject['branch']}")
        if found.get(self.default, {}).get("commit", self.implementation["sha"]) != self.implementation["sha"]:
            raise _fail(f"a key names another head of {self.default} than the protected head")
        return found

    def _check_jobs(self, attempt_jobs: list[dict[str, Any]], attempt: int) -> list[list[Any]]:
        observed: list[list[Any]] = []

        def successful(name: str) -> dict[str, Any]:
            job = find_job(attempt_jobs, name, run_attempt=attempt)
            if job.get("status") != "completed" or job.get("conclusion") != "success":
                raise _fail(f"job {name!r} of this attempt did not succeed", reason="job-graph")
            observed.append([name, job.get("id"), "success"])
            return job

        successful(caller_job_name("verify_kit"))
        successful(api_job_name("publish", "admit"))
        building = find_job(attempt_jobs, api_job_name("publish", "build"), run_attempt=attempt)
        if building.get("status") != "in_progress" or building.get("conclusion") is not None:
            raise _fail("the build job of this attempt is not in progress", reason="job-graph")
        expected = set()
        for leg in self.legs:
            if leg.family is None:
                name = api_job_name("publish", "collect", key=leg.key)
            else:
                name = api_job_name("publish", "family", family=leg.family, key=leg.key)
            expected.add(name)
            leg.job = successful(name)
        if (not any(leg.family is not None for leg in self.legs)
                and any(job.get("name") == _UNEXPANDED_FAMILY_JOB for job in attempt_jobs)):
            # Without a family leg the ``family`` matrix job is skipped by its job-level ``if`` before
            # its matrix expands: the jobs API reports it once, under its unexpanded template name, as a
            # completed/skipped job of this attempt. Anything else under that name is a stray (with a
            # family leg, the name itself is one).
            skipped = find_job(attempt_jobs, _UNEXPANDED_FAMILY_JOB, run_attempt=attempt)
            if skipped.get("status") != "completed" or skipped.get("conclusion") != "skipped":
                raise _fail("the family job of a publication without family legs was not skipped", reason="job-graph")
            expected.add(_UNEXPANDED_FAMILY_JOB)
            observed.append([_UNEXPANDED_FAMILY_JOB, skipped.get("id"), "skipped"])
        collect_prefix = api_job_name("publish", "collect", key="k0").removesuffix("k0")
        strays = sorted(str(job.get("name")) for job in attempt_jobs
                        if str(job.get("name")).startswith(collect_prefix) and job.get("name") not in expected)
        if strays:
            raise _fail(f"this attempt ran collect jobs outside the publication: {strays[:3]}", reason="job-graph")
        return observed

    def _check_artifacts(self, listed: list[artifacts.Artifact]) -> list[list[Any]]:
        by_name: dict[str, list[artifacts.Artifact]] = {}
        for artifact in listed:
            parsed = grammar.parse_artifact_name(artifact.name)
            if parsed is not None and parsed.kind in ("collected", "collected-family") and not artifact.expired:
                by_name.setdefault(artifact.name, []).append(artifact)
        wanted = {grammar.collected_name(leg.key) if leg.family is None
                  else grammar.collected_family_name(leg.family, leg.key): leg for leg in self.legs}
        strays = sorted(set(by_name) - set(wanted))
        if strays:
            raise _fail(f"this run uploaded collected artifacts outside the publication: {strays[:3]}",
                        reason="artifact")
        observed: list[list[Any]] = []
        for name, leg in sorted(wanted.items()):
            found = by_name.get(name, [])
            if len(found) > 1 or (not found and leg.family is None):
                raise _fail(f"this run must hold exactly one {name}, found {len(found)}", reason="artifact")
            if not found:
                leg.artifact = None
                continue
            artifact = found[0]
            if (artifact.head_sha, artifact.head_branch) != (self.implementation["sha"], self.default):
                raise _fail(f"{name} names another head than this run", reason="artifact")
            if not _in_job_window(artifact, leg.job):
                raise _fail(f"{name} was not uploaded inside its collect job", reason="artifact")
            leg.artifact = artifact
            observed.append([name, artifact.id, artifact.digest, artifact.size, artifact.created_at])
        return observed

    # -- step 6: ordinary bundles -------------------------------------------------------------------

    def download(self, leg: _Leg, collected_dir: Path, families_dir: Path) -> None:
        """Download ``leg``'s collected artifact by immutable id into its new work directory.

        The metadata is this attempt's observed inventory (:meth:`observe`: this run, this head,
        inside the leg's job window, not expired), which the final recheck observes again, so it is
        not re-read per artifact as :func:`mod_base.github.artifacts.download` does; the size must
        fit the extraction bound before a byte is fetched, and the bytes must be exactly that size
        and hash to that digest before the bounded extraction."""

        artifact = leg.artifact
        assert artifact is not None
        if leg.family is None:
            output, kind = collected_dir / leg.key, "collected"
        else:
            family_root = families_dir / leg.family
            if not family_root.exists():
                _new_directory(family_root, "family work directory")
            output, kind = family_root / leg.key, "collected-family"
        extraction = LIMITS_BY_KIND[kind]
        if not 0 < artifact.size <= min(artifacts.MAX_ARCHIVE_BYTES, archive_limit(extraction)):
            raise _fail(f"{artifact.name} exceeds its extraction bound", reason="artifact")
        data = self.api.download(f"/repos/{self.repository}/actions/artifacts/{artifact.id}/zip",
                                 max_bytes=artifact.size)
        if len(data) != artifact.size or "sha256:" + hashlib.sha256(data).hexdigest() != artifact.digest:
            raise _fail(f"{artifact.name} does not match its size and digest", reason="artifact")
        extract(data, output, extraction)
        leg.root = output

    def bundle(self, target: Mapping[str, Any], root: Path) -> dict[str, Any]:
        """Validate one collected compact bundle; returns the render input of :func:`render_site`."""

        key, subject = target["key"], target["subject"]
        bundle = validate.load_compact(self.invocation, root, key=key, expected_subject_commit=subject["commit"])
        validate.check_compact_pixels(bundle)
        manifest, selection = bundle.manifest, bundle.selection
        assert selection is not None
        if manifest["subject"] != subject:
            raise _fail(f"the collected bundle of {key} covers another subject than its target", reason="stale-subject")
        if manifest["kit"] != self.invocation.kit or selection["kit"] != self.invocation.kit:
            raise _fail(f"the collected bundle of {key} was not compacted by the executing kit", reason="kit-binding")
        if selection["implementation"] != self.implementation:
            raise _fail(f"the collected bundle of {key} was not authenticated by this Pages attempt",
                        reason="selection-binding")
        expectation = rederive(self.invocation, bundle, selection)
        recomputed = self.reauthenticate(bundle)
        if canonical_json(recomputed) != bundle.selection_raw:
            raise _fail(f"the embedded selection of {key} does not re-authenticate", reason="selection-binding")
        return {"root": root, "manifest": manifest, "expectation": expectation, "selection": selection,
                "manifest_sha256": sha256_hex(bundle.manifest_raw)}

    def reauthenticate(self, bundle: validate.Bundle) -> dict[str, Any]:
        """The embedded selection recomputed from the API and the bundle (see module docstring)."""

        manifest, selection = bundle.manifest, bundle.selection
        assert selection is not None
        key, subject, provenance = manifest["key"], manifest["subject"], manifest["provenance"]
        handoff, tested, reuse = provenance["handoff"], provenance["tested"], provenance["reuse"]
        source = self.config.source
        recorded = selection["selected_artifact"]
        artifact = self.listed(recorded["run_id"], recorded["id"])
        if artifact is None or artifact.expired or (artifact.name, artifact.digest, artifact.size) != (
                recorded["name"], recorded["digest"], recorded["size"]):
            raise _fail(f"the selected artifact {recorded['id']} of {key} changed or expired", reason="artifact")
        kind = recorded["kind"]
        if kind == "handoff":
            if (artifact.name != grammar.handoff_name(key, handoff["run_attempt"])
                    or (artifact.run_id, recorded["run_attempt"]) != (handoff["run_id"], handoff["run_attempt"])
                    or (artifact.head_sha, artifact.head_branch) != (handoff["commit"], handoff["branch"])):
                raise _fail(f"the selected handoff of {key} is not the upload of its handoff run", reason="artifact")
            if not _uploaded_in_step(artifact, self.jobs_of(handoff["run_id"], handoff["run_attempt"]),
                                     source["handoff_job"].replace("{key}", key), source["handoff_step"],
                                     handoff["run_attempt"]):
                raise _fail(f"the selected handoff of {key} was not uploaded by the successful source.handoff_step",
                            reason="artifact")
            binding = {"source": "workflow_file", "sha": self.pin_at(source["workflow"], handoff["controller_sha"])}
            workflow_path = source["workflow"]
        else:
            owner = self.attempt(artifact.run_id, recorded["run_attempt"])
            if (artifact.name != grammar.cache_name(key, subject["commit"]) or artifact.head_branch != self.default
                    or not self.pages_owner(owner, artifact)):
                raise _fail(f"the selected cache of {key} is not owned by a successful earlier Pages run",
                            reason="artifact")
            binding = {"source": "referenced_workflows", "sha": runs.referenced_kit_sha(owner)}
            workflow_path = PAGES_WORKFLOW_PATH
        handoff_run = self.attempt(handoff["run_id"], handoff["run_attempt"])
        self._require_source_run(handoff_run, handoff, tested_branch=subject["branch"], label="handoff run")
        handoff_record = runs.run_record(handoff_run, handoff)
        record: dict[str, Any] = {"handoff_run": handoff_record, "reuse": reuse, "kit_binding": binding}
        if reuse == "none":
            tested_record = runs.run_record(handoff_run, tested)
        elif reuse == "attested":
            name = source["attestation_job"]
            if name is None:
                raise _fail("attested reuse needs a configured source.attestation_job", reason="reuse")
            job = find_job(self.jobs_of(handoff["run_id"], handoff["run_attempt"]), name,
                           run_attempt=handoff["run_attempt"])
            if job.get("status") != "completed" or job.get("conclusion") != "success":
                raise _fail(f"the attestation job of {key} did not succeed", reason="reuse")
            record["attestation_job"] = {"name": name, "id": grammar.require_positive_int(job.get("id"), "job id"),
                                         "conclusion": "success"}
            tested_run = self.attempt(tested["run_id"], tested["run_attempt"])
            self._require_source_run(tested_run, tested, tested_branch=tested["branch"], label="tested run")
            tested_record = runs.run_record(tested_run, tested)
        else:
            tested_run = self.attempt(tested["run_id"], tested["run_attempt"])
            if not self.of_workflow(tested_run, source["workflow"]) or not _is_success(tested_run):
                raise _fail(f"the delegated tested run of {key} is not a successful source run", reason="reuse")
            tested_record = runs.run_record(tested_run, tested, require_controller_head=False)
        if source["require_job_graph"]:
            expected = host.call(self.invocation, "expected_source_jobs",
                                 {"expectation": bundle.expectation, "tested_run": tested_record})
            if expected is None:
                raise _fail("source.require_job_graph needs the adapter's expected_source_jobs graph",
                            reason="job-graph")
            graph = jobs.require_job_graph(self.jobs_of(tested["run_id"], tested["run_attempt"]), expected)
            tested_record = {**tested_record, "job_graph_sha256": graph}
            record["job_graph_sha256"] = graph
        record["tested_run"] = tested_record
        verified: list[str] = []
        if manifest["extensions"] is not None:
            result = host.call(self.invocation, "authenticate_extensions",
                               {"manifest": manifest, "extensions": bundle.extensions},
                               network="authenticate_extensions" in self.config.network_hooks)
            verified = validate.check_extensions_verified(manifest, result)
        recomputed: dict[str, Any] = {
            "kind": "mod-base.selection",
            "schema_version": 1,
            "repository": self.repository,
            "key": key,
            "kit": self.invocation.kit,
            "implementation": self.implementation,
            "subject": subject,
            "coverage_sha": provenance["coverage_sha"],
            "selected_artifact": {"kind": kind, "id": artifact.id, "name": artifact.name, "digest": artifact.digest,
                                  "size": artifact.size, "run_id": artifact.run_id,
                                  "run_attempt": recorded["run_attempt"], "workflow_path": workflow_path,
                                  "created_at": _timestamp(artifact)},
            "source": record,
            "expectation_sha256": manifest["expectation"]["sha256"],
            "source_manifest_sha256": selection["source_manifest_sha256"],
            "extensions_verified": verified,
            "manifest_sha256": documents.compact_identity_sha256(manifest),
            "binding": {"mode": "reencode-identical" if kind == "handoff" else "cache-revalidated",
                        "frames": len(manifest["frames"]),
                        "derivatives": len({frame["derivative"]["path"] for frame in manifest["frames"]})},
        }
        if "composition" in selection:
            recomputed["composition"] = selection["composition"]
        return documents.validate_selection(recomputed)

    def _require_source_run(self, run: Mapping[str, Any], claim: Mapping[str, Any], *, tested_branch: str,
                            label: str) -> None:
        """A successful ``source.workflow`` attempt (path and workflow id) of this repository on the
        canonical branch at the claim's own head, started by an event admissible for the branch it
        tested: ``events.canonical`` for the canonical branch, else also ``events.other`` (the rule of
        ``authenticate``; the display title and the rest of the policy were applied at collect, and
        the byte-equal run records prove the API still reports those facts)."""

        source, canonical = self.config.source, self.default
        events = set(source["events"]["canonical"])
        if tested_branch != canonical:
            events &= set(source["events"]["other"])
        head = run.get("head_repository")
        if not (self.of_workflow(run, source["workflow"]) and claim["workflow_path"] == source["workflow"]
                and _is_success(run) and isinstance(head, Mapping) and head.get("full_name") == self.repository
                and isinstance(run.get("event"), str) and run.get("event") in events
                and run.get("head_branch") == canonical == claim["controller_branch"]
                and run.get("head_sha") == claim["controller_sha"]):
            raise _fail(f"the {label} {claim['run_id']} attempt {claim['run_attempt']} failed provenance",
                        reason="source-authentication")

    def pages_owner(self, run: Mapping[str, Any], artifact: artifacts.Artifact) -> bool:
        """``run`` is a successful earlier ``pages.yml`` run (path and workflow id) of this repository
        on the default branch at ``artifact``'s head (the owner of a cache), never this run."""

        head = run.get("head_repository")
        return (self.of_workflow(run, PAGES_WORKFLOW_PATH) and _is_success(run) and run.get("event") in PAGES_EVENTS
                and run.get("head_branch") == self.default and run.get("head_sha") == artifact.head_sha
                and isinstance(head, Mapping) and head.get("full_name") == self.repository
                and run.get("id") == artifact.run_id and run.get("id") != self.implementation["run_id"])

    # -- step 7: families -----------------------------------------------------------------------------

    def family(self, leg: _Leg, coverage_sha: str) -> dict[str, Any]:
        """R4 (and R5 for a carried generation) on one downloaded family leg, then its generation's
        producer run, run record and kit, and the artifact it was collected from (module docstring)."""

        assert leg.family is not None and leg.root is not None
        root = leg.root
        selected = collected_family_selection(root, family=leg.family, key=leg.key, reason="family-projection")
        envelope = validate_envelope_dir(self.invocation, root / SOURCE_DIRECTORY, family=leg.family, key=leg.key)
        projection = validate_projection(self.invocation, root / PROJECTION_NAME, images_root=root,
                                         family=leg.family, key=leg.key, expected_coverage_sha=coverage_sha)
        config = self.config.family(leg.family)
        if projection["subject"] != envelope["subject"]:
            raise _fail(f"the {leg.family} projection of {leg.key} is not its envelope's generation",
                        reason="family-projection")
        if "carried_from" in envelope:
            self.carry_forward(envelope["carried_from"], envelope["coverage_sha"])
        if envelope["coverage_sha"] != coverage_sha:
            if not config["carry_forward"]:
                raise _fail(f"family {leg.family} does not allow carry-forward", reason="carry-forward")
            self.carry_forward(envelope["coverage_sha"], coverage_sha)
        producer = self.producer_run(leg, config, envelope)
        self.require_producer_record(leg, envelope, projection, producer)
        self.bind_family_kit(envelope, producer)
        return {"projection": projection,
                "selected_artifact_id": self.family_selection(leg, config, envelope, coverage_sha, selected)}

    def producer_run(self, leg: _Leg, family: Mapping[str, Any], envelope: Mapping[str, Any]) -> dict[str, Any]:
        """The envelope's producer attempt (attempt endpoint, read once): a successful
        ``families[].producer.workflow`` run (path and workflow id) of this repository on the default
        branch at ``producer.commit``, started by one of ``producer.events`` (SPEC §1.8: the owner a
        family generation's kit is bound to)."""

        claim, producer = envelope["producer"], family["producer"]
        run = self.attempt(claim["run_id"], claim["run_attempt"])
        head, event = run.get("head_repository"), run.get("event")
        if not (self.of_workflow(run, producer["workflow"]) and claim["workflow_path"] == producer["workflow"]
                and _is_success(run) and isinstance(event, str) and event in producer["events"]
                and run.get("id") == claim["run_id"] and run.get("head_branch") == self.default
                and run.get("head_sha") == claim["commit"]
                and isinstance(head, Mapping) and head.get("full_name") == self.repository):
            raise _fail(f"the {leg.family} producer run {claim['run_id']} attempt {claim['run_attempt']} of {leg.key} "
                        "failed provenance", reason="family-provenance")
        return run

    def require_producer_record(self, leg: _Leg, envelope: Mapping[str, Any], projection: Mapping[str, Any],
                                run: Mapping[str, Any]) -> None:
        """The projection's ``provenance.producer`` is exactly the producer attempt's run record, so
        its ``created_at``, ``display_title`` and ``event`` are the API's, never adapter claims; a
        ``job_graph_sha256``, when present, must be the graph of that attempt's jobs."""

        claim = envelope["producer"]
        recorded = projection["provenance"]["producer"]
        expected = runs.run_record(run, claim)
        if "job_graph_sha256" in recorded:
            observed = self.jobs_of(claim["run_id"], claim["run_attempt"])
            expected["job_graph_sha256"] = jobs.job_graph_sha256(jobs.job_graph(observed))
        if expected != recorded:
            raise _fail(f"the {leg.family} projection of {leg.key} does not carry its producer run's record",
                        reason="family-provenance")

    def bind_family_kit(self, envelope: Mapping[str, Any], producer: Mapping[str, Any]) -> None:
        """``envelope.kit`` is the pin of ``families[].producer.workflow`` at the producer run's head
        (SPEC §1.8), for a family handoff and a family cache alike, since ``refresh`` copies the
        producer's bundle verbatim into the cache: :func:`mod_base.pages.authenticate.kit_binding` as
        a ``family-handoff`` owned by the producer run, read once per workflow, head and kit."""

        kit = envelope["kit"]
        memo = (producer["path"], producer["head_sha"], kit["sha"], kit["version"])
        if memo not in self._family_kits:
            kit_binding(self.api, self.invocation, manifest=envelope, owner_run=producer,
                        selected_kind="family-handoff")
            self._family_kits.add(memo)

    def producer_handoff(self, leg: _Leg, family: Mapping[str, Any],
                         claim: Mapping[str, Any]) -> artifacts.Artifact | None:
        """The family handoff of the envelope's producer attempt, as ``select`` takes it: the
        producer run's inventory (read once) must hold at most one upload of that name, at the
        producer's head on the default branch (anything else fails closed); it is usable when
        unexpired and within the family's archive limit (``select.family_archive_limit``), else
        ``None``."""

        assert leg.family is not None
        name = grammar.family_handoff_name(leg.family, leg.key, claim["run_attempt"])
        uploads = [artifact for artifact in self.artifacts_of(claim["run_id"]) if artifact.name == name]
        if len(uploads) > 1 or any((artifact.head_sha, artifact.head_branch) != (claim["commit"], self.default)
                                   for artifact in uploads):
            raise _fail(f"the {leg.family} producer run {claim['run_id']} does not own exactly one {name} of its head",
                        reason="family-projection")
        if not uploads or uploads[0].expired or uploads[0].size > family_archive_limit(family):
            return None
        return uploads[0]

    def family_selection(self, leg: _Leg, family: Mapping[str, Any], envelope: Mapping[str, Any],
                         coverage_sha: str, selected: Selected) -> int:
        """The id of the family generation the family job collected (its :data:`FAMILY_SELECTED_NAME`
        record), re-authenticated by id at a cost independent of how far ``select`` walked (module
        docstring); anything that is not an admissible generation of this envelope fails closed."""

        assert leg.family is not None
        label = f"the {leg.family} generation {selected.artifact_id} collected for {leg.key}"
        artifact = self.listed(selected.run_id, selected.artifact_id)
        if (artifact is None or artifact.expired or artifact.run_id != selected.run_id
                or (artifact.name, artifact.digest, artifact.size) != (selected.name, selected.digest, selected.size)):
            raise _fail(f"{label} is not listed unexpired, with its recorded name, digest and size, by its owner run "
                        f"{selected.run_id}", reason="family-selection")
        if artifact.size > family_archive_limit(family):
            raise _fail(f"{label} exceeds the family's archive bound", reason="family-selection")
        producer = envelope["producer"]
        if selected.kind == "family-handoff":
            if ((selected.run_id, selected.run_attempt) != (producer["run_id"], producer["run_attempt"])
                    or self.producer_handoff(leg, family, producer) != artifact):
                raise _fail(f"{label} is not the usable family handoff of its envelope's producer attempt",
                            reason="family-selection")
            return artifact.id
        owner = self.run(artifact.run_id)
        if (artifact.head_branch != self.default or not self.pages_owner(owner, artifact)
                or owner.get("run_attempt") != selected.run_attempt):
            raise _fail(f"{label} is not a family cache owned by a successful earlier Pages run attempt",
                        reason="family-selection")
        parsed = grammar.parse_artifact_name(selected.name)
        named = None if parsed is None else parsed.coverage_sha
        if named is None or named not in self.generations.history(coverage_sha):
            raise _fail(f"{label} is named with {named}, outside the bounded first-parent history of {coverage_sha}",
                        reason="family-selection")
        if named != envelope["coverage_sha"]:
            # The cache's own publication carried this envelope forward to the cache's coverage (R5).
            self.carry_forward(envelope["coverage_sha"], named)
        return artifact.id


def _heads(builder: _Builder) -> dict[str, str]:
    heads = {builder.default: builder.implementation["sha"]}
    heads.update({branch: subject["commit"] for branch, subject in builder.subjects().items()})
    return dict(sorted(heads.items()))


def build_site(invocation: Invocation, *, api: GitHubApi, kit_root: Path, collected_dir: Path, families_dir: Path,
               output: Path, promotion_dir: Path) -> BuildResult:
    """Render and publish the atomic site (see module docstring); ``output`` must not exist."""

    builder = _Builder(invocation, api, kit_root)
    for path, label in ((output, "site output"), (promotion_dir, "promotion directory")):
        if os.path.lexists(path):
            raise _fail(f"the {label} must not exist", reason="output")
    builder.targets = discover_targets(invocation, api=api)
    keys = [target["key"] for target in builder.targets]
    builder.legs = [_Leg(key=key, family=None, job={}) for key in keys]
    builder.legs += [_Leg(key=key, family=family["id"], job={}) for family in invocation.config.families for key in keys]
    observation = builder.observe()

    collected = _new_directory(collected_dir, "collected work directory")
    families_root = _new_directory(families_dir, "family work directory")
    for leg in builder.legs:
        if leg.artifact is not None:
            builder.download(leg, collected, families_root)

    bundles = []
    by_key = {leg.key: leg for leg in builder.legs if leg.family is None}
    for target in builder.targets:
        leg = by_key[target["key"]]
        assert leg.root is not None
        bundle = builder.bundle(target, leg.root)
        bundle["collected"] = leg.artifact
        bundles.append(bundle)
    coverage = {bundle["manifest"]["key"]: bundle["manifest"]["provenance"]["coverage_sha"] for bundle in bundles}
    families: list[dict[str, Any]] = []
    for leg in builder.legs:
        if leg.family is None:
            continue
        entry: dict[str, Any] = {"family": leg.family, "key": leg.key, "status": "unavailable", "root": None,
                                 "projection": None}
        if leg.artifact is not None:
            entry.update(builder.family(leg, coverage[leg.key]), status="available", root=leg.root,
                         collected=leg.artifact)
        families.append(entry)

    heads = _heads(builder)
    draft = {
        "kind": "mod-base.promotion",
        "schema_version": 1,
        "repository": builder.repository,
        "implementation": builder.implementation,
        "kit": invocation.kit,
        "heads": heads,
        "bundles": [{"key": bundle["manifest"]["key"], "collected_artifact_id": bundle["collected"].id,
                     "collected_digest": bundle["collected"].digest, "manifest_sha256": bundle["manifest_sha256"],
                     "coverage_sha": bundle["manifest"]["provenance"]["coverage_sha"],
                     "selected_artifact_id": bundle["manifest"]["source_artifact"]["id"]} for bundle in bundles],
        "families": [_promoted_family(entry, coverage) for entry in families],
    }
    documents.validate_promotion(draft, draft=True)
    try:
        host.call(invocation, "verify_publication", {"promotion_draft": draft},
                  network="verify_publication" in invocation.config.network_hooks)
    except HookUnsupported:
        pass

    def writer(stage: Path, stage_fd: int) -> dict[str, bytes]:
        written = render_site(invocation, kit_root=builder.kit_root, bundles=bundles, families=families,
                              stage_fd=stage_fd)
        rechecks: list[Any] = []
        seal_output(stage_fd, written, rechecks=rechecks)
        if builder.observe() != observation:
            raise _fail("this run, its jobs, its artifacts or a subject head changed while the site was rendered",
                        reason="stale-subject")
        for recheck in rechecks:
            recheck()
        return written

    written = atomic_directory(Path(output), writer)
    records = _inventory(written)
    promotion = {**draft, "site": {"files": len(records), "bytes": sum(record["size"] for record in records),
                                   "inventory_sha256": documents.inventory_sha256(records)}}
    documents.validate_promotion(promotion)
    data = canonical_json(promotion)
    if len(data) > lim.MAX_PROMOTION_BYTES:
        raise _fail(f"the promotion exceeds {lim.MAX_PROMOTION_BYTES} bytes")

    def promotion_writer(stage: Path, stage_fd: int) -> None:
        write_new(stage_fd, PROMOTION_FILE, data)
        seal_output(stage_fd, {PROMOTION_FILE: data}, max_files=1, max_bytes=lim.MAX_PROMOTION_BYTES)

    atomic_directory(Path(promotion_dir), promotion_writer)
    return BuildResult(heads=heads, site_sha256=promotion["site"]["inventory_sha256"], promotion=promotion)


def _promoted_family(entry: Mapping[str, Any], coverage: Mapping[str, str]) -> dict[str, Any]:
    if entry["status"] != "available":
        return {"family": entry["family"], "key": entry["key"], "available": False, "status": entry["status"]}
    return {"family": entry["family"], "key": entry["key"], "available": True, "status": "available",
            "collected_artifact_id": entry["collected"].id, "collected_digest": entry["collected"].digest,
            "coverage_sha": coverage[entry["key"]], "selected_artifact_id": entry["selected_artifact_id"]}


def _inventory(written: Mapping[str, bytes]) -> list[dict[str, Any]]:
    """``[{path, sha256, size}]`` of every published file except ``build.json`` (SCHEMAS)."""

    return sorted(({"path": path, "sha256": sha256_hex(data), "size": len(data)}
                   for path, data in written.items() if path != BUILD_RECORD), key=lambda record: record["path"])


# -- Rendering ---------------------------------------------------------------------------------------


def _from_matrix(invocation: Invocation, dotted: str) -> str:
    raw = read_child_file(invocation.repo_root, MATRIX_PATH, max_bytes=MAX_MATRIX_BYTES)
    value: Any = strict_loads(raw, label=MATRIX_PATH, max_bytes=MAX_MATRIX_BYTES)
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise _fail(f"{MATRIX_PATH} has no {dotted}", reason="project")
        value = value[part]
    if not is_display_text(value, 400):
        raise _fail(f"{MATRIX_PATH} {dotted} is not display text of at most 400 characters", reason="project")
    return value


def _project(invocation: Invocation) -> dict[str, Any]:
    """The ``site-data.project`` of this mod (text from the config, URLs derived)."""

    project = invocation.config.project
    description = project["description"]
    if isinstance(description, dict):
        description = _from_matrix(invocation, description["from_matrix"])
    repository_url = f"https://github.com/{invocation.repository}"
    return {"name": project["name"], "tagline": project["tagline"], "eyebrow": project["eyebrow"],
            "description": description, "license_label": project["license_label"],
            "repository_url": repository_url, "issues_url": f"{repository_url}/issues",
            "icon": ICON if project["icon"] is not None else None,
            "links": [dict(link) for link in project["links"]]}


def _icon_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    """``[(type, body)]`` of every chunk of ``data`` through ``IEND``, each CRC verified; nothing
    may follow ``IEND`` and no unknown critical chunk may appear."""

    if not data.startswith(_PNG_SIGNATURE):
        raise _fail("the project icon is not a PNG", reason="project")
    chunks, position = [], len(_PNG_SIGNATURE)
    while position < len(data):
        if position + 12 > len(data):
            raise _fail("the project icon has a truncated chunk", reason="project")
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        end = position + 12 + length
        if end > len(data) or not kind.isalpha():
            raise _fail("the project icon has a malformed chunk", reason="project")
        body = data[position + 8:end - 4]
        if struct.unpack(">I", data[end - 4:end])[0] != zlib.crc32(kind + body):
            raise _fail("the project icon has a chunk with a wrong CRC", reason="project")
        if kind[0:1].isupper() and kind not in _ICON_CHUNKS:
            raise _fail(f"the project icon has an unknown critical chunk {kind!r}", reason="project")
        chunks.append((kind, body))
        position = end
        if kind == b"IEND":
            break
    if position != len(data) or not chunks or chunks[-1] != (b"IEND", b""):
        raise _fail("the project icon does not end with one empty IEND chunk", reason="project")
    return chunks


def _scanline_bytes(width: int, height: int, bits_per_pixel: int, interlaced: bool) -> list[int]:
    """The length of every filtered scanline (filter byte included), in stream order."""

    passes = _ADAM7 if interlaced else ((0, 0, 1, 1),)
    lengths = []
    for x0, y0, dx, dy in passes:
        columns = (width - x0 + dx - 1) // dx if width > x0 else 0
        rows = (height - y0 + dy - 1) // dy if height > y0 else 0
        if columns and rows:
            lengths += [1 + (columns * bits_per_pixel + 7) // 8] * rows
    return lengths


def _icon_png(data: bytes) -> bytes:
    """The icon re-encoded without metadata, keeping its pixels and transparency.

    Only ``IHDR``, ``PLTE`` (indexed colour), ``tRNS`` and one ``IDAT`` are written: the chunks are
    parsed with every CRC verified and in PNG order, the concatenated ``IDAT`` stream must inflate to
    exactly the scanlines ``IHDR`` implies (valid filter bytes, nothing before or after the zlib
    stream, no extra rows) and is deflated again deterministically; the result must decode through
    the kit's imaging (``canonical_png``)."""

    chunks = _icon_chunks(data)
    listed = [kind for kind, _ in chunks]
    kinds = [kind for kind in listed if kind in _ICON_CHUNKS]
    positions = [_ICON_ORDER.index(kind) for kind in kinds]
    idat = [index for index, kind in enumerate(listed) if kind == b"IDAT"]
    # IHDR first, then PLTE, tRNS, the consecutive IDATs and IEND, each other chunk at most once.
    if (listed[0] != b"IHDR" or positions != sorted(positions)
            or any(kinds.count(kind) != 1 for kind in set(kinds) - {b"IDAT"})
            or not idat or idat != list(range(idat[0], idat[-1] + 1))):
        raise _fail("the project icon's chunks are missing, repeated or out of order", reason="project")
    header = chunks[0][1]
    if len(header) != 13:
        raise _fail("the project icon has a malformed IHDR chunk", reason="project")
    width, height, depth, colour, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    channels = _PNG_CHANNELS.get(colour)
    if (channels is None or depth not in _PNG_DEPTHS[colour] or (compression, filtering) != (0, 0)
            or interlace not in (0, 1) or not (0 < width <= lim.MAX_ICON_DIMENSION)
            or not (0 < height <= lim.MAX_ICON_DIMENSION)):
        raise _fail("the project icon has an unsupported IHDR (type, depth, method or size)", reason="project")
    bodies = {kind: body for kind, body in chunks if kind in (b"PLTE", b"tRNS")}
    palette = bodies.get(b"PLTE")
    if colour == 3 and (palette is None or not palette or len(palette) % 3 or len(palette) // 3 > 1 << depth):
        raise _fail("the indexed project icon needs a palette of at most 2^depth entries", reason="project")
    if colour in (0, 4) and palette is not None:
        raise _fail("a grey project icon may not carry a palette", reason="project")
    transparency = bodies.get(b"tRNS")
    if transparency is not None and not (
            (colour == 0 and len(transparency) == 2) or (colour == 2 and len(transparency) == 6)
            or (colour == 3 and palette is not None and 0 < len(transparency) <= len(palette) // 3)):
        raise _fail("the project icon has a malformed tRNS chunk", reason="project")
    scanlines = _scanline_bytes(width, height, channels * depth, interlace == 1)
    expected = sum(scanlines)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(b"".join(body for kind, body in chunks if kind == b"IDAT"), expected + 1)
    except zlib.error as exc:
        raise _fail(f"the project icon's image stream does not inflate: {exc}", reason="project") from exc
    if len(raw) != expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise _fail("the project icon's image stream is not exactly its IHDR scanlines", reason="project")
    position = 0
    for length in scanlines:
        if raw[position] > 4:
            raise _fail("the project icon has an invalid scanline filter", reason="project")
        position += length
    parts = [(b"IHDR", header)]
    if colour == 3:
        parts.append((b"PLTE", palette))
    if transparency is not None:
        parts.append((b"tRNS", transparency))
    parts += [(b"IDAT", zlib.compress(raw, 9)), (b"IEND", b"")]
    icon = _PNG_SIGNATURE + b"".join(struct.pack(">I", len(body)) + kind + body
                                     + struct.pack(">I", zlib.crc32(kind + body)) for kind, body in parts)
    try:
        canonical_png(icon)
    except ImageError as exc:
        raise _fail(f"the project icon does not decode: {exc}", reason="project") from exc
    return icon


def _version_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in value.split("."))


def _actions_url(invocation: Invocation) -> str:
    """The workflow page of ``config.source.workflow`` (SPEC §3.9 ``project.actions_url``)."""

    workflow = posixpath.basename(invocation.config.source["workflow"])
    return f"https://github.com/{invocation.repository}/actions/workflows/{workflow}"


def _loader_name(invocation: Invocation, loader: str) -> str:
    return invocation.config.labels["loaders"].get(loader, loader)


def _alt(frame: Mapping[str, Any], loader_name: str, role_label: str) -> str:
    text = (f"{frame['title']} in Minecraft {frame['minecraft']} on {loader_name}, {role_label}. "
            f"Expected view: {frame['expectation']}")
    if len(text) <= MAX_ALT_CHARS:
        return text
    return text[:MAX_ALT_CHARS - 1].rstrip() + "…"


def _read_image(root: Path, record: Mapping[str, Any]) -> bytes:
    data = read_child_file(root, record["path"], max_bytes=lim.MAX_DERIVATIVE_BYTES)
    if len(data) != record["size"] or sha256_hex(data) != record["sha256"]:
        raise _fail(f"{record['path']} changed after it was validated", reason="image")
    return data


def _gallery_release(invocation: Invocation, bundle: Mapping[str, Any]) -> dict[str, Any]:
    manifest, expectation, selection = bundle["manifest"], bundle["expectation"], bundle["selection"]
    repository = invocation.repository
    lanes = manifest["lanes"]
    loaders = sorted({lane["loader"] for lane in lanes}, key=lambda loader: (_loader_name(invocation, loader), loader))
    subject = manifest["subject"]
    handoff = selection["source"]["handoff_run"]
    tested = selection["source"]["tested_run"]
    return {
        "key": manifest["key"],
        "label": expectation["label"],
        "minecraft": sorted({lane["minecraft"] for lane in lanes}, key=_version_key),
        "loaders": loaders,
        "loader_names": [_loader_name(invocation, loader) for loader in loaders],
        "frame_count": len(manifest["frames"]),
        "lane_count": len(lanes),
        "scenarios": [scenario["id"] for scenario in expectation["scenarios"]],
        "contract_sha256": manifest["contract_sha256"],
        "contract_url": f"https://github.com/{repository}/blob/{subject['commit']}/{expectation['contract_path']}",
        "matrix_sha256": manifest["matrix_sha256"],
        "subject": dict(subject),
        "coverage_sha": manifest["provenance"]["coverage_sha"],
        "short_sha": grammar.short_sha(subject["commit"]),
        "handoff": {"run_id": handoff["run_id"], "run_url": grammar.run_url(repository, handoff["run_id"]),
                    "created_at": handoff["created_at"]},
        "tested": {"run_id": tested["run_id"], "run_url": grammar.run_url(repository, tested["run_id"]),
                   "created_at": tested["created_at"], "commit": tested["commit"]},
        "scope": manifest["scope"]["kind"],
        "reuse": manifest["provenance"]["reuse"],
    }


def _gallery_evidence(invocation: Invocation, bundle: Mapping[str, Any], rank: int,
                      images: dict[str, bytes]) -> tuple[list[tuple[Any, dict[str, Any]]], list[tuple[Any, dict[str, Any]]],
                                                        list[tuple[Any, dict[str, Any]]]]:
    """``(lanes, frames, comparisons)`` of one bundle, each paired with its sort key; the published
    derivatives are added to ``images`` (``e2e/images/<key>/<sha>.webp``)."""

    repository = invocation.repository
    labels = invocation.config.labels
    manifest, selection = bundle["manifest"], bundle["selection"]
    key = manifest["key"]
    handoff = manifest["provenance"]["handoff"]
    tested_run = selection["source"]["tested_run"]
    epochs: dict[str, str] = {}
    for frame in manifest["frames"]:
        if "epoch" in frame:
            epochs.setdefault(frame["lane_id"], frame["epoch"])
    lanes = []
    for lane in manifest["lanes"]:
        record = {"lane_id": lane["lane_id"], "key": key, "artifact_node": lane["artifact_node"],
                  "minecraft": lane["minecraft"], "loader": lane["loader"],
                  "loader_name": _loader_name(invocation, lane["loader"]), "scenario": lane["scenario"],
                  "roles": list(lane["roles"]), "status": lane["status"], "jars": dict(lane["jars"])}
        if "elapsed_s" in lane:
            record["elapsed_s"] = lane["elapsed_s"]
        if lane["lane_id"] in epochs:
            record["epoch"] = epochs[lane["lane_id"]]
        lanes.append(((rank, lane["lane_id"]), record))
    frames = []
    for position, frame in enumerate(manifest["frames"]):
        derivative, source = frame["derivative"], frame["source"]
        relative = f"images/{key}/{derivative['sha256']}.webp"
        data = _read_image(bundle["root"], derivative)
        if images.setdefault(f"e2e/{relative}", data) != data:
            raise _fail(f"two derivatives claim {relative}", reason="image")
        tested = frame.get("tested", tested_run)
        loader_name = _loader_name(invocation, frame["loader"])
        record = {
            **{field: frame[field] for field in ("frame_id", "capture_id", "capture_order", "title", "expectation",
                                                 "runtime_evidence", "review_tier", "lane_id", "artifact_node",
                                                 "minecraft", "loader", "scenario", "role", "step")},
            "key": key,
            "loader_name": loader_name,
            "image": relative,
            "width": derivative["width"],
            "height": derivative["height"],
            "alt": _alt(frame, loader_name, labels["roles"].get(frame["role"], frame["role"])),
            "source": {"width": source["width"], "height": source["height"], "file_sha256": source["sha256"],
                       "pixel": source["pixel"]},
            "published": {"file_sha256": derivative["sha256"], "format": "webp", "pixel": derivative["pixel"]},
            "provenance": {"handoff_run_url": grammar.run_url(repository, handoff["run_id"]),
                           "handoff_commit": handoff["commit"],
                           "tested_run_url": grammar.run_url(repository, tested["run_id"]),
                           "tested_commit": tested["commit"], "tested_created_at": tested["created_at"],
                           "coverage_sha": manifest["provenance"]["coverage_sha"]},
        }
        if "epoch" in frame:
            record["epoch"] = frame["epoch"]
        order = (rank, _version_key(frame["minecraft"]), loader_name, frame["capture_order"], position)
        frames.append((order, record))
    comparisons = [((rank, item["comparison_id"]),
                    {"comparison_id": item["comparison_id"], "key": key, "lane_id": item["lane_id"],
                     "role": item["role"], "first_frame_id": item["first_frame_id"],
                     "second_frame_id": item["second_frame_id"], "source": item["source"],
                     "published": item["derivative"]}) for item in manifest["comparisons"]]
    return lanes, frames, comparisons


def _gallery_family(invocation: Invocation, family: Mapping[str, Any], legs: list[Mapping[str, Any]],
                    rank: Mapping[str, int], images: dict[str, bytes]) -> dict[str, Any]:
    """One family view: its per-key releases, lanes and not-applicable entries (images copied to
    ``e2e/families/<family>/images/<sha>.webp``)."""

    repository = invocation.repository
    family_id = family["id"]
    prefix = f"families/{family_id}/images/"
    releases, lanes, not_applicable = [], [], []
    for leg in legs:
        if leg["status"] != "available":
            if leg["status"] not in ("superseded", "unavailable") or leg["projection"] is not None:
                raise _fail(f"the {family_id} leg of {leg['key']} has an invalid render status")
            releases.append({"key": leg["key"], "available": False, "status": leg["status"]})
            continue
        projection = leg["projection"]
        if projection is None or leg["root"] is None:
            raise _fail(f"the available {family_id} leg of {leg['key']} has no projection")
        releases.append({"key": leg["key"], "available": True, "status": "available",
                         "coverage_sha": projection["coverage_sha"], "contracts": dict(projection["contracts"]),
                         "links": [{"label": link["label"], "run_url": grammar.run_url(repository, link["run_id"])}
                                   for link in projection["provenance"]["links"]],
                         "image_policy": dict(projection["image_policy"])})
        for lane in projection["lanes"]:
            pairs = []
            for pair in lane["pairs"]:
                sides = {}
                for side in ("reference", "candidate"):
                    image = pair[side]["image"]
                    data = _read_image(leg["root"], image)
                    published = f"{prefix}{image['sha256']}.webp"
                    if images.setdefault(f"e2e/{published}", data) != data:
                        raise _fail(f"two family images claim {published}", reason="image")
                    sides[side] = {"image": {**image, "path": published}, "source": pair[side]["source"]}
                pairs.append({**pair, **sides})
            record = {**lane, "pairs": pairs, "key": leg["key"]}
            order = (rank[leg["key"]], _loader_name(invocation, lane["loader"]), lane["variant"]["name"],
                     lane["lane_id"])
            lanes.append((order, record))
        for entry in projection["not_applicable"]:
            order = (rank[leg["key"]], _loader_name(invocation, entry["loader"]), entry["variant_name"],
                     entry["artifact_node"])
            not_applicable.append((order, {**entry, "key": leg["key"]}))
    available = any(release["available"] for release in releases)
    if available:
        status = "available"
    elif releases and all(release["status"] == "superseded" for release in releases):
        status = "superseded"
    else:
        status = "unavailable"
    return {"family": family_id, "title": family["title"], "description": family["description"],
            "available": available, "status": status, "releases": releases,
            "lanes": [record for _, record in sorted(lanes, key=lambda item: item[0])],
            "not_applicable": [record for _, record in sorted(not_applicable, key=lambda item: item[0])]}


def _documents(invocation: Invocation, bundles: list[dict[str, Any]],
               families: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    """``(site-data, gallery-data, images)`` of the authenticated inputs, both documents validated."""

    config = invocation.config
    repository_url = f"https://github.com/{invocation.repository}"
    rank = {bundle["manifest"]["key"]: index for index, bundle in enumerate(bundles)}
    if len(rank) != len(bundles):
        raise _fail("two bundles claim one key")
    images: dict[str, bytes] = {}
    releases = [_gallery_release(invocation, bundle) for bundle in bundles]
    lanes: list[tuple[Any, dict[str, Any]]] = []
    frames: list[tuple[Any, dict[str, Any]]] = []
    comparisons: list[tuple[Any, dict[str, Any]]] = []
    for bundle in bundles:
        bundle_lanes, bundle_frames, bundle_comparisons = _gallery_evidence(
            invocation, bundle, rank[bundle["manifest"]["key"]], images)
        lanes += bundle_lanes
        frames += bundle_frames
        comparisons += bundle_comparisons
    family_views = []
    for family in config.families:
        legs = [leg for leg in families if leg["family"] == family["id"]]
        if {leg["key"] for leg in legs} - set(rank):
            raise _fail(f"family {family['id']} names a key outside the ordinary bundles")
        family_views.append(_gallery_family(invocation, family, sorted(legs, key=lambda leg: rank[leg["key"]]),
                                            rank, images))
    kit = invocation.kit
    gallery = {
        "kind": "mod-base.gallery",
        "schema_version": 1,
        "project": {"name": config.project["name"], "repository_url": repository_url,
                    "actions_url": _actions_url(invocation)},
        "labels": {name: (dict(value) if isinstance(value, dict) else value) for name, value in config.labels.items()},
        "copy": {"gallery_lead": config.copy["gallery_lead"], "methodology": list(config.copy["methodology"]),
                 "family_notes": dict(config.copy["family_notes"])},
        "releases": releases,
        "lanes": [record for _, record in sorted(lanes, key=lambda item: item[0])],
        "frames": [record for _, record in sorted(frames, key=lambda item: item[0])],
        "comparisons": [record for _, record in sorted(comparisons, key=lambda item: item[0])],
        "families": family_views,
        "build": {"implementation_sha": invocation.implementation_sha, "kit_sha": kit["sha"],
                  "kit_version": kit["version"], "pixel_metrics_version": PIXEL_METRICS_VERSION},
    }
    documents.validate_gallery(gallery)
    site = {
        "kind": "mod-base.site",
        "schema_version": 1,
        "project": _project(invocation),
        "gallery_url": "e2e/",
        "releases": [{field: release[field] for field in ("key", "label", "minecraft", "loaders", "loader_names",
                                                          "frame_count", "lane_count", "short_sha")}
                     | {"subject_commit": release["subject"]["commit"], "tested_run_url": release["tested"]["run_url"]}
                     for release in releases],
        "families": [{"family": view["family"], "title": view["title"], "lane_count": len(view["lanes"]),
                      "available": view["available"]} for view in family_views],
        "copy": {"principles": list(config.copy["principles"]), "evidence_lead": config.copy["evidence_lead"]},
        "generated": {"implementation_sha": invocation.implementation_sha, "kit_sha": kit["sha"],
                      "kit_version": kit["version"],
                      "pages_run_url": grammar.run_url(invocation.repository,
                                                       _decimal(invocation, "GITHUB_RUN_ID", lim.MAX_RUN_ID))},
    }
    documents.validate_site(site)
    return site, gallery, images


def _pages(invocation: Invocation, project: Mapping[str, Any], sources: Mapping[str, bytes]) -> dict[str, bytes]:
    """The two templated pages of the allowlist."""

    config = invocation.config
    links = project["links"]
    primary = links[0] if links else {"url": project["repository_url"], "title": "GitHub"}
    values = {
        "name": project["name"], "tagline": project["tagline"], "eyebrow": project["eyebrow"],
        "description": project["description"], "license": project["license_label"],
        "repository_url": project["repository_url"], "issues_url": project["issues_url"],
        "actions_url": _actions_url(invocation),
        "primary_link_url": primary["url"], "primary_link_title": primary["title"],
        "theme_color": templating.theme_color(config.theme),
        "color_scheme": templating.color_scheme(config.theme),
    }
    conditions = {"icon": project["icon"] is not None, "primary_link": bool(links), "families": bool(config.families)}
    landing = (f"{project['name']} downloads, source and verified packaged-Minecraft visual evidence." if links
               else f"{project['name']} source and verified packaged-Minecraft visual evidence.")
    descriptions = {"index.html": landing,
                    "e2e/index.html": f"Versioned packaged-Minecraft E2E screenshots for {project['name']}."}
    pages = {}
    for relative in TEMPLATES:
        try:
            template = sources[relative].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _fail(f"kit site/{relative} is not UTF-8", reason="template") from exc
        rendered = templating.render(template, {**values, "meta_description": descriptions[relative]}, conditions)
        pages[relative] = rendered.encode("utf-8")
    return pages


def _build_record(invocation: Invocation, written: Mapping[str, bytes]) -> bytes:
    run_id = _decimal(invocation, "GITHUB_RUN_ID", lim.MAX_RUN_ID)
    record = {
        "kind": "mod-base.build",
        "schema_version": 1,
        "repository": invocation.repository,
        "implementation": {"sha": invocation.implementation_sha, "run_id": run_id,
                           "run_attempt": _decimal(invocation, "GITHUB_RUN_ATTEMPT", lim.MAX_RUN_ATTEMPT),
                           "run_url": grammar.run_url(invocation.repository, run_id),
                           "workflow_ref": invocation.environ.get("GITHUB_WORKFLOW_REF")},
        "kit": invocation.kit,
        "pixel_metrics_version": PIXEL_METRICS_VERSION,
        "site_inventory_sha256": documents.inventory_sha256(_inventory(written)),
    }
    documents.validate_build(record)
    return canonical_json(record)


def render_site(invocation: Invocation, *, kit_root: Path, bundles: list[dict[str, Any]],
                families: list[dict[str, Any]], stage_fd: int) -> dict[str, bytes]:
    """Pure rendering of already authenticated inputs into the stage; returns the written
    ``{relative path: bytes}`` map that :func:`mod_base.io.seal.seal_output` verifies.

    ``bundles`` lists the validated compact bundles in display order, each ``{"root": Path,
    "manifest", "expectation", "selection"}`` (extra keys are ignored); ``families`` lists one
    entry per family leg ``{"family", "key", "status", "root": Path | None, "projection": dict |
    None}``, available exactly when ``status == "available"``."""

    written: dict[str, bytes] = {}

    def put(relative: str, data: bytes) -> None:
        if relative in written:
            raise _fail(f"the site writes {relative} twice")
        write_new(stage_fd, relative, data)
        written[relative] = data

    sources = _copy_static(kit_root)
    icon = invocation.config.project["icon"]
    planned = _planned_images(bundles, families)
    generated = (THEME_CSS, NOJEKYLL, SITE_DATA, GALLERY_DATA, BUILD_RECORD) + ((ICON,) if icon is not None else ())
    if (len(planned) + len(sources) + len(generated) > lim.MAX_SITE_FILES
            or sum(planned.values()) + sum(len(data) for data in sources.values()) > lim.MAX_SITE_BYTES):
        raise _fail(f"the site would exceed {lim.MAX_SITE_FILES} files or {lim.MAX_SITE_BYTES} bytes",
                    reason="site-bounds")
    project = _project(invocation)
    for relative, data in _pages(invocation, project, sources).items():
        put(relative, data)
    for relative in SITE_ALLOWLIST:
        if relative not in TEMPLATES:
            put(relative, sources[relative])
    theme = templating.theme_css(invocation.config.theme)
    if icon is not None and icon["rendering"] == "pixelated":
        theme += ".brand-icon{image-rendering:pixelated}\n"
    put(THEME_CSS, theme.encode("utf-8"))
    if icon is not None:
        put(ICON, _icon_png(read_child_file(invocation.repo_root, icon["path"], max_bytes=lim.MAX_ICON_BYTES)))
    put(NOJEKYLL, b"")
    site, gallery, images = _documents(invocation, bundles, families)
    for relative in sorted(images):
        put(relative, images[relative])
    for relative, document, maximum in ((SITE_DATA, site, lim.MAX_SITE_DATA_BYTES),
                                        (GALLERY_DATA, gallery, lim.MAX_GALLERY_DATA_BYTES)):
        data = canonical_json(document)
        if len(data) > maximum:
            raise _fail(f"{relative} exceeds {maximum} bytes")
        put(relative, data)
    put(BUILD_RECORD, _build_record(invocation, written))
    return written


def _planned_images(bundles: list[dict[str, Any]], families: list[dict[str, Any]]) -> dict[str, int]:
    """Published path -> declared size of every derivative and family image, from the validated
    manifests and projections before any image is read (one entry per content-addressed path)."""

    planned: dict[str, int] = {}

    def plan(path: str, size: int) -> None:
        if planned.setdefault(path, size) != size:
            raise _fail(f"two images claim {path}", reason="image")

    for bundle in bundles:
        manifest = bundle["manifest"]
        for frame in manifest["frames"]:
            derivative = frame["derivative"]
            plan(f"e2e/images/{manifest['key']}/{derivative['sha256']}.webp", derivative["size"])
    for leg in families:
        if leg["status"] != "available" or leg["projection"] is None:
            continue
        for lane in leg["projection"]["lanes"]:
            for pair in lane["pairs"]:
                for side in ("reference", "candidate"):
                    image = pair[side]["image"]
                    plan(f"e2e/families/{leg['family']}/images/{image['sha256']}.webp", image["size"])
    return planned


def _copy_static(kit_root: Path) -> dict[str, bytes]:
    """The allowlisted kit ``site/`` files (BP ``_copy_static``): the exact list, never a directory
    walk, each read without following a symlink anywhere below the kit root."""

    return {relative: read_child_file(kit_root, f"site/{relative}", max_bytes=MAX_SITE_SOURCE_BYTES)
            for relative in SITE_ALLOWLIST}
