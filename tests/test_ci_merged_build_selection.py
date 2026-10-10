"""The newest original Build of a merged pull request is selected under its historical source.

Fake API, literal job listings and a real bundle. The original run stays recorded under the pull
request's head commit and branch; only the source admission changes, from the live pull request
to its merged history. One selection is one observation: a consumer repeats it before its effect.
"""

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.protocol import plan_sha256
from mod_base.build_ci.selection import (revalidate_latest_merged_pr_build, select_latest_merged_pr_build,
                                         select_latest_pr_build)
from mod_base.errors import MbError
from mod_base.model import grammar
from tests.helpers import ci_graph_jobs, ci_push_plan
from tests.test_ci_build_selection import LISTING, later_run
from tests.test_ci_merged_gate_transport import PULL, merge
from tests.test_ci_transport import build_world

ARTIFACTS = "/repos/example/mod/actions/runs/42/artifacts"


def merged_build_world(*, same=False):
    """A merged pull request whose original Build run finished with a real complete bundle."""

    return merge(build_world(), same=same)


class MergedBuildSelectionTests(unittest.TestCase):
    def call(self, world, **changes):
        arguments = {"plan": world.plan, "controller_sha": world.controller, "merged_sha": world.merged, **changes}
        return select_latest_merged_pr_build(world.api, **arguments)

    def revalidate(self, world):
        return revalidate_latest_merged_pr_build(world.api, descriptor=world.bundle, plan=world.plan,
                                                 controller_sha=world.controller, merged_sha=world.merged)

    def test_original_controller_attempt_and_descriptor_survive_final_merge(self):
        # Merged source: 3 mutable reads and two commits and two ancestries (one commit when the final
        # merge is the tested merge). Then the listing, the run, its jobs, the bundle listing and record.
        for same, requests in ((False, 12), (True, 11)):
            world = merged_build_world(same=same)
            before = copy.deepcopy((world.plan, world.bundle))
            with self.subTest(same=same):
                with patch.object(world.api, "get_json", wraps=world.api.get_json) as get:
                    result = self.call(world)
                self.assertEqual(result, world.bundle)
                self.assertNotEqual(result["producer"]["api_head_sha"], world.controller)
                self.assertEqual(result["producer"]["api_head_sha"], world.plan["identity"]["head_sha"])
                self.assertEqual(result["identity"]["tested_sha"], world.plan["identity"]["tested_sha"])
                self.assertEqual(world.api.request_count, requests)
                # The original run is still found where GitHub recorded it: under the pull request's head.
                listings = [call for call in get.call_args_list if call.args[0] == LISTING]
                self.assertEqual(len(listings), 1)
                params = listings[0].kwargs["params"]
                self.assertEqual({key: params[key] for key in ("branch", "head_sha", "event")},
                                 {"branch": "feature/example", "head_sha": "1" * 40, "event": "pull_request_target"})
                self.assertNotIn("status", params)
                self.assertIsNone(self.revalidate(world))
                self.assertEqual((world.plan, world.bundle), before)
                self.assertEqual(world.api.mutations, [])
                with self.assertRaises(MbError):  # the pull request is closed: the live route refuses it
                    select_latest_pr_build(world.api, plan=world.plan)

    def test_newest_failed_cancelled_or_pending_never_uses_old_success(self):
        for status, conclusion in (("completed", "failure"), ("completed", "cancelled"),
                                   ("completed", "neutral"), ("queued", None), ("in_progress", None)):
            world = merged_build_world()
            later_run(world, status=status, conclusion=conclusion)
            with self.subTest(status=status, conclusion=conclusion):
                if status == "completed":
                    with self.assertRaisesRegex(MbError, "newest exact Build"):
                        self.call(world)
                else:
                    self.assertIsNone(self.call(world))
                with self.assertRaises(MbError):
                    self.revalidate(world)

    def test_no_original_run_is_absence_not_reuse_authority(self):
        # A run belongs to the pull request only under its head commit, branch, repository and event:
        # the run of another pull request, of a fork, or the push run of the final merge is not one.
        for changes in ({"head_sha": "f" * 40, "head_branch": "feature/other"}, {"head_sha": "f" * 40},
                        {"head_branch": "other"}, {"head_repository": {"full_name": "fork/mod"}},
                        {"event": "pull_request"}, {"event": "push", "head_sha": "b" * 40, "head_branch": "master"}):
            world = merged_build_world()
            world.set_run(42, **changes)
            with self.subTest(changes=changes):
                self.assertIsNone(self.call(world))
                with self.assertRaisesRegex(MbError, "no longer the newest"):
                    self.revalidate(world)

    def test_invalid_original_bindings_reject_before_api(self):
        for changes in ({"controller_sha": True}, {"merged_sha": None}, {"merged_sha": "B" * 40},
                        {"plan": ci_push_plan()}):
            world = merged_build_world()
            with self.subTest(changes=changes), patch.object(world.api, "get_json") as reads, \
                    self.assertRaises(MbError):
                self.call(world, **changes)
            reads.assert_not_called()
        # The producer workflow is the managed Build caller, never a caller's argument.
        world = merged_build_world()
        with patch.object(world.api, "get_json") as reads, self.assertRaises(TypeError):
            self.call(world, workflow_path="arbitrary.py")
        reads.assert_not_called()

    def test_unmerged_wrong_tree_parents_or_current_controller_reject(self):
        for change in ("unmerged", "tree", "parents", "controller"):
            world = merged_build_world()
            identity = world.plan["identity"]
            if change == "unmerged":
                world.pr["merged"] = False
                world.api.add_response(PULL, world.pr)
            elif change == "tree":
                world.api.add_commit(world.merged, "f" * 40, parents=[identity["base_sha"]])
            elif change == "parents":
                world.api.add_commit(identity["tested_sha"], identity["tested_tree"],
                                     parents=list(reversed(identity["tested_parents"])))
            else:
                world.api.set_branch("master", "e" * 40, "f" * 40)
            with self.subTest(change=change), self.assertRaises(MbError):
                self.call(world)

    def test_newest_attempt_kit_graph_and_missing_bundle_reject(self):
        def repin(index):
            def change(world):
                entry = world.runs[42]["referenced_workflows"][index]
                entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "9" * 40, sha="9" * 40)
                world.api.add_run(world.runs[42])
            return change

        changes = {
            # Attempt 3 has no jobs: nothing of the attempt the listing showed is reused.
            "attempt": (lambda world: world.api.during_listing(LISTING, lambda: world.set_run(42, run_attempt=3)), ""),
            "controller": (repin(0), "admitted controller commit"),
            "kit": (repin(1), "pinned kit"),
            "graph": (lambda world: world.set_jobs(42, [{"name": "Unexpected", "status": "completed",
                                                         "conclusion": "success"}]), ""),
            "extra job": (lambda world: world.set_jobs(42, ci_graph_jobs("build-full-extra-job")), ""),
            "missing job": (lambda world: world.set_jobs(42, ci_graph_jobs("build-full-missing-job")), ""),
            "bundle": (lambda world: world.set_artifact(100, name=grammar.ci_artifact_name("build", 42, 1)),
                       "lacks one unique complete bundle"),
        }
        for name, (change, message) in changes.items():
            world = merged_build_world()
            change(world)
            with self.subTest(change=name), self.assertRaisesRegex(MbError, message):
                self.call(world)

    def test_newer_run_and_source_movement_after_the_observation_reject_at_revalidation(self):
        for change in ("run", "source"):
            world = merged_build_world()
            action = ((lambda: later_run(world, status="queued", conclusion=None)) if change == "run"
                      else (lambda: world.api.set_branch("master", "e" * 40, "f" * 40)))
            world.api.during_listing(ARTIFACTS, action)
            with self.subTest(change=change):
                # One selection is one observation: the source and the run listing were read before
                # the change. Repeating it around consumption is what refuses the moved state.
                self.assertEqual(self.call(world), world.bundle)
                with self.assertRaises(MbError):
                    self.revalidate(world)

    def test_plan_replaced_during_the_command_cannot_change_what_was_admitted_live_or_historical(self):
        for historical in (False, True):
            for state in ("success", "pending", "absence"):
                world = merged_build_world() if historical else build_world()
                if state == "pending":
                    later_run(world, status="queued", conclusion=None)
                elif state == "absence":
                    world.set_run(42, head_sha="f" * 40)
                admitted = copy.deepcopy(world.plan)
                expected = copy.deepcopy(world.bundle) if state == "success" else None

                def mutate():
                    world.plan["identity"]["inventory_sha256"] = "f" * 64
                    world.plan["plan_sha256"] = plan_sha256(world.plan)

                world.api.during_listing(LISTING, mutate)
                with self.subTest(historical=historical, state=state):
                    observed = self.call(world) if historical else select_latest_pr_build(world.api, plan=world.plan)
                    self.assertNotEqual(world.plan, admitted)  # the caller's plan did change mid-command
                    self.assertEqual(observed, expected)  # the plan copied on entry is the one that counts
                    if expected is not None:
                        self.assertEqual((observed["identity"], observed["plan_sha256"]),
                                         (admitted["identity"], admitted["plan_sha256"]))

    def test_revalidation_rejects_another_descriptor_and_copies_the_callers_on_entry(self):
        for key, value in (("digest", "sha256:" + "f" * 64), ("id", 104), ("size", 1),
                           ("created_at", "2026-10-07T10:03:01Z")):
            world = merged_build_world()
            world.bundle["artifact"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(MbError, "no longer the newest"):
                self.revalidate(world)
        world = merged_build_world()  # the bundle of an earlier attempt of the same run
        world.bundle["producer"]["run_attempt"] = 1
        world.bundle["artifact"]["name"] = grammar.ci_artifact_name("build", 42, 1)
        with self.assertRaisesRegex(MbError, "no longer the newest"):
            self.revalidate(world)
        # The descriptor is copied on entry: changing the caller's object mid-command decides nothing.
        world = merged_build_world()
        world.api.during_listing(LISTING, lambda: world.bundle["artifact"].update(digest="sha256:" + "f" * 64))
        self.assertIsNone(self.revalidate(world))

    def test_api_failure_is_not_absence_or_old_success(self):
        world = merged_build_world()
        with patch.object(world.api, "get_json", side_effect=MbError("API unavailable")), \
                self.assertRaisesRegex(MbError, "API unavailable"):
            self.call(world)
        with patch.object(world.api, "get_json", side_effect=MbError("API unavailable")), \
                self.assertRaisesRegex(MbError, "API unavailable"):
            self.revalidate(world)


if __name__ == "__main__":
    unittest.main()
