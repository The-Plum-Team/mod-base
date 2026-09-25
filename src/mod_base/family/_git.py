"""Read-only queries of the mod checkout's Git object store for families (MB4).

Every call is one ``git --no-replace-objects -C <repo> <command>`` without a shell and with a
sanitized environment: no inherited ``GIT_*`` variable, no global or system configuration, no
terminal prompt, no lazy fetch of a missing object, no replacement refs, no graft file, no
optional locks and a discovery ceiling at the checkout's parent (a checkout that is not a Git
repository is refused instead of resolving to an enclosing one). Only objects already present
are consulted (the ``family`` job fetches coverage ancestry anonymously as inert objects before
``family collect`` runs); nothing is ever fetched, checked out or written.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model import grammar

#: The only places ``git`` is looked up (the hosted runners install it there). The inherited
#: ``PATH`` (which earlier steps may extend through ``GITHUB_PATH``) never chooses the executable
#: that performs the ancestry proof.
_GIT_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
_GIT_TIMEOUT_SECONDS = 120
#: Every query but :func:`read_blob` prints at most one object id, type or size line.
_MAX_OUTPUT_BYTES = 4096


def _fail(message: str) -> MbError:
    return MbError(message, reason="git")


def _executable() -> str:
    found = shutil.which("git", path=_GIT_SEARCH_PATH)
    if not found:
        raise _fail(f"git is not installed in {_GIT_SEARCH_PATH} for reading inert objects")
    return found


def _environment(repo_root: Path, home: str) -> dict[str, str]:
    return {
        "PATH": _GIT_SEARCH_PATH,
        "HOME": home,
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(repo_root.parent),
    }


def _query(repo_root: Path, arguments: list[str], *, accepted: tuple[int, ...], max_output: int) -> tuple[int, bytes]:
    """Run one read-only query and return ``(exit status, stdout bytes)``; any status outside
    ``accepted``, a timeout or more than ``max_output`` bytes of output raises :class:`MbError`."""

    root = Path(os.path.abspath(repo_root))
    command = [_executable(), "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(root), *arguments]
    with tempfile.TemporaryDirectory(prefix="mb-family-git-") as home:
        try:
            completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, env=_environment(root, home),
                                       timeout=_GIT_TIMEOUT_SECONDS, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise _fail(f"git {arguments[0]} failed: {exc}") from exc
    if completed.returncode not in accepted:
        raise _fail(f"git {arguments[0]} failed with exit status {completed.returncode}")
    if len(completed.stdout) > max_output:
        raise _fail(f"git {arguments[0]} printed more than {max_output} bytes")
    return completed.returncode, completed.stdout


def _run(repo_root: Path, arguments: list[str], *, accepted: tuple[int, ...] = (0,)) -> tuple[int, str]:
    """One query printing at most one object id or type line: ``(exit status, stripped stdout)``."""

    status, output = _query(repo_root, arguments, accepted=accepted, max_output=_MAX_OUTPUT_BYTES)
    return status, output.decode("ascii", "replace").strip()


def head(repo_root: Path) -> tuple[str, str]:
    """``(commit, tree)`` of the checkout's ``HEAD``, both SHA-1 object ids."""

    _, commit = _run(repo_root, ["rev-parse", "--verify", "HEAD^{commit}"])
    _, tree = _run(repo_root, ["rev-parse", "--verify", "HEAD^{tree}"])
    return grammar.require_sha1(commit, "HEAD commit"), grammar.require_sha1(tree, "HEAD tree")


def require_commit(repo_root: Path, sha: str, label: str) -> None:
    """``sha`` must be present in the object store as a commit object itself (never a tag or any
    other object that peels to one)."""

    grammar.require_sha1(sha, label)
    status, kind = _run(repo_root, ["cat-file", "-t", sha], accepted=(0, 1, 128))
    if status != 0:
        raise _fail(f"the {label} {sha} is not present as an inert object")
    if kind != "commit":
        raise _fail(f"the {label} {sha} is a {kind[:20]!r} object, not a commit")


def is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    """``git merge-base --is-ancestor ancestor descendant`` (both must be present commits)."""

    status, _ = _run(repo_root, ["merge-base", "--is-ancestor", grammar.require_sha1(ancestor, "ancestor"),
                                 grammar.require_sha1(descendant, "descendant")], accepted=(0, 1))
    return status == 0


def read_blob(repo_root: Path, commit: str, path: str, max_bytes: int) -> bytes:
    """The bytes of the regular file ``path`` at the present commit ``commit`` (its tree entry, never
    a symlink or submodule), at most ``max_bytes``, from the object store: never fetched, never
    checked out."""

    require_commit(repo_root, commit, "commit")
    if not grammar.is_repo_path(path):
        raise _fail(f"not a canonical repository path: {path[:120]!r}")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise _fail("the blob bound must be a positive integer")
    _, entry = _run(repo_root, ["ls-tree", "--full-tree", commit, "--", path])
    fields = entry.split("\t", 1)
    head = fields[0].split(" ")
    if len(fields) != 2 or fields[1] != path or len(head) != 3:
        raise _fail(f"{path} is not a file at {commit}")
    mode, kind, oid = head
    if mode not in ("100644", "100755") or kind != "blob" or not (
            grammar.is_match(grammar.SHA1, oid) or grammar.is_match(grammar.SHA256, oid)):
        raise _fail(f"{path} at {commit} is not a regular file")
    _, size = _run(repo_root, ["cat-file", "-s", oid])
    if not size.isdigit() or int(size) > max_bytes:
        raise _fail(f"{path} at {commit} is larger than {max_bytes} bytes")
    _, data = _query(repo_root, ["cat-file", "blob", oid], accepted=(0,), max_output=max_bytes)
    if len(data) != int(size):
        raise _fail(f"{path} at {commit} changed while it was read")
    return data
