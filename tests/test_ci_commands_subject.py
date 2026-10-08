"""``ci subject`` end to end: real mod checkout, real state directory, fake GitHub API."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, identity
from mod_base.workflow import CI_CALLER_WORKFLOWS
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
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


if __name__ == "__main__":
    unittest.main()
