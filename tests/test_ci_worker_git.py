"""Git metadata admission over real directories and a real shallow checkout.

Nothing here replaces a part of the module: selection, ref text and the HEAD rule read real files
owned by the test user, and Git itself makes the checkout of the last class. Publication into a
really allocated candidate runs as root in ``tests/ci_linux_worker.py``.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import worker_git as git
from mod_base.build_ci.host import HostBoundary
from mod_base.errors import MbError
from mod_base.model import limits
from tests.test_ci_gradle_cache import CANDIDATE
from tests.test_ci_host import BOUNDARY


LINUX = sys.platform == "linux"
COMMIT, OTHER = "a" * 40, "b" * 40
LOOSE = "objects/ab/" + "c" * 38
PACK = "objects/pack/pack-" + "d" * 40
#: What a checkout step leaves in ``.git``; the bytes are inert, only their names and types matter.
CLONE = {
    "HEAD": (COMMIT + "\n").encode(),
    "config": b"[http]\n\textraheader = AUTHORIZATION: basic fixture-secret\n",
    "description": b"Unnamed repository\n",
    "FETCH_HEAD": (COMMIT + "\t\tfixture\n").encode(),
    "ORIG_HEAD": (OTHER + "\n").encode(),
    "hooks/post-checkout": b"#!/bin/sh\nexit 1\n",
    "info/exclude": b"# exclude\n",
    "logs/HEAD": b"moved\n",
    "index": b"DIRC inert index",
    LOOSE: b"inert loose object",
    PACK + ".pack": b"PACK", PACK + ".idx": b"idx", PACK + ".rev": b"rev",
    "objects/info/packs": b"P pack\n",
    "packed-refs": ("# pack-refs with: peeled fully-peeled sorted \n" + COMMIT + " refs/tags/v0\n^" + OTHER + "\n").encode(),
    "refs/heads/main": (COMMIT + "\n").encode(),
    "refs/pull/7/merge": (COMMIT + "\n").encode(),
    "refs/remotes/origin/HEAD": b"ref: refs/remotes/origin/main\n",
    "refs/remotes/origin/main": (COMMIT + "\n").encode(),
    "refs/tags/v1": (COMMIT + "\n").encode(),
    "shallow": (OTHER + "\n").encode(),
}
SELECTED = ("index", LOOSE, PACK + ".idx", PACK + ".pack", PACK + ".rev", "packed-refs", "refs/heads/main",
            "refs/pull/7/merge", "refs/remotes/origin/HEAD", "refs/remotes/origin/main", "refs/tags/v1", "shallow")


def runner_boundary() -> HostBoundary:
    """A receipt naming the test user as the runner: the owner the selection requires of every entry."""
    return HostBoundary("/home/runner", os.getuid(), os.getgid(), 1, 10, 0o755)


def write_metadata(root: Path, files: dict[str, bytes]) -> None:
    """Files 0644 below directories 0755, whatever the caller's umask is."""
    root.mkdir(mode=0o755, exist_ok=True)
    root.chmod(0o755)
    for name, data in files.items():
        parent = root
        for part in name.split("/")[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True)
            parent.chmod(0o755)
        (root / name).write_bytes(data)
        (root / name).chmod(0o644)


