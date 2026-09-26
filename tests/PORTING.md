# Porting ledger

Where every Pages test file that Quick Skin (QS) and Block Pops (BP) delete when they adopt
mod-base went (SPEC §7.1, §7.2, §7.4), which old cases were deliberately not ported, and the
disposition of every gallery-pipeline feature (SPEC §7.0). `tests/test_porting_ledger.py` checks
this file: every deleted path listed in SPEC §7.1/§7.2 has a row, every kit destination names an
existing test file and test class, and every other row gives a retirement reason.

Destination syntax: `tests/<file>.py::<Class>` is a kit test class; `QS <path>` and `BP <path>` are
tests that stay in (or move within) that mod; `retired: <reason>` ends a row with no destination.

## Deleted test files

| Deleted file | Mod | Destination |
|---|---|---|
| `scripts/release/tests/test_pages_site.py` | QS | `tests/test_build_render.py::QuickSkinSiteTest`, `tests/test_build_render.py::IconTest`, `tests/test_frontend_static.py::PageSafetyTest`, `tests/test_frontend_static.py::ScriptSafetyTest`, `tests/test_frontend_static.py::AccessibilityTest`, `tests/test_build_frontend.py::FrontEndTest`, `tests/test_build_templating.py::RenderTest`, `tests/test_build_templating.py::ThemeTest`, `QS scripts/release/tests/test_mod_base_adapter.py` (capture ids, 17 keys, 360 frames), `QS scripts/pages/mod_base_fixtures.py` (the moved `fixture_png` and `result.json` writer) |
| `scripts/release/tests/test_pages_artifact_rotation.py` | QS | `tests/test_rotate.py::GenerationTests`, `tests/test_rotate.py::RetirementRuleTests`, `tests/test_rotate.py::FamilyLegTests`, `tests/test_rotate.py::BudgetAndDeferralTests`, `tests/test_rotate.py::DeletionBudgetTests`, `tests/test_rotate.py::CommandTests` (unported cases below) |
| `scripts/release/tests/test_pages_selection_api_budget.py` | QS | `tests/test_select.py::QuickSkinSelectionTest`, `tests/test_workflow_api_budget.py::RecoveryInventoryBudgetTest` |
| `scripts/ci/tests/test_pages_publication_progress.py` | QS | `tests/test_admission.py::PublicationPolicyTest`, `tests/test_admission.py::QuickSkinAdmissionTest`, `tests/test_workflow_names_constants.py::JobNameTableTest` (unported case below) |
| `scripts/ci/tests/test_pages_workflow_api_budget.py` | QS | `tests/test_workflow_api_budget.py::ProgressOperationBudgetTest`, `tests/test_workflow_api_budget.py::RecoveryInventoryBudgetTest` |
| `tests/test_pages_atomic.py` | BP | `tests/test_io_atomic.py::AtomicDirectoryTests`, `tests/test_io_atomic.py::WriteNewTests`, `tests/test_io_atomic.py::TraversalTests`, `tests/test_io_atomic.py::ExclusiveRenameHostTests` |
| `tests/test_pages_branch_discovery.py` | BP | `tests/test_targets_enrolled_branches.py::EnrolledBranchesTest`, `tests/test_targets_enrolled_branches.py::FetchInertTest` |
| `tests/test_pages_build_workflow.py` | BP | `tests/test_workflow_publish.py::PublishStructureTests`, `tests/test_workflow_publish.py::PublishShellTests`, `tests/test_managed_caller.py::DeployRecheckExecutionTests` |
| `tests/test_pages_collect_workflow.py` | BP | `tests/test_workflow_publish.py::PublishStructureTests`, `tests/test_workflow_publish.py::PublishShellTests` |
| `tests/test_pages_compact_scope.py` | BP | `tests/test_prepare_validate_compact.py::BlockPopsTest`, `tests/test_prepare_validate_compact.py::CompactTest` |
| `tests/test_pages_compact_selection.py` | BP | `tests/test_prepare_validate_compact.py::CompactTest`, `tests/test_model_documents.py::CompactSelectionBindingTest` |
| `tests/test_pages_discovery_inventory.py` | BP | `tests/test_targets_enrolled_branches.py::EnrolledBranchesTest`, `tests/test_workflow_api_budget.py::EnrolledBranchesBudgetTest` |
| `tests/test_pages_nest_lone_download.py` | BP | retired: `nest_lone_download.py` is retired with the feature; the kit downloads every artifact by id into its own directory (`tests/test_github_artifacts.py::DownloadTests` covers the replacement) |
| `tests/test_pages_newest_source.py` | BP | `tests/test_select.py::BlockPopsNewestSourceTest` |
| `tests/test_pages_publication.py` | BP | `tests/test_authenticate.py::BlockPopsAuthenticationTest`, `tests/test_build_reauthenticate.py::BlockPopsReauthenticationTest` (the checks now repeated at build) |
| `tests/test_pages_raw_scope.py` | BP | `tests/test_prepare_validate_compact.py::BlockPopsTest`, `tests/test_prepare_validate_compact.py::ValidateHandoffTest` |
| `tests/test_pages_refresh_cache.py` | BP | `tests/test_refresh.py::RefreshTest`, `tests/test_refresh.py::PrerequisiteTest`, `tests/test_refresh.py::PromotionTest`, `tests/test_refresh.py::CollectedTest`, `tests/test_refresh.py::DriftTest` (post-validation drift) |
| `tests/test_pages_refresh_workflow.py` | BP | `tests/test_workflow_finalize.py::FinalizeStructureTests`, `tests/test_workflow_finalize.py::FinalizeShellTests` |
| `tests/test_pages_rotation_actions.py` | BP | `tests/test_rotate.py::ReobservationTests`, `tests/test_rotate.py::OwnerAndReplacementTests`, `tests/test_workflow_rotate.py::RotateShellTests` |
| `tests/test_pages_rotation_inputs.py` | BP | `tests/test_rotate.py::ReobservationTests`, `tests/test_workflow_rotate.py::RotateStructureTests`, `tests/test_workflow_rotate.py::RotateShellTests` |
| `tests/test_pages_scoped_cli.py` | BP | `tests/test_prepare_validate_compact.py::CommandLineTest`, `tests/test_cli_registry.py::SurfaceTest` |
| `tests/test_pages_scoped_producer.py` | BP | `tests/test_prepare_validate_compact.py::BlockPopsTest`, `tests/test_prepare_validate_compact.py::PrepareTest`, `tests/test_hook_reverification.py::BlockPopsExpectationRuleTest` |
| `tests/test_pages_site_atomic.py` | BP | `tests/test_build_seal.py::AtomicSiteTest` |
| `tests/test_pages_site_companions.py` | BP | `tests/test_build_current_attempt.py::SelectionTest`, `tests/test_build_current_attempt.py::ArtifactTest`, `tests/test_build_current_attempt.py::InvocationTest` |
| `tests/test_pages_site_output.py` | BP | `tests/test_build_seal.py::SiteOutputTest`, `tests/test_build_render.py::BlockPopsSiteTest` |
| `tests/test_pages_site_scope.py` | BP | `tests/test_build_current_attempt.py::HeadTest`, `tests/test_build_current_attempt.py::JobGraphTest` |
| `tests/test_pages_source_scope.py` | BP | `tests/test_authenticate.py::BlockPopsAuthenticationTest` |
| `tests/test_visual_anchor.py` | BP | `tests/test_anchor.py::BlockPopsAnchorTest`, `tests/test_anchor.py::QuickSkinAnchorTest` |
| `tests/test_visual_anchor_schema2.py` | BP | `tests/test_anchor.py::BlockPopsAnchorTest`, `tests/test_model_documents.py::AnchorExpectationTest` |
| `tests/test_visual_anchor_scoped_producer.py` | BP | `tests/test_anchor.py::BlockPopsAnchorTest`, `tests/test_rotate.py::AnchorTests` |

