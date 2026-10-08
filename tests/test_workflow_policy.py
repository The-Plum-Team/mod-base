"""Textual policy of the kit-owned workflows and composites (SPEC §1.2, §1.4, §5.3-5.6, §5.10).

The module also holds the strict YAML-subset reader and the shell-execution helpers the other
``tests/test_workflow_*.py``, ``test_managed_caller.py``, ``test_composite_credential_boundary.py``
and ``test_tree_digest_literal.py`` modules import (functions only, so no test runs twice). The
reader understands exactly the constructs the kit YAML uses (block mappings and sequences, plain,
quoted and flow scalars, ``|``/``>-`` block scalars) and raises on anything else, so a policy test
can never silently read a construct it does not understand. No PyYAML is needed (SPEC §2.3).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from mod_base import cli, workflow
from mod_base.adapter import protocol
from mod_base.model import grammar
from mod_base.model import limits as lim

ROOT = Path(__file__).resolve().parents[1]
CALLEE_PATHS = {name: ROOT / path for name, path in workflow.CALLEE_WORKFLOWS.items()}
CALLER_PATH = ROOT / "template/managed/.github/workflows/pages.yml"
COMPOSITES = ("setup", "prepare-evidence", "publish-family", "notify-pages")
COMPOSITE_PATHS = {name: ROOT / f"actions/{name}/action.yml" for name in COMPOSITES}
#: Composites that run the kit tree check as their first step (SPEC §1.4).
VERIFYING_COMPOSITES = ("setup", "prepare-evidence", "publish-family")

CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
SETUP_PYTHON = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"
UPLOAD = "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
UPLOAD_PAGES = "actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9"
DEPLOY_PAGES = "actions/deploy-pages@cd2ce8fcbc39b97be8ca5fce6e763baed58fa128"
#: The one action of the managed gate status caller: its publishing job mints the statuses-only
#: App token with it (the pin Block Pops reviewed for its own status writer).
APP_TOKEN = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
#: The reviewed nested pins (verified against both mods' workflows) and their version comments.
PINNED_ACTIONS = {CHECKOUT: "v7.0.1", SETUP_PYTHON: "v7.0.0", UPLOAD: "v7.0.1", UPLOAD_PAGES: "v5.0.0",
                  DEPLOY_PAGES: "v5.0.0", APP_TOKEN: "v3.2.0"}

#: SPEC §1.2 step 6: every step that does not call the API first unsets every credential...
SCRUB = ("unset ACTIONS_RUNTIME_TOKEN ACTIONS_CACHE_URL ACTIONS_RESULTS_URL ACTIONS_ID_TOKEN_REQUEST_TOKEN "
         "ACTIONS_ID_TOKEN_REQUEST_URL GITHUB_TOKEN GH_TOKEN")
#: ...and a step that calls the API keeps only its own step-scoped GH_TOKEN.
SCRUB_API = SCRUB.removesuffix(" GH_TOKEN")
KIT_PYTHON = ('env -u PYTHONPATH PYTHONPATH="$GITHUB_WORKSPACE/kit/src" PYTHONSAFEPATH=1 '
              "PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \\\n  python3 -P -m mod_base ")
ACTION_PYTHON = ('env -u PYTHONPATH PYTHONPATH="$GITHUB_ACTION_PATH/../../src" PYTHONSAFEPATH=1 '
                 "PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \\\n  python3 -P ")
VERIFY_TREE = ACTION_PYTHON + '"$GITHUB_ACTION_PATH/../../tools/verify_action_tree.py" --mod-root "$MOD_ROOT"'
TOKEN_VALUE = "${{ github.token }}"

#: The prologue every callee job runs first (SPEC §1.2), by step name and in this order.
PROLOGUE = (
    "Validate call inputs before any checkout",
    "Check out the protected mod head",
    "Check out the pinned mod-base kit",
    "Bind the two-part implementation identity",
    "Install Python",
    "Install the hash-locked imaging dependency",
)
#: Rotation authenticates its owner between input validation and any checkout (SPEC §5.5).
ROTATE_OWNER_STEP = "Authenticate the completed successful owner before any checkout"

#: The mod_base commands each callee job may run; no hook-dispatching command runs anywhere else.
JOB_COMMANDS = {
    ("publish", "admit"): {"admit"},
    ("publish", "collect"): {"select", "download", "authenticate", "compose", "compact", "validate"},
    ("publish", "family"): {"select", "download", "family"},
    ("publish", "build"): {"build"},
    ("finalize", "refresh"): {"refresh"},
    ("finalize", "refresh-family"): {"refresh"},
    ("rotate", "rotate"): {"rotate"},
}
#: Commands that may run an adapter hook (SPEC §4.3) or build an invocation that loads one.
HOOK_COMMANDS = frozenset({"expect", "prepare", "validate", "compact", "compose", "anchor", "family", "admit",
                           "select", "authenticate", "build"})
#: Every mod_base command a composite may run.
COMPOSITE_COMMANDS = {
    "setup": set(),
    "prepare-evidence": {"prepare", "validate", "anchor"},
    "publish-family": {"family", "validate"},
    "notify-pages": set(),
}

MODULE_INVOCATION = re.compile(r"python3 -P -m mod_base (?:\"\$\{arguments\[@\]\}\"|([a-z-]+))")
EXPRESSION = re.compile(r"\$\{\{")


# -- The strict YAML-subset reader ------------------------------------------------------------------


class YamlSubsetError(AssertionError):
    """A kit YAML file uses a construct the textual policy tests do not understand."""


_KEY = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*):(?:[ ]+(.*)|)$")
_BLOCK_SCALAR = re.compile(r"^[|>][-+]?$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(value: str) -> str:
    quote = ""
    for index, character in enumerate(value):
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'" and (index == 0 or value[index - 1] in " [{,:"):
            quote = character
        elif character == "#" and index > 0 and value[index - 1] == " ":
            return value[:index].rstrip()
    return value.rstrip()


def _scalar(text: str, where: str) -> str:
    text = text.strip()
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise YamlSubsetError(f"{where}: unterminated double-quoted scalar")
        try:
            value = json.loads(text)
        except ValueError:
            raise YamlSubsetError(f"{where}: unsupported double-quoted escape") from None
        return value
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise YamlSubsetError(f"{where}: unterminated single-quoted scalar")
        return text[1:-1].replace("''", "'")
    if text[:1] in {"&", "*", "!", "%", "@", "`"}:
        raise YamlSubsetError(f"{where}: anchors, aliases, tags and reserved indicators are not used")
    return text


def _split_flow(body: str, where: str) -> list[str]:
    parts, quote, current = [], "", []
    for character in body:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
        elif character in "[{":
            raise YamlSubsetError(f"{where}: nested flow collections are not used")
        elif character == ",":
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    if quote:
        raise YamlSubsetError(f"{where}: unterminated flow collection")
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()] if body.strip() else []


def _flow(text: str, where: str) -> Any:
    if text.startswith("{"):
        if not text.endswith("}"):
            raise YamlSubsetError(f"{where}: unterminated flow mapping")
        result: dict[str, str] = {}
        for part in _split_flow(text[1:-1], where):
            key, separator, value = part.partition(":")
            if not separator or not _KEY.match(f"{key.strip()}:"):
                raise YamlSubsetError(f"{where}: malformed flow mapping entry {part!r}")
            if key.strip() in result:
                raise YamlSubsetError(f"{where}: duplicate key {key.strip()!r}")
            result[key.strip()] = _scalar(value, where)
        return result
    if not text.endswith("]"):
        raise YamlSubsetError(f"{where}: unterminated flow sequence")
    return [_scalar(part, where) for part in _split_flow(text[1:-1], where)]


class _Reader:
    def __init__(self, text: str, label: str) -> None:
        if "\t" in text or "\r" in text:
            raise YamlSubsetError(f"{label}: tabs and carriage returns are not used")
        self.lines = text.split("\n")
        self.index = 0
        self.label = label

    def where(self) -> str:
        return f"{self.label}:{self.index + 1}"

    def skip(self) -> None:
        while self.index < len(self.lines):
            stripped = self.lines[self.index].strip()
            if stripped and not stripped.startswith("#"):
                return
            self.index += 1

    def peek_indent(self) -> int | None:
        self.skip()
        if self.index >= len(self.lines):
            return None
        return _indent_of(self.lines[self.index])

    def node(self, indent: int) -> Any:
        self.skip()
        content = self.lines[self.index][indent:]
        if content == "-" or content.startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while True:
            current = self.peek_indent()
            if current is None or current < indent:
                return result
            if current > indent:
                raise YamlSubsetError(f"{self.where()}: unexpected indentation")
            content = self.lines[self.index][indent:]
            if content.startswith("- "):
                raise YamlSubsetError(f"{self.where()}: a sequence must be indented under its key")
            match = _KEY.match(content)
            if match is None:
                raise YamlSubsetError(f"{self.where()}: expected 'key: value'")
            key, rest = match.group(1), match.group(2) or ""
            if key in result:
                raise YamlSubsetError(f"{self.where()}: duplicate key {key!r}")
            self.index += 1
            result[key] = self.value(rest, indent)

    def value(self, rest: str, indent: int) -> Any:
        where = self.where()
        text = _strip_comment(rest)
        if _BLOCK_SCALAR.match(text):
            return self.block_scalar(text, indent)
        if not text:
            nested = self.peek_indent()
            if nested is None or nested <= indent:
                raise YamlSubsetError(f"{where}: empty values are not used")
            return self.node(nested)
        if text[0] in "{[":
            return _flow(text, where)
        return _scalar(text, where)

    def sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []
        while True:
            current = self.peek_indent()
            if current is None or current < indent:
                return items
            content = self.lines[self.index][indent:]
            if current != indent or not (content == "-" or content.startswith("- ")):
                if current == indent:
                    return items
                raise YamlSubsetError(f"{self.where()}: unexpected indentation in a sequence")
            after = content[1:].lstrip(" ")
            if not after:
                raise YamlSubsetError(f"{self.where()}: nested block items are not used")
            if _KEY.match(_strip_comment(after)):
                offset = len(content) - len(after)
                self.lines[self.index] = " " * (indent + offset) + after
                items.append(self.mapping(indent + offset))
            else:
                self.index += 1
                text = _strip_comment(after)
                items.append(_flow(text, self.where()) if text[:1] in "{[" else _scalar(text, self.where()))

    def block_scalar(self, style: str, indent: int) -> str:
        collected: list[str] = []
        block_indent: int | None = None
        while self.index < len(self.lines):
            line = self.lines[self.index]
            if not line.strip():
                collected.append("")
                self.index += 1
                continue
            current = _indent_of(line)
            if current <= indent:
                break
            if block_indent is None:
                block_indent = current
            if current < block_indent:
                raise YamlSubsetError(f"{self.where()}: block scalar dedents below its first line")
            collected.append(line[block_indent:])
            self.index += 1
        while collected and not collected[-1]:
            collected.pop()
        if not collected:
            raise YamlSubsetError(f"{self.where()}: empty block scalar")
        if style.startswith("|"):
            text = "\n".join(collected)
        else:
            text = ""
            for line in collected:
                text += ("\n" if not line else (" " if text and not text.endswith("\n") else "") + line)
        return text + ("" if style.endswith("-") else "\n")


def parse_yaml(text: str, label: str = "yaml") -> dict[str, Any]:
    """Parse the kit's YAML subset into dicts, lists and strings (every scalar stays a string)."""

    reader = _Reader(text, label)
    document = reader.mapping(0)
    if reader.peek_indent() is not None:
        raise YamlSubsetError(f"{reader.where()}: trailing content")
    return document


