"""``runtime.system_profile`` of the protected Build config and ``ci system-profile``.

The command runs through the CLI on a job of the synthetic mod for everything that happens before
root is asked: the profile it reads, the no-op without one and the refusal once an account exists.
How the root commands run is :func:`system_profile.install_system_profile` called with a recording
stand-in for ``sudo`` (its ``sudo`` argument; the CLI always passes ``/usr/bin/sudo``), a real
process that writes down its argument vector, environment and working directory and then succeeds,
fails, floods or hangs as the case asks. That the real command installs the profile as root, and
that the host fence still passes after it, is ``tests/ci_linux_system_profile.py`` (hosted runner
only).
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
from mod_base.build_ci.protocol import SYSTEM_PROFILES
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
        "DEBIAN_FRONTEND=noninteractive", f"PATH={PATH}", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "/usr/bin/apt-get", "-q",
        "-o", "DPkg::Lock::Timeout=120", "-o", "Acquire::Retries=3"]

#: The stand-in for sudo. ``{record}`` is its directory: ``mode`` says how it behaves, and every
#: call leaves ``call-<n>.json`` there.
FAKE_SUDO = r'''#!{python} -IS
import json, os, signal, sys, time
record = {record!r}
with open(os.path.join(record, "mode"), encoding="utf-8") as stream:
    mode = stream.read().strip()
number = sum(name.startswith("call-") for name in os.listdir(record))
with open(os.path.join(record, f"call-{{number}}.json"), "w", encoding="utf-8") as stream:
    json.dump({{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(), "pid": os.getpid(),
               "stdin": os.isatty(0) or os.read(0, 1) != b""}}, stream)
phase = "install" if "install" in sys.argv else "update"
if mode == "fail-" + phase:
    print("Reading package lists...\n\x1b[31mE: Unable to locate package xvfb ::warning::x")
    sys.exit(100)
if mode == "root-timeout-" + phase:
    print("Get:1 http://archive.ubuntu.com/ubuntu noble InRelease")
    sys.exit(124)
if mode == "root-killed-" + phase:
    print("Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend")
    sys.exit(137)
if mode in ("hang-" + phase, "stubborn-" + phase):
    if mode.startswith("stubborn-"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
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
        packages = SYSTEM_PROFILES["xvfb-mesa"]
        self.assertEqual(list(SYSTEM_PROFILES), ["xvfb-mesa"])
        self.assertEqual(packages, tuple(sorted(set(QUICK_SKIN) | set(BLOCK_POPS))))
        self.assertEqual(len(packages), 18)
        self.assertEqual(set(BLOCK_POPS) - set(QUICK_SKIN), {"libegl1", "libegl-mesa0"})
        self.assertLessEqual(set(QUICK_SKIN), set(BLOCK_POPS))

    def test_the_lock_wait_fits_inside_the_command_bound(self) -> None:
        self.assertLess(limits.CI_SYSTEM_PROFILE_LOCK_WAIT_SECONDS, limits.CI_SYSTEM_PROFILE_TIMEOUT_SECONDS // 2)

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

    def test_the_config_schema_does_not_load_the_execution_module(self) -> None:
        import subprocess

        # The pure schema validates a closed name; it needs neither the installer nor the account
        # code that the installer runs beside.
        probe = ("import sys; import mod_base.build_ci.config; "
                 "print(sorted(m for m in ('mod_base.build_ci.system_profile', 'mod_base.build_ci.worker') "
                 "if m in sys.modules))")
        source = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run([sys.executable, "-B", "-c", probe], capture_output=True, text=True, check=True,
                                env={**os.environ, "PYTHONPATH": source, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)
        self.assertEqual(result.stdout, "[]\n")


class SystemProfileCommandTests(unittest.TestCase):
    """``ci system-profile`` up to the point where it would ask root: no ``sudo`` runs here."""

    def setUp(self) -> None:
        if sys.platform != "linux":
            self.skipTest("the command reads the passwd database")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()

    def run_command(self, profile: str | None) -> tuple[int, str, str]:
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

    def test_a_config_without_a_profile_installs_nothing(self) -> None:
        self.assertEqual(self.run_command(None), (
            0, "system-profile: the protected Build config names no system profile; nothing installed\n", ""))

    def test_it_refuses_once_a_worker_account_exists(self) -> None:
        import pwd

        existing = pwd.getpwuid(os.getuid()).pw_name
        for profile in ("xvfb-mesa", None):
            with self.subTest(profile=profile), mock.patch.dict(worker.WORKER_ACCOUNTS, {"validator": existing}):
                code, stdout, stderr = self.run_command(profile)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn("a system profile must be installed before any worker account exists; "
                              f"found {existing}", stderr)
        self.assertEqual(system_profile.existing_worker_accounts(), ())


class SystemProfileInstallTests(unittest.TestCase):
    """The two root commands, run through a recording stand-in for ``sudo``."""

    def setUp(self) -> None:
        if sys.platform != "linux":
            self.skipTest("the installer runs Linux processes and reads the passwd database")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name).resolve()
        self.record = self.temporary / "sudo-record"
        self.record.mkdir()
        self.sudo = self.temporary / "fake-sudo"
        self.sudo.write_text(FAKE_SUDO.format(python=sys.executable, record=str(self.record)), encoding="utf-8")
        self.sudo.chmod(0o755)
        self.mode("succeed")

    def mode(self, mode: str) -> None:
        for path in self.record.glob("call-*"):
            path.unlink()
        (self.record / "mode").write_text(mode, encoding="utf-8")

    def calls(self) -> list[dict]:
        count = sum(path.name.startswith("call-") for path in self.record.iterdir())
        return [json.loads((self.record / f"call-{index}.json").read_bytes()) for index in range(count)]

    def install(self, profile: str | None = "xvfb-mesa", *, systemd: bool = False) -> tuple[int | None, str, str]:
        """The package count (``None`` on a rejection), what was logged and the rejection."""

        logged: list[str] = []
        try:
            count = system_profile.install_system_profile(profile, log=logged.append, sudo=str(self.sudo),
                                                          systemd=systemd)
        except system_profile.SystemProfileError as error:
            return None, "".join(logged), str(error)
        return count, "".join(logged), ""

    def assert_stopped(self, call: dict) -> None:
        with self.assertRaises(ProcessLookupError):
            os.kill(call["pid"], 0)

    def test_the_profile_is_installed_with_fixed_arguments_and_environment(self) -> None:
        self.assertEqual(self.install(), (18, "", ""))
        environment = {"PATH": PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        self.assertEqual([{key: call[key] for key in ("argv", "env", "cwd", "stdin")} for call in self.calls()], [
            {"argv": [*ROOT, "update"], "env": environment, "cwd": "/", "stdin": False},
            {"argv": [*ROOT, "install", "--yes", "--no-install-recommends", *SYSTEM_PROFILES["xvfb-mesa"]],
             "env": environment, "cwd": "/", "stdin": False},
        ])

    def test_on_systemd_the_services_apt_woke_are_stopped_again(self) -> None:
        # The hosted image's PackageKit apt hook wakes packagekitd over D-Bus; it must not outlive the step.
        self.assertEqual(self.install(systemd=True), (18, "", ""))
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2]["argv"], [*ROOT[:ROOT.index("/usr/bin/apt-get")], "/usr/bin/systemctl", "stop",
                                            "packagekit.service"])
        self.assertEqual(set(calls[2]["env"]), {"PATH", "LANG", "LC_ALL"})
        self.mode("succeed")
        self.assertEqual(self.install(None, systemd=True), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_no_token_or_job_variable_reaches_sudo(self) -> None:
        with mock.patch.dict(os.environ, {"GH_TOKEN": "t", "GITHUB_TOKEN": "t", "ACTIONS_RUNTIME_TOKEN": "t",
                                          "DEBIAN_FRONTEND": "readline", "LD_PRELOAD": "/x.so"}):
            self.assertEqual(self.install()[0], 18)
        for call in self.calls():
            self.assertEqual(set(call["env"]), {"PATH", "LANG", "LC_ALL"})

    def test_no_profile_runs_nothing(self) -> None:
        self.assertEqual(self.install(None), (0, "", ""))
        self.assertEqual(self.calls(), [])

    def test_nothing_runs_once_a_worker_account_exists(self) -> None:
        import pwd

        with mock.patch.dict(worker.WORKER_ACCOUNTS, {"candidate": pwd.getpwuid(os.getuid()).pw_name}):
            self.assertIsNone(self.install()[0])
        self.assertEqual(self.calls(), [])

    def test_a_failing_command_names_its_phase_and_shows_its_output_neutralised(self) -> None:
        for phase, calls in (("update", 1), ("install", 2)):
            with self.subTest(phase=phase):
                self.mode("fail-" + phase)
                count, logged, error = self.install()
                self.assertIsNone(count)
                self.assertRegex(error, rf"^system profile xvfb-mesa: apt-get {phase} exited with status 100: "
                                        r"\?\[31mE: Unable to locate package xvfb : :warning: :x \(elapsed [0-9]+s\)$")
                self.assertEqual(logged, f"[apt-get {phase}] Reading package lists...\n"
                                         f"[apt-get {phase}] ?[31mE: Unable to locate package xvfb : :warning: :x\n")
                self.assertEqual(len(self.calls()), calls, "nothing runs after a failed command")

    def test_a_flood_of_output_is_kept_to_its_bounded_tail(self) -> None:
        self.mode("flood-install")
        count, logged, error = self.install()
        self.assertIsNone(count)
        self.assertIn("apt-get install exited with status 1: E: the last words", error)
        lines = logged.splitlines()
        self.assertEqual(lines[0], "[apt-get install] ... earlier output not kept")
        self.assertEqual(lines[-1], "[apt-get install] E: the last words")
        self.assertLessEqual(len(logged.encode()) - len(lines) * len("[apt-get install] "),
                             limits.MAX_CI_SYSTEM_PROFILE_LOG_BYTES + 64)
        self.assertNotIn("line 0 ", logged)

    def test_root_timeout_is_reported_as_a_timeout_whether_it_ended_on_term_or_kill(self) -> None:
        for mode, line in (
                ("root-timeout-update", "Get:1 http://archive.ubuntu.com/ubuntu noble InRelease"),
                ("root-killed-update", "Waiting for cache lock: Could not get lock /var/lib/dpkg/lock-frontend")):
            with self.subTest(mode=mode):
                self.mode(mode)
                count, logged, error = self.install()
                self.assertIsNone(count)
                self.assertRegex(error, r"^system profile xvfb-mesa: apt-get update timed out after its bound of "
                                        r"600s \(elapsed [0-9]+s\)$")
                self.assertEqual(logged, f"[apt-get update] {line}\n")
                self.assertEqual(len(self.calls()), 1)

    def test_a_command_that_outlives_the_deadline_is_stopped_and_its_tail_shown(self) -> None:
        # ``hang`` ends on the TERM relayed through sudo; ``stubborn`` ignores it and is killed.
        for mode in ("hang-install", "stubborn-install"):
            with self.subTest(mode=mode), \
                    mock.patch.object(limits, "CI_SYSTEM_PROFILE_TIMEOUT_SECONDS", 1), \
                    mock.patch.object(limits, "CI_TERMINATION_GRACE_SECONDS", 1.0):
                self.mode(mode)
                count, logged, error = self.install()
                self.assertIsNone(count)
                self.assertRegex(error, r"^system profile xvfb-mesa: apt-get install timed out \(elapsed [0-9]+s\)$")
                self.assertEqual(logged, "[apt-get install] Waiting for cache lock\n")
                calls = self.calls()
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1]["argv"][:5], ["-n", "--", "/usr/bin/timeout", "--kill-after=1s", "1s"])
                self.assert_stopped(calls[1])

    def test_a_command_that_cannot_start_names_its_phase(self) -> None:
        self.sudo.unlink()
        count, logged, error = self.install()
        self.assertIsNone(count)
        self.assertRegex(error, r"^system profile xvfb-mesa: apt-get update could not start: ")
        self.assertEqual(logged, "")

    def test_an_unknown_profile_is_refused_before_anything_runs(self) -> None:
        with self.assertRaisesRegex(MbError, "unknown system profile 'xvfb'"):
            system_profile.install_system_profile("xvfb", log=lambda text: None, sudo=str(self.sudo))
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
