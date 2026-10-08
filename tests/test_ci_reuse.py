"""Post-merge reuse is decided three ways and never in between: admitted, full run, or an error.

``tests/ci_reuse.py`` builds the world: the push and, beside it, the merged pull request with both
of its original gates. Only the GitHub API is faked; record archives are real ZIPs that the kit's
bounded reader extracts in a temporary directory. The cases are those of Quick Skin's
``scripts/ci/tests/test_ci_reuse.py`` at ``c0cdc01`` and the reuse rows of the failure table in
``docs/BUILD-E2E-DESIGN.md`` ("Post-merge reuse").
"""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci import reuse
from mod_base.build_ci.protocol import plan_sha256
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError, ApiNotFound, RequestBudgetExhausted
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests import ci_mod_harness as h
from tests.ci_attempt import ATTEMPT
from tests.ci_reuse import (BASE_SHA, BUILD_SEAL, BUNDLE, COMMIT_PULLS, FIRST_PAGE, GATE, LANE, ORIGINAL_RUN,
                            PACKAGED_SEAL, PULL, RESULTS, REUSE_LISTING, TEST_MERGE, Merged, ReuseCase, downloaded,
                            earlier)
from tests.helpers import ci_api_run
from tests.test_ci_transport import after_download

POLICY = "Shared Build / Verify protected policy"
ASSEMBLE = "Shared Build / Seal complete Build bundle"
LANE_JOB = "Shared Packaged E2E / Run packaged lane lane-a"
RUNS = {producer: f"/repos/{h.REPOSITORY}/actions/workflows/mod-base-{name}.yml/runs"
        for producer, name in (("build", "build"), ("packaged", "packaged-e2e"))}
#: What the admission has sent when it knows the answer: the pull requests of the commit; then the
#: listing, the run, its jobs and its artifacts for each original run; then the Build record.
DIRECT_PUSH_REQUESTS, CLAIM_REQUESTS = 1, 11
#: An admitted reuse: the claim, the merged history (7), each gate's record by id and its two
#: download requests and the sources it names (4 and 4); then 12 more to observe again.
ADMITTED_REQUESTS, RECHECKED_REQUESTS = 26, 38


