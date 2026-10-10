"""Per-kind compatibility with the immediate predecessor release, in both directions.

``docs/BUILD-E2E-DESIGN.md`` ("Pinning managed callers and schema evolution"): every document kind
writes N and reads N and N-1. A common kind whose schema version is unchanged must stay readable by
the previous release; only a genuinely new kind or an explicitly version-changed format is exempt,
and then the previous reader must reject it, visibly and for that reason. The ledger
``tests/fixtures/documents/compatibility.json`` records the decision for every kind, and no fixture
is left out.

The predecessor is not configured anywhere: it is the newest release ``CHANGELOG.md`` records
before the release this tree becomes (:func:`predecessor`), and its reader is the archived source
of that release under ``tests/fixtures/previous_release``, which reports its own version. So the
ledger cannot keep comparing against an older release once a new one has been cut.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import mod_base
from mod_base.config import validate_config
from mod_base.model import grammar
from mod_base.model.canonical import strict_loads
from mod_base.model.documents import validate_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "documents"
PREVIOUS = ROOT / "tests" / "fixtures" / "previous_release"
ADR = ROOT / "docs" / "adr" / "0007-protected-build-and-packaged-runtime.md"
VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
RELEASE_HEADING = re.compile(f"v{VERSION}")
UNRELEASED_HEADING = re.compile(rf"Unreleased(?: \(planned v{VERSION}\))?")
DECISIONS = ("unchanged", "new-kind", "version-change")
#: Where the previous reader refuses a kind it does not know, and a version it does not read.
UNKNOWN_KIND, UNREADABLE_VERSION = "$.kind", "$.schema_version"
#: Run with the archived release as the only import root: validates a list of documents with that
#: release's readers and reports where each rejection points.
CHECKER = """
import json, sys
import mod_base
from mod_base.errors import MbError
from mod_base.model.documents import validate_document
from mod_base.config import validate_config
outcomes = []
for value in json.loads(sys.stdin.read()):
    try:
        (validate_config if value.get("kind") == "mod-base.config" else validate_document)(value)
    except MbError as error:
        outcomes.append({"path": getattr(error, "path", None), "message": str(error)})
    else:
        outcomes.append(None)
