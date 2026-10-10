"""Descriptors of a running attempt's own artifacts: fake GitHub API, literal job listings."""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci import describe, selection, transport
from mod_base.build_ci.records import validate_descriptor
from mod_base.errors import MbError
from mod_base.model import limits
from tests.ci_attempt import ATTEMPT, AttemptCase, expand
from tests.helpers import ci_graph_jobs
from tests.test_ci_transport import build_archive

GUARD = "Verify pinned mod-base / Authenticate the pinned kit"
PLAN = "Shared Build / Plan protected Build"
POLICY = "Shared Build / Verify protected policy"
TARGET = "Shared Build / Compile target target-a"
ASSEMBLE = "Shared Build / Seal complete Build bundle"
GATE = "Shared Build / Verify complete Build"
SELECT = "Select exact Build / Select exact Build source"
INPUT = "Shared Packaged E2E / Authenticate exact Build"
LANE = "Shared Packaged E2E / Run packaged lane lane-a"
AGGREGATE = "Shared Packaged E2E / Seal complete packaged results"
RERUN = "a failed-jobs-only rerun mixes attempts; rerun all jobs"


class DescribeAttemptTests(AttemptCase):
    def world(self, *, second: int | None = ATTEMPT):
        """A full Build of two targets whose assembling job runs: every target is sealed and
        uploaded. ``second`` is the attempt whose name the second partition carries, or ``None``
        when it was never uploaded."""

        attempt = self.attempt(listing="build-full", targets=2)
        attempt.sealing(ASSEMBLE)
        for index, target in enumerate(attempt.plan["targets"]):
            data, _ = build_archive(attempt.plan, attempt.producer("full"), target_id=target["id"])
            if index == 0 or second is not None:
                attempt.publish("target", target["id"], data, artifact_id=110 + index,
                                attempt=ATTEMPT if index == 0 else second)
        return attempt

    def describe(self, attempt, **changes):
        arguments = dict(producer=attempt.producer("full"), plan=attempt.plan, mode="full",
                         expected=[("target", target["id"]) for target in attempt.plan["targets"]],
                         finished=describe.settled_jobs("build", "full", attempt.plan, "build"))
        arguments.update(changes)
        return describe.describe_attempt(attempt.api, **arguments)

    def rejected(self, attempt, pattern: str, **changes) -> str:
        with self.assertRaisesRegex(MbError, pattern) as caught:
            self.describe(attempt, **changes)
        return str(caught.exception)

    def test_every_target_is_described_in_plan_order_from_two_requests(self) -> None:
        attempt = self.world()
        descriptors = self.describe(attempt)
        self.assertEqual(attempt.api.request_count, 2)
        self.assertEqual([descriptor["artifact"]["name"] for descriptor in descriptors],
                         ["mb-ci-target--42--a2--target-a", "mb-ci-target--42--a2--target-02"])
        for index, descriptor in enumerate(descriptors):
            record = attempt.records[110 + index]
            self.assertIs(validate_descriptor(descriptor), descriptor)
            self.assertEqual(descriptor["producer"], {**attempt.producer("full"), "upload_window": {
                "started_at": "2026-10-07T10:01:00Z", "completed_at": "2026-10-07T10:02:00Z"}})
            self.assertEqual(descriptor["artifact"], {
                "id": record["id"], "name": record["name"], "digest": record["digest"],
                "size": record["size_in_bytes"], "created_at": record["created_at"], "expires_at": record["expires_at"]})
            self.assertEqual({key: descriptor[key] for key in ("identity", "plan_sha256", "profile")},
                             {key: attempt.plan[key] for key in ("identity", "plan_sha256", "profile")})

    def test_the_existing_target_reader_accepts_the_descriptors(self) -> None:
        attempt = self.world()
        descriptors = self.describe(attempt)
        partitions = transport.download_target_set(attempt.api, descriptors=descriptors, plan=attempt.plan,
                                                   run_id=42, run_attempt=ATTEMPT, output=self.temporary / "targets")
        self.assertEqual([partition["descriptor"] for partition in partitions], descriptors)

    def test_the_complete_build_is_described_as_selection_describes_the_finished_run(self) -> None:
        attempt = self.attempt(listing="build-full")
        attempt.sealing(GATE)
        data, _ = build_archive(attempt.plan, attempt.producer("full"))
        attempt.publish("build", None, data, artifact_id=100)
        described = self.describe(attempt, expected=[("build", None)],
                                  finished=describe.settled_jobs("build", "full", attempt.plan, "tested", "build"))
        attempt.jobs = expand(ci_graph_jobs("build-full"), attempt.plan)
        attempt.seed_jobs()
        attempt.set_run(status="completed", conclusion="success")
        self.assertEqual([selection.select_latest_pr_build(attempt.api, plan=attempt.plan)], described)

    def test_a_missing_an_expired_a_repeated_an_oversized_and_a_foreign_artifact_reject(self) -> None:
        cases = {
            "expired": (lambda attempt: attempt.set_artifact(110, expired=True),
                        r"artifact 'mb-ci-target--42--a2--target-a' has expired"),
            "repeated": (lambda attempt: attempt.api.add_artifact({**attempt.records[110], "id": 190},
                                                                  attempt.archives[110]),
                         r"artifact 'mb-ci-target--42--a2--target-a' is listed more than once"),
            "oversized": (lambda attempt: attempt.set_artifact(
                110, size_in_bytes=limits.MAX_CI_BUNDLE_COMPRESSED_BYTES + 1), r"exceeds the size cap of its kind"),
            "another head": (lambda attempt: attempt.set_artifact(110, workflow_run={"head_sha": "9" * 40}),
                             r"is not recorded under this run and the subject's head"),
            "another run": (lambda attempt: attempt.set_artifact(110, workflow_run={"id": 77}),
                            r"this attempt has no artifact 'mb-ci-target--42--a2--target-a'"),
        }
        messages = {self.rejected(self.world(second=None),
                                  r"this attempt has no artifact 'mb-ci-target--42--a2--target-02'")}
        for label, (change, pattern) in cases.items():
            with self.subTest(case=label):
                attempt = self.world()
                change(attempt)
                messages.add(self.rejected(attempt, pattern))
        self.assertEqual(len(messages), len(cases) + 1)

    def test_an_artifact_the_plan_does_not_expect_of_this_attempt_rejects(self) -> None:
        attempt = self.world()
        attempt.publish("target", "target-zz", b"extra", artifact_id=150, job=TARGET)
        self.rejected(attempt, r"artifact 'mb-ci-target--42--a2--target-zz' is not one the plan expects")
        # Another kind and an earlier attempt's artifact are not this job's concern.
        attempt = self.world()
        attempt.publish("build", None, b"later", artifact_id=150, job=TARGET)
        attempt.publish("target", "target-zz", b"earlier", artifact_id=151, attempt=1, job=TARGET)
        self.assertEqual(len(self.describe(attempt)), 2)

    def test_a_job_that_did_not_succeed_or_did_not_seal_before_its_upload_rejects(self) -> None:
        def reorder(attempt) -> None:
            steps = attempt.job(TARGET)["steps"]
            steps[2]["completed_at"] = "2026-10-07T10:01:30.000Z"
            attempt.seed_jobs()

        cases = {
            "failed target": (lambda attempt: attempt.change_job(TARGET, conclusion="failure"),
                              rf"required job '{TARGET}' has not finished as the graph expects"),
            "running policy": (lambda attempt: attempt.change_job(POLICY, status="in_progress", conclusion=None),
                               rf"required job '{POLICY}' has not finished as the graph expects"),
            "cancelled plan": (lambda attempt: attempt.change_job(PLAN, conclusion="cancelled"),
                               rf"required job '{PLAN}' has not finished as the graph expects"),
            "unsealed upload": (reorder, r"upload started before sealing finished"),
            "unenrolled job": (lambda attempt: attempt.job(POLICY).update(name="Shared Build / Something else")
                               or attempt.seed_jobs(), r"duplicate or unenrolled job"),
        }
        for label, (change, pattern) in cases.items():
            with self.subTest(case=label):
                attempt = self.world()
                change(attempt)
                self.rejected(attempt, pattern)

    def test_a_job_or_an_artifact_of_an_earlier_attempt_is_a_mixed_attempt(self) -> None:
        attempt = self.world()
        attempt.change_job(PLAN, run_attempt=1)
        job = self.rejected(attempt, rf"job '{PLAN}' ran in attempt 1, not in attempt 2: {RERUN}")
        artifact = self.rejected(self.world(second=1),
                                 rf"artifact 'mb-ci-target--42--a1--target-02' is not of attempt 2: {RERUN}")
        missing = self.rejected(self.world(second=None), r"this attempt has no artifact")
        self.assertEqual(len({job, artifact, missing}), 3)

    def test_the_producer_must_carry_the_graph_of_the_mode_and_the_expectation_be_exact(self) -> None:
        attempt = self.world()
        self.rejected(attempt, r"is not the graph of this run in that mode",
                      producer={**attempt.producer("full"), "graph_sha256": "0" * 64})
        self.rejected(attempt, r"must name distinct artifacts", expected=[("target", "target-a")] * 2)
        self.rejected(attempt, r"\$\.producer", producer={**attempt.producer("full"), "upload_window": {}})
        with self.assertRaises(MbError):
            self.describe(attempt, expected=[("runtime", "lane-a")])  # the Build caller runs no lane
        self.assertEqual(attempt.api.request_count, 0)
        self.rejected(attempt, r"required job is outside this graph", finished=["Shared Build / No such job"])
        # With nothing expected only the jobs are read and required.
        self.assertEqual(self.describe(attempt, expected=[]), [])
        self.assertEqual(attempt.api.request_count, 2)


