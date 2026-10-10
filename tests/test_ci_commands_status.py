"""``ci gate-status`` end to end: which state each protected gate may show on a pull request head.

A copy of the synthetic mod with an activation manifest, a private state directory, literal job
listings and real tested-record ZIPs on the real filesystem (Linux). Only the GitHub API is faked.
The status job issues the command twice: ``--settle`` first, which answers without a plan or says
that it cannot, and then, after ``ci subject`` and ``ci plan``, the evaluation that verifies.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mod_base.build_ci import commands_status, identity, status
from mod_base.build_ci.config import load_build_config
from mod_base.build_ci.protocol import plan_sha256
from mod_base.errors import MbError
from mod_base.github.api import ApiError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from tests import ci_mod_harness as h
from tests.helpers import ci_api_run, ci_failed_jobs_rerun, ci_graph_jobs, ci_run_descriptor, ci_run_gate
from tests.test_ci_build_selection import later_run
from tests.test_ci_commands_packaged import JobWorld, run_ci, synthetic_plan
from tests.test_ci_gate_timeline import PACKAGED_GATE, add_gate
from tests.test_ci_gate_transport import reseal
from tests.test_ci_transport import ASSEMBLE, GATE, build_archive, record_archive

BUILD_LISTING = f"/repos/{h.REPOSITORY}/actions/workflows/mod-base-build.yml/runs"
PACKAGED_LISTING = f"/repos/{h.REPOSITORY}/actions/workflows/mod-base-packaged-e2e.yml/runs"
PULL = f"/repos/{h.REPOSITORY}/pulls/7"
CONTEXTS = {"build": "Synthetic / Build and verify", "packaged": "Synthetic / Packaged E2E gate"}
VERIFIED = {"build": "the newest Build run is complete and its gate is verified",
            "packaged": "the newest packaged E2E run is complete and its gate is verified"}
WAITING = {"build": "waiting: no Build run exists for this head yet",
           "packaged": "waiting: no packaged E2E run exists for this head yet"}
RUNNING = {"build": "waiting: the newest Build run is in progress",
           "packaged": "waiting: the newest packaged E2E run is in progress"}
UNPLANNED = {"build": "the newest Build run cannot be verified: this job derived no protected plan",
             "packaged": "the newest packaged E2E run cannot be verified: this job derived no protected plan"}
DRAFT = "deferred: the pull request is a draft"


def run_url(run_id: int) -> str:
    return f"https://github.com/{h.REPOSITORY}/actions/runs/{run_id}"


def activated(directory: Path, mode: str = "shared-build-and-e2e", rollback_from: str | None = None) -> Path:
    """A copy of the synthetic mod that has adopted the shared gates in ``mode``."""

    mod = h.materialize(Path(tempfile.mkdtemp(dir=directory)) / "mod")
    manifest = {"kind": "mod-base.ci.activation", "schema_version": 1, "repository": h.REPOSITORY,
                "profile": "quick-skin", "mode": mode, "rollback_from": rollback_from}
    (mod / "site" / "mod-base-build-activation.json").write_bytes(h.pretty(manifest))
    return mod


class StatusWorld(JobWorld):
    """A pull request of the synthetic mod as the status caller's ``evaluate`` job sees it."""

    def __init__(self, directory: Path, **options: Any) -> None:
        super().__init__(directory, caller="status", **options)
        self.environment["GITHUB_EVENT_NAME"] = "workflow_run"
        self.documents: dict[str, Any] = {}
        self.seals: dict[str, Any] = {}

    def planned(self, name: str = "state", plan: dict[str, Any] | None = None) -> Path:
        """The state directory of an evaluate job that derived the plan."""

        state = self.directory / name
        identity.create_state(state)
        identity.write_state_record(state, grammar.CI_PLAN_NAME, canonical_json(self.plan if plan is None else plan))
        return state

    def gated(self, *, packaged: bool = True) -> "StatusWorld":
        """Finish the Build run with its sealed gate and, unless told otherwise, the packaged run."""

        self.build()
        add_gate(self, "build", "full")
        if packaged:
            self.add_run("packaged", "packaged-pull-request")
            add_gate(self, "packaged", "pull-request")
        return self

    def later_packaged(self, *, run_id: int = 45, listing: str | None = None, **changes: Any) -> None:
        """A newer packaged run for the same head; by default it has failed."""

        run = ci_api_run(self.plan, "packaged", **{"id": run_id, "created_at": "2026-10-07T11:00:00Z",
                                                   "conclusion": "failure", **changes})
        self.runs[run_id] = run
        self.api.add_run(run)
        if listing is not None:
            self.api.add_jobs(run_id, run["run_attempt"], [{**job, "run_id": run_id} for job in ci_graph_jobs(listing)])

    def rebuilt_generation(self, run_id: int = 44) -> dict[str, Any]:
        """A second complete Build run of the same head, newer than run 42, with its own bundle
        and its own sealed gate; returns the descriptor of that bundle."""

        run = {**self.runs[42], "id": run_id, "created_at": "2026-10-07T11:00:00Z"}
        self.runs[run_id] = run
        self.api.add_run(run)
        self.api.add_jobs(run_id, 2, [{**job, "run_id": run_id} for job in ci_graph_jobs("build-full")])
        record = {**ci_run_descriptor(self.plan, "build", "full", "build")["producer"], "run_id": run_id}
        data, _ = build_archive(self.plan, record)
        bundle = self.describe("build", "full", "build", data, artifact_id=110)
        bundle["producer"]["run_id"] = run_id
        bundle["artifact"]["name"] = grammar.ci_artifact_name("build", run_id, 2)
        self.publish(bundle, data)
        document = ci_run_gate(self.plan, "build", "full")
        document["producer"]["run_id"] = run_id
        document["artifacts"] = [copy.deepcopy(bundle)]
        data = record_archive(document)
        seal = self.describe("build", "full", "tested-build", data, artifact_id=210)
        seal["producer"]["run_id"] = run_id
        seal["artifact"]["name"] = grammar.ci_artifact_name("tested", run_id, 2, "build")
        self.publish(seal, data)
        return bundle


class GateStatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.output = self.directory / "github-output"
        self.mod = activated(self.directory)

    def world(self, **options: Any) -> StatusWorld:
        # The plan of a job belongs to the checkout it runs on: the activation manifest is policy.
        return StatusWorld(Path(tempfile.mkdtemp(dir=self.directory)), **{"mod": self.mod, **options})

    def evaluate(self, world: StatusWorld, state: Path, *, pr: str = "7", mod: Path | None = None,
                 environment: dict[str, str] | None = None) -> tuple[int, str, bytes]:
        if self.output.exists():
            self.output.unlink()
        return run_ci(self, world, state, "gate-status", "--pr", pr, "--github-output", str(self.output),
                      mod=self.mod if mod is None else mod, environment=environment)

    def intents(self, world: StatusWorld, state: Path, **options: Any) -> dict[str, Any]:
        """Run the command, require a clean read-only success and return its document."""

        code, stderr, stdout = self.evaluate(world, state, **options)
        self.assertEqual((code, stderr), (0, ""))
        document = strict_loads(stdout, label="status intents", max_bytes=limits.MAX_CI_RECORD_BYTES)
        self.assertEqual(stdout, canonical_json(document))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "intents=" + stdout.decode("utf-8"))
        self.assertEqual((document["repository"], document["pr_number"]), (h.REPOSITORY, 7))
        for intent in document["gates"].values():
            self.assertEqual(sorted(intent), ["context", "description", "state", "target_url"])
            self.assertIn(intent["state"], ("success", "pending", "failure"))
            self.assertLessEqual(len(intent["description"]), 140)
            self.assertNotRegex(intent["description"], "[\x00-\x1f\x7f]")
        self.assertEqual(world.api.mutations, [])
        self.assertEqual(world.budgets[-1], limits.MAX_CI_GATE_STATUS_REQUESTS)
        self.assertEqual(sorted(path.name for path in state.iterdir() if path.name != grammar.CI_PLAN_NAME), [])
        return document

    def states(self, document: dict[str, Any]) -> dict[str, tuple[str, str, str | None]]:
        return {gate: (intent["state"], intent["description"], intent["target_url"])
                for gate, intent in document["gates"].items()}

    def refused(self, world: StatusWorld, state: Path, message: str, **options: Any) -> str:
        code, stderr, stdout = self.evaluate(world, state, **options)
        self.assertNotEqual(code, 0)
        self.assertIn(message, stderr)
        self.assertEqual(stdout, b"")
        self.assertFalse(self.output.exists())
        self.assertEqual(world.api.mutations, [])
        return stderr


