"""The rendered site is sealed and published atomically (SPEC §5.3.2 steps 9-10, §6.1).

Ports Block Pops ``test_pages_site_atomic`` (a parent swapped before the static copy, a replaced
stage and a concurrently created destination are never published or deleted; an unsafe parent
fails before publication) and ``test_pages_site_output`` (every output kind is bound to the bytes
actually written; extra files, directories, links, special files and replaced entries are rejected;
a file changed after byte verification is caught by the final stamp pass) onto ``build_site``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.io.atomic_directory import AtomicDirectoryError
from mod_base.io.seal import SealError
from mod_base.pages import build
from mod_base.pages.build import build_site
from tests import test_build_support as bs
from tests.fixtures.mods import support


class SealFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-seal-")).resolve()
        cls.pub = bs.Publication(cls.directory / "publication")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.parent = self.root / "publication"
        self.parent.mkdir()
        self.output = self.parent / "site"
        self.held = self.root / "held"
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.sentinel = self.outside / "sentinel"
        self.sentinel.write_bytes(b"preserve")
        self.world = self.pub.world(stage="build")

    def build(self, output: Path | None = None) -> build.BuildResult:
        with mock.patch.object(host, "call", support.InProcessHost(api=self.world.api)):
            return build_site(self.pub.invocation(), api=self.world.api, kit_root=self.pub.kit,
                              collected_dir=self.root / "collected", families_dir=self.root / "families",
                              output=output or self.output, promotion_dir=self.root / "promotion")

    def corrupt(self, stage: int, relative: str) -> None:
        """Flip the last byte of ``relative`` in place (same inode, same size; a byte for an empty file)."""

        descriptor = os.open(relative, os.O_RDWR | os.O_NOFOLLOW, dir_fd=stage)
        try:
            before = os.fstat(descriptor)
            data = bytearray(os.read(descriptor, before.st_size))
            if data:
                data[-1] ^= 1
            else:
                data.append(1)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, data)
            self.assertEqual(before.st_ino, os.fstat(descriptor).st_ino)
        finally:
            os.close(descriptor)


class AtomicSiteTest(SealFlow):
    def test_the_published_site_preserves_static_and_compact_bytes(self) -> None:
        self.build()
        for relative in ("assets/site.js", "assets/gallery.js", "assets/styles.css"):
            self.assertEqual((self.pub.kit / "site" / relative).read_bytes(), (self.output / relative).read_bytes())
        self.assertEqual([path.name for path in self.parent.iterdir()], ["site"], "no stage is left behind")

    def test_a_parent_swapped_before_the_static_copy_is_never_written_or_published(self) -> None:
        original = build._copy_static

        def swapped(*arguments: Any) -> dict[str, bytes]:
            stage = next(self.parent.glob(".site.building-*"))
            self.parent.rename(self.held)
            self.parent.symlink_to(self.outside, target_is_directory=True)
            impostor = self.outside / stage.name
            impostor.mkdir()
            (impostor / "impostor").write_bytes(b"keep")
            return original(*arguments)

        with mock.patch.object(build, "_copy_static", side_effect=swapped), self.assertRaises(AtomicDirectoryError):
            self.build()
        self.assertEqual(self.sentinel.read_bytes(), b"preserve")
        self.assertFalse((self.outside / "site").exists())
        self.assertEqual([path.name for directory in self.outside.glob(".site.building-*") for path in directory.iterdir()],
                         ["impostor"])
        self.assertFalse((self.root / "promotion").exists())

    def test_a_replaced_stage_is_neither_published_nor_deleted(self) -> None:
        original = build._copy_static

        def swapped(*arguments: Any) -> dict[str, bytes]:
            stage = next(self.parent.glob(".site.building-*"))
            result = original(*arguments)
            stage.rename(self.held)
            stage.mkdir()
            (stage / "impostor").write_bytes(b"keep")
            return result

        with mock.patch.object(build, "_copy_static", side_effect=swapped), self.assertRaises(MbError):
            self.build()
        self.assertFalse(self.output.exists())
        self.assertEqual([path.name for directory in self.parent.glob(".site.building-*") for path in directory.iterdir()],
                         ["impostor"])

    def test_a_concurrent_empty_destination_is_not_replaced(self) -> None:
        original = build._copy_static
        occupied: list[int] = []

        def occupy(*arguments: Any) -> dict[str, bytes]:
            result = original(*arguments)
            self.output.mkdir()
            occupied.append(self.output.stat().st_ino)
            return result

        with mock.patch.object(build, "_copy_static", side_effect=occupy), self.assertRaises(AtomicDirectoryError):
            self.build()
        self.assertEqual(occupied, [self.output.stat().st_ino])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_an_unsafe_parent_fails_before_publication(self) -> None:
        link = self.root / "link"
        link.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(MbError):
            self.build(output=link / "site")
        self.assertEqual(list(self.outside.iterdir()), [self.sentinel])


class SiteOutputTest(SealFlow):
    def test_every_output_kind_is_bound_to_the_bytes_actually_written(self) -> None:
        write = build.write_new
        for selected in ("image", "index.html", "e2e/index.html", "assets/gallery.js", "assets/theme.css",
                         "assets/icon.png", "site-data.json", "e2e/gallery-data.json", ".nojekyll"):
            with self.subTest(selected=selected):
                self.setUp()

                def changed(stage: int, relative: str, data: bytes, target: str = selected) -> None:
                    write(stage, relative, data)
                    if relative == "build.json":
                        path = next(name for name in self.images(stage)) if target == "image" else target
                        self.corrupt(stage, path)

                with mock.patch.object(build, "write_new", side_effect=changed), self.assertRaises(SealError):
                    self.build()
                self.assertFalse(self.output.exists())
                self.assertFalse((self.root / "promotion").exists())

    @staticmethod
    def images(stage: int) -> list[str]:
        descriptor = os.open("e2e/images/mc26.3", os.O_RDONLY | os.O_DIRECTORY, dir_fd=stage)
        try:
            return [f"e2e/images/mc26.3/{name}" for name in sorted(os.listdir(descriptor))]
        finally:
            os.close(descriptor)

    def test_extra_entries_links_special_files_and_replacements_are_rejected(self) -> None:
        write = build.write_new
        for kind in ("file", "directory", "symlink", "fifo", "replacement-directory", "replacement-link"):
            with self.subTest(kind=kind):
                self.setUp()

                def changed(stage: int, relative: str, data: bytes, variant: str = kind) -> None:
                    write(stage, relative, data)
                    if relative != "build.json":
                        return
                    if variant == "file":
                        write(stage, "unreferenced", b"unvalidated")
                    elif variant == "directory":
                        os.mkdir("unused", dir_fd=stage)
                    elif variant == "symlink":
                        os.symlink(self.sentinel, "foreign", dir_fd=stage)
                    elif variant == "fifo":
                        os.mkfifo("fifo", dir_fd=stage)
                    elif variant == "replacement-directory":
                        os.unlink("index.html", dir_fd=stage)
                        os.mkdir("index.html", dir_fd=stage)
                    else:
                        os.unlink("index.html", dir_fd=stage)
                        os.symlink(self.sentinel, "index.html", dir_fd=stage)

                with mock.patch.object(build, "write_new", side_effect=changed), self.assertRaises(SealError):
                    self.build()
                self.assertFalse(self.output.exists())
                self.assertEqual(self.sentinel.read_bytes(), b"preserve")

    def test_a_file_changed_after_byte_verification_is_caught_by_the_final_stamps(self) -> None:
        observe = build._Builder.observe
        calls: list[int] = []
        stages: list[int] = []
        seal = build.seal_output

        def remember(stage: int, expected: Any, **options: Any) -> Any:
            stages.append(stage)
            return seal(stage, expected, **options)

        def observed(builder: Any) -> str:
            calls.append(1)
            if len(calls) == 2:
                self.corrupt(stages[0], "assets/gallery.js")
            return observe(builder)

        with mock.patch.object(build, "seal_output", side_effect=remember), \
                mock.patch.object(build._Builder, "observe", autospec=True, side_effect=observed), \
                self.assertRaisesRegex(SealError, "changed after byte verification"):
            self.build()
        self.assertEqual(len(calls), 2, "the API recheck runs between the seal and its final stamp pass")
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
