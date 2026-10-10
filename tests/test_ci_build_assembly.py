"""Complete target byte assembly; real POSIX copying is covered by required Linux fixtures."""

import copy
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import exports
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.test_ci_exports import partitions_fixture


def assembly_fixture(parent):
    plan, partitions = partitions_fixture()
    inputs = parent / "inputs"
    inputs.mkdir()
    for index in range(len(partitions)):
        (inputs / f"target-{index}").mkdir()
    return plan, partitions, inputs, parent / "output"


class BuildAssemblyTests(unittest.TestCase):
    def call(self, fixture, *, run_id=42, run_attempt=2):
        plan, partitions, inputs, output = fixture
        return exports.assemble_build_export(inputs, partitions=partitions, plan=plan,
                                               run_id=run_id, run_attempt=run_attempt, output=output)

    def seams(self, fixture, *, changed_input=False, bad_copy=False, late_copy_error=False, bad_output=False):
        plan, partitions, inputs, output = fixture
        visits = {}
        def verify(root, **kwargs):
            if root.name.startswith("target-"):
                index = int(root.name.split("-")[1])
                visits[index] = visits.get(index, 0) + 1
                value = copy.deepcopy(partitions[index]["envelope"])
                if changed_input and visits[index] > 1:
                    value["producer"]["graph_sha256"] = "f" * 64
                return value
            raw = canonical_json(writes[0][2]) if writes else None
            from mod_base.io.secure_json import loads
            value = loads(raw, label="fixture", max_bytes=limits.MAX_CI_ENVELOPE_BYTES)
            if bad_output:
                value["producer"]["graph_sha256"] = "f" * 64
            return value
        copies, writes = [], []
        def copying(root, fd, **kwargs):
            index = int(root.name.split("-")[1])
            copies.append((root, fd, kwargs))
            if late_copy_error and index:
                raise MbError("second target rejected")
            records = [{key: file[key] for key in ("path", "size", "sha256")}
                       for file in partitions[index]["envelope"]["files"]]
            return records[:-1] if bad_copy else records
        def writing(fd, name, raw):
            from mod_base.io.secure_json import loads
            writes.append((fd, name, loads(raw, label="fixture", max_bytes=limits.MAX_CI_ENVELOPE_BYTES)))
        stack = ExitStack()
        stack.enter_context(patch.object(exports, "validate_tree_entries"))
        stack.enter_context(patch.object(exports, "verify_build_export", side_effect=verify))
        stack.enter_context(patch.object(exports, "copy_selected_regular_files", side_effect=copying))
        stack.enter_context(patch.object(exports, "write_new", side_effect=writing))
        publication = stack.enter_context(patch.object(exports, "atomic_directory",
            side_effect=lambda p, writer: writer(output, 99)))
        return stack, copies, writes, publication

    def test_complete_union_has_new_canonical_envelope_and_copies_only_declared_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = assembly_fixture(Path(directory))
            before = copy.deepcopy(fixture[1])
            stack, copies, writes, publication = self.seams(fixture)
            with stack:
                observed = self.call(fixture)
            self.assertEqual((observed["scope"], observed["target_id"]), ("complete", None))
            self.assertEqual(observed["files"], sorted([file for p in fixture[1] for file in p["envelope"]["files"]],
                                                       key=lambda file: file["path"]))
            self.assertNotIn("upload_window", observed["producer"])
            self.assertEqual(fixture[1], before)
            publication.assert_called_once()
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0][1], grammar.CI_ENVELOPE_NAME)
            for index, (_, fd, kwargs) in enumerate(copies):
                self.assertEqual(fd, 99)
                self.assertEqual(kwargs["paths"], tuple(file["path"] for file in fixture[1][index]["envelope"]["files"]))
                self.assertNotIn(grammar.CI_ENVELOPE_NAME, kwargs["paths"])
            fixture[1][0]["envelope"]["producer"]["run_id"] = 99
            self.assertEqual(observed["producer"]["run_id"], 42)

    def test_incomplete_reordered_mixed_and_wrong_run_inputs_reject_before_publication(self):
        for mutation in ("missing", "reordered", "mixed", "run", "attempt"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = assembly_fixture(Path(directory))
                if mutation == "missing":
                    fixture[1].pop()
                elif mutation == "reordered":
                    fixture[1].reverse()
                elif mutation == "mixed":
                    fixture[1][1]["envelope"]["producer"]["api_head_sha"] = "f" * 40
                stack, _, _, publication = self.seams(fixture)
                with stack, self.assertRaises(MbError):
                    self.call(fixture, run_id=43 if mutation == "run" else 42,
                              run_attempt=3 if mutation == "attempt" else 2)
                publication.assert_not_called()

    def test_extra_missing_and_renamed_physical_target_children_reject(self):
        for mutation in ("extra", "missing", "renamed"):
            with tempfile.TemporaryDirectory() as directory:
                fixture = assembly_fixture(Path(directory))
                if mutation == "extra":
                    (fixture[2] / "arbitrary").mkdir()
                elif mutation == "missing":
                    (fixture[2] / "target-1").rmdir()
                else:
                    (fixture[2] / "target-1").rename(fixture[2] / "target-x")
                stack, _, _, publication = self.seams(fixture)
                with stack, self.assertRaisesRegex(MbError, "fixed ordinals"):
                    self.call(fixture)
                publication.assert_not_called()

    def test_source_envelope_drift_before_and_after_copy_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = assembly_fixture(Path(directory))
            stack, _, _, _ = self.seams(fixture, changed_input=True)
            with stack, self.assertRaisesRegex(MbError, "target bytes"):
                self.call(fixture)

    def test_copy_loss_late_target_failure_and_wrong_assembled_bytes_reject(self):
        for kwargs in ({"bad_copy": True}, {"late_copy_error": True}, {"bad_output": True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                fixture = assembly_fixture(Path(directory))
                stack, _, _, _ = self.seams(fixture, **kwargs)
                with stack, self.assertRaises(MbError):
                    self.call(fixture)

    def test_complete_envelope_and_logical_caps_apply_before_publication(self):
        for name, value in (("MAX_CI_ENVELOPE_BYTES", 1), ("MAX_CI_EXPORT_FILES", 7),
                            ("MAX_CI_EXPORT_TREE_BYTES", 900), ("MAX_CI_EXPORT_ENTRIES", 1)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture = assembly_fixture(Path(directory))
                stack, _, _, publication = self.seams(fixture)
                with stack, patch.object(limits, name, value), self.assertRaises(MbError):
                    self.call(fixture)
                publication.assert_not_called()

    def test_overlapping_input_and_output_roots_reject(self):
        for descendant in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                fixture = list(assembly_fixture(Path(directory)))
                fixture[3] = fixture[2] / "output" if descendant else fixture[2].parent
                stack, _, _, publication = self.seams(fixture)
                with stack, self.assertRaisesRegex(MbError, "overlap"):
                    self.call(fixture)
                publication.assert_not_called()

    def test_filesystem_errors_surface_as_bounded_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = assembly_fixture(Path(directory))
            stack, _, _, _ = self.seams(fixture)
            with stack, patch.object(exports.os, "scandir", side_effect=OSError("fixture")), self.assertRaises(MbError):
                self.call(fixture)
        with tempfile.TemporaryDirectory() as directory:
            fixture = assembly_fixture(Path(directory))
            with patch.object(Path, "resolve", side_effect=RuntimeError("symlink loop")), self.assertRaises(MbError):
                self.call(fixture)
