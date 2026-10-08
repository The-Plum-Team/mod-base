"""``template check|sync|init`` (MB9, SPEC §8.2).

``check`` reports every drift as a unified diff: managed files byte-identical to
``template/managed/<path>`` (``pages.yml``'s managed region after ``{{PIN}}``/``{{VERSION}}``
substitution plus the extension-region rules of SPEC §5.2), fragment files holding their required
lines/markers, the ``AGENTS.md`` grammar of SPEC §8.3, the managed-docs link rule, and the absence
of ``CLAUDE.md``, ``.claude/CLAUDE.md`` and ``CLAUDE.local.md``; files in ``template.deferred`` may
be absent (see below). ``sync`` rewrites managed files and the caller's managed region (with
``write``), preserving the pin and the extension region. ``init`` seeds a new repository and never
overwrites.

Detailed rules (``docs/OPERATIONS.md`` and ``docs/ONBOARDING.md`` describe the procedures):

* The manifest classes are ``managed`` (byte-identical), ``fragment`` (seeded once; ``lines`` must
  appear as whole lines, ``markers`` as substrings) and ``seeded`` (copied once by ``init --seed``,
  never checked).
* ``template.deferred`` stages an adoption across pull requests, as when Block Pops can change its
  root files only after its controller upgrade. It may name only :data:`DEFERRABLE` paths, which
  sit outside the publication control path; deferring the caller, the bootstrap, a shared agent
  document, ``CODEOWNERS`` or any other path is a ``forbidden`` drift, so deferral can never hide
  drift of privileged files. A deferred path may be absent. When present it is checked under its
  class, with one allowance: the required ``lines`` a present deferred fragment still lacks (for
  example an older ``.gitignore`` that its adoption pull request cannot yet change) are reported
  by :func:`pending` instead of failing :func:`check`. A deferred managed file must be
  byte-identical, and every other fragment rule (markers, ``AGENTS.md`` grammar, the Dependabot
  ignore) is strict. An empty ``deferred`` checks everything strictly.
* ``.github/workflows/pages.yml`` is exactly the managed region (from its ``# >>> mod-base managed:``
  first line to ``# <<< mod-base managed``), the extension begin line and the extension end line,
  with only extension jobs in between. Extension jobs sit at the two-space job level, are named
  ``ext-[a-z0-9-]+``, are block mappings, never mention the kit repository, the ``github-pages``
  environment or a ``mod-base-pages-`` concurrency group, grant no ``pages``, ``id-token``,
  ``write-all`` or ``actions: write`` permission, never need ``rotate``, use no YAML anchors or
  aliases (so nothing can be smuggled in by reference) and carry no display name that could pass
  for a caller or callee job in the jobs API (a ``/``, an expression or a caller job name). Content
  before the first extension job would continue the managed ``rotate`` job and is refused. So
  that these rules see what GitHub parses, the region is a small YAML subset
  (:func:`extension_violations`): no escapes, explicit keys or multi-line quoted scalars outside
  block scalars, plain job-level keys, balanced flow collections, and no block indentation
  indicators; each job-level key is also checked with all its deeper lines joined.
* Structural fragment rules: ``AGENTS.md`` is exactly the two shared imports followed by
  ``template.agents_local`` (each an existing regular file) with a trailing newline;
  ``.github/CODEOWNERS`` gives every listed pattern at least one owner and has no owner-less rule
  (an owner-less rule removes ownership); ``.github/dependabot.yml`` makes every ``github-actions``
  update ignore, for all versions, ``The-Plum-Team/mod-base*`` and every third-party action the
  managed region of a workflow in its manifest entry's ``ignore_actions_of`` pins (derived from the
  kit's template, :func:`pinned_actions`: ``actions/deploy-pages`` for the caller), since a
  Dependabot bump of either would be managed-file drift.
* Managed Markdown may link only to other managed documents or absolute ``https://`` URLs; a
  ``](`` whose destination the rule cannot parse is refused too.
* Every repository path is reached component by component without following symlinks; a
  symlinked managed or fragment file is a drift, and ``sync``/``init`` refuse to write through one.
* Line endings are compared, never normalized: GitHub reads a workflow, and the bootstrap runs,
  from the committed bytes, and a CRLF blob committed with ``core.autocrlf=false`` must fail in CI.
  Checkouts are deterministic instead: the managed ``.gitattributes`` pins ``text eol=lf`` for every
  managed and fragment path, so ``core.autocrlf=true`` (Git for Windows' default) checks them out
  with LF. A managed file (or caller) with CRLF line endings, a checkout made before that rule, is
  reported with that advice (:data:`CRLF_ADVICE`), plus the diff of its LF form when that still
  differs, instead of a whole-file diff; ``sync --write`` rewrites it with LF, and a CRLF caller
  keeps its extension region (read with LF line endings).
* ``init`` renders ``{{name}}``-style seed placeholders from the config it seeds (``project.name``,
  ``license_label``, ``canonical_branch`` and the Modrinth/CurseForge slugs of ``project.links``)
  and leaves every other placeholder for the maintainer. ``LICENSE`` is seeded only for a known
  ``license_label`` (legal text is never managed afterwards).
* Every workflow the kit renders with the mod's pin is enrolled in :data:`RENDERED_CALLERS`, which
  is code: manifest data can neither enrol a caller, remap one nor choose how it is rendered, and a
  template the pin parser would read (a workflow, or an ``action.yml`` below ``.github/actions``)
  may hold ``{{PIN}}``/``{{VERSION}}`` only when it is enrolled. The Pages caller is a manifest
  entry and always managed. The Build/E2E callers (the guard, the Build, the packaged E2E and the
  status caller) are enrolled in code alone and managed only in the activation modes that list them
  (:data:`mod_base.build_ci.activation.MANAGED_CALLERS`); they are rendered whole, so they have no
  extension region, and ``template.deferred`` cannot name them.
* The activation state is read before anything else (:func:`load_template_activation`). A mod with
  neither ``site/mod-base-build-activation.json`` nor ``scripts/ci/mod-base-build.json`` never
  adopted the shared Build/E2E: nothing about those callers is checked or written, exactly as
  before they existed. A Build configuration without a manifest is an error, so deleting the
  manifest never frees a caller from the check. With a manifest, a caller its mode manages is
  missing, changed or current like any managed file, and one its mode does not manage must not
  exist (``forbidden``): a kit caller is either drift-checked or absent. ``sync`` and ``init``
  write the callers the mode manages and never delete one.
* While a mode manages callers, ``.github/dependabot.yml`` must also ignore every third-party
  action those callers pin, for the reason it ignores the Pages caller's.
"""

from __future__ import annotations

import difflib
import os
import posixpath
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mod_base
from mod_base.build_ci.activation import (ACTIVATION_PATH, CALLERS, MANAGED_CALLERS, managed_mode, managing_modes,
                                          parse_activation)
from mod_base.build_ci.config import BUILD_CONFIG_PATH, validate_build_config
from mod_base.config import DEFAULT_CONFIG_PATH, Config, load_config, parse_config
from mod_base.errors import MbError, single_line
from mod_base.model import limits as lim
from mod_base.model.canonical import read_regular_file, strict_loads
from mod_base.model.documents import validate_template_manifest
from mod_base.model.validators import DocumentError
from mod_base.pin import (ACTION_FILES, PIN_LINE, Pin, checkout_head, parse_pin, parse_pin_files, read_pin_files,
                          release_tags_at)
from mod_base.workflow import CALLER

OWNER = "MB9"
MANIFEST_PATH = "template/manifest.json"

CALLER_PATH = ".github/workflows/pages.yml"


@dataclass(frozen=True)
class RenderedCaller:
    """One workflow the kit renders with the mod's pin: ``path`` in the mod, the template ``source``
    below ``template/``, the ``renderer`` (a :data:`RENDERERS` name) and the activation ``modes``
    that manage it (:func:`mod_base.build_ci.activation.managed_mode` values). ``modes`` is ``None``
    for a caller that is a manifest entry and managed whatever the activation."""

    path: str
    source: str
    renderer: str
    modes: frozenset[str] | None


