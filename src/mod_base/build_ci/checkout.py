"""Reads of the Git objects of a job's checkouts (MB11): one closed Git, and commits by their bytes.

Before any kit code runs, a job checks out the protected mod at the controller commit and, where
candidate code is staged, the candidate at the tested commit. This module reads the Git objects
of such a checkout by plumbing; it never reads a working file:

* :func:`read_objects` is the only way Git runs on a checkout: ``/usr/bin/git`` on the checkout's
  own ``.git`` directory with a closed environment. No template, hook, system or global
  configuration and no replacement object takes part, and nothing is fetched.
* :func:`head_commit` reads the commit ``HEAD`` names as the bytes of the object and recomputes the
  object's name from them. Git names a commit by the hash of its content, so a tree and parents
  parsed from bytes that hash to the name are that commit's, wherever the bytes were stored: the
  API can report nothing else for the same name.

Why the bytes and not Git's reading of them. ``--no-replace-objects`` keeps a replacement ref out,
but a grafts file and the shallow boundary of every ``actions/checkout`` clone still change the
parents Git reports (``rev-parse <commit>^2``, ``rev-list --parents``), and ``cat-file`` prints an
object without comparing it with the name it was stored under. Neither a replacement, a graft, a
shallow boundary nor an object stored under another object's name can therefore change what
:func:`head_commit` returns; the last one is a rejection.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from mod_base.errors import MbError
from mod_base.model import grammar, limits

GIT = "/usr/bin/git"
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent",
                "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
                "GIT_LITERAL_PATHSPECS": "1"}


class CheckoutError(MbError):
    """A checkout's Git objects cannot be read, or are not the objects their names say (exit 2)."""

    default_reason = "git"


@dataclass(frozen=True)
class Commit:
    """One commit as its own object states it: its name, its tree and its ordered parents."""

    sha: str
    tree: str
    parents: tuple[str, ...]


def read_objects(checkout: Path, *arguments: str, max_bytes: int) -> bytes:
    """One read of a checkout's object store: fixed program, environment and bound.

    The checkout must have a Git directory of its own: a real ``.git`` directory, not a link and
    not a file that points elsewhere. No template, hook, system or global configuration and no
    replacement object takes part, and nothing is fetched. Only ``rev-parse``, ``ls-tree`` and
    ``cat-file`` are ever passed.
    """

    checkout = Path(os.path.abspath(checkout))
    try:
        own = stat.S_ISDIR(os.lstat(checkout / ".git").st_mode)
    except OSError:
        own = False
    if not own:
        raise CheckoutError(f"the {checkout.name} checkout has no Git directory of its own")
    command = (GIT, f"--git-dir={checkout / '.git'}", "--no-replace-objects", "-c", "core.hooksPath=/dev/null",
               *arguments)
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=dict(_ENVIRONMENT), cwd="/",
                                   timeout=limits.CI_GIT_READ_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise CheckoutError(f"git {arguments[0]} could not read the {checkout.name} checkout") from error
    if completed.returncode != 0 or len(completed.stdout) > max_bytes:
        raise CheckoutError(f"git {arguments[0]} rejected the {checkout.name} checkout")
    return completed.stdout


def head_commit(checkout: Path) -> Commit:
    """The commit ``HEAD`` of ``checkout`` names, proven by its own bytes.

    ``HEAD`` is resolved to an object name without reading the object, and the size of that object
    is asked before its bytes. The object is then read whole as a commit and must hash to that
    name, so ``HEAD`` names the commit itself and not a tag of it. The tree is its first header
    line and the parents are the ``parent`` lines that follow it, in their order; a commit object
    that carries either header anywhere else can be read in two ways and is refused. The parents
    need not be present: a shallow checkout holds the one commit.
    """

    name = Path(os.path.abspath(checkout)).name
    answer = limits.MAX_CI_GIT_ANSWER_BYTES
    sha = read_objects(checkout, "rev-parse", "--verify", "HEAD", max_bytes=answer).decode("ascii", "replace")
    if not (sha.endswith("\n") and grammar.is_match(grammar.SHA1, sha[:-1])):
        raise CheckoutError(f"HEAD of the {name} checkout does not resolve to the name of a SHA-1 object")
    sha = sha[:-1]
    size = read_objects(checkout, "cat-file", "-s", sha, max_bytes=answer).decode("ascii", "replace")
    if not (size.endswith("\n") and size[:-1].isdigit() and int(size) <= limits.MAX_CI_GIT_COMMIT_BYTES):
        raise CheckoutError(f"the object at HEAD of the {name} checkout is larger than a commit may be")
    raw = read_objects(checkout, "cat-file", "commit", sha, max_bytes=limits.MAX_CI_GIT_COMMIT_BYTES)
    if hashlib.sha1(b"commit %d\0" % len(raw) + raw).hexdigest() != sha:  # noqa: S324 - Git object id
        raise CheckoutError(f"the commit read at HEAD of the {name} checkout does not hash to its name")
    lines = raw.partition(b"\n\n")[0].split(b"\n")
    tree = _object_name(lines[0], b"tree ")
    parents: list[str] = []
    for line in lines[1:]:
        parent = _object_name(line, b"parent ")
        if parent is None:
            break
        parents.append(parent)
    if tree is None or any(line.startswith((b"tree ", b"parent ")) for line in lines[1 + len(parents):]):
        raise CheckoutError(f"the HEAD commit of the {name} checkout is not a canonical commit object")
    return Commit(sha, tree, tuple(parents))


def _object_name(line: bytes, key: bytes) -> str | None:
    """The name in the header line ``<key><name>`` of a commit object; ``None`` for any other line."""

    name = line[len(key):].decode("ascii", "replace")
    return name if line.startswith(key) and grammar.is_match(grammar.SHA1, name) else None
