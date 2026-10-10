"""Streaming read rejects malformed paths/bounds before opening an untrusted tree."""

import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io.tree import stream_child_file


class StreamPreflightTests(unittest.TestCase):
    def test_invalid_paths_bounds_and_consumer_reject_before_any_open(self):
        for relative, maximum, consume in (("../outside", 1, lambda _: None),
                ("payload", True, lambda _: None), ("payload", 0, lambda _: None),
                ("payload", -1, lambda _: None), ("payload", 1, None)):
            with self.subTest(relative=relative, maximum=maximum), \
                    patch("mod_base.io.tree._open_root") as opening, self.assertRaises(MbError):
                stream_child_file(Path("unopened"), relative, max_bytes=maximum, consume=consume)
            opening.assert_not_called()
