"""``ci worker-validate`` up to the point where it needs an account: its flags, the hook, unit and
roles it admits and the upload directory it refuses.

Real state directories and the synthetic mod of ``tests/ci_mod_harness.py``; only the GitHub API is
a fake. What the command does with accounts, sudo and root runs in ``tests/ci_linux_worker.py``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base import runtime
from mod_base.build_ci import adapter, identity, lifecycle, validation
from mod_base.build_ci.host import HostBoundary
from mod_base.build_ci.toolchain import ToolTreeProof
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h

SUBJECT_REQUESTS = 4
BOTH, ALONE = ("candidate", "validator"), ("validator",)


class JobCase(unittest.TestCase):
    """A job of the fixture pull request on a copy of the synthetic mod, after ``ci subject``."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.mod = h.materialize(self.temporary / "mod")
        self.state = self.temporary / "state"
        self.upload = self.temporary / "mb-upload"
        self.api, _ = h.github()
        self.environment = h.environment()
        self.commands = fixture.Commands(self.mod, self.state, self.api, self.environment)
        self.commands.subject(self.temporary / "github-output")
        self.job = lifecycle.open_job(runtime.build_invocation(self.mod, None, self.environment), self.state)

    def planned(self) -> dict:
        """Record the plan the job would derive (``derive_plan`` runs as a plain process here)."""

        sandbox = h.Sandbox(self.temporary / "pure", protected=self.mod)
        sandbox.subject = self.job.subject
        plan = sandbox.derive_plan()
        identity.write_state_record(self.state, lifecycle.PLAN_NAME, canonical_json(plan))
        return plan

    def worker(self, roles: tuple[str, ...]) -> lifecycle.Worker:
        """The worker record of a job that allocated ``roles``; no such account exists here."""

        accounts = {role: WorkerAccount(role, 2000 + index, 2000 + index, str(WORKER_ROOT / f"{role}-home"))
                    for index, role in enumerate(("candidate", "validator")) if role in roles}
        return lifecycle.Worker(HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755), accounts,
                                "/opt/python/bin/python3", (), ToolTreeProof(("/opt/python",), "a" * 64, 1, 2, 100),
                                self.job.config.sha256)


class CommandTests(JobCase):
    def test_the_hooks_are_the_three_verifications_and_each_names_the_accounts_of_its_job(self) -> None:
        self.assertEqual(lifecycle.VALIDATION_HOOKS, {"verify_target": BOTH, "verify_build": ALONE,
                                                      "verify_runtime": BOTH})
        self.assertEqual(sorted(lifecycle.VALIDATION_HOOKS), sorted(validation.VERIFICATION_HOOKS))
        self.assertEqual(set(lifecycle.VALIDATION_HOOKS.values()), set(lifecycle.ROLE_SETS.values()))
        for hook in lifecycle.VALIDATION_HOOKS:  # Each is a hook the validator runs from the protected copy.
            self.assertEqual(adapter.HOOKS[hook].role, "validator")

    def test_malformed_flags_are_usage_errors_before_anything_is_read(self) -> None:
        output = ("--output", str(self.upload))
        cases = [(), ("--hook", "verify_build"), output, ("--hook", "derive_plan", *output),
                 ("--hook", "build_target", "--unit", "1.20.1", *output), ("--hook", "verify", *output),
                 ("--hook", "verify_target", "--unit", "", *output),
                 ("--hook", "verify_target", "--unit", "Fabric-1.20.1", *output),
                 ("--hook", "verify_target", "--unit", "a--b", *output),
                 ("--hook", "verify_target", "--unit", "../1.20.1", *output),
                 ("--hook", "verify_target", "--unit", "x" * 81, *output),
                 ("--hook", "verify_build", "--output", ""), ("--hook", "verify_build", *output, "--roles", "validator"),
                 ("--hook", "verify_build", *output, "--github-output", "out"), ("--hook", "verify_build", *output, "extra")]
        for arguments in cases:
            code, stdout, stderr = self.commands.run("worker-validate", *arguments)
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assertFalse(self.upload.exists())
        self.assertEqual({path.name for path in self.state.iterdir()}, {"identity.json"})
        self.assertEqual(self.api.request_count, SUBJECT_REQUESTS)

    def test_a_job_without_a_prepared_worker_verifies_nothing_and_asks_the_api_nothing(self) -> None:
        for arguments in (("--hook", "verify_build"), ("--hook", "verify_target", "--unit", "1.20.1"),
                          ("--hook", "verify_runtime", "--unit", "fabric-1.20.1")):
            code, stdout, stderr = self.commands.run("worker-validate", *arguments, "--output", str(self.upload))
            with self.subTest(arguments=arguments):
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("ci-state: cannot read the state record worker.json", stderr)
        self.assertFalse(self.upload.exists())
        self.assertEqual((self.api.request_count, self.commands.budgets),
                         (SUBJECT_REQUESTS, [limits.MAX_CI_SUBJECT_REQUESTS]))
        code, stdout, stderr = self.commands.run("worker-validate", "--hook", "verify_build", "--output",
                                                 str(self.upload), state=self.temporary / "no-such-state")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("ci-state: cannot open the state directory", stderr)


