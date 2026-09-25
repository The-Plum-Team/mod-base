"""``render_site`` (SPEC §3.8, §3.9, §6.1, §6.2): the published tree of a Quick Skin-like and a Block
Pops-like fixture site, its data documents and the Quick Skin golden text.

Ports the render/pixel parts of Quick Skin ``test_pages_site.py`` (link page, gallery and machine
inventory; content-addressed images whose digest is the served bytes; the complete per-capture
validation record; compact paired family evidence; untrusted project text never becomes markup)
onto the v1 documents, plus the v1 rules: the allowlisted kit files, the generated ``theme.css``
and icon, ``build.json``, the family view hidden without families and the light theme.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import tempfile
import unittest
import warnings
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest import mock

import mod_base
from mod_base.errors import MbError
from mod_base.imaging.metrics import SizePolicy, inspect_webp
from mod_base.model import documents, grammar
from mod_base.model.canonical import canonical_json
from mod_base.pages import build
from mod_base.pages.templating import theme_css
from mod_base.runtime import build_invocation
from tests import test_build_support as bs
from tests.fixtures.mods import support

QS_REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
QS_URL = f"https://github.com/{QS_REPOSITORY}"
#: Today's Quick Skin landing page (``site/index.html``): its visible static text, in order. The
#: intentional v1 changes are applied in :data:`LANDING_TEXT`: the principles and the evidence lead
#: now come from ``site-data.copy`` (rendered by ``site.js``), and the hero heading is plain text
#: before ``site.js`` accents its second sentence (``test_build_frontend`` checks the script-rendered
#: text against today's output).
QS_LANDING_TEXT = [
    "Skip to content", "Quick Skin", "E2E gallery", "Source", "Minecraft appearance mod",
    "Change your look.", "Stay in the game.",
    "Change, preview and synchronize skins and capes across supported Minecraft loaders.",
    "Explore verified E2E", "Get Quick Skin", "Official destinations", "Everything in one place",
    "JavaScript is required to render the verified release inventory. Direct links remain available in the navigation.",
    "Tested in packaged Minecraft", "Evidence you can inspect",
    "Every public frame comes from a successful packaged-JAR scenario and is bound to a branch, commit, loader and "
    "Actions run. Completed optional-mod lanes add compact clean-versus-modded comparisons.",
    "Loading verified versions…", "What “verified” means", "Real jars, real clients, public proof.",
    "The same staged production jars launch under their exact Minecraft and loader versions.",
    "Required screenshots pass integrity, entropy and before/after pixel checks.",
    "The gallery publishes only curated images and provenance—never logs, crash reports or secrets.",
    "Optional-mod pairs appear only after the full runtime lane and complete visual review are clean.",
    "Quick Skin · All Rights Reserved", "Report an issue",
]
QS_PRINCIPLES = QS_LANDING_TEXT[19:23]
QS_EVIDENCE_LEAD = QS_LANDING_TEXT[15]
LANDING_TEXT = [text for text in QS_LANDING_TEXT if text not in QS_PRINCIPLES and text != QS_EVIDENCE_LEAD]
LANDING_TEXT[LANDING_TEXT.index("Change your look.")] = "Change your look. Stay in the game."
LANDING_TEXT.remove("Stay in the game.")
QS_LANDING_LINKS = ["#main", "./", "e2e/", QS_URL, "e2e/", "https://modrinth.com/mod/quick-skin", f"{QS_URL}/issues"]
#: Today's gallery page static text; the lead, the compatibility copy and filters and the
#: methodology paragraphs now come from ``gallery-data`` (their copy is checked separately), the
#: filter bar gains the Minecraft filter and the compare note names releases.
GALLERY_TEXT = [
    "Skip to evidence", "Quick Skin", "E2E gallery", "Actions", "Packaged-runtime evidence", "See what passed.",
    "Visual explorer", "Validated captures", "Gallery", "Compare versions", "Minecraft", "All versions", "Loader",
    "All loaders", "Scenario", "All scenarios", "Client role", "All clients", "Find a checkpoint",
    "Checkpoint to align", "Loader", "All loaders",
    "A blank cell is explicit: no validated frame was published for that exact release, Minecraft version and loader "
    "checkpoint.",
    "Loading validated evidence…",
    "JavaScript is required for filtering and comparison. The evidence inventory remains available as", "JSON", ".",
    "Close", "Evidence contract", "What this page does—and does not—claim",
    "Download the machine-readable gallery inventory", "Quick Skin · packaged E2E evidence", "Back to project home",
]
GALLERY_LINKS = ["#main", "../", "./", f"{QS_URL}/actions/workflows/on-demand-e2e.yml", "gallery-data.json",
                 "gallery-data.json", "../"]
#: Today's gallery copy, now delivered through ``gallery-data.copy``; the third methodology paragraph
#: drops the stale "two local pairs and, where contracted, two multiplayer pairs" count (SPEC §6.2).
QS_GALLERY_LEAD = (
    "Browse every validated capture by version, align the same semantic checkpoint across supported Minecraft "
    "versions and loaders, or inspect paired evidence from the optional-mod compatibility runs. Select any regular "
    "capture to open its complete validation record: the checkpoint contract, the assertion the packaged client "
    "actually passed, the pixel and comparison measurements, the tested JAR digest and the exact runs that produced it.")
QS_METHODOLOGY = [
    "The required E2E gate validates mod state, screenshot integrity and expected pixel changes. These images let "
    "humans compare the resulting UI and rendering across versions.",
    "Every capture carries the evidence that admitted it: the versioned scenario contract it belongs to, the passed "
    "deterministic assertion emitted by the packaged client, the decoded pixel measurements of both the original PNG "
    "and the published image, the required pixel change against its paired checkpoint, and the SHA-256 of the exact "
    "mod JAR under test. Open a capture to read that record; nothing on this page is published without it.",
    "The AI visual review remains advisory for normal release evidence. Optional-mod comparisons are published only "
    "after the complete compatibility lane passes its deterministic runtime assertions and every reviewed frame "
    "receives a clean visual verdict. Absence of compatibility evidence is not a claim of incompatibility.",
]
#: Quick Skin's ``:root`` tokens and hand-written colour literals today, under their v1 names: every
#: token ``theme.css`` generates for the Quick Skin palette (``color-scheme`` included).
QS_TOKENS = {"color-scheme": "dark", "--bg": "#0d120f", "--surface": "#141b16", "--surface-raised": "#1a241d",
             "--surface-soft": "#202c24", "--text": "#f3f7f3", "--muted": "#a9b7ac", "--line": "#304137",
             "--accent": "#77e39b", "--accent-strong": "#40c973", "--highlight": "#e6c875", "--danger": "#ff9b91",
             "--image-well": "#080b09", "--on-accent": "#07150c", "--code-well": "#0b100d",
             "--surface-overlay": "rgb(20 27 22 / 88%)", "--accent-rgb": "119 227 155",
             "--accent-strong-rgb": "64 201 115", "--backdrop": "rgb(4 8 6 / 74%)", "--shadow-rgb": "0 0 0"}
#: Today's Quick Skin ``<meta name="theme-color">``.
QS_THEME_COLOR = "#111713"


class Text(HTMLParser):
    """Visible text chunks (outside ``<head>``) and every ``href`` of an HTML page."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[str] = []
        self._head = False
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self._head = True
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._head = False

    def handle_data(self, data: str) -> None:
        if not self._head and data.strip():
            self.chunks.append(" ".join(data.split()))


