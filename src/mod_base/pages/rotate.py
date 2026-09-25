"""Exact-ID rotation of superseded evidence after an authenticated successful Pages run (MB7).

Quick Skin ``rotate_artifacts`` engine (retirement families, ``DeletionBudget``, deferral,
delete delay) + Block Pops ``_RotationReads`` (pinned reads, per-delete re-observation). Rotation
reads config **data** only: no adapter hook runs and no repository fact is checked.

``R`` is the owner run (``--owner-run-id``/``--owner-sha``): a completed successful
``.github/workflows/pages.yml`` run of this repository on the default branch (which must be
``config.canonical_branch``) at ``owner_sha``, never the rotation run itself. ``T_R`` is its
``created_at``.

**Pinned reads.** Every run and artifact the plan uses is read through :class:`_Reads`: each object
is observed once and every later observation of it, from any listing or single read, must be
identical (BP "rotation policy input changed"). A pinned replacement must never disappear; a
candidate that vanished concurrently is simply not deleted.

**Replacements first.** R's single ``mb-promotion`` is downloaded by id and validated (canonical
bytes, as ``build`` wrote it and ``refresh`` requires; a final ``documents.validate_promotion``
bound to R: implementation branch/SHA/run, attempt at most R's, kit SHA equal to R's
``referenced_workflows``). Then every promoted key's ``mb-cache--<key>--
<coverage>`` and every available family's ``mb-family-cache--<family>--<key>--<coverage>``, each
owned by R exactly once, is downloaded by id and validated structurally: strict documents, the
exact file inventory, the manifest hash the promotion recorded and the selection R wrote (compact),
or the envelope's repository, family and key and its native inventory (family). This is R's own
evidence, so it is checked against R's promotion and R's embedded expectation, never against the
live config's image policy, extension list or family list (the rotation run reads the config at a
possibly newer head). A family cache holds its producer's envelope verbatim (``refresh`` rolls the
collected ``source/`` forward), so the envelope's ``coverage_sha`` is the producer's commit: equal to
the promoted coverage, or an ancestor of it for a **carried** leg, whose ancestry (SPEC §4.4 R5) R's
``build`` proved against inert Git objects rotation does not have; the cache's name and owner, not
the envelope, bind it to the promoted coverage.

R's inventory must be exactly that generation (QS "cache inventory disagrees", BP one fan-in
bundle, one ``github-pages``): every unexpired artifact of R carries R's head, its caches are
exactly the promoted replacements, each ``mb-collected*`` is the collected artifact the promotion
recorded (id and digest; a retry may find some already retired) and it holds at most one
``github-pages``. An owner, promotion, key replacement or inventory that cannot be authenticated
stops the whole rotation before a single deletion, so a predecessor is never retired without a
verified replacement. A **family leg** whose replacement cannot be verified rejects only that leg:
its predecessors, its collected artifact, ``github-pages`` and the ``mb-promotion`` (which yield to
every replacement, so a retry can re-plan) are retained and reported, and every other scope
proceeds.

**Discovery by exact names.** GitHub filters artifacts only by exact name, and a repository-wide
listing grows with every unrelated artifact (Quick Skin holds tens of thousands), so rotation never
lists the repository. The *history* of a subject branch is the newest successful source-workflow
runs on it (GitHub lists at most 1,000 filtered runs, read in pages of 100) created before R.
Rotation names what it may retire:

* R's own artifacts (``list_for_run(R)``);
* the previous generation's caches: the Pages runs owning ``mb-cache--<probe key>--<commit>``, the
  probe key being the first promoted key of a subject branch, for each commit of that branch's
  *window* (R's coverage, then the older heads of its history; at most ``limits.GENERATION_PROBES``
  commits), stopping at the first older commit published by a superseding Pages run; every cache
  and family cache those owners hold is a candidate;
* **leftovers** of earlier generations: the Pages runs owning the *last* promoted key's
  ``mb-cache--<key>--<commit>`` or ``mb-family-cache--<family>--<key>--<commit>`` (its verified
  legs) at any commit of the same window, besides the previous generation's owners; every cache and
  family cache they still hold is a candidate. Read only when the executor reaches the leftover
  phase with budget left (see below), at most one read per name and commit. A budget spent in any
  earlier rotation stops in the order below, so what it leaves of a generation is always a suffix
  of that order: the last key's cache and family caches are the last to go, and one of them is
  present while anything of that generation is (a retry of the same owner, whose probe-key
  predecessor is already retired, finds its previous generation this way too);
* handoffs ``mb-handoff--<key>--a1`` plus the attempt of the handoff R consumed; family handoffs
  alike (attempt 1 plus the attempt of the family handoff R consumed);
* anchors ``mb-anchor--<key>--<head>--<run>--a<attempt>`` named from the ``limits.ANCHOR_PROBES``
  newest anchor-eligible runs of the history created at or before the grace boundary
  ``now - anchor.successor_grace_days``: an artifact is created after its run, so a newer run cannot
  own an anchor whose successor grace has passed.

Anything rotation cannot name is left to its own retention, in particular:

* generations whose coverage is outside the window (a newly added key that sorts first leaves the
  probe nothing to find: the previous generation is then retired as a leftover);
* leftovers of a generation that holds none of the last key's names any more: its promotion lacked
  that key or leg (a newly added key sorting last), an earlier rotation retired those names but not
  the rest (its owner promoted only some family legs), or they remain from a rejected or stopped
  group rather than from the budget;
* anchors of runs further behind the boundary than ``limits.ANCHOR_PROBES`` eligible runs (each
  rotation moves the boundary forward only by the time since the previous one) or uploaded by an
  attempt other than the run's latest; handoffs of attempts other than 1 and the consumed one's.

**Retirement rules** (SPEC §5.5; "superseded" = owned by a completed successful ``pages.yml`` run of
the default branch created before ``T_R``; every candidate except R's transients was created before
``T_R``, is unexpired and belongs to a key or family leg R promoted):

* ``mb-cache--k--*`` / ``mb-family-cache--f--k--*``: superseded and not R's own (a previous or an
  earlier generation alike);
* ``mb-handoff--k--*``: the handoff R consumed (``bundles[k].selected_artifact_id``, identical to
  the selection R authenticated), or one whose producer run is older than the selected artifact's
  run with the same producer branch; ``mb-family-handoff--f--k--*`` alike, per family, bounded by
  the selected family artifact as read with its replacement, before any deletion (a carried leg
  selected a family cache, whose owner is the bound; when it was already gone, the envelope's
  producer run is the conservative bound);
* ``mb-anchor--k--*``: older than ``B``, the newest authenticated anchor of ``k`` created at or
  before the grace boundary (and before ``T_R``). Every such anchor's first successor is at or
  before ``B``, so ``now - first_successor.created_at >= anchor.successor_grace_days``; ``B`` and
  every newer anchor (among them the newest one) are kept;
* R's ``mb-collected*``, ``mb-promotion`` (last) and ``github-pages``: always;
* ``mb-baseline--*`` and every non-``mb-`` name: never.

A run of this repository that owns a kit name it should not (another workflow, event, branch or
head) rejects its whole retirement family, which is then retained and reported (QS). A fork's run
(a fork pull request can upload any name, even from a branch named like the default branch) and an
authentic owner that did not succeed, is still running or is not older than R only make their
artifact a non-candidate.

**Execution, longest-lived first.** :class:`DeletionBudget` bounds the deletion *attempts* of the
whole invocation (``limits.DELETION_BUDGET``, 64), so the order of the retirement groups decides
what a spent budget leaves to retention. Groups run in phases of decreasing retention (SPEC §3.0):

1. the 90-day kinds of the previous generation, one promoted key at a time: its superseded caches,
   then each of its family legs' superseded family caches (family order), then its anchors;
2. the leftovers of earlier generations (90-day kinds), in the same key and family order: the key's
   caches, then its legs' family caches;
3. every family leg's family handoffs (``families[].retention_days``, at most 7 days);
4. every key's 1-day handoffs;
5. R's 1-day transients, the promotion last.

Deciding: a Quick Skin generation (17 keys, one family) supersedes about 35 long-lived artifacts
(17 caches, 17 family caches of up to ``handoff_max_bytes`` each, and its anchor) besides its
handoffs, family handoffs and R's 36 transients, about 105 in all. SPEC §5.5's original budget of 32
could not retire even the long-lived ones, so about three superseded 90-day artifacts accumulated
per publication; the budget is therefore 64 (an MB0 amendment, ``docs/INTERNAL-API.md``). Retiring
one key scope after another (caches, handoffs, anchors) would spend it before the last family legs,
so the phases go longest-lived first: a whole generation's long-lived artifacts always fit, the
rest of the budget drains the leftovers an earlier rotation deferred, and what a spent budget still
defers is short-lived (it expires within a day, or a week for a family handoff). Deferred
long-lived artifacts stay discoverable as leftovers: the next rotation, or a retry of this owner,
retires them. The previous generation's artifacts come before the leftovers because they live
longest. A group reached with an empty budget is deferred without being planned (so the
leftover discovery reads nothing); a group's candidates are cut to the remaining budget and the
rest deferred. A budget spent before the promotion's attempt therefore retains it, so a rotation
retried for the same owner re-plans and continues where this one stopped.

Before its group's first deletion every authorizing owner run is read again and must be unchanged.
Before each deletion R and the replacements that authorize it are read again and must be unchanged
(its scope's replacement; for R's ``github-pages`` and ``mb-promotion`` every replacement, since the
promotion is what lets a retry re-plan) and, after the budget is charged, the candidate itself is
re-observed by id; it must still equal its pinned metadata, and the final guard re-proves the rules
above. A concurrent 404 counts as an attempt but not as a deletion. A rejected group (its plan
failed, or an owner run changed before its first deletion) is retained and reported while the
scope's other groups proceed; a failed deletion check stops that key, leg or R's transients, with
the deletions it already made (:class:`RotationDeferred`), in every later phase, while the other
scopes continue. ``dry_run`` plans and re-observes exactly the same way but never sends a
``DELETE`` (and never sleeps).
"""

