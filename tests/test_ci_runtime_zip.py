"""Real hostile ZIP parsing/streaming with Windows atomic/file-opening substitutions."""

import io
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from mod_base.io import bounded_zip
from mod_base.model import grammar, limits
from tests import test_io_bounded_zip as fixtures


class RuntimeZipTest(unittest.TestCase):
    def extract(self, data, *, scope='lane'):
        """Run actual admission and CRC/size streaming; never substitute archive inspection."""
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            output = root/'output'
            def atomic(destination, writer):
                stage = root/'stage'
                stage.mkdir()
                try:
                    result = writer(stage, stage)
                    # Test-only publication copy avoids Windows scanner rename races.
                    # It asserts no Linux atomic/exclusive syscall guarantee.
                    shutil.copytree(stage, destination)
                    return result
                finally:
                    if stage.exists():
                        shutil.rmtree(stage)
            def opening(stage, name):
                child = stage/name
                child.parent.mkdir(parents=True, exist_ok=True)
                return os.open(child, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
            stack.enter_context(patch.object(bounded_zip.atomic, 'atomic_directory', side_effect=atomic))
            stack.enter_context(patch.object(bounded_zip.atomic, '_open_new_file', side_effect=opening))
            stack.enter_context(patch.object(bounded_zip.atomic, '_seal_new_file', side_effect=os.fsync))
            try:
                names = bounded_zip.extract_runtime(data, output, scope=scope)
            except bounded_zip.ZipRejected:
                self.assertFalse(output.exists())
                self.assertFalse((root/'stage').exists())
                raise
            return names, {name: (output/name).read_bytes() for name in names}

    def test_stored_and_deflated_empty_logs_preserve_actual_bytes(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            data = fixtures.archive({'ci-runtime-envelope.json': b'{}', 'lane/report.json': b'opaque',
                                     'lane/runtime.log': b''}, compression=compression)
            names, files = self.extract(data)
            self.assertEqual(names, sorted(files))
            self.assertEqual(files['lane/runtime.log'], b'')
            self.assertEqual(files['lane/report.json'], b'opaque')
            # ZIP transport alone never admits this intentionally invalid native/outer report.

    def test_existing_pages_and_build_still_refuse_empty_files(self):
        data = fixtures.archive({'empty.log': b''}, compression=zipfile.ZIP_STORED)
        for extract in (lambda: bounded_zip.extract_build(data, Path('unused')),
                        lambda: bounded_zip.extract(data, Path('unused'), bounded_zip.ExtractionLimits(4, 100, 100))):
            with patch.object(bounded_zip.atomic, 'atomic_directory') as publication:
                with self.assertRaisesRegex(bounded_zip.ZipRejected, 'outside 1'):
                    extract()
                publication.assert_not_called()

    def test_invalid_scope_and_compressed_cap_precede_zip_allocation(self):
        with patch.object(bounded_zip.zipfile, 'ZipFile') as parser:
            for scope in (True, None, 'target', 'LANE'):
                with self.assertRaises(bounded_zip.ZipRejected):
                    bounded_zip.extract_runtime(b'bad', Path('unused'), scope=scope)
            with patch.object(limits, 'MAX_CI_BUNDLE_COMPRESSED_BYTES', 2):
                for source in (b'123', bytearray(b'123')):
                    with self.assertRaises(bounded_zip.ZipRejected):
                        bounded_zip.extract_runtime(source, Path('unused'), scope='complete')
                with tempfile.TemporaryDirectory() as temporary:
                    source = Path(temporary)/'oversized.zip'
                    source.write_bytes(b'123')
                    with self.assertRaises(bounded_zip.ZipRejected):
                        bounded_zip.extract_runtime(source, Path(temporary)/'output', scope='complete')
            parser.assert_not_called()

    def test_central_directory_bound_precedes_zipfile_allocation(self):
        data = fixtures.archive({'file': b'x'})
        added = len((grammar.CI_VALIDATION_NAME,)) + limits.MAX_CI_LANES
        oversized = fixtures.patch_eocd(
            data, size=(limits.MAX_CI_RUNTIME_ENTRIES + added) * bounded_zip._MAX_CENTRAL_ENTRY_BYTES + 1)
        with patch.object(bounded_zip.zipfile, 'ZipFile') as parser, self.assertRaises(bounded_zip.ZipRejected):
            self.extract(oversized, scope='complete')
        parser.assert_not_called()
        with patch.object(limits, 'MAX_CI_RUNTIME_ENTRIES', 2), patch.object(bounded_zip.zipfile, 'ZipFile') as parser:
            with self.assertRaises(bounded_zip.ZipRejected):
                self.extract(fixtures.patch_eocd(data, count=3 + added), scope='complete')
            parser.assert_not_called()

    def test_file_count_is_separate_from_legal_directory_entries(self):
        for scope, cap in (('lane', limits.MAX_CI_RUNTIME_FILES), ('complete', limits.MAX_CI_RUNTIME_AGGREGATE_FILES)):
            records = len((grammar.CI_RUNTIME_ENVELOPE_NAME, grammar.CI_VALIDATION_NAME))
            reports = 1 if scope == 'lane' else limits.MAX_CI_LANES
            data = fixtures.archive({f'{index:04d}.log': b'' for index in range(cap + records + reports + 1)})
            with patch.object(bounded_zip, '_extract_entry') as inflate, self.assertRaisesRegex(bounded_zip.ZipRejected, 'file-count'):
                self.extract(data, scope=scope)
            inflate.assert_not_called()
        data = fixtures.archive({'a/': b'', 'a/b/': b'', 'a/b/log': b''})
        self.assertEqual(self.extract(data)[1], {'a/b/log': b''})

    def test_unsafe_names_alias_links_specials_duplicates_and_methods_reject(self):
        for names in ({'../escape': b''}, {'/absolute': b''}, {'a': b'', 'a/b': b''},
                      {'A/one': b'', 'a/two': b''}, {'a/': b''}):
            with self.assertRaises(bounded_zip.ZipRejected):
                self.extract(fixtures.archive(names))
        # Windows ZipInfo normalizes backslashes while authoring; patch stored name bytes.
        with self.assertRaises(bounded_zip.ZipRejected):
            self.extract(fixtures.archive({'aXb': b''}).replace(b'aXb', b'a\\b'))
        with self.assertRaises(bounded_zip.ZipRejected):
            self.extract(fixtures.archive_with([(fixtures.entry('same'), b''), (fixtures.entry('same'), b'')]))
        for mode in (stat.S_IFLNK | 0o777, stat.S_IFIFO | 0o600):
            info = fixtures.entry('unsafe')
            info.external_attr = mode << 16
            with self.assertRaises(bounded_zip.ZipRejected):
                self.extract(fixtures.archive_with([(info, b'x')]))
        data = fixtures.archive({'log': b'x'})
        for mutated in (fixtures.patch_central(data, 0, method=99),
                        fixtures.patch_central(data, 0, flags=1)):
            with self.assertRaises(bounded_zip.ZipRejected):
                self.extract(mutated)

    def test_native_entry_total_and_ratio_bounds_precede_inflation(self):
        maximum = limits.MAX_CI_PNG_BYTES
        compressed = (maximum + limits.MAX_ZIP_RATIO - 1)//limits.MAX_ZIP_RATIO
        data = fixtures.archive({f'{i}.png': b'\0'*compressed for i in range(9)}, compression=zipfile.ZIP_STORED)
        for i in range(9):
            data = fixtures.patch_central(data, i, method=zipfile.ZIP_DEFLATED, size=maximum)
        with patch.object(bounded_zip, '_extract_entry') as inflate, self.assertRaisesRegex(bounded_zip.ZipRejected, 'total byte'):
            self.extract(data)
        inflate.assert_not_called()
        data = fixtures.archive({'file': b'x'}, compression=zipfile.ZIP_STORED)
        for size in (maximum+1, 201):
            mutated = fixtures.patch_central(data, 0, method=zipfile.ZIP_DEFLATED, size=size)
            with patch.object(bounded_zip, '_extract_entry') as inflate, self.assertRaises(bounded_zip.ZipRejected):
                self.extract(mutated)
            inflate.assert_not_called()

    def test_crc_failure_during_actual_streaming_cleans_unpublished_stage(self):
        data = fixtures.archive({'file.log': b'actual payload'}, compression=zipfile.ZIP_STORED)
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            self.assertEqual(package.read('file.log'), b'actual payload')
        mutated = fixtures.patch_central(data, 0, crc=0)
        with self.assertRaisesRegex(bounded_zip.ZipRejected, 'cannot extract'):
            self.extract(mutated)
