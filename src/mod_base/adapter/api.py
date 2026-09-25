"""The ``ctx`` object every adapter hook receives (MB3), constructed by the child process.

Members (SPEC §4.2): ``repo_root``, ``config`` (the validated :class:`mod_base.config.Config`),
``tmpdir`` (a private per-call directory), ``implementation_sha`` (``GITHUB_SHA`` in Pages jobs, the
``--subject-commit`` in ``prepare``), ``read_blob`` (bounded ``git cat-file`` of inert objects),
``runtime_tree`` (a bounded regular-file view), ``image_metrics`` (the kit's PixelMetrics) and
``api`` (a read-only :class:`mod_base.github.api.GitHubApi`, present only for a declared network
hook running in a token job; ``None`` otherwise).

Git runs only as ``git --no-replace-objects -C <repo_root> cat-file`` with a sanitized environment
(no inherited ``GIT_*`` variable, no global or system configuration, no terminal prompt and no
lazy fetch of a missing object), so a hook can read objects the core already fetched as inert
objects and nothing else: ``read_blob`` never checks out, never fetches and never follows a
replacement ref.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.config import Config
from mod_base.errors import MbError
from mod_base.github.api import GitHubApi
from mod_base.imaging.metrics import SizePolicy, inspect_png
from mod_base.io.tree import TreeError, read_child_file, regular_files
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import strict_loads

OWNER = "MB3"

#: The largest file a runtime tree may hold (a source PNG; runtime JSON is bounded by the caller).
_MAX_RUNTIME_FILE_BYTES = lim.MAX_SOURCE_PNG_BYTES
#: Where ``git`` is looked up (the hook child's ``PATH`` is already this plus the interpreter's).
_GIT_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
_GIT_TIMEOUT_SECONDS = 120
#: Bound of ``cat-file --batch-check`` output (one object line).
_MAX_BATCH_CHECK_BYTES = 4096


class RuntimeTree:
    """A bounded, regular-file-only, symlink-refusing view of a runtime directory. Paths are
    canonical relative POSIX paths; every read is bounded and stat-stable."""

    def __init__(self, root: Path, *, max_files: int, max_total_bytes: int) -> None:
        for label, value in (("max_files", max_files), ("max_total_bytes", max_total_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise TreeError(f"runtime tree {label} must be a positive integer")
        candidate = Path(os.path.abspath(root))
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise TreeError(f"cannot inspect the runtime root: {exc.strerror or exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TreeError("the runtime root must be a real directory")
        self._root = candidate
        self._max_files = max_files
        self._max_total_bytes = max_total_bytes
        self._files: list[str] | None = None

    @property
    def root(self) -> Path:
        return self._root

    def files(self) -> list[str]:
        """Every regular file, sorted (the walk enforces the bounds)."""

        if self._files is None:
            self._files = list(regular_files(self._root, max_files=self._max_files,
                                             max_total_bytes=self._max_total_bytes,
                                             max_file_bytes=_MAX_RUNTIME_FILE_BYTES))
        return list(self._files)

    def _kind(self, relative: str) -> str | None:
        """``"file"``, ``"directory"`` or ``None`` (absent) for ``relative``; any symlink or special
        entry on the way raises instead of being followed."""

        if not grammar.is_bundle_path(relative):
            raise TreeError(f"runtime path is not a canonical relative path: {relative!r}"[:300])
        current = self._root
        parts = relative.split("/")
        for position, part in enumerate(parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise TreeError(f"cannot inspect runtime path {relative!r}: {exc.strerror or exc}"[:300]) from exc
            if stat.S_ISLNK(info.st_mode):
                raise TreeError(f"runtime path crosses a symlink: {relative!r}"[:300])
            last = position == len(parts) - 1
            if stat.S_ISDIR(info.st_mode):
                if last:
                    return "directory"
                continue
            if not stat.S_ISREG(info.st_mode):
                raise TreeError(f"runtime path is a special file: {relative!r}"[:300])
            return "file" if last else None
        return None

    def exists(self, relative: str) -> bool:
        return self._kind(relative) == "file"

    def read_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        return read_child_file(self._root, relative, max_bytes=max_bytes)

    def read_json(self, relative: str, *, max_bytes: int) -> Any:
        """Strict JSON (duplicate keys and non-finite numbers refused)."""

        return strict_loads(self.read_bytes(relative, max_bytes=max_bytes), label=relative, max_bytes=max_bytes)

    def path(self, relative: str) -> Path:
        """The absolute path of an existing regular file (for image inspection)."""

        if not self.exists(relative):
            raise TreeError(f"runtime file does not exist: {relative!r}"[:300])
        return self._root.joinpath(*relative.split("/"))


def _git_environment(home: Path) -> dict[str, str]:
    """A minimal environment for a read-only git call: no inherited ``GIT_*`` variable."""

    return {
        "PATH": _GIT_SEARCH_PATH,
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _git_executable() -> str:
    found = shutil.which("git", path=_GIT_SEARCH_PATH) or shutil.which("git")
    if not found:
        raise MbError("git is not available for reading inert objects", reason="git")
    return found


def _run_git(repo_root: Path, arguments: list[str], *, home: Path, input_bytes: bytes | None = None,
            max_output_bytes: int) -> bytes:
    """Run one read-only ``git --no-replace-objects -C repo_root <arguments>`` and return stdout.

    A non-zero exit, a timeout or more than ``max_output_bytes`` of output raises
    :class:`MbError`. Only object-database reads (``cat-file``, ``rev-parse``) are ever passed."""

    command = [_git_executable(), "--no-replace-objects", "-C", str(repo_root), *arguments]
    try:
        completed = subprocess.run(command, input=input_bytes, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   env=_git_environment(home), timeout=_GIT_TIMEOUT_SECONDS, check=False,
                                   stdin=None if input_bytes is not None else subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MbError(f"git {arguments[0]} failed: {exc}", reason="git") from exc
    if completed.returncode != 0:
        raise MbError(f"git {arguments[0]} failed with exit status {completed.returncode}", reason="git")
    if len(completed.stdout) > max_output_bytes:
        raise MbError(f"git {arguments[0]} output exceeds {max_output_bytes} bytes", reason="git")
    return completed.stdout


def _object_id(data: bytes, algorithm_length: int) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\x00"
    digest = hashlib.sha1() if algorithm_length == 40 else hashlib.sha256()  # noqa: S324 - Git object ids
    digest.update(header)
    digest.update(data)
    return digest.hexdigest()


def _read_blob(repo_root: Path, commit: str, path: str, max_bytes: int, *, home: Path) -> bytes:
    """Bounded read of ``path`` at ``commit`` from ``repo_root``'s object store (see
    :meth:`Context.read_blob`); the Git object id of the returned bytes is recomputed."""

    grammar.require_sha1(commit, "read_blob commit")
    if not grammar.is_repo_path(path) or "\n" in path:
        raise MbError(f"read_blob path is not a canonical repository path: {path!r}"[:200], reason="git")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= lim.MAX_API_RESPONSE_BYTES:
        raise MbError("read_blob max_bytes must be a positive integer within the API response bound", reason="git")
    line = _run_git(repo_root, ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"], home=home,
                   input_bytes=f"{commit}:{path}\n".encode("utf-8"), max_output_bytes=_MAX_BATCH_CHECK_BYTES)
    fields = line.decode("ascii", "replace").strip().split(" ")
    if len(fields) != 3 or fields[1] == "missing":
        raise MbError(f"{path} is not present at {commit} as an inert object", reason="git")
    oid, kind, size_text = fields
    if not (grammar.is_match(grammar.SHA1, oid) or grammar.is_match(grammar.SHA256, oid)):
        raise MbError(f"{path} at {commit} has a malformed object id", reason="git")
    if kind != "blob":
        raise MbError(f"{path} at {commit} is a {kind[:20]}, not a blob", reason="git")
    if not grammar.POSITIVE_DECIMAL.fullmatch(size_text) and size_text != "0":
        raise MbError(f"{path} at {commit} has a malformed size", reason="git")
    size = int(size_text)
    if size > max_bytes:
        raise MbError(f"{path} at {commit} is {size} bytes, more than {max_bytes}", reason="git")
    data = _run_git(repo_root, ["cat-file", "blob", oid], home=home, max_output_bytes=max_bytes)
    if len(data) != size or _object_id(data, len(oid)) != oid:
        raise MbError(f"{path} at {commit} does not match its object id", reason="git")
    return data


def _size_policy(value: SizePolicy | tuple[str, int, int]) -> SizePolicy:
    if isinstance(value, SizePolicy):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 3 and value[0] in ("exact", "minimum"):
        return SizePolicy(value[0], value[1], value[2])
    raise MbError("image size policy must be a SizePolicy or ('exact'|'minimum', width, height)")


@dataclass(frozen=True)
class Context:
    """Passed as ``ctx`` to every hook."""

    repo_root: Path
    config: Config
    tmpdir: Path
    implementation_sha: str
    api: GitHubApi | None = None

    def read_blob(self, commit: str, path: str, max_bytes: int) -> bytes:
        """Bytes of ``path`` at ``commit`` from the local object store (``git -C repo_root cat-file``
        with a sanitized environment); the object must already be present (fetched as an inert
        object) and be a blob no larger than ``max_bytes``. Never checks anything out."""

        return _read_blob(Path(self.repo_root), commit, path, max_bytes, home=Path(self.tmpdir))

    def runtime_tree(self, root: str | Path) -> RuntimeTree:
        """A :class:`RuntimeTree` over ``root`` with the kit's runtime bounds."""

        return RuntimeTree(Path(root), max_files=lim.MAX_RUNTIME_FILES, max_total_bytes=lim.MAX_RAW_BUNDLE_BYTES)

    def image_metrics(self, path: str | Path, size_policy: SizePolicy | tuple[str, int, int]) -> dict[str, Any]:
        """The kit's PixelMetrics for a PNG (``imaging.metrics.inspect_png``)."""

        return inspect_png(Path(path), _size_policy(size_policy))
