from __future__ import annotations

import re
import unittest

from mod_base.model.validators import (
    Bool,
    Const,
    DocumentError,
    Int,
    List,
    Map,
    Nullable,
    Num,
    Obj,
    OneOf,
    Region,
    Size,
    Str,
    is_display_text,
    is_evidence_text,
)


class CombinatorTest(unittest.TestCase):
    def assertRejects(self, validator, value, message: str) -> None:  # noqa: N802
        with self.assertRaises(DocumentError) as caught:
            validator(value, "$.x")
        self.assertIn(message, str(caught.exception))
        self.assertTrue(caught.exception.path.startswith("$.x"))

    def test_integers_never_accept_bool_or_float(self) -> None:
        validator = Int(0, 10)
        self.assertEqual(validator(3, "$"), 3)
        for bad in (True, False, 3.0, "3", None):
            self.assertRejects(validator, bad, "must be an integer")
        self.assertRejects(validator, 11, "must be between 0 and 10")

    def test_numbers_accept_int_and_float_but_not_bool_or_non_finite(self) -> None:
        validator = Num(0.0, 1.0)
        self.assertEqual(validator(1, "$"), 1)
        self.assertEqual(validator(0.5, "$"), 0.5)
        for bad in (True, "0.5", None):
            self.assertRejects(validator, bad, "must be a number")
        for bad in (float("nan"), float("inf")):
            self.assertRejects(validator, bad, "must be finite")

    def test_strings(self) -> None:
        self.assertRejects(Str(max_len=3), "abcd", "length must be between 1 and 3")
        self.assertRejects(Str(re.compile(r"^[a-z]+$")), "A", "does not match the required grammar")
        self.assertRejects(Str(choices=("a", "b")), "c", "must be one of")
        self.assertRejects(Str(), 1, "must be a string")

    def test_evidence_text_rule(self) -> None:
        self.assertTrue(is_evidence_text("a" * 4096, 4096))
        self.assertTrue(is_evidence_text("PASS \u0085 \u00e9", 100))
        self.assertTrue(is_evidence_text("PASS \U0001F600 paired surrogates decode to one code point", 100))
        for bad in ("a" * 4097, "", "   ", "a\nb", "a\tb", "a\x00", "a\x7f", "\x1b[0m", 5, "PASS \ud800",
                    "\udfff", "a\udc80b"):
            with self.subTest(bad=bad):
                self.assertFalse(is_evidence_text(bad, 4096))

    def test_display_text_rule(self) -> None:
        for good in ("Cape, skin, title screen\u2026", "Box, claw machine, figure\u2026",
                     "R\u00e9f \u2192 \u2713 \u00b7 \u2014 \u65e5\u672c", "A & B"):
            with self.subTest(good=good):
                self.assertTrue(is_display_text(good, 80))
        for bad in ("<b>", "a > b", "{{mb:name}}", "x }}", " padded", "a\u202eb", "a\u00a0b", "a\u200bb", "",
                    "a\ud800", "a\ue000b", "tab\there", "line\nbreak"):
            with self.subTest(bad=bad):
                self.assertFalse(is_display_text(bad, 80))

    def test_display_text_is_identical_on_every_python_version(self) -> None:
        # U+1FAE8 (Unicode 15) is printable on 3.13 (UCD 15.1) but unassigned on 3.11 (UCD 14.0);
        # the frozen Unicode 3.2 table refuses it on both, so a config valid in CI is valid locally.
        self.assertFalse(is_display_text("Shaking \U0001FAE8 face", 80))
        self.assertFalse(is_display_text("Rupee \u20b9", 80))  # assigned in Unicode 6.0
        self.assertTrue(is_display_text("Euro \u20ac", 80))  # assigned in Unicode 2.1

    def test_non_string_keys_are_rejected_cleanly(self) -> None:
        self.assertRejects(Obj({"a": Int(0, 1)}), {"a": 1, 2: 3}, "has a non-string key")
        self.assertRejects(Obj({"a": Int(0, 1)}), {"a": 1, True: 3}, "has a non-string key")
        self.assertRejects(Map(re.compile(r"^[a-z]+$"), Int(0, 9), max_items=5), {1: 1}, "has an invalid key")

    def test_objects_reject_unknown_and_missing_keys(self) -> None:
        validator = Obj({"a": Int(0, 1)}, {"b": Bool()})
        self.assertEqual(validator({"a": 1}, "$"), {"a": 1})
        self.assertRejects(validator, {"a": 1, "c": 2}, "has unknown keys")
        self.assertRejects(validator, {"b": True}, "is missing required keys ['a']")
        self.assertRejects(validator, [], "must be an object")
        with self.assertRaises(ValueError):
            Obj({"a": Int(0, 1)}, {"a": Int(0, 1)})

    def test_lists(self) -> None:
        self.assertRejects(List(Int(0, 9), max_items=2), [1, 2, 3], "between 0 and 2 items")
        self.assertRejects(List(Int(0, 9), max_items=5, unique=True), [1, 1], "duplicates an earlier item")
        self.assertRejects(List(Int(0, 9), max_items=5, sorted_values=True), [2, 1], "strictly ascending")
        self.assertRejects(List(Obj({"id": Int(0, 9)}), max_items=5, unique_by=lambda item: item["id"]),
                           [{"id": 1}, {"id": 1}], "duplicates an earlier item")
        with self.assertRaises(DocumentError) as caught:
            List(Int(0, 9), max_items=5)([1, "x"], "$.list")
        self.assertEqual(caught.exception.path, "$.list[1]")

    def test_maps_const_nullable_oneof(self) -> None:
        validator = Map(re.compile(r"^[a-z]+$"), Int(0, 9), max_items=2)
        self.assertRejects(validator, {"A": 1}, "has an invalid key")
        self.assertRejects(validator, {"a": 1, "b": 2, "c": 3}, "between 0 and 2 entries")
        self.assertRejects(Const(1), True, "must be 1")
        self.assertIsNone(Nullable(Int(0, 1))(None, "$"))
        either = OneOf(Const("a"), Int(0, 1))
        self.assertEqual(either(1, "$"), 1)
        self.assertRejects(either, "b", "must be an integer")

    def test_size_and_region(self) -> None:
        self.assertEqual(Size(1, 10)([2, 3], "$"), [2, 3])
        self.assertRejects(Size(1, 10), [2], "[width, height] pair")
        self.assertRejects(Region(), [0.5, 0.0, 0.5, 1.0], "left < right")
        self.assertRejects(Region(), [0.0, 0.0, 1.0, 1.5], "must be between 0.0 and 1.0")


if __name__ == "__main__":
    unittest.main()