class AdmittedTests(ReuseCase):
    def test_a_merge_with_the_tested_tree_is_covered_by_both_original_gates(self) -> None:
        for targets, lanes in ((1, 1), (17, 2)):
            world = self.world(targets=targets, lanes=lanes)
            with self.subTest(targets=targets, lanes=lanes), \
                    patch.object(world.api, "download", wraps=world.api.download) as downloads:
                outcome = self.decide(world)
                self.assertIsInstance(outcome, reuse.ReuseAdmitted)
                self.assertEqual((outcome.pr_number, outcome.source), (7, world.source))
                # The reference names the commit that was tested, not the one that merged.
                self.assertEqual(outcome.source["identity"]["tested_sha"], TEST_MERGE)
                self.assertEqual(outcome.source["identity"]["tested_parents"], [BASE_SHA, h.HEAD_SHA])
                self.assertNotEqual(TEST_MERGE, world.merged)
                self.assertEqual(outcome.source["identity"]["tested_tree"], world.covered["identity"]["tested_tree"])
                self.assertEqual(world.api.request_count, ADMITTED_REQUESTS)
                outcome.recheck()
                self.assertEqual(world.api.request_count, RECHECKED_REQUESTS)
                self.assertLess(RECHECKED_REQUESTS, 60)
                # Only the two records are downloaded, never a bundle or a lane's results. The Build
                # record is read twice: as the claim that names the test merge, then as evidence.
                self.assertEqual(downloaded(downloads), [BUILD_SEAL, BUILD_SEAL, PACKAGED_SEAL])

    def test_the_final_commit_may_be_the_test_merge_itself(self) -> None:
        # A fast-forward of the test merge: the pushed commit has the ordered parents [base, head].
        attempt = self.attempt(listing=REUSE_LISTING["build"], push=True)
        attempt.plan["identity"]["tested_parents"] = [BASE_SHA, h.HEAD_SHA]
        attempt.plan["plan_sha256"] = plan_sha256(attempt.plan)
        world = Merged(attempt, tested_sha=h.CONTROLLER_SHA)
        outcome = self.decide(world)
        self.assertIsInstance(outcome, reuse.ReuseAdmitted)
        self.assertEqual(outcome.source["identity"]["tested_sha"], world.covered["identity"]["tested_sha"])
        # The two bindings stay apart even then: the reference names a pull request, the push none.
        self.assertEqual((outcome.source["identity"]["pr_number"], world.covered["identity"]["pr_number"]), (7, 0))

    def test_an_unrelated_pull_request_that_holds_the_commit_is_ignored(self) -> None:
        world = self.world()
        other = {**copy.deepcopy(world.pull), "number": 9, "state": "open", "merged_at": None,
                 "merge_commit_sha": "f" * 40}
        world.associated = [other, world.pull]
        world.seed_pull()
        self.assertIsInstance(self.decide(world), reuse.ReuseAdmitted)

    def test_an_admission_is_observed_again_before_its_effect(self) -> None:
        def second_merge(world):
            world.associated = [world.pull, {**copy.deepcopy(world.pull), "number": 8}]
            world.seed_pull()

        moves = {
            "Build record expired": lambda world: world.set_artifact(BUILD_SEAL, expired=True),
            "packaged record expired": lambda world: world.set_artifact(PACKAGED_SEAL, expired=True),
            "bundle expired": lambda world: world.set_artifact(BUNDLE, expired=True),
            "lane expired": lambda world: world.set_artifact(LANE, expired=True),
            "results expired": lambda world: world.set_artifact(RESULTS, expired=True),
            "Build rerun": lambda world: world.set_run("build", run_attempt=3, status="queued", conclusion=None),
            "packaged rerun": lambda world: world.set_run("packaged", run_attempt=3),
            "newer run": lambda world: world.api.add_run(ci_api_run(world.plan, "build", id=150)),
            "merge changed": lambda world: world.change_pull(merged_at="2026-10-07T09:30:00Z"),
            "second merged pull request": second_merge,
            "default branch moved": lambda world: world.api.set_branch(h.BRANCH, "f" * 40, "e" * 40),
        }
        for name, move in moves.items():
            world = self.world()
            outcome = self.decide(world)
            move(world)
            with self.subTest(move=name), self.assertRaisesRegex(MbError, r"changed between|\$\.merged") as caught:
                outcome.recheck()
            # What moves while a command runs is a rejection, never a late reason for a full run.
            self.assertNotIsInstance(caught.exception, Unavailable)
        world = self.world()
        outcome = self.decide(world)
        world.change_pull(updated_at="2026-10-07T12:00:00Z", title="Renamed after the merge", labels=[{"name": "x"}])
        outcome.recheck()


