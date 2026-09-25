"""The kit pin, kit-digest-v1, the stamp, kit resolution, the managed bootstrap and the release
order that pin verification imposes on the kit's own documentation (MB9).

``mod_base.pin`` and the stdlib-only bootstrap ``template/managed/scripts/ci/mod_base_kit.py`` run
over the same inputs and must agree. Fetches go to a local bare repository (``KIT_REMOTE`` is
patched); the GitHub API is a fake; nothing touches the network or writes outside temporary
directories.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli, pin
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiNotFound

KIT_ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_PATH = KIT_ROOT / "template" / "managed" / "scripts" / "ci" / "mod_base_kit.py"
SHA_A = "a" * 40
SHA_B = "b" * 40


def load_bootstrap() -> Any:
    spec = importlib.util.spec_from_file_location("mod_base_kit_under_test", BOOTSTRAP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BOOT = load_bootstrap()


def pin_line(path: str = "actions/setup", sha: str = SHA_A, version: str = "v1.2.3", indent: str = "      - ") -> str:
    return f"{indent}uses: The-Plum-Team/mod-base/{path}@{sha} # {version}"


def write(root: Path, relative: str, data: str | bytes, mode: int = 0o644) -> Path:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    os.chmod(target, mode)
    return target


def git_environment(home: Path) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_TERMINAL_PROMPT": "0"})
    return environment


def git(cwd: Path, *arguments: str, home: Path) -> str:
    command = ["git", "-c", "user.name=mod-base test", "-c", "user.email=test@example.invalid",
               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", f"core.hooksPath={os.devnull}",
               "-c", "init.defaultBranch=main", *arguments]
    return subprocess.run(command, cwd=cwd, env=git_environment(home), check=True, capture_output=True,
                          text=True).stdout.strip()


def kit_tree(root: Path, marker: str = "one") -> Path:
    """A minimal kit root whose ``src/`` carries the staged-file lock of its template/ and tools/."""

    write(root, "src/mod_base/__init__.py", f"MARKER = {marker!r}\n")
    write(root, "src/mod_base/__main__.py", "import sys\nprint('kit', *sys.argv[1:])\n")
    write(root, "site/index.html", "<!doctype html>\n")
    write(root, "requirements/pillow.txt", "Pillow==12.3.0\n")
    write(root, "template/manifest.json", "{}\n")
    write(root, "tools/kit_digest.sh", "#!/bin/sh\n", 0o755)
    write(root, pin.STAGED_LOCK, pin.staged_listing(root))
    return root


def mod_repo(root: Path, sha: str = SHA_A, version: str = "v1.2.3") -> Path:
    write(root, ".github/workflows/pages.yml",
          "# >>> mod-base managed: pages caller v1 — edit only in The-Plum-Team/mod-base template/managed/x\n"
          "jobs:\n"
          "  publish:\n"
          f"    uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@{sha} # {version}\n"
          "  rotate:\n"
          f"    uses: The-Plum-Team/mod-base/.github/workflows/rotate.yml@{sha} # {version}\n")
    write(root, ".github/workflows/e2e.yml",
          "jobs:\n  evidence:\n    steps:\n"
          "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"
          f"{pin_line('actions/prepare-evidence', sha, version)}\n")
    return root


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="mb-pin-")
        self.root = Path(os.path.realpath(self._directory.name))
        self.home = self.root / "home"
        self.home.mkdir()
        environment = mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.addCleanup(self._directory.cleanup)


# -- Parser ------------------------------------------------------------------------------------------


def parse_both(files: dict[str, bytes]) -> tuple[Any, Any]:
    """``(mod_base result, bootstrap result)``; each is a Pin tuple or the exception class name."""

    results = []
    for parse, error in ((pin.parse_pin_files, MbError), (BOOT.parse_pin_files, BOOT.KitError)):
        try:
            found = parse(files)
        except error:
            results.append("error")
        else:
            results.append((found.sha, found.version, found.references))
    return results[0], results[1]


VALID = {
    ".github/workflows/pages.yml": "\n".join([
        "# >>> mod-base managed: pages caller v1 — edit only in The-Plum-Team/mod-base template/managed/x",
        "          | select(.path | startswith(\"The-Plum-Team/mod-base/.github/workflows/\"))",
        "          reach=\"$(retry \"repos/The-Plum-Team/mod-base/compare/$kit_sha...main\")\"",
        "          grep -oE 'The-Plum-Team/mod-base/[^@[:space:]]+@[0-9a-f]{40}' | sort -u",
        f"    uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@{SHA_A} # v1.2.3",
        f"    uses: The-Plum-Team/mod-base/.github/workflows/finalize.yml@{SHA_A} # v1.2.3  ",
        "",
    ]).encode(),
    ".github/actions/local/action.yml": f"runs:\n  steps:\n{pin_line('actions/setup')}\n".encode(),
}


class ParserTest(unittest.TestCase):
    def test_single_pin_and_sorted_references(self) -> None:
        mine, theirs = parse_both(VALID)
        self.assertEqual(mine, theirs)
        self.assertEqual(mine, (SHA_A, "v1.2.3", (".github/actions/local/action.yml@3",
                                                   ".github/workflows/pages.yml@5",
                                                   ".github/workflows/pages.yml@6")))

    def test_crlf_and_list_dash_forms(self) -> None:
        files = {".github/workflows/a.yml": f"jobs:\r\n{pin_line()}\r\n    uses: The-Plum-Team/mod-base/"
                                             f".github/workflows/rotate.yml@{SHA_A} # v1.2.3\r\n".encode()}
        mine, theirs = parse_both(files)
        self.assertEqual(mine, theirs)
        self.assertEqual(mine[2], (".github/workflows/a.yml@2", ".github/workflows/a.yml@3"))

    def test_rejections_agree(self) -> None:
        illegal = {
            "no pin": "jobs: {}\n",
            "two shas": f"{pin_line()}\n{pin_line('actions/notify-pages', SHA_B)}\n",
            "two versions": f"{pin_line()}\n{pin_line('actions/notify-pages', version='v1.2.4')}\n",
            "non-kit path": f"{pin_line('scripts/ci/x')}\n",
            "workflow without yml": f"{pin_line('.github/workflows/publish')}\n",
            "tag reference": f"{pin_line()}\n      - uses: The-Plum-Team/mod-base/actions/setup@v1.2.3\n",
            "branch reference": f"{pin_line()}\n    uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@main\n",
            "case variant": f"{pin_line()}\n      - uses: the-plum-team/MOD-BASE/actions/setup@{SHA_A} # v1.2.3\n",
            "missing version": f"{pin_line()}\n      - uses: The-Plum-Team/mod-base/actions/setup@{SHA_A}\n",
            "quoted": f"{pin_line()}\n      - uses: \"The-Plum-Team/mod-base/actions/setup@{SHA_A}\" # v1.2.3\n",
            "flow mapping": f"{pin_line()}\n      - {{uses: The-Plum-Team/mod-base/actions/setup@{SHA_A}}}\n",
            "checkout of the kit": f"{pin_line()}\n          repository: The-Plum-Team/mod-base\n",
            "uses repository only": f"{pin_line()}\n      - uses: The-Plum-Team/mod-base\n",
            "escaped": f"{pin_line()}\n      - uses: \"The-Plum-Team\\x2fmod-base/actions/setup@{SHA_A}\"\n",
            "block scalar": f"{pin_line()}\n      - uses: >-\n          The-Plum-Team\n",
            "unterminated quote": f"{pin_line()}\n      - uses: \"The-Plum-Team/mod-\n          base/actions/x\"\n",
            "unicode space": f"{pin_line()}\n      - uses: The-Plum-Team/mod-base/actions/setup@{SHA_A} # v1.2.3\n",
            "short sha": f"{pin_line()}\n      - uses: The-Plum-Team/mod-base/actions/setup@{SHA_A[:12]} # v1.2.3\n",
            "leading zero": f"{pin_line(version='v1.02.3')}\n",
            "commented reference": f"{pin_line()}\n#      - uses: The-Plum-Team/mod-base/actions/setup@{SHA_B} # v1.2.3\n",
            # A second, unverified kit checkout: the kit repository named without a path.
            "flow checkout": f"{pin_line()}\n        with: {{repository: The-Plum-Team/mod-base, ref: main}}\n",
            "expression string": f"{pin_line()}\n          repository: ${{{{ 'The-Plum-Team/mod-base' }}}}\n",
            "plain next line": f"{pin_line()}\n          repository:\n            The-Plum-Team/mod-base\n",
            "folded value": f"{pin_line()}\n          repository: >-\n            The-Plum-Team/mod-base\n",
            "git suffix": f"{pin_line()}\n          repository: The-Plum-Team/mod-base.git\n",
            "escaped repository": f"{pin_line()}\n          repository: \"The-Plum-Team/\\x6dod-base\"\n",
            "escaped line break": f"{pin_line()}\n          repository: \"The-Plum-Team/mo\\\n            d-base\"\n",
            # A second composite at a fork SHA spelled so that no line holds the literal token.
            "escaped key and value": (f"{pin_line()}\n      - \"u\\x73es\": "
                                      f"\"The-Plum-Team/mod-bas\\x65/actions/setup@{SHA_B}\"\n"),
            "value on the next line": (f"{pin_line()}\n      - uses:\n"
                                       f"          \"The-Plum-Team/mod-bas\\x65/actions/notify-pages@{SHA_B}\"\n"),
            "explicit key": (f"{pin_line()}\n      - ? uses\n"
                             f"        : \"The-Plum-Team/mod-bas\\u0065/actions/setup@{SHA_B}\"\n"),
            "unicode escape": f"{pin_line()}\n      - uses: \"The-Plum-Team/mod-bas\\U00000065/actions/setup@{SHA_B}\"\n",
            "escaped uses key": f"{pin_line()}\n      - {{\"u\\x73es\": ./local}}\n",
            "empty uses": f"{pin_line()}\n      - uses:\n          ./.github/actions/local\n",
            "tagged uses": f"{pin_line()}\n      - uses: !!str ./.github/actions/local\n",
            "aliased uses": f"{pin_line()}\n      - uses: *step\n",
        }
        for label, text in illegal.items():
            with self.subTest(label):
                mine, theirs = parse_both({".github/workflows/x.yml": text.encode()})
                self.assertEqual(mine, "error")
                self.assertEqual(theirs, "error")

    def test_non_utf8_is_rejected(self) -> None:
        self.assertEqual(parse_both({".github/workflows/x.yml": pin_line().encode() + b"\n\xff\n"}),
                         ("error", "error"))

    def test_plain_uses_lines_are_accepted(self) -> None:
        text = "\n".join([
            pin_line(),
            "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
            "        uses: ./.github/actions/run-packaged-e2e",
            "    uses: './.github/workflows/build-matrix.yml'",
            "      statuses: write",
            "# The-Plum-Team/mod-base is the shared kit (a comment-only mention).",
            "        run: |",
            "          grep -c 'uses:' .github/workflows/x.yml",
            '          echo "uses: $(jq -r .uses step.json)"',
            '          : "${GITHUB_TOKEN:?}"',
            "          printf 'a\\x41\\n' \\",
            '            | gh api "repos/The-Plum-Team/mod-base/compare/$sha...main" \\',
            "            --jq .status",
            "",
        ])
        mine, theirs = parse_both({".github/workflows/x.yml": text.encode()})
        self.assertEqual(mine, theirs)
        self.assertEqual(mine[0], SHA_A)


class DiscoveryTest(TempCase):
    def test_workflows_yaml_and_nested_actions(self) -> None:
        write(self.root, ".github/workflows/a.yaml", pin_line() + "\n")
        write(self.root, ".github/actions/one/two/three/four/five/action.yaml", pin_line() + "\n")
        write(self.root, ".github/actions/one/notes.md", "The-Plum-Team/mod-base@anything is not scanned\n")
        found = pin.parse_pin(self.root)
        self.assertEqual(found.references, (".github/actions/one/two/three/four/five/action.yaml@1",
                                            ".github/workflows/a.yaml@1"))
        self.assertEqual(BOOT.parse_pin(self.root).references, found.references)

    def test_symlinks_and_missing_pin_are_rejected(self) -> None:
        write(self.root, ".github/workflows/real.yml", pin_line() + "\n")
        cases = []
        link = self.root / ".github" / "workflows" / "link.yml"
        link.symlink_to(self.root / ".github" / "workflows" / "real.yml")
        cases.append(("symlinked workflow", link))
        for label, created in cases:
            with self.subTest(label):
                with self.assertRaises(MbError):
                    pin.parse_pin(self.root)
                with self.assertRaises(BOOT.KitError):
                    BOOT.parse_pin(self.root)
            created.unlink()
        target = self.root / "elsewhere"
        target.mkdir()
        write(target, "action.yml", pin_line("actions/notify-pages", SHA_B) + "\n")
        (self.root / ".github" / "actions").mkdir()
        (self.root / ".github" / "actions" / "linked").symlink_to(target)
        with self.assertRaises(MbError):
            pin.parse_pin(self.root)
        with self.assertRaises(BOOT.KitError):
            BOOT.parse_pin(self.root)

    def test_repository_without_workflows_has_no_pin(self) -> None:
        with self.assertRaisesRegex(MbError, "no mod-base pin"):
            pin.parse_pin(self.root)
        with self.assertRaisesRegex(BOOT.KitError, "no mod-base pin"):
            BOOT.parse_pin(self.root)


# -- kit-digest-v1 -------------------------------------------------------------------------------------


def expected_digest(root: Path) -> str:
    records = []
    for top in pin.DIGESTED_DIRS:
        for path in sorted((root / top).rglob("*")):
            if path.is_file():
                records.append((f"./{path.relative_to(root).as_posix()}",
                                hashlib.sha256(path.read_bytes()).hexdigest()))
    listing = "".join(f"{digest}  {path}\n" for path, digest in sorted(records))
    return "sha256:" + hashlib.sha256(listing.encode()).hexdigest()


class DigestTest(TempCase):
    def test_both_implementations_compute_kit_digest_v1(self) -> None:
        kit = kit_tree(self.root / "kit")
        write(kit, "src/mod_base/a-b/c_d.1.py", "x = 1\n")
        value = pin.kit_tree_digest(kit)
        self.assertEqual(value, expected_digest(kit))
        self.assertEqual(BOOT.tree_digest(kit), value)
        write(kit, "tools/other.txt", "not digested\n")
        self.assertEqual(pin.kit_tree_digest(kit), value)
        write(kit, "site/index.html", "changed\n")
        self.assertNotEqual(pin.kit_tree_digest(kit), value)

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash and sha256sum")
    def test_the_shell_digest_agrees(self) -> None:
        script = KIT_ROOT / "tools" / "kit_digest.sh"
        if not script.is_file():
            self.fail("tools/kit_digest.sh (MB8) is missing")
        kit = kit_tree(self.root / "kit")
        os.chmod(kit / "tools" / "kit_digest.sh", 0o644)
        result = subprocess.run(["bash", str(script), str(kit)], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), pin.kit_tree_digest(kit))

    def test_unsafe_trees_are_refused(self) -> None:
        mutations = {
            "symlink": lambda kit: (kit / "site" / "link").symlink_to(kit / "site" / "index.html"),
            "executable": lambda kit: os.chmod(kit / "site" / "index.html", 0o755),
            "bytecode": lambda kit: write(kit, "src/mod_base/__pycache__/x.pyc", b"\0"),
            "bad name": lambda kit: write(kit, "site/a b.html", "x"),
            "missing site": lambda kit: shutil.rmtree(kit / "site"),
            "fifo": lambda kit: os.mkfifo(kit / "requirements" / "pipe"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                kit = kit_tree(self.root / f"kit-{label.replace(' ', '-')}")
                mutate(kit)
                with self.assertRaises(MbError):
                    pin.kit_tree_digest(kit)
                with self.assertRaises(BOOT.KitError):
                    BOOT.tree_digest(kit)

    def test_staged_listing_binds_template_and_tools(self) -> None:
        kit = kit_tree(self.root / "kit")
        listing = pin.staged_listing(kit)
        self.assertEqual(BOOT.staged_listing(kit), listing)
        manifest, script = (hashlib.sha256(data).hexdigest() for data in (b"{}\n", b"#!/bin/sh\n"))
        self.assertEqual(listing.decode().splitlines(), [f"{manifest}  ./template/manifest.json",
                                                         f"{script}  ./tools/kit_digest.sh"])
        pin.verify_staged_files(kit)
        BOOT.verify_staged_files(kit)
        mutations = {
            "edited manifest": lambda root: write(root, "template/manifest.json", '{"files": []}\n'),
            "added tool": lambda root: write(root, "tools/extra.py", "x = 1\n"),
            "removed tool": lambda root: (root / "tools" / "kit_digest.sh").unlink(),
            "bytecode": lambda root: write(root, "tools/__pycache__/x.pyc", b"\0"),
            "missing lock": lambda root: (root / pin.STAGED_LOCK).unlink(),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                broken = kit_tree(self.root / f"kit-{label.replace(' ', '-')}")
                mutate(broken)
                with self.assertRaises(MbError):
                    pin.verify_staged_files(broken)
                with self.assertRaises(BOOT.KitError):
                    BOOT.verify_staged_files(broken)


# -- Stamp ---------------------------------------------------------------------------------------------


class StampTest(TempCase):
    def stamp(self, document: Any) -> Path:
        directory = self.root / "stamp"
        directory.mkdir(exist_ok=True)
        text = document if isinstance(document, str) else json.dumps(document)
        write(directory, pin.STAMP_NAME, text)
        return directory

    def test_valid_stamp(self) -> None:
        document = {"kind": "mod-base.kit-stamp", "schema_version": 1, "sha": SHA_A, "version": "1.2.3",
                    "tree_digest": "sha256:" + "0" * 64}
        directory = self.stamp(document)
        self.assertEqual(pin.read_stamp(directory), document)
        self.assertEqual(BOOT.read_stamp(directory), document)
        self.assertEqual(BOOT.stamp_document(BOOT.Pin(SHA_A, "v1.2.3", ()), document["tree_digest"]), document)
        self.assertEqual(pin.stamp_document(pin.Pin(SHA_A, "v1.2.3", ()), document["tree_digest"]), document)

    def test_invalid_stamps_are_refused(self) -> None:
        base = {"kind": "mod-base.kit-stamp", "schema_version": 1, "sha": SHA_A, "version": "1.2.3",
                "tree_digest": "sha256:" + "0" * 64}
        cases = {
            "unknown key": {**base, "extra": 1},
            "missing key": {key: value for key, value in base.items() if key != "tree_digest"},
            "v prefix": {**base, "version": "v1.2.3"},
            "short sha": {**base, "sha": "a" * 39},
            "trailing newline": {**base, "sha": SHA_A + "\n"},
            "bool version": {**base, "schema_version": True},
            "kind": {**base, "kind": "mod-base.other"},
            "duplicate": '{"kind":"mod-base.kit-stamp","kind":"mod-base.kit-stamp","schema_version":1}',
            "nan": '{"schema_version": NaN}',
        }
        for label, document in cases.items():
            with self.subTest(label):
                directory = self.stamp(document)
                with self.assertRaises(MbError):
                    pin.read_stamp(directory)
                with self.assertRaises(BOOT.KitError):
                    BOOT.read_stamp(directory)


# -- Network verification ----------------------------------------------------------------------------


class FakeApi:
    """The only three kit reads ``verify --network`` makes, keyed by path."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.calls.append((path, params))
        if path not in self.responses:
            raise ApiNotFound(f"GET {path}: 404", status=404, method="GET", path=path)
        return self.responses[path]

    def getter(self) -> Any:
        def get(path: str) -> Any:
            bare, _, query = path.partition("?")
            self.calls.append((bare, query))
            if bare not in self.responses:
                raise BOOT.HttpError(f"GET {bare} failed with HTTP 404", 404)
            return self.responses[bare]
        return get