class AdmissionTests(JobCase):
    """What ``validate_export`` settles from the state alone, before it touches the host."""

    def test_each_hook_verifies_its_unit_of_the_plan_in_its_kind_of_job(self) -> None:
        plan = self.planned()
        self.assertEqual(lifecycle.validation_unit(plan, hook="verify_target", unit_id="1.21.1", roles=BOTH),
                         plan["targets"][1])
        self.assertEqual(lifecycle.validation_unit(plan, hook="verify_runtime", unit_id="forge-1.20.1", roles=BOTH),
                         plan["lanes"][1])
        self.assertIsNone(lifecycle.validation_unit(plan, hook="verify_build", unit_id=None, roles=ALONE))

    def test_a_hook_in_a_job_with_other_accounts_is_refused(self) -> None:
        plan = self.planned()
        for hook, unit_id, roles in (("verify_build", None, BOTH), ("verify_target", "1.20.1", ALONE),
                                     ("verify_runtime", "fabric-1.20.1", ALONE), ("verify_build", None, ()),
                                     ("verify_target", "1.20.1", ("validator", "candidate")),
                                     ("verify_target", "1.20.1", ("candidate",))):
            with self.subTest(hook=hook, roles=roles), self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.validation_unit(plan, hook=hook, unit_id=unit_id, roles=roles)
            self.assertIn(f"{hook} runs in a job that allocated "
                          f"{' and '.join(lifecycle.VALIDATION_HOOKS[hook])}", str(caught.exception))

    def test_a_unit_outside_the_plan_or_of_the_other_kind_and_any_other_hook_are_refused(self) -> None:
        plan = self.planned()
        wrong = [("verify_target", "1.19.4", BOTH), ("verify_target", "fabric-1.20.1", BOTH),
                 ("verify_target", None, BOTH), ("verify_runtime", "1.20.1", BOTH),
                 ("verify_runtime", "quilt-1.20.1", BOTH), ("verify_runtime", None, BOTH),
                 ("verify_build", "1.20.1", ALONE), ("derive_plan", None, ALONE),
                 ("derive_runtime", "fabric-1.20.1", ALONE), ("build_target", "1.20.1", BOTH),
                 ("run_lane", "fabric-1.20.1", BOTH), ("policy", None, BOTH), ("verify", None, ALONE),
                 (None, None, ALONE)]
        for hook, unit_id, roles in wrong:
            with self.subTest(hook=hook, unit_id=unit_id), self.assertRaises(MbError):
                lifecycle.validation_unit(plan, hook=hook, unit_id=unit_id, roles=roles)

    def test_an_existing_upload_directory_is_refused_before_the_plan_or_the_worker_is_looked_at(self) -> None:
        # No plan was recorded and the worker is nothing at all: neither may be reached.
        for existing in ("directory", "file", "dangling link"):
            if existing == "directory":
                self.upload.mkdir()
                (self.upload / "earlier").write_bytes(b"kept")
            elif existing == "file":
                self.upload.write_bytes(b"kept")
            else:
                self.upload.symlink_to(self.temporary / "nowhere")
            log: list[str] = []
            with self.subTest(existing=existing), self.assertRaises(lifecycle.LifecycleError) as caught:
                lifecycle.validate_export(self.job, None, hook="verify_build", unit_id=None, output=self.upload,
                                          log=log.append)
            self.assertIn("the upload directory exists already", str(caught.exception))
            self.assertEqual(log, [])
            if existing == "directory":
                self.assertEqual((self.upload / "earlier").read_bytes(), b"kept")
                (self.upload / "earlier").unlink()
                self.upload.rmdir()
            else:
                self.assertTrue(self.upload.is_symlink() or self.upload.read_bytes() == b"kept")
                self.upload.unlink()
        self.assertEqual({path.name for path in self.state.iterdir()}, {"identity.json"})

    def test_the_hook_unit_and_roles_are_settled_from_the_plan_before_any_account_is_touched(self) -> None:
        plan = self.planned()
        log: list[str] = []
        cases = [("verify_target", "1.20.1", ALONE, "this job allocated validator"),
                 ("verify_build", None, BOTH, "this job allocated candidate and validator"),
                 ("verify_runtime", "quilt-1.20.1", BOTH, "outside the protected plan"),
                 ("verify_build", "1.20.1", ALONE, "verify_build runs for no unit")]
        for hook, unit_id, roles, message in cases:
            with self.subTest(hook=hook, roles=roles), self.assertRaises(MbError) as caught:
                lifecycle.validate_export(self.job, self.worker(roles), hook=hook, unit_id=unit_id,
                                          output=self.upload, log=log.append)
            self.assertIn(message, str(caught.exception))
        self.assertEqual(log, [])
        self.assertFalse(self.upload.exists())
        self.assertEqual(lifecycle.read_plan(self.job), plan)

    def test_without_a_plan_there_is_nothing_to_verify(self) -> None:
        with self.assertRaises(MbError) as caught:
            lifecycle.validate_export(self.job, self.worker(ALONE), hook="verify_build", unit_id=None,
                                      output=self.upload, log=lambda _line: None)
        self.assertIn("cannot read the state record ci-plan.json", str(caught.exception))
        self.assertFalse(self.upload.exists())


if __name__ == "__main__":
    unittest.main()