class PullRequestTests(ReuseCase):
    def rows(self, world: Merged, *changes: dict) -> None:
        """What GitHub lists for the pushed commit: the pull request once for each of ``changes``."""

        world.associated = []
        for change in changes:
            row = copy.deepcopy(world.pull)
            for key, value in change.items():
                if key in ("head", "base") and isinstance(value, dict):
                    row[key].update(value)
                else:
                    row[key] = value
            world.associated.append(row)
        world.api.add_response(COMMIT_PULLS, world.associated, params=FIRST_PAGE)

    def test_a_push_that_no_merged_pull_request_ends_in_runs_in_full(self) -> None:
        cases = {"direct push": [], "not merged": [{"merged_at": None, "state": "open"}],
                 "another final commit": [{"merge_commit_sha": "f" * 40}],
                 "another base branch": [{"base": {"ref": "release"}}],
                 "another base repository": [{"base": {"repo": {"full_name": "other/synthetic-mod"}}}]}
        for name, changes in cases.items():
            world = self.world()
            self.rows(world, *changes)
            with self.subTest(case=name), patch.object(world.api, "download") as downloads:
                self.assert_full(world, "no-merged-pull-request", requests=DIRECT_PUSH_REQUESTS)
                downloads.assert_not_called()

    def test_more_than_one_merged_pull_request_or_one_from_a_fork_runs_in_full(self) -> None:
        world = self.world()
        self.rows(world, {}, {"number": 8})
        self.assert_full(world, "several-merged-pull-requests", requests=DIRECT_PUSH_REQUESTS)
        for head in ({"repo": {"full_name": "fork/synthetic-mod"}}, {"repo": None}):
            world = self.world()
            self.rows(world, {"head": head})
            with self.subTest(head=head):
                self.assert_full(world, "foreign-pull-request", requests=DIRECT_PUSH_REQUESTS)

    def test_malformed_pull_request_metadata_stops_the_job(self) -> None:
        cases = [{"merge_commit_sha": "not-a-commit"}, {"merge_commit_sha": 5}, {"base": None}, {"base": {"ref": 5}},
                 {"number": 0}, {"number": True}, {"number": "7"}, {"state": "open"}, {"head": None},
                 {"head": {"sha": "abc"}}, {"head": {"ref": ""}}, {"head": {"ref": "bad\nbranch"}},
                 {"merged_at": "yesterday"}, {"merged_at": 5}]
        for change in cases:
            world = self.world()
            self.rows(world, change)
            with self.subTest(change=change):
                self.assert_stops(world)
                self.assertEqual(world.api.request_count, DIRECT_PUSH_REQUESTS)
        world = self.world()
        world.api.add_response(COMMIT_PULLS, {"message": "not a list"}, params=FIRST_PAGE)
        self.assert_stops(world, "is not an array")
        # More associated pull requests than one page holds is no merge the kit attributes.
        world = self.world()
        self.rows(world, *({"number": 100 + index, "merge_commit_sha": "f" * 40} for index in range(100)))
        world.api.add_response(COMMIT_PULLS, [world.pull], params={"per_page": 100, "page": 2})
        self.assert_stops(world)

    def test_the_merge_must_still_be_what_the_commit_listing_said(self) -> None:
        def separately(change):
            def apply(world):
                world.associated = [copy.deepcopy(world.pull)]
                world.pull.update(change)
                world.seed_pull()
            return apply

        cases = {"unmerged": separately({"merged": False}), "reopened": separately({"state": "open"}),
                 "draft": separately({"draft": True}),
                 "another final commit": separately({"merge_commit_sha": "f" * 40}),
                 "head moved": lambda world: (world.pull["head"].update(sha="f" * 40),
                                              world.api.add_response(PULL, world.pull)),
                 "default branch moved": lambda world: world.api.set_branch(h.BRANCH, "f" * 40, "e" * 40)}
        for name, change in cases.items():
            world = self.world()
            world.associated = [copy.deepcopy(world.pull)]
            world.seed_pull()
            change(world)
            with self.subTest(case=name):
                self.assert_stops(world, r"\$\.merged")


