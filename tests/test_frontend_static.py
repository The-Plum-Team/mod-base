"""Static safety and accessibility checks of the kit front end (SPEC §6.3, §6.4, §5.10 ``Front end``).

Ports the DOM/CSP parts of Quick Skin ``test_pages_site.py`` (no ``innerHTML``, local assets only,
the capture dialog without inline HTML) and adds the v1 rules: the exact meta CSP and referrer on
both pages, no inline script, style or event handler, no remote script or stylesheet, ``node
--check`` for both scripts, colour-literal-free ``styles.css`` whose every custom property is
defined, and the accessibility contract (skip links, labelled groups, tabs, live regions, dialog).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

from mod_base.pages.build import SITE_ALLOWLIST
from mod_base.pages.templating import theme_css

SITE = Path(__file__).resolve().parents[1] / "site"
PAGES = ("index.html", "e2e/index.html")
SCRIPTS = ("assets/site.js", "assets/gallery.js")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
THEME = {"bg": "#0d120f", "surface": "#141b16", "surface_raised": "#1a241d", "surface_soft": "#202c24",
         "text": "#f3f7f3", "muted": "#a9b7ac", "line": "#304137", "accent": "#77e39b", "accent_strong": "#40c973",
         "highlight": "#e6c875", "danger": "#ff9b91", "image_well": "#080b09"}
#: Quick Skin's 19 hand-written colour literals (qs-site §3), none of which may survive.
QS_LITERALS = ("rgb(64 201 115", "rgb(119 227 155", "#07150c", "rgb(0 0 0", "rgb(20 27 22", "#080b09",
               "rgb(4 8 6", "#0b100d")
FORBIDDEN_SCRIPT = {
    "innerHTML": r"\binnerHTML\b",
    "outerHTML": r"\bouterHTML\b",
    "insertAdjacentHTML": r"\binsertAdjacentHTML\b",
    "document.write": r"\bdocument\s*\.\s*write(?:ln)?\b",
    "eval": r"\beval\s*\(",
    "new Function": r"\bnew\s+Function\b",
    "string timer": r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]",
    "srcdoc": r"\bsrcdoc\b",
    "event property": r"\.on[a-z]+\s*=",
    "javascript URL": r"javascript:",
    "createContextualFragment": r"createContextualFragment",
    "DOMParser": r"\bDOMParser\b",
}


def read(relative: str) -> str:
    return (SITE / relative).read_text(encoding="utf-8")


class Page(HTMLParser):
    """Every start tag with its attributes, plus the text inside ``<script>``/``<style>``."""

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.raw: list[tuple[str, str]] = []
        self._raw_tag: str | None = None
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag in ("script", "style"):
            self._raw_tag = tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag == self._raw_tag:
            self._raw_tag = None

    def handle_data(self, data: str) -> None:
        if self._raw_tag is not None and data.strip():
            self.raw.append((self._raw_tag, data))

    def find(self, tag: str, **attributes: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags
                if name == tag and all(attrs.get(key.replace("_", "-")) == value for key, value in attributes.items())]


class InventoryTest(unittest.TestCase):
    def test_the_kit_site_holds_exactly_the_allowlist(self) -> None:
        present = sorted(path.relative_to(SITE).as_posix() for path in SITE.rglob("*") if path.is_file())
        self.assertEqual(present, sorted(SITE_ALLOWLIST))
        for path in SITE.rglob("*"):
            self.assertFalse(path.is_symlink(), path)


class ScriptSafetyTest(unittest.TestCase):
    def test_scripts_never_build_markup_or_evaluate_code(self) -> None:
        for relative in SCRIPTS:
            source = read(relative)
            for label, pattern in FORBIDDEN_SCRIPT.items():
                with self.subTest(script=relative, rule=label):
                    self.assertIsNone(re.search(pattern, source), f"{relative} uses {label}")
            self.assertTrue(source.startswith('"use strict";'))
            self.assertIn("textContent", source)

    def test_node_accepts_both_scripts(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the front-end checks (kit CI 'Front end')")
        for relative in SCRIPTS:
            completed = subprocess.run([node, "--check", str(SITE / relative)], capture_output=True, timeout=60,
                                       check=False)
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))

    def test_scripts_gate_exactly_their_v1_documents(self) -> None:
        site, gallery = read("assets/site.js"), read("assets/gallery.js")
        self.assertIn('data.kind === "mod-base.site"', site)
        self.assertIn('data.kind === "mod-base.gallery"', gallery)
        for source in (site, gallery):
            self.assertIn("data.schema_version === 1", source)
        for fragment in ('fetch("site-data.json", { credentials: "same-origin" })',):
            self.assertIn(fragment, site)
        self.assertIn('fetch("gallery-data.json", { credentials: "same-origin" })', gallery)
        for gate in ("Array.isArray(data.families)", "family.available === \"boolean\"",
                     "Array.isArray(family.not_applicable)", "data.frames.length > 0"):
            self.assertIn(gate.replace('\\"', '"'), gallery)

    def test_links_and_images_pass_their_guards(self) -> None:
        guarded = (("assets/site.js", {"safeHref"}), ("assets/gallery.js", {"sameOriginPath", "httpsLink"}))
        for relative, guards in guarded:
            source = read(relative)
            # Every assignment to a link or image URL, whatever its right-hand side.
            assignments = re.findall(r"\.(?:href|src)\s*=(?!=)\s*([^;\n]*)", source)
            self.assertTrue(assignments, relative)
            for value in assignments:
                with self.subTest(script=relative, value=value):
                    guard = re.fullmatch(r"([A-Za-z]+)\(.*\)", value.strip())
                    self.assertIsNotNone(guard, f"{relative} assigns an unguarded URL: {value}")
                    self.assertIn(guard.group(1), guards)
            self.assertIsNone(re.search(r"setAttribute\(\s*['\"`](?:href|src|srcset|action|formaction|xlink:href)\b",
                                        source), f"{relative} sets a URL attribute directly")
            self.assertIsNone(re.search(r"\bsrcset\b|\.action\s*=", source))
        gallery = read("assets/gallery.js")
        self.assertIn("this.runPrefix", gallery)
        self.assertIn("/actions/runs/", gallery)

    def test_the_guard_patterns_catch_an_unguarded_assignment(self) -> None:
        pattern = r"\.(?:href|src)\s*=(?!=)\s*([^;\n]*)"
        for unguarded in ("image.src = frame.image;", "link.href = href;", "link.href=`${base}/x`;"):
            value = re.findall(pattern, unguarded)[0]
            self.assertIsNone(re.fullmatch(r"([A-Za-z]+)\(.*\)", value.strip()), unguarded)
        self.assertEqual(re.findall(pattern, "if (url.href === value) return;"), [])

    def test_the_dialog_record_reads_the_v1_fields_without_inline_html(self) -> None:
        page, script = read("e2e/index.html"), read("assets/gallery.js")
        self.assertIn('id="capture-dialog"', page)
        self.assertIn('id="capture-dialog-body"', page)
        self.assertIn("showModal", script)
        for field in ("runtime_evidence", "frame.source.pixel", "frame.published.pixel", "lane_id",
                      "production_sha256", "contract_sha256", "contract_url", "tested_run_url", "handoff_run_url",
                      "kit_sha", "kit_version", "pixel_metrics_version", "epoch"):
            self.assertIn(field, script)
        for retired in ("jar_sha256", "source_pixel_validation", "published_pixel_validation", "release.version",
                        "compatibility.lanes", "source_run_url", "target_run_url"):
            self.assertNotIn(retired, script)

    def test_family_badges_come_from_the_verdict(self) -> None:
        script = read("assets/gallery.js")
        self.assertIn('if (verdict.runtime_passed) badges.append(node("span", "verified-badge", "Runtime passed"))',
                      script)
        self.assertIn("verdict.semantic_valid && !verdict.defect", script)
        self.assertNotIn("/${lane.reviewed_frame_count}", script)
        self.assertIn('plural(lane.review.reviewed_frame_count, "reviewed frame")', script)


class PageSafetyTest(unittest.TestCase):
    def test_both_pages_carry_the_exact_policy_and_referrer(self) -> None:
        for relative in PAGES:
            page = Page(read(relative))
            policies = page.find("meta", http_equiv="Content-Security-Policy")
            self.assertEqual([attrs["content"] for attrs in policies], [CSP], relative)
            self.assertEqual([attrs["content"] for attrs in page.find("meta", name="referrer")], ["no-referrer"])
            # ``dark`` or ``dark light`` by config (templating.color_scheme); theme.css decides the rest.
            self.assertEqual([attrs["content"] for attrs in page.find("meta", name="color-scheme")],
                             ["{{mb:color_scheme}}"])
            self.assertEqual([attrs["content"] for attrs in page.find("meta", name="theme-color")],
                             ["{{mb:theme_color}}"])
            self.assertEqual(page.find("html")[0].get("lang"), "en")
            self.assertEqual(len(page.find("meta", charset="utf-8")), 1)

    def test_no_inline_code_style_or_handlers(self) -> None:
        for relative in PAGES:
            page = Page(read(relative))
            self.assertEqual(page.raw, [], f"{relative} holds inline script or style")
            self.assertEqual(page.find("style"), [])
            for tag, attrs in page.tags:
                for name in attrs:
                    self.assertFalse(name.startswith("on"), f"{relative} <{tag} {name}>")
                    self.assertNotEqual(name, "style", f"{relative} <{tag} style>")
                    self.assertNotEqual(name, "srcdoc")

    def test_scripts_stylesheets_and_images_are_local(self) -> None:
        for relative in PAGES:
            page = Page(read(relative))
            scripts = page.find("script")
            self.assertEqual(len(scripts), 1)
            for attrs in scripts:
                self.assertRegex(attrs["src"] or "", r"^(?:\.\./)?assets/(?:site|gallery)\.js$")
                self.assertIn("defer", attrs)
            stylesheets = [attrs["href"] for tag, attrs in page.tags if tag == "link" and attrs.get("rel") == "stylesheet"]
            self.assertEqual([Path(href or "").name for href in stylesheets], ["theme.css", "styles.css"])
            for tag, attrs in page.tags:
                for name in ("src", "href"):
                    value = attrs.get(name)
                    if value is None or value.startswith(("{{mb:", "#")):
                        continue
                    self.assertNotRegex(value, r"^[a-z]+:|^//", f"{relative} <{tag} {name}={value}>")

    def test_the_pages_render_title_brand_and_description_without_javascript(self) -> None:
        landing = read("index.html")
        for fragment in ("<title>{{mb:name}}</title>", '<meta name="description" content="{{mb:meta_description}}">',
                         "<span>{{mb:name}}</span>", ">{{mb:description}}</p>", ">{{mb:tagline}}</h1>"):
            self.assertIn(fragment, landing)
        self.assertIn("<title>Verified E2E · {{mb:name}}</title>", read("e2e/index.html"))


class AccessibilityTest(unittest.TestCase):
    def test_landmarks_skip_links_and_live_regions(self) -> None:
        for relative, target in (("index.html", "Skip to content"), ("e2e/index.html", "Skip to evidence")):
            page = Page(read(relative))
            self.assertEqual(page.find("a", **{"class": "skip-link"})[0]["href"], "#main")
            self.assertIn(target, read(relative))
            self.assertEqual(len(page.find("main", id="main")), 1)
            self.assertEqual(len(page.find("nav", aria_label="Primary navigation")), 1)
            self.assertTrue(page.find("p", role="status"))
            self.assertIn("<noscript>", read(relative))
        gallery = Page(read("e2e/index.html"))
        self.assertEqual(gallery.find("div", id="view-switch")[0]["role"], "group")
        self.assertEqual(gallery.find("div", id="view-switch")[0]["aria-label"], "Choose gallery view")
        self.assertEqual(gallery.find("div", id="release-tabs")[0]["role"], "tablist")
        self.assertEqual(gallery.find("dialog", id="capture-dialog")[0]["aria-labelledby"], "capture-dialog-title")
        self.assertEqual(gallery.find("div", id="release-summary")[0]["aria-live"], "polite")
        for button in gallery.find("button", **{"class": "view-button is-active"}):
            self.assertEqual(button["aria-pressed"], "true")

    def test_the_gallery_script_implements_the_keyboard_and_dialog_contract(self) -> None:
        script = read("assets/gallery.js")
        for fragment in ('setAttribute("role", "tab")', 'setAttribute("role", "tabpanel")', '"aria-selected"',
                         '"aria-controls"', "tabIndex", '"ArrowRight"', '"ArrowLeft"', '"Home"', '"End"',
                         '"aria-pressed"', 'title.id = "capture-dialog-title"', "event.target === this.dialog",
                         'addEventListener("close"', "replaceChildren()"):
            self.assertIn(fragment, script)

    def test_the_stylesheet_keeps_targets_focus_and_motion_rules(self) -> None:
        styles = read("assets/styles.css")
        self.assertIn("min-height: 44px", styles)
        self.assertRegex(styles, r":focus-visible \{\n  outline: 3px solid var\(--highlight\);")
        self.assertIn("@media (prefers-reduced-motion: reduce)", styles)


class CapturePermalinkTest(unittest.TestCase):
    """Every validated capture has its own URL, ``#capture/<key>/<frame id>``, that opens its record."""

    def test_the_gallery_names_the_open_capture_in_the_address(self) -> None:
        script = read("assets/gallery.js")
        for fragment in ('const CAPTURE_FRAGMENT = "#capture/";', 'window.addEventListener("hashchange"',
                         "window.history.replaceState(null, \"\", url)", "this.revealFromLocation();",
                         "this.forgetCapture();", 'this.captureLink(frame, "Link to this capture")',
                         "navigator.clipboard.writeText(url)", "const swapped = this.dialog.open;",
                         "this.recordTitle.focus();", '"aria-label",'):
            self.assertIn(fragment, script)
        # The address never reaches the DOM or a URL sink except through the frame inventory lookup.
        self.assertIn("this.frameById.get(this.identity(target.key, target.frameId))", script)
        self.assertNotIn("location.hash =", script)

    def test_fragments_round_trip_and_reject_anything_else(self) -> None:
        node = shutil.which("node")
        self.assertIsNotNone(node, "node is required for the front-end checks (kit CI 'Front end')")
        script = read("assets/gallery.js")
        constant = re.search(r'^const CAPTURE_FRAGMENT = .*;$', script, re.MULTILINE)
        functions = re.search(r"^function captureFragment\(.*?^}\n\n.*?^function parseCaptureFragment\(.*?^}\n",
                              script, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(constant)
        self.assertIsNotNone(functions)
        harness = constant.group(0) + "\n" + functions.group(0) + r"""
const assert = require("node:assert/strict");
const frame = "fabric-1.21.1/full/client_a/cape_import_standard";
assert.equal(captureFragment("mc1.21.1", frame), "#capture/mc1.21.1/fabric-1.21.1/full/client_a/cape_import_standard");
assert.deepEqual(parseCaptureFragment(captureFragment("mc1.21.1", frame)), { key: "mc1.21.1", frameId: frame });
for (const [key, id] of [["release/1.20", "a b/c#d/e?f"], ["é", "x%2Fy/%"], ["k", "only"]]) {
  assert.deepEqual(parseCaptureFragment(captureFragment(key, id)), { key, frameId: id });
}
for (const hash of ["", "#", "#capture/", "#capture/key", "#capture/key/", "#capture//frame", "#other/key/frame",
                    "#capture/%E0%A4%A/frame", "#capture/key/%E0%A4%A", null, 7]) {
  assert.equal(parseCaptureFragment(hash), null, String(hash));
}
console.log("ok");
"""
        completed = subprocess.run([node, "-e", harness], capture_output=True, timeout=60, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8", "replace"))
        self.assertEqual(completed.stdout.decode().strip(), "ok")


class StylesheetTest(unittest.TestCase):
    def test_no_colour_literal_survives(self) -> None:
        styles = read("assets/styles.css")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", styles))
        self.assertIsNone(re.search(r"\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(\s*[0-9.]", styles))
        self.assertIsNone(re.search(r":\s*(?:white|black|red|green|blue|gray|grey)\b", styles))
        for literal in QS_LITERALS:
            self.assertNotIn(literal, styles)
        self.assertNotIn("image-rendering", styles, "pixelated rendering is a generated theme rule")

    def test_every_custom_property_is_defined(self) -> None:
        styles = read("assets/styles.css")
        defined = set(re.findall(r"(--[a-z-]+)\s*:", theme_css({"dark": THEME, "light": None})))
        defined |= set(re.findall(r"(--[a-z-]+)\s*:", styles))
        used = set(re.findall(r"var\((--[a-z-]+)\)", styles))
        self.assertEqual(used - defined, set())
        self.assertTrue({"--accent-rgb", "--image-well", "--surface-overlay"} <= used)


if __name__ == "__main__":
    unittest.main()
