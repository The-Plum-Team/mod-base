"""Exact job lookup, attempt-scoped listing and generic job-graph hashing (MB1).

``find_job`` is :func:`mod_base.workflow.find_job` (exact equality, one match, same attempt).
The job graph is the canonical form of Block Pops ``e2e_job_graph.validate_jobs``: the sorted
list of ``{name, conclusion}`` of one attempt, hashed as ``canonical_json``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.workflow import find_job

OWNER = "MB1"
__all__ = ["attempt_jobs", "find_job", "job_graph", "job_graph_sha256", "require_job_graph", "step_window",
           "require_successful_step"]


def attempt_jobs(api: GitHubApi, run_id: int, run_attempt: int) -> list[dict[str, Any]]:
    """Every job of exactly one attempt (``/runs/{id}/attempts/{n}/jobs``, paginated, bounded by
    ``limits.MAX_JOBS_PER_ATTEMPT``); job ids must be unique and each job's ``run_attempt`` equal."""

    raise NotImplementedError("owned by MB1")


def job_graph(jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The canonical ``[{name, conclusion}]`` of ``jobs`` sorted by name (names must be unique)."""

    raise NotImplementedError("owned by MB1")


def job_graph_sha256(graph: Sequence[Mapping[str, str]]) -> str:
    """SHA-256 of ``canonical_json(graph)``."""

    raise NotImplementedError("owned by MB1")


def require_job_graph(jobs: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> str:
    """Require the observed graph to equal ``expected`` exactly (adapter ``expected_source_jobs``)
    and return its SHA-256; any missing, extra or differently concluded job raises."""

    raise NotImplementedError("owned by MB1")


def step_window(job: Mapping[str, Any], step_name: str) -> tuple[datetime, datetime]:
    """``(started_at, completed_at)`` of the single step named exactly ``step_name`` in ``job``."""

    raise NotImplementedError("owned by MB1")


def require_successful_step(job: Mapping[str, Any], step_name: str) -> dict[str, Any]:
    """The single step named exactly ``step_name``, which must be ``completed``/``success``."""

    raise NotImplementedError("owned by MB1")
