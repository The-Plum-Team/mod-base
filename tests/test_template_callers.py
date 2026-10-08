"""The Build/E2E callers in ``template check|sync|init``: gated by activation mode, rendered whole.

Every case runs the tool over real files in temporary directories. The kit root holds the real
``template/`` with small synthetic caller templates (like ``test_template_tool``'s Pages caller), so
the tool is tested independently of the callers' YAML; the kit's own templates are checked on their
own at the end. The states and the files each manages come from ``test_ci_activation``, which
states them as the architecture does.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli, workflow
from mod_base.build_ci.activation import ACTIVATION_PATH, CALLERS
from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from mod_base.pin import Pin, parse_pin, parse_pin_files
from mod_base.template import tool
from tests.helpers import ci_activation, ci_config
from tests.test_ci_activation import BUILD, GUARD, MANAGED, PACKAGED, STATES, STATUS
from tests.test_template_tool import (KIT_ROOT, OTHER_SHA, QS_EXTENSION, SHA, SYNTHETIC_CALLER, TEMPLATE_ROOT,
                                      TemplateCase, config_document, git, write)
from tests.test_workflow_policy import parse_yaml, require_tools

VERSION = "v1.2.3"
#: ``canonical_branch`` of the mods these cases build (``config_document``).
BRANCH = "master"
PAGES = ".github/workflows/pages.yml"

#: Small caller templates with the pin where the real ones carry it: on every kit reference, and
#: in the guard as the literal it verifies.
SYNTHETIC_CALLERS = {
    GUARD: ("# synthetic guard\nname: guard\non:\n  workflow_call:\nenv:\n"
            '  MB_KIT_SHA: "{{PIN}}"\n  MB_KIT_VERSION: "{{VERSION}}"\n'),
    BUILD: ("# synthetic Build caller\nname: Build\njobs:\n  guard:\n"
            "    uses: ./.github/workflows/mod-base-guard.yml\n  shared:\n    needs: guard\n"
            "    uses: The-Plum-Team/mod-base/.github/workflows/build.yml@{{PIN}} # {{VERSION}}\n"),
    PACKAGED: ("# synthetic packaged E2E caller\nname: Packaged\njobs:\n  select:\n"
               "    uses: The-Plum-Team/mod-base/.github/workflows/select-build.yml@{{PIN}} # {{VERSION}}\n"
               "  shared:\n"
               "    uses: The-Plum-Team/mod-base/.github/workflows/packaged-e2e.yml@{{PIN}} # {{VERSION}}\n"),
    STATUS: ("# synthetic status caller\nname: Status\njobs:\n  evaluate:\n    steps:\n"
             "      - uses: The-Plum-Team/mod-base/actions/setup@{{PIN}} # {{VERSION}}\n"),
}


def row(state: str) -> str:
    """The ``MANAGED`` row of a state: the mode a rollback leaves, else the state itself."""

    return state.rpartition(":")[2]


def render(text: str, sha: str = SHA, version: str = VERSION, branch: str = BRANCH) -> bytes:
    return (text.replace("{{PIN}}", sha).replace("{{VERSION}}", version).replace("{{BRANCH}}", branch)
            .encode("utf-8"))


def tree(root: Path) -> dict[str, bytes]:
    """Every file and symlink below ``root`` (a symlink as its target), keyed by POSIX path."""

    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            found[path.relative_to(root).as_posix()] = b"-> " + os.readlink(path).encode()
        elif path.is_file():
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def enter(repo: Path, state: str) -> None:
    """Give ``repo`` the Build configuration and the activation manifest of ``state`` (a
    ``test_ci_activation.STATES`` label); ``absent`` removes both."""

    for relative in (ACTIVATION_PATH, BUILD_CONFIG_PATH):
        (repo / relative).unlink(missing_ok=True)
    if state != "absent":
        mode, _, left = state.partition(":")
        write(repo, BUILD_CONFIG_PATH, canonical_json(ci_config()))
        write(repo, ACTIVATION_PATH, canonical_json(ci_activation(mode, left or None)))


class CallerCase(TemplateCase):
    def setUp(self) -> None:
        super().setUp()
        for path, text in SYNTHETIC_CALLERS.items():
            write(self.kit, f"template/managed/{path}", text)

    def enter(self, state: str) -> None:
        enter(self.repo, state)

    def rendered(self, path: str, sha: str = SHA, version: str = VERSION) -> bytes:
        return render(SYNTHETIC_CALLERS[path], sha, version)

    def callers(self) -> dict[str, bytes]:
        """The caller files the mod holds."""

        return {path: (self.repo / path).read_bytes() for path in CALLERS if (self.repo / path).is_file()}

    def remove_callers(self) -> None:
        for path in CALLERS:
            (self.repo / path).unlink(missing_ok=True)

    def synced(self, state: str) -> None:
        """A clean mod in ``state`` whose callers ``sync`` wrote."""

        self.make_clean_mod()
        self.enter(state)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(self.check(), [])


# -- What each state manages ------------------------------------------------------------------------


class ModeGatingTest(CallerCase):
    def test_sync_writes_exactly_the_callers_of_each_state(self) -> None:
        self.make_clean_mod()
        for state in STATES:
            with self.subTest(state=state):
                self.remove_callers()
                self.enter(state)
                planned = tool.sync(self.repo, kit_root=self.kit, write=False)
                self.assertEqual([(drift.path, drift.kind) for drift in planned],
                                 [(path, "missing") for path in CALLERS if path in MANAGED[row(state)]])
                self.assertEqual(self.callers(), {}, "a dry run writes nothing")
                written = tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual({drift.path for drift in written}, MANAGED[row(state)])
                self.assertEqual(self.callers(), {path: self.rendered(path) for path in MANAGED[row(state)]})
                self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
                self.assertEqual(self.check(), [])
                for path in MANAGED[row(state)]:
                    self.assertEqual(os.stat(self.repo / path).st_mode & 0o777, 0o644)

    def test_check_reports_each_caller_as_its_state_requires(self) -> None:
        self.make_clean_mod()
        for state in STATES[1:]:
            with self.subTest(state=state):
                self.remove_callers()
                self.enter(state)
                self.assertEqual(self.kinds(), [(path, "missing") for path in CALLERS if path in MANAGED[row(state)]])
                for path in CALLERS:
                    write(self.repo, path, self.rendered(path))
                drifts = self.check()
                self.assertEqual([(drift.path, drift.kind) for drift in drifts],
                                 [(path, "forbidden") for path in CALLERS if path not in MANAGED[row(state)]])
                for drift in drifts:
                    self.assertIn(f"must not exist in the {state.partition(':')[0]} activation mode", drift.detail)
                self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [],
                                 "sync never deletes a caller its mode does not manage")
                self.assertEqual(set(self.callers()), set(CALLERS))

    def test_init_creates_the_callers_of_each_state_and_never_overwrites(self) -> None:
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        for index, state in enumerate(STATES):
            with self.subTest(state=state):
                repo = self.root / f"new-{index}"
                repo.mkdir()
                write(repo, ".github/workflows/e2e.yml",
                      f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # {VERSION}\n")
                enter(repo, state)
                created = tool.init(repo, kit_root=self.kit, seed=False, from_config=config)
                self.assertEqual([path for path in created if path in CALLERS],
                                 [path for path in CALLERS if path in MANAGED[row(state)]])
                self.assertEqual(created.index(".gitattributes"), 0, "the manifest's files come first")
                for path in CALLERS:
                    if path in MANAGED[row(state)]:
                        self.assertEqual((repo / path).read_bytes(), self.rendered(path))
                    else:
                        self.assertFalse((repo / path).exists())
                self.assertEqual(tool.init(repo, kit_root=self.kit, seed=False, from_config=config), [])
        repo = self.root / "kept"
        repo.mkdir()
        write(repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # {VERSION}\n")
        write(repo, BUILD, "ours\n")
        enter(repo, "shadow")
        created = tool.init(repo, kit_root=self.kit, seed=False, from_config=config)
        self.assertNotIn(BUILD, created)
        self.assertEqual((repo / BUILD).read_bytes(), b"ours\n")
        self.assertEqual([path for path in created if path in CALLERS], [GUARD, PACKAGED, STATUS])

    def test_a_new_repository_receives_its_callers_at_the_kit_checkout_pin(self) -> None:
        write(self.kit, "src/mod_base/__init__.py", "")
        git(self.kit, "init", "-q", home=self.home)
        git(self.kit, "add", "-A", home=self.home)
        git(self.kit, "commit", "-q", "-m", "kit", home=self.home)
        git(self.kit, "tag", "v1.4.0", home=self.home)
        head = git(self.kit, "rev-parse", "HEAD", home=self.home)
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        enter(self.repo, "shared-build")
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home)}):
            created = tool.init(self.repo, kit_root=self.kit, seed=False, from_config=config)
        self.assertEqual([path for path in created if path in CALLERS], [GUARD, BUILD, STATUS])
        self.assertEqual(self.callers(), {path: self.rendered(path, head, "v1.4.0") for path in (GUARD, BUILD, STATUS)})
        self.assertEqual((parse_pin(self.repo).sha, parse_pin(self.repo).version), (head, "v1.4.0"))

    def test_a_mod_without_a_manifest_behaves_exactly_as_before_the_callers_existed(self) -> None:
        """The differential: the same operations with the registry of v1.0.3 (the Pages caller
        alone) and with today's give identical reports and identical trees."""

        pages_only = tuple(record for record in tool.RENDERED_CALLERS if record.modes is None)
        self.assertEqual([record.path for record in pages_only], [PAGES])
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        outcomes = []
        for label, registry in (("before", pages_only), ("now", tool.RENDERED_CALLERS)):
            repo = self.root / f"legacy-{label}"
            repo.mkdir()
            self.repo = repo
            with mock.patch.object(tool, "RENDERED_CALLERS", registry):
                self.make_clean_mod()
                write(repo, "docs/ai/shared/REPOSITORY.md", "edited\n")
                (repo / ".gitattributes").unlink()
                report = [
                    tool.evaluate(repo, kit_root=self.kit),
                    tool.sync(repo, kit_root=self.kit, write=False),
                    tool.sync(repo, kit_root=self.kit, write=True),
                    tool.init(repo, kit_root=self.kit, seed=True, from_config=config),
                    tool.evaluate(repo, kit_root=self.kit),
                ]
            outcomes.append((report, tree(repo)))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertFalse(any(path in outcomes[1][1] for path in CALLERS))
        self.assertEqual(outcomes[1][0][-1], ([], []))

    def test_a_mod_without_a_manifest_keeps_whatever_lies_at_a_caller_path(self) -> None:
        self.make_clean_mod()
        write(self.repo, BUILD, "name: the mod's own workflow\n")
        before = tree(self.repo)
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.assertEqual(tool.init(self.repo, kit_root=self.kit, seed=False, from_config=None), [])
        self.assertEqual(tree(self.repo), before)

    def test_disabled_writes_nothing_and_reports_nothing_missing(self) -> None:
        self.make_clean_mod()
        self.enter("disabled")
        before = tree(self.repo)
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.assertEqual(tool.init(self.repo, kit_root=self.kit, seed=False, from_config=None), [])
        self.assertEqual(tree(self.repo), before)

    def test_a_change_of_mode_changes_what_must_exist(self) -> None:
        self.synced("shadow")
        self.enter("reviewed-rollback:shadow")
        self.assertEqual(self.check(), [], "a rollback keeps the callers of the mode it leaves")
        self.enter("disabled")
        self.assertEqual(self.kinds(), [(path, "forbidden") for path in CALLERS])
        self.assertEqual(tool.sync(self.repo, kit_root=self.kit, write=True), [])
        self.remove_callers()
        self.assertEqual(self.check(), [])
        self.enter("shared-build")
        self.assertEqual(self.kinds(), [(path, "missing") for path in (GUARD, BUILD, STATUS)])
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(self.check(), [])
        self.enter("shared-build-and-e2e")
        self.assertEqual(self.kinds(), [(PACKAGED, "missing")])
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.enter("reviewed-rollback:shared-build")
        self.assertEqual(self.kinds(), [(PACKAGED, "forbidden")])