def load_yaml(path: Path) -> dict[str, Any]:
    return parse_yaml(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())


def render_caller(sha: str = "0123456789abcdef0123456789abcdef01234567", version: str = "v1.2.3") -> str:
    """The managed caller as ``template sync`` renders it for a mod pinned at ``sha``/``version``."""

    return CALLER_PATH.read_text(encoding="utf-8").replace("{{PIN}}", sha).replace("{{VERSION}}", version)


def callee(name: str) -> dict[str, Any]:
    return load_yaml(CALLEE_PATHS[name])


def caller() -> dict[str, Any]:
    return parse_yaml(render_caller(), "pages.yml")


def composite(name: str) -> dict[str, Any]:
    return load_yaml(COMPOSITE_PATHS[name])


def step(steps: Sequence[Mapping[str, Any]], name: str) -> dict[str, Any]:
    matches = [item for item in steps if item.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one step named {name!r}, found {len(matches)}")
    return dict(matches[0])


def iter_documents() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every kit-owned workflow and composite: ``(label, document)``."""

    for name in workflow.CALLEE_WORKFLOWS:
        yield f"callee {name}", callee(name)
    yield "managed caller", caller()
    for name in COMPOSITES:
        yield f"composite {name}", composite(name)


def iter_steps() -> Iterator[tuple[str, dict[str, Any]]]:
    """``(label, step)`` for every step of every kit-owned workflow job and composite."""

    for label, document in iter_documents():
        if "jobs" in document:
            for job_id, job in document["jobs"].items():
                for item in job.get("steps", []):
                    yield f"{label} {job_id} / {item.get('name')}", item
        else:
            for item in document["runs"]["steps"]:
                yield f"{label} / {item.get('name')}", item


def mod_base_commands(script: str) -> list[str]:
    """The ``mod_base`` subcommands a run script invokes (``"${arguments[@]}"`` resolves to the
    array's first word)."""

    commands: list[str] = []
    for match in MODULE_INVOCATION.finditer(script):
        if match.group(1):
            commands.append(match.group(1))
        else:
            array = re.search(r"arguments=\(([a-z-]+) ", script)
            if array is None:
                raise AssertionError("an argument-array invocation has no literal command word")
            commands.append(array.group(1))
    return commands


def parse_kit_argv(argv: Sequence[str]) -> Any:
    """Parse one recorded ``mod_base`` command line with the kit's own registered (frozen) parser,
    so a YAML flag that the CLI does not define, or a value its type rejects, fails the test."""

    return cli.build_parser(argv[0]).parse_args(list(argv))


def permission_level(permissions: Any, scope: str) -> int:
    """0 none, 1 read, 2 write for one scope of a ``permissions`` value."""

    if permissions in ("{}", {}):
        return 0
    if not isinstance(permissions, Mapping):
        raise AssertionError(f"permissions must be an explicit mapping, not {permissions!r}")
    return {"none": 0, "read": 1, "write": 2}[permissions.get(scope, "none")]


# -- Shell execution with stub tools (the Block Pops workflow-shell test style) ---------------------

GH_STUB = r'''
import json, os, pathlib, subprocess, sys
arguments = sys.argv[1:]
route, jq_filter, payload = " ".join(arguments), None, None
if arguments[:1] == ["api"]:
    rest, method, endpoint, index = arguments[1:], "GET", None, 0
    while index < len(rest):
        item = rest[index]
        if item in ("--method", "-X"):
            method = rest[index + 1]; index += 2
        elif item == "--jq":
            jq_filter = rest[index + 1]; index += 2
        elif item in ("-f", "-F", "--raw-field", "--field"):
            index += 2
        elif item == "--input":
            payload = pathlib.Path(rest[index + 1]).read_text(encoding="utf-8"); index += 2
        elif item.startswith("-"):
            raise SystemExit(f"stub gh: unexpected flag {item}")
        elif endpoint is not None:
            raise SystemExit("stub gh: two endpoints")
        else:
            endpoint = item; index += 1
    route = f"{method} {endpoint}"
with pathlib.Path(os.environ["STUB_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"tool": "gh", "argv": arguments, "route": route, "input": payload,
                             "token": os.environ.get("GH_TOKEN"), "repo": os.environ.get("GH_REPO"),
                             "retry": [os.environ.get("GITHUB_API_RETRY_ATTEMPTS"),
                                       os.environ.get("GITHUB_API_RETRY_MAX_DELAY_SECONDS")]}) + "\n")
fixtures = json.loads(pathlib.Path(os.environ["GH_FIXTURES"]).read_text(encoding="utf-8"))
responses = fixtures.get(route)
if responses is None:
    sys.stderr.write(f"gh: Not Found (HTTP 404) for {route}\n")
    raise SystemExit(1)
if isinstance(responses, dict):
    responses = [responses]
state_path = pathlib.Path(os.environ["STUB_CALLS"] + ".state")
state = json.loads(state_path.read_text()) if state_path.exists() else {}
position = state.get(route, 0)
state[route] = position + 1
state_path.write_text(json.dumps(state))
response = responses[min(position, len(responses) - 1)]
if response.get("fail"):
    sys.stderr.write(response["fail"] + "\n")
    raise SystemExit(response.get("status", 1))
body = response.get("body")
if jq_filter is not None:
    result = subprocess.run(["jq", "-r", "-c", jq_filter], input=json.dumps(body), capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    raise SystemExit(result.returncode)
if body is not None:
    sys.stdout.write(body if isinstance(body, str) else json.dumps(body))
'''

RECORDER_STUB = r'''
import json, os, pathlib, sys
arguments = sys.argv[1:]
program = pathlib.Path(sys.argv[0]).name
record = {"tool": program, "argv": arguments,
          "env": {name: os.environ.get(name) for name in json.loads(os.environ.get("STUB_ENV_NAMES", "[]"))}}
with pathlib.Path(os.environ["STUB_CALLS"]).open("a", encoding="utf-8") as stream:
    stream.write(json.dumps(record) + "\n")
script = os.environ.get(f"STUB_{program.upper().replace('-', '_')}_SCRIPT")
if script:
    namespace = {"arguments": arguments, "os": os, "pathlib": pathlib, "json": json, "sys": sys}
    exec(compile(script, f"<{program} stub>", "exec"), namespace)
'''


def require_tools(*names: str) -> None:
    """Fail (in CI) or skip (locally) when an external tool the shell tests execute is absent."""

    missing = [name for name in names if shutil.which(name) is None]
    if "bash" in names and "bash" not in missing:
        # The runner's bash is 5.x; mapfile and the other 4.x builtins the steps use are absent from 3.2.
        major = subprocess.run(["bash", "-c", 'printf %s "${BASH_VERSINFO[0]}"'], capture_output=True, text=True,
                               timeout=30).stdout
        if not major.isdigit() or int(major) < 4:
            missing.append("bash>=4")
    if missing:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            raise AssertionError(f"required tools missing in CI: {', '.join(missing)}")
        raise unittest.SkipTest(f"local run without {', '.join(missing)}")


class ShellHarness:
    """Run an extracted ``run:`` body under ``bash`` with stub ``gh``/``git``/``python3``/``sleep``.

    Real ``jq``, ``sha256sum``, ``base64`` and coreutils stay on ``PATH``; the stubs record every
    call (argv plus selected environment) in a JSON-lines file. The environment starts empty apart
    from ``PATH``/``HOME``/``LC_ALL`` so no host credential or exported shell function leaks in.
    """

    def __init__(self, root: Path, *, stubs: Sequence[str] = ("gh", "git", "python3", "sleep")) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        for name in stubs:
            body = GH_STUB if name == "gh" else RECORDER_STUB
            path = self.bin / name
            path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
            path.chmod(0o755)
        self.calls = root / "calls.jsonl"

    def run(self, script: str, env: Mapping[str, str], *, fixtures: Mapping[str, Any] | None = None,
            cwd: Path | None = None, record_env: Sequence[str] = ()) -> subprocess.CompletedProcess[str]:
        if self.calls.exists():
            self.calls.unlink()
        state = Path(str(self.calls) + ".state")
        if state.exists():
            state.unlink()
        fixture_file = self.root / "gh-fixtures.json"
        fixture_file.write_text(json.dumps(fixtures or {}), encoding="utf-8")
        environment = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root),
            "LC_ALL": "C",
            "STUB_CALLS": str(self.calls),
            "GH_FIXTURES": str(fixture_file),
            "STUB_ENV_NAMES": json.dumps(list(record_env)),
            **env,
        }
        return subprocess.run(["bash", "-c", script], cwd=cwd or self.root, env=environment, capture_output=True,
                              text=True, timeout=120)

    def records(self) -> list[dict[str, Any]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]


