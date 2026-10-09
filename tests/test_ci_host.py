"""Closed host-layout admission and private home-fence regressions.

Syscall mocks here are protocol tests; the required Linux fixture exercises actual UID access.
"""

import os
import shutil
import stat
import subprocess
import sys
import time
import tempfile
import unittest
from pathlib import Path
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

    def protect(self, stamps, *, passwd_home="/home/runner", passwd_gid=121, uid=1001, gid=121, call=None):
        module = SimpleNamespace(getpwuid=lambda value: SimpleNamespace(pw_dir=passwd_home, pw_gid=passwd_gid))
        with patch.object(host.sys, "platform", "linux"), patch.dict(sys.modules, {"pwd": module}), \
                patch.object(host.os, "getuid", return_value=uid, create=True), \
                patch.object(host.os, "getgid", return_value=gid, create=True), \
                patch.object(host, "_open_directory", side_effect=[10, 11, 12]) as opening, \
                patch.object(host.os, "fstat", side_effect=stamps), \
                patch.object(host.os, "fchmod", create=True) as chmod, \
                patch.object(host.os, "fsync"), patch.object(host.os, "close") as close:
            try:
                boundary = (call or host.protect_worker_host)(**ARGS)
            finally:
                # Whatever happened, every directory that was opened is closed again.
                self.assertEqual(close.call_count, opening.call_count)
        if call is None:
            chmod.assert_called_once_with(10, 0o700)
        else:
            chmod.assert_not_called()
        return boundary

    def test_fence_binds_exact_runner_inode_and_closes_traversal(self):
        self.assertEqual(self.protect([metadata(mode=0o755), metadata(), metadata(), metadata()]), BOUNDARY)

    def test_inspection_returns_the_same_receipt_without_touching_the_home(self):
        self.assertEqual(self.protect([metadata(mode=0o755), metadata(), metadata()],
                                      call=host.inspect_worker_host), BOUNDARY)
        self.assertEqual(self.protect([metadata(mode=0o750), metadata(), metadata()], call=host.inspect_worker_host),
                         host.HostBoundary("/home/runner", 1001, 121, 1, 10, 0o750))

    def test_home_group_passwd_group_and_process_group_must_be_one(self):
        # Every later receipt check compares these three; a host where they differ is refused first.
        for stamps, extra in (([metadata(group=118)], {}), ([metadata()], {"passwd_gid": 118}),
                              ([metadata()], {"gid": 118})):
            for call in (None, host.inspect_worker_host):
                with self.subTest(extra=extra, call=call), self.assertRaisesRegex(MbError, "group"):
                    self.protect(list(stamps), call=call, **extra)

    def restore(self, info, *, boundary=BOUNDARY, uid=1001, after=None):
        with patch.object(host.sys, "platform", "linux"), \
                patch.object(host.os, "getuid", return_value=uid, create=True), \
                patch.object(host.os, "getgid", return_value=121, create=True), \
                patch.object(host, "_open_directory", return_value=10) as opening, \
                patch.object(host.os, "fstat", side_effect=[info, after or metadata(mode=boundary.original_mode)]), \
                patch.object(host.os, "fchmod", create=True) as chmod, \
                patch.object(host.os, "fsync"), patch.object(host.os, "close") as close:
            try:
                host.restore_worker_host(boundary)
            finally:
                self.assertEqual(close.call_count, opening.call_count)
        opening.assert_called_once_with(("home", "runner"))
        return chmod

    def test_restore_gives_the_recorded_home_its_mode_back_and_may_run_twice(self):
        self.restore(metadata()).assert_called_once_with(10, 0o755)
        self.restore(metadata(mode=0o755)).assert_called_once_with(10, 0o755)

    def test_restore_refuses_another_directory_owner_mode_or_runner(self):
        for info in (metadata(inode=11), metadata(owner=2000), metadata(group=2000), metadata(mode=0o711)):
            with self.subTest(info=info), self.assertRaises(MbError):
                self.restore(info)
        with self.assertRaises(MbError):
            self.restore(metadata(), after=metadata())  # The mode did not take.
        with self.assertRaisesRegex(MbError, "different runner"):
            self.restore(metadata(), uid=2000)
        with patch.object(host.sys, "platform", "linux"), patch.object(host, "_open_directory") as untouched, \
                self.assertRaises(MbError):
            host.restore_worker_host(host.HostBoundary("/home/runner", 0, 121, 1, 10, 0o755))
        untouched.assert_not_called()

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


@unittest.skipUnless(sys.platform == "linux" and all(map(os.path.exists, ("/usr/bin/find", "/usr/bin/setfacl"))),
                     "the fence runs GNU find, chmod and setfacl")
