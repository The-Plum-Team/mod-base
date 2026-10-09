"""Textual policy of the Build/E2E callee workflows (docs/BUILD-E2E-DESIGN.md, "Build controller prologue").

The Pages callees keep their registry (``workflow.CALLEE_WORKFLOWS``) and their tests
(``tests/test_workflow_policy.py``). This module applies the same rules to the second registry,
``workflow.CI_CALLEE_WORKFLOWS``, through that registry's own tables: ``CI_CALLEE_JOBS`` (job ids
and names), ``CI_JOB_VERBS`` (the ``ci`` verb of every step, in order), ``CI_JOB_ARTIFACTS`` (the
sealing jobs and what they upload) and ``CI_JOB_PERMISSIONS``. A callee is policed from the moment
its file exists: a workflow file without rows in those tables fails, and so do rows without a file.
``CiOutputTests`` holds the names that cross steps and jobs: an output a workflow reads must be one
the command of that step writes, and a job output must be declared where it is read.

The module also holds what the per-workflow modules (``tests/test_workflow_build.py``,
``tests/test_workflow_select_build.py``, ``tests/test_workflow_packaged_e2e.py`` and
``tests/test_workflow_gate_status.py``) import (functions and tables only, so no test runs
twice): the loader, the closed tables below and
:class:`CiStepRunner`, which executes a step's ``run:`` body under the Pages tests' ``ShellHarness``
and returns the ``ci`` command lines it issued.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import cli, workflow
from mod_base.adapter import protocol
from mod_base.build_ci import adapter as build_adapter
from mod_base.build_ci import graph, planning
from mod_base.build_ci.config import validate_build_config
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_config, ci_plan, ci_selection
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
             "packaged-e2e": ("kit-sha", "pr-number", "mode", "build-run-id"),
             "gate-status": ("kit-sha", "pr-number")}
#: What each callee returns to its caller, in order: output -> the job that has an output of that name.
CI_OUTPUTS = {"build": {}, "select-build": {"mode": "select", "found": "select", "build-run-id": "select"},
              "packaged-e2e": {}, "gate-status": {"intents": "evaluate"}}
#: The producer each callee's jobs authenticate as (``ci subject --producer``, ``ci seal-gate --gate``):
#: one of the managed callers its binding step admits (``workflow.CI_CALLEE_CALLERS``).
CI_PRODUCER = {"build": "build", "select-build": "packaged", "packaged-e2e": "packaged", "gate-status": "status"}
#: Where a callee reads its mode and the tested commit from (the callee's own spelling): the callees
#: whose gate seals one of two records, and the callees with a job that stages candidate code.
MODE_EXPRESSION = {"build": "needs.plan.outputs.mode", "packaged-e2e": "inputs.mode"}
CANDIDATE_REF = {"build": "${{ needs.plan.outputs.tested-sha }}",
                 "packaged-e2e": "${{ needs.input.outputs.tested-sha }}"}
#: Where the environment of a callee's steps differs from a Build step of pull request 17: the
#: managed caller that reaches the callee and, for the one no pull request reaches, the event.
PACKAGED_CALLER_REF = "example/mod/" + workflow.CI_CALLER_WORKFLOWS["packaged"] + "@refs/heads/master"
STATUS_CALLER_REF = "example/mod/" + workflow.CI_CALLER_WORKFLOWS["status"] + "@refs/heads/master"
CI_ENVIRONMENT = {"build": {},
                  "select-build": {"GITHUB_EVENT_NAME": "push", "GITHUB_WORKFLOW_REF": PACKAGED_CALLER_REF},
                  "packaged-e2e": {"GITHUB_WORKFLOW_REF": PACKAGED_CALLER_REF},
                  "gate-status": {"GITHUB_WORKFLOW_REF": STATUS_CALLER_REF}}

#: The third checkout, present exactly in the jobs that stage and run candidate code.
CANDIDATE_CHECKOUT = "Check out the tested candidate"
FUTURE_KIT_CHECKOUT = "Check out the admitted candidate kit"
CANDIDATE_VERBS = frozenset({"worker-stage", "worker-run"})
#: The verbs that call the API: exactly the steps that run one hold the step-scoped token.
API_VERBS = frozenset({"subject", "plan", "reuse-admit", "assemble", "seal-gate", "select-build", "fetch-build",
                       "aggregate", "gate-status"})
#: The verbs that must also run after a failure or a cancellation, and the step each depends on.
#: A bare ``always()`` would run kit Python in a job whose prologue failed, that is from a kit tree
#: nobody verified; a step that follows the prologue can only have succeeded when the prologue did.
#: The sweep runs once the job authenticated its subject (the next step creates the accounts), the
#: candidate is locked once its account exists.
ALWAYS_VERBS = {"worker-seal": "${{ always() && steps.prepare.outcome == 'success' }}",
                "worker-finish": "${{ always() && steps.subject.outcome == 'success' }}"}
#: The step ids those conditions name, by the verb of the step that carries each.
GATING_IDS = {"subject": "subject", "worker-prepare": "prepare"}
#: The one job that issues a verb before it authenticates a subject, and that verb: the status
#: evaluation first asks, without a plan, whether every gate is already decided (a draft, a run
#: that has not finished or that failed). Such a step is unconditional; the job's rules for
#: ``subject`` first, ``worker-prepare`` next and no verb twice apply to the verbs after it.
SETTLE_VERBS = {("gate-status", "evaluate"): ("gate-status",)}
#: The verbs a job issues between its subject and its accounts, by callee and job: root work on the
#: image that must be done before any worker account exists. Only a lane runs a client, so only a
#: lane installs the protected config's system profile; the step is unconditional and holds no token.
PRE_ACCOUNT_VERBS = {("packaged-e2e", "lane"): ("system-profile",)}
#: Only when that first call could not settle does the status job authenticate, prepare, plan and
#: evaluate again; ``'false'`` is what the call wrote, never an output that is merely missing.
UNSETTLED = "steps.settle.outputs.settled == 'false'"
#: The step of a packaged job that writes the selection record of its ``input`` job to a file. It
#: runs no kit command and holds no token.
RECEIVE_STEP = "Receive the selected Build"
#: The only other condition a step may carry, by callee, job and verb (or, for a step that runs
#: no kit command, its name). Reuse is admitted for a push alone, and a job that also runs in
#: reuse mode has no Build then: its ``input`` job, which would have selected one, was skipped
#: like its ``lane`` and ``aggregate`` jobs. In the status job the rows are the steps after its
#: settling call (``SETTLE_VERBS``).
CONDITIONAL_STEPS = {
    ("build", "plan", "reuse-admit"): "github.event_name == 'push'",
    ("select-build", "select", "reuse-admit"): "github.event_name == 'push'",
    ("select-build", "select", "select-build"): "steps.reuse.outputs.mode != 'reuse'",
    ("packaged-e2e", "gate", RECEIVE_STEP): "inputs.mode != 'reuse'",
    ("gate-status", "evaluate", "subject"): UNSETTLED,
    ("gate-status", "evaluate", "worker-prepare"): UNSETTLED,
    ("gate-status", "evaluate", "plan"): UNSETTLED,
    ("gate-status", "evaluate", "gate-status"): UNSETTLED,
    ("gate-status", "evaluate", CANDIDATE_CHECKOUT): UNSETTLED,
    ("build", "policy", FUTURE_KIT_CHECKOUT): "steps.plan.outputs.candidate_kit_sha != ''",
    ("build", "target", FUTURE_KIT_CHECKOUT): "steps.plan.outputs.candidate_kit_sha != ''",
    ("packaged-e2e", "lane", FUTURE_KIT_CHECKOUT): "steps.plan.outputs.candidate_kit_sha != ''",
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
PENDING_VERBS = frozenset()

#: The one fixed first line of every kit command of a Build/E2E job.
CI_COMMAND = re.compile(r"^  python3 -P -m mod_base ci ([a-z][a-z-]*) --repo mod --config mod/site/mod-base\.json "
                        r'--state "\$RUNNER_TEMP/mb-state"(?: \\)?$', re.MULTILINE)
#: What a step's ``env:`` may carry: a call input, an output of an earlier job, the matrix unit or
#: the step-scoped token. Never an event payload field and never a secret.
ENV_VALUE = re.compile(r"^\$\{\{ (?:inputs\.[a-z][a-z0-9-]*|needs\.[a-z][a-z-]*\.outputs\.[a-z][a-z0-9-]*|matrix\.id"
                       r"|github\.token) \}\}$")
#: What a job may return: one output of one of its own steps, a choice between two literals by it,
#: or the first written of two such outputs (the settling call's or the planned evaluation's).
JOB_OUTPUT = re.compile(r"^\$\{\{ steps\.[a-z]+\.outputs\.[a-z0-9_]+(?: == '[a-z]+' && '[a-z]+' \|\| '[a-z]+'"
                        r"| \|\| steps\.[a-z]+\.outputs\.[a-z0-9_]+)? \}\}$")
#: How an expression names an output of a step of its own job and of a job it needs, and how a
#: callee returns one to its caller.
STEP_OUTPUT = re.compile(r"\bsteps\.([a-z][a-z0-9_-]*)\.outputs\.([A-Za-z0-9_-]+)")
NEEDED_OUTPUT = re.compile(r"\bneeds\.([a-z][a-z0-9_-]*)\.outputs\.([A-Za-z0-9_-]+)")
RETURNED_OUTPUT = re.compile(r"\bjobs\.([a-z][a-z0-9_-]*)\.outputs\.([A-Za-z0-9_-]+)")
#: What each ``ci`` verb of a callee job writes to ``$GITHUB_OUTPUT``; a verb without a row writes
#: nothing. A workflow reads these by name and a name nobody wrote is silently the empty string,
#: so they are spelled out here and :func:`ci_verb_outputs` reads the same from the commands.
CI_VERB_OUTPUTS = {
    "subject": frozenset({"tested_sha", "pr_number"}),
    "plan": frozenset({"plan_sha256", "targets", "lanes", "candidate_kit_sha"}),
    "reuse-admit": frozenset({"mode", "reason"}),
    "select-build": frozenset({"found", "run_id", "selection"}),
    "gate-status": frozenset({"settled", "intents"}),
}
DOCUMENT_KEYS = {"name", "on", "permissions", "env", "jobs"}
JOB_KEYS = {"name", "needs", "if", "runs-on", "timeout-minutes", "permissions", "outputs", "strategy", "steps"}
RUN_STEP_KEYS = {"name", "id", "if", "shell", "env", "run"}
USES_STEP_KEYS = {"name", "uses", "with", "if"}

#: The one difference between the Pages binding step and the Build/E2E one: which workflow of the
#: canonical branch may call the job.
PAGES_GUARD = ('[[ "$GITHUB_WORKFLOW_REF" == "$GITHUB_REPOSITORY/' + workflow.PAGES_WORKFLOW_PATH
               + '@refs/heads/$canonical_branch" ]] ||\n'
               "  fail \"this job is not called by the canonical branch's pages.yml\"\n")
#: What the binding step says when none of the managed callers it admits is the calling workflow,
#: by those callers (``workflow.CI_CALLEE_CALLERS``).
CI_GUARD_FAILURES = {
    ("build", "packaged"): "this job is not called by a managed Build or packaged E2E caller of the canonical branch",
    ("status",): "this job is not called by the managed gate status caller of the canonical branch",
}


def ci_guard(name: str) -> str:
    """The lines of the binding step that admit the managed callers of the callee ``name``."""

    callers = workflow.CI_CALLEE_CALLERS[name]
    return ("[[ " + " ||\n   ".join(
        '"$GITHUB_WORKFLOW_REF" == "$GITHUB_REPOSITORY/' + workflow.CI_CALLER_WORKFLOWS[caller]
        + '@refs/heads/$canonical_branch"' for caller in callers) + " ]] ||\n"
        f'  fail "{CI_GUARD_FAILURES[callers]}"\n')


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


def ci_verb_outputs() -> dict[str, frozenset[str]]:
    """Registered ``ci`` verb of a callee job -> the output names its command writes, read from
    the command itself: the mapping of every ``cli.write_github_output`` call its handler makes,
    directly or through a function of its own module. A mapping whose names this reader cannot
    tell fails, so a command cannot start writing an output unseen."""

    def names(mapping: ast.expr, verb: str) -> set[str]:
        if isinstance(mapping, ast.Dict):
            keys = [key.value for key in mapping.keys if isinstance(key, ast.Constant) and type(key.value) is str]
            if len(keys) == len(mapping.keys):
                return set(keys)
        if isinstance(mapping, ast.Call) and ast.unparse(mapping.func) == "planning.plan_outputs":
            return set(planning.plan_outputs(ci_plan()))
        raise AssertionError(f"ci {verb}: cannot tell the output names of `{ast.unparse(mapping)}`")

    tabled = {verb for jobs in workflow.CI_JOB_VERBS.values() for verbs in jobs.values() for verb in verbs}
    registered = _verbs_action(_verbs_action(cli.build_parser("ci")).choices["ci"]).choices
    written = {}
    for verb in sorted(tabled & set(registered)):
        handler = registered[verb].get_default("handler")
        module = ast.parse(inspect.getsource(sys.modules[handler.__module__]))
        functions = {node.name: node for node in module.body if isinstance(node, ast.FunctionDef)}
        reached: set[str] = set()
        found: set[str] = set()
        pending = [handler.__name__]
        while pending:
            name = pending.pop()
            if name in reached:
                continue
            reached.add(name)
            for call in (node for node in ast.walk(functions[name]) if isinstance(node, ast.Call)):
                if isinstance(call.func, ast.Name) and call.func.id in functions:
                    pending.append(call.func.id)
                elif isinstance(call.func, ast.Attribute) and call.func.attr == "write_github_output":
                    found |= names(call.args[1], verb)
        written[verb] = frozenset(found)
    return written


def strings(value: Any) -> Iterator[str]:
    """Every string a parsed YAML value holds, at any depth."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