def outputs(path: Path) -> dict[str, str]:
    """Parse a ``$GITHUB_OUTPUT``/``$GITHUB_ENV`` file of ``name=value`` lines (last value wins)."""

    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if not separator:
                raise AssertionError(f"malformed output line {line!r}")
            values[name] = value
    return values


# -- The policy tests -------------------------------------------------------------------------------


class YamlSubsetReaderTests(unittest.TestCase):
    def test_reads_every_kit_owned_file(self) -> None:
        documents = dict(iter_documents())
        self.assertEqual(len(documents), 3 + 1 + len(COMPOSITES))

    def test_reader_understands_the_constructs_it_claims(self) -> None:
        document = parse_yaml(textwrap.dedent("""\
            a: plain # comment
            b: "quoted # not a comment"
            c: {x: 1, y: two words}
            d: [one, two]
            e: |
              line one
                indented

              after blank
            f: >-
              folded
              text
            g:
              - name: first
                run: echo
              - second
            """))
        self.assertEqual(document["a"], "plain")
        self.assertEqual(document["b"], "quoted # not a comment")
        self.assertEqual(document["c"], {"x": "1", "y": "two words"})
        self.assertEqual(document["d"], ["one", "two"])
        self.assertEqual(document["e"], "line one\n  indented\n\nafter blank\n")
        self.assertEqual(document["f"], "folded text")
        self.assertEqual(document["g"], [{"name": "first", "run": "echo"}, "second"])

    def test_reader_refuses_constructs_it_does_not_understand(self) -> None:
        for text in ("a: &anchor x\n", "a: *alias\n", "a: 1\na: 2\n", "a:\n- b\n", "a: [x, [y]]\n", "a:\tb\n",
                     "a:\n", "a: b\n  c: d\n"):
            with self.subTest(text=text), self.assertRaises(YamlSubsetError):
                parse_yaml(text)


