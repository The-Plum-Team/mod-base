"""Disposable-account protocol regressions; real Linux boundary proof is separately required."""

from __future__ import annotations

import os
import stat
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mod_base.build_ci import worker
from mod_base.build_ci.protocol import subject_of
from mod_base.errors import MbError
from mod_base.model import limits
from tests.helpers import ci_plan


ACCOUNT = worker.WorkerAccount("candidate", 2000, 2000, str(worker.WORKER_ROOT / "candidate-home"))


class AllocationTests(unittest.TestCase):
    def metadata(self, *, owner=1001, group=1001, mode=0o711, inode=1):
        return SimpleNamespace(st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode,
                               st_dev=1, st_ino=inode)

    def test_boundary_requires_hosted_linux_and_rejects_existing_identity_before_mutation(self):
        for platform, environment in [("win32", "github-hosted"), ("linux", "self-hosted")]:
            with self.subTest(platform=platform), patch.object(worker.sys, "platform", platform), \
                    patch.object(worker.os, "open") as opening, self.assertRaises(MbError):
                worker.prepare_worker_boundary(runner_environment=environment)
            opening.assert_not_called()
        module = SimpleNamespace(getpwnam=MagicMock(return_value=SimpleNamespace()))
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.os, "getgid", return_value=1001, create=True), \
                patch.object(worker.os, "open") as opening, self.assertRaises(MbError):
            worker.prepare_worker_boundary(runner_environment="github-hosted")
        opening.assert_not_called()

    def test_existing_boundary_path_fails_exclusive_creation_and_closes_descriptor(self):
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=KeyError))
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.os, "getgid", return_value=1001, create=True), \
                patch.object(worker.os, "O_DIRECTORY", 65536, create=True), \
                patch.object(worker.os, "O_NOFOLLOW", 131072, create=True), \
                patch.object(worker.os, "open", return_value=10), \
                patch.object(worker.os, "mkdir", side_effect=FileExistsError) as mkdir, \
                patch.object(worker.os, "close") as close, self.assertRaises(MbError):
            worker.prepare_worker_boundary(runner_environment="github-hosted")
        mkdir.assert_called_once_with("mod-base-sandbox-boundary", mode=0o711, dir_fd=10)
        close.assert_called_once_with(10)

    def test_fresh_boundary_binds_both_directories_and_closes_every_descriptor(self):
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=KeyError))
        metadata = self.metadata(group=121)
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.os, "getgid", return_value=121, create=True), \
                patch.object(worker.os, "O_DIRECTORY", 65536, create=True), \
                patch.object(worker.os, "O_NOFOLLOW", 131072, create=True), \
                patch.object(worker.os, "open", side_effect=[10, 11, 12]), \
                patch.object(worker.os, "mkdir") as mkdir, \
                patch.object(worker.os, "stat", return_value=metadata), \
                patch.object(worker.os, "fstat", return_value=metadata), \
                patch.object(worker.os, "fchmod", create=True) as chmod, \
                patch.object(worker.os, "fsync"), patch.object(worker.os, "close") as close:
            worker.prepare_worker_boundary(runner_environment="github-hosted")
        self.assertEqual([call.args[0] for call in mkdir.call_args_list],
                         ["mod-base-sandbox-boundary", "mod-base-worker"])
        self.assertEqual([call.args for call in chmod.call_args_list], [(11, 0o711), (12, 0o711)])
        self.assertEqual([call.args[0] for call in close.call_args_list], [10, 11, 12])

    def test_root_runner_user_or_group_is_rejected_before_filesystem_access(self):
        for uid, gid in ((0, 121), (1001, 0)):
            with self.subTest(uid=uid, gid=gid), patch.object(worker.sys, "platform", "linux"), \
                    patch.object(worker.os, "getuid", return_value=uid, create=True), \
                    patch.object(worker.os, "getgid", return_value=gid, create=True), \
                    patch.object(worker.os, "open") as opening, self.assertRaises(MbError):
                worker.prepare_worker_boundary(runner_environment="github-hosted")
            opening.assert_not_called()

    def test_fresh_account_has_private_owned_directories_and_no_sudo_policy(self):
        stamps = [self.metadata(), self.metadata(inode=2)]
        for inode in (3, 4, 5):
            stamps.extend([self.metadata(mode=0o700, inode=inode),
                           self.metadata(owner=2000, group=2000, mode=0o700, inode=inode)])
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=KeyError))
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.Path, "lstat", side_effect=stamps), \
                patch.object(worker.Path, "mkdir") as mkdir, \
                patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                patch.object(worker, "_control", return_value=b"") as control:
            self.assertEqual(worker.allocate_worker_account("candidate"), ACCOUNT)
        self.assertEqual(mkdir.call_count, 3)
        self.assertTrue(all(call.kwargs == {"mode": 0o700} for call in mkdir.call_args_list))
        calls = control.call_args_list
        self.assertIn("useradd", calls[0].args[0][2])
        self.assertEqual(calls[1].args[0][-2:], ("-n", "--list"))
        self.assertEqual(calls[1].kwargs["accepted"], frozenset({1}))
        self.assertEqual(len(calls), 5)
        self.assertTrue(all("--no-dereference" in call.args[0] for call in calls[2:]))
        self.assertTrue(all("-R" not in call.args[0] for call in calls))

    def test_preexisting_account_or_unsafe_boundary_prevents_creation(self):
        for record in (SimpleNamespace(), None):
            module = SimpleNamespace(getpwnam=MagicMock(return_value=record, side_effect=None if record else KeyError))
            with self.subTest(record=record), patch.object(worker.sys, "platform", "linux"), \
                    patch.dict(sys.modules, {"pwd": module}), \
                    patch.object(worker.os, "getuid", return_value=1001, create=True), \
                    patch.object(worker.Path, "lstat", return_value=self.metadata(mode=0o777)), \
                    patch.object(worker.Path, "mkdir") as mkdir, \
                    patch.object(worker, "_control") as control, self.assertRaises(MbError):
                worker.allocate_worker_account("candidate")
            mkdir.assert_not_called()
            control.assert_not_called()

    def test_failed_sudo_policy_probe_terminates_allocated_identity(self):
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=KeyError))
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.Path, "lstat", return_value=self.metadata()), \
                patch.object(worker.Path, "mkdir"), \
                patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                patch.object(worker, "_control", side_effect=[b"", worker.WorkerError("policy admitted")]), \
                patch.object(worker, "terminate_worker") as terminate, self.assertRaises(MbError):
            worker.allocate_worker_account("candidate")
        terminate.assert_called_once_with(ACCOUNT)

    def test_non_linux_and_unknown_role_are_rejected(self):
        with patch.object(worker.sys, "platform", "win32"), self.assertRaises(MbError):
            worker.allocate_worker_account("candidate")
        with patch.object(worker.sys, "platform", "linux"), self.assertRaises(MbError):
            worker.allocate_worker_account("other")

    def test_partially_failed_creation_locks_name_without_killing_unverified_uid(self):
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=[KeyError, SimpleNamespace(pw_uid=0)]))
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.Path, "lstat", return_value=self.metadata()), \
                patch.object(worker.Path, "mkdir"), \
                patch.object(worker, "_control", side_effect=[worker.WorkerError("partial creation"), b""]) as control, \
                patch.object(worker, "terminate_worker") as terminate, self.assertRaises(MbError):
            worker.allocate_worker_account("candidate")
        terminate.assert_not_called()
        self.assertEqual(control.call_args_list[-1].args[0],
                         ("/usr/bin/sudo", "-n", "/usr/sbin/usermod", "--lock", "--expiredate",
                          "1970-01-02", "modbase_candidate"))

    def test_changed_home_ownership_forbids_return_and_terminates_new_identity(self):
        module = SimpleNamespace(getpwnam=MagicMock(side_effect=KeyError))
        stamps = [self.metadata(), self.metadata(), self.metadata(mode=0o700, inode=3),
                  self.metadata(owner=2000, group=2000, mode=0o700, inode=4)]
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(worker.os, "getuid", return_value=1001, create=True), \
                patch.object(worker.Path, "lstat", side_effect=stamps), \
                patch.object(worker.Path, "mkdir"), \
                patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                patch.object(worker, "_control", return_value=b""), \
                patch.object(worker, "terminate_worker") as terminate, self.assertRaises(MbError):
            worker.allocate_worker_account("candidate")
        terminate.assert_called_once_with(ACCOUNT)


