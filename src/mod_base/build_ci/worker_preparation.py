"""Protected preparation of candidate-only source/Git/cache/overlay data (MB11).

Root runs this for the ``stage-candidate`` operation and never calls the GitHub API. The tested
commit, tree and inventory arrive in the runner's private request; the inventory is bound to the
tree by recomputing the tree's Git object name, and the checkout is bound to the inventory byte by
byte before anything is published.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.gradle_cache import _BOUNDS as CACHE_BOUNDS, _quiet, _stamp, stage_privileged_gradle_cache
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.source import GitSourceEntry, validate_source_inventory, verify_source_copy
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.build_ci.worker_git import _BOUNDS as GIT_BOUNDS, _admit as _admit_git, stage_privileged_worker_git
from mod_base.build_ci.worker_overlay import _admit as _admit_overlay, stage_privileged_worker_overlay
from mod_base.build_ci.worker_source import stage_privileged_worker_source
from mod_base.io.tree import authenticate_tree_private_access, regular_data_records
from mod_base.model import grammar
from mod_base.pin import OVERLAY_PATH, Pin


def _tree_id(inventory: tuple[GitSourceEntry, ...]) -> str:
    """The Git tree object name (SHA-1 format) of the tree holding exactly these blobs and links.

    A tree lists its entries by name, a directory sorting as ``name/``, and names every blob and
    subtree by object name. A complete inventory therefore determines the root tree, and no other
    inventory has the same name: root needs no object store and no API to bind one to the other.
    """
    validate_source_inventory(inventory)
    root: dict[str, Any] = {}
    for entry in inventory:
        *parents, leaf = entry.path.split('/')
        node = root
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = entry

    def write(node: dict[str, Any]) -> bytes:
        rows = []
        for name, child in node.items():
            raw = name.encode('utf-8')
            if type(child) is dict:
                rows.append((raw + b'/', b'40000 ' + raw + b'\0' + write(child)))
            else:
                rows.append((raw, child.mode.encode('ascii') + b' ' + raw + b'\0' + bytes.fromhex(child.git_blob)))
        body = b''.join(row for _, row in sorted(rows))
        return hashlib.sha1(b'tree ' + str(len(body)).encode('ascii') + b'\0' + body).digest()  # Git object identity

    return write(root).hex()


def _collides(inventory: tuple[GitSourceEntry, ...]) -> bool:
    """Whether a tracked path takes ``out`` itself or the kit overlay slot, or differs from either
    only in case: the checkout could then never be verified with the overlay beside it."""
    top = OVERLAY_PATH.split('/')[0]
    slot = OVERLAY_PATH.casefold() + '/'
    for entry in inventory:
        folded = entry.path.casefold()
        if (folded == top or (folded + '/').startswith(slot)
                or (folded.startswith(top + '/') and not entry.path.startswith(top + '/'))):
            return True
    return False


def prepare_privileged_worker_checkout(source: Path, gradle_seed: Path | None, overlay: Path, *,
                                       boundary: HostBoundary, account: WorkerAccount, repository: str,
                                       tested_commit: str, tested_tree: str,
                                       inventory: tuple[GitSourceEntry, ...], pin: Pin,
                                       expected_digest: str) -> dict[str, list[dict[str, Any]]]:
    """Root-only: stage the candidate's source, Git data, cache and overlay before it ever runs.

    The runner-side caller authenticates the tested commit and tree, that the pin is a released
    kit commit, the candidate upgrade and a secret-free cache restoration; no earlier candidate
    execution or other writer is permitted. Every original is admitted before the first effect:
    the inventory must hash to ``tested_tree``, the checkout must hold exactly the inventory, its
    HEAD must be detached at ``tested_commit`` and the overlay must match its pin, stamp and
    digest. The seed is optional and admitted when its own phase starts. The fixed candidate
    repository must not exist and the allocated cache must be empty: a populated root is never
    reused. Returned copy observations confer no execution authority; any failure terminates and
    locks the candidate.
    """
    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account('candidate')
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
        grammar.require(grammar.REPOSITORY, repository, 'repository')
        grammar.require_sha1(tested_commit, 'tested commit')
        grammar.require_sha1(tested_tree, 'tested tree')
        roots = (source, overlay) if gradle_seed is None else (source, overlay, gradle_seed)
        if any(not isinstance(value, Path) for value in roots):
            raise WorkerError('checkout preparation requires protected source data paths')
        canonical = tuple(_canonical_path(value.as_posix()) for value in roots)
        if any(not path.as_posix().startswith(boundary.home + '/') for path in canonical):
            raise WorkerError('checkout preparation sources must be below private runner home')
        for position, path in enumerate(canonical):
            for other in canonical[position + 1:]:
                if path == other or path in other.parents or other in path.parents:
                    raise WorkerError('checkout preparation source/cache/overlay roots overlap')
        checkout = Path(str(WORKER_ROOT / 'repository'))
        try:
            os.lstat(checkout)
        except FileNotFoundError:
            pass
        else:
            raise WorkerError('candidate repository already exists; refusing to reuse a populated root')
        git_source = source / '.git'
        for value in (source, git_source, *roots[1:]):
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
        cache = Path(str(WORKER_ROOT / 'candidate-home' / 'gradle-home'))
        git = checkout / '.git'
        copied_overlay = checkout / OVERLAY_PATH
        retain(cache, private=True)  # Bind the original allocated cache before any source effect.
        with os.scandir(retained[0][1]) as entries:
            if next(entries, None) is not None:  # Bytes in it mean the candidate ran or was staged before.
                raise WorkerError('allocated Gradle cache is not empty; refusing reuse')
        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account('candidate') != account:
                raise WorkerError('checkout preparation candidate identity changed')
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
        if _tree_id(inventory) != tested_tree:
            raise WorkerError('source inventory is not the inventory of the tested tree')
        if _collides(inventory):
            raise WorkerError('tracked source collides with the reserved kit overlay path')
        original_source = verify_source_copy(source, inventory=inventory)
        _admit_git(git_source, boundary, tested_commit)
        original_overlay = _admit_overlay(overlay, pin, expected_digest)
        recheck()
        copied_source = stage_privileged_worker_source(source, boundary=boundary, account=account, inventory=inventory)
        if copied_source != original_source:
            raise WorkerError('prepared source differs from independently admitted original')
        retain(checkout, private=True)
        recheck()
        copied_git = stage_privileged_worker_git(git_source, boundary=boundary, account=account,
                                                 repository=repository, tested_commit=tested_commit)
        retain(git, private=True)
        recheck()
        copied_cache = ([] if gradle_seed is None else
                        stage_privileged_gradle_cache(gradle_seed, boundary=boundary, account=account))
        recheck()
        overlay_records = stage_privileged_worker_overlay(overlay, boundary=boundary, account=account,
                                                         pin=pin, expected_digest=expected_digest)
        if overlay_records != original_overlay:
            raise WorkerError('prepared overlay differs from independently admitted original')
        retain(checkout / 'out', private=True)
        retain(copied_overlay, private=False)
        recheck()
        if (verify_source_copy(checkout, inventory=inventory, generated_roots=(OVERLAY_PATH,)) != copied_source
                or verify_source_copy(source, inventory=inventory) != original_source
                or regular_data_records(git, **GIT_BOUNDS) != copied_git
                or regular_data_records(cache, **CACHE_BOUNDS) != copied_cache
                or (gradle_seed is not None and regular_data_records(gradle_seed, **CACHE_BOUNDS) != copied_cache)
                or _admit_overlay(copied_overlay, pin, expected_digest) != original_overlay
                or _admit_overlay(overlay, pin, expected_digest) != original_overlay):
            raise WorkerError('checkout preparation data changed across staging phases')
        for value, bounds in ((git, GIT_BOUNDS), (cache, CACHE_BOUNDS)):
            authenticate_tree_private_access(value, owner_uid=account.uid, owner_gid=account.gid,
                                               max_entries=bounds['max_entries'])
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
