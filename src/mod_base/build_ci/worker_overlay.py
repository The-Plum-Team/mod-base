"""Exclusive candidate-only kit overlay copying by the old protected implementation (MB11)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.gradle_cache import _quiet
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.installation import _paths, _stamp
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.github.api import GitHubApi
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import copy_regular_data_files, grant_regular_data_read_access, regular_data_records, validate_tree_entries
from mod_base.model import grammar, limits
from mod_base.pin import (DIGESTED_DIRS, OVERLAY_PATH, STAMP_NAME, Pin, kit_tree_digest, read_stamp,
                          stamp_document, verify_released, verify_staged_files)


_BOUNDS = {"max_files": limits.MAX_CI_KIT_INSTALL_FILES, "max_entries": limits.MAX_CI_KIT_INSTALL_ENTRIES,
           "max_total_bytes": limits.MAX_CI_KIT_INSTALL_BYTES, "max_file_bytes": limits.MAX_CI_KIT_INSTALL_BYTES}


def _admit(root: Path, pin: Pin, digest: str) -> list[dict[str, Any]]:
    paths, _ = _paths(root, remaining_entries=_BOUNDS["max_entries"])
    if len(paths) > _BOUNDS["max_files"] or any(path.endswith(".pyd") for path in paths):
        raise WorkerError("worker kit overlay contains excessive files or bytecode")
    descriptor = _open_directory(tuple(root.parts[1:]))
    try:
        allowed = {*DIGESTED_DIRS, "template", "tools", "actions", STAMP_NAME}
        tops = set()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                if entry.name not in allowed or entry.name in tops:
                    raise WorkerError("worker kit overlay has an unexpected root")
                tops.add(entry.name)
        if not set(DIGESTED_DIRS) <= tops or STAMP_NAME not in tops:
            raise WorkerError("worker kit overlay is missing required roots")
    finally:
        os.close(descriptor)
    validate_tree_entries(root, max_entries=_BOUNDS["max_entries"])
    records = regular_data_records(root, **_BOUNDS)
    if read_stamp(root) != stamp_document(pin, digest) or kit_tree_digest(root) != digest:
        raise WorkerError("worker kit overlay differs from the independently bound pin/digest")
    verify_staged_files(root)
    return records


def _plain(root: int, account: WorkerAccount) -> None:
    """Keep bootstrap's plain 0644/0755 contract after ACL-removing private copy handoff."""
    remaining = [_BOUNDS["max_entries"]]
    def walk(directory: int, depth: int = 0) -> None:
        if depth > limits.MAX_CI_TOOL_TREE_DEPTH:
            raise WorkerError("overlay permission walk exceeds its depth cap")
        remaining[0] -= 1
        if remaining[0] < 0:
            raise WorkerError("overlay permission walk exceeds its cap")
        initial = os.fstat(directory)
        if (initial.st_uid, initial.st_gid) != (account.uid, account.gid):
            raise WorkerError("overlay directory ownership changed")
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if (info.st_uid, info.st_gid) != (account.uid, account.gid):
                    raise WorkerError("overlay entry ownership changed")
                if stat.S_ISDIR(info.st_mode):
                    child = _open_directory((entry.name,), root=directory)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise WorkerError("overlay directory changed while opened")
                        walk(child, depth + 1)
                        if _stamp(os.stat(entry.name, dir_fd=directory, follow_symlinks=False)) != _stamp(os.fstat(child)):
                            raise WorkerError("overlay directory name changed during normalization")
                    finally:
                        os.close(child)
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    remaining[0] -= 1
                    if remaining[0] < 0:
                        raise WorkerError("overlay permission walk exceeds its cap")
                    child = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise WorkerError("overlay file changed while opened")
                        os.fchmod(child, 0o644)
                        os.fsync(child)
                        if _stamp(os.stat(entry.name, dir_fd=directory, follow_symlinks=False)) != _stamp(os.fstat(child)):
                            raise WorkerError("overlay file name changed during normalization")
                    finally:
                        os.close(child)
                else:
                    raise WorkerError("overlay contains a link or special file")
        os.fchmod(directory, 0o755)
        os.fsync(directory)
        final = os.fstat(directory)
        if (final.st_dev, final.st_ino, final.st_uid, final.st_gid) != (
                initial.st_dev, initial.st_ino, account.uid, account.gid):
            raise WorkerError("overlay directory identity changed during normalization")
    walk(root)


