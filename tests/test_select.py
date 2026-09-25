"""``select`` (MB5): newest authenticated evidence, nominations without fallback and exit 3.

Ports QS ``test_pages_selection_api_budget`` (complete exact inventories, one owner per
candidate, API failures never become absence) and BP ``test_pages_newest_source`` plus the
``select`` cases of ``test_pages_publication`` (exact display title on the default controller,
newest run never falls back, cache bound to the newest run) onto v1 names with ``FakeGitHub``.

:class:`World` is the shared seeding helper of the MB5 test modules.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.adapter import host
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.pages import commands_control, targets
from mod_base.pages.select import SELECTED_KEYS, Selected, select_evidence
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH
from tests.fixtures.mods import support

SOURCE = support.SOURCE_WORKFLOW
FAMILY = "mod-compatibility"
FAMILY_WORKFLOW = ".github/workflows/mod-compatibility-review.yml"
QS_REPOSITORY = support.REPOSITORIES["qs_like"]
BP_REPOSITORY = support.REPOSITORIES["bp_like"]
QS_KEY = "mc1.20.1"
BP_KEY = "0" * 24
RELEASE_KEY = "1" * 24
RELEASE = "release/1.21.1"
BASE = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
HEAD = "a" * 40
TREE = "b" * 40
CONTROLLER = "c" * 40
QS_HANDOFF_JOB = "Prepare public evidence for {key} (advisory)"
QS_HANDOFF_STEP = "Upload stable public evidence for this Minecraft target"
BP_HANDOFF_JOB = "Curate current-head public evidence (advisory)"
BP_HANDOFF_STEP = "Prepare and hand off current-head public evidence"
FAMILY_JOB = "Publish compact compatibility evidence"
FAMILY_STEP = "Upload the mod-base family handoff"
#: The workflow ids every seeded run of these paths carries (``GET /actions/workflows/<file>``).
WORKFLOW_IDS = {SOURCE: 77, PAGES_WORKFLOW_PATH: 88, FAMILY_WORKFLOW: 99}


def at(offset: float) -> str:
    """``BASE + offset`` seconds as a GitHub timestamp."""

    return (BASE + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(offset: float) -> float:
    return (BASE + timedelta(seconds=offset)).timestamp()


class RecordingFake(FakeGitHub):
    """A ``FakeGitHub`` that records every GET ``(path, params)`` (paginated pages included)."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.failures: dict[str, BaseException] = {}

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls.append((path, dict(params or {})))
        for fragment, error in self.failures.items():
            if path.endswith(fragment):
                raise error
        return super().get_json(path, params=params)

    def requests(self, fragment: str) -> list[tuple[str, dict[str, Any]]]:
        return [call for call in self.calls if fragment in call[0]]