class OriginalRunTests(ReuseCase):
    def test_a_pull_request_without_an_original_run_runs_in_full(self) -> None:
        hidden = ({"head_branch": "another/branch"}, {"event": "push"}, {"head_sha": "f" * 40},
                  {"head_repository": {"full_name": "fork/synthetic-mod"}})
        for producer, requests in (("build", 2), ("packaged", 6)):
            for change in hidden:
                world = self.world()
                world.set_run(producer, **change)
                with self.subTest(producer=producer, change=change), patch.object(world.api, "download") as downloads:
                    self.assert_full(world, "no-original-run", requests=requests)
                    downloads.assert_not_called()

    def test_a_failed_newest_run_never_falls_back_to_an_older_success(self) -> None:
        for producer, requests in (("build", 3), ("packaged", 7)):
            for conclusion in ("failure", "cancelled", "timed_out", "skipped", "startup_failure"):
                world = self.world()
                world.set_run(producer, conclusion=conclusion)
                with self.subTest(producer=producer, conclusion=conclusion), \
                        patch.object(world.api, "download") as downloads:
                    self.assert_full(world, "original-run-failed", requests=requests)
                    downloads.assert_not_called()
        # The newest run is chosen before any result is read: by creation time, whatever its result.
        world = self.world()
        world.api.add_run(ci_api_run(world.plan, "build", id=150, conclusion="failure"))
        self.assert_full(world, "original-run-failed", requests=3)
        world = self.world()
        world.set_run("build", conclusion="cancelled")
        world.api.add_run(earlier(ci_api_run(world.plan, "build", id=141, created_at="2026-10-05T10:00:00Z")))
        self.assert_full(world, "original-run-failed", requests=3)

    def test_an_original_run_that_has_not_finished_stops_the_job(self) -> None:
        for producer, requests in (("build", 2), ("packaged", 6)):
            for status in ("queued", "in_progress", "waiting", "requested", "pending"):
                world = self.world()
                world.set_run(producer, status=status, conclusion=None)
                with self.subTest(producer=producer, status=status), patch.object(world.api, "download") as downloads:
                    error = self.assert_stops(world, "has not finished")
                    self.assertIsInstance(error, reuse.OriginalPending)
                    self.assertEqual((error.reason, error.exit_code), ("ci-original-pending", 2))
                    self.assertEqual(world.api.request_count, requests)
                    downloads.assert_not_called()
        # A rerun that starts after the listing was read is seen on the run itself.
        world = self.world()
        world.api.during_listing(RUNS["build"], lambda: world.set_run("build", run_attempt=3, status="in_progress",
                                                                      conclusion=None))
        self.assertIsInstance(self.assert_stops(world, "has not finished"), reuse.OriginalPending)
        for change in ({"status": "odd", "conclusion": None}, {"status": "in_progress", "conclusion": "success"},
                       {"status": "completed", "conclusion": None}):
            world = self.world()
            world.set_run("packaged", **change)
            with self.subTest(change=change):
                self.assertNotIsInstance(self.assert_stops(world), reuse.OriginalPending)

    def test_a_deferral_or_a_reuse_run_is_no_tested_generation(self) -> None:
        for producer in ("build", "packaged"):
            for listing, reason in ((f"{producer}-deferred", "original-run-deferred"),
                                    (f"{producer}-reuse", "original-run-reused")):
                world = self.world()
                world.add_run(producer, listing)
                with self.subTest(listing=listing), patch.object(world.api, "download") as downloads:
                    self.assert_full(world, reason)
                    downloads.assert_not_called()
        # References never chain: a run that ever uploaded one is not original evidence.
        for producer in ("build", "packaged"):
            for attempt in (1, ATTEMPT):
                world = self.world()
                record = copy.deepcopy(world.records[BUNDLE if producer == "build" else RESULTS])
                record.update(id=650, name=grammar.ci_artifact_name("reuse", ORIGINAL_RUN[producer], attempt))
                world.api.add_artifact(record, b"a reference")
                with self.subTest(producer=producer, attempt=attempt), \
                        patch.object(world.api, "download") as downloads:
                    outcome = self.assert_full(world, "original-run-reused")
                    self.assertIn(record["name"], outcome.detail)
                    downloads.assert_not_called()

    def test_another_kit_pin_runs_in_full_before_anything_is_downloaded(self) -> None:
        def pins(world, producer, sha):
            """The run called the kit at ``sha``, or (``None``) lists no kit workflow at all."""

            entries = world.runs[producer]["referenced_workflows"]
            guard = [entry for entry in entries if entry["path"].startswith(h.REPOSITORY)]
            kit = [{**entry, "path": entry["path"].rsplit("@", 1)[0] + "@" + sha, "sha": sha}
                   for entry in entries if entry not in guard] if sha else []
            world.set_run(producer, referenced_workflows=guard + kit)

        for producer in ("build", "packaged"):
            for sha in ("f" * 40, None):
                world = self.world()
                pins(world, producer, sha)
                with self.subTest(producer=producer, sha=sha), patch.object(world.api, "download") as downloads:
                    self.assert_full(world, "kit-pin-differs")
                    downloads.assert_not_called()

    def test_a_run_that_is_not_what_its_listing_said_stops_the_job(self) -> None:
        foreign = "not recorded under the pull request's head"
        for change, message in (({"created_at": "2026-10-06T11:00:00Z"}, "run listing and run disagree"),
                                ({"head_sha": "f" * 40}, foreign), ({"head_branch": "another"}, foreign),
                                ({"event": "push"}, foreign), ({"path": ".github/workflows/other.yml"}, foreign)):
            world = self.world()
            world.api.during_listing(RUNS["build"], lambda world=world, change=change: world.set_run("build", **change))
            with self.subTest(change=change):
                self.assert_stops(world, message)

    def test_a_failed_jobs_only_rerun_is_no_complete_attempt(self) -> None:
        world = self.world()
        jobs = world.listing("build")
        for job in jobs[3:]:
            job.update(run_attempt=3)
        world.set_run("build", run_attempt=3)
        world.set_jobs("build", jobs, run_attempt=3)
        self.assert_stops(world)


