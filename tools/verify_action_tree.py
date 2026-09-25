#!/usr/bin/env python3
"""Verify the executing composite's kit against the pin of the checked-out mod (SPEC §1.4).

The first step of the ``setup``, ``prepare-evidence`` and ``publish-family`` composites runs::

    env -u PYTHONPATH PYTHONPATH="$GITHUB_ACTION_PATH/../../src" PYTHONSAFEPATH=1 \\
      PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \\
      python3 -P "$GITHUB_ACTION_PATH/../../tools/verify_action_tree.py" --mod-root "$MOD_ROOT"

with ``GH_TOKEN`` for this one step. It

1. parses the single pin of the job's mod checkout (``.github/workflows/*.yml`` and
   ``.github/actions/*/action.yml``) with ``mod_base.pin.parse_pin``, the parser the managed
   bootstrap is parity-tested against;
2. reads ``GET repos/The-Plum-Team/mod-base/git/trees/<pin>?recursive=1`` and refuses a truncated
   listing (``mod_base.github.contents.tree``);
3. requires every regular file under ``src/``, ``site/``, ``requirements/``, ``tools/`` and the
   executing action's own directory to carry the Git blob id of the tree entry at the same path,
   with exactly the same file set;
4. appends ``MOD_BASE_KIT_PATH``, ``MOD_BASE_KIT_SHA`` and ``MOD_BASE_KIT_VERSION`` (the pin's
   ``vX.Y.Z``) to ``$GITHUB_ENV``.

Any difference between the executing tree and the pinned tree exits 78 (:class:`ControllerSkew`):
the executing composite does not belong to the checked-out mod, as when a job checks out another
commit than the one whose workflow is running. This detects skew and corruption; it is not an
impostor defense (SPEC §1.7). A ``__pycache__`` directory in the executing tree is a difference
too (it is never part of the pinned tree), which is why every kit Python step runs with
``PYTHONDONTWRITEBYTECODE=1``. Every other rejection exits 2. The kit root is this file's own
grandparent, and the imported ``mod_base`` (stdlib-only, like this tool) must come from that tree's
``src/``, so the code that verifies is part of the verified set.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import mod_base
from mod_base import errors
from mod_base.errors import ControllerSkew, MbError
from mod_base.github import contents
from mod_base.github.api import DEFAULT_BASE_URL, GitHubApi
from mod_base.pin import Pin, parse_pin

#: The composites that run this check (``actions/<name>/action.yml``).
ACTIONS = ("setup", "prepare-evidence", "publish-family")
#: Kit directories every composite verifies besides its own ``actions/<name>/`` directory.
VERIFIED_ROOTS = ("src", "site", "requirements", "tools")
MAX_FILES = 4096
MAX_FILE_BYTES = 32 << 20
MAX_TOTAL_BYTES = 256 << 20
MAX_REPORTED_PATHS = 8
TREE_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
BLOB_ID = re.compile(r"^[0-9a-f]{40}$")
BLOB_MODES = frozenset({"100644", "100755"})
ENV_VALUE = re.compile(r"^[^\x00-\x1f\x7f]{1,4096}$")


def git_blob_id(data: bytes) -> str:
    """The Git object id of ``data`` stored as a blob (SHA-1 of ``blob <size>\\0<data>``)."""

    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def verified_prefixes(action: str) -> tuple[str, ...]:
    """The tree-relative directories (with a trailing ``/``) the ``action`` composite verifies."""

    if action not in ACTIONS:
        raise MbError(f"{action!r} is not a composite that verifies its kit tree", reason="usage")
    return (*(f"{root}/" for root in VERIFIED_ROOTS), f"actions/{action}/")


def _read_blob_id(path: Path, relative: str) -> tuple[str, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ControllerSkew(f"executing kit file {relative} cannot be read: {exc.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ControllerSkew(f"executing kit file {relative} is not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise ControllerSkew(f"executing kit file {relative} exceeds {MAX_FILE_BYTES} bytes")
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) != info.st_size:
        raise ControllerSkew(f"executing kit file {relative} changed while it was read")
    return git_blob_id(data), len(data)


def local_inventory(kit_root: Path, action: str) -> dict[str, str]:
    """``{tree path: blob id}`` of every regular file of the executing kit in the verified set.

    A symlink, special file or unreadable entry is a skew: a checkout of the pinned tree holds only
    regular files there."""

    inventory: dict[str, str] = {}
    total = 0
    for prefix in verified_prefixes(action):
        top = kit_root / prefix.rstrip("/")
        try:
            mode = os.lstat(top).st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode):
            raise ControllerSkew(f"executing kit path {prefix} is not a real directory")
        pending = [top]
        while pending:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    relative = Path(entry.path).relative_to(kit_root).as_posix()
                    entry_mode = entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(entry_mode):
                        pending.append(Path(entry.path))
                        continue
                    if not stat.S_ISREG(entry_mode):
                        raise ControllerSkew(f"executing kit path {relative} is a symlink or special file")
                    if len(inventory) >= MAX_FILES:
                        raise ControllerSkew(f"the executing kit holds more than {MAX_FILES} verified files")
                    inventory[relative], size = _read_blob_id(Path(entry.path), relative)
                    total += size
                    if total > MAX_TOTAL_BYTES:
                        raise ControllerSkew(f"the executing kit exceeds {MAX_TOTAL_BYTES} verified bytes")
    return inventory


def tree_inventory(entries: Sequence[Mapping[str, Any]], action: str) -> dict[str, str]:
    """``{tree path: blob id}`` of the pinned tree's entries in the verified set.

    Entries outside the verified set and directory entries are skipped; inside it, a symlink or
    submodule, a malformed row and a duplicate path are rejected."""

    prefixes = verified_prefixes(action)
    inventory: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str):
            raise MbError("the pinned tree listing holds a malformed entry")
        path, kind, mode, oid = entry["path"], entry.get("type"), entry.get("mode"), entry.get("sha")
        if not path.startswith(prefixes):
            continue
        if not TREE_PATH.fullmatch(path) or ".." in path.split("/"):
            raise MbError(f"the pinned tree listing holds a malformed path {path[:80]!r}")
        if kind == "tree":
            continue
        if kind != "blob" or mode not in BLOB_MODES:
            raise MbError(f"the pinned tree holds a symlink or submodule at {path}")
        if not isinstance(oid, str) or not BLOB_ID.fullmatch(oid):
            raise MbError(f"the pinned tree entry {path} has a malformed blob id")
        if path in inventory:
            raise MbError(f"the pinned tree lists {path} twice")
        if len(inventory) >= MAX_FILES:
            raise MbError(f"the pinned tree holds more than {MAX_FILES} verified files")
        inventory[path] = oid
    return inventory


def _sample(paths: set[str]) -> str:
    listed = sorted(paths)
    shown = ", ".join(listed[:MAX_REPORTED_PATHS])
    return shown + (f" (+{len(listed) - MAX_REPORTED_PATHS} more)" if len(listed) > MAX_REPORTED_PATHS else "")


def compare_inventories(local: Mapping[str, str], pinned: Mapping[str, str], *, pin: str) -> None:
    """Raise :class:`ControllerSkew` unless both inventories hold the same paths and blob ids."""

    if not pinned:
        raise ControllerSkew(f"the pinned tree {pin} holds none of the verified kit files")
    missing = set(pinned) - set(local)
    extra = set(local) - set(pinned)
    changed = {path for path in set(local) & set(pinned) if local[path] != pinned[path]}
    problems = []
    if missing:
        problems.append(f"missing {_sample(missing)}")
    if extra:
        problems.append(f"not in the pinned tree {_sample(extra)}")
    if changed:
        problems.append(f"different content {_sample(changed)}")
    if problems:
        raise ControllerSkew(f"the executing kit is not the tree pinned at {pin}: " + "; ".join(problems))


def verify(kit_root: Path, action: str, mod_root: Path, api: GitHubApi) -> Pin:
    """Steps 1-3 of the module docstring; returns the verified pin."""

    pin = parse_pin(mod_root)
    local = local_inventory(kit_root, action)
    pinned = tree_inventory(contents.tree(api, pin.sha, recursive=True), action)
    compare_inventories(local, pinned, pin=pin.sha)
    return pin


def export(github_env: Path, kit_root: Path, pin: Pin) -> None:
    """Append the kit identity to ``$GITHUB_ENV`` (one ``NAME=value`` line each)."""

    values = {"MOD_BASE_KIT_PATH": str(kit_root), "MOD_BASE_KIT_SHA": pin.sha, "MOD_BASE_KIT_VERSION": pin.version}
    for name, value in values.items():
        if not ENV_VALUE.fullmatch(value):
            raise MbError(f"{name} would not be one printable line", reason="environment")
    with open(github_env, "a", encoding="utf-8") as stream:
        stream.writelines(f"{name}={value}\n" for name, value in values.items())


def executing_action(environ: Mapping[str, str], kit_root: Path) -> str:
    """The name of the composite whose ``GITHUB_ACTION_PATH`` is ``kit_root/actions/<name>``."""

    action_path = environ.get("GITHUB_ACTION_PATH")
    if not action_path:
        raise MbError("GITHUB_ACTION_PATH is not set; run this only as a composite step", reason="environment")
    action_dir = Path(action_path).resolve()
    if action_dir.parent.name != "actions" or action_dir.parent.parent != kit_root:
        raise ControllerSkew(f"the executing action {action_dir} is not a composite of the kit at {kit_root}")
    if action_dir.name not in ACTIONS:
        raise MbError(f"composite {action_dir.name!r} does not verify its kit tree", reason="usage")
    return action_dir.name


def run(argv: Sequence[str], environ: Mapping[str, str], *, api: GitHubApi | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_action_tree", description=__doc__.splitlines()[0])
    parser.add_argument("--mod-root", type=Path, required=True, metavar="DIR", help="the mod checkout")
    args = parser.parse_args(list(argv))
    kit_root = Path(__file__).resolve().parents[1]
    imported = Path(mod_base.__file__).resolve()
    if not imported.is_relative_to(kit_root / "src"):
        raise ControllerSkew(f"mod_base was imported from {imported}, outside the executing kit {kit_root}")
    action = executing_action(environ, kit_root)
    github_env = environ.get("GITHUB_ENV")
    if not github_env:
        raise MbError("GITHUB_ENV is not set", reason="environment")
    if api is None:
        token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN")
        if not token:
            raise MbError("GH_TOKEN is required to read the pinned kit tree", reason="environment")
        if environ.get("GITHUB_API_URL", DEFAULT_BASE_URL) != DEFAULT_BASE_URL:
            raise MbError("the kit tree is read only from https://api.github.com", reason="environment")
        api = GitHubApi(repository=mod_base.KIT_REPOSITORY, token=token, base_url=DEFAULT_BASE_URL)
    pin = verify(kit_root, action, args.mod_root, api)
    export(Path(github_env), kit_root, pin)
    print(f"verified the executing mod-base kit {pin.version} at {pin.sha} ({action})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    return errors.run_main(lambda: run(arguments, os.environ), program="verify_action_tree")


if __name__ == "__main__":
    raise SystemExit(main())
