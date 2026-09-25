from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mod_base.config import (
    DEFAULT_CONFIG_PATH,
    THEME_KEYS,
    Config,
    check_repository,
    load_config,
    parse_config,
    validate_config,
)
from mod_base.errors import MbError
from mod_base.model.canonical import StrictJsonError, canonical_json
from mod_base.model.validators import DocumentError
from tests.helpers import DOCUMENT_FIXTURES, apply_mutation, load_fixture, pattern_png

CONFIGS = DOCUMENT_FIXTURES / "config"


def make_repository(root: Path, config: dict, *, icon: bytes | None = None) -> None:
    """Create the files a config's repository checks require under ``root``."""

    (root / "site").mkdir(parents=True, exist_ok=True)
    (root / DEFAULT_CONFIG_PATH).write_bytes(canonical_json(config))
    adapter = config["adapter"]
    for relative in (adapter["path"], adapter.get("fixtures_path"), config["source"]["workflow"],
                     *(family["producer"]["workflow"] for family in config["families"])):
        if relative:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text("# placeholder\n", encoding="utf-8")
    for entry in adapter["python_path"]:
        (root / entry).mkdir(parents=True, exist_ok=True)
    if config["project"]["icon"] is not None:
        (root / config["project"]["icon"]["path"]).write_bytes(icon if icon is not None else pattern_png(64, 64))


class SampleConfigsTest(unittest.TestCase):
    def test_quick_skin_and_block_pops_samples_validate(self) -> None:
        for name in ("qs", "bp"):
            with self.subTest(name=name):
                config = parse_config((CONFIGS / f"{name}.json").read_bytes(), path=f"{name}.json")
                self.assertEqual(config.canonical_branch, "master")
                self.assertEqual(set(config.theme["dark"]), set(THEME_KEYS))

    def test_quick_skin_values(self) -> None:
        config = parse_config((CONFIGS / "qs.json").read_bytes())
        self.assertEqual(config.targets, {"mode": "default-branch", "max": 64})
        self.assertEqual(config.image_policy(), {"source_size": [1920, 1080], "derivative_box": [1600, 900],
                                                 "webp_quality": 82, "webp_method": 6, "pixel_metrics_version": 1})
        self.assertEqual(config.network_hooks, frozenset({"authenticate_extensions", "compose", "verify_publication"}))
        self.assertEqual(config.family("mod-compatibility")["retention_days"], 7)
        self.assertIsNone(config.theme["light"])
        self.assertEqual(config.admission["mode"], "progress")
        with self.assertRaises(MbError):
            config.family("other")

    def test_block_pops_values(self) -> None:
        config = parse_config((CONFIGS / "bp.json").read_bytes())
        self.assertEqual(config.targets["mode"], "enrolled-branches")
        self.assertEqual(config.source["attestation_job"], "Attest exact tested packaged tree / Verify exact tested tree")
        self.assertEqual(config.image_policy()["source_size"], [1600, 900])
        self.assertEqual(config.families, [])
        self.assertIsNotNone(config.theme["light"])
        self.assertEqual(config.anchor["successor_grace_days"], 8)
        self.assertEqual(config.adapter_timeout_seconds, 600)
        self.assertEqual(config.extension_names, frozenset({"block-pops.aggregate_scope"}))

    def test_timeout_defaults_to_600(self) -> None:
        document = apply_mutation(load_fixture("config/qs.json"), {"delete": ["/adapter/timeout_seconds"]})
        self.assertEqual(Config(data=validate_config(document), sha256="", path="x").adapter_timeout_seconds, 600)


class ConfigMutationsTest(unittest.TestCase):
    def test_mutation_cases(self) -> None:
        cases = load_fixture("invalid/config-mutations.json")["cases"]
        self.assertEqual(len({case["name"] for case in cases}), len(cases))
        for case in cases:
            document = apply_mutation(load_fixture(f"config/{case['fixture']}.json"), case)
            with self.subTest(case=case["name"]):
                if case["expect"] == "valid":
                    validate_config(document)
                else:
                    with self.assertRaises(DocumentError) as caught:
                        validate_config(document)
                    self.assertIn(case["error"], str(caught.exception))

    def test_parse_config_prefixes_the_file_path(self) -> None:
        document = apply_mutation(load_fixture("config/qs.json"), {"set": [["/theme/dark/bg", "red"]]})
        with self.assertRaises(DocumentError) as caught:
            parse_config(canonical_json(document), path="site/mod-base.json")
        self.assertTrue(str(caught.exception).startswith("site/mod-base.json:$.theme.dark.bg"))

    def test_strict_json_is_enforced(self) -> None:
        with self.assertRaises(StrictJsonError):
            parse_config(b'{"kind":"mod-base.config","kind":"mod-base.config"}')
        with self.assertRaises(StrictJsonError):
            parse_config(b"x" * (256 * 1024 + 1))


class RepositoryChecksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.qs = load_fixture("config/qs.json")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_loads_a_complete_repository(self) -> None:
        make_repository(self.root, self.qs)
        config = load_config(self.root)
        self.assertEqual(config.path, "site/mod-base.json")
        self.assertEqual(len(config.sha256), 64)
        self.assertEqual(load_config(self.root, self.root / "site/mod-base.json").sha256, config.sha256)

    def test_explicit_config_outside_the_repository(self) -> None:
        repository = self.root / "repo"
        make_repository(repository, self.qs)
        outside = self.root / "outside.json"
        outside.write_bytes((repository / "site/mod-base.json").read_bytes())
        self.assertEqual(load_config(repository, outside).path, "outside.json")

    def test_block_pops_repository_with_dot_python_path(self) -> None:
        make_repository(self.root, load_fixture("config/bp.json"))
        self.assertEqual(load_config(self.root).adapter["python_path"], ["."])

    def test_missing_files_fail_closed(self) -> None:
        make_repository(self.root, self.qs)
        for relative in ("scripts/pages/mod_base_adapter.py", ".github/workflows/mod-compatibility-review.yml",
                         "scripts/pages/mod_base_fixtures.py"):
            with self.subTest(relative=relative):
                (self.root / relative).rename(self.root / "moved")
                with self.assertRaisesRegex(MbError, "does not exist"):
                    load_config(self.root)
                (self.root / "moved").rename(self.root / relative)
        self.assertIsNotNone(load_config(self.root))

    def test_rotation_reads_config_data_only(self) -> None:
        (self.root / "site").mkdir()
        (self.root / DEFAULT_CONFIG_PATH).write_bytes(canonical_json(self.qs))
        with self.assertRaises(MbError):
            load_config(self.root)
        self.assertEqual(load_config(self.root, check_repository_facts=False).canonical_branch, "master")

    def test_symlink_components_are_refused(self) -> None:
        make_repository(self.root, self.qs)
        workflow = self.root / ".github/workflows/on-demand-e2e.yml"
        outside = self.root / "outside.yml"
        workflow.rename(outside)
        os.symlink(outside, workflow)
        with self.assertRaisesRegex(MbError, "crosses a symlink"):
            load_config(self.root)
        workflow.unlink()
        outside.rename(workflow)
        pages = self.root / "scripts/pages"
        real = self.root / "real-pages"
        pages.rename(real)
        os.symlink(real, pages)
        with self.assertRaisesRegex(MbError, "crosses a symlink"):
            load_config(self.root)

    def test_config_symlink_is_refused(self) -> None:
        make_repository(self.root, self.qs)
        config = self.root / DEFAULT_CONFIG_PATH
        config.rename(self.root / "real.json")
        os.symlink(self.root / "real.json", config)
        with self.assertRaisesRegex(MbError, "crosses a symlink"):
            load_config(self.root)
        with self.assertRaisesRegex(MbError, "crosses a symlink"):
            load_config(self.root, config)

    def test_explicit_config_behind_a_symlinked_directory_is_refused(self) -> None:
        make_repository(self.root, self.qs)
        (self.root / "site").rename(self.root / "real-site")
        os.symlink(self.root / "real-site", self.root / "site")
        with self.assertRaisesRegex(MbError, "crosses a symlink"):
            load_config(self.root, self.root / "site" / "mod-base.json")

    def test_python_path_must_be_a_directory(self) -> None:
        make_repository(self.root, self.qs)
        (self.root / "e2e").rmdir()
        (self.root / "e2e").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(MbError, "must be a directory"):
            load_config(self.root)

    def test_icon_rules(self) -> None:
        cases = {
            b"GIF89a" + b"\x00" * 40: "must be a PNG",
            pattern_png(1025, 16): "at most 1024x1024",
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (512 * 1024): "size must be between 1 and",
        }
        for data, message in cases.items():
            with self.subTest(message=message):
                make_repository(self.root, self.qs, icon=data)
                with self.assertRaisesRegex(MbError, message):
                    load_config(self.root)
        make_repository(self.root, self.qs, icon=pattern_png(1024, 1024))
        check_repository(load_config(self.root, check_repository_facts=False), self.root)


if __name__ == "__main__":
    unittest.main()
