"""``io.tree``: bounded regular-file walks (Quick Skin ``evidence._bounded_entries``/
``reject_symlinks``) and descriptor-relative child reads (Block Pops ``_child_file``)."""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import tree
from mod_base.io.tree import TreeError, file_records, read_child_file, reject_symlinks, regular_files, sha256_file

BOUNDS = {"max_files": 16, "max_total_bytes": 1024, "max_file_bytes": 256}


class TreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "bundle"
        (self.root / "images" / "deep").mkdir(parents=True)
        (self.root / "manifest.json").write_bytes(b'{"kind":"x"}')
        (self.root / "images" / "b.webp").write_bytes(b"RIFF-b")
        (self.root / "images" / "deep" / "a.png").write_bytes(b"\x89PNG-a")
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.json").write_bytes(b"{}")


class RegularFilesTests(TreeTestCase):
    def test_returns_the_sorted_inventory_of_sizes(self) -> None:
        files = regular_files(self.root, **BOUNDS)
        self.assertEqual({"images/b.webp": 6, "images/deep/a.png": 6, "manifest.json": 12}, files)
        self.assertEqual(sorted(files), list(files))
        self.assertEqual(files, regular_files(self.root, **BOUNDS, suffixes={".json", ".png", ".webp"}))

    def test_refuses_links_special_files_hard_links_and_empty_files(self) -> None:
        mutations = {
            "file symlink": lambda: (self.root / "images" / "link.json").symlink_to(self.outside / "secret.json"),
            "directory symlink": lambda: (self.root / "linked").symlink_to(self.outside, target_is_directory=True),
            "dangling symlink": lambda: (self.root / "dangling").symlink_to(self.base / "absent"),
            "hard link": lambda: os.link(self.root / "manifest.json", self.outside / "alias"),
            "empty file": lambda: (self.root / "images" / "empty.webp").write_bytes(b""),
        }
        if hasattr(os, "mkfifo"):
            mutations["fifo"] = lambda: os.mkfifo(self.root / "images" / "fifo")
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                with self.assertRaises(TreeError):
                    regular_files(self.root, **BOUNDS)

    def test_refuses_names_that_are_not_canonical_bundle_paths(self) -> None:
        for name in ("space name.json", "..hidden", "-dash", "unicodé.json", "colon:name"):
            with self.subTest(name=name):
                self.setUp()
                (self.root / name).write_bytes(b"x")
                with self.assertRaisesRegex(TreeError, "canonical bundle path"):
                    regular_files(self.root, **BOUNDS)

    def test_enforces_count_size_total_suffix_and_depth_bounds(self) -> None:
        cases = (
            ({**BOUNDS, "max_files": 2}, "file-count or byte"),
            ({**BOUNDS, "max_total_bytes": 23}, "file-count or byte"),
            ({**BOUNDS, "max_file_bytes": 11}, "outside 1..11"),
        )
        for bounds, pattern in cases:
            with self.subTest(bounds=bounds), self.assertRaisesRegex(TreeError, pattern):
                regular_files(self.root, **bounds)
        with self.assertRaisesRegex(TreeError, "suffix"):
            regular_files(self.root, **BOUNDS, suffixes={".json", ".webp"})
        deep = self.root.joinpath(*["d"] * 20)
        deep.mkdir(parents=True)
        (deep / "leaf.json").write_bytes(b"{}")
        with self.assertRaises(TreeError):
            regular_files(self.root, max_files=64, max_total_bytes=1024, max_file_bytes=256)
        for bad in (-1, True, 1.5):
            with self.subTest(bound=bad), self.assertRaisesRegex(TreeError, "bounds"):
                regular_files(self.root, max_files=bad, max_total_bytes=1024, max_file_bytes=256)

    def test_listing_is_bounded_before_a_hostile_directory_is_read_whole(self) -> None:
        crowded = self.root / "crowded"
        crowded.mkdir()
        for index in range(200):
            (crowded / f"f{index:03d}.json").write_bytes(b"{}")
        with self.assertRaisesRegex(TreeError, "entry bound|file-count"):
            regular_files(self.root, max_files=8, max_total_bytes=1 << 20, max_file_bytes=256)

    def test_root_must_be_a_real_directory(self) -> None:
        link = self.base / "link"
        link.symlink_to(self.root, target_is_directory=True)
        for root in (link, self.root / "manifest.json", self.base / "absent"):
            with self.subTest(root=root.name), self.assertRaises(TreeError):
                regular_files(root, **BOUNDS)

    def test_a_directory_replaced_by_a_symlink_during_the_walk_is_refused(self) -> None:
        real_stat = os.stat
        swapped = []

        def swapping_stat(name, *arguments, **keywords):
            info = real_stat(name, *arguments, **keywords)
            if name == "images" and not swapped:
                swapped.append(True)
                (self.root / "images").rename(self.base / "held")
                (self.root / "images").symlink_to(self.outside, target_is_directory=True)
            return info

        with patch.object(tree.os, "stat", side_effect=swapping_stat), self.assertRaises(TreeError):
            regular_files(self.root, **BOUNDS)
        self.assertEqual([True], swapped)


