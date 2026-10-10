# Build and packaged E2E: status

Where the implementation of [BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md) stands, by its
independently mergeable migration steps. [BUILD-PROTOCOL.md](BUILD-PROTOCOL.md) describes the
commands and evidence; [ADR 0007](adr/0007-protected-build-and-packaged-runtime.md) records the decisions.

K1–K6 now compose through registered commands and their workflow or operator entries. They are
tagged as v1.1.0, the K7 release candidate; v1.1.1, which restores the managed `.gitattributes`
of v1.0.3 so that Block Pops can adopt it; v1.1.2, which refuses the jobs GitHub carries over
into a rerun of failed jobs only (K7 canary cases C2 and C4); and v1.1.3, whose gate status
decides a gate whose bundle is gone instead of publishing nothing (K7 canary case F1). No mod is active: none has an
activation manifest, and managed Build/E2E callers have run on GitHub only in the K7 canary
repository. The design awaits owner agreement; ADR 0007 is Proposed. **K7 is done: its rounds ran at v1.1.1 and the v1.1.3 fixes passed in a second canary (see its row
below); the v1.1.3 Release is published. When K7 closed, Quick Skin and Block Pops pinned v1.1.1
without an activation manifest, and Q1–Q10, B1–B7 and the GitHub settings of the mods were untouched.**

## Evidence and its scope

- **Ordinary suite:** 3,050 tests on each Python 3.11–3.13 at the v1.1.0 release commit and on
  Python 3.11 and 3.13 at the v1.1.1 release commit, 3,059 on Python 3.11 and 3.13 at the v1.1.2
  release commit, 3,060 on Python 3.11 and 3.13 at the v1.1.3 release commit, with one skip and one documented expected failure. GitHub is `FakeGitHub`;
  integration cases use real files, Git and processes, while older isolated tests also replace
  system/account operations.
- **Hosted account proof:** `ci_linux_worker.py` (132 tests), `ci_linux_deferred.py` (2 tests) and
  `ci_linux_system_profile.py` (1 test, a real `xvfb-mesa` install before the fence) run separately in the kit's own CI on each Python version. They exercise the real accounts,
  host fence, root operations, cleanup and deferred-execution deadlines.
