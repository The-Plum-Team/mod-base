"""The kit pin: parser, verifier, kit resolution and ``kit-digest-v1`` (MB9).

The same parser as the managed bootstrap ``scripts/ci/mod_base_kit.py`` (parity-tested against it):
every ``.github/workflows/*.yml`` and ``.github/actions/*/action.yml`` line matching
:data:`PIN_LINE` contributes ``(sha, version)``; exactly one pair must result and any other
``The-Plum-Team/mod-base`` token in those files is an error. ``kit-digest-v1`` hashes the listing
``"<sha256>  ./<path>\\n"`` (sorted under ``LC_ALL=C``) of every file under ``src/``, ``site/`` and
``requirements/``, refusing symlinks, special files, executable bits and ``__pycache__``; it is
printed as ``sha256:<hex>`` and equals ``tools/kit_digest.sh``.

Precise rules shared with the bootstrap (``tests/test_pin.py`` runs both over the same inputs):

* the scanned files are ``.github/workflows/*.yml``/``*.yaml`` and every ``action.yml``/
  ``action.yaml`` anywhere below ``.github/actions``; any symlink or special file on those paths is
  an error, and the walk and every read are bounded;
* matching is ASCII-only (``re.ASCII``): YAML separates a key from its value only with ASCII
  whitespace, so a line using any other space can never count as a pin;
* a pin line's path must name a kit workflow (``.github/workflows/<name>.yml``) or composite
  (``actions/<name>``), and its version must be ``vX.Y.Z`` without leading zeros (:data:`TAG`);
* every other line is checked as written and with its YAML double-quoted escapes decoded, and a
  line ending in an escaped line break is also checked joined to the next line, which is how YAML
  reads it. Case-insensitively (owners and repositories are case-insensitive on GitHub), such a
  line may hold no ``owner/repo[/path]@ref`` token naming the kit, no ``uses:`` key together with
  the kit repository and no YAML key whose value starts with the kit repository; outside a
  comment-only line it may not name the bare kit repository either (``repository:
  The-Plum-Team/mod-base``, a flow value, a quoted or folded scalar). A mention followed by a path,
  such as the managed caller's ``repos/The-Plum-Team/mod-base/compare/...`` shell, is not a
  reference. YAML folds every other line break into a space or a newline, so no kit reference can
  be spelled across lines without one of the checked lines holding it;
* a ``uses:`` key (at the start of a line, after a list dash or inside a flow collection) must
  carry its value on the same line as a plain single-line scalar: no backslash (a YAML escape), no
  block scalar, no unterminated quote, and no YAML tag, anchor or alias;
* ``references`` are ``"<path>@<line>"`` strings sorted by path, then line number.

Values assembled at run time (an expression such as ``format(...)`` or a shell command) are
outside what any static parser can see; they are governed by review like every workflow change.

Kit resolution (:func:`kit_path`) follows the bootstrap order: the ``out/mod-base-kit`` overlay
stamp, ``MOD_BASE_KIT_PATH`` with ``MOD_BASE_KIT_SHA``, the user cache outside the repository and an
anonymous shallow fetch into it. The first candidate that exists wins and is verified; a candidate
that exists but fails verification is an error, never a fall-through. Bytecode is never tolerated:
Python loads a planted ``__pycache__`` file in place of the verified source, so an overlay or cache
holding one is refused, and :func:`kit_path` turns off bytecode writing for the process that
imports the kit. An overlay's ``template/`` and ``tools/``, which kit-digest-v1 does not cover,
must equal the listing :data:`STAGED_LOCK` inside the digested ``src/``, and its ``actions/``, when
staged, the listing :data:`ACTIONS_LOCK` there (:func:`verify_staged_files`). A cache checkout is clean
only when ``git status`` lists no tracked change and no untracked *or ignored* path, so no ignore
rule can hide an added file. Every git call runs without hooks, filesystem monitors, ``GIT_*``
variables, or system and global git configuration.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.errors import MbError, Unavailable, single_line
from mod_base.github.api import ApiNotFound, GitHubApi
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import strict_loads
from mod_base.model.documents import validate_kit_stamp
from mod_base.model.validators import DocumentError

OWNER = "MB9"
PIN_LINE = re.compile(
    r"^\s*(?:-\s+)?uses:\s+The-Plum-Team/mod-base/(\S+)@([0-9a-f]{40})\s+#\s+(v\d+\.\d+\.\d+)\s*$", re.ASCII
)
KIT_TOKEN = "The-Plum-Team/mod-base"
DIGESTED_DIRS = ("src", "site", "requirements")
STAMP_NAME = "MOD_BASE_KIT.json"
OVERLAY_PATH = "out/mod-base-kit"

KIT_REMOTE = "https://github.com/The-Plum-Team/mod-base.git"
#: The only kit paths a mod may reference: the reusable workflows and the composites.
KIT_PATH = re.compile(r"(?:\.github/workflows/[A-Za-z0-9_-]+\.yml|actions/[a-z0-9-]+)", re.ASCII)
#: Any ``owner/repo[/path]@ref`` token naming the kit, whatever its case.
KIT_REFERENCE = re.compile(r"the-plum-team/mod-base(?:/[A-Za-z0-9._/-]*)?@\S", re.IGNORECASE | re.ASCII)
KIT_REPOSITORY_TOKEN = re.compile(r"the-plum-team/mod-base(?![A-Za-z0-9_.-])", re.IGNORECASE | re.ASCII)
#: The kit repository named without a following path: a checkout ``repository:``, a flow value, a
#: quoted or folded scalar. Only a comment-only line may hold it.
KIT_REPOSITORY_BARE = re.compile(r"the-plum-team/mod-base(?:\.git)?(?![A-Za-z0-9_./-])", re.IGNORECASE | re.ASCII)
#: A YAML key (``uses``, ``repository``...) whose value starts with the kit repository.
KIT_VALUE = re.compile(r"^\s*(?:-\s+)?[\"']?[A-Za-z0-9_-]+[\"']?\s*:\s*[\"']?the-plum-team/mod-base(?![A-Za-z0-9_.-])",
                       re.IGNORECASE | re.ASCII)
#: A ``uses`` key where YAML can read one: starting a line (after list dashes or an explicit-key
#: indicator) or inside a flow collection.
USES_KEY = re.compile(r"(?:^\s*(?:[-?]\s+)*|[{\[,]\s*)[\"']?uses[\"']?\s*:", re.ASCII)
USES_VALUE = re.compile(r"(?:^\s*(?:[-?]\s+)*|[{\[,]\s*)[\"']?uses[\"']?\s*:\s*(.*)$", re.ASCII)
#: One YAML double-quoted escape: ``\xNN``, ``\uNNNN``, ``\UNNNNNNNN`` or a single character.
ESCAPE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.ASCII | re.DOTALL)
SIMPLE_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v", "f": "\f",
                  "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0",
                  "L": " ", "P": " "}
TAG = re.compile(r"v(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", re.ASCII)
KIT_PATH_NAME = re.compile(r"[A-Za-z0-9._/-]+", re.ASCII)
LINE_BREAK = re.compile(r"\r\n|\r|\n")
BYTECODE_DIRECTORY = "__pycache__"
ACTION_FILES = ("action.yml", "action.yaml")
#: Lists tracked changes, untracked files and (whatever the ignore rules) ignored files.
STATUS_ARGUMENTS = ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching")
#: The staged directories kit-digest-v1 does not cover, and the listing of their files that the
#: digested ``src/`` carries (regenerated by ``python3 -m mod_base.template.lock --write``).
LOCKED_DIRS = ("template", "tools")
STAGED_LOCK = "src/mod_base/template/staged_files.sha256"
#: ``actions/`` (the pinned composites a mod's gate may check, Block Pops' ``prepare-evidence``),
#: staged from v0.9.2 under a lock of its own. :data:`STAGED_LOCK` keeps listing exactly
#: :data:`LOCKED_DIRS`, so a bootstrap older than v0.9.2, which requires ``template/`` and
#: ``tools/`` to equal that lock and stages no ``actions/``, still stages a newer candidate kit for
#: a controller upgrade (SPEC §1.5).
ACTIONS_DIR = "actions"
ACTIONS_LOCK = "src/mod_base/template/staged_actions.sha256"
MAX_LOCK_BYTES = 1024 * 1024

MAX_PIN_FILES = 512
MAX_PIN_FILE_BYTES = 1024 * 1024
MAX_PIN_TOTAL_BYTES = 16 * 1024 * 1024
MAX_ACTION_ENTRIES = 4096
MAX_KIT_FILES = 20000
MAX_KIT_BYTES = 512 * 1024 * 1024
MAX_TAG_PEELS = 4
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0
GIT_TIMEOUT_SECONDS = 600
#: Every git call ignores configured hooks and filesystem monitors: the kit cache runs no code.
GIT_SAFETY = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false")

_sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class Pin:
    """The single pin of a mod: ``sha`` (40-hex), ``version`` (``vX.Y.Z``) and every referencing
    ``path@line`` location, sorted."""

    sha: str
    version: str
    references: tuple[str, ...]


# -- Pin -------------------------------------------------------------------------------------------


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
    """True when ``line`` ends in an odd run of backslashes: in a double-quoted scalar, an escaped
    line break that joins the next line without a space."""

    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _uses_problem(line: str) -> str | None:
    """Why the ``uses:`` value on ``line`` is not a plain single-line scalar, or ``None``."""

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
            raise MbError(f"{path} is not UTF-8 text", reason="pin") from None
        joined = ""
        for number, line in enumerate(LINE_BREAK.split(text), start=1):
            if joined:
                view = joined + line.lstrip(" \t")
                problem = line_problem(view)
                if problem is not None:
                    raise MbError(f"{path}@{number}: {problem} (joined to the previous line by an escaped line "
                                  f"break): {single_line(view.strip(), limit=300)}", reason="pin")
            else:
                view = line
            joined = view[:-1] if _escaped_break(line) else ""
            match = PIN_LINE.match(line)
            if match is not None:
                if not KIT_PATH.fullmatch(match.group(1)):
                    raise MbError(f"{path}@{number}: {match.group(1)!r} is not a mod-base workflow or action",
                                  reason="pin")
                if not TAG.fullmatch(match.group(3)):
                    raise MbError(f"{path}@{number}: {match.group(3)!r} is not a vX.Y.Z release without leading "
                                  "zeros", reason="pin")
                pairs.add((match.group(2), match.group(3)))
                references.append((path, number))
                continue
            problem = line_problem(line)
            if problem is not None:
                raise MbError(f"{path}@{number}: {problem}: {single_line(line.strip(), limit=300)}", reason="pin")
    if not pairs:
        raise MbError("no mod-base pin: no workflow or action line is 'uses: The-Plum-Team/mod-base/<path>@<40-hex> "
                      "# vX.Y.Z'", reason="pin")
    if len(pairs) != 1:
        found = ", ".join(f"{sha} {version}" for sha, version in sorted(pairs))
        raise MbError(f"every mod-base reference must carry one SHA and version; found {found}", reason="pin")
    (sha, version), = pairs
    return Pin(sha, version, tuple(f"{path}@{number}" for path, number in sorted(references)))


def _directory_state(path: Path) -> bool | None:
    """``None`` when ``path`` is absent, ``True`` when it is anything but a real directory."""

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
        raise MbError(f"cannot read {label}: {exc.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise MbError(f"{label} is not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise MbError(f"{label} exceeds {limit} bytes")
    return data


def _pin_file_paths(repo: Path) -> list[str]:
    paths: list[str] = []
    github = repo / ".github"
    state = _directory_state(github)
    if state is None:
        return paths
    if state:
        raise MbError(".github must be a real directory", reason="pin")
    workflows = github / "workflows"
    state = _directory_state(workflows)
    if state:
        raise MbError(".github/workflows must be a real directory", reason="pin")
    if state is False:
        with os.scandir(workflows) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if entry.name.endswith((".yml", ".yaml")):
                    if not entry.is_file(follow_symlinks=False):
                        raise MbError(f".github/workflows/{entry.name} must be a regular file", reason="pin")
                    paths.append(f".github/workflows/{entry.name}")
    actions = github / "actions"
    state = _directory_state(actions)
    if state:
        raise MbError(".github/actions must be a real directory", reason="pin")
    if state is False:
        visited = 0
        pending: list[tuple[Path, str]] = [(actions, ".github/actions")]
        while pending:
            directory, relative = pending.pop()
            with os.scandir(directory) as entries:
                for entry in sorted(entries, key=lambda item: item.name):
                    visited += 1
                    if visited > MAX_ACTION_ENTRIES:
                        raise MbError(f".github/actions holds more than {MAX_ACTION_ENTRIES} entries", reason="pin")
                    child = f"{relative}/{entry.name}"
                    if entry.is_symlink():
                        raise MbError(f"{child} must not be a symlink", reason="pin")
                    if entry.is_dir(follow_symlinks=False):
                        pending.append((Path(entry.path), child))
                    elif entry.name in ACTION_FILES:
                        if not entry.is_file(follow_symlinks=False):
                            raise MbError(f"{child} must be a regular file", reason="pin")
                        paths.append(child)
    if len(paths) > MAX_PIN_FILES:
        raise MbError(f"more than {MAX_PIN_FILES} workflow and action files", reason="pin")
    return sorted(paths)


def read_pin_files(repo: Path) -> dict[str, bytes]:
    """Read (bounded, no symlinks) every workflow and action file of ``repo``."""

    files: dict[str, bytes] = {}
    total = 0
    for relative in _pin_file_paths(repo):
        data = _read_bounded(repo / relative, MAX_PIN_FILE_BYTES, relative)
        total += len(data)
        if total > MAX_PIN_TOTAL_BYTES:
            raise MbError(f"workflow and action files exceed {MAX_PIN_TOTAL_BYTES} bytes", reason="pin")
        files[relative] = data
    return files


def parse_pin(repo: Path) -> Pin:
    """Read (bounded) the mod's workflow and action files and parse its single pin."""

    return parse_pin_files(read_pin_files(Path(os.path.abspath(repo))))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MbError(f"{label} is not a JSON object", reason="pin-release")
    return value