#: The closed registry of rendered callers. It is code: neither the manifest nor the activation
#: manifest can add an entry, move one or choose its renderer. A Build/E2E caller is enrolled here
#: alone (never in ``template/manifest.json``) and takes its modes from the activation table.
RENDERED_CALLERS: tuple[RenderedCaller, ...] = (
    RenderedCaller(CALLER_PATH, f"managed/{CALLER_PATH}", "pages-extension", None),
    *(RenderedCaller(path, f"managed/{path}", "pinned", managing_modes(path)) for path in CALLERS),
)

AGENTS_PATH = "AGENTS.md"
CODEOWNERS_PATH = ".github/CODEOWNERS"
DEPENDABOT_PATH = ".github/dependabot.yml"
LICENSE_PATH = "LICENSE"
SHARED_IMPORTS = ("docs/ai/shared/REPOSITORY.md", "docs/ai/shared/PUBLIC-EVIDENCE.md")
DEFAULT_AGENTS_LOCAL = ("docs/ai/PROJECT.md",)
FORBIDDEN_PATHS = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")
#: ``project.license_label`` -> the seed template of ``LICENSE``; any other label seeds no LICENSE.
LICENSE_TEMPLATES = {
    "All Rights Reserved": "seed/LICENSE-ARR.tmpl",
    "LGPL-2.1-only": "seed/LICENSE-NOTICE-LGPL-2.1.tmpl",
}
DEPENDABOT_IGNORE = "The-Plum-Team/mod-base*"
#: The only paths ``template.deferred`` may name: shared files outside the publication control path.
DEFERRABLE = frozenset({".gitattributes", ".gitignore", ".github/dependabot.yml", ".github/pull_request_template.md",
                        AGENTS_PATH})

MANAGED_BEGIN = "# >>> mod-base managed:"
MANAGED_END = "# <<< mod-base managed\n"
EXTENSION_BEGIN = "# >>> mod-local extensions:"
EXTENSION_END = "# <<< mod-local extensions\n"
#: What a region marker line starts with: a caller that is rendered whole holds no such line.
_REGION_MARKERS = (MANAGED_BEGIN, MANAGED_END.rstrip("\n"), EXTENSION_BEGIN, EXTENSION_END.rstrip("\n"))
PIN_PLACEHOLDER = "{{PIN}}"
VERSION_PLACEHOLDER = "{{VERSION}}"

MAX_FILE_BYTES = 1024 * 1024
MAX_DETAIL_CHARS = 20000
CRLF_ADVICE = ("has CRLF line endings: this checkout converted them (core.autocrlf) before the managed "
               ".gitattributes pinned eol=lf; delete the file and check it out again (git checkout -- <path>) "
               "or run template sync --write, never commit CRLF")
#: The managed ``.gitattributes`` lists the manifest's files, so a Build/E2E caller has no rule yet.
ACTIVATION_CRLF_ADVICE = ("has CRLF line endings: this checkout converted them (core.autocrlf), because the managed "
                          ".gitattributes pins eol=lf for no Build/E2E caller yet; add '/<path> text eol=lf' to "
                          ".git/info/attributes and check the file out again, or run template sync --write, never "
                          "commit CRLF")
SEED_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)\}\}")

_EXTENSION_JOB = re.compile(r"^  (ext-[a-z0-9-]+):\s*$")
_ANY_JOB = re.compile(r"^  (\S[^:]*):")
#: A job-level line: a plain (unquoted, untagged) key.
_JOB_KEY = re.compile(r"^ +([A-Za-z_][A-Za-z0-9_-]*):(?:\s+(.*))?$")
_FORBIDDEN_EXTENSION_TEXT = (
    (re.compile(r"(?:^|[\s{,\[\"'])pages[\"']?\s*:", re.IGNORECASE), "grants a pages permission"),
    (re.compile(r"(?:^|[\s{,\[\"'])id-token[\"']?\s*:", re.IGNORECASE), "grants an id-token permission"),
    (re.compile(r"(?:^|[\s{,\[\"'])actions[\"']?\s*:\s*(?:(?:![^\s,{}\[\]]*|[|>][-+0-9]*)\s+)*[\"']?write",
                re.IGNORECASE), "grants actions: write"),
    (re.compile(r"write-all", re.IGNORECASE), "grants write-all permissions"),
    (re.compile(r"the-plum-team/mod-base", re.IGNORECASE), "references mod-base"),
    (re.compile(r"github-pages", re.IGNORECASE), "names the github-pages environment or artifact"),
    (re.compile(r"mod-base-pages-", re.IGNORECASE), "names a mod-base concurrency group"),
)
#: Characters some YAML parsers treat as line breaks or invisible separators: never in extensions.
_HIDDEN_BREAKS = re.compile("[\x00-\x1f\x7f-\x9f  ﻿]")
_ANCHOR_OR_ALIAS = re.compile(r"(?:(?::|^\s*-|[\[{,])\s*)[&*][A-Za-z0-9_-]")
#: An explicit YAML key (``? key`` / ``: value``), which splits a key from its value across lines.
_EXPLICIT_KEY = re.compile(r"^\s*(?:-\s+)*[?:](?:\s|$)")
#: A value that is a block scalar header (``key: |``, ``- >-``...); group 1 holds its indicators.
_BLOCK_HEADER = re.compile(r"(?:^\s*-|:)(?:\s+[!&]\S*)*\s+[|>]([-+0-9]*)$")
_NEEDS_WORD = re.compile(r"\bneeds\b")
_ROTATE_WORD = re.compile(r"(?<![A-Za-z0-9_-])rotate(?![A-Za-z0-9_-])")
_JOB_ID_WORD = re.compile(r"[A-Za-z0-9_-]+")
_OWNER_TOKEN = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9._-]+)?|[^@\s]+@[^@\s]+\.[^@\s]+)$")
_UPDATES_KEY = re.compile(r"^updates\s*:\s*(?:#.*)?$")
_ECOSYSTEM = re.compile(r"^\s*(?:-\s+)?package-ecosystem\s*:\s*[\"']?([A-Za-z0-9_-]+)[\"']?\s*(?:#.*)?$")
_IGNORE_KEY = re.compile(r"^(\s*)ignore\s*:\s*(?:#.*)?$")
_IGNORE_FLOW = re.compile(r"^\s*ignore\s*:\s*\[(.*)\]\s*(?:#.*)?$")
_FLOW_ENTRY = re.compile(r"\{([^{}]*)\}")
_ENTRY_KEY = re.compile(r"^\s*(?:-\s+)?[\"']?([A-Za-z0-9_-]+)[\"']?\s*:\s*(.*?)\s*(?:#.*)?$")
#: A ``uses:`` reference to a remote action or reusable workflow, ``owner/repo[/path]@ref``: group 1
#: is its Dependabot ``dependency-name`` (local ``./`` actions and ``docker://`` images never match).
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*[\"']?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:/[^@\s\"']*)?@")

_INLINE_LINK = re.compile(r"\]\(\s*<?([^)\s>]*)>?(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)")
_ANGLE_LINK = re.compile(r"\]\(\s*<([^>\n]*)>")
_LINK_OPENER = re.compile(r"\]\(")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\]]+\]:\s*<?([^\s>]*)>?", re.MULTILINE)
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*)>")
_HTML_LINK = re.compile(r"(?<![\w-])(?:href|src|srcset|poster|action|formaction|cite|background)\s*=\s*"
                        r"(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'`=<>]+))", re.IGNORECASE)
_BARE_INSECURE = re.compile(r"(?<![A-Za-z0-9])(?:(?:http|ftp)://|www\.)[^\s<>()\]]+", re.IGNORECASE)
_HTTPS_URL = re.compile(r"^https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?:/[^\s<>\"'`]*)?(?:[?#][^\s<>\"'`]*)?$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")


@dataclass(frozen=True)
class Drift:
    """One difference: ``kind`` is ``missing``, ``changed``, ``fragment``, ``agents``, ``links``,
    ``forbidden`` or ``extension``; ``detail`` is a bounded unified diff or message."""

    path: str
    kind: str
    detail: str


# -- Bounded, symlink-free repository access -------------------------------------------------------


