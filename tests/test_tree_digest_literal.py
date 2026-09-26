"""kit-digest-v1 and the MB_KIT_TREE_DIGEST literal of the callee workflows (SPEC §1.2 step 4).

Pins six things: the committed literal equals the digest of this kit tree; ``tools/kit_digest.sh``,
the function inlined in every callee prologue, ``tools/update_tree_digest.py`` (over the Git index)
and ``mod_base.pin.kit_tree_digest`` compute the same value and refuse the same trees; the tool
digests only what a commit records and refuses a working tree that differs from it; the tool's
``--write``/``--check`` modes; the tool's staged-file lock gate (a stale lock fails ``--check`` and
is regenerated, never digested, by ``--write``); and the executed "Bind the two-part implementation
identity" step.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base import workflow
from mod_base.errors import MbError
from mod_base.pin import ACTIONS_LOCK, STAGED_LOCK, actions_listing, kit_tree_digest, staged_listing
from tests.test_workflow_policy import (CALLEE_PATHS, PROLOGUE, ROOT, ShellHarness, callee, outputs,
                                        require_tools, step)

TOOL = ROOT / "tools/update_tree_digest.py"
SHELL = ROOT / "tools/kit_digest.sh"
BEGIN, END = "# >>> kit-digest-v1\n", "# <<< kit-digest-v1\n"


def _load_tool():
    spec = importlib.util.spec_from_file_location("mb_update_tree_digest", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


update_tree_digest = _load_tool()


def digest_block(text: str) -> str:
    """The marked kit-digest-v1 function (markers included), dedented to column 0."""

    lines = text.splitlines()
    first = [index for index, line in enumerate(lines) if line.strip() == BEGIN.strip()]
    last = [index for index, line in enumerate(lines) if line.strip() == END.strip()]
    if len(first) != 1 or len(last) != 1 or last[0] <= first[0]:
        raise AssertionError("expected exactly one marked kit-digest-v1 block")
    indent = len(lines[first[0]]) - len(lines[first[0]].lstrip(" "))
    block = lines[first[0]:last[0] + 1]
    if any(line and not line.startswith(" " * indent) for line in block):
        raise AssertionError("the marked block is not uniformly indented")
    return "".join(line[indent:] + "\n" for line in block)


def reference_digest(root: Path) -> str:
    """An independent kit-digest-v1 for well-formed trees (no refusals)."""

    paths = []
    for top in ("src", "site", "requirements"):
        for path in (root / top).rglob("*"):
            if path.is_file():
                paths.append("./" + path.relative_to(root).as_posix())
    listing = "".join(f"{hashlib.sha256((root / path[2:]).read_bytes()).hexdigest()}  {path}\n"
                      for path in sorted(paths, key=lambda value: value.encode()))
    return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()


def git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Run git in ``root`` without the caller's ``GIT_*`` redirection."""

    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, timeout=60, check=check,
                          env=environment)


def stage(root: Path) -> Path:
    """Initialise ``root`` as a Git repository (once) and stage its whole working tree."""

    if not (root / ".git").exists():
        git(root, "init", "-q")
    git(root, "add", "-A", check=False)
    return root


def make_kit(root: Path) -> Path:
    files = {
        "src/mod_base/__init__.py": b"VERSION = 1\n",
        "src/mod_base/a-b.py": b"dash\n",
        "src/mod_base/a.b": b"dot\n",
        "src/mod_base/a/b.py": b"slash\n",
        "src/mod_base/Z.py": b"upper\n",
        "src/mod_base/template/__init__.py": b"",
        "site/index.html": "<!doctype html><title>é</title>\n".encode(),
        "site/assets/site.js": b"'use strict';\n" * 3000,
        "requirements/pillow.txt": b"Pillow==12.3.0 \\\n    --hash=sha256:00\n",
        "template/managed/.github/workflows/pages.yml": b"name: Project site\n",
        "template/manifest.json": b"{}\n",
        "tools/helper.sh": b"#!/bin/sh\nexit 0\n",
        "actions/setup/action.yml": b"name: Setup\n",
    }
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    os.chmod(root / "tools/helper.sh", 0o755)  # the staged-file lock addresses content only
    write_lock(root)
    return root


