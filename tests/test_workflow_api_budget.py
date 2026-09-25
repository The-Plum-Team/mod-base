"""The bounded GitHub API work of Pages admission (MB5).

Ports QS ``test_pages_workflow_api_budget`` (discovery: every exact cache name is queried once but
a validated owner is read once, a quota failure aborts without probing further candidates or
fanning out, the memoized owner never authenticates another artifact's head, a later owner failure
is never a cache miss, a source change during the cache inventory never accepts that snapshot)
from the inline ``discover`` shell step onto ``admit``, plus the QS ``publication_progress``
operation-count test: every read is an exact, counted GET within ``MAX_REQUESTS``, and an
``enrolled-branches`` budget test: the reads of an admission over many enrolled branches with a
long canonical run history stay far below the Pages client's ``MAX_PAGES_API_READS`` (one branch
listing, one memoized canonical run listing, no per-branch commit read). The QS build step's
bracketing head checks belong to ``mod_base.pages.build`` (MB6).
"""

from __future__ import annotations

import collections
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.github import contents
from mod_base.github.api import ApiError
from mod_base.model import grammar
from mod_base.model.limits import MAX_PAGES_API_READS
from mod_base.pages import admission
from mod_base.pages.admission import Admission, WakeInputs, admit
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH
from tests.fixtures.mods import support
from tests.test_admission import NOW, AdmissionTest, QuickSkinWorld
from tests.test_select import BP_HANDOFF_JOB, BP_HANDOFF_STEP, World, epoch


class RecoveryInventoryBudgetTest(AdmissionTest):
    def reads(self, suffix: str) -> int:
        """GETs of exactly the resource ending in ``suffix`` (not its sub-resources)."""

        return sum(path.endswith(suffix) for path, _ in self.qs.api.calls)

    def cache(self, key: str, owner: dict[str, Any], *, head_sha: str | None = None) -> None:
        run = {**owner, "head_sha": head_sha or owner["head_sha"]}
        self.qs.world.artifact(grammar.cache_name(key, self.qs.head), run, created=2000)

    def owner(self, run_id: int = 1000) -> dict[str, Any]:
        return self.qs.world.run(run_id, path=PAGES_WORKFLOW_PATH, head_sha=self.qs.head, created=1900)

    def test_each_exact_name_is_queried_once_and_one_validated_owner_is_read(self) -> None:
        owner = self.owner()
        for key in self.qs.keys:
            self.cache(key, owner)
        self.assertEqual(self.qs.admit(), Admission(eligible=False, reason="current"))
        self.assertEqual(len(self.qs.api.requests("/actions/artifacts")), len(self.qs.keys))
        self.assertEqual({call[1]["name"] for call in self.qs.api.requests("/actions/artifacts")},
                         {grammar.cache_name(key, self.qs.head) for key in self.qs.keys})
        self.assertEqual(self.reads("/actions/runs/1000"), 1)
        self.assertEqual(self.reads("/actions/workflows/pages.yml"), 1, "the owner's workflow id is read once")
        self.assertEqual(len(self.qs.api.requests("/branches/master")), 2, "the head is rechecked after the inventory")
        # A cache is not a tombstone: one exact source listing proves no newer attempt settled since.
        self.assertEqual([call[1] for call in self.qs.api.requests("/on-demand-e2e.yml/runs")],
                         [{"branch": "master", "head_sha": self.qs.head, "per_page": 100, "page": 1}])
        self.assertEqual(self.qs.api.request_count, 9)

    def test_quota_failure_aborts_without_additional_candidates_or_fanout(self) -> None:
        for status in (403, 429):
            with self.subTest(status=status):
                self.setUp()
                owner = self.owner()
                for key in self.qs.keys:
                    self.cache(key, owner)
                self.qs.api.failures["/actions/runs/1000"] = ApiError("API rate limit exceeded", status=status,
                                                                     method="GET", path="x")
                with self.assertRaisesRegex(ApiError, "rate limit"):
                    self.qs.admit()
                self.assertEqual(self.reads("/actions/runs/1000"), 1)
                self.assertEqual(len(self.qs.api.requests("/actions/artifacts")), 1)
                self.assertEqual(self.qs.api.requests("/on-demand-e2e.yml/runs"), [])

    def test_the_memoized_owner_never_authenticates_another_artifact_head(self) -> None:
        self.qs.e2e()
        owner = self.owner()
        self.qs.world.jobs(owner, [self.qs.world.job("Publish / Build atomic static site")])
        self.cache(self.qs.keys[0], owner)
        self.cache(self.qs.keys[1], owner, head_sha="c" * 40)
        result = self.qs.admit()
        self.assertEqual((result.eligible, result.reason), (True, "initial-ordinary"))
        self.assertEqual(self.reads("/actions/runs/1000"), 1)
        inventories = [call for call in self.qs.api.calls if call[0].endswith("/actions/artifacts")]
        self.assertEqual([call[1]["name"] for call in inventories],
                         [grammar.cache_name(key, self.qs.head) for key in self.qs.keys])

    def test_a_later_owner_failure_is_never_a_cache_miss(self) -> None:
        self.cache(self.qs.keys[0], self.owner(1000))
        self.cache(self.qs.keys[1], self.owner(1001))
        self.qs.api.failures["/actions/runs/1001"] = ApiError("quota", status=403, method="GET", path="x")
        with self.assertRaises(ApiError):
            self.qs.admit()
        self.assertEqual((self.reads("/actions/runs/1000"),
                          self.reads("/actions/runs/1001")), (1, 1))
        self.assertEqual(self.qs.api.requests("/on-demand-e2e.yml/runs"), [])

    def test_a_source_change_during_the_inventory_never_accepts_its_snapshot(self) -> None:
        owner = self.owner()
        for key in self.qs.keys:
            self.cache(key, owner)
        heads = iter([self.qs.head, "d" * 40])
        real = contents.branch_head

        def moving(api: Any, branch: str) -> tuple[str, str]:
            commit, tree = real(api, branch)
            return next(heads, commit), tree

        with mock.patch.object(contents, "branch_head", side_effect=moving):
            self.assertEqual(self.qs.admit().reason, "stale-implementation")

    def test_an_eligible_admission_rechecks_the_head_last(self) -> None:
        self.qs.e2e()
        heads = iter([self.qs.head, "d" * 40])
        real = contents.branch_head

        def moving(api: Any, branch: str) -> tuple[str, str]:
            commit, tree = real(api, branch)
            return next(heads, commit), tree

        with mock.patch.object(contents, "branch_head", side_effect=moving):
            self.assertEqual(self.qs.admit("deploy", admission.WakeInputs(run_id=900, sha=self.qs.head)),
                             Admission(eligible=False, reason="stale-implementation"))


