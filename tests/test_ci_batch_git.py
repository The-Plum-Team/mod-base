"""The protected Git writer against real repositories: no part of it is replaced.

Every repository is authored with plumbing under fixed identities and dates
(``tests/fixtures/ci_batch/support.py``), so the ids asserted here are stable.
"""

from __future__ import annotations

import base64
import contextlib
import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from mod_base.build_ci import batch_git
from mod_base.build_ci.batch_git import (BOT_EMAIL, BOT_NAME, BatchGitError, BatchRemote, StackMember,
                                         allowed_path_roots, empty_batch_branch_lease, github_remote,
                                         open_batch_store, patch_sha256, supported_git_version,
                                         validate_batch_push_receipt)
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from tests.fixtures.ci_batch.support import ALLOWED, BASE_FILES, EPOCH, POSIX_ONLY, World
from tests.helpers import ci_batch

IDENTITY = f"{BOT_NAME} <{BOT_EMAIL}> {EPOCH} +0000"


def members(world: World, *numbers: int) -> tuple[StackMember, ...]:
    return tuple(StackMember(number, f"fix: change {number}", world.heads[number]) for number in numbers)


def build(world: World, stack_members: tuple[StackMember, ...], *, allowed: tuple[str, ...] | None = ALLOWED,
          publish: str | None = None, live: bool = True, base: str | None = None):
    """Fetch, build and optionally publish; the store is gone when this returns."""

    with world.store() as store:
        store.fetch(base_sha=base or world.base, base_branch="master" if live else None,
                    heads={member.number: member.head_sha for member in stack_members})
        stack = store.build_stack(base_sha=base or world.base, base_branch="master", members=stack_members,
                                  allowed_paths=allowed)
        if publish is not None:
            store.publish(branch=publish, commit_sha=stack.commits[-1].squash_sha)
        return stack


