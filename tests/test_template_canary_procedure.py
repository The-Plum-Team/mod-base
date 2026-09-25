"""The canary procedure of ``docs/OPERATIONS.md`` against the canary it seeds (MB9, SPEC §9.2).

The procedure is the only place an operator learns how to seed and drive The-Plum-Team/mod-base-canary,
so it must name every dispatchable canary workflow with each of its inputs, and say exactly which
template files the canary carries itself and which ones ``template init`` creates. Both lists are
derived here from ``canary/``, ``template/manifest.json`` and a real ``init`` of a copy of the canary,
so a new canary workflow, input or template file fails this test until the procedure names it.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from mod_base.template import tool
from tests.test_workflow_policy import parse_yaml

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "docs" / "OPERATIONS.md"
CANARY = ROOT / "canary"
CANARY_WORKFLOWS = CANARY / ".github" / "workflows"
PIN = "0123456789abcdef0123456789abcdef01234567"
VERSION = "v0.9.0"
DISPATCH = re.compile(r"gh workflow run (\S+)(.*)")
INPUT_FLAG = re.compile(r"(?:^|\s)-f\s+([A-Za-z0-9_-]+)=")
CODE_SPAN = re.compile(r"`([^`\n]+)`")
#: A repository path inside a code span (``init`` or ``created <path>`` are not paths).
PATH_TOKEN = re.compile(r"(?=.*[./]|[A-Z]+$)[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*")
CARRIED_LEAD = "The canary carries these template files itself"
CREATED_LEAD = "`init` creates every other template file"


def procedure() -> str:
    """The "Canary procedure" section of ``docs/OPERATIONS.md``, up to the next level-2 heading."""

    text = OPERATIONS.read_text(encoding="utf-8")
    start = text.index("\n## Canary procedure\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def paragraph(section: str, lead: str) -> str:
    matches = [block for block in re.split(r"\n\s*\n", section) if block.strip().startswith(lead)]
    if len(matches) != 1:
        raise AssertionError(f"the canary procedure needs exactly one paragraph starting {lead!r}, found "
                             f"{len(matches)}")
    return matches[0]


def listed_paths(block: str) -> list[str]:
    return [token for token in CODE_SPAN.findall(block) if PATH_TOKEN.fullmatch(token)]


def dispatch_commands(section: str) -> dict[str, list[str]]:
    """Workflow file -> the argument text of every ``gh workflow run`` naming it (comments dropped)."""

    commands: dict[str, list[str]] = {}
    for line in section.splitlines():
        for match in DISPATCH.finditer(line):
            arguments = match.group(2).split("`", 1)[0].split(" #", 1)[0]
            commands.setdefault(match.group(1), []).append(arguments)
    return commands


def dispatch_inputs(path: Path) -> set[str] | None:
    """The ``workflow_dispatch`` inputs of ``path``, or ``None`` when it cannot be dispatched."""

    triggers = parse_yaml(path.read_text(encoding="utf-8"), path.name)["on"]
    if not isinstance(triggers, dict) or "workflow_dispatch" not in triggers:
        return None
    dispatch = triggers["workflow_dispatch"]
    return set(dispatch.get("inputs", {})) if isinstance(dispatch, dict) else set()


def template_paths() -> set[str]:
    return {entry["path"] for entry in tool.load_manifest(ROOT)["files"]}


def carried_paths() -> set[str]:
    return {path for path in template_paths() if (CANARY / path).is_file()}


class CanaryDispatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.section = procedure()
        self.commands = dispatch_commands(self.section)

    def test_every_operator_workflow_has_a_dispatch_command_with_every_input(self) -> None:
        operator = sorted(CANARY_WORKFLOWS.glob("canary-*.yml"))
        self.assertLessEqual({"canary-family.yml", "canary-probe.yml", "canary-producer.yml"},
                             {path.name for path in operator})
        for path in operator:
            with self.subTest(workflow=path.name):
                inputs = dispatch_inputs(path)
                self.assertIsNotNone(inputs, f"{path.name} has no workflow_dispatch trigger")
                commands = self.commands.get(path.name, [])
                self.assertTrue(commands, f"the canary procedure never dispatches {path.name}")
                for arguments in commands:
                    self.assertIn('-R "$CANARY"', arguments)
                    self.assertRegex(arguments, r"(?:^|\s)--ref main(?:\s|$)")
                named = {name for arguments in commands for name in INPUT_FLAG.findall(arguments)}
                self.assertEqual(named, inputs, f"the procedure must show every {path.name} input (-f name=...)")

    def test_every_dispatch_command_names_a_canary_workflow_and_its_inputs(self) -> None:
        self.assertTrue(self.commands)
        for name, commands in self.commands.items():
            with self.subTest(workflow=name):
                path = CANARY_WORKFLOWS / name
                self.assertTrue(path.is_file(), f"the procedure dispatches {name}, which the canary lacks")
                inputs = dispatch_inputs(path)
                self.assertIsNotNone(inputs)
                for arguments in commands:
                    self.assertLessEqual(set(INPUT_FLAG.findall(arguments)), inputs)

    def test_the_family_and_probe_steps_show_their_optional_forms(self) -> None:
        family = self.commands["canary-family.yml"]
        self.assertTrue(any("-f" not in arguments for arguments in family), "the first-key form (no key)")
        self.assertTrue(any("-f key=<key>" in arguments for arguments in family))
        self.assertIn("-f pages-run-id=<pages-run-id>", " ".join(self.commands["canary-probe.yml"]))
        self.assertIn("(empty: the newest)", " ".join(self.section.split()))


class CanaryTemplateFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.section = procedure()
        self.work = Path(tempfile.mkdtemp(prefix="mb-canary-procedure-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)

    def seeded_copy(self) -> tuple[Path, list[str]]:
        """Steps 1-3 of the procedure on a copy of ``canary/``: copy, fill the pins, ``init``."""

        root = self.work / "mod-base-canary"
        shutil.copytree(CANARY, root, symlinks=True)
        for path in sorted((root / ".github").rglob("*")):
            if path.is_file() and "{{PIN}}" in path.read_text(encoding="utf-8"):
                path.write_text(path.read_text(encoding="utf-8").replace("{{PIN}}", PIN)
                                .replace("{{VERSION}}", VERSION), encoding="utf-8")
        before = {path: (root / path).read_bytes() for path in carried_paths()}
        created = tool.init(root, kit_root=ROOT, seed=True, from_config=root / "site" / "mod-base.json")
        for path, data in before.items():
            self.assertEqual((root / path).read_bytes(), data, f"init rewrote the carried {path}")
        return root, created

    def test_the_carried_list_is_exactly_the_template_files_the_canary_holds(self) -> None:
        listed = listed_paths(paragraph(self.section, CARRIED_LEAD))
        self.assertEqual(len(listed), len(set(listed)), listed)
        self.assertEqual(set(listed), carried_paths())
        self.assertTrue({"LICENSE", ".github/CODEOWNERS", "CONTRIBUTING.md", "docs/ai/PROJECT.md"} <= set(listed))

    def test_the_created_list_is_exactly_what_init_creates(self) -> None:
        _root, created = self.seeded_copy()
        listed = listed_paths(paragraph(self.section, CREATED_LEAD))
        self.assertEqual(len(listed), len(set(listed)), listed)
        self.assertEqual(set(listed), set(created))
        self.assertEqual(set(created) | carried_paths(), template_paths(), "every template file is accounted for")
        self.assertFalse(set(created) & carried_paths())

    def test_the_seeded_copy_passes_template_check(self) -> None:
        root, _created = self.seeded_copy()
        self.assertEqual(tool.check(root, kit_root=ROOT), [])

    def test_the_path_reader_rejects_what_is_not_a_path(self) -> None:
        self.assertEqual(listed_paths("`init` prints `created <path>` for `LICENSE`, `.gitignore` and `a/b.md`"),
                         ["LICENSE", ".gitignore", "a/b.md"])
        with self.assertRaises(AssertionError):
            paragraph("first\n\nsecond", CARRIED_LEAD)


if __name__ == "__main__":
    unittest.main()
