"""Candidate-only Git metadata curation from a protected self-contained SHA-1 checkout (MB11)."""

from __future__ import annotations

import hashlib
import itertools
import os
import re
import stat
from pathlib import Path
from typing import Any

from mod_base.build_ci.gradle_cache import _quiet, _stamp
from mod_base.build_ci.host import HostBoundary, _canonical_path, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.worker import WORKER_ROOT, WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import (authenticate_tree_private_access, copy_selected_regular_data_files,
                              privatize_regular_data_copy, read_child_file, regular_data_records,
                              selected_regular_data_records)
from mod_base.model import grammar, limits


_BOUNDS = dict(max_files=limits.MAX_CI_GIT_METADATA_FILES, max_entries=limits.MAX_CI_GIT_METADATA_ENTRIES,
               max_file_bytes=limits.MAX_CI_GIT_METADATA_FILE_BYTES, max_total_bytes=limits.MAX_CI_GIT_METADATA_TREE_BYTES)
_IGNORED = {'HEAD', 'config', 'description', 'FETCH_HEAD', 'ORIG_HEAD', 'logs', 'hooks', 'info', 'branches'}
_PACK = re.compile(r'^objects/pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev|bitmap|keep|mtimes)$')
_LOOSE = re.compile(r'^objects/[0-9a-f]{2}/[0-9a-f]{38}$')


def _ref(value: str) -> bool:
    return (grammar.is_repo_path(value) and value.startswith(('refs/heads/', 'refs/tags/', 'refs/remotes/', 'refs/pull/'))
            and '..' not in value and not any(part.startswith('.') or part.endswith(('.lock', '.')) for part in value.split('/')))


