#!/usr/bin/env python3
# mod-base managed file: edit only in The-Plum-Team/mod-base template/managed/scripts/ci/mod_base_kit.py
"""Find, verify and run the pinned mod-base kit.

This bootstrap is managed by mod-base: it is byte-identical in every mod and ``template check``
fails on any drift. It is stdlib-only, runs on Python 3.11+, and is the only way mod code finds
the kit. It makes no network call except ``verify --network``, ``stage``, ``bump`` and the
anonymous fetch fallback of kit resolution.

The pin is the single ``(sha, version)`` pair of every line of ``.github/workflows/*.y(a)ml`` and
``.github/actions/**/action.y(a)ml`` that matches :data:`PIN_LINE` (ASCII whitespace only); the
version is ``vX.Y.Z`` without leading zeros. Any other mod-base reference in those files is an
error, so a tag, a branch, a second SHA or an abbreviated pin cannot slip in. Every other line is
checked as written, with its YAML double-quoted escapes decoded, and (after an escaped line break)
joined to the next line as YAML reads it; compared case-insensitively, it may hold no
``owner/repo[/path]@ref`` token naming the kit, no ``uses:`` key together with the kit repository,
no YAML key whose value starts with the kit repository and, outside a comment-only line, no bare
kit repository name that a path does not follow (``repository: The-Plum-Team/mod-base``, a flow
value, a quoted or folded scalar). YAML folds every other line break into a space or a newline, so
no reference can be spelled across lines unseen. A ``uses:`` value must also be a plain scalar on
its own line (no backslash escape, block scalar, unterminated quote, tag, anchor or alias). Values
assembled at run time by an expression or a shell command are outside any static parser and stay
governed by review.

The kit root is resolved in this order; the first candidate that exists wins and is verified,
and a candidate that exists but fails verification is an error, never a fall-through:

1. ``<repo>/out/mod-base-kit/`` holding ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) whose
   ``sha`` equals the pin and whose ``tree_digest`` equals the recomputed kit-digest-v1 (the Block
   Pops sandbox overlay staged by ``stage``);
2. ``MOD_BASE_KIT_PATH`` with ``MOD_BASE_KIT_SHA`` equal to the pin (the ``setup`` composite
   exports both). Without ``MOD_BASE_KIT_SHA`` the path is a developer override, allowed only when
   ``MOD_BASE_ALLOW_UNPINNED=1`` and neither ``CI`` nor ``GITHUB_ACTIONS`` is set;
3. the user cache ``<cache>/mod-base/<pin>/`` outside the repository, where ``<cache>`` is
   ``$MOD_BASE_CACHE_DIR``, else ``~/Library/Caches`` (macOS), ``%LOCALAPPDATA%`` (Windows) or
   ``${XDG_CACHE_HOME:-~/.cache}``: a git worktree whose HEAD is the pin and whose
   ``git status --porcelain --untracked-files=all`` is empty. Ignore rules cannot hide a file
   there: ignored paths, bytecode included, are listed too;
4. an anonymous shallow fetch of the pin into (3), re-verified before it is published there.

Every git call runs without hooks, filesystem monitors, ``GIT_*`` variables, or system and global
git configuration, so the kit cache runs no code and a configured URL rewrite cannot turn the
anonymous HTTPS fetch into another transport (a proxy still comes from ``HTTPS_PROXY``).

kit-digest-v1 hashes the listing ``"<sha256>  ./<path>\\n"``, sorted bytewise, of every regular
file under ``src/``, ``site/`` and ``requirements/`` of a kit root, refusing symlinks, special
files, executable files, ``__pycache__`` and paths outside ``[A-Za-z0-9._/-]``, and prints
``sha256:<hex>`` of that listing. A staged overlay's ``template/`` and ``tools/``, which the digest
does not cover, must equal the listing :data:`STAGED_LOCK` inside its digested ``src/``, and its
``actions/`` the listing :data:`ACTIONS_LOCK` there; a kit older than v0.9.2 carries no
:data:`ACTIONS_LOCK`, and :func:`copy_kit` then stages no ``actions/``. Bytecode
is never tolerated in a verified kit, because Python loads a planted ``__pycache__`` file in place
of the verified source: :func:`kit_path` turns bytecode writing off for the importing process and
``run`` sets ``PYTHONDONTWRITEBYTECODE=1``.

Usage::

    python3 scripts/ci/mod_base_kit.py pin [--repo DIR]
    python3 scripts/ci/mod_base_kit.py path [--repo DIR]
    python3 scripts/ci/mod_base_kit.py run [--repo DIR] [--] ARGS...
    python3 scripts/ci/mod_base_kit.py verify [--network] [--repo DIR]
    python3 scripts/ci/mod_base_kit.py stage --controller-repo DIR --candidate-repo DIR --output DIR
    python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z [--repo DIR]

Mod tests import this file and call :func:`kit_path`, which raises :class:`KitError` (a
``RuntimeError``) when the kit is unavailable; they fail and never skip.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

KIT_REPOSITORY = "The-Plum-Team/mod-base"
KIT_REMOTE = "https://github.com/The-Plum-Team/mod-base.git"
API_ROOT = "https://api.github.com"
PIN_LINE = re.compile(
    r"^\s*(?:-\s+)?uses:\s+The-Plum-Team/mod-base/(\S+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$", re.ASCII
)
#: The only kit paths a mod may reference: the reusable workflows and the composites.
KIT_PATH = re.compile(r"(?:\.github/workflows/[A-Za-z0-9_-]+\.yml|actions/[a-z0-9-]+)", re.ASCII)
KIT_REFERENCE = re.compile(r"the-plum-team/mod-base(?:/[A-Za-z0-9._/-]*)?@\S", re.IGNORECASE | re.ASCII)
KIT_REPOSITORY_TOKEN = re.compile(r"the-plum-team/mod-base(?![A-Za-z0-9_.-])", re.IGNORECASE | re.ASCII)
KIT_REPOSITORY_BARE = re.compile(r"the-plum-team/mod-base(?:\.git)?(?![A-Za-z0-9_./-])", re.IGNORECASE | re.ASCII)
KIT_VALUE = re.compile(r"^\s*(?:-\s+)?[\"']?[A-Za-z0-9_-]+[\"']?\s*:\s*[\"']?the-plum-team/mod-base(?![A-Za-z0-9_.-])",
                       re.IGNORECASE | re.ASCII)
USES_KEY = re.compile(r"(?:^\s*(?:[-?]\s+)*|[{\[,]\s*)[\"']?uses[\"']?\s*:", re.ASCII)
USES_VALUE = re.compile(r"(?:^\s*(?:[-?]\s+)*|[{\[,]\s*)[\"']?uses[\"']?\s*:\s*(.*)$", re.ASCII)
ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.ASCII | re.DOTALL)
SIMPLE_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
                  "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
                  "L": "\u2028", "P": "\u2029"}
TAG = re.compile(r"v(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", re.ASCII)
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", re.ASCII)
SHA1 = re.compile(r"[0-9a-f]{40}", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)
KIT_PATH_NAME = re.compile(r"[A-Za-z0-9._/-]+", re.ASCII)
LINE_BREAK = re.compile(r"\r\n|\r|\n")

DIGESTED_DIRS = ("src", "site", "requirements")
LOCKED_DIRS = ("template", "tools")
STAGED_LOCK = "src/mod_base/template/staged_files.sha256"
#: Staged from v0.9.2 under its own lock, so :data:`STAGED_LOCK` still lists exactly
#: :data:`LOCKED_DIRS` and an older bootstrap can stage a newer kit.
ACTIONS_DIR = "actions"
ACTIONS_LOCK = "src/mod_base/template/staged_actions.sha256"
STAGED_DIRS = DIGESTED_DIRS + LOCKED_DIRS + (ACTIONS_DIR,)
STAMP_NAME = "MOD_BASE_KIT.json"
STAMP_KIND = "mod-base.kit-stamp"
OVERLAY_PATH = ("out", "mod-base-kit")
BYTECODE_DIRECTORY = "__pycache__"
ACTION_FILES = ("action.yml", "action.yaml")
#: Lists tracked changes, untracked files and (whatever the ignore rules) ignored files.
STATUS_ARGUMENTS = ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")

MAX_PIN_FILES = 512
MAX_PIN_FILE_BYTES = 1024 * 1024
MAX_PIN_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ACTION_ENTRIES = 4096
MAX_KIT_FILES = 20000
MAX_KIT_BYTES = 512 * 1024 * 1024
MAX_STAMP_BYTES = 4096
MAX_LOCK_BYTES = 1024 * 1024
MAX_API_BYTES = 32 * 1024 * 1024
MAX_TAG_PEELS = 4
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0
API_ATTEMPTS = 3
API_TIMEOUT_SECONDS = 30
GIT_TIMEOUT_SECONDS = 600
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
#: Longest wait before retrying a rate-limited request (Retry-After or X-RateLimit-Reset).
RATE_LIMIT_WAIT_CAP_SECONDS = 60.0
DECIMAL = re.compile(r"[0-9]{1,10}", re.ASCII)
#: Every git call ignores configured hooks and filesystem monitors: the kit cache runs no code.
GIT_SAFETY = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")
UNAVAILABLE = ("mod-base kit unavailable for candidate pin {sha}; "
               "a controller upgrade must pin a released mod-base commit")

_sleep: Callable[[float], None] = time.sleep
_now: Callable[[], float] = time.time


class KitError(RuntimeError):
    """The pin is inconsistent, or the pinned kit cannot be found and verified."""


class HttpError(KitError):
    """A GitHub API request failed with HTTP ``status``."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class Pin(NamedTuple):
    """The single pin of a mod and every ``path@line`` that references it, sorted.

    A ``NamedTuple``, so that the file also works when a mod test loads it with ``importlib``
    without registering it in ``sys.modules``.
    """

    sha: str
    version: str
    references: tuple[str, ...]


