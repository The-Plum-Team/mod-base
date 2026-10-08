"""Read-only ordered batch source observations (MB11); no construction or writer authority."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from mod_base.build_ci.authenticate import PrGeneration, authenticate_pr_identity, read_pr_generation
from mod_base.build_ci.batch_schema import validate_batch_manifest
from mod_base.build_ci.protocol import validate_identity
from mod_base.build_ci.source import (GitSourceEntry, read_source_tree_inventory,
                                      validate_source_inventory, verify_source_copy)
from mod_base.github.api import ApiNotFound, GitHubApi
from mod_base.github.contents import commit_tree, compare
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.model.validators import Int, check


BATCH_BRANCH_PREFIX = grammar.CI_BATCH_BRANCH_PREFIX


@dataclass(frozen=True)
class BatchMember:
    """API-bound source/readiness and complete head-tree observation, not ordinary-path admission."""

    generation: PrGeneration
    head_tree: str


@dataclass(frozen=True)
class BatchBranchLease:
    """Retained absence/member observations; never an atomic lock or writer approval."""

    repository: str
    branch: str
    controller_sha: str
    members: tuple[BatchMember, ...]


def empty_batch_branch_lease(branch: str) -> str:
    """Fixed Git empty-expect lease argument; not a command, ref mutation or writer approval."""
    check(grammar.is_batch_branch(branch), '$.batch.branch', 'invalid batch Git branch')
    return '--force-with-lease=refs/heads/'+branch+':'


def validate_batch_push_receipt(data: bytes, *, exit_code: int, remote: str,
                                 branch: str, commit_sha: str) -> None:
    """Require one exact new-branch --porcelain receipt, never up-to-date/no-op success.

    Caller must independently bind the completed original protected Git command, sanitized C
    locale/environment, exact independently admitted remote/commit and empty-expect push, then
    reauthenticate the created ref and native/live writer prerequisites. Fabricated bytes/exit
    codes are not execution, destination or writer provenance. No Git/API/ref effect occurs.
    """
    empty_batch_branch_lease(branch)
    grammar.require_sha1(commit_sha, 'batch pushed commit SHA')
    check(type(exit_code) is int and exit_code == 0, '$.batch.push', 'batch push did not succeed')
    check(type(remote) is str and 1 <= len(remote) <= limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES
          and all(32 <= ord(character) <= 126 for character in remote),
          '$.batch.remote', 'expected batch destination is malformed or exceeds its cap')
    check(type(data) is bytes and 1 <= len(data) <= limits.MAX_CI_BATCH_PUSH_RECEIPT_BYTES,
          '$.batch.push', 'push receipt is empty or exceeds its cap')
    check(all(32 <= character <= 126 or character in (9, 10, 13) for character in data),
          '$.batch.push', 'push receipt contains invalid encoding/control bytes')
    text = data.decode('ascii')
    check(text.endswith('\n') and '\r' not in text.replace('\r\n', ''),
          '$.batch.push', 'push receipt has invalid line framing')
    expected = [f'To {remote}', f'*\t{commit_sha}:refs/heads/{branch}\t[new branch]', 'Done']
    check(text.splitlines() == expected, '$.batch.push', 'push did not create the exact new batch branch')


def observe_batch_branch_lease(api: GitHubApi, *, controller_sha: str, branch: str,
                               pr_numbers: tuple[int, ...]) -> BatchBranchLease:
    """Bracket exact branch absence with live ordered member/controller admission.

    Only an exact GET-ref ApiNotFound 404 is absence after successful source/controller access.
    Other API failures, existing or malformed responses fail closed. The caller independently
    admits native ordinary policy and safe Git/program/runtime, bytes, application/results and
    actual writer authority. Repeat around effects and use the explicit empty-expect Git lease
    on the final push: repeated reads do not reserve the name or prevent future races.
    """
    empty_batch_branch_lease(branch)
    members = authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=pr_numbers)
    endpoint = f'/repos/{api.repository}/git/ref/heads/{branch}'

    def absent() -> None:
        try:
            api.get_json(endpoint)
        except ApiNotFound as exc:
            if exc.status == 404 and exc.method == 'GET' and exc.path == endpoint:
                return
            raise
        check(False, '$.batch.branch', 'batch branch already exists or returned a malformed response')

    absent()
    check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=pr_numbers) == members,
          '$.batch.members', 'batch source/controller changed during empty branch observation')
    absent()
    check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=pr_numbers) == members,
          '$.batch.members', 'batch source/controller changed after final empty branch read')
    return BatchBranchLease(api.repository, branch, controller_sha, members)


def recheck_batch_branch_lease(api: GitHubApi, lease: BatchBranchLease, *, controller_sha: str,
                               branch: str, pr_numbers: tuple[int, ...]) -> None:
    """Re-observe against independent original caller arguments, never receipt authority."""
    check(type(lease) is BatchBranchLease and type(lease.members) is tuple
          and 1 <= len(lease.members) <= limits.MAX_CI_BATCH_MEMBERS
          and all(type(member) is BatchMember and type(member.generation) is PrGeneration for member in lease.members),
          '$.batch.lease', 'malformed batch branch observation')
    check((lease.repository, lease.branch, lease.controller_sha,
           tuple(member.generation.pr_number for member in lease.members)) ==
          (api.repository, branch, controller_sha, pr_numbers),
          '$.batch.lease', 'lease differs from original caller repository/branch/controller/order')
    check(observe_batch_branch_lease(api, controller_sha=controller_sha, branch=branch,
                                    pr_numbers=pr_numbers) == lease,
          '$.batch.lease', 'retained batch members or controller changed')


@dataclass(frozen=True)
class BatchPatchEntry:
    """One changed path's exact before/after Git identities; deletions/additions retain None."""

    path: str
    before: GitSourceEntry | None
    after: GitSourceEntry | None


