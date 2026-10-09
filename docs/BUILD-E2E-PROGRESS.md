# Build and packaged E2E: status

Where the implementation of [BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md) stands, by its
independently mergeable migration steps. [BUILD-PROTOCOL.md](BUILD-PROTOCOL.md) describes the
commands and evidence; [ADR 0007](adr/0007-protected-build-and-packaged-runtime.md) records the decisions.

K1–K6 now compose through registered commands and their workflow or operator entries. Nothing
is released or active: planned v1.1.0 remains unreleased, no mod pins it or has an activation
manifest, and no managed Build/E2E caller has run on GitHub. The design awaits owner agreement;
ADR 0007 is Proposed. **K7, Q1–Q10, B1–B7 and GitHub settings remain untouched.**

## Evidence and its scope

- **Ordinary suite:** Python 3.11–3.13; GitHub is `FakeGitHub`. Files, Git and processes are real
  in the integration cases; older isolated tests also replace system/account operations.
- **Hosted account proof:** `ci_linux_worker.py` (128 tests) and `ci_linux_deferred.py` (2 tests)
  run separately in the kit's own CI on each Python version. They exercise the real accounts,
  host fence, root operations, cleanup and deferred-execution deadlines.
- **Pipeline proof:** `ci_linux_pipeline.py` runs workflow-derived command lines through the
  entire synthetic PR Build → packaged → status chain, with real checkouts, hooks, accounts,
  root launches, selection hand-over and generated upload ZIPs. Both status intents succeed;
  corrupt lane ZIP, missing target, newer Build attempt and draft controls reject. GitHub,
  caller orchestration and third-party Actions remain fake. The coordinator's integrated
  Python 3.12 run passed in 241.342 seconds, observing 13 real fence launches and 211 requests;
  required CI 37865793803 failed `build/plan` worker preparation on all three Python versions
  ("tool closure changed after protected admission"); its correction and final hosted proof are pending.
- **Workflow policy:** registry/YAML checks, actionlint, shellcheck and execution of shell bodies.
  The CI `Test` gate requires the ordinary, worker, deferred and pipeline jobs in every Python leg.

The final section of BUILD-PROTOCOL.md lists what only K7's managed-caller canary can establish.
T1 CI 37847494098 at `7c25343` already showed both accounts denied access to existing
Docker/containerd sockets on all three Python versions; `pkexec` was absent, so that path and
actual daemon functionality are not claimed.

## The kit (K1 to K7)

Paths below are relative to `src/mod_base/build_ci/` unless otherwise stated.

| Step | Delivered and checked | Remaining constraints |
| --- | --- | --- |
| K1: protocol, schemas, graphs | Thirteen strict v1 kinds, compatibility ledger, literal caller/callee graph fixtures, native plan/output parity and the complete limits ledger. `mod-base.ci.results` is the lane-descriptor/receipt index. | Owner ratification of the design/ADR and narrower kit bounds: 512 MiB archives for every profile; 512 files/256 MiB for a whole lane; Quick Skin native reports capped at 4 MiB versus its native 16 MiB reader. |
| K2: worker, sealing, second validator | `subject`, `worker-prepare`, `plan`, `worker-stage`, `worker-run`, `worker-seal`, `worker-validate`, `worker-finish` compose real staging, protected-pin overlay, hook execution, source proof, envelopes and receipts. Hosted tests cover account/root and deferred cleanup, including failure paths. | Native adapters remain Q1/B1. Workflows supply no Gradle seed (empty candidate Gradle homes); cache restore/save and lane system packages before fencing remain adoption work. Reserved record/report path collisions fail later at upload preparation, not planning. |
| K3: Build and packaged workflows | `build.yml`, `select-build.yml`, `packaged-e2e.yml`, `gate-status.yml`; `select-build`, `fetch-build`, `assemble`, `aggregate`, `seal-gate`, `gate-status`. P1 runs the full synthetic command chain and negative controls. Complete Build validation records/reports are mandatory; loaded protected config digests are bound. | Managed caller execution, job names/references, real uploads, selection job-output transport and real request allowance still require K7. Hook/job timing needs native adoption measurements. |
| K4: callers, activation, bootstrap | Four registered managed callers, activation checks in `template check/sync/init`, `template activation/transition`, bootstrap bump/rollback checks, canonical-base PR filters and LF checkout rules. | Owner creates the `mod-base-gate` environment and App variable/secret before the status caller is active. Transition admission is an operator command; no workflow runs it. Future candidate kit pins remain unsupported below. |
| K5: batches | `ci batch-prepare`, `ci batch-settle`, strict batch manifests and real-Git rebuild/settlement tests. CLI flags and effects are in the protocol. | No workflow or managed caller starts them. The owner/adopter must supply a reviewed batch procedure, protected allowed-path list and App/automation credentials allowed to push `batch/*` and open/close PRs. |
| K6: post-merge reuse | `ci reuse-admit` is wired into Build planning and protected selection; reuse gates re-admit and seal `ci-reuse.json`. Original gate-pair and Build readers verify retained evidence; policy digests cover the activation manifest and four callers. | No command consumes reuse references yet. Q9/B6 must consume original runtime via the results index and required lane artifacts; unused byte-union runtime readers were removed. Live reuse remains a K7 case. |
| K7: release candidate and hosted canary | Not started. Existing `canary/` and OPERATIONS.md's canary procedure cover Pages; `tests/fixtures/ci_mod/` supplies the synthetic Build adapter. | Owner-controlled immutable release-candidate tag, isolated repository, activation manifest and gate App; all K7 design cases. A failed tag gets no Release and no mod pin. |