def resolve_tag(version: str, api: GitHubApi) -> str:
    """The commit the kit tag ``version`` peels to (``git/ref/tags`` then ``git/tags``)."""

    if not isinstance(version, str) or not TAG.fullmatch(version):
        raise MbError(f"{single_line(version, limit=40)!r} is not a vX.Y.Z tag", reason="pin")
    try:
        reference = _object(api.get_json(f"/repos/{KIT_TOKEN}/git/ref/tags/{version}"), "tag ref")
    except ApiNotFound:
        raise MbError(f"tag {version} does not exist in {KIT_TOKEN}", reason="pin-release") from None
    if reference.get("ref") != f"refs/tags/{version}":
        raise MbError(f"tag {version} does not exist in {KIT_TOKEN}", reason="pin-release")
    target = _object(reference.get("object"), "tag ref object")
    for _ in range(MAX_TAG_PEELS):
        if target.get("type") != "tag":
            break
        tag_sha = target.get("sha")
        if not grammar.is_match(grammar.SHA1, tag_sha):
            raise MbError(f"tag {version} names a malformed tag object", reason="pin-release")
        target = _object(_object(api.get_json(f"/repos/{KIT_TOKEN}/git/tags/{tag_sha}"), "tag").get("object"),
                         "tag object")
    commit = target.get("sha")
    if target.get("type") != "commit" or not grammar.is_match(grammar.SHA1, commit):
        raise MbError(f"tag {version} does not peel to a commit", reason="pin-release")
    return commit