class ComparisonTests(ReuseCase):
    def test_a_difference_in_what_was_tested_runs_in_full(self) -> None:
        def kit(**changes):
            return lambda now: {"kit": {**now["kit"], **changes}}

        cases = [("merged-tree-differs", {"tested_tree": "7" * 40}), ("policy-differs", {"policy_sha256": "f" * 64}),
                 ("kit-pin-differs", kit(version="9.9.9")),
                 ("kit-pin-differs", kit(tree_digest="sha256:" + "f" * 64)),
                 ("inventory-differs", {"inventory_sha256": "f" * 64}),
                 ("inventory-differs", {"inventory_blob": "f" * 40}),
                 ("scenario-contract-differs", {"scenario_sha256": "f" * 64}),
                 ("runtime-selection-differs", {"runtime_selection_sha256": "f" * 64}),
                 ("original-run-of-another-pull-request", {"pr_number": 8})]
        for index, (reason, change) in enumerate(cases):
            attempt = self.attempt(listing=REUSE_LISTING["build"], push=True)
            world = Merged(attempt, **(change(attempt.plan["identity"]) if callable(change) else change))
            with self.subTest(reason=reason, index=index), \
                    patch.object(world.api, "download", wraps=world.api.download) as downloads:
                self.assert_full(world, reason, requests=CLAIM_REQUESTS)
                self.assertEqual(downloaded(downloads), [BUILD_SEAL])

    def test_gates_sealed_for_another_plan_run_in_full(self) -> None:
        edits = {"plan input": lambda plan: plan["plan_inputs"][0].update(sha256="f" * 64),
                 "obligation": lambda plan: plan["lanes"][0]["obligations"].append("scenario/example/server/second"),
                 "native contract": lambda plan: plan["targets"][0].update(native_contract_sha256="f" * 64),
                 "java": lambda plan: plan["targets"][0].update(java=17)}
        for name, edit in edits.items():
            world = self.world(edit=edit)
            with self.subTest(edit=name):
                self.assert_full(world, "plan-differs", requests=CLAIM_REQUESTS)

    def test_a_record_of_another_caller_or_repository_stops_the_job(self) -> None:
        workflow = ".github/workflows/other-build.yml"
        world = self.world(controller_workflow=workflow,
                           controller_ref=grammar.workflow_ref(h.REPOSITORY, workflow, h.BRANCH))
        self.assert_stops(world, "not of this repository's protected caller")
        self.assertEqual(world.api.request_count, CLAIM_REQUESTS)