class EnvironmentTests(unittest.TestCase):
    def environment(self, **changes):
        arguments = {"role": "candidate", "python": "/opt/python/bin/python3", "java_home": "/opt/jdk",
                     "identity": ci_plan()["identity"], "run_id": 42, "run_attempt": 2, "values": {}}
        arguments.update(changes)
        return worker.worker_environment(**arguments)

    def test_identity_is_explicit_ambient_credentials_are_absent_and_homes_are_distinct(self):
        with patch.dict(os.environ, {"ACTIONS_RUNTIME_TOKEN": "secret", "GITHUB_TOKEN": "secret", "PYTHONPATH": "/candidate"}):
            candidate = dict(entry.split("=", 1) for entry in self.environment())
            validator = dict(entry.split("=", 1) for entry in self.environment(role="validator"))
        self.assertEqual(candidate["MB_TESTED_SHA"], ci_plan()["identity"]["tested_sha"])
        self.assertNotEqual(candidate["MB_TESTED_SHA"], ci_plan()["identity"]["controller_sha"])
        self.assertEqual(candidate["MB_RUN_ATTEMPT"], "2")
        for key in ("ACTIONS_RUNTIME_TOKEN", "GITHUB_TOKEN", "GITHUB_OUTPUT", "PYTHONPATH", "RUNNER_TEMP"):
            self.assertNotIn(key, candidate)
            self.assertNotIn(key, validator)
        for key in ("HOME", "TMPDIR", "GRADLE_USER_HOME", "PYTHONPYCACHEPREFIX"):
            self.assertNotEqual(candidate[key], validator[key])
        self.assertEqual(candidate["PYTHONNOUSERSITE"], "1")
        self.assertEqual(validator["PYTHONSAFEPATH"], "1")
        self.assertNotIn("GIT_CONFIG_COUNT", candidate)
        self.assertEqual(validator["GIT_CONFIG_KEY_0"], "safe.directory")
        self.assertEqual(validator["GIT_CONFIG_VALUE_0"], str(worker.WORKER_ROOT / "repository"))

    def test_closed_env_refuses_credentials_hooks_path_and_identity_overrides(self):
        for name in ("GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN", "GITHUB_ENV", "RUNNER_TEMP", "HOME", "PATH",
                     "LD_PRELOAD", "PYTHONPATH", "BASH_ENV", "GIT_CONFIG_COUNT", "MB_TESTED_SHA", "JAVA_HOME"):
            with self.subTest(name=name), self.assertRaises(MbError):
                self.environment(values={name: "override"})

    def test_tool_paths_types_values_and_caps_are_strict(self):
        changes = [{"role": []}, {"role": "other"}, {"run_id": True}, {"run_attempt": 0},
                   {"python": "relative/python"}, {"python": "/usr/../bin/python"},
                   {"python": "/opt/python:/malicious/python"}, {"java_home": "/jdk\n"},
                   {"python": "/\ud800"}, {"values": {"E2E_ROW_JSON": "\ud800"}},
                   {"values": {"E2E_ROW_JSON": "\0"}}, {"values": {"E2E_ROW_JSON": True}},
                   {"values": {"LIBGL_ALWAYS_SOFTWARE": "preload"}}, {"values": {"MB_LANE_ID": "../other"}},
                   {"values": {"SOURCE_DATE_EPOCH": "-1"}},
                   {"values": {"E2E_ROW_JSON": "x" * (limits.MAX_CI_ENV_VALUE_BYTES + 1)}},
                   {"values": {"E2E_ROW_JSON": "x" * limits.MAX_CI_ENV_VALUE_BYTES,
                               "E2E_SCENARIOS": "x" * limits.MAX_CI_ENV_VALUE_BYTES}}]
        for change in changes:
            with self.subTest(change=list(change)), self.assertRaises(MbError):
                self.environment(**change)

    def test_a_subject_before_the_plan_gives_the_environment_of_its_complete_identity(self):
        identity = ci_plan()["identity"]
        subject = subject_of(identity)
        self.assertNotEqual(subject, identity)
        self.assertEqual(self.environment(identity=subject), self.environment())
        self.assertEqual(worker.execution_subject(identity), subject)
        self.assertEqual(worker.execution_subject(subject), subject)

    def test_neither_a_partial_identity_nor_a_changed_subject_is_a_subject(self):
        identity = ci_plan()["identity"]
        subject = subject_of(identity)
        plan_field = next(key for key in identity if key not in subject)
        rejected = [None, [], "subject", {}, {key: value for key, value in subject.items() if key != "tested_sha"},
                    {**subject, plan_field: identity[plan_field]}, {**identity, "unknown": "x"},
                    {key: value for key, value in identity.items() if key != plan_field},
                    {**subject, "tested_sha": "not-a-sha"}, {**identity, "policy_sha256": "0"},
                    {**subject, "tested_parents": []}]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(MbError):
                worker.execution_subject(value)
            with self.subTest(environment=value), self.assertRaises(MbError):
                self.environment(identity=value)

    def test_every_jdk_home_of_the_job_is_listed_in_order_behind_java_home(self):
        homes = ["/opt/jdk", "/opt/hostedtoolcache/Java_Temurin/21.0.4/x64", "/usr/lib/jvm/temurin-25-jdk-amd64"]
        for count in (1, 2, 3):
            value = ":".join(homes[:count])
            result = dict(entry.split("=", 1) for entry in self.environment(values={"MB_JAVA_HOMES": value}))
            with self.subTest(count=count):
                self.assertEqual((result["JAVA_HOME"], result["MB_JAVA_HOMES"]), ("/opt/jdk", value))
                self.assertEqual(result["PATH"].split(":")[:2], ["/opt/python/bin", "/opt/jdk/bin"])
        self.assertEqual(worker.JAVA_HOMES_ENVIRONMENT, "MB_JAVA_HOMES")
        self.assertNotIn("MB_JAVA_HOMES", dict(entry.split("=", 1) for entry in self.environment()))
        rejected = ["", "/opt/other", "/opt/other:/opt/jdk", "/opt/jdk:/opt/jdk", "/opt/jdk:", ":/opt/jdk",
                    "/opt/jdk:relative/jdk", "/opt/jdk:/opt/../etc", "/opt/jdk:/opt/jdk21\n", "/opt/jdk /opt/jdk21:x",
                    ":".join(["/opt/jdk", *(f"/opt/jdk-{index}" for index in range(limits.MAX_CI_TOOL_ROOTS))])]
        for value in rejected:
            with self.subTest(value=value[:40]), self.assertRaises(MbError):
                self.environment(values={"MB_JAVA_HOMES": value})
        with self.assertRaises(MbError):  # A job without a JDK has no list either.
            self.environment(java_home=None, values={"MB_JAVA_HOMES": "/opt/jdk"})

    def test_reviewed_runtime_inputs_are_passed_without_shell_interpolation(self):
        values = {"E2E_ROW_JSON": '{"data":"$(touch /outside)"}', "E2E_SCENARIOS": "example/server",
                  "LIBGL_ALWAYS_SOFTWARE": "1", "MB_LANE_ID": "lane-a", "SOURCE_DATE_EPOCH": "123"}
        result = dict(entry.split("=", 1) for entry in self.environment(values=values))
        self.assertEqual({key: result[key] for key in values}, values)