# -- Executing a step's shell ------------------------------------------------------------------------

#: One value for every name a step's ``env:`` may define; an unknown name fails the run. ``MODE``
#: and ``BUILD_RUN_ID`` are the packaged call inputs of a pull request (both empty); the record
#: of the Build the ``input`` job then selected reaches the later jobs as ``SELECTION``, the one
#: line ``ci select-build`` writes as its ``selection`` output.
SAMPLES = {"KIT_SHA": "b" * 40, "PR_NUMBER": "17", "GH_TOKEN": "step-token", "PLAN_SHA256": "c" * 64,
           "TESTED_SHA": "d" * 40, "TARGET": "target-a", "LANE": "lane-a", "MODE": "", "BUILD_RUN_ID": "",
           "SELECTION": canonical_json(ci_selection()).decode("utf-8").rstrip("\n")}
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
        # What `ci subject` left earlier in the job; the recording python3 creates nothing.
        (self.runner_temp / "mb-state").mkdir(mode=0o700)
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
                "lane": ("subject", "system-profile", "worker-prepare", "plan", "fetch-build", "worker-stage",
                         "worker-run", "worker-seal", "worker-validate", "worker-finish"),
                "aggregate": ("subject", "worker-prepare", "plan", "aggregate", "worker-finish"),
                "gate": ("subject", "worker-prepare", "plan", "seal-gate", "worker-finish"),
            },
            "gate-status": {
                "evaluate": ("gate-status", "subject", "worker-prepare", "plan", "gate-status", "worker-finish"),
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
            "packaged-e2e": {"input": read, "lane": read, "aggregate": read, "gate": read},
            "gate-status": {"evaluate": read}})

    def test_every_table_describes_exactly_the_jobs_of_its_callee(self) -> None:
        self.assertEqual(set(workflow.CI_JOB_PERMISSIONS), set(CI_CALLEES))
        self.assertLessEqual(set(workflow.CI_JOB_ARTIFACTS), set(CI_CALLEES))
        self.assertLessEqual(set(CI_CALLEES), set(workflow.CI_CALLEE_WORKFLOWS))
        for table in (CI_INPUTS, CI_OUTPUTS, CI_PRODUCER, CI_ENVIRONMENT, workflow.CI_CALLEE_CALLERS):
            self.assertEqual(set(table), set(CI_CALLEES))
        self.assertEqual(set(CI_GUARD_FAILURES), set(workflow.CI_CALLEE_CALLERS.values()))
        for (name, job_id), verbs in SETTLE_VERBS.items():
            self.assertEqual(workflow.CI_JOB_VERBS[name][job_id][:len(verbs)], verbs, "a settling call runs first")
            self.assertEqual(set(verbs) & (set(ALWAYS_VERBS) | set(GATING_IDS) | CANDIDATE_VERBS | SEAL_VERBS), set(),
                             "before a subject exists a job only reads the API")
            self.assertLessEqual(set(verbs), API_VERBS)
        # A mode is spelled where a gate seals one of two records, a tested commit where a job
        # stages candidate code; a callee without either has no row to go stale.
        self.assertEqual(set(MODE_EXPRESSION), {name for name, jobs in workflow.CI_JOB_ARTIFACTS.items()
                                                if any(len(kinds) > 1 for kinds in jobs.values())})
        self.assertEqual(set(CANDIDATE_REF), {name for name in CI_CALLEES if any(
            CANDIDATE_VERBS & set(verbs) for verbs in workflow.CI_JOB_VERBS[name].values())})
        for (name, job_id, verb), condition in CONDITIONAL_STEPS.items():
            named = [item["name"] for item in ci_callee(name)["jobs"][job_id]["steps"] if step_verb(item) is None]
            self.assertIn(verb, (*workflow.CI_JOB_VERBS[name][job_id], *named),
                          "a condition is tabled for a step that exists")
            self.assertNotIn(verb, ALWAYS_VERBS)
            self.assertNotRegex(condition, r"always\(\)|failure\(\)|cancelled\(\)|success\(\)")
        for name in CI_CALLEES:
            jobs = list(workflow.CI_CALLEE_JOBS[name])
            with self.subTest(callee=name):
                self.assertEqual(list(workflow.CI_JOB_VERBS[name]), jobs)
                self.assertEqual(list(workflow.CI_JOB_PERMISSIONS[name]), jobs)
                self.assertIn(CI_PRODUCER[name], workflow.CI_CALLEE_CALLERS[name])
                for job_id, verbs in workflow.CI_JOB_VERBS[name].items():
                    sealing = [verb for verb in verbs if verb in SEAL_VERBS]
                    self.assertEqual(len(sealing), int(job_id in workflow.CI_JOB_ARTIFACTS.get(name, {})), job_id)
                    # What follows a settling call is a job like every other: subject, prepare, ...
                    verbs = verbs[len(SETTLE_VERBS.get((name, job_id), ())):]
                    before = PRE_ACCOUNT_VERBS.get((name, job_id), ())
                    self.assertEqual(verbs[:2 + len(before)], ("subject", *before, "worker-prepare"), job_id)
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
        for name in workflow.CI_JOB_ARTIFACTS:  # only a producer's callee seals and uploads
            producer = CI_PRODUCER[name]
            call = next(call for call, called in workflow.CI_CALLS[producer].items() if called == name)
            for job_id, kinds in workflow.CI_JOB_ARTIFACTS.get(name, {}).items():
                fields = {"id": target} if is_matrix_job(name, job_id) else {}
                for kind in kinds.values():
                    unit = producer if kind == "tested" else fields.get("id")
                    with self.subTest(callee=name, job=job_id, kind=kind):
                        self.assertEqual(graph.upload_job_name(producer, kind, unit),
                                         workflow.ci_api_job_name(producer, call, job_id, **fields))


