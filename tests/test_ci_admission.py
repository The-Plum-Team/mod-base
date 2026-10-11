"""The protected admission of a pull request: which change needs an approver's decision, and which
decision on which head admits it. Only the GitHub API is faked."""

from __future__ import annotations

import copy
import unittest
from typing import Any

from mod_base.build_ci.admission import admission_refusal, changed_protected_path, head_decisions
from mod_base.build_ci.config import BuildConfig
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.errors import MbError
from mod_base.github.api import ApiError
from mod_base.github.fake import FakeGitHub
from mod_base.model import limits
from tests.helpers import ci_config, ci_plan

REPOSITORY = "example/mod"
BASE_SHA, BASE_TREE, TESTED_TREE = "c" * 40, "d" * 40, "f" * 40
HEAD = "a" * 40
OTHER_HEAD = "b" * 40
COMMAND = "/controller-upgrade"
APPROVERS = [{"login": "AkaNebur", "association": "MEMBER"}]
ADMISSION = {"protected_paths": [".github/workflows", "scripts/ci", "site"], "command": COMMAND,
             "approvers": APPROVERS}
COMMENTS = f"/repos/{REPOSITORY}/issues/7/comments"


def oid(seed: str) -> str:
    return (seed * 40)[:40]


def entry(name: str, kind: str, sha: str, mode: str | None = None) -> dict[str, Any]:
    return {"path": name, "mode": mode or ("040000" if kind == "tree" else "100644"), "type": kind, "sha": sha}


def comment(body: str, *, number: int = 1, login: str = "AkaNebur", association: str = "MEMBER",
            user_type: str = "User", created: str = "2026-10-11T10:00:00Z", updated: str | None = None) -> dict[str, Any]:
    return {"id": number, "body": body, "user": {"login": login, "type": user_type}, "author_association": association,
            "created_at": created, "updated_at": created if updated is None else updated}


class Repository:
    """A base commit and a tested merge tree whose protected directories share their objects
    unless a test changes one."""

    def __init__(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY)
        self.api.add_commit(BASE_SHA, BASE_TREE, parents=["9" * 40])
        self.base = {"": [entry(".github", "tree", oid("1")), entry("scripts", "tree", oid("2")),
                          entry("site", "tree", oid("3")), entry("src", "tree", oid("4")),
                          entry("README.md", "blob", oid("5"))],
                     oid("1"): [entry("workflows", "tree", oid("6")), entry("CODEOWNERS", "blob", oid("7"))],
                     oid("2"): [entry("ci", "tree", oid("8")), entry("release", "tree", oid("9"))]}
        self.tested = copy.deepcopy(self.base)
        self.tested[""] = [row if row["path"] != "src" else entry("src", "tree", oid("a1")) for row in self.base[""]]
        self.seed()

    def seed(self) -> None:
        for side, root in ((self.base, BASE_TREE), (self.tested, TESTED_TREE)):
            for sha, rows in side.items():
                self.api.add_tree(root if sha == "" else sha, rows)

    def change(self, directory: str, name: str, row: dict[str, Any] | None, new_sha: str) -> None:
        """Replace (or remove, with ``row`` None) ``name`` in the tested copy of ``directory`` (a
        tree id of the base, or "" for the root), which gets the new id ``new_sha``; the root then
        names the new directory object."""

        rows = [item for item in self.tested[directory] if item["path"] != name] + ([] if row is None else [row])
        if directory == "":
            self.tested[""] = rows
        else:
            del self.tested[directory]
            self.tested[new_sha] = rows
            self.tested[""] = [entry(item["path"], "tree", new_sha) if item["type"] == "tree" and item["sha"] == directory
                               else item for item in self.tested[""]]
        self.seed()

    def changed(self, paths: list[str] | None = None) -> str | None:
        return changed_protected_path(CommandReads.of(self.api), base_sha=BASE_SHA, tested_tree=TESTED_TREE,
                                      protected_paths=paths or ADMISSION["protected_paths"])


