# Feature-selected runtime scope: design note

Proposed 10 October 2026; decided by the owner on 11 October 2026 and implemented on the branch
`kit/runtime-selection` (unreleased). This note describes how a packaged lane runs only the
scenarios a pull request affects (Quick Skin adoption gap QS-G6). The owner took the recommended
option of every decision; where the note gave none, the implementation took the most fail-closed
option. [Decisions taken](#decisions-taken) says which and why. The note authorizes no release:
the feature ships with the next kit release, and a mod enables it in its protected Build config.

## What is asked

Quick Skin's native packaged E2E narrows each lane's `E2E_SCENARIOS` to the scenarios a pull
request affects. A protected feature-policy job checks out the base policy and the full candidate
history. With a read token it downloads a complete baseline certificate and proves its ancestry.
The native fallback is the full profile. The kit's lanes always run every obligation of every
lane: `derive_runtime` reads only `validation-input/`, which has no base tree, no history and no
API ([BUILD-ADAPTER.md](BUILD-ADAPTER.md#how-a-hook-runs)).

[BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md) allows the extension and fixes its semantics. It stays
domain policy, and it may narrow scope only with complete-baseline proofs. Unknown or unavailable
proof chooses full profiles before execution. Malformed authenticated proof or an API failure
stops admission. A selected run passes the ordinary gate only with every required lane and every
selected obligation, and it never serves as full-baseline, release or full-scope proof (design,
"Ownership and shared interfaces" and the failure table). Block Pops gets no selective exception.

The design does not say where the selection is bound, which code reads the API, or which schema
and adapter versions carry it. The first and third change document kinds, the adapter protocol
or the job graph, which are frozen interfaces ([KIT.md](ai/KIT.md#frozen-interfaces)) and need a
version decision. The second moves the trust boundary of the design's ownership table, which only
the owner can do.

## Why the mapping's minimal change cannot ship as written

The mapping proposes that the packaged `input` job run a protected selection script and fold its
digest into `identity.runtime_selection_sha256` (`planning.py:42`). Three producers share that
plan, however, and each derives it independently:

- The Build run plans for itself and seals its bundle and tested record under its `identity` and
  `plan_sha256`. A packaged consumer validates the sealed Build envelope against its own plan and
  requires both to be equal (`records.validate_build_envelope` through `records._plan_binding`,
  `records.py:137-145`). If only the packaged plan carried the selection, every lane would refuse
  its Build when it fetches the bundle, as "does not equal the complete admitted binding". The
  selection record itself would pass, because `ci select-build` writes its descriptor from the
  packaged plan (`selection.describe_artifact`).
- `gate-status.yml` derives one plan ("Derive the protected plan") and verifies both runs' tested
  records against it (`status._generation_plan`). It runs later, on its own triggers.
- Every job of a generation plans again with `--expect-sha256`, and the same inputs must give the
  same document.

A selection that each producer computes again from the API would read mutable state, such as
whether a baseline exists or has expired, at different times. The plan would then differ between
Build, packaged and status. That fails closed, but it would make gates fail at random, so it is not
a safe optimization.

## Decisions taken

**D1 b: a sealed packaged scope record bound outside the plan.** The plan keeps every obligation
of every lane, so the Build, the packaged run and the status evaluation keep deriving one plan and
nothing about the selection reaches a Build envelope. The packaged `input` job runs `ci scope`
after `ci select-build` and seals a new kind, `mod-base.ci.scope`, bound to its plan and to its own
run attempt (`request`, as in a selection record). It reaches every later job as the job output
`scope`, the way the selection record does, and the same receiving step writes it to a file:
empty for a run in full. `fetch-build` keeps it in the lane's state for the lane's protected hooks;
`aggregate` binds the whole record into the results index (optional `scope`); the packaged
`seal-gate` requires the index to hold exactly the record its job was handed and seals its
digest into the tested record (optional `scope_sha256`). A run in full has no record, so its
documents are byte for byte those of a kit without the feature. Why: it is the note's
recommendation, it changes no graph, job name or public context, and the Build stays independent
of scope.

**D2: kit-authenticated inputs and a token-free hook.** The note gave no recommendation; this is
the option that does not move the trust boundary. No mod code holds a token: protected kit code
reads, with the input job's read-only token, (1) every path whose entry differs between the
recursive Git trees of the pull request's base commit (`identity.base_sha`) and of the tested tree
(a truncated listing, or one over `MAX_CI_SCOPE_TREE_ENTRIES`, is unknown and runs full), and (2)
the base commit's full packaged evidence: the newest packaged run of a push of the base commit,
chosen before its result is read, completed successfully as its latest attempt under the
executing kit pin, and its tested record, or, when that push was covered by post-merge reuse, its
reuse reference and the original packaged record it names. The record must test the base commit
under the same repository, caller, kit pin, policy, inventory, scenario contract, runtime selection
and graph version, with the same profile and the same plan (its plan hash is the pull request's
plan under the base commit's identity, the comparison post-merge reuse makes), and must not be
narrowed. The new protected hook
`derive_scope` runs as the validator from the protected adapter copy, with the plan inputs `ci
plan` granted and that data as one line of canonical JSON in `MB_SCOPE_INPUT` (format
`changed-paths-v1`); root hands its one output, `scope.json`, to the runner (root operation
`take-derived-scope`, as for `derive_plan` and `derive_runtime`). `BUILD_ADAPTER_API` stays 1:
`derive_scope` is an optional hook that runs only for a config with `runtime_selection`, so no mod
without it needs a change. Quick Skin's baseline becomes the kit's sealed full evidence of the base
commit instead of its own certificate, which is the consequence of this option the note named.

The request cost is measured and pinned (`tests/test_ci_scope.py`, `MAX_CI_SCOPE_REQUESTS` 32):
none without `runtime_selection`, for a push, a dispatch or a changed kit pin; 11 behind a direct
baseline record (3 for the trees, 5 for the run, its listing and the record, 3 to observe the run
listing, the run and the record again before the effect) and 15 behind a reuse reference. The gate,
the status evaluation and reuse read the scope from documents they already download and add none.
The generation ledger is unchanged without the feature (198 / 398 / 300); with it Quick Skin's
generation is 409, or 413 behind a reference, within the test budget of 440.

**D3: a new kind and optional fields within schema 1.** `mod-base.ci.scope` is a new kind at
schema version 1; the compatibility ledger records it as `new-kind`, moves its baseline to v1.2.1
(whose reader is archived in `tests/fixtures/previous_release/v1.2.1`) and the v1.2.1 reader
rejects it as an unknown kind. `runtime_selection` of `mod-base.build.config`, `scope` of
`mod-base.ci.results` and `scope_sha256` of `mod-base.ci.gate` are optional fields within schema 1,
validated whenever present, as `candidate_kit` and `seeds` were: a v1.2.1 reader refuses a
document that has one. Why: it is what the schema rules allow without a version change, and
nothing a mod does today changes until its protected config names `runtime_selection`; a pin must
not be rolled back below this release while it does.

**D4: what a scope may say.** The kit enforces what the note listed: the record names every
planned lane in plan order, each lane's scenarios are its own obligations in plan order, the
record is bounded (`MAX_CI_SCOPE_BYTES`, 64 KiB) and carries the baseline provenance and the digest
of the changed paths. A lane with nothing selected still runs: the hook names its smoke scenario,
and a lane the hook names no scenario for runs every obligation. No record ever says
"selected-empty". Why: the design requires every required lane; full is the fail-closed reading of
"nothing selected", and the kit cannot pick a smoke scenario because it does not interpret
obligations. A selection that narrows no lane writes no record. Coverage is the mod's own
`verify_runtime`: a lane gives its entry, `{"lane_id", "scenarios", "scope_sha256"}`, to
`derive_runtime` and `verify_runtime` in `MB_RUNTIME_SCOPE` (an environment value of the validator,
not a file of `validation-input/`, so the root-granted input layout does not change), and the
verifier proves that its native results cover exactly those scenarios.

**D5: a narrowed pull request never covers its push.** `reuse.FULL_RUN_REASONS` gains
`original-run-narrowed`: when the pull request's packaged tested record carries `scope_sha256`,
the push runs in full. A reuse reference that names a narrowed original is refused when it is read
back. A narrowed record is never a baseline either: `ci scope` refuses one behind a reference
(`baseline-narrowed`), and a push, a dispatch or a release rehearsal never narrows. Releases keep
their full rehearsals.

### What runs full

`scope.FULL_SCOPE_REASONS` names every reason, and `ci scope` outputs it as `reason`: no
`runtime_selection` in the protected config; a subject that is not a pull request, or a pull
request from another repository; a changed kit
pin (`candidate_kit`); a pull request that changes the protected Build config, an adapter source
(the selection policy is one of them), a candidate file a plan is derived from (inventory, scenario
contract, extra plan inputs) or a control file (`scope.selection_inputs`); changes that the API does
not list completely or that do not fit the hook's input; a base commit without a push run, with
one still running, failed, under another kit pin, whose evidence has expired, differs or is
narrowed; a selection that narrows nothing; a record over its byte cap. A malformed API answer,
an API failure, a failed hook and a malformed selection stop the `input` job instead.

## Decisions for the owner (as proposed)

**D1. Where the selection is bound.**

- *(a) In the plan identity, decided once by the Build run.* The Build's planning job produces the
  selection, and packaged and status jobs receive it from the authenticated Build instead of
  deriving it again. This adds a step or an artifact to `BuildGraphV1`, and packaged planning must
  read the Build before it plans (today it plans first and then selects). That is a graph change,
  so it needs its own graph protocol decision and a compatibility-first consumer upgrade.
- *(b) Outside the plan, sealed by the packaged run (recommended).* The plan keeps every lane's full
  obligations, so Build, packaged and status keep one plan. The packaged `input` job seals a small
  bounded scope record bound to its `plan_sha256` and run attempt. It hands the record over the way
  `ci-selection.json` is handed over today
  ([BUILD-PROTOCOL.md](BUILD-PROTOCOL.md), "Receive the selected Build"). The results index and
  the packaged tested record bind its digest; the gate, status and reuse read the scope from that
  sealed evidence. The Build does not depend on scope, because it compiles every target anyway,
  and a push or dispatch stays full by construction. The cost is a new kind, or new optional
  fields in `mod-base.ci.results` and the tested record (D3).

**D2. What code may read the API for a selection.** The mapping would run a mod-owned
`adapter.files` script as the runner user with a read token. Today no adapter code runs outside
the disposable accounts, none holds a token, and the hook set is closed (`adapter.HOOKS`; design,
"Ownership and shared interfaces"). There are two options:

- *Token-bearing mod producer.* This keeps Quick Skin's own certificate check and ancestry proof,
  but it widens the boundary: protected mod code would run with a token on the fenced runner. It
  does not keep the certificate's source. Quick Skin's protected issuer certifies a complete
  native run and its job graph. Once Q7 replaces the native packaged E2E, no run of that kind
  exists, so the mod must first issue its certificate from the kit's sealed full evidence.
- *Kit-authenticated inputs and a token-free hook.* The kit authenticates generic inputs and gives
  a new validator hook only data. One input is the paths changed between the tested merge's first
  parent (`identity.tested_parents[0]`, the base) and the tested tree. The other is the base
  commit's own sealed full packaged tested record and results index, read with the readers reuse
  already has. The boundary does not change, but Quick Skin's baseline would become the kit's
  evidence of the base commit instead of its own certificate, and that is the mod's decision.

Either option adds a hook, which needs a `BUILD_ADAPTER_API` decision: an optional hook within
version 1, or version 2.

Either option also spends requests that nobody has counted. They come from the 1,000 REST
requests per hour that every run of the repository shares, so the producer needs its own
`max_requests` cap and a test that pins its typical count, like every other command
([BUILD-PROTOCOL.md](BUILD-PROTOCOL.md#request-budget)). Under (b) the gate, status and reuse
should add no requests, because they read the scope from documents they already download. One
Quick Skin generation already counts 412 requests in the ledger, or 501 with 89 pending selection
polls, and the test-only regression budget `MAX_CI_GENERATION_REQUESTS` is 440. Any requests the
producer adds count against both, so they have to be measured and approved before it ships.

**D3. Schema evolution.** The changes are an optional `runtime_selection` entry in
`mod-base.build.config`, the scope record, and its digest in the results index and tested record.
v1.1.3 readers reject unknown keys, and every kind is `unchanged` in
`tests/fixtures/documents/compatibility.json`. The design asks for an approved ledger amendment
and a version decision before such a field ships (design, "Pinning managed callers and schema
evolution", and risk 7). The v1 `candidate_kit` plan field is the precedent: the protected
executing release had to understand the field before any consumer could use it.

**D4. What a scope may say.** The kit can enforce the following:

- the scope names every planned lane;
- each lane's scenarios are a subset of its obligations, in plan order;
- the record is bounded;
- it carries the baseline provenance the producer authenticated.

Two questions are for the owner:

- *An empty selection.* Does a lane with nothing affected still run, as a smoke run, or is it
  recorded as selected-empty? The design requires "every required lane", which suggests that it
  runs.
- *Who checks coverage.* The kit does not interpret obligations, so only the mod's
  `verify_runtime` can prove that its native results cover exactly the selected scenarios. That
  hook would read the scope from `validation-input/`.

**D5. Reuse and release.** Under (b) the plans of a selected pull request and of its merge are
equal, so reuse admission would need a new `FULL_RUN_REASONS` entry: a selected packaged original
never covers a push. Under (a), `runtime-selection-differs` (`reuse.py:108`) already gives a full
run. Releases keep their full rehearsals either way.

## What any choice keeps

- The candidate still receives only a narrowed `E2E_SCENARIOS`, and no token, history or root.
- The fence, `worker-run` and the second validator do not change.
- Unknown or unavailable proof means full scope before any lane starts. A malformed record or an
  API failure stops the `input` job.
- The selection policy is protected code at the executing controller, never the candidate's. A
  candidate's files reach it only as data. A pull request that changes the selection policy, or
  any input the selection reads (the scenario contract, the inventory, a plan input), runs full,
  as Quick Skin's native consumer already does for changed policy. Otherwise a candidate could
  narrow its own scope by editing what the selection reads.
- Forks, dispatches, pushes, release rehearsals and explicit full recovery always run full.
- Every planned lane stays required, and the job graph and the public contexts do not change
  under (b).

The implementation keeps each of them. The candidate's environment is unchanged
(`worker.worker_environment` refuses a scope for any account but the validator). The fence,
`worker-run`'s candidate path and the root-granted input layout do not change; the hook's
output reaches the runner through its own fixed root operation. Unknown proof is a
`FullScope` before the hook runs, and an API failure, a malformed answer, a failed hook or a
malformed selection raises (`tests/test_ci_scope.py`). `scope.selection_inputs` names the config,
every adapter source, every plan input and the control files. Every run that is not a pull
request of this repository runs full (`not-a-pull-request`, `foreign-pull-request`), release
rehearsals and recovery dispatches included, and a pull request's packaged run takes no `mode`
that could ask for less. The job
graph, the job names and the contexts are those of v1.2.1 (`tests/test_workflow_packaged_e2e.py`).
A selected run is never full-baseline proof: no reader takes a record with `scope_sha256` as a
full run (`reuse`, `scope`), and only the packaged gate of a pull request may carry one.

## What it unblocks

*As proposed on 10 October; the decisions above are now taken and implemented, unreleased.*

Q1–Q6 do not need this. The native workflows, with their selection, stay required through Q6,
and the shadow runs of Q4 can run full profiles, which fit the lane budget (180 minutes, the same
as native). Q7 can activate the shared callers with full profiles, which is a cost the design
accepts and nobody has measured. The native baseline does not measure it either. Native PR pairs
took 26m55s–36m51s for Build and E2E together
([BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md#measured-ci-baseline)). The 34 lane artifacts of one
of those runs (37435530757) were 25.2–48.3 MiB each
(`tests/fixtures/ci_native/quick-skin/measured.json`). Both lanes sampled in detail there carry
a `full` evidence profile, so these numbers do not show what a selected run saves.

Once D1–D5 are decided, the change splits into kit pull requests: schema and ledger, then the
producer and hand-over, then the gate, status and reuse readers. A kit release and a Quick Skin
adapter change follow, before or after Q7.
