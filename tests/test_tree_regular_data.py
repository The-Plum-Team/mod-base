"""Empty-data admission and export separation, using explicit descriptor/read seams."""

import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import tree


BOUNDS = dict(max_files=4, max_entries=8, max_total_bytes=10, max_file_bytes=10)


class RegularDataTests(unittest.TestCase):
    def inspect(self, entries, contents, **bounds):
        with ExitStack() as stack:
            stack.enter_context(patch.object(tree, '_open_root', return_value=7))
            stack.enter_context(patch.object(tree, '_parent_descriptor', return_value=8))
            stack.enter_context(patch.object(tree.os, 'close'))
            def walk(fd, parent, *, depth, budget, visit):
                for path, mode, size, links in entries:
                    visit(path, SimpleNamespace(st_mode=mode, st_size=size, st_nlink=links))
            stack.enter_context(patch.object(tree, '_walk', side_effect=walk))
            def stream(fd, name, *, max_bytes, allow_empty, consume):
                self.assertTrue(allow_empty)
                data = contents[name]
                self.assertLessEqual(len(data), max_bytes)
                consume(data)
                return len(data)
            reader = stack.enter_context(patch.object(tree, '_stream_regular', side_effect=stream))
            result = tree.regular_data_records(Path('source'), **{**BOUNDS, **bounds})
            return result, reader.call_count

    def test_empty_files_and_directories_are_data_and_sorted(self):
        result, reads = self.inspect([
            ('empty-dir', stat.S_IFDIR, 0, 2), ('z', stat.S_IFREG, 3, 1),
            ('a', stat.S_IFREG, 0, 1)], {'z': b'abc', 'a': b''})
        self.assertEqual([{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                          for name, data in [('a', b''), ('z', b'abc')]], result)
        self.assertEqual(2, reads)

    def test_case_alias_git_hardlink_and_caps_reject(self):
        for entries, bounds in [
            ([('A', stat.S_IFREG, 0, 1), ('a', stat.S_IFREG, 0, 1)], {}),
            ([('.git/config', stat.S_IFREG, 0, 1)], {}),
            ([('file', stat.S_IFREG, 0, 2)], {}),
            ([('file', stat.S_IFREG, 11, 1)], {}),
            ([('a', stat.S_IFREG, 6, 1), ('b', stat.S_IFREG, 6, 1)], {}),
            ([('a', stat.S_IFREG, 0, 1), ('b', stat.S_IFREG, 0, 1)], {'max_files': 1})]:
            with self.subTest(entries=entries), self.assertRaises(MbError):
                self.inspect(entries, {}, **bounds)

    def test_bad_bounds_do_not_open(self):
        for key in BOUNDS:
            for value in (True, 0, -1, '4'):
                with self.subTest(key=key, value=value), patch.object(tree, '_open_root') as opening:
                    with self.assertRaises(MbError):
                        tree.regular_data_records(Path('source'), **{**BOUNDS, key: value})
                    opening.assert_not_called()

    def test_export_copy_keeps_old_contract_and_data_copy_selects_empty_support(self):
        with patch.object(tree, '_copy_regular_files', return_value=[]) as copy:
            tree.copy_regular_files(Path('source'), 7, **BOUNDS)
            self.assertNotIn('data', copy.call_args.kwargs)
            tree.copy_regular_data_files(Path('source'), 7, **BOUNDS)
            self.assertIs(copy.call_args.kwargs['data'], True)
