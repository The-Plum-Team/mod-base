"""The activation manifest: its closed data, the callers each mode manages and the transitions.

The mode table and the transition list are written out here exactly as the architecture states
them, independently of ``mod_base.build_ci.activation``, and every pair of states is tried, so an
edit to either table fails unless both the code and this statement of the rule change.
"""

from __future__ import annotations

import itertools
import unittest
from typing import Any

from mod_base.build_ci import activation
from mod_base.build_ci.activation import (ABSENT_STATE, ACTIVATION_MODES, ACTIVATION_PATH, CALLERS, MANAGED_CALLERS,
                                          ROLLBACK_SOURCES, TRANSITIONS, activation_state, managed_callers,
                                          managed_mode, managing_modes, next_states, parse_activation,
                                          transition_refusal, validate_activation)
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document, validate_document
from tests.helpers import ci_activation, ci_config

GUARD = ".github/workflows/mod-base-guard.yml"
BUILD = ".github/workflows/mod-base-build.yml"
PACKAGED = ".github/workflows/mod-base-packaged-e2e.yml"
STATUS = ".github/workflows/mod-base-gate-status.yml"

#: What is managed in each state (the architecture's section 5), a reviewed rollback by the mode it leaves.
MANAGED = {
    "absent": set(),
    "disabled": set(),
    "shadow": {GUARD, BUILD, PACKAGED, STATUS},
    "shared-build": {GUARD, BUILD, STATUS},
    "shared-build-and-e2e": {GUARD, BUILD, PACKAGED, STATUS},
}

#: Every allowed change of state (the architecture's section 5): a manifest appears and disappears
#: only as ``disabled``; ``disabled`` and ``shadow`` exchange; ``disabled`` or ``shadow`` reach
#: ``shared-build``; ``shadow`` or ``shared-build`` reach ``shared-build-and-e2e``; every mode that
#: manages callers reaches the ``reviewed-rollback`` naming it, which reaches only ``disabled``.
ALLOWED = {
    ("absent", "disabled"), ("disabled", "absent"),
    ("disabled", "shadow"), ("shadow", "disabled"),
    ("disabled", "shared-build"), ("shadow", "shared-build"),
    ("shadow", "shared-build-and-e2e"), ("shared-build", "shared-build-and-e2e"),
    ("shadow", "reviewed-rollback:shadow"), ("shared-build", "reviewed-rollback:shared-build"),
    ("shared-build-and-e2e", "reviewed-rollback:shared-build-and-e2e"),
    ("reviewed-rollback:shadow", "disabled"), ("reviewed-rollback:shared-build", "disabled"),
    ("reviewed-rollback:shared-build-and-e2e", "disabled"),
}
STATES = ("absent", "disabled", "shadow", "shared-build", "shared-build-and-e2e", "reviewed-rollback:shadow",
          "reviewed-rollback:shared-build", "reviewed-rollback:shared-build-and-e2e")


def manifest(state: str) -> dict[str, Any] | None:
    """The validated manifest of a :data:`STATES` label (``None`` for ``absent``)."""

    if state == "absent":
        return None
    mode, _, left = state.partition(":")
    return validate_activation(ci_activation(mode, left or None))


