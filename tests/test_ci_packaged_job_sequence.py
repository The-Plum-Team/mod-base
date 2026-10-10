"""The ``ci`` steps of one packaged job, run in the order and with the values its workflow gives them.

``tests/test_workflow_packaged_e2e.py`` pins the command lines of ``packaged-e2e.yml`` against a
stub and ``tests/test_ci_commands_packaged.py`` runs each verb alone in a state of its own. Here
the real verbs meet the way a job composes them: one private state directory per job, the outputs
of one step as the flags of the next, and the names the callee workflows read. Only the GitHub API
is faked.

:class:`PackagedRunTests` runs a whole run of the packaged caller that way, job by job, with the
command lines taken from ``packaged-e2e.yml`` itself (:class:`PackagedRun`): what the ``input`` job
selected must reach the ``lane``, ``aggregate`` and ``gate`` jobs, and what each of them is given
must be what its command accepts.
"""

from __future__ import annotations

import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, commands_build, exports, identity, planning, selection
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.records import validate_source_selection
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.workflow import CI_CALLER_WORKFLOWS, CI_JOB_VERBS
from tests import ci_mod_harness as h
from tests.ci_attempt import (ATTEMPT, RUN, Attempt, expand, lane_archive, record_zip, runtime_input_sha256, sealed,
                              validation, zip_tree)
from tests.helpers import ci_graph_jobs
from tests.test_ci_commands_packaged import CommandTestCase, JobWorld, decoded, run_ci
from tests.test_ci_gate import AGGREGATE, LISTINGS, PACKAGED_GATE, GateCase, seed_build
from tests.test_ci_packaged_selection import rebuild
from tests.test_ci_transport import build_archive
from tests.test_tree_digest_literal import GIT_STUB
from tests.test_workflow_ci_policy import (ALWAYS_VERBS, CI_INPUTS, CI_VERB_OUTPUTS, JDKS, ci_callee,
                                           registered_ci_verbs)
from tests.test_workflow_policy import PROLOGUE, ShellHarness, outputs, parse_kit_argv, require_tools

NAME = "packaged-e2e"
#: Callee workflow -> the job whose ``select`` step runs ``ci select-build`` and hands its outputs on.
SELECTING_JOBS = {NAME: "input", "select-build": "select"}
_STEP_OUTPUT = re.compile(r"steps\.select\.outputs\.([A-Za-z0-9_-]+)")
#: The verbs of a packaged job that :class:`PackagedRun` runs for real, in-process, against the
#: fake API. ``ci subject`` and ``ci plan`` leave their state and outputs through the fixture (their
#: own modules run them), and the worker verbs need the accounts of a hosted runner
#: (``tests/ci_linux_worker.py``).
REAL_VERBS = frozenset({"select-build", "fetch-build", "aggregate", "seal-gate"})
#: How every command line of a job names the protected mod checkout of its workspace.
CHECKOUTS = ["--repo", "mod", "--config", "mod/site/mod-base.json"]
NOT_REUSE = "inputs.mode != 'reuse'"
INPUT = "Shared Packaged E2E / Authenticate exact Build"
#: Packaged caller mode -> what its caller passes to the callee beside the pin: the pull request,
#: or for a standalone run the run its select job found or ``same-run`` after its rebuild job.
ROUTES = {"pull-request": {"pr-number": "7"}, "selected": {"mode": "full", "build-run-id": str(RUN["build"])},
          "rebuilt": {"mode": "full", "build-run-id": "same-run"}}
#: The one form a step's ``env:`` value has, and the one form a job output has.
_REFERENCE = re.compile(r"^\$\{\{ ([a-z]+(?:\.[A-Za-z0-9_-]+)+) \}\}$")
_JOB_OUTPUT = re.compile(r"^\$\{\{ steps\.([a-z]+)\.outputs\.([a-z0-9_]+) \}\}$")
_COMPARISON = re.compile(r"([a-z_]+(?:\.[a-z0-9_-]+)*) (==|!=) '([^']*)'")


@dataclass(frozen=True)
class Job:
    """One executed job: the directory of its runner and the outputs its workflow declares."""

    root: Path
    outputs: dict[str, str]

    @property
    def temp(self) -> Path:
        return self.root / "temp"

    @property
    def sealed_build(self) -> Path:
        return self.root / "worker" / "sealed-build"