class ProgressOperationBudgetTest(AdmissionTest):
    def test_actual_bounded_operations_are_exact_and_counted(self) -> None:
        self.qs.e2e()
        self.qs.publish()
        self.qs.family("mc1.20.1", 2000, at=3000)
        result = self.qs.admit(now=3610)
        self.assertEqual((result.eligible, result.reason), (True, "half-coverage"))
        categories = collections.Counter()
        for path, params in self.qs.api.calls:
            tail = path.split("/actions/", 1)[1] if "/actions/" in path else "repository"
            if tail.startswith("workflows/") and tail.endswith("/runs"):
                categories["workflow-runs"] += 1
            elif tail.startswith("workflows/"):
                categories["workflow-record"] += 1
            elif tail.endswith("/jobs"):
                categories["exact-attempt-jobs"] += 1
            elif tail.endswith("/artifacts"):
                categories["run-artifacts"] += 1
            elif tail == "artifacts":
                categories["artifact-inventory"] += 1
            elif tail.startswith("artifacts/"):
                categories["exact-artifact"] += 1
            elif tail.startswith("runs/"):
                categories["owner-run"] += 1
            else:
                categories[tail] += 1
        self.assertEqual(dict(categories), {
            "repository": 3,  # default branch, live head before and after
            "artifact-inventory": 3,  # the two exact cache names and the ready family cache name
            "owner-run": 1,  # the memoized Pages owner
            "workflow-runs": 9,  # five active-status pages, the source and producer runs, failure and cancelled
            "workflow-record": 2,  # the source and pages.yml workflow ids, each read once
            "run-artifacts": 2,  # the producer and the Pages owner inventories (cached keys need no source scan)
            "exact-attempt-jobs": 2,  # the owner's and the producer's exact attempts
            "exact-artifact": 1,  # the ready family handoff re-read by id
        })
        self.assertEqual(self.qs.api.request_count, sum(categories.values()))
        self.assertLessEqual(self.qs.api.request_count, admission.MAX_REQUESTS)

    def test_the_request_budget_is_a_hard_stop(self) -> None:
        # The Pages client is built with MAX_PAGES_API_READS; a smaller budget stops just the same.
        self.qs = QuickSkinWorld(self, max_requests=5)
        self.qs.e2e()
        with self.assertRaises(MbError) as caught:
            self.qs.admit()
        self.assertEqual(caught.exception.reason, "request-budget")
        self.assertEqual(self.qs.api.request_count, 5)