class GateStatusTests(GateStatusTestCase):
    def test_both_green_gates_are_success_on_the_pull_request_head_within_budget(self) -> None:
        world = self.world().gated()
        document = self.intents(world, world.planned())
        self.assertEqual(document, {
            "repository": h.REPOSITORY, "pr_number": 7, "target_sha": h.HEAD_SHA,
            "gates": {gate: {"context": CONTEXTS[gate], "state": "success", "description": VERIFIED[gate],
                             "target_url": run_url(run_id)} for gate, run_id in (("build", 42), ("packaged", 43))}})
        self.assertEqual(list(document["gates"]), ["build", "packaged"])
        # The pull request and both listings (3), the Build run with its jobs, record listing and
        # receipt read (18), the packaged run likewise with the Build it consumed (21), and the
        # pull request and both listings once more (3).
        self.assertEqual(world.api.request_count, 45)
        self.assertLess(world.api.request_count, 60)
        self.assertLessEqual(limits.MAX_CI_GATE_STATUS_REQUESTS, 100)

    def test_a_green_build_with_a_missing_or_running_packaged_run_is_success_and_pending(self) -> None:
        world = self.world().gated(packaged=False)
        self.assertEqual(self.states(self.intents(world, world.planned())),
                         {"build": ("success", VERIFIED["build"], run_url(42)),
                          "packaged": ("pending", WAITING["packaged"], None)})
        for status_ in ("queued", "in_progress", "waiting", "requested", "pending"):
            world = self.world().gated(packaged=False)
            world.add_run("packaged", "packaged-pull-request", status=status_, conclusion=None)
            with self.subTest(status=status_):
                self.assertEqual(self.states(self.intents(world, world.planned())),
                                 {"build": ("success", VERIFIED["build"], run_url(42)),
                                  "packaged": ("pending", RUNNING["packaged"], run_url(43))})

    def test_no_run_and_a_running_build_are_pending(self) -> None:
        world = self.world()
        document = self.intents(world, world.planned())
        self.assertEqual(self.states(document), {gate: ("pending", WAITING[gate], None) for gate in CONTEXTS})
        self.assertEqual((document["target_sha"], world.api.request_count), (h.HEAD_SHA, 6))
        world = self.world()
        world.add_run("build", "build-full", status="in_progress", conclusion=None)
        world.add_run("packaged", "packaged-pull-request", status="queued", conclusion=None)
        self.assertEqual(self.states(self.intents(world, world.planned())),
                         {"build": ("pending", RUNNING["build"], run_url(42)),
                          "packaged": ("pending", RUNNING["packaged"], run_url(43))})
        # The listing still shows the finished attempt while a rerun of the same run has started.
        world = self.world().gated()
        world.api.during_listing(BUILD_LISTING, lambda: world.set_run(42, run_attempt=3, status="queued",
                                                                     conclusion=None))
        self.refused(world, world.planned(), "newest Build run changed between the start of the command")

    def test_a_newer_failed_run_is_a_failure_whatever_an_older_run_proved(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "skipped", "neutral"):
            world = self.world().gated()
            later_run(world, conclusion=conclusion)
            with self.subTest(gate="build", conclusion=conclusion):
                self.assertEqual(self.states(self.intents(world, world.planned())),
                                 {"build": ("failure", "the newest Build run failed or was cancelled", run_url(44)),
                                  "packaged": ("failure", "the Build gate of this head did not pass", run_url(43))})
            world = self.world().gated()
            world.later_packaged(conclusion=conclusion)
            with self.subTest(gate="packaged", conclusion=conclusion):
                self.assertEqual(self.states(self.intents(world, world.planned())),
                                 {"build": ("success", VERIFIED["build"], run_url(42)),
                                  "packaged": ("failure", "the newest packaged E2E run failed or was cancelled",
                                               run_url(45))})

    def test_a_newer_run_in_progress_is_pending_whatever_an_older_run_proved(self) -> None:
        world = self.world().gated()
        later_run(world, status="in_progress", conclusion=None)
        self.assertEqual(self.states(self.intents(world, world.planned())),
                         {"build": ("pending", RUNNING["build"], run_url(44)),
                          "packaged": ("pending", "waiting: the Build gate of this head is not verified yet",
                                       run_url(43))})

    def test_a_head_that_moved_since_the_runs_has_no_run_of_its_own(self) -> None:
        world = self.world().gated()
        head, merge = "9" * 40, "7" * 40
        moved = {**world.pr, "head": {**world.pr["head"], "sha": head}, "merge_commit_sha": merge}
        h.seed_pull_request(world.api, moved)
        world.api.add_commit(merge, "5" * 40, parents=[h.CONTROLLER_SHA, head])
        plan = synthetic_plan({**h.subject(), "head_sha": head, "tested_sha": merge, "tested_tree": "5" * 40,
                               "tested_parents": [h.CONTROLLER_SHA, head]}, self.mod)
        document = self.intents(world, world.planned(plan=plan))
        self.assertEqual(document["target_sha"], head)
        self.assertEqual(self.states(document), {gate: ("pending", WAITING[gate], None) for gate in CONTEXTS})
        # A plan that was derived before the head moved describes a generation that is gone.
        self.refused(world, world.planned("stale"), "the pull request moved after this job planned")
        # Without a plan the answer is the same: the green runs belong to another commit.
        document = self.intents(world, self.directory / "unplanned")
        self.assertEqual((document["target_sha"], self.states(document)),
                         (head, {gate: ("pending", WAITING[gate], None) for gate in CONTEXTS}))

    def test_a_base_that_moved_since_the_runs_makes_them_failures(self) -> None:
        world = self.world().gated()
        controller, merge = "9" * 40, "7" * 40
        world.api.set_branch(h.BRANCH, controller, "6" * 40)
        world.api.add_commit(merge, "5" * 40, parents=[controller, h.HEAD_SHA])
        h.seed_pull_request(world.api, {**world.pr, "base": {**world.pr["base"], "sha": controller},
                                        "merge_commit_sha": merge})
        plan = synthetic_plan({**h.subject(), "base_sha": controller, "controller_sha": controller,
                               "tested_sha": merge, "tested_tree": "5" * 40,
                               "tested_parents": [controller, h.HEAD_SHA]}, self.mod)
        environment = {**world.environment, "GITHUB_SHA": controller}
        states = self.states(self.intents(world, world.planned(plan=plan), environment=environment))
        for gate, run_id in (("build", 42), ("packaged", 43)):
            self.assertEqual(states[gate][0::2], ("failure", run_url(run_id)))
            self.assertIn("did not execute the admitted controller commit", states[gate][1])
        # A job that still executes the old controller holds the plan of a generation that is gone.
        self.refused(world, world.planned("old"), "the pull request moved after this job planned")

    def test_a_draft_is_pending_and_never_reads_a_run(self) -> None:
        for planned in (True, False):
            world = self.world().gated()
            h.seed_pull_request(world.api, {**world.pr, "draft": True})
            state = world.planned() if planned else self.directory / "draft"
            with self.subTest(planned=planned):
                document = self.intents(world, state)
                self.assertEqual(self.states(document), {gate: ("pending", DRAFT, None) for gate in CONTEXTS})
                self.assertEqual((document["target_sha"], world.api.request_count), (h.HEAD_SHA, 2))

    def test_a_newest_run_that_is_a_draft_deferral_is_pending_not_success_or_failure(self) -> None:
        world = self.world().gated()
        later_run(world, listing="build-deferred", conclusion="success")
        world.later_packaged(listing="packaged-deferred", conclusion="success")
        self.assertEqual(self.states(self.intents(world, world.planned())),
                         {"build": ("pending", "deferred: the newest Build run was a draft deferral", run_url(44)),
                          "packaged": ("pending", "deferred: the newest packaged E2E run was a draft deferral",
                                       run_url(45))})
        world = self.world()
        world.add_run("build", "build-deferred")
        world.add_run("packaged", "packaged-deferred")
        states = self.states(self.intents(world, world.planned()))
        self.assertEqual({gate: state[0] for gate, state in states.items()},
                         {"build": "pending", "packaged": "pending"})
        # The graph of the other caller's deferral is no deferral of this gate.
        world = self.world().gated()
        later_run(world, listing="packaged-deferred", conclusion="success")
        self.assertEqual(self.states(self.intents(world, world.planned()))["build"][0], "failure")

    def test_a_packaged_run_that_consumed_another_build_is_a_failure(self) -> None:
        world = self.world().gated()
        newer = world.rebuilt_generation()
        self.assertNotEqual(world.documents["packaged"]["owning_build"], newer)
        self.assertEqual(self.states(self.intents(world, world.planned())),
                         {"build": ("success", VERIFIED["build"], run_url(44)),
                          "packaged": ("failure", "the packaged E2E run consumed another Build than the one the "
                                                  "Build gate sealed", run_url(43))})

    def test_a_tested_record_whose_bytes_or_binding_differ_is_a_failure(self) -> None:
        def flipped(world: StatusWorld, gate: str) -> None:
            seal = world.seals[gate]["artifact"]
            data = bytearray(world.archives[seal["id"]])
            data[len(data) // 2] ^= 0xFF
            world.api.add_artifact(world.records[seal["id"]], bytes(data))

        def other_digest(world: StatusWorld, gate: str) -> None:
            world.set_artifact(world.seals[gate]["artifact"]["id"], digest="sha256:" + "f" * 64)

        def other_plan(world: StatusWorld, gate: str) -> None:
            world.documents[gate]["plan_sha256"] = "f" * 64
            reseal(world, gate)

        def other_report(world: StatusWorld, gate: str) -> None:
            world.documents[gate]["native_receipts"][0]["native_contract_sha256"] = "f" * 64
            reseal(world, gate)

        def expired(world: StatusWorld, gate: str) -> None:
            world.set_artifact(world.seals[gate]["artifact"]["id"], expired=True)

        def missing(world: StatusWorld, gate: str) -> None:
            world.set_artifact(world.seals[gate]["artifact"]["id"], name=grammar.ci_artifact_name("reuse", 42, 2))

        cases = {"bytes": (flipped, "download length or SHA-256 differs"),
                 "digest": (other_digest, "download length or SHA-256 differs"),
                 "plan": (other_plan, ""), "receipts": (other_report, ""), "expired": (expired, "expired artifact"),
                 "missing": (missing, "lacks one unique tested record")}
        for name, (change, message) in cases.items():
            for gate in CONTEXTS:
                world = self.world().gated()
                change(world, gate)
                states = self.states(self.intents(world, world.planned()))
                with self.subTest(case=name, gate=gate):
                    self.assertEqual(states[gate][0], "failure")
                    self.assertIn(message, states[gate][1])
                    if gate == "build":
                        self.assertEqual(states["packaged"],
                                         ("failure", "the Build gate of this head did not pass", run_url(43)))
                    else:
                        self.assertEqual(states["build"], ("success", VERIFIED["build"], run_url(42)))

    def test_a_graph_an_attempt_or_a_pin_that_is_not_the_exact_one_is_a_failure(self) -> None:
        def mixed(world: StatusWorld) -> None:
            world.job(42, ASSEMBLE)["run_attempt"] = 1
            world.set_jobs(42, world.jobs[42])

        def unsealed(world: StatusWorld) -> None:
            job = world.job(42, GATE)
            job["steps"] = [step for step in job["steps"] if step["name"] != "Validate frozen native exports"]
            world.set_jobs(42, world.jobs[42])

        def repinned(world: StatusWorld) -> None:
            entry = world.runs[42]["referenced_workflows"][1]
            entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)
            world.api.add_run(world.runs[42])

        def carried(world: StatusWorld) -> None:
            # K7 canary: v1.1.1 published success for the attempt 2 of run 38032224931.
            world.runs[42] = ci_failed_jobs_rerun(world.api, world.runs[42], world.jobs[42])

        cases = {"extra job": lambda w: w.set_jobs(42, ci_graph_jobs("build-full-extra-job")),
                 "missing job": lambda w: w.set_jobs(42, ci_graph_jobs("build-full-missing-job")),
                 "reuse graph": lambda w: w.set_jobs(42, ci_graph_jobs("build-reuse")),
                 "mixed attempts": mixed, "failed-jobs-only rerun": carried, "unsealed gate": unsealed,
                 "kit pin": repinned}
        for name, change in cases.items():
            world = self.world().gated()
            change(world)
            states = self.states(self.intents(world, world.planned()))
            with self.subTest(case=name):
                self.assertEqual((states["build"][0], states["build"][2]), ("failure", run_url(42)))
                self.assertTrue(states["build"][1].startswith("the newest Build run was rejected: "), states["build"])
                self.assertEqual(states["packaged"][0], "failure")
        world = self.world().gated()
        world.job(43, PACKAGED_GATE)["run_attempt"] = 1
        world.set_jobs(43, world.jobs[43])
        states = self.states(self.intents(world, world.planned()))
        self.assertEqual((states["build"][0], states["packaged"][0]), ("success", "failure"))

    def test_without_a_plan_no_finished_run_can_be_verified(self) -> None:
        world = self.world().gated()
        state = self.directory / "created"
        document = self.intents(world, state)
        self.assertEqual(self.states(document), {"build": ("failure", UNPLANNED["build"], run_url(42)),
                                                 "packaged": ("failure", UNPLANNED["packaged"], run_url(43))})
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(world.api.request_count, 8)  # no job, artifact or record is read
        # A state that exists without a plan record behaves the same.
        self.assertEqual(self.states(self.intents(world, state)), self.states(document))

    def test_a_plan_of_another_pull_request_controller_or_policy_is_refused(self) -> None:
        world = self.world().gated()
        plans = {"pull request": synthetic_plan({**h.subject(), "pr_number": 8}, self.mod),
                 "policy": synthetic_plan(h.subject(), self.mod), "profile": synthetic_plan(h.subject(), self.mod)}
        plans["policy"]["identity"]["policy_sha256"] = "f" * 64
        plans["profile"]["profile"] = "block-pops"
        for plan in plans.values():
            plan["plan_sha256"] = plan_sha256(plan)
        for name, plan in plans.items():
            with self.subTest(plan=name):
                self.refused(world, world.planned(name.replace(" ", "-"), plan=plan),
                             "not the protected plan of this pull request and controller")
        self.refused(world, world.planned("moved-controller"),
                     "not the protected plan of this pull request and controller",
                     environment={**world.environment, "GITHUB_SHA": "9" * 40})
        state = self.directory / "junk"
        identity.create_state(state)
        identity.write_state_record(state, grammar.CI_PLAN_NAME, b"{}\n")
        self.refused(world, state, "")
        self.assertEqual(world.api.request_count, 4)  # each mismatch is found after the pull request is read

    def test_shadow_mode_suffixes_both_contexts_and_shared_build_evaluates_the_build_alone(self) -> None:
        world = self.world().gated()
        shadow = {gate: context + " (shadow)" for gate, context in CONTEXTS.items()}
        cases = {("shadow", None): shadow, ("shared-build-and-e2e", None): CONTEXTS,
                 ("shared-build", None): {"build": CONTEXTS["build"]},
                 ("reviewed-rollback", "shadow"): shadow, ("reviewed-rollback", "shared-build-and-e2e"): CONTEXTS,
                 ("reviewed-rollback", "shared-build"): {"build": CONTEXTS["build"]}}
        for index, ((mode, rollback_from), contexts) in enumerate(cases.items()):
            mod = activated(self.directory, mode, rollback_from)
            world = self.world(mod=mod).gated()
            document = self.intents(world, world.planned(f"state-{index}"), mod=mod)
            with self.subTest(mode=mode, rollback_from=rollback_from):
                self.assertEqual({gate: intent["context"] for gate, intent in document["gates"].items()}, contexts)
                self.assertEqual({intent["state"] for intent in document["gates"].values()}, {"success"})
        disabled = activated(self.directory, "disabled")
        world = self.world(mod=disabled).gated()
        self.refused(world, world.planned("disabled"), "manages no gate status caller", mod=disabled)
        self.refused(world, world.planned("unconfigured"), "", mod=h.MOD)  # a Build config without a manifest

    def test_a_change_during_the_evaluation_produces_no_intent(self) -> None:
        changes = {"draft": lambda w: h.seed_pull_request(w.api, {**w.pr, "draft": True}),
                   "head": lambda w: h.seed_pull_request(w.api, {**w.pr, "head": {**w.pr["head"], "sha": "9" * 40}}),
                   "merge": lambda w: h.seed_pull_request(w.api, {**w.pr, "merge_commit_sha": "9" * 40}),
                   "new Build run": lambda w: later_run(w, status="queued", conclusion=None),
                   "new packaged run": lambda w: w.later_packaged(run_id=46, status="queued", conclusion=None)}
        for name, change in changes.items():
            world = self.world().gated()
            world.api.during_listing(PACKAGED_LISTING, lambda: change(world))
            with self.subTest(change=name):
                self.refused(world, world.planned(), "changed between the start of the command and its effect")

    def test_an_api_failure_or_a_spent_budget_is_never_a_state(self) -> None:
        world = self.world().gated()
        failure = ApiError("GitHub API GET failed: HTTP 502", status=502, method="GET", path="/")
        original = world.api.get_json
        paths = (PULL, BUILD_LISTING, f"/repos/{h.REPOSITORY}/actions/runs/42",
                 f"/repos/{h.REPOSITORY}/actions/runs/42/attempts/2/jobs",
                 f"/repos/{h.REPOSITORY}/actions/artifacts/200")
        for index, path in enumerate(paths):
            def get(target, _path=path, **kwargs):
                if target == _path:
                    raise failure
                return original(target, **kwargs)

            with self.subTest(path=path), patch.object(world.api, "get_json", side_effect=get):
                self.refused(world, world.planned(f"state-{index}"), "github-api")
        for budget in (1, 5, 20, 44):
            world = self.world(max_requests=budget).gated()
            with self.subTest(budget=budget):
                self.refused(world, world.planned(), "request-budget")
                self.assertEqual(world.api.request_count, budget)

    def test_a_closed_foreign_or_unknown_pull_request_is_refused(self) -> None:
        world = self.world().gated()
        fork = {**world.pr["head"], "repo": {"full_name": "fork/synthetic-mod"}}
        for name, pull in (("closed", {**world.pr, "state": "closed"}), ("fork", {**world.pr, "head": fork}),
                           ("number", {**world.pr, "number": 8}), ("malformed", {**world.pr, "draft": None})):
            world.api.add_response(PULL, pull)
            with self.subTest(pull=name):
                self.refused(world, world.planned(name), "")
        self.refused(world, world.planned("absent"), "github-not-found", pr="9")

    def test_malformed_flags_are_usage_errors_before_any_request(self) -> None:
        world = self.world().gated()
        state = world.planned()
        for pr in ("0", "-1", "07", "seven", ""):
            code, stderr, _ = self.evaluate(world, state, pr=pr)
            with self.subTest(pr=pr):
                self.assertEqual(code, 2)
                self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        code, stderr, _ = run_ci(self, world, state, "gate-status", "--pr", "7", mod=self.mod)
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: usage: "), stderr)
        self.assertEqual((world.api.request_count, world.budgets), (0, []))


