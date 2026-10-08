"""Admission of an activation change (``mod_base.build_ci.transition``) and its two ``template`` verbs.

``admit_transition`` is tried over every pair of states, at an equal pin and across a pin change,
and over hostile manifest bytes. ``verify_candidate_callers`` runs against the kit's own templates.
``template activation`` and ``template transition`` run over real checkouts in temporary
directories, the way an operator runs them before opening an activation pull request.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import shutil
import unittest
from pathlib import Path
from unittest import mock

from mod_base import cli
from mod_base.build_ci.activation import ACTIVATION_PATH, CALLERS, TRANSITIONS
from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.build_ci.transition import Transition, admit_transition, verify_candidate_callers
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.pin import Pin
from mod_base.template import tool
from tests.helpers import ci_activation
from tests.test_ci_activation import ALLOWED, BUILD, GUARD, MANAGED, PACKAGED, STATES, STATUS, manifest
from tests.test_pin import BOOT
from tests.test_template_callers import SYNTHETIC_CALLERS, CallerCase, enter, real_callers, render, row
from tests.test_template_tool import KIT_ROOT, OTHER_SHA, SHA, write

VERSION = "v1.2.3"
PIN = Pin(SHA, VERSION, ())
MOVED = (Pin(OTHER_SHA, VERSION, ()), Pin(SHA, "v1.2.4", ()), Pin(OTHER_SHA, "v1.2.4", ()))


def data(state: str) -> bytes | None:
    """The manifest bytes of a ``STATES`` label (``None`` for ``absent``)."""

    document = manifest(state)
    return None if document is None else canonical_json(document)


class AdmitTransitionTest(unittest.TestCase):
    def test_every_pair_of_states_at_an_equal_pin(self) -> None:
        for before, after in itertools.product(STATES, repeat=2):
            with self.subTest(before=before, after=after):
                if before == after or (before, after) in ALLOWED:
                    admitted = admit_transition(data(before), data(after), protected_pin=PIN, candidate_pin=PIN)
                    self.assertEqual(admitted, Transition(before.partition(":")[0], after.partition(":")[0],
                                                          before != after,
                                                          tuple(path for path in CALLERS if path in MANAGED[row(after)])))
                else:
                    with self.assertRaises(MbError) as caught:
                        admit_transition(data(before), data(after), protected_pin=PIN, candidate_pin=PIN)
                    self.assertEqual((caught.exception.exit_code, caught.exception.reason), (2, "activation"))

    def test_a_transition_never_comes_with_a_pin_change(self) -> None:
        for before, after in sorted(ALLOWED):
            for moved in MOVED:
                with self.subTest(before=before, after=after, moved=moved), \
                        self.assertRaisesRegex(MbError, "never comes with a pin change"):
                    admit_transition(data(before), data(after), protected_pin=PIN, candidate_pin=moved)
        admitted = admit_transition(data("disabled"), data("shadow"), protected_pin=PIN,
                                    candidate_pin=Pin(SHA, VERSION, (".github/workflows/mod-base-build.yml@19",)))
        self.assertTrue(admitted.changed, "the pin is its commit and version; where it is referenced may change")

    def test_an_unchanged_manifest_is_no_transition_whatever_the_pins(self) -> None:
        for state in STATES:
            for moved in (PIN, *MOVED):
                with self.subTest(state=state, moved=moved):
                    admitted = admit_transition(data(state), data(state), protected_pin=PIN, candidate_pin=moved)
                    self.assertFalse(admitted.changed)
                    self.assertEqual(admitted.previous, admitted.current)
        pretty = b'{"mode": "shadow",\n "kind": "mod-base.ci.activation", "schema_version": 1,\n' \
                 b' "repository": "example/mod", "profile": "block-pops", "rollback_from": null}\n'
        self.assertFalse(admit_transition(data("shadow"), pretty, protected_pin=PIN, candidate_pin=MOVED[2]).changed,
                         "the same document in other bytes is the same state")

    def test_an_illegal_transition_is_refused_before_the_pins_are_compared(self) -> None:
        with self.assertRaisesRegex(MbError, "cannot change from disabled to shared-build-and-e2e"):
            admit_transition(data("disabled"), data("shared-build-and-e2e"), protected_pin=PIN, candidate_pin=MOVED[0])
        with self.assertRaisesRegex(MbError, "cannot be removed in the shadow mode"):
            admit_transition(data("shadow"), None, protected_pin=PIN, candidate_pin=PIN)
        with self.assertRaisesRegex(MbError, "from absent it may change to disabled"):
            admit_transition(None, data("shadow"), protected_pin=PIN, candidate_pin=PIN)
        with self.assertRaisesRegex(MbError, "must name it: rollback_from is shadow"):
            admit_transition(data("shared-build"), data("reviewed-rollback:shadow"), protected_pin=PIN, candidate_pin=PIN)
        other = canonical_json({**ci_activation("shadow"), "repository": "example/other"})
        with self.assertRaisesRegex(MbError, "never the repository or the profile"):
            admit_transition(data("disabled"), other, protected_pin=PIN, candidate_pin=PIN)

    def test_hostile_manifest_bytes_are_refused_on_either_side(self) -> None:
        shadow = data("shadow")
        assert shadow is not None
        hostile = {
            "empty": b"",
            "not JSON": b"mode: shadow\n",
            "byte-order mark": b"\xef\xbb\xbf" + shadow,
            "not UTF-8": shadow.replace(b"shadow", b"shad\xffw"),
            "duplicate mode": shadow.replace(b'"mode":"shadow"', b'"mode":"disabled","mode":"shadow"'),
            "non-finite": shadow.replace(b'"schema_version":1', b'"schema_version":Infinity'),
            "too large": shadow.rstrip(b"\n") + b" " * limits.MAX_CI_ACTIVATION_BYTES,
            "array": b"[" + shadow.strip() + b"]",
            "an unknown mode": shadow.replace(b'"shadow"', b'"shared"'),
            "an unknown key": canonical_json({**ci_activation("shadow"), "pin": SHA}),
            "another kind": canonical_json({**ci_activation("shadow"), "kind": "mod-base.build.config"}),
            "a rollback from nowhere": canonical_json({**ci_activation("shadow"), "mode": "reviewed-rollback"}),
            "a later schema": canonical_json({**ci_activation("shadow"), "schema_version": 2}),
        }
        for label, raw in hostile.items():
            for protected, candidate in ((raw, shadow), (shadow, raw), (raw, raw), (None, raw), (raw, None)):
                with self.subTest(label=label, protected=protected is raw, candidate=candidate is raw), \
                        self.assertRaises(MbError):
                    admit_transition(protected, candidate, protected_pin=PIN, candidate_pin=PIN)
        with self.assertRaises(MbError):
            admit_transition("{}", shadow, protected_pin=PIN, candidate_pin=PIN)  # type: ignore[arg-type]

    def test_the_error_names_the_side_whose_manifest_is_malformed(self) -> None:
        with self.assertRaisesRegex(MbError, "the protected activation manifest is not valid JSON"):
            admit_transition(b"{", data("shadow"), protected_pin=PIN, candidate_pin=PIN)
        with self.assertRaisesRegex(MbError, "the candidate activation manifest is not valid JSON"):
            admit_transition(data("shadow"), b"{", protected_pin=PIN, candidate_pin=PIN)

    def test_pins_are_parsed_pins(self) -> None:
        for protected, candidate in (((SHA, VERSION), PIN), (PIN, SHA), (None, PIN), (PIN, BOOT.Pin(SHA, VERSION, ()))):
            with self.subTest(protected=protected, candidate=candidate), self.assertRaisesRegex(MbError, "parsed pin"):
                admit_transition(data("shadow"), data("shadow"), protected_pin=protected,  # type: ignore[arg-type]
                                 candidate_pin=candidate)  # type: ignore[arg-type]


class VerifyCandidateCallersTest(unittest.TestCase):
    def test_exactly_the_rendered_callers_of_each_state_are_admitted(self) -> None:
        rendered = real_callers()
        for state in STATES:
            managed = {path: rendered[path] for path in CALLERS if path in MANAGED[row(state)]}
            with self.subTest(state=state):
                self.assertEqual(verify_candidate_callers(managed, candidate=data(state), pin=PIN, kit_root=KIT_ROOT),
                                 tuple(managed))
                for path in CALLERS:
                    files = dict(managed)
                    if path in managed:
                        del files[path]
                        expected = f"{path} is missing"
                    else:
                        files[path] = rendered[path]
                        expected = f"{path} must not exist in the {state.partition(':')[0]} state"
                    with self.assertRaises(MbError) as caught:
                        verify_candidate_callers(files, candidate=data(state), pin=PIN, kit_root=KIT_ROOT)
                    self.assertIn(expected, str(caught.exception))
                    self.assertEqual(caught.exception.reason, "activation")

    def test_any_other_byte_in_a_managed_caller_is_refused(self) -> None:
        rendered = real_callers()
        build = rendered[BUILD]
        changes = {
            "an appended job": build + b"  ext-extra:\n    runs-on: ubuntu-24.04\n",
            "an extension region": build + b"# >>> mod-local extensions: x\n# <<< mod-local extensions\n",
            "CRLF line endings": build.replace(b"\n", b"\r\n"),
            "no final newline": build[:-1],
            "another commit": real_callers(OTHER_SHA)[BUILD],
            "another version": real_callers(SHA, "v1.2.4")[BUILD],
            "the unrendered template": (KIT_ROOT / "template/managed" / BUILD).read_bytes(),
            "a wider trigger": build.replace(b"on: workflow_dispatch", b"on: pull_request_target"),
            "a wider permission": build.replace(b"contents: read", b"contents: write", 1),
            "another caller's bytes": rendered[PACKAGED],
            "empty": b"",
        }
        for label, changed in changes.items():
            with self.subTest(label), self.assertRaisesRegex(MbError, f"{BUILD} differs from the kit template"):
                self.assertNotEqual(changed, build)
                verify_candidate_callers({**rendered, BUILD: changed}, candidate=data("shadow"), pin=PIN,
                                         kit_root=KIT_ROOT)

    def test_every_problem_is_reported_at_once_and_other_paths_are_refused(self) -> None:
        rendered = real_callers()
        files = {GUARD: rendered[GUARD] + b"#\n", PACKAGED: rendered[PACKAGED], ".github/workflows/pages.yml": b"x\n",
                 "scripts/ci/x.py": b"x\n"}
        with self.assertRaises(MbError) as caught:
            verify_candidate_callers(files, candidate=data("shared-build"), pin=PIN, kit_root=KIT_ROOT)
        message = str(caught.exception)
        for expected in (".github/workflows/pages.yml is not a mod-base Build/E2E caller",
                         "scripts/ci/x.py is not a mod-base Build/E2E caller", f"{GUARD} differs from the kit template",
                         f"{BUILD} is missing", f"{PACKAGED} must not exist in the shared-build state",
                         f"{STATUS} is missing"):
            self.assertIn(expected, message)
        with self.assertRaises(MbError) as caught:
            verify_candidate_callers({f"docs/{index}.md": b"" for index in range(40)}, candidate=None, pin=PIN,
                                     kit_root=KIT_ROOT)
        self.assertIn("and 32 more", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 1000)

    def test_a_candidate_without_a_manifest_holds_no_caller(self) -> None:
        self.assertEqual(verify_candidate_callers({}, candidate=None, pin=PIN, kit_root=KIT_ROOT), ())
        with self.assertRaisesRegex(MbError, f"{BUILD} must not exist in the absent state"):
            verify_candidate_callers({BUILD: real_callers()[BUILD]}, candidate=None, pin=PIN, kit_root=KIT_ROOT)

    def test_malformed_arguments_are_refused(self) -> None:
        rendered = real_callers()
        with self.assertRaisesRegex(MbError, "must be read as bytes"):
            verify_candidate_callers({**rendered, BUILD: rendered[BUILD].decode()},  # type: ignore[dict-item]
                                     candidate=data("shadow"), pin=PIN, kit_root=KIT_ROOT)
        with self.assertRaisesRegex(MbError, "parsed pin"):
            verify_candidate_callers(rendered, candidate=data("shadow"), pin=(SHA, VERSION),  # type: ignore[arg-type]
                                     kit_root=KIT_ROOT)
        with self.assertRaisesRegex(MbError, "the candidate activation manifest is not valid JSON"):
            verify_candidate_callers(rendered, candidate=b"{", pin=PIN, kit_root=KIT_ROOT)
        with self.assertRaises(MbError):
            verify_candidate_callers(rendered, candidate=data("shadow"), pin=PIN, kit_root=KIT_ROOT / "docs")


class TransitionCommandTest(CallerCase):
    """Two checkouts of one mod: the protected base and the candidate of a pull request."""

    def checkout(self, name: str, state: str, *, pin: Pin = PIN, sync: bool = True) -> Path:
        repo = self.root / name
        repo.mkdir()
        self.repo = repo
        self.make_clean_mod()
        if (pin.sha, pin.version) != (SHA, VERSION):
            BOOT.rewrite_pin(repo, BOOT.Pin(pin.sha, pin.version, ()))
            tool.sync(repo, kit_root=self.kit, write=True)
        enter(repo, state)
        if sync:
            tool.sync(repo, kit_root=self.kit, write=True)
        return repo

    def run_cli(self, *arguments: str, kit_sha: str | None = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {} if kit_sha is None else {"MOD_BASE_KIT_SHA": kit_sha}
        with mock.patch("mod_base.runtime.kit_root", return_value=self.kit), \
                mock.patch("mod_base.cli.environ", lambda: environment), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(arguments))
        return code, stdout.getvalue(), stderr.getvalue()

    def transition(self, base: Path, candidate: Path, **options: str | None) -> tuple[int, str, str]:
        return self.run_cli("template", "transition", "--repo", str(candidate), "--base", str(base), **options)

    def test_activation_prints_the_state_its_callers_and_what_may_follow(self) -> None:
        self.make_clean_mod()
        for state in STATES:
            self.enter(state)
            mode, _, left = state.partition(":")
            expected = [f"state: {mode}"]
            if state != "absent":
                expected += ["repository: example/mod", "profile: block-pops"]
            if left:
                expected.append(f"rollback_from: {left}")
            expected += [f"managed: {path}" for path in CALLERS if path in MANAGED[row(state)]]
            expected += [f"next: {following}" for following in TRANSITIONS[mode]]
            with self.subTest(state=state):
                self.assertEqual(self.run_cli("template", "activation", "--repo", str(self.repo)),
                                 (0, "".join(f"{line}\n" for line in expected), ""))
        self.assertEqual(self.run_cli("template", "activation", "--repo", str(self.repo))[1].count("managed: "), 4)

    def test_activation_refuses_an_inconsistent_state_with_one_line(self) -> None:
        self.make_clean_mod()
        cases = {
            "a configuration without a manifest": lambda: (self.enter("shadow"), (self.repo / ACTIVATION_PATH).unlink()),
            "a manifest without a configuration": lambda: (self.enter("shadow"), (self.repo / BUILD_CONFIG_PATH).unlink()),
            "a malformed manifest": lambda: (self.enter("shadow"), (self.repo / ACTIVATION_PATH).write_bytes(b"{")),
            "an unknown mode": lambda: (self.enter("shadow"), (self.repo / ACTIVATION_PATH).write_bytes(
                canonical_json({**ci_activation(), "mode": "enabled"}))),
        }
        for label, prepare in cases.items():
            with self.subTest(label):
                prepare()
                code, stdout, stderr = self.run_cli("template", "activation", "--repo", str(self.repo))
                self.assertEqual((code, stdout), (2, ""))
                self.assertEqual(stderr.count("\n"), 1)
                self.assertTrue(stderr.startswith("mod_base: "))
        self.assertEqual(self.run_cli("template", "activation", "--repo", str(self.root / "absent"))[0], 2)
        self.assertEqual(self.run_cli("template", "activation")[0], 2)

    def test_an_allowed_transition_with_synced_callers_is_admitted(self) -> None:
        for index, (before, after) in enumerate(sorted(ALLOWED)):
            with self.subTest(before=before, after=after):
                base = self.checkout(f"base-{index}", before)
                candidate = self.checkout(f"candidate-{index}", after, sync=False)
                for path in CALLERS:
                    if path in MANAGED[row(after)]:
                        write(candidate, path, self.rendered(path))
                managed = [path for path in CALLERS if path in MANAGED[row(after)]]
                self.assertEqual(self.transition(base, candidate), (
                    0, f"transition: {before.partition(':')[0]} -> {after.partition(':')[0]}\npin: {SHA} {VERSION}\n"
                    + "".join(f"managed: {path}\n" for path in managed), ""))
                self.assertEqual(self.transition(base, candidate, kit_sha=SHA)[0], 0)
                self.assertEqual(tool.check(candidate, kit_root=self.kit), [], "the candidate's own check agrees")

    def test_every_other_change_of_state_is_refused(self) -> None:
        checkouts = {state: self.checkout(f"state-{index}", state) for index, state in enumerate(STATES)}
        for before, after in itertools.product(STATES, repeat=2):
            code, stdout, stderr = self.transition(checkouts[before], checkouts[after])
            with self.subTest(before=before, after=after):
                if before == after:
                    self.assertEqual((code, stdout.splitlines()[0]), (0, f"transition: {after.partition(':')[0]} (unchanged)"))
                elif (before, after) in ALLOWED:
                    self.assertEqual((code, stdout.splitlines()[0]),
                                     (0, f"transition: {before.partition(':')[0]} -> {after.partition(':')[0]}"))
                else:
                    self.assertEqual((code, stdout), (2, ""))
                    self.assertTrue(stderr.startswith("mod_base: activation: "), stderr)
                    self.assertEqual(stderr.count("\n"), 1)

    def test_a_transition_with_a_pin_change_is_refused_and_a_bump_alone_is_admitted(self) -> None:
        moved = Pin(OTHER_SHA, "v1.2.4", ())
        base = self.checkout("base", "disabled")
        candidate = self.checkout("candidate", "shadow", pin=moved)
        self.assertEqual(tool.check(candidate, kit_root=self.kit), [])
        code, stdout, stderr = self.transition(base, candidate)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("never comes with a pin change", stderr)
        self.assertIn(f"{SHA} {VERSION}", stderr)
        self.assertIn(f"{OTHER_SHA} v1.2.4", stderr)
        active = self.checkout("active", "shadow")
        self.assertEqual(self.transition(active, candidate), (
            0, f"transition: shadow (unchanged)\npin: {OTHER_SHA} v1.2.4\n" + "".join(f"managed: {path}\n" for path in CALLERS),
            ""))

    def test_the_candidate_callers_must_be_the_rendered_templates(self) -> None:
        base = self.checkout("base", "disabled")
        candidate = self.checkout("candidate", "shared-build")
        self.assertEqual(self.transition(base, candidate)[0], 0)
        clean = {path: (candidate / path).read_bytes() for path in (GUARD, BUILD, STATUS)}
        cases = {
            "a missing caller": (lambda: (candidate / STATUS).unlink(), f"{STATUS} is missing"),
            "an edited caller": (lambda: write(candidate, BUILD, clean[BUILD] + b"  ext-x:\n    runs-on: x\n"),
                                 f"{BUILD} differs from the kit template"),
            "a caller of another mode": (lambda: write(candidate, PACKAGED, self.rendered(PACKAGED)),
                                         f"{PACKAGED} must not exist in the shared-build state"),
            "a stale guard literal": (lambda: write(candidate, GUARD, self.rendered(GUARD, OTHER_SHA)),
                                      f"{GUARD} differs from the kit template"),
        }
        for label, (change, expected) in cases.items():
            with self.subTest(label):
                change()
                code, stdout, stderr = self.transition(base, candidate)
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(expected, stderr)
                (candidate / PACKAGED).unlink(missing_ok=True)
                for path, content in clean.items():
                    write(candidate, path, content)
        outside = write(self.root, "outside.yml", clean[BUILD])
        (candidate / BUILD).unlink()
        (candidate / BUILD).symlink_to(outside)
        self.assertEqual(self.transition(base, candidate)[0], 2)

    def test_leaving_a_mode_requires_its_callers_to_go(self) -> None:
        base = self.checkout("base", "shadow")
        candidate = self.checkout("candidate", "shadow")
        enter(candidate, "disabled")
        code, _, stderr = self.transition(base, candidate)
        self.assertEqual(code, 2)
        for path in CALLERS:
            self.assertIn(f"{path} must not exist in the disabled state", stderr)
        for path in CALLERS:
            (candidate / path).unlink()
        self.assertEqual(self.transition(base, candidate)[:2], (0, f"transition: shadow -> disabled\npin: {SHA} {VERSION}\n"))

    def test_removing_the_manifest_of_an_active_mod_is_refused(self) -> None:
        base = self.checkout("base", "shared-build")
        candidate = self.checkout("candidate", "shared-build")
        enter(candidate, "absent")
        code, _, stderr = self.transition(base, candidate)
        self.assertEqual(code, 2)
        self.assertIn("cannot be removed in the shared-build mode", stderr)
        (candidate / BUILD_CONFIG_PATH.rsplit("/", 1)[0]).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(base / BUILD_CONFIG_PATH, candidate / BUILD_CONFIG_PATH)
        code, _, stderr = self.transition(base, candidate)
        self.assertEqual(code, 2)
        self.assertIn("exists without site/mod-base-build-activation.json", stderr)

    def test_the_templates_compared_are_those_of_the_kit_the_candidate_pins(self) -> None:
        base = self.checkout("base", "disabled")
        candidate = self.checkout("candidate", "shadow")
        code, stdout, stderr = self.transition(base, candidate, kit_sha=OTHER_SHA)
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn(f"must run from the kit the candidate pins ({SHA})", stderr)
        self.assertEqual(self.transition(base, candidate, kit_sha=SHA)[0], 0)
        write(self.kit, f"template/managed/{BUILD}", SYNTHETIC_CALLERS[BUILD] + "# a later template\n")
        code, _, stderr = self.transition(base, candidate, kit_sha=SHA)
        self.assertEqual(code, 2)
        self.assertIn(f"{BUILD} differs from the kit template", stderr)

    def test_both_checkouts_are_required_and_must_carry_a_pin(self) -> None:
        base = self.checkout("base", "disabled")
        self.assertEqual(self.run_cli("template", "transition", "--repo", str(base))[0], 2)
        self.assertEqual(self.run_cli("template", "transition", "--base", str(base))[0], 2)
        self.assertEqual(self.transition(base, self.root / "absent")[0], 2)
        self.assertEqual(self.transition(self.root / "absent", base)[0], 2)
        empty = self.root / "empty"
        empty.mkdir()
        code, _, stderr = self.transition(empty, base)
        self.assertEqual(code, 2)
        self.assertIn("no mod-base pin", stderr)

    def test_the_synthetic_templates_are_what_this_kit_renders(self) -> None:
        self.assertEqual(tool.expected_callers(self.kit, PIN, ci_activation("shadow")),
                         {path: render(SYNTHETIC_CALLERS[path]) for path in CALLERS})


if __name__ == "__main__":
    unittest.main()