class PureRuleTests(unittest.TestCase):
    def test_only_git_2_40_or_later_is_supported(self):
        for line, version in ((b"git version 2.40.0\n", (2, 40)), (b"git version 2.43.0\n", (2, 43)),
                              (b"git version 2.54.0.windows.1\n", (2, 54)), (b"git version 3.0.1\n", (3, 0)),
                              (b"git version 2.45.2 (Apple Git-146)\n", (2, 45)), (b"git version 2.40", (2, 40))):
            self.assertEqual(supported_git_version(line), version)
        for line in (b"git version 2.39.5\n", b"git version 2.39.3 (Apple Git-146)\n", b"git version 1.9.1\n"):
            with self.subTest(line=line), self.assertRaisesRegex(BatchGitError, "needs Git 2.40 or later"):
                supported_git_version(line)
        for line in (b"", b"git version\n", b"hg version 2.43.0\n", b"git version 2.43.0\nextra\n",
                     b"git version 2.x\n", "git version 2.43.0\n", None, b"git version 2.43.0" + b"x" * 200):
            with self.subTest(line=line), self.assertRaisesRegex(BatchGitError, "cannot read the version"):
                supported_git_version(line)

    def test_a_remote_is_one_validated_url_and_transport(self):
        remote = github_remote("example/mod", "t0ken")
        self.assertEqual((remote.url, remote.transport), ("https://github.com/example/mod.git", "https"))
        self.assertEqual(BatchRemote("/srv/mirror/mod.git", "file").environment(), {"GIT_ALLOW_PROTOCOL": "file"})
        for url, transport, token in (
                ("https://github.com/example/mod", "https", None), ("https://github.com/example.git", "https", None),
                ("https://evil.test/example/mod.git", "https", None), ("https://github.com/a/b/c.git", "https", None),
                ("https://t0ken@github.com/example/mod.git", "https", None),
                ("http://github.com/example/mod.git", "https", None), ("git@github.com:example/mod.git", "ssh", None),
                ("https://github.com/example/mod.git", "https", ""), ("https://github.com/example/mod.git", "https", "a b"),
                ("https://github.com/example/mod.git", "https", "café"), ("relative/mod.git", "file", None),
                ("/srv/mod.git", "file", "t0ken"), ("/srv/with space/mod.git", "file", None),
                ("/srv/mod.git", "ext", None), (None, "https", None), ("/srv/mod.git", None, None)):
            with self.subTest(url=url, transport=transport, token=token), self.assertRaises(BatchGitError):
                BatchRemote(url, transport, token)
        with self.assertRaises(MbError):
            github_remote("not a repository", None)

    def test_a_token_is_a_header_for_github_only_and_never_part_of_the_url(self):
        remote = github_remote("example/mod", "t0ken")
        environment = remote.environment()
        self.assertEqual(environment, {
            "GIT_ALLOW_PROTOCOL": "https", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:t0ken").decode()})
        self.assertNotIn("t0ken", remote.url)
        self.assertEqual(github_remote("example/mod", None).environment(), {"GIT_ALLOW_PROTOCOL": "https"})

    def test_allowed_paths_are_a_canonical_ascending_tuple(self):
        self.assertEqual(allowed_path_roots(("README.md", "docs", "src/main")), {"README.md", "docs", "src/main"})
        for value in ((), ["src"], ("src", "src"), ("src", "docs"), ("../x",), (".git/config",), ("a//b",),
                      ("/abs",), (b"src",), (None,), ("src/",), tuple(f"p{index:05d}" for index in range(
                          limits.MAX_CI_BATCH_ALLOWED_PATHS + 1))):
            with self.subTest(value=value[:3]), self.assertRaises(MbError):
                allowed_path_roots(value)

    def test_empty_lease_argument_and_branch_grammar(self):
        for branch in ("batch/x", "batch/-x", "batch/a./b", "batch/a.LOCK", "batch/" + "a" * 194):
            self.assertEqual(empty_batch_branch_lease(branch), "--force-with-lease=refs/heads/" + branch + ":")
        for branch in ("batch/", "master", "batch/a/", "batch/a.", "batch/.hidden", "batch/a.lock",
                       "batch/a.lock/b", "batch/a..b", "batch/a//b", "batch/a:b", None, True):
            with self.subTest(branch=branch), self.assertRaises(MbError):
                empty_batch_branch_lease(branch)

    def test_push_receipt_requires_exactly_one_new_branch(self):
        branch, commit, remote = "batch/fixture", "f" * 40, "https://github.com/example/mod.git"
        raw = f"To {remote}\n*\t{commit}:refs/heads/{branch}\t[new branch]\nDone\n".encode()
        for data in (raw, raw.replace(b"\n", b"\r\n")):
            validate_batch_push_receipt(data, exit_code=0, remote=remote, branch=branch, commit_sha=commit)
        for data in (b"", raw[:-1], raw + b"Done\n", raw.replace(b"*\t", b"=\t"),
                     raw.replace(b"[new branch]", b"[up to date]"), raw.replace(b"Done", b"unknown"),
                     raw.replace(b"example/mod", b"foreign/mod"), raw.replace(b"f" * 40, b"e" * 40),
                     raw.replace(b"batch/fixture", b"batch/other"), raw.replace(b"\n", b"\r"),
                     raw + b"\xff", raw + b"\x00", b"x" * (limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES + 1)):
            with self.subTest(data=data[:40]), self.assertRaises(MbError):
                validate_batch_push_receipt(data, exit_code=0, remote=remote, branch=branch, commit_sha=commit)
        for status in (False, 1, -1, "0"):
            with self.subTest(status=status), self.assertRaises(MbError):
                validate_batch_push_receipt(raw, exit_code=status, remote=remote, branch=branch, commit_sha=commit)


@POSIX_ONLY
class StoreTests(unittest.TestCase):
    def test_the_store_is_private_holds_no_hook_or_attribute_file_and_is_removed(self):
        world = World(self)
        with world.store():
            (root,) = list(world.state.iterdir())
            self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["home", "store.git"])
            self.assertEqual(sorted(path.name for path in (root / "store.git").iterdir()),
                             ["HEAD", "config", "objects", "refs"])
            self.assertEqual(list((root / "home").iterdir()), [])
        self.assertEqual(list(world.state.iterdir()), [])

    def test_the_state_directory_must_be_a_real_private_directory(self):
        world = World(self)
        shared = world.root / "shared"
        shared.mkdir(mode=0o700)
        shared.chmod(0o777)
        link = world.root / "link"
        link.symlink_to(world.state)
        regular = world.root / "file"
        regular.write_bytes(b"x")
        for parent in (shared, link, regular, world.root / "missing"):
            with self.subTest(parent=parent.name), self.assertRaises(BatchGitError):
                with open_batch_store(parent, world.git_remote):
                    self.fail("a store was opened")
        for parent, remote in ((world.state, None), (str(world.state), world.git_remote), (None, world.git_remote)):
            with self.subTest(parent=parent, remote=remote), self.assertRaises(BatchGitError):
                with open_batch_store(parent, remote):
                    self.fail("a store was opened")
        self.assertEqual(list(world.state.iterdir()), [])

    def test_a_relative_state_directory_works_from_any_working_directory(self):
        world = World(self)
        world.standard()
        with contextlib.chdir(world.root), open_batch_store(Path("state"), world.git_remote) as store:
            store.fetch(base_sha=world.base, base_branch="master", heads={1: world.heads[1]})
            stack = store.build_stack(base_sha=world.base, base_branch="master", members=members(world, 1),
                                      allowed_paths=ALLOWED)
            store.publish(branch="batch/relative", commit_sha=stack.commits[-1].squash_sha)
        self.assertEqual(world.remote.rev("refs/heads/batch/relative"), stack.commits[-1].squash_sha)
        self.assertEqual(list(world.state.iterdir()), [])

    def test_git_resolves_the_token_header_for_the_github_origin_only(self):
        world = World(self)
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(world.root), "GIT_CONFIG_NOSYSTEM": "1",
                       "GIT_CONFIG_GLOBAL": os.devnull, **github_remote("example/mod", "t0ken").environment()}

        def header(url: str) -> str:
            return subprocess.run(["git", "config", "--get-urlmatch", "http.extraheader", url], env=environment,
                                  capture_output=True, cwd=world.root, timeout=30).stdout.decode().strip()

        expected = "AUTHORIZATION: basic " + base64.b64encode(b"x-access-token:t0ken").decode()
        self.assertEqual(header("https://github.com/example/mod.git"), expected)
        for url in ("https://evil.test/example/mod.git", "https://github.com.evil.test/example/mod.git",
                    "http://github.com/example/mod.git", "https://api.github.com/"):
            self.assertEqual(header(url), "")


