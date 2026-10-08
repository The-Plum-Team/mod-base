"""Full tested-record chronology uses API steps, never a record's claimed execution time."""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci.graph import BuildGraphV1, PackagedGraphV1, authenticate_gate_timeline
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.github.jobs import job_graph_sha256
from mod_base.workflow import (CI_BUILD_CALL, CI_BUILD_JOBS, CI_PACKAGED_CALL, CI_PACKAGED_JOBS,
                               CI_SEAL_STEP, CI_UPLOAD_STEP)
from tests.helpers import ci_gate, ci_plan
from tests.test_ci_records import selected_record


def timeline_fixture(gate="build"):
    plan, document = ci_plan(), ci_gate(gate)
    api = FakeGitHub(repository=plan["identity"]["repository"])
    graphs = {"build": BuildGraphV1(), "packaged": PackagedGraphV1()}
    digest = job_graph_sha256(graphs[gate].jobs(plan))
    document["producer"]["graph_sha256"] = digest
    for source in document["artifacts"]:
        source["producer"]["graph_sha256"] = digest
    if document["owning_build"]:
        document["owning_build"]["producer"]["graph_sha256"] = job_graph_sha256(graphs["build"].jobs(plan))
    descriptor = selected_record(document)
    all_jobs = {}
    for kind in ("build", "packaged") if gate == "packaged" else ("build",):
        call, names = (CI_BUILD_CALL, CI_BUILD_JOBS) if kind == "build" else (CI_PACKAGED_CALL, CI_PACKAGED_JOBS)
        jobs = [{**entry, "status": "completed", "started_at": "2026-10-07T10:00:00Z",
                 "completed_at": "2026-10-07T10:02:00Z", "steps": []} for entry in graphs[kind].jobs(plan)]
        for job in jobs:
            if job["name"] in graphs[kind].sealed_jobs(plan):
                job["steps"] = [
                    {"name": CI_SEAL_STEP, "status": "completed", "conclusion": "success",
                     "started_at": "2026-10-07T10:00:00Z", "completed_at": "2026-10-07T10:01:00Z"},
                    {"name": CI_UPLOAD_STEP, "status": "completed", "conclusion": "success",
                     "started_at": "2026-10-07T10:01:00Z", "completed_at": "2026-10-07T10:02:00Z"}]
            if kind == gate and job["name"] == f"{call} / {names['gate']}":
                job.update(started_at="2026-10-07T10:02:00Z", completed_at="2026-10-07T10:06:00Z")
                job["steps"] = [
                    {"name": CI_SEAL_STEP, "status": "completed", "conclusion": "success",
                     "started_at": "2026-10-07T10:03:00Z", "completed_at": "2026-10-07T10:04:00Z"},
                    {"name": CI_UPLOAD_STEP, "status": "completed", "conclusion": "success",
                     "started_at": "2026-10-07T10:05:00Z", "completed_at": "2026-10-07T10:06:00Z"}]
        all_jobs[kind] = jobs
        api.add_jobs(42 if kind == "build" else 43, 2, jobs)
    return plan, api, document, descriptor, all_jobs


