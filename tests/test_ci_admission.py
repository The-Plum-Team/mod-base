"""The protected admission of a pull request: which change needs an approver's decision, and which
decision on which head admits it. Only the GitHub API is faked."""

from __future__ import annotations

import copy
import unittest
from typing import Any

from mod_base.build_ci.admission import admission_refusal, changed_protected_path, head_decisions
from mod_base.build_ci.config import BuildConfig
from mod_base.build_ci.membership import MembershipError, OrgMembers
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
            user_type: str = "User", user_id: int = 1, created: str = "2026-10-11T10:00:00Z",
            updated: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": number, "body": body, "user": {"login": login, "id": user_id, "type": user_type},
            "author_association": association, "created_at": created,
            "updated_at": created if updated is None else updated, **extra}


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
            {"id": 1, "decision": "approve", "created_at": "2026-10-11T10:00:00Z", "edited": False, "late": False},
            {"id": 2, "decision": "revoke", "created_at": "2026-10-11T11:00:00Z", "edited": False, "late": False}])
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
        # An edited revocation refuses too, even under a later approval; an edited decision on
        # another head or by somebody else is no decision on this head and changes nothing.
        edited = f"admission: a `{COMMAND}` decision on this head was edited; delete it and decide again"
        self.assertEqual(self.refusal([approve, {**revoke, "updated_at": "2026-10-11T11:30:00Z"}, later]), edited)
        self.assertIsNone(self.refusal([comment(f"{COMMAND} revoke {OTHER_HEAD}", number=4,
                                                updated="2026-10-11T10:30:00Z"),
                                        comment(f"{COMMAND} revoke {HEAD}", number=5, login="someone",
                                                updated="2026-10-11T10:30:00Z"), approve]))
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


#: The organization that owns ``REPOSITORY``, as an approver: every active member of it.
ORGANIZATION = [{"organization": "example", "role": "member"}]
ORG_ADMISSION = {**ADMISSION, "approvers": ORGANIZATION}
#: Comment authors by user id: a member who shows no association to the job token, a second member,
#: an outside collaborator and an invitee who has not accepted yet.
CONCEALED, MEMBER, COLLABORATOR, INVITEE = 202, 505, 303, 404


def snapshot(*, checked: tuple[int, ...] = (CONCEALED, MEMBER, COLLABORATOR, INVITEE),
             members: tuple[int, ...] = (CONCEALED, MEMBER), through: int = 100,
             organization: str = "example") -> OrgMembers:
    return OrgMembers(organization, 7, through, frozenset(checked), frozenset(members))


def by(user_id: int, body: str, **changes: Any) -> dict[str, Any]:
    """A comment by the author ``user_id`` of the snapshot, who is listed among no users."""

    logins = {CONCEALED: "concealed", MEMBER: "member", COLLABORATOR: "collaborator", INVITEE: "invitee"}
    associations = {CONCEALED: "CONTRIBUTOR", MEMBER: "MEMBER", COLLABORATOR: "COLLABORATOR", INVITEE: "NONE"}
    changes.setdefault("association", associations.get(user_id, "NONE"))
    return comment(body, login=logins.get(user_id, f"user-{user_id}"), user_id=user_id, **changes)


