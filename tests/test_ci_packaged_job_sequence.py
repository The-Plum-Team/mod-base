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
from tests.test_ci_commands_packaged import CommandTestCase, JobWorld, run_ci
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


if __name__ == "__main__":
    unittest.main()
