"""Source ownership mechanics through descriptor seams; physical evidence requires Linux."""

import errno
import stat
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import tree
from tests.helpers import ci_stat as info


BOUNDS = dict(max_files=4, max_entries=8, max_total_bytes=100, max_file_bytes=100, max_link_bytes=100)


class PrivateSourceTests(unittest.TestCase):
    def exercise(self, *, kind=None):
        nodes = {7: info(inode=7, mode=stat.S_IFDIR | 0o700),
                 8: info(inode=8, mode=stat.S_IFDIR | 0o755),
                 9: info(inode=9, mode=stat.S_IFREG | 0o644),
                 10: info(inode=10, mode=stat.S_IFREG | 0o755, size=3),
                 11: info(inode=11, mode=stat.S_IFLNK | 0o777, size=8)}
        names = {7: ['dir', 'link', 'script'], 8: ['empty']}
        edges = {(7, 'dir'): 8, (7, 'link'): 11, (7, 'script'): 10, (8, 'empty'): 9}
        records = [{'path': p, 'mode': m, 'size': n, 'sha256': 'a'*64, 'git_blob': 'b'*40}
                   for p, m, n in [('dir/empty', '100644', 0), ('link', '120000', 8), ('script', '100755', 3)]]
        events = []
        if kind == 'foreign': nodes[10].st_uid = 2001
        if kind == 'hardlink': nodes[9].st_nlink = 2
        if kind == 'git': names[7].append('.git'); edges[(7, '.git')] = 8
        def change(fd, uid, gid):
            events.append(('owner', fd)); nodes[fd].st_uid, nodes[fd].st_gid = uid, gid
        def mode(fd, value):
            nodes[fd].st_mode = stat.S_IFMT(nodes[fd].st_mode) | value
        def link(name, uid, gid, *, dir_fd, follow_symlinks):
            self.assertIs(follow_symlinks, False)
            self.assertEqual((dir_fd, name), (7, 'link'))
            change(11, uid, gid)
            if kind == 'link-swap': nodes[11].st_ino = 12
        with ExitStack() as stack:
            stack.enter_context(patch.object(tree.sys, 'platform', 'linux'))
            stack.enter_context(patch.object(tree.os, 'geteuid', return_value=0, create=True))
            stack.enter_context(patch.object(tree, 'source_records', side_effect=[records, [] if kind == 'bytes' else records]))
            stack.enter_context(patch.object(tree, '_open_root', return_value=7))
            stack.enter_context(patch.object(tree.os, 'scandir', side_effect=lambda fd:
                nullcontext(iter(SimpleNamespace(name=n) for n in names[fd]))))
            stack.enter_context(patch.object(tree.os, 'stat', side_effect=lambda n, *, dir_fd, follow_symlinks:
                SimpleNamespace(**vars(nodes[edges[(dir_fd, n)]]))))
            stack.enter_context(patch.object(tree.os, 'open', side_effect=lambda n, flags, *, dir_fd: edges[(dir_fd, n)]))
            stack.enter_context(patch.object(tree.os, 'fstat', side_effect=lambda fd: SimpleNamespace(**vars(nodes[fd]))))
            stack.enter_context(patch.object(Path, 'lstat', side_effect=lambda: SimpleNamespace(**vars(nodes[7]))))
            stack.enter_context(patch.object(tree.os, 'fchown', side_effect=change, create=True))
            stack.enter_context(patch.object(tree.os, 'chown', side_effect=link, create=True))
            stack.enter_context(patch.object(tree.os, 'fchmod', side_effect=mode, create=True))
            stack.enter_context(patch.object(tree.os, 'fsync'))
            stack.enter_context(patch.object(tree.os, 'close'))
            stack.enter_context(patch.object(tree.os, 'removexattr', create=True,
                side_effect=OSError(errno.EPERM, 'ACL fixture') if kind == 'acl' else None))
            stack.enter_context(patch.object(tree.os, 'getxattr', create=True, side_effect=OSError(errno.ENODATA, 'no ACL')))
            result = tree.privatize_source_copy(Path('fresh'), tracked_paths=('dir/empty', 'link', 'script'),
                source_owner_uid=0, owner_uid=2001, owner_gid=2001, **BOUNDS)
        self.assertEqual(records, result)
        self.assertEqual(('owner', 7), events[-1])
        self.assertEqual([0o700, 0o700, 0o600, 0o700, 0o777],
                         [stat.S_IMODE(nodes[fd].st_mode) for fd in (7, 8, 9, 10, 11)])
        self.assertTrue(all((n.st_uid, n.st_gid) == (2001, 2001) for n in nodes.values()))

    def test_private_transfer_preserves_exec_empty_and_literal_link_root_last(self):
        self.exercise()

    def test_foreign_hardlink_git_acl_bytes_and_link_swap_reject(self):
        for kind in ('foreign', 'hardlink', 'git', 'acl', 'bytes', 'link-swap'):
            with self.subTest(kind=kind), self.assertRaises(MbError):
                self.exercise(kind=kind)

    def test_invalid_role_identity_and_inventory_do_not_inspect_source(self):
        arguments = dict(tracked_paths=('file',), source_owner_uid=0, owner_uid=2001, owner_gid=2001, **BOUNDS)
        with patch.object(tree.sys, 'platform', 'linux'), patch.object(tree.os, 'geteuid', return_value=0, create=True):
            for extra in ({'owner_uid': True}, {'owner_gid': 0}, {'tracked_paths': ({},)},
                          {'tracked_paths': ('z', 'a')}, {'tracked_paths': ('../out',)}):
                with self.subTest(extra=extra), patch.object(tree, 'source_records') as reader, self.assertRaises(MbError):
                    tree.privatize_source_copy(Path('unopened'), **{**arguments, **extra})
                reader.assert_not_called()
