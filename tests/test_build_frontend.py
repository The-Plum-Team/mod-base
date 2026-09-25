"""The rendered front end consumes exactly ``mod-base.site`` v1 and ``mod-base.gallery`` v1.

The published ``site.js`` and ``gallery.js`` of a rendered Quick Skin-like site (two keys, one
available and one unavailable family leg) and of a Block Pops-like site (one key, no family) run
under ``node`` against a minimal DOM that only offers the text-only APIs the scripts may use
(assigning ``innerHTML`` or an ``on*`` attribute throws). The tests drive the real interactions:
release tabs and the keyboard, the Minecraft filter, a capture's nine-section validation record
with its Kit fact, the compare grid keyed by release, Minecraft version and loader, the generic
paired family view with verdict-derived badges, and the gates that reject another document kind.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import test_build_support as bs

HARNESS = r'''
"use strict";
const fs = require("fs");
const vm = require("vm");
const config = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const html = fs.readFileSync(config.page, "utf8");

class BaseNode {}
class TextNode extends BaseNode {
  constructor(value) { super(); this.data = String(value); this.parentNode = null; }
  get textContent() { return this.data; }
}
class ClassList {
  constructor(owner) { this.owner = owner; }
  names() { return this.owner.className.split(/\s+/).filter(Boolean); }
  contains(name) { return this.names().includes(name); }
  toggle(name, force) {
    const names = new Set(this.names());
    const on = force === undefined ? !names.has(name) : Boolean(force);
    if (on) names.add(name); else names.delete(name);
    this.owner.className = [...names].join(" ");
    return on;
  }
}
class ElementNode extends BaseNode {
  constructor(tag) {
    super();
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.attributes = {};
    this.listeners = {};
    this.dataset = {};
    this.hidden = false;
    this.className = "";
    this.parentNode = null;
    this.classList = new ClassList(this);
  }
  append(...items) {
    for (const item of items) {
      const node = typeof item === "string" ? new TextNode(item) : item;
      if (!(node instanceof BaseNode)) throw new Error("append of a non-node");
      node.parentNode = this;
      this.childNodes.push(node);
    }
  }
  replaceChildren(...items) { this.childNodes = []; this.append(...items); }
  get children() { return this.childNodes.filter((node) => node instanceof ElementNode); }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) { this.childNodes = []; if (value !== "") this.append(new TextNode(value)); }
  set innerHTML(value) { throw new Error("innerHTML is forbidden"); }
  set outerHTML(value) { throw new Error("outerHTML is forbidden"); }
  // Link and image URLs must arrive through a guard: absolute and normalized (what safeHref,
  // sameOriginPath and httpsLink return), https for a link and same-origin for an image.
  set href(value) { this._href = guardedUrl(value, "href"); }
  get href() { return this._href; }
  set src(value) { this._src = guardedUrl(value, "src"); }
  get src() { return this._src; }
  setAttribute(name, value) {
    if (/^on/i.test(name) || name === "style") throw new Error(`attribute ${name} is forbidden`);
    if (["href", "src", "srcset", "action", "formaction"].includes(name)) throw new Error(`attribute ${name} bypasses the URL guards`);
    this.attributes[name] = String(value);
    if (name === "id") this.id = String(value);
  }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  addEventListener(type, listener) { (this.listeners[type] = this.listeners[type] || []).push(listener); }
  fire(type, extra = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extra };
    for (const listener of this.listeners[type] || []) listener(event);
    return event;
  }
  focus() { state.focused = this; }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === "SELECT") {
      const first = this.children.find((node) => node.tagName === "OPTION");
      return first ? first.value : "";
    }
    return "";
  }
  set value(value) { this._value = String(value); }
}

const state = { focused: null };
const pageLocation = new URL(config.url);
function guardedUrl(value, kind) {
  const text = String(value);
  let url = null;
  try { url = new URL(text); } catch (error) { url = null; }
  if (url === null || url.href !== text) throw new Error(`${kind} ${text} was not normalized by a guard`);
  if (kind === "src" && url.origin !== pageLocation.origin) throw new Error(`cross-origin image ${text}`);
  if (kind === "href" && url.protocol !== "https:") throw new Error(`non-https link ${text}`);
  return text;
}
const byId = new Map();
for (const match of html.matchAll(/<([a-z0-9]+)\b[^>]*\bid="([^"]+)"/g)) {
  const element = new ElementNode(match[1]);
  element.id = match[2];
  byId.set(match[2], element);
}
for (const match of html.matchAll(/<select id="([^"]+)"><option value="([^"]*)">([^<]*)<\/option><\/select>/g)) {
  const item = new ElementNode("option");
  item.value = match[2];
  item.textContent = match[3];
  byId.get(match[1]).append(item);
}
for (const match of html.matchAll(/<(?:h1|button) id="([^"]+)"[^>]*>([^<]*)<\/(?:h1|button)>/g)) {
  byId.get(match[1]).textContent = match[2];
}
// The static view buttons live inside the view switch (the only static nesting the scripts walk).
const switcher = html.match(/<div id="view-switch"[^>]*>([\s\S]*?)<\/div>/);
if (switcher) for (const match of switcher[1].matchAll(/<button id="([^"]+)"/g)) byId.get("view-switch").append(byId.get(match[1]));

const document = {
  createElement: (tag) => new ElementNode(tag),
  createTextNode: (text) => new TextNode(text),
  querySelector(selector) {
    if (!/^#[A-Za-z0-9_-]+$/.test(selector)) throw new Error(`unsupported selector ${selector}`);
    return byId.get(selector.slice(1)) || null;
  }
};
const location = new URL(config.url);
const context = vm.createContext({
  window: { location: { href: location.href, origin: location.origin } },
  document,
  Node: BaseNode,
  URL,
  console,
  fetch: async (url, options) => {
    if (url !== config.fetch || !options || options.credentials !== "same-origin") throw new Error(`unexpected fetch ${url}`);
    return { ok: true, status: 200, json: async () => JSON.parse(fs.readFileSync(config.data, "utf8")) };
  }
});

function* walk(node) {
  yield node;
  if (node instanceof ElementNode) for (const child of node.childNodes) yield* walk(child);
}
function elements(root, predicate) { return [...walk(root)].filter((node) => node instanceof ElementNode && predicate(node)); }
function byClass(root, name) { return elements(root, (node) => node.classList.contains(name)); }
function texts(root, name) { return byClass(root, name).map((node) => node.textContent); }
function settle() { return new Promise((resolve) => setTimeout(resolve, 20)); }
function id(name) { return byId.get(name); }

async function landing() {
  const result = {};
  result.status = id("site-status").textContent;
  result.error = id("site-status").dataset.error || null;
  result.hero = id("hero-title").childNodes.map((node) => node instanceof ElementNode ? `<${node.tagName}>${node.textContent}` : node.textContent);
  result.description = id("project-description").textContent;
  result.lead = id("evidence-lead").textContent;
  result.principles = id("principles-list").children.map((node) => node.textContent);
  result.cards = byClass(id("destination-grid"), "link-card").map((node) => [node.children[0].children[0].textContent, node.children[0].children[1].textContent, node.href]);
  result.releases = byClass(id("release-grid"), "release-card").map((node) => ({ href: node.href, label: node.getAttribute("aria-label"), text: node.textContent }));
  return result;
}

async function gallery() {
  const result = {};
  const status = () => id("gallery-status").textContent;
  result.error = id("gallery-status").dataset.error || null;
  result.status = status();
  if (result.error) return result;
  result.lead = id("gallery-lead").textContent;
  result.placeholder = id("capture-search").placeholder;
  result.methodology = id("methodology-copy").children.map((node) => node.textContent);
  result.summary = texts(id("release-summary"), "summary-item");
  result.tablist = id("release-tabs").getAttribute("aria-label");
  const tabs = id("release-tabs").children;
  result.tabs = tabs.map((tab) => [tab.textContent, tab.getAttribute("aria-selected"), tab.tabIndex]);
  result.minecraft = id("minecraft-filter").children.map((node) => node.value);
  result.loaders = id("loader-filter").children.map((node) => node.textContent);
  result.scenarios = id("scenario-filter").children.map((node) => node.textContent);
  const panels = id("release-panels").children;
  result.cards = panels.map((panel) => byClass(panel, "capture-card").length);
  result.epochs = texts(id("release-panels"), "epoch-badge");
  tabs[0].fire("keydown", { key: "ArrowRight" });
  result.afterArrow = tabs.map((tab) => tab.getAttribute("aria-selected"));
  result.focused = state.focused === tabs[1];
  tabs[1].fire("keydown", { key: "End" });
  result.allCards = byClass(panels[panels.length - 1], "capture-card").length;
  result.allMinecraft = id("minecraft-filter").children.map((node) => node.value);
  id("minecraft-filter").value = result.allMinecraft[result.allMinecraft.length - 1];
  id("minecraft-filter").fire("change");
  result.filteredCards = byClass(panels[panels.length - 1], "capture-card").length;
  result.filteredStatus = status();
  id("minecraft-filter").value = "all";
  id("minecraft-filter").fire("change");
  tabs[0].fire("click");

  const opener = byClass(panels[0], "capture-open")[0];
  opener.fire("click");
  const dialog = id("capture-dialog");
  const body = id("capture-dialog-body");
  result.dialogOpen = Boolean(dialog.open);
  result.sections = body.children.map((node) => node.tagName === "SECTION" ? node.children[0].textContent : node.tagName);
  result.dialogTitle = elements(body, (node) => node.id === "capture-dialog-title").map((node) => node.textContent);
  const terms = elements(body, (node) => node.tagName === "DT").map((node) => node.textContent);
  result.terms = terms;
  result.dialogLinks = elements(body, (node) => node.tagName === "A").map((node) => node.href);
  result.raw = elements(body, (node) => node.tagName === "PRE").map((node) => Object.keys(JSON.parse(node.textContent)));
  dialog.fire("click", { target: dialog });
  result.dialogClosed = !dialog.open && body.children.length === 0;

  id("compare-view-button").fire("click");
  result.compareVisible = !id("compare-view").hidden && id("gallery-view").hidden;
  result.comparePressed = id("compare-view-button").getAttribute("aria-pressed");
  result.cells = texts(id("comparison-grid"), "compare-column-label");
  result.cellCards = byClass(id("comparison-grid"), "capture-card").length;
  result.compareStatus = status();
  const compareLoader = id("compare-loader");
  compareLoader.value = compareLoader.children[compareLoader.children.length - 1].value;
  compareLoader.fire("change");
  result.notApplicable = texts(id("comparison-grid"), "missing-card");

  const switcher = id("view-switch");
  result.views = switcher.children.map((button) => [button.id, button.textContent]);
  result.families = [];
  for (const button of switcher.children.slice(2)) {
    button.fire("click");
    const panel = byId.get("family-views").children.find((view) => view.id === `${button.id}-panel`);
    result.families.push({
      id: button.id,
      pressed: button.getAttribute("aria-pressed"),
      visible: !panel.hidden,
      notes: texts(panel, "compare-note"),
      lanes: byClass(panel, "family-lane").length,
      badges: texts(panel, "verified-badge"),
      reviewed: texts(panel, "review-count"),
      links: elements(panel, (node) => node.tagName === "A" && node.target === "_blank" && !node.href.includes("/e2e/")).map((node) => [node.textContent, node.href]),
      notApplicable: elements(panel, (node) => node.tagName === "LI").map((node) => node.textContent),
      images: elements(panel, (node) => node.tagName === "IMG").map((node) => node.src),
      status: status()
    });
  }
  return result;
}

(async () => {
  let result;
  try {
    vm.runInContext(fs.readFileSync(config.script, "utf8"), context, { filename: config.script });
    await settle();
    result = config.kind === "landing" ? await landing() : await gallery();
  } catch (error) {
    result = { crash: String(error && error.stack || error) };
  }
  process.stdout.write(JSON.stringify(result));
})();
'''


def run_page(site: Path, kind: str, *, data: Path | None = None) -> dict[str, Any]:
    """Run the published script of ``kind`` (``landing``/``gallery``) of ``site`` under node."""

    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is required for the front-end behaviour tests")
    with tempfile.TemporaryDirectory(prefix="mb-frontend-") as directory:
        root = Path(directory)
        (root / "harness.js").write_text(HARNESS, encoding="utf-8")
        page = site / ("index.html" if kind == "landing" else "e2e/index.html")
        config = {
            "kind": kind, "page": str(page),
            "script": str(site / "assets" / ("site.js" if kind == "landing" else "gallery.js")),
            "data": str(data or (site / ("site-data.json" if kind == "landing" else "e2e/gallery-data.json"))),
            "fetch": "site-data.json" if kind == "landing" else "gallery-data.json",
            "url": "https://the-plum-team.github.io/qs-like/" + ("" if kind == "landing" else "e2e/"),
        }
        (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        completed = subprocess.run([node, str(root / "harness.js"), str(root / "config.json")], capture_output=True,
                                   timeout=120, check=False)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.decode("utf-8", "replace"))
    result = json.loads(completed.stdout)
    if "crash" in result:
        raise AssertionError(result["crash"])
    return result


#: Today's Quick Skin ``site.js`` output for the same inputs (``Minecraft <version>`` keys): the
#: destination cards, each release card's text and accessible name, and the status line. The one
#: intentional v1 change is the "Verified E2E" card, whose paired-evidence sentence now names the
#: configured family titles (QS: "…every supported version, including paired optional-mod
#: compatibility evidence.").
QS_REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
QS_URL = f"https://github.com/{QS_REPOSITORY}"
QS_GALLERY_CARD = ("Verified E2E", "Browse packaged-Minecraft screenshots across every supported version. "
                   "Includes paired evidence: Mod compatibility.")


def qs_release_card(version: str, loaders: list[str], captures: int, short_sha: str) -> dict[str, str]:
    """A release card exactly as Quick Skin's ``site.js`` renders it today."""

    return {"text": f"{version}Verified{captures} validated captures{''.join(loaders)}commit {short_sha}",
            "label": f"Minecraft {version}, Verified, {' and '.join(loaders)}, {captures} validated captures, "
                     f"commit {short_sha}; open packaged E2E run on GitHub Actions"}


class FrontEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-frontend-")).resolve()
        cls.publication = bs.Publication(cls.directory / "qs")
        cls.qs = cls.directory / "qs-site"
        bs.render(cls.publication.invocation(), cls.qs, kit_root=cls.publication.kit,
                  bundles=cls.publication.bundles(), families=cls.publication.families())
        cls.repository_url = f"https://github.com/{cls.publication.mod.repository}"
        bp_mod, bp_bundle, bp_kit = bs.bp_bundle(cls.directory / "bp")
        cls.bp_mod = bp_mod
        cls.bp = cls.directory / "bp-site"
        from mod_base.runtime import build_invocation
        from tests.fixtures.mods import support

        bs.render(build_invocation(bp_mod.root, None, support.pages_environment(bp_mod, job="build"), root=bp_kit),
                  cls.bp, kit_root=bp_kit, bundles=[bp_bundle], families=[])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def test_the_landing_page_renders_site_data(self) -> None:
        result = run_page(self.qs, "landing")
        self.assertIsNone(result["error"], result["status"])
        self.assertEqual(result["status"], "2 versions · 12 validated captures · provenance linked to GitHub Actions")
        self.assertEqual(result["hero"], ["Change your look.", "<BR>", "<SPAN>Stay in the game."])
        self.assertEqual(result["description"], bs.DESCRIPTION)
        site = json.loads((self.qs / "site-data.json").read_bytes())
        self.assertEqual(result["lead"], site["copy"]["evidence_lead"])
        self.assertEqual(result["principles"], site["copy"]["principles"])
        self.assertEqual([card[0] for card in result["cards"]], ["Modrinth", "CurseForge", "GitHub", "Verified E2E"])
        self.assertEqual(result["cards"][2][2], self.repository_url)
        self.assertEqual(result["cards"][3][2], "https://the-plum-team.github.io/qs-like/e2e/")
        self.assertIn("Includes paired evidence: Mod compatibility.", result["cards"][3][1])
        self.assertEqual(len(result["releases"]), 2)
        self.assertTrue(all(release["href"].startswith(f"{self.repository_url}/actions/runs/")
                            for release in result["releases"]))
        self.assertTrue(result["releases"][0]["label"].startswith("Minecraft 1.20.1, Verified, Fabric and Forge"))

    def test_the_quick_skin_golden_script_text(self) -> None:
        """What ``site.js`` renders on the Quick Skin fixture site equals today's Quick Skin output."""

        site = self.directory / "golden-site"
        if not site.exists():
            bs.render(self.publication.invocation(
                GITHUB_REPOSITORY=QS_REPOSITORY,
                GITHUB_WORKFLOW_REF=f"{QS_REPOSITORY}/.github/workflows/pages.yml@refs/heads/master"),
                site, kit_root=self.publication.kit, bundles=self.publication.bundles(),
                families=self.publication.families())
        result = run_page(site, "landing")
        self.assertIsNone(result["error"], result["status"])
        data = json.loads((site / "site-data.json").read_bytes())
        links = [(link["title"], link["description"], link["url"]) for link in data["project"]["links"]]
        self.assertEqual([tuple(card) for card in result["cards"]], [
            *links, ("GitHub", "Source, releases and issue tracking.", QS_URL),
            (*QS_GALLERY_CARD, "https://the-plum-team.github.io/qs-like/e2e/")])
        expected = [qs_release_card(release["minecraft"][0], release["loader_names"], release["frame_count"],
                                    release["short_sha"]) for release in data["releases"]]
        self.assertEqual([{"text": card["text"], "label": card["label"]} for card in result["releases"]], expected)
        self.assertEqual([card["href"] for card in result["releases"]],
                         [release["tested_run_url"] for release in data["releases"]])
        self.assertEqual(result["status"], "2 versions · 12 validated captures · provenance linked to GitHub Actions")
        self.assertEqual(result["hero"], ["Change your look.", "<BR>", "<SPAN>Stay in the game."])

    def test_the_gallery_renders_every_view_from_gallery_data(self) -> None:
        result = run_page(self.qs, "gallery")
        self.assertIsNone(result["error"], result["status"])
        gallery = json.loads((self.qs / "e2e" / "gallery-data.json").read_bytes())
        self.assertEqual(result["lead"], gallery["copy"]["gallery_lead"])
        self.assertEqual(result["placeholder"], "Cape, skin, title screen…")
        self.assertEqual(result["methodology"], gallery["copy"]["methodology"])
        self.assertEqual(result["summary"][:3], ["2 releases", "12 validated captures",
                                                 "1 published Mod compatibility lane"])
        self.assertEqual(result["tablist"], "Minecraft version")
        self.assertEqual(result["tabs"], [["Minecraft 1.20.1", "true", 0], ["Minecraft 26.3", "false", -1],
                                          ["All releases", "false", -1]])
        self.assertEqual(result["minecraft"], ["all", "1.20.1"])
        self.assertEqual(result["loaders"], ["All loaders", "Fabric", "Forge"])
        self.assertIn("Full suite", result["scenarios"])
        self.assertEqual(result["cards"], [8, 0, 0])
        self.assertEqual(result["afterArrow"], ["false", "true", "false"])
        self.assertTrue(result["focused"])
        self.assertEqual(result["allCards"], 12)
        self.assertEqual(result["allMinecraft"], ["all", "1.20.1", "26.3"])
        self.assertEqual(result["filteredCards"], 4)
        self.assertEqual(result["filteredStatus"], "4 validated captures shown")
        self.assertEqual(result["epochs"], [])

    def test_a_capture_opens_its_nine_section_record_with_the_kit_fact(self) -> None:
        result = run_page(self.qs, "gallery")
        self.assertTrue(result["dialogOpen"])
        self.assertEqual(result["sections"], ["HEADER", "FIGURE", "Checkpoint contract", "Deterministic assertion",
                                              "Image integrity", "Pixel comparisons", "Packaged lane", "Provenance",
                                              "DETAILS"])
        self.assertEqual(len(result["dialogTitle"]), 1)
        for term in ("Contract SHA-256", "Contract source", "Mod JAR SHA-256", "Tested run", "Tested commit", "Kit",
                     "Pixel metrics version", "File SHA-256", "Changed pixels"):
            self.assertIn(term, result["terms"])
        self.assertIn(f"https://github.com/The-Plum-Team/mod-base/commit/{bs.support.KIT_SHA}", result["dialogLinks"])
        self.assertTrue(any(link.startswith(f"{self.repository_url}/blob/") for link in result["dialogLinks"]))
        self.assertEqual(result["raw"], [["frame", "lane", "comparisons"]])
        self.assertTrue(result["dialogClosed"])

    def test_the_compare_grid_is_keyed_by_release_minecraft_and_loader(self) -> None:
        result = run_page(self.qs, "gallery")
        self.assertTrue(result["compareVisible"])
        self.assertEqual(result["comparePressed"], "true")
        self.assertEqual(result["cells"], ["Minecraft 1.20.1 · Fabric", "Minecraft 1.20.1 · Forge",
                                           "Minecraft 26.3 · Fabric"])
        self.assertEqual(result["cellCards"], 3)
        self.assertEqual(result["notApplicable"],
                         ["Minecraft 26.3 · ForgeNot applicable — Forge is not part of this release."])

    def test_the_family_view_renders_verdict_badges_and_run_links(self) -> None:
        result = run_page(self.qs, "gallery")
        self.assertEqual(result["views"], [["gallery-view-button", "Gallery"],
                                           ["compare-view-button", "Compare versions"],
                                           ["family-view-mod-compatibility", "Mod compatibility"]])
        family = result["families"][0]
        self.assertEqual(family["pressed"], "true")
        self.assertTrue(family["visible"])
        self.assertEqual(family["notes"][0],
                         "Clean reference versus mod-installed pairs, published only after a complete clean review.")
        self.assertEqual(family["lanes"], 1)
        self.assertEqual(family["badges"], ["Runtime passed", "AI clean"])
        self.assertEqual(family["reviewed"], ["2 reviewed frames"])
        self.assertEqual(family["links"], [["Compatibility runtime ↗", f"{self.repository_url}/actions/runs/303"]])
        self.assertEqual(family["notApplicable"],
                         ["Minecraft 1.20.1 · Forge · Ears: "
                          "Ears publishes no Forge build for Minecraft 1.20.1."])
        self.assertTrue(all("/e2e/families/mod-compatibility/images/" in image for image in family["images"]))
        self.assertEqual(family["status"], "1 published Mod compatibility lane shown")

    def test_a_mod_without_families_gets_no_family_ui_and_its_own_labels(self) -> None:
        landing = run_page(self.bp, "landing")
        self.assertIsNone(landing["error"], landing["status"])
        self.assertEqual([card[0] for card in landing["cards"]], ["GitHub", "Verified E2E"])
        self.assertNotIn("Includes paired evidence", landing["cards"][1][1])
        # A branch-labelled key keeps its label as the heading; its Minecraft version becomes a badge.
        site = json.loads((self.bp / "site-data.json").read_bytes())
        release = site["releases"][0]
        self.assertEqual(landing["releases"][0]["text"],
                         f"masterVerified{release['frame_count']} validated capturesMinecraft 1.21.1"
                         f"{''.join(release['loader_names'])}commit {release['short_sha']}")
        self.assertEqual(landing["status"],
                         f"1 version · {release['frame_count']} validated captures · provenance linked to GitHub Actions")
        self.assertEqual(landing["hero"], ["Collect, display and play with pop figures."])
        gallery = run_page(self.bp, "gallery")
        self.assertIsNone(gallery["error"], gallery["status"])
        self.assertEqual([view[0] for view in gallery["views"]], ["gallery-view-button", "compare-view-button"])
        self.assertEqual(gallery["families"], [])
        self.assertEqual(gallery["tablist"], "Branch")
        self.assertEqual(gallery["tabs"][0][0], "master")
        self.assertEqual(gallery["placeholder"], "Box, claw machine, figure…")
        self.assertEqual(len(gallery["summary"]), 3)

    def test_the_gates_reject_another_kind_or_schema(self) -> None:
        gallery = json.loads((self.qs / "e2e" / "gallery-data.json").read_bytes())
        site = json.loads((self.qs / "site-data.json").read_bytes())
        cases = {
            "gallery": [dict(gallery, kind="mod-base.site"), dict(gallery, schema_version=2), dict(gallery, frames=[]),
                        dict(gallery, families=[{**gallery["families"][0], "available": "yes"}]),
                        {key: value for key, value in gallery.items() if key != "comparisons"}],
            "landing": [dict(site, kind="mod-base.gallery"), dict(site, schema_version=3),
                        {key: value for key, value in site.items() if key != "families"}],
        }
        for kind, documents in cases.items():
            for position, document in enumerate(documents):
                with self.subTest(kind=kind, case=position), tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "data.json"
                    path.write_text(json.dumps(document), encoding="utf-8")
                    result = run_page(self.qs, kind, data=path)
                    self.assertEqual(result["error"], "true")
                    self.assertIn("could not be loaded", result["status"])

    def test_hostile_text_stays_text(self) -> None:
        gallery = json.loads((self.qs / "e2e" / "gallery-data.json").read_bytes())
        payload = "<img src=x onerror=alert(1)>"
        gallery["frames"][0]["title"] = payload
        gallery["copy"]["gallery_lead"] = payload
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "data.json"
            path.write_text(json.dumps(gallery), encoding="utf-8")
            result = run_page(self.qs, "gallery", data=path)
        self.assertIsNone(result["error"], result["status"])
        self.assertEqual(result["lead"], payload)

    def test_the_dom_stub_refuses_an_unguarded_url(self) -> None:
        """The harness itself fails closed: a link assigned without its guard never renders."""

        for script, guarded, unguarded in (("site.js", "link.href = safeHref(href);", "link.href = href;"),
                                          ("gallery.js", "image.src = sameOriginPath(frame.image);",
                                           "image.src = frame.image;")):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                site = Path(directory) / "site"
                shutil.copytree(self.qs, site)
                path = site / "assets" / script
                source = path.read_text(encoding="utf-8")
                self.assertIn(guarded, source)
                path.write_text(source.replace(guarded, unguarded, 1), encoding="utf-8")
                result = run_page(site, "landing" if script == "site.js" else "gallery")
                self.assertEqual(result["error"], "true")
                self.assertIn("was not normalized by a guard", result["status"])

    def test_a_foreign_provenance_link_fails_closed(self) -> None:
        gallery = json.loads((self.qs / "e2e" / "gallery-data.json").read_bytes())
        for link in ("https://evil.example/actions/runs/1", f"{self.repository_url}/actions/runs/1/../../x",
                     "javascript:alert(1)"):
            gallery["families"][0]["releases"][0]["links"][0]["run_url"] = link
            with self.subTest(link=link), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "data.json"
                path.write_text(json.dumps(gallery), encoding="utf-8")
                with self.assertRaises(AssertionError) as caught:
                    run_page(self.qs, "gallery", data=path)
                self.assertIn("not a run of this repository", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
