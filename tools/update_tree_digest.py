#!/usr/bin/env python3
"""Write or check the ``MB_KIT_TREE_DIGEST`` literal of the callee workflows (SPEC §1.2).

Every job of the Pages callees ``publish.yml``, ``finalize.yml`` and ``rotate.yml`` and of the
Build/E2E callees ``build.yml``, ``select-build.yml``, ``packaged-e2e.yml`` and ``gate-status.yml``
(:data:`CALLEES`) recomputes kit-digest-v1 over its kit checkout (``src/``, ``site/`` and
``requirements/``) and requires it to equal the workflow-level literal. The callee YAML is not
part of the digested tree, so rewriting the literal never changes the digest (no fixed point).
Stage every change under those directories and under ``template/``, ``tools/`` and ``actions/``,
then run from the kit clone and commit the rewritten workflows with it::

    python3 tools/update_tree_digest.py --write    # rewrite every stale literal in place
    python3 tools/update_tree_digest.py --check    # exit 1 and name the stale workflows

The digested ``src/`` carries the staged-file locks ``src/mod_base/template/staged_files.sha256``
and ``src/mod_base/template/staged_actions.sha256``, the listings of every ``template/`` and
``tools/`` file and of every ``actions/`` file (see ``mod_base.template.lock``), so a change there
changes a lock and therefore the digest. Both modes first require the locks the index records to
list exactly the indexed ``template/``, ``tools/`` and ``actions/`` (:func:`staged_lock_current`):
``--check`` exits 1 naming the stale locks; ``--write`` regenerates them through
``mod_base.template.lock`` and exits 1 without touching a literal, because the regenerated locks
must be staged before the index can be digested (stage them and run ``--write`` again).

The literal must equal what a fresh ``actions/checkout`` of the commit produces, so
:func:`tree_digest` hashes the Git index (the content the commit records), never whatever else lies
in the working tree. It refuses (exit 2, naming the paths):

* an index entry the prologue would refuse after checkout: a mode other than ``100644``
  (executable, symlink or submodule), an unmerged entry, a ``__pycache__`` component or a name
  outside ``[A-Za-z0-9._-]``; and
* a working tree whose three directories differ from the index: a tracked file that is modified,
  missing, executable or not a regular file, and every untracked or ignored file (an editor backup,
  a ``.DS_Store``), so the digest always describes exactly the files on disk.

The one tolerated difference is a ``__pycache__`` directory outside the index, which a local
interpreter writes; once staged, it is refused as the prologue refuses it. The staged-file lock
check likewise refuses (exit 2) ``template/``, ``tools/`` and ``actions/`` whose working tree differs
from the index, since the locks are regenerated from the working tree. ``tools/kit_digest.sh`` and
``mod_base.pin.kit_tree_digest`` compute the same value over a checkout
(``tests/test_tree_digest_literal.py`` pins the parity). Stdlib, plus this script's own kit package
(``src/mod_base``) for the staged-file lock, imported without writing bytecode into the digested
tree.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

DIGESTED_DIRS = ("src", "site", "requirements")
#: The ``src/`` of the kit this script belongs to; its ``mod_base`` owns the staged-file lock format.
KIT_SOURCE = Path(__file__).resolve().parents[1] / "src"
#: Every kit workflow that carries the literal: the three Pages callees, then the Build/E2E callees.
CALLEES = (".github/workflows/publish.yml", ".github/workflows/finalize.yml", ".github/workflows/rotate.yml",
           ".github/workflows/build.yml", ".github/workflows/select-build.yml",
           ".github/workflows/packaged-e2e.yml", ".github/workflows/gate-status.yml")
LITERAL = re.compile(r'^  MB_KIT_TREE_DIGEST: "(sha256:[0-9a-f]{64})"$', re.MULTILINE)
NAME = re.compile(r"^[A-Za-z0-9._-]+$")
#: One ``git ls-files -z --stage`` record: mode, object id, stage and path.
INDEX_ENTRY = re.compile(rb"([0-7]{6}) ([0-9a-f]{40}|[0-9a-f]{64}) ([0-3])\t([^\0]+)")
REGULAR_MODE = b"100644"
#: The staged-file lock addresses content only, so an executable ``tools/`` script is listed too.
LOCKED_MODES = (REGULAR_MODE, b"100755")
#: One staged-file lock line: ``<sha256>  ./<path>``.
LOCK_LINE = re.compile(r"([0-9a-f]{64})  \./([A-Za-z0-9._/-]+)")
BYTECODE_DIRECTORY = "__pycache__"
MAX_FILES = 20000
MAX_FILE_BYTES = 64 << 20
MAX_TREE_BYTES = 256 << 20
MAX_WORKFLOW_BYTES = 1 << 20
MAX_NAMED_PATHS = 20
GIT_TIMEOUT_SECONDS = 120
EXECUTABLE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


class DigestError(Exception):
    """The kit tree or a callee workflow cannot be digested or rewritten."""


def _named(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:MAX_NAMED_PATHS])
    return shown if len(paths) <= MAX_NAMED_PATHS else f"{shown} and {len(paths) - MAX_NAMED_PATHS} more"


def _git(root: Path, arguments: Sequence[str], *, stdin: bytes | None = None) -> bytes:
    """Run ``git`` in ``root`` with no inherited ``GIT_*`` redirection; the output is size-bounded."""

    command = ["git", "--literal-pathspecs", "-C", str(root), "-c", "core.fsmonitor=false", *arguments]
    environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            completed = subprocess.run(command, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                                       stdout=output, stderr=errors, env=environment, timeout=GIT_TIMEOUT_SECONDS,
                                       check=False)
        except subprocess.TimeoutExpired:
            raise DigestError(f"git {arguments[0]} did not finish within {GIT_TIMEOUT_SECONDS} s") from None
        if completed.returncode != 0:
            errors.seek(0)
            message = errors.read(4096).decode("utf-8", "replace").strip()
            raise DigestError(f"git {arguments[0]} failed: {message}")
        if os.fstat(output.fileno()).st_size > MAX_TREE_BYTES:
            raise DigestError(f"git {arguments[0]} printed more than {MAX_TREE_BYTES} bytes")
        output.seek(0)
        return output.read()


def _index_entries(root: Path, pathspecs: Sequence[str] = DIGESTED_DIRS, *,
                   tops: Sequence[str] = DIGESTED_DIRS, modes: Sequence[bytes] = (REGULAR_MODE,)
                   ) -> list[tuple[str, bytes]]:
    """The ``(path, object id)`` of every index entry matching ``pathspecs``, validated: each lies
    below one of ``tops`` and has one of ``modes``."""

    toplevel = _git(root, ["rev-parse", "--show-toplevel"]).removesuffix(b"\n")
    if Path(os.fsdecode(toplevel)).resolve() != root.resolve():
        raise DigestError(f"{root} is not the top level of its Git working tree")
    raw = _git(root, ["ls-files", "-z", "--stage", "--", *pathspecs])
    if raw and not raw.endswith(b"\0"):
        raise DigestError("git ls-files printed a truncated index listing")
    entries: list[tuple[str, bytes]] = []
    refused: list[str] = []
    for record in raw[:-1].split(b"\0") if raw else []:
        match = INDEX_ENTRY.fullmatch(record)
        if match is None:
            raise DigestError("git ls-files printed a malformed index entry")
        mode, object_id, stage, raw_path = match.groups()
        path = raw_path.decode("ascii", "replace")
        components = path.split("/")
        if (mode not in modes or stage != b"0" or not raw_path.isascii() or len(components) < 2
                or components[0] not in tops
                or any(not NAME.fullmatch(part) or part == BYTECODE_DIRECTORY for part in components)):
            refused.append(f"{path} ({mode.decode()}, stage {stage.decode()})")
        else:
            entries.append((path, object_id))
        if len(entries) + len(refused) > MAX_FILES:
            raise DigestError(f"the kit index holds more than {MAX_FILES} files")
    if refused:
        allowed = " or ".join(mode.decode() for mode in modes)
        raise DigestError(f"refusing index entries the callee prologue refuses (a mode other than {allowed}, "
                          f"an unmerged entry, __pycache__ or an unsafe name): {_named(refused)}")
    return entries


def _index_digests(root: Path, pathspecs: Sequence[str] = DIGESTED_DIRS, *, tops: Sequence[str] = DIGESTED_DIRS,
                   modes: Sequence[bytes] = (REGULAR_MODE,)) -> dict[str, str]:
    """``{path: sha256}`` of the indexed blobs :func:`_index_entries` selects, read through one
    ``git cat-file --batch``."""

    entries = _index_entries(root, pathspecs, tops=tops, modes=modes)
    if not entries:
        return {}
    raw = _git(root, ["cat-file", "--batch"], stdin=b"".join(object_id + b"\n" for _, object_id in entries))
    digests: dict[str, str] = {}
    offset = 0
    for path, object_id in entries:
        end = raw.find(b"\n", offset, offset + 256)
        header = raw[offset:end].split(b" ") if end >= 0 else []
        if len(header) != 3 or header[:2] != [object_id, b"blob"] or not header[2].isdigit():
            raise DigestError(f"git cat-file returned no blob for {path}")
        size = int(header[2])
        start, stop = end + 1, end + 1 + size
        if size > MAX_FILE_BYTES or raw[stop:stop + 1] != b"\n":
            raise DigestError(f"{path} is not a bounded blob")
        digests[path] = hashlib.sha256(raw[start:stop]).hexdigest()
        offset = stop + 1
    if offset != len(raw):
        raise DigestError("git cat-file printed more than the requested blobs")
    return digests


def _worktree_digests(root: Path) -> dict[str, str]:
    """``{path: sha256}`` of every working-tree file, refusing what ``kit_digest.sh`` refuses.

    ``__pycache__`` directories are skipped; the index check refuses them once they are staged.
    """

    paths: list[str] = []
    for directory in DIGESTED_DIRS:
        top = root / directory
        try:
            info = os.lstat(top)
        except FileNotFoundError:
            raise DigestError(f"{directory}/ is not a real directory") from None
        if not stat.S_ISDIR(info.st_mode):
            raise DigestError(f"{directory}/ is not a real directory")
        pending = [(top, directory)]
        while pending:
            current, relative = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    child = f"{relative}/{entry.name}"
                    mode = entry.stat(follow_symlinks=False).st_mode
                    if entry.name == BYTECODE_DIRECTORY and stat.S_ISDIR(mode):
                        continue
                    if not NAME.fullmatch(entry.name) or entry.name == BYTECODE_DIRECTORY:
                        raise DigestError(f"refusing {child!r} (__pycache__ or unsafe name)")
                    if stat.S_ISDIR(mode):
                        pending.append((Path(entry.path), child))
                    elif not stat.S_ISREG(mode) or mode & EXECUTABLE:
                        raise DigestError(f"refusing {child} (symlink, special file or executable)")
                    else:
                        paths.append(child)
                        if len(paths) > MAX_FILES:
                            raise DigestError(f"the kit tree holds more than {MAX_FILES} files")
    return {path: _file_sha256(root / path) for path in paths}


def _file_sha256(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise DigestError(f"{path} is not a bounded regular file")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) != info.st_size:
        raise DigestError(f"{path} changed while it was read")
    return hashlib.sha256(data).hexdigest()


def _listing(digests: Mapping[str, str]) -> str:
    """The ``sha256sum`` listing of ``{path: sha256}``, sorted bytewise (kit-digest-v1 and the lock)."""

    return "".join(f"{digests[path]}  ./{path}\n" for path in sorted(digests, key=lambda value: value.encode()))


def _listing_digest(digests: Mapping[str, str]) -> str:
    """kit-digest-v1 of ``{path: sha256}``: the ``sha256sum`` listing sorted bytewise, hashed."""

    return "sha256:" + hashlib.sha256(_listing(digests).encode("ascii")).hexdigest()


def _differences(indexed: Mapping[str, str], on_disk: Mapping[str, str]) -> list[str]:
    return sorted(
        [f"{path} (untracked or ignored)" for path in on_disk.keys() - indexed.keys()]
        + [f"{path} (missing from the working tree)" for path in indexed.keys() - on_disk.keys()]
        + [f"{path} (not staged)" for path in indexed.keys() & on_disk.keys() if indexed[path] != on_disk[path]])


def tree_digest(root: Path) -> str:
    """kit-digest-v1 of the kit tree the Git index of ``root`` records.

    Raises :class:`DigestError` unless every index entry survives the prologue's refusals and the
    working tree holds exactly the indexed bytes (``__pycache__`` directories excepted).
    """

    indexed = _index_digests(root)
    for directory in DIGESTED_DIRS:
        if not any(path.startswith(f"{directory}/") for path in indexed):
            raise DigestError(f"{directory}/ holds no file in the Git index")
    differences = _differences(indexed, _worktree_digests(root))
    if differences:
        raise DigestError(f"the working tree differs from the Git index; stage or remove: {_named(differences)}")
    return _listing_digest(indexed)


def _import_kit() -> None:
    """Make this script's own kit package (:data:`KIT_SOURCE`) importable, with bytecode writing off
    so the tool never leaves ``__pycache__`` in the digested ``src/``.

    The staged-file lock helpers import ``mod_base`` inside each function, after this call, so
    :func:`tree_digest` stays stdlib-only and every kit name they use is visible where it is used
    (``tests/test_internal_api.py`` requires each to be a documented, frozen name).
    """

    sys.dont_write_bytecode = True
    source = str(KIT_SOURCE)
    if source not in sys.path:
        sys.path.insert(0, source)
    try:
        importlib.import_module("mod_base.template.lock")
    except ImportError as exc:
        raise DigestError(f"cannot import the staged-file lock helpers from {KIT_SOURCE}: {exc}") from None


def _indexed_listing(root: Path, tops: Sequence[str], listing: Callable[[Path], bytes]) -> bytes:
    """The listing of the indexed blobs below ``tops``, in the format of the kit's ``listing``
    function, which must list exactly those bytes from the working tree."""

    _import_kit()
    from mod_base import errors

    names = " and ".join(f"{top}/" for top in tops)
    indexed = _index_digests(root, tops, tops=tops, modes=LOCKED_MODES)
    try:
        listed = listing(root)
    except errors.MbError as exc:
        raise DigestError(f"{names} cannot be listed: {errors.describe(exc)}") from None
    on_disk: dict[str, str] = {}
    for line in listed.decode("ascii").splitlines():
        match = LOCK_LINE.fullmatch(line)
        if match is None:
            raise DigestError("the kit's staged-file listing printed a malformed line")
        on_disk[match.group(2)] = match.group(1)
    differences = _differences(indexed, on_disk)
    if differences:
        raise DigestError(f"{names} {'differs' if len(tops) == 1 else 'differ'} from the Git index, so the "
                          f"staged-file lock cannot describe the commit; stage or remove: {_named(differences)}")
    return _listing(indexed).encode("ascii")


def indexed_staged_listing(root: Path) -> bytes:
    """The staged-file lock the Git index of ``root`` implies: the listing of its indexed
    ``template/`` and ``tools/`` blobs, in ``mod_base.pin.staged_listing``'s format.

    Raises :class:`DigestError` unless those entries are content-only regular files (mode 100644 or
    100755) with safe names and the working tree of both directories holds exactly the indexed
    bytes, as ``mod_base.pin.staged_listing`` lists them, so a lock regenerated from the working
    tree is the lock the commit needs. An absent directory is simply empty.
    """

    _import_kit()
    from mod_base import pin

    return _indexed_listing(root, pin.LOCKED_DIRS, pin.staged_listing)


def indexed_actions_listing(root: Path) -> bytes:
    """The actions lock the Git index of ``root`` implies: the listing of its indexed ``actions/``
    blobs, in ``mod_base.pin.actions_listing``'s format, checked as :func:`indexed_staged_listing`."""

    _import_kit()
    from mod_base import pin

    return _indexed_listing(root, (pin.ACTIONS_DIR,), pin.actions_listing)


