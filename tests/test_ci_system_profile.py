"""``runtime.system_profile`` of the protected Build config and ``ci system-profile``.

The command runs for real through the CLI on a job of the synthetic mod; only ``sudo`` is a
recording stand-in (``system_profile.SUDO``), a real process that writes down its argument vector,
environment and working directory and then succeeds, fails, floods or hangs as the case asks.
That the real command installs the profile as root, and that the host fence still passes after it,
is ``tests/ci_linux_system_profile.py`` (hosted runner only).
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base.build_ci import system_profile, worker
from mod_base.build_ci.config import BUILD_CONFIG_PATH, load_build_config, system_profile as configured_profile
from mod_base.build_ci.config import validate_build_config
from mod_base.errors import MbError
from mod_base.model import limits
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h
from tests.helpers import ci_config

#: The natives' software-rendering installs, in their own order, at origin/master of each mod:
#: Quick Skin ``.github/actions/run-packaged-e2e/action.yml`` lines 78-94, Block Pops the same file
#: lines 66-69.
QUICK_SKIN = ("xvfb", "xauth", "mesa-utils", "libgl1-mesa-dri", "libglx-mesa0", "libasound2t64", "libopenal1",
              "libx11-6", "libxcursor1", "libxext6", "libxi6", "libxinerama1", "libxrandr2", "libxrender1",
              "libxtst6", "libxxf86vm1")
BLOCK_POPS = ("xvfb", "xauth", "mesa-utils", "libgl1-mesa-dri", "libglx-mesa0", "libegl1", "libegl-mesa0",
              "libasound2t64", "libopenal1", "libx11-6", "libxcursor1", "libxext6", "libxi6", "libxinerama1",
              "libxrandr2", "libxrender1", "libxtst6", "libxxf86vm1")
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
#: Everything before the package manager's own arguments, at the shipped bounds.
ROOT = ["-n", "--", "/usr/bin/timeout", "--kill-after=15s", "600s", "/usr/bin/env", "-i",
        "DEBIAN_FRONTEND=noninteractive", f"PATH={PATH}", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "/usr/bin/apt-get", "-q"]

#: The stand-in for sudo. ``{record}`` is its directory: ``mode`` says how it behaves, and every
#: call leaves ``call-<n>.json`` there.
FAKE_SUDO = r'''#!{python} -IS
import json, os, sys, time
record = {record!r}
with open(os.path.join(record, "mode"), encoding="utf-8") as stream:
    mode = stream.read().strip()
number = sum(name.startswith("call-") for name in os.listdir(record))
with open(os.path.join(record, f"call-{{number}}.json"), "w", encoding="utf-8") as stream:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
               "stdin": os.isatty(0) or os.read(0, 1) != b""}}, stream)
phase = "install" if "install" in sys.argv else "update"
if mode == "fail-" + phase:
    print("Reading package lists...\n\x1b[31mE: Unable to locate package xvfb ::warning::x")
    sys.exit(100)
if mode == "root-timeout-" + phase:
    print("Get:1 http://archive.ubuntu.com/ubuntu noble InRelease")
    sys.exit(124)
if mode == "hang-" + phase:
    print("Waiting for cache lock", flush=True)
    time.sleep(600)
if mode == "flood-" + phase:
    for index in range(4096):
        print(f"line {{index}} " + "x" * 100)
    print("E: the last words")
    sys.exit(1)
print(f"fake apt-get {{phase}} done")
'''


def with_profile(document: dict, profile: str | None) -> dict:
    document = copy.deepcopy(document)
    if profile is not None:
        document["runtime"] = {"system_profile": profile}
    return document


class SystemProfileConfigTests(unittest.TestCase):
    def test_the_profile_is_the_union_of_both_native_installs(self) -> None:
        packages = system_profile.SYSTEM_PROFILES["xvfb-mesa"]
        self.assertEqual(list(system_profile.SYSTEM_PROFILES), ["xvfb-mesa"])
        self.assertEqual(packages, tuple(sorted(set(QUICK_SKIN) | set(BLOCK_POPS))))
        self.assertEqual(len(packages), 18)
        self.assertEqual(set(BLOCK_POPS) - set(QUICK_SKIN), {"libegl1", "libegl-mesa0"})
        self.assertLessEqual(set(QUICK_SKIN), set(BLOCK_POPS))

    def test_an_absent_runtime_names_no_profile_and_a_named_profile_is_admitted(self) -> None:
        self.assertNotIn("runtime", ci_config())
        self.assertIsNone(configured_profile(validate_build_config(ci_config())))
        document = validate_build_config(with_profile(ci_config(), "xvfb-mesa"))
        self.assertEqual(configured_profile(document), "xvfb-mesa")

    def test_only_a_kit_profile_by_name_is_admitted(self) -> None:
        rejected = {
            "unknown profile": {"system_profile": "xvfb"},
            "spelled none": {"system_profile": "none"},
            "empty": {"system_profile": ""},
            "case": {"system_profile": "XVFB-MESA"},
            "null": {"system_profile": None},
            "a list": {"system_profile": ["xvfb-mesa"]},
            "no name": {},
            "a package list": {"system_profile": "xvfb-mesa", "packages": ["xvfb"]},
            "an extra key": {"system_profile": "xvfb-mesa", "apt_sources": []},
        }
        for label, runtime in rejected.items():
            with self.subTest(label), self.assertRaises(MbError):
                validate_build_config({**ci_config(), "runtime": runtime})
        for label, value in (("string", "xvfb-mesa"), ("null", None), ("list", [])):
            with self.subTest(runtime=label), self.assertRaises(MbError):
                validate_build_config({**ci_config(), "runtime": value})
        with self.assertRaises(MbError):
            validate_build_config({**ci_config(), "system_profile": "xvfb-mesa"})

    def test_the_named_profile_is_part_of_the_protected_config_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mod = h.materialize(Path(directory) / "mod")
            before = load_build_config(mod, repository=h.REPOSITORY)
            (mod / BUILD_CONFIG_PATH).write_bytes(h.pretty(with_profile(h.build_config(), "xvfb-mesa")))
            after = load_build_config(mod, repository=h.REPOSITORY)
        self.assertEqual((configured_profile(before.data), configured_profile(after.data)), (None, "xvfb-mesa"))
        self.assertNotEqual(before.sha256, after.sha256)


class SystemProfileCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        if sys.platform != "linux":
            self.skipTest("the command runs Linux processes and reads the passwd database")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.record = self.temporary / "sudo-record"
        self.record.mkdir()
        self.sudo = self.temporary / "fake-sudo"
        self.sudo.write_text(FAKE_SUDO.format(python=sys.executable, record=str(self.record)), encoding="utf-8")
        self.sudo.chmod(0o755)
        self.mode("succeed")
        patch = mock.patch.object(system_profile, "SUDO", str(self.sudo))
        patch.start()
        self.addCleanup(patch.stop)

    def mode(self, mode: str) -> None:
        (self.record / "mode").write_text(mode, encoding="utf-8")

    def calls(self) -> list[dict]:
        count = sum(path.name.startswith("call-") for path in self.record.iterdir())
        return [json.loads((self.record / f"call-{index}.json").read_bytes()) for index in range(count)]

    def run_command(self, profile: str | None = "xvfb-mesa") -> tuple[int, str, str]:
        """``ci subject`` then ``ci system-profile`` on a fresh job of the synthetic mod."""

        root = Path(tempfile.mkdtemp(dir=self.temporary))
        mod = h.materialize(root / "mod")
        (mod / BUILD_CONFIG_PATH).write_bytes(h.pretty(with_profile(h.build_config(), profile)))
        api, _ = h.github()
        commands = fixture.Commands(mod, root / "state", api, h.environment())
        commands.subject(root / "github-output")
        clients = len(commands.budgets)
        result = commands.run("system-profile")
        self.assertEqual(len(commands.budgets), clients, "ci system-profile builds no API client")
        return result

    def test_the_profile_is_installed_with_fixed_arguments_and_environment(self) -> None:
        code, stdout, stderr = self.run_command()
        self.assertEqual((code, stderr), (0, ""))
        self.assertRegex(stdout, r"^system-profile: xvfb-mesa installed, 18 packages in [0-9]+s; "
                                 r"no worker account exists yet\n$")
        environment = {"PATH": PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        self.assertEqual(self.calls(), [
            {"argv": [*ROOT, "update"], "env": environment, "cwd": "/", "stdin": False},
            {"argv": [*ROOT, "install", "--yes", "--no-install-recommends",
                      *system_profile.SYSTEM_PROFILES["xvfb-mesa"]], "env": environment, "cwd": "/", "stdin": False},
        ])

    def test_no_token_or_job_variable_reaches_sudo(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "t", "ACTIONS_RUNTIME_TOKEN": "t",
                                          "DEBIAN_FRONTEND": "readline", "LD_PRELOAD": "/x.so"}):
            self.assertEqual(self.run_command()[0], 0)
        for call in self.calls():
            self.assertEqual(set(call["env"]), {"PATH", "LANG", "LC_ALL"})

    def test_a_config_without_a_profile_installs_nothing(self) -> None:
        self.assertEqual(self.run_command(None), (
            0, "system-profile: the protected Build config names no system profile; nothing installed\n", ""))
        self.assertEqual(self.calls(), [])

    def test_it_refuses_once_a_worker_account_exists(self) -> None:
        import pwd

        existing = pwd.getpwuid(os.getuid()).pw_name
        for profile in ("xvfb-mesa", None):
            with self.subTest(profile=profile), mock.patch.dict(worker.WORKER_ACCOUNTS, {"validator": existing}):
                code, stdout, stderr = self.run_command(profile)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("a system profile must be installed before any worker account exists; "
                              f"found {existing}", stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(system_profile.existing_worker_accounts(), ())

    def test_a_failing_command_names_its_phase_and_shows_its_output_neutralised(self) -> None:
        for phase, calls in (("update", 1), ("install", 2)):
            with self.subTest(phase=phase):
                for path in self.record.glob("call-*"):
                    path.unlink()
                self.mode("fail-" + phase)
                code, stdout, stderr = self.run_command()
                self.assertEqual(code, 2)
                self.assertIn(f"system profile xvfb-mesa: apt-get {phase} exited with status 100: "
                              "?[31mE: Unable to locate package xvfb : :warning: :x", stderr)
                self.assertEqual(stdout, f"[apt-get {phase}] Reading package lists...\n"
                                         f"[apt-get {phase}] ?[31mE: Unable to locate package xvfb : :warning: :x\n")
                self.assertEqual(len(self.calls()), calls, "nothing runs after a failed command")

    def test_a_flood_of_output_is_kept_to_its_bounded_tail(self) -> None:
        self.mode("flood-install")
        code, stdout, stderr = self.run_command()
        self.assertEqual(code, 2)
        self.assertIn("apt-get install exited with status 1: E: the last words", stderr)
        lines = stdout.splitlines()
        self.assertEqual(lines[0], "[apt-get install] ... earlier output not kept")
        self.assertEqual(lines[-1], "[apt-get install] E: the last words")
        self.assertLessEqual(len(stdout.encode()) - len(lines) * len("[apt-get install] "),
                             limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES + 64)
        self.assertNotIn("line 0 ", stdout)

    def test_root_timeout_is_reported_as_a_timeout(self) -> None:
        self.mode("root-timeout-update")
        code, _, stderr = self.run_command()
        self.assertEqual(code, 2)
        self.assertIn("system profile xvfb-mesa: apt-get update timed out after its bound of 600s", stderr)
        self.assertEqual(len(self.calls()), 1)

    def test_a_command_that_outlives_the_bound_is_stopped(self) -> None:
        self.mode("hang-install")
        with mock.patch.object(limits, "CI_SYSTEM_PROFILE_TIMEOUT_SECONDS", 1), \
                mock.patch.object(limits, "CI_TERMINATION_GRACE_SECONDS", 1.0):
            code, _, stderr = self.run_command()
        self.assertEqual(code, 2)
        self.assertRegex(stderr, r"system profile xvfb-mesa: apt-get install timed out \(elapsed [0-9]+s\)")
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["argv"][:5], ["-n", "--", "/usr/bin/timeout", "--kill-after=1s", "1s"])

    def test_an_unknown_profile_is_refused_before_anything_runs(self) -> None:
        with self.assertRaisesRegex(MbError, "unknown system profile 'xvfb'"):
            system_profile.install_system_profile("xvfb", log=lambda text: None)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