## Partially moved test files (kept in the mod)

| Kept file | Mod | Destination of the moved part |
|---|---|---|
| `tests/test_content_cache.py` | BP | `tests/test_io_content_cache.py::ContentCacheTests` (the `scripts/lib/content_cache.py` part; BP keeps its copy for non-Pages code) |
| `tests/test_release_evidence_download.py` | BP | `tests/test_github_artifacts.py::DownloadTests`, `tests/test_github_artifacts.py::CredentialSafeTransportTests` (the transport part; BP keeps `scripts/release/artifact_transport.py` for release tooling) |

## Old cases not ported

| Origin | Case | Reason |
|---|---|---|
| QS `test_pages_artifact_rotation.py` | GitHub API retry and pagination | moved to the API client: `tests/test_github_api.py::RetryTests`, `tests/test_github_api.py::PaginationTests` |
| QS `test_pages_artifact_rotation.py` | legacy `pages-cache-<branch>` names and flag aliases | retired: the kit reads and deletes only `mb-` names (SPEC §3.12, feature 47) |
| QS `test_pages_artifact_rotation.py` | ordinary carry-forward of a cache to a descendant head | retired: ordinary evidence is never carried forward (SPEC §3.2 rule 4, feature 46) |
| QS `test_pages_artifact_rotation.py` | head-changed no-op | retired: rotation is its own `operation=rotate` run bound to the owner's exact `head_sha` after `completed/success` (`tests/test_rotate.py::OwnerAndReplacementTests`) |
| QS `test_pages_artifact_rotation.py` | bound on re-validating the kept transients | retired: every transient of the owner is re-validated one by one against its promotion (`tests/test_rotate.py::GenerationTests`) |
| QS `test_pages_artifact_rotation.py` | selection and probe cases | moved to selection: `tests/test_select.py::QuickSkinSelectionTest`, `tests/test_select.py::FamilyCarryForwardTest` |
| BP `test_pages_rotation_inputs.py` | same-run input leases of the in-run rotation | retired: the in-run `rotate-current` and the single lifecycle lock are retired (features 18 and 19); rotation leases are `tests/test_rotate.py::ReobservationTests` |
| QS `test_pages_publication_progress.py` | replay of recorded QS readiness | not ported: it depends on QS fixtures and matrix history; the policy table itself is `tests/test_admission.py::PublicationPolicyTest` |

