"""``ci scope``: a pull request's packaged lanes run a narrowed scope only on authenticated ground.

docs/BUILD-RUNTIME-SELECTION.md, D1-D5 as the owner decided them: the scope is sealed outside the
plan (D1 b), derived by a token-free protected hook from data the kit authenticated (D2), carried
by a new kind and optional fields (D3), names every lane with at least one scenario, a lane named
with none running in full (D4), and never lets a pull request cover its push (D5, in
``test_ci_reuse``). Unknown means full scope; only the GitHub API is faked.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.build_ci import commands, commands_scope, identity, lifecycle, scope
from mod_base.build_ci.config import load_build_config, validate_build_config
from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.records import bind_scope_record, validate_scope_record
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests import ci_mod_harness as h
from tests.ci_attempt import plan_of, record_zip
from tests.helpers import ci_api_run, ci_run_gate, ci_run_producer

PACKAGED = CI_CALLER_WORKFLOWS["packaged"]
#: The run attempt of the packaged caller whose input job derives the scope.
RUN_ID, RUN_ATTEMPT = 77, 1
#: Each lane's obligations; the synthetic hook keeps the first of every lane.
OBLIGATIONS = ["scenario/smoke/title", "scenario/gallery/inventory"]
BLOB = {name: f"{index:040x}" for index, name in enumerate(
    ("readme", "readme-changed", "source", "config", "config-changed", "contract"), start=1)}
#: The base commit's packaged push run, as ``ci_run_gate`` seals its record, and its tested record.
BASE_RUN, BASE_ATTEMPT, BASE_RECORD = 43, 2, 900
#: The pull request run whose original packaged record a reuse reference names.
ORIGINAL_RUN, ORIGINAL_RECORD, REFERENCE = 60, 901, 902
#: ``ci scope`` behind a direct baseline and behind a reuse reference (limits.MAX_CI_SCOPE_REQUESTS).
DIRECT_REQUESTS, REUSED_REQUESTS = 11, 15
#: The changes alone: the base commit and both trees.
CHANGES_REQUESTS = 3


def pull_request_plan(lanes: int = 2) -> dict[str, Any]:
    plan = plan_of(h.subject(), lanes=lanes)
    for lane in plan["lanes"]:
        lane["obligations"] = list(OBLIGATIONS)
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def base_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """The plan the push of the pull request's base commit was tested under: the same work."""

    base = copy.deepcopy(plan)
    base["identity"] = {**h.subject(pull_request=False),
                        **{key: plan["identity"][key] for key in ("policy_sha256", "inventory_blob",
                                                                  "inventory_sha256", "scenario_sha256",
                                                                  "runtime_selection_sha256")}}
    base["plan_sha256"] = plan_sha256(base)
    return base