class EvidenceTests(ReuseCase):
    def test_an_expired_or_missing_original_artifact_runs_in_full(self) -> None:
        for artifact_id in (BUILD_SEAL, PACKAGED_SEAL, BUNDLE, LANE, RESULTS):
            for gone in ("expired", "missing"):
                world = self.world(missing=(artifact_id,) if gone == "missing" else ())
                if gone == "expired":
                    world.set_artifact(artifact_id, expired=True)
                with self.subTest(artifact_id=artifact_id, gone=gone), \
                        patch.object(world.api, "download", wraps=world.api.download) as downloads:
                    # Without the Build record there is nothing to download: two requests fewer.
                    requests = CLAIM_REQUESTS - 2 if artifact_id == BUILD_SEAL else CLAIM_REQUESTS
                    outcome = self.assert_full(world, "original-evidence-unavailable", requests=requests)
                    self.assertIn(world.records[artifact_id]["name"], outcome.detail)
                    self.assertEqual(downloaded(downloads), [] if artifact_id == BUILD_SEAL else [BUILD_SEAL])

    def test_evidence_that_goes_while_it_is_read_runs_in_full(self) -> None:
        # The listing still showed the artifact; the reader that selects it by id finds it gone.
        for artifact_id, count in ((BUNDLE, 1), (PACKAGED_SEAL, 2), (LANE, 3), (RESULTS, 3)):
            world = self.world()
            with self.subTest(artifact_id=artifact_id), \
                    after_download(world.api, lambda: world.set_artifact(artifact_id, expired=True), count=count):
                self.assert_full(world, "original-evidence-unavailable")
        world = self.world()
        gone = ApiNotFound("GitHub API GET 404", status=404, method="GET",
                           path=f"/repos/{h.REPOSITORY}/actions/artifacts/{BUNDLE}")
        original = world.api.get_json

        def get(path, **arguments):
            if path == gone.path:
                raise gone
            return original(path, **arguments)

        with patch.object(world.api, "get_json", side_effect=get):
            self.assertEqual(self.assert_full(world, "original-evidence-unavailable").detail,
                             f"original evidence is gone: GitHub answers 404 for {gone.path}")
        world = self.world()
        expired = ApiError("GitHub API GET 410", status=410, method="GET",
                           path=f"/repos/{h.REPOSITORY}/actions/artifacts/{BUILD_SEAL}/zip")
        with patch.object(world.api, "download", side_effect=expired):
            self.assert_full(world, "original-evidence-unavailable", requests=CLAIM_REQUESTS - 2)

    def test_ambiguous_or_foreign_artifact_metadata_stops_the_job(self) -> None:
        def twice(world):
            world.api.add_artifact({**world.records[BUILD_SEAL], "id": 699}, world.archives[BUILD_SEAL])

        cases = {"listed twice": (twice, "listed more than once"),
                 "another head": (lambda world: world.set_artifact(BUILD_SEAL, workflow_run={"head_sha": "f" * 40}),
                                  "not recorded under its run"),
                 "another branch": (lambda world: world.set_artifact(LANE, workflow_run={"head_branch": h.BRANCH}),
                                    "not recorded under its run"),
                 # Corruption is reported before availability: an artifact that is both is corrupt.
                 "foreign and expired": (lambda world: world.set_artifact(BUNDLE, expired=True,
                                                                         workflow_run={"head_sha": "f" * 40}),
                                         "not recorded under its run"),
                 "oversized record": (lambda world: world.set_artifact(BUILD_SEAL,
                                                                      size_in_bytes=limits.MAX_CI_RECORD_BYTES + 1),
                                      "exceeds the record cap"),
                 "no expiry": (lambda world: world.set_artifact(RESULTS, expires_at=None), ""),
                 "created outside its upload": (lambda world: world.set_artifact(
                     PACKAGED_SEAL, created_at="2026-10-06T10:20:00Z"), "upload window")}
        for name, (change, message) in cases.items():
            world = self.world()
            change(world)
            with self.subTest(case=name):
                self.assert_stops(world, message)