from __future__ import annotations

import math
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mod_base.errors import MbError, Unavailable, single_line
from mod_base.family.envelope import ENVELOPE_NAME
from mod_base.github import artifacts, contents, runs
from mod_base.github.api import ApiNotFound, GitHubApi
from mod_base.github.artifacts import Artifact
from mod_base.io import tree
from mod_base.io.bounded_zip import LIMITS_BY_KIND, ExtractionLimits
from mod_base.model import documents, grammar, limits
from mod_base.model.canonical import canonical_json, sha256_hex, strict_loads
from mod_base.pages.build import PROMOTION_FILE
from mod_base.runtime import Invocation
from mod_base.workflow import PAGES_EVENTS, PAGES_WORKFLOW_PATH

OWNER = "MB7"

#: Scope label of R's own transient artifacts (never a key: it contains a ``/``).
PAGES_RUN_SCOPE = "owner-run/transients"

#: Kinds a rotation may ever delete; ``baseline`` keeps its independent 90-day expiry.
_RETIRABLE_KINDS = frozenset({"handoff", "anchor", "family-handoff", "cache", "family-cache", "collected",
                              "collected-family", "promotion", "pages"})
#: R's own kinds, retired after its caches; the promotion goes last so a retry can re-plan.
_TRANSIENT_ORDER = {"collected": 0, "collected-family": 1, "pages": 2, "promotion": 3}
#: Run fields a pinned observation must keep (BP ``_RotationReads.run_fields``).
_RUN_FIELDS = ("id", "run_attempt", "workflow_id", "path", "head_branch", "head_sha", "event", "display_title",
               "created_at", "status", "conclusion")


def _fail(message: str, reason: str = "rotation") -> MbError:
    return MbError(message, reason=reason)


def _drift(message: str) -> MbError:
    return MbError(message, reason="rotation-drift")


class RotationDeferred(MbError):
    """Some candidates were retained after completing these exact deletions (budget spent)."""

    default_reason = "rotation-deferred"

    def __init__(self, message: str, deleted_artifact_ids: list[int]) -> None:
        super().__init__(message)
        self.deleted_artifact_ids = list(deleted_artifact_ids)


@dataclass
class DeletionBudget:
    """Bounds authenticated deletion attempts across one rotation invocation."""

    remaining: int = limits.DELETION_BUDGET
    last_deferred_count: int = 0

    def __post_init__(self) -> None:
        for value in (self.remaining, self.last_deferred_count):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise _fail("deletion budget counters must be non-negative integers", "usage")

    def begin_scope(self) -> None:
        """Start counting the candidates one retirement scope leaves to retention."""

        self.last_deferred_count = 0

    def select(self, artifacts: list[object]) -> list[object]:
        """The first candidates the remaining budget can attempt; the rest count as deferred."""

        selected = list(artifacts[: self.remaining])
        self.last_deferred_count += len(artifacts) - len(selected)
        return selected

    def consume(self) -> None:
        """Charge one deletion attempt (even one that later finds a 404 or fails)."""

        if self.remaining <= 0:
            raise _fail("the global deletion budget is exhausted before this retirement", "rotation-budget")
        self.remaining -= 1


# -- pinned reads -----------------------------------------------------------------------------------


def _run_projection(run: Mapping[str, Any]) -> dict[str, Any]:
    projection = {name: run.get(name) for name in _RUN_FIELDS}
    repository = run.get("head_repository")
    projection["head_repository"] = repository.get("full_name") if isinstance(repository, Mapping) else None
    return projection


