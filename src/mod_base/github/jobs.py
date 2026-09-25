"""Exact job lookup, attempt-scoped listing and generic job-graph hashing (MB1).

``find_job`` is :func:`mod_base.workflow.find_job` (exact equality, one match, same attempt).
The job graph is the canonical form of Block Pops ``e2e_job_graph.validate_jobs``: the sorted
list of ``{name, conclusion}`` of one attempt, hashed as ``canonical_json``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from mod_base.adapter.protocol import JOB_CONCLUSIONS
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.model import limits
from mod_base.model.canonical import canonical_sha256
from mod_base.workflow import find_job

OWNER = "MB1"
__all__ = ["attempt_jobs", "find_job", "job_graph", "job_graph_sha256", "require_job_graph", "step_window",
           "require_successful_step"]

#: Actions timestamps: UTC ``Z`` or a numeric offset, optionally with fractional seconds.
_STEP_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
MAX_STEPS_PER_JOB = 1000


def _fail(message: str) -> MbError:
    return MbError(message, reason="job-graph")


def _positive(value: Any, label: str, maximum: int = limits.MAX_RUN_ID) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise _fail(f"{label} must be a positive integer no larger than {maximum}")
    return value


def _name(value: Any, label: str) -> str:
    if (not isinstance(value, str) or not value or len(value) > limits.MAX_JOB_NAME_LENGTH
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise _fail(f"{label} must be a printable name of at most {limits.MAX_JOB_NAME_LENGTH} characters")
    return value


def actions_time(value: Any, label: str) -> datetime:
    """Parse an Actions job/step timestamp into an aware UTC datetime."""

    if not isinstance(value, str) or _STEP_TIME.fullmatch(value) is None:
        raise _fail(f"{label} must be an RFC 3339 timestamp")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    head, _, rest = text.partition(".")
    if rest:  # Python 3.11 wants 3 or 6 fractional digits; truncating to microseconds keeps the order.
        fraction, offset = rest[:-6], rest[-6:]
        text = f"{head}.{fraction[:6].ljust(6, '0')}{offset}"
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except ValueError as exc:
        raise _fail(f"{label} is not a real calendar time") from exc


def attempt_jobs(api: GitHubApi, run_id: int, run_attempt: int) -> list[dict[str, Any]]:
    """Every job of exactly one attempt (``/runs/{id}/attempts/{n}/jobs``, paginated, bounded by
    ``limits.MAX_JOBS_PER_ATTEMPT``); job ids must be unique and each job's ``run_attempt`` equal."""

    _positive(run_id, "run id")
    _positive(run_attempt, "run attempt", limits.MAX_RUN_ATTEMPT)
    jobs = api.paginate(f"/repos/{api.repository}/actions/runs/{run_id}/attempts/{run_attempt}/jobs",
                        field="jobs", max_items=limits.MAX_JOBS_PER_ATTEMPT)
    if not jobs:
        raise _fail(f"workflow run {run_id} has no jobs for attempt {run_attempt}")
    ids: set[int] = set()
    for job in jobs:
        job_id = _positive(job.get("id"), "job id")
        _name(job.get("name"), "job name")
        if job_id in ids:
            raise _fail("jobs listing repeats a job id")
        ids.add(job_id)
        if isinstance(job.get("run_attempt"), bool) or job.get("run_attempt") != run_attempt:
            raise _fail(f"job {job_id} does not belong to attempt {run_attempt}")
        if isinstance(job.get("run_id"), bool) or job.get("run_id") != run_id:
            raise _fail(f"job {job_id} does not belong to run {run_id}")
    return jobs


def _graph_entry(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"name", "conclusion"}:
        raise _fail(f"{label} must be exactly {{name, conclusion}}")
    if value["conclusion"] not in JOB_CONCLUSIONS:
        raise _fail(f"{label} has an unknown conclusion")
    return {"name": _name(value["name"], f"{label} name"), "conclusion": value["conclusion"]}


