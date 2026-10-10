"""Private Gradle seed handoff before a fresh candidate UID starts (MB11).

Protected restore policy must supply a secret-free seed and exclude concurrent writers. This
copies untrusted candidate cache data, never installer approval or privileged executable code.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, _control, authenticate_worker_account, terminate_worker
from mod_base.io.tree import (SEED_PATHS, authenticate_tree_private_access, copy_regular_data_files, regular_data_records,
                              privatize_regular_data_copy, validate_tree_entries)
from mod_base.model import limits


#: Caps and path rule of every seed and cache inventory. A real cache names its entries freely
#: (``1.20.1+build.10``) and nests them deeply, so only its structure is checked: no link, no
#: special file, no traversal, within these counts and sizes.
_BOUNDS = {"max_files": limits.MAX_CI_SOURCE_FILES, "max_entries": limits.MAX_CI_SOURCE_ENTRIES,
           "max_total_bytes": limits.MAX_CI_SOURCE_TREE_BYTES, "max_file_bytes": limits.MAX_CI_SOURCE_FILE_BYTES,
           "rule": SEED_PATHS}


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _quiet(account: WorkerAccount) -> None:
    if _control(("/usr/bin/pgrep", "-u", str(account.uid)), timeout=limits.CI_TERMINATION_GRACE_SECONDS,
                accepted=frozenset({1})) != b"":
        raise WorkerError("candidate staging requires a quiescent fresh candidate UID")


def _records(seed: Path) -> list[dict[str, Any]]:
    validate_tree_entries(seed, max_entries=_BOUNDS["max_entries"])
    return regular_data_records(seed, **_BOUNDS)


def _seed_roots(seed: int) -> None:
    """A seed root holds the conventional ``caches`` and ``wrapper`` directories and nothing else.

    ``gradle.properties``, ``init.d`` and every other configuration or credential a Gradle user
    home may carry are refused by name, before any byte below the root is read.
    """
    with os.scandir(seed) as entries:
        for entry in entries:
            if entry.name not in {"caches", "wrapper"}:
                raise WorkerError("Gradle seed includes a configuration/credential or unexpected root")
            if not stat.S_ISDIR(entry.stat(follow_symlinks=False).st_mode):
                raise WorkerError("Gradle seed root child is not a real directory")


def stage_privileged_gradle_cache(seed: Path, *, boundary: HostBoundary,
                                 account: WorkerAccount) -> list[dict[str, Any]]:
    """Root-only: populate the candidate's empty allocated cache from a seed, then hand it over.

    Only the conventional caches/wrapper seed directories are copied; configuration/credential
    paths at the seed root reject. This shape check cannot certify absence of secrets in arbitrary
    data: protected restoration policy owns that prerequisite. Any admitted staging failure
    terminates/locks the candidate and returns no execution authority. Partial bytes can remain
    inaccessible in the fixed private cache.
    """

    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account("candidate")
    if type(account) is not WorkerAccount or account != actual or account.uid == boundary.uid:
        raise WorkerError("Gradle cache handoff requires the fresh candidate account")
    source = destination = None
    try:
        _quiet(account)
        if not isinstance(seed, Path):
            raise WorkerError("Gradle seed path must be a protected data path")
        path = _canonical_path(seed.as_posix())
        if not path.as_posix().startswith(boundary.home + "/"):
            raise WorkerError("Gradle seed must be under the protected runner home")
        source = _open_directory(tuple(path.parts[1:]))
        original = os.fstat(source)
        if original.st_uid not in (0, boundary.uid) or original.st_mode & 0o022:
            raise WorkerError("Gradle seed root has unsafe ownership or permissions")
        _seed_roots(source)
        expected = _records(seed)
        target = Path(str(WORKER_ROOT / "candidate-home" / "gradle-home"))
        destination = _open_directory(tuple(target.parts[1:]))
        allocated = os.fstat(destination)
        if (not stat.S_ISDIR(allocated.st_mode) or (allocated.st_uid, allocated.st_gid) != (account.uid, account.gid)
                or stat.S_IMODE(allocated.st_mode) != 0o700):
            raise WorkerError("allocated Gradle cache metadata differs")
        with os.scandir(destination) as entries:
            if next(entries, None) is not None:
                raise WorkerError("allocated Gradle cache is not empty; refusing reuse")
        _quiet(account)
        os.fchown(destination, 0, 0)
        os.fchmod(destination, 0o700)

        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account("candidate") != account:
                raise WorkerError("Gradle cache worker identity changed")
            _quiet(account)
            current = _open_directory(tuple(path.parts[1:]))
            named = None
            try:
                named = _open_directory(tuple(target.parts[1:]))
                if (_stamp(os.fstat(current)) != _stamp(original) or _stamp(os.fstat(source)) != _stamp(original)
                        or (os.fstat(named).st_dev, os.fstat(named).st_ino) != (allocated.st_dev, allocated.st_ino)
                        or _stamp(os.fstat(named)) != _stamp(os.fstat(destination))):
                    raise WorkerError("Gradle seed/cache directory binding changed")
            finally:
                if named is not None:
                    os.close(named)
                os.close(current)

        recheck()
        copied = copy_regular_data_files(seed, destination, **_BOUNDS)
        if copied != expected or _records(seed) != expected:
            raise WorkerError("Gradle seed bytes changed during private copying")
        recheck()
        if privatize_regular_data_copy(target, source_owner_uid=0, owner_uid=account.uid, owner_gid=account.gid,
                               **_BOUNDS) != expected:
            raise WorkerError("private Gradle copy differs after ownership handoff")
        authenticate_tree_private_access(target, owner_uid=account.uid, owner_gid=account.gid,
                                          max_entries=_BOUNDS["max_entries"])
        if _records(target) != expected or _records(seed) != expected:
            raise WorkerError("Gradle cache/source changed after ownership handoff")
        recheck()
        return expected
    except (OSError, UnicodeError, ValueError) as error:
        terminate_worker(account)
        raise WorkerError("cannot stage private Gradle cache") from error
    except BaseException:
        terminate_worker(account)
        raise
    finally:
        for descriptor in (destination, source):
            if descriptor is not None:
                os.close(descriptor)