class AccountTests(unittest.TestCase):
    def authenticate(self, record=None, *, groups=None, runner_uid=1001):
        record = record or SimpleNamespace(pw_name="modbase_candidate", pw_uid=2000, pw_gid=2000,
                                          pw_dir=ACCOUNT.home)
        module = SimpleNamespace(getpwnam=lambda name: record)
        with patch.object(worker.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
             patch.object(worker.os, "getuid", return_value=runner_uid, create=True), \
             patch.object(worker.os, "getgid", return_value=1001, create=True), \
             patch.object(worker.os, "getgrouplist", return_value=[2000] if groups is None else groups, create=True):
            return worker.authenticate_worker_account("candidate")

    def test_fixed_passwd_account_and_private_home_are_bound(self):
        self.assertEqual(self.authenticate(), ACCOUNT)

    def test_privileged_runner_groups_and_wrong_home_are_rejected(self):
        for change in ({"pw_uid": 0}, {"pw_gid": 0}, {"pw_uid": 1001}, {"pw_gid": 1001},
                       {"pw_dir": "/root"}, {"pw_name": "another-user"}):
            fields = {"pw_name": "modbase_candidate", "pw_uid": 2000, "pw_gid": 2000, "pw_dir": ACCOUNT.home}
            fields.update(change)
            with self.subTest(change=change), self.assertRaises(MbError):
                self.authenticate(SimpleNamespace(**fields))
        with self.assertRaises(MbError):
            self.authenticate(groups=[2000, 27])

    def test_nonlinux_and_unknown_roles_fail_before_account_access(self):
        with patch.object(worker.sys, "platform", "win32"), self.assertRaises(MbError):
            worker.authenticate_worker_account("candidate")
        for role in (None, [], "runner"):
            with self.assertRaises(MbError):
                worker.authenticate_worker_account(role)
            with self.assertRaises(MbError):
                worker.worker_account_exists(role)
            with self.assertRaises(MbError):
                worker.lock_worker_account(role)
        with patch.object(worker.sys, "platform", "win32"), self.assertRaises(MbError):
            worker.worker_account_exists("candidate")

    def passwd(self, *names, failure=None):
        def getpwnam(name):
            if failure is not None:
                raise failure
            if name not in names:
                raise KeyError(name)
            return SimpleNamespace(pw_name=name)
        return patch.dict(sys.modules, {"pwd": SimpleNamespace(getpwnam=getpwnam)})

    def test_existence_is_the_passwd_entry_alone_and_a_failed_lookup_is_not_absence(self):
        with patch.object(worker.sys, "platform", "linux"):
            with self.passwd("modbase_validator"):
                self.assertTrue(worker.worker_account_exists("validator"))
                self.assertFalse(worker.worker_account_exists("candidate"))
            with self.passwd(failure=OSError("passwd unreadable")), self.assertRaises(MbError):
                worker.worker_account_exists("candidate")

    def peer(self, account, *, existing, live=None, runner=(1001, 1001)):
        validator = worker.WorkerAccount("validator", 2001, 2001, str(worker.WORKER_ROOT / "validator-home"))
        live = {"candidate": ACCOUNT, "validator": validator, **(live or {})}
        self.authenticated = []
        def authenticate(role):
            self.authenticated.append(role)
            return live[role]
        with patch.object(worker, "worker_account_exists", side_effect=lambda role: role in existing), \
                patch.object(worker, "authenticate_worker_account", side_effect=authenticate):
            return worker.authenticate_peer_account(account, runner_uid=runner[0], runner_gid=runner[1])

    def test_peer_is_the_other_account_or_none_in_a_job_that_allocated_one(self):
        validator = worker.WorkerAccount("validator", 2001, 2001, str(worker.WORKER_ROOT / "validator-home"))
        self.assertEqual(self.peer(validator, existing={"candidate", "validator"}), ACCOUNT)
        self.assertEqual(self.authenticated, ["candidate"])
        self.assertEqual(self.peer(ACCOUNT, existing={"candidate", "validator"}), validator)
        self.assertEqual(self.authenticated, ["validator"])
        self.assertIsNone(self.peer(validator, existing={"validator"}))
        self.assertEqual(self.authenticated, [])  # An absent peer is never looked up as an account.

    def test_peer_and_runner_must_differ_in_user_and_group_from_every_account(self):
        validator = worker.WorkerAccount("validator", 2001, 2001, str(worker.WORKER_ROOT / "validator-home"))
        both = {"candidate", "validator"}
        clashes = [dict(existing=both, live={"candidate": worker.WorkerAccount("candidate", 2001, 2000, ACCOUNT.home)}),
                   dict(existing=both, live={"candidate": worker.WorkerAccount("candidate", 2000, 2001, ACCOUNT.home)}),
                   dict(existing=both, runner=(2000, 1001)), dict(existing=both, runner=(1001, 2000)),
                   dict(existing=both, runner=(2001, 1001)), dict(existing={"validator"}, runner=(1001, 2001))]
        for clash in clashes:
            with self.subTest(clash=clash), self.assertRaisesRegex(MbError, "not isolated"):
                self.peer(validator, **clash)
        for forged in (None, ("validator", 2001, 2001), worker.WorkerAccount("runner", 2001, 2001, "/home/runner")):
            with self.subTest(forged=forged), self.assertRaises(MbError):
                self.peer(forged, existing=both)


class TerminationTests(unittest.TestCase):
    """Legacy command-shape checks; real revocation is tests.ci_linux_deferred."""

    @staticmethod
    def inactive(command):
        return b"inactive\ninactive\ninactive\n" if "--property=ActiveState" in command else b""

    def test_double_real_effective_sweeps_retry_then_lock_and_expire(self):
        queries = iter([b"123\n", b"", b"", b"", b"", b""])
        def control(command, **kwargs):
            return next(queries) if command[0] == "/usr/bin/pgrep" else self.inactive(command)
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
             patch.object(worker, "_control", side_effect=control) as run, patch.object(worker.time, "sleep"):
            worker.terminate_worker(ACCOUNT)
        commands = [call.args[0] for call in run.call_args_list]
        kills = [command for command in commands if "/usr/bin/pkill" in command]
        self.assertEqual(kills[:4], [
            ("/usr/bin/sudo", "-n", "/usr/bin/pkill", "-KILL", flag, "2000") for _ in range(2) for flag in ("-u", "-U")])
        self.assertEqual(sum(command[0] == "/usr/bin/pgrep" for command in commands), 6)
        self.assertIn(("/usr/bin/sudo", "-n", "/usr/sbin/usermod", "--lock", "--expiredate",
                       "1970-01-02", "modbase_candidate"), commands)
        self.assertEqual(commands[-2:], [("/usr/bin/pgrep", flag, "2000") for flag in ("-u", "-U")])

    def test_process_appearing_during_lock_is_killed_before_return(self):
        state = {"locked": False, "alive": False, "post_lock_kills": 0}
        def control(command, **kwargs):
            if "--lock" in command:
                state.update(locked=True, alive=True)
            elif "/usr/bin/pkill" in command and state["locked"]:
                state["alive"] = False
                state["post_lock_kills"] += 1
            elif command[0] == "/usr/bin/pgrep":
                return b"123\n" if state["alive"] else b""
            return self.inactive(command)
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                patch.object(worker, "_control", side_effect=control):
            worker.terminate_worker(ACCOUNT)
        self.assertTrue(state["locked"])
        self.assertFalse(state["alive"])
        self.assertEqual(state["post_lock_kills"], 4)

    def test_post_lock_query_failure_forbids_success(self):
        def control(command, **kwargs):
            if command[0] == "/usr/bin/pgrep" and control.locked:
                raise worker.WorkerError("post-lock observation failed")
            if "--lock" in command:
                control.locked = True
            return self.inactive(command)
        control.locked = False
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                patch.object(worker, "_control", side_effect=control), self.assertRaises(MbError):
            worker.terminate_worker(ACCOUNT)
        self.assertTrue(control.locked)

    def test_surviving_uid_is_fatal_but_still_locked(self):
        clock = [0.0]
        def control(command, **kwargs):
            return b"123\n" if command[0] == "/usr/bin/pgrep" else self.inactive(command)
        def sleep(duration):
            clock[0] += duration
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
             patch.object(worker, "_control", side_effect=control) as run, \
             patch.object(worker.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(worker.time, "sleep", side_effect=sleep), self.assertRaises(MbError):
            worker.terminate_worker(ACCOUNT)
        self.assertTrue(any("--lock" in call.args[0] for call in run.call_args_list))
        self.assertLessEqual(clock[0], 2 * limits.CI_TERMINATION_GRACE_SECONDS)

    def test_control_failure_still_locks_and_failed_lock_is_fatal(self):
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
             patch.object(worker, "_control", side_effect=[worker.WorkerError("kill failed"), b""] + [b""] * 30) as run, \
             self.assertRaises(MbError):
            worker.terminate_worker(ACCOUNT)
        self.assertTrue(any("--lock" in call.args[0] for call in run.call_args_list))
        def lock_failure(command, **kwargs):
            if "--lock" in command:
                raise worker.WorkerError("lock failed")
            return self.inactive(command)
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
             patch.object(worker, "_control", side_effect=lock_failure), \
             self.assertRaises(MbError):
            worker.terminate_worker(ACCOUNT)

    def test_changed_passwd_identity_cannot_kill_a_reassigned_uid(self):
        other = worker.WorkerAccount("candidate", 2001, 2001, ACCOUNT.home)
        with patch.object(worker, "authenticate_worker_account", return_value=other), \
             patch.object(worker, "_control") as run, self.assertRaises(MbError):
            worker.terminate_worker(ACCOUNT)
        run.assert_not_called()
        with patch.object(worker, "authenticate_worker_account", return_value=other), \
             patch.object(worker, "_control") as run, self.assertRaises(MbError):
            worker.worker_processes(ACCOUNT)
        run.assert_not_called()

    def test_liveness_asks_for_running_states_by_effective_and_real_user_and_signals_nothing(self):
        for answers, alive in (([b"", b""], False), ([b"123\n"], True), ([b"", b"77\n"], True)):
            with self.subTest(answers=answers), \
                    patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
                    patch.object(worker, "_control", side_effect=list(answers)) as run:
                self.assertIs(worker.worker_processes(ACCOUNT), alive)
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands, [("/usr/bin/pgrep", "--runstates", "DIKPRSTWt", flag, "2000")
                                        for flag in ("-u", "-U")][:len(answers)])
            self.assertNotIn("Z", commands[0][2])  # A zombie runs nothing.
            self.assertTrue(all(call.kwargs["accepted"] == frozenset({0, 1}) for call in run.call_args_list))

    def test_lock_by_name_runs_one_usermod_and_requires_its_success(self):
        with patch.object(worker, "_control", return_value=b"") as run:
            worker.lock_worker_account("validator")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ("/usr/bin/sudo", "-n", "/usr/sbin/usermod", "--lock",
                                                 "--expiredate", "1970-01-02", "modbase_validator"))
        self.assertEqual(run.call_args.kwargs["accepted"], frozenset({0}))


