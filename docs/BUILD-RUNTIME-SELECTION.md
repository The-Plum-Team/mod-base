# Feature-selected runtime scope: design note

Draft for owner decision, 10 October 2026. This note proposes how a packaged lane could run only
the scenarios a pull request affects (Quick Skin adoption gap QS-G6). It is not an
implementation. It changes no code, schema, workflow, digest or lock, and it authorizes no
release.

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
and adapter versions carry it. Each of those is a frozen interface ([KIT.md](ai/KIT.md#frozen-interfaces)),
so each needs a version decision.

## Why the mapping's minimal change cannot ship as written

The mapping proposes that the packaged `input` job run a protected selection script and fold its
digest into `identity.runtime_selection_sha256` (`planning.py:42`). Three producers share that
plan, however, and each derives it independently:

- The Build run plans for itself and seals its bundle and tested record under its `identity` and
  `plan_sha256`. A packaged consumer requires both to equal its own plan
  (`transport._bound`, `transport.py:95-97`). If only the packaged plan carried the selection, the
  consumer would refuse its Build as "selected artifact differs from the admitted plan".
- `gate-status.yml` derives one plan ("Derive the protected plan") and verifies both runs' tested
  records against it (`status._generation_plan`). It runs later, on its own triggers.
- Every job of a generation plans again with `--expect-sha256`, and the same inputs must give the
  same document.

A selection that each producer computes again from the API would read mutable state, such as
whether a baseline exists or has expired, at different times. The plan would then differ between
Build, packaged and status. That fails closed, but it would make gates fail at random, so it is not
a safe optimization.

## Decisions for the owner

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

- *Token-bearing mod producer.* This keeps Quick Skin's own certificate and ancestry proof
  unchanged, but it widens the boundary: protected mod code would run with a token on the fenced
  runner.
- *Kit-authenticated inputs and a token-free hook.* The kit authenticates generic inputs and gives
  a new validator hook only data. One input is the paths changed between the tested merge's first
  parent (`identity.tested_parents[0]`, the base) and the tested tree. The other is the base
  commit's own sealed full packaged tested record and results index, read with the readers reuse
  already has. The boundary does not change, but Quick Skin's baseline would become the kit's
  evidence of the base commit instead of its own certificate, and that is the mod's decision.

Either option adds a hook, which needs a `BUILD_ADAPTER_API` decision: an optional hook within
version 1, or version 2.

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
- Forks, dispatches, pushes, release rehearsals and explicit full recovery always run full.
- Every planned lane stays required, and the job graph and the public contexts do not change
  under (b).

## What it unblocks

Q1–Q6 do not need this, because full profiles fit the lane budget (180 minutes, the same as
native). Q7 can activate the shared callers with full profiles, which is a cost the design accepts
and nobody has measured. Without selection Quick Skin does lose native parity: native PR lane
artifacts were 26–48 MB each, and its paired Build+E2E time was 26m55s–36m51s
([BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md#measured-ci-baseline)). Once D1–D5 are decided, the
change splits into kit pull requests: schema and ledger, then the producer and hand-over, then the
gate, status and reuse readers. A kit release and a Quick Skin adapter change follow, before or
after Q7.
