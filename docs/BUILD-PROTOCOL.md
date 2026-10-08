# Protected Build and packaged E2E: protocol reference

How the shared Build and packaged E2E pipeline of the kit works: what a run proves, which records
it writes and what each job step does. The code is `src/mod_base/build_ci/`, the reusable
workflows are `build.yml`, `select-build.yml`, `packaged-e2e.yml` and `gate-status.yml` in
`.github/workflows/`, and a mod calls them from managed caller workflows. Nothing here is released
or active in a mod: [BUILD-E2E-PROGRESS.md](BUILD-E2E-PROGRESS.md) says what exists and what
remains. The reasons are in [ADR 0007](adr/0007-protected-build-and-packaged-runtime.md) and
[BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md), the contract for a mod's own code in
[BUILD-ADAPTER.md](BUILD-ADAPTER.md), and the way a mod turns the pipeline on and off in
[OPERATIONS.md](OPERATIONS.md#builde2e-activation-and-rollback).

The selected Build hand-over between packaged jobs and the request budget are still being
changed, and the page marks them below. The candidate staging, execution and sealing commands
are registered and compose the real account and root operations.

## Trust model

A pull request is built by the protected default branch, not by itself. The managed callers run on
`pull_request_target`, so `github.sha` names a commit of the default branch (the *controller*), and
the workflow files, the kit pin and the mod's adapter all come from there. Kit code is checked out
at the pinned commit and compared with a tree digest before any of it runs. It authenticates the
pull request and the merge commit GitHub offers for testing, and checks that commit out as data
(the *candidate*), without credentials. Candidate code (its Python, Gradle and Minecraft) runs only
as the disposable account `modbase_candidate`, with an environment built from nothing and no token.
Protected code then terminates and locks that account, proves the tracked sources unchanged and
freezes what was exported. A second disposable account, `modbase_validator`, runs the mod's
protected verifier over the frozen bytes. Only after that does a job upload, and the token of every
kit job can only read. Required statuses are published by one job of the managed status caller,
the only job that holds the key of the mod's App; no kit workflow receives a secret. The boundary
is a pair of Unix accounts on a GitHub-hosted Linux runner, not a virtual machine:
`ci worker-prepare` refuses any other runner.

## Identity

### The subject of a job

`ci subject` is the first kit step of every Build and packaged job, and of a status job once its
evaluation needs the plan. It learns what the job tests from the API, never from the run's own
`head_sha` or from a caller input alone.

- **A pull request** (`--pr N`) must be open, not a draft, have its head and base in this
  repository (a fork is refused) and be based on the default branch at exactly the executing
  commit. The tested commit is the merge GitHub offers (`merge_commit_sha`); its parents must be
  the base and the head, in that order. While `mergeable` or `merge_commit_sha` is null,
  `ci subject` waits at most 15 seconds and four observations, within its existing request cap.
  A conflict or draft rejects immediately; the caller defers a draft and never calls. An outdated
  base field or first merge parent rejects with `ci-pr-base-outdated`: update the branch and push
  again after the default branch moves. The exact-current-base rule stays mandatory. A gate job (`--producer build` or
  `packaged`) takes a pull request on `pull_request_target` alone. A status job
  (`--producer status`) takes one on every event that starts the status caller and derives the
  same identity, and so the same plan, as the gates.
- **A protected subject** (`--pr ""`: a push to the default branch or a manual dispatch there).
  The live head of the default branch must be the executing commit, which is the tested commit and
  the controller at once.

Both give the same tuple (`build_ci.protocol.validate_identity`), written to the job's private
state as `identity.json`:

| Part | Fields |
| --- | --- |
| Source | `repository`, `source_repository`, `pr_number` (0 for a protected subject), `head_sha`, `head_branch`, `base_sha`, `base_branch` |
| Controller and kit | `controller_sha`, `controller_workflow` (always the managed Build caller, so the Build run and the packaged run that test one subject derive the same identity and the same plan), `controller_ref`; `kit.repository`, `kit.sha`, `kit.version`, `kit.tree_digest` (kit-digest-v1 of the verified checkout) |
| Tested commit, graph version | `tested_sha`, `tested_tree`, `tested_parents`; `graph_version` |
| Bound by `ci plan` | `policy_sha256`, `inventory_blob`, `inventory_sha256`, `scenario_sha256`, `runtime_selection_sha256` |

`policy_sha256` covers the bytes of the protected Build config, every adapter file it lists, the
mod's own control files (`config.CONTROL_PATHS`: the activation manifest and the four
`mod-base-*.yml` callers, each by its bytes or as absent), the kit pin with its tree digest,
`BUILD_ADAPTER_API` and both graph versions. Post-merge reuse compares it instead of the
controller commit, which every merge moves, so a merge that changes a caller or the activation
mode is tested in full afterwards. Every plan, envelope, validation record, selection, results
index, gate receipt and reuse reference carries the tuple as `identity`, with `plan_sha256` and
the `profile` (`quick-skin` or `block-pops`). A reader compares all of it with what it
authenticated itself: an artifact name, a caller input or the run API alone never establishes an
identity.

### A producer run

A producer run is a run of the managed Build caller or of the managed packaged E2E caller. A
reader finds it by what GitHub records on a run (`selection.newest_run`): the path of the caller's
workflow file and, for a pull request, the event `pull_request_target` with the pull request's
head commit, head branch and source repository; for a protected subject, the event `push` or
`workflow_dispatch` on the default branch at the tested commit. GitHub records a
`pull_request_target` run under the head of the pull request, not under the base it executes from.
The listing carries no status filter. The newest run by `(created_at, id)` and its latest attempt
are chosen before any result is read, and a run that does not qualify is never replaced by an
older one.

