"""``io.secure_json``: ported from Block Pops ``tests/test_secure_json.py`` plus the kit's parity
with ``model.canonical`` (one definition of strict and canonical JSON)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.io.secure_json import SecureJsonError, canonical_json, loads, read, read_json, require_object
from mod_base.model import canonical


class SecureJsonLoadsTests(unittest.TestCase):
    def test_accepts_utf8_and_produces_deterministic_canonical_bytes(self) -> None:
        value = loads('{"z":1,"label":"café","items":[true,null]}'.encode(), label="fixture", max_bytes=1024)

        self.assertEqual(canonical_json(value), '{"items":[true,null],"label":"café","z":1}\n'.encode())
        self.assertEqual(canonical_json(value), canonical.canonical_json(value))

    def test_rejects_duplicate_keys_at_any_depth(self) -> None:
        for raw in (b'{"lane":1,"lane":2}', b'{"outer":{"id":1,"id":2}}', b'[{"a":1,"a":1}]'):
            with self.subTest(raw=raw), self.assertRaisesRegex(SecureJsonError, "duplicate JSON object key"):
                loads(raw, label="candidate", max_bytes=1024)

    def test_rejects_non_finite_numbers_invalid_utf8_and_non_bytes(self) -> None:
        for raw in (b'{"number":NaN}', b'{"number":Infinity}', b'{"number":-Infinity}'):
            with self.subTest(raw=raw), self.assertRaisesRegex(SecureJsonError, "non-finite JSON number"):
                loads(raw, label="candidate", max_bytes=1024)
        with self.assertRaisesRegex(SecureJsonError, "non-finite"):
            loads(b'{"number":1e999}', label="candidate", max_bytes=1024)
        with self.assertRaisesRegex(SecureJsonError, "not valid UTF-8"):
            loads(b'\xff', label="candidate", max_bytes=1024)
        with self.assertRaisesRegex(SecureJsonError, "must be bytes"):
            loads("{}", label="candidate", max_bytes=1024)  # type: ignore[arg-type]

    def test_rejects_byte_order_mark_lone_surrogates_and_trailing_data(self) -> None:
        for raw, pattern in ((b'\xef\xbb\xbf{}', "byte-order mark"), (b'{"a":"\\ud800"}', "lone surrogate"),
                             (b'{"\\udfff":1}', "lone surrogate"), (b'{} {}', "not valid JSON")):
            with self.subTest(raw=raw), self.assertRaisesRegex(SecureJsonError, pattern):
                loads(raw, label="candidate", max_bytes=1024)

    def test_rejects_empty_and_oversized_inputs(self) -> None:
        with self.assertRaisesRegex(SecureJsonError, "is empty"):
            loads(b"", label="candidate", max_bytes=4)
        with self.assertRaisesRegex(SecureJsonError, "exceeds the 2-byte input limit"):
            loads(b"{}\n", label="candidate", max_bytes=2)

    def test_errors_are_fail_closed_kit_rejections(self) -> None:
        with self.assertRaises(SecureJsonError) as caught:
            loads(b"[", label="candidate", max_bytes=8)
        self.assertIsInstance(caught.exception, MbError)
        self.assertEqual(2, caught.exception.exit_code)
        self.assertEqual("invalid-json", caught.exception.reason)
        self.assertIn("candidate", str(caught.exception))

    def test_require_object_is_exact_key_fail_closed(self) -> None:
        self.assertEqual(
            require_object({"required": 1, "optional": 2}, label="record", required={"required"},
                           optional={"optional"}),
            {"required": 1, "optional": 2},
        )
        with self.assertRaisesRegex(SecureJsonError, "missing.*unknown"):
            require_object({"surprise": True}, label="record", required={"required"})
        with self.assertRaisesRegex(SecureJsonError, "record must be an object"):
            require_object([], label="record", required=set())
        many = {f"extra{index:03d}": index for index in range(500)}
        with self.assertRaises(SecureJsonError) as caught:
            require_object(many, label="record", required=set())
        self.assertLess(len(str(caught.exception)), 600)

    def test_canonical_json_refuses_values_that_are_not_canonical_json(self) -> None:
        for value in ({1: "int key"}, {"x": float("nan")}, {"x": object()}, {"x": "\ud800"}):
            with self.subTest(value=repr(value)), self.assertRaises(SecureJsonError):
                canonical_json(value)


class SecureJsonFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_reads_one_bounded_regular_file(self) -> None:
        path = self.root / "input.json"
        path.write_bytes(b'{"ok":true}')

        value, raw = read(path, label="input", max_bytes=64)

        self.assertEqual(value, {"ok": True})
        self.assertEqual(raw, b'{"ok":true}')
        self.assertEqual({"ok": True}, read_json(path, label="input", max_bytes=64))

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_rejects_final_component_symlink(self) -> None:
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "input.json"
        link.symlink_to(target)

        with self.assertRaisesRegex(SecureJsonError, "must not be a symlink"):
            read(link, label="input", max_bytes=64)

    def test_rejects_directories_empty_files_oversized_files_and_missing_paths(self) -> None:
        empty = self.root / "empty.json"
        empty.touch()
        large = self.root / "large.json"
        large.write_bytes(b"{} ")
        with self.assertRaisesRegex(SecureJsonError, "regular file"):
            read(self.root, label="input", max_bytes=64)
        with self.assertRaisesRegex(SecureJsonError, "size must be between"):
            read(empty, label="input", max_bytes=64)
        with self.assertRaisesRegex(SecureJsonError, "size must be between"):
            read(large, label="input", max_bytes=2)
        with self.assertRaisesRegex(SecureJsonError, "cannot stat"):
            read(self.root / "absent.json", label="input", max_bytes=64)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs are unavailable")
    def test_rejects_special_files_without_blocking(self) -> None:
        fifo = self.root / "input.json"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(SecureJsonError, "regular file"):
            read(fifo, label="input", max_bytes=64)

    def test_refuses_exactly_what_the_model_reader_refuses(self) -> None:
        cases = {"valid.json": b'{"a":[1,2]}', "duplicate.json": b'{"a":1,"a":2}', "nan.json": b"[NaN]",
                 "empty.json": b"", "bom.json": b"\xef\xbb\xbf[]", "large.json": b"[" + b"1," * 40 + b"1]"}
        for name, data in cases.items():
            path = self.root / name
            path.write_bytes(data)
            with self.subTest(name=name):
                try:
                    expected = canonical.read_json_file(path, label="input", max_bytes=64)
                except MbError:
                    with self.assertRaises(SecureJsonError):
                        read(path, label="input", max_bytes=64)
                else:
                    self.assertEqual(expected, read(path, label="input", max_bytes=64))


if __name__ == "__main__":
    unittest.main()