# -- A managed caller is the rendered template, byte for byte ----------------------------------------


class CallerDriftTest(CallerCase):
    def test_any_other_byte_is_drift_and_sync_restores_the_template(self) -> None:
        self.synced("shared-build")
        build = self.repo / BUILD
        clean = build.read_bytes()
        self.assertEqual(clean, self.rendered(BUILD))
        cases = {
            "edited": clean.replace(b"needs: guard", b"needs: other"),
            "appended": clean + b"# a local note\n",
            "no final newline": clean[:-1],
            "empty": b"",
            "NUL byte": clean + b"\0",
            "trailing space": clean.replace(b"name: Build\n", b"name: Build \n"),
        }
        for label, data in cases.items():
            with self.subTest(label):
                build.write_bytes(data)
                drifts = self.check()
                self.assertEqual([(drift.path, drift.kind) for drift in drifts], [(BUILD, "changed")])
                self.assertIn("--- mod-base/template/.github/workflows/mod-base-build.yml", drifts[0].detail)
                planned = tool.sync(self.repo, kit_root=self.kit, write=False)
                self.assertEqual([(drift.path, drift.kind) for drift in planned], [(BUILD, "changed")])
                self.assertEqual(build.read_bytes(), data)
                tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(build.read_bytes(), clean)
                self.assertEqual(self.check(), [])

    def test_bytes_that_break_the_single_pin_are_drift_and_stop_sync(self) -> None:
        self.synced("shared-build")
        build = self.repo / BUILD
        clean = build.read_bytes()
        cases = {
            "another commit": self.rendered(BUILD, OTHER_SHA),
            "another version": self.rendered(BUILD, SHA, "v1.2.4"),
            "the unrendered template": SYNTHETIC_CALLERS[BUILD].encode(),
            "a tag reference": clean.replace(f"@{SHA} # {VERSION}".encode(), b"@v1.2.3"),
            "a second kit checkout": clean + b"          repository: The-Plum-Team/mod-base\n",
            "not UTF-8": clean + b"\xff\n",
        }
        for label, data in cases.items():
            with self.subTest(label):
                build.write_bytes(data)
                drifts = [drift for drift in self.check() if drift.path == BUILD]
                self.assertEqual([drift.kind for drift in drifts], ["changed"])
                self.assertIn("cannot determine the mod's single pin", drifts[0].detail)
                before = tree(self.repo)
                with self.assertRaises(MbError):
                    tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(tree(self.repo), before)
        build.write_bytes(clean)
        self.assertEqual(self.check(), [])

    def test_a_stale_pin_literal_outside_a_uses_line_is_drift_that_sync_renders_again(self) -> None:
        """What a bump leaves between its pin rewrite and its sync: the pin lines moved, the guard's
        literal did not."""

        self.synced("shadow")
        guard = self.repo / GUARD
        write(self.repo, GUARD, self.rendered(GUARD, OTHER_SHA, "v1.2.2"))
        self.assertEqual(parse_pin(self.repo).sha, SHA)
        drifts = self.check()
        self.assertEqual([(drift.path, drift.kind) for drift in drifts], [(GUARD, "changed")])
        self.assertIn(f'-  MB_KIT_SHA: "{SHA}"', drifts[0].detail)
        self.assertIn(f'+  MB_KIT_SHA: "{OTHER_SHA}"', drifts[0].detail)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(guard.read_bytes(), self.rendered(GUARD))

    def test_crlf_line_endings_are_named_and_the_rest_is_diffed_on_the_lf_form(self) -> None:
        self.synced("shared-build")
        build = self.repo / BUILD
        clean = build.read_bytes()
        build.write_bytes(clean.replace(b"\n", b"\r\n"))
        self.assertEqual([(drift.path, drift.kind, drift.detail) for drift in self.check()],
                         [(BUILD, "changed", tool.ACTIVATION_CRLF_ADVICE)])
        self.assertIn(".git/info/attributes", tool.ACTIVATION_CRLF_ADVICE)
        build.write_bytes(clean.replace(b"\n", b"\r\n") + b"extra\r\n")
        advice, drift = self.check()
        self.assertEqual(advice.detail, tool.ACTIVATION_CRLF_ADVICE)
        self.assertIn("+extra", drift.detail)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(build.read_bytes(), clean)

    def test_a_caller_has_no_extension_region(self) -> None:
        self.synced("shared-build")
        build = self.repo / BUILD
        clean = build.read_bytes()
        extension = ("# >>> mod-local extensions: only jobs whose id starts with \"ext-\"\n"
                     + QS_EXTENSION + "# <<< mod-local extensions\n").encode()
        for label, data in {"an ext- job": clean + QS_EXTENSION.encode(), "a whole extension region": clean + extension,
                            "a managed region": b"# >>> mod-base managed: x\n" + clean + b"# <<< mod-base managed\n"
                                                + extension}.items():
            with self.subTest(label):
                build.write_bytes(data)
                self.assertEqual(self.kinds(), [(BUILD, "changed")], "never an 'extension' drift: nothing is the mod's")
                tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(build.read_bytes(), clean, "sync keeps nothing of the mod's file")

    def test_template_deferred_cannot_name_a_caller(self) -> None:
        deferred = [BUILD, GUARD, ".gitattributes"]
        self.make_clean_mod(config_document(deferred=deferred))
        self.enter("shared-build")
        self.assertEqual(self.kinds(), [(BUILD, "forbidden"), (GUARD, "forbidden"), (GUARD, "missing"),
                                        (BUILD, "missing"), (STATUS, "missing")])
        self.assertEqual(tool.pending(self.repo, kit_root=self.kit), [])
        written = tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual([drift.path for drift in written], [GUARD, BUILD, STATUS], "a deferred caller is written anyway")
        build = self.repo / BUILD
        build.write_bytes(build.read_bytes() + b"# drift\n")
        self.assertEqual(self.kinds(), [(BUILD, "forbidden"), (GUARD, "forbidden"), (BUILD, "changed")])
        self.assertFalse(set(CALLERS) & tool.DEFERRABLE)

    def test_a_caller_that_is_not_a_plain_file_is_drift_and_is_never_written_through(self) -> None:
        self.synced("shared-build")
        build = self.repo / BUILD
        clean = build.read_bytes()
        outside = write(self.root, "outside.yml", clean)

        def link() -> None:
            build.symlink_to(outside)

        for label, replace in {"symlink": link, "directory": build.mkdir, "fifo": lambda: os.mkfifo(build)}.items():
            with self.subTest(label):
                build.unlink()
                replace()
                self.assertIn((BUILD, "changed"), self.kinds())
                with self.assertRaises(MbError):
                    tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(outside.read_bytes(), clean)
                if build.is_dir() and not build.is_symlink():
                    build.rmdir()
                else:
                    build.unlink()
                build.write_bytes(clean)
        self.assertEqual(self.check(), [])
        elsewhere = self.root / "elsewhere"
        shutil.copytree(self.repo / ".github/workflows", elsewhere)
        shutil.rmtree(self.repo / ".github/workflows")
        (self.repo / ".github/workflows").symlink_to(elsewhere)
        (elsewhere / "mod-base-guard.yml").write_bytes(b"edited behind the link\n")
        linked = tree(elsewhere)
        kinds = self.kinds()
        for path in (PAGES, GUARD, BUILD, STATUS):
            self.assertIn((path, "changed"), kinds)
        with self.assertRaises(MbError):
            tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(tree(elsewhere), linked, "a symlinked workflow directory is never written through")

    def test_an_oversized_caller_is_refused_before_it_is_read(self) -> None:
        self.synced("shared-build")
        write(self.repo, STATUS, self.rendered(STATUS) + b"#" * tool.MAX_FILE_BYTES)
        with self.assertRaises(MbError):
            self.check()
        with self.assertRaises(MbError):
            tool.sync(self.repo, kit_root=self.kit, write=True)


