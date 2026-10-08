"""Textual policy of the Build/E2E callee workflows (docs/BUILD-E2E-DESIGN.md, "Build controller prologue").

The Pages callees keep their registry (``workflow.CALLEE_WORKFLOWS``) and their tests
(``tests/test_workflow_policy.py``). This module applies the same rules to the second registry,
``workflow.CI_CALLEE_WORKFLOWS``, through that registry's own tables: ``CI_CALLEE_JOBS`` (job ids
and names), ``CI_JOB_VERBS`` (the ``ci`` verb of every step, in order), ``CI_JOB_ARTIFACTS`` (the
sealing jobs and what they upload) and ``CI_JOB_PERMISSIONS``. A callee is policed from the moment
its file exists: a workflow file without rows in those tables fails, and so do rows without a file.

The module also holds what the per-workflow modules (``tests/test_workflow_build.py``,
``tests/test_workflow_select_build.py`` and ``tests/test_workflow_packaged_e2e.py``) import
(functions and tables only, so no test runs twice): the loader, the closed tables below and
:class:`CiStepRunner`, which executes a step's ``run:`` body under the Pages tests' ``ShellHarness``
and returns the ``ci`` command lines it issued.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import cli, workflow
from mod_base.adapter import protocol
from mod_base.build_ci import graph
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model import limits as lim
from tests.helpers import ci_plan
from tests.test_tree_digest_literal import (GIT_STUB, SHELL, digest_block, make_kit, reference_digest,
                                            update_tree_digest)
from tests.test_workflow_pins import uses_values
from tests.test_workflow_policy import (CHECKOUT, EXPRESSION, KIT_PYTHON, PINNED_ACTIONS, PROLOGUE, ROOT, SCRUB,
                                        SCRUB_API, SETUP_PYTHON, TOKEN_VALUE, UPLOAD, ShellHarness, callee,
                                        load_yaml, outputs, parse_kit_argv, require_tools, step)

WORKFLOWS = ROOT / ".github/workflows"
#: mod-base's own CI, the one kit workflow no mod calls.
OWN_WORKFLOW = "ci.yml"
CI_CALLEE_PATHS = {name: ROOT / path for name, path in workflow.CI_CALLEE_WORKFLOWS.items()}
#: The callees whose workflow file is written: exactly the ids the registry's job tables describe.
CI_CALLEES = tuple(workflow.CI_JOB_VERBS)

#: The inputs of each callee, in order; ``kit-sha`` is the only required one.
CI_INPUTS = {"build": ("kit-sha", "pr-number"), "select-build": ("kit-sha",),
             "packaged-e2e": ("kit-sha", "pr-number", "mode", "build-run-id")}
#: What each callee returns to its caller, in order: output -> the job that has an output of that name.
CI_OUTPUTS = {"build": {}, "select-build": {"mode": "select", "found": "select", "build-run-id": "select"},
              "packaged-e2e": {}}
#: The producer each callee's jobs authenticate as (``ci subject --producer``, ``ci seal-gate --gate``).
CI_PRODUCER = {"build": "build", "select-build": "packaged", "packaged-e2e": "packaged"}
#: Where a callee reads its mode and the tested commit from (the callee's own spelling): the callees
#: whose gate seals one of two records, and the callees with a job that stages candidate code.
MODE_EXPRESSION = {"build": "needs.plan.outputs.mode", "packaged-e2e": "inputs.mode"}
CANDIDATE_REF = {"build": "${{ needs.plan.outputs.tested-sha }}",
                 "packaged-e2e": "${{ needs.input.outputs.tested-sha }}"}
#: Where the environment of a callee's steps differs from a Build step of pull request 17: the
#: managed caller that reaches the callee and, for the one no pull request reaches, the event.
PACKAGED_CALLER_REF = "example/mod/" + workflow.CI_CALLER_WORKFLOWS["packaged"] + "@refs/heads/master"
CI_ENVIRONMENT = {"build": {},
                  "select-build": {"GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW_REF": PACKAGED_CALLER_REF},
                  "packaged-e2e": {"GITHUB_WORKFLOW_REF": PACKAGED_CALLER_REF}}

#: The third checkout, present exactly in the jobs that stage and run candidate code.
CANDIDATE_CHECKOUT = "Check out the tested candidate"
CANDIDATE_VERBS = frozenset({"worker-stage", "worker-run"})
#: The verbs that call the API: exactly the steps that run one hold the step-scoped token.
API_VERBS = frozenset({"subject", "plan", "reuse-admit", "assemble", "seal-gate", "select-build", "fetch-build",
                       "aggregate"})
#: The verbs that must also run after a failure or a cancellation, and the step each depends on.
#: A bare ``always()`` would run kit Python in a job whose prologue failed, that is from a kit tree
#: nobody verified; a step that follows the prologue can only have succeeded when the prologue did.
#: The sweep runs once the job authenticated its subject (the next step creates the accounts), the
#: candidate is locked once its account exists.
ALWAYS_VERBS = {"worker-seal": "${{ always() && steps.prepare.outcome == 'success' }}",
                "worker-finish": "${{ always() && steps.subject.outcome == 'success' }}"}
#: The step ids those conditions name, by the verb of the step that carries each.
GATING_IDS = {"subject": "subject", "worker-prepare": "prepare"}
#: The only other condition a step may carry, by callee, job and verb. Reuse is admitted for a push
#: alone, and a job that also runs in reuse mode selects no Build then. (The packaged ``input``,
#: ``lane`` and ``aggregate`` jobs are skipped as a whole in that mode.)
CONDITIONAL_STEPS = {
    ("build", "plan", "reuse-admit"): "github.event_name == 'push'",
    ("select-build", "select", "reuse-admit"): "github.event_name == 'push'",
    ("select-build", "select", "select-build"): "steps.reuse.outputs.mode != 'reuse'",
    ("packaged-e2e", "gate", "select-build"): "inputs.mode != 'reuse'",
}
#: The verbs of a job's one ``workflow.CI_SEAL_STEP``: a hook of the validator account, a gate, or
#: the kit's own index of the lane results (``ci aggregate`` runs no hook).
SEAL_VERBS = frozenset({"worker-validate", "seal-gate", "aggregate"})
#: The third-party actions a Build/E2E callee may use, at the reviewed pins of the Pages callees.
CI_ACTIONS = frozenset({CHECKOUT, SETUP_PYTHON, UPLOAD})
UPLOAD_PATH = "${{ runner.temp }}/mb-upload"
UPLOAD_FLAG = '--output "$RUNNER_TEMP/mb-upload"'
RUN_EXPRESSION, ATTEMPT_EXPRESSION = "${{ github.run_id }}", "${{ github.run_attempt }}"

#: The ``ci`` verbs the workflows already issue but no work package has registered yet. Each one
#: leaves this set in the change that registers it; from then on its command lines must parse.
PENDING_VERBS = frozenset({"worker-stage", "worker-run", "worker-seal"})

#: The one fixed first line of every kit command of a Build/E2E job.
CI_COMMAND = re.compile(r"^  python3 -P -m mod_base ci ([a-z][a-z-]*) --repo mod --config mod/site/mod-base\.json "
                        r'--state "\$RUNNER_TEMP/mb-state"(?: \\)?$', re.MULTILINE)
#: What a step's ``env:`` may carry: a call input, an output of an earlier job, the matrix unit or
#: the step-scoped token. Never an event payload field and never a secret.
ENV_VALUE = re.compile(r"^\$\{\{ (?:inputs\.[a-z][a-z0-9-]*|needs\.[a-z][a-z-]*\.outputs\.[a-z][a-z0-9-]*|matrix\.id"
                       r"|github\.token) \}\}$")
#: What a job may return: one output of one of its own steps, or a choice between two literals by it.
JOB_OUTPUT = re.compile(r"^\$\{\{ steps\.[a-z]+\.outputs\.[a-z0-9_]+(?: == '[a-z]+' && '[a-z]+' \|\| '[a-z]+')? \}\}$")
DOCUMENT_KEYS = {"name", "on", "permissions", "env", "jobs"}
JOB_KEYS = {"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "outputs", "strategy", "steps"}
RUN_STEP_KEYS = {"name", "id", "if", "shell", "env", "run"}
USES_STEP_KEYS = {"name", "uses", "with"}

#: The one difference between the Pages binding step and the Build/E2E one: which workflow of the
#: canonical branch may call the job.
PAGES_GUARD = ('[[ "$GITHUB_WORKFLOW_REF" == "$GITHUB_REPOSITORY/' + workflow.PAGES_WORKFLOW_PATH
               + '@refs/heads/$canonical_branch" ]] ||\n'
               "  fail \"this job is not called by the canonical branch's pages.yml\"\n")
CI_GUARD = ("[[ " + " ||\n   ".join(
    '"$GITHUB_WORKFLOW_REF" == "$GITHUB_REPOSITORY/' + workflow.CI_CALLER_WORKFLOWS[producer]
    + '@refs/heads/$canonical_branch"' for producer in workflow.CI_CALLS) + " ]] ||\n"
    '  fail "this job is not called by a managed Build or packaged E2E caller of the canonical branch"\n')


def ci_callee(name: str) -> dict[str, Any]:
    return load_yaml(CI_CALLEE_PATHS[name])


def iter_ci_jobs() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """``(callee id, job id, job)`` for every job of every written Build/E2E callee."""

    for name in CI_CALLEES:
        for job_id, job in ci_callee(name)["jobs"].items():
            yield name, job_id, job


def ci_verbs(script: str) -> list[str]:
    """The ``ci`` verbs a run script issues, each on the fixed first line of a kit command."""

    return CI_COMMAND.findall(script)


def step_verb(item: Mapping[str, Any]) -> str | None:
    """The ``ci`` verb a step runs, or ``None`` for a step that runs no kit command."""

    verbs = ci_verbs(item.get("run", ""))
    if len(verbs) > 1:
        raise AssertionError(f"step {item.get('name')!r} runs more than one ci command")
    return verbs[0] if verbs else None


def is_matrix_job(name: str, job_id: str) -> bool:
    return "{id}" in workflow.CI_CALLEE_JOBS[name][job_id]


def upload_name(name: str, job_id: str) -> str:
    """The ``name:`` of a sealing job's upload step, written from ``workflow.CI_JOB_ARTIFACTS``.

    A job that uploads one kind spells the artifact name with the run, the attempt and (for a
    matrix job) the unit as expressions; a gate, which seals a tested record or a reuse reference,
    picks one of two templates by the callee's mode in a single ``format`` call."""

    kinds = workflow.CI_JOB_ARTIFACTS[name][job_id]

    def template(kind: str, run: str, attempt: str) -> str:
        unit = CI_PRODUCER[name] if kind == "tested" else "${{ matrix.id }}" if is_matrix_job(name, job_id) else None
        return f"{grammar.CI_ARTIFACT_PREFIXES[kind]}--{run}--a{attempt}" + ("" if unit is None else f"--{unit}")

    if list(kinds) == ["full"]:
        return template(kinds["full"], RUN_EXPRESSION, ATTEMPT_EXPRESSION)
    if list(kinds) == ["full", "reuse"] and not is_matrix_job(name, job_id):
        return ("${{ format(" + MODE_EXPRESSION[name] + " == 'reuse' && '" + template(kinds["reuse"], "{0}", "{1}")
                + "' || '" + template(kinds["full"], "{0}", "{1}") + "', github.run_id, github.run_attempt) }}")
    raise AssertionError(f"{name}/{job_id}: no upload name is defined for the modes {sorted(kinds)}")


