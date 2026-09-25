"""Assertions on the rendered site of a conformance run (MB10, SPEC §6, §9.1 step 9).

Every check reads the published tree only: the front-end data gate of ``e2e/gallery-data.json``
(and the full ``documents.validate_gallery``/``validate_site`` schemas), the exact meta CSP and
``no-referrer`` policy on both pages, no remote or inline script, stylesheet, image or frame, no
executable URL scheme and no leftover template syntax, ``node --check`` on every published script
(when ``node`` is installed), and ``build.json``: the two-part identity of the simulated run and
kit and the inventory of every other published file.
"""

from __future__ import annotations

import html.parser
import os
import posixpath
import re
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import PIXEL_METRICS_VERSION
from mod_base.errors import MbError
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import read_json_file, sha256_hex

#: SPEC §6.3: the exact meta policy of both pages.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'")
PAGES = ("index.html", "e2e/index.html")
#: Elements whose URL attribute loads a subresource (never remote, never inline data).
_LOADING = {"script": "src", "link": "href", "img": "src", "source": "src", "iframe": "src", "embed": "src",
            "object": "data", "audio": "src", "video": "src", "track": "src", "frame": "src"}
_FORBIDDEN_TAGS = frozenset({"iframe", "frame", "object", "embed", "base"})
NODE_TIMEOUT_SECONDS = 120
#: A stylesheet URL with a scheme (``https:``, ``data:``...) or a network-path reference (``//host``).
_REMOTE_CSS_URL = re.compile(r"url\(\s*['\"]?\s*(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)


def _fail(message: str) -> MbError:
    return MbError(message, reason="conformance-site")


class _Page(html.parser.HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.loads: list[str] = []
        self.policies: list[str] = []
        self.referrer: list[str] = []
        self.problems: list[str] = []
        self.open_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if tag in _FORBIDDEN_TAGS:
            self.problems.append(f"<{tag}> element")
        for name, value in values.items():
            if name.startswith("on"):
                self.problems.append(f"inline handler {name}")
            if name in ("href", "src", "data", "action", "srcset") and value.strip().lower().startswith(
                    ("javascript:", "data:", "vbscript:")):
                self.problems.append(f"{tag} {name}={value[:40]!r}")
        if tag == "meta" and values.get("http-equiv", "").lower() == "content-security-policy":
            self.policies.append(values.get("content", ""))
        if tag == "meta" and values.get("name", "").lower() == "referrer":
            self.referrer.append(values.get("content", ""))
        if tag == "style":
            self.problems.append("inline <style>")
        if tag == "script":
            self.open_script = True
            if not values.get("src"):
                self.problems.append("inline <script>")
        attribute = _LOADING.get(tag)
        if attribute is not None and attribute in values:
            if tag == "link" and values.get("rel", "").lower() not in ("stylesheet", "icon"):
                return
            target = _resolve(self.base, values[attribute])
            if target is None:
                self.problems.append(f"non-local {tag} {attribute}={values[attribute][:60]!r}")
            else:
                self.loads.append(target)
        if "style" in values:
            self.problems.append(f"inline style attribute on <{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.open_script = False

    def handle_data(self, data: str) -> None:
        if self.open_script and data.strip():
            self.problems.append("inline script text")


def _resolve(base: str, url: str) -> str | None:
    """The site path a relative URL of a page in directory ``base`` names, or ``None`` for anything
    else: a scheme, a host, an absolute path, a query or fragment, or traversal above the site."""

    value = url.strip()
    if (not value or ":" in value.split("/", 1)[0] or value.startswith(("/", "\\")) or "\\" in value
            or any(character in value for character in "?#")):
        return None
    resolved = posixpath.normpath(posixpath.join(base, value))
    return None if resolved == ".." or resolved.startswith(("../", "/")) else resolved


def _read(site: Path, relative: str) -> bytes:
    path = site / relative
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _fail(f"{relative} is not a regular published file")
    return path.read_bytes()


def check_pages(site: Path) -> int:
    """The CSP, referrer and subresource rules of both pages; returns the number of checks."""

    checks = 0
    for relative in PAGES:
        text = _read(site, relative).decode("utf-8")
        parser = _Page(posixpath.dirname(relative))
        parser.feed(text)
        parser.close()
        if parser.policies != [CSP]:
            raise _fail(f"{relative} does not carry exactly the SPEC §6.3 meta CSP")
        if parser.referrer != ["no-referrer"]:
            raise _fail(f"{relative} does not set the no-referrer policy")
        if parser.problems:
            raise _fail(f"{relative}: {parser.problems[0]}")
        missing = sorted(target for target in parser.loads if not (site / target).is_file())
        if missing:
            raise _fail(f"{relative} loads {missing[0]}, which the site does not publish")
        for leftover in ("{{", "<!-- mb:"):
            if leftover in text:
                raise _fail(f"{relative} keeps template syntax {leftover!r}")
        checks += 4
    for relative in sorted(_files(site)):
        if relative.endswith(".css"):
            text = _read(site, relative).decode("utf-8")
            if "@import" in text.lower() or _REMOTE_CSS_URL.search(text):
                raise _fail(f"{relative} imports a stylesheet or loads a remote or inline resource")
            checks += 1
    return checks


def _files(site: Path) -> list[str]:
    found = []
    for directory, subdirectories, names in os.walk(site):
        subdirectories.sort()
        for name in names:
            found.append(Path(directory, name).relative_to(site).as_posix())
    return found


def check_scripts(site: Path) -> tuple[int, bool]:
    """``node --check`` every published script; returns ``(checks, node_found)``."""

    scripts = sorted(relative for relative in _files(site) if relative.endswith(".js"))
    if not scripts:
        raise _fail("the site publishes no script")
    node = shutil.which("node")
    if node is None:
        return 0, False
    for relative in scripts:
        completed = subprocess.run([node, "--check", str(site / relative)], stdin=subprocess.DEVNULL,
                                   capture_output=True, timeout=NODE_TIMEOUT_SECONDS, check=False)
        if completed.returncode != 0:
            raise _fail(f"node --check rejects {relative}")
    return len(scripts), True


def check_data(site: Path) -> tuple[dict[str, Any], int]:
    """The front-end data gate and the full schemas of the published data; returns the gallery."""

    gallery, _ = read_json_file(site / "e2e" / "gallery-data.json", label="gallery-data.json",
                                max_bytes=lim.MAX_GALLERY_DATA_BYTES)
    if not (isinstance(gallery, dict) and gallery.get("kind") == "mod-base.gallery"
            and gallery.get("schema_version") == 1 and isinstance(gallery.get("frames"), list) and gallery["frames"]
            and all(isinstance(gallery.get(name), list) for name in ("releases", "lanes", "comparisons", "families"))
            and all(isinstance(family, dict) and isinstance(family.get("available"), bool)
                    and isinstance(family.get("lanes"), list) and isinstance(family.get("not_applicable"), list)
                    for family in gallery["families"])):
        raise _fail("e2e/gallery-data.json fails the front-end data gate")
    documents.validate_gallery(gallery)
    site_data, _ = read_json_file(site / "site-data.json", label="site-data.json", max_bytes=lim.MAX_SITE_DATA_BYTES)
    documents.validate_site(site_data)
    for frame in gallery["frames"]:
        image = _resolve("e2e", frame["image"])
        if image is None or not image.startswith("e2e/") or not (site / image).is_file():
            raise _fail(f"frame {frame['frame_id']} does not publish its local image")
    return gallery, 3 + len(gallery["frames"])


def check_build_record(site: Path, *, repository: str, implementation: Mapping[str, Any],
                       kit: Mapping[str, str], site_sha256: str) -> int:
    """``build.json`` names the simulated run and kit and the inventory of every other file."""

    record, _ = read_json_file(site / "build.json", label="build.json", max_bytes=lim.MAX_BUILD_RECORD_BYTES)
    documents.validate_build(record)
    wanted = {"sha": implementation["sha"], "run_id": implementation["run_id"],
              "run_attempt": implementation["run_attempt"],
              "run_url": grammar.run_url(repository, implementation["run_id"]),
              "workflow_ref": implementation["workflow_ref"]}
    if record["repository"] != repository or record["implementation"] != wanted:
        raise _fail("build.json does not name the simulated Pages run")
    if record["kit"] != dict(kit):
        raise _fail("build.json does not name the simulated kit")
    if record["pixel_metrics_version"] != PIXEL_METRICS_VERSION:
        raise _fail("build.json names another pixel_metrics_version")
    inventory = []
    for relative in sorted(_files(site)):
        if relative != "build.json":
            data = _read(site, relative)
            inventory.append({"path": relative, "sha256": sha256_hex(data), "size": len(data)})
    observed = documents.inventory_sha256(inventory)
    if record["site_inventory_sha256"] != observed or site_sha256 != observed:
        raise _fail("build.json does not name the published site inventory")
    return 4
