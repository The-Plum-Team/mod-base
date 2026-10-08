# 0007. Share protected Build and packaged runtime mechanisms

Date: 2026-10-08

## Status

Proposed. This record describes the implementation proposed in the pull request that carries it.
[BUILD-E2E-DESIGN.md](../BUILD-E2E-DESIGN.md) is still a draft awaiting owner agreement, so
merging this record ratifies nothing by itself: no consumer is activated, no release is cut and no
GitHub setting, App or ruleset changes. The hosted canary of the design's step K7 is still
required before any mod adopts the pipeline.

## Context

Quick Skin and Block Pops each run their own Build and Packaged E2E workflows. Quick Skin compiles
one Minecraft target per runner and hands one bundle to its runtime lanes, but its workflows and
helpers run from the candidate checkout. Block Pops runs from a protected controller and confines
the candidate to a disposable account, but compiles everything twice on one runner behind a global
lock. The design combines Quick Skin's topology with Block Pops' boundary in the kit. Nine audits
of the first implementation found library code that no command or workflow called, and several
wrong assumptions about GitHub; the decisions D1 to D10 below settle them.

## Decision

### Trust model

Pull-request orchestration runs from the protected default branch on `pull_request_target`:
`github.sha` names the controller, never the candidate. Protected code authenticates the pull
request and its synthetic merge, then checks the candidate out as data. Candidate Python, tests,
Gradle and Minecraft run only as the disposable account `modbase_candidate`, with a fixed
environment and no token. Protected code then kills and locks that account, proves tracked sources
unchanged and freezes the export. A second disposable account, `modbase_validator`, runs the mod's
protected verifier over the frozen bytes. Only after that may an upload step use the runner's
credentials. A separate caller-owned job with the mod's own App credentials publishes the required
statuses. An account boundary is not a virtual machine; self-hosted persistent runners are not
supported.

### Identity

Every plan, envelope, selection, gate receipt and reuse reference binds the same tuple, compared in
full and never taken from an artifact name, a caller input or the run API alone:

- repository, source repository and pull-request number (0 for a protected push or dispatch);
- head SHA and branch, base SHA and branch;
- controller SHA, controller workflow path and workflow ref;
- kit repository, SHA, version and tree digest;
- tested SHA, tested tree and ordered tested parents (base, then head, for a pull request);
- policy digest, inventory blob and hash, scenario contract hash, runtime selection hash and graph
  version;
- with it, the plan hash and the profile (`quick-skin` or `block-pops`).

A record that names an artifact adds the producer (run id, attempt, workflow path and ref, API head
SHA, event, graph digest, upload window) and the artifact (numeric id, name, digest, size, creation
and expiry time).

### Document kinds

All are new, strict, canonical JSON at schema version 1. The previous release rejects each as an
unknown kind (`tests/test_schema_evolution.py`).

| Kind | Holds |
| --- | --- |
| `mod-base.build.config` | The mod's protected Build configuration, `scripts/ci/mod-base-build.json`: adapter entry points with their hashed import closure, input and bundle paths, status contexts, timeouts. |
| `mod-base.build.plan` | Targets and lanes derived from the release inventory and scenario contract, with every planned output, under the identity. |
| `mod-base.build.envelope` | The inventory of one sealed Build export, a target partition or the complete bundle: path, size, hash, lane and role of every file. |
| `mod-base.ci.runtime-envelope` | The inventory of one sealed runtime export, a lane or the complete results, with the Build it ran against. |
| `mod-base.ci.validation` | The validator's receipt: the native verification reports of one hook and unit, bound to plan, run and input hash. |
| `mod-base.ci.selection` | The exact Build a packaged run selected: its full descriptor and envelope hash. |
| `mod-base.ci.gate` | The tested record of a Build or packaged gate: its artifacts, owning Build and native receipts. |
| `mod-base.ci.reuse` | A direct reference from a merged commit to both original pull-request gate records. |
| `mod-base.ci.execution` | A private runner-to-root record of one hook execution: bounded log, nonce and context. |
| `mod-base.ci.root-request` | A private request to the root child to seal a Build export. |
| `mod-base.ci.runtime-root-request` | The same for a runtime export. |
| `mod-base.ci.activation` | The profile activation mode of a mod, `site/mod-base-build-activation.json`. |
| `mod-base.ci.batch` | A batch manifest: base, ordered members with their heads, trees and patches, resulting tree. |

`mod-base.ci.kit-installation` existed in the first implementation and is removed by D4.

### Bounds

A bound of the shared pipeline is a mod's own bound, a platform fact or a kit decision, and is never
raised because a pipeline fails. `tests/test_ci_limits.py` pins every one with its source; the
table lists the native ones. Sources are `path:line` at Block Pops `47a890ae` (BP) and Quick Skin
`c0cdc01a` (QS).