@dataclass(frozen=True)
class BatchPatch:
    """Retained member/merge-base/patch API observations; not Git writer or native policy authority."""

    member: BatchMember
    merge_base_sha: str
    merge_base_tree: str
    changes: tuple[BatchPatchEntry, ...]


@dataclass(frozen=True)
class BatchPatchBytes:
    """Retained complete source-byte observations; not original-root, Git or writer authority."""

    patch: BatchPatch
    merge_base_bytes_sha256: str
    head_bytes_sha256: str


@dataclass(frozen=True)
class BatchManifestSources:
    """Manifest digest and retained source observations, never Git application/writer approval."""

    manifest_sha256: str
    members: tuple[BatchPatchBytes, ...]


def _manifest_snapshot(api: GitHubApi, document: dict[str, Any], *, controller_sha: str,
                       profile: str, policy_sha256: str, permitted_paths: tuple[str, ...],
                       source_roots: tuple[tuple[Path, Path], ...]) -> tuple[dict[str, Any], bytes, tuple[int, ...]]:
    """Shared bounded data/native-argument preflight before any API read."""
    validate_batch_manifest(document)
    grammar.require_sha1(controller_sha, 'batch executing controller SHA')
    grammar.require(grammar.SHA256, policy_sha256, 'batch admitted native policy SHA-256')
    _permitted_paths(permitted_paths)
    check(profile in ('quick-skin', 'block-pops') and document['profile'] == profile
          and document['policy_sha256'] == policy_sha256,
          '$.batch.policy', 'manifest differs from admitted native profile/policy')
    check(document['repository'] == api.repository and document['base_sha'] == controller_sha,
          '$.batch.base', 'manifest differs from executing repository/controller')
    check(type(source_roots) is tuple and len(source_roots) == len(document['members'])
          and all(type(pair) is tuple and len(pair) == 2 and all(isinstance(root, Path) for root in pair)
                  for pair in source_roots), '$.batch.roots', 'source roots lack ordered merge-base/head pairs')
    raw = canonical_json(document)
    manifest = strict_loads(raw, label='batch manifest', max_bytes=limits.MAX_CI_BATCH_DOCUMENT_BYTES)
    numbers = tuple(member['pr_number'] for member in manifest['members'])
    return manifest, raw, numbers


@dataclass(frozen=True)
class BatchPublication:
    """Current API ref/source observations, not creator, writer, PR or settlement approval."""

    sources: BatchManifestSources
    branch: str
    commit_sha: str
    result_tree: str