def _state(root: Path, relative: str) -> str:
    """``absent``, ``file`` (a regular file reached without symlinks) or ``invalid``."""

    current = root
    parts = relative.split("/")
    for index, part in enumerate(parts):
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            return "absent"
        except OSError:
            return "invalid"
        if stat.S_ISLNK(status.st_mode):
            return "invalid"
        if index < len(parts) - 1:
            if not stat.S_ISDIR(status.st_mode):
                return "invalid"
        elif not stat.S_ISREG(status.st_mode):
            return "invalid"
    return "file"


def _read(root: Path, relative: str, label: str) -> bytes:
    if _state(root, relative) != "file":
        raise MbError(f"{label} {relative} must be a regular file reached without symlinks", reason="template")
    return read_regular_file(root / relative, label=f"{label} {relative}", max_bytes=MAX_FILE_BYTES,
                             allow_empty=True)


def _real_directory(path: Path, label: str) -> Path:
    try:
        status = os.lstat(path)
    except OSError:
        raise MbError(f"{label} {path} must be an existing real directory", reason="template") from None
    if not stat.S_ISDIR(status.st_mode):
        raise MbError(f"{label} {path} must be an existing real directory", reason="template")
    return path


def _prepare_parent(root: Path, relative: str) -> Path:
    """Create the missing parent directories of ``relative`` under ``root`` without following symlinks."""

    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        try:
            status = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o755)
            continue
        if not stat.S_ISDIR(status.st_mode):
            raise MbError(f"cannot write {relative}: {current} is not a real directory", reason="template")
    return current


def _write_new(root: Path, relative: str, data: bytes) -> None:
    parent = _prepare_parent(root, relative)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(parent / relative.split("/")[-1], flags, 0o644), "wb") as stream:
        stream.write(data)


def _write_replace(root: Path, relative: str, data: bytes) -> None:
    if _state(root, relative) == "invalid":
        raise MbError(f"cannot write {relative}: it is not a regular file reached without symlinks", reason="template")
    parent = _prepare_parent(root, relative)
    descriptor, temporary = tempfile.mkstemp(prefix=".mod-base-sync-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, parent / relative.split("/")[-1])
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def _bounded(text: str) -> str:
    cleaned = "".join(character if character in "\n\t" or 32 <= ord(character) != 127 else "?" for character in text)
    if len(cleaned) > MAX_DETAIL_CHARS:
        cleaned = cleaned[: MAX_DETAIL_CHARS - 20] + "\n... (diff truncated)"
    return cleaned


def _diff(path: str, expected: bytes, actual: bytes) -> str:
    before = expected.decode("utf-8", errors="replace").splitlines(keepends=True)
    after = actual.decode("utf-8", errors="replace").splitlines(keepends=True)
    lines = difflib.unified_diff(before, after, fromfile=f"mod-base/template/{path}", tofile=path, n=2)
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    return _bounded(text or "(the files differ only in line endings or bytes that do not decode as UTF-8)\n")


# -- Manifest --------------------------------------------------------------------------------------


def _caller_record(entry: dict[str, Any]) -> RenderedCaller | None:
    """The registry record the manifest ``entry`` names, or ``None`` for any other file.

    An entry that names an enrolled path or source in any spelling must be that record exactly, of
    class ``managed``; and only a caller without activation modes may be a manifest entry at all,
    so manifest data can never make a Build/E2E caller a file every mod must hold.
    """

    for record in RENDERED_CALLERS:
        if entry["path"].casefold() == record.path.casefold() or entry["source"].casefold() == record.source.casefold():
            if entry["path"] != record.path or entry["source"] != record.source or entry["class"] != "managed":
                raise MbError("rendered caller path/source/class differs from the closed registry", reason="template")
            if record.modes is not None:
                raise MbError(f"{record.path} is managed by activation mode and enrolled in the code registry "
                              "alone; it must not be a template manifest entry", reason="template")
            return record
    return None


def _pin_scanned(path: str) -> bool:
    """Whether ``path`` is a file the pin parser reads, in any letter case: a workflow, or an
    ``action.yml``/``action.yaml`` below ``.github/actions``."""

    folded = path.casefold()
    return folded.startswith(".github/workflows/") or (
        folded.startswith(".github/actions/") and folded.rsplit("/", 1)[1] in ACTION_FILES)


def _read_activation(repo: Path) -> tuple[bytes, dict[str, Any]] | None:
    """``(bytes, validated document)`` of the mod's activation manifest, bound to its Build
    configuration, or ``None`` for a mod that has neither file."""

    state = _state(repo, ACTIVATION_PATH)
    configured = _state(repo, BUILD_CONFIG_PATH)
    if state == "absent":
        if configured == "absent":
            return None
        raise MbError(f"{BUILD_CONFIG_PATH} exists without {ACTIVATION_PATH}: a Build configuration needs its "
                      "activation manifest (the disabled mode manages no caller)", reason="template")
    if state != "file":
        raise MbError(f"{ACTIVATION_PATH} must be a regular file reached without symlinks", reason="template")
    data = read_regular_file(repo / ACTIVATION_PATH, label=ACTIVATION_PATH, max_bytes=lim.MAX_CI_ACTIVATION_BYTES)
    document = parse_activation(data)
    if configured != "file":
        raise MbError(f"{ACTIVATION_PATH} needs {BUILD_CONFIG_PATH} as a regular file reached without symlinks",
                      reason="template")
    raw = read_regular_file(repo / BUILD_CONFIG_PATH, label=BUILD_CONFIG_PATH, max_bytes=lim.MAX_CI_CONFIG_BYTES)
    config = validate_build_config(strict_loads(raw, label=BUILD_CONFIG_PATH, max_bytes=lim.MAX_CI_CONFIG_BYTES))
    if (document["repository"], document["profile"]) != (config["repository"], config["profile"]):
        raise MbError(f"{ACTIVATION_PATH} names another repository or profile than {BUILD_CONFIG_PATH}",
                      reason="template")
    return data, document


def load_template_activation(repo: Path) -> dict[str, Any] | None:
    """The mod's validated activation manifest, or ``None`` for a mod that never adopted the shared
    Build/E2E (it has neither the manifest nor a Build configuration).

    The manifest is read bounded and without following symlinks and must name the repository and
    profile of ``scripts/ci/mod-base-build.json``. A Build configuration without a manifest is an
    error: the manifest is the only thing that says which callers are managed, so its absence is
    never taken for ``disabled`` once a mod is configured. ``check``, ``sync`` and ``init`` call
    this before they read the template manifest or write anything.
    """

    found = _read_activation(_real_directory(Path(os.path.abspath(repo)), "repository"))
    return None if found is None else found[1]


def activation_bytes(repo: Path) -> bytes | None:
    """The bytes of the manifest :func:`load_template_activation` accepts (``None`` where it
    returns ``None``): what a transition is admitted from."""

    found = _read_activation(_real_directory(Path(os.path.abspath(repo)), "repository"))
    return None if found is None else found[0]


def load_manifest(kit_root: Path) -> dict[str, Any]:
    """Read and validate ``template/manifest.json`` (``mod-base.template-manifest`` v1) and the
    kit's rendered-caller registry with it: every enrolled template is well formed for its
    renderer, and no other template the pin parser would read holds a pin placeholder."""

    kit_root = Path(os.path.abspath(kit_root))
    raw = _read(kit_root, MANIFEST_PATH, "template manifest")
    document = strict_loads(raw, label=MANIFEST_PATH, max_bytes=lim.MAX_TEMPLATE_MANIFEST_BYTES)
    try:
        validate_template_manifest(document)
    except DocumentError as exc:
        raise MbError(f"{MANIFEST_PATH}: {exc}", reason="template") from None
    _check_registry(kit_root)
    for entry in document["files"]:
        record = _caller_record(entry)
        if _state(kit_root / "template", entry["source"]) != "file":
            raise MbError(f"{MANIFEST_PATH}: template/{entry['source']} is not a regular file", reason="template")
        if record is None and _pin_scanned(entry["path"]):
            data = _template_bytes(kit_root, entry["source"])
            if any(token.encode("ascii") in data for token in (PIN_PLACEHOLDER, VERSION_PLACEHOLDER)):
                raise MbError(f"{entry['path']} is not an enrolled caller, so its template may not hold "
                              "{{PIN}}/{{VERSION}}: it would reach the mod unrendered", reason="template")
    return document


def _check_registry(kit_root: Path) -> None:
    """Require :data:`RENDERED_CALLERS` to be coherent and every enrolled template to be well formed.

    Each record has a known renderer and a path and source no other record claims in any spelling.
    A caller with activation modes names rows of the activation table and is rendered whole
    (``pinned``): a Build/E2E caller never carries a mod-local extension region.
    """

    paths: set[str] = set()
    sources: set[str] = set()
    for record in RENDERED_CALLERS:
        renderer = _renderer(record)
        if record.path.casefold() in paths or record.source.casefold() in sources:
            raise MbError(f"rendered caller {record.path} is enrolled twice", reason="template")
        paths.add(record.path.casefold())
        sources.add(record.source.casefold())
        if record.modes is not None:
            if not record.modes or not record.modes <= set(MANAGED_CALLERS):
                raise MbError(f"rendered caller {record.path} names an unknown activation mode", reason="template")
            if record.renderer != "pinned":
                raise MbError(f"rendered caller {record.path} is managed by activation mode, so it is rendered "
                              "whole: it can carry no extension region", reason="template")
        renderer.text(kit_root, record.source)


def _template_bytes(kit_root: Path, source: str) -> bytes:
    return _read(Path(os.path.abspath(kit_root)) / "template", source, "template source")


def _entries(manifest: dict[str, Any], klass: str) -> list[dict[str, Any]]:
    return [entry for entry in manifest["files"] if entry["class"] == klass]


# -- The caller ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Caller:
    managed: str
    begin: str
    body: tuple[str, ...]


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _split_caller(text: str) -> _Caller:
    """Split a caller into its regions or raise ``ValueError`` naming the structural problem."""

    lines = _lines(text)
    if not lines or not lines[0].startswith(MANAGED_BEGIN):
        raise ValueError(f"the first line must start with {MANAGED_BEGIN!r}")
    starts = [index for index, line in enumerate(lines) if line.startswith(MANAGED_BEGIN)]
    ends = [index for index, line in enumerate(lines) if line == MANAGED_END]
    begins = [index for index, line in enumerate(lines) if line.startswith(EXTENSION_BEGIN)]
    closes = [index for index, line in enumerate(lines) if line == EXTENSION_END]
    if len(starts) != 1 or len(ends) != 1 or len(begins) != 1 or len(closes) != 1:
        raise ValueError("each region marker must appear exactly once")
    end, begin, close = ends[0], begins[0], closes[0]
    if begin != end + 1:
        raise ValueError("the extension region must start right after the managed region")
    if close != len(lines) - 1:
        raise ValueError("nothing may follow the extension region, and its end marker must end with a newline")
    return _Caller("".join(lines[: end + 1]), lines[begin], tuple(lines[begin + 1: close]))


def _caller_template(kit_root: Path, source: str) -> _Caller:
    try:
        template = _split_caller(_template_bytes(kit_root, source).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MbError(f"the kit's caller template is malformed: {exc}", reason="template") from None
    if template.body or PIN_PLACEHOLDER not in template.managed or VERSION_PLACEHOLDER not in template.managed:
        raise MbError("the kit's caller template must hold {{PIN}}/{{VERSION}} and an empty extension region",
                      reason="template")
    return template


def _render_managed(template: _Caller, pin: Pin) -> str:
    return template.managed.replace(PIN_PLACEHOLDER, pin.sha).replace(VERSION_PLACEHOLDER, pin.version)


def _pages_text(kit_root: Path, source: str) -> str:
    return _caller_template(kit_root, source).managed


def _pinned_template(kit_root: Path, source: str) -> str:
    """The text of a caller template that is rendered whole: UTF-8 with LF line endings and a final
    newline, holding both pin placeholders and no region marker, since no part of it is the mod's."""

    try:
        text = _template_bytes(kit_root, source).decode("utf-8")
    except UnicodeDecodeError:
        raise MbError(f"the kit's caller template {source} is not UTF-8", reason="template") from None
    if "\r" in text or not text.endswith("\n") or PIN_PLACEHOLDER not in text or VERSION_PLACEHOLDER not in text:
        raise MbError(f"the kit's caller template {source} must hold {{{{PIN}}}}/{{{{VERSION}}}} and end every "
                      "line with LF", reason="template")
    if any(line.startswith(_REGION_MARKERS) for line in text.split("\n")):
        raise MbError(f"the kit's caller template {source} is rendered whole and can hold no region marker",
                      reason="template")
    return text


def _render_pinned(kit_root: Path, record: RenderedCaller, pin: Pin, current: bytes | None) -> bytes:
    """The whole caller rendered for ``pin``; nothing of the mod's ``current`` file is kept."""

    text = _pinned_template(kit_root, record.source)
    return text.replace(PIN_PLACEHOLDER, pin.sha).replace(VERSION_PLACEHOLDER, pin.version).encode("utf-8")


def _scan(line: str) -> tuple[str, bool, int]:
    """``(line without its comment, a quoted scalar is still open, flow-bracket balance)``.

    A quote opens a scalar only where YAML starts a token (at the start of the line or after
    ``:``, ``-``, ``?``, ``[``, ``{`` or ``,`` and whitespace), so an apostrophe inside a plain
    scalar is text. Inside a double-quoted scalar a backslash escapes the next character; inside a
    single-quoted one ``''`` is a quote.
    """

    quote = ""
    previous = ""
    depth = 0
    index = 0
    while index < len(line):
        character = line[index]
        if quote == "'":
            if character == "'":
                if line[index + 1:index + 2] == "'":
                    index += 2
                    continue
                quote = ""
            index += 1
            continue
        if quote == '"':
            if character == "\\":
                index += 2
                continue
            if character == '"':
                quote = ""
            index += 1
            continue
        if character in "\"'" and previous in ("", ":", "-", "?", "[", "{", ",") and (
                index == 0 or line[index - 1] in " [{,"):
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] == " "):
            return line[:index], False, depth
        elif character in "{[":
            depth += 1
        elif character in "}]":
            depth -= 1
        if character != " ":
            previous = character
        index += 1
    return line, bool(quote), depth


