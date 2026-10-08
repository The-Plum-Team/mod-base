"""The ``ci`` steps of one packaged job, run in the order and with the values its workflow gives them.

``tests/test_workflow_packaged_e2e.py`` pins the command lines of ``packaged-e2e.yml`` against a
stub and ``tests/test_ci_commands_packaged.py`` runs each verb alone in a state of its own. Here
the real verbs meet the way a job composes them: one private state directory per job, the outputs
of one step as the flags of the next, and the names the callee workflows read. Only the GitHub API
is faked.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from mod_base.build_ci import exports
from mod_base.build_ci.exports import verify_build_export
from tests.test_ci_commands_packaged import CommandTestCase, JobWorld, run_ci
from tests.test_ci_packaged_selection import rebuild
from tests.test_workflow_ci_policy import ci_callee

#: Callee workflow -> the job whose ``select`` step runs ``ci select-build`` and hands its outputs on.
SELECTING_JOBS = {"packaged-e2e": "input", "select-build": "select"}
_STEP_OUTPUT = re.compile(r"steps\.select\.outputs\.([A-Za-z0-9_-]+)")


class PackagedJobSequenceTests(CommandTestCase):
    def fresh(self) -> None:
        super().fresh()
        (self.directory / "worker").mkdir()
        self.sealed = self.directory / "worker" / "sealed-build"

    def written(self) -> dict[str, str]:
        """The outputs the last ``ci select-build`` wrote to its ``$GITHUB_OUTPUT`` file."""

        return dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines())

    def fetch(self, world: JobWorld, state: Path) -> tuple[int, str]:
        """``ci fetch-build`` with the record the job's own ``ci select-build`` has just written."""

        with mock.patch.object(exports, "BUILD_VALIDATION_ROOT", self.sealed):
            code, stderr, stdout = run_ci(self, world, state, "fetch-build", "--selection", str(self.record))
        self.assertEqual(stdout, b"")
        return code, stderr

    def test_select_build_writes_every_output_its_callee_workflows_read(self) -> None:
        read = {callee: {name for value in ci_callee(callee)["jobs"][job]["outputs"].values()
                         for name in _STEP_OUTPUT.findall(value)} for callee, job in SELECTING_JOBS.items()}
        # Both callees hand the run of the selected Build on, to their caller or to their later jobs.
        self.assertTrue(all("run_id" in names for names in read.values()), read)
        world = JobWorld(self.directory).build()
        self.assertEqual(self.select(world, world.state("input")), (0, ""))
        self.assertLessEqual(read["packaged-e2e"], set(self.written()))
        self.assertEqual(self.written()["run_id"], str(world.bundle["producer"]["run_id"]))
        # Nothing to select: select-build.yml still reads both of its outputs, one of them empty.
        self.fresh()
        world = JobWorld(self.directory, push=True)
        self.assertEqual(self.select(world, world.state("select")), (0, ""))
        self.assertLessEqual(read["select-build"], set(self.written()))
        self.assertEqual((self.written()["found"], self.written()["run_id"]), ("false", ""))

    def named(self, world: JobWorld, *argv: str) -> str:
        """Run ``ci select-build`` as the ``input`` job does, in a state of its own; return the
        run it hands to the later jobs (``needs.input.outputs.build-run-id``)."""

        self.assertEqual(self.select(world, world.state("input"), *argv), (0, ""))
        run = self.written()["run_id"]
        self.fresh()
        return run

    # packaged-e2e.yml gives the lane, aggregate and gate jobs the run their input job named
    # (``--build-run-id <id>``), for a pull request too; ``selection.select_build`` refuses a run
    # id for a pull request. The sequence is being replaced: the selection record crosses the jobs.
    @unittest.expectedFailure
    def test_a_later_job_of_a_pull_request_selects_the_run_its_input_job_named(self) -> None:
        world = JobWorld(self.directory).build()
        named = self.named(world)
        self.assertEqual(self.select(world, world.state("lane"), "--build-run-id", named), (0, ""))

    # The run a rebuilding input job names is the packaged run itself; a later job that names it
    # is answered as if it had named a run of the Build caller.
    @unittest.expectedFailure
    def test_a_later_job_of_a_rebuilding_run_selects_the_run_its_input_job_named(self) -> None:
        world = rebuild(JobWorld(self.directory, push=True))
        named = self.named(world, "--build-run-id", "same-run")
        self.assertEqual(self.select(world, world.state("lane"), "--build-run-id", named), (0, ""))

    # ``ci select-build`` and ``ci fetch-build`` both create ``ci-selection.json`` in the state,
    # and a lane job runs one after the other in its one state: the second never replaces a record.
    @unittest.expectedFailure
    def test_a_lane_job_fetches_the_build_it_has_just_selected(self) -> None:
        world = JobWorld(self.directory, push=True).build()
        named = self.named(world)
        state = world.state("lane")
        self.assertEqual(self.select(world, state, "--build-run-id", named), (0, ""))
        self.assertEqual(self.fetch(world, state), (0, ""))
        self.assertEqual(verify_build_export(self.sealed, plan=world.plan), world.envelope)


if __name__ == "__main__":
    unittest.main()
