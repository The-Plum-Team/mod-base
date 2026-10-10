"""``ci select-build`` and ``ci fetch-build`` end to end.

The synthetic mod checkout, the private state directory a job holds after ``ci subject`` and
``ci plan``, real bundle ZIPs and the real filesystem (Linux). Only the GitHub API is faked; the
fixed ``sealed-build`` root is pointed at a temporary directory.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, exports, identity, selection
from mod_base.build_ci.config import load_build_config
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.records import validate_source_selection
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests import ci_mod_harness as h
from tests.helpers import ci_plan
from tests.test_ci_build_selection import later_run
from tests.test_ci_packaged_selection import rebuild
from tests.test_ci_transport import World

PACKAGED = CI_CALLER_WORKFLOWS["packaged"]


def synthetic_plan(subject: dict[str, Any], mod: Path = h.MOD) -> dict[str, Any]:
    """The plan of a subject of the synthetic mod under its real protected policy, with the units
    of the fixture plan (the literal job listings name ``target-a`` and ``lane-a``). ``mod`` is the
    protected checkout the job runs on: its control files are part of the policy."""

    config = load_build_config(mod, repository=h.REPOSITORY)
    plan = ci_plan()
    hashes = {key: plan["identity"][key] for key in ("inventory_blob", "inventory_sha256", "scenario_sha256",
                                                     "runtime_selection_sha256")}
    plan["identity"] = {**subject, "policy_sha256": identity.policy_sha256(config, subject), **hashes}
    plan["profile"] = config.data["profile"]
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


class JobWorld(World):
    """One generation of the synthetic mod on a fake GitHub, seen from run 43, attempt 2 of a
    managed caller: its runs, literal job listings, real archives and private job states."""

    def __init__(self, directory: Path, *, push: bool = False, event: str | None = None,
                 max_requests: int | None = None, caller: str = "packaged", mod: Path = h.MOD) -> None:
        self.directory = directory
        self.event = event or ("push" if push else "pull_request_target")
        self.subject = h.subject(pull_request=not push)
        self.plan = synthetic_plan(self.subject, mod)
        self.config_sha256 = load_build_config(mod, repository=h.REPOSITORY).sha256
        self.api, self.pr = h.github(max_requests=max_requests)
        self.runs, self.jobs, self.records, self.archives = {}, {}, {}, {}
        self.environment = {**h.environment(event=self.event, caller=caller),
                            "GITHUB_RUN_ID": "43", "GITHUB_RUN_ATTEMPT": "2"}
        self.budgets: list[int | None] = []

    def state(self, name: str, *, plan: dict[str, Any] | None = None, producer: str = "packaged",
              caller: str = "packaged") -> Path:
        """A new state directory as ``ci subject`` and ``ci plan`` leave it in one job."""

        state = self.directory / name
        identity.write_subject(state, {"producer": producer, "event": self.event,
                                       "workflow_path": CI_CALLER_WORKFLOWS[caller],
                                       "controller_tree": h.CONTROLLER_TREE, "subject": self.subject})
        identity.write_state_record(state, grammar.CI_PLAN_NAME, canonical_json(self.plan if plan is None else plan))
        return state

    def build(self, **run: Any) -> "JobWorld":
        """Add the finished full Build run of the subject with its complete bundle."""

        self.add_run("build", "build-full", **run)
        self.add_bundle()
        return self


def run_ci(test: unittest.TestCase, world: JobWorld, state: Path, verb: str, *argv: str,
           environment: dict[str, str] | None = None, mod: Path = h.MOD) -> tuple[int, str, bytes]:
    """Run one ``ci`` verb through the real entry point; return its exit code, stderr and stdout."""

    def client(environ, *, writable=False, max_requests=None):
        test.assertIs(writable, False)  # none of these verbs writes to GitHub
        world.budgets.append(max_requests)
        return world.api

    stdout, stderr = io.TextIOWrapper(io.BytesIO()), io.StringIO()
    with mock.patch.object(cli, "environ", return_value=world.environment if environment is None else environment), \
            mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
            mock.patch.object(sys, "stdout", stdout), redirect_stderr(stderr):
        code = cli.main(["ci", verb, "--repo", str(mod), "--config", str(mod / "site" / "mod-base.json"),
                         "--state", str(state), *argv])
    stdout.flush()
    return code, stderr.getvalue(), stdout.buffer.getvalue()


def decoded(raw: bytes) -> Any:
    return strict_loads(raw, label="selection record", max_bytes=limits.MAX_CI_RECORD_BYTES)


class CommandTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fresh()

    def fresh(self) -> None:
        """A new empty directory with the two files ``select-build`` is asked to write."""

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.record = self.directory / "selection.json"
        self.output = self.directory / "github-output"

    def select(self, world: JobWorld, state: Path, *argv: str, **options: Any) -> tuple[int, str]:
        code, stderr, stdout = run_ci(self, world, state, "select-build", *argv, "--output", str(self.record),
                                      "--github-output", str(self.output), **options)
        self.assertEqual(stdout, b"")
        return code, stderr

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines())

    def assert_nothing_written(self, state: Path) -> None:
        self.assertFalse(self.record.exists())
        self.assertFalse(self.output.exists())
        self.assertEqual(sorted(path.name for path in state.iterdir()), [grammar.CI_PLAN_NAME, identity.IDENTITY_NAME])


class SelectBuildCommandTests(CommandTestCase):
    def assert_selected(self, world: JobWorld, state: Path, *, requests: int) -> dict[str, Any]:
        raw = self.record.read_bytes()
        document = validate_source_selection(decoded(raw), plan=world.plan)
        self.assertEqual(raw, canonical_json(document))
        self.assertEqual((self.record.stat().st_mode & 0o777, (state / selection.SELECTION_NAME).read_bytes()),
                         (0o600, raw))
        self.assertEqual((document["build"], document["envelope_sha256"]),
                         (world.bundle, canonical_sha256(world.envelope)))
        request = {key: value for key, value in document["request"].items() if key != "nonce"}
        self.assertEqual(request, {"run_id": 43, "run_attempt": 2, "workflow_path": PACKAGED,
                                   "workflow_ref": grammar.workflow_ref(h.REPOSITORY, PACKAGED, h.BRANCH)})
        self.assertEqual(self.outputs(), {"found": "true", "run_id": str(world.bundle["producer"]["run_id"]),
                                          "selection": raw.decode("utf-8").rstrip("\n")})
        self.assertEqual((world.api.request_count, world.budgets), (requests, [limits.MAX_CI_SELECT_BUILD_REQUESTS]))
        self.assertLess(requests, 60)
        self.assertEqual(world.api.mutations, [])
        self.assertEqual(sorted(path.name for path in state.iterdir()),
                         [grammar.CI_PLAN_NAME, selection.SELECTION_NAME, identity.IDENTITY_NAME])
        return document

    def test_a_pull_request_selects_the_newest_build_of_its_head_in_seventeen_requests(self) -> None:
        for argv in ((), ("--build-run-id", ""), ("--wait-seconds", "60")):
            with self.subTest(argv=argv):
                self.fresh()
                world = JobWorld(self.directory).build()
                state = world.state("input")
                self.assertEqual(self.select(world, state, *argv), (0, ""))
                self.assert_selected(world, state, requests=17)

    def test_selection_rejects_validation_for_another_protected_config(self) -> None:
        world = JobWorld(self.directory)
        world.config_sha256 = "f" * 64
        world.build()
        state = world.state("input")
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertIn("protected execution/input context", stderr)
        self.assert_nothing_written(state)

    def test_a_pull_request_without_a_build_fails_closed_when_its_wait_ends(self) -> None:
        world = JobWorld(self.directory)
        state = world.state("input")
        code, stderr = self.select(world, state, "--wait-seconds", "1")
        self.assertEqual(code, 2)
        self.assertIn("Build wait exhausted the 1-second/observation budget; rerun complete Build and E2E", stderr)
        self.assert_nothing_written(state)

    def test_a_failed_newest_build_rejects_without_an_older_one_and_writes_nothing(self) -> None:
        world = JobWorld(self.directory).build()
        later_run(world)
        state = world.state("input")
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertIn("newest exact Build failed or was cancelled", stderr)
        self.assert_nothing_written(state)

    def test_a_protected_subject_selects_the_newest_build_of_its_commit(self) -> None:
        world = JobWorld(self.directory, push=True).build()
        state = world.state("select")
        self.assertEqual(self.select(world, state), (0, ""))
        self.assert_selected(world, state, requests=15)

    def test_a_protected_subject_without_a_build_answers_found_false(self) -> None:
        for listing in (None, "build-reuse"):
            with self.subTest(listing=listing):
                self.fresh()
                world = JobWorld(self.directory, push=True)
                if listing is not None:
                    world.add_run("build", listing)
                state = world.state("select")
                self.assertEqual(self.select(world, state), (0, ""))
                self.assertEqual(self.output.read_text(encoding="utf-8"), "found=false\nrun_id=\n")
                self.assertFalse(self.record.exists())
                self.assertEqual(sorted(path.name for path in state.iterdir()),
                                 [grammar.CI_PLAN_NAME, identity.IDENTITY_NAME])
                self.assertEqual(world.api.mutations, [])

    def test_a_pending_build_of_a_protected_subject_is_waited_for_and_fails_closed_when_its_wait_ends(self) -> None:
        # The packaged run of a push finds its sibling Build run still running: it waits like a
        # pull request does, within --wait-seconds, and neither rebuilds beside it nor answers
        # found=false while the result is unknown.
        world = JobWorld(self.directory, push=True)
        world.add_run("build", "build-full", status="in_progress", conclusion=None)
        state = world.state("select")
        code, stderr = self.select(world, state, "--wait-seconds", "1")
        self.assertEqual(code, 2)
        self.assertIn("Build wait exhausted the 1-second/observation budget", stderr)
        self.assert_nothing_written(state)
        # The subject (3) and one listing: the one-second wait ends after its first poll.
        self.assertEqual((world.api.request_count, world.budgets), (4, [limits.MAX_CI_SELECT_BUILD_REQUESTS]))

    def test_a_named_build_run_must_be_the_newest_one(self) -> None:
        world = JobWorld(self.directory, push=True, event="workflow_dispatch").build()
        state = world.state("input")
        self.assertEqual(self.select(world, state, "--build-run-id", "42"), (0, ""))
        self.assert_selected(world, state, requests=15)
        self.fresh()
        world = JobWorld(self.directory, push=True).build()
        state = world.state("input")
        code, stderr = self.select(world, state, "--build-run-id", "41")
        self.assertEqual(code, 2)
        self.assertIn("not the newest exact Build of this subject", stderr)
        self.assert_nothing_written(state)

    def test_same_run_selects_the_build_this_run_built(self) -> None:
        world = rebuild(JobWorld(self.directory, push=True))
        state = world.state("input")
        self.assertEqual(self.select(world, state, "--build-run-id", "same-run"), (0, ""))
        document = self.assert_selected(world, state, requests=15)
        self.assertEqual((document["build"]["producer"]["run_id"], document["build"]["producer"]["workflow_path"]),
                         (43, PACKAGED))
        # Seen from another attempt of the packaged run, that Build is not "this run's".
        self.fresh()
        world = rebuild(JobWorld(self.directory, push=True))
        state = world.state("input")
        code, _ = self.select(world, state, "--build-run-id", "same-run",
                              environment={**world.environment, "GITHUB_RUN_ATTEMPT": "3"})
        self.assertEqual(code, 2)
        self.assert_nothing_written(state)

    def test_a_pull_request_names_no_build_run(self) -> None:
        world = JobWorld(self.directory).build()
        state = world.state("input")
        for value in ("42", "same-run"):
            code, stderr = self.select(world, state, "--build-run-id", value)
            with self.subTest(value=value):
                self.assertEqual(code, 2)
                self.assertIn("names none", stderr)
        self.assertEqual(world.api.request_count, 0)
        self.assert_nothing_written(state)

    def test_only_a_job_of_the_packaged_caller_with_its_own_plan_selects(self) -> None:
        world = JobWorld(self.directory, push=True).build()
        state = world.state("build-job", producer="build", caller="build")
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertIn("only the packaged caller selects a Build", stderr)
        other = synthetic_plan(h.subject())
        state = world.state("other-plan", plan=other)
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertIn("the plan belongs to another subject", stderr)
        state = world.state("no-run")
        for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
            for value in (None, "0", "1.0", "x"):
                environment = {key: text for key, text in world.environment.items() if key != name}
                if value is not None:
                    environment[name] = value
                code, stderr = self.select(world, state, environment=environment)
                with self.subTest(name=name, value=value):
                    self.assertEqual(code, 2)
                    self.assertTrue(stderr.startswith("mod_base: environment: "), stderr)
        self.assertEqual(world.api.request_count, 0)
        self.assertFalse(self.record.exists() or self.output.exists())

    def test_a_state_without_identity_or_plan_is_refused_before_any_request(self) -> None:
        world = JobWorld(self.directory).build()
        state = self.directory / "empty"
        identity.create_state(state)
        absent = self.directory / "absent"
        partial = self.directory / "partial"
        identity.write_subject(partial, identity.read_subject(world.state("donor")))
        for directory in (state, absent, partial):
            code, stderr = self.select(world, directory)
            with self.subTest(state=directory.name):
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("mod_base: ci-state: "), stderr)
        self.assertEqual((world.api.request_count, world.budgets), (0, []))

    def test_an_existing_output_file_is_never_replaced(self) -> None:
        world = JobWorld(self.directory).build()
        state = world.state("input")
        self.record.write_bytes(b"kept")
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: output: "), stderr)
        self.assertEqual(self.record.read_bytes(), b"kept")
        self.assertFalse(self.output.exists())

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        world = JobWorld(self.directory, max_requests=6).build()
        state = world.state("input")
        code, stderr = self.select(world, state)
        self.assertEqual(code, 2)
        self.assertIn("request-budget", stderr)
        self.assertEqual(world.api.request_count, 6)
        self.assert_nothing_written(state)

    def test_malformed_flags_are_usage_errors_before_any_request(self) -> None:
        world = JobWorld(self.directory).build()
        state = world.state("input")
        for argv in (("--build-run-id", "latest"), ("--build-run-id", "0"), ("--build-run-id", "042"),
                     ("--build-run-id", "Same-Run"), ("--build-run-id", str(2 ** 63)), ("--wait-seconds", "0"),
                     ("--wait-seconds", "5401"), ("--wait-seconds", "60.0"), ("--wait-seconds", "")):
            code, stderr = self.select(world, state, *argv)
            with self.subTest(argv=argv):
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assertEqual((world.api.request_count, world.budgets), (0, []))
        self.assert_nothing_written(state)


class FetchBuildCommandTests(CommandTestCase):
    def fresh(self) -> None:
        super().fresh()
        (self.directory / "worker").mkdir()
        self.sealed = self.directory / "worker" / "sealed-build"

    def test_fetch_rejects_validation_for_another_protected_config(self) -> None:
        world = JobWorld(self.directory)
        world.config_sha256 = "f" * 64
        world.build()
        document = selection.select_build(world.api, plan=world.plan, run_id=43, run_attempt=2,
                                          workflow_path=PACKAGED, event="pull_request_target",
                                          temporary_root=self.directory)
        self.record.write_bytes(canonical_json(document))
        state = world.state("lane")
        code, stderr = self.fetch(world, state)
        self.assertEqual(code, 2)
        self.assertIn("protected execution/input context", stderr)
        self.assertFalse(self.sealed.exists())
        self.assertFalse((state / selection.SELECTION_NAME).exists())

    def selected(self, world: JobWorld, *argv: str) -> None:
        """Run the ``input`` job's ``select-build`` and forget what it spent."""

        self.assertEqual(self.select(world, world.state("input"), *argv), (0, ""))
        self.spent = world.api.request_count
        world.budgets.clear()

    def fetch(self, world: JobWorld, state: Path, *, selection_file: Path | None = None,
              environment: dict[str, str] | None = None) -> tuple[int, str]:
        with mock.patch.object(exports, "BUILD_VALIDATION_ROOT", self.sealed):
            code, stderr, stdout = run_ci(self, world, state, "fetch-build", "--selection",
                                          str(self.record if selection_file is None else selection_file),
                                          environment=environment)
        self.assertEqual(stdout, b"")
        return code, stderr

    def assert_refused(self, world: JobWorld, state: Path, code: int, stderr: str, message: str) -> None:
        self.assertEqual(code, 2)
        self.assertIn(message, stderr)
        self.assertFalse(self.sealed.exists())
        self.assertEqual(list((self.directory / "worker").iterdir()), [])
        self.assertEqual(sorted(path.name for path in state.iterdir()), [grammar.CI_PLAN_NAME, identity.IDENTITY_NAME])
        self.assertEqual(world.api.mutations, [])

    def test_each_route_materialises_the_selected_bundle_in_the_fixed_root(self) -> None:
        cases = {"pull request": (lambda: JobWorld(self.directory).build(), (), 2),
                 "selected": (lambda: JobWorld(self.directory, push=True).build(), ("--build-run-id", "42"), 2),
                 "rebuilt": (lambda: rebuild(JobWorld(self.directory, push=True)), ("--build-run-id", "same-run"), 2)}
        for name, (build, argv, requests) in cases.items():
            with self.subTest(route=name):
                self.fresh()
                world = build()
                self.selected(world, *argv)
                state = world.state("lane")
                self.assertEqual(self.fetch(world, state), (0, ""))
                self.assertEqual(verify_build_export(self.sealed, plan=world.plan), world.envelope)
                self.assertEqual(self.sealed.stat().st_mode & 0o777, 0o700)
                self.assertEqual(list((self.directory / "worker").iterdir()), [self.sealed])
                self.assertEqual((state / selection.SELECTION_NAME).read_bytes(), self.record.read_bytes())
                self.assertEqual((world.api.request_count - self.spent, world.budgets),
                                 (requests, [limits.MAX_CI_FETCH_BUILD_REQUESTS]))
                self.assertLess(requests, 60)
                self.assertEqual(world.api.mutations, [])

    def test_a_bundle_that_expired_after_selection_is_refused_before_use(self) -> None:
        for push in (False, True):
            with self.subTest(push=push):
                self.fresh()
                world = JobWorld(self.directory, push=push).build()
                self.selected(world)
                world.set_artifact(100, expired=True)
                state = world.state("lane")
                self.assert_refused(world, state, *self.fetch(world, state), "artifact has expired")

    def test_a_lane_consumes_the_selected_bytes_while_the_gate_decides_freshness(self) -> None:
        world = JobWorld(self.directory).build()
        self.selected(world)
        later_run(world, status="queued", conclusion=None)
        state = world.state("lane")
        self.assertEqual(self.fetch(world, state), (0, ""))
        self.assertEqual(world.api.request_count - self.spent, 2)

    def test_another_attempt_of_the_run_never_inherits_the_selection(self) -> None:
        world = JobWorld(self.directory).build()
        self.selected(world)
        state = world.state("lane")
        for name, value in (("GITHUB_RUN_ATTEMPT", "3"), ("GITHUB_RUN_ID", "44")):
            code, stderr = self.fetch(world, state, environment={**world.environment, name: value})
            with self.subTest(name=name):
                self.assert_refused(world, state, code, stderr, "rerun all jobs")
        self.assertEqual(world.api.request_count, self.spent)

    def test_a_missing_malformed_or_foreign_selection_file_is_refused_before_any_request(self) -> None:
        world = JobWorld(self.directory).build()
        self.selected(world)
        state = world.state("lane")
        document = decoded(self.record.read_bytes())
        files = {"missing": None, "empty": b"", "text": b"not json", "array": b"[]\n",
                 "duplicate key": self.record.read_bytes()[:-2] + b',"kind":"mod-base.ci.selection"}\n',
                 "other plan": canonical_json({**document, "plan_sha256": "f" * 64}),
                 "other kind": canonical_json({**document, "kind": "mod-base.ci.gate"})}
        for name, data in files.items():
            path = self.directory / f"selection-{name.replace(' ', '-')}.json"
            if data is not None:
                path.write_bytes(data)
            code, stderr = self.fetch(world, state, selection_file=path)
            with self.subTest(file=name):
                self.assert_refused(world, state, code, stderr, "")
        self.assertEqual(world.api.request_count, self.spent)

    def test_an_existing_fixed_root_is_never_replaced(self) -> None:
        world = JobWorld(self.directory).build()
        self.selected(world)
        self.sealed.mkdir()
        (self.sealed / "kept").write_bytes(b"kept")
        state = world.state("lane")
        code, stderr = self.fetch(world, state)
        self.assertEqual(code, 2)
        self.assertIn("preexisting output", stderr)
        self.assertEqual([path.name for path in self.sealed.iterdir()], ["kept"])
        self.assertFalse((state / selection.SELECTION_NAME).exists())

    def test_a_missing_selection_flag_is_a_usage_error(self) -> None:
        world = JobWorld(self.directory).build()
        state = world.state("lane")
        code, stderr, _ = run_ci(self, world, state, "fetch-build")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assertEqual((world.api.request_count, world.budgets), (0, []))


if __name__ == "__main__":
    unittest.main()