class CiOutputTests(unittest.TestCase):
    """The names that cross steps and jobs. GitHub answers an output nobody wrote, and a job
    output nobody declared, with the empty string: neither the runner nor ``actionlint`` objects."""

    def test_the_outputs_table_is_what_the_commands_write(self) -> None:
        written = ci_verb_outputs()
        tabled = {verb for jobs in workflow.CI_JOB_VERBS.values() for verbs in jobs.values() for verb in verbs}
        self.assertEqual(set(written), tabled - PENDING_VERBS)
        self.assertEqual({verb: names for verb, names in written.items() if names}, CI_VERB_OUTPUTS)

    def test_every_step_output_a_callee_reads_is_one_the_command_of_that_step_writes(self) -> None:
        read = set()
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            verbs = {item["id"]: step_verb(item) for item in steps if "id" in item}
            position = {item["id"]: index for index, item in enumerate(steps) if "id" in item}
            self.assertEqual(len(verbs), sum("id" in item for item in steps), f"{name}/{job_id}: a step id repeats")
            # A job output may read every step; a step reads the steps before it.
            places = [(f"output {output}", value, len(steps)) for output, value in job.get("outputs", {}).items()]
            places += [(item["name"], text, index) for index, item in enumerate(steps)
                       for key, value in item.items() if key != "run" for text in strings(value)]
            for place, text, limit in places:
                for step_id, output in STEP_OUTPUT.findall(text):
                    with self.subTest(callee=name, job=job_id, place=place, read=f"{step_id}.{output}"):
                        self.assertIn(step_id, verbs, "no step of this job has that id")
                        self.assertLess(position[step_id], limit, "only an earlier step has written")
                        self.assertIn(output, CI_VERB_OUTPUTS.get(verbs[step_id], frozenset()),
                                      f"`ci {verbs[step_id]}` writes no output of that name")
                        read.add((verbs[step_id], output))
        # Everything that leaves a job or decides a later step is among them.
        self.assertLessEqual({("subject", "tested_sha"), ("plan", "plan_sha256"), ("plan", "targets"),
                              ("plan", "lanes"), ("reuse-admit", "mode"), ("select-build", "found"),
                              ("select-build", "run_id"), ("select-build", "selection"),
                              ("gate-status", "settled"), ("gate-status", "intents")}, read)

    def test_every_job_output_is_declared_where_it_is_read_and_read_where_it_is_declared(self) -> None:
        for name in CI_CALLEES:
            document = ci_callee(name)
            jobs = document["jobs"]
            returned = set(RETURNED_OUTPUT.findall(" ".join(strings(document["on"]))))
            read = set()
            for job_id, job in jobs.items():
                needed = job.get("needs", [])
                needed = [needed] if isinstance(needed, str) else needed
                for text in strings(job):
                    for needed_job, output in NEEDED_OUTPUT.findall(text):
                        with self.subTest(callee=name, job=job_id, read=f"{needed_job}.{output}"):
                            self.assertIn(needed_job, needed, "a job reads only the jobs it needs")
                            self.assertIn(output, jobs[needed_job].get("outputs", {}))
                        read.add((needed_job, output))
            declared = {(job_id, output) for job_id, job in jobs.items() for output in job.get("outputs", {})}
            with self.subTest(callee=name):
                self.assertEqual(declared, read | returned, "a job output is declared exactly where one is read")


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

    def test_a_job_that_waits_for_a_build_outlives_the_wait(self) -> None:
        # A selection may poll for the whole bounded wait. A job that ends sooner would cancel a
        # selection that is still entitled to an answer, and the run would fail for no reason of its
        # own. No other job selects: a later job is handed the record and waits for nothing.
        waiting = [(name, job_id, job) for name, job_id, job in iter_ci_jobs()
                   if any(step_verb(item) == "select-build" for item in job["steps"])]
        self.assertEqual({(name, job_id) for name, job_id, _ in waiting},
                         {("select-build", "select"), ("packaged-e2e", "input")})
        for name, job_id, job in waiting:
            with self.subTest(callee=name, job=job_id):
                self.assertGreaterEqual(int(job["timeout-minutes"]) * 60, lim.CI_BUILD_WAIT_SECONDS + 600)

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
                self.assertEqual(checkouts, [PROLOGUE[1], PROLOGUE[2], CANDIDATE_CHECKOUT,
                                             *([FUTURE_KIT_CHECKOUT] if candidate else [])])
                for item in steps:
                    keys = USES_STEP_KEYS if "uses" in item else RUN_STEP_KEYS
                    self.assertLessEqual(set(item), keys, item["name"])
                    self.assertTrue(("uses" in item) != ("run" in item), item["name"])

    def test_prologue_steps_are_identical_wherever_they_repeat(self) -> None:
        # The binding step names the managed callers of its callee, so it repeats byte for byte
        # among the callees that admit the same callers; every other step repeats everywhere.
        shared: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for name in CI_CALLEES:
            validations = []
            for job in ci_callee(name)["jobs"].values():
                validations.append(step(job["steps"], PROLOGUE[0]))
                for step_name in PROLOGUE[1:]:
                    admitted = workflow.CI_CALLEE_CALLERS[name] if step_name == PROLOGUE[3] else ()
                    shared.setdefault((step_name, *admitted), []).append(step(job["steps"], step_name))
            self.assertTrue(all(item == validations[0] for item in validations),
                            f"{name}: one input validation for every job")
        self.assertEqual(len(shared), len(PROLOGUE) - 2 + len(set(workflow.CI_CALLEE_CALLERS.values())))
        for key, copies in shared.items():
            self.assertTrue(all(item == copies[0] for item in copies),
                            f"{key!r} is byte-identical in every Build/E2E callee job")

    def test_the_prologue_is_the_pages_prologue_bound_to_the_managed_callers_of_each_callee(self) -> None:
        pages = callee("finalize")["jobs"]["refresh"]["steps"]
        pages_bind = step(pages, PROLOGUE[3])
        self.assertEqual(pages_bind["run"].count(PAGES_GUARD), 1)
        self.assertEqual(ci_guard("build"), ci_guard("select-build"))
        self.assertEqual(ci_guard("build"), ci_guard("packaged-e2e"))
        self.assertEqual(ci_guard("gate-status").count("GITHUB_WORKFLOW_REF"), 1, "the status caller alone")
        self.assertIn(workflow.CI_CALLER_WORKFLOWS["status"], ci_guard("gate-status"))
        self.assertNotIn(workflow.CI_CALLER_WORKFLOWS["status"], ci_guard("build"))
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            with self.subTest(callee=name, job=job_id):
                for index in (1, 2, 4, 5):
                    self.assertEqual(step(steps, PROLOGUE[index]), step(pages, PROLOGUE[index]))
                self.assertEqual(step(steps, PROLOGUE[3]),
                                 {**pages_bind, "run": pages_bind["run"].replace(PAGES_GUARD, ci_guard(name))})
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
                    future = step(steps, FUTURE_KIT_CHECKOUT)
                    self.assertEqual(future, {"name": FUTURE_KIT_CHECKOUT,
                        "if": "steps.plan.outputs.candidate_kit_sha != ''", "uses": CHECKOUT, "with": {
                            "repository": "The-Plum-Team/mod-base", "ref": "${{ steps.plan.outputs.candidate_kit_sha }}",
                            "path": "candidate-kit", "persist-credentials": "false"}})
                    self.assertLess(next(i for i, item in enumerate(steps) if step_verb(item) == "plan"), steps.index(future))
                    self.assertLess(steps.index(future), next(i for i, item in enumerate(steps) if step_verb(item) == "worker-stage"))
                else:
                    expected = {"name": CANDIDATE_CHECKOUT, "uses": CHECKOUT, "with": {
                        "ref": "${{ steps.subject.outputs.tested_sha }}", "path": "candidate", "persist-credentials": "false"}}
                    if name == "gate-status":
                        expected["if"] = UNSETTLED
                    self.assertEqual(step(steps, CANDIDATE_CHECKOUT), expected)
                    self.assertLess(next(i for i, item in enumerate(steps) if step_verb(item) == "subject"),
                                    steps.index(step(steps, CANDIDATE_CHECKOUT)))
                self.assertIn("--pin-candidate candidate", next(item["run"] for item in steps if step_verb(item) == "plan"))

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
            settling = len(SETTLE_VERBS.get((name, job_id), ()))
            issued = 0
            for item in job["steps"]:
                verb = step_verb(item)
                issued += verb is not None
                with self.subTest(callee=name, job=job_id, step=item["name"]):
                    # A settling call decides whether the rest of the job runs: it has no condition.
                    expected = (None if verb is not None and issued <= settling
                                else ALWAYS_VERBS.get(verb, CONDITIONAL_STEPS.get((name, job_id,
                                                                                    verb or item["name"]))))
                    self.assertEqual(item.get("if"), expected)
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

    def test_root_work_on_the_image_runs_between_the_subject_and_the_accounts_of_the_lane_alone(self) -> None:
        # `ci system-profile` refuses once a worker account exists, and what it installs must be
        # there before `ci worker-prepare` fences the image and admits its tools. It reads the
        # protected config through the state `ci subject` wrote, and calls no API.
        placed = set()
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            verbs = [step_verb(item) for item in steps]
            before = PRE_ACCOUNT_VERBS.get((name, job_id), ())
            with self.subTest(callee=name, job=job_id):
                subject, prepare = verbs.index("subject"), verbs.index("worker-prepare")
                self.assertEqual(tuple(verb for verb in verbs[subject + 1:prepare] if verb), before,
                                 "no other kit command runs in between")
                for verb in before:
                    item = steps[verbs.index(verb)]
                    placed.add((name, job_id, verb))
                    self.assertEqual(set(item), {"name", "shell", "run"}, "unconditional, no id and no env")
                    self.assertEqual(item["run"].splitlines()[1], SCRUB)
                    self.assertEqual(ci_verbs(item["run"]), [verb])
                    self.assertTrue(item["run"].rstrip().endswith('--state "$RUNNER_TEMP/mb-state"'), "no flag")
        self.assertEqual(placed, {(name, job_id, verb) for (name, job_id), verbs in PRE_ACCOUNT_VERBS.items()
                                  for verb in verbs})
        self.assertEqual({verb for verbs in PRE_ACCOUNT_VERBS.values() for verb in verbs}
                         & (API_VERBS | set(ALWAYS_VERBS) | set(GATING_IDS) | CANDIDATE_VERBS | SEAL_VERBS), set())

    def test_a_subject_is_derived_exactly_where_the_candidate_is_held_and_then_proven_by_the_plan(self) -> None:
        # `ci subject --candidate` spends one request instead of four or five and does not observe
        # the default branch (`identity.derive_subject`). So only a job that holds the candidate
        # checkout uses it, and that job then reproduces the plan of the job of its run that
        # authenticated the subject in full and named the commit the candidate was checked out at.
        derived = set()
        for name, job_id, job in iter_ci_jobs():
            steps = job["steps"]
            names = [item["name"] for item in steps]
            subject = next(item for item in steps if step_verb(item) == "subject")
            words = subject["run"].splitlines()[-1].split()
            with self.subTest(callee=name, job=job_id):
                self.assertEqual("--candidate" in words, names.index(CANDIDATE_CHECKOUT) < names.index(subject["name"]))
                if "--candidate" not in words:
                    continue
                derived.add((name, job_id))
                self.assertEqual(words[words.index("--candidate") + 1], "candidate")
                self.assertLess(names.index(CANDIDATE_CHECKOUT), names.index(subject["name"]))
                plan = next(item for item in steps if step_verb(item) == "plan")
                self.assertLess(names.index(subject["name"]), names.index(plan["name"]))
                self.assertIn('--candidate candidate --expect-sha256 "$PLAN_SHA256"', plan["run"].splitlines()[-1])
                source = re.fullmatch(r"\$\{\{ needs\.([a-z]+)\.outputs\.plan-sha256 \}\}", plan["env"]["PLAN_SHA256"])
                self.assertIsNotNone(source, "the plan to reproduce comes from an earlier job of this run")
                first = ci_callee(name)["jobs"][source[1]]
                self.assertEqual(first["outputs"]["plan-sha256"], "${{ steps.plan.outputs.plan_sha256 }}")
                self.assertEqual(CANDIDATE_REF[name], "${{ needs." + source[1] + ".outputs.tested-sha }}")
                authenticated = next(item for item in first["steps"] if step_verb(item) == "subject")
                self.assertNotIn("--candidate", authenticated["run"], "that job authenticates in full")
        self.assertEqual(derived, {("build", "policy"), ("build", "target"), ("packaged-e2e", "lane")})


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


