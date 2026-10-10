"""``mod_base_kit.py bump`` against real kits: activation, planning and the restored pin.

Nothing of the bootstrap is replaced. The kits it bumps between are commits of a local git
repository served bare (``KIT_REMOTE`` points at it, the API is the fake of ``test_pin``): this
kit's own tree, the same tree with a later template, the released v1.0.3 kit rebuilt from the
archived fixtures (``previous_release/v1.0.3/source.zip`` and ``released_templates/v1.0.3.zip``),
and small authored kits whose write phase fails in a chosen way. The mods are real directories
synchronized by this kit.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

from mod_base.build_ci import activation
from mod_base.build_ci.activation import ACTIVATION_PATH, CALLERS
from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import Pin, parse_pin
from mod_base.template import tool
from tests.helpers import ci_activation, ci_config
from tests.test_ci_activation import BUILD, GUARD, MANAGED, STATES, manifest
from tests.test_pin import BOOT, FakeApi, git, released, write
from tests.test_template_callers import enter, real_mod, row, tree

KIT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = KIT_ROOT / "tests" / "fixtures"
KIT_TOPS = ("src", "site", "requirements", "template", "tools", "actions")
NOTE = "\nA sentence the next release adds.\n"


def current_kit(work: Path) -> None:
    """This kit's staged directories, as a release commit holds them."""

    for top in KIT_TOPS:
        shutil.copytree(KIT_ROOT / top, work / top, ignore=shutil.ignore_patterns("__pycache__"))


def next_kit(work: Path) -> None:
    """This kit with a later managed document and a later Build caller template."""

    current_kit(work)
    for relative in ("template/managed/docs/ai/shared/REPOSITORY.md", f"template/managed/{BUILD}"):
        path = work / relative
        path.write_bytes(path.read_bytes() + (NOTE.encode() if relative.endswith(".md") else b"# a later template\n"))


def released_kit(tag: str) -> Callable[[Path], None]:
    """The released kit ``tag``: its archived ``mod_base`` package and its archived ``template/``."""

    def populate(work: Path) -> None:
        with zipfile.ZipFile(FIXTURES / "previous_release" / tag / "source.zip") as archive:
            archive.extractall(work / "src")
        with zipfile.ZipFile(FIXTURES / "released_templates" / f"{tag}.zip") as archive:
            archive.extractall(work)

    return populate


def authored_kit(main: str) -> Callable[[Path], None]:
    """A kit whose plan passes and whose ``python -m mod_base ...`` runs ``main``."""

    def populate(work: Path) -> None:
        write(work, "src/mod_base/__init__.py", "")
        write(work, "src/mod_base/errors.py", "def run_main(entry):\n    return entry()\n")
        write(work, "src/mod_base/template/__init__.py", "")
        write(work, "src/mod_base/template/tool.py", "def sync(repo, *, kit_root, write):\n    return []\n")
        write(work, "src/mod_base/__main__.py", main)

    return populate


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Send what child processes write to standard error to the null device. A kit that fails on
    purpose prints its traceback there, and a child inherits the descriptor, not ``sys.stderr``."""

    sys.stderr.flush()
    saved = os.dup(2)
    with open(os.devnull, "wb") as null:
        os.dup2(null.fileno(), 2)
    try:
        yield
    finally:
        os.dup2(saved, 2)
        os.close(saved)


class Kits:
    """One git repository holding every kit release of a test class, served as a bare clone."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir()
        self.work = root / "work"
        self.work.mkdir()
        git(self.work, "init", "-q", home=self.home)
        self.shas: dict[str, str] = {}

    def release(self, tag: str, populate: Callable[[Path], None]) -> str:
        for child in self.work.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        populate(self.work)
        git(self.work, "add", "-A", home=self.home)
        git(self.work, "commit", "-q", "-m", tag, home=self.home)
        git(self.work, "tag", tag, home=self.home)
        self.shas[tag] = git(self.work, "rev-parse", "HEAD", home=self.home)
        return self.shas[tag]

    def serve(self) -> str:
        bare = self.root / "kit.git"
        git(self.root, "clone", "-q", "--bare", str(self.work), str(bare), home=self.home)
        return bare.as_uri()


