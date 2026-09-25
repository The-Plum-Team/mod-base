"""``io.content_cache``: the library part of Block Pops ``tests/test_content_cache.py``.

Content-hash memoization never replaces a changed parameter or a failed computation."""

from __future__ import annotations

import unittest

from mod_base.io.content_cache import ContentCache


class ContentCacheTests(unittest.TestCase):
    def test_failures_are_recomputed_and_parameters_are_part_of_the_key(self) -> None:
        cache = ContentCache(entries=8)
        calls = []

        def failing():
            calls.append("fail")
            raise ValueError("rejected")

        for _ in range(2):
            with self.assertRaises(ValueError):
                cache.get_or_compute(b"bytes", ("a",), failing)
        self.assertEqual(["fail", "fail"], calls)
        self.assertEqual(0, len(cache))

        self.assertEqual(1, cache.get_or_compute(b"bytes", ("a",), lambda: 1))
        self.assertEqual(1, cache.get_or_compute(b"bytes", ("a",), lambda: 2))
        self.assertEqual(3, cache.get_or_compute(b"bytes", ("b",), lambda: 3))
        self.assertEqual(4, cache.get_or_compute(b"other", ("a",), lambda: 4))
        self.assertEqual(5, cache.get_or_compute(b"bytes", ("a", 1), lambda: 5))

    def test_key_binds_the_exact_bytes_not_only_their_prefix(self) -> None:
        cache = ContentCache(entries=8)
        self.assertEqual("short", cache.get_or_compute(b"ab", (), lambda: "short"))
        self.assertEqual("long", cache.get_or_compute(b"ab\x00", (), lambda: "long"))
        self.assertEqual("byte array", cache.get_or_compute(bytearray(b"xy"), (), lambda: "byte array"))
        self.assertEqual("byte array", cache.get_or_compute(b"xy", (), lambda: "other"))

    def test_callers_cannot_alter_a_stored_result(self) -> None:
        cache = ContentCache(entries=8)
        first = cache.get_or_compute(b"bytes", (), lambda: {"width": 1600, "nested": [1]})
        first["width"] = 1
        first["nested"].append(2)
        second = cache.get_or_compute(b"bytes", (), lambda: {"width": 0})
        self.assertEqual({"width": 1600, "nested": [1]}, second)
        second["nested"].clear()
        self.assertEqual({"width": 1600, "nested": [1]}, cache.get_or_compute(b"bytes", (), lambda: {}))

    def test_entries_and_bytes_stay_bounded_least_recent_first(self) -> None:
        cache = ContentCache(entries=2, max_bytes=10)
        cache.get_or_compute(b"a", (), lambda: b"1234")
        cache.get_or_compute(b"b", (), lambda: b"1234")
        cache.get_or_compute(b"a", (), lambda: b"miss")  # refresh a
        cache.get_or_compute(b"c", (), lambda: b"1234")  # evicts b
        self.assertEqual(2, len(cache))
        self.assertEqual(b"1234", cache.get_or_compute(b"a", (), lambda: b"miss"))
        self.assertEqual(b"miss", cache.get_or_compute(b"b", (), lambda: b"miss"))

        cache.get_or_compute(b"big", (), lambda: b"x" * 9, size=len)
        self.assertLessEqual(cache.stored_bytes, 10)
        self.assertEqual(b"y" * 11, cache.get_or_compute(b"huge", (), lambda: b"y" * 11, size=len))
        self.assertLessEqual(cache.stored_bytes, 10)
        self.assertEqual(b"recomputed", cache.get_or_compute(b"huge", (), lambda: b"recomputed", size=len))

    def test_clear_and_invalid_bounds(self) -> None:
        cache = ContentCache(entries=4, max_bytes=100)
        cache.get_or_compute(b"a", (), lambda: b"xyz", size=len)
        self.assertEqual(3, cache.stored_bytes)
        cache.clear()
        self.assertEqual((0, 0), (len(cache), cache.stored_bytes))
        for arguments in ({"entries": 0}, {"entries": -1}, {"entries": True}, {"entries": 1, "max_bytes": 0},
                          {"entries": 1, "max_bytes": 1.5}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ContentCache(**arguments)

    def test_malformed_keys_and_sizes_are_programming_errors(self) -> None:
        cache = ContentCache(entries=2, max_bytes=10)
        with self.assertRaises(TypeError):
            cache.get_or_compute("text", (), lambda: 1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            cache.get_or_compute(b"x", ["list"], lambda: 1)  # type: ignore[arg-type]
        for size in (-1, 1.5, True):
            with self.subTest(size=size), self.assertRaises(ValueError):
                cache.get_or_compute(b"x", (repr(size),), lambda: 1, size=lambda _value, size=size: size)
        self.assertEqual(0, len(cache))


if __name__ == "__main__":
    unittest.main()
