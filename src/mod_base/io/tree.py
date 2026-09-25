"""Bounded regular-file walks and descriptor-relative child reads (MB1).

Union of Quick Skin ``evidence._bounded_entries``/``reject_symlinks`` and Block Pops
``_child_file``: every walk refuses symlinks, special files, hard-linked files and paths that are
not canonical bundle paths (:func:`mod_base.model.grammar.is_bundle_path`), and enforces count
and byte bounds before reading any content.
"""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path
from typing import Any

from mod_base.errors import MbError

OWNER = "MB1"


class TreeError(MbError):
    """A directory tree is not a bounded tree of regular files (exit 2)."""

    default_reason = "unsafe-tree"


def regular_files(root: Path, *, max_files: int, max_total_bytes: int, max_file_bytes: int,
                  suffixes: Collection[str] | None = None) -> dict[str, int]:
    """Return ``{relative POSIX path: size}`` for every file under ``root`` (sorted by path).

    ``root`` must be a real directory; every entry must be a directory or a single-link regular
    file; empty files are refused; with ``suffixes`` every file must end with one of them.
    """

    raise NotImplementedError("owned by MB1")


def reject_symlinks(root: Path) -> None:
    """Raise :class:`TreeError` if any entry at or under ``root`` is a symlink or special file."""

    raise NotImplementedError("owned by MB1")


def read_child_file(root: Path, relative: str, *, max_bytes: int) -> bytes:
    """Read ``root/relative`` walking every component through ``O_NOFOLLOW`` directory
    descriptors (no symlink anywhere), stat-stable, 1..``max_bytes`` bytes."""

    raise NotImplementedError("owned by MB1")


def sha256_file(path: Path, *, max_bytes: int) -> str:
    """SHA-256 hex of one stable regular file of at most ``max_bytes`` (streamed, ``O_NOFOLLOW``)."""

    raise NotImplementedError("owned by MB1")


def file_records(root: Path, *, exclude: Collection[str] = (), max_files: int, max_total_bytes: int,
                 max_file_bytes: int) -> list[dict[str, Any]]:
    """Return the exact inventory ``[{path, sha256, size}]`` of ``root`` sorted by path, leaving out
    the relative paths in ``exclude`` (for example ``manifest.json``)."""

    raise NotImplementedError("owned by MB1")