class CalleeWorkflowPolicyTests(unittest.TestCase):
    def test_callees_are_workflow_call_only_without_concurrency_or_secrets(self) -> None:
        for name in workflow.CALLEE_WORKFLOWS:
            document = callee(name)
            with self.subTest(callee=name):
                self.assertEqual(list(document["on"]), ["workflow_call"])
                self.assertNotIn("secrets", document["on"]["workflow_call"])
                self.assertNotIn("concurrency", document)
                self.assertEqual(document["permissions"], {})
                self.assertEqual(list(document["env"]), ["MB_KIT_TREE_DIGEST"])
                self.assertRegex(document["env"]["MB_KIT_TREE_DIGEST"], r"^sha256:[0-9a-f]{64}$")
                for job_id, job in document["jobs"].items():
                    self.assertNotIn("concurrency", job, job_id)
                    self.assertEqual(job["runs-on"], "ubuntu-24.04", job_id)
                    self.assertRegex(job["timeout-minutes"], r"^[1-9][0-9]?$", job_id)
                    self.assertNotIn("secrets", job, job_id)
                    self.assertNotIn("uses", job, f"{job_id} must not call another reusable workflow")
                    self.assertNotIn("environment", job, job_id)
                text = CALLEE_PATHS[name].read_text(encoding="utf-8")
                self.assertNotIn("secrets.", text)
                self.assertNotIn("secrets: inherit", text)

    def test_callee_inputs_are_strings_and_optional_ones_default_empty(self) -> None:
        expected = {
            "publish": ["kit-sha", "operation", "run-id", "sha", "family", "bundle-key", "artifact-id",
                        "artifact-digest", "coverage-sha"],
            "finalize": ["kit-sha", "bundle-keys", "families", "heads"],
            "rotate": ["kit-sha", "pages-run-id", "pages-run-sha"],
        }
        required = {"publish": {"kit-sha", "operation"}, "finalize": set(expected["finalize"]),
                    "rotate": set(expected["rotate"])}
        for name, names in expected.items():
            inputs = callee(name)["on"]["workflow_call"]["inputs"]
            with self.subTest(callee=name):
                self.assertEqual(list(inputs), names)
                for input_name, spec in inputs.items():
                    self.assertEqual(spec["type"], "string", input_name)
                    if input_name in required[name]:
                        self.assertEqual(spec["required"], "true", input_name)
                        self.assertNotIn("default", spec, input_name)
                    else:
                        self.assertEqual((spec["required"], spec["default"]), ("false", ""), input_name)

    def test_callee_permissions_subset_of_managed_caller_grants(self) -> None:
        grants = caller()["jobs"]
        for name in workflow.CALLEE_WORKFLOWS:
            calling = [job for job in grants.values()
                       if str(job.get("uses", "")).startswith(f"The-Plum-Team/mod-base/.github/workflows/{name}.yml@")]
            self.assertEqual(len(calling), 1, name)
            grant = calling[0]["permissions"]
            for job_id, job in callee(name)["jobs"].items():
                permissions = job["permissions"]
                self.assertIsInstance(permissions, dict, f"{name}/{job_id} must declare explicit permissions")
                for scope in permissions:
                    with self.subTest(callee=name, job=job_id, scope=scope):
                        self.assertLessEqual(permission_level(permissions, scope), permission_level(grant, scope))

    def test_network_hook_jobs_are_read_only(self) -> None:
        publish = callee("publish")["jobs"]
        self.assertEqual(protocol.TOKEN_JOBS, frozenset(publish))
        for job_id in protocol.TOKEN_JOBS:
            with self.subTest(job=job_id):
                self.assertEqual(publish[job_id]["permissions"], {"actions": "read", "contents": "read"})
        every_job = {job_id for name in workflow.CALLEE_WORKFLOWS for job_id in callee(name)["jobs"]}
        self.assertTrue({"refresh", "refresh-family", "rotate"} <= protocol.FORBIDDEN_JOBS & every_job)
        self.assertFalse(protocol.TOKEN_JOBS & protocol.FORBIDDEN_JOBS)

    def test_adapter_never_in_write_jobs(self) -> None:
        for name in workflow.CALLEE_WORKFLOWS:
            for job_id, job in callee(name)["jobs"].items():
                commands = {command for item in job["steps"] if "run" in item
                            for command in mod_base_commands(item["run"])}
                with self.subTest(callee=name, job=job_id):
                    self.assertTrue(commands, "every callee job runs the kit")
                    self.assertLessEqual(commands, JOB_COMMANDS[(name, job_id)])
                    writes = any(permission_level(job["permissions"], scope) == 2 for scope in job["permissions"])
                    if writes or job_id in protocol.FORBIDDEN_JOBS:
                        self.assertFalse(commands & HOOK_COMMANDS, "no hook-dispatching command here")
        for job_id, job in caller()["jobs"].items():
            for item in job.get("steps", []):
                with self.subTest(caller_job=job_id):
                    self.assertNotIn("python", item.get("run", ""), "caller-owned jobs run no kit or adapter code")

    def test_prologue_runs_first_and_in_order(self) -> None:
        for name in workflow.CALLEE_WORKFLOWS:
            for job_id, job in callee(name)["jobs"].items():
                names = [item["name"] for item in job["steps"]]
                expected = list(PROLOGUE)
                if name == "rotate":
                    expected.insert(1, ROTATE_OWNER_STEP)
                with self.subTest(callee=name, job=job_id):
                    self.assertEqual(names[:len(expected)], expected)
                    self.assertEqual(sum(1 for item in job["steps"] if str(item.get("uses", "")).startswith(CHECKOUT)),
                                     2, "exactly the two prologue checkouts")

    def test_prologue_steps_are_identical_wherever_they_repeat(self) -> None:
        bind = set()
        python = set()
        pillow = set()
        kit_checkout = set()
        for name in workflow.CALLEE_WORKFLOWS:
            validations = set()
            mod_checkouts = set()
            for job in callee(name)["jobs"].values():
                steps = job["steps"]
                validations.add(json.dumps(step(steps, PROLOGUE[0]), sort_keys=True))
                mod_checkouts.add(json.dumps(step(steps, PROLOGUE[1]), sort_keys=True))
                kit_checkout.add(json.dumps(step(steps, PROLOGUE[2]), sort_keys=True))
                bind.add(json.dumps(step(steps, PROLOGUE[3]), sort_keys=True))
                python.add(json.dumps(step(steps, PROLOGUE[4]), sort_keys=True))
                pillow.add(json.dumps(step(steps, PROLOGUE[5]), sort_keys=True))
            self.assertEqual(len(validations), 1, f"{name}: one input validation for every job")
            self.assertEqual(len(mod_checkouts), 1, f"{name}: one mod checkout for every job")
        for label, values in (("kit checkout", kit_checkout), ("binding", bind), ("python", python),
                              ("pillow", pillow)):
            self.assertEqual(len(values), 1, f"the {label} step is byte-identical in every callee job")

    def test_checkouts_bind_the_protected_head_and_the_verified_kit(self) -> None:
        for name in workflow.CALLEE_WORKFLOWS:
            steps = next(iter(callee(name)["jobs"].values()))["steps"]
            mod = step(steps, PROLOGUE[1])
            kit = step(steps, PROLOGUE[2])
            with self.subTest(callee=name):
                self.assertEqual(mod["uses"], CHECKOUT)
                self.assertEqual({key: mod["with"][key] for key in ("ref", "path", "persist-credentials")},
                                 {"ref": "${{ github.sha }}", "path": "mod", "persist-credentials": "false"})
                self.assertEqual(kit["uses"], CHECKOUT)
                self.assertEqual(kit["with"], {"repository": "The-Plum-Team/mod-base", "ref": "${{ inputs.kit-sha }}",
                                               "path": "kit", "persist-credentials": "false"})
                expected_extra = {"publish": {"fetch-depth": "0"}, "finalize": {},
                                  "rotate": {"sparse-checkout": "site/mod-base.json",
                                             "sparse-checkout-cone-mode": "false"}}[name]
                self.assertEqual({key: value for key, value in mod["with"].items()
                                  if key not in ("ref", "path", "persist-credentials")}, expected_extra)
                bind = step(steps, PROLOGUE[3])
                self.assertEqual(bind["env"], {"KIT_SHA": "${{ inputs.kit-sha }}"})
                python = step(steps, PROLOGUE[4])
                self.assertEqual((python["uses"], python["with"]), (SETUP_PYTHON, {"python-version": "3.13"}))
                self.assertIn("python3 -m pip install --only-binary=:all: --require-hashes --requirement "
                              "kit/requirements/pillow.txt", step(steps, PROLOGUE[5])["run"])

    def test_every_kit_invocation_is_isolated(self) -> None:
        for name in workflow.CALLEE_WORKFLOWS:
            for job_id, job in callee(name)["jobs"].items():
                for item in job["steps"]:
                    script = item.get("run", "")
                    for line_number, line in enumerate(script.splitlines()):
                        if "python3 -P -m mod_base" in line:
                            previous = script.splitlines()[line_number - 1]
                            with self.subTest(callee=name, job=job_id, step=item["name"]):
                                self.assertEqual(previous + "\n" + line[:line.index("mod_base") + len("mod_base")],
                                                 KIT_PYTHON.rstrip())
                    self.assertNotRegex(script, r"(?m)^\s*python3? (?!-m pip install|-P -m mod_base)")

    def test_run_blocks_interpolate_no_expression(self) -> None:
        for label, item in iter_steps():
            if "run" in item:
                with self.subTest(step=label):
                    self.assertNotRegex(item["run"], EXPRESSION)
                    self.assertEqual(item.get("shell"), "bash")

    def test_every_run_block_starts_strict_and_scrubbed(self) -> None:
        for label, item in iter_steps():
            if "run" not in item or label.startswith("managed caller"):
                continue
            lines = item["run"].splitlines()
            token = item.get("env", {}).get("GH_TOKEN")
            with self.subTest(step=label):
                self.assertEqual(lines[0], "set -euo pipefail")
                self.assertEqual(lines[1], SCRUB_API if token else SCRUB)
                if token is not None:
                    self.assertEqual(token, TOKEN_VALUE)

    def test_tokens_are_step_scoped_and_only_where_the_api_is_called(self) -> None:
        api_steps = {
            "Admit an authenticated publication", "Select the newest authenticated evidence",
            "Download the selected artifact by immutable ID", "Authenticate the selected source",
            "Compose selected evidence with its authenticated baseline", "Recheck the source head",
            "Select the newest authenticated family generation",
            "Download the selected family generation by immutable ID", "Recheck and render the atomic site",
            "Revalidate the promoted bundle", "Revalidate the promoted family bundle", ROTATE_OWNER_STEP,
            "Retire the superseded generation by exact artifact ID",
        }
        seen = set()
        for name in workflow.CALLEE_WORKFLOWS:
            document = callee(name)
            for job_id, job in document["jobs"].items():
                self.assertNotIn("env", job, f"{name}/{job_id}: no job-level environment")
                for item in job["steps"]:
                    has_token = "GH_TOKEN" in item.get("env", {})
                    self.assertEqual(has_token, item["name"] in api_steps, f"{name}/{job_id}/{item['name']}")
                    self.assertNotIn("GITHUB_TOKEN", item.get("env", {}))
                    self.assertNotIn("token", item.get("with", {}), "checkouts use the job token read-only")
                    if has_token:
                        seen.add(item["name"])
        self.assertEqual(seen, api_steps)

    def test_uploads_have_fixed_names_and_retention(self) -> None:
        key, family, sha = "mc1.20.1", "mod-compatibility", "a" * 40
        expected = {
            ("publish", "collect", "Upload the collected bundle for the atomic build"):
                ("mb-collected--${{ matrix.key }}", str(lim.RETENTION_DAYS["collected"]),
                 grammar.collected_name(key)),
            ("publish", "family", "Upload the collected family bundle for the atomic build"):
                ("mb-collected-family--${{ matrix.family }}--${{ matrix.key }}",
                 str(lim.RETENTION_DAYS["collected-family"]), grammar.collected_family_name(family, key)),
            ("publish", "build", "Upload the promotion record"):
                (grammar.PROMOTION_NAME, str(lim.RETENTION_DAYS["promotion"]), grammar.PROMOTION_NAME),
            ("finalize", "refresh", workflow.STEPS["cache_upload"]):
                ("${{ steps.refresh.outputs.cache_name }}", str(lim.RETENTION_DAYS["cache"]), None),
            ("finalize", "refresh", workflow.STEPS["baseline_upload"]):
                ("${{ steps.refresh.outputs.baseline_name }}", "${{ steps.refresh.outputs.baseline_retention_days }}",
                 None),
            ("finalize", "refresh-family", workflow.STEPS["family_cache_upload"]):
                ("${{ steps.refresh.outputs.cache_name }}", str(lim.RETENTION_DAYS["family-cache"]), None),
        }
        observed = {}
        for name in workflow.CALLEE_WORKFLOWS:
            for job_id, job in callee(name)["jobs"].items():
                for item in job["steps"]:
                    if str(item.get("uses", "")).startswith(UPLOAD):
                        observed[(name, job_id, item["name"])] = item
        self.assertEqual(set(observed), set(expected))
        for identity, (name_template, retention, rendered) in expected.items():
            item = observed[identity]
            with self.subTest(upload=identity):
                self.assertEqual(item["uses"], UPLOAD)
                self.assertEqual(item["with"]["name"], name_template)
                self.assertEqual(item["with"]["retention-days"], retention)
                self.assertEqual(item["with"]["if-no-files-found"], "error")
                self.assertEqual(item["with"]["include-hidden-files"], "true")
                if rendered is not None:
                    self.assertEqual(name_template.replace("${{ matrix.key }}", key)
                                     .replace("${{ matrix.family }}", family), rendered)
        pages = step(callee("publish")["jobs"]["build"]["steps"], "Upload the atomic Pages artifact")
        self.assertEqual(pages["uses"], UPLOAD_PAGES)
        self.assertEqual(pages["with"], {"path": "_site", "include-hidden-files": "true",
                                         "retention-days": str(lim.RETENTION_DAYS["pages"])})
        self.assertEqual(grammar.cache_name(key, sha), f"mb-cache--{key}--{sha}")

    def test_baseline_retention_is_bounded_by_the_config_limit(self) -> None:
        script = step(callee("finalize")["jobs"]["refresh"]["steps"], "Revalidate the promoted bundle")["run"]
        self.assertIn(f"(( retention > {lim.MAX_BASELINE_RETENTION_DAYS} ))", script)
        self.assertIn("steps.refresh.outputs.baseline_name != ''",
                      step(callee("finalize")["jobs"]["refresh"]["steps"],
                           workflow.STEPS["baseline_upload"])["if"])