def require_reachable(sha: str, api: GitHubApi) -> None:
    """Require ``compare/<sha>...main`` to be ``ahead`` or ``identical`` with ``behind_by == 0``.

    ``per_page=1`` keeps the comparison small; ``status`` and ``behind_by`` describe the whole
    range whatever the page size.
    """

    try:
        comparison = _object(api.get_json(f"/repos/{KIT_TOKEN}/compare/{sha}...main", params={"per_page": 1}),
                             "compare")
    except ApiNotFound:
        raise MbError(f"pin {sha} is not a commit of {KIT_TOKEN} main", reason="pin-release") from None
    behind = comparison.get("behind_by")
    if comparison.get("status") not in ("ahead", "identical") or type(behind) is not int or behind != 0:
        raise MbError(f"pin {sha} is not reachable from {KIT_TOKEN} main (status "
                      f"{single_line(comparison.get('status'), limit=40)}, behind_by {single_line(behind, limit=40)}); "
                      "it may be an impostor commit from a fork", reason="pin-release")


def verify_released(pin: Pin, api: GitHubApi) -> None:
    """Require the pin to be reachable from mod-base ``main`` and its tag to peel to the pin."""

    require_reachable(pin.sha, api)
    tagged = resolve_tag(pin.version, api)
    if tagged != pin.sha:
        raise MbError(f"tag {pin.version} peels to {tagged}, not the pin {pin.sha}", reason="pin-release")


