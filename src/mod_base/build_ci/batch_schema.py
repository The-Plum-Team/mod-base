"""The closed batch manifest (K5): what a batch claims, as plain data.

A manifest travels in the body of its batch pull request, where anyone with write access can edit
it, so it is a hint. ``batch.rebuild_batch`` decides whether it is true: it builds the stack again
from the base, the member heads, the titles and the order named here and requires every id to be
the one written here. This module only checks the shape and what the shape alone can contradict.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.model import grammar as g, limits as lim
from mod_base.model.validators import Const, Int, List, Obj, Str, check


_SHA1 = Str(g.SHA1, max_len=40)
_BRANCH = Str(g.BRANCH, max_len=200)
_MEMBER = Obj({"pr_number": Int(1, lim.MAX_RUN_ID),
               "title": Str(max_len=lim.MAX_CI_BATCH_TITLE_CHARS, text="evidence"),
               "head_sha": _SHA1, "head_tree": _SHA1, "merge_base_sha": _SHA1,
               "patch_sha256": Str(g.SHA256, max_len=64), "squash_sha": _SHA1, "result_tree": _SHA1})
_BATCH = Obj({"kind": Const("mod-base.ci.batch"),
              "schema_version": Int(min(readable_schema_versions("mod-base.ci.batch")),
                                    max(readable_schema_versions("mod-base.ci.batch"))),
              "repository": Str(g.REPOSITORY, max_len=201), "base_branch": _BRANCH,
              "base_sha": _SHA1, "base_tree": _SHA1, "branch": _BRANCH,
              "members": List(_MEMBER, min_items=1, max_items=lim.MAX_CI_BATCH_MEMBERS),
              "result_tree": _SHA1})


def validate_batch_manifest(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Validate the closed ``mod-base.ci.batch`` shape and return the same object.

    Beyond exact keys, types and bounds: ``branch`` is a batch branch and ``base_branch`` is not;
    member numbers and heads are distinct; a member has commits of its own; every squash commit is
    a new commit, distinct from the base, from every head and merge base and from the other squash
    commits; each member's result differs from the tree before it; and the final result is the
    last member's.
    """

    _BATCH(document, path)
    check(g.is_batch_branch(document["branch"]), f"{path}.branch", "must be a batch/<name> branch")
    check(not document["base_branch"].startswith(g.CI_BATCH_BRANCH_PREFIX), f"{path}.base_branch",
          "a batch is never based on a batch branch")
    members = document["members"]
    existing = {document["base_sha"]} | {member[key] for member in members for key in ("head_sha", "merge_base_sha")}
    numbers: set[int] = set()
    heads: set[str] = set()
    squashes: set[str] = set()
    previous_tree = document["base_tree"]
    for index, member in enumerate(members):
        here = f"{path}.members[{index}]"
        check(member["pr_number"] not in numbers, f"{here}.pr_number", "duplicate batch member")
        numbers.add(member["pr_number"])
        check(member["head_sha"] not in heads, f"{here}.head_sha", "two members share one head")
        heads.add(member["head_sha"])
        check(member["merge_base_sha"] != member["head_sha"], f"{here}.merge_base_sha",
              "a member needs commits of its own")
        check(member["squash_sha"] not in existing and member["squash_sha"] not in squashes,
              f"{here}.squash_sha", "a squash commit is a new commit of this batch")
        squashes.add(member["squash_sha"])
        check(member["result_tree"] != previous_tree, f"{here}.result_tree", "a member must change the stack")
        previous_tree = member["result_tree"]
    check(document["result_tree"] == previous_tree, f"{path}.result_tree", "must be the last member's result")
    return document
