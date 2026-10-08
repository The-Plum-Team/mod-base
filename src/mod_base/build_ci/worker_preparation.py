"""Protected preparation of candidate-only source/Git/cache/overlay data (MB11)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import authenticate_source_identity
from mod_base.build_ci.gradle_cache import _BOUNDS as CACHE_BOUNDS, _quiet, _stamp, stage_privileged_gradle_cache
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.source import authenticate_source_inventory, verify_source_copy
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.build_ci.worker_git import _BOUNDS as GIT_BOUNDS, stage_privileged_worker_git
from mod_base.build_ci.worker_overlay import _admit, stage_privileged_worker_overlay
from mod_base.build_ci.worker_source import stage_privileged_worker_source
from mod_base.github.api import GitHubApi
from mod_base.io.tree import authenticate_tree_private_access, regular_data_records
from mod_base.model import grammar
from mod_base.pin import OVERLAY_PATH, Pin, verify_released


def prepare_privileged_worker_checkout(api: GitHubApi, source: Path, gradle_seed: Path, overlay: Path, *,
                                       boundary: HostBoundary, account: WorkerAccount,
                                       identity: dict[str, Any], pin: Pin,
                                       expected_digest: str) -> dict[str, list[dict[str, Any]]]:
    """Compose exclusive source, Git, cache and overlay staging before any candidate starts.

    Original caller/runtime, protected original Git checkout/index/graph, native policy/request,
    candidate upgrade and secret-free cache restoration are independently admitted by the caller.
    No earlier UID execution or other writers are permitted. API inventory/source rechecks do
    not replace those prerequisites. Returned copy observations confer no execution authority.
    """
    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account('worker')
    if type(account) is not WorkerAccount or account != actual or account.uid == boundary.uid:
        raise WorkerError('checkout preparation requires the fresh candidate account')
    originals: list[tuple[Path, int, os.stat_result]] = []
    retained: list[tuple[Path, int, tuple[int, int], bool]] = []
    try:
        _quiet(account)
        if type(pin) is not Pin or type(pin.version) is not str or not pin.version.startswith('v'):
            raise WorkerError('checkout preparation requires an independently bound kit pin')
        grammar.require_sha1(pin.sha)
        grammar.require(grammar.VERSION, pin.version[1:], 'kit version')
        grammar.require(grammar.DIGEST, expected_digest, 'kit digest')
        if any(not isinstance(value, Path) for value in (source, gradle_seed, overlay)):
            raise WorkerError('checkout preparation requires protected source data paths')
        canonical = tuple(_canonical_path(value.as_posix()) for value in (source, gradle_seed, overlay))
        if any(not path.as_posix().startswith(boundary.home + '/') for path in canonical):
            raise WorkerError('checkout preparation sources must be below private runner home')
        for position, path in enumerate(canonical):
            for other in canonical[position + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    raise WorkerError('checkout preparation source/cache/overlay roots overlap')
        git_source = source / '.git'
        for value in (source, git_source, gradle_seed, overlay):
            path = _canonical_path(value.as_posix())
            descriptor = _open_directory(tuple(path.parts[1:]))
            info = os.fstat(descriptor)
            originals.append((value, descriptor, info))
            if info.st_uid not in (0, boundary.uid) or info.st_mode & 0o022:
                raise WorkerError('checkout preparation source root is not protected')
        def retain(value: Path, *, private: bool) -> None:
            descriptor = _open_directory(tuple(value.parts[1:]))
            info = os.fstat(descriptor)
            retained.append((value, descriptor, (info.st_dev, info.st_ino), private))
            if ((info.st_uid, info.st_gid) != (account.uid, account.gid)
                    or stat.S_IMODE(info.st_mode) != (0o700 if private else 0o755)):
                raise WorkerError('checkout preparation destination ownership or mode differs')
        checkout = Path(str(WORKER_ROOT / 'repository'))
        cache = Path(str(WORKER_ROOT / 'worker-home' / 'gradle-home'))
        git = checkout / '.git'
        copied_overlay = checkout / OVERLAY_PATH
        retain(cache, private=True)  # Bind the original allocated cache before any source effect.
        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account('worker') != account:
                raise WorkerError('checkout preparation worker identity changed')
            _quiet(account)
            for value, descriptor, original in originals:
                path = _canonical_path(value.as_posix())
                named = _open_directory(tuple(path.parts[1:]))
                try:
                    if _stamp(os.fstat(named)) != _stamp(original) or _stamp(os.fstat(descriptor)) != _stamp(original):
                        raise WorkerError('checkout preparation original source binding changed')
                finally:
                    os.close(named)
            for value, descriptor, original_id, private in retained:
                named = _open_directory(tuple(value.parts[1:]))
                try:
                    info = os.fstat(descriptor)
                    if (_stamp(os.fstat(named)) != _stamp(info) or (info.st_dev, info.st_ino) != original_id
                            or (info.st_uid, info.st_gid) != (account.uid, account.gid)
                            or stat.S_IMODE(info.st_mode) != (0o700 if private else 0o755)):
                        raise WorkerError('checkout preparation copied root binding changed')
                finally:
                    os.close(named)
        recheck()
        inventory = authenticate_source_inventory(api, identity=identity)
        if any(entry.path == 'out' or entry.path == OVERLAY_PATH or entry.path.startswith(OVERLAY_PATH + '/')
               for entry in inventory):
            raise WorkerError('tracked source collides with the reserved kit overlay path')
        original_source = verify_source_copy(source, inventory=inventory)
        original_overlay = _admit(overlay, pin, expected_digest)
        verify_released(pin, api)
        recheck()
        copied_source = stage_privileged_worker_source(source, boundary=boundary, account=account, inventory=inventory)
        if copied_source != original_source:
            raise WorkerError('prepared source differs from independently admitted original')
        retain(checkout, private=True)
        recheck()
        copied_git = stage_privileged_worker_git(git_source, boundary=boundary, account=account,
            repository=identity['repository'], tested_commit=identity['tested_sha'])
        retain(git, private=True)
        recheck()
        copied_cache = stage_privileged_gradle_cache(gradle_seed, boundary=boundary, account=account)
        recheck()
        overlay_records = stage_privileged_worker_overlay(api, overlay, boundary=boundary, account=account,
                                                         pin=pin, expected_digest=expected_digest)
        if overlay_records != original_overlay:
            raise WorkerError('prepared overlay differs from independently admitted original')
        retain(copied_overlay, private=False)
        recheck()
        if (verify_source_copy(checkout, inventory=inventory, generated_roots=(OVERLAY_PATH,)) != copied_source
                or verify_source_copy(source, inventory=inventory) != original_source
                or regular_data_records(git, **GIT_BOUNDS) != copied_git
                or regular_data_records(cache, **CACHE_BOUNDS) != copied_cache
                or regular_data_records(gradle_seed, **CACHE_BOUNDS) != copied_cache
                or _admit(copied_overlay, pin, expected_digest) != original_overlay
                or _admit(overlay, pin, expected_digest) != original_overlay):
            raise WorkerError('checkout preparation data changed across staging phases')
        for value, bounds in ((git, GIT_BOUNDS), (cache, CACHE_BOUNDS)):
            authenticate_tree_private_access(value, owner_uid=account.uid, owner_gid=account.gid,
                                               max_entries=bounds['max_entries'])
        authenticate_source_identity(api, identity)
        verify_released(pin, api)
        recheck()
        return {'source': copied_source, 'git': copied_git, 'gradle': copied_cache, 'overlay': overlay_records}
    except (OSError, UnicodeError, ValueError) as error:
        terminate_worker(account)
        raise WorkerError('cannot prepare protected candidate checkout') from error
    except BaseException:
        terminate_worker(account)
        raise
    finally:
        for _, descriptor, *_ in (*retained, *originals):
            os.close(descriptor)
