"use strict";

// Gallery renderer for mod-base.gallery v1 (e2e/gallery-data.json). Every string reaches the page
// through textContent; images must be same-origin paths and every provenance link must be an https
// run URL of this repository.

const PERCENT = new Intl.NumberFormat(undefined, {
  style: "percent",
  maximumSignificantDigits: 3
});
const KIT_REPOSITORY_URL = "https://github.com/The-Plum-Team/mod-base";
const COMMIT = /^[0-9a-f]{40}$/;

function node(tag, className, text) {
  const value = document.createElement(tag);
  if (className) value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
}

function sameOriginPath(value) {
  const url = new URL(value, window.location.href);
  if (url.origin !== window.location.origin) throw new Error("Cross-origin image path rejected");
  return url.href;
}

function httpsLink(value) {
  const url = new URL(value);
  if (url.protocol !== "https:") throw new Error("Non-HTTPS provenance link rejected");
  return url.href;
}

function externalLink(href, text) {
  const link = node("a", "", text);
  link.href = httpsLink(href);
  link.target = "_blank";
  link.rel = "noopener";
  return link;
}

function option(select, value, label) {
  const item = document.createElement("option");
  item.value = value;
  item.textContent = label;
  select.append(item);
}

function versionOrder(left, right) {
  return left.localeCompare(right, undefined, { numeric: true });
}