class World:
    """Seeds one repository's runs, attempt jobs and artifacts into a :class:`RecordingFake`."""

    def __init__(self, repository: str, *, default_branch: str = "master", max_requests: int | None = None) -> None:
        self.repository = repository
        self.api = RecordingFake(repository=repository, default_branch=default_branch, max_requests=max_requests)
        self._artifact_ids = itertools.count(5001)
        self._job_ids = itertools.count(70001)
        for path, identity in WORKFLOW_IDS.items():
            self.api.add_response(f"/repos/{repository}/actions/workflows/{path.rsplit('/', 1)[1]}",
                                  {"id": identity, "path": path})

    def run(self, run_id: int, *, path: str = SOURCE, head_sha: str = HEAD, head_branch: str = "master",
            event: str = "workflow_dispatch", status: str = "completed", conclusion: str | None = "success",
            attempt: int = 1, created: float = 0, updated: float | None = None, title: str | None = None,
            repository: str | None = None, workflow_id: int | None = None, kit_sha: str | None = None,
            earlier: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
        identity = WORKFLOW_IDS.get(path, 70) if workflow_id is None else workflow_id
        record = {"id": run_id, "run_attempt": attempt, "path": path, "head_sha": head_sha, "head_branch": head_branch,
                  "event": event, "status": status, "conclusion": conclusion, "created_at": at(created),
                  "updated_at": at(created + 60 if updated is None else updated), "workflow_id": identity,
                  "head_repository": {"full_name": repository or self.repository},
                  "display_title": title if title is not None else "Run"}
        if kit_sha is not None:
            path = f"The-Plum-Team/mod-base/.github/workflows/publish.yml@{kit_sha}"
            record["referenced_workflows"] = [{"path": path, "ref": "refs/tags/v0.9.0", "sha": kit_sha}]
        self.api.add_run(record, attempts=[{**record, **changes} for changes in earlier])
        return record

    def job(self, name: str, *, steps: tuple[tuple[str, float, float], ...] = (), conclusion: str = "success",
            started: float = 0, completed: float = 600, status: str = "completed") -> dict[str, Any]:
        return {"id": next(self._job_ids), "name": name, "status": status, "conclusion": conclusion,
                "started_at": at(started), "completed_at": at(completed),
                "steps": [{"name": step, "number": index + 1, "status": "completed", "conclusion": "success",
                           "started_at": at(first), "completed_at": at(last)}
                          for index, (step, first, last) in enumerate(steps)]}

    def jobs(self, run: dict[str, Any], jobs: list[dict[str, Any]], *, attempt: int | None = None) -> None:
        self.api.add_jobs(run["id"], attempt or run["run_attempt"], jobs)

    def artifact(self, name: str, run: dict[str, Any], *, created: float, archive: bytes = b"PK\x05\x06" + bytes(18),
                 expired: bool = False, artifact_id: int | None = None, **changes: Any) -> dict[str, Any]:
        record = {"id": artifact_id or next(self._artifact_ids), "name": name, "created_at": at(created),
                  "expired": expired, "workflow_run": {"id": run["id"], "head_sha": run["head_sha"],
                                                       "head_branch": run["head_branch"]}, **changes}
        self.api.add_artifact(record, archive)
        return {"digest": "sha256:" + hashlib.sha256(archive).hexdigest(), "size_in_bytes": len(archive), **record}

    def handoff(self, run: dict[str, Any], key: str, *, created: float = 300, job: str = QS_HANDOFF_JOB,
                step: str = QS_HANDOFF_STEP, extra_jobs: tuple[dict[str, Any], ...] = (),
                **changes: Any) -> dict[str, Any]:
        """A handoff of ``run``'s latest attempt uploaded inside its handoff job's upload step."""

        self.jobs(run, [self.job(job.replace("{key}", key), steps=((step, created - 10, created + 10),)),
                        *extra_jobs])
        return self.artifact(grammar.handoff_name(key, run["run_attempt"]), run, created=created, **changes)

    def pages_cache(self, run_id: int, key: str, subject: str, *, created: float = 900, head_sha: str = HEAD,
                    **run_changes: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        owner = self.run(run_id, **{"path": PAGES_WORKFLOW_PATH, "head_sha": head_sha, "created": created - 300,
                                    **run_changes})
        return owner, self.artifact(grammar.cache_name(key, subject), owner, created=created)


def qs_invocation(**environ: str):
    env = support.environment(support.FixtureMod("qs_like", support.MODS / "qs_like", HEAD, TREE, "master",
                                                 QS_REPOSITORY), run_id=9000, sha=HEAD, job="collect",
                              workflow=PAGES_WORKFLOW_PATH)
    env.update(environ)
    return build_invocation(support.MODS / "qs_like", None, env)


def bp_invocation(**environ: str):
    env = support.environment(support.FixtureMod("bp_like", support.MODS / "bp_like", HEAD, TREE, "master",
                                                 BP_REPOSITORY), run_id=9000, sha=HEAD, job="collect",
                              workflow=PAGES_WORKFLOW_PATH)
    env.update(environ)
    return build_invocation(support.MODS / "bp_like", None, env)


def bp_title(commit: str) -> str:
    return f"Packaged E2E / {commit}"


class SelectedTest(unittest.TestCase):
    def valid(self, **changes: Any) -> dict[str, Any]:
        return {"kind": "handoff", "artifact_id": 5, "name": grammar.handoff_name(QS_KEY, 2),
                "digest": "sha256:" + "d" * 64, "size": 10, "run_id": 77, "run_attempt": 2, **changes}

    def test_round_trip_is_exactly_the_selected_keys(self) -> None:
        selected = Selected.parse(self.valid())
        self.assertEqual(tuple(selected.to_json()), SELECTED_KEYS)
        self.assertEqual(Selected.parse(selected.to_json()), selected)
        cache = Selected.parse(self.valid(kind="cache", name=grammar.cache_name(QS_KEY, HEAD), run_attempt=4))
        self.assertEqual(cache.run_attempt, 4)
        family = Selected.parse(self.valid(kind="family-handoff", name=grammar.family_handoff_name(FAMILY, QS_KEY, 2)))
        self.assertEqual(family.kind, "family-handoff")

    def test_hostile_objects_fail_closed(self) -> None:
        cases = [
            self.valid(extra=1), {key: value for key, value in self.valid().items() if key != "digest"},
            self.valid(kind="anchor"), self.valid(artifact_id=True), self.valid(size=0),
            self.valid(size=(1 << 30) + 1), self.valid(run_attempt=1001), self.valid(run_id="77"),
            self.valid(digest="sha256:" + "D" * 64), self.valid(name="pages-e2e-mc1.20.1"),
            self.valid(name=grammar.cache_name(QS_KEY, HEAD)), self.valid(run_attempt=3),
            self.valid(kind="cache", name=grammar.handoff_name(QS_KEY, 2)), [], None,
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(MbError):
                Selected.parse(case)


class QuickSkinSelectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(QS_REPOSITORY)
        self.invocation = qs_invocation()

    def select(self, **options: Any) -> Selected:
        arguments = {"key": QS_KEY, "expected_subject_commit": HEAD, **options}
        return select_evidence(self.invocation, api=self.world.api, **arguments)

    def test_newest_successful_run_handoff_wins_with_bounded_reads(self) -> None:
        older = self.world.run(20, created=0)
        self.world.handoff(older, QS_KEY)
        newer = self.world.run(30, created=100)
        artifact = self.world.handoff(newer, QS_KEY, created=400)
        self.world.pages_cache(40, QS_KEY, HEAD, created=900)
        selected = self.select()
        self.assertEqual((selected.kind, selected.artifact_id, selected.run_id, selected.run_attempt),
                         ("handoff", artifact["id"], 30, 1))
        self.assertEqual((selected.digest, selected.size), (artifact["digest"], artifact["size_in_bytes"]))
        # One run listing and one artifact inventory of the newest run; no cache or owner read.
        self.assertEqual(len(self.world.api.requests("/actions/workflows/on-demand-e2e.yml/runs")), 1)
        self.assertEqual([call[0] for call in self.world.api.requests("/artifacts")],
                         [f"/repos/{QS_REPOSITORY}/actions/runs/30/artifacts"])

    def test_failed_foreign_and_earlier_attempt_handoffs_are_never_selected(self) -> None:
        failed = self.world.run(31, created=300, conclusion="failure")
        self.world.handoff(failed, QS_KEY)
        foreign = self.world.run(32, created=200, repository="fork/qs-like")
        self.world.handoff(foreign, QS_KEY)
        pushed = self.world.run(33, created=100, event="push")
        self.world.handoff(pushed, QS_KEY)
        rerun = self.world.run(34, created=50, attempt=2, earlier=({"run_attempt": 1},))
        self.world.artifact(grammar.handoff_name(QS_KEY, 1), rerun, created=100)
        expired = self.world.run(35, created=10)
        self.world.handoff(expired, QS_KEY, expired=True)
        with self.assertRaises(Unavailable) as caught:
            self.select()
        self.assertEqual(caught.exception.exit_code, 3)

    def test_newest_valid_cache_when_no_handoff_exists(self) -> None:
        self.world.pages_cache(40, QS_KEY, HEAD, created=900)
        _, other_id = self.world.pages_cache(41, QS_KEY, HEAD, created=950, workflow_id=77)
        _, invalid = self.world.pages_cache(42, QS_KEY, HEAD, created=1000, path=".github/workflows/build-gate.yml")
        selected = self.select()
        self.assertEqual((selected.kind, selected.run_id, selected.run_attempt), ("cache", 40, 1))
        self.assertNotIn(selected.artifact_id, (invalid["id"], other_id["id"]))
        self.assertEqual([call[0].rsplit("/", 1)[1] for call in self.world.api.requests("/actions/runs/4")],
                         ["42", "41", "40"])
        self.assertEqual(len(self.world.api.requests("/actions/workflows/pages.yml")), 1, "one workflow id read")

    def test_a_handoff_is_preferred_to_a_newer_cache(self) -> None:
        run = self.world.run(30, created=0)
        artifact = self.world.handoff(run, QS_KEY, created=100)
        self.world.pages_cache(40, QS_KEY, HEAD, created=900)
        self.assertEqual(self.select().artifact_id, artifact["id"])

    def test_unavailable_owner_stops_after_one_request_instead_of_probing_older_evidence(self) -> None:
        for status in (403, 429):
            with self.subTest(status=status):
                self.setUp()
                self.world.pages_cache(40, QS_KEY, HEAD, created=900)
                self.world.pages_cache(41, QS_KEY, HEAD, created=1000)
                self.world.api.failures["/actions/runs/41"] = ApiError("quota", status=status, method="GET", path="x")
                with self.assertRaises(ApiError):
                    self.select()
                self.assertEqual(len(self.world.api.requests("/actions/runs/4")), 1)

    def test_inventory_failure_is_never_absence(self) -> None:
        self.world.api.failures["/actions/artifacts"] = ApiError("quota", status=403, method="GET", path="x")
        with self.assertRaises(ApiError):
            self.select()
        self.setUp()
        self.world.run(30)
        self.world.api.failures["/actions/runs/30/artifacts"] = ApiError("down", status=502, method="GET", path="x")
        with self.assertRaises(ApiError):
            self.select()

    def test_a_cache_covers_only_its_exact_subject(self) -> None:
        self.world.pages_cache(40, QS_KEY, "e" * 40, created=900)
        with self.assertRaises(Unavailable):
            self.select()

    def test_nomination_is_reauthenticated_and_never_replaced_by_a_fallback(self) -> None:
        run = self.world.run(30)
        artifact = self.world.handoff(run, QS_KEY)
        self.world.pages_cache(40, QS_KEY, HEAD)
        selected = self.select(nomination=artifact["id"])
        self.assertEqual((selected.kind, selected.artifact_id, selected.run_attempt), ("handoff", artifact["id"], 1))
        self.assertEqual(len(self.world.api.requests(f"/actions/artifacts/{artifact['id']}")), 2,
                         "the nominated id is re-read after owner admission")

    def test_nominations_of_another_key_attempt_window_or_owner_fail_closed(self) -> None:
        def world_with(**changes: Any) -> tuple[World, dict[str, Any]]:
            world = World(QS_REPOSITORY)
            run = world.run(30, **changes.pop("run", {}))
            artifact = world.handoff(run, changes.pop("key", QS_KEY), **changes)
            world.pages_cache(40, QS_KEY, HEAD)
            return world, artifact

        cases = {
            "another key": world_with(key="mc26.3"),
            "expired": world_with(expired=True),
            "outside the upload step": world_with(created=700, step="Other step"),
            "failed owner": world_with(run={"conclusion": "failure"}),
            "another head": world_with(run={"head_sha": "e" * 40}),
            "schedule event": world_with(run={"event": "schedule"}),
            "another workflow id": world_with(run={"workflow_id": 78}),
            "another branch": world_with(run={"head_branch": "topic"}),
        }
        for label, (world, artifact) in cases.items():
            with self.subTest(label), self.assertRaises(MbError) as caught:
                select_evidence(self.invocation, api=world.api, key=QS_KEY, expected_subject_commit=HEAD,
                                nomination=artifact["id"])
            self.assertNotIsInstance(caught.exception, Unavailable, "a failed nomination is never 'no evidence'")
        world = World(QS_REPOSITORY)
        run = world.run(30, attempt=2, earlier=({"run_attempt": 1},))
        world.jobs(run, [world.job(QS_HANDOFF_JOB.replace("{key}", QS_KEY), steps=((QS_HANDOFF_STEP, 0, 900),))],
                   attempt=1)
        stale = world.artifact(grammar.handoff_name(QS_KEY, 1), run, created=300)
        with self.assertRaisesRegex(MbError, "attempt"):
            select_evidence(self.invocation, api=world.api, key=QS_KEY, expected_subject_commit=HEAD,
                            nomination=stale["id"])
        with self.assertRaises(MbError):
            select_evidence(self.invocation, api=self.world.api, key=QS_KEY, expected_subject_commit=HEAD,
                            nomination=424242)

    def test_family_handoff_then_family_cache(self) -> None:
        producer = self.world.run(60, path=FAMILY_WORKFLOW, event="repository_dispatch", created=100)
        self.world.jobs(producer, [self.world.job(FAMILY_JOB, steps=((FAMILY_STEP, 290, 310),))])
        handoff = self.world.artifact(grammar.family_handoff_name(FAMILY, QS_KEY, 1), producer, created=300)
        selected = self.select(family=FAMILY)
        self.assertEqual((selected.kind, selected.artifact_id), ("family-handoff", handoff["id"]))
        self.assertEqual(self.select(family=FAMILY, nomination=handoff["id"]).artifact_id, handoff["id"])
        self.setUp()
        wrong = self.world.run(61, path=FAMILY_WORKFLOW, event="pull_request")
        self.world.artifact(grammar.family_handoff_name(FAMILY, QS_KEY, 1), wrong, created=300)
        owner = self.world.run(62, path=PAGES_WORKFLOW_PATH, created=500)
        cache = self.world.artifact(grammar.family_cache_name(FAMILY, QS_KEY, HEAD), owner, created=600)
        selected = self.select(family=FAMILY)
        self.assertEqual((selected.kind, selected.artifact_id, selected.run_id), ("family-cache", cache["id"], 62))
        with self.assertRaises(MbError):
            self.select(family="unknown-family")

    def test_family_generations_are_bound_to_the_default_branch_head(self) -> None:
        other = self.world.run(60, path=FAMILY_WORKFLOW, head_sha="e" * 40)
        self.world.artifact(grammar.family_handoff_name(FAMILY, QS_KEY, 1), other, created=300)
        topic = self.world.run(61, path=FAMILY_WORKFLOW, head_branch="topic")
        self.world.artifact(grammar.family_handoff_name(FAMILY, QS_KEY, 1), topic, created=300)
        with self.assertRaises(Unavailable):
            self.select(family=FAMILY)


class BlockPopsNewestSourceTest(unittest.TestCase):
    """BP ``newest_exact_source``: the newest exact-subject run decides; never an older one.

    The Pages run executes at the protected ``master`` head ``CONTROLLER``; ``HEAD`` is a release
    subject that ``master`` runs attest by display title."""

    def setUp(self) -> None:
        self.world = World(BP_REPOSITORY)
        self.world.api.set_branch("master", CONTROLLER, TREE)
        self.invocation = bp_invocation(GITHUB_SHA=CONTROLLER)

    def select(self, commit: str = HEAD, **options: Any) -> Selected:
        options.setdefault("key", BP_KEY)
        return select_evidence(self.invocation, api=self.world.api, expected_subject_commit=commit, **options)

    def source(self, run_id: int, *, commit: str = HEAD, **changes: Any) -> dict[str, Any]:
        options = {"head_sha": CONTROLLER, "title": bp_title(commit), **changes}
        return self.world.run(run_id, **options)

    def test_release_subject_is_found_by_exact_title_on_the_default_controller(self) -> None:
        run = self.source(88, attempt=2, earlier=({"run_attempt": 1},))
        artifact = self.world.handoff(run, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        selected = self.select()
        self.assertEqual((selected.kind, selected.artifact_id, selected.run_id, selected.run_attempt),
                         ("handoff", artifact["id"], 88, 2))
        listings = self.world.api.requests("/workflows/on-demand-e2e.yml/runs")
        self.assertEqual({call[1].get("branch") for call in listings}, {"master"},
                         "only the canonical controller branch is searched")
        self.assertEqual(self.world.api.requests("/branches"), [], "a subject off the protected head is a release")
        with self.assertRaises(Unavailable):
            self.select("f" * 40)

    def test_newest_failed_pending_or_cancelled_never_falls_back_to_older_success(self) -> None:
        for status, conclusion in (("completed", "failure"), ("queued", None), ("in_progress", None),
                                   ("completed", "cancelled")):
            with self.subTest(status=status, conclusion=conclusion):
                self.setUp()
                older = self.source(100, created=0)
                self.world.handoff(older, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
                self.world.pages_cache(40, BP_KEY, HEAD, created=900, head_sha=CONTROLLER)
                self.source(101, created=3600, status=status, conclusion=conclusion)
                with self.assertRaises(Unavailable) as caught:
                    self.select()
                self.assertEqual(caught.exception.reason, "newest-run")

    def test_a_cache_is_selected_only_when_the_newest_run_lost_its_handoff(self) -> None:
        self.source(101, attempt=4, created=0)
        self.world.pages_cache(900, BP_KEY, HEAD, created=900, head_sha="9" * 40, attempt=2)
        selected = self.select()
        self.assertEqual((selected.kind, selected.run_id, selected.run_attempt), ("cache", 900, 2))

    def test_retry_order_uses_the_latest_attempt_but_dispatch_order_uses_creation(self) -> None:
        run = self.source(100, attempt=2, earlier=({"run_attempt": 1},))
        self.world.handoff(run, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        self.assertEqual(self.select().run_attempt, 2)
        self.setUp()
        self.source(100, attempt=2, conclusion="failure", earlier=({"run_attempt": 1},))
        with self.assertRaises(Unavailable):
            self.select()
        self.setUp()
        old = self.source(100, attempt=9, created=0, updated=86400)
        self.world.handoff(old, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        new = self.source(101, created=3600)
        artifact = self.world.handoff(new, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        self.assertEqual(self.select().artifact_id, artifact["id"])

    def test_wrong_controller_repository_workflow_title_and_event_are_never_sources(self) -> None:
        mutations = {"head_branch": "release/1.21.1", "repository": "fork/bp-like", "title": bp_title("e" * 40),
                     "event": "pull_request", "path": ".github/workflows/other.yml", "workflow_id": 78}
        for key, value in mutations.items():
            with self.subTest(key=key):
                self.setUp()
                self.source(100, **{key: value})
                with self.assertRaises(Unavailable):
                    self.select()
                good = self.source(99, created=-100)
                artifact = self.world.handoff(good, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
                self.assertEqual(self.select().artifact_id, artifact["id"])

    def test_schedule_vouches_only_for_the_canonical_subject(self) -> None:
        run = self.source(100, commit=CONTROLLER, event="schedule")
        artifact = self.world.handoff(run, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        self.assertEqual(self.select(CONTROLLER).artifact_id, artifact["id"])
        self.setUp()
        scheduled = self.source(100, event="schedule")
        self.world.handoff(scheduled, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        with self.assertRaises(Unavailable):
            self.select()

    def test_a_release_branch_run_is_never_a_source(self) -> None:
        # Only the protected controller produces evidence: a release branch's own copy of the source
        # workflow (dispatchable by anyone who can push that branch) never vouches, even for itself.
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                self.setUp()
                direct = self.source(100, head_branch=RELEASE, head_sha=HEAD, event=event)
                artifact = self.world.handoff(direct, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
                with self.assertRaises(Unavailable):
                    self.select()
                with self.assertRaises(MbError) as caught:
                    self.select(nomination=artifact["id"])
                self.assertNotIsInstance(caught.exception, Unavailable)

    def test_a_subject_sharing_the_protected_head_is_resolved_by_the_adapter(self) -> None:
        # master and a release branch share CONTROLLER: the scheduled run can vouch only for master,
        # so the release key's newest source is the older dispatched run (BP handoff_events).
        self.world.api.set_branch(RELEASE, CONTROLLER, TREE)
        older = self.source(100, commit=CONTROLLER, created=0)
        release_handoff = self.world.handoff(older, RELEASE_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        newer = self.source(101, commit=CONTROLLER, created=3600, event="schedule")
        master_handoff = self.world.handoff(newer, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        declared = [{"key": key, "label": branch, "subject": {"branch": branch, "commit": CONTROLLER, "tree": TREE},
                     "matrix_sha256": "1" * 64, "contract_sha256": "2" * 64}
                    for key, branch in ((BP_KEY, "master"), (RELEASE_KEY, RELEASE))]
        with mock.patch.object(host, "call", return_value=declared) as hook, \
                mock.patch.object(targets, "commit_tree_local", return_value=TREE):
            self.assertEqual(self.select(CONTROLLER, key=RELEASE_KEY).artifact_id, release_handoff["id"])
            self.assertEqual(self.select(CONTROLLER).artifact_id, master_handoff["id"])
            with self.assertRaises(Unavailable):
                self.select(CONTROLLER, key="2" * 24)
        self.assertEqual(hook.call_args.args[1:], ("targets", {"branches": [
            {"name": "master", "commit": CONTROLLER, "tree": TREE},
            {"name": RELEASE, "commit": CONTROLLER, "tree": TREE}]}))
        self.world.api.set_branch("master", "e" * 40, TREE)
        with self.assertRaises(MbError) as caught:
            self.select(CONTROLLER)
        self.assertEqual(caught.exception.reason, "stale-implementation")

    def test_a_nomination_from_an_older_run_is_refused(self) -> None:
        older = self.source(100, created=0)
        artifact = self.world.handoff(older, BP_KEY, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        self.assertEqual(self.select(nomination=artifact["id"]).artifact_id, artifact["id"])
        self.source(101, created=3600)
        with self.assertRaises(MbError) as caught:
            self.select(nomination=artifact["id"])
        self.assertNotIsInstance(caught.exception, Unavailable)
        self.assertEqual(caught.exception.reason, "newest-run")


class SelectCommandTest(unittest.TestCase):
    def test_outputs_and_the_selected_json_document(self) -> None:
        world = World(QS_REPOSITORY)
        run = world.run(30)
        artifact = world.handoff(run, QS_KEY)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(commands_control, "_client", return_value=world.api), \
                mock.patch.object(cli, "environ", return_value=dict(qs_invocation().environ)):
            output, document = Path(directory) / "output", Path(directory) / "selected.json"
            arguments = ["select", "--repo", str(support.MODS / "qs_like"), "--key", QS_KEY,
                         "--expected-subject-commit", HEAD, "--github-output", str(output), "--output", str(document)]
            self.assertEqual(cli.main(arguments), 0)
            lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(lines, {"kind": "handoff", "artifact_id": str(artifact["id"]), "name": artifact["name"],
                                     "digest": artifact["digest"], "size": str(artifact["size_in_bytes"]),
                                     "run_id": "30", "run_attempt": "1"})
            written = document.read_bytes()
            self.assertEqual(Selected.parse(json.loads(written)).artifact_id, artifact["id"])
            self.assertTrue(written.endswith(b"\n"))
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(cli.main(arguments), 2, "--output never replaces an existing file")
            missing = arguments[:4] + ["mc26.3"] + arguments[5:-2]
            (Path(directory) / "output").unlink()
            with redirect_stderr(stderr):
                self.assertEqual(cli.main(missing), 3)
            self.assertFalse(output.exists(), "no output is written for absent evidence")


if __name__ == "__main__":
    unittest.main()
