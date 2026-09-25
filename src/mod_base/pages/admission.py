"""Publication admission (MB5): QS ``publication_progress.decide`` + wake/recovery/family admission.

``decide`` is QS's pure policy with identical constants and prefix-aware exact job names
(:mod:`mod_base.workflow`); producer upload windows come from ``config.source.handoff_job/
handoff_step`` and ``families[].producer.job/step``. ``admit`` adds: live default head ==
``GITHUB_SHA`` (else ``stale-implementation``, exit 0), canonical branch == API default branch,
deploy/family wake authentication (polling the source run up to 30 x 2 s), ``stale-wake``,
``current`` (every key cached at its subject with no newer handoff, and every family current),
``deferred-active-source``, and the v1 cutover reason ``awaiting-complete-v1-evidence``.

The order of ``admit`` (SPEC §5.3.1), each step ending the admission when it decides:

1. the API default branch is ``config.canonical_branch`` and its live head is ``GITHUB_SHA``
   (``stale-implementation`` otherwise); the adapter's targets are discovered;
2. a ``deploy`` wake names a ``source.workflow`` run (polled to completion; path, workflow id,
   this repository) that succeeded on the canonical branch at ``sha`` with an ``events.canonical``
   event; ``sha`` no longer the protected head is ``stale-wake``; the handoffs of the run's latest
   attempt name only target keys and become the nominations, except those whose key's current
   subject the run no longer names (a wake left with none is ``stale-wake``). A ``family`` wake
   names by id an ``mb-family-handoff`` of a configured family and a target key, with its digest,
   owned by that latest producer attempt on the default branch; unless ``sha == coverage_sha`` is
   the live head (and the key's subject) the wake is ``stale-wake``;
3. ``recovery``/``manual``: ``current`` when every key has an ``mb-cache--<key>--<subject>`` owned by
   a successful Pages run at the protected head (owners memoized, every artifact still checked
   against its own head) and no successful source run for that subject that settled after the
   owner started handed the key off (a cache is not a tombstone), and every family leg is current:
   it has no family handoff at the head, or a family cache there that supersedes the newest one
   (:func:`mod_base.pages.select.supersedes`: its Pages owner was created after that upload, so a
   publication would select the cache, never the handoff);
4. except for ``manual``, ``deferred-active-source`` while a ``source.workflow`` run that could
   produce admissible evidence for a subject is requested, queued, pending, waiting or in progress;
5. ``awaiting-complete-v1-evidence`` while some key (other than a nominated one) has neither a v1
   cache nor a v1 handoff for its subject (the cutover keeps the previous site until every key can
   be collected);
6. ``manual`` is admitted (``manual``); ``admission.mode == "always"`` admits (``always``,
   ``family-wake``); ``"progress"`` runs the QS publication-progress snapshot below and
   :func:`decide`; the live head is rechecked last.

The progress snapshot is QS ``publication_progress.plan`` over v1 artifacts: the newest atomic
Pages owner at the head whose exact attempt succeeded in every ``Publish``/``Deploy``/``Finalize``
job and uploaded every ordinary cache inside its refresh step (published family legs keyed by
their collector's selection boundary); a single bounded producer query that reopens a complete
publication after a lost same-head family wake (always for a family wake); ready family handoffs
inside their producer upload step (re-read by id); the newest complete ordinary attempt per subject
settled after the published selection boundary; and, only when the policy would admit, the failed
Pages publications since the newest readiness. Family legs are ``<family>/<key>``. A mod without
families decides over one synthetic ordinary leg, so the same policy yields ``initial-ordinary``,
``ordinary-replacement`` or ``complete``. Nominations of a deploy wake stand unless the snapshot
names a newer complete ordinary attempt, whose window-checked handoffs replace them (QS parity).
"""

from __future__ import annotations

import functools
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mod_base.errors import MbError
from mod_base.github import artifacts, contents, jobs, runs
from mod_base.github.api import GitHubApi, InconsistentListing
from mod_base.io.bounded_zip import artifact_limit
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.pages.select import (
    SourceRuns,
    display_title,
    family_archive_limit,
    handoff_job_name,
    is_success,
    names_commit,
    pages_owner_valid,
    producer_run_valid,
    source_run_valid,
    subject_events,
    successful_job,
    supersedes,
    upload_in_window,
)
from mod_base.pages.targets import discover_targets
from mod_base.runtime import Invocation
from mod_base.workflow import (
    PAGES_EVENTS,
    PAGES_WORKFLOW_PATH,
    PUBLISH_OPERATIONS,
    api_job_name,
    caller_job_name,
    step_name,
)

OWNER = "MB5"

COALESCE_SECONDS = 10 * 60
PARTIAL_DEADLINE_SECONDS = 45 * 60
RECOVERY_INTERVAL_SECONDS = 60 * 60
#: QS ``publication_progress.MAX_REQUESTS``: the read budget of one Pages job's client.
MAX_REQUESTS = lim.MAX_PAGES_API_READS
#: Owner runs authenticated for one exact-name or run inventory (``select``'s bound).
MAX_CANDIDATES = lim.MAX_CANDIDATES
MAX_FAILED_PUBLICATIONS = 3
ACTIVE_RUN_STATUSES = frozenset({"requested", "queued", "pending", "waiting", "in_progress"})
REASONS = frozenset({
    "stale-implementation", "stale-wake", "current", "deferred-active-source", "awaiting-complete-v1-evidence",
    "ordinary-handoffs-pending", "complete", "unchanged", "publisher-active",
    "publication-recovery-budget-exhausted", "initial-ordinary", "ordinary-replacement", "final-complete",
    "partial-deadline", "half-coverage", "coalescing", "family-wake", "always", "manual",
})