class Resolution(NamedTuple):
    """A verified kit root; ``pinned`` is false only for the developer override."""

    root: Path
    pin: Pin
    source: str
    pinned: bool


# -- Pin -------------------------------------------------------------------------------------------


def _one_line(value: object, limit: int = 300) -> str:
    text = "".join(" " if ord(character) < 32 or ord(character) == 127 else character for character in str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def yaml_unescape(text: str) -> str:
    """``text`` with every YAML double-quoted escape decoded (unknown escapes are kept)."""

    def decode(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] in "xuU" and len(code) > 1:
            value = int(code[1:], 16)
            return chr(value) if value <= 0x10FFFF else match.group(0)
        return SIMPLE_ESCAPES.get(code, match.group(0))

    return ESCAPE.sub(decode, text)


def _escaped_break(line: str) -> bool:
    """True when ``line`` ends in an odd run of backslashes (an escaped line break)."""

    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _uses_problem(line: str) -> str | None:
    if "\\" in line:
        return "a uses: line must not contain a backslash (YAML escape)"
    match = USES_VALUE.search(line)
    if match is None:
        return None
    value = match.group(1).strip()
    if not value or value.startswith("#"):
        return "a uses: value must be written on its uses: line"
    if value[:1] in ("|", ">"):
        return "a uses: value must not be a block scalar"
    if value[:1] in ("!", "&", "*"):
        return "a uses: value must not carry a YAML tag, anchor or alias"
    if value[:1] in ("'", '"') and value.count(value[0]) < 2:
        return "a uses: value must be quoted on one line"
    return None


def line_problem(line: str) -> str | None:
    """Why ``line`` (not a pin line) is an illegal mod-base reference, or ``None``.

    ``line`` is checked as written and with its YAML escapes decoded.
    """

    decoded = yaml_unescape(line)
    comment = line.lstrip(" \t").startswith("#")
    for view in dict.fromkeys((line, decoded)):
        if KIT_REFERENCE.search(view) or KIT_VALUE.search(view) or (
                USES_KEY.search(view) and KIT_REPOSITORY_TOKEN.search(view)):
            return "a mod-base reference must be 'uses: The-Plum-Team/mod-base/<path>@<40-hex> # vX.Y.Z'"
        if not comment and KIT_REPOSITORY_BARE.search(view):
            return ("the mod-base repository without a following path may appear only on a pin line or a "
                    "comment-only line")
    if not comment and USES_KEY.search(decoded):
        return _uses_problem(line)
    return None


def parse_pin_files(files: Mapping[str, bytes]) -> Pin:
    """Parse the pin from ``{repo-relative path: bytes}`` of the workflow and action files."""

    pairs: set[tuple[str, str]] = set()
    references: list[tuple[str, int]] = []
    for path in sorted(files):
        try:
            text = files[path].decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise KitError(f"{path} is not UTF-8 text") from None
        joined = ""
        for number, line in enumerate(LINE_BREAK.split(text), start=1):
            if joined:
                view = joined + line.lstrip(" \t")
                problem = line_problem(view)
                if problem is not None:
                    raise KitError(f"{path}@{number}: {problem} (joined to the previous line by an escaped line "
                                   f"break): {_one_line(view.strip())}")
            else:
                view = line
            joined = view[:-1] if _escaped_break(line) else ""
            match = PIN_LINE.match(line)
            if match is not None:
                if not KIT_PATH.fullmatch(match.group(1)):
                    raise KitError(f"{path}@{number}: {match.group(1)!r} is not a mod-base workflow or action")
                if not TAG.fullmatch(match.group(3)):
                    raise KitError(f"{path}@{number}: {match.group(3)!r} is not a vX.Y.Z release without leading zeros")
                pairs.add((match.group(2), match.group(3)))
                references.append((path, number))
                continue
            problem = line_problem(line)
            if problem is not None:
                raise KitError(f"{path}@{number}: {problem}: {_one_line(line.strip())}")
    if not pairs:
        raise KitError("no mod-base pin: no workflow or action line is "
                       "'uses: The-Plum-Team/mod-base/<path>@<40-hex> # vX.Y.Z'")
    if len(pairs) != 1:
        found = ", ".join(f"{sha} {version}" for sha, version in sorted(pairs))
        raise KitError(f"every mod-base reference must carry one SHA and version; found {found}")
    (sha, version), = pairs
    return Pin(sha, version, tuple(f"{path}@{number}" for path, number in sorted(references)))


def _directory_state(path: Path) -> bool | None:
    """None when ``path`` is absent, True when it is anything but a real directory."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    return not stat.S_ISDIR(status.st_mode)


def _read_bounded(path: Path, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise KitError(f"cannot read {label}: {exc.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise KitError(f"{label} is not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise KitError(f"{label} exceeds {limit} bytes")
    return data


def _pin_file_paths(repo: Path) -> list[str]:
    paths: list[str] = []
    github = repo / ".github"
    state = _directory_state(github)
    if state is None:
        return paths
    if state:
        raise KitError(".github must be a real directory")
    workflows = github / "workflows"
    state = _directory_state(workflows)
    if state:
        raise KitError(".github/workflows must be a real directory")
    if state is False:
        with os.scandir(workflows) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name.endswith((".yml", ".yaml")):
                    if not entry.is_file(follow_symlinks=False):
                        raise KitError(f".github/workflows/{entry.name} must be a regular file")
                    paths.append(f".github/workflows/{entry.name}")
    actions = github / "actions"
    state = _directory_state(actions)
    if state:
        raise KitError(".github/actions must be a real directory")
    if state is False:
        visited = 0
        pending: list[tuple[Path, str]] = [(actions, ".github/actions")]
        while pending:
            directory, relative = pending.pop()
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    visited += 1
                    if visited > MAX_ACTION_ENTRIES:
                        raise KitError(f".github/actions holds more than {MAX_ACTION_ENTRIES} entries")
                    child = f"{relative}/{entry.name}"
                    if entry.is_symlink():
                        raise KitError(f"{child} must not be a symlink")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), child))
                    elif entry.name in ACTION_FILES:
                        if not entry.is_file(follow_symlinks=False):
                            raise KitError(f"{child} must be a regular file")
                        paths.append(child)
    if len(paths) > MAX_PIN_FILES:
        raise KitError(f"more than {MAX_PIN_FILES} workflow and action files")
    return sorted(paths)


def read_pin_files(repo: Path) -> dict[str, bytes]:
    """Read (bounded, no symlinks) every workflow and action file of ``repo``."""

    files: dict[str, bytes] = {}
    total = 0
    for relative in _pin_file_paths(repo):
        data = _read_bounded(repo / relative, MAX_PIN_FILE_BYTES, relative)
        total += len(data)
        if total > MAX_PIN_TOTAL_BYTES:
            raise KitError(f"workflow and action files exceed {MAX_PIN_TOTAL_BYTES} bytes")
        files[relative] = data
    return files


def parse_pin(repo: Path) -> Pin:
    """Read the mod's workflow and action files and parse its single pin."""

    return parse_pin_files(read_pin_files(Path(os.path.abspath(repo))))


# -- kit-digest-v1 ---------------------------------------------------------------------------------


def _listing(root: Path, tops: Sequence[str], *, locked: bool) -> str:
    """The sorted ``"<sha256>  ./<path>\\n"`` listing of every file under ``tops`` of ``root``.

    kit-digest-v1 (``locked`` false) requires every top and refuses executable files; the
    staged-file lock (``locked`` true) addresses content only, so a top may be absent.
    """

    root = Path(os.path.abspath(root))
    if _directory_state(root) is not False:
        raise KitError(f"kit root {root} is not a real directory")
    records: list[tuple[bytes, str]] = []
    total = 0
    for top in tops:
        base = root / top
        state = _directory_state(base)
        if state is None and locked:
            continue
        if state is not False:
            raise KitError(f"kit {top}/ is missing or not a real directory")
        pending = [(base, top)]
        while pending:
            directory, relative = pending.pop()
            with os.scandir(directory) as entries:
                listed = list(entries)
            for entry in listed:
                child = f"{relative}/{entry.name}"
                status = entry.stat(follow_symlinks=False)
                if entry.name == BYTECODE_DIRECTORY:
                    raise KitError(f"kit tree holds bytecode: {child}")
                if not KIT_PATH_NAME.fullmatch(child):
                    raise KitError(f"kit path {child!r} has characters outside [A-Za-z0-9._/-]")
                if stat.S_ISDIR(status.st_mode):
                    pending.append((Path(entry.path), child))
                    continue
                if not stat.S_ISREG(status.st_mode):
                    raise KitError(f"kit entry {child} is a symlink or special file")
                if status.st_mode & 0o111 and not locked:
                    raise KitError(f"kit file {child} is executable")
                total += status.st_size
                if len(records) >= MAX_KIT_FILES or total > MAX_KIT_BYTES:
                    raise KitError("kit tree exceeds its file or byte bound")
                data = _read_bounded(Path(entry.path), MAX_KIT_BYTES, child)
                if len(data) != status.st_size:
                    raise KitError(f"kit file {child} changed while it was hashed")
                records.append((f"./{child}".encode("ascii"), hashlib.sha256(data).hexdigest()))
    return "".join(f"{hexdigest}  {path.decode('ascii')}\n" for path, hexdigest in sorted(records))


def tree_digest(root: Path) -> str:
    """``sha256:<hex>`` kit-digest-v1 of the kit root ``root`` (see the module docstring)."""

    listing = _listing(root, DIGESTED_DIRS, locked=False)
    if not listing:
        raise KitError("kit tree holds no files")
    return "sha256:" + hashlib.sha256(listing.encode("ascii")).hexdigest()


def staged_listing(root: Path) -> bytes:
    """The listing of ``template/`` and ``tools/`` of ``root``: what :data:`STAGED_LOCK` holds."""

    return _listing(root, LOCKED_DIRS, locked=True).encode("ascii")


def actions_listing(root: Path) -> bytes:
    """The listing of ``actions/`` of ``root``: what :data:`ACTIONS_LOCK` holds."""

    return _listing(root, (ACTIONS_DIR,), locked=True).encode("ascii")


def _lock_path(root: Path, lock: str) -> Path:
    return Path(root).joinpath(*lock.split("/"))


def verify_staged_files(root: Path) -> None:
    """Require ``template/`` and ``tools/`` of ``root`` to equal its :data:`STAGED_LOCK` listing and
    a present ``actions/`` its :data:`ACTIONS_LOCK` listing (an absent one binds nothing)."""

    expected = _read_bounded(_lock_path(root, STAGED_LOCK), MAX_LOCK_BYTES, STAGED_LOCK)
    if staged_listing(root) != expected:
        raise KitError(f"the kit's template/ and tools/ do not match its {STAGED_LOCK}")
    if _directory_state(Path(root) / ACTIONS_DIR) is None:
        return
    if not os.path.lexists(_lock_path(root, ACTIONS_LOCK)):
        raise KitError(f"the kit's {ACTIONS_DIR}/ is not bound by an {ACTIONS_LOCK}")
    if actions_listing(root) != _read_bounded(_lock_path(root, ACTIONS_LOCK), MAX_LOCK_BYTES, ACTIONS_LOCK):
        raise KitError(f"the kit's {ACTIONS_DIR}/ does not match its {ACTIONS_LOCK}")


# -- Strict JSON -----------------------------------------------------------------------------------


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KitError(f"duplicate JSON key {_one_line(key, 80)!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise KitError(f"non-finite JSON number {value!r}")


def strict_json(data: bytes, label: str) -> Any:
    """Decode strict JSON: UTF-8 without BOM, no duplicate keys, no NaN/Infinity."""

    if data.startswith(b"\xef\xbb\xbf"):
        raise KitError(f"{label} starts with a byte-order mark")
    try:
        return json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_unique,
                          parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise KitError(f"{label} is not strict JSON: {_one_line(exc)}") from None


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def _string(value: Any, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


# -- Stamp -----------------------------------------------------------------------------------------


def read_stamp(directory: Path) -> dict[str, Any]:
    """Read and validate ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) in ``directory``."""

    document = strict_json(_read_bounded(Path(directory) / STAMP_NAME, MAX_STAMP_BYTES, STAMP_NAME), STAMP_NAME)
    if not isinstance(document, dict) or set(document) != {"kind", "schema_version", "sha", "version", "tree_digest"}:
        raise KitError(f"{STAMP_NAME} must hold exactly kind, schema_version, sha, version and tree_digest")
    if document["kind"] != STAMP_KIND or type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise KitError(f"{STAMP_NAME} is not a {STAMP_KIND} v1 document")
    if not _string(document["sha"], SHA1):
        raise KitError(f"{STAMP_NAME} sha is not a 40-hex commit")
    if not _string(document["version"], VERSION):
        raise KitError(f"{STAMP_NAME} version is not X.Y.Z")
    if not _string(document["tree_digest"], DIGEST):
        raise KitError(f"{STAMP_NAME} tree_digest is not sha256:<64-hex>")
    return document


def stamp_document(pin: Pin, digest: str) -> dict[str, Any]:
    return {"kind": STAMP_KIND, "schema_version": 1, "sha": pin.sha, "version": pin.version[1:], "tree_digest": digest}


# -- Git and GitHub --------------------------------------------------------------------------------


def _git_environment(ceiling: Path) -> dict[str, str]:
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CEILING_DIRECTORIES": str(ceiling), "LC_ALL": "C",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    return environment


def _git(arguments: Sequence[str], *, cwd: Path, ceiling: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *GIT_SAFETY, *arguments], cwd=cwd, env=_git_environment(ceiling),
                              stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
                              timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KitError(f"git {arguments[0]} failed: {_one_line(exc)}") from None


def _git_ok(arguments: Sequence[str], *, cwd: Path, ceiling: Path) -> str:
    result = _git(arguments, cwd=cwd, ceiling=ceiling)
    if result.returncode != 0:
        raise KitError(f"git {' '.join(arguments[:2])} failed: {_one_line(result.stderr.strip() or result.returncode)}")
    return result.stdout


def unclean_paths(status: str) -> list[str]:
    """Paths of ``git status`` (:data:`STATUS_ARGUMENTS`) output that make a kit checkout unclean.

    Every entry counts: a tracked change, and any untracked or ignored path, bytecode included.
    """

    fields = status.split("\0")
    unclean: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        if entry[:1] in ("R", "C"):
            index += 1
        unclean.append(entry[3:])
    return unclean


def verify_checkout(path: Path, sha: str) -> None:
    """Require ``path`` to be its own clean git worktree whose HEAD is ``sha``."""

    if _directory_state(path) is not False or _directory_state(path / ".git") is not False:
        raise KitError(f"{path} is not a git checkout; delete it to fetch the kit again")
    head = _git_ok(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path, ceiling=path.parent).strip()
    if head != sha:
        raise KitError(f"{path} is at {_one_line(head, 40)}, not the pin {sha}; delete it to fetch the kit again")
    unclean = unclean_paths(_git_ok(list(STATUS_ARGUMENTS), cwd=path, ceiling=path.parent))
    if unclean:
        raise KitError(f"{path} has local changes ({_one_line(', '.join(unclean[:5]), 200)}); "
                       "delete it to fetch the kit again")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _retry_delay(error: urllib.error.HTTPError, attempt: int) -> float | None:
    """Seconds to wait before retrying ``error``, or ``None`` when it is permanent.

    408, 429 and 5xx are transient. GitHub answers its primary and secondary rate limits with 403
    (or 429) carrying ``X-RateLimit-Remaining: 0`` or ``Retry-After``; those wait for the advertised
    time, capped at :data:`RATE_LIMIT_WAIT_CAP_SECONDS`.
    """

    headers = error.headers
    retry_after = (headers.get("Retry-After") or "").strip() if headers is not None else ""
    remaining = (headers.get("X-RateLimit-Remaining") or "").strip() if headers is not None else ""
    reset = (headers.get("X-RateLimit-Reset") or "").strip() if headers is not None else ""
    rate_limited = error.code == 429 or (error.code == 403 and (bool(retry_after) or remaining == "0"))
    if error.code not in RETRYABLE_HTTP and not rate_limited:
        return None
    delay = FETCH_BACKOFF_SECONDS * attempt
    if rate_limited:
        if DECIMAL.fullmatch(retry_after):
            delay = max(delay, float(retry_after))
        elif remaining == "0" and DECIMAL.fullmatch(reset):
            delay = max(delay, float(reset) - _now())
    return min(delay, RATE_LIMIT_WAIT_CAP_SECONDS)


def api_getter(environ: Mapping[str, str]) -> Callable[[str], Any]:
    """A bounded GET-only GitHub client (no proxies, no redirects, bounded retry).

    It is anonymous unless the step provides ``GH_TOKEN``/``GITHUB_TOKEN``; every request it
    makes reads the public kit repository. Transient and rate-limit responses are retried
    (:func:`_retry_delay`); every other failure is final.
    """

    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or ""
    if token and not all(33 <= ord(character) < 127 for character in token):
        raise KitError("GH_TOKEN/GITHUB_TOKEN is malformed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def get_json(path: str) -> Any:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "mod-base-kit-bootstrap"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        for attempt in range(1, API_ATTEMPTS + 1):
            request = urllib.request.Request(API_ROOT + path, headers=headers, method="GET")
            try:
                with opener.open(request, timeout=API_TIMEOUT_SECONDS) as response:
                    data = response.read(MAX_API_BYTES + 1)
                if len(data) > MAX_API_BYTES:
                    raise KitError(f"GET {path} returned more than {MAX_API_BYTES} bytes")
                return strict_json(data, f"GET {path}")
            except urllib.error.HTTPError as exc:
                exc.close()
                delay = _retry_delay(exc, attempt)
                if delay is None or attempt == API_ATTEMPTS:
                    raise HttpError(f"GET {path} failed with HTTP {exc.code}", exc.code) from None
            except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
                if attempt == API_ATTEMPTS:
                    raise KitError(f"GET {path} failed: {_one_line(exc)}") from None
                delay = FETCH_BACKOFF_SECONDS * attempt
            _sleep(delay)
        raise KitError(f"GET {path} failed")

    return get_json


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KitError(f"{label} is not a JSON object")
    return value


def resolve_tag(version: str, get_json: Callable[[str], Any]) -> str:
    """Return the commit the kit tag ``version`` peels to."""

    if not _string(version, TAG):
        raise KitError(f"{_one_line(version, 40)!r} is not a vX.Y.Z tag")
    try:
        reference = _object(get_json(f"/repos/{KIT_REPOSITORY}/git/ref/tags/{version}"), "tag ref")
    except HttpError as exc:
        if exc.status == 404:
            raise KitError(f"tag {version} does not exist in {KIT_REPOSITORY}") from None
        raise
    if reference.get("ref") != f"refs/tags/{version}":
        raise KitError(f"tag {version} does not exist in {KIT_REPOSITORY}")
    target = _object(reference.get("object"), "tag ref object")
    for _ in range(MAX_TAG_PEELS):
        if target.get("type") != "tag":
            break
        sha = target.get("sha")
        if not _string(sha, SHA1):
            raise KitError(f"tag {version} names a malformed tag object")
        tag = _object(get_json(f"/repos/{KIT_REPOSITORY}/git/tags/{sha}"), "tag")
        target = _object(tag.get("object"), "tag object")
    sha = target.get("sha")
    if target.get("type") != "commit" or not _string(sha, SHA1):
        raise KitError(f"tag {version} does not peel to a commit")
    return sha


def require_reachable(sha: str, get_json: Callable[[str], Any]) -> None:
    """Require ``compare/<sha>...main`` to be ``ahead`` or ``identical`` with ``behind_by == 0``.

    ``per_page=1`` keeps the comparison small; ``status`` and ``behind_by`` describe the whole
    range whatever the page size.
    """

    try:
        comparison = _object(get_json(f"/repos/{KIT_REPOSITORY}/compare/{sha}...main?per_page=1"), "compare")
    except HttpError as exc:
        if exc.status == 404:
            raise KitError(f"pin {sha} is not a commit of {KIT_REPOSITORY} main") from None
        raise
    behind = comparison.get("behind_by")
    if comparison.get("status") not in ("ahead", "identical") or type(behind) is not int or behind != 0:
        raise KitError(f"pin {sha} is not reachable from {KIT_REPOSITORY} main "
                       f"(status {_one_line(comparison.get('status'), 40)}, behind_by {_one_line(behind, 40)}); "
                       "it may be an impostor commit from a fork")


def verify_released(pin: Pin, get_json: Callable[[str], Any]) -> None:
    """Require the pin to be reachable from mod-base ``main`` and its tag to peel to the pin."""

    require_reachable(pin.sha, get_json)
    tagged = resolve_tag(pin.version, get_json)
    if tagged != pin.sha:
        raise KitError(f"tag {pin.version} peels to {tagged}, not the pin {pin.sha}")


def verify(repo: Path, *, network: bool, get_json: Callable[[str], Any] | None = None) -> Pin:
    """Pin consistency; with ``network`` also the released-commit checks of :func:`verify_released`."""

    pin = parse_pin(repo)
    if network:
        if get_json is None:
            raise KitError("verify --network needs a GitHub API client")
        verify_released(pin, get_json)
    return pin


# -- Kit resolution --------------------------------------------------------------------------------


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def cache_root(environ: Mapping[str, str], repo: Path) -> Path:
    """``<cache>/mod-base``: never inside ``repo``."""

    explicit = environ.get("MOD_BASE_CACHE_DIR", "")
    if explicit:
        if not os.path.isabs(explicit):
            raise KitError("MOD_BASE_CACHE_DIR must be an absolute path")
        base = Path(explicit)
    elif sys.platform == "darwin":
        base = Path(environ.get("HOME") or Path.home()) / "Library" / "Caches"
    elif os.name == "nt":
        local = environ.get("LOCALAPPDATA", "")
        if not os.path.isabs(local):
            raise KitError("LOCALAPPDATA is not set; set MOD_BASE_CACHE_DIR")
        base = Path(local)
    else:
        xdg = environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if os.path.isabs(xdg) else Path(environ.get("HOME") or Path.home()) / ".cache"
    root = Path(os.path.abspath(base)) / "mod-base"
    if _within(os.path.realpath(root), os.path.realpath(repo)):
        raise KitError(f"the kit cache {root} must lie outside the repository")
    return root


def _require_kit_tree(root: Path, label: str) -> Path:
    try:
        status = os.lstat(root / "src" / "mod_base" / "__init__.py")
    except OSError:
        status = None
    if _directory_state(root) is not False or status is None or not stat.S_ISREG(status.st_mode):
        raise KitError(f"{label} {root} is not a mod-base kit root (no src/mod_base/__init__.py)")
    return root


def _verify_overlay(overlay: Path, pin: Pin) -> Path:
    if _directory_state(overlay.parent) or _directory_state(overlay):
        raise KitError(f"{overlay} must be a real directory")
    stamp = read_stamp(overlay)
    if stamp["sha"] != pin.sha or stamp["version"] != pin.version[1:]:
        raise KitError(f"the staged kit {overlay} is {stamp['sha']} {stamp['version']}, not the pin "
                       f"{pin.sha} {pin.version}; delete it or stage it again")
    actual = tree_digest(overlay)
    if actual != stamp["tree_digest"]:
        raise KitError(f"the staged kit {overlay} digest {actual} does not equal its stamp {stamp['tree_digest']}")
    verify_staged_files(overlay)
    return _require_kit_tree(overlay, "staged kit")


def _remove_tree(path: Path) -> None:
    def retry_writable(function: Callable[..., Any], target: str, _info: Any) -> None:
        os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(target)

    if os.path.lexists(path):
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry_writable)
        else:
            shutil.rmtree(path, onerror=retry_writable)


def fetch_pin(pin: Pin, destination: Path) -> Path:
    """Anonymously fetch exactly the pinned commit into ``destination`` and verify it.

    The fetch happens in a private staging directory and is published with one rename, so a
    concurrent or interrupted fetch never leaves a half-populated cache. Git content addressing is
    the integrity check: the checked-out HEAD must equal the pin.
    """

    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".fetch-{pin.sha[:12]}-", dir=parent))
    try:
        _git_ok(["init", "-q", "--template=", str(staging)], cwd=parent, ceiling=parent)
        failure = ""
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            result = _git(["-c", "protocol.version=2", "fetch", "-q", "--depth=1", "--no-tags",
                           "--no-recurse-submodules", KIT_REMOTE, pin.sha], cwd=staging, ceiling=parent)
            if result.returncode == 0:
                break
            failure = _one_line(result.stderr.strip() or result.returncode)
            if attempt < FETCH_ATTEMPTS:
                _sleep(FETCH_BACKOFF_SECONDS * attempt)
        else:
            raise KitError(f"cannot fetch mod-base {pin.sha}: {failure}")
        _git_ok(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=staging,
                ceiling=parent)
        verify_checkout(staging, pin.sha)
        try:
            os.rename(staging, destination)
        except OSError:
            if not os.path.lexists(destination):
                raise KitError(f"cannot publish the fetched kit to {destination}") from None
        verify_checkout(destination, pin.sha)
        return destination
    finally:
        _remove_tree(staging)


def cached_kit(pin: Pin, environ: Mapping[str, str], repo: Path) -> Resolution:
    """The verified user-cache kit of ``pin``, fetched when absent."""

    destination = cache_root(environ, repo) / pin.sha
    if os.path.lexists(destination):
        verify_checkout(destination, pin.sha)
        source = "cache"
    else:
        fetch_pin(pin, destination)
        source = "fetch"
    return Resolution(_require_kit_tree(destination, "cached kit"), pin, source, True)


def default_repo() -> Path:
    """The repository holding this file (``<repo>/scripts/ci/mod_base_kit.py``)."""

    return Path(os.path.abspath(__file__)).parents[2]


def resolve(repo: Path | None = None, environ: Mapping[str, str] | None = None, *, overlay: bool = True,
            allow_unpinned: bool = True) -> Resolution:
    """Resolve and verify the kit root of ``repo`` (see the module docstring)."""

    repo = Path(os.path.abspath(repo if repo is not None else default_repo()))
    environ = os.environ if environ is None else environ
    pin = parse_pin(repo)
    staged = repo.joinpath(*OVERLAY_PATH)
    if overlay and os.path.lexists(staged):
        return Resolution(_verify_overlay(staged, pin), pin, "overlay", True)
    kit_sha = environ.get("MOD_BASE_KIT_SHA", "")
    if kit_sha and kit_sha != pin.sha:
        raise KitError(f"MOD_BASE_KIT_SHA {_one_line(kit_sha, 60)} does not equal the pin {pin.sha}")
    kit_path_value = environ.get("MOD_BASE_KIT_PATH", "")
    if kit_path_value:
        root = Path(os.path.abspath(kit_path_value))
        if kit_sha:
            return Resolution(_require_kit_tree(root, "MOD_BASE_KIT_PATH"), pin, "environment", True)
        if not allow_unpinned:
            raise KitError("MOD_BASE_KIT_PATH without MOD_BASE_KIT_SHA is not accepted here")
        if environ.get("MOD_BASE_ALLOW_UNPINNED") != "1" or "CI" in environ or "GITHUB_ACTIONS" in environ:
            raise KitError("MOD_BASE_KIT_PATH without MOD_BASE_KIT_SHA is a developer override: set "
                           "MOD_BASE_ALLOW_UNPINNED=1, and it is always refused when CI or GITHUB_ACTIONS is set")
        return Resolution(_require_kit_tree(root, "MOD_BASE_KIT_PATH"), pin, "unpinned", False)
    return cached_kit(pin, environ, repo)


def kit_path(repo: Path | None = None, environ: Mapping[str, str] | None = None) -> Path:
    """The verified kit root for ``repo`` (default: the repository holding this file).

    Mod tests call this and put ``<kit>/src`` on ``sys.path``; a :class:`KitError` here must fail
    the test run, never skip it. It turns off bytecode writing for this process
    (``sys.dont_write_bytecode``): importing the kit must never add ``__pycache__`` to a verified
    tree, which the next resolution would refuse. Child processes that import the kit need
    ``PYTHONDONTWRITEBYTECODE=1`` too.
    """

    root = resolve(repo, environ).root
    sys.dont_write_bytecode = True
    return root


# -- stage and bump --------------------------------------------------------------------------------


def _write_new(target: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(target, flags, 0o644), "wb") as stream:
        stream.write(data)


def copy_kit(source: Path, output: Path) -> None:
    """Copy the kit directories of ``source`` into the new directory ``output``: the digested
    ones, ``template/`` and ``tools/``, and ``actions/`` when ``source`` carries :data:`ACTIONS_LOCK`.

    Only regular files are copied, each read bounded and written exclusively as mode 0644;
    ``__pycache__`` directories are skipped, and any symlink or special file is an error.
    """

    if os.path.lexists(output):
        raise KitError(f"{output} already exists")
    if _directory_state(output.parent) is not False:
        raise KitError(f"the parent of {output} must be an existing real directory")
    tops = STAGED_DIRS if os.path.lexists(_lock_path(source, ACTIONS_LOCK)) else DIGESTED_DIRS + LOCKED_DIRS
    output.mkdir(mode=0o755)
    files = total = 0
    for top in tops:
        base = source / top
        state = _directory_state(base)
        if state is None and top not in DIGESTED_DIRS:
            continue
        if state is not False:
            raise KitError(f"kit {top}/ is missing or not a real directory")
        pending = [(base, output / top, top)]
        while pending:
            directory, target, relative = pending.pop()
            target.mkdir(mode=0o755)
            with os.scandir(directory) as entries:
                listed = sorted(entries, key=lambda item: item.name)
            for entry in listed:
                child = f"{relative}/{entry.name}"
                status = entry.stat(follow_symlinks=False)
                if entry.name == BYTECODE_DIRECTORY and stat.S_ISDIR(status.st_mode):
                    continue
                if not KIT_PATH_NAME.fullmatch(child):
                    raise KitError(f"kit path {child!r} has characters outside [A-Za-z0-9._/-]")
                if stat.S_ISDIR(status.st_mode):
                    pending.append((Path(entry.path), target / entry.name, child))
                elif stat.S_ISREG(status.st_mode):
                    data = _read_bounded(Path(entry.path), MAX_KIT_BYTES, child)
                    files += 1
                    total += len(data)
                    if files > MAX_KIT_FILES or total > MAX_KIT_BYTES:
                        raise KitError("kit tree exceeds its file or byte bound")
                    _write_new(target / entry.name, data)
                else:
                    raise KitError(f"kit entry {child} is a symlink or special file")


def stage(controller_repo: Path, candidate_repo: Path, output: Path, environ: Mapping[str, str],
          get_json: Callable[[str], Any] | None = None) -> Path:
    """Stage the kit the candidate pins into ``output`` with its stamp (Block Pops controller only).

    The candidate's workflow files are read only as bounded bytes. With the controller's own pin,
    the controller-verified kit (``MOD_BASE_KIT_PATH`` from ``setup``, or the verified cache) is
    copied. With a different pin, which only a controller upgrade can carry, that pin must first
    pass :func:`verify_released`; it is then fetched anonymously and re-verified before the copy.
    The copy's staged directories must match its staged-file locks. Any failure, including
    an operating-system error, removes a partial ``output`` and raises the :data:`UNAVAILABLE`
    message.
    """

    controller_repo = Path(os.path.abspath(controller_repo))
    output = Path(os.path.abspath(output))
    candidate_sha = "<unparseable>"
    created = False
    try:
        candidate = parse_pin(Path(os.path.abspath(candidate_repo)))
        candidate_sha = candidate.sha
        controller = parse_pin(controller_repo)
        if candidate.sha == controller.sha:
            if candidate.version != controller.version:
                raise KitError(f"the candidate labels the pin {candidate.version}, the controller {controller.version}")
            source = resolve(controller_repo, environ, overlay=False, allow_unpinned=False).root
        else:
            verify_released(candidate, get_json if get_json is not None else api_getter(environ))
            source = cached_kit(candidate, environ, controller_repo).root
        created = not os.path.lexists(output)
        copy_kit(source, output)
        verify_staged_files(output)
        _write_new(output / STAMP_NAME, canonical_json(stamp_document(candidate, tree_digest(output))))
    except (KitError, OSError) as exc:
        if created:
            _remove_tree(output)
        raise KitError(f"{UNAVAILABLE.format(sha=candidate_sha)} ({_one_line(exc)})") from None
    return output


def _replace_file(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".mod-base-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, stat.S_IMODE(os.lstat(path).st_mode))
        os.replace(temporary, path)
    except BaseException:
        if os.path.lexists(temporary):
            os.unlink(temporary)
        raise


def rewrite_pin(repo: Path, target: Pin) -> list[str]:
    """Rewrite the SHA and ``# v`` comment of every pin line of ``repo``; return the changed files."""

    changed: list[str] = []
    for relative, data in read_pin_files(repo).items():
        pieces = re.split(r"(\r\n|\r|\n)", data.decode("utf-8"))
        for index in range(0, len(pieces), 2):
            match = PIN_LINE.match(pieces[index])
            if match is not None:
                line = pieces[index]
                line = line[: match.start(3)] + target.version + line[match.end(3):]
                pieces[index] = line[: match.start(2)] + target.sha + line[match.end(2):]
        updated = "".join(pieces).encode("utf-8")
        if updated != data:
            _replace_file(repo / relative, updated)
            changed.append(relative)
    return changed


def kit_environment(resolution: Resolution) -> dict[str, str]:
    """The environment ``run`` gives the kit: its ``src`` alone on ``PYTHONPATH``."""

    environment = {name: value for name, value in os.environ.items() if name not in ("PYTHONHOME", "PYTHONSTARTUP")}
    environment.update({"PYTHONPATH": str(resolution.root / "src"), "PYTHONSAFEPATH": "1",
                        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"})
    if resolution.pinned:
        environment["MOD_BASE_KIT_SHA"] = resolution.pin.sha
    return environment


def bump(repo: Path, version: str, environ: Mapping[str, str], get_json: Callable[[str], Any] | None = None) -> Pin:
    """Pin ``repo`` to the released kit ``version`` and resynchronize its managed files.

    Before any file changes, the tag is resolved and verified like ``verify --network`` and the
    new kit is fetched into the verified user cache; every pin line is then rewritten and the
    managed files are resynchronized by ``template sync --write`` of the newly pinned kit.
    """

    repo = Path(os.path.abspath(repo))
    get_json = get_json if get_json is not None else api_getter(environ)
    parse_pin(repo)
    target = Pin(resolve_tag(version, get_json), version, ())
    require_reachable(target.sha, get_json)
    resolution = cached_kit(target, environ, repo)
    rewrite_pin(repo, target)
    pin = parse_pin(repo)
    if (pin.sha, pin.version) != (target.sha, target.version):
        raise KitError("the rewritten pin is inconsistent")
    command = [sys.executable, "-P", "-m", "mod_base", "template", "sync", "--repo", str(repo), "--write"]
    result = subprocess.run(command, env=kit_environment(resolution), stdin=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise KitError(f"template sync --write failed with exit {result.returncode}")
    return pin


# -- CLI -------------------------------------------------------------------------------------------


def _run(resolution: Resolution, arguments: Sequence[str]) -> int:
    command = [sys.executable, "-P", "-m", "mod_base", *arguments]
    environment = kit_environment(resolution)
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "posix":
        os.execve(sys.executable, command, environment)
    return subprocess.run(command, env=environment, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mod_base_kit.py", description=(__doc__ or "").split("\n\n")[0])
    verbs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for name, text in (("pin", "print '<sha> <version>'"), ("path", "print the verified kit root")):
        verbs.add_parser(name, help=text).add_argument("--repo", type=Path)
    run = verbs.add_parser("run", help="run 'python3 -P -m mod_base ARGS' from the pinned kit")
    run.add_argument("--repo", type=Path)
    run.add_argument("arguments", nargs=argparse.REMAINDER)
    check = verbs.add_parser("verify", help="check pin consistency (and, with --network, the release)")
    check.add_argument("--repo", type=Path)
    check.add_argument("--network", action="store_true")
    staging = verbs.add_parser("stage", help="stage the candidate's pinned kit for the sandbox overlay")
    staging.add_argument("--controller-repo", type=Path, required=True)
    staging.add_argument("--candidate-repo", type=Path, required=True)
    staging.add_argument("--output", type=Path, required=True)
    bumping = verbs.add_parser("bump", help="pin a released kit tag and resynchronize managed files")
    bumping.add_argument("--repo", type=Path)
    bumping.add_argument("--to", required=True, metavar="vX.Y.Z")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    repo = arguments.repo if getattr(arguments, "repo", None) is not None else default_repo()
    try:
        if arguments.command == "pin":
            pin = parse_pin(repo)
            print(f"{pin.sha} {pin.version}")
        elif arguments.command == "path":
            print(kit_path(repo))
        elif arguments.command == "run":
            forwarded = list(arguments.arguments)
            if forwarded[:1] == ["--"]:
                forwarded = forwarded[1:]
            if not forwarded:
                raise KitError("run needs the kit command, for example: run -- template check --repo .")
            return _run(resolve(repo), forwarded)
        elif arguments.command == "verify":
            pin = verify(repo, network=arguments.network,
                         get_json=api_getter(os.environ) if arguments.network else None)
            print(f"{pin.sha} {pin.version}")
        elif arguments.command == "stage":
            print(stage(arguments.controller_repo, arguments.candidate_repo, arguments.output, os.environ))
        else:
            pin = bump(repo, arguments.to, os.environ)
            print(f"{pin.sha} {pin.version}")
    except (KitError, OSError) as exc:
        print(f"mod_base_kit: error: {_one_line(exc, 1000)}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
