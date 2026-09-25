"""Seal a generated output tree against its written bytes (MB1).

Port of Block Pops ``build_site._seal_output``: walk the stage through directory descriptors,
require the entry set to equal exactly the written paths, every file to be a regular single-link
file whose bytes hash to the written bytes, and every directory/file stamp (dev, inode, mode,
nlink, size, mtime_ns, ctime_ns) to stay unchanged across a second, stamp-only pass. The
returned ``recheck`` callables can be run again immediately before publication.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from mod_base.errors import MbError
from mod_base.model import limits

OWNER = "MB1"


class SealError(MbError):
    """The generated tree differs from what was written, or changed while being sealed (exit 2)."""

    default_reason = "seal"


def seal_output(stage_fd: int, expected: Mapping[str, bytes], *, max_files: int = limits.MAX_SITE_FILES,
                max_bytes: int = limits.MAX_SITE_BYTES,
                rechecks: list[Callable[[], None]] | None = None) -> tuple[int, int]:
    """Verify the stage behind ``stage_fd`` holds exactly ``expected`` (relative path -> bytes).

    Returns ``(file_count, total_bytes)``; appends a stamp-only recheck to ``rechecks`` when given.
    Raises :class:`SealError` on any extra/missing entry, link, special file, size, byte or stamp
    difference, or when the bounds are exceeded (checked before any hashing).
    """

    raise NotImplementedError("owned by MB1")
