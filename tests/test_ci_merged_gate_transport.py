"""An original pull-request gate is read after merge: real archives, fake API, original identities.

The original runs and artifacts stay recorded under the pull request's head commit and branch;
only the source admission changes, from the live pull request to its merged history.
"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.helpers import ci_graph_jobs, ci_push_plan, ci_run_descriptor
from tests.test_ci_gate_timeline import gate_world
from tests.test_ci_gate_transport import reseal
from tests.test_ci_transport import after_download

PULL = "/repos/example/mod/pulls/7"


def merge(world, *, same=False):
    """Merge the world's pull request: closed and merged as a final commit with the tested tree
    (``same``: as the tested merge commit itself), with the default branch already further on."""

    identity = world.plan["identity"]
    world.merged, world.controller = (identity["tested_sha"] if same else "b" * 40), "c" * 40
    world.pr.update(state="closed", merged=True, merged_at="2026-10-08T10:00:00Z", merge_commit_sha=world.merged)
    world.pr["base"]["sha"] = world.controller  # the API base moves on after the merge
    world.api.add_response(PULL, world.pr)
    if not same:
        world.api.add_commit(world.merged, identity["tested_tree"], parents=[identity["base_sha"]])
    world.api.set_branch("master", world.controller, "d" * 40)
    world.api.add_compare(identity["base_sha"], world.merged, {"status": "ahead", "ahead_by": 2, "behind_by": 0})
    world.api.add_compare(world.merged, world.controller, {"status": "ahead", "ahead_by": 3, "behind_by": 0})
    return world


def merged_gate_world(gate="build"):
    return merge(gate_world(gate))


class MergedGateTransportTests(unittest.TestCase):
    def call(self, world, name, parent, **changes):
        arguments = {"descriptor": world.seals[name], "plan": world.plan, "gate": name,
                     "controller_sha": world.controller, "merged_sha": world.merged, "temporary_root": parent,
                     **changes}
        return transport.download_merged_gate_receipt(world.api, **arguments)

    def refused(self, world, name, message=""):
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "download") as downloads:
            with self.assertRaisesRegex(MbError, message):
                self.call(world, name, Path(directory))
            downloads.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_both_original_gates_keep_producer_identity_and_numeric_id(self):
        # The merged source costs 3 mutable reads and 4 immutable ones (two commits, two ancestries).
        for gate, requests in (("build", 19), ("packaged", 24)):
            world = merged_gate_world(gate)
            before = copy.deepcopy((world.plan, world.documents[gate], world.seals[gate]))
            seal = world.seals[gate]["artifact"]
            with self.subTest(gate=gate), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download", wraps=world.api.download) as downloads:
                result = self.call(world, gate, Path(directory))
                self.assertEqual(result, world.documents[gate])
                self.assertEqual(result["identity"]["controller_sha"], "2" * 40)
                self.assertNotEqual(result["identity"]["controller_sha"], world.controller)
                self.assertEqual(result["producer"]["api_head_sha"], world.plan["identity"]["head_sha"])
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(world.api.request_count, requests)
                # The pull request is closed: the live route no longer admits its source.
                with self.assertRaises(MbError):
                    transport.download_gate_receipt(world.api, descriptor=world.seals[gate], plan=world.plan,
                                                    gate=gate, temporary_root=Path(directory))
            downloads.assert_called_once_with(f"/repos/example/mod/actions/artifacts/{seal['id']}/zip",
                                              max_bytes=seal["size"])
            self.assertEqual((world.plan, world.documents[gate], world.seals[gate]), before)
            self.assertEqual(world.api.mutations, [])

    def test_the_final_merge_may_be_the_tested_merge_itself(self):
        world = merge(gate_world("build"), same=True)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.call(world, "build", Path(directory)), world.documents["build"])

    def test_independent_bindings_kind_and_subject_reject_before_any_read(self):
        world = merged_gate_world()
        packaged = merged_gate_world("packaged")
        push = ci_push_plan()
        cases = [{"controller_sha": False}, {"merged_sha": None}, {"merged_sha": "B" * 40}, {"gate": "reuse"},
                 {"gate": "packaged"}, {"descriptor": packaged.seals["packaged"]},
                 {"descriptor": world.documents["build"]["artifacts"][0]},
                 {"descriptor": {**world.seals["build"], "plan_sha256": "f" * 64}},
                 {"plan": push, "descriptor": ci_run_descriptor(push, "build", "full", "tested", unit_id="build")}]
        for index, changes in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, self.assertRaises(MbError):
                self.call(world, "build", Path(directory), **changes)
            reads.assert_not_called()

    def test_wrong_historical_source_tree_or_current_controller_reject_before_download(self):
        def pull(world, **changes):
            head = changes.pop("head", {})
            world.pr.update(changes)
            world.pr["head"].update(head)
            world.api.add_response(PULL, world.pr)

        mutations = {"unmerged": lambda world: pull(world, merged=False),
                     "open": lambda world: pull(world, state="open"),
                     "draft": lambda world: pull(world, draft=True),
                     "head": lambda world: pull(world, head={"sha": "f" * 40}),
                     "branch": lambda world: pull(world, head={"ref": "other"}),
                     "final": lambda world: pull(world, merge_commit_sha="f" * 40),
                     "tree": lambda world: world.api.add_commit("b" * 40, "f" * 40, parents=["2" * 40]),
                     "controller": lambda world: world.api.set_branch("master", "e" * 40, "f" * 40),
                     "history": lambda world: world.api.add_compare(
                         "b" * 40, "c" * 40, {"status": "diverged", "ahead_by": 1, "behind_by": 1})}
        for name, mutate in mutations.items():
            world = merged_gate_world()
            mutate(world)
            with self.subTest(mutation=name):
                self.refused(world, "build")

    def test_original_runs_stay_recorded_under_the_pull_request_head(self):
        mutations = {"attempt": {"run_attempt": 3},
                     # Neither the final merged commit nor the current controller replaces the original head.
                     "merged head": {"head_sha": "b" * 40, "head_branch": "master"},
                     "controller head": {"head_sha": "c" * 40, "head_branch": "master"},
                     "base head": {"head_sha": "2" * 40, "head_branch": "master"},
                     "failed": {"conclusion": "failure"}, "event": {"event": "push"}}
        for gate in ("build", "packaged"):
            for name, changes in mutations.items():
                world = merged_gate_world(gate)
                world.set_run(world.seals[gate]["producer"]["run_id"], **changes)
                with self.subTest(gate=gate, mutation=name):
                    self.refused(world, gate)
            world = merged_gate_world(gate)
            run_id = world.seals[gate]["producer"]["run_id"]
            for entry in world.runs[run_id]["referenced_workflows"]:
                # The run must have executed the original controller, not the one that is current now.
                entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + "c" * 40, sha="c" * 40)
            world.api.add_run(world.runs[run_id])
            with self.subTest(gate=gate, mutation="current controller"):
                self.refused(world, gate)
            world = merged_gate_world(gate)
            world.set_jobs(run_id, world.jobs[run_id][:-1])
            with self.subTest(gate=gate, mutation="partial graph"):
                self.refused(world, gate, "exact job graph mismatch")

    def test_record_and_source_artifacts_keep_the_original_head_and_stay_available(self):
        for artifact_id in (201, 100, 101, 102):
            for changes in ({"expired": True}, {"digest": "sha256:" + "f" * 64}, {"workflow_run": {"id": 999}},
                            {"workflow_run": {"head_sha": "b" * 40}}, {"workflow_run": {"head_branch": "master"}}):
                world = merged_gate_world("packaged")
                world.set_artifact(artifact_id, **copy.deepcopy(changes))
                with self.subTest(artifact_id=artifact_id, changes=changes), \
                        tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                    self.call(world, "packaged", Path(directory))

    def test_packaged_owning_build_has_its_own_latest_attempt_and_graph(self):
        for name, mutate in (("attempt", lambda world: world.set_run(42, run_attempt=3)),
                             ("deferred", lambda world: world.set_jobs(42, ci_graph_jobs("build-deferred")))):
            world = merged_gate_world("packaged")
            mutate(world)
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
                self.call(world, "packaged", Path(directory))
        world = merged_gate_world("packaged")
        world.documents["packaged"]["owning_build"]["producer"].update(run_id=44)
        world.documents["packaged"]["owning_build"]["artifact"]["name"] = grammar.ci_artifact_name("build", 44, 2)
        reseal(world, "packaged")
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "packaged", Path(directory))

    def test_gate_timeline_and_canonical_record_bytes_cannot_be_bypassed(self):
        world = merged_gate_world()
        world.job(42, "Shared Build / Seal complete Build bundle")["completed_at"] = "2026-10-07T10:04:01Z"
        world.set_jobs(42, world.jobs[42])
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(MbError):
            self.call(world, "build", Path(directory))
        document = merged_gate_world().documents["build"]
        for raw in (canonical_json(document) + b"\n", canonical_json({**document, "extra": True}),
                    canonical_json(document).replace(b'"mode":"full"', b'"mode":"full","mode":"full"')):
            world = reseal(merged_gate_world(), "build", raw=raw)
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(MbError):
                    self.call(world, "build", Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_record_digest_corruption_rejects_before_extraction(self):
        world = merged_gate_world()
        world.api.add_artifact(world.records[200], b"wrong")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MbError, "download length or SHA-256 differs"):
                self.call(world, "build", Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_historical_state_attempt_and_source_movement_after_download_rejects(self):
        def merged_at(world):
            world.pr["merged_at"] = "2026-10-08T10:01:00Z"
            world.api.add_response(PULL, world.pr)

        changes = {"merged_at": merged_at,
                   "controller": lambda world: world.api.set_branch("master", "f" * 40, "e" * 40),
                   "attempt": lambda world: world.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "record": lambda world: world.set_artifact(200, expired=True),
                   "source": lambda world: world.set_artifact(100, expired=True)}
        for name, change in changes.items():
            world = merged_gate_world()
            with self.subTest(change=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world)), self.assertRaises(MbError):
                    self.call(world, "build", Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caller_documents_are_copied_on_entry(self):
        world = merged_gate_world()
        expected = copy.deepcopy(world.documents["build"])
        plan, seal = world.plan, world.seals["build"]
        with tempfile.TemporaryDirectory() as directory, \
                after_download(world.api, lambda: (plan["identity"]["kit"].update(sha="f" * 40),
                                                   seal["artifact"].update(digest="sha256:" + "f" * 64))):
            self.assertEqual(self.call(world, "build", Path(directory)), expected)

    def test_api_errors_propagate_without_fallback_or_residue(self):
        world = merged_gate_world()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "download", side_effect=MbError("API unavailable")):
            with self.assertRaisesRegex(MbError, "API unavailable"):
                self.call(world, "build", Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])
        for target in (PULL, "/git/commits/", "/compare/", "/branches/", "/actions/runs/42"):
            world = merged_gate_world()
            original, failure = world.api.get_json, MbError("unavailable API")

            def get(path, **arguments):
                if target in path:
                    raise failure
                return original(path, **arguments)

            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json", side_effect=get), self.assertRaises(MbError) as caught:
                self.call(world, "build", Path(directory))
            self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
