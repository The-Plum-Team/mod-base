"""``mod_base.imaging.webp.derive_webp``: deterministic, metadata-free, correctly sized derivatives (MB2).

Byte parity with both mods' encoders is ``test_imaging_parity``; here the acceptance bullet
"deterministic WebP bytes across two runs" is pinned in-process and across two fresh interpreters,
together with the size rule, the encoder parameters and the refused sources.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mod_base.imaging import metrics
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_webp
from mod_base.imaging.png import canonical_png, pattern_png
from mod_base.imaging.webp import derive_webp
from mod_base.model import limits
from mod_base.model.documents import thumbnail_size

SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
INPUTS = Path(__file__).resolve().parent / "fixtures" / "imaging" / "inputs"
#: (box, quality, method): Quick Skin and Block Pops frames, and the Quick Skin family images.
POLICIES = {"quick-skin": ((1600, 900), 82, 6), "block-pops": ((1280, 720), 82, 6), "family": ((1280, 720), 80, 6)}
#: The derivatives a fresh interpreter prints: input name -> policy name.
CROSS_PROCESS = (("pattern-s0.png", "quick-skin"), ("scene.png", "block-pops"),
                 ("pattern-1920x1080.png", "family"), ("metadata.png", "quick-skin"))
CHILD = """
import hashlib, json, sys
from pathlib import Path
from mod_base.imaging.png import canonical_png
from mod_base.imaging.webp import derive_webp
inputs, cases = Path(sys.argv[1]), json.loads(sys.argv[2])
result = []
for name, box, quality, method in cases:
    source = (inputs / name).read_bytes()
    result.append([hashlib.sha256(derive_webp(source, box=box, quality=quality, method=method)).hexdigest(),
                   hashlib.sha256(canonical_png(source)).hexdigest()])
