"""Publication admission (MB5): QS ``publication_progress.decide`` + wake/recovery/family admission.

``decide`` is QS's pure policy with identical constants and prefix-aware exact job names
(:mod:`mod_base.workflow`); producer upload windows come from ``config.source.handoff_job/
handoff_step`` and ``families[].producer.job/step``. ``admit`` adds: live default head ==
``GITHUB_SHA`` (else ``stale-implementation``, exit 0), canonical branch == API default branch,
deploy/family wake authentication (polling the source run up to 30 x 2 s), ``stale-wake``,
``current`` (every key cached at its subject by a successful Pages run and every family current),
``deferred-active-source``, and the v1 cutover reason ``awaiting-complete-v1-evidence``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB5"

COALESCE_SECONDS = 10 * 60
PARTIAL_DEADLINE_SECONDS = 45 * 60
RECOVERY_INTERVAL_SECONDS = 60 * 60
MAX_REQUESTS = 160
MAX_CANDIDATES = 8
MAX_FAILED_PUBLICATIONS = 3
ACTIVE_RUN_STATUSES = frozenset({"requested", "queued", "pending", "waiting", "in_progress"})
REASONS = frozenset({
    "stale-implementation", "stale-wake", "current", "deferred-active-source", "awaiting-complete-v1-evidence",
    "ordinary-handoffs-pending", "complete", "unchanged", "publisher-active",
    "publication-recovery-budget-exhausted", "initial-ordinary", "ordinary-replacement", "final-complete",
    "partial-deadline", "half-coverage", "coalescing", "family-wake", "always", "manual",
})


@dataclass(frozen=True)
class ProgressPolicy:
    coalesce_seconds: int = COALESCE_SECONDS
    partial_deadline_seconds: int = PARTIAL_DEADLINE_SECONDS
    recovery_interval_seconds: int = RECOVERY_INTERVAL_SECONDS
    max_failed_publications: int = MAX_FAILED_PUBLICATIONS

    @classmethod
    def from_config(cls, admission: Mapping[str, Any]) -> "ProgressPolicy":
        """The policy of a ``progress``-mode ``config.admission``."""

        raise NotImplementedError("owned by MB5")


@dataclass(frozen=True)
class Decision:
    eligible: bool
    reason: str
    ready: int
    published: int
    next_check_at: float | None = None


def decide(*, expected: set[str], published: dict[str, float], ready: dict[str, float], ordinary_ready: bool,
           published_at: float | None, now: float, publisher_available: bool = True, failed_publications: int = 0,
           ordinary_changed: bool = False, policy: ProgressPolicy = ProgressPolicy()) -> Decision:
    """QS ``publication_progress.decide`` (pure, deterministic; same reasons and ordering)."""

    raise NotImplementedError("owned by MB5")


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


def admit(invocation: Invocation, *, api: GitHubApi, operation: str, wake: WakeInputs, now: float | None = None,
          sleep: Callable[[float], None] = time.sleep) -> Admission:
    """Decide whether this Pages run publishes (see module docstring). ``operation`` is one of
    ``workflow.PUBLISH_OPERATIONS``. An ineligible admission is a normal outcome (exit 0)."""

    raise NotImplementedError("owned by MB5")