def authenticate_batch_publication(api: GitHubApi, document: dict[str, Any], *,
                                   controller_sha: str, branch: str, commit_sha: str,
                                   profile: str, policy_sha256: str, permitted_paths: tuple[str, ...],
                                   source_roots: tuple[tuple[Path, Path], ...]) -> BatchPublication:
    """Bind the exact published ref to independent expected branch/commit and complete sources.

    Original safe Git/runtime, native approval, local application/results, empty-name lease and
    actual completed new-branch push provenance remain independently mandatory. A current ref
    alone proves neither creator nor permission. Retain original private writer-excluded roots.
    This reads no batch PR/gates, creates no PR/ref and does not approve settlement or statuses.
    Re-admit live/native/writer conditions around later effects; reads do not prevent future races.
    """
    empty_batch_branch_lease(branch)
    grammar.require_sha1(commit_sha, 'expected published batch SHA')
    manifest, raw, numbers = _manifest_snapshot(api, document, controller_sha=controller_sha,
                                               profile=profile, policy_sha256=policy_sha256,
                                               permitted_paths=permitted_paths, source_roots=source_roots)
    check(manifest['branch'] == branch and manifest['members'][-1]['squash_sha'] == commit_sha,
          '$.batch.publication', 'manifest differs from original published branch/commit')
    endpoint = f'/repos/{api.repository}/git/ref/heads/{branch}'

    def reference() -> None:
        value = api.get_json(endpoint)
        check(type(value) is dict and value.get('ref') == 'refs/heads/'+branch
              and type(value.get('object')) is dict and value['object'].get('type') == 'commit'
              and value['object'].get('sha') == commit_sha,
              '$.batch.ref', 'published batch ref differs from exact branch/commit')
        check(commit_tree(api, commit_sha) == manifest['result_tree'],
              '$.batch.result', 'published batch commit differs from final manifest tree')

    reference()
    sources = verify_batch_manifest_sources(api, manifest, controller_sha=controller_sha, profile=profile,
                                            policy_sha256=policy_sha256, permitted_paths=permitted_paths,
                                            source_roots=source_roots)
    reference()
    check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=numbers) ==
          tuple(observation.patch.member for observation in sources.members),
          '$.batch.members', 'batch membership changed after final published ref read')
    validate_batch_manifest(document)
    check(canonical_json(document) == raw, '$.batch.manifest', 'caller manifest changed during publication inspection')
    return BatchPublication(sources, branch, commit_sha, manifest['result_tree'])


@dataclass(frozen=True)
class BatchPr:
    """Ready batch PR, manifest/source/ref and merge identity observations; never gate authority."""

    publication: BatchPublication
    generation: PrGeneration
    identity_sha256: str


def authenticate_batch_pr(api: GitHubApi, document: dict[str, Any], identity: dict[str, Any], *,
                          controller_sha: str, profile: str, policy_sha256: str,
                          permitted_paths: tuple[str, ...],
                          source_roots: tuple[tuple[Path, Path], ...]) -> BatchPr:
    """Bind a ready batch PR's exact synthetic merge to manifest, ref and complete member sources.

    Identity comes from the independent original protected caller/native plan. Its kit/workflow/
    inventory/scenario/graph fields need their own genuine admission; shape is not that approval.
    Original safe Git/runtime, private writer-excluded roots, local construction and completed
    empty-name/new-branch push provenance remain required. No PR creation/merge/closure, candidate
    execution, complete gate evidence, status or settlement permission is supplied by this read.
    """
    validate_identity(identity)
    manifest, raw, numbers = _manifest_snapshot(api, document, controller_sha=controller_sha,
                                               profile=profile, policy_sha256=policy_sha256,
                                               permitted_paths=permitted_paths, source_roots=source_roots)
    branch, commit_sha = manifest['branch'], manifest['members'][-1]['squash_sha']
    empty_batch_branch_lease(branch)
    check(identity['pr_number'] > 0 and identity['pr_number'] not in numbers,
          '$.batch.pr', 'batch PR cannot be a source member or non-PR subject')
    check((identity['repository'], identity['source_repository'], identity['controller_sha'],
           identity['base_sha'], identity['base_branch'], identity['head_sha'], identity['head_branch'],
           identity['tested_tree'], identity['policy_sha256']) ==
          (api.repository, api.repository, controller_sha, controller_sha, manifest['base_branch'],
           commit_sha, branch, manifest['result_tree'], policy_sha256),
          '$.batch.identity', 'batch identity differs from original manifest/controller/policy')
    identity_raw = canonical_json(identity)
    retained_identity = strict_loads(identity_raw, label='batch PR identity', max_bytes=limits.MAX_CI_PLAN_BYTES)
    generation = read_pr_generation(api, pr_number=retained_identity['pr_number'], controller_sha=controller_sha)
    authenticate_pr_identity(api, retained_identity)
    publication = authenticate_batch_publication(api, manifest, controller_sha=controller_sha,
                                                 branch=branch, commit_sha=commit_sha, profile=profile,
                                                 policy_sha256=policy_sha256, permitted_paths=permitted_paths,
                                                 source_roots=source_roots)
    authenticate_pr_identity(api, retained_identity)
    check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=numbers) ==
          tuple(observation.patch.member for observation in publication.sources.members),
          '$.batch.members', 'source members changed during batch PR admission')
    check(read_pr_generation(api, pr_number=retained_identity['pr_number'], controller_sha=controller_sha) == generation,
          '$.batch.pr', 'batch PR readiness/source/merge changed during admission')
    validate_batch_manifest(document)
    validate_identity(identity)
    check(canonical_json(document) == raw and canonical_json(identity) == identity_raw,
          '$.batch.inputs', 'caller manifest/identity changed during batch PR admission')
    return BatchPr(publication, generation, hashlib.sha256(identity_raw).hexdigest())


