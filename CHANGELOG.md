# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

## Unreleased

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
  reader refuses it as unknown. Every other kind, `ADAPTER_API`, `BUILD_ADAPTER_API`,
  `pixel_metrics_version`, the managed files and the workflows are unchanged. The compatibility
  baseline moves to the archived v1.3.1 reader (`tests/fixtures/previous_release/v1.3.1`).
- `reuse.download_reuse_reference` is split into the reference read and the original pair read
  (`reuse._read_reference`, `reuse._originals_finished`) so that `ci select-generation` reads two
  references and the pair once; its behaviour and request count are unchanged.

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
