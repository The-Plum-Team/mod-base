"""Path rules of sealed CI exports and Gradle seeds over real temporary trees (no seams).

A sealed export keeps the mod's own file names (``EXPORT_PATHS``); a Gradle seed is opaque cache
data whose names carry no grammar (``SEED_PATHS``); every other tree keeps the narrower bundle and
repository grammars. The handoffs that change ownership need root and stay with the Linux fixture.
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path

from mod_base.io import tree
from mod_base.io.tree import EXPORT_PATHS, SEED_PATHS, TreeError
from mod_base.model import limits

#: What Block Pops and Quick Skin stage today.
REAL_EXPORT = {
    "BlockPops - Fabric - 1.20.1-1.2.3.jar": b"block pops production",
    "BlockPops E2E - Fabric - 1.20.1-0.0.0.jar": b"block pops harness",
    "Quick Skin - Fabric - 1.21.4-1.0.0.jar": b"quick skin production",
    "Quick Skin E2E - Fabric - 1.21.4-0.0.0.jar": b"quick skin harness",
    "sbom/quick-skin.cdx.json": b'{"bomFormat":"CycloneDX"}',
}
#: Names a Linux filesystem holds and no export may; each is tried as a file and as a directory.
HOSTILE_NAMES = (" leading-space.jar", "trailing-space.jar ", ".leading-dot", "trailing-dot.", "double  space.jar",
                 "tab\tname.jar", "line\nbreak.jar", "escape\x1b.jar", "delete\x7f.jar", "back\\slash.jar",
                 "colon:name.jar", "unicod\xe9.jar", "\N{FULLWIDTH LATIN CAPITAL LETTER F}ullwidth.jar", "tilde~.jar",
                 "at@name.jar", "percent%20.jar", "star*.jar", "question?.jar", "quote\".jar", "pipe|.jar")
#: Shapes a Gradle user home can take: versions with "+" as in the real caches, groups that differ
#: only in case, lock files without a byte, and names no grammar could enumerate.
SEED = {
    "caches/modules-2/files-2.1/net.fabricmc/yarn/1.20.1+build.10/2d1f/yarn-1.20.1+build.10-v2.jar": b"yarn",
    "caches/modules-2/files-2.1/com.github.LlamaLad7/MixinExtras/0.3.5/1a/MixinExtras-0.3.5.jar": b"upper",
    "caches/modules-2/files-2.1/com.github.llamalad7/mixinextras/0.3.5/2b/mixinextras-0.3.5.jar": b"lower",
    "caches/modules-2/modules-2.lock": b"",
    "caches/8.8/kotlin-dsl/~tmp@1%20x/a b.class": b"scratch",
    "caches/transforms-4/3c/transformed/back\\slash:colon.jar": b"odd",
    "caches/transforms-4/3c/.hidden/.git/config": b"hidden",
    "caches/fabric-loom/\xe9-\N{GREEK SMALL LETTER ALPHA}.txt": b"unicode",
    "caches/transforms-4/4d/" + "/".join(f"pkg{index}" for index in range(40)) + "/Deep.class": b"deep",
    "wrapper/dists/gradle-8.8-bin/5e/gradle-8.8/lib/gradle-launcher-8.8.jar": b"wrapper",
}
FILES = {"max_files": 64, "max_total_bytes": 1 << 20, "max_file_bytes": 1 << 16}
TREE = {**FILES, "max_entries": 256}
SEED_BOUNDS = {**TREE, "rule": SEED_PATHS}


class RealTreeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "tree"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "secret").write_bytes(b"outside the tree")

    def write(self, files: dict[str, bytes]) -> list[dict[str, object]]:
        """Create ``files`` under the root and return the inventory every walk must report for them."""

        for name, data in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for name, data in sorted(files.items())]

    def stage(self) -> tuple[Path, int]:
        """A fresh private ``0700`` stage, as ``atomic_directory`` hands one to its writer."""

        path = Path(tempfile.mkdtemp(dir=self.base))
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, descriptor)
        return path, descriptor

    def assert_copied(self, stage: Path, files: dict[str, bytes]) -> None:
        for name, data in files.items():
            copied, original = os.stat(stage / name), os.stat(self.root / name)
            self.assertEqual((stage / name).read_bytes(), data)
            self.assertNotEqual(copied.st_ino, original.st_ino, "a copy never aliases its source inode")
            self.assertEqual((copied.st_nlink, stat.S_IMODE(copied.st_mode)), (1, 0o644))
        self.assertEqual(sum(len(names) for _, _, names in os.walk(stage)), len(files))


class SealedExportPathTests(RealTreeTestCase):
    def assert_export_refused(self) -> None:
        """Neither inventory admits the tree and neither copy writes a byte of it."""

        with self.assertRaises(TreeError):
            tree.file_records(self.root, **FILES, rule=EXPORT_PATHS)
        with self.assertRaises(TreeError):
            tree.regular_data_records(self.root, **TREE, rule=EXPORT_PATHS)
        for copy in (tree.copy_regular_files, tree.copy_regular_data_files):
            stage, descriptor = self.stage()
            with self.assertRaises(TreeError):
                copy(self.root, descriptor, **TREE, rule=EXPORT_PATHS)
            self.assertEqual(os.listdir(stage), [])

    def test_real_mod_file_names_are_inventoried_streamed_and_copied_unchanged(self) -> None:
        expected = self.write(REAL_EXPORT)
        self.assertEqual(tree.file_records(self.root, **FILES, rule=EXPORT_PATHS), expected)
        self.assertEqual(tree.file_records(self.root, exclude=("sbom/quick-skin.cdx.json",), **FILES, rule=EXPORT_PATHS),
                         expected[:-1])
        for name, data in REAL_EXPORT.items():
            chunks: list[bytes] = []
            self.assertEqual(tree.stream_child_file(self.root, name, max_bytes=len(data), consume=chunks.append,
                                                    rule=EXPORT_PATHS), len(data))
            self.assertEqual(b"".join(chunks), data)
        stage, descriptor = self.stage()
        self.assertEqual(tree.copy_regular_files(self.root, descriptor, **TREE, rule=EXPORT_PATHS), expected)
        self.assert_copied(stage, REAL_EXPORT)
        self.assertEqual(tree.file_records(stage, **FILES, rule=EXPORT_PATHS), expected)

    def test_a_declared_selection_of_real_names_is_appended_without_the_rest(self) -> None:
        expected = self.write(REAL_EXPORT)
        stage, descriptor = self.stage()
        jars = tuple(sorted(name for name in REAL_EXPORT if name.endswith(".jar")))
        self.assertEqual(tree.copy_selected_regular_files(self.root, descriptor, paths=jars, **TREE, rule=EXPORT_PATHS),
                         expected[:4])
        self.assertEqual(tree.copy_selected_regular_files(self.root, descriptor, paths=("sbom/quick-skin.cdx.json",),
                                                          **TREE, rule=EXPORT_PATHS), expected[4:])
        self.assert_copied(stage, REAL_EXPORT)
        with self.assertRaises(TreeError):  # an existing file is never replaced
            tree.copy_selected_regular_files(self.root, descriptor, paths=jars[:1], **TREE, rule=EXPORT_PATHS)
        self.assert_copied(stage, REAL_EXPORT)

    def test_runtime_data_keeps_real_names_and_empty_logs(self) -> None:
        files = {**REAL_EXPORT, "logs/latest.log": b"", "crash-reports/crash-2026-10-08_12.00.00-client.txt": b"trace"}
        expected = self.write(files)
        (self.root / "screenshots" / "Quick Skin 1.21.4+build.1").mkdir(parents=True)  # an empty directory is no record
        self.assertEqual(tree.regular_data_records(self.root, **TREE, rule=EXPORT_PATHS), expected)
        stage, descriptor = self.stage()
        self.assertEqual(tree.copy_regular_data_files(self.root, descriptor, **TREE, rule=EXPORT_PATHS), expected)
        self.assert_copied(stage, files)
        self.assertEqual(tree.regular_data_records(stage, **TREE, rule=EXPORT_PATHS), expected)

    def test_pages_bundle_and_repository_rules_still_refuse_these_names(self) -> None:
        self.write(REAL_EXPORT)
        for inventory in (tree.regular_files, tree.file_records):
            with self.assertRaisesRegex(TreeError, "canonical bundle path"):
                inventory(self.root, **FILES)
        with self.assertRaises(TreeError):
            tree.regular_data_records(self.root, **TREE)
        for copy in (tree.copy_regular_files, tree.copy_regular_data_files):
            stage, descriptor = self.stage()
            with self.assertRaises(TreeError):
                copy(self.root, descriptor, **TREE)
            self.assertEqual(os.listdir(stage), [])
        read: list[bytes] = []
        for name in REAL_EXPORT:
            if " " in name:
                with self.subTest(name=name):
                    with self.assertRaisesRegex(TreeError, "canonical bundle path"):
                        tree.read_child_file(self.root, name, max_bytes=64)
                    with self.assertRaisesRegex(TreeError, "canonical bundle path"):
                        tree.stream_child_file(self.root, name, max_bytes=64, consume=read.append)
                    stage, descriptor = self.stage()
                    with self.assertRaises(TreeError):
                        tree.copy_selected_regular_files(self.root, descriptor, paths=(name,), **TREE)
        self.assertEqual(read, [])

    def test_hostile_file_and_directory_names_are_refused_before_any_copy(self) -> None:
        for name in HOSTILE_NAMES:
            for relative in (name, f"{name}/inner.jar", f"libs/{name}"):
                with self.subTest(relative=relative):
                    self.setUp()
                    self.write({**REAL_EXPORT, relative: b"hostile"})
                    self.assert_export_refused()
                    read: list[bytes] = []
                    with self.assertRaises(TreeError):
                        tree.stream_child_file(self.root, relative, max_bytes=64, consume=read.append, rule=EXPORT_PATHS)
                    self.assertEqual(read, [])

    def test_traversal_and_absolute_paths_are_refused_before_any_read(self) -> None:
        self.write(REAL_EXPORT)
        read: list[bytes] = []
        for relative in ("../outside/secret", "sbom/../../outside/secret", str(self.outside / "secret"), "/etc/hostname",
                         "sbom//quick-skin.cdx.json", "sbom/./quick-skin.cdx.json", "sbom/", "", ".", ".."):
            with self.subTest(relative=relative):
                with self.assertRaises(TreeError):
                    tree.stream_child_file(self.root, relative, max_bytes=64, consume=read.append, rule=EXPORT_PATHS)
                with self.assertRaises(TreeError):
                    tree.file_records(self.root, exclude=(relative,), **FILES, rule=EXPORT_PATHS)
                stage, descriptor = self.stage()
                with self.assertRaises(TreeError):
                    tree.copy_selected_regular_files(self.root, descriptor, paths=(relative,), **TREE, rule=EXPORT_PATHS)
                self.assertEqual(os.listdir(stage), [])
        self.assertEqual(read, [])

    def test_case_aliases_of_files_and_of_directories_are_refused(self) -> None:
        for pair in (("Mod - Fabric.jar", "mod - fabric.jar"), ("libs/Mod.jar", "libs/mod.jar"),
                     ("SBOM/a.cdx.json", "sbom/b.cdx.json"), ("libs/Sub/a.jar", "libs/sub/b.jar")):
            with self.subTest(pair=pair):
                self.setUp()
                self.write(dict.fromkeys(pair, b"alias"))
                self.assertEqual(sum(len(names) for _, _, names in os.walk(self.root)), 2,
                                 "this filesystem folds case, so it cannot hold the pair under test")
                self.assert_export_refused()
                stage, descriptor = self.stage()
                with self.assertRaises(TreeError):
                    tree.copy_selected_regular_files(self.root, descriptor, paths=tuple(sorted(pair)), **TREE,
                                                     rule=EXPORT_PATHS)
                self.assertEqual(os.listdir(stage), [])
        self.setUp()
        self.write({"sbom/quick-skin.cdx.json": b"{}"})
        (self.root / "SBOM").mkdir()  # even an empty directory may not alias an inventoried one
        self.assert_export_refused()

    def test_depth_component_and_total_length_bounds_still_apply(self) -> None:
        depth, chars = limits.MAX_BUNDLE_PATH_DEPTH, limits.MAX_BUNDLE_PATH_CHARS
        widest = f"{'a' * 128}/{'b' * 128}/"
        expected = self.write({"/".join(["d"] * (depth - 1) + ["leaf.jar"]): b"deep", "n" * 128: b"long",
                               widest + "c" * (chars - len(widest)): b"wide"})
        self.assertEqual(tree.file_records(self.root, **FILES, rule=EXPORT_PATHS), expected)
        self.assertEqual(tree.regular_data_records(self.root, **TREE, rule=EXPORT_PATHS), expected)
        for relative in ("/".join(["d"] * depth + ["leaf.jar"]), "n" * 129, widest + "c" * (chars - len(widest) + 1)):
            with self.subTest(components=relative.count("/") + 1, characters=len(relative)):
                self.setUp()
                self.write({relative: b"beyond"})
                self.assert_export_refused()


class GradleSeedPathTests(RealTreeTestCase):
    def assert_seed_refused(self, **bounds: int) -> None:
        with self.assertRaises(TreeError):
            tree.regular_data_records(self.root, **{**SEED_BOUNDS, **bounds})
        stage, descriptor = self.stage()
        with self.assertRaises(TreeError):
            tree.copy_regular_data_files(self.root, descriptor, **{**SEED_BOUNDS, **bounds})
        self.assertEqual(os.listdir(stage), [])

    def test_cache_names_and_deep_nesting_copy_exactly(self) -> None:
        expected = self.write(SEED)
        (self.root / "caches" / "8.8" / "empty+dir").mkdir()  # counted, never copied
        self.assertEqual(tree.regular_data_records(self.root, **SEED_BOUNDS), expected)
        stage, descriptor = self.stage()
        self.assertEqual(tree.copy_regular_data_files(self.root, descriptor, **SEED_BOUNDS), expected)
        self.assert_copied(stage, SEED)
        self.assertEqual(tree.regular_data_records(stage, **SEED_BOUNDS), expected)
        self.assertEqual(stat.S_IMODE(os.stat(stage / "caches" / "modules-2").st_mode), 0o700)
        self.assertFalse((stage / "caches" / "8.8" / "empty+dir").exists())

    def test_every_grammar_refuses_what_a_seed_holds(self) -> None:
        self.write(SEED)
        for rule in (tree.BUNDLE_PATHS, tree.REPO_PATHS, EXPORT_PATHS):
            with self.subTest(rule=rule.name):
                with self.assertRaises(TreeError):
                    tree.regular_data_records(self.root, **{**SEED_BOUNDS, "rule": rule})
                stage, descriptor = self.stage()
                with self.assertRaises(TreeError):
                    tree.copy_regular_data_files(self.root, descriptor, **{**SEED_BOUNDS, "rule": rule})
                self.assertEqual(os.listdir(stage), [])

    def test_links_and_special_files_are_refused_before_any_copy(self) -> None:
        lock = "caches/modules-2/modules-2.lock"
        mutations = {
            "file symlink": lambda: (self.root / "caches" / "link.jar").symlink_to(self.outside / "secret"),
            "directory symlink": lambda: (self.root / "caches" / "linked").symlink_to(self.outside, target_is_directory=True),
            "dangling symlink": lambda: (self.root / "wrapper" / "dangling").symlink_to(self.base / "absent"),
            "hard link to the outside": lambda: os.link(self.root / lock, self.outside / "alias"),
            "hard link inside": lambda: os.link(self.root / lock, self.root / "wrapper" / "second-name"),
            "fifo": lambda: os.mkfifo(self.root / "caches" / "fifo"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                self.write(SEED)
                mutate()
                self.assert_seed_refused()

    def test_count_and_byte_caps_are_refused_before_any_copy(self) -> None:
        self.write(SEED)
        total = sum(map(len, SEED.values()))
        self.assert_seed_refused(max_files=len(SEED) - 1)
        self.assert_seed_refused(max_total_bytes=total - 1)
        self.assert_seed_refused(max_file_bytes=max(map(len, SEED.values())) - 1)
        self.assert_seed_refused(max_entries=16)
        (self.root / "caches" / "oversized.bin").write_bytes(b"x" * (SEED_BOUNDS["max_file_bytes"] + 1))
        self.assert_seed_refused()

    def test_depth_is_bounded_by_the_seed_limit_and_not_by_a_grammar(self) -> None:
        self.assertLess(limits.MAX_BUNDLE_PATH_DEPTH, limits.MAX_CI_SEED_PATH_DEPTH)
        self.assertLessEqual(limits.MAX_CI_SEED_PATH_DEPTH, tree.MAX_WALK_DEPTH)
        deepest = "/".join(["d"] * limits.MAX_CI_SEED_PATH_DEPTH)
        expected = self.write({deepest: b"leaf"})
        self.assertEqual(tree.regular_data_records(self.root, **SEED_BOUNDS), expected)
        stage, descriptor = self.stage()
        self.assertEqual(tree.copy_regular_data_files(self.root, descriptor, **SEED_BOUNDS), expected)
        self.assert_copied(stage, {deepest: b"leaf"})
        self.setUp()
        self.write({f"d/{deepest}": b"leaf"})
        self.assert_seed_refused()


if __name__ == "__main__":
    unittest.main()