| Bound | Value | Native source |
| --- | --- | --- |
| `MAX_CI_EXPORT_FILES`, `MAX_CI_EXPORT_ENTRIES` | 10,000 files, 20,000 entries per sealed tree | BP `scripts/ci/untrusted_runner.py:46-47` |
| `MAX_CI_EXPORT_FILE_BYTES`, `MAX_CI_EXPORT_TREE_BYTES` | 1 GiB per file, 2 GiB per tree | BP `scripts/ci/untrusted_runner.py:48-49` |
| `MAX_CI_LOG_BYTES`, `MAX_CI_ENV_BYTES` | 16 MiB log, 256 KiB environment | BP `scripts/ci/untrusted_runner.py:50`, `:44` |
| `CI_TERMINATION_GRACE_SECONDS`, `CI_TERMINATION_POLL_SECONDS` | 15 s, polled every 0.25 s | BP `scripts/ci/untrusted_runner.py:42`, `:718` |
| `MAX_CI_SOURCE_ENTRIES`, `MAX_CI_SOURCE_FILES`, `MAX_CI_SOURCE_FILE_BYTES`, `MAX_CI_SOURCE_TREE_BYTES` | 250,000 entries, 200,000 files, 2 GiB per file, 20 GiB (Gradle seed and source copies) | BP `scripts/ci/untrusted_runner.py:491-494` |
| `MAX_CI_JAR_BYTES` | 256 MiB per JAR | BP `scripts/release/artifact_manifest.py:41` |
| `MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE` | Block Pops 8 MiB, Quick Skin 4 MiB | BP `scripts/release/build_evidence.py:21`; Quick Skin's value is a kit choice below its native 16 MiB readers |
| `MAX_CI_RUNTIME_FILES`, `MAX_CI_RUNTIME_BYTES` | 512 files, 256 MiB per lane | BP `e2e/packaged_runtime.py:180-181`, QS `e2e/packaged_runtime.py:201-202` (there per evidence profile) |
| `MAX_CI_REPORT_BYTES`, `MAX_CI_PNG_BYTES` | 4 MiB report, 32 MiB screenshot | BP `e2e/packaged_runtime.py:183-184`, QS `:204-205` |
| `MAX_CI_RUNTIME_AGGREGATE_FILES`, `MAX_CI_RUNTIME_AGGREGATE_BYTES` | 4,096 files, 512 MiB per complete runtime export | BP `scripts/ci/e2e_fanin.py:80-81` |
| `MAX_CI_BUNDLE_COMPRESSED_BYTES` | 512 MiB per artifact archive | QS `scripts/ci/staged_build_bundle.py:25`, `scripts/ci/ci_reuse.py:35` |
| `CI_BUILD_WAIT_SECONDS` | 5,400 s wait for the exact Build | QS `scripts/ci/staged_build_bundle.py:96` |
| `MAX_CI_BATCH_MEMBERS` | 50 pull requests per batch | QS `scripts/ci/pr_batch.py:29` |
| `CI_RETENTION_DAYS` | target partitions 1 day; Build bundles, lane results and complete results 7; tested and reuse records 90 | QS `build-matrix.yml:126`, `build-gate.yml:378` and `:391` |

Three native bounds have no kit constant. At most 16 export roots per seal holds by construction,
because a kit job seals exactly one tree. The limits inside a JAR (8,192 entries, 1 GiB expanded,
32 nested archives to depth 4) stay with Block Pops' protected verifier, the code that opens the
JAR. The native 512 MiB fan-in budget is the aggregate bound above.

Two scopes are stricter than the mods and are decided here, for the owner to confirm:

- The 512 MiB archive cap applies to every profile and artifact kind. The design names it for Quick
  Skin; Block Pops' evaluator admits 2 GiB. The kit downloads an archive into memory within the
  Pages artifact cap (1 GiB + 32 MiB), which the design keeps, so 2 GiB could not be carried.
  Measured bundles: Quick Skin 192.8 MiB, Block Pops 118.6 MiB.
- 512 files and 256 MiB count a whole lane, where both mods count each evidence profile (one
  scenario of a lane). Measured largest lane: 141 files and 52.7 MiB (Quick Skin, seven scenarios).

One measurement is an open problem. Block Pops' aggregate of 20 lanes is 764 files and 218 MiB and
fits the fan-in budget. Quick Skin's 34 lanes are about 4,760 files and 1.2 GiB of archives, so its
complete runtime export cannot be the byte union of its lanes. The bound is not raised; what that
export holds for Quick Skin is still to be decided.

### Graphs

Managed caller workflows compose kit callees at one pinned SHA. The jobs API reports a callee job
as `<caller job name> / <callee job name>`.

