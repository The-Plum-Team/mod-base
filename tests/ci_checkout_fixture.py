"""Real Git checkouts of one generation of the synthetic mod, as the jobs of a run have them.

:class:`Generation` builds the repository GitHub would hold (``upstream``): the default branch at
the controller commit, the head of pull request 7 and its test merge, whose parents are
``[controller, head]``. :meth:`Generation.checkout` then makes a checkout of one commit the way
``actions/checkout`` does: a depth-1 fetch of that commit by its name into a new repository,
detached at it. Such a checkout is shallow and holds no parent of its commit, so Git itself
reports the commit without parents there.

Only the GitHub API is a fake. :meth:`Generation.github` seeds it with what the test reads from
``upstream`` by plain Git, never with what the code under test returned. The helpers at the end
damage a checkout on purpose: an object stored under a name its bytes do not hash to, a ``HEAD``
written by hand.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from mod_base import runtime
from mod_base.build_ci.checkout import Commit
from mod_base.github.fake import FakeGitHub
from tests import ci_mod_harness as h
from tests.ci_lifecycle_fixture import GIT, git

PULL_REQUEST = 7
HEAD_BRANCH = "feature/synthetic"
SIGNATURE = "Fixture <fixture@example.invalid> 1767225600 +0000"
#: A repository as GitHub holds one: its objects are named by SHA-1, whatever this Git defaults to.
INIT = ("init", "-q", "--object-format=sha1")


def installed() -> bool:
    """Whether the Git the kit reads checkouts with exists on this host."""

    return Path(GIT).exists()


class RecordingGitHub(FakeGitHub):
    """A fake GitHub that also remembers the path of every request, in order."""

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.paths: list[str] = []

    def get_json(self, path: str, *, params: Any = None) -> Any:
        self.paths.append(path)
        return super().get_json(path, params=params)


class Generation:
    """One pull request generation of the synthetic mod in a real repository below ``directory``."""

    def __init__(self, directory: Path) -> None:
        self.upstream = h.materialize(directory / "upstream")
        git(self.upstream, *INIT)
        self.base = self._commit("base")
        (self.upstream / "src" / "payload.txt").write_bytes(b"the protected default branch moved on\n")
        self.controller = self._commit("controller")
        git(self.upstream, "checkout", "-q", "-b", HEAD_BRANCH, self.base.sha)
        (self.upstream / "src" / "feature.txt").write_bytes(b"what the pull request adds\n")
        self.head = self._commit("head")
        git(self.upstream, "checkout", "-q", h.BRANCH)
        git(self.upstream, "merge", "-q", "--no-ff", "-m", "test merge", self.head.sha)
        self.tested = self.commit("HEAD")
        git(self.upstream, "update-ref", f"refs/pull/{PULL_REQUEST}/merge", self.tested.sha)
        git(self.upstream, "reset", "-q", "--hard", self.controller.sha)

    def _commit(self, message: str) -> Commit:
        git(self.upstream, "add", "-A")
        git(self.upstream, "commit", "-q", "-m", message)
        git(self.upstream, "update-ref", f"refs/fixture/{message}", "HEAD")
        return self.commit("HEAD")

    def commit(self, name: str) -> Commit:
        """The commit ``name`` of ``upstream`` as plain Git states it there, in a complete history."""

        sha, *parents = git(self.upstream, "rev-list", "--parents", "--max-count=1", name).split()
        return Commit(sha, git(self.upstream, "rev-parse", f"{sha}^{{tree}}"), tuple(parents))

    def commit_tree(self, tree: str, *parents: str, message: str = "another commit") -> Commit:
        """A new commit of ``upstream`` with ``tree`` and exactly ``parents``, kept by a ref."""

        options = [word for parent in parents for word in ("-p", parent)]
        sha = git(self.upstream, "commit-tree", tree, *options, "-m", message)
        git(self.upstream, "update-ref", f"refs/fixture/{sha}", sha)
        return self.commit(sha)

    def checkout(self, destination: Path, commit: str, *, packed: bool = False) -> Path:
        """Check ``commit`` out at ``destination`` as ``actions/checkout`` does. A fetch this small
        is stored as loose objects; ``packed`` keeps the pack, as the fetch of a real mod does."""

        destination.mkdir(parents=True)
        git(destination, *INIT)
        git(destination, "remote", "add", "origin", self.upstream.as_uri())
        git(destination, "-c", "protocol.version=2", "-c", f"fetch.unpackLimit={1 if packed else 100}", "fetch", "-q",
            "--no-tags", "--prune", "--no-recurse-submodules", "--depth=1", "origin", commit)
        git(destination, "checkout", "-q", "--force", "--detach", commit)
        return destination

    def pull_request(self, **changes: Any) -> dict[str, Any]:
        """The ready pull request of this generation as the API reports it, with ``changes``."""

        return {"number": PULL_REQUEST, "state": "open", "draft": False, "merge_commit_sha": self.tested.sha,
                "head": {"sha": self.head.sha, "ref": HEAD_BRANCH, "repo": {"full_name": h.REPOSITORY}},
                "base": {"sha": self.controller.sha, "ref": h.BRANCH, "repo": {"full_name": h.REPOSITORY}},
                **changes}

    def github(self, *commits: Commit, max_requests: int | None = None) -> tuple[RecordingGitHub, dict[str, Any]]:
        """A fake GitHub holding ``upstream``: the default branch at the controller, the commit
        objects of the controller, the test merge and every one of ``commits``, and the pull
        request. Returns it with the seeded pull request record."""

        api = RecordingGitHub(repository=h.REPOSITORY, default_branch=h.BRANCH, max_requests=max_requests)
        for commit in (self.controller, self.tested, *commits):
            api.add_commit(commit.sha, commit.tree, parents=commit.parents)
        api.set_branch(h.BRANCH, self.controller.sha, self.controller.tree)
        pull = self.pull_request()
        h.seed_pull_request(api, pull)
        return api, pull

    def environment(self, *, event: str = "pull_request_target", caller: str = "build") -> dict[str, str]:
        """The environment of a job of this generation: the fixture's, executing the controller."""

        return {**h.environment(event=event, caller=caller), "GITHUB_SHA": self.controller.sha}