@POSIX_ONLY
class FetchTests(unittest.TestCase):
    def test_live_refs_must_be_the_commits_the_api_reported(self):
        world = World(self)
        world.standard()
        heads = {1: world.heads[1], 2: world.heads[2]}
        with world.store() as store:
            store.fetch(base_sha=world.base, base_branch="master", heads=heads)
        later = world.remote.commit({**BASE_FILES, "alpha.txt": b"later\n"}, world.heads[1])
        world.remote.set("refs/pull/1/head", later)
        with world.store() as store, self.assertRaisesRegex(BatchGitError, "#1 moved while the batch was being"):
            store.fetch(base_sha=world.base, base_branch="master", heads=heads)
        world.remote.set("refs/pull/1/head", world.heads[1])
        world.remote.set("refs/heads/master", world.remote.commit({**BASE_FILES, "keep.txt": b"kept\n"}, world.base))
        with world.store() as store, self.assertRaisesRegex(BatchGitError, "master moved while the batch was being"):
            store.fetch(base_sha=world.base, base_branch="master", heads=heads)
        with world.store() as store, self.assertRaisesRegex(BatchGitError, "git fetch failed"):
            store.fetch(base_sha=world.base, base_branch="master", heads={9: world.heads[1]})

    def test_exact_commits_are_fetched_by_id_after_their_refs_moved_on(self):
        world = World(self)
        world.standard()
        batched = world.heads[1]
        # A forced push: no ref reaches the batched head any more.
        world.move(1, "fix/alpha", world.remote.commit({**BASE_FILES, "alpha.txt": b"later\n"}, world.base))
        world.remote.set("refs/heads/master", world.remote.commit({**BASE_FILES, "keep.txt": b"kept\n"}, world.base))
        stack = build(world, (StackMember(1, "fix: alpha", batched),), live=False)
        self.assertEqual(stack.base_sha, world.base)
        with world.store() as store, self.assertRaisesRegex(BatchGitError, "git fetch failed"):
            store.fetch(base_sha=world.base, heads={1: "a" * 40})
        with world.store() as store, self.assertRaises(MbError):
            store.fetch(base_sha=world.base, heads={})
        with world.store() as store, self.assertRaises(MbError):
            store.fetch(base_sha=world.base, heads={number: batched for number in range(1, 52)})


