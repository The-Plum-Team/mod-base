"""Protected Git source inventory and post-quiescence byte verification (MB11).

Inventory bytes must originate from the protected object store at the authenticated tested
tree. Parsing alone does not authenticate that origin. Never run Git in a candidate copy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import authenticate_source_identity
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.github.contents import exact_tree
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import copy_source_files, source_records
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json


class SourceError(MbError):
    """Source inventory or immutable copy failed closed."""

    default_reason = "ci-source"


@dataclass(frozen=True)
class GitSourceEntry:
    path: str
    mode: str
    size: int
    git_blob: str


def _validate_inventory(inventory: tuple[GitSourceEntry, ...]) -> None:
    if (type(inventory) is not tuple or not 1 <= len(inventory) <= limits.MAX_CI_SOURCE_FILES
            or any(type(entry) is not GitSourceEntry for entry in inventory)):
        raise SourceError("source inventory is empty, malformed or exceeds its file cap")
    spellings: dict[str, str] = {}
    leaves: set[str] = set()
    parents: set[str] = set()
    total = 0
    previous = ""
    for entry in inventory:
        if (not grammar.is_repo_path(entry.path) or entry.path <= previous
                or entry.path.split("/")[0].casefold() == ".git"
                or entry.mode not in ("100644", "100755", "120000")
                or type(entry.size) is not int or entry.size < 0
                or not grammar.is_match(grammar.SHA1, entry.git_blob)):
            raise SourceError("source entry has invalid path, ordering, mode, size or blob identity")
        cap = limits.MAX_CI_SOURCE_LINK_BYTES if entry.mode == "120000" else limits.MAX_CI_SOURCE_FILE_BYTES
        if entry.size > cap or (entry.mode == "120000" and entry.size == 0):
            raise SourceError("source entry exceeds its byte cap")
        total += entry.size
        if total > limits.MAX_CI_SOURCE_TREE_BYTES:
            raise SourceError("source inventory exceeds its whole-tree byte cap")
        parts = entry.path.split("/")
        for position in range(1, len(parts) + 1):
            prefix = "/".join(parts[:position])
            folded = prefix.casefold()
            if folded in spellings and spellings[folded] != prefix:
                raise SourceError("source inventory has a case alias")
            if folded not in spellings and len(spellings) + 2 > limits.MAX_CI_SOURCE_ENTRIES:
                raise SourceError("source inventory exceeds its entry cap")
            spellings[folded] = prefix
            if position < len(parts):
                parents.add(prefix)
        leaves.add(entry.path)
        previous = entry.path
    if leaves & parents:
        raise SourceError("source file is also a directory")
    if len(leaves | parents) + 1 > limits.MAX_CI_SOURCE_ENTRIES:
        raise SourceError("source inventory exceeds its entry cap")


def validate_source_inventory(inventory: tuple[GitSourceEntry, ...]) -> None:
    """Validate complete typed inventory shape and bounds; never authenticate tree provenance."""

    _validate_inventory(inventory)


def parse_source_inventory(data: bytes) -> tuple[GitSourceEntry, ...]:
    """Parse bounded ``git ls-tree -r -l -z --full-tree <tested-tree>`` output.

    Only tracked blobs are supported; submodules fail closed. Preserve executable mode,
    empty files and literal symlink targets. Return canonical path ordering.
    """

    if (type(data) is not bytes or not data or len(data) > limits.MAX_CI_SOURCE_LIST_BYTES
            or not data.endswith(b"\0") or not 1 <= data.count(b"\0") <= limits.MAX_CI_SOURCE_FILES):
        raise SourceError("source listing is empty, unterminated or exceeds its cap")
    entries = []
    for record in data[:-1].split(b"\0"):
        match = re.fullmatch(rb"(100644|100755|120000) blob ([0-9a-f]{40}) +([0-9]+)\t([^\t\r\n]+)", record)
        if match is None:
            raise SourceError("source listing contains an unsupported or malformed entry")
        mode, blob, size, path = match.groups()
        # Bound decimal conversion before int(), including on Python 3.11.
        if len(size) > len(str(limits.MAX_CI_SOURCE_FILE_BYTES)) or (len(size) > 1 and size.startswith(b"0")):
            raise SourceError("source listing contains a noncanonical or oversized size")
        try:
            entries.append(GitSourceEntry(path.decode("utf-8", "strict"), mode.decode("ascii"),
                                          int(size), blob.decode("ascii")))
        except UnicodeError as error:
            raise SourceError("source listing path is not strict UTF-8") from error
    inventory = tuple(sorted(entries, key=lambda entry: entry.path))
    _validate_inventory(inventory)
    return inventory


def authenticate_source_inventory(api: GitHubApi, *, identity: dict[str, Any]) -> tuple[GitSourceEntry, ...]:
    """Read a complete immutable tested-tree inventory bracketed by source authentication.

    Reuses the existing bounded GitHub tree transport (including its independent 100,000-entry
    cap). This never runs Git or imports modules in a candidate checkout. Blob bytes must still
    match these object identities during copy verification; policy/pin admission is separate.
    """

    authenticate_source_identity(api, identity)
    inventory = read_source_tree_inventory(api, tree_sha=identity["tested_tree"])
    authenticate_source_identity(api, identity)
    return inventory


def read_source_tree_inventory(api: GitHubApi, *, tree_sha: str) -> tuple[GitSourceEntry, ...]:
    """Read complete exact-tree metadata; caller must independently authenticate tree provenance.

    No commit aliases, truncated listings, unknown modes or incomplete directory closure pass.
    This does not authenticate a live source, approve candidate code or verify blob bytes.
    """

    rows = exact_tree(api, tree_sha, recursive=True)
    if not rows or len(rows) + 1 > limits.MAX_CI_SOURCE_ENTRIES:
        raise SourceError("authenticated source tree is empty or exceeds its entry cap")
    entries: list[GitSourceEntry] = []
    directories: set[str] = set()
    metadata_bytes = 0
    for row in rows:
        metadata_bytes += len(canonical_json(row))
        if metadata_bytes > limits.MAX_CI_SOURCE_LIST_BYTES:
            raise SourceError("authenticated source metadata exceeds its byte cap")
        path = row["path"]
        if not grammar.is_repo_path(path) or path.split("/")[0].casefold() == ".git":
            raise SourceError("authenticated source tree contains an unsafe path")
        if row["type"] == "tree" and row["mode"] == "040000":
            directories.add(path)
        elif row["type"] == "blob" and row["mode"] in {"100644", "100755", "120000"}:
            size = row.get("size")
            if type(size) is not int:
                raise SourceError("authenticated source blob has no exact byte size")
            entries.append(GitSourceEntry(path, row["mode"], size, row["sha"]))
            if len(entries) > limits.MAX_CI_SOURCE_FILES:
                raise SourceError("authenticated source tree exceeds its file cap")
        else:
            raise SourceError("authenticated source tree contains an unsupported entry")
    inventory = tuple(sorted(entries, key=lambda entry: entry.path))
    _validate_inventory(inventory)
    # Expand prefixes only after the shared validator has bounded their complete closure.
    inferred = {"/".join(entry.path.split("/")[:index]) for entry in inventory
                for index in range(1, len(entry.path.split("/")))}
    if directories != inferred:
        raise SourceError("authenticated source directory closure is incomplete or has extra trees")
    return inventory


def verify_source_copy(root: Path, *, inventory: tuple[GitSourceEntry, ...],
                       generated_roots: tuple[str, ...] = ()) -> list[dict[str, str | int]]:
    """Match every tracked byte/mode against protected inventory after account quiescence.

    Generated roots must be independently derived by protected native policy. This is a
    read-only inspector; it neither freezes/copies the source nor replaces .git metadata.
    """

    _validate_inventory(inventory)
    records = source_records(root, tracked_paths=tuple(entry.path for entry in inventory),
                             generated_roots=generated_roots, max_files=limits.MAX_CI_SOURCE_FILES,
                             max_entries=limits.MAX_CI_SOURCE_ENTRIES,
                             max_total_bytes=limits.MAX_CI_SOURCE_TREE_BYTES,
                             max_file_bytes=limits.MAX_CI_SOURCE_FILE_BYTES,
                             max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES)
    if len(records) != len(inventory):
        raise SourceError("tracked source inventory is incomplete")
    for entry, record in zip(inventory, records):
        if any(record[key] != getattr(entry, key) for key in ("path", "mode", "size", "git_blob")):
            raise SourceError("tracked source differs from the protected tested tree")
    return records


def materialize_source_copy(root: Path, output: Path, *,
                            inventory: tuple[GitSourceEntry, ...]) -> list[dict[str, str | int]]:
    """Atomically publish a new private tracked-source copy bound to protected Git identities.

    Source must be a clean protected checkout, with no other writers. Copy only tracked leaves;
    Git metadata, overlays and caches require their own protected staging lifecycle. Existing
    output is never replaced, and failed staging never publishes a partial copy.
    """

    expected = verify_source_copy(root, inventory=inventory)

    def writer(stage: Path, stage_fd: int) -> list[dict[str, str | int]]:
        copied = copy_source_files(root, stage_fd, tracked_paths=tuple(entry.path for entry in inventory),
                                   max_files=limits.MAX_CI_SOURCE_FILES,
                                   max_entries=limits.MAX_CI_SOURCE_ENTRIES,
                                   max_total_bytes=limits.MAX_CI_SOURCE_TREE_BYTES,
                                   max_file_bytes=limits.MAX_CI_SOURCE_FILE_BYTES,
                                   max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES)
        if copied != expected:
            raise SourceError("source changed after protected inventory admission")
        sealed = verify_source_copy(stage, inventory=inventory)
        if sealed != expected:
            raise SourceError("materialized source differs from the admitted bytes")
        return sealed

    return atomic_directory(output, writer)
