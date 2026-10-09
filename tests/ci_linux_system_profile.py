"""Hosted proof that ``ci system-profile`` really installs the profile as root before the fence.

Explicitly invoked by CI, never collected by the plain suite: it installs packages on the runner
with the real ``sudo`` and ``apt-get``, then fences the host and allocates the real accounts with
``ci worker-prepare``. Only the GitHub API is a fake (``LinuxLifecycleCommandTests``).
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path

from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.build_ci.system_profile import SYSTEM_PROFILES
from tests import ci_linux_worker as hosted
from tests import ci_mod_harness as h

#: What the lane's client runs, from the profile's packages: the X server, its wrapper, the X
#: authority tool and Mesa's GL probe.
BINARIES = ("/usr/bin/Xvfb", "/usr/bin/xvfb-run", "/usr/bin/xauth", "/usr/bin/glxinfo")


class LinuxSystemProfileTests(unittest.TestCase):
    HOME = hosted.LinuxLifecycleCommandTests.HOME
    setUp = hosted.LinuxLifecycleCommandTests.setUp
    cleanup = hosted.LinuxLifecycleCommandTests.cleanup
    job = hosted.LinuxLifecycleCommandTests.job
    prepare = hosted.LinuxLifecycleCommandTests.prepare
    finish = hosted.LinuxLifecycleCommandTests.finish
    account = hosted.LinuxLifecycleCommandTests.account
    as_account = hosted.LinuxLifecycleCommandTests.as_account
    assert_resting = hosted.LinuxLifecycleCommandTests.assert_resting
    assert_finished = hosted.LinuxLifecycleCommandTests.assert_finished
    home = hosted.LinuxLifecycleCommandTests.home

    def test_the_profile_is_installed_before_the_fence_and_stays_closed_to_the_accounts(self):
        mod = h.materialize(self.temporary / "mod")
        (mod / BUILD_CONFIG_PATH).write_bytes(h.pretty({**h.build_config(), "runtime": {"system_profile": "xvfb-mesa"}}))
        commands = self.job(mod=mod)
        code, stdout, stderr = commands.run("system-profile")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertRegex(stdout, r"^system-profile: xvfb-mesa installed, 18 packages in [0-9]+s; "
                                 r"no worker account exists yet\n$")
        print(stdout, end="")
        packages = SYSTEM_PROFILES["xvfb-mesa"]
        status = subprocess.run(["/usr/bin/dpkg-query", "--show", "--showformat=${Package} ${db:Status-Abbrev}\\n",
                                 *packages], capture_output=True, text=True, check=True, timeout=60).stdout
        self.assertEqual(sorted(line.rstrip() for line in status.splitlines()),
                         sorted(f"{package} ii" for package in packages))
        # Root's files: nobody else may write them or a directory on their way.
        for binary in BINARIES:
            with self.subTest(binary=binary):
                info = os.stat(binary)
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual((info.st_uid, info.st_gid), (0, 0))
                self.assertEqual(stat.S_IMODE(info.st_mode) & 0o022, 0)
                for parent in Path(binary).parents:
                    self.assertEqual(os.lstat(parent).st_uid, 0, parent)
                    self.assertEqual(stat.S_IMODE(os.lstat(parent).st_mode) & 0o022, 0, parent)

        # The host fence and the tool admission still pass over the installed image.
        code, stdout, stderr = self.prepare("candidate+validator")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stdout.split(";")[0], "worker-prepare: candidate and validator allocated and locked")
        for role in ("candidate", "validator"):
            for binary in BINARIES:
                with self.subTest(role=role, binary=binary):
                    self.assertEqual(self.as_account(role, "/usr/bin/test", "-w", binary), 1, "writable")
                    self.assertEqual(self.as_account(role, "/usr/bin/test", "-x", binary), 0, "not executable")
            for directory in ("/usr/bin", "/usr/lib/xorg", "/usr/share/X11/xkb"):
                with self.subTest(role=role, directory=directory):
                    self.assertTrue(os.path.isdir(directory))
                    self.assertEqual(self.as_account(role, "/usr/bin/test", "-w", directory), 1, "writable")

        # Once the accounts exist, root installs nothing more.
        code, stdout, stderr = commands.run("system-profile")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("a system profile must be installed before any worker account exists; "
                      "found modbase_candidate, modbase_validator", stderr)
        self.assert_finished("candidate", "validator")


if __name__ == "__main__":
    unittest.main()