def stale_locks(root: Path) -> list[str]:
    """The staged-file locks whose bytes the Git index of ``root`` records differ from what its
    indexed ``template/``, ``tools/`` and ``actions/`` imply (a lock the index lacks is stale)."""

    _import_kit()
    from mod_base import pin

    recorded = _index_digests(root, (pin.STAGED_LOCK, pin.ACTIONS_LOCK))
    wanted = ((pin.STAGED_LOCK, indexed_staged_listing(root)), (pin.ACTIONS_LOCK, indexed_actions_listing(root)))
    return [lock for lock, listing in wanted if recorded.get(lock) != hashlib.sha256(listing).hexdigest()]


def staged_lock_current(root: Path) -> bool:
    """Whether every staged-file lock the Git index of ``root`` records is current
    (:func:`stale_locks` is empty)."""

    return not stale_locks(root)


def _stale_lock(root: Path, stale: Sequence[str], *, write: bool) -> int:
    """Report stale staged-file locks (exit 1). ``write`` first regenerates the working-tree locks
    through ``mod_base.template.lock``; no literal is written until the regenerated locks are
    staged, because only an index that records them can be digested."""

    _import_kit()
    from mod_base import errors
    from mod_base.template import lock

    names = " and ".join(stale)
    if not write:
        print(f"update_tree_digest: stale {names}: they must list the staged template/, tools/ and actions/; "
              "run python3 tools/update_tree_digest.py --write, stage the locks and run it again", file=sys.stderr)
        return 1
    try:
        regenerated = lock.write(root)
    except errors.MbError as exc:
        raise DigestError(f"cannot regenerate {names}: {errors.describe(exc)}") from None
    state = "regenerated" if regenerated else "the working-tree copy is current but not staged:"
    print(f"update_tree_digest: {state} {names}; stage {'it' if len(stale) == 1 else 'them'} and run "
          "python3 tools/update_tree_digest.py --write again (no literal was written)", file=sys.stderr)
    return 1


