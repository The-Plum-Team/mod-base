"""``finalize.yml`` (SPEC §5.4): the post-deployment cache roll-forward and its executed shell."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from mod_base.model import limits as lim
from tests.test_workflow_policy import (PROLOGUE, UPLOAD, ShellHarness, callee, outputs, parse_kit_argv,
                                        require_tools, step)

HEAD = "a" * 40
PYTHON_STUB = """
if os.environ.get("STUB_OUTPUTS"):
    target = arguments[arguments.index("--github-output") + 1]
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(os.environ["STUB_OUTPUTS"])
raise SystemExit(int(os.environ.get("STUB_EXIT", "0")))
"""
CACHE = f"mb-cache--mc1.20.1--{HEAD}"
BASELINE = f"mb-baseline--mc1.20.1--{HEAD}--36042781699"
FAMILY_CACHE = f"mb-family-cache--mod-compatibility--mc26.3--{HEAD}"


class FinalizeStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = callee("finalize")["jobs"]

    def test_job_graph_and_matrices(self) -> None:
        self.assertEqual(list(self.jobs), ["refresh", "refresh-family"])
        self.assertNotIn("needs", self.jobs["refresh"])
        self.assertNotIn("if", self.jobs["refresh"])
        self.assertEqual(self.jobs["refresh-family"]["if"], "inputs.families != '[]'")
        self.assertEqual(self.jobs["refresh"]["strategy"]["matrix"], {"key": "${{ fromJSON(inputs.bundle-keys) }}"})
        self.assertEqual(self.jobs["refresh-family"]["strategy"]["matrix"],
                         {"include": "${{ fromJSON(inputs.families) }}"})
        for job in self.jobs.values():
            self.assertEqual(job["timeout-minutes"], "20")
            self.assertEqual(job["strategy"]["fail-fast"], "false")
            self.assertEqual(job["permissions"], {"actions": "read", "contents": "read"})

    def test_steps_and_uploads(self) -> None:
        refresh = self.jobs["refresh"]["steps"]
        self.assertEqual([item["name"] for item in refresh], [
            *PROLOGUE, "Revalidate the promoted bundle", workflow.STEPS["cache_upload"],
            workflow.STEPS["baseline_upload"]])
        family = self.jobs["refresh-family"]["steps"]
        self.assertEqual([item["name"] for item in family], [
            *PROLOGUE, "Revalidate the promoted family bundle", workflow.STEPS["family_cache_upload"]])
        cache = step(refresh, workflow.STEPS["cache_upload"])
        self.assertNotIn("if", cache)
        self.assertEqual((cache["uses"], cache["with"]["path"], cache["with"]["retention-days"]),
                         (UPLOAD, "${{ runner.temp }}/mb/cache", str(lim.RETENTION_DAYS["cache"])))
        baseline = step(refresh, workflow.STEPS["baseline_upload"])
        self.assertEqual(baseline["if"], "steps.refresh.outputs.baseline_name != ''")
        self.assertEqual(baseline["with"]["path"], "${{ runner.temp }}/mb/cache")
        self.assertEqual(step(refresh, "Revalidate the promoted bundle")["env"], {
            "GH_TOKEN": "${{ github.token }}", "KEY": "${{ matrix.key }}", "HEADS": "${{ inputs.heads }}"})
        self.assertEqual(step(family, "Revalidate the promoted family bundle")["env"], {
            "GH_TOKEN": "${{ github.token }}", "FAMILY": "${{ matrix.family }}", "KEY": "${{ matrix.key }}",
            "COVERAGE_SHA": "${{ matrix.coverage_sha }}"})
        family_cache = step(family, workflow.STEPS["family_cache_upload"])
        self.assertEqual(family_cache["if"], "steps.refresh.outputs.available == 'true'")
        self.assertEqual(family_cache["with"]["retention-days"], str(lim.RETENTION_DAYS["family-cache"]))


class FinalizeShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="finalize shell ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.output = self.root / "github_output"
        self.workspace = self.root / "workspace"
        (self.workspace / "mod/site").mkdir(parents=True)
        self.runner_temp = self.root / "runner temp"
        self.jobs = callee("finalize")["jobs"]

    def invoke(self, job: str, name: str, env: dict[str, str]):
        if self.output.exists():
            self.output.unlink()
        shutil.rmtree(self.runner_temp, ignore_errors=True)
        self.runner_temp.mkdir()
        base = {"GITHUB_SHA": HEAD, "GITHUB_REF": "refs/heads/master", "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_OUTPUT": str(self.output), "RUNNER_TEMP": str(self.runner_temp),
                "GITHUB_WORKSPACE": str(self.workspace), "STUB_PYTHON3_SCRIPT": PYTHON_STUB}
        result = self.harness.run(step(self.jobs[job]["steps"], name)["run"], {**base, **env}, cwd=self.workspace,
                                  record_env=("GH_TOKEN", "PYTHONPATH"))
        calls = [record for record in self.harness.records() if record["tool"] == "python3"]
        for call in calls:
            parse_kit_argv(call["argv"][3:])
        return result, outputs(self.output), calls

    def validate(self, **env: str):
        values = {"KIT_SHA": "b" * 40, "BUNDLE_KEYS": '["mc1.20.1","mc26.3"]',
                  "FAMILIES": '[{"coverage_sha":"' + HEAD + '","family":"mod-compatibility","key":"mc26.3"}]',
                  "HEADS": json.dumps({"master": HEAD}), **env}
        return self.invoke("refresh", PROLOGUE[0], values)[0]

    def test_call_input_validation(self) -> None:
        self.assertEqual(self.validate().returncode, 0)
        self.assertEqual(self.validate(FAMILIES="[]", GITHUB_EVENT_NAME="schedule").returncode, 0)
        self.assertEqual(self.validate(HEADS=json.dumps({"master": HEAD, "release/1.20": "c" * 40})).returncode, 0)
        rejected = [
            {"KIT_SHA": "b" * 39}, {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_REF": "refs/tags/v1"},
            {"BUNDLE_KEYS": "[]"}, {"BUNDLE_KEYS": '["mc1.20.1","mc1.20.1"]'}, {"BUNDLE_KEYS": '["MC1"]'},
            {"BUNDLE_KEYS": '["a--b"]'}, {"BUNDLE_KEYS": '"mc1.20.1"'}, {"BUNDLE_KEYS": "not json"},
            {"BUNDLE_KEYS": json.dumps([f"k{index:02d}" for index in range(65)])},
            {"FAMILIES": '[{"family":"mod-compatibility","key":"mc26.3"}]'},
            {"FAMILIES": '[{"coverage_sha":"' + HEAD + '","family":"mod-compatibility","key":"mc9"}]'},
            {"FAMILIES": '[{"coverage_sha":"' + HEAD + '","family":"Bad","key":"mc26.3"}]'},
            {"FAMILIES": '[{"coverage_sha":"x","family":"mod-compatibility","key":"mc26.3"}]'},
            {"FAMILIES": '[{"coverage_sha":"' + HEAD + '","family":"mod-compatibility","key":"mc26.3","x":1}]'},
            {"FAMILIES": json.dumps([{"coverage_sha": HEAD, "family": "f", "key": "mc26.3"}] * 2)},
            {"FAMILIES": "{}"},
            {"HEADS": "{}"}, {"HEADS": json.dumps({"master": "c" * 40})}, {"HEADS": json.dumps({"release": HEAD})},
            {"HEADS": json.dumps({"master": HEAD, "../x": HEAD})}, {"HEADS": json.dumps({"master": HEAD, "a": 1})},
        ]
        for env in rejected:
            with self.subTest(env=env):
                result = self.validate(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid call input", result.stderr)

    def config(self, document: dict) -> None:
        (self.workspace / "mod/site/mod-base.json").write_text(json.dumps(document))

    def refresh(self, lines: str, **env: str):
        values = {"GH_TOKEN": "t", "KEY": "mc1.20.1", "HEADS": json.dumps({"master": HEAD}), "STUB_OUTPUTS": lines,
                  **env}
        return self.invoke("refresh", "Revalidate the promoted bundle", values)

    def test_refresh_passes_on_exactly_the_validated_cache_identity(self) -> None:
        mb = self.runner_temp / "mb"
        self.config({"baseline_archive": {"enabled": True, "retention_days": 45}})
        result, values, calls = self.refresh(f"available=true\ncache_name={CACHE}\nbaseline_name={BASELINE}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"cache_name": CACHE, "baseline_name": BASELINE, "baseline_retention_days": "45"})
        self.assertEqual([call["argv"][3:] for call in calls], [[
            "refresh", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--input",
            f"{mb}/cache", "--github-output", f"{mb}/refresh.out"]])
        self.assertEqual(calls[0]["env"], {"GH_TOKEN": "t", "PYTHONPATH": str(self.workspace / "kit/src")})
        result, values, _ = self.refresh(f"available=true\ncache_name={CACHE}\n")
        self.assertEqual((result.returncode, values), (0, {"cache_name": CACHE}), result.stderr)
        second = "c" * 40
        result, values, _ = self.refresh(f"available=true\ncache_name=mb-cache--mc1.20.1--{second}\n",
                                         HEADS=json.dumps({"master": HEAD, "release/1.20": second}))
        self.assertEqual(result.returncode, 0, "an enrolled branch head is a published head")
        self.config({"baseline_archive": {"enabled": False}})
        result, values, _ = self.refresh(f"available=true\ncache_name={CACHE}\n")
        self.assertEqual((result.returncode, values), (0, {"cache_name": CACHE}), result.stderr)

    def test_refresh_outputs_that_are_not_a_cache_identity_fail_the_leg(self) -> None:
        self.config({"baseline_archive": {"enabled": True, "retention_days": 90}})
        cases = {
            "unavailable": f"available=false\ncache_name={CACHE}\n",
            "no availability": f"cache_name={CACHE}\n",
            "no cache name": "available=true\n",
            "empty cache name": "available=true\ncache_name=\n",
            "another key": f"available=true\ncache_name=mb-cache--mc26.3--{HEAD}\n",
            "another kind": f"available=true\ncache_name=mb-family-cache--f--mc1.20.1--{HEAD}\n",
            "unpublished head": "available=true\ncache_name=mb-cache--mc1.20.1--" + "d" * 40 + "\n",
            "suffix": f"available=true\ncache_name={CACHE}-x\n",
            "duplicate": f"available=true\ncache_name={CACHE}\ncache_name={CACHE}\n",
            "unknown output": f"available=true\ncache_name={CACHE}\nname=x\n",
            "not an output line": f"available=true\ncache_name={CACHE}\ngarbage\n",
            "baseline of another key": f"available=true\ncache_name={CACHE}\nbaseline_name=mb-baseline--mc26.3--{HEAD}--1\n",
            "baseline without run": f"available=true\ncache_name={CACHE}\nbaseline_name=mb-baseline--mc1.20.1--{HEAD}\n",
            "baseline run zero": f"available=true\ncache_name={CACHE}\nbaseline_name=mb-baseline--mc1.20.1--{HEAD}--0\n",
        }
        for label, lines in cases.items():
            with self.subTest(case=label):
                result, values, _ = self.refresh(lines)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Evidence cache refresh", result.stderr)
                self.assertNotIn("cache_name", values)
        self.config({"baseline_archive": {"enabled": False}})
        result, values, _ = self.refresh(f"available=true\ncache_name={CACHE}\nbaseline_name={BASELINE}\n")
        self.assertNotEqual(result.returncode, 0, "a baseline requires the configured archive")
        self.assertNotIn("baseline_name", values)
        for retention in (0, 91, "90", None):
            with self.subTest(retention=retention):
                self.config({"baseline_archive": {"enabled": True, "retention_days": retention}})
                result, values, _ = self.refresh(f"available=true\ncache_name={CACHE}\nbaseline_name={BASELINE}\n")
                if retention == "90":
                    self.assertEqual(result.returncode, 0, "a JSON string 90 is still the digits 90")
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("baseline_retention_days", values)
        self.assertEqual(lim.MAX_BASELINE_RETENTION_DAYS, 90)

    def test_refresh_failures_and_malformed_keys_stop_the_leg(self) -> None:
        self.config({"baseline_archive": {"enabled": False}})
        result, values, _ = self.refresh("", STUB_EXIT="2")
        self.assertEqual((result.returncode, values), (2, {}))
        result, _, calls = self.refresh(f"available=true\ncache_name={CACHE}\n", KEY="../x")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])

    def family_refresh(self, lines: str, **env: str):
        values = {"GH_TOKEN": "t", "KEY": "mc26.3", "FAMILY": "mod-compatibility", "COVERAGE_SHA": HEAD,
                  "STUB_OUTPUTS": lines, **env}
        return self.invoke("refresh-family", "Revalidate the promoted family bundle", values)

    def test_family_refresh_arguments_and_availability(self) -> None:
        mb = self.runner_temp / "mb"
        result, values, calls = self.family_refresh(f"available=true\ncache_name={FAMILY_CACHE}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"available": "true", "cache_name": FAMILY_CACHE})
        self.assertEqual([call["argv"][3:] for call in calls], [[
            "refresh", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc26.3", "--family",
            "mod-compatibility", "--input", f"{mb}/cache", "--github-output", f"{mb}/refresh.out"]])
        result, values, _ = self.family_refresh("available=false\ncache_name=\n")
        self.assertEqual((result.returncode, values), (0, {"available": "false"}), result.stderr)
        self.assertIn("no family cache is rolled forward", result.stdout)
        for lines in (f"available=false\ncache_name={FAMILY_CACHE}\n", "cache_name=\n",
                      f"available=true\ncache_name=mb-family-cache--mod-compatibility--mc26.3--{'c' * 40}\n",
                      f"available=true\ncache_name=mb-family-cache--other--mc26.3--{HEAD}\n",
                      f"available=true\ncache_name={FAMILY_CACHE}\nbaseline_name={BASELINE}\n"):
            with self.subTest(lines=lines):
                result, values, _ = self.family_refresh(lines)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(values, {})
        for env in ({"FAMILY": "Mod"}, {"KEY": "MC"}, {"COVERAGE_SHA": "x"}):
            with self.subTest(env=env):
                result, _, calls = self.family_refresh("available=false\n", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