def write_lock(root: Path) -> None:
    """Write the staged-file locks of ``root``'s current ``template/``, ``tools/`` and ``actions/``."""

    (root / STAGED_LOCK).parent.mkdir(parents=True, exist_ok=True)
    (root / STAGED_LOCK).write_bytes(staged_listing(root))
    (root / ACTIONS_LOCK).write_bytes(actions_listing(root))


class DigestParityTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "find", "sort", "sha256sum", "cut", "git")
        temporary = tempfile.TemporaryDirectory(prefix="kit digest ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def shell(self, kit: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(SHELL), str(kit)], capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"})

    def inline(self, kit: Path) -> subprocess.CompletedProcess[str]:
        bind = step(callee("publish")["jobs"]["admit"]["steps"], PROLOGUE[3])["run"]
        script = "set -euo pipefail\n" + digest_block(bind) + 'kit_digest_v1 "$1"\n'
        return subprocess.run(["bash", "-c", script, "inline", str(kit)], capture_output=True, text=True, timeout=60,
                              env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C"})

    def test_every_implementation_computes_the_same_digest(self) -> None:
        kit = stage(make_kit(self.root / "kit"))
        expected = reference_digest(kit)
        for label, result in (("shell", self.shell(kit)), ("inline", self.inline(kit))):
            with self.subTest(implementation=label):
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected + "\n")
        self.assertEqual(update_tree_digest.tree_digest(kit), expected)
        self.assertEqual(kit_tree_digest(kit), expected)
        (kit / "site/index.html").write_bytes(b"changed\n")
        with self.assertRaisesRegex(update_tree_digest.DigestError, r"site/index\.html \(not staged\)"):
            update_tree_digest.tree_digest(kit)
        stage(kit)
        self.assertEqual(update_tree_digest.tree_digest(kit), reference_digest(kit))
        self.assertNotEqual(reference_digest(kit), expected)

    def test_the_prologues_inline_exactly_the_tool_function(self) -> None:
        block = digest_block(SHELL.read_text(encoding="utf-8"))
        for name in workflow.CALLEE_WORKFLOWS:
            for job_id, job in callee(name)["jobs"].items():
                with self.subTest(callee=name, job=job_id):
                    self.assertEqual(digest_block(step(job["steps"], PROLOGUE[3])["run"]), block)

    def test_every_implementation_refuses_the_same_trees(self) -> None:
        def symlink_file(kit: Path) -> None:
            (kit / "src/link.py").symlink_to(kit / "src/mod_base/Z.py")

        def symlink_dir(kit: Path) -> None:
            (kit / "site/linked").symlink_to(kit / "src/mod_base")

        def executable(kit: Path) -> None:
            os.chmod(kit / "src/mod_base/Z.py", 0o755)

        def fifo(kit: Path) -> None:
            os.mkfifo(kit / "requirements/pipe")

        def bytecode_file(kit: Path) -> None:
            (kit / "src/__pycache__").write_bytes(b"x")

        def unsafe_file(kit: Path) -> None:
            (kit / "site/with space.txt").write_bytes(b"x")

        def unsafe_directory(kit: Path) -> None:
            (kit / "site/bad dir").mkdir()

        def non_ascii_name(kit: Path) -> None:
            (kit / "site/caf\u00e9.txt").write_bytes(b"x")

        def missing_site(kit: Path) -> None:
            shutil.rmtree(kit / "site")

        def site_symlink(kit: Path) -> None:
            shutil.rmtree(kit / "site")
            (kit / "site").symlink_to(kit / "requirements")

        cases = [symlink_file, symlink_dir, executable, fifo, bytecode_file, unsafe_file, unsafe_directory,
                 non_ascii_name, missing_site, site_symlink]
        for index, mutate in enumerate(cases):
            kit = make_kit(self.root / f"refuse-{index}")
            mutate(kit)
            stage(kit)
            with self.subTest(case=mutate.__name__):
                self.assertNotEqual(self.shell(kit).returncode, 0)
                self.assertNotEqual(self.inline(kit).returncode, 0)
                with self.assertRaises(update_tree_digest.DigestError):
                    update_tree_digest.tree_digest(kit)
                with self.assertRaises(MbError):
                    kit_tree_digest(kit)

    def test_bytecode_is_refused_by_ci_and_by_the_tool_once_staged(self) -> None:
        kit = stage(make_kit(self.root / "bytecode"))
        expected = reference_digest(kit)
        (kit / "src/mod_base/__pycache__").mkdir()
        (kit / "src/mod_base/__pycache__/x.cpython-313.pyc").write_bytes(b"\x00")
        self.assertNotEqual(self.shell(kit).returncode, 0)
        self.assertNotEqual(self.inline(kit).returncode, 0)
        with self.assertRaises(MbError):
            kit_tree_digest(kit)
        self.assertEqual(update_tree_digest.tree_digest(kit), expected, "an unstaged __pycache__ is never committed")
        stage(kit)
        with self.assertRaisesRegex(update_tree_digest.DigestError, r"src/mod_base/__pycache__/x\.cpython-313\.pyc"):
            update_tree_digest.tree_digest(kit)

    def test_an_empty_tree_is_refused(self) -> None:
        kit = self.root / "empty"
        for top in ("src", "site", "requirements"):
            (kit / top).mkdir(parents=True)
        stage(kit)
        self.assertNotEqual(self.shell(kit).returncode, 0)
        self.assertNotEqual(self.inline(kit).returncode, 0)
        with self.assertRaises(update_tree_digest.DigestError):
            update_tree_digest.tree_digest(kit)