def stage_privileged_worker_overlay(api: GitHubApi, overlay: Path, *, boundary: HostBoundary,
                                    account: WorkerAccount, pin: Pin, expected_digest: str) -> list[dict[str, Any]]:
    """Copy the old bootstrap's authenticated overlay to a fresh candidate's fixed kit path.

    The caller authenticates its own executing code/runtime, candidate source and upgrade route,
    and supplies the digest from an authenticated checkout/bootstrap, never first observed bytes.
    Released tag/ancestry is independently rechecked; future candidate code is never imported.
    This confers no protected-code, native or App authority on the copied future pin.
    """
    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account("worker")
    if type(account) is not WorkerAccount or account != actual or account.uid == boundary.uid:
        raise WorkerError("worker overlay requires the fresh candidate account")
    source = parent = output_parent = None
    try:
        if type(pin) is not Pin or type(pin.version) is not str or not pin.version.startswith("v"):
            raise WorkerError("worker overlay requires a canonical protected pin")
        grammar.require_sha1(pin.sha)
        grammar.require(grammar.VERSION, pin.version[1:], "kit version")
        grammar.require(grammar.DIGEST, expected_digest, "kit digest")
        _quiet(account)
        if not isinstance(overlay, Path):
            raise WorkerError("worker overlay source path is invalid")
        path = _canonical_path(overlay.as_posix())
        if not path.as_posix().startswith(boundary.home + "/"):
            raise WorkerError("worker overlay source is outside the private runner home")
        source = _open_directory(tuple(path.parts[1:]))
        original = os.fstat(source)
        if original.st_uid not in (0, boundary.uid) or original.st_mode & 0o022:
            raise WorkerError("worker overlay source root has unsafe permissions")
        verify_released(pin, api)
        expected = _admit(overlay, pin, expected_digest)
        repository = WORKER_ROOT / "repository"
        parent = _open_directory(tuple(repository.parts[1:]))
        repository_info = os.fstat(parent)
        if repository_info.st_uid not in (0, boundary.uid, account.uid) or repository_info.st_mode & 0o022:
            raise WorkerError("candidate overlay parent is not protected")
        try:
            os.mkdir("out", 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        output_parent = _open_directory(("out",), root=parent)
        output_info = os.fstat(output_parent)
        destination = Path(str(repository / OVERLAY_PATH))

        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account("worker") != account:
                raise WorkerError("overlay worker identity changed")
            _quiet(account)
            current = _open_directory(tuple(path.parts[1:]))
            repo = None
            out = None
            try:
                repo = _open_directory(tuple(repository.parts[1:]))
                out = _open_directory(("out",), root=repo)
                info = os.fstat(repo)
                out_info = os.fstat(out)
                if (_stamp(os.fstat(current)) != _stamp(original) or _stamp(os.fstat(source)) != _stamp(original)
                        or (info.st_dev, info.st_ino) != (repository_info.st_dev, repository_info.st_ino)
                        or info.st_uid not in (0, boundary.uid, account.uid) or info.st_mode & 0o022
                        or (out_info.st_dev, out_info.st_ino) != (output_info.st_dev, output_info.st_ino)
                        or out_info.st_uid not in (0, boundary.uid, account.uid) or out_info.st_mode & 0o022):
                    raise WorkerError("overlay source or output parent changed")
            finally:
                for descriptor in (out, repo, current):
                    if descriptor is not None:
                        os.close(descriptor)

        def fill(stage: Path, stage_fd: int) -> tuple[int, int]:
            initial = os.fstat(stage_fd)
            os.fchown(stage_fd, 0, 0)
            os.fchmod(stage_fd, 0o700)
            recheck()
            if copy_regular_data_files(overlay, stage_fd, **_BOUNDS) != expected or _admit(overlay, pin, expected_digest) != expected:
                raise WorkerError("worker overlay changed during independent copying")
            for top in DIGESTED_DIRS:
                try:
                    os.mkdir(top, 0o700, dir_fd=stage_fd)
                except FileExistsError:
                    pass
            if grant_regular_data_read_access(stage, source_owner_uid=0, owner_uid=account.uid,
                                              reader_gid=account.gid, **_BOUNDS) != expected:
                raise WorkerError("worker overlay private ownership handoff changed bytes")
            _quiet(account)
            _plain(stage_fd, account)
            if _admit(stage, pin, expected_digest) != expected:
                raise WorkerError("copied worker overlay differs")
            verify_released(pin, api)
            recheck()
            return initial.st_dev, initial.st_ino

        recheck()
        identity = atomic_directory(destination, fill)
        recheck()
        installed = _open_directory(tuple(destination.parts[1:]))
        try:
            info = os.fstat(installed)
            if (info.st_dev, info.st_ino, info.st_uid, info.st_gid) != (*identity, account.uid, account.gid):
                raise WorkerError("published worker overlay identity differs")
            if _admit(destination, pin, expected_digest) != expected or _admit(overlay, pin, expected_digest) != expected:
                raise WorkerError("published worker overlay bytes differ")
            verify_released(pin, api)
            recheck()
            named = _open_directory(tuple(destination.parts[1:]))
            try:
                if _stamp(os.fstat(named)) != _stamp(os.fstat(installed)):
                    raise WorkerError("published worker overlay name was replaced")
            finally:
                os.close(named)
        finally:
            os.close(installed)
        return expected
    except (OSError, UnicodeError, ValueError) as error:
        terminate_worker(account)
        raise WorkerError("cannot stage protected worker kit overlay") from error
    except BaseException:
        terminate_worker(account)
        raise
    finally:
        for descriptor in (output_parent, parent, source):
            if descriptor is not None:
                os.close(descriptor)