The run API does not say which commit a `pull_request_target` run executed from. The run's
`referenced_workflows` does: every managed caller first calls the mod's own `mod-base-guard.yml` as
a local reusable workflow, so GitHub lists that file at the commit the caller ran from.
`graph.authenticate_referenced_workflows` requires that entry at `identity.controller_sha` and
every kit workflow whose calling job runs in the run's mode at `identity.kit.sha`. A kit workflow of
this caller whose calling job is skipped may be listed too, at the same commit; any other entry,
another commit or a repeat is a rejection. The guard checks the list from inside the run, where a
kit workflow whose job has not started may still be absent, and requires the mod's single pin to
be the pin it was rendered for and a released commit of the kit's `main`. There is no run-title
contract: the records inside the artifacts bind the tested merge.

A record that names an artifact holds a *descriptor* (`records.validate_descriptor`): the identity,
`plan_sha256` and `profile`; the producer (`run_id`, `run_attempt`, `workflow_path`,
`workflow_ref`, `api_head_sha`, `event`, `graph_sha256`, `upload_window`); and the artifact (`id`,
`name`, `digest`, `size`, `created_at`, `expires_at`). The upload window is the upload step of the
job that produced the artifact, and `created_at` must lie inside it within the two-second
`CI_ARTIFACT_UPLOAD_SKEW_SECONDS` tolerance for the service and runner clocks. Upload, seal and job
ordering and artifact expiry stay exact. Timestamps are accepted in
the shapes the API returns and compared as whole-second UTC (`grammar.normalize_timestamp`).

## Document kinds

Thirteen kinds, all new at schema version 1 and strict (an unknown or duplicate key is a
rejection); the records the kit writes are canonical JSON. The previous release rejects each as an
unknown kind (`tests/test_schema_evolution.py`). [SCHEMAS.md](SCHEMAS.md) has the field tables of
the Build config, the batch manifest, the results index and the activation manifest. For the others
the validator named here, a function of `mod_base.build_ci`, is the definition, and
`tests/fixtures/documents/valid/ci-*.json` holds a valid example of each.