def verify_batch_manifest_sources(api: GitHubApi, document: dict[str, Any], *,
                                  controller_sha: str, profile: str, policy_sha256: str,
                                  permitted_paths: tuple[str, ...],
                                  source_roots: tuple[tuple[Path, Path], ...]) -> BatchManifestSources:
    """Bind a closed manifest to repeated live API, complete bytes and squash graph observations.

    Original protected caller/native policy and private writer-excluded root lifetimes must be
    independently admitted. Roots are in manifest order, merge-base then head. API graph equality
    does not prove safe local Git patch application or fixed bot construction. Those proofs,
    branch leases, merged full-gate provenance and settlement remain prerequisites for effects.
    No candidate/Git execution, API/ref/account mutation or atomic future lease is provided.
    """
    manifest, raw, numbers = _manifest_snapshot(api, document, controller_sha=controller_sha,
                                               profile=profile, policy_sha256=policy_sha256,
                                               permitted_paths=permitted_paths, source_roots=source_roots)

    def observe() -> tuple[tuple[BatchPatchBytes, ...], str]:
        collection = authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=numbers)
        retained = []
        for declared, live, roots in zip(manifest['members'], collection, source_roots):
            generation = live.generation
            check((generation.base_branch, generation.controller_tree) ==
                  (manifest['base_branch'], manifest['base_tree']), '$.batch.base', 'manifest base tree/ref disagrees')
            check((generation.repository, generation.head_branch, generation.head_sha, live.head_tree,
                   generation.draft) == (declared['source_repository'], declared['head_branch'],
                                         declared['head_sha'], declared['head_tree'], declared['draft']),
                  '$.batch.member', 'manifest source/readiness differs from live member')
            observed = verify_batch_patch_bytes(api, roots[0], roots[1], controller_sha=controller_sha,
                                                pr_number=declared['pr_number'], permitted_paths=permitted_paths)
            check(observed.patch.member == live, '$.batch.member', 'member changed during manifest source inspection')
            check((observed.patch.merge_base_sha, observed.patch.merge_base_tree,
                   observed.merge_base_bytes_sha256, observed.head_bytes_sha256) ==
                  (declared['merge_base_sha'], declared['merge_base_tree'],
                   declared['merge_base_bytes_sha256'], declared['head_bytes_sha256']),
                  '$.batch.source', 'manifest merge-base/source bytes disagree')
            patch = []
            for entry in observed.patch.changes:
                sides = {side: None if value is None else
                         {'mode': value.mode, 'size': value.size, 'git_blob': value.git_blob}
                         for side, value in (('before', entry.before), ('after', entry.after))}
                patch.append({'path': entry.path, **sides})
            check(patch == declared['patch'], '$.batch.patch', 'manifest patch differs from complete source trees')
            retained.append(observed)

        # The API-selected result graph is independently inspected, not inferred from hashes.
        graph = hashlib.sha256(canonical_json({'format': 'mod-base.batch-result-inventory-v1'}))
        for index, declared in enumerate(manifest['members']):
            value = api.get_json(f"/repos/{api.repository}/git/commits/{declared['squash_sha']}")
            check(type(value) is dict and value.get('sha') == declared['squash_sha']
                  and type(value.get('tree')) is dict and value['tree'].get('sha') == declared['result_tree'],
                  '$.batch.result', 'squash commit/result tree disagrees')
            parents = value.get('parents')
            check(type(parents) is list and len(parents) == 1 and type(parents[0]) is dict
                  and parents[0].get('sha') == declared['parent_sha'],
                  '$.batch.parents', 'squash requires its exact single ordered parent')
            graph.update(canonical_json({'member_index': index, 'tree_sha': declared['result_tree']}))
            for entry in read_source_tree_inventory(api, tree_sha=declared['result_tree']):
                graph.update(canonical_json({'path': entry.path, 'mode': entry.mode,
                                             'size': entry.size, 'git_blob': entry.git_blob}))
        check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=numbers) == collection,
              '$.batch.members', 'complete manifest membership changed during graph inspection')
        return tuple(retained), graph.hexdigest()

    first = observe()
    check(observe() == first, '$.batch.manifest', 'manifest source bytes or result inventories changed')
    validate_batch_manifest(document)
    check(canonical_json(document) == raw, '$.batch.manifest', 'caller manifest changed during inspection')
    return BatchManifestSources(hashlib.sha256(raw).hexdigest(), first[0])


