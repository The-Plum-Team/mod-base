"""Original evidence that is gone is told apart from original evidence that is corrupt.

The readers of a merged pull request's gates report an artifact that has expired, that its run no
longer lists or whose numeric id GitHub no longer knows as ``transport.OriginalUnavailable``
(exit 3): nothing is wrong, the evidence is gone, and whoever meant to reuse it tests again. What
is still there and differs stays a rejection, an API failure stays an API failure, and the live
routes keep rejecting a missing artifact exactly as before.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import transport
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError, ApiNotFound
from mod_base.model.validators import DocumentError
from tests.test_ci_gate_timeline import gate_world
from tests.test_ci_merged_gate_pair import merged_pair_world, pair_arguments
from tests.test_ci_merged_gate_transport import merged_gate_world
from tests.test_ci_transport import after_download

ARTIFACT = "/repos/example/mod/actions/artifacts/"
#: The two tested records, the Build bundle, the lane's results and the results index of the pair.
SEALS, SOURCES = (200, 201), (100, 101, 102)


class OriginalUnavailableTests(unittest.TestCase):
    def pair(self, world):
        with tempfile.TemporaryDirectory() as directory:
            try:
                return transport.download_merged_gate_pair(
                    world.api, **pair_arguments(world, temporary_root=Path(directory)))
            finally:
                self.assertEqual(list(Path(directory).iterdir()), [])

    def gone(self, world, message: str) -> None:
        with self.assertRaisesRegex(transport.OriginalUnavailable, message) as caught:
            self.pair(world)
        self.assertIsInstance(caught.exception, Unavailable)
        self.assertEqual((caught.exception.reason, caught.exception.exit_code), ("ci-original-unavailable", 3))
        self.assertEqual(world.api.mutations, [])

    def corrupt(self, world, message: str = "") -> MbError:
        with self.assertRaisesRegex(MbError, message) as caught:
            self.pair(world)
        self.assertNotIsInstance(caught.exception, Unavailable)
        self.assertEqual(caught.exception.exit_code, 2)
        return caught.exception

    def test_an_expired_record_or_source_is_gone(self) -> None:
        for artifact_id in (*SEALS, *SOURCES):
            world = merged_pair_world()
            world.set_artifact(artifact_id, expired=True)
            with self.subTest(artifact_id=artifact_id):
                self.gone(world, "original evidence is gone: .*expired artifact")

    def test_an_artifact_its_run_no_longer_lists_is_gone(self) -> None:
        # The lane and the index are read from the run's listing: one that moved away is not in it.
        for artifact_id in (101, 102):
            world = merged_pair_world()
            world.set_artifact(artifact_id, workflow_run={"id": 999})
            with self.subTest(artifact_id=artifact_id):
                self.gone(world, "no longer listed for its producer run")

    def test_an_id_github_no_longer_knows_is_gone_and_an_api_failure_is_not(self) -> None:
        def failing(world, target, failure, method="get_json"):
            original = getattr(world.api, method)

            def call(path, **arguments):
                if path == target:
                    raise failure
                return original(path, **arguments)

            return patch.object(world.api, method, side_effect=call)

        for path, method in ((f"{ARTIFACT}200", "get_json"), (f"{ARTIFACT}100", "get_json"),
                             (f"{ARTIFACT}200/zip", "download"), (f"{ARTIFACT}201/zip", "download")):
            for status in (404, 410):
                world = merged_pair_world()
                kind = ApiNotFound if status == 404 else ApiError
                failure = kind(f"GitHub API GET {path} failed with HTTP {status}", status=status, method="GET",
                               path=path)
                with self.subTest(path=path, status=status), failing(world, path, failure, method):
                    self.gone(world, f"GitHub answers {status} for {path}")
            for status in (0, 403, 500, 502):
                world = merged_pair_world()
                failure = ApiError("GitHub is unavailable", status=status, method="GET", path=path)
                with self.subTest(path=path, status=status), failing(world, path, failure, method), \
                        self.assertRaises(MbError) as caught:
                    self.pair(world)
                self.assertIs(caught.exception, failure)
        # A 404 for anything but an artifact is no statement about evidence.
        for path in ("/repos/example/mod/actions/runs/42", "/repos/example/mod/pulls/7"):
            world = merged_pair_world()
            failure = ApiNotFound("GitHub API GET failed with HTTP 404", status=404, method="GET", path=path)
            with self.subTest(path=path), failing(world, path, failure), self.assertRaises(MbError) as caught:
                self.pair(world)
            self.assertIs(caught.exception, failure)

    def test_what_is_still_there_and_differs_is_corrupt(self) -> None:
        changes = ({"digest": "sha256:" + "f" * 64}, {"workflow_run": {"head_sha": "b" * 40}},
                   {"workflow_run": {"head_branch": "master"}}, {"size_in_bytes": 7},
                   # Corruption is compared first: an artifact that is foreign and expired is foreign.
                   {"expired": True, "workflow_run": {"head_sha": "b" * 40}},
                   {"expired": True, "digest": "sha256:" + "f" * 64})
        for artifact_id in (200, 100, 101):
            for change in changes:
                world = merged_pair_world()
                world.set_artifact(artifact_id, **{key: dict(value) if isinstance(value, dict) else value
                                                   for key, value in change.items()})
                with self.subTest(artifact_id=artifact_id, change=change):
                    self.assertIsInstance(self.corrupt(world), DocumentError)

    def test_evidence_that_goes_after_it_was_read_is_a_change_not_an_absence(self) -> None:
        for artifact_id in (*SEALS, 100):
            world = merged_pair_world()
            with self.subTest(artifact_id=artifact_id), \
                    after_download(world.api, lambda: world.set_artifact(artifact_id, expired=True), count=2):
                self.corrupt(world, "artifact availability changed between the start of the command and its effect")

    def test_one_original_receipt_read_alone_says_the_same(self) -> None:
        for gate, artifact_ids in (("build", (200, 100)), ("packaged", (201, 100, 101, 102))):
            for artifact_id in artifact_ids:
                world = merged_gate_world(gate)
                world.set_artifact(artifact_id, expired=True)
                with self.subTest(gate=gate, artifact_id=artifact_id), tempfile.TemporaryDirectory() as directory, \
                        self.assertRaises(transport.OriginalUnavailable):
                    transport.download_merged_gate_receipt(
                        world.api, descriptor=world.seals[gate], plan=world.plan, gate=gate,
                        controller_sha=world.controller, merged_sha=world.merged, temporary_root=Path(directory))

    def test_a_live_route_rejects_a_missing_artifact_as_it_always_did(self) -> None:
        for change, message in (({"expired": True}, "expired artifact or wrong protected producer"),
                                ({"workflow_run": {"id": 999}},
                                 "selected artifact is no longer listed for its producer run")):
            world = gate_world("packaged")
            world.set_artifact(101, **change)
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, \
                    self.assertRaises(transport.ArtifactUnavailable) as caught:
                transport.download_gate_receipt(world.api, descriptor=world.seals["packaged"], plan=world.plan,
                                                gate="packaged", temporary_root=Path(directory))
            error = caught.exception
            self.assertIsInstance(error, DocumentError)
            self.assertNotIsInstance(error, Unavailable)
            self.assertEqual((error.reason, error.exit_code, str(error)),
                             ("invalid-document", 2, f"$.artifact: {message}"))


if __name__ == "__main__":
    unittest.main()