def _key_column(content: str) -> int:
    """The column of the node a block scalar header on ``content`` belongs to: the key, or the
    list dash when the scalar is the list item itself. Its content must be indented further."""

    column = len(content) - len(content.lstrip(" "))
    rest = content[column:]
    while rest[:1] == "-" and rest[1:2] in (" ", ""):
        item = rest[1:].lstrip(" ")
        if item[:1] in ("|", ">"):
            return column
        column += len(rest) - len(item)
        rest = item
    return column


def _aggregated_key_problem(job: str | None, key: str, value: str) -> str | None:
    """The rule a job-level ``needs``/``name`` value (with its continuation lines) breaks, if any."""

    if key == "needs":
        if "*" in value or "&" in value:
            return f"{job}: needs must list job ids, not YAML aliases"
        if "rotate" in _JOB_ID_WORD.findall(value):
            return f"{job}: extension jobs never need rotate"
        return None
    display = value.strip().strip("\"'")
    if display[:1] in ("|", ">") or "/" in display or "${{" in display or display in CALLER.values():
        return f"{job}: its display name could impersonate a mod-base job"
    return None


@dataclass
class _Entry:
    """A job-level key with every deeper line of its value (continuations, nested mappings and
    block scalar content), checked as one text so no rule can be split across lines."""

    key: str
    value: list[str]
    texts: list[str]
    depth: int
    reported: set[str]


