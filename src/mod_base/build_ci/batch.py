"""Batches of pull requests (K5): construction, verification by rebuilding, settlement.

A batch lands several open same-repository pull requests through one pull request, so that the
complete gates run once for all of them. ``prepare_batch`` squashes each member, in the given
order, onto the live head of the default branch (``batch_git``), pushes the stack as a new
``batch/<name>`` branch and opens one ready pull request for it. ``settle_batch`` closes the
members after that pull request merged.

The stack is reproducible: its commit ids depend only on the base commit and on each member's
number, title and head commit, in order. The batch pull request's body carries those in one
marker line, ``<!-- mod-base-batch {manifest} -->``. Anyone who can edit the body can edit the
marker, so it is never believed: ``rebuild_batch`` builds the stack again from what the marker
names and requires the result to equal the marker in full. A commit that holds anything but its
member's patch, a wrong member, title, head, base or order all give other ids.

Members are pull requests of this repository only (code of a fork never enters a branch of the
repository), open, based on the default branch, and not themselves a batch or the base branch.
At most 50 are batched, in the order given. The path policy is the caller's: the allowed-path
list of ``prepare_batch`` comes from the mod's protected policy, never from a member.

The token is the invocation's; the kit mints none. A pull request opened with the default
``GITHUB_TOKEN`` of a workflow starts no workflow runs, so a batch opened with it would never be
gated: the caller must supply the token of a GitHub App or of an automation account that may
push ``batch/*`` branches, open pull requests and, for settlement, close them.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci import reuse
from mod_base.build_ci.batch_git import BatchStack, BatchStore, StackMember, allowed_path_roots, patch_sha256
from mod_base.build_ci.batch_schema import validate_batch_manifest
from mod_base.build_ci.protocol import PRODUCERS, validate_plan
from mod_base.build_ci.reads import CommandReads, Watch
from mod_base.build_ci.records import original_plan
from mod_base.build_ci.transport import OriginalUnavailable, download_merged_gate_pair
from mod_base.errors import Unavailable
from mod_base.github.api import ApiNotFound, GitHubApi
from mod_base.github.contents import branch_head, default_branch
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import Int, check, is_evidence_text

BATCH_BRANCH_PREFIX = grammar.CI_BATCH_BRANCH_PREFIX
BATCH_KIND = "mod-base.ci.batch"
_MARKER_OPEN = "<!-- mod-base-batch "
_MARKER_CLOSE = " -->"


@dataclass(frozen=True)
class BatchMember:
    """One member pull request as the API reports it now."""

    number: int
    title: str
    head_branch: str
    head_sha: str


@dataclass(frozen=True)
class BatchObservation:
    """The live state a batch is built on: the default branch head and the ordered members."""

    repository: str
    base_branch: str
    base_sha: str
    base_tree: str
    members: tuple[BatchMember, ...]


def _pull(api: GitHubApi, number: int) -> dict[str, Any]:
    record = api.get_json(f"/repos/{api.repository}/pulls/{number}")
    check(type(record) is dict and type(record.get("number")) is int and record["number"] == number
          and type(record.get("state")) is str, "$.batch.pull", f"#{number} is not a pull request")
    return record


def _side(record: dict[str, Any], side: str) -> tuple[Any, Any, Any]:
    """``(repository, ref, sha)`` of the ``head`` or ``base`` of a pull request, ``None`` where absent."""

    value = record.get(side)
    if type(value) is not dict:
        return None, None, None
    repository = value.get("repo")
    return (repository.get("full_name") if type(repository) is dict else None), value.get("ref"), value.get("sha")


def observe_batch(api: GitHubApi, *, pr_numbers: tuple[int, ...]) -> BatchObservation:
    """Read the default branch head and every member once, and refuse what cannot be batched.

    A member is open, of this repository on both sides, based on the default branch, not itself a
    batch or the base branch, and has a printable title. The numbers are checked before any read.
    """

    grammar.require(grammar.REPOSITORY, api.repository, "batch repository")
    check(type(pr_numbers) is tuple and 1 <= len(pr_numbers) <= limits.MAX_CI_BATCH_MEMBERS,
          "$.batch.members", f"a batch needs between 1 and {limits.MAX_CI_BATCH_MEMBERS} pull requests")
    for number in pr_numbers:
        Int(1, limits.MAX_RUN_ID)(number, "$.batch.members")
    check(len(set(pr_numbers)) == len(pr_numbers), "$.batch.members", "duplicate batch member")
    base_branch = default_branch(api)
    base_sha, base_tree = branch_head(api, base_branch)
    members = []
    for number in pr_numbers:
        record, label = _pull(api, number), f"#{number}"
        check(record["state"] == "open" and record.get("merged_at") is None, "$.batch.members", f"{label} is not open")
        base_repository, base_ref, _ = _side(record, "base")
        check(base_repository == api.repository and base_ref == base_branch, "$.batch.members",
              f"{label} does not target {api.repository}:{base_branch}")
        head_repository, head_ref, head_sha = _side(record, "head")
        check(head_repository == api.repository, "$.batch.members",
              f"{label} comes from a fork; review it and land it on its own")
        check(grammar.is_match(grammar.BRANCH, head_ref) and grammar.is_match(grammar.SHA1, head_sha),
              "$.batch.members", f"{label} has no valid head")
        check(head_ref != base_branch and not head_ref.startswith(BATCH_BRANCH_PREFIX), "$.batch.members",
              f"{label} is itself a batch or base branch")
        title = record.get("title")
        check(is_evidence_text(title, limits.MAX_CI_BATCH_TITLE_CHARS), "$.batch.members",
              f"{label} needs a printable title of at most {limits.MAX_CI_BATCH_TITLE_CHARS} characters")
        members.append(BatchMember(number, title, head_ref, head_sha))
    return BatchObservation(api.repository, base_branch, base_sha, base_tree, tuple(members))


def _require_unchanged(api: GitHubApi, observed: BatchObservation, when: str) -> None:
    current = observe_batch(api, pr_numbers=tuple(member.number for member in observed.members))
    check((current.base_branch, current.base_sha, current.base_tree)
          == (observed.base_branch, observed.base_sha, observed.base_tree),
          "$.batch.base", f"{observed.base_branch} moved {when}")
    for before, now in zip(observed.members, current.members):
        check(before == now, "$.batch.members", f"#{before.number} changed {when}")


def _batch_ref(api: GitHubApi, branch: str, expected: str | None) -> None:
    """Require the batch branch to be absent (``expected`` is ``None``) or to be ``expected``."""

    endpoint = f"/repos/{api.repository}/git/ref/heads/{branch}"
    try:
        value = api.get_json(endpoint)
    except ApiNotFound as error:
        # Only the 404 of exactly this read is absence; any other failure stays a failure.
        if expected is None and (error.status, error.method, error.path) == (404, "GET", endpoint):
            return
        raise
    check(expected is not None, "$.batch.branch", f"{branch} already exists; a batch never reuses a branch")
    target = value.get("object") if type(value) is dict else None
    check(type(target) is dict and value.get("ref") == "refs/heads/" + branch
          and target.get("type") == "commit" and target.get("sha") == expected,
          "$.batch.branch", f"{branch} is not the pushed batch commit")


def _manifest(repository: str, base_branch: str, branch: str, stack: BatchStack) -> dict[str, Any]:
    return validate_batch_manifest({
        "kind": BATCH_KIND, "schema_version": SCHEMA_VERSIONS[BATCH_KIND], "repository": repository,
        "base_branch": base_branch, "base_sha": stack.base_sha, "base_tree": stack.base_tree, "branch": branch,
        "members": [{"pr_number": commit.member.number, "title": commit.member.title,
                     "head_sha": commit.member.head_sha, "head_tree": commit.head_tree,
                     "merge_base_sha": commit.merge_base_sha, "patch_sha256": patch_sha256(commit.patch),
                     "squash_sha": commit.squash_sha, "result_tree": commit.result_tree}
                    for commit in stack.commits],
        "result_tree": stack.commits[-1].result_tree})


def batch_marker(manifest: dict[str, Any]) -> str:
    """The one line that carries ``manifest`` in the body of its batch pull request.

    The canonical JSON of the manifest with every character outside printable ASCII and every
    ``<``, ``>`` and ``&`` written as a JSON escape: the line is an HTML comment that no title can
    close, and it has exactly one spelling.
    """

    validate_batch_manifest(manifest)
    text = []
    for character in canonical_json(manifest).decode("utf-8").rstrip("\n"):
        code = ord(character)
        if 32 <= code < 127 and character not in "<>&":
            text.append(character)
        elif code < 0x10000:
            text.append(f"\\u{code:04x}")
        else:
            code -= 0x10000
            text.append(f"\\u{0xd800 + (code >> 10):04x}\\u{0xdc00 + (code & 0x3ff):04x}")
    return _MARKER_OPEN + "".join(text) + _MARKER_CLOSE


def read_batch_marker(body: object) -> dict[str, Any]:
    """The manifest of the single marker line of a batch pull request body: a hint, never proof."""

    check(type(body) is str and len(body) <= limits.MAX_CI_BATCH_DOCUMENT_BYTES, "$.batch.body",
          "the batch pull request has no body within its bound")
    check(body.count(_MARKER_OPEN) == 1, "$.batch.marker", "the batch pull request body has no single batch marker")
    start = body.index(_MARKER_OPEN)
    end = body.find("\n", start)
    line = (body[start:] if end < 0 else body[start:end]).removesuffix("\r")
    check((start == 0 or body[start - 1] == "\n") and line.endswith(_MARKER_CLOSE) and line.isascii(),
          "$.batch.marker", "the batch marker is not one whole line")
    manifest = validate_batch_manifest(strict_loads(
        line[len(_MARKER_OPEN):-len(_MARKER_CLOSE)].encode("ascii"), label="batch marker",
        max_bytes=limits.MAX_CI_BATCH_DOCUMENT_BYTES))
    check(batch_marker(manifest) == line, "$.batch.marker", "the batch marker is not in its canonical spelling")
    return manifest


def _cell(text: str) -> str:
    """Text for one Markdown table cell that can open neither a column nor an HTML tag."""

    for character, escaped in (("&", "&amp;"), ("\\", "\\\\"), ("|", "\\|"), ("<", "&lt;"), (">", "&gt;")):
        text = text.replace(character, escaped)
    return text


def _body(manifest: dict[str, Any]) -> str:
    members = manifest["members"]
    rows = "\n".join(f"| #{member['pr_number']} | {_cell(member['title'])} | `{member['head_sha'][:12]}` "
                     f"| `{member['squash_sha'][:12]}` |" for member in members)
    listed = ", ".join(f"#{member['pr_number']}" for member in members)
    body = (
        "## Summary\n\n"
        f"Batch of {len(members)} pull request(s), validated together by the required gates of this pull "
        f"request. Each entry is one squashed commit on `{manifest['branch']}`, so `git bisect` over the "
        "batch isolates a failure.\n\n"
        "| PR | Title | Batched head | Batch commit |\n|---|---|---|---|\n"
        f"{rows}\n\n"
        "After this pull request merges, `ci batch-settle` closes the included pull requests whose head is "
        "still the batched commit.\n\n"
        "## Scope\n\n"
        f"- Base branch: `{manifest['base_branch']}` at `{manifest['base_sha'][:12]}`\n"
        f"- Included pull requests: {listed}\n"
        "- Scope, validation notes and AI assistance are recorded in each included pull request.\n\n"
        "## Validation\n\n"
        "The required gates of this pull request validate the combined tree.\n\n"
        f"{batch_marker(manifest)}\n")
    check(len(body.encode("utf-8")) <= limits.MAX_CI_BATCH_DOCUMENT_BYTES, "$.batch.body",
          "the batch description exceeds the pull request body bound; batch fewer pull requests")
    return body


def prepare_batch(api: GitHubApi, store: BatchStore, *, name: str, pr_numbers: tuple[int, ...],
                  allowed_paths: tuple[str, ...], dry_run: bool = False) -> dict[str, Any]:
    """Build the batch ``batch/<name>`` from ``pr_numbers`` and, unless ``dry_run``, publish it.

    Observe the members and the default branch head, fetch them (a ref that no longer is what the
    API reported has moved), build the stack and its manifest. A dry run stops there, having
    written nothing. Otherwise the live state is read again immediately before the push, which can
    only create the branch, and once more before the ready pull request is opened; a base or a
    member that changed in between stops the batch (after the push the branch is left for the
    operator to delete, since nothing was opened for it).

    ``allowed_paths`` is the protected policy's list (``batch_git.allowed_path_roots``). ``api``
    is writable unless ``dry_run``; see the module docstring for the token it needs. Returns
    ``{"dry_run", "manifest", "pr_number"}``; ``pr_number`` is ``None`` for a dry run.
    """

    check(type(dry_run) is bool, "$.batch.dry_run", "must be a boolean")
    check(dry_run or api.writable, "$.batch.api", "publishing a batch needs a writable GitHub client")
    branch = BATCH_BRANCH_PREFIX + grammar.require(grammar.CI_BATCH_NAME, name, "batch name")
    check(grammar.is_batch_branch(branch), "$.batch.name", "does not name a valid batch branch")
    allowed_path_roots(allowed_paths)
    observed = observe_batch(api, pr_numbers=pr_numbers)
    _batch_ref(api, branch, None)
    store.fetch(base_sha=observed.base_sha, base_branch=observed.base_branch,
                heads={member.number: member.head_sha for member in observed.members})
    stack = store.build_stack(
        base_sha=observed.base_sha, base_branch=observed.base_branch, allowed_paths=allowed_paths,
        members=tuple(StackMember(member.number, member.title, member.head_sha) for member in observed.members))
    check(stack.base_tree == observed.base_tree, "$.batch.base", "Git and the API disagree about the base tree")
    manifest = _manifest(observed.repository, observed.base_branch, branch, stack)
    body = _body(manifest)
    result: dict[str, Any] = {"dry_run": dry_run, "manifest": manifest, "pr_number": None}
    if dry_run:
        return result
    head = stack.commits[-1].squash_sha
    _require_unchanged(api, observed, "while the batch was being prepared; run it again")
    _batch_ref(api, branch, None)
    store.publish(branch=branch, commit_sha=head)
    _require_unchanged(api, observed, f"after {branch} was pushed; delete that branch and run it again")
    _batch_ref(api, branch, head)
    numbers = ", ".join(f"#{member.number}" for member in observed.members)
    title = f"chore: batch {numbers}" if len(numbers) <= 80 else f"chore: batch {len(observed.members)} pull requests"
    created = api.post_json(f"/repos/{api.repository}/pulls", {
        "title": title, "head": branch, "base": observed.base_branch, "body": body, "draft": False})
    check(type(created) is dict and type(created.get("number")) is int and created["number"] > 0
          and _side(created, "head")[2] == head, "$.batch.pull",
          f"GitHub did not open the batch pull request for {branch}")
    result["pr_number"] = created["number"]
    return result


def rebuild_batch(store: BatchStore, manifest: dict[str, Any]) -> None:
    """Prove ``manifest`` by building its stack again: the verifier of a batch.

    Fetch the base and the member heads by id, build the stack from the manifest's base, numbers,
    titles, heads and order, and require the manifest to equal what that gives, field by field.
    Because a commit id covers its tree, parent, identity, dates and message, an equal last id
    means the published stack is this one: each commit holds its member's patch and nothing else.
    The repository and branch names are not Git's to prove and a shorter stack is a true batch
    too: the caller binds those names and the last commit to the pull request it verifies. No
    path policy applies here; it was the constructor's and the batch pull request's to enforce.
    """

    validate_batch_manifest(manifest)
    members = tuple(StackMember(member["pr_number"], member["title"], member["head_sha"])
                    for member in manifest["members"])
    store.fetch(base_sha=manifest["base_sha"], heads={member.number: member.head_sha for member in members})
    stack = store.build_stack(base_sha=manifest["base_sha"], base_branch=manifest["base_branch"],
                              members=members, allowed_paths=None)
    check(_manifest(manifest["repository"], manifest["base_branch"], manifest["branch"], stack) == manifest,
          "$.batch.manifest", "the batch manifest differs from the stack rebuilt from its base and member heads")


class BatchUnsettled(Unavailable):
    """The gates of a merged batch cannot prove it now (exit 3).

    Its newest Build or packaged run under the batch head is missing, failed, a draft deferral or a
    reuse run, ran another kit pin, was sealed for another plan than the protected policy derives
    now, or an artifact it names has expired. Nothing is wrong and nothing was written: the members
    stay open, for an operator to settle them with the gates' own plan and seals or close them by
    hand."""

    default_reason = "ci-batch-unsettled"


#: The identity fields the Build record of a batch's gates must share with the default-branch
#: commit the settlement runs at: the same repository, protected caller, graph and kit. The tested
#: tree, the policy, the inventory, the scenario contract and the runtime selection are the gates'
#: own; the original plan hash covers what they mean.
_SAME_CALLER = ("repository", "source_repository", "base_branch", "controller_workflow", "controller_ref",
                "graph_version", "kit")


@dataclass(frozen=True)
class BatchGates:
    """The gates of a merged batch pull request, derived and authenticated (:func:`derive_batch_gates`).

    ``plan`` is the plan they ran with, ``build_seal`` and ``packaged_seal`` the descriptors of
    their tested records, and ``watch`` everything mutable the derivation read: whoever acts on
    the gates calls ``watch.recheck()`` immediately before its effect."""

    plan: dict[str, Any]
    build_seal: dict[str, Any]
    packaged_seal: dict[str, Any]
    watch: Watch = field(repr=False, compare=False)


def derive_batch_gates(api: GitHubApi | CommandReads, *, pr_number: int, head_sha: str, head_branch: str,
                       plan: dict[str, Any], temporary_root: Path) -> BatchGates:
    """Locate the gates of the merged batch pull request ``pr_number`` (last head ``head_sha`` on
    ``head_branch``) from ``plan``, the protected plan of the default-branch commit the job runs
    at (KQ8-1): ``ci batch-settle`` without an operator's plan and seals.

    The gates are found as post-merge reuse finds those of a merged pull request
    (``reuse._original_run``): the newest Build run and the newest packaged run GitHub lists under
    the batch head, chosen before any result is read, as their latest attempts, completed and
    successful under the executing kit pin, neither a draft deferral nor a reuse run. The Build
    record says which identity it was sealed for. That is a claim: it must name this pull request
    and its head and the same repository, caller, graph and kit as ``plan``, and the original plan
    is ``plan`` under that identity (``records.original_plan``), whose hash the record must carry,
    so the gates were sealed for exactly the work the protected policy derives now. Each run must
    show exactly the full graph of its caller for that plan and every artifact both records name
    must still be listed. The tested tree is not compared here: the settlement binds it to the
    batch marker and to the merged commit.

    A newest run that is missing, failed, deferred or a reuse run, another kit pin, another plan and
    evidence that is gone raise :class:`BatchUnsettled` (exit 3); a run still in progress raises
    ``reuse.OriginalPending``; malformed or contradictory metadata is a rejection. Nothing is
    written.
    """

    Int(1, limits.MAX_RUN_ID)(pr_number, "$.batch.pull")
    plan = validate_plan(plan)
    now = plan["identity"]
    reads, watch = CommandReads.of(api), Watch()
    check(now["repository"] == now["source_repository"] == reads.repository and now["pr_number"] == 0
          and now["head_branch"] == now["base_branch"] and now["tested_sha"] == now["controller_sha"],
          "$.batch.plan", "the gates of a batch are derived from the protected plan of a default-branch commit")
    check(grammar.is_batch_branch(head_branch) and grammar.is_match(grammar.SHA1, head_sha), "$.batch.pull",
          f"#{pr_number} has no batch head")
    pull = reuse._Pull(pr_number, head_sha, head_branch)
    try:
        build, packaged = (reuse._original_run(reads, watch, producer, pull, plan) for producer in PRODUCERS)
        claim = reuse._claimed_receipt(reads, build.artifact("tested", "build"), temporary_root)
        then = claim["identity"]
        if then["pr_number"] != pr_number:
            raise reuse._FullRun("original-run-of-another-pull-request", f"#{then['pr_number']}, not #{pr_number}")
        if then["kit"] != now["kit"]:
            raise reuse._FullRun("kit-pin-differs", f"pull request #{pr_number}")
        for key in _SAME_CALLER:
            check(then[key] == now[key], f"$.record.identity.{key}",
                  "the batch's Build record is not of this repository's protected caller")
        check((then["head_sha"], then["head_branch"]) == (head_sha, head_branch), "$.record.identity",
              "the batch's Build record names another head than the merged batch pull request")
        original = original_plan(plan, then)
        if claim["plan_sha256"] != original["plan_sha256"]:
            raise reuse._FullRun("plan-differs", f"pull request #{pr_number}")
        # Everything both records will name, by name: what is gone is known before anything is required.
        packaged.artifact("tested", "packaged")
        build.artifact("build")
        for lane in original["lanes"]:
            packaged.artifact("runtime", lane["id"])
        packaged.artifact("results")
        build_seal = reuse._seal_descriptor(original, build)
        packaged_seal = reuse._seal_descriptor(original, packaged)
    except reuse._FullRun as unsettled:
        raise BatchUnsettled(f"batch #{pr_number} cannot be settled from its gates: {unsettled.outcome.detail}",
                             reason=f"ci-batch-{unsettled.outcome.reason}") from None
    except OriginalUnavailable as gone:
        raise BatchUnsettled(f"batch #{pr_number} cannot be settled from its gates: {gone}",
                             reason="ci-batch-evidence-unavailable") from None
    return BatchGates(original, build_seal, packaged_seal, watch)


@dataclass(frozen=True)
class _Settlement:
    """A merged batch whose provenance holds (:func:`_verify_settlement`)."""

    pr_number: int
    repository: str
    base_branch: str
    merged_sha: str
    manifest: dict[str, Any]


def _merged_batch(api: GitHubApi, pr_number: int) -> tuple[dict[str, Any], str, str, str]:
    """``(record, head branch, head sha, base branch)`` of the merged ``batch/*`` pull request ``pr_number``."""

    record = _pull(api, pr_number)
    check(record["state"] == "closed" and record.get("merged") is True, "$.batch.pull",
          f"batch #{pr_number} has not merged")
    head_repository, branch, head_sha = _side(record, "head")
    base_repository, base_branch, _ = _side(record, "base")
    check(head_repository == base_repository == api.repository and grammar.is_batch_branch(branch)
          and grammar.is_match(grammar.SHA1, head_sha), "$.batch.pull",
          f"#{pr_number} is not a batch pull request of {api.repository}")
    return record, branch, head_sha, base_branch


def _verify_settlement(api: GitHubApi, store: BatchStore, *, pr_number: int, plan: dict[str, Any],
                       build_seal: dict[str, Any], packaged_seal: dict[str, Any],
                       temporary_root: Path) -> _Settlement:
    """Everything :func:`settle_batch` proves before its first write; see there."""

    Int(1, limits.MAX_RUN_ID)(pr_number, "$.batch.pull")
    identity = validate_plan(plan)["identity"]
    repository = api.repository
    check(identity["pr_number"] == pr_number and identity["repository"] == identity["source_repository"] == repository,
          "$.batch.plan", f"the plan is not the plan of pull request #{pr_number} of {repository}")
    record, branch, head_sha, base_branch = _merged_batch(api, pr_number)
    merged_sha = grammar.require_sha1(record.get("merge_commit_sha"), "merged batch commit")
    manifest = read_batch_marker(record.get("body"))
    members = manifest["members"]
    check((manifest["repository"], manifest["branch"], manifest["base_branch"]) == (repository, branch, base_branch)
          and pr_number not in {member["pr_number"] for member in members},
          "$.batch.marker", "the batch marker describes another batch")
    check((identity["head_branch"], identity["head_sha"], identity["base_branch"], identity["base_sha"],
           identity["tested_tree"])
          == (branch, head_sha, base_branch, manifest["base_sha"], manifest["result_tree"])
          and head_sha == members[-1]["squash_sha"], "$.batch.plan",
          "the gates did not test the stack of the batch marker on its own base")
    rebuild_batch(store, manifest)
    check(default_branch(api) == base_branch, "$.batch.base", "the batch was not merged to the default branch")
    controller_sha, _ = branch_head(api, base_branch)
    download_merged_gate_pair(api, build_descriptor=build_seal, packaged_descriptor=packaged_seal, plan=plan,
                              controller_sha=controller_sha, merged_sha=merged_sha, temporary_root=temporary_root)
    return _Settlement(pr_number, repository, base_branch, merged_sha, manifest)


def _close_members(api: GitHubApi, settled: _Settlement, *, delete_branches: bool) -> dict[str, list[int]]:
    """Close the members of a verified settlement; see :func:`settle_batch`."""

    repository, base_branch, pr_number = settled.repository, settled.base_branch, settled.pr_number
    report: dict[str, list[int]] = {"closed": [], "changed": [], "already_closed": [], "deleted": []}
    for member in settled.manifest["members"]:
        number, batched = member["pr_number"], member["head_sha"]
        endpoint = f"/repos/{repository}/pulls/{number}"
        live = _pull(api, number)
        if live["state"] != "open":
            report["already_closed"].append(number)
            continue
        live_repository, ref, live_sha = _side(live, "head")
        if (live_repository, live_sha) != (repository, batched) or _side(live, "base")[:2] != (repository, base_branch):
            report["changed"].append(number)
            continue
        api.post_json(f"/repos/{repository}/issues/{number}/comments", {
            "body": f"Merged through batch #{pr_number} (batch commit `{member['squash_sha'][:12]}`)."})
        closed = api.patch_json(endpoint, {"state": "closed"})
        if type(closed) is not dict or _side(closed, "head")[2] != batched:
            # A push raced the close: those commits did not land, so the pull request stays open.
            api.patch_json(endpoint, {"state": "open"})
            report["changed"].append(number)
            continue
        report["closed"].append(number)
        if delete_branches and grammar.is_match(grammar.BRANCH, ref) and ref != base_branch \
                and not ref.startswith(BATCH_BRANCH_PREFIX):
            quoted = urllib.parse.quote(ref, safe="/")
            current = f"/repos/{repository}/git/ref/heads/{quoted}"
            try:
                value = api.get_json(current)
            except ApiNotFound as error:
                if (error.status, error.method, error.path) != (404, "GET", current):
                    raise
                continue
            target = value.get("object") if type(value) is dict else None
            if type(target) is dict and value.get("ref") == "refs/heads/" + ref \
                    and (target.get("type"), target.get("sha")) == ("commit", batched):
                api.delete(f"/repos/{repository}/git/refs/heads/{quoted}")
                report["deleted"].append(number)
    return report


def _settlement_intents(settled: _Settlement) -> dict[str, Any]:
    """The closing intents of a verified settlement: everything the closing job of the managed
    batch caller may do with the batch App, and nothing else.

    ``{"repository", "pr_number", "base_branch", "merged_sha", "members": [{"pr_number",
    "head_sha", "squash_sha"}]}``, members in batch order. That job closes a member only while it
    is still open, of this repository, against ``base_branch`` and at ``head_sha``, as
    :func:`settle_batch` would, and deletes no branch."""

    return {"repository": settled.repository, "pr_number": settled.pr_number, "base_branch": settled.base_branch,
            "merged_sha": settled.merged_sha,
            "members": [{"pr_number": member["pr_number"], "head_sha": member["head_sha"],
                         "squash_sha": member["squash_sha"]} for member in settled.manifest["members"]]}


def settle_batch(api: GitHubApi, store: BatchStore, *, pr_number: int, plan: dict[str, Any],
                 build_seal: dict[str, Any], packaged_seal: dict[str, Any], temporary_root: Path,
                 delete_branches: bool = False) -> dict[str, list[int]]:
    """Close the members of the merged batch pull request ``pr_number`` once its provenance holds.

    ``plan`` is the plan the batch pull request's gates ran with, and the two seals are the
    descriptors of its Build and packaged tested records; each seal names the run that sealed it.
    Before any write:

    * the pull request is a merged ``batch/*`` pull request of this repository whose body carries
      one marker, and the plan's identity names that pull request, its head, the marker's base
      (the gates tested the stack on the commit it was built on) and the marker's final tree;
    * the stack rebuilt from the marker equals the marker (``rebuild_batch``), so the head the
      gates tested is that stack;
    * ``transport.download_merged_gate_pair`` authenticates both original gates, whose runs
      GitHub records under that head, and that the merged commit has the tested tree and lies
      in the default branch.

    Then each member that is still open at its batched head (and still of this repository, to the
    same base) gets a comment and is closed; one that received commits stays open, because those
    never reached the default branch. If a head moved between the last read and the close, the
    pull request is reopened. With ``delete_branches`` a closed member's branch is deleted while
    it still points at the batched head. Returns ``{"closed", "changed", "already_closed",
    "deleted"}`` as lists of pull request numbers; running it again closes nothing twice.
    """

    check(type(delete_branches) is bool, "$.batch.delete_branches", "must be a boolean")
    check(api.writable, "$.batch.api", "settling a batch needs a writable GitHub client")
    settled = _verify_settlement(api, store, pr_number=pr_number, plan=plan, build_seal=build_seal,
                                 packaged_seal=packaged_seal, temporary_root=temporary_root)
    return _close_members(api, settled, delete_branches=delete_branches)


def settle_derived_batch(api: GitHubApi, store: BatchStore, *, pr_number: int, plan: dict[str, Any],
                         temporary_root: Path, intents: bool = False,
                         delete_branches: bool = False) -> dict[str, Any]:
    """Settle the merged batch pull request ``pr_number`` with gates derived from ``plan``, the
    protected plan of the default-branch commit the job runs at.

    :func:`derive_batch_gates` locates and authenticates the gates, the settlement is proven as
    :func:`settle_batch` proves it, and the gates are observed again immediately before the effect
    (design risk 11: a fresh live-evidence check before anything is closed). With ``intents`` the
    effect is the return of :func:`_settlement_intents` and nothing is written, so a read-only job
    decides and a job of the caller that holds the batch App closes; otherwise ``api`` must be
    writable and the members are closed as :func:`settle_batch` closes them, returning its report.
    """

    Int(1, limits.MAX_RUN_ID)(pr_number, "$.batch.pull")
    check(type(intents) is bool and type(delete_branches) is bool, "$.batch.options", "must be booleans")
    check(not (intents and delete_branches), "$.batch.options", "closing intents never delete a branch")
    check(intents or api.writable, "$.batch.api", "settling a batch needs a writable GitHub client")
    # One command's reads: a commit, a tree or the job list of a completed attempt is read once.
    reads = CommandReads.of(api)
    _, branch, head_sha, _ = _merged_batch(reads, pr_number)
    gates = derive_batch_gates(reads, pr_number=pr_number, head_sha=head_sha, head_branch=branch, plan=plan,
                               temporary_root=temporary_root)
    settled = _verify_settlement(reads, store, pr_number=pr_number, plan=gates.plan, build_seal=gates.build_seal,
                                 packaged_seal=gates.packaged_seal, temporary_root=temporary_root)
    gates.watch.recheck()
    if intents:
        return _settlement_intents(settled)
    return _close_members(api, settled, delete_branches=delete_branches)
