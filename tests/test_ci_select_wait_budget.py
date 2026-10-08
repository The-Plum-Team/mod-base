"""``ci select-build`` inside its request budget for the whole wait a pull request is promised.

``tests/test_ci_packaged_selection.py`` waits the 5400 seconds on a client without a budget and
proves the budget by arithmetic for a Build run that is still pending (one listing request a
poll). Here the client has exactly the budget the command gives it
(``limits.MAX_CI_SELECT_BUILD_REQUESTS``) and the wait is the other one the design names: the
newest Build run is a finished draft deferral, which is ignored as a producer while a fresh ready
generation is waited for, "bounded by the wait limit".
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base.build_ci import selection
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests.test_ci_commands_packaged import JobWorld


class SelectBuildWaitBudgetTests(unittest.TestCase):
    def wait(self, world: JobWorld, directory: Path) -> None:
        """The whole wait of the packaged ``input`` job of a pull request, on a clock that only
        its own sleeps advance."""

        elapsed = [0]

        def sleep(seconds: float) -> None:
            elapsed[0] += seconds

        selection.select_build(world.api, plan=world.plan, run_id=43, run_attempt=2,
                               workflow_path=CI_CALLER_WORKFLOWS["packaged"], event="pull_request_target",
                               temporary_root=directory, monotonic=lambda: elapsed[0], sleep=sleep)

    def test_a_whole_wait_on_a_pending_build_ends_at_the_deadline_inside_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = JobWorld(Path(directory), max_requests=limits.MAX_CI_SELECT_BUILD_REQUESTS)
            world.add_run("build", "build-full", status="in_progress", conclusion=None)
            with self.assertRaisesRegex(MbError, "exhausted the 5400-second"):
                self.wait(world, Path(directory))
            self.assertLess(world.api.request_count, limits.MAX_CI_SELECT_BUILD_REQUESTS)

    # A finished deferral is read again on every poll (the listing and the run): 185 requests for
    # the 90 polls of the wait, and the command's budget is 155.
    @unittest.expectedFailure
    def test_a_whole_wait_on_a_draft_deferral_ends_at_the_deadline_inside_the_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            world = JobWorld(Path(directory), max_requests=limits.MAX_CI_SELECT_BUILD_REQUESTS)
            world.add_run("build", "build-deferred")
            with self.assertRaisesRegex(MbError, "exhausted the 5400-second"):
                self.wait(world, Path(directory))


if __name__ == "__main__":
    unittest.main()
