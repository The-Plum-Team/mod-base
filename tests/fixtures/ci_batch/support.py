"""A real repository behind the fake API, for the batch tests.

GitHub is one repository seen two ways: as Git and as an API. :class:`Remote` is the Git side, a
local bare repository authored with plumbing under fixed identities and dates, so every object id
is reproducible. :class:`Hub` is the API side, a ``FakeGitHub`` whose branches and refs are read
from that repository before each request that depends on them, so a push made by the code under
test is what its next API read sees, as on GitHub. :class:`World` puts both in a temporary
directory with a private state directory for the writer. Nothing the batch code owns is replaced:
its Git runs for real.

:func:`seed_gates` gives a merged batch pull request the two tested records ``settle_batch``
proves through ``transport.download_merged_gate_pair``. Its runs, artifacts, descriptors and
receipts come from the builders of ``tests/helpers.py`` and its jobs are the literal listings of
``tests/fixtures/ci_graphs``, as in the transport tests. So the producer model lives in those
builders alone: both runs are recorded under the batch pull request's head, the last squash commit
on the batch branch, and reach the controller through the guard workflow they reference.
"""

from __future__ import annotations

import copy
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mod_base.build_ci import batch
from mod_base.build_ci.batch_git import BatchRemote, open_batch_store
from mod_base.build_ci.protocol import plan_sha256, validate_plan
from mod_base.github.fake import FakeGitHub
from tests.helpers import ci_api_artifact, ci_api_run, ci_graph_jobs, ci_plan, ci_run_descriptor, ci_run_gate
from tests.test_ci_transport import IDS, WINDOWS, record_archive

REPOSITORY = "example/mod"
SEARCH_PATH = "/usr/bin:/bin:/usr/local/bin"
#: 2000-01-01T00:00:00Z, the committer time of every authored commit unless a test says otherwise.
EPOCH = 946684800
BASE_FILES = {"shared.txt": b"one\ntwo\nthree\n", "keep.txt": b"keep\n", "src/a.txt": b"a\n"}
ALLOWED = ("alpha.txt", "beta.txt", "shared.txt", "src")
AUTHOR = ("Author", "author@example.invalid")
POSIX_ONLY = unittest.skipUnless(os.name == "posix", "the protected Git writer runs on POSIX hosts only")

Files = Mapping[str, "bytes | tuple[str, bytes | str]"]