## Measured request budget

The workflow-derived ledger includes Build, packaged E2E and one final status evaluation:
synthetic 2 targets/3 lanes/1 extra input: **61 + 92 + 58 = 211**; Quick Skin 17/34/1:
**106 + 247 + 58 = 411**; Block Pops 10/20/0: **82 + 174 + 57 = 313**. It counts kit
traffic through FakeGitHub, including storage GETs, with single-page listings and no waiting,
retries, other generations or earlier status events. Third-party Actions' internal traffic is
outside this measurement. Every pending poll adds one; Quick Skin plus 89 polls is 500 (<600).

A job's `GITHUB_TOKEN` allowance is 1,000 REST requests/hour/repository, shared by runs.
`MAX_CI_GENERATION_REQUESTS = 440` is a test-only no-wait regression budget. The coordinator
retained structural bounds of 256 targets/256 lanes, whose cost is at least 2,287 plus pagination;
those bounds do not promise an executable generation within the allowance.

## Known behavior and explicit scope decisions

Exactly two actual `expectedFailure` tests remain, each with the coordinator's written decision
in BUILD-PROTOCOL.md:

- `test_ci_lifecycle_candidate.KitOverlayTests.test_a_candidate_kit_bump_resolves_the_lifecycle_overlay`:
  the real overlay/Git/bootstrap regression passes for the protected pin but a changed candidate
  pin fails stamp matching, even after release-tag/ancestry admission. Implement separate protected
  admission and staging of the candidate kit **before Q/B adopts that upgrade route**. A successful
  canary cannot supply the missing implementation; current K1–K6 scope uses the same pin.
- `test_workflow_ci_policy.CiConfiguredTimeoutTests.test_no_admitted_hook_timeout_is_as_long_as_the_job_that_runs_it`:
  per-hook admission does not guarantee that a whole job fits its hard deadline. Timeout fails
  closed. Generic fixture hook sums of 70/140/100 minutes versus jobs of 60/120/100 minutes are
  not native adapter measurements; calibration belongs to K7/Q/B.

Subject admission waits up to 15 seconds/four observations for a pending PR test merge. An
outdated base fails with `ci-pr-base-outdated` and asks for a branch update. A default branch
advance invalidates in-flight generations; rerunning the old run keeps its old controller, so a
new PR event is needed. Upload creation times allow two seconds of service/runner clock skew;
other chronology remains exact. Minor consolidation of duplicate job openers and the state-file
catalogue remains; no bound was raised to make a pipeline pass.

## Quick Skin (Q1 to Q10)

None of these steps has started: the Quick Skin repository has not been changed for this pipeline
and keeps its own Build and Packaged E2E workflows. Every step waits for a released kit; the
release is the owner's procedure and needs K7 to pass first. Q1, Q2, Q7, Q8, Q9 and Q10 are
changes in the Quick Skin repository. Four steps need the owner to act on GitHub and cannot be
done in code alone. Q3 ends in a proposal for a dedicated App that only publishes statuses and
for the event policy of `pull_request_target`, which the owner has to authorise. Q4 and Q5 need
that App to be provisioned, with the environment `mod-base-gate`, its variable and its secret in
the repository ([OPERATIONS.md](OPERATIONS.md#the-gate-status-app)). Q6 is a change of the
ruleset: the expected source of the two required contexts moves from GitHub Actions to that App.
The design also asks the owner to accept strict dependency verification (Q2) and the governance
changes that go with Q3 and Q6; without them Quick Skin cannot adopt this model.

## Block Pops (B1 to B7)

None of these steps has started. The design begins them only after the kit and Quick Skin have
validated the pipeline, from a fresh worktree of `master`. All seven are changes in the Block Pops
repository. B1 is a `controller-upgrade/*` change that needs the owner's approval of its exact
head, and B2 and B3 are the two approved activations, shared Build first and shared E2E second.
Block Pops keeps its existing App and its two required contexts, so the design lists no App to
provision and no ruleset to change for it. What it needs from the owner is those approvals, the
environment `mod-base-gate` with the client id and a key of that App before a mode manages the
status caller, and, before B1, a released kit to pin.
