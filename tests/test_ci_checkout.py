"""The one closed Git of a job's checkouts, and the commit at ``HEAD`` read by its own bytes.

Everything here is real: repositories, the checkouts ``actions/checkout`` would make of them and
``/usr/bin/git``. The hostile cases are a replacement ref, a grafts file, an object store that
lies about an object and a ``HEAD`` that names something else than a commit.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from mod_base.build_ci import lifecycle
from mod_base.build_ci.checkout import GIT, CheckoutError, Commit, head_commit, read_objects
from mod_base.errors import MbError
from mod_base.model import limits
from tests import ci_checkout_fixture as fixture
from tests.ci_lifecycle_fixture import git

ANSWER = limits.MAX_CI_GIT_ANSWER_BYTES


class HeadCommitTests(fixture.GenerationCase):
    def detached_at(self, body: bytes) -> Commit:
        """Store ``body`` as a commit of the candidate checkout, put ``HEAD`` there and read it."""

        name = fixture.write_object(self.candidate, "commit", body)
        fixture.detach(self.candidate, name)
        commit = head_commit(self.candidate)
        self.assertEqual(commit.sha, name)
        return commit

    def test_a_checkout_as_the_action_makes_it_states_the_commit_git_itself_no_longer_reports(self) -> None:
        tested, controller = self.generation.tested, self.generation.controller
        self.assertEqual(tested.parents, (controller.sha, self.generation.head.sha))
        self.assertEqual((self.candidate / ".git" / "shallow").read_bytes(), f"{tested.sha}\n".encode("ascii"))
        self.assertEqual((self.candidate / ".git" / "HEAD").read_bytes(), f"{tested.sha}\n".encode("ascii"))
        # The shallow boundary hides the parents from Git, and their objects were never fetched.
        self.assertEqual(git(self.candidate, "rev-list", "--parents", "--max-count=1", "HEAD"), tested.sha)
        for parent in tested.parents:
            with self.assertRaises(AssertionError):
                git(self.candidate, "cat-file", "-e", parent)
        self.assertEqual(head_commit(self.candidate), tested)
        self.assertEqual(head_commit(self.mod), controller)
        self.assertEqual(head_commit(Path(os.path.relpath(self.candidate))), tested)

    def test_a_packed_and_a_complete_repository_state_the_same_commits(self) -> None:
        tested = self.generation.tested
        packed = self.generation.checkout(self.temporary / "packed", tested.sha, packed=True)
        objects = packed / ".git" / "objects"
        self.assertEqual([path.suffix for path in objects.glob("pack/*.pack")], [".pack"])
        self.assertFalse((objects / tested.sha[:2]).exists(), "the commit is in the pack alone")
        self.assertTrue((self.candidate / ".git" / "objects" / tested.sha[:2] / tested.sha[2:]).is_file())
        self.assertEqual(head_commit(packed), tested)
        # The repository GitHub holds: a branch at HEAD, the whole history, no shallow boundary.
        self.assertTrue((self.generation.upstream / ".git" / "HEAD").read_bytes().startswith(b"ref: "))
        self.assertEqual(head_commit(self.generation.upstream), self.generation.controller)
        self.assertEqual(self.generation.controller.parents, (self.generation.base.sha,))

    def test_a_replacement_ref_cannot_change_the_commit_that_is_read(self) -> None:
        tested, base = self.generation.tested, self.generation.base
        tree = git(self.candidate, "rev-parse", f"{tested.sha}:src")
        other = fixture.write_object(self.candidate, "commit", fixture.commit_bytes(tree, [base.sha]))
        git(self.candidate, "replace", tested.sha, other)
        # Plain Git now reads another commit under the name of the tested one.
        self.assertEqual(git(self.candidate, "rev-parse", f"{tested.sha}^{{tree}}"), tree)
        self.assertIn(f"parent {base.sha}", git(self.candidate, "cat-file", "commit", tested.sha))
        self.assertEqual(head_commit(self.candidate), tested)

    def test_a_grafts_file_cannot_change_the_parents_that_are_read(self) -> None:
        tested, base = self.generation.tested, self.generation.base
        complete = fixture.copy(self.generation.upstream, self.temporary / "complete")
        git(complete, "checkout", "-q", "--detach", tested.sha)
        parents = f"{tested.sha} {tested.parents[0]} {tested.parents[1]}"
        self.assertEqual(git(complete, "rev-list", "--parents", "--max-count=1", "HEAD"), parents)
        (complete / ".git" / "info").mkdir(exist_ok=True)
        (complete / ".git" / "info" / "grafts").write_bytes(f"{tested.sha} {base.sha}\n".encode("ascii"))
        self.assertEqual(git(complete, "rev-list", "--parents", "--max-count=1", "HEAD"), f"{tested.sha} {base.sha}",
                         "this Git no longer honours a grafts file: the case proves nothing any more")
        # The closed Git of the kit honours it too. Only the bytes of the object are out of its reach.
        self.assertEqual(read_objects(complete, "rev-parse", "--verify", f"{tested.sha}^1", max_bytes=ANSWER),
                         f"{base.sha}\n".encode("ascii"))
        with self.assertRaises(CheckoutError):
            read_objects(complete, "rev-parse", "--verify", f"{tested.sha}^2", max_bytes=ANSWER)
        self.assertEqual(head_commit(complete), tested)

    def test_an_object_stored_under_the_name_of_another_is_refused(self) -> None:
        tested, controller = self.generation.tested, self.generation.controller
        # The same parents, another tree: what a forged test merge would want to be read as.
        forged = fixture.commit_bytes(controller.tree, tested.parents)
        fixture.write_object(self.candidate, "commit", forged, name=tested.sha)
        # Git prints the object without comparing it with its name.
        self.assertEqual(read_objects(self.candidate, "cat-file", "commit", tested.sha,
                                      max_bytes=limits.MAX_CI_GIT_COMMIT_BYTES), forged)
        with self.assertRaisesRegex(CheckoutError, "does not hash to its name") as caught:
            head_commit(self.candidate)
        self.assertEqual((caught.exception.reason, caught.exception.exit_code), ("git", 2))
        fixture.write_object(self.mod, "commit", fixture.commit_bytes(tested.tree, controller.parents),
                             name=controller.sha)
        with self.assertRaisesRegex(CheckoutError, "the commit read at HEAD of the mod checkout"):
            head_commit(self.mod)

    def test_head_must_name_a_commit_itself(self) -> None:
        tested = self.generation.tested
        git(self.candidate, "checkout", "-q", "-b", "work")
        self.assertEqual((self.candidate / ".git" / "HEAD").read_bytes(), b"ref: refs/heads/work\n")
        self.assertEqual(head_commit(self.candidate), tested, "a branch at the commit names the commit")
        git(self.candidate, "tag", "-a", "-m", "a tag object", "v1", tested.sha)
        tag = git(self.candidate, "rev-parse", "v1")
        self.assertNotEqual(tag, tested.sha)
        blob = git(self.candidate, "rev-parse", f"{tested.sha}:src/payload.txt")
        cases = {"a tag of the commit": (tag, "does not hash to its name"), "its tree": (tested.tree, "rejected"),
                 "a blob": (blob, "rejected"), "an object that is absent": ("9" * 40, "rejected")}
        for label, (name, message) in cases.items():
            fixture.detach(self.candidate, name)
            with self.subTest(case=label), self.assertRaisesRegex(CheckoutError, message):
                head_commit(self.candidate)
        (self.candidate / ".git" / "HEAD").write_bytes(b"ref: refs/heads/unborn\n")
        with self.assertRaisesRegex(CheckoutError, "git rev-parse rejected the candidate checkout"):
            head_commit(self.candidate)

    def test_a_directory_without_a_git_directory_of_its_own_is_no_checkout(self) -> None:
        plain = self.temporary / "plain"
        plain.mkdir()
        nested = self.candidate / "nested"  # Inside a repository, but not one itself: nothing is discovered.
        nested.mkdir()
        pointer = self.temporary / "pointer"
        pointer.mkdir()
        (pointer / ".git").write_text(f"gitdir: {self.candidate / '.git'}\n", encoding="utf-8")
        linked = self.temporary / "linked"
        linked.mkdir()
        (linked / ".git").symlink_to(self.candidate / ".git")
        for directory in (plain, nested, pointer, linked, self.temporary / "absent"):
            with self.subTest(directory=directory.name), self.assertRaises(CheckoutError) as caught:
                head_commit(directory)
            self.assertEqual(str(caught.exception),
                             f"the {directory.name} checkout has no Git directory of its own")
        # Git itself would have followed both the pointer and the link.
        self.assertEqual(git(pointer, "rev-parse", "HEAD"), self.generation.tested.sha)
        self.assertEqual(git(linked, "rev-parse", "HEAD"), self.generation.tested.sha)

    def test_the_tree_and_the_parents_are_the_first_header_lines_and_nothing_else(self) -> None:
        tree, first, second, third = (digit * 40 for digit in "1234")
        self.assertEqual(self.detached_at(fixture.commit_bytes(tree, [])).parents, ())
        self.assertEqual(self.detached_at(fixture.commit_bytes(tree, [second, first])),
                         Commit(fixture.object_name("commit", fixture.commit_bytes(tree, [second, first])), tree,
                                (second, first)))
        # The reader keeps what the object says; that a subject has at most two parents is the subject's rule.
        self.assertEqual(self.detached_at(fixture.commit_bytes(tree, [first, second, third])).parents,
                         (first, second, third))
        signature = ("gpgsig -----BEGIN PGP SIGNATURE-----\n \n"
                     f" parent {third}\n tree {third}\n -----END PGP SIGNATURE-----\n")
        signed = self.detached_at(fixture.commit_bytes(tree, [first], trailer=signature))
        self.assertEqual((signed.tree, signed.parents), (tree, (first,)))
        message = f"a message\n\nparent {third}\ntree {third}\n"
        told = self.detached_at(fixture.commit_bytes(tree, [first], message=message))
        self.assertEqual((told.tree, told.parents), (tree, (first,)))
        silent = self.detached_at(fixture.commit_bytes(tree, [first], message=""))
        self.assertEqual((silent.tree, silent.parents), (tree, (first,)))

    def test_a_commit_object_that_can_be_read_in_two_ways_is_refused(self) -> None:
        tree, first, second = (digit * 40 for digit in "123")
        header = fixture.commit_bytes(tree, [first]).partition(b"\n\n")[0].decode("ascii")
        bodies = {
            "a parent after the committer": fixture.commit_bytes(tree, [first], trailer=f"parent {second}\n"),
            "a second tree": fixture.commit_bytes(tree, [first], trailer=f"tree {second}\n"),
            "a parent before the tree": f"parent {first}\n{header}\n\nmessage\n".encode("ascii"),
            "no tree": header.partition("\n")[2].encode("ascii") + b"\n\nmessage\n",
            "a short parent": header.replace(first, first[:39]).encode("ascii") + b"\n\nmessage\n",
            "an upper-case parent": header.replace(first, "A" * 40).encode("ascii") + b"\n\nmessage\n",
            "a parent with a carriage return": header.replace(first, first + "\r").encode("ascii") + b"\n\nmessage\n",
            "a tree with two names": header.replace(tree, f"{tree} {second}").encode("ascii") + b"\n\nmessage\n",
            "nothing": b"",
        }
        for label, body in bodies.items():
            fixture.detach(self.candidate, fixture.write_object(self.candidate, "commit", body))
            with self.subTest(case=label), self.assertRaisesRegex(CheckoutError, "not a canonical commit object"):
                head_commit(self.candidate)

    def test_a_commit_is_read_whole_within_its_bound(self) -> None:
        tree, parent = "1" * 40, "2" * 40
        room = limits.MAX_CI_GIT_COMMIT_BYTES - len(fixture.commit_bytes(tree, [parent], message=""))
        largest = self.detached_at(fixture.commit_bytes(tree, [parent], message="m" * room))
        self.assertEqual((largest.tree, largest.parents), (tree, (parent,)))
        beyond = fixture.commit_bytes(tree, [parent], message="m" * (room + 1))
        fixture.detach(self.candidate, fixture.write_object(self.candidate, "commit", beyond))
        # Refused by its size, which is asked first: the bytes beyond the bound are never read.
        with self.assertRaisesRegex(CheckoutError, "the object at HEAD of the candidate checkout is larger than"):
            head_commit(self.candidate)

    def test_a_repository_of_another_object_format_is_refused(self) -> None:
        other = self.temporary / "sha256"
        other.mkdir()
        git(other, "init", "-q", "--object-format=sha256")
        (other / "file").write_bytes(b"content\n")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "a commit named by another hash")
        self.assertEqual(len(git(other, "rev-parse", "HEAD")), 64)
        with self.assertRaisesRegex(CheckoutError, "HEAD of the sha256 checkout does not resolve to the name of a "
                                                   "SHA-1 object"):
            head_commit(other)


class ReadObjectsTests(fixture.GenerationCase):
    def test_a_read_answers_within_its_bound_or_is_a_rejection(self) -> None:
        tested = self.generation.tested.sha
        answer = f"{tested}\n".encode("ascii")
        self.assertEqual(GIT, "/usr/bin/git")
        self.assertEqual(read_objects(self.candidate, "rev-parse", "--verify", "HEAD", max_bytes=len(answer)), answer)
        with self.assertRaises(CheckoutError) as caught:
            read_objects(self.candidate, "rev-parse", "--verify", "HEAD", max_bytes=len(answer) - 1)
        self.assertEqual((str(caught.exception), caught.exception.reason),
                         ("git rev-parse rejected the candidate checkout", "git"))
        with self.assertRaisesRegex(MbError, "git cat-file rejected the mod checkout"):
            read_objects(self.mod, "cat-file", "commit", tested, max_bytes=ANSWER)

    def test_the_environment_of_the_process_has_no_say(self) -> None:
        tested, controller = self.generation.tested, self.generation.controller
        present = [GIT, f"--git-dir={self.candidate / '.git'}", "cat-file", "-e", tested.sha]
        self.assertEqual(subprocess.run(present, timeout=60, check=False).returncode, 0)
        # Even a Git that is told its repository takes the object store from the environment.
        with mock.patch.dict(os.environ, {"GIT_OBJECT_DIRECTORY": str(self.mod / ".git" / "objects")}):
            self.assertNotEqual(subprocess.run(present, stderr=subprocess.DEVNULL, timeout=60, check=False).returncode,
                                0, "a Git that inherits this environment reads the other object store")
            self.assertEqual(head_commit(self.candidate), tested)
            self.assertEqual(head_commit(self.mod), controller)

    def test_the_plan_reads_the_candidate_checkout_with_the_same_git(self) -> None:
        self.assertIs(lifecycle._git, read_objects)


if __name__ == "__main__":
    unittest.main()