class _Reads:
    """Pin policy reads (BP ``_RotationReads``): the first observation of a run or artifact is the
    one the plan uses; any later observation of the same id must be identical."""

    def __init__(self, api: GitHubApi) -> None:
        self.api = api
        self._runs: dict[int, dict[str, Any]] = {}
        self._projections: dict[int, dict[str, Any]] = {}
        self._artifacts: dict[int, Artifact] = {}
        self._by_id: dict[int, Artifact | None] = {}
        self._named: dict[str, list[Artifact]] = {}
        self._for_run: dict[int, list[Artifact]] = {}
        self._histories: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def _pin_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        _, run_id, _ = runs.run_order(run)
        projection = _run_projection(run)
        if self._projections.setdefault(run_id, projection) != projection:
            raise _drift(f"workflow run {run_id} changed while rotation was planning")
        return self._runs.setdefault(run_id, dict(run))

    def _pin_artifacts(self, observed: Iterable[Artifact]) -> list[Artifact]:
        pinned = []
        for artifact in observed:
            if self._artifacts.setdefault(artifact.id, artifact) != artifact:
                raise _drift(f"artifact {artifact.id} metadata changed while rotation was planning")
            pinned.append(artifact)
        return pinned

    def run(self, run_id: int) -> dict[str, Any]:
        if run_id not in self._runs:
            self._pin_run(runs.get_run(self.api, run_id))
        return self._runs[run_id]

    def artifact(self, artifact_id: int) -> Artifact | None:
        """The artifact by id, ``None`` when GitHub no longer has it."""

        if artifact_id not in self._by_id:
            try:
                found = self._pin_artifacts([artifacts.get_artifact(self.api, artifact_id)])[0]
            except ApiNotFound:
                found = None
            self._by_id[artifact_id] = found
        return self._by_id[artifact_id]

    def named(self, name: str) -> list[Artifact]:
        if name not in self._named:
            self._named[name] = self._pin_artifacts(artifacts.list_named(self.api, name))
        return self._named[name]

    def for_run(self, run_id: int) -> list[Artifact]:
        if run_id not in self._for_run:
            self._for_run[run_id] = self._pin_artifacts(artifacts.list_for_run(self.api, run_id))
        return self._for_run[run_id]

    def history(self, workflow_path: str, branch: str) -> list[dict[str, Any]]:
        """The newest successful runs of ``workflow_path`` on ``branch``, newest first (GitHub
        lists at most 1,000 filtered runs: ``runs.workflow_runs``' default bound)."""

        key = (workflow_path, branch)
        if key not in self._histories:
            rows = runs.workflow_runs(self.api, workflow_path, branch=branch, status="success")
            self._histories[key] = [self._pin_run(row) for row in rows]
        return self._histories[key]

    def observe_run(self, run_id: int) -> None:
        """Read a pinned run again; it must be unchanged."""

        if _run_projection(runs.get_run(self.api, run_id)) != self._projections[run_id]:
            raise _drift(f"workflow run {run_id} changed before its retirement")

    def observe_artifact(self, artifact_id: int) -> Artifact | None:
        """Read a pinned artifact again by id: ``None`` when it is gone, otherwise it must be
        exactly its pinned metadata."""

        try:
            current = artifacts.get_artifact(self.api, artifact_id)
        except ApiNotFound:
            return None
        if current != self._artifacts[artifact_id]:
            raise _drift(f"artifact {artifact_id} changed before its retirement")
        return current


# -- plan -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Keep:
    """A replacement R published: the cache every predecessor in its scope yields to."""

    scope: str
    key: str
    family: str | None
    artifact: Artifact
    coverage_sha: str
    selected_id: int
    subject_branch: str
    #: Ordinary keys: the compact ``source_artifact`` R consumed, its authenticated creation time,
    #: the handoff producer's branch and whether the expectation declares an anchor.
    selected: Mapping[str, Any] = field(default_factory=dict)
    selected_created_at: datetime | None = None
    producer_branch: str = ""
    anchored: bool = False
    #: Family legs: the envelope's producer run, the "older than" bound when the selected family
    #: artifact itself is gone.
    producer_run_id: int = 0


@dataclass(frozen=True)
class _Candidate:
    artifact: Artifact
    owners: tuple[int, ...] = ()
    #: The replacements re-observed before this candidate's deletion.
    keeps: tuple[Artifact, ...] = ()
    transient: bool = False


@dataclass(frozen=True)
class _Group:
    label: str
    candidates: tuple[_Candidate, ...] = ()
    rejection: str | None = None


@dataclass(frozen=True)
class _Step:
    """One retirement group of one summary scope, planned only when the executor reaches it."""

    section: str
    label: str
    group: str
    plan: Callable[[], list[_Candidate]] | None = None
    #: A group rejected before planning (an unverified family replacement), reported at no cost.
    rejection: str | None = None


@dataclass
class _Outcome:
    """What the groups of one summary scope did."""

    deleted: list[int] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    budget_deferred: int = 0
    unplanned: bool = False
    stopped: bool = False

    def reason(self) -> str | None:
        parts = list(self.reasons)
        if self.budget_deferred:
            parts.append(f"the global deletion budget left {self.budget_deferred} artifact(s) to retention")
        if self.unplanned:
            parts.append("the global deletion budget is exhausted; retention will retire the rest")
        return single_line("; ".join(parts), limit=1000) if parts else None


def _leg_label(family: str, key: str) -> str:
    return f"{family}--{key}"


def _oldest_first(pool: Iterable[Artifact]) -> list[Artifact]:
    unique = {artifact.id: artifact for artifact in pool}
    return sorted(unique.values(), key=lambda artifact: artifact.order)


def _read_json(root: Path, relative: str, max_bytes: int) -> tuple[Any, bytes]:
    data = tree.read_child_file(root, relative, max_bytes=max_bytes)
    return strict_loads(data, label=relative, max_bytes=max_bytes), data


def _inventory(root: Path, exclude: str, extraction: ExtractionLimits) -> list[dict[str, Any]]:
    return tree.file_records(root, exclude={exclude}, max_files=extraction.max_entries,
                             max_total_bytes=extraction.max_total_bytes, max_file_bytes=extraction.max_entry_bytes)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _fail(message, "rotation-replacement")


