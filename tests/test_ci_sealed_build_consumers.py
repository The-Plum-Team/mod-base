"""The complete Build as its assembling job uploads it, read by the jobs that consume it.

One writer makes every sealed artifact: ``ci worker-validate`` writes the upload directory with the
frozen export, its envelope and, beside the envelope, the validation record of that export with its
reports (``validation.materialize_validated_export``). ``tests/test_ci_gate.py`` reads such a
Build back at the Build gate. The other readers of the same artifact are the packaged jobs: the
``input`` job selects it (``selection.select_build``) and every lane fetches it
(``selection.fetch_build``). Every complete Build reader requires those sealed files; a bare export is rejected.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base.build_ci import selection, transport
from mod_base.build_ci.exports import verify_build_export
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.workflow import CI_CALLER_WORKFLOWS
from tests.test_ci_gate import GateCase
from tests.test_ci_transport import World, build_archive, build_world
from tests.test_ci_validated_export import UploadCase, tree, upload_archive

PACKAGED = CI_CALLER_WORKFLOWS["packaged"]
REPORT = "target-a.json"


class SealedBuildConsumerTests(GateCase):
    def finished_build(self, **options):
        """The finished full Build run of pull request 7 with the artifacts its jobs uploaded."""

        attempt = self.build_world(**options)
        attempt.complete("build-full")
        return attempt

    def select(self, attempt, directory: Path, **arguments):
        """The selection of run 43, attempt 2 of the packaged caller, which never has to wait."""

        return selection.select_build(
            attempt.api, plan=attempt.plan, run_id=43, run_attempt=2, workflow_path=PACKAGED,
            temporary_root=directory, monotonic=lambda: 0,
            sleep=lambda _: self.fail("a finished Build must not sleep"),
            **{"event": "pull_request_target", **arguments})

    def test_the_input_job_selects_the_build_its_assembling_job_uploaded(self) -> None:
        attempt = self.finished_build()
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            record = self.select(attempt, Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(record["build"], attempt.descriptor("full", 100))

    def test_a_lane_job_fetches_exactly_the_export_the_build_was_sealed_with(self) -> None:
        attempt = self.finished_build()
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            record = self.select(attempt, Path(directory))
            output = Path(directory) / "sealed-build"
            envelope = selection.fetch_build(attempt.api, record=record, plan=attempt.plan, run_id=43,
                                             run_attempt=2, workflow_path=PACKAGED, event="pull_request_target",
                                             output=output)
            # What a lane stages and its validator reads is the export alone: the inventory of its
            # envelope, without the record and the reports of the Build's own verification.
            self.assertEqual(verify_build_export(output, plan=attempt.plan), envelope)
            self.assertEqual(sorted(path.name for path in Path(directory).iterdir()), ["sealed-build"])
        self.assertEqual(canonical_sha256(envelope), record["envelope_sha256"])

    def test_a_job_of_a_rebuilding_run_selects_the_build_that_run_uploaded(self) -> None:
        # The packaged run built for itself; its Build jobs have finished and its own gate is running.
        attempt = self.packaged_world(mode="rebuilt")
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            record = self.select(attempt, Path(directory), event="push", build_run_id=selection.SAME_RUN)
        self.assertEqual(record["build"], attempt.descriptor("rebuilt", 100))

    def test_a_record_that_was_not_frozen_for_these_bytes_in_this_attempt_publishes_nothing(self) -> None:
        def other_report(document, files, data):
            files[REPORT] = canonical_json({"unit": "target-a", "verified": False})

        def no_report(document, files, data):
            del files[REPORT]

        def stray_file(document, files, data):
            files["stray.json"] = b"{}\n"

        changes = {"another input": lambda document, files, data: document.update(input_sha256="f" * 64),
                   "another attempt": lambda document, files, data: document.update(run_attempt=1),
                   "another run": lambda document, files, data: document.update(run_id=41),
                   "another hook": lambda document, files, data: document.update(hook="verify_target",
                                                                                unit_id="target-a"),
                   "another plan": lambda document, files, data: document.update(plan_sha256="f" * 64),
                   "no validation": lambda document, files, data: document.clear(),
                   "another report": other_report, "a missing report": no_report, "a stray file": stray_file}
        for name, change in changes.items():
            attempt = self.finished_build(change=change)
            with self.subTest(change=name), tempfile.TemporaryDirectory(dir=self.temporary) as directory:
                with self.assertRaises(MbError):
                    self.select(attempt, Path(directory))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_fetch_rejects_a_bare_complete_export(self) -> None:
        world = build_world()
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            root = Path(directory)
            record = selection.select_build(world.api, plan=world.plan, run_id=43, run_attempt=2,
                                             workflow_path=PACKAGED, event="pull_request_target", temporary_root=root)
            data, _ = build_archive(world.plan, world.bundle["producer"])
            record["build"] = world.publish(world.describe("build", "full", "build", data), data)
            with self.assertRaises(MbError):
                selection.fetch_build(world.api, record=record, plan=world.plan, run_id=43,
                                      run_attempt=2, workflow_path=PACKAGED, event="pull_request_target",
                                      output=root / "build")
            self.assertEqual(list(root.iterdir()), [])

    def test_a_loaded_config_digest_is_bound_by_selection_and_fetch(self) -> None:
        attempt = self.finished_build()
        digest = attempt.validation["source_config_sha256"]
        with tempfile.TemporaryDirectory(dir=self.temporary) as directory:
            root = Path(directory)
            record = self.select(attempt, root, source_config_sha256=digest)
            with self.assertRaisesRegex(MbError, "protected execution/input context"):
                self.select(attempt, root, source_config_sha256="f" * 64)
            output = root / "build"
            with self.assertRaisesRegex(MbError, "protected execution/input context"):
                selection.fetch_build(attempt.api, record=record, plan=attempt.plan, run_id=43,
                                      run_attempt=2, workflow_path=PACKAGED, event="pull_request_target",
                                      output=output, source_config_sha256="f" * 64)
            self.assertFalse(output.exists())
            self.assertEqual(list(root.iterdir()), [])



class WriterRoundTripTests(UploadCase):
    """The upload directory the real writer makes (``validation.materialize_validated_export``),
    archived the way ``actions/upload-artifact`` does and read back with no step in between."""

    def test_the_upload_of_the_assembling_job_reaches_the_reader_of_a_completed_build(self) -> None:
        world = World()
        world.add_run("build", "build-full")
        self.within("complete")
        arguments = self.verified("verify_build", None, plan=world.plan)
        self.write_upload(arguments)
        archive = upload_archive(self.upload)
        descriptor = world.publish(world.describe("build", "full", "build", archive), archive)
        output = self.temporary / "downloaded"
        envelope = transport.download_completed_build(world.api, descriptor=descriptor, plan=world.plan, output=output)
        self.assertEqual(envelope, arguments["envelope"])
        # The published copy is the sealed export itself: the record and its reports stay behind.
        self.assertEqual(tree(output), tree(self.export))
        self.assertLess(set(tree(self.export)), set(tree(self.upload)))


if __name__ == "__main__":
    unittest.main()