def released(sha: str, version: str, *, status: str = "ahead", behind: int = 0, annotated: bool = False,
             tagged: str | None = None) -> dict[str, Any]:
    commit = {"type": "commit", "sha": tagged or sha}
    responses: dict[str, Any] = {
        f"/repos/The-Plum-Team/mod-base/compare/{sha}...main": {"status": status, "behind_by": behind,
                                                                "ahead_by": 3},
    }
    if annotated:
        responses[f"/repos/The-Plum-Team/mod-base/git/ref/tags/{version}"] = {
            "ref": f"refs/tags/{version}", "object": {"type": "tag", "sha": "c" * 40}}
        responses[f"/repos/The-Plum-Team/mod-base/git/tags/{'c' * 40}"] = {"object": commit}
    else:
        responses[f"/repos/The-Plum-Team/mod-base/git/ref/tags/{version}"] = {"ref": f"refs/tags/{version}",
                                                                            "object": commit}
    return responses


class VerifyNetworkTest(TempCase):
    def check_both(self, responses: dict[str, Any], sha: str = SHA_A, version: str = "v1.2.3") -> tuple[bool, bool]:
        outcomes = []
        api = FakeApi(responses)
        try:
            pin.verify_released(pin.Pin(sha, version, ()), api)
            outcomes.append(True)
        except MbError:
            outcomes.append(False)
        try:
            BOOT.verify_released(BOOT.Pin(sha, version, ()), FakeApi(responses).getter())
            outcomes.append(True)
        except BOOT.KitError:
            outcomes.append(False)
        return outcomes[0], outcomes[1]

    def test_released_pins_pass(self) -> None:
        for label, responses in {"ahead": released(SHA_A, "v1.2.3"),
                                 "identical": released(SHA_A, "v1.2.3", status="identical"),
                                 "annotated": released(SHA_A, "v1.2.3", annotated=True)}.items():
            with self.subTest(label):
                self.assertEqual(self.check_both(responses), (True, True))

    def test_comparison_is_requested_with_one_commit_per_page(self) -> None:
        api = FakeApi(released(SHA_A, "v1.2.3"))
        pin.verify_released(pin.Pin(SHA_A, "v1.2.3", ()), api)
        self.assertEqual(api.calls[0], (f"/repos/The-Plum-Team/mod-base/compare/{SHA_A}...main", {"per_page": 1}))
        fake = FakeApi(released(SHA_A, "v1.2.3"))
        BOOT.verify_released(BOOT.Pin(SHA_A, "v1.2.3", ()), fake.getter())
        self.assertEqual(fake.calls[0][1], "per_page=1")

    def test_impostor_and_tag_mismatches_fail(self) -> None:
        compare = f"/repos/The-Plum-Team/mod-base/compare/{SHA_A}...main"
        cases = {
            "diverged fork commit": released(SHA_A, "v1.2.3", status="diverged", behind=2),
            "behind": released(SHA_A, "v1.2.3", status="behind", behind=1),
            "behind_by not int": {**released(SHA_A, "v1.2.3"), compare: {"status": "ahead", "behind_by": "0"}},
            "not in the repository": {key: value for key, value in released(SHA_A, "v1.2.3").items() if key != compare},
            "tag names another commit": released(SHA_A, "v1.2.3", tagged=SHA_B),
            "tag missing": {compare: {"status": "ahead", "behind_by": 0}},
            "tag ref mismatch": {**released(SHA_A, "v1.2.3"),
                                 "/repos/The-Plum-Team/mod-base/git/ref/tags/v1.2.3": {"ref": "refs/tags/v1.2.30",
                                                                                     "object": {"type": "commit",
                                                                                                "sha": SHA_A}}},
            "tag peels to a tree": {**released(SHA_A, "v1.2.3"),
                                    "/repos/The-Plum-Team/mod-base/git/ref/tags/v1.2.3": {
                                        "ref": "refs/tags/v1.2.3", "object": {"type": "tree", "sha": SHA_A}}},
        }
        for label, responses in cases.items():
            with self.subTest(label):
                self.assertEqual(self.check_both(responses), (False, False))

    def test_verify_requires_a_client_for_network(self) -> None:
        mod_repo(self.root)
        self.assertEqual(pin.verify(self.root, network=False).sha, SHA_A)
        with self.assertRaises(MbError):
            pin.verify(self.root, network=True)
        with self.assertRaises(BOOT.KitError):
            BOOT.verify(self.root, network=True)
        self.assertEqual(pin.verify(self.root, network=True, api=FakeApi(released(SHA_A, "v1.2.3"))).version, "v1.2.3")


