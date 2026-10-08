"""Selected-file append mechanics; real no-follow/inode/partial cleanup fixtures require Linux."""

import copy
import hashlib
import stat
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.io import tree
from mod_base.errors import MbError


class SelectedCopyTests(unittest.TestCase):
    def call(self, *, paths=("a.txt",), drift=False, missing=False, bad_stage=False, zero_write=False):
        payload = b"abc"
        records = [{"path": "a.txt", "size": 3, "sha256": hashlib.sha256(payload).hexdigest()},
                   {"path": "ci-envelope.json", "size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}]
        after = copy.deepcopy(records)
        if drift:
            after[1]["sha256"] = "f" * 64
        if missing:
            records.pop(0)
        stage = SimpleNamespace(st_mode=stat.S_IFDIR | (0o755 if bad_stage else 0o700), st_uid=1001)
        streamed, written = [], []
        def stream(parent, name, *, consume, **kwargs):
            streamed.append(name)
            consume(payload)
            return len(payload)
        def write(fd, data):
            written.append(bytes(data))
            return 0 if zero_write else 1
        with ExitStack() as stack:
            for name in ("validate_tree_entries",):
                stack.enter_context(patch.object(tree, name))
            stack.enter_context(patch.object(tree, "file_records", side_effect=[records, after]))
            stack.enter_context(patch.object(tree, "_open_root", return_value=10))
            stack.enter_context(patch.object(tree, "_parent_descriptor", return_value=11))
            opened = stack.enter_context(patch.object(tree, "_open_new_file", return_value=12))
            stack.enter_context(patch.object(tree, "_stream_regular", side_effect=stream))
            stack.enter_context(patch.object(tree.os, "fstat", return_value=stage))
            stack.enter_context(patch.object(tree.os, "geteuid", return_value=1001, create=True))
            stack.enter_context(patch.object(tree.os, "write", side_effect=write))
            stack.enter_context(patch.object(tree.os, "fchmod", create=True))
            stack.enter_context(patch.object(tree.os, "fsync"))
            stack.enter_context(patch.object(tree.os, "close"))
            scanning = stack.enter_context(patch.object(tree.os, "scandir"))
            observed = tree.copy_selected_regular_files(Path("source"), 99, paths=paths,
                max_files=10, max_entries=20, max_total_bytes=100, max_file_bytes=50)
            opened.assert_called_once_with(99, "a.txt")
            scanning.assert_not_called()
        self.assertEqual(observed, records[:1])
        self.assertEqual(streamed, ["a.txt"])
        self.assertEqual(written, [b"abc", b"bc", b"c"])

    def test_appends_only_selected_bytes_and_handles_short_writes(self):
        self.call()

    def test_unselected_source_drift_is_still_rejected(self):
        with self.assertRaisesRegex(MbError, "changed during"):
            self.call(drift=True)

    def test_missing_path_unsafe_stage_and_zero_progress_reject(self):
        for kwargs in ({"missing": True}, {"bad_stage": True}, {"zero_write": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(MbError):
                self.call(**kwargs)

    def test_bad_selectors_reject_before_source_reads(self):
        for paths in ((), [], ("../bad",), ("a.txt", "a.txt"), ("b.txt", "a.txt"),
                      ("A.txt", "a.txt"), (["a.txt"],)):
            with self.subTest(paths=paths), patch.object(tree, "file_records") as reading, self.assertRaises(MbError):
                tree.copy_selected_regular_files(Path("unused"), 99, paths=paths,
                    max_files=10, max_entries=20, max_total_bytes=100, max_file_bytes=50)
            reading.assert_not_called()
