"""The ``ctx.api`` of the conformance extension fixtures (MB10): the simulated GitHub, read and seeded.

``delegated_extensions`` and ``selected_extensions`` (``docs/ADAPTER.md``, "Optional conformance
fixtures") return extension objects that a mod's network hooks (``authenticate_extensions``,
``compose``) authenticate against GitHub. A real adapter reads more than JSON bodies: Quick Skin's
``runtime_source`` downloads the tested run's ``tested-source`` seal and the handoff run's
``reused-source`` descriptor, its feature selection a coverage certificate, each a ZIP artifact
bound to its owner run and jobs. So these two fixture functions receive a :class:`FixtureGitHub`
as ``ctx.api``: the read surface of the simulated :class:`mod_base.github.fake.FakeGitHub`
(``repository``, ``get_json``, ``paginate``, ``read_listing``, ``download``; read-only, at most
:data:`MAX_FIXTURE_READS` requests) plus typed, bounded seeders for exactly the evidence the kit's
own world does not hold:

* :meth:`FixtureGitHub.add_response`: the exact JSON body of one GET of this repository that
  nothing answers yet (a pull request, a synthetic commit), never a kit-owned route
  (``/actions/``, ``/branches``, ``/contents/``, the repository itself) and never shadowing a path
  the simulated GitHub already answers;
* :meth:`FixtureGitHub.add_run`: a completed run of another workflow of this repository (never
  ``pages.yml``, ``source.workflow`` or a family producer, whose runs the simulation owns), its
  ``id``, attempt 1, ``workflow_id`` and ``head_repository`` assigned; the record is returned;
* :meth:`FixtureGitHub.add_jobs`: jobs added to an attempt of any seeded run, the handoff run
  included; they are appended to the jobs the simulation seeds for it (before or after this call)
  and never share a name with one of them;
* :meth:`FixtureGitHub.add_artifact`: an artifact of any seeded run with its ZIP bytes (a readable
  ZIP of at most :data:`MAX_FIXTURE_ARCHIVE_BYTES`), never a kit (``mb-``), ``github-pages`` or
  simulation (``conformance-``) name; its API record, id and digest included, is returned, so the
  extension objects can name it.

:attr:`FixtureGitHub.handoff_run` names the run whose handoff the extensions will be proven for
(``{id, run_attempt, path, event, head_branch, head_sha}``); it is already seeded when the fixture
runs, and its jobs are seeded later. One facade serves one fixture call: the bounds below are per
call. A seeding mistake raises ``ValueError``, which fails the run.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from mod_base.conformance._world import World
from mod_base.github.api import ApiError, ReadOnlyViolation
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.workflow import PAGES_WORKFLOW_PATH

#: Bounds of one fixture call.
MAX_FIXTURE_READS = lim.MAX_PAGES_API_READS
MAX_FIXTURE_RESPONSES = 64
MAX_FIXTURE_RUNS = 16
MAX_FIXTURE_JOBS = 128
MAX_FIXTURE_ARTIFACTS = 32
MAX_FIXTURE_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_FIXTURE_ARCHIVES_BYTES = 64 * 1024 * 1024
MAX_FIXTURE_ARCHIVE_ENTRIES = 1024
MAX_FIXTURE_RECORD_BYTES = 64 * 1024
#: Artifact names a fixture may upload (a conservative subset of GitHub's).
ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_RESERVED_PREFIXES = (grammar.ARTIFACT_PREFIX, "conformance-")
#: Run fields :meth:`FixtureGitHub.add_run` assigns.
_ASSIGNED_RUN_FIELDS = frozenset({"id", "run_attempt", "workflow_id", "head_repository", "repository"})
_ASSIGNED_JOB_FIELDS = frozenset({"id", "run_id", "run_attempt"})
_KIT_ROUTES = ("actions", "branches", "contents")
_T = TypeVar("_T")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _json_record(value: Any, label: str) -> dict[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be a JSON object")
    record = dict(value)
    try:
        encoded = canonical_json(record)
    except Exception as exc:  # noqa: BLE001 - any encoding failure is a seeding mistake
        raise ValueError(f"{label} is not JSON: {exc}") from None
    _require(len(encoded) <= MAX_FIXTURE_RECORD_BYTES, f"{label} exceeds {MAX_FIXTURE_RECORD_BYTES} bytes")
    return record


def _check_archive(archive: Any) -> bytes:
    _require(isinstance(archive, (bytes, bytearray)), "an artifact archive must be bytes")
    data = bytes(archive)
    _require(0 < len(data) <= MAX_FIXTURE_ARCHIVE_BYTES,
             f"an artifact archive must hold 1..{MAX_FIXTURE_ARCHIVE_BYTES} bytes")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            entries = bundle.infolist()
            _require(len(entries) <= MAX_FIXTURE_ARCHIVE_ENTRIES
                     and sum(entry.file_size for entry in entries) <= MAX_FIXTURE_ARCHIVES_BYTES,
                     "an artifact archive expands beyond its bounds")
            _require(bundle.testzip() is None, "an artifact archive holds a corrupt entry")
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise ValueError(f"an artifact archive is not a readable ZIP: {exc}") from None
    return data


class FixtureGitHub:
    """The fixture-call view of the simulated GitHub (see the module docstring)."""

    def __init__(self, world: World, *, handoff_run: Mapping[str, Any], protected_workflows: frozenset[str]) -> None:
        self._world = world
        self._api = world.api
        self._protected = frozenset(protected_workflows) | {PAGES_WORKFLOW_PATH}
        self._handoff = {field: handoff_run[field]
                         for field in ("id", "run_attempt", "path", "event", "head_branch", "head_sha")}
        self._reads_spent = 0
        self._responses = 0
        self._runs = 0
        self._jobs = 0
        self._artifacts = 0
        self._archive_bytes = 0

    # -- facts and the read surface --------------------------------------------------------------

    @property
    def repository(self) -> str:
        return self._api.repository

    @property
    def writable(self) -> bool:
        return False

    @property
    def handoff_run(self) -> dict[str, Any]:
        """The run whose handoff the returned extensions are proven for (already seeded)."""

        return dict(self._handoff)

    def _read(self, read: Callable[[], _T]) -> _T:
        _require(self._reads_spent < MAX_FIXTURE_READS, f"a fixture may make at most {MAX_FIXTURE_READS} reads")
        before = self._api.request_count
        try:
            return read()
        finally:
            self._reads_spent += self._api.request_count - before

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        return self._read(lambda: self._api.get_json(path, params=params))

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        return self._read(lambda: self._api.paginate(path, field=field, params=params, max_items=max_items))

    def read_listing(self, read: Callable[[], _T]) -> _T:
        return self._read(lambda: self._api.read_listing(read))

    def download(self, path: str, *, max_bytes: int) -> bytes:
        return self._read(lambda: self._api.download(path, max_bytes=max_bytes))

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        raise ReadOnlyViolation(f"refusing POST {path!r}: a fixture seeds through add_* only"[:200])

    def delete(self, path: str) -> None:
        raise ReadOnlyViolation(f"refusing DELETE {path!r}: a fixture seeds through add_* only"[:200])

    # -- seeders ------------------------------------------------------------------------------------

    def add_response(self, path: str, payload: Any, *, params: Mapping[str, str | int] | None = None) -> None:
        """Seed the exact JSON body of one GET ``path`` of this repository (see the module docstring)."""

        _require(self._responses < MAX_FIXTURE_RESPONSES, f"a fixture may seed at most {MAX_FIXTURE_RESPONSES} responses")
        prefix = f"/repos/{self.repository}/"
        _require(isinstance(path, str) and path.startswith(prefix), "a response path must be one of this repository's")
        _require(path[len(prefix):].split("/", 1)[0] not in _KIT_ROUTES,
                 f"{path} is a kit-owned route: seed runs, jobs and artifacts with add_run/add_jobs/add_artifact")
        try:
            self._api.get_json(path, params=params)
        except ApiError:
            pass
        else:
            raise ValueError(f"{path} is already answered by the simulated GitHub; a fixture never shadows it")
        self._api.add_response(path, payload, params=params)
        self._responses += 1

    def add_run(self, run: Mapping[str, Any]) -> dict[str, Any]:
        """Seed a run of another workflow of this repository and return its API record."""

        _require(self._runs < MAX_FIXTURE_RUNS, f"a fixture may seed at most {MAX_FIXTURE_RUNS} runs")
        record = _json_record(run, "a seeded run")
        _require(not _ASSIGNED_RUN_FIELDS & set(record), f"a seeded run's {sorted(_ASSIGNED_RUN_FIELDS)} are assigned")
        path = record.get("path")
        _require(grammar.is_match(grammar.WORKFLOW_PATH, path), "a seeded run needs a .github/workflows path")
        _require(path not in self._protected,
                 f"{path} is a workflow whose runs the simulation owns (pages.yml, source, family producers)")
        _require(grammar.is_match(grammar.EVENT, record.get("event")), "a seeded run needs an event")
        _require(grammar.is_match(grammar.BRANCH, record.get("head_branch")), "a seeded run needs a head_branch")
        _require(grammar.is_match(grammar.SHA1, record.get("head_sha")), "a seeded run needs a head_sha commit")
        record.setdefault("status", "completed")
        record.setdefault("conclusion", "success" if record["status"] == "completed" else None)
        _require(record["status"] in ("queued", "in_progress", "completed"), "a seeded run has an unknown status")
        record.setdefault("created_at", self._world.runs[self._handoff["id"]]["created_at"])
        grammar.parse_timestamp(record["created_at"], "a seeded run's created_at")
        seeded = self._world.fixture_run(record)
        self._runs += 1
        return seeded

    def add_jobs(self, run_id: int, run_attempt: int, jobs: list[Mapping[str, Any]]) -> None:
        """Append ``jobs`` to attempt ``run_attempt`` of the seeded run ``run_id``."""

        run = self._world.runs.get(run_id) if isinstance(run_id, int) and not isinstance(run_id, bool) else None
        _require(run is not None, f"run {run_id!r} is not seeded")
        _require(isinstance(run_attempt, int) and not isinstance(run_attempt, bool)
                 and 1 <= run_attempt <= run["run_attempt"], f"run {run_id} has no attempt {run_attempt!r}")
        _require(isinstance(jobs, list) and 0 < len(jobs) <= MAX_FIXTURE_JOBS - self._jobs,
                 f"a fixture may add 1..{MAX_FIXTURE_JOBS} jobs in all")
        records = []
        for position, job in enumerate(jobs):
            record = _json_record(job, f"job {position}")
            _require(not _ASSIGNED_JOB_FIELDS & set(record), f"a job's {sorted(_ASSIGNED_JOB_FIELDS)} are assigned")
            name = record.get("name")
            _require(isinstance(name, str) and 0 < len(name) <= 200 and name.isprintable(),
                     "a job needs a printable name of at most 200 characters")
            record.setdefault("status", "completed")
            record.setdefault("conclusion", "success" if record["status"] == "completed" else None)
            records.append(record)
        self._world.add_fixture_jobs(run, run_attempt, records)
        self._jobs += len(records)

    def add_artifact(self, run_id: int, name: str, archive: bytes, *, created_at: str | None = None) -> dict[str, Any]:
        """Seed an artifact ``name`` of the seeded run ``run_id`` with its ZIP bytes; return its API
        record (``id``, ``name``, ``digest``, ``size_in_bytes``, ``created_at``, ``expired``,
        ``workflow_run``)."""

        _require(self._artifacts < MAX_FIXTURE_ARTIFACTS, f"a fixture may seed at most {MAX_FIXTURE_ARTIFACTS} artifacts")
        run = self._world.runs.get(run_id) if isinstance(run_id, int) and not isinstance(run_id, bool) else None
        _require(run is not None, f"run {run_id!r} is not seeded")
        _require(isinstance(name, str) and ARTIFACT_NAME.fullmatch(name) is not None,
                 "an artifact name must match [A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
        _require(not name.lower().startswith(_RESERVED_PREFIXES) and name.lower() != grammar.PAGES_ARTIFACT_NAME
                 and grammar.parse_artifact_name(name) is None,
                 f"{name} is a kit or simulation artifact name")
        data = _check_archive(archive)
        _require(self._archive_bytes + len(data) <= MAX_FIXTURE_ARCHIVES_BYTES,
                 f"a fixture may seed at most {MAX_FIXTURE_ARCHIVES_BYTES} archive bytes")
        moment = created_at if created_at is not None else run.get("updated_at", run["created_at"])
        grammar.parse_timestamp(moment, "an artifact's created_at")
        record = self._world.fixture_artifact(name, run, created_at=moment, archive=data)
        self._artifacts += 1
        self._archive_bytes += len(data)
        return dict(record)

