"""Adapter ``targets`` orchestration, enrolled-branch listing and inert fetches (MB5).

``default-branch`` mode: the adapter receives ``branches=None`` and every key's subject is the
protected head (``GITHUB_SHA``). ``enrolled-branches`` mode: the core lists at most
``targets.max_branches`` branches (one page, ``per_page=100``), fetches their distinct heads
anonymously as inert objects (``git -C repo fetch --no-tags --depth=<n> origin <sha...>``, never
checked out) and passes ``[{name, commit, tree}]``; the adapter decides enrollment through
``ctx.read_blob``.

Block Pops ``version_branches.discover_pages_repository`` read the enrolled heads from the local
remote-tracking refs; here the inventory is the branches API and every head's tree is read from
the fetched, content-addressed commit object (no per-branch API read, so the admission budget does
not grow with the number of branches). A listed name outside the kit's branch grammar can never be
a subject, so it is skipped (it still counts toward the bounds); a malformed row (a non-string
name, an invalid head commit, a repeated name) fails the whole inventory. The canonical branch must
be listed at the protected head: a head that moved since the job started is reported as
``stale-implementation`` (never a partial inventory).

Git runs with a sanitized environment (no inherited ``GIT_*`` variable, no global or system
configuration, no prompt, no replacement objects or graft file, and a discovery ceiling at the
checkout's parent, so a directory that is not a repository never resolves to an enclosing one) and
command-line overrides that reset the unscoped credential helper and extra HTTP header, disable
hooks, submodules and automatic maintenance, and allow only the ``https`` transport. URL-scoped configuration outranks those
overrides (``http.<url>.extraheader`` is what ``actions/checkout`` persists), so a fetch is refused
outright while the repository's effective configuration carries any credential, extra header,
cookie file, askpass program or URL rewrite, or an ``origin`` URL with user information.

:func:`first_parent_history` reads a bounded first-parent history from the local object store
(never a fetch): the Pages jobs that walk it check out the protected head with its full history.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.github import contents
from mod_base.github.api import PER_PAGE, GitHubApi
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.runtime import Invocation

OWNER = "MB5"

FETCH_ATTEMPTS = 3
FETCH_BACKOFF_SECONDS = 2.0
FETCH_TIMEOUT_SECONDS = 600
GIT_TIMEOUT_SECONDS = 120
MAX_FETCH_DEPTH = 1000
#: Transports a fetch may use (tests widen it to ``file`` for a local origin).
FETCH_PROTOCOLS: tuple[str, ...] = ("https",)
_GIT_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
_MAX_GIT_OUTPUT = 4096
_MAX_CONFIG_OUTPUT = 256 * 1024
#: Replaced by tests to skip the backoff waits.
_sleep = time.sleep


def _fail(message: str, reason: str = "targets") -> MbError:
    return MbError(message, reason=reason)


def branch_heads(api: GitHubApi, *, max_branches: int) -> dict[str, str]:
    """Branch name -> head commit from one page of the branches API. More than ``max_branches``
    rows, or a full page, fails closed; a name outside the branch grammar is skipped (it can never
    be a subject) but counts toward both bounds; a malformed row fails the whole listing."""

    maximum = grammar.require_positive_int(max_branches, "max_branches", maximum=lim.MAX_BRANCHES)
    rows = api.get_json(f"/repos/{api.repository}/branches", params={"per_page": PER_PAGE, "page": 1})
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _fail("the branch listing is malformed")
    if len(rows) > maximum:
        raise _fail(f"the repository lists {len(rows)} branches, more than targets.max_branches {maximum}",
                    reason="branch-limit")
    if len(rows) >= PER_PAGE:
        raise _fail(f"the one-page branch listing is full ({PER_PAGE}); it cannot prove it is complete",
                    reason="branch-limit")
    listed: set[str] = set()
    heads: dict[str, str] = {}
    for position, row in enumerate(rows):
        name = row.get("name")
        commit = row.get("commit", {}).get("sha") if isinstance(row.get("commit"), dict) else None
        if not isinstance(name, str) or not name or not grammar.is_match(grammar.SHA1, commit):
            raise _fail(f"branch listing row {position} has no branch name and valid head commit")
        if name in listed:
            raise _fail(f"the branch listing repeats {name!r}"[:200])
        listed.add(name)
        if grammar.is_match(grammar.BRANCH, name):
            heads[name] = commit
    return dict(sorted(heads.items()))


def list_enrolled_branches(api: GitHubApi, *, max_branches: int) -> list[dict[str, str]]:
    """``[{name, commit, tree}]`` of the repository's branches (one page; more than
    ``max_branches`` fails closed), each tree read from the commit API."""

    return [{"name": name, "commit": commit, "tree": contents.commit_tree(api, commit)}
            for name, commit in branch_heads(api, max_branches=max_branches).items()]


def _git_environment(home: Path, root: Path) -> dict[str, str]:
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
        "GIT_GRAFT_FILE": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(root.parent),
    }


def _git_executable() -> str:
    found = shutil.which("git", path=_GIT_SEARCH_PATH) or shutil.which("git")
    if not found:
        raise _fail("git is not available", reason="git")
    return found


def _git(root: Path, arguments: list[str], *, home: Path, timeout: float = GIT_TIMEOUT_SECONDS,
         check: bool = True) -> subprocess.CompletedProcess[bytes]:
    command = [_git_executable(), "--no-replace-objects", "-C", str(root), *arguments]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=_git_environment(home, root), timeout=timeout,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _fail(f"git {arguments[0]} failed: {exc}", reason="git") from exc
    if check and completed.returncode != 0:
        raise _fail(f"git {arguments[0]} failed with exit status {completed.returncode}", reason="git")
    return completed


def _repository(repo_root: Path) -> Path:
    root = Path(os.path.abspath(repo_root))
    try:
        info = root.lstat()
    except OSError as exc:
        raise _fail(f"cannot inspect the repository: {exc.strerror or exc}", reason="git") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail("the repository must be a real directory", reason="git")
    return root


def _has_commit(root: Path, commit: str, *, home: Path) -> bool:
    return _git(root, ["cat-file", "-e", f"{commit}^{{commit}}"], home=home, check=False).returncode == 0


def commit_tree_local(repo_root: Path, commit: str) -> str:
    """The tree of ``commit`` from the local object store (the object must be present)."""

    root = _repository(repo_root)
    grammar.require_sha1(commit)
    with tempfile.TemporaryDirectory(prefix="mb-git-") as home:
        output = _git(root, ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"], home=Path(home)).stdout
    if len(output) > _MAX_GIT_OUTPUT:
        raise _fail("git rev-parse output exceeds its bound", reason="git")
    return grammar.require_sha1(output.decode("ascii", "replace").strip(), "local tree")


def first_parent_history(repo_root: Path, commit: str, *, max_commits: int) -> list[str]:
    """``commit`` followed by at most ``max_commits`` of its first-parent ancestors, newest first,
    read from the local object store (``git rev-list --first-parent``: inert objects, no fetch).
    A root commit or a shallow boundary ends the list early; an absent ``commit`` fails closed."""

    root = _repository(repo_root)
    grammar.require_sha1(commit, "history commit")
    maximum = grammar.require_positive_int(max_commits, "max_commits", maximum=MAX_FETCH_DEPTH)
    with tempfile.TemporaryDirectory(prefix="mb-git-") as directory:
        home = Path(directory)
        if not _has_commit(root, commit, home=home):
            raise _fail(f"commit {commit} is not in the local object store", reason="git")
        output = _git(root, ["rev-list", "--first-parent", f"--max-count={maximum + 1}", "--end-of-options", commit],
                      home=home).stdout
    if len(output) > (maximum + 1) * 41:
        raise _fail("git rev-list output exceeds its bound", reason="git")
    history = output.decode("ascii", "replace").split("\n")
    if history[-1] == "":
        history.pop()
    if (not history or history[0] != commit or len(set(history)) != len(history)
            or not all(grammar.is_match(grammar.SHA1, entry) for entry in history)):
        raise _fail(f"the first-parent history of {commit} is malformed", reason="git")
    return history


def _require_anonymous_configuration(root: Path, *, home: Path) -> None:
    """Refuse a fetch while the effective repository configuration (local file, worktree file and
    includes; system and global are disabled) could attach credentials or rewrite the remote."""

    output = _git(root, ["config", "--list", "--includes", "-z"], home=home).stdout
    if len(output) > _MAX_CONFIG_OUTPUT:
        raise _fail("the repository configuration exceeds its bound", reason="git")
    for entry in output.split(b"\0"):
        name, _, value = entry.decode("utf-8", "replace").partition("\n")
        key = name.lower()
        if (key.startswith("credential.") or key == "core.askpass"
                or (key.startswith("http.") and key.endswith((".extraheader", ".cookiefile")))
                or (key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof")))):
            # A subsection is a URL that may itself carry credentials: name only section and variable.
            section, _, variable = key.partition(".")
            shown = f"{section}.*.{variable.rpartition('.')[2]}" if "." in variable else key
            raise _fail(f"the repository configuration sets {shown!r}; an inert fetch must be anonymous",
                        reason="git-credentials")
        if key == "remote.origin.url" and "://" in value:
            try:
                parts = urllib.parse.urlsplit(value)
            except ValueError:
                raise _fail("the origin URL is malformed", reason="git") from None
            if parts.username is not None or parts.password is not None:
                raise _fail("the origin URL carries user information; an inert fetch must be anonymous",
                            reason="git-credentials")


def fetch_inert(repo_root: Path, commits: Sequence[str], *, depth: int = 1) -> None:
    """Fetch ``commits`` into ``repo_root``'s object store without checkout, tags or credentials
    (sanitized ``git`` environment, ``protocol.version=2``, bounded retries)."""

    root = _repository(repo_root)
    if isinstance(commits, (str, bytes)) or not isinstance(commits, Sequence):
        raise _fail("commits must be a list of commit SHAs", reason="usage")
    wanted = [grammar.require_sha1(commit) for commit in commits]
    if len(set(wanted)) != len(wanted) or len(wanted) > lim.MAX_BRANCHES:
        raise _fail(f"commits must be at most {lim.MAX_BRANCHES} distinct SHAs", reason="usage")
    grammar.require_positive_int(depth, "fetch depth", maximum=MAX_FETCH_DEPTH)
    with tempfile.TemporaryDirectory(prefix="mb-git-") as directory:
        home = Path(directory)
        missing = [commit for commit in wanted if not _has_commit(root, commit, home=home)]
        if not missing:
            return
        _require_anonymous_configuration(root, home=home)
        overrides = ["-c", "protocol.version=2", "-c", "protocol.allow=never",
                     *[item for protocol in FETCH_PROTOCOLS for item in ("-c", f"protocol.{protocol}.allow=always")],
                     "-c", "credential.helper=", "-c", "http.extraHeader=", "-c", f"core.hooksPath={os.devnull}",
                     "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "fetch.recurseSubmodules=false",
                     "-c", "submodule.recurse=false"]
        command = [*overrides, "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
                   f"--depth={depth}", "origin", *missing]
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            if _git(root, command, home=home, timeout=FETCH_TIMEOUT_SECONDS, check=False).returncode == 0:
                break
            if attempt == FETCH_ATTEMPTS:
                raise _fail(f"git fetch of {len(missing)} inert commits failed after {FETCH_ATTEMPTS} attempts",
                            reason="git")
            _sleep(FETCH_BACKOFF_SECONDS * 2 ** (attempt - 1))
        absent = [commit for commit in missing if not _has_commit(root, commit, home=home)]
        if absent:
            raise _fail(f"the fetch did not provide commit {absent[0]}", reason="git")


def discover_targets(invocation: Invocation, *, api: GitHubApi | None) -> list[dict[str, Any]]:
    """Run the adapter ``targets`` hook for the configured mode and return 1..``targets.max``
    validated targets (``api`` is required in enrolled-branches mode)."""

    config = invocation.config
    head = invocation.implementation_sha
    if config.targets["mode"] == "default-branch":
        subject = {"branch": config.canonical_branch, "commit": head,
                   "tree": commit_tree_local(invocation.repo_root, head)}
        targets = host.call(invocation, "targets", {"branches": None})
        for target in targets:
            if target["subject"] != subject:
                raise _fail(f"target {target['key']} does not name the protected head {head} of "
                            f"{config.canonical_branch}")
    else:
        if api is None:
            raise _fail("enrolled-branches discovery needs the read-only API", reason="usage")
        heads = branch_heads(api, max_branches=config.targets["max_branches"])
        if config.canonical_branch not in heads:
            raise _fail(f"the canonical branch {config.canonical_branch} is not in the branch listing")
        if heads[config.canonical_branch] != head:
            raise _fail(f"{config.canonical_branch} advanced past the protected head {head}",
                        reason="stale-implementation")
        commits = sorted(set(heads.values()))
        fetch_inert(invocation.repo_root, commits, depth=1)
        trees = {commit: commit_tree_local(invocation.repo_root, commit) for commit in commits}
        branches = [{"name": name, "commit": commit, "tree": trees[commit]} for name, commit in heads.items()]
        targets = host.call(invocation, "targets", {"branches": branches})
    if not 1 <= len(targets) <= config.targets["max"]:
        raise _fail(f"the adapter returned {len(targets)} targets; targets.max is {config.targets['max']}")
    return [dict(target) for target in targets]