class GateTimelineTests(unittest.TestCase):
    def call(self, fixture):
        plan, api, document, descriptor, jobs = fixture
        for kind, entries in jobs.items():
            api.add_jobs(42 if kind == "build" else 43, 2, entries)
        return authenticate_gate_timeline(api, document=document, descriptor=descriptor, plan=plan)

    def gate_job(self, fixture):
        kind = fixture[2]["gate"]
        call, names = (CI_BUILD_CALL, CI_BUILD_JOBS) if kind == "build" else (CI_PACKAGED_CALL, CI_PACKAGED_JOBS)
        return next(job for job in fixture[4][kind] if job["name"] == f"{call} / {names['gate']}")

    def test_complete_build_and_packaged_timeline_is_read_only(self):
        for kind in ("build", "packaged"):
            fixture = timeline_fixture(kind)
            before = copy.deepcopy(fixture[2:4])
            self.assertEqual(self.call(fixture), fixture[2]["producer"]["graph_sha256"])
            self.assertEqual(fixture[2:4], before)
            self.assertEqual(fixture[1].mutations, [])

    def test_gate_cannot_validate_before_sources_finish_even_when_record_upload_is_later(self):
        for kind in ("build", "packaged"):
            fixture = timeline_fixture(kind)
            self.gate_job(fixture)["steps"][0]["started_at"] = "2026-10-07T10:02:00Z"
            source = next(job for job in fixture[4][kind] if job["steps"] and job is not self.gate_job(fixture))
            source["completed_at"] = "2026-10-07T10:02:01Z"
            with self.subTest(kind=kind), self.assertRaisesRegex(MbError, "prerequisites"):
                self.call(fixture)

    def test_equal_time_boundary_is_allowed(self):
        fixture = timeline_fixture()
        self.gate_job(fixture)["steps"][0]["started_at"] = "2026-10-07T10:02:00Z"
        self.call(fixture)

    def test_source_self_reported_window_must_equal_actual_upload(self):
        for kind, owning in (("build", False), ("packaged", False), ("packaged", True)):
            fixture = timeline_fixture(kind)
            source = fixture[2]["owning_build"] if owning else fixture[2]["artifacts"][0]
            source["producer"]["upload_window"]["started_at"] = "2026-10-07T10:00:30Z"
            with self.subTest(kind=kind, owning=owning), self.assertRaisesRegex(MbError, "actual API"):
                self.call(fixture)

    def test_gate_window_must_equal_selected_upload(self):
        fixture = timeline_fixture()
        self.gate_job(fixture)["steps"][1]["started_at"] = "2026-10-07T10:04:30Z"
        with self.assertRaisesRegex(MbError, "tested-record"):
            self.call(fixture)

    def test_missing_duplicate_failed_or_overlapping_gate_steps_reject(self):
        mutations = [lambda j: j["steps"].pop(), lambda j: j["steps"].append(copy.deepcopy(j["steps"][0])),
                     lambda j: j["steps"][0].update(conclusion="failure"),
                     lambda j: j["steps"][0].update(completed_at="2026-10-07T10:05:01Z")]
        for index, mutate in enumerate(mutations):
            fixture = timeline_fixture()
            mutate(self.gate_job(fixture))
            with self.subTest(index=index), self.assertRaises(MbError):
                self.call(fixture)

    def test_missing_reversed_or_outside_job_times_reject(self):
        mutations = [lambda j: j.pop("started_at"),
                     lambda j: j.update(completed_at="2026-10-07T09:00:00Z"),
                     lambda j: j["steps"][0].update(started_at="2026-10-07T10:01:59Z")]
        for index, mutate in enumerate(mutations):
            fixture = timeline_fixture()
            mutate(self.gate_job(fixture))
            with self.subTest(index=index), self.assertRaises(MbError):
                self.call(fixture)

    def test_every_policy_and_target_prerequisite_must_finish_first(self):
        for name in (CI_BUILD_JOBS["policy"], CI_BUILD_JOBS["target"].format(id="target-a")):
            fixture = timeline_fixture()
            job = next(j for j in fixture[4]["build"] if j["name"] == f"{CI_BUILD_CALL} / {name}")
            job["completed_at"] = "2026-10-07T10:03:01Z"
            with self.subTest(name=name), self.assertRaisesRegex(MbError, "prerequisites"):
                self.call(fixture)

    def test_owning_build_full_graph_and_completion_are_independent(self):
        for mutation in ("missing", "late", "digest", "seal"):
            fixture = timeline_fixture("packaged")
            jobs = fixture[4]["build"]
            if mutation == "missing":
                jobs.pop()
            elif mutation == "late":
                jobs[0]["completed_at"] = "2026-10-07T10:03:01Z"
            elif mutation == "digest":
                fixture[2]["owning_build"]["producer"]["graph_sha256"] = "f" * 64
            else:
                next(j for j in jobs if j["steps"])["steps"].pop()
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(fixture)

    def test_full_gate_graph_rejects_missing_extra_failed_and_wrong_digest(self):
        for mutation in ("missing", "extra", "failed", "digest"):
            fixture = timeline_fixture()
            jobs = fixture[4]["build"]
            if mutation == "missing":
                jobs.pop()
            elif mutation == "extra":
                jobs.append({**jobs[0], "name": "Unexpected"})
            elif mutation == "failed":
                jobs[0]["conclusion"] = "failure"
            else:
                fixture[2]["producer"]["graph_sha256"] = "f" * 64
                fixture[3]["producer"]["graph_sha256"] = "f" * 64
                fixture[2]["artifacts"][0]["producer"]["graph_sha256"] = "f" * 64
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(fixture)
