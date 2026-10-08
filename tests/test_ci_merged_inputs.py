"""Both original byte sets of a merged pull request are published together or not at all.

Real archives and the kit's own extraction, hashing and atomic copies in a temporary directory
(Linux); only the GitHub API is fake. The fixed private children are ``build`` and ``runtime``.
"""

import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import runtime_exports, transport
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.runtime_exports import verify_runtime_export
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from tests.test_ci_merged_build import (FINAL_READS, MOVED, MOVEMENTS, after_call, at_final_read,
                                        downloads_and_final_reads, recorded, replace_payload, stage_of, tamper)
from tests.test_ci_merged_gate_pair import merged_pair_world, pair_arguments
from tests.test_ci_merged_runtime import REPORT
from tests.test_ci_transport import after_download


def staged(directory):
    """The one unpublished combined stage below the caller's private parent, and its children."""

    (stage,) = Path(directory).iterdir()
    return stage, sorted(child.name for child in stage.iterdir())


class MergedInputsTest(unittest.TestCase):
    def call(self, world, output, **changes):
        return transport.download_merged_inputs(world.api, **pair_arguments(world, output=output, **changes))

    def test_both_original_complete_byte_sets_publish_under_fixed_children(self):
        world = merged_pair_world()
        before = copy.deepcopy((world.plan, world.seals))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            with recorded(world.api, output) as events:
                self.assertEqual(self.call(world, output), (world.envelope, world.runtime_envelope))
            self.assertEqual(sorted(child.name for child in output.iterdir()), ["build", "runtime"])
            for child, envelope in (("build", world.envelope), ("runtime", world.runtime_envelope)):
                for file in envelope["files"]:
                    self.assertEqual(hashlib.sha256((output / child / file["path"]).read_bytes()).hexdigest(),
                                     file["sha256"])
            self.assertEqual(verify_build_export(output / "build", plan=world.plan), world.envelope)
            self.assertEqual(verify_runtime_export(output / "runtime", plan=world.plan), world.runtime_envelope)
            self.assertEqual([child.name for child in Path(directory).iterdir()], ["output"])
        # Four archives, each fetched once; the pair is observed again, as metadata, after both
        # byte sets are staged, and nothing exists at the output before the last read.
        downloads, final = downloads_and_final_reads(events)
        self.assertEqual(downloads, [200, 201, 100, 102])
        self.assertEqual(final, FINAL_READS)
        self.assertFalse(any(published for *_, published in events))
        self.assertEqual(world.api.request_count, 32)  # the pair's 28 and two payload downloads (2 each)
        self.assertEqual((world.plan, world.seals), before)
        self.assertEqual(world.api.mutations, [])

    def test_bad_output_and_bindings_reject_before_api(self):
        world = merged_pair_world()
        with tempfile.TemporaryDirectory() as directory:
            cases = [{"output": False}, {"output": Path(directory)}, {"output": str(Path(directory) / "output")},
                     {"controller_sha": False}, {"merged_sha": None},
                     {"packaged_descriptor": {**world.seals["packaged"], "plan_sha256": "f" * 64}},
                     {"build_descriptor": world.seals["packaged"]}]
            for index, changes in enumerate(cases):
                arguments = {"output": Path(directory) / "output", **changes}
                with self.subTest(index=index), patch.object(world.api, "get_json") as reads, \
                        self.assertRaises(MbError):
                    self.call(world, **arguments)
                reads.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_runtime_failure_after_build_copy_publishes_neither_input(self):
        world = merged_pair_world()
        world.api.add_artifact(world.records[102], b"x" * len(world.archives[102]))
        observed = []
        with tempfile.TemporaryDirectory() as directory:
            def observe():
                stage, children = staged(directory)
                observed.append((children, verify_build_export(stage / "build", plan=world.plan)))

            with after_download(world.api, observe, count=4), \
                    self.assertRaisesRegex(MbError, "download length or SHA-256 differs"):
                self.call(world, Path(directory) / "output")
            self.assertEqual(observed, [(["build"], world.envelope)])  # the Build bytes were staged first
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_build_bytes_changed_during_runtime_copy_reject_at_combined_admission(self):
        world = merged_pair_world()
        name = world.envelope["files"][0]["path"]
        with tempfile.TemporaryDirectory() as directory, \
                after_call(runtime_exports, "copy_regular_data_files",
                           lambda root, stage_fd: tamper(stage_of(root, stage_fd).parent / "build" / name)) as copies:
            with self.assertRaisesRegex(MbError, "frozen export differs from its exact file inventory"):
                self.call(world, Path(directory) / "output")
            copies.assert_called_once()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_self_consistent_replacement_of_either_source_after_binding_publishes_neither_input(self):
        cases = {"Build": ("extract_build", "bind_build_envelope", grammar.CI_ENVELOPE_NAME, verify_build_export),
                 "runtime": ("extract_runtime", "bind_runtime_envelope", grammar.CI_RUNTIME_ENVELOPE_NAME,
                             verify_runtime_export)}
        for kind, (extract, bind, envelope_name, verify) in cases.items():
            world = merged_pair_world()
            envelope = world.envelope if kind == "Build" else world.runtime_envelope
            roots = []

            def replace(*_):
                document = replace_payload(roots[0], envelope, envelope_name, envelope["files"][0]["path"])
                self.assertEqual(verify(roots[0], plan=world.plan), document)  # it verifies, as another export
                self.assertNotEqual(document, envelope)

            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory, \
                    after_call(transport, extract, lambda archive, root: roots.append(root)), \
                    after_call(transport, bind, replace) as bound:
                with self.assertRaisesRegex(MbError, f"original {kind} bytes changed before publication"):
                    self.call(world, Path(directory) / "output")
                bound.assert_called_once()
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_source_or_attempt_movement_after_both_copies_publishes_neither_input(self):
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
                after_call(transport, "extract_build", substitute) as extractions:
            output = Path(directory) / "output"
            self.assertEqual(self.call(world, output), (world.envelope, world.runtime_envelope))
            extractions.assert_called_once()
            self.assertEqual(verify_build_export(output / "build", plan=admitted), world.envelope)
            self.assertEqual(verify_runtime_export(output / "runtime", plan=admitted), world.runtime_envelope)
        self.assertNotEqual(world.plan, admitted)

    def test_final_pair_read_cannot_hide_either_payload_or_extra_child_tampering(self):
        messages = {"build": "frozen export differs from its exact file inventory",
                    "runtime": "frozen runtime differs from its complete file inventory",
                    "extra": "combined inputs differ from fixed private children",
                    "expired": "artifact availability " + MOVED}
        for target, message in messages.items():
            world = merged_pair_world()
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                def change():
                    stage, children = staged(directory)
                    self.assertEqual(children, ["build", "runtime"])  # both inner copies are complete
                    if target == "extra":
                        (stage / "extra").write_bytes(b"")
                    elif target == "expired":
                        world.set_artifact(102, expired=True)
                    elif target == "build":
                        tamper(stage / "build" / world.envelope["files"][0]["path"])
                    else:
                        tamper(stage / "runtime" / REPORT)

                with at_final_read(world.api, change, downloads=4) as final:
                    with self.assertRaisesRegex(MbError, message):
                        self.call(world, Path(directory) / "output")
                self.assertTrue(final["done"])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_complete_entry_cap_precedes_final_payload_content_reads(self):
        world = merged_pair_world()
        with tempfile.TemporaryDirectory() as directory:
            self.call(world, Path(directory) / "output")
            entries = 1 + sum(1 for _ in (Path(directory) / "output").rglob("*"))  # the root counts
        self.assertLess(entries, limits.MAX_CI_ORIGINAL_INPUT_ENTRIES)
        for cap in (entries, entries - 1):
            world = merged_pair_world()
            with self.subTest(fits=cap == entries), tempfile.TemporaryDirectory() as directory, \
                    patch.object(limits, "MAX_CI_ORIGINAL_INPUT_ENTRIES", cap), \
                    patch.object(transport, "validate_tree_entries", wraps=transport.validate_tree_entries) as bound, \
                    patch.object(transport, "verify_build_export", wraps=transport.verify_build_export) as build, \
                    patch.object(transport, "verify_runtime_export", wraps=transport.verify_runtime_export) as runtime:
                if cap == entries:
                    self.call(world, Path(directory) / "output")
                else:
                    with self.assertRaisesRegex(MbError, "directory tree exceeds its entry bound"):
                        self.call(world, Path(directory) / "output")
                    self.assertEqual(list(Path(directory).iterdir()), [])
                bound.assert_called_once()
                self.assertEqual(bound.call_args.kwargs, {"max_entries": cap})
                # Each payload is read once when it is extracted and once more only after the cap held.
                self.assertEqual((build.call_count, runtime.call_count), (2, 2) if cap == entries else (1, 1))


if __name__ == "__main__":
    unittest.main()