print(json.dumps(result))
"""


def derive(source: bytes | Path, policy: str) -> bytes:
    box, quality, method = POLICIES[policy]
    return derive_webp(source, box=box, quality=quality, method=method)


def riff_chunks(payload: bytes) -> list[bytes]:
    """The top-level chunk types of a RIFF/WebP file, after checking the RIFF framing."""

    assert payload[:4] == b"RIFF" and payload[8:12] == b"WEBP"
    assert struct.unpack("<I", payload[4:8])[0] == len(payload) - 8
    types, offset = [], 12
    while offset < len(payload):
        size = struct.unpack("<I", payload[offset + 4:offset + 8])[0]
        types.append(payload[offset:offset + 4])
        offset += 8 + size + (size & 1)
    assert offset == len(payload)
    return types


def decoded_size(payload: bytes) -> tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        return image.size


class DeterminismTest(unittest.TestCase):
    def test_identical_inputs_give_identical_bytes(self) -> None:
        for name in ("pattern-s0.png", "scene.png", "rgba-alpha.png"):
            source = (INPUTS / name).read_bytes()
            for policy in POLICIES:
                with self.subTest(input=name, policy=policy):
                    first = derive(source, policy)
                    self.assertEqual(derive(source, policy), first)
                    self.assertEqual(derive(INPUTS / name, policy), first)

    def test_identical_pixels_give_identical_bytes(self) -> None:
        # The derivative is a function of the decoded pixels: metadata, alpha 255 and trailing bytes
        # after IEND change the file but not the WebP.
        reference = derive((INPUTS / "pattern-s0.png").read_bytes(), "quick-skin")
        for name in ("metadata.png", "pattern-s0-rgba.png", "trailing-data.png"):
            with self.subTest(input=name):
                self.assertEqual(derive((INPUTS / name).read_bytes(), "quick-skin"), reference)
        canonical = canonical_png((INPUTS / "pattern-s0.png").read_bytes())
        self.assertEqual(derive(canonical, "quick-skin"), reference)

    def test_two_fresh_interpreters_derive_the_same_bytes(self) -> None:
        cases = [[name, *POLICIES[policy]] for name, policy in CROSS_PROCESS]
        expected = [[hashlib.sha256(derive((INPUTS / name).read_bytes(), policy)).hexdigest(),
                     hashlib.sha256(canonical_png((INPUTS / name).read_bytes())).hexdigest()]
                    for name, policy in CROSS_PROCESS]
        outputs = []
        with tempfile.TemporaryDirectory() as directory:
            for hash_seed in ("1", "2"):
                environment = {"PATH": os.defpath, "PYTHONPATH": str(SOURCE_ROOT), "PYTHONHASHSEED": hash_seed,
                               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}
                completed = subprocess.run(
                    [sys.executable, "-B", "-P", "-c", CHILD, str(INPUTS), json.dumps(cases)],
                    capture_output=True, cwd=directory, env=environment, check=False, timeout=300,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
                outputs.append(json.loads(completed.stdout))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], expected)


class DerivativeTest(unittest.TestCase):
    def test_the_size_is_pillow_thumbnail_size(self) -> None:
        box = (1600, 900)
        for size in ((1600, 900), (1920, 1080), (3200, 1800), (1601, 900), (1600, 901), (1000, 1000), (901, 1601),
                     (641, 359), (300, 200), (8, 4), (16384, 16), (16, 16384)):
            with self.subTest(size=size):
                derived = derive_webp(pattern_png(*size), box=box, quality=82, method=4)
                self.assertEqual(decoded_size(derived), thumbnail_size(size, box))
        self.assertEqual(thumbnail_size((1920, 1080), (1280, 720)), (1280, 720))

    def test_a_derivative_is_an_inspectable_webp_of_the_thumbnail_size(self) -> None:
        for name, policy, size in (("pattern-s0.png", "quick-skin", (1600, 900)),
                                   ("pattern-s0.png", "block-pops", (1280, 720)),
                                   ("pattern-1920x1080.png", "quick-skin", (1600, 900)),
                                   ("pattern-1920x1080.png", "family", (1280, 720))):
            with self.subTest(input=name, policy=policy):
                derived = derive((INPUTS / name).read_bytes(), policy)
                self.assertLessEqual(len(derived), limits.MAX_DERIVATIVE_BYTES)
                self.assertEqual(tuple(inspect_webp(derived, SizePolicy.exact(*size))[k] for k in ("width", "height")),
                                 size)

    def test_a_derivative_carries_no_source_metadata(self) -> None:
        from PIL import Image

        with Image.open(INPUTS / "pattern-s0.png") as opened:
            base = opened.convert("RGB")
        exif = Image.Exif()
        exif[0x0131] = "mod-base golden"  # Software
        stream = io.BytesIO()
        base.save(stream, format="PNG", exif=exif, icc_profile=b"not-a-real-profile", dpi=(144, 144))
        for source in (stream.getvalue(), (INPUTS / "metadata.png").read_bytes()):
            for policy in POLICIES:
                with self.subTest(policy=policy):
                    self.assertEqual(riff_chunks(derive(source, policy)), [b"VP8 "])

    def test_every_parameter_reaches_the_encoder(self) -> None:
        source = (INPUTS / "scene.png").read_bytes()
        reference = derive_webp(source, box=(1280, 720), quality=82, method=6)
        self.assertNotEqual(derive_webp(source, box=(1280, 720), quality=60, method=6), reference)
        self.assertNotEqual(derive_webp(source, box=(1280, 720), quality=82, method=0), reference)
        self.assertEqual(decoded_size(derive_webp(source, box=[800, 800], quality=82, method=6)), (800, 450))
        self.assertEqual(derive_webp(source, box=[1280, 720], quality=82, method=6), reference)


class RejectionTest(unittest.TestCase):
    def test_the_arguments_are_validated(self) -> None:
        source = pattern_png(64, 36)
        for box in ((0, 900), (1600,), (1600, 900, 1), "16", b"16", (1600.0, 900), (True, 900),
                    (limits.MAX_IMAGE_DIMENSION + 1, 900), None, {1600: 900, 900: 1600}):
            with self.subTest(box=box), self.assertRaisesRegex(ImageError, "derivative box"):
                derive_webp(source, box=box, quality=82, method=6)  # type: ignore[arg-type]
        for quality in (0, 101, 82.0, True, "82", None):
            with self.subTest(quality=quality), self.assertRaisesRegex(ImageError, "webp quality"):
                derive_webp(source, box=(64, 36), quality=quality, method=6)  # type: ignore[arg-type]
        for method in (-1, 7, 6.0, False, "6", None):
            with self.subTest(method=method), self.assertRaisesRegex(ImageError, "webp method"):
                derive_webp(source, box=(64, 36), quality=82, method=method)  # type: ignore[arg-type]

    def test_only_bounded_static_pngs_are_derived(self) -> None:
        for name, message in (("apng.png", "one static frame"), ("truncated.png", "cannot be decoded"),
                              ("idat-flipped.png", "cannot be decoded"), ("qs-pattern-s0.webp", "is not a PNG image"),
                              ("pattern-s0.jpg.png", "is not a PNG image"), ("scene.gif.png", "is not a PNG image"),
                              ("text.png", "is not a PNG image"), ("bomb-5000x5000.png", "dimensions"),
                              ("bomb-header.png", "dimensions")):
            with self.subTest(input=name), self.assertRaisesRegex(ImageError, message):
                derive((INPUTS / name).read_bytes(), "quick-skin")
        with self.assertRaisesRegex(ImageError, "bytes"):
            derive(b"", "quick-skin")
        with self.assertRaisesRegex(ImageError, "path or bytes"):
            derive(str(INPUTS / "pattern-s0.png"), "quick-skin")  # type: ignore[arg-type]

    def test_an_untrusted_encoder_result_is_refused(self) -> None:
        # Each check after encoding, driven by a simulated faulty encoder.
        from PIL import Image

        source = pattern_png(320, 180)
        with mock.patch.object(Image.Image, "thumbnail", new=lambda self, size, resample=None: None):
            with self.assertRaisesRegex(ImageError, r"is \(320, 180\), expected \(160, 90\)"):
                derive_webp(source, box=(160, 90), quality=82, method=0)
        with mock.patch.dict(metrics._MAX_BYTES, {"WEBP": 64}):
            with self.assertRaisesRegex(ImageError, "the WebP derivative of PNG image exceeds its byte bound"):
                derive_webp(source, box=(160, 90), quality=82, method=0)
        with mock.patch.object(Image.Image, "save", side_effect=OSError("encoder unavailable")):
            with self.assertRaisesRegex(ImageError, "cannot create the WebP derivative of PNG image: encoder unavail"):
                derive_webp(source, box=(160, 90), quality=82, method=0)
        self.assertEqual(decoded_size(derive_webp(source, box=(160, 90), quality=82, method=0)), (160, 90))

    def test_only_regular_files_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "frame.png").write_bytes(pattern_png(64, 36))
            (root / "link.png").symlink_to(root / "frame.png")
            self.assertEqual(derive(root / "frame.png", "family"), derive(pattern_png(64, 36), "family"))
            with self.assertRaisesRegex(ImageError, "not a regular file"):
                derive(root / "link.png", "family")


if __name__ == "__main__":
    unittest.main()
