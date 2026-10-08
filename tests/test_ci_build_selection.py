"""Newest exact PR generation is chosen without any success filter or older fallback."""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.selection import revalidate_latest_pr_build, select_latest_pr_build, wait_for_latest_pr_build
from mod_base.build_ci.protocol import plan_sha256
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model import limits
from tests.test_ci_transport import transport_fixture


def selection_fixture():
    plan, api, descriptor, _, run, artifact, _ = transport_fixture()
    run["display_title"] = grammar.ci_pr_build_title(profile=plan["profile"],
        **{key: plan["identity"][key] for key in ("pr_number", "head_sha", "base_sha", "tested_sha")})
    api.add_run(run)
    return plan, api, descriptor, run, artifact


def later_run(fixture, *, status="completed", conclusion="failure", **changes):
    run = copy.deepcopy(fixture[3])
    run.update(id=44, created_at="2026-10-07T11:00:00Z", status=status, conclusion=conclusion)
    run.update(changes)
    fixture[1].add_run(run)
    return run


class PrBuildSelectionTests(unittest.TestCase):
    def call(self, fixture):
        return select_latest_pr_build(fixture[1], plan=fixture[0], workflow_path=".github/workflows/build-gate.yml")

    def test_successful_latest_bundle_retains_exact_descriptor_and_read_only_api(self):
        fixture = selection_fixture()
        observed = self.call(fixture)
        self.assertEqual(observed, fixture[2])
        self.assertEqual(fixture[1].mutations, [])
        fixture[0]["identity"]["head_sha"] = "f" * 40
        self.assertEqual(observed["identity"]["head_sha"], "1" * 40)

    def test_newest_failed_cancelled_neutral_and_skipped_runs_never_use_old_success(self):
        for conclusion in ("failure", "cancelled", "neutral", "skipped", "timed_out"):
            fixture = selection_fixture()
            later_run(fixture, conclusion=conclusion)
            with self.subTest(conclusion=conclusion), self.assertRaisesRegex(MbError, "newest exact Build"):
                self.call(fixture)

    def test_newest_pending_run_returns_no_bundle_and_reads_no_jobs_or_artifacts(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            fixture = selection_fixture()
            later_run(fixture, status=status, conclusion=None)
            with self.subTest(status=status), patch.object(fixture[1], "paginate", wraps=fixture[1].paginate) as pages:
                self.assertIsNone(self.call(fixture))
            self.assertEqual(pages.call_count, 0)

    def test_unknown_titles_profiles_and_malformed_pending_states_fail_closed(self):
        for changes in ({"display_title": "legacy-or-candidate-title"}, {"display_title": None},
                        {"display_title": selection_fixture()[3]["display_title"].replace("block-pops", "quick-skin")},
                        {"status": "unknown", "conclusion": None}, {"status": "queued", "conclusion": "success"}):
            fixture = selection_fixture()
            later_run(fixture, **changes)
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.call(fixture)

    def test_other_pr_or_head_base_tested_tuple_is_not_a_matching_generation(self):
        for key, value in (("pr_number", 8), ("head_sha", "f" * 40), ("base_sha", "f" * 40), ("tested_sha", "f" * 40)):
            fixture = selection_fixture()
            context = {key: fixture[0]["identity"][key] for key in ("pr_number", "head_sha", "base_sha", "tested_sha")}
            context[key] = value
            later_run(fixture, display_title=grammar.ci_pr_build_title(profile=fixture[0]["profile"], **context))
            with self.subTest(key=key):
                self.assertEqual(self.call(fixture), fixture[2])

    def test_order_uses_created_at_then_run_id_not_attempt_or_result(self):
        fixture = selection_fixture()
        later_run(fixture, created_at=fixture[3]["created_at"])
        with self.assertRaisesRegex(MbError, "newest exact Build"):
            self.call(fixture)

    def test_queries_never_apply_status_filter_and_artifact_name_is_exact(self):
        fixture = selection_fixture()
        with patch.object(fixture[1], "get_json", wraps=fixture[1].get_json) as get:
            self.call(fixture)
        listing = [call for call in get.call_args_list if "/actions/workflows/" in call.args[0]]
        self.assertEqual(len(listing), 2)
        for call in listing:
            params = call.kwargs["params"]
            self.assertNotIn("status", params)
            self.assertEqual(params["head_sha"], fixture[0]["identity"]["controller_sha"])
            self.assertEqual(params["event"], "pull_request_target")
        artifacts = [call for call in get.call_args_list if call.args[0].endswith("/42/artifacts")]
        self.assertEqual(artifacts[0].kwargs["params"]["name"], fixture[2]["artifact"]["name"])

    def test_absent_generation_returns_none_without_compiler_or_dispatch(self):
        fixture = selection_fixture()
        fixture[3]["display_title"] = fixture[3]["display_title"].replace("pr=7", "pr=8")
        fixture[1].add_run(fixture[3])
        self.assertIsNone(self.call(fixture))
        self.assertEqual(fixture[1].mutations, [])

    def test_missing_duplicate_expired_and_wrong_owner_bundle_reject_without_fallback(self):
        for mutation in ("missing", "duplicate", "expired", "owner"):
            fixture = selection_fixture()
            if mutation == "missing":
                fixture[4]["name"] = grammar.ci_artifact_name("build", 42, 1)
            elif mutation == "duplicate":
                fixture[1].add_artifact({**fixture[4], "id": 101}, b"duplicate")
            elif mutation == "expired":
                fixture[4]["expired"] = True
            else:
                fixture[4]["workflow_run"]["head_sha"] = "f" * 40
            if mutation != "duplicate":
                fixture[1].add_artifact(fixture[4], b"unused")
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(fixture)

    def test_same_head_ready_pr_cannot_use_latest_deferred_graph(self):
        fixture = selection_fixture()
        fixture[1].add_jobs(42, 2, [{"name": "Deferred draft Build", "status": "completed", "conclusion": "success"}])
        with self.assertRaisesRegex(MbError, "graph mismatch"):
            self.call(fixture)

    def test_newer_run_during_selection_and_newer_attempt_after_listing_reject(self):
        for mutation in ("run", "attempt"):
            fixture = selection_fixture()
            if mutation == "run":
                fixture[1].during_listing("/repos/example/mod/actions/runs/42/artifacts",
                                           lambda: later_run(fixture, status="queued", conclusion=None))
            else:
                run = {**fixture[3], "run_attempt": 3}
                fixture[1].during_listing("/repos/example/mod/actions/workflows/build-gate.yml/runs",
                                           lambda: fixture[1].add_run(run))
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                self.call(fixture)

    def test_live_source_and_non_pr_request_are_independent_requirements(self):
        fixture = selection_fixture()
        fixture[1].set_branch("master", "f" * 40, "e" * 40)
        with self.assertRaises(MbError):
            self.call(fixture)
        fixture = selection_fixture()
        identity = fixture[0]["identity"]
        identity.update(pr_number=0, head_sha=identity["tested_sha"], head_branch="master")
        fixture[0]["plan_sha256"] = plan_sha256(fixture[0])
        with patch.object(fixture[1], "get_json") as get, self.assertRaises(MbError):
            self.call(fixture)
        get.assert_not_called()

    def test_latest_kit_graph_and_api_failures_are_fatal(self):
        for mutation in ("kit", "graph", "api"):
            fixture = selection_fixture()
            if mutation == "kit":
                fixture[3]["referenced_workflows"][0]["sha"] = "f" * 40
                fixture[1].add_run(fixture[3])
            elif mutation == "graph":
                fixture[1].add_jobs(42, 2, [{"name": "Unexpected", "status": "completed", "conclusion": "success"}])
            with self.subTest(mutation=mutation), self.assertRaises(MbError):
                if mutation == "api":
                    with patch.object(fixture[1], "get_json", side_effect=MbError("API unavailable")):
                        self.call(fixture)
                else:
                    self.call(fixture)

    def test_workflow_id_drift_between_listing_and_run_read_rejects(self):
        fixture = selection_fixture()
        run = {**fixture[3], "workflow_id": 999}
        fixture[1].during_listing("/repos/example/mod/actions/workflows/build-gate.yml/runs",
                                   lambda: fixture[1].add_run(run))
        with self.assertRaisesRegex(MbError, "workflow id"):
            self.call(fixture)


class PrBuildWaitTests(unittest.TestCase):
    def wait(self, fixture, *, monotonic, sleep):
        return wait_for_latest_pr_build(fixture[1], plan=fixture[0],
            workflow_path=".github/workflows/build-gate.yml", monotonic=monotonic, sleep=sleep)

    def test_completed_build_returns_without_sleep(self):
        fixture = selection_fixture()
        sleep = unittest.mock.Mock()
        for origin in (100, 1 << 4096):
            with self.subTest(origin_bits=origin.bit_length()):
                self.assertEqual(self.wait(fixture, monotonic=lambda: origin, sleep=sleep), fixture[2])
        sleep.assert_not_called()

    def test_pending_then_success_reauthenticates_without_independent_compilation(self):
        fixture = selection_fixture()
        success = copy.deepcopy(fixture[3])
        fixture[1].add_run({**success, "status": "queued", "conclusion": None})
        elapsed = [0]
        def sleep(seconds):
            elapsed[0] += seconds
            fixture[1].add_run(success)
        self.assertEqual(self.wait(fixture, monotonic=lambda: elapsed[0], sleep=sleep), fixture[2])
        self.assertEqual(elapsed[0], limits.CI_BUILD_POLL_SECONDS)
        self.assertEqual(fixture[1].mutations, [])

    def test_absent_build_waits_to_deadline_without_late_observation(self):
        fixture = selection_fixture()
        fixture[3]["display_title"] = fixture[3]["display_title"].replace("pr=7", "pr=8")
        fixture[1].add_run(fixture[3])
        elapsed, sleeps = [0], []
        def sleep(seconds):
            sleeps.append(seconds)
            elapsed[0] += seconds
        with patch("mod_base.build_ci.selection.select_latest_pr_build", wraps=select_latest_pr_build) as select:
            with self.assertRaisesRegex(MbError, "rerun complete Build and E2E"):
                self.wait(fixture, monotonic=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(elapsed[0], 5400)
        self.assertEqual(select.call_count, 90)
        self.assertEqual(sleeps, [60] * 90)
        self.assertEqual(fixture[1].mutations, [])

    def test_slow_api_success_at_deadline_never_admits_and_sleep_is_clipped(self):
        for slow_success in (False, True):
            fixture = selection_fixture()
            elapsed, sleeps = [0], []
            def select(*args, **kwargs):
                elapsed[0] += 5400 if slow_success else 5390
                return select_latest_pr_build(*args, **kwargs) if slow_success else None
            def sleep(seconds):
                sleeps.append(seconds)
                elapsed[0] += seconds
            with self.subTest(slow_success=slow_success), patch(
                    "mod_base.build_ci.selection.select_latest_pr_build", side_effect=select) as observed:
                with self.assertRaisesRegex(MbError, "wait exhausted"):
                    self.wait(fixture, monotonic=lambda: elapsed[0], sleep=sleep)
            self.assertEqual(observed.call_count, 1)
            self.assertEqual(sleeps, [] if slow_success else [10])

    def test_pending_then_failure_or_moved_source_rejects_at_next_poll(self):
        for change in ("failure", "source"):
            fixture = selection_fixture()
            fixture[1].add_run({**fixture[3], "status": "queued", "conclusion": None})
            elapsed = [0]
            def sleep(seconds):
                elapsed[0] += seconds
                if change == "source":
                    fixture[1].set_branch("master", "f" * 40, "e" * 40)
                else:
                    fixture[1].add_run({**fixture[3], "conclusion": "failure"})
            with self.subTest(change=change), self.assertRaises(MbError):
                self.wait(fixture, monotonic=lambda: elapsed[0], sleep=sleep)
            self.assertEqual(elapsed[0], 60)

    def test_api_failure_and_sleep_error_never_become_absence(self):
        fixture = selection_fixture()
        sleep = unittest.mock.Mock()
        with patch.object(fixture[1], "get_json", side_effect=MbError("API unavailable")):
            with self.assertRaisesRegex(MbError, "API unavailable"):
                self.wait(fixture, monotonic=lambda: 0, sleep=sleep)
        sleep.assert_not_called()
        fixture[1].add_run({**fixture[3], "status": "queued", "conclusion": None})
        with self.assertRaisesRegex(MbError, "wait interrupted"):
            self.wait(fixture, monotonic=lambda: 0, sleep=unittest.mock.Mock(side_effect=OSError("interrupted")))

    def test_clock_rejection_and_observation_bound(self):
        fixture = selection_fixture()
        for values in ([float("nan")], [float("inf")], [True], [2, 1]):
            clock = iter(values)
            with self.subTest(values=values), self.assertRaisesRegex(MbError, "clock"):
                self.wait(fixture, monotonic=lambda: next(clock), sleep=lambda _: None)
        fixture[1].add_run({**fixture[3], "status": "queued", "conclusion": None})
        with patch("mod_base.build_ci.selection.select_latest_pr_build", wraps=select_latest_pr_build) as select:
            with self.assertRaisesRegex(MbError, "observation budget"):
                self.wait(fixture, monotonic=lambda: 0, sleep=lambda _: None)
        self.assertEqual(select.call_count, limits.MAX_CI_BUILD_POLLS)

    def test_cancellation_propagates_and_plan_cannot_be_replaced_between_polls(self):
        fixture = selection_fixture()
        fixture[1].add_run({**fixture[3], "status": "queued", "conclusion": None})
        with self.assertRaises(KeyboardInterrupt):
            self.wait(fixture, monotonic=lambda: 0,
                      sleep=unittest.mock.Mock(side_effect=KeyboardInterrupt))
        elapsed = [0]
        def sleep(seconds):
            elapsed[0] += seconds
            fixture[0].clear()
            fixture[1].add_run(fixture[3])
        self.assertEqual(self.wait(fixture, monotonic=lambda: elapsed[0], sleep=sleep), fixture[2])

    def test_consumption_recheck_accepts_only_unchanged_newest_descriptor(self):
        for change in (None, "pending", "failure", "metadata", "descriptor"):
            fixture = selection_fixture()
            if change in ("pending", "failure"):
                later_run(fixture, status="queued" if change == "pending" else "completed",
                          conclusion=None if change == "pending" else "failure")
            elif change == "metadata":
                fixture[4]["digest"] = "sha256:" + "f" * 64
                fixture[1].add_artifact(fixture[4], b"unused")
            elif change == "descriptor":
                fixture[2]["artifact"]["id"] += 1
            def revalidate():
                return revalidate_latest_pr_build(fixture[1], descriptor=fixture[2], plan=fixture[0],
                    workflow_path=".github/workflows/build-gate.yml")
            with self.subTest(change=change):
                if change is None:
                    self.assertIsNone(revalidate())
                else:
                    with self.assertRaises(MbError):
                        revalidate()
            self.assertEqual(fixture[1].mutations, [])


class PrBuildTitleTests(unittest.TestCase):
    def test_round_trip_and_strict_rejection(self):
        fixture = selection_fixture()
        title = fixture[3]["display_title"]
        marker = grammar.parse_ci_pr_build_title(title)
        self.assertEqual(marker.pr_number, 7)
        self.assertEqual(marker.tested_sha, fixture[0]["identity"]["tested_sha"])
        for bad in (title + "\n", title + " extra", title.replace("pr=7", "pr=07"),
                    title.replace("pr=7", "pr=0"), title.replace("pr=7", "pr=9999999999999999999"),
                    title.replace("profile=block-pops", "profile=unknown"), title.replace("head=", "head=F"),
                    title.replace("build-v1", "build-v2"), None):
            with self.subTest(bad=bad):
                self.assertIsNone(grammar.parse_ci_pr_build_title(bad))

    def test_builder_rejects_boolean_zero_unknown_profile_and_bad_sha(self):
        args = {"profile": "block-pops", "pr_number": 7, "head_sha": "1" * 40, "base_sha": "2" * 40, "tested_sha": "3" * 40}
        for changes in ({"pr_number": True}, {"pr_number": 0}, {"profile": "unknown"}, {"tested_sha": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                grammar.ci_pr_build_title(**{**args, **changes})
