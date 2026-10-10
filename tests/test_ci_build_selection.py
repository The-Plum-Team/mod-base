"""The newest Build run of a pull-request head is chosen before any result is read.

No status filter, no run title and no older fallback: the listing is keyed by what GitHub records
on a ``pull_request_target`` run (the pull request's head commit, branch and repository).
"""

import copy
import unittest
from unittest.mock import Mock, patch

from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.selection import revalidate_latest_pr_build, select_latest_pr_build, wait_for_latest_pr_build
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from tests.helpers import ci_api_run, ci_failed_jobs_rerun, ci_graph_jobs
from tests.test_ci_transport import ASSEMBLE, World, build_world

LISTING = "/repos/example/mod/actions/workflows/mod-base-build.yml/runs"


def later_run(world, *, run_id=44, listing=None, **changes):
    """A newer Build run for the same head; by default it has failed."""

    run = ci_api_run(world.plan, "build", **{"id": run_id, "created_at": "2026-10-07T11:00:00Z",
                                             "conclusion": "failure", **changes})
    world.runs[run_id] = run
    world.api.add_run(run)
    if listing is not None:
        jobs = ci_graph_jobs(listing)
        for job in jobs:
            job["run_id"] = run_id
        world.api.add_jobs(run_id, run["run_attempt"], jobs)
    return run