# -- Release procedure -------------------------------------------------------------------------------

DOCS = KIT_ROOT / "docs"
NUMBERED_STEP = re.compile(r"(\d+)\. ")


def markdown_section(path: Path, heading: str) -> str:
    """The body of the Markdown section ``heading`` (for example ``## Rollout``) of ``path``, up to
    the next heading of the same or a higher level."""

    text = path.read_text(encoding="utf-8")
    marker = f"\n{heading}\n"
    start = text.index(marker) + len(marker)
    level = len(heading) - len(heading.lstrip("#"))
    following = re.search(rf"^#{{1,{level}}} ", text[start:], re.MULTILINE)
    return text[start:start + following.start()] if following else text[start:]


def numbered_steps(section: str) -> list[str]:
    """The top-level ``1.``, ``2.``... items of ``section`` with their indented continuations; the
    list ends at the first unindented line that is not the next item."""

    steps: list[str] = []
    for line in section.splitlines():
        match = NUMBERED_STEP.match(line)
        if match:
            if int(match.group(1)) != len(steps) + 1:
                raise AssertionError(f"step {match.group(1)} follows step {len(steps)}")
            steps.append(line)
        elif steps and (not line.strip() or line.startswith("   ")):
            steps[-1] += "\n" + line
        elif steps:
            break
    return steps