class KitsCase(unittest.TestCase):
    """A served repository of kit releases (``RELEASES``), a fresh cache and a temporary root."""

    RELEASES: dict[str, Callable[[Path], None]] = {}
    kits: Kits
    remote: str

    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mb-kits-")
        cls.addClassCleanup(directory.cleanup)
        cls.kits = Kits(Path(os.path.realpath(directory.name)))
        for tag, populate in cls.RELEASES.items():
            cls.kits.release(tag, populate)
        cls.remote = cls.kits.serve()

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mb-bump-")
        self.addCleanup(directory.cleanup)
        self.root = Path(os.path.realpath(directory.name))
        self.cache = self.root / "cache"
        self.environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        self.patch(BOOT)

    def patch(self, bootstrap: object) -> None:
        for name, value in (("KIT_REMOTE", self.remote), ("_sleep", lambda _seconds: None)):
            patcher = mock.patch.object(bootstrap, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sha(self, tag: str) -> str:
        return self.kits.shas[tag]

    def getter(self, tag: str) -> Callable[[str], object]:
        return FakeApi(released(self.sha(tag), tag)).getter()

    def checkout(self, tag: str) -> Path:
        return self.cache / "mod-base" / self.sha(tag)

    def mod(self, state: str, tag: str = "v1.1.2", name: str = "mod") -> Path:
        """A clean mod pinned to ``tag`` in ``state``, its callers written by this kit."""

        repo = real_mod(self.root / name, self.sha(tag), tag)
        enter(repo, state)
        tool.sync(repo, kit_root=KIT_ROOT, write=True)
        self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])
        return repo

    def bump(self, repo: Path, tag: str) -> object:
        return BOOT.bump(repo, tag, self.environ, get_json=self.getter(tag))


class ActivationDemandTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mb-demand-")
        self.addCleanup(directory.cleanup)
        self.root = Path(os.path.realpath(directory.name))
        self.repo = self.root / "mod"
        self.repo.mkdir()

    def test_the_bootstrap_mirrors_the_kit_names_and_bound(self) -> None:
        self.assertEqual(BOOT.ACTIVATION_PATH, activation.ACTIVATION_PATH)
        self.assertEqual(BOOT.ACTIVATION_KIND, activation.ACTIVATION_KIND)
        self.assertEqual(BOOT.ACTIVATION_DISABLED, activation.DISABLED_MODE)
        self.assertEqual(BOOT.BUILD_CONFIG_PATH, BUILD_CONFIG_PATH)
        self.assertEqual(BOOT.MAX_ACTIVATION_BYTES, limits.MAX_CI_ACTIVATION_BYTES)
        self.assertEqual(activation.MANAGED_CALLERS[BOOT.ACTIVATION_DISABLED], (),
                         "the one mode the bootstrap treats as demanding nothing manages no caller")
        self.assertTrue(all(callers for mode, callers in activation.MANAGED_CALLERS.items()
                            if mode != BOOT.ACTIVATION_DISABLED))

    def test_only_a_mode_that_manages_callers_demands_a_reader(self) -> None:
        for state in STATES:
            enter(self.repo, state)
            document = manifest(state)
            expected = None if not MANAGED[row(state)] else (1, document["mode"])
            with self.subTest(state=state):
                self.assertEqual(BOOT.activation_demand(self.repo), expected)

    def test_an_undefined_state_never_moves_the_pin(self) -> None:
        shadow = canonical_json(ci_activation("shadow"))
        outside = write(self.root, "outside.json", shadow)
        target = self.repo / ACTIVATION_PATH
        target.parent.mkdir(parents=True)
        cases = {
            "symlink": lambda: target.symlink_to(outside),
            "directory": target.mkdir,
            "fifo": lambda: os.mkfifo(target),
            "empty": lambda: target.write_bytes(b""),
            "too large": lambda: target.write_bytes(shadow + b" " * limits.MAX_CI_ACTIVATION_BYTES),
            "byte-order mark": lambda: target.write_bytes(b"\xef\xbb\xbf" + shadow),
            "duplicate mode": lambda: target.write_bytes(shadow.replace(b'"mode":"shadow"',
                                                                        b'"mode":"disabled","mode":"shadow"')),
            "array": lambda: target.write_bytes(b"[]"),
            "another kind": lambda: target.write_bytes(canonical_json(ci_config())),
            "boolean version": lambda: target.write_bytes(canonical_json({**ci_activation("shadow"),
                                                                           "schema_version": True})),
            "no mode": lambda: target.write_bytes(canonical_json({**ci_activation(), "mode": ""})),
            "numeric mode": lambda: target.write_bytes(canonical_json({**ci_activation(), "mode": 1})),
        }
        for label, create in cases.items():
            with self.subTest(label):
                create()
                with self.assertRaises(BOOT.KitError):
                    BOOT.activation_demand(self.repo)
                target.rmdir() if target.is_dir() and not target.is_symlink() else target.unlink()
        write(self.repo, BUILD_CONFIG_PATH, canonical_json(ci_config()))
        with self.assertRaisesRegex(BOOT.KitError, "exists without site/mod-base-build-activation.json"):
            BOOT.activation_demand(self.repo)
        target.write_bytes(canonical_json({**ci_activation(), "mode": "a-mode-of-a-later-kit"}))
        self.assertEqual(BOOT.activation_demand(self.repo), (1, "a-mode-of-a-later-kit"),
                         "an unknown mode is not disabled: the target kit must read and judge it")