class Remote:
    """The repository on GitHub's side, as a local bare repository written with plumbing."""

    def __init__(self, root: Path) -> None:
        executable = shutil.which("git", path=SEARCH_PATH)
        assert executable is not None, f"git is not installed in {SEARCH_PATH}"
        self._executable = executable
        self.path = root / "remote.git"
        self._home = root / "author-home"
        self._index = root / "author-index"
        self._home.mkdir()
        self.git("init", "--quiet", "--bare", "--template=", "--object-format=sha1", "--initial-branch=master",
                 str(self.path), in_repository=False)

    def git(self, *arguments: str, data: bytes = b"", index: bool = False, in_repository: bool = True,
            date: int = EPOCH, identity: tuple[str, str] = AUTHOR) -> str:
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(self._home), "LC_ALL": "C", "TZ": "UTC",
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                       "GIT_AUTHOR_NAME": identity[0], "GIT_AUTHOR_EMAIL": identity[1],
                       "GIT_COMMITTER_NAME": identity[0], "GIT_COMMITTER_EMAIL": identity[1],
                       "GIT_AUTHOR_DATE": f"@{date} +0000", "GIT_COMMITTER_DATE": f"@{date} +0000"}
        if index:
            environment["GIT_INDEX_FILE"] = str(self._index)
        command = [self._executable, *([f"--git-dir={self.path}"] if in_repository else []), *arguments]
        completed = subprocess.run(command, input=data, capture_output=True, env=environment, timeout=60,
                                   cwd=self._home, check=False)
        assert completed.returncode == 0, (arguments, completed.stderr.decode("utf-8", "replace"))
        return completed.stdout.decode("utf-8").strip()

    def tree(self, files: Files) -> str:
        """A tree of ``files``: bytes are a regular file, ``(mode, bytes)`` any blob entry and
        ``("160000", commit)`` a submodule entry."""

        lines = b""
        for path, value in sorted(files.items()):
            mode, data = ("100644", value) if isinstance(value, bytes) else value
            oid = data if mode == "160000" else self.git("hash-object", "-t", "blob", "-w", "--stdin", data=data)
            lines += f"{mode} {oid}\t{path}\0".encode("utf-8")
        self._index.unlink(missing_ok=True)
        self.git("update-index", "-z", "--index-info", data=lines, index=True)
        return self.git("write-tree", index=True)

    def commit_tree(self, tree: str, *parents: str, message: str = "work", date: int = EPOCH,
                    identity: tuple[str, str] = AUTHOR) -> str:
        arguments = ["commit-tree", tree]
        for parent in parents:
            arguments += ["-p", parent]
        return self.git(*arguments, data=(message + "\n").encode("utf-8"), date=date, identity=identity)

    def commit(self, files: Files, *parents: str, message: str = "work", date: int = EPOCH) -> str:
        return self.commit_tree(self.tree(files), *parents, message=message, date=date)

    def set(self, ref: str, sha: str) -> None:
        self.git("update-ref", ref, sha)

    def rev(self, name: str) -> str | None:
        completed = subprocess.run([self._executable, f"--git-dir={self.path}", "rev-parse", "--verify", "--quiet",
                                    name], capture_output=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
                                   timeout=60, check=False)
        return completed.stdout.decode("ascii").strip() or None

    def refs(self, prefix: str = "refs/") -> dict[str, str]:
        listing = self.git("for-each-ref", "--format=%(refname) %(objectname)", prefix)
        return dict(line.split(" ") for line in listing.splitlines())

    def commit_text(self, sha: str) -> str:
        return self.git("cat-file", "commit", sha)

    def files(self, treeish: str) -> dict[str, bytes]:
        """Path -> content of every blob of ``treeish``."""

        paths = self.git("ls-tree", "-r", "--name-only", treeish).splitlines()
        return {path: subprocess.run([self._executable, f"--git-dir={self.path}", "cat-file", "blob",
                                      f"{treeish}:{path}"], capture_output=True, check=True, timeout=60,
                                     env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout for path in paths}


class Hub(FakeGitHub):
    """``FakeGitHub`` as the API face of one :class:`Remote`.

    Before every request that reads or uses a branch head (a branch, a ``heads/`` ref, any write)
    the branches and ``heads/`` refs are read from the repository. :meth:`after` runs an action
    right after the n-th request of one path, whatever its outcome: the place where a test lets
    the world change between two steps of the code under test.
    """

    def __init__(self, remote: Remote, *, writable: bool = True, max_requests: int | None = None) -> None:
        super().__init__(repository=REPOSITORY, default_branch="master", writable=writable, max_requests=max_requests)
        self.remote = remote
        self.calls: list[tuple[str, str]] = []
        self._hooks: list[list[Any]] = []

    def after(self, method: str, path: str, action: Callable[[], None], *, nth: int = 1) -> None:
        self._hooks.append([method, path, nth, action])

    def _observed(self, method: str, path: str, call: Callable[[], Any]) -> Any:
        if method != "GET" or "/branches/" in path or "/git/ref/" in path:
            listing = self.remote.git("for-each-ref", "--format=%(refname:lstrip=2) %(objectname) %(tree)",
                                      "refs/heads/")
            for name, commit, tree in (line.split(" ") for line in listing.splitlines()):
                self.add_ref(f"heads/{name}", commit)
                self.set_branch(name, commit, tree)
        self.calls.append((method, path))
        try:
            return call()
        finally:
            for hook in [hook for hook in self._hooks if hook[:2] == [method, path]]:
                hook[2] -= 1
                if hook[2] == 0:
                    self._hooks.remove(hook)
                    hook[3]()

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        return self._observed("GET", path, lambda: FakeGitHub.get_json(self, path, params=params))

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._observed("POST", path, lambda: FakeGitHub.post_json(self, path, payload))

    def patch_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._observed("PATCH", path, lambda: FakeGitHub.patch_json(self, path, payload))

    def delete(self, path: str) -> None:
        def call() -> None:
            FakeGitHub.delete(self, path)
            prefix = f"/repos/{REPOSITORY}/git/refs/"
            if path.startswith(prefix):  # the API and Git are one repository: the ref is gone in both
                self.remote.git("update-ref", "-d", "refs/" + path[len(prefix):])

        return self._observed("DELETE", path, call)

    def pull(self, number: int, branch: str, **fields: Any) -> dict[str, Any]:
        """Seed pull request ``number`` from ``branch``, its head read from ``refs/pull/<number>/head``."""

        record = {"number": number, "state": "open", "draft": True, "merged": False, "merged_at": None,
                  "merge_commit_sha": None, "title": f"fix: change {branch}", "body": "",
                  "head": {"ref": branch, "sha": self.remote.rev(f"refs/pull/{number}/head"),
                           "repo": {"full_name": REPOSITORY}},
                  "base": {"ref": "master", "sha": self.remote.rev("refs/heads/master"),
                           "repo": {"full_name": REPOSITORY}}}
        record.update(fields)
        self.add_pull(record)
        return record

    def writes(self) -> list[tuple[str, str]]:
        return [call for call in self.calls if call[0] != "GET"]


class World:
    """One repository with its API, a base commit on ``master`` and a private state directory."""

    def __init__(self, test: unittest.TestCase, *, writable: bool = True, max_requests: int | None = None,
                 base_files: Files = BASE_FILES) -> None:
        directory = tempfile.TemporaryDirectory(prefix="mb-batch-test-")
        test.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / "state"
        self.state.mkdir(mode=0o700)
        self.remote = Remote(self.root)
        self.base = self.remote.commit(base_files, message="chore: seed")
        self.remote.set("refs/heads/master", self.base)
        self.hub = Hub(self.remote, writable=writable, max_requests=max_requests)
        self.heads: dict[int, str] = {}

    @property
    def git_remote(self) -> BatchRemote:
        return BatchRemote(str(self.remote.path), "file")

    def member(self, number: int, branch: str, files: Files, *, parent: str | None = None,
               **fields: Any) -> str:
        """Open pull request ``number`` from ``branch`` whose head holds exactly ``files``."""

        head = self.remote.commit(files, self.base if parent is None else parent, message=f"work for {branch}")
        self.move(number, branch, head)
        self.hub.pull(number, branch, **fields)
        return head

    def move(self, number: int, branch: str, head: str) -> None:
        """Point the branch and the pull-request ref of ``number`` at ``head`` (Git side only)."""

        self.remote.set(f"refs/heads/{branch}", head)
        self.remote.set(f"refs/pull/{number}/head", head)
        self.heads[number] = head

    def standard(self) -> None:
        """#1 adds ``alpha.txt``, #2 extends ``shared.txt`` and adds ``beta.txt``, #3 rewrites the
        line #2 appends."""

        self.member(1, "fix/alpha", {**BASE_FILES, "alpha.txt": b"alpha\n"})
        self.member(2, "feat/beta", {**BASE_FILES, "beta.txt": b"beta\n", "shared.txt": b"one\ntwo\nthree\nfour\n"})
        self.member(3, "fix/gamma", {**BASE_FILES, "shared.txt": b"one\ntwo\nthree\nFOUR\n"})

    def store(self):  # noqa: ANN201 - the context manager of batch_git.open_batch_store
        return open_batch_store(self.state, self.git_remote)

    def prepare(self, numbers: tuple[int, ...] | list[int], *, name: str = "tested",
                allowed_paths: tuple[str, ...] = ALLOWED, dry_run: bool = False) -> dict[str, Any]:
        with self.store() as store:
            return batch.prepare_batch(self.hub, store, name=name, pr_numbers=tuple(numbers),
                                       allowed_paths=allowed_paths, dry_run=dry_run)

    def merge(self, result: Mapping[str, Any], *, parents: str = "squash") -> dict[str, Any]:
        """Merge the batch pull request of a ``prepare`` result as GitHub would, seed both gates
        and return the arguments ``settle_batch`` needs besides the API and the store."""

        manifest, number = result["manifest"], result["pr_number"]
        base, head, tree = manifest["base_sha"], manifest["members"][-1]["squash_sha"], manifest["result_tree"]
        tested = self.remote.commit_tree(tree, base, head, message=f"Merge {head} into {base}")
        final = [base, head] if parents == "merge" else [base]
        merged = self.remote.commit_tree(tree, *final, message=f"chore: batch (#{number})", date=EPOCH + 3600)
        self.remote.set("refs/heads/master", merged)
        self.remote.set(f"refs/pull/{number}/merge", tested)
        record = FakeGitHub.get_json(self.hub, f"/repos/{REPOSITORY}/pulls/{number}")
        record.update(state="closed", merged=True, merged_at="2026-10-08T10:00:00Z", merge_commit_sha=merged)
        self.hub.add_pull(record)
        self.hub.add_commit(tested, tree, parents=[base, head])
        self.hub.add_commit(merged, tree, parents=final)
        self.hub.add_compare(base, merged, {"status": "ahead", "ahead_by": len(final), "behind_by": 0})
        self.hub.add_compare(merged, merged, {"status": "identical", "ahead_by": 0, "behind_by": 0})
        plan = batch_plan(pr_number=number, head_sha=head, head_branch=manifest["branch"], base_sha=base,
                          controller_sha=base, tested_sha=tested, tested_tree=tree, tested_parents=[base, head])
        build_seal, packaged_seal = seed_gates(self.hub, plan)
        return {"pr_number": number, "plan": plan, "build_seal": build_seal, "packaged_seal": packaged_seal,
                "temporary_root": self.state}

    def settle(self, arguments: Mapping[str, Any], **options: Any) -> dict[str, list[int]]:
        with self.store() as store:
            return batch.settle_batch(self.hub, store, **{**arguments, **options})


def batch_plan(**identity: Any) -> dict[str, Any]:
    """The synthetic protocol plan of ``tests.helpers.ci_plan`` bound to another identity."""

    plan = ci_plan()
    plan["identity"].update(identity)
    plan["plan_sha256"] = plan_sha256(plan)
    return validate_plan(plan)


def seed_gates(hub: FakeGitHub, plan: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seed the finished Build run and packaged run of the pull request ``plan`` names, each with
    its literal job listing and its artifacts, seal both tested records and return their
    descriptors (Build, packaged)."""

    def publish(producer: str, key: str, data: bytes, unit_id: str | None = None) -> dict[str, Any]:
        """One artifact of the run of ``producer``, uploaded in the window the listing shows for
        ``key``, and the descriptor that selects it."""

        kind, _, gate = key.partition("-")
        descriptor = ci_run_descriptor(plan, producer, None, kind, unit_id=gate or unit_id, artifact_id=IDS[key])
        started, completed, created = WINDOWS[key]
        descriptor["producer"]["upload_window"] = {"started_at": started, "completed_at": completed}
        descriptor["artifact"].update(created_at=created, size=len(data),
                                      digest="sha256:" + hashlib.sha256(data).hexdigest())
        hub.add_artifact(ci_api_artifact(descriptor), data)
        return descriptor

    for producer, listing in (("build", "build-full"), ("packaged", "packaged-pull-request")):
        run = ci_api_run(plan, producer)
        hub.add_run(run)
        hub.add_jobs(run["id"], run["run_attempt"], ci_graph_jobs(listing))
    (lane,) = plan["lanes"]  # the literal packaged listing runs one lane
    # The settlement reads the metadata of these three and never their bytes.
    bundle = publish("build", "build", b"complete Build bundle")
    build = ci_run_gate(plan, "build", "full")
    build["artifacts"] = [copy.deepcopy(bundle)]
    packaged = ci_run_gate(plan, "packaged", "pull-request")
    packaged["artifacts"] = [publish("packaged", "runtime", b"runtime evidence of one lane", lane["id"]),
                             publish("packaged", "results", b"complete packaged results")]
    packaged["owning_build"] = copy.deepcopy(bundle)
    return (publish("build", "tested-build", record_archive(build)),
            publish("packaged", "tested-packaged", record_archive(packaged)))
