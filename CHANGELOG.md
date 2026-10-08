# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

## Unreleased (planned v1.1.0)

- Define the Build adapter contract in code (`build_ci.adapter`: eight hooks, argv, environment,
  file names, strict parsers of `plan.json` and `runtime.json`) and for mod authors
  (`docs/BUILD-ADAPTER.md`). The unreleased `mod-base.build.config` gains the required fields
  `inventory.path`, `scenario_contract.path`, `bundle.path`, `contexts.build` and
  `contexts.packaged`. Add the `ci` command with `ci subject`, which authenticates the tested
  subject and writes the private identity record, pure plan construction with the policy digest,
  and a synthetic mod (`tests/fixtures/ci_mod`) that implements every hook. Planned output paths
  accept the mods' real file names (single inner spaces and `+`).
- The unreleased Build kinds describe what Block Pops and Quick Skin really stage.
  `mod-base.build.plan`: an output's `lane_id` may be `null` for a file of the target as a whole
  (its staged manifest, a report, a log, one SBOM for all its lanes). Every lane has exactly one
  production and one harness output, an SBOM is optional (at most one per lane and one per
  target) and every target has a native report. Paths stay unique in the whole plan, so an adapter
  stages a file every target writes below `targets/<target id>/`. The plan gains `plan_inputs`
  (`[{name, sha256}]`). `mod-base.build.config` gains the required `plan_inputs`
  (`[{name, path}]`, 0..8): more candidate files a plan is derived from, staged for the protected
  hooks next to the inventory and the scenario contract (Quick Skin lists `gradle.properties`,
  which holds the version in its JAR names). `mod-base.build.envelope`: `lane_id` is nullable in
  the same way, a file path is an export path and an `sbom` file is at most 16 MiB.
  `mod-base.ci.runtime-envelope`: a file path is an export path. Sealed Build and runtime
  archives keep the mods' own file names when they are encoded and extracted; Pages archives are
  unchanged.
- Managed files: the bootstrap `scripts/ci/mod_base_kit.py` changes. `bump` now refuses, before it
  edits anything, a kit that does not read the mod's `site/mod-base-build-activation.json` while a
  mode other than `disabled` is active there (a rollback to v1.0.3 or older first returns the mod
  to `disabled`), and it restores every workflow and action file when its write phase fails. A mod
  without that manifest bumps exactly as before; every released bootstrap still stages and bumps
  to this kit.
- Managed files, by activation only: four caller workflows (`.github/workflows/mod-base-guard.yml`,
  `mod-base-build.yml`, `mod-base-packaged-e2e.yml`, `mod-base-gate-status.yml`) become managed
  files of a mod whose activation mode lists them. They are not template-manifest entries and no
  mod without an activation manifest receives or is asked for one. Their templates in this change
  are provisional stand-ins; the managed `.gitattributes` has no `eol=lf` rule for them yet.
- Add the first Build/E2E callee workflow, `.github/workflows/build.yml`: jobs `plan`, `policy`,
  `target` (one per planned target), `assemble` and `gate`, inputs `kit-sha` and `pr-number`. It
  has its own registry (`workflow.CI_CALLEE_WORKFLOWS` with the job tables `CI_JOB_VERBS`,
  `CI_JOB_ARTIFACTS` and `CI_JOB_PERMISSIONS`), its own Build controller prologue, which admits
  only the managed Build and packaged E2E callers of the canonical branch, and its own policy
  tests. The Pages callees, their registry and their prologue are unchanged.
  `tools/update_tree_digest.py` maintains its `MB_KIT_TREE_DIGEST` literal with the other three.
- `mod-base.ci.activation` v1 (still unreleased) gains the required `rollback_from` field, which
  names the mode a `reviewed-rollback` leaves and is `null` otherwise. `template check|sync|init`
  accept every mode, manage exactly the callers of the mod's mode, report a caller outside its mode
  as `forbidden`, and fail on a Build configuration without a manifest. While a mode manages
  callers, `.github/dependabot.yml` must also ignore the third-party actions they pin.
- New commands (additive): `template activation --repo DIR` prints the validated activation state,
  its managed callers and the allowed next states; `template transition --repo DIR --base DIR`
  admits a change of activation against a checkout of the protected base (allowed transition,
  unchanged pin, candidate callers equal to the rendered templates).