def verify_batch_patch_bytes(api: GitHubApi, before_root: Path, after_root: Path, *,
                             controller_sha: str, pr_number: int,
                             permitted_paths: tuple[str, ...]) -> BatchPatchBytes:
    """Inspect complete quiescent source copies against authenticated merge-base/head trees.

    Caller must independently retain the original private roots, exclude writers and admit
    protected native policy. Git metadata is outside tracked-source byte inspection. Re-admit
    root lifetimes, safe local Git graph/application, resulting trees and leases around later
    effects. No candidate import, Git execution, ref mutation or account operation occurs here.
    """

    admitted = authenticate_batch_patch(api, controller_sha=controller_sha, pr_number=pr_number,
                                        permitted_paths=permitted_paths)
    before = read_source_tree_inventory(api, tree_sha=admitted.merge_base_tree)
    after = read_source_tree_inventory(api, tree_sha=admitted.member.head_tree)
    check(derive_batch_patch_inventory(before=before, after=after, permitted_paths=permitted_paths)
          == admitted.changes, '$.batch.patch', 'batch patch inventory changed before byte inspection')

    def inspect() -> tuple[str, str]:
        values = []
        for root, inventory, tree_sha in ((before_root, before, admitted.merge_base_tree),
                                         (after_root, after, admitted.member.head_tree)):
            records = verify_source_copy(root, inventory=inventory)
            digest = hashlib.sha256(canonical_json({'format': 'mod-base.batch-source-bytes-v1',
                                                    'tree_sha': tree_sha}))
            for record in records:
                grammar.require(grammar.SHA256, record.get('sha256'), 'batch source byte SHA-256')
                digest.update(canonical_json({key: record[key]
                                              for key in ('path', 'mode', 'size', 'git_blob', 'sha256')}))
            values.append(digest.hexdigest())
        return values[0], values[1]

    observed = inspect()
    check(authenticate_batch_patch(api, controller_sha=controller_sha, pr_number=pr_number,
                                    permitted_paths=permitted_paths) == admitted,
          '$.batch.patch', 'batch source/patch changed during byte inspection')
    check(read_source_tree_inventory(api, tree_sha=admitted.merge_base_tree) == before
          and read_source_tree_inventory(api, tree_sha=admitted.member.head_tree) == after,
          '$.batch.trees', 'complete batch inventory changed during byte inspection')
    check(inspect() == observed, '$.batch.bytes', 'complete batch source bytes changed')
    check(authenticate_batch_patch(api, controller_sha=controller_sha, pr_number=pr_number,
                                    permitted_paths=permitted_paths) == admitted,
          '$.batch.patch', 'batch source/patch changed after final byte inspection')
    return BatchPatchBytes(admitted, observed[0], observed[1])


