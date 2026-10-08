"""Real in-memory TAR/GZIP hostility; no installer or runtime execution."""

import gzip
import hashlib
import io
import tarfile
import unittest
from unittest.mock import patch

from mod_base.build_ci import python_archive
from mod_base.build_ci.worker import WorkerError
from mod_base.model import limits


def tar_bytes(rows):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.GNU_FORMAT) as writer:
        for name, kind, data in rows:
            item = tarfile.TarInfo(name)
            item.mode = 0o755 if kind == "directory" else 0o644
            if kind == "directory":
                item.type = tarfile.DIRTYPE
            elif kind == "link":
                item.type, item.linkname = tarfile.SYMTYPE, data
            elif kind == "file":
                item.size = len(data)
            else:
                item.type = kind
            writer.addfile(item, io.BytesIO(data) if kind == "file" else None)
    return stream.getvalue()


ROWS = [("./", "directory", None), ("./bin/", "directory", None),
        ("./bin/python3.11", "file", b"known fixture binary\0"),
        ("./bin/python3", "link", "python3.11"), ("./empty.py", "file", b"")]


def inspect(raw):
    compressed = gzip.compress(raw, mtime=0)
    return python_archive.inspect_python_installer(io.BytesIO(compressed), expected_size=len(compressed),
                       expected_digest="sha256:" + hashlib.sha256(compressed).hexdigest())


def rechecksum(header):
    header[148:156] = b"        "
    header[148:156] = ("%06o\0 " % sum(header)).encode("ascii")
    return header