- Add inactive historical PR Build selection/revalidation preserving original controller,
  newest run/attempt, complete graph and immutable bundle metadata after actual merged-source
  admission. Retain original plan bytes in live/historical selection and descriptor bytes during
  historical revalidation; both gates/native/policy/consumer admission remain mandatory.

- Make template caller/extension test fixtures use explicit UTF-8 and authored LF bytes across
  platforms. Keep malformed UTF-8, CRLF, hostile extensions, managed drift and POSIX ownership
  assertions intact so locale conversion cannot prevent the intended checks from executing.

- Preserve exact binary file bytes on Windows in the shared bounded reader. Explicit binary
  descriptors prevent CRLF translation and 0x1A truncation without changing identity, size,
  type or symlink checks; JSON readers retain the original input bytes too.

- Correct inactive initial reuse-v1 admission for a final merge whose SHA equals the original
  synthetic tested commit. Keep separate original PR/current subject and producer bindings,
  direct coherent source seals and all independent K6 admission requirements.

- Bind inactive tool-fenced native lane verification to retained frozen plan, exact complete
  owning Build and runtime bytes, with pre/post metadata/inventory/directory checks and original
  caller drift rejection. Execution/context data confer no receipt or native success authority;
  Add Root-only receipt binding to genuinely retained successful execution and that exact
  three-input context, using the existing independent validation export freeze with pre/post
  byte/identity/caller checks. Add Root-only runtime read grants preserving empty regular data
  through original bounds/ACL/root-last transfer and complete three-input closing checks.
  Genuine candidate reclamation/privilege handoff and real hosted lifecycle remain required.

- Add inactive Root-only runtime export reclamation through independent copying with original
  execution, full tracked-source and exact selected owning Build/plan/lane bindings. Retain
  admission inside publication and after private ownership transfer, including named-root checks.
  Preserve empty data through bounded private transfer; native second-account verification,
  genuine provenance and actual hosted/workflow/upload admission remain required.

- Add inactive runtime runner-to-Root execution handoff through existing closed execution-v1
  data and fixed private publication. Retain exact nonce/source/attempt/three-input context and
  recheck original private record, inputs and caller snapshots around receipt freezing. Shared
  writer also rereads staged bytes after closing admission. Fixed runtime Root process/request
  enrollment, genuine provenance and real hosted/native/workflow authority remain required.

- Add required hosted Linux runtime candidate-copy/read-grant cases with actual source/private
  ownership, empty data, independent inodes, validator-only reads and hardlink rejection before
  copying. Existing hosted prerequisites are unchanged.

- Add inactive closed mod-base.ci.runtime-envelope v1 inventory data with original Build/plan
  bindings, ordered lane contracts and role/path validation. Preserve per-lane limits inside
  the complete aggregate and original BP aggregate bounds; previous readers reject the new kind.
  Bind the exact original runtime/Build selections and verify complete frozen file bytes,
  preserving empty logs. Copy independently through private atomic publication with source/stage
  rechecks. Native output mapping, API runtime transport and activation remain required.

- Add batches of pull requests (K5), inactive in mods until a workflow calls them: `ci
  batch-prepare` squashes up to 50 open same-repository pull requests, in the given order, onto
  the default branch head, pushes the stack as a new `batch/<name>` branch and opens one ready
  pull request; `ci batch-settle` closes the members after that pull request merged. The squash
  commits are written with `git commit-tree` in a private store that no ambient Git
  configuration reaches, under one fixed bot identity and the base commit's time, so their ids
  follow from the base and the member heads, titles, numbers and order alone. New closed
  `mod-base.ci.batch` v1 data (the predecessor rejects it) travels as one marker line in the
  batch pull request's body and is only a hint: a batch is verified by building its stack again
  and comparing ids. Conflicts, members that add nothing, forks, moved heads or bases, submodule
  entries and paths outside the caller's protected allowed-path list stop construction; the push
  can only create its branch. Settlement first proves the merged commit, the rebuilt stack and
  both original gates, then closes only members still at their batched head. Both commands use
  the invocation's token: a pull request opened with a workflow's default `GITHUB_TOKEN` starts
  no workflows, so supply an App or automation token. Git 2.40 or later is required. The GitHub
  client gains `patch_json`.
  Add separate historical merged PR observations retaining exact original synthetic parents,
  equal complete final tree and original/current protected history across merge/squash/rebase.
  Live PR admission stays unchanged; full historical gates, reuse and settlement remain open.
  Read original full tested seals after merge with retained historical identity and unchanged
  run/attempt/kit/graph/upload/artifact/canonical record/owning Build checks. Complete coherent
  native sources and reuse/settlement integration remain separately required.
  Bind both original full seals as one pair, requiring packaged's whole owning Build descriptor
  to equal the Build seal's actual bundle, with repeated records/source and caller snapshots.
  Independently valid mixed generations reject; native payload and reuse admission stay required.
  Materialize only that pair's exact original complete Build bytes, retaining original identities
  and shared ZIP/envelope/inventory checks. Recheck both seals/source/caller inside atomic copy
  before publication and reverify staged bytes; runtime/native/reuse authority remains separate.