class _Rotation:
    """One rotation invocation: authenticate R, load its replacements, then plan and retire group
    by group, longest-lived first (see the module docstring)."""

    def __init__(self, invocation: Invocation, api: GitHubApi, *, owner_run_id: int, owner_sha: str,
                 delete_delay_seconds: float, dry_run: bool, now: datetime, sleep: Callable[[float], None],
                 workdir: Path) -> None:
        self.config = invocation.config
        self.api = api
        self.repository = api.repository
        self.canonical = self.config.canonical_branch
        self.owner_run_id = owner_run_id
        self.owner_sha = owner_sha
        self.delay = delete_delay_seconds
        self.dry_run = dry_run
        self.now = now
        self.sleep = sleep
        self.workdir = workdir
        self.reads = _Reads(api)
        self.budget = DeletionBudget()
        self.protected: set[int] = set()
        self.planned: list[int] = []
        source = self.config.source
        self.source_workflow: str = source["workflow"]
        self.canonical_events = frozenset(source["events"]["canonical"])
        self.source_events = self.canonical_events | frozenset(source["events"]["other"])
        self._generation_owner_ids: set[int] | None = None
        self._generations: dict[str, list[Artifact]] | None = None
        self._leftovers: dict[str, list[Artifact]] | None = None
        self._histories: dict[str, list[dict[str, Any]]] = {}

    # -- owner and replacements ---------------------------------------------------------------------

    def authenticate_owner(self) -> None:
        if contents.default_branch(self.api) != self.canonical:
            raise _fail("config.canonical_branch is not the repository's default branch")
        owner = self.reads.run(self.owner_run_id)
        try:
            runs.validate_run(owner, repository=self.repository, workflow_path=PAGES_WORKFLOW_PATH,
                              events=PAGES_EVENTS, head_branch=self.canonical, head_sha=self.owner_sha,
                              require_success=True)
        except MbError as exc:
            raise _fail(f"the owner is not a completed successful default-branch Pages run: {exc}") from exc
        self.owner_created, _, self.owner_attempt = runs.run_order(owner)
        workflow_id = owner.get("workflow_id")
        if isinstance(workflow_id, bool) or not isinstance(workflow_id, int) or workflow_id <= 0:
            raise _fail("the owner run has no workflow id")
        self.workflow_id = workflow_id
        self.kit_sha = runs.referenced_kit_sha(owner)
        self.owner_artifacts = [artifact for artifact in self.reads.for_run(self.owner_run_id) if not artifact.expired]
        _require(all(artifact.head_sha == self.owner_sha and artifact.head_branch == self.canonical
                     for artifact in self.owner_artifacts), "an artifact of the owner run names another head")

    def _owned_once(self, name: str) -> Artifact:
        matches = [artifact for artifact in self.owner_artifacts if artifact.name == name]
        _require(len(matches) == 1, f"the owner run must hold exactly one unexpired {name}, found {len(matches)}")
        return matches[0]

    def _download(self, artifact: Artifact, kind: str) -> tuple[Path, list[str]]:
        output = self.workdir / str(artifact.id)
        paths = artifacts.download(self.api, artifact_id=artifact.id, name=artifact.name, digest=artifact.digest,
                                   size=artifact.size, run_id=artifact.run_id, output=output,
                                   extraction=LIMITS_BY_KIND[kind])
        return output, paths

    def load_promotion(self) -> None:
        matches = [artifact for artifact in self.owner_artifacts if artifact.name == grammar.PROMOTION_NAME]
        if not matches:
            raise Unavailable("the owner run holds no unexpired mb-promotion: its generation was already "
                              "rotated or has expired")
        _require(len(matches) == 1, "the owner run holds several mb-promotion artifacts")
        promotion_artifact = matches[0]
        root, paths = self._download(promotion_artifact, "promotion")
        _require(paths == [PROMOTION_FILE], f"mb-promotion must hold exactly {PROMOTION_FILE}")
        promotion, promotion_bytes = _read_json(root, PROMOTION_FILE, limits.MAX_PROMOTION_BYTES)
        _require(canonical_json(promotion) == promotion_bytes, f"{PROMOTION_FILE} is not canonical JSON")
        documents.validate_promotion(promotion)
        implementation = promotion["implementation"]
        _require(promotion["repository"] == self.repository and implementation["branch"] == self.canonical
                 and implementation["sha"] == self.owner_sha and implementation["run_id"] == self.owner_run_id
                 and implementation["run_attempt"] <= self.owner_attempt,
                 "mb-promotion does not describe the owner run")
        _require(promotion["kit"]["sha"] == self.kit_sha, "mb-promotion was not written by the owner's kit")
        self.promotion = promotion

    def load_replacements(self) -> None:
        """Verify every replacement R promoted. A key's failure stops the rotation; a family leg's
        only rejects that leg (``rejected_legs``), whose promoted cache stays protected."""

        self.keeps = [self._load_keep(bundle) for bundle in sorted(self.promotion["bundles"],
                                                                   key=lambda bundle: bundle["key"])]
        self.family_keeps: list[_Keep] = []
        self.rejected_legs: dict[tuple[str, str], str] = {}
        unverified: set[int] = set()
        for entry in sorted(self.promotion["families"], key=lambda entry: (entry["family"], entry["key"])):
            if not entry["available"]:
                continue
            try:
                keep = self._load_family_keep(entry)
                # Pinned before any deletion: the family handoffs are planned after the 90-day phase,
                # which may retire the family cache a carried leg selected (its owner is the bound).
                self.reads.artifact(keep.selected_id)
                self.family_keeps.append(keep)
            except MbError as exc:
                self.rejected_legs[(entry["family"], entry["key"])] = single_line(exc, limit=400)
                name = grammar.family_cache_name(entry["family"], entry["key"], entry["coverage_sha"])
                unverified.update(artifact.id for artifact in self.owner_artifacts if artifact.name == name)
        self.keeps_by_key = {keep.key: keep for keep in self.keeps}
        self.family_keeps_by_scope = {(keep.family, keep.key): keep for keep in self.family_keeps}
        self.protected.update(keep.artifact.id for keep in (*self.keeps, *self.family_keeps))
        self.protected.update(unverified)
        self._check_owner_inventory(unverified)

    def _check_owner_inventory(self, unverified: set[int]) -> None:
        """R's unexpired kit artifacts are exactly its promoted generation (module docstring);
        ``unverified`` are R's caches under the promoted name of a rejected family leg."""

        replacements = {keep.artifact.id for keep in (*self.keeps, *self.family_keeps)} | unverified
        collected: dict[tuple[str | None, str], tuple[int, str]] = {
            (None, bundle["key"]): (bundle["collected_artifact_id"], bundle["collected_digest"])
            for bundle in self.promotion["bundles"]}
        collected.update({(entry["family"], entry["key"]): (entry["collected_artifact_id"], entry["collected_digest"])
                          for entry in self.promotion["families"] if entry["available"]})
        pages = 0
        for artifact in self.owner_artifacts:
            parsed = grammar.parse_artifact_name(artifact.name)
            if parsed is None:
                continue
            if parsed.kind in {"cache", "family-cache"}:
                _require(artifact.id in replacements, f"the owner run holds {artifact.name}, which it did not promote")
            elif parsed.kind in {"collected", "collected-family"}:
                _require(collected.get((parsed.family, str(parsed.key))) == (artifact.id, artifact.digest),
                         f"the owner run's {artifact.name} is not the collected artifact its promotion recorded")
            elif parsed.kind == "pages":
                pages += 1
        _require(pages <= 1, "the owner run holds several github-pages artifacts")

    def _load_keep(self, bundle: Mapping[str, Any]) -> _Keep:
        key, coverage = bundle["key"], bundle["coverage_sha"]
        keep = self._owned_once(grammar.cache_name(key, coverage))
        root, _ = self._download(keep, "cache")
        manifest, manifest_bytes = _read_json(root, "manifest.json", limits.MAX_MANIFEST_BYTES)
        _require(sha256_hex(manifest_bytes) == bundle["manifest_sha256"],
                 f"{keep.name} is not the compact bundle the promotion recorded")
        expectation, expectation_bytes = _read_json(root, "expectation.json", limits.MAX_EXPECTATION_BYTES)
        selection, selection_bytes = _read_json(root, "selection.json", limits.MAX_SELECTION_BYTES)
        documents.validate_expectation(expectation)
        documents.validate_compact(manifest, expectation=expectation)
        documents.validate_selection(selection)
        documents.check_compact_selection(manifest, selection)
        _require(manifest["expectation"]["sha256"] == sha256_hex(expectation_bytes)
                 and manifest["selection"]["sha256"] == sha256_hex(selection_bytes)
                 and _inventory(root, "manifest.json", LIMITS_BY_KIND["cache"]) == manifest["files"],
                 f"{keep.name} does not hold exactly its recorded files")
        implementation = selection["implementation"]
        _require(manifest["repository"] == self.repository and manifest["key"] == key
                 and manifest["subject"]["commit"] == coverage and manifest["provenance"]["coverage_sha"] == coverage
                 and manifest["kit"]["sha"] == self.kit_sha
                 and manifest["source_artifact"]["id"] == bundle["selected_artifact_id"]
                 and implementation["run_id"] == self.owner_run_id and implementation["sha"] == self.owner_sha
                 and implementation["branch"] == self.canonical and implementation["run_attempt"] <= self.owner_attempt,
                 f"{keep.name} was not collected by the owner run for {key}")
        return _Keep(scope=key, key=key, family=None, artifact=keep, coverage_sha=coverage,
                     selected_id=bundle["selected_artifact_id"], subject_branch=manifest["subject"]["branch"],
                     selected=manifest["source_artifact"],
                     selected_created_at=grammar.parse_timestamp(selection["selected_artifact"]["created_at"]),
                     producer_branch=manifest["provenance"]["handoff"]["branch"],
                     anchored=expectation["anchor"] is not None)

    def _load_family_keep(self, entry: Mapping[str, Any]) -> _Keep:
        """The leg's family cache, verified structurally. Its envelope may cover an ancestor of the
        promoted coverage (a carried leg, module docstring): R's build proved R5 and refresh named
        the cache by the promoted coverage, so only repository, family and key must match."""

        family_id, key, coverage = entry["family"], entry["key"], entry["coverage_sha"]
        keep = self._owned_once(grammar.family_cache_name(family_id, key, coverage))
        root, _ = self._download(keep, "family-cache")
        envelope, _ = _read_json(root, ENVELOPE_NAME, limits.MAX_ENVELOPE_BYTES)
        documents.validate_family_envelope(envelope)
        _require(_inventory(root, ENVELOPE_NAME, LIMITS_BY_KIND["family-cache"]) == envelope["files"],
                 f"{keep.name} does not hold exactly its enveloped files")
        _require(envelope["repository"] == self.repository and envelope["family"] == family_id
                 and envelope["key"] == key,
                 f"{keep.name} does not envelope {family_id} evidence for {key} of this repository")
        return _Keep(scope=_leg_label(family_id, key), key=key, family=family_id, artifact=keep, coverage_sha=coverage,
                     selected_id=entry["selected_artifact_id"], subject_branch=envelope["subject"]["branch"],
                     producer_run_id=envelope["producer"]["run_id"])

    # -- owners -------------------------------------------------------------------------------------

    def _superseding(self, owner: Mapping[str, Any]) -> bool:
        created, _, _ = runs.run_order(owner)
        return (owner.get("status") == "completed" and owner.get("conclusion") == "success"
                and created < self.owner_created)

    def _own_run(self, run_id: int) -> dict[str, Any] | None:
        """The pinned run ``run_id`` when it ran from this repository; ``None`` for a fork's run (a
        fork pull request can upload any artifact name, even from a branch named like ours)."""

        owner = self.reads.run(run_id)
        repository = owner.get("head_repository")
        return owner if isinstance(repository, Mapping) and repository.get("full_name") == self.repository else None

    def _owner(self, artifact: Artifact, *, workflow_path: str, events: Iterable[str], head_branch: str,
               workflow_id: int | None = None, attempt: int | None = None) -> dict[str, Any] | None:
        """The owner of ``artifact`` when it is an authentic run of ``workflow_path`` at the
        artifact's head that superseded (completed, successful, created before R). ``None`` for a
        fork's run or an authentic run that did not supersede; a run of this repository that is
        not such an owner (another workflow, event, branch or head) raises, rejecting the family."""

        owner = self._own_run(artifact.run_id)
        if owner is None:
            return None
        runs.validate_run(owner, repository=self.repository, workflow_path=workflow_path, events=frozenset(events),
                          head_branch=head_branch, head_sha=artifact.head_sha, workflow_id=workflow_id,
                          require_success=False)
        if attempt is not None and attempt > runs.run_order(owner)[2]:
            raise _fail(f"artifact {artifact.id} names an attempt its run never had")
        return owner if self._superseding(owner) else None

    def _older(self, artifact: Artifact) -> bool:
        return (not artifact.expired and artifact.run_id != self.owner_run_id
                and artifact.order[0] < self.owner_created)

    # -- discovery ----------------------------------------------------------------------------------

    def _source_history(self, branch: str) -> list[dict[str, Any]]:
        """Successful source runs of ``branch`` created before R, newest first. A row that fails
        provenance or names no 40-hex head is skipped: it can only hide a candidate."""

        if branch not in self._histories:
            history = []
            for row in self.reads.history(self.source_workflow, branch):
                try:
                    runs.validate_run(row, repository=self.repository, workflow_path=self.source_workflow,
                                      events=self.source_events, head_branch=branch, require_success=True)
                except MbError:
                    continue
                if grammar.is_match(grammar.SHA1, row.get("head_sha")) and runs.run_order(row)[0] < self.owner_created:
                    history.append(row)
            self._histories[branch] = history
        return self._histories[branch]

    def _window(self, branch: str, coverage: str) -> list[str]:
        """``coverage`` and the older heads of ``branch``'s history, newest first: the at most
        ``limits.GENERATION_PROBES`` commits whose cache names discovery reads."""

        commits = [coverage, *(row["head_sha"] for row in self._source_history(branch))]
        return list(dict.fromkeys(commits))[:limits.GENERATION_PROBES]

    def _cache_owners(self, name: str) -> set[int]:
        """Runs of this repository (never a fork's) owning an unexpired ``name`` of the default
        branch created before R."""

        return {artifact.run_id for artifact in self.reads.named(name)
                if self._older(artifact) and artifact.head_branch == self.canonical
                and self._own_run(artifact.run_id) is not None}

    def _generation_owners(self) -> set[int]:
        """The Pages runs that published R's coverage before R and the newest older published head
        (probing the first promoted key of each subject branch)."""

        if self._generation_owner_ids is None:
            owners: set[int] = set()
            probes: dict[str, _Keep] = {}
            for keep in self.keeps:
                probes.setdefault(keep.subject_branch, keep)
            for branch, probe in probes.items():
                for position, commit in enumerate(self._window(branch, probe.coverage_sha)):
                    found = self._cache_owners(grammar.cache_name(probe.key, commit))
                    owners |= found
                    if position and any(self._superseding_pages_run(run_id) for run_id in found):
                        break
            self._generation_owner_ids = owners
        return self._generation_owner_ids

    def _leftover_owners(self) -> set[int]:
        """Earlier Pages runs, beyond :meth:`_generation_owners`, that still hold caches of
        promoted keys (module docstring): the owners of the last promoted key's cache and family
        caches at every commit of its subject branch's window."""

        sentinels: dict[str, _Keep] = {}
        for keep in self.keeps:
            sentinels[keep.subject_branch] = keep
        owners: set[int] = set()
        for branch, sentinel in sentinels.items():
            families = [str(leg.family) for leg in self.family_keeps if leg.key == sentinel.key]
            for commit in self._window(branch, sentinel.coverage_sha):
                owners |= self._cache_owners(grammar.cache_name(sentinel.key, commit))
                for family in families:
                    owners |= self._cache_owners(grammar.family_cache_name(family, sentinel.key, commit))
        return owners - self._generation_owners()

    def _pools(self, owners: Iterable[int]) -> dict[str, list[Artifact]]:
        """The caches and family caches of promoted keys and verified legs ``owners`` hold, by
        summary scope."""

        pools: dict[str, list[Artifact]] = {}
        for run_id in sorted(owners):
            for artifact in self.reads.for_run(run_id):
                parsed = grammar.parse_artifact_name(artifact.name)
                if parsed is None:
                    continue
                if parsed.kind == "cache" and parsed.key in self.keeps_by_key:
                    pools.setdefault(parsed.key, []).append(artifact)
                elif parsed.kind == "family-cache" and (parsed.family, parsed.key) in self.family_keeps_by_scope:
                    pools.setdefault(_leg_label(str(parsed.family), str(parsed.key)), []).append(artifact)
        return pools

    def _caches(self, *, leftover: bool) -> dict[str, list[Artifact]]:
        """The previous generation's pools, or (``leftover``) the earlier generations' ones; each
        discovered when its first group is planned, so a spent budget reads nothing more."""

        if leftover:
            if self._leftovers is None:
                self._leftovers = self._pools(self._leftover_owners())
            return self._leftovers
        if self._generations is None:
            self._generations = self._pools(self._generation_owners())
        return self._generations

    def _superseding_pages_run(self, run_id: int) -> bool:
        owner = self.reads.run(run_id)
        try:
            runs.validate_run(owner, repository=self.repository, workflow_path=PAGES_WORKFLOW_PATH,
                              events=PAGES_EVENTS, head_branch=self.canonical, workflow_id=self.workflow_id,
                              require_success=True)
        except MbError:
            return False
        return self._superseding(owner)

    # -- retirement families --------------------------------------------------------------------------

    def _group(self, label: str, plan: Callable[[], list[_Candidate]]) -> _Group:
        try:
            return _Group(label, tuple(plan()))
        except MbError as exc:
            return _Group(label, rejection=single_line(exc, limit=400))

    def _cache_candidates(self, keep: _Keep, *, leftover: bool = False) -> list[_Candidate]:
        candidates = []
        for artifact in _oldest_first(self._caches(leftover=leftover).get(keep.scope, [])):
            if artifact.id == keep.artifact.id or not self._older(artifact) or artifact.head_branch != self.canonical:
                continue
            if self._owner(artifact, workflow_path=PAGES_WORKFLOW_PATH, events=PAGES_EVENTS, head_branch=self.canonical,
                           workflow_id=self.workflow_id) is not None:
                candidates.append(_Candidate(artifact, owners=(artifact.run_id,), keeps=(keep.artifact,)))
        return candidates

    def _handoff_candidates(self, keep: _Keep) -> list[_Candidate]:
        selected = keep.selected
        consumed_kind = selected["kind"] == "handoff"
        reference = runs.run_order(self.reads.run(selected["run_id"]))[:2]
        attempts = sorted({1, selected["run_attempt"]} if consumed_kind else {1})
        pool = [artifact for attempt in attempts
                for artifact in self.reads.named(grammar.handoff_name(keep.key, attempt))]
        candidates = []
        for artifact in _oldest_first(pool):
            if not self._older(artifact):
                continue
            consumed = consumed_kind and artifact.id == selected["id"]
            if consumed:
                if ((artifact.name, artifact.digest, artifact.size, artifact.run_id, artifact.order[0])
                        != (selected["name"], selected["digest"], selected["size"], selected["run_id"],
                            keep.selected_created_at)):
                    raise _fail(f"handoff {artifact.id} differs from the selection the owner authenticated")
            elif artifact.head_branch != keep.producer_branch:
                continue
            parsed = grammar.require_artifact_name(artifact.name, "handoff")
            owner = self._owner(artifact, workflow_path=self.source_workflow, events=self.source_events,
                                head_branch=artifact.head_branch, attempt=parsed.attempt)
            if owner is not None and (consumed or runs.run_order(owner)[:2] < reference):
                candidates.append(_Candidate(artifact, owners=(artifact.run_id,), keeps=(keep.artifact,)))
        return candidates

    def _anchor_candidates(self, keep: _Keep) -> list[_Candidate]:
        """Authenticated anchors older than ``B``, the newest one created at or before the grace
        boundary (module docstring); ``B`` is protected."""

        if not keep.anchored:
            return []
        branch = keep.subject_branch
        template = self.config.source["display_title"]
        boundary = self.now - timedelta(days=self.config.anchor["successor_grace_days"])
        anchors: list[_Candidate] = []
        probes = 0
        for row in self._source_history(branch):
            if probes == limits.ANCHOR_PROBES:
                break
            if runs.run_order(row)[0] > boundary:
                continue  # its anchor is newer than the boundary: never B, never older than B
            title = None if template is None else template.format(subject_commit=row["head_sha"])
            try:
                runs.validate_run(row, repository=self.repository, workflow_path=self.source_workflow,
                                  events=self.canonical_events, head_branch=branch, require_success=True,
                                  display_title=title)
            except MbError:
                continue  # never an authenticated anchor owner: neither a candidate nor a successor
            probes += 1
            name = grammar.anchor_name(keep.key, row["head_sha"], row["id"], row["run_attempt"])
            for artifact in self.reads.named(name):
                if (self._older(artifact) and artifact.run_id == row["id"] and artifact.head_sha == row["head_sha"]
                        and artifact.head_branch == branch):
                    anchors.append(_Candidate(artifact, owners=(row["id"],), keeps=(keep.artifact,)))
        settled = [candidate.artifact for candidate in anchors if candidate.artifact.order[0] <= boundary]
        if not settled:
            return []
        bound = max(settled, key=lambda artifact: artifact.order)
        self.protected.add(bound.id)
        return sorted((candidate for candidate in anchors if candidate.artifact.order < bound.order),
                      key=lambda candidate: candidate.artifact.order)

    def _family_handoff_candidates(self, keep: _Keep) -> list[_Candidate]:
        family = str(keep.family)
        producer = self.config.family(family)["producer"]
        selected = self.reads.artifact(keep.selected_id)
        attempts = {1}
        bound_run_id = keep.producer_run_id
        if selected is not None:
            parsed = grammar.parse_artifact_name(selected.name)
            if (parsed is None or parsed.kind not in {"family-handoff", "family-cache"}
                    or (parsed.family, parsed.key) != (family, keep.key)):
                raise _fail(f"the selected family artifact {selected.id} is not {keep.scope} evidence")
            bound_run_id = selected.run_id
            if parsed.kind == "family-handoff" and parsed.attempt is not None:
                attempts.add(parsed.attempt)
        reference = runs.run_order(self.reads.run(bound_run_id))[:2]
        pool = [artifact for attempt in sorted(attempts)
                for artifact in self.reads.named(grammar.family_handoff_name(family, keep.key, attempt))]
        candidates = []
        for artifact in _oldest_first(pool):
            if not self._older(artifact) or artifact.head_branch != self.canonical:
                continue
            parsed = grammar.require_artifact_name(artifact.name, "family-handoff")
            owner = self._owner(artifact, workflow_path=producer["workflow"], events=producer["events"],
                                head_branch=self.canonical, attempt=parsed.attempt)
            if owner is not None and (artifact.id == keep.selected_id or runs.run_order(owner)[:2] < reference):
                candidates.append(_Candidate(artifact, owners=(artifact.run_id,), keeps=(keep.artifact,)))
        return candidates

    def _transient_candidates(self) -> list[_Candidate]:
        """R's transients (``_check_owner_inventory`` already bound each one to the promotion): a
        collected artifact yields to its own scope's replacement; ``github-pages`` and the promotion
        (QS: every keep before each transient) to every replacement. A rejected leg's collected
        artifact, and with it ``github-pages`` and the promotion, are retained (see :meth:`steps`)."""

        every_keep = tuple(keep.artifact for keep in (*self.keeps, *self.family_keeps))
        chosen = []
        for artifact in self.owner_artifacts:
            parsed = grammar.parse_artifact_name(artifact.name)
            if parsed is None or parsed.kind not in _TRANSIENT_ORDER:
                continue
            if parsed.kind == "collected":
                keeps = (self.keeps_by_key[str(parsed.key)].artifact,)
            elif parsed.kind == "collected-family":
                leg = self.family_keeps_by_scope.get((str(parsed.family), str(parsed.key)))
                if leg is None:
                    continue
                keeps = (leg.artifact,)
            elif self.rejected_legs:
                continue
            else:
                keeps = every_keep
            chosen.append((_TRANSIENT_ORDER[parsed.kind], artifact.name, artifact.id,
                           _Candidate(artifact, keeps=keeps, transient=True)))
        return [candidate for *_, candidate in sorted(chosen, key=lambda item: item[:3])]

    def labels(self) -> dict[str, list[str]]:
        """Every summary label per section, in summary order: each promoted key, each available
        family leg (verified or rejected) and R's transients."""

        legs = sorted({*self.family_keeps_by_scope, *self.rejected_legs})
        return {"keys": [keep.scope for keep in self.keeps], "families": [_leg_label(*leg) for leg in legs],
                "pages-run": [PAGES_RUN_SCOPE]}

    def steps(self) -> list[_Step]:
        """Every retirement group in execution order, longest-lived first (module docstring)."""

        legs = sorted({*self.family_keeps_by_scope, *self.rejected_legs})
        planned: list[_Step] = []
        for keep in self.keeps:
            planned.append(_Step("keys", keep.scope, "cache", lambda keep=keep: self._cache_candidates(keep)))
            for family, key in legs:
                if key != keep.key:
                    continue
                leg = self.family_keeps_by_scope.get((family, key))
                if leg is None:
                    planned.append(_Step("families", _leg_label(family, key), "family", rejection=(
                        f"its replacement cannot be verified: {self.rejected_legs[(family, key)]}")))
                else:
                    planned.append(_Step("families", leg.scope, "family cache",
                                         lambda leg=leg: self._cache_candidates(leg)))
            planned.append(_Step("keys", keep.scope, "anchor", lambda keep=keep: self._anchor_candidates(keep)))
        for keep in self.keeps:
            planned.append(_Step("keys", keep.scope, "leftover cache",
                                 lambda keep=keep: self._cache_candidates(keep, leftover=True)))
            planned += [_Step("families", leg.scope, "leftover family cache",
                              lambda leg=leg: self._cache_candidates(leg, leftover=True))
                        for leg in self.family_keeps if leg.key == keep.key]
        planned += [_Step("families", keep.scope, "family handoff",
                          lambda keep=keep: self._family_handoff_candidates(keep)) for keep in self.family_keeps]
        planned += [_Step("keys", keep.scope, "handoff", lambda keep=keep: self._handoff_candidates(keep))
                    for keep in self.keeps]
        if self.rejected_legs:
            unverified = ", ".join(_leg_label(*leg) for leg in sorted(self.rejected_legs))
            planned.append(_Step("pages-run", PAGES_RUN_SCOPE, "pages-run transient", rejection=(
                f"the collected artifact of {unverified}, github-pages and {grammar.PROMOTION_NAME} yield to a "
                "replacement that cannot be verified")))
        planned.append(_Step("pages-run", PAGES_RUN_SCOPE, "pages-run transient", self._transient_candidates))
        return planned

    # -- execution ----------------------------------------------------------------------------------

    def retire_group(self, group: _Group) -> list[int]:
        """QS ``_rotate_candidate_groups`` for one group: a rejected group, or one whose owner run
        changed, raises :class:`MbError` before any deletion (the scope's other groups proceed); a
        failed deletion check raises :class:`RotationDeferred` with the deletions already made (the
        scope stops)."""

        if group.rejection is not None:
            raise _fail(f"{group.label} retirement: {group.rejection}")
        selected = self.budget.select(list(group.candidates))
        try:
            for run_id in dict.fromkeys(owner for candidate in selected for owner in candidate.owners):
                self.reads.observe_run(run_id)
        except MbError as exc:
            raise _fail(f"{group.label} retirement: {single_line(exc, limit=400)}") from exc
        deleted: list[int] = []
        try:
            for candidate in selected:
                if self._retire(candidate):
                    deleted.append(candidate.artifact.id)
        except MbError as exc:
            raise RotationDeferred(f"{group.label} retirement: {single_line(exc, limit=400)}", deleted) from exc
        return deleted

    def _retire(self, candidate: _Candidate) -> bool:
        """Re-observe R and the authorizing replacements (an observation equal to the pinned,
        unexpired metadata, or a drift error), charge the budget, re-observe the candidate, guard
        it and delete it; ``True`` only for an actual deletion."""

        self.reads.observe_run(self.owner_run_id)
        for keep in candidate.keeps:
            if self.reads.observe_artifact(keep.id) is None:
                raise _drift(f"replacement {keep.name} disappeared before a retirement")
        self.budget.consume()
        current = self.reads.observe_artifact(candidate.artifact.id)
        if current is None:
            return False
        self._guard(current, candidate)
        self.planned.append(current.id)
        if self.dry_run:
            return False
        try:
            artifacts.delete(self.api, current.id)
            deleted = True
        except ApiNotFound:
            deleted = False
        if self.delay:
            self.sleep(self.delay)
        return deleted

    def _guard(self, artifact: Artifact, candidate: _Candidate) -> None:
        """The invariants no plan may break: only retirable ``mb-`` kinds, never a replacement or
        the newest anchor, and nothing newer than R except R's own transients."""

        parsed = grammar.parse_artifact_name(artifact.name)
        if parsed is None or parsed.kind not in _RETIRABLE_KINDS or artifact.id in self.protected:
            raise _fail(f"refusing to retire protected or foreign artifact {artifact.id}", "rotation-guard")
        if candidate.transient:
            safe = parsed.kind in _TRANSIENT_ORDER and artifact.run_id == self.owner_run_id
        else:
            safe = artifact.run_id != self.owner_run_id and artifact.order[0] < self.owner_created
        if not safe:
            raise _fail(f"refusing to retire artifact {artifact.id} outside the owner's generation", "rotation-guard")


