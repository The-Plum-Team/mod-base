# Build and packaged E2E: status

Where the implementation of [BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md) stands, by the steps of its
section "Independently mergeable migration". [BUILD-PROTOCOL.md](BUILD-PROTOCOL.md) explains how
the pieces work and [ADR 0007](adr/0007-protected-build-and-packaged-runtime.md) why.

Nothing is released and nothing is active. The work is the unreleased section of `CHANGELOG.md`
(planned v1.1.0). No mod pins it, no mod has an activation manifest, and no managed Build or
packaged E2E caller has run on GitHub. The design is a draft that awaits the owner's agreement,
and ADR 0007 is "Proposed". **Step K7, the release candidate and the hosted canary, has not been
done and cannot be done without the owner.** Two parts of the kit's own steps were still being
changed when this page was written; "Still moving" lists them.

## How to read the evidence

- **Suite**: a module of `tests/` that the ordinary suite runs on Python 3.11, 3.12 and 3.13. The
  GitHub API is always a fake (`mod_base.github.fake.FakeGitHub`). The newer modules use real
  files, Git and processes; several older ones also replace system calls or account operations and
  say so in their docstrings.
- **Hosted**: a class of `tests/ci_linux_worker.py`. It creates the real accounts and uses `sudo`,
  so the suite does not collect it. The kit's CI runs it on a GitHub-hosted runner in every Python
  leg (step "Require real disposable-account termination on Linux").
- **Workflow policy**: `tests/test_workflow_ci_policy.py` and one module for each kit workflow.
  They read the YAML against the registry tables of `workflow.py`, run actionlint and shellcheck
  and execute every `run:` body with the kit command replaced by a recorder.

None of these runs a job of the pipeline on GitHub Actions. What only such a run can show is the
last section of BUILD-PROTOCOL.md.

## The kit (K1 to K7)

Module names are relative to `src/mod_base/build_ci/` and test names to `tests/`.

