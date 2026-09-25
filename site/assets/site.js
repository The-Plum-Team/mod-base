"use strict";

// Landing page renderer for mod-base.site v1 (site-data.json). Every string reaches the page
// through textContent; links pass safeHref (https or same origin) before they are assigned.

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function safeHref(value) {
  const url = new URL(value, window.location.href);
  if (url.protocol !== "https:" && url.origin !== window.location.origin) {
    throw new Error("Unsupported link protocol");
  }
  return url.href;
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function linkCard(title, description, href) {
  const link = element("a", "link-card");
  link.href = safeHref(href);
  const copy = element("div");
  copy.append(element("h3", "", title), element("p", "", description));
  link.append(copy, element("span", "card-arrow", "↗"));
  return link;
}

function releaseHeading(release) {
  // A key whose label only restates its single Minecraft version ("Minecraft 1.20.1") shows the bare
  // version; any other label (a branch, a multi-version key) is the heading and its versions badges.
  const [only] = release.minecraft;
  return release.minecraft.length === 1 && release.label.includes(only) ? only : release.label;
}

function releaseCard(release) {
  const link = element("a", "release-card");
  link.href = safeHref(release.tested_run_url);
  link.setAttribute(
    "aria-label",
    `${release.label}, Verified, ${release.loader_names.join(" and ")}, ${release.frame_count} validated captures, commit ${release.short_sha}; open packaged E2E run on GitHub Actions`
  );

  const header = element("header");
  header.append(
    element("span", "version-number", releaseHeading(release)),
    element("span", "verified-badge", "Verified")
  );
  const body = element("div");
  body.append(element("p", "", `${release.frame_count} validated captures`));
  const badges = element("div", "release-meta");
  for (const version of release.minecraft) {
    if (!release.label.includes(version)) badges.append(element("span", "meta-badge", `Minecraft ${version}`));
  }
  for (const loader of release.loader_names) badges.append(element("span", "meta-badge", loader));
  badges.append(element("span", "meta-badge", `commit ${release.short_sha}`));
  body.append(badges);
  link.append(header, body);
  return link;
}

function accentTagline(heading) {
  // "First sentence. Second sentence." keeps the two-line hero with an accented second line.
  const text = heading.textContent;
  const split = text.indexOf(". ");
  if (split < 0 || split + 2 >= text.length) return;
  const accent = element("span", "", text.slice(split + 2));
  heading.replaceChildren(document.createTextNode(text.slice(0, split + 1)), document.createElement("br"), accent);
}

function galleryDescription(families) {
  const base = "Browse packaged-Minecraft screenshots across every supported version.";
  if (!families.length) return base;
  return `${base} Includes paired evidence: ${families.map((family) => family.title).join(", ")}.`;
}

function isSiteInventory(data) {
  return Boolean(data)
    && data.kind === "mod-base.site"
    && data.schema_version === 1
    && Boolean(data.project)
    && Array.isArray(data.project.links)
    && Array.isArray(data.releases)
    && Array.isArray(data.families)
    && Boolean(data.copy)
    && Array.isArray(data.copy.principles);
}

async function start() {
  const status = document.querySelector("#site-status");
  accentTagline(document.querySelector("#hero-title"));
  try {
    const response = await fetch("site-data.json", { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (!isSiteInventory(data)) throw new Error("Unsupported site inventory");

    document.querySelector("#project-description").textContent = data.project.description;
    document.querySelector("#evidence-lead").textContent = data.copy.evidence_lead;
    const principles = document.querySelector("#principles-list");
    for (const principle of data.copy.principles) principles.append(element("li", "", principle));

    const destinations = document.querySelector("#destination-grid");
    for (const item of data.project.links) {
      destinations.append(linkCard(item.title, item.description, item.url));
    }
    destinations.append(
      linkCard("GitHub", "Source, releases and issue tracking.", data.project.repository_url),
      linkCard("Verified E2E", galleryDescription(data.families), data.gallery_url)
    );

    const releases = document.querySelector("#release-grid");
    for (const release of data.releases) releases.append(releaseCard(release));
    const captures = data.releases.reduce((total, release) => total + release.frame_count, 0);
    const versions = new Set(data.releases.flatMap((release) => release.minecraft)).size;
    status.textContent = `${plural(versions, "version")} · ${captures} validated captures · provenance linked to GitHub Actions`;
  } catch (error) {
    status.dataset.error = "true";
    status.textContent = `The verified inventory could not be loaded: ${error.message}`;
  }
}

start();
