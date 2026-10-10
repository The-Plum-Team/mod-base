"""``packaged-e2e.yml``: the packaged E2E callee's job graph, its steps and their executed shell.

The rules every Build/E2E callee shares are in ``tests/test_workflow_ci_policy.py``. This module
spells out what is particular to packaged E2E: the four jobs and how they depend on each other,
how the one exact Build reaches every job (the ``input`` job authenticates it and hands its
selection record to each later job, which writes it to the file its command is given), what reuse
skips, the command line of every step (the contract the ``ci`` verbs are written against) and what
each script refuses before the kit runs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mod_base import cli, workflow
from mod_base.build_ci import adapter, graph, planning
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.build_ci.records import GATE_MODES
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_graph_jobs, ci_plan, ci_push_plan, ci_selection
from tests.test_workflow_ci_policy import (CANDIDATE_CHECKOUT, FUTURE_KIT_CHECKOUT, JDKS, RECEIVE_STEP, SAMPLES, CiStepRunner, ci_callee,
                                           step_verb)
from tests.test_workflow_policy import PROLOGUE, UPLOAD, parse_kit_argv, require_tools, step

NAME = "packaged-e2e"
SUBJECT = "Authenticate the tested subject"
PROFILE = "Install the declared system profile"
PREPARE = "Fence the host and prepare the worker accounts"
PLAN = "Derive the protected plan"
REPLAN = "Rederive the protected plan"
SELECT = "Select the exact Build"
RECEIVE = RECEIVE_STEP
FETCH = "Fetch the sealed Build bundle"
STAGE = "Stage the candidate and the sealed Build bundle"
RUN = "Run the lane as the candidate"
LOCK = "Lock the candidate and freeze its export"
FINISH = "Terminate and lock the worker accounts"
SEAL, SEND = workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP
#: The steps of each job, in order: section 5 of the architecture, per job type. The aggregate
#: job's sealing step is ``ci aggregate`` itself: the kit indexes the lane results, no hook runs.
STEPS = {
    "input": [*PROLOGUE, SUBJECT, CANDIDATE_CHECKOUT, PREPARE, PLAN, SELECT, FINISH],
    "lane": [*PROLOGUE, CANDIDATE_CHECKOUT, SUBJECT, PROFILE, PREPARE, REPLAN, RECEIVE, FETCH, FUTURE_KIT_CHECKOUT, STAGE, RUN, LOCK,
             SEAL, SEND, FINISH],
    "aggregate": [*PROLOGUE, SUBJECT, CANDIDATE_CHECKOUT, PREPARE, REPLAN, RECEIVE, SEAL, SEND, FINISH],
    "gate": [*PROLOGUE, SUBJECT, CANDIDATE_CHECKOUT, PREPARE, REPLAN, RECEIVE, SEAL, SEND, FINISH],
}
NOT_REUSE = "inputs.mode != 'reuse'"
PREFIX = workflow.CI_PACKAGED_CALL + " / "
RUNNER_STEPS = ("Set up job", "Complete job")
#: Packaged caller mode -> the mode this callee runs in (a deferred run never calls it).
CALLEE_MODE = {"pull-request": "full", "selected": "full", "rebuilt": "full", "reuse": "reuse"}
#: A step the literal job listings name although no job has it. The listings (pinned by digest in
#: ``tests/test_ci_protocol.py``) were written when the aggregate job was to assemble a byte union
#: and validate it in a second step; the results export became an index that ``ci aggregate``
#: seals in the one sealing step. The entry leaves this table when the listings are rewritten.
STALE_LISTED_STEPS = {"aggregate": ["Assemble the exact lane union"]}
#: What a caller passes for a protected push that selected run 36042781699, and for admitted reuse.
PUSH = {"GITHUB_EVENT_NAME": "push", "PR_NUMBER": ""}
SELECTED = {**PUSH, "MODE": "full", "BUILD_RUN_ID": "36042781699"}
REUSED = {**PUSH, "MODE": "reuse", "PLAN_SHA256": "", "SELECTION": ""}
#: Where a later job writes the record it is handed, below ``$RUNNER_TEMP``.
RECEIVED = "mb-state/build-selection.json"


class PackagedStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ci_callee(NAME)
        self.jobs = self.document["jobs"]

    def test_inputs_and_input_job_outputs(self) -> None:
        call = self.document["on"]["workflow_call"]
        self.assertEqual(list(call), ["inputs"], "packaged E2E returns nothing to its caller")
        self.assertEqual(list(call["inputs"]), ["kit-sha", "pr-number", "mode", "build-run-id"])
        self.assertEqual({job_id: job.get("outputs") for job_id, job in self.jobs.items()}, {
            "input": {"tested-sha": "${{ steps.subject.outputs.tested_sha }}",
                      "plan-sha256": "${{ steps.plan.outputs.plan_sha256 }}",
                      "lanes": "${{ steps.plan.outputs.lanes }}",
                      # The record of the Build the command selected: one line, for every later job.
                      "selection": "${{ steps.select.outputs.selection }}"},
            "lane": None, "aggregate": None, "gate": None})
        steps = self.jobs["input"]["steps"]
        self.assertEqual({item["id"]: item["name"] for item in steps if "id" in item},
                         {"subject": SUBJECT, "prepare": PREPARE, "plan": PLAN, "select": SELECT})
        # The plan outputs are the ones the kit writes for a plan.
        self.assertEqual(set(planning.plan_outputs(ci_plan())), {"plan_sha256", "targets", "lanes", "candidate_kit_sha"})

    def test_job_graph_conditions_and_timeouts(self) -> None:
        self.assertEqual(list(self.jobs), ["input", "lane", "aggregate", "gate"])
        self.assertEqual({job_id: job.get("needs") for job_id, job in self.jobs.items()}, {
            "input": None, "lane": "input", "aggregate": ["input", "lane"], "gate": ["input", "lane", "aggregate"]})
        # Reuse skips the three jobs that consume a Build: there is none. The gate runs whenever the
        # run is not cancelled, so a failed or skipped job makes it fail on the graph instead of
        # being skipped itself.
        self.assertEqual({job_id: job.get("if") for job_id, job in self.jobs.items()}, {
            "input": NOT_REUSE, "lane": NOT_REUSE, "aggregate": NOT_REUSE, "gate": "${{ !cancelled() }}"})
        self.assertEqual({job_id: job["timeout-minutes"] for job_id, job in self.jobs.items()}, {
            "input": "100", "lane": "180", "aggregate": "30", "gate": "15"})
        self.assertEqual(self.jobs["lane"]["strategy"], {
            "fail-fast": "false", "matrix": {"id": "${{ fromJSON(needs.input.outputs.lanes) }}"}})
        # The input job may wait the whole Build budget and still has time to authenticate the run.
        self.assertGreaterEqual(int(self.jobs["input"]["timeout-minutes"]) * 60, lim.CI_BUILD_WAIT_SECONDS + 600)

    def test_steps_run_in_the_order_of_their_job_type(self) -> None:
        self.assertEqual({job_id: [item["name"] for item in job["steps"]] for job_id, job in self.jobs.items()}, STEPS)
        # The candidate is locked whenever its account exists and the accounts are swept whenever
        # the job got past its subject, whatever failed or was cancelled in between. The gate is the
        # one job that also runs in reuse mode, and then no input job has handed it a selection.
        always = {LOCK: "${{ always() && steps.prepare.outcome == 'success' }}",
                  FINISH: "${{ always() && steps.subject.outcome == 'success' }}"}
        for job_id, job in self.jobs.items():
            conditions = {item["name"]: item["if"] for item in job["steps"] if "if" in item}
            expected = {name: condition for name, condition in always.items() if name in STEPS[job_id]}
            if job_id == "lane":
                expected[FUTURE_KIT_CHECKOUT] = "steps.plan.outputs.candidate_kit_sha != ''"
            with self.subTest(job=job_id):
                self.assertEqual(conditions, {**expected, **({RECEIVE: NOT_REUSE} if job_id == "gate" else {})})

    def test_job_names_equal_the_registry_and_the_literal_job_listings(self) -> None:
        lane = ci_plan()["lanes"][0]["id"]
        expanded, unexpanded = {}, {}
        for job_id, job in self.jobs.items():
            fields = {"id": lane} if job_id == "lane" else {}
            self.assertEqual(job["name"], workflow.ci_workflow_template_name(NAME, job_id))
            expanded[workflow.ci_api_job_name("packaged", "shared", job_id, **fields)] = job_id
            unexpanded[PREFIX + job["name"]] = job_id
            self.assertIn(PREFIX + job["name"].replace("${{ matrix.id }}", lane), expanded)
        listings = {mode: [listed for listed in ci_graph_jobs(f"packaged-{mode}") if listed["name"].startswith(PREFIX)]
                    for mode in CALLEE_MODE}
        for mode, callee_mode in CALLEE_MODE.items():
            with self.subTest(listing=mode):
                if callee_mode == "full":
                    self.assertEqual(sorted(listed["name"] for listed in listings[mode]), sorted(expanded))
                    self.assertEqual({listed["conclusion"] for listed in listings[mode]}, {"success"})
                else:
                    # A job its `if` skips is listed once under its YAML name: the matrix never expands.
                    self.assertEqual({listed["name"]: listed["conclusion"] for listed in listings[mode]}, {
                        name: "skipped" if self.jobs[job_id].get("if") == NOT_REUSE else "success"
                        for name, job_id in unexpanded.items()})
        # A deferred run never reaches the callee: the skipped call is one job under the caller's name.
        self.assertEqual([listed["name"] for listed in ci_graph_jobs("packaged-deferred")
                          if listed["name"].startswith(workflow.CI_PACKAGED_CALL)], [workflow.CI_PACKAGED_CALL])
        self.assertEqual(sorted([*CALLEE_MODE, "deferred"]), sorted(graph.PACKAGED_MODES))
        # Every step a listing names is a step of that job, in that order.
        stale = set()
        for mode, listing in listings.items():
            for listed in listing:
                job_id = expanded.get(listed["name"], unexpanded.get(listed["name"]))
                named = [entry["name"] for entry in listed["steps"] if entry["name"] not in RUNNER_STEPS]
                known = [name for name in named if name not in STALE_LISTED_STEPS.get(job_id, ())]
                stale.update((job_id, name) for name in named if name not in known)
                with self.subTest(listing=mode, job=listed["name"], conclusion=listed["conclusion"]):
                    if listed["conclusion"] == "skipped":
                        self.assertEqual(named, [], "a skipped job lists no step")
                    else:
                        steps = iter(STEPS[job_id])
                        self.assertTrue(known and all(name in steps for name in known), named)
        self.assertEqual(stale, {(job_id, name) for job_id, names in STALE_LISTED_STEPS.items() for name in names},
                         "the listings were rewritten: empty STALE_LISTED_STEPS")
        for job_id, names in STALE_LISTED_STEPS.items():
            self.assertFalse(set(names) & set(STEPS[job_id]))

    def test_conditions_and_sealing_jobs_are_those_of_the_run_graph(self) -> None:
        for mode, callee_mode in CALLEE_MODE.items():
            plan = ci_plan() if mode == "pull-request" else ci_push_plan()
            contract = graph.run_graph("packaged", mode)
            conclusions = {entry["name"]: entry["conclusion"] for entry in contract.jobs(plan)}
            sealed = set(contract.sealed_jobs(plan))
            self.assertTrue(contract.called()[NAME])
            for job_id, job in self.jobs.items():
                skipped = callee_mode == "reuse" and job.get("if") == NOT_REUSE
                fields = {"id": plan["lanes"][0]["id"]} if job_id == "lane" and not skipped else {}
                name = (workflow.ci_unexpanded_api_job_name("packaged", "shared", job_id)
                        if job_id == "lane" and skipped
                        else workflow.ci_api_job_name("packaged", "shared", job_id, **fields))
                with self.subTest(mode=mode, job=job_id):
                    self.assertEqual(conclusions[name], "skipped" if skipped else "success")
                    self.assertEqual(name in sealed, not skipped and SEAL in STEPS[job_id])
                    self.assertEqual(callee_mode in workflow.CI_JOB_ARTIFACTS[NAME].get(job_id, {}), name in sealed)
        self.assertFalse(graph.run_graph("packaged", "deferred").called()[NAME])
        # The gate seals a tested record in exactly the modes that end in a packaged receipt.
        self.assertEqual(sorted(GATE_MODES["packaged"]["packaged"]),
                         sorted(mode for mode, callee_mode in CALLEE_MODE.items() if callee_mode == "full"))

    def test_uploads_spelled_out(self) -> None:
        common = {"path": "${{ runner.temp }}/mb-upload", "if-no-files-found": "error",
                  "include-hidden-files": "true", "compression-level": "6"}
        expected = {
            "lane": ("mb-ci-runtime--${{ github.run_id }}--a${{ github.run_attempt }}--${{ matrix.id }}", "7"),
            "aggregate": ("mb-ci-results--${{ github.run_id }}--a${{ github.run_attempt }}", "7"),
            "gate": ("${{ format(inputs.mode == 'reuse' && 'mb-ci-reuse--{0}--a{1}' || "
                     "'mb-ci-tested--{0}--a{1}--packaged', github.run_id, github.run_attempt) }}", "90"),
        }
        for job_id, (name, days) in expected.items():
            with self.subTest(job=job_id):
                self.assertEqual(step(self.jobs[job_id]["steps"], SEND), {
                    "name": SEND, "uses": UPLOAD, "with": {"name": name, **common, "retention-days": days}})
        self.assertEqual([lim.CI_RETENTION_DAYS[kind] for kind in ("runtime", "results", "tested", "reuse")],
                         [7, 7, 90, 90])
        self.assertNotIn(SEND, STEPS["input"], "the selection record leaves its job as an output, not an artifact")


class PackagedShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="packaged shell ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name), NAME)
        self.jobs = ci_callee(NAME)["jobs"]
        temp = self.runner.runner_temp
        self.job = ["--repo", "mod", "--config", "mod/site/mod-base.json", "--state", f"{temp}/mb-state"]
        self.out = ["--github-output", str(self.runner.output)]
        self.upload = ["--output", f"{temp}/mb-upload"]
        self.selection = f"{temp}/{RECEIVED}"

    def run_step(self, job_id: str, name: str, **overrides: str | None):
        return self.runner.run(step(self.jobs[job_id]["steps"], name), **overrides)

    def commands(self, job_id: str, **overrides: str | None) -> list[list[str]]:
        """Every kit command line the job issues after its prologue, in step order; in reuse mode
        without the steps that mode skips."""

        issued = []
        for item in self.jobs[job_id]["steps"][len(PROLOGUE):]:
            if "run" not in item or (overrides.get("MODE") == "reuse" and item.get("if") == NOT_REUSE):
                continue
            outcome = self.runner.run(item, **overrides)
            self.assertEqual(outcome.result.returncode, 0, f"{job_id} / {item['name']}: {outcome.result.stderr}")
            issued.extend(outcome.commands)
        return issued

    def prepare(self, roles: str) -> list[str]:
        return ["ci", "worker-prepare", *self.job, "--roles", roles, "--python", self.runner.python,
                *(word for home in JDKS.values() for word in ("--java-home", home))]

    def test_call_input_validation(self) -> None:
        dispatch = {**PUSH, "GITHUB_EVENT_NAME": "workflow_dispatch"}
        accepted = [
            # A pull request names no run (its input job waits for the separate Build run) and no
            # mode, or the mode it has anyway.
            {}, {"MODE": "full"}, {"PR_NUMBER": "1"}, {"PR_NUMBER": "9" * 18},
            {"GITHUB_REF": "refs/heads/release/1.20"},
            # A standalone run names what its select job found, or `same-run` after its rebuild job.
            SELECTED, {**SELECTED, "BUILD_RUN_ID": "1"}, {**SELECTED, "BUILD_RUN_ID": "9" * 18},
            {**SELECTED, "BUILD_RUN_ID": "same-run"}, {**dispatch, "MODE": "full", "BUILD_RUN_ID": "36042781699"},
            {**dispatch, "MODE": "full", "BUILD_RUN_ID": "same-run"},
            # An empty mode runs every lane, like `full`. Whether a standalone run may leave its
            # Build unnamed is the kit's decision (`ci select-build`), not this step's.
            {**PUSH, "BUILD_RUN_ID": "36042781699"}, {**PUSH, "MODE": "full"}, dispatch,
            # Only a push reuses; the run a caller names with it is never read.
            {**PUSH, "MODE": "reuse"}, {**PUSH, "MODE": "reuse", "BUILD_RUN_ID": "same-run"},
        ]
        rejected = [
            {"KIT_SHA": "b" * 39}, {"KIT_SHA": "B" * 40}, {"KIT_SHA": ""}, {"GITHUB_SHA": "a" * 41},
            {"GITHUB_REF": "refs/tags/v1"}, {"GITHUB_REF": "refs/pull/17/merge"}, {"GITHUB_REF": ""},
            {"PR_NUMBER": ""}, {"PR_NUMBER": "0"}, {"PR_NUMBER": "017"}, {"PR_NUMBER": "17 "}, {"PR_NUMBER": "17\n"},
            {"PR_NUMBER": "-1"}, {"PR_NUMBER": "1e3"}, {"PR_NUMBER": "1" * 19}, {"PR_NUMBER": "17;id"},
            {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_EVENT_NAME": "workflow_dispatch"},
            {"GITHUB_EVENT_NAME": "pull_request"}, {**PUSH, "GITHUB_EVENT_NAME": "pull_request"},
            {**PUSH, "GITHUB_EVENT_NAME": "schedule"}, {**PUSH, "GITHUB_EVENT_NAME": "merge_group"},
            {**PUSH, "GITHUB_EVENT_NAME": "workflow_run"}, {**PUSH, "GITHUB_EVENT_NAME": ""},
            {**PUSH, "GITHUB_EVENT_NAME": "push "},
            # A pull request never compiles in this run, never names a Build and never reuses.
            {"BUILD_RUN_ID": "same-run"}, {"BUILD_RUN_ID": "36042781699"}, {"MODE": "reuse"},
            {**dispatch, "MODE": "reuse"},
            *({**PUSH, "MODE": mode} for mode in ("Full", "FULL", "full ", " full", "full\n", "reuse\n", "reused",
                                                    "deferred", "selected", "rebuilt", "pull-request", "true", "0",
                                                    "full;id", "$(id)")),
            *({**SELECTED, "BUILD_RUN_ID": run} for run in ("0", "017", "-1", "1e3", "1" * 19, "17 ", "17\n",
                                                             "17;id", "same_run", "SAME-RUN", "same-run ",
                                                             "same-run\n", "samerun", "latest", "$(id)")),
        ]
        for job_id in self.jobs:
            self.assertEqual(self.run_step(job_id, PROLOGUE[0]).result.returncode, 0, job_id)
        for overrides in accepted:
            with self.subTest(accepted=overrides):
                outcome = self.run_step("gate", PROLOGUE[0], **overrides)
                self.assertEqual((outcome.result.returncode, outcome.commands), (0, []), outcome.result.stderr)
        for overrides in rejected:
            with self.subTest(rejected=overrides):
                outcome = self.run_step("gate", PROLOGUE[0], **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("Invalid call input", outcome.result.stderr)

    def test_the_binding_admits_the_packaged_caller_that_reaches_these_jobs(self) -> None:
        outcome = self.run_step("input", PROLOGUE[3])
        self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
        self.assertEqual((outcome.exported, outcome.commands), ({"MOD_BASE_KIT_SHA": "b" * 40}, []))
        self.assertIn(workflow.CI_CALLER_WORKFLOWS["packaged"], self.runner.base["GITHUB_WORKFLOW_REF"])
        outcome = self.run_step("input", PROLOGUE[3], GITHUB_WORKFLOW_REF="example/mod/"
                                + workflow.CI_CALLEE_WORKFLOWS[NAME] + "@refs/heads/master")
        self.assertNotEqual(outcome.result.returncode, 0)
        self.assertEqual(outcome.exported, {})

    def test_every_job_issues_exactly_its_command_lines(self) -> None:
        job, out, upload = self.job, self.out, self.upload
        subject = ["ci", "subject", *job, "--producer", "packaged", "--pr", "17", *out]
        # A job that holds the candidate checkout derives its subject from it and from one request.
        derived = ["ci", "subject", *job, "--producer", "packaged", "--pr", "17", "--candidate", "candidate", *out]
        expected = ["--expect-sha256", SAMPLES["PLAN_SHA256"]]
        # Every later job gives the record it was handed to the one command that needs the Build.
        selection = ["--selection", self.selection]
        finish = ["ci", "worker-finish", *job]
        self.assertEqual({job_id: self.commands(job_id) for job_id in self.jobs}, {
            # A pull request names no run: the kit finds its separate Build run and waits for it.
            "input": [subject, self.prepare("validator"), ["ci", "plan", *job, "--pin-candidate", "candidate", *out],
                      ["ci", "select-build", *job, "--build-run-id", "", "--wait-seconds", "5400",
                       "--output", self.selection, *out], finish],
            # The lane alone runs a client: the image packages it needs go in before its accounts.
            "lane": [derived, ["ci", "system-profile", *job], self.prepare("candidate+validator"),
                     ["ci", "plan", *job, "--pin-candidate", "candidate", "--candidate", "candidate", *expected, *out],
                     ["ci", "fetch-build", *job, *selection],
                     ["ci", "worker-stage", *job, "--candidate", "candidate", "--future-kit", "candidate-kit", "--bundle"],
                     ["ci", "worker-run", *job, "--hook", "run_lane", "--unit", "lane-a"],
                     ["ci", "worker-seal", *job],
                     ["ci", "worker-validate", *job, "--hook", "verify_runtime", "--unit", "lane-a", *upload],
                     finish],
            # The kit indexes the lane results itself: no validator hook and no Build bundle here.
            "aggregate": [subject, self.prepare("validator"), ["ci", "plan", *job, "--pin-candidate", "candidate", *expected, *out],
                          ["ci", "aggregate", *job, *selection, *upload], finish],
            "gate": [subject, self.prepare("validator"), ["ci", "plan", *job, "--pin-candidate", "candidate", *out, *expected],
                     ["ci", "seal-gate", *job, "--gate", "packaged", *selection, *upload], finish],
        })
        self.assertEqual(list(JDKS), ["JAVA_HOME_17_X64", "JAVA_HOME_21_X64", "JAVA_HOME_25_X64"])

    def test_the_input_job_selects_what_the_caller_named_and_waits_the_build_budget(self) -> None:
        for overrides in ({}, SELECTED, {**SELECTED, "BUILD_RUN_ID": "same-run"}):
            command = self.run_step("input", SELECT, **overrides).commands[0]
            flags = dict(zip(command[2::2], command[3::2]))
            with self.subTest(overrides=overrides):
                self.assertEqual(flags["--build-run-id"], overrides.get("BUILD_RUN_ID", ""))
                self.assertEqual(int(flags["--wait-seconds"]), lim.CI_BUILD_WAIT_SECONDS)
                self.assertEqual(flags["--output"], self.selection)
        # No later job selects, so none waits: each is handed the record of this one selection.
        for job_id in ("lane", "aggregate", "gate"):
            self.assertNotIn("select-build", workflow.CI_JOB_VERBS[NAME][job_id], job_id)
            self.assertEqual(step(self.jobs[job_id]["steps"], RECEIVE)["env"],
                             {"SELECTION": "${{ needs.input.outputs.selection }}"}, job_id)

    def test_in_reuse_mode_the_gate_plans_alone_and_selects_no_build(self) -> None:
        job, out = self.job, self.out
        self.assertEqual(self.commands("gate", **REUSED), [
            ["ci", "subject", *job, "--producer", "packaged", "--pr", "", *out], self.prepare("validator"),
            # The input job was skipped: there is no planned digest to expect and no selection to judge.
            ["ci", "plan", *job, "--pin-candidate", "candidate", *out],
            ["ci", "seal-gate", *job, "--gate", "packaged", *self.upload], ["ci", "worker-finish", *job]])
        self.assertEqual([command[1] for command in self.commands("gate", **REUSED)],
                         list(workflow.CI_JOB_VERBS[NAME]["gate"]))
        for overrides in ({**REUSED, "PLAN_SHA256": SAMPLES["PLAN_SHA256"]}, {**REUSED, "PLAN_SHA256": "0"},
                          {**REUSED, "MODE": "full"}, {**REUSED, "MODE": ""}):
            with self.subTest(overrides=overrides):
                outcome = self.run_step("gate", REPLAN, **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertEqual(outcome.commands, [])
        # The step that reuse skips would refuse the record it is not given.
        outcome = self.run_step("gate", RECEIVE, **REUSED)
        self.assertNotEqual(outcome.result.returncode, 0)
        self.assertIn("malformed selection record", outcome.result.stderr)
        self.assertFalse((self.runner.runner_temp / RECEIVED).exists())

    def test_hooks_roles_and_producer_are_those_of_the_adapter_contract(self) -> None:
        for job_id in self.jobs:
            runs_candidate = "worker-run" in workflow.CI_JOB_VERBS[NAME][job_id]
            for command in self.commands(job_id):
                flags = dict(zip(command[2::2], command[3::2])) if command[1] != "worker-prepare" else {}
                with self.subTest(job=job_id, verb=command[1]):
                    if "--hook" in flags:
                        hook = adapter.HOOKS[flags["--hook"]]
                        self.assertEqual(hook.role, "candidate" if command[1] == "worker-run" else "validator")
                        self.assertEqual((hook.unit, flags["--unit"]), ("lane", SAMPLES["LANE"]))
                    if command[1] == "worker-prepare":
                        self.assertEqual(command[command.index("--roles") + 1],
                                         "candidate+validator" if runs_candidate else "validator")
                    for flag in ("--producer", "--gate"):
                        if flag in flags:
                            self.assertEqual(flags[flag], "packaged")
        self.assertIn("packaged", PRODUCERS)

    def test_a_protected_push_authenticates_a_subject_without_a_pull_request(self) -> None:
        for event in ("push", "workflow_dispatch"):
            outcome = self.run_step("input", SUBJECT, **{**SELECTED, "GITHUB_EVENT_NAME": event})
            self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
            self.assertEqual(outcome.commands[0][outcome.commands[0].index("--pr") + 1], "")
            self.assertIsNone(parse_kit_argv(outcome.commands[0]).pr)
        parsed = parse_kit_argv(self.run_step("input", SUBJECT).commands[0])
        self.assertEqual((parsed.producer, parsed.pr), ("packaged", 17))

    def test_malformed_values_from_the_input_job_stop_a_step_before_the_kit_runs(self) -> None:
        cases = [
            (job_id, REPLAN, {"PLAN_SHA256": value})
            for job_id in ("lane", "aggregate", "gate")
            for value in ("", "c" * 63, "c" * 65, "C" * 64, "sha256:" + "c" * 64, "c" * 64 + "\n")
        ] + [
            ("lane", REPLAN, overrides)
            for overrides in ({"TESTED_SHA": ""}, {"TESTED_SHA": "d" * 39}, {"TESTED_SHA": "D" * 40},
                              {"STUB_CANDIDATE_HEAD": "e" * 40}, {"STUB_CANDIDATE_HEAD": None},
                              {"TESTED_SHA": "e" * 40})
        ] + [
            ("lane", name, {"LANE": value})
            for name in (RUN, SEAL)
            for value in ("", "a--b", "../x", "A", "a b", "-a", "a" * 81, "a;id", "$(id)")
        ]
        for job_id, name, overrides in cases:
            with self.subTest(job=job_id, step=name, overrides=overrides):
                outcome = self.run_step(job_id, name, **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertEqual(outcome.commands, [])

    def test_a_later_job_writes_exactly_the_record_it_is_handed(self) -> None:
        written = self.runner.runner_temp / RECEIVED
        steps = {job_id: step(self.jobs[job_id]["steps"], RECEIVE) for job_id in ("lane", "aggregate", "gate")}
        for job_id, item in steps.items():
            for locale in ("C", "C.UTF-8"):
                outcome = self.runner.run(item, LC_ALL=locale)
                with self.subTest(job=job_id, locale=locale):
                    self.assertEqual((outcome.result.returncode, outcome.commands), (0, []), outcome.result.stderr)
                    # The canonical bytes `ci select-build` wrote for its own job, for this user alone.
                    self.assertEqual(written.read_bytes(), canonical_json(ci_selection()))
                    self.assertEqual(written.stat().st_mode & 0o777, 0o600)
        # One step in three jobs; only the gate, which also runs in reuse mode, may skip it.
        self.assertEqual(steps["lane"], steps["aggregate"])
        self.assertEqual(steps["gate"], {**steps["lane"], "if": NOT_REUSE})
        self.assertEqual(step_verb(steps["lane"]), None, "the record is written by the shell, without a token")
        # The step admits what one output of a kit command can hold, and the kit decodes the rest.
        self.assertIn(f'"${{#SELECTION}}" -le {cli.MAX_OUTPUT_VALUE_CHARS} ', steps["lane"]["run"])
        self.assertLess(len(SAMPLES["SELECTION"]), cli.MAX_OUTPUT_VALUE_CHARS)

    def test_a_later_job_refuses_what_is_not_one_line_of_a_selection_record(self) -> None:
        record = SAMPLES["SELECTION"]
        kind = '"kind":"mod-base.ci.selection"'
        longest = '{' + kind + ',"pad":"' + "a" * (cli.MAX_OUTPUT_VALUE_CHARS - len(kind) - 11) + '"}'
        self.assertEqual(len(longest), cli.MAX_OUTPUT_VALUE_CHARS)
        rejected = {
            "empty": "", "not an object": record[1:-1], "an array": "[" + record + "]", "another kind":
            record.replace("mod-base.ci.selection", "mod-base.ci.gate"), "two lines": record + "\n" + record,
            "a trailing newline": record + "\n", "a carriage return": record + "\r", "a space": record.replace(":", ": ", 1),
            "a tab": record.replace(",", ",\t", 1), "a control character": record.replace(",", ",\x01", 1),
            "a backslash": record.replace("mb-ci-build", "mb\\u002dci-build"), "a command": record.replace(
                "master", "$(id)"), "a quote": record.replace("master", "ma'ster"), "a glob": record.replace(
                "master", "*"), "not ASCII": record.replace("master", "m\u00e4ster"), "too long": longest[:-2] + 'a"}',
        }
        for label, value in rejected.items():
            for locale in ("C", "C.UTF-8"):
                outcome = self.run_step("lane", RECEIVE, SELECTION=value, LC_ALL=locale)
                with self.subTest(value=label, locale=locale):
                    self.assertNotEqual(outcome.result.returncode, 0)
                    self.assertEqual(outcome.result.stderr, "Build selection: malformed selection record\n")
                    self.assertFalse((self.runner.runner_temp / RECEIVED).exists())
        # The longest line a kit command can write as one output is still received.
        self.assertEqual(self.run_step("lane", RECEIVE, SELECTION=longest).result.returncode, 0)
        self.assertEqual((self.runner.runner_temp / RECEIVED).stat().st_size, cli.MAX_OUTPUT_VALUE_CHARS + 1)

    def test_a_later_job_never_replaces_or_follows_what_is_already_there(self) -> None:
        item = step(self.jobs["lane"]["steps"], RECEIVE)
        temp = Path(tempfile.mkdtemp(dir=self.runner.runner_temp.parent))
        environment = {**self.runner.base, "SELECTION": SAMPLES["SELECTION"], "RUNNER_TEMP": str(temp)}
        # Without the state directory of `ci subject` there is nowhere to write.
        result = self.runner.harness.run(item["run"], environment, cwd=self.runner.workspace)
        self.assertNotEqual(result.returncode, 0)
        (temp / "mb-state").mkdir(mode=0o700)
        for existing in (b"kept", b""):
            (temp / RECEIVED).write_bytes(existing)
            result = self.runner.harness.run(item["run"], environment, cwd=self.runner.workspace)
            with self.subTest(existing=existing):
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((temp / RECEIVED).read_bytes(), existing)
            (temp / RECEIVED).unlink()
        (temp / RECEIVED).symlink_to(temp / "elsewhere")
        result = self.runner.harness.run(item["run"], environment, cwd=self.runner.workspace)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((temp / "elsewhere").exists(), "the record is never written through a link")
        (temp / RECEIVED).unlink()
        result = self.runner.harness.run(item["run"], environment, cwd=self.runner.workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), ("", ""))
        self.assertEqual(sorted(path.name for path in (temp / "mb-state").iterdir()), ["build-selection.json"])
        self.assertEqual((temp / RECEIVED).read_bytes(), canonical_json(ci_selection()))

    def test_the_unit_check_is_the_unit_grammar(self) -> None:
        # The matrix unit becomes part of an artifact name, so the shell admits exactly the kit's ids.
        scripts = [step(self.jobs["lane"]["steps"], name)["run"] for name in (RUN, SEAL)]
        checks = {line for script in scripts for line in script.splitlines() if '"$LANE" =~' in line}
        self.assertEqual(len(checks), 1, "the two steps that name the unit apply one check")
        characters = [chr(code) for code in range(1, 128)] + ["é"]
        samples = ["", "lane-a", "1.20.1-fabric", "a" * 79, "a" * 80, "a" * 81, "0" * 80, "a" * 79 + "-", "a--b",
                   "--a", "a--", "a---b", "a-b-c", "a-.-b", "a.-_b", "-", "--", *characters,
                   *("a" + character for character in characters),
                   *("a" + character + "a" for character in characters),
                   *("a-" + character for character in characters)]
        program = ('fail() { exit 1; }\nwhile IFS= read -r -d "" LANE; do\n  if ( ' + checks.pop()
                   + ' ); then printf 1; else printf 0; fi\ndone\n')
        for locale in ("C", "C.UTF-8"):
            result = subprocess.run(["bash", "-c", program], input="\0".join(samples) + "\0", capture_output=True,
                                    text=True, timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                                "LC_ALL": locale})
            self.assertEqual((result.returncode, len(result.stdout)), (0, len(samples)), result.stderr)
            for sample, verdict in zip(samples, result.stdout):
                with self.subTest(locale=locale, unit=sample):
                    self.assertEqual(verdict == "1", grammar.is_match(grammar.CI_UNIT_ID, sample))
        for name in (RUN, SEAL):
            self.assertEqual(self.run_step("lane", name, LANE="1.20.1-fabric").result.returncode, 0)

    def test_a_missing_tool_or_a_failing_command_fails_the_step(self) -> None:
        for home in JDKS:
            outcome = self.run_step("input", PREPARE, **{home: None})
            self.assertNotEqual(outcome.result.returncode, 0, home)
            self.assertEqual(outcome.commands, [], "an image without one of the JDKs prepares no worker")
        for job_id, job in self.jobs.items():
            for item in job["steps"][len(PROLOGUE):]:
                if "run" in item and step_verb(item) is not None:
                    with self.subTest(job=job_id, step=item["name"]):
                        outcome = self.runner.run(item, STUB_PYTHON3_SCRIPT="raise SystemExit(3)")
                        self.assertEqual(outcome.result.returncode, 3, "the step's status is the kit's")


if __name__ == "__main__":
    unittest.main()
