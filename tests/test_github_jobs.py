"""``github.jobs``: exact attempt-scoped job listings, the canonical job graph (Block Pops
``e2e_job_graph.validate_jobs`` semantics: the observed names equal the expected set exactly, each
completed with its expected conclusion) and exact-name step windows."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from mod_base import workflow
from mod_base.errors import MbError
from mod_base.github import jobs
from mod_base.github.fake import FakeGitHub
from mod_base.model.canonical import canonical_sha256
from tests.helpers import REPOSITORY

EXPECTED = [{"name": "Build / verify", "conclusion": "success"}, {"name": "Packaged E2E / 1.20.1-fabric",
                                                                    "conclusion": "success"},
            {"name": "Optional", "conclusion": "skipped"}]
STEP = "Retain the complete compact generation for feature evidence reuse"


def job(name: str, conclusion: str = "success", *, status: str = "completed", **changes: Any) -> dict[str, Any]:
    value = {"name": name, "status": status, "conclusion": conclusion, "steps": [
        {"name": "Set up job", "status": "completed", "conclusion": "success", "number": 1,
         "started_at": "2026-09-25T10:00:00Z", "completed_at": "2026-09-25T10:00:01Z"},
        {"name": STEP, "status": "completed", "conclusion": "success", "number": 2,
         "started_at": "2026-09-25T10:00:02.123456789+02:00", "completed_at": "2026-09-25T10:00:05.5+02:00"},
    ]}
    value.update(changes)
    return value


def observed() -> list[dict[str, Any]]:
    return [job(entry["name"], entry["conclusion"]) for entry in reversed(EXPECTED)]


class AttemptJobsTests(unittest.TestCase):
    def test_every_page_of_exactly_one_attempt(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        api.add_jobs(7, 2, [job(f"lane {index:03d}") for index in range(150)])
        api.add_jobs(7, 1, [job("earlier attempt")])
        listed = jobs.attempt_jobs(api, 7, 2)
        self.assertEqual(150, len(listed))
        self.assertEqual({2}, {item["run_attempt"] for item in listed})
        self.assertEqual(2, api.request_count)
        self.assertEqual(["earlier attempt"], [item["name"] for item in jobs.attempt_jobs(api, 7, 1)])

    def test_foreign_duplicate_or_malformed_jobs_are_rejected(self) -> None:
        cases = {
            "other attempt": [job("a", run_attempt=1)],
            "other run": [job("a", run_id=8)],
            "duplicate id": [job("a", id=5), job("b", id=5)],
            "bool id": [job("a", id=True)],
            "bool attempt": [job("a", run_attempt=True)],
            "unnamed": [job("", id=6)],
            "control name": [job("a\nb")],
            "long name": [job("x" * 201)],
            "empty": [],
        }
        for label, rows in cases.items():
            with self.subTest(label=label):
                api = FakeGitHub(repository=REPOSITORY)
                api.add_jobs(7, 2, rows)
                with self.assertRaises(MbError):
                    jobs.attempt_jobs(api, 7, 2)
        api = FakeGitHub(repository=REPOSITORY)
        api.add_jobs(7, 2, [job(f"lane {index}") for index in range(1001)])
        with self.assertRaisesRegex(MbError, "bound"):
            jobs.attempt_jobs(api, 7, 2)
        for run_id, attempt in ((0, 1), (1, 0), (True, 1), (1, 1001)):
            with self.subTest(run_id=run_id, attempt=attempt), self.assertRaises(MbError):
                jobs.attempt_jobs(api, run_id, attempt)


class JobGraphTests(unittest.TestCase):
    def test_graph_is_the_sorted_name_conclusion_list_and_its_hash_is_canonical(self) -> None:
        graph = jobs.job_graph(observed())
        self.assertEqual(sorted(EXPECTED, key=lambda entry: entry["name"]), graph)
        self.assertEqual(canonical_sha256(graph), jobs.job_graph_sha256(graph))
        self.assertEqual(jobs.job_graph_sha256(graph), jobs.job_graph_sha256(list(reversed(graph))))
        self.assertNotEqual(jobs.job_graph_sha256(graph),
                            jobs.job_graph_sha256([{**graph[0], "conclusion": "failure"}, *graph[1:]]))

    def test_graph_requires_finished_uniquely_named_jobs(self) -> None:
        for rows in ([job("a"), job("a")], [job("a", status="in_progress", conclusion=None)],
                     [job("a", conclusion="exploded")], [job("a", conclusion=None)], ["job"]):
            with self.subTest(rows=str(rows)[:50]), self.assertRaises(MbError):
                jobs.job_graph(rows)  # type: ignore[arg-type]
        for graph in ([{"name": "a", "conclusion": "success", "id": 1}], [{"name": "a"}], "graph",
                      [{"name": "a", "conclusion": "success"}] * 2):
            with self.subTest(graph=str(graph)[:50]), self.assertRaises(MbError):
                jobs.job_graph_sha256(graph)  # type: ignore[arg-type]

    def test_require_job_graph_is_exact(self) -> None:
        self.assertEqual(jobs.job_graph_sha256(EXPECTED), jobs.require_job_graph(observed(), EXPECTED))
        cases = {
            "missing": observed()[1:],
            "unexpected": [*observed(), job("Injected")],
            "different conclusion": [job(entry["name"], "failure") for entry in EXPECTED],
            "suffix match only": [job("Publish / " + entry["name"], entry["conclusion"]) for entry in EXPECTED],
        }
        for label, rows in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(MbError, "job graph"):
                jobs.require_job_graph(rows, EXPECTED)
        with self.assertRaisesRegex(MbError, "empty"):
            jobs.require_job_graph([], [])
        with self.assertRaises(MbError):
            jobs.require_job_graph(observed(), [*EXPECTED, EXPECTED[0]])


class FindJobAndStepTests(unittest.TestCase):
    def test_find_job_is_the_workflow_exact_lookup(self) -> None:
        self.assertIs(workflow.find_job, jobs.find_job)
        rows = [job("Publish / Build atomic static site", run_attempt=2), job("Build atomic static site", run_attempt=2)]
        self.assertEqual("Build atomic static site",
                         jobs.find_job(rows, "Build atomic static site", run_attempt=2)["name"])
        with self.assertRaises(MbError):
            jobs.find_job(rows, "Build atomic static site", run_attempt=1)

    def test_step_window_of_the_single_exactly_named_step(self) -> None:
        started, completed = jobs.step_window(job("a"), STEP)
        self.assertEqual(datetime(2026, 9, 25, 8, 0, 2, 123456, tzinfo=timezone.utc), started)
        self.assertEqual(started + timedelta(seconds=3, microseconds=376544), completed)
        self.assertEqual(STEP, jobs.require_successful_step(job("a"), STEP)["name"])

    def test_step_lookups_fail_closed(self) -> None:
        base = job("a")
        failed = job("a", steps=[{**base["steps"][1], "conclusion": "failure"}])
        running = job("a", steps=[{**base["steps"][1], "status": "in_progress", "conclusion": None}])
        duplicated = job("a", steps=[base["steps"][1], base["steps"][1]])
        backwards = job("a", steps=[{**base["steps"][1], "completed_at": "2026-09-25T07:00:00Z"}])
        for label, value, name in (("absent", base, "Retain"), ("prefix", base, STEP + " "),
                                   ("duplicated", duplicated, STEP), ("no steps", job("a", steps=None), STEP),
                                   ("malformed step", job("a", steps=["step"]), STEP)):
            with self.subTest(label=label):
                self.assertRaises(MbError, jobs.step_window, value, name)
                self.assertRaises(MbError, jobs.require_successful_step, value, name)
        self.assertRaises(MbError, jobs.require_successful_step, failed, STEP)
        self.assertRaises(MbError, jobs.require_successful_step, running, STEP)
        self.assertRaisesRegex(MbError, "before it started", jobs.step_window, backwards, STEP)
        for stamp in ("2026-09-25 10:00:00Z", "2026-09-25T10:00:00", "2026-13-01T00:00:00Z", None, 5):
            with self.subTest(stamp=stamp):
                self.assertRaises(MbError, jobs.step_window,
                                  job("a", steps=[{**base["steps"][1], "started_at": stamp}]), STEP)


if __name__ == "__main__":
    unittest.main()