class ControlPipeTests(unittest.TestCase):
    """Check allocation/reaping behavior; mocked pipes do not establish Linux account isolation."""

    def control(self, chunks, *, status=0, ready=True, cwd=None):
        process = MagicMock()
        process.poll.return_value = status
        process.wait.return_value = status
        selector = MagicMock()
        selector.select.return_value = [(None, None)] if ready else []
        with patch.object(worker.subprocess, "Popen", return_value=process) as launch, \
             patch.object(worker.selectors, "DefaultSelector") as create, \
             patch.object(worker.os, "set_blocking", create=True), patch.object(worker.os, "read", side_effect=chunks):
            create.return_value.__enter__.return_value = selector
            try:
                return worker._control(("/usr/bin/pgrep", "-u", "2000"), timeout=15,
                                       accepted=frozenset({0, 1}), **({"cwd":cwd} if cwd is not None else {}))
            finally:
                process.stdout.close.assert_called_once()
                process.wait.assert_called()
                self.assertEqual(launch.call_args.kwargs["env"], worker._HOST_ENV)
                self.assertNotIn("shell", launch.call_args.kwargs)
                if cwd is None:self.assertNotIn("cwd",launch.call_args.kwargs)
                else:self.assertEqual(launch.call_args.kwargs["cwd"],cwd)

    def test_bounded_pid_output_and_exit_status(self):
        self.assertEqual(self.control([b"123\n", b""]), b"123\n")
        self.assertEqual(self.control([b""], status=1), b"")
        with self.assertRaises(MbError):
            self.control([b""], status=2)

    def test_oversize_or_stalled_pipe_is_fatal_and_reaped(self):
        with self.assertRaises(MbError):
            self.control([b"x" * (limits.MAX_CI_CONTROL_OUTPUT_BYTES + 1)])
        with self.assertRaises(MbError):
            self.control([], ready=False)

    def test_fixed_privileged_cwd_preserves_clean_environment_and_reaping(self):
        self.assertEqual(self.control([b""],cwd=worker.WORKER_ROOT/"privileged-kit"),b"")


