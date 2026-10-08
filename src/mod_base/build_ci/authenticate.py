"""Read-only live and historical PR/Git observations (MB11), never gate authority.

Every admission function here is one observation of mutable GitHub state (the default branch and
its head, a pull request) plus the immutable Git objects it names. A caller that acts on an
observation makes it again immediately before its effect and requires the same result
(``mod_base.build_ci.reads.Watch``); nothing here repeats a read on its own. Passing a
``mod_base.build_ci.reads.CommandReads`` as ``api`` reads each Git object once per command.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from mod_base.build_ci.protocol import validate_identity
from mod_base.github.api import GitHubApi
from mod_base.github.contents import branch_head, compare, default_branch
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import Int, check


@dataclass(frozen=True)
class PrGeneration:
    """Retained API source/readiness observation, never execution or success authority."""

    repository: str
    pr_number: int
    base_branch: str
    base_sha: str
    controller_tree: str
    head_branch: str
    head_sha: str
    draft: bool
    merge_sha: str | None


@dataclass(frozen=True)
class MergedPr:
    """Historical Git/API observations; not full-gate, policy, reuse or settlement approval."""

    repository: str
    pr_number: int
    identity_sha256: str
    merged_sha: str
    merged_tree: str
    merged_parents: tuple[str, ...]
    merged_at: str
    controller_sha: str


def run_head(identity: dict[str, Any]) -> tuple[str, str, str]:
    """``(head_sha, head_branch, head_repository)`` GitHub records on every run and artifact that
    a managed caller produces for ``identity``.

    A ``pull_request_target`` run is recorded under the pull request's head commit and branch in
    the source repository, never under the base it executes from. A protected push or dispatch on
    the default branch is recorded under the commit it ran from, which is both its tested commit
    and its controller; any other non-PR subject has no producer run."""

    validate_identity(identity)
    if identity["pr_number"]:
        return identity["head_sha"], identity["head_branch"], identity["source_repository"]
    check(identity["tested_sha"] == identity["controller_sha"], "$.tested_sha",
          "a protected non-PR producer tests the commit it runs from")
    return identity["tested_sha"], identity["base_branch"], identity["repository"]


def _commit(api: GitHubApi, sha: str, path: str) -> tuple[str, tuple[str, ...]]:
    """``(tree, ordered parents)`` of the Git commit ``sha``, which never changes."""

    value = api.get_json(f"/repos/{api.repository}/git/commits/{sha}")
    check(type(value) is dict and value.get("sha") == sha, path, "wrong Git object")
    tree, parents = value.get("tree"), value.get("parents")
    check(type(tree) is dict and grammar.is_match(grammar.SHA1, tree.get("sha")), path, "malformed Git tree")
    check(type(parents) is list and len(parents) <= 2
          and all(type(parent) is dict and grammar.is_match(grammar.SHA1, parent.get("sha")) for parent in parents),
          path, "malformed or unsupported Git parents")
    ids = tuple(parent["sha"] for parent in parents)
    check(len(set(ids)) == len(ids) and sha not in ids, path, "duplicate or self Git parents")
    return tree["sha"], ids


def authenticate_merged_pr_identity(api: GitHubApi, identity: dict[str, Any], *,
                                    controller_sha: str, merged_sha: str) -> MergedPr:
    """Bind the original synthetic test merge to a merged PR and current protected history.

    The original protected identity and independent current controller/final merge SHA must be
    admitted by the caller. Final merge/squash/rebase parents may differ, but the original tested
    merge still requires its exact ordered base/head parents and equal complete final tree.
    Both full gates, native semantics, original/current protected policy and pin, source seals,
    latest attempts and original writer/owner authority remain separate mandatory admission.
    This route never weakens live PR admission, approves reuse/settlement or mutates anything.
    The identity is read once on entry; the result describes that admitted value.
    """
    validate_identity(identity)
    grammar.require_sha1(controller_sha, 'current protected controller SHA')
    grammar.require_sha1(merged_sha, 'expected final merged SHA')
    check(identity['repository'] == identity['source_repository'] == api.repository
          and identity['pr_number'] > 0 and identity['controller_sha'] == identity['base_sha']
          and merged_sha != identity['base_sha'],
          '$.merged.identity', 'requires the original same-repository PR controller identity')
    raw = canonical_json(identity)
    retained = strict_loads(raw, label='original merged PR identity', max_bytes=limits.MAX_CI_PLAN_BYTES)
    branch = default_branch(api)
    check(branch == retained['base_branch'], '$.merged.base', 'protected default branch changed')
    current = branch_head(api, branch)
    check(current[0] == controller_sha, '$.merged.controller', 'current protected controller moved')
    check(controller_sha != merged_sha or current[1] == retained['tested_tree'],
          '$.merged.controller', 'live final commit tree differs from the original tested tree')

    pr = api.get_json(f"/repos/{api.repository}/pulls/{retained['pr_number']}")
    check(type(pr) is dict and type(pr.get('number')) is int and pr['number'] == retained['pr_number']
          and pr.get('state') == 'closed' and pr.get('merged') is True and pr.get('draft') is False,
          '$.merged.pr', 'requires the exact closed merged ready PR')
    for side in ('head', 'base'):
        value = pr.get(side)
        check(type(value) is dict and type(value.get('repo')) is dict
              and value['repo'].get('full_name') == api.repository,
              f'$.merged.{side}', 'missing or foreign merged PR repository')
        # The API base SHA may advance after merge; it is not the original tested parent.
        grammar.require_sha1(value.get('sha'), f'merged PR {side} SHA')
        check(value.get('ref') == retained[f'{side}_branch'],
              f'$.merged.{side}', 'merged PR branch differs from original identity')
    check(pr['head']['sha'] == retained['head_sha'] and pr.get('merge_commit_sha') == merged_sha,
          '$.merged.pr', 'merged PR source/final commit differs from original binding')
    grammar.parse_timestamp(pr.get('merged_at'), 'merged PR timestamp')

    tested_tree, tested_parents = _commit(api, retained['tested_sha'], '$.merged.tested')
    check(tested_tree == retained['tested_tree'], '$.merged.tree', 'original tested tree differs from its identity')
    check(tested_parents == tuple(retained['tested_parents']),
          '$.merged.tested_parents', 'original synthetic merge lacks exact ordered base/head parents')
    merged_tree, merged_parents = _commit(api, merged_sha, '$.merged.final')
    check(merged_tree == retained['tested_tree'], '$.merged.tree', 'final merged tree differs from the tested tree')
    check(len(merged_parents) >= 1, '$.merged.parents', 'final merged commit has no parent')
    ancestry = compare(api, retained['base_sha'], merged_sha)
    check(ancestry['status'] == 'ahead' and ancestry['ahead_by'] > 0 and ancestry['behind_by'] == 0,
          '$.merged.base', 'final commit is outside original protected base history')
    ancestry = compare(api, merged_sha, controller_sha)
    check(ancestry['behind_by'] == 0
          and (ancestry['status'] == 'identical' and ancestry['ahead_by'] == 0 if merged_sha == controller_sha
               else ancestry['status'] == 'ahead' and ancestry['ahead_by'] > 0),
          '$.merged.history', 'final commit is outside current protected default history')
    return MergedPr(api.repository, retained['pr_number'], hashlib.sha256(raw).hexdigest(), merged_sha,
                    merged_tree, merged_parents, pr['merged_at'], controller_sha)


def _pr_generation(api: GitHubApi, number: int, branch: str, controller: str,
                   tree: str) -> PrGeneration:
    pr = api.get_json(f"/repos/{api.repository}/pulls/{number}")
    check(type(pr) is dict and type(pr.get("number")) is int and pr["number"] == number,
          "$.pr.number", "wrong or malformed PR")
    check(pr.get("state") == "open" and type(pr.get("draft")) is bool,
          "$.pr", "requires an open PR with exact readiness state")
    for side in ("head", "base"):
        value = pr.get(side)
        check(type(value) is dict and type(value.get("repo")) is dict,
              f"$.pr.{side}", "missing source repository")
        check(value["repo"].get("full_name") == api.repository,
              f"$.pr.{side}.repo", "foreign/fork candidate is unsupported")
        grammar.require_sha1(value.get("sha"), f"PR {side} SHA")
        grammar.require(grammar.BRANCH, value.get("ref"), f"PR {side} branch")
    check(pr["base"]["ref"] == branch and pr["base"]["sha"] == controller,
          "$.pr.base", "PR base differs from the protected executing controller")
    check("merge_commit_sha" in pr, "$.pr.merge_commit_sha", "missing merge state")
    merge = pr["merge_commit_sha"]
    if merge is not None:
        grammar.require_sha1(merge, "PR merge SHA")
    return PrGeneration(api.repository, number, branch, controller, tree,
                        pr["head"]["ref"], pr["head"]["sha"], pr["draft"], merge)


def read_pr_generation(api: GitHubApi, *, pr_number: int, controller_sha: str) -> PrGeneration:
    """One observation of an open same-repository PR and the protected default head.

    Bind the live default/base to the caller's already protected executing controller SHA.
    Drafts and ready PRs with unavailable merge objects remain observations only. No Git merge
    object, candidate code, artifacts or mutation is consumed here. Ready execution still needs
    authenticate_pr_identity and independent policy/pin/plan/approval admission. Future events
    are not atomic with this read: a caller that acts on it reads again immediately before its
    effect and requires an equal generation.
    """

    Int(1, limits.MAX_RUN_ID)(pr_number, "$.pr_number")
    grammar.require_sha1(controller_sha, "executing controller SHA")
    grammar.require(grammar.REPOSITORY, api.repository, "repository")
    branch = default_branch(api)
    controller, tree = branch_head(api, branch)
    check(controller == controller_sha, "$.controller_sha", "protected executing controller has moved")
    return _pr_generation(api, pr_number, branch, controller, tree)


def authenticate_source_identity(api: GitHubApi, identity: dict[str, Any]) -> None:
    """Authenticate a live PR or an exact subject in the protected default history, once.

    Historical subjects remain distinct from the live controller. Request/run authentication,
    approved profile selection and explicit full-run recovery policy are separate obligations;
    reachability alone never authorizes execution, reuse or a success publication. The default
    branch, its head and the pull request are mutable: callers repeat this before their effect.
    """

    validate_identity(identity)
    if identity["pr_number"]:
        authenticate_pr_identity(api, identity)
        return
    check(identity["repository"] == api.repository and identity["source_repository"] == api.repository,
          "$.repository", "foreign protected subject is unsupported")
    branch = default_branch(api)
    check(identity["base_branch"] == identity["head_branch"] == branch,
          "$.base_branch", "subject must name the protected default history")
    controller, controller_tree = branch_head(api, branch)
    check(controller == identity["controller_sha"] == identity["base_sha"],
          "$.controller_sha", "protected controller has moved")
    tree, parents = _commit(api, identity["tested_sha"], "$.commit")
    check(tree == identity["tested_tree"], "$.tested_tree", "wrong complete protected tree")
    check(list(parents) == identity["tested_parents"], "$.tested_parents", "wrong ordered protected parents")
    if identity["tested_sha"] == controller:
        check(identity["tested_tree"] == controller_tree, "$.tested_tree", "live branch and Git tree disagree")
    else:
        ancestry = compare(api, identity["tested_sha"], controller)
        check(ancestry["status"] == "ahead" and ancestry["ahead_by"] > 0 and ancestry["behind_by"] == 0,
              "$.tested_sha", "subject is outside protected default history")


def authenticate_pr_identity(api: GitHubApi, identity: dict[str, Any]) -> None:
    """Check a ready same-repository PR, the protected head, exact merge parents and tree, once.

    Owner approval, policy/pin/plan re-derivation and artifact freshness are additional admission
    obligations. This function cannot authorize a controller upgrade or mint status authority.
    """

    validate_identity(identity)
    check(identity["repository"] == api.repository and identity["source_repository"] == api.repository,
          "$.repository", "foreign/fork candidate is unsupported")
    check(identity["pr_number"] > 0, "$.pr_number", "requires a PR subject")
    branch = default_branch(api)
    check(branch == identity["base_branch"], "$.base_branch", "must target the protected default branch")
    controller, controller_tree = branch_head(api, branch)
    check(controller == identity["controller_sha"] == identity["base_sha"],
          "$.controller_sha", "protected default/base has moved")
    observed = _pr_generation(api, identity["pr_number"], branch, controller, controller_tree)
    check(observed.draft is False, "$.pr", "PR must be open and ready")
    check(observed.head_sha == identity["head_sha"] and observed.head_branch == identity["head_branch"],
          "$.pr.head", "source identity moved")
    check(observed.merge_sha == identity["tested_sha"], "$.tested_sha", "test merge moved or unavailable")
    tree, parents = _commit(api, identity["tested_sha"], "$.commit")
    check(tree == identity["tested_tree"], "$.tested_tree", "wrong complete tree")
    check(len(parents) == 2, "$.tested_parents", "Git object has no exact merge parents")
    check(list(parents) == identity["tested_parents"], "$.tested_parents", "wrong ordered parents")
