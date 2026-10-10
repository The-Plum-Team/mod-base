"""The protected Git writer of batch stacks (K5): plumbing in a private bare store.

A batch is a stack of squash commits, one per member pull request, on top of the base commit.
Every commit is written with ``git commit-tree`` under one fixed bot identity and the base
commit's committer time, with the message ``<member title> (#<number>)`` and the trailer
``Batch-Member: <number> <head sha>``. Its id therefore depends on nothing but the base, the
member heads, titles, numbers and order: whoever holds those can build the stack again and
compare ids, which is how a batch is verified (``batch.rebuild_batch``).

A member's patch is the difference between its single merge base with the base commit and its
head. It is applied to the stack with ``git merge-tree --write-tree`` and accepted only when the
result is that patch applied path by path: Git reports no conflict; no path the member and the
stack both changed was deleted by either of them; and the result differs from its parent only on
the member's own paths, each path only the member changed ending exactly as in the member's head.
Those rules make the result independent of rename detection: Git 2.43 ignores every switch for
it in ``merge-tree`` while Git 2.54 honours them, so it is the one part of a merge that would
otherwise differ between the Git that builds a stack and the Git that rebuilds it. The last
rule, like the check that a written commit has exactly the expected id, holds whenever the first
two do; both are kept as checks on what Git answers.

Nothing of the ambient Git state reaches the store. Every call runs the ``git`` found on a fixed
search path with an environment built from nothing: a private empty ``HOME``, the C locale and
UTC, no system or global configuration, no replacement objects, no system attributes and the
empty tree as attribute source (attributes shipped by a member never select a merge driver or a
filter), one allowed transport, no terminal prompt, and the fixed identity. Each call also
disables hooks, credential helpers, automatic maintenance and redirects and checks fetched
objects. The store is created without a template, so it has no hooks and no ``info/attributes``
(which Git would read even with an attribute source set). Some of these overlap on purpose: the
empty ``HOME`` alone already hides a global configuration, and the empty store its hooks.
Git 2.40 is the first version with ``merge-tree --merge-base``; older versions are refused.

The push that publishes a stack can only create its branch: it carries an empty lease, and its
porcelain output must report exactly one new branch (``validate_batch_push_receipt``).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from mod_base.errors import MbError, single_line
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_sha256
from mod_base.model.validators import check, is_evidence_text

BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"
MINIMUM_GIT_VERSION = (2, 40)
GITHUB_ORIGIN = "https://github.com/"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
TRANSPORTS = ("https", "file")
#: The modes a member's patch may give a path: regular file, executable file, symbolic link.
PATCH_MODES = ("100644", "100755", "120000")

#: The only places ``git`` is looked up; the inherited ``PATH`` never chooses the writer.
_SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
_CONFIG = ("-c", f"core.hooksPath={os.devnull}", "-c", "credential.helper=", "-c", "gc.auto=0",
           "-c", "fetch.fsckObjects=true", "-c", "http.followRedirects=false")
_VERSION = re.compile(rb"git version ([0-9]{1,4})\.([0-9]{1,4})(?:[. (][\x20-\x7e]{0,80})?\n?")
_DIFF = re.compile(rb":([0-7]{6}) ([0-7]{6}) ([0-9a-f]{40}) ([0-9a-f]{40}) [ADMT]")
_EPOCH = re.compile(r"[0-9]{1,11}")
_ABSENT_MODE = b"000000"


class BatchGitError(MbError):
    """The Git writer refused, or Git itself failed."""

    default_reason = "ci-batch-git"


def empty_batch_branch_lease(branch: str) -> str:
    """The push option that lets a batch branch only be created: an empty expected value."""

    check(grammar.is_batch_branch(branch), "$.batch.branch", "invalid batch Git branch")
    return "--force-with-lease=refs/heads/" + branch + ":"


def validate_batch_push_receipt(data: bytes, *, exit_code: int, remote: str,
                                branch: str, commit_sha: str) -> None:
    """Require the ``git push --porcelain`` output of exactly one newly created batch branch.

    An exit status of 0 is not enough: a branch that already pointed at the same commit answers
    ``up to date`` with status 0, and that branch was not created by this push.
    """

    empty_batch_branch_lease(branch)
    grammar.require_sha1(commit_sha, "batch pushed commit SHA")
    check(type(exit_code) is int and exit_code == 0, "$.batch.push", "batch push did not succeed")
    check(type(remote) is str and 1 <= len(remote) <= limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES
          and all(32 <= ord(character) <= 126 for character in remote),
          "$.batch.remote", "expected batch destination is malformed or exceeds its cap")
    check(type(data) is bytes and 1 <= len(data) <= limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES,
          "$.batch.push", "push receipt is empty or exceeds its cap")
    check(all(32 <= character <= 126 or character in (9, 10, 13) for character in data),
          "$.batch.push", "push receipt contains invalid encoding/control bytes")
    text = data.decode("ascii")
    check(text.endswith("\n") and "\r" not in text.replace("\r\n", ""),
          "$.batch.push", "push receipt has invalid line framing")
    expected = [f"To {remote}", f"*\t{commit_sha}:refs/heads/{branch}\t[new branch]", "Done"]
    check(text.splitlines() == expected, "$.batch.push", "push did not create the exact new batch branch")


def supported_git_version(output: bytes) -> tuple[int, int]:
    """``(major, minor)`` of a ``git version`` line; anything before Git 2.40 is refused."""

    match = _VERSION.fullmatch(output) if type(output) is bytes else None
    if match is None:
        raise BatchGitError("cannot read the version of the installed Git")
    version = int(match[1]), int(match[2])
    if version < MINIMUM_GIT_VERSION:
        raise BatchGitError(f"Git {version[0]}.{version[1]} is too old: a batch needs Git "
                            f"{MINIMUM_GIT_VERSION[0]}.{MINIMUM_GIT_VERSION[1]} or later")
    return version


@dataclass(frozen=True)
class BatchRemote:
    """The repository as Git reaches it: one URL through exactly one transport.

    ``https`` is ``https://github.com/<owner>/<name>.git``. Its token travels only as an
    authorization header scoped to that origin, in the environment of the Git process: never in
    an argument, a URL or a configuration file. ``file`` is the absolute path of a local
    repository (a mirror, or the remote of a test) and carries no token.
    """

    url: str
    transport: str
    token: str | None = None

    def __post_init__(self) -> None:
        if type(self.transport) is not str or self.transport not in TRANSPORTS:
            raise BatchGitError("a batch remote uses the https or the file transport")
        text = type(self.url) is str and 1 <= len(self.url) <= limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES and all(
            33 <= ord(character) <= 126 for character in self.url)
        if self.transport == "https":
            repository = self.url[len(GITHUB_ORIGIN):-len(".git")] if text else None
            if (not text or self.url != f"{GITHUB_ORIGIN}{repository}.git"
                    or not grammar.is_match(grammar.REPOSITORY, repository)):
                raise BatchGitError("an https batch remote is https://github.com/<owner>/<name>.git")
            # Half the bound of one environment value: the header holds the token in base64.
            if self.token is not None and not (
                    type(self.token) is str and 1 <= len(self.token) <= limits.MAX_CI_ENV_VALUE_BYTES // 2
                    and all(33 <= ord(character) <= 126 for character in self.token)):
                raise BatchGitError("the batch remote token is malformed")
        elif not text or not self.url.startswith("/") or self.token is not None:
            raise BatchGitError("a file batch remote is an absolute path without a token")

    def environment(self) -> dict[str, str]:
        """The Git environment of this remote: its one transport and, for a token, the header."""

        environment = {"GIT_ALLOW_PROTOCOL": self.transport}
        if self.token is not None:
            credential = base64.b64encode(f"x-access-token:{self.token}".encode("ascii")).decode("ascii")
            environment.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"http.{GITHUB_ORIGIN}.extraheader",
                                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}"})
        return environment


def github_remote(repository: str, token: str | None) -> BatchRemote:
    """The https remote of ``repository`` on github.com, authorized by ``token`` when given."""

    return BatchRemote(f"{GITHUB_ORIGIN}{grammar.require(grammar.REPOSITORY, repository, 'repository')}.git",
                       "https", token)


def allowed_path_roots(allowed_paths: tuple[str, ...]) -> frozenset[str]:
    """Validate the caller's allowed-path list and return it as a set.

    Each entry is a canonical repository path. It admits itself and, as a directory, everything
    below it; the list is in strictly ascending order, so it has one spelling.
    """

    check(type(allowed_paths) is tuple and 1 <= len(allowed_paths) <= limits.MAX_CI_BATCH_ALLOWED_PATHS,
          "$.batch.allowed_paths", "must be a bounded non-empty tuple of paths")
    previous = ""
    for path in allowed_paths:
        check(grammar.is_repo_path(path) and path > previous, "$.batch.allowed_paths",
              "must hold canonical repository paths in strictly ascending order")
        previous = path
    return frozenset(allowed_paths)


@dataclass(frozen=True)
class PatchEntry:
    """One path of a member's patch; a side is ``(mode, blob id)`` or ``None`` when absent."""

    path: str
    before: tuple[str, str] | None
    after: tuple[str, str] | None