@POSIX_ONLY
class StackTests(unittest.TestCase):
    def test_two_members_stack_as_bot_commits_with_exact_parents_trees_and_messages(self):
        world = World(self)
        world.standard()
        stack = build(world, members(world, 2, 1), publish="batch/stack")
        first, second = stack.commits
        self.assertEqual((stack.base_sha, stack.base_tree), (world.base, world.remote.rev(world.base + "^{tree}")))
        self.assertEqual(world.remote.rev("refs/heads/batch/stack"), second.squash_sha)
        self.assertEqual(world.remote.commit_text(first.squash_sha), (
            f"tree {first.result_tree}\nparent {world.base}\nauthor {IDENTITY}\ncommitter {IDENTITY}\n\n"
            f"fix: change 2 (#2)\n\nBatch-Member: 2 {world.heads[2]}"))
        self.assertEqual(world.remote.commit_text(second.squash_sha), (
            f"tree {second.result_tree}\nparent {first.squash_sha}\nauthor {IDENTITY}\ncommitter {IDENTITY}\n\n"
            f"fix: change 1 (#1)\n\nBatch-Member: 1 {world.heads[1]}"))
        self.assertEqual(world.remote.files(first.result_tree),
                         {**BASE_FILES, "beta.txt": b"beta\n", "shared.txt": b"one\ntwo\nthree\nfour\n"})
        self.assertEqual(world.remote.files(second.result_tree), {
            **BASE_FILES, "alpha.txt": b"alpha\n", "beta.txt": b"beta\n", "shared.txt": b"one\ntwo\nthree\nfour\n"})
        self.assertEqual([entry.path for entry in first.patch], ["beta.txt", "shared.txt"])
        self.assertEqual((first.patch[0].before, first.patch[0].after[0]), (None, "100644"))
        self.assertEqual((first.merge_base_sha, first.head_tree),
                         (world.base, world.remote.rev(world.heads[2] + "^{tree}")))
        self.assertEqual(len(patch_sha256(first.patch)), 64)
        self.assertNotEqual(patch_sha256(first.patch), patch_sha256(second.patch))

    def test_equal_inputs_give_equal_ids_and_any_other_input_gives_other_ids(self):
        world = World(self)
        world.standard()
        original = build(world, members(world, 1, 2))
        self.assertEqual(build(world, members(world, 1, 2)), original)
        self.assertEqual(build(world, members(world, 1, 2), allowed=None, live=False), original)
        ids = {original.commits[-1].squash_sha}
        for changed in (members(world, 2, 1),
                        (StackMember(1, "another title", world.heads[1]), *members(world, 2)),
                        (StackMember(7, "fix: change 1", world.heads[1]), *members(world, 2))):
            if changed[0].number == 7:
                world.remote.set("refs/pull/7/head", world.heads[1])
            ids.add(build(world, changed).commits[-1].squash_sha)
        self.assertEqual(len(ids), 4)
        other = World(self)
        other.remote.set("refs/heads/master", other.remote.commit(BASE_FILES, message="chore: seed", date=EPOCH + 1))
        other.base = other.remote.rev("refs/heads/master")
        other.standard()
        self.assertNotIn(build(other, members(other, 1, 2)).commits[-1].squash_sha, ids)

    def test_the_committed_document_fixture_is_this_stack(self):
        # tests/helpers.ci_batch() is the valid mod-base.ci.batch fixture: a state Git produces.
        world = World(self)
        world.standard()
        fixture = ci_batch()
        stack = build(world, tuple(StackMember(member["pr_number"], member["title"], world.heads[member["pr_number"]])
                                   for member in fixture["members"]))
        self.assertEqual((fixture["base_sha"], fixture["base_tree"]), (stack.base_sha, stack.base_tree))
        self.assertEqual([{"pr_number": commit.member.number, "title": commit.member.title,
                           "head_sha": commit.member.head_sha, "head_tree": commit.head_tree,
                           "merge_base_sha": commit.merge_base_sha, "patch_sha256": patch_sha256(commit.patch),
                           "squash_sha": commit.squash_sha, "result_tree": commit.result_tree}
                          for commit in stack.commits], fixture["members"])
        self.assertEqual(fixture["result_tree"], stack.commits[-1].result_tree)

    def test_a_member_is_applied_as_its_own_patch_from_its_merge_base(self):
        world = World(self)
        old = world.base
        world.member(1, "fix/old", {**BASE_FILES, "alpha.txt": b"alpha\n"})
        newer = world.remote.commit({**BASE_FILES, "keep.txt": b"kept later\n"}, old, message="chore: later")
        world.remote.set("refs/heads/master", newer)
        world.base = newer
        stack = build(world, members(world, 1))
        self.assertEqual(stack.commits[0].merge_base_sha, old)
        self.assertEqual([entry.path for entry in stack.commits[0].patch], ["alpha.txt"])
        build(world, members(world, 1), publish="batch/old")
        self.assertEqual(world.remote.files("refs/heads/batch/old"),
                         {**BASE_FILES, "keep.txt": b"kept later\n", "alpha.txt": b"alpha\n"})

    def test_members_changing_different_parts_of_one_file_merge(self):
        world = World(self, base_files={**BASE_FILES, "src/a.txt": b"1\n2\n3\n4\n5\n6\n7\n8\n9\n"})
        files = {**BASE_FILES, "src/a.txt": b"1\n2\n3\n4\n5\n6\n7\n8\n9\n"}
        world.member(1, "fix/top", {**files, "src/a.txt": b"one\n2\n3\n4\n5\n6\n7\n8\n9\n"})
        world.member(2, "fix/bottom", {**files, "src/a.txt": b"1\n2\n3\n4\n5\n6\n7\n8\nnine\n"})
        build(world, members(world, 1, 2), publish="batch/both")
        self.assertEqual(world.remote.files("refs/heads/batch/both")["src/a.txt"], b"one\n2\n3\n4\n5\n6\n7\n8\nnine\n")

    def test_a_same_line_conflict_names_the_pull_request_and_the_path(self):
        world = World(self)
        world.standard()
        with self.assertRaisesRegex(BatchGitError, r"#3 conflicts with master or an earlier batch entry: shared\.txt$"):
            build(world, members(world, 2, 3), publish="batch/conflict")
        self.assertIsNone(world.remote.rev("refs/heads/batch/conflict"))
        build(world, members(world, 3, 1))

    def test_a_member_that_adds_nothing_to_the_stack_is_refused(self):
        world = World(self)
        world.standard()
        world.move(4, "fix/nothing", world.base)
        world.member(5, "fix/same", {**BASE_FILES, "alpha.txt": b"alpha\n"})
        merged = world.remote.commit({**BASE_FILES, "alpha.txt": b"alpha\n"}, world.base, world.heads[1])
        world.move(6, "fix/merged", merged)
        for numbers, refused in (((4,), 4), ((1, 4), 4), ((1, 5), 5), ((5, 1), 1), ((1, 6), 6)):
            with self.subTest(numbers=numbers), self.assertRaisesRegex(
                    BatchGitError, f"#{refused} adds nothing on top of master and earlier batch entries"):
                build(world, members(world, *numbers))

    def test_duplicate_numbers_and_more_than_fifty_members_stop_before_git(self):
        world = World(self)
        world.standard()
        one = members(world, 1)
        with world.store() as store:
            store.fetch(base_sha=world.base, base_branch="master", heads={1: world.heads[1]})
            for stack_members in (one + one, (), list(one), tuple(StackMember(number, "t", world.heads[1])
                                                                 for number in range(1, 52)),
                                  (StackMember(0, "t", world.heads[1]),), (StackMember(1, "", world.heads[1]),),
                                  (StackMember(1, "two\nlines", world.heads[1]),),
                                  (StackMember(1, "t" * 257, world.heads[1]),), (StackMember(1, "t", "f" * 39),),
                                  ((1, "t", world.heads[1]),)):
                with self.subTest(members=len(stack_members)), patch.object(
                        subprocess, "run", side_effect=AssertionError("Git ran")), self.assertRaises(MbError):
                    store.build_stack(base_sha=world.base, base_branch="master", members=stack_members,
                                      allowed_paths=ALLOWED)
            self.assertEqual(len(store.build_stack(base_sha=world.base, base_branch="master", members=one,
                                                   allowed_paths=ALLOWED).commits), 1)

    def test_a_deletion_that_meets_another_change_is_refused_whatever_rename_detection_says(self):
        body = b"".join(b"line %d\n" % index for index in range(40))
        files = {**BASE_FILES, "src/a.txt": body}
        world = World(self, base_files=files)
        world.member(1, "fix/modify", {**files, "src/a.txt": body + b"modified\n"})
        renamed = {key: value for key, value in files.items() if key != "src/a.txt"}
        world.member(2, "fix/rename", {**renamed, "src/b.txt": body})
        world.member(3, "fix/rename-too", {**renamed, "src/c.txt": body})
        world.member(4, "fix/delete", renamed)
        for numbers, refused in (((1, 2), 2), ((2, 1), 1), ((2, 3), 3), ((4, 2), 2), ((1, 4), 4)):
            with self.subTest(numbers=numbers), self.assertRaisesRegex(
                    BatchGitError, rf"#{refused} conflicts with master or an earlier batch entry: src/a\.txt$"):
                build(world, members(world, *numbers))
        # A rename alone is a deletion and an addition of the member's own: nothing else touched them.
        build(world, members(world, 2), publish="batch/rename")
        self.assertEqual(sorted(world.remote.files("refs/heads/batch/rename")), ["keep.txt", "shared.txt", "src/b.txt"])

    def test_a_renamed_directory_and_a_file_that_meets_a_directory_are_conflicts(self):
        def body(name: bytes) -> bytes:
            return b"".join(b"%s line %d\n" % (name, index) for index in range(30))

        files = {"keep.txt": b"keep\n", "old/a.txt": body(b"a"), "old/b.txt": body(b"b")}
        world = World(self, base_files=files)
        world.member(1, "fix/rename", {"keep.txt": b"keep\n", "new/a.txt": body(b"a"), "new/b.txt": body(b"b")})
        world.member(2, "fix/add", {**files, "old/c.txt": b"c\n"})
        world.member(3, "fix/file", {**files, "x": b"file\n"})
        world.member(4, "fix/directory", {**files, "x/y.txt": b"directory\n"})
        for numbers, refused in (((1, 2), 2), ((2, 1), 1), ((3, 4), 4), ((4, 3), 3)):
            with self.subTest(numbers=numbers), self.assertRaisesRegex(
                    BatchGitError, f"#{refused} conflicts with master or an earlier batch entry: "):
                build(world, members(world, *numbers), allowed=None)
        self.assertEqual(len(build(world, members(world, 1, 3), allowed=None).commits), 2)

    def test_only_allowed_paths_may_change(self):
        world = World(self)
        world.member(1, "fix/workflow", {**BASE_FILES, ".github/workflows/ci.yml": b"on: push\n"})
        world.member(2, "fix/nested", {**BASE_FILES, "src/deep/er/file.txt": b"x\n", "alpha.txt": b"a\n"})
        world.member(3, "fix/sibling", {**BASE_FILES, "src2/file.txt": b"x\n"})
        world.member(4, "fix/delete", {key: value for key, value in BASE_FILES.items() if key != "keep.txt"})
        with self.assertRaisesRegex(BatchGitError, r"#1 changes a path the protected policy does not allow in a "
                                                   r"batch: \.github/workflows/ci\.yml$"):
            build(world, members(world, 1))
        build(world, members(world, 1), allowed=(".github",))
        build(world, members(world, 1), allowed=(".github/workflows/ci.yml",))
        build(world, members(world, 2))
        with self.assertRaisesRegex(BatchGitError, "#2 changes a path .*: alpha.txt$"):
            build(world, members(world, 2), allowed=("src",))
        with self.assertRaisesRegex(BatchGitError, "#2 changes a path .*: src/deep/er/file.txt$"):
            build(world, members(world, 2), allowed=("alpha.txt", "src/deep/er/file.txt.orig", "src/deeper"))
        with self.assertRaisesRegex(BatchGitError, "#3 changes a path .*: src2/file.txt$"):
            build(world, members(world, 3))
        with self.assertRaisesRegex(BatchGitError, "#4 changes a path .*: keep.txt$"):
            build(world, members(world, 4))
        # A rebuild of an already merged batch applies no path policy.
        self.assertEqual(len(build(world, members(world, 1, 3, 4), allowed=None).commits), 3)

    def test_submodule_entries_odd_paths_and_modes_are_refused(self):
        world = World(self)
        world.member(1, "fix/submodule", {**BASE_FILES, "src/vendor": ("160000", "1" * 40)})
        world.member(2, "fix/space", {**BASE_FILES, "src/we ird.txt": b"x\n"})
        world.member(3, "fix/link", {**BASE_FILES, "src/link": ("120000", b"a.txt")})
        world.member(4, "fix/executable", {**BASE_FILES, "src/a.txt": ("100755", b"a\n")})
        with self.assertRaisesRegex(BatchGitError, "#1 changes a submodule entry: src/vendor$"):
            build(world, members(world, 1))
        with self.assertRaisesRegex(BatchGitError, "#1 changes a submodule entry"):
            build(world, members(world, 1), allowed=None)
        with self.assertRaisesRegex(BatchGitError, "#2 changes a path outside the repository path grammar: src/we ird"):
            build(world, members(world, 2))
        build(world, members(world, 3, 4), publish="batch/modes")
        listing = world.remote.git("ls-tree", "-r", "refs/heads/batch/modes", "src")
        self.assertIn("120000 blob", listing)
        self.assertIn("100755 blob", listing)

    def test_a_tree_with_a_git_directory_is_never_fetched(self):
        # Only mktree writes such a tree; the fetch checks every object (fetch.fsckObjects).
        world = World(self)
        blob = world.remote.git("hash-object", "-t", "blob", "-w", "--stdin", data=b"[core]\n")
        inner = world.remote.git("mktree", "-z", data=f"100644 blob {blob}\tconfig\0".encode())
        entries = world.remote.git("ls-tree", "-z", world.base).encode() + f"040000 tree {inner}\t.GIT\0".encode()
        world.move(1, "fix/dotgit", world.remote.commit_tree(world.remote.git("mktree", "-z", data=entries), world.base))
        with world.store() as store, self.assertRaisesRegex(BatchGitError, "git fetch failed"):
            store.fetch(base_sha=world.base, base_branch="master", heads={1: world.heads[1]})

    def test_members_without_exactly_one_merge_base_are_refused(self):
        world = World(self)
        unrelated = world.remote.commit({"other.txt": b"x\n"}, message="unrelated root")
        world.move(1, "fix/unrelated", unrelated)
        left = world.remote.commit({**BASE_FILES, "alpha.txt": b"l\n"}, world.base)
        right = world.remote.commit({**BASE_FILES, "beta.txt": b"r\n"}, world.base)
        both = {**BASE_FILES, "alpha.txt": b"l\n", "beta.txt": b"r\n"}
        world.remote.set("refs/heads/master", world.remote.commit(both, left, right))
        world.base = world.remote.rev("refs/heads/master")
        world.move(2, "fix/crossed", world.remote.commit({**both, "src/a.txt": b"crossed\n"}, right, left))
        for number in (1, 2):
            with self.subTest(number=number), self.assertRaisesRegex(
                    BatchGitError, f"#{number} needs exactly one merge base with master; rebase it"):
                build(world, members(world, number))

    def test_attributes_shipped_by_a_member_select_no_merge_driver(self):
        world = World(self)
        world.standard()
        world.member(4, "fix/union", {**BASE_FILES, "shared.txt": b"one\ntwo\nthree\nFIVE\n",
                                      ".gitattributes": b"shared.txt merge=union\n* filter=evil\n"})
        with self.assertRaisesRegex(BatchGitError, "#4 conflicts with master or an earlier batch entry: shared.txt"):
            build(world, members(world, 2, 4), allowed=(".gitattributes", *ALLOWED))

    def test_titles_reach_the_commit_subject_verbatim(self):
        world = World(self)
        world.standard()
        title = 'feat: "café" <b> | \U0001f600 --> %s'
        build(world, (StackMember(1, title, world.heads[1]),), publish="batch/title")
        self.assertEqual(world.remote.git("log", "-1", "--format=%s", "refs/heads/batch/title"), f"{title} (#1)")


