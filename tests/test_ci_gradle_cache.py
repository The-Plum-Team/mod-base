"""Gradle seed admission over real temporary caches.

Nothing here replaces a part of the module: every case walks, reads or copies real files. The
handoff to the candidate needs root and a really allocated account and runs in
``tests/ci_linux_worker.py``.
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

from mod_base.build_ci import gradle_cache as cache
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount
from mod_base.errors import MbError
from mod_base.io import tree
from mod_base.model import limits
from tests.test_ci_host import BOUNDARY


#: A candidate as passwd would describe it; no test here lets code act for this account.
CANDIDATE = WorkerAccount("candidate", 2001, 2001, str(WORKER_ROOT / "candidate-home"))
LINUX = sys.platform == "linux"


class SeedInventoryTests(unittest.TestCase):
    """The module's own caps and path rule over real temporary caches (no seams).

    The ownership handoff needs root and stays with the Linux fixture; what a seed may hold is
    decided by the inventory and the copy exercised here.
    """

    FILES = {
        "caches/modules-2/files-2.1/net.fabricmc/yarn/1.20.1+build.10/2d1f/yarn-1.20.1+build.10-v2.jar": b"yarn",
        "caches/modules-2/modules-2.lock": b"",
        "caches/8.8/kotlin-dsl/~tmp@1%20x/a b.class": b"scratch",
        "caches/transforms-4/4d/" + "/".join(f"pkg{index}" for index in range(40)) + "/Deep.class": b"deep",
        "wrapper/dists/gradle-8.8-bin/5e/gradle-8.8/lib/gradle-launcher-8.8.jar": b"wrapper",
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.seed = self.base / "seed"
        for name, data in self.FILES.items():
            (self.seed / name).parent.mkdir(parents=True, exist_ok=True)
            (self.seed / name).write_bytes(data)

    def refused(self):
        with self.assertRaises(MbError):
            cache._records(self.seed)
        stage = Path(tempfile.mkdtemp(dir=self.base))
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with self.assertRaises(MbError):
                tree.copy_regular_data_files(self.seed, descriptor, **cache._BOUNDS)
        finally:
            os.close(descriptor)
        self.assertEqual(os.listdir(stage), [])

    def test_a_real_cache_is_inventoried_and_copied_exactly(self):
        expected = [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, data in sorted(self.FILES.items())]
        self.assertIs(cache._BOUNDS["rule"], tree.SEED_PATHS)
        self.assertEqual(cache._records(self.seed), expected)
        stage = Path(tempfile.mkdtemp(dir=self.base))
        descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assertEqual(tree.copy_regular_data_files(self.seed, descriptor, **cache._BOUNDS), expected)
        finally:
            os.close(descriptor)
        self.assertEqual(cache._records(stage), expected)
        for name, data in self.FILES.items():
            self.assertEqual((stage / name).read_bytes(), data)
            self.assertNotEqual(os.stat(stage / name).st_ino, os.stat(self.seed / name).st_ino)

    def test_links_special_files_and_an_oversized_file_are_refused(self):
        lock = Path("caches") / "modules-2" / "modules-2.lock"
        self.assertEqual(len(cache._records(self.seed)), len(self.FILES))
        mutations = {
            "symlink": lambda: (self.seed / "caches" / "link.jar").symlink_to(self.seed / lock),
            "directory symlink": lambda: (self.seed / "wrapper" / "linked").symlink_to(self.seed / "caches"),
            "hard link": lambda: os.link(self.seed / lock, self.seed / "wrapper" / "second-name"),
            "fifo": lambda: os.mkfifo(self.seed / "caches" / "fifo"),
            # Sparse: one byte past the per-file cap without writing two gibibytes.
            "oversized file": lambda: os.truncate(self.seed / lock, limits.MAX_CI_SOURCE_FILE_BYTES + 1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                self.refused()


@unittest.skipUnless(LINUX, "seed admission walks real no-follow descriptors on Linux")
class SeedRootTests(unittest.TestCase):
    """What may sit at the root of a seed: the two conventional directories, by name and type."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.seed = Path(temporary.name).resolve() / "seed"
        self.seed.mkdir()

    def check(self):
        descriptor = os.open(self.seed, os.O_RDONLY | os.O_DIRECTORY)
        try:
            cache._seed_roots(descriptor)
        finally:
            os.close(descriptor)

    def test_caches_and_wrapper_directories_alone_are_a_seed(self):
        self.check()  # A cache that was never filled is a seed with nothing to copy.
        (self.seed / "caches").mkdir()
        self.check()
        (self.seed / "wrapper").mkdir()
        (self.seed / "caches" / "gradle.properties").write_bytes(b"below a cache directory this is data")
        self.check()

    def test_configuration_credentials_and_any_other_root_entry_are_refused_by_name(self):
        (self.seed / "caches").mkdir()
        entries = {
            "gradle.properties": lambda path: path.write_bytes(b"orgSecret=fixture\n"),
            "init.d": lambda path: path.mkdir(),
            "daemon": lambda path: path.mkdir(),
            "Caches": lambda path: path.mkdir(),
            ".tmp": lambda path: path.mkdir(),
        }
        for name, create in entries.items():
            with self.subTest(name=name):
                create(self.seed / name)
                with self.assertRaisesRegex(MbError, "configuration/credential or unexpected root"):
                    self.check()
                (self.seed / name).rmdir() if (self.seed / name).is_dir() else (self.seed / name).unlink()
        self.check()

    def test_a_conventional_name_must_be_a_real_directory(self):
        elsewhere = self.seed.parent / "elsewhere"
        elsewhere.mkdir()
        for name, create in (("file", lambda path: path.write_bytes(b"not a directory")),
                             ("symlink", lambda path: path.symlink_to(elsewhere)),
                             ("fifo", lambda path: os.mkfifo(path))):
            with self.subTest(name=name):
                create(self.seed / "wrapper")
                with self.assertRaisesRegex(MbError, "root child is not a real directory"):
                    self.check()
                (self.seed / "wrapper").unlink()


@unittest.skipUnless(LINUX, "the staging entries are Linux-only")
class RootOnlyTests(unittest.TestCase):
    def test_staging_refuses_the_unprivileged_runner_before_it_looks_at_the_seed(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            cache.stage_privileged_gradle_cache(Path("/home/runner/absent-seed"), boundary=BOUNDARY, account=CANDIDATE)


if __name__ == "__main__":
    unittest.main()