def patch_sha256(patch: tuple[PatchEntry, ...]) -> str:
    """The identity of a patch inventory: SHA-256 of its canonical JSON, entries in path order."""

    def side(value: tuple[str, str] | None) -> dict[str, str] | None:
        return None if value is None else {"mode": value[0], "git_blob": value[1]}

    return canonical_sha256([{"path": entry.path, "before": side(entry.before), "after": side(entry.after)}
                             for entry in patch])


@dataclass(frozen=True)
class StackMember:
    """What one squash commit is made from: the member's number, title and head commit."""

    number: int
    title: str
    head_sha: str


@dataclass(frozen=True)
class StackCommit:
    """One squash commit of a built stack and the member patch it carries."""

    member: StackMember
    head_tree: str
    merge_base_sha: str
    patch: tuple[PatchEntry, ...]
    squash_sha: str
    result_tree: str


@dataclass(frozen=True)
class BatchStack:
    """A built stack: the base it stands on and one commit per member, in order."""

    base_sha: str
    base_tree: str
    commits: tuple[StackCommit, ...]


def _paths(paths: list[bytes]) -> str:
    """A bounded, readable list of paths for a refusal."""

    shown = sorted(set(paths))[:limits.MAX_CI_BATCH_REPORTED_PATHS]
    text = ", ".join(path.decode("utf-8", "replace") for path in shown)
    extra = len(set(paths)) - len(shown)
    return single_line(text + (f" and {extra} more" if extra else ""), limit=600)


