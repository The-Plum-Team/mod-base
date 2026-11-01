"""``mod-base.release.ledger`` v1: the durable publication state of one tag of a release.

A tag's draft GitHub release carries its ledger at the very end of its body, as one hidden block
``<!-- mod-base-release-ledger:v1`` + newline + the ledger's canonical JSON (which ends in a
newline) + ``-->``. The body is the plan's changelog text, a blank line and the block
(:func:`render_body`), at most :data:`MAX_RELEASE_BODY_BYTES`, GitHub's bound. When the changelog
does not fit beside the largest ledger the tag can ever have, it is cut after its last whole line
that fits and followed by a blank line and :data:`TRUNCATION_LINE`; the cut depends on the plan and
the tag only, so the text part of a body never changes while its ledger advances.

The ledger has one row per lane of the tag and marketplace of the plan, keyed
``<lane>--<marketplace>`` (a lane id never holds ``--``). A row is ``unstarted``, ``uploading``
(the upload intent, written before a file is sent), ``pending`` (accepted, awaiting moderation) or
``verified``, with the marketplace's id of the file: null while unstarted, required once verified.
:func:`advance` allows only :data:`TRANSITIONS`; a verified row never changes again, and a row
never returns to ``uploading``, so a lost upload response is never sent a second time.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from mod_base.io.secure_json import loads
from mod_base.model import grammar as g
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Map, Nullable, Obj, Str, check
from mod_base.release.config import MARKETPLACES, MAX_RELEASE_LANES, MAX_RELEASE_TAG_CHARS, is_tag, release_header
from mod_base.release.plan import MAX_RELEASE_PLAN_BYTES, plan_digest, validate_release_plan

# Bounds (they move to model/limits.py with the release component).
#: UTF-8 bytes of a GitHub release body, GitHub's own bound.
MAX_RELEASE_BODY_BYTES = 125_000
#: Rows of one ledger: every lane of a release in one tag, on both marketplaces.
MAX_RELEASE_LEDGER_ROWS = len(MARKETPLACES) * MAX_RELEASE_LANES
#: Characters of a marketplace's id of an uploaded file (a Modrinth version id, a CurseForge file id).
MAX_RELEASE_REMOTE_ID_CHARS = 80
#: The plan size bound, for a consumer that checks a downloaded plan before reading it.
MAX_PLAN_ASSET_BYTES = MAX_RELEASE_PLAN_BYTES

#: The first line of the ledger block; nothing else in a body may hold its prefix :data:`MARKER`.
BLOCK_START = "<!-- mod-base-release-ledger:v1"
MARKER = "<!-- mod-base-release-ledger:"
BLOCK_END = "-->"
#: The line that ends a changelog cut to fit beside the ledger.
TRUNCATION_LINE = "Full changelog: CHANGELOG.md"
STATES = ("unstarted", "uploading", "pending", "verified")
#: ``(from, to)`` pairs :func:`advance` allows. ``unstarted`` -> ``verified`` records a file that
#: reconciliation found already published; ``pending`` -> ``pending`` and ``verified`` ->
#: ``verified`` are allowed as no-ops only, with the same remote id.
#: ``uploading`` -> ``unstarted`` is the one way back: the marketplace refused the upload with a
#: definitive answer (an outcome that may hide an accepted upload keeps the row fenced instead).
TRANSITIONS = frozenset({("unstarted", "uploading"), ("uploading", "pending"), ("uploading", "verified"),
                         ("pending", "verified"), ("unstarted", "verified"), ("uploading", "unstarted")})
#: A marketplace's id of an uploaded file.
REMOTE_ID = re.compile(r"^[A-Za-z0-9]{1,80}$")
_ROW_KEY = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}--(?:modrinth|curseforge)$")

_ROW = Obj({"state": Str(choices=STATES),
            "remote_id": Nullable(Str(REMOTE_ID, max_len=MAX_RELEASE_REMOTE_ID_CHARS))})


def _tag(value: Any, path: str) -> str:
    check(isinstance(value, str) and len(value) <= MAX_RELEASE_TAG_CHARS and is_tag(value), path,
          "must be a Git tag of the release tag grammar")
    return value


_LEDGER = Obj({
    **release_header("mod-base.release.ledger"),
    "tag": _tag,
    "plan_sha256": Str(g.SHA256, max_len=64),
    "rows": Map(_ROW_KEY, _ROW, min_items=1, max_items=MAX_RELEASE_LEDGER_ROWS, max_key_len=92),
})


def row_key(lane: str, marketplace: str) -> str:
    return f"{lane}--{marketplace}"


def _plan_tag(plan: dict[str, Any], tag: str) -> dict[str, Any]:
    entry = next((item for item in plan["tags"] if item["tag"] == tag), None)
    check(entry is not None, "$.tag", "is not a tag of the plan")
    return entry


def expected_rows(plan: dict[str, Any], tag: str) -> list[str]:
    """The row keys of ``tag`` in a validated plan: its lanes by the plan's marketplaces."""

    return [row_key(lane, marketplace) for lane in _plan_tag(plan, tag)["lanes"]
            for marketplace in MARKETPLACES if marketplace in plan["lanes"][lane]]