def _verbs_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    actions = [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]
    if len(actions) != 1:
        raise AssertionError("expected exactly one sub-command group")
    return actions[0]


def registered_ci_verbs() -> frozenset[str]:
    """The verbs the kit's own ``ci`` parser accepts today."""

    return frozenset(_verbs_action(_verbs_action(cli.build_parser("ci")).choices["ci"]).choices)


# -- Executing a step's shell ------------------------------------------------------------------------

#: One value for every name a step's ``env:`` may define; an unknown name fails the run. ``MODE``
#: and ``BUILD_RUN_ID`` are the packaged call inputs of a pull request (both empty); the run the
#: ``input`` job then authenticated reaches the later jobs as ``SELECTED_RUN_ID``.
SAMPLES = {"KIT_SHA": "b" * 40, "PR_NUMBER": "17", "GH_TOKEN": "step-token", "PLAN_SHA256": "c" * 64,
           "TESTED_SHA": "d" * 40, "TARGET": "target-a", "LANE": "lane-a", "MODE": "", "BUILD_RUN_ID": "",
           "SELECTED_RUN_ID": "36042781699"}
#: What the runner could leak into a step: every script scrubs these before it runs anything.
AMBIENT = ("ACTIONS_RUNTIME_TOKEN", "ACTIONS_CACHE_URL", "ACTIONS_RESULTS_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
           "ACTIONS_ID_TOKEN_REQUEST_URL", "GITHUB_TOKEN", "GH_TOKEN")