def verify(repo: Path, *, network: bool, api: GitHubApi | None = None) -> Pin:
    """Pin consistency; with ``network`` also ``compare/<pin>...main`` is ``ahead|identical`` with
    ``behind_by == 0`` and ``git/ref/tags/<version>`` peels to the pin (``api`` required)."""

    pin = parse_pin(repo)
    if network:
        if api is None:
            raise MbError("pin verify --network needs a GitHub API client", reason="environment")
        verify_released(pin, api)
    return pin


# -- kit-digest-v1 ---------------------------------------------------------------------------------


def _listing(root: Path, tops: Sequence[str], *, locked: bool) -> str:
    """The ``"<sha256>  ./<path>\\n"`` listing, sorted bytewise, of every file under ``tops``.

    Symlinks, special files, ``__pycache__`` and paths outside ``[A-Za-z0-9._/-]`` are refused.
    kit-digest-v1 (``locked`` false) requires every top directory and refuses executable files; the
    staged-file lock (``locked`` true) addresses content only, so a top may be absent and the mode
    is irrelevant.
    """

    root = Path(os.path.abspath(root))
    if _directory_state(root) is not False:
        raise MbError(f"kit root {root} is not a real directory", reason="kit-digest")
    records: list[tuple[bytes, str]] = []
    total = 0
    for top in tops:
        base = root / top
        state = _directory_state(base)
        if state is None and locked:
            continue
        if state is not False:
            raise MbError(f"kit {top}/ is missing or not a real directory", reason="kit-digest")
        pending = [(base, top)]
        while pending:
            directory, relative = pending.pop()
            with os.scandir(directory) as entries:
                listed = list(entries)
            for entry in listed:
                child = f"{relative}/{entry.name}"
                status = entry.stat(follow_symlinks=False)
                if entry.name == BYTECODE_DIRECTORY:
                    raise MbError(f"kit tree holds bytecode: {child}", reason="kit-digest")
                if not KIT_PATH_NAME.fullmatch(child):
                    raise MbError(f"kit path {child!r} has characters outside [A-Za-z0-9._/-]", reason="kit-digest")
                if stat.S_ISDIR(status.st_mode):
                    pending.append((Path(entry.path), child))
                    continue
                if not stat.S_ISREG(status.st_mode):
                    raise MbError(f"kit entry {child} is a symlink or special file", reason="kit-digest")
                if status.st_mode & 0o111 and not locked:
                    raise MbError(f"kit file {child} is executable", reason="kit-digest")
                total += status.st_size
                if len(records) >= MAX_KIT_FILES or total > MAX_KIT_BYTES:
                    raise MbError("kit tree exceeds its file or byte bound", reason="kit-digest")
                data = _read_bounded(Path(entry.path), MAX_KIT_BYTES, child)
                if len(data) != status.st_size:
                    raise MbError(f"kit file {child} changed while it was hashed", reason="kit-digest")
                records.append((f"./{child}".encode("ascii"), hashlib.sha256(data).hexdigest()))
    return "".join(f"{hexdigest}  {path.decode('ascii')}\n" for path, hexdigest in sorted(records))