class AppCommentTests(unittest.TestCase):
    def test_a_comment_posted_through_a_github_app_never_decides(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        approve = comment(f"{COMMAND} approve {HEAD}", number=1)
        through_app = {**approve, "performed_via_github_app": {"id": 1, "slug": "some-app"}}
        for row, expected in ((approve, 1), ({**approve, "performed_via_github_app": None}, 1), (through_app, 0),
                              ({**approve, "performed_via_github_app": ""}, 0)):
            api.set_comments(7, [row])
            with self.subTest(app=row.get("performed_via_github_app", "absent")):
                self.assertEqual(len(head_decisions(api, pr_number=7, head_sha=HEAD, command=COMMAND,
                                                    approvers=APPROVERS)), expected)

    def test_the_author_of_a_decision_needs_a_numeric_id(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        for user in ({"login": "AkaNebur", "type": "User"}, {"login": "AkaNebur", "type": "User", "id": "1"},
                     {"login": "AkaNebur", "type": "User", "id": 0}, {"id": 1, "type": "User"}):
            api.set_comments(7, [{**comment(f"{COMMAND} approve {HEAD}"), "user": user}])
            with self.subTest(user=user), self.assertRaises(MbError):
                head_decisions(api, pr_number=7, head_sha=HEAD, command=COMMAND, approvers=APPROVERS)
        # A comment that is no decision is not looked at further.
        api.set_comments(7, [{**comment("thanks"), "user": {"login": "x", "type": "User"}}])
        self.assertEqual(head_decisions(api, pr_number=7, head_sha=HEAD, command=COMMAND, approvers=APPROVERS), [])


class OrganizationDecisionTests(unittest.TestCase):
    """Approvers that name the organization: its members decide, proven by the snapshot by id."""

    def decisions(self, comments: list[dict[str, Any]], members: OrgMembers | None = snapshot(),
                  approvers: list[dict[str, Any]] = ORGANIZATION) -> list[tuple[int, str, bool]]:
        api = FakeGitHub(repository=REPOSITORY)
        api.set_comments(7, comments)
        return [(item["id"], item["decision"], item["late"]) for item in
                head_decisions(api, pr_number=7, head_sha=HEAD, command=COMMAND, approvers=approvers,
                               members=members)]

    def test_an_active_member_decides_whatever_association_github_shows(self) -> None:
        approve = f"{COMMAND} approve {HEAD}"
        self.assertEqual(self.decisions([by(CONCEALED, approve, number=1), by(MEMBER, approve, number=2)]),
                         [(1, "approve", False), (2, "approve", False)])
        for association in ("NONE", "CONTRIBUTOR", "COLLABORATOR", "OWNER", None):
            with self.subTest(association=association):
                self.assertEqual(self.decisions([by(CONCEALED, approve, association=association)]),
                                 [(1, "approve", False)])

    def test_a_checked_author_who_is_no_member_never_decides(self) -> None:
        # The outside collaborator with write access and the invitee whose invitation is pending.
        approve = f"{COMMAND} approve {HEAD}"
        self.assertEqual(self.decisions([by(COLLABORATOR, approve, number=1), by(INVITEE, approve, number=2)]), [])
        # Accepting the invitation needs no new comment: the next snapshot lists the invitee.
        self.assertEqual(self.decisions([by(INVITEE, approve, number=2)],
                                        snapshot(members=(CONCEALED, MEMBER, INVITEE))), [(2, "approve", False)])

    def test_a_bot_or_an_app_comment_never_decides_even_with_a_member_s_id(self) -> None:
        approve = f"{COMMAND} approve {HEAD}"
        self.assertEqual(self.decisions([by(CONCEALED, approve, user_type="Bot", number=1),
                                         by(MEMBER, approve, number=2,
                                            performed_via_github_app={"id": 9, "slug": "an-app"})]), [])

    def test_a_member_is_matched_by_id_after_a_rename(self) -> None:
        renamed = {**by(CONCEALED, f"{COMMAND} approve {HEAD}"), "user": {"login": "renamed", "id": CONCEALED,
                                                                          "type": "User"}}
        self.assertEqual(self.decisions([renamed]), [(1, "approve", False)])
        # A login that was a member's, now on another account, is not the member.
        taken = {**by(COLLABORATOR, f"{COMMAND} approve {HEAD}"), "user": {"login": "concealed", "id": COLLABORATOR,
                                                                           "type": "User"}}
        self.assertEqual(self.decisions([taken]), [])

    def test_the_text_is_matched_as_for_a_user_and_the_author_was_checked_whatever_it_says(self) -> None:
        # Python strips more than ASCII white space; the snapshot checked every author anyway.
        decisions = self.decisions([by(CONCEALED, f" {COMMAND} approve {HEAD} ", number=1),
                                    by(MEMBER, f"\x1f{COMMAND} revoke {HEAD}　", number=2,
                                       created="2026-10-11T11:00:00Z")])
        self.assertEqual(decisions, [(1, "approve", False), (2, "revoke", False)])

    def test_a_decision_after_the_snapshot_is_late_by_anybody(self) -> None:
        approve = f"{COMMAND} approve {HEAD}"
        for author in (CONCEALED, COLLABORATOR, 999):
            with self.subTest(author=author):
                self.assertEqual(self.decisions([by(author, approve, number=101)]), [(101, "approve", True)])
        self.assertEqual(self.decisions([by(CONCEALED, approve, number=100)]), [(100, "approve", False)])
        # A comment after the snapshot that is no decision is no concern of the admission.
        self.assertEqual(self.decisions([by(999, "LGTM", number=150)]), [])

    def test_a_decision_the_snapshot_should_have_checked_is_an_error(self) -> None:
        with self.assertRaises(MembershipError):
            self.decisions([by(999, f"{COMMAND} approve {HEAD}", number=100)])
        # An author it never checked who wrote no decision changes nothing.
        self.assertEqual(self.decisions([by(999, "hello", number=99)]), [])

    def test_without_a_snapshot_no_member_decides(self) -> None:
        self.assertEqual(self.decisions([by(CONCEALED, f"{COMMAND} approve {HEAD}")], members=None), [])

    def test_listed_users_decide_beside_the_organization_by_their_association(self) -> None:
        mixed = [*APPROVERS, *ORGANIZATION]
        owner = comment(f"{COMMAND} approve {HEAD}", number=150, user_id=1)
        # A listed user decides without the snapshot: never late, never checked.
        self.assertEqual(self.decisions([owner], approvers=mixed), [(150, "approve", False)])
        self.assertEqual(self.decisions([owner], members=None, approvers=mixed), [(150, "approve", False)])

    def test_a_snapshot_of_another_admission_is_refused(self) -> None:
        with self.assertRaises(MembershipError):
            self.decisions([], approvers=APPROVERS)
        with self.assertRaises(MembershipError):
            self.decisions([], members=snapshot(organization="other"))


class OrganizationRefusalTests(unittest.TestCase):
    """``admission_refusal`` with approvers that name the organization."""

    setUp = RefusalTests.setUp
    config = RefusalTests.config
    protect = RefusalTests.protect

    def refusal(self, comments: list[dict[str, Any]], members: OrgMembers | None = snapshot()) -> str | None:
        self.repository.api.set_comments(7, comments)
        return admission_refusal(CommandReads.of(self.repository.api), Watch(), config=self.config(ORG_ADMISSION),
                                 plan=self.plan, members=members)

    def test_any_member_approves_and_any_member_revokes(self) -> None:
        self.protect()
        approve = by(CONCEALED, f"{COMMAND} approve {HEAD}", number=1)
        revoke = by(MEMBER, f"{COMMAND} revoke {HEAD}", number=2, created="2026-10-11T11:00:00Z")
        revoked = f"admission: the latest `{COMMAND}` decision on this head revokes it"
        self.assertIsNone(self.refusal([approve]))
        self.assertEqual(self.refusal([approve, revoke]), revoked)
        # A non-member's later decision changes nothing, either way.
        stranger = by(COLLABORATOR, f"{COMMAND} approve {HEAD}", number=3, created="2026-10-11T12:00:00Z")
        self.assertEqual(self.refusal([approve, revoke, stranger]), revoked)
        stranger = by(COLLABORATOR, f"{COMMAND} revoke {HEAD}", number=3, created="2026-10-11T12:00:00Z")
        self.assertIsNone(self.refusal([approve, stranger]))

    def test_a_removed_member_s_decisions_stop_counting(self) -> None:
        self.protect()
        approve = by(CONCEALED, f"{COMMAND} approve {HEAD}", number=1)
        revoke = by(MEMBER, f"{COMMAND} revoke {HEAD}", number=2, created="2026-10-11T11:00:00Z")
        # The revocation of a member who has been removed no longer counts: the approval decides again.
        self.assertIsNone(self.refusal([approve, revoke], members=snapshot(members=(CONCEALED,))))
        self.assertEqual(self.refusal([approve], members=snapshot(members=(MEMBER,))),
                         f"admission: scripts/ci is protected; needs `{COMMAND} approve <head sha>` from an approver")

    def test_without_a_snapshot_the_refusal_says_so(self) -> None:
        self.protect()
        unread = ("admission: scripts/ci is protected; the organization's members were not read, so no member's "
                  "approval counts")
        self.assertEqual(self.refusal([by(CONCEALED, f"{COMMAND} approve {HEAD}")], members=None), unread)
        self.assertLessEqual(len(unread), limits.MAX_CI_STATUS_DESCRIPTION_CHARS)
        self.assertEqual(self.refusal([], members=snapshot()),
                         f"admission: scripts/ci is protected; needs `{COMMAND} approve <head sha>` from an approver")

    def test_a_decision_after_the_snapshot_refuses_until_the_next_evaluation(self) -> None:
        self.protect()
        approve = by(CONCEALED, f"{COMMAND} approve {HEAD}", number=1)
        late = f"admission: a `{COMMAND}` decision on this head was posted after its members were read; evaluate again"
        self.assertLessEqual(len(late), limits.MAX_CI_STATUS_DESCRIPTION_CHARS)
        for author in (CONCEALED, MEMBER, COLLABORATOR, 999):
            for decision in ("approve", "revoke"):
                later = by(author, f"{COMMAND} {decision} {HEAD}", number=101, created="2026-10-11T12:00:00Z")
                with self.subTest(author=author, decision=decision):
                    self.assertEqual(self.refusal([approve, later]), late)
        # The next evaluation's snapshot reads the comment and decides it.
        later = by(COLLABORATOR, f"{COMMAND} revoke {HEAD}", number=101, created="2026-10-11T12:00:00Z")
        self.assertIsNone(self.refusal([approve, later], members=snapshot(through=101)))

    def test_an_edited_member_decision_refuses(self) -> None:
        self.protect()
        approve = by(CONCEALED, f"{COMMAND} approve {HEAD}", number=1, updated="2026-10-11T10:30:00Z")
        self.assertEqual(self.refusal([approve]),
                         f"admission: a `{COMMAND}` decision on this head was edited; delete it and decide again")
