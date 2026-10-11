"""The protected seed of a candidate's Gradle home: its configuration, its key and its export.

``seeds.gradle`` and ``seeds.runtime`` of the protected Build config, the cache key ``ci seed-key``
derives from protected files of the default branch, and the runner side of ``ci seed-export``.
Root's copy of a locked candidate's home (``gradle_cache.export_privileged_gradle_seed``) runs with
real accounts in ``tests/ci_linux_worker.py``; here root is a stand-in that lays out what it leaves.
"""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mod_base.build_ci import commands_seed, lifecycle, seeds
from mod_base.build_ci.config import SEED_KINDS, seed_key_files, validate_build_config
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_config, ci_plan

KEY_FILES = ("gradle.properties", "gradle/verification-metadata.xml", "gradle/wrapper/gradle-wrapper.properties")


def seeded(**kinds: tuple[str, ...]) -> dict:
    document = ci_config()
    document["seeds"] = {kind: {"key_files": [{"path": path} for path in paths]} for kind, paths in kinds.items()}
    return document


class SeedConfigTests(unittest.TestCase):
    def test_seeds_are_optional_per_kind_and_keyed_by_sorted_protected_files(self) -> None:
        self.assertEqual(SEED_KINDS, ("gradle", "runtime"))
        self.assertNotIn("seeds", ci_config())
        for kind in SEED_KINDS:
            self.assertIsNone(seed_key_files(ci_config(), kind), "no seed unless the protected config names one")
        both = seeded(gradle=KEY_FILES, runtime=("e2e/runtime-recipes.json", "gradle.properties"))
        validate_build_config(both)
        self.assertEqual(seed_key_files(both, "gradle"), KEY_FILES)
        self.assertEqual(seed_key_files(both, "runtime"), ("e2e/runtime-recipes.json", "gradle.properties"))
        validate_build_config(seeded(runtime=("gradle.properties",)))
        most = tuple(f"keys/{index:02d}.txt" for index in range(limits.MAX_CI_SEED_KEY_FILES))
        validate_build_config(seeded(gradle=most))
        # A key file may be any protected file, the inventory and an adapter source included.
        validate_build_config(seeded(gradle=("release/release-matrix.json", "scripts/ci/pr_gate.py")))
        with self.assertRaises(MbError):
            seed_key_files(both, "policy")

    def test_a_seed_entry_is_closed_and_bounded(self) -> None:
        rejected = {
            "no kind": {},
            "an unknown kind": {"policy": {"key_files": [{"path": "gradle.properties"}]}},
            "no key file": {"gradle": {"key_files": []}},
            "one too many": {"gradle": {"key_files": [{"path": f"keys/{index:02d}.txt"}
                                                      for index in range(limits.MAX_CI_SEED_KEY_FILES + 1)]}},
            "not sorted": {"gradle": {"key_files": [{"path": "gradle/b"}, {"path": "gradle/a"}]}},
            "repeated": {"gradle": {"key_files": [{"path": "gradle.properties"}, {"path": "gradle.properties"}]}},
            "a hash, like an adapter file": {"gradle": {"key_files": [{"path": "a", "sha256": "0" * 64}]}},
            "a bare path": {"gradle": {"key_files": ["gradle.properties"]}},
            "a cache key": {"gradle": {"key_files": [{"path": "gradle.properties"}], "key": "mine"}},
            "a restore prefix": {"gradle": {"key_files": [{"path": "gradle.properties"}], "restore_keys": ["mb-"]}},
            "a path to restore into": {"gradle": {"key_files": [{"path": "gradle.properties"}], "path": "/tmp"}},
            "a path outside the repository": {"gradle": {"key_files": [{"path": "../gradle.properties"}]}},
            "an absolute path": {"gradle": {"key_files": [{"path": "/etc/passwd"}]}},
            "Git metadata": {"gradle": {"key_files": [{"path": ".git/HEAD"}]}},
            "a list": [{"gradle": {"key_files": [{"path": "gradle.properties"}]}}],
            "null": None,
        }
        for label, value in rejected.items():
            document = ci_config()
            document["seeds"] = copy.deepcopy(value)
            with self.subTest(case=label), self.assertRaises(MbError):
                validate_build_config(document)


class SeedKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="seed key ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "mod"
        for path in KEY_FILES:
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_bytes(f"{path}\n".encode())
        self.document = seeded(gradle=KEY_FILES, runtime=("gradle.properties",))

    def key(self, kind: str = "gradle", unit: str = "1.20.1", document: dict | None = None) -> str | None:
        return seeds.seed_key(self.root, document or self.document, kind, unit)

    def test_the_key_is_the_format_kind_unit_and_digest_of_the_protected_files(self) -> None:
        files = [{"path": path, "sha256": hashlib.sha256(f"{path}\n".encode()).hexdigest()} for path in KEY_FILES]
        material = {"format": "mb-seed-v1", "runner": "ubuntu-24.04-x64", "kind": "gradle", "unit": "1.20.1",
                    "files": files}
        self.assertEqual(self.key(), "mb-seed-v1-gradle-1.20.1-" + hashlib.sha256(canonical_json(material)).hexdigest())
        self.assertEqual(self.key(), self.key(), "equal protected files give equal keys on every run")
        self.assertIsNone(self.key(document=ci_config()))
        self.assertIsNone(self.key("runtime", document=seeded(gradle=KEY_FILES)))

    def test_every_input_of_the_key_changes_it(self) -> None:
        base = self.key()
        others = {self.key("runtime"), self.key(unit="1.21.1"), self.key(unit="1.20.1-fabric")}
        (self.root / "gradle.properties").write_bytes(b"gradle.properties\nchanged=1\n")
        others.add(self.key())
        # A file the checkout does not have is a state of its own, different from an empty file.
        os.unlink(self.root / "gradle.properties")
        absent = self.key()
        (self.root / "gradle.properties").write_bytes(b"")
        others |= {absent, self.key()}
        self.assertNotIn(base, others)
        self.assertEqual(len(others), 6)

    def test_the_key_is_one_actions_cache_accepts_for_every_unit(self) -> None:
        for unit in ("1.20.1", "neoforge-1.21.10", "a" * 80, "0"):
            for kind in SEED_KINDS:
                key = self.key(kind, unit)
                with self.subTest(kind=kind, unit=unit):
                    self.assertLessEqual(len(key), 512)
                    self.assertNotIn(",", key)
                    self.assertTrue(key.startswith(f"mb-seed-v1-{kind}-{unit}-"))
        for unit in ("", "A", "a--b", "../x", "a,b", "a" * 81, None):
            with self.subTest(unit=unit), self.assertRaises(MbError):
                self.key(unit=unit)

    def test_a_linked_special_or_oversized_key_file_is_refused(self) -> None:
        target = self.root / "gradle.properties"
        os.unlink(target)
        os.mkdir(target)
        with self.assertRaises(MbError):
            self.key()
        os.rmdir(target)
        with mock.patch.object(limits, "MAX_CI_SEED_KEY_FILE_BYTES", 4):
            target.write_bytes(b"12345")
            with self.assertRaises(MbError):
                self.key()
        if hasattr(os, "symlink"):
            os.unlink(target)
            try:
                os.symlink(self.root / KEY_FILES[1], target)
            except OSError:
                return  # A Windows host without symlink privilege cannot plant one.
            with self.assertRaisesRegex(MbError, "crosses a symlink"):
                self.key()

    def test_a_job_keys_only_a_planned_unit_of_its_own_kind(self) -> None:
        plan = ci_plan()
        config = SimpleNamespace(data=self.document)

        def job(producer: str) -> SimpleNamespace:
            return SimpleNamespace(record={"producer": producer}, config=config)

        with mock.patch.object(lifecycle, "read_plan", return_value=plan):
            self.assertEqual(seeds.derive_seed_key(job("build"), self.root, "gradle", "target-a"),
                             self.key("gradle", "target-a"))
            self.assertEqual(seeds.derive_seed_key(job("packaged"), self.root, "runtime", "lane-a"),
                             self.key("runtime", "lane-a"))
            for producer, kind, unit in (("build", "gradle", "lane-a"), ("build", "gradle", "target-b"),
                                         ("packaged", "runtime", "target-a"), ("packaged", "gradle", "target-a"),
                                         ("build", "runtime", "lane-a"), ("status", "gradle", "target-a"),
                                         ("build", "policy", "target-a")):
                with self.subTest(producer=producer, kind=kind, unit=unit), self.assertRaises(MbError):
                    seeds.derive_seed_key(job(producer), self.root, kind, unit)