def validate_ledger(document: Any, *, plan: dict[str, Any] | None = None, tag: str | None = None,
                    path: str = "$") -> dict[str, Any]:
    """The closed schema and the row rules; with ``tag``, the ledger is that tag's; with ``plan``,
    it is of exactly that plan (:func:`mod_base.release.plan.plan_digest`), its tag is one of the
    plan's and its rows are exactly that tag's lanes on the plan's marketplaces."""

    _LEDGER(document, path)
    for key, row in document["rows"].items():
        lane = key.rsplit("--", 1)[0]
        check(g.CI_UNIT_ID.fullmatch(lane) is not None, f"{path}.rows", f"has an invalid lane in {key!r}"[:200])
        if row["state"] == "unstarted":
            check(row["remote_id"] is None, f"{path}.rows.{key}", "an unstarted row has no remote id")
        if row["state"] == "verified":
            check(row["remote_id"] is not None, f"{path}.rows.{key}", "a verified row names its remote id")
    if tag is not None:
        check(document["tag"] == tag, f"{path}.tag", "is not the ledger of this tag")
    if plan is not None:
        check(document["plan_sha256"] == plan_digest(plan), f"{path}.plan_sha256", "is not the ledger of this plan")
        check(sorted(document["rows"]) == sorted(expected_rows(plan, document["tag"])), f"{path}.rows",
              "must hold exactly one row per lane of the tag and marketplace of the plan")
    return document


def new_ledger(plan: dict[str, Any], tag: str) -> dict[str, Any]:
    """The ledger of ``tag`` before anything is published: every row ``unstarted``."""

    validate_release_plan(plan)
    document = {"kind": "mod-base.release.ledger", "schema_version": 1, "tag": tag, "plan_sha256": plan_digest(plan),
                "rows": {key: {"state": "unstarted", "remote_id": None} for key in expected_rows(plan, tag)}}
    return validate_ledger(document, plan=plan, tag=tag)


def _block(ledger: dict[str, Any]) -> str:
    return BLOCK_START + "\n" + canonical_json(ledger).decode("utf-8") + BLOCK_END


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _notes(plan: dict[str, Any], tag: str) -> str:
    """The changelog part of the body of ``tag``: the whole text when it fits beside the largest
    block the tag's ledger can ever render, else its longest prefix of whole lines that fits with
    a blank line and :data:`TRUNCATION_LINE` after it."""

    largest = {"kind": "mod-base.release.ledger", "schema_version": 1, "tag": tag, "plan_sha256": "0" * 64,
               "rows": {key: {"state": max(STATES, key=len), "remote_id": "0" * MAX_RELEASE_REMOTE_ID_CHARS}
                        for key in expected_rows(plan, tag)}}
    budget = MAX_RELEASE_BODY_BYTES - _size("\n\n" + _block(largest))
    text = plan["changelog"]["text"]
    if _size(text) <= budget:
        return text
    suffix = "\n\n" + TRUNCATION_LINE
    check(_size(TRUNCATION_LINE) <= budget, "$.body", "the ledger leaves no room for the changelog line")
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        cost = _size(line) + (1 if kept else 0)
        if used + cost + _size(suffix) > budget:
            break
        kept.append(line)
        used += cost
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + suffix if kept else TRUNCATION_LINE