class BatchStore:
    """One private bare repository; create it with :func:`open_batch_store`."""

    def __init__(self, executable: str, root: Path, remote: BatchRemote) -> None:
        self._executable = executable
        self._home = root / "home"
        self._store = root / "store.git"
        self._remote = remote
        self._date: str | None = None
        self._trees: dict[str, str] = {}

    def _git(self, *arguments: str, stdin: bytes = b"", accepted: tuple[int, ...] = (0,),
             transfer: bool = False, in_store: bool = True) -> tuple[int, bytes]:
        """Run one Git command in the hardened environment: ``(exit status, stdout)``."""

        command = [self._executable, *_CONFIG, *([f"--git-dir={self._store}"] if in_store else []), *arguments]
        environment = {
            "PATH": _SEARCH_PATH, "HOME": str(self._home), "XDG_CONFIG_HOME": str(self._home),
            "LC_ALL": "C", "TZ": "UTC", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ATTR_NOSYSTEM": "1", "GIT_ATTR_SOURCE": EMPTY_TREE,
            "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
            "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL,
            **self._remote.environment(),
        }
        if self._date is not None:
            environment.update(GIT_AUTHOR_DATE=self._date, GIT_COMMITTER_DATE=self._date)
        timeout = (limits.CI_BATCH_GIT_TRANSFER_TIMEOUT_SECONDS if transfer
                   else limits.CI_BATCH_GIT_TIMEOUT_SECONDS)
        try:
            completed = subprocess.run(command, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, cwd=self._home, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise BatchGitError(f"git {arguments[0]} did not complete: {type(error).__name__}") from error
        if completed.returncode not in accepted:
            detail = single_line(completed.stderr.decode("utf-8", "replace"), limit=300)
            raise BatchGitError(f"git {arguments[0]} failed with status {completed.returncode}: {detail}")
        if len(completed.stdout) > limits.MAX_CI_BATCH_GIT_OUTPUT_BYTES:
            raise BatchGitError(f"git {arguments[0]} printed more than its output bound")
        return completed.returncode, completed.stdout

    def _create(self) -> None:
        supported_git_version(self._git("version", in_store=False)[1])
        self._git("init", "--quiet", "--bare", "--template=", "--object-format=sha1",
                  "--initial-branch=batch-store", str(self._store), in_store=False)
        # ``info/attributes`` of a repository overrides the empty attribute source, and a hooks
        # directory would be executable content: a store built without a template has neither.
        if sorted(os.listdir(self._store)) != ["HEAD", "config", "objects", "refs"]:
            raise BatchGitError("the private Git store was not created empty")

    def _object(self, *arguments: str, stdin: bytes = b"") -> str:
        """The single object id a command prints."""

        text = self._git(*arguments, stdin=stdin)[1].decode("ascii", "replace").strip()
        if not grammar.is_match(grammar.SHA1, text):
            raise BatchGitError(f"git {arguments[0]} did not name one object")
        return text

    def _tree(self, commit: str) -> str:
        if commit not in self._trees:
            self._trees[commit] = self._object("rev-parse", "--verify", "--end-of-options", commit + "^{tree}")
        return self._trees[commit]

    def fetch(self, *, base_sha: str, heads: Mapping[int, str], base_branch: str | None = None) -> None:
        """Fetch the base and every member head, and require them to be the expected commits.

        With ``base_branch`` the live refs are fetched (``refs/heads/<base_branch>`` and each
        ``refs/pull/<number>/head``): an id that differs from the expected one is a base or a head
        that moved after the API reported it. Without it the commits are fetched by id, which is
        how a merged batch is rebuilt after its refs have moved on.
        """

        expected = {"refs/mb/base": (grammar.require_sha1(base_sha, "batch base SHA"), base_branch or "the base")}
        sources = {"refs/mb/base": base_sha if base_branch is None
                   else "refs/heads/" + grammar.require(grammar.BRANCH, base_branch, "batch base branch")}
        check(1 <= len(heads) <= limits.MAX_CI_BATCH_MEMBERS, "$.batch.members", "requires 1 to 50 member heads")
        for number, head in heads.items():
            grammar.require_positive_int(number, "batch member number")
            expected[f"refs/mb/pr/{number}"] = (grammar.require_sha1(head, "batch member head SHA"), f"#{number}")
            sources[f"refs/mb/pr/{number}"] = head if base_branch is None else f"refs/pull/{number}/head"
        self._git("fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules", "--no-auto-gc",
                  "--quiet", self._remote.url, *(f"+{source}:{target}" for target, source in sources.items()),
                  transfer=True)
        listing = self._git("for-each-ref", "--format=%(refname) %(objectname) %(objecttype)", "refs/mb/")[1]
        observed = {fields[0]: (fields[1], fields[2]) for fields in
                    (line.split(" ") for line in listing.decode("ascii", "replace").split("\n")) if len(fields) == 3}
        for target, (sha, label) in expected.items():
            if observed.get(target) != (sha, "commit"):
                raise BatchGitError(f"{label} moved while the batch was being prepared; run it again"
                                    if base_branch is not None else f"{label} is not the expected commit")

    def _changes(self, before: str, after: str) -> dict[bytes, tuple[tuple[str, str] | None, tuple[str, str] | None]]:
        """Path -> ``(before, after)`` of every path that differs between two trees, renames off."""

        if before == after:
            return {}
        fields = self._git("diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-ext-diff", "--no-abbrev",
                           "--end-of-options", before, after)[1].split(b"\0")
        if len(fields) % 2 != 1 or fields[-1]:
            raise BatchGitError("git diff-tree printed an unreadable listing")
        changes: dict[bytes, tuple[tuple[str, str] | None, tuple[str, str] | None]] = {}
        for header, path in zip(fields[0:-1:2], fields[1:-1:2]):
            match = _DIFF.fullmatch(header)
            if match is None or not path or path in changes:
                raise BatchGitError("git diff-tree printed an unreadable entry")
            sides = tuple(None if mode == _ABSENT_MODE else (mode.decode("ascii"), blob.decode("ascii"))
                          for mode, blob in ((match[1], match[3]), (match[2], match[4])))
            changes[path] = (sides[0], sides[1])
        return changes

    def _patch(self, label: str, merge_base_tree: str, head_tree: str,
               roots: frozenset[str] | None) -> tuple[PatchEntry, ...]:
        """The member's own patch, every path of it canonical, of a supported mode and allowed."""

        entries = []
        for raw, (before, after) in self._changes(merge_base_tree, head_tree).items():
            try:
                path = raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                path = ""
            if not grammar.is_repo_path(path):
                raise BatchGitError(f"{label} changes a path outside the repository path grammar: {_paths([raw])}")
            modes = {side[0] for side in (before, after) if side is not None}
            if "160000" in modes:
                raise BatchGitError(f"{label} changes a submodule entry: {path}")
            if not modes <= set(PATCH_MODES):
                raise BatchGitError(f"{label} changes an entry of an unsupported mode: {path}")
            parts = path.split("/")
            if roots is not None and not any("/".join(parts[:count]) in roots for count in range(1, len(parts) + 1)):
                raise BatchGitError(f"{label} changes a path the protected policy does not allow in a batch: {path}")
            entries.append(PatchEntry(path, before, after))
        return tuple(sorted(entries, key=lambda entry: entry.path))

    def _squash(self, member: StackMember, *, base_sha: str, base_branch: str, epoch: str, parent: str,
                parent_tree: str, roots: frozenset[str] | None) -> StackCommit:
        label = f"#{member.number}"
        status, output = self._git("merge-base", "--all", "--end-of-options", base_sha, member.head_sha,
                                   accepted=(0, 1))
        bases = output.decode("ascii", "replace").split()
        if status != 0 or len(bases) != 1 or not grammar.is_match(grammar.SHA1, bases[0]):
            raise BatchGitError(f"{label} needs exactly one merge base with {base_branch}; rebase it")
        merge_base = bases[0]
        merge_base_tree, head_tree = self._tree(merge_base), self._tree(member.head_sha)
        patch = self._patch(label, merge_base_tree, head_tree, roots)
        theirs = {entry.path.encode("utf-8"): entry.after for entry in patch}
        ours = self._changes(merge_base_tree, parent_tree)
        conflict = f"{label} conflicts with {base_branch} or an earlier batch entry: "
        # A path both sides changed and one of them deleted is where rename detection decides the
        # outcome; it is refused here so that the result never depends on it.
        deleted = [path for path, after in theirs.items() if path in ours and (after is None or ours[path][1] is None)]
        if deleted:
            raise BatchGitError(conflict + _paths(deleted))
        status, output = self._git("merge-tree", "--write-tree", "--name-only", "-z", "--no-messages",
                                   f"--merge-base={merge_base}", parent, member.head_sha, accepted=(0, 1))
        fields = output.split(b"\0")
        if status == 1:
            raise BatchGitError(conflict + _paths([path for path in fields[1:] if path]))
        result_tree = fields[0].decode("ascii", "replace")
        if len(fields) != 2 or fields[1] or not grammar.is_match(grammar.SHA1, result_tree):
            raise BatchGitError("git merge-tree printed an unreadable result")
        if result_tree == parent_tree:  # an empty patch, or one the stack already holds
            raise BatchGitError(f"{label} adds nothing on top of {base_branch} and earlier batch entries")
        changed = {path: sides[1] for path, sides in self._changes(parent_tree, result_tree).items()}
        outside = [path for path in changed if path not in theirs]
        inexact = [path for path, after in theirs.items()
                   if path not in ours and (path not in changed or changed[path] != after)]
        if outside or inexact:
            raise BatchGitError(f"{label} does not apply as its own patch (a rename was followed): "
                                + _paths(outside + inexact))
        message = (f"{member.title} (#{member.number})\n\n"
                   f"Batch-Member: {member.number} {member.head_sha}\n").encode("utf-8")
        squash = self._object("commit-tree", "--no-gpg-sign", result_tree, "-p", parent, stdin=message)
        # The id must be that of exactly this object: no header Git might add on its own.
        identity = f"{BOT_NAME} <{BOT_EMAIL}> {epoch} +0000"
        body = (f"tree {result_tree}\nparent {parent}\nauthor {identity}\ncommitter {identity}\n\n".encode("ascii")
                + message)
        if squash != hashlib.sha1(b"commit %d\0" % len(body) + body).hexdigest():  # noqa: S324 - a Git object id
            raise BatchGitError("Git wrote a commit that is not the canonical batch commit")
        return StackCommit(member, head_tree, merge_base, patch, squash, result_tree)

    def build_stack(self, *, base_sha: str, base_branch: str, members: tuple[StackMember, ...],
                    allowed_paths: tuple[str, ...] | None) -> BatchStack:
        """Build one squash commit per member, in order, on top of the fetched base.

        The same call builds a batch and rebuilds it for verification: equal inputs give equal
        commit ids. ``allowed_paths`` is the protected policy's list (:func:`allowed_path_roots`);
        ``None`` checks no path policy, for a rebuild of what a batch pull request already merged.
        A conflict, a member that adds nothing to the stack before it, a submodule entry, a path
        outside the list and a merge that is not the member's patch applied path by path are
        refused, naming the pull request and the paths.
        """

        roots = None if allowed_paths is None else allowed_path_roots(allowed_paths)
        grammar.require_sha1(base_sha, "batch base SHA")
        grammar.require(grammar.BRANCH, base_branch, "batch base branch")
        check(type(members) is tuple and 1 <= len(members) <= limits.MAX_CI_BATCH_MEMBERS
              and all(type(member) is StackMember for member in members), "$.batch.members",
              "requires 1 to 50 stack members")
        for member in members:
            grammar.require_positive_int(member.number, "batch member number")
            grammar.require_sha1(member.head_sha, "batch member head SHA")
            check(is_evidence_text(member.title, limits.MAX_CI_BATCH_TITLE_CHARS), "$.batch.members",
                  f"#{member.number} needs a title of printable text")
        check(len({member.number for member in members}) == len(members), "$.batch.members", "duplicate batch member")
        epoch = self._git("show", "--no-patch", "--format=%ct", "--end-of-options",
                          base_sha + "^{commit}")[1].decode("ascii", "replace").strip()
        if _EPOCH.fullmatch(epoch) is None:
            raise BatchGitError("the batch base has no readable committer time")
        self._date = f"@{epoch} +0000"
        base_tree = self._tree(base_sha)
        parent, parent_tree, commits = base_sha, base_tree, []
        for member in members:
            commit = self._squash(member, base_sha=base_sha, base_branch=base_branch, epoch=epoch, parent=parent,
                                  parent_tree=parent_tree, roots=roots)
            commits.append(commit)
            parent, parent_tree = commit.squash_sha, commit.result_tree
        return BatchStack(base_sha, base_tree, tuple(commits))

    def publish(self, *, branch: str, commit_sha: str) -> None:
        """Push ``commit_sha`` as the new branch ``branch``; an existing branch is never touched."""

        lease = empty_batch_branch_lease(branch)
        grammar.require_sha1(commit_sha, "batch pushed commit SHA")
        status, receipt = self._git("push", "--porcelain", "--no-verify", lease, self._remote.url,
                                    f"{commit_sha}:refs/heads/{branch}", accepted=(0, 1), transfer=True)
        if status != 0:
            raise BatchGitError(f"could not create {branch}: it already exists or the push was refused")
        validate_batch_push_receipt(receipt, exit_code=status, remote=self._remote.url, branch=branch,
                                    commit_sha=commit_sha)


@contextlib.contextmanager
def open_batch_store(parent: Path, remote: BatchRemote) -> Iterator[BatchStore]:
    """A new private store in a fresh mode-0700 directory below ``parent``, removed afterwards.

    ``parent`` is a directory only this user can write (a runner-private state directory).
    """

    if os.name != "posix":
        raise BatchGitError("the protected Git writer runs on POSIX hosts only")
    if type(remote) is not BatchRemote or not isinstance(parent, Path):
        raise BatchGitError("a batch store needs its remote and a state directory")
    executable = shutil.which("git", path=_SEARCH_PATH)
    if executable is None:
        raise BatchGitError(f"git is not installed in {_SEARCH_PATH}")
    # Git runs in the store's own directory, so every path it is given must be absolute.
    parent = Path(os.path.abspath(parent))
    try:
        info = os.lstat(parent)
    except OSError as error:
        raise BatchGitError("the batch state directory is not accessible") from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise BatchGitError("the batch state directory must be a real directory only its owner can write")
    try:
        root = Path(tempfile.mkdtemp(prefix="mb-batch-", dir=parent))
    except OSError as error:
        raise BatchGitError("cannot create the private Git store") from error
    try:
        os.mkdir(root / "home", 0o700)
        store = BatchStore(executable, root, remote)
        store._create()
        yield store
    finally:
        shutil.rmtree(root, ignore_errors=True)