def extension_violations(body: tuple[str, ...] | list[str]) -> list[str]:
    """Every extension-region rule (SPEC §5.2) the extension ``body`` lines break.

    The region is read with a deliberately small YAML subset, so that what the rules see is what
    GitHub parses: outside block scalars there are no backslashes (escapes), explicit keys,
    anchors or aliases, and no quoted scalar or flow collection continues past its job-level key;
    job-level lines are plain ``key:`` entries; block scalars take no indentation indicator. Every
    line is checked on its own, and each job-level key is checked again with all its deeper lines
    joined, which catches a permission or ``needs`` written across lines.
    """

    problems: list[str] = []
    seen: set[str] = set()
    job: str | None = None
    key_indent: int | None = None
    entry: _Entry | None = None
    #: ``[header column, content indentation]`` of an open block scalar.
    block: list[int] | None = None

    def report(message: str, rule: str = "") -> None:
        problems.append(message)
        if entry is not None and rule:
            entry.reported.add(rule)

    def close_entry() -> None:
        nonlocal entry
        if entry is None:
            return
        closing, entry = entry, None
        joined = " ".join(closing.texts)
        for pattern, message in _FORBIDDEN_EXTENSION_TEXT:
            if message not in closing.reported and pattern.search(joined):
                problems.append(f"{job}: its {closing.key} entry {message}")
        if closing.depth != 0:
            problems.append(f"{job}: its {closing.key} entry leaves a flow collection open or unbalanced")
        if closing.key in ("needs", "name"):
            problem = _aggregated_key_problem(job, closing.key, " ".join(closing.value))
            if problem is not None:
                problems.append(problem)

    for number, raw in enumerate(body, start=1):
        line = raw[:-1] if raw.endswith("\n") else raw
        where = f"extension line {number}"
        if _HIDDEN_BREAKS.search(line):
            problems.append(f"{where}: tabs, carriage returns, control characters and Unicode line separators "
                            "are not allowed")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block is not None:
            if not line.strip():
                continue
            if indent > block[0] and (block[1] < 0 or indent >= block[1]):
                if block[1] < 0:
                    block[1] = indent
                for pattern, message in _FORBIDDEN_EXTENSION_TEXT:
                    if pattern.search(line):
                        report(f"{job}: {where} {message}", message)
                if entry is not None:
                    entry.texts.append(line.strip())
                    entry.value.append(line.strip())
                continue
            block = None
        if "\\" in line:
            report(f"{where}: a backslash (a YAML escape) is allowed only inside a block scalar")
        content, quoted, depth = _scan(line)
        content = content.rstrip()
        if not content.strip():
            continue
        if quoted:
            report(f"{where}: a quoted scalar must close on its own line")
        if indent < 2:
            close_entry()
            problems.append(f"{where}: extension content must stay inside the jobs mapping")
            job = None
            continue
        if indent == 2:
            close_entry()
            match = _EXTENSION_JOB.match(content)
            if match is None:
                named = _ANY_JOB.match(content)
                label = named.group(1) if named else content.strip()
                problems.append(f"{where}: {single_line(label, limit=80)!r} is not a block-mapping job whose id "
                                "matches ^ext-[a-z0-9-]+$")
                job = None
                continue
            job, key_indent = match.group(1), None
            if job in seen:
                problems.append(f"{where}: duplicate extension job {job}")
            seen.add(job)
            continue
        if job is None:
            problems.append(f"{where}: content outside an ext- job (it would extend a managed job)")
            continue
        if key_indent is None:
            key_indent = indent
        if indent <= key_indent:
            close_entry()
            match = _JOB_KEY.match(content)
            if indent < key_indent or match is None:
                report(f"{job}: {where} is not a plain job-level key at the job's indentation")
            else:
                entry = _Entry(match.group(1), [match.group(2) or ""], [content.strip()], 0, set())
        elif entry is not None:
            entry.texts.append(content.strip())
            entry.value.append(content.strip())
        if entry is not None:
            entry.depth += depth
            if entry.depth < 0:
                report(f"{job}: {where} closes a flow collection it never opened")
        for pattern, message in _FORBIDDEN_EXTENSION_TEXT:
            if pattern.search(content):
                report(f"{job}: {where} {message}", message)
        if _ANCHOR_OR_ALIAS.search(content):
            report(f"{job}: {where} uses a YAML anchor or alias")
        if _EXPLICIT_KEY.match(content):
            report(f"{job}: {where} uses an explicit YAML key (? or :)")
        if _NEEDS_WORD.search(content) and _ROTATE_WORD.search(content):
            report(f"{job}: {where} refers to the rotate job")
        header = _BLOCK_HEADER.search(content)
        if header is not None:
            if any(character.isdigit() for character in header.group(1)):
                report(f"{job}: {where} gives a block scalar an indentation indicator")
            block = [_key_column(content), -1]
    close_entry()
    return list(dict.fromkeys(problems))


def _managed_drifts(path: str, expected: bytes, actual: bytes, advice: str = CRLF_ADVICE) -> list[Drift]:
    """The drifts of the managed file ``path``: none when byte-identical; CRLF line endings are
    reported with ``advice``, and whatever else differs as the diff of the LF form."""

    if actual == expected:
        return []
    if b"\r\n" not in actual:
        return [Drift(path, "changed", _diff(path, expected, actual))]
    normalized = actual.replace(b"\r\n", b"\n")
    drifts = [Drift(path, "changed", advice)]
    if normalized != expected:
        drifts.append(Drift(path, "changed", _diff(path, expected, normalized)))
    return drifts


def _pages_drifts(repo: Path, kit_root: Path, record: RenderedCaller, actual: bytes) -> list[Drift]:
    """The caller's drifts; CRLF line endings are reported with :data:`CRLF_ADVICE` and the rest is
    checked on the LF form, since no region marker matches a CRLF line."""

    if b"\r\n" not in actual:
        return _region_drifts(repo, kit_root, record, actual)
    return [Drift(record.path, "changed", CRLF_ADVICE),
            *_region_drifts(repo, kit_root, record, actual.replace(b"\r\n", b"\n"))]