## Feature ledger (SPEC §7.0)

| # | Feature (origin) | Disposition | Where |
|---|---|---|---|
| 1 | Landing page, `site-data.json`, link and release cards (QS) | CORE, config-driven | SPEC §6, `pages/build.py` |
| 2 | Gallery tabs, filters, search, compare grid, validation dialog (QS) | CORE | `site/e2e`, `gallery.js` |
| 3 | BP 3-file gallery with branch/loader/capture filters | RETIRED, superseded by #2 | The QS UI covers the loader filter and capture search; the branch filter becomes release tabs plus a Minecraft filter |
| 4 | Optional-mod compatibility gallery: pairs, not-applicable, review proofs (QS) | CORE family `paired` + HOOK `family_validate` | SPEC §3.5, §4.5 |
| 5 | Compatibility contract drift means unavailable, exit 3 (QS) | CORE `superseded` status | SPEC §3.5 |
| 6 | Compatibility `coverage_sha` carry-forward behind a non-impact proof (QS) | HOOK (impact) + CORE ancestry re-proof R5 | SPEC §4.4 |
| 7 | 2/5/7-checkpoint compatibility product and `reviewed_frame_count` (QS) | Native (QS adapter) to `review.reviewed_frame_count` | SPEC §4.5 |
| 8 | Selected/composed feature evidence, schemas 5/6/7 (QS) | CORE `scope`/`epoch`/`tested` (per frame; a partially re-captured lane records `baseline_run`, v0.9.2) + HOOK `compose`/`verify_publication` + CORE R3 | SPEC §3.3, §4.4 |
| 9 | PR runtime reuse, `runtime_source`/`ci_reuse` (QS) | CORE `reuse:"delegated"` + HOOK `authenticate_extensions` | SPEC §4.8 |
| 10 | Attested reuse, BP `attest_run_id` / `Verify exact tested tree` | CORE `reuse:"attested"`, exact job name | SPEC §4.8 |
| 11 | `pages-full-baseline-*` archives (QS) | CORE `baseline_archive` to `mb-baseline--` | SPEC §5.4 |
| 12 | Publication-progress cost admission: coalesce, halfway/final, 45-minute deadline, 3-failure stop (QS) | CORE `admission.mode:"progress"` | SPEC §5.3.1 |
| 13 | Wake coalescing, stale-wake exit 0, active-producer deferral, "all caches current" exit (QS) | CORE `admit` + config flags | SPEC §5.3.1 |
| 14 | Hourly (QS) / monthly (BP) recovery cron | CORE managed caller, hourly for both | D17 |
| 15 | API retry, backoff, rate-limit detection, budget telemetry (QS) | CORE `github.api`, `budget`, `tools/github_api_retry.sh` | SPEC §2 |
| 16 | Exact-ID rotation after success, deletion budget, deferral, family generations (QS) | CORE `rotate` | SPEC §5.5 |
| 17 | Rotation pinned-read leases and per-delete re-observation (BP) | CORE `rotate` | SPEC §5.5 |
| 18 | In-run `rotate-current` (BP) | RETIRED | Proving `completed/success` is stronger, and a separate lock cannot be starved (D11) |
| 19 | Single lifecycle lock (BP) | RETIRED, replaced by two locks | Same reason as #18 |
| 20 | `workflow_run` Pages trigger (BP) | RETIRED as a Pages trigger; a signal in `notify-pages.yml` | D4: producers wake Pages by `workflow_dispatch` |
| 21 | `pages-deploy`/`pages-rotate` dispatch types (BP) | RETIRED, replaced by `operation=manual/rotate` | Same capability, authenticated identically |
| 22 | QS `repository_dispatch` wakes and the wake jobs | RETIRED, replaced by `notify-pages` `workflow_dispatch` | Removes `contents: write` |
| 23 | `implementation_sha` resolved from the live default head (BP) | RETIRED, replaced by `github.sha` + mandatory live-head equality | Strictly stronger |
| 24 | Head rechecks at discover/collect/build x2/deploy/refresh (QS) | CORE (deploy now retried) | SPEC §5.2, §5.3 |
| 25 | `authenticate_source.py`: attempt endpoints, `display_title`, job-graph hash, controller split (BP) | CORE `pages.authenticate` + HOOK `expected_source_jobs` | SPEC §4.8 |
| 26 | Re-encode binding, byte-identical WebP (BP `_bind_compact`) | CORE `validate --bind-raw` | SPEC §3.3 |
| 27 | `pages-selection-*` companions re-verified in build (BP) | CORE `selection.json` embedded, byte-equal recompute | SPEC §3.6 |
| 28 | Protected reconstruction of the raw manifest from `result.json` (BP `validate_raw`) | CORE R1 via HOOK `collect`, now also for QS | SPEC §4.4 |
| 29 | Explicit `files[]` inventory and exact directory equality (BP) | CORE, every kind | SPEC §3 |
| 30 | `scripts/lib` `secure_json`/`atomic_directory`/`content_cache`, sealed output (BP) | CORE `io` (BP keeps its own copy for non-Pages code) | SPEC §2 |
| 31 | `download_artifact.py`: bounded ZIP, credential-safe redirect (BP) | CORE `github.artifacts`; BP keeps `scripts/release/artifact_transport.py` for release tooling | SPEC §7.2 |
| 32 | Opaque `branch_token` keys + multi-branch enrollment (BP) | CORE `targets.mode:"enrolled-branches"` + HOOK `targets` | SPEC §4.6; kept, only `master` enrollable today |
| 33 | Scoped aggregates legacy/full/lane + `pr-anchors`/`scheduled-anchors` (BP) | HOOK `expectation` + `scope.detail` + extension | SPEC §3.11 |
| 34 | Matrix embedded in compact bundles (BP) | RETIRED, replaced by the embedded expectation + `matrix_sha256` + build-time re-derivation | R2 is stronger |
| 35 | `nest_lone_download.py` (BP) | RETIRED | Per-artifact download by id |
| 36 | `refresh_cache.py` current-attempt revalidation (BP) | CORE `refresh` | SPEC §5.4 |
| 37 | `GITHUB_WORKFLOW_REF` exact check (BP) | CORE prologue, now also for QS | SPEC §1.2 |
| 38 | Per-step token hygiene (BP) | CORE composites and callee steps | SPEC §1.4 |
| 39 | Lossless anchor: BP `visual_anchor.py` (8-day grace) and QS 90-day raw handoff | CORE `evidence.anchor` (`mb-anchor--`) | SPEC §3.4 |
| 40 | Per-frame `runtime_evidence` (QS; BP had the data) | CORE, mandatory, at most 4096 characters | SPEC §3.0 |
| 41 | Source and derivative pixel metrics; comparisons re-measured (QS); BP comparisons validated but not stored | CORE, now also for BP | SPEC §3.2, §3.3 |
| 42 | `--expected-bundles-json` exact key set (QS) | CORE `build` step 3 | SPEC §5.3.2 |
| 43 | Content-addressed images, 1 GiB cap, `.nojekyll`, relative URLs (both) | CORE | SPEC §6.1 |
| 44 | Escaped DOM, local assets, CSP, URL guards (both) | CORE, tightened | SPEC §6.3 |
| 45 | `request-feature-coverage` after cache retention (QS) | Mod-local extension job `ext-feature-coverage` | SPEC §5.7 |
| 46 | `--allow-continuation` and ordinary carry-forward (QS, schema 2 only) | RETIRED | Already refused with `--bundle-key` |
| 47 | Legacy readers: evidence 1-7, compatibility 1-5, `pages-cache-<branch>`, flag aliases (QS) | RETIRED | New namespace plus forced regeneration (SPEC §3.12) |
| 48 | Historical visual-review reference comparison consuming `pages-e2e-*` (QS) | RETIRED, fail-closed; recovery from tag `pre-mod-base-gallery` | Unreachable under schema 3 and already broken (V12) |
| 49 | Workflow-shell unit tests that run the real `run:` blocks with stubs (BP) | CORE kit test style | SPEC §9.1 |
| 50 | BP light theme | CORE `theme.light` | SPEC §6.2 |
| 51 | Display title binding `Packaged E2E / <commit>` (BP) | CORE config `source.display_title` | SPEC §4.8 |
| 52 | Loader display names (QS map) and scenario/role/tier labels | CORE config `labels` | SPEC §4.1 |
| 53 | `node --check` of the site JS in Build (QS) | Kit CI; removed from QS Build | SPEC §5.10 |
| 54 | `evidence_target` `mc<ver>` inventory + `artifact_pattern` (QS) | Stays in QS; `validate_handoffs` and `raw_retention_days` move into the kit | SPEC §7.1 |