class ActivationReaderTest(unittest.TestCase):
    """``require_activation_reader`` asks a kit directory, through a real child interpreter, which
    schema versions it writes, and applies the kit's rule: a reader accepts that version and the
    one before."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mb-reader-")
        self.addCleanup(directory.cleanup)
        self.root = Path(os.path.realpath(directory.name))
        self.kits = 0

    def refusal(self, source: str, version: int = 1) -> str | None:
        """Why a kit whose ``mod_base/__init__.py`` is ``source`` is refused for a manifest of
        schema ``version`` in the ``shadow`` mode, or ``None`` when it reads that manifest."""

        self.kits += 1
        kit = self.root / f"kit-{self.kits}"
        write(kit, "src/mod_base/__init__.py", source)
        resolution = BOOT.Resolution(kit, BOOT.Pin("a" * 40, "v9.9.9", ()), "cache", True)
        try:
            with quiet():
                BOOT.require_activation_reader(resolution, BOOT.kit_environment(resolution), (version, "shadow"))
        except BOOT.KitError as exc:
            message = str(exc)
            self.assertIn("mod-base v9.9.9 cannot keep this mod's Build/E2E callers managed", message)
            self.assertIn(f"schema_version {version} in the shadow mode", message)
            self.assertIn("pin unchanged", message)
            return message
        return None

    def test_a_kit_reads_the_version_it_writes_and_the_one_before(self) -> None:
        for written, version, reads in ((1, 1, True), (2, 1, True), (2, 2, True), (3, 2, True), (3, 3, True),
                                        (3, 1, False), (1, 2, False), (4, 2, False), (2, 7, False)):
            with self.subTest(written=written, version=version):
                refusal = self.refusal(f"SCHEMA_VERSIONS = {{'mod-base.ci.activation': {written}}}\n", version)
                if reads:
                    self.assertIsNone(refusal)
                else:
                    self.assertIn(f"that kit reads only schema_version {written} and the one before",
                                  refusal or "it was accepted")

    def test_a_kit_that_does_not_declare_the_kind_reads_no_manifest(self) -> None:
        sources = {
            "other kinds only": "SCHEMA_VERSIONS = {'mod-base.config': 1, 'mod-base.kit-stamp': 1}\n",
            "no table": "MARKER = 'one'\n",
            "a string version": "SCHEMA_VERSIONS = {'mod-base.ci.activation': '1'}\n",
            "a boolean version": "SCHEMA_VERSIONS = {'mod-base.ci.activation': True}\n",
            "a float version": "SCHEMA_VERSIONS = {'mod-base.ci.activation': 1.0}\n",
            "a list": "SCHEMA_VERSIONS = ['mod-base.ci.activation']\n",
            "not JSON": "SCHEMA_VERSIONS = {'mod-base.ci.activation': {1}}\n",
            "an import error": "raise ImportError('a broken kit')\n",
            "a silent exit": "raise SystemExit(0)\n",
            "a failing exit": "print('{\"mod-base.ci.activation\": 1}')\nraise SystemExit(3)\n",
            "a flood": "print('x' * 200000)\nSCHEMA_VERSIONS = {'mod-base.ci.activation': 1}\n",
            "a second line": "print('{}')\nSCHEMA_VERSIONS = {'mod-base.ci.activation': 1}\n",
        }
        for label, source in sources.items():
            with self.subTest(label):
                self.assertIn("that kit reads no activation manifest", self.refusal(source) or "it was accepted")

    def test_this_kit_reads_it_and_the_released_v1_0_3_kit_does_not(self) -> None:
        current = BOOT.Resolution(KIT_ROOT, BOOT.Pin("a" * 40, "v9.9.9", ()), "environment", True)
        BOOT.require_activation_reader(current, BOOT.kit_environment(current), (1, "shadow"))
        previous = self.root / "v1.0.3"
        released_kit("v1.0.3")(previous)
        resolution = BOOT.Resolution(previous, BOOT.Pin("b" * 40, "v1.0.3", ()), "cache", True)
        with self.assertRaisesRegex(BOOT.KitError, "mod-base v1.0.3 cannot keep .* in the shared-build mode, and that "
                                                   "kit reads no activation manifest"):
            BOOT.require_activation_reader(resolution, BOOT.kit_environment(resolution), (1, "shared-build"))


class BumpTest(KitsCase):
    RELEASES = {"v1.0.3": released_kit("v1.0.3"), "v1.1.2": current_kit, "v1.1.3": next_kit}

    def test_a_bump_moves_every_pin_and_resynchronizes_the_callers_of_every_state(self) -> None:
        for index, state in enumerate(STATES):
            with self.subTest(state=state):
                repo = self.mod(state, name=f"mod-{index}")
                result = self.bump(repo, "v1.1.3")
                pin = parse_pin(repo)
                self.assertEqual((result.sha, result.version, pin.sha, pin.version),
                                 (self.sha("v1.1.3"), "v1.1.3", self.sha("v1.1.3"), "v1.1.3"))
                kit = self.checkout("v1.1.3")
                expected = tool.expected_callers(kit, Pin(pin.sha, pin.version, ()), manifest(state),
                                                 tool.canonical_branch(repo))
                for path in CALLERS:
                    if path in MANAGED[row(state)]:
                        self.assertEqual((repo / path).read_bytes(), expected[path])
                    else:
                        self.assertFalse((repo / path).exists())
                if BUILD in MANAGED[row(state)]:
                    self.assertTrue((repo / BUILD).read_bytes().endswith(b"# a later template\n"))
                    self.assertIn(f'MB_KIT_SHA: "{pin.sha}"'.encode(), (repo / GUARD).read_bytes())
                self.assertTrue((repo / "docs/ai/shared/REPOSITORY.md").read_text(encoding="utf-8").endswith(NOTE))
                self.assertNotIn(self.sha("v1.1.2").encode(), b"".join(tree(repo / ".github").values()))
                self.assertEqual(tool.check(repo, kit_root=kit), [])

    def test_a_rollback_below_activation_is_refused_while_callers_are_managed(self) -> None:
        for index, state in enumerate(STATES[2:]):
            with self.subTest(state=state):
                repo = self.mod(state, name=f"mod-{index}")
                before = tree(repo)
                with self.assertRaises(BOOT.KitError) as caught:
                    self.bump(repo, "v1.0.3")
                message = str(caught.exception)
                self.assertIn("mod-base v1.0.3 cannot keep this mod's Build/E2E callers managed", message)
                self.assertIn(f"in the {state.partition(':')[0]} mode", message)
                self.assertIn("that kit reads no activation manifest", message)
                self.assertIn("pin unchanged", message)
                self.assertEqual(tree(repo), before)

    def test_a_rollback_below_activation_is_allowed_where_no_caller_is_managed(self) -> None:
        for state in ("absent", "disabled"):
            with self.subTest(state=state):
                repo = self.mod(state, name=f"mod-{state}")
                kept = {path: (repo / path).read_bytes() for path in (ACTIVATION_PATH, BUILD_CONFIG_PATH)
                        if (repo / path).exists()}
                self.assertEqual(self.bump(repo, "v1.0.3").sha, self.sha("v1.0.3"))
                kit = self.checkout("v1.0.3")
                self.assertEqual((repo / "scripts/ci/mod_base_kit.py").read_bytes(),
                                 (kit / "template/managed/scripts/ci/mod_base_kit.py").read_bytes())
                self.assertNotEqual((repo / "scripts/ci/mod_base_kit.py").read_bytes(), BOOT_BYTES)
                self.assertEqual({path: (repo / path).read_bytes() for path in kept}, kept)
                self.assertFalse(any((repo / path).exists() for path in CALLERS))
                checked = subprocess.run([sys.executable, "-P", "-m", "mod_base", "template", "check", "--repo", str(repo)],
                                         env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(kit / "src"),
                                              "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"},
                                         capture_output=True, text=True, timeout=120, check=False)
                self.assertEqual((checked.returncode, checked.stdout), (0, ""), checked.stderr)

    def test_a_plan_the_new_kit_rejects_changes_nothing(self) -> None:
        repo = self.mod("shadow")
        pages = repo / ".github/workflows/pages.yml"
        pages.write_bytes(pages.read_bytes().replace(b"# <<< mod-local extensions\n", b""))
        before = tree(repo)
        with self.assertRaisesRegex(BOOT.KitError, "template sync planning failed with exit 2; pin unchanged"):
            self.bump(repo, "v1.1.3")
        self.assertEqual(tree(repo), before)

    def test_a_malformed_manifest_stops_the_bump_before_any_request(self) -> None:
        repo = self.mod("shadow")
        (repo / ACTIVATION_PATH).write_bytes(b"{")
        before = tree(repo)
        api = FakeApi(released(self.sha("v1.1.3"), "v1.1.3"))
        with self.assertRaises(BOOT.KitError):
            BOOT.bump(repo, "v1.1.3", self.environ, get_json=api.getter())
        self.assertEqual((api.calls, tree(repo), self.cache.exists()), ([], before, False))

    def test_a_refused_bump_is_one_line_and_exit_two_on_the_command_line(self) -> None:
        repo = self.mod("shared-build-and-e2e")
        before = tree(repo)
        responses = released(self.sha("v1.0.3"), "v1.0.3")
        stderr = io.StringIO()
        with mock.patch.object(BOOT, "api_getter", lambda _environ: FakeApi(responses).getter()), \
                mock.patch.dict(os.environ, self.environ), contextlib.redirect_stderr(stderr):
            code = BOOT.main(["bump", "--repo", str(repo), "--to", "v1.0.3"])
        self.assertEqual(code, 2)
        self.assertEqual(stderr.getvalue().count("\n"), 1)
        self.assertTrue(stderr.getvalue().startswith("mod_base_kit: error: mod-base v1.0.3 cannot keep this mod's "
                                                     "Build/E2E callers managed"), stderr.getvalue())
        self.assertEqual(tree(repo), before)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_a_write_phase_that_fails_restores_and_removes_every_workflow_file(self) -> None:
        repo = self.mod("shadow")
        (repo / ".github/workflows/pages.yml").unlink()
        before = tree(repo)
        shared = repo / "docs/ai/shared"
        os.chmod(shared, 0o555)
        self.addCleanup(os.chmod, shared, 0o755)
        with self.assertRaises(BOOT.KitError) as caught:
            self.bump(repo, "v1.1.3")
        self.assertIn("template sync --write failed", str(caught.exception))
        self.assertIn("every workflow and action file was restored", str(caught.exception))
        self.assertEqual(tree(repo), before)
        self.assertEqual(parse_pin(repo).sha, self.sha("v1.1.2"))


#: A write phase that edits, creates and then fails: what a restore must undo.
MEDDLING_MAIN = """import pathlib, sys
repo = pathlib.Path(sys.argv[sys.argv.index("--repo") + 1])
(repo / ".github/workflows/e2e.yml").write_text("rewritten\\n")
(repo / ".github/workflows/created.yml").write_text("created\\n")
(repo / ".github/actions/new").mkdir(parents=True)
(repo / ".github/actions/new/action.yml").write_text("created\\n")
(repo / ".github/workflows/pages.yml").unlink()
raise SystemExit(2)
"""
#: A write phase that fails and leaves the workflow directory read-only, so the restore fails too.
LOCKING_MAIN = """import os, pathlib, sys
repo = pathlib.Path(sys.argv[sys.argv.index("--repo") + 1])
os.chmod(repo / ".github/workflows", 0o555)
raise SystemExit(2)
"""
BOOT_BYTES = (KIT_ROOT / "template/managed/scripts/ci/mod_base_kit.py").read_bytes()


class RestoreTest(KitsCase):
    RELEASES = {"v1.1.2": current_kit, "v2.0.0": authored_kit(MEDDLING_MAIN), "v2.0.1": authored_kit(LOCKING_MAIN)}

    def test_whatever_a_failed_write_phase_did_to_the_pin_files_is_undone(self) -> None:
        repo = self.mod("absent")
        write(repo, ".github/actions/local/action.yml",
              f"runs:\r\n  steps:\r\n      - uses: The-Plum-Team/mod-base/actions/setup@{self.sha('v1.1.2')} # v1.1.2\r\n")
        before = tree(repo)
        with self.assertRaisesRegex(BOOT.KitError, "sync --write failed with exit 2; every workflow and action file "
                                                   "was restored"):
            self.bump(repo, "v2.0.0")
        self.assertEqual(tree(repo), before)
        self.assertFalse((repo / ".github/actions/new/action.yml").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_a_restore_that_fails_says_so(self) -> None:
        repo = self.mod("absent")
        self.addCleanup(os.chmod, repo / ".github/workflows", 0o755)
        with self.assertRaisesRegex(BOOT.KitError, "sync --write failed with exit 2; restoring the workflow and "
                                                   "action files failed too .*restore .github from version control"):
            self.bump(repo, "v2.0.1")

    def test_restore_pin_files_rewrites_recreates_and_removes(self) -> None:
        repo = self.mod("shared-build")
        before = BOOT.read_pin_files(repo)
        snapshot = tree(repo)
        BOOT.restore_pin_files(repo, before)
        self.assertEqual(tree(repo), snapshot)
        (repo / BUILD).write_bytes(b"changed\n")
        (repo / GUARD).unlink()
        write(repo, ".github/workflows/added.yaml", "added\n")
        write(repo, ".github/actions/a/b/action.yml", "added\n")
        write(repo, ".github/actions/a/notes.md", "not a pin file\n")
        BOOT.restore_pin_files(repo, before)
        self.assertEqual(tree(repo), {**snapshot, ".github/actions/a/notes.md": b"not a pin file\n"})


if __name__ == "__main__":
    unittest.main()
