from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model.canonical import (
    StrictJsonError,
    canonical_json,
    canonical_sha256,
    read_json_file,
    read_regular_file,
    strict_loads,
)
from tests.helpers import DOCUMENT_FIXTURES

RAW = DOCUMENT_FIXTURES / "invalid" / "raw"


class StrictLoadsTest(unittest.TestCase):
    def test_raw_fixtures_are_rejected_before_validation(self) -> None:
        expected = {
            "duplicate-key.json": "duplicate JSON object key 'sha'",
            "nested-duplicate-key.json": "duplicate JSON object key 'c'",
            "nan.json": "non-finite JSON number 'NaN'",
            "infinity.json": "non-finite JSON number 'Infinity'",
            "negative-infinity.json": "non-finite JSON number '-Infinity'",
            "overflow-number.json": "contains a non-finite number",
            "byte-order-mark.json": "byte-order mark",
            "invalid-utf8.json": "not valid UTF-8",
            "empty.json": "is empty",
            "trailing-data.json": "is not valid JSON",
            "lone-surrogate.json": "contains a lone surrogate escape",
            "lone-surrogate-key.json": "lone surrogate escape in an object key",
        }
        self.assertEqual(sorted(expected), sorted(path.name for path in RAW.iterdir() if path.name != "not-an-object.json"))
        for name, message in expected.items():
            with self.subTest(name=name), self.assertRaises(StrictJsonError) as caught:
                strict_loads((RAW / name).read_bytes(), label=name, max_bytes=1 << 20)
            self.assertIn(message, str(caught.exception))
            self.assertEqual(caught.exception.exit_code, 2)

    def test_size_bound_is_checked_before_decoding(self) -> None:
        with self.assertRaisesRegex(StrictJsonError, "exceeds the 4-byte input limit"):
            strict_loads(b'{"a":1}', label="doc", max_bytes=4)

    def test_rejects_non_bytes(self) -> None:
        with self.assertRaises(StrictJsonError):
            strict_loads('{"a":1}', label="doc", max_bytes=100)  # type: ignore[arg-type]

    def test_deep_nesting_fails_closed(self) -> None:
        data = b"[" * 100_000 + b"]" * 100_000
        with self.assertRaises(StrictJsonError):
            strict_loads(data, label="deep", max_bytes=len(data))

    def test_oversized_integer_literal_fails_closed(self) -> None:
        data = b'{"a":' + b"9" * 5000 + b"}"
        with self.assertRaisesRegex(StrictJsonError, "is not valid JSON"):
            strict_loads(data, label="doc", max_bytes=len(data))

    def test_lone_surrogate_escapes_fail_closed(self) -> None:
        for data in (b'"\\ud800"', b'{"a":"x\\udfffy"}', b'{"\\ud800":1}', b'[{"a":["\\udc00"]}]',
                     b'"\\udc00\\ud800"'):
            with self.subTest(data=data), self.assertRaisesRegex(StrictJsonError, "lone surrogate"):
                strict_loads(data, label="doc", max_bytes=100)
        # A surrogate *pair* escape is one valid code point, so it is accepted and re-encodable.
        value = strict_loads(b'{"a":"\\ud83d\\ude00"}', label="doc", max_bytes=100)
        self.assertEqual(value, {"a": "\U0001F600"})
        self.assertEqual(canonical_json(value), '{"a":"\U0001F600"}\n'.encode("utf-8"))

    def test_accepts_strict_json(self) -> None:
        self.assertEqual(strict_loads(b'{"a":[1,2.5,"\xc3\xa9"],"b":null}', label="d", max_bytes=100),
                         {"a": [1, 2.5, "é"], "b": None})


class CanonicalJsonTest(unittest.TestCase):
    def test_sorted_compact_utf8_with_trailing_newline(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": ["é", 1.5, None, True]}),
                         '{"a":["é",1.5,null,true],"b":1}\n'.encode("utf-8"))

    def test_rejects_non_finite_and_unencodable(self) -> None:
        for value in (float("nan"), float("inf"), {"a": object()}, "\ud800", {"a": ["x\udfff"]}, {"\ud800": 1}):
            with self.subTest(value=value), self.assertRaises(MbError) as caught:
                canonical_json(value)
            self.assertEqual(caught.exception.exit_code, 2)

    def test_rejects_non_string_keys_instead_of_coercing_them(self) -> None:
        for value in ({True: 1}, {1: "x"}, {None: 1}, {"a": [{2.5: 1}]}, {"a": 1, 2: 3}):
            with self.subTest(value=value), self.assertRaisesRegex(MbError, "is not a string"):
                canonical_json(value)

    def test_canonical_sha256_hashes_the_document_bytes(self) -> None:
        import hashlib

        value = {"z": 1, "a": 2}
        self.assertEqual(canonical_sha256(value), hashlib.sha256(canonical_json(value)).hexdigest())

    def test_round_trip_is_stable(self) -> None:
        value = {"k": [1, {"x": "y"}], "n": 0.1}
        data = canonical_json(value)
        self.assertEqual(canonical_json(strict_loads(data, label="d", max_bytes=1000)), data)


class ReadRegularFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_reads_a_regular_file(self) -> None:
        path = self.root / "doc.json"
        path.write_bytes(b'{"a":1}')
        self.assertEqual(read_regular_file(path, label="doc", max_bytes=100), b'{"a":1}')
        self.assertEqual(read_json_file(path, label="doc", max_bytes=100), ({"a": 1}, b'{"a":1}'))

    def test_binary_bytes_and_crlf_json_are_preserved_exactly(self) -> None:
        path = self.root / "payload.bin"
        for payload in (b"header\r\nbody\x1afooter\r\n", bytes(range(256)),
                        b"x" * ((1 << 16) - 1) + b"\r\n\x1a\x00tail"):
            path.write_bytes(payload)
            with self.subTest(size=len(payload)):
                self.assertEqual(read_regular_file(path, label="binary", max_bytes=len(payload)), payload)
        raw = b'{\r\n"a":1\r\n}\r\n'
        path.write_bytes(raw)
        self.assertEqual(read_json_file(path, label="doc", max_bytes=len(raw)), ({"a": 1}, raw))

    def test_refuses_symlink_directory_empty_and_oversize(self) -> None:
        target = self.root / "target.json"
        target.write_bytes(b"{}")
        link = self.root / "link.json"
        os.symlink(target, link)
        empty = self.root / "empty.json"
        empty.write_bytes(b"")
        big = self.root / "big.json"
        big.write_bytes(b"x" * 11)
        cases = {link: "must not be a symlink", self.root: "must be a regular file",
                 empty: "size must be between 1", big: "size must be between 1 and 10",
                 self.root / "missing.json": "cannot stat"}
        for path, message in cases.items():
            with self.subTest(path=path.name), self.assertRaisesRegex(StrictJsonError, message):
                read_regular_file(path, label="doc", max_bytes=10)
        self.assertEqual(read_regular_file(empty, label="doc", max_bytes=10, allow_empty=True), b"")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires FIFOs")
    def test_refuses_a_fifo_without_blocking(self) -> None:
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(StrictJsonError, "must be a regular file"):
            read_regular_file(fifo, label="pipe", max_bytes=10)


if __name__ == "__main__":
    unittest.main()
