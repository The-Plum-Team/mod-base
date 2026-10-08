"""Kit overlay admission and mode normalisation over real directories.

Nothing here replaces a part of the module: every overlay is a real tree owned by the test user,
one of them this checkout's kit as a mod's bootstrap stages it. Copying into a really allocated
candidate runs as root in ``tests/ci_linux_worker.py``.
"""

import hashlib
import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import worker_overlay as overlay
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import ACTIONS_LOCK, STAGED_LOCK, STAMP_NAME, Pin, kit_tree_digest, stamp_document
from tests.test_ci_gradle_cache import CANDIDATE
from tests.test_ci_host import BOUNDARY


LINUX = sys.platform == "linux"
KIT = Path(__file__).resolve().parents[1]
PIN = Pin("a" * 40, "v1.0.3", ())
#: The smallest kit: an importable package and an empty lock for the absent template/ and tools/.
FILES = {"src/mod_base/__init__.py": b"", STAGED_LOCK: b""}


def authored_digest(files: dict[str, bytes]) -> str:
    """kit-digest-v1 from authored bytes, never from what a tree on disk happens to hold."""
    listing = "".join(f"{hashlib.sha256(data).hexdigest()}  ./{name}\n" for name, data in sorted(files.items())
                      if name.split("/")[0] in ("src", "site", "requirements"))
    return "sha256:" + hashlib.sha256(listing.encode("ascii")).hexdigest()


def write_overlay(root: Path, files: dict[str, bytes], *, pin: Pin = PIN, digest: str | None = None) -> str:
    """A staged kit: plain 0644 files below 0755 directories, the three digested roots and a stamp."""
    digest = authored_digest(files) if digest is None else digest
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    for name in ("src", "site", "requirements"):
        (root / name).mkdir()
        (root / name).chmod(0o755)
    add_files(root, files)
    add_files(root, {STAMP_NAME: canonical_json(stamp_document(pin, digest))})
    return digest