def _region_drifts(repo: Path, kit_root: Path, record: RenderedCaller, actual: bytes) -> list[Drift]:
    path = record.path
    template = _caller_template(kit_root, record.source)
    try:
        caller = _split_caller(actual.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return [Drift(path, "changed", f"the caller is not the managed region plus an extension region: {exc}")]
    drifts: list[Drift] = []
    try:
        pin = parse_pin(repo)
    except MbError as exc:
        drifts.append(Drift(path, "changed", f"cannot determine the mod's single pin: {single_line(exc)}"))
    else:
        expected = _render_managed(template, pin) + template.begin
        observed = caller.managed + caller.begin
        if observed != expected:
            drifts.append(Drift(path, "changed", _diff(path, expected.encode("utf-8"), observed.encode("utf-8"))))
    drifts.extend(Drift(path, "extension", problem) for problem in extension_violations(caller.body))
    return drifts


def _pinned_drifts(repo: Path, kit_root: Path, record: RenderedCaller, actual: bytes) -> list[Drift]:
    """The drifts of a caller that is rendered whole: any byte that is not the template rendered
    with the mod's single pin, so nothing a mod adds to it (an ``ext-`` job least of all) passes."""

    try:
        pin = parse_pin(repo)
    except MbError as exc:
        return [Drift(record.path, "changed", f"cannot determine the mod's single pin: {single_line(exc)}")]
    return _managed_drifts(record.path, _render_pinned(kit_root, record, pin, None), actual, ACTIVATION_CRLF_ADVICE)


def _render_pages(kit_root: Path, record: RenderedCaller, pin: Pin, current: bytes | None) -> bytes:
    """The caller ``sync`` writes: the managed region rendered for ``pin`` and the extension region
    of the mod's ``current`` caller, read with LF line endings (a CRLF checkout is rewritten with LF)."""

    template = _caller_template(kit_root, record.source)
    body: tuple[str, ...] = ()
    if current is not None:
        try:
            body = _split_caller(current.decode("utf-8").replace("\r\n", "\n")).body
        except (UnicodeDecodeError, ValueError) as exc:
            raise MbError(f"cannot preserve the extension region of {record.path}: {exc}; repair it by hand",
                          reason="template") from None
    return (_render_managed(template, pin) + template.begin + "".join(body) + EXTENSION_END).encode("utf-8")


@dataclass(frozen=True)
class _Renderer:
    """One way of rendering a caller: ``text`` reads the managed text of its kit template (and
    refuses a malformed one), ``render`` gives the bytes ``sync`` and ``init`` write for a pin and
    the mod's current file, and ``drifts`` gives what ``check`` reports for the mod's file."""

    text: Callable[[Path, str], str]
    render: Callable[[Path, RenderedCaller, Pin, bytes | None], bytes]
    drifts: Callable[[Path, Path, RenderedCaller, bytes], list[Drift]]


_RENDERERS = {
    "pages-extension": _Renderer(_pages_text, _render_pages, _pages_drifts),
    "pinned": _Renderer(_pinned_template, _render_pinned, _pinned_drifts),
}
#: Every renderer a :class:`RenderedCaller` may name: a managed region followed by a region of
#: mod-local ``ext-`` jobs, or the whole file with nothing of the mod's.
RENDERERS = tuple(_RENDERERS)


def _renderer(record: RenderedCaller) -> _Renderer:
    """The renderer ``record`` names. This lookup is the only dispatch on a renderer, so a caller of
    an unknown kind is an error everywhere and never passes for a byte-identical managed file."""

    renderer = _RENDERERS.get(record.renderer)
    if renderer is None:
        raise MbError(f"rendered caller {record.path} names the unknown renderer "
                      f"{single_line(record.renderer, limit=60)!r}; the renderers are {', '.join(RENDERERS)}",
                      reason="template")
    return renderer


def _activation_callers(activation: dict[str, Any] | None) -> list[tuple[RenderedCaller, bool]]:
    """``(record, whether the mod's activation manages it)`` for every caller enrolled with
    activation modes, in registry order."""

    mode = managed_mode(activation)
    return [(record, mode in record.modes) for record in RENDERED_CALLERS if record.modes is not None]


def expected_callers(kit_root: Path, pin: Pin, activation: dict[str, Any] | None) -> dict[str, bytes | None]:
    """What every Build/E2E caller path must hold in a mod whose validated activation manifest is
    ``activation`` (``None``: it has none) and whose pin is ``pin``: the kit template of
    ``kit_root`` rendered with that pin where the mode manages the caller, ``None`` where the file
    must not exist. ``kit_root`` must be the kit that ``pin`` names."""

    return {record.path: _renderer(record).render(Path(kit_root), record, pin, None) if managed else None
            for record, managed in _activation_callers(activation)}


# -- Fragments -------------------------------------------------------------------------------------


def _codeowners(path: str, lines: list[str], patterns: list[str]) -> list[Drift]:
    drifts: list[Drift] = []
    rules: dict[str, list[str]] = {}
    for number, line in enumerate(lines, start=1):
        tokens = line.split()
        if not tokens or tokens[0].startswith("#"):
            continue
        owners = []
        for token in tokens[1:]:
            if token.startswith("#"):
                break
            owners.append(token)
        if not owners or not all(_OWNER_TOKEN.match(owner) for owner in owners):
            drifts.append(Drift(path, "fragment", f"line {number}: every rule needs at least one valid owner "
                                                  f"(an owner-less rule removes ownership): "
                                                  f"{single_line(line, limit=200)}"))
            continue
        rules.setdefault(tokens[0], []).extend(owners)
    for pattern in patterns:
        if pattern not in rules:
            drifts.append(Drift(path, "fragment", f"no rule gives {pattern} an owner"))
    return drifts


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _meaningful(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _block_items(lines: list[str]) -> list[list[str]]:
    """Split block-sequence ``lines`` into items at the indentation of the first ``- ``."""

    items: list[list[str]] = []
    dash: int | None = None
    for line in lines:
        if not _meaningful(line):
            continue
        stripped = line.lstrip(" ")
        if stripped.startswith("- ") or stripped == "-":
            if dash is None:
                dash = _indent(line)
            if _indent(line) == dash:
                items.append([line])
                continue
        if items:
            items[-1].append(line)
    return items


def pinned_actions(kit_root: Path, manifest: dict[str, Any], entry: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(dependency name, workflow)`` of every third-party action pinned in the managed region of
    each managed workflow the fragment ``entry`` lists in ``ignore_actions_of`` (the whole template
    for a managed workflow without a caller region), read from the kit's template; the kit's own
    references are excluded. Sorted and unique by name, the first listing workflow kept."""

    entries = {item["path"]: item for item in _entries(manifest, "managed")}
    found: dict[str, str] = {}
    for workflow in entry.get("ignore_actions_of", ()):
        record = _caller_record(entries[workflow])
        if record is not None:
            text = _renderer(record).text(kit_root, record.source)
        else:
            try:
                text = _template_bytes(kit_root, entries[workflow]["source"]).decode("utf-8")
            except UnicodeDecodeError:
                raise MbError(f"the kit's template of {workflow} is not UTF-8", reason="template") from None
        _collect_actions(found, workflow, text)
    return tuple(sorted(found.items()))


def _collect_actions(found: dict[str, str], workflow: str, text: str) -> None:
    """Add the third-party actions ``text`` pins to ``found`` (name -> the first workflow listing it)."""

    for line in text.split("\n"):
        match = _USES.match(line)
        if match is not None and match.group(1).lower() != mod_base.KIT_REPOSITORY.lower():
            found.setdefault(match.group(1), workflow)


def _ignored_actions(kit_root: Path, manifest: dict[str, Any], entry: dict[str, Any],
                     activation: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """What the Dependabot fragment ``entry`` must ignore in this mod: :func:`pinned_actions` and the
    third-party actions of every Build/E2E caller the mod's activation manages."""

    found = dict(pinned_actions(kit_root, manifest, entry))
    for record, managed in _activation_callers(activation):
        if managed:
            _collect_actions(found, record.path, _renderer(record).text(kit_root, record.source))
    return tuple(sorted(found.items()))


def _dependabot(path: str, lines: list[str], actions: tuple[tuple[str, str], ...]) -> list[Drift]:
    names = (DEPENDABOT_IGNORE, *(name for name, _workflow in actions))
    starts = [index for index, line in enumerate(lines) if _UPDATES_KEY.match(line)]
    if len(starts) != 1:
        return [Drift(path, "fragment", "expected exactly one top-level updates: list")]
    block: list[str] = []
    for line in lines[starts[0] + 1:]:
        if _meaningful(line) and _indent(line) == 0 and not line.startswith("-"):
            break
        block.append(line)
    updates = [item for item in _block_items(block)
               if any((match := _ECOSYSTEM.match(line)) and match.group(1) == "github-actions" for line in item)]
    if not updates:
        return [Drift(path, "fragment", f"no github-actions update: its ignore list must hold {', '.join(names)}")]
    drifts = []
    for item in updates:
        ignored = _ignored(item)
        if DEPENDABOT_IGNORE not in ignored:
            drifts.append(Drift(path, "fragment", f"every github-actions update must ignore {DEPENDABOT_IGNORE} "
                                                  "(a dependency-name entry with no versions or update-types)"))
        for name, workflow in actions:
            if name not in ignored:
                drifts.append(Drift(path, "fragment", f"every github-actions update must ignore {name}, which the "
                                                      f"managed region of {workflow} pins (a dependency-name entry "
                                                      "with no versions or update-types)"))
    return drifts


def _flow_entry_name(entry: str) -> str | None:
    keys: dict[str, str] = {}
    for part in entry.split(","):
        key, separator, value = part.partition(":")
        if not separator:
            return None
        keys[key.strip().strip("\"'")] = value.strip().strip("\"'")
    return keys["dependency-name"] if set(keys) == {"dependency-name"} else None


def _ignored(item: list[str]) -> set[str]:
    """The dependencies the update ``item`` ignores for every version: each ``ignore`` entry that is
    exactly ``{dependency-name: <name>}``, in block or single-line flow style."""

    names: set[str] = set()
    for index, line in enumerate(item):
        flow = _IGNORE_FLOW.match(line)
        if flow is not None:
            names.update(name for name in map(_flow_entry_name, _FLOW_ENTRY.findall(flow.group(1))) if name)
            continue
        match = _IGNORE_KEY.match(line)
        if match is None:
            continue
        indent = len(match.group(1))
        block = []
        for following in item[index + 1:]:
            if _meaningful(following) and (_indent(following) < indent or (
                    _indent(following) == indent and not following.lstrip(" ").startswith("-"))):
                break
            block.append(following)
        for entry in _block_items(block):
            keys = {}
            for entry_line in entry:
                key = _ENTRY_KEY.match(entry_line)
                if key is not None:
                    keys[key.group(1)] = key.group(2).strip("\"'")
            if set(keys) == {"dependency-name"} and keys["dependency-name"]:
                names.add(keys["dependency-name"])
    return names


def _agents(repo: Path, text: str, config: Config) -> list[Drift]:
    local = list(config.template["agents_local"])
    expected = "".join(f"@{path}\n" for path in (*SHARED_IMPORTS, *local))
    drifts: list[Drift] = []
    if text != expected:
        drifts.append(Drift(AGENTS_PATH, "agents", _diff(AGENTS_PATH, expected.encode("utf-8"), text.encode("utf-8"))))
    for path in local:
        if path in SHARED_IMPORTS:
            drifts.append(Drift(AGENTS_PATH, "agents", f"{path} is already a shared import"))
    for path in dict.fromkeys((*SHARED_IMPORTS, *local)):
        if _state(repo, path) != "file":
            drifts.append(Drift(AGENTS_PATH, "agents", f"the imported {path} is not a regular file"))
    return drifts


def _check_fragment(repo: Path, entry: dict[str, Any], data: bytes, config: Config,
                    actions: tuple[tuple[str, str], ...] = ()) -> tuple[list[Drift], list[Drift]]:
    """``(missing required lines, every other drift)`` of a present fragment file (``actions``: the
    :func:`pinned_actions` of the Dependabot fragment)."""

    path = entry["path"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [], [Drift(path, "fragment", "not UTF-8 text")]
    lines = [line.rstrip("\r") for line in text.split("\n")]
    missing = [Drift(path, "fragment", f"missing the required line {required!r}")
               for required in entry.get("lines", ()) if required not in lines]
    drifts: list[Drift] = []
    if path == CODEOWNERS_PATH:
        drifts.extend(_codeowners(path, lines, entry.get("markers", [])))
    else:
        for marker in entry.get("markers", ()):
            if marker not in text:
                drifts.append(Drift(path, "fragment", f"missing the required marker {marker!r}"))
    if path == DEPENDABOT_PATH:
        drifts.extend(_dependabot(path, lines, actions))
    if path == AGENTS_PATH:
        drifts.extend(_agents(repo, text, config))
    return missing, drifts


# -- Managed-docs link rule ------------------------------------------------------------------------


def _without_code(text: str) -> str:
    kept: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        match = _FENCE.match(line)
        if fence is not None:
            if match is not None and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            kept.append("")
            continue
        if match is not None:
            fence = match.group(1)
            kept.append("")
            continue
        kept.append(_INLINE_CODE.sub("", line))
    return "\n".join(kept)


def link_violations(document: str, text: str, managed_documents: frozenset[str] | set[str]) -> list[str]:
    """Links of the managed Markdown ``document`` that point anywhere but a managed document or an
    absolute ``https://`` URL."""

    prose = _without_code(text)
    targets = [match.group(1) for pattern in (_INLINE_LINK, _ANGLE_LINK, _REFERENCE_DEFINITION, _AUTOLINK)
               for match in pattern.finditer(prose)]
    targets.extend(next(group for group in match.groups() if group is not None)
                   for match in _HTML_LINK.finditer(prose))
    problems = [f"{document}: insecure or bare link {single_line(match.group(0), limit=120)!r}"
                for match in _BARE_INSECURE.finditer(prose)]
    parsed = {match.start() for pattern in (_INLINE_LINK, _ANGLE_LINK) for match in pattern.finditer(prose)}
    problems.extend(f"{document}: link {single_line(prose[opener.start():opener.start() + 60], limit=80)!r} "
                    "has a destination the link rule cannot parse"
                    for opener in _LINK_OPENER.finditer(prose) if opener.start() not in parsed)
    for target in targets:
        if target.startswith("https://"):
            if not _HTTPS_URL.match(target):
                problems.append(f"{document}: malformed https link {single_line(target, limit=120)!r}")
            continue
        if target.startswith("#") and len(target) > 1:
            continue
        path = target.split("#", 1)[0]
        if not path or _SCHEME.match(target) or path.startswith(("/", "\\")) or "?" in path or "\\" in path:
            problems.append(f"{document}: link {single_line(target, limit=120)!r} is neither a managed document "
                            "nor an absolute https URL")
            continue
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(document), path))
        if resolved not in managed_documents:
            problems.append(f"{document}: link {single_line(target, limit=120)!r} leaves the managed documents")
    return list(dict.fromkeys(problems))


def _managed_documents(manifest: dict[str, Any]) -> frozenset[str]:
    return frozenset(entry["path"] for entry in _entries(manifest, "managed") if entry["path"].endswith(".md"))


# -- check / sync / init ---------------------------------------------------------------------------


def _deferred_drifts(manifest: dict[str, Any], deferred: set[str]) -> list[Drift]:
    checked = {entry["path"] for entry in manifest["files"] if entry["class"] in ("managed", "fragment")}
    return [Drift(path, "forbidden", "template.deferred may name only "
                                     f"{', '.join(sorted(DEFERRABLE & checked))}")
            for path in sorted(deferred - (DEFERRABLE & checked))]


def evaluate(repo: Path, *, kit_root: Path) -> tuple[list[Drift], list[Drift]]:
    """``(failing drifts, pending drifts)`` of ``repo``: a present deferred fragment's missing
    required lines are pending; every other drift fails."""

    repo = _real_directory(Path(os.path.abspath(repo)), "repository")
    activation = load_template_activation(repo)
    manifest = load_manifest(kit_root)
    config = load_config(repo, check_repository_facts=False)
    deferred = set(config.template["deferred"])
    staged = deferred & DEFERRABLE
    documents = _managed_documents(manifest)
    drifts = _deferred_drifts(manifest, deferred)
    pending: list[Drift] = []
    for entry in manifest["files"]:
        path, klass = entry["path"], entry["class"]
        if klass == "seeded":
            continue
        state = _state(repo, path)
        if state == "absent":
            if path not in staged:
                drifts.append(Drift(path, "missing", f"{path} is missing ({klass}); run template sync --write "
                                                     "or seed it"))
            continue
        if state == "invalid":
            drifts.append(Drift(path, "changed", f"{path} must be a regular file reached without symlinks"))
            continue
        actual = _read(repo, path, klass)
        if klass == "managed":
            record = _caller_record(entry)
            if record is not None:
                drifts.extend(_renderer(record).drifts(repo, Path(kit_root), record, actual))
            else:
                drifts.extend(_managed_drifts(path, _template_bytes(kit_root, entry["source"]), actual))
            if path in documents:
                drifts.extend(Drift(path, "links", problem)
                              for problem in link_violations(path, actual.decode("utf-8", errors="replace"), documents))
        else:
            actions = _ignored_actions(kit_root, manifest, entry, activation) if path == DEPENDABOT_PATH else ()
            missing, other = _check_fragment(repo, entry, actual, config, actions)
            (pending if path in staged else drifts).extend(missing)
            drifts.extend(other)
    if activation is not None:
        drifts.extend(_activation_drifts(repo, Path(kit_root), activation))
    for path in FORBIDDEN_PATHS:
        if _state(repo, path) != "absent":
            drifts.append(Drift(path, "forbidden", f"{path} must not exist: Claude Code reads AGENTS.md only while "
                                                   "no CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md exists"))
    return drifts, pending


def _activation_drifts(repo: Path, kit_root: Path, activation: dict[str, Any]) -> list[Drift]:
    """The drifts of the Build/E2E callers of a mod with an activation manifest: a caller its mode
    manages is checked like a managed file, and any other must not exist. No deferral applies."""

    drifts: list[Drift] = []
    for record, managed in _activation_callers(activation):
        path = record.path
        state = _state(repo, path)
        if not managed:
            if state != "absent":
                drifts.append(Drift(path, "forbidden", f"{path} must not exist in the {activation['mode']} activation "
                                                       "mode, which does not manage it: a mod-base Build/E2E caller "
                                                       "is either managed and checked or absent; remove it"))
        elif state == "absent":
            drifts.append(Drift(path, "missing", f"{path} is missing (managed in the {activation['mode']} activation "
                                                 "mode); run template sync --write"))
        elif state == "invalid":
            drifts.append(Drift(path, "changed", f"{path} must be a regular file reached without symlinks"))
        else:
            drifts.extend(_renderer(record).drifts(repo, kit_root, record, _read(repo, path, "managed")))
    return drifts


def caller_files(repo: Path) -> dict[str, bytes]:
    """The bytes of every Build/E2E caller path that exists in ``repo``, each a bounded regular file
    reached without symlinks: what a mod holds where :func:`expected_callers` says what it must."""

    repo = _real_directory(Path(os.path.abspath(repo)), "repository")
    files: dict[str, bytes] = {}
    for record, _managed in _activation_callers(None):
        state = _state(repo, record.path)
        if state == "invalid":
            raise MbError(f"{record.path} must be a regular file reached without symlinks", reason="template")
        if state == "file":
            files[record.path] = _read(repo, record.path, "caller")
    return files


def _require_single_pin(repo: Path, writes: dict[str, bytes]) -> None:
    """Refuse to write workflow bytes that would leave ``repo`` without its single pin: an
    unrendered placeholder or a malformed kit reference in a template stops here, before any file
    changes, instead of reaching the mod."""

    scanned = {path: data for path, data in writes.items() if _pin_scanned(path)}
    if not scanned:
        return
    try:
        parse_pin_files({**read_pin_files(repo), **scanned})
    except MbError as exc:
        raise MbError(f"refusing to write {', '.join(sorted(scanned))}: the mod's workflows would no longer carry "
                      f"one pin ({single_line(exc, limit=400)})", reason="template") from None


def check(repo: Path, *, kit_root: Path) -> list[Drift]:
    return evaluate(repo, kit_root=kit_root)[0]


def pending(repo: Path, *, kit_root: Path) -> list[Drift]:
    """The required lines a present fragment in ``template.deferred`` still lacks: reported, never
    failing, until the adoption completes and ``deferred`` is emptied."""

    return evaluate(repo, kit_root=kit_root)[1]


def sync(repo: Path, *, kit_root: Path, write: bool) -> list[Drift]:
    repo = _real_directory(Path(os.path.abspath(repo)), "repository")
    activation = load_template_activation(repo)
    manifest = load_manifest(kit_root)
    config = load_config(repo, check_repository_facts=False)
    staged = set(config.template["deferred"]) & DEFERRABLE
    managed: list[tuple[str, str, RenderedCaller | None]] = [
        (entry["path"], entry["source"], _caller_record(entry)) for entry in _entries(manifest, "managed")]
    managed.extend((record.path, record.source, record)
                   for record, active in _activation_callers(activation) if active)
    planned: list[tuple[Drift, str, bytes]] = []
    for path, source, record in managed:
        state = _state(repo, path)
        if state == "absent" and path in staged:
            continue
        if state == "invalid":
            raise MbError(f"cannot sync {path}: it is not a regular file reached without symlinks", reason="template")
        actual = _read(repo, path, "managed") if state == "file" else None
        if record is not None:
            expected = _renderer(record).render(Path(kit_root), record, parse_pin(repo), actual)
        else:
            expected = _template_bytes(kit_root, source)
        if actual is None:
            planned.append((Drift(path, "missing", f"{path} would be created from the kit template"), path, expected))
        elif actual != expected:
            planned.append((Drift(path, "changed", _diff(path, expected, actual)), path, expected))
    _require_single_pin(repo, {path: data for _drift, path, data in planned})
    if write:
        for _drift, path, data in planned:
            _write_replace(repo, path, data)
    return [drift for drift, _path, _data in planned]


def _initial_pin(repo: Path, kit_root: Path) -> Pin:
    """The pin a new caller receives: the mod's own pin when it has one, else the kit checkout.

    A repository without any pin line is pinned to the HEAD of the clean kit git checkout running
    ``init``, labelled with the release tag at that commit (or the kit's ``__version__``). The label
    is re-verified by ``mod_base_kit.py verify --network`` before the repository can use it.
    """

    if any(PIN_LINE.match(line) for data in read_pin_files(repo).values()
           for line in data.decode("utf-8", errors="replace").splitlines()):
        return parse_pin(repo)
    try:
        head = checkout_head(kit_root)
        tags = release_tags_at(kit_root, head)
    except MbError as exc:
        raise MbError(f"the repository has no pin and the kit is not a clean git checkout ({single_line(exc)}): run "
                      "init from a clean mod-base checkout at a released tag", reason="template") from None
    if len(tags) > 1:
        raise MbError(f"the kit commit {head} carries several release tags: {', '.join(tags)}", reason="template")
    return Pin(head, tags[0] if tags else f"v{mod_base.__version__}", ())


def seed_values(config: Config | None) -> dict[str, str]:
    """The seed placeholders ``init`` can fill from ``config`` (everything else stays visible)."""

    local = list(config.template["agents_local"]) if config is not None else list(DEFAULT_AGENTS_LOCAL)
    values = {"agents_local": "".join(f"@{path}\n" for path in local)}
    if config is None:
        return values
    project = config.project
    values.update({"name": project["name"], "license_label": project["license_label"],
                   "canonical_branch": config.canonical_branch})
    slugs = {"modrinth": re.compile(r"https://modrinth\.com/mod/([a-z0-9_-]+)/?"),
             "curseforge": re.compile(r"https://www\.curseforge\.com/minecraft/mc-mods/([a-z0-9_-]+)/?")}
    for link in project["links"]:
        pattern = slugs.get(link["id"])
        match = pattern.fullmatch(link["url"]) if pattern is not None else None
        if match is not None:
            values[f"{link['id']}_slug"] = match.group(1)
    return values


def unresolved_placeholders(data: bytes) -> list[str]:
    """The ``{{name}}`` seed placeholders still present in seeded bytes, sorted."""

    return sorted(set(SEED_PLACEHOLDER.findall(data.decode("utf-8", errors="replace"))))


def _render_seed(text: str, values: dict[str, str]) -> str:
    return SEED_PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), text)


