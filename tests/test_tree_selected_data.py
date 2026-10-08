"""Selected empty-data reads never inspect unselected content; descriptor seams are explicit."""

import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import tree
from tests.helpers import ci_stat as info


BOUNDS = dict(max_files=4, max_entries=10, max_total_bytes=100, max_file_bytes=100)


class SelectedDataTests(unittest.TestCase):
    def test_hashes_only_selected_empty_file_after_full_metadata_bound(self):
        with ExitStack() as stack:
            bound = stack.enter_context(patch.object(tree, 'validate_tree_entries'))
            stack.enter_context(patch.object(tree, '_open_root', return_value=7))
            stack.enter_context(patch.object(tree, '_parent_descriptor', return_value=8))
            stack.enter_context(patch.object(tree.os, 'stat', return_value=info(inode=9, mode=stat.S_IFREG|0o600)))
            stack.enter_context(patch.object(tree.os, 'close'))
            def stream(fd, name, *, max_bytes, allow_empty, consume):
                self.assertEqual((fd,name,max_bytes,allow_empty),(8,'selected',0,True));consume(b'');return 0
            reader = stack.enter_context(patch.object(tree, '_stream_regular', side_effect=stream))
            self.assertEqual([{'path':'selected','size':0,'sha256':hashlib.sha256(b'').hexdigest()}],
                             tree.selected_regular_data_records(Path('root'),paths=('selected',),**BOUNDS))
            reader.assert_called_once()
            bound.assert_called_once_with(Path('root'),max_entries=10)

    def test_invalid_paths_or_caps_never_open_or_bound_source_for_read_or_copy(self):
        for options in ({'paths':()}, {'paths':['file']}, {'paths':('file','file')},
                        {'paths':('z','a')}, {'paths':('../outside',)}, {'max_files':True}, {'max_entries':0}):
            arguments={'paths':('file',),**BOUNDS,**options}
            for copying in (False,True):
                with self.subTest(options=options,copying=copying), patch.object(tree,'validate_tree_entries') as bound:
                    with self.assertRaises(MbError):
                        if copying: tree.copy_selected_regular_data_files(Path('root'),7,**arguments)
                        else: tree.selected_regular_data_records(Path('root'),**arguments)
                    bound.assert_not_called()

    def test_selected_copy_uses_empty_data_route_without_changing_export_route(self):
        with patch.object(tree,'_copy_regular_files',return_value=[]) as copy:
            tree.copy_selected_regular_data_files(Path('root'),7,paths=('file',),**BOUNDS)
            self.assertIs(copy.call_args.kwargs['data'],True)
            tree.copy_selected_regular_files(Path('root'),7,paths=('file',),**BOUNDS)
            self.assertNotIn('data',copy.call_args.kwargs)