#: QS's listing order of the active statuses (the frozenset above has no stable iteration order).
_ACTIVE_STATUS_ORDER = ("requested", "queued", "pending", "waiting", "in_progress")
#: The synthetic leg of a mod without families (never a ``<family>/<key>`` id).
_ORDINARY_LEG = "ordinary"


@dataclass(frozen=True)
class ProgressPolicy:
    coalesce_seconds: int = COALESCE_SECONDS
    partial_deadline_seconds: int = PARTIAL_DEADLINE_SECONDS
    recovery_interval_seconds: int = RECOVERY_INTERVAL_SECONDS
    max_failed_publications: int = MAX_FAILED_PUBLICATIONS

    @classmethod
    def from_config(cls, admission: Mapping[str, Any]) -> "ProgressPolicy":
        """The policy of a ``progress``-mode ``config.admission``."""

        if not isinstance(admission, Mapping) or admission.get("mode") != "progress":
            raise MbError("a progress policy needs admission.mode 'progress'", reason="config")
        values = {}
        for name in ("coalesce_seconds", "partial_deadline_seconds", "recovery_interval_seconds",
                     "max_failed_publications"):
            value = admission.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MbError(f"admission.{name} must be a non-negative integer", reason="config")
            values[name] = value
        if values["coalesce_seconds"] > values["partial_deadline_seconds"] or values["max_failed_publications"] < 1:
            raise MbError("admission timing is inconsistent", reason="config")
        return cls(**values)


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    ready: int
    published: int
    next_check_at: float | None = None


def _timestamp_valid(value: Any, now: float) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            and 0 <= value <= now)


def decide(*, expected: set[str], published: dict[str, float], ready: dict[str, float], ordinary_ready: bool,
           published_at: float | None, now: float, publisher_available: bool = True, failed_publications: int = 0,
           ordinary_changed: bool = False, policy: ProgressPolicy = ProgressPolicy()) -> Decision:
    """QS ``publication_progress.decide`` (pure, deterministic; same reasons and ordering)."""

    if (not isinstance(policy, ProgressPolicy) or isinstance(now, bool) or not isinstance(now, (int, float))
            or not math.isfinite(now) or now < 0 or not expected or len(expected) > lim.MAX_FAMILY_LEGS
            or not set(published) <= expected or not set(ready) <= expected or not set(published) <= set(ready)
            or isinstance(failed_publications, bool) or not isinstance(failed_publications, int)
            or failed_publications < 0
            or any(not _timestamp_valid(value, now) for value in [*published.values(), *ready.values()])
            or published_at is not None and not _timestamp_valid(published_at, now)):
        raise MbError("invalid authenticated publication progress", reason="progress")
    base = dict(ready=len(ready), published=len(published))
    if not ordinary_ready:
        return Decision(False, "ordinary-handoffs-pending", **base)
    dirty = {key: value for key, value in ready.items() if key not in published or value > published[key]}
    if published_at is not None and not dirty and not ordinary_changed:
        return Decision(False, "complete" if set(published) == expected else "unchanged", **base)
    if not publisher_available:
        return Decision(False, "publisher-active", **base, next_check_at=now)
    if failed_publications >= policy.max_failed_publications:
        return Decision(False, "publication-recovery-budget-exhausted", **base)
    if published_at is None:
        return Decision(True, "initial-ordinary", **base)
    if ordinary_changed:
        return Decision(True, "ordinary-replacement", **base)
    if set(ready) == expected:
        return Decision(True, "final-complete", **base)
    oldest = min(dirty.values())
    deadline = oldest + policy.partial_deadline_seconds
    milestone = len(published) < math.ceil(len(expected) / 2) <= len(ready)
    coalesced = oldest + policy.coalesce_seconds
    if now >= deadline:
        return Decision(True, "partial-deadline", **base)
    if milestone and now >= coalesced:
        return Decision(True, "half-coverage", **base)
    return Decision(False, "coalescing", **base, next_check_at=min(deadline, coalesced) if milestone else deadline)


@dataclass(frozen=True)
class WakeInputs:
    """The identifier inputs of ``pages.yml`` (all optional; validated per operation)."""

    run_id: int | None = None
    sha: str | None = None
    family: str | None = None
    bundle_key: str | None = None
    artifact_id: int | None = None
    artifact_digest: str | None = None
    coverage_sha: str | None = None


