"""The staged-file lock ``src/mod_base/template/staged_files.sha256`` (MB9).

``stage`` copies ``template/`` and ``tools/`` into the Block Pops sandbox overlay next to the digested
``src/``, ``site/`` and ``requirements/``, but kit-digest-v1 and its five-key stamp cover only the
digested directories. ``template check`` reads its manifest and managed bytes from the overlay's
``template/``, so those files must be bound too: the digested ``src/`` carries this listing of every
``template/`` and ``tools/`` file (:func:`mod_base.pin.staged_listing`, the kit-digest-v1 line
format), and kit resolution refuses an overlay whose files differ from it.

Regenerate it after any change below ``template/`` or ``tools/``, then refresh the tree-digest
literal, because the lock lives in ``src/``::

    PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock --write
    python3 tools/update_tree_digest.py --write
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from mod_base import runtime
from mod_base.errors import MbError, describe
from mod_base.pin import STAGED_LOCK, staged_listing


def recorded(kit_root: Path) -> bytes | None:
    """The lock ``kit_root`` carries, or ``None`` when it has none."""

    path = Path(kit_root) / STAGED_LOCK
    try:
        return path.read_bytes() if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def write(kit_root: Path) -> bool:
    """Rewrite the lock of ``kit_root`` when stale; return whether it changed."""

    data = staged_listing(kit_root)
    if recorded(kit_root) == data:
        return False
    target = Path(kit_root) / STAGED_LOCK
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m mod_base.template.lock",
                                     description="check or rewrite the staged-file lock of the kit")
    parser.add_argument("--root", type=Path, default=None, help="kit root (default: this kit)")
    parser.add_argument("--write", action="store_true", help="rewrite the lock when it is stale")
    arguments = parser.parse_args(argv)
    root = arguments.root if arguments.root is not None else runtime.kit_root()
    try:
        if arguments.write:
            print(f"{'wrote' if write(root) else 'unchanged'} {STAGED_LOCK}")
            return 0
        if recorded(root) != staged_listing(root):
            print(f"{STAGED_LOCK} is stale: run python3 -m mod_base.template.lock --write", file=sys.stderr)
            return 1
    except MbError as exc:
        print(f"mod_base.template.lock: error: {describe(exc)}", file=sys.stderr)
        return 2
    print(f"{STAGED_LOCK} is current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