| Step | What exists | Evidence | What remains |
| --- | --- | --- | --- |
| K1: protocol, schemas, graphs | ADR 0007. Thirteen document kinds at version 1 with strict validators (`protocol.py`, `records.py`, `runtime_schema.py`, `validation.py`, `handoff.py`, `root_request_schema.py`, `config.py`, `activation.py`, `batch_schema.py`) and the compatibility ledger `tests/fixtures/documents/compatibility.json`. The graph contract (`graph.py`, the names in `workflow.py`) with a literal job listing of every mode in `tests/fixtures/ci_graphs/`. The bounds in `model/limits.py`. Parity fixtures of both mods in `tests/fixtures/ci_native/`. | Suite: `test_schema_evolution.py`, `test_ci_protocol.py`, `test_ci_records.py`, `test_ci_results_index.py`, `test_ci_runtime_envelope.py`, `test_ci_limits.py`, `test_ci_native.py`, `test_ci_native_plan.py`, `test_ci_gate_timeline.py`. | The owner's agreement to the design and to the ADR, and the owner's confirmation of the two bounds that are stricter than the mods' own: one 512 MiB archive cap for every profile, and 512 files and 256 MiB counted for a whole lane. ADR 0007 needs two corrections: it lists the kind `mod-base.ci.runtime-root-request`, which no longer exists, and it calls the format of Quick Skin's complete results undecided, which `mod-base.ci.results` settled. |
| K2: worker, sealing, second validator | Accounts and execution (`worker.py`), the host fence (`host.py`), tool admission (`toolchain.py`), the root channel (`root_request.py`, `root_request_schema.py`, `root_request_operations.py`, `tools/ci_privileged_bootstrap.py`), candidate staging (`worker_preparation.py`, `worker_source.py`, `worker_git.py`, `worker_overlay.py`, `gradle_cache.py`, `source.py`), sealing and verification (`exports.py`, `runtime_freeze.py`, `runtime_exports.py`, `inputs.py`, `runtime_inputs.py`, `controller.py`, `validation.py`, `handoff.py`, `runtime_handoff.py`), the policy runner's rules (`policy.py`) and the adapter contract (`adapter.py`, `config.py`, `planning.py`, `identity.py`, [BUILD-ADAPTER.md](BUILD-ADAPTER.md)). `ci subject` writes the identity record, and `lifecycle.py` composes the rest into `ci worker-prepare`, `ci plan`, `ci worker-stage`, `ci worker-run`, `ci worker-seal`, `ci worker-validate` and `ci worker-finish`. A synthetic mod with all eight hooks is `tests/fixtures/ci_mod/`. | Hosted: `LinuxHostFenceTests`, `LinuxWorkerTests`, `LinuxCandidateStagingTests`, `LinuxLifecycleCommandTests`, `LinuxCandidateCommandTests`, `LinuxWorkerValidateTests`, `LinuxJobChainTests` and the copy classes `LinuxSourceTests`, `LinuxControllerSourceTests`, `LinuxValidationPlanTests`, `LinuxValidationExportTests`, `LinuxExportCopyTests`. Suite: `test_ci_lifecycle.py`, `test_ci_worker*.py`, `test_ci_host.py`, `test_ci_toolchain.py`, `test_ci_root_request*.py`, `test_ci_privileged_bootstrap.py`, `test_ci_mod.py`, `test_ci_adapter.py`, `test_ci_planning.py`, `test_ci_identity.py`, `test_ci_commands_subject.py`, `test_ci_policy.py`, `test_ci_validated_export.py`. | Neither mod has an adapter for `BUILD_ADAPTER_API = 1` (Q1, B1): the hooks have run only for the synthetic mod. Planning does not yet refuse an output whose path collides with a report name of the artifact it is uploaded in; the uploading job fails instead. The kit workflows pass no Gradle seed, so every candidate starts with an empty Gradle home. Several older suite modules (`test_ci_runtime_freeze.py`, `test_ci_runtime_handoff.py`, `test_ci_runtime_inputs.py`, `test_ci_execution_handoff.py` and `test_ci_host.py` among them) replace system calls or account operations, so that code is proven only where a hosted class runs it. |
| K3: Build and packaged workflows | `.github/workflows/build.yml` (`plan`, `policy`, `target`, `assemble`, `gate`), `select-build.yml` (`select`), `packaged-e2e.yml` (`input`, `lane`, `aggregate`, `gate`) and `gate-status.yml` (`evaluate`), with their registry and job tables in `workflow.py`. Selection, transport and gates (`selection.py`, `transport.py`, `describe.py`, `gate.py`, `reads.py`, `authenticate.py`, `archive.py`) with `ci select-build`, `ci fetch-build`, `ci assemble`, `ci aggregate` and `ci seal-gate`. The status evaluation (`status.py`) with `ci gate-status`, which settles what needs no plan before it derives one. | Workflow policy: `test_workflow_ci_policy.py`, `test_workflow_build.py`, `test_workflow_select_build.py`, `test_workflow_packaged_e2e.py`, `test_workflow_gate_status.py`. Suite: `test_ci_build_selection.py`, `test_ci_packaged_selection.py`, `test_ci_latest_download.py`, `test_ci_transport.py`, `test_ci_describe.py`, `test_ci_commands_build.py`, `test_ci_commands_packaged.py`, `test_ci_packaged_job_sequence.py` (a whole packaged run with the command lines of its workflow), `test_ci_aggregate.py`, `test_ci_gate.py`, `test_ci_gate_transport.py`, `test_ci_commands_status.py`. Hosted (a real Linux filesystem, no accounts): `LinuxBuildTransportTests`, `LinuxBuildAssemblyTests`, `LinuxGateTransportTests`, `LinuxLatestBuildDownloadTests`, `LinuxBuildArchiveTests`. | The request budget: see "Still moving". No workflow has run on GitHub (K7), so whether a job output carries the selection record of the `input` job intact to the later jobs of a packaged run rests on GitHub's documented limits (1 MB for the outputs of one job; the record is about 4 kB). |
| K4: managed callers, activation, bootstrap | The closed registry of rendered callers and the activation check of `template check`, `sync` and `init` (`src/mod_base/template/tool.py`); the activation manifest and its transitions (`activation.py`, `transition.py`); the commands `template activation` and `template transition`; the bootstrap's `bump`, which requires the target kit to read the manifest and restores the pin files when its write phase fails. The managed templates in `template/managed/.github/workflows/`: `mod-base-guard.yml`, which all three callers call first, `mod-base-build.yml`, `mod-base-packaged-e2e.yml` and `mod-base-gate-status.yml` (`guard`, `locate`, `evaluate`, `publish`), the only writer of the two gate statuses. The procedures are in [OPERATIONS.md](OPERATIONS.md#builde2e-activation-and-rollback). | Suite: `test_ci_activation.py`, `test_template_activation.py`, `test_template_transition.py`, `test_template_rendered_registry.py`, `test_managed_ci_callers.py`, `test_managed_status_caller.py`, `test_bootstrap_bump_planning.py`, `test_template_released_bootstraps.py`, `test_ci_controller_activation.py`. | The status caller publishes nothing until the owner has created the environment `mod-base-gate` with the variable and the secret of the mod's App in the mod's repository; until then a run of it fails in `publish` and can never publish a success instead. The admission of a transition is library code and the operator command `template transition`; no workflow runs it. The managed `.gitattributes` has no `eol=lf` rule for the four callers. |
| K5: batches | `batch.py`, `batch_git.py`, `batch_schema.py` and `commands_batch.py`: `ci batch-prepare` and `ci batch-settle`, with the kind `mod-base.ci.batch`. | Suite: `test_ci_batch.py`, `test_ci_batch_git.py`, `test_ci_batch_manifest.py`, `test_ci_batch_settle.py`, `test_ci_batch_commands.py`, `test_ci_batch_api.py`. | No workflow, managed caller or written procedure starts the two commands. They need the token of an App or of an automation account that may push `batch/*` branches and open and close pull requests, and the allowed-path list of the mod's protected policy. |
| K6: post-merge reuse | The decision (`reuse.py`: `admit_post_merge_reuse` with its three outcomes, admitted, full run with a reason, or an error), `ci reuse-admit` in the `plan` job of `build.yml` and in `select-build.yml` (`commands_reuse.py`), the gate of a reuse run, which decides again and seals the reference `ci-reuse.json` (`gate.seal_reuse`), and the reader `reuse.download_reuse_reference`. The readers of the original evidence of a merged pull request (`authenticate.authenticate_merged_pr_identity`, `transport.download_merged_gate_pair` and the other `download_merged_*` functions). The policy digest that reuse compares covers the mod's caller workflows and activation manifest, so a merge that changes one of them is tested in full. | Suite: `test_ci_reuse.py`, `test_ci_reuse_seal.py`, `test_ci_merged_unavailable.py`, `test_ci_records.py`, `test_ci_merged_pr.py`, `test_ci_merged_build_selection.py`, `test_ci_merged_gate_transport.py`, `test_ci_merged_gate_pair.py`, `test_ci_merged_build.py`, `test_ci_merged_runtime.py`, `test_ci_merged_inputs.py`, and the listings `build-reuse.json` and `packaged-reuse.json`. | No command consumes a reuse reference yet: the status evaluation covers pull requests only, and the mods' own consumers need readers (Q9, B6). No reuse run on GitHub (K7). |
| K7: release candidate and hosted canary | Nothing for this pipeline. `canary/` and the canary procedure of OPERATIONS.md cover Pages only. The synthetic mod `tests/fixtures/ci_mod/` is the fixture the canary is to use. | None. | All of it, and it needs the owner: the immutable tag of the release candidate, which the owner's release procedure creates; an isolated test repository with the managed callers, an activation manifest and an App that can publish statuses; then the runs the design's row K7 lists. A tag whose canary fails gets no Release, and no mod pins it. |

## Still moving

The request budget was being changed when this page was written. The reference describes it as
it is meant to work and marks it.

1. **The request budget.** A workflow token may send 1,000 REST requests an hour for its
   repository. As the workflows stand, one generation of a pull request (its Build run and its
   packaged run) would cost about 1,860 for the largest mod, with 17 targets and 34 lanes. The
   later jobs of a packaged run no longer select again; a lighter `ci fetch-build`, and a
   `ci subject` that costs one request in a
   job that holds the candidate checkout, are to cut this further, and the total of a generation is to
   become a pinned test. The numbers in BUILD-PROTOCOL.md, "Request budget", are pending until then.

## Known behaviour

A pull request is tested on the commit of the default branch that was its base when the run
started. When the default branch moves, the runs of that pull request that are in flight reject
("protected executing controller has moved"), and a status evaluation that needs the plan fails in
`ci subject` and publishes nothing, so the statuses stay as they were. A rerun of the same run
cannot succeed, because GitHub reruns a run at its original commit. The pull request needs a new
event, a push or an update of its branch, which starts a new generation on the new base.

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