class ActivationDataTests(unittest.TestCase):
    def test_the_fixed_path_kind_and_modes(self) -> None:
        self.assertEqual(ACTIVATION_PATH, "site/mod-base-build-activation.json")
        self.assertEqual(activation.ACTIVATION_KIND, "mod-base.ci.activation")
        self.assertEqual(ACTIVATION_MODES, ("disabled", "shadow", "shared-build", "shared-build-and-e2e",
                                            "reviewed-rollback"))
        self.assertEqual((activation.DISABLED_MODE, activation.ROLLBACK_MODE), ("disabled", "reviewed-rollback"))
        self.assertEqual(ROLLBACK_SOURCES, ("shadow", "shared-build", "shared-build-and-e2e"))
        self.assertEqual(ci_activation()["profile"], ci_config()["profile"])

    def test_every_mode_and_profile_validates_with_its_rollback_field(self) -> None:
        for profile in ("quick-skin", "block-pops"):
            for state in STATES[1:]:
                document = {**manifest(state), "profile": profile}
                with self.subTest(profile=profile, state=state):
                    self.assertIs(validate_activation(document), document)
                    self.assertIs(validate_document(document), document)
                    self.assertEqual(set(document), {"kind", "schema_version", "repository", "profile", "mode",
                                                     "rollback_from"})

    def test_a_rollback_names_the_mode_it_leaves_and_nothing_else_does(self) -> None:
        for mode in ACTIVATION_MODES:
            for left in (None, *ACTIVATION_MODES, "absent", "", 1, True, ["shadow"]):
                document = {**ci_activation(), "mode": mode, "rollback_from": left}
                valid = (left in ROLLBACK_SOURCES) if mode == "reviewed-rollback" else left is None
                with self.subTest(mode=mode, rollback_from=left):
                    if valid:
                        validate_activation(document)
                    else:
                        with self.assertRaises(MbError):
                            validate_activation(document)

    def test_unknown_profiles_modes_types_and_versions_reject(self) -> None:
        for field, value in (("mode", "enabled"), ("mode", True), ("mode", None), ("profile", "other"),
                             ("profile", []), ("schema_version", True), ("schema_version", 0), ("schema_version", 2),
                             ("schema_version", "1"), ("repository", "../foreign"), ("repository", "a/" + "b" * 201),
                             ("kind", "mod-base.build.config")):
            with self.subTest(field=field, value=value), self.assertRaises(MbError):
                validate_activation({**ci_activation(), field: value})
        for field in ci_activation():
            document = ci_activation()
            del document[field]
            with self.subTest(missing=field), self.assertRaises(MbError):
                validate_activation(document)
        for document in (None, [], "shadow", 1):
            with self.subTest(document=document), self.assertRaises(MbError):
                validate_activation(document)

    def test_no_execution_template_pin_or_authority_key_is_accepted(self) -> None:
        for field, value in (("templates", ["native.yml"]), ("jobs", {}), ("permissions", {"contents": "write"}),
                             ("secrets", {}), ("kit_sha", "a" * 40), ("pin", "a" * 40), ("extensions", {}),
                             ("deferred", ["native.yml"]), ("approval", True), ("matrix", []), ("scenarios", []),
                             ("path", "other.json"), ("callers", [BUILD]), ("managed", [BUILD]),
                             ("contexts", {"build": "Build and verify"})):
            with self.subTest(field=field), self.assertRaises(MbError):
                validate_activation({**ci_activation("shadow"), field: value})

    def test_the_reader_is_strict_and_bounded(self) -> None:
        raw = canonical_json(ci_activation("shadow"))
        self.assertEqual(parse_activation(raw), ci_activation("shadow"))
        self.assertEqual(load_document(raw, kind="mod-base.ci.activation"), ci_activation("shadow"))
        hostile = {
            "duplicate key": raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
            "duplicate mode": raw.replace(b'"mode":"shadow"', b'"mode":"disabled","mode":"shadow"'),
            "non-finite": raw.replace(b'"schema_version":1', b'"schema_version":NaN'),
            "float version": raw.replace(b'"schema_version":1', b'"schema_version":1.0'),
            "byte-order mark": b"\xef\xbb\xbf" + raw,
            "not UTF-8": raw.replace(b"shadow", b"shad\xffw"),
            "truncated": raw[:-3],
            "trailing document": raw + raw,
            "array": b"[" + raw.strip() + b"]",
            "empty": b"",
            "null": b"null",
            "too large": raw.rstrip(b"\n") + b" " * limits.MAX_CI_ACTIVATION_BYTES,
            "lone surrogate": raw.replace(b"example/mod", b"example/\\ud800"),
        }
        self.assertEqual(limits.MAX_CI_ACTIVATION_BYTES, 8 * 1024)
        for label, data in hostile.items():
            for reader in (parse_activation, lambda value: load_document(value, kind="mod-base.ci.activation")):
                with self.subTest(label=label), self.assertRaises(MbError):
                    reader(data)
        padded = raw.rstrip(b"\n") + b" " * (limits.MAX_CI_ACTIVATION_BYTES - len(raw) + 1)
        self.assertEqual(len(padded), limits.MAX_CI_ACTIVATION_BYTES)
        self.assertEqual(parse_activation(padded), ci_activation("shadow"))


