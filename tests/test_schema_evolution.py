"""Schema evolution across releases (SPEC §1.6): release N reads N and N-1 and writes N.

From v1.1 on, this test validates every document the current release writes (the valid fixtures
of ``tests/fixtures/documents/valid``) with the **previous release's** validators: a copy of that
release's ``src/mod_base`` kept at ``tests/fixtures/previous_release/<tag>/src`` (fetched once at
the previous tag, never over the network during a test). Within one ``schema_version`` only
optional fields may be added, so every current document must still validate there.

v0.9.0 and v1.0.0 are the first releases of the v1 schemas: there is no previous release to
compare with, which this test asserts (and then passes) instead of skipping.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

import mod_base

ROOT = Path(__file__).resolve().parents[1]
VALID = ROOT / "tests" / "fixtures" / "documents" / "valid"
PREVIOUS = ROOT / "tests" / "fixtures" / "previous_release"
CHANGELOG = ROOT / "CHANGELOG.md"
#: The first release whose predecessor wrote the same v1 schemas.
FIRST_EVOLVING_RELEASE = (1, 1, 0)
RELEASE_HEADING = re.compile(r"^## v(\d+)\.(\d+)\.(\d+)$", re.MULTILINE)
#: A child validating one document with the previous release's code (stdin: the JSON document).
CHECKER = ("import json, sys\n"
           "from mod_base.model.documents import validate_document\n"
           "validate_document(json.loads(sys.stdin.read()))\n")


def version(text: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in text.split("."))
    return major, minor, patch


def releases() -> list[tuple[int, int, int]]:
    """Every release the changelog records, newest first."""

    found = [tuple(int(part) for part in match) for match in RELEASE_HEADING.findall(CHANGELOG.read_text("utf-8"))]
    return sorted(found, reverse=True)  # type: ignore[arg-type]


def previous_release() -> tuple[int, int, int] | None:
    """The release the current one must stay readable by, or ``None`` before v1.1."""

    current = version(mod_base.__version__)
    if current < FIRST_EVOLVING_RELEASE:
        return None
    older = [release for release in releases() if release < current]
    return older[0] if older else None


class SchemaEvolutionTest(unittest.TestCase):
    def test_the_changelog_records_the_current_release(self) -> None:
        self.assertIn(version(mod_base.__version__), releases())

    def test_readers_accept_the_current_and_the_previous_schema_version(self) -> None:
        for kind, current in mod_base.SCHEMA_VERSIONS.items():
            with self.subTest(kind=kind):
                self.assertEqual(mod_base.readable_schema_versions(kind),
                                 frozenset(value for value in (current, current - 1) if value >= 1))

    def test_the_previous_release_still_reads_every_current_document(self) -> None:
        previous = previous_release()
        if previous is None:
            # v0.9/v1.0: the first v1 release; nothing older exists to read the current documents.
            self.assertLess(version(mod_base.__version__), FIRST_EVOLVING_RELEASE)
            self.assertEqual(set(mod_base.SCHEMA_VERSIONS.values()), {1})
            return
        tag = "v{}.{}.{}".format(*previous)
        source = PREVIOUS / tag / "src"
        self.assertTrue((source / "mod_base" / "__init__.py").is_file(),
                        f"copy the {tag} release's src/ into tests/fixtures/previous_release/{tag}/src")
        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(source),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"}
        for path in sorted(VALID.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(document=path.name):
                completed = subprocess.run([sys.executable, "-P", "-c", CHECKER], input=json.dumps(document),
                                           capture_output=True, text=True, env=environment, timeout=120, check=False)
                self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