def kit_tree_digest(root: Path) -> str:
    """``sha256:<hex>`` kit-digest-v1 of ``root`` (the kit checkout root)."""

    listing = _listing(root, DIGESTED_DIRS, locked=False)
    if not listing:
        raise MbError("kit tree holds no files", reason="kit-digest")
    return "sha256:" + hashlib.sha256(listing.encode("ascii")).hexdigest()


def staged_listing(root: Path) -> bytes:
    """The listing of ``template/`` and ``tools/`` of the kit root ``root``: the bytes
    :data:`STAGED_LOCK` must hold."""

    return _listing(root, LOCKED_DIRS, locked=True).encode("ascii")


def actions_listing(root: Path) -> bytes:
    """The listing of ``actions/`` of the kit root ``root``: the bytes :data:`ACTIONS_LOCK` must hold."""

    return _listing(root, (ACTIONS_DIR,), locked=True).encode("ascii")


def verify_staged_files(root: Path) -> None:
    """Require ``template/`` and ``tools/`` of ``root`` to equal the :data:`STAGED_LOCK` listing
    inside its digested ``src/`` (verify the digest first), and a present ``actions/`` to equal the
    :data:`ACTIONS_LOCK` listing there. An absent ``actions/`` binds nothing, so an overlay staged by
    a bootstrap older than v0.9.2 (which stages none) stays valid; an ``actions/`` without
    :data:`ACTIONS_LOCK` (a kit older than v0.9.2) is unbound and refused."""

    root = Path(root)
    expected = _read_bounded(root / STAGED_LOCK, MAX_LOCK_BYTES, STAGED_LOCK)
    if staged_listing(root) != expected:
        raise MbError(f"the kit's template/ and tools/ do not match its {STAGED_LOCK}", reason="kit-digest")
    if _directory_state(root / ACTIONS_DIR) is None:
        return
    if not os.path.lexists(root / ACTIONS_LOCK):
        raise MbError(f"the kit's {ACTIONS_DIR}/ is not bound by an {ACTIONS_LOCK}", reason="kit-digest")
    if actions_listing(root) != _read_bounded(root / ACTIONS_LOCK, MAX_LOCK_BYTES, ACTIONS_LOCK):
        raise MbError(f"the kit's {ACTIONS_DIR}/ does not match its {ACTIONS_LOCK}", reason="kit-digest")


