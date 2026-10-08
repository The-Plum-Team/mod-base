"""The original complete runtime results of a merged pull request are copied under coherent gate proof.

A real results archive (one report, one empty log and the canonical runtime envelope) and the
kit's own extraction, hashing and atomic copy in a temporary directory (Linux); only the GitHub
API is fake. This proves bytes and their binding to the original pair, never native validity.
"""

import copy
import hashlib
import io
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import runtime_exports, transport
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.errors import MbError
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from tests.test_ci_gate_transport import reseal
from tests.test_ci_merged_build import (FINAL_READS, MOVED, MOVEMENTS, after_call, at_final_read,
                                        downloads_and_final_reads, recorded, replace_payload, stage_of, tamper)
from tests.test_ci_merged_gate_pair import merged_pair_world, pair_arguments

REPORT = "lanes/lane-a/result.json"
LOG = "lanes/lane-a/runtime.log"
DIFFERS = "frozen runtime differs from its complete file inventory"


def republish_results(world, envelope):
    """Publish the results aggregate again with ``envelope`` in place of its own, as the artifact
    the packaged gate names: a self-consistent archive whose envelope claims something else."""

    stream = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(world.archives[102])) as original, \
            zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in original.namelist():
            archive.writestr(name, canonical_json(envelope) if name == grammar.CI_RUNTIME_ENVELOPE_NAME
                             else original.read(name))
    data = stream.getvalue()
    world.results["artifact"].update(size=len(data), digest="sha256:" + hashlib.sha256(data).hexdigest())
    world.archives[102] = data
    world.set_artifact(102, size_in_bytes=len(data), digest=world.results["artifact"]["digest"])
    world.documents["packaged"]["artifacts"][1] = copy.deepcopy(world.results)
    return reseal(world, "packaged")