class RejectSymlinksTests(TreeTestCase):
    def test_accepts_a_clean_tree_and_a_regular_file(self) -> None:
        reject_symlinks(self.root)
        reject_symlinks(self.root / "manifest.json")
        (self.root / "empty-dir").mkdir()
        (self.root / "space name").write_bytes(b"")  # only links and special files matter here
        reject_symlinks(self.root)

    def test_refuses_any_symlink_or_special_file(self) -> None:
        link = self.base / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(TreeError, "symlink"):
            reject_symlinks(link)
        (self.root / "images" / "deep" / "nested").symlink_to(self.outside, target_is_directory=True)
        with self.assertRaisesRegex(TreeError, "symlink"):
            reject_symlinks(self.root)
        if hasattr(os, "mkfifo"):
            self.setUp()
            os.mkfifo(self.root / "images" / "fifo")
            with self.assertRaisesRegex(TreeError, "special"):
                reject_symlinks(self.root)
            with self.assertRaisesRegex(TreeError, "special"):
                reject_symlinks(self.root / "images" / "fifo")
        with self.assertRaises(TreeError):
            reject_symlinks(self.base / "absent")


class ChildReadTests(TreeTestCase):
    def test_reads_nested_children_exactly(self) -> None:
        self.assertEqual(b"\x89PNG-a", read_child_file(self.root, "images/deep/a.png", max_bytes=64))
        self.assertEqual(b'{"kind":"x"}', read_child_file(self.root, "manifest.json", max_bytes=12))

    def test_refuses_links_at_any_component_and_non_canonical_paths(self) -> None:
        (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
        (self.root / "file-link.json").symlink_to(self.outside / "secret.json")
        for relative in ("linked/secret.json", "file-link.json", "../outside/secret.json", "/etc/passwd",
                         "images//b.webp", "images/./b.webp", "images\\b.webp", ""):
            with self.subTest(relative=relative), self.assertRaises(TreeError):
                read_child_file(self.root, relative, max_bytes=64)
        root_link = self.base / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(TreeError):
            read_child_file(root_link, "manifest.json", max_bytes=64)

    def test_refuses_bad_sizes_directories_and_missing_files(self) -> None:
        (self.root / "empty.json").write_bytes(b"")
        for relative, maximum in (("empty.json", 64), ("manifest.json", 11), ("images", 64), ("absent.json", 64),
                                  ("images/absent/a.png", 64)):
            with self.subTest(relative=relative), self.assertRaises(TreeError):
                read_child_file(self.root, relative, max_bytes=maximum)

    def test_a_file_that_grows_while_read_is_refused(self) -> None:
        real_read = os.read

        def growing(descriptor: int, count: int) -> bytes:
            data = real_read(descriptor, count)
            if data:
                with open(self.root / "manifest.json", "ab") as stream:
                    stream.write(b" ")
            return data

        with patch.object(tree.os, "read", side_effect=growing), self.assertRaisesRegex(TreeError, "changed"):
            read_child_file(self.root, "manifest.json", max_bytes=64)


class HashAndRecordTests(TreeTestCase):
    def test_sha256_file_streams_one_regular_file(self) -> None:
        data = (self.root / "manifest.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), sha256_file(self.root / "manifest.json", max_bytes=64))
        (self.root / "empty").write_bytes(b"")
        self.assertEqual(hashlib.sha256(b"").hexdigest(), sha256_file(self.root / "empty", max_bytes=64))
        link = self.root / "link.json"
        link.symlink_to(self.outside / "secret.json")
        for path, maximum in ((link, 64), (self.root / "manifest.json", 11), (self.root / "images", 64),
                              (self.root / "absent", 64)):
            with self.subTest(path=path.name), self.assertRaises(TreeError):
                sha256_file(path, max_bytes=maximum)

    def test_file_records_are_the_exact_sorted_inventory(self) -> None:
        records = file_records(self.root, exclude=("manifest.json",), **BOUNDS)
        self.assertEqual(
            [{"path": "images/b.webp", "sha256": hashlib.sha256(b"RIFF-b").hexdigest(), "size": 6},
             {"path": "images/deep/a.png", "sha256": hashlib.sha256(b"\x89PNG-a").hexdigest(), "size": 6}],
            records)
        self.assertEqual(3, len(file_records(self.root, **BOUNDS)))
        with self.assertRaises(TreeError):
            file_records(self.root, exclude=("../manifest.json",), **BOUNDS)
        (self.root / "images" / "link.webp").symlink_to(self.outside / "secret.json")
        with self.assertRaises(TreeError):
            file_records(self.root, **BOUNDS)

    def test_errors_are_kit_rejections(self) -> None:
        with self.assertRaises(MbError) as caught:
            regular_files(self.base / "absent", **BOUNDS)
        self.assertEqual("unsafe-tree", caught.exception.reason)


if __name__ == "__main__":
    unittest.main()
