"""``ci subject`` end to end: real mod checkout, real state directory, fake GitHub API.

``DerivedSubjectCommandTests`` runs ``ci subject --candidate`` on real Git checkouts of the mod and
of the candidate (``tests/ci_checkout_fixture.py``) and compares what it writes with what the
command without the option writes for the same subject.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, identity, lifecycle
from mod_base.github import api as github_api
from mod_base.workflow import CI_CALLER_WORKFLOWS
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests import ci_checkout_fixture as checkouts
from tests import ci_mod_harness as h


class SubjectCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name)
        self.state = self.temporary / "state"
        self.output = self.temporary / "github-output"
        self.api, self.pull = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
        self.budgets: list[int | None] = []

    def run_subject(self, *, pr: str = "7", producer: str = "build", environment: dict[str, str] | None = None,
                    state: Path | None = None) -> tuple[int, str]:
        """Run ``ci subject`` through the real entry point; return its exit code and stderr."""

        environment = h.environment() if environment is None else environment

        def client(environ, *, writable=False, max_requests=None):
            self.assertEqual(dict(environ), {name: value for name, value in environment.items()})
            self.assertIs(writable, False)  # authenticating a subject writes nothing to GitHub
            self.budgets.append(max_requests)
            return self.api

        argv = ["ci", "subject", "--repo", str(h.MOD), "--config", str(h.MOD / "site" / "mod-base.json"),
                "--state", str(self.state if state is None else state), "--producer", producer, "--pr", pr,
                "--github-output", str(self.output)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "environ", return_value=environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        self.assertEqual(stdout.getvalue(), "")
        return code, stderr.getvalue()

    def test_a_pull_request_writes_the_record_and_outputs_in_four_budgeted_requests(self) -> None:
        self.assertEqual(self.run_subject(), (0, ""))
        self.assertEqual(self.budgets, [limits.MAX_CI_SUBJECT_REQUESTS])
        self.assertLessEqual(limits.MAX_CI_SUBJECT_REQUESTS, 60)
        self.assertEqual(self.api.request_count, 4)
        self.assertEqual(self.api.mutations, [])
        record = identity.read_subject(self.state)
        self.assertEqual((record["producer"], record["event"], record["workflow_path"]),
                         ("build", "pull_request_target", CI_CALLER_WORKFLOWS["build"]))
        self.assertEqual(record["subject"]["pr_number"], 7)
        self.assertEqual((self.state / identity.IDENTITY_NAME).read_bytes(), canonical_json(record))
        self.assertEqual(self.output.read_text(encoding="utf-8"), f"tested_sha={h.TESTED_SHA}\npr_number=7\n")

    def test_pending_merge_fields_wait_then_write_the_same_subject(self) -> None:
        for index, pending in enumerate(({"mergeable": None}, {"merge_commit_sha": None},
                                         {"mergeable": None, "merge_commit_sha": None})):
            with self.subTest(pending=pending):
                self.api, self.pull = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
                h.seed_pull_request(self.api, {**self.pull, **pending})
                self.api.during_listing(f"/repos/{h.REPOSITORY}/pulls/7",
                                        lambda: h.seed_pull_request(self.api, self.pull), after_pages=2)
                state = self.temporary / f"pending-{index}"
                with mock.patch.object(limits, "CI_TEST_MERGE_POLL_SECONDS", 0.001):
                    self.assertEqual(self.run_subject(state=state), (0, ""))
                self.assertEqual(self.api.request_count, 10)
                self.assertEqual(identity.read_subject(state)["subject"]["tested_sha"], h.TESTED_SHA)
                self.assertEqual(self.api.mutations, [])

    def test_pending_merge_exhausts_the_poll_and_wall_time_bounds_without_writing(self) -> None:
        for wait, requests in ((limits.CI_TEST_MERGE_WAIT_SECONDS, 3 * limits.MAX_CI_TEST_MERGE_POLLS), (0, 3)):
            self.api, self.pull = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
            h.seed_pull_request(self.api, {**self.pull, "mergeable": None, "merge_commit_sha": None})
            with self.subTest(wait=wait), mock.patch.object(limits, "CI_TEST_MERGE_POLL_SECONDS", 0.001), \
                    mock.patch.object(limits, "CI_TEST_MERGE_WAIT_SECONDS", wait):
                code, stderr = self.run_subject()
            self.assertEqual(code, 2)
            self.assertIn("ci-test-merge-pending", stderr)
            self.assertIn("rerun the job", stderr)
            self.assertEqual(self.api.request_count, requests)
            self.assertFalse(self.state.exists())
            self.assertFalse(self.output.exists())

    def test_conflicting_or_malformed_merge_state_is_not_polled(self) -> None:
        for mergeable in (False, "unknown", 1):
            self.api, self.pull = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
            h.seed_pull_request(self.api, {**self.pull, "mergeable": mergeable})
            code, stderr = self.run_subject()
            with self.subTest(mergeable=mergeable):
                self.assertEqual(code, 2)
                self.assertEqual(self.api.request_count, 3)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.output.exists())

    def test_a_source_change_during_merge_computation_is_not_admitted(self) -> None:
        h.seed_pull_request(self.api, {**self.pull, "mergeable": None})
        changed = {**self.pull, "head": {**self.pull["head"], "sha": "9" * 40}}
        self.api.during_listing(f"/repos/{h.REPOSITORY}/pulls/7",
                                lambda: h.seed_pull_request(self.api, changed))
        with mock.patch.object(limits, "CI_TEST_MERGE_POLL_SECONDS", 0.001):
            code, stderr = self.run_subject()
        self.assertEqual(code, 2)
        self.assertIn("changed while waiting", stderr)
        self.assertEqual(self.api.request_count, 6)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())

    def test_an_outdated_base_has_an_actionable_reason(self) -> None:
        for stale in ("base field", "merge parent"):
            self.api, self.pull = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
            if stale == "base field":
                h.seed_pull_request(self.api, {**self.pull, "base": {**self.pull["base"], "sha": "9" * 40}})
            else:
                self.api.add_commit(h.TESTED_SHA, h.TESTED_TREE, parents=["9" * 40, h.HEAD_SHA])
            code, stderr = self.run_subject()
            with self.subTest(stale=stale):
                self.assertEqual(code, 2)
                self.assertIn("ci-pr-base-outdated", stderr)
                self.assertIn("update the branch", stderr)
                self.assertEqual(self.api.request_count, 3 if stale == "base field" else 4)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.output.exists())

    def test_an_empty_pr_is_a_protected_subject_in_five_requests(self) -> None:
        self.assertEqual(self.run_subject(pr="", environment=h.environment(event="push")), (0, ""))
        self.assertEqual(self.api.request_count, 5)
        record = identity.read_subject(self.state)
        self.assertEqual((record["event"], record["subject"]["pr_number"], record["subject"]["tested_sha"]),
                         ("push", 0, h.CONTROLLER_SHA))
        self.assertEqual(self.output.read_text(encoding="utf-8"), f"tested_sha={h.CONTROLLER_SHA}\npr_number=\n")

    def test_the_packaged_producer_writes_the_same_subject(self) -> None:
        self.assertEqual(self.run_subject(), (0, ""))
        build = identity.read_subject(self.state)
        self.api, _ = h.github()
        packaged_state = self.temporary / "packaged"
        self.assertEqual(self.run_subject(producer="packaged", environment=h.environment(caller="packaged"),
                                          state=packaged_state), (0, ""))
        packaged = identity.read_subject(packaged_state)
        self.assertEqual(packaged["subject"], build["subject"])
        self.assertEqual(packaged["producer"], "packaged")

    def test_a_rejection_writes_no_state_and_no_output(self) -> None:
        h.seed_pull_request(self.api, {**self.pull, "draft": True})
        code, stderr = self.run_subject()
        self.assertEqual(code, 2)
        self.assertEqual(stderr, "mod_base: draft: pull request 7 is a draft: the caller must defer, not call\n")
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        self.api, _ = h.github(max_requests=3)
        code, stderr = self.run_subject()
        self.assertEqual(code, 2)
        self.assertIn("request-budget", stderr)
        self.assertEqual(self.api.request_count, 3)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.output.exists())

    def test_an_existing_state_directory_is_refused_before_any_output(self) -> None:
        self.state.mkdir(mode=0o700)
        code, stderr = self.run_subject()
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: ci-state: "), stderr)
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertFalse(self.output.exists())

    def test_malformed_flags_are_usage_errors_before_any_request(self) -> None:
        for pr in ("0", "-1", "07", "7.0", "seven", " 7", "7 ", str(2 ** 63)):
            code, stderr = self.run_subject(pr=pr)
            with self.subTest(pr=pr):
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        for producer in ("release", "gate", ""):
            code, stderr = self.run_subject(producer=producer)
            with self.subTest(producer=producer):
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assertEqual((self.api.request_count, self.budgets), (0, []))
        self.assertFalse(self.state.exists())

    def test_the_status_producer_writes_the_subject_of_the_gates_on_every_event_of_its_caller(self) -> None:
        self.assertEqual(self.run_subject(), (0, ""))
        build = identity.read_subject(self.state)
        for event in identity.STATUS_EVENTS:
            self.api, _ = h.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
            state = self.temporary / f"status-{event}"
            self.output.unlink()
            with self.subTest(event=event):
                self.assertEqual(self.run_subject(producer="status", state=state,
                                                  environment=h.environment(event=event, caller="status")), (0, ""))
                record = identity.read_subject(state)
                self.assertEqual((record["producer"], record["event"], record["workflow_path"]),
                                 ("status", event, CI_CALLER_WORKFLOWS["status"]))
                self.assertEqual(record["subject"], build["subject"], "the plan it derives is the plan of the gates")
                self.assertEqual((self.api.request_count, self.api.mutations), (4, []))
                self.assertEqual(self.output.read_text(encoding="utf-8"), f"tested_sha={h.TESTED_SHA}\npr_number=7\n")

    def test_a_status_job_without_a_pull_request_or_outside_its_caller_is_refused_before_any_request(self) -> None:
        cases = {"no pull request": {"pr": "", "environment": h.environment(event="schedule", caller="status")},
                 "the Build caller": {"environment": h.environment()},
                 "a push": {"environment": h.environment(event="push", caller="status")}}
        for label, options in cases.items():
            code, stderr = self.run_subject(producer="status", **options)
            with self.subTest(case=label):
                self.assertEqual(code, 2)
                self.assertFalse(stderr.startswith("mod_base: usage: "), stderr)
                self.assertEqual(self.api.request_count, 0)
                self.assertFalse(self.state.exists())
                self.assertFalse(self.output.exists())


class DerivedSubjectCommandTests(checkouts.GenerationCase):
    """``ci subject --candidate``: the record, the state and the outputs of the full command, from
    one budgeted request."""

    def setUp(self) -> None:
        super().setUp()
        self.budgets: list[int | None] = []

    def run_subject(self, api, name: str, *, candidate: Path | str | None, pr: str = str(checkouts.PULL_REQUEST),
                    producer: str = "build", environment: dict[str, str] | None = None) -> tuple[int, str]:
        """Run ``ci subject`` of the job whose mod checkout is ``self.mod`` through the real entry
        point, with the state ``<name>-state`` and the outputs ``<name>-output``; return its exit
        code and stderr."""

        environment = self.generation.environment() if environment is None else environment

        def client(environ, *, writable=False, max_requests=None):
            self.assertIs(writable, False)
            self.budgets.append(max_requests)
            return api

        argv = ["ci", "subject", "--repo", str(self.mod), "--config", str(self.mod / "site" / "mod-base.json"),
                "--state", str(self.temporary / f"{name}-state"), "--producer", producer, "--pr", pr,
                *(() if candidate is None else ("--candidate", str(candidate))),
                "--github-output", str(self.temporary / f"{name}-output")]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "environ", return_value=environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        self.assertEqual(stdout.getvalue(), "")
        return code, stderr.getvalue()

    def written(self, name: str) -> tuple[bytes, str]:
        """The identity record and the step outputs the command named ``name`` wrote."""

        return ((self.temporary / f"{name}-state" / identity.IDENTITY_NAME).read_bytes(),
                (self.temporary / f"{name}-output").read_text(encoding="utf-8"))

    def assert_nothing_written(self, name: str) -> None:
        self.assertFalse((self.temporary / f"{name}-state").exists())
        self.assertFalse((self.temporary / f"{name}-output").exists())

    def test_a_pull_request_writes_what_the_full_command_writes_from_one_budgeted_request(self) -> None:
        full, _ = self.generation.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
        self.assertEqual(self.run_subject(full, "full", candidate=None), (0, ""))
        api, _ = self.generation.github(max_requests=limits.MAX_CI_DERIVED_SUBJECT_REQUESTS)
        self.assertEqual(self.run_subject(api, "derived", candidate=self.candidate), (0, ""))
        self.assertEqual(self.budgets, [limits.MAX_CI_SUBJECT_REQUESTS, limits.MAX_CI_DERIVED_SUBJECT_REQUESTS])
        # One request, and the budget is that request with every attempt the client may retry it for.
        self.assertEqual(limits.MAX_CI_DERIVED_SUBJECT_REQUESTS, github_api.REQUEST_ATTEMPTS)
        self.assertEqual((full.request_count, api.request_count, api.mutations), (4, 1, []))
        self.assertEqual(api.paths, [f"/repos/{h.REPOSITORY}/pulls/{checkouts.PULL_REQUEST}"])
        self.assertEqual(self.written("derived"), self.written("full"))
        record, outputs = self.written("derived")
        self.assertEqual(outputs, f"tested_sha={self.generation.tested.sha}\npr_number={checkouts.PULL_REQUEST}\n")
        self.assertEqual(record, canonical_json(identity.read_subject(self.temporary / "derived-state")))
        state = self.temporary / "derived-state"
        self.assertEqual((state.stat().st_mode & 0o7777, (state / identity.IDENTITY_NAME).stat().st_mode & 0o7777),
                         (0o700, 0o600))

    def test_a_candidate_waits_for_pending_merge_in_the_same_request_budget(self) -> None:
        api, pull = self.generation.github(max_requests=limits.MAX_CI_DERIVED_SUBJECT_REQUESTS)
        path = f"/repos/{h.REPOSITORY}/pulls/{checkouts.PULL_REQUEST}"
        api.add_response(path, {**pull, "mergeable": None})
        api.during_listing(path, lambda: api.add_response(path, pull))
        with mock.patch.object(limits, "CI_TEST_MERGE_POLL_SECONDS", 0.001):
            self.assertEqual(self.run_subject(api, "pending", candidate=self.candidate), (0, ""))
        self.assertEqual(api.request_count, 2)
        self.assertEqual(api.paths, [path, path])
        self.assertEqual(self.budgets, [limits.MAX_CI_DERIVED_SUBJECT_REQUESTS])
        self.assertEqual(identity.read_subject(self.temporary / "pending-state")["subject"]["tested_sha"],
                         self.generation.tested.sha)

    def test_a_candidate_pending_merge_exhausts_the_bound_without_writing(self) -> None:
        api, pull = self.generation.github(max_requests=limits.MAX_CI_DERIVED_SUBJECT_REQUESTS)
        api.add_response(f"/repos/{h.REPOSITORY}/pulls/{checkouts.PULL_REQUEST}",
                         {**pull, "merge_commit_sha": None})
        with mock.patch.object(limits, "CI_TEST_MERGE_POLL_SECONDS", 0.001):
            code, stderr = self.run_subject(api, "pending", candidate=self.candidate)
        self.assertEqual(code, 2)
        self.assertIn("ci-test-merge-pending", stderr)
        self.assertEqual(api.request_count, limits.MAX_CI_TEST_MERGE_POLLS)
        self.assert_nothing_written("pending")

    def test_a_protected_subject_writes_what_the_full_command_writes_from_one_budgeted_request(self) -> None:
        candidate = checkouts.copy(self.mod, self.temporary / "protected" / "candidate")
        for event in ("push", "workflow_dispatch"):
            environment = self.generation.environment(event=event)
            full, _ = self.generation.github(max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
            self.assertEqual(self.run_subject(full, f"full-{event}", candidate=None, pr="", environment=environment),
                             (0, ""))
            api, _ = self.generation.github(max_requests=limits.MAX_CI_DERIVED_SUBJECT_REQUESTS)
            self.assertEqual(self.run_subject(api, f"derived-{event}", candidate=candidate, pr="",
                                              environment=environment), (0, ""))
            with self.subTest(event=event):
                self.assertEqual((full.request_count, api.request_count), (5, 1))
                self.assertEqual(api.paths, [f"/repos/{h.REPOSITORY}/branches/{h.BRANCH}"])
                self.assertEqual(self.written(f"derived-{event}"), self.written(f"full-{event}"))
                self.assertEqual(self.written(f"derived-{event}")[1],
                                 f"tested_sha={self.generation.controller.sha}\npr_number=\n")

    def test_the_next_steps_of_the_job_open_the_derived_state(self) -> None:
        api, _ = self.generation.github()
        self.assertEqual(self.run_subject(api, "job", candidate=self.candidate), (0, ""))
        invocation = checkouts.invocation(self.mod, self.generation.environment())
        job = lifecycle.open_job(invocation, self.temporary / "job-state")
        self.assertEqual((job.subject["tested_sha"], job.record["controller_tree"], job.sources.controller_sha),
                         (self.generation.tested.sha, self.generation.controller.tree,
                          self.generation.controller.sha))
        # `ci plan --candidate` reads the same checkout with the same Git and finds the tested tree there.
        files = lifecycle.checkout_candidate_files(self.candidate, job)
        self.assertEqual(list(files.values())[0], (self.candidate / "release" / "inventory.json").read_bytes())
        self.assertEqual(api.request_count, 1)

    def test_a_rejection_writes_no_state_and_no_output(self) -> None:
        api, _ = self.generation.github()
        # The mod checkout is at the controller: no candidate of this pull request.
        code, stderr = self.run_subject(api, "elsewhere", candidate=self.mod)
        self.assertEqual(code, 2)
        self.assertIn("the candidate checkout is not at the current test merge of the pull request", stderr)
        self.assertEqual(api.request_count, 1)
        self.assert_nothing_written("elsewhere")
        plain = h.materialize(self.temporary / "plain")
        code, stderr = self.run_subject(api, "plain", candidate=plain)
        self.assertEqual((code, stderr), (2, "mod_base: git: the plain checkout has no Git directory of its own\n"))
        self.assertEqual(api.request_count, 1, "nothing is asked once a checkout cannot be read")
        self.assert_nothing_written("plain")
        api, pull = self.generation.github()
        h.seed_pull_request(api, {**pull, "draft": True})
        code, stderr = self.run_subject(api, "draft", candidate=self.candidate)
        self.assertEqual((code, stderr), (2, f"mod_base: draft: pull request {checkouts.PULL_REQUEST} is a draft: "
                                             "the caller must defer, not call\n"))
        self.assert_nothing_written("draft")

    def test_a_status_job_and_an_empty_candidate_are_refused_before_any_request(self) -> None:
        api, _ = self.generation.github()
        code, stderr = self.run_subject(api, "status", candidate=self.candidate, producer="status",
                                        environment=self.generation.environment(caller="status"))
        self.assertEqual(code, 2)
        self.assertIn("only a Build or packaged job holds a candidate checkout", stderr)
        self.assert_nothing_written("status")
        code, stderr = self.run_subject(api, "empty", candidate="")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assert_nothing_written("empty")
        self.assertEqual((api.request_count, self.budgets), (0, [limits.MAX_CI_DERIVED_SUBJECT_REQUESTS]))


if __name__ == "__main__":
    unittest.main()