def _selection(root: Path, boundary: HostBoundary) -> tuple[str, ...]:
    """Inspect complete bounded metadata, but never read unselected config/hooks/log bytes."""
    descriptor = _open_directory(tuple(root.parts[1:]))
    paths: list[str] = []
    seen: set[str] = set()
    budget = [limits.MAX_CI_GIT_METADATA_ENTRIES - 1]
    try:
        def walk(parent: int, prefix: str, depth: int) -> None:
            if depth > limits.MAX_CI_TOOL_TREE_DEPTH:
                raise WorkerError('Git metadata exceeds its depth cap')
            with os.scandir(parent) as listing:
                names = [entry.name for entry in itertools.islice(listing, budget[0] + 1)]
            if len(names) > budget[0]:
                raise WorkerError('Git metadata exceeds its entry cap')
            budget[0] -= len(names)
            initial = os.fstat(parent)
            for name in sorted(names):
                relative = prefix + name
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
                folded = relative.casefold()
                if (not grammar.is_repo_path(relative) or folded in seen
                        or info.st_uid not in (0, boundary.uid) or info.st_mode & 0o022
                        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                    raise WorkerError('Git metadata has unsafe paths, types, ownership or links')
                seen.add(folded)
                top = relative.split('/')[0]
                if '/' not in relative and top in _IGNORED:
                    if stat.S_ISDIR(info.st_mode) != (top in {'logs', 'hooks', 'info', 'branches'}):
                        raise WorkerError('Git ignored root entry has the wrong type')
                selected = (relative in {'index', 'packed-refs', 'shallow'} or bool(_LOOSE.fullmatch(relative))
                            or bool(_PACK.fullmatch(relative)) or _ref(relative))
                ignored = top in _IGNORED or relative.startswith('objects/info/')
                directory = (relative in {'objects', 'objects/pack', 'objects/info', 'refs'}
                             or re.fullmatch(r'objects/[0-9a-f]{2}', relative) is not None
                             or relative.startswith('refs/') or ignored)
                if relative in {'objects/info/alternates', 'objects/info/http-alternates', 'info/grafts'}:
                    raise WorkerError('Git metadata borrows external objects or grafts')
                if stat.S_ISDIR(info.st_mode):
                    if not directory:
                        raise WorkerError('Git metadata has an unsupported directory')
                    child = _open_directory((name,), root=parent)
                    try:
                        if _stamp(os.fstat(child)) != _stamp(info):
                            raise WorkerError('Git metadata directory changed while opened')
                        walk(child, relative + '/', depth + 1)
                    finally:
                        os.close(child)
                elif selected:
                    if len(paths) >= limits.MAX_CI_GIT_METADATA_FILES - 2:
                        raise WorkerError('Git metadata exceeds its selected file cap')
                    paths.append(relative)
                elif not ignored:
                    raise WorkerError('Git metadata has an unsupported file')
            if _stamp(os.fstat(parent)) != _stamp(initial):
                raise WorkerError('Git metadata changed during selection')
        walk(descriptor, '', 0)
    finally:
        os.close(descriptor)
    if 'head' not in seen or 'index' not in paths or not any(path.startswith('objects/') for path in paths):
        raise WorkerError('Git metadata lacks index or self-contained object data')
    return tuple(sorted(paths))


def _ref_lines(raw: bytes, *, max_lines: int) -> list[str]:
    if (not raw.endswith(b'\n') or raw.count(b'\n') > max_lines
            or re.search(rb'[^\x20-\x7e\n]', raw) is not None):
        raise WorkerError('Git ref list has unsupported characters or line count')
    # LF alone separates records. str.splitlines also treats several control bytes as
    # separators, bypassing an LF count; bound every line before splitting its fields.
    lines = raw.decode('ascii').split('\n')[:-1]
    if any(len(line) > limits.MAX_CI_GIT_REF_BYTES for line in lines):
        raise WorkerError('Git ref list exceeds its per-line cap')
    return lines


def _texts(root: Path, paths: tuple[str, ...]) -> None:
    refs: set[str] = set()
    for path in paths:
        if path.startswith('refs/'):
            raw = read_child_file(root, path, max_bytes=limits.MAX_CI_GIT_REF_BYTES)
            value = raw.decode('ascii')
            if value.endswith('\n'):
                value = value[:-1]
            if not (grammar.is_match(grammar.SHA1, value) or value.startswith('ref: ') and _ref(value[5:])):
                raise WorkerError('Git loose ref has unsupported contents')
        elif path == 'shallow':
            raw = read_child_file(root, path, max_bytes=limits.MAX_CI_GIT_REF_LIST_BYTES)
            lines = _ref_lines(raw, max_lines=limits.MAX_CI_GIT_METADATA_FILES)
            if (not raw.endswith(b'\n') or not lines or len(lines) > limits.MAX_CI_GIT_METADATA_FILES
                    or len(set(lines)) != len(lines) or any(not grammar.is_match(grammar.SHA1, line) for line in lines)):
                raise WorkerError('Git shallow boundary is malformed')
        elif path == 'packed-refs':
            raw = read_child_file(root, path, max_bytes=limits.MAX_CI_GIT_REF_LIST_BYTES)
            lines = _ref_lines(raw, max_lines=2 * limits.MAX_CI_GIT_METADATA_FILES + 1)
            previous = False
            for line in lines:
                if line.startswith('# pack-refs with:'):
                    if any(word not in {'peeled', 'fully-peeled', 'sorted'} for word in line[17:].split()):
                        raise WorkerError('Git packed refs have unsupported flags')
                    previous = False
                elif line.startswith('^'):
                    if not previous or not grammar.is_match(grammar.SHA1, line[1:]):
                        raise WorkerError('Git peeled ref is malformed')
                    previous = False
                else:
                    fields = line.split(' ')
                    if (len(fields) != 2 or not grammar.is_match(grammar.SHA1, fields[0])
                            or not _ref(fields[1]) or fields[1].casefold() in refs):
                        raise WorkerError('Git packed ref is unsafe or duplicated')
                    refs.add(fields[1].casefold())
                    if len(refs) > limits.MAX_CI_GIT_METADATA_FILES:
                        raise WorkerError('Git packed refs exceed their count cap')
                    previous = True


def _configuration(repository: str) -> bytes:
    grammar.require(grammar.REPOSITORY, repository, 'repository')
    return ('[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n'
            '\tlogallrefupdates = false\n\thooksPath = /dev/null\n\tfsmonitor = false\n'
            '\tsymlinks = true\n\tautocrlf = false\n'
            '[remote "origin"]\n\turl = https://github.com/' + repository + '.git\n'
            '\tfetch = +refs/heads/*:refs/remotes/origin/*\n').encode('ascii')


def stage_privileged_worker_git(root: Path, *, boundary: HostBoundary, account: WorkerAccount,
                                repository: str, tested_commit: str) -> list[dict[str, Any]]:
    """Curate original protected Git data, exclusively publishing candidate-owned .git.

    Caller independently authenticates original self-contained SHA-1 checkout/commit/tree/index,
    tracked worker source, original code/runtime, excluded writers and no earlier UID activity.
    Never parse object/index bytes with Git or execute copied data as Root. Fixed configuration
    replaces source config, HEAD is the admitted detached commit; no hooks/logs/credentials copy.
    Data copying is not independent object-graph/native/privileged Git execution approval.
    """
    authenticate_privileged_host_boundary(boundary)
    actual = authenticate_worker_account('worker')
    if type(account) is not WorkerAccount or account != actual or account.uid == boundary.uid:
        raise WorkerError('Git staging requires the fresh candidate account')
    source = parent = installed = None
    try:
        grammar.require_sha1(tested_commit)
        configuration = _configuration(repository)
        _quiet(account)
        if not isinstance(root, Path):
            raise WorkerError('Git source must be a protected data path')
        path = _canonical_path(root.as_posix())
        if not path.as_posix().startswith(boundary.home + '/'):
            raise WorkerError('Git source must be inside private runner home')
        source = _open_directory(tuple(path.parts[1:]))
        original = os.fstat(source)
        if original.st_uid not in (0, boundary.uid) or original.st_mode & 0o022:
            raise WorkerError('Git source root is not protected')
        paths = _selection(root, boundary)
        _texts(root, paths)
        expected = selected_regular_data_records(root, paths=paths, **_BOUNDS)
        packet = {'HEAD': (tested_commit + '\n').encode('ascii'), 'config': configuration}
        final = sorted(expected + [{'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                                   for name, data in packet.items()], key=lambda row: row['path'])
        if sum(row['size'] for row in final) > limits.MAX_CI_GIT_METADATA_TREE_BYTES:
            raise WorkerError('Git metadata with generated files exceeds total cap')
        checkout = WORKER_ROOT / 'repository'
        parent = _open_directory(tuple(checkout.parts[1:]))
        checkout_info = os.fstat(parent)
        if (checkout_info.st_uid, checkout_info.st_gid, stat.S_IMODE(checkout_info.st_mode)) != (account.uid, account.gid, 0o700):
            raise WorkerError('Git destination requires the admitted private candidate repository')
        destination = Path(str(checkout / '.git'))
        def recheck() -> None:
            authenticate_privileged_host_boundary(boundary)
            if authenticate_worker_account('worker') != account:
                raise WorkerError('Git staging worker identity changed')
            _quiet(account)
            current = _open_directory(tuple(path.parts[1:]))
            repo = None
            try:
                repo = _open_directory(tuple(checkout.parts[1:]))
                info = os.fstat(repo)
                if (_stamp(os.fstat(source)) != _stamp(original) or _stamp(os.fstat(current)) != _stamp(original)
                        or (info.st_dev, info.st_ino) != (checkout_info.st_dev, checkout_info.st_ino)
                        or _stamp(info) != _stamp(os.fstat(parent))
                        or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (account.uid, account.gid, 0o700)):
                    raise WorkerError('Git source or candidate parent binding changed')
            finally:
                if repo is not None: os.close(repo)
                os.close(current)
        def fill(stage: Path, descriptor: int) -> tuple[int, int]:
            info = os.fstat(descriptor)
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o700)
            recheck()
            if copy_selected_regular_data_files(root, descriptor, paths=paths, **_BOUNDS) != expected:
                raise WorkerError('Git data changed during copying')
            for name, data in packet.items(): write_new(descriptor, name, data)
            for name in ('objects', 'refs'):
                try: os.mkdir(name, 0o700, dir_fd=descriptor)
                except FileExistsError: pass
            if (regular_data_records(stage, **_BOUNDS) != final
                    or privatize_regular_data_copy(stage, source_owner_uid=0, owner_uid=account.uid,
                                                   owner_gid=account.gid, **_BOUNDS) != final):
                raise WorkerError('Git private ownership handoff changed data')
            authenticate_tree_private_access(stage, owner_uid=account.uid, owner_gid=account.gid,
                                              max_entries=limits.MAX_CI_GIT_METADATA_ENTRIES)
            if _selection(root, boundary) != paths or selected_regular_data_records(root, paths=paths, **_BOUNDS) != expected:
                raise WorkerError('Git source changed around publication')
            _texts(root, paths)
            recheck()
            return info.st_dev, info.st_ino
        recheck()
        identity = atomic_directory(destination, fill)
        recheck()
        installed = _open_directory(tuple(destination.parts[1:]))
        info = os.fstat(installed)
        if (info.st_dev, info.st_ino) != identity:
            raise WorkerError('Git publication original inode changed')
        authenticate_tree_private_access(destination, owner_uid=account.uid, owner_gid=account.gid,
                                          max_entries=limits.MAX_CI_GIT_METADATA_ENTRIES)
        if (regular_data_records(destination, **_BOUNDS) != final or _selection(root, boundary) != paths
                or selected_regular_data_records(root, paths=paths, **_BOUNDS) != expected):
            raise WorkerError('Git publication/source data changed')
        _texts(root, paths)
        recheck()
        named = _open_directory(tuple(destination.parts[1:]))
        try:
            if _stamp(os.fstat(named)) != _stamp(os.fstat(installed)):
                raise WorkerError('Git publication name changed')
        finally: os.close(named)
        return final
    except (OSError, UnicodeError, ValueError) as error:
        terminate_worker(account)
        raise WorkerError('cannot curate private worker Git metadata') from error
    except BaseException:
        terminate_worker(account)
        raise
    finally:
        for descriptor in (installed, parent, source):
            if descriptor is not None: os.close(descriptor)