class ShellSyntaxTests(unittest.TestCase):
    def test_every_run_body_parses_and_passes_shellcheck(self) -> None:
        require_tools("bash", "shellcheck")
        for label, item in iter_steps():
            if "run" not in item:
                continue
            with self.subTest(step=label):
                parsed = subprocess.run(["bash", "-n"], input=item["run"], capture_output=True, text=True, timeout=60)
                self.assertEqual(parsed.returncode, 0, parsed.stderr)
                # SC2154 (variable referenced but not assigned) is excluded as actionlint does: the
                # step's env: mapping and the runner assign those variables.
                checked = subprocess.run(["shellcheck", "-s", "bash", "-e", "SC2154", "-"], input=item["run"],
                                         capture_output=True, text=True, timeout=60)
                self.assertEqual(checked.returncode, 0, checked.stdout)

    def test_tools_parse_and_pass_shellcheck(self) -> None:
        require_tools("bash", "shellcheck")
        for path in (ROOT / "tools/kit_digest.sh", ROOT / "tools/github_api_retry.sh"):
            with self.subTest(tool=path.name):
                self.assertEqual(subprocess.run(["bash", "-n", str(path)], timeout=60).returncode, 0)
                checked = subprocess.run(["shellcheck", str(path)], capture_output=True, text=True, timeout=60)
                self.assertEqual(checked.returncode, 0, checked.stdout)


