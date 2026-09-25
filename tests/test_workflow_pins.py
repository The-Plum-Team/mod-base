"""Pins inside mod-base itself (SPEC §1.6, §1.7, §5.3, §5.6).

Every ``uses:`` in the kit is SHA-pinned with a ``# vX.Y.Z`` comment; the kit-owned workflows and
composites use exactly the reviewed action pins both mods use; nothing in mod-base references
mod-base by ``uses:`` (only ``canary/`` files, which the canary repository copies, and the managed
caller's ``{{PIN}}``/``{{VERSION}}`` placeholders); composites never ``uses:`` a sibling and never
read ``GITHUB_ACTION_REF``/``GITHUB_ACTION_REPOSITORY``.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_workflow_policy import (CALLEE_PATHS, CALLER_PATH, CHECKOUT, COMPOSITE_PATHS, DEPLOY_PAGES,
                                        PINNED_ACTIONS, ROOT, SETUP_PYTHON, UPLOAD, UPLOAD_PAGES)

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(.*?)\s*$")
PINNED = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$")
TEMPLATE_PIN = re.compile(r"^The-Plum-Team/mod-base/\.github/workflows/(publish|finalize|rotate)\.yml@\{\{PIN\}\} "
                          r"# \{\{VERSION\}\}$")
KIT_TOKEN = re.compile(r"(?i)the-plum-team/mod-base(?:/[A-Za-z0-9._/-]*)?@")
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


class PinTests(unittest.TestCase):
    def test_every_kit_uses_is_sha_pinned_with_a_version_comment(self) -> None:
        for path in kit_yaml_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                match = USES.match(line)
                if match is None:
                    continue
                value = match.group(1)
                with self.subTest(file=path.relative_to(ROOT).as_posix(), line=number):
                    if path == CALLER_PATH and "mod-base" in value:
                        self.assertRegex(value, TEMPLATE_PIN)
                    else:
                        self.assertRegex(value, PINNED)
                        self.assertNotRegex(value, r"^\./")

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