class EnrolledBranchesBudgetTest(unittest.TestCase):
    """Block Pops-like admission over many enrolled branches within the Pages read budget."""

    BRANCHES = 24
    RUNS_PER_SUBJECT = 11  # 264 canonical runs: three listing pages

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-budget-enrolled-")).resolve()
        cls.mod = support.materialize("bp_like", cls.directory / "repo")
        matrix = json.loads((cls.mod.root / "release/release-matrix.json").read_text())
        cls.heads = {"master": cls.mod}
        for index in range(cls.BRANCHES - 1):
            name = f"release/1.{index}"
            enrolled = dict(matrix, branch={**matrix["branch"], "name": name})
            cls.heads[name] = support.commit_on_branch(cls.mod, name, {
                "release/release-matrix.json": json.dumps(enrolled).encode()})

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.world = World(self.mod.repository, max_requests=MAX_PAGES_API_READS)
        for name, head in self.heads.items():
            self.world.api.set_branch(name, head.commit, head.tree)
        patcher = mock.patch.object(host, "call", support.InProcessHost())
        patcher.start()
        self.addCleanup(patcher.stop)
        # Every subject has a history of successful canonical runs; only the newest handed off.
        run_ids = iter(range(10_000, 20_000))
        for position, (name, head) in enumerate(self.heads.items()):
            key = hashlib.sha256(name.encode()).hexdigest()[:24]
            for age in range(self.RUNS_PER_SUBJECT):
                run = self.world.run(next(run_ids), head_sha=self.mod.commit, title=f"Packaged E2E / {head.commit}",
                                     created=position * 10 + age * 1000)
            self.world.handoff(run, key, job=BP_HANDOFF_JOB, step=BP_HANDOFF_STEP,
                               created=position * 10 + self.RUNS_PER_SUBJECT * 1000 + 300)

    def admit(self, operation: str = "recovery", wake: WakeInputs = WakeInputs()) -> Admission:
        environ = support.environment(self.mod, run_id=9000, job="admit", workflow=PAGES_WORKFLOW_PATH)
        return admit(build_invocation(self.mod.root, None, environ), api=self.world.api, operation=operation,
                     wake=wake, now=epoch(NOW), sleep=lambda _seconds: None)

    def test_the_first_publication_reads_one_listing_per_kind_and_one_inventory_per_subject(self) -> None:
        result = self.admit()
        self.assertEqual((result.eligible, result.reason, len(result.bundle_keys)), (True, "always", self.BRANCHES))
        calls = [path for path, _ in self.world.api.calls]
        self.assertEqual(sum(path.endswith("/branches") for path in calls), 1)
        self.assertEqual([path for path in calls if "/git/commits/" in path], [], "trees come from fetched objects")
        self.assertEqual(sum(path.endswith("/on-demand-e2e.yml/runs") for path in calls), 3,
                         "one memoized canonical listing (three pages) serves every subject")
        self.assertEqual(sum(path.endswith("/artifacts") and "/runs/" in path for path in calls), self.BRANCHES,
                         "only the newest run of each subject is inventoried")
        self.assertLessEqual(self.world.api.request_count, 2 * self.BRANCHES + 10)

    def test_a_published_inventory_is_current_within_the_budget(self) -> None:
        owner = self.world.run(8800, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, created=20_000)
        for name, head in self.heads.items():
            key = hashlib.sha256(name.encode()).hexdigest()[:24]
            self.world.artifact(grammar.cache_name(key, head.commit), owner, created=20_100)
        self.assertEqual(self.admit(), Admission(eligible=False, reason="current"))
        recovery = self.world.api.request_count
        self.assertLessEqual(recovery, self.BRANCHES + 12)
        wake = self.admit("deploy", WakeInputs(run_id=10_000 + self.RUNS_PER_SUBJECT - 1, sha=self.mod.commit))
        self.assertEqual((wake.reason, len(wake.nominations)), ("always", 1))
        self.assertLessEqual(self.world.api.request_count - recovery, self.BRANCHES + 10)