class MergedRuntimeTest(unittest.TestCase):
    def call(self, world, output):
        return transport.download_merged_runtime(world.api, **pair_arguments(world, output=output))

    @contextmanager
    def tampering(self, world, when, name, data):
        """Another writer changing ``name``: in the extracted source, in the stage after the copy,
        or in the stage while the pair is observed for the last time."""

        if when == "final":
            stages = []
            with after_call(runtime_exports, "copy_regular_data_files",
                            lambda root, stage_fd: stages.append(stage_of(root, stage_fd))), \
                    at_final_read(world.api, lambda: tamper(stages[0] / name, data), downloads=3) as final:
                yield
            self.assertTrue(final["done"])
            return
        hook = (after_call(transport, "extract_runtime", lambda archive, root: tamper(root / name, data))
                if when == "extract" else
                after_call(runtime_exports, "copy_regular_data_files",
                           lambda root, stage_fd: tamper(stage_of(root, stage_fd) / name, data)))
        with hook as calls:
            yield
        calls.assert_called_once()

    def test_original_full_runtime_bytes_empty_log_and_pair_are_preserved(self):
        world = merged_pair_world()
        before = copy.deepcopy((world.plan, world.seals))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with recorded(world.api, output) as events:
                self.assertEqual(self.call(world, output), world.runtime_envelope)
            for file in world.runtime_envelope["files"]:
                self.assertEqual(hashlib.sha256((output / file["path"]).read_bytes()).hexdigest(), file["sha256"])
            self.assertEqual((output / LOG).stat().st_size, 0)
            self.assertEqual(verify_runtime_export(output, plan=world.plan), world.runtime_envelope)
            self.assertEqual([child.name for child in Path(directory).iterdir()], ["output"])
        self.assertEqual(world.runtime_envelope["owning_build"], world.documents["build"]["artifacts"][0])
        # Each seal is fetched once and then the aggregate; the pair is observed again, as metadata,
        # after the aggregate arrived, and nothing exists at the output before the last read.
        downloads, final = downloads_and_final_reads(events)
        self.assertEqual(downloads, [200, 201, 102])
        self.assertEqual(final, FINAL_READS)
        self.assertFalse(any(published for *_, published in events))
        self.assertEqual(world.api.request_count, 30)  # the pair's 28 and the aggregate download (2)
        self.assertEqual((world.plan, world.seals), before)
        self.assertEqual(world.api.mutations, [])

    def test_output_preflight_rejects_before_api(self):
        world = merged_pair_world()
        with tempfile.TemporaryDirectory() as directory:
            for output in (False, Path(directory), str(Path(directory) / "output")):
                with self.subTest(output=output), patch.object(world.api, "get_json") as reads, \
                        self.assertRaises(MbError):
                    self.call(world, output)
                reads.assert_not_called()

    def test_zip_digest_rejects_before_parser(self):
        for size in (5, None):
            world = merged_pair_world()
            world.api.add_artifact(world.records[102], b"x" * (size or len(world.archives[102])))
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory, \
                    patch.object(transport, "extract_runtime", wraps=transport.extract_runtime) as parser:
                with self.assertRaisesRegex(MbError, "download length or SHA-256 differs"):
                    self.call(world, Path(directory) / "output")
                parser.assert_not_called()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_payload_source_stage_and_final_api_read_tampering_reject(self):
        for when in ("extract", "copy", "final"):
            for name, data in ((LOG, b"tampered"), (REPORT, None)):  # a grown log, a same-size report
                world = merged_pair_world()
                with self.subTest(when=when, name=name), tempfile.TemporaryDirectory() as directory, \
                        self.tampering(world, when, name, data):
                    with self.assertRaisesRegex(MbError, DIFFERS):
                        self.call(world, Path(directory) / "output")
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_valid_but_different_owner_producer_or_scope_rejects_original_selection(self):
        for target in ("owner", "producer", "scope"):
            world = merged_pair_world()
            document = copy.deepcopy(world.runtime_envelope)
            if target == "owner":
                document["owning_build"]["artifact"]["digest"] = "sha256:" + "f" * 64
            elif target == "producer":
                document["producer"]["graph_sha256"] = "f" * 64
            else:
                document.update(scope="lane", lane_id="lane-a")
            validate_runtime_envelope(document, plan=world.plan)  # the shape alone is valid
            republish_results(world, document)
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory, \
                    patch.object(world.api, "download", wraps=world.api.download) as downloads:
                with self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                # The pair itself is coherent: the aggregate was fetched and refused on what it claims.
                self.assertEqual([call.args[0].split("/")[-2] for call in downloads.call_args_list],
                                 ["200", "201", "102"])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_self_consistent_source_replacement_after_binding_cannot_publish(self):
        world = merged_pair_world()
        roots = []

        def replace(*_):
            document = replace_payload(roots[0], world.runtime_envelope, grammar.CI_RUNTIME_ENVELOPE_NAME, LOG)
            self.assertEqual(verify_runtime_export(roots[0], plan=world.plan), document)
            self.assertNotEqual(document, world.runtime_envelope)

        with tempfile.TemporaryDirectory() as directory, \
                after_call(transport, "extract_runtime", lambda archive, root: roots.append(root)), \
                after_call(transport, "bind_runtime_envelope", replace) as bound:
            with self.assertRaisesRegex(MbError, "original runtime bytes changed before publication"):
                self.call(world, Path(directory) / "output")
            bound.assert_called_once()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_original_seal_runtime_or_lane_expiry_inside_copy_forbids_output(self):
        for artifact_id in (201, 200, 102, 101, 100):
            world = merged_pair_world()
            with self.subTest(artifact_id=artifact_id), tempfile.TemporaryDirectory() as directory, \
                    after_call(runtime_exports, "copy_regular_data_files",
                               lambda *_: world.set_artifact(artifact_id, expired=True)) as copies:
                with self.assertRaisesRegex(MbError, "artifact availability " + MOVED):
                    self.call(world, Path(directory) / "output")
                copies.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_or_attempt_movement_inside_copy_forbids_output(self):
        for name, change in MOVEMENTS.items():
            world = merged_pair_world()
            with self.subTest(change=name), tempfile.TemporaryDirectory() as directory, \
                    after_call(runtime_exports, "copy_regular_data_files", lambda *_: change(world)) as copies:
                with self.assertRaises(MbError):
                    self.call(world, Path(directory) / "output")
                copies.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_caller_documents_are_copied_on_entry(self):
        world = merged_pair_world()
        admitted = copy.deepcopy(world.plan)

        def substitute(*_):
            world.plan["identity"]["kit"]["sha"] = "f" * 40
            for seal in world.seals.values():
                seal["artifact"]["digest"] = "sha256:" + "f" * 64

        with tempfile.TemporaryDirectory() as directory, \
                after_call(runtime_exports, "copy_regular_data_files", substitute) as copies:
            output = Path(directory) / "output"
            self.assertEqual(self.call(world, output), world.runtime_envelope)
            copies.assert_called_once()
            self.assertEqual(verify_runtime_export(output, plan=admitted), world.runtime_envelope)
        self.assertNotEqual(world.plan, admitted)


if __name__ == "__main__":
    unittest.main()
