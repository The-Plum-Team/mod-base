"""The organization members snapshot of a protected admission (``mod-base.ci.org-members``, MB11).

An admission whose approvers name the organization that owns the repository
(:func:`mod_base.build_ci.config.admission_organization`) counts a decision of every active member
of it. Neither the evaluation's read-only job token nor a comment's ``author_association`` can
prove a membership the member keeps private, so the status caller's ``members`` job proves it: it
runs only when the protected config at the base names such an approver, reads the comments of the
pull request with the job token, mints a token of the gate App that may read the organization's
members, and hands the evaluation one document:

* ``checked``: the numeric id of every author of a comment it read that is a ``User`` and posted
  no comment through a GitHub App, whatever the comment says. No body grammar is applied, so no
  decision the kit recognises can come from an author the job did not check;
* ``members``: those of ``checked`` that are active members of the organization (a pending
  invitation is no membership), compared by id, never by login;
* ``through_comment_id``: the greatest comment id among the comments it read (0 for none). A
  decision with a greater id was posted after the snapshot: the admission refuses and the next
  evaluation decides it (:mod:`mod_base.build_ci.admission`).

The job never prints the organization's member list: only the ids of ``checked`` authors appear,
and only that subset of the list leaves it. The document is no trust root of its own: the
evaluation accepts it only for this repository's owner and this pull request, in its canonical
bytes, with ``members`` a subset of ``checked``; and the publishing job reads the membership of the
same ``checked`` ids again before it writes a status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import readable_schema_versions
from mod_base.errors import MbError
from mod_base.io.secure_json import loads
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_regular_file
from mod_base.model.validators import Const, Int, List, Obj, Str, check

ORG_MEMBERS_KIND = "mod-base.ci.org-members"
#: An organization login, as GitHub spells a login.
_ORGANIZATION = Str(re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"), max_len=39)
_IDS = List(Int(1, lim.MAX_RUN_ID), max_items=lim.MAX_CI_ADMISSION_COMMENTS, sorted_values=True)
_ORG_MEMBERS = Obj({
    "kind": Const(ORG_MEMBERS_KIND),
    "schema_version": Int(min(readable_schema_versions(ORG_MEMBERS_KIND)),
                          max(readable_schema_versions(ORG_MEMBERS_KIND))),
    "organization": _ORGANIZATION,
    "pr_number": Int(1, lim.MAX_RUN_ID),
    "through_comment_id": Int(0, lim.MAX_RUN_ID),
    "checked": _IDS,
    "members": _IDS,
})


class MembershipError(MbError):
    """The organization members snapshot cannot be used for this evaluation (exit 2); no intent is
    produced."""

    default_reason = "ci-status"


def validate_org_members(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.ci.org-members`` v1: user ids strictly ascending, at most
    ``MAX_CI_ADMISSION_COMMENTS`` of each, and every member among the checked ids."""

    _ORG_MEMBERS(document, path)
    check(set(document["members"]) <= set(document["checked"]), f"{path}.members",
          "names a user the snapshot did not check")
    return document


@dataclass(frozen=True)
class OrgMembers:
    """A validated snapshot: the organization, the pull request, the greatest comment id it read
    and the user ids it checked, with the active members among them."""

    organization: str
    pr_number: int
    through_comment_id: int
    checked: frozenset[int]
    members: frozenset[int]


def load_org_members(path: Path, *, repository: str, pr_number: int) -> OrgMembers:
    """Read the snapshot the status caller's ``members`` job wrote to ``path`` for pull request
    ``pr_number`` of ``repository``: a regular file of at most ``MAX_CI_ORG_MEMBERS_BYTES``, in
    canonical bytes, valid, of the organization that owns ``repository`` and of that pull request."""

    raw = read_regular_file(path, label="organization members snapshot", max_bytes=lim.MAX_CI_ORG_MEMBERS_BYTES)
    document = validate_org_members(loads(raw, label="organization members snapshot",
                                          max_bytes=lim.MAX_CI_ORG_MEMBERS_BYTES))
    if raw != canonical_json(document):
        raise MembershipError("the organization members snapshot is not in its canonical bytes")
    if document["organization"] != repository.split("/")[0]:
        raise MembershipError("the organization members snapshot is of another organization than the repository's")
    if document["pr_number"] != pr_number:
        raise MembershipError("the organization members snapshot is of another pull request")
    return OrgMembers(document["organization"], document["pr_number"], document["through_comment_id"],
                      frozenset(document["checked"]), frozenset(document["members"]))