class PythonArchiveTests(unittest.TestCase):
    def test_real_gnu_inventory_empty_file_and_links(self):
        result = inspect(tar_bytes(ROWS))
        self.assertIsInstance(result, tuple)
        self.assertEqual([row.path for row in result], ["", "bin", "bin/python3", "bin/python3.11", "empty.py"])
        self.assertEqual(result[2].target, "python3.11")
        self.assertEqual(result[3].sha256, hashlib.sha256(ROWS[2][2]).hexdigest())
        self.assertEqual((result[4].size, result[4].sha256), (0, hashlib.sha256(b"").hexdigest()))

    def test_real_gnu_long_name_is_bounded_and_not_a_member(self):
        name = "./" + "x" * 150 + ".py"
        result = inspect(tar_bytes([ROWS[0], (name, "file", b"source")]))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1].path, name[2:])
        with patch.object(limits, "MAX_CI_TOOL_PATH_BYTES", 100), self.assertRaises(WorkerError):
            inspect(tar_bytes([ROWS[0], (name, "file", b"source")]))

    def test_no_decode_before_independently_supplied_hash_and_size_match(self):
        compressed = gzip.compress(tar_bytes(ROWS), mtime=0)
        digest = "sha256:" + hashlib.sha256(compressed).hexdigest()
        for size, wanted in [(len(compressed), "sha256:" + "0" * 64), (len(compressed) - 1, digest),
                             (len(compressed) + 1, digest), (True, digest), (0, digest),
                             (limits.MAX_CI_PYTHON_ARCHIVE_BYTES + 1, digest), (len(compressed), digest.upper())]:
            with self.subTest(size=size, digest=wanted), patch.object(python_archive, "_inspect") as parser, \
                    self.assertRaises(WorkerError):
                python_archive.inspect_python_installer(io.BytesIO(compressed), expected_size=size, expected_digest=wanted)
            parser.assert_not_called()

    def test_checksum_magic_number_mode_and_unsupported_types(self):
        raw = tar_bytes(ROWS)
        edits = [(148, b"xxxxxxx\0"), (257, b"ustar\x0000"), (100, b"0004755\0"),
                 (124, b"\x80" + b"\0" * 11), (156, b"1"), (156, b"x"),
                 (156, b"g"), (156, b"S"), (156, b"6"), (10, b"\0hidden")]
        for offset, replacement in edits:
            header = bytearray(raw[:512]); header[offset:offset + len(replacement)] = replacement
            if offset != 148:
                rechecksum(header)
            with self.subTest(offset=offset, replacement=replacement), self.assertRaises(WorkerError):
                inspect(bytes(header) + raw[512:])

    def test_traversal_control_absolute_duplicate_and_missing_parent(self):
        for name in ("/absolute", "./../escape", "./bin//double", "./bin/./dot", "./a\\b", "./c:d", "./c\x01d"):
            with self.subTest(name=name), self.assertRaises(WorkerError):
                inspect(tar_bytes([ROWS[0], (name, "file", b"bad")]))
        for rows in ([ROWS[0], ROWS[4], ROWS[4]], [ROWS[0], ("./absent/file", "file", b"bad")],
                     [ROWS[4]], [ROWS[0], ("./parent", "file", b""), ("./parent/child", "file", b"")]):
            with self.subTest(rows=rows), self.assertRaises(WorkerError):
                inspect(tar_bytes(rows))

    def test_links_cannot_escape_cycle_be_dangling_or_resolve_to_a_directory(self):
        for target in ("/outside", "../outside", "./empty.py", "absent", "alias", "bin"):
            with self.subTest(target=target), self.assertRaises(WorkerError):
                inspect(tar_bytes(ROWS + [("./alias", "link", target)]))
        with self.assertRaises(WorkerError):
            inspect(tar_bytes(ROWS + [("./one", "link", "two"), ("./two", "link", "one")]))
        rows = ROWS + [("./one", "link", "two"), ("./two", "link", "empty.py")]
        with patch.object(limits, "MAX_CI_TOOL_SYMLINK_HOPS", 2):
            inspect(tar_bytes(rows))
        with patch.object(limits, "MAX_CI_TOOL_SYMLINK_HOPS", 1), self.assertRaises(WorkerError):
            inspect(tar_bytes(rows))

    def test_header_expanded_depth_and_trailing_padding_caps(self):
        raw = tar_bytes(ROWS)
        with patch.object(limits, "MAX_CI_PYTHON_ARCHIVE_HEADERS", len(ROWS)):
            inspect(raw)
        for name, value in [("MAX_CI_PYTHON_ARCHIVE_HEADERS", len(ROWS) - 1),
                            ("MAX_CI_PYTHON_EXPANDED_BYTES", 512),
                            ("MAX_CI_PYTHON_COMPRESSION_RATIO", 1),
                            ("MAX_CI_PYTHON_TAR_PADDING_BYTES", 1), ("MAX_CI_TOOL_TREE_DEPTH", 1)]:
            with self.subTest(bound=name), patch.object(limits, name, value), self.assertRaises(WorkerError):
                inspect(raw)

    def test_truncation_terminators_padding_and_compressed_crc_are_rejected(self):
        raw = tar_bytes(ROWS)
        for bad in (raw[:512], raw[:2048] + b"\0" * 512, raw + b"junk", raw + b"\0",
                    raw[:2068] + b"x" + raw[2069:]):
            with self.subTest(length=len(bad)), self.assertRaises(WorkerError):
                inspect(bad)
        compressed = gzip.compress(raw, mtime=0)
        corrupt = compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:]
        with self.assertRaises(WorkerError):
            python_archive.inspect_python_installer(io.BytesIO(corrupt), expected_size=len(corrupt),
                                expected_digest="sha256:" + hashlib.sha256(corrupt).hexdigest())

    def test_source_reinspection_after_complete_inventory(self):
        compressed = gzip.compress(tar_bytes(ROWS), mtime=0)
        source = io.BytesIO(compressed)
        original = python_archive._inspect
        def mutate(decoded, **kwargs):
            result = original(decoded, **kwargs)
            source.seek(0); source.write(b"x")
            return result
        with patch.object(python_archive, "_inspect", side_effect=mutate), self.assertRaises(WorkerError):
            python_archive.inspect_python_installer(source, expected_size=len(compressed),
                               expected_digest="sha256:" + hashlib.sha256(compressed).hexdigest())

    def test_stream_io_failure_is_normalized(self):
        source = io.BytesIO(b"bytes")
        source.close()
        with self.assertRaises(WorkerError):
            python_archive.inspect_python_installer(source, expected_size=5, expected_digest="sha256:" + "0" * 64)

    def test_gnu_long_name_cannot_hide_disagreeing_or_orphan_metadata(self):
        raw = tar_bytes([ROWS[0], ("./" + "x" * 150, "file", b"source")])
        changed = bytearray(raw)
        changed[1536] = ord("z")
        changed[1536:2048] = rechecksum(changed[1536:2048])
        for bad in (bytes(changed), raw[:1536] + bytes(1024)):
            with self.subTest(length=len(bad)), self.assertRaises(WorkerError):
                inspect(bad)
