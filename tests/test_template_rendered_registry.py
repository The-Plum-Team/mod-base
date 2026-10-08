"""The closed rendered-caller registry and the manifest admission around it, on real kit trees.

The registry is code. These tests show that nothing else can stand in for it: the template manifest
cannot enrol, remap or re-class a caller; a template the pin parser reads cannot carry a pin
placeholder unless it is enrolled; a record of an unknown renderer is an error in every verb instead
of a byte-identical managed file; and a malformed enrolled template stops every verb before any
write. Each case edits a temporary kit (the real ``template/`` with synthetic callers) or swaps the
registry tuple itself; no reader is replaced.
"""

from __future__ import annotations

import dataclasses
import json
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.build_ci.activation import CALLERS
from mod_base.errors import MbError
from mod_base.template import tool
from tests.test_ci_activation import BUILD, GUARD, PACKAGED, STATUS
from tests.test_template_callers import PAGES, SYNTHETIC_CALLERS, CallerCase, tree
from tests.test_template_tool import KIT_ROOT, SHA, SYNTHETIC_CALLER, config_document, write

TOKENS = ("{{PIN}}", "{{VERSION}}", "{{BRANCH}}")


class RegistryCase(CallerCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_clean_mod()
        self.config = write(self.root, "config.json", json.dumps(config_document()) + "\n")

    def manifest(self) -> dict[str, Any]:
        return json.loads((self.kit / "template/manifest.json").read_text(encoding="utf-8"))

    def write_manifest(self, document: dict[str, Any]) -> None:
        write(self.kit, "template/manifest.json", json.dumps(document, indent=2, sort_keys=True) + "\n")

    def add_entry(self, entry: dict[str, Any], data: str | bytes | None = "plain\n") -> None:
        """Add ``entry`` to the kit's manifest (and its template source, unless ``data`` is ``None``)."""

        document = self.manifest()
        document["files"].append(entry)
        self.write_manifest(document)
        if data is not None:
            write(self.kit, f"template/{entry['source']}", data)

    def verbs(self) -> dict[str, Any]:
        return {
            "load_manifest": lambda: tool.load_manifest(self.kit),
            "check": lambda: tool.check(self.repo, kit_root=self.kit),
            "sync": lambda: tool.sync(self.repo, kit_root=self.kit, write=False),
            "sync --write": lambda: tool.sync(self.repo, kit_root=self.kit, write=True),
            "init": lambda: tool.init(self.repo, kit_root=self.kit, seed=True, from_config=self.config),
        }

    def assert_refused(self, pattern: str) -> None:
        """Every verb refuses the kit and none writes a byte, even where a write is due."""

        (self.repo / "docs/ai/shared/REPOSITORY.md").write_bytes(b"drift a sync would repair\n")
        (self.repo / ".gitattributes").unlink(missing_ok=True)
        before = tree(self.repo)
        for label, verb in self.verbs().items():
            with self.subTest(verb=label), self.assertRaisesRegex(MbError, pattern):
                verb()
        self.assertEqual(tree(self.repo), before)


class ManifestAdmissionTest(RegistryCase):
    def test_the_kit_manifest_and_registry_load(self) -> None:
        self.assertEqual(len(tool.load_manifest(KIT_ROOT)["files"]), 16)
        self.assertEqual(len(tool.load_manifest(self.kit)["files"]), 16)

    def test_an_enrolled_path_or_source_cannot_be_remapped_re_classed_or_case_aliased(self) -> None:
        original = self.manifest()
        index = next(position for position, entry in enumerate(original["files"]) if entry["path"] == PAGES)
        changes = (
            {"source": "managed/other.yml"},
            {"path": ".github/workflows/other.yml"},
            {"path": ".github/workflows/Pages.yml"},
            {"source": "managed/.github/workflows/Pages.yml"},
            {"path": ".GITHUB/workflows/pages.yml", "source": "managed/.GITHUB/workflows/pages.yml"},
            {"class": "seeded", "source": "seed/.github/workflows/pages.yml"},
            {"class": "fragment", "source": "seed/.github/workflows/pages.yml"},
        )
        for change in changes:
            with self.subTest(change=change):
                document = json.loads(json.dumps(original))
                for entry in document["files"]:
                    entry.pop("ignore_actions_of", None)  # it may name managed workflows only
                document["files"][index].update(change)
                self.write_manifest(document)
                write(self.kit, f"template/{document['files'][index]['source']}", SYNTHETIC_CALLER)
                self.assert_refused("differs from the closed registry")
        self.write_manifest(original)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(self.check(), [])

    def test_a_build_caller_can_never_be_a_manifest_entry(self) -> None:
        original = self.manifest()
        for caller in CALLERS:
            entries = {
                "managed": ({"class": "managed", "path": caller, "source": f"managed/{caller}"},
                            "must not be a template manifest entry"),
                "seeded": ({"class": "seeded", "path": caller, "source": f"seed/{caller}"},
                           "differs from the closed registry"),
                "fragment": ({"class": "fragment", "path": caller, "source": f"seed/{caller}", "lines": ["x"]},
                             "differs from the closed registry"),
                "its source under another path": ({"class": "managed", "path": ".github/workflows/renamed.yml",
                                                   "source": f"managed/{caller}"}, "differs from the closed registry"),
                "its path from another source": ({"class": "managed", "path": caller, "source": "managed/renamed.yml"},
                                                 "differs from the closed registry"),
                "a case alias": ({"class": "managed", "path": caller.replace("mod-base", "Mod-Base"),
                                  "source": f"managed/{caller}".replace("mod-base", "Mod-Base")},
                                 "differs from the closed registry"),
            }
            for label, (entry, pattern) in entries.items():
                with self.subTest(caller=caller, entry=label):
                    self.write_manifest(original)
                    self.add_entry(entry, SYNTHETIC_CALLERS[caller])
                    self.assert_refused(pattern)
        self.write_manifest(original)

    def test_a_template_the_pin_parser_reads_holds_a_placeholder_only_when_enrolled(self) -> None:
        original = self.manifest()
        scanned = (".github/workflows/future.yml", ".github/workflows/Future.YAML", ".github/workflows/sub/x.yml",
                   ".github/actions/local/action.yml", ".github/actions/a/b/c/action.yaml",
                   ".GitHub/Actions/local/Action.YML", ".GITHUB/WORKFLOWS/future.yml")
        for path in scanned:
            for klass in ("managed", "fragment", "seeded"):
                for token in TOKENS:
                    entry = {"class": klass, "path": path,
                             "source": ("managed/" if klass == "managed" else "seed/") + path + ".tmpl"}
                    if klass == "fragment":
                        entry["lines"] = ["x"]
                    with self.subTest(path=path, klass=klass, token=token):
                        self.write_manifest(original)
                        self.add_entry(entry, f"jobs:\n  x:\n    uses: The-Plum-Team/mod-base/actions/setup@{token}\n")
                        self.assert_refused("is not an enrolled caller")
        self.write_manifest(original)

    def test_templates_without_placeholders_and_files_the_pin_parser_ignores_are_admitted(self) -> None:
        self.add_entry({"class": "managed", "path": ".github/workflows/plain.yml", "source": "managed/plain.yml"},
                       "name: plain\n")
        self.add_entry({"class": "managed", "path": ".github/actions/local/action.yml",
                        "source": "managed/local-action.yml"}, "name: local\nruns:\n  using: composite\n")
        self.add_entry({"class": "managed", "path": ".github/actions/local/README.md",
                        "source": "managed/local-readme.md"}, "Pinned at {{PIN}} ({{VERSION}}).\n")
        self.add_entry({"class": "seeded", "path": "docs/pin.md", "source": "seed/pin.md.tmpl"}, "{{PIN}} {{VERSION}}\n")
        self.assertEqual(len(tool.load_manifest(self.kit)["files"]), 20)
        written = tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual([drift.path for drift in written],
                         [".github/workflows/plain.yml", ".github/actions/local/action.yml",
                          ".github/actions/local/README.md"])
        self.assertEqual((self.repo / ".github/actions/local/README.md").read_bytes(),
                         b"Pinned at {{PIN}} ({{VERSION}}).\n", "an unenrolled file is never rendered")
        self.assertEqual(self.check(), [])

    def test_a_missing_or_symlinked_template_source_is_refused(self) -> None:
        self.add_entry({"class": "managed", "path": "docs/extra.md", "source": "managed/docs/extra.md"}, None)
        self.assert_refused("template/managed/docs/extra.md is not a regular file")
        outside = write(self.root, "outside.md", "x\n")
        (self.kit / "template/managed/docs/extra.md").symlink_to(outside)
        self.assert_refused("template/managed/docs/extra.md is not a regular file")


class RegistryCoherenceTest(RegistryCase):
    def replaced(self, path: str, **changes: Any) -> tuple[tool.RenderedCaller, ...]:
        return tuple(dataclasses.replace(record, **changes) if record.path == path else record
                     for record in tool.RENDERED_CALLERS)

    def test_a_record_of_an_unknown_renderer_is_an_error_in_every_verb(self) -> None:
        """Before the dispatch was exhaustive such a record was a byte-identical managed file: a
        mod without activation was told its Build caller was missing, and ``sync --write`` wrote
        the unrendered ``build.yml@{{PIN}}`` into it."""

        for state in ("absent", "disabled", "shared-build"):
            self.enter(state)
            for path in (PAGES, BUILD, PACKAGED):
                for renderer in ("build-caller", "", "managed", "Pinned", "pages-extension "):
                    with self.subTest(state=state, path=path, renderer=renderer), \
                            mock.patch.object(tool, "RENDERED_CALLERS", self.replaced(path, renderer=renderer)):
                        self.assert_refused("names the unknown renderer")
                        self.assertEqual(self.callers(), {})

    def test_a_caller_managed_by_mode_is_always_rendered_whole(self) -> None:
        write(self.kit, f"template/managed/{BUILD}", SYNTHETIC_CALLER)
        with mock.patch.object(tool, "RENDERED_CALLERS", self.replaced(BUILD, renderer="pages-extension")):
            self.assert_refused("it can carry no extension region")

    def test_incoherent_enrolment_is_refused(self) -> None:
        build = next(record for record in tool.RENDERED_CALLERS if record.path == BUILD)
        cases = {
            "an unknown mode": self.replaced(BUILD, modes=frozenset({"shared-build", "enabled"})),
            "the reviewed-rollback mode itself": self.replaced(BUILD, modes=frozenset({"reviewed-rollback"})),
            "no mode": self.replaced(BUILD, modes=frozenset()),
            "the same path twice": (*tool.RENDERED_CALLERS, build),
            "a case alias of a path": (*tool.RENDERED_CALLERS,
                                       dataclasses.replace(build, path=BUILD.upper(), source="managed/other.yml")),
            "the same source twice": (*tool.RENDERED_CALLERS,
                                      dataclasses.replace(build, path=".github/workflows/other.yml")),
        }
        patterns = {"an unknown mode": "unknown activation mode", "the reviewed-rollback mode itself":
                    "unknown activation mode", "no mode": "unknown activation mode"}
        for label, registry in cases.items():
            with self.subTest(label), mock.patch.object(tool, "RENDERED_CALLERS", registry):
                self.assert_refused(patterns.get(label, "is enrolled twice"))

    def test_a_malformed_caller_template_stops_every_verb_for_every_mod(self) -> None:
        clean = SYNTHETIC_CALLERS[STATUS]
        outside = write(self.root, "outside.yml", clean)
        template = self.kit / f"template/managed/{STATUS}"
        cases = {
            "no pin placeholder": clean.replace("{{PIN}}", "0" * 40),
            "no version placeholder": clean.replace("{{VERSION}}", "v1.0.0"),
            "CRLF line endings": clean.replace("\n", "\r\n"),
            "a lone carriage return": clean + "# note\r",
            "no final newline": clean.rstrip("\n"),
            "not UTF-8": clean.encode() + b"\xff\n",
            "empty": "",
            "an extension region": clean + "# >>> mod-local extensions: only ext- jobs\n# <<< mod-local extensions\n",
            "an extension end marker": clean + "# <<< mod-local extensions\n",
            "a managed region": "# >>> mod-base managed: status caller\n" + clean + "# <<< mod-base managed\n",
        }
        patterns = {"an extension region": "can hold no region marker", "an extension end marker":
                    "can hold no region marker", "a managed region": "can hold no region marker",
                    "not UTF-8": "is not UTF-8"}
        for state in ("absent", "shared-build"):
            self.enter(state)
            for label, data in cases.items():
                with self.subTest(state=state, template=label):
                    write(self.kit, f"template/managed/{STATUS}", data)
                    self.assert_refused(patterns.get(label, "must hold {{PIN}}/{{VERSION}} and end every line with LF"))
            template.unlink()
            with self.subTest(state=state, template="missing"):
                self.assert_refused("must be a regular file reached without symlinks")
            template.symlink_to(outside)
            with self.subTest(state=state, template="symlink"):
                self.assert_refused("must be a regular file reached without symlinks")
            template.unlink()
            write(self.kit, f"template/managed/{STATUS}", clean)

    def test_a_malformed_pages_template_stops_every_verb_too(self) -> None:
        for label, data in {"no placeholder": SYNTHETIC_CALLER.replace("{{PIN}}", "0" * 40).replace("{{VERSION}}", "v1"),
                            "no extension region": SYNTHETIC_CALLER.split("# >>> mod-local extensions")[0],
                            "a filled extension region": SYNTHETIC_CALLER.replace(
                                "# <<< mod-local extensions\n", "  ext-x:\n    runs-on: x\n# <<< mod-local extensions\n")
                            }.items():
            with self.subTest(label):
                write(self.kit, f"template/managed/{PAGES}", data)
                self.assert_refused("the kit's caller template")

    def test_the_guard_literal_and_every_reference_are_rendered_from_one_pin(self) -> None:
        self.enter("shadow")
        tool.sync(self.repo, kit_root=self.kit, write=True)
        for path in CALLERS:
            data = (self.repo / path).read_bytes()
            self.assertNotIn(b"{{", data)
            self.assertEqual(data, self.rendered(path))
        self.assertIn(f'  MB_KIT_SHA: "{SHA}"\n  MB_KIT_VERSION: "v1.2.3"\n'.encode(), (self.repo / GUARD).read_bytes())


class RegistryShapeTest(unittest.TestCase):
    def test_a_record_names_its_path_source_renderer_and_modes(self) -> None:
        self.assertEqual([field.name for field in dataclasses.fields(tool.RenderedCaller)],
                         ["path", "source", "renderer", "modes"])
        for record in tool.RENDERED_CALLERS:
            with self.subTest(path=record.path):
                self.assertEqual(record.source, f"managed/{record.path}")
                self.assertIn(record.renderer, tool.RENDERERS)
                self.assertTrue((Path(KIT_ROOT) / "template" / record.source).is_file())
                with self.assertRaises(dataclasses.FrozenInstanceError):
                    record.renderer = "pinned"  # type: ignore[misc]
        self.assertIsInstance(tool.RENDERED_CALLERS, tuple)


if __name__ == "__main__":
    unittest.main()