class PrBuildSelectionTests(unittest.TestCase):
    def call(self, world):
        return select_latest_pr_build(world.api, plan=world.plan)

    def test_successful_newest_bundle_is_described_from_api_data(self):
        world = build_world()
        observed = self.call(world)
        self.assertEqual(observed, world.bundle)
        self.assertEqual(observed["producer"]["api_head_sha"], world.plan["identity"]["head_sha"])
        self.assertEqual(observed["producer"]["workflow_path"], ".github/workflows/mod-base-build.yml")
        self.assertEqual(observed["producer"]["upload_window"],
                         {"started_at": "2026-10-07T10:02:30Z", "completed_at": "2026-10-07T10:03:30Z"})
        self.assertEqual(world.api.mutations, [])
        world.plan["identity"]["head_sha"] = "f" * 40
        self.assertEqual(observed["identity"]["head_sha"], "1" * 40)

    def test_listing_is_keyed_by_the_pull_request_head_without_status_or_title(self):
        world = build_world()
        with patch.object(world.api, "get_json", wraps=world.api.get_json) as get:
            self.call(world)
        listings = [call for call in get.call_args_list if call.args[0] == LISTING]
        self.assertEqual(len(listings), 1)
        params = listings[0].kwargs["params"]
        self.assertEqual({key: params[key] for key in ("branch", "head_sha", "event")},
                         {"branch": "feature/example", "head_sha": "1" * 40, "event": "pull_request_target"})
        self.assertNotIn("status", params)
        artifacts = [call for call in get.call_args_list if call.args[0].endswith("/42/artifacts")]
        self.assertEqual(artifacts[0].kwargs["params"]["name"], world.bundle["artifact"]["name"])
        # Any title: the run is found and bound without one.
        for title in ("Example change", "mb-ci-build-v1 profile=block-pops pr=8", None):
            world = build_world(display_title=title)
            self.assertEqual(self.call(world), world.bundle)
        self.assertFalse(hasattr(grammar, "ci_pr_build_title") or hasattr(grammar, "parse_ci_pr_build_title"))

    def test_runs_of_another_head_branch_repository_event_or_workflow_are_not_candidates(self):
        for changes in ({"head_sha": "f" * 40}, {"head_branch": "other"}, {"event": "pull_request"},
                        {"head_repository": {"full_name": "fork/mod"}},
                        {"path": ".github/workflows/mod-base-packaged-e2e.yml"},
                        {"head_sha": "2" * 40, "head_branch": "master"}):
            world = build_world()
            later_run(world, **changes)
            with self.subTest(changes=changes):
                self.assertEqual(self.call(world), world.bundle)
            world = World()
            world.add_run("build", "build-full", **changes)
            with self.subTest(changes=changes, only=True):
                self.assertIsNone(self.call(world))

    def test_newest_failed_cancelled_neutral_and_skipped_runs_never_use_old_success(self):
        for conclusion in ("failure", "cancelled", "neutral", "skipped", "timed_out"):
            world = build_world()
            later_run(world, conclusion=conclusion)
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(MbError, "newest exact Build"):
                self.call(world)

    def test_newest_pending_run_returns_nothing_from_the_listing_alone(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            world = build_world()
            later_run(world, status=status, conclusion=None)
            with self.subTest(status=status):
                self.assertIsNone(self.call(world))
                self.assertEqual(world.api.request_count, 5)  # source 3, tested commit, run listing

    def test_malformed_pending_states_fail_closed(self):
        for changes in ({"status": "unknown", "conclusion": None}, {"status": "queued", "conclusion": "success"},
                        {"status": None, "conclusion": None}):
            world = build_world()
            later_run(world, **changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(MbError, "pending"):
                self.call(world)

    def test_order_uses_created_at_then_run_id_not_attempt_or_result(self):
        world = build_world()
        later_run(world, created_at=world.runs[42]["created_at"], run_attempt=1)
        with self.assertRaisesRegex(MbError, "newest exact Build"):
            self.call(world)
        world = build_world()
        later_run(world, run_id=41, created_at="2026-10-07T09:00:00Z", run_attempt=9)
        self.assertEqual(self.call(world), world.bundle)

    def test_newest_draft_deferral_is_not_a_producer_and_is_never_replaced_by_an_older_build(self):
        world = build_world()
        later_run(world, listing="build-deferred", conclusion="success")
        self.assertIsNone(self.call(world))
        world = World()
        world.add_run("build", "build-deferred")
        self.assertIsNone(self.call(world))
        self.assertEqual(world.api.request_count, 7)  # source 4, listing, run, jobs

    def test_other_graphs_of_the_newest_run_are_rejections_not_deferrals(self):
        for listing in ("build-reuse", "build-attest-only", "build-full-extra-job", "build-full-missing-job",
                        "build-full-duplicated-job", "build-full-wrong-conclusion", "packaged-deferred"):
            world = build_world()
            later_run(world, listing=listing, conclusion="success")
            with self.subTest(listing=listing), self.assertRaises(MbError):
                self.call(world)

    def test_absent_generation_returns_none_without_compiler_or_dispatch(self):
        world = World()
        self.assertIsNone(self.call(world))
        self.assertEqual(world.api.mutations, [])

    def test_missing_duplicate_expired_and_wrong_owner_bundle_reject_without_fallback(self):
        for mutation in ("missing", "duplicate", "expired", "head"):
            world = build_world()
            if mutation == "missing":
                world.set_artifact(100, name=grammar.ci_artifact_name("build", 42, 1))
            elif mutation == "duplicate":
                world.api.add_artifact({**world.records[100], "id": 105}, b"duplicate")
            elif mutation == "expired":
                world.set_artifact(100, expired=True)
            else:
                world.set_artifact(100, workflow_run={"head_sha": "2" * 40})
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(world)

    def test_another_controller_commit_or_kit_pin_is_a_rejection(self):
        for index, message in ((0, "admitted controller commit"), (1, "pinned kit")):
            world = build_world()
            entry = world.runs[42]["referenced_workflows"][index]
            entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)
            world.api.add_run(world.runs[42])
            with self.subTest(index=index), self.assertRaisesRegex(MbError, message):
                self.call(world)

    def test_newer_attempt_than_the_listing_shows_is_the_one_that_counts(self):
        world = build_world()
        world.api.during_listing(LISTING, lambda: world.set_run(42, run_attempt=3, status="queued", conclusion=None))
        self.assertIsNone(self.call(world))
        world = build_world()
        world.api.during_listing(LISTING, lambda: world.set_run(42, run_attempt=3))
        with self.assertRaises(MbError):  # attempt 3 has no jobs: nothing of attempt 2 is reused
            self.call(world)

    def test_a_failed_jobs_only_rerun_is_refused_and_a_rerun_of_all_jobs_selected(self):
        world = build_world()
        world.runs[42] = ci_failed_jobs_rerun(world.api, world.runs[42], world.jobs[42])
        with self.assertRaisesRegex(MbError, "Authenticate the pinned kit' started before attempt 2 did, in an "
                                             "earlier attempt: a failed-jobs-only rerun mixes attempts; rerun all jobs"):
            self.call(world)
        # Attempt 2 of a rerun of all jobs: its first job starts in the second the attempt does.
        world = build_world(run_started_at="2026-10-07T10:00:05Z")
        world.api.add_run(world.runs[42], attempts=[{**world.runs[42], "run_attempt": 1, "conclusion": "cancelled",
                                                     "run_started_at": "2026-10-07T09:00:00Z"}])
        self.assertEqual(self.call(world), world.bundle)

    def test_live_source_and_non_pr_request_are_independent_requirements(self):
        world = build_world()
        world.api.set_branch("master", "f" * 40, "e" * 40)
        with self.assertRaises(MbError):
            self.call(world)
        world = build_world()
        world.api.add_response("/repos/example/mod/pulls/7", {**world.pr, "draft": True})
        with self.assertRaisesRegex(MbError, "ready"):
            self.call(world)
        world = build_world(push=True)
        with patch.object(world.api, "get_json") as get, self.assertRaisesRegex(MbError, "original PR plan"):
            self.call(world)
        get.assert_not_called()

    def test_api_failure_is_never_absence(self):
        world = build_world()
        with patch.object(world.api, "get_json", side_effect=MbError("API unavailable")), \
                self.assertRaisesRegex(MbError, "API unavailable"):
            self.call(world)

    def test_step_times_in_any_api_shape_are_stored_as_whole_seconds(self):
        world = build_world()
        upload = world.job(42, ASSEMBLE)["steps"][3]
        upload.update(started_at="2026-10-07T03:02:30.250-07:00", completed_at="2026-10-07T10:03:30.999Z")
        world.set_jobs(42, world.jobs[42])
        world.set_artifact(100, created_at="2026-10-07T10:03:00.5Z")
        self.assertEqual(self.call(world), world.bundle)


class PrBuildWaitTests(unittest.TestCase):
    def wait(self, world, *, monotonic, sleep):
        return wait_for_latest_pr_build(world.api, plan=world.plan, monotonic=monotonic, sleep=sleep)

    def test_completed_build_returns_without_sleep(self):
        sleep = Mock()
        for origin in (100, 1 << 4096):
            world = build_world()
            with self.subTest(origin_bits=origin.bit_length()):
                self.assertEqual(self.wait(world, monotonic=lambda: origin, sleep=sleep), world.bundle)
                self.assertEqual(world.api.request_count, 9)
        sleep.assert_not_called()

    def test_one_pending_poll_costs_one_request_after_the_first(self):
        world = build_world()
        world.set_run(42, status="queued", conclusion=None)
        elapsed, counts = [0], []

        def sleep(seconds):
            counts.append(world.api.request_count)
            elapsed[0] += seconds
            if len(counts) == 4:
                world.set_run(42, status="completed", conclusion="success")

        self.assertEqual(self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep), world.bundle)
        # First poll: the pull request (3), its tested commit and the run listing. Then the listing only.
        self.assertEqual(counts, [5, 6, 7, 8])
        # The poll that finds the Build: listing, run, jobs, bundle listing and record, and the pull request again.
        self.assertEqual(world.api.request_count - counts[-1], 8)
        self.assertEqual(elapsed[0], 4 * limits.CI_BUILD_POLL_SECONDS)
        self.assertEqual(world.api.mutations, [])

    def test_a_whole_wait_stays_within_the_request_budget(self):
        world = World()
        elapsed = [0]

        def sleep(seconds):
            elapsed[0] += seconds

        with self.assertRaisesRegex(MbError, "rerun complete Build and E2E"):
            self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(elapsed[0], 5400)
        self.assertEqual(world.api.request_count, 4 + 90)

    def test_deferred_newest_run_keeps_the_wait_open_until_the_ready_build_exists(self):
        world = World()
        world.add_run("build", "build-deferred")
        elapsed = [0]

        def sleep(seconds):
            elapsed[0] += seconds
            if elapsed[0] == 120:
                ready = build_world()
                run = {**ready.runs[42], "id": 44, "created_at": "2026-10-07T11:00:00Z"}
                world.api.add_run(run)
                jobs = ready.jobs[42]
                for job in jobs:
                    job["run_id"] = 44
                world.api.add_jobs(44, 2, jobs)

        with self.assertRaises(MbError):  # the ready run exists but has uploaded no bundle
            self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(elapsed[0], 120)

    def test_absent_build_waits_to_deadline_without_late_observation(self):
        world = World()
        elapsed, sleeps = [0], []

        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += seconds

        with self.assertRaisesRegex(MbError, "rerun complete Build and E2E"):
            self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(sleeps, [60] * 90)
        self.assertEqual(world.api.mutations, [])

    def test_slow_api_success_at_deadline_never_admits_and_sleep_is_clipped(self):
        for slow_success in (False, True):
            world = build_world()
            if not slow_success:
                world.set_run(42, status="queued", conclusion=None)
            elapsed, sleeps = [0], []
            original = world.api.get_json

            def get(path, **kwargs):
                if path == LISTING and not sleeps:
                    elapsed[0] = 5400 if slow_success else 5390
                return original(path, **kwargs)

            def sleep(seconds):
                sleeps.append(seconds)
                elapsed[0] += seconds

            with self.subTest(slow_success=slow_success), patch.object(world.api, "get_json", side_effect=get):
                with self.assertRaisesRegex(MbError, "wait exhausted"):
                    self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep)
            self.assertEqual(sleeps, [] if slow_success else [10])

    def test_pending_then_failure_rejects_at_next_poll_and_moved_source_when_the_build_is_found(self):
        for change in ("failure", "source"):
            world = build_world()
            world.set_run(42, status="queued", conclusion=None)
            elapsed = [0]

            def sleep(seconds):
                elapsed[0] += seconds
                if change == "source":
                    world.api.set_branch("master", "f" * 40, "e" * 40)
                    world.set_run(42, status="completed", conclusion="success")
                else:
                    world.set_run(42, status="completed", conclusion="failure")

            with self.subTest(change=change), self.assertRaises(MbError):
                self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep)
            self.assertEqual(elapsed[0], 60)

    def test_api_failure_and_sleep_error_never_become_absence(self):
        world = build_world()
        sleep = Mock()
        with patch.object(world.api, "get_json", side_effect=MbError("API unavailable")):
            with self.assertRaisesRegex(MbError, "API unavailable"):
                self.wait(world, monotonic=lambda: 0, sleep=sleep)
        sleep.assert_not_called()
        world.set_run(42, status="queued", conclusion=None)
        with self.assertRaisesRegex(MbError, "wait interrupted"):
            self.wait(world, monotonic=lambda: 0, sleep=Mock(side_effect=OSError("interrupted")))

    def test_clock_rejection_and_observation_bound(self):
        world = build_world()
        for values in ([float("nan")], [float("inf")], [True], [2, 1]):
            clock = iter(values)
            with self.subTest(values=values), self.assertRaisesRegex(MbError, "clock"):
                self.wait(world, monotonic=lambda: next(clock), sleep=lambda _: None)
        world.set_run(42, status="queued", conclusion=None)
        sleeps = []
        with self.assertRaisesRegex(MbError, "observation budget"):
            self.wait(world, monotonic=lambda: 0, sleep=sleeps.append)
        self.assertEqual(len(sleeps), limits.MAX_CI_BUILD_POLLS)

    def test_cancellation_propagates_and_plan_cannot_be_replaced_between_polls(self):
        world = build_world()
        world.set_run(42, status="queued", conclusion=None)
        with self.assertRaises(KeyboardInterrupt):
            self.wait(world, monotonic=lambda: 0, sleep=Mock(side_effect=KeyboardInterrupt))
        elapsed, expected = [0], copy.deepcopy(world.bundle)

        def sleep(seconds):
            elapsed[0] += seconds
            world.plan.clear()
            world.set_run(42, status="completed", conclusion="success")

        self.assertEqual(self.wait(world, monotonic=lambda: elapsed[0], sleep=sleep), expected)

    def test_consumption_recheck_accepts_only_unchanged_newest_descriptor(self):
        for change in (None, "pending", "failure", "metadata", "descriptor", "deferred"):
            world = build_world()
            if change in ("pending", "failure"):
                later_run(world, status="queued" if change == "pending" else "completed",
                          conclusion=None if change == "pending" else "failure")
            elif change == "deferred":
                later_run(world, listing="build-deferred", conclusion="success")
            elif change == "metadata":
                world.set_artifact(100, digest="sha256:" + "f" * 64)
            elif change == "descriptor":
                world.bundle["artifact"]["id"] += 1

            def revalidate():
                return revalidate_latest_pr_build(world.api, descriptor=world.bundle, plan=world.plan)

            with self.subTest(change=change):
                if change is None:
                    self.assertIsNone(revalidate())
                    self.assertEqual(world.api.request_count, 9)
                else:
                    with self.assertRaises(MbError):
                        revalidate()
            self.assertEqual(world.api.mutations, [])

    def test_plan_of_another_generation_selects_nothing_it_did_not_admit(self):
        world = build_world()
        world.plan["identity"]["inventory_sha256"] = "f" * 64
        world.plan["plan_sha256"] = plan_sha256(world.plan)
        observed = select_latest_pr_build(world.api, plan=world.plan)
        # The run is the same; what was built is bound by the envelope inside the bundle, read on download.
        self.assertEqual(observed["plan_sha256"], world.plan["plan_sha256"])
        self.assertNotEqual(observed, world.bundle)


if __name__ == "__main__":
    unittest.main()