function unique(values) {
  return [...new Set(values)].sort(versionOrder);
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function percent(value) {
  return typeof value === "number" && Number.isFinite(value) ? PERCENT.format(value) : "—";
}

function seconds(value) {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(1)} s` : "—";
}

function regionLabel(region) {
  if (!Array.isArray(region) || region.length !== 4) return "the whole frame";
  const [left, top, right, bottom] = region;
  return `x ${percent(left)}–${percent(right)}, y ${percent(top)}–${percent(bottom)} of the frame`;
}

function factList(rows) {
  const list = node("dl", "fact-list");
  for (const [term, value, className] of rows) {
    if (value === undefined || value === null || value === "") continue;
    const definition = node("dd", className);
    if (value instanceof Node) definition.append(value);
    else definition.textContent = String(value);
    list.append(node("dt", "", term), definition);
  }
  return list;
}

function recordSection(title, lead, ...children) {
  const section = node("section", "record-section");
  section.append(node("h3", "", title));
  if (lead) section.append(node("p", "record-lead", lead));
  section.append(...children);
  return section;
}

function pixelFacts(metrics) {
  if (!metrics) return factList([["Measurements", "not published for this image"]]);
  return factList([
    ["Decoded size", `${metrics.width} × ${metrics.height}`],
    ["File SHA-256", node("span", "mono", metrics.file_sha256)],
    ["Pixel SHA-256", node("span", "mono", metrics.pixel_sha256)],
    ["Luma entropy", metrics.luma_entropy],
    ["Distinct meaningful colours", metrics.meaningful_colors],
    ["Near-black pixels", percent(metrics.dark_fraction)],
    ["Near-white pixels", percent(metrics.light_fraction)]
  ]);
}

function comparisonFacts(metrics) {
  return factList([
    ["Changed pixels", percent(metrics.changed_fraction)],
    ["Required minimum", percent(metrics.required_changed_fraction)],
    ["RMS difference", metrics.rms_difference],
    ["Measured region", regionLabel(metrics.region)]
  ]);
}

function epochLabel(epoch) {
  return { baseline: "Reused from baseline", selected: "Selected update" }[epoch] || "";
}

function isGalleryInventory(data) {
  return Boolean(data)
    && data.kind === "mod-base.gallery"
    && data.schema_version === 1
    && Array.isArray(data.frames)
    && data.frames.length > 0
    && Array.isArray(data.releases)
    && Array.isArray(data.lanes)
    && Array.isArray(data.comparisons)
    && Array.isArray(data.families)
    && data.families.every((family) => Boolean(family)
      && typeof family.available === "boolean"
      && Array.isArray(family.releases)
      && Array.isArray(family.lanes)
      && Array.isArray(family.not_applicable));
}

class Gallery {
  constructor(data) {
    this.data = data;
    this.labels = data.labels;
    this.activeTab = 0;
    this.tabs = data.releases.map((release, index) => ({
      id: `release-${index}`,
      label: release.label,
      key: release.key
    })).concat([{ id: "all", label: "All releases", key: null }]);
    this.tabButtons = [];
    this.panels = [];
    this.familyViews = [];
    this.dialog = document.querySelector("#capture-dialog");
    this.openCapture = (frame) => this.showRecord(frame);
    this.runPrefix = `${data.project.repository_url}/actions/runs/`;
    this.releaseByKey = new Map(data.releases.map((release) => [release.key, release]));
    this.laneById = new Map(data.lanes.map((lane) => [this.identity(lane.key, lane.lane_id), lane]));
    this.frameById = new Map(data.frames.map((frame) => [this.identity(frame.key, frame.frame_id), frame]));
    this.comparisonsByFrame = new Map();
    for (const comparison of data.comparisons) {
      for (const frameId of [comparison.first_frame_id, comparison.second_frame_id]) {
        const identity = this.identity(comparison.key, frameId);
        if (!this.comparisonsByFrame.has(identity)) this.comparisonsByFrame.set(identity, []);
        this.comparisonsByFrame.get(identity).push(comparison);
      }
    }
  }

  identity(key, id) {
    return `${key}\u0000${id}`;
  }

  scenarioLabel(value) {
    return this.labels.scenarios[value] || value;
  }

  roleLabel(value) {
    return this.labels.roles[value] || value;
  }

  tierLabel(value) {
    return this.labels.tiers[value] || value;
  }

  loaderLabel(value) {
    return this.labels.loaders[value] || value;
  }

  releaseLabel(key) {
    const release = this.releaseByKey.get(key);
    return release ? release.label : key;
  }

  scopeLabel(key, minecraft) {
    // "Minecraft 1.20.1" for a key labelled by its version, "master · Minecraft 1.21.1" otherwise.
    const label = this.releaseLabel(key);
    return label.includes(minecraft) ? label : `${label} · Minecraft ${minecraft}`;
  }

  runLink(href, text) {
    // Provenance links are exactly run URLs of this repository (never a bundle-supplied page).
    if (typeof href !== "string" || !href.startsWith(this.runPrefix) || !/^[1-9][0-9]*$/.test(href.slice(this.runPrefix.length))) {
      throw new Error("Provenance link is not a run of this repository");
    }
    return externalLink(href, text);
  }

  start() {
    document.querySelector("#gallery-lead").textContent = this.data.copy.gallery_lead;
    document.querySelector("#capture-search").placeholder = this.labels.search_placeholder;
    const methodology = document.querySelector("#methodology-copy");
    for (const paragraph of this.data.copy.methodology) methodology.append(node("p", "", paragraph));
    this.renderSummary();
    this.populateFilters();
    this.createTabs();
    this.createFamilyViews();
    this.bindFilters();
    this.bindViewSwitch();
    this.bindDialog();
    this.renderComparison();
    this.renderGallery();
  }

  renderSummary() {
    const summary = document.querySelector("#release-summary");
    summary.append(node("span", "summary-item", plural(this.data.releases.length, "release")));
    summary.append(node("span", "summary-item", `${this.data.frames.length} validated captures`));
    for (const family of this.data.families) {
      summary.append(node("span", "summary-item", `${plural(family.lanes.length, `published ${family.title} lane`)}`));
    }
    for (const release of this.data.releases) {
      const item = node("span", "summary-item");
      const strong = document.createElement("strong");
      strong.textContent = release.label;
      item.append(strong, document.createTextNode(` · ${release.loader_names.join(" + ")}`));
      summary.append(item);
    }
  }

  populateFilters() {
    const frames = this.data.frames;
    for (const loader of unique(frames.map((frame) => frame.loader))) {
      const label = frames.find((frame) => frame.loader === loader).loader_name;
      option(document.querySelector("#loader-filter"), loader, label);
      option(document.querySelector("#compare-loader"), loader, label);
    }
    for (const scenario of unique(frames.map((frame) => frame.scenario))) {
      option(document.querySelector("#scenario-filter"), scenario, this.scenarioLabel(scenario));
    }
    for (const role of unique(frames.map((frame) => frame.role))) {
      option(document.querySelector("#role-filter"), role, this.roleLabel(role));
    }
    this.populateMinecraftFilter();

    const captures = new Map();
    for (const frame of frames) {
      if (!captures.has(frame.capture_id)) {
        captures.set(frame.capture_id, `${frame.title} · ${this.scenarioLabel(frame.scenario)} · ${this.roleLabel(frame.role)}`);
      }
    }
    const compare = document.querySelector("#capture-compare");
    for (const [captureId, label] of [...captures].sort((left, right) => left[1].localeCompare(right[1]))) {
      option(compare, captureId, label);
    }
  }

  populateMinecraftFilter() {
    // The Minecraft filter lists the versions of the active release tab (every version for "All").
    const select = document.querySelector("#minecraft-filter");
    const previous = select.value;
    const tab = this.tabs[this.activeTab];
    const frames = this.data.frames.filter((frame) => !tab.key || frame.key === tab.key);
    const versions = unique(frames.map((frame) => frame.minecraft));
    select.replaceChildren();
    option(select, "all", "All versions");
    for (const version of versions) option(select, version, `Minecraft ${version}`);
    select.value = versions.includes(previous) ? previous : "all";
  }

  createTabs() {
    const tablist = document.querySelector("#release-tabs");
    const panels = document.querySelector("#release-panels");
    tablist.setAttribute("aria-label", this.labels.release_prefix === "Minecraft" ? "Minecraft version" : this.labels.release_prefix);
    this.tabs.forEach((tab, index) => {
      const button = node("button", "version-tab", tab.label);
      button.type = "button";
      button.id = `tab-${tab.id}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-controls", `panel-${tab.id}`);
      button.setAttribute("aria-selected", index === 0 ? "true" : "false");
      button.tabIndex = index === 0 ? 0 : -1;
      button.addEventListener("click", () => this.activateTab(index, false));
      button.addEventListener("keydown", (event) => this.handleTabKey(event, index));
      tablist.append(button);
      this.tabButtons.push(button);

      const panel = node("section", "version-panel");
      panel.id = `panel-${tab.id}`;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", button.id);
      panel.tabIndex = 0;
      panel.hidden = index !== 0;
      panels.append(panel);
      this.panels.push(panel);
    });
  }

  handleTabKey(event, index) {
    let target = null;
    if (event.key === "ArrowRight") target = (index + 1) % this.tabs.length;
    if (event.key === "ArrowLeft") target = (index - 1 + this.tabs.length) % this.tabs.length;
    if (event.key === "Home") target = 0;
    if (event.key === "End") target = this.tabs.length - 1;
    if (target !== null) {
      event.preventDefault();
      this.activateTab(target, true);
    }
  }

  activateTab(index, focus) {
    this.activeTab = index;
    this.tabButtons.forEach((button, buttonIndex) => {
      const active = buttonIndex === index;
      button.setAttribute("aria-selected", active ? "true" : "false");
      button.tabIndex = active ? 0 : -1;
      this.panels[buttonIndex].hidden = !active;
    });
    if (focus) this.tabButtons[index].focus();
    this.populateMinecraftFilter();
    this.renderGallery();
  }

  bindFilters() {
    document.querySelector("#gallery-filters").addEventListener("submit", (event) => event.preventDefault());
    for (const selector of ["#minecraft-filter", "#loader-filter", "#scenario-filter", "#role-filter"]) {
      document.querySelector(selector).addEventListener("change", () => this.renderGallery());
    }
    document.querySelector("#capture-search").addEventListener("input", () => this.renderGallery());
    document.querySelector("#capture-compare").addEventListener("change", () => this.renderComparison());
    document.querySelector("#compare-loader").addEventListener("change", () => this.renderComparison());
  }

  bindViewSwitch() {
    const views = [
      ["gallery", document.querySelector("#gallery-view-button"), document.querySelector("#gallery-view"), () => this.renderGallery()],
      ["compare", document.querySelector("#compare-view-button"), document.querySelector("#compare-view"), () => this.renderComparison()]
    ];
    for (const family of this.familyViews) {
      views.push([family.id, family.button, family.view, () => this.renderFamily(family)]);
    }
    const setView = (selected) => {
      for (const [name, button, view, render] of views) {
        const active = name === selected;
        view.hidden = !active;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", active ? "true" : "false");
        if (active) render();
      }
    };
    for (const [name, button] of views) button.addEventListener("click", () => setView(name));
  }

  bindDialog() {
    this.dialog.addEventListener("click", (event) => {
      if (event.target === this.dialog) this.dialog.close();
    });
    this.dialog.addEventListener("close", () => {
      document.querySelector("#capture-dialog-body").replaceChildren();
    });
  }

  filteredFrames() {
    const tab = this.tabs[this.activeTab];
    const minecraft = document.querySelector("#minecraft-filter").value;
    const loader = document.querySelector("#loader-filter").value;
    const scenario = document.querySelector("#scenario-filter").value;
    const role = document.querySelector("#role-filter").value;
    const search = document.querySelector("#capture-search").value.trim().toLocaleLowerCase();
    return this.data.frames.filter((frame) => {
      if (tab.key && frame.key !== tab.key) return false;
      if (minecraft !== "all" && frame.minecraft !== minecraft) return false;
      if (loader !== "all" && frame.loader !== loader) return false;
      if (scenario !== "all" && frame.scenario !== scenario) return false;
      if (role !== "all" && frame.role !== role) return false;
      if (search && !`${frame.title} ${frame.expectation} ${frame.capture_id}`.toLocaleLowerCase().includes(search)) return false;
      return true;
    });
  }

  captureCard(frame) {
    const figure = node("figure", "capture-card");
    const trigger = node("button", "capture-open");
    trigger.type = "button";
    trigger.setAttribute(
      "aria-label",
      `Open the validation record for ${frame.title}, ${this.scopeLabel(frame.key, frame.minecraft)}, ${frame.loader_name}, ${this.roleLabel(frame.role)}`
    );
    const image = document.createElement("img");
    image.src = sameOriginPath(frame.image);
    image.alt = frame.alt;
    image.width = frame.width;
    image.height = frame.height;
    image.loading = "lazy";
    image.decoding = "async";
    trigger.append(image);
    trigger.addEventListener("click", () => this.openCapture(frame));

    const caption = document.createElement("figcaption");
    const titleRow = node("div", "capture-title-row");
    titleRow.append(node("h3", "", frame.title), node("span", "verified-badge", "Passed"));
    const metadata = node("div", "capture-meta");
    for (const text of [frame.minecraft, frame.loader_name, this.scenarioLabel(frame.scenario), this.roleLabel(frame.role)]) {
      metadata.append(node("span", "", text));
    }
    if (frame.epoch) metadata.append(node("span", "epoch-badge", epochLabel(frame.epoch)));
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "What this validates";
    details.append(summary, node("p", "", frame.expectation));
    const record = node("button", "record-button", "Full validation record");
    record.type = "button";
    record.addEventListener("click", () => this.openCapture(frame));
    const provenance = node("div", "provenance-line");
    const origin = frame.provenance;
    if (origin.tested_run_url === origin.handoff_run_url) {
      provenance.append(
        this.runLink(origin.handoff_run_url, "tested & publishing run ↗"),
        node("span", "", origin.handoff_commit.slice(0, 12))
      );
    } else {
      provenance.append(
        this.runLink(origin.tested_run_url, "tested run ↗"),
        node("span", "", origin.tested_commit.slice(0, 12)),
        this.runLink(origin.handoff_run_url, "publishing run ↗"),
        node("span", "", origin.handoff_commit.slice(0, 12))
      );
    }
    caption.append(titleRow, metadata, details, record, provenance);
    figure.append(trigger, caption);
    return figure;
  }

  renderGallery() {
    const panel = this.panels[this.activeTab];
    panel.replaceChildren();
    const frames = this.filteredFrames();
    if (!frames.length) {
      panel.append(node("div", "empty-state", "No validated captures match these filters."));
    } else {
      const grid = node("div", "capture-grid");
      for (const frame of frames) grid.append(this.captureCard(frame));
      panel.append(grid);
    }
    document.querySelector("#gallery-status").textContent = `${plural(frames.length, "validated capture")} shown`;
  }

  renderComparison() {
    const captureId = document.querySelector("#capture-compare").value;
    const selectedLoader = document.querySelector("#compare-loader").value;
    const grid = document.querySelector("#comparison-grid");
    grid.replaceChildren();
    if (!captureId) return;
    let cells = 0;
    for (const release of this.data.releases) {
      const loaders = selectedLoader === "all" ? release.loaders : [selectedLoader];
      for (const minecraft of release.minecraft) {
        for (const loader of loaders) {
          const index = release.loaders.indexOf(loader);
          const loaderName = index >= 0 ? release.loader_names[index] : this.loaderLabel(loader);
          const scope = release.minecraft.length > 1 ? `${release.label} · ${minecraft}` : release.label;
          const label = `${scope} · ${loaderName}`;
          const cell = node("section", "comparison-cell");
          const cellLabel = node("span", "compare-column-label", label);
          cellLabel.id = `compare-cell-${cells}`;
          cell.setAttribute("aria-labelledby", cellLabel.id);
          cell.append(cellLabel);
          cells += 1;
          grid.append(cell);
          if (index < 0) {
            cell.append(this.messageCard(label, `Not applicable — ${loaderName} is not part of this release.`));
            continue;
          }
          const frame = this.data.frames.find((item) => item.key === release.key && item.minecraft === minecraft
            && item.loader === loader && item.capture_id === captureId);
          cell.append(frame ? this.captureCard(frame)
            : this.messageCard(label, "No validated capture was published for this exact cell."));
        }
      }
    }
    document.querySelector("#gallery-status").textContent = `${plural(cells, "release/loader cell")} aligned by semantic checkpoint`;
  }

  messageCard(label, text) {
    const card = node("div", "missing-card");
    const copy = node("div");
    copy.append(node("h3", "", label), node("p", "", text));
    card.append(copy);
    return card;
  }

  // -- Families: one generic paired view per configured family ---------------------------------

  createFamilyViews() {
    const container = document.querySelector("#family-views");
    if (!container || !this.data.families.length) return;
    const switcher = document.querySelector("#view-switch");
    for (const family of this.data.families) {
      const id = `family-view-${family.family}`;
      const button = node("button", "view-button", family.title);
      button.type = "button";
      button.id = id;
      button.setAttribute("aria-pressed", "false");
      switcher.append(button);

      const view = node("div");
      view.id = `${id}-panel`;
      view.hidden = true;
      view.setAttribute("aria-labelledby", id);
      view.append(node("p", "compare-note", family.description));
      const note = this.data.copy.family_notes[family.family];
      if (note) view.append(node("p", "compare-note", note));
      const form = node("form", "family-controls");
      form.setAttribute("aria-label", `Filter ${family.title} evidence`);
      form.addEventListener("submit", (event) => event.preventDefault());
      const filters = {};
      for (const [name, label, all] of [["release", "Release", "All releases"], ["loader", "Loader", "All loaders"],
        ["variant", "Variant", "All variants"]]) {
        const field = node("label", "", label);
        const select = document.createElement("select");
        option(select, "all", all);
        select.addEventListener("change", () => this.renderFamily(entry));
        field.append(select);
        form.append(field);
        filters[name] = select;
      }
      const grid = node("div", "family-grid");
      const notApplicable = node("div", "family-na");
      view.append(form, grid, notApplicable);
      container.append(view);
      const entry = { id, family, button, view, filters, grid, notApplicable };
      this.populateFamilyFilters(entry);
      this.familyViews.push(entry);
    }
  }

  populateFamilyFilters(entry) {
    const rows = entry.family.lanes.map((lane) => ({ key: lane.key, loader: lane.loader, variant: lane.variant.id,
      variantName: lane.variant.name }))
      .concat(entry.family.not_applicable.map((row) => ({ key: row.key, loader: row.loader, variant: row.variant_id,
        variantName: row.variant_name })));
    for (const release of this.data.releases) {
      if (rows.some((row) => row.key === release.key)) option(entry.filters.release, release.key, release.label);
    }
    for (const loader of unique(rows.map((row) => row.loader))) option(entry.filters.loader, loader, this.loaderLabel(loader));
    const variants = new Map(rows.map((row) => [row.variant, row.variantName]));
    for (const [variant, name] of [...variants].sort((left, right) => left[1].localeCompare(right[1]))) {
      option(entry.filters.variant, variant, name);
    }
  }

  familyMatches(entry, row, variant) {
    const release = entry.filters.release.value;
    const loader = entry.filters.loader.value;
    const selected = entry.filters.variant.value;
    return (release === "all" || row.key === release)
      && (loader === "all" || row.loader === loader)
      && (selected === "all" || variant === selected);
  }

  renderFamily(entry) {
    const family = entry.family;
    entry.grid.replaceChildren();
    entry.notApplicable.replaceChildren();
    const lanes = family.lanes.filter((lane) => this.familyMatches(entry, lane, lane.variant.id));
    const notApplicable = family.not_applicable.filter((row) => this.familyMatches(entry, row, row.variant_id));
    if (!lanes.length) {
      entry.grid.append(node("div", "empty-state", family.available
        ? `No published ${family.title} lane matches these filters.`
        : `No ${family.title} evidence has been published yet.`));
    } else {
      for (const lane of lanes) entry.grid.append(this.familyLaneCard(family, lane));
    }
    if (notApplicable.length) {
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `${plural(notApplicable.length, "explicitly not-applicable combination")}`;
      const list = node("ul", "family-na-list");
      for (const row of notApplicable) {
        list.append(node("li", "",
          `${this.scopeLabel(row.key, row.minecraft)} · ${this.loaderLabel(row.loader)} · ${row.variant_name}: ${row.reason}`));
      }
      details.append(summary, list);
      entry.notApplicable.append(details);
    }
    document.querySelector("#gallery-status").textContent = `${plural(lanes.length, `published ${family.title} lane`)} shown`;
  }

  familyShot(record, label, alt) {
    const figure = node("figure", "family-shot");
    const image = document.createElement("img");
    image.src = sameOriginPath(record.image.path);
    image.alt = alt;
    image.width = record.image.width;
    image.height = record.image.height;
    image.loading = "lazy";
    image.decoding = "async";
    const caption = document.createElement("figcaption");
    caption.append(node("strong", "", label));
    const imageLink = node("a", "", "Open image ↗");
    imageLink.href = sameOriginPath(record.image.path);
    imageLink.target = "_blank";
    imageLink.rel = "noopener";
    caption.append(imageLink);
    figure.append(image, caption);
    return figure;
  }

  familyCheckpoint(pair, lane) {
    const checkpoint = node("article", "family-checkpoint");
    const heading = node("div", "family-checkpoint-heading");
    heading.append(node("h4", "", pair.title));
    const badges = node("div", "family-badges");
    const verdict = pair.verdict;
    if (verdict.runtime_passed) badges.append(node("span", "verified-badge", "Runtime passed"));
    if (verdict.semantic_valid && !verdict.defect) badges.append(node("span", "verified-badge", "AI clean"));
    if (verdict.matches_reference === true) badges.append(node("span", "verified-badge", "Matches reference"));
    heading.append(badges);

    const where = `Minecraft ${lane.minecraft}, ${this.loaderLabel(lane.loader)}`;
    const pairView = node("div", "family-pair");
    pairView.append(
      this.familyShot(pair.reference, "Clean reference", `${pair.title} clean reference for ${where}`),
      this.familyShot(pair.candidate, `${lane.variant.name} installed`,
        `${pair.title} with ${lane.variant.name} ${lane.variant.version} installed on ${where}`)
    );

    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = "Validation details";
    details.append(
      summary,
      node("p", "record-prose", pair.expectation),
      factList([
        ["Deterministic assertion", node("span", "mono", pair.runtime_evidence)],
        ["Semantic pixels changed", percent(pair.metrics.semantic_changed_fraction)],
        ["Perceptual delta", pair.metrics.perceptual_delta],
        ["Candidate semantic SHA-256", node("span", "mono", pair.metrics.candidate_semantic_sha256)],
        ["Reference semantic SHA-256", node("span", "mono", pair.metrics.reference_semantic_sha256)]
      ])
    );
    checkpoint.append(heading, pairView, details);
    return checkpoint;
  }

  familyLaneCard(family, lane) {
    const card = node("section", "family-lane");
    const header = node("header", "family-lane-heading");
    const title = node("div");
    title.append(
      node("p", "eyebrow", `${this.scopeLabel(lane.key, lane.minecraft)} · ${this.loaderLabel(lane.loader)}`),
      node("h3", "", `${lane.variant.name} ${lane.variant.version}`)
    );
    header.append(title, node("span", "review-count", plural(lane.review.reviewed_frame_count, "reviewed frame")));

    const metadata = node("div", "capture-meta");
    for (const value of [lane.artifact_node, lane.variant.version_id, lane.lane_id]) {
      metadata.append(node("span", "mono", value));
    }
    const checkpoints = node("div", "family-checkpoints");
    for (const pair of lane.pairs) checkpoints.append(this.familyCheckpoint(pair, lane));

    const proof = document.createElement("details");
    proof.className = "family-proof";
    const proofSummary = document.createElement("summary");
    proofSummary.textContent = "Authenticated review identity";
    proof.append(
      proofSummary,
      factList([
        ["Review manifest SHA-256", node("span", "mono", lane.review.manifest_sha256)],
        ["Curation proof SHA-256", node("span", "mono", lane.review.proof_sha256)],
        ["Normalized report SHA-256", node("span", "mono", lane.review.report_sha256)]
      ])
    );

    const provenance = node("div", "family-provenance");
    const release = family.releases.find((item) => item.key === lane.key);
    for (const link of (release && release.links) || []) {
      provenance.append(this.runLink(link.run_url, `${link.label} ↗`));
    }
    card.append(header, metadata, checkpoints, proof, provenance);
    return card;
  }

  // -- Validation record dialog ------------------------------------------------------------------

  recordHeader(frame) {
    const header = node("header", "record-header");
    const titleRow = node("div", "capture-title-row");
    const title = node("h2", "", frame.title);
    title.id = "capture-dialog-title";
    titleRow.append(title, node("span", "verified-badge", "Gate passed"));
    const metadata = node("div", "capture-meta");
    for (const text of [
      this.scopeLabel(frame.key, frame.minecraft),
      frame.loader_name,
      this.scenarioLabel(frame.scenario),
      this.roleLabel(frame.role),
      `step ${frame.step}`
    ]) {
      metadata.append(node("span", "", text));
    }
    if (frame.epoch) metadata.append(node("span", "epoch-badge", epochLabel(frame.epoch)));
    header.append(titleRow, metadata);
    return header;
  }

  recordFigure(frame) {
    const figure = node("figure", "record-figure");
    const image = document.createElement("img");
    image.src = sameOriginPath(frame.image);
    image.alt = frame.alt;
    image.width = frame.width;
    image.height = frame.height;
    image.decoding = "async";
    const caption = document.createElement("figcaption");
    const link = node("a", "", `Open the published ${frame.published.format.toUpperCase()} ↗`);
    link.href = sameOriginPath(frame.image);
    link.target = "_blank";
    link.rel = "noopener";
    caption.append(link);
    figure.append(image, caption);
    return figure;
  }

  contractSection(frame, release) {
    const rows = [
      ["Scenario", `${this.scenarioLabel(frame.scenario)} (${frame.scenario})`],
      ["Client role", `${this.roleLabel(frame.role)} (${frame.role})`],
      ["Report step", node("span", "mono", frame.step)],
      ["Capture id", node("span", "mono", frame.capture_id)],
      ["Contract order", frame.capture_order],
      ["Advisory review tier", `${this.tierLabel(frame.review_tier)} (${frame.review_tier})`]
    ];
    if (release) {
      rows.push(["Contract SHA-256", node("span", "mono", release.contract_sha256)]);
      rows.push(["Contract source", externalLink(release.contract_url, "scenario contract at this commit ↗")]);
    }
    return recordSection(
      "Checkpoint contract",
      "The versioned scenario contract is the only authored source for this checkpoint. Its expectation, ordering and review tier are fixed before the run.",
      node("p", "record-prose", frame.expectation),
      factList(rows)
    );
  }

  assertionSection(frame) {
    return recordSection(
      "Deterministic assertion",
      "The packaged Minecraft client emitted this message when the checkpoint's mandatory assertion passed. A failed, empty or missing assertion stops the run before any screenshot can be published.",
      node("p", "evidence-quote mono", frame.runtime_evidence)
    );
  }

  integritySection(frame) {
    const columns = node("div", "record-columns");
    const source = node("div");
    source.append(
      node("h4", "", "Original capture (PNG)"),
      node("p", "record-lead", "Written by the packaged client, decoded and measured by the gate. Its bytes never leave the short-lived run artifact."),
      pixelFacts(frame.source.pixel)
    );
    const published = node("div");
    published.append(
      node("h4", "", `Published image (${frame.published.format.toUpperCase()})`),
      node("p", "record-lead", "Re-decoded and re-measured by protected code before deployment. The file name is the SHA-256 of the exact bytes served to you."),
      pixelFacts(frame.published.pixel)
    );
    columns.append(source, published);
    return recordSection(
      "Image integrity",
      "Both images are decoded and rejected if they are corrupt, implausibly sized or effectively blank.",
      columns
    );
  }

  comparisonSection(frame) {
    const comparisons = this.comparisonsByFrame.get(this.identity(frame.key, frame.frame_id)) || [];
    if (!comparisons.length) {
      return recordSection(
        "Pixel comparisons",
        "This checkpoint is not part of a required pixel-change pair; its proof is the assertion plus the image integrity checks above."
      );
    }
    const children = [];
    for (const comparison of comparisons) {
      const first = this.frameById.get(this.identity(comparison.key, comparison.first_frame_id));
      const second = this.frameById.get(this.identity(comparison.key, comparison.second_frame_id));
      const pair = node("article", "comparison-record");
      const role = comparison.first_frame_id === frame.frame_id ? "before" : "after";
      pair.append(
        node("h4", "", `${first ? first.title : comparison.first_frame_id} → ${second ? second.title : comparison.second_frame_id}`),
        node("p", "record-lead", `This capture is the ${role} frame of the pair. The run fails unless the measured change reaches the contract minimum.`)
      );
      const columns = node("div", "record-columns");
      const original = node("div");
      original.append(node("h5", "", "Measured on the original PNGs"), comparisonFacts(comparison.source));
      const derived = node("div");
      derived.append(node("h5", "", "Re-measured on the published images"), comparisonFacts(comparison.published));
      columns.append(original, derived);
      pair.append(columns);
      children.push(pair);
    }
    return recordSection(
      "Pixel comparisons",
      "Directed comparisons prove the interface actually changed between two checkpoints instead of repeating one frame.",
      ...children
    );
  }

  laneSection(frame) {
    const lane = this.laneById.get(this.identity(frame.key, frame.lane_id));
    if (!lane) {
      return recordSection("Packaged lane", "No lane record was published for this capture.");
    }
    // A partially re-captured lane keeps the baseline execution of the captures it did not re-test.
    const run = frame.epoch === "baseline" && lane.baseline_run ? lane.baseline_run : lane;
    return recordSection(
      "Packaged lane",
      "The capture comes from a real headless Minecraft run of the packaged production JAR, not a development launch.",
      factList([
        ["Lane", node("span", "mono", lane.lane_id)],
        ["Artifact node", node("span", "mono", lane.artifact_node)],
        ["Loader", `${lane.loader_name} · Minecraft ${lane.minecraft}`],
        ["Clients in this lane", lane.roles.map((role) => this.roleLabel(role)).join(", ")],
        ["Lane run", lane.baseline_run ? epochLabel(frame.epoch) : undefined],
        ["Mod JAR SHA-256", node("span", "mono", run.jars.production_sha256)],
        ["Harness JAR SHA-256", run.jars.harness_sha256 ? node("span", "mono", run.jars.harness_sha256) : undefined],
        ["Lane result", run.status],
        ["Lane wall time", seconds(run.elapsed_s)]
      ])
    );
  }

  kitLink() {
    const build = this.data.build;
    if (!COMMIT.test(build.kit_sha)) throw new Error("Kit identity is not a commit");
    return externalLink(`${KIT_REPOSITORY_URL}/commit/${build.kit_sha}`,
      `mod-base ${build.kit_version} (${build.kit_sha.slice(0, 12)}) ↗`);
  }

  provenanceSection(frame, release) {
    const origin = frame.provenance;
    const rows = [
      ["Tested run", this.runLink(origin.tested_run_url, "GitHub Actions run ↗")],
      ["Tested commit", node("span", "mono", origin.tested_commit)],
      ["Tested at", origin.tested_created_at]
    ];
    if (origin.tested_run_url !== origin.handoff_run_url || origin.tested_commit !== origin.handoff_commit) {
      rows.push(
        ["Publishing run", this.runLink(origin.handoff_run_url, "GitHub Actions run ↗")],
        ["Published commit", node("span", "mono", origin.handoff_commit)]
      );
      if (release) rows.push(["Published at", release.handoff.created_at]);
    }
    if (release) rows.push(["Branch", node("span", "mono", release.subject.branch)]);
    if (origin.coverage_sha !== origin.tested_commit) {
      rows.push(
        ["Coverage commit", node("span", "mono", origin.coverage_sha)],
        ["Evidence reuse", frame.epoch === "baseline"
          ? "This capture is reused from the authenticated complete baseline; its feature and dependencies are unchanged since the tested commit."
          : "The tested run is an authenticated reuse of an earlier run of the same source tree."]
      );
    }
    rows.push(
      ["Kit", this.kitLink()],
      ["Pixel metrics version", this.data.build.pixel_metrics_version]
    );
    return recordSection(
      "Provenance",
      "Each image retains its original tested run and commit. Reused images also name the newer commit whose coverage was verified.",
      factList(rows)
    );
  }

  rawSection(frame) {
    const details = document.createElement("details");
    details.className = "record-raw";
    const summary = document.createElement("summary");
    summary.textContent = "Machine-readable record";
    const payload = {
      frame,
      lane: this.laneById.get(this.identity(frame.key, frame.lane_id)) || null,
      comparisons: this.comparisonsByFrame.get(this.identity(frame.key, frame.frame_id)) || []
    };
    const block = document.createElement("pre");
    block.textContent = JSON.stringify(payload, null, 2);
    details.append(summary, block);
    return details;
  }

  showRecord(frame) {
    const body = document.querySelector("#capture-dialog-body");
    const release = this.releaseByKey.get(frame.key);
    body.replaceChildren(
      this.recordHeader(frame),
      this.recordFigure(frame),
      this.contractSection(frame, release),
      this.assertionSection(frame),
      this.integritySection(frame),
      this.comparisonSection(frame),
      this.laneSection(frame),
      this.provenanceSection(frame, release),
      this.rawSection(frame)
    );
    if (typeof this.dialog.showModal === "function") this.dialog.showModal();
    else this.dialog.setAttribute("open", "");
  }
}

async function startGallery() {
  const status = document.querySelector("#gallery-status");
  try {
    const response = await fetch("gallery-data.json", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!isGalleryInventory(data)) throw new Error("Unsupported or empty gallery inventory");
    new Gallery(data).start();
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `The evidence gallery could not be loaded: ${error.message}`;
  }
}

startGallery();