| Workflow | Caller jobs | Callee jobs |
| --- | --- | --- |
| Build | "Verify pinned mod-base", "Build deferred for draft", "Shared Build" | "Plan protected Build", "Verify protected policy", "Compile target {id}", "Seal complete Build bundle", "Verify complete Build" |
| Packaged E2E | "Verify pinned mod-base", "Packaged E2E deferred for draft", "Select exact Build" (one callee job, "Select exact Build source"), "Shared Build" (only when a non-PR selection found nothing), "Shared Packaged E2E" | "Authenticate exact Build", "Run packaged lane {id}", "Seal complete packaged results", "Verify complete packaged E2E" |
| Status | "Evaluate protected gates", "Publish protected gate statuses" | none |

A sealed job has the step "Validate frozen native exports" followed by "Upload sealed outputs".
Modes are a closed set: `full` (every worker succeeds), `deferred` (a draft: only the guard and the
deferred job succeed) and `reuse` (a push to the default branch with admitted reuse: plan and gate
succeed, workers are skipped). An attest-only dispatch stays on the mods' legacy route.
Activation modes are `disabled`, `shadow`, `shared-build`, `shared-build-and-e2e` and
`reviewed-rollback`.

### Process model

Each workflow step runs one `ci` command as the `runner` user. Work that needs root runs in a child
started with `sudo -n` on `tools/ci_privileged_bootstrap.py`, which is stdlib-only, recomputes the
kit digest of the verified checkout before importing from it and dispatches a closed set of
operations. Parent and child exchange data only through private records below the worker root
`/tmp/mod-base-sandbox-boundary/mod-base-worker`. Root never calls the GitHub API. The mod's
adapter is reached only through eight hooks of `BUILD_ADAPTER_API = 1`, run by a fixed dispatcher
inside an account: `derive_plan`, `derive_runtime`, `verify_target`, `verify_build` and
`verify_runtime` as the validator, `policy`, `build_target` and `run_lane` as the candidate. Mods
write native files only; the kit inventories, hashes and binds them.

### Decisions and their reasons

- **D1. A producer run is found by its pull-request head.** The API reports a
  `pull_request_target` run under the head branch and head SHA of the pull request, not the base
  (seen on real runs of both kinds). The controller is authenticated from `referenced_workflows`.
  There is no run-title contract.
- **D2. An exact graph includes the caller's jobs.** The guard the design requires can only be
  another job of the same run, so a graph of callee jobs alone rejects every real run.
- **D3. One process model.** Privilege is taken in one audited place, and root needs no token.
- **D4. The private interpreter and installation tower is removed.** No design sentence asks for
  it, its trust is circular (the installer is imported from the checkout it would protect), and it
  cannot work on a hosted image.
- **D5. A host fence for hosted images.** On a hosted runner the tool caches, JDKs and
  `/usr/local/bin` are world-writable, so any local account could replace the interpreter. Root
  removes that write access and proves none is left before a worker account exists.
- **D6. Export paths have their own grammar.** Both mods' JARs are named with spaces
  (`files/Quick Skin - Fabric - 1.20.1-3.1.0.jar`); a repository-path grammar refuses all of them.
- **D7. One `PROFILES` constant.** The profile tuple was written ten times.
- **D8. Timestamps are normalised.** The API returns fractional seconds and offsets; readers accept
  them and store whole-second UTC.
- **D9. Build callees have their own registry, prologue and policy tests.** Pages stays unchanged.
  Callee jobs use inline steps only and may check out the protected mod, the kit and the candidate.
- **D10. Only the mod publishes required statuses.** The kit supplies a read-only evaluation; the
  App credentials and the context strings stay in a caller-owned job.

## Consequences

- This amends [ADR 0004](0004-unified-evidence-schema-v1.md): compatibility is decided per kind in
  `tests/fixtures/documents/compatibility.json`. An unchanged kind still passes the previous
  reader; a new kind starts at 1 and is rejected there as unknown. The baseline is always the
  immediate predecessor release, derived from the changelog.
- Pages document shapes, `ADAPTER_API`, the pixel metric version and existing workflow names do not
  change. The planned release is v1.1.0.
- Block Pops' source artifacts are kept seven days instead of one: about 0.5 GiB per pull-request
  generation by today's sizes.
- `tests/fixtures/ci_native` holds the inventories, lane lists, output names, job listings and
  measured sizes of both mods at the reviewed commits, for parity tests.

## Alternatives considered

- Copying Quick Skin's workflows into the kit: fast, but candidate code would hold the tokens that
  Block Pops' boundary withholds.
- Per-profile archive caps with Block Pops at 2 GiB: needs a streaming download the kit does not
  have, for a bundle that is a quarter of the common cap today.
- Raising the fan-in budget until Quick Skin's lanes fit: forbidden by the design; the format of
  the complete export is the thing to decide.