class CorruptionTests(ReuseCase):
    def test_every_job_of_both_original_graphs_must_have_passed(self) -> None:
        for producer, name in (("build", POLICY), ("build", ASSEMBLE), ("packaged", LANE_JOB),
                               ("packaged", GATE["packaged"])):
            for change in ("failure", "cancelled", "skipped", "in_progress", "missing", "duplicate"):
                world = self.world()
                jobs = world.jobs[producer]
                job = world.job(producer, name)
                if change == "missing":
                    jobs.remove(job)
                elif change == "duplicate":
                    jobs.append({**copy.deepcopy(job), "id": job["id"] + 1})
                elif change == "in_progress":
                    job.update(status="in_progress", conclusion=None)
                else:
                    job.update(conclusion=change)
                world.set_jobs(producer, jobs)
                with self.subTest(producer=producer, job=name, change=change):
                    self.assert_stops(world)

    def test_a_record_that_is_not_what_was_sealed_stops_the_job(self) -> None:
        def other_bytes(world, artifact_id):
            world.archives[artifact_id] = b"other bytes"
            world.api.add_artifact(world.records[artifact_id], b"other bytes")

        for gate, artifact_id in (("build", BUILD_SEAL), ("packaged", PACKAGED_SEAL)):
            document = self.world().documents[gate]
            raw = canonical_json(document)
            cases = {"digest": lambda world: other_bytes(world, artifact_id),
                     "trailing line": lambda world: world.seal(gate, raw=raw + b"\n"),
                     "unknown key": lambda world: world.seal(gate, raw=canonical_json({**document, "extra": True})),
                     "duplicate key": lambda world: world.seal(
                         gate, raw=raw.replace(b'"gate":', b'"gate":"x","gate":', 1)),
                     "another file": lambda world: world.seal(gate, name="other.json"),
                     "the other gate": lambda world: world.seal(gate, raw=canonical_json(
                         world.documents["packaged" if gate == "build" else "build"]))}
            # The Build record is refused as a claim already; the packaged one where it is bound.
            messages = {"another file": "exactly its fixed root filename|must hold exactly ci-gate.json",
                        "the other gate": ("is not the Build receipt of a pull request" if gate == "build"
                                           else "record differs from selected writer identity")}
            for name, change in cases.items():
                world = self.world()
                change(world)
                with self.subTest(gate=gate, case=name):
                    self.assert_stops(world, messages.get(name, ""))

    def test_the_packaged_gate_must_have_run_the_bundle_the_build_gate_sealed(self) -> None:
        world = self.world()
        other = copy.deepcopy(world.bundle)
        other["producer"]["run_id"] = 141
        other["artifact"].update(id=BUNDLE + 1, name=grammar.ci_artifact_name("build", 141, ATTEMPT))
        world.api.add_run(earlier(ci_api_run(world.plan, "build", id=141, created_at="2026-10-05T10:00:00Z")))
        jobs = world.listing("build")
        for job in jobs:
            job.update(run_id=141, id=job["id"] + 1_000_000)
        world.api.add_jobs(141, ATTEMPT, jobs)
        record = copy.deepcopy(world.records[BUNDLE])
        record.update(id=BUNDLE + 1, name=other["artifact"]["name"])
        record["workflow_run"]["id"] = 141
        world.api.add_artifact(record, world.archives[BUNDLE])
        document = copy.deepcopy(world.documents["packaged"])
        document["owning_build"] = other
        world.seal("packaged", document)
        self.assert_stops(world, "different complete Build bundle")

    def test_a_record_that_misses_a_planned_lane_stops_the_job(self) -> None:
        world = self.world(lanes=2)
        document = copy.deepcopy(world.documents["packaged"])
        del document["artifacts"][1], document["native_receipts"][1]
        world.seal("packaged", raw=canonical_json(document))
        self.assert_stops(world)

    def test_the_original_test_merge_and_its_timeline_are_proven_not_believed(self) -> None:
        world = self.world()
        then = world.plan["identity"]
        cases = {
            "parents reversed": lambda world: world.api.add_commit(TEST_MERGE, then["tested_tree"],
                                                                   parents=then["tested_parents"][::-1]),
            "one parent": lambda world: world.api.add_commit(TEST_MERGE, then["tested_tree"], parents=[BASE_SHA]),
            "another tree": lambda world: world.api.add_commit(TEST_MERGE, "7" * 40, parents=then["tested_parents"]),
            "outside the base history": lambda world: world.api.add_compare(
                BASE_SHA, world.merged, {"status": "diverged", "ahead_by": 1, "behind_by": 1}),
            "gate before its prerequisites": lambda world: (
                world.job("build", ASSEMBLE).update(completed_at="2026-10-06T10:04:01Z"),
                world.set_jobs("build", world.jobs["build"])),
        }
        for name, change in cases.items():
            world = self.world()
            change(world)
            with self.subTest(case=name):
                self.assert_stops(world)