def composite_as_workflow(name: str) -> str:
    """A synthetic ``workflow_call`` workflow running the composite's steps verbatim.

    actionlint lints workflows, not composite action metadata, so this is how its expression,
    context and embedded shellcheck checks reach the composites' steps."""

    text = COMPOSITE_PATHS[name].read_text(encoding="utf-8")
    marker = "\nruns:\n  using: composite\n  steps:\n"
    if text.count(marker) != 1:
        raise AssertionError(f"composite {name} must end with its runs.steps block")
    steps = text[text.index(marker) + len(marker):]
    lines = ["name: composite " + name, "on:", "  workflow_call:"]
    inputs = composite(name).get("inputs", {})
    if inputs:
        lines.append("    inputs:")
        for input_name in inputs:
            lines.extend([f"      {input_name}:", "        type: string", "        required: false"])
    lines.extend(["permissions: {}", "jobs:", "  composite:", "    runs-on: ubuntu-24.04", "    steps:"])
    indented = "".join(("  " + line if line.strip() else line) for line in steps.splitlines(keepends=True))
    return "\n".join(lines) + "\n" + indented


class ActionlintTests(unittest.TestCase):
    """actionlint (with its embedded shellcheck) over every kit-owned workflow and composite.

    The composites reach actionlint only through this wrapper, so, like every other external tool,
    a missing actionlint or shellcheck fails the test in CI and skips it only on a local run."""

    def test_actionlint_is_clean(self) -> None:
        require_tools("actionlint", "shellcheck")
        actionlint = shutil.which("actionlint")
        assert actionlint is not None
        with tempfile.TemporaryDirectory(prefix="actionlint ") as temporary:
            workflows = Path(temporary) / ".github/workflows"
            workflows.mkdir(parents=True)
            for name, path in CALLEE_PATHS.items():
                shutil.copyfile(path, workflows / f"{name}.yml")
            (workflows / "pages.yml").write_text(render_caller(), encoding="utf-8")
            (workflows / "pages-template.yml").write_text(CALLER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            for name in COMPOSITES:
                (workflows / f"composite-{name}.yml").write_text(composite_as_workflow(name), encoding="utf-8")
            files = sorted(str(path.relative_to(temporary)) for path in workflows.iterdir())
            result = subprocess.run([actionlint, "-no-color", *files], cwd=temporary, capture_output=True, text=True,
                                    timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class CompositePolicyTests(unittest.TestCase):
    def test_composites_are_bash_composites_with_pinned_nested_actions(self) -> None:
        for name in COMPOSITES:
            document = composite(name)
            with self.subTest(composite=name):
                self.assertEqual(document["runs"]["using"], "composite")
                self.assertTrue(document["name"].startswith("mod-base "))
                for item in document["runs"]["steps"]:
                    if "uses" in item:
                        self.assertIn(item["uses"], (SETUP_PYTHON, UPLOAD), item["name"])
                    else:
                        self.assertEqual(item["shell"], "bash")
                text = COMPOSITE_PATHS[name].read_text(encoding="utf-8")
                self.assertNotIn("GITHUB_ACTION_REF", text)
                self.assertNotIn("GITHUB_ACTION_REPOSITORY", text)
                self.assertNotIn("secrets.", text)
                self.assertNotRegex(text, r"uses:\s*\./")

    def test_verifying_composites_check_the_tree_first(self) -> None:
        for name in VERIFYING_COMPOSITES:
            steps = composite(name)["runs"]["steps"]
            first = steps[0]
            with self.subTest(composite=name):
                self.assertEqual(first["name"], "Verify the executing kit against the checked-out pin")
                self.assertTrue(first["run"].rstrip("\n").endswith(VERIFY_TREE))
                self.assertEqual(first["env"]["GH_TOKEN"], TOKEN_VALUE)
                self.assertEqual(first["env"]["MOD_ROOT"], "${{ inputs.mod-root }}")
                self.assertEqual(sum("verify_action_tree.py" in item.get("run", "") for item in steps), 1)

    def test_composite_python_is_isolated_and_limited_to_its_commands(self) -> None:
        for name in COMPOSITES:
            commands = set()
            for item in composite(name)["runs"]["steps"]:
                script = item.get("run", "")
                commands.update(mod_base_commands(script))
                for line_number, line in enumerate(script.splitlines()):
                    if "python3 -P" in line:
                        previous = script.splitlines()[line_number - 1]
                        with self.subTest(composite=name, step=item["name"]):
                            self.assertTrue(previous.lstrip().startswith(ACTION_PYTHON.split(" \\\n")[0])
                                            or previous.lstrip().startswith(
                                                "identity=\"$(" + ACTION_PYTHON.split(" \\\n")[0]))
            with self.subTest(composite=name):
                self.assertEqual(commands, COMPOSITE_COMMANDS[name])

    def test_composite_uploads_have_fixed_names_and_retention(self) -> None:
        prepare = composite("prepare-evidence")["runs"]["steps"]
        handoff = step(prepare, "Upload the single-use evidence handoff")
        self.assertEqual(handoff["uses"], UPLOAD)
        self.assertEqual(handoff["with"]["name"], "mb-handoff--${{ inputs.key }}--a${{ github.run_attempt }}")
        self.assertEqual(handoff["with"]["name"].replace("${{ inputs.key }}", "mc1.20.1")
                         .replace("${{ github.run_attempt }}", "2"), grammar.handoff_name("mc1.20.1", 2))
        self.assertEqual(handoff["with"]["retention-days"], str(lim.RETENTION_DAYS["handoff"]))
        anchor_step = step(prepare, "Cut the lossless anchor from the uploaded handoff")
        self.assertEqual(anchor_step["env"]["HANDOFF_NAME"], handoff["with"]["name"])
        self.assertIn(f"(( retention > {lim.MAX_ANCHOR_RETENTION_DAYS} ))", anchor_step["run"])
        anchor_upload = step(prepare, "Upload the durable lossless anchor")
        self.assertEqual(anchor_upload["with"]["name"], "${{ steps.anchor.outputs.name }}")
        self.assertEqual(anchor_upload["with"]["retention-days"], "${{ steps.anchor.outputs.retention_days }}")
        family = composite("publish-family")["runs"]["steps"]
        upload = step(family, "Upload the family handoff")
        self.assertEqual(upload["with"]["name"],
                         "mb-family-handoff--${{ inputs.family }}--${{ inputs.key }}--a${{ github.run_attempt }}")
        self.assertEqual(upload["with"]["name"].replace("${{ inputs.family }}", "mod-compatibility")
                         .replace("${{ inputs.key }}", "mc26.3").replace("${{ github.run_attempt }}", "1"),
                         grammar.family_handoff_name("mod-compatibility", "mc26.3", 1))
        self.assertEqual(upload["with"]["retention-days"], "${{ steps.envelope.outputs.retention_days }}")
        self.assertIn(f"^[1-{lim.MAX_FAMILY_RETENTION_DAYS}]$",
                      step(family, "Wrap the family bundle in its envelope")["run"])
        for item in (handoff, anchor_upload, upload):
            self.assertEqual(item["with"]["if-no-files-found"], "error")


if __name__ == "__main__":
    unittest.main()
