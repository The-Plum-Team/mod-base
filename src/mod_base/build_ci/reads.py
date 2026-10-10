"""How one Build/E2E command reads GitHub (MB11): immutable objects once, mutable state twice.

Objects that can never change (a commit, tree or blob named by SHA, a comparison of two SHAs, the
job list of a completed run attempt) are read once per command: :class:`CommandReads`. Mutable
state (the default branch and its head, a pull request, a run's latest attempt, an artifact's
availability) is read at the start of a command and again immediately before the command
produces an effect: :class:`Watch`.

Every job list of one attempt holds only jobs that attempt ran. A rerun of failed jobs only does
not run the others again: GitHub lists them under the new attempt, with new job ids and the new
``run_attempt``, but with the times and runner of the attempt that ran them (K7 canary, run
38032224931 attempt 2). Such a job started before its attempt did and is refused as what a
failed-jobs-only rerun leaves behind (:data:`RERUN`). A rerun of all jobs runs every job again.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from mod_base.github.api import GitHubApi
from mod_base.github.jobs import attempt_jobs
from mod_base.github.runs import get_run, get_run_attempt
from mod_base.model import grammar
from mod_base.model.validators import check, fail

_T = TypeVar("_T")
#: GET paths whose answer is fixed by the object ids in them.
_IMMUTABLE = re.compile(
    r"^/repos/[^/]+/[^/]+/(?:git/(?:commits|trees|blobs)/[0-9a-f]{40}|compare/[0-9a-f]{40}\.\.\.[0-9a-f]{40})$"
)
#: The recovery every reader of one attempt names when it meets the work of another attempt.
RERUN = "a failed-jobs-only rerun mixes attempts; rerun all jobs"


def require_ran_in_attempt(jobs: list[dict[str, Any]], *, run_attempt: int, run_started_at: Any) -> None:
    """Refuse a job of ``jobs`` that started before its attempt did (``run_started_at`` of the
    attempt record): a job GitHub carried over from an earlier attempt into a rerun of failed jobs.

    Times are compared as whole seconds, so a job that started in the same second as its attempt
    (the first job of a first attempt may) is the attempt's own. A job without a start time and a
    skipped job, whose times GitHub fills in without running anything, are not judged by time."""

    started = grammar.normalize_timestamp(run_started_at, f"run_started_at of attempt {run_attempt}")
    for job in jobs:
        if job.get("conclusion") == "skipped" or job.get("started_at") is None:
            continue
        if grammar.normalize_timestamp(job.get("started_at"), "job started_at") < started:
            raise fail("$.jobs", f"job {str(job.get('name'))[:120]!r} started before attempt {run_attempt} "
                                 f"did, in an earlier attempt: {RERUN}")


class _OneAttempt:
    """The reads of one attempt's job listing as ``github.jobs.attempt_jobs`` makes them, refusing
    in its own words a job that the listing itself places in an earlier attempt."""

    def __init__(self, reads: CommandReads, run_attempt: int) -> None:
        self.repository, self._reads, self._attempt = reads.repository, reads, run_attempt

    def paginate(self, path: str, *, field: str | None, max_items: int) -> list[dict[str, Any]]:
        jobs = self._reads.paginate(path, field=field, max_items=max_items)
        for job in jobs:
            attempt = job.get("run_attempt") if type(job) is dict else None
            if type(attempt) is int and attempt < self._attempt:
                raise fail("$.jobs", f"job {str(job.get('name'))[:120]!r} ran in attempt {attempt}, not in "
                                     f"attempt {self._attempt}: {RERUN}")
        return jobs


class CommandReads:
    """One command's reads of GitHub through its one budgeted client.

    It offers the client's read surface, so it is passed wherever a client is read from. A commit,
    tree or blob named by SHA and a comparison of two SHAs are fetched once and answered from
    memory afterwards, and so is the job list of an attempt that :meth:`run` has seen completed.
    Every other read reaches the client every time."""

    def __init__(self, api: GitHubApi) -> None:
        self._api = api
        self._objects: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}
        self._completed: set[tuple[int, int]] = set()
        self._jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._started: dict[tuple[int, int], Any] = {}

    @classmethod
    def of(cls, api: GitHubApi | CommandReads) -> CommandReads:
        """``api`` itself when it already is a command's reads, otherwise new reads over it."""

        return api if isinstance(api, cls) else cls(api)

    @property
    def repository(self) -> str:
        return self._api.repository

    @property
    def request_count(self) -> int:
        return self._api.request_count

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        if _IMMUTABLE.fullmatch(path) is None:
            return self._api.get_json(path, params=params)
        key = (path, tuple(sorted((name, str(value)) for name, value in (params or {}).items())))
        if key not in self._objects:
            self._objects[key] = self._api.get_json(path, params=params)
        return copy.deepcopy(self._objects[key])

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        return self._api.paginate(path, field=field, params=params, max_items=max_items)

    def read_listing(self, read: Callable[[], _T]) -> _T:
        return self._api.read_listing(read)

    def download(self, path: str, *, max_bytes: int) -> bytes:
        return self._api.download(path, max_bytes=max_bytes)

    def run(self, run_id: int) -> dict[str, Any]:
        """The run as its latest attempt, read every time; a completed attempt is remembered, and
        so is when that attempt started."""

        run = get_run(self, run_id)
        attempt = run.get("run_attempt")
        if type(attempt) is int:
            self._started[(run_id, attempt)] = run.get("run_started_at")
            if run.get("status") == "completed":
                self._completed.add((run_id, attempt))
        return run

    def attempt_started(self, run_id: int, run_attempt: int) -> Any:
        """``run_started_at`` of one attempt, as it arrived: from the record :meth:`run` read when
        that was this attempt, as every command that authenticates the run has, and otherwise from
        one read of the attempt's own record (``/runs/{id}/attempts/{n}``)."""

        key = (run_id, run_attempt)
        if key not in self._started:
            self._started[key] = get_run_attempt(self, run_id, run_attempt).get("run_started_at")
        return self._started[key]

    def attempt_jobs(self, run_id: int, run_attempt: int) -> list[dict[str, Any]]:
        """Every job of one attempt, each one run by that attempt: a job the listing places in an
        earlier attempt, or one that started before the attempt did, is refused (:data:`RERUN`).
        The list of an attempt :meth:`run` saw completed is read once; the jobs of a running
        attempt are read every time."""

        key = (run_id, run_attempt)
        if key not in self._jobs:
            jobs = attempt_jobs(_OneAttempt(self, run_attempt), run_id, run_attempt)
            require_ran_in_attempt(jobs, run_attempt=run_attempt,
                                   run_started_at=self.attempt_started(run_id, run_attempt))
            if key not in self._completed:
                return jobs
            self._jobs[key] = jobs
        return copy.deepcopy(self._jobs[key])


class Watch:
    """The mutable GitHub state one effect depends on.

    :meth:`read` performs a read the first time its key is asked for and answers from that
    observation afterwards, so one phase of a command reads each thing once. :meth:`recheck`,
    called immediately before the effect, performs every read again and requires the same answers."""

    def __init__(self) -> None:
        self._reads: dict[tuple[Any, ...], tuple[Callable[[], Any], Any]] = {}

    def read(self, key: tuple[Any, ...], reader: Callable[[], _T]) -> _T:
        """``reader()`` as first observed under ``key``; ``key[0]`` names the state in errors."""

        if key not in self._reads:
            self._reads[key] = (reader, reader())
        return copy.deepcopy(self._reads[key][1])

    def recheck(self) -> None:
        for key, (reader, value) in list(self._reads.items()):
            check(reader() == value, "$.admission",
                  f"{key[0]} changed between the start of the command and its effect")