# -- Fragments ---------------------------------------------------------------------------------------


class ActivationFragmentTest(CallerCase):
    def test_dependabot_ignores_the_actions_of_a_caller_only_while_its_mode_manages_it(self) -> None:
        write(self.kit, f"template/managed/{STATUS}", SYNTHETIC_CALLERS[STATUS]
              + f"      - uses: actions/checkout@{OTHER_SHA} # v7.0.1\n")
        write(self.kit, f"template/managed/{PACKAGED}", SYNTHETIC_CALLERS[PACKAGED]
              + f"    steps:\n      - uses: github/codeql-action/init@{OTHER_SHA} # v4.0.0\n")
        self.make_clean_mod()
        dependabot = self.repo / ".github/dependabot.yml"
        seeded = dependabot.read_text(encoding="utf-8")
        required = {
            "absent": [], "disabled": [],
            "shadow": [("actions/checkout", STATUS), ("github/codeql-action", PACKAGED)],
            "shared-build": [("actions/checkout", STATUS)],
            "shared-build-and-e2e": [("actions/checkout", STATUS), ("github/codeql-action", PACKAGED)],
        }
        for state in STATES:
            with self.subTest(state=state):
                self.remove_callers()
                self.enter(state)
                tool.sync(self.repo, kit_root=self.kit, write=True)
                dependabot.write_text(seeded, encoding="utf-8", newline="\n")
                drifts = self.check()
                self.assertEqual([(drift.path, drift.kind) for drift in drifts],
                                 [(".github/dependabot.yml", "fragment")] * len(required[row(state)]))
                for drift, (name, caller) in zip(drifts, required[row(state)]):
                    self.assertIn(f"must ignore {name}, which the managed region of {caller} pins", drift.detail)
                ignores = "".join(f"      - dependency-name: {name}\n" for name, _caller in required[row(state)])
                dependabot.write_text(seeded.replace("    groups:\n", ignores + "    groups:\n"), encoding="utf-8",
                                      newline="\n")
                self.assertEqual(self.check(), [])

    def test_an_absent_deferred_dependabot_file_needs_no_ignore(self) -> None:
        write(self.kit, f"template/managed/{STATUS}", SYNTHETIC_CALLERS[STATUS]
              + f"      - uses: actions/checkout@{OTHER_SHA} # v7.0.1\n")
        self.make_clean_mod(config_document(deferred=[".github/dependabot.yml"]))
        (self.repo / ".github/dependabot.yml").unlink()
        self.enter("shared-build")
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual(tool.evaluate(self.repo, kit_root=self.kit), ([], []))
        write(self.repo, ".github/dependabot.yml", self.seed(".github/dependabot.yml.tmpl"))
        self.assertEqual(self.kinds(), [(".github/dependabot.yml", "fragment")], "present, the rule is strict")

    def test_the_required_owners_cover_every_activation_control_path(self) -> None:
        """No new CODEOWNERS marker is needed: the manifest already requires an owner for every
        directory that holds the activation manifest, the Build configuration or a caller."""

        manifest = tool.load_manifest(KIT_ROOT)
        markers = next(entry for entry in manifest["files"] if entry["path"] == tool.CODEOWNERS_PATH)["markers"]
        for path in (ACTIVATION_PATH, BUILD_CONFIG_PATH, *CALLERS, *ci_config()["adapter"].values()):
            if isinstance(path, str):
                with self.subTest(path=path):
                    self.assertTrue(any(f"/{path}".startswith(marker) and marker.endswith("/") for marker in markers),
                                    f"{path} lies below no required CODEOWNERS pattern")


