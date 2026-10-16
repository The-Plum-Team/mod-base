# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

## Unreleased

- `ci worker-prepare` prints where the root host fence spent its time, as a second line
  `worker-prepare: host fence timing {...}` (the phases `mounts`, `closure`, `walked_closed`,
  `repair`, `verification`, `ok` and `total`, per image tree). The kit already measured it but
  only published it as an audit event, so the 1.5–4 minutes the fence costs in every Build and
  E2E job of a mod could not be broken down from a job log. No document kind, managed file or
  adapter protocol changes.
- A GitHub API client waits for a rate limit to clear, up to `MAX_API_RATE_LIMIT_WAIT_SECONDS`
  (15 minutes) in total per client, instead of failing as soon as `Retry-After` or the reset of an
  exhausted hourly budget asks for more than 30 seconds. On 2026-10-15 a burst of other workflows
  spent a mod's 5,000 requests of the hour; all 34 packaged lanes then failed in "Fetch the sealed
  Build bundle" minutes before the budget reset, and the generation had to run again. Every other
  failure keeps the ordinary 30-second cap, and every request still spends the command's budget.

## v1.6.1

Patch release: a hook whose descendants are still exiting when it returns no longer fails for
leaving a process behind, and a hook that does leave one names it (#37, #38); the callees and
composites move to `actions/upload-artifact` v7.0.2 (#39). A mod moves from v1.6.0 to v1.6.1 with
an ordinary pin bump.

- `ci worker-run` gives the processes of a hook that exited 0 `CI_WORKER_EXIT_GRACE_SECONDS` (10)
  to exit before it fails the hook for leaving one behind, and the error now names up to four
  survivors by pid and inert process name (`worker dispatcher left a process behind: pid 4242
  java`) (#37, #38). A descendant still shutting down, such as a JVM, no longer fails a lane whose
  scenarios all passed; a real leftover still fails it, 10 seconds later. No document kind,
  managed file or adapter protocol changes.
- The callees `build.yml`, `packaged-e2e.yml`, `publish.yml` and `finalize.yml` and the composites
  `prepare-evidence` and `publish-family` pin `actions/upload-artifact` v7.0.2 (`cf430e0`, retries
  an HTTP 429 of the artifact service) instead of v7.0.1 (#39).
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1, the managed files, the workflows' jobs, steps and permissions and the
  request counts are those of v1.6.0. A `worker-run.json` records the longer error line in its
  existing free-form `error` field. The compatibility baseline moves to the archived v1.6.0 reader
  (`tests/fixtures/previous_release/v1.6.0`, built from the release commit like v1.5.0's), which
  reads every kind of this release unchanged.
- Upgrade: bump the pin to v1.6.1 as usual (`python3 scripts/ci/mod_base_kit.py bump --to v1.6.1`,
  then `verify --network` and `template check`). A mod with `admission` lands the bump through its
  protected route like any change to its callers.
- Rollback: an ordinary pin bump back to v1.6.0; nothing is stored that it would not read.

## v1.6.0

Minor release: any current member of the organization that owns a mod may decide its protected
admission (#35, owner decision of 2026-10-13), proven by the gate App's member list rather than by
`author_association`, and a late consumer of a default-branch commit whose shared evidence expired
requests a full generation of the live head with `ci request-generation` (#33, KQ9-4), with route A
decided for Pages consumption of a shared generation (KQ9-3). One new document kind,
`mod-base.ci.org-members`. The managed `mod-base-gate-status.yml` and the callee `gate-status.yml`
change. A mod moves from v1.5.0 to v1.6.0 with an ordinary pin bump; nothing changes for it until
its protected config names an organization approver or its own consumer job calls `ci
request-generation`.

- `ci request-generation --reason TOKEN --github-output F` (KQ9-4, D-Q9-b as the owner accepted it
  on 2026-10-13: one request per commit, never a native fallback, a superseded commit fails
  closed). The step of a consumer's own job after `ci subject --producer generation`, when `ci
  select-generation` selected nothing for `ci-generation-absent` or
  `ci-generation-evidence-unavailable`: while the default branch is still at the commit and no run
  of either managed caller for it is in progress, it dispatches the Build caller and, once that run
  is listed and the branch is still at the commit, the packaged caller
  (`generation.request_generation`). Outputs `requested` and `outcome`: `requested`, `superseded`
  (the branch moved on: an older commit's expired evidence is never rebuilt, also when the branch
  moves while the dispatched Build run is awaited, so the packaged run never lands on a newer
  head), `running` or `already-requested` (a newest dispatched run of the commit did not succeed).
  A pending, failed, mixed or corrupt generation is never requested (a failed one is an
  operator's re-run: a request at once would repeat a rate limit or a cancellation and spend the
  commit's one request), and the reason must agree with the newest runs. A commit whose newest
  run is a dispatched one is requested again only once one of its newest runs lists an expired
  artifact or none, so a stale reason (a re-run request job) starts nothing. Each dispatch is sent
  once: `GitHubApi.post_json(..., replay=False)` retries only a rate-limit answer, never a
  transport error or a 5xx that may hide an accepted dispatch. What it requests is always a full
  generation: the managed callers take no dispatch inputs, `ci reuse-admit` reuses only for a push
  and `ci scope` narrows only a pull request's packaged run, so a reused generation whose
  originals expired is replaced by a full one. Its job runs no hook; its `GH_TOKEN` is an
  installation token of a dedicated App with Actions: write on the mod's repository alone, kept in
  an environment restricted to the default branch (never the org-wide gate App), for the
  dispatched runs' completion to wake the consumer's `workflow_run` (a `github.token` dispatch
  wakes none); its concurrency group is per commit (docs/OPERATIONS.md). New bounds
  `MAX_CI_REQUEST_GENERATION_REQUESTS` (64; 11 for a request whose Build run is listed at the
  first poll, at most 34, and 33 when it never appears), `CI_REQUEST_GENERATION_POLLS` (24) and
  `CI_REQUEST_GENERATION_POLL_SECONDS` (5). The message of `ci-generation-mixed` now says that an
  operator dispatches the full generation.
- Pages consumption of a shared generation (KQ9-3): route A by owner decision of 2026-10-13. The
  mod's consumer workflow is the producer Pages reads (`source.workflow` or
  `families[].producer.workflow`); Pages, `publish.yml`, selection and schemas are unchanged, and
  route B (a shared-generation source kind) is not planned (docs/BUILD-E2E-DESIGN.md).
- Compatibility of KQ9-3 and KQ9-4: no document kind, schema version, managed file, job or step
  changes; `src/` is digested, so the `MB_KIT_TREE_DIGEST` literals move. The baseline moves to the
  archived v1.5.0 reader (`tests/fixtures/previous_release/v1.5.0`, built from the release commit
  1273a53 like v1.4.1's, whose archive the same build reproduces byte for byte), the immediate
  predecessor in this changelog. It reads every kind of this tree that existed in v1.5.0 unchanged.
- Organization approvers of the protected admission (owner decision of 2026-10-13: any current
  member of the organization may decide a `/controller-upgrade`, future members included, with no
  further change). An approver of `admission.approvers` in the Build config may now be
  `{"organization": "<owner>", "role": "member"}` (`config.ADMISSION_ROLES`,
  `config.admission_organization`): every active member of the organization that owns the
  repository, its owners included, whatever the visibility of the membership. At most one such entry,
  spelled exactly as the repository's owner; it may stand beside `{login, association}` entries.
  `author_association` cannot prove a private membership, so the managed `mod-base-gate-status.yml`
  gains a job `members` ("Read the organization members", `workflow.CI_CALLER_JOBS`) that runs only
  when the protected config at the base names such an entry (`locate` reads it, with `contents:
  read`, and outputs `organization` and the admission's `command`); otherwise nothing changes and
  no new permission is needed. In the environment `mod-base-gate`, it checks that an organization
  of that name owns the repository, reads the pull request's comments with the job token (more
  than 300 fail, as the kit's admission does), checks every `User` author that did not post through
  a GitHub App whatever they wrote, and, only when a comment could be a decision (it contains
  `<command> approve ` or `<command> revoke `: its author is a candidate), mints a token of the
  gate App limited to `permission-members: read` and reads the organization's member list until
  every candidate is found (at most 1000 members, by numeric id, never printed; a candidate missing
  from it is asked for its own membership, `active` in the role `admin` or `member`, at most 20).
  Its output is the new document kind `mod-base.ci.org-members` (schema 1, `build_ci.membership`,
  at most `MAX_CI_ORG_MEMBERS_BYTES`, 16 KiB): `checked`, `candidates`, `members` and
  `through_comment_id`. `gate-status.yml` takes it as the new optional input `members` and passes
  it to the verifying call only, as `ci gate-status --org-members FILE` (refused with `--settle`).
  The admission counts a decision of a member by id; a decision the snapshot did not read as one
  (posted after it, or by a checked author who was no candidate, so edited into one since) is a
  published refusal ("evaluate again"); a decision below it by an author the snapshot did not check
  fails without publishing. A failed `members` job does not stop the caller: the evaluation runs
  without the snapshot, and without it an admission that names an organization refuses every
  change to a protected path before reading any comment, a listed user's approval included, so a
  failure, a revocation or a newer failed run is still published and a pull request that changes
  no protected path is evaluated as before. Before writing a `success` (and only then), `publish`
  reads the membership of the same candidates again (never the comments) and publishes nothing if
  it changed. For every approver, listed users included, a comment posted through a GitHub App
  (`performed_via_github_app`) now never decides, and a decision's author must carry a numeric id.
  No request is added to `ci gate-status` (`MAX_CI_GATE_STATUS_REQUESTS` unchanged).
- Owner action before any mod names an organization entry: grant the gate App (`plum-mod-base-gate`,
  and `plum-mod-base-canary` for the gate-status canary) the organization permission "Members:
  Read-only" and accept the request on the installation (docs/OPERATIONS.md, "The gate status App";
  the owner granted it to both Apps and accepted both requests on 2026-10-13). Until then a mod that names one gets no `success` on a pull request that changes a protected
  path (any decision makes its `members` job fail); every other pull request is unaffected.
  The revoke procedure is now a revocation plus a new head or a draft, then a dispatch of the status
  caller, because a collaborator with write access can delete a revocation (docs/OPERATIONS.md,
  "Protected admission"; docs/SECURITY-MODEL.md).
- Compatibility of the organization approvers: one new document kind, `mod-base.ci.org-members`,
  a `new-kind` that the v1.5.0 reader rejects as unknown (ledger, ADR 0007, SCHEMAS.md). The
  managed file `.github/workflows/mod-base-gate-status.yml` changes (the `members` job, `locate`'s
  `contents: read`, three mint steps of the one pinned App-token action, the publish decision and recheck), and
  so does the callee `gate-status.yml` (input `members`). A Build config with an organization entry
  is refused by kits older than v1.6.0: revert the entry before a pin rollback below v1.6.0. The
  `MB_KIT_TREE_DIGEST` literals and the staged-file lock move.
- Compatibility of the release as a whole: the baseline is the archived v1.5.0 reader, the
  immediate predecessor. `mod-base.ci.org-members` is `new-kind` in the ledger and every other kind
  is `unchanged`; the optional organization entry of `admission.approvers` stays within schema 1 of
  `mod-base.build.config`, and a v1.5.0 reader refuses a config that has it. `ADAPTER_API` 1,
  `BUILD_ADAPTER_API` 1 and `pixel_metrics_version` 1 are those of v1.5.0. The only managed file
  that changes is `.github/workflows/mod-base-gate-status.yml`, which `bump` resynchronizes with
  the pin. Without an organization entry the `members` job is skipped and no new permission is
  used, so every evaluation is that of v1.5.0, except that a comment posted through a GitHub App
  never decides and a decision's author must carry a numeric id.
- Upgrade: bump the pin to v1.6.0 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.6.0`, then `verify --network` and `template check`). A mod with `admission` lands the bump
  through its protected route like any change to its callers: the config of the default branch
  still names its listed approvers, so one of them approves the bump's exact head. To let every
  member decide, the mod then adds `{"organization": "<owner>", "role": "member"}` to
  `admission.approvers` in its protected Build config, in a second pull request that the listed
  approvers decide (keep a listed user beside it as a fallback if wanted). Only an organization
  has members: a user-owned repository names no organization entry. The gate App needs the
  organization permission "Members: Read-only" first (above). A consumer that wants expired
  evidence rebuilt adds the `ci request-generation` step and its dedicated App (docs/OPERATIONS.md,
  "Late consumers of a default-branch commit"); nothing else needs a change.
- Rollback: a rollback below v1.6.0 requires removing a mod's organization approver first. Revert
  the organization entry of `admission.approvers` to listed users through the mod's protected
  route (its listed approvers or, while the entry stands, any member decides it), wait until that
  change is on the default branch, and only then bump the pin back to v1.5.0: a v1.5.0 reader
  refuses a config with an organization entry, so a pin rolled back first would run a kit that
  cannot read the mod's protected config. Also remove any
  consumer step that calls `ci request-generation`, a command v1.5.0 does not have. Nothing a
  v1.6.0 run stores outlives it in a form v1.5.0 must read: the `mod-base.ci.org-members`
  snapshot is produced and consumed within one status run, and a generation that `ci
  request-generation` dispatched is an ordinary full generation of the managed callers.

## v1.5.0

Minor release: a pull request's packaged lanes can run a narrowed, sealed runtime scope (#22,
Quick Skin gap QS-G6, opt-in with `runtime_selection`), and `select --family` and `admit` find
each family handoff by its exact name across a whole wave of producer runs (#31), which lets a
mod coalesce the publication of a wave whose producer hands off one key per run. One new document
kind, `mod-base.ci.scope`. A mod moves from v1.4.1 to v1.5.0 with an ordinary pin bump; nothing
changes for it until its protected config names `runtime_selection`.

- Find each family handoff by its exact name across a whole wave of producer runs. `select
  --family` (`pages.select.FamilyGenerations`) and `admit` (ready family handoffs and the
  `current` check) looked a key's `mb-family-handoff--<f>--<key>--a<attempt>` up only in the
  newest `limits.MAX_CANDIDATES` (8) successful producer runs at the leg coverage. A producer that
  hands off one key per run (Quick Skin's mod compatibility review: one run per target, 17 targets,
  15 runs in one observed wave) left the earliest keys of a wave outside that window, so a
  publication that coalesced a wave silently fell back to an older family cache (or the
  carry-forward walk), and recovery could call a site `current` while a newer handoff waited. Both
  now search the newest `limits.MAX_FAMILY_PRODUCER_RUNS` (32, new) producer runs of the one
  `head_sha` listing, newest first, for the exact name of each run's latest attempt; inventories
  are read only until the name is found and are shared by every key, and `admit` reads a run's job
  graph only when its inventory holds a pending key's handoff. Every authentication and fail-closed
  rule is unchanged: inadmissible runs (another repository, event, branch or conclusion) are never
  producers, an earlier attempt's name in a rerun is not the exact name, two uploads of the name in
  one run or one naming another head fail the leg, a selected handoff is still bound to its
  envelope's exact producer attempt and kit (`kit-binding`), and a key with no handoff in the
  window falls back to the superseding or admitted family cache, then the walk, exactly as before.
  The bound only ever hides an older run. The carry-forward walk inventories the same bound of
  producer runs at its earlier commits (also `limits.MAX_CANDIDATES` before), so a wave that a new
  head overtook before it was published is carried by its exact names instead of being walked past
  to an older generation. Worst-case reads: one `select` reads at most 48 more inventories (140 of
  the Pages client's 160 with a full window at the coverage and a full walk, pinned by a test); for
  the QS wave above, `admit` reads 17 inventories and only the job graphs of the runs holding a
  ready key, and a full window at Quick Skin's 17 targets stays within its 160 (pinned by a test). No document,
  schema, CLI flag, job or step name changes; `src/` is digested, so the `MB_KIT_TREE_DIGEST`
  literals move.
- Let a pull request's packaged lanes run a narrowed scope (kit gap QS-G6 of the Quick Skin
  mapping), as the owner decided D1-D5 of docs/BUILD-RUNTIME-SELECTION.md on 11 October 2026. The
  plan still holds every obligation of every lane, so the Build, the packaged run and the status
  evaluation keep one plan. The packaged `input` job runs the new `ci scope` after `select-build`:
  with the job's read-only token it reads the paths the tested merge changes against its base
  (both Git trees) and the base commit's full packaged evidence (the tested record of its newest
  successful push run under the same kit pin, or the reuse reference of one and the original it
  names), then runs the new protected hook `derive_scope` as the validator, without a token, with
  that data in `MB_SCOPE_INPUT`. What the hook selects must be each lane's own obligations in plan
  order; a lane it names none for runs in full. The result is sealed as a new kind,
  `mod-base.ci.scope`, bound to the plan and to the requesting run attempt, and handed to the
  later jobs as the job output `scope`, like the selection record. A lane gives its entry to
  `derive_runtime` and `verify_runtime` in `MB_RUNTIME_SCOPE`; the results index holds the record
  (optional `scope`) and the packaged tested record its digest (optional `scope_sha256`); the
  status description says "for a narrowed scope". Full is the answer, without a record, whenever
  anything is unknown: no `runtime_selection` in the protected config, a push, a dispatch or a
  release, a pull request from a fork, a changed kit pin, a pull request that changes the config, an adapter source, a plan
  input or a control file, a tree the API lists incompletely, a base without complete,
  successful, unnarrowed evidence of the same policy and plan, or a selection that narrows
  nothing (`scope.FULL_SCOPE_REASONS`). Post-merge reuse never lets a narrowed pull request cover
  the push after its merge (new `FULL_RUN_REASONS` entry `original-run-narrowed`), and a reuse
  reference that names a narrowed original is refused. New root operation `take-derived-scope`,
  limits `MAX_CI_SCOPE_REQUESTS` (32), `MAX_CI_SCOPE_BYTES` (64 KiB) and
  `MAX_CI_SCOPE_TREE_ENTRIES` (100,000). Cost: `ci scope` sends no request without
  `runtime_selection`, so the generation ledger is unchanged (198 / 398 / 300); with it, 11
  requests behind a direct baseline and 15 behind a reuse reference, which puts Quick Skin's
  generation at 409 (or 413), within the test budget of 440.
- Compatibility: the baseline moves to the archived v1.4.1 reader
  (`tests/fixtures/previous_release/v1.4.1`, built from the release commit 9f9fddd like v1.4.0's,
  whose archive it reproduces byte for byte), the immediate predecessor in this changelog. One
  new document kind, `mod-base.ci.scope` at schema version 1 (`new-kind` in the ledger), which
  v1.4.1 rejects as unknown; every other kind is `unchanged`. Optional fields within schema 1:
  `runtime_selection` of `mod-base.build.config`, `scope` of `mod-base.ci.results` and
  `scope_sha256` of `mod-base.ci.gate`, which a v1.4.1 reader refuses. `BUILD_ADAPTER_API` stays 1:
  `derive_scope` is an optional hook that runs only for a config with `runtime_selection`.
  `ADAPTER_API`, `pixel_metrics_version` 1 and the managed files are those of v1.4.1. The packaged
  E2E callee gains the `input` job's step "Derive the narrowed runtime scope" and its output
  `scope`, which the receiving step of the later jobs writes for `fetch-build`, `aggregate` and
  `seal-gate` (`--scope`); the job graph, job names and permissions are unchanged. `ci
  select-generation` refuses a reused generation whose original packaged run was narrowed, as
  `reuse.download_reuse_reference` does. Opt-in: without `runtime_selection` every run is that of
  v1.4.1. Remove `runtime_selection` before rolling a pin back below this release.
- Upgrade: bump the pin to v1.5.0 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.5.0`, then `verify --network` and `template check`). A mod with `admission` lands the bump
  through its protected route like any change to its callers. To narrow a pull request's lanes, a
  mod then adds `runtime_selection: {input: "changed-paths-v1"}` to its protected Build config and
  a `derive_scope` hook to its protected adapter (docs/BUILD-RUNTIME-SELECTION.md); that change is
  itself a config change, so its own pull request runs in full. Family handoffs need no change: a
  producer that already hands off one key per run is found by name, and the mod may now publish a
  wave once instead of once per run.
- Rollback: first remove `runtime_selection` from the protected config (a v1.4.1 reader refuses a
  config, results index or tested record that carries the new optional fields), then bump the pin
  back to v1.4.1. A narrowed run's records are only ever read by the release that wrote them:
  post-merge reuse and `ci select-generation` never take a narrowed original, so the push after the
  rollback runs in full. Without `runtime_selection` nothing is stored that v1.4.1 would not read.

## v1.4.1

Patch release: the gallery's **Compare versions** grid gets a cell only for a lane a release
publishes (#29). v1.4.0 was tagged but never became a GitHub Release, so v1.4.1 is the first
Release that carries `ci select-generation` (#25, Q9 groundwork KQ9-1 and KQ9-2): everything in
the v1.4.0 section below ships with it. A mod moves from v1.3.1 (or v1.4.0) to v1.4.1 with an
ordinary pin bump.

- Give the compare grid a cell only for a published lane (#29). `_gallery_release`
  (`pages/build.py`) flattens a release's `minecraft` and `loaders` independently from its lanes,
  and `renderComparison` (`site/assets/gallery.js`) crossed the two lists, recreating pairs that
  are no lane. A key that spans several Minecraft versions with loaders on only some of them (Block
  Pops' `master`: Forge only on 1.20.1, NeoForge only on 1.21+) showed an empty slot ("No validated
  capture was published for this exact cell.") for each such pair: 30 cells for 20 lanes. The grid
  now skips a pair that is not a lane of the release, from the gallery's own `lanes` records; a
  loader absent from the whole release keeps its "Not applicable" cell. On the published data Block
  Pops goes from 30 cells to 20 (one per capture) and Quick Skin, whose keys each hold one Minecraft
  version, keeps its 34 identical cells. No schema, builder or document changes; `site/` is
  digested, so the `MB_KIT_TREE_DIGEST` literals move.
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1, the managed files, the workflows' jobs, steps and permissions and the
  request counts are those of v1.4.0. The compatibility baseline moves to the archived v1.4.0
  reader (`tests/fixtures/previous_release/v1.4.0`, built from the release commit like v1.3.1's),
  the immediate predecessor in this changelog, which already reads `mod-base.ci.generation`, so
  every kind is `unchanged`. Against v1.3.1 the compatibility, upgrade, consumer adoption and
  rollback notes of v1.4.0 apply as written.
- Upgrade: bump the pin to v1.4.1 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.4.1`, then `verify --network` and `template check`). The next Pages run publishes the site
  with the new `gallery.js`. A mod with `admission` lands the bump through its protected route like
  any change to its callers.
- Rollback: an ordinary pin bump back to v1.4.0, or to v1.3.1 after removing the consumer jobs as
  the v1.4.0 rollback note says; nothing is stored that either release would not read.

## v1.4.0

Minor release: `ci select-generation` (#25, Q9 groundwork KQ9-1 and KQ9-2). A consumer job of a
mod's default branch (status pages, visual review, feature coverage, mod compatibility, Pages)
can now read the one Build bundle and the one set of runtime results that were tested together
for a commit, whether the commit's own gates tested them or post-merge reuse covers it with the
merged pull request's gates. Before this, the gates could seal a reuse reference but no command
followed one, so a consumer of a push had to keep reading native push runs. Nothing changes for a
mod that does not add a consumer job: the managed callers, the gates and their request counts are
those of v1.3.1. v1.4.0 is tagged first and becomes a GitHub Release once the canary passes it.

- Add `ci select-generation` (Q9 groundwork, KQ9-1): the read-only step of a consumer of a
  default-branch commit selects and authenticates the tested generation that covers it, whether
  the commit's own runs tested it or post-merge reuse covers it with the merged pull request's
  gates, and publishes it privately and atomically as `generation.json` (and, with
  `--with-build`, the complete Build bundle the packaged gate tested). Before this, `ci
  reuse-admit` could admit a reuse but no command consumed a reuse reference, so no consumer of a
  push could follow one. The newest run of each managed caller is selected before any result is
  read; the packaged run decides the mode and the Build run must agree, or the generation is
  mixed and rejected (`ci-generation-mixed`, exit 2). Both reuse references must name the same
  original pair, which is authenticated once as `reuse.download_reuse_reference` authenticates
  it (references never chain). A run in progress, a failed newest run, no packaged run and
  evidence that has expired or is gone select nothing (exit 3, `ci-generation-pending`,
  `-failed`, `-absent`, `-evidence-unavailable`): the consumer requests a full generation instead,
  and nothing partial is published. Outputs `generation` (`full`, `reuse`, `none`) and `reason`.
  With `--lane ID` (repeatable) or `--all-lanes` it also keeps each chosen lane's export (reports,
  logs, screenshots, as the adapter wrote them) under `lanes/<id>/` (KQ9-2): the results index must
  list exactly the packaged gate's lanes, Build and reports, and each lane is verified as `ci
  aggregate` verifies it and must be what the index sealed. A reused bundle or lane keeps the
  identity it was built or run under. New module `build_ci.generation`, new limit
  `MAX_CI_SELECT_GENERATION_REQUESTS` (96 + 3 x `MAX_CI_LANES` = 864: 27 requests for a full
  generation, 52 for a reused one, 2 for the bundle, 2 for the index and 2 per lane kept, 124 for
  every lane of a reused Quick Skin generation). `transport._read_sealed_lane` can keep the
  verified export (`keep`).
- Add the consumer subject `ci subject --producer generation --pr ""`: a job of the mod's own
  workflow on the default branch (any workflow of the mod, on a push, `workflow_run`,
  `workflow_dispatch` or `schedule`) authenticates the live default-branch head exactly like a
  push and derives the identity, and so the plan, of that commit's own gates; it never has a pull
  request, runs no candidate hook and seals nothing. With it a consumer job runs the steps of the
  status callee's evaluate job and then `ci select-generation` (BUILD-PROTOCOL.md, "The subject
  of a job"). New `identity.GENERATION_PRODUCER`, `GENERATION_EVENTS` and `admits_workflow`.
- New document kind `mod-base.ci.generation` v1 (`records.validate_generation`,
  `records.generation_record`), written only by `ci select-generation`, never by a gate; a v1.3.1
  reader refuses it as unknown.
- `reuse.download_reuse_reference` is split into the reference read and the original pair read
  (`reuse._read_reference`, `reuse._originals_finished`) so that `ci select-generation` reads two
  references and the pair once; its behaviour and request count are unchanged.
- Compatibility: `ADAPTER_API`, `BUILD_ADAPTER_API` 1, `pixel_metrics_version` 1, the managed
  files and the workflows' jobs, steps and permissions are those of v1.3.1 (only the
  `MB_KIT_TREE_DIGEST` literals move, as at every release). The one new kind is
  `mod-base.ci.generation` v1, which no gate writes or reads; a v1.3.1 reader refuses it as
  unknown. Every other kind is `unchanged` against the archived v1.3.1 reader
  (`tests/fixtures/previous_release/v1.3.1`), the immediate predecessor in this changelog.
- Upgrade: bump the pin to v1.4.0 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.4.0`, then `verify --network` and `template check`). `template sync` rewrites no managed
  file beyond the pin. A mod with `admission` lands the bump through its protected route like any
  change to its callers.
- Consumer adoption (optional, after the bump): one job per consumer family, on `workflow_run` of
  "mod-base Build" and "mod-base packaged E2E" for the default branch, with only `actions: read`
  and `contents: read`. Its steps are those of the kit's `gate-status.yml` evaluate job: check
  out the mod at `github.sha` and the pinned kit; `ci subject --producer generation --pr ""`;
  check out the same commit as `candidate`; `ci worker-prepare --roles validator`; `ci plan
  --pin-candidate candidate`; `ci select-generation --output "$RUNNER_TEMP/generation"
  [--with-build] [--lane ID ... | --all-lanes]`; and always `ci worker-finish`. The family then
  reads `generation.json`, the bundle under `build/` and the lane exports under `lanes/<id>/`.
  Exit 3 publishes nothing: `pending` resolves itself when the other caller's `workflow_run`
  fires; after `failed`, `absent` or `evidence-unavailable` the consumer asks for a full
  generation (D-Q9-b), never a native fallback. Exit 2 (`ci-generation-mixed`, corrupt evidence, API
  failures) is a rejection. Before porting a family, check that the lane export carries every
  file its native consumer reads; a missing file is an adapter change in the mod. Remove a
  native push duplicate only after every family that read it consumes the selected generation.
- Rollback: first remove or disable the consumer jobs that call `ci select-generation` or `ci
  subject --producer generation` (v1.3.1 has neither and refuses both), then an ordinary pin bump
  back to v1.3.1. Nothing the gates store changes: `generation.json` stays in the consumer job's
  private output and the kit never uploads it, so v1.3.1 never meets the new kind.

## v1.3.1

Patch release: one kit process remembers its successful pixel checks by content (#26), which
cuts the protected policy's conformance run of a mod with public evidence to a third. Nothing else
changes; a mod moves from v1.3.0 (or v1.2.1) to v1.3.1 with an ordinary pin bump.

- Remember successful pixel checks by content within one process (#26). A public-evidence
  conformance run measured the same PNG bytes thousands of times (`inspect_png` about 8,940 calls
  in Block Pops' run, through the handoff, raw-bind, prepare, anchor and compact checks) and
  compared the same pairs again at every validation pass. `inspect_png`, `inspect_webp` and
  `compare` now keep successful results in a bounded, process-local LRU
  (`limits.MAX_PIXEL_MEMO_ENTRIES`, 8,192 entries). The key is the SHA-256 the imaging module
  computes itself from the exact bytes it has just read and admitted, plus every argument that
  shapes the result: the format and the size policy (only the kit's own `SizePolicy`; a subclass is
  measured every time), and for `compare` both payload hashes and the exact type and value of
  `minimum_changed_fraction` and `region`, which the result echoes (`0`, `0.0` and `-0.0` are
  different keys; other types are never remembered). A caller's claimed hash is never a key,
  nothing is kept on disk or shared between processes, a rejection is never remembered, results
  are handed out as copies, and the reading, byte-bound and signature checks still run on every
  call. Measured on the conformance child (5 concurrent, Python 3.13), reports byte-identical:
  Block Pops master 707 s -> 308 s, Quick Skin master 3,897 s -> 1,272 s.
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1 (every metric is computed exactly as before), the managed files, the
  workflows' jobs, steps and permissions and the request counts are those of v1.3.0. The
  compatibility baseline moves to the archived v1.3.0 reader (`tests/fixtures/previous_release/v1.3.0`,
  built from the release commit like v1.2.1's), the immediate predecessor in this changelog.
- Upgrade: bump the pin to v1.3.1 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.3.1`, then `verify --network` and `template check`). A mod with `admission` lands the bump
  through its protected route like any change to its callers.
- Rollback: an ordinary pin bump back to v1.3.0; nothing is stored that v1.3.0 would read.

## v1.3.0

Minor release: an optional protected admission in the kit's gate status (#18), which Block Pops
needs before B3 (`shared-build-and-e2e`) and Quick Skin before its required contexts move to the
kit, and the new-mod guide with its gate environment tool (#19). Nothing changes for a mod that
does not add `admission` to its protected config. v1.3.0 is tagged first and becomes a GitHub
Release once the canary passes it.

- Add an optional protected admission to the kit's gate status (#18, the B3 blocker of the Block
  Pops activation: in `shared-build-and-e2e` no evaluator of the mod runs on a pull request, and
  the gate status checked no protected path, no owner approval and no restricted transition). The
  optional `admission` of `mod-base.build.config` (within schema 1: `protected_paths`, `command`,
  `approvers`) is read from the protected default branch. When the tested merge of a pull request
  changes a protected path (its tree entry against the base), `ci gate-status` turns every gate
  that would have been `success` into `failure` unless the latest decision of a listed approver on
  the exact head, a comment `<command> approve <head sha>`, approves it; a revocation, an edited
  decision or none at all refuses. The list must protect the config, the adapter sources, the
  activation manifest and the four callers. New module `build_ci.admission`, limits
  `MAX_CI_ADMISSION_PATHS` (64), `MAX_CI_ADMISSION_DIRECTORIES` (16), `MAX_CI_ADMISSION_APPROVERS`
  (8) and `MAX_CI_ADMISSION_COMMENTS` (300), and `FakeGitHub.set_comments`. Cost: none without
  `admission`; 3 requests for a pull request that changes no protected directory, and at most
  1 + 2 * 16 + 8 more otherwise, within the unchanged `MAX_CI_GATE_STATUS_REQUESTS` (96). Opt-in:
  without `admission` every evaluation is that of v1.2.1. A comment does not start the status
  caller: after a decision on a finished generation the owner dispatches it
  (OPERATIONS.md, "Protected admission").
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1, the managed files, the workflows' jobs, steps and permissions are
  those of v1.2.1. A kit before v1.3.0 refuses a config that has `admission`. The compatibility
  baseline moves to the archived v1.2.1 reader (`tests/fixtures/previous_release/v1.2.1`, built
  from the release commit like v1.2.0's), the immediate predecessor in this changelog.
- Add the new-mod guide and the gate environment tool (#19). `docs/NEW-MOD.md` orders the
  steps from an empty repository to the shared gates: repository settings and ruleset, the kit
  files, Pages and the evidence adapter, the Maven mirror, the `mod-base-gate` environment, the
  Build adapter, `disabled -> shadow` and `shadow -> shared-build(-and-e2e)`, with what to check
  before each move. `tools/new_mod_env.py` (stdlib, Python 3.11+) creates that environment,
  limits it to the default branch and reads the restriction back before it stores the client id
  variable and the key secret; it reads the key only from the path it is given (a file, a pipe or
  `-`), refuses a key file other users can read, never prints the key and deletes nothing.
  OPERATIONS.md records the gate App (`plum-mod-base-gate`, installed on every repository of the
  organization, its key only in each mod's environment and the owner's copy only in a password
  manager). No `src/` code and no managed template file change; the staged-file lock covers the
  new tool.
- Upgrade: bump the pin to v1.3.0 as usual (`python3 scripts/ci/mod_base_kit.py bump --to
  v1.3.0`, then `verify --network` and `template check`; OPERATIONS.md, "Bumping the kit in a
  mod"). Admission stays off until the mod enables it: add `admission` (`protected_paths`,
  `command`, `approvers`) to `scripts/ci/mod-base-build.json` in a pull request that goes through
  the mod's existing approval route; the config of the default branch decides, so it applies from
  that merge on. The list must cover the config, every adapter source, the activation manifest and
  the four `mod-base-*.yml` callers. From then on approve each exact head of a pull request that
  changes a protected path with `<command> approve <head sha>` and dispatch
  `mod-base-gate-status.yml` when its gates already finished (OPERATIONS.md, "Protected
  admission").
- Rollback: to switch admission off, remove `admission` from the config through the same
  protected route; v1.3.0 without it evaluates as v1.2.1. To pin back to v1.2.1, remove
  `admission` first: v1.2.1 refuses a config that has it. Without `admission` the rollback is an
  ordinary pin bump to v1.2.1.

## v1.2.1

Supersedes v1.2.0, which stays a tag and is never released. v1.2.1 contains everything of
v1.2.0: the single read of a mutable state to start watching it (QS-G7, #15) with its smaller
request budget, and the optional protected seeds (QS-G2/G3, #16); see the v1.2.0 section below. A
mod moves from v1.1.3 straight to v1.2.1. v1.2.1 is tagged first and becomes a GitHub Release once
a canary Build whose protected policy outlives its targets passes on it.

- Seal the Build only after the protected policy has finished (#20). `ci assemble` requires every
  job before it in its attempt (the plan, the policy and every target) to have finished as the
  graph expects, but the `assemble` job of `build.yml` needed only the plan and the targets.
  Wherever the protected policy outlives the targets, as in the Quick Skin and Block Pops shadow
  runs, assemble refused ("required job 'Shared Build / Verify protected policy' has not finished
  as the graph expects") and the gate then failed on the missing bundle; the synthetic canaries
  never showed it because their policy is faster than their targets. The gap dates from v1.1.0.
  `assemble` now needs `[plan, policy, target]`: it waits for the slowest leg, as the gate already
  did, so a Build takes no longer; a failed policy skips the assembly and the gate fails, as
  before. A new test requires every job that reads its own running attempt (assemble, aggregate,
  both gates and the input of a rebuilt run) to need every job it requires to have finished, in
  every mode of both producers.
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1, the managed files, the job graphs and their digests, job names,
  permissions and request counts are those of v1.2.0. The compatibility baseline moves to the
  archived v1.2.0 reader, a tag without a Release as v1.1.2 was.

## v1.2.0

Release candidate. v1.2.0 is tagged but has no GitHub Release: it becomes one only after the
owner's decisions listed in pull request #16 (cache-scope trust, cache storage, the per-lane
runtime keys, a job-level `cache-mode`, required key files and the runtime store layout) and a K7
canary run on this kit, which pull request #15 asks for before a release.

- Read a mutable state once to start watching it (kit gap QS-G7 of the Quick Skin mapping). A
  protected command that has just read an artifact through its run's listing, or a receipt read
  that has just rechecked the Build run and bundle, starts watching that state from the answer it
  already has instead of reading it again (`reads.Watch.observe`, `reads.Watch.after`). Only what
  an earlier recheck found unchanged is lent, and every recheck before an effect still reads
  everything its command depends on; a listing row that the read by id contradicts fails the
  recheck, closed. `ci assemble` authenticates its attempt as the gates do and then publishes
  (`gate.collect_targets`). Costs: `ci assemble` 13 + 2 per target (was 16), `ci select-build`
  16 / 14 (was 17 / 15), the packaged `ci seal-gate` 22 (was 23), `ci gate-status` 36 (was 45).
  The generation ledger is 198 / 398 / 300 for the synthetic mod, Quick Skin and Block Pops (was
  212 / 412 / 314), or 207 / 407 / 309 with a changed candidate pin (was 221 / 421 / 323). The
  caps in `model/limits.py` are unchanged. `transport.download_target_set` and
  `transport.download_gate_receipt` keep their documented behaviour but no longer have a
  production caller; whether to retire them is an owner decision.
- Add optional protected seeds (kit gaps QS-G2 and QS-G3): the optional `seeds` of
  `mod-base.build.config` (within schema 1), `ci seed-key`, `ci seed-export` and the root
  operation `export-seed`. Target and lane jobs restore an exact protected key (derived from the
  protected checkout of the default branch and the planned unit, never chosen by the candidate)
  with `actions/cache/restore` v6.1.0 and stage it only on an exact hit; only protected pushes and
  dispatches export the candidate's Gradle home after the upload and save it, never a pull
  request. No request is added to the ledger. The feature is opt-in: without `seeds` every job
  behaves exactly as in v1.1.3, and `seeds` stays off until the owner has made the checks in
  OPERATIONS.md, "Protected seeds".
- Compatibility: document kinds and their schema versions, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1 and the managed files are those of v1.1.3. The Build and packaged E2E
  callee workflows gain the seed steps; their job graph, job names and permissions are unchanged.
  A kit older than v1.2.0 refuses a Build config with `seeds`: remove `seeds` before rolling a pin
  back below v1.2.0.

## v1.1.3

- Decide a gate whose evidence is gone instead of failing the status evaluation. The K7 canary
  (The-Plum-Team/mod-base-canary, evidence rows "R1-F | F1" and "R1-F | F2") deleted the Build
  bundle of PR #13 three seconds after its packaged run selected it: the lanes failed their fetch
  and the packaged run failed, as designed, but every status run (38043290126, 38043626870,
  38044221317) stopped in "Evaluate gate states" on `github-not-found ... actions/artifacts/
  11666144281 ... HTTP 404`. A single artifact is read by its numeric id, and v1.1.2 let that 404
  propagate as an API failure, so no intent was published: both statuses stayed `pending` with a
  stale "in progress" description and the failed packaged run was never reported as `failure`.
  `ci gate-status` now answers `failure` for such a gate ("the newest Build run was rejected:
  artifact N it needs is gone (HTTP 404)"), as it already did for an artifact that has expired or
  that its run's listing no longer shows. Only a `GET` of an artifact's metadata or archive that
  answers 404 or 410 counts (`transport.artifact_gone`, which the readers of a merged pull
  request's original evidence already used); any other API failure still produces no intent.
  Recovery is unchanged: rerun the Build, then the packaged run (canary case F3).
- Nothing else changes: request counts, schemas, `ADAPTER_API`, `BUILD_ADAPTER_API` 1,
  `pixel_metrics_version` 1 and the managed files are those of v1.1.2.

## v1.1.2

- Refuse the jobs GitHub carries over into a rerun of failed jobs only. The K7 canary
  (The-Plum-Team/mod-base-canary, evidence rows "R1-C | C2" and "R1-C | C4") reran the failed
  jobs of Build run 38032224931: the job listing of attempt 2 showed the plan and guard jobs of
  attempt 1 under new job ids with `run_attempt` 2, but with the times and runner of attempt 1
  (the plan job started at 06:48:55Z, attempt 2 at 06:55:06Z). v1.1.1 refused only a job whose
  `run_attempt` was lower, so it sealed a green attempt 2 fed by the plan of attempt 1 and the gate
  status App published success; packaged run 38032224927 carried its guard job over the same way.
  Every reader of one attempt's jobs now also refuses a job that started before the attempt's
  `run_started_at`, with "a failed-jobs-only rerun mixes attempts; rerun all jobs". Times compare
  as whole seconds, so a job that starts in the second its attempt does is the attempt's own;
  skipped jobs and jobs without a start time are not judged by time. A rerun of all jobs runs
  every job again and is accepted: in the attempts 3 of both canary runs, reruns of all jobs that
  ran v1.1.1, every job started after its attempt (first jobs at 07:07:40Z and 07:29:44Z, attempts
  at 07:07:36Z and 07:29:41Z), and the tests replay these listings. No hosted run of v1.1.2 has
  shown it yet: K7 has to repeat C2 and C4 on a v1.1.2 pin.
- The start of an attempt comes from the run record each command already reads. Only
  `ci assemble`, which reads its jobs before its run, reads the record of its own attempt: 16
  requests and 2 per target (50 for 17 targets), within its unchanged budget. The
  workflow-derived generation ledger moves by one: synthetic 212, Quick Skin 412, Block Pops 314
  (221/421/323 with a changed candidate pin). Nothing else changes: schemas, `ADAPTER_API`,
  `BUILD_ADAPTER_API` 1, `pixel_metrics_version` 1 and the managed files are those of v1.1.1.

## v1.1.1

- Admit the `.git/config.worktree` that actions/checkout leaves in every checkout (it runs
  `git sparse-checkout disable`). v1.1.0 refused that metadata in `stage-candidate`, so no
  policy, target or lane job could run on a hosted runner; the K7 canary found it. Like `config`,
  the file is never read or copied. The hosted pipeline test now checks out with the same
  commands as the action.
- Restore the managed `.gitattributes` of v1.0.3. v1.1.0 added `eol=lf` rules for the four
  Build/E2E callers, but Block Pops can change that file through no pull request a kit bump can
  be, so v1.1.0 could not be adopted there. A Windows clone with `core.autocrlf=true` gets CRLF
  copies of the callers, which `template check` reports as line-ending drift with its fix: add
  `/<path> text eol=lf` to `.git/info/attributes` and check the file out again, or run
  `template sync --write`; never commit CRLF. Nothing else changes: schemas, `ADAPTER_API`,
  `BUILD_ADAPTER_API` 1 and `pixel_metrics_version` 1 are those of v1.1.0.

## v1.1.0

Adds the protected Build and packaged E2E pipeline (design steps K1–K6). Nothing changes for a
mod until it adds a Build adapter and an activation manifest; Pages, its schemas, `ADAPTER_API`
and `pixel_metrics_version` 1 are unchanged. The tag is the K7 release candidate: mods pin it only
after its canary is green and its GitHub Release is published.

- Add `BUILD_ADAPTER_API = 1`, separate from the Pages adapter: eight hooks derive the plan
  and runtime values, run policy/target/lane work, and verify target/Build/runtime exports.
  A protected hashed adapter closure supplies validation; mod hooks write native files only.
- Add thirteen strict document kinds at `schema_version: 1`: `mod-base.build.config`,
  `mod-base.build.plan`, `mod-base.build.envelope`, `mod-base.ci.runtime-envelope`,
  `mod-base.ci.validation`, `mod-base.ci.selection`, `mod-base.ci.results`,
  `mod-base.ci.gate`, `mod-base.ci.reuse`, `mod-base.ci.execution`,
  `mod-base.ci.root-request`, `mod-base.ci.activation` and `mod-base.ci.batch`.
  The previous release rejects these new kinds; existing Pages schemas, `ADAPTER_API`
  and `PIXEL_METRICS_VERSION` are unchanged.
- Add `ci subject`, `worker-prepare`, `plan`, `worker-stage`, `worker-run`, `worker-seal`,
  `worker-validate` and `worker-finish`. Protected orchestration admits the exact PR merge
  or default-branch subject and runs candidate code in a disposable account without tokens.
  A second account runs the protected verifier before any sealed artifact is uploaded.
- Fence hosted Linux tools before creating accounts. Closed root operations verify the kit
  digest, stage source/Build/kit inputs, prove tracked sources unchanged, freeze exports and
  seal validation reports through private requests. Account cleanup revokes user managers,
  linger, cron and at jobs as well as processes; an error prevents successful sealing.
  Admit linked JDK certificates through stable ancestor identity/type/permissions while keeping
  full tool stamps and race checks; unrelated `/etc` timestamp changes no longer reject admission.
- Admit a changed candidate kit pin once per run, bind it into the optional v1 plan field
  `candidate_kit`, and use a separate pinned checkout for candidate-only execution. Protected
  code verifies release tag, main ancestry, commit, own digest and both locks; the executing kit
  retains all privileged work. Stage/root record formats and the job graph stay unchanged;
  checkout steps are added. Released rollbacks use the same checks. Older strict plan readers
  and unsupported digest/lock formats require a compatibility-first protected release.
- Reduce host-fence traversal by closing only unused Android, CodeQL, .NET and Swift SDK roots.
  Prove closure against real accounts, ACL/hard-link and bind-mount aliases; retain full repair
  when a mount is ambiguous. All other repair coverage and existing deadlines remain intact.
  Bound mountinfo to 64 KiB and report first-fence phase/tree timings in every hosted CI job.
- Add `ci assemble` for the exact union of target partitions and `ci aggregate` for the
  packaged results index. The index authenticates every lane's descriptor and receipt hashes
  without combining all lane bytes into one archive. Complete Builds require their validation
  record and expected native reports; commands bind the loaded protected config digest.
- Add the optional `runtime.system_profile` to `mod-base.build.config` (schema 1) and
  `ci system-profile`: a packaged lane installs the named kit profile (`xvfb-mesa`, Xvfb and Mesa
  software rendering) as root before the host fence and its accounts. Mods name it, never packages.
- Add `ci select-build` and `ci fetch-build`: select the newest exact Build, pass the canonical
  selection between jobs of the same attempt, and fetch its archive by numeric id and digest.
  Candidate-checkout subject derivation costs one request; a lane fetch costs two including
  storage. Gates repeat the live source/newest-run observations before sealing.
- Add `ci seal-gate` and read-only `ci gate-status`. Full caller/callee graphs, upload chronology,
  attempts, descriptors and paired native receipts determine gate intents. Required statuses
  are published only by the managed caller's native job holding the mod's App credentials.
- Add reusable `build.yml`, `select-build.yml`, `packaged-e2e.yml` and `gate-status.yml`, with
  pinned actions, per-job read permissions, sealed uploads and always-run worker cleanup.
  Build runs one target per runner; packaged E2E runs one lane per runner against that Build.
- Add the managed callers `mod-base-guard.yml`, `mod-base-build.yml`,
  `mod-base-packaged-e2e.yml` and `mod-base-gate-status.yml`. They bind the protected caller
  and kit references, defer drafts, filter PRs to the canonical base and render the single pin.
  Managed `.gitattributes` now forces LF for all four, including Windows clones.
- Add activation modes `disabled`, `shadow`, `shared-build`, `shared-build-and-e2e` and
  `reviewed-rollback`, checked by `template check/sync/init`; add `template activation` and
  `template transition`. Mode transitions use their own PR at an unchanged pin. Bootstrap
  bump checks target-kit support and restores pin files if its write phase fails.
- Add `ci batch-prepare` and `ci batch-settle`: reproduce ordered native Git changes on the
  current base, open a batch, then verify its merged tree and both gate records before closing
  unchanged members. Up to 50 members; expired original evidence exits 3.
- Add `ci reuse-admit` to protected-push planning/selection. Identical tested tree, policy,
  plan, kit and retained original gates admit reuse; normal misses run full gates and corrupt
  evidence/API failures reject. Reuse gates re-admit and write `ci-reuse.json`, a direct
  reference to original PR gates that renews no artifact retention.
- Bind policy digests to the activation manifest and managed callers as well as adapter/config
  and kit bytes, so control-plane changes require full post-merge gates. Retain original
  merged Build/gate readers; original runtime consumers must use the results index and lanes.
- Accept native export names with spaces and optional target-scoped SBOM/report outputs.
  Wait at most 15 seconds/four observations for a pending test merge; stale bases use
  `ci-pr-base-outdated` and request a branch update. Allow two seconds of upload-clock skew
  while keeping other chronology exact. Failed-jobs-only mixed attempts are refused.
- Pin API command caps and workflow-derived generation costs: synthetic 211, Quick Skin 411,
  Block Pops 313, including one final status evaluation and storage GETs, with no waiting.
  Changed candidate pins cost 220/420/322 respectively in the pinned annotated-tag case;
  matrix workers verify the plan locally without repeating release admission.
  Pending selection polls add one each. `MAX_CI_GENERATION_REQUESTS = 440` is a test-only
  regression budget; structural 256/256 plans exceed the repository's 1,000 REST/hour allowance.
  These are FakeGitHub kit-traffic measurements, excluding third-party Actions and retries.
- Add native parity/graph fixtures and required Python 3.11–3.13 ordinary, hosted worker,
  deferred-execution and full command-chain CI jobs. The pipeline exercises real Git, accounts,
  hooks, root operations and artifacts with fake GitHub, plus corruption/missing/stale/draft controls.
  Both the ordinary and future-kit generations traverse the complete command chain; the future
  kit differs and refuses execution by protected or validator accounts.
- Keep retention at one day for targets, seven for Build/lane/results, 90 for gates/reuse.
  Owner review remains for common 512 MiB archives, whole-lane 512-file/256 MiB bounds and
  Quick Skin's 4 MiB native-report cap. Protocol and artifact bounds are not raised for failures.
- Operator prerequisites: ratify the design/ADR, complete K7 and release; then provide native
  adapters and, where wanted, cache restore/save (current Gradle seeds are empty).
  Provision `mod-base-gate` with `MOD_BASE_GATE_APP_CLIENT_ID` and
  `MOD_BASE_GATE_APP_PRIVATE_KEY` before managing the status caller. Transition admission
  remains an operator route; batch commands need reviewed paths/procedure and App credentials.
- Adoption constraints: per-hook timeout admission does not guarantee whole-job fit and remains
  the one documented expected failure. The candidate-pin regression now passes. Runtime reuse
  consumers remain Q9/B6.

## v1.0.3

A front-end release: every validated capture of the E2E gallery has its own URL. Schema versions
(all 1), `pixel_metrics_version` 1, `ADAPTER_API` 1 and every managed file are unchanged; mods move
with `python3 scripts/ci/mod_base_kit.py bump --to v1.0.3`, and the new gallery reaches a site with
its next publication.

### Added

- **Capture URLs.** `e2e/#capture/<key>/<frame_id>` (both percent-encoded per segment) opens that
  capture's validation record over the gallery of its release, version, loader, scenario and role.
  Opening a record from the page writes its address with `history.replaceState` (no new history
  entry), and closing it restores the gallery address. A pasted or edited address is followed on
  `hashchange`. Every card has a "Link to this capture" link, and every record has the link plus a
  "Copy link to this capture" button, which falls back to showing the URL where the clipboard is
  unavailable. Each link's accessible name also names its capture. A record replaced in place starts
  at its top with focus on its title, and a record's own link only rewrites the address. An address
  naming no published frame, such as one from an earlier generation, opens nothing and says so in
  the status line. The fragment is only looked up in the frame inventory. It
  never reaches the page as markup, and every `href` still passes `sameOriginPath`.
  `docs/SCHEMAS.md` ("Capture URLs") records the format.

## v1.0.2

A fix release for a defect Block Pops' adoption found at `v1.0.1`. Schema versions (all 1),
`pixel_metrics_version` 1 and `ADAPTER_API` 1 are unchanged. One managed file changes, the
bootstrap `scripts/ci/mod_base_kit.py`, which `bump --to v1.0.2` rewrites; no other mod change is
needed.

### Fixed

- **Pillow installed with `pip install --user`.** Block Pops' credentialless candidate sandbox
  installs the hash-locked Pillow into the sandbox account's user site, and its Build gate's
  `conformance` run (run 36239090095) failed with `hook-failed: adapter hook 'synthesize' failed:
  ModuleNotFoundError: No module named 'PIL'`: the bootstrap's `run` starts the kit with
  `PYTHONNOUSERSITE=1`, and the kit's isolated children, the hook child and the `conformance`
  simulation child, run with a private `HOME` and `PYTHONNOUSERSITE=1`, so none of them saw that
  user site. The new `mod_base.adapter.host.imaging_user_site()` names this process's user base
  only when this process has its user site enabled and the hash-locked Pillow (`PIL`, located and
  never imported) is a package directly in that user site; then the child gets
  `PYTHONUSERBASE=<that user base>` in place of `PYTHONNOUSERSITE=1`, so its user site is the
  parent's own, after the standard library exactly as in the parent (`PYTHONPATH` is never
  extended). The managed bootstrap applies the same rule when `run` starts the kit. In every other
  case (a global or virtual-environment Pillow, as in every Pages job, a disabled user site, no
  Pillow, or a user base that is relative, missing or holds `:` or a control character) every
  environment is exactly the `v1.0.1` one. `docs/ADAPTER.md` ("Isolation") and
  `docs/SECURITY-MODEL.md` record the rule.

## v1.0.1

A fix release for three defects the Quick Skin adoption found at `v1.0.0`. Schema versions (all 1;
the template manifest gains one optional field), `pixel_metrics_version` 1 and `ADAPTER_API` 1 are
unchanged, and so is every managed file: mods move their pin with `bump --to v1.0.1`. Two fixes can
need a mod change in that same pull request, marked **Action** below.

### Fixed

- **Dependabot and the caller's managed pins.** The managed region of `.github/workflows/pages.yml`
  pins `actions/deploy-pages`, which only a kit bump may move, yet `template check` required only
  the `The-Plum-Team/mod-base*` ignore and the seeded `.github/dependabot.yml` lacked one for it, so
  a Dependabot bump of it would have been managed-file drift. The Dependabot fragment rule now also
  requires, in every `github-actions` update, an ignore entry with no `versions` or `update-types`
  for every third-party action pinned in the managed region of each workflow its manifest entry
  lists in the new optional `template/manifest.json` field `ignore_actions_of`; `template check`
  derives the names from the kit's own template (today `actions/deploy-pages`), and the seed
  carries the entry. **Action:** a `.github/dependabot.yml` without
  `- dependency-name: "actions/deploy-pages"` in its `github-actions` ignore list fails
  `template check` at `v1.0.1`, deferred or not; add the line beside the kit's ignore.
- **Conformance runs a real selection.** The `selected` variant composed its handoff with a
  baseline of the same commit, while Quick Skin recomputes a selection as the Git diff from the
  baseline's commit to the tested head, which is empty there. The variant now runs last, on a new
  head one commit after the newest published baseline's commit, adding the new file the fixtures
  module names as `SELECTED_CHANGE` (new, optional; default
  `docs/mod-base-conformance/selected.md`). `selected_extensions` gets the new seeder
  `ctx.api.retained_baseline(key)`, the retained baseline of any declared key at that commit (a
  stand-in retained by a successful Pages run for a key outside `--keys`), for a certificate that
  names every release's baseline, as Quick Skin's does; simulated jobs name their run's `head_sha`
  and `head_branch`, as GitHub's do. A forged baseline may now be refused by the adapter's own
  `compose` hook as well as by R3; any other hook's failure, or accepting it, still fails. Both
  forgeries are uploaded at the baseline's commit, so each differs from the genuine baseline only
  in its owner's workflow or upload window.
- **Conformance delegated claim.** The `delegated` variant's tested claim took its branch and
  commit from the handoff run while naming the tested run. It now names the tested run's own
  branch (`conformance/reused-pull-request`) and commit. **Action:** a `delegated_extensions`
  fixture whose reuse record copied the handoff's branch (Quick Skin's `tested-source` seal) must
  record the tested run's `head_branch` and `head_sha`.

## v1.0.0

The first stable release: the same code as `v0.9.3`, released after the canary proved every
behaviour that cannot be verified locally (G1–G7) in one green cycle at `v0.9.3` from a separate
repository (`docs/OPERATIONS.md#canary-evidence`). Schema versions (all 1),
`pixel_metrics_version` 1, `ADAPTER_API` 1 and every managed file are unchanged from `v0.9.3`; mods
pin it with `bump --to v1.0.0`.

## v0.9.3

A fix release for a false rejection the canary found at `v0.9.2`. Schema versions,
`pixel_metrics_version` 1, `ADAPTER_API` 1 and every managed file are unchanged, so mods move their
pin with `bump --to v0.9.3` and nothing else.

- `build` and `refresh` identify their own Pages run as the unfinished, conclusionless `pages.yml`
  run of the protected head, whatever non-terminal status GitHub reports (`requested`, `queued`,
  `pending`, `waiting` or `in_progress`). GitHub reports a running workflow run as queued or
  waiting while some of its matrix jobs wait for a runner, and three sibling refresh jobs of canary
  run 36210848548 failed closed on that. A finished run is still refused, and the refusal now names
  the observed status and conclusion.

## v0.9.2

A fix release for the defects the Quick Skin and Block Pops migrations found at `v0.9.0`/`v0.9.1`,
before either adopts the kit. Schema versions (all 1; only optional fields are added),
`pixel_metrics_version` 1 and `ADAPTER_API` 1 are unchanged. Two managed files change, so mods move
their pin with `bump --to v0.9.2`, which re-synchronizes them: `.gitattributes` and
`scripts/ci/mod_base_kit.py`.

### Fixed

- Partially re-captured lanes compose. R3 refused a composed lane whose frames mix epochs
  (`lane <id> mixes baseline and selected frames`), while a Quick Skin selective generation
  re-captures single checkpoints (hud-preview: 2 of the 63 `full` captures), so no real selective
  generation could be published. Composition is now per frame, as Quick Skin's schema-7 view was:
  every frame keeps its `epoch` and `tested` run (with its epoch's lane JAR); a re-tested lane keeps
  the selected execution as its record and, when it still holds baseline frames, records the
  baseline execution as the new optional composed-only `lanes[].baseline_run`; a comparison never
  spans the two epochs. `validate_compact` checks the epoch consistency of every composed bundle,
  R3 checks it against the sources (a re-tested lane is exactly the selected lane record, so a
  selection must run every role of a lane it re-tests, as Quick Skin's do), `--bind-raw` binds the
  raw lanes, and the gallery publishes `baseline_run` on the lane and shows that
  execution's JAR, result and wall time in a baseline frame's validation record. Every earlier
  composed bundle stays valid.
- `template check` on Windows. A clone with `core.autocrlf=true` (Git for Windows' default) checked
  the managed files out with CRLF and failed the byte-exact check. The managed `.gitattributes`
  keeps its `*.bat whitespace=cr-at-eol` rule and now pins `text eol=lf` for every managed and
  fragment path. The check stays byte-exact (committed bytes are what GitHub reads) and reports CRLF
  line endings with the fix (and the diff of the LF form when that still differs) instead of a
  whole-file diff; `template sync --write`, and so `bump`, rewrites a CRLF managed file or caller
  with LF, keeping the caller's extension region. An existing clone applies the rule at its next
  checkout of those files.
- Conformance can run a real adapter's delegated and selected variants. The extension fixtures
  (`delegated_extensions`, `selected_extensions`) now receive a seeding API as `ctx.api`: reads of
  the simulated GitHub, `handoff_run`, and typed, bounded `add_artifact` (ZIP bytes, returning the
  artifact record), `add_run`, `add_jobs` and `add_response`. The Quick Skin-like fixture uses it for
  a sealed runtime reuse, a coverage certificate and a mixed-epoch composition; the report's `site`
  gains `composed_lanes`.
- `conformance` no longer hides the simulation's error behind a cleanup error of its scratch
  directory.
- Block Pops' staged kit includes `actions/`, so its gate can check the pinned composites. They are
  bound by a second lock inside the digested `src/`, `src/mod_base/template/staged_actions.sha256`,
  while `staged_files.sha256` keeps listing exactly `template/` and `tools/`. So a controller
  upgrade stages in both directions: a controller bootstrap older than `v0.9.2` still stages a
  `v0.9.2` candidate (without `actions/`), and a `v0.9.2` bootstrap stages a candidate pinned back to
  an older kit (without `actions/`). A gate step that reads the overlay's `actions/` therefore needs
  a `v0.9.2` or later controller bootstrap.

## v0.9.1

A fix release for a defect the cross-repository canary found at `v0.9.0`. Document kinds and
schema versions, `pixel_metrics_version` 1, `ADAPTER_API` 1 and the managed files are unchanged;
mods move their pin with `bump --to v0.9.1`.

### Fixed

- Consistent GitHub listings. In canary run 36190041285, `Finalize / Refresh evidence cache for
  mc1.20.1` failed closed on `listing total_count 6 disagrees with 5 listed rows` while the sibling
  finalize jobs of the same run uploaded their caches: GitHub's run-artifact listing is eventually
  consistent during concurrent uploads (Quick Skin runs 17 keys and 17 family legs at once). The site
  deployed, but that generation lost its cache refresh and its rotation. A listing snapshot whose
  rows disagree with its `total_count`, whose `total_count` changes between pages or that repeats a
  row is now discarded and read again from page 1: at most `limits.LISTING_READ_ATTEMPTS` (4) reads,
  2, 4 and 8 seconds apart (`LISTING_RETRY_DELAY_SECONDS` doubling up to
  `MAX_LISTING_RETRY_DELAY_SECONDS`), every read counted against the job's request budget. Only the
  last inconsistent read fails closed, as before; an incomplete listing is never used. The rule
  covers every paginated listing (artifacts, jobs), the workflow-run listings and `admit`'s active
  source-run inventory.
- Only a short page ends a listing. A full page that reaches `total_count` is now confirmed by the
  next page (one more read, only when the rows are an exact multiple of 100), so a `total_count`
  lagging behind the rows can no longer hide the rows past it; `admit`'s one-page active source-run
  inventory reads page 2 after a full page. A `workflow_runs` read truncated to its newest
  `max_items` runs (or to the 1,000 newest GitHub lists for a filtered search,
  `limits.MAX_FILTERED_RUNS_LISTED`) must list exactly that many: a short page before them used to
  return the truncated listing as complete, and is now an inconsistent snapshot read again.
- `refresh` no longer lists its whole Pages run: the promotion and its collected artifact are each
  one exact-name listing of the run (`/actions/runs/{id}/artifacts?name=`), so the caches its sibling
  jobs upload concurrently never enter its inventory, and each name must still be held exactly
  once. `select` reads a source run's handoff the same way. `build` (which lists its own run before
  it uploads anything), `admit`, rotation and the family walk keep one inventory per settled run.

### Conformance

- Every simulated refresh job after a sibling's upload is served one inconsistent artifact listing
  and must read it again, exactly once, within its budget; the report gains
  `site.listing_rereads`. `FakeGitHub` gains the seams `skew_listing` and `during_listing`.

## v0.9.0

The first release, used by the canary and by the mods' draft adoption pull requests.

### Document kinds (all `schema_version` 1)

- Evidence: `mod-base.evidence.expectation`, `mod-base.evidence.handoff` (raw, 1 day),
  `mod-base.evidence.compact` (public cache, 90 days) and `mod-base.evidence.anchor` (lossless).
- Families: `mod-base.family.envelope` around a mod's native bundle and the generic
  `mod-base.family.paired` projection.
- Publication: `mod-base.selection` (embedded in every collected bundle), `mod-base.promotion`,
  `mod-base.build` (`_site/build.json`), `mod-base.site` (`site-data.json`) and `mod-base.gallery`
  (`e2e/gallery-data.json`).
- Repository: `mod-base.config` (`site/mod-base.json`), `mod-base.template-manifest` and
  `mod-base.kit-stamp`.
- Artifacts use the new `mb-*` namespace (`mb-handoff--`, `mb-anchor--`, `mb-family-handoff--`,
  `mb-collected--`, `mb-collected-family--`, `mb-promotion`, `mb-cache--`, `mb-family-cache--`,
  `mb-baseline--`). No converter reads the mods' earlier `pages-*` or `visual-anchor-v1-*`
  artifacts, and the kit never deletes a non-`mb-` artifact: the first v1 generation of every key
  is produced fresh.
- `runtime_evidence` is mandatory on every frame (1 to 4096 printable characters).
- Family byte budget: `families[].handoff_max_bytes` (a family's native bundle) is at most
  784,269,312 bytes, and `family_validate`'s projection images at most 256 MiB, so a generation plus
  its envelope, projection and selection record always fits one 1 GiB collected family artifact
  (`docs/SCHEMAS.md`, "Collected-family limits and the family byte budget").

### Pixel metrics

- `pixel_metrics_version` 1: the 8-key PixelMetrics shared by Quick Skin and Block Pops (luma
  entropy, meaningful colours, dark and light fractions, file and pixel SHA-256, dimensions), with
  Pillow 12.3.0 pinned by `requirements/pillow.txt` (86 hashes).

### Adapter protocol

- `ADAPTER_API` 1: the hooks `targets`, `expectation`, `collect`, `expected_source_jobs`,
  `authenticate_extensions`, `compose`, `verify_publication`, `family_validate` and
  `anchor_selection`, plus the test-only fixture hook `synthesize` and the optional conformance
  fixture functions `family_bundle`, `delegated_extensions` and `selected_extensions`
  (`FAMILY_OUTCOMES`).
- `conformance` pushes documentation-only commits (`docs/mod-base-conformance/head-<n>.md`) to
  simulate later generations: a family with `carry_forward` must carry its generation across them
  (its `family_validate` impact decision treats that path as documentation). Its private snapshot
  holds the kit's synthetic pinned `source.workflow` and family `producer.workflow` files instead of
  the mod's own.
- A family generation's `envelope.kit` must be the pin of `families[].producer.workflow` in the
  producer's own checkout: `family collect` checks it from the inert object store, and `select` and
  `build` from the API (SPEC §1.8).

### Managed files

- `.github/workflows/pages.yml` (the managed caller region, `pages caller v1`),
  `scripts/ci/mod_base_kit.py` (the bootstrap), `docs/ai/shared/REPOSITORY.md`,
  `docs/ai/shared/PUBLIC-EVIDENCE.md` and `.gitattributes`.
