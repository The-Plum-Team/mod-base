"""A private, one-commit Git snapshot of the mod under test (MB10).

``conformance`` never touches the mod's own checkout: it copies the mod's files into a fresh
repository whose first commit is on ``config.canonical_branch`` and runs the whole simulation
there; the later generations of the simulation add documentation-only commits on top of it
(:func:`commit_file`), as pushes to the protected branch would. Adapters read their matrix and contract through ``ctx.read_blob`` (inert Git objects), so
the snapshot is what makes a plain directory (the kit's ``canary/``, a fixture mod) and an
uncommitted working tree testable exactly like a protected head.

The snapshot commits the synthetic workflow files the simulated GitHub serves at every head (the
kit's pin in ``source.workflow`` and every family ``producer.workflow``, ``replace``), so a local
read of the producer's checkout (``family collect``'s kit binding) sees the same pin as the API
does; the mod's own workflow files are never read.

Which files are copied: inside a Git work tree, the tracked and untracked files that are not
ignored (``git ls-files --cached --others --exclude-standard``, relative to the mod root, so build
output and caches never travel); otherwise every file below the root except ``.git``, ``__pycache__``
and compiled Python. Every copied path must be a regular file reached without following a symlink
(a symlinked or special file refuses the snapshot), and the copy is bounded in files and bytes.
Git runs with a sanitized environment (no user or system configuration, no hooks, fixed identity
and dates), so the snapshot commit depends only on the copied bytes.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Mapping
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model import grammar

#: Where ``git`` is looked up (the kit's own search path, then ``PATH``).
GIT_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
GIT_TIMEOUT_SECONDS = 300
#: Bounds of one snapshot (the largest mod today holds under 1,000 files and 20 MiB).
MAX_SNAPSHOT_FILES = 20000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_LISTING_BYTES = 16 * 1024 * 1024
#: The fixed author and committer of every snapshot commit.
IDENTITY = {"GIT_AUTHOR_NAME": "mod-base conformance", "GIT_AUTHOR_EMAIL": "conformance@mod-base.invalid",
            "GIT_COMMITTER_NAME": "mod-base conformance", "GIT_COMMITTER_EMAIL": "conformance@mod-base.invalid",
            "GIT_AUTHOR_DATE": "2026-09-01T12:00:00Z", "GIT_COMMITTER_DATE": "2026-09-01T12:00:00Z"}
_SKIPPED_NAMES = frozenset({".git", "__pycache__"})
_SKIPPED_SUFFIXES = (".pyc", ".pyo")


def _fail(message: str) -> MbError:
    return MbError(message, reason="conformance-snapshot")


def git_executable() -> str:
    found = shutil.which("git", path=GIT_SEARCH_PATH) or shutil.which("git")
    if not found:
        raise _fail("git is required to build the conformance snapshot")
    return found


def git(root: Path, *arguments: str, stdin: bytes | None = None, max_output: int = MAX_LISTING_BYTES) -> bytes:
    """Run ``git -C root <arguments>`` with a sanitized environment and return its bounded stdout."""

    home = root.parent
    environment = {"PATH": f"{GIT_SEARCH_PATH}:{os.path.dirname(git_executable())}", "HOME": str(home),
                   "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                   "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
                   "GIT_CEILING_DIRECTORIES": str(root.parent), **IDENTITY}
    command = [git_executable(), "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", "gc.auto=0", "-C", str(root),
               *arguments]
    try:
        completed = subprocess.run(command, input=stdin, stdin=None if stdin is not None else subprocess.DEVNULL,
                                   capture_output=True, env=environment, timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(f"git {arguments[0]} failed: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _fail(f"git {arguments[0]} failed (exit {completed.returncode}): {detail[-1] if detail else ''}"[:300])
    if len(completed.stdout) > max_output:
        raise _fail(f"git {arguments[0]} wrote more than {max_output} bytes")
    return completed.stdout


def _inside_work_tree(root: Path) -> bool:
    try:
        return git(root, "rev-parse", "--is-inside-work-tree").strip() == b"true"
    except MbError:
        return False


def _listed(root: Path) -> list[str]:
    """The relative paths to copy (see the module docstring), sorted and unique."""

    if _inside_work_tree(root):
        raw = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--full-name")
        prefix = git(root, "rev-parse", "--show-prefix").decode("utf-8").strip()
        paths = []
        for item in raw.split(b"\0"):
            if not item:
                continue
            name = item.decode("utf-8")
            if not name.startswith(prefix):
                raise _fail(f"git listed {name!r} outside the mod root")
            relative = name[len(prefix):]
            if os.path.lexists(root / relative):  # a tracked file deleted in the work tree is not copied
                paths.append(relative)
        return sorted(set(paths))
    paths = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _SKIPPED_NAMES)
        for name in files:
            if name.endswith(_SKIPPED_SUFFIXES):
                continue
            paths.append(Path(directory, name).relative_to(root).as_posix())
            if len(paths) > MAX_SNAPSHOT_FILES:
                raise _fail(f"the mod holds more than {MAX_SNAPSHOT_FILES} files")
    return sorted(paths)


def _copy(root: Path, relative: str, destination: Path) -> int:
    """Copy one regular file reached component by component without following a symlink."""

    if not grammar.is_repo_path(relative):
        raise _fail(f"the mod holds a path the snapshot cannot represent: {relative!r}"[:200])
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _fail(f"{relative} lies below a symlink or non-directory")
    source = root / relative
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise _fail(f"{relative} is a symlink or special file; the snapshot copies regular files only")
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise _fail(f"{relative} changed while it was copied")
    target.write_bytes(data)
    return len(data)


def make_snapshot(source: Path, destination: Path, *, branch: str,
                  replace: Mapping[str, bytes] | None = None) -> tuple[str, str]:
    """Copy the mod at ``source`` into the new directory ``destination``, write each ``replace``
    file (a repository path, created or overwritten) over the copy, commit it on ``branch`` and
    return ``(commit, tree)``."""

    grammar.require(grammar.BRANCH, branch, "canonical branch")
    root = Path(os.path.abspath(source))
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail("--repo must be a real directory")
    destination.mkdir(mode=0o700)
    total = 0
    paths = _listed(root)
    if not paths:
        raise _fail("the mod root holds no files")
    if len(paths) > MAX_SNAPSHOT_FILES:
        raise _fail(f"the mod holds more than {MAX_SNAPSHOT_FILES} files")
    for relative in paths:
        total += _copy(root, relative, destination)
        if total > MAX_SNAPSHOT_BYTES:
            raise _fail(f"the mod holds more than {MAX_SNAPSHOT_BYTES} bytes")
    for relative, data in sorted((replace or {}).items()):
        if not grammar.is_repo_path(relative) or not isinstance(data, bytes):
            raise _fail(f"cannot replace {relative!r} in the snapshot"[:200])
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(target) and not stat.S_ISREG(target.lstat().st_mode):
            raise _fail(f"{relative} is not a regular file of the snapshot")
        target.write_bytes(data)
    git(destination, "init", "-q", f"--initial-branch={branch}")
    git(destination, "add", "-A", "--", ".")
    git(destination, "commit", "-q", "--no-verify", "-m", "mod-base conformance snapshot")
    commit = git(destination, "rev-parse", "HEAD").decode("ascii").strip()
    tree = git(destination, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    return grammar.require_sha1(commit), grammar.require_sha1(tree, "snapshot tree")


def require_snapshot(root: Path, *, branch: str) -> tuple[str, str]:
    """``(commit, tree)`` of a clean snapshot repository checked out on ``branch`` (the child's
    ``--repo``); anything else is refused."""

    root = Path(os.path.abspath(root))
    if git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip() != str(root.resolve()):
        raise _fail("--repo must be the root of the conformance snapshot repository")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise _fail("the conformance snapshot is not clean")
    if git(root, "symbolic-ref", "--short", "HEAD").decode("utf-8").strip() != branch:
        raise _fail(f"the conformance snapshot is not checked out on {branch}")
    commit = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    return grammar.require_sha1(commit), grammar.require_sha1(tree, "snapshot tree")


def commit_file(root: Path, relative: str, data: bytes, *, branch: str) -> tuple[str, str]:
    """Add the new regular file ``relative`` to the clean snapshot repository ``root`` (checked out on
    ``branch``) and commit it on top of the head; return the new ``(commit, tree)``. The later
    generations of the simulation move the protected head this way, as a push would."""

    require_snapshot(root, branch=branch)
    if not grammar.is_repo_path(relative):
        raise _fail(f"not a repository path: {relative!r}"[:200])
    root = Path(os.path.abspath(root))
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        if os.path.lexists(current):
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _fail(f"{relative} lies below a symlink or non-directory")
        else:
            current.mkdir(mode=0o700)
    try:
        descriptor = os.open(root / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                             0o644)
    except OSError as exc:
        raise _fail(f"cannot add {relative} to the snapshot: {exc.strerror or exc}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
    git(root, "add", "--", relative)
    git(root, "commit", "-q", "--no-verify", "-m", f"mod-base conformance: add {relative}")
    return require_snapshot(root, branch=branch)
