"""``io.seal``: ported from Block Pops ``tests/test_pages_site_output.py`` (the renderer seals the
actual output bytes) against the generic ``seal_output`` primitive."""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from mod_base.io import seal
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.seal import SealError, seal_output

PAYLOADS = {
    "index.html": b"<!doctype html>\n",
    "assets/site.css": b"body{}\n",
    "assets/gallery.js": b"'use strict';\n",
    "images/aa.webp": b"RIFF-one",
    ".nojekyll": b"\n",
}


def corrupt(stage: int, relative: str) -> None:
    """Flip the last byte in place (same inode and size)."""

    descriptor = os.open(relative, os.O_RDWR | os.O_NOFOLLOW, dir_fd=stage)
    try:
        before = os.fstat(descriptor)
        data = bytearray(os.read(descriptor, before.st_size))
        data[-1] ^= 1
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, data)
    finally:
        os.close(descriptor)


class SealOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / "site"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.sentinel = self.outside / "sentinel"
        self.sentinel.write_bytes(b"preserve")

    def build(self, after_write: Callable[[int, Path], None] | None = None, *, expected: dict[str, bytes] | None = None,
              during_seal: Callable[[int], contextlib.AbstractContextManager] | None = None,
              **bounds) -> tuple[int, int]:
        def writer(stage_path: Path, stage: int) -> tuple[int, int]:
            for relative, data in PAYLOADS.items():
                write_new(stage, relative, data)
            if after_write is not None:
                after_write(stage, stage_path)
            with (during_seal(stage) if during_seal is not None else contextlib.nullcontext()):
                return seal_output(stage, PAYLOADS if expected is None else expected, **bounds)

        return atomic_directory(self.output, writer)

    def test_seals_exactly_the_written_bytes(self) -> None:
        rechecks: list = []

        def writer(_stage_path: Path, stage: int) -> tuple[int, int]:
            for relative, data in PAYLOADS.items():
                write_new(stage, relative, data)
            result = seal_output(stage, PAYLOADS, rechecks=rechecks)
            for recheck in rechecks:
                recheck()
            return result

        self.assertEqual((len(PAYLOADS), sum(map(len, PAYLOADS.values()))), atomic_directory(self.output, writer))
        self.assertEqual(1, len(rechecks))
        self.assertEqual(PAYLOADS["images/aa.webp"], (self.output / "images/aa.webp").read_bytes())

    def test_every_file_is_bound_to_the_bytes_actually_supplied(self) -> None:
        for selected in PAYLOADS:
            with self.subTest(selected=selected):
                with self.assertRaisesRegex(SealError, "bytes differ"):
                    self.build(lambda stage, _path, selected=selected: corrupt(stage, selected))
                self.assertFalse(self.output.exists())

    def test_expected_bytes_that_differ_from_the_file_are_rejected(self) -> None:
        for relative, data in (("index.html", b"<!doctype html>!"), ("index.html", b"short")):
            with self.subTest(data=data), self.assertRaisesRegex(SealError, "differ"):
                self.build(expected={**PAYLOADS, relative: data})
            self.assertFalse(self.output.exists())

    def test_extra_missing_links_specials_and_replaced_entries_are_rejected(self) -> None:
        def mutation(kind: str) -> Callable[[int, Path], None]:
            def apply(stage: int, _path: Path) -> None:
                if kind == "file":
                    write_new(stage, "unreferenced", b"unvalidated")
                elif kind == "directory":
                    os.mkdir("unused", dir_fd=stage)
                elif kind == "empty-subdirectory":
                    os.mkdir("assets/empty", dir_fd=stage)
                elif kind == "symlink":
                    os.symlink(self.sentinel, "foreign", dir_fd=stage)
                elif kind == "fifo":
                    os.mkfifo("fifo", dir_fd=stage)
                elif kind == "hard-link":
                    # The inventory still matches; only the second name outside the stage differs.
                    os.link("index.html", self.outside / "alias", src_dir_fd=stage)
                elif kind == "missing":
                    os.unlink(".nojekyll", dir_fd=stage)
                elif kind == "replacement-directory":
                    os.unlink("index.html", dir_fd=stage)
                    os.mkdir("index.html", dir_fd=stage)
                elif kind == "replacement-link":
                    os.unlink("index.html", dir_fd=stage)
                    os.symlink(self.sentinel, "index.html", dir_fd=stage)
                elif kind == "replacement-file-for-directory":
                    os.unlink("images/aa.webp", dir_fd=stage)
                    os.rmdir("images", dir_fd=stage)
                    write_new(stage, "images", b"RIFF-one")
            return apply

        kinds = ("file", "directory", "empty-subdirectory", "symlink", "hard-link", "missing",
                 "replacement-directory", "replacement-link", "replacement-file-for-directory")
        if hasattr(os, "mkfifo"):
            kinds += ("fifo",)
        for kind in kinds:
            with self.subTest(kind=kind), self.assertRaises(SealError):
                self.build(mutation(kind))
            self.assertFalse(self.output.exists())
            self.assertEqual(b"preserve", self.sentinel.read_bytes())
            for alias in self.outside.glob("alias"):
                alias.unlink()

    def test_stamp_pass_detects_an_earlier_file_changed_while_hashing_a_later_file(self) -> None:
        read = os.read
        changed: list[bool] = []

        def during_seal(stage: int) -> contextlib.AbstractContextManager:
            last = os.stat("index.html", dir_fd=stage).st_ino

            def during_read(descriptor: int, maximum: int) -> bytes:
                data = read(descriptor, maximum)
                if data and not changed and os.fstat(descriptor).st_ino == last:
                    changed.append(True)
                    corrupt(stage, "assets/gallery.js")
                return data

            return patch.object(seal.os, "read", side_effect=during_read)

        with self.assertRaisesRegex(SealError, "changed after byte verification"):
            self.build(during_seal=during_seal)
        self.assertEqual([True], changed)
        self.assertFalse(self.output.exists())

    def test_recheck_detects_a_change_after_sealing(self) -> None:
        rechecks: list = []
        mutations = {
            "rewrite": lambda stage: corrupt(stage, "assets/site.css"),
            "add": lambda stage: write_new(stage, "late", b"x"),
            "remove": lambda stage: os.unlink(".nojekyll", dir_fd=stage),
        }
        for name, mutate in mutations.items():
            rechecks.clear()

            def writer(_stage_path: Path, stage: int, mutate=mutate) -> None:
                for relative, data in PAYLOADS.items():
                    write_new(stage, relative, data)
                seal_output(stage, PAYLOADS, rechecks=rechecks)
                mutate(stage)
                rechecks[0]()

            with self.subTest(mutation=name), self.assertRaises(SealError):
                atomic_directory(self.output, writer)
            self.assertFalse(self.output.exists())

    def test_bounds_are_checked_before_any_file_is_opened(self) -> None:
        opened = []
        real_open = os.open

        def counting_open(*arguments, **keywords):
            opened.append(arguments[0])
            return real_open(*arguments, **keywords)

        for bounds, pattern in (({"max_files": len(PAYLOADS) - 1}, "file-count"),
                                ({"max_bytes": sum(map(len, PAYLOADS.values())) - 1}, "byte bound")):
            with self.subTest(bounds=bounds):
                with self.assertRaisesRegex(SealError, pattern):
                    self.build(during_seal=lambda _stage: patch.object(seal.os, "open", side_effect=counting_open),
                               **bounds)
                self.assertEqual([], opened)

    def test_malformed_expectations_are_rejected(self) -> None:
        for expected in ({"../escape": b"x"}, {"/abs": b"x"}, {"a//b": b"x"}, {"a": "text"},
                         {"a": b"x", "a/b": b"y"}, {"bad name": b"x"}):
            with self.subTest(expected=expected), self.assertRaises(SealError):
                self.build(expected=expected)
            self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
