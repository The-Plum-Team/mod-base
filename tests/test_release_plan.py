"""The release plan: derived from a tested generation and its Build bundle, sealed and admitted.

``World`` builds what ``release plan`` reads on a real filesystem: the ``mod-base.ci.generation``
of the documents fixture with one runtime lane per config lane, the complete Build bundle it names
(a canonical envelope with production and harness JARs, a native report and one SBOM per target at
``targets/<minecraft>/sbom/``), and a protected checkout holding the version file and the changelog.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.release.config import validate_release_config
from mod_base.release.plan import (ASSETS_DIRECTORY, MAX_RELEASE_ASSETS, PLAN_NAME, admit_release,
                                   derive_release_plan, github_asset_name, plan_digest, read_changelog, read_version,
                                   released_commit, seal_release, validate_release_plan)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RELEASE = FIXTURES / "release"
VERSION = "4.0.0"
CHANGELOG = (
    "# Changelog\n\n"
    f"## {VERSION} (2026-10-31)\n\n"
    "### Added\n\n- A new skin menu.\n- Nicknames <!-- not a ledger -->.\n\n"
    "## 3.1.0 (2026-09-01)\n\n### Fixed\n\n- Older fix.\n"
)
SECTION = "### Added\n\n- A new skin menu.\n- Nicknames <!-- not a ledger -->."


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class World:
    """A generation, its bundle and a checkout for the release config ``config_name``."""

    def __init__(self, root: Path, config_name: str = "config-single-tag.json", *, version: str = VERSION,
                 changelog: str = CHANGELOG, sbom: bool = True) -> None:
        self.root = root
        self.config = load_fixture(f"release/{config_name}")
        self.config["repository"] = "example/mod"
        validate_release_config(self.config)
        self.config_sha256 = hashlib.sha256(canonical_json(self.config)).hexdigest()
        lanes = sorted(self.config["lanes"])
        self.generation = load_fixture("documents/valid/ci-generation.json")
        template = self.generation["lanes"][0]
        self.generation["lanes"] = []
        for index, lane in enumerate(lanes):
            entry = copy.deepcopy(template)
            entry["id"] = lane
            entry["descriptor"]["artifact"]["id"] = 9000 + index
            entry["descriptor"]["artifact"]["name"] = f"mb-ci-runtime--43--a2--{lane}"
            self.generation["lanes"].append(entry)
        identity = self.generation["identity"]
        self.kit = {key: identity["kit"][key] for key in ("sha", "version", "tree_digest")}
        self.bundle = root / "build"
        self.files: dict[str, bytes] = {}
        records = []
        for lane in lanes:
            for role, path in (("production", f"files/Mod - {lane}-{version}.jar"), ("harness", f"harness/{lane}.jar")):
                records.append(self.add(path, f"{role} of {lane}\n".encode() * 3, role, lane))
        records.append(self.add("targets/report.json", b'{"report": true}\n', "native-report", None))
        if sbom:
            for minecraft in sorted({self.config["lanes"][lane]["minecraft"] for lane in lanes}):
                records.append(self.add(f"targets/{minecraft}/sbom/mod.cdx.json", f"sbom {minecraft}\n".encode(),
                                        "sbom", None))
        self.envelope = load_fixture("documents/valid/ci-envelope.json")
        self.envelope["files"] = sorted(records, key=lambda item: item["path"])
        self.envelope["native_reports"] = ["targets/report.json"]
        self.write_bundle()
        self.repo = root / "repo"
        if self.config["version"]["format"] == "json":
            self.write_repo("release/release-matrix.json",
                            json.dumps({"project": {"name": "Mod", "mod_version": version}}).encode())
        else:
            self.write_repo("gradle.properties", f"org.gradle.jvmargs=-Xmx2G\nmod_version={version}\n".encode())
        self.write_repo("CHANGELOG.md", changelog.encode("utf-8"))

    def add(self, path: str, data: bytes, role: str, lane: str | None) -> dict:
        self.files[path] = data
        return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "lane_id": lane,
                "role": role}

    def write_bundle(self) -> None:
        for path, data in self.files.items():
            target = self.bundle / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.bundle / "ci-envelope.json").write_bytes(canonical_json(self.envelope))

    def write_repo(self, relative: str, data: bytes) -> None:
        target = self.repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def derive(self, **changes) -> dict:
        arguments = {"generation": self.generation, "bundle": self.bundle, "config": self.config,
                     "config_sha256": self.config_sha256, "repo": self.repo, "kit": self.kit, **changes}
        return derive_release_plan(**arguments)


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def world(self, *args, **kwargs) -> World:
        return World(self.root, *args, **kwargs)

    def rejects(self, function, *args, message: str | None = None, **kwargs) -> MbError:
        with self.assertRaises(MbError) as caught:
            function(*args, **kwargs)
        if message is not None:
            self.assertIn(message, str(caught.exception))
        return caught.exception


class DeriveTests(Case):
    def test_one_tag_holds_every_lane_and_every_target_sbom(self) -> None:
        world = self.world()
        plan = world.derive()
        self.assertEqual(set(plan), {"kind", "schema_version", "identity", "generation_sha256", "mode",
                                     "config_sha256", "kit", "version", "changelog", "tags", "lanes"})
        self.assertEqual(plan["identity"], world.generation["identity"])
        self.assertEqual(released_commit(plan), world.generation["identity"]["tested_sha"])
        self.assertEqual(plan["generation_sha256"], canonical_sha256(world.generation))
        self.assertEqual((plan["mode"], plan["version"], plan["config_sha256"]), ("reuse", VERSION, world.config_sha256))
        self.assertEqual(plan["changelog"], {"text": SECTION, "sha256": hashlib.sha256(SECTION.encode()).hexdigest()})
        [tag] = plan["tags"]
        self.assertEqual((tag["tag"], tag["name"], tag["minecraft"]), ("v4.0.0", "Block Pops 4.0.0", None))
        self.assertEqual(tag["lanes"], sorted(world.config["lanes"]))
        jars, sboms = tag["assets"][:4], tag["assets"][4:]
        self.assertEqual([asset["lane"] for asset in jars], tag["lanes"])
        for asset in jars:
            data = world.files[asset["path"]]
            self.assertEqual(asset, {"lane": asset["lane"], "path": f"files/Mod - {asset['lane']}-4.0.0.jar",
                                     "filename": f"Mod - {asset['lane']}-4.0.0.jar", "size": len(data),
                                     "sha256": hashlib.sha256(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
                                     "sha512": hashlib.sha512(data).hexdigest()})
        self.assertEqual([(asset["lane"], asset["path"], asset["filename"]) for asset in sboms],
                         [(None, "targets/1.20.1/sbom/mod.cdx.json", "1.20.1-mod.cdx.json"),
                          (None, "targets/1.21.1/sbom/mod.cdx.json", "1.21.1-mod.cdx.json")])
        lane = plan["lanes"]["fabric-1.20.1"]
        self.assertEqual(lane, {"tag": "v4.0.0", "modrinth": {
            "project_id": "u1JjJR7q", "version_number": "4.0.0+fabric-1.20.1",
            "name": "Block Pops 4.0.0 (Fabric 1.20.1)", "game_versions": ["1.20.1"], "loaders": ["fabric", "quilt"],
            "version_type": "release", "dependencies": [{"project_id": "fabric-api", "dependency_type": "required"}]}})
        self.assertEqual(plan["lanes"]["neoforge-1.21.1"]["modrinth"]["version_type"], "beta")
        self.assertIs(validate_release_plan(plan), plan)

    def test_one_tag_per_minecraft_version_holds_its_own_lanes_and_sbom(self) -> None:
        world = self.world("config-quick-skin.json")
        plan = world.derive()
        self.assertEqual(len(plan["tags"]), 17)
        tag = next(item for item in plan["tags"] if item["tag"] == "mc1.21.3-v4.0.0")
        self.assertEqual((tag["name"], tag["minecraft"], tag["lanes"]),
                         ("Quick Skin 4.0.0 for Minecraft 1.21.3", "1.21.3", ["fabric-1.21.3", "neoforge-1.21.3"]))
        self.assertEqual([(asset["lane"], asset["filename"]) for asset in tag["assets"]],
                         [("fabric-1.21.3", "Mod - fabric-1.21.3-4.0.0.jar"),
                          ("neoforge-1.21.3", "Mod - neoforge-1.21.3-4.0.0.jar"), (None, "mod.cdx.json")])
        self.assertEqual(plan["lanes"]["neoforge-1.21.10"]["modrinth"]["version_number"], "mc1.21.10-v4.0.0-neoforge")
        self.assertEqual(plan["lanes"]["fabric-1.21.3"]["curseforge"],
                         {"project_id": 1323980, "display_name": "Quick-Skin-1.21.3-v4.0.0 [Fabric]",
                          "release_type": "release", "game_versions": ["1.21.3"], "loaders": ["fabric"],
                          "relations": [{"slug": "architectury-api", "type": "requiredDependency"},
                                        {"slug": "fabric-api", "type": "requiredDependency"}]})
        self.assertEqual([item["tag"] for item in plan["tags"]], sorted(item["tag"] for item in plan["tags"]))

    def test_a_bundle_without_sboms_publishes_the_jars_alone(self) -> None:
        plan = self.world(sbom=False).derive()
        self.assertEqual([asset["lane"] for asset in plan["tags"][0]["assets"]], plan["tags"][0]["lanes"])

    def test_an_sbom_elsewhere_or_of_another_target_is_not_published(self) -> None:
        world = self.world("config-quick-skin.json", sbom=False)
        records = [world.add("targets/1.21.3/mod.cdx.json", b"elsewhere\n", "sbom", None),
                   world.add("targets/9.9/sbom/mod.cdx.json", b"no tag\n", "sbom", None)]
        world.envelope["files"] = sorted(world.envelope["files"] + records, key=lambda item: item["path"])
        world.write_bundle()
        plan = world.derive()
        self.assertFalse(any(asset["lane"] is None for tag in plan["tags"] for asset in tag["assets"]))

    def test_two_files_of_one_tag_never_share_a_name(self) -> None:
        world = self.world()
        envelope = world.envelope
        jar = next(file for file in envelope["files"] if file["lane_id"] == "forge-1.20.1"
                   and file["role"] == "production")
        del world.files[jar["path"]]
        (world.bundle / jar["path"]).unlink()
        # The prefixed name of the 1.20.1 SBOM in a tag that holds every target.
        envelope["files"] = sorted([file for file in envelope["files"] if file is not jar]
                                   + [world.add("files/1.20.1-mod.cdx.json", b"jar\n", "production", "forge-1.20.1")],
                                   key=lambda item: item["path"])
        world.write_bundle()
        self.rejects(world.derive, message="repeats a file name of the tag")
        world = World(self.root / "spaces")
        names = [file for file in world.envelope["files"] if file["role"] == "production"][:2]
        for file, name in zip(names, ("files/A B.jar", "files/A.B.jar")):
            data = world.files.pop(file["path"])
            (world.bundle / file["path"]).unlink()
            file.update(world.add(name, data, "production", file["lane_id"]))
        world.envelope["files"].sort(key=lambda item: item["path"])
        world.write_bundle()
        self.rejects(world.derive, message="repeats a file name of the tag")

    def test_the_lanes_are_exactly_the_planned_lanes_with_one_production_jar_each(self) -> None:
        world = self.world()
        extra = copy.deepcopy(world.config)
        extra["lanes"]["quilt-1.21.1"] = copy.deepcopy(extra["lanes"]["fabric-1.21.1"])
        extra["lanes"]["quilt-1.21.1"]["loaders"] = ["quilt"]
        self.rejects(world.derive, config=extra, message="does not list exactly the planned lanes")
        missing = copy.deepcopy(world.config)
        del missing["lanes"]["forge-1.20.1"]
        self.rejects(world.derive, config=missing, message="planned lanes")
        generation = copy.deepcopy(world.generation)
        generation["lanes"].pop()
        self.rejects(world.derive, generation=generation, message="one production JAR for every planned lane")

    def test_a_lane_with_two_production_jars_or_none_is_refused(self) -> None:
        world = self.world()
        record = world.add("files/second.jar", b"second\n", "production", "forge-1.20.1")
        world.envelope["files"] = sorted(world.envelope["files"] + [record], key=lambda item: item["path"])
        world.write_bundle()
        self.rejects(world.derive, message="has two production JARs")

    def test_a_production_jar_of_an_unplanned_lane_is_refused(self) -> None:
        world = self.world()
        record = world.add("files/stray.jar", b"stray\n", "production", "stray-lane")
        world.envelope["files"] = sorted(world.envelope["files"] + [record], key=lambda item: item["path"])
        world.write_bundle()
        self.rejects(world.derive, message="no other")

    def test_the_bundle_must_hold_exactly_the_bytes_of_its_envelope(self) -> None:
        world = self.world()
        jar = world.bundle / "files/Mod - forge-1.20.1-4.0.0.jar"
        original = jar.read_bytes()
        jar.write_bytes(bytes([original[0] ^ 1]) + original[1:])
        self.rejects(world.derive, message="differs from its envelope")
        jar.write_bytes(original + b"x")
        self.rejects(world.derive, message="exactly the files of its envelope")
        jar.write_bytes(original)
        (world.bundle / "files/extra.jar").write_bytes(b"extra")
        self.rejects(world.derive, message="exactly the files of its envelope")
        os.unlink(world.bundle / "files/extra.jar")
        world.derive()
        os.unlink(jar)
        os.symlink(world.bundle / "harness/forge-1.20.1.jar", jar)
        self.rejects(world.derive, message="symlink")

    def test_the_envelope_must_be_the_canonical_envelope_of_the_generations_bundle(self) -> None:
        world = self.world()
        envelope_path = world.bundle / "ci-envelope.json"
        envelope_path.write_bytes(json.dumps(world.envelope, indent=1).encode())
        self.rejects(world.derive, message="canonical")
        for change in ({"plan_sha256": "0" * 64}, {"profile": "quick-skin"}):
            changed = {**world.envelope, **change}
            envelope_path.write_bytes(canonical_json(changed))
            self.rejects(world.derive, message="not the Build bundle of the generation")
        changed = copy.deepcopy(world.envelope)
        changed["producer"]["graph_sha256"] = "1" * 64
        envelope_path.write_bytes(canonical_json(changed))
        self.rejects(world.derive, message="producer")
        changed = {**world.envelope, "unknown": 1}
        envelope_path.write_bytes(canonical_json(changed))
        self.rejects(world.derive, message="unknown keys")

    def test_the_kit_and_repository_are_those_of_the_released_commit(self) -> None:
        world = self.world()
        self.rejects(world.derive, kit={**world.kit, "sha": "9" * 40}, message="kit the released commit pins")
        self.rejects(world.derive, kit={**world.kit, "repository": "x/y"}, message="unknown keys")
        config = {**world.config, "repository": "example/other"}
        self.rejects(world.derive, config=config, message="repository of the released commit")
        self.rejects(world.derive, config_sha256="A" * 64)

    def test_the_generation_is_validated_by_the_kits_own_reader(self) -> None:
        world = self.world()
        generation = copy.deepcopy(world.generation)
        generation["lanes"][1]["descriptor"]["artifact"]["id"] = generation["lanes"][0]["descriptor"]["artifact"]["id"]
        self.rejects(world.derive, generation=generation, message="repeats an artifact id")
        generation = {**world.generation, "mode": "partial"}
        self.rejects(world.derive, generation=generation)

    def test_a_derived_plan_is_deterministic(self) -> None:
        world = self.world("config-quick-skin.json")
        self.assertEqual(canonical_json(world.derive()), canonical_json(world.derive()))


class VersionTests(Case):
    def version(self, data: bytes, **changes) -> str:
        repo = self.root / "repo"
        (repo / "gradle.properties").parent.mkdir(parents=True, exist_ok=True)
        (repo / "gradle.properties").write_bytes(data)
        return read_version(repo, {"file": "gradle.properties", "format": "properties", "key": "mod_version",
                                   **changes})

    def test_one_plain_line_defines_the_version(self) -> None:
        self.assertEqual(self.version(b"# c\n! c\n\n  mod_version = 4.0.0-beta.1\nother=1\n"), "4.0.0-beta.1")
        self.assertEqual(self.version(b"a=b\r\nmod_version=1.2\r\n"), "1.2")
        self.assertEqual(self.version(b"long=a\\\n  mod_version=9.9.9\nmod_version=1.0.0\n"), "1.0.0")
        self.assertEqual(self.version(b"# mod_version=9.9 \\\nmod_version=1.0.0\n"), "1.0.0")

    def test_every_ambiguous_definition_is_refused(self) -> None:
        for data in (b"mod_version=1.0.0\nmod_version=1.0.1\n", b"mod_version:1.0.0\n", b"mod_version 1.0.0\n",
                     b"mod_versio\\n=1.0.0\n", b"mod_\\u0076ersion=1.0.0\n", b"mod_version=1.0.\\\n 0\n",
                     b"mod_version=1.0.0\\\n", b"other=1\n", b"mod_version=1.0.0 \n", b"mod_version=v1.0.0\n",
                     b"mod_version=1\n", b"mod_version=1.0.0+a+b\n",
                     b"mod_version=" + b"1." * 40 + b"0\n"):
            with self.subTest(data=data):
                self.rejects(self.version, data)

    def test_a_malformed_escape_in_a_key_is_refused(self) -> None:
        self.rejects(self.version, b"\\u00zz=1\nmod_version=1.0.0\n", message="malformed")

    def test_a_json_pointer_names_a_string(self) -> None:
        repo = self.root / "repo"
        (repo / "release").mkdir(parents=True)

        def read(document, pointer="/project/mod_version"):
            (repo / "release/m.json").write_bytes(document if isinstance(document, bytes) else json.dumps(document).encode())
            return read_version(repo, {"file": "release/m.json", "format": "json", "pointer": pointer})

        self.assertEqual(read({"project": {"mod_version": "2.0.2"}}), "2.0.2")
        self.assertEqual(read({"a/b": {"m~n": ["0.1", "1.2.3"]}}, "/a~1b/m~0n/1"), "1.2.3")
        for document, pointer in (({"project": {}}, "/project/mod_version"), ({"project": {"mod_version": 2}}, None),
                                  ({"project": ["1.0.0"]}, "/project/01"), ({"project": ["1.0.0"]}, "/project/1"),
                                  ({"project": "1.0.0"}, "/project/mod_version"),
                                  (b'{"project":{"mod_version":"1.0.0","mod_version":"1.0.1"}}', None),
                                  (b'{"project":{"mod_version":"1.0.0"}', None)):
            with self.subTest(document=document, pointer=pointer):
                self.rejects(read, document, pointer or "/project/mod_version")

    def test_the_version_file_is_read_through_no_symlink(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        (self.root / "real.properties").write_bytes(b"mod_version=1.0.0\n")
        os.symlink(self.root / "real.properties", repo / "gradle.properties")
        self.rejects(read_version, repo, {"file": "gradle.properties", "format": "properties", "key": "mod_version"},
                     message="symlink")
        os.unlink(repo / "gradle.properties")
        os.symlink(self.root, repo / "dir")
        self.rejects(read_version, repo, {"file": "dir/real.properties", "format": "properties", "key": "mod_version"})


class ChangelogTests(Case):
    def section(self, text: str | bytes, version: str = VERSION) -> str:
        repo = self.root / "repo"
        repo.mkdir(exist_ok=True)
        (repo / "CHANGELOG.md").write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return read_changelog(repo, {"file": "CHANGELOG.md"}, version)

    def test_the_section_of_the_first_heading(self) -> None:
        self.assertEqual(self.section(CHANGELOG), SECTION)
        self.assertEqual(self.section("## 4.0.0 (2026-02-28)\n\n\n  - indented \u00e9\n\n"), "  - indented \u00e9")
        self.assertEqual(self.section("## 4.0.0 (2026-02-28)\nline\n### Sub\n#### x\n"), "line\n### Sub\n#### x")

    def test_a_wrong_missing_or_empty_section_is_refused(self) -> None:
        for text, message in (
                ("## 3.1.0 (2026-09-01)\n- old\n## 4.0.0 (2026-10-31)\n- new\n", "is not version"),
                ("# Changelog\n- nothing\n", "no release heading"),
                ("## 4.0.0\n- new\n", "must read"),
                ("## 4.0.0 (31-10-2026)\n- new\n", "must read"),
                ("## 4.0.0 (2026-02-30)\n- new\n", "real date"),
                ("## 4.0.0 (2026-10-31) \n- new\n", "must read"),
                ("## 4.0.0 (2026-10-31)\n\n   \n## 3.1.0 (2026-09-01)\n- old\n", "empty"),
                ("## 4.0.0 (2026-10-31)\r\n- new\r\n", "carriage return"),
                ("## 4.0.0 (2026-10-31)\n- new <!-- mod-base-release-ledger:v1\n", "ledger marker"),
                ("## 4.0.0 (2026-10-31)\n- bell \x07\n", "control character"),
                ("## 4.0.0 (2026-10-31)\n" + "x" * (64 * 1024 + 1) + "\n", "at most"),
        ):
            with self.subTest(message=message, text=text[:40]):
                self.rejects(self.section, text, message=message)
        self.rejects(self.section, b"\xef\xbb\xbf## 4.0.0 (2026-10-31)\n- new\n", message="byte-order mark")
        self.rejects(self.section, b"## 4.0.0 (2026-10-31)\n- \xff\n", message="UTF-8")

    def test_the_changelog_is_read_through_no_symlink(self) -> None:
        world = self.world()
        real = self.root / "elsewhere.md"
        real.write_text(CHANGELOG, encoding="utf-8")
        os.unlink(world.repo / "CHANGELOG.md")
        os.symlink(real, world.repo / "CHANGELOG.md")
        self.rejects(world.derive, message="symlink")


    def test_a_changelog_file_per_version_is_read_whole(self) -> None:
        world = self.world()
        world.config["changelog"] = {"file": "release/changelogs/{version}.md", "format": "file"}
        validate_release_config(world.config)
        world.write_repo(f"release/changelogs/{VERSION}.md", b"\n# Update - 4.0.0\n## New\n\n- A menu.\n\n")
        self.assertEqual(world.derive()["changelog"]["text"], "# Update - 4.0.0\n## New\n\n- A menu.")
        world.write_repo(f"release/changelogs/{VERSION}.md", b"\n\n")
        self.rejects(world.derive, message="empty")

    def test_a_per_version_path_and_the_file_format_go_together(self) -> None:
        world = self.world()
        for changelog in ({"file": "release/changelogs/{version}.md"},
                          {"file": "CHANGELOG.md", "format": "file"},
                          {"file": "notes/{version}/{version}.md", "format": "file"}):
            with self.subTest(changelog=changelog), self.assertRaises(MbError):
                validate_release_config({**world.config, "changelog": changelog})


class ValidationTests(Case):
    def setUp(self) -> None:
        super().setUp()
        self.world_ = self.world("config-quick-skin.json")
        self.plan = self.world_.derive()

    def changed(self, mutate) -> dict:
        plan = copy.deepcopy(self.plan)
        mutate(plan)
        return plan

    def test_every_rule_that_ties_the_plan_together(self) -> None:
        def tag(plan, name="mc1.21.3-v4.0.0"):
            return next(item for item in plan["tags"] if item["tag"] == name)

        def swap(plan):
            plan["tags"][0], plan["tags"][1] = plan["tags"][1], plan["tags"][0]

        def rename(plan):
            jar = tag(plan)["assets"][1]
            jar["filename"] = "Mod.-.fabric-1.21.3-4.0.0.jar"
            jar["path"] = "files/" + jar["filename"]

        def case_alias(plan):
            jar = tag(plan)["assets"][1]
            jar["filename"] = tag(plan)["assets"][0]["filename"].upper()
            jar["path"] = "other/" + jar["filename"]

        def shared_path(plan):
            other = tag(plan, "mc1.20.1-v4.0.0")["assets"][0]
            tag(plan)["assets"][0].update(path=other["path"], filename=other["filename"])

        def two_tags(plan):
            copied = copy.deepcopy(tag(plan, "mc26.3-v4.0.0"))
            copied["tag"] = "mc26.3-v4.0.0x"
            for asset in copied["assets"]:
                asset["path"] = asset["path"].replace(".jar", "-copy.jar").replace("mod.cdx", "copy.cdx")
                asset["filename"] = asset["path"].rsplit("/", 1)[-1]
            plan["tags"].append(copied)

        cases = {
            "unknown keys": lambda plan: plan.update(extra=1),
            "unknown keys ": lambda plan: tag(plan)["assets"][0].update(role="production"),
            "does not bind": lambda plan: None,
            "sorted by tag": swap,
            "SHA-256 of the changelog": lambda plan: plan["changelog"].update(sha256="0" * 64),
            "in lane order": lambda plan: tag(plan)["assets"].reverse(),
            "names no lane": lambda plan: tag(plan)["assets"][2].update(lane="fabric-1.21.3"),
            "another target": lambda plan: tag(plan, "mc1.20.1-v4.0.0").update(minecraft="1.21.3"),
            "must be '1.21.3-mod.cdx.json'": lambda plan: tag(plan).update(minecraft=None),
            "last component": lambda plan: tag(plan)["assets"][0].update(filename="other.jar"),
            "repeats a file name": rename,
            "repeats a file name ": case_alias,
            "repeats a file of the plan": shared_path,
            "is in two tags": two_tags,
            "not the tag that holds": lambda plan: plan["lanes"]["fabric-1.21.3"].update(tag="mc1.20.1-v4.0.0"),
            "other marketplaces": lambda plan: plan["lanes"]["fabric-1.21.3"].pop("curseforge"),
            "more than one modrinth project": lambda plan: plan["lanes"]["fabric-1.21.3"]["modrinth"].update(
                project_id="OtHeR001"),
            "repeats a Modrinth version number": lambda plan: plan["lanes"]["fabric-1.21.3"]["modrinth"].update(
                version_number=plan["lanes"]["fabric-1.20.1"]["modrinth"]["version_number"]),
            "kit the released commit pins": lambda plan: plan["kit"].update(version="9.9.9"),
            "default branch": lambda plan: plan["identity"].update(head_branch="feature"),
            "Git tag": lambda plan: tag(plan).update(tag="mc1.21.3-v4.0.0.lock"),
            "must be a SHA": lambda plan: tag(plan)["assets"][0].update(sha1="0" * 39),
            "release version": lambda plan: plan.update(version="4"),
            "between 1 and": lambda plan: tag(plan)["assets"][0].update(size=0),
        }
        for message, mutate in cases.items():
            with self.subTest(message=message):
                plan = self.changed(mutate)
                if message == "does not bind":
                    self.assertIs(validate_release_plan(plan), plan)
                    continue
                error = self.rejects(validate_release_plan, plan)
                if message.strip() not in ("must be a SHA", "between 1 and"):
                    self.assertIn(message.strip(), str(error))

    def test_github_renames_spaces_to_periods(self) -> None:
        self.assertEqual(github_asset_name("Quick Skin - Fabric - 1.20.1-4.0.0.jar"),
                         "Quick.Skin.-.Fabric.-.1.20.1-4.0.0.jar")

    def test_the_digest_is_of_the_canonical_bytes(self) -> None:
        self.assertEqual(plan_digest(self.plan), hashlib.sha256(canonical_json(self.plan)).hexdigest())
        self.assertNotIn("plan_sha256", self.plan)
        self.assertGreaterEqual(MAX_RELEASE_ASSETS, 2 * len(self.plan["lanes"]))


class SealTests(Case):
    def sealed(self, config_name: str = "config-single-tag.json") -> tuple[World, dict, Path]:
        world = self.world(config_name)
        plan = world.derive()
        output = self.root / "release"
        self.assertEqual(seal_release(output, plan=plan, bundle=world.bundle), plan)
        return world, plan, output

    def test_a_sealed_release_holds_exactly_the_plan_and_its_assets(self) -> None:
        world, plan, output = self.sealed()
        files = sorted(str(path.relative_to(output)) for path in output.rglob("*") if path.is_file())
        expected = sorted([PLAN_NAME] + [f"{ASSETS_DIRECTORY}/{tag['tag']}/{asset['filename']}"
                                         for tag in plan["tags"] for asset in tag["assets"]])
        self.assertEqual(files, expected)
        self.assertEqual((output / PLAN_NAME).read_bytes(), canonical_json(plan))
        for tag in plan["tags"]:
            for asset in tag["assets"]:
                self.assertEqual((output / ASSETS_DIRECTORY / tag["tag"] / asset["filename"]).read_bytes(),
                                 world.files[asset["path"]])
        self.assertEqual(admit_release(output), plan)
        self.assertEqual([path.name for path in self.root.iterdir() if path.name.startswith(".")], [])

    def test_many_tags_seal_and_admit(self) -> None:
        _, plan, output = self.sealed("config-quick-skin.json")
        self.assertEqual(admit_release(output), plan)

    def test_an_existing_output_or_changed_bundle_publishes_nothing(self) -> None:
        world, plan, output = self.sealed()
        self.rejects(seal_release, output, plan=plan, bundle=world.bundle, message="new path")
        jar = world.bundle / plan["tags"][0]["assets"][0]["path"]
        data = jar.read_bytes()
        jar.write_bytes(bytes([data[0] ^ 1]) + data[1:])
        other = self.root / "other"
        self.rejects(seal_release, other, plan=plan, bundle=world.bundle, message="differs from the plan")
        self.assertFalse(os.path.lexists(other))
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["build", "release", "repo"])
        jar.write_bytes(data)

    def test_a_tampered_sealed_release_is_refused(self) -> None:
        world = self.world()
        plan = world.derive()
        tag = plan["tags"][0]
        source = world.bundle / tag["assets"][0]["path"]
        other_hash = copy.deepcopy(plan)
        other_hash["tags"][0]["assets"][0]["sha1"] = "0" * 40
        tampering = {
            "same size, other bytes": lambda out, jar: jar.write_bytes(bytes([jar.read_bytes()[0] ^ 1])
                                                                      + jar.read_bytes()[1:]),
            "extra file": lambda out, jar: (out / "notes.txt").write_bytes(b"x"),
            "extra asset": lambda out, jar: (jar.parent / "extra.jar").write_bytes(b"x"),
            "extra tag": lambda out, jar: (out / ASSETS_DIRECTORY / "v9.9.9").mkdir(),
            "empty directory": lambda out, jar: (out / "empty").mkdir(),
            "missing asset": lambda out, jar: jar.unlink(),
            "symlinked asset": lambda out, jar: (jar.unlink(), os.symlink(source, jar)),
            "hard-linked asset": lambda out, jar: os.link(jar, self.root / f"{out.name}.jar"),
            "special file": lambda out, jar: os.mkfifo(out / "fifo"),
            "non-canonical plan": lambda out, jar: (out / PLAN_NAME).write_bytes(json.dumps(plan).encode()),
            "other asset hash": lambda out, jar: (out / PLAN_NAME).write_bytes(canonical_json(other_hash)),
            "missing plan": lambda out, jar: (out / PLAN_NAME).unlink(),
        }
        for index, (name, tamper) in enumerate(tampering.items()):
            with self.subTest(name=name):
                output = self.root / f"sealed-{index}"
                seal_release(output, plan=plan, bundle=world.bundle)
                self.assertEqual(admit_release(output), plan)
                tamper(output, output / ASSETS_DIRECTORY / tag["tag"] / tag["assets"][0]["filename"])
                self.rejects(admit_release, output)

    def test_a_consistent_directory_of_another_plan_is_refused_by_digest(self) -> None:
        world, plan, output = self.sealed()
        (output / PLAN_NAME).write_bytes(canonical_json({**plan, "version": "4.0.1"}))
        self.assertEqual(admit_release(output)["version"], "4.0.1")
        self.rejects(admit_release, output, plan_sha256=plan_digest(plan), message="is not the sealed plan")
        (output / PLAN_NAME).write_bytes(canonical_json(plan))
        self.assertEqual(admit_release(output, plan_sha256=plan_digest(plan)), plan)
        self.rejects(admit_release, output, plan_sha256="Z" * 64)

    def test_a_symlinked_release_directory_is_refused(self) -> None:
        _, _, output = self.sealed()
        os.symlink(output, self.root / "link")
        self.rejects(admit_release, self.root / "link")


class PlanCommandTests(Case):
    """``release plan`` as the plan job runs it: the executing kit from its environment (at the
    version the generation fixture names), the generation directory ``ci select-generation
    --with-build`` writes, a sealed output and the job's outputs."""

    def run_plan(self, world: World, **environ: str) -> tuple[dict[str, str], Path]:
        import argparse
        from unittest import mock

        import mod_base
        from mod_base.release import commands

        kit = world.generation["identity"]["kit"]
        (self.root / "generation.json").write_bytes(canonical_json(world.generation))
        config = self.root / "mod-base-release.json"
        config.write_bytes(canonical_json(world.config))
        output, github_output = self.root / "sealed", self.root / "github-output"
        args = argparse.Namespace(repo=world.repo, release_config=config, generation=self.root, output=output,
                                  github_output=github_output)
        environ = {"GITHUB_REPOSITORY": world.config["repository"], "MOD_BASE_KIT_SHA": kit["sha"],
                   "MB_KIT_TREE_DIGEST": kit["tree_digest"],
                   "GITHUB_SHA": world.generation["identity"]["tested_sha"], **environ}
        with mock.patch.object(commands.cli, "environ", return_value=environ), \
                mock.patch.object(mod_base, "__version__", kit["version"]):
            self.assertEqual(commands.run_plan(args), 0)
        lines = github_output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines), output

    def test_the_plan_job_seals_the_plan_of_the_executing_kit(self) -> None:
        world = self.world()
        outputs, sealed = self.run_plan(world)
        plan = admit_release(sealed, plan_sha256=outputs["plan_sha256"])
        identity = world.generation["identity"]["kit"]
        self.assertEqual(plan["kit"], {key: identity[key] for key in ("sha", "version", "tree_digest")})
        self.assertEqual((outputs["version"], outputs["environment"]), (VERSION, world.config["environment"]))
        self.assertEqual(outputs["tags"], " ".join(tag["tag"] for tag in plan["tags"]))

    def test_the_plan_job_refuses_another_kit_or_commit(self) -> None:
        for name, value in (("MOD_BASE_KIT_SHA", "5" * 40), ("MB_KIT_TREE_DIGEST", "sha256:" + "6" * 64),
                            ("GITHUB_SHA", "7" * 40)):
            with self.subTest(name=name):
                self.rejects(self.run_plan, self.world(), **{name: value})


if __name__ == "__main__":
    unittest.main()