def render_body(plan: dict[str, Any], tag: str, ledger: dict[str, Any]) -> str:
    """The body of ``tag``'s release: the changelog (cut as the module describes), a blank line and
    the ledger block."""

    validate_release_plan(plan)
    validate_ledger(ledger, plan=plan, tag=tag)
    body = _notes(plan, tag) + "\n\n" + _block(ledger)
    check(_size(body) <= MAX_RELEASE_BODY_BYTES, "$.body", f"exceeds {MAX_RELEASE_BODY_BYTES} bytes")
    return body


def parse_body(body: Any, *, plan: dict[str, Any] | None = None, tag: str | None = None) -> dict[str, Any]:
    """The ledger of a release body: exactly one block, at the very end, after a blank line, whose
    JSON is the canonical encoding of a valid ledger. With ``plan`` the ledger must be of that plan
    (and of ``tag`` when given) and the whole body exactly what :func:`render_body` writes for it,
    so an edited changelog part is refused as well."""

    check(isinstance(body, str), "$.body", "must be a string")
    try:
        size = _size(body)
    except UnicodeEncodeError:
        size = MAX_RELEASE_BODY_BYTES + 1
    check(size <= MAX_RELEASE_BODY_BYTES, "$.body", f"must be valid UTF-8 of at most {MAX_RELEASE_BODY_BYTES} bytes")
    check(body.count(MARKER) == 1, "$.body", "must hold exactly one release ledger block")
    start = body.index(MARKER)
    check(body.startswith(BLOCK_START + "\n", start), "$.body", "holds an unsupported release ledger block")
    check(body.endswith(BLOCK_END) and start >= 2 and body[start - 2:start] == "\n\n", "$.body",
          "the ledger block must follow a blank line and end the body")
    data = body[start + len(BLOCK_START) + 1:len(body) - len(BLOCK_END)].encode("utf-8")
    ledger = loads(data, label="release ledger", max_bytes=MAX_RELEASE_BODY_BYTES)
    check(canonical_json(ledger) == data, "$.body", "the ledger must be canonical JSON")
    if plan is None:
        return validate_ledger(ledger, tag=tag)
    validate_release_plan(plan)
    validate_ledger(ledger, plan=plan, tag=tag)
    check(body == render_body(plan, ledger["tag"], ledger), "$.body", "is not the body of this plan and ledger")
    return ledger


def advance(ledger: dict[str, Any], row: str, state: str, remote_id: str | None = None) -> dict[str, Any]:
    """A copy of ``ledger`` with ``row`` moved to ``state`` (see :data:`TRANSITIONS`). A remote id
    once recorded never changes, a move to ``uploading`` records none and a move to ``verified``
    requires one."""

    validate_ledger(ledger)
    check(row in ledger["rows"], "$.row", "is not a row of the ledger")
    check(state in STATES, "$.state", f"must be one of {list(STATES)}")
    check(remote_id is None or (isinstance(remote_id, str) and REMOTE_ID.fullmatch(remote_id) is not None),
          "$.remote_id", "must be null or a marketplace file id")
    current = ledger["rows"][row]
    if (current["state"], state) not in TRANSITIONS:
        check(current["state"] == state and state in ("pending", "verified") and current["remote_id"] == remote_id,
              "$.state", f"cannot move {row} from {current['state']} to {state}"[:200])
    check(state != "uploading" or remote_id is None, "$.remote_id", "an upload intent has no remote id yet")
    check(current["remote_id"] is None or remote_id == current["remote_id"], "$.remote_id",
          "differs from the remote id the row already records")
    result = copy.deepcopy(ledger)
    result["rows"][row] = {"state": state, "remote_id": remote_id}
    return validate_ledger(result)


def complete(ledger: dict[str, Any]) -> bool:
    """Whether every row of a valid ledger is verified: the tag's release may be published."""

    validate_ledger(ledger)
    return all(row["state"] == "verified" for row in ledger["rows"].values())
