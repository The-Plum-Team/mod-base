"""Real ZIP encoding with explicit Windows filesystem-admission seams."""

import copy
import hashlib
import io
import shutil
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import archive
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests.test_ci_transport import transport_fixture


def archive_fixture(root):
    plan, _, _, envelope, *_ = transport_fixture()
    root.mkdir()
    for file in envelope["files"]:
        path = root / file["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((file["path"] + "\n").encode())
    (root / grammar.CI_ENVELOPE_NAME).write_bytes(canonical_json(envelope))
    return plan, envelope


class ArchiveEncodingTests(unittest.TestCase):
    def seams(self, stack, root, envelope, *, stream=None, verify=None):
        stage_holder = []
        def atomic(output, writer):
            stage = output.parent / "private-stage"
            stage.mkdir()
            stage_holder[:] = [stage]
            try:
                result = writer(stage, 0)
                stage.rename(output)
                return result
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        def stream_file(source, relative, *, max_bytes, consume):
            data = (source / relative).read_bytes()
            consume(data)
            return len(data)
        def extract(source, output):
            output.mkdir()
            with zipfile.ZipFile(source) as package:
                package.extractall(output)  # Test-only, encoder owns this archive; production uses strict extractor.
        stack.enter_context(patch.object(archive, "atomic_directory", side_effect=atomic))
        stack.enter_context(patch.object(archive, "_archive_stream", side_effect=lambda fd:
            (stage_holder[0] / grammar.CI_ARCHIVE_NAME).open("w+b")))
        stack.enter_context(patch.object(archive, "stream_child_file", side_effect=stream or stream_file))
        stack.enter_context(patch.object(archive, "extract_build", side_effect=extract))
        def hashing(path, *, max_bytes):
            data = path.read_bytes()
            if len(data) > max_bytes:
                raise MbError("test seam archive exceeds hash bound")
            return hashlib.sha256(data).hexdigest()
        stack.enter_context(patch.object(archive, "sha256_file", side_effect=hashing))
        def verifying(source, **kwargs):
            for file in envelope["files"]:
                data = (source / file["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != file["sha256"]:
                    raise MbError("test seam detected changed payload")
            if (source / grammar.CI_ENVELOPE_NAME).read_bytes() != canonical_json(envelope):
                raise MbError("test seam detected changed envelope")
            return copy.deepcopy(envelope)
        stack.enter_context(patch.object(archive, "verify_build_export", side_effect=verify or verifying))

    def test_deterministic_stored_archive_has_exact_files_bytes_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            plan, envelope = archive_fixture(base / "source")
            self.seams(stack, base / "source", envelope)
            first = archive.encode_build_export(base / "source", base / "one", plan=plan)
            second = archive.encode_build_export(base / "source", base / "two", plan=plan)
            data = (base / "one" / grammar.CI_ARCHIVE_NAME).read_bytes()
            self.assertEqual(data, (base / "two" / grammar.CI_ARCHIVE_NAME).read_bytes())
            self.assertEqual(first, second)
            self.assertEqual(first, {"path": grammar.CI_ARCHIVE_NAME, "size": len(data),
                                      "sha256": hashlib.sha256(data).hexdigest()})
            with zipfile.ZipFile(io.BytesIO(data)) as package:
                self.assertEqual(package.namelist(), sorted(
                    [f["path"] for f in envelope["files"]] + [grammar.CI_ENVELOPE_NAME]))
                for info in package.infolist():
                    self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                    self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                    self.assertEqual(package.read(info), (base / "source" / info.filename).read_bytes())
            self.assertEqual(list((base / "one").iterdir()), [base / "one" / grammar.CI_ARCHIVE_NAME])

    def test_compressed_cap_includes_headers_and_central_directory(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            plan, envelope = archive_fixture(base / "source")
            self.seams(stack, base / "source", envelope)
            result = archive.encode_build_export(base / "source", base / "reference", plan=plan)
            with patch.object(limits, "MAX_CI_BUNDLE_COMPRESSED_BYTES", result["size"] - 1):
                with self.assertRaises(MbError):
                    archive.encode_build_export(base / "source", base / "output", plan=plan)
            self.assertFalse((base / "output").exists())
            self.assertFalse((base / "private-stage").exists())

    def test_streamed_hash_drift_and_late_source_drift_forbid_publication(self):
        for late in (False, True):
            with self.subTest(late=late), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                base = Path(directory)
                plan, envelope = archive_fixture(base / "source")
                calls = [0]
                def stream(source, relative, *, max_bytes, consume):
                    data = (source / relative).read_bytes()
                    consume(data if late else b"x" * len(data))
                    calls[0] += 1
                    if late and calls[0] == len(envelope["files"]) + 1:
                        (source / envelope["files"][0]["path"]).write_bytes(b"changed")
                    return len(data)
                self.seams(stack, base / "source", envelope, stream=stream)
                with self.assertRaises(MbError):
                    archive.encode_build_export(base / "source", base / "output", plan=plan)
                self.assertFalse((base / "output").exists())
                self.assertFalse((base / "private-stage").exists())

    def test_overlap_is_rejected_before_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for output in (root, root / "nested", root.parent):
                with self.subTest(output=output), patch.object(archive, "verify_build_export") as verify:
                    with self.assertRaisesRegex(MbError, "overlap"):
                        archive.encode_build_export(root, output, plan={})
                verify.assert_not_called()

    def test_sink_handles_short_writes_and_rejects_no_progress_before_exceeding_cap(self):
        class Short(io.BytesIO):
            def write(self, data):
                return super().write(data[:2])
        raw = Short()
        sink = archive._CappedStream(raw)
        self.assertEqual(sink.write(b"abcdef"), 6)
        self.assertEqual(raw.getvalue(), b"abcdef")
        with patch.object(raw, "write", return_value=0), self.assertRaises(MbError):
            sink.write(b"x")
        with patch.object(limits, "MAX_CI_BUNDLE_COMPRESSED_BYTES", 6), self.assertRaises(MbError):
            sink.write(b"x")
        self.assertEqual(raw.getvalue(), b"abcdef")
