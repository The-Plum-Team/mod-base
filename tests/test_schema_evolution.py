"""Per-kind compatibility with every current writer and immutable predecessor fixture.

An unchanged kind/version must remain readable by the predecessor. New kinds must be
advertised in the ledger and rejected by its reader, never omitted from testing.
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

import mod_base
from mod_base.config import validate_config
from mod_base.model.canonical import strict_loads
from mod_base.model.documents import validate_document

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests' / 'fixtures' / 'documents'
PREVIOUS = ROOT / 'tests' / 'fixtures' / 'previous_release'
RELEASE_HEADING = re.compile(r'^## v(\d+)\.(\d+)\.(\d+)$', re.MULTILINE)
CHECKER = '''
import json, sys
import mod_base
from mod_base.errors import MbError
from mod_base.model.documents import validate_document
from mod_base.config import validate_config
value = json.loads(sys.stdin.read())
if value is None:
    print(json.dumps(mod_base.SCHEMA_VERSIONS, sort_keys=True))
else:
    try:
        (validate_config if value.get('kind') == 'mod-base.config' else validate_document)(value)
    except MbError:
        sys.exit(2)
'''


def documents(directory):
    return sorted((directory / 'valid').glob('*.json')) + sorted((directory / 'config').glob('*.json'))


class SchemaEvolutionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = strict_loads((FIXTURES / 'compatibility.json').read_bytes(),
                                 label='compatibility ledger', max_bytes=65536)
        cls.baseline = PREVIOUS / cls.ledger['baseline']['tag']
        cls.source = cls.baseline / 'source.zip'

    def previous_reader(self, document):
        environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'PYTHONPATH': str(self.source),
                       'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONSAFEPATH': '1'}
        if 'SYSTEMROOT' in os.environ:
            environment['SYSTEMROOT'] = os.environ['SYSTEMROOT']
        with tempfile.TemporaryDirectory() as directory:
            return subprocess.run([sys.executable, '-P', '-c', CHECKER], input=json.dumps(document),
                                  capture_output=True, text=True, env=environment, cwd=directory,
                                  timeout=120, check=False)

    def test_the_changelog_records_the_current_release(self):
        releases = {tuple(map(int, parts)) for parts in RELEASE_HEADING.findall((ROOT / 'CHANGELOG.md').read_text('utf-8'))}
        self.assertIn(tuple(map(int, mod_base.__version__.split('.'))), releases)

    def test_readers_accept_current_and_previous_schema_versions(self):
        for kind, current in mod_base.SCHEMA_VERSIONS.items():
            self.assertEqual(mod_base.readable_schema_versions(kind), frozenset(v for v in (current, current - 1) if v >= 1))

    def test_snapshot_integrity_and_exhaustive_ledger(self):
        self.assertEqual(set(self.ledger), {'baseline', 'kinds'})
        self.assertEqual(set(self.ledger['kinds']), set(mod_base.SCHEMA_VERSIONS))
        baseline = self.ledger['baseline']
        self.assertEqual(baseline['commit'], 'f99ef433b66c729a869a2c1fbf038332c8b2f3e8')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), baseline['source_sha256'])
        hashes = json.loads((self.baseline / 'sha256.json').read_bytes())
        actual = {path.relative_to(self.baseline).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.baseline.rglob('*') if path.is_file() and path.name != 'sha256.json'}
        self.assertEqual(actual, hashes)
        completed = self.previous_reader(None)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        previous = json.loads(completed.stdout)
        self.assertLessEqual(set(previous), set(self.ledger['kinds']), 'old kinds may not disappear')
        for kind, entry in self.ledger['kinds'].items():
            with self.subTest(kind=kind):
                self.assertEqual(set(entry), {'current_version', 'previous_version', 'decision'})
                self.assertEqual(entry['current_version'], mod_base.SCHEMA_VERSIONS[kind])
                self.assertEqual(entry['previous_version'], previous.get(kind))
                decision = 'new-kind' if kind not in previous else (
                    'unchanged' if previous[kind] == mod_base.SCHEMA_VERSIONS[kind] else 'version-change')
                self.assertEqual(entry['decision'], decision)

    def test_every_current_writer_against_previous_reader(self):
        covered = set()
        for path in documents(FIXTURES):
            document = json.loads(path.read_bytes())
            kind = document['kind']
            covered.add(kind)
            entry = self.ledger['kinds'][kind]
            with self.subTest(document=path.name):
                completed = self.previous_reader(document)
                self.assertEqual(completed.returncode, 0 if entry['decision'] == 'unchanged' else 2,
                                 completed.stderr[-2000:])
        self.assertEqual(covered, set(self.ledger['kinds']), 'every kind needs writer evidence')

    def test_current_reader_accepts_every_supported_predecessor_fixture(self):
        covered = set()
        for path in documents(self.baseline / 'documents'):
            document = strict_loads(path.read_bytes(), label=path.name, max_bytes=16 * 1024 * 1024)
            kind = document['kind']
            covered.add(kind)
            with self.subTest(document=path.name):
                self.assertIn(document['schema_version'], mod_base.readable_schema_versions(kind))
                (validate_config if kind == 'mod-base.config' else validate_document)(document)
                completed = self.previous_reader(document)
                self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        self.assertEqual(covered, {kind for kind, entry in self.ledger['kinds'].items()
                                  if entry['decision'] != 'new-kind'})


if __name__ == '__main__':
    unittest.main()