class CiConfiguredTimeoutTests(unittest.TestCase):
    """The generic config fixture admits hook budgets that need not fit a whole job.

    ``tests.helpers.ci_config`` grants policy 3600 seconds in a 60-minute job and target 7200
    in a 120-minute job, before planning, setup and verification. This is a schema/workflow
    observation, not a measurement of either mod's adapter.
    """

    def hooks(self, name: str, job_id: str, job: Mapping[str, Any]) -> list[str]:
        """The adapter hooks a job runs: the ones its steps name, and the planning hook of ``ci plan``."""

        named = set(re.findall(r"--hook ([a-z_]+)", "\n".join(item.get("run", "") for item in job["steps"])))
        if "plan" in workflow.CI_JOB_VERBS[name][job_id]:
            named.add("derive_plan")
        self.assertLessEqual(named, set(build_adapter.HOOKS), f"{name}/{job_id} names a hook the contract lacks")
        return sorted(named)

    # Decision: the six-hour schema cap bounds each hook, not the whole job. The job deadline
    # remains authoritative and fails closed; adapter timing and margin need K7/Q/B measurements.
    # Retain this expected failure as the documented distinction: docs/BUILD-PROTOCOL.md#failures.
    @unittest.expectedFailure
    def test_no_admitted_hook_timeout_is_as_long_as_the_job_that_runs_it(self) -> None:
        granted = []
        jobs = 0
        for name, job_id, job in iter_ci_jobs():
            seconds = int(job["timeout-minutes"]) * 60
            for hook in self.hooks(name, job_id, job):
                jobs += 1
                document = ci_config()
                document["timeouts"][build_adapter.HOOKS[hook].timeout] = seconds
                try:
                    validate_build_config(document)
                except MbError:
                    continue
                granted.append(f"{name}/{job_id}: {hook} may run for all {seconds} seconds of its job")
        self.assertGreater(jobs, 0)
        self.assertEqual(granted, [])


if __name__ == "__main__":
    unittest.main()
