"""``rotate.yml`` (SPEC §5.5): the writable rotation job and its executed shell.

The owner run is authenticated before any checkout (polled until completed, then exactly a
successful default-branch ``pages.yml`` run at the given commit); the mod is checked out sparse
(config data only, no adapter code); the kit runs ``rotate`` alone.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_workflow_policy import (PROLOGUE, ROTATE_OWNER_STEP, TOKEN_VALUE, ShellHarness, callee,
                                        mod_base_commands, parse_kit_argv, require_tools, step)

REPOSITORY = "The-Plum-Team/Block-Pops-Minecraft-Mod"
OWNER_ID = "36042781699"
OWNER_SHA = "a" * 40
WORKFLOW_ID = 12345678
PYTHON_STUB = "raise SystemExit(int(os.environ.get('STUB_EXIT', '0')))"


def owner(**overrides: object) -> dict:
    run = {"id": int(OWNER_ID), "workflow_id": WORKFLOW_ID, "status": "completed", "conclusion": "success",
           "path": ".github/workflows/pages.yml", "event": "workflow_dispatch", "head_branch": "master",
           "head_sha": OWNER_SHA, "head_repository": {"full_name": REPOSITORY}}
    run.update(overrides)
    return run


class RotateStructureTests(unittest.TestCase):
    def test_single_writable_job_runs_only_rotation(self) -> None:
        jobs = callee("rotate")["jobs"]
        self.assertEqual(list(jobs), ["rotate"])
        job = jobs["rotate"]
        self.assertEqual(job["permissions"], {"actions": "write", "contents": "read"})
        names = [item["name"] for item in job["steps"]]
        self.assertEqual(names, [PROLOGUE[0], ROTATE_OWNER_STEP, *PROLOGUE[1:],
                                 "Retire the superseded generation by exact artifact ID"])
        commands = [command for item in job["steps"] if "run" in item for command in mod_base_commands(item["run"])]
        self.assertEqual(commands, ["rotate"])
        mod = step(job["steps"], PROLOGUE[1])
        self.assertEqual(mod["with"]["sparse-checkout"], "site/mod-base.json")
        self.assertEqual(mod["with"]["sparse-checkout-cone-mode"], "false")
        self.assertNotIn("fetch-depth", mod["with"])
        owner_step = step(job["steps"], ROTATE_OWNER_STEP)
        self.assertEqual(owner_step["env"], {"GH_TOKEN": TOKEN_VALUE, "PAGES_RUN_ID": "${{ inputs.pages-run-id }}",
                                             "PAGES_RUN_SHA": "${{ inputs.pages-run-sha }}"})
        self.assertNotIn("python", owner_step["run"])
        retire = step(job["steps"], "Retire the superseded generation by exact artifact ID")
        self.assertIn('--owner-run-id "$PAGES_RUN_ID"', retire["run"])
        self.assertIn('--owner-sha "$PAGES_RUN_SHA" --delete-delay-seconds 1.0', retire["run"])
        self.assertNotIn("--dry-run", retire["run"])
        self.assertNotIn("mod_base_adapter", str(job))


class RotateShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="rotate shell ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.steps = callee("rotate")["jobs"]["rotate"]["steps"]

    def validate(self, **env: str):
        values = {"KIT_SHA": "b" * 40, "GITHUB_SHA": OWNER_SHA, "GITHUB_EVENT_NAME": "workflow_dispatch",
                  "GITHUB_RUN_ID": "36042799999", "PAGES_RUN_ID": OWNER_ID, "PAGES_RUN_SHA": OWNER_SHA, **env}
        return self.harness.run(step(self.steps, PROLOGUE[0])["run"], values)

    def test_call_input_validation(self) -> None:
        self.assertEqual(self.validate().returncode, 0)
        for env in ({"GITHUB_EVENT_NAME": "schedule"}, {"PAGES_RUN_ID": ""}, {"PAGES_RUN_ID": "0"},
                    {"PAGES_RUN_ID": "1 2"}, {"PAGES_RUN_SHA": OWNER_SHA[:39]}, {"PAGES_RUN_SHA": ""},
                    {"PAGES_RUN_ID": "36042799999"}, {"KIT_SHA": "main"}):
            with self.subTest(env=env):
                result = self.validate(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid call input", result.stderr)

    def authenticate(self, runs: object, **env: str):
        fixtures = {
            f"GET repos/{REPOSITORY}": {"body": {"default_branch": "master"}},
            f"GET repos/{REPOSITORY}/actions/runs/{OWNER_ID}": runs,
            f"GET repos/{REPOSITORY}/actions/workflows/pages.yml": {"body": {"id": WORKFLOW_ID}},
        }
        values = {"GH_TOKEN": "t", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": "refs/heads/master",
                  "PAGES_RUN_ID": OWNER_ID, "PAGES_RUN_SHA": OWNER_SHA, **env}
        result = self.harness.run(step(self.steps, ROTATE_OWNER_STEP)["run"], values, fixtures=fixtures)
        records = self.harness.records()
        reads = sum(record.get("route") == f"GET repos/{REPOSITORY}/actions/runs/{OWNER_ID}" for record in records)
        sleeps = [record["argv"] for record in records if record["tool"] == "sleep"]
        return result, reads, sleeps

    def test_a_completed_successful_owner_is_accepted(self) -> None:
        for event in ("workflow_dispatch", "schedule"):
            result, reads, sleeps = self.authenticate({"body": owner(event=event)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((reads, sleeps), (1, []))

    def test_the_owner_is_polled_until_it_completes(self) -> None:
        running = {"body": owner(status="in_progress", conclusion=None)}
        result, reads, sleeps = self.authenticate([running, running, {"body": owner()}])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((reads, sleeps), (3, [["2"], ["2"]]))
        result, reads, sleeps = self.authenticate(running)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((reads, len(sleeps)), (30, 29))

    def test_every_other_owner_is_refused(self) -> None:
        cases = {
            "failed": owner(conclusion="failure"), "cancelled": owner(conclusion="cancelled"),
            "other workflow file": owner(path=".github/workflows/other.yml"),
            "other workflow id": owner(workflow_id=WORKFLOW_ID + 1), "other branch": owner(head_branch="feature"),
            "other commit": owner(head_sha="c" * 40), "push event": owner(event="push"),
            "fork": owner(head_repository={"full_name": "attacker/fork"}), "other run": owner(id=1),
        }
        for label, run in cases.items():
            with self.subTest(case=label):
                result, _, _ = self.authenticate({"body": run})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Rotation owner", result.stderr)
        result, reads, _ = self.authenticate({"body": owner()}, GITHUB_REF="refs/heads/feature")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(reads, 0)

    def test_rotation_invocation(self) -> None:
        env = {"GH_TOKEN": "t", "PAGES_RUN_ID": OWNER_ID, "PAGES_RUN_SHA": OWNER_SHA,
               "STUB_PYTHON3_SCRIPT": PYTHON_STUB,
               "GITHUB_WORKSPACE": str(self.root / "workspace")}
        result = self.harness.run(step(self.steps, "Retire the superseded generation by exact artifact ID")["run"], env,
                                  record_env=("GH_TOKEN", "PYTHONPATH"))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [record for record in self.harness.records() if record["tool"] == "python3"]
        self.assertEqual([call["argv"] for call in calls], [[
            "-P", "-m", "mod_base", "rotate", "--repo", "mod", "--config", "mod/site/mod-base.json", "--owner-run-id",
            OWNER_ID, "--owner-sha", OWNER_SHA, "--delete-delay-seconds", "1.0"]])
        self.assertEqual(calls[0]["env"]["GH_TOKEN"], "t")
        parse_kit_argv(calls[0]["argv"][3:])
        result = self.harness.run(step(self.steps, "Retire the superseded generation by exact artifact ID")["run"],
                                  {**env, "STUB_EXIT": "2"})
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
