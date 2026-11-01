# 0008. Publish releases from a tested generation

Date: 2026-11-01

## Status

Proposed. This record describes the implementation proposed in the pull request that carries it.
No mod is activated, no kit release is cut and no GitHub setting, environment or secret changes by
merging it. [RELEASE-PROTOCOL.md](../RELEASE-PROTOCOL.md) is the reference for what it decides.

## Context

Every Plum mod publishes releases on its own:

- Quick Skin tags one `mc<minecraft>-v<version>` per target and pushes the tags one at a time. Each
  tag run compiles its target twice, proves the bytes identical, attests them, runs the release E2E,
  and publishes through `Kira-NT/mc-publish` behind one repository-wide lock, with a ledger in the
  draft release body and a scheduled verifier that finalizes after moderation. Its 4.0.0 release of
  31 October 2026 (17 targets) took about five hours: a 68-minute validation-only rehearsal on four
  build slots, tag runs that rebuilt and retested the same bytes (about 1 h 45), and 102
  environment approvals one job at a time (105 minutes). Its five-minute verifier cron fired every
  three to six hours, and CurseForge's new file renaming left every CurseForge row pending.
- Multiplayer Spawners tags `v<version>` and publishes 69 files in about six minutes, but its
  release run rebuilds the jars, so what it publishes was never tested, and nothing checks its
  gates, records its uploads or verifies them afterwards.
- Block Pops publishes the jars of an authenticated `mb-ci-build` bundle with its own stdlib client
  and per-step tokens, but checks only the Build gate, has no tag and no GitHub Release, and has
  never run its workflow.

All three already pin the same kit and run its protected Build and packaged E2E
([ADR 0007](0007-protected-build-and-packaged-runtime.md)), whose tested generation of a
default-branch commit is exactly the set of bytes a release should publish.

## Decision

### The bytes

A release publishes the production files of one tested generation of the live default-branch head,
selected by `ci select-generation --with-build`, and nothing else. It never compiles. A `full`
generation and a `reuse` generation are both releasable: the reused bundle was built from the
merged pull request's tested merge, whose tree equals the released commit's, and the plan records
both identities. A mod that stamps a commit hash into its jars sees the pull request's merge
commit there; that is accepted and documented rather than forcing a rebuild after every merge.
SBOMs are published when the Build plan produces them. Byte-for-byte reproducibility stays a Build
concern.

### The trigger

A release is a `workflow_dispatch` of the managed caller `mod-base-release.yml` on the default
branch, at its live head: the only shape the managed guard admits and the only commit
`select-generation` serves. The release creates every tag at that commit itself; nobody pushes a
tag by hand, so no tag can name an untested commit and the "one tag per push" rule disappears. A
dispatch with `publish: false` is the rehearsal: the same plan, no credential.

### The trust split

As for Pages ([ADR 0003](0003-deploy-in-caller.md)) and the gate statuses (ADR 0007 D10), the kit
callee only reads and seals: it authenticates the generation, derives the plan from the protected
release config and seals the plan with the exact assets. The caller's `publish` job holds the
credentials in a default-branch-only environment, runs no action and no adapter hook, runs the kit
only through the managed bootstrap of the protected head (which verifies the pinned kit) and
gives each credential to one step only.

### The network

The uploads are kit Python, not a third-party action: `mod_base.marketplace` adds bounded clients
for Modrinth, CurseForge (upload API, public listing, CDN) and GitHub Releases (API and uploads
origin), each sending its token only to its own origin, refusing redirects (except one
credential-less redirect to the asset storage, as `mod_base.github` does for artifacts) and never
replaying a non-idempotent POST after an answer that may hide its acceptance. This amends the rule
that `mod_base.github` is the kit's only network client. `mc-publish` is dropped because a retried
CurseForge upload duplicates files and its outcome cannot be reconciled by content.

### One approval, parallel uploads

The `publish` job references the environment once, so a release asks for one approval. Inside it,
the GitHub step creates the tags and draft releases, then each marketplace step uploads every lane
with at most four uploads at once. No lock serializes jobs across a release; the caller's single
concurrency group only keeps two releases of one mod apart.

### State and resume

Quick Skin's ledger becomes `mod-base.release.ledger`, one per tag in its draft release body:
`unstarted`, `uploading`, `pending`, `verified`. Intent is written before a file is sent; every
effect reads the ledger and the remote state first; reconciliation is by content (Modrinth by
SHA-512, CurseForge by its listing under either file name plus a CDN download hashed against the
plan, GitHub by normalized asset name plus SHA-256). A re-dispatch resumes; a row stuck in
`uploading` with no remote file stays fenced for the operator.

### Verification without a schedule

The `publish` job verifies right after its uploads and waits a bounded time for moderation, then
publishes every release whose rows are all verified. What moderation still holds back is finished
by the same caller's hourly schedule, in a job with no environment and no secret: it reads only
public marketplace data, takes the plan from the draft's own `mod-base-release-plan.json` asset
(whose digest the ledger binds) and publishes a draft only when its ledger proves every row
verified. That approval was already given for the plan being finished.

### Document kinds

- `mod-base.release.config`: the mod's protected release data in `scripts/ci/mod-base-release.json`.
- `mod-base.release.plan`: the sealed decision of one release: commit, generation, version,
  changelog, tags with their exact assets, and each lane's marketplace metadata.
- `mod-base.release.ledger`: the publication state of one tag, kept in its draft release's body.

## Consequences

- Releasing takes the length of one plan job plus the uploads and moderation, not two more builds.
- A release needs a live tested generation: within the seven days of its Build bundle, at the
  default branch's head. A later release requests a new generation first.
- Each mod keeps only its release config (`scripts/ci/mod-base-release.json`) and its
  environments; the native release workflows retire after the canary.
- The kit gains a network client for three more services, with its own tests and limits, and a
  managed caller holding marketplace credentials. [SECURITY-MODEL.md](../SECURITY-MODEL.md) must
  record both.

## Open points for the owner

- A `reuse` generation is releasable (above). Requiring `full` would force a rebuild after merges.
- The rehearsal is the same dispatch without `publish`; Quick Skin's separate per-target rehearsal
  runs and release-profile E2E are not kept, because the packaged gate already ran the bytes.
- The release canary needs a Modrinth staging project and a CurseForge test project, or a dry run.
