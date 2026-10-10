"""Hosted proof that ``ci system-profile`` really installs the profile as root before the fence.

Explicitly invoked by CI, never collected by the plain suite: it installs packages on the runner
with the real ``sudo`` and ``apt-get``, then fences the host and allocates the real accounts with
``ci worker-prepare``. Only the GitHub API is a fake (``LinuxLifecycleCommandTests``).

The host fence proves that nothing outside the worker boundary is writable by everyone; it does
not look for what a package's files or maintainer scripts could leave besides: a setuid or setgid
file, a file whose owner or group no account has yet (an account created later could get that
id), a new account or group, a process left running or a socket left listening. So the test takes
those of the host before and after the install and requires them to be the same.
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path

from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.build_ci.protocol import SYSTEM_PROFILES
from tests import ci_linux_worker as hosted
from tests import ci_mod_harness as h

#: What the lane's client runs, from the profile's packages: the X server, its wrapper, the X
#: authority tool and Mesa's GL probe.
BINARIES = ("/usr/bin/Xvfb", "/usr/bin/xvfb-run", "/usr/bin/xauth", "/usr/bin/glxinfo")
#: Where every X server of the host puts its socket, shared by the accounts that run one.
X11_SOCKETS = Path("/tmp/.X11-unix")
#: One whole scan of the root filesystem; the host fence allows its own commands as long.
SCAN_SECONDS = 900


def _run(*args: str, timeout: float = 60) -> str:
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=hosted.HOST_ENV,
                            timeout=timeout, check=False)
    if result.returncode != 0:
        raise AssertionError(f"{args[0]} failed ({result.returncode}): {result.stderr[-2000:]}")
    return result.stdout


def _wsl() -> bool:
    """Whether this is a WSL rig rather than a hosted image (``/tmp/.X11-unix`` is WSLg's there)."""

    return "microsoft" in Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8").lower()


def host_snapshot() -> dict[str, object]:
    """What a package could add that the host fence does not look for."""

    # Files: setuid or setgid, or owned by a uid or gid no account or group has, on the root
    # filesystem (the one the packages are installed to).
    special = _run("/usr/bin/sudo", "-n", "--", "/usr/bin/find", "/", "-xdev",
                   "(", "-perm", "/6000", "-o", "-nouser", "-o", "-nogroup", ")", "-print0", timeout=SCAN_SECONDS)
    # Processes other than kernel threads, by owner and name: a daemon a maintainer script started.
    processes = set()
    for line in _run("/usr/bin/ps", "-e", "-o", "pid=,ppid=,uid=,comm=").splitlines():
        pid, ppid, uid, name = line.split(None, 3)
        if "2" not in (pid, ppid):
            processes.add((int(uid), name))
    # Listening sockets, by kind and local address (a Unix socket's inode changes with every run).
    sockets = set()
    for line in _run("/usr/bin/ss", "-H", "-l", "-n", "-x", "-t", "-u").splitlines():
        fields = line.split()
        sockets.add((fields[0], fields[4]))
    return {
        "special files": sorted(filter(None, special.split("\0"))),
        "processes": sorted(processes),
        "listening sockets": sorted(sockets),
        "/etc/passwd": Path("/etc/passwd").read_text(encoding="utf-8"),
        "/etc/group": Path("/etc/group").read_text(encoding="utf-8"),
        "packages": sorted(_run("/usr/bin/dpkg-query", "--show", "--showformat=${Package}:${Architecture} "
                                "${db:Status-Abbrev}\\n").splitlines()),
    }


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
        before = host_snapshot()
        code, stdout, stderr = commands.run("system-profile")
        self.assertEqual((code, stderr), (0, ""), stdout)
        self.assertRegex(stdout, r"^system-profile: xvfb-mesa installed, 18 packages in [0-9]+s; "
                                 r"no worker account exists yet\n$")
        print(stdout, end="")
        after = host_snapshot()
        packages = SYSTEM_PROFILES["xvfb-mesa"]
        status = subprocess.run(["/usr/bin/dpkg-query", "--show", "--showformat=${Package} ${db:Status-Abbrev}\\n",
                                 *packages], capture_output=True, text=True, check=True, timeout=60).stdout
        self.assertEqual(sorted(line.rstrip() for line in status.splitlines()),
                         sorted(f"{package} ii" for package in packages))
        added = sorted(set(after.pop("packages")) - set(before.pop("packages")))
        print(f"system-profile: {len(added)} packages newly installed: {' '.join(added)}")
        # Nothing the fence does not look for appeared: no setuid, setgid or orphan-owned file, no
        # account or group, no process, no listening socket.
        for key, value in before.items():
            with self.subTest(snapshot=key):
                if isinstance(value, list):
                    self.assertEqual(sorted(set(after[key]) - set(value)), [])
                else:
                    self.assertEqual(after[key], value)
        # The X socket directory every X server shares: absent, or root's, sticky and open to all.
        # On a WSL rig it is WSLg's own mount (0777, not sticky), which no hosted image has.
        if _wsl():
            print(f"system-profile: {X11_SOCKETS} not checked on a WSL rig (WSLg's mount)")
        elif os.path.lexists(X11_SOCKETS):
            info = os.lstat(X11_SOCKETS)
            self.assertTrue(stat.S_ISDIR(info.st_mode))
            self.assertEqual((info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)), (0, 0, 0o1777))
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