ISOLATION = {"PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
#: The JDKs of the runner image, by the variable that names each home.
JDKS = {f"JAVA_HOME_{version}_X64": f"/usr/lib/jvm/temurin-{version}-jdk-amd64" for version in (17, 21, 25)}


@dataclass(frozen=True)
class StepOutcome:
    """One executed ``run:`` body: the process, the kit command lines it issued (the words after
    ``python3 -P -m mod_base``), the environment each of them saw and what the step exported."""

    result: subprocess.CompletedProcess[str]
    commands: list[list[str]]
    environments: list[dict[str, str | None]]
    exported: dict[str, str]


class CiStepRunner:
    """A job's workspace after its checkouts, with stub ``git`` and ``python3``.

    ``kit/`` is a small kit tree whose kit-digest-v1 is the workflow literal, ``mod/site/mod-base.json``
    names the canonical branch ``master`` and the environment is the one a step of pull request 17
    gets in the callee ``name`` (``CI_ENVIRONMENT``), plus every credential a runner could leak.
    ``git`` answers ``rev-parse HEAD`` from ``STUB_<CHECKOUT>_HEAD`` and ``python3`` records its
    command line and environment."""

    def __init__(self, root: Path, name: str = "build") -> None:
        self.harness = ShellHarness(root / "harness", stubs=("git", "python3"))
        self.workspace = root / "work space"
        make_kit(self.workspace / "kit")
        (self.workspace / "mod/site").mkdir(parents=True)
        (self.workspace / "mod/site/mod-base.json").write_text('{"canonical_branch": "master"}\n', encoding="utf-8")
        self.runner_temp = root / "runner temp"
        self.output = root / "github_output"
        self.exported = root / "github_env"
        self.python = str(self.harness.bin / "python3")
        self.base = {
            "GITHUB_SHA": "a" * 40, "GITHUB_REF": "refs/heads/master", "GITHUB_EVENT_NAME": "pull_request_target",
            "GITHUB_REPOSITORY": "example/mod", "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
            "GITHUB_WORKFLOW_REF": "example/mod/" + workflow.CI_CALLER_WORKFLOWS["build"] + "@refs/heads/master",
            "GITHUB_OUTPUT": str(self.output), "GITHUB_ENV": str(self.exported),
            "GITHUB_WORKSPACE": str(self.workspace), "RUNNER_TEMP": str(self.runner_temp),
            "MB_KIT_TREE_DIGEST": reference_digest(self.workspace / "kit"), **JDKS,
            "STUB_GIT_SCRIPT": GIT_STUB, "STUB_MOD_HEAD": "a" * 40, "STUB_KIT_HEAD": "b" * 40,
            "STUB_CANDIDATE_HEAD": SAMPLES["TESTED_SHA"], **dict.fromkeys(AMBIENT, "leaked"),
            **CI_ENVIRONMENT[name],
        }

    def run(self, item: Mapping[str, Any], **overrides: str | None) -> StepOutcome:
        """Run the step ``item``; an override replaces one variable and ``None`` removes it."""

        for path in (self.output, self.exported):
            path.unlink(missing_ok=True)
        shutil.rmtree(self.runner_temp, ignore_errors=True)
        self.runner_temp.mkdir()
        unknown = sorted(set(item.get("env", {})) - set(SAMPLES))
        if unknown:
            raise AssertionError(f"step {item.get('name')!r} defines {unknown}: give each a value in SAMPLES")
        environment = {**self.base, **{name: SAMPLES[name] for name in item.get("env", {})}, **overrides}
        environment = {name: value for name, value in environment.items() if value is not None}
        result = self.harness.run(item["run"], environment, cwd=self.workspace,
                                  record_env=(*AMBIENT, "PYTHONPATH", *ISOLATION))
        kit = [record for record in self.harness.records()
               if record["tool"] == "python3" and record["argv"][:3] == ["-P", "-m", "mod_base"]]
        return StepOutcome(result, [record["argv"][3:] for record in kit], [record["env"] for record in kit],
                           outputs(self.exported))


# -- The registry ------------------------------------------------------------------------------------


class CiRegistryTests(unittest.TestCase):
    def test_job_tables_spelled_out(self) -> None:
        self.assertEqual(workflow.CI_JOB_VERBS, {
            "build": {
                "plan": ("subject", "worker-prepare", "plan", "reuse-admit", "worker-finish"),
                "policy": ("subject", "worker-prepare", "plan", "worker-stage", "worker-run", "worker-seal",
                           "worker-finish"),
                "target": ("subject", "worker-prepare", "plan", "worker-stage", "worker-run", "worker-seal",
                           "worker-validate", "worker-finish"),
                "assemble": ("subject", "worker-prepare", "plan", "assemble", "worker-validate", "worker-finish"),
                "gate": ("subject", "worker-prepare", "plan", "seal-gate", "worker-finish"),
            },
            "select-build": {
                "select": ("subject", "worker-prepare", "plan", "reuse-admit", "select-build", "worker-finish"),
            },
            "packaged-e2e": {
                "input": ("subject", "worker-prepare", "plan", "select-build", "worker-finish"),
                "lane": ("subject", "worker-prepare", "plan", "select-build", "fetch-build", "worker-stage",
                         "worker-run", "worker-seal", "worker-validate", "worker-finish"),
                "aggregate": ("subject", "worker-prepare", "plan", "select-build", "aggregate", "worker-finish"),
                "gate": ("subject", "worker-prepare", "plan", "select-build", "seal-gate", "worker-finish"),
            },
        })
        self.assertEqual(list(workflow.CI_JOB_VERBS),
                         [name for name in workflow.CI_CALLEE_WORKFLOWS if name in workflow.CI_JOB_VERBS],
                         "the tables keep the registry's order")
        self.assertEqual(workflow.CI_JOB_ARTIFACTS, {
            "build": {"target": {"full": "target"}, "assemble": {"full": "build"},
                      "gate": {"full": "tested", "reuse": "reuse"}},
            "packaged-e2e": {"lane": {"full": "runtime"}, "aggregate": {"full": "results"},
                             "gate": {"full": "tested", "reuse": "reuse"}}})
        read = {"actions": "read", "contents": "read", "pull-requests": "read"}
        self.assertEqual(workflow.CI_JOB_PERMISSIONS, {
            "build": {"plan": read, "policy": read, "target": read, "assemble": read, "gate": read},
            "select-build": {"select": read},
            "packaged-e2e": {"input": read, "lane": read, "aggregate": read, "gate": read}})

    def test_every_table_describes_exactly_the_jobs_of_its_callee(self) -> None:
        self.assertEqual(set(workflow.CI_JOB_PERMISSIONS), set(CI_CALLEES))
        self.assertLessEqual(set(workflow.CI_JOB_ARTIFACTS), set(CI_CALLEES))
        self.assertLessEqual(set(CI_CALLEES), set(workflow.CI_CALLEE_WORKFLOWS))
        for table in (CI_INPUTS, CI_OUTPUTS, CI_PRODUCER, CI_ENVIRONMENT):
            self.assertEqual(set(table), set(CI_CALLEES))
        # A mode is spelled where a gate seals one of two records, a tested commit where a job
        # stages candidate code; a callee without either has no row to go stale.
        self.assertEqual(set(MODE_EXPRESSION), {name for name, jobs in workflow.CI_JOB_ARTIFACTS.items()
                                                if any(len(kinds) > 1 for kinds in jobs.values())})
        self.assertEqual(set(CANDIDATE_REF), {name for name in CI_CALLEES if any(
            CANDIDATE_VERBS & set(verbs) for verbs in workflow.CI_JOB_VERBS[name].values())})
        for (name, job_id, verb), condition in CONDITIONAL_STEPS.items():
            self.assertIn(verb, workflow.CI_JOB_VERBS[name][job_id], "a condition is tabled for a step that exists")
            self.assertNotIn(verb, ALWAYS_VERBS)
            self.assertNotRegex(condition, r"always\(\)|failure\(\)|cancelled\(\)|success\(\)")
        for name in CI_CALLEES:
            jobs = list(workflow.CI_CALLEE_JOBS[name])
            with self.subTest(callee=name):
                self.assertEqual(list(workflow.CI_JOB_VERBS[name]), jobs)
                self.assertEqual(list(workflow.CI_JOB_PERMISSIONS[name]), jobs)
                self.assertIn(CI_PRODUCER[name], workflow.CI_CALLS)
                for job_id, verbs in workflow.CI_JOB_VERBS[name].items():
                    sealing = [verb for verb in verbs if verb in SEAL_VERBS]
                    self.assertEqual(len(sealing), int(job_id in workflow.CI_JOB_ARTIFACTS.get(name, {})), job_id)
                    self.assertEqual(verbs[:2], ("subject", "worker-prepare"), job_id)
                    self.assertEqual(verbs[-1], "worker-finish", job_id)
                    self.assertEqual(len(set(verbs)), len(verbs), job_id)
                for job_id, kinds in workflow.CI_JOB_ARTIFACTS.get(name, {}).items():
                    self.assertIn(job_id, jobs)
                    self.assertEqual(len({lim.CI_RETENTION_DAYS[kind] for kind in kinds.values()}), 1, job_id)
                    self.assertLessEqual(set(kinds.values()), set(grammar.CI_ARTIFACT_PREFIXES), job_id)

    def test_a_callee_is_tabled_exactly_when_its_file_exists(self) -> None:
        for name, path in CI_CALLEE_PATHS.items():
            with self.subTest(callee=name):
                self.assertEqual(path.is_file(), name in CI_CALLEES,
                                 "a Build/E2E callee and its rows in workflow.CI_JOB_VERBS arrive together")

    def test_every_kit_workflow_file_is_registered(self) -> None:
        registered = {Path(path).name for path in workflow.CALLEE_WORKFLOWS.values()}
        registered |= {CI_CALLEE_PATHS[name].name for name in CI_CALLEES}
        self.assertEqual({path.name for path in WORKFLOWS.iterdir()}, registered | {OWN_WORKFLOW},
                         "a workflow outside both registries is policed by nothing")

    def test_every_callee_has_its_own_test_module_in_the_workflow_policy_job(self) -> None:
        policy = load_yaml(WORKFLOWS / OWN_WORKFLOW)["jobs"]["workflow-policy"]
        script = step(policy["steps"], "Run the textual workflow policy tests")["run"]
        self.assertRegex(script, r"\btests\.test_workflow_ci_policy\b")
        for name in CI_CALLEES:
            module = "test_workflow_" + name.replace("-", "_")
            with self.subTest(callee=name):
                self.assertTrue((ROOT / "tests" / f"{module}.py").is_file(), "the job graph of each callee is pinned")
                self.assertRegex(script, rf"\btests\.{module}\b")

    def test_the_artifact_table_names_the_uploading_jobs_of_the_run_graph(self) -> None:
        target = ci_plan()["targets"][0]["id"]
        for name in CI_CALLEES:
            producer = CI_PRODUCER[name]
            call = next(call for call, called in workflow.CI_CALLS[producer].items() if called == name)
            for job_id, kinds in workflow.CI_JOB_ARTIFACTS.get(name, {}).items():
                fields = {"id": target} if is_matrix_job(name, job_id) else {}
                for kind in kinds.values():
                    unit = producer if kind == "tested" else fields.get("id")
                    with self.subTest(callee=name, job=job_id, kind=kind):
                        self.assertEqual(graph.upload_job_name(producer, kind, unit),
                                         workflow.ci_api_job_name(producer, call, job_id, **fields))


# -- The policy --------------------------------------------------------------------------------------


class CiCalleePolicyTests(unittest.TestCase):
    def test_callees_are_workflow_call_only_without_concurrency_or_secrets(self) -> None:
        for name in CI_CALLEES:
            document = ci_callee(name)
            text = CI_CALLEE_PATHS[name].read_text(encoding="utf-8")
            with self.subTest(callee=name):
                self.assertEqual(set(document), DOCUMENT_KEYS)
                self.assertEqual(document["name"], f"mod-base {name}")
                self.assertEqual(list(document["on"]), ["workflow_call"])
                self.assertEqual(list(document["on"]["workflow_call"]),
                                 ["inputs", "outputs"] if CI_OUTPUTS[name] else ["inputs"], "no secrets")
                self.assertEqual(document["permissions"], {})
                self.assertEqual(list(document["env"]), ["MB_KIT_TREE_DIGEST"])
                self.assertRegex(document["env"]["MB_KIT_TREE_DIGEST"], r"^sha256:[0-9a-f]{64}$")
                for job_id, job in document["jobs"].items():
                    self.assertLessEqual(set(job), JOB_KEYS, f"{job_id}: no concurrency, secrets, uses, environment "
                                                             "or job-level env")
                    self.assertEqual(job["runs-on"], "ubuntu-24.04", job_id)
                for forbidden in ("secrets", "concurrency", "github.event.", "continue-on-error"):
                    self.assertNotIn(forbidden, text)
                self.assertNotIn("{{", text.replace("${{", ""))

    def test_inputs_are_strings_and_only_the_kit_sha_is_required(self) -> None:
        for name in CI_CALLEES:
            inputs = ci_callee(name)["on"]["workflow_call"]["inputs"]
            with self.subTest(callee=name):
                self.assertEqual(tuple(inputs), CI_INPUTS[name])
                self.assertEqual(CI_INPUTS[name][0], "kit-sha")
                for input_name, spec in inputs.items():
                    self.assertEqual(spec["type"], "string", input_name)
                    if input_name == "kit-sha":
                        self.assertEqual(spec["required"], "true")
                        self.assertNotIn("default", spec)
                    else:
                        self.assertEqual((spec["required"], spec["default"]), ("false", ""), input_name)

    def test_outputs_are_the_same_named_outputs_of_one_job(self) -> None:
        for name in CI_CALLEES:
            document = ci_callee(name)
            declared = document["on"]["workflow_call"].get("outputs", {})
            with self.subTest(callee=name):
                self.assertEqual(list(declared), list(CI_OUTPUTS[name]))
                for output, job_id in CI_OUTPUTS[name].items():
                    self.assertEqual(set(declared[output]), {"description", "value"}, output)
                    self.assertEqual(declared[output]["value"], "${{ jobs." + job_id + ".outputs." + output + " }}")
                    self.assertIn(output, document["jobs"][job_id]["outputs"])
                # A job output is a value its own steps wrote: nothing of the event, no input and no token.
                for job_id, job in document["jobs"].items():
                    for output, value in job.get("outputs", {}).items():
                        self.assertRegex(value, JOB_OUTPUT, f"{job_id}.{output}")

    def test_jobs_are_the_registry_jobs_with_their_names_and_permissions(self) -> None:
        for name in CI_CALLEES:
            jobs = ci_callee(name)["jobs"]
            with self.subTest(callee=name):
                self.assertEqual(list(jobs), list(workflow.CI_CALLEE_JOBS[name]))
            for job_id, job in jobs.items():
                with self.subTest(callee=name, job=job_id):
                    self.assertEqual(job["name"], workflow.ci_workflow_template_name(name, job_id))
                    self.assertEqual(job["permissions"], workflow.CI_JOB_PERMISSIONS[name][job_id])
                    self.assertNotIn("write", job["permissions"].values(), "no Build/E2E job writes")
                    # The Pages hook host decides by $GITHUB_JOB alone where a Pages hook may run.
                    self.assertNotIn(job_id, protocol.TOKEN_JOBS | protocol.FORBIDDEN_JOBS)
                    self.assertRegex(job["timeout-minutes"], r"^[1-9][0-9]{0,2}$")
                    self.assertLessEqual(int(job["timeout-minutes"]), 360)
                    if is_matrix_job(name, job_id):
                        self.assertEqual(set(job["strategy"]), {"fail-fast", "matrix"}, "no max-parallel")
                        self.assertEqual(job["strategy"]["fail-fast"], "false")
                        self.assertEqual(list(job["strategy"]["matrix"]), ["id"])
                    else:
                        self.assertNotIn("strategy", job)

    def test_prologue_runs_first_and_in_order(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            candidate = bool(CANDIDATE_VERBS & set(workflow.CI_JOB_VERBS[name][job_id]))
            checkouts = [item["name"] for item in steps if str(item.get("uses", "")).startswith("actions/checkout@")]
            with self.subTest(callee=name, job=job_id):
                self.assertEqual([item["name"] for item in steps[:len(PROLOGUE)]], list(PROLOGUE))
                self.assertEqual(checkouts, [PROLOGUE[1], PROLOGUE[2], *([CANDIDATE_CHECKOUT] if candidate else [])],
                                 "the two prologue checkouts, and the candidate exactly where its code is staged")
                for item in steps:
                    keys = USES_STEP_KEYS if "uses" in item else RUN_STEP_KEYS
                    self.assertLessEqual(set(item), keys, item["name"])
                    self.assertTrue(("uses" in item) != ("run" in item), item["name"])

    def test_prologue_steps_are_identical_wherever_they_repeat(self) -> None:
        shared: dict[str, list[dict[str, Any]]] = {name: [] for name in PROLOGUE[1:]}
        for name in CI_CALLEES:
            validations = []
            for job in ci_callee(name)["jobs"].values():
                validations.append(step(job["steps"], PROLOGUE[0]))
                for step_name in shared:
                    shared[step_name].append(step(job["steps"], step_name))
            self.assertTrue(all(item == validations[0] for item in validations),
                            f"{name}: one input validation for every job")
        for step_name, copies in shared.items():
            self.assertTrue(all(item == copies[0] for item in copies),
                            f"{step_name!r} is byte-identical in every Build/E2E callee job")

    def test_the_prologue_is_the_pages_prologue_bound_to_the_managed_build_callers(self) -> None:
        pages = callee("finalize")["jobs"]["refresh"]["steps"]
        pages_bind = step(pages, PROLOGUE[3])
        self.assertEqual(pages_bind["run"].count(PAGES_GUARD), 1)
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            with self.subTest(callee=name, job=job_id):
                for index in (1, 2, 4, 5):
                    self.assertEqual(step(steps, PROLOGUE[index]), step(pages, PROLOGUE[index]))
                self.assertEqual(step(steps, PROLOGUE[3]),
                                 {**pages_bind, "run": pages_bind["run"].replace(PAGES_GUARD, CI_GUARD)})
                validation = step(steps, PROLOGUE[0])
                self.assertEqual(validation["env"], {"KIT_SHA": "${{ inputs.kit-sha }}", **{
                    input_name.upper().replace("-", "_"): "${{ inputs." + input_name + " }}"
                    for input_name in CI_INPUTS[name][1:]}})

    def test_checkouts_bind_the_protected_head_the_verified_kit_and_the_planned_candidate(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            with self.subTest(callee=name, job=job_id):
                self.assertEqual(step(steps, PROLOGUE[1]), {"name": PROLOGUE[1], "uses": CHECKOUT, "with": {
                    "ref": "${{ github.sha }}", "path": "mod", "persist-credentials": "false"}})
                self.assertEqual(step(steps, PROLOGUE[2]), {"name": PROLOGUE[2], "uses": CHECKOUT, "with": {
                    "repository": "The-Plum-Team/mod-base", "ref": "${{ inputs.kit-sha }}", "path": "kit",
                    "persist-credentials": "false"}})
                if CANDIDATE_VERBS & set(workflow.CI_JOB_VERBS[name][job_id]):
                    self.assertEqual(steps[len(PROLOGUE)], {"name": CANDIDATE_CHECKOUT, "uses": CHECKOUT, "with": {
                        "ref": CANDIDATE_REF[name], "path": "candidate", "persist-credentials": "false"}})

    def test_every_kit_invocation_is_isolated_and_the_next_verb_of_its_job(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            issued = []
            for index, item in enumerate(job["steps"]):
                script = item.get("run", "")
                lines = script.splitlines()
                mentions = [number for number, line in enumerate(lines) if "mod_base" in line]
                with self.subTest(callee=name, job=job_id, step=item["name"]):
                    verbs = ci_verbs(script)
                    self.assertEqual(len(verbs), len(mentions), "the kit is named only on a ci command line")
                    self.assertLessEqual(len(verbs), 1, "one ci command per step")
                    self.assertNotRegex(script, r"(?m)^\s*python3? (?!-m pip install |-P -m mod_base ci )")
                    if index < len(PROLOGUE):
                        self.assertEqual(verbs, [], "the prologue runs no kit command")
                    for number in mentions:
                        self.assertEqual(lines[number - 1] + "\n" + lines[number][:lines[number].index("mod_base") + 8],
                                         KIT_PYTHON.rstrip())
                        # The command is the step's last statement: its status is the step's.
                        self.assertTrue(all(line.endswith(" \\") for line in lines[number:-1]))
                        self.assertTrue(all(line.startswith("  --") for line in lines[number + 1:]))
                        self.assertFalse(lines[-1].endswith("\\"))
                issued.extend(ci_verbs(script))
            with self.subTest(callee=name, job=job_id):
                self.assertEqual(tuple(issued), workflow.CI_JOB_VERBS[name][job_id])

    def test_run_blocks_start_strict_and_scrubbed_and_interpolate_no_expression(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            for item in job["steps"]:
                if "run" not in item:
                    continue
                lines = item["run"].splitlines()
                with self.subTest(callee=name, job=job_id, step=item["name"]):
                    self.assertNotRegex(item["run"], EXPRESSION)
                    self.assertEqual(item["shell"], "bash")
                    self.assertEqual(lines[0], "set -euo pipefail")
                    self.assertEqual(lines[1], SCRUB_API if "GH_TOKEN" in item.get("env", {}) else SCRUB)

    def test_tokens_are_step_scoped_and_only_where_the_api_is_called(self) -> None:
        for name in CI_CALLEES:
            holders = 0
            for job_id, job in ci_callee(name)["jobs"].items():
                for item in job["steps"]:
                    environment = item.get("env", {})
                    with self.subTest(callee=name, job=job_id, step=item["name"]):
                        self.assertEqual("GH_TOKEN" in environment, step_verb(item) in API_VERBS)
                        self.assertNotIn("GITHUB_TOKEN", environment)
                        self.assertNotIn("token", item.get("with", {}), "checkouts use the job token read-only")
                        for variable, value in environment.items():
                            self.assertRegex(value, ENV_VALUE, variable)
                        if "GH_TOKEN" in environment:
                            self.assertEqual(environment["GH_TOKEN"], TOKEN_VALUE)
                            holders += 1
            text = CI_CALLEE_PATHS[name].read_text(encoding="utf-8")
            self.assertEqual(text.count("github.token"), holders, f"{name}: the token appears nowhere else")
        tabled = {verb for jobs in workflow.CI_JOB_VERBS.values() for verbs in jobs.values() for verb in verbs}
        for verbs in (API_VERBS, ALWAYS_VERBS, GATING_IDS, SEAL_VERBS, CANDIDATE_VERBS):
            self.assertLessEqual(set(verbs), tabled, "a closed verb table names a verb no job runs")

    def test_steps_run_unconditionally_except_the_tabled_verbs(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            for item in job["steps"]:
                verb = step_verb(item)
                with self.subTest(callee=name, job=job_id, step=item["name"]):
                    self.assertEqual(item.get("if"),
                                     ALWAYS_VERBS.get(verb, CONDITIONAL_STEPS.get((name, job_id, verb))))
                    if verb in GATING_IDS:
                        self.assertEqual(item.get("id"), GATING_IDS[verb])
                    else:
                        self.assertNotIn(item.get("id"), GATING_IDS.values())
            # No step may run after a failure unless a step that follows the prologue succeeded.
            text = "\n".join(str(item.get("if", "")) for item in job["steps"])
            self.assertEqual(text.count("always()"), sum(verb in ALWAYS_VERBS
                                                         for verb in workflow.CI_JOB_VERBS[name][job_id]))
            for function in ("failure()", "cancelled()", "success()"):
                self.assertNotIn(function, text)

    def test_every_uses_is_a_reviewed_pin(self) -> None:
        for name in CI_CALLEES:
            for value in uses_values(CI_CALLEE_PATHS[name]):
                action, _, comment = value.partition(" # ")
                with self.subTest(callee=name, uses=action):
                    self.assertIn(action, CI_ACTIONS, "inline steps only: no composite, no local and no new action")
                    self.assertEqual(comment, PINNED_ACTIONS[action])
            for job_id, job in ci_callee(name)["jobs"].items():
                python = step(job["steps"], PROLOGUE[4])
                self.assertEqual((python["uses"], python["with"]), (SETUP_PYTHON, {"python-version": "3.13"}), job_id)

    def test_sealing_jobs_seal_and_then_upload_exactly_as_tabled(self) -> None:
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            names = [item["name"] for item in steps]
            uploads = [item for item in steps if str(item.get("uses", "")).startswith("actions/upload-artifact@")]
            kinds = workflow.CI_JOB_ARTIFACTS.get(name, {}).get(job_id)
            sealing = (names.count(workflow.CI_SEAL_STEP), names.count(workflow.CI_UPLOAD_STEP))
            with self.subTest(callee=name, job=job_id):
                if kinds is None:
                    self.assertEqual((uploads, sealing), ([], (0, 0)))
                    continue
                self.assertEqual(sealing, (1, 1))
                position = names.index(workflow.CI_SEAL_STEP)
                seal = steps[position]
                self.assertIn(step_verb(seal), SEAL_VERBS)
                self.assertTrue(seal["run"].splitlines()[-1].endswith(" " + UPLOAD_FLAG),
                                "the seal writes the directory the next step uploads")
                self.assertEqual(uploads, [steps[position + 1]], "the one upload directly follows the seal")
                retention = {lim.CI_RETENTION_DAYS[kind] for kind in kinds.values()}
                self.assertEqual(steps[position + 1], {"name": workflow.CI_UPLOAD_STEP, "uses": UPLOAD, "with": {
                    "name": upload_name(name, job_id), "path": UPLOAD_PATH, "if-no-files-found": "error",
                    "include-hidden-files": "true", "compression-level": "6",
                    "retention-days": str(retention.pop())}})

    def test_upload_names_are_the_grammar_names_of_their_kinds(self) -> None:
        run_id, attempt, unit = 36042781699, 3, "1.20.1-fabric"
        for name in CI_CALLEES:
            for job_id, kinds in workflow.CI_JOB_ARTIFACTS.get(name, {}).items():
                written = upload_name(name, job_id)
                for mode, kind in kinds.items():
                    expected = grammar.ci_artifact_name(
                        kind, run_id, attempt,
                        CI_PRODUCER[name] if kind == "tested" else unit if is_matrix_job(name, job_id) else None)
                    with self.subTest(callee=name, job=job_id, mode=mode):
                        if len(kinds) == 1:
                            rendered = (written.replace(RUN_EXPRESSION, str(run_id))
                                        .replace(ATTEMPT_EXPRESSION, str(attempt)).replace("${{ matrix.id }}", unit))
                        else:
                            templates = re.findall(r"'(mb-ci-[^']*)'", written)
                            self.assertEqual(len(templates), 2)
                            rendered = templates[0 if mode == "reuse" else 1].format(run_id, attempt)
                        self.assertEqual(rendered, expected)

    def test_the_prologues_inline_exactly_the_digest_tool_function(self) -> None:
        block = digest_block(SHELL.read_text(encoding="utf-8"))
        for name, job_id, job in iter_ci_jobs():
            with self.subTest(callee=name, job=job_id):
                self.assertEqual(digest_block(step(job["steps"], PROLOGUE[3])["run"]), block)

    def test_the_digest_tool_maintains_the_literal_of_every_callee(self) -> None:
        require_tools("git")
        tracked = [*workflow.CALLEE_WORKFLOWS.values(), *(workflow.CI_CALLEE_WORKFLOWS[name] for name in CI_CALLEES)]
        self.assertEqual(list(update_tree_digest.CALLEES), tracked)
        carrying = sorted(path.relative_to(ROOT).as_posix() for path in WORKFLOWS.iterdir()
                          if "MB_KIT_TREE_DIGEST" in path.read_text(encoding="utf-8"))
        self.assertEqual(carrying, sorted(tracked), "a literal the tool does not rewrite goes stale silently")
        digest = update_tree_digest.tree_digest(ROOT)
        for name in CI_CALLEES:
            self.assertEqual(ci_callee(name)["env"]["MB_KIT_TREE_DIGEST"], digest,
                             "run python3 tools/update_tree_digest.py --write")


class CiShellSyntaxTests(unittest.TestCase):
    def test_every_run_body_parses_and_passes_shellcheck(self) -> None:
        require_tools("bash", "shellcheck")
        for name, job_id, job in iter_ci_jobs():
            for item in job["steps"]:
                if "run" not in item:
                    continue
                with self.subTest(callee=name, job=job_id, step=item["name"]):
                    parsed = subprocess.run(["bash", "-n"], input=item["run"], capture_output=True, text=True,
                                            timeout=60)
                    self.assertEqual(parsed.returncode, 0, parsed.stderr)
                    # SC2154 is excluded as in the Pages tests: env: and the runner assign those variables.
                    checked = subprocess.run(["shellcheck", "-s", "bash", "-e", "SC2154", "-"], input=item["run"],
                                             capture_output=True, text=True, timeout=60)
                    self.assertEqual(checked.returncode, 0, checked.stdout)


class CiActionlintTests(unittest.TestCase):
    def test_actionlint_is_clean(self) -> None:
        require_tools("actionlint", "shellcheck")
        actionlint = shutil.which("actionlint")
        assert actionlint is not None
        with tempfile.TemporaryDirectory(prefix="ci actionlint ") as temporary:
            workflows = Path(temporary) / ".github/workflows"
            workflows.mkdir(parents=True)
            for name in CI_CALLEES:
                shutil.copyfile(CI_CALLEE_PATHS[name], workflows / CI_CALLEE_PATHS[name].name)
            files = sorted(str(path.relative_to(temporary)) for path in workflows.iterdir())
            result = subprocess.run([actionlint, "-no-color", *files], cwd=temporary, capture_output=True, text=True,
                                    timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CiCommandLineTests(unittest.TestCase):
    """Every step of every Build/E2E callee, executed: what reaches the kit and with which credentials."""

    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="ci steps ")
        self.addCleanup(temporary.cleanup)
        self.runner = CiStepRunner(Path(temporary.name))

    def issued(self) -> Iterator[tuple[str, list[str], dict[str, str | None]]]:
        """``(label, command line, environment)`` of every kit command of every step."""

        for name, job_id, job in iter_ci_jobs():
            for item in job["steps"]:
                if "run" not in item:
                    continue
                outcome = self.runner.run(item, **CI_ENVIRONMENT[name])
                label = f"{name} {job_id} / {item['name']}"
                self.assertEqual(outcome.result.returncode, 0, f"{label}: {outcome.result.stderr}")
                self.assertEqual([command[:2] for command in outcome.commands],
                                 [["ci", verb] for verb in ci_verbs(item["run"])], label)
                for command, environment in zip(outcome.commands, outcome.environments):
                    yield label, command, environment

    def test_every_issued_command_line_parses_with_the_real_ci_parser(self) -> None:
        registered = registered_ci_verbs()
        self.assertIn("subject", registered)
        self.assertEqual(sorted(PENDING_VERBS & registered), [],
                         "these verbs are registered now: remove them from PENDING_VERBS")
        issued = set()
        for label, command, _ in self.issued():
            verb = command[1]
            issued.add(verb)
            with self.subTest(step=label):
                self.assertIn(verb, registered | PENDING_VERBS, "a step issues a ci verb nobody registers")
                if verb in registered:
                    try:
                        parse_kit_argv(command)
                    except MbError as error:
                        self.fail(f"the kit's parser refuses {command}: {error}")
        self.assertEqual(sorted(PENDING_VERBS - issued), [], "PENDING_VERBS names a verb no step issues")

    def test_every_kit_command_is_isolated_and_holds_only_its_own_step_token(self) -> None:
        source = str(self.runner.workspace / "kit/src")
        for label, command, environment in self.issued():
            token = SAMPLES["GH_TOKEN"] if command[1] in API_VERBS else None
            with self.subTest(step=label):
                self.assertEqual(environment, {**dict.fromkeys(AMBIENT), "GH_TOKEN": token, "PYTHONPATH": source,
                                               **ISOLATION})


if __name__ == "__main__":
    unittest.main()