@dataclass(frozen=True)
class Admission:
    """``admit``'s outputs (each written to ``$GITHUB_OUTPUT`` as one line of canonical JSON).

    * ``bundle_keys``: the sorted keys to collect (the ``collect`` and ``refresh`` matrices);
    * ``subjects``: ``{key: {branch, commit, tree}}`` for exactly ``bundle_keys``, the authenticated
      target subject of each key (``collect`` passes ``subjects[key].commit`` as ``select
      --expected-subject-commit``; in ``enrolled-branches`` mode keys are opaque tokens, so this is
      the only key -> branch/commit mapping a workflow has);
    * ``families``: ``[{family, key, coverage_sha}]``, the family legs, sorted by ``(family,
      key)``; ``key`` is a bundle key and ``coverage_sha == subjects[key].commit`` (``family
      collect --expected-coverage-sha``);
    * ``nominations``: a bundle key (or ``<family>/<key>``) mapped to an exact artifact id;
    * ``heads``: subject branches mapped to their commits.

    An ineligible admission carries empty collections."""

    eligible: bool
    reason: str
    bundle_keys: list[str] = field(default_factory=list)
    subjects: dict[str, dict[str, str]] = field(default_factory=dict)
    families: list[dict[str, str]] = field(default_factory=list)
    nominations: dict[str, int] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)


def _ineligible(reason: str) -> Admission:
    assert reason in REASONS
    return Admission(eligible=False, reason=reason)


def _fail(message: str, reason: str = "admission") -> MbError:
    return MbError(message, reason=reason)


def _seconds(value: Any, label: str) -> float:
    return jobs.actions_time(value, label).timestamp()


def _validate_wake(operation: str, wake: WakeInputs) -> None:
    if not isinstance(wake, WakeInputs):
        raise _fail("wake inputs must be a WakeInputs record", reason="usage")
    present = {name for name in ("run_id", "sha", "family", "bundle_key", "artifact_id", "artifact_digest",
                                 "coverage_sha") if getattr(wake, name) is not None}
    wanted = {"recovery": set(), "manual": set(), "deploy": {"run_id", "sha"},
              "family": {"run_id", "sha", "family", "bundle_key", "artifact_id", "artifact_digest", "coverage_sha"}}
    if present != wanted[operation]:
        raise _fail(f"a {operation} admission takes exactly the wake inputs {sorted(wanted[operation])}",
                    reason="usage")
    for name in ("run_id", "artifact_id"):
        if name in present:
            grammar.require_positive_int(getattr(wake, name), name)
    for name in ("sha", "coverage_sha"):
        if name in present:
            grammar.require_sha1(getattr(wake, name), name)
    if wake.family is not None:
        grammar.require_family(wake.family)
    if wake.bundle_key is not None:
        grammar.require_key(wake.bundle_key, "bundle key")
    if wake.artifact_digest is not None:
        grammar.require(grammar.DIGEST, wake.artifact_digest, "artifact digest")


