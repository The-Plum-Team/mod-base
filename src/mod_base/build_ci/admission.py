"""Protected admission: a pull request that changes a protected path needs an approver's decision
on its exact head before any of its gates may show ``success`` (MB11, read-only).

The kit's status evaluation is the only writer of a mod's required contexts once a mode puts them
under the kit, and its graph and record checks prove what ran, not who may change how it runs. A
mod's own evaluator did that before: it refused a pull request that changed its protected
controllers unless the owner had approved that exact head. ``admission`` of the protected Build
config (:func:`mod_base.build_ci.config.admission`) states the same rule as data, and
:func:`admission_refusal` applies it:

* The protected paths come from the config of the protected checkout that executes, never from
  the candidate. A protected path is a file or a directory; it is changed when its tree entry
  (mode, type and object id) in the tested merge differs from the one in the pull request's base,
  an entry that appears or disappears included. The tree objects are read by id through the API
  and compared from the root down, so a directory whose object is equal on both sides is not read
  further.
* A decision is a comment of the pull request whose whole text (surrounding white space aside) is
  ``<command> approve <head sha>`` or ``<command> revoke <head sha>`` for the head under
  evaluation, written by a ``User`` (never a bot, and never through a GitHub App) who is an
  approver: a user listed among the approvers with the association listed for them or, when the
  approvers name the organization that owns the repository, an active member of it. Membership is
  proven by the snapshot of the status caller's ``members`` job (:mod:`mod_base.build_ci.membership`),
  by user id, as it is when the evaluation runs, not when the comment was written: an approval
  posted by an invitee counts once the invitation is accepted, and every decision of a member who
  has been removed stops counting, revocations included. Any member may approve, also a pull
  request they wrote, and any member may revoke another member's approval.
* The latest decision counts, and it must approve. A decision on another head, by anybody else or
  in other words counts for nothing, and a decision that was edited after it was written refuses
  the admission until it is deleted: anybody with write access can edit a comment. A decision
  the members snapshot did not read as one (posted after it, a comment id above its
  ``through_comment_id``, or edited into one since) refuses too, until an evaluation with a newer
  snapshot decides it. Without a snapshot an admission that names an organization refuses every
  change to a protected path, whoever decided, and a comment that the snapshot should have checked
  but did not is an error.
* A refusal is a reason, which the evaluation publishes as a failure of every gate that would
  otherwise have been a success. An API failure is never a refusal: it propagates and no intent is
  produced, as for every other read of the evaluation.

The decisions are mutable state: they are read once through the evaluation's watch and read again
immediately before its document is returned. The trees are immutable and read once.
"""

from __future__ import annotations

import functools
from typing import Any

from mod_base.build_ci.config import BuildConfig, admission, admission_organization
from mod_base.build_ci.membership import MembershipError, OrgMembers
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.github.api import GitHubApi
from mod_base.github.contents import commit_tree, exact_tree
from mod_base.model import grammar, limits
from mod_base.model.validators import check

#: The tree entry of a repository root, as compared with the entries below it.
_ROOT = "040000", "tree"


def _child(api: GitHubApi | CommandReads, entry: tuple[str, str, str] | None,
           name: str) -> tuple[str, str, str] | None:
    """The entry ``name`` of the tree ``entry``, or None when ``entry`` is no tree or lacks it."""

    if entry is None or entry[1] != "tree":
        return None
    for row in exact_tree(api, entry[2], recursive=False):
        if row["path"] == name:
            return row["mode"], row["type"], row["sha"]
    return None


def changed_protected_path(api: GitHubApi | CommandReads, *, base_sha: str, tested_tree: str,
                           protected_paths: list[str] | tuple[str, ...]) -> str | None:
    """The first of ``protected_paths`` whose tree entry differs between the commit ``base_sha`` and
    the tree ``tested_tree``, or None when the pull request changes none of them."""

    grammar.require_sha1(tested_tree, "tested tree")
    base_root = commit_tree(api, base_sha)
    for root in protected_paths:
        base: tuple[str, str, str] | None = (*_ROOT, base_root)
        tested: tuple[str, str, str] | None = (*_ROOT, tested_tree)
        for name in root.split("/"):
            if base == tested:
                break
            base, tested = _child(api, base, name), _child(api, tested, name)
        if base != tested:
            return root
    return None


