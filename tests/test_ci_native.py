"""The native parity fixtures are what their manifest says, and the kit can name what they hold.

``tests/fixtures/ci_native`` keeps data of the two consumer commits ``docs/BUILD-E2E-DESIGN.md``
reviewed (``tests/ci_native.py`` describes every file). The lists in it were produced by each mod's
own code; these tests derive them a second time, directly from the mod's inventory and scenario
contract bytes, so a fixture can neither drift from those bytes nor from the counts the design
states (Block Pops: 10 targets and 20 lanes; Quick Skin: 17 targets and 34 lanes). The last class
checks the names against the kit's own grammar: what a plan can take verbatim, and what it cannot.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mod_base.build_ci.records import validate_build_envelope
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import strict_loads
from tests import ci_native

ENVELOPE = Path(__file__).resolve().parent / "fixtures" / "documents" / "valid" / "ci-envelope.json"

#: (targets, lanes) of the reviewed commits (design, "Measured CI baseline").
COUNTS = {"block-pops": (10, 20), "quick-skin": (17, 34)}
#: Jobs API rows of one pull-request run: (Build listed, Build executed, Packaged E2E listed, Packaged
#: E2E executed). The rows that did not execute are ``skipped``.
JOBS = {"block-pops": (4, 3, 26, 24), "quick-skin": (27, 24, 48, 39)}
#: What staging writes beside the JARs, and whether it stages one lane or one whole target at a time.
STAGING = {"block-pops": ("lane", {"artifacts.json"}),
           "quick-skin": ("target", {"artifacts.json", "sbom/quick-skin.cdx.json"})}
RUN_ID = 37435530842


def numeric(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def pull_request_scenarios(contract: dict[str, Any]) -> list[str]:
    """Both mods: the contract's scenarios, in order, whose execution profiles include ``pr``."""

    return [item["scenario"] for item in contract["scenarios"] if "pr" in item["execution_profiles"]]


def block_pops_targets(profile: str) -> dict[str, Any]:
    """Block Pops: lanes are the inventory's artifacts ordered by Minecraft version and loader; a
    target is the lanes that share ``minecraft``; a runtime row is ``<artifact node>--pr-behavior``."""

    inventory = ci_native.load(profile, "release-matrix.json")
    runtimes = {row["artifact_node"]: row for row in inventory["runtimes"]}
    lanes = []
    for row in sorted(inventory["artifacts"], key=lambda row: (numeric(row["minecraft"]), row["loader"])):
        node = row["artifact_node"]
        lanes.append({"artifact_node": node, "target": row["minecraft"], "loader": row["loader"],
                      "java": row["java"], "runtime_java": runtimes[node]["java"],
                      "mod_version": row["mod_version"],
                      "production_jar": row["jar"].replace("{mod_version}", row["mod_version"]),
                      "harness_jar": row["harness_jar"], "runtime_version": runtimes[node]["minecraft"],
                      "runtime_id": f"{node}--pr-behavior".replace(".", "_")})
    return {"target_key": "minecraft", "runtime_key": "minecraft", "gradle_java": inventory["gradle_java"],
            "pr_scenarios": pull_request_scenarios(ci_native.load(profile, "scenario-contract.json")),
            "targets": grouped(lanes), "lanes": lanes}


def quick_skin_targets(profile: str) -> dict[str, Any]:
    """Quick Skin: targets are the distinct ``artifact_version`` values in numeric order, each with
    its artifacts in inventory order; the mod version comes from ``gradle.properties``; a runtime row
    is ``<artifact node>--<runtime version>--pr-behavior``."""

    inventory = ci_native.load(profile, "release-matrix.json")
    properties = {}
    for line in ci_native.read(profile, "gradle.properties").decode("utf-8").splitlines():
        if line.strip() and not line.strip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            properties[key.strip()] = value.strip()
    mod_version = properties[inventory["project"]["mod_version_property"]]
    runtimes = {row["artifact_node"]: row for row in inventory["runtimes"]}
    lanes = []
    for version in sorted({row["artifact_version"] for row in inventory["artifacts"]}, key=numeric):
        for row in inventory["artifacts"]:
            if row["artifact_version"] != version:
                continue
            node, runtime = row["artifact_node"], runtimes[row["artifact_node"]]
            lanes.append({"artifact_node": node, "target": version, "loader": row["loader"], "java": row["java"],
                          "runtime_java": runtime["java"], "mod_version": mod_version,
                          "production_jar": row["jar"].replace("{mod_version}", mod_version),
                          "harness_jar": row["harness_jar"].replace("{mod_version}", mod_version),
                          "runtime_version": runtime["runtime_version"],
                          "runtime_id": f"{node}--{runtime['runtime_version']}--pr-behavior".replace(".", "_")})
    return {"target_key": "artifact_version", "runtime_key": "runtime_version",
            "pr_scenarios": pull_request_scenarios(ci_native.load(profile, "scenario-contract.json")),
            "targets": grouped(lanes), "lanes": lanes}