def first_step(steps: list[str], pattern: str) -> int:
    """The index of the first step matching ``pattern``."""

    for index, step in enumerate(steps):
        if re.search(pattern, step):
            return index
    raise AssertionError(f"no step matches {pattern!r}")


class ReleaseProcedureTest(unittest.TestCase):
    """The kit's release order (SPEC §9.3 S1 and S3): merge, green CI, tag, then the canary pinned
    to the tag, then the GitHub Release.

    The canary cannot run before its tag exists: the canary procedure checks out the tag, and its
    ``verify --network`` (like ``bump`` and ``stage``) refuses a pin whose tag is missing or peels
    elsewhere (``VerifyNetworkTest``, "tag missing"). Every tag also needs its own commit, because
    a handoff records the executing ``__version__`` and authentication requires it to equal the
    pin's ``# vX.Y.Z``."""

    def test_operations_tags_before_the_canary_and_publishes_the_release_after_it(self) -> None:
        steps = numbered_steps(markdown_section(DOCS / "OPERATIONS.md", "## Releasing mod-base"))
        green = first_step(steps, r"`mod-base CI`")
        tag = first_step(steps, r"git tag -a vX\.Y\.Z")
        canary = first_step(steps, r"\[Canary procedure\]\(#canary-procedure\)")
        release = first_step(steps, r"gh release create vX\.Y\.Z")
        self.assertLess(green, tag)
        self.assertLess(tag, canary)
        self.assertLess(canary, release)
        self.assertEqual([step for step in steps[:tag] if re.search(r"(?i)canary", step)], [])

    def test_the_canary_procedure_starts_from_the_pushed_tag(self) -> None:
        steps = numbered_steps(markdown_section(DOCS / "OPERATIONS.md", "## Canary procedure"))
        self.assertIn("(#releasing-mod-base) step 3", steps[0])
        self.assertIn('switch --detach "$TAG"', steps[0])
        self.assertIn('must print "$PIN $TAG"', steps[2])
        self.assertIn("mod_base_kit.py verify --network", steps[2])

    def test_the_agent_guide_tags_before_the_canary(self) -> None:
        steps = numbered_steps(markdown_section(DOCS / "ai" / "KIT.md", "## Releases"))
        green = first_step(steps, r"`mod-base CI`")
        tag = first_step(steps, r"tag `vX\.Y\.Z`")
        canary = first_step(steps, r"(?i)canary")
        release = first_step(steps, r"GitHub Release")
        self.assertLess(green, tag)
        self.assertLess(tag, canary)
        self.assertLess(canary, release)

    def test_the_readme_release_line_tags_before_the_canary(self) -> None:
        section = markdown_section(KIT_ROOT / "README.md", "## Pins and bumps")
        releases = [item for item in section.split("\n- ") if item.lstrip("- ").startswith("Release:")]
        self.assertEqual(len(releases), 1)
        line = releases[0]
        self.assertLess(line.index("tag"), line.index("canary"))
        self.assertLess(line.index("canary"), line.index("GitHub Release"))
        self.assertIn("#releasing-mod-base", line)

    def test_mods_pin_a_tag_only_after_its_canary(self) -> None:
        roots = markdown_section(DOCS / "SECURITY-MODEL.md", "## Trust roots")
        rows = [row for row in roots.splitlines() if row.startswith("| mod-base `v*` tags |")]
        self.assertEqual(len(rows), 1)
        self.assertIn("mods pin a tag only after the canary, pinned to it, passes", rows[0])

    def test_every_tag_names_a_commit_whose_version_is_the_tag(self) -> None:
        steps = numbered_steps(markdown_section(DOCS / "OPERATIONS.md", "## Releasing mod-base"))
        tag = steps[first_step(steps, r"git tag -a vX\.Y\.Z")]
        self.assertIn('__version__ = "X.Y.Z"', tag)
        self.assertLess(tag.index('__version__ = "X.Y.Z"'), tag.index("git tag -a"))
        rollout = markdown_section(DOCS / "OPERATIONS.md", "## Rollout")
        stage = [row for row in rollout.splitlines() if row.startswith("| S3 |")]
        self.assertEqual(len(stage), 1)
        self.assertIn("own release commit", stage[0])
        for path in (DOCS / "OPERATIONS.md", DOCS / "ai" / "KIT.md", KIT_ROOT / "README.md"):
            with self.subTest(path.name):
                self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?i)v1\.0\.0[^\n]*same commit")


# -- Kit resolution ----------------------------------------------------------------------------------


class KitSource:
    """A local kit repository with two commits (tagged v1.2.3 and v1.2.4) and a bare clone."""

    def __init__(self, root: Path, home: Path) -> None:
        self.home = home
        self.work = kit_tree(root / "kit-src")
        os.chmod(self.work / "tools" / "kit_digest.sh", 0o644)
        write(self.work, ".gitignore", "*.egg-info/\n")
        git(self.work, "init", "-q", home=home)
        git(self.work, "add", "-A", home=home)
        git(self.work, "commit", "-q", "-m", "one", home=home)
        git(self.work, "tag", "v1.2.3", home=home)
        self.first = git(self.work, "rev-parse", "HEAD", home=home)
        write(self.work, "src/mod_base/__init__.py", "MARKER = 'two'\n")
        write(self.work, "src/mod_base/__main__.py",
              "import json, os, sys\n"
              "record = os.environ.get('MOD_BASE_TEST_RECORD')\n"
              "if record:\n"
              "    with open(record, 'w', encoding='utf-8') as stream:\n"
              "        json.dump({'argv': sys.argv[1:], 'pythonpath': os.environ.get('PYTHONPATH'),\n"
              "                   'kit_sha': os.environ.get('MOD_BASE_KIT_SHA')}, stream)\n")
        git(self.work, "add", "-A", home=home)
        git(self.work, "commit", "-q", "-m", "two", home=home)
        git(self.work, "tag", "v1.2.4", home=home)
        self.second = git(self.work, "rev-parse", "HEAD", home=home)
        self.bare = root / "kit.git"
        git(root, "clone", "-q", "--bare", str(self.work), str(self.bare), home=home)
        self.remote = self.bare.as_uri()


class ResolutionTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = KitSource(self.root, self.home)
        self.repo = mod_repo(self.root / "mod", self.source.first)
        self.cache = self.root / "cache"
        for module in (pin, BOOT):
            patcher = mock.patch.object(module, "KIT_REMOTE", self.source.remote)
            patcher.start()
            self.addCleanup(patcher.stop)
            sleeper = mock.patch.object(module, "_sleep", lambda _seconds: None)
            sleeper.start()
            self.addCleanup(sleeper.stop)

    def both(self, environ: dict[str, str], repo: Path | None = None) -> tuple[Any, Any]:
        """``(pin result, bootstrap result)``: a resolved path or ``"error"``."""

        repo = repo or self.repo
        results = []
        try:
            results.append(pin.kit_path(repo, environ))
        except Unavailable:
            results.append("error")
        try:
            results.append(BOOT.kit_path(repo, environ))
        except BOOT.KitError:
            results.append("error")
        return results[0], results[1]

    def stage_overlay(self, repo: Path, sha: str, *, digest_from: Path | None = None) -> Path:
        kit = kit_tree(self.root / f"staged-{sha[:6]}")
        overlay = repo / "out" / "mod-base-kit"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        BOOT.copy_kit(kit, overlay)
        digest = BOOT.tree_digest(digest_from or overlay)
        write(overlay, pin.STAMP_NAME, BOOT.canonical_json(BOOT.stamp_document(BOOT.Pin(sha, "v1.2.3", ()), digest)))
        return overlay

    def test_overlay_wins_over_everything(self) -> None:
        overlay = self.stage_overlay(self.repo, self.source.first)
        environ = {"MOD_BASE_KIT_PATH": str(self.root / "elsewhere"), "MOD_BASE_KIT_SHA": "0" * 40, "CI": "true",
                   "MOD_BASE_CACHE_DIR": str(self.cache)}
        self.assertEqual(self.both(environ), (overlay, overlay))
        self.assertFalse(self.cache.exists())

    def test_planted_bytecode_in_an_overlay_is_refused(self) -> None:
        """Python loads an unchecked-hash .pyc in place of the digest-verified source."""

        overlay = self.stage_overlay(self.repo, self.source.first)
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache), "CI": "true"}
        self.assertEqual(self.both(environ), (overlay, overlay))
        planted = overlay / "src" / "mod_base" / "__pycache__" / f"__init__.{sys.implementation.cache_tag}.pyc"
        planted.parent.mkdir()
        source = self.root / "planted.py"
        source.write_text("MARKER = 'planted'\n")
        py_compile.compile(str(source), cfile=str(planted), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.assertEqual(self.both(environ), ("error", "error"))

    def test_an_overlay_with_unbound_template_files_is_refused(self) -> None:
        """kit-digest-v1 does not cover template/ and tools/: the src/ lock does."""

        overlay = self.stage_overlay(self.repo, self.source.first)
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        write(overlay, "template/manifest.json", '{"kind": "rewritten"}\n')
        self.assertEqual(self.both(environ), ("error", "error"))
        write(overlay, "template/manifest.json", "{}\n")
        self.assertEqual(self.both(environ), (overlay, overlay))
        write(overlay, "template/managed/extra.md", "added\n")
        self.assertEqual(self.both(environ), ("error", "error"))

    def test_an_invalid_overlay_never_falls_through(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        overlay = self.stage_overlay(self.repo, SHA_B)
        self.assertEqual(self.both(environ), ("error", "error"))
        shutil.rmtree(overlay)
        overlay = self.stage_overlay(self.repo, self.source.first)
        write(overlay, "site/index.html", "tampered\n")
        self.assertEqual(self.both(environ), ("error", "error"))
        (overlay / pin.STAMP_NAME).unlink()
        self.assertEqual(self.both(environ), ("error", "error"))
        self.assertFalse(self.cache.exists())

    def test_kit_path_turns_bytecode_writing_off(self) -> None:
        overlay = self.stage_overlay(self.repo, self.source.first)
        original = sys.dont_write_bytecode
        self.addCleanup(setattr, sys, "dont_write_bytecode", original)
        for resolve_path in (lambda: pin.kit_path(self.repo, {}), lambda: BOOT.kit_path(self.repo, {})):
            sys.dont_write_bytecode = False
            self.assertEqual(resolve_path(), overlay)
            self.assertTrue(sys.dont_write_bytecode)

    def test_environment_path_requires_the_pinned_sha(self) -> None:
        kit = kit_tree(self.root / "env-kit")
        pinned = {"MOD_BASE_KIT_PATH": str(kit), "MOD_BASE_KIT_SHA": self.source.first, "CI": "true"}
        self.assertEqual(self.both(pinned), (kit, kit))
        self.assertEqual(self.both({**pinned, "MOD_BASE_KIT_SHA": SHA_B}), ("error", "error"))
        self.assertEqual(self.both({**pinned, "MOD_BASE_KIT_PATH": str(self.root)}), ("error", "error"))

    def test_unpinned_override_is_a_local_developer_convenience(self) -> None:
        kit = kit_tree(self.root / "dev-kit")
        allowed = {"MOD_BASE_KIT_PATH": str(kit), "MOD_BASE_ALLOW_UNPINNED": "1"}
        self.assertEqual(self.both(allowed), (kit, kit))
        for refused in ({"MOD_BASE_KIT_PATH": str(kit)}, {**allowed, "CI": "true"}, {**allowed, "CI": ""},
                        {**allowed, "GITHUB_ACTIONS": "true"}, {**allowed, "MOD_BASE_ALLOW_UNPINNED": "yes"}):
            with self.subTest(refused):
                self.assertEqual(self.both(refused), ("error", "error"))

    def test_fetch_then_cache(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        destination = self.cache / "mod-base" / self.source.first
        root, found, source = pin.resolve(self.repo, environ)
        self.assertEqual((root, found.sha, source), (destination, self.source.first, "fetch"))
        self.assertEqual((destination / "src" / "mod_base" / "__init__.py").read_text(), "MARKER = 'one'\n")
        self.assertEqual(pin.resolve(self.repo, environ)[2], "cache")
        resolution = BOOT.resolve(self.repo, environ)
        self.assertEqual((resolution.root, resolution.source, resolution.pinned), (destination, "cache", True))
        self.assertEqual([path.name for path in destination.parent.iterdir()], [self.source.first])

    def test_bootstrap_fetch_matches(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        resolution = BOOT.resolve(self.repo, environ)
        self.assertEqual((resolution.source, resolution.root), ("fetch", self.cache / "mod-base" / self.source.first))
        self.assertEqual(pin.resolve(self.repo, environ)[2], "cache")

    def test_a_modified_cache_is_refused(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        destination = pin.kit_path(self.repo, environ)
        write(destination, "src/mod_base/injected.py", "x = 1\n")
        self.assertEqual(self.both(environ), ("error", "error"))
        (destination / "src" / "mod_base" / "injected.py").unlink()
        self.assertEqual(self.both(environ), (destination, destination))
        planted = write(destination, "src/mod_base/__pycache__/__init__.cpython-313.pyc", b"\0")
        self.assertEqual(self.both(environ), ("error", "error"))
        shutil.rmtree(planted.parent)
        git(destination, "checkout", "-q", "--detach", self.source.first, home=self.home)
        write(destination, "site/index.html", "edited\n")
        self.assertEqual(self.both(environ), ("error", "error"))

    def test_ignore_rules_cannot_hide_an_added_file(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        destination = pin.kit_path(self.repo, environ)
        injected = write(destination, "src/mod_base.egg-info/sitecustomize.py", "raise SystemExit\n")
        self.assertEqual(self.both(environ), ("error", "error"))
        injected.unlink()
        injected.parent.rmdir()
        excludes = write(self.home, "global-excludes", "sitecustomize.py\n*.pyd\n")
        write(self.home, ".gitconfig", f"[core]\n\texcludesFile = {excludes}\n")
        write(destination, "src/sitecustomize.py", "raise SystemExit\n")
        self.assertEqual(self.both(environ), ("error", "error"))
        (destination / "src" / "sitecustomize.py").unlink()
        write(destination, "src/mod_base/errors.pyd", b"\0")
        self.assertEqual(self.both(environ), ("error", "error"))
        (destination / "src" / "mod_base" / "errors.pyd").unlink()
        self.assertEqual(self.both(environ), (destination, destination))

    def test_global_git_configuration_is_ignored(self) -> None:
        write(self.home, ".gitconfig", f'[url "{(self.root / "nowhere").as_uri()}/"]\n'
                                       f"\tinsteadOf = {self.source.remote}\n")
        for index, module in enumerate((pin, BOOT)):
            with self.subTest(module.__name__):
                environ = {"MOD_BASE_CACHE_DIR": str(self.root / f"cache-{index}")}
                self.assertEqual(module.resolve(self.repo, environ)[2], "fetch")

    def test_a_cache_at_another_commit_is_refused(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        destination = self.cache / "mod-base" / self.source.first
        destination.parent.mkdir(parents=True)
        git(self.root, "clone", "-q", str(self.source.bare), str(destination), home=self.home)
        self.assertEqual(self.both(environ), ("error", "error"))

    def test_an_unreachable_pin_fails_after_bounded_attempts(self) -> None:
        repo = mod_repo(self.root / "impostor", SHA_B)
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache)}
        self.assertEqual(self.both(environ, repo), ("error", "error"))
        leftovers = [path.name for path in (self.cache / "mod-base").iterdir()]
        self.assertEqual(leftovers, [])

    def test_the_cache_never_lives_inside_the_repository(self) -> None:
        self.assertEqual(self.both({"MOD_BASE_CACHE_DIR": str(self.repo / "cache")}), ("error", "error"))
        self.assertEqual(self.both({"MOD_BASE_CACHE_DIR": "relative/cache"}), ("error", "error"))

    def test_platform_cache_locations(self) -> None:
        home = str(self.home)
        with mock.patch.object(pin.sys, "platform", "linux"), mock.patch.object(pin.os, "name", "posix"):
            self.assertEqual(pin.cache_root({"HOME": home}, self.repo), self.home / ".cache" / "mod-base")
            self.assertEqual(pin.cache_root({"HOME": home, "XDG_CACHE_HOME": str(self.root / "x")}, self.repo),
                             self.root / "x" / "mod-base")
        with mock.patch.object(pin.sys, "platform", "darwin"):
            self.assertEqual(pin.cache_root({"HOME": home}, self.repo), self.home / "Library" / "Caches" / "mod-base")
        with mock.patch.object(BOOT.sys, "platform", "darwin"):
            self.assertEqual(BOOT.cache_root({"HOME": home}, self.repo), self.home / "Library" / "Caches" / "mod-base")


# -- stage, bump and the bootstrap CLI ----------------------------------------------------------------


class KitSourceCase(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.source = KitSource(self.root, self.home)
        self.cache = self.root / "cache"
        patcher = mock.patch.object(BOOT, "KIT_REMOTE", self.source.remote)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeper = mock.patch.object(BOOT, "_sleep", lambda _seconds: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.controller = mod_repo(self.root / "controller", self.source.first, "v1.2.3")


class StageTest(KitSourceCase):
    def test_equal_pins_stage_the_controller_verified_kit(self) -> None:
        candidate = mod_repo(self.root / "candidate", self.source.first, "v1.2.3")
        action = kit_tree(self.root / "action-kit")
        os.chmod(action / "tools" / "kit_digest.sh", 0o644)
        write(action, "src/mod_base/__pycache__/x.pyc", b"\0")
        environ = {"MOD_BASE_KIT_PATH": str(action), "MOD_BASE_KIT_SHA": self.source.first, "CI": "true"}
        output = self.root / "runner-temp" / "mod-base-kit"
        output.parent.mkdir()
        self.assertEqual(BOOT.stage(self.controller, candidate, output, environ), output)
        self.assertEqual(sorted(path.name for path in output.iterdir()),
                         ["MOD_BASE_KIT.json", "requirements", "site", "src", "template", "tools"])
        self.assertFalse((output / "src" / "mod_base" / "__pycache__").exists())
        stamp = pin.read_stamp(output)
        self.assertEqual((stamp["sha"], stamp["version"]), (self.source.first, "1.2.3"))
        self.assertEqual(stamp["tree_digest"], pin.kit_tree_digest(output))
        shutil.copytree(output, candidate / "out" / "mod-base-kit")
        overlay = candidate / "out" / "mod-base-kit"
        self.assertEqual(pin.resolve(candidate, {"CI": "true"}), (overlay, pin.parse_pin(candidate), "overlay"))
        self.assertEqual(BOOT.resolve(candidate, {"CI": "true"}).source, "overlay")

    def test_a_different_released_pin_is_fetched_verified_and_staged(self) -> None:
        candidate = mod_repo(self.root / "candidate", self.source.second, "v1.2.4")
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache), "CI": "true"}
        output = self.root / "staged"
        getter = FakeApi(released(self.source.second, "v1.2.4")).getter()
        BOOT.stage(self.controller, candidate, output, environ, get_json=getter)
        self.assertEqual(pin.read_stamp(output)["sha"], self.source.second)
        self.assertEqual((output / "src" / "mod_base" / "__init__.py").read_text(), "MARKER = 'two'\n")

    def test_unreleased_or_unparseable_candidates_are_refused(self) -> None:
        environ = {"MOD_BASE_CACHE_DIR": str(self.cache), "CI": "true"}
        candidate = mod_repo(self.root / "candidate", self.source.second, "v1.2.4")
        getter = FakeApi(released(self.source.second, "v1.2.4", status="diverged", behind=1)).getter()
        with self.assertRaisesRegex(BOOT.KitError, f"mod-base kit unavailable for candidate pin {self.source.second}"):
            BOOT.stage(self.controller, candidate, self.root / "out1", environ, get_json=getter)
        self.assertFalse((self.root / "out1").exists())
        broken = self.root / "broken"
        write(broken, ".github/workflows/x.yml", "uses: The-Plum-Team/mod-base/actions/setup@main\n")
        with self.assertRaisesRegex(BOOT.KitError, "candidate pin <unparseable>"):
            BOOT.stage(self.controller, broken, self.root / "out2", environ, get_json=getter)
        relabelled = mod_repo(self.root / "relabelled", self.source.first, "v9.9.9")
        with self.assertRaises(BOOT.KitError):
            BOOT.stage(self.controller, relabelled, self.root / "out3", environ, get_json=getter)

    def test_staging_refuses_existing_outputs_and_links(self) -> None:
        candidate = mod_repo(self.root / "candidate", self.source.first, "v1.2.3")
        action = kit_tree(self.root / "action-kit")
        environ = {"MOD_BASE_KIT_PATH": str(action), "MOD_BASE_KIT_SHA": self.source.first}
        existing = self.root / "exists"
        existing.mkdir()
        with self.assertRaises(BOOT.KitError):
            BOOT.stage(self.controller, candidate, existing, environ)
        (action / "template" / "linked").symlink_to(action / "site")
        with self.assertRaises(BOOT.KitError):
            BOOT.stage(self.controller, candidate, self.root / "fresh", environ)
        self.assertFalse((self.root / "fresh").exists())

    def test_a_source_whose_template_does_not_match_its_lock_is_not_staged(self) -> None:
        candidate = mod_repo(self.root / "candidate", self.source.first, "v1.2.3")
        action = kit_tree(self.root / "action-kit")
        write(action, "template/manifest.json", '{"kind": "rewritten"}\n')
        environ = {"MOD_BASE_KIT_PATH": str(action), "MOD_BASE_KIT_SHA": self.source.first}
        output = self.root / "staged"
        with self.assertRaisesRegex(BOOT.KitError, f"unavailable for candidate pin {self.source.first}.*template"):
            BOOT.stage(self.controller, candidate, output, environ)
        self.assertFalse(output.exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_operating_system_errors_report_the_unavailable_pin(self) -> None:
        candidate = mod_repo(self.root / "candidate", self.source.first, "v1.2.3")
        action = kit_tree(self.root / "action-kit")
        environ = {"MOD_BASE_KIT_PATH": str(action), "MOD_BASE_KIT_SHA": self.source.first}
        locked = self.root / "locked"
        locked.mkdir()
        os.chmod(locked, 0o555)
        self.addCleanup(os.chmod, locked, 0o755)
        with self.assertRaisesRegex(BOOT.KitError, f"^mod-base kit unavailable for candidate pin {self.source.first}; "
                                                   "a controller upgrade must pin a released mod-base commit"):
            BOOT.stage(self.controller, candidate, locked / "mod-base-kit", environ)
        with mock.patch.dict(os.environ, environ), contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = BOOT.main(["stage", "--controller-repo", str(self.controller), "--candidate-repo", str(candidate),
                              "--output", str(locked / "kit")])
        self.assertEqual(code, 2)
        self.assertIn(f"mod-base kit unavailable for candidate pin {self.source.first}", stderr.getvalue())


class BumpTest(KitSourceCase):
    def test_bump_rewrites_every_pin_and_syncs_from_the_new_kit(self) -> None:
        repo = self.controller
        write(repo, ".github/actions/local/action.yml",
              f"runs:\r\n  steps:\r\n{pin_line('actions/setup', self.source.first, 'v1.2.3')}\r\n")
        record = self.root / "record.json"
        getter = FakeApi(released(self.source.second, "v1.2.4")).getter()
        with mock.patch.dict(os.environ, {"MOD_BASE_TEST_RECORD": str(record)}):
            result = BOOT.bump(repo, "v1.2.4", {"MOD_BASE_CACHE_DIR": str(self.cache)}, get_json=getter)
        self.assertEqual((result.sha, result.version), (self.source.second, "v1.2.4"))
        self.assertEqual(pin.parse_pin(repo).references, (".github/actions/local/action.yml@3",
                                                          ".github/workflows/e2e.yml@5",
                                                          ".github/workflows/pages.yml@4",
                                                          ".github/workflows/pages.yml@6"))
        action = (repo / ".github/actions/local/action.yml").read_bytes()
        self.assertIn(b"\r\n", action)
        self.assertIn(f"@{self.source.second} # v1.2.4\r\n".encode(), action)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1",
                      (repo / ".github/workflows/e2e.yml").read_text())
        seen = json.loads(record.read_text())
        self.assertEqual(seen["argv"], ["template", "sync", "--repo", str(repo), "--write"])
        self.assertEqual(seen["pythonpath"], str(self.cache / "mod-base" / self.source.second / "src"))
        self.assertEqual(seen["kit_sha"], self.source.second)

    def test_bump_fetches_the_new_kit_before_editing(self) -> None:
        repo = self.controller
        before = {path: path.read_bytes() for path in repo.rglob("*.yml")}
        unknown = "c" * 40
        with self.assertRaisesRegex(BOOT.KitError, "cannot fetch"):
            BOOT.bump(repo, "v1.2.4", {"MOD_BASE_CACHE_DIR": str(self.cache)},
                      get_json=FakeApi(released(unknown, "v1.2.4")).getter())
        self.assertEqual({path: path.read_bytes() for path in repo.rglob("*.yml")}, before)

    def test_bump_to_an_unverified_tag_changes_nothing(self) -> None:
        repo = self.controller
        before = {path: path.read_bytes() for path in repo.rglob("*.yml")}
        for label, responses in {"missing": {}, "impostor": released(self.source.second, "v1.2.4", status="diverged",
                                                                    behind=4)}.items():
            with self.subTest(label):
                with self.assertRaises(BOOT.KitError):
                    BOOT.bump(repo, "v1.2.4", {"MOD_BASE_CACHE_DIR": str(self.cache)},
                              get_json=FakeApi(responses).getter())
                self.assertEqual({path: path.read_bytes() for path in repo.rglob("*.yml")}, before)
        with self.assertRaises(BOOT.KitError):
            BOOT.bump(repo, "1.2.4", {}, get_json=FakeApi({}).getter())


class UncleanPathsTest(unittest.TestCase):
    def test_every_entry_is_unclean_bytecode_included(self) -> None:
        cases = {
            "": [],
            "?? src/mod_base/__pycache__/a.cpython-313.pyc\0": ["src/mod_base/__pycache__/a.cpython-313.pyc"],
            "!! src/mod_base/__pycache__/\0?? tools/__pycache__/x.pyc\0": ["src/mod_base/__pycache__/",
                                                                          "tools/__pycache__/x.pyc"],
            "?? src/mod_base/new.py\0": ["src/mod_base/new.py"],
            "!! src/foo.egg-info/\0": ["src/foo.egg-info/"],
            " M src/mod_base/cli.py\0": ["src/mod_base/cli.py"],
            "R  new.py\0old.py\0?? a/b.pyc\0": ["new.py", "a/b.pyc"],
        }
        for status, expected in cases.items():
            with self.subTest(status):
                self.assertEqual(pin.unclean_paths(status), expected)
                self.assertEqual(BOOT.unclean_paths(status), expected)


class BootstrapApiClientTest(unittest.TestCase):
    """The bootstrap's own GET client against a loopback server (never the network)."""

    def setUp(self) -> None:
        import http.server
        import threading

        self.requests: list[tuple[str, str | None]] = []
        self.script: list[tuple[int, dict[str, str], bytes]] = []
        test = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                test.requests.append((self.path, self.headers.get("Authorization")))
                status, headers, body = test.script.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_arguments: Any) -> None:
                return None

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        for name, value in (("API_ROOT", f"http://127.0.0.1:{self.server.server_address[1]}"),
                            ("_sleep", lambda _seconds: None)):
            patcher = mock.patch.object(BOOT, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_retries_transient_failures_then_decodes_strictly(self) -> None:
        self.script = [(503, {}, b""), (502, {}, b""), (200, {}, b'{"status":"ahead"}')]
        get = BOOT.api_getter({"GH_TOKEN": "t0ken"})
        self.assertEqual(get("/repos/x"), {"status": "ahead"})
        self.assertEqual(self.requests, [("/repos/x", "Bearer t0ken")] * 3)

    def test_rate_limits_wait_for_the_advertised_time(self) -> None:
        slept: list[float] = []
        for name, value in (("_sleep", slept.append), ("_now", lambda: 1000.0)):
            patcher = mock.patch.object(BOOT, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        get = BOOT.api_getter({})
        self.script = [(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}, b"{}"),
                       (403, {"Retry-After": "7"}, b"{}"),
                       (429, {"Retry-After": "600"}, b"{}"),
                       (200, {}, b"{}")]
        with mock.patch.object(BOOT, "API_ATTEMPTS", 4):
            self.assertEqual(get("/repos/limited"), {})
        self.assertEqual(slept, [30.0, 7.0, BOOT.RATE_LIMIT_WAIT_CAP_SECONDS])
        slept.clear()
        self.script = [(403, {}, b"{}")]
        with self.assertRaises(BOOT.HttpError):
            get("/repos/forbidden")
        self.assertEqual(slept, [])

    def test_permanent_errors_redirects_and_bad_bodies_fail(self) -> None:
        get = BOOT.api_getter({})
        self.script = [(404, {}, b"{}")]
        with self.assertRaises(BOOT.HttpError) as caught:
            get("/repos/missing")
        self.assertEqual(caught.exception.status, 404)
        self.script = [(302, {"Location": "https://example.invalid/elsewhere"}, b"")]
        with self.assertRaises(BOOT.HttpError):
            get("/repos/moved")
        self.script = [(200, {}, b'{"a":1,"a":2}')]
        with self.assertRaisesRegex(BOOT.KitError, "duplicate JSON key"):
            get("/repos/duplicate")
        self.script = [(503, {}, b"")] * BOOT.API_ATTEMPTS
        with self.assertRaises(BOOT.HttpError):
            get("/repos/down")
        self.assertEqual([authorization for _, authorization in self.requests], [None] * (3 + BOOT.API_ATTEMPTS))
        with self.assertRaises(BOOT.KitError):
            BOOT.api_getter({"GH_TOKEN": "bad token\n"})


class BootstrapCliTest(TempCase):
    def environment(self, **extra: str) -> dict[str, str]:
        environment = {name: value for name, value in os.environ.items()
                       if name not in ("CI", "GITHUB_ACTIONS", "MOD_BASE_KIT_PATH", "MOD_BASE_KIT_SHA",
                                       "MOD_BASE_ALLOW_UNPINNED", "PYTHONPATH")}
        environment.update({"PYTHONDONTWRITEBYTECODE": "1", **extra})
        return environment

    def run_bootstrap(self, *arguments: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(BOOTSTRAP_PATH), *arguments], env=self.environment(**extra),
                              capture_output=True, text=True, timeout=120, check=False)

    def test_pin_verify_and_run(self) -> None:
        repo = mod_repo(self.root / "mod")
        kit = kit_tree(self.root / "kit")
        self.assertEqual(self.run_bootstrap("pin", "--repo", str(repo)).stdout, f"{SHA_A} v1.2.3\n")
        self.assertEqual(self.run_bootstrap("verify", "--repo", str(repo)).stdout, f"{SHA_A} v1.2.3\n")
        developer = {"MOD_BASE_KIT_PATH": str(kit), "MOD_BASE_ALLOW_UNPINNED": "1"}
        for arguments in (("--", "template", "check", "--repo", "."), ("template", "check", "--repo", ".")):
            with self.subTest(arguments):
                result = self.run_bootstrap("run", "--repo", str(repo), *arguments, **developer)
                self.assertEqual((result.returncode, result.stdout), (0, "kit template check --repo .\n"))
        refused = self.run_bootstrap("run", "--repo", str(repo), "--", "x", CI="true", **developer)
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(refused.stderr.count("\n"), 1)
        self.assertIn("developer override", refused.stderr)
        self.assertEqual(self.run_bootstrap("run", "--repo", str(repo), **developer).returncode, 2)
        path = self.run_bootstrap("path", "--repo", str(repo), **developer)
        self.assertEqual(path.stdout, f"{kit}\n")

    def test_errors_are_one_line_exit_two(self) -> None:
        result = self.run_bootstrap("pin", "--repo", str(self.root))
        self.assertEqual(result.returncode, 2)
        self.assertTrue(result.stderr.startswith("mod_base_kit: error: no mod-base pin"))
        self.assertEqual(result.stderr.count("\n"), 1)

    def test_default_repository_is_the_one_holding_the_bootstrap(self) -> None:
        repo = mod_repo(self.root / "mod")
        write(repo, "scripts/ci/mod_base_kit.py", BOOTSTRAP_PATH.read_bytes())
        result = subprocess.run([sys.executable, str(repo / "scripts/ci/mod_base_kit.py"), "pin"],
                                env=self.environment(), capture_output=True, text=True, timeout=60, cwd=self.root,
                                check=False)
        self.assertEqual(result.stdout, f"{SHA_A} v1.2.3\n")


class BootstrapSourceTest(unittest.TestCase):
    def test_bootstrap_is_stdlib_only_and_python_311_compatible(self) -> None:
        source = BOOTSTRAP_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 11))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertLessEqual(imported, set(sys.stdlib_module_names) | {"__future__"})
        self.assertNotIn("mod_base", imported)
        self.assertEqual(os.stat(BOOTSTRAP_PATH).st_mode & 0o111, 0, "managed files are mode 0644 in every mod")

    def test_bootstrap_and_kit_share_the_pin_grammar(self) -> None:
        self.assertEqual(BOOT.PIN_LINE.pattern, pin.PIN_LINE.pattern)
        self.assertEqual(BOOT.PIN_LINE.flags, pin.PIN_LINE.flags)
        for name in ("KIT_PATH", "KIT_REFERENCE", "KIT_REPOSITORY_TOKEN", "KIT_REPOSITORY_BARE", "KIT_VALUE",
                     "USES_KEY", "USES_VALUE", "ESCAPE", "SIMPLE_ESCAPES", "TAG", "KIT_PATH_NAME", "DIGESTED_DIRS",
                     "LOCKED_DIRS", "STAGED_LOCK", "STAMP_NAME", "GIT_SAFETY", "STATUS_ARGUMENTS", "BYTECODE_DIRECTORY",
                     "MAX_PIN_FILES", "MAX_PIN_FILE_BYTES", "MAX_PIN_TOTAL_BYTES", "MAX_ACTION_ENTRIES",
                     "MAX_KIT_FILES", "MAX_KIT_BYTES", "MAX_LOCK_BYTES", "MAX_TAG_PEELS", "FETCH_ATTEMPTS",
                     "GIT_TIMEOUT_SECONDS"):
            with self.subTest(name):
                ours, theirs = getattr(pin, name), getattr(BOOT, name)
                self.assertEqual(getattr(ours, "pattern", ours), getattr(theirs, "pattern", theirs))
        self.assertEqual("/".join(BOOT.OVERLAY_PATH), pin.OVERLAY_PATH)
        self.assertEqual(BOOT._git_environment(Path("/c")), pin._git_environment(Path("/c")))
        self.assertEqual(BOOT.KIT_REPOSITORY, pin.KIT_TOKEN)
        self.assertEqual(BOOT.KIT_REMOTE, pin.KIT_REMOTE)
        self.assertLessEqual(set(BOOT.LOCKED_DIRS) | set(BOOT.DIGESTED_DIRS), set(BOOT.STAGED_DIRS))

    def test_the_kit_carries_a_current_staged_file_lock(self) -> None:
        """template/ and tools/ reach the sandbox overlay outside kit-digest-v1; src/ binds them."""

        recorded = (KIT_ROOT / pin.STAGED_LOCK).read_bytes()
        self.assertEqual(pin.staged_listing(KIT_ROOT), recorded,
                         f"{pin.STAGED_LOCK} is stale: run PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 "
                         "python3 -m mod_base.template.lock --write, then tools/update_tree_digest.py --write")
        self.assertEqual(BOOT.staged_listing(KIT_ROOT), recorded)


class PinCommandsTest(TempCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_pin_verify_and_digest(self) -> None:
        repo = mod_repo(self.root / "mod")
        self.assertEqual(self.run_cli("pin", "verify", "--repo", str(repo))[:2], (0, f"{SHA_A} v1.2.3\n"))
        broken = self.root / "broken"
        write(broken, ".github/workflows/x.yml", pin_line() + "\n" + pin_line(sha=SHA_B) + "\n")
        self.assertEqual(self.run_cli("pin", "verify", "--repo", str(broken))[0], 2)
        kit = kit_tree(self.root / "kit")
        value = pin.kit_tree_digest(kit)
        self.assertEqual(self.run_cli("digest", "--root", str(kit))[:2], (0, f"{value}\n"))
        self.assertEqual(self.run_cli("digest", "--root", str(kit), "--check", value)[0], 0)
        code, _, stderr = self.run_cli("digest", "--root", str(kit), "--check", "sha256:" + "0" * 64)
        self.assertEqual(code, 2)
        self.assertIn("does not equal", stderr)
        self.assertEqual(self.run_cli("digest", "--root", str(kit), "--check", "sha256:xyz")[0], 2)

    def test_network_verify_uses_a_read_only_kit_client(self) -> None:
        repo = mod_repo(self.root / "mod")
        seen: list[Any] = []

        def fake_verify(path: Path, *, network: bool, api: Any = None) -> pin.Pin:
            seen.append(api)
            return pin.Pin(SHA_A, "v1.2.3", ())

        with mock.patch("mod_base.pin_commands.verify", fake_verify), \
                mock.patch("mod_base.cli.environ", lambda: {"GH_TOKEN": "t0ken"}):
            self.assertEqual(self.run_cli("pin", "verify", "--repo", str(repo), "--network")[0], 0)
        self.assertEqual((seen[0].repository, seen[0].writable), ("The-Plum-Team/mod-base", False))


if __name__ == "__main__":
    unittest.main()
