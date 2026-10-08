"""Permission-transfer mechanics; real UID/ACL access requires the Linux fixture."""

import errno
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import tree


class HandoffTests(unittest.TestCase):
    def exercise(self, *, foreign=False, drift=False, acl_error=None, root_error=False, source=False, private=False, data=False):
        root = SimpleNamespace(st_dev=1, st_ino=10, st_uid=1001, st_gid=121,
                               st_mode=stat.S_IFDIR | 0o700, st_size=0, st_nlink=2)
        leaf = SimpleNamespace(st_dev=1, st_ino=12, st_uid=2000 if foreign else 1001, st_gid=121,
                               st_mode=stat.S_IFREG | 0o644, st_size=3, st_nlink=1)
        nodes = {10: root, 12: leaf}
        records = [{"path": "payload", "size": 3, "sha256": "a" * 64}]
        if source:
            leaf.st_size = 0
            leaf.st_mode = stat.S_IFREG | 0o755
            records = [{"path": "payload", "size": 0, "sha256": "a" * 64, "git_blob": "b" * 40}]
        if data:
            leaf.st_size = 0
            records = [{"path": "payload", "size": 0, "sha256": "a" * 64}]
        events = []
        def mode(fd, value):
            events.append(("mode", fd, value))
            if root_error and fd == 10 and value == 0o750:
                raise OSError(errno.EPERM, "fixture root failure")
            nodes[fd].st_mode = stat.S_IFMT(nodes[fd].st_mode) | value
        def ownership(fd, uid, gid):
            events.append(("owner", fd, uid, gid))
            nodes[fd].st_uid, nodes[fd].st_gid = uid, gid
        def walk(directory, parent, **kwargs):
            kwargs["visit"]("payload", SimpleNamespace(**vars(leaf)))
        with ExitStack() as stack:
            for name, value in [("_open_root", lambda *args: 10), ("_parent_descriptor", lambda *args: 11),
                                ("validate_tree_entries", lambda *args, **kwargs: None), ("_walk", walk)]:
                stack.enter_context(patch.object(tree, name, side_effect=value))
            stack.enter_context(patch.object(tree, "file_records", side_effect=[records, [] if drift else records]))
            if data:
                stack.enter_context(patch.object(tree, "regular_data_records", side_effect=[records, [] if drift else records]))
            if source:
                observed = [{**record, "mode": "100755"} for record in records]
                after = [] if drift else [{**record, "mode": "100644"} for record in records]
                stack.enter_context(patch.object(tree, "source_records", side_effect=[observed, after]))
            stack.enter_context(patch.object(tree.sys, "platform", "linux"))
            stack.enter_context(patch.object(tree.os, "geteuid", return_value=0, create=True))
            stack.enter_context(patch.object(tree.os, "open", return_value=12))
            stack.enter_context(patch.object(tree.os, "fstat", side_effect=lambda fd: nodes[fd]))
            stack.enter_context(patch.object(tree.os, "close"))
            stack.enter_context(patch.object(tree.os, "fchown", side_effect=ownership, create=True))
            stack.enter_context(patch.object(tree.os, "fchmod", side_effect=mode, create=True))
            stack.enter_context(patch.object(tree.os, "fsync"))
            acl = stack.enter_context(patch.object(tree.os, "removexattr", side_effect=acl_error, create=True))
            try:
                grant = (tree.privatize_regular_data_copy if data and private else tree.grant_regular_data_read_access if data else tree.privatize_tree_copy if private
                         else tree.grant_source_read_access if source else tree.grant_tree_read_access)
                extra = {"tracked_paths": ("payload",)} if source else {}
                group = {"owner_gid" if private else "reader_gid": 2001}
                result = grant(Path("fresh"), **extra, source_owner_uid=1001,
                                                     owner_uid=1001, **group,
                                                     max_files=10, max_entries=20,
                                                     max_total_bytes=100, max_file_bytes=100)
                self.assertEqual(result, records)
                self.assertEqual((root.st_gid, stat.S_IMODE(root.st_mode)), (2001, 0o700 if private else 0o750))
                self.assertEqual((leaf.st_gid, stat.S_IMODE(leaf.st_mode)), (2001, 0o600 if private else 0o640))
                self.assertEqual(events[-1], ("mode", 10, 0o700 if private else 0o750))
                self.assertEqual(acl.call_count, 4)
            except MbError:
                self.assertEqual(stat.S_IMODE(root.st_mode), 0o700)
                raise

    def test_read_group_transfer_exposes_root_last_and_removes_acls(self):
        self.exercise()

    def test_regular_data_read_grant_keeps_empty_files_and_shared_failure_privacy(self):
        self.exercise(data=True)
        for args in ({"foreign": True}, {"drift": True},
                     {"acl_error": OSError(errno.EPERM, "ACL refused")}, {"root_error": True}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(data=True, **args)

    def test_regular_data_private_transfer_keeps_empty_files_and_private_modes(self):
        self.exercise(data=True, private=True)
        for args in ({"foreign": True}, {"drift": True},
                     {"acl_error": OSError(errno.EPERM, "ACL refused")}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(data=True, private=True, **args)

    def test_data_grant_preserves_bounds_and_rejects_unprivileged_setup_before_bytes(self):
        args = dict(source_owner_uid=1001, owner_uid=1001, reader_gid=2001,
                    max_files=4, max_entries=8, max_total_bytes=10, max_file_bytes=10)
        def handoff(root, **kwargs):
            self.assertEqual(kwargs['max_entries'], 8)
            self.assertTrue(kwargs['path_is_safe']('logs/empty.log'))
            self.assertEqual(kwargs['records'](), [])
            return []
        with patch.object(tree, '_grant_read_access', side_effect=handoff), \
                patch.object(tree, 'regular_data_records', return_value=[]) as reading:
            tree.grant_regular_data_read_access(Path('fresh'), **args)
            reading.assert_called_once_with(Path('fresh'), max_files=4, max_entries=8,
                                           max_total_bytes=10, max_file_bytes=10, rule=tree.REPO_PATHS)
        for platform, uid in [('win32', 0), ('linux', 1001)]:
            with patch.object(tree.sys, 'platform', platform), \
                    patch.object(tree.os, 'geteuid', return_value=uid, create=True), \
                    patch.object(tree, 'regular_data_records') as reading, self.assertRaises(MbError):
                tree.grant_regular_data_read_access(Path('unused'), **args)
            reading.assert_not_called()

    def test_each_handoff_admits_entries_under_the_rule_of_its_inventory(self):
        """One definition per handoff: the entry admission is never wider than the inventory."""
        bounds = dict(max_files=4, max_entries=8, max_total_bytes=10, max_file_bytes=10)
        jar = 'libs/Quick Skin - Fabric - 1.21.4-1.0.0.jar'
        cases = [(tree.grant_regular_data_read_access, 'reader_gid', 'regular_data_records', None,
                  ['.github/empty.log'], [jar, '.git/config', 'a/.GIT/x']),
                 (tree.privatize_regular_data_copy, 'owner_gid', 'regular_data_records', None,
                  ['.github/empty.log'], [jar, '.git/config', 'a/.GIT/x']),
                 (tree.grant_regular_data_read_access, 'reader_gid', 'regular_data_records', tree.EXPORT_PATHS,
                  [jar], ['.github/empty.log', 'libs/a  b.jar']),
                 (tree.privatize_regular_data_copy, 'owner_gid', 'regular_data_records', tree.SEED_PATHS,
                  [jar, '.git/1.20.1+build.10/a:b~c@d%e'], ['caches/../escape', '/absolute']),
                 (tree.grant_tree_read_access, 'reader_gid', 'file_records', None,
                  ['.git/config', '.nojekyll'], [jar]),
                 (tree.grant_tree_read_access, 'reader_gid', 'file_records', tree.EXPORT_PATHS,
                  [jar], ['.nojekyll', 'libs/trailing.']),
                 (tree.privatize_tree_copy, 'owner_gid', 'file_records', None,
                  ['.github/report.json'], [jar, '.git/config', 'a/.GIT/x']),
                 (tree.privatize_tree_copy, 'owner_gid', 'file_records', tree.EXPORT_PATHS,
                  [jar], ['.github/report.json', 'libs/ leading.jar'])]
        for grant, group, inventory, rule, admitted, refused in cases:
            def handoff(root, **kwargs):
                self.assertEqual([name for name in admitted + refused if kwargs['path_is_safe'](name)], admitted)
                return kwargs['records']()
            with self.subTest(grant=grant.__name__, rule=rule), \
                    patch.object(tree, '_grant_read_access', side_effect=handoff), \
                    patch.object(tree, inventory, return_value=[]) as reading:
                grant(Path('fresh'), source_owner_uid=0, owner_uid=1001, **{group: 2001}, **bounds,
                      **({} if rule is None else {'rule': rule}))
                default = tree.REPO_PATHS if inventory == 'regular_data_records' else tree.BUNDLE_PATHS
                self.assertIs(reading.call_args.kwargs['rule'], default if rule is None else rule)

    def test_private_copy_transfer_keeps_group_other_denied_and_rechecks_content(self):
        self.exercise(private=True)
        for args in ({"foreign": True}, {"drift": True}, {"acl_error": OSError(errno.EPERM, "ACL refused")}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(private=True, **args)

    def test_source_handoff_accepts_empty_files_strips_exec_and_rechecks_blob_bytes(self):
        self.exercise(source=True)
        with self.assertRaises(MbError):
            self.exercise(source=True, drift=True)

    def test_source_inventory_rejects_git_metadata_duplicates_and_malformed_paths_before_io(self):
        for paths in ((), [], ("../bad",), (".git/config",), ("payload", "payload"), (["bad"],)):
            with self.subTest(paths=paths), patch.object(tree, "source_records") as reading, self.assertRaises(MbError):
                tree.grant_source_read_access(Path("unused"), tracked_paths=paths, source_owner_uid=1001,
                                             owner_uid=1001, reader_gid=2001, max_files=10, max_entries=20,
                                             max_total_bytes=100, max_file_bytes=100)
            reading.assert_not_called()

    def test_absent_acls_are_accepted(self):
        self.exercise(acl_error=OSError(errno.ENODATA, "no ACL"))

    def test_foreign_owner_content_drift_acl_failure_and_root_failure_stay_private(self):
        for args in ({"foreign": True}, {"drift": True},
                     {"acl_error": OSError(errno.EPERM, "ACL refused")}, {"root_error": True}):
            with self.subTest(args=list(args)), self.assertRaises(MbError):
                self.exercise(**args)

    def test_non_linux_or_non_root_setup_is_rejected_before_io(self):
        for platform, uid in [("win32", 0), ("linux", 1001)]:
            with patch.object(tree.sys, "platform", platform), \
                    patch.object(tree.os, "geteuid", return_value=uid, create=True), \
                    patch.object(tree, "file_records") as content, self.assertRaises(MbError):
                tree.grant_tree_read_access(Path("unused"), source_owner_uid=1001, owner_uid=1001,
                                            reader_gid=2001, max_files=10, max_entries=20,
                                            max_total_bytes=100, max_file_bytes=100)
            content.assert_not_called()


    def test_boolean_negative_and_privileged_destination_ids_are_rejected(self):
        for changes in ({"source_owner_uid": True}, {"source_owner_uid": -1},
                        {"owner_uid": 0}, {"reader_gid": 0}, {"reader_gid": True},
                        {"owner_uid": 10 ** 100}, {"reader_gid": (1 << 32) - 1}):
            args = dict(source_owner_uid=1001, owner_uid=1001, reader_gid=2001)
            with patch.object(tree.sys, "platform", "linux"), \
                    patch.object(tree.os, "geteuid", return_value=0, create=True), \
                    patch.object(tree, "file_records") as content, self.assertRaises(MbError):
                tree.grant_tree_read_access(Path("unused"), **{**args, **changes}, max_files=10,
                                            max_entries=20, max_total_bytes=100, max_file_bytes=100)
            content.assert_not_called()


class ReadAccessAuthenticationTests(unittest.TestCase):
    def exercise(self, *, mode=0o640, group=2001, links=1, changed_inode=False, acl=False, private=False):
        root = SimpleNamespace(st_dev=1, st_ino=10, st_uid=1001, st_gid=2001,
                               st_mode=stat.S_IFDIR | 0o750, st_size=0, st_nlink=2)
        leaf = SimpleNamespace(st_dev=1, st_ino=12, st_uid=1001, st_gid=group,
                               st_mode=stat.S_IFREG | mode, st_size=0, st_nlink=links)
        if private:
            root.st_mode = stat.S_IFDIR | 0o700
        opened = SimpleNamespace(**vars(leaf))
        if changed_inode:
            opened.st_ino = 99
        def walk(fd, parent, **kwargs):
            kwargs["visit"]("empty.py", leaf)
        with ExitStack() as stack:
            stack.enter_context(patch.object(tree.sys, "platform", "linux"))
            stack.enter_context(patch.object(tree, "_open_root", return_value=10))
            stack.enter_context(patch.object(tree, "_parent_descriptor", return_value=11))
            stack.enter_context(patch.object(tree, "_walk", side_effect=walk))
            stack.enter_context(patch.object(tree.os, "open", return_value=12))
            stack.enter_context(patch.object(tree.os, "close"))
            stack.enter_context(patch.object(tree.os, "fstat", side_effect=lambda fd: root if fd == 10 else opened))
            stack.enter_context(patch.object(tree.os, "getxattr", create=True,
                                             side_effect=None if acl else OSError(errno.ENODATA, "no ACL"),
                                             return_value=b"unexpected ACL"))
            if private:
                tree.authenticate_tree_private_access(Path("ready"), owner_uid=1001, owner_gid=2001, max_entries=10)
            else:
                tree.authenticate_tree_read_access(Path("ready"), owner_uid=1001, reader_gid=2001, max_entries=10)

    def test_exact_read_permissions_allow_empty_regular_files(self):
        self.exercise()

    def test_private_access_accepts_only_exact_owner_private_modes(self):
        self.exercise(private=True, mode=0o600)
        with self.assertRaises(MbError):
            self.exercise(private=True, mode=0o640)

    def test_write_bits_foreign_group_hardlink_inode_drift_and_acls_reject(self):
        for args in ({"mode": 0o660}, {"group": 2000}, {"links": 2}, {"changed_inode": True}, {"acl": True}):
            with self.subTest(args=args), self.assertRaises(MbError):
                self.exercise(**args)

    def test_platform_bad_identity_and_bad_cap_reject_before_open(self):
        for platform, owner, cap in (("win32", 1001, 10), ("linux", True, 10), ("linux", 1001, 0)):
            with patch.object(tree.sys, "platform", platform), patch.object(tree, "_open_root") as opening, self.assertRaises(MbError):
                tree.authenticate_tree_read_access(Path("unused"), owner_uid=owner, reader_gid=2001, max_entries=cap)
            opening.assert_not_called()
