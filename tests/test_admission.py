"""``admit`` and ``decide`` (MB5, SPEC §5.3.1).

Ports QS ``test_pages_publication_progress`` (the ``decide`` table with the identical constants,
the atomic published owner, lost same-head replacement wakes, ordinary replacement, the bounded
failed-publication budget, exact-attempt upload windows, stale and foreign handoffs) and the QS
``wake``/``discover`` semantics (stale-wake exit 0, deferral on active source runs, the all-current
exit, the v1 cutover) onto v1 artifact names and prefix-aware job names, against ``FakeGitHub``
and the qs-like (``progress``, families) and bp-like (``always``, enrolled branches) fixture mods.
The QS recorded-readiness replay (``fixtures/pages-progress-reference.json``) stays with Quick
Skin: it depends on QS's own matrix history, not on the kit's policy.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.github.api import ApiError, InconsistentListing
from mod_base.io.bounded_zip import artifact_limit
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.pages import admission, commands_control
from mod_base.pages.admission import Admission, Decision, ProgressPolicy, WakeInputs, admit, decide
from mod_base.pages.select import family_archive_limit
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, caller_job_name, step_name
from tests.fixtures.mods import support
from tests.test_select import (
    BP_HANDOFF_JOB,
    BP_HANDOFF_STEP,
    FAMILY,
    FAMILY_JOB,
    FAMILY_STEP,
    FAMILY_WORKFLOW,
    QS_HANDOFF_JOB,
    QS_HANDOFF_STEP,
    World,
    epoch,
)

LEGS = {f"{FAMILY}/mc1.{index}" for index in range(16)}
RELEASE = "release/1.21.1"
NOW = 10_000


class PublicationPolicyTest(unittest.TestCase):
    """The QS ``decide`` table (identical constants and reasons)."""

    def decision(self, published=(), ready=(), at=5000, published_at=2000, **kwargs: Any) -> Decision:
        return decide(expected=LEGS, published={key: 2000 for key in published},
                      ready={key: (2000 if key in published else 3000) for key in set(ready) | set(published)},
                      ordinary_ready=True, published_at=published_at, now=at, **kwargs)

    def test_constants_are_quick_skins(self) -> None:
        self.assertEqual((admission.COALESCE_SECONDS, admission.PARTIAL_DEADLINE_SECONDS,
                          admission.RECOVERY_INTERVAL_SECONDS, admission.MAX_REQUESTS, admission.MAX_CANDIDATES,
                          admission.MAX_FAILED_PUBLICATIONS), (600, 2700, 3600, 160, 8, 3))
        self.assertEqual(ProgressPolicy(), ProgressPolicy(600, 2700, 3600, 3))

    def test_initial_half_and_immediate_final_are_separate_milestones(self) -> None:
        self.assertEqual("initial-ordinary", self.decision(published_at=None).reason)
        half = set(sorted(LEGS)[:math.ceil(len(LEGS) / 2)])
        self.assertEqual("coalescing", self.decision(ready=half, at=3599).reason)
        self.assertEqual(3600, self.decision(ready=half, at=3599).next_check_at)
        self.assertEqual("half-coverage", self.decision(ready=half, at=3600).reason)
        self.assertEqual("final-complete", self.decision(published=half, ready=LEGS, at=3000).reason)

    def test_stalled_partial_deadline_and_scheduler_interval_are_deterministic(self) -> None:
        ready = {next(iter(LEGS))}
        deadline = 3000 + admission.PARTIAL_DEADLINE_SECONDS
        self.assertFalse(self.decision(ready=ready, at=deadline - 1).eligible)
        self.assertEqual(deadline, self.decision(ready=ready, at=deadline - 1).next_check_at)
        for offset in (0, admission.RECOVERY_INTERVAL_SECONDS):
            self.assertEqual("partial-deadline", self.decision(ready=ready, at=deadline + offset).reason)

    def test_final_during_build_queues_as_soon_as_publisher_is_available(self) -> None:
        active = self.decision(ready=LEGS, publisher_available=False)
        self.assertEqual(("publisher-active", 5000), (active.reason, active.next_check_at))
        self.assertEqual("final-complete", self.decision(ready=LEGS, at=5001).reason)

    def test_cancelled_publish_does_not_advance_coverage_and_retry_is_bounded(self) -> None:
        for failures in range(admission.MAX_FAILED_PUBLICATIONS):
            self.assertTrue(self.decision(ready=LEGS, failed_publications=failures).eligible)
        self.assertEqual("publication-recovery-budget-exhausted", self.decision(
            ready=LEGS, failed_publications=admission.MAX_FAILED_PUBLICATIONS).reason)

    def test_complete_unchanged_pending_and_replacement(self) -> None:
        self.assertEqual("complete", self.decision(published=LEGS, ready=LEGS).reason)
        self.assertEqual("unchanged", self.decision(published={next(iter(LEGS))}).reason)
        self.assertEqual("ordinary-replacement", self.decision(published=LEGS, ready=LEGS,
                                                               ordinary_changed=True).reason)
        pending = decide(expected=LEGS, published={}, ready={}, ordinary_ready=False, published_at=None, now=1)
        self.assertEqual(Decision(False, "ordinary-handoffs-pending", 0, 0), pending)

    def test_foreign_future_or_regressing_progress_is_rejected(self) -> None:
        leg = next(iter(LEGS))
        for fields in ({"ready": {"foreign": 2}}, {"ready": {leg: 6001}}, {"published": {leg: 2}},
                       {"now": float("nan")}, {"now": True}, {"published_at": 7000}, {"expected": set()},
                       {"failed_publications": -1}, {"failed_publications": True}, {"ready": {leg: float("inf")}},
                       {"policy": None}):
            arguments: dict[str, Any] = dict(expected=LEGS, published={}, ready={}, ordinary_ready=True,
                                             published_at=2000, now=6000)
            arguments.update(fields)
            with self.subTest(fields=list(fields)), self.assertRaises(MbError):
                decide(**arguments)

    def test_a_configured_policy_moves_every_threshold(self) -> None:
        config = {"mode": "progress", "coalesce_seconds": 60, "partial_deadline_seconds": 120,
                  "recovery_interval_seconds": 900, "max_failed_publications": 1, "defer_on_active_source_runs": True}
        policy = ProgressPolicy.from_config(config)
        self.assertEqual(policy, ProgressPolicy(60, 120, 900, 1))
        ready = {next(iter(LEGS))}
        self.assertEqual("partial-deadline", self.decision(ready=ready, at=3120, policy=policy).reason)
        self.assertEqual("publication-recovery-budget-exhausted",
                         self.decision(ready=LEGS, failed_publications=1, policy=policy).reason)
        for bad in ({"mode": "always", "defer_on_active_source_runs": False}, {**config, "coalesce_seconds": 121},
                    {**config, "max_failed_publications": 0}, {**config, "coalesce_seconds": True}):
            with self.subTest(bad=bad), self.assertRaises(MbError):
                ProgressPolicy.from_config(bad)


class QuickSkinWorld:
    """A materialized qs-like repository (progress admission, one family) and its fake GitHub."""

    def __init__(self, test: unittest.TestCase, *, max_requests: int | None = None) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-admission-")).resolve()
        test.addCleanup(shutil.rmtree, self.directory, True)
        self.mod = support.materialize("qs_like", self.directory / "repo")
        self.head = self.mod.commit
        self.keys = list(support.QS_KEYS)
        self.world = World(self.mod.repository, max_requests=max_requests)
        self.api = self.world.api
        self.api.set_branch("master", self.mod.commit, self.mod.tree)
        self.sleeps: list[float] = []

    def invocation(self, config: Path | None = None):
        environ = support.environment(self.mod, run_id=9000, job="admit", workflow=PAGES_WORKFLOW_PATH,
                                      token="fixture-token")
        return build_invocation(self.mod.root, config, environ)

    def config_with(self, **admission_changes: Any) -> Path:
        data = json.loads((self.mod.root / "site/mod-base.json").read_text())
        data["admission"] = {**data["admission"], **admission_changes}
        path = self.directory / f"config-{len(list(self.directory.glob('config-*')))}.json"
        path.write_text(json.dumps(data))
        return path

    def admit(self, operation: str = "recovery", wake: WakeInputs = WakeInputs(), *, now: float = NOW,
              config: Path | None = None) -> Admission:
        return admit(self.invocation(config), api=self.api, operation=operation, wake=wake, now=epoch(now),
                     sleep=self.sleeps.append)

    def e2e(self, run_id: int = 900, *, at: float = 1000, keys: list[str] | None = None,
            size_in_bytes: int | None = None, **run: Any) -> dict[str, dict[str, Any]]:
        """A successful source run whose latest attempt handed off ``keys`` inside their jobs (each
        handoff archive ``size_in_bytes`` large when given)."""

        record = self.world.run(run_id, **{"head_sha": self.head, "created": at - 50, "updated": at + 10, **run})
        keys = self.keys if keys is None else keys
        self.world.jobs(record, [self.world.job(QS_HANDOFF_JOB.replace("{key}", key),
                                                steps=((QS_HANDOFF_STEP, at - 5, at + 5),)) for key in keys])
        size = {} if size_in_bytes is None else {"size_in_bytes": size_in_bytes}
        return {key: self.world.artifact(grammar.handoff_name(key, record["run_attempt"]), record, created=at, **size)
                for key in keys}

    def publish(self, run_id: int = 1000, *, at: float = 2000, legs: tuple[str, ...] = (),
                select_at: float = 1900, **run: Any) -> dict[str, Any]:
        """One atomic successful Pages owner at the head: every collector, deploy and refresh job
        succeeded and uploaded every ordinary cache (and the family caches of ``legs``)."""

        owner = self.world.run(run_id, **{"path": PAGES_WORKFLOW_PATH, "head_sha": self.head, "created": at - 150,
                                          "updated": at + 10, "kit_sha": support.KIT_SHA, **run})
        jobs = [self.world.job(api_job_name("publish", "build")), self.world.job(caller_job_name("deploy"))]
        for key in self.keys:
            jobs += [
                self.world.job(api_job_name("publish", "collect", key=key),
                               steps=((step_name("select"), select_at, select_at + 5),)),
                self.world.job(api_job_name("finalize", "refresh", key=key),
                               steps=((step_name("cache_upload"), at - 5, at + 5),)),
                self.world.job(api_job_name("publish", "family", family=FAMILY, key=key),
                               steps=((step_name("family_select"), select_at, select_at + 5),)),
                self.world.job(api_job_name("finalize", "refresh_family", family=FAMILY, key=key),
                               steps=((step_name("family_cache_upload"), at - 5, at + 5),)),
            ]
        self.world.jobs(owner, jobs)
        for key in self.keys:
            self.world.artifact(grammar.cache_name(key, self.head), owner, created=at)
        for key in legs:
            self.world.artifact(grammar.family_cache_name(FAMILY, key, self.head), owner, created=at)
        return owner

    def family(self, key: str, run_id: int, *, at: float = 3000, size_in_bytes: int | None = None,
               **run: Any) -> dict[str, Any]:
        """A successful producer run at the head that handed off ``key``'s family generation."""

        record = self.world.run(run_id, **{"path": FAMILY_WORKFLOW, "event": "repository_dispatch",
                                           "head_sha": self.head, "created": at - 50, "updated": at + 10, **run})
        self.world.jobs(record, [self.world.job(FAMILY_JOB, steps=((FAMILY_STEP, at - 5, at + 5),))])
        size = {} if size_in_bytes is None else {"size_in_bytes": size_in_bytes}
        return self.world.artifact(grammar.family_handoff_name(FAMILY, key, record["run_attempt"]), record, created=at,
                                   **size)

    def failed_pages(self, run_id: int, *, build: bool, at: float = 4000) -> None:
        record = self.world.run(run_id, path=PAGES_WORKFLOW_PATH, head_sha=self.head, conclusion="failure",
                                created=at, updated=at + 60)
        name = api_job_name("publish", "build") if build else api_job_name("publish", "admit")
        self.world.jobs(record, [self.world.job(name, conclusion="failure")])


class AdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.qs = QuickSkinWorld(self)
        self.host = support.InProcessHost()
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)


class QuickSkinAdmissionTest(AdmissionTest):
    def test_stale_implementation_and_foreign_default_branch(self) -> None:
        self.qs.api.set_branch("master", "e" * 40, self.qs.mod.tree)
        self.assertEqual(self.qs.admit(), Admission(eligible=False, reason="stale-implementation"))
        self.assertEqual(self.host.calls, [], "no adapter code runs for a stale implementation")
        other = QuickSkinWorld(self)
        other.api = other.world.api = type(other.world.api)(repository=other.mod.repository, default_branch="main")
        with self.assertRaises(MbError) as caught:
            other.admit()
        self.assertEqual(caught.exception.reason, "canonical-branch")

    def test_cutover_waits_for_complete_v1_evidence(self) -> None:
        self.assertEqual(self.qs.admit().reason, "awaiting-complete-v1-evidence")
        self.qs.e2e(keys=self.qs.keys[:1])
        self.assertEqual(self.qs.admit().reason, "awaiting-complete-v1-evidence")
        self.qs.e2e(901, keys=self.qs.keys[1:])
        # Every key now has a v1 handoff, but no single complete ordinary attempt exists yet.
        self.assertEqual(self.qs.admit().reason, "ordinary-handoffs-pending")

    def test_deploy_wake_admits_the_initial_ordinary_publication(self) -> None:
        handoffs = self.qs.e2e()
        result = self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head))
        self.assertEqual((result.eligible, result.reason), (True, "initial-ordinary"))
        self.assertEqual(result.bundle_keys, self.qs.keys)
        self.assertEqual(result.subjects, {key: self.qs.mod.subject for key in self.qs.keys})
        self.assertEqual(result.families, [{"family": FAMILY, "key": key, "coverage_sha": self.qs.head}
                                           for key in self.qs.keys])
        self.assertEqual(result.nominations, {key: handoffs[key]["id"] for key in self.qs.keys})
        self.assertEqual(result.heads, {"master": self.qs.head})
        self.assertEqual(self.host.calls, [("targets", False)])
        self.assertLessEqual(self.qs.api.request_count, admission.MAX_REQUESTS)

    def test_deploy_wake_polls_the_settling_source_run(self) -> None:
        self.qs.e2e()
        self.qs.world.run(900, head_sha=self.qs.head, status="in_progress", conclusion=None, created=950)

        def settle(seconds: float) -> None:
            self.qs.sleeps.append(seconds)
            if len(self.qs.sleeps) == 3:
                self.qs.world.run(900, head_sha=self.qs.head, created=950, updated=1010)

        result = admit(self.qs.invocation(), api=self.qs.api, operation="deploy",
                       wake=WakeInputs(run_id=900, sha=self.qs.head), now=epoch(NOW), sleep=settle)
        self.assertTrue(result.eligible)
        self.assertEqual(self.qs.sleeps, [2.0, 2.0, 2.0])
        self.qs.world.run(900, head_sha=self.qs.head, status="in_progress", conclusion=None, created=950)
        self.qs.sleeps.clear()
        with self.assertRaises(MbError):
            self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head))
        self.assertEqual(len(self.qs.sleeps), 29, "30 observations, 2 s apart")

    def test_a_wake_for_an_older_head_is_stale_and_exits_cleanly(self) -> None:
        old = "e" * 40
        self.qs.world.run(900, head_sha=old)
        self.assertEqual(self.qs.admit("deploy", WakeInputs(run_id=900, sha=old)).reason, "stale-wake")
        producer = self.qs.world.run(2000, path=FAMILY_WORKFLOW, event="repository_dispatch", head_sha=old)
        artifact = self.qs.world.artifact(grammar.family_handoff_name(FAMILY, "mc1.20.1", 1), producer, created=100)
        wake = WakeInputs(run_id=2000, sha=old, family=FAMILY, bundle_key="mc1.20.1", artifact_id=artifact["id"],
                          artifact_digest=artifact["digest"], coverage_sha=old)
        self.assertEqual(self.qs.admit("family", wake).reason, "stale-wake")

    def test_unauthenticated_deploy_wakes_fail_closed(self) -> None:
        cases = {
            "failed run": ({"conclusion": "failure"}, {}),
            "scheduled run": ({"event": "schedule"}, {}),
            "foreign repository": ({"repository": "fork/qs-like"}, {}),
            "another workflow id": ({"workflow_id": 78}, {}),
            "a release branch run": ({"head_branch": "release/1.21.1"}, {}),
            "another sha": ({}, {"sha": "d" * 40}),
        }
        for label, (run, wake) in cases.items():
            with self.subTest(label):
                self.setUp()
                self.qs.e2e(**run)
                with self.assertRaises(MbError) as caught:
                    self.qs.admit("deploy", WakeInputs(**{"run_id": 900, "sha": self.qs.head, **wake}))
                self.assertEqual(caught.exception.reason, "wake")

    def test_deploy_handoffs_must_be_current_targets(self) -> None:
        record = self.qs.world.run(900, head_sha=self.qs.head, attempt=2, earlier=({"run_attempt": 1},))
        self.qs.world.artifact(grammar.handoff_name("mc1.20.1", 1), record, created=100)
        with self.assertRaisesRegex(MbError, "0 keys"):
            self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head))
        self.qs.world.artifact(grammar.handoff_name("mc9.9", 2), record, created=100)
        with self.assertRaisesRegex(MbError, "not a target"):
            self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head))
        self.setUp()
        self.qs.e2e()
        expired = self.qs.world.run(901, head_sha=self.qs.head, created=2000)
        self.qs.world.artifact(grammar.handoff_name("mc1.20.1", 1), expired, created=2100, expired=True)
        with self.assertRaisesRegex(MbError, "expired"):
            self.qs.admit("deploy", WakeInputs(run_id=901, sha=self.qs.head))

    def test_wake_inputs_are_validated_per_operation(self) -> None:
        for operation, wake in (("recovery", WakeInputs(run_id=1)), ("manual", WakeInputs(sha=self.qs.head)),
                                ("deploy", WakeInputs(run_id=1)), ("deploy", WakeInputs(run_id=1, sha="x")),
                                ("deploy", WakeInputs(run_id=True, sha=self.qs.head)),
                                ("family", WakeInputs(run_id=1, sha=self.qs.head)), ("rotate", WakeInputs())):
            with self.subTest(operation=operation, wake=wake), self.assertRaises(MbError):
                self.qs.admit(operation, wake)
        self.assertEqual(self.qs.api.request_count, 0, "inputs are validated before any read")

    def test_complete_successful_owner_ends_recovery_as_current(self) -> None:
        self.qs.e2e()
        self.qs.publish(legs=tuple(self.qs.keys))
        self.assertEqual(self.qs.admit(), Admission(eligible=False, reason="current"))
        self.assertEqual(self.qs.admit("manual"), Admission(eligible=False, reason="current"))
        self.assertEqual(self.qs.api.requests("/attempts/"), [], "no job graph is read for a current site")

    def test_a_newer_family_handoff_keeps_recovery_open(self) -> None:
        self.qs.publish(legs=tuple(self.qs.keys))
        self.qs.family("mc1.20.1", 2000, at=3000)
        result = self.qs.admit()
        self.assertTrue(result.eligible)
        self.assertEqual(result.reason, "final-complete", "a lost same-head replacement wake is recovered")
        self.assertIn(f"{FAMILY}/mc1.20.1", result.nominations)

    def test_a_family_leg_is_current_only_after_a_publication_that_started_after_its_handoff(self) -> None:
        # Pages run 1000 (created at 1850) selected before the family handoffs were uploaded (1900)
        # and rolled its family caches forward afterwards (2000): those caches may carry an older
        # generation and a publication would select the handoffs (select.supersedes), so recovery
        # stays open until a publication created after the uploads.
        for index, key in enumerate(self.qs.keys):
            self.qs.family(key, 2000 + index, at=1900)
        self.qs.publish(legs=tuple(self.qs.keys), at=2000)
        self.assertNotEqual(self.qs.admit().reason, "current")
        self.qs.publish(1001, legs=tuple(self.qs.keys), at=2300)
        self.assertEqual(self.qs.admit(), Admission(eligible=False, reason="current"))

    def test_half_coverage_coalesces_then_admits(self) -> None:
        self.qs.publish()
        self.qs.family("mc1.20.1", 2000, at=3000)
        self.assertEqual(self.qs.admit(now=3599).reason, "coalescing")
        self.assertEqual(self.qs.admit(now=3610).reason, "half-coverage")
        self.qs.family("mc26.3", 2001, at=3100)
        result = self.qs.admit(now=3200)
        self.assertEqual(result.reason, "final-complete")
        self.assertEqual(set(result.nominations), {f"{FAMILY}/{key}" for key in self.qs.keys})

    def test_ordinary_replacement_and_consumed_attempts(self) -> None:
        self.qs.e2e(at=1000)
        self.qs.publish(legs=tuple(self.qs.keys), select_at=1900)
        self.assertEqual(self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head)).reason, "complete",
                         "a duplicate wake of a consumed attempt changes nothing")
        replacement = self.qs.e2e(901, at=3000)
        result = self.qs.admit("deploy", WakeInputs(run_id=901, sha=self.qs.head))
        self.assertEqual(result.reason, "ordinary-replacement")
        self.assertEqual({key: result.nominations[key] for key in self.qs.keys},
                         {key: replacement[key]["id"] for key in self.qs.keys})

    def test_partial_ordinary_replacement_does_not_supersede_a_complete_site(self) -> None:
        self.qs.publish(legs=tuple(self.qs.keys))
        self.qs.e2e(901, at=3000, keys=self.qs.keys[:1])
        # The newer handoff reopens the check, but only a complete attempt replaces the site (QS).
        self.assertEqual(self.qs.admit(), Admission(eligible=False, reason="complete"))
        self.assertEqual(self.qs.admit("deploy", WakeInputs(run_id=901, sha=self.qs.head)).reason, "complete")

    def test_a_lost_same_head_ordinary_attempt_is_recovered_by_the_schedule(self) -> None:
        # QS test_lost_same_head_ordinary_attempt_replaces_every_raw_handoff_automatically: a cache
        # is not a tombstone, so recovery (and manual) never call a superseded site current.
        self.qs.e2e(at=1000)
        self.qs.publish(legs=tuple(self.qs.keys), select_at=1900)
        self.assertEqual(self.qs.admit().reason, "current")
        replacement = self.qs.e2e(901, at=3000)
        result = self.qs.admit()
        self.assertEqual((result.eligible, result.reason), (True, "ordinary-replacement"))
        self.assertEqual({key: result.nominations[key] for key in self.qs.keys},
                         {key: replacement[key]["id"] for key in self.qs.keys})
        self.assertEqual(self.qs.admit("manual").reason, "manual")
        self.qs.publish(1001, at=4000, legs=tuple(self.qs.keys), select_at=3900)
        self.assertEqual(self.qs.admit().reason, "current", "the replacement publication is current again")

    def test_upload_window_and_attempt_bind_every_readiness(self) -> None:
        self.qs.publish()
        artifact = self.qs.family("mc1.20.1", 2000, at=3000)
        self.qs.api.add_artifact({**artifact, "created_at": "2026-09-01T12:01:40Z"}, b"PK\x05\x06" + bytes(18))
        self.assertEqual(self.qs.admit(now=9000).reason, "unchanged", "an upload outside its step is not readiness")
        self.setUp()
        self.qs.publish()
        self.qs.family("mc1.20.1", 2000, at=3000, repository="fork/qs-like")
        self.qs.family("mc26.3", 2001, at=3000, head_branch="topic")
        self.assertEqual(self.qs.admit(now=9000).reason, "current", "foreign or off-branch producers are not readiness")

    def test_the_published_owner_is_one_exact_atomic_attempt(self) -> None:
        self.qs.publish()
        self.qs.api.add_jobs(1000, 1, [self.qs.world.job(api_job_name("publish", "build"))])
        self.qs.family("mc1.20.1", 2000, at=3000)
        # Without a complete atomic owner the publication is the initial one again.
        self.assertEqual(self.qs.admit(now=9000).reason, "ordinary-handoffs-pending")
        self.setUp()
        self.qs.e2e()
        owner = self.qs.publish()
        self.qs.family("mc1.20.1", 2000, at=3000)
        self.qs.world.run(1000, **{"path": PAGES_WORKFLOW_PATH, "head_sha": self.qs.head, "created": 1850,
                                   "updated": 2010, "attempt": 2, "earlier": ({"run_attempt": 1},)})
        self.assertEqual(owner["run_attempt"], 1)
        with self.assertRaisesRegex(MbError, "attempt 2"):
            self.qs.admit(now=9000)

    def test_three_real_failed_publications_stop_automatic_retry_but_failed_wakes_do_not(self) -> None:
        for publication in (False, True):
            with self.subTest(publication=publication):
                self.setUp()
                self.qs.publish()
                for key, run_id in zip(self.qs.keys, (2000, 2001)):
                    self.qs.family(key, run_id, at=3000)
                for run_id in range(4000, 4003):
                    self.qs.failed_pages(run_id, build=publication)
                result = self.qs.admit()
                self.assertEqual(result.eligible, not publication)
                self.assertEqual(result.reason,
                                 "publication-recovery-budget-exhausted" if publication else "final-complete")
                result = self.qs.admit("manual")
                self.assertEqual((result.eligible, result.reason), (True, "manual"),
                                 "manual publication remains the operator recovery")

    def test_active_source_runs_defer_everything_but_manual(self) -> None:
        self.qs.e2e()
        self.qs.world.run(950, head_sha=self.qs.head, status="in_progress", conclusion=None, created=1500)
        self.assertEqual(self.qs.admit().reason, "deferred-active-source")
        self.assertEqual(self.qs.admit("deploy", WakeInputs(run_id=900, sha=self.qs.head)).reason,
                         "deferred-active-source")
        self.assertEqual(self.qs.admit("manual").reason, "manual")
        self.assertEqual(self.qs.admit(config=self.qs.config_with(defer_on_active_source_runs=False)).reason,
                         "initial-ordinary")
        self.qs.world.run(950, head_sha="d" * 40, status="in_progress", conclusion=None, created=1500)
        self.assertEqual(self.qs.admit().reason, "initial-ordinary", "another head is never waited for")

    def test_an_inconsistent_active_source_page_is_read_again_then_fails_closed(self) -> None:
        # Runs start and settle while the active inventory is read: a page may count a run it does
        # not list yet. The page is read again (the client's budget and backoff), never trusted.
        self.qs.e2e()
        self.qs.world.run(950, head_sha=self.qs.head, status="in_progress", conclusion=None, created=1500)
        original = self.qs.api.get_json
        skewed = {"in_progress": 1}

        def get_json(path: str, *, params: Any = None) -> Any:
            served = original(path, params=params)
            status = (params or {}).get("status")
            if path.endswith("/on-demand-e2e.yml/runs") and skewed.get(status, 0) > 0:
                skewed[status] -= 1
                served["total_count"] += 1
            return served

        with mock.patch.object(self.qs.api, "get_json", side_effect=get_json):
            self.assertEqual(self.qs.admit().reason, "deferred-active-source")
            self.assertEqual(self.qs.api.sleeps, [2.0])
            skewed["in_progress"] = lim.LISTING_READ_ATTEMPTS
            with self.assertRaisesRegex(InconsistentListing, "lists 1 of its total_count 2 runs"):
                self.qs.admit()
        self.assertEqual(self.qs.api.sleeps, [2.0, 2.0, 4.0, 8.0])
        self.assertEqual(self.qs.sleeps, [], "the admission's own run polling never waited")

    def test_a_full_active_source_page_is_confirmed_by_an_empty_second_page(self) -> None:
        # The inventory is one page of at most 100 runs; a full page reaching its total_count could
        # still hide a run behind a lagging count, so page 2 must be empty under the same count.
        self.qs.e2e()
        for index in range(100):
            self.qs.world.run(3000 + index, head_sha=self.qs.head, status="in_progress", conclusion=None,
                              created=1500 + index)
        original = self.qs.api.get_json
        pages: list[int] = []
        lagging = {"responses": 0}

        def get_json(path: str, *, params: Any = None) -> Any:
            served = original(path, params=params)
            if path.endswith("/on-demand-e2e.yml/runs") and (params or {}).get("status") == "in_progress":
                pages.append(int(params.get("page", 1)))
                if lagging["responses"] > 0:
                    lagging["responses"] -= 1
                    served["total_count"] -= 1
            return served

        with mock.patch.object(self.qs.api, "get_json", side_effect=get_json):
            self.assertEqual(self.qs.admit().reason, "deferred-active-source")
            self.assertEqual(([1, 2], []), (pages, self.qs.api.sleeps))
            # 101 active runs under a lagging total_count of 100: page 2 shows the hidden run, the
            # inventory is read again, and the fresh count is beyond the one-page bound.
            self.qs.world.run(3100, head_sha=self.qs.head, status="in_progress", conclusion=None, created=1700)
            pages.clear()
            lagging["responses"] = 2
            with self.assertRaisesRegex(MbError, "malformed, truncated or oversized"):
                self.qs.admit()
        self.assertEqual(([1, 2, 1], [2.0]), (pages, self.qs.api.sleeps))

    def test_family_wake_is_authenticated_by_id_and_reopens_a_complete_publication(self) -> None:
        self.qs.e2e()
        self.qs.publish(legs=tuple(self.qs.keys))
        artifact = self.qs.family("mc1.20.1", 2000, at=3000)
        wake = WakeInputs(run_id=2000, sha=self.qs.head, family=FAMILY, bundle_key="mc1.20.1",
                          artifact_id=artifact["id"], artifact_digest=artifact["digest"], coverage_sha=self.qs.head)
        result = self.qs.admit("family", wake)
        self.assertEqual((result.eligible, result.reason), (True, "final-complete"))
        self.assertEqual(result.nominations[f"{FAMILY}/mc1.20.1"], artifact["id"])
        for label, changes in (("digest", {"artifact_digest": "sha256:" + "f" * 64}), ("key", {"bundle_key": "mc26.3"}),
                               ("run", {"run_id": 900}), ("unknown family", {"family": "other-family"}),
                               ("not a target", {"bundle_key": "mc9.9"})):
            with self.subTest(label), self.assertRaises(MbError):
                self.qs.admit("family", WakeInputs(**{**wake.__dict__, **changes}))
        self.assertEqual(self.qs.admit("family", WakeInputs(**{**wake.__dict__, "coverage_sha": "d" * 40})).reason,
                         "stale-wake")

    def test_artifact_sizes_are_bounded_by_their_archive_limits(self) -> None:
        # A bundle at its expanded bound becomes a larger archive: admission bounds the archive by the
        # kind's archive limit (bounded_zip.artifact_limit), never by the expanded bound itself, so
        # what a producer's own bound admits is never refused here (docs/SCHEMAS.md).
        handoff = artifact_limit("handoff")
        self.assertGreater(handoff, lim.MAX_RAW_BUNDLE_BYTES)
        for size, admitted in ((lim.MAX_RAW_BUNDLE_BYTES + 1, True), (handoff, True), (handoff + 1, False)):
            with self.subTest(kind="handoff", size=size):
                self.setUp()
                self.qs.e2e(size_in_bytes=size)
                wake = WakeInputs(run_id=900, sha=self.qs.head)
                if admitted:
                    self.assertTrue(self.qs.admit("deploy", wake).eligible)
                else:
                    with self.assertRaises(MbError) as caught:
                        self.qs.admit("deploy", wake)
                    self.assertIn("oversized", str(caught.exception))
        family = self.qs.invocation().config.family(FAMILY)
        limit = family_archive_limit(family)
        self.assertGreater(limit, family["handoff_max_bytes"])
        for size, admitted in ((family["handoff_max_bytes"] + 1, True), (limit, True), (limit + 1, False)):
            with self.subTest(kind="family-handoff", size=size):
                self.setUp()
                self.qs.e2e()
                self.qs.publish(legs=tuple(self.qs.keys))
                artifact = self.qs.family("mc1.20.1", 2000, at=3000, size_in_bytes=size)
                wake = WakeInputs(run_id=2000, sha=self.qs.head, family=FAMILY, bundle_key="mc1.20.1",
                                  artifact_id=artifact["id"], artifact_digest=artifact["digest"],
                                  coverage_sha=self.qs.head)
                if admitted:
                    self.assertTrue(self.qs.admit("family", wake).eligible)
                else:
                    with self.assertRaises(MbError) as caught:
                        self.qs.admit("family", wake)
                    self.assertEqual(caught.exception.reason, "wake")

    def test_api_failures_are_never_absence(self) -> None:
        self.qs.e2e()
        self.qs.api.failures["/actions/runs/900/artifacts"] = ApiError("quota", status=403, method="GET", path="x")
        with self.assertRaises(ApiError):
            self.qs.admit()
        budget = QuickSkinWorld(self, max_requests=3)
        with self.assertRaises(MbError) as caught:
            budget.admit()
        self.assertEqual(caught.exception.reason, "request-budget")


