"""Private helpers shared by the evidence modules (MB3): strict bundle reads, exact inventories,
new-file writes and the size policies of source and derivative images."""

from __future__ import annotations

import dataclasses
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.imaging.metrics import SizePolicy
from mod_base.io.tree import file_records, read_child_file
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file, strict_loads
from mod_base.model.documents import thumbnail_size
from mod_base.runtime import Invocation

MANIFEST = "manifest.json"
EXPECTATION = "expectation.json"
EXTENSIONS = "extensions.json"
SELECTION = "selection.json"


def fail(message: str, reason: str = "evidence") -> MbError:
    return MbError(message, reason=reason)


def producer_invocation(invocation: Invocation, commit: str) -> Invocation:
    """``invocation`` whose hooks see ``ctx.implementation_sha == commit``: every hook of the
    ``prepare-evidence`` composite (``prepare``, its fresh-process ``validate --kind handoff`` and
    the ``anchor`` verbs) runs with the subject commit (SPEC §4.2), even when the producing run's
    ``GITHUB_SHA`` is a controller commit."""

    return dataclasses.replace(invocation, implementation_sha_override=grammar.require_sha1(commit, "subject commit"))


def real_directory(path: Path, label: str) -> Path:
    """``path`` made absolute; it must be an existing real directory (not a symlink)."""

    candidate = Path(os.path.abspath(path))
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise fail(f"cannot inspect the {label}: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise fail(f"the {label} must be a real directory")
    return candidate


def read_document(root: Path, name: str, *, max_bytes: int, label: str) -> tuple[Any, bytes]:
    """Read ``root/name`` without following any symlink and decode it strictly. Every JSON file of a
    kit bundle is written as ``canonical_json``, so any other encoding of the same value is refused
    (one document, one byte sequence, one hash)."""

    raw = read_child_file(root, name, max_bytes=max_bytes)
    value = strict_loads(raw, label=label, max_bytes=max_bytes)
    if canonical_json(value) != raw:
        raise fail(f"{label} is not canonical JSON")
    return value, raw


def read_json_argument(path: Path, *, max_bytes: int, label: str) -> tuple[Any, bytes]:
    """Read a JSON document handed to a command (``--selection``, ``--extensions``...)."""

    return read_json_file(Path(os.path.abspath(path)), label=label, max_bytes=max_bytes)


def require_inventory(root: Path, recorded: list[Mapping[str, Any]], *, max_files: int, max_total_bytes: int,
                      max_file_bytes: int, label: str) -> None:
    """The exact directory inventory of ``root`` (hashes and sizes of every file but
    ``manifest.json``) must equal the manifest's ``files`` list."""

    observed = file_records(root, exclude=(MANIFEST,), max_files=max_files + 1,
                            max_total_bytes=max_total_bytes + lim.MAX_MANIFEST_BYTES, max_file_bytes=max_file_bytes)
    if observed != [dict(record) for record in recorded]:
        wanted = {record["path"] for record in recorded}
        present = {record["path"] for record in observed}
        detail = (f"missing={sorted(wanted - present)[:5]} extra={sorted(present - wanted)[:5]}"
                  if wanted != present else "a file hash or size differs")
        raise fail(f"the {label} inventory differs from its manifest ({detail})"[:600])


def write_new_file(path: Path, data: bytes) -> None:
    """Create one new file (never replacing or following anything) holding ``data``."""

    target = Path(os.path.abspath(path))
    real_directory(target.parent, "output directory")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def source_policy(expectation: Mapping[str, Any]) -> SizePolicy:
    """Source PNGs are exactly ``image_policy.source_size`` (SPEC §4.7)."""

    width, height = expectation["image_policy"]["source_size"]
    return SizePolicy.exact(width, height)


def derivative_policy(width: int, height: int, box: list[int] | tuple[int, int]) -> SizePolicy:
    """A derivative is exactly ``thumbnail(source, derivative_box)`` (SPEC §4.7)."""

    return SizePolicy.exact(*thumbnail_size((width, height), box))


def require_equal(observed: Any, wanted: Any, message: str) -> None:
    if canonical_json(observed) != canonical_json(wanted):
        raise fail(message)
