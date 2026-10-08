"""Private Gradle handoff orchestration; OS/data seams do not establish hosted isolation."""

import hashlib
import os
import stat
import tempfile
import unittest
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mod_base.build_ci import gradle_cache as cache
from mod_base.build_ci.worker import WorkerAccount, WorkerError
from mod_base.errors import MbError
from mod_base.io import tree
from mod_base.model import limits
from tests.helpers import ci_stat as info
from tests.test_ci_host import BOUNDARY


ACCOUNT = WorkerAccount("worker", 2001, 2001, "/tmp/mod-base-sandbox-boundary/mod-base-worker/worker-home")
SEED = Path("/home/runner/gradle-seed")
RECORDS = [{"path": "caches/module.jar", "size": 3, "sha256": "a" * 64}]


class GradleCacheTests(unittest.TestCase):
    def seams(self, stack):
        host = stack.enter_context(patch.object(cache, "authenticate_privileged_host_boundary"))
        account = stack.enter_context(patch.object(cache, "authenticate_worker_account", return_value=ACCOUNT))
        quiet = stack.enter_context(patch.object(cache, "_quiet"))
        close = stack.enter_context(patch.object(cache.os, "close"))
        stack.enter_context(patch.object(cache, "_open_directory", side_effect=lambda parts: 7 if parts[-1] == "gradle-seed" else 8))
        stats = {7: info(inode=7, uid=BOUNDARY.uid, gid=BOUNDARY.gid, mode=stat.S_IFDIR | 0o700),
                 8: info(inode=8, uid=ACCOUNT.uid, gid=ACCOUNT.gid, mode=stat.S_IFDIR | 0o700)}
        stack.enter_context(patch.object(cache.os, "fstat", side_effect=lambda fd: stats[fd]))
        listing = SimpleNamespace(names=["caches", "wrapper"], occupied=False)
        def scan(fd):
            names = listing.names if fd == 7 else ["existing"] if listing.occupied else []
            return nullcontext(iter(SimpleNamespace(name=name, stat=lambda **kw: info()) for name in names))
        stack.enter_context(patch.object(cache.os, "scandir", side_effect=scan))
        for name in ("fchown", "fchmod"):
            stack.enter_context(patch.object(cache.os, name, create=True))
        records = stack.enter_context(patch.object(cache, "_records", return_value=RECORDS))
        copied = stack.enter_context(patch.object(cache, "copy_regular_data_files", return_value=RECORDS))
        private = stack.enter_context(patch.object(cache, "privatize_regular_data_copy", return_value=RECORDS))
        access = stack.enter_context(patch.object(cache, "authenticate_tree_private_access"))
        terminate = stack.enter_context(patch.object(cache, "terminate_worker"))
        return host, account, quiet, stats, listing, records, copied, private, access, terminate

    def test_private_copy_matches_source_and_bp_caps_before_handoff(self):
        with ExitStack() as stack:
            host, account, quiet, stats, listing, records, copied, private, access, terminate = self.seams(stack)
            self.assertEqual(RECORDS, cache.stage_privileged_gradle_cache(SEED, boundary=BOUNDARY, account=ACCOUNT))
            self.assertEqual(4, records.call_count)
            self.assertEqual((250000, 200000, 2 * 1024**3, 20 * 1024**3),
                (cache._BOUNDS["max_entries"], cache._BOUNDS["max_files"], cache._BOUNDS["max_file_bytes"], cache._BOUNDS["max_total_bytes"]))
            copied.assert_called_once_with(SEED, 8, **cache._BOUNDS)
            self.assertEqual(private.call_args.kwargs, {"source_owner_uid": 0, "owner_uid": ACCOUNT.uid,
                                                       "owner_gid": ACCOUNT.gid, **cache._BOUNDS})
            self.assertGreaterEqual(quiet.call_count, 5)
            access.assert_called_once()
            terminate.assert_not_called()

    def test_bad_role_or_account_never_touches_the_cache(self):
        with ExitStack() as stack:
            host, account, quiet, *rest = self.seams(stack)
            host.side_effect = WorkerError("role")
            with self.assertRaises(WorkerError):
                cache.stage_privileged_gradle_cache(SEED, boundary=BOUNDARY, account=ACCOUNT)
            account.assert_not_called()
            host.side_effect = None
            with self.assertRaises(WorkerError):
                cache.stage_privileged_gradle_cache(SEED, boundary=BOUNDARY, account=object())
            quiet.assert_not_called()

    def test_wrong_source_shape_path_or_existing_cache_locks_the_worker(self):
        for kind in ("credentials", "config", "path", "occupied", "owner", "quiet"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, stats, listing, records, copied, private, access, terminate = self.seams(stack)
                seed = SEED
                if kind in ("credentials", "config"):
                    listing.names = ["caches", ".credentials" if kind == "credentials" else "gradle.properties"]
                elif kind == "path":
                    seed = Path("/tmp/unprotected")
                elif kind == "occupied":
                    listing.occupied = True
                elif kind == "owner":
                    stats[8] = info(inode=8, uid=1001, mode=stat.S_IFDIR | 0o700)
                else:
                    quiet.side_effect = WorkerError("running UID")
                with self.assertRaises(WorkerError):
                    cache.stage_privileged_gradle_cache(seed, boundary=BOUNDARY, account=ACCOUNT)
                copied.assert_not_called()
                terminate.assert_called_once_with(ACCOUNT)

    def test_copy_source_handoff_and_os_failures_never_return_cache_authority(self):
        for kind in ("copied", "source", "handoff", "private", "os"):
            with self.subTest(kind=kind), ExitStack() as stack:
                host, account, quiet, stats, listing, records, copied, private, access, terminate = self.seams(stack)
                if kind == "copied":
                    copied.return_value = []
                elif kind == "source":
                    records.side_effect = [RECORDS, []]
                elif kind == "handoff":
                    private.return_value = []
                elif kind == "private":
                    access.side_effect = WorkerError("wrong private ownership")
                else:
                    copied.side_effect = OSError("read failed")
                with self.assertRaises(WorkerError):
                    cache.stage_privileged_gradle_cache(SEED, boundary=BOUNDARY, account=ACCOUNT)
                terminate.assert_called_once_with(ACCOUNT)

    def test_directory_substitution_is_rejected(self):
        with ExitStack() as stack:
            host, account, quiet, stats, listing, records, copied, private, access, terminate = self.seams(stack)
            def mutate(*args, **kwargs):
                stats[7] = info(inode=70, uid=BOUNDARY.uid, gid=BOUNDARY.gid, mode=stat.S_IFDIR | 0o700)
                return RECORDS
            copied.side_effect = mutate
            with self.assertRaises(WorkerError):
                cache.stage_privileged_gradle_cache(SEED, boundary=BOUNDARY, account=ACCOUNT)
            private.assert_not_called()
            terminate.assert_called_once_with(ACCOUNT)


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
