"""The activation state ``template check|sync|init`` read first, from real files.

A mod with neither an activation manifest nor a Build configuration is legacy. Every other
combination is either a manifest bound to its configuration or an error, so a missing or hostile
manifest can never pass for "no callers to check". Nothing here replaces a reader: the manifest and
the configuration are files in temporary directories, symlinks and FIFOs included.
"""

from __future__ import annotations

import json
import os
import unittest

from mod_base.build_ci.activation import ACTIVATION_PATH, CALLERS, transition_refusal
from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.template import tool
from tests.helpers import ci_activation, ci_config
from tests.test_ci_activation import STATES, manifest
from tests.test_template_callers import CallerCase, tree
from tests.test_template_tool import config_document, write

SHADOW = canonical_json(ci_activation("shadow"))
CONFIG = canonical_json(ci_config())


class ActivationStateTest(CallerCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_clean_mod()

    def load(self) -> object:
        return tool.load_template_activation(self.repo)

    def operations(self) -> dict[str, object]:
        return {
            "load": self.load,
            "bytes": lambda: tool.activation_bytes(self.repo),
            "check": lambda: tool.check(self.repo, kit_root=self.kit),
            "pending": lambda: tool.pending(self.repo, kit_root=self.kit),
            "sync": lambda: tool.sync(self.repo, kit_root=self.kit, write=False),
            "sync --write": lambda: tool.sync(self.repo, kit_root=self.kit, write=True),
            "init": lambda: tool.init(self.repo, kit_root=self.kit, seed=True, from_config=None),
        }

    def assert_refused(self, pattern: str) -> None:
        """Every verb refuses the mod's activation state and none writes a byte."""

        (self.repo / "docs/ai/shared/REPOSITORY.md").write_bytes(b"drift a sync would repair\n")
        (self.repo / ".gitattributes").unlink(missing_ok=True)
        before = tree(self.repo)
        for label, operation in self.operations().items():
            with self.subTest(operation=label), self.assertRaisesRegex(MbError, pattern):
                operation()
        self.assertEqual(tree(self.repo), before)

    def test_a_mod_with_neither_file_is_legacy(self) -> None:
        self.assertIsNone(self.load())
        self.assertIsNone(tool.activation_bytes(self.repo))
        self.assertEqual(self.check(), [])

    def test_every_state_is_read_and_bound_to_the_build_configuration(self) -> None:
        for state in STATES[1:]:
            with self.subTest(state=state):
                self.enter(state)
                self.assertEqual(self.load(), manifest(state))
                self.assertEqual(tool.activation_bytes(self.repo), (self.repo / ACTIVATION_PATH).read_bytes())
        pretty = b'{\n  "schema_version": 1, "kind": "mod-base.ci.activation", "repository": "example/mod",\n' \
                 b'  "profile": "block-pops", "mode": "shadow", "rollback_from": null\n}\n'
        write(self.repo, ACTIVATION_PATH, pretty)
        self.assertEqual(self.load(), ci_activation("shadow"), "a hand-written manifest need not be canonical")
        self.assertEqual(tool.activation_bytes(self.repo), pretty)

    def test_a_build_configuration_without_a_manifest_is_never_legacy(self) -> None:
        outside = write(self.root, "outside.json", CONFIG)
        cases = {
            "valid": lambda: write(self.repo, BUILD_CONFIG_PATH, CONFIG),
            "malformed": lambda: write(self.repo, BUILD_CONFIG_PATH, b"{ not json"),
            "empty": lambda: write(self.repo, BUILD_CONFIG_PATH, b""),
            "symlink": lambda: (self.repo / BUILD_CONFIG_PATH).symlink_to(outside),
            "dangling symlink": lambda: (self.repo / BUILD_CONFIG_PATH).symlink_to(self.root / "nowhere"),
            "directory": lambda: (self.repo / BUILD_CONFIG_PATH).mkdir(),
            "fifo": lambda: os.mkfifo(self.repo / BUILD_CONFIG_PATH),
        }
        (self.repo / "scripts/ci").mkdir(parents=True, exist_ok=True)
        for label, create in cases.items():
            with self.subTest(label):
                create()
                self.assert_refused("exists without site/mod-base-build-activation.json")
                path = self.repo / BUILD_CONFIG_PATH
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()
                tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(self.check(), [])

    def test_deleting_the_manifest_of_an_active_mod_fails_validation(self) -> None:
        self.enter("shared-build")
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(self.check(), [])
        (self.repo / ACTIVATION_PATH).unlink()
        self.assert_refused("exists without site/mod-base-build-activation.json")

    def test_deleting_the_configuration_too_is_what_the_protected_transition_refuses(self) -> None:
        """Locally a mod without both files is indistinguishable from one that never adopted the
        shared Build/E2E, so nothing about its callers is checked. The change that got it there is
        refused where both sides are known: a mode that manages callers never becomes absent."""

        self.enter("shared-build")
        tool.sync(self.repo, kit_root=self.kit, write=True)
        (self.repo / ACTIVATION_PATH).unlink()
        (self.repo / BUILD_CONFIG_PATH).unlink()
        self.assertIsNone(self.load())
        self.assertEqual(self.check(), [])
        self.assertIn("cannot be removed in the shared-build mode", transition_refusal(manifest("shared-build"), None))

    def test_a_manifest_needs_its_valid_build_configuration(self) -> None:
        outside = write(self.root, "outside.json", CONFIG)
        write(self.repo, ACTIVATION_PATH, SHADOW)
        (self.repo / "scripts/ci").mkdir(parents=True, exist_ok=True)
        self.assert_refused("needs scripts/ci/mod-base-build.json as a regular file")
        cases = {
            "symlink": (lambda: (self.repo / BUILD_CONFIG_PATH).symlink_to(outside), "needs scripts/ci/mod-base-build"),
            "directory": (lambda: (self.repo / BUILD_CONFIG_PATH).mkdir(), "needs scripts/ci/mod-base-build"),
            "malformed": (lambda: write(self.repo, BUILD_CONFIG_PATH, b"{ not json"), "not valid JSON"),
            "another kind": (lambda: write(self.repo, BUILD_CONFIG_PATH, SHADOW), "missing required keys"),
            "an unknown key": (lambda: write(self.repo, BUILD_CONFIG_PATH,
                                             canonical_json({**ci_config(), "permissions": {}})), "unknown keys"),
            "another repository": (lambda: write(self.repo, BUILD_CONFIG_PATH,
                                                 canonical_json({**ci_config(), "repository": "example/other"})),
                                   "names another repository or profile"),
            "another profile": (lambda: write(self.repo, BUILD_CONFIG_PATH,
                                              canonical_json({**ci_config(), "profile": "quick-skin"})),
                                "names another repository or profile"),
        }
        for label, (create, pattern) in cases.items():
            with self.subTest(label):
                create()
                self.assert_refused(pattern)
                path = self.repo / BUILD_CONFIG_PATH
                path.rmdir() if path.is_dir() and not path.is_symlink() else path.unlink()

    def test_hostile_manifest_files_are_refused(self) -> None:
        write(self.repo, BUILD_CONFIG_PATH, CONFIG)
        outside = write(self.root, "outside-manifest.json", SHADOW)
        target = self.repo / ACTIVATION_PATH
        cases = {
            "symlink to a valid manifest": (lambda: target.symlink_to(outside), "regular file reached without symlinks"),
            "dangling symlink": (lambda: target.symlink_to(self.root / "nowhere"), "regular file reached without"),
            "directory": (target.mkdir, "regular file reached without symlinks"),
            "fifo": (lambda: os.mkfifo(target), "regular file reached without symlinks"),
            "empty": (lambda: target.write_bytes(b""), "size must be between"),
            "too large": (lambda: target.write_bytes(SHADOW.rstrip(b"\n") + b" " * limits.MAX_CI_ACTIVATION_BYTES),
                          "size must be between"),
            "byte-order mark": (lambda: target.write_bytes(b"\xef\xbb\xbf" + SHADOW), "byte-order mark"),
            "not UTF-8": (lambda: target.write_bytes(SHADOW.replace(b"shadow", b"shad\xffw")), "not valid UTF-8"),
            "duplicate mode": (lambda: target.write_bytes(SHADOW.replace(b'"mode":"shadow"',
                                                                         b'"mode":"disabled","mode":"shadow"')),
                               "duplicate JSON object key"),
            "not JSON": (lambda: target.write_bytes(b"mode: shadow\n"), "not valid JSON"),
            "array": (lambda: target.write_bytes(b"[" + SHADOW.strip() + b"]\n"), "must be an object"),
            "an unknown mode": (lambda: target.write_bytes(canonical_json({**ci_activation(), "mode": "enabled"})),
                                "must be one of"),
            "a caller list": (lambda: target.write_bytes(canonical_json({**ci_activation("shadow"),
                                                                         "callers": list(CALLERS)})), "unknown keys"),
            "a rollback from nowhere": (lambda: target.write_bytes(canonical_json({**ci_activation("shadow"),
                                                                                    "mode": "reviewed-rollback"})),
                                        "must name the mode a reviewed-rollback leaves"),
            "the Build configuration's bytes": (lambda: target.write_bytes(CONFIG), "missing required keys"),
        }
        for label, (create, pattern) in cases.items():
            with self.subTest(label):
                create()
                self.assert_refused(pattern)
                target.rmdir() if target.is_dir() and not target.is_symlink() else target.unlink()

    def test_a_symlinked_site_directory_hides_no_manifest(self) -> None:
        write(self.repo, BUILD_CONFIG_PATH, CONFIG)
        elsewhere = self.root / "elsewhere-site"
        (self.repo / "site").rename(elsewhere)
        write(elsewhere, "mod-base-build-activation.json", SHADOW)
        (self.repo / "site").symlink_to(elsewhere)
        for label, operation in self.operations().items():
            with self.subTest(operation=label), self.assertRaisesRegex(MbError, "regular file reached without symlinks"):
                operation()

    @unittest.skipUnless(os.path.exists(__file__) and not os.path.exists(__file__.upper()),
                         "needs a case-sensitive filesystem")
    def test_a_case_alias_of_the_manifest_is_not_the_manifest(self) -> None:
        write(self.repo, "site/Mod-Base-Build-Activation.json", SHADOW)
        self.assertIsNone(self.load(), "without a Build configuration the alias is an unrelated file")
        write(self.repo, BUILD_CONFIG_PATH, CONFIG)
        self.assert_refused("exists without site/mod-base-build-activation.json")

    def test_the_state_is_read_before_the_template_manifest_and_before_any_write(self) -> None:
        write(self.repo, ACTIVATION_PATH, b"{ not json")
        write(self.repo, BUILD_CONFIG_PATH, CONFIG)
        (self.kit / "template/manifest.json").write_bytes(b"{ not json either")
        self.assert_refused("site/mod-base-build-activation.json is not valid JSON")

    def test_a_repository_that_is_not_a_directory_is_refused(self) -> None:
        for operation in (tool.load_template_activation, tool.activation_bytes, tool.caller_files):
            for path in (self.root / "absent", self.repo / "site/mod-base.json"):
                with self.subTest(operation=operation.__name__, path=path.name), self.assertRaises(MbError):
                    operation(path)

    def test_template_deferred_cannot_name_the_manifest_or_the_configuration(self) -> None:
        write(self.repo, "site/mod-base.json",
              json.dumps(config_document(deferred=[ACTIVATION_PATH, BUILD_CONFIG_PATH]), indent=2) + "\n")
        self.assertEqual(self.kinds(), [(BUILD_CONFIG_PATH, "forbidden"), (ACTIVATION_PATH, "forbidden")])
        write(self.repo, BUILD_CONFIG_PATH, CONFIG)
        with self.assertRaisesRegex(MbError, "exists without"):
            self.check()


class CallerFilesTest(CallerCase):
    def test_caller_files_reads_exactly_the_callers_that_exist(self) -> None:
        self.synced("shared-build")
        self.assertEqual(tool.caller_files(self.repo), self.callers())
        self.assertEqual(len(self.callers()), 3)
        self.remove_callers()
        self.assertEqual(tool.caller_files(self.repo), {})
        write(self.repo, CALLERS[0], b"\xff not text \0")
        self.assertEqual(tool.caller_files(self.repo), {CALLERS[0]: b"\xff not text \0"})
        outside = write(self.root, "outside.yml", b"x\n")
        (self.repo / CALLERS[1]).symlink_to(outside)
        with self.assertRaisesRegex(MbError, "regular file reached without symlinks"):
            tool.caller_files(self.repo)
        (self.repo / CALLERS[1]).unlink()
        write(self.repo, CALLERS[1], b"#" * (tool.MAX_FILE_BYTES + 1))
        with self.assertRaises(MbError):
            tool.caller_files(self.repo)


if __name__ == "__main__":
    unittest.main()
