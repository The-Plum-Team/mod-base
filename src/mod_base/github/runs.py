"""Workflow-run reads and exact run validation (MB1).

Block Pops ``_validate_run``/``_historical_run``/``_run_order`` plus Quick Skin owner checks.
Attempts are always read through ``/actions/runs/{id}/attempts/{n}`` when an attempt matters.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Mapping
from datetime import datetime
from typing import Any

from mod_base import KIT_REPOSITORY
from mod_base.github.api import GitHubApi
from mod_base.model import limits

OWNER = "MB1"


def get_run(api: GitHubApi, run_id: int) -> dict[str, Any]:
    """``GET /repos/{repo}/actions/runs/{run_id}`` (an object or :class:`ApiError`)."""

    raise NotImplementedError("owned by MB1")


def get_run_attempt(api: GitHubApi, run_id: int, run_attempt: int) -> dict[str, Any]:
    """The historical attempt endpoint; its ``run_attempt`` must equal ``run_attempt``."""

    raise NotImplementedError("owned by MB1")


def validate_run(run: Mapping[str, Any], *, repository: str, workflow_path: str, events: Collection[str],
                 head_branch: str | None = None, head_sha: str | None = None, workflow_id: int | None = None,
                 require_success: bool = True, display_title: str | None = None) -> None:
    """Require exact provenance: ``path``, ``event`` in ``events``, ``head_repository.full_name``,
    and (when given) ``head_branch``, ``head_sha``, ``workflow_id``, ``display_title``; with
    ``require_success`` also ``status == "completed"`` and ``conclusion == "success"``.
    Raises :class:`mod_base.errors.MbError` on any difference."""

    raise NotImplementedError("owned by MB1")


def run_order(run: Mapping[str, Any]) -> tuple[datetime, int, int]:
    """``(created_at, id, run_attempt)`` after strict shape validation (a total dispatch order)."""

    raise NotImplementedError("owned by MB1")


def referenced_kit_sha(run: Mapping[str, Any], *, kit_repository: str = KIT_REPOSITORY) -> str:
    """The single kit SHA a run resolved: every ``referenced_workflows[]`` entry whose ``path``
    starts with ``<kit_repository>/.github/workflows/`` must end in ``@<sha>`` equal to its
    ``sha``, and exactly one distinct 40-hex SHA must result (SPEC §1.2 step 3)."""

    raise NotImplementedError("owned by MB1")


def workflow_runs(api: GitHubApi, workflow_path: str, *, branch: str | None = None, head_sha: str | None = None,
                  event: str | None = None, status: str | None = None,
                  max_items: int = 1000) -> list[dict[str, Any]]:
    """List runs of ``workflow_path`` (by file name) newest first with the given filters; the
    response ``total_count`` must equal the listed rows when it is at most ``max_items``."""

    raise NotImplementedError("owned by MB1")


def wait_for_completion(api: GitHubApi, run_id: int, *, attempts: int = limits.RUN_POLL_ATTEMPTS,
                        interval: float = limits.RUN_POLL_INTERVAL_SECONDS,
                        sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Poll a run until ``status == "completed"`` (at most ``attempts`` reads); raise otherwise."""

    raise NotImplementedError("owned by MB1")


def run_record(run: Mapping[str, Any], claim: Mapping[str, Any], *,
               require_controller_head: bool = True) -> dict[str, Any]:
    """Build the ``RunRecord`` (SPEC §3.0) for an authenticated API ``run`` and its ``RunClaim``:
    the claim's run id/attempt/workflow path must equal the run's; ``head_sha``, ``event``,
    ``created_at``, ``conclusion`` (``success``) and ``display_title`` come from the run. The
    result validates as ``documents.run_record``.

    With ``require_controller_head`` (the default) the run's ``head_sha`` must equal the claim's
    ``controller_sha``: a handoff or family producer run (its own controller, see
    ``documents.own_run_record``) and a ``none``/``attested`` tested run. Pass ``False`` only for
    a ``delegated`` tested run, whose API head legitimately differs (Quick Skin PR reuse tests a
    merge commit that is not the PR run's head); that reuse is proven by the adapter's
    ``authenticate_extensions`` instead (SPEC §4.8 step 4)."""

    raise NotImplementedError("owned by MB1")