print(json.dumps({"version": mod_base.__version__, "kinds": mod_base.SCHEMA_VERSIONS, "outcomes": outcomes}))
"""

Version = tuple[int, ...]


def documents(directory: Path) -> list[Path]:
    return sorted((directory / "valid").glob("*.json")) + sorted((directory / "config").glob("*.json"))


def version_text(version: Version) -> str:
    return ".".join(map(str, version))


def changelog() -> tuple[bool, Version | None, list[Version]]:
    """``(unreleased, planned, releases)`` of ``CHANGELOG.md``.

    ``releases`` are its ``## vX.Y.Z`` headings in file order. ``unreleased`` says whether an
    ``## Unreleased`` heading opens the file, and ``planned`` is the version that heading plans
    (``## Unreleased (planned vX.Y.Z)``), if it names one. Any other level-2 heading is an error.
    """

    headings = [line[3:] for line in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")]
    opening = UNRELEASED_HEADING.fullmatch(headings[0]) if headings else None
    planned = tuple(map(int, opening.groups())) if opening is not None and opening.group(1) is not None else None
    releases: list[Version] = []
    for heading in headings[1:] if opening is not None else headings:
        match = RELEASE_HEADING.fullmatch(heading)
        if match is None:
            raise AssertionError(f"CHANGELOG.md heading is neither a release nor an opening Unreleased: {heading!r}")
        releases.append(tuple(map(int, match.groups())))
    return opening is not None, planned, releases


def predecessor() -> Version:
    """The release every unchanged kind must stay readable by.

    While the changelog opens with an unreleased section, this tree is the next release in the
    making and the newest recorded release is its predecessor. In a release commit the changelog
    opens with the release being cut, and the predecessor is the one recorded before it.
    """

    unreleased, _planned, releases = changelog()
    return releases[0 if unreleased else 1]


class SchemaEvolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ledger = strict_loads((FIXTURES / "compatibility.json").read_bytes(),
                                  label="compatibility ledger", max_bytes=65536)
        cls.tag = "v" + version_text(predecessor())
        cls.baseline = PREVIOUS / cls.tag
        cls.source = cls.baseline / "source.zip"
        if not cls.source.is_file():
            raise AssertionError(f"the predecessor {cls.tag} has no archived reader: add "
                                 f"tests/fixtures/previous_release/{cls.tag} and move the ledger baseline to it")
        cls.current = {path.name: json.loads(path.read_bytes()) for path in documents(FIXTURES)}

    def previous_reader(self, values: list[Any]) -> dict[str, Any]:
        """The archived release's version, its kinds, and its verdict on each of ``values``: ``None``
        when it accepts the document, else where it rejects it."""

        environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(self.source),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONSAFEPATH": "1"}
        if "SYSTEMROOT" in os.environ:
            environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        with tempfile.TemporaryDirectory() as directory:
            completed = subprocess.run([sys.executable, "-P", "-c", CHECKER], input=json.dumps(values),
                                       capture_output=True, text=True, env=environment, cwd=directory,
                                       timeout=120, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        report = json.loads(completed.stdout)
        self.assertEqual(len(report["outcomes"]), len(values))
        return report

    def test_the_changelog_orders_its_releases_and_records_the_current_one(self) -> None:
        unreleased, planned, releases = changelog()
        self.assertGreaterEqual(len(releases), 2)
        self.assertEqual(releases, sorted(set(releases), reverse=True), "releases are listed once, newest first")
        # The runtime version moves in the release commit only, so it always is the newest release
        # the changelog records: the one being cut, or the one the unreleased work builds on.
        self.assertEqual(version_text(releases[0]), mod_base.__version__)
        if planned is not None:
            self.assertGreater(planned, releases[0], "the planned release follows the newest one")
        self.assertTrue(unreleased or planned is None)

    def test_the_baseline_is_the_immediate_predecessor(self) -> None:
        unreleased, _planned, releases = changelog()
        expected = predecessor()
        # Nothing was released between the baseline and the release this tree becomes.
        self.assertEqual([release for release in releases if release > expected],
                         [] if unreleased else [releases[0]])
        baseline = self.ledger["baseline"]
        self.assertEqual(set(baseline), {"commit", "source_sha256", "tag"})
        self.assertEqual(baseline["tag"], "v" + version_text(expected),
                         "the ledger is decided against the immediate predecessor release")
        self.assertTrue(grammar.is_match(grammar.SHA1, baseline["commit"]))
        # The archive is that release: its own package reports the version.
        self.assertEqual(self.previous_reader([])["version"], version_text(expected))

    def test_readers_accept_current_and_previous_schema_versions(self) -> None:
        for kind, current in mod_base.SCHEMA_VERSIONS.items():
            self.assertEqual(mod_base.readable_schema_versions(kind),
                             frozenset(version for version in (current, current - 1) if version >= 1))

    def test_snapshot_integrity_and_exhaustive_ledger(self) -> None:
        self.assertEqual(set(self.ledger), {"baseline", "kinds"})
        self.assertEqual(set(self.ledger["kinds"]), set(mod_base.SCHEMA_VERSIONS))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.ledger["baseline"]["source_sha256"])
        hashes = json.loads((self.baseline / "sha256.json").read_bytes())
        actual = {path.relative_to(self.baseline).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.baseline.rglob("*") if path.is_file() and path.name != "sha256.json"}
        self.assertEqual(actual, hashes)
        previous = self.previous_reader([])["kinds"]
        self.assertLessEqual(set(previous), set(self.ledger["kinds"]), "old kinds may not disappear")
        for kind, entry in self.ledger["kinds"].items():
            with self.subTest(kind=kind):
                self.assertEqual(set(entry), {"current_version", "previous_version", "decision"})
                self.assertEqual(entry["current_version"], mod_base.SCHEMA_VERSIONS[kind])
                self.assertEqual(entry["previous_version"], previous.get(kind))
                decision = "new-kind" if kind not in previous else (
                    "unchanged" if previous[kind] == mod_base.SCHEMA_VERSIONS[kind] else "version-change")
                self.assertEqual(entry["decision"], decision)
                self.assertIn(entry["decision"], DECISIONS)
                if kind in previous:
                    # N reads N-1: a kind moves by one schema version at a time, and never back.
                    self.assertIn(entry["current_version"] - previous[kind], (0, 1))

    def test_every_new_kind_starts_at_schema_version_one(self) -> None:
        previous = self.previous_reader([])["kinds"]
        new = {kind: entry for kind, entry in self.ledger["kinds"].items() if entry["decision"] == "new-kind"}
        self.assertEqual(set(new), set(mod_base.SCHEMA_VERSIONS) - set(previous))
        for kind, entry in new.items():
            with self.subTest(kind=kind):
                # No fictional predecessor: a kind the previous release never wrote has no earlier
                # version to read.
                self.assertEqual((entry["current_version"], entry["previous_version"]), (1, None))
                self.assertEqual(mod_base.SCHEMA_VERSIONS[kind], 1)
                self.assertEqual(mod_base.readable_schema_versions(kind), frozenset({1}))

    def test_every_current_writer_against_previous_reader(self) -> None:
        names = sorted(self.current)
        # Each fixture, and beside it a document that holds nothing but the fixture's kind.
        values = [value for name in names for value in (self.current[name], {"kind": self.current[name]["kind"]})]
        outcomes = self.previous_reader(values)["outcomes"]
        covered = set()
        for index, name in enumerate(names):
            kind = self.current[name]["kind"]
            covered.add(kind)
            verdict, bare = outcomes[2 * index], outcomes[2 * index + 1]
            decision = self.ledger["kinds"][kind]["decision"]
            with self.subTest(document=name, decision=decision):
                self.assertIsNotNone(bare)
                if decision == "unchanged":
                    self.assertIsNone(verdict)
                    # The previous reader knows the kind: an empty document of it fails on content.
                    self.assertNotEqual(bare["path"], UNKNOWN_KIND)
                elif decision == "new-kind":
                    # Rejected because the kind is unknown, not for anything the fixture holds: the
                    # verdict names the kind and is the one an empty document of that kind gets.
                    self.assertIsNotNone(verdict)
                    self.assertEqual(verdict["path"], UNKNOWN_KIND)
                    self.assertIn(repr(kind), verdict["message"])
                    self.assertEqual(verdict, bare)
                else:
                    self.assertIsNotNone(verdict)
                    self.assertEqual(verdict["path"], UNREADABLE_VERSION)
        self.assertEqual(covered, set(self.ledger["kinds"]), "every kind needs writer evidence")

    def test_the_previous_reader_rejects_a_future_version_as_a_version(self) -> None:
        # What a version-changed format must meet in the previous reader: a rejection of the
        # version itself. Shown with the next version of every kind that reader knows.
        names = sorted(name for name, document in self.current.items()
                       if self.ledger["kinds"][document["kind"]]["decision"] == "unchanged")
        self.assertTrue(names)
        values = [{**self.current[name], "schema_version": self.current[name]["schema_version"] + 1} for name in names]
        for name, verdict in zip(names, self.previous_reader(values)["outcomes"]):
            with self.subTest(document=name):
                self.assertIsNotNone(verdict)
                self.assertEqual(verdict["path"], UNREADABLE_VERSION)

    def test_current_reader_accepts_every_supported_predecessor_fixture(self) -> None:
        paths = documents(self.baseline / "documents")
        archived = [strict_loads(path.read_bytes(), label=path.name, max_bytes=16 * 1024 * 1024) for path in paths]
        outcomes = self.previous_reader(archived)["outcomes"]
        covered = set()
        for path, document, verdict in zip(paths, archived, outcomes):
            kind = document["kind"]
            covered.add(kind)
            with self.subTest(document=path.name):
                self.assertIn(document["schema_version"], mod_base.readable_schema_versions(kind))
                (validate_config if kind == "mod-base.config" else validate_document)(document)
                self.assertIsNone(verdict, "the archived fixture is valid for its own release")
        self.assertEqual(covered, {kind for kind, entry in self.ledger["kinds"].items()
                                   if entry["decision"] != "new-kind"})

    def test_the_decision_record_names_every_new_kind(self) -> None:
        text = ADR.read_text(encoding="utf-8")
        missing = sorted(kind for kind, entry in self.ledger["kinds"].items()
                         if entry["decision"] == "new-kind" and f"`{kind}`" not in text)
        self.assertEqual(missing, [], "ADR 0007 gives every new document kind one line")


if __name__ == "__main__":
    unittest.main()