# -- Guards against a template that would break a mod ------------------------------------------------


class SinglePinGuardTest(CallerCase):
    def test_sync_refuses_a_caller_template_whose_reference_is_not_a_pin_line(self) -> None:
        self.make_clean_mod()
        self.enter("shared-build")
        broken = {
            "no version comment": SYNTHETIC_CALLERS[BUILD].replace(" # {{VERSION}}", "") + "# {{VERSION}}\n",
            "a second kit checkout": SYNTHETIC_CALLERS[BUILD] + "      repository: The-Plum-Team/mod-base # {{PIN}}\n",
            "a quoted reference": SYNTHETIC_CALLERS[BUILD].replace(
                "uses: The-Plum-Team/mod-base/.github/workflows/build.yml@{{PIN}}",
                'uses: "The-Plum-Team/mod-base/.github/workflows/build.yml@{{PIN}}"'),
        }
        for label, text in broken.items():
            with self.subTest(label):
                write(self.kit, f"template/managed/{BUILD}", text)
                before = tree(self.repo)
                for write_files in (False, True):
                    with self.assertRaisesRegex(MbError, "would no longer carry one pin"):
                        tool.sync(self.repo, kit_root=self.kit, write=write_files)
                self.assertEqual(tree(self.repo), before, "nothing is written, not even the callers planned first")

    def test_init_refuses_it_too(self) -> None:
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        write(self.repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # {VERSION}\n")
        enter(self.repo, "shared-build")
        write(self.kit, f"template/managed/{BUILD}", SYNTHETIC_CALLERS[BUILD].replace(" # {{VERSION}}", "")
              + "# {{VERSION}}\n")
        before = tree(self.repo)
        with self.assertRaisesRegex(MbError, "would no longer carry one pin"):
            tool.init(self.repo, kit_root=self.kit, seed=True, from_config=config)
        self.assertEqual(tree(self.repo), before)


# -- The canonical branch a caller is rendered for ---------------------------------------------------

#: The synthetic Build caller with the branch filter the real one carries: a ``push`` trigger takes
#: its branch as a literal, so the template names the mod's canonical branch with a placeholder.
BRANCHED = SYNTHETIC_CALLERS[BUILD].replace("jobs:\n", 'on:\n  push:\n    branches: ["{{BRANCH}}"]\njobs:\n')


class CanonicalBranchTest(CallerCase):
    """``{{BRANCH}}``: the third and last placeholder of a caller that is rendered whole."""

    def setUp(self) -> None:
        super().setUp()
        write(self.kit, f"template/managed/{BUILD}", BRANCHED)

    def verbs(self) -> list[Any]:
        return [lambda: tool.load_manifest(self.kit), lambda: tool.check(self.repo, kit_root=self.kit),
                lambda: tool.sync(self.repo, kit_root=self.kit, write=True)]

    def test_sync_renders_the_canonical_branch_and_any_other_filter_is_drift(self) -> None:
        self.assertIsNone(tool.canonical_branch(self.repo), "a repository without a configuration names none")
        self.synced("shared-build")
        self.assertEqual(tool.canonical_branch(self.repo), BRANCH)
        build = self.repo / BUILD
        clean = build.read_bytes()
        self.assertEqual(clean, render(BRANCHED))
        self.assertIn(b'    branches: ["master"]\n', clean)
        others = {"another branch": b'["main"]', "a wider filter": b'["master", "release/**"]',
                  "every branch": b'["**"]', "the placeholder": b'["{{BRANCH}}"]'}
        for label, other in others.items():
            with self.subTest(label):
                build.write_bytes(clean.replace(b'["master"]', other))
                self.assertEqual(self.kinds(), [(BUILD, "changed")])
                tool.sync(self.repo, kit_root=self.kit, write=True)
                self.assertEqual(build.read_bytes(), clean)

    def test_a_change_of_the_canonical_branch_is_drift_until_the_callers_are_synced(self) -> None:
        self.synced("shared-build")
        config = config_document()
        config["canonical_branch"] = "release/1.21"
        write(self.repo, "site/mod-base.json", json.dumps(config, indent=2) + "\n")
        drifts = self.check()
        self.assertEqual([(drift.path, drift.kind) for drift in drifts], [(BUILD, "changed")])
        self.assertIn('-    branches: ["release/1.21"]', drifts[0].detail)
        self.assertIn('+    branches: ["master"]', drifts[0].detail)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual((self.repo / BUILD).read_bytes(), render(BRANCHED, branch="release/1.21"))
        self.assertEqual(self.check(), [])

    def test_a_caller_is_rendered_only_for_a_valid_branch_and_needs_one_where_its_template_names_it(self) -> None:
        pin = Pin(SHA, VERSION, ())
        shared = ci_activation("shared-build")
        self.assertEqual(tool.expected_callers(self.kit, pin, shared, "main")[BUILD], render(BRANCHED, branch="main"))
        with self.assertRaisesRegex(MbError, "its template names the mod's canonical branch"):
            tool.expected_callers(self.kit, pin, shared)
        self.assertEqual(tool.expected_callers(self.kit, pin, ci_activation("disabled")), dict.fromkeys(CALLERS),
                         "a caller no mode manages is not rendered, so it needs no branch")
        hostile = ("", "a b", "x\ny", "/x", "a..b", "a//b", '"]\n  pull_request: {}', "{{BRANCH}}", "{{PIN}}", "*",
                   "!master", "b" * 201)
        for branch in hostile:
            with self.subTest(branch=branch), self.assertRaisesRegex(MbError, "is not a valid branch name"):
                tool.expected_callers(self.kit, pin, shared, branch)
        write(self.kit, f"template/managed/{BUILD}", SYNTHETIC_CALLERS[BUILD])
        self.assertEqual(tool.expected_callers(self.kit, pin, shared)[BUILD], render(SYNTHETIC_CALLERS[BUILD]))
        with self.assertRaisesRegex(MbError, "is not a valid branch name"):
            tool.expected_callers(self.kit, pin, shared, "a b")

    def test_the_placeholders_are_a_closed_set(self) -> None:
        self.assertEqual(tool.PINNED_PLACEHOLDERS, ("{{PIN}}", "{{VERSION}}", "{{BRANCH}}"))
        self.assertEqual(tool.PAGES_PLACEHOLDERS, ("{{PIN}}", "{{VERSION}}"))
        self.synced("shared-build")
        before = tree(self.repo)
        unknown = {"another placeholder": BRANCHED + "# {{OWNER}}\n",
                   "a seed placeholder in capitals": BRANCHED.replace("{{BRANCH}}", "{{CANONICAL_BRANCH}}"),
                   "a numbered placeholder": BRANCHED + "# {{PIN2}}\n"}
        for label, text in unknown.items():
            write(self.kit, f"template/managed/{BUILD}", text)
            for verb in self.verbs():
                with self.subTest(label), self.assertRaisesRegex(MbError, "which its renderer does not fill in"):
                    verb()
        write(self.kit, f"template/managed/{BUILD}", BRANCHED)
        write(self.kit, f"template/managed/{PAGES}", SYNTHETIC_CALLER.replace("# <<< mod-base managed\n",
                                                                             "# {{BRANCH}}\n# <<< mod-base managed\n"))
        for verb in self.verbs():
            with self.assertRaisesRegex(MbError, r"pages\.yml holds \{\{BRANCH\}\}, which its renderer does not fill in"):
                verb()
        self.assertEqual(tree(self.repo), before, "no verb wrote a byte")
        write(self.kit, f"template/managed/{PAGES}", SYNTHETIC_CALLER)
        expression = BRANCHED + "# ${{ github.sha }} ${{INPUT}} {{ lower }} { {X} }\n"
        write(self.kit, f"template/managed/{BUILD}", expression)
        tool.sync(self.repo, kit_root=self.kit, write=True)
        self.assertEqual((self.repo / BUILD).read_bytes(), render(expression), "an expression is no placeholder")

    def test_init_renders_the_branch_of_the_configuration_it_is_given(self) -> None:
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        write(self.repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # {VERSION}\n")
        enter(self.repo, "shared-build")
        before = tree(self.repo)
        with self.assertRaisesRegex(MbError, "its template names the mod's canonical branch"):
            tool.init(self.repo, kit_root=self.kit, seed=True, from_config=None)
        self.assertEqual(tree(self.repo), before, "nothing is seeded when a caller cannot be rendered")
        created = tool.init(self.repo, kit_root=self.kit, seed=True, from_config=config)
        self.assertIn(BUILD, created)
        self.assertEqual((self.repo / BUILD).read_bytes(), render(BRANCHED))


# -- The command line --------------------------------------------------------------------------------


class CallerCommandsTest(CallerCase):
    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("mod_base.runtime.kit_root", return_value=self.kit), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_check_and_sync_exit_codes_in_an_active_mode(self) -> None:
        self.make_clean_mod()
        self.enter("shared-build")
        code, stdout, stderr = self.run_cli("template", "check", "--repo", str(self.repo))
        self.assertEqual(code, 2)
        self.assertEqual([line for line in stdout.splitlines() if line.startswith("missing: ")],
                         [f"missing: {path}" for path in (GUARD, BUILD, STATUS)])
        self.assertEqual(stderr.count("\n"), 1)
        self.assertEqual(self.run_cli("template", "sync", "--repo", str(self.repo))[0], 2)
        self.assertEqual(self.callers(), {})
        code, stdout, _ = self.run_cli("template", "sync", "--repo", str(self.repo), "--write")
        self.assertEqual((code, stdout), (0, "".join(f"wrote {path}\n" for path in (GUARD, BUILD, STATUS))))
        self.assertEqual(self.run_cli("template", "check", "--repo", str(self.repo)), (0, "", ""))
        write(self.repo, PACKAGED, self.rendered(PACKAGED))
        code, stdout, _ = self.run_cli("template", "check", "--repo", str(self.repo))
        self.assertEqual(code, 2)
        self.assertIn(f"forbidden: {PACKAGED}\n", stdout)

    def test_init_lists_the_callers_it_created(self) -> None:
        config = write(self.root, "config.json", json.dumps(config_document()) + "\n")
        write(self.repo, ".github/workflows/e2e.yml", f"      - uses: The-Plum-Team/mod-base/actions/setup@{SHA} # {VERSION}\n")
        enter(self.repo, "shadow")
        code, stdout, _ = self.run_cli("template", "init", "--repo", str(self.repo), "--from-config", str(config))
        self.assertEqual(code, 0)
        self.assertEqual([line for line in stdout.splitlines() if "mod-base-" in line],
                         [f"created {path}" for path in CALLERS])


# -- The kit's own caller templates ------------------------------------------------------------------


def real_callers(sha: str = SHA, version: str = VERSION, branch: str = BRANCH) -> dict[str, bytes]:
    """The kit's four caller templates rendered for a pin and a canonical branch."""

    return {path: render((TEMPLATE_ROOT / "managed" / path).read_text(encoding="utf-8"), sha, version, branch)
            for path in CALLERS}


def real_mod(repo: Path, sha: str = SHA, version: str = VERSION, kit_root: Path = KIT_ROOT) -> Path:
    """A mod pinned to ``sha``/``version`` that ``template check`` of the kit at ``kit_root``
    (this kit by default) finds clean: synced managed files, seeded fragments, no activation."""

    write(repo, "site/mod-base.json", json.dumps(config_document(), indent=2) + "\n")
    write(repo, ".github/workflows/e2e.yml",
          f"jobs:\n  evidence:\n    steps:\n      - uses: The-Plum-Team/mod-base/actions/prepare-evidence@{sha} # {version}\n")
    tool.sync(repo, kit_root=kit_root, write=True)
    seeds = Path(kit_root) / "template" / "seed"
    for name, target in ((".gitignore.base", ".gitignore"), (".github/dependabot.yml.tmpl", ".github/dependabot.yml"),
                         (".github/pull_request_template.md.tmpl", ".github/pull_request_template.md")):
        write(repo, target, (seeds / name).read_bytes())
    write(repo, ".github/CODEOWNERS",
          (seeds / ".github/CODEOWNERS.tmpl").read_text(encoding="utf-8").replace("{{owner}}", "AkaNebur"))
    write(repo, "AGENTS.md", "@docs/ai/shared/REPOSITORY.md\n@docs/ai/shared/PUBLIC-EVIDENCE.md\n@docs/ai/PROJECT.md\n")
    write(repo, "docs/ai/PROJECT.md", "# Project\n")
    return repo


class KitCallerTemplatesTest(unittest.TestCase):
    def test_the_registry_enrols_the_pages_caller_and_the_four_callers_by_mode(self) -> None:
        records = {record.path: record for record in tool.RENDERED_CALLERS}
        self.assertEqual(list(records), [PAGES, *CALLERS])
        self.assertEqual((records[PAGES].renderer, records[PAGES].modes), ("pages-extension", None))
        for path in CALLERS:
            with self.subTest(path=path):
                self.assertEqual((records[path].source, records[path].renderer), (f"managed/{path}", "pinned"))
                self.assertEqual(records[path].modes, {state for state in MANAGED if path in MANAGED[state]})
        self.assertEqual(tool.RENDERERS, ("pages-extension", "pinned"))
        manifest = tool.load_manifest(KIT_ROOT)
        self.assertFalse({entry["path"] for entry in manifest["files"]} & set(CALLERS),
                         "a Build/E2E caller is never a manifest entry")
        self.assertFalse({entry["source"] for entry in manifest["files"]} & {f"managed/{path}" for path in CALLERS})

    def test_every_template_renders_to_the_single_pin_with_no_placeholder_left(self) -> None:
        expected = tool.expected_callers(KIT_ROOT, Pin(SHA, VERSION, ()), ci_activation("shadow"), BRANCH)
        self.assertEqual(expected, real_callers())
        for path, data in expected.items():
            with self.subTest(path=path):
                text = data.decode("utf-8")
                self.assertNotIn("{{", text.replace("${{", ""), "no placeholder is left")
                self.assertNotIn("\r", text)
                self.assertTrue(text.endswith("\n"))
                self.assertIn(SHA, text)
                self.assertRegex(text.splitlines()[0],
                                 r"^# mod-base managed: .+, edit only in The-Plum-Team/mod-base template/managed/"
                                 + path.replace(".", r"\.") + "$")
                self.assertNotIn("PROVISIONAL", text, "no caller is a stand-in any more")
                self.assertEqual(f'    branches: ["{BRANCH}"]' in text.splitlines(), path in (BUILD, PACKAGED),
                                 "the Build and the packaged E2E caller run on a push to the canonical branch")
        found = parse_pin_files(expected)
        self.assertEqual((found.sha, found.version), (SHA, VERSION))
        self.assertEqual({reference.split("@")[0] for reference in found.references}, {BUILD, PACKAGED, STATUS})
        self.assertEqual(len(found.references), 5)

    def test_expected_callers_follows_the_activation_state(self) -> None:
        pin = Pin(SHA, VERSION, ())
        for state in STATES:
            mode, _, left = state.partition(":")
            document = None if state == "absent" else ci_activation(mode, left or None)
            expected = tool.expected_callers(KIT_ROOT, pin, document, BRANCH)
            with self.subTest(state=state):
                self.assertEqual(list(expected), list(CALLERS))
                self.assertEqual({path for path, data in expected.items() if data is not None}, MANAGED[row(state)])
                for path, data in expected.items():
                    self.assertIn(data, (None, real_callers()[path]))

    def test_the_callers_have_the_jobs_and_references_of_the_architecture(self) -> None:
        """The shape the other cases of this module rely on. ``test_managed_ci_callers`` holds the
        policy of the guard, the Build caller and the packaged E2E caller, and
        ``test_managed_status_caller`` that of the gate status caller."""

        documents = {path: parse_yaml(data.decode("utf-8"), path) for path, data in real_callers().items()}
        for path, document in documents.items():
            with self.subTest(path=path):
                self.assertEqual(document["permissions"], {})
                # One job of one caller names one secret: the status caller's publishing job.
                self.assertEqual(json.dumps(document).count("secrets"), int(path == STATUS))
        events = ["pull_request_target", "push", "workflow_dispatch"]
        self.assertEqual({path: document["on"] if isinstance(document["on"], str) else list(document["on"])
                          for path, document in documents.items()},
                         {GUARD: ["workflow_call"], BUILD: events, PACKAGED: events,
                          STATUS: ["workflow_run", "pull_request_target", "schedule", "workflow_dispatch"]},
                         "the status caller runs whenever the answer for a pull request may have changed")
        guard = documents[GUARD]
        self.assertEqual(list(guard["on"]["workflow_call"]["outputs"]), ["kit-sha"])
        self.assertEqual(guard["env"], {"MB_KIT_SHA": SHA, "MB_KIT_VERSION": VERSION})
        self.assertEqual({name: job["name"] for name, job in guard["jobs"].items()}, workflow.CI_GUARD_JOBS)
        kit = "The-Plum-Team/mod-base/.github/workflows/"
        local = "./.github/workflows/mod-base-guard.yml"
        build = documents[BUILD]["jobs"]
        self.assertEqual(workflow.CALLER["verify_kit"], "Verify pinned mod-base")
        self.assertEqual({name: (job["name"], job.get("uses")) for name, job in build.items()},
                         {"guard": ("Verify pinned mod-base", local),
                          "deferred": ("Build deferred for draft", None),
                          "shared": ("Shared Build", f"{kit}build.yml@{SHA}")})
        packaged = documents[PACKAGED]["jobs"]
        self.assertEqual({name: (job["name"], job.get("uses")) for name, job in packaged.items()},
                         {"guard": ("Verify pinned mod-base", local),
                          "deferred": ("Packaged E2E deferred for draft", None),
                          "select": ("Select exact Build", f"{kit}select-build.yml@{SHA}"),
                          "rebuild": ("Shared Build", f"{kit}build.yml@{SHA}"),
                          "shared": ("Shared Packaged E2E", f"{kit}packaged-e2e.yml@{SHA}")})
        status = documents[STATUS]["jobs"]
        self.assertEqual({name: (job["name"], job.get("uses")) for name, job in status.items()},
                         {"guard": ("Verify pinned mod-base", local),
                          "locate": ("Locate the pull request", None),
                          "evaluate": ("Evaluate protected gates", f"{kit}gate-status.yml@{SHA}"),
                          "publish": ("Publish protected gate statuses", None)})
        self.assertEqual({name: job["name"] for name, job in status.items()}, workflow.CI_CALLER_JOBS["status"])
        for jobs in (build, packaged, status):
            for name, job in jobs.items():
                if "uses" in job and name != "guard":
                    self.assertEqual(job["with"]["kit-sha"], "${{ needs.guard.outputs.kit-sha }}", name)
        # The one action a caller runs: the App token of the status caller's publishing job.
        token = "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1"
        self.assertEqual({(path, name): [step["uses"] for step in job.get("steps", []) if "uses" in step]
                          for path, document in documents.items() for name, job in document["jobs"].items()
                          if any("uses" in step for step in job.get("steps", []))},
                         {(STATUS, "publish"): [token]})

    def test_the_rendered_callers_pass_actionlint(self) -> None:
        require_tools("actionlint", "shellcheck")
        with tempfile.TemporaryDirectory(prefix="actionlint callers ") as temporary:
            for path, data in real_callers().items():
                write(Path(temporary), path, data)
            files = sorted(path for path in CALLERS)
            result = subprocess.run([shutil.which("actionlint") or "actionlint", "-no-color", *files], cwd=temporary,
                                    capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_mod_synced_from_the_kit_is_clean_in_every_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mb-callers-real-") as directory:
            repo = real_mod(Path(directory))
            self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])
            for state in STATES:
                with self.subTest(state=state):
                    for path in CALLERS:
                        (repo / path).unlink(missing_ok=True)
                    enter(repo, state)
                    written = tool.sync(repo, kit_root=KIT_ROOT, write=True)
                    self.assertEqual({drift.path for drift in written}, MANAGED[row(state)])
                    self.assertEqual(tool.check(repo, kit_root=KIT_ROOT), [])
                    for path in MANAGED[row(state)]:
                        self.assertEqual((repo / path).read_bytes(), real_callers()[path])


if __name__ == "__main__":
    unittest.main()
