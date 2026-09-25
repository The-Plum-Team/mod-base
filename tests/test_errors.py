from __future__ import annotations

import io
import unittest

from mod_base import errors
from mod_base.errors import ControllerSkew, MbError, Superseded, Unavailable, run_main, single_line


def raising(error: BaseException):
    def entry() -> int:
        raise error

    return entry


class ExitCodeTest(unittest.TestCase):
    def run_entry(self, entry) -> tuple[int, str]:
        stream = io.StringIO()
        return run_main(entry, stderr=stream), stream.getvalue()

    def test_hierarchy_and_codes(self) -> None:
        self.assertEqual(MbError("x").exit_code, 2)
        self.assertEqual(Unavailable("x").exit_code, 3)
        self.assertEqual(Superseded("x").exit_code, 3)
        self.assertEqual(Superseded("x").reason, "superseded")
        self.assertEqual(Unavailable("x").reason, "unavailable")
        self.assertEqual(ControllerSkew("x").exit_code, 78)
        self.assertIsInstance(Superseded("x"), Unavailable)
        self.assertEqual(MbError("x", reason="custom").reason, "custom")

    def test_run_main_maps_every_outcome(self) -> None:
        cases = [
            (lambda: 0, 0, ""),
            (lambda: None, 0, ""),
            (lambda: 3, 3, ""),
            (raising(MbError("bad input")), 2, "mod_base: rejected: bad input\n"),
            (raising(Unavailable("no evidence")), 3, "mod_base: unavailable: no evidence\n"),
            (raising(Superseded("drift")), 3, "mod_base: superseded: drift\n"),
            (raising(ControllerSkew("skew")), 78, "mod_base: controller-skew: skew\n"),
            (raising(NotImplementedError("owned by MB5")), 2, "mod_base: not implemented: owned by MB5\n"),
            (raising(KeyError("k")), 1, "mod_base: internal error: KeyError: 'k'\n"),
            (raising(KeyboardInterrupt()), 130, "mod_base: interrupted\n"),
            (raising(SystemExit(0)), 0, ""),
            (raising(SystemExit(4)), 4, ""),
            (raising(SystemExit("text")), 2, "mod_base: rejected: text\n"),
            (lambda: True, 1, "mod_base: internal error: entry point returned bool\n"),
            (lambda: "0", 1, "mod_base: internal error: entry point returned str\n"),
        ]
        for entry, code, message in cases:
            with self.subTest(message=message or code):
                self.assertEqual(self.run_entry(entry), (code, message))

    def test_messages_are_one_bounded_line(self) -> None:
        code, message = self.run_entry(raising(MbError("line one\nline two\r\x1b[31m::error::forged " + "x" * 5000)))
        self.assertEqual(code, 2)
        self.assertEqual(message.count("\n"), 1)
        self.assertNotIn("\x1b", message)
        self.assertLessEqual(len(message), len("mod_base: ") + errors.MAX_MESSAGE_CHARS + 1)

    def test_single_line(self) -> None:
        self.assertEqual(single_line("a\nb\tc"), "a b c")
        self.assertEqual(single_line(""), "(no message)")
        self.assertEqual(single_line("x" * 20, limit=10), "xxxxxxx...")
        self.assertEqual(single_line("a\u0085b\x7fc"), "a b c")


if __name__ == "__main__":
    unittest.main()