- Managed bootstrap bumps now plan template synchronization using the verified target kit before
  rewriting pins. Ordinary template drift is accepted; planning rejection leaves pins unchanged.
  Write failures after planning still reject and are not transactional rollback.

- Add inactive mod-base.ci.activation v1 profile data with five closed modes and an 8 KiB reader
  cap; new-kind compatibility explicitly rejects the predecessor reader. It introduces no
  consumer activation, owner approval, arbitrary execution selectors or optional Pages fields.

The protected Build/packaged E2E foundation is under implementation and is not enabled in any
consumer. This is not a published release; the existing runtime version remains v1.0.3 until
the release change. Pages formats, `ADAPTER_API` and `pixel_metrics_version` remain unchanged.

- Bind selected worker Python/JDK destinations to explicitly enrolled tool roots before
  dispatch, rejecting external aliases and unusable executable/home types without launch.
  Protected installer provenance and complete import enrollment remain required.
- Run root work in one process model. `tools/ci_privileged_bootstrap.py`, started by the runner
  as `sudo -n -- <python> -I -B -S <kit>/tools/ci_privileged_bootstrap.py --operation <name>
  --kit <kit> --kit-digest <digest> --nonce <nonce>`, re-computes kit-digest-v1 of the
  prologue-verified kit checkout before importing from it and dispatches a closed set of
  operations. Parent and child exchange data only through the local-only
  `mod-base.ci.root-request` v1 kind: one private canonical request per operation with a closed
  argument object, read by root with the private record reader after it has derived the runner
  from the host. Root never calls the GitHub API.
- Remove the private interpreter and kit-copy tower, which no design requirement asked for and
  which could not work on a hosted image: the root-owned kit copy with its record and installed
  guard, the privileged launcher, the Python archive download, inspection and installation, the
  byte digest of tool trees with its byte-fenced worker and validator routes, the release-asset
  download of the GitHub client, `requirements/python-ubuntu24-x64.sha256`,
  `docs/PYTHON-INSTALLER.md` and the unreleased `mod-base.ci.kit-installation` and
  `mod-base.ci.runtime-root-request` kinds. Runtime lane verification now uses the same
  tool-fenced second-account route as Build verification.
