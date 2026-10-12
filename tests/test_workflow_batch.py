"""``batch.yml``: the batch settlement callee's one job, its steps and their executed shell.

The rules every Build/E2E callee shares are in ``tests/test_workflow_ci_policy.py``. This module
spells out what is particular to the settlement: only the batch caller reaches it, on a push to the
default branch or a manual request there and always for one merged batch pull request; its job
authenticates the default-branch commit it runs at as a consumer of a generation does, derives that
commit's protected plan and then issues ``ci batch-settle --intents`` once; one document goes back
to the caller, and nothing is staged, run, sealed or uploaded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from mod_base.build_ci import identity
from mod_base.errors import MbError
from tests.test_workflow_ci_policy import BATCH_CALLER_REF, CANDIDATE_CHECKOUT, JDKS, CiStepRunner, ci_callee
from tests.test_workflow_policy import PROLOGUE, parse_kit_argv, require_tools, step

NAME = "batch"
JOB = "settle"
SUBJECT = "Authenticate the default-branch commit"
PREPARE = "Fence the host and prepare the worker accounts"
PLAN = "Derive the protected plan"
SETTLE = "Verify the merged batch and derive its closing intents"
FINISH = "Terminate and lock the worker accounts"
#: The steps of the one job, in order: subject, prepare and plan like every job, the settlement with
#: that plan, and the sweep.
STEPS = [*PROLOGUE, SUBJECT, CANDIDATE_CHECKOUT, PREPARE, PLAN, SETTLE, FINISH]
SWEEP = "${{ always() && steps.subject.outcome == 'success' }}"
#: What the jobs API calls the job.
API_NAME = workflow.CI_BATCH_CALL + " / " + workflow.CI_BATCH_JOBS[JOB]
EVENTS = ("push", "workflow_dispatch")


class BatchStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ci_callee(NAME)
        self.jobs = self.document["jobs"]
        self.steps = self.jobs[JOB]["steps"]

    def test_the_pin_and_the_batch_pull_request_go_in_and_the_intents_come_back(self) -> None:
        call = self.document["on"]["workflow_call"]
        self.assertEqual(list(call), ["inputs", "outputs"])
        self.assertEqual(list(call["inputs"]), ["kit-sha", "pr-number"], "nothing else of the event reaches the kit")
        self.assertEqual({name: output["value"] for name, output in call["outputs"].items()},
                         {"intents": "${{ jobs.settle.outputs.intents }}"})
        self.assertEqual(self.jobs[JOB]["outputs"], {"intents": "${{ steps.settle.outputs.intents }}"})
        self.assertEqual({item["id"]: item["name"] for item in self.steps if "id" in item},
                         {"subject": SUBJECT, "prepare": PREPARE, "plan": PLAN, "settle": SETTLE})

    def test_one_job_without_needs_condition_or_matrix(self) -> None:
        self.assertEqual(list(self.jobs), [JOB])
        job = self.jobs[JOB]
        self.assertEqual([key for key in ("needs", "if", "strategy") if key in job], [])
        self.assertEqual(job["timeout-minutes"], "15")

    def test_the_steps_run_in_order_and_only_the_sweep_is_conditional(self) -> None:
        self.assertEqual([item["name"] for item in self.steps], STEPS)
        self.assertEqual({item["name"]: item["if"] for item in self.steps if "if" in item}, {FINISH: SWEEP})
        names = [item["name"] for item in self.steps]
        for absent in (workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP):
            self.assertNotIn(absent, names, "the settlement stages no candidate and seals nothing")
        self.assertNotIn(NAME, workflow.CI_JOB_ARTIFACTS)
        self.assertEqual([item["name"] for item in self.steps[len(PROLOGUE):] if "uses" in item], [CANDIDATE_CHECKOUT])
        self.assertEqual(step(self.steps, CANDIDATE_CHECKOUT)["with"]["ref"], "${{ steps.subject.outputs.tested_sha }}")

    def test_no_step_reads_anything_but_the_two_inputs_and_its_own_token(self) -> None:
        self.assertEqual({item["name"]: sorted(item["env"]) for item in self.steps if "env" in item}, {
            PROLOGUE[0]: ["KIT_SHA", "PR_NUMBER"], PROLOGUE[3]: ["KIT_SHA"], SUBJECT: ["GH_TOKEN"],
            PLAN: ["GH_TOKEN"], SETTLE: ["GH_TOKEN", "PR_NUMBER"]})
        for item in self.steps:
            if "PR_NUMBER" in item.get("env", {}):
                self.assertEqual(item["env"]["PR_NUMBER"], "${{ inputs.pr-number }}", item["name"])

    def test_job_name_equals_the_registry_and_the_batch_caller_is_no_producer(self) -> None:
        self.assertEqual(self.jobs[JOB]["name"], workflow.ci_workflow_template_name(NAME, JOB))
        self.assertEqual(API_NAME, "Verify merged batch / Verify merged batch provenance")
        self.assertEqual(workflow.ci_api_job_name("batch", "evaluate", JOB), API_NAME)
        self.assertEqual(workflow.ci_skipped_call_job_name("batch", "evaluate"), workflow.CI_BATCH_CALL)
        self.assertEqual(workflow.ci_api_job_name("batch", "guard", "verify"),
                         "Verify pinned mod-base / Authenticate the pinned kit")
        self.assertEqual(workflow.CI_BATCH_CALLS["evaluate"], NAME)
        self.assertEqual(workflow.CI_CALLEE_CALLERS[NAME], ("batch",))
        for call in (lambda: workflow.ci_producer(workflow.CI_CALLER_WORKFLOWS["batch"]),
                     lambda: workflow.ci_api_job_name("batch", "close", JOB),
                     lambda: workflow.ci_api_job_name("batch", "prepare", JOB),
                     lambda: workflow.ci_skipped_call_job_name("batch", "locate")):
            with self.assertRaises(MbError):
                call()

    def test_the_events_are_those_a_consumer_subject_is_authenticated_on(self) -> None:
        script = step(self.steps, PROLOGUE[0])["run"]
        self.assertIn("\n  " + " | ".join(EVENTS) + ") ;;\n", script)
        self.assertLessEqual(set(EVENTS), set(identity.GENERATION_EVENTS))


class BatchShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="batch shell ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name), NAME)
        self.steps = ci_callee(NAME)["jobs"][JOB]["steps"]

    def run_step(self, name: str, **overrides: str | None):
        return self.runner.run(step(self.steps, name), **overrides)

    def test_only_the_events_of_the_batch_caller_with_a_batch_pull_request_are_admitted(self) -> None:
        self.assertEqual(self.runner.base["GITHUB_EVENT_NAME"], "push")
        accepted = [{"GITHUB_EVENT_NAME": event} for event in EVENTS]
        accepted += [{"PR_NUMBER": "1"}, {"PR_NUMBER": "9" * 18}]
        rejected = [
            {"KIT_SHA": "b" * 39}, {"KIT_SHA": "B" * 40}, {"KIT_SHA": ""}, {"GITHUB_SHA": "a" * 41},
            {"GITHUB_REF": "refs/tags/v1"}, {"GITHUB_REF": "refs/pull/17/merge"}, {"GITHUB_REF": ""},
            # A pull request's event never reaches a job that holds the batch App's decisions.
            {"GITHUB_EVENT_NAME": "pull_request_target"}, {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "workflow_run"}, {"GITHUB_EVENT_NAME": "schedule"},
            {"GITHUB_EVENT_NAME": "issue_comment"}, {"GITHUB_EVENT_NAME": ""}, {"GITHUB_EVENT_NAME": "push "},
            {"PR_NUMBER": ""}, {"PR_NUMBER": "0"}, {"PR_NUMBER": "017"}, {"PR_NUMBER": "17 "}, {"PR_NUMBER": "17\n18"},
            {"PR_NUMBER": "17; id"}, {"PR_NUMBER": "-1"}, {"PR_NUMBER": "1" * 19},
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

    def test_the_binding_admits_the_batch_caller_of_the_canonical_branch_alone(self) -> None:
        outcome = self.run_step(PROLOGUE[3])
        self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
        self.assertEqual((outcome.exported, outcome.commands), ({"MOD_BASE_KIT_SHA": "b" * 40}, []))
        self.assertEqual(self.runner.base["GITHUB_WORKFLOW_REF"], BATCH_CALLER_REF)
        batch = workflow.CI_CALLER_WORKFLOWS["batch"]
        refused = {
            "the Build caller": "example/mod/" + workflow.CI_CALLER_WORKFLOWS["build"] + "@refs/heads/master",
            "the status caller": "example/mod/" + workflow.CI_CALLER_WORKFLOWS["status"] + "@refs/heads/master",
            "the guard": "example/mod/" + workflow.CI_GUARD_WORKFLOW_PATH + "@refs/heads/master",
            "another branch": f"example/mod/{batch}@refs/heads/feature",
            "a tag": f"example/mod/{batch}@refs/tags/master",
            "another repository": f"attacker/mod/{batch}@refs/heads/master",
            "a longer path": f"example/mod/{batch}x@refs/heads/master",
        }
        for label, reference in refused.items():
            with self.subTest(caller=label):
                outcome = self.run_step(PROLOGUE[3], GITHUB_WORKFLOW_REF=reference)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("not called by the managed batch caller", outcome.result.stderr)
                self.assertEqual(outcome.exported, {})

    def test_the_job_issues_exactly_its_command_lines(self) -> None:
        temp = self.runner.runner_temp
        job = ["--repo", "mod", "--config", "mod/site/mod-base.json", "--state", f"{temp}/mb-state"]
        out = ["--github-output", str(self.runner.output)]
        issued = []
        for item in self.steps[len(PROLOGUE):]:
            if "run" not in item:
                continue
            outcome = self.runner.run(item)
            self.assertEqual(outcome.result.returncode, 0, f"{item['name']}: {outcome.result.stderr}")
            issued.extend(outcome.commands)
        self.assertEqual(issued, [
            # The default-branch commit, as a consumer of its generation: never a pull request.
            ["ci", "subject", *job, "--producer", "generation", "--pr", "", *out],
            ["ci", "worker-prepare", *job, "--roles", "validator", "--python", self.runner.python,
             *(word for home in JDKS.values() for word in ("--java-home", home))],
            ["ci", "plan", *job, "--pin-candidate", "candidate", *out],
            # With the plan `ci plan` left in the state: derive the batch's gates, write nothing.
            ["ci", "batch-settle", *job, "--pr", "17", "--intents", *out],
            ["ci", "worker-finish", *job],
        ])
        self.assertEqual([command[1] for command in issued], list(workflow.CI_JOB_VERBS[NAME][JOB]))
        settle = parse_kit_argv(issued[3])
        self.assertEqual((settle.pr, settle.intents, settle.plan, settle.build_seal, settle.packaged_seal,
                          settle.delete_branches), (17, True, None, None, None, False))
        subject = parse_kit_argv(issued[0])
        self.assertEqual((subject.producer, subject.pr), ("generation", None))

    def test_the_batch_pull_request_is_the_call_input_and_nothing_of_the_event(self) -> None:
        leaked = {"GITHUB_HEAD_REF": "batch/x", "GITHUB_REF_NAME": "18/merge", "PR": "18", "GITHUB_EVENT_PATH": "/dev/null"}
        command = self.run_step(SETTLE, PR_NUMBER="23", **leaked).commands[0]
        self.assertEqual(command[command.index("--pr") + 1], "23")
        self.assertNotIn("18", command)
        # A number that is no number reaches the kit unchanged, as one word, and the kit refuses it.
        command = self.run_step(SETTLE, PR_NUMBER="7 --plan p.json").commands[0]
        self.assertEqual(command[command.index("--pr") + 1], "7 --plan p.json")
        with self.assertRaises(MbError):
            parse_kit_argv(command)

    def test_a_failing_command_fails_the_step(self) -> None:
        for item in self.steps[len(PROLOGUE):]:
            if "run" not in item:
                continue
            with self.subTest(step=item["name"]):
                outcome = self.runner.run(item, STUB_PYTHON3_SCRIPT="raise SystemExit(3)")
                self.assertEqual(outcome.result.returncode, 3, "the step's status is the kit's")

    def test_what_the_settlement_writes_is_what_the_job_returns(self) -> None:
        document = '{"base_branch":"master","members":[],"merged_sha":"' + "a" * 40 + '","pr_number":17}'
        writer = ("with open(arguments[arguments.index('--github-output') + 1], 'a', encoding='utf-8') as stream:\n"
                  "    stream.write('intents=' + os.environ['STUB_DOCUMENT'] + '\\n')\n")
        outcome = self.run_step(SETTLE, STUB_PYTHON3_SCRIPT=writer, STUB_DOCUMENT=document)
        self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
        written = dict(line.split("=", 1) for line in self.runner.output.read_text(encoding="utf-8").splitlines())
        self.assertEqual(written, {"intents": document})


if __name__ == "__main__":
    unittest.main()