class CommittedTreeTests(unittest.TestCase):
    """The tool digests the Git index and refuses whatever a checkout of the commit would not hold."""

    def setUp(self) -> None:
        require_tools("git")
        temporary = tempfile.TemporaryDirectory(prefix="kit index ")
        self.addCleanup(temporary.cleanup)
        self.kit = stage(make_kit(Path(temporary.name) / "kit"))
        self.expected = reference_digest(self.kit)

    def assertRefused(self, pattern: str) -> None:
        with self.assertRaisesRegex(update_tree_digest.DigestError, pattern):
            update_tree_digest.tree_digest(self.kit)

    def test_a_staged_tree_needs_no_commit(self) -> None:
        self.assertEqual(update_tree_digest.tree_digest(self.kit), self.expected)
        self.assertNotEqual(git(self.kit, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode, 0)

    def test_untracked_and_ignored_files_are_refused_not_hashed(self) -> None:
        (self.kit / "site/.DS_Store").write_bytes(b"\x00")
        self.assertRefused(r"site/\.DS_Store \(untracked or ignored\)")
        (self.kit / ".gitignore").write_text(".DS_Store\n", encoding="utf-8")
        self.assertRefused(r"site/\.DS_Store \(untracked or ignored\)")
        (self.kit / "site/.DS_Store").unlink()
        (self.kit / "src/mod_base/Z.py.orig").write_bytes(b"backup\n")
        self.assertRefused(r"src/mod_base/Z\.py\.orig \(untracked or ignored\)")

    def test_unstaged_and_deleted_files_are_refused(self) -> None:
        (self.kit / "requirements/pillow.txt").write_bytes(b"changed\n")
        self.assertRefused(r"requirements/pillow\.txt \(not staged\)")
        stage(self.kit)
        (self.kit / "src/mod_base/a-b.py").unlink()
        self.assertRefused(r"src/mod_base/a-b\.py \(missing from the working tree\)")

    def test_index_entries_the_checkout_would_refuse_are_refused(self) -> None:
        git(self.kit, "update-index", "--chmod=+x", "src/mod_base/Z.py")
        self.assertRefused(r"src/mod_base/Z\.py \(100755")
        git(self.kit, "update-index", "--chmod=-x", "src/mod_base/Z.py")
        self.assertEqual(update_tree_digest.tree_digest(self.kit), self.expected)
        (self.kit / "site/assets/link.js").symlink_to("site.js")
        git(self.kit, "add", "site/assets/link.js")
        self.assertRefused(r"site/assets/link\.js \(120000")

    def test_a_directory_without_indexed_files_is_refused(self) -> None:
        git(self.kit, "rm", "-q", "--cached", "-r", "site")
        self.assertRefused(r"site/ holds no file in the Git index")

    def test_the_root_must_be_a_git_top_level(self) -> None:
        with self.assertRaisesRegex(update_tree_digest.DigestError, "top level"):
            update_tree_digest.tree_digest(self.kit / "src")
        plain = make_kit(self.kit.parent / "plain")
        with self.assertRaises(update_tree_digest.DigestError):
            update_tree_digest.tree_digest(plain)

    def test_inherited_git_redirection_is_ignored(self) -> None:
        other = stage(make_kit(self.kit.parent / "other"))
        (other / "site/index.html").write_bytes(b"other\n")
        stage(other)
        with mock.patch.dict(os.environ, {"GIT_DIR": str(other / ".git"), "GIT_WORK_TREE": str(other),
                                          "GIT_INDEX_FILE": str(other / ".git/index")}):
            self.assertEqual(update_tree_digest.tree_digest(self.kit), self.expected)


class LiteralTests(unittest.TestCase):
    def test_committed_literals_equal_this_kit_tree(self) -> None:
        require_tools("git")
        digest = update_tree_digest.tree_digest(ROOT)
        stale = update_tree_digest.stale_workflows(ROOT, digest)
        self.assertEqual(stale, [], f"run python3 tools/update_tree_digest.py --write (expected {digest})")
        for name in workflow.CALLEE_WORKFLOWS:
            self.assertEqual(callee(name)["env"]["MB_KIT_TREE_DIGEST"], digest)

    def test_committed_staged_lock_describes_the_committed_template_and_tools(self) -> None:
        require_tools("git")
        self.assertTrue(update_tree_digest.staged_lock_current(ROOT),
                        f"{STAGED_LOCK} is stale: run python3 tools/update_tree_digest.py --write, stage the lock "
                        "and run it again")

    def test_write_and_check_modes(self) -> None:
        require_tools("git")
        with tempfile.TemporaryDirectory(prefix="kit literal ") as temporary:
            root = stage(make_kit(Path(temporary)))
            (root / ".github/workflows").mkdir(parents=True)
            for path in CALLEE_PATHS.values():
                shutil.copyfile(path, root / ".github/workflows" / path.name)
            tool = [sys.executable, str(TOOL), "--root", str(root)]
            check = subprocess.run([*tool, "--check"], capture_output=True, text=True, timeout=60)
            self.assertEqual(check.returncode, 1)
            self.assertIn(".github/workflows/publish.yml", check.stderr)
            before = {path.name: (root / ".github/workflows" / path.name).read_text() for path in CALLEE_PATHS.values()}
            written = subprocess.run([*tool, "--write"], capture_output=True, text=True, timeout=60)
            self.assertEqual(written.returncode, 0, written.stderr)
            digest = reference_digest(root)
            self.assertEqual(written.stdout, digest + "\n")
            for name, text in before.items():
                after = (root / ".github/workflows" / name).read_text()
                changed = [(old, new) for old, new in zip(text.splitlines(), after.splitlines()) if old != new]
                self.assertEqual(len(after.splitlines()), len(text.splitlines()))
                self.assertLessEqual(len(changed), 1, name)
                self.assertEqual(update_tree_digest.read_literal(root / ".github/workflows" / name), digest)
            self.assertEqual(subprocess.run([*tool, "--check"], capture_output=True, timeout=60).returncode, 0)
            again = subprocess.run([*tool, "--write"], capture_output=True, text=True, timeout=60)
            self.assertEqual(again.returncode, 0)
            (root / "site/.DS_Store").write_bytes(b"\x00")
            written = {path.name: path.read_bytes() for path in (root / ".github/workflows").iterdir()}
            for mode in ("--write", "--check"):
                refused = subprocess.run([*tool, mode], capture_output=True, text=True, timeout=60)
                self.assertEqual(refused.returncode, 2, mode)
                self.assertIn("site/.DS_Store", refused.stderr)
                self.assertEqual({path.name: path.read_bytes() for path in (root / ".github/workflows").iterdir()},
                                 written)
            (root / "site/.DS_Store").unlink()
            publish = root / ".github/workflows/publish.yml"
            publish.write_text(publish.read_text() + f'  MB_KIT_TREE_DIGEST: "{digest}"\n')
            self.assertEqual(subprocess.run([*tool, "--check"], capture_output=True, timeout=60).returncode, 2)
            self.assertNotEqual(subprocess.run([sys.executable, str(TOOL)], capture_output=True, timeout=60).returncode,
                                0, "a mode is required")


class StagedLockTests(unittest.TestCase):
    """The tool refuses to digest an index whose staged-file lock does not list its template/ and tools/."""

    def setUp(self) -> None:
        require_tools("git")
        temporary = tempfile.TemporaryDirectory(prefix="kit lock ")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.kit("kit")

    def kit(self, name: str) -> Path:
        """A staged fixture kit ``self.base/name`` carrying copies of the callee workflows."""

        root = stage(make_kit(self.base / name))
        (root / ".github/workflows").mkdir(parents=True)
        for path in CALLEE_PATHS.values():
            shutil.copyfile(path, root / ".github/workflows" / path.name)
        return root

    def tool(self, mode: str) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), mode], capture_output=True,
                              text=True, timeout=60, env=environment)

    def snapshot(self) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in (self.root / ".github/workflows").iterdir()}

    def test_the_index_listing_is_the_kits_staged_listing(self) -> None:
        self.assertEqual(update_tree_digest.indexed_staged_listing(self.root), staged_listing(self.root))
        self.assertIn(b"  ./tools/helper.sh\n", staged_listing(self.root))
        self.assertTrue(update_tree_digest.staged_lock_current(self.root))
        self.assertEqual(self.tool("--write").returncode, 0)
        self.assertEqual(self.tool("--check").returncode, 0)

    def test_a_stale_lock_fails_the_check_and_write_regenerates_it_before_digesting(self) -> None:
        self.assertEqual(self.tool("--write").returncode, 0)
        current = self.snapshot()
        lock = (self.root / STAGED_LOCK).read_bytes()
        (self.root / "tools/helper.sh").write_bytes(b"#!/bin/sh\nexit 1\n")
        stage(self.root)
        self.assertFalse(update_tree_digest.staged_lock_current(self.root))
        checked = self.tool("--check")
        self.assertEqual(checked.returncode, 1)
        self.assertIn(f"stale {STAGED_LOCK}", checked.stderr)
        self.assertEqual(((self.root / STAGED_LOCK).read_bytes(), self.snapshot()), (lock, current))
        written = self.tool("--write")
        self.assertEqual(written.returncode, 1)
        self.assertIn(f"regenerated {STAGED_LOCK}; stage it", written.stderr)
        self.assertEqual(written.stdout, "")
        self.assertEqual((self.root / STAGED_LOCK).read_bytes(), staged_listing(self.root))
        self.assertNotEqual((self.root / STAGED_LOCK).read_bytes(), lock)
        self.assertEqual(self.snapshot(), current, "no literal is written over a stale index")
        again = self.tool("--write")
        self.assertEqual(again.returncode, 1)
        self.assertIn("current but not staged", again.stderr)
        self.assertEqual(self.snapshot(), current)
        stage(self.root)
        written = self.tool("--write")
        self.assertEqual(written.returncode, 0, written.stderr)
        self.assertEqual(written.stdout, reference_digest(self.root) + "\n")
        self.assertNotEqual(self.snapshot(), current)
        self.assertEqual(self.tool("--check").returncode, 0)

    def test_the_actions_lock_is_gated_like_the_staged_lock(self) -> None:
        self.assertEqual(update_tree_digest.indexed_actions_listing(self.root), actions_listing(self.root))
        self.assertIn(b"  ./actions/setup/action.yml\n", actions_listing(self.root))
        self.assertNotIn(b"./actions/", update_tree_digest.indexed_staged_listing(self.root))
        self.assertEqual(self.tool("--write").returncode, 0)
        current = self.snapshot()
        (self.root / "actions/setup/action.yml").write_bytes(b"name: Changed\n")
        stage(self.root)
        self.assertEqual(update_tree_digest.stale_locks(self.root), [ACTIONS_LOCK])
        checked = self.tool("--check")
        self.assertEqual(checked.returncode, 1)
        self.assertIn(f"stale {ACTIONS_LOCK}", checked.stderr)
        written = self.tool("--write")
        self.assertEqual(written.returncode, 1)
        self.assertIn(f"regenerated {ACTIONS_LOCK}; stage it", written.stderr)
        self.assertEqual((self.root / ACTIONS_LOCK).read_bytes(), actions_listing(self.root))
        self.assertEqual(self.snapshot(), current, "no literal is written over a stale index")
        stage(self.root)
        self.assertEqual(self.tool("--write").returncode, 0)
        self.assertEqual(self.tool("--check").returncode, 0)
        (self.root / "actions/new").mkdir()
        (self.root / "actions/new/action.yml").write_bytes(b"name: New\n")
        refused = self.tool("--check")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("actions/new/action.yml (untracked or ignored)", refused.stderr)

    def test_a_missing_lock_is_stale(self) -> None:
        git(self.root, "rm", "-q", "-f", STAGED_LOCK)
        self.assertFalse(update_tree_digest.staged_lock_current(self.root))
        self.assertEqual(self.tool("--check").returncode, 1)
        self.assertEqual(self.tool("--write").returncode, 1)
        self.assertEqual((self.root / STAGED_LOCK).read_bytes(), staged_listing(self.root))
        stage(self.root)
        self.assertEqual(self.tool("--write").returncode, 0)

    def test_template_and_tools_must_equal_the_index(self) -> None:
        cases = {
            "template/manifest.json (not staged)": lambda: (self.root / "template/manifest.json").write_bytes(b"[]\n"),
            "tools/new.py (untracked or ignored)": lambda: (self.root / "tools/new.py").write_bytes(b"x\n"),
            "template/managed/.github/workflows/pages.yml (missing from the working tree)":
                lambda: (self.root / "template/managed/.github/workflows/pages.yml").unlink(),
        }
        for index, (label, mutate) in enumerate(cases.items()):
            with self.subTest(case=label):
                self.root = self.kit(f"case-{index}")
                mutate()
                lock = (self.root / STAGED_LOCK).read_bytes()
                before = self.snapshot()
                with self.assertRaisesRegex(update_tree_digest.DigestError, re.escape(label)):
                    update_tree_digest.staged_lock_current(self.root)
                for mode in ("--check", "--write"):
                    refused = self.tool(mode)
                    self.assertEqual(refused.returncode, 2, mode)
                    self.assertIn(label, refused.stderr)
                    self.assertEqual(((self.root / STAGED_LOCK).read_bytes(), self.snapshot()), (lock, before))

    def test_unsafe_template_or_tools_entries_are_refused(self) -> None:
        (self.root / "tools/link.sh").symlink_to("helper.sh")
        git(self.root, "add", "tools/link.sh")
        with self.assertRaisesRegex(update_tree_digest.DigestError, r"tools/link\.sh \(120000"):
            update_tree_digest.staged_lock_current(self.root)
        self.assertEqual(self.tool("--write").returncode, 2)
        git(self.root, "rm", "-q", "--cached", "tools/link.sh")
        (self.root / "tools/link.sh").unlink()
        (self.root / "tools/__pycache__").mkdir()
        (self.root / "tools/__pycache__/helper.cpython-313.pyc").write_bytes(b"\x00")
        with self.assertRaisesRegex(update_tree_digest.DigestError, r"tools/__pycache__"):
            update_tree_digest.staged_lock_current(self.root)