def rotate_generation(invocation: Invocation, *, api: GitHubApi, owner_run_id: int, owner_sha: str,
                      delete_delay_seconds: float = 1.0, dry_run: bool = False, now: float | None = None,
                      sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
    """Retire everything R superseded and return the JSON summary (QS keys plus
    ``remaining_rotation_deletions``). ``dry_run`` plans and re-observes without deleting.

    Summary: ``deleted_artifact_ids`` (key -> ids) and ``deferred_branches`` (keys) for the promoted
    keys; ``deleted_compatibility_artifact_ids`` and ``compatibility_deferred_branches`` for the
    family legs, labelled ``<family>--<key>``; ``deleted_pages_run_artifact_ids`` and
    ``pages_run_deferred`` (with ``pages_run_deferral_reason``, one bounded line or null) for R's
    transients; ``deferral_reasons`` (key or family label -> one bounded line);
    ``planned_artifact_ids`` (every id that passed its re-observation, in order: deleted, found
    gone at ``DELETE``, or, in a dry run, left in place); ``remaining_rotation_deletions``,
    ``rotation_deletion_limit``, ``dry_run`` and ``owner_run_id``. Deferral is a normal outcome,
    and so is a family leg whose replacement cannot be verified (only that leg, its collected
    artifact, ``github-pages`` and the promotion are retained); an owner, promotion, key replacement
    or owner inventory that cannot be authenticated raises before any deletion (``Unavailable`` when
    R holds no promotion any more)."""

    grammar.require_positive_int(owner_run_id, "owner run id")
    grammar.require_sha1(owner_sha, "owner SHA")
    if (isinstance(delete_delay_seconds, bool) or not isinstance(delete_delay_seconds, (int, float))
            or not math.isfinite(delete_delay_seconds) or delete_delay_seconds < 0):
        raise _fail("the delete delay must be a finite non-negative number of seconds", "usage")
    if not isinstance(dry_run, bool):
        raise _fail("dry_run must be a boolean", "usage")
    if now is not None and (isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now)):
        raise _fail("now must be a finite POSIX timestamp", "usage")
    if invocation.environ.get("GITHUB_REPOSITORY", api.repository) != api.repository:
        raise _fail("the GitHub client is bound to another repository than this invocation")
    if invocation.environ.get("GITHUB_RUN_ID") == str(owner_run_id):
        raise _fail("a rotation run cannot rotate its own generation")
    if not dry_run and not api.writable:
        raise _fail("a rotation that deletes needs a writable GitHub client", "usage")
    moment = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    with tempfile.TemporaryDirectory(prefix="mod-base-rotate-") as scratch:
        rotation = _Rotation(invocation, api, owner_run_id=owner_run_id, owner_sha=owner_sha,
                             delete_delay_seconds=float(delete_delay_seconds), dry_run=dry_run, now=moment,
                             sleep=sleep, workdir=Path(scratch))
        rotation.authenticate_owner()
        rotation.load_promotion()
        rotation.load_replacements()
        return _execute(rotation)