class ApiFailureTests(ReuseCase):
    def test_an_api_failure_is_never_absence(self) -> None:
        targets = ("/commits/", "/actions/workflows/mod-base-build.yml/runs",
                   "/actions/workflows/mod-base-packaged-e2e.yml/runs",
                   "/actions/runs/142", "/actions/runs/143", "/attempts/2/jobs", "/actions/runs/142/artifacts",
                   "/actions/runs/143/artifacts", f"/actions/artifacts/{BUILD_SEAL}", f"/actions/artifacts/{BUNDLE}",
                   "/pulls/7", "/git/commits/", "/compare/", "/branches/")
        for target in targets:
            world = self.world()
            original = world.api.get_json
            failure = ApiError("GitHub is unavailable", status=502, method="GET", path=target)

            def get(path, target=target, original=original, failure=failure, **arguments):
                if path.endswith(target) or (target.endswith("/") and target in path):
                    raise failure
                return original(path, **arguments)

            with self.subTest(target=target), patch.object(world.api, "get_json", side_effect=get), \
                    self.assertRaises(MbError) as caught:
                self.decide(world)
            self.assertIs(caught.exception, failure)
        world = self.world()
        failure = ApiError("GitHub is unavailable", status=502, method="GET",
                           path=f"/repos/{h.REPOSITORY}/actions/artifacts/{BUILD_SEAL}/zip")
        with patch.object(world.api, "download", side_effect=failure), self.assertRaises(MbError) as caught:
            self.decide(world)
        self.assertIs(caught.exception, failure)

    def test_a_spent_request_budget_stops_the_job(self) -> None:
        for budget in (1, 5, 12, 25):
            world = self.world(max_requests=budget)
            with self.subTest(budget=budget), self.assertRaises(RequestBudgetExhausted):
                self.decide(world)


class EventTests(ReuseCase):
    def test_only_a_push_to_the_default_branch_reuses(self) -> None:
        world = self.world()
        for event in ("workflow_dispatch", "schedule", "pull_request_target"):
            with self.subTest(event=event):
                outcome = self.decide(world, event=event)
                self.assertEqual(outcome, reuse.FullRunRequired("not-a-push", reuse.FULL_RUN_REASONS["not-a-push"]))
        pull_request = self.attempt(listing="build-full")
        outcome = reuse.admit_post_merge_reuse(pull_request.api, plan=pull_request.plan, event="pull_request_target",
                                               temporary_root=self.scratch)
        self.assertEqual(outcome.reason, "not-a-push")
        self.assertEqual((world.api.request_count, pull_request.api.request_count), (0, 0))
        for event in ("release", "pull_request", "", None):
            with self.subTest(event=event), self.assertRaises(MbError):
                self.decide(world, event=event)
        with self.assertRaisesRegex(MbError, "tests the default-branch commit it runs from"):
            reuse.admit_post_merge_reuse(pull_request.api, plan=pull_request.plan, event="push",
                                         temporary_root=self.scratch)
        self.assertEqual((world.api.request_count, pull_request.api.request_count), (0, 0))

    def test_every_reason_has_its_words_and_no_other_is_given(self) -> None:
        self.assertEqual(len(reuse.FULL_RUN_REASONS), 17)
        for reason, words in reuse.FULL_RUN_REASONS.items():
            self.assertRegex(reason, r"^[a-z]+(?:-[a-z]+)+$")
            self.assertEqual(reuse.FullRunRequired(reason, words).detail, words)
        with self.assertRaises(MbError):
            reuse.FullRunRequired("identical-tested-tree", "not a reason for a full run")


if __name__ == "__main__":
    unittest.main()
