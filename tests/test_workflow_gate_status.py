"""``gate-status.yml``: the status evaluation callee's one job, its steps and their executed shell.

The rules every Build/E2E callee shares are in ``tests/test_workflow_ci_policy.py``. This module
spells out what is particular to the evaluation: only the status caller reaches it, on the four
events that start that caller and always for one pull request; its job issues ``ci gate-status``
twice, first to settle what needs no plan and then, only if that was not enough, after it
authenticated the pull request and derived the plan; one document goes back to the caller, and
nothing is staged, run, sealed or uploaded.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import workflow
from mod_base.build_ci import identity
from mod_base.errors import MbError
from tests.test_managed_ci_callers import evaluate, interpolate, truthy
from tests.test_workflow_ci_policy import (CANDIDATE_CHECKOUT, JDKS, SETTLE_VERBS, STATUS_CALLER_REF, UNSETTLED,
                                           CiStepRunner, ci_callee)
from tests.test_workflow_policy import PROLOGUE, parse_kit_argv, require_tools, step

NAME = "gate-status"
JOB = "evaluate"
SETTLE = "Settle the gates that need no plan"
SUBJECT = "Authenticate the pull request under evaluation"
PREPARE = "Fence the host and prepare the worker accounts"
PLAN = "Derive the protected plan"
EVALUATE = "Evaluate the protected gates"
FINISH = "Terminate and lock the worker accounts"
#: The steps of the one job, in order: the settling call, then (only when it could not settle)
#: subject, prepare and plan like every job, the evaluation with that plan, and the sweep.
STEPS = [*PROLOGUE, SETTLE, SUBJECT, PREPARE, PLAN, EVALUATE, FINISH]
SWEEP = "${{ always() && steps.subject.outcome == 'success' }}"
#: What the jobs API calls the job, and the calling job when its ``if`` skipped the call.
API_NAME = workflow.CI_STATUS_CALL + " / " + workflow.CI_STATUS_JOBS[JOB]
EVENTS = ("workflow_run", "pull_request_target", "schedule", "workflow_dispatch")


def run_job(steps: list[Mapping[str, Any]], outcomes: Mapping[str, str],
            written: Mapping[str, Mapping[str, str]]) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """The outcome of every step of the job as the runner decides it, and what each step wrote.

    ``outcomes`` names the steps that fail and ``written`` the outputs a step that ran wrote. A
    step runs when its ``if:`` is true; without a status function ``success()`` is implied, that
    is no earlier step failed. A skipped step has the outcome ``skipped`` and no output."""

    results: dict[str, str] = {}
    context: dict[str, dict[str, Any]] = {}
    failed = False
    for item in steps:
        text = item.get("if")
        functions = {"always": lambda: True, "success": lambda failed=failed: not failed}
        if text is None:
            runs = not failed
        elif text.startswith("${{"):
            runs = truthy(evaluate(text[3:-2], {"steps": context}, functions))
            assert "always()" in text, "an expression that names no status function still implies success()"
        else:
            runs = not failed and truthy(evaluate(text, {"steps": context}, functions))
        outcome = outcomes.get(item["name"], "success") if runs else "skipped"
        failed = failed or outcome == "failure"
        results[item["name"]] = outcome
        if "id" in item:
            context[item["id"]] = {"outcome": outcome,
                                   "outputs": dict(written.get(item["name"], {})) if runs else {}}
    return results, {name: value["outputs"] for name, value in context.items()}


class GateStatusStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ci_callee(NAME)
        self.jobs = self.document["jobs"]
        self.steps = self.jobs[JOB]["steps"]

    def test_the_pin_and_the_pull_request_go_in_and_the_intents_come_back(self) -> None:
        call = self.document["on"]["workflow_call"]
        self.assertEqual(list(call), ["inputs", "outputs"])
        self.assertEqual(list(call["inputs"]), ["kit-sha", "pr-number"], "nothing else of the event reaches the kit")
        self.assertEqual({name: output["value"] for name, output in call["outputs"].items()},
                         {"intents": "${{ jobs.evaluate.outputs.intents }}"})
        # One document: the settling call's when it settled, else the planned evaluation's. Only
        # one of the two steps ever writes it.
        self.assertEqual(self.jobs[JOB]["outputs"],
                         {"intents": "${{ steps.settle.outputs.intents || steps.evaluate.outputs.intents }}"})
        self.assertEqual({item["id"]: item["name"] for item in self.steps if "id" in item},
                         {"settle": SETTLE, "subject": SUBJECT, "prepare": PREPARE, "plan": PLAN,
                          "evaluate": EVALUATE})

    def test_one_job_without_needs_condition_or_matrix(self) -> None:
        self.assertEqual(list(self.jobs), [JOB])
        job = self.jobs[JOB]
        self.assertEqual([key for key in ("needs", "if", "strategy") if key in job], [])
        self.assertEqual(job["timeout-minutes"], "15")

    def test_everything_after_the_settling_call_depends_on_its_answer(self) -> None:
        self.assertEqual([item["name"] for item in self.steps], STEPS)
        self.assertEqual({item["name"]: item["if"] for item in self.steps if "if" in item}, {
            SUBJECT: UNSETTLED, PREPARE: UNSETTLED, PLAN: UNSETTLED, EVALUATE: UNSETTLED, FINISH: SWEEP})
        self.assertEqual(UNSETTLED, "steps.settle.outputs.settled == 'false'")
        self.assertEqual(SETTLE_VERBS[(NAME, JOB)], ("gate-status",))
        names = [item["name"] for item in self.steps]
        for absent in (CANDIDATE_CHECKOUT, workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP):
            self.assertNotIn(absent, names, "the evaluation stages no candidate and seals nothing")
        self.assertNotIn(NAME, workflow.CI_JOB_ARTIFACTS)
        self.assertFalse(any("uses" in item for item in self.steps[len(PROLOGUE):]), "kit commands only")

    def test_no_step_reads_anything_but_the_two_inputs_and_its_own_token(self) -> None:
        self.assertEqual({item["name"]: sorted(item["env"]) for item in self.steps if "env" in item}, {
            PROLOGUE[0]: ["KIT_SHA", "PR_NUMBER"], PROLOGUE[3]: ["KIT_SHA"], SETTLE: ["GH_TOKEN", "PR_NUMBER"],
            SUBJECT: ["GH_TOKEN", "PR_NUMBER"], PLAN: ["GH_TOKEN"], EVALUATE: ["GH_TOKEN", "PR_NUMBER"]})
        for item in self.steps:
            if "PR_NUMBER" in item.get("env", {}):
                self.assertEqual(item["env"]["PR_NUMBER"], "${{ inputs.pr-number }}", item["name"])

    def test_job_name_equals_the_registry_and_the_status_caller_is_no_producer(self) -> None:
        self.assertEqual(self.jobs[JOB]["name"], workflow.ci_workflow_template_name(NAME, JOB))
        self.assertEqual(API_NAME, "Evaluate protected gates / Evaluate gate states")
        self.assertEqual(workflow.ci_api_job_name("status", "evaluate", JOB), API_NAME)
        self.assertEqual(workflow.ci_skipped_call_job_name("status", "evaluate"), workflow.CI_STATUS_CALL)
        self.assertEqual(workflow.ci_api_job_name("status", "guard", "verify"),
                         "Verify pinned mod-base / Authenticate the pinned kit")
        self.assertEqual(workflow.CI_STATUS_CALLS["evaluate"], NAME)
        self.assertEqual(workflow.CI_CALLEE_CALLERS[NAME], ("status",))
        for call in (lambda: workflow.ci_producer(workflow.CI_CALLER_WORKFLOWS["status"]),
                     lambda: workflow.ci_api_job_name("status", "publish", JOB),
                     lambda: workflow.ci_api_job_name("build", "shared", JOB),
                     lambda: workflow.ci_skipped_call_job_name("status", "locate")):
            with self.assertRaises(MbError):
                call()

    def test_the_events_are_those_the_status_producer_is_authenticated_on(self) -> None:
        script = step(self.steps, PROLOGUE[0])["run"]
        self.assertIn("\n  " + " | ".join(EVENTS) + ") ;;\n", script)
        self.assertEqual(identity.STATUS_EVENTS, EVENTS)


class GateStatusFlowTests(unittest.TestCase):
    """The two phases, decided by the runner's own rules for ``if:`` and by what the calls write."""

    def setUp(self) -> None:
        self.steps = ci_callee(NAME)["jobs"][JOB]["steps"]
        self.output = ci_callee(NAME)["jobs"][JOB]["outputs"]["intents"]

    def run_flow(self, outcomes: Mapping[str, str], written: Mapping[str, Mapping[str, str]]) -> tuple[dict, str]:
        results, outputs = run_job(self.steps, outcomes, written)
        intents = interpolate(self.output, {"steps": {name: {"outputs": value} for name, value in outputs.items()}})
        return {name: outcome for name, outcome in results.items() if name not in PROLOGUE}, intents

    def test_a_settled_first_call_ends_the_job_with_its_document(self) -> None:
        results, intents = self.run_flow({}, {SETTLE: {"settled": "true", "intents": "settled-document"},
                                              EVALUATE: {"intents": "never"}})
        self.assertEqual(results, {SETTLE: "success", SUBJECT: "skipped", PREPARE: "skipped", PLAN: "skipped",
                                   EVALUATE: "skipped", FINISH: "skipped"})
        self.assertEqual(intents, "settled-document")

    def test_an_unsettled_first_call_plans_and_returns_the_second_document(self) -> None:
        results, intents = self.run_flow({}, {SETTLE: {"settled": "false"},
                                              EVALUATE: {"intents": "verified-document"}})
        self.assertEqual(set(results.values()), {"success"})
        self.assertEqual(list(results), [SETTLE, SUBJECT, PREPARE, PLAN, EVALUATE, FINISH])
        self.assertEqual(intents, "verified-document")

    def test_a_failed_step_returns_no_document_and_the_sweep_follows_the_subject(self) -> None:
        unsettled = {SETTLE: {"settled": "false"}, EVALUATE: {"intents": "verified-document"}}
        expected = {
            SETTLE: ({SETTLE: "failure", SUBJECT: "skipped", PREPARE: "skipped", PLAN: "skipped",
                      EVALUATE: "skipped", FINISH: "skipped"}),
            SUBJECT: ({SETTLE: "success", SUBJECT: "failure", PREPARE: "skipped", PLAN: "skipped",
                       EVALUATE: "skipped", FINISH: "skipped"}),
            PREPARE: ({SETTLE: "success", SUBJECT: "success", PREPARE: "failure", PLAN: "skipped",
                       EVALUATE: "skipped", FINISH: "success"}),
            PLAN: ({SETTLE: "success", SUBJECT: "success", PREPARE: "success", PLAN: "failure",
                    EVALUATE: "skipped", FINISH: "success"}),
            EVALUATE: ({SETTLE: "success", SUBJECT: "success", PREPARE: "success", PLAN: "success",
                        EVALUATE: "failure", FINISH: "success"}),
        }
        for failing, outcomes in expected.items():
            # A step that fails wrote nothing the job returns.
            written = {name: value for name, value in unsettled.items() if name != failing}
            with self.subTest(failing=failing):
                results, intents = self.run_flow({failing: "failure"}, written)
                self.assertEqual((results, intents), (outcomes, ""))

    def test_an_answer_that_is_neither_true_nor_false_plans_nothing(self) -> None:
        for answer in ({}, {"settled": ""}, {"settled": "FALSE "}, {"settled": "no"}, {"settled": "0"}):
            with self.subTest(answer=answer):
                results, intents = self.run_flow({}, {SETTLE: answer, EVALUATE: {"intents": "never"}})
                self.assertEqual([results[name] for name in (SUBJECT, PREPARE, PLAN, EVALUATE, FINISH)],
                                 ["skipped"] * 5)
                self.assertEqual(intents, "")


class GateStatusShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="status shell ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name), NAME)
        self.steps = ci_callee(NAME)["jobs"][JOB]["steps"]

    def run_step(self, name: str, **overrides: str | None):
        return self.runner.run(step(self.steps, name), **overrides)

    def test_only_the_events_of_the_status_caller_with_a_pull_request_are_admitted(self) -> None:
        self.assertEqual(self.runner.base["GITHUB_EVENT_NAME"], "pull_request_target")
        accepted = [{"GITHUB_EVENT_NAME": event} for event in EVENTS]
        accepted += [{"PR_NUMBER": "1"}, {"PR_NUMBER": "9" * 18}, {"GITHUB_REF": "refs/heads/release/1.20"}]
        rejected = [
            {"KIT_SHA": "b" * 39}, {"KIT_SHA": "B" * 40}, {"KIT_SHA": ""}, {"GITHUB_SHA": "a" * 41},
            {"GITHUB_REF": "refs/tags/v1"}, {"GITHUB_REF": "refs/pull/17/merge"}, {"GITHUB_REF": ""},
            # A gate run is no status run, and nothing else starts the status caller.
            {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_EVENT_NAME": "pull_request"},
            {"GITHUB_EVENT_NAME": "merge_group"}, {"GITHUB_EVENT_NAME": "issue_comment"},
            {"GITHUB_EVENT_NAME": "workflow_call"}, {"GITHUB_EVENT_NAME": ""}, {"GITHUB_EVENT_NAME": "schedule "},
            {"GITHUB_EVENT_NAME": "workflow_run\n"}, {"GITHUB_EVENT_NAME": "SCHEDULE"},
            # Every evaluation names its pull request: there is no protected subject to evaluate.
            {"PR_NUMBER": ""}, {"PR_NUMBER": "0"}, {"PR_NUMBER": "017"}, {"PR_NUMBER": "17 "}, {"PR_NUMBER": "17\n18"},
            {"PR_NUMBER": "17; id"}, {"PR_NUMBER": "-1"}, {"PR_NUMBER": "1" * 19}, {"PR_NUMBER": "[17]"},
        ]
        for event in EVENTS:
            rejected.append({"GITHUB_EVENT_NAME": event, "PR_NUMBER": ""})
        for overrides in accepted:
            with self.subTest(accepted=overrides):
                outcome = self.run_step(PROLOGUE[0], **overrides)
                self.assertEqual((outcome.result.returncode, outcome.commands), (0, []), outcome.result.stderr)
        for overrides in rejected:
            with self.subTest(rejected=overrides):
                outcome = self.run_step(PROLOGUE[0], **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("Invalid call input", outcome.result.stderr)

    def test_the_binding_admits_the_status_caller_of_the_canonical_branch_alone(self) -> None:
        outcome = self.run_step(PROLOGUE[3])
        self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
        self.assertEqual((outcome.exported, outcome.commands), ({"MOD_BASE_KIT_SHA": "b" * 40}, []))
        self.assertEqual(self.runner.base["GITHUB_WORKFLOW_REF"], STATUS_CALLER_REF)
        status = workflow.CI_CALLER_WORKFLOWS["status"]
        refused = {
            "the Build caller": "example/mod/" + workflow.CI_CALLER_WORKFLOWS["build"] + "@refs/heads/master",
            "the packaged caller": "example/mod/" + workflow.CI_CALLER_WORKFLOWS["packaged"] + "@refs/heads/master",
            "the guard": "example/mod/" + workflow.CI_GUARD_WORKFLOW_PATH + "@refs/heads/master",
            "the callee itself": "example/mod/" + workflow.CI_CALLEE_WORKFLOWS[NAME] + "@refs/heads/master",
            "another branch": f"example/mod/{status}@refs/heads/feature",
            "a tag": f"example/mod/{status}@refs/tags/master",
            "a pull request ref": f"example/mod/{status}@refs/pull/17/merge",
            "another repository": f"attacker/mod/{status}@refs/heads/master",
            "a longer path": f"example/mod/{status}x@refs/heads/master",
        }
        for label, reference in refused.items():
            with self.subTest(caller=label):
                outcome = self.run_step(PROLOGUE[3], GITHUB_WORKFLOW_REF=reference)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("not called by the managed gate status caller", outcome.result.stderr)
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
            # Before a subject exists: no plan, no state, and an answer only when none is needed.
            ["ci", "gate-status", *job, "--pr", "17", "--settle", *out],
            ["ci", "subject", *job, "--producer", "status", "--pr", "17", *out],
            ["ci", "worker-prepare", *job, "--roles", "validator", "--python", self.runner.python,
             *(word for home in JDKS.values() for word in ("--java-home", home))],
            ["ci", "plan", *job, *out],
            # With the plan `ci plan` left in the state: the evaluation that can verify a gate.
            ["ci", "gate-status", *job, "--pr", "17", *out],
            ["ci", "worker-finish", *job],
        ])
        self.assertEqual([command[1] for command in issued], list(workflow.CI_JOB_VERBS[NAME][JOB]))

    def test_only_the_first_call_settles_and_the_subject_is_the_status_producer_s(self) -> None:
        self.assertIn("status", identity.SUBJECT_PRODUCERS)
        for event in EVENTS:
            with self.subTest(event=event):
                first = parse_kit_argv(self.run_step(SETTLE, GITHUB_EVENT_NAME=event).commands[0])
                second = parse_kit_argv(self.run_step(EVALUATE, GITHUB_EVENT_NAME=event).commands[0])
                subject = parse_kit_argv(self.run_step(SUBJECT, GITHUB_EVENT_NAME=event).commands[0])
                self.assertEqual((first.settle, first.pr, second.settle, second.pr), (True, 17, False, 17))
                self.assertEqual((subject.producer, subject.pr), ("status", 17))
                self.assertEqual(first.state, second.state, "one private state directory for the job")

    def test_the_pull_request_is_the_call_input_and_nothing_of_the_event(self) -> None:
        leaked = {"GITHUB_HEAD_REF": "feature/x", "GITHUB_BASE_REF": "master", "GITHUB_REF_NAME": "18/merge",
                  "PR": "18", "GITHUB_EVENT_PATH": "/dev/null"}
        for name in (SETTLE, SUBJECT, EVALUATE):
            command = self.run_step(name, PR_NUMBER="23", **leaked).commands[0]
            with self.subTest(step=name):
                self.assertEqual(command[command.index("--pr") + 1], "23")
                self.assertNotIn("18", command)
        # A number that is no number reaches the kit unchanged, as one word, and the kit refuses it.
        command = self.run_step(SETTLE, PR_NUMBER="7 --settle --pr 9").commands[0]
        self.assertEqual(command[command.index("--pr") + 1], "7 --settle --pr 9")
        with self.assertRaises(MbError):
            parse_kit_argv(command)

    def test_a_missing_tool_or_a_failing_command_fails_the_step(self) -> None:
        for home in JDKS:
            outcome = self.run_step(PREPARE, **{home: None})
            self.assertNotEqual(outcome.result.returncode, 0, home)
            self.assertEqual(outcome.commands, [], "an image without one of the JDKs prepares no worker")
        for item in self.steps[len(PROLOGUE):]:
            with self.subTest(step=item["name"]):
                outcome = self.runner.run(item, STUB_PYTHON3_SCRIPT="raise SystemExit(3)")
                self.assertEqual(outcome.result.returncode, 3, "the step's status is the kit's")

    def test_what_the_calls_write_is_what_the_job_returns(self) -> None:
        document = '{"gates":{},"pr_number":17,"repository":"example/mod","target_sha":"' + "a" * 40 + '"}'
        writer = ("lines = ['settled=true'] if '--settle' in arguments else []\n"
                  "lines.append('intents=' + os.environ['STUB_DOCUMENT'])\n"
                  "with open(arguments[arguments.index('--github-output') + 1], 'a', encoding='utf-8') as stream:\n"
                  "    stream.write('\\n'.join(lines) + '\\n')\n")
        for name, expected in ((SETTLE, {"settled": "true", "intents": document}), (EVALUATE, {"intents": document})):
            outcome = self.run_step(name, STUB_PYTHON3_SCRIPT=writer, STUB_DOCUMENT=document)
            with self.subTest(step=name):
                self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
                written = dict(line.split("=", 1)
                               for line in self.runner.output.read_text(encoding="utf-8").splitlines())
                self.assertEqual(written, expected)


if __name__ == "__main__":
    unittest.main()
