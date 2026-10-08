"""The Build adapter contract: closed hooks, argv, environment, file names and hook-output parsers."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from mod_base.build_ci import adapter, controller, exports, inputs, runtime_inputs, validation
from mod_base.build_ci.config import validate_build_config
from mod_base.build_ci.worker import WORKER_ACCOUNTS, WORKER_ROOT, _execution_command, worker_environment
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_config, ci_plan

PYTHON = "/opt/python/bin/python3"


def derived(plan: dict | None = None) -> dict:
    plan = ci_plan() if plan is None else plan
    return {"targets": copy.deepcopy(plan["targets"]), "lanes": copy.deepcopy(plan["lanes"])}


def encoded(document: object) -> bytes:
    return json.dumps(document).encode("utf-8")


class ContractTableTests(unittest.TestCase):
    def test_the_eight_hooks_are_exactly_the_contract_table(self) -> None:
        self.assertEqual([(hook.name, hook.role, hook.unit, hook.timeout) for hook in adapter.HOOKS.values()], [
            ("derive_plan", "validator", None, "validator_seconds"),
            ("policy", "candidate", None, "policy_seconds"),
            ("build_target", "candidate", "target", "target_seconds"),
            ("verify_target", "validator", "target", "validator_seconds"),
            ("verify_build", "validator", None, "validator_seconds"),
            ("derive_runtime", "validator", "lane", "validator_seconds"),
            ("run_lane", "candidate", "lane", "runtime_seconds"),
            ("verify_runtime", "validator", "lane", "validator_seconds"),
        ])
        self.assertEqual(adapter.PROTECTED_HOOKS,
                         ("derive_plan", "verify_target", "verify_build", "derive_runtime", "verify_runtime"))
        self.assertEqual(adapter.CANDIDATE_HOOKS, ("policy", "build_target", "run_lane"))

    def test_roles_and_directories_are_the_worker_layout(self) -> None:
        self.assertEqual({hook.role for hook in adapter.HOOKS.values()}, set(WORKER_ACCOUNTS))
        self.assertEqual(WORKER_ROOT / adapter.CHECKOUT_DIRECTORY["validator"], controller.CONTROLLER_VALIDATION_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.CHECKOUT_DIRECTORY["candidate"], exports.CANDIDATE_SOURCE_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.INPUT_DIRECTORY, inputs.VALIDATOR_INPUT_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.HOME_DIRECTORY["validator"] / adapter.OUTPUT_DIRECTORY,
                         validation.VALIDATOR_OUTPUT_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.HOME_DIRECTORY["candidate"] / adapter.EXPORT_DIRECTORY,
                         exports.CANDIDATE_OUTPUT_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.SEALED_BUILD_DIRECTORY, exports.BUILD_VALIDATION_ROOT)
        self.assertEqual(WORKER_ROOT / adapter.SEALED_RUNTIME_DIRECTORY, runtime_inputs.RUNTIME_VALIDATION_ROOT)

    def test_file_names_and_reserved_units(self) -> None:
        self.assertEqual((adapter.INVENTORY_INPUT, adapter.SCENARIO_INPUT, adapter.PLAN_INPUT),
                         ("inventory", "scenario-contract", "ci-plan.json"))
        self.assertEqual((adapter.PLAN_OUTPUT, adapter.RUNTIME_OUTPUT), ("plan.json", "runtime.json"))
        self.assertEqual(adapter.RESERVED_UNIT_IDS, frozenset({"plan", "runtime", "ci-validation"}))
        self.assertEqual(adapter.RUNTIME_VALUES, ("E2E_ROW_JSON", "E2E_SCENARIOS"))


class CommandTests(unittest.TestCase):
    def test_every_hook_has_the_fixed_argv_the_worker_launches(self) -> None:
        dispatcher = ci_config()["adapter"]["dispatcher"]
        for name, hook in adapter.HOOKS.items():
            checkout = WORKER_ROOT / adapter.CHECKOUT_DIRECTORY[hook.role]
            command = adapter.hook_command(name, python=PYTHON, checkout=str(checkout), dispatcher=dispatcher)
            with self.subTest(hook=name):
                self.assertEqual(command, (PYTHON, "-I", "-B", f"{checkout}/{dispatcher}", "--hook", name))
                self.assertEqual(_execution_command(command, PYTHON, hook.role), Path(str(checkout)))

    def test_unknown_hooks_and_unsafe_parts_are_rejected(self) -> None:
        good = {"python": PYTHON, "checkout": "/w/controller", "dispatcher": "scripts/ci/dispatch.py"}
        for hook in ("verify", "derive_plan ", "", None, 7, ["derive_plan"]):
            with self.subTest(hook=hook), self.assertRaises(MbError):
                adapter.hook_command(hook, **good)
        for change in ({"python": "python3"}, {"python": ""}, {"python": None}, {"checkout": "controller"},
                       {"checkout": "/w/controller/"}, {"checkout": "/w/\0x"}, {"dispatcher": "../dispatch.py"},
                       {"dispatcher": "/abs/dispatch.py"}, {"dispatcher": "scripts/.git/x.py"}, {"dispatcher": None}):
            with self.subTest(change=change), self.assertRaises(MbError):
                adapter.hook_command("derive_plan", **{**good, **change})

    def test_timeouts_come_from_the_protected_config(self) -> None:
        config = ci_config()
        config["timeouts"] = {"policy_seconds": 1, "target_seconds": 2, "runtime_seconds": 3, "validator_seconds": 4}
        self.assertEqual({name: adapter.hook_timeout_seconds(name, config) for name in adapter.HOOKS}, {
            "derive_plan": 4, "policy": 1, "build_target": 2, "verify_target": 4, "verify_build": 4,
            "derive_runtime": 4, "run_lane": 3, "verify_runtime": 4})
        with self.assertRaises(MbError):
            adapter.hook_timeout_seconds("compile", config)


class EnvironmentTests(unittest.TestCase):
    RUNTIME = {"E2E_ROW_JSON": '{"id":"lane-a"}', "E2E_SCENARIOS": "smoke,gallery"}

    def test_values_per_hook(self) -> None:
        self.assertEqual(adapter.hook_values("derive_plan"), {})
        self.assertEqual(adapter.hook_values("policy"), {})
        self.assertEqual(adapter.hook_values("verify_build"), {})
        for hook in ("build_target", "verify_target"):
            self.assertEqual(adapter.hook_values(hook, unit_id="1.20.1"), {"MB_TARGET_ID": "1.20.1"})
        for hook in ("derive_runtime", "verify_runtime"):
            self.assertEqual(adapter.hook_values(hook, unit_id="fabric-1.20.1"), {"MB_LANE_ID": "fabric-1.20.1"})
        self.assertEqual(adapter.hook_values("run_lane", unit_id="lane-a", runtime=self.RUNTIME),
                         {"MB_LANE_ID": "lane-a", **self.RUNTIME})

    def test_the_worker_environment_accepts_every_hooks_values(self) -> None:
        identity = ci_plan()["identity"]
        for name, hook in adapter.HOOKS.items():
            values = adapter.hook_values(name, unit_id=None if hook.unit is None else "unit-1",
                                         runtime=self.RUNTIME if name == "run_lane" else None)
            pairs = worker_environment(role=hook.role, python=PYTHON, java_home=None, identity=identity, run_id=1,
                                       run_attempt=1, values=values)
            with self.subTest(hook=name):
                self.assertLessEqual({f"{key}={value}" for key, value in values.items()}, set(pairs))

    def test_wrong_units_and_runtime_values_are_rejected(self) -> None:
        cases = [
            ("derive_plan", {"unit_id": "target-a"}), ("policy", {"unit_id": "target-a"}),
            ("verify_build", {"unit_id": "target-a"}), ("build_target", {}), ("verify_target", {"unit_id": None}),
            ("build_target", {"unit_id": "Target"}), ("derive_runtime", {"unit_id": "a--b"}),
            ("verify_runtime", {"unit_id": "x" * 81}), ("run_lane", {"unit_id": "lane-a"}),
            ("run_lane", {"unit_id": "lane-a", "runtime": {}}),
            ("run_lane", {"unit_id": "lane-a", "runtime": {**self.RUNTIME, "PATH": "/tmp"}}),
            ("run_lane", {"unit_id": "lane-a", "runtime": {"E2E_ROW_JSON": "{}"}}),
            ("run_lane", {"unit_id": "lane-a", "runtime": {**self.RUNTIME, "E2E_SCENARIOS": "a\nb"}}),
            ("run_lane", {"unit_id": "lane-a", "runtime": {**self.RUNTIME, "E2E_SCENARIOS": 7}}),
            ("run_lane", {"unit_id": "lane-a", "runtime": [("E2E_ROW_JSON", "{}")]}),
            ("verify_runtime", {"unit_id": "lane-a", "runtime": self.RUNTIME}),
            ("derive_runtime", {"unit_id": "lane-a", "runtime": self.RUNTIME}),
            ("deploy", {}),
        ]
        for hook, arguments in cases:
            with self.subTest(hook=hook, arguments=arguments), self.assertRaises(MbError):
                adapter.hook_values(hook, **arguments)


class FileSetTests(unittest.TestCase):
    def test_inputs(self) -> None:
        config = ci_config()
        config["plan_inputs"] = []
        self.assertEqual(adapter.plan_sources(config), {"inventory": "release/release-matrix.json",
                                                        "scenario-contract": "e2e/scenario-contract.json"})
        self.assertEqual(adapter.hook_inputs("derive_plan", config), ("inventory", "scenario-contract"))
        for hook in ("derive_runtime", "verify_target", "verify_build", "verify_runtime"):
            self.assertEqual(adapter.hook_inputs(hook, config), ("ci-plan.json", "inventory", "scenario-contract"))
        for hook in adapter.CANDIDATE_HOOKS:
            self.assertEqual(adapter.hook_inputs(hook, config), ())
        with self.assertRaises(MbError):
            adapter.hook_inputs("compile", config)

    def test_extra_plan_inputs_are_staged_for_every_protected_hook_under_their_names(self) -> None:
        config = ci_config()
        config["plan_inputs"] = [{"name": "gradle-properties", "path": "gradle.properties"},
                                 {"name": "versions.toml", "path": "gradle/libs.versions.toml"}]
        validate_build_config(config)
        self.assertEqual(list(adapter.plan_sources(config).items()), [
            ("inventory", "release/release-matrix.json"), ("scenario-contract", "e2e/scenario-contract.json"),
            ("gradle-properties", "gradle.properties"), ("versions.toml", "gradle/libs.versions.toml")])
        extra = ("inventory", "scenario-contract", "gradle-properties", "versions.toml")
        self.assertEqual(adapter.hook_inputs("derive_plan", config), extra)
        for hook in ("derive_runtime", "verify_target", "verify_build", "verify_runtime"):
            self.assertEqual(adapter.hook_inputs(hook, config), ("ci-plan.json", *extra))
        for hook in adapter.CANDIDATE_HOOKS:
            self.assertEqual(adapter.hook_inputs(hook, config), ())

    def test_an_extra_plan_input_never_takes_a_name_the_kit_stages(self) -> None:
        self.assertEqual(adapter.RESERVED_INPUT_NAMES, frozenset({"inventory", "scenario-contract", "ci-plan.json"}))
        for name in ("gradle-properties", "gradle.properties", "a", "x" * 80, "plan.json", "inventory.json"):
            with self.subTest(name=name):
                self.assertEqual(adapter.plan_input_name(name, "$.name"), name)
        for name in (*sorted(adapter.RESERVED_INPUT_NAMES), "Inventory", "gradle properties", "gradle/properties",
                     "../inventory", ".hidden", "a--b", "x" * 81, "", None, 7, ["inventory"]):
            with self.subTest(name=name), self.assertRaises(MbError):
                adapter.plan_input_name(name, "$.name")

    def test_outputs_are_exact(self) -> None:
        plan = ci_plan()
        self.assertEqual(adapter.hook_outputs("derive_plan"), ("plan.json",))
        self.assertEqual(adapter.hook_outputs("derive_runtime", plan=plan, unit_id="lane-a"), ("runtime.json",))
        self.assertEqual(adapter.hook_outputs("verify_target", plan=plan, unit_id="target-a"), ("target-a.json",))
        self.assertEqual(adapter.hook_outputs("verify_build", plan=plan), ("target-a.json",))
        self.assertEqual(adapter.hook_outputs("verify_runtime", plan=plan, unit_id="lane-a"), ("lane-a.json",))
        self.assertEqual(adapter.target_outputs(plan, "target-a"),
                         tuple(sorted(output["path"] for output in plan["targets"][0]["outputs"])))

    def test_units_outside_the_plan_and_misuse_are_rejected(self) -> None:
        plan = ci_plan()
        cases = [
            lambda: adapter.hook_outputs("verify_target", plan=plan, unit_id="target-b"),
            lambda: adapter.hook_outputs("verify_target", plan=plan, unit_id="lane-a"),
            lambda: adapter.hook_outputs("verify_runtime", plan=plan, unit_id="target-a"),
            lambda: adapter.hook_outputs("verify_runtime", plan=plan),
            lambda: adapter.hook_outputs("verify_build", plan=plan, unit_id="target-a"),
            lambda: adapter.hook_outputs("verify_build"),
            lambda: adapter.hook_outputs("derive_plan", unit_id="target-a"),
            lambda: adapter.hook_outputs("build_target", plan=plan, unit_id="target-a"),
            lambda: adapter.hook_outputs("policy"),
            lambda: adapter.hook_outputs("verify_build", plan={**plan, "plan_sha256": "0" * 64}),
            lambda: adapter.target_outputs(plan, "lane-a"),
            lambda: adapter.plan_unit(plan, "run_lane", None),
            lambda: adapter.plan_unit(plan, "policy", "target-a"),
            lambda: adapter.plan_unit(plan, "compile", "target-a"),
        ]
        for index, call in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(MbError):
                call()
        self.assertEqual(adapter.plan_unit(plan, "run_lane", "lane-a"), plan["lanes"][0])
        self.assertEqual(adapter.plan_unit(plan, "build_target", "target-a"), plan["targets"][0])
        self.assertIsNone(adapter.plan_unit(plan, "policy", None))


class DerivedPlanTests(unittest.TestCase):
    def test_plan_shape_without_identity_is_accepted(self) -> None:
        document = derived()
        self.assertEqual(adapter.parse_derived_plan(encoded(document)), document)

    def test_real_jar_names_with_spaces_are_planned_outputs(self) -> None:
        document = derived()
        document["targets"][0]["outputs"][0]["path"] = "files/Quick Skin - Fabric - 1.20.1-3.1.0+build.5.jar"
        document["targets"][0]["outputs"][1]["path"] = "harness/BlockPops E2E - NeoForge - 1.21.1-0.0.0.jar"
        self.assertEqual(adapter.parse_derived_plan(encoded(document)), document)

    def test_hostile_documents_are_rejected(self) -> None:
        def change(mutate) -> bytes:
            document = derived()
            mutate(document)
            return encoded(document)

        plan = ci_plan()
        cases = {
            "complete plan": canonical_json(plan),
            "identity": change(lambda d: d.update(identity=plan["identity"])),
            "plan hash": change(lambda d: d.update(plan_sha256=plan["plan_sha256"])),
            "command": change(lambda d: d["targets"][0].update(command=["gradle"])),
            "runner": change(lambda d: d["lanes"][0].update(runs_on="self-hosted")),
            "missing lanes": change(lambda d: d.pop("lanes")),
            "no targets": change(lambda d: d.update(targets=[])),
            "array": encoded([derived()]),
            "traversal": change(lambda d: d["targets"][0]["outputs"][0].update(path="../production.jar")),
            "absolute": change(lambda d: d["targets"][0]["outputs"][0].update(path="/tmp/production.jar")),
            "hidden": change(lambda d: d["targets"][0]["outputs"][0].update(path="staged/.hidden.jar")),
            "double space": change(lambda d: d["targets"][0]["outputs"][0].update(path="staged/a  b.jar")),
            "trailing dot": change(lambda d: d["targets"][0]["outputs"][0].update(path="staged/production.")),
            "envelope name": change(lambda d: d["targets"][0]["outputs"][0].update(path="ci-envelope.json")),
            "case alias": change(lambda d: d["targets"][0]["outputs"][1].update(path="STAGED/lane-a/harness.jar")),
            "missing role": change(lambda d: d["targets"][0]["outputs"].pop()),
            "foreign lane": change(lambda d: d["targets"][0]["outputs"][0].update(lane_id="lane-b")),
            "uncovered target": change(lambda d: d["lanes"][0].update(target_id="target-b")),
            "bool java": change(lambda d: d["targets"][0].update(java=True)),
            "no obligations": change(lambda d: d["lanes"][0].update(obligations=[])),
            "duplicate key": b'{"targets": [], "targets": [], "lanes": []}',
            "not json": b"{",
            "not utf-8": b'{"targets": "\xff"}',
            "nan": b'{"targets": NaN, "lanes": []}',
            "empty": b"",
            "oversized": b" " * (limits.MAX_CI_PLAN_BYTES + 1),
        }
        for label, data in cases.items():
            with self.subTest(case=label), self.assertRaises(MbError):
                adapter.parse_derived_plan(data)

    def test_reserved_unit_ids_cannot_name_a_report(self) -> None:
        for kind, reserved in (("targets", "plan"), ("targets", "ci-validation"), ("lanes", "runtime")):
            document = derived()
            previous = document[kind][0]["id"]
            text = json.dumps(document).replace(f'"{previous}"', f'"{reserved}"')
            with self.subTest(kind=kind, reserved=reserved), self.assertRaises(MbError) as caught:
                adapter.parse_derived_plan(text.encode("utf-8"))
            self.assertIn("reserved validation file", str(caught.exception))


class RuntimeValuesTests(unittest.TestCase):
    VALUES = {"E2E_ROW_JSON": '{"id":"lane-a","loader":"fabric"}', "E2E_SCENARIOS": "smoke,gallery"}

    def test_exactly_the_two_values_are_returned(self) -> None:
        self.assertEqual(adapter.parse_runtime_values(encoded({"values": self.VALUES})), self.VALUES)
        largest = {**self.VALUES, "E2E_ROW_JSON": "x" * limits.MAX_CI_ENV_VALUE_BYTES}
        self.assertEqual(adapter.parse_runtime_values(encoded({"values": largest})), largest)

    def test_anything_else_is_rejected(self) -> None:
        cases = {
            "bare values": encoded(self.VALUES),
            "extra top-level key": encoded({"values": self.VALUES, "command": "sh"}),
            "missing value": encoded({"values": {"E2E_ROW_JSON": "{}"}}),
            "other environment name": encoded({"values": {**self.VALUES, "LD_PRELOAD": "/tmp/x.so"}}),
            "display name": encoded({"values": {**self.VALUES, "GALLIUM_DRIVER": "llvmpipe"}}),
            "number": encoded({"values": {**self.VALUES, "E2E_SCENARIOS": 3}}),
            "object": encoded({"values": {**self.VALUES, "E2E_ROW_JSON": {"id": "lane-a"}}}),
            "empty": encoded({"values": {**self.VALUES, "E2E_SCENARIOS": ""}}),
            "blank": encoded({"values": {**self.VALUES, "E2E_SCENARIOS": "  "}}),
            "newline": encoded({"values": {**self.VALUES, "E2E_SCENARIOS": "smoke\ngallery"}}),
            "nul": encoded({"values": {**self.VALUES, "E2E_ROW_JSON": "{}\u0000"}}),
            "too many bytes": encoded({"values": {**self.VALUES, "E2E_ROW_JSON": "\u00e9" * (limits.MAX_CI_ENV_VALUE_BYTES // 2 + 1)}}),
            "too many characters": encoded({"values": {**self.VALUES, "E2E_ROW_JSON": "x" * (limits.MAX_CI_ENV_VALUE_BYTES + 1)}}),
            "duplicate key": b'{"values": {"E2E_ROW_JSON": "{}", "E2E_ROW_JSON": "{}", "E2E_SCENARIOS": "s"}}',
            "lone surrogate": b'{"values": {"E2E_ROW_JSON": "\\ud800", "E2E_SCENARIOS": "s"}}',
            "array": encoded([self.VALUES]),
            "empty file": b"",
            "oversized": b" " * (limits.MAX_CI_REPORT_BYTES + 1),
        }
        for label, data in cases.items():
            with self.subTest(case=label), self.assertRaises(MbError):
                adapter.parse_runtime_values(data)


class ExportPathGrammarTests(unittest.TestCase):
    def test_real_staged_names_are_export_paths(self) -> None:
        for path in ("files/Quick Skin - Fabric - 1.20.1-3.1.0.jar", "harness/BlockPops E2E - NeoForge - 1.21.1-0.0.0.jar",
                     "sbom/quick-skin.cdx.json", "artifacts.json", "a", "_private/x", "v1.0.0+build.7/mod.jar",
                     "a b c", "-", "+x/-y", "/".join(["d"] * limits.MAX_BUNDLE_PATH_DEPTH), "x" * 128):
            with self.subTest(path=path):
                self.assertTrue(grammar.is_export_path(path))

    def test_unsafe_or_ambiguous_names_are_not(self) -> None:
        for path in ("", "/abs", "a//b", "a/", "../a", "a/../b", "a/./b", ".", "..", ".hidden", "a/.git/config",
                     ".git/config", " a", "a ", "a  b", "a.", "a/b.", ".a", "a/ b", "a\\b", "a\tb", "a\nb", "a\0b",
                     "caf\u00e9.jar", "a:b", "a*b", "a?b", 'a"b', "a|b", "x" * 129,
                     "/".join(["d"] * (limits.MAX_BUNDLE_PATH_DEPTH + 1)), "x" * 100 + "/" + "y" * 100 + "/" + "z" * 100,
                     None, 7, b"a", ["a"]):
            with self.subTest(path=path):
                self.assertFalse(grammar.is_export_path(path))


if __name__ == "__main__":
    unittest.main()