@POSIX_ONLY
class PublishTests(unittest.TestCase):
    def test_a_batch_branch_is_created_once_and_never_replaced_or_adopted(self):
        world = World(self)
        world.standard()
        with world.store() as store:
            store.fetch(base_sha=world.base, base_branch="master", heads={1: world.heads[1], 2: world.heads[2]})
            stack = store.build_stack(base_sha=world.base, base_branch="master", members=members(world, 1, 2),
                                      allowed_paths=ALLOWED)
            first, second = (commit.squash_sha for commit in stack.commits)
            store.publish(branch="batch/x", commit_sha=first)
            self.assertEqual(world.remote.rev("refs/heads/batch/x"), first)
            # The same id again is "up to date" for Git: that branch was not created by this push.
            with self.assertRaisesRegex(MbError, "push did not create the exact new batch branch"):
                store.publish(branch="batch/x", commit_sha=first)
            # Another id, even a fast-forward, never moves an existing branch.
            with self.assertRaisesRegex(BatchGitError, "could not create batch/x: it already exists"):
                store.publish(branch="batch/x", commit_sha=second)
            with self.assertRaisesRegex(BatchGitError, "could not create batch/x/child"):
                store.publish(branch="batch/x/child", commit_sha=second)
            for branch in ("master", "fix/alpha", "batch/", "batch/a.lock"):
                with self.subTest(branch=branch), self.assertRaises(MbError):
                    store.publish(branch=branch, commit_sha=second)
        self.assertEqual({ref: sha for ref, sha in world.remote.refs("refs/heads/").items()
                          if ref.startswith("refs/heads/batch/") or ref == "refs/heads/master"},
                         {"refs/heads/batch/x": first, "refs/heads/master": world.base})
        for accepted in ("batch/x", "batch/-x", "batch/a./b", "batch/a.LOCK"):
            self.assertTrue(grammar.is_batch_branch(accepted))
            world.remote.git("check-ref-format", "refs/heads/" + accepted)


