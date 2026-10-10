"""Independent tracked source publication to the fresh candidate UID (MB11)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.gradle_cache import _quiet, _stamp
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.source import GitSourceEntry, materialize_source_copy, verify_source_copy
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.io.tree import privatize_source_copy
from mod_base.model import limits


def stage_privileged_worker_source(root: Path, *, boundary: HostBoundary, account: WorkerAccount,
                                   inventory: tuple[GitSourceEntry, ...]) -> list[dict[str, Any]]:
    """Root-only: exclusively copy the tested tree's tracked files into the candidate repository.

    Caller admits its original code/runtime, the tested-tree inventory, native policy, excluded
    writers and no previous candidate UID execution. The checkout must hold exactly the inventory:
    an undeclared path, a hard link or a file of another type or mode is refused before anything
    is published. Git metadata is omitted; tracked links are copied as data and never followed
    for ownership or privileged execution. This does not authorize imports, native execution,
    validation or upload.
    """
    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account('candidate')
    if type(account) is not WorkerAccount or account != actual or account.uid == boundary.uid:
        raise WorkerError("source staging requires the fresh candidate account")
    source = parent = installed = None
    try:
        _quiet(account)
        if not isinstance(root, Path):
            raise WorkerError("worker source must be a protected checkout path")
        path = _canonical_path(root.as_posix())
        if not path.as_posix().startswith(boundary.home + '/'):
            raise WorkerError("worker source must be below the private runner home")
        source = _open_directory(tuple(path.parts[1:]))
        original = os.fstat(source)
        if original.st_uid not in (0, boundary.uid) or original.st_mode & 0o022:
            raise WorkerError("worker source root has unsafe permissions")
        expected = verify_source_copy(root, inventory=inventory)
        parent = _open_directory(tuple(WORKER_ROOT.parts[1:]))
        parent_info = os.fstat(parent)
        if parent_info.st_uid not in (0, boundary.uid) or parent_info.st_mode & 0o022:
            raise WorkerError("worker source destination parent is not protected")
        destination = Path(str(WORKER_ROOT / 'repository'))
        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account('candidate') != account:
                raise WorkerError("source staging worker identity changed")
            _quiet(account)
            named_source = _open_directory(tuple(path.parts[1:]))
            named_parent = None
            try:
                named_parent = _open_directory(tuple(WORKER_ROOT.parts[1:]))
                info = os.fstat(named_parent)
                if (_stamp(os.fstat(source)) != _stamp(original)
                        or _stamp(os.fstat(named_source)) != _stamp(original)
                        or (info.st_dev, info.st_ino) != (parent_info.st_dev, parent_info.st_ino)
                        or info.st_uid not in (0, boundary.uid) or info.st_mode & 0o022
                        or (os.fstat(parent).st_dev, os.fstat(parent).st_ino) != (info.st_dev, info.st_ino)):
                    raise WorkerError("worker source or destination parent binding changed")
            finally:
                if named_parent is not None:
                    os.close(named_parent)
                os.close(named_source)
        recheck()
        if materialize_source_copy(root, destination, inventory=inventory) != expected:
            raise WorkerError("worker source differs after exclusive publication")
        installed = _open_directory(tuple(destination.parts[1:]))
        copied = os.fstat(installed)
        if ((copied.st_uid, copied.st_gid, stat.S_IMODE(copied.st_mode)) != (0, 0, 0o700)
                or (copied.st_dev, copied.st_ino) == (original.st_dev, original.st_ino)):
            raise WorkerError("worker source must be a new protected Root copy")
        recheck()
        if privatize_source_copy(destination, tracked_paths=tuple(entry.path for entry in inventory),
                source_owner_uid=0, owner_uid=account.uid, owner_gid=account.gid,
                max_files=limits.MAX_CI_SOURCE_FILES, max_entries=limits.MAX_CI_SOURCE_ENTRIES,
                max_total_bytes=limits.MAX_CI_SOURCE_TREE_BYTES, max_file_bytes=limits.MAX_CI_SOURCE_FILE_BYTES,
                max_link_bytes=limits.MAX_CI_SOURCE_LINK_BYTES) != expected:
            raise WorkerError("worker source private handoff changed content")
        if (verify_source_copy(destination, inventory=inventory) != expected
                or verify_source_copy(root, inventory=inventory) != expected):
            raise WorkerError("worker source bytes changed after private handoff")
        recheck()
        named = _open_directory(tuple(destination.parts[1:]))
        try:
            info = os.fstat(installed)
            if (_stamp(os.fstat(named)) != _stamp(info)
                    or (info.st_dev, info.st_ino) != (copied.st_dev, copied.st_ino)
                    or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (account.uid, account.gid, 0o700)):
                raise WorkerError("worker source private publication identity differs")
        finally:
            os.close(named)
        return expected
    except (OSError, UnicodeError, ValueError) as error:
        terminate_worker(account)
        raise WorkerError("cannot stage independent worker source") from error
    except BaseException:
        terminate_worker(account)
        raise
    finally:
        for descriptor in (installed, parent, source):
            if descriptor is not None:
                os.close(descriptor)