def scoped_config(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(load_build_config(h.MOD, repository=h.REPOSITORY).data)
    document["runtime_selection"] = {"input": "changed-paths-v1"}
    document.update(changes)
    return validate_build_config(document)


def tree(**paths: str) -> list[dict[str, Any]]:
    return [{"path": path, "mode": "100644", "type": "blob", "sha": sha} for path, sha in paths.items()]


def first_scenarios(plan: dict[str, Any]) -> bytes:
    """What the synthetic hook writes: the first obligation of every lane."""

    return canonical_json({"lanes": [{"id": lane["id"], "scenarios": lane["obligations"][:1]}
                                     for lane in plan["lanes"]]})


class World:
    """The fixture repository with pull request 7, its base commit's trees and the packaged push
    run of that base commit, which sealed its tested record."""

    def __init__(self, *, lanes: int = 2) -> None:
        self.api, _ = h.github()
        self.plan = pull_request_plan(lanes)
        self.base = base_plan(self.plan)
        self.config = scoped_config()
        self.api.add_tree(h.CONTROLLER_TREE, tree(**{"README.md": BLOB["readme"], "src/Mod.java": BLOB["source"],
                                                     "e2e/scenario-contract.json": BLOB["contract"]}))
        self.changed({"README.md": BLOB["readme-changed"]})
        self.run = ci_api_run(self.base, "packaged", id=BASE_RUN, run_attempt=BASE_ATTEMPT)
        self.api.add_run(self.run)
        self.record = ci_run_gate(self.base, "packaged", "selected")
        self.artifacts: dict[int, tuple[dict[str, Any], bytes]] = {}
        self.publish(BASE_RUN, BASE_RECORD, grammar.ci_artifact_name("tested", BASE_RUN, BASE_ATTEMPT, "packaged"),
                     grammar.CI_GATE_NAME, self.record, head=(h.BRANCH, h.CONTROLLER_SHA))
        self.inputs: list[str] = []
        self.selection: bytes | None = None

    def changed(self, paths: dict[str, str], *, truncated: bool = False) -> None:
        """The tested tree: the base tree with ``paths`` changed (a sha of ``None`` removes one)."""

        entries = {"README.md": BLOB["readme"], "src/Mod.java": BLOB["source"],
                   "e2e/scenario-contract.json": BLOB["contract"], **paths}
        self.api.add_tree(h.TESTED_TREE, tree(**{path: sha for path, sha in entries.items() if sha}),
                          truncated=truncated)

    def publish(self, run_id: int, artifact_id: int, name: str, file: str, document: dict[str, Any], *,
                head: tuple[str, str], **changes: Any) -> None:
        archive = record_zip(file, canonical_json(document))
        record = {"id": artifact_id, "name": name, "created_at": "2026-10-07T10:01:30Z",
                  "updated_at": "2026-10-07T10:01:30Z", "expires_at": "2027-01-05T10:01:30Z",
                  "workflow_run": {"id": run_id, "head_branch": head[0], "head_sha": head[1]}, **changes}
        self.api.add_artifact(record, archive)
        self.artifacts[artifact_id] = (record, archive)

    def derive(self, value: str) -> bytes:
        self.inputs.append(value)
        return first_scenarios(self.plan) if self.selection is None else self.selection

    def decide(self, **changes: Any) -> dict[str, Any] | scope.FullScope:
        arguments = {"plan": self.plan, "config": self.config, "event": "pull_request_target", "run_id": RUN_ID,
                     "run_attempt": RUN_ATTEMPT, "workflow_path": PACKAGED, "derive": self.derive, **changes}
        with tempfile.TemporaryDirectory() as directory:
            outcome = scope.derive_scope(self.api, temporary_root=Path(directory), **arguments)
            self.assert_cleaned(Path(directory))
        self.assert_read_only()
        return outcome

    @staticmethod
    def assert_cleaned(directory: Path) -> None:
        if list(directory.iterdir()):
            raise AssertionError("a baseline record was left behind")

    def assert_read_only(self) -> None:
        if self.api.mutations:
            raise AssertionError("ci scope writes nothing to GitHub")


class ScopeCase(unittest.TestCase):
    def assert_full(self, world: World, reason: str, *, requests: int | None = None,
                    **changes: Any) -> scope.FullScope:
        outcome = world.decide(**changes)
        self.assertIsInstance(outcome, scope.FullScope)
        self.assertEqual(outcome.reason, reason, outcome.detail)
        self.assertTrue(outcome.detail.startswith(scope.FULL_SCOPE_REASONS[reason]))
        self.assertNotIn("\n", outcome.detail)
        if requests is not None:
            self.assertEqual(world.api.request_count, requests)
        return outcome


class NarrowedTests(ScopeCase):
    def test_a_scope_rests_on_the_changed_paths_and_the_base_commits_full_record(self) -> None:
        world = World()
        record = world.decide()
        self.assertIsInstance(record, dict)
        self.assertEqual(world.api.request_count, DIRECT_REQUESTS)
        self.assertLessEqual(DIRECT_REQUESTS, limits.MAX_CI_SCOPE_REQUESTS)
        validate_scope_record(record, plan=world.plan)
        bind_scope_record(record, plan=world.plan, run_id=RUN_ID, run_attempt=RUN_ATTEMPT, workflow_path=PACKAGED)
        self.assertEqual(record["lanes"], [{"id": lane["id"], "scenarios": OBLIGATIONS[:1]}
                                           for lane in world.plan["lanes"]])
        self.assertEqual(record["changes"], {"base_sha": h.CONTROLLER_SHA, "base_tree": h.CONTROLLER_TREE,
                                             "tested_tree": h.TESTED_TREE, "paths": 1,
                                             "paths_sha256": canonical_sha256(["README.md"])})
        self.assertEqual(record["baseline"], {
            "evidence": "tested", "original": None, "plan_sha256": world.base["plan_sha256"],
            "record_sha256": canonical_sha256(world.record),
            "record": {"run_id": BASE_RUN, "run_attempt": BASE_ATTEMPT, "artifact_id": BASE_RECORD,
                       "artifact_name": grammar.ci_artifact_name("tested", BASE_RUN, BASE_ATTEMPT, "packaged"),
                       "digest": world.api.get_json(f"/repos/{h.REPOSITORY}/actions/artifacts/{BASE_RECORD}")["digest"]}})
        self.assertEqual((record["request"]["run_id"], record["request"]["run_attempt"]), (RUN_ID, RUN_ATTEMPT))
        self.assertRegex(record["request"]["nonce"], "^[0-9a-f]{64}$")
        # The hook ran once, with one line of the kit's data and nothing of the candidate's.
        self.assertEqual(len(world.inputs), 1)
        value = strict_loads(world.inputs[0].encode(), label="input", max_bytes=limits.MAX_CI_SCOPE_BYTES)
        self.assertEqual(canonical_json(value).decode().rstrip("\n"), world.inputs[0])
        self.assertEqual(value, {"format": "changed-paths-v1", "base_sha": h.CONTROLLER_SHA,
                                 "base_tree": h.CONTROLLER_TREE, "tested_tree": h.TESTED_TREE,
                                 "changed_paths": ["README.md"], "baseline": record["baseline"]})

    def test_added_removed_and_relinked_paths_are_changes(self) -> None:
        world = World()
        # README.md removed, docs/new.md added, src/Mod.java made executable; a tree entry is no path.
        world.api.add_tree(h.TESTED_TREE, [
            *tree(**{"docs/new.md": BLOB["readme"], "e2e/scenario-contract.json": BLOB["contract"]}),
            {"path": "src/Mod.java", "mode": "100755", "type": "blob", "sha": BLOB["source"]},
            {"path": "docs", "mode": "040000", "type": "tree", "sha": "9" * 40}])
        record = world.decide()
        self.assertIsInstance(record, dict)
        self.assertEqual(strict_loads(world.inputs[0].encode(), label="input", max_bytes=1 << 20)["changed_paths"],
                         ["README.md", "docs/new.md", "src/Mod.java"])
        self.assertEqual(record["changes"]["paths"], 3)
        # A changed mode of a file the selection reads is a change of it too.
        world = World()
        world.api.add_tree(h.TESTED_TREE, [*tree(**{"README.md": BLOB["readme"], "src/Mod.java": BLOB["source"]}),
                                           {"path": "e2e/scenario-contract.json", "mode": "100755", "type": "blob",
                                            "sha": BLOB["contract"]}])
        outcome = world.decide()
        self.assertEqual((outcome.reason, world.inputs), ("selection-input-changed", []))

    def test_a_lane_the_hook_names_no_scenario_for_runs_every_obligation(self) -> None:
        world = World()
        lanes = world.plan["lanes"]
        world.selection = canonical_json({"lanes": [{"id": lanes[0]["id"], "scenarios": []},
                                                    {"id": lanes[1]["id"], "scenarios": OBLIGATIONS[1:]}]})
        record = world.decide()
        self.assertEqual(record["lanes"], [{"id": lanes[0]["id"], "scenarios": OBLIGATIONS},
                                           {"id": lanes[1]["id"], "scenarios": OBLIGATIONS[1:]}])

    def test_the_base_commit_may_be_covered_by_a_reuse_reference_of_a_full_pull_request(self) -> None:
        world = self.reused()
        record = world.decide()
        self.assertIsInstance(record, dict, getattr(record, "detail", ""))
        self.assertEqual(world.api.request_count, REUSED_REQUESTS)
        self.assertLessEqual(REUSED_REQUESTS, limits.MAX_CI_SCOPE_REQUESTS)
        baseline = record["baseline"]
        self.assertEqual((baseline["evidence"], baseline["record"]["artifact_id"], baseline["original"]["artifact_id"],
                          baseline["original"]["run_id"]), ("reuse", REFERENCE, ORIGINAL_RECORD, ORIGINAL_RUN))
        validate_scope_record(record, plan=world.plan)

    def reused(self, *, narrowed: bool = False) -> World:
        """The base commit's push run reused the gates of the full pull request that merged it."""

        world = World()
        # A reuse run seals no tested record of its own: the push run lists the reference instead.
        world.api.add_artifact({**world.artifacts[BASE_RECORD][0],
                                "workflow_run": {"id": 999, "head_branch": h.BRANCH, "head_sha": h.CONTROLLER_SHA}},
                               world.artifacts[BASE_RECORD][1])
        original_plan = copy.deepcopy(world.plan)
        original_plan["identity"].update(pr_number=5, head_sha="5" * 40, tested_sha="6" * 40,
                                         tested_tree=world.base["identity"]["tested_tree"],
                                         tested_parents=["b" * 40, "5" * 40], base_sha="b" * 40,
                                         controller_sha="b" * 40)
        original_plan["plan_sha256"] = plan_sha256(original_plan)
        original = ci_run_gate(original_plan, "packaged", "pull-request")
        original["producer"]["run_id"] = ORIGINAL_RUN
        for item in original["artifacts"]:
            item["producer"]["run_id"] = ORIGINAL_RUN
            name = grammar.parse_ci_artifact_name(item["artifact"]["name"])
            item["artifact"]["name"] = grammar.ci_artifact_name(name.kind, ORIGINAL_RUN, name.run_attempt, name.unit_id)
        if narrowed:
            original["scope_sha256"] = "a" * 64
        world.publish(ORIGINAL_RUN, ORIGINAL_RECORD,
                      grammar.ci_artifact_name("tested", ORIGINAL_RUN, 2, "packaged"), grammar.CI_GATE_NAME,
                      original, head=("feature/merged", "5" * 40))
        state, archive = world.artifacts[ORIGINAL_RECORD]
        state = {**state, "size_in_bytes": len(archive), "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
        seal = {"identity": original["identity"], "plan_sha256": original["plan_sha256"],
                "profile": original["profile"],
                "producer": {**original["producer"],
                             "upload_window": {"started_at": "2026-10-07T10:01:00Z",
                                               "completed_at": "2026-10-07T10:02:00Z"}},
                "artifact": {"id": ORIGINAL_RECORD, "name": state["name"], "digest": state["digest"],
                             "size": state["size_in_bytes"], "created_at": state["created_at"],
                             "expires_at": state["expires_at"]}}
        build_seal = copy.deepcopy(seal)
        build_seal["producer"].update(run_id=ORIGINAL_RUN - 1, workflow_path=CI_CALLER_WORKFLOWS["build"],
                                      workflow_ref=grammar.workflow_ref(h.REPOSITORY, CI_CALLER_WORKFLOWS["build"],
                                                                        h.BRANCH))
        build_seal["artifact"].update(id=ORIGINAL_RECORD - 10,
                                      name=grammar.ci_artifact_name("tested", ORIGINAL_RUN - 1, 2, "build"))
        producer = {key: value for key, value in ci_run_producer(world.base, "packaged", "reuse").items()
                    if key != "upload_window"}
        reference = {"kind": "mod-base.ci.reuse", "schema_version": 1, "identity": world.base["identity"],
                     "plan_sha256": world.base["plan_sha256"], "profile": world.base["profile"],
                     "producer": producer,
                     "source": {"identity": original["identity"], "plan_sha256": original["plan_sha256"],
                                "profile": original["profile"], "build_seal": build_seal, "packaged_seal": seal}}
        world.publish(BASE_RUN, REFERENCE, grammar.ci_artifact_name("reuse", BASE_RUN, BASE_ATTEMPT),
                      grammar.CI_REUSE_NAME, reference, head=(h.BRANCH, h.CONTROLLER_SHA))
        return world

    def test_a_narrowed_original_behind_a_reference_is_no_full_baseline(self) -> None:
        self.assert_full(self.reused(narrowed=True), "baseline-narrowed", requests=CHANGES_REQUESTS + 8)


class FullTests(ScopeCase):
    def test_what_the_job_knows_already_answers_full_without_a_request(self) -> None:
        cases = {
            "not-enabled": {"config": validate_build_config(copy.deepcopy(
                load_build_config(h.MOD, repository=h.REPOSITORY).data))},
            "not-a-pull-request": {"event": "push"},
        }
        for reason, changes in cases.items():
            world = World()
            with self.subTest(reason=reason):
                self.assert_full(world, reason, requests=0, **changes)
                self.assertEqual(world.inputs, [])
        world = World()
        world.plan["candidate_kit"] = {"sha": "9" * 40, "version": "1.3.0"}
        world.plan["plan_sha256"] = plan_sha256(world.plan)
        self.assertIn("1.3.0", self.assert_full(world, "candidate-kit", requests=0).detail)
        # A pull request from a fork never narrows.
        world = World()
        world.plan["identity"]["source_repository"] = "fork/synthetic-mod"
        world.plan["plan_sha256"] = plan_sha256(world.plan)
        self.assertIn("fork/synthetic-mod", self.assert_full(world, "foreign-pull-request", requests=0).detail)
        # A push plan never narrows, whatever its event says.
        world = World()
        world.plan = base_plan(world.plan)
        self.assert_full(world, "not-a-pull-request", requests=0)

    def test_a_change_of_the_selection_policy_or_of_what_it_reads_runs_full(self) -> None:
        config = scoped_config()
        paths = sorted(scope.selection_inputs(config))
        for expected in ("scripts/ci/mod-base-build.json", "scripts/ci/mod_base_build_dispatch.py",
                         "release/inventory.json", "e2e/scenario-contract.json", "gradle.properties",
                         "site/mod-base-build-activation.json", ".github/workflows/mod-base-packaged-e2e.yml"):
            self.assertIn(expected, paths)
        for path in paths:
            world = World()
            world.changed({path: BLOB["config-changed"]})
            with self.subTest(path=path):
                outcome = self.assert_full(world, "selection-input-changed", requests=CHANGES_REQUESTS)
                self.assertIn(path, outcome.detail)
                self.assertEqual(world.inputs, [])

    def test_changes_that_are_not_listed_completely_run_full(self) -> None:
        world = World()
        world.changed({}, truncated=True)
        self.assert_full(world, "changes-unknown", requests=CHANGES_REQUESTS)
        world = World()
        with mock.patch.object(limits, "MAX_CI_SCOPE_TREE_ENTRIES", 2):
            self.assert_full(world, "changes-unknown", requests=2)

    def test_malformed_trees_and_commits_stop_the_job(self) -> None:
        cases = {
            "a tree of another sha": lambda world: world.api.add_response(
                f"/repos/{h.REPOSITORY}/git/trees/{h.TESTED_TREE}",
                {"sha": "1" * 40, "tree": [], "truncated": False}, params={"recursive": 1}),
            "no truncation flag": lambda world: world.api.add_response(
                f"/repos/{h.REPOSITORY}/git/trees/{h.TESTED_TREE}", {"sha": h.TESTED_TREE, "tree": []},
                params={"recursive": 1}),
            "an unsafe path": lambda world: world.api.add_tree(h.TESTED_TREE, tree(**{"../x": BLOB["readme"]})),
            "a repeated path": lambda world: world.api.add_tree(
                h.TESTED_TREE, tree(**{"README.md": BLOB["readme"]}) * 2),
            "a malformed commit": lambda world: world.api.add_response(
                f"/repos/{h.REPOSITORY}/git/commits/{h.CONTROLLER_SHA}", {"sha": h.CONTROLLER_SHA, "tree": "x"}),
        }
        for name, change in cases.items():
            world = World()
            change(world)
            with self.subTest(case=name), self.assertRaises(MbError):
                world.decide()
            self.assertEqual(world.inputs, [])

    def test_a_base_without_complete_successful_full_evidence_runs_full(self) -> None:
        def run(**changes):
            return lambda world: world.api.add_run({**world.run, **changes})

        def artifact(**changes):
            return lambda world: world.api.add_artifact({**world.artifacts[BASE_RECORD][0], **changes},
                                                        world.artifacts[BASE_RECORD][1])

        def record(**identity_changes):
            def change(world):
                other = copy.deepcopy(world.base)
                other["identity"].update(identity_changes)
                other["plan_sha256"] = plan_sha256(other)
                document = ci_run_gate(other, "packaged", "selected")
                world.publish(BASE_RUN, BASE_RECORD, world.artifacts[BASE_RECORD][0]["name"], grammar.CI_GATE_NAME,
                              document, head=(h.BRANCH, h.CONTROLLER_SHA))
            return change

        other_kit = [{**item, "sha": "9" * 40, "path": item["path"].replace(h.KIT_SHA, "9" * 40)}
                     if item["path"].startswith("The-Plum-Team/") else item
                     for item in ci_api_run(base_plan(pull_request_plan()), "packaged")["referenced_workflows"]]
        cases = {
            "no-baseline": (lambda world: world.api.add_run({**world.run, "head_sha": "9" * 40}), 4),
            "baseline-pending": (run(status="in_progress", conclusion=None), 5),
            "baseline-failed": (run(conclusion="failure"), 5),
            "baseline-kit-differs": (run(referenced_workflows=other_kit), 5),
            "baseline-unavailable": (artifact(expired=True), 6),
            "baseline-differs": (record(policy_sha256="f" * 64), 8),
        }
        for reason, (change, requests) in cases.items():
            world = World()
            change(world)
            with self.subTest(reason=reason):
                self.assert_full(world, reason, requests=requests)
                self.assertEqual(world.inputs, [])
        world = World()
        world.api.add_artifact({**world.artifacts[BASE_RECORD][0], "name": "mb-ci-runtime--43--a2--lane-a"},
                               world.artifacts[BASE_RECORD][1])
        self.assert_full(world, "baseline-unavailable", requests=6)

    def test_ambiguous_or_foreign_baseline_evidence_stops_the_job(self) -> None:
        cases = {
            "a run of another commit": lambda world: world.api.add_response(
                f"/repos/{h.REPOSITORY}/actions/runs/{BASE_RUN}", {**world.run, "head_sha": "9" * 40}),
            "a record of another commit": lambda world: world.api.add_artifact(
                {**world.artifacts[BASE_RECORD][0], "workflow_run": {"id": BASE_RUN, "head_branch": h.BRANCH,
                                                                     "head_sha": "9" * 40}},
                world.artifacts[BASE_RECORD][1]),
            "a record and a reference": lambda world: world.publish(
                BASE_RUN, REFERENCE, grammar.ci_artifact_name("reuse", BASE_RUN, BASE_ATTEMPT), grammar.CI_GATE_NAME,
                world.record, head=(h.BRANCH, h.CONTROLLER_SHA)),
            "the Build record": lambda world: world.publish(
                BASE_RUN, BASE_RECORD, world.artifacts[BASE_RECORD][0]["name"], grammar.CI_GATE_NAME,
                ci_run_gate(world.base, "build", "full"), head=(h.BRANCH, h.CONTROLLER_SHA)),
            "corrupt bytes": lambda world: world.api.add_artifact(world.artifacts[BASE_RECORD][0], b"other bytes"),
        }
        for name, change in cases.items():
            world = World()
            change(world)
            with self.subTest(case=name), self.assertRaises(MbError):
                world.decide()
            self.assertEqual(world.inputs, [])

    def test_a_selection_that_narrows_nothing_or_does_not_fit_runs_full(self) -> None:
        world = World()
        world.selection = canonical_json({"lanes": [{"id": lane["id"], "scenarios": lane["obligations"]}
                                                    for lane in world.plan["lanes"]]})
        self.assert_full(world, "nothing-narrowed", requests=CHANGES_REQUESTS + 5)
        world = World()
        world.selection = canonical_json({"lanes": [{"id": lane["id"], "scenarios": []}
                                                    for lane in world.plan["lanes"]]})
        self.assert_full(world, "nothing-narrowed")
        world = World()
        with mock.patch.object(limits, "MAX_CI_SCOPE_BYTES", 1500):
            self.assert_full(world, "scope-too-large")
        world = World()
        with mock.patch.object(limits, "MAX_CI_SCOPE_BYTES", 100):
            self.assert_full(world, "changes-too-large")
            self.assertEqual(world.inputs, [])

    def test_a_selection_of_anything_but_the_planned_obligations_stops_the_job(self) -> None:
        def lanes(*entries):
            return canonical_json({"lanes": list(entries)})

        first, second = (lane["id"] for lane in pull_request_plan()["lanes"])
        cases = {
            "an unplanned obligation": lanes({"id": first, "scenarios": ["scenario/other"]},
                                             {"id": second, "scenarios": []}),
            "another order": lanes({"id": first, "scenarios": OBLIGATIONS[::-1][:2]}, {"id": second, "scenarios": []}),
            "a missing lane": lanes({"id": first, "scenarios": OBLIGATIONS[:1]}),
            "lanes out of order": lanes({"id": second, "scenarios": []}, {"id": first, "scenarios": OBLIGATIONS[:1]}),
            "an unknown key": canonical_json({"lanes": [], "full": True}),
            "not JSON": b"smoke",
        }
        for name, selection in cases.items():
            world = World()
            world.selection = selection
            with self.subTest(case=name), self.assertRaises(MbError):
                world.decide()

    def test_what_the_scope_rests_on_is_observed_again_before_it_is_written(self) -> None:
        moves = {
            "baseline expired": lambda world: world.api.add_artifact(
                {**world.artifacts[BASE_RECORD][0], "expired": True}, world.artifacts[BASE_RECORD][1]),
            "baseline rerun": lambda world: world.api.add_run({**world.run, "run_attempt": 3}),
            "newer baseline run": lambda world: world.api.add_run(
                {**world.run, "id": BASE_RUN + 1, "created_at": "2026-10-08T10:00:00Z"}),
        }
        for name, move in moves.items():
            world = World()

            def derive(value: str, world: World = world, move=move) -> bytes:
                move(world)
                return World.derive(world, value)

            with self.subTest(move=name), self.assertRaisesRegex(MbError, "changed between"):
                world.decide(derive=derive)


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World()
        self.record = self.world.decide()
        self.raw = canonical_json(self.record)

    def received(self, raw: bytes, **changes: Any) -> dict[str, Any] | None:
        arguments = {"plan": self.world.plan, "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT,
                     "workflow_path": PACKAGED, **changes}
        return scope.received_scope(raw, **arguments)

    def test_a_job_receives_exactly_its_attempts_scope_or_none(self) -> None:
        self.assertEqual(self.received(self.raw), self.record)
        self.assertIsNone(self.received(b""))
        self.assertIsNone(self.received(b"\n"))
        refused = {
            "another attempt": ({}, {"run_attempt": RUN_ATTEMPT + 1}),
            "another run": ({}, {"run_id": RUN_ID + 1}),
            "the Build caller": ({}, {"workflow_path": CI_CALLER_WORKFLOWS["build"]}),
            "another plan": ({}, {"plan": pull_request_plan(lanes=1)}),
        }
        for name, (_, changes) in refused.items():
            with self.subTest(case=name), self.assertRaises(MbError):
                self.received(self.raw, **changes)
        for name, raw in {"not canonical": self.raw.replace(b",", b", ", 1), "no newline": self.raw[:-1],
                          "two records": self.raw + self.raw, "a selection": b'{"kind":"mod-base.ci.selection"}\n'
                          }.items():
            with self.subTest(case=name), self.assertRaises(MbError):
                self.received(raw)

    def test_a_lane_receives_its_own_entry_and_the_records_digest(self) -> None:
        lane = self.world.plan["lanes"][1]["id"]
        value = scope.lane_scope_value(self.record, lane)
        self.assertEqual(strict_loads(value.encode(), label="lane scope", max_bytes=1 << 16),
                         {"lane_id": lane, "scenarios": OBLIGATIONS[:1], "scope_sha256": canonical_sha256(self.record)})
        self.assertNotIn("\n", value)
        self.assertIsNone(scope.lane_scope_value(None, lane))
        with self.assertRaises(MbError):
            scope.lane_scope_value(self.record, "lane-zz")

    def test_every_reason_has_its_words_and_no_other_is_given(self) -> None:
        self.assertEqual(len(scope.FULL_SCOPE_REASONS), 16)
        for reason, words in scope.FULL_SCOPE_REASONS.items():
            self.assertRegex(reason, r"^[a-z]+(?:-[a-z]+)+$")
            self.assertEqual(scope.FullScope(reason, words).detail, words)
        with self.assertRaises(MbError):
            scope.FullScope("narrowed", "not a reason for a full scope")


class ScopeCommandTests(unittest.TestCase):
    """``ci scope`` through the real parser, with the job and its hook faked: what it writes."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state"
        self.output = Path(directory.name) / "github-output"
        self.world = World()
        record = {"producer": "packaged", "event": "pull_request_target", "workflow_path": PACKAGED,
                  "controller_tree": h.CONTROLLER_TREE, "subject": h.subject()}
        identity.write_subject(self.state, record)
        identity.write_state_record(self.state, grammar.CI_PLAN_NAME, canonical_json(self.world.plan))
        config = dataclasses.replace(load_build_config(h.MOD, repository=h.REPOSITORY), data=self.world.config)
        self.job = lifecycle.Job(self.state, identity.read_subject(self.state), config, None, h.MOD, "sha256:" + "4" * 64,
                                 RUN_ID, RUN_ATTEMPT)
        self.budgets: list[int] = []

    def run_command(self) -> tuple[int, str, dict[str, str]]:
        def client(invocation, *, max_requests, writable=False):
            self.budgets.append(max_requests)
            return self.world.api

        arguments = argparse.Namespace(repo=h.MOD, config=None, state=self.state, github_output=self.output)
        stdout = io.StringIO()
        with mock.patch.object(lifecycle, "open_job", return_value=self.job), \
                mock.patch.object(lifecycle, "open_worker", return_value=None), \
                mock.patch.object(lifecycle, "derive_scope", lambda job, worker, plan, value, log: self.world.derive(value)), \
                mock.patch.object(commands, "api_client", client), \
                mock.patch.object(commands_scope.runtime, "build_invocation", return_value=None), \
                redirect_stdout(stdout):
            code = commands_scope.run_scope(arguments)
        written = dict(line.split("=", 1) for line in self.output.read_text(encoding="utf-8").splitlines())
        return code, stdout.getvalue(), written

    def test_a_narrowed_scope_is_written_and_output_in_eleven_budgeted_requests(self) -> None:
        code, stdout, written = self.run_command()
        self.assertEqual((code, self.budgets, self.world.api.request_count),
                         (0, [limits.MAX_CI_SCOPE_REQUESTS], DIRECT_REQUESTS))
        raw = identity.read_state_record(self.state, scope.SCOPE_NAME, max_bytes=limits.MAX_CI_SCOPE_BYTES)
        record = strict_loads(raw, label="scope", max_bytes=limits.MAX_CI_SCOPE_BYTES)
        self.assertEqual(raw, canonical_json(record))
        self.assertEqual(written, {"scope": raw.decode().rstrip("\n"), "reason": "narrowed"})
        self.assertEqual(stdout, f"scope: 2 of 4 obligations in 2 lanes, 1 changed paths against {h.CONTROLLER_SHA}; "
                                 f"scope {canonical_sha256(record)}\n")
        # The lane job keeps the same bytes, and its protected hooks find their lane's entry.
        self.assertEqual(lifecycle.read_scope(self.job, self.world.plan), record)

    def test_a_full_scope_outputs_its_reason_and_keeps_nothing(self) -> None:
        self.world.changed({"scripts/ci/mod-base-build.json": BLOB["config-changed"]})
        code, stdout, written = self.run_command()
        self.assertEqual((code, written), (0, {"scope": "", "reason": "selection-input-changed"}))
        self.assertTrue(stdout.startswith("scope: every lane runs every obligation: the pull request changes"))
        self.assertFalse((self.state / scope.SCOPE_NAME).exists())
        self.assertIsNone(lifecycle.read_scope(self.job, self.world.plan))


if __name__ == "__main__":
    unittest.main()