- **Pipeline proof:** `ci_linux_pipeline.py` runs workflow-derived command lines through the
  entire synthetic PR Build → packaged → status chain, with real checkouts, hooks, accounts,
  root launches, selection hand-over and generated upload ZIPs. Both status intents succeed;
  corrupt lane ZIP, missing target, newer Build attempt and draft controls reject. A second
  generation uses a released future kit with different bytes, proves candidate resolution and
  refuses privileged imports of it: **198/207 requests and 26 real fence launches** per Python leg
  (212/221 before the observations were reused; see Measured request budget).
  The release commit is merged only once its CI passes every ordinary and hosted module on
  Python 3.11–3.13.
  GitHub, caller orchestration and third-party Actions remain simulated. Latest-head acceptance
  requires every required job under
  [PR8 checks](https://github.com/The-Plum-Team/mod-base/pull/8/checks) to pass.
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
| K2: worker, sealing, second validator | `subject`, `worker-prepare`, `plan`, `worker-stage`, `worker-run`, `worker-seal`, `worker-validate`, `worker-finish` compose real staging, admitted candidate-kit overlays, hook execution, source proof, envelopes and receipts. Hosted tests cover account/root and deferred cleanup, including failure paths. | Native adapters remain Q1/B1. Workflows supply no Gradle seed (empty candidate Gradle homes); cache restore/save remain adoption work; lane system packages come from the closed `runtime.system_profile` (`xvfb-mesa`) installed before the fence. Reserved record/report path collisions fail later at upload preparation, not planning. |
| K3: Build and packaged workflows | `build.yml`, `select-build.yml`, `packaged-e2e.yml`, `gate-status.yml`; `select-build`, `fetch-build`, `assemble`, `aggregate`, `seal-gate`, `gate-status`. P1 runs the full synthetic command chain and negative controls. Complete Build validation records/reports are mandatory; loaded protected config digests are bound. | Managed caller execution, job names/references, real uploads, selection job-output transport and real request allowance still require K7. Hook/job timing needs native adoption measurements. |
| K4: callers, activation, bootstrap | Four registered managed callers, activation checks in `template check/sync/init`, `template activation/transition`, bootstrap bump/rollback checks, candidate release admission bound by the plan hash, canonical-base PR filters (the managed `.gitattributes` keeps its v1.0.3 bytes, see v1.1.1). | Owner creates the `mod-base-gate` environment and App variable/secret before the status caller is active. Transition admission is an operator command; no workflow runs it. The executing release must understand the candidate kit's digest and both lock formats. |
| K5: batches | `ci batch-prepare`, `ci batch-settle`, strict batch manifests and real-Git rebuild/settlement tests. CLI flags and effects are in the protocol. | No workflow or managed caller starts them. The owner/adopter must supply a reviewed batch procedure, protected allowed-path list and App/automation credentials allowed to push `batch/*` and open/close PRs. |
| K6: post-merge reuse | `ci reuse-admit` is wired into Build planning and protected selection; reuse gates re-admit and seal `ci-reuse.json`. Original gate-pair and Build readers verify retained evidence; policy digests cover the activation manifest and four callers. | No command consumes reuse references yet. Q9/B6 must consume original runtime via the results index and required lane artifacts; unused byte-union runtime readers were removed. Live reuse remains a K7 case. |
| K7: release candidate and hosted canary | The rounds ran in The-Plum-Team/mod-base-canary on 2026-10-10, from preflight to closeout (evidence and findings: OPERATIONS.md, "Build/E2E canary (K7)"). Passed at v1.1.1: preflight, the bump and the activation rounds with the first push and pull-request generations (P0, S4, MA), concurrent generations and controller versus candidate (R1a), the draft lifecycle and kill safety (R1b), cancellation (C1), the reruns of all jobs (C3, attempt 3 of C4), a head push (H), merges, reuse and default-branch moves (R2), the ruleset experiment (R3) and Pages G1–G7 (R4). Failed: P0 at v1.1.0, on the `.git/config.worktree` that v1.1.1 admits; the reruns of failed jobs only of C2 and C4, which went green on jobs GitHub carried over from attempt 1, which v1.1.2 refuses; and bundle disappearance (F1), whose gate status evaluation stopped on the 404 of the deleted bundle and published nothing, which v1.1.3 decides as `failure`. The closeout verdict is to fix forward: v1.1.0 got no Release, and the v1.1.1 Release (published that morning by owner decision so that the mods could pin it inactive) names the open rerun defect. `tests/fixtures/ci_mod/` supplies the synthetic Build adapter. | Done. C2, C4 and F1 passed again on a v1.1.3 pin in The-Plum-Team/mod-base-canary-2 (PRs #2, #3): the carried-over jobs were refused, the reruns of all jobs accepted, and both F1 statuses became `failure`; F4 showed that v1.1.3 also turns a published `success` into `failure` once the bundle is gone. The v1.1.3 Release is published; v1.1.2 has none. Open: restricting the gate App installation (now all repositories); the old/new record reader (v1.1.3 reading the v1.1.1 generations retained in mod-base-canary). |

## Measured request budget

The workflow-derived ledger includes Build, packaged E2E and one final status evaluation:
synthetic 2 targets/3 lanes/1 extra input: **59 + 90 + 49 = 198**; Quick Skin 17/34/1:
**104 + 245 + 49 = 398**; Block Pops 10/20/0: **80 + 172 + 48 = 300**. It counts kit
traffic through FakeGitHub, including storage GETs, with single-page listings and no waiting,
retries, other generations or earlier status events. Third-party Actions' internal traffic is
outside this measurement. Every pending poll adds one; Quick Skin plus 89 polls is 487 (<600).

Changing the candidate pin adds three release-admission requests in each run's first plan:
**207 synthetic, 407 Quick Skin, 309 Block Pops** for the pinned annotated-tag case. Workers
verify the protected plan hash locally; they do not repeat admission. Equal pins add nothing.

These totals are 14 lower than v1.1.3's (212, 412, 314; 221, 421, 323). The kit gap QS-G7 of
the Quick Skin mapping found states read twice in a row inside one protected command, and they
are now read once to start watching them (BUILD-PROTOCOL.md, Request budget): `ci assemble`
13 instead of 16, `ci select-build` 16 instead of 17, the packaged `ci seal-gate` 22 instead of
23 and the final `ci gate-status` 36 instead of 45. Every recheck before an effect is unchanged.

GitHub documents a job's `GITHUB_TOKEN` allowance as 1,000 REST requests/hour/repository, shared
by runs; K7 measured 5,000/hour in this organization, with each job's token reporting its own
window, and 142–233 requests per pull-request generation of the synthetic mod (OPERATIONS.md,
"Build/E2E canary (K7)"). The ledger keeps the documented 1,000 as its bound.
`MAX_CI_GENERATION_REQUESTS = 440` is a test-only no-wait regression budget. The coordinator
retained structural bounds of 256 targets/256 lanes, whose cost is at least 2,274 plus pagination;
those bounds do not promise an executable generation within the allowance.

## Cold host-fence cost

Before optimization, CI 37990116589 measured nine fresh GitHub runners at **127.3–386.2 s**,
median **185.6 s**. After it, CI 37997489264 and 38004405307 measured eighteen fresh runners at
**41.9–144.8 s**, median **102.2 s**: well inside the 600-second phase limits, but still one to
two and a half minutes per Build or E2E job, mostly the repair walk of `/opt`, `/usr/share` and
`/usr/local` and a 30–40-second verification walk.

The fence closes four unused SDK roots (Android, CodeQL, .NET and Swift), proves them inaccessible
to new accounts and skips their contents only when mount admission permits. All other repair
coverage remains; ambiguous mount aliases retain the full SDK walk. Hosted tests exercise ACL,
hard-link and bind-mount bypasses with real accounts. Python/JDK, Node, Git and runner paths stay
available. Repair and verification keep their separate 600-second limits; no timeout was raised.
Image layout or future tool requirements can change this cost and must be checked at adoption.

## Known behavior and explicit scope decisions

Exactly one actual `expectedFailure` remains, with the written decision in BUILD-PROTOCOL.md:

- `test_workflow_ci_policy.CiConfiguredTimeoutTests.test_no_admitted_hook_timeout_is_as_long_as_the_job_that_runs_it`:
  per-hook admission does not guarantee that a whole job fits its hard deadline. Timeout fails
  closed. Generic fixture hook sums of 70/140/100 minutes versus jobs of 60/120/100 minutes are
  not native adapter measurements; calibration belongs to K7/Q/B.

Candidate kit upgrades and compatible released rollbacks use the same admission checks; the
old executing kit retains all protected authority. The optional v1 plan field `candidate_kit` requires this capability in the
protected release first; unsupported future digest/lock formats require a compatibility-first
release. Stage/root record formats and the job graph stay unchanged; conditional checkout steps
are new. Tag-peel, main-ancestry, digest and lock-format controls reject invalid future kits.

Subject admission waits up to 15 seconds/four observations for a pending PR test merge. An
outdated base fails with `ci-pr-base-outdated` and asks for a branch update. A default branch
advance invalidates in-flight generations; rerunning the old run keeps its old controller, so a
new PR event is needed. Upload creation times allow two seconds of service/runner clock skew;
other chronology remains exact. Minor consolidation of duplicate job openers and the state-file
catalogue remains; no protocol or artifact bound was raised to make a pipeline pass.

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