class PackagedRun:
    """A run of the packaged caller on the fake GitHub of ``attempt``, executed job by job the way
    ``packaged-e2e.yml`` composes it.

    Every step after the prologue that has a ``run:`` body is executed by bash with a recording
    ``python3``, in a workspace and a ``$RUNNER_TEMP`` that last for the job. So what the shell
    checks and writes is real, and the command line it issues is the one the workflow gives the
    kit. A ``${{ }}`` reference in a step's ``env:`` is answered by the call inputs, the matrix unit
    and the outputs earlier jobs really produced; one that nothing answers fails. The verbs of
    :data:`REAL_VERBS` then run through the real entry point with that command line (only the two
    workspace-relative checkout paths become the fixture mod). ``ci subject`` and ``ci plan`` leave
    the state and the outputs their commands leave, and a worker verb only has to parse.
    """

    def __init__(self, test: unittest.TestCase, attempt: Attempt, inputs: dict[str, str]) -> None:
        self.test, self.attempt = test, attempt
        self.inputs = {**dict.fromkeys(CI_INPUTS[NAME], ""), "kit-sha": h.KIT_SHA, **inputs}
        self.jobs = ci_callee(NAME)["jobs"]
        self.shell = ShellHarness(attempt.directory / "shell", stubs=("git", "python3"))
        self.registered = registered_ci_verbs()
        #: ``(job, verb, API requests)`` of every real command, in the order they ran.
        self.requests: list[tuple[str, str, int]] = []

    def job(self, job_id: str, *, needs: dict[str, Job] | None = None, unit: str | None = None) -> Job:
        """Run the job ``job_id`` (for the matrix unit ``unit``) after the jobs it ``needs``."""

        root = self.attempt.directory / (job_id if unit is None else f"{job_id}--{unit}")
        for name in ("workspace", "temp", "worker"):
            (root / name).mkdir(parents=True)
        context = {"github.token": "job-token", "github.event_name": self.attempt.record["event"],
                   **{f"inputs.{name}": value for name, value in self.inputs.items()},
                   **{f"needs.{name}.outputs.{key}": value for name, needed in (needs or {}).items()
                      for key, value in needed.outputs.items()}}
        if unit is not None:
            context["matrix.id"] = unit
        written: dict[str, dict[str, str]] = {}
        for position, item in enumerate(self.jobs[job_id]["steps"][len(PROLOGUE):]):
            if "run" not in item or not self._runs(item, context):
                continue  # An action this module does not run writes no output: GitHub answers "".
            label = f"{root.name} / {item['name']}"
            output = root / f"output-{position}"
            environment = {"GITHUB_WORKSPACE": str(root / "workspace"), "RUNNER_TEMP": str(root / "temp"),
                           "GITHUB_OUTPUT": str(output), "STUB_GIT_SCRIPT": GIT_STUB,
                           "STUB_CANDIDATE_HEAD": self.attempt.record["subject"]["tested_sha"], **JDKS,
                           **{name: self._answer(value, context, label) for name, value in item.get("env", {}).items()}}
            result = self.shell.run(item["run"], environment, cwd=root / "workspace")
            self.test.assertEqual(result.returncode, 0, f"{label}: {result.stderr}")
            for record in self.shell.records():
                if record["tool"] == "python3" and record["argv"][:3] == ["-P", "-m", "mod_base"]:
                    self._kit(label, root, record["argv"][3:])
            if "id" in item:
                written[item["id"]] = outputs(output)
                context.update({f"steps.{item['id']}.outputs.{key}": value for key, value in written[item["id"]].items()})
        declared = self.jobs[job_id].get("outputs", {})
        return Job(root, {name: self._output(value, written, f"{job_id}.{name}") for name, value in declared.items()})

    @staticmethod
    def _runs(item: dict[str, Any], context: dict[str, str]) -> bool:
        condition = item.get("if")
        if condition is None or condition in ALWAYS_VERBS.values():
            return True
        if condition == NOT_REUSE:
            return context["inputs.mode"] != "reuse"
        # The seed steps compare step outputs and the event; an output no step wrote is "".
        decided = True
        for comparison in condition.split(" && "):
            match = _COMPARISON.fullmatch(comparison)
            if match is None or not match[1].startswith(("steps.", "github.event_name")):
                raise AssertionError(f"step {item['name']!r}: this module cannot decide `{condition}`")
            decided &= (context.get(match[1], "") == match[3]) == (match[2] == "==")
        return decided

    @staticmethod
    def _answer(value: str, context: dict[str, str], label: str) -> str:
        reference = _REFERENCE.fullmatch(value)
        if reference is not None and reference[1].startswith("steps."):
            return context.get(reference[1], "")
        if reference is None or reference[1] not in context:
            raise AssertionError(f"{label}: nothing of this run answers {value!r}")
        return context[reference[1]]

    @staticmethod
    def _output(value: str, written: dict[str, dict[str, str]], label: str) -> str:
        reference = _JOB_OUTPUT.fullmatch(value)
        if reference is None:
            raise AssertionError(f"{label}: this module cannot evaluate {value!r}")
        # An output that no step wrote is empty, as it is on a runner.
        return written.get(reference[1], {}).get(reference[2], "")

    def _kit(self, label: str, root: Path, argv: list[str]) -> None:
        """One kit command line a step issued."""

        verb, attempt = argv[1], self.attempt
        self.test.assertEqual(argv[2:6], CHECKOUTS, label)
        if verb in REAL_VERBS:
            before = attempt.api.request_count
            code, stderr = self._command(root, [*argv[:2], "--repo", str(h.MOD), "--config",
                                                str(h.MOD / "site" / "mod-base.json"), *argv[6:]])
            self.test.assertEqual((code, stderr), (0, ""), f"{label}: ci {' '.join(argv[1:])}")
            self.requests.append((root.name, verb, attempt.api.request_count - before))
        elif verb in self.registered:
            arguments = parse_kit_argv(argv)
            if verb == "subject":
                subject = attempt.record["subject"]
                self.test.assertEqual((arguments.producer, arguments.pr or 0), ("packaged", subject["pr_number"]), label)
                identity.write_subject(arguments.state, attempt.record)
                self._write(arguments.github_output, verb, {"tested_sha": subject["tested_sha"],
                                                            "pr_number": subject["pr_number"] or ""})
            elif verb == "plan":
                self.test.assertIn(arguments.expect_sha256, (None, attempt.plan["plan_sha256"]), label)
                identity.write_state_record(arguments.state, grammar.CI_PLAN_NAME, canonical_json(attempt.plan))
                self._write(arguments.github_output, verb, planning.plan_outputs(attempt.plan))

    def _write(self, path: Path, verb: str, values: dict[str, Any]) -> None:
        self.test.assertEqual(set(values), CI_VERB_OUTPUTS[verb], f"what `ci {verb}` writes")
        cli.write_github_output(path, values)

    def _command(self, root: Path, argv: list[str]) -> tuple[int, str]:
        """Run one command line through the real entry point as a step of this run: its exit code
        and stderr. The fixed worker root is the job's own."""

        def client(environ: Any, *, writable: bool = False, max_requests: int | None = None) -> Any:
            self.test.assertIs(writable, False)
            self.test.assertIsNotNone(max_requests)
            return self.attempt.api

        sealed_build, stderr = root / "worker" / "sealed-build", io.StringIO()
        with mock.patch.object(cli, "environ", return_value=self.attempt.environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                mock.patch.object(exports, "BUILD_VALIDATION_ROOT", sealed_build), \
                mock.patch.object(commands_build, "BUILD_VALIDATION_ROOT", sealed_build), \
                redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stderr.getvalue()


class PackagedJobSequenceTests(CommandTestCase):
    def fresh(self) -> None:
        super().fresh()
        (self.directory / "worker").mkdir()
        self.sealed = self.directory / "worker" / "sealed-build"

    def written(self) -> dict[str, str]:
        """The outputs the last ``ci select-build`` wrote to its ``$GITHUB_OUTPUT`` file."""

        return dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines())

    def fetch(self, world: JobWorld, state: Path) -> tuple[int, str]:
        """``ci fetch-build`` with the record file of this job (``self.record``)."""

        with mock.patch.object(exports, "BUILD_VALIDATION_ROOT", self.sealed):
            code, stderr, stdout = run_ci(self, world, state, "fetch-build", "--selection", str(self.record))
        self.assertEqual(stdout, b"")
        return code, stderr

    def test_select_build_writes_every_output_its_callee_workflows_read(self) -> None:
        read = {callee: {name for value in ci_callee(callee)["jobs"][job]["outputs"].values()
                         for name in _STEP_OUTPUT.findall(value)} for callee, job in SELECTING_JOBS.items()}
        # select-build.yml returns the run of the selected Build to its caller; the packaged input
        # job hands the whole record to the later jobs of its run.
        self.assertEqual(read, {NAME: {"selection"}, "select-build": {"found", "run_id"}})
        world = JobWorld(self.directory).build()
        self.assertEqual(self.select(world, world.state("input")), (0, ""))
        self.assertLessEqual(read[NAME], set(self.written()))
        self.assertEqual(self.written()["run_id"], str(world.bundle["producer"]["run_id"]))
        self.assertEqual(self.written()["selection"].encode("utf-8") + b"\n", self.record.read_bytes())
        # Nothing to select: select-build.yml still reads both of its outputs, one of them empty.
        self.fresh()
        world = JobWorld(self.directory, push=True)
        self.assertEqual(self.select(world, world.state("select")), (0, ""))
        self.assertLessEqual(read["select-build"], set(self.written()))
        self.assertEqual((self.written()["found"], self.written()["run_id"]), ("false", ""))

    def handed(self, world: JobWorld, *argv: str) -> dict[str, Any]:
        """Run ``ci select-build`` as the ``input`` job does, in a state of its own, and write the
        record it hands on (its ``selection`` output) where a later job receives it: that one
        line in a file of the job (``self.record``). Returns the record."""

        self.assertEqual(self.select(world, world.state("input"), *argv), (0, ""))
        line = self.written()["selection"].encode("utf-8") + b"\n"
        self.fresh()
        self.record.write_bytes(line)
        return decoded(line)

    def test_a_later_job_of_a_pull_request_is_handed_the_build_its_input_job_selected(self) -> None:
        world = JobWorld(self.directory).build()
        record = self.handed(world)
        self.assertEqual(record["build"], world.bundle)
        self.assertEqual(self.fetch(world, world.state("lane")), (0, ""))
        self.assertEqual(verify_build_export(self.sealed, plan=world.plan), world.envelope)
        # Selecting again stays what it was: a pull request names no run, whichever job asks.
        code, stderr = self.select(world, world.state("again"), "--build-run-id",
                                   str(record["build"]["producer"]["run_id"]))
        self.assertEqual(code, 2)
        self.assertIn("a pull request waits for its own Build run and names none", stderr)

    def test_a_later_job_of_a_rebuilding_run_is_handed_the_build_that_run_built(self) -> None:
        world = rebuild(JobWorld(self.directory, push=True))
        record = self.handed(world, "--build-run-id", "same-run")
        # The Build is the packaged run's own: a run id of it is nothing the Build caller knows,
        # and the record says whose it is.
        self.assertEqual((record["build"]["producer"]["run_id"], record["build"]["producer"]["workflow_path"]),
                         (43, CI_CALLER_WORKFLOWS["packaged"]))
        self.assertEqual(self.fetch(world, world.state("lane")), (0, ""))
        self.assertEqual(verify_build_export(self.sealed, plan=world.plan), world.envelope)

    def test_a_lane_job_fetches_into_a_state_no_selection_was_written_to(self) -> None:
        # `ci select-build` and `ci fetch-build` both keep the record as `ci-selection.json` of
        # their state, and neither replaces a record. Only the input job selects, so a lane job
        # fetches into a state that holds none, and leaves it there for its worker steps.
        for job_id, verbs in CI_JOB_VERBS[NAME].items():
            self.assertEqual("select-build" in verbs, job_id == "input", job_id)
        world = JobWorld(self.directory, push=True).build()
        self.handed(world)
        state = world.state("lane")
        self.assertFalse((state / selection.SELECTION_NAME).exists())
        self.assertEqual(self.fetch(world, state), (0, ""))
        self.assertEqual(verify_build_export(self.sealed, plan=world.plan), world.envelope)
        self.assertEqual((state / selection.SELECTION_NAME).read_bytes(), self.record.read_bytes())


