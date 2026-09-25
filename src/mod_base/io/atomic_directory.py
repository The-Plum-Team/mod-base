"""Descriptor-bound exclusive directory publication (MB1).

Port of Block Pops ``scripts/lib/atomic_directory.py``: a writer fills a private ``0700`` stage
next to ``output`` through a directory descriptor; the stage is published with
``renameat2(RENAME_NOREPLACE)`` (Linux) or ``renameatx_np(RENAME_EXCL)`` (macOS) so an existing
output is never replaced, and every identity (parent, stage) is re-bound before and after the
writer. A failed writer leaves no stage behind. Unsupported hosts fail before creating anything.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from mod_base.errors import MbError

OWNER = "MB1"
T = TypeVar("T")


class AtomicDirectoryError(MbError):
    """An owned output cannot be safely written or published (exit 2)."""

    default_reason = "unsafe-output"


def atomic_directory(output: Path, writer: Callable[[Path, int], T]) -> T:
    """Create ``output`` atomically: call ``writer(stage_path, stage_fd)`` and publish the stage.

    Raises :class:`AtomicDirectoryError` if ``output`` exists, its parent is not a real directory,
    an identity changed, or exclusive rename is unavailable. Returns the writer's result.
    """

    raise NotImplementedError("owned by MB1")


def write_new(stage: int, relative: str, data: bytes) -> None:
    """Create ``relative`` (canonical POSIX path, parents created ``0700``) under the stage
    descriptor with ``O_CREAT|O_EXCL|O_NOFOLLOW``, write ``data``, fsync, and chmod ``0644``."""

    raise NotImplementedError("owned by MB1")