def authenticate_batch_patch(api: GitHubApi, *, controller_sha: str, pr_number: int,
                             permitted_paths: tuple[str, ...]) -> BatchPatch:
    """Bind complete-tree changes to a live member's API-selected, reachable merge-base.

    Ignore compare's bounded files/patch listing. Re-read complete exact trees, immutable object
    bindings and the live member around derivation. Independently admitted original caller/native
    policy, patch bytes, matching safe local Git graph/application, result trees, leases and
    settlement remain required. No Git program, candidate import or API mutation is performed.
    """

    _permitted_paths(permitted_paths)
    member = authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=(pr_number,))[0]

    def merge_base() -> tuple[str, str, str, int, int]:
        value = api.get_json(f'/repos/{api.repository}/compare/{controller_sha}...{member.generation.head_sha}',
                             params={'per_page': 1})
        check(type(value) is dict and value.get('status') in ('ahead', 'behind', 'identical', 'diverged'),
              '$.batch.compare', 'malformed batch comparison')
        for name in ('ahead_by', 'behind_by'):
            Int(0, limits.MAX_RUN_ID)(value.get(name), '$.batch.compare.'+name)
        base = value.get('base_commit')
        common = value.get('merge_base_commit')
        check(type(base) is dict and base.get('sha') == controller_sha and type(common) is dict,
              '$.batch.compare', 'comparison lacks the exact protected base or merge-base')
        sha = grammar.require_sha1(common.get('sha'), 'batch merge-base SHA')
        commit = common.get('commit')
        check(type(commit) is dict and type(commit.get('tree')) is dict,
              '$.batch.merge_base_tree', 'merge-base lacks its complete tree')
        tree_sha = grammar.require_sha1(commit['tree'].get('sha'), 'batch merge-base tree')
        check(commit_tree(api, sha) == tree_sha, '$.batch.merge_base_tree', 'merge-base Git tree disagrees')
        for endpoint in (controller_sha, member.generation.head_sha):
            ancestry = compare(api, sha, endpoint)
            check(ancestry['status'] in ('ahead', 'identical') and ancestry['behind_by'] == 0,
                  '$.batch.merge_base', 'merge-base is outside a member or protected base history')
        return sha, tree_sha, value['status'], value['ahead_by'], value['behind_by']

    original = merge_base()
    check(commit_tree(api, member.generation.head_sha) == member.head_tree,
          '$.batch.head_tree', 'member head tree changed')
    before = read_source_tree_inventory(api, tree_sha=original[1])
    after = read_source_tree_inventory(api, tree_sha=member.head_tree)
    changes = derive_batch_patch_inventory(before=before, after=after, permitted_paths=permitted_paths)
    check(read_source_tree_inventory(api, tree_sha=original[1]) == before
          and read_source_tree_inventory(api, tree_sha=member.head_tree) == after,
          '$.batch.trees', 'complete batch tree inventory changed')
    check(merge_base() == original and commit_tree(api, member.generation.head_sha) == member.head_tree,
          '$.batch.merge_base', 'batch object/merge-base observation changed')
    check(authenticate_batch_members(api, controller_sha=controller_sha, pr_numbers=(pr_number,))[0] == member,
          '$.batch.member', 'live batch member changed during patch admission')
    return BatchPatch(member, original[0], original[1], changes)


def derive_batch_patch_inventory(*, before: tuple[GitSourceEntry, ...],
                                 after: tuple[GitSourceEntry, ...],
                                 permitted_paths: tuple[str, ...]) -> tuple[BatchPatchEntry, ...]:
    """Derive nonempty complete-tree changes under independently admitted native path policy.

    Retain genuine protected merge-base/head tree and inventory provenance independently. Never
    substitute controller-vs-head differences for a member's merge-base patch. A permitted-path
    tuple supplied by candidate data is not approval. Native mode/link/restricted-transition
    admission, patch bytes, safe merge application/result trees and writer leases remain required.
    No byte is read, program is executed, Git object is constructed or ref is mutated here.
    """

    validate_source_inventory(before)
    validate_source_inventory(after)
    permitted = _permitted_paths(permitted_paths)
    blob_sizes: dict[str, int] = {}
    for inventory in (before, after):
        for entry in inventory:
            check(blob_sizes.get(entry.git_blob, entry.size) == entry.size,
                  "$.batch.patch", "one Git blob has inconsistent declared sizes")
            blob_sizes[entry.git_blob] = entry.size
    old = {entry.path: entry for entry in before}
    new = {entry.path: entry for entry in after}
    paths = sorted(set(old) | set(new))
    changes = []
    for path in paths:
        if old.get(path) == new.get(path):
            continue
        check(path in permitted, "$.batch.patch", "changed path is outside native ordinary policy")
        changes.append(BatchPatchEntry(path, old.get(path), new.get(path)))
    check(bool(changes), "$.batch.patch", "batch member patch is a no-op")
    return tuple(changes)