- Fence the hosted image before any worker account exists. A hosted `ubuntu-24.04` image ships
  `/opt` with the tool cache, `/usr/share`, `/usr/local` (the head of sudo's PATH) and the JDKs
  world-writable, the `/opt` trees with default ACLs. The new root operation `host-fence`
  removes group/other write permission and default ACLs from those trees and `/var/lib/gems`,
  then fails unless no world-writable non-sticky directory and no world-writable regular file
  remains reachable on the root filesystem outside the worker boundary. Tool roots are admitted
  by ownership and mode wherever they live (`TOOL_INSTALL_PREFIXES` and `TOOL_LINK_PREFIXES` are
  gone), and a directory that carries a default ACL is never part of an admitted tool tree.
- Stage the candidate's checkout through the new root operation `stage-candidate`. The staging
  modules could never run: they asked for a `worker` role and a `worker-home` that do not exist
  (the roles are `candidate` and `validator`), left `repository/out` to root so a build could not
  write beside the kit overlay, and read the tested tree and the pin's release through the GitHub
  API as root. The runner now passes the tested commit, tree and complete inventory in its private
  request (`root_request.request_candidate_staging`); root recomputes the tree's Git name from the
  inventory, requires the checkout to hold exactly those bytes with a HEAD detached at the tested
  commit, and publishes tracked source, a curated `.git`, the kit overlay and an optional Gradle
  seed for the candidate alone. `out` belongs to the candidate, the seed is optional, and a
  populated root is never reused.
- Add a local-only execution handoff v1 kind and private runner-to-root Build result data
  channel, with strict binary-log/context/nonce binding and independent receipt freeze.
  Genuine execution/native validity and enrolled root-program/import provenance remain required.
- Add inactive pre-plan PR generation/readiness reads bound to the protected executing
  controller, with independent PR/default rechecks. Draft observations and unavailable merges
  remain ineligible execution evidence; deferred workflow/status integration stays pending.
  Complete ready-PR merge authentication now brackets Git object reads with the same retained
  generation and default/controller checks, rejecting readiness/source drift before returning.
- Preserve Block Pops' original 8 MiB compiler-report payload limit in the inactive Build
  envelope, separately from 4 MiB validator outputs and Quick Skin's initial transport cap.
  Whole-export/JAR/log/archive limits are unchanged; native conformance remains required.
- Add inactive root-side Build execution/receipt binding: require the retained successful exact
  input digest and producer attempt, derive hook/unit from the canonical envelope, and inspect
  read-only input bytes/ownership/inodes before and after independent receipt freeze. Native
  domain validity, authenticated privilege bridging and final workflow/API authority stay pending.
- Add inactive local sealed-export ZIP encoding with streamed no-follow reads, fixed stored
  entries, the original compressed admission cap including ZIP metadata, strict independent
  extraction/byte verification and atomic private publication. Add bounded child streaming;
  existing whole-file APIs remain unchanged. This is not a nested GitHub artifact format or
  proof of actual service ZIP metadata; upload/native/workflow/Linux gates remain pending.

- Add strict `mod-base.build.plan` v1, separate Build adapter API 1, full-execution graph
  contracts and inert live PR identity checks. These are validation primitives, not a working
  Build runner or authority to publish successful statuses.
- Add inactive complete Build numeric-ID transport with latest-attempt/full-graph/upload-window
  admission, immutable metadata and ZIP digest checks, canonical inventory verification and
  independent private publication. Add a fixed CI ZIP extraction entry point without widening
  Pages limits. Newest-run selection, native validity, running target fan-in and workflow
  integration remain incomplete; required real Linux transport fixtures remain unexecuted.
- Add inactive same-run/attempt target partition transport for fan-in during Build execution.
  Require protected target enrollment, a closed partial graph and successful seal/upload job;
  retain full-graph success requirements for complete bundles and forbid target run mixing.
  Whole-union/native policy integration and Linux execution evidence remain pending.
- Add inactive complete ordered target-input preparation with one private atomic publication,
  successful protected plan/policy, shared source/producer/job authentication and complete-union
  checks. Enforce original logical entry limits across partitions and additional 4 GiB total
  compressed-download/derived physical-input bounds. Native/runtime fan-in limits remain
  unchanged; native aggregate/bundle integration and real Linux results are still pending.
- Finalize the new inactive Build envelope schema 1 before first release: keep pre-upload
  producer identity in the envelope and actual API upload window/immutable transport metadata
  in the selected descriptor. Strict binding checks every producer field and artifact scope.
  Reject the unreleased draft self-reported window shape; no released schema or Pages format
  changes, and the predecessor still rejects this new kind. Gate/reuse timing audit is pending.
- Finalize the new inactive gate/reuse schema 1 record producers before first release with
  pre-upload identity only. Selected-descriptor binding retains exact writer/kind/unit,
  distinct source IDs and actual source-before-record upload chronology. Reuse verification
  start-time authentication, original graph/tree/source proof and final record transport remain
  required and incomplete. Old local draft future-window shapes reject; released schemas stay unchanged.
- Add inactive full-gate API chronology binding: every prerequisite finishes before protected
  gate validation, actual selected/source upload windows match exact successful steps, and all
  step windows lie within completed jobs. Packaged checks its owning Build's independent full
  graph and sealing. Final transport/native/status authority and historical reuse remain pending.
- Add inactive full tested-record numeric-ID transport with exact protected latest attempt,
  graph/upload/metadata/digest binding, one fixed canonical root JSON file, bracketed source
  metadata/availability and API chronology, and independent packaged owning-Build enrollment.
  Preserve existing record/ZIP limits. Source payload/native/status authority, historical reuse
  and real Linux execution evidence remain pending.
- Add inactive newest exact PR Build selection using an explicit initial v1 protected generation
  marker and status-unfiltered listing. Reject a failed newest producer, preserve pending/absent
  as no bundle, and authenticate the successful attempt/kit/whole graph and immutable bundle.
  Relist/recheck before return; never use older success or compile for a PR. Native payload proof,
  and managed caller/canary wiring remain pending.
- Add inactive 5400-second monotonic PR Build waiting with bounded observations, clipped sleeps,
  repeated source/newest authentication and rejection of late success. API/corruption/failed-run
  errors never become absence. Add exact-descriptor newest revalidation around consumption;
  payload/native proof, production workflow integration and hosted canary remain required.
- Add inactive latest-PR-Build download composition: bounded waiting, exact newest selection,
  immutable-ID complete byte transport and newest/source revalidation inside independent atomic
  publication. A newer producer found at final admission prevents publication; reinspect staged
  bytes after that admission. Existing descriptor-based transport/copy signatures stay unchanged. Native sealing,
  workflow activation and real Linux evidence remain pending.
- Add inactive complete Build byte assembly from the exact ordered same-attempt target input
  set. Copy declared payloads independently, verify source/stage inventories and whole-union
  bounds, create one canonical current envelope and atomically publish the private export.
  Preserve existing regular-file copy behavior and add a selected-file append helper. Native
  aggregate receipts, ZIP/workflow integration and real Linux results remain pending.
- Add bounded protected Git source inventories and descriptor-based tracked-source comparison,
  preserving executable modes, empty files and literal tracked links. Reject undeclared paths,
  hard links, aliases and changed bytes; generated roots do not exempt tracked leaves.
  Add live-PR-bracketed immutable GitHub tree inventory admission with exact blob sizes and
  directory closure, retaining both source and existing transport caps.
  Extend source/controller reads to exact non-PR subjects in authenticated protected default
  history, retaining distinct historical subject and live controller identities and freshness
  checks. Request/run/nonce and full recovery authorization remain separate and unimplemented.
  Add atomic private tracked-source materialization, with streamed bytes, exclusive output
  publication and independent staged inventory checks; Git metadata and cache staging remain pending.
  Isolated Python dispatchers require `-I -B`: isolated mode ignores bytecode environment settings.
- Add fresh fixed worker-account allocation with account/home reuse rejection, primary-group and
  sudo-policy checks, and verified private home/tmp/cache directory ownership. Host-boundary
  lifecycle and complete worker sealing remain inactive and unfinished.
- Add inactive protected-root candidate Build freeze: terminate the fixed UID, recheck tracked
  source before and after independent export copying, and transfer only the private new copy
  to runner ownership. Reject source drift, unsafe original permissions, failed execution and
  copy identity/content changes. Required real-UID Linux cases remain unexecuted; native
  validation, protected staging and production integration are still pending.
- Add inactive fixed read-only plan/Build input binding for protected aggregate verification.
  Atomically materialize the existing plan kind without new schema fields, grant fixed second-UID
  reads and recheck exact input metadata/bytes/identity before and after execution. Retain actual
  execution plus canonical envelope digest for verifier-output freezing. Cross-run/runtime
  composition, native semantics and actual hosted Linux evidence remain pending.
- Add inactive target-partition verifier composition using the same fixed read-only inputs and
  retained execution/digest. Require exact enrolled target, partition output coverage and producer
  attempt; complete or other-target bundles reject before launch. Align unreleased Build CI unit
  admission with the existing artifact delimiter rule, rejecting `--` in target/lane IDs early.
  Native witnesses, runtime/cross-run composition, workflows and hosted evidence remain pending.
- Add explicit generic policy-runner profiles with native discovery/count parity: strict BP
  defaults, QS start-root imports/whole-class fixture skips, complete worker results and nonzero
  executed-suite checks. Fix repeated fixture setup after teardown for reexported classes;
  retain native method/cleanup skip and expected-failure semantics. Bound discovery/workers and
  per-unit UTF-8 diagnostic retention. The staged tools lock changes; no JSON schema changes.
  Discovery/execution require the credentialless worker and verified kit imports. Full native
  policy parity, protected integration and required real Linux UID cases remain pending.
- Add inert protected-controller Git tree/blob source admission against native approved paths,
  configured SHA-256 and regular Git modes, bracketed by live PR checks. Minimal import copies
  reject changed/undeclared files and Git metadata. Additional code-source caps are 4 MiB per
  file and 64 MiB total; complete import-root provenance and lifecycle integration remain pending.
  Materialize retained protected source bytes into exclusive no-follow regular files in a
  private stage, preserve Git executable modes and independently verify before atomic publication;
  existing outputs are never replaced. Native validator execution and import enrollment remain pending.
- Add fixed protected-controller source read handoff, with authenticated host/accounts/layout,
  candidate termination, exact byte/mode checks before access and normalized byte/blob checks
  afterward. Only the fixed validator group receives reads; files become 0640 and root 0750.
  The additive source permission helper supports empty Python sources and repository paths;
  existing export handoff keeps rejecting empty artifacts. Real Linux access evidence,
  complete import provenance and native execution/sealing remain pending.
- Add closed second-account verifier execution with protected dispatcher/timeout binding,
  exact target/lane selection, source byte/permission checks before and after, existing tool
  fencing and final UID termination. Recheck all source owner/group/modes and absent ACLs;
  hashes alone do not prove read-only code. Execution remains inactive and requires native
  inputs/import provenance/conformance; zero exit/logs never authorize receipts or uploads.
- Add new inactive `mod-base.ci.validation` schema 1, explicitly rejected by the predecessor
  reader. Bind exact protected plan/hook/unit/run/attempt/config/input context and report
  contracts; independently verify canonical strict native JSON and exact byte inventory.
  Add private independent verifier-output copying with staged revalidation and exclusive
  publication. Native closed-schema semantics, UID reclamation/integration and real hosted
  validation remain required; existing gate/envelope/Pages shapes are unchanged.
- Add protected-root fixed verifier output freezing: terminate/lock the actual verifier,
  admit its private 0700/0600 tree, independently copy exact context/reports and transfer only
  the new protected copy to runner-private ownership with final metadata/content/host checks.
  The verifier original is never chowned. Native semantics, input/import provenance, root
  dispatch and final gate/upload integration remain pending; existing read handoffs are unchanged.
- Add the initial hosted-Linux runner-home fence and pre-dispatch identity/mode recheck,
  private-cwd execution and explicit host-descriptor closure. Required Linux probes cover
  inert host files/processes, proc memory/environment, ptrace and inherited descriptors;
  hosted execution and the complete sealing lifecycle remain unverified.
- Add bounded host tool-tree permission/identity closure checks, including link targets and
  ancestors, with Python/JDK path binding and full reinspection before isolated dispatch.
  Mutable or foreign-owned installations reject. Installer provenance, complete import-root
  enrollment, native observations and real hosted validation remain pending.
- Add `mod-base.build.config`, `mod-base.build.envelope`, `mod-base.ci.selection`, `mod-base.ci.gate` and
  `mod-base.ci.reuse` v1 with strict immutable descriptors, complete plan coverage and direct
  original-source reference checks. Separate `mb-ci-*` names remain outside Pages rotation.
  Plans retain all declared native reports and bounded logs rather than requiring one report
  per lane. Add exact target-union and canonical frozen-byte inventory checks. Worker freezing,
  API/domain authentication, workflow integration and reuse execution are still pending.
  Add atomic independent Build export copying with streamed hash comparison and staged
  inventory revalidation; ownership reclamation and second-account native sealing remain pending.
  Add protected Linux-root-only read-group handoff for fresh private copies, removing inherited
  ACLs and opening root traversal last; identity admission and full lifecycle wiring remain pending.
  Bind Build read handoff to the fixed copy and actual runner/validator accounts, with candidate
  termination, copy/envelope/host rechecks and private failure cleanup. Native validation and
  the complete protected lifecycle remain inactive and unfinished.
- Replace universal predecessor readability with an exhaustive per-kind ledger, bidirectional
  unchanged-format checks and explicit unsupported-kind rejection against a digest-bound
  v1.0.3 reader snapshot. Existing kinds remain schema 1.
- Start the inactive disposable-account port with explicit bounded environments, dedicated
  account binding and real/effective UID termination/locking. Add required hosted Linux account
  integration tests to every Python CI leg. The complete worker lifecycle and Linux proof remain
  pending; no consumer begins executing through these primitives.
- Add inactive bounded dispatcher execution, failure/cancellation cleanup and orphan-pipe
  termination. Preserve raw bounded diagnostics and render logs with terminal/modern/legacy
  Actions-command escaping; a visible prefix alone does not neutralize legacy commands.
  Account termination rechecks and kills processes after lock/expiry under the original
  sweep deadline; pre-lock quiescence alone cannot authorize sealing.
- Managed `docs/ai/shared/PUBLIC-EVIDENCE.md` clarifies per-kind schema evolution and strict
  optional-field compatibility. A future kit bump synchronizes it; no consumer is changed here.

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
