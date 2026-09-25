"""The kit's imaging equals both mods' functions on the committed golden vectors (SPEC §10.1 MB2).

``tests/fixtures/imaging/golden.json`` was produced once by ``generate_golden.py`` running the REAL
Quick Skin ``inspect_screenshot``/``compare_screenshots``/``canonicalize_png_snapshot``/``_encode_webp``
and Block Pops ``_screenshot_metrics``/``compare_screenshots``/``canonicalize_png``/
``_encode_webp_uncached`` over ``inputs/``. Quick Skin's size gate is ``SizePolicy.minimum(640, 360)``
and Block Pops' is ``SizePolicy.exact(1600, 900)``; the derivative boxes are 1600x900 and 1280x720.

Every accepted input must give the identical dict (metrics, comparison) or identical bytes
(canonical PNG, WebP derivative); every rejection must be a kit :class:`ImageError` of the same
class (blank with the same printed statistics, size, unchanged, size mismatch, empty region, or an
unusable image). ``DEVIATIONS`` lists the few inputs the kit refuses on purpose although a mod
accepts them; each is a stricter check, never a different measurement.

Encoded bytes (the canonical PNG and WebP digests and lengths) depend on the zlib and libwebp
builds inside the Pillow wheel, so they are compared only on the platform that produced the golden
file (SPEC §7.4 "compared within one platform only"); sizes, pixel digests, metrics and
comparisons are compared everywhere.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import struct
import unittest
from pathlib import Path
from typing import Any

from mod_base.imaging.compare import compare
from mod_base.imaging.metrics import ImageError, SizePolicy, inspect_png, inspect_webp
from mod_base.imaging.png import canonical_png
from mod_base.imaging.webp import derive_webp

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "imaging"
INPUTS = FIXTURES / "inputs"
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
MODS = ("quick-skin", "block-pops")
POLICIES = {"quick-skin": SizePolicy.minimum(640, 360), "block-pops": SizePolicy.exact(1600, 900)}
WEBP_BOXES = {"quick-skin": (1600, 900), "block-pops": (1280, 720)}
WEBP_QUALITY, WEBP_METHOD = 82, 6

#: (operation, case identity, mod or None for both) -> why the kit refuses what the mod accepts.
DEVIATIONS: dict[tuple[str, tuple[Any, ...], str | None], str] = {
    ("inspect", ("apng.png", "PNG"), None): "static",  # the mods measure frame 0 of an APNG
    ("inspect", ("animated.webp", "WEBP"), None): "static",  # ... and of an animated WebP
    ("compare", ("pattern-s0.png", "qs-pattern-s0.webp"), None): "mixed",  # a PNG against a WebP
    ("webp", ("apng.png",), None): "static",
    ("webp", ("pattern-s0.jpg.png",), "block-pops"): "format",  # BP's encoder opens any format
    ("webp", ("scene.gif.png",), "block-pops"): "format",
    # Pillow only warns between 20 M and 40 M pixels, and Quick Skin's encoder does not escalate it.
    ("webp", ("bomb-5000x5000.png",), "quick-skin"): "bound",
}
#: The exact refusal each deviation must produce (not merely some ImageError).
DEVIATION_PHRASES = {
    "static": "must be one static frame",
    "mixed": "cannot compare a PNG image with a WEBP image",
    "format": "is not a PNG image",
    "bound": "PNG image dimensions violate the bound of 20000000 pixels",
}
#: Block Pops bounds its derivative encoder by its own 1600x900 source size; the kit bounds the
#: source through the inspection's SizePolicy instead, so these derive in the kit only.
BLOCK_POPS_SOURCE_BOUND = {"pattern-1920x1080.png", "pattern-3200x1800.png"}

#: (accepted, rejected) inputs straddling colour count, entropy, deviation, dark and light.
BLANK_THRESHOLD_PAIRS = (
    ("colours-4.png", "colours-3.png"),
    ("entropy-above.png", "entropy-below.png"),
    ("stddev-above.png", "stddev-below.png"),
    ("dark-limit.png", "dark-over.png"),
    ("light-limit.png", "light-over.png"),
)

MOD_CLASSES = (
    ("effectively blank", "blank"),
    ("did not change enough", "unchanged"),
    ("changed dimensions unexpectedly", "size-mismatch"),
    ("is empty at", "empty-region"),
    ("dimensions", "size"),
    ("smaller than", "size"),
    ("exceeds limit of", "size"),
)
KIT_CLASSES = MOD_CLASSES[:5]
BLANK_DETAIL = re.compile(r"\((entropy=[^)]*)\)")
#: Output fields that are the encoder's bytes rather than a function of the decoded pixels.
ENCODED_KEYS = frozenset({"sha256", "bytes"})
CANONICAL_KEYS = ("sha256", "bytes", "size", "pixel_sha256", "file_sha256")
WEBP_KEYS = ("sha256", "bytes", "size")


def current_platform() -> dict[str, str]:
    from PIL import __version__ as pillow, features

    return {"system": platform.system(), "machine": platform.machine(), "pillow": pillow,
            "libwebp": features.version("webp"), "zlib": features.version("zlib")}


GOLDEN_PLATFORM = {key: GOLDEN["provenance"]["platform"][key] for key in current_platform()}
SAME_ENCODERS = current_platform() == GOLDEN_PLATFORM


def data(name: str) -> bytes:
    return (INPUTS / name).read_bytes()


def mod_class(outcome: dict[str, Any]) -> str:
    if outcome["error"] == "DecompressionBombError":
        return "size"
    return next((name for phrase, name in MOD_CLASSES if phrase in outcome["message"]), "unusable")


def kit_class(error: ImageError) -> str:
    return next((name for phrase, name in KIT_CLASSES if phrase in str(error)), "unusable")


def decoded(payload: bytes) -> tuple[list[int], bytes]:
    """The size and RGB pixels of an image the kit produced."""

    from PIL import Image

    with Image.open(io.BytesIO(payload)) as image:
        return list(image.size), image.convert("RGB").tobytes()


def block_pops_canonical_policy(payload: bytes) -> SizePolicy | None:
    """Block Pops canonicalizes at an expected size; the generator passed each PNG's own IHDR size
    (1600x900 for a non-PNG). A size no policy can hold (a bomb) leaves only the kit's bounds."""

    size = struct.unpack(">II", payload[16:24]) if payload[12:16] == b"IHDR" else (1600, 900)
    try:
        return SizePolicy.exact(*size)
    except ImageError:
        return None


