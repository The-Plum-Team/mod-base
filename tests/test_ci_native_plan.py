"""The complete plan of each mod, built from its native fixtures through ``planning.build_plan``.

``tests/fixtures/ci_native`` holds what Block Pops and Quick Skin derive and stage at their reviewed
commits. This module writes from it what each mod's protected ``derive_plan`` hook has to write
(every target and lane, the real staged JAR names, the manifest of every target and Quick Skin's
SBOM of every target), turns that into the protected plan exactly as a job does, and validates the
plan and a matching Build envelope of every target partition and of the assembled Build.

So a plan rule that one of the two mods cannot satisfy fails here, with the mod's own names and
counts, before either adapter exists.
"""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import mod_base
from mod_base.build_ci import adapter, planning
from mod_base.build_ci.config import BUILD_CONFIG_PATH, BuildConfig, load_build_config
from mod_base.build_ci.exports import validate_target_partitions
from mod_base.build_ci.protocol import BUILD_GRAPH_VERSION, validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests import ci_native
from tests.helpers import ci_config, ci_envelope, ci_run_descriptor
from tests.test_ci_native import COUNTS, STAGING

BRANCH = "master"
#: The protected adapter closure of the fixture config; its bytes only have to match their hashes.
SOURCES = {path: f"# {path}\n".encode("utf-8") for path in (
    "scripts/ci/mod_base_build_adapter.py", "scripts/ci/mod_base_build_dispatch.py", "scripts/ci/pr_gate.py")}
#: Where an adapter stages a file every target writes under the same native name.
PER_TARGET = "targets/{target}/{name}"


def beside(profile: str) -> dict[str, str]:
    """``{native name: role}`` of what a target partition holds beside its JARs: the manifest is
    the target's native report, and Quick Skin's SBOM its SBOM."""

    outputs = ci_native.load(profile, "outputs.json")
    names = {name for unit in outputs["units"] for name in unit["staged"]
             if not name.startswith(("files/", "harness/"))}
    return {name: "native-report" if name == outputs["manifest"] else "sbom" for name in sorted(names)}


def derived(profile: str, *, per_target: str = PER_TARGET) -> dict[str, Any]:
    """What the mod's ``derive_plan`` writes: a lane per artifact node (a runtime row id holds
    ``--``), its two JARs under their staged names, and the target's own files under a
    per-target path."""

    native = ci_native.load(profile, "targets.json")
    rows = {lane["artifact_node"]: lane for lane in native["lanes"]}
    targets = []
    for target in native["targets"]:
        outputs = []
        for node in target["lanes"]:
            production, harness = ci_native.staged_names(rows[node])
            outputs += [{"path": production, "lane_id": node, "role": "production"},
                        {"path": harness, "lane_id": node, "role": "harness"}]
        outputs += [{"path": per_target.format(target=target["id"], name=name), "lane_id": None, "role": role}
                    for name, role in beside(profile).items()]
        (java,) = {rows[node]["java"] for node in target["lanes"]}
        targets.append({"id": target["id"], "java": java, "outputs": outputs, "native_contract_sha256": canonical_sha256(
            {"target": target["id"], "lanes": [rows[node] for node in target["lanes"]]})})
    lanes = [{"id": lane["artifact_node"], "target_id": lane["target"],
              "native_contract_sha256": canonical_sha256({"lane": lane, "scenarios": native["pr_scenarios"]}),
              "obligations": [f"scenario/{name}" for name in native["pr_scenarios"]]} for lane in native["lanes"]]
    return {"targets": targets, "lanes": lanes}


def subject(profile: str) -> dict[str, Any]:
    """A protected push of the reviewed commit, as ``ci subject`` authenticates it."""

    entry = ci_native.manifest()["profiles"][profile]
    repository, commit, workflow = entry["repository"], entry["commit"], CI_CALLER_WORKFLOWS["build"]
    return {"repository": repository, "source_repository": repository, "pr_number": 0,
            "head_sha": commit, "head_branch": BRANCH, "base_sha": commit, "base_branch": BRANCH,
            "controller_sha": commit, "controller_workflow": workflow,
            "controller_ref": grammar.workflow_ref(repository, workflow, BRANCH),
            "kit": {"repository": mod_base.KIT_REPOSITORY, "sha": "3" * 40, "version": mod_base.__version__,
                    "tree_digest": "sha256:" + "4" * 64},
            "tested_sha": commit, "tested_tree": entry["tree"], "tested_parents": ["b" * 40],
            "graph_version": BUILD_GRAPH_VERSION}