def _execute(rotation: _Rotation) -> dict[str, object]:
    labels = rotation.labels()
    outcomes = {(section, label): _Outcome() for section, names in labels.items() for label in names}
    budget = rotation.budget
    for step in rotation.steps():
        outcome = outcomes[(step.section, step.label)]
        if outcome.stopped:
            continue
        if step.rejection is not None:
            outcome.reasons.append(f"{step.group} retirement: {step.rejection}")
            continue
        if budget.remaining == 0:
            outcome.unplanned = True
            continue
        assert step.plan is not None
        budget.begin_scope()
        try:
            outcome.deleted += rotation.retire_group(rotation._group(step.group, step.plan))
        except RotationDeferred as exc:
            outcome.deleted += exc.deleted_artifact_ids
            outcome.reasons.append(single_line(exc, limit=1000))
            outcome.stopped = True
        except MbError as exc:
            outcome.reasons.append(single_line(exc, limit=1000))
        else:
            # Only a completed group reports the tail its budget cut; a rejected or stopped group's
            # reason already covers every candidate it retained.
            outcome.budget_deferred += budget.last_deferred_count

    def section(name: str) -> dict[str, _Outcome]:
        return {label: outcomes[(name, label)] for label in labels[name]}

    keys, families = section("keys"), section("families")
    transients = outcomes[("pages-run", PAGES_RUN_SCOPE)]
    reasons = {label: outcome.reason() for label, outcome in {**keys, **families}.items()}
    return {
        "compatibility_deferred_branches": [label for label in families if reasons[label] is not None],
        "deferral_reasons": {label: reason for label, reason in reasons.items() if reason is not None},
        "deferred_branches": [label for label in keys if reasons[label] is not None],
        "deleted_artifact_ids": {label: outcome.deleted for label, outcome in keys.items()},
        "deleted_compatibility_artifact_ids": {label: outcome.deleted for label, outcome in families.items()},
        "deleted_pages_run_artifact_ids": transients.deleted,
        "dry_run": rotation.dry_run,
        "owner_run_id": rotation.owner_run_id,
        "pages_run_deferral_reason": transients.reason(),
        "pages_run_deferred": transients.reason() is not None,
        "planned_artifact_ids": list(rotation.planned),
        "remaining_rotation_deletions": budget.remaining,
        "rotation_deletion_limit": limits.DELETION_BUDGET,
    }