class SeedExportTests(unittest.TestCase):
    """The runner side of ``ci seed-export``, with a stand-in for root's ``export-seed``."""

    EXECUTION = {"returncode": 0, "truncated": False, "log_bytes": 3, "log_sha256": "a" * 64}

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="seed export ")
        self.addCleanup(temporary.cleanup)
        self.temporary = Path(temporary.name)
        self.exported = self.temporary / "seed-export"
        self.output = self.temporary / "runner-temp" / "mb-seed"
        self.output.parent.mkdir()
        self.logged: list[str] = []
        self.root_calls: list[str] = []
        self.files = {"caches/modules-2/files-2.1/net.fabricmc/yarn/1.20.1+build.10/2d1f/yarn.jar": b"yarn",
                      "caches/modules-2/modules-2.lock": b"",
                      "wrapper/dists/gradle-9.8.0-bin/5e/gradle-9.8.0-bin.zip.ok": b""}
        candidate = SimpleNamespace(uid=2000, gid=2000)
        self.worker = SimpleNamespace(accounts={"candidate": candidate, "validator": SimpleNamespace(uid=2001, gid=2001)},
                                      validator=SimpleNamespace(uid=2001, gid=2001), python="/usr/bin/python3",
                                      boundary=SimpleNamespace(uid=os.getuid() if hasattr(os, "getuid") else 0,
                                                               gid=os.getgid() if hasattr(os, "getgid") else 0))
        patches = [
            mock.patch.object(seeds, "SEED_EXPORT_ROOT", self.exported),
            mock.patch.object(seeds, "request_seed_export", side_effect=self.request),
            mock.patch.object(lifecycle, "_root", side_effect=self.root),
            mock.patch.object(lifecycle, "rest_workers"),
            mock.patch.object(seeds, "authenticate_host_boundary"),
            mock.patch.object(seeds, "_layout"),
            mock.patch.object(seeds, "authenticate_tree_private_access"),
            mock.patch.object(lifecycle, "read_run", side_effect=lambda job: self.run),
            mock.patch.object(lifecycle, "read_seal", side_effect=lambda job: {"hook": self.run["hook"]}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.run = {"hook": "build_target", "unit_id": "target-a", "succeeded": True, "error": None, **self.EXECUTION}

    def request(self, **arguments: object) -> str:
        self.assertEqual(arguments["execution"], self.EXECUTION)
        self.assertIs(arguments["candidate"], self.worker.accounts["candidate"])
        return "nonce"

    def root(self, job: object, python: str, operation: str, nonce: str) -> None:
        self.assertEqual((operation, nonce), ("export-seed", "nonce"))
        self.root_calls.append(operation)
        for path, data in self.files.items():
            (self.exported / path).parent.mkdir(parents=True, exist_ok=True)
            (self.exported / path).write_bytes(data)
        self.exported.mkdir(exist_ok=True)

    def job(self, *, pr: int = 0, document: dict | None = None) -> SimpleNamespace:
        return SimpleNamespace(record={"subject": {"pr_number": pr}}, subject={"pr_number": pr},
                               config=SimpleNamespace(data=document or seeded(gradle=KEY_FILES, runtime=KEY_FILES)))

    def export(self, kind: str = "gradle", **job: object) -> list | None:
        return seeds.export_seed(self.job(**job), self.worker, kind, self.output, log=self.logged.append)

    def test_a_protected_job_writes_exactly_the_exported_seed_roots(self) -> None:
        records = self.export()
        self.assertEqual(self.root_calls, ["export-seed"])
        self.assertEqual([record["path"] for record in records], sorted(self.files))
        self.assertEqual({path.relative_to(self.output).as_posix(): path.read_bytes()
                          for path in self.output.rglob("*") if path.is_file()}, self.files)
        self.assertEqual(self.logged, [])
        lifecycle.rest_workers.assert_called_once_with(self.worker.accounts)

    def test_a_lane_exports_its_runtime_seed_after_run_lane(self) -> None:
        self.run = {**self.run, "hook": "run_lane", "unit_id": "lane-a"}
        self.assertEqual(len(self.export("runtime")), len(self.files))
        with self.assertRaises(MbError):
            self.export("gradle")

    def test_a_candidate_that_left_nothing_saves_nothing(self) -> None:
        self.files = {}
        self.assertEqual(self.export(), [])
        self.assertFalse(os.path.lexists(self.output))

    def test_preconditions_are_rejections(self) -> None:
        cases = {
            "a pull request": dict(pr=7),
            "no seed of this kind": dict(document=seeded(runtime=KEY_FILES)),
            "no seed at all": dict(document=ci_config()),
        }
        for label, job in cases.items():
            with self.subTest(case=label), self.assertRaises(MbError):
                self.export(**job)
        for run in ({**self.run, "succeeded": False, "error": "failed", "returncode": 1},
                    {**self.run, "hook": "policy", "unit_id": None}):
            self.run = run
            with self.subTest(run=run["hook"]), self.assertRaises(MbError):
                self.export()
        self.assertEqual(self.root_calls, [], "root never copies a home it was not entitled to export")
        self.exported.mkdir()
        self.run = {**self.run, "hook": "build_target", "succeeded": True, "error": None, "returncode": 0}
        with self.assertRaisesRegex(MbError, "runs once"):
            self.export()

    def test_what_cannot_be_exported_declines_the_seed_instead_of_failing_the_job(self) -> None:
        # A restore that failed half-way left the directory: it is neither replaced nor saved.
        self.output.mkdir()
        self.assertIsNone(self.export())
        self.assertEqual(self.root_calls, [])
        self.output.rmdir()
        self.assertEqual(self.logged, ["seed-export: no gradle seed, mb-seed exists already\n"])
        # Root refusing the home (a link, a special file, the bounds) is a decline too.
        self.logged.clear()
        with mock.patch.object(lifecycle, "_root", side_effect=MbError("the candidate's Gradle home holds a link")):
            self.assertIsNone(self.export())
        self.assertEqual(self.logged, ["seed-export: no gradle seed, the candidate's Gradle home holds a link\n"])
        self.assertFalse(os.path.lexists(self.output))
        lifecycle.rest_workers.assert_called_with(self.worker.accounts)

    def test_the_command_says_whether_there_is_a_seed_to_save(self) -> None:
        output = self.temporary / "github-output"
        args = SimpleNamespace(repo=Path("mod"), config=None, state=Path("state"), kind="gradle",
                               output=self.output, github_output=output)
        with mock.patch.object(commands_seed.lifecycle, "open_job", return_value=self.job()), \
                mock.patch.object(commands_seed.lifecycle, "open_worker", return_value=self.worker), \
                mock.patch.object(commands_seed.runtime, "build_invocation"), \
                mock.patch("sys.stdout.write") as write:
            self.assertEqual(commands_seed.run_seed_export(args), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "saved=true\n")
            self.assertIn("ready to save", write.call_args_list[-1].args[0])
            output.unlink()
            with mock.patch.object(seeds, "export_seed", return_value=None):
                self.assertEqual(commands_seed.run_seed_export(args), 0)
            self.assertEqual(output.read_text(encoding="utf-8"), "saved=false\n")


if __name__ == "__main__":
    unittest.main()
