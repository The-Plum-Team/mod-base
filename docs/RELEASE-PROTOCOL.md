# Release protocol

Draft, 1 November 2026. This page is the reference for the shared release pipeline that
[ADR 0008](adr/0008-shared-release-publication.md) proposes: what a release publishes, where its
bytes come from, the jobs and commands that publish it, the documents they exchange and how a
partial publication resumes. Until a kit release carries it and a mod enables it, no mod runs it.

## What a release is

One release publishes one version of one mod: every production JAR of one authenticated tested
generation of the live head of the mod's default branch, to GitHub Releases, Modrinth and
CurseForge. It never compiles anything. The bytes it publishes are the bytes the protected Build
built and the protected packaged E2E ran, selected and verified by
`ci select-generation --with-build` exactly as every other consumer of a default-branch commit
selects them.

A mod names its own tag shape. A mod with one release tag per version (`v{version}`) gets one GitHub
Release holding every lane; a mod with one tag per Minecraft target (`mc{minecraft}-v{version}`)
gets one GitHub Release per target, each holding that target's lanes. Either way one dispatch
publishes the whole version, and every tag is created at the released commit by the release itself.

## Lessons this design keeps

These come from the three native pipelines it replaces and from the Quick Skin 4.0.0 release of
31 October 2026, which took about five hours for 17 targets:

- **Never rebuild what a gate tested.** Quick Skin's tag runs compiled and ran the release E2E again
  on the bytes its validation-only rehearsal had just tested (about 1 h 45 of the five hours);
  Multiplayer Spawners rebuilds on tag, so what it publishes was never tested. Block Pops already
  publishes the jars of a tested `mb-ci-build` bundle.
- **One approval per release.** Quick Skin needed 102 `release` environment approvals (six per tag)
  behind one repository-wide lock, one job at a time: 105 minutes, 57% of them waiting for an
  approval. A job that waits on an environment shows its run as `pending`, not `waiting`.
- **Upload in parallel, reconcile by content.** Every upload is checked before and after it by the
  bytes it published, so a re-dispatch resumes and never uploads a second copy.
- **Marketplaces rename files.** GitHub turns spaces in an asset name into periods; since late
  October 2026 CurseForge turns every run of spaces and hyphens that holds a space into one hyphen
  and its CDN answers 403 for the original name. Reconciliation knows both rules.
- **Do not wait for a schedule.** Quick Skin's five-minute verifier cron fired every three to six
  hours. The publishing job verifies right after its uploads and finalizes what it can.
- **Durable state, not memory.** Quick Skin's ledger (`unstarted`, `uploading`, `pending`,
  `verified`) is the prior art: upload intent is written before a file is sent, so a lost response
  is never uploaded again.
- **Token per step.** Block Pops gives each token to one step only.
- **Declared dependencies and unique version numbers per lane**, as Multiplayer Spawners does.

## Jobs

The managed caller `mod-base-release.yml` runs on `workflow_dispatch` of the default branch only,
with input `publish` (`false` rehearses, `true` publishes), and on an hourly schedule that only
finishes pending releases. It holds one concurrency group, `mod-base-release`, never cancelled.

| Job | Runs | Holds | Does |
| --- | --- | --- | --- |
| `guard` | `mod-base-guard.yml` with `callees: release` | nothing | binds the kit pin as every managed caller does |
| `plan` | the kit callee `release.yml` | `actions: read`, `contents: read` | authenticates the live head's tested generation, derives the release plan from the protected release config, seals the plan and the exact assets as one artifact |
| `publish` | caller-owned, only when `publish` is true | environment `mod-base-release`; `contents: write`; `MODRINTH_TOKEN` and `CURSEFORGE_TOKEN`, each in one step | admits the sealed plan, creates the tags and draft releases, uploads every lane to both marketplaces in parallel, verifies and publishes every release whose rows are all verified |
| `finish` | caller-owned, on the caller's hourly schedule | `contents: write`, no environment and no secret | reads every draft release that carries a ledger, reconciles its pending rows from public data and publishes the drafts whose rows are all verified |

A rehearsal runs `guard` and `plan` and stops: it proves the generation, the plan and every asset
without a credential. A publication is the same run with one approval, given once to the
`publish` job.

The publish and finish jobs run no action and no adapter hook. They fetch the protected head as
the guard does, check that its pin is the kit the guard bound to the run, and run the kit only
through the head's managed bootstrap (`scripts/ci/mod_base_kit.py run`), which fetches and
verifies the kit of that pin. Each step receives only its own credential through `env:`.

## Commands

| Command | Step |
| --- | --- |
| `release plan --repo DIR --release-config FILE --generation DIR --output DIR --github-output F` | in `plan`, after `ci subject --producer generation`, `ci plan` and `ci select-generation --with-build`: derives `mod-base.release.plan` from the generation, its verified bundle and the protected release config, and seals it with exactly the production files it names; outputs `plan_sha256`, `version` and `tags` |
| `release admit --plan-sha256 HEX --output DIR` | first step of `publish`: downloads this run attempt's sealed plan artifact by numeric id, checks its digest and size, admits the plan and every asset again (size, SHA-256, SHA-1, SHA-512), requires the plan's digest to be the one the plan job output and the default branch to be still at the released commit |
| `release github --plan DIR` | `publish`: creates each tag at the released commit (an existing tag elsewhere fails), creates or resumes one draft release per tag with its assets, the plan itself as `mod-base-release-plan.json` and an empty ledger |
| `release upload --plan DIR --marketplace modrinth\|curseforge` | `publish`: uploads every lane of the plan to one marketplace, at most four at once; each row's intent reaches the ledger before its file is sent |
| `release verify --plan DIR --wait-seconds N` | `publish`: reconciles every row from public data, waits within the bound for moderation and publishes each draft whose rows are all verified |
| `release verify --drafts` | `finish`: the same for every draft release that carries a ledger, reading the plan from the draft's own `mod-base-release-plan.json` asset, whose digest the ledger binds |

## Documents

| Kind | File | Holds |
| --- | --- | --- |
| `mod-base.release.config` | `scripts/ci/mod-base-release.json` | the mod's protected release data: marketplace project ids, tag and name templates, the version and changelog sources, and per lane its loaders, game versions, dependencies and version type |
| `mod-base.release.plan` | `release-plan.json` in the sealed artifact | the decision: identity of the released commit, the generation it came from, version, changelog text, every tag with its assets (path, size, SHA-256, SHA-1, SHA-512, lane) and every lane's marketplace metadata |
| `mod-base.release.ledger` | a hidden block at the end of each draft release's body | one row per lane and marketplace: `unstarted`, `uploading`, `pending` or `verified`, with the remote id once verified |

## Module ownership

| Package | Owns |
| --- | --- |
| `mod_base.marketplace` | the bounded HTTPS clients for Modrinth, CurseForge (upload API, public listing and CDN) and GitHub Releases (API and uploads origin), the name rules of each marketplace and the fakes the tests use |
| `mod_base.release` | the config, plan and ledger documents, the plan derivation, the publication state machine, the reconciliation and the `release` command |

## Failure and resume

A dispatch that stops anywhere is re-dispatched. Every effect reads the ledger and the remote state
first: an asset or a marketplace file whose bytes match is accepted as published, a row in
`uploading` with no remote file stays fenced until an operator confirms the marketplace never
accepted it, and a byte conflict fails closed. Nothing is deleted, no tag is moved and no second
version number is invented to hide a partial release.