| Kind, file, validator | What it holds |
| --- | --- |
| `mod-base.build.config`, `scripts/ci/mod-base-build.json`, `config.validate_build_config` | The mod's protected Build configuration, read from the default branch only: the adapter entry points with the hashed list of files they import, the candidate files a plan is derived from, where a lane expects the Build, the two status contexts and the hook timeouts. |
| `mod-base.build.plan`, `ci-plan.json`, `protocol.validate_plan` | The targets and lanes of one subject with every planned output, under the identity. Protected code builds it around what `derive_plan` wrote. Every job that works on the subject derives it again, and the hashes must agree. |
| `mod-base.build.envelope`, `ci-envelope.json`, `records.validate_build_envelope` | The inventory of one sealed Build export, a target's partition or the complete bundle: path, size, SHA-256, lane and role of every file. The file set must equal the planned outputs. |
| `mod-base.ci.runtime-envelope`, `ci-runtime-envelope.json`, `runtime_schema.validate_runtime_envelope` | The inventory of one lane's sealed results, with the descriptor of the Build the lane ran against. |
| `mod-base.ci.validation`, `ci-validation.json`, `validation.validate_validation_receipt` | What a verification left: the hook, the unit, the run attempt, the digests of the protected config and of the inputs the hook was given, and the native reports the hook wrote, one for each unit. Root writes it; a hook never does. |
| `mod-base.ci.selection`, `ci-selection.json`, `records.validate_source_selection` | The exact Build one attempt of a packaged run consumes: the descriptor of the bundle, the hash of its envelope and the requesting run attempt with a nonce. No other attempt may consume it. |
| `mod-base.ci.results`, `ci-results.json`, `records.validate_results_index` | The complete results of a packaged attempt as an index: for every lane the descriptor of its artifact and the hashes of its envelope, validation record and report, and the owning Build. It is not a union of the lanes' bytes, which for Quick Skin exceed the 512 MiB allowed for one complete runtime export. |
| `mod-base.ci.gate`, `ci-gate.json`, `records.validate_gate_receipt` | The tested record of a gate: the mode of the run, the artifacts the gate verified, for a packaged gate the owning Build, and one native receipt (the hash of the verifier's report) for every target or lane, in plan order. |
| `mod-base.ci.reuse`, `ci-reuse.json`, `records.validate_reuse_reference` | The reference a reuse run seals instead of a tested record: the covered push, the reuse run and, as `source`, the identity and plan hash of the merged pull request with the descriptors of both its tested records. A source is always a tested record, never another reference. Both identities must agree on the tested tree, the policy digest, the inventory, the scenario contract, the runtime selection, the kit and the graph version, and the original plan hash must be the hash of the push's own plan under the original identity. A reference renews no retention. `reuse.download_reuse_reference` reads one back; no command consumes one yet. |
| `mod-base.ci.execution`, `ci-execution.json`, `handoff.validate_execution_handoff` | Private and local: the result of one successful protected hook as the runner hands it to root, with a nonce, the plan, config and input digests and the bounded log. |
| `mod-base.ci.root-request`, `ci-root-request.json`, `root_request_schema.validate_root_request` | Private and local: the one request of a root operation ("Root operations"). |
| `mod-base.ci.activation`, `site/mod-base-build-activation.json`, `activation.validate_activation` | How far a mod has adopted the pipeline: one of five modes, which alone decides the managed callers the mod has. |
| `mod-base.ci.batch`, `batch_schema.validate_batch_manifest` | The manifest of a batch pull request, carried in its body as a hint. A batch is verified by building its commits again. |

## Job graphs and modes

The graph of a run is the exact multiset of its job names, each with one expected conclusion: the
jobs the caller owns and the jobs of every workflow it calls. The jobs API reports a job of a
called workflow as `<calling job name> / <callee job name>`, a calling job that was skipped once
under its own name, and a matrix job that was skipped before it expanded once under its YAML name
(`Shared Build / Compile target ${{ matrix.id }}`). The names are constants of `mod_base.workflow`
and are compared with exact equality. `build_ci.graph` derives a graph from the caller, one mode
and the plan, never from the jobs it observes; its digest is the `graph_sha256` of a producer
record, and `tests/fixtures/ci_graphs/` holds a literal listing of every mode.

| Workflow | Jobs: id and exact name |
| --- | --- |
| `mod-base-build.yml` (managed caller) | `guard` "Verify pinned mod-base", which calls the mod's `mod-base-guard.yml` (one job, `verify` "Authenticate the pinned kit"); `deferred` "Build deferred for draft"; `shared` "Shared Build", which calls `build.yml` |
| `mod-base-packaged-e2e.yml` (managed caller) | `guard` "Verify pinned mod-base"; `deferred` "Packaged E2E deferred for draft"; `select` "Select exact Build", which calls `select-build.yml`; `rebuild` "Shared Build", which calls `build.yml`; `shared` "Shared Packaged E2E", which calls `packaged-e2e.yml` |
| `mod-base-gate-status.yml` (managed caller) | `guard` "Verify pinned mod-base"; `locate` "Locate the pull request"; `evaluate` "Evaluate protected gates", which calls `gate-status.yml`; `publish` "Publish protected gate statuses" |
| `build.yml` | `plan` "Plan protected Build"; `policy` "Verify protected policy"; `target` "Compile target {id}", one for each planned target; `assemble` "Seal complete Build bundle"; `gate` "Verify complete Build" |
| `select-build.yml` | `select` "Select exact Build source" |
| `packaged-e2e.yml` | `input` "Authenticate exact Build"; `lane` "Run packaged lane {id}", one for each planned lane; `aggregate` "Seal complete packaged results"; `gate` "Verify complete packaged E2E" |
| `gate-status.yml` | `evaluate` "Evaluate gate states" |

The two producers, the Build caller and the packaged E2E caller, run on `pull_request_target`
(opened, synchronize, reopened, ready for review, converted to draft), on a push to the mod's
canonical branch and on `workflow_dispatch`. Each such event starts one *generation*: a Build run
and a packaged run for the same subject. A new generation of a pull request cancels the one before
it. The status caller is no producer and its runs are nobody's evidence: it runs when a run of
either producer is requested or has completed, on the same pull-request events, once an hour for
one open pull request in turn and on request. The modes of the producers are a closed set
(`graph.BUILD_MODES`, `graph.PACKAGED_MODES`):

| Caller | Mode | When | Jobs that succeed; every other job is skipped |
| --- | --- | --- | --- |
| Build | `full` | a ready pull request; a push or dispatch without admitted reuse | the guard and all five jobs of `build.yml` |
| Build | `deferred` | a draft pull request | the guard and "Build deferred for draft" |
| Build | `reuse` | a push that `ci reuse-admit` admits | the guard, `plan` and `gate` |
| Packaged | `pull-request` | a ready pull request; the Build is a separate run | the guard and all four jobs of `packaged-e2e.yml` |
| Packaged | `deferred` | a draft pull request | the guard and "Packaged E2E deferred for draft" |
| Packaged | `selected` | a push or dispatch for which a Build run exists | the guard, `select` and all four jobs of `packaged-e2e.yml` |
| Packaged | `rebuilt` | a push or dispatch for which none exists | the guard, `select`, all five jobs of `build.yml` under "Shared Build" and all four jobs of `packaged-e2e.yml` |
| Packaged | `reuse` | a push that `ci reuse-admit` admits | the guard, `select` and the `gate` of `packaged-e2e.yml` |

A consumer decides the mode from what it has admitted itself (the subject, a sealed record) and
then requires exactly that graph. A deferred run seals nothing, a reuse run seals a reuse
reference, and every other mode ends in the tested record of its gate (`records.GATE_MODES`). An
attest-only dispatch of a mod stays on the mod's own route (negative fixture
`build-attest-only.json`). Which callers a mod has follows from the mode of its activation
manifest: none (`disabled`, or no manifest), all four (`shadow`, `shared-build-and-e2e`) or all
but the packaged E2E caller (`shared-build`); `reviewed-rollback` keeps those of the mode it leaves.

## The life of one job

Every job of a kit workflow has the same shape. Each kit step runs one command as the `runner`
user, `python3 -P -m mod_base ci <verb> --repo mod --config mod/site/mod-base.json --state DIR`,
with `PYTHONPATH` set to the verified kit, the state in `$RUNNER_TEMP/mb-state` and an API token
only where the verb reads the API. The steps of a `target` job, the most complete one, follow.
Steps 5 to 7 are written into the workflows, but their verbs were still being finished when this
page was written: they are described as specified.