def _read_workflow(workflow: Path) -> str:
    descriptor = os.open(workflow, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise DigestError(f"{workflow} is not a regular file")
        data = stream.read(MAX_WORKFLOW_BYTES + 1)
    if len(data) > MAX_WORKFLOW_BYTES:
        raise DigestError(f"{workflow} is larger than {MAX_WORKFLOW_BYTES} bytes")
    return data.decode("utf-8")


def read_literal(workflow: Path) -> str:
    """The single workflow-level ``MB_KIT_TREE_DIGEST`` literal of a callee workflow."""

    matches = LITERAL.findall(_read_workflow(workflow))
    if len(matches) != 1:
        raise DigestError(f"{workflow} must hold exactly one workflow-level MB_KIT_TREE_DIGEST literal")
    return matches[0]


def _replace_literal(workflow: Path, digest: str) -> None:
    text = _read_workflow(workflow)
    updated = LITERAL.sub(f'  MB_KIT_TREE_DIGEST: "{digest}"', text, count=1)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{workflow.name}.", dir=workflow.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(updated)
        os.chmod(temporary, stat.S_IMODE(os.stat(workflow).st_mode))
        os.replace(temporary, workflow)
    except BaseException:
        os.unlink(temporary)
        raise


def stale_workflows(root: Path, digest: str) -> list[str]:
    """The callee workflows (relative paths) whose literal differs from ``digest``."""

    return [relative for relative in CALLEES if read_literal(root / relative) != digest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true",
                      help="regenerate stale staged-file locks (exit 1: stage them and rerun), else rewrite every "
                           "stale literal")
    mode.add_argument("--check", action="store_true", help="exit 1 when a staged-file lock or any literal is stale")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="the kit clone (default: this script's kit)")
    args = parser.parse_args(argv)
    try:
        stale_lock_paths = stale_locks(args.root)
        if stale_lock_paths:
            return _stale_lock(args.root, stale_lock_paths, write=args.write)
        digest = tree_digest(args.root)
        stale = stale_workflows(args.root, digest)
        if args.write:
            for relative in stale:
                _replace_literal(args.root / relative, digest)
    except (DigestError, OSError, UnicodeDecodeError) as exc:
        print(f"update_tree_digest: {exc}", file=sys.stderr)
        return 2
    if args.check and stale:
        print(f"update_tree_digest: stale MB_KIT_TREE_DIGEST in {', '.join(stale)}; expected {digest}; "
              "run python3 tools/update_tree_digest.py --write", file=sys.stderr)
        return 1
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