class _Admitter:
    """One admission's bounded, memoized view of the repository (API errors always propagate)."""

    def __init__(self, invocation: Invocation, api: GitHubApi, now: float, sleep: Callable[[float], None]) -> None:
        self.invocation = invocation
        self.config = invocation.config
        self.api = api
        self.now = now
        self.sleep = sleep
        self.head = invocation.implementation_sha
        self.default = invocation.config.canonical_branch
        self.subjects: dict[str, dict[str, str]] = {}
        self.sources = SourceRuns(api, invocation)
        self._named: dict[str, list[artifacts.Artifact]] = {}
        self._owners: dict[int, dict[str, Any]] = {}
        self._uploads: dict[int, list[artifacts.Artifact]] = {}
        self._family_handoffs: dict[str, dict[str, tuple[artifacts.Artifact, dict[str, Any]]]] = {}
        self._producer_runs: dict[str, list[dict[str, Any]]] = {}

    # -- memoized reads ----------------------------------------------------------------------------

    def named(self, name: str) -> list[artifacts.Artifact]:
        if name not in self._named:
            self._named[name] = artifacts.list_named(self.api, name)
        return self._named[name]

    def owner(self, run_id: int) -> dict[str, Any]:
        if run_id not in self._owners:
            self._owners[run_id] = runs.get_run(self.api, run_id)
        return self._owners[run_id]

    def uploads(self, run_id: int) -> dict[str, artifacts.Artifact]:
        if run_id not in self._uploads:
            self._uploads[run_id] = artifacts.list_for_run(self.api, run_id)
        listed = self._uploads[run_id]
        exact = {artifact.name: artifact for artifact in listed}
        if len(exact) != len(listed):
            raise _fail(f"run {run_id} owns duplicate artifact names")
        return exact

    def successful_runs(self, commit: str, subject_canonical: bool) -> list[dict[str, Any]]:
        """The source runs that may supply a subject's evidence, newest first: with
        ``source.require_newest_run`` only the newest run, when it succeeded (``select`` never looks
        further), otherwise the newest ``limits.MAX_CANDIDATES`` successful runs (one memoized listing
        serves every subject; see :class:`mod_base.pages.select.SourceRuns`)."""

        found = self.sources.for_subject(commit, subject_canonical=subject_canonical)
        if self.config.source["require_newest_run"]:
            return found[:1] if found and is_success(found[0]) else []
        return [run for run in found if is_success(run)][:lim.MAX_CANDIDATES]

    def head_current(self) -> bool:
        return contents.branch_head(self.api, self.default)[0] == self.head

    # -- shared checks -----------------------------------------------------------------------------

    def keys(self) -> list[str]:
        return sorted(self.subjects)

    def legs(self) -> list[tuple[str, str]]:
        legs = sorted((family["id"], key) for family in self.config.families for key in self.keys())
        if len(legs) > lim.MAX_FAMILY_LEGS:
            raise _fail(f"{len(legs)} family legs exceed the {lim.MAX_FAMILY_LEGS}-job matrix bound")
        return legs

    def is_canonical(self, key: str) -> bool:
        return self.subjects[key]["branch"] == self.default

    def groups(self) -> dict[tuple[str, bool], list[str]]:
        """``(subject commit, subject is the canonical branch)`` -> its sorted keys (the source runs
        and their admissible events depend on both)."""

        grouped: dict[tuple[str, bool], list[str]] = {}
        for key in self.keys():
            grouped.setdefault((self.subjects[key]["commit"], self.is_canonical(key)), []).append(key)
        return grouped

    def window_valid(self, artifact: artifacts.Artifact, run: Mapping[str, Any], job: Mapping[str, Any] | None,
                     step: str, *, max_size: int | None = None) -> bool:
        """QS ``artifact_valid``: the exact upload of this successful attempt's job step (at most
        ``max_size`` archive bytes; by default a handoff's :func:`~mod_base.io.bounded_zip.artifact_limit`)."""

        maximum = artifact_limit("handoff") if max_size is None else max_size
        return (not artifact.expired and 0 < artifact.size <= maximum and artifact.run_id == run.get("id")
                and artifact.head_sha == run.get("head_sha") and artifact.head_branch == run.get("head_branch")
                and upload_in_window(artifact, job, step))

    def owned_cache(self, name: str, *, after: artifacts.Artifact | None = None) -> artifacts.Artifact | None:
        """The newest ``name`` owned by a successful Pages run at the protected head; with ``after``
        (a handoff) only one that :func:`~mod_base.pages.select.supersedes` it, as ``select`` takes
        it (a cache uploaded no later than ``after`` cannot, so no older owner is read)."""

        candidates = [artifact for artifact in self.named(name) if not artifact.expired
                      and artifact.head_branch == self.default and artifact.head_sha == self.head]
        for artifact in candidates[:lim.MAX_CANDIDATES]:
            if after is not None and artifact.order[0] <= after.order[0]:
                return None
            owner = self.owner(artifact.run_id)
            if self.pages_owner(owner) and (after is None or supersedes(owner, after)):
                return artifact
        return None

    def pages_owner(self, run: Mapping[str, Any]) -> bool:
        return pages_owner_valid(run, self.invocation, default_branch=self.default,
                                 pages_workflow_id=self.sources.pages_workflow_id, head_sha=self.head)

    def producer_runs(self, family: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The successful producer runs of ``family`` at the head, newest first (one listing per
        producer workflow)."""

        workflow = family["producer"]["workflow"]
        if workflow not in self._producer_runs:
            self._producer_runs[workflow] = runs.workflow_runs(self.api, workflow, head_sha=self.head,
                                                               status="success", max_items=lim.MAX_SUBJECT_RUNS)
        return [run for run in self._producer_runs[workflow]
                if producer_run_valid(run, self.invocation, family, default_branch=self.default, head_sha=self.head)]

    def family_handoffs(self, family: Mapping[str, Any]) -> dict[str, tuple[artifacts.Artifact, dict[str, Any]]]:
        """Key -> the newest usable family handoff at the head and its producer run."""

        family_id = family["id"]
        if family_id not in self._family_handoffs:
            found: dict[str, tuple[artifacts.Artifact, dict[str, Any]]] = {}
            for run in self.producer_runs(family)[:lim.MAX_CANDIDATES]:
                for name, artifact in self.uploads(run["id"]).items():
                    parsed = grammar.parse_artifact_name(name)
                    if (parsed is not None and parsed.kind == "family-handoff" and parsed.family == family_id
                            and parsed.attempt == run["run_attempt"] and parsed.key in self.subjects
                            and not artifact.expired and 0 < artifact.size <= family_archive_limit(family)):
                        found.setdefault(parsed.key, (artifact, run))
            self._family_handoffs[family_id] = found
        return self._family_handoffs[family_id]

    def run_handoffs(self, run: Mapping[str, Any]) -> dict[str, artifacts.Artifact]:
        """Key -> the handoff ``run``'s latest attempt uploaded (names are unique per run)."""

        handoffs: dict[str, artifacts.Artifact] = {}
        for name, artifact in self.uploads(run["id"]).items():
            parsed = grammar.parse_artifact_name(name)
            if parsed is None or parsed.kind != "handoff" or parsed.attempt != run["run_attempt"]:
                continue
            handoffs[parsed.key] = artifact  # type: ignore[index]
        return handoffs

    # -- wakes -------------------------------------------------------------------------------------

    def deploy(self, wake: WakeInputs) -> dict[str, int] | None:
        """The nominations of an authenticated deploy wake, or ``None`` for a stale wake.

        The run must be a successful source run on the canonical branch (the protected controller;
        Block Pops' wake rule) at ``sha``, which must still be the protected head. Each handoff of
        its latest attempt must name a target key; a key whose current subject the run no longer
        names (its branch moved on) is dropped, and a wake left with none is stale. A run whose
        event cannot vouch for a key's subject fails the wake."""

        source = self.config.source
        run = runs.wait_for_completion(self.api, wake.run_id, attempts=lim.RUN_POLL_ATTEMPTS,  # type: ignore[arg-type]
                                       interval=lim.RUN_POLL_INTERVAL_SECONDS, sleep=self.sleep)
        runs.run_order(run)
        if not (source_run_valid(self.invocation, run, source_workflow_id=self.sources.source_workflow_id)
                and is_success(run) and isinstance(run.get("event"), str)
                and run.get("event") in subject_events(self.invocation, subject_canonical=True)
                and run.get("head_sha") == wake.sha):
            raise _fail(f"the deploy wake does not name a successful {source['workflow']} run of {self.default} at "
                        f"{wake.sha}", reason="wake")
        if wake.sha != self.head:
            return None
        handoffs: dict[str, artifacts.Artifact] = {}
        for name, artifact in self.uploads(run["id"]).items():
            parsed = grammar.parse_artifact_name(name)
            if parsed is None or parsed.kind != "handoff":
                continue
            if parsed.key not in self.subjects:
                raise _fail(f"the deploy wake's run hands off {name}, which is not a target", reason="wake")
            if parsed.attempt != run["run_attempt"]:
                continue
            if (artifact.expired or not 0 < artifact.size <= artifact_limit("handoff")
                    or artifact.head_sha != wake.sha or artifact.head_branch != self.default):
                raise _fail(f"the deploy wake's handoff {name} is expired, oversized or foreign", reason="wake")
            handoffs[parsed.key] = artifact  # type: ignore[index]
        if not 1 <= len(handoffs) <= lim.MAX_KEYS:
            raise _fail(f"the deploy wake's run hands off {len(handoffs)} keys in attempt {run['run_attempt']}",
                        reason="wake")
        nominations: dict[str, int] = {}
        for key, artifact in sorted(handoffs.items()):
            if not names_commit(self.invocation, run, self.subjects[key]["commit"]):
                continue
            if run["event"] not in subject_events(self.invocation, subject_canonical=self.is_canonical(key)):
                raise _fail(f"the deploy wake's {run['event']} run cannot vouch for {key}", reason="wake")
            nominations[key] = artifact.id
        return nominations or None

    def family_wake(self, wake: WakeInputs) -> dict[str, int] | None:
        """The nomination of an authenticated family wake, or ``None`` for a stale wake."""

        family = self.config.family(wake.family)  # type: ignore[arg-type]
        if wake.bundle_key not in self.subjects:
            raise _fail(f"the family wake names {wake.bundle_key}, which is not a target", reason="wake")
        artifact = artifacts.get_artifact(self.api, wake.artifact_id)  # type: ignore[arg-type]
        parsed = grammar.require_artifact_name(artifact.name, "family-handoff")
        if (parsed.family != wake.family or parsed.key != wake.bundle_key or artifact.digest != wake.artifact_digest
                or artifact.run_id != wake.run_id or artifact.expired
                or not 0 < artifact.size <= family_archive_limit(family)
                or artifact.head_sha != wake.sha or artifact.head_branch != self.default):
            raise _fail(f"the family wake's artifact {wake.artifact_id} is not the named family handoff", reason="wake")
        run = runs.wait_for_completion(self.api, wake.run_id, attempts=lim.RUN_POLL_ATTEMPTS,  # type: ignore[arg-type]
                                       interval=lim.RUN_POLL_INTERVAL_SECONDS, sleep=self.sleep)
        if not producer_run_valid(run, self.invocation, family, default_branch=self.default,
                                  head_sha=wake.sha):  # type: ignore[arg-type]
            raise _fail(f"the family wake does not name a successful {family['producer']['workflow']} run at "
                        f"{wake.sha}", reason="wake")
        subject = self.subjects[wake.bundle_key]  # type: ignore[index]
        if (run.get("run_attempt") != parsed.attempt or not wake.coverage_sha == wake.sha == self.head
                or subject["commit"] != wake.coverage_sha):
            return None
        return {f"{family['id']}/{parsed.key}": artifact.id}

    # -- current, deferral and cutover ---------------------------------------------------------------

    def current(self, legs: list[tuple[str, str]]) -> bool:
        """Every key is cached at its subject by a successful Pages run at the head, and no source
        run for that subject settled after that owner started while handing the key off (a cache
        is not a tombstone: a lost same-head replacement wake stays recoverable, QS discover);
        every family leg has no family handoff at the head or a cache there that supersedes it."""

        caches: dict[str, artifacts.Artifact] = {}
        for key in self.keys():
            cache = self.owned_cache(grammar.cache_name(key, self.subjects[key]["commit"]))
            if cache is None:
                return False
            caches[key] = cache
        for key, cache in caches.items():
            started = _seconds(self.owner(cache.run_id).get("created_at"), "Pages owner created_at")
            for run in self.successful_runs(self.subjects[key]["commit"], self.is_canonical(key)):
                if _seconds(run.get("updated_at"), "run updated_at") < started:
                    continue
                handoff = self.run_handoffs(run).get(key)
                if handoff is not None and not handoff.expired:
                    return False
        for family_id, key in legs:
            commit = self.subjects[key]["commit"]
            if commit != self.head:
                continue
            handoff = self.family_handoffs(self.config.family(family_id)).get(key)
            if handoff is None:
                continue
            if self.owned_cache(grammar.family_cache_name(family_id, key, commit), after=handoff[0]) is None:
                return False
        return True

    def _active_page(self, path: str, status: str) -> list[dict[str, Any]]:
        """The one page of ``status`` runs at ``path``; rows that disagree with its ``total_count``
        (runs start and settle while it is read) are an :class:`InconsistentListing`. A full page is
        confirmed by an empty second one, so a lagging ``total_count`` hides no run."""

        def read(params: dict[str, str | int]) -> tuple[list[dict[str, Any]], int]:
            page = self.api.get_json(path, params={"status": status, "per_page": 100, **params})
            rows = page.get("workflow_runs") if isinstance(page, dict) else None
            total = page.get("total_count") if isinstance(page, dict) else None
            if (isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= 100
                    or not isinstance(rows, list) or len(rows) > 100
                    or any(not isinstance(row, dict) for row in rows)):
                raise _fail("the active source-run inventory is malformed, truncated or oversized")
            return rows, total

        def inconsistent(listed: int, total: int) -> InconsistentListing:
            return InconsistentListing(f"the active source-run inventory lists {listed} of its total_count {total} "
                                       "runs", status=200, method="GET", path=path)

        rows, total = read({})
        if len(rows) != total:
            raise inconsistent(len(rows), total)
        if len(rows) == 100:
            following, count = read({"page": 2})
            if following or count != total:
                raise inconsistent(len(rows) + len(following), count)
        return rows

    def active_source_runs(self) -> int:
        """Source runs still settling that could hand off evidence for a subject (QS discover). Each
        status is one page, read again while it is inconsistent (``api.read_listing``)."""

        source = self.config.source
        commits = {commit for commit, _ in self.groups()}
        titles = {display_title(self.invocation, commit) for commit in commits} - {None}
        events = frozenset(source["events"]["canonical"]) | frozenset(source["events"]["other"])
        path = f"/repos/{self.api.repository}/actions/workflows/{source['workflow'].rsplit('/', 1)[1]}/runs"
        count = 0
        for status in _ACTIVE_STATUS_ORDER:
            rows = self.api.read_listing(functools.partial(self._active_page, path, status))
            for run in rows:
                if run.get("path") != source["workflow"]:
                    raise _fail("the active source-run inventory lists another workflow")
                event, head, title = run.get("event"), run.get("head_sha"), run.get("display_title")
                if (run.get("status") != "completed" and run.get("head_branch") == self.default
                        and isinstance(event, str) and event in events
                        and ((isinstance(head, str) and head in commits)
                             or (isinstance(title, str) and title in titles))):
                    count += 1
        return count

    def missing_v1_evidence(self, nominations: Mapping[str, int]) -> list[str]:
        """Keys with neither a v1 cache nor a v1 handoff for their subject (cheapest first: the
        memoized cache inventories, then source runs only until every remaining key is found)."""

        missing = []
        for (commit, canonical), keys in self.groups().items():
            remaining = [key for key in keys if key not in nominations and not any(
                not artifact.expired and artifact.head_branch == self.default
                for artifact in self.named(grammar.cache_name(key, commit)))]
            for run in self.successful_runs(commit, canonical) if remaining else []:
                handoffs = self.run_handoffs(run)
                remaining = [key for key in remaining if key not in handoffs or handoffs[key].expired]
                if not remaining:
                    break
            missing += remaining
        return sorted(missing)

    # -- progress (QS publication_progress.plan) ------------------------------------------------------

    def published_snapshot(self, legs: list[tuple[str, str]]) -> tuple[dict[str, float], float | None, float | None]:
        """Reuse only the newest exact successful atomic owner, never a union of sibling runs."""

        keys = self.keys()
        probe = grammar.cache_name(keys[0], self.subjects[keys[0]]["commit"])
        candidates = sorted(self.named(probe), key=lambda artifact: artifact.id, reverse=True)[:lim.MAX_CANDIDATES]
        for candidate in candidates:
            if candidate.expired or candidate.head_sha != self.head or candidate.head_branch != self.default:
                continue
            run = self.owner(candidate.run_id)
            if not self.pages_owner(run):
                continue
            attempt = run["run_attempt"]
            attempt_jobs = jobs.attempt_jobs(self.api, run["id"], attempt)
            required = [api_job_name("publish", "build"), caller_job_name("deploy")]
            for key in keys:
                required += [api_job_name("publish", "collect", key=key), api_job_name("finalize", "refresh", key=key)]
            for family_id, key in legs:
                required += [api_job_name("publish", "family", family=family_id, key=key),
                             api_job_name("finalize", "refresh_family", family=family_id, key=key)]
            if any(successful_job(attempt_jobs, name, attempt) is None for name in required):
                continue
            exact = self.uploads(run["id"])
            caches = {key: exact.get(grammar.cache_name(key, self.subjects[key]["commit"])) for key in keys}
            if (any(cache is None or not self.window_valid(
                        cache, run, successful_job(attempt_jobs, api_job_name("finalize", "refresh", key=key), attempt),
                        step_name("cache_upload")) for key, cache in caches.items())
                    or exact[probe].id != candidate.id):
                continue
            published = {}
            for family_id, key in legs:
                cache = exact.get(grammar.family_cache_name(family_id, key, self.subjects[key]["commit"]))
                if cache is None:
                    continue
                refresh = successful_job(attempt_jobs, api_job_name("finalize", "refresh_family", family=family_id,
                                                                    key=key), attempt)
                if not self.window_valid(cache, run, refresh, step_name("family_cache_upload")):
                    raise _fail("a successful Pages family cache belongs to another attempt or step")
                # An upload time cannot prove what an earlier collector observed: republish
                # conservatively from the collector's selection boundary (QS).
                published[f"{family_id}/{key}"] = self._step_started(
                    successful_job(attempt_jobs, api_job_name("publish", "family", family=family_id, key=key), attempt),
                    step_name("family_select"))
            boundary = min(self._step_started(successful_job(attempt_jobs, api_job_name("publish", "collect", key=key),
                                                             attempt), step_name("select")) for key in keys)
            return published, _seconds(run.get("updated_at"), "Pages owner updated_at"), boundary
        return {}, None, None

    @staticmethod
    def _step_started(job: Mapping[str, Any] | None, name: str) -> float:
        if job is None:
            raise _fail("a published collector lacks its exact selection boundary")
        step = jobs.require_successful_step(job, name)
        return _seconds(step.get("started_at"), f"step {name!r} started_at")

    def producer_settled_since(self, oldest: float) -> bool:
        """One bounded producer query per family: a same-head producer finished after the oldest
        published family boundary (a lost replacement wake)."""

        for family in self.config.families:
            if any(_seconds(run.get("updated_at"), "producer updated_at") >= oldest
                   for run in self.producer_runs(family)):
                return True
        return False

    def ready_snapshot(self, legs: list[tuple[str, str]], published: Mapping[str, float]
                       ) -> tuple[dict[str, float], dict[str, int]]:
        ready = dict(published)
        nominations: dict[str, int] = {}
        for family in self.config.families:
            family_id, producer = family["id"], family["producer"]
            keys = [key for leg_family, key in legs
                    if leg_family == family_id and self.subjects[key]["commit"] == self.head]
            found: set[str] = set()
            for run in self.producer_runs(family)[:lim.MAX_CANDIDATES]:
                pending = [key for key in keys if key not in found]
                if not pending:
                    break
                attempt_jobs = jobs.attempt_jobs(self.api, run["id"], run["run_attempt"])
                job = successful_job(attempt_jobs, producer["job"], run["run_attempt"])
                exact = self.uploads(run["id"])
                for key in pending:
                    artifact = exact.get(grammar.family_handoff_name(family_id, key, run["run_attempt"]))
                    if artifact is None or not self.window_valid(artifact, run, job, producer["step"],
                                                                 max_size=family_archive_limit(family)):
                        continue
                    # The inventory is immutable-id addressed; recheck the exact id after owner
                    # admission so a stale, rerun or deleted nomination cannot suppress work (QS).
                    if artifacts.get_artifact(self.api, artifact.id) != artifact:
                        raise _fail(f"family handoff {artifact.id} changed during admission")
                    leg = f"{family_id}/{key}"
                    created = max(artifact.order[0].timestamp(), _seconds(run.get("updated_at"), "producer updated_at"))
                    ready[leg] = max(ready.get(leg, 0.0), created)
                    nominations[leg] = artifact.id
                    found.add(key)
        return ready, nominations

    def ordinary_snapshot(self, consumed_before: float | None) -> tuple[dict[str, int], float | None, bool]:
        """Per subject, the newest successful source attempt settled after ``consumed_before`` whose
        window-checked handoffs cover every key of that subject (QS: one complete E2E run)."""

        source = self.config.source
        nominations: dict[str, int] = {}
        ordinary_at: float | None = None
        complete = True
        for (commit, canonical), keys in self.groups().items():
            found = False
            for run in self.successful_runs(commit, canonical):
                if consumed_before is not None and _seconds(run.get("updated_at"), "run updated_at") < consumed_before:
                    continue
                handoffs = self.run_handoffs(run)
                if set(handoffs) != set(keys):
                    continue
                attempt_jobs = jobs.attempt_jobs(self.api, run["id"], run["run_attempt"])
                if all(self.window_valid(handoffs[key], run,
                                         successful_job(attempt_jobs, handoff_job_name(self.invocation, key),
                                                        run["run_attempt"]), source["handoff_step"]) for key in keys):
                    nominations.update({key: handoffs[key].id for key in keys})
                    settled = _seconds(run.get("updated_at"), "run updated_at")
                    ordinary_at = settled if ordinary_at is None else max(ordinary_at, settled)
                    found = True
                    break
            complete = complete and found
        return nominations, ordinary_at, complete

    def failed_publications(self, since: float, policy: ProgressPolicy) -> int:
        """Failed or cancelled Pages runs at the head since ``since`` whose build actually started
        (failed wake authentication and rotation are never publication attempts)."""

        build = api_job_name("publish", "build")
        count = 0
        for status in ("failure", "cancelled"):
            listing = runs.workflow_runs(self.api, PAGES_WORKFLOW_PATH, head_sha=self.head, status=status,
                                         max_items=lim.MAX_SUBJECT_RUNS)
            for run in listing[:lim.MAX_CANDIDATES]:
                head, event = run.get("head_repository"), run.get("event")
                if (_seconds(run.get("created_at"), "Pages run created_at") < since
                        or run.get("head_branch") != self.default or run.get("status") != "completed"
                        or not isinstance(event, str) or event not in PAGES_EVENTS or not isinstance(head, Mapping)
                        or head.get("full_name") != self.invocation.repository):
                    continue
                rows = self.api.paginate(
                    f"/repos/{self.api.repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs",
                    field="jobs", max_items=lim.MAX_JOBS_PER_ATTEMPT)
                if any(job.get("name") == build and job.get("started_at") and job.get("conclusion") != "skipped"
                       for job in rows):
                    count += 1
                    if count >= policy.max_failed_publications:
                        return count
        return count

    def progress(self, legs: list[tuple[str, str]], *, check_complete: bool) -> tuple[Decision, dict[str, int]]:
        policy = ProgressPolicy.from_config(self.config.admission)
        leg_ids = {f"{family_id}/{key}" for family_id, key in legs}
        published, published_at, boundary = self.published_snapshot(legs)
        complete = bool(leg_ids) and set(published) == leg_ids
        if complete and not check_complete:
            check_complete = self.producer_settled_since(min(published.values()))
        if complete and not check_complete:
            ready, family_nominations = dict(published), {}
        else:
            ready, family_nominations = self.ready_snapshot(legs, published)
        ordinary_nominations, ordinary_at, ordinary_complete = self.ordinary_snapshot(boundary)
        if leg_ids:
            expected, published_view, ready_view = leg_ids, published, ready
        else:
            expected = {_ORDINARY_LEG}
            published_view = ready_view = {} if published_at is None else {_ORDINARY_LEG: published_at}
        arguments: dict[str, Any] = dict(expected=expected, published=published_view, ready=ready_view,
                                         ordinary_ready=published_at is not None or ordinary_complete,
                                         published_at=published_at, now=self.now,
                                         ordinary_changed=published_at is not None and bool(ordinary_nominations),
                                         policy=policy)
        decision = decide(**arguments)
        if decision.eligible:
            since = max([published_at or 0.0, ordinary_at or 0.0, *ready.values()])
            decision = decide(**arguments, failed_publications=self.failed_publications(since, policy))
        return decision, {**ordinary_nominations, **family_nominations}

    # -- the admission --------------------------------------------------------------------------------

    def admit(self, operation: str, wake: WakeInputs) -> Admission:
        if contents.default_branch(self.api) != self.default:
            raise _fail(f"config.canonical_branch {self.default} is not the repository's default branch",
                        reason="canonical-branch")
        if not self.head_current():
            return _ineligible("stale-implementation")
        try:
            targets = discover_targets(self.invocation, api=self.api)
        except MbError as exc:
            if exc.reason == "stale-implementation":
                return _ineligible("stale-implementation")
            raise
        self.subjects = {target["key"]: dict(target["subject"]) for target in targets}
        nominations: dict[str, int] = {}
        if operation in ("deploy", "family"):
            woken = self.deploy(wake) if operation == "deploy" else self.family_wake(wake)
            if woken is None:
                return _ineligible("stale-wake")
            nominations.update(woken)
        legs = self.legs()
        if operation in ("recovery", "manual") and self.current(legs):
            return _ineligible("current" if self.head_current() else "stale-implementation")
        if operation != "manual" and self.config.admission["defer_on_active_source_runs"] and self.active_source_runs():
            return _ineligible("deferred-active-source")
        if self.missing_v1_evidence(nominations):
            return _ineligible("awaiting-complete-v1-evidence")
        if operation == "manual":
            reason = "manual"
        elif self.config.admission["mode"] == "always":
            reason = "family-wake" if operation == "family" else "always"
        else:
            decision, progressed = self.progress(legs, check_complete=operation == "family")
            if not decision.eligible:
                return _ineligible(decision.reason)
            reason = decision.reason
            nominations.update(progressed)
        if not self.head_current():
            return _ineligible("stale-implementation")
        return Admission(
            eligible=True,
            reason=reason,
            bundle_keys=self.keys(),
            subjects={key: dict(self.subjects[key]) for key in self.keys()},
            families=[{"family": family_id, "key": key, "coverage_sha": self.subjects[key]["commit"]}
                      for family_id, key in legs],
            nominations=dict(sorted(nominations.items())),
            heads={subject["branch"]: subject["commit"] for subject in self.subjects.values()},
        )


def admit(invocation: Invocation, *, api: GitHubApi, operation: str, wake: WakeInputs, now: float | None = None,
          sleep: Callable[[float], None] = time.sleep) -> Admission:
    """Decide whether this Pages run publishes (see module docstring). ``operation`` is one of
    ``workflow.PUBLISH_OPERATIONS``. An ineligible admission is a normal outcome (exit 0)."""

    if operation not in PUBLISH_OPERATIONS:
        raise _fail(f"unknown publication operation {operation!r}"[:80], reason="usage")
    _validate_wake(operation, wake)
    moment = time.time() if now is None else now
    if isinstance(moment, bool) or not isinstance(moment, (int, float)) or not math.isfinite(moment) or moment < 0:
        raise _fail("the admission clock must be a finite non-negative time", reason="usage")
    return _Admitter(invocation, api, float(moment), sleep).admit(operation, wake)