def _normalized(graph: Sequence[Mapping[str, Any]], label: str) -> list[dict[str, str]]:
    if not isinstance(graph, Sequence) or isinstance(graph, (str, bytes)):
        raise _fail(f"{label} must be a list")
    if len(graph) > limits.MAX_JOBS_PER_ATTEMPT:
        raise _fail(f"{label} exceeds {limits.MAX_JOBS_PER_ATTEMPT} jobs")
    entries = [_graph_entry(item, f"{label}[{index}]") for index, item in enumerate(graph)]
    names = [entry["name"] for entry in entries]
    if len(set(names)) != len(names):
        raise _fail(f"{label} repeats a job name")
    return sorted(entries, key=lambda entry: entry["name"])


def job_graph(jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The canonical ``[{name, conclusion}]`` of ``jobs`` sorted by name (names must be unique).

    Every job must be ``completed`` with a known conclusion: a graph describes a finished attempt."""

    if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
        raise _fail("jobs must be a list")
    graph = []
    for index, job in enumerate(jobs):
        if not isinstance(job, Mapping):
            raise _fail(f"job {index} is malformed")
        if job.get("status") != "completed":
            raise _fail(f"job {_name(job.get('name'), 'job name')!r} has not completed")
        graph.append({"name": job.get("name"), "conclusion": job.get("conclusion")})
    return _normalized(graph, "job graph")


def job_graph_sha256(graph: Sequence[Mapping[str, str]]) -> str:
    """SHA-256 of ``canonical_json(graph)``.

    The graph is validated and sorted by name first, so one set of jobs has exactly one hash."""

    return canonical_sha256(_normalized(graph, "job graph"))


def require_job_graph(jobs: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> str:
    """Require the observed graph to equal ``expected`` exactly (adapter ``expected_source_jobs``)
    and return its SHA-256; any missing, extra or differently concluded job raises."""

    wanted = {entry["name"]: entry["conclusion"] for entry in _normalized(expected, "expected job graph")}
    if not wanted:
        raise _fail("expected job graph is empty")
    observed_graph = job_graph(jobs)
    observed = {entry["name"]: entry["conclusion"] for entry in observed_graph}
    missing = sorted(set(wanted) - set(observed))
    unexpected = sorted(set(observed) - set(wanted))
    concluded = sorted(name for name in set(wanted) & set(observed) if wanted[name] != observed[name])
    if missing or unexpected or concluded:
        raise _fail(f"exact job graph mismatch: missing={missing[:10]}, unexpected={unexpected[:10]}, "
                    f"different conclusion={concluded[:10]}")
    return job_graph_sha256(observed_graph)


def _single_step(job: Mapping[str, Any], step_name: str) -> Mapping[str, Any]:
    if not isinstance(job, Mapping):
        raise _fail("job record is malformed")
    _name(step_name, "step name")
    steps = job.get("steps")
    if not isinstance(steps, list) or len(steps) > MAX_STEPS_PER_JOB:
        raise _fail(f"job {job.get('name')!r} has no bounded step list"[:200])
    matches = [step for step in steps if isinstance(step, Mapping) and step.get("name") == step_name]
    if any(not isinstance(step, Mapping) for step in steps):
        raise _fail("job step record is malformed")
    if len(matches) != 1:
        raise _fail(f"expected exactly one step named {step_name!r}, found {len(matches)}")
    return matches[0]


def step_window(job: Mapping[str, Any], step_name: str) -> tuple[datetime, datetime]:
    """``(started_at, completed_at)`` of the single step named exactly ``step_name`` in ``job``."""

    step = _single_step(job, step_name)
    started = actions_time(step.get("started_at"), f"step {step_name!r} started_at")
    completed = actions_time(step.get("completed_at"), f"step {step_name!r} completed_at")
    if completed < started:
        raise _fail(f"step {step_name!r} completed before it started")
    return started, completed


def require_successful_step(job: Mapping[str, Any], step_name: str) -> dict[str, Any]:
    """The single step named exactly ``step_name``, which must be ``completed``/``success``."""

    step = _single_step(job, step_name)
    if step.get("status") != "completed" or step.get("conclusion") != "success":
        raise _fail(f"step {step_name!r} did not complete successfully")
    return dict(step)
