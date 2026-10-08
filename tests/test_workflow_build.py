"""``build.yml``: the Build callee's job graph, its steps and their executed shell.

The rules every Build/E2E callee shares are in ``tests/test_workflow_ci_policy.py``. This module
spells out what is particular to the Build: the five jobs and how they depend on each other, what
reuse skips, the command line of every step (the contract the ``ci`` verbs are written against)
and what each script refuses before the kit runs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from mod_base.build_ci import adapter, graph, planning
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.model import grammar
from mod_base.model import limits as lim
from tests.helpers import ci_graph_jobs, ci_plan, ci_push_plan
from tests.test_workflow_ci_policy import CANDIDATE_CHECKOUT, JDKS, SAMPLES, CiStepRunner, ci_callee
from tests.test_workflow_policy import PROLOGUE, UPLOAD, parse_kit_argv, require_tools, step

SUBJECT = "Authenticate the tested subject"
PREPARE = "Fence the host and prepare the worker accounts"
PLAN = "Derive the protected plan"
REPLAN = "Rederive the protected plan"
REUSE = "Admit post-merge reuse"
STAGE = "Stage the candidate for its worker"
POLICY = "Run the protected policy suite"
COMPILE = "Build the target as the candidate"
LOCK = "Lock the candidate and freeze its export"
ASSEMBLE = "Assemble the exact target union"
FINISH = "Terminate and lock the worker accounts"
SEAL, SEND = workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP
#: The steps of each job, in order: section 5 of the architecture, per job type.
STEPS = {
    "plan": [*PROLOGUE, SUBJECT, PREPARE, PLAN, REUSE, FINISH],
    "policy": [*PROLOGUE, CANDIDATE_CHECKOUT, SUBJECT, PREPARE, REPLAN, STAGE, POLICY, LOCK, FINISH],
    "target": [*PROLOGUE, CANDIDATE_CHECKOUT, SUBJECT, PREPARE, REPLAN, STAGE, COMPILE, LOCK, SEAL, SEND, FINISH],
    "assemble": [*PROLOGUE, SUBJECT, PREPARE, REPLAN, ASSEMBLE, SEAL, SEND, FINISH],
    "gate": [*PROLOGUE, SUBJECT, PREPARE, REPLAN, SEAL, SEND, FINISH],
}
NOT_REUSE = "needs.plan.outputs.mode != 'reuse'"
PREFIX = workflow.CI_BUILD_CALL + " / "
RUNNER_STEPS = ("Set up job", "Complete job")


class BuildStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = ci_callee("build")
        self.jobs = self.document["jobs"]

    def test_inputs_and_plan_outputs(self) -> None:
        call = self.document["on"]["workflow_call"]
        self.assertEqual(list(call), ["inputs"], "the Build returns nothing to its caller")
        self.assertEqual(list(call["inputs"]), ["kit-sha", "pr-number"])
        self.assertEqual({job_id: job.get("outputs") for job_id, job in self.jobs.items()}, {
            "plan": {"tested-sha": "${{ steps.subject.outputs.tested_sha }}",
                     "plan-sha256": "${{ steps.plan.outputs.plan_sha256 }}",
                     "targets": "${{ steps.plan.outputs.targets }}",
                     "mode": "${{ steps.reuse.outputs.mode == 'reuse' && 'reuse' || 'full' }}"},
            "policy": None, "target": None, "assemble": None, "gate": None})
        plan = self.jobs["plan"]["steps"]
        self.assertEqual({item["id"]: item["name"] for item in plan if "id" in item},
                         {"subject": SUBJECT, "prepare": PREPARE, "plan": PLAN, "reuse": REUSE})
        # The plan outputs are the ones the kit writes for a plan.
        self.assertEqual(set(planning.plan_outputs(ci_plan())), {"plan_sha256", "targets", "lanes"})

    def test_job_graph_conditions_and_timeouts(self) -> None:
        self.assertEqual(list(self.jobs), ["plan", "policy", "target", "assemble", "gate"])
        self.assertEqual({job_id: job.get("needs") for job_id, job in self.jobs.items()}, {
            "plan": None, "policy": "plan", "target": "plan", "assemble": ["plan", "target"],
            "gate": ["plan", "policy", "target", "assemble"]})
        # Reuse skips the three worker jobs. The gate runs whenever the run is not cancelled, so a
        # failed or skipped worker makes it fail on the graph instead of being skipped itself.
        self.assertEqual({job_id: job.get("if") for job_id, job in self.jobs.items()}, {
            "plan": None, "policy": NOT_REUSE, "target": NOT_REUSE, "assemble": NOT_REUSE,
            "gate": "${{ !cancelled() }}"})
        self.assertEqual({job_id: job["timeout-minutes"] for job_id, job in self.jobs.items()}, {
            "plan": "15", "policy": "60", "target": "120", "assemble": "30", "gate": "15"})
        self.assertEqual(self.jobs["target"]["strategy"], {
            "fail-fast": "false", "matrix": {"id": "${{ fromJSON(needs.plan.outputs.targets) }}"}})
        self.assertEqual(step(self.jobs["plan"]["steps"], REUSE)["if"], "github.event_name == 'push'")

    def test_steps_run_in_the_order_of_their_job_type(self) -> None:
        self.assertEqual({job_id: [item["name"] for item in job["steps"]] for job_id, job in self.jobs.items()}, STEPS)
        # The candidate is locked whenever its account exists and the accounts are swept whenever
        # the job got past its subject, whatever failed or was cancelled in between.
        always = {LOCK: "${{ always() && steps.prepare.outcome == 'success' }}",
                  FINISH: "${{ always() && steps.subject.outcome == 'success' }}"}
        for job_id, job in self.jobs.items():
            conditions = {item["name"]: item["if"] for item in job["steps"] if "if" in item}
            expected = {name: condition for name, condition in always.items() if name in STEPS[job_id]}
            with self.subTest(job=job_id):
                self.assertEqual(conditions, {**expected, **({REUSE: "github.event_name == 'push'"}
                                                             if job_id == "plan" else {})})

    def test_job_names_equal_the_registry_and_the_literal_job_listings(self) -> None:
        target = ci_plan()["targets"][0]["id"]
        expanded = {}
        for job_id, job in self.jobs.items():
            fields = {"id": target} if job_id == "target" else {}
            self.assertEqual(job["name"], workflow.ci_workflow_template_name("build", job_id))
            expanded[workflow.ci_api_job_name("build", "shared", job_id, **fields)] = job_id
            self.assertIn(PREFIX + job["name"].replace("${{ matrix.id }}", target), expanded)
        full = [listed for listed in ci_graph_jobs("build-full") if listed["name"].startswith(PREFIX)]
        self.assertEqual(sorted(listed["name"] for listed in full), sorted(expanded))
        self.assertEqual({listed["conclusion"] for listed in full}, {"success"})
        # A job its `if` skips is listed once under its YAML name: the matrix never expands.
        reuse = [listed for listed in ci_graph_jobs("build-reuse") if listed["name"].startswith(PREFIX)]
        self.assertEqual({listed["name"]: listed["conclusion"] for listed in reuse}, {
            PREFIX + job["name"]: "skipped" if job.get("if") == NOT_REUSE else "success"
            for job in self.jobs.values()})
        # Every step a listing names is a step of that job, in that order.
        for listed in full + reuse:
            named = [entry["name"] for entry in listed["steps"] if entry["name"] not in RUNNER_STEPS]
            with self.subTest(job=listed["name"], conclusion=listed["conclusion"]):
                if listed["conclusion"] == "skipped":
                    self.assertEqual(named, [], "a skipped job lists no step")
                else:
                    steps = iter(STEPS[expanded[listed["name"]]])
                    self.assertTrue(named and all(name in steps for name in named), named)

    def test_conditions_and_sealing_jobs_are_those_of_the_run_graph(self) -> None:
        for mode, plan in (("full", ci_plan()), ("reuse", ci_push_plan())):
            contract = graph.run_graph("build", mode)
            conclusions = {entry["name"]: entry["conclusion"] for entry in contract.jobs(plan)}
            sealed = set(contract.sealed_jobs(plan))
            for job_id, job in self.jobs.items():
                skipped = mode == "reuse" and job.get("if") == NOT_REUSE
                fields = {"id": plan["targets"][0]["id"]} if job_id == "target" and not skipped else {}
                name = (workflow.ci_unexpanded_api_job_name("build", "shared", job_id)
                        if job_id == "target" and skipped
                        else workflow.ci_api_job_name("build", "shared", job_id, **fields))
                with self.subTest(mode=mode, job=job_id):
                    self.assertEqual(conclusions[name], "skipped" if skipped else "success")
                    self.assertEqual(name in sealed, not skipped and SEAL in STEPS[job_id])
                    self.assertEqual(mode in workflow.CI_JOB_ARTIFACTS["build"].get(job_id, {}), name in sealed)

    def test_uploads_spelled_out(self) -> None:
        common = {"path": "${{ runner.temp }}/mb-upload", "if-no-files-found": "error",
                  "include-hidden-files": "true", "compression-level": "6"}
        expected = {
            "target": ("mb-ci-target--${{ github.run_id }}--a${{ github.run_attempt }}--${{ matrix.id }}", "1"),
            "assemble": ("mb-ci-build--${{ github.run_id }}--a${{ github.run_attempt }}", "7"),
            "gate": ("${{ format(needs.plan.outputs.mode == 'reuse' && 'mb-ci-reuse--{0}--a{1}' || "
                     "'mb-ci-tested--{0}--a{1}--build', github.run_id, github.run_attempt) }}", "90"),
        }
        for job_id, (name, days) in expected.items():
            with self.subTest(job=job_id):
                self.assertEqual(step(self.jobs[job_id]["steps"], SEND), {
                    "name": SEND, "uses": UPLOAD, "with": {"name": name, **common, "retention-days": days}})
        self.assertEqual([lim.CI_RETENTION_DAYS[kind] for kind in ("target", "build", "tested", "reuse")],
                         [1, 7, 90, 90])


class BuildShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="build shell ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name))
        self.jobs = ci_callee("build")["jobs"]

    def run_step(self, job_id: str, name: str, **overrides: str | None):
        return self.runner.run(step(self.jobs[job_id]["steps"], name), **overrides)

    def commands(self, job_id: str, **overrides: str | None) -> list[list[str]]:
        """Every kit command line the job issues after its prologue, in step order."""

        issued = []
        for item in self.jobs[job_id]["steps"][len(PROLOGUE):]:
            if "run" in item:
                outcome = self.runner.run(item, **overrides)
                self.assertEqual(outcome.result.returncode, 0, f"{job_id} / {item['name']}: {outcome.result.stderr}")
                issued.extend(outcome.commands)
        return issued

    def test_call_input_validation(self) -> None:
        push = {"GITHUB_EVENT_NAME": "push", "PR_NUMBER": ""}
        accepted = [{}, {"PR_NUMBER": "1"}, {"PR_NUMBER": "9" * 18}, push,
                    {"GITHUB_EVENT_NAME": "workflow_dispatch", "PR_NUMBER": ""},
                    {"GITHUB_REF": "refs/heads/release/1.20"}]
        rejected = [
            {"KIT_SHA": "b" * 39}, {"KIT_SHA": "B" * 40}, {"KIT_SHA": ""}, {"GITHUB_SHA": "a" * 41},
            {"GITHUB_REF": "refs/tags/v1"}, {"GITHUB_REF": "refs/pull/17/merge"}, {"GITHUB_REF": ""},
            {"PR_NUMBER": ""}, {"PR_NUMBER": "0"}, {"PR_NUMBER": "017"}, {"PR_NUMBER": "17 "}, {"PR_NUMBER": "17\n"},
            {"PR_NUMBER": "-1"}, {"PR_NUMBER": "1e3"}, {"PR_NUMBER": "1" * 19}, {"PR_NUMBER": "17;id"},
            {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_EVENT_NAME": "workflow_dispatch"},
            {"GITHUB_EVENT_NAME": "pull_request"}, {"GITHUB_EVENT_NAME": "pull_request", "PR_NUMBER": ""},
            {"GITHUB_EVENT_NAME": "schedule", "PR_NUMBER": ""}, {"GITHUB_EVENT_NAME": "merge_group", "PR_NUMBER": ""},
            {"GITHUB_EVENT_NAME": "workflow_run", "PR_NUMBER": ""}, {"GITHUB_EVENT_NAME": "", "PR_NUMBER": ""},
            {"GITHUB_EVENT_NAME": "push ", "PR_NUMBER": ""},
        ]
        for job_id in self.jobs:
            self.assertEqual(self.run_step(job_id, PROLOGUE[0]).result.returncode, 0, job_id)
        for overrides in accepted:
            with self.subTest(accepted=overrides):
                outcome = self.run_step("plan", PROLOGUE[0], **overrides)
                self.assertEqual((outcome.result.returncode, outcome.commands), (0, []), outcome.result.stderr)
        for overrides in rejected:
            with self.subTest(rejected=overrides):
                outcome = self.run_step("plan", PROLOGUE[0], **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("Invalid call input", outcome.result.stderr)

    def test_the_binding_admits_only_the_managed_build_callers_of_the_canonical_branch(self) -> None:
        def ref(path: str, where: str = "refs/heads/master", repository: str = "example/mod") -> dict[str, str]:
            return {"GITHUB_WORKFLOW_REF": f"{repository}/{path}@{where}"}

        build, packaged = (workflow.CI_CALLER_WORKFLOWS[producer] for producer in ("build", "packaged"))
        for overrides in ({}, ref(build), ref(packaged)):
            with self.subTest(accepted=overrides):
                outcome = self.run_step("plan", PROLOGUE[3], **overrides)
                self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
                self.assertEqual((outcome.exported, outcome.commands), ({"MOD_BASE_KIT_SHA": "b" * 40}, []))
        rejected = {
            "the Pages caller": ref(workflow.PAGES_WORKFLOW_PATH),
            "the status caller": ref(workflow.CI_CALLER_WORKFLOWS["status"]),
            "the guard workflow": ref(workflow.CI_GUARD_WORKFLOW_PATH),
            "the callee itself": ref(workflow.CI_CALLEE_WORKFLOWS["build"]),
            "another branch": ref(build, "refs/heads/dev"),
            "a branch below the canonical one": ref(build, "refs/heads/master/x"),
            "a tag": ref(build, "refs/tags/master"),
            "a pull request ref": ref(build, "refs/pull/17/merge"),
            "another repository": ref(build, repository="attacker/mod"),
            "a repository suffix": ref(build, repository="x/example/mod"),
            "the caller of another repository": {"GITHUB_REPOSITORY": "attacker/fork"},
            "a renamed caller": ref(build + ".yml"),
            "digest literal": {"MB_KIT_TREE_DIGEST": "sha256:" + "0" * 64},
            "git environment": {"GIT_DIR": "/tmp/elsewhere"},
            "mod head": {"STUB_MOD_HEAD": "c" * 40},
            "kit head": {"STUB_KIT_HEAD": "c" * 40},
            "dirty mod": {"STUB_MOD_STATUS": "?? injected.py\n"},
            "dirty kit": {"STUB_KIT_STATUS": " M src/mod_base/Z.py\n"},
        }
        for label, overrides in rejected.items():
            with self.subTest(rejected=label):
                outcome = self.run_step("plan", PROLOGUE[3], **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertIn("Implementation identity", outcome.result.stderr)
                self.assertEqual(outcome.exported, {})

    def test_every_job_issues_exactly_its_command_lines(self) -> None:
        temp = self.runner.runner_temp
        job = ["--repo", "mod", "--config", "mod/site/mod-base.json", "--state", f"{temp}/mb-state"]
        out = ["--github-output", str(self.runner.output)]
        upload = ["--output", f"{temp}/mb-upload"]
        subject = ["ci", "subject", *job, "--producer", "build", "--pr", "17", *out]
        # A job that holds the candidate checkout derives its subject from it and from one request.
        derived = ["ci", "subject", *job, "--producer", "build", "--pr", "17", "--candidate", "candidate", *out]
        expected = ["--expect-sha256", SAMPLES["PLAN_SHA256"], *out]
        replan = ["ci", "plan", *job, *expected]
        replan_candidate = ["ci", "plan", *job, "--candidate", "candidate", *expected]
        stage = ["ci", "worker-stage", *job, "--candidate", "candidate"]
        finish = ["ci", "worker-finish", *job]

        def prepare(roles: str) -> list[str]:
            return ["ci", "worker-prepare", *job, "--roles", roles, "--python", self.runner.python,
                    *(word for home in JDKS.values() for word in ("--java-home", home))]

        self.assertEqual({job_id: self.commands(job_id) for job_id in self.jobs}, {
            "plan": [subject, prepare("validator"), ["ci", "plan", *job, *out], ["ci", "reuse-admit", *job, *out],
                     finish],
            "policy": [derived, prepare("candidate+validator"), replan_candidate, stage,
                       ["ci", "worker-run", *job, "--hook", "policy"], ["ci", "worker-seal", *job], finish],
            "target": [derived, prepare("candidate+validator"), replan_candidate, stage,
                       ["ci", "worker-run", *job, "--hook", "build_target", "--unit", "target-a"],
                       ["ci", "worker-seal", *job],
                       ["ci", "worker-validate", *job, "--hook", "verify_target", "--unit", "target-a", *upload],
                       finish],
            "assemble": [subject, prepare("validator"), replan, ["ci", "assemble", *job],
                         ["ci", "worker-validate", *job, "--hook", "verify_build", *upload], finish],
            "gate": [subject, prepare("validator"), replan, ["ci", "seal-gate", *job, "--gate", "build", *upload],
                     finish],
        })
        self.assertEqual(list(JDKS), ["JAVA_HOME_17_X64", "JAVA_HOME_21_X64", "JAVA_HOME_25_X64"])

    def test_hooks_roles_and_producer_are_those_of_the_adapter_contract(self) -> None:
        for job_id in self.jobs:
            runs_candidate = "worker-run" in workflow.CI_JOB_VERBS["build"][job_id]
            for command in self.commands(job_id):
                flags = dict(zip(command[2::2], command[3::2])) if command[1] != "worker-prepare" else {}
                with self.subTest(job=job_id, verb=command[1]):
                    if "--hook" in flags:
                        hook = adapter.HOOKS[flags["--hook"]]
                        self.assertEqual(hook.role, "candidate" if command[1] == "worker-run" else "validator")
                        self.assertEqual(hook.unit, "target" if "--unit" in flags else None)
                    if command[1] == "worker-prepare":
                        self.assertEqual(command[command.index("--roles") + 1],
                                         "candidate+validator" if runs_candidate else "validator")
                    for flag in ("--producer", "--gate"):
                        if flag in flags:
                            self.assertEqual(flags[flag], "build")
        self.assertIn("build", PRODUCERS)

    def test_a_protected_push_authenticates_a_subject_without_a_pull_request(self) -> None:
        for event in ("push", "workflow_dispatch"):
            outcome = self.run_step("plan", SUBJECT, GITHUB_EVENT_NAME=event, PR_NUMBER="")
            self.assertEqual(outcome.result.returncode, 0, outcome.result.stderr)
            self.assertEqual(outcome.commands[0][outcome.commands[0].index("--pr") + 1], "")
            self.assertIsNone(parse_kit_argv(outcome.commands[0]).pr)
        self.assertEqual(parse_kit_argv(self.run_step("plan", SUBJECT).commands[0]).pr, 17)

    def test_malformed_values_from_the_plan_job_stop_a_step_before_the_kit_runs(self) -> None:
        cases = [
            (job_id, REPLAN, {"PLAN_SHA256": value})
            for job_id in ("policy", "target", "assemble", "gate")
            for value in ("", "c" * 63, "c" * 65, "C" * 64, "sha256:" + "c" * 64, "c" * 64 + "\n")
        ] + [
            (job_id, REPLAN, overrides)
            for job_id in ("policy", "target")
            for overrides in ({"TESTED_SHA": ""}, {"TESTED_SHA": "d" * 39}, {"TESTED_SHA": "D" * 40},
                              {"STUB_CANDIDATE_HEAD": "e" * 40}, {"STUB_CANDIDATE_HEAD": None},
                              {"TESTED_SHA": "e" * 40})
        ] + [
            ("target", name, {"TARGET": value})
            for name in (COMPILE, SEAL)
            for value in ("", "a--b", "../x", "A", "a b", "-a", "a" * 81, "a;id", "$(id)")
        ]
        for job_id, name, overrides in cases:
            with self.subTest(job=job_id, step=name, overrides=overrides):
                outcome = self.run_step(job_id, name, **overrides)
                self.assertNotEqual(outcome.result.returncode, 0)
                self.assertEqual(outcome.commands, [])

    def test_the_unit_check_is_the_unit_grammar(self) -> None:
        # The matrix unit becomes part of an artifact name, so the shell admits exactly the kit's ids.
        scripts = [step(self.jobs["target"]["steps"], name)["run"] for name in (COMPILE, SEAL)]
        checks = {line for script in scripts for line in script.splitlines() if '"$TARGET" =~' in line}
        self.assertEqual(len(checks), 1, "the two steps that name the unit apply one check")
        characters = [chr(code) for code in range(1, 128)] + ["é"]
        samples = ["", "target-a", "1.20.1-fabric", "a" * 79, "a" * 80, "a" * 81, "0" * 80, "a" * 79 + "-", "a--b",
                   "--a", "a--", "a---b", "a-b-c", "a-.-b", "a.-_b", "-", "--", *characters,
                   *("a" + character for character in characters),
                   *("a" + character + "a" for character in characters),
                   *("a-" + character for character in characters)]
        program = ('fail() { exit 1; }\nwhile IFS= read -r -d "" TARGET; do\n  if ( ' + checks.pop()
                   + ' ); then printf 1; else printf 0; fi\ndone\n')
        for locale in ("C", "C.UTF-8"):
            result = subprocess.run(["bash", "-c", program], input="\0".join(samples) + "\0", capture_output=True,
                                    text=True, timeout=60, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                                                                "LC_ALL": locale})
            self.assertEqual((result.returncode, len(result.stdout)), (0, len(samples)), result.stderr)
            for sample, verdict in zip(samples, result.stdout):
                with self.subTest(locale=locale, unit=sample):
                    self.assertEqual(verdict == "1", grammar.is_match(grammar.CI_UNIT_ID, sample))
        for name in (COMPILE, SEAL):
            self.assertEqual(self.run_step("target", name, TARGET="1.20.1-fabric").result.returncode, 0)

    def test_a_missing_tool_or_a_failing_command_fails_the_step(self) -> None:
        for home in JDKS:
            outcome = self.run_step("plan", PREPARE, **{home: None})
            self.assertNotEqual(outcome.result.returncode, 0, home)
            self.assertEqual(outcome.commands, [], "an image without one of the JDKs prepares no worker")
        for job_id, job in self.jobs.items():
            for item in job["steps"][len(PROLOGUE):]:
                if "run" in item:
                    with self.subTest(job=job_id, step=item["name"]):
                        outcome = self.runner.run(item, STUB_PYTHON3_SCRIPT="raise SystemExit(3)")
                        self.assertEqual(outcome.result.returncode, 3, "the step's status is the kit's")


if __name__ == "__main__":
    unittest.main()
