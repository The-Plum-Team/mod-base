"""``io.atomic_directory``: ported from Block Pops ``tests/test_pages_atomic.py`` and
``tests/test_atomic_directory_traversal.py``, plus both host code paths of the exclusive rename.

Writers must not follow replaced output directories, delete impostors or replace an output."""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import atomic_directory
from mod_base.io.atomic_directory import AtomicDirectoryError, write_new


def publish(output: Path, writer):
    return atomic_directory.atomic_directory(output, writer)


class AtomicDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parent = self.root / "publication"
        self.parent.mkdir()
        self.output = self.parent / "bundle"
        self.held = self.root / "held"
        self.outside = self.root / "unrelated"
        self.outside.mkdir()
        self.sentinel = self.outside / "sentinel"
        self.sentinel.write_bytes(b"preserve")

    def swap_parent(self, stage: Path) -> Path:
        self.parent.rename(self.held)
        self.parent.symlink_to(self.outside, target_is_directory=True)
        impostor = self.outside / stage.name
        impostor.mkdir()
        return impostor

    def assert_preserved(self) -> None:
        self.assertEqual(b"preserve", self.sentinel.read_bytes())
        self.assertFalse((self.outside / "bundle").exists())

    def test_publishes_the_writer_result_and_bytes(self) -> None:
        def writer(stage_path: Path, stage: int) -> str:
            self.assertTrue(stage_path.name.startswith(".bundle.building-"))
            self.assertEqual(self.parent, stage_path.parent)
            self.assertEqual(os.fstat(stage).st_ino, stage_path.lstat().st_ino)
            write_new(stage, "manifest.json", b"{}\n")
            write_new(stage, "images/deep/frame.webp", b"RIFF")
            return "result"

        self.assertEqual("result", publish(self.output, writer))
        self.assertEqual(b"{}\n", (self.output / "manifest.json").read_bytes())
        self.assertEqual(b"RIFF", (self.output / "images/deep/frame.webp").read_bytes())
        self.assertEqual(0o644, stat.S_IMODE((self.output / "manifest.json").stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE((self.output / "images").stat().st_mode))
        self.assertEqual([self.output], list(self.parent.iterdir()))

    def test_creates_missing_parents_privately(self) -> None:
        output = self.root / "missing" / "nested" / "bundle"
        publish(output, lambda _path, stage: write_new(stage, "value", b"x"))
        self.assertEqual(b"x", (output / "value").read_bytes())
        self.assertEqual(0o700, stat.S_IMODE((self.root / "missing").stat().st_mode))

    def test_parent_swap_after_writing_cannot_publish_impostor(self) -> None:
        def writer(stage_path: Path, stage: int) -> None:
            write_new(stage, "manifest.json", b"{}")
            (self.swap_parent(stage_path) / "impostor").write_bytes(b"unvalidated")

        with self.assertRaises(AtomicDirectoryError):
            publish(self.output, writer)
        self.assert_preserved()
        self.assertEqual([b"unvalidated"], [p.read_bytes() for p in self.outside.glob(".bundle.building-*/impostor")])
        self.assertEqual([], list(self.held.iterdir()))

    def test_parent_swap_before_write_never_writes_in_impostor(self) -> None:
        def writer(stage_path: Path, stage: int) -> None:
            self.swap_parent(stage_path)
            write_new(stage, "nested/value", b"owned")

        with self.assertRaises(AtomicDirectoryError):
            publish(self.output, writer)
        self.assert_preserved()
        self.assertEqual([], [p for d in self.outside.glob(".bundle.building-*") for p in d.rglob("*")])
        self.assertEqual([], list(self.held.iterdir()))

    def test_replaced_stage_is_not_published_or_cleaned(self) -> None:
        def writer(stage_path: Path, stage: int) -> None:
            stage_path.rename(self.held)
            stage_path.mkdir()
            (stage_path / "impostor").write_bytes(b"keep")
            write_new(stage, "owned", b"owned")

        with self.assertRaises(AtomicDirectoryError):
            publish(self.output, writer)
        self.assertFalse(self.output.exists())
        self.assertEqual([b"keep"], [p.read_bytes() for p in self.parent.glob(".bundle.building-*/impostor")])
        self.assertEqual([], list(self.held.iterdir()))

    def test_concurrent_destination_is_preserved(self) -> None:
        occupied = []

        def writer(_stage_path: Path, stage: int) -> None:
            write_new(stage, "owned", b"owned")
            self.output.mkdir()
            occupied.append(self.output.stat().st_ino)

        with self.assertRaisesRegex(AtomicDirectoryError, "exclusive output publication failed"):
            publish(self.output, writer)
        self.assertEqual(occupied, [self.output.stat().st_ino])
        self.assertEqual([], list(self.output.iterdir()))
        self.assertEqual([self.output], list(self.parent.iterdir()))

    def test_existing_output_or_unsafe_parent_is_refused_before_the_writer(self) -> None:
        calls = []
        self.output.mkdir()
        with self.assertRaisesRegex(AtomicDirectoryError, "refusing to replace existing output"):
            publish(self.output, lambda *_: calls.append(True))
        (self.parent / "file").write_bytes(b"x")
        with self.assertRaises(AtomicDirectoryError):
            publish(self.parent / "file", lambda *_: calls.append(True))
        with self.assertRaisesRegex(AtomicDirectoryError, "real directory"):
            publish(self.parent / "file" / "bundle", lambda *_: calls.append(True))
        link = self.root / "link"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(AtomicDirectoryError, "real directory"):
            publish(link / "bundle", lambda *_: calls.append(True))
        for name in ("..", "."):
            with self.subTest(name=name), self.assertRaises(AtomicDirectoryError):
                publish(self.parent / name, lambda *_: calls.append(True))
        self.assertEqual([], calls)
        self.assertEqual([self.sentinel], list(self.outside.iterdir()))

    def test_success_failure_and_nested_writer(self) -> None:
        def inner(_stage_path: Path, stage: int) -> None:
            write_new(stage, "value", b"inner")

        def outer(_stage_path: Path, stage: int) -> str:
            publish(self.root / "inner", inner)
            write_new(stage, "manifest.json", b"{}")
            return "result"

        self.assertEqual("result", publish(self.output, outer))
        self.assertEqual(b"inner", (self.root / "inner/value").read_bytes())
        self.assertTrue((self.output / "manifest.json").is_file())

        def failed(stage_path: Path, stage: int) -> None:
            write_new(stage, "owned/file", b"owned")
            (stage_path / "link").symlink_to(self.outside, target_is_directory=True)
            raise OSError("simulated failure")

        with self.assertRaisesRegex(OSError, "simulated"):
            publish(self.parent / "failed", failed)
        self.assertEqual(b"preserve", self.sentinel.read_bytes())
        self.assertEqual([self.sentinel], list(self.outside.iterdir()))
        self.assertEqual([], list(self.parent.glob(".failed.building-*")))
        self.assertFalse((self.parent / "failed").exists())

    def test_writer_errors_propagate_unchanged(self) -> None:
        class WriterError(MbError):
            pass

        def refusing(_stage_path: Path, stage: int) -> None:
            write_new(stage, "partial", b"x")
            raise WriterError("writer refused")

        with self.assertRaisesRegex(WriterError, "writer refused"):
            publish(self.output, refusing)
        self.assertEqual([], list(self.parent.iterdir()))

    def test_unavailable_primitives_fail_before_parent_creation(self) -> None:
        with patch.object(atomic_directory.os, "supports_dir_fd", set()):
            with self.assertRaisesRegex(AtomicDirectoryError, "descriptor-relative"):
                publish(self.root / "missing/bundle", lambda *_: None)
        self.assertFalse((self.root / "missing").exists())
        unsupported = AtomicDirectoryError("output publication requires atomic exclusive directory rename")
        with patch.object(atomic_directory, "_exclusive_directory_rename", side_effect=unsupported):
            with self.assertRaisesRegex(AtomicDirectoryError, "exclusive directory rename"):
                publish(self.root / "missing/bundle", lambda *_: None)
        self.assertFalse((self.root / "missing").exists())


class WriteNewTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stage = self.root / "stage"
        self.stage.mkdir()
        self.fd = os.open(self.stage, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.outside = self.root / "outside"
        self.outside.mkdir()

    def test_writes_exclusive_files_with_publishable_mode(self) -> None:
        write_new(self.fd, "a/b/c.json", b"data")
        write_new(self.fd, "top", bytearray(b"more"))
        self.assertEqual(b"data", (self.stage / "a/b/c.json").read_bytes())
        self.assertEqual(0o644, stat.S_IMODE((self.stage / "top").stat().st_mode))
        with self.assertRaisesRegex(AtomicDirectoryError, "cannot create"):
            write_new(self.fd, "top", b"again")
        self.assertEqual(b"more", (self.stage / "top").read_bytes())

    def test_refuses_non_canonical_paths(self) -> None:
        for relative in ("", "/abs", "../escape", "a/../b", "a//b", "a/./b", "./a", "a/", "a\\b", "c:x", "a\x00b"):
            with self.subTest(relative=relative), self.assertRaisesRegex(AtomicDirectoryError, "canonical relative"):
                write_new(self.fd, relative, b"x")
        with self.assertRaisesRegex(AtomicDirectoryError, "bytes"):
            write_new(self.fd, "text", "not bytes")  # type: ignore[arg-type]
        self.assertEqual([], list(self.stage.iterdir()))

    def test_never_follows_a_symlink_at_any_component(self) -> None:
        (self.stage / "link").symlink_to(self.outside, target_is_directory=True)
        (self.stage / "file-link").symlink_to(self.outside / "target")
        for relative in ("link/file", "link/nested/file", "file-link"):
            with self.subTest(relative=relative), self.assertRaises(AtomicDirectoryError):
                write_new(self.fd, relative, b"x")
        self.assertEqual([], list(self.outside.iterdir()))

    def test_escaped_writes_cannot_reach_outside_the_stage(self) -> None:
        for relative in ("../outside/file", str(self.outside / "file")):
            with self.subTest(relative=relative), self.assertRaises(AtomicDirectoryError):
                write_new(self.fd, relative, b"x")
        self.assertEqual([], list(self.outside.iterdir()))


class TraversalTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    @unittest.skipUnless(hasattr(os, "O_PATH"), "only Linux can hold a directory by path alone")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a search-only directory anyway")
    def test_search_only_ancestor_is_passed_through(self) -> None:
        boundary = self.base / "boundary"
        inner = boundary / "inner"
        inner.mkdir(parents=True)
        boundary.chmod(0o111)
        self.addCleanup(boundary.chmod, 0o755)
        with self.assertRaises(PermissionError):
            os.open(boundary, os.O_RDONLY | os.O_DIRECTORY)
        output = inner / "published"

        def writer(_path: Path, stage: int) -> str:
            write_new(stage, "nested/file.txt", b"written")
            return "done"

        self.assertEqual("done", publish(output, writer))
        self.assertEqual(b"written", (output / "nested/file.txt").read_bytes())

    def test_link_in_the_middle_of_the_path_is_still_refused(self) -> None:
        real = self.base / "real"
        (real / "child").mkdir(parents=True)
        (self.base / "link").symlink_to(real, target_is_directory=True)
        with self.assertRaises(OSError):
            descriptor = atomic_directory._directory_fd(self.base / "link" / "child")
            os.close(descriptor)


class _FakeRenameFunction:
    """A libc rename symbol: records its arguments and fails with ``error`` when set."""

    def __init__(self, error: int = 0) -> None:
        self.calls: list[tuple] = []
        self.error = error
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        self.calls.append(arguments)
        if self.error:
            ctypes.set_errno(self.error)
            return -1
        return 0


class ExclusiveRenameHostTests(unittest.TestCase):
    """Both host code paths (Linux ``renameat2`` and macOS ``renameatx_np``) through the loader seam,
    plus the real primitive of the host running the tests."""

    def rename_for(self, platform: str, symbol: str, function: _FakeRenameFunction):
        library = type("Library", (), {symbol: function})()
        return atomic_directory._exclusive_directory_rename(platform=platform, loader=lambda: library)

    def test_linux_uses_renameat2_with_rename_noreplace(self) -> None:
        function = _FakeRenameFunction()
        self.rename_for("linux", "renameat2", function)(7, "stage", "output")
        self.assertEqual([(7, b"stage", 7, b"output", 1)], function.calls)
        self.assertEqual([ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint],
                         function.argtypes)
        self.assertIs(ctypes.c_int, function.restype)

    def test_macos_uses_renameatx_np_with_rename_excl(self) -> None:
        function = _FakeRenameFunction()
        self.rename_for("darwin", "renameatx_np", function)(9, "stage", "output")
        self.assertEqual([(9, b"stage", 9, b"output", 4)], function.calls)

    def test_a_refused_rename_reports_the_host_error(self) -> None:
        for platform, symbol in (("linux", "renameat2"), ("darwin", "renameatx_np")):
            function = _FakeRenameFunction(errno.EEXIST)
            with self.subTest(platform=platform), self.assertRaisesRegex(AtomicDirectoryError, os.strerror(errno.EEXIST)):
                self.rename_for(platform, symbol, function)(3, "stage", "output")

    def test_missing_symbol_unknown_host_and_unloadable_libc_fail_closed(self) -> None:
        with self.assertRaisesRegex(AtomicDirectoryError, "exclusive directory rename"):
            self.rename_for("linux", "renameatx_np", _FakeRenameFunction())  # wrong symbol for the host
        for platform in ("win32", "freebsd13", "cygwin"):
            with self.subTest(platform=platform), self.assertRaisesRegex(AtomicDirectoryError, "exclusive"):
                self.rename_for(platform, "renameat2", _FakeRenameFunction())

        def unloadable():
            raise OSError("no libc")

        with self.assertRaisesRegex(AtomicDirectoryError, "exclusive directory rename"):
            atomic_directory._exclusive_directory_rename(platform="linux", loader=unloadable)

    @unittest.skipUnless(sys.platform in atomic_directory.EXCLUSIVE_RENAME, "host has no exclusive rename")
    def test_real_host_rename_never_replaces_an_existing_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "stage").mkdir()
            (root / "stage/owned").write_bytes(b"owned")
            (root / "occupied").mkdir()
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                rename = atomic_directory._exclusive_directory_rename()
                with self.assertRaises(AtomicDirectoryError):
                    rename(parent, "stage", "occupied")
                self.assertEqual([], list((root / "occupied").iterdir()))
                rename(parent, "stage", "fresh")
            finally:
                os.close(parent)
            self.assertEqual(b"owned", (root / "fresh/owned").read_bytes())
            self.assertFalse((root / "stage").exists())


if __name__ == "__main__":
    unittest.main()
