"""The simulated GitHub of one conformance run (MB10): a seeded ``FakeGitHub`` and its timeline.

Everything the kit reads from the API is seeded here with the exact names of
:mod:`mod_base.workflow` and :mod:`mod_base.model.grammar`: the default branch and its live head
(moved by :meth:`World.advance`, a push), the workflow records of ``source.workflow``, every family
producer and ``pages.yml`` (their ids bind every run), the pin line of each producer workflow at
every head (the kit binding of SPEC §1.8), source and producer runs with their attempt jobs and
upload steps, Pages runs with ``referenced_workflows`` naming the simulated kit, and every artifact
as the ZIP bytes the kit downloads by id. Times are offsets in seconds from :data:`BASE`; every
upload lies inside the step window that produced it. The conformance extension fixtures add their
own runs, jobs and artifacts through :mod:`mod_base.conformance._fixture_api` (:meth:`World.fixture_run`,
:meth:`World.add_fixture_jobs`, :meth:`World.fixture_artifact`).

The fake accepts ``DELETE`` (a rotation that is not a dry run retires artifacts for real); the
simulation requires every other job to leave :attr:`FakeGitHub.mutations` unchanged.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import mod_base
from mod_base.config import Config
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.workflow import CALLEE_WORKFLOWS, PAGES_WORKFLOW_PATH

#: The simulated kit commit every producer pins and every Pages run resolves.
KIT_SHA = hashlib.sha1(b"mod-base conformance kit").hexdigest()  # noqa: S324 - a synthetic commit id
BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
PAGES_WORKFLOW_ID = 100
SOURCE_WORKFLOW_ID = 101
#: The read-only token the simulated Pages jobs hold (never a real credential).
TOKEN = "conformance-read-only-token"
MAX_ZIP_FILES = 20000
#: Run and workflow ids of the fixtures' own runs, far from every run id the simulation uses.
FIXTURE_RUN_IDS = range(700_001, 800_000)
FIXTURE_WORKFLOW_IDS = range(300, 400)


def at(offset: float) -> str:
    return (BASE + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def timestamp(offset: float) -> float:
    return (BASE + timedelta(seconds=offset)).timestamp()


def pin_workflow(workflow: str) -> bytes:
    """The producer workflow served at the subject commit: exactly one mod-base pin line."""

    return (f"# mod-base conformance: the synthetic {workflow} at the subject commit\n"
            f"name: Conformance producer\non:\n  workflow_dispatch:\npermissions: {{}}\njobs:\n  produce:\n"
            f"    runs-on: ubuntu-24.04\n    steps:\n"
            f"      - uses: The-Plum-Team/mod-base/actions/prepare-evidence@{KIT_SHA} # v{mod_base.__version__}\n"
            ).encode("utf-8")


def pinned_workflows(config: Config) -> dict[str, bytes]:
    """``source.workflow`` and every family ``producer.workflow`` as the simulated repository holds
    them at every head (:func:`pin_workflow`): committed into the snapshot and served by the fake."""

    paths = {config.source["workflow"], *(family["producer"]["workflow"] for family in config.families)}
    return {path: pin_workflow(path) for path in sorted(paths)}


def zip_directory(root: Path) -> bytes:
    """A deterministic deflate ZIP of every regular file below ``root`` (an uploaded artifact)."""

    stream = io.BytesIO()
    files = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories.sort()
        for name in names:
            path = Path(directory, name)
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f"{path} is not a regular file")
            files.append(path)
    if len(files) > MAX_ZIP_FILES:
        raise ValueError(f"{root} holds more than {MAX_ZIP_FILES} files")
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(2026, 9, 1, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return stream.getvalue()


def zip_files(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 1, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name])
    return stream.getvalue()


class World:
    """One simulated repository (see the module docstring)."""

    def __init__(self, *, repository: str, config: Config, head: str, tree: str) -> None:
        self.repository = repository
        self.config = config
        self.head = head
        self.tree = tree
        self.branch = config.canonical_branch
        self.api = FakeGitHub(repository=repository, default_branch=self.branch, writable=True)
        self._artifact_ids = iter(range(5001, 1_000_000))
        #: Every seeded artifact's API record and ZIP bytes, by id (a deleted one included).
        self.records: dict[int, dict[str, Any]] = {}
        self.archives: dict[int, bytes] = {}
        #: Every seeded run's latest record, by id.
        self.runs: dict[int, dict[str, Any]] = {}
        self._job_ids = iter(range(70001, 1_000_000))
        #: Per attempt, the jobs the simulation seeded and the jobs a fixture added after them.
        self._seeded_jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._fixture_jobs: dict[tuple[int, int], list[dict[str, Any]]] = {}
        self._fixture_run_ids = iter(FIXTURE_RUN_IDS)
        self._fixture_workflow_ids = iter(FIXTURE_WORKFLOW_IDS)
        self.fixture_workflows: dict[str, int] = {}
        self.workflow_ids = {PAGES_WORKFLOW_PATH: PAGES_WORKFLOW_ID, config.source["workflow"]: SOURCE_WORKFLOW_ID}
        for position, family in enumerate(config.families):
            self.workflow_ids.setdefault(family["producer"]["workflow"], 200 + position)
        self.api.set_branch(self.branch, head, tree)
        for path, identity in self.workflow_ids.items():
            self.api.add_response(f"/repos/{repository}/actions/workflows/{path.rsplit('/', 1)[1]}",
                                  {"id": identity, "path": path, "state": "active"})
        self._pin(head)

    def _pin(self, commit: str) -> None:
        for path, data in pinned_workflows(self.config).items():
            self.api.add_file(commit, path, data)

    def advance(self, head: str, tree: str) -> None:
        """A push: the default branch moves to ``head`` (whose producer workflows pin the kit)."""

        self.head, self.tree = head, tree
        self.api.set_branch(self.branch, head, tree)
        self._pin(head)

    def alive(self) -> dict[int, dict[str, Any]]:
        """Every seeded artifact the fake still holds (never deleted), by id."""

        deleted = set(self.api.deleted_artifact_ids)
        return {artifact_id: record for artifact_id, record in self.records.items() if artifact_id not in deleted}

    # -- runs, jobs and artifacts --------------------------------------------------------------------

    def run(self, run_id: int, *, path: str, event: str, created: float, updated: float | None = None,
            status: str = "completed", conclusion: str | None = "success", head_branch: str | None = None,
            head_sha: str | None = None, title: str = "Conformance run", pages: bool = False) -> dict[str, Any]:
        """Seed a run of this repository (its latest and only attempt 1) and return its record."""

        record: dict[str, Any] = {
            "id": run_id, "run_attempt": 1, "path": path, "workflow_id": self.workflow_ids[path],
            "event": event, "status": status, "conclusion": conclusion,
            "head_branch": head_branch or self.branch, "head_sha": head_sha or self.head,
            "head_repository": {"full_name": self.repository}, "display_title": title,
            "created_at": at(created), "updated_at": at(created + 60 if updated is None else updated),
        }
        if pages:
            record["referenced_workflows"] = [
                {"path": f"{mod_base.KIT_REPOSITORY}/{workflow}@{KIT_SHA}", "ref": f"refs/tags/v{mod_base.__version__}",
                 "sha": KIT_SHA} for workflow in CALLEE_WORKFLOWS.values()]
        self.api.add_run(record)
        self.runs[run_id] = record
        return record

    def job(self, name: str, *, started: float, completed: float | None, steps: tuple[tuple[str, float, float], ...] = (),
            conclusion: str | None = "success") -> dict[str, Any]:
        running = completed is None
        return {"id": next(self._job_ids), "name": name, "status": "in_progress" if running else "completed",
                "conclusion": None if running else conclusion, "started_at": at(started),
                "completed_at": None if running else at(completed),
                "steps": [{"name": step, "number": position + 1, "status": "completed", "conclusion": "success",
                           "started_at": at(first), "completed_at": at(last)}
                          for position, (step, first, last) in enumerate(steps)]}

    def jobs(self, run: dict[str, Any], jobs: list[dict[str, Any]]) -> None:
        """Seed the jobs of ``run``'s attempt; a fixture's jobs of that attempt stay after them."""

        attempt = (run["id"], run["run_attempt"])
        self._seeded_jobs[attempt] = list(jobs)
        self._seed_jobs(attempt)

    def _seed_jobs(self, attempt: tuple[int, int]) -> None:
        """Serve the attempt's jobs as GitHub's jobs API does: each also names its run's ``head_sha``
        and ``head_branch`` (unless a fixture's job gives its own)."""

        seeded, added = self._seeded_jobs.get(attempt, []), self._fixture_jobs.get(attempt, [])
        names = [job["name"] for job in seeded + added]
        if len(names) != len(set(names)):
            raise ValueError(f"a fixture job repeats a job name of run {attempt[0]} attempt {attempt[1]}")
        run = self.runs[attempt[0]]
        self.api.add_jobs(attempt[0], attempt[1], [{"head_sha": run["head_sha"], "head_branch": run["head_branch"], **job}
                                                   for job in seeded + added])

    # -- the fixtures' own evidence ------------------------------------------------------------------

    def fixture_run(self, record: dict[str, Any]) -> dict[str, Any]:
        """Seed a fixture's run (attempt 1) of a workflow the simulation does not own; return it."""

        path = record["path"]
        if path not in self.fixture_workflows:
            self.fixture_workflows[path] = next(self._fixture_workflow_ids)
            self.api.add_response(f"/repos/{self.repository}/actions/workflows/{path.rsplit('/', 1)[1]}",
                                  {"id": self.fixture_workflows[path], "path": path, "state": "active"})
        seeded = {**record, "id": next(self._fixture_run_ids), "run_attempt": 1,
                  "workflow_id": self.fixture_workflows[path], "head_repository": {"full_name": self.repository}}
        seeded.setdefault("updated_at", seeded["created_at"])
        self.api.add_run(seeded)
        self.runs[seeded["id"]] = seeded
        return dict(seeded)

    def add_fixture_jobs(self, run: dict[str, Any], run_attempt: int, jobs: list[dict[str, Any]]) -> None:
        """Append a fixture's ``jobs`` (ids assigned) to attempt ``run_attempt`` of ``run``."""

        attempt = (run["id"], run_attempt)
        added = [{**job, "id": next(self._job_ids)} for job in jobs]
        self._fixture_jobs[attempt] = self._fixture_jobs.get(attempt, []) + added
        self._seed_jobs(attempt)

    def fixture_artifact(self, name: str, run: dict[str, Any], *, created_at: str, archive: bytes) -> dict[str, Any]:
        """Seed a fixture's (non-kit) artifact of ``run``; return its API record."""

        record = {"id": next(self._artifact_ids), "name": name, "created_at": created_at, "expired": False,
                  "size_in_bytes": len(archive), "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                  "workflow_run": {"id": run["id"], "head_branch": run["head_branch"], "head_sha": run["head_sha"]}}
        self.api.add_artifact(record, archive)
        self.records[record["id"]] = record
        self.archives[record["id"]] = archive
        return record

    def artifact(self, name: str, run: dict[str, Any], *, created: float, archive: bytes,
                 artifact_id: int | None = None) -> dict[str, Any]:
        """Seed one artifact of ``run``; return its API record (with ``digest`` and ``size_in_bytes``)."""

        if grammar.parse_artifact_name(name) is None and not name.startswith("conformance-"):
            raise ValueError(f"not a kit artifact name: {name}")
        record = {"id": artifact_id or next(self._artifact_ids), "name": name, "created_at": at(created),
                  "expired": False, "size_in_bytes": len(archive),
                  "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                  "workflow_run": {"id": run["id"], "head_branch": run["head_branch"], "head_sha": run["head_sha"]}}
        self.api.add_artifact(record, archive)
        self.records[record["id"]] = record
        self.archives[record["id"]] = archive
        return record