class ExecutionTests(unittest.TestCase):
    def execute(self, *, chunks=(b"native-output\n", b""), code=0, command=None, ready=True,
                clock=None, termination_error=None, read_error=None, launch_error=None, abandoned=False,
                liveness_error=None, late_exit=False):
        process = MagicMock()
        process.poll.return_value = None if late_exit else code
        process.wait.return_value = code
        events = []
        selector = MagicMock()
        selector.select.return_value = [(None, None)] if ready else []
        argv = command if command is not None else (
            "/opt/python/bin/python3", "-I", "-B", str(worker.WORKER_ROOT / "repository/scripts/ci/dispatch.py"), "build-target")
        read = read_error if read_error is not None else list(chunks)
        def observe(account):
            events.append("observe")
            if liveness_error is not None:
                raise liveness_error
            return abandoned
        def kill(account):
            events.append("terminate")
            if termination_error is not None:
                raise termination_error
        with patch.object(worker, "authenticate_worker_account", return_value=ACCOUNT), \
             patch.object(worker, "worker_processes", side_effect=observe), \
             patch.object(worker, "terminate_worker", side_effect=kill) as terminate, \
             patch.object(worker.subprocess, "Popen", return_value=process, side_effect=launch_error) as launch, \
             patch.object(worker.selectors, "DefaultSelector") as create, \
             patch.object(worker.os, "set_blocking", create=True), patch.object(worker.os, "read", side_effect=read), \
             patch.object(worker.signal, "signal", return_value="previous-handler") as signals, \
             patch.object(worker.time, "monotonic", side_effect=clock, return_value=0):
            create.return_value.__enter__.return_value = selector
            try:
                result = worker.execute_worker(
                    ACCOUNT, command=argv, python="/opt/python/bin/python3", java_home=None,
                    identity=ci_plan()["identity"], run_id=42, run_attempt=2, values={}, timeout_seconds=1)
                arguments = launch.call_args.args[0]
                self.assertEqual(arguments[:9], ("/usr/bin/sudo", "-n", "--user", "#2000", "--",
                                                "/usr/bin/setpriv", "--no-new-privs", "--", "/usr/bin/env"))
                self.assertIn("-i", arguments)
                self.assertEqual(arguments[-len(argv):], argv)
                # The account's own process sets the private umask and then becomes the dispatcher.
                wrapper = arguments[-len(argv) - 6:-len(argv)]
                self.assertEqual(wrapper[:5], ("/opt/python/bin/python3", "-I", "-S", "-B", "-c"))
                self.assertIn("os.umask(0o077)", wrapper[5])
                self.assertIn("os.execv(sys.argv[1],sys.argv[1:])", wrapper[5])
                self.assertIn("=", arguments[-len(argv) - 7])  # Directly after the closed environment.
                # What the hook left behind is looked for once, before the sweep that would hide it.
                self.assertEqual(events, ["observe", "terminate"])
                self.assertEqual(launch.call_args.kwargs["env"], worker._HOST_ENV)
                self.assertIn(f"--chdir={worker.WORKER_ROOT / 'repository'}", arguments)
                self.assertEqual(launch.call_args.kwargs["cwd"].as_posix(), str(worker.WORKER_ROOT))
                self.assertIs(launch.call_args.kwargs["close_fds"], True)
                return result
            finally:
                if termination_error is not None:
                    self.assertEqual(terminate.call_args_list, [unittest.mock.call(ACCOUNT)] * 2)
                else:
                    terminate.assert_called_once_with(ACCOUNT)
                self.assertLessEqual(events.count("observe"), 1)
                calls = [call.args for call in signals.call_args_list]
                if calls:
                    self.assertIn((worker.signal.SIGTERM, worker.signal.SIG_IGN), calls)
                    self.assertEqual(calls[-1], (worker.signal.SIGTERM, "previous-handler"))
                if launch.called and launch_error is None:
                    process.stdout.close.assert_called_once()
                    process.wait.assert_called()

    def test_success_returns_only_after_account_termination_and_launcher_reap(self):
        result = self.execute()
        self.assertEqual(result, worker.WorkerResult(0, b"native-output\n", False))

    def test_a_dispatcher_that_left_a_process_behind_fails_after_its_uid_was_swept(self):
        for late_exit in (False, True):  # The exit is seen in the loop, or only after the output ended.
            with self.subTest(late_exit=late_exit), \
                    self.assertRaisesRegex(worker.WorkerExecutionError, "left a process behind") as caught:
                self.execute(abandoned=True, late_exit=late_exit)
            self.assertEqual(caught.exception.result, worker.WorkerResult(0, b"native-output\n", False))
        self.assertEqual(self.execute(late_exit=True), worker.WorkerResult(0, b"native-output\n", False))
        with self.assertRaisesRegex(worker.WorkerExecutionError, "returned failure"):
            self.execute(abandoned=True, code=3)  # A failed hook is reported as failed, whatever it left.

    def test_a_failed_liveness_query_is_no_success_and_the_uid_is_still_swept(self):
        with self.assertRaises(worker.WorkerExecutionError):
            self.execute(liveness_error=worker.WorkerError("pgrep failed"))

    def test_failed_dispatcher_preserves_bounded_diagnostics_and_never_returns_success(self):
        with self.assertRaises(worker.WorkerExecutionError) as caught:
            self.execute(code=17)
        self.assertEqual(caught.exception.result.returncode, 17)
        self.assertEqual(caught.exception.result.log, b"native-output\n")

    def test_capture_truncates_and_keeps_draining(self):
        with patch.object(worker.lim, "MAX_CI_LOG_BYTES", 4):
            result = self.execute(chunks=(b"12345", b"more-data", b""))
        self.assertEqual(result.log, b"1234")
        self.assertTrue(result.truncated)

    def test_timeout_and_signal_interruption_terminate_the_account(self):
        with self.assertRaisesRegex(worker.WorkerExecutionError, "timed out"):
            self.execute(code=None, ready=False, clock=[0, 0, 2])
        with self.assertRaisesRegex(worker.WorkerExecutionError, "timed out"):
            self.execute(code=0, clock=[0, 2])  # A late observed exit cannot reset an expired deadline.
        def interrupted(_fd, _size):
            raise worker.WorkerError("worker interrupted by signal 15")
        with self.assertRaisesRegex(worker.WorkerExecutionError, "interrupted"):
            self.execute(read_error=interrupted)

    def test_launch_read_and_cleanup_failures_are_fatal(self):
        for arguments in ({"launch_error": OSError("cannot launch")}, {"read_error": OSError("bad pipe")},
                          {"termination_error": worker.WorkerError("UID survived")}):
            with self.subTest(arguments=list(arguments)), self.assertRaises(MbError):
                self.execute(**arguments)

    def test_malformed_dispatchers_cannot_select_other_import_roots_or_shell_programs(self):
        python = "/opt/python/bin/python3"
        for command in (("/bin/bash", "-c", "echo bad"), (python, "-c", "arbitrary"),
                        (python, "-I", "/outside/scripts/ci/dispatch.py"),
                        (python, "-I", "-B", "/outside/scripts/ci/dispatch.py"),
                        (python, "-I", "-B", str(worker.WORKER_ROOT / "controller/scripts/ci/dispatch.py")),
                        (python, "-I", "-B", str(worker.WORKER_ROOT / "repository/scripts/ci/../dispatch.py")),
                        (python, "-I", "-B", "/bad\0path"),
                        (python, "-I", "-B", str(worker.WORKER_ROOT / "repository/scripts/ci/dispatch.py"), "\ud800")):
            with self.subTest(command=command[:2]), self.assertRaises(MbError):
                self.execute(command=command)