class AttemptShapeTests(AttemptCase):
    def test_the_producer_identity_of_an_attempt(self) -> None:
        attempt = self.attempt(listing="build-full")
        identity = attempt.plan["identity"]
        self.assertEqual(attempt.producer("full"), {
            "run_id": 42, "run_attempt": 2, "workflow_path": ".github/workflows/mod-base-build.yml",
            "workflow_ref": f"{identity['repository']}/.github/workflows/mod-base-build.yml@refs/heads/main",
            "api_head_sha": identity["head_sha"], "event": "pull_request_target",
            "graph_sha256": attempt.producer("full")["graph_sha256"]})
        self.assertNotEqual(attempt.producer("full")["graph_sha256"], attempt.producer("reuse")["graph_sha256"])
        other = copy.deepcopy(attempt.plan)
        other["identity"]["pr_number"] = 8
        for arguments in (dict(plan=other), dict(mode="pull-request"), dict(run_id=0), dict(run_attempt=True)):
            with self.subTest(arguments=sorted(arguments)), self.assertRaises(MbError):
                describe.attempt_producer(**{"record": attempt.record, "plan": attempt.plan, "mode": "full",
                                             "run_id": 42, "run_attempt": 2, **arguments})

    def test_a_protected_push_is_recorded_under_the_commit_it_runs_from(self) -> None:
        attempt = self.attempt(listing="build-full", push=True)
        producer = attempt.producer("full")
        self.assertEqual((producer["event"], producer["api_head_sha"]),
                         ("push", attempt.plan["identity"]["controller_sha"]))

    def test_the_jobs_and_artifacts_that_are_settled_when_a_job_seals(self) -> None:
        plan = self.attempt(listing="build-full").plan
        deferred, e2e_deferred = "Build deferred for draft", "Packaged E2E deferred for draft"
        target, lane = ("target", "target-a"), ("runtime", "lane-a")
        cases = [
            (("build", "full", "build"), [GUARD, PLAN, POLICY, TARGET, deferred], [target]),
            (("build", "full", "tested", "build"), [GUARD, PLAN, POLICY, TARGET, ASSEMBLE, deferred],
             [target, ("build", None)]),
            (("build", "reuse", "tested", "build"),
             [GUARD, PLAN, deferred, POLICY, "Shared Build / Compile target ${{ matrix.id }}", ASSEMBLE], []),
            (("packaged", "pull-request", "results"),
             [GUARD, INPUT, LANE, e2e_deferred, "Select exact Build", "Shared Build"], [lane]),
            (("packaged", "selected", "tested", "packaged"),
             [GUARD, SELECT, INPUT, LANE, AGGREGATE, e2e_deferred, "Shared Build"], [lane, ("results", None)]),
            (("packaged", "rebuilt", "build"), [GUARD, SELECT, PLAN, POLICY, TARGET, e2e_deferred], [target]),
            (("packaged", "rebuilt", "tested", "build"), [GUARD, SELECT, PLAN, POLICY, TARGET, ASSEMBLE, e2e_deferred],
             [target, ("build", None)]),
            (("packaged", "rebuilt", "tested", "packaged"),
             [GUARD, SELECT, PLAN, POLICY, TARGET, ASSEMBLE, GATE, INPUT, LANE, AGGREGATE, e2e_deferred],
             [target, ("build", None), ("tested", "build"), lane, ("results", None)]),
        ]
        for arguments, jobs, artifacts in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(sorted(describe.settled_jobs(arguments[0], arguments[1], plan, *arguments[2:])),
                                 sorted(jobs))
                self.assertEqual(describe.settled_artifacts(arguments[0], arguments[1], plan, *arguments[2:]),
                                 artifacts)
        for arguments in (("build", "full", "target", "target-a"), ("build", "full", "build", "x"),
                          ("build", "full", "results"), ("build", "deferred", "tested", "build"),
                          ("packaged", "pull-request", "tested", "build")):
            with self.subTest(arguments=arguments), self.assertRaises(MbError):
                describe.settled_jobs(arguments[0], arguments[1], plan, *arguments[2:])


if __name__ == "__main__":
    unittest.main()