| Step | What it does |
| --- | --- |
| 1. Prologue: shell and pinned actions, no kit code | Validates the call inputs, checks out the protected mod at `github.sha` into `mod/` and the kit at `inputs.kit-sha` into `kit/`, and requires both to be clean and at those commits, the kit's tree digest to equal the workflow's `MB_KIT_TREE_DIGEST` literal and the calling workflow to be a managed caller of the mod's canonical branch. Installs Python 3.13 and the hash-locked Pillow. A job that runs candidate code also checks out the tested commit into `candidate/`, without credentials; no step of the runner executes anything from it. |
| 2. `ci subject --producer build\|packaged\|status --pr N --github-output F` | Authenticates the subject, creates the state directory and writes `identity.json`. Outputs: `tested_sha`, `pr_number`. |
| 3. `ci worker-prepare --roles validator\|candidate+validator --python PATH [--java-home PATH]...` | Closes the runner home, has root fence the image, admits the tool trees (the interpreter's prefix and every JDK home: no foreign owner, no group or other write access, no special file), allocates the accounts and hands the protected adapter copy to the validator. It sends no API request. |
| 4. `ci plan [--candidate DIR] [--expect-sha256 HEX] [--github-output F]` | Stages the candidate files the config names (from the Git objects of `candidate/`, or from the API when the job has no such checkout), runs `derive_plan` as the validator, builds the plan around the result and writes `ci-plan.json`. With `--expect-sha256`, the hash the planning job derived, another plan fails the job. Outputs: `plan_sha256` and the job matrices `targets` and `lanes`. |
| 5. `ci worker-stage --candidate DIR [--bundle] [--gradle-seed DIR]` | Has root publish the tested tree for the candidate as `repository/`, with a `.git` reduced to objects and refs (no hooks, no configuration), the kit overlay (the copy of the pinned kit that a mod's bootstrap stages inside a checkout) and, when given, a Gradle seed. For a lane, `--bundle` also stages the Build from `sealed-build/` at the `bundle.path` of the config. |
| 6. `ci worker-run --hook policy\|build_target\|run_lane [--unit ID]` | Runs one candidate hook as `modbase_candidate` under the timeout the config names for it and always terminates the account. A non-zero exit, a timeout or a process left behind fails the step. A lane first gets the two values `derive_runtime` returns for it. |
| 7. `ci worker-seal`, also after a failure once step 3 succeeded | Terminates and locks the candidate, proves the tracked sources unchanged, freezes the export and writes its envelope. |
| 8. `ci worker-validate --hook verify_target\|verify_build\|verify_runtime [--unit ID] --output DIR`, the seal step "Validate frozen native exports" | Root hands the sealed export to the validator read-only, the hook runs from the protected adapter copy, and root copies exactly the expected reports out of the validator's home and writes the validation record. The command then writes the upload directory: the export under its own paths with its envelope, `ci-validation.json` and one `<unit id>.json` for each report. |
| 9. "Upload sealed outputs", `actions/upload-artifact` | The only step that uploads. Its start and end are the upload window of the artifact's descriptor. |
| 10. `ci worker-finish`, also after a failure once step 2 succeeded | Terminates and locks both accounts, requires that neither owns a process and only then reopens the runner home. |

**Candidate kit pin limitation.** `lifecycle.stage_kit_overlay` currently stages the protected
executing pin from the job's subject. An ordinary generation whose candidate retains that pin
resolves the overlay successfully. A candidate that bumps its pin cannot use this path: the
managed bootstrap rejects the staged old pin, even if the future pin's release tag and ancestry
pass protected admission. The real staging/bootstrap regression is
`tests/test_ci_lifecycle_candidate.py::KitOverlayTests::test_a_candidate_kit_bump_resolves_the_lifecycle_overlay`
(expected failure); the adjacent unchanged-pin test passes.

The scope decision is to keep ordinary K1–K6 generations on the protected pin and leave future-pin
staging unsupported until it is implemented before Q/B adopts that upgrade route. The design's
distinction between the protected executing pin and an approved candidate's future pin still
applies. Completion needs protected admission and staging of the candidate overlay separately
from the executing kit, with the stage record naming the kit actually supplied. The existing
bootstrap `stage` operation supports that distinction, but the worker lifecycle does not compose
it. K4's activation and bootstrap tests are not complete proof of future kit upgrades; a hosted
canary cannot supply this missing behavior. Do not bypass pin verification to make a bump pass.

The verbs of every job after the prologue, in step order (`workflow.CI_JOB_VERBS`, without the
step the note below the table describes):

| Job | Verbs |
| --- | --- |
| Build `plan` | `subject`, `worker-prepare`, `plan`, `reuse-admit` (a push only), `worker-finish` |
| Build `policy` | `subject`, `worker-prepare`, `plan`, `worker-stage`, `worker-run`, `worker-seal`, `worker-finish` |
| Build `target` | `subject`, `worker-prepare`, `plan`, `worker-stage`, `worker-run`, `worker-seal`, `worker-validate`, `worker-finish` |
| Build `assemble` | `subject`, `worker-prepare`, `plan`, `assemble`, `worker-validate`, `worker-finish` |
| Build `gate` | `subject`, `worker-prepare`, `plan`, `seal-gate`, `worker-finish` |
| `select-build` `select` | `subject`, `worker-prepare`, `plan`, `reuse-admit` (a push only), `select-build` (unless reuse was admitted), `worker-finish` |
| Packaged `input` | `subject`, `worker-prepare`, `plan`, `select-build`, `worker-finish` |
| Packaged `lane` | `subject`, `worker-prepare`, `plan`, `fetch-build`, `worker-stage`, `worker-run`, `worker-seal`, `worker-validate`, `worker-finish` |
| Packaged `aggregate` | `subject`, `worker-prepare`, `plan`, `aggregate`, `worker-finish` |
| Packaged `gate` | `subject`, `worker-prepare`, `plan`, `seal-gate`, `worker-finish` |
| `gate-status` `evaluate` | `gate-status --settle`; only when that could not settle: `subject`, `worker-prepare`, `plan`, `gate-status`, `worker-finish` |

The `lane`, `aggregate` and `gate` jobs of a packaged run work on the Build that `input`
selected, and none of them selects again. `input` returns its selection record as the job output
`selection`: the one line `ci select-build` writes. Each later job has a step without a kit
command and without a token, "Receive the selected Build", which admits one line of at most
65,536 characters in the alphabet of the kit's canonical JSON that names the record's kind, and
writes it to `$RUNNER_TEMP/mb-state/build-selection.json`, a new file of the runner alone. The
job's `ci fetch-build`, `ci aggregate` or `ci seal-gate` is given that file as `--selection` and
reads it before it sends a request (`commands_packaged.received_selection`): the bytes must be the
canonical JSON of a `mod-base.ci.selection` record of the job's own plan, requested by this very
run attempt of the packaged caller. A job output is a carrier, not a proof: the gate requires the
results index to own exactly the Build of that record and authenticates that Build through the
API. In a reuse run `input` is skipped, the gate skips the step and is given no `--selection`.

The verbs outside the worker lifecycle:

| Verb | What it does |
| --- | --- |
| `ci reuse-admit --github-output F` | For a push to the default branch, decides whether both gates of the pull request it merged cover the pushed commit (`reuse.admit_post_merge_reuse`); any other event answers `full` without a request. Admitted, `mode=reuse`: the commit is the final commit of exactly one merged pull request of this repository; the newest Build run and the newest packaged run of that pull request's last head completed successfully as their latest attempts under the kit pin that executes now, and neither is a deferral or a reuse run; the tree, the policy digest, the kit pin, the inventory, the scenario contract, the runtime selection and the plan are those of the push; both runs show their full graph and their tested records pair; every artifact they name is still available. Full run, `mode=full` with a `reason` of `reuse.FULL_RUN_REASONS`: an ordinary reason to test again, an expired original artifact among them. Error: an API failure, evidence that is still there and differs, or an original run that has not finished. The answer is kept as the state record `ci-reuse-admission.json`. |
| `ci assemble` | Describes the partition of every planned target of its own attempt from the API (one unexpired artifact for each, bound to the upload step of the job that sealed it), downloads each by numeric id, verifies it with the validation record its job uploaded and assembles the exact union into `sealed-build/`. |
| `ci select-build [--build-run-id ID\|same-run] [--wait-seconds N] --output FILE --github-output F` | Finds and authenticates the exact Build of a packaged run and writes the selection record. A pull request names no run and waits for the newest Build run of its head: one listing a minute, at most 5,400 seconds. A protected subject takes the newest Build run of its commit and waits within the same bound while that run is in progress (a push starts both callers together); when there is none it says so and its caller builds in the same run, and `same-run` then authenticates that Build. The bundle is downloaded and verified once, for the hash of its envelope. |
| `ci fetch-build --selection FILE` | Repeats the observation immediately before a lane uses the Build, requires the selected descriptor again, downloads the bundle by numeric id into `sealed-build/` and requires its envelope to have the recorded hash. The hand-over described above is to make it lighter. |
| `ci aggregate --selection FILE --output DIR` | The seal step of the `aggregate` job. Requires exactly one artifact of its own attempt for every planned lane, reads them by numeric id one at a time, verifies each against the plan, its validation record and the Build of the selection record, and writes `ci-results.json`. |
| `ci seal-gate --gate build\|packaged [--selection FILE] --output DIR` | The seal step of a gate. A gate runs inside the run it judges, so it authenticates the attempt as far as it exists: the live source; the run as its latest attempt, bound to the controller and the kit; every earlier job with the conclusion its graph expects; every artifact listed once and unexpired. A Build gate then downloads and verifies the complete Build. A packaged gate reads the results index, requires exactly the lane artifacts of its attempt, requires the index to own the Build and the envelope hash of the selection record, and authenticates that Build: unless this run rebuilt it, it must still be the complete bundle of the newest Build run of the live subject, which the gate observes here and once more before the receipt (no lane asks that question). It writes `ci-gate.json`. In a reuse run it decides the reuse again, as `ci reuse-admit` did, and writes `ci-reuse.json`; when the original gates no longer cover the commit it seals nothing and fails. A reader of a tested record later requires the completed graph and the real chronology (`graph.authenticate_gate_timeline`). |
| `ci gate-status --pr N [--settle] --github-output F` | Read-only: the state each gate may show on the current head of a pull request, from the newest run of each producer. The first call of the status job, `--settle`, runs before any subject and answers only when no gate needs the plan: a draft, no run yet, a run in progress, or a newest run that failed or was cancelled (`settled=true` with `intents`). When a newest run finished successfully it outputs `settled=false`; the job then authenticates the pull request, derives the plan and calls the command again. `success` needs that second call: the newest run is complete, its exact graph authenticates, its tested record binds to the plan, the packaged gate consumed exactly the bundle the Build gate sealed, and the live pull request still has the head, base and test merge of that plan. `pending`: a draft, no run yet, a run in progress or a draft deferral. `failure`: anything else, with one line that says why. `intents` holds for each gate the context string of the protected Build config (with the suffix ` (shadow)` in shadow mode, so that it cannot be a required name), the state, the description and the URL of the deciding run; the target is the head commit of the pull request. |
| `ci batch-prepare --name NAME --allowed-paths FILE [--dry-run] PR...` | No job step. Squashes up to 50 open pull requests of this repository, in the given order, onto the head of the default branch, pushes them as a new `batch/<name>` branch and opens one ready pull request. Like `batch-settle` it uses the token of the invocation, which must belong to an App or an automation account: a pull request opened with the default token of a workflow starts no runs. |
| `ci batch-settle --pr PR --plan FILE --build-seal FILE --packaged-seal FILE [--delete-branches]` | No job step. After the batch pull request has merged: proves the merged commit, the rebuilt commits and both tested records, then closes the members that are still at their batched head. |

The intents are published by the `publish` job of the managed status caller, the one job that
names the environment `mod-base-gate` and, in it, the client id and the key of the mod's App,
which the owner creates ([OPERATIONS.md](OPERATIONS.md#the-gate-status-app)). It mints an App
token that can only write commit statuses, admits the document as a whole or not at all, reads the
live pull request again and posts one status for each gate, a state that is no success first.

## The sandbox

Every path is fixed (`build_ci.worker.WORKER_ROOT`, the directory constants of `build_ci.adapter`):

```
/tmp/mod-base-sandbox-boundary/mod-base-worker/
  candidate-home/     HOME of modbase_candidate: export/ (hook output), tmp/, gradle-home/
  validator-home/     HOME of modbase_validator: validation/ (hook output)
  repository/         the tested commit, staged for the candidate
  controller/         the protected Build config and adapter files, read-only for the validator
  validation-input/   inventory, scenario-contract, the extra plan inputs and ci-plan.json
  derived-plan/       the one file derive_plan wrote, taken out by root
  sealed-build/       a frozen Build export with ci-envelope.json
  sealed-runtime/     a frozen lane export with ci-runtime-envelope.json
  sealed-validation/  the reports of one verification with ci-validation.json
  execution-handoff/  the private record of one protected hook execution
  root-request-<operation>/   the private request of one root operation
```

The state of a job is `$RUNNER_TEMP/mb-state`, a directory only the runner can enter, whose
records are written once and never replaced: `identity.json`, `worker-host.json`, `worker.json`,
`ci-plan.json` and, where a job writes them, `ci-selection.json`, `ci-partitions.json` and
`ci-reuse-admission.json`. Both accounts are created for the job; an account or a worker root that
already exists is refused. A hook starts with a umask of 077 as
`sudo -n --user '#<uid>' -- setpriv --no-new-privs -- env -i --chdir=<checkout> <environment> ...`,
and between two hooks both accounts are terminated and locked.

## Root operations

Creating, locking, killing and entering an account are fixed `sudo` command lines of
`build_ci.worker`. All other root work runs in one child process that the runner starts with a
fixed environment and no inherited descriptors (`root_request.run_root_operation`):

```
/usr/bin/sudo -n -- <python> -I -B -S <kit>/tools/ci_privileged_bootstrap.py \
    --operation <name> --kit <kit> --kit-digest sha256:<hex> --nonce <hex>
```

The bootstrap uses the standard library alone until it has recomputed kit-digest-v1 of `<kit>`,
the checkout the prologue verified, and found it equal to `--kit-digest`; only then does it import
the operations from that checkout and run one. Everything else travels in one private
`mod-base.ci.root-request` in `root-request-<operation>/` (a 0700 directory, a 0600 file with one
link, owned by the runner): the operation, the nonce, the identity of the closed runner home and
a closed argument object. Root admits the request and the live fence, runs the operation and reads
the request again unchanged. A request selects no program, hook or destination, and root never
calls the GitHub API. The closed list is `grammar.CI_ROOT_OPERATIONS`, which the bootstrap mirrors
without importing the kit (`tests/test_ci_privileged_bootstrap.py` keeps both equal). The verbs
that are still being finished may add to it; when this page was written it held:

| Operation | What root does |
| --- | --- |
| `host-fence` | Before any worker account exists: removes group and other write permission and default ACLs from `/opt`, `/usr/share`, `/usr/local`, `/usr/lib/jvm` and `/var/lib/gems`, then fails unless no world-writable regular file and no world-writable directory without the sticky bit is left on the root filesystem outside the worker boundary |
| `grant-controller` | Hands the protected adapter copy in `controller/` to the validator, read-only |
| `grant-plan-inputs`, `take-derived-plan`, `grant-validation-inputs` | Around `derive_plan`: hands the staged candidate files in `validation-input/` to the validator, takes the one file the hook wrote out of the validator's home, and hands `validation-input/` over again with the plan |
| `stage-candidate` | Publishes the tested tree, its reduced `.git`, the kit overlay and an optional Gradle seed for the candidate; the only operation whose request names directories, all below the fenced runner home |
| `grant-build-validation`, `grant-runtime-validation` | Verifies the frozen copy in `sealed-build/`, or in `sealed-runtime/` for one lane, and hands it to the validator, read-only |
| `freeze-build-validation`, `freeze-runtime-validation` | Terminates the validator, copies exactly the expected reports into `sealed-validation/` and writes the validation record, for a Build export or for one lane |

## Artifacts

Names come from `grammar.ci_artifact_name` alone (`R` is the run id, `N` the attempt), retention
from `limits.CI_RETENTION_DAYS`; `tests/test_workflow_ci_policy.py` holds the workflows to both.

| Kind | Name | Uploaded by | Holds | Days |
| --- | --- | --- | --- | --- |
| `target` | `mb-ci-target--R--aN--<target id>` | Build `target` | one target's sealed outputs with `ci-envelope.json`, `ci-validation.json` and `<target id>.json` | 1 |
| `build` | `mb-ci-build--R--aN` | Build `assemble` | the complete Build with its envelope, its validation record and one report for every target | 7 |
| `runtime` | `mb-ci-runtime--R--aN--<lane id>` | packaged `lane` | one lane's sealed results with `ci-runtime-envelope.json`, `ci-validation.json` and `<lane id>.json` | 7 |
| `results` | `mb-ci-results--R--aN` | packaged `aggregate` | `ci-results.json` alone | 7 |
| `tested` | `mb-ci-tested--R--aN--build`, `mb-ci-tested--R--aN--packaged` | the `gate` of each workflow | `ci-gate.json` alone | 90 |
| `reuse` | `mb-ci-reuse--R--aN` | the `gate` of a reuse run | `ci-reuse.json` alone | 90 |

An artifact is read only by its numeric id through the kit's own client (`build_ci.transport`); no
workflow uses a download action. The archive's length and SHA-256 must equal the descriptor before
it is opened, an archive is at most 512 MiB and a record at most 4 MiB, and every extracted file is
compared with the envelope. The Pages rotation does not recognise these names.

## Failures

What the commands do in the situations of the design's failure table, and in the one the fourth
row adds. Every rejection is an `MbError`: exit code 2 and one bounded line on standard error.

| Situation | Behaviour |
| --- | --- |
| The newest Build run of a ready pull request is a draft deferral | `ci select-build` does not take it and keeps waiting for a newer run; `ci gate-status` answers `pending`. `ci subject` itself rejects a draft, so a kit workflow never works for one |
| No Build run, a Build still running, or the wait runs out | `ci select-build` waits for a Build run that is in progress, for a pull request and for a push alike: one listing a minute, at most 5,400 seconds. A pull request also waits while no run exists; a protected subject then reports `found=false` and its caller builds in the same run. When the wait runs out the command fails and asks for a rerun of Build and E2E. `ci gate-status` answers `pending` |
| The newest Build failed or was cancelled, or the head, the base or the test merge moved | A rejection. No command falls back to an older run. The source is observed at the start of a command and again immediately before its effect (`reads.Watch`) |
| The default branch moves while a pull-request generation runs | The pull request is no longer based on the executing commit, so the jobs of that generation reject from then on ("protected executing controller has moved"), and a status evaluation that needs the plan fails in the same way and publishes nothing. A rerun cannot succeed, because GitHub reruns a run at its original commit. The pull request needs a new event: a push or an update of its branch |
| The Build bundle of a pull request is missing or has expired | A rejection that asks for a rerun of the Build. A pull request never compiles inside its packaged run |
| A standalone run has no Build | The caller's `rebuild` job calls `build.yml` in the same run, and `packaged-e2e.yml` consumes that Build (`build-run-id: same-run`). A newest Build run that exists but failed or lost its bundle is a rejection, not a reason to rebuild |
| Post-merge evidence cannot be reused | `ci reuse-admit` answers `mode=full` with its reason and the push runs the full graph: no merged pull request ends in the commit, the tree, the policy or the plan differs, the original run failed, deferred a draft or was itself a reuse, or an original artifact has expired or is gone. Evidence that is still there and differs fails the job |
| An original run of the merged pull request has not finished | `ci reuse-admit` fails: reuse is not admitted, and a full run must not race a result that is unknown. Rerun the job when the original run has finished |
| The gate of a reuse run finds the reuse no longer admitted | `ci seal-gate` seals nothing and fails. Rerunning all jobs of the run decides again and tests in full |
| Malformed or ambiguous metadata, a digest, hash or graph mismatch, an unsafe archive | A rejection. Nothing turns it into "not found" |
| A GitHub API failure | The client sends a request at most four times (transport errors, HTTP 408, 429, 500, 502, 503, 504 and rate-limit answers, with delays of at most 30 seconds); then the command fails, as it does when its request budget is spent. `ci gate-status` then produces no intent at all |
| A rerun of failed jobs only | A job or an artifact of an earlier attempt is refused with "a failed-jobs-only rerun mixes attempts; rerun all jobs", and a selection record serves only the attempt that requested it |
| An artifact disappears after it was selected | `ci fetch-build` repeats the selection immediately before the download and requires the same unexpired descriptor; `ci seal-gate` observes the source, the run and its artifacts once more before it returns |
| A target, a lane or a batch member is missing | `ci assemble` and `ci aggregate` require exactly one artifact of their own attempt for every planned unit, and a gate one native receipt for every planned unit. `ci batch-settle` refuses a batch whose rebuilt commits differ from its manifest |
| A Pages, AI review or notification failure | Outside this pipeline: no `ci` command reads or writes Pages state, and the Pages workflows are unchanged |

Hook timeouts and job deadlines are separate bounds. The configuration's six-hour maximum is
per hook, not a promise that every admitted configuration fits a whole job; the job deadline
remains authoritative and a timed-out job cannot produce a successful gate. The generic test
configuration permits a 60-minute policy hook inside a 60-minute job and a 120-minute target hook
inside a 120-minute job, before setup, planning and verification. The packaged input job permits
a 10-minute planning hook plus a 90-minute Build wait inside 100 minutes. This distinction is
retained deliberately as `CiConfiguredTimeoutTests.test_no_admitted_hook_timeout_is_as_long_as_the_job_that_runs_it`
in `tests/test_workflow_ci_policy.py` (`expectedFailure`). No adapter timing has been measured for
the mods: choosing their budgets and setup margin belongs to K7 and the Q/B migrations.

## Request budget

The token of a workflow run may send 1,000 REST requests an hour for its repository. Every
command that reads the API therefore builds its client with an explicit `max_requests`, retries
included (`commands.api_client`), a test pins the number of requests of a typical case, and
objects named by SHA and the job list of a completed attempt are fetched once per command
(`reads.CommandReads`). The six `worker-*` verbs receive no token.

| Command | Requests in the pinned case | Cap in `model/limits.py` | Pinned in |
| --- | --- | --- | --- |
| `ci subject` | 4 for a pull request, 5 for a protected subject | `MAX_CI_SUBJECT_REQUESTS`, 16 | `tests/test_ci_commands_subject.py` |
| `ci plan` | 0 with a candidate checkout; otherwise the tree and one blob for each candidate file, 3 to 11 | `MAX_CI_PLAN_REQUESTS`, 24 | `tests/test_ci_lifecycle.py` |
| `ci reuse-admit` | 38 for an admitted reuse, 11 when the merged tree differs, 1 for a push that merges no pull request | `MAX_CI_REUSE_ADMIT_REQUESTS`, 96 | `tests/test_ci_reuse.py` |
| `ci select-build` | 17 for a pull request whose Build is complete and one more for every poll before that; 15 for a protected subject | `MAX_CI_SELECT_BUILD_REQUESTS`, 155 | `tests/test_ci_commands_packaged.py` |
| `ci fetch-build` | 21 for a pull request; 18 for a selected and 15 for a rebuilt Build | `MAX_CI_FETCH_BUILD_REQUESTS`, 48 | `tests/test_ci_commands_packaged.py` |
| `ci assemble` | 15 and 2 for each target: 49 for 17 targets | `MAX_CI_ASSEMBLE_REQUESTS`, 816 | `tests/test_ci_commands_build.py` |
| `ci aggregate` | 13 and 2 for each lane: 81 for 34 lanes | `MAX_CI_AGGREGATE_REQUESTS`, 816 | `tests/test_ci_aggregate.py` |
| `ci seal-gate` | 15 for a Build gate, 23 for the packaged gate of a pull request, 47 for the gate of a reuse run | `MAX_CI_GATE_REQUESTS`, 96 | `tests/test_ci_gate.py`, `tests/test_ci_reuse_seal.py` |
| `ci gate-status` | 2 to 8 with `--settle`; 45 with both runs complete | `MAX_CI_GATE_STATUS_REQUESTS`, 96 | `tests/test_ci_commands_status.py` |
| `ci batch-prepare` | 10 and 3 for each member: 160 for 50 | `MAX_CI_BATCH_PREPARE_REQUESTS`, 216 | `tests/test_ci_batch_commands.py` |
| `ci batch-settle` | 3, 28 for both gates and at most 5 for each member: 281 for 50 | `MAX_CI_BATCH_SETTLE_REQUESTS`, 348 | `tests/test_ci_batch_commands.py` |

Still moving: these numbers are pending. As the workflows stood when this page was written, one
pull-request generation of the largest mod (Quick Skin: 17 targets, 34 lanes) would cost about
1,860 requests, more than the hourly allowance. The Build run accounts for about 160 of them and
the 34 lane jobs for about 1,430, because every lane job authenticates the subject, selects the
Build again and fetches it. Two changes in progress cut this: the hand-over of the selection
record described above, with a lighter `ci fetch-build`, and a `ci subject` that costs one request
in a job that holds the candidate checkout. The total of a generation then becomes a pinned test;
until it does, the figures of those three commands and every total are provisional.

## What only a hosted canary can confirm

The unit tests fake the GitHub API, and `tests/ci_linux_worker.py` runs the accounts, `sudo` and
the root operations on a GitHub-hosted runner in the kit's own CI. Neither runs a managed caller on
GitHub. The following rests on documentation, on fixtures or on single observations, and the
canary of step K7 has to show it before a mod adopts the pipeline:

- that a run of a managed `pull_request_target` caller and its artifacts are recorded under the
  head commit and branch of the pull request, and that a listing filtered by branch, `head_sha` and
  event returns the run. This was seen on runs of Block Pops' own workflow
  (`tests/fixtures/ci_native/block-pops/jobs.json`), not on a managed caller;
- what `referenced_workflows` lists for such a run: the local guard at the controller commit, each
  kit workflow at the kit commit, and whether a kit workflow whose calling job was skipped appears;
- how the jobs API names a skipped calling job, a matrix job that was skipped before it expanded
  and a skipped job of the caller. The listings in `tests/fixtures/ci_graphs/` assume these names;
- that an artifact's `created_at` lies inside the window of its upload step once both are cut to
  whole seconds, and which timestamp shapes the API returns;
- that marking a draft ready starts a new run for the same head, which the wait for a Build relies
  on to leave a deferral behind;
- that the kit workflows run as written on a hosted `ubuntu-24.04` image: the steps of a job have
  run as separate commands in tests, never as one job, and the workflows pass the image's
  `JAVA_HOME_17_X64`, `JAVA_HOME_21_X64` and `JAVA_HOME_25_X64` as JDK homes;
- how many API requests a real generation spends against the hourly allowance of its token;
- that a `workflow_run` event of a producer run carries the head branch, head commit and head
  repository by which the status caller finds the pull request, and that the pull requests GitHub
  lists for a pushed commit include the one post-merge reuse looks for;
- how a required-status rule treats a status that an App publishes on the head of a pull request
  next to a check of the same name, which the design wants tried in a synthetic protected
  repository. Its row K7 lists the further cases the canary has to run.