def _permitted_paths(permitted_paths: tuple[str, ...]) -> set[str]:
    check(type(permitted_paths) is tuple
          and 1 <= len(permitted_paths) <= limits.MAX_CI_BATCH_PATCH_FILES,
          "$.batch.permitted_paths", "native path policy is malformed or exceeds its cap")
    previous = ''
    spellings: dict[str, str] = {}
    path_bytes = 0
    for path in permitted_paths:
        check(grammar.is_repo_path(path) and path > previous
              and path.split('/')[0].casefold() != '.git',
              "$.batch.permitted_paths", "native paths are invalid, duplicate or unordered")
        path_bytes += len(path.encode('utf-8'))
        check(path_bytes <= limits.MAX_CI_BATCH_PATCH_PATH_BYTES,
              "$.batch.permitted_paths", "native path policy exceeds its byte cap")
        parts = path.split('/')
        for position in range(1, len(parts) + 1):
            prefix = '/'.join(parts[:position])
            check(spellings.get(prefix.casefold(), prefix) == prefix,
                  "$.batch.permitted_paths", "native path policy contains a case alias")
            check(prefix.casefold() in spellings or len(spellings) < limits.MAX_CI_BATCH_PATCH_ENTRIES,
                  "$.batch.permitted_paths", "native path policy exceeds its entry cap")
            spellings[prefix.casefold()] = prefix
        previous = path
    return set(permitted_paths)


def authenticate_batch_members(api: GitHubApi, *, controller_sha: str,
                               pr_numbers: tuple[int, ...]) -> tuple[BatchMember, ...]:
    """Observe up to 50 distinct open same-repository members in the requested order.

    The caller must independently admit the original protected controller and ordinary native
    path policy. These observations do not authenticate patches/resulting trees, construct a
    manifest, approve restricted changes, execute Git, write refs or settle PRs. Re-admit the
    complete collection around later effects; even repeated reads cannot make future events
    atomic. Budget retries as well as the bounded per-member API reads in the protected caller.
    """

    grammar.require_sha1(controller_sha, "batch protected controller SHA")
    grammar.require(grammar.REPOSITORY, api.repository, "batch repository")
    check(type(pr_numbers) is tuple and 1 <= len(pr_numbers) <= limits.MAX_CI_BATCH_MEMBERS,
          "$.batch.members", "requires a bounded nonempty ordered member tuple")
    for number in pr_numbers:
        Int(1, limits.MAX_RUN_ID)(number, "$.batch.member.number")
    check(len(set(pr_numbers)) == len(pr_numbers), "$.batch.members", "duplicate batch member")

    def observe(number: int) -> BatchMember:
        generation = read_pr_generation(api, pr_number=number, controller_sha=controller_sha)
        check(generation.head_branch != generation.base_branch
              and not generation.head_branch.startswith(BATCH_BRANCH_PREFIX),
              "$.batch.member.head_branch", "batch member is itself a batch or base branch")
        head_tree = commit_tree(api, generation.head_sha)
        check(read_pr_generation(api, pr_number=number, controller_sha=controller_sha) == generation,
              "$.batch.member", "batch member changed during head-tree read")
        return BatchMember(generation, head_tree)

    retained = tuple(observe(number) for number in pr_numbers)
    check(len({member.generation.controller_tree for member in retained}) == 1,
          "$.batch.base_tree", "members do not share one protected base tree")
    for number, member in zip(pr_numbers, retained):
        check(observe(number) == member, "$.batch.members", "batch source changed during collection")
    return retained