def run_git(*arguments: str, cwd: Path) -> str:
    """Git as a checkout step runs it: no user or system configuration, fixed identity and dates."""
    environment = {"PATH": os.path.dirname(shutil.which("git")) + os.pathsep + os.defpath, "HOME": str(cwd), "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                   "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                   "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z", "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z"}
    result = subprocess.run(["git", *arguments], cwd=cwd, env=environment, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {arguments[0]} failed: {result.stderr.decode(errors='replace')[-400:]}")
    return result.stdout.decode()


def shallow_checkout(base: Path, files: dict[str, tuple[str, bytes]]) -> tuple[Path, str]:
    """``(checkout, commit)``: three commits upstream, fetched two deep into a fresh repository and
    detached at the newest, the way a checkout step checks out one commit."""
    upstream = base / "upstream"
    upstream.mkdir()
    run_git("init", "-q", "--initial-branch=main", ".", cwd=upstream)
    for subject in ("first", "second"):
        (upstream / "history.txt").write_bytes(subject.encode() + b"\n")
        run_git("add", "-A", cwd=upstream)
        run_git("commit", "-q", "-m", subject, cwd=upstream)
    for name, (mode, data) in files.items():
        leaf = upstream / name
        leaf.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            os.symlink(data, os.fsencode(leaf))
        else:
            leaf.write_bytes(data)
            leaf.chmod(0o755 if mode == "100755" else 0o644)
    run_git("add", "-A", cwd=upstream)
    run_git("commit", "-q", "-m", "tested", cwd=upstream)
    commit = run_git("rev-parse", "HEAD", cwd=upstream).strip()
    checkout = base / "checkout"
    checkout.mkdir(mode=0o755)
    checkout.chmod(0o755)
    run_git("init", "-q", ".", cwd=checkout)
    run_git("-c", "protocol.version=2", "fetch", "-q", "--no-tags", "--depth=2", upstream.as_uri(),
            "+refs/heads/main:refs/remotes/origin/main", cwd=checkout)
    run_git("checkout", "-q", "--force", "--detach", commit, cwd=checkout)
    return checkout, commit


class GitConfigurationTests(unittest.TestCase):
    def test_fixed_config_has_no_source_credentials_filters_includes_or_hooks(self):
        data = git._configuration('owner/project')
        self.assertIn(b'https://github.com/owner/project.git', data)
        self.assertIn(b'hooksPath = /dev/null', data)
        self.assertIn(b'fsmonitor = false', data)
        self.assertNotIn(b'credential', data)
        for repository in ('owner/repo\n[include]', 'owner/repo/token', 'https://host/repo'):
            with self.assertRaises(MbError):
                git._configuration(repository)


@unittest.skipUnless(LINUX, "Git metadata admission walks real no-follow descriptors on Linux")
class GitMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "metadata"
        write_metadata(self.root, CLONE)
        self.boundary = runner_boundary()

    def refuse(self, reason):
        with self.assertRaisesRegex(MbError, reason):
            git._selection(self.root, self.boundary)

    def test_selection_keeps_index_objects_and_refs_and_never_config_head_hooks_or_logs(self):
        self.assertEqual(git._selection(self.root, self.boundary), SELECTED)
        self.assertEqual(git._admit(self.root, self.boundary, COMMIT), SELECTED)

    def test_links_foreign_writers_and_special_files_are_refused(self):
        mutations = {
            "symlinked index": lambda: ((self.root / "index").unlink(),
                                        (self.root / "index").symlink_to(self.root / "config")),
            "hard-linked object": lambda: os.link(self.root / LOOSE, self.base / "second-name"),
            "group-writable file": lambda: (self.root / "index").chmod(0o664),
            "world-writable directory": lambda: (self.root / "refs").chmod(0o777),
            "fifo": lambda: os.mkfifo(self.root / "hooks" / "pipe", 0o644),
            "symlinked directory": lambda: (self.root / "logs" / "refs").symlink_to(self.root / "refs"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                mutate()
                self.refuse("unsafe paths, types, ownership or links")

    def test_an_entry_owned_by_neither_root_nor_the_runner_is_refused(self):
        if os.getuid() == 0:
            self.skipTest("root owns the fixture and root-owned entries are always accepted")
        stranger = HostBoundary("/home/runner", os.getuid() + 1, os.getgid(), 1, 10, 0o755)
        with self.assertRaisesRegex(MbError, "unsafe paths, types, ownership or links"):
            git._selection(self.root, stranger)

    def test_borrowed_shared_partial_and_nested_stores_are_refused(self):
        cases = {
            "commondir": ("unsupported file", b"../shared\n"),
            "objects/info/alternates": ("borrows external objects or grafts", b"/elsewhere/objects\n"),
            "objects/info/http-alternates": ("borrows external objects or grafts", b"https://host/objects\n"),
            "info/grafts": ("borrows external objects or grafts", (COMMIT + " " + OTHER + "\n").encode()),
            PACK + ".promisor": ("unsupported file", b""),
            "objects/pack/multi-pack-index": ("unsupported file", b"MIDX"),
            "refs/replace/" + COMMIT: ("unsupported file", (OTHER + "\n").encode()),
            "modules/sub/HEAD": ("unsupported directory", (COMMIT + "\n").encode()),
            "lfs/objects/aa/bb": ("unsupported directory", b"large"),
            "worktrees/other/HEAD": ("unsupported directory", (COMMIT + "\n").encode()),
            "refs/heads/feature@1": ("unsafe paths, types, ownership or links", (COMMIT + "\n").encode()),
            "refs/heads/Main": ("unsafe paths, types, ownership or links", (COMMIT + "\n").encode()),
            "CONFIG": ("unsupported file", b"[core]\n"),
        }
        for name, (reason, data) in cases.items():
            with self.subTest(name=name):
                self.setUp()
                write_metadata(self.root, {name: data})
                self.refuse(reason)

    def test_a_checkout_without_head_index_or_its_own_objects_is_refused(self):
        for names in (("HEAD",), ("index",), (LOOSE, PACK + ".pack", PACK + ".idx", PACK + ".rev")):
            with self.subTest(names=names):
                self.setUp()
                for name in names:
                    (self.root / name).unlink()
                self.refuse("lacks index or self-contained object data")

    def test_an_ignored_root_entry_of_the_wrong_type_is_refused(self):
        self.setUp()
        shutil.rmtree(self.root / "hooks")
        write_metadata(self.root, {"hooks": b"not a directory"})
        self.refuse("ignored root entry has the wrong type")
        self.setUp()
        (self.root / "HEAD").unlink()
        write_metadata(self.root, {"HEAD/inside": b"a directory named HEAD"})
        self.refuse("ignored root entry has the wrong type")

    def test_entry_and_selected_file_caps_bound_the_walk(self):
        with patch.object(limits, "MAX_CI_GIT_METADATA_ENTRIES", 8):
            self.refuse("exceeds its entry cap")
        with patch.object(limits, "MAX_CI_GIT_METADATA_FILES", len(SELECTED) + 1):
            self.refuse("exceeds its selected file cap")
        with patch.object(limits, "MAX_CI_TOOL_TREE_DEPTH", 2):
            self.refuse("exceeds its depth cap")

    def test_head_must_be_detached_at_exactly_the_tested_commit(self):
        for name, data in (("another commit", (OTHER + "\n").encode()),
                           ("a branch", b"ref: refs/heads/main\n"),
                           ("no line end", COMMIT.encode()),
                           ("carriage return", (COMMIT + "\r\n").encode()),
                           ("trailing bytes", (COMMIT + "\n" + OTHER + "\n").encode()),
                           ("upper case", (COMMIT.upper() + "\n").encode())):
            with self.subTest(name=name):
                write_metadata(self.root, {"HEAD": data})
                self.assertEqual(git._selection(self.root, self.boundary), SELECTED)
                with self.assertRaisesRegex(MbError, "HEAD is not detached at the tested commit"):
                    git._admit(self.root, self.boundary, COMMIT)
        write_metadata(self.root, {"HEAD": (COMMIT + "\n").encode()})
        with self.assertRaisesRegex(MbError, "HEAD is not detached at the tested commit"):
            git._admit(self.root, self.boundary, OTHER)
        self.assertEqual(git._admit(self.root, self.boundary, COMMIT), SELECTED)

    def test_ref_text_accepts_clone_shapes(self):
        git._texts(self.root, SELECTED)

    def test_ref_text_rejects_redirects_replacements_duplicates_and_malformed_lists(self):
        cases = [("refs/heads/main", b"ref: /outside\n"), ("refs/heads/main", b"ref: refs/replace/x\n"),
                 ("refs/heads/main", COMMIT.encode() + b"\nextra"),
                 ("shallow", (COMMIT + "\n" + COMMIT + "\n").encode()), ("shallow", COMMIT.encode()),
                 ("shallow", b"not a commit\n"),
                 ("packed-refs", (COMMIT + " refs/replace/" + COMMIT + "\n").encode()),
                 ("packed-refs", (COMMIT + " refs/tags/v1\n" + COMMIT + " refs/tags/v1\n").encode()),
                 ("packed-refs", (COMMIT + " refs/tags/v1\n" + COMMIT + " refs/tags/V1\n").encode()),
                 ("packed-refs", b"^" + COMMIT.encode() + b"\n"), ("packed-refs", b"# unsupported\n"),
                 ("packed-refs", (COMMIT + " refs/tags/v1").encode()),
                 ("packed-refs", b"# pack-refs with: peeled\vfully-peeled\nsorted\n"),
                 ("packed-refs", b"# pack-refs with: peeled\tfully-peeled\n"),
                 ("packed-refs", b"# pack-refs with: peeled unknown-trait\n"),
                 ("packed-refs", b"\r" * 1000 + b"\n")]
        for path, raw in cases:
            with self.subTest(path=path, raw=raw[:40]):
                write_metadata(self.root, {path: raw})
                with self.assertRaises(MbError):
                    git._texts(self.root, (path,))

    def test_ref_lists_are_bounded_per_line_and_in_count(self):
        write_metadata(self.root, {"packed-refs": b"# pack-refs with:" + b" peeled" * 600 + b"\n",
                                   "shallow": (COMMIT + "\n" + OTHER + "\n").encode()})
        with self.assertRaisesRegex(MbError, "per-line cap"):
            git._texts(self.root, ("packed-refs",))
        git._texts(self.root, ("shallow",))
        with patch.object(limits, "MAX_CI_GIT_METADATA_FILES", 1), self.assertRaises(MbError):
            git._texts(self.root, ("shallow",))
        write_metadata(self.root, {"packed-refs": (COMMIT + " refs/tags/v1\n" + COMMIT + " refs/tags/v2\n").encode()})
        git._texts(self.root, ("packed-refs",))
        with patch.object(limits, "MAX_CI_GIT_METADATA_FILES", 1), self.assertRaises(MbError):
            git._texts(self.root, ("packed-refs",))


@unittest.skipUnless(LINUX, "Git metadata admission walks real no-follow descriptors on Linux")
class RealCheckoutTests(unittest.TestCase):
    def setUp(self):
        if shutil.which("git") is None:
            self.skipTest("git is unavailable")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.checkout, self.commit = shallow_checkout(self.base, {"file": ("100644", b"tracked\n")})
        self.metadata = self.checkout / ".git"
        self.boundary = runner_boundary()

    def test_a_shallow_detached_checkout_is_admitted_with_what_git_needs_and_nothing_else(self):
        paths = git._admit(self.metadata, self.boundary, self.commit)
        self.assertIn("index", paths)
        self.assertIn("shallow", paths)
        self.assertIn("refs/remotes/origin/main", paths)
        self.assertTrue(any(path.startswith("objects/") for path in paths))
        for path in paths:
            self.assertTrue((self.metadata / path).is_file(), path)
        present = {entry.name for entry in self.metadata.iterdir()}
        self.assertTrue({"HEAD", "config", "FETCH_HEAD"} <= present, present)
        self.assertEqual({path.split("/")[0] for path in paths}, {"index", "objects", "refs", "shallow"})

    def test_a_checkout_whose_head_moved_is_refused(self):
        parent = run_git("rev-parse", "HEAD^", cwd=self.checkout).strip()
        run_git("update-ref", "--no-deref", "HEAD", parent, cwd=self.checkout)
        self.assertEqual(run_git("status", "--porcelain=v1", cwd=self.checkout), "A  file\n")
        with self.assertRaisesRegex(MbError, "HEAD is not detached at the tested commit"):
            git._admit(self.metadata, self.boundary, self.commit)
        run_git("update-ref", "--no-deref", "HEAD", self.commit, cwd=self.checkout)
        git._admit(self.metadata, self.boundary, self.commit)
        run_git("update-ref", "refs/heads/work", self.commit, cwd=self.checkout)
        run_git("symbolic-ref", "HEAD", "refs/heads/work", cwd=self.checkout)
        self.assertEqual(run_git("rev-parse", "HEAD", cwd=self.checkout).strip(), self.commit)
        with self.assertRaisesRegex(MbError, "HEAD is not detached at the tested commit"):
            git._admit(self.metadata, self.boundary, self.commit)

    def test_a_checkout_someone_committed_in_is_refused(self):
        run_git("commit", "-q", "--allow-empty", "-m", "made in the job", cwd=self.checkout)
        with self.assertRaises(MbError):
            git._admit(self.metadata, self.boundary, self.commit)


@unittest.skipUnless(LINUX, "the staging entries are Linux-only")
class RootOnlyTests(unittest.TestCase):
    def test_staging_refuses_the_unprivileged_runner_before_it_looks_at_the_checkout(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            git.stage_privileged_worker_git(Path("/home/runner/absent/.git"), boundary=BOUNDARY, account=CANDIDATE,
                                            repository="owner/project", tested_commit=COMMIT)


if __name__ == "__main__":
    unittest.main()
