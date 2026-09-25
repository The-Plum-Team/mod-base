"""``github.contents``: file-at-SHA reads with Git blob re-verification (the handoff kit binding),
live branch heads, commit trees, bounded tree listings (``tools/verify_action_tree.py``), blobs and
the ``compare`` projection used for kit reachability."""

from __future__ import annotations

import base64
import hashlib
import unittest
from typing import Any

from mod_base import KIT_REPOSITORY
from mod_base.errors import MbError
from mod_base.github import contents
from mod_base.github.api import ApiNotFound
from mod_base.github.fake import FakeGitHub
from tests.helpers import COMMIT, REPOSITORY, TREE, h

WORKFLOW = ".github/workflows/pages.yml"


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class ContentsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY, default_branch="master")
        self.api.set_branch("master", COMMIT, TREE)
        self.api.set_branch("release/1.21", h("release", 40), h("release-tree", 40))


class RepositoryTests(ContentsTestCase):
    def test_default_branch_and_branch_heads(self) -> None:
        self.assertEqual("master", contents.default_branch(self.api))
        self.assertEqual((COMMIT, TREE), contents.branch_head(self.api, "master"))
        self.assertEqual((h("release", 40), h("release-tree", 40)), contents.branch_head(self.api, "release/1.21"))
        with self.assertRaises(ApiNotFound):
            contents.branch_head(self.api, "absent")
        for bad in ("../master", "", "a..b", "/master"):
            with self.subTest(branch=bad), self.assertRaises(MbError):
                contents.branch_head(self.api, bad)

    def test_malformed_repository_and_branch_responses_are_rejected(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        api.add_response(f"/repos/{REPOSITORY}", {"full_name": "other/repo", "default_branch": "master"})
        with self.assertRaisesRegex(MbError, "does not name this repository"):
            contents.default_branch(api)
        api.add_response(f"/repos/{REPOSITORY}", {"full_name": REPOSITORY, "default_branch": "../x"})
        with self.assertRaises(MbError):
            contents.default_branch(api)
        good = {"name": "master", "commit": {"sha": COMMIT, "commit": {"tree": {"sha": TREE}}}}
        for label, payload in {"other branch": {**good, "name": "main"},
                               "short sha": {**good, "commit": {"sha": "abc", "commit": {"tree": {"sha": TREE}}}},
                               "no tree": {**good, "commit": {"sha": COMMIT}}, "array": [good]}.items():
            with self.subTest(label=label):
                api.add_response(f"/repos/{REPOSITORY}/branches/master", payload)
                with self.assertRaises(MbError):
                    contents.branch_head(api, "master")

    def test_commit_tree_requires_the_exact_commit(self) -> None:
        self.assertEqual(TREE, contents.commit_tree(self.api, COMMIT))
        self.api.add_response(f"/repos/{REPOSITORY}/git/commits/{h('c', 40)}",
                              {"sha": h("other", 40), "tree": {"sha": TREE}})
        with self.assertRaisesRegex(MbError, "another commit"):
            contents.commit_tree(self.api, h("c", 40))
        with self.assertRaises(MbError):
            contents.commit_tree(self.api, "master")


class FileAtTests(ContentsTestCase):
    def test_small_and_large_files_are_verified_against_their_blob_id(self) -> None:
        small = b"uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@" + COMMIT.encode() + b"\n"
        self.api.add_file(COMMIT, WORKFLOW, small)
        self.assertEqual(small, contents.file_at(self.api, WORKFLOW, COMMIT, max_bytes=1024))
        large = bytes(range(256)) * 5000  # beyond the 1 MiB inline limit: served through the blob API
        self.api.add_file(COMMIT, "site/large.bin", large)
        self.assertEqual(large, contents.file_at(self.api, "site/large.bin", COMMIT, max_bytes=len(large)))
        with self.assertRaisesRegex(MbError, "exceeds"):
            contents.file_at(self.api, "site/large.bin", COMMIT, max_bytes=len(large) - 1)
        with self.assertRaises(ApiNotFound):
            contents.file_at(self.api, WORKFLOW, h("other", 40), max_bytes=1024)

    def test_forged_or_malformed_contents_are_rejected(self) -> None:
        data = b"name: Project site\n"
        encoded = base64.b64encode(data).decode()
        good = {"type": "file", "path": WORKFLOW, "size": len(data), "sha": blob_id(data), "encoding": "base64",
                "content": encoded}
        cases: dict[str, Any] = {
            "blob id": {**good, "sha": blob_id(b"other")},
            "size": {**good, "size": len(data) + 1},
            "bytes": {**good, "content": base64.b64encode(b"name: Evil site!!\n").decode()},
            "directory": {**good, "type": "dir"},
            "other path": {**good, "path": "site/mod-base.json"},
            "encoding": {**good, "encoding": "utf-8"},
            "bad base64": {**good, "content": "@@@@"},
            "embedded spaces": {**good, "content": encoded[:4] + " " + encoded[4:]},
            "listing": [good],
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                self.api.add_response(f"/repos/{REPOSITORY}/contents/{WORKFLOW}", payload, params={"ref": COMMIT})
                with self.assertRaisesRegex(MbError, WORKFLOW.split("/")[-1] + "|contents|base64|encoding"):
                    contents.file_at(self.api, WORKFLOW, COMMIT, max_bytes=1024)
        wrapped = {**good, "content": "\n".join(encoded[index:index + 4] for index in range(0, len(encoded), 4))}
        self.api.add_response(f"/repos/{REPOSITORY}/contents/{WORKFLOW}", wrapped, params={"ref": COMMIT})
        self.assertEqual(data, contents.file_at(self.api, WORKFLOW, COMMIT, max_bytes=1024))

    def test_inputs_are_validated_before_any_request(self) -> None:
        for path, ref in (("../etc/passwd", COMMIT), (".git/config", COMMIT), ("/abs", COMMIT), (WORKFLOW, "master"),
                          (WORKFLOW, COMMIT.upper())):
            with self.subTest(path=path, ref=ref), self.assertRaises(MbError):
                contents.file_at(self.api, path, ref, max_bytes=10)
        for bound in (0, -1, True):
            with self.subTest(bound=bound), self.assertRaises(MbError):
                contents.file_at(self.api, WORKFLOW, COMMIT, max_bytes=bound)
        self.assertEqual(0, self.api.request_count)


class TreeAndBlobTests(ContentsTestCase):
    ENTRIES = [
        {"path": "action.yml", "mode": "100644", "type": "blob", "sha": h("action", 40), "size": 120},
        {"path": "src", "mode": "040000", "type": "tree", "sha": h("src", 40)},
        {"path": "src/run.sh", "mode": "100755", "type": "blob", "sha": h("run", 40), "size": 9},
    ]

    def test_recursive_tree_entries_are_validated(self) -> None:
        self.api.add_tree(TREE, self.ENTRIES)
        self.assertEqual(self.ENTRIES, contents.tree(self.api, TREE))
        self.assertEqual(self.ENTRIES[:2], contents.tree(self.api, TREE, recursive=False))
        self.assertEqual(self.ENTRIES, contents.tree(self.api, COMMIT))  # a commit lists its root tree

    def test_truncated_or_malformed_trees_are_refused(self) -> None:
        self.api.add_tree(TREE, self.ENTRIES, truncated=True)
        with self.assertRaisesRegex(MbError, "truncated"):
            contents.tree(self.api, TREE)
        cases = {
            "traversal": {**self.ENTRIES[0], "path": "../x"},
            "absolute": {**self.ENTRIES[0], "path": "/x"},
            "empty component": {**self.ENTRIES[0], "path": "a//b"},
            "mode": {**self.ENTRIES[0], "mode": "100777"},
            "type": {**self.ENTRIES[0], "type": "tag"},
            "object id": {**self.ENTRIES[0], "sha": "HEAD"},
            "tree size": {**self.ENTRIES[1], "size": 5},
            "negative size": {**self.ENTRIES[0], "size": -1},
            "unhashable mode": {**self.ENTRIES[0], "mode": ["100644"]},
            "unhashable type": {**self.ENTRIES[0], "type": {"blob": 1}},
        }
        for label, row in cases.items():
            with self.subTest(label=label):
                self.api.add_tree(TREE, [row])
                with self.assertRaisesRegex(MbError, "tree entry"):
                    contents.tree(self.api, TREE)
        self.api.add_tree(TREE, [self.ENTRIES[0], self.ENTRIES[0]])
        with self.assertRaisesRegex(MbError, "repeats"):
            contents.tree(self.api, TREE)
        foreign = h("foreign", 40)
        self.api.add_response(f"/repos/{REPOSITORY}/git/trees/{foreign}", {"sha": TREE, "tree": [], "truncated": False},
                              params={"recursive": 1})
        self.api.add_commit(foreign, h("elsewhere", 40))
        with self.assertRaisesRegex(MbError, "neither"):
            contents.tree(self.api, foreign)
        self.api.add_response(f"/repos/{REPOSITORY}/git/trees/{h('t', 40)}", {"sha": h("t", 40), "tree": []},
                              params={"recursive": 1})
        with self.assertRaisesRegex(MbError, "truncated"):
            contents.tree(self.api, h("t", 40))

    def test_blobs_are_rehashed(self) -> None:
        oid = self.api.add_blob(b"pinned bytes\n")
        self.assertEqual(b"pinned bytes\n", contents.blob(self.api, oid, max_bytes=100))
        self.assertEqual(b"", contents.blob(self.api, self.api.add_blob(b""), max_bytes=1))
        corrupt = self.api.add_blob(b"tampered", oid=h("claimed", 40))
        with self.assertRaisesRegex(MbError, "stale"):
            contents.blob(self.api, corrupt, max_bytes=100)
        with self.assertRaisesRegex(MbError, "outside"):
            contents.blob(self.api, oid, max_bytes=5)
        self.api.add_response(f"/repos/{REPOSITORY}/git/blobs/{h('x', 40)}",
                              {"sha": h("x", 40), "size": 1, "encoding": "utf-8", "content": "x"})
        with self.assertRaisesRegex(MbError, "malformed"):
            contents.blob(self.api, h("x", 40), max_bytes=10)
        foreign = self.api.add_blob(b"kit bytes", repository=KIT_REPOSITORY)
        with self.assertRaises(ApiNotFound):
            contents.blob(self.api, foreign, max_bytes=100)


class CompareTests(ContentsTestCase):
    def test_projection_and_repository_scope(self) -> None:
        kit_sha = h("kit", 40)
        self.api.add_compare(kit_sha, "main", {"status": "ahead", "ahead_by": 3, "behind_by": 0, "commits": []},
                             repository=KIT_REPOSITORY)
        self.assertEqual({"status": "ahead", "ahead_by": 3, "behind_by": 0},
                         contents.compare(self.api, kit_sha, "main", repository=KIT_REPOSITORY))
        with self.assertRaises(ApiNotFound):
            contents.compare(self.api, kit_sha, "main")
        self.api.add_compare(COMMIT, "release/1.21", {"status": "identical", "ahead_by": 0, "behind_by": 0})
        self.assertEqual("identical", contents.compare(self.api, COMMIT, "release/1.21")["status"])

    def test_invalid_results_and_refs_are_rejected(self) -> None:
        for result in ({"status": "unknown", "ahead_by": 0, "behind_by": 0},
                       {"status": "ahead", "ahead_by": -1, "behind_by": 0},
                       {"status": "ahead", "ahead_by": True, "behind_by": 0},
                       {"status": "ahead", "ahead_by": 1},
                       {"status": ["ahead"], "ahead_by": 1, "behind_by": 0},
                       {"status": {"ahead": 1}, "ahead_by": 1, "behind_by": 0}):
            with self.subTest(result=result):
                self.api.add_compare(COMMIT, "main", result)
                with self.assertRaisesRegex(MbError, "compare"):
                    contents.compare(self.api, COMMIT, "main")
        for base, head, repository in (("../x", "main", None), (COMMIT, "a..b", None), (COMMIT, "main", "bad")):
            with self.subTest(base=base, head=head), self.assertRaises(MbError):
                contents.compare(self.api, base, head, repository=repository)


if __name__ == "__main__":
    unittest.main()
