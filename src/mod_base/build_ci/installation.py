"""Private digest-bound kit copy for privileged imports, not pin/release authorization.

The protected caller supplies the independently approved digest and authenticates the executing
pin before this module runs. The copy contains only kit-digest-v1 roots; it installs no tools,
enrolls no interpreter/stdlib and imports no copied Python. Worker/root entry integration is
separate from this filesystem primitive.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from mod_base.build_ci.host import (HostBoundary, _canonical_path, _open_directory,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.inputs import _layout
from mod_base.build_ci.worker import WORKER_ROOT, WorkerError
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import authenticate_tree_private_access, copy_source_files, source_records
from mod_base.model import grammar, limits
from mod_base.model.validators import Int, check
from mod_base.pin import DIGESTED_DIRS, KIT_PATH_NAME
from mod_base.runtime import Invocation


PRIVILEGED_KIT_ROOT = WORKER_ROOT / "privileged-kit"


@dataclass(frozen=True)
class KitInstallation:
    kit_sha: str
    kit_version: str
    digest: str
    files: int
    total_bytes: int
    device: int
    inode: int


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _paths(root: Path, *, remaining_entries: int) -> tuple[tuple[str, ...], int]:
    """Bound discovery before hashing/copying, retaining empty Python source leaves."""
    Int(0, limits.MAX_CI_KIT_INSTALL_ENTRIES)(remaining_entries, "$.kit.remaining_entries")
    descriptor = _open_directory(tuple(_canonical_path(root.as_posix()).parts[1:]))
    paths: list[str] = []
    entries = 0
    try:
        initial = _stamp(os.fstat(descriptor))

        def walk(parent: int, prefix: str, depth: int) -> None:
            nonlocal entries
            if depth > limits.MAX_CI_TOOL_TREE_DEPTH:
                raise WorkerError("privileged kit tree exceeds its depth cap")
            before = _stamp(os.fstat(parent))
            with os.scandir(parent) as listing:
                names = [item.name for item in itertools.islice(listing, remaining_entries - entries + 1)]
            if len(names) > remaining_entries - entries:
                raise WorkerError("privileged kit tree exceeds its entry cap")
            entries += len(names)
            for name in sorted(names):
                path = prefix + name
                if (name == "__pycache__" or name.endswith((".pyc", ".pyo", ".pth"))
                        or not KIT_PATH_NAME.fullmatch(path)):
                    raise WorkerError("privileged kit contains bytecode or a noncanonical path")
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    child = _open_directory((name,), root=parent)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise WorkerError("privileged kit directory changed while opened")
                        walk(child, path + "/", depth + 1)
                    finally:
                        os.close(child)
                elif (stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                      and not info.st_mode & 0o111):
                    if len(paths) >= limits.MAX_CI_KIT_INSTALL_FILES:
                        raise WorkerError("privileged kit tree exceeds its file cap")
                    paths.append(path)
                else:
                    raise WorkerError("privileged kit contains a link, executable or special file")
            if _stamp(os.fstat(parent)) != before:
                raise WorkerError("privileged kit directory changed during discovery")

        walk(descriptor, "", 0)
        if _stamp(os.fstat(descriptor)) != initial:
            raise WorkerError("privileged kit root changed during discovery")
        named = _open_directory(tuple(root.parts[1:]))
        try:
            if _stamp(os.fstat(named)) != initial:
                raise WorkerError("privileged kit root was replaced during discovery")
        finally:
            os.close(named)
        return tuple(sorted(paths)), entries
    finally:
        os.close(descriptor)


def _inventory(root: Path) -> tuple[dict[str, list[dict]], str, int, int]:
    inventories: dict[str, list[dict]] = {}
    files, entries, total = 0, len(DIGESTED_DIRS), 0
    lines: list[tuple[str, str]] = []
    for top in DIGESTED_DIRS:
        paths, count = _paths(root / top, remaining_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES - entries)
        entries += count
        records = source_records(root / top, tracked_paths=paths, generated_roots=(),
                    max_files=limits.MAX_CI_KIT_INSTALL_FILES, max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES,
                    max_total_bytes=limits.MAX_CI_KIT_INSTALL_BYTES, max_file_bytes=limits.MAX_CI_KIT_INSTALL_BYTES,
                    max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES)
        files += len(records)
        total += sum(record["size"] for record in records)
        check(files <= limits.MAX_CI_KIT_INSTALL_FILES and total <= limits.MAX_CI_KIT_INSTALL_BYTES,
              "$.kit", "kit installation exceeds whole-tree bounds")
        check(all(record["mode"] == "100644" for record in records), "$.kit", "kit source modes changed")
        inventories[top] = records
        lines.extend((f"./{top}/{record['path']}", record["sha256"]) for record in records)
    check(files > 0, "$.kit", "kit installation cannot be empty")
    listing = "".join(f"{sha}  {path}\n" for path, sha in sorted(lines)).encode("ascii")
    return inventories, "sha256:" + hashlib.sha256(listing).hexdigest(), files, total


def _shape(root: Path) -> tuple[int, ...]:
    """The installation is exactly the three digested directories, with a stable root name."""
    parts = tuple(_canonical_path(root.as_posix()).parts[1:])
    descriptor = _open_directory(parts)
    try:
        initial = _stamp(os.fstat(descriptor))
        with os.scandir(descriptor) as entries:
            names = [entry.name for entry in itertools.islice(entries, len(DIGESTED_DIRS) + 1)]
        if sorted(names) != sorted(DIGESTED_DIRS):
            raise WorkerError("privileged kit installation has missing or extra root entries")
        if _stamp(os.fstat(descriptor)) != initial:
            raise WorkerError("privileged kit installation changed during admission")
        named = _open_directory(parts)
        try:
            if _stamp(os.fstat(named)) != initial:
                raise WorkerError("privileged kit installation root was replaced")
        finally:
            os.close(named)
        return initial
    finally:
        os.close(descriptor)


def install_privileged_kit(invocation: Invocation, *, boundary: HostBoundary,
                            expected_digest: str) -> KitInstallation:
    """Root-only new private copy; the supplied digest/pin must already be independently admitted."""
    try:
        authenticate_privileged_host_boundary(boundary)
        check(type(invocation) is Invocation, "$.invocation", "requires a protected invocation")
        grammar.require(grammar.DIGEST, expected_digest, "approved kit digest")
        source = invocation.kit_root
        check(isinstance(source, Path), "$.kit_root", "kit source must be an invocation path")
        home = _canonical_path(boundary.home)
        check(home in _canonical_path(source.as_posix()).parents, "$.kit_root", "kit source must be behind the runner home fence")
        kit = invocation.kit
        _layout(boundary)
        inventory, digest, files, total = _inventory(source)
        check(digest == expected_digest, "$.kit.digest", "source kit differs from the approved digest")

        def fill(stage: Path, stage_fd: int) -> KitInstallation:
            for top in DIGESTED_DIRS:
                os.mkdir(top, mode=0o700, dir_fd=stage_fd)
                child = _open_directory((top,), root=stage_fd)
                try:
                    copied = copy_source_files(source / top, child,
                        tracked_paths=tuple(record["path"] for record in inventory[top]),
                        max_files=limits.MAX_CI_KIT_INSTALL_FILES, max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES,
                        max_total_bytes=limits.MAX_CI_KIT_INSTALL_BYTES, max_file_bytes=limits.MAX_CI_KIT_INSTALL_BYTES,
                        max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES)
                    check(copied == inventory[top], "$.kit", "kit source changed before copying")
                finally:
                    os.close(child)
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0,
                                             max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES)
            initial = _shape(stage)
            check(_inventory(stage) == (inventory, digest, files, total), "$.kit", "independent kit copy differs")
            check(_inventory(source) == (inventory, digest, files, total), "$.kit", "source kit changed during installation")
            authenticate_tree_private_access(stage, owner_uid=0, owner_gid=0,
                                             max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES)
            check(_shape(stage) == initial, "$.kit", "private kit root changed during installation")
            authenticate_privileged_host_boundary(boundary)
            _layout(boundary)
            return KitInstallation(kit["sha"], kit["version"], digest, files, total, initial[0], initial[1])

        return atomic_directory(Path(str(PRIVILEGED_KIT_ROOT)), fill)
    except OSError as error:
        raise WorkerError("cannot install private privileged kit") from error


def authenticate_privileged_kit(installation: KitInstallation, *, boundary: HostBoundary) -> None:
    """Recheck the fixed root-owned copy; a constructible receipt cannot authorize a pin or import."""
    try:
        authenticate_privileged_host_boundary(boundary)
        check(type(installation) is KitInstallation, "$.installation", "invalid kit installation receipt")
        grammar.require_sha1(installation.kit_sha, "installed kit SHA")
        grammar.require(grammar.VERSION, installation.kit_version, "installed kit version")
        grammar.require(grammar.DIGEST, installation.digest, "installed kit digest")
        check(type(installation.files) is int and type(installation.total_bytes) is int,
              "$.installation", "kit counts must be exact integers")
        Int(1, limits.MAX_CI_KIT_INSTALL_FILES)(installation.files, "$.installation.files")
        Int(0, limits.MAX_CI_KIT_INSTALL_BYTES)(installation.total_bytes, "$.installation.total_bytes")
        check(type(installation.device) is int and installation.device >= 0
              and type(installation.inode) is int and installation.inode > 0,
              "$.installation", "invalid private kit root identity")
        _layout(boundary)
        root = Path(str(PRIVILEGED_KIT_ROOT))
        initial = _shape(root)
        check(initial[:2] == (installation.device, installation.inode), "$.installation", "private kit root identity changed")
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0,
                                         max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES)
        _, digest, files, total = _inventory(root)
        check((digest, files, total) == (installation.digest, installation.files, installation.total_bytes),
              "$.installation", "privileged kit copy changed after installation")
        authenticate_tree_private_access(root, owner_uid=0, owner_gid=0,
                                         max_entries=limits.MAX_CI_KIT_INSTALL_ENTRIES)
        check(_shape(root) == initial, "$.installation", "private kit root changed during reinspection")
        authenticate_privileged_host_boundary(boundary)
    except OSError as error:
        raise WorkerError("cannot authenticate private privileged kit") from error