class Page(HTMLParser):
    """The ``content`` of every ``<meta name=...>`` of a page."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.metas: list[tuple[str | None, str | None]] = []
        self.feed(page)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and "name" in attributes:
            self.metas.append((attributes["name"], attributes.get("content")))

    def meta(self, name: str) -> list[str | None]:
        return [content for found, content in self.metas if found == name]


def listing(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class RenderFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-render-")).resolve()
        cls.publication = bs.Publication(cls.directory / "qs")
        cls.site = cls.directory / "qs-site"
        cls.written = bs.render(cls.publication.invocation(), cls.site, kit_root=cls.publication.kit,
                                bundles=cls.publication.bundles(), families=cls.publication.families())
        cls.gallery = json.loads((cls.site / "e2e" / "gallery-data.json").read_bytes())
        cls.site_data = json.loads((cls.site / "site-data.json").read_bytes())
        cls.bp_mod, cls.bp_bundle, cls.bp_kit = bs.bp_bundle(cls.directory / "bp")
        cls.bp_site = cls.directory / "bp-site"
        bs.render(cls.bp_invocation(), cls.bp_site, kit_root=cls.bp_kit, bundles=[cls.bp_bundle], families=[])

    @classmethod
    def bp_invocation(cls, **changes: str):
        return build_invocation(cls.bp_mod.root, None, {**support.pages_environment(cls.bp_mod, job="build"),
                                                        **changes}, root=cls.bp_kit)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))

    def render(self, invocation: Any = None, *, bundles: list[dict[str, Any]] | None = None,
               families: list[dict[str, Any]] | None = None, name: str = "site") -> dict[str, bytes]:
        return bs.render(invocation or self.publication.invocation(), self.work / name, kit_root=self.publication.kit,
                         bundles=self.publication.bundles() if bundles is None else bundles,
                         families=self.publication.families() if families is None else families)


class QuickSkinSiteTest(RenderFixture):
    def test_the_published_tree_is_exactly_the_allowlist_plus_generated_files(self) -> None:
        files = listing(self.site)
        self.assertEqual(files, self.written)
        generated = {".nojekyll", "site-data.json", "build.json", "e2e/gallery-data.json", "assets/theme.css",
                     "assets/icon.png"}
        images = {path for path in files if path.startswith(("e2e/images/", "e2e/families/"))}
        self.assertEqual(set(files), set(build.SITE_ALLOWLIST) | generated | images)
        self.assertEqual(files[".nojekyll"], b"")
        for relative in ("assets/site.js", "assets/gallery.js", "assets/styles.css"):
            self.assertEqual(files[relative], (bs.KIT_ROOT / "site" / relative).read_bytes())
        for relative in build.TEMPLATES:
            page = files[relative].decode("utf-8")
            self.assertNotIn("{{", page)
            self.assertNotIn("<!-- mb:", page)
        for path in images:
            self.assertRegex(path, r"^e2e/(?:images/mc[0-9.]+|families/mod-compatibility/images)/[0-9a-f]{64}\.webp$")
            self.assertEqual(Path(path).stem, hashlib.sha256(files[path]).hexdigest())

    def test_theme_and_icon_are_generated_from_the_config(self) -> None:
        config = self.publication.invocation().config
        theme = (self.site / "assets" / "theme.css").read_text(encoding="utf-8")
        self.assertEqual(theme, theme_css(config.theme) + ".brand-icon{image-rendering:pixelated}\n")
        icon = (self.site / "assets" / "icon.png").read_bytes()
        source = (self.publication.mod.root / "icon.png").read_bytes()
        self.assertIn(b"tEXt", source)
        self.assertNotIn(b"tEXt", icon)
        self.assertNotIn(b"private note", icon)
        self.assertEqual(icon[25], 6, "the icon keeps its RGBA colour type")
        self.assertEqual(build._icon_png(icon), icon, "the icon encoding is idempotent")

    def test_the_gallery_publishes_every_validated_capture_record(self) -> None:
        documents.validate_gallery(self.gallery)
        bundles = {bundle["manifest"]["key"]: bundle for bundle in self.publication.bundles()}
        self.assertEqual([release["key"] for release in self.gallery["releases"]], list(bs.QS_KEYS))
        self.assertEqual(len(self.gallery["frames"]), sum(len(bundle["manifest"]["frames"]) for bundle in bundles.values()))
        for frame in self.gallery["frames"]:
            manifest = bundles[frame["key"]]["manifest"]
            source = next(item for item in manifest["frames"] if item["frame_id"] == frame["frame_id"])
            for field in ("runtime_evidence", "review_tier", "title", "expectation", "capture_order", "step", "role"):
                self.assertEqual(frame[field], source[field])
            served = (self.site / "e2e" / frame["image"]).read_bytes()
            self.assertEqual(served, (bundles[frame["key"]]["root"] / source["derivative"]["path"]).read_bytes())
            self.assertEqual(frame["published"]["file_sha256"], hashlib.sha256(served).hexdigest())
            self.assertEqual(frame["published"]["pixel"], inspect_webp(served, SizePolicy.exact(160, 90)))
            self.assertEqual(frame["source"]["pixel"], source["source"]["pixel"])
            self.assertNotEqual(frame["source"]["file_sha256"], frame["published"]["file_sha256"])
            self.assertEqual(frame["provenance"]["tested_run_url"], grammar.run_url(bs.support.REPOSITORIES["qs_like"],
                                                                                    bs.E2E_RUN))
            self.assertLessEqual(len(frame["alt"]), build.MAX_ALT_CHARS)
        self.assertTrue(self.gallery["comparisons"])
        for comparison in self.gallery["comparisons"]:
            manifest = bundles[comparison["key"]]["manifest"]
            source = next(item for item in manifest["comparisons"] if item["comparison_id"] == comparison["comparison_id"])
            self.assertEqual((comparison["source"], comparison["published"]), (source["source"], source["derivative"]))
        release = self.gallery["releases"][0]
        self.assertEqual(release["contract_url"], f"https://github.com/The-Plum-Team/qs-like/blob/"
                                                  f"{self.publication.mod.commit}/e2e/scenario-contract.json")
        self.assertEqual(release["loader_names"], ["Fabric", "Forge"])
        lanes = {(lane["key"], lane["lane_id"]): lane for lane in self.gallery["lanes"]}
        self.assertTrue(all(lane["jars"]["production_sha256"] for lane in lanes.values()))

    def test_labels_copy_and_build_identity_come_from_the_config_and_kit(self) -> None:
        config = self.publication.invocation().config
        self.assertEqual(self.gallery["labels"], config.labels)
        self.assertEqual(self.gallery["copy"], {"gallery_lead": config.copy["gallery_lead"],
                                                "methodology": config.copy["methodology"],
                                                "family_notes": config.copy["family_notes"]})
        self.assertEqual(self.gallery["project"]["actions_url"],
                         "https://github.com/The-Plum-Team/qs-like/actions/workflows/on-demand-e2e.yml")
        self.assertEqual(self.gallery["build"], {"implementation_sha": self.publication.mod.commit,
                                                 "kit_sha": bs.support.KIT_SHA, "kit_version": mod_base.__version__,
                                                 "pixel_metrics_version": 1})
        documents.validate_site(self.site_data)
        self.assertEqual(self.site_data["project"]["description"], bs.DESCRIPTION)
        self.assertEqual(self.site_data["project"]["icon"], "assets/icon.png")
        self.assertEqual(self.site_data["copy"]["principles"], config.copy["principles"])
        self.assertEqual(self.site_data["families"], [{"family": "mod-compatibility", "title": "Mod compatibility",
                                                       "lane_count": 1, "available": True}])

    def test_build_json_names_the_inventory_of_every_other_file(self) -> None:
        record = json.loads((self.site / "build.json").read_bytes())
        documents.validate_build(record)
        self.assertEqual(canonical_json(record), (self.site / "build.json").read_bytes())
        inventory = [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                     for path, data in listing(self.site).items() if path != "build.json"]
        self.assertEqual(record["site_inventory_sha256"], documents.inventory_sha256(inventory))
        self.assertEqual(record["implementation"]["run_id"], bs.PAGES_RUN)
        self.assertEqual(record["kit"]["sha"], bs.support.KIT_SHA)

    def test_family_images_and_releases_are_published_per_key(self) -> None:
        family = self.gallery["families"][0]
        self.assertEqual((family["available"], family["status"]), (True, "available"))
        self.assertEqual([(release["key"], release["status"]) for release in family["releases"]],
                         [("mc1.20.1", "available"), ("mc26.3", "unavailable")])
        collected = self.publication.collected_family
        for lane in family["lanes"]:
            self.assertEqual(lane["key"], "mc1.20.1")
            for pair in lane["pairs"]:
                for side in ("reference", "candidate"):
                    image = pair[side]["image"]
                    self.assertTrue(image["path"].startswith("families/mod-compatibility/images/"))
                    self.assertEqual((self.site / "e2e" / image["path"]).read_bytes(),
                                     (collected / "images" / f"{image['sha256']}.webp").read_bytes())

    def test_a_family_with_no_available_key_is_unavailable(self) -> None:
        self.render(families=[bs.load_family(None, key=key) for key in bs.QS_KEYS])
        gallery = json.loads((self.work / "site" / "e2e" / "gallery-data.json").read_bytes())
        self.assertEqual((gallery["families"][0]["available"], gallery["families"][0]["status"]), (False, "unavailable"))
        self.assertEqual(gallery["families"][0]["lanes"], [])
        self.assertFalse(list((self.work / "site" / "e2e").glob("families")))
        self.render(families=[bs.load_family(None, key=key, status="superseded") for key in bs.QS_KEYS], name="drift")
        drift = json.loads((self.work / "drift" / "e2e" / "gallery-data.json").read_bytes())
        self.assertEqual(drift["families"][0]["status"], "superseded")

    def test_composed_frames_keep_their_epoch_and_original_tested_run(self) -> None:
        bundles = self.publication.bundles()
        manifest = bundles[0]["manifest"]
        baseline_run = {**bundles[0]["selection"]["source"]["tested_run"], "run_id": 3131,
                        "created_at": "2026-08-01T10:00:00Z", "commit": "d" * 40, "jar_sha256": "e" * 64}
        for position, frame in enumerate(manifest["frames"]):
            frame["epoch"] = "baseline" if frame["lane_id"].startswith("fabric") else "selected"
            frame["tested"] = baseline_run if frame["epoch"] == "baseline" else {
                **bundles[0]["selection"]["source"]["tested_run"], "jar_sha256": "f" * 64}
        manifest["scope"] = {"kind": "composed"}
        self.render(bundles=bundles, families=[bs.load_family(None, key=key) for key in bs.QS_KEYS])
        gallery = json.loads((self.work / "site" / "e2e" / "gallery-data.json").read_bytes())
        baseline = [frame for frame in gallery["frames"] if frame.get("epoch") == "baseline"]
        self.assertTrue(baseline)
        for frame in baseline:
            self.assertEqual(frame["provenance"]["tested_run_url"],
                             grammar.run_url(bs.support.REPOSITORIES["qs_like"], 3131))
            self.assertEqual(frame["provenance"]["tested_commit"], "d" * 40)
        self.assertEqual({lane["epoch"] for lane in gallery["lanes"] if lane["key"] == "mc1.20.1"},
                         {"baseline", "selected"})
        self.assertEqual(gallery["releases"][0]["scope"], "composed")

    def test_the_quick_skin_golden_text(self) -> None:
        site = self.work / "golden"
        invocation = self.publication.invocation(
            GITHUB_REPOSITORY=QS_REPOSITORY,
            GITHUB_WORKFLOW_REF=f"{QS_REPOSITORY}/.github/workflows/pages.yml@refs/heads/master")
        bs.render(invocation, site, kit_root=self.publication.kit,
                  bundles=self.publication.bundles(), families=self.publication.families())
        landing = Text((site / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(landing.chunks, LANDING_TEXT)
        self.assertEqual(landing.links, QS_LANDING_LINKS)
        data = json.loads((site / "site-data.json").read_bytes())
        self.assertEqual(data["copy"]["principles"], QS_PRINCIPLES)
        self.assertEqual(data["copy"]["evidence_lead"], QS_EVIDENCE_LEAD)
        self.assertEqual([link["url"] for link in data["project"]["links"]],
                         ["https://modrinth.com/mod/quick-skin", "https://www.curseforge.com/minecraft/mc-mods/quick-skin"])
        gallery = Text((site / "e2e" / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(gallery.chunks, GALLERY_TEXT)
        gallery_data = json.loads((site / "e2e" / "gallery-data.json").read_bytes())
        self.assertEqual(gallery_data["copy"]["gallery_lead"], QS_GALLERY_LEAD)
        self.assertEqual(gallery_data["copy"]["methodology"], QS_METHODOLOGY)
        self.assertEqual(gallery_data["labels"]["search_placeholder"], "Cape, skin, title screen…")
        self.assertEqual(gallery.links, GALLERY_LINKS)
        head = (site / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Quick Skin</title>", head)
        self.assertIn('content="Quick Skin downloads, source and verified packaged-Minecraft visual evidence."', head)
        self.assertIn("<title>Verified E2E · Quick Skin</title>", (site / "e2e" / "index.html").read_text("utf-8"))
        tokens = dict(item.split(":", 1) for item in
                      re.search(r":root\{([^}]*)\}", (site / "assets" / "theme.css").read_text("utf-8"))[1].split(";"))
        self.assertEqual(tokens, QS_TOKENS)
        for relative in build.TEMPLATES:
            page = Page((site / relative).read_text(encoding="utf-8"))
            self.assertEqual(page.meta("theme-color"), [QS_THEME_COLOR])
            self.assertEqual(page.meta("color-scheme"), ["dark"], "Quick Skin is dark-only (no light first paint)")

    def test_untrusted_text_is_escaped_or_refused(self) -> None:
        config = json.loads((self.publication.mod.root / "site" / "mod-base.json").read_text(encoding="utf-8"))
        config["project"]["name"] = 'Q & "S"'
        path = self.work / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.render(self.publication.invocation(config=path), name="escaped")
        page = (self.work / "escaped" / "index.html").read_text(encoding="utf-8")
        self.assertIn("<title>Q &amp; &quot;S&quot;</title>", page)
        self.assertIn('aria-label="Q &amp; &quot;S&quot; home"', page)
        self.assertNotIn('Q & "S"', page)

    def test_a_matrix_description_must_be_display_text(self) -> None:
        mod = support.materialize("qs_like", self.work / "hostile", mutate=lambda root: (
            bs.qs_site_mutation(root), self._hostile_matrix(root)))
        invocation = build_invocation(mod.root, None, support.pages_environment(mod, job="build"),
                                      root=self.publication.kit)
        with self.assertRaisesRegex(MbError, "is not display text"):
            bs.render(invocation, self.work / "hostile-site", kit_root=self.publication.kit,
                      bundles=self.publication.bundles(), families=self.publication.families())
        self.assertFalse((self.work / "hostile-site").exists())

    @staticmethod
    def _hostile_matrix(root: Path) -> None:
        path = root / "release" / "release-matrix.json"
        matrix = json.loads(path.read_text(encoding="utf-8"))
        matrix["project"] = {"description": "<img src=x onerror=alert(1)>"}
        path.write_text(json.dumps(matrix), encoding="utf-8")

    def test_the_site_bounds_are_enforced_before_any_image_is_read(self) -> None:
        images = {path: data for path, data in self.written.items() if path.startswith(("e2e/images/", "e2e/families/"))}
        statics = sum(len((bs.KIT_ROOT / "site" / relative).read_bytes()) for relative in build.SITE_ALLOWLIST)
        generated = 6  # theme.css, .nojekyll, site-data.json, gallery-data.json, build.json and the icon
        limits = {"bytes": {"MAX_SITE_BYTES": statics + sum(map(len, images.values())) - 1},
                  "files": {"MAX_SITE_FILES": len(images) + len(build.SITE_ALLOWLIST) + generated - 1}}
        for label, bound in limits.items():
            with self.subTest(label), mock.patch.object(build, "_read_image", side_effect=AssertionError("read")), \
                    mock.patch.multiple(build.lim, **bound), self.assertRaises(MbError) as caught:
                self.render(name=f"bounded-{label}")
            self.assertEqual(caught.exception.reason, "site-bounds")
            self.assertFalse((self.work / f"bounded-{label}").exists())

    def test_inputs_that_changed_or_disagree_are_refused(self) -> None:
        tampered = self.work / "tampered"
        shutil.copytree(self.publication.collected["mc26.3"], tampered)
        image = next((tampered / "images").iterdir())
        data = image.read_bytes()
        image.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
        bundles = self.publication.bundles()
        bundles[1] = bs.load_bundle(tampered)
        families = self.publication.families()
        cases = {
            "image changed after validation": (bundles, families),
            "two bundles of one key": (self.publication.bundles()[:1] * 2, families[:1]),
            "family key outside the bundles": (self.publication.bundles()[:1], families),
            "available leg without projection": (self.publication.bundles(),
                                                 [{**families[0], "projection": None}, families[1]]),
            "unknown status": (self.publication.bundles(), [families[0], {**families[1], "status": "gone"}]),
        }
        for label, (given_bundles, given_families) in cases.items():
            with self.subTest(label), self.assertRaises(MbError):
                self.render(bundles=given_bundles, families=given_families, name=label.replace(" ", "-"))
            self.assertFalse((self.work / label.replace(" ", "-")).exists())


class BlockPopsSiteTest(RenderFixture):
    def test_the_light_theme_and_no_family_ui(self) -> None:
        files = listing(self.bp_site)
        self.assertNotIn("assets/icon.png", files)
        theme = files["assets/theme.css"].decode("utf-8")
        self.assertIn("@media (prefers-color-scheme: light){:root{color-scheme:light;--bg:#f4f7fd;", theme)
        self.assertNotIn("image-rendering", theme)
        gallery_page = files["e2e/index.html"].decode("utf-8")
        for page in (gallery_page, files["index.html"].decode("utf-8")):
            self.assertEqual(Page(page).meta("color-scheme"), ["dark light"], "a light theme admits both schemes")
        self.assertNotIn('id="family-views"', gallery_page)
        self.assertNotIn("icon.png", gallery_page)
        landing = files["index.html"].decode("utf-8")
        self.assertNotIn("Get BlockPops", landing)
        self.assertIn('<p id="project-description" class="hero-description">Collect, display and play with pop '
                      "figures.</p>", landing)
        self.assertIn('content="BlockPops source and verified packaged-Minecraft visual evidence."', landing)
        gallery = json.loads(files["e2e/gallery-data.json"])
        documents.validate_gallery(gallery)
        self.assertEqual(gallery["families"], [])
        self.assertEqual(gallery["labels"]["release_prefix"], "Branch")
        release = gallery["releases"][0]
        self.assertEqual(release["label"], self.bp_bundle["expectation"]["label"])
        self.assertEqual(release["minecraft"], ["1.21.1"])
        self.assertEqual(release["loader_names"], ["Fabric", "NeoForge"])
        site = json.loads(files["site-data.json"])
        self.assertEqual((site["families"], site["project"]["links"], site["project"]["icon"]), ([], [], None))
        self.assertTrue(all(path.startswith(f"e2e/images/{self.bp_bundle['manifest']['key']}/")
                            for path in files if path.startswith("e2e/images/")))


class IconTest(unittest.TestCase):
    @staticmethod
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    def test_malformed_icons_are_refused(self) -> None:
        icon = bs.icon_png()
        end = self.chunk(b"IEND", b"")
        body = icon[8:-len(end)]
        cases = {
            "not a PNG": b"GIF89a" + icon[6:],
            "trailing bytes": icon + b"x",
            "bad CRC": icon[:-1] + bytes([icon[-1] ^ 1]),
            "unknown critical chunk": icon[:8] + self.chunk(b"ABCD", b"x") + body + end,
            "no IHDR first": icon[:8] + self.chunk(b"tEXt", b"a\0b") + end,
            "truncated": icon[:40],
            "second IHDR": icon[:8] + icon[8:33] + icon[8:33] + icon[33:],
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(MbError):
                build._icon_png(data)

    def png(self, *, width: int = 2, height: int = 2, colour: int = 6, depth: int = 8, interlace: int = 0,
            stream: bytes | None = None, before_idat: tuple[tuple[bytes, bytes], ...] = (),
            after_idat: tuple[tuple[bytes, bytes], ...] = (), split: tuple[tuple[bytes, bytes], ...] | None = None
            ) -> bytes:
        """A PNG of ``width``x``height`` whose IDAT is ``stream`` (default: zero-filtered scanlines);
        ``split`` cuts the stream into two IDAT chunks around the given chunks."""

        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[colour]
        row = (width * channels * depth + 7) // 8
        raw = b"".join(b"\0" + bytes(index % 251 + 1 for index in range(row)) for _ in range(height))
        data = zlib.compress(raw, 9) if stream is None else stream
        idat = [(b"IDAT", data)] if split is None else [(b"IDAT", data[:5]), *split, (b"IDAT", data[5:])]
        header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, interlace)
        chunks = [(b"IHDR", header), *before_idat, *idat, *after_idat, (b"IEND", b"")]
        return b"\x89PNG\r\n\x1a\n" + b"".join(self.chunk(kind, body) for kind, body in chunks)

    @staticmethod
    def idat(icon: bytes) -> bytes:
        position, found = 8, []
        while position < len(icon):
            length, kind = struct.unpack(">I4s", icon[position:position + 8])
            if kind == b"IDAT":
                found.append(icon[position + 8:position + 8 + length])
            position += 12 + length
        return b"".join(found)

    def test_the_pixel_stream_is_re_encoded_not_copied(self) -> None:
        raw = b"".join(b"\0" + bytes(range(1, 9)) for _ in range(2))
        stored = zlib.compress(raw, 0)
        icon = build._icon_png(self.png(stream=stored))
        self.assertEqual(self.idat(icon), zlib.compress(raw, 9))
        self.assertNotEqual(self.idat(icon), stored)
        self.assertEqual(build._icon_png(self.png(split=())), build._icon_png(self.png()))

    def test_hidden_or_malformed_pixel_streams_are_refused(self) -> None:
        raw = b"".join(b"\0" + bytes(range(1, 9)) for _ in range(2))
        payload = b"SECRET-PAYLOAD-" * 20
        compressor = zlib.compressobj(9)
        excess = compressor.compress(raw + b"\0" + bytes(8)) + compressor.flush()
        cases = {
            "data after the zlib stream": zlib.compress(raw, 9) + payload,
            "an extra scanline": excess,
            "data after the scanlines": zlib.compress(raw + payload, 9),
            "a short stream": zlib.compress(raw[:-1], 9),
            "a truncated stream": zlib.compress(raw, 9)[:-4],
            "an invalid filter byte": zlib.compress(b"\x05" + raw[1:], 9),
            "not zlib": b"not a zlib stream",
        }
        for label, stream in cases.items():
            with self.subTest(label), self.assertRaises(MbError):
                build._icon_png(self.png(stream=stream))

    def test_chunk_order_and_types_are_enforced(self) -> None:
        palette = bytes([0, 0, 0, 255, 0, 0])
        cases = {
            "PLTE after IDAT": self.png(colour=3, after_idat=((b"PLTE", palette),)),
            "no palette": self.png(colour=3),
            "palette on grey": self.png(colour=0, before_idat=((b"PLTE", palette),)),
            "tRNS on RGBA": self.png(before_idat=((b"tRNS", b"\0\0"),)),
            "two tRNS": self.png(colour=2, before_idat=((b"tRNS", bytes(6)), (b"tRNS", bytes(6)))),
            "bad depth": self.png(colour=2, depth=4),
            "zero width": self.png(width=0),
            "too wide": self.png(width=1025, height=1),
            "interlace method": self.png(interlace=2),
            "IDAT interrupted": self.png(split=((b"tEXt", b"a\0b"),)),
            "ancillary chunk before IHDR": self.png()[:8] + self.chunk(b"tEXt", b"a\0b") + self.png()[8:],
        }
        for label, data in cases.items():
            with self.subTest(label), self.assertRaises(MbError):
                build._icon_png(data)

    def test_indexed_interlaced_and_grey_icons_keep_their_pixels(self) -> None:
        from PIL import Image
        import io

        for mode, options in (("P", {}), ("RGBA", {"interlace": 1}), ("L", {}), ("LA", {"interlace": 1})):
            with self.subTest(mode=mode, **options):
                image = Image.new(mode, (13, 7))
                for x in range(13):
                    for y in range(7):
                        value = (x * 17 + y * 29) % 256
                        image.putpixel((x, y), {"P": value % 4, "RGBA": (value, 255 - value, x, 128 + y),
                                                "L": value, "LA": (value, 40 * y)}[mode])
                if mode == "P":
                    image.putpalette([0, 0, 0, 200, 10, 10, 10, 200, 10, 10, 10, 200])
                    image.info["transparency"] = bytes([0, 255, 128])
                stream = io.BytesIO()
                image.save(stream, format="PNG", **({"interlace": 1} if options else {}),
                           **({"transparency": image.info["transparency"]} if mode == "P" else {}))
                source = stream.getvalue()
                with warnings.catch_warnings():
                    # Pillow warns when an indexed image with a tRNS table is flattened to RGB(A).
                    warnings.simplefilter("ignore", UserWarning)
                    icon = build._icon_png(source)
                with Image.open(io.BytesIO(source)) as expected, Image.open(io.BytesIO(icon)) as actual, \
                        warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    self.assertEqual(actual.mode, expected.mode)
                    self.assertEqual(actual.convert("RGBA").tobytes(), expected.convert("RGBA").tobytes())

    def test_ancillary_chunks_are_dropped_and_transparency_kept(self) -> None:
        icon = bs.icon_png()
        clean = build._icon_png(icon)
        kinds = []
        position = 8
        while position < len(clean):
            length, kind = struct.unpack(">I4s", clean[position:position + 8])
            kinds.append(kind)
            position += 12 + length
        self.assertEqual(kinds[0], b"IHDR")
        self.assertEqual(kinds[-1], b"IEND")
        self.assertTrue(set(kinds) <= {b"IHDR", b"IDAT", b"IEND", b"PLTE", b"tRNS"})
        from PIL import Image
        import io

        with Image.open(io.BytesIO(clean)) as decoded:
            self.assertEqual(decoded.mode, "RGBA")
            self.assertEqual(decoded.getpixel((0, 0))[3], 0)
            self.assertEqual(decoded.info.get("Comment"), None)


if __name__ == "__main__":
    unittest.main()
