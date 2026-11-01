"""The release config: its closed schema, its templates and the rehearsal of every rendering."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.release.config import (LANE_PLACEHOLDERS, SAMPLE_VERSION, is_tag, load_release_config, placeholders,
                                     release_layout, render, validate_release_config)

RELEASE = Path(__file__).resolve().parent / "fixtures" / "release"


def fixture(name: str = "config-quick-skin.json") -> dict:
    return json.loads((RELEASE / name).read_text(encoding="utf-8"))


class Case(unittest.TestCase):
    def rejects(self, document: dict, message: str | None = None) -> None:
        with self.assertRaises(MbError) as caught:
            validate_release_config(document)
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def changed(self, mutate, name: str = "config-quick-skin.json") -> dict:
        document = fixture(name)
        mutate(document)
        return document


class FixtureTests(Case):
    def test_both_example_configs_are_valid(self) -> None:
        for name in ("config-quick-skin.json", "config-single-tag.json"):
            with self.subTest(name=name):
                document = fixture(name)
                self.assertIs(validate_release_config(document), document)

    def test_load_returns_the_document_and_the_hash_of_its_exact_bytes(self) -> None:
        path = RELEASE / "config-quick-skin.json"
        document, sha256 = load_release_config(path)
        self.assertEqual(document, fixture())
        self.assertEqual(sha256, hashlib.sha256(path.read_bytes()).hexdigest())

    def test_load_refuses_a_symlink_duplicate_keys_and_an_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.symlink(RELEASE / "config-quick-skin.json", root / "link.json")
            with self.assertRaises(MbError):
                load_release_config(root / "link.json")
            (root / "dup.json").write_bytes(b'{"kind":"mod-base.release.config","kind":"x"}')
            with self.assertRaisesRegex(MbError, "duplicate"):
                load_release_config(root / "dup.json")
            (root / "big.json").write_bytes(b" " * (256 * 1024 + 1))
            with self.assertRaisesRegex(MbError, "size"):
                load_release_config(root / "big.json")
            (root / "nan.json").write_bytes(json.dumps(fixture()).replace('"release"', "NaN", 1).encode())
            with self.assertRaises(MbError):
                load_release_config(root / "nan.json")

    def test_the_quick_skin_layout_is_one_tag_per_minecraft_version(self) -> None:
        tags, lanes = release_layout(fixture(), "4.0.0")
        self.assertEqual(len(tags), 17)
        self.assertEqual(tags["mc1.20.1-v4.0.0"], {"name": "Quick Skin 4.0.0 for Minecraft 1.20.1",
                                                  "minecraft": "1.20.1", "lanes": ["fabric-1.20.1", "forge-1.20.1"]})
        self.assertEqual(lanes["forge-1.20.1"]["modrinth"]["version_number"], "mc1.20.1-v4.0.0-forge")
        self.assertEqual(lanes["forge-1.20.1"]["modrinth"]["name"], "Quick-Skin-1.20.1-v4.0.0 [Forge]")
        self.assertEqual(lanes["fabric-1.21.1"]["modrinth"]["dependencies"],
                         [{"project_id": "architectury-api", "dependency_type": "required"},
                          {"project_id": "fabric-api", "dependency_type": "required"}])


class SchemaTests(Case):
    def test_the_top_level_and_every_object_are_closed(self) -> None:
        for mutate in (lambda d: d.update(extra=1), lambda d: d["version"].update(extra=1),
                       lambda d: d["changelog"].update(extra=1), lambda d: d["tags"].update(extra=1),
                       lambda d: d["modrinth"].update(token="x"), lambda d: d["curseforge"].update(extra=1),
                       lambda d: d["lanes"]["fabric-1.20.1"].update(jar="x.jar")):
            with self.subTest(mutate=mutate):
                self.rejects(self.changed(mutate))
        document = fixture()
        for key in ("kind", "schema_version", "repository", "version", "changelog", "tags", "release_name", "lanes"):
            with self.subTest(missing=key):
                self.rejects({k: v for k, v in document.items() if k != key})

    def test_exact_types_and_grammars(self) -> None:
        cases = [
            lambda d: d.update(schema_version=2), lambda d: d.update(schema_version=True),
            lambda d: d.update(kind="mod-base.release.plan"), lambda d: d.update(repository="../x"),
            lambda d: d["version"].update(file="../gradle.properties"), lambda d: d["version"].update(file=".git/x"),
            lambda d: d["version"].update(key="mod version"), lambda d: d["version"].pop("format"),
            lambda d: d["version"].update(format="toml"), lambda d: d["changelog"].update(file="/CHANGELOG.md"),
            lambda d: d["tags"].update(group="loader"), lambda d: d["curseforge"].update(project_id=True),
            lambda d: d["curseforge"].update(project_id=0), lambda d: d["curseforge"].update(project_id="1323980"),
            lambda d: d["curseforge"].update(project_id=2**31), lambda d: d["modrinth"].update(project_id="a"),
            lambda d: d["modrinth"].update(project_id="has space"),
            lambda d: d.update(lanes={}),
            lambda d: d["lanes"].update({"Fabric-1.20.1": d["lanes"]["fabric-1.20.1"]}),
            lambda d: d["lanes"].update({"fabric--1.20.1": d["lanes"]["fabric-1.20.1"]}),
            lambda d: d["lanes"]["fabric-1.20.1"].update(loaders=[]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(loaders=["fabric", "fabric"]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(loaders=["rift"]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(loaders=["fabric", "quilt", "forge", "neoforge", "fabric"]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(game_versions=[]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(game_versions=["1.20.1", "1.20.1"]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(game_versions=["1.20.1"] + [f"1.{n}" for n in range(64)]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(version_type="snapshot"),
            lambda d: d["lanes"]["fabric-1.20.1"].update(minecraft="1.20 .1"),
            lambda d: d["lanes"]["fabric-1.20.1"].update(modrinth_dependencies=[["fabric-api"]]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(modrinth_dependencies=[["fabric-api", "needed"]]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(modrinth_dependencies=[["fabric-api", "required"],
                                                                                 ["Fabric-API", "optional"]]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(modrinth_dependencies=[["x", "required"]] * 33),
            lambda d: d["lanes"]["fabric-1.20.1"].update(curseforge_relations=[["Fabric-API", "requiredDependency"]]),
            lambda d: d["lanes"]["fabric-1.20.1"].update(curseforge_relations=[["fabric-api", "required"]]),
        ]
        for index, mutate in enumerate(cases):
            with self.subTest(case=index):
                self.rejects(self.changed(mutate))

    def test_a_marketplace_is_required_and_its_dependencies_need_it(self) -> None:
        self.rejects(self.changed(lambda d: (d.pop("modrinth"), d.pop("curseforge"))), "Modrinth, CurseForge or both")
        self.rejects(self.changed(lambda d: d.pop("modrinth")), "does not publish to (modrinth)")
        self.rejects(self.changed(lambda d: d.pop("curseforge")), "does not publish to (curseforge)")

        def curseforge_only(document):
            document.pop("modrinth")
            for lane in document["lanes"].values():
                lane.pop("modrinth_dependencies")

        validate_release_config(self.changed(curseforge_only))

    def test_a_lane_publishes_its_own_minecraft_version_and_never_depends_on_itself(self) -> None:
        self.rejects(self.changed(lambda d: d["lanes"]["fabric-1.20.1"].update(game_versions=["1.20"])),
                     "own Minecraft version")
        self.rejects(self.changed(lambda d: d["lanes"]["fabric-1.20.1"]["modrinth_dependencies"].append(
            ["ZAIE84CH", "embedded"])), "released project itself")

    def test_a_json_pointer_version_source(self) -> None:
        validate_release_config(fixture("config-single-tag.json"))
        for pointer in ("", "/", "project/mod_version", "/a~2b", "/a~", "/" + "x/" * 16 + "y", "/" + "x" * 256):
            with self.subTest(pointer=pointer):
                self.rejects(self.changed(lambda d: d["version"].update(pointer=pointer), "config-single-tag.json"))
        for pointer in ("/project/mod_version", "/a~0b/~1/0", "/ "):
            with self.subTest(pointer=pointer):
                validate_release_config(self.changed(lambda d: d["version"].update(pointer=pointer),
                                                     "config-single-tag.json"))
        self.rejects(self.changed(lambda d: d["version"].update(key="mod_version"), "config-single-tag.json"))
        self.rejects(self.changed(lambda d: d["version"].update(format="properties"), "config-single-tag.json"))


class TemplateTests(Case):
    def test_render_replaces_exactly_the_given_placeholders(self) -> None:
        self.assertEqual(render("mc{minecraft}-v{version}", minecraft="1.20.1", version="4.0.0"), "mc1.20.1-v4.0.0")
        self.assertEqual(render("plain"), "plain")
        self.assertEqual(placeholders("{a}{b_c}x{a}"), ("a", "b_c", "a"))
        for template, values in (("v{version}", {}), ("v{Version}", {"Version": "1"}), ("v{version", {"version": "1"}),
                                 ("v}{version}", {"version": "1"}), ("{{version}}", {"version": "1"}),
                                 ("v{ version }", {"version": "1"}), ("x" * 257, {}), ("a\nb", {}),
                                 (" lead", {}), ("<b>", {}), ("v{version}", {"version": "1\n2"}),
                                 ("{x}", {"x": " "}), ("bidi‮", {}), ("v{version}", {"version": 1})):
            with self.subTest(template=template, values=values), self.assertRaises(MbError):
                render(template, **values)

    def test_tag_placeholders_follow_the_group(self) -> None:
        self.rejects(self.changed(lambda d: d["tags"].update(template="v{version}")), "must name {minecraft}")
        self.rejects(self.changed(lambda d: d["tags"].update(template="mc{minecraft}")), "must name {version}")
        self.rejects(self.changed(lambda d: d["tags"].update(template="mc{minecraft}-v{version}-{loader}")),
                     "unknown placeholder {loader}")
        self.rejects(self.changed(lambda d: d["tags"].update(template="v{version}-{minecraft}"),
                                  "config-single-tag.json"), "unknown placeholder {minecraft}")
        self.rejects(self.changed(lambda d: d.update(release_name="Mod {version} {minecraft}"),
                                  "config-single-tag.json"), "unknown placeholder {minecraft}")
        self.rejects(self.changed(lambda d: d.update(release_name="Mod {tag}")), "unknown placeholder {tag}")

    def test_a_tag_must_be_a_valid_git_tag(self) -> None:
        for template in ("v{version}.", "v{version}.lock", "v..{version}", "-v{version}", "v {version}",
                         "v{version}/x", "v{version}" + "x" * 128, "v{version}~1", "v{version}@{{"):
            with self.subTest(template=template):
                self.rejects(self.changed(lambda d: d["tags"].update(template=template), "config-single-tag.json"))
        self.assertTrue(is_tag("mc1.20.1-v4.0.0+build.1"))
        self.assertFalse(is_tag("v1.0.lock") or is_tag("v1..0") or is_tag("v1.") or is_tag(".v1"))

    def test_marketplace_templates_know_the_lane_placeholders(self) -> None:
        self.assertEqual(LANE_PLACEHOLDERS, ("version", "minecraft", "loader", "loader_name", "lane", "tag"))
        document = self.changed(lambda d: d["modrinth"].update(name="{lane} {loader} {loader_name} {tag}"))
        _, lanes = release_layout(document, "4.0.0")
        self.assertEqual(lanes["neoforge-1.21.1"]["modrinth"]["name"],
                         "neoforge-1.21.1 neoforge NeoForge mc1.21.1-v4.0.0")
        self.rejects(self.changed(lambda d: d["modrinth"].update(name="{target}")), "unknown placeholder {target}")
        self.rejects(self.changed(lambda d: d["curseforge"].update(display_name="{java}")), "unknown placeholder")
        self.rejects(self.changed(lambda d: d["modrinth"].update(version_number="{lane}")), "{version} or {tag}")

    def test_the_first_loader_names_a_lane_with_several(self) -> None:
        _, lanes = release_layout(fixture("config-single-tag.json"), "2.0.2")
        self.assertEqual(lanes["fabric-1.20.1"]["modrinth"]["name"], "Block Pops 2.0.2 (Fabric 1.20.1)")
        self.assertEqual(lanes["fabric-1.20.1"]["modrinth"]["loaders"], ["fabric", "quilt"])

    def test_a_modrinth_version_number_is_at_most_32_characters(self) -> None:
        # Quick Skin's longest lane: mc1.21.10-v1.0.0-neoforge-1.21.10 is 33 characters.
        self.rejects(self.changed(lambda d: d["modrinth"].update(version_number="{tag}-{lane}")), "at most 32")
        _, lanes = release_layout(fixture(), "4.0.0")
        self.assertTrue(all(len(lane["modrinth"]["version_number"]) <= 32 for lane in lanes.values()))
        with self.assertRaisesRegex(MbError, "at most 32"):
            release_layout(fixture(), "4.0.0-rc.1000000")

    def test_names_are_bounded(self) -> None:
        self.rejects(self.changed(lambda d: d["modrinth"].update(name="N" * 60 + " {lane}")), "at most 64")
        self.rejects(self.changed(lambda d: d["curseforge"].update(display_name="N" * 240 + " {lane}")), "at most 256")
        self.rejects(self.changed(lambda d: d["curseforge"].update(display_name="N" * 257)), "between 1 and 256")

    def test_renderings_must_be_unambiguous(self) -> None:
        self.rejects(self.changed(lambda d: d["modrinth"].update(version_number="{tag}")), "two lanes")
        self.rejects(self.changed(lambda d: d["modrinth"].update(version_number="{version}"),
                                  "config-single-tag.json"), "two lanes")
        self.rejects(self.changed(lambda d: d.update(release_name="Quick Skin {version}")), "one name for two tags")

    def test_the_version_is_rehearsed_and_checked(self) -> None:
        self.assertEqual(SAMPLE_VERSION, "1.0.0")
        for version in ("4", "4.0.0.0.0", "v4.0.0", "4.0.0+", "4.0.0 ", "04.0.0-beta+1", "4." * 40 + "0", 4):
            with self.subTest(version=version), self.assertRaises(MbError):
                release_layout(fixture(), version)
        for version in ("4.0", "4.0.0.1", "04.0.0", "4.0.0-beta.1", "4.0.0+b-5"):
            with self.subTest(version=version):
                release_layout(fixture(), version)

    def test_layout_does_not_change_the_config(self) -> None:
        document = fixture()
        before = copy.deepcopy(document)
        tags, lanes = release_layout(document, "4.0.0")
        lanes["fabric-1.20.1"]["modrinth"]["loaders"].append("quilt")
        tags["mc1.20.1-v4.0.0"]["lanes"].append("x")
        self.assertEqual(document, before)


if __name__ == "__main__":
    unittest.main()