def add_files(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        parent = root
        for part in name.split("/")[:-1]:
            parent = parent / part
            parent.mkdir(exist_ok=True)
            parent.chmod(0o755)
        (root / name).write_bytes(data)
        (root / name).chmod(0o644)


@unittest.skipUnless(LINUX, "overlay admission walks real no-follow descriptors on Linux")
class OverlayAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "overlay"
        self.digest = write_overlay(self.root, FILES)

    def refuse(self, reason, *, pin=PIN, digest=None):
        with self.assertRaisesRegex(MbError, reason):
            overlay._admit(self.root, pin, self.digest if digest is None else digest)

    def test_a_stamped_kit_is_admitted_with_every_byte_recorded(self):
        stamp = canonical_json(stamp_document(PIN, self.digest))
        expected = [{"path": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                    for name, data in sorted({**FILES, STAMP_NAME: stamp}.items())]
        self.assertEqual(overlay._admit(self.root, PIN, self.digest), expected)
        self.assertEqual(overlay._paths(self.root), tuple(sorted({**FILES, STAMP_NAME: stamp})))

    def test_the_kit_a_bootstrap_stages_from_this_checkout_is_admitted(self):
        spec = importlib.util.spec_from_file_location(
            "mod_base_kit_overlay_fixture", KIT / "template" / "managed" / "scripts" / "ci" / "mod_base_kit.py")
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        staged = self.base / "staged"
        bootstrap.copy_kit(KIT, staged)
        digest = kit_tree_digest(KIT)
        (staged / STAMP_NAME).write_bytes(canonical_json(stamp_document(PIN, digest)))
        records = overlay._admit(staged, PIN, digest)
        paths = [record["path"] for record in records]
        self.assertEqual(paths, sorted(paths))
        for name in (STAMP_NAME, "src/mod_base/__init__.py", STAGED_LOCK, ACTIONS_LOCK,
                     "tools/ci_privileged_bootstrap.py", "template/managed/scripts/ci/mod_base_kit.py"):
            self.assertIn(name, paths)
        self.assertEqual({path.split("/")[0] for path in paths},
                         {STAMP_NAME, "src", "site", "requirements", "template", "tools", "actions"})

    def test_another_pin_version_digest_or_a_changed_byte_is_refused(self):
        self.refuse("differs from the independently bound pin/digest", pin=Pin("b" * 40, "v1.0.3", ()))
        self.refuse("differs from the independently bound pin/digest", pin=Pin("a" * 40, "v1.0.4", ()))
        self.refuse("differs from the independently bound pin/digest", digest="sha256:" + "0" * 64)
        add_files(self.root, {"src/mod_base/added.py": b"unbound = True\n"})
        self.refuse("differs from the independently bound pin/digest")

    def test_roots_outside_the_closed_kit_layout_are_refused(self):
        for name, data in (("credentials", b"token"), ("docs/README.md", b"text"), (".git/config", b"[core]\n")):
            with self.subTest(name=name):
                self.setUp()
                add_files(self.root, {name: data})
                self.refuse("unexpected root")
        for name in ("site", STAMP_NAME):
            with self.subTest(missing=name):
                self.setUp()
                (self.root / name).rmdir() if name == "site" else (self.root / name).unlink()
                self.refuse("missing required roots")

    def test_staged_directories_must_match_the_locks_inside_the_digest(self):
        add_files(self.root, {"tools/run.py": b"print('unlocked')\n"})
        self.refuse("do not match")
        self.setUp()
        add_files(self.root, {"actions/extra/action.yml": b"name: Unbound\n"})
        self.refuse("is not bound by")

    def test_bytecode_search_path_files_and_noncanonical_names_are_refused(self):
        for name in ("src/mod_base/__pycache__/module.cpython-312.pyc", "src/mod_base/module.pyc",
                     "src/mod_base/module.pyo", "src/evil.pth", "src/a b.py"):
            with self.subTest(name=name):
                self.setUp()
                add_files(self.root, {name: b"data"})
                self.refuse("bytecode or a noncanonical path")
        self.setUp()
        add_files(self.root, {"src/mod_base/native.pyd": b"MZ"})
        self.refuse("excessive files or bytecode")

    def test_links_executables_and_special_files_are_refused(self):
        module = self.root / "src" / "mod_base" / "__init__.py"
        mutations = {
            "executable": lambda: module.chmod(0o755),
            "symlink": lambda: (self.root / "src" / "linked.py").symlink_to(module),
            "directory symlink": lambda: (self.root / "src" / "linked").symlink_to(self.root / "site"),
            "hard link": lambda: os.link(module, self.base / "second-name"),
            "fifo": lambda: os.mkfifo(self.root / "src" / "pipe", 0o644),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.setUp()
                module = self.root / "src" / "mod_base" / "__init__.py"
                mutate()
                with self.assertRaises(MbError):
                    overlay._admit(self.root, PIN, self.digest)

    def test_discovery_is_bounded_in_depth(self):
        with patch.object(limits, "MAX_CI_TOOL_TREE_DEPTH", 1):
            self.refuse("exceeds its depth cap")


@unittest.skipUnless(LINUX, "mode normalisation uses real descriptors on Linux")
class OverlayModeTests(unittest.TestCase):
    """``_plain`` gives a handed-over copy the 0644/0755 modes a mod's bootstrap expects."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "copy"
        write_overlay(self.root, FILES)
        for directory, names, files in os.walk(self.root):
            for name in files:
                os.chmod(os.path.join(directory, name), 0o600)
            os.chmod(directory, 0o700)
        self.owner = WorkerAccount("candidate", os.getuid(), os.getgid(), str(WORKER_ROOT / "candidate-home"))

    def normalise(self, account):
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            overlay._plain(descriptor, account)
        finally:
            os.close(descriptor)

    def test_every_file_becomes_0644_and_every_directory_0755(self):
        self.normalise(self.owner)
        modes = {stat.S_IMODE(os.lstat(self.root).st_mode)}
        for directory, names, files in os.walk(self.root):
            modes.update(stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) for name in names)
            self.assertEqual({stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) for name in files} - {0o644},
                             set())
        self.assertEqual(modes, {0o755})
        self.assertEqual((self.root / "src" / "mod_base" / "__init__.py").read_bytes(), b"")

    def test_entries_of_another_owner_links_and_special_files_are_refused(self):
        with self.assertRaisesRegex(MbError, "ownership changed"):
            self.normalise(WorkerAccount("candidate", os.getuid() + 1, os.getgid(), self.owner.home))
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        (self.root / "src" / "linked.py").symlink_to(self.root / STAMP_NAME)
        with self.assertRaisesRegex(MbError, "link or special file"):
            self.normalise(self.owner)
        (self.root / "src" / "linked.py").unlink()
        os.link(self.root / STAMP_NAME, self.root.parent / "second-name")
        with self.assertRaisesRegex(MbError, "link or special file"):
            self.normalise(self.owner)

    def test_the_walk_is_bounded_in_depth(self):
        with patch.object(limits, "MAX_CI_TOOL_TREE_DEPTH", 1), self.assertRaisesRegex(MbError, "exceeds its depth cap"):
            self.normalise(self.owner)


@unittest.skipUnless(LINUX, "the staging entries are Linux-only")
class RootOnlyTests(unittest.TestCase):
    def test_staging_refuses_the_unprivileged_runner_before_it_looks_at_the_overlay(self):
        if os.getuid() == 0:
            self.skipTest("the unit suite runs as the unprivileged runner")
        with self.assertRaisesRegex(MbError, "requires protected root setup"):
            overlay.stage_privileged_worker_overlay(Path("/home/runner/absent-overlay"), boundary=BOUNDARY,
                                                    account=CANDIDATE, pin=PIN, expected_digest=authored_digest(FILES))


if __name__ == "__main__":
    unittest.main()