class GateSettleTests(GateStatusTestCase):
    """``ci gate-status --settle``: the first call of the status job, before any subject or plan."""

    def settle(self, world: StatusWorld, *, state: Path | None = None, **options: Any) -> tuple[int, str, bytes]:
        if self.output.exists():
            self.output.unlink()
        self.state = self.directory / "unborn" if state is None else state
        return run_ci(self, world, self.state, "gate-status", "--pr", options.pop("pr", "7"), "--settle",
                      "--github-output", str(self.output), mod=options.pop("mod", self.mod), **options)

    def settled(self, world: StatusWorld, *, requests: int, **options: Any) -> dict[str, Any]:
        """Require a settled answer: the document, on standard output and as the output ``intents``."""

        code, stderr, stdout = self.settle(world, **options)
        self.assertEqual((code, stderr), (0, ""))
        document = strict_loads(stdout, label="status intents", max_bytes=limits.MAX_CI_RECORD_BYTES)
        self.assertEqual(stdout, canonical_json(document))
        self.assertEqual(self.output.read_text(encoding="utf-8"), "settled=true\nintents=" + stdout.decode("utf-8"))
        self.assertNotIn("success", {intent["state"] for intent in document["gates"].values()})
        self.assertFalse(self.state.exists(), "`ci subject` creates the state: the first call leaves none")
        self.assertEqual((world.api.request_count, world.api.mutations), (requests, []))
        self.assertEqual(world.budgets[-1], limits.MAX_CI_GATE_STATUS_REQUESTS)
        return document

    def unsettled(self, world: StatusWorld, *, requests: int, **options: Any) -> None:
        """Require the answer that sends the job on to its plan: no document at all."""

        code, stderr, stdout = self.settle(world, **options)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout.decode("utf-8"), commands_status.UNSETTLED)
        self.assertEqual(self.output.read_text(encoding="utf-8"), "settled=false\n")
        self.assertFalse(self.state.exists())
        self.assertEqual((world.api.request_count, world.api.mutations), (requests, []))

    def test_a_draft_is_settled_as_pending_in_two_requests(self) -> None:
        world = self.world().gated()
        h.seed_pull_request(world.api, {**world.pr, "draft": True})
        document = self.settled(world, requests=2)
        self.assertEqual(self.states(document), {gate: ("pending", DRAFT, None) for gate in CONTEXTS})
        self.assertEqual(document["target_sha"], h.HEAD_SHA)

    def test_a_generation_that_has_not_started_or_still_runs_is_settled_as_pending(self) -> None:
        world = self.world()
        document = self.settled(world, requests=6)  # the pull request and both listings, twice
        self.assertEqual(self.states(document), {gate: ("pending", WAITING[gate], None) for gate in CONTEXTS})
        for status_ in ("queued", "in_progress", "waiting", "requested", "pending"):
            world = self.world()
            world.add_run("build", "build-full", status=status_, conclusion=None)
            world.add_run("packaged", "packaged-pull-request", status=status_, conclusion=None)
            with self.subTest(status=status_):
                self.assertEqual(self.states(self.settled(world, requests=6)),
                                 {"build": ("pending", RUNNING["build"], run_url(42)),
                                  "packaged": ("pending", RUNNING["packaged"], run_url(43))})
        # A newer run in progress decides the gate whatever an older run proved.
        world = self.world().gated()
        later_run(world, status="in_progress", conclusion=None)
        world.later_packaged(status="queued", conclusion=None)
        self.assertEqual(self.states(self.settled(world, requests=6)),
                         {"build": ("pending", RUNNING["build"], run_url(44)),
                          "packaged": ("pending", RUNNING["packaged"], run_url(45))})

    def test_a_failed_or_cancelled_newest_run_is_settled_as_a_failure(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out"):
            world = self.world().gated()
            later_run(world, conclusion=conclusion)
            world.later_packaged(conclusion=conclusion)
            with self.subTest(conclusion=conclusion):
                # The pull request and both listings twice, and each failed run once.
                self.assertEqual(self.states(self.settled(world, requests=8)), {
                    "build": ("failure", "the newest Build run failed or was cancelled", run_url(44)),
                    "packaged": ("failure", "the newest packaged E2E run failed or was cancelled", run_url(45))})
        world = self.world()
        world.add_run("build", "build-full", conclusion="failure")
        self.assertEqual(self.states(self.settled(world, requests=7)),
                         {"build": ("failure", "the newest Build run failed or was cancelled", run_url(42)),
                          "packaged": ("pending", WAITING["packaged"], None)})

    def test_a_finished_newest_run_is_left_to_the_plan_and_never_answered(self) -> None:
        # Both gates green: the pull request, both listings and both runs; nothing is read again.
        self.unsettled(self.world().gated(), requests=5)
        # One finished run is enough, whatever the other gate shows.
        self.unsettled(self.world().gated(packaged=False), requests=4)
        world = self.world().gated()
        world.later_packaged(conclusion="failure")
        self.unsettled(world, requests=5)
        world = self.world().gated()
        later_run(world, status="in_progress", conclusion=None)
        self.unsettled(world, requests=4)
        # A draft deferral finished successfully too: only the plan tells it from a verified gate.
        world = self.world()
        world.add_run("build", "build-deferred")
        world.add_run("packaged", "packaged-deferred")
        self.unsettled(world, requests=5)

    def test_the_two_calls_of_one_job_end_in_the_verified_document(self) -> None:
        world = self.world().gated()
        self.unsettled(world, requests=5)
        state = self.state
        # `ci subject --producer status` creates the state the first call left unborn...
        code, stderr, stdout = run_ci(self, world, state, "subject", "--producer", "status", "--pr", "7",
                                      "--github-output", str(self.directory / "subject-output"), mod=self.mod)
        self.assertEqual((code, stderr, stdout), (0, "", b""))
        record = identity.read_subject(state)
        self.assertEqual((record["producer"], record["event"], record["subject"]["pr_number"]),
                         ("status", "workflow_run", 7))
        self.assertEqual(world.api.request_count, 5 + 4)
        # ...`ci plan` writes the plan of that subject, and the second call verifies both gates.
        identity.write_state_record(state, grammar.CI_PLAN_NAME, canonical_json(world.plan))
        code, stderr, stdout = run_ci(self, world, state, "gate-status", "--pr", "7", "--github-output",
                                      str(self.output), mod=self.mod)
        self.assertEqual((code, stderr), (0, ""))
        document = strict_loads(stdout, label="status intents", max_bytes=limits.MAX_CI_RECORD_BYTES)
        self.assertEqual(self.states(document), {gate: ("success", VERIFIED[gate], run_url(run_id))
                                                 for gate, run_id in (("build", 42), ("packaged", 43))})
        self.assertEqual(self.output.read_text(encoding="utf-8"), "settled=false\nintents=" + stdout.decode("utf-8"))
        self.assertEqual(world.api.request_count, 5 + 4 + 45)
        self.assertEqual(world.budgets, [limits.MAX_CI_GATE_STATUS_REQUESTS, limits.MAX_CI_SUBJECT_REQUESTS,
                                         limits.MAX_CI_GATE_STATUS_REQUESTS])

    def test_a_job_that_holds_a_state_has_passed_the_first_call(self) -> None:
        world = self.world().gated()
        for label, state in (("planned", world.planned()), ("empty", self.directory / "empty")):
            if label == "empty":
                identity.create_state(state)
            code, stderr, stdout = self.settle(world, state=state)
            with self.subTest(state=label):
                self.assertEqual((code, stdout), (2, b""))
                self.assertIn("runs before `ci subject`", stderr)
                self.assertFalse(self.output.exists())
        self.assertEqual((world.api.request_count, world.budgets), (0, []))

    def test_a_change_an_api_failure_or_a_refused_pull_request_is_never_an_answer(self) -> None:
        world = self.world()
        world.add_run("build", "build-full", status="in_progress", conclusion=None)
        world.api.during_listing(PACKAGED_LISTING, lambda: world.set_run(42, status="completed", conclusion="success"))
        code, stderr, stdout = self.settle(world)
        self.assertEqual((code, stdout), (2, b""))
        self.assertIn("changed between the start of the command and its effect", stderr)
        self.assertFalse(self.output.exists())
        world = self.world(max_requests=5)
        code, stderr, stdout = self.settle(world)
        self.assertEqual((code, stdout), (2, b""))
        self.assertIn("request-budget", stderr)
        self.assertFalse(self.output.exists())
        world = self.world()
        world.api.add_response(PULL, {**world.pr, "state": "closed"})
        code, _, stdout = self.settle(world)
        self.assertEqual((code, stdout, self.output.exists()), (2, b"", False))
        code, stderr, stdout = self.settle(self.world(), mod=activated(self.directory, "disabled"))
        self.assertEqual((code, stdout, self.output.exists()), (2, b"", False))
        self.assertIn("manages no gate status caller", stderr)

    def test_shadow_mode_and_shared_build_settle_their_own_gates(self) -> None:
        document = self.settled(self.world(), requests=6, mod=activated(self.directory, "shadow"))
        self.assertEqual({gate: intent["context"] for gate, intent in document["gates"].items()},
                         {gate: context + " (shadow)" for gate, context in CONTEXTS.items()})
        # Without a managed packaged caller only the Build gate exists: one listing, read twice.
        document = self.settled(self.world(), requests=4, mod=activated(self.directory, "shared-build"))
        self.assertEqual(self.states(document), {"build": ("pending", WAITING["build"], None)})
        # A finished packaged run of a mod that keeps its own packaged gate is nobody's business here.
        world = self.world()
        world.add_run("packaged", "packaged-pull-request")
        self.settled(world, requests=4, mod=activated(self.directory, "shared-build"))


class SettleGatesTests(unittest.TestCase):
    """``status.settle_gates``: the document without a plan, or None; never a success."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.config = load_build_config(h.MOD, repository=h.REPOSITORY)
        self.activation = {"kind": "mod-base.ci.activation", "schema_version": 1, "repository": h.REPOSITORY,
                           "profile": "quick-skin", "mode": "shared-build-and-e2e", "rollback_from": None}

    def settle(self, world: StatusWorld, **changes: Any) -> dict[str, Any] | None:
        arguments = {"pr_number": 7, "config": self.config, "activation": self.activation,
                     "controller_sha": h.CONTROLLER_SHA, **changes}
        return status.settle_gates(world.api, **arguments)

    def test_the_settled_document_is_the_one_the_full_evaluation_returns(self) -> None:
        def running(world: StatusWorld) -> None:
            world.add_run("build", "build-full", status="in_progress", conclusion=None)

        def failed(world: StatusWorld) -> None:
            world.add_run("build", "build-full", conclusion="failure")
            world.add_run("packaged", "packaged-pull-request", conclusion="cancelled")

        def draft(world: StatusWorld) -> None:
            world.gated()
            h.seed_pull_request(world.api, {**world.pr, "draft": True})

        for change in (lambda world: None, running, failed, draft):
            first, second = StatusWorld(Path(tempfile.mkdtemp(dir=self.directory))), \
                StatusWorld(Path(tempfile.mkdtemp(dir=self.directory)))
            change(first)
            change(second)
            settled = self.settle(first)
            with self.subTest(change=getattr(change, "__name__", "nothing")):
                self.assertIsNotNone(settled)
                self.assertEqual(settled, status.evaluate_gates(
                    second.api, pr_number=7, config=self.config, activation=self.activation,
                    controller_sha=h.CONTROLLER_SHA, plan=second.plan, temporary_root=self.directory))
                self.assertEqual(first.api.request_count, second.api.request_count)

    def test_a_finished_run_is_none_and_no_plan_never_yields_success(self) -> None:
        world = StatusWorld(self.directory).gated()
        self.assertIsNone(self.settle(world))
        self.assertEqual(world.api.request_count, 5)
        # The full evaluation of a job that holds no plan keeps its restrictive answer.
        document = status.evaluate_gates(world.api, pr_number=7, config=self.config, activation=self.activation,
                                         controller_sha=h.CONTROLLER_SHA, plan=None, temporary_root=self.directory)
        self.assertEqual({gate: (intent["state"], intent["description"]) for gate, intent in document["gates"].items()},
                         {gate: ("failure", UNPLANNED[gate]) for gate in CONTEXTS})
        # Even an evaluation that somehow answered success without a plan would be refused.
        green = {"repository": h.REPOSITORY, "pr_number": 7, "target_sha": h.HEAD_SHA,
                 "gates": {"build": {"context": CONTEXTS["build"], "state": "success", "description": "x",
                                     "target_url": None}}}
        with patch.object(status, "_evaluate", return_value=green), \
                self.assertRaisesRegex(MbError, "may never answer success"):
            self.settle(world)

    def test_malformed_arguments_are_refused_before_any_request(self) -> None:
        world = StatusWorld(self.directory)
        for changes in ({"pr_number": 0}, {"pr_number": True}, {"pr_number": "7"}, {"controller_sha": "c" * 39},
                        {"activation": None}, {"activation": {**self.activation, "mode": "disabled"}}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.settle(world, **changes)
        self.assertEqual(world.api.request_count, 0)
        with self.assertRaises(MbError):
            status.evaluate_gates(world.api, pr_number=7, config=self.config, activation=self.activation,
                                  controller_sha=h.CONTROLLER_SHA, plan=None, temporary_root=None)
        self.assertEqual(world.api.request_count, 0)


class GateContextTests(unittest.TestCase):
    def test_contexts_are_the_protected_strings_for_the_managed_gates(self) -> None:
        config = load_build_config(h.MOD, repository=h.REPOSITORY)

        def manifest(mode: str, rollback_from: str | None = None) -> dict[str, Any]:
            return {"kind": "mod-base.ci.activation", "schema_version": 1, "repository": h.REPOSITORY,
                    "profile": "quick-skin", "mode": mode, "rollback_from": rollback_from}

        self.assertEqual(status.gate_contexts(config, manifest("shared-build-and-e2e")), CONTEXTS)
        self.assertEqual(status.gate_contexts(config, manifest("shadow")),
                         {gate: context + status.SHADOW_SUFFIX for gate, context in CONTEXTS.items()})
        self.assertEqual(status.SHADOW_SUFFIX, " (shadow)")
        self.assertEqual(status.gate_contexts(config, manifest("shared-build")), {"build": CONTEXTS["build"]})
        for activation in (None, manifest("disabled")):
            with self.subTest(activation=activation), self.assertRaisesRegex(MbError, "manages no gate status"):
                status.gate_contexts(config, activation)


if __name__ == "__main__":
    unittest.main()
