"""The staged-file locks ``src/mod_base/template/staged_files.sha256`` and
``src/mod_base/template/staged_actions.sha256`` (MB9).

``stage`` copies ``template/``, ``tools/`` and (from v0.9.2) ``actions/`` into the Block Pops
sandbox overlay next to the digested ``src/``, ``site/`` and ``requirements/``, but kit-digest-v1
and its five-key stamp cover only the digested directories. ``template check`` reads its manifest
and managed bytes from the overlay's ``template/`` and a mod's gate may check the pinned composites
in ``actions/``, so those files must be bound too: the digested ``src/`` carries the listing of
every ``template/`` and ``tools/`` file (:data:`mod_base.pin.STAGED_LOCK`,
:func:`mod_base.pin.staged_listing`) and of every ``actions/`` file
(:data:`mod_base.pin.ACTIONS_LOCK`, :func:`mod_base.pin.actions_listing`), in the kit-digest-v1
line format, and kit resolution refuses an overlay whose files differ from them. ``actions/`` has a
lock of its own so that the first one keeps the exact bytes a bootstrap older than v0.9.2 checks.

Regenerate them after any change below ``actions/``, ``template/`` or ``tools/``, then refresh the
tree-digest literal, because the locks live in ``src/``::

    PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock --write
    python3 tools/update_tree_digest.py --write
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mod_base import runtime
from mod_base.errors import MbError, describe
from mod_base.pin import ACTIONS_LOCK, STAGED_LOCK, actions_listing, staged_listing

#: Every staged-file lock with the listing it must hold.
LOCKS: tuple[tuple[str, Callable[[Path], bytes]], ...] = ((STAGED_LOCK, staged_listing),
                                                          (ACTIONS_LOCK, actions_listing))


def recorded(kit_root: Path, lock: str = STAGED_LOCK) -> bytes | None:
    """The lock ``lock`` (a :data:`LOCKS` path) ``kit_root`` carries, or ``None`` when it has none."""

    path = Path(kit_root) / lock
    try:
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def stale(kit_root: Path) -> list[str]:
    """The :data:`LOCKS` paths whose recorded bytes differ from the listing of ``kit_root``."""

    return [lock for lock, listing in LOCKS if recorded(kit_root, lock) != listing(kit_root)]


def write(kit_root: Path) -> bool:
    """Rewrite every stale lock of ``kit_root``; return whether any changed."""

    changed = False
    for lock, listing in LOCKS:
        data = listing(kit_root)
        if recorded(kit_root, lock) == data:
            continue
        target = Path(kit_root) / lock
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        changed = True
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m mod_base.template.lock",
                                     description="check or rewrite the staged-file locks of the kit")
    parser.add_argument("--root", type=Path, default=None, help="kit root (default: this kit)")
    parser.add_argument("--write", action="store_true", help="rewrite the locks that are stale")
    arguments = parser.parse_args(argv)
    root = arguments.root if arguments.root is not None else runtime.kit_root()
    names = " and ".join(lock for lock, _ in LOCKS)
    try:
        if arguments.write:
            print(f"{'wrote' if write(root) else 'unchanged'} {names}")
            return 0
        outdated = stale(root)
        if outdated:
            print(f"{', '.join(outdated)} stale: run python3 -m mod_base.template.lock --write", file=sys.stderr)
            return 1
    except MbError as exc:
        print(f"mod_base.template.lock: error: {describe(exc)}", file=sys.stderr)
        return 2
    print(f"{names} current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