class ChangedPathTests(unittest.TestCase):
    def test_a_change_outside_the_protected_paths_reads_only_the_roots(self) -> None:
        repository = Repository()
        self.assertIsNone(repository.changed())
        # The base commit and both roots: each protected directory's object is equal on both sides.
        self.assertEqual(repository.api.request_count, 3)

    def test_a_file_changed_inside_a_protected_directory_names_that_directory(self) -> None:
        repository = Repository()
        repository.api.add_tree(oid("b1"), [entry("pr_gate.py", "blob", oid("c1"))])
        repository.change(oid("2"), "ci", entry("ci", "tree", oid("b1")), oid("b2"))
        self.assertEqual(repository.changed(), "scripts/ci")
        # A sibling that changed is not the protected path.
        repository = Repository()
        repository.change(oid("2"), "release", entry("release", "tree", oid("b1")), oid("b2"))
        self.assertIsNone(repository.changed())
        self.assertEqual(repository.api.request_count, 5)

    def test_an_added_removed_retyped_or_remoded_entry_is_a_change(self) -> None:
        cases = {
            "added": ("", "site", None),
            "removed": (oid("1"), "workflows", None),
            "a file instead of a directory": (oid("1"), "workflows", entry("workflows", "blob", oid("6"))),
            "executable": (oid("1"), "workflows", entry("workflows", "tree", oid("6"), mode="100755")),
        }
        for label, (directory, name, row) in cases.items():
            repository = Repository()
            if label == "added":
                repository.base[""] = [item for item in repository.base[""] if item["path"] != "site"]
                repository.api.add_tree(BASE_TREE, repository.base[""])
            else:
                repository.change(directory, name, row, oid("e1"))
            with self.subTest(case=label):
                self.assertEqual(repository.changed(), "site" if label == "added" else ".github/workflows")

    def test_a_protected_file_compares_its_mode_and_blob(self) -> None:
        repository = Repository()
        repository.change(oid("1"), "CODEOWNERS", entry("CODEOWNERS", "blob", oid("7"), mode="100755"), oid("e1"))
        self.assertEqual(repository.changed([".github/CODEOWNERS", "scripts/ci"]), ".github/CODEOWNERS")
        repository = Repository()
        repository.change(oid("1"), "CODEOWNERS", entry("CODEOWNERS", "blob", oid("e2")), oid("e1"))
        self.assertEqual(repository.changed([".github/CODEOWNERS"]), ".github/CODEOWNERS")
        self.assertIsNone(repository.changed([".github/workflows"]))
        # Absent on both sides is no change.
        self.assertIsNone(Repository().changed(["buildSrc", "gradle/wrapper"]))

    def test_a_truncated_or_foreign_tree_or_an_unknown_commit_is_an_error(self) -> None:
        repository = Repository()
        repository.api.add_tree(TESTED_TREE, repository.tested[""], truncated=True)
        with self.assertRaises(MbError):
            repository.changed()
        repository = Repository()
        with self.assertRaises(ApiError):
            changed_protected_path(repository.api, base_sha="9" * 40, tested_tree=TESTED_TREE,
                                   protected_paths=["scripts/ci"])
        with self.assertRaises(MbError):
            changed_protected_path(repository.api, base_sha=BASE_SHA, tested_tree="tree",
                                   protected_paths=["scripts/ci"])