class WorkerLogTests(unittest.TestCase):
    def test_commands_terminal_escapes_binary_bytes_and_cr_newlines_are_inert(self):
        result = worker.WorkerResult(0, b"::error::candidate annotation\n\x1b[31mred\x7f\r::set-output name=x::y\n"
                                   b"##[error]legacy\n::::error::::nested\n\xff", False)
        output = worker.render_worker_log(result, role="candidate")
        self.assertTrue(all(line.startswith("[candidate] ") for line in output.splitlines()))
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\x7f", output)
        self.assertIn("[candidate] : :error: :candidate annotation", output)
        self.assertNotIn("::", output)
        self.assertNotIn("##[", output)
        self.assertIn("\ufffd", output)

    def test_c1_controls_and_truncation_marker_are_bounded_and_prefixed(self):
        output = worker.render_worker_log(worker.WorkerResult(0, "\u009b31m\n".encode(), True), role="validator")
        self.assertNotIn("\u009b", output)
        self.assertIn("[validator] ?31m", output)
        self.assertIn("[validator] output exceeded", output)
        with self.assertRaises(MbError):
            worker.render_worker_log(worker.WorkerResult(0, b"x" * (limits.MAX_CI_LOG_BYTES + 1), False), role="candidate")


if __name__ == "__main__":
    unittest.main()
