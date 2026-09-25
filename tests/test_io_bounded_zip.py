"""``io.bounded_zip``: ported from Quick Skin ``scripts/ci/tests/test_bounded_zip.py`` plus the ZIP
rules of Block Pops ``scripts/pages/download_artifact.py`` (encrypted entries, compression methods,
link/special entries, the 200x ratio) and the kit's own central-directory and output rules."""

from __future__ import annotations

import io
import random
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.io import bounded_zip
from mod_base.io.bounded_zip import LIMITS_BY_KIND, ExtractionLimits, ZipRejected, extract
from mod_base.model import grammar, limits

LIMITS = ExtractionLimits(max_entries=16, max_total_bytes=1 << 20, max_entry_bytes=1 << 19)
CENTRAL = struct.Struct("<4s4B4HL2L5H2L")


def archive(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression) as package:
        for name, payload in entries.items():
            package.writestr(name, payload)
    return stream.getvalue()


def archive_with(infos: list[tuple[zipfile.ZipInfo, bytes]]) -> bytes:
    stream = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(stream, "w") as package:
        warnings.simplefilter("ignore", UserWarning)  # deliberate duplicate names
        for info, payload in infos:
            package.writestr(info, payload)
    return stream.getvalue()


def entry(name: str, *, mode: int | None = None, compression: int = zipfile.ZIP_STORED) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 9, 25, 0, 0, 0))
    info.compress_type = compression
    if mode is not None:
        info.create_system = 3
        info.external_attr = mode << 16
    return info


def patch_central(data: bytes, index: int = 0, **fields: int) -> bytes:
    """Rewrite fields of the ``index``-th central directory header (``flags``, ``method``,
    ``crc``, ``compressed``, ``size``, ``external``) without touching the local header."""

    names = ("signature", "made_by", "system", "needed", "reserved", "flags", "method", "time", "date", "crc", "compressed",
             "size", "name_length", "extra_length", "comment_length", "disk", "internal", "external", "offset")
    buffer = bytearray(data)
    position = -1
    for _ in range(index + 1):
        position = buffer.index(b"PK\x01\x02", position + 1)
    values = dict(zip(names, CENTRAL.unpack_from(buffer, position)))
    values.update(fields)
    CENTRAL.pack_into(buffer, position, *(values[name] for name in names))
    return bytes(buffer)


def zip64_offset(data: bytes, offset: int) -> bytes:
    """Move the first central entry's local-header offset into a Zip64 extra field of ``offset``
    (the 32-bit field becomes 0xFFFFFFFF), keeping the end record consistent."""

    buffer = bytearray(data)
    position = buffer.index(b"PK\x01\x02")
    name_length, extra_length, comment_length = struct.unpack_from("<3H", buffer, position + 28)
    name_end = position + 46 + name_length
    extra = struct.pack("<HHQ", 1, 8, offset)
    header = bytearray(buffer[position:name_end]) + extra
    struct.pack_into("<H", header, 30, len(extra))
    struct.pack_into("<L", header, 42, 0xFFFFFFFF)
    tail = buffer[name_end + extra_length + comment_length:]
    rebuilt = buffer[:position] + header + buffer[name_end + extra_length:name_end + extra_length + comment_length]
    eocd = tail.rindex(b"PK\x05\x06")
    directory = rebuilt[position:] + tail[:eocd]
    end = bytearray(tail[eocd:])
    struct.pack_into("<L", end, 12, len(directory))
    return bytes(rebuilt + tail[:eocd] + end)


def patch_eocd(data: bytes, **fields: int) -> bytes:
    names = ("signature", "disk", "disk_directory", "disk_count", "count", "size", "offset", "comment")
    buffer = bytearray(data)
    position = buffer.rindex(b"PK\x05\x06")
    values = dict(zip(names, bounded_zip._EOCD.unpack_from(buffer, position)))
    values.update(fields)
    bounded_zip._EOCD.pack_into(buffer, position, *(values[name] for name in names))
    return bytes(buffer)


class BoundedZipTestCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.index = 0

    def destination(self) -> Path:
        self.index += 1
        return self.root / f"out-{self.index}"

    def assert_rejected(self, data: bytes | Path, pattern: str = "", limits_: ExtractionLimits = LIMITS) -> None:
        destination = self.destination()
        with self.assertRaisesRegex(ZipRejected, pattern):
            extract(data, destination, limits_)
        self.assertFalse(destination.exists())
        self.assertEqual([], [path for path in self.root.iterdir() if path.name.startswith(".")])


class ExtractTests(BoundedZipTestCase):
    def test_extracts_regular_entries_into_a_fresh_destination(self) -> None:
        for compression in (zipfile.ZIP_DEFLATED, zipfile.ZIP_STORED):
            with self.subTest(compression=compression):
                destination = self.destination()
                data = archive({"manifest.json": b"{}\n", "images/deep/frame.png": b"\x89PNG" * 10},
                               compression=compression)
                self.assertEqual(["images/deep/frame.png", "manifest.json"], extract(data, destination, LIMITS))
                self.assertEqual(b"{}\n", (destination / "manifest.json").read_bytes())
                self.assertEqual(b"\x89PNG" * 10, (destination / "images/deep/frame.png").read_bytes())
                self.assertEqual(0o644, stat.S_IMODE((destination / "manifest.json").stat().st_mode))
                self.assertEqual(0o700, stat.S_IMODE((destination / "images").stat().st_mode))

    def test_accepts_a_regular_file_path_and_explicit_parent_directories(self) -> None:
        path = self.root / "evidence.zip"
        path.write_bytes(archive({"logs/": b"", "logs/failure.txt": b"failure\n", "top/": b"", "top/x/y.txt": b"y"}))
        destination = self.destination()
        self.assertEqual(["logs/failure.txt", "top/x/y.txt"], extract(path, destination, LIMITS))
        self.assertEqual(b"failure\n", (destination / "logs/failure.txt").read_bytes())

    def test_rejects_traversal_absolute_backslash_and_case_collisions(self) -> None:
        cases = (
            {"../escape": b"x"},
            {"/absolute": b"x"},
            {"bad\\path": b"x"},
            {"a/./b": b"x"},
            {"a//b": b"x"},
            {"drive:name": b"x"},
            {"control\x01": b"x"},
            {"space name": b"x"},
            {"café": b"x"},
            {"A.txt": b"x", "a.txt": b"y"},
            {"A/first.txt": b"x", "a/second.txt": b"y"},
            {"parent": b"x", "parent/child": b"y"},
            {"parent/child": b"y", "parent": b"x"},
            {"x" * 600: b"x"},
        )
        for entries in cases:
            with self.subTest(entries=list(entries)[:2]):
                self.assert_rejected(archive(entries))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_rejects_names_truncated_or_duplicated_by_zipfile(self) -> None:
        truncated = archive({"realXhidden.txt": b"x"}, compression=zipfile.ZIP_STORED)
        truncated = truncated.replace(b"realXhidden.txt", b"real\x00hidden.txt")
        self.assertEqual(["real"], zipfile.ZipFile(io.BytesIO(truncated)).namelist())  # what zipfile would write
        self.assert_rejected(truncated, "unsafe entry name")
        self.assert_rejected(archive_with([(entry("same"), b"x"), (entry("same"), b"y")]), "duplicate")
        self.assert_rejected(archive_with([(entry("dir/"), b""), (entry("dir/"), b""), (entry("dir/f"), b"f")]),
                             "duplicate")

    def test_rejects_symlink_and_special_file_entries(self) -> None:
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600, stat.S_IFCHR | 0o600, stat.S_IFBLK | 0o600,
                     stat.S_IFSOCK | 0o600):
            with self.subTest(mode=oct(mode)):
                self.assert_rejected(archive_with([(entry("unsafe", mode=mode), b"target")]), "link|special")

    def test_rejects_entries_whose_type_disagrees_with_their_name(self) -> None:
        self.assert_rejected(archive_with([(entry("file", mode=stat.S_IFDIR | 0o755), b"x")]), "disagrees")
        self.assert_rejected(archive_with([(entry("dir/", mode=stat.S_IFREG | 0o644), b""),
                                           (entry("dir/file"), b"x")]), "disagrees")
        dos = entry("file")
        dos.external_attr = 0x10
        self.assert_rejected(archive_with([(dos, b"x")]), "directory attribute")

    def test_directory_entries_are_accepted_only_as_empty_parents(self) -> None:
        self.assert_rejected(archive({"lonely/": b"", "file.txt": b"x"}), "not the parent")
        self.assert_rejected(archive({"only/": b""}), "no files")
        data = patch_central(archive({"dir/": b"", "dir/file": b"x"}, compression=zipfile.ZIP_STORED), 0, size=4)
        self.assert_rejected(data, "carries data")

    def test_rejects_encrypted_entries(self) -> None:
        for flag in (0x1, 0x40, 0x2000):
            with self.subTest(flag=hex(flag)):
                self.assert_rejected(patch_central(archive({"secret.txt": b"payload" * 10}), flags=flag), "encrypted")

    def test_rejects_unapproved_compression_methods(self) -> None:
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA, 9, 99):
            with self.subTest(method=method):
                data = patch_central(archive({"data.txt": b"payload" * 10}), method=method)
                self.assert_rejected(data, "compression method")

    def test_rejects_a_compression_ratio_above_the_bound(self) -> None:
        bomb = archive({"bomb.txt": b"\0" * 400_000})
        info = zipfile.ZipFile(io.BytesIO(bomb)).infolist()[0]
        self.assertGreater(info.file_size, info.compress_size * limits.MAX_ZIP_RATIO)
        self.assert_rejected(bomb, "compression ratio exceeds 200")

    def test_ratio_bound_is_inclusive_and_configurable(self) -> None:
        data = archive({"text.txt": b"abcdefgh" * 400})
        info = zipfile.ZipFile(io.BytesIO(data)).infolist()[0]
        ratio = info.file_size / info.compress_size
        self.assertLess(ratio, limits.MAX_ZIP_RATIO)
        below = ExtractionLimits(16, 1 << 20, 1 << 19, max_ratio=int(ratio))
        self.assert_rejected(data, "compression ratio", below)
        at_or_above = ExtractionLimits(16, 1 << 20, 1 << 19, max_ratio=int(ratio) + 1)
        self.assertEqual(["text.txt"], extract(data, self.destination(), at_or_above))
        self.assert_rejected(data, "ratio may not exceed", ExtractionLimits(16, 1 << 20, 1 << 19, max_ratio=201))

    def test_rejects_entry_total_count_and_suffix_limits(self) -> None:
        small = ExtractionLimits(max_entries=2, max_total_bytes=32, max_entry_bytes=20)
        self.assert_rejected(archive({"large.txt": b"a" * 21}), "size is outside", small)
        self.assert_rejected(archive({"a.txt": b"a" * 20, "b.txt": b"b" * 20}), "total byte bound", small)
        self.assert_rejected(archive({"a": b"a", "b": b"b", "c": b"c"}), "entry count|central directory exceeds",
                             small)
        self.assert_rejected(archive({"empty.json": b""}), "size is outside")
        typed = ExtractionLimits(8, 1024, 512, suffixes=frozenset({".json", ".webp"}))
        self.assert_rejected(archive({"manifest.json": b"{}", "script.sh": b"#!"}), "unapproved suffix", typed)
        self.assertEqual(["a.webp", "manifest.json"],
                         extract(archive({"manifest.json": b"{}", "a.webp": b"RIFF"}), self.destination(), typed))

    def test_invalid_limits_are_rejected(self) -> None:
        data = archive({"a.txt": b"a"})
        for bad in (ExtractionLimits(0, 1, 1), ExtractionLimits(1, True, 1), ExtractionLimits(1, 1, -1),
                    ExtractionLimits(1, 1, 1, suffixes=frozenset({"json"})),
                    ExtractionLimits(1, 1, 1, suffixes=frozenset()), None, {"max_entries": 1}):
            with self.subTest(limits=bad):
                self.assert_rejected(data, "extraction", bad)  # type: ignore[arg-type]
                with self.assertRaisesRegex(ZipRejected, "extraction"):
                    bounded_zip.archive_limit(bad)  # type: ignore[arg-type]

    def test_rejects_data_that_disagrees_with_the_central_directory(self) -> None:
        stored = archive({"value.txt": b"0123456789" * 10}, compression=zipfile.ZIP_STORED)
        info = zipfile.ZipFile(io.BytesIO(stored)).infolist()[0]
        self.assert_rejected(patch_central(stored, crc=info.CRC ^ 1), "cannot extract")
        self.assert_rejected(patch_central(stored, size=info.file_size - 1), "sizes disagree")
        deflated = archive({"value.txt": b"0123456789" * 10})
        info = zipfile.ZipFile(io.BytesIO(deflated)).infolist()[0]
        self.assert_rejected(patch_central(deflated, size=info.file_size - 5), "cannot extract|expanded")
        local = bytearray(stored)
        name_offset = local.index(b"value.txt")
        local[name_offset] = ord("V")
        self.assert_rejected(bytes(local), "cannot extract")

    def test_rejects_garbage_truncated_and_empty_archives(self) -> None:
        full = archive({"a.txt": b"a" * 100})
        for data in (b"not a zip", full[: len(full) // 2], full[:-3], b"PK\x05\x06" + b"\0" * 18):
            with self.subTest(data=data[:8]):
                self.assert_rejected(data)
        self.assert_rejected(b"", "size is outside")
        self.assert_rejected(archive({}), "no end-of-central-directory|entry count|outside")

    def test_existing_destination_symlinked_and_special_archives_are_refused(self) -> None:
        data = archive({"value.txt": b"value"})
        path = self.root / "evidence.zip"
        path.write_bytes(data)
        occupied = self.root / "occupied"
        occupied.mkdir()
        with self.assertRaisesRegex(ZipRejected, "existing output"):
            extract(data, occupied, LIMITS)
        self.assertEqual([], list(occupied.iterdir()))
        linked = self.root / "linked.zip"
        linked.symlink_to(path)
        self.assert_rejected(linked, "regular file")
        self.assert_rejected(self.root, "regular file")
        with self.assertRaisesRegex(ZipRejected, "bytes or a path"):
            extract(str(path), self.destination(), LIMITS)  # type: ignore[arg-type]
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "parent-link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ZipRejected):
            extract(data, self.root / "parent-link" / "out", LIMITS)
        self.assertEqual([], list(outside.iterdir()))

    def test_oversized_archives_are_refused_before_parsing(self) -> None:
        tiny = ExtractionLimits(max_entries=1, max_total_bytes=1, max_entry_bytes=1)
        oversized = b"\0" * (bounded_zip.archive_limit(tiny) + 1)
        with patch.object(bounded_zip.zipfile, "ZipFile") as parser:
            self.assert_rejected(oversized, "archive size", tiny)
        parser.assert_not_called()

    def test_failure_while_extracting_leaves_no_destination_or_stage(self) -> None:
        data = archive({"a.txt": b"a" * 10, "b.txt": b"b" * 10})
        calls = []
        original = bounded_zip._extract_entry

        def failing(package, item, stage, limits_, extracted):
            calls.append(item.name)
            if len(calls) == 2:
                raise ZipRejected("simulated failure")
            return original(package, item, stage, limits_, extracted)

        with patch.object(bounded_zip, "_extract_entry", failing):
            self.assert_rejected(data, "simulated failure")
        self.assertEqual(["a.txt", "b.txt"], calls)

    def test_errors_are_fail_closed_kit_rejections(self) -> None:
        self.assertTrue(issubclass(ZipRejected, MbError))
        self.assertEqual(2, ZipRejected("x").exit_code)
        self.assertEqual("zip-rejected", ZipRejected("x").reason)


class CentralDirectoryBoundTests(BoundedZipTestCase):
    def test_counts_and_sizes_beyond_the_limits_are_refused_before_zipfile_parses(self) -> None:
        data = archive({"a.txt": b"a"})
        for fields in ({"count": LIMITS.max_entries + 1},
                       {"size": LIMITS.max_entries * bounded_zip._MAX_CENTRAL_ENTRY_BYTES + 1},
                       {"size": 0xFFFFFFFF}):
            with self.subTest(fields=fields), patch.object(bounded_zip.zipfile, "ZipFile") as parser:
                self.assert_rejected(patch_eocd(data, **fields), "central directory exceeds")
                parser.assert_not_called()

    def test_the_record_is_located_like_zipfile_behind_a_comment(self) -> None:
        def commented(comment: bytes) -> bytes:
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as package:
                package.writestr("a.txt", b"a")
                package.comment = comment
            return stream.getvalue()

        data = commented(b"an archive comment " + b"x" * 64)
        self.assertEqual(["a.txt"], extract(data, self.destination(), LIMITS))
        self.assert_rejected(patch_eocd(data, count=LIMITS.max_entries + 1), "central directory exceeds")
        # zipfile selects the last signature, even inside the comment; the bound reads that record too.
        decoy = commented(b"PK\x05\x06" + b"\xff" * 18 + b" decoy")
        self.assertEqual([], zipfile.ZipFile(io.BytesIO(commented(b"PK\x05\x06" + b"\0" * 18 + b" decoy"))).namelist())
        with patch.object(bounded_zip.zipfile, "ZipFile") as parser:
            self.assert_rejected(decoy, "central directory exceeds")
        parser.assert_not_called()

    def test_zip64_end_records_are_bounded_too(self) -> None:
        stream = io.BytesIO()
        with patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 0), zipfile.ZipFile(stream, "w") as package:
            package.writestr("a.txt", b"a")
        data = stream.getvalue()
        self.assertIn(bounded_zip._ZIP64_EOCD_SIGNATURE, data)
        self.assertEqual(["a.txt"], extract(data, self.destination(), LIMITS))
        position = data.rindex(bounded_zip._ZIP64_EOCD_SIGNATURE)
        for index, value in ((7, LIMITS.max_entries + 1), (8, 1 << 40)):
            with self.subTest(field=index):
                buffer = bytearray(data)
                fields = list(bounded_zip._ZIP64_EOCD.unpack_from(buffer, position))
                fields[index] = value
                bounded_zip._ZIP64_EOCD.pack_into(buffer, position, *fields)
                with patch.object(bounded_zip.zipfile, "ZipFile") as parser:
                    self.assert_rejected(bytes(buffer), "central directory exceeds")
                parser.assert_not_called()


class HostileDirectoryTests(BoundedZipTestCase):
    """Whatever :mod:`zipfile` raises for a hostile directory surfaces as :class:`ZipRejected`."""

    def as_path(self, data: bytes) -> Path:
        self.index += 1
        path = self.root / f"archive-{self.index}.zip"
        path.write_bytes(data)
        return path

    def assert_rejected_both_ways(self, data: bytes, pattern: str = "") -> None:
        self.assert_rejected(data, pattern)
        path = self.as_path(data)
        self.assert_rejected(path, pattern)
        path.unlink()

    def test_a_version_needed_beyond_zipfile_support_is_a_rejection(self) -> None:
        data = archive({"a.json": b"{}" * 50})
        for needed in (64, 200, 255):
            with self.subTest(needed=needed):
                self.assert_rejected_both_ways(patch_central(data, needed=needed), "not a valid ZIP")

    def test_local_header_offsets_outside_the_archive_are_rejections(self) -> None:
        data = archive({"a.json": b"{}"}, compression=zipfile.ZIP_STORED)
        self.assertEqual(["a.json"], extract(zip64_offset(data, 0), self.destination(), LIMITS))
        for offset in (2**63 - 1, 2**63, 2**64 - 1, len(data)):
            with self.subTest(offset=offset):
                self.assert_rejected_both_ways(zip64_offset(data, offset), "outside the archive|not a valid ZIP")
        self.assert_rejected_both_ways(patch_central(data, offset=len(data) - 31), "outside the archive")

    def test_entries_sharing_archive_bytes_are_rejected(self) -> None:
        data = archive({"a.json": b"{}" * 40, "b.json": b"[]" * 40}, compression=zipfile.ZIP_STORED)
        for offset in (0, 10, 30 + len("a.json") + 80 - 1):
            with self.subTest(offset=offset), patch.object(bounded_zip.zipfile.ZipFile, "open") as reader:
                self.assert_rejected(patch_central(data, 1, offset=offset), "overlap")
                reader.assert_not_called()
        self.assertEqual(["a.json", "b.json"], extract(data, self.destination(), LIMITS))

    def test_random_corruption_is_always_a_kit_rejection(self) -> None:
        """Seeded single-byte corruption, half of it aimed at the central directory and end record:
        every outcome is a clean extraction or a :class:`ZipRejected`, never another exception."""

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as package:
            package.writestr("images/", b"")
            package.writestr("images/a.json", b'{"frame":1}' * 20, compress_type=zipfile.ZIP_DEFLATED)
            package.writestr("images/b.json", b'{"frame":2}', compress_type=zipfile.ZIP_STORED)
            package.writestr("manifest.json", b"{}" * 30, compress_type=zipfile.ZIP_DEFLATED)
        data = stream.getvalue()
        directory = data.index(b"PK\x01\x02")
        generator = random.Random(20260925)
        for trial in range(600):
            buffer = bytearray(data)
            start = directory if trial % 2 else 0
            position = generator.randrange(start, len(buffer))
            buffer[position] = generator.choice((0, 0xFF, 0x7F, 0x80, generator.randrange(256)))
            destination = self.destination()
            try:
                extract(bytes(buffer), destination, LIMITS)
            except ZipRejected:
                self.assertFalse(destination.exists(), f"trial {trial} left output")
            except Exception as exc:  # noqa: BLE001 - the property under test
                self.fail(f"trial {trial} (byte {position}) escaped as {type(exc).__name__}: {exc}")
        self.assertEqual([], [path for path in self.root.iterdir() if path.name.startswith(".")])


class KindLimitsTests(unittest.TestCase):
    def test_every_downloadable_kit_artifact_kind_has_valid_bounded_limits(self) -> None:
        self.assertEqual(set(grammar.ARTIFACT_PREFIXES) | {"promotion"}, set(LIMITS_BY_KIND))
        for kind, value in LIMITS_BY_KIND.items():
            with self.subTest(kind=kind):
                bounded_zip._check_limits(value)
                self.assertEqual(limits.MAX_ZIP_RATIO, value.max_ratio)
                self.assertLessEqual(value.max_entry_bytes, value.max_total_bytes)
        self.assertEqual(frozenset({".json", ".png"}), LIMITS_BY_KIND["handoff"].suffixes)
        self.assertEqual(frozenset({".json", ".webp"}), LIMITS_BY_KIND["cache"].suffixes)
        self.assertIsNone(LIMITS_BY_KIND["collected-family"].suffixes)  # source/ carries native files

    def test_a_collected_family_artifact_with_native_source_files_extracts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "family"
            data = archive({"paired.json": b"{}", "images/" + "a" * 64 + ".webp": b"RIFF",
                            "source/envelope.json": b"{}", "source/native/report.txt": b"native"})
            self.assertIn("source/native/report.txt", extract(data, destination, LIMITS_BY_KIND["collected-family"]))


if __name__ == "__main__":
    unittest.main()
