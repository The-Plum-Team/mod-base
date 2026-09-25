"""Exact-ID rotation of superseded evidence after an authenticated successful Pages run (MB7).

QS ``rotate_artifacts`` engine (families, ``DeletionBudget(32)``, deferral) + BP ``_RotationReads``
(pinned reads, re-observation). ``R`` is the owner run (``--owner-run-id``/``--owner-sha``),
authenticated ``completed/success`` on ``pages.yml`` at the default head. Its ``mb-promotion`` and
caches are downloaded by id and validated structurally; the retirement plan follows SPEC §5.5;
each DELETE is preceded by a re-observation of the artifact (id, name, owner run, ``created_at``
not newer than R) and spaced by ``delete_delay_seconds``. Never touches a non-``mb-`` name,
anything newer than R, the newest anchor per key, or ``mb-baseline--*``. Reads config data only.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.model import limits
from mod_base.runtime import Invocation

OWNER = "MB7"


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

    def begin_scope(self) -> None:
        raise NotImplementedError("owned by MB7")

    def select(self, artifacts: list[object]) -> list[object]:
        raise NotImplementedError("owned by MB7")

    def consume(self) -> None:
        raise NotImplementedError("owned by MB7")


def rotate_generation(invocation: Invocation, *, api: GitHubApi, owner_run_id: int, owner_sha: str,
                      delete_delay_seconds: float = 1.0, dry_run: bool = False, now: float | None = None,
                      sleep: Callable[[float], None] = time.sleep) -> dict[str, object]:
    """Retire everything R superseded and return the JSON summary (QS keys plus
    ``remaining_rotation_deletions``). ``dry_run`` plans and re-observes without deleting."""

    raise NotImplementedError("owned by MB7")