# -- Stamp -----------------------------------------------------------------------------------------


def read_stamp(directory: Path) -> dict[str, Any]:
    """Read and validate ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) in ``directory``."""

    raw = _read_bounded(Path(directory) / STAMP_NAME, lim.MAX_KIT_STAMP_BYTES, STAMP_NAME)
    document = strict_loads(raw, label=STAMP_NAME, max_bytes=lim.MAX_KIT_STAMP_BYTES)
    try:
        return validate_kit_stamp(document)
    except DocumentError as exc:
        raise MbError(f"{STAMP_NAME}: {exc}", reason="kit-stamp") from None


def stamp_document(pin: Pin, digest: str) -> dict[str, Any]:
    """The stamp ``stage`` writes for ``pin`` (its version without the ``v``)."""

    return {"kind": "mod-base.kit-stamp", "schema_version": 1, "sha": pin.sha, "version": pin.version[1:],
            "tree_digest": digest}


# -- Kit resolution --------------------------------------------------------------------------------


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
        raise Unavailable(f"git {arguments[0]} failed: {single_line(exc, limit=300)}") from None


def _git_ok(arguments: Sequence[str], *, cwd: Path, ceiling: Path) -> str:
    result = _git(arguments, cwd=cwd, ceiling=ceiling)
    if result.returncode != 0:
        raise Unavailable(f"git {' '.join(arguments[:2])} failed: "
                          f"{single_line(result.stderr.strip() or result.returncode, limit=300)}")
    return result.stdout


