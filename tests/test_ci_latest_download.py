"""The newest selection stays binding until the private copy is published; real files, fake API."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.selection import download_latest_pr_build
from mod_base.errors import MbError
from tests.helpers import ci_api_run
from tests.test_ci_transport import after_download, build_world


def supersede(world):
    world.api.add_run(ci_api_run(world.plan, "build", id=44, created_at="2026-10-07T11:00:00Z",
                                 status="queued", conclusion=None))


class LatestDownloadTests(unittest.TestCase):
    def call(self, world, output):
        return download_latest_pr_build(world.api, plan=world.plan, output=output, monotonic=lambda: 0,
                                        sleep=lambda _: self.fail("successful producer must not sleep"))

    def test_numeric_download_publishes_the_bound_bundle_within_the_request_budget(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "download", wraps=world.api.download) as download:
            output = Path(directory) / "output"
            observed = self.call(world, output)
            self.assertEqual(observed, {"descriptor": world.bundle, "envelope": world.envelope})
            self.assertEqual(verify_build_export(output, plan=world.plan), world.envelope)
            self.assertEqual(list(Path(directory).iterdir()), [output])
        download.assert_called_once_with("/repos/example/mod/actions/artifacts/100/zip",
                                         max_bytes=world.bundle["artifact"]["size"])
        # Select 8 (source 4, listing, run, jobs, the bundle's row in its listing), download 2, and
        # before publication the source 3, the listing, the run and the bundle record by id.
        self.assertEqual(world.api.request_count, 16)
        self.assertEqual(world.api.mutations, [])

    def test_newer_run_attempt_or_expiry_during_download_leaves_no_published_output(self):
        changes = {"run": supersede,
                   "attempt": lambda w: w.set_run(42, run_attempt=3, status="queued", conclusion=None),
                   "expired": lambda w: w.set_artifact(100, expired=True),
                   "source": lambda w: w.api.set_branch("master", "f" * 40, "e" * 40),
                   "draft": lambda w: w.api.add_response("/repos/example/mod/pulls/7", {**w.pr, "draft": True})}
        for name, change in changes.items():
            world = build_world()
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                with after_download(world.api, lambda: change(world)), self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_digest_mismatch_cannot_publish(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(world.api, "download", return_value=world.archives[100] + b"corrupt"), \
                self.assertRaisesRegex(MbError, "SHA-256"):
            self.call(world, Path(directory) / "output")

    def test_existing_output_rejects_before_any_read(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, patch.object(world.api, "get_json") as reads, \
                self.assertRaises(MbError):
            self.call(world, Path(directory))
        reads.assert_not_called()

    def test_caller_plan_mutation_during_download_cannot_replace_retained_identity(self):
        world = build_world()
        with tempfile.TemporaryDirectory() as directory, after_download(world.api, world.plan.clear):
            observed = self.call(world, Path(directory) / "output")
        self.assertEqual(observed["envelope"], world.envelope)
        self.assertEqual(observed["descriptor"]["identity"], world.envelope["identity"])


if __name__ == "__main__":
    unittest.main()