class DecisionTests(unittest.TestCase):
    def decisions(self, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        api = FakeGitHub(repository=REPOSITORY)
        api.set_comments(7, comments)
        return head_decisions(api, pr_number=7, head_sha=HEAD, command=COMMAND, approvers=APPROVERS)

    def test_only_an_exact_command_on_this_head_by_a_listed_approver_counts(self) -> None:
        counted = [comment(f"{COMMAND} approve {HEAD}", number=1),
                   comment(f"  {COMMAND} revoke {HEAD}\r\n", number=2, login="akanebur",
                           created="2026-10-11T11:00:00Z")]
        ignored = [comment(f"{COMMAND} approve {OTHER_HEAD}", number=3),
                   comment(f"{COMMAND} approve {HEAD[:7]}", number=4),
                   comment(f"{COMMAND} approve {HEAD.upper()}", number=5),
                   comment(f"LGTM\n{COMMAND} approve {HEAD}", number=6),
                   comment(f"{COMMAND} approve {HEAD} please", number=7),
                   comment(f"/restricted-transition approve {HEAD}", number=8),
                   comment(f"{COMMAND}  approve {HEAD}", number=9),
                   comment(f"{COMMAND} approve {HEAD}", number=10, login="someone"),
                   comment(f"{COMMAND} approve {HEAD}", number=11, association="OWNER"),
                   comment(f"{COMMAND} approve {HEAD}", number=12, association="CONTRIBUTOR"),
                   comment(f"{COMMAND} approve {HEAD}", number=13, user_type="Bot"),
                   {**comment(f"{COMMAND} approve {HEAD}", number=14), "user": None}]
        self.assertEqual(self.decisions(ignored + counted), [
            {"id": 1, "decision": "approve", "created_at": "2026-10-11T10:00:00Z", "edited": False},
            {"id": 2, "decision": "revoke", "created_at": "2026-10-11T11:00:00Z", "edited": False}])
        self.assertEqual(self.decisions([]), [])

    def test_decisions_are_ordered_by_time_then_id_and_edits_are_marked(self) -> None:
        decisions = self.decisions([
            comment(f"{COMMAND} revoke {HEAD}", number=9, created="2026-10-11T12:00:00.5Z"),
            comment(f"{COMMAND} approve {HEAD}", number=3, created="2026-10-11T12:00:00Z",
                    updated="2026-10-11T12:05:00Z"),
            comment(f"{COMMAND} approve {HEAD}", number=4, created="2026-10-11T10:00:00+00:00")])
        self.assertEqual([(item["id"], item["edited"]) for item in decisions], [(4, False), (3, True), (9, False)])

    def test_a_malformed_comment_or_too_many_comments_is_an_error(self) -> None:
        for broken in ({"id": "1", "body": "x"}, {"id": 0, "body": "x"}, {"id": 1, "body": None}, {"id": 1}):
            with self.subTest(comment=broken), self.assertRaises(MbError):
                self.decisions([broken])
        with self.assertRaises(MbError):
            self.decisions([comment(f"{COMMAND} approve {HEAD}", created="yesterday")])
        many = [comment("hello", number=index + 1) for index in range(limits.MAX_CI_ADMISSION_COMMENTS)]
        self.assertEqual(self.decisions(many), [])
        with self.assertRaises(ApiError):
            self.decisions([*many, comment("hello", number=len(many) + 1)])


class RefusalTests(unittest.TestCase):
    """``admission_refusal``: the reason a gate may not succeed, from the protected config alone."""

    def setUp(self) -> None:
        self.repository = Repository()
        self.plan = ci_plan()
        self.plan["identity"].update(base_sha=BASE_SHA, tested_tree=TESTED_TREE, head_sha=HEAD, pr_number=7)

    def config(self, admission: dict[str, Any] | None = ADMISSION) -> BuildConfig:
        document = ci_config()
        if admission is not None:
            document["admission"] = copy.deepcopy(admission)
        return BuildConfig(document, b"", "0" * 64, (), ())

    def refusal(self, comments: list[dict[str, Any]], admission: dict[str, Any] | None = ADMISSION,
                watch: Watch | None = None) -> str | None:
        self.repository.api.set_comments(7, comments)
        return admission_refusal(CommandReads.of(self.repository.api), watch or Watch(),
                                 config=self.config(admission), plan=self.plan)

    def protect(self) -> None:
        self.repository.api.add_tree(oid("b1"), [entry("pr_gate.py", "blob", oid("c1"))])
        self.repository.change(oid("2"), "ci", entry("ci", "tree", oid("b1")), oid("b2"))

    def test_without_an_admission_or_a_protected_change_nothing_is_refused(self) -> None:
        self.protect()
        self.assertIsNone(self.refusal([], admission=None))
        self.assertEqual(self.repository.api.request_count, 0)
        self.repository = Repository()
        self.assertIsNone(self.refusal([]))
        # No decision is read when nothing protected changed.
        self.assertEqual(self.repository.api.request_count, 3)

    def test_a_protected_change_needs_the_latest_decision_on_its_head_to_approve(self) -> None:
        self.protect()
        approve = comment(f"{COMMAND} approve {HEAD}", number=1)
        revoke = comment(f"{COMMAND} revoke {HEAD}", number=2, created="2026-10-11T11:00:00Z")
        later = comment(f"{COMMAND} approve {HEAD}", number=3, created="2026-10-11T12:00:00Z")
        missing = f"admission: scripts/ci is protected; needs `{COMMAND} approve <head sha>` from an approver"
        self.assertEqual(self.refusal([]), missing)
        self.assertEqual(self.refusal([comment(f"{COMMAND} approve {OTHER_HEAD}")]), missing)
        self.assertEqual(self.refusal([comment(f"{COMMAND} approve {HEAD}", login="someone")]), missing)
        self.assertIsNone(self.refusal([approve]))
        self.assertEqual(self.refusal([approve, revoke]),
                         f"admission: the latest `{COMMAND}` decision on this head revokes it")
        self.assertIsNone(self.refusal([approve, revoke, later]))
        self.assertEqual(self.refusal([{**approve, "updated_at": "2026-10-11T10:30:00Z"}, revoke, later]),
                         f"admission: a `{COMMAND}` decision on this head was edited; delete it and decide again")
        for reason in (missing, self.refusal([approve, revoke])):
            self.assertLessEqual(len(reason), limits.MAX_CI_STATUS_DESCRIPTION_CHARS)

    def test_the_decisions_are_watched_until_the_evaluation_returns(self) -> None:
        self.protect()
        watch = Watch()
        approve = comment(f"{COMMAND} approve {HEAD}", number=1)
        self.assertIsNone(self.refusal([approve], watch=watch))
        watch.recheck()
        # An unrelated comment changes nothing; a revocation after the read fails the recheck.
        self.repository.api.set_comments(7, [approve, comment("thanks", number=2)])
        watch.recheck()
        self.repository.api.set_comments(7, [approve, comment(f"{COMMAND} revoke {HEAD}", number=2,
                                                              created="2026-10-11T11:00:00Z")])
        with self.assertRaises(MbError) as caught:
            watch.recheck()
        self.assertIn("approver decisions on the pull request head changed", str(caught.exception))

    def test_the_plan_names_the_compared_sides_and_the_head(self) -> None:
        self.protect()
        self.plan["identity"]["head_sha"] = OTHER_HEAD
        self.assertIsNotNone(self.refusal([comment(f"{COMMAND} approve {HEAD}")]))
        self.assertIsNone(self.refusal([comment(f"{COMMAND} approve {OTHER_HEAD}")]))
        self.plan["identity"]["tested_tree"] = BASE_TREE
        self.assertIsNone(self.refusal([]))