class HostFenceCommandTests(unittest.TestCase):
    """The fence's two real commands over a hosted-like tree the current user owns.

    The root-only composition over the fixed image trees runs in ``tests/ci_linux_worker.py``.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="mod-base-fence-")
        self.addCleanup(directory.cleanup)
        self.addCleanup(lambda: subprocess.run(("/usr/bin/chmod", "-R", "u+rwX", directory.name), check=False))
        # One level below the private temporary directory: a real fence that proves this host at
        # the same moment never enters it, so it cannot trip over this stand-in.
        self.base = Path(directory.name) / "host"
        self.base.mkdir()
        self.base.chmod(0o755)
        self.tree = self.base / "opt"
        (self.tree / "toolcache/Python/bin").mkdir(parents=True)
        (self.tree / "toolcache/Python/bin/python3").write_bytes(b"elf")
        (self.tree / "toolcache/Python/odd name\n+.py").write_bytes(b"")
        (self.tree / "toolcache/Python/link").symlink_to("bin/python3")
        (self.tree / "toolcache/Ruby").mkdir()
        os.mkfifo(self.tree / "toolcache/pipe")
        for path in sorted(self.tree.rglob("*"), reverse=True):
            if not path.is_symlink():
                path.chmod(0o777)
        (self.tree / "toolcache/Ruby").chmod(0o1777)
        self.tree.chmod(0o777)
        subprocess.run(("/usr/bin/setfacl", "-R", "-d", "-m", "o::rwx", str(self.tree / "toolcache")), check=True)
        self.skip = str(self.base / "boundary")

    def entries(self):
        found, complete = host._reachable_writable_entries(str(self.base), skip=self.skip)
        self.assertTrue(complete)
        return sorted(found)

    def default_acls(self):
        return [str(path) for path in (self.tree, *self.tree.rglob("*")) if path.is_dir() and not path.is_symlink()
                and "system.posix_acl_default" in os.listxattr(path)]

    def test_hosted_like_tree_is_closed_and_a_second_run_changes_nothing(self):
        tree = str(self.tree)
        self.assertEqual(self.entries(), sorted([tree, tree + "/toolcache", tree + "/toolcache/Python",
                                                 tree + "/toolcache/Python/bin", tree + "/toolcache/Python/bin/python3",
                                                 tree + "/toolcache/Python/odd name\n+.py"]))
        self.assertEqual(len(self.default_acls()), 4)
        fresh = self.tree / "toolcache/Python/created-before"
        fresh.mkdir()
        self.assertTrue(fresh.stat().st_mode & stat.S_IWOTH)  # What a default ACL does to new entries.
        host._close_writable_trees((tree,))
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.default_acls(), [])
        for path in (self.tree, *self.tree.rglob("*")):
            if path.is_symlink():
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            if stat.S_ISFIFO(path.lstat().st_mode):
                self.assertEqual(mode, 0o777, path)  # Special files are neither changed nor reported.
            else:
                self.assertEqual(mode & 0o022, 0, path)
        self.assertEqual(stat.S_IMODE((self.tree / "toolcache/Ruby").stat().st_mode), 0o1755)
        self.assertEqual(stat.S_IMODE((self.tree / "toolcache/Python/bin/python3").stat().st_mode), 0o755)
        self.assertEqual((self.tree / "toolcache/Python/bin/python3").read_bytes(), b"elf")
        later = self.tree / "toolcache/Python/created-after"
        later.mkdir()
        self.assertFalse(later.stat().st_mode & stat.S_IWOTH)
        stamps = {path: path.lstat().st_ctime_ns for path in self.tree.rglob("*")}
        host._close_writable_trees((tree,))
        self.assertEqual({path: path.lstat().st_ctime_ns for path in self.tree.rglob("*")}, stamps)

    def test_proof_reports_only_reachable_non_sticky_entries_on_the_same_filesystem(self):
        host._close_writable_trees((str(self.tree),))
        sticky = self.base / "tmp"
        sticky.mkdir()
        sticky.chmod(0o1777)
        (sticky / "inside").mkdir()
        (sticky / "inside").chmod(0o777)
        private = self.base / "home"
        private.mkdir()
        (private / "cache.lock").write_bytes(b"")
        (private / "cache.lock").chmod(0o666)
        (private / "open").mkdir()
        (private / "open").chmod(0o777)
        private.chmod(0o700)
        boundary = self.base / "boundary"
        (boundary / "candidate-home").mkdir(parents=True)
        (boundary / "candidate-home").chmod(0o777)
        loose = self.base / "var/lib/loose"
        loose.mkdir(parents=True)
        (loose / "file").write_bytes(b"x")
        (loose / "file").chmod(0o666)
        (loose / "link").symlink_to("file")
        # Sticky directories are kept but entered; an owner-only directory and the boundary are not.
        self.assertEqual(self.entries(), sorted([str(sticky / "inside"), str(loose / "file")]))
        private.chmod(0o710)
        self.assertEqual(self.entries(), sorted([str(sticky / "inside"), str(loose / "file"),
                                                 str(private / "cache.lock"), str(private / "open")]))
        private.chmod(0o700)
        os.link(private / "cache.lock", loose / "alias")
        self.assertIn(str(loose / "alias"), self.entries())

    def test_closed_sdk_is_not_walked_and_a_writable_hard_link_alias_is_still_rejected(self):
        leaf = self.tree / "toolcache/Python/bin/python3"
        host._close_unused_image_tree(str(self.tree))
        self.assertEqual(stat.S_IMODE(self.tree.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o777)
        host._close_writable_trees((str(self.tree),), closed_trees=(str(self.tree),))
        self.assertEqual(stat.S_IMODE(leaf.stat().st_mode), 0o777)
        self.assertTrue(self.default_acls())  # The inaccessible contents were not visited.
        self.assertEqual(self.entries(), [])
        alias = self.base / "alias"
        os.link(leaf, alias)
        leaf.chmod(0o660)  # Group write, without other write, is enough for a future ACL identity.
        self.assertEqual(self.entries(), [str(alias)])
        leaf.chmod(0o640)
        self.assertEqual(self.entries(), [])

    def test_other_private_directories_keep_the_original_complete_repair(self):
        self.tree.chmod(0o700)
        host._close_writable_trees((str(self.tree),))
        self.assertEqual(stat.S_IMODE((self.tree / "toolcache/Python/bin/python3").stat().st_mode), 0o755)
        self.assertEqual(self.default_acls(), [])

    def test_closure_never_follows_a_symlink(self):
        link = self.base / "sdk-link"
        link.symlink_to(self.tree, target_is_directory=True)
        with self.assertRaisesRegex(MbError, "non-directory or symlink"):
            host._close_unused_image_tree(str(link))
        self.assertEqual(stat.S_IMODE(self.tree.stat().st_mode), 0o777)

    def test_mount_records_are_bounded_strict_and_decode_kernel_escapes_once(self):
        valid = b"1 0 8:1 / / rw,relatime - ext4 /dev/root rw\n"
        self.assertEqual(host._parse_mounts(valid)[0].root, host.PurePosixPath("/"))
        escaped = b"2 1 8:1 /with\\040space /literal\\134040 ro - ext4 /dev/root rw\n"
        item = host._parse_mounts(escaped)[0]
        self.assertEqual(str(item.root), "/with space")
        self.assertEqual(str(item.point), "/literal\\040")
        self.assertFalse(item.writable)
        for raw in (b"", valid[:-1], valid.replace(b" / ", b" /../ ", 1),
                    valid.replace(b" / ", b" /bad\\777 ", 1), valid.replace(b"relatime", b"\xff"),
                    valid.replace(b"rw,relatime", b"rw,ro"), valid.replace(b" - ", b" "),
                    b"x" * (limits.MAX_CI_HOST_MOUNTINFO_BYTES + 1)):
            with self.subTest(raw=raw[:80]), self.assertRaises(MbError):
                host._parse_mounts(raw)

    def test_mount_alias_proof_maps_the_sdk_through_its_containing_filesystem_root(self):
        raw = (b"1 0 8:1 / / rw - ext4 /dev/root rw\n"
               b"2 1 8:2 /installed /opt rw - ext4 /dev/tools rw\n"
               b"3 1 8:2 /installed/sdk /var/lib/whole rw - ext4 /dev/tools rw\n")
        self.assertTrue(host._admit_closed_tree_mounts("/opt/sdk", os.makedev(8, 2), host._parse_mounts(raw)))
        alias = b"4 1 8:2 /installed/sdk/bin /var/lib/alias rw - ext4 /dev/tools rw\n"
        with self.assertRaisesRegex(MbError, "writable descendant mount alias"):
            host._admit_closed_tree_mounts("/opt/sdk", os.makedev(8, 2), host._parse_mounts(raw + alias))
        for safe in (alias.replace(b" rw -", b" ro -"), alias.replace(b"/var/lib/alias", b"/opt/sdk/alias"),
                     alias.replace(b"8:2", b"8:3")):
            self.assertTrue(host._admit_closed_tree_mounts("/opt/sdk", os.makedev(8, 2), host._parse_mounts(raw + safe)))
        hidden_alias = alias.replace(b"/installed/sdk/bin", b"/private/hard-link").replace(b"/var/lib/alias", b"/dev/shm/alias")
        self.assertFalse(host._admit_closed_tree_mounts("/opt/sdk", os.makedev(8, 2), host._parse_mounts(raw + hidden_alias)))

    def test_mount_points_of_other_filesystems_are_not_root_filesystem_entries(self):
        if not os.path.ismount("/dev/shm") or not stat.S_IMODE(os.stat("/dev/shm").st_mode) & stat.S_IWOTH:
            self.skipTest("no world-writable tmpfs mount to look at")
        found, complete = host._reachable_writable_entries("/dev", skip=self.skip)
        self.assertTrue(complete)
        self.assertNotIn("/dev/shm", found)

    def test_report_is_bounded_and_an_incomplete_listing_is_never_a_clean_result(self):
        wide = self.base / "wide"
        wide.mkdir()
        for index in range(40):
            (wide / f"entry-{index:03d}-{'x' * 100}").write_bytes(b"")
            (wide / f"entry-{index:03d}-{'x' * 100}").chmod(0o666)
        with patch.object(limits, "MAX_CI_HOST_FENCE_REPORT_BYTES", 1024):
            found, complete = host._reachable_writable_entries(str(wide), skip=self.skip)
        self.assertFalse(complete)
        self.assertTrue(0 < len(found) < 40)
        self.assertTrue(all(name.startswith(str(wide) + "/entry-") and name.endswith("x" * 100) for name in found))

    def test_failed_or_missing_command_is_a_rejection_with_its_first_diagnostic(self):
        with self.assertRaisesRegex(MbError, r"command failed: \(no message\) "
                                            r"\(phase=administrative; elapsed=\d+\.\d{2}s\)$"):
            host._fence_command(("/usr/bin/false",))
        with self.assertRaisesRegex(MbError, "could not complete"):
            host._fence_command(("/usr/bin/mod-base-no-such-command",))
        with self.assertRaisesRegex(MbError, r"command failed: \S*find: .*missing.*No such file or directory "
                                            r"\(phase=repair; elapsed=\d+\.\d{2}s\)$"):
            host._close_writable_trees((str(self.base / "missing"),))
        self.assertEqual(host._fence_command(("/usr/bin/printf", "kept")), (b"kept", True))
        # A program the walk runs fails the walk, and both streams are drained while only stdout
        # decides completeness.
        with self.assertRaisesRegex(MbError, r"command failed: \S*rmdir: .*python3.*Not a directory"):
            host._fence_command(("/usr/bin/find", str(self.tree), "-type", "f", "-name", "python3",
                                 "-exec", "/usr/bin/rmdir", "--", "{}", "+"))
        self.assertEqual((self.tree / "toolcache/Python/bin/python3").read_bytes(), b"elf")
        with patch.object(limits, "MAX_CI_HOST_FENCE_REPORT_BYTES", 16):
            self.assertEqual(host._fence_command(("/usr/bin/sh", "-c", "printf %s 0123456789abcdef; printf %64s >&2")),
                             (b"0123456789abcdef", True))
            self.assertEqual(host._fence_command(("/usr/bin/sh", "-c", "printf %s 0123456789abcdefg")),
                             (b"0123456789abcdef", False))

    def test_command_that_outlives_its_bound_is_killed_and_rejected(self):
        for phase in ("repair", "verification"):
            with self.subTest(phase=phase), patch.object(limits, "CI_HOST_FENCE_TIMEOUT_SECONDS", 0.3), \
                    self.assertRaisesRegex(MbError, rf"timed out \(phase={phase}; elapsed=\d+\.\d{{2}}s\)$"):
                host._fence_command(("/usr/bin/sleep", "30"), phase=phase)

    def test_repair_reports_each_real_tree_and_preserves_a_shared_deadline(self):
        other = self.base / "other"
        other.mkdir(mode=0o777)
        timings = {}
        host._close_writable_trees((str(self.tree), str(other)), timings=timings)
        self.assertEqual(set(timings), {str(self.tree), str(other)})
        self.assertTrue(all(type(value) is float and value >= 0 for value in timings.values()))
        with self.assertRaisesRegex(MbError, "timed out .*phase=repair"):
            host._fence_command(("/usr/bin/sleep", "30"), phase="repair", deadline=time.monotonic() - 1)

    def test_verification_command_failure_identifies_its_phase_and_elapsed_time(self):
        unreadable = self.base / "unreadable"
        unreadable.mkdir()
        unreadable.chmod(0o111)  # Reachable by a future account, but find cannot list it.
        try:
            with self.assertRaisesRegex(MbError, r"command failed: .*Permission denied "
                                                r"\(phase=verification; elapsed=\d+\.\d{2}s\)$"):
                host._reachable_writable_entries(str(unreadable), skip=self.skip)
        finally:
            unreadable.chmod(0o700)

    def test_root_only_fence_refuses_the_unprivileged_runner_before_any_command(self):
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            host.fence_worker_host(boundary=BOUNDARY)
        self.assertEqual(stat.S_IMODE(self.tree.stat().st_mode), 0o777)
        self.assertEqual(host.HOST_FENCE_TREES, ("/opt", "/usr/share", "/usr/local", "/usr/lib/jvm", "/var/lib/gems"))
        self.assertTrue(shutil.which("chmod", path="/usr/bin"))
