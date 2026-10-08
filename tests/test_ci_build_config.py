"""The protected Build config: its schema and its loader over a real protected checkout."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mod_base.build_ci.config import BUILD_CONFIG_PATH, AdapterFile, BuildConfig, load_build_config, validate_build_config
from mod_base.build_ci.protocol import PROFILES
from mod_base.errors import MbError
from mod_base.model import limits
from tests.helpers import ci_config

SOURCES = {
    "scripts/ci/mod_base_build_adapter.py": b"BUILD_ADAPTER_API = 1\n",
    "scripts/ci/mod_base_build_dispatch.py": b"import mod_base_build_adapter\n",
    "scripts/ci/pr_gate.py": b"def main() -> int:\n    return 0\n",
}


def config_for(sources: dict[str, bytes]) -> dict:
    document = ci_config()
    document["adapter"]["files"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                                    for path, data in sorted(sources.items())]
    return document


def checkout(root: Path, document: dict, sources: dict[str, bytes]) -> Path:
    for path, data in {BUILD_CONFIG_PATH: json.dumps(document).encode("utf-8"), **sources}.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root


class SchemaTests(unittest.TestCase):
    def test_every_field_is_required_and_closed(self) -> None:
        document = ci_config()
        self.assertIs(validate_build_config(document), document)
        self.assertEqual(set(document), {"kind", "schema_version", "repository", "profile", "build_adapter_api",
                                         "adapter", "inventory", "scenario_contract", "bundle", "contexts", "timeouts"})
        for key in document:
            changed = copy.deepcopy(document)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(MbError):
                validate_build_config(changed)

    def test_profiles_are_the_protocol_constant(self) -> None:
        self.assertEqual(PROFILES, ("quick-skin", "block-pops"))
        for profile in PROFILES:
            validate_build_config({**ci_config(), "profile": profile})
        with self.assertRaises(MbError):
            validate_build_config({**ci_config(), "profile": "canary"})

    def test_named_paths_never_alias_or_contain_one_another(self) -> None:
        rejected = [
            ("inventory", "scripts/ci/mod-base-build.json"),
            ("scenario_contract", "scripts/ci/mod_base_build_dispatch.py"),
            ("bundle", "scripts/ci/pr_gate.py"),
            ("bundle", "scripts"),
            ("bundle", "scripts/ci"),
            ("bundle", "release/release-matrix.json"),
            ("bundle", "E2E"),
            ("inventory", "build/release"),
            ("inventory", "build/release/release-matrix.json"),
            ("scenario_contract", "Build/release/contract.json"),
            ("inventory", "ci-envelope.json"),
        ]
        for key, path in rejected:
            document = ci_config()
            document[key]["path"] = path
            with self.subTest(key=key, path=path), self.assertRaises(MbError):
                validate_build_config(document)
        for key, path in (("bundle", "build/staged release"), ("inventory", "release/matrix v3.json")):
            document = ci_config()
            document[key]["path"] = path
            with self.subTest(key=key, path=path), self.assertRaises(MbError):
                validate_build_config(document)
        accepted = ci_config()
        accepted["bundle"]["path"] = "out"
        accepted["inventory"]["path"] = "gradle.properties"
        validate_build_config(accepted)

    def test_the_adapter_closure_is_sorted_complete_and_free_of_aliases(self) -> None:
        def closure(*paths: str) -> dict:
            document = ci_config()
            document["adapter"]["files"] = [{"path": path, "sha256": "0" * 64} for path in paths]
            return document

        entries = [ci_config()["adapter"][key] for key in ("path", "dispatcher", "policy")]
        validate_build_config(closure(*sorted([*entries, "scripts/ci/lib/__init__.py"])))
        for paths in (list(reversed(sorted(entries))),                              # not sorted
                      sorted(entries)[:-1] + ["scripts/ci/zz_other.py"],            # an entry point is missing
                      sorted([*entries, "scripts/ci/PR_GATE.py"]),                  # differs by case only
                      sorted([*entries, "scripts/ci/pr_gate.py/helper.py"]),        # a file as a directory
                      sorted([*entries, "scripts/ci/mod-base-build.json"]),         # the config itself
                      sorted([*entries, "Scripts/ci/helper.py"])):                  # a directory differing by case
            with self.subTest(paths=paths), self.assertRaises(MbError):
                validate_build_config(closure(*paths))

    def test_contexts_are_bounded_printable_ascii(self) -> None:
        for value in ("", " ", "Build ", " Build", "Build\tand verify", "Build\nand verify", "B\u00fcild", "Build \u2713",
                      "<b>Build</b>", "{{PIN}}", "x" * (limits.MAX_CI_STATUS_CONTEXT_CHARS + 1), None, 7, ["Build"]):
            document = ci_config()
            document["contexts"]["build"] = value
            with self.subTest(value=value), self.assertRaises(MbError):
                validate_build_config(document)
        for value in ("Build and verify", "Trusted PR / Build and verify", "ci/build (shadow) #1 [x]",
                      "x" * limits.MAX_CI_STATUS_CONTEXT_CHARS):
            document = ci_config()
            document["contexts"]["build"] = value
            validate_build_config(document)
        same = ci_config()
        same["contexts"]["packaged"] = same["contexts"]["build"]
        with self.assertRaises(MbError):
            validate_build_config(same)


class LoadTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "mod"
        self.root.mkdir()

    def load(self) -> BuildConfig:
        return load_build_config(self.root, repository="example/mod")

    def test_returns_the_exact_bytes_of_the_config_and_its_closure(self) -> None:
        document = config_for(SOURCES)
        checkout(self.root, document, SOURCES)
        raw = (self.root / BUILD_CONFIG_PATH).read_bytes()
        loaded = self.load()
        self.assertEqual(loaded.data, document)
        self.assertEqual(loaded.raw, raw)
        self.assertEqual(loaded.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(loaded.files, tuple(AdapterFile(path, hashlib.sha256(data).hexdigest(), data)
                                             for path, data in sorted(SOURCES.items())))

    def test_an_empty_source_belongs_to_the_closure(self) -> None:
        sources = {**SOURCES, "scripts/ci/__init__.py": b""}
        checkout(self.root, config_for(sources), sources)
        self.assertEqual(self.load().files[0], AdapterFile("scripts/ci/__init__.py", hashlib.sha256(b"").hexdigest(), b""))

    def test_a_source_that_differs_from_its_hash_is_rejected(self) -> None:
        checkout(self.root, config_for(SOURCES), SOURCES)
        (self.root / "scripts/ci/pr_gate.py").write_bytes(SOURCES["scripts/ci/pr_gate.py"] + b"\n")
        with self.assertRaises(MbError) as caught:
            self.load()
        self.assertIn("scripts/ci/pr_gate.py", str(caught.exception))

    def test_missing_linked_or_special_sources_are_rejected(self) -> None:
        def missing(root: Path) -> None:
            (root / "scripts/ci/pr_gate.py").unlink()

        def linked(root: Path) -> None:
            target = root / "scripts/ci/pr_gate.py"
            target.rename(root / "elsewhere.py")
            target.symlink_to(root / "elsewhere.py")

        def linked_parent(root: Path) -> None:
            (root / "scripts").rename(root / "real-scripts")
            (root / "scripts").symlink_to(root / "real-scripts", target_is_directory=True)

        def directory(root: Path) -> None:
            (root / "scripts/ci/pr_gate.py").unlink()
            (root / "scripts/ci/pr_gate.py").mkdir()

        def linked_config(root: Path) -> None:
            target = root / BUILD_CONFIG_PATH
            target.rename(root / "config.json")
            target.symlink_to(root / "config.json")

        def no_config(root: Path) -> None:
            (root / BUILD_CONFIG_PATH).unlink()

        for index, damage in enumerate((missing, linked, linked_parent, directory, linked_config, no_config)):
            root = self.root / str(index)
            root.mkdir()
            checkout(root, config_for(SOURCES), SOURCES)
            load_build_config(root, repository="example/mod")
            damage(root)
            with self.subTest(damage=damage.__name__), self.assertRaises(MbError):
                load_build_config(root, repository="example/mod")

    def test_another_repository_or_an_invalid_document_is_rejected(self) -> None:
        checkout(self.root, config_for(SOURCES), SOURCES)
        with self.assertRaises(MbError):
            load_build_config(self.root, repository="example/other")
        for data in (b"", b"{", b'{"kind": "mod-base.build.config", "kind": "mod-base.build.config"}',
                     json.dumps({**config_for(SOURCES), "permissions": {}}).encode("utf-8"),
                     b" " * (limits.MAX_CI_CONFIG_BYTES + 1)):
            (self.root / BUILD_CONFIG_PATH).write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(MbError):
                self.load()

    def test_file_and_closure_byte_caps_hold(self) -> None:
        oversized = {**SOURCES, "scripts/ci/pr_gate.py": bytes(limits.MAX_CI_ADAPTER_FILE_BYTES + 1)}
        checkout(self.root, config_for(oversized), oversized)
        with self.assertRaises(MbError):
            self.load()
        count = limits.MAX_CI_ADAPTER_TREE_BYTES // limits.MAX_CI_ADAPTER_FILE_BYTES
        block = bytes(limits.MAX_CI_ADAPTER_FILE_BYTES)
        many = {**SOURCES, **{f"scripts/ci/block_{index:02d}.py": block for index in range(count)}}
        root = self.root / "many"
        root.mkdir()
        checkout(root, config_for(many), many)
        with self.assertRaises(MbError) as caught:
            load_build_config(root, repository="example/mod")
        self.assertIn("whole-byte cap", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