def checkout_head(path: Path) -> str:
    """The HEAD commit of the clean git checkout ``path`` (tracked files unmodified)."""

    path = Path(os.path.abspath(path))
    if _directory_state(path) is not False:
        raise Unavailable(f"{path} is not a directory")
    head = _git_ok(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path, ceiling=path.parent).strip()
    if not grammar.is_match(grammar.SHA1, head):
        raise Unavailable(f"{path} has no SHA-1 HEAD commit")
    if _git_ok(["status", "--porcelain", "--untracked-files=no"], cwd=path, ceiling=path.parent):
        raise Unavailable(f"{path} has uncommitted changes")
    return head


def release_tags_at(path: Path, sha: str) -> list[str]:
    """The ``vX.Y.Z`` tags of the git checkout ``path`` that point at ``sha``, sorted."""

    listing = _git_ok(["tag", "--points-at", sha, "--list", "v*"], cwd=path, ceiling=Path(path).parent)
    return sorted(tag for tag in listing.split() if TAG.fullmatch(tag))


def unclean_paths(status: str) -> list[str]:
    """Paths of ``git status`` (:data:`STATUS_ARGUMENTS`) output that make a kit checkout unclean.

    Every entry counts: a tracked change, and any untracked or ignored path, bytecode included
    (Python would load a planted ``__pycache__`` file in place of the verified source).
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
        raise Unavailable(f"{path} is not a git checkout; delete it to fetch the kit again")
    head = _git_ok(["rev-parse", "--verify", "HEAD^{commit}"], cwd=path, ceiling=path.parent).strip()
    if head != sha:
        raise Unavailable(f"{path} is at {single_line(head, limit=40)}, not the pin {sha}; delete it to fetch the kit "
                          "again")
    unclean = unclean_paths(_git_ok(list(STATUS_ARGUMENTS), cwd=path, ceiling=path.parent))
    if unclean:
        raise Unavailable(f"{path} has local changes ({single_line(', '.join(unclean[:5]), limit=200)}); "
                          "delete it to fetch the kit again")


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def cache_root(environ: Mapping[str, str], repo: Path) -> Path:
    """``<cache>/mod-base`` (``MOD_BASE_CACHE_DIR``, else the platform cache directory); never
    inside ``repo``."""

    explicit = environ.get("MOD_BASE_CACHE_DIR", "")
    if explicit:
        if not os.path.isabs(explicit):
            raise Unavailable("MOD_BASE_CACHE_DIR must be an absolute path")
        base = Path(explicit)
    elif sys.platform == "darwin":
        base = Path(environ.get("HOME") or Path.home()) / "Library" / "Caches"
    elif os.name == "nt":
        local = environ.get("LOCALAPPDATA", "")
        if not os.path.isabs(local):
            raise Unavailable("LOCALAPPDATA is not set; set MOD_BASE_CACHE_DIR")
        base = Path(local)
    else:
        xdg = environ.get("XDG_CACHE_HOME", "")
        base = Path(xdg) if os.path.isabs(xdg) else Path(environ.get("HOME") or Path.home()) / ".cache"
    root = Path(os.path.abspath(base)) / "mod-base"
    if _within(os.path.realpath(root), os.path.realpath(repo)):
        raise Unavailable(f"the kit cache {root} must lie outside the repository")
    return root


def _require_kit_tree(root: Path, label: str) -> Path:
    try:
        status = os.lstat(root / "src" / "mod_base" / "__init__.py")
    except OSError:
        status = None
    if _directory_state(root) is not False or status is None or not stat.S_ISREG(status.st_mode):
        raise Unavailable(f"{label} {root} is not a mod-base kit root (no src/mod_base/__init__.py)")
    return root


def _verify_overlay(overlay: Path, pin: Pin) -> Path:
    if _directory_state(overlay.parent) or _directory_state(overlay):
        raise Unavailable(f"{overlay} must be a real directory")
    try:
        stamp = read_stamp(overlay)
    except MbError as exc:
        raise Unavailable(f"the staged kit {overlay} is invalid: {exc}") from None
    if stamp["sha"] != pin.sha or stamp["version"] != pin.version[1:]:
        raise Unavailable(f"the staged kit {overlay} is {stamp['sha']} {stamp['version']}, not the pin "
                          f"{pin.sha} {pin.version}; delete it or stage it again")
    try:
        actual = kit_tree_digest(overlay)
        if actual != stamp["tree_digest"]:
            raise MbError(f"digest {actual} does not equal its stamp {stamp['tree_digest']}")
        verify_staged_files(overlay)
    except MbError as exc:
        raise Unavailable(f"the staged kit {overlay} is invalid: {exc}") from None
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

    The fetch happens in a private staging directory next to ``destination`` and is published
    with one rename, so a concurrent fetch or an interrupted one never leaves a half-populated cache.
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
            failure = single_line(result.stderr.strip() or result.returncode, limit=300)
            if attempt < FETCH_ATTEMPTS:
                _sleep(FETCH_BACKOFF_SECONDS * attempt)
        else:
            raise Unavailable(f"cannot fetch mod-base {pin.sha}: {failure}")
        _git_ok(["-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"], cwd=staging,
                ceiling=parent)
        verify_checkout(staging, pin.sha)
        try:
            os.rename(staging, destination)
        except OSError:
            if not os.path.lexists(destination):
                raise Unavailable(f"cannot publish the fetched kit to {destination}") from None
        verify_checkout(destination, pin.sha)
        return destination
    finally:
        _remove_tree(staging)


def cached_kit(pin: Pin, environ: Mapping[str, str], repo: Path) -> tuple[Path, str]:
    """The verified user-cache kit of ``pin``, fetched when absent; returns ``(root, source)``."""

    destination = cache_root(environ, repo) / pin.sha
    if os.path.lexists(destination):
        verify_checkout(destination, pin.sha)
        source = "cache"
    else:
        fetch_pin(pin, destination)
        source = "fetch"
    return _require_kit_tree(destination, "cached kit"), source


def resolve(repo: Path, environ: Mapping[str, str], *, overlay: bool = True,
            allow_unpinned: bool = True) -> tuple[Path, Pin, str]:
    """``(kit root, pin, source)`` with ``source`` in ``overlay|environment|unpinned|cache|fetch``."""

    repo = Path(os.path.abspath(repo))
    pin = parse_pin(repo)
    staged = repo / OVERLAY_PATH
    if overlay and os.path.lexists(staged):
        return _verify_overlay(staged, pin), pin, "overlay"
    kit_sha = environ.get("MOD_BASE_KIT_SHA", "")
    if kit_sha and kit_sha != pin.sha:
        raise Unavailable(f"MOD_BASE_KIT_SHA {single_line(kit_sha, limit=60)} does not equal the pin {pin.sha}")
    kit_path_value = environ.get("MOD_BASE_KIT_PATH", "")
    if kit_path_value:
        root = Path(os.path.abspath(kit_path_value))
        if kit_sha:
            return _require_kit_tree(root, "MOD_BASE_KIT_PATH"), pin, "environment"
        if not allow_unpinned:
            raise Unavailable("MOD_BASE_KIT_PATH without MOD_BASE_KIT_SHA is not accepted here")
        if environ.get("MOD_BASE_ALLOW_UNPINNED") != "1" or "CI" in environ or "GITHUB_ACTIONS" in environ:
            raise Unavailable("MOD_BASE_KIT_PATH without MOD_BASE_KIT_SHA is a developer override: set "
                              "MOD_BASE_ALLOW_UNPINNED=1, and it is always refused when CI or GITHUB_ACTIONS is set")
        return _require_kit_tree(root, "MOD_BASE_KIT_PATH"), pin, "unpinned"
    root, source = cached_kit(pin, environ, repo)
    return root, pin, source


def kit_path(repo: Path, environ: Mapping[str, str]) -> Path:
    """Resolve the kit root for ``repo`` in the bootstrap order (overlay stamp, env, user cache,
    anonymous fetch), verifying each candidate; raise :class:`mod_base.errors.Unavailable`.

    It also turns off bytecode writing for this process (``sys.dont_write_bytecode``): importing
    the kit must never add ``__pycache__`` to a verified tree, which would then be refused.
    """

    root = resolve(repo, environ)[0]
    sys.dont_write_bytecode = True
    return root