def _load_seed_config(repo: Path, from_config: Path | None) -> tuple[Config | None, bytes | None]:
    if from_config is not None:
        raw = read_regular_file(from_config, label="--from-config", max_bytes=lim.MAX_CONFIG_BYTES)
        return parse_config(raw, path=str(from_config)), raw
    if _state(repo, DEFAULT_CONFIG_PATH) == "file":
        return load_config(repo, check_repository_facts=False), None
    return None, None


def init(repo: Path, *, kit_root: Path, seed: bool, from_config: Path | None) -> list[str]:
    """Seed missing files; return the created paths; refuse to overwrite anything."""

    repo = _real_directory(Path(os.path.abspath(repo)), "repository")
    activation = load_template_activation(repo)
    manifest = load_manifest(kit_root)
    config, config_bytes = _load_seed_config(repo, from_config)
    values = seed_values(config)
    deferred = set(config.template["deferred"]) & DEFERRABLE if config is not None else set()
    planned: list[tuple[str, bytes]] = []
    for entry in manifest["files"]:
        path, klass, source = entry["path"], entry["class"], entry["source"]
        if (klass == "seeded" and not seed) or path in deferred or _state(repo, path) != "absent":
            continue
        record = _caller_record(entry)
        if record is not None:
            data = _renderer(record).render(Path(kit_root), record, _initial_pin(repo, kit_root), None)
        elif klass == "managed":
            data = _template_bytes(kit_root, source)
        elif path == DEFAULT_CONFIG_PATH:
            data = config_bytes if config_bytes is not None else _template_bytes(kit_root, source)
        elif path == LICENSE_PATH:
            license_source = LICENSE_TEMPLATES.get(config.project["license_label"], "") if config is not None else ""
            if not license_source:
                continue
            data = _render_seed(_template_bytes(kit_root, license_source).decode("utf-8"), values).encode("utf-8")
        else:
            data = _render_seed(_template_bytes(kit_root, source).decode("utf-8"), values).encode("utf-8")
        planned.append((path, data))
    for record, active in _activation_callers(activation):
        if active and _state(repo, record.path) == "absent":
            planned.append((record.path, _renderer(record).render(Path(kit_root), record, _initial_pin(repo, kit_root),
                                                                  None)))
    _require_single_pin(repo, dict(planned))
    for path, data in planned:
        _write_new(repo, path, data)
    return [path for path, _data in planned]
