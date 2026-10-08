"""Closed host-layout admission and private home-fence regressions.

Syscall mocks here are protocol tests; the required Linux fixture exercises actual UID access.
"""

import stat
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import host, worker
from mod_base.errors import MbError
from mod_base.model import limits


ARGS = dict(runner_environment="github-hosted", runner_home="/home/runner",
            workspace="/home/runner/work/repo/repo", runner_temp="/home/runner/work/_temp")
BOUNDARY = host.HostBoundary("/home/runner", 1001, 121, 1, 10, 0o755)


def metadata(*, inode=10, owner=1001, group=121, mode=0o700):
    return SimpleNamespace(st_dev=1, st_ino=inode, st_uid=owner, st_gid=group, st_mode=stat.S_IFDIR | mode)


class HostFenceTests(unittest.TestCase):
    def privileged(self, *, uid=0, gid=0, home="/home/runner", info=None):
        module = SimpleNamespace(getpwuid=lambda value: SimpleNamespace(pw_uid=1001, pw_gid=121, pw_dir=home))
        with patch.object(host.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(host.os, "getuid", return_value=uid, create=True), \
                patch.object(host.os, "geteuid", return_value=uid, create=True), \
                patch.object(host.os, "getgid", return_value=gid, create=True), \
                patch.object(host, "_open_directory", return_value=10), \
                patch.object(host.os, "fstat", return_value=metadata() if info is None else info), \
                patch.object(host.os, "close"):
            host.authenticate_privileged_host_boundary(BOUNDARY)

    def test_protected_root_rechecks_actual_runner_passwd_and_private_inode(self):
        self.privileged()
        for changes in ({"uid": 1001}, {"gid": 121}, {"home": "/home/other"},
                        {"info": metadata(inode=11)}, {"info": metadata(mode=0o755)},
                        {"info": metadata(owner=2000)}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.privileged(**changes)
    def test_unknown_host_layout_and_hostile_paths_are_rejected_before_filesystem_access(self):
        cases = [{"runner_environment": "self-hosted"}, {"runner_home": "/home/other"},
                 {"workspace": "/tmp/candidate"}, {"runner_temp": "/tmp/runtime"},
                 {"workspace": ARGS["runner_temp"]}, {"runner_temp": "/home/runner/../other"},
                 {"workspace": "/home/runner/work//repo"}, {"workspace": "/home/runner/work/\n"},
                 {"runner_temp": "/home/runner/\ud800"}, {"runner_temp": True},
                 {"workspace": "/home/runner/" + "x" * limits.MAX_CI_TOOL_PATH_BYTES}]
        with patch.object(host.sys, "platform", "linux"), patch.object(host, "_open_directory") as opening:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(MbError):
                    host.protect_worker_host(**{**ARGS, **changes})
        opening.assert_not_called()
        with patch.object(host.sys, "platform", "win32"), self.assertRaises(MbError):
            host.protect_worker_host(**ARGS)

    def protect(self, stamps, *, passwd_home="/home/runner", uid=1001, gid=121):
        module = SimpleNamespace(getpwuid=lambda value: SimpleNamespace(pw_dir=passwd_home))
        with patch.object(host.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(host.os, "getuid", return_value=uid, create=True), \
                patch.object(host.os, "getgid", return_value=gid, create=True), \
                patch.object(host, "_open_directory", side_effect=[10, 11, 12]), \
                patch.object(host.os, "fstat", side_effect=stamps), \
                patch.object(host.os, "fchmod", create=True) as chmod, \
                patch.object(host.os, "fsync"), patch.object(host.os, "close"):
            boundary = host.protect_worker_host(**ARGS)
        chmod.assert_called_once_with(10, 0o700)
        return boundary

    def test_fence_binds_exact_runner_inode_and_closes_traversal(self):
        self.assertEqual(self.protect([metadata(mode=0o755), metadata(), metadata(), metadata()]), BOUNDARY)

    def test_foreign_owner_passwd_home_root_uid_and_postchmod_change_are_rejected(self):
        cases = [([metadata(owner=2000)], {}), ([metadata(), metadata(owner=2000)], {}),
                 ([metadata(), metadata(), metadata(), metadata(inode=11)], {}),
                 ([metadata(), metadata(), metadata(), metadata(mode=0o755)], {}),
                 ([], {"passwd_home": "/home/other"}), ([], {"uid": 0}), ([], {"gid": 0})]
        for stamps, extra in cases:
            with self.subTest(extra=extra, stamps=stamps), self.assertRaises(MbError):
                self.protect(stamps, **extra)

    def test_receipt_recheck_rejects_changed_permissions_inode_or_owner(self):
        for info in [metadata(mode=0o755), metadata(inode=11), metadata(owner=2000), metadata(group=2000)]:
            with self.subTest(info=info), patch.object(host.sys, "platform", "linux"), \
                    patch.object(host.os, "getuid", return_value=1001, create=True), \
                    patch.object(host.os, "getgid", return_value=121, create=True), \
                    patch.object(host, "_open_directory", return_value=10), \
                    patch.object(host.os, "fstat", return_value=info), \
                    patch.object(host.os, "close"), self.assertRaises(MbError):
                host.authenticate_host_boundary(BOUNDARY)
        with patch.object(host.sys, "platform", "linux"), \
                patch.object(host.os, "getuid", return_value=1001, create=True), \
                patch.object(host.os, "getgid", return_value=121, create=True), \
                patch.object(host, "_open_directory", return_value=10), \
                patch.object(host.os, "fstat", return_value=metadata()), patch.object(host.os, "close"):
            host.authenticate_host_boundary(BOUNDARY)

    def test_symlink_parent_and_swapped_directory_are_refused_without_descriptor_leak(self):
        for before, opened in [(SimpleNamespace(st_mode=stat.S_IFLNK | 0o777), None),
                               (metadata(), metadata(inode=11))]:
            with self.subTest(before=before), patch.object(host.os, "O_DIRECTORY", 65536, create=True), \
                    patch.object(host.os, "O_NOFOLLOW", 131072, create=True), \
                    patch.object(host.os, "open", side_effect=[10, 11]), \
                    patch.object(host.os, "stat", return_value=before), \
                    patch.object(host.os, "fstat", return_value=opened), \
                    patch.object(host.os, "close") as close, self.assertRaises(MbError):
                host._open_directory(("home",))
            self.assertEqual([call.args[0] for call in close.call_args_list], [10] if opened is None else [11, 10])

    def test_failed_fence_never_launches_and_terminates_disposable_account(self):
        account = worker.WorkerAccount("candidate", 2000, 2000, "/tmp/private")
        with patch.object(host, "authenticate_host_boundary", side_effect=worker.WorkerError("fence changed")), \
                patch.object(host, "execute_worker") as execute, \
                patch.object(host, "terminate_worker") as terminate, self.assertRaises(MbError):
            host.execute_isolated_worker(account, boundary=BOUNDARY, command=(), python="/opt/python",
                                         java_home=None, identity={}, run_id=42, run_attempt=2,
                                         values={}, timeout_seconds=60)
        execute.assert_not_called()
        terminate.assert_called_once_with(account)

    def test_malformed_receipt_or_root_runner_cannot_admit_dispatch(self):
        cases = [None, host.HostBoundary("/home/other", 1001, 121, 1, 10, 0o755),
                 host.HostBoundary("/home/runner", 1001, 121, True, 10, 0o755),
                 host.HostBoundary("/home/runner", 1001, 121, 1, -1, 0o755),
                 host.HostBoundary("/home/runner", 0, 121, 1, 10, 0o755)]
        with patch.object(host.sys, "platform", "linux"), \
                patch.object(host.os, "getuid", return_value=1001, create=True), \
                patch.object(host.os, "getgid", return_value=121, create=True), \
                patch.object(host, "_open_directory") as opening:
            for boundary in cases:
                with self.subTest(boundary=boundary), self.assertRaises(MbError):
                    host.authenticate_host_boundary(boundary)
        opening.assert_not_called()
