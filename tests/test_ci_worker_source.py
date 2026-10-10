"""Tracked-source staging is root-only; this suite proves the gate and nothing else.

The module composes the source inventory and tree primitives, which have their own real-file
tests (``tests/test_ci_source.py``, ``tests/test_tree_source_private.py``). Publishing a checkout
to a really allocated candidate, and every refusal of an unfaithful one, runs as root in
``tests/ci_linux_worker.py``.
"""

import os
import sys
import unittest
from pathlib import Path

from mod_base.build_ci import worker_source as source
from mod_base.build_ci.source import GitSourceEntry
from mod_base.errors import MbError
from tests.test_ci_gradle_cache import CANDIDATE
from tests.test_ci_host import BOUNDARY


@unittest.skipUnless(sys.platform == "linux", "the staging entries are Linux-only")
class RootOnlyTests(unittest.TestCase):
    def test_staging_refuses_the_unprivileged_runner_before_it_looks_at_the_checkout(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        inventory = (GitSourceEntry("script.sh", "100755", 3, "a" * 40),)
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            source.stage_privileged_worker_source(Path("/home/runner/absent-checkout"), boundary=BOUNDARY,
                                                  account=CANDIDATE, inventory=inventory)


if __name__ == "__main__":
    unittest.main()
