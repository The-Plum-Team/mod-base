"""Pins ``requirements/pillow.txt`` to the Pillow==12.3.0 stanza shared by both mods (SPEC §2.4, V10)."""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "requirements" / "pillow.txt"
#: SHA-256 of the stanza ("Pillow==12.3.0 \\" line through the last hash line, LF, trailing newline)
#: exactly as it appears in Quick Skin and Block Pops ``e2e/requirements.txt``.
STANZA_SHA256 = "0e1bfea9c69d8ff40f04784e40d22d91c2d86f1670394a896258df74253b60a5"
PAGES_HASH = "0847a763afefb695bc912d7c131e7e0632d4edc1d8698f58ddabec8e46b8b6d3"


def _text() -> str:
    # A local checkout may convert line endings; the lock's content is compared with LF endings.
    return LOCK.read_bytes().decode("ascii").replace("\r\n", "\n")


class PillowLockTest(unittest.TestCase):
    def test_one_origin_comment_then_the_exact_stanza(self) -> None:
        lines = _text().split("\n")
        self.assertTrue(lines[0].startswith("# "))
        self.assertIn("e2e/requirements.txt", lines[0])
        stanza = "\n".join(lines[1:])
        self.assertEqual(hashlib.sha256(stanza.encode("ascii")).hexdigest(), STANZA_SHA256)

    def test_version_and_hash_count(self) -> None:
        lines = [line for line in _text().split("\n")[1:] if line]
        self.assertEqual(lines[0], "Pillow==12.3.0 \\")
        hashes = lines[1:]
        self.assertEqual(len(hashes), 86)
        pattern = re.compile(r"^    --hash=sha256:[0-9a-f]{64}( \\)?$")
        for line in hashes:
            self.assertRegex(line, pattern)
        self.assertFalse(hashes[-1].endswith("\\"))
        self.assertTrue(all(line.endswith(" \\") for line in hashes[:-1]))
        digests = [line.split(":")[1].split()[0] for line in hashes]
        self.assertEqual(len(set(digests)), 86)

    def test_contains_the_pages_linux_wheel(self) -> None:
        self.assertIn(f"--hash=sha256:{PAGES_HASH}", _text())

    def test_file_is_plain_ascii_without_other_requirements(self) -> None:
        text = _text()
        self.assertTrue(text.endswith("\n"))
        requirements = [line for line in text.split("\n") if "==" in line and not line.startswith("#")]
        self.assertEqual(requirements, ["Pillow==12.3.0 \\"])


if __name__ == "__main__":
    unittest.main()
