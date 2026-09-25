"""``tests/PORTING.md`` (SPEC §7.4): every Pages test file the mods delete has a destination.

The ledger lists every QS/BP test file deleted in SPEC §7.1/§7.2 exactly once, and each row names
either kit test classes that exist, tests that stay in a mod, or a retirement reason. The partially
moved files, the deliberately unported cases and the complete §7.0 feature ledger are checked too.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "tests" / "PORTING.md"

#: SPEC §7.1: the Quick Skin Pages test files deleted by the adoption.
QS_DELETED = (
    "scripts/release/tests/test_pages_site.py",
    "scripts/release/tests/test_pages_artifact_rotation.py",
    "scripts/release/tests/test_pages_selection_api_budget.py",
    "scripts/ci/tests/test_pages_publication_progress.py",
    "scripts/ci/tests/test_pages_workflow_api_budget.py",
)
#: SPEC §7.2: the Block Pops Pages test files deleted by PR A (22 plus the three anchor files).
BP_DELETED = tuple(f"tests/test_pages_{name}.py" for name in (
    "atomic", "branch_discovery", "build_workflow", "collect_workflow", "compact_scope", "compact_selection",
    "discovery_inventory", "nest_lone_download", "newest_source", "publication", "raw_scope", "refresh_cache",
    "refresh_workflow", "rotation_actions", "rotation_inputs", "scoped_cli", "scoped_producer", "site_atomic",
    "site_companions", "site_output", "site_scope", "source_scope",
)) + ("tests/test_visual_anchor.py", "tests/test_visual_anchor_schema2.py", "tests/test_visual_anchor_scoped_producer.py")
#: SPEC §7.4: files that stay in Block Pops while part of them moves to the kit.
BP_PARTIAL = ("tests/test_content_cache.py", "tests/test_release_evidence_download.py")
FEATURES = 54
KIT_REFERENCE = re.compile(r"`(tests/[A-Za-z0-9_]+\.py)::([A-Za-z_][A-Za-z0-9_]*)`")
MOD_REFERENCE = re.compile(r"`(QS|BP) [A-Za-z0-9_./-]+`")


def section(text: str, heading: str) -> str:
    start = text.index(f"\n## {heading}\n")
    end = text.find("\n## ", start + 1)
    return text[start:end if end != -1 else len(text)]


def rows(block: str) -> list[list[str]]:
    """The body rows of the Markdown table in ``block`` (header and separator dropped)."""

    table = [line for line in block.splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in line.strip("|").split(" | ")] for line in table[2:]]


def test_classes(relative: str) -> set[str]:
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    return {node.name for node in tree.body if isinstance(node, ast.ClassDef)}


class PortingLedgerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = LEDGER.read_text(encoding="utf-8")

    def assert_destination(self, label: str, destination: str) -> None:
        """A destination names existing kit test classes, mod tests, or a retirement reason."""

        if destination.startswith("retired:"):
            self.assertGreater(len(destination.removeprefix("retired:").strip()), 20, f"{label}: no reason given")
            return
        kit = KIT_REFERENCE.findall(destination)
        self.assertTrue(kit or MOD_REFERENCE.search(destination), f"{label} lacks a destination")
        for relative, name in kit:
            with self.subTest(row=label, destination=f"{relative}::{name}"):
                self.assertTrue((ROOT / relative).is_file(), f"{relative} does not exist")
                self.assertIn(name, test_classes(relative), f"{relative} defines no {name}")

    def test_every_deleted_test_file_has_exactly_one_destination_row(self) -> None:
        listed = rows(section(self.text, "Deleted test files"))
        paths = [row[0].strip("`") for row in listed]
        required = [*QS_DELETED, *BP_DELETED]
        self.assertEqual(sorted(paths), sorted(required), "the ledger must list exactly the SPEC §7.1/§7.2 deletions")
        self.assertEqual(len(paths), len(set(paths)), "a deleted file is listed twice")
        for path, mod, destination in listed:
            with self.subTest(path=path):
                self.assertEqual(mod, "QS" if path.strip("`") in QS_DELETED else "BP")
                self.assert_destination(path, destination)

    def test_partially_moved_files_name_their_kit_destination(self) -> None:
        listed = rows(section(self.text, "Partially moved test files (kept in the mod)"))
        self.assertEqual(sorted(row[0].strip("`") for row in listed), sorted(BP_PARTIAL))
        for path, _mod, destination in listed:
            with self.subTest(path=path):
                self.assertTrue(KIT_REFERENCE.search(destination), f"{path} names no kit destination")
                self.assert_destination(path, destination)

    def test_every_unported_case_has_a_reason(self) -> None:
        listed = rows(section(self.text, "Old cases not ported"))
        self.assertGreaterEqual(len(listed), 8)
        for origin, case, reason in listed:
            with self.subTest(case=case):
                self.assertRegex(origin, r"^(QS|BP) `[a-z_]+\.py`$")
                self.assertTrue(reason.startswith(("retired:", "moved to", "not ported:")), reason)
                self.assertGreater(len(reason), 30)
                for relative, name in KIT_REFERENCE.findall(reason):
                    self.assertIn(name, test_classes(relative), f"{relative} defines no {name}")

    def test_the_feature_ledger_is_complete(self) -> None:
        listed = rows(section(self.text, "Feature ledger (SPEC §7.0)"))
        self.assertEqual([int(row[0]) for row in listed], list(range(1, FEATURES + 1)))
        for number, feature, disposition, where in listed:
            with self.subTest(feature=number):
                self.assertTrue(feature and disposition and where, f"feature {number} has an empty cell")
                if disposition.startswith("RETIRED"):
                    self.assertGreater(len(where), 3, f"retired feature {number} gives no reason")

    def test_every_kit_reference_in_the_ledger_exists(self) -> None:
        for relative, name in sorted(set(KIT_REFERENCE.findall(self.text))):
            with self.subTest(destination=f"{relative}::{name}"):
                self.assertIn(name, test_classes(relative))


if __name__ == "__main__":
    unittest.main()