class PackagedRunTests(GateCase):
    """A whole run of the packaged caller, with every command line taken from ``packaged-e2e.yml``."""

    def setUp(self) -> None:
        super().setUp()
        require_tools("bash")

    def packaged_run(self, mode: str, **options: Any) -> PackagedRun:
        """A run of the packaged caller in ``mode`` whose ``input`` job is about to select, and the
        complete Build it will consume as an assembling job uploads one (the export and, beside
        its envelope, the validation record of ``verify_build``): the Build of the finished run of
        the Build caller, or the one this very run has just built."""

        attempt = self.attempt(listing=LISTINGS[mode], caller="packaged", push=mode != "pull-request", **options)
        attempt.mode = mode
        attempt.sealing(INPUT)
        if mode == "rebuilt":
            seed_build(attempt, mode)
            attempt.publish("tested", "build", record_zip(grammar.CI_GATE_NAME, b"{}\n"), artifact_id=200)
            attempt.owning = attempt.descriptor(mode, 100)
            attempt.envelope = build_archive(attempt.plan, attempt.producer(mode))[1]
        else:
            producer = selection.producer_record(attempt.plan, caller="build", run_id=RUN["build"],
                                                 run_attempt=ATTEMPT, event=attempt.record["event"],
                                                 graph_sha256=run_graph("build", "full").sha256(attempt.plan))
            data, attempt.envelope = build_archive(attempt.plan, producer)
            record = validation(attempt.plan, hook="verify_build", unit_id=None, run_id=RUN["build"],
                                input_sha256=canonical_sha256(attempt.envelope))
            attempt.owning = attempt.add_build_run(sealed(data, *record))
        return PackagedRun(self, attempt, ROUTES[mode])

    @staticmethod
    def upload_lane(attempt: Attempt, job: Job, lane_id: str, artifact_id: int) -> None:
        """The artifact a lane job uploads: its results, validated against the Build the job's own
        state names (``ci-selection.json``, which ``ci fetch-build`` leaves for the worker steps)."""

        chosen = decoded(identity.read_state_record(job.temp / "mb-state", selection.SELECTION_NAME,
                                                    max_bytes=limits.MAX_CI_RECORD_BYTES))
        data, envelope = lane_archive(attempt.plan, attempt.producer(attempt.mode), chosen["build"], lane_id)
        record = validation(attempt.plan, hook="verify_runtime", unit_id=lane_id, run_id=attempt.run_id,
                            input_sha256=runtime_input_sha256(attempt.plan, chosen["envelope_sha256"], envelope))
        attempt.publish("runtime", lane_id, sealed(data, *record), artifact_id=artifact_id)

    def run_every_job(self, mode: str, **options: Any) -> PackagedRun:
        """The four jobs of one run in ``mode``, each on a runner of its own; what a job uploads
        becomes an artifact of the run before the next job starts."""

        run = self.packaged_run(mode, **options)
        attempt = run.attempt
        needs = {"input": run.job("input")}
        # What crosses the jobs is the record of the input job: one line, valid for this plan.
        record = validate_source_selection(decoded(needs["input"].outputs["selection"].encode("utf-8") + b"\n"),
                                           plan=attempt.plan)
        self.assertEqual((record["build"], record["envelope_sha256"]),
                         (attempt.owning, canonical_sha256(attempt.envelope)))
        attempt.jobs = expand(ci_graph_jobs(LISTINGS[mode]), attempt.plan)
        attempt.seed_jobs()
        lanes = json.loads(needs["input"].outputs["lanes"])
        self.assertEqual(lanes, [lane["id"] for lane in attempt.plan["lanes"]])
        for index, lane in enumerate(lanes):
            job = run.job("lane", needs=needs, unit=lane)
            self.assertEqual(verify_build_export(job.sealed_build, plan=attempt.plan), attempt.envelope)
            self.assertEqual((job.temp / "mb-state" / "build-selection.json").read_bytes(), canonical_json(record))
            self.upload_lane(attempt, job, lane, 300 + index)
        attempt.sealing(AGGREGATE)
        results = run.job("aggregate", needs=needs).temp / "mb-upload"
        index = json.loads((results / grammar.CI_RESULTS_NAME).read_bytes())
        self.assertEqual((index["owning_build"], index["build_envelope_sha256"]),
                         (record["build"], record["envelope_sha256"]))
        attempt.jobs = expand(ci_graph_jobs(LISTINGS[mode]), attempt.plan)
        attempt.sealing(PACKAGED_GATE)
        attempt.publish("results", None, zip_tree(results), artifact_id=400)
        receipt = (run.job("gate", needs=needs).temp / "mb-upload" / grammar.CI_GATE_NAME).read_bytes()
        document = self.assert_readers_accept(attempt, "packaged", mode, receipt, artifact_id=201)
        self.assertEqual(document["owning_build"], attempt.owning)
        self.assertEqual([entry["unit_id"] for entry in document["native_receipts"]], lanes)
        return run

    def test_every_job_of_a_pull_request_run_works_on_the_build_its_input_job_selected(self) -> None:
        run = self.run_every_job("pull-request", lanes=2)
        self.assertEqual([verb for _, verb, _ in run.requests],
                         ["select-build", "fetch-build", "fetch-build", "aggregate", "seal-gate"])

    def test_every_job_of_a_standalone_run_works_on_the_build_its_input_job_selected(self) -> None:
        for mode in ("selected", "rebuilt"):
            with self.subTest(mode=mode):
                run = self.run_every_job(mode)
                self.assertEqual([verb for _, verb, _ in run.requests],
                                 ["select-build", "fetch-build", "aggregate", "seal-gate"])


if __name__ == "__main__":
    unittest.main()
