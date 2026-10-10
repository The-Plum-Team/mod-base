"""Both original gates of a merged pull request are read as one coherent pair.

Real record archives, fake API. Each seal is downloaded once; what can change (the merged pull
request, both runs' latest attempts, every artifact's availability) is read again before the pair
is returned.
"""

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError
from mod_base.model import grammar
from tests.helpers import ci_api_artifact, ci_api_run
from tests.test_ci_gate_timeline import pair_world
from tests.test_ci_gate_transport import reseal
from tests.test_ci_merged_gate_transport import PULL, merge
from tests.test_ci_transport import after_download


def merged_pair_world():
    return merge(pair_world())


def pair_arguments(world, **changes):
    return {"build_descriptor": world.seals["build"], "packaged_descriptor": world.seals["packaged"],
            "plan": world.plan, "controller_sha": world.controller, "merged_sha": world.merged, **changes}


class MergedGatePairTests(unittest.TestCase):
    def call(self, world, parent, **changes):
        return transport.download_merged_gate_pair(world.api, **pair_arguments(world, temporary_root=parent,
                                                                              **changes))

    def test_coherent_pair_reads_each_seal_once_within_the_request_budget(self):
        world = merged_pair_world()
        before = copy.deepcopy((world.plan, world.seals))
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "download", wraps=world.api.download) as downloads, \
                patch.object(world.api, "get_json", wraps=world.api.get_json) as reads:
            result = self.call(world, Path(directory))
            self.assertEqual(result, (world.documents["build"], world.documents["packaged"]))
            self.assertEqual(result[1]["owning_build"], result[0]["artifacts"][0])
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual([call.args[0].split("/")[-2] for call in downloads.call_args_list], ["200", "201"])
        self.assertEqual((world.plan, world.seals), before)
        self.assertEqual(world.api.mutations, [])
        # Start: merged source 7; Build gate 6 (run, jobs, seal, download 2, bundle); packaged gate 6
        # (run, jobs, seal, download 2, lane and results listing: its Build run is already read).
        # Before returning: source 3, two runs, two seals, the bundle and the listing.
        self.assertEqual(world.api.request_count, 28)
        self.assertLess(world.api.request_count, 60)
        paths = [call.args[0] for call in reads.call_args_list]
        for immutable in ("/git/commits/", "/compare/", "/attempts/2/jobs"):
            self.assertEqual(sum(immutable in path for path in paths), 2, immutable)  # two objects, once each
        self.assertEqual(sum(path == PULL for path in paths), 2)
        self.assertEqual(sum(path.endswith("/actions/runs/42") for path in paths), 2)
        self.assertEqual(sum(path.endswith("/actions/runs/43") for path in paths), 2)

    def test_malformed_or_mixed_pair_rejects_before_any_read(self):
        def swap_unit(world):
            world.seals["packaged"]["artifact"]["name"] = grammar.ci_artifact_name("tested", 43, 2, "build")

        def same_run(world):
            world.seals["build"] = copy.deepcopy(world.seals["packaged"])

        mutations = {"controller": {"controller_sha": False}, "merged": {"merged_sha": None},
                     "unit": swap_unit, "same seal": same_run,
                     "plan": lambda world: world.seals["packaged"].update(plan_sha256="f" * 64),
                     "same id": lambda world: world.seals["packaged"]["artifact"].update(id=200),
                     "swapped": lambda world: world.seals.update(build=world.seals["packaged"],
                                                                 packaged=world.seals["build"])}
        for name, mutation in mutations.items():
            world = merged_pair_world()
            changes = mutation if isinstance(mutation, dict) else (mutation(world) or {})
            with self.subTest(mutation=name), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "get_json") as reads, self.assertRaises(MbError):
                self.call(world, Path(directory), **changes)
            reads.assert_not_called()

    def test_individually_valid_packaged_gate_of_another_build_generation_is_rejected(self):
        world = merged_pair_world()
        # A second complete Build run of the same pull-request head, consumed by the packaged gate.
        other = copy.deepcopy(world.bundle)
        other["producer"]["run_id"] = 44
        other["artifact"].update(id=104, name=grammar.ci_artifact_name("build", 44, 2))
        world.api.add_run(ci_api_run(world.plan, "build", id=44))
        jobs = copy.deepcopy(world.jobs[42])
        for job in jobs:
            job.update(run_id=44, id=job["id"] + 500000)
        world.api.add_jobs(44, 2, jobs)
        world.api.add_artifact(ci_api_artifact(other), world.archives[100])
        world.documents["packaged"]["owning_build"] = other
        reseal(world, "packaged")
        with tempfile.TemporaryDirectory() as directory:
            alone = transport.download_merged_gate_receipt(
                world.api, descriptor=world.seals["packaged"], plan=world.plan, gate="packaged",
                controller_sha=world.controller, merged_sha=world.merged, temporary_root=Path(directory))
            self.assertEqual(alone["owning_build"]["producer"]["run_id"], 44)
            with self.assertRaisesRegex(MbError, "different complete Build bundle"):
                self.call(world, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_expiry_of_any_seal_or_source_after_it_was_read_refuses_the_pair(self):
        for artifact_id in (200, 201, 100, 101, 102):
            world = merged_pair_world()
            with self.subTest(artifact_id=artifact_id), tempfile.TemporaryDirectory() as directory, \
                    after_download(world.api, lambda: world.set_artifact(artifact_id, expired=True), count=2):
                with self.assertRaises(MbError):
                    self.call(world, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_historical_source_and_attempt_changes_between_the_two_seals_refuse_the_pair(self):
        def merged_at(world):
            world.pr["merged_at"] = "2026-10-08T10:01:00Z"
            world.api.add_response(PULL, world.pr)

        changes = {"merged_at": merged_at,
                   "controller": lambda world: world.api.set_branch("master", "f" * 40, "e" * 40),
                   "build attempt": lambda world: world.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "packaged attempt": lambda world: world.set_run(43, run_attempt=3)}
        for name, change in changes.items():
            for count in (1, 2):
                world = merged_pair_world()
                with self.subTest(change=name, after_seal=count), tempfile.TemporaryDirectory() as directory, \
                        after_download(world.api, lambda: change(world), count=count):
                    with self.assertRaises(MbError):
                        self.call(world, Path(directory))
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caller_documents_are_copied_on_entry(self):
        world = merged_pair_world()
        expected = copy.deepcopy((world.documents["build"], world.documents["packaged"]))
        plan, seals = world.plan, world.seals

        def substitute():
            plan["identity"]["kit"]["sha"] = "f" * 40
            seals["packaged"]["artifact"]["digest"] = "sha256:" + "f" * 64

        with tempfile.TemporaryDirectory() as directory, after_download(world.api, substitute):
            self.assertEqual(self.call(world, Path(directory)), expected)

    def test_api_failure_in_the_later_gate_never_returns_an_earlier_partial_success(self):
        world = merged_pair_world()
        failure = MbError("later gate unavailable")

        def fail():
            raise failure

        with tempfile.TemporaryDirectory() as directory, after_download(world.api, fail, count=2), \
                self.assertRaises(MbError) as caught:
            self.call(world, Path(directory))
        self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
