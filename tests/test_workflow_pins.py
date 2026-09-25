"""Pins inside mod-base itself (SPEC §1.6, §1.7, §5.3, §5.6).

Every ``uses:`` in the kit is SHA-pinned with a ``# vX.Y.Z`` comment; the kit-owned workflows and
composites use exactly the reviewed action pins both mods use; nothing in mod-base references
mod-base by ``uses:`` except the managed caller and the ``canary/`` files, which the canary
repository copies. Both write every kit reference as ``@{{PIN}} # {{VERSION}}``: the kit cannot
contain its own future commit, so ``template init`` (the caller) and the canary procedure's ``sed``
(docs/OPERATIONS.md, step 2) fill the placeholders in. Composites never ``uses:`` a sibling and
never read ``GITHUB_ACTION_REF``/``GITHUB_ACTION_REPOSITORY``.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_workflow_policy import (CALLEE_PATHS, CALLER_PATH, CHECKOUT, COMPOSITE_PATHS, COMPOSITES,
                                        DEPLOY_PAGES, PINNED_ACTIONS, ROOT, SETUP_PYTHON, UPLOAD, UPLOAD_PAGES)

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
PINNED = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$")
TEMPLATE_PIN = re.compile(r"^The-Plum-Team/mod-base/\.github/workflows/(publish|finalize|rotate)\.yml@\{\{PIN\}\} "
                          r"# \{\{VERSION\}\}$")
#: The only form of a kit reference in a ``canary/`` file: the path of a kit callee or composite at
#: the placeholder pin that the canary procedure fills in (docs/OPERATIONS.md, step 2).
CANARY_KIT_PIN = re.compile(r"^The-Plum-Team/mod-base/(\S+)@\{\{PIN\}\} # \{\{VERSION\}\}$")
#: What a canary kit reference may name: the three callee workflows and the four composites.
CANARY_KIT_TARGETS = frozenset({*(path.relative_to(ROOT).as_posix() for path in CALLEE_PATHS.values()),
                                *(f"actions/{name}" for name in COMPOSITES)})
KIT_TOKEN = re.compile(r"(?i)the-plum-team/mod-base(?:/[A-Za-z0-9._/-]*)?@")
CANARY = ROOT / "canary"
CANARY_CALLER = CANARY / ".github/workflows/pages.yml"
PLACEHOLDERS = ("{{PIN}}", "{{VERSION}}")
OWNED = [*CALLEE_PATHS.values(), CALLER_PATH, *COMPOSITE_PATHS.values()]
#: Where each nested action may appear (a composite never deploys; only the caller deploys).
PLACES = {
    CHECKOUT: {"publish.yml", "finalize.yml", "rotate.yml"},
    SETUP_PYTHON: {"publish.yml", "finalize.yml", "rotate.yml", "prepare-evidence", "publish-family"},
    UPLOAD: {"publish.yml", "finalize.yml", "prepare-evidence", "publish-family"},
    UPLOAD_PAGES: {"publish.yml"},
    DEPLOY_PAGES: {"pages.yml"},
}


def kit_yaml_files() -> list[Path]:
    """Every YAML file of the kit outside ``.git`` (canary files included; they are checked apart)."""

    return sorted(path for pattern in ("*.yml", "*.yaml") for path in ROOT.rglob(pattern)
                  if ".git" not in path.relative_to(ROOT).parts)


def place(path: Path) -> str:
    return path.parent.name if path.name == "action.yml" else path.name


def in_canary(path: Path) -> bool:
    return path.relative_to(ROOT).parts[:1] == ("canary",)


def uses_values(path: Path) -> list[str]:
    """The ``uses:`` values of one YAML file, in order."""

    return [match.group(1) for line in path.read_text(encoding="utf-8").splitlines()
            if (match := USES.match(line)) is not None]


def pin_problem(path: Path, value: str) -> str | None:
    """Why ``value``, a ``uses:`` of the kit file ``path``, is not pinned as its place requires."""

    if in_canary(path) and "mod-base" in value.lower():
        kit = CANARY_KIT_PIN.match(value)
        if kit is None:
            return "a canary kit reference must be @{{PIN}} # {{VERSION}}"
        if kit.group(1) not in CANARY_KIT_TARGETS:
            return f"a canary kit reference names no kit callee or composite: {kit.group(1)}"
        return None
    if path == CALLER_PATH and "mod-base" in value:
        return None if TEMPLATE_PIN.match(value) else "a caller kit reference must be @{{PIN}} # {{VERSION}}"
    if PINNED.match(value) is None:
        return "not SHA-pinned with a # vX.Y.Z comment"
    if value.startswith("./"):
        return "a local action reference"
    return None


class PinTests(unittest.TestCase):
    def test_every_kit_uses_is_sha_pinned_with_a_version_comment(self) -> None:
        for path in kit_yaml_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = USES.match(line)
                if match is None:
                    continue
                with self.subTest(file=path.relative_to(ROOT).as_posix(), line=number):
                    self.assertIsNone(pin_problem(path, match.group(1)), match.group(1))

    def test_pin_rules_reject_every_other_form(self) -> None:
        canary = CANARY / ".github/workflows/canary-producer.yml"
        sha = "0" * 40
        accepted = {
            (canary, "The-Plum-Team/mod-base/actions/setup@{{PIN}} # {{VERSION}}"),
            (canary, "The-Plum-Team/mod-base/.github/workflows/rotate.yml@{{PIN}} # {{VERSION}}"),
            (canary, f"{CHECKOUT} # v7.0.1"),
            (CALLER_PATH, "The-Plum-Team/mod-base/.github/workflows/publish.yml@{{PIN}} # {{VERSION}}"),
            (CALLEE_PATHS["publish"], f"{CHECKOUT} # v7.0.1"),
        }
        rejected = {
            # A canary kit reference is only ever the placeholder, never a real or tag pin...
            (canary, f"The-Plum-Team/mod-base/actions/setup@{sha} # v1.0.0"),
            (canary, "The-Plum-Team/mod-base/actions/setup@v1.0.0"),
            (canary, "The-Plum-Team/mod-base/actions/setup@{{PIN}}"),
            (canary, "The-Plum-Team/mod-base/actions/setup@{{PIN}}  # {{VERSION}}"),
            (canary, "the-plum-team/MOD-BASE/actions/setup@{{PIN}} # {{VERSION}}"),
            # ...naming exactly a kit callee or composite...
            (canary, "The-Plum-Team/mod-base/actions/setup/../../evil@{{PIN}} # {{VERSION}}"),
            (canary, "The-Plum-Team/mod-base/.github/workflows/ci.yml@{{PIN}} # {{VERSION}}"),
            (canary, "The-Plum-Team/mod-base/actions/unknown@{{PIN}} # {{VERSION}}"),
            # ...and every other canary uses stays strictly pinned.
            (canary, "actions/checkout@{{PIN}} # {{VERSION}}"),
            (canary, "actions/checkout@v7"),
            (canary, "./actions/setup"),
            # The placeholder never escapes the caller and canary/.
            (CALLEE_PATHS["publish"], "The-Plum-Team/mod-base/actions/setup@{{PIN}} # {{VERSION}}"),
            (COMPOSITE_PATHS["setup"], "The-Plum-Team/mod-base/actions/setup@{{PIN}} # {{VERSION}}"),
            (CALLER_PATH, "The-Plum-Team/mod-base/actions/setup@{{PIN}} # {{VERSION}}"),
            (CALLER_PATH, f"The-Plum-Team/mod-base/.github/workflows/publish.yml@{sha} # v1.0.0"),
        }
        for path, value in accepted:
            with self.subTest(file=path.relative_to(ROOT).as_posix(), uses=value):
                self.assertIsNone(pin_problem(path, value))
        for path, value in rejected:
            with self.subTest(file=path.relative_to(ROOT).as_posix(), uses=value):
                self.assertIsNotNone(pin_problem(path, value))

    def test_canary_third_party_uses_are_exactly_the_reviewed_pins(self) -> None:
        canary_files = [path for path in kit_yaml_files() if in_canary(path)]
        self.assertIn(CANARY_CALLER, canary_files)
        for path in canary_files:
            for value in uses_values(path):
                if "mod-base" in value.lower():
                    continue
                action, _, comment = value.partition(" # ")
                with self.subTest(file=path.relative_to(ROOT).as_posix(), uses=action):
                    self.assertIn(action, PINNED_ACTIONS)
                    self.assertEqual(comment, PINNED_ACTIONS[action])
                    self.assertTrue(action != DEPLOY_PAGES or path == CANARY_CALLER, "only the caller deploys")

    def test_canary_placeholders_sit_only_on_its_kit_references(self) -> None:
        # docs/OPERATIONS.md step 2 fills every {{PIN}}/{{VERSION}} under the canary's .github and
        # then requires none to be left, so each placeholder must belong to a kit reference there.
        kit_references = 0
        for path in sorted(item for item in CANARY.rglob("*") if item.is_file()):
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_bytes().decode("utf-8", errors="replace")
            references = [value for value in (uses_values(path) if path.suffix in {".yml", ".yaml"} else [])
                          if CANARY_KIT_PIN.match(value)]
            kit_references += len(references)
            with self.subTest(file=relative):
                for placeholder in PLACEHOLDERS:
                    self.assertEqual(text.count(placeholder), len(references), placeholder)
                if references:
                    self.assertTrue(relative.startswith("canary/.github/"), relative)
                for number, line in enumerate(text.splitlines(), start=1):
                    match = USES.match(line)
                    if KIT_TOKEN.search(line):
                        self.assertTrue(match is not None and CANARY_KIT_PIN.match(match.group(1)),
                                        f"line {number} references mod-base other than as a placeholder pin")
        self.assertGreater(kit_references, 0)

    def test_only_the_canary_caller_calls_the_callees_and_exactly_like_the_managed_caller(self) -> None:
        self.assertEqual(uses_values(CANARY_CALLER), uses_values(CALLER_PATH))
        for path in kit_yaml_files():
            if not in_canary(path) or path == CANARY_CALLER:
                continue
            for value in uses_values(path):
                kit = CANARY_KIT_PIN.match(value)
                if kit is not None:
                    with self.subTest(file=path.relative_to(ROOT).as_posix(), uses=value):
                        self.assertTrue(kit.group(1).startswith("actions/"), "a producer runs composites only")

    def test_owned_files_use_exactly_the_reviewed_pins_in_their_places(self) -> None:
        seen = set()
        for path in OWNED:
            for line in path.read_text(encoding="utf-8").splitlines():
                match = USES.match(line)
                if match is None or TEMPLATE_PIN.match(match.group(1)):
                    continue
                action, _, comment = match.group(1).partition(" # ")
                with self.subTest(file=path.relative_to(ROOT).as_posix(), uses=action):
                    self.assertIn(action, PINNED_ACTIONS)
                    self.assertEqual(comment, PINNED_ACTIONS[action])
                    self.assertIn(place(path), PLACES[action])
                seen.add(action)
        self.assertEqual(seen, set(PINNED_ACTIONS))

    def test_mod_base_never_references_itself_outside_canary(self) -> None:
        for path in kit_yaml_files():
            relative = path.relative_to(ROOT).as_posix()
            if relative.startswith("canary/"):
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = USES.match(line)
                with self.subTest(file=relative, line=number):
                    if match is not None and "mod-base" in match.group(1).lower():
                        self.assertEqual(path, CALLER_PATH)
                        self.assertRegex(match.group(1), TEMPLATE_PIN)
                    if path != CALLER_PATH:
                        self.assertNotRegex(line, KIT_TOKEN)

    def test_template_placeholders_only_on_callee_uses(self) -> None:
        text = CALLER_PATH.read_text(encoding="utf-8")
        self.assertEqual(text.count("{{PIN}}"), 3)
        self.assertEqual(text.count("{{VERSION}}"), 3)
        for path in OWNED:
            if path != CALLER_PATH:
                self.assertNotIn("{{", path.read_text(encoding="utf-8").replace("${{", ""),
                                 path.relative_to(ROOT).as_posix())

    def test_composites_resolve_the_kit_only_through_their_action_path(self) -> None:
        for path in COMPOSITE_PATHS.values():
            text = path.read_text(encoding="utf-8")
            with self.subTest(composite=path.parent.name):
                self.assertNotIn("GITHUB_ACTION_REF", text)
                self.assertNotIn("GITHUB_ACTION_REPOSITORY", text)
                self.assertNotIn("github.action_ref", text)
                self.assertNotIn("github.action_repository", text)
                for reference in re.findall(r"\$GITHUB_ACTION_PATH/[^\s\"']*", text):
                    self.assertTrue(reference.startswith("$GITHUB_ACTION_PATH/../../"), reference)
                    self.assertRegex(reference, r"^\$GITHUB_ACTION_PATH/\.\./\.\./(src|requirements|tools)(?:/|$)")
                self.assertNotIn("actions/checkout@", text)


if __name__ == "__main__":
    unittest.main()