def grouped(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for lane in lanes:
        if not targets or targets[-1]["id"] != lane["target"]:
            targets.append({"id": lane["target"], "lanes": []})
        targets[-1]["lanes"].append(lane["artifact_node"])
    return targets


DERIVED = {"block-pops": block_pops_targets, "quick-skin": quick_skin_targets}


def envelope_with(paths: list[str]) -> dict[str, Any]:
    """The valid Build envelope fixture with ``paths`` added to its inventory as build logs, so that
    only the export path rule decides whether the envelope stays valid."""

    document = strict_loads(ENVELOPE.read_bytes(), label=ENVELOPE.name, max_bytes=limits.MIB)
    added = [{**document["files"][0], "path": path, "role": "build-log"} for path in paths]
    document["files"] = sorted([*document["files"], *added], key=lambda item: item["path"])
    return document


def inventories(profile: str) -> tuple[list[str], list[str]]:
    """The two real inventories of a profile: its complete staged bundle and its Gradle outputs."""

    paths = ci_native.output_paths(profile)
    return ([path for path in paths if "/versions/" not in path], [path for path in paths if "/versions/" in path])


class ManifestTest(unittest.TestCase):
    def test_the_fixtures_cover_exactly_the_build_profiles(self) -> None:
        self.assertEqual(ci_native.profiles(), tuple(sorted(limits.MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE)))
        self.assertEqual(set(ci_native.profiles()), set(COUNTS))

    def test_the_manifest_lists_exactly_the_files_with_their_hashes(self) -> None:
        listed = {ci_native.MANIFEST}
        for profile in ci_native.profiles():
            for name, record in ci_native.records(profile).items():
                with self.subTest(profile=profile, name=name):
                    data = ci_native.read(profile, name)
                    self.assertEqual((len(data), hashlib.sha256(data).hexdigest()), (record["size"], record["sha256"]))
                    self.assertNotIn(b"\r", data)
                listed.add(f"{profile}/{name}")
        present = {path.relative_to(ci_native.ROOT).as_posix() for path in ci_native.ROOT.rglob("*") if path.is_file()}
        self.assertEqual(present, listed)

    def test_native_files_are_the_blobs_of_the_reviewed_commits(self) -> None:
        for profile, entry in ci_native.manifest()["profiles"].items():
            with self.subTest(profile=profile):
                self.assertTrue(grammar.is_match(grammar.REPOSITORY, entry["repository"]))
                self.assertTrue(grammar.is_match(grammar.SHA1, entry["commit"]))
                self.assertTrue(grammar.is_match(grammar.SHA1, entry["tree"]))
                self.assertLessEqual({"release-matrix.json", "scenario-contract.json"}, set(entry["native"]))
            for name, record in entry["native"].items():
                with self.subTest(profile=profile, name=name):
                    data = ci_native.read(profile, name)
                    # ``git rev-parse <commit>:<path>`` in the consumer answers this id.
                    self.assertEqual(hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest(), record["blob"])
                    self.assertTrue(grammar.is_repo_path(record["path"]))
            for name, record in entry["derived"].items():
                with self.subTest(profile=profile, name=name):
                    self.assertTrue(record["derivation"].strip())

    def test_an_edited_fixture_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            copy = Path(temporary) / "ci_native"
            shutil.copytree(ci_native.ROOT, copy)
            self.assertEqual(ci_native.read("block-pops", "targets.json", copy),
                             ci_native.read("block-pops", "targets.json"))
            path = copy / "block-pops" / "targets.json"
            path.write_bytes(path.read_bytes().replace(b'"fabric-1.20.1"', b'"fabric-1.20.2"', 1))
            with self.assertRaisesRegex(AssertionError, "block-pops/targets.json differs"):
                ci_native.read("block-pops", "targets.json", copy)
            with self.assertRaisesRegex(AssertionError, "block-pops/targets.json differs"):
                ci_native.load("block-pops", "targets.json", copy)


class DerivationTest(unittest.TestCase):
    def test_counts(self) -> None:
        for profile, (targets, lanes) in COUNTS.items():
            with self.subTest(profile=profile):
                document = ci_native.load(profile, "targets.json")
                self.assertEqual((len(document["targets"]), len(document["lanes"])), (targets, lanes))
                self.assertEqual(ci_native.load(profile, "release-matrix.json")["lane_count"], lanes)
                nodes = [lane["artifact_node"] for lane in document["lanes"]]
                self.assertEqual(len(set(nodes)), lanes)
                self.assertEqual([node for target in document["targets"] for node in target["lanes"]], nodes)
                loaders = {lane["artifact_node"]: lane["loader"] for lane in document["lanes"]}
                for target in document["targets"]:
                    # One Fabric lane and one Forge or NeoForge lane per Minecraft target.
                    self.assertEqual(len({loaders[node] for node in target["lanes"]}), 2, target)
                    self.assertIn("fabric", {loaders[node] for node in target["lanes"]})

    def test_targets_lanes_and_scenarios_follow_from_the_native_bytes(self) -> None:
        for profile, derive in DERIVED.items():
            with self.subTest(profile=profile):
                inventory = ci_native.load(profile, "release-matrix.json")
                # Every lane is a pull-request anchor at the reviewed commits, so the runtime rows
                # of a pull request are all the lanes.
                self.assertTrue(all(row["pr_anchor"] is True for row in inventory["runtimes"]))
                self.assertEqual(ci_native.load(profile, "targets.json"), derive(profile))
        self.assertEqual(ci_native.load("block-pops", "targets.json")["pr_scenarios"], ["ui-regression", "in-world"])
        self.assertEqual(len(ci_native.load("quick-skin", "targets.json")["pr_scenarios"]), 7)

    def test_staged_outputs_follow_from_the_lanes(self) -> None:
        for profile, (scope, beside) in STAGING.items():
            with self.subTest(profile=profile):
                outputs = ci_native.load(profile, "outputs.json")
                document = ci_native.load(profile, "targets.json")
                lanes = [lane for lane in document["lanes"] if lane["target"] == outputs["target"]]
                self.assertEqual(len(lanes), 2)
                self.assertIn(outputs["manifest"], beside)
                groups = [[lane] for lane in lanes] if scope == "lane" else [lanes]
                self.assertEqual([(unit["scope"], unit["id"]) for unit in outputs["units"]],
                                 [(scope, group[0]["artifact_node"] if scope == "lane" else outputs["target"])
                                  for group in groups])
                for unit, group in zip(outputs["units"], groups):
                    self.assertEqual(unit["gradle"], {"production": [lane["production_jar"] for lane in group],
                                                      "harness": [lane["harness_jar"] for lane in group]})
                    staged = {name for lane in group for name in ci_native.staged_names(lane)}
                    self.assertEqual(unit["staged"], sorted(staged | beside))
                    self.assertEqual(len(unit["staged"]), 2 * len(group) + len(beside))
                self.assertLessEqual({name for unit in outputs["units"] for name in unit["staged"]},
                                     set(ci_native.output_paths(profile)))


class JobListingTest(unittest.TestCase):
    def test_listings_hold_one_job_per_target_and_lane(self) -> None:
        for profile, (build_listed, build_executed, packaged_listed, packaged_executed) in JOBS.items():
            document = ci_native.load(profile, "jobs.json")
            targets = ci_native.load(profile, "targets.json")
            expanded = {
                "build": [document["templates"]["target"].format(target=target["id"]) for target in targets["targets"]]
                if "target" in document["templates"] else [],
                "packaged": [document["templates"]["lane"].format(runtime_id=lane["runtime_id"])
                             for lane in targets["lanes"]],
            }
            for graph, listed, executed in (("build", build_listed, build_executed),
                                            ("packaged", packaged_listed, packaged_executed)):
                with self.subTest(profile=profile, graph=graph):
                    jobs = document[graph]["jobs"]
                    names = [job["name"] for job in jobs]
                    self.assertEqual(names, sorted(set(names)))
                    self.assertLessEqual({job["conclusion"] for job in jobs}, {"success", "skipped"})
                    self.assertEqual((len(jobs), sum(job["conclusion"] == "success" for job in jobs)),
                                     (listed, executed))
                    conclusions = {job["name"]: job["conclusion"] for job in jobs}
                    self.assertEqual([conclusions.get(name) for name in expanded[graph]],
                                     ["success"] * len(expanded[graph]))
                    self.assertLessEqual(len(jobs), limits.MAX_JOBS_PER_ATTEMPT)
                    self.assertTrue(all(0 < len(name) <= limits.MAX_JOB_NAME_LENGTH for name in names))

    def test_both_listings_are_one_successful_pull_request_generation(self) -> None:
        for profile in ci_native.profiles():
            with self.subTest(profile=profile):
                document = ci_native.load(profile, "jobs.json")
                build, packaged = document["build"], document["packaged"]
                self.assertEqual(set(document), {"build", "packaged", "templates"})
                for graph in (build, packaged):
                    self.assertTrue(grammar.is_match(grammar.WORKFLOW_PATH, graph["workflow"]))
                    self.assertTrue(grammar.is_match(grammar.SHA1, graph["workflow_blob"]))
                    self.assertEqual((graph["run_attempt"], graph["conclusion"]), (1, "success"))
                self.assertNotEqual(build["run_id"], packaged["run_id"])
                self.assertNotEqual(build["workflow"], packaged["workflow"])
                for key in ("event", "head_sha", "head_branch"):
                    self.assertEqual(build[key], packaged[key])
        # The two authority models of the design: a protected controller, and the candidate itself.
        self.assertEqual(ci_native.load("block-pops", "jobs.json")["build"]["event"], "pull_request_target")
        self.assertEqual(ci_native.load("quick-skin", "jobs.json")["build"]["event"], "pull_request")

    def test_a_skipped_call_and_an_unexpanded_matrix_are_listed_once_each(self) -> None:
        # The shapes the exact graphs must model (design, "Stable contexts and exact graphs"): a
        # reusable-workflow call that was skipped is one row named by its job id, and a matrix job
        # skipped before it expanded is one row whose name still holds the expression.
        skipped = [job["name"] for job in ci_native.load("quick-skin", "jobs.json")["packaged"]["jobs"]
                   if job["conclusion"] == "skipped"]
        self.assertIn("compile", skipped)
        self.assertIn("Prepare public evidence for ${{ matrix.bundle_key }} (advisory)", skipped)
        executed = [job["name"] for job in ci_native.load("quick-skin", "jobs.json")["build"]["jobs"]
                    if job["conclusion"] == "success"]
        self.assertIn("compile / Plan every supported build target", executed)


class MeasuredTest(unittest.TestCase):
    def test_measured_artifacts_are_the_fixture_lanes(self) -> None:
        for profile in ci_native.profiles():
            with self.subTest(profile=profile):
                measured = ci_native.load(profile, "measured.json")
                document = ci_native.load(profile, "targets.json")
                lanes = {lane["runtime_id"]: lane for lane in document["lanes"]}
                names = [lane["name"] for lane in measured["packaged"]["lanes"]]
                self.assertEqual(len(names), len(lanes))
                self.assertEqual({next(identity for identity in lanes if name.endswith("-" + identity))
                                  for name in names}, set(lanes))
                bundle = measured["build"]["bundle"]
                self.assertEqual(bundle["files"], 2 * len(lanes) + len(STAGING[profile][1]))
                self.assertEqual(set(bundle["other_files"]), STAGING[profile][1])
                self.assertLessEqual(bundle["expanded_bytes"], bundle["artifact"]["bytes"])
                for sample in measured["packaged"]["samples"]:
                    lane = next(lanes[identity] for identity in lanes
                                if sample["artifact"]["name"].endswith("-" + identity))
                    # One evidence profile per pull-request scenario of the lane.
                    self.assertEqual(set(sample["evidence_profiles"]),
                                     {f"{lane['artifact_node']}--{lane['runtime_version']}--{scenario}"
                                      for scenario in document["pr_scenarios"]})
                    profiles = sample["evidence_profiles"].values()
                    self.assertLess(sum(item["files"] for item in profiles), sample["files"])
                    self.assertLess(sum(item["bytes"] for item in profiles), sample["expanded_bytes"])
                    self.assertTrue(grammar.is_match(grammar.DIGEST, sample["artifact"]["digest"]))


class KitNamingTest(unittest.TestCase):
    """What a kit plan can take from the mods as it is."""

    def test_targets_and_artifact_nodes_are_unit_ids_but_runtime_rows_are_not(self) -> None:
        for profile in ci_native.profiles():
            document = ci_native.load(profile, "targets.json")
            self.assertLessEqual(len(document["targets"]), limits.MAX_CI_TARGETS)
            self.assertLessEqual(len(document["lanes"]), limits.MAX_CI_LANES)
            for kind, units in (("target", [target["id"] for target in document["targets"]]),
                                ("runtime", [lane["artifact_node"] for lane in document["lanes"]])):
                for unit in units:
                    with self.subTest(profile=profile, kind=kind, unit=unit):
                        self.assertTrue(grammar.is_match(grammar.CI_UNIT_ID, unit))
                        name = grammar.ci_artifact_name(kind, RUN_ID, 1, unit)
                        parsed = grammar.parse_ci_artifact_name(name)
                        self.assertEqual((parsed.kind, parsed.unit_id), (kind, unit))
            for lane in document["lanes"]:
                with self.subTest(profile=profile, runtime_id=lane["runtime_id"]):
                    # The mods' runtime row ids hold "--", which separates the fields of an artifact
                    # name: a plan names a lane by its artifact node instead.
                    self.assertIn("--", lane["runtime_id"])
                    self.assertFalse(grammar.is_match(grammar.CI_UNIT_ID, lane["runtime_id"]))
                    with self.assertRaises(MbError):
                        grammar.ci_artifact_name("runtime", RUN_ID, 1, lane["runtime_id"])

    def test_real_output_paths_differ_from_a_repository_path_only_by_single_inner_spaces(self) -> None:
        validate_build_envelope(envelope_with([]))
        for profile in ci_native.profiles():
            staged, built = inventories(profile)
            with self.subTest(profile=profile):
                self.assertEqual((len(staged), len(built)),
                                 (2 * COUNTS[profile][1] + len(STAGING[profile][1]), 2 * COUNTS[profile][1]))
                for path in (*staged, *built):
                    self.assertEqual(" " in path, path.endswith(".jar"), path)  # Every JAR, nothing else.
                    self.assertNotIn("  ", path)
                    self.assertFalse(any(part != part.strip() for part in path.split("/")), path)
                # With the spaces written as "_", depth, length, traversal and the case-alias rule
                # already admit each inventory as a whole.
                validate_build_envelope(envelope_with([path.replace(" ", "_") for path in staged]))
                validate_build_envelope(envelope_with([path.replace(" ", "_") for path in built]))

    def test_every_real_output_path_passes_the_export_path_rule(self) -> None:
        # Decision D6: an export path keeps the mod's own file name (letters, digits, ".", "_", "-",
        # "+" and single inner spaces), which a repository path refuses for its spaces.
        refused: dict[str, list[str]] = {}
        for profile in ci_native.profiles():
            for path in ci_native.output_paths(profile):
                try:
                    validate_build_envelope(envelope_with([path]))
                except MbError:
                    refused.setdefault(profile, []).append(path)
        if refused:
            self.fail("the export path rule refuses real output paths: " + "; ".join(
                f"{profile}: {len(paths)} of {len(ci_native.output_paths(profile))}, first {paths[0]!r}"
                for profile, paths in refused.items()))
        for profile in ci_native.profiles():
            for inventory in inventories(profile):
                validate_build_envelope(envelope_with(inventory))


if __name__ == "__main__":
    unittest.main()