class BlockPopsAdmissionTest(unittest.TestCase):
    """``always`` mode over enrolled branches (Block Pops keeps admitting every authenticated wake)."""

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="mb-admission-bp-")).resolve()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.mod = support.materialize("bp_like", self.directory / "repo")
        self.key = hashlib.sha256(b"master").hexdigest()[:24]
        self.world = World(self.mod.repository)
        self.world.api.set_branch("master", self.mod.commit, self.mod.tree)
        patcher = mock.patch.object(host, "call", support.InProcessHost())
        patcher.start()
        self.addCleanup(patcher.stop)

    def release_head(self, local_branch: str, *, notes: bytes | None = None) -> support.FixtureMod:
        """A commit (on ``local_branch`` of the local repository) whose matrix enrolls ``RELEASE``."""

        matrix = json.loads((self.mod.root / "release/release-matrix.json").read_text())
        changes = {"release/release-matrix.json": json.dumps(dict(matrix, branch={**matrix["branch"],
                                                                                "name": RELEASE})).encode()}
        if notes is not None:
            changes["notes.txt"] = notes
        return support.commit_on_branch(self.mod, local_branch, changes)

    def admit(self, operation: str = "recovery", wake: WakeInputs = WakeInputs()) -> Admission:
        environ = support.environment(self.mod, run_id=9000, job="admit", workflow=PAGES_WORKFLOW_PATH)
        return admit(build_invocation(self.mod.root, None, environ), api=self.world.api, operation=operation,
                     wake=wake, now=epoch(NOW), sleep=lambda _seconds: None)

    def e2e(self, run_id: int = 101, **run: Any) -> dict[str, Any]:
        record = self.world.run(run_id, **{"head_sha": self.mod.commit, "title": f"Packaged E2E / {self.mod.commit}",
                                           **run})
        return self.world.handoff(record, self.key, job="Curate current-head public evidence (advisory)",
                                  step="Prepare and hand off current-head public evidence")

    def test_an_authenticated_wake_is_always_admitted(self) -> None:
        handoff = self.e2e(event="schedule")
        result = self.admit("deploy", WakeInputs(run_id=101, sha=self.mod.commit))
        self.assertEqual((result.eligible, result.reason), (True, "always"))
        self.assertEqual(result.nominations, {self.key: handoff["id"]})
        self.assertEqual(result.subjects, {self.key: self.mod.subject})
        self.assertEqual(result.families, [])
        self.world.api.set_branch("master", self.mod.commit, self.mod.tree)
        self.world.run(8800, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, created=900)
        self.world.artifact(grammar.cache_name(self.key, self.mod.commit), {"id": 8800, "head_sha": self.mod.commit,
                                                                            "head_branch": "master"}, created=1000)
        self.assertEqual(self.admit("deploy", WakeInputs(run_id=101, sha=self.mod.commit)).reason, "always",
                         "an authenticated wake is admitted even when every key is current")

    def test_a_wake_nominates_only_the_current_subjects_its_run_can_vouch_for(self) -> None:
        release = self.release_head(RELEASE)
        self.world.api.set_branch(RELEASE, release.commit, release.tree)
        release_key = hashlib.sha256(RELEASE.encode()).hexdigest()[:24]
        self.e2e(100)
        run = self.world.run(101, head_sha=self.mod.commit, title=f"Packaged E2E / {release.commit}", created=100)
        handoff = self.world.handoff(run, release_key, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        result = self.admit("deploy", WakeInputs(run_id=101, sha=self.mod.commit))
        self.assertEqual((result.eligible, result.reason, result.nominations),
                         (True, "always", {release_key: handoff["id"]}))
        self.assertEqual(result.subjects[release_key], release.subject)
        # The release branch moved on: run 101 tested a commit that is no longer its subject.
        moved = self.release_head("release-next", notes=b"next\n")
        self.world.api.set_branch(RELEASE, moved.commit, moved.tree)
        self.assertEqual(self.admit("deploy", WakeInputs(run_id=101, sha=self.mod.commit)).reason, "stale-wake")
        # A scheduled canonical run never vouches for a release subject (BP handoff_events).
        self.world.api.set_branch(RELEASE, release.commit, release.tree)
        scheduled = self.world.run(102, head_sha=self.mod.commit, title=f"Packaged E2E / {release.commit}",
                                   event="schedule", created=200)
        self.world.handoff(scheduled, release_key, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP)
        with self.assertRaises(MbError) as caught:
            self.admit("deploy", WakeInputs(run_id=102, sha=self.mod.commit))
        self.assertEqual(caught.exception.reason, "wake")

    def test_recovery_is_current_or_always(self) -> None:
        self.e2e()
        self.assertEqual(self.admit().reason, "always")
        owner = self.world.run(8800, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, created=900)
        self.world.artifact(grammar.cache_name(self.key, self.mod.commit), owner, created=1000)
        self.assertEqual(self.admit().reason, "current")

    def test_a_branch_listing_behind_the_protected_head_is_stale(self) -> None:
        self.e2e()
        self.world.api.add_commit("e" * 40, self.mod.tree)
        self.world.api.add_response(f"/repos/{self.mod.repository}/branches",
                                    [{"name": "master", "commit": {"sha": "e" * 40}}],
                                    params={"per_page": 100, "page": 1})
        self.assertEqual(self.admit(), Admission(eligible=False, reason="stale-implementation"))

    def test_a_family_operation_without_families_fails_closed(self) -> None:
        self.e2e()
        wake = WakeInputs(run_id=101, sha=self.mod.commit, family="mod-compatibility", bundle_key=self.key,
                          artifact_id=1, artifact_digest="sha256:" + "a" * 64, coverage_sha=self.mod.commit)
        with self.assertRaises(MbError):
            self.admit("family", wake)


class AdmitCommandTest(AdmissionTest):
    def test_outputs_are_single_line_canonical_json(self) -> None:
        handoffs = self.qs.e2e()
        environ = dict(self.qs.invocation().environ)
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(commands_control, "_client", return_value=self.qs.api), \
                mock.patch.object(cli, "environ", return_value=environ), \
                mock.patch.object(admission.time, "sleep"):
            output = Path(directory) / "output"
            arguments = ["admit", "--repo", str(self.qs.mod.root), "--operation", "deploy", "--run-id", "900",
                         "--sha", self.qs.head, "--github-output", str(output)]
            self.assertEqual(cli.main(arguments), 0)
            lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual(lines["eligible"], "true")
            self.assertEqual(lines["reason"], "initial-ordinary")
            self.assertEqual(json.loads(lines["bundle_keys"]), self.qs.keys)
            self.assertEqual(json.loads(lines["nominations"]), {key: handoffs[key]["id"] for key in self.qs.keys})
            self.assertEqual(json.loads(lines["heads"]), {"master": self.qs.head})
            self.assertEqual(lines["subjects"], json.dumps({key: self.qs.mod.subject for key in self.qs.keys},
                                                           sort_keys=True, separators=(",", ":")))
            output.unlink()
            self.qs.api.set_branch("master", "e" * 40, self.qs.mod.tree)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["admit", "--repo", str(self.qs.mod.root), "--operation", "recovery",
                                           "--github-output", str(output)]), 0)
            lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
            self.assertEqual((lines["eligible"], lines["bundle_keys"], lines["families"]), ("false", "[]", "[]"))


if __name__ == "__main__":
    unittest.main()