GIT_STUB = """
where = arguments[1] if arguments[:1] == ["-C"] else "."
rest = arguments[2:] if arguments[:1] == ["-C"] else arguments
if rest == ["rev-parse", "HEAD"]:
    print(os.environ["STUB_" + where.upper() + "_HEAD"])
elif rest == ["status", "--porcelain", "--untracked-files=all"]:
    if os.environ.get("STUB_" + where.upper() + "_STATUS_FAILS"):
        raise SystemExit("fatal: not a git repository")
    sys.stdout.write(os.environ.get("STUB_" + where.upper() + "_STATUS", ""))
else:
    raise SystemExit("unexpected git " + " ".join(arguments))
"""


class BindStepExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "find", "sort", "sha256sum", "cut")
        temporary = tempfile.TemporaryDirectory(prefix="bind step ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.workspace = self.root / "work space"
        make_kit(self.workspace / "kit")
        (self.workspace / "mod/site").mkdir(parents=True)
        (self.workspace / "mod/site/mod-base.json").write_text('{"canonical_branch": "master"}\n')
        self.script = step(callee("finalize")["jobs"]["refresh"]["steps"], PROLOGUE[3])["run"]
        self.digest = reference_digest(self.workspace / "kit")

    def invoke(self, **overrides: str):
        env_file = self.root / "github_env"
        if env_file.exists():
            env_file.unlink()
        env = {
            "KIT_SHA": "b" * 40, "GITHUB_SHA": "a" * 40, "MB_KIT_TREE_DIGEST": self.digest,
            "GITHUB_REPOSITORY": "The-Plum-Team/Quick-Skin-Mod",
            "GITHUB_WORKFLOW_REF": "The-Plum-Team/Quick-Skin-Mod/.github/workflows/pages.yml@refs/heads/master",
            "GITHUB_ENV": str(env_file), "STUB_MOD_HEAD": "a" * 40, "STUB_KIT_HEAD": "b" * 40,
            "STUB_GIT_SCRIPT": GIT_STUB, "GH_TOKEN": "must-be-unset", **overrides,
        }
        env = {name: value for name, value in env.items() if value is not None}
        result = self.harness.run(self.script, env, cwd=self.workspace)
        return result, outputs(env_file)

    def test_a_bound_identity_exports_only_the_kit_sha(self) -> None:
        result, exported = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(exported, {"MOD_BASE_KIT_SHA": "b" * 40})
        calls = [record["argv"] for record in self.harness.records()]
        self.assertEqual(calls, [["-C", "mod", "rev-parse", "HEAD"],
                                 ["-C", "mod", "status", "--porcelain", "--untracked-files=all"],
                                 ["-C", "kit", "rev-parse", "HEAD"],
                                 ["-C", "kit", "status", "--porcelain", "--untracked-files=all"]])

    def test_every_broken_binding_fails_before_export(self) -> None:
        cases = {
            "digest literal": {"MB_KIT_TREE_DIGEST": "sha256:" + "0" * 64},
            "malformed literal": {"MB_KIT_TREE_DIGEST": "sha256:xyz"},
            "malformed kit sha": {"KIT_SHA": "b" * 39, "STUB_KIT_HEAD": "b" * 39},
            "git environment": {"GIT_DIR": "/tmp/elsewhere"},
            "git config environment": {"GIT_CONFIG_PARAMETERS": "'core.fsmonitor=evil'"},
            "mod head": {"STUB_MOD_HEAD": "c" * 40},
            "kit head": {"STUB_KIT_HEAD": "c" * 40},
            "dirty mod": {"STUB_MOD_STATUS": "?? injected.py\n"},
            "dirty kit": {"STUB_KIT_STATUS": " M src/mod_base/Z.py\n"},
            "unreadable mod status": {"STUB_MOD_STATUS_FAILS": "1"},
            "unreadable kit status": {"STUB_KIT_STATUS_FAILS": "1"},
            "caller on another branch": {
                "GITHUB_WORKFLOW_REF": "The-Plum-Team/Quick-Skin-Mod/.github/workflows/pages.yml@refs/heads/dev"},
            "another workflow": {
                "GITHUB_WORKFLOW_REF": "The-Plum-Team/Quick-Skin-Mod/.github/workflows/other.yml@refs/heads/master"},
            "another repository": {"GITHUB_REPOSITORY": "attacker/fork"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                result, exported = self.invoke(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(exported, {})
        (self.workspace / "mod/site/mod-base.json").write_text('{"canonical_branch": "../x"}\n')
        result, exported = self.invoke(
            GITHUB_WORKFLOW_REF="The-Plum-Team/Quick-Skin-Mod/.github/workflows/pages.yml@refs/heads/../x")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(exported, {})

    def test_a_changed_kit_file_breaks_the_binding(self) -> None:
        (self.workspace / "kit/src/mod_base/Z.py").write_bytes(b"tampered\n")
        result, exported = self.invoke()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from the callee literal", result.stderr)
        self.assertEqual(exported, {})


if __name__ == "__main__":
    unittest.main()