@POSIX_ONLY
class HostileAmbientTests(unittest.TestCase):
    """Whatever Git configuration surrounds the process, the writer neither uses nor touches it."""

    def hostile(self, world: World) -> tuple[dict[str, str], Path, Path]:
        root = world.root / "hostile"
        traces = root / "traces"
        traces.mkdir(parents=True)

        def script(name: str, body: str = "") -> Path:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"#!/bin/sh\ntouch '{traces}/{name.replace('/', '-')}'\n{body}", encoding="utf-8", newline="\n")
            path.chmod(0o755)
            return path

        for hook in ("pre-push", "reference-transaction", "post-update", "pre-receive", "update", "pre-commit",
                     "pre-merge-commit", "post-merge", "post-checkout", "pre-auto-gc"):
            script(f"hooks/{hook}")
            script(f"template/hooks/{hook}")
        (root / "template" / "info").mkdir()
        attributes = "* merge=evil filter=evil diff=evil\n"
        (root / "template" / "info" / "attributes").write_text(attributes, encoding="utf-8", newline="\n")
        (root / "attributes").write_text(attributes, encoding="utf-8", newline="\n")
        stray = root / "stray.git"
        world.remote.git("init", "--quiet", "--bare", "--template=", str(stray), in_repository=False)
        config = root / "gitconfig"
        config.write_text(
            f"[core]\n\thooksPath = {root}/hooks\n\tattributesFile = {root}/attributes\n"
            f"\tfsmonitor = {script('fsmonitor')}\n\tsshCommand = {script('ssh')}\n"
            f"[merge]\n\tdefault = evil\n\trenames = false\n"
            f"[merge \"evil\"]\n\tname = evil\n\tdriver = {script('merge-driver', 'exit 0')} %A\n"
            f"[filter \"evil\"]\n\tclean = {script('filter-clean', 'cat')}\n\tsmudge = {script('filter-smudge', 'cat')}\n"
            f"\trequired = true\n"
            f"[diff]\n\texternal = {script('diff-external')}\n[diff \"evil\"]\n\tcommand = {script('diff-driver')}\n"
            f"[user]\n\tname = Mallory\n\temail = mallory@example.invalid\n"
            f"[commit]\n\tgpgSign = true\n[gpg]\n\tprogram = {script('gpg', 'exit 1')}\n"
            f"[credential]\n\thelper = {script('credential')}\n"
            f"[init]\n\ttemplateDir = {root}/template\n\tdefaultObjectFormat = sha256\n"
            f"[url \"{stray}\"]\n\tinsteadOf = {world.remote.path}\n"
            f"[i18n]\n\tcommitEncoding = ISO-8859-1\n[fetch]\n\tfsckObjects = false\n"
            f"[transfer]\n\thideRefs = refs/pull\n[gc]\n\tauto = 1\n",
            encoding="utf-8", newline="\n")
        (root / "xdg" / "git").mkdir(parents=True)
        (root / "xdg" / "git" / "config").write_bytes(config.read_bytes())
        (root / "xdg" / "git" / "attributes").write_text(attributes, encoding="utf-8", newline="\n")
        (root / ".gitconfig").write_bytes(config.read_bytes())
        script("bin/git", 'exec /usr/bin/git "$@"\n')
        environment = {
            "HOME": str(root), "XDG_CONFIG_HOME": str(root / "xdg"), "PATH": f"{root}/bin:/usr/bin:/bin",
            "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": str(config), "GIT_CONFIG_NOSYSTEM": "0",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": f"{root}/hooks",
            "GIT_DIR": str(stray), "GIT_WORK_TREE": str(root), "GIT_INDEX_FILE": str(root / "stray-index"),
            "GIT_OBJECT_DIRECTORY": str(stray / "objects"), "GIT_TEMPLATE_DIR": str(root / "template"),
            "GIT_EXTERNAL_DIFF": str(script("diff-environment")), "GIT_SSH_COMMAND": str(script("ssh-environment")),
            "GIT_ASKPASS": str(script("askpass")), "GIT_EDITOR": str(script("editor")),
            "GIT_AUTHOR_NAME": "Mallory", "GIT_AUTHOR_EMAIL": "mallory@example.invalid",
            "GIT_COMMITTER_NAME": "Mallory", "GIT_COMMITTER_EMAIL": "mallory@example.invalid",
            "GIT_AUTHOR_DATE": "@1 +0000", "GIT_COMMITTER_DATE": "@1 +0000", "GIT_ALLOW_PROTOCOL": "ext:ssh",
            "GIT_ATTR_SOURCE": "HEAD", "GIT_NAMESPACE": "evil", "GIT_NO_REPLACE_OBJECTS": "", "LC_ALL": "tr_TR.UTF-8",
            "TZ": "Pacific/Kiritimati"}
        return environment, traces, stray

    def test_hooks_drivers_filters_identity_and_a_stray_git_dir_leave_no_trace(self):
        clean = World(self)
        clean.standard()
        expected = build(clean, members(clean, 2, 1), publish="batch/hostile")

        world = World(self)
        world.standard()
        environment, traces, stray = self.hostile(world)

        def snapshot() -> list[tuple[str, int]]:
            return sorted((str(path.relative_to(stray)), path.stat().st_size if path.is_file() else -1)
                          for path in stray.rglob("*"))

        before = snapshot()
        with patch.dict(os.environ, environment):
            # The surroundings are live: a Git started the ordinary way is the planted program and
            # reads the planted configuration.
            control = subprocess.run(["git", "config", "--get", "merge.default"], capture_output=True, timeout=60)
            self.assertEqual(control.stdout.strip(), b"evil")
            self.assertEqual([path.name for path in traces.iterdir()], ["bin-git"])
            (traces / "bin-git").unlink()
            stack = build(world, members(world, 2, 1), publish="batch/hostile")
        self.assertEqual(stack, expected)
        self.assertEqual(list(traces.iterdir()), [])
        self.assertEqual(world.remote.rev("refs/heads/batch/hostile"), expected.commits[-1].squash_sha)
        self.assertIn(f"author {IDENTITY}\ncommitter {IDENTITY}\n",
                      world.remote.commit_text(stack.commits[0].squash_sha))
        self.assertEqual(sorted(path.name for path in world.remote.path.iterdir()),
                         ["HEAD", "config", "objects", "refs"])
        self.assertEqual(list(world.state.iterdir()), [])
        self.assertEqual(snapshot(), before)
        self.assertFalse((world.root / "hostile" / "stray-index").exists())


if __name__ == "__main__":
    unittest.main()
