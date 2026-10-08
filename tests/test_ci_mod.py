"""The synthetic mod's hooks, run as plain processes: the adapter contract end to end, in files."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mod_base import runtime
from mod_base.build_ci import adapter, planning
from mod_base.build_ci.config import BUILD_CONFIG_PATH, load_build_config
from mod_base.build_ci.protocol import validate_plan
from mod_base.model import limits
from mod_base.model.canonical import canonical_json, strict_loads
from tests import ci_mod_harness as h

PNG = b"\x89PNG\r\n\x1a\n"


def flip(path: Path, position: int) -> None:
    data = bytearray(path.read_bytes())
    data[position] ^= 0x01
    path.write_bytes(bytes(data))


class FixtureTests(unittest.TestCase):
    def test_the_committed_build_config_lists_the_scripts_as_they_are(self) -> None:
        self.assertEqual((h.MOD / BUILD_CONFIG_PATH).read_bytes(), h.pretty(h.build_config()),
                         "run tests.ci_mod_harness.write_config() after editing a fixture script")
        config = load_build_config(h.MOD, repository=h.REPOSITORY)
        self.assertEqual([file.path for file in config.files], sorted(
            config.data["adapter"][key] for key in ("path", "dispatcher", "policy")))

    def test_the_mod_is_a_complete_repository_for_every_ci_command(self) -> None:
        invocation = runtime.build_invocation(h.MOD, h.MOD / "site" / "mod-base.json", h.environment())
        self.assertEqual((invocation.repository, invocation.config.canonical_branch), (h.REPOSITORY, h.BRANCH))
        sources = adapter.plan_sources(h.build_config())
        self.assertEqual(list(sources), ["inventory", "scenario-contract", h.PROPERTIES_INPUT])
        for path in sources.values():
            self.assertTrue((h.MOD / path).is_file(), path)

    def test_the_dispatcher_knows_exactly_the_eight_hooks(self) -> None:
        source = (h.MOD / "scripts/ci/mod_base_build_adapter.py").read_text(encoding="utf-8")
        assignments = {node.targets[0].id: ast.literal_eval(node.value) for node in ast.parse(source).body
                       if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                       and node.targets[0].id in {"HOOKS", "BUILD_ADAPTER_API"}}
        self.assertEqual(assignments, {"HOOKS": tuple(adapter.HOOKS), "BUILD_ADAPTER_API": 1})

    def test_a_request_outside_the_contract_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            box = h.Sandbox(Path(directory))
            dispatcher = str(box.checkout["validator"] / box.config.data["adapter"]["dispatcher"])
            for arguments in ([], ["--hook"], ["--hook", "deploy"], ["--hook", "derive_plan", "extra"], ["derive_plan"]):
                process = subprocess.run([sys.executable, "-I", "-B", dispatcher, *arguments], capture_output=True,
                                         env={"HOME": str(box.home["validator"])}, check=False)
                with self.subTest(arguments=arguments):
                    self.assertEqual(process.returncode, 2)
            self.assertEqual(h.files(box.validation), set())
            box.derive_plan()
            for hook, unit in (("build_target", "9.9.9"), ("verify_target", "9.9.9"), ("derive_runtime", "fabric-9.9.9")):
                with self.subTest(hook=hook, unit=unit):
                    self.assertEqual(box.run(hook, unit_id=unit).returncode, 1)


class PipelineTests(unittest.TestCase):
    """One complete generation: plan, policy, two targets, the assembled Build and three lanes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        box = cls.box = h.Sandbox(Path(cls.directory.name))
        box.stage_inputs()
        cls.derived = box.run("derive_plan")
        cls.derived_files = h.files(box.validation)
        cls.derived_bytes = (box.validation / adapter.PLAN_OUTPUT).read_bytes()
        shutil.rmtree(box.validation)
        cls.plan = box.derive_plan()
        cls.policy = box.run("policy")
        cls.policy_files = h.files(box.export)
        cls.exports, cls.target_checks, cls.reports = {}, {}, {}
        for target in h.TARGETS:
            process = box.run("build_target", unit_id=target)
            cls.exports[target] = {"process": process, "files": {
                name: (box.export / name).read_bytes() for name in h.files(box.export)}}
            box.seal(box.export, box.sealed_build)
            cls.target_checks[target] = cls.verify(box, "verify_target", target)
        cls.build_check = cls.verify(box, "verify_build", None)
        cls.runtime, cls.lane_checks = {}, {}
        for lane in h.LANES:
            process = box.run("derive_runtime", unit_id=lane)
            cls.runtime[lane] = {"process": process, "files": h.files(box.validation),
                                 "bytes": (box.validation / adapter.RUNTIME_OUTPUT).read_bytes()}
            shutil.rmtree(box.validation)
            box.run_lane(lane)
            cls.lane_checks[lane] = cls.verify(box, "verify_runtime", lane)

    @staticmethod
    def verify(box: h.Sandbox, hook: str, unit: str | None) -> dict:
        process = box.run(hook, unit_id=unit)
        observed = {"process": process, "files": {name: (box.validation / name).read_bytes()
                                                  for name in h.files(box.validation)}}
        shutil.rmtree(box.validation, ignore_errors=True)
        return observed

    def test_derive_plan_writes_one_document_that_becomes_a_valid_plan(self) -> None:
        self.assertEqual(self.derived.returncode, 0, self.derived.stdout)
        self.assertEqual(self.derived_files, set(adapter.hook_outputs("derive_plan")))
        units = adapter.parse_derived_plan(self.derived_bytes)
        self.assertEqual(set(units), {"targets", "lanes"})
        validate_plan(self.plan)
        self.assertEqual({"targets": self.plan["targets"], "lanes": self.plan["lanes"]}, units)
        self.assertEqual(planning.matrices(self.plan), {"targets": list(h.TARGETS), "lanes": list(h.LANES)})
        self.assertEqual([lane["target_id"] for lane in self.plan["lanes"]], ["1.20.1", "1.20.1", "1.21.1"])
        inventory = (h.MOD / "release" / "inventory.json").read_bytes()
        self.assertEqual(self.plan["identity"]["inventory_sha256"], hashlib.sha256(inventory).hexdigest())
        properties = (h.MOD / "gradle.properties").read_bytes()
        self.assertEqual(self.plan["plan_inputs"], [{"name": h.PROPERTIES_INPUT,
                                                     "sha256": hashlib.sha256(properties).hexdigest()}])
        self.assertEqual(self.plan["profile"], "quick-skin")

    def test_planned_outputs_carry_the_real_mods_file_names(self) -> None:
        paths = [output["path"] for target in self.plan["targets"] for output in target["outputs"]]
        self.assertIn("files/Synthetic Mod - Fabric - 1.20.1-1.0.0.jar", paths)
        self.assertIn("harness/Synthetic Mod E2E - Forge - 1.20.1-0.0.0.jar", paths)
        self.assertEqual(len(paths), 15)
        self.assertEqual({output["role"] for target in self.plan["targets"] for output in target["outputs"]},
                         {"production", "harness", "sbom", "native-report", "build-log"})

    def test_policy_passes_and_writes_nothing(self) -> None:
        self.assertEqual(self.policy.returncode, 0, self.policy.stdout)
        self.assertEqual(self.policy_files, set())
        self.assertIn(b"4 checks, 0 failures", self.policy.stdout)

    def test_build_target_leaves_exactly_the_planned_outputs(self) -> None:
        for target in h.TARGETS:
            observed = self.exports[target]
            with self.subTest(target=target):
                self.assertEqual(observed["process"].returncode, 0, observed["process"].stdout)
                self.assertEqual(tuple(sorted(observed["files"])), adapter.target_outputs(self.plan, target))
                self.assertTrue(all(observed["files"].values()))
        self.assertEqual(h.files(self.box.sealed_build), {output["path"] for target in self.plan["targets"]
                                                          for output in target["outputs"]})

    def test_verification_hooks_accept_the_sealed_exports_and_write_exactly_their_reports(self) -> None:
        checks = {("verify_target", target): self.target_checks[target] for target in h.TARGETS}
        checks[("verify_build", None)] = self.build_check
        checks.update({("verify_runtime", lane): self.lane_checks[lane] for lane in h.LANES})
        for (hook, unit), observed in checks.items():
            with self.subTest(hook=hook, unit=unit):
                self.assertEqual(observed["process"].returncode, 0, observed["process"].stdout)
                self.assertEqual(sorted(observed["files"]),
                                 sorted(adapter.hook_outputs(hook, plan=self.plan, unit_id=unit)))
                for name, data in observed["files"].items():
                    report = strict_loads(data, label=name, max_bytes=limits.MAX_CI_REPORT_BYTES)
                    self.assertIs(type(report), dict)
                    self.assertEqual(data, canonical_json(report), "a report is a canonical JSON object")
                    self.assertEqual((report["hook"], report["unit"]), (hook, name.removesuffix(".json")))
                    units = self.plan["lanes" if hook == "verify_runtime" else "targets"]
                    self.assertEqual(report["native_contract_sha256"], next(
                        item["native_contract_sha256"] for item in units if item["id"] == report["unit"]))
        self.assertEqual(sorted(self.build_check["files"]), ["1.20.1.json", "1.21.1.json"])

    def test_verify_target_reports_the_rehashed_planned_files(self) -> None:
        for target in h.TARGETS:
            report = json.loads(self.target_checks[target]["files"][f"{target}.json"])
            expected = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                        for path, data in sorted(self.exports[target]["files"].items())]
            with self.subTest(target=target):
                self.assertEqual(report["files"], expected)

    def test_derive_runtime_returns_exactly_the_allowed_values(self) -> None:
        for lane in h.LANES:
            observed = self.runtime[lane]
            with self.subTest(lane=lane):
                self.assertEqual(observed["process"].returncode, 0, observed["process"].stdout)
                self.assertEqual(observed["files"], set(adapter.hook_outputs("derive_runtime", plan=self.plan, unit_id=lane)))
                values = adapter.parse_runtime_values(observed["bytes"])
                self.assertEqual(set(values), set(adapter.RUNTIME_VALUES))
                row = json.loads(values["E2E_ROW_JSON"])
                self.assertEqual(row["id"], lane)
                self.assertIn(row["production"], adapter.target_outputs(self.plan, row["minecraft"]))
        self.assertEqual(adapter.parse_runtime_values(self.runtime["forge-1.20.1"]["bytes"])["E2E_SCENARIOS"], "smoke")
        self.assertEqual(adapter.parse_runtime_values(self.runtime["fabric-1.21.1"]["bytes"])["E2E_SCENARIOS"],
                         "smoke,gallery")

    def test_run_lane_writes_a_result_report_a_log_and_small_screenshots(self) -> None:
        for lane in self.plan["lanes"]:
            root = self.box.sealed_runtime / "lanes" / lane["id"]
            names = h.files(root)
            result = json.loads((root / "result.json").read_bytes())
            with self.subTest(lane=lane["id"]):
                self.assertEqual([check["obligation"] for check in result["checks"]], lane["obligations"])
                self.assertEqual(names, {"result.json", "logs/client.log", *(check["screenshot"] for check in result["checks"])})
                self.assertTrue((root / "logs" / "client.log").read_bytes())
                for check in result["checks"]:
                    image = (root / check["screenshot"]).read_bytes()
                    self.assertTrue(image.startswith(PNG) and len(image) < 200, check["screenshot"])
        self.assertEqual(len(h.files(self.box.sealed_runtime)), 14)  # three reports, three logs, eight screenshots


class TamperTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.box = h.Sandbox(Path(directory.name))
        self.box.derive_plan()
        self.box.build("1.21.1")

    def rejected(self, hook: str, unit: str | None) -> None:
        process = self.box.run(hook, unit_id=unit)
        self.assertEqual(process.returncode, 1, process.stdout)
        self.assertIn(b" rejected: ", process.stdout)
        self.assertNotIn(b"Traceback", process.stdout)
        self.assertEqual(h.files(self.box.validation), set(), "a rejecting hook leaves no report")

    def accepted(self, hook: str, unit: str | None) -> None:
        process = self.box.run(hook, unit_id=unit)
        self.assertEqual(process.returncode, 0, process.stdout)
        shutil.rmtree(self.box.validation)

    def test_one_changed_byte_in_any_sealed_build_file_is_rejected(self) -> None:
        self.accepted("verify_target", "1.21.1")
        for name in adapter.target_outputs(self.box.plan, "1.21.1"):
            path = self.box.sealed_build / name
            for position in (0, path.stat().st_size // 2, path.stat().st_size - 1):
                flip(path, position)
                with self.subTest(file=name, position=position):
                    self.rejected("verify_target", "1.21.1")
                flip(path, position)
        self.accepted("verify_target", "1.21.1")

    def test_a_missing_an_unplanned_or_a_linked_file_is_rejected_but_the_kit_envelope_is_not(self) -> None:
        production = self.box.sealed_build / "files" / "Synthetic Mod - Fabric - 1.21.1-1.0.0.jar"
        (self.box.sealed_build / "ci-envelope.json").write_bytes(b"{}\n")
        self.accepted("verify_target", "1.21.1")
        (self.box.sealed_build / "files" / "unplanned.jar").write_bytes(production.read_bytes())
        self.rejected("verify_target", "1.21.1")
        (self.box.sealed_build / "files" / "unplanned.jar").unlink()
        kept = production.read_bytes()
        production.unlink()
        self.rejected("verify_target", "1.21.1")
        (self.box.sealed_build / "elsewhere.jar").write_bytes(kept)
        production.symlink_to(self.box.sealed_build / "elsewhere.jar")
        self.rejected("verify_target", "1.21.1")
        production.unlink()
        (self.box.sealed_build / "elsewhere.jar").rename(production)
        self.accepted("verify_target", "1.21.1")

    def test_the_assembled_build_needs_every_target(self) -> None:
        self.rejected("verify_build", None)
        self.box.build("1.20.1")
        self.accepted("verify_build", None)
        flip(self.box.sealed_build / "sbom" / "forge-1.20.1.cdx.json", 5)
        self.rejected("verify_build", None)
        self.accepted("verify_target", "1.21.1")

    def test_one_changed_byte_in_any_runtime_file_is_rejected(self) -> None:
        lane = "fabric-1.21.1"
        self.box.run_lane(lane)
        self.accepted("verify_runtime", lane)
        root = self.box.sealed_runtime / "lanes" / lane
        for name in sorted(h.files(root)):
            path = root / name
            for position in (0, path.stat().st_size // 2, path.stat().st_size - 1):
                flip(path, position)
                with self.subTest(file=name, position=position):
                    self.rejected("verify_runtime", lane)
                flip(path, position)
        (root / "screenshots" / "unreported.png").write_bytes((root / "screenshots" / "smoke-title.png").read_bytes())
        self.rejected("verify_runtime", lane)
        (root / "screenshots" / "unreported.png").unlink()
        self.accepted("verify_runtime", lane)
        flip(self.box.sealed_build / "files" / "Synthetic Mod - Fabric - 1.21.1-1.0.0.jar", 40)
        self.rejected("verify_runtime", lane)

    def test_a_lane_refuses_a_build_of_another_commit_and_values_of_another_lane(self) -> None:
        lane = "fabric-1.21.1"
        values = adapter.parse_runtime_values(self.derive(lane))
        bundle = self.box.checkout["candidate"] / self.box.config.data["bundle"]["path"]
        shutil.copytree(self.box.sealed_build, bundle)
        unknown = {**values, "E2E_SCENARIOS": "smoke,unknown"}
        self.assertEqual(self.box.run("run_lane", unit_id=lane, runtime=unknown).returncode, 1)
        self.assertEqual(self.box.run("run_lane", unit_id="forge-1.20.1", runtime=values).returncode, 1)
        self.assertEqual(h.files(self.box.export), set())
        self.box.plan["identity"]["tested_sha"] = "9" * 40
        self.assertEqual(self.box.run("run_lane", unit_id=lane, runtime=values).returncode, 1)
        self.assertEqual(h.files(self.box.export), set())

    def derive(self, lane: str) -> bytes:
        process = self.box.run("derive_runtime", unit_id=lane)
        self.assertEqual(process.returncode, 0, process.stdout)
        data = (self.box.validation / adapter.RUNTIME_OUTPUT).read_bytes()
        shutil.rmtree(self.box.validation)
        return data

    def test_protected_hooks_refuse_inputs_the_plan_does_not_bind(self) -> None:
        # Each candidate file in turn: the two the identity binds and the extra one of ``plan_inputs``.
        self.assertEqual(h.files(self.box.inputs), {*adapter.hook_inputs("verify_build", self.box.config.data)})
        self.box.build("1.20.1")
        for name in adapter.hook_inputs("derive_plan", self.box.config.data):
            staged = self.box.inputs / name
            original = staged.read_bytes()
            staged.write_bytes(original + b"\n")
            for hook, unit in (("verify_target", "1.21.1"), ("verify_build", None), ("derive_runtime", "fabric-1.21.1"),
                               ("verify_runtime", "fabric-1.21.1")):
                with self.subTest(changed=name, hook=hook):
                    self.rejected(hook, unit)
            staged.write_bytes(original)
        self.accepted("verify_build", None)


class PlanInputTests(unittest.TestCase):
    """The mod version comes from ``gradle.properties``, the one extra plan input of the fixture."""

    def sandbox(self, properties: bytes | None = None) -> h.Sandbox:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        candidate = h.materialize(Path(directory.name) / "candidate")
        if properties is not None:
            (candidate / "gradle.properties").write_bytes(properties)
        return h.Sandbox(Path(directory.name), candidate=candidate)

    def test_the_extra_file_decides_the_jar_names_and_is_bound_by_the_plan(self) -> None:
        released, bumped = self.sandbox(), self.sandbox(b"mod_version=2.3.4\n")
        plans = [box.derive_plan() for box in (released, bumped)]
        paths = [[output["path"] for target in plan["targets"] for output in target["outputs"]] for plan in plans]
        self.assertIn("files/Synthetic Mod - Fabric - 1.20.1-1.0.0.jar", paths[0])
        self.assertIn("files/Synthetic Mod - Fabric - 1.20.1-2.3.4.jar", paths[1])
        self.assertFalse(any("1.0.0" in path for path in paths[1]))
        # Nothing but the extra file differs: the identity is the same, and the plan is another.
        self.assertEqual(plans[0]["identity"], plans[1]["identity"])
        self.assertEqual(plans[1]["plan_inputs"], [{"name": h.PROPERTIES_INPUT,
                                                    "sha256": hashlib.sha256(b"mod_version=2.3.4\n").hexdigest()}])
        self.assertNotEqual(plans[0]["plan_sha256"], plans[1]["plan_sha256"])
        # The candidate hook reads the same file from its own checkout and builds what was planned.
        bumped.build("1.21.1")
        self.assertIn("files/Synthetic Mod - Fabric - 1.21.1-2.3.4.jar", h.files(bumped.sealed_build))
        process = bumped.run("verify_target", unit_id="1.21.1")
        self.assertEqual(process.returncode, 0, process.stdout)

    def test_derive_plan_needs_the_extra_file_with_one_version_in_it(self) -> None:
        box = self.sandbox()
        box.stage_inputs()
        staged = box.inputs / h.PROPERTIES_INPUT
        staged.unlink()
        self.assertEqual(box.run("derive_plan").returncode, 1)
        for properties in (b"# no version\n", b"mod_version=\n", b"mod_version=one\n", b"mod_version\n",
                           b"mod_version=1.0.0\nmod_version=1.0.1\n", b"\xff\n"):
            staged.write_bytes(properties)
            with self.subTest(properties=properties):
                process = box.run("derive_plan")
                self.assertEqual(process.returncode, 1, process.stdout)
                self.assertNotIn(b"Traceback", process.stdout)
                self.assertEqual(h.files(box.validation), set())
        staged.write_bytes(b"other=1\nmod_version = 4.5.6 \n")
        self.assertEqual(box.run("derive_plan").returncode, 1)  # A padded key is malformed, not trimmed.
        staged.write_bytes(b"other=1\nmod_version= 4.5.6 \n")
        self.assertEqual(box.run("derive_plan").returncode, 0)


class DeterminismTests(unittest.TestCase):
    def test_the_same_inputs_give_the_same_plan_and_the_same_build_bytes(self) -> None:
        observed = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as directory:
                box = h.Sandbox(Path(directory))
                plan = box.derive_plan()
                box.build("1.20.1")
                observed.append((plan, {name: (box.sealed_build / name).read_bytes()
                                        for name in sorted(h.files(box.sealed_build))}))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(len(observed[0][1]), 10)


class FaultTests(unittest.TestCase):
    """Every hook fails on purpose when the candidate's inventory says so, and only then."""

    UNITS = {"build_target": "1.20.1", "verify_target": "1.20.1", "derive_runtime": "fabric-1.20.1",
             "run_lane": "fabric-1.20.1", "verify_runtime": "fabric-1.20.1"}
    RUNTIME = {"E2E_ROW_JSON": '{"id":"fabric-1.20.1"}', "E2E_SCENARIOS": "smoke"}

    def sandbox(self, faults: list[dict]) -> h.Sandbox:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        candidate = h.materialize(Path(directory.name) / "candidate", faults=faults)
        return h.Sandbox(Path(directory.name), candidate=candidate)

    def test_fail_makes_each_of_the_eight_hooks_exit_non_zero_without_output(self) -> None:
        box = self.sandbox([{"hook": name, "unit": self.UNITS.get(name), "mode": "fail"} for name in adapter.HOOKS])
        box.stage_inputs()
        for name in adapter.HOOKS:
            process = box.run(name, unit_id=self.UNITS.get(name), runtime=self.RUNTIME if name == "run_lane" else None)
            with self.subTest(hook=name):
                self.assertEqual(process.returncode, 1, process.stdout)
                self.assertIn(b"the release inventory requests this failure", process.stdout)
        self.assertEqual(h.files(box.validation) | h.files(box.export), set())

    def test_a_fault_selects_one_unit_and_changes_nothing_else(self) -> None:
        box = self.sandbox([{"hook": "build_target", "unit": "1.21.1", "mode": "fail"}])
        clean = h.Sandbox(Path(self.enterContext(tempfile.TemporaryDirectory())))
        plan = box.derive_plan()
        self.assertEqual({key: plan[key] for key in ("targets", "lanes")},
                         {key: clean.derive_plan()[key] for key in ("targets", "lanes")})
        self.assertNotEqual(plan["identity"]["inventory_sha256"], clean.plan["identity"]["inventory_sha256"])
        box.build("1.20.1")
        self.assertEqual(box.run("build_target", unit_id="1.21.1").returncode, 1)
        self.assertEqual(h.files(box.export), set())

    def test_missing_and_extra_break_the_exact_file_set(self) -> None:
        for mode, hook, unit in (("missing", "build_target", "1.21.1"), ("extra", "build_target", "1.21.1"),
                                 ("missing", "derive_plan", None), ("extra", "derive_plan", None)):
            box = self.sandbox([{"hook": hook, "unit": unit, "mode": mode}])
            with self.subTest(mode=mode, hook=hook):
                if hook == "derive_plan":
                    box.stage_inputs()
                    process, written, expected = box.run(hook), h.files(box.validation), {adapter.PLAN_OUTPUT}
                else:
                    box.derive_plan()
                    process, written = box.run(hook, unit_id=unit), h.files(box.export)
                    expected = set(adapter.target_outputs(box.plan, unit))
                self.assertEqual(process.returncode, 0, process.stdout)
                self.assertEqual(len(written ^ expected), 1)
                self.assertEqual(written - expected, {"unplanned.txt"} if mode == "extra" else set())

    def test_hang_never_finishes(self) -> None:
        box = self.sandbox([{"hook": "policy", "unit": None, "mode": "hang"}])
        with self.assertRaises(subprocess.TimeoutExpired):
            box.run("policy", timeout=1.5)

    def test_orphan_leaves_a_process_behind(self) -> None:
        box = self.sandbox([{"hook": "policy", "unit": None, "mode": "orphan"}])
        process = box.run("policy")
        self.assertEqual(process.returncode, 0, process.stdout)
        pid = int(process.stdout.split(b"orphan pid=")[1].split()[0])
        try:
            os.kill(pid, 0)
        finally:
            os.kill(pid, signal.SIGKILL)

    def test_an_inventory_with_a_malformed_fault_fails_every_hook(self) -> None:
        box = self.sandbox([{"hook": "compile", "unit": None, "mode": "fail"}])
        box.stage_inputs()
        self.assertEqual(box.run("derive_plan").returncode, 1)
        self.assertEqual(box.run("policy").returncode, 1)
        self.assertEqual(h.files(box.validation) | h.files(box.export), set())


if __name__ == "__main__":
    unittest.main()