def head_decisions(api: GitHubApi | CommandReads, *, pr_number: int, head_sha: str, command: str,
                   approvers: list[dict[str, Any]], members: OrgMembers | None = None) -> list[dict[str, Any]]:
    """Every decision on ``head_sha`` among the comments of pull request ``pr_number``, oldest
    first: ``{id, decision, created_at, edited, late}`` for each comment whose text is exactly
    ``<command> approve|revoke <head_sha>``, written by a ``User`` and not through a GitHub App,
    whose author is a user of ``approvers`` with the association listed for them or, when
    ``approvers`` name an organization and ``members`` is its snapshot, one of its members. ``late``
    marks a decision by an author no user entry admits that the snapshot did not read as one:
    posted after it (an id above ``members.through_comment_id``), or by a checked author who was no
    candidate, so the comment became a decision by an edit since. Its author is not known to be a
    member yet. At most ``limits.MAX_CI_ADMISSION_COMMENTS`` comments are read.

    A comment is skipped for its author's type and for ``performed_via_github_app`` before its text
    is read, as the ``members`` job skips it before it records its author: the job checks every
    author that remains, whatever they wrote, so an author the snapshot misses below its
    ``through_comment_id`` is an inconsistency and raises."""

    grammar.require_sha1(head_sha, "head SHA")
    users = {(approver["login"].casefold(), approver["association"]) for approver in approvers
             if "login" in approver}
    organization = admission_organization({"approvers": approvers})
    if members is not None and members.organization != organization:
        raise MembershipError("an organization members snapshot was passed for an admission that names no such "
                              "organization")
    texts = {f"{command} approve {head_sha}": "approve", f"{command} revoke {head_sha}": "revoke"}
    rows = api.paginate(f"/repos/{api.repository}/issues/{pr_number}/comments", field=None,
                        max_items=limits.MAX_CI_ADMISSION_COMMENTS)
    decisions = []
    for index, row in enumerate(rows):
        where = f"$.comments[{index}]"
        check(type(row.get("id")) is int and row["id"] > 0 and type(row.get("body")) is str, where,
              "a comment of the pull request is malformed")
        user = row.get("user")
        # The members job's own filters, in its order: a user account, no comment through an App.
        if type(user) is not dict or user.get("type") != "User" or row.get("performed_via_github_app") is not None:
            continue
        decision = texts.get(row["body"].strip())
        if decision is None:
            continue
        check(type(user.get("login")) is str and type(user.get("id")) is int and user["id"] > 0, f"{where}.user",
              "the author of a decision is malformed")
        late = False
        if (user["login"].casefold(), row.get("author_association")) not in users:
            if organization is None or members is None:
                continue
            if row["id"] > members.through_comment_id:
                late = True
            elif user["id"] not in members.checked:
                raise MembershipError("the organization members snapshot did not check the author of a decision it "
                                      "should have read")
            elif user["id"] not in members.candidates:
                # Its comment was no decision when the snapshot read it, so it was edited since,
                # and the author's membership was not read.
                late = True
            elif user["id"] not in members.members:
                continue
        created = grammar.normalize_timestamp(row.get("created_at"), f"{where}.created_at")
        updated = grammar.normalize_timestamp(row.get("updated_at"), f"{where}.updated_at")
        decisions.append({"id": row["id"], "decision": decision, "created_at": created, "edited": updated != created,
                          "late": late})
    return sorted(decisions, key=lambda item: (item["created_at"], item["id"]))


def admission_refusal(api: GitHubApi | CommandReads, watch: Watch, *, config: BuildConfig,
                      plan: dict[str, Any], members: OrgMembers | None = None) -> str | None:
    """Why the gates of the generation ``plan`` may not succeed under the protected admission of
    ``config``, or None when they may: the config names no admission, the pull request changes no
    protected path, or the latest approver decision on its head approves it.

    ``plan`` is the verified plan of the evaluation, so its head, base and tested tree are those of
    the live pull request. ``members`` is the organization members snapshot of the status caller,
    or None when it passed none: an admission that names an organization then refuses a change to a
    protected path without reading any decision. The decisions are read through ``watch``, which the evaluation
    rechecks before it returns its document."""

    policy = admission(config.data)
    if policy is None:
        return None
    identity = plan["identity"]
    changed = changed_protected_path(api, base_sha=identity["base_sha"], tested_tree=identity["tested_tree"],
                                     protected_paths=policy["protected_paths"])
    if changed is None:
        return None
    if admission_organization(policy) is not None and members is None:
        # The members job failed (or no caller passed its snapshot). A member's revocation could
        # then not be counted, so no decision is, a listed user's included, and no comment is read.
        return f"admission: {changed} is protected; the organization's members were not read; evaluate again"
    command = policy["command"]
    decisions = watch.read(("approver decisions on the pull request head",), functools.partial(
        head_decisions, api, pr_number=identity["pr_number"], head_sha=identity["head_sha"], command=command,
        approvers=policy["approvers"], members=members))
    if not decisions:
        return f"admission: {changed} is protected; needs `{command} approve <head sha>` from an approver"
    if any(decision["edited"] for decision in decisions):
        return f"admission: a `{command}` decision on this head was edited; delete it and decide again"
    if any(decision["late"] for decision in decisions):
        return f"admission: a `{command}` decision on this head is newer than its members snapshot; evaluate again"
    if decisions[-1]["decision"] != "approve":
        return f"admission: the latest `{command}` decision on this head revokes it"
    return None