class GenerationCase(unittest.TestCase):
    """A test case with one generation for its class and, for every test, private copies of the
    two checkouts a job has: ``mod`` at the controller and ``candidate`` at the test merge."""

    generation: Generation | None = None

    @classmethod
    def setUpClass(cls) -> None:
        # A host without this Git skips every test in setUp: the kit's runner counts a class that
        # skips as a whole as a failure.
        if not installed():
            return
        directory = tempfile.TemporaryDirectory(prefix="generation ")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)
        cls.generation = Generation(root)
        cls._mod = cls.generation.checkout(root / "pristine" / "mod", cls.generation.controller.sha)
        cls._candidate = cls.generation.checkout(root / "pristine" / "candidate", cls.generation.tested.sha)

    def setUp(self) -> None:
        if self.generation is None:
            self.skipTest("git is not installed at /usr/bin/git")
        directory = tempfile.TemporaryDirectory(prefix="checkouts ")
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name)
        self.mod = copy(self._mod, self.temporary / "mod")
        self.candidate = copy(self._candidate, self.temporary / "candidate")


def invocation(mod: Path, environment: dict[str, str]) -> runtime.Invocation:
    return runtime.build_invocation(mod, None, environment)


def copy(checkout: Path, destination: Path) -> Path:
    """A private copy of a checkout, which a test may damage."""

    shutil.copytree(checkout, destination, symlinks=True)
    return destination


def commit_bytes(tree: str, parents: Sequence[str], *, trailer: str = "", message: str = "written by hand\n") -> bytes:
    """The bytes of a commit object with ``tree`` and ``parents``; ``trailer`` is appended to its
    header as it is (more header lines, each ending in a newline)."""

    lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents), f"author {SIGNATURE}",
             f"committer {SIGNATURE}"]
    return ("\n".join(lines) + "\n" + trailer + "\n" + message).encode("utf-8")


def object_name(kind: str, body: bytes) -> str:
    return hashlib.sha1(b"%s %d\0" % (kind.encode("ascii"), len(body)) + body).hexdigest()  # noqa: S324 - Git id


def write_object(checkout: Path, kind: str, body: bytes, *, name: str | None = None) -> str:
    """Store ``body`` as a loose object of ``kind`` and return the name it is stored under: its
    own, or ``name``, which makes the store lie about that object. Git prefers a pack to a loose
    object, so an object to lie about must be a loose one already."""

    path = checkout / ".git" / "objects"
    if name is None:
        name = object_name(kind, body)
    elif not (path / name[:2] / name[2:]).is_file():
        raise AssertionError(f"{name} is not a loose object of {checkout}")
    path = path / name[:2] / name[2:]
    path.parent.mkdir(exist_ok=True)
    path.unlink(missing_ok=True)
    path.write_bytes(zlib.compress(b"%s %d\0" % (kind.encode("ascii"), len(body)) + body))
    return name


def detach(checkout: Path, name: str) -> None:
    """Make ``HEAD`` of ``checkout`` name ``name`` directly, whatever kind of object that is."""

    (checkout / ".git" / "HEAD").write_bytes(name.encode("ascii") + b"\n")