def protected_config(root: Path, profile: str) -> BuildConfig:
    """The mod's protected Build config, loaded from a real protected checkout below ``root``: it
    names the native paths of the two candidate files at the reviewed commit."""

    entry = ci_native.manifest()["profiles"][profile]
    document = ci_config()
    document.update(repository=entry["repository"], profile=profile)
    document["adapter"]["files"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()}
                                    for path, data in sorted(SOURCES.items())]
    document["inventory"]["path"] = entry["native"]["release-matrix.json"]["path"]
    document["scenario_contract"]["path"] = entry["native"]["scenario-contract.json"]["path"]
    for path, data in {BUILD_CONFIG_PATH: json.dumps(document).encode("utf-8"), **SOURCES}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    return load_build_config(root, repository=entry["repository"])


def build_plan(root: Path, profile: str, document: dict[str, Any] | None = None) -> dict[str, Any]:
    return planning.build_plan(
        subject=subject(profile), config=protected_config(root, profile),
        inventory=ci_native.read(profile, "release-matrix.json"),
        scenario_contract=ci_native.read(profile, "scenario-contract.json"),
        derived=canonical_json(derived(profile) if document is None else document))


class NativePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.plans = {profile: build_plan(cls.root / profile, profile) for profile in ci_native.profiles()}

    def sized(self, profile: str, envelope: dict[str, Any]) -> dict[str, Any]:
        """``envelope`` with the measured size of each kind of file of the real bundle."""

        bundle = ci_native.load(profile, "measured.json")["build"]["bundle"]
        for file in envelope["files"]:
            name = file["path"].split("/", 2)[2] if file["lane_id"] is None else None
            file["size"] = bundle["largest_jar_bytes"] if name is None else bundle["other_files"][name]
        return envelope

    def test_each_mod_has_a_complete_valid_plan(self) -> None:
        for profile, (targets, lanes) in COUNTS.items():
            plan = self.plans[profile]
            native = ci_native.load(profile, "targets.json")
            with self.subTest(profile=profile):
                self.assertIs(validate_plan(plan), plan)
                self.assertEqual(load_document(canonical_json(plan), kind="mod-base.build.plan"), plan)
                self.assertLess(len(canonical_json(plan)), limits.MAX_CI_PLAN_BYTES // 16)
                self.assertEqual((plan["profile"], len(plan["targets"]), len(plan["lanes"])), (profile, targets, lanes))
                self.assertEqual(planning.matrices(plan), {
                    "targets": [target["id"] for target in native["targets"]],
                    "lanes": [lane["artifact_node"] for lane in native["lanes"]]})
                self.assertEqual(plan["identity"]["inventory_sha256"],
                                 ci_native.records(profile)["release-matrix.json"]["sha256"])
                self.assertEqual(plan["identity"]["inventory_blob"],
                                 ci_native.records(profile)["release-matrix.json"]["blob"])
                self.assertEqual(plan["identity"]["scenario_sha256"],
                                 ci_native.records(profile)["scenario-contract.json"]["sha256"])
                self.assertEqual({key: plan[key] for key in ("targets", "lanes")},
                                 adapter.parse_derived_plan(canonical_json(derived(profile))))

    def test_outputs_are_the_real_staged_files_of_every_target(self) -> None:
        for profile, (targets, lanes) in COUNTS.items():
            plan = self.plans[profile]
            outputs = [output for target in plan["targets"] for output in target["outputs"]]
            whole = [output for output in outputs if output["lane_id"] is None]
            with self.subTest(profile=profile):
                self.assertEqual(len(outputs), 2 * lanes + len(STAGING[profile][1]) * targets)
                # The JARs keep the names the mod stages them under, and every JAR name has a space.
                jars = sorted(output["path"] for output in outputs if output["lane_id"] is not None)
                self.assertEqual(jars, [path for path in ci_native.output_paths(profile)
                                        if path.startswith(("files/", "harness/"))])
                self.assertTrue(all(" " in path for path in jars))
                self.assertEqual({(output["lane_id"], output["role"]) for output in outputs if output["lane_id"]},
                                 {(lane["id"], role) for lane in plan["lanes"] for role in ("production", "harness")})
                # Everything else is the target's own, under the native name below the target's directory.
                self.assertEqual({output["path"].split("/", 2)[2] for output in whole}, STAGING[profile][1])
                for target in plan["targets"]:
                    roles = sorted(output["role"] for output in target["outputs"] if output["lane_id"] is None)
                    self.assertEqual(roles, sorted(beside(profile).values()), target["id"])
                    self.assertEqual(adapter.target_outputs(plan, target["id"]),
                                     tuple(sorted(output["path"] for output in target["outputs"])))
        # Block Pops stages no SBOM at all; Quick Skin one for each target, never one for a lane.
        self.assertEqual(sorted(beside("block-pops").values()), ["native-report"])
        self.assertEqual(sorted(beside("quick-skin").values()), ["native-report", "sbom"])

    def test_a_matching_envelope_of_every_partition_and_of_the_build_is_valid(self) -> None:
        for profile in ci_native.profiles():
            plan = self.plans[profile]
            partitions = []
            for index, target in enumerate(plan["targets"]):
                envelope = self.sized(profile, ci_envelope(plan, target_id=target["id"]))
                with self.subTest(profile=profile, target=target["id"]):
                    validate_build_envelope(envelope, plan=plan)
                    self.assertEqual(len(envelope["native_reports"]), 1)
                partitions.append({"envelope": envelope, "descriptor": ci_run_descriptor(
                    plan, "build", None, "target", unit_id=target["id"], artifact_id=100 + index)})
            complete = self.sized(profile, ci_envelope(plan))
            with self.subTest(profile=profile):
                validate_build_envelope(complete, plan=plan)
                self.assertEqual(validate_target_partitions(partitions, plan=plan), complete["files"])
                self.assertEqual(len(complete["native_reports"]), COUNTS[profile][0])
                self.assertLess(len(canonical_json(complete)), limits.MAX_CI_ENVELOPE_BYTES // 16)
                # A file nobody planned, and a planned file that is missing, are both refused.
                extra = copy.deepcopy(complete)
                extra["files"].append({**extra["files"][0], "path": "zz-unplanned.jar"})
                missing = copy.deepcopy(complete)
                del missing["files"][0]
                for document in (extra, missing):
                    with self.assertRaisesRegex(MbError, "exact planned output union"):
                        validate_build_envelope(document, plan=plan)

    def test_the_native_names_of_per_target_files_collide_across_targets(self) -> None:
        # Both mods write ``artifacts.json`` in every partition. Planned under that bare name the
        # assembled Build would hold one path for every target, which no plan allows: the reason
        # an adapter stages such a file below ``targets/<target id>/``.
        for profile in ci_native.profiles():
            with self.subTest(profile=profile), self.assertRaisesRegex(MbError, "case-insensitive alias"):
                build_plan(self.root / f"{profile}-bare", profile, derived(profile, per_target="{name}"))

    def test_a_lane_may_own_a_report_but_always_owns_its_two_jars(self) -> None:
        for profile in ci_native.profiles():
            document = derived(profile)
            lane = document["lanes"][0]["id"]
            # Giving a manifest to one lane of its target is still a plan (a lane may have reports),
            for output in document["targets"][0]["outputs"]:
                if output["role"] == "native-report":
                    output["lane_id"] = lane
            build_plan(self.root / f"{profile}-lane-report", profile, document)
            # but a JAR without its lane, or a lane without one of its JARs, never is.
            for label, mutate in (("target-scoped JAR", lambda outputs: outputs[0].update(lane_id=None)),
                                  ("missing harness", lambda outputs: outputs.pop(1))):
                broken = derived(profile)
                mutate(broken["targets"][0]["outputs"])
                with self.subTest(profile=profile, case=label), self.assertRaises(MbError):
                    build_plan(self.root / f"{profile}-{label.replace(' ', '-')}", profile, broken)


if __name__ == "__main__":
    unittest.main()
