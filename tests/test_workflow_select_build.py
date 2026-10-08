"""``select-build.yml``: the selection callee's one job, its steps and their executed shell.

The rules every Build/E2E callee shares are in ``tests/test_workflow_ci_policy.py``. This module
spells out what is particular to the selection: only a protected push or dispatch reaches it, its
one job decides between admitted reuse and the exact existing Build of the subject, three values
go back to the caller, and nothing is staged, run or uploaded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from mod_base.build_ci import graph
from mod_base.build_ci.protocol import PRODUCERS
from tests.helpers import ci_graph_jobs, ci_plan, ci_push_plan
from tests.test_workflow_ci_policy import CANDIDATE_CHECKOUT, JDKS, CiStepRunner, ci_callee
from tests.test_workflow_policy import PROLOGUE, parse_kit_argv, require_tools, step

NAME = "select-build"
SUBJECT = "Authenticate the tested subject"
PREPARE = "Fence the host and prepare the worker accounts"
PLAN = "Derive the protected plan"
REUSE = "Admit post-merge reuse"
SELECT = "Select the exact Build source"
FINISH = "Terminate and lock the worker accounts"
#: The steps of the one job, in order: subject, prepare and plan like every job, then its two own
#: verbs (admit reuse, else select) and the sweep.
STEPS = [*PROLOGUE, SUBJECT, PREPARE, PLAN, REUSE, SELECT, FINISH]
ON_PUSH = "github.event_name == 'push'"
NOT_REUSE = "steps.reuse.outputs.mode != 'reuse'"
#: What the jobs API calls the job, and the calling job when its ``if`` skipped the call.
API_NAME = workflow.CI_SELECT_CALL + " / " + workflow.CI_SELECT_JOBS["select"]
SKIPPED_CALL = workflow.CI_SELECT_CALL
RUNNER_STEPS = ("Set up job", "Complete job")
#: Packaged caller mode -> whether the caller's ``select`` job calls this workflow in that mode.
CALLED = {"pull-request": False, "deferred": False, "selected": True, "rebuilt": True, "reuse": True}


class SelectBuildStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ci_callee(NAME)
        self.jobs = self.document["jobs"]

    def test_the_pin_goes_in_and_three_values_come_back(self) -> None:
        call = self.document["on"]["workflow_call"]
        self.assertEqual(list(call), ["inputs", "outputs"])
        self.assertEqual(list(call["inputs"]), ["kit-sha"], "the subject is the protected ref itself")
        self.assertEqual({name: output["value"] for name, output in call["outputs"].items()}, {
            "mode": "${{ jobs.select.outputs.mode }}", "found": "${{ jobs.select.outputs.found }}",
            "build-run-id": "${{ jobs.select.outputs.build-run-id }}"})
        # The mode is full unless reuse was admitted. In reuse mode the selecting step is skipped,
        # so nothing is found and no run is named: `found` is true or false only in full mode.
        self.assertEqual(self.jobs["select"]["outputs"], {
            "mode": "${{ steps.reuse.outputs.mode == 'reuse' && 'reuse' || 'full' }}",
            "found": "${{ steps.select.outputs.found }}",
            "build-run-id": "${{ steps.select.outputs.run_id }}"})
        steps = self.jobs["select"]["steps"]
        self.assertEqual({item["id"]: item["name"] for item in steps if "id" in item},
                         {"subject": SUBJECT, "prepare": PREPARE, "plan": PLAN, "reuse": REUSE, "select": SELECT})

    def test_one_job_without_needs_condition_or_matrix(self) -> None:
        self.assertEqual(list(self.jobs), ["select"])
        job = self.jobs["select"]
        self.assertEqual([key for key in ("needs", "if", "strategy") if key in job], [])
        self.assertEqual(job["timeout-minutes"], "100")

    def test_steps_run_in_order_and_only_the_two_deciding_steps_are_conditional(self) -> None:
        steps = self.jobs["select"]["steps"]
        self.assertEqual([item["name"] for item in steps], STEPS)
        # Reuse is admitted for a push alone; a Build is looked for unless reuse was admitted, so a
        # dispatch (whose reuse step is skipped and outputs nothing) always selects.
        self.assertEqual({item["name"]: item["if"] for item in steps if "if" in item}, {
            REUSE: ON_PUSH, SELECT: NOT_REUSE,
            FINISH: "${{ always() && steps.subject.outcome == 'success' }}"})
        names = [item["name"] for item in steps]
        for absent in (CANDIDATE_CHECKOUT, workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP):
            self.assertNotIn(absent, names, "the selection stages no candidate and seals nothing")
        self.assertNotIn(NAME, workflow.CI_JOB_ARTIFACTS)

    def test_no_step_reads_anything_but_the_pin_and_its_own_token(self) -> None:
        steps = self.jobs["select"]["steps"]
        self.assertEqual({item["name"]: sorted(item["env"]) for item in steps if "env" in item}, {
            PROLOGUE[0]: ["KIT_SHA"], PROLOGUE[3]: ["KIT_SHA"], SUBJECT: ["GH_TOKEN"], PLAN: ["GH_TOKEN"],
            REUSE: ["GH_TOKEN"], SELECT: ["GH_TOKEN"]})

    def test_job_name_equals_the_registry_and_the_literal_job_listings(self) -> None:
        self.assertEqual(self.jobs["select"]["name"], workflow.ci_workflow_template_name(NAME, "select"))
        self.assertEqual(workflow.ci_api_job_name("packaged", "select", "select"), API_NAME)
        self.assertEqual(workflow.ci_skipped_call_job_name("packaged", "select"), SKIPPED_CALL)
        self.assertEqual(tuple(CALLED), graph.PACKAGED_MODES)
        for mode, called in CALLED.items():
            listed = [entry for entry in ci_graph_jobs(f"packaged-{mode}")
                      if entry["name"] in (API_NAME, SKIPPED_CALL)]
            with self.subTest(listing=mode):
                self.assertEqual([(entry["name"], entry["conclusion"]) for entry in listed],
                                 [(API_NAME, "success") if called else (SKIPPED_CALL, "skipped")])
                # Every step a listing names is a step of the job, in that order; a skipped call lists none.
                named = [entry["name"] for entry in listed[0]["steps"] if entry["name"] not in RUNNER_STEPS]
                steps = iter(STEPS)
                self.assertEqual(bool(named), called)
                self.assertTrue(all(name in steps for name in named), named)

    def test_the_job_is_the_select_call_of_the_run_graph_and_seals_nothing(self) -> None:
        for mode, called in CALLED.items():
            contract = graph.run_graph("packaged", mode)
            plan = ci_plan() if mode in ("pull-request", "deferred") else ci_push_plan()
            conclusions = {entry["name"]: entry["conclusion"] for entry in contract.jobs(plan)}
            with self.subTest(mode=mode):
                self.assertEqual(contract.called()[NAME], called)
                self.assertEqual(conclusions.get(API_NAME), "success" if called else None)
                self.assertEqual(conclusions.get(SKIPPED_CALL), None if called else "skipped")
                self.assertNotIn(API_NAME, contract.sealed_jobs(plan))


class SelectBuildShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="select shell ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name), NAME)
        self.steps = ci_callee(NAME)["jobs"]["select"]["steps"]

    def run_step(self, name: str, **overrides: str | None):
        return self.runner.run(step(self.steps, name), **overrides)

    def test_only_a_protected_push_or_dispatch_is_admitted(self) -> None:
        self.assertEqual(self.runner.base["GITHUB_EVENT_NAME"], "push")
        accepted = [{}, {"GITHUB_EVENT_NAME": "workflow_dispatch"}, {"GITHUB_REF": "refs/heads/release/1.20"}]
        rejected = [
            {"KIT_SHA": "b" * 39}, {"KIT_SHA": "B" * 40}, {"KIT_SHA": ""}, {"GITHUB_SHA": "a" * 41},
            {"GITHUB_REF": "refs/tags/v1"}, {"GITHUB_REF": "refs/pull/17/merge"}, {"GITHUB_REF": ""},
            # A pull request waits for its own Build run inside packaged-e2e.yml: it never selects.
            {"GITHUB_EVENT_NAME": "pull_request_target"}, {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "schedule"}, {"GITHUB_EVENT_NAME": "merge_group"},
            {"GITHUB_EVENT_NAME": "workflow_run"}, {"GITHUB_EVENT_NAME": ""}, {"GITHUB_EVENT_NAME": "push "},
            {"GITHUB_EVENT_NAME": "push\n"}, {"GITHUB_EVENT_NAME": "PUSH"},
        ]
        for overrides in accepted:
            with self.subTest(accepted=overrides):
                outcome = self.run_step(PROLOGUE[0], **overrides)
                self.assertEqual((outcome.result.returncode, outcome.commands), (0, []), outcome.result.stderr)
        for overrides in rejected:
            with self.subTest(rejected=overrides):
                outcome = self.run_step(PROLOGUE[0], **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("Invalid call input", outcome.result.stderr)

    def test_the_binding_admits_the_packaged_caller_that_reaches_this_job(self) -> None:
        outcome = self.run_step(PROLOGUE[3])
        self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
        self.assertEqual((outcome.exported, outcome.commands), ({"MOD_BASE_KIT_SHA": "b" * 40}, []))
        self.assertIn(workflow.CI_CALLER_WORKFLOWS["packaged"], self.runner.base["GITHUB_WORKFLOW_REF"])
        outcome = self.run_step(PROLOGUE[3], GITHUB_WORKFLOW_REF="example/mod/" + workflow.CI_CALLEE_WORKFLOWS[NAME]
                                + "@refs/heads/master")
        self.assertNotEqual(outcome.result.returncode, 0)
        self.assertEqual(outcome.exported, {})

    def test_the_job_issues_exactly_its_command_lines(self) -> None:
        temp = self.runner.runner_temp
        job = ["--repo", "mod", "--config", "mod/site/mod-base.json", "--state", f"{temp}/mb-state"]
        out = ["--github-output", str(self.runner.output)]
        issued = []
        for item in self.steps[len(PROLOGUE):]:
            outcome = self.runner.run(item)
            self.assertEqual(outcome.result.returncode, 0, f"{item['name']}: {outcome.result.stderr}")
            issued.extend(outcome.commands)
        self.assertEqual(issued, [
            ["ci", "subject", *job, "--producer", "packaged", "--pr", "", *out],
            ["ci", "worker-prepare", *job, "--roles", "validator", "--python", self.runner.python,
             *(word for home in JDKS.values() for word in ("--java-home", home))],
            ["ci", "plan", *job, *out],
            ["ci", "reuse-admit", *job, *out],
            # No run is named and nothing is waited for: the exact Build of this subject exists or
            # it does not. The record stays in the job's private state.
            ["ci", "select-build", *job, "--output", f"{temp}/mb-state/build-selection.json", *out],
            ["ci", "worker-finish", *job],
        ])
        self.assertEqual([command[1] for command in issued], list(workflow.CI_JOB_VERBS[NAME]["select"]))

    def test_the_subject_is_a_protected_subject_of_the_packaged_producer(self) -> None:
        self.assertIn("packaged", PRODUCERS)
        for event in ("push", "workflow_dispatch"):
            command = self.run_step(SUBJECT, GITHUB_EVENT_NAME=event).commands[0]
            with self.subTest(event=event):
                parsed = parse_kit_argv(command)
                self.assertEqual((parsed.producer, parsed.pr), ("packaged", None))
        # Not even a leaked pull-request number reaches the kit: the step takes none.
        command = self.run_step(SUBJECT, PR_NUMBER="17").commands[0]
        self.assertEqual(command[command.index("--pr") + 1], "")

    def test_a_missing_tool_or_a_failing_command_fails_the_step(self) -> None:
        for home in JDKS:
            outcome = self.run_step(PREPARE, **{home: None})
            self.assertNotEqual(outcome.result.returncode, 0, home)
            self.assertEqual(outcome.commands, [], "an image without one of the JDKs prepares no worker")
        for item in self.steps[len(PROLOGUE):]:
            with self.subTest(step=item["name"]):
                outcome = self.runner.run(item, STUB_PYTHON3_SCRIPT="raise SystemExit(3)")
                self.assertEqual(outcome.result.returncode, 3, "the step's status is the kit's")


if __name__ == "__main__":
    unittest.main()