def comparable(result: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """``result`` restricted to ``keys``, without the encoded-byte keys off the golden platform."""

    return {key: result[key] for key in keys if SAME_ENCODERS or key not in ENCODED_KEYS}


class GoldenFixtureTest(unittest.TestCase):
    def test_inputs_are_the_recorded_bytes(self) -> None:
        names = sorted(path.name for path in INPUTS.iterdir())
        self.assertEqual(names, sorted(GOLDEN["inputs"]))
        for name in names:
            with self.subTest(input=name):
                payload = data(name)
                self.assertEqual(len(payload), GOLDEN["inputs"][name]["bytes"])
                self.assertEqual(hashlib.sha256(payload).hexdigest(), GOLDEN["inputs"][name]["sha256"])

    def test_both_mods_share_one_compare_screenshots(self) -> None:
        sources = GOLDEN["provenance"]["sources"]
        self.assertEqual(sources["quick-skin"]["compare_screenshots_ast_sha256"],
                         sources["block-pops"]["compare_screenshots_ast_sha256"])
        for kind in ("inspect", "compare", "canonical", "webp"):
            for mod in MODS:
                self.assertEqual(len(GOLDEN["outcomes"][mod][kind]), len(GOLDEN["cases"][kind]))

    def test_every_deviation_is_a_mod_acceptance(self) -> None:
        keys = {("inspect", (case["input"], case["format"])) for case in GOLDEN["cases"]["inspect"]}
        keys |= {("compare", (case["first"], case["second"])) for case in GOLDEN["cases"]["compare"]}
        keys |= {("webp", (case["input"],)) for case in GOLDEN["cases"]["webp"]}
        for operation, identity, _mod in DEVIATIONS:
            self.assertIn((operation, identity), keys)

    def test_the_golden_covers_every_outcome_class(self) -> None:
        seen = {mod_class(outcome) for mod in MODS for kind in ("inspect", "compare")
                for outcome in GOLDEN["outcomes"][mod][kind] if "result" not in outcome}
        self.assertEqual(seen, {"blank", "unchanged", "size-mismatch", "empty-region", "size", "unusable"})

    def test_the_golden_brackets_every_blank_threshold(self) -> None:
        # generate_golden.boundary_inputs: each pair sits on either side of one blank-gate check
        # (test_imaging_metrics pins the values), so a drifted threshold changes an outcome here.
        outcomes = {(mod, case["input"]): GOLDEN["outcomes"][mod]["inspect"][index]
                    for index, case in enumerate(GOLDEN["cases"]["inspect"]) if case["format"] == "PNG"
                    for mod in MODS}
        for accepted, rejected in BLANK_THRESHOLD_PAIRS:
            for mod in MODS:
                with self.subTest(pair=(accepted, rejected), mod=mod):
                    self.assertIn("result", outcomes[mod, accepted])
                    self.assertEqual(mod_class(outcomes[mod, rejected]), "blank")
        for mod in MODS:
            self.assertIn("result", outcomes[mod, "luma-edges.png"])

    @unittest.skipUnless(SAME_ENCODERS, "encoded bytes are compared only on the golden platform")
    def test_encoded_bytes_are_compared_on_this_platform(self) -> None:
        self.assertEqual(current_platform(), GOLDEN_PLATFORM)


class ParityTest(unittest.TestCase):
    maxDiff = None

    def assert_same_rejection(self, outcome: dict[str, Any], error: ImageError) -> None:
        self.assertEqual(kit_class(error), mod_class(outcome), f"kit: {error}; mod: {outcome['message']}")
        if mod_class(outcome) == "blank":
            self.assertEqual(BLANK_DETAIL.search(str(error)).group(1),
                             BLANK_DETAIL.search(outcome["message"]).group(1))

    def check(self, operation: str, identity: tuple[Any, ...], mod: str, outcome: dict[str, Any],
              run: Any, keys: tuple[str, ...] | None = None) -> None:
        deviation = DEVIATIONS.get((operation, identity, None)) or DEVIATIONS.get((operation, identity, mod))
        if deviation is not None:
            self.assertIn("result", outcome)
            with self.assertRaises(ImageError) as caught:
                run()
            self.assertIn(DEVIATION_PHRASES[deviation], str(caught.exception))
            return
        if "result" not in outcome:
            with self.assertRaises(ImageError) as caught:
                run()
            self.assert_same_rejection(outcome, caught.exception)
            return
        if keys is None:
            self.assertEqual(run(), outcome["result"])
        else:
            self.assertEqual(comparable(run(), keys), comparable(outcome["result"], keys))

    def test_inspection(self) -> None:
        for index, case in enumerate(GOLDEN["cases"]["inspect"]):
            inspect = inspect_png if case["format"] == "PNG" else inspect_webp
            for mod in MODS:
                outcome = GOLDEN["outcomes"][mod]["inspect"][index]
                with self.subTest(case=case, mod=mod):
                    self.check("inspect", (case["input"], case["format"]), mod, outcome,
                               lambda: inspect(data(case["input"]), POLICIES[mod]))

    def test_inspection_reads_paths_like_bytes(self) -> None:
        for name in ("pattern-s0.png", "scene.png", "qs-pattern-s0.webp"):
            inspect = inspect_webp if name.endswith(".webp") else inspect_png
            with self.subTest(input=name):
                self.assertEqual(inspect(INPUTS / name, POLICIES["quick-skin"]),
                                 inspect(data(name), POLICIES["quick-skin"]))

    def test_comparison(self) -> None:
        for index, case in enumerate(GOLDEN["cases"]["compare"]):
            for mod in MODS:
                outcome = GOLDEN["outcomes"][mod]["compare"][index]
                with self.subTest(case=case, mod=mod):
                    self.check("compare", (case["first"], case["second"]), mod, outcome,
                               lambda: compare(data(case["first"]), data(case["second"]),
                                               minimum_changed_fraction=case["minimum_changed_fraction"],
                                               region=case["region"]))

    def test_canonical_png(self) -> None:
        for index, case in enumerate(GOLDEN["cases"]["canonical"]):
            payload = data(case["input"])
            policies = {"quick-skin": POLICIES["quick-skin"], "block-pops": block_pops_canonical_policy(payload)}
            for mod in MODS:
                outcome = GOLDEN["outcomes"][mod]["canonical"][index]

                def run(policy: SizePolicy | None = policies[mod]) -> dict[str, Any]:
                    canonical = canonical_png(payload, policy=policy)
                    size, pixels = decoded(canonical)
                    return {"sha256": hashlib.sha256(canonical).hexdigest(), "bytes": len(canonical), "size": size,
                            "pixel_sha256": hashlib.sha256(pixels).hexdigest(),
                            "file_sha256": hashlib.sha256(payload).hexdigest()}

                with self.subTest(case=case, mod=mod):
                    self.check("canonical", (case["input"],), mod, outcome, run, CANONICAL_KEYS)

    def test_webp_derivatives(self) -> None:
        for index, case in enumerate(GOLDEN["cases"]["webp"]):
            payload = data(case["input"])
            for mod in MODS:
                outcome = GOLDEN["outcomes"][mod]["webp"][index]

                def run(box: tuple[int, int] = WEBP_BOXES[mod]) -> dict[str, Any]:
                    derived = derive_webp(payload, box=box, quality=WEBP_QUALITY, method=WEBP_METHOD)
                    return {"sha256": hashlib.sha256(derived).hexdigest(), "bytes": len(derived),
                            "size": decoded(derived)[0]}

                with self.subTest(case=case, mod=mod):
                    if mod == "block-pops" and case["input"] in BLOCK_POPS_SOURCE_BOUND:
                        self.assertIn("exceeds limit of", outcome["message"])
                        self.assertEqual(run()["size"], [1280, 720])
                        continue
                    self.check("webp", (case["input"],), mod, outcome, run, WEBP_KEYS)


if __name__ == "__main__":
    unittest.main()