class ManagedCallersTests(unittest.TestCase):
    def test_the_four_caller_paths(self) -> None:
        self.assertEqual(CALLERS, (GUARD, BUILD, PACKAGED, STATUS))
        self.assertEqual((activation.GUARD_CALLER, activation.BUILD_CALLER, activation.PACKAGED_CALLER,
                          activation.STATUS_CALLER), CALLERS)

    def test_each_state_manages_exactly_the_files_of_its_row(self) -> None:
        self.assertEqual(set(MANAGED_CALLERS), set(ACTIVATION_MODES) - {"reviewed-rollback"})
        for state in STATES:
            document = manifest(state)
            row = state.rpartition(":")[2]
            with self.subTest(state=state):
                self.assertEqual(set(managed_callers(document)), MANAGED[row])
                self.assertEqual(managed_mode(document), "disabled" if row == "absent" else row)
                self.assertEqual([path for path in CALLERS if path in MANAGED[row]], list(managed_callers(document)))

    def test_a_rollback_keeps_the_files_of_the_mode_it_leaves(self) -> None:
        for left in ROLLBACK_SOURCES:
            self.assertEqual(managed_callers(manifest(f"reviewed-rollback:{left}")), managed_callers(manifest(left)))
            self.assertNotEqual(managed_callers(manifest(left)), ())

    def test_managing_modes_is_the_table_read_by_caller(self) -> None:
        self.assertEqual(managing_modes(PACKAGED), {"shadow", "shared-build-and-e2e"})
        for caller in (GUARD, BUILD, STATUS):
            self.assertEqual(managing_modes(caller), {"shadow", "shared-build", "shared-build-and-e2e"})
        self.assertEqual(managing_modes(".github/workflows/pages.yml"), frozenset())
        for caller in CALLERS:
            self.assertNotIn("disabled", managing_modes(caller))


class TransitionTests(unittest.TestCase):
    def test_every_pair_of_states_is_allowed_exactly_as_listed(self) -> None:
        for before, after in itertools.product(STATES, repeat=2):
            refusal = transition_refusal(manifest(before), manifest(after))
            with self.subTest(before=before, after=after):
                if before == after or (before, after) in ALLOWED:
                    self.assertIsNone(refusal)
                else:
                    self.assertIsInstance(refusal, str)
                    self.assertTrue(refusal)
        self.assertEqual(len(ALLOWED), 14)

    def test_the_table_lists_the_same_transitions(self) -> None:
        listed = {(before, after) for before, targets in TRANSITIONS.items() for after in targets}
        collapsed = {(before.partition(":")[0], after.partition(":")[0]) for before, after in ALLOWED}
        self.assertEqual(listed, collapsed)
        self.assertEqual(set(TRANSITIONS), {ABSENT_STATE, *ACTIVATION_MODES})
        for state in STATES:
            self.assertEqual(next_states(manifest(state)), TRANSITIONS[state.partition(":")[0]])
            self.assertEqual(activation_state(manifest(state)), state.partition(":")[0])

    def test_a_mode_that_manages_callers_never_becomes_a_missing_manifest(self) -> None:
        for state in STATES[2:]:
            with self.subTest(state=state):
                self.assertIn("cannot be removed", transition_refusal(manifest(state), None))
        self.assertIsNone(transition_refusal(manifest("disabled"), None))

    def test_a_manifest_appears_only_as_disabled(self) -> None:
        for state in STATES[2:]:
            with self.subTest(state=state):
                self.assertIn("from absent it may change to disabled", transition_refusal(None, manifest(state)))

    def test_a_rollback_must_name_exactly_the_mode_it_leaves(self) -> None:
        for before, left in itertools.product(ROLLBACK_SOURCES, ROLLBACK_SOURCES):
            refusal = transition_refusal(manifest(before), manifest(f"reviewed-rollback:{left}"))
            with self.subTest(before=before, rollback_from=left):
                if before == left:
                    self.assertIsNone(refusal)
                else:
                    self.assertIn(f"must name it: rollback_from is {left}", refusal)
        self.assertIn("cannot change from disabled to reviewed-rollback",
                      transition_refusal(manifest("disabled"), manifest("reviewed-rollback:shadow")))

    def test_a_rollback_leads_only_to_disabled(self) -> None:
        for left in ROLLBACK_SOURCES:
            rollback = manifest(f"reviewed-rollback:{left}")
            self.assertIsNone(transition_refusal(rollback, manifest("disabled")))
            for after in ("shadow", "shared-build", "shared-build-and-e2e"):
                with self.subTest(left=left, after=after):
                    self.assertIn("it may change to disabled", transition_refusal(rollback, manifest(after)))
            for other in ROLLBACK_SOURCES:
                if other != left:
                    self.assertIn("while its mode stayed reviewed-rollback",
                                  transition_refusal(rollback, manifest(f"reviewed-rollback:{other}")))

    def test_a_transition_changes_nothing_but_the_mode(self) -> None:
        before = manifest("disabled")
        for field, value in (("repository", "example/other"), ("profile", "quick-skin")):
            for mode in ("disabled", "shadow"):
                after = validate_activation({**ci_activation(mode), field: value})
                with self.subTest(field=field, mode=mode):
                    self.assertIn("never the repository or the profile", transition_refusal(before, after))
        self.assertIsNone(transition_refusal(before, manifest("disabled")))
        self.assertIsNone(transition_refusal(None, None))


if __name__ == "__main__":
    unittest.main()
