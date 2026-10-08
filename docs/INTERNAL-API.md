# Internal API

Produced by introspecting the frozen MB0 scaffolding (`src/mod_base`); `tests/test_internal_api.py`
fails if anything listed here disappears or changes its parameters. This is the one reference every parallel
unit (MB1-MB10) codes against: **the names, parameters, defaults, return types and exception classes
below are frozen**. A unit fills in the bodies of the modules it owns (every stub body raises
`NotImplementedError("owned by MBn")`); it may add private helpers and new private modules, but
it must not rename, remove or change the signature of anything listed here. A needed change is
announced to MB0's owner first (SPEC §10). A name one unit uses from a module another unit owns is
listed here (and so frozen) before it is used: the test also runs in the other direction and fails
when a kit module or kit tool imports, or reads as a module attribute, another unit's name that its
module section does not list, or any private (`_name`) module or name of another unit.

Conventions shared by every entry point:

* Every rejection raises `mod_base.errors.MbError` (exit 2) or a subclass: `Unavailable` (exit 3,
  "no admissible evidence"), `Superseded` (exit 3, reason `superseded`), `ControllerSkew` (exit 78).
  Unit-specific error classes subclass `MbError`. Only `mod_base.errors.run_main` turns them into
  exit codes and one bounded stderr line.
* Entry points receive a `mod_base.runtime.Invocation` (repo root, validated `Config`, kit root and
  a frozen snapshot of the GitHub environment) and never read `os.environ` themselves.
* Documents are plain JSON-compatible dicts validated by `mod_base.model.documents`; written
  documents are `mod_base.model.canonical.canonical_json` bytes (sorted keys, compact separators,
  UTF-8, trailing newline); their SHA-256 is the SHA-256 of those bytes.
* Bounds come from `mod_base.model.limits`: every bound of a document, bundle, artifact, image or
  API budget, and every bound more than one module enforces. A module may keep a private operational
  constant (a retry count, a backoff, a timeout, the read cap of a file only it reads); one another
  unit uses is frozen in that module's section, and a same-named copy of a `limits` bound must hold
  the same value (`tests/test_model_limits.py`). Artifact names come only from
  `mod_base.model.grammar`; job and step names only from `mod_base.workflow`.
* Pillow is imported only inside `mod_base.imaging` (lazily, inside functions).
* `mod_base.model.documents` also exports its field validators (listed in its section): each is a
  `(value, path) -> value` callable raising `DocumentError`; reuse them instead of new regexes.
* Entry formats: `* `def f(...)`` and `* `class C`` (with its members indented) are callables and
  classes whose parameters are checked; `* `NAME = value`` and `* `NAME`: ...` are module-level
  names; `* `A`, `B`, `C`` lists several module-level names.

## Amendments announced by MB0

Changes to the frozen interface after the first MB0 review (every consumer codes against these):

* **Two-phase selection.** `authenticate` writes a selection *draft*
  (`documents.validate_selection(draft=True)`: no `manifest_sha256`, `binding`, `composition`);
  `compact` (or `compose` for a `selected` handoff) completes it and embeds it; published bundles
  satisfy `documents.check_compact_selection`. `manifest_sha256` is
  `documents.compact_identity_sha256` (the manifest without its `selection` member and
  `selection.json` record). `kit_binding.sha` is the selected artifact's kit, never compared with
  the executing `kit`. See `docs/SCHEMAS.md` "The collect flow".
* **CLI:** `compose` takes `--selection F` (the draft); `select` takes an optional `--output F`
  (the `Selected` JSON object, keys `pages.select.SELECTED_KEYS`); `admit` also outputs
  `subjects` and every `families` entry carries `coverage_sha`.
* **Composed flow:** `compose_selected(..., selection_path, ...)`; the core's selected compaction is
  a `validate_compact(intermediate=True)` bundle handed to the `compose` hook.
* **Promotion draft:** `verify_publication` receives `validate_promotion(draft=True)` (no `site`).
* **Runs:** `github.runs.run_record(run, claim, *, require_controller_head=True)`; `False` only for a
  `delegated` tested run.
* **Build/refresh data flow:** `build` downloads this run's collected artifacts itself into the new
  `--collected`/`--families` directories; `refresh` downloads the promotion and the collected
  artifact itself and writes the exact upload bytes into the new `--input` directory; a family
  cache is the collected family artifact's `source/` (`family.paired.SOURCE_DIRECTORY`).
* **Gallery families:** one entry per family with per-key `releases` (`coverage_sha`, `contracts`,
  `links`, `image_policy` per available key) and `key` on every family lane and not-applicable entry.
* **Conformance seams:** `imaging.png.pattern_png` is the `protocol.ImageFactory`;
  `adapter.host_child.run_hook` is the in-process dispatch (with a `FakeGitHub` context);
  `adapter.host.adapter_pythonpath` is the child `PYTHONPATH`; `conformance.run.main` is the
  simulation child; `FakeGitHub` gains `add_commit`, `add_tree`, `add_blob`, `add_ref`, `add_response`.
* **Anchors:** a non-null `anchor_selection` result equals `expectation.anchor`; `validate_anchor`
  with an expectation requires that declared anchor.
* **Text and grammar:** strict JSON refuses lone surrogates; `canonical_json` refuses non-`str` keys;
  display text uses the frozen Unicode 3.2 table; `REPOSITORY` never admits `.`/`..` components.

Integration-round amendments:

* **Cross-unit names.** Every name one unit uses from another is now listed under its module and
  frozen (each section's "Frozen for other units" block): MB0's `cli` argument types and
  `COMMANDS`, `workflow.CALLER` and its sibling tables, every `protocol` name, the `documents`
  validators and field tables, the `grammar` patterns and every `limits` bound; MB1's
  `jobs.actions_time`, `bounded_zip.LIMITS_BY_KIND` and `archive_limit`; MB3's `validate.Bundle`,
  `load_handoff`, `load_compact`, `check_handoff`, `check_compact_pixels`, `check_extensions_verified`,
  `compact.rederive`, `read_draft`, `bind_draft`, `finalize`, `compose.authenticate_baseline`,
  `anchor.eligible_nodes`, `expectation.tested_run_projection`, `require_rederived_for_runs` and
  `host.check_placement`, `placement`, `MAX_CHILD_OUTPUT_BYTES`; MB4's
  `envelope.MAX_NATIVE_FILE_BYTES`; MB1's `artifacts.MAX_ARCHIVE_BYTES`; MB5's shared `select`
  (`SourceRuns`, `FamilyGenerations` and the run predicates) and `targets` helpers and
  `authenticate.write_new_file`; MB6's `build.current_implementation`, `require_current_run`,
  `check_checkouts`, `BuildError` and `templating.theme_color`; MB9's `pin` and `template.tool`
  helpers and the new module `mod_base.template.lock`.
* **Bounds moved into `limits`.** The unit-local bounds live in `mod_base.model.limits` with
  unchanged values: `MAX_CANDIDATES` (8, `select`/`admit`), `MAX_SUBJECT_RUNS` (100),
  `MAX_CANONICAL_RUNS` (300), `MAX_FAMILY_LEGS` (256), `MAX_WORKFLOW_FILE_BYTES` (1 MiB),
  `MAX_SELECTED_JSON_BYTES` (64 KiB), `GENERATION_PROBES` (32) and `ANCHOR_PROBES` (16). Their users
  (`select`, `admission`, `authenticate`, `build`, `rotate`) read them from there; only a documented
  alias remains (`pages.admission.MAX_CANDIDATES`, `MAX_REQUESTS`), holding the same value
  (`tests/test_model_limits.py`). `MAX_CACHE_OWNERS` is retired: `build` no longer walks family
  cache owners (the recorded selection below).
* **Collected-family limits and the family byte budget.** MB4's collected layout (`paired.json`,
  `selected.json`, `images/` and `source/`, the family handoff or cache verbatim with native files of
  any suffix) widened `bounded_zip.LIMITS_BY_KIND["collected-family"]` beyond every other kind. Its
  ceilings are named in `limits`: `MAX_COLLECTED_FAMILY_FILES` = 2 x `MAX_FAMILY_FILES` + 3 = 16387
  entries (no suffix restriction, each entry at most `MAX_SOURCE_PNG_BYTES`) and
  `MAX_COLLECTED_FAMILY_BYTES` = `MAX_RAW_BUNDLE_BYTES` (1 GiB) expanded, because the artifact is
  downloaded as one archive. `MAX_ZIP_ENTRIES` is the largest kind bound (those 16387 entries) and
  `bounded_zip` refuses any `ExtractionLimits` above it. The family bounds partition that 1 GiB
  exactly (`tests/test_model_limits.py` `FamilyBudgetTest`, `docs/SCHEMAS.md`): the projection images
  get the new `MAX_FAMILY_PROJECTION_BYTES` = `MAX_COMPACT_BUNDLE_BYTES` (256 MiB; they were bounded
  by `MAX_FAMILY_HANDOFF_BYTES` before), and `MAX_FAMILY_HANDOFF_BYTES`, the config ceiling of
  `families[].handoff_max_bytes` (the native bundle), is what remains after them, `MAX_PAIRED_BYTES`,
  `MAX_SELECTED_JSON_BYTES` and `MAX_ENVELOPE_BYTES`: 784,269,312 bytes instead of 1 GiB. The new
  `MAX_FAMILY_BUNDLE_BYTES` = `MAX_FAMILY_HANDOFF_BYTES` + `MAX_ENVELOPE_BYTES` is a family handoff or
  cache (the `family-handoff`/`family-cache` extraction total, which used to omit the envelope). So
  every generation `family envelope` accepts is collectable: `family collect` never refuses (exit 2,
  which blocks every publication) a generation its producer accepted.
* **Artifact archive cap (SPEC §3.0 "Raw bundle 1 GiB").** Every SPEC §3.0 expanded bound keeps its
  value (`MAX_RAW_BUNDLE_BYTES`, `MAX_ANCHOR_BUNDLE_BYTES`, `MAX_COLLECTED_FAMILY_BYTES`: 1 GiB; a
  family bundle is smaller, above). The archive of a bundle is larger than its files, so the cap on
  the ZIP bytes admitted, selected and downloaded into memory is `limits.MAX_ARTIFACT_BYTES` =
  `MAX_RAW_BUNDLE_BYTES` + `MAX_ARCHIVE_OVERHEAD_BYTES` (1 GiB + 32 MiB), and
  `bounded_zip.archive_limit(kind) - max_total_bytes <= MAX_ARCHIVE_OVERHEAD_BYTES` for every kind
  (`tests/test_model_limits.py`): a producer within its expanded bound uploads an archive every
  consumer accepts. An artifact size a consumer accepts (a listed or nominated upload's
  `Artifact.size`, `Selected.size`, `selected_artifact.size`, a compact bundle's
  `source_artifact.size`) is bounded by the new `bounded_zip.artifact_limit(kind, *,
  max_total_bytes=None)` (the kind's `archive_limit`), never by an expanded total. For a family
  handoff or cache the new `select.family_archive_limit(family)` narrows it to the family's
  `handoff_max_bytes` plus `envelope.json`: in `admit`, in `select` (a family handoff over it is
  skipped, a family cache over it fails selection like an ordinary cache) and in `build` (either kind
  of a leg's recorded selection); rotation reads family caches within the `family-cache` kind's
  limit. This replaces SPEC §5.3.1's `size <= 1 GiB` (`deploy`) and `size <= handoff_max_bytes`
  (`family`). The download caps `github.artifacts.MAX_ARCHIVE_BYTES` and `github.api.MAX_DOWNLOAD_BYTES`
  alias `MAX_ARTIFACT_BYTES`.
* **Skipped family job.** For a mod without families `admit` outputs `families == []`, so the
  `publish.yml` `family` matrix job is skipped by its job-level `if` before its matrix expands and the
  jobs API reports it once under its unexpanded name, `Publish / Collect ${{ matrix.family }} ${{
  matrix.key }}` (Quick Skin `e2e_job_graph.UNEXPANDED_SCENARIO_JOB`). The new
  `workflow.unexpanded_api_job_name(workflow, job)` names it; `build` accepts that one job (or its
  absence) only when the publication has no family leg and only `completed/skipped`, and every other
  `Publish / Collect ...` job outside the publication still fails closed (reason `job-graph`). The conformance
  simulation emits it (and finalize's skipped `refresh_family`) for a mod without families.
* **Rotation budget (SPEC §5.5).** `limits.DELETION_BUDGET` is 64, not 32: a Quick Skin generation
  supersedes about 35 long-lived artifacts (17 caches, 17 family caches and an anchor), so 32 could
  never drain the leftovers an earlier rotation deferred. The deletion delay, exact-ID deletion and
  every owner and replacement check are unchanged; `rotate` reads `promotion.json` as canonical bytes.
* **Staged-file lock (SPEC §1.5 amendment).** kit-digest-v1 and the kit stamp cover `src/`, `site/`
  and `requirements/`, but `stage` also copies `template/` and `tools/` (and, from v0.9.2, `actions/`)
  into the Block Pops overlay and `template check` reads the overlay's `template/`. So the digested
  `src/` carries `pin.STAGED_LOCK` (`src/mod_base/template/staged_files.sha256`): the
  kit-digest-v1-format listing of every `template/` and `tools/` file (`pin.staged_listing`), and,
  from v0.9.2, `pin.ACTIONS_LOCK` (`src/mod_base/template/staged_actions.sha256`), the listing of
  every `actions/` file (`pin.actions_listing`). Kit resolution verifies the digest first, then
  refuses an overlay whose `template/` or `tools/` differ from the first listing or whose `actions/`,
  when present, differs from the second (`pin.verify_staged_files`, reason `kit-digest`). After any
  change below `actions/`, `template/` or `tools/`, run `python3 -m mod_base.template.lock --write`,
  then refresh the tree-digest literal (`tools/update_tree_digest.py --write`), because the locks
  live in `src/`.
* **Command outputs** (beyond the SPEC §2.2 table): `anchor identity` prints
  `canonical_json({eligible, name})` on stdout (`name` is `null` when not eligible) and writes
  `anchor_eligible` and `anchor_name` (empty when not eligible) to `$GITHUB_OUTPUT`; `family collect`
  writes `status` (`available`, `superseded` or `unavailable`) and `available` (`true`/`false`) before
  it exits 3 for an absence, and then writes no output directory; `refresh` writes `available`,
  `cache_name` (empty when not available) and `baseline_name` only when a baseline is retained;
  `prepare` writes `anchor_eligible`; `build` writes `heads` (one line of canonical JSON) and
  `site_sha256` (`promotion.site.inventory_sha256`).
* **Adapter contracts.** `docs/ADAPTER.md` and `docs/SCHEMAS.md` record the implemented rules every
  adapter must follow: the `tested_run` projection and the `expectation-drift` check, runtime
  `HOOK_JOBS` placement, the child output cap and call layout, the `ctx.api` read budget, anchor
  eligibility, `Unavailable` for an unenrolled subject, the handoff re-derivation inside the
  producing run only, the `family_validate` obligations, compose's full-use rule and canonical
  bytes for every kit JSON file.
* **Family generations.** `select --family` walks a bounded first-parent history below the leg
  coverage for a `carry_forward` family (`select.FamilyGenerations`, at most
  `limits.GENERATION_PROBES` commits and cache owners), prefers a family cache that supersedes the
  family handoff at the same commit, and binds a selected family handoff to its kit before
  collection (`authenticate.kit_binding`, reason `kit-binding`). `authenticate.kit_binding` with
  `selected_kind` `family-handoff` or `family-cache` takes the envelope as `manifest` and its
  producer run as `owner_run` (never a cache's Pages owner) and binds the pin of
  `families[].producer.workflow` at that run's head; `build` applies it to every collected leg after
  authenticating the producer attempt and its run record. Rotation keeps a carried leg's cache
  although its envelope coverage differs from its name (SCHEMAS.md "The family generation chain").
* **Recorded family selection (CLI amendment to SPEC §2.2).** `family collect` takes the required
  `--selected-json F`: the canonical `Selected` object `select --family --output F` wrote
  (`collect_family(..., selected_json)`). It binds that selection to its input before the adapter
  runs (a `family-handoff` or `family-cache` of this family and key; a handoff is the envelope
  producer attempt's upload; a cache is named by a commit between the envelope's coverage and the
  leg coverage; `envelope.kit` is the pin of `producer.workflow` at `producer.commit`, read from the
  inert object store) and writes those bytes as `family.paired.SELECTED_NAME` (`selected.json`) inside
  its atomic output. `build` re-authenticates that one artifact by id (`build.FAMILY_SELECTED_NAME`,
  an alias of `SELECTED_NAME`; `build.collected_family_selection`) instead of repeating the walk, so
  its reads never grow with the walk's depth; it uses `FamilyGenerations` only for `history`.
  `refresh-family` requires the recorded id to be the promotion's `selected_artifact_id`. The
  `publish.yml` family job passes `--selected-json "$RUNNER_TEMP/mb/selected.json"`, the file its
  `select` step wrote.
* **Templating.** `templating.PLACEHOLDERS` gains `color_scheme`, the `<meta name="color-scheme">`
  value: `dark` for a dark-only theme, `dark light` when `theme.light` is set (SPEC §6.2).
* **Consistent listings (v0.9.1).** GitHub's listings are eventually consistent while the listed run
  still uploads or a rotation deletes artifacts: the canary's `Finalize / Refresh evidence cache` job
  failed closed on `listing total_count 6 disagrees with 5 listed rows` while its sibling finalize
  jobs uploaded their caches. A snapshot whose rows disagree with `total_count`, whose `total_count`
  changes between pages or that repeats a row is now the new `github.api.InconsistentListing`, and
  the new `github.api.read_consistently` discards it and reads the whole listing again from page 1:
  at most `limits.LISTING_READ_ATTEMPTS` (4) reads, waiting `listing_retry_delay` (2, 4 and 8 s:
  `LISTING_RETRY_DELAY_SECONDS` doubling to `MAX_LISTING_RETRY_DELAY_SECONDS`) in between, every read
  spending the client's request budget; only the last inconsistent snapshot fails closed, and an
  incomplete listing is never returned. Only a short page ends a listing: a full page that reaches
  `total_count` is confirmed by the next (empty) page, so a `total_count` lagging behind the rows
  hides none of them (one more read, only when the rows are an exact multiple of 100; `admit`'s
  one-page active source-run inventory likewise reads page 2 after a full page). A truncated
  `workflow_runs` read (more runs than `max_items`, or than the `limits.MAX_FILTERED_RUNS_LISTED`
  (1,000) newest runs GitHub lists for a filtered search) must list exactly that many rows.
  `GitHubApi.paginate` (and the fake's) applies the retry to every paginated listing; `runs.workflow_runs` and `admit`'s active source-run inventory go through the new
  `GitHubApi.read_listing` / `FakeGitHub.read_listing`. Callers that need only some names of a run
  that may still be uploading list them by exact name through the new
  `artifacts.list_run_named(api, run_id, name, *, max_items=512)`: `refresh` (the promotion and its
  collected artifact) and `select`'s handoff of a source run. `build` (its own run's full inventory,
  read before it uploads anything), `admit`, rotation and the family walk keep whole-run inventories
  of settled runs, where one listing serves many names. `FakeGitHub` gains an optional `sleep`
  (recorded in `sleeps`, never slept by default) and the seams `skew_listing` and `during_listing`;
  the conformance simulation skews every refresh's first listing after a sibling's upload.
* **Pre-1.0 adoption fixes (v0.9.2).** Found while migrating Quick Skin and Block Pops at v0.9.0/v0.9.1:
  * *Partially re-captured lanes.* R3 refused a composed lane holding frames of both epochs, so no
    real Quick Skin selective generation (hud-preview re-captures 2 of 63 `full` checkpoints) could
    be composed. A composed bundle is now composed per frame, faithfully to Quick Skin's schema-7
    view: a re-tested lane is the selected source's lane record (a selection runs every role of a
    lane it re-tests) and, when it still holds baseline frames, records the baseline execution as
    the new optional composed-only field `lanes[].baseline_run` (and the gallery lane likewise,
    `baseline_run: {status, elapsed_s?, jars}`, which the validation record shows for a baseline
    frame); `documents.validate_compact` checks the epoch consistency of every composed bundle
    (`SCHEMAS.md`, "Composed lanes and epochs"), and
    `compose.verify_composition` its sources. Only optional fields are added (N/N-1 holds: every
    v0.9.0/v0.9.1 composed bundle stays valid); the private `compose._lane_epochs` is gone.
  * *Conformance seeding API.* The extension fixtures (`delegated_extensions`,
    `selected_extensions`) receive the new `conformance._fixture_api.FixtureGitHub` as `ctx.api`:
    the simulated GitHub's read surface plus typed, bounded `add_artifact` (ZIP bytes),
    `add_run`, `add_jobs` and `add_response` (`ADAPTER.md`, "Optional conformance fixtures"); the
    `responses` return form goes through the same `add_response` rules. The report's `site` gains
    `composed_lanes`. The fixture signatures are unchanged.
  * *Conformance scratch.* `run_conformance` no longer uses `TemporaryDirectory`, whose cleanup error
    replaced the simulation's real one: a cleanup failure after an error is dropped, after a success
    it is an `MbError` (read-only trees are made writable first).
  * *Staged set (SPEC §1.5 amendment).* `stage` also copies `actions/`, so a mod's gate can check the
    pinned composites, bound by a lock of its own, `pin.ACTIONS_LOCK` (`pin.ACTIONS_DIR`,
    `pin.actions_listing`); `pin.STAGED_LOCK` and `pin.LOCKED_DIRS` are unchanged. A second lock, not a
    longer first one, keeps SPEC §1.5's controller upgrade working in both directions: a bootstrap
    older than v0.9.2 requires `template/` and `tools/` to equal `STAGED_LOCK` byte for byte and stages
    no `actions/`, so it still stages a v0.9.2 candidate kit (whose own verification accepts an overlay
    without `actions/`), and a v0.9.2 bootstrap stages a candidate pinned back to an older kit, which
    carries no `ACTIONS_LOCK`, without `actions/`. An `actions/` no lock binds is refused. The managed
    bootstrap changes accordingly (`ACTIONS_DIR`, `ACTIONS_LOCK`, `STAGED_DIRS`, `actions_listing`,
    `verify_staged_files`, `copy_kit`); `mod_base.template.lock` maintains both locks (`LOCKS`, `stale`,
    and `recorded` takes the lock path) and `tools/update_tree_digest.py` gates both.
  * *Line endings.* The managed `.gitattributes` pins `text eol=lf` for every managed and fragment path,
    so a `core.autocrlf=true` checkout passes `template check`, which stays byte-exact and reports CRLF
    line endings with `tool.CRLF_ADVICE` (plus the diff of the LF form when that still differs);
    `template sync --write`, and so `bump`, rewrites a CRLF managed file or caller with LF, keeping the
    caller's extension region.
* **Quick Skin adoption fixes (v1.0.1).** Found while Quick Skin adopted `v1.0.0`:
  * *Dependabot rule.* `template/manifest.json` (`mod-base.template-manifest` v1) gains the optional
    field `ignore_actions_of`: only on the `.github/dependabot.yml` fragment, 1..8 managed workflows
    of the same manifest (`documents.validate_template_manifest`). The new
    `template.tool.pinned_actions(kit_root, manifest, entry)` derives from the kit's template every
    third-party action pinned in the managed region of those workflows (the kit's own references
    excluded), and `template check` requires every `github-actions` update to ignore each of them,
    beside `The-Plum-Team/mod-base*`, with no `versions` or `update-types`; the private
    `_ignores_kit`/`_flow_entry_ignores_kit` became `_ignored`/`_flow_entry_name`. The seeded
    `.github/dependabot.yml` ignores `actions/deploy-pages`.
  * *Selected variant.* The conformance `selected` variant runs last, on a new head one commit after
    the newest baseline the simulation retained for the key (`_simulation.Simulation.latest_baselines`),
    adding the fixtures module's new optional `SELECTED_CHANGE` (`_simulation.SELECTED_CHANGE`,
    default `SELECTED_DOCUMENT`); `_generations.Generations.push` now goes through the new
    `commit_on_head(relative, data, *, label)`. `_fixture_api.FixtureGitHub(..., baselines=None)`
    and its new `retained_baseline(key)` serve `selected_extensions` only
    (`Simulation.baseline_provider`: the simulated key's own newest baseline, or a stand-in retained
    by a successful Pages run for a declared key outside `--keys`); `Simulation.fixture_extensions`
    takes that provider as a fourth positional argument. `_hooks.InProcessHooks.refusals` records
    the hook of every `HookFailed`, so the forged-baseline sub-checks accept a refusal by `compose`
    (never by another hook) as well as R3's; both forged baselines are uploaded at the baseline's
    commit (by a successful source run, and by a successful Pages run outside its retention step),
    so each differs from the genuine one only in its owner's workflow or upload window.
    `_world.World` jobs carry their run's `head_sha` and `head_branch`, as GitHub's jobs API does.
    `docs/ADAPTER.md` records the contract ("The selected head", the `SELECTED_CHANGE` row and the
    `retained_baseline` seeder).
  * *Delegated variant.* Its tested claim takes `run_id`, `run_attempt`, `branch` and `commit` from
    the tested run (`conformance/reused-pull-request` at its `head_sha`); the controller stays the
    handoff run's.
* **User-site Pillow (v1.0.2).** Block Pops' credentialless candidate sandbox installs the
  hash-locked Pillow with `pip install --user`, and its `conformance` run failed (run 36239090095,
  `hook-failed: adapter hook 'synthesize' failed: ModuleNotFoundError: No module named 'PIL'`):
  both isolated children, the hook child (`host._environment`) and the simulation child
  (`conformance.run._child_environment`), get a private `HOME` and `PYTHONNOUSERSITE=1`, and the
  managed bootstrap's `run` starts the kit itself with `PYTHONNOUSERSITE=1`. The new public
  `adapter.host.imaging_user_site() -> dict[str, str]` returns `{"PYTHONUSERBASE": site.getuserbase()}`
  only when this process has its user site enabled (`site.ENABLE_USER_SITE is True`,
  `sys.flags.no_user_site == 0`), `importlib.util.find_spec("PIL")` (never an import) is a regular
  package whose parent directory equals, after `os.path.realpath`, this process's
  `site.getusersitepackages()`, and the user base is an absolute, existing directory without `:` or
  a control character; otherwise `{}`. For a non-empty result both child builders omit
  `PYTHONNOUSERSITE` and set `PYTHONUSERBASE` in its place (the child's user site is the parent's,
  after the standard library; `PYTHONPATH` is never extended); for `{}` both environments are
  byte-for-byte the v1.0.1 ones. `conformance.run` (MB10) uses the name, so it is frozen under the
  host's section. The managed bootstrap gains the same rule as its own stdlib copy
  (`imaging_user_site`, `IMAGING_PACKAGE`), which `kit_environment` applies to the kit process,
  whose disabled user site would otherwise hide Pillow from the host before it could pass it on;
  `tests/test_pin.py` runs both copies over the same process states (`tests/user_site.py`).
* **Checked document.** `tests/test_internal_api.py` also checks every class's documented dataclass
  `fields` (names, order and each default's `repr`, `<factory>` for a default factory) and
  properties, every name of a described name list, and every documented constant value written in
  full (an abbreviated value ends with `...`); an entry line it cannot read fails the test instead of
  being skipped.

## Ownership

| Unit | Modules |
|---|---|
| MB0 | `mod_base`, `mod_base.__main__`, `mod_base.errors`, `mod_base.cli`, `mod_base.runtime`, `mod_base.config`, `mod_base.workflow`, `mod_base.model.grammar`, `mod_base.model.limits`, `mod_base.model.canonical`, `mod_base.model.validators`, `mod_base.model.documents`, `mod_base.adapter.protocol` |
| MB1 | `mod_base.io.secure_json`, `mod_base.io.atomic_directory`, `mod_base.io.content_cache`, `mod_base.io.seal`, `mod_base.io.tree`, `mod_base.io.bounded_zip`, `mod_base.github.api`, `mod_base.github.runs`, `mod_base.github.jobs`, `mod_base.github.artifacts`, `mod_base.github.contents`, `mod_base.github.fake`, `mod_base.github.commands` |
| MB2 | `mod_base.imaging.metrics`, `mod_base.imaging.compare`, `mod_base.imaging.png`, `mod_base.imaging.webp` |
| MB3 | `mod_base.adapter.api`, `mod_base.adapter.host`, `mod_base.adapter.host_child`, `mod_base.evidence.expectation`, `mod_base.evidence.prepare`, `mod_base.evidence.validate`, `mod_base.evidence.compact`, `mod_base.evidence.compose`, `mod_base.evidence.anchor`, `mod_base.evidence.commands` |
| MB4 | `mod_base.family.envelope`, `mod_base.family.paired`, `mod_base.family.commands` |
| MB5 | `mod_base.pages.targets`, `mod_base.pages.admission`, `mod_base.pages.select`, `mod_base.pages.authenticate`, `mod_base.pages.commands_control` |
| MB6 | `mod_base.pages.build`, `mod_base.pages.templating`, `mod_base.pages.refresh`, `mod_base.pages.commands_build` |
| MB7 | `mod_base.pages.rotate`, `mod_base.pages.commands_rotate` |
| MB9 | `mod_base.pin`, `mod_base.pin_commands`, `mod_base.template.tool`, `mod_base.template.commands`, `mod_base.template.lock` |
| MB10 | `mod_base.conformance.run`, `mod_base.conformance.commands` |
| MB11 | `mod_base.build_ci.adapter`, `mod_base.build_ci.identity`, `mod_base.build_ci.planning`, `mod_base.build_ci.commands`, `mod_base.build_ci.commands_subject`, `mod_base.build_ci.lifecycle`, `mod_base.build_ci.commands_worker` |
| MB11 | `mod_base.build_ci.protocol`, `mod_base.build_ci.graph`, `mod_base.build_ci.authenticate`, `mod_base.build_ci.reads`, `mod_base.build_ci.records`, `mod_base.build_ci.config`, `mod_base.build_ci.activation`, `mod_base.build_ci.transition`, `mod_base.build_ci.controller`, `mod_base.build_ci.inputs`, `mod_base.build_ci.policy`, `mod_base.build_ci.validation`, `mod_base.build_ci.exports`, `mod_base.build_ci.worker`, `mod_base.build_ci.source`, `mod_base.build_ci.host`, `mod_base.build_ci.toolchain`, `mod_base.build_ci.transport`, `mod_base.build_ci.selection`, `mod_base.build_ci.archive`, `mod_base.build_ci.handoff`, `mod_base.build_ci.root_request_schema`, `mod_base.build_ci.root_request`, `mod_base.build_ci.root_request_operations`, `mod_base.build_ci.gradle_cache`, `mod_base.build_ci.worker_overlay`, `mod_base.build_ci.worker_source`, `mod_base.build_ci.worker_git`, `mod_base.build_ci.worker_preparation`, `mod_base.build_ci.batch`, `mod_base.build_ci.batch_schema`, `mod_base.build_ci.runtime_schema`, `mod_base.build_ci.runtime_exports`, `mod_base.build_ci.runtime_inputs`, `mod_base.build_ci.runtime_freeze`, `mod_base.build_ci.runtime_handoff` |
| MB11 | `mod_base.build_ci.batch_git`, `mod_base.build_ci.commands_batch` |
| MB11 | `mod_base.build_ci.status`, `mod_base.build_ci.commands_packaged`, `mod_base.build_ci.commands_status` |
| MB11 | `mod_base.build_ci.describe`, `mod_base.build_ci.gate`, `mod_base.build_ci.commands_build` |

A private module (`_name`, for example `mod_base.evidence._common`) belongs to the unit that owns
the other modules of its package and is never imported by another unit. A package `__init__`
(`mod_base.evidence`, `mod_base.pages`, ...) holds only its docstring; kit tools (`tools/*.py`) are
users of this interface like any other unit.

## `mod_base`

Owner: MB0 (implemented).

mod-base: the shared public-evidence and Pages publication kit.

Constants:

* `ADAPTER_API = 1`
* `PIXEL_METRICS_VERSION = 1`
* `KIT_REPOSITORY = 'The-Plum-Team/mod-base'`
* `__version__`: the kit version `X.Y.Z` (its release tag without `v`), reported by `KitRef`s
  (`runtime.kit_ref`) and `--version`.
* `SCHEMA_VERSIONS`: every document kind (`mod-base.*`) mapped to the schema version this kit
  writes (all `1`).

* `def readable_schema_versions(kind: str) -> frozenset[int]`: Return the schema versions a reader of ``kind`` accepts: the current one and N-1 (>= 1).

## `mod_base.errors`

Owner: MB0 (implemented).

Kit exception hierarchy and the single process-exit mapping used by every entry point.

Constants:

* `MAX_MESSAGE_CHARS = 1000`
* `EXIT_OK = 0`
* `EXIT_INTERNAL = 1`
* `EXIT_REJECTED = 2`
* `EXIT_UNAVAILABLE = 3`
* `EXIT_CONTROLLER_SKEW = 78`
* `EXIT_INTERRUPTED = 130`

* `class MbError(Exception)`: A fail-closed rejection. ``reason`` is a short machine-readable token for outputs.
  * `__init__(self, message: str, *, reason: str | None = None) -> None`
* `class Unavailable(MbError)`: No admissible evidence exists (for example no authenticated artifact); exit 3.
* `class Superseded(Unavailable)`: The evidence is valid but bound to a superseded contract (family drift); exit 3.
* `class ControllerSkew(MbError)`: The executing kit tree does not match the pin in the checked-out mod; exit 78.
* `def single_line(message: object, *, limit: int = 1000) -> str`: Return ``message`` as one printable line of at most ``limit`` characters.
* `def exit_code_for(error: BaseException) -> int`: Map an exception raised by an entry point to its process exit code.
* `def describe(error: BaseException) -> str`: Return the one-line stderr description of ``error`` (without the program prefix).
* `def run_main(entry: Callable[[], int | None], *, program: str = 'mod_base', stderr: TextIO | None = None) -> int`: Run ``entry`` and convert every outcome into an exit code plus at most one stderr line.

## `mod_base.cli`

Owner: MB0 (implemented).

``python3 -P -m mod_base <command>``: the static command registry (SPEC §2.2, §10).

Constants:

* `MAX_OUTPUT_VALUE_CHARS = 65536`
* `COMMANDS`: command name -> the module whose `register(subparsers)` adds it (SPEC §10
  "Command registry"; only that module is imported for the command).
* `OUTPUT_NAME`: the pattern of a `$GITHUB_OUTPUT` name (`^[a-z][a-z0-9_]{0,63}$`).

Argument types (argparse `type=` converters built by `typed`; a failure is a
one-line usage error; frozen for every command module):

* `KEY`: a `KEY` (`grammar.require_key`).
* `FAMILY`: a `FAMILY` (`grammar.require_family`).
* `SHA1`: a lowercase 40-hex commit SHA.
* `DIGEST`: `sha256:<64 hex>`.
* `BRANCH`: a `BRANCH`.
* `POSITIVE`: a decimal positive integer (no sign, no leading zero), at most `2**63 - 1`.
* `KEYS`: comma-separated unique keys, as a tuple.
* `DELAY`: seconds in `0.0..60.0`, as a float.
* `PATH`: a non-empty path without NUL, as a `Path`.

* `class KitArgumentParser(ArgumentParser)`: An ``ArgumentParser`` whose usage errors raise :class:`MbError` (one line, exit 2).
  * `error(self, message: str) -> NoReturn`
* `def typed(check: Callable[[str], Any], description: str) -> Callable[[str], Any]`: Wrap a grammar check as an argparse ``type=`` whose failure is a one-line usage error.
* `def add_repo_config(parser: argparse.ArgumentParser) -> None`: Add ``--repo DIR`` (required) and ``--config FILE`` (default ``<repo>/site/mod-base.json``; an explicit path is relative to the working directory, like ``--repo``).
* `def write_github_output(path: Path | None, values: Mapping[str, str | int | bool]) -> None`: Append ``name=value`` lines to a ``$GITHUB_OUTPUT`` file.
* `def load_group(command: str) -> ModuleType`: Import the module owning ``command`` or raise one clean :class:`MbError`.
* `def build_parser(command: str) -> KitArgumentParser`: A parser holding only ``command``'s group, registered by its owning module.
* `def dispatch(argv: Sequence[str]) -> int`
* `def main(argv: Sequence[str] | None = None) -> int`: Process entry point: every outcome becomes an exit code plus at most one stderr line.
* `def environ() -> Mapping[str, str]`: The process environment (a seam for handlers; entry points use ``Invocation.environ``).

## `mod_base.runtime`

Owner: MB0 (implemented).

The per-process invocation context every command builds once and passes to entry points.

Constants:

* `ENVIRONMENT_NAMES = ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_JOB', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REF', 'GIT...`: every environment name the kit consumes: GitHub's run facts, the pinned kit commit, the tokens and, for `ci worker-prepare`, the runner layout (`RUNNER_ENVIRONMENT`, `GITHUB_WORKSPACE`, `RUNNER_TEMP`).

* `def kit_root() -> Path`: The root of the executing kit checkout (the directory holding ``src/``, ``site/``...).
* `def kit_ref(sha: str) -> dict[str, str]`: Return the ``KitRef`` of the executing kit at ``sha`` (SPEC §3.0).
* `class Invocation`: Everything an entry point may know about its process. Accessors fail closed when a fact the caller needs is absent instead of inventing a default.
  * fields: `repo_root: Path, config: Config, kit_root: Path, environ: Mapping[str, str] = <factory>, implementation_sha_override: str | None = None`
  * `kit_src` (property) -> `Path`
  * `repository` (property) -> `str`
  * `implementation_sha` (property) -> `str`
  * `kit` (property) -> `dict[str, str]`
  * `github_job` (property) -> `str | None`
  * `token` (property) -> `str | None`
  * `api_url` (property) -> `str`
  * `github_output(self, explicit: Path | None = None) -> Path | None`: The ``$GITHUB_OUTPUT`` file (an explicit ``--github-output`` wins).
* `def snapshot_environment(environ: Mapping[str, str]) -> Mapping[str, str]`: A read-only copy of exactly the environment names the kit consumes.
* `def build_invocation(repo: Path | str, config_path: Path | str | None, environ: Mapping[str, str], *, check_repository: bool = True, implementation_sha: str | None = None, root: Path | None = None) -> Invocation`: Load the config of ``repo`` and bind the environment snapshot into an :class:`Invocation`.

## `mod_base.config`

Owner: MB0 (implemented).

Strict loader and validator for ``site/mod-base.json`` (``mod-base.config`` v1, SPEC §4.1).

Constants:

* `KIND = 'mod-base.config'`
* `DEFAULT_CONFIG_PATH = 'site/mod-base.json'`
* `THEME_KEYS = ('bg', 'surface', 'surface_raised', 'surface_soft', 'text', 'muted', 'line', 'accent', 'accent_strong', 'hi...`

* `def validate_config(document: Any, *, path: str = '$') -> dict[str, Any]`: Validate a decoded ``mod-base.config`` v1 document and return it.
* `class Config`: A validated ``mod-base.config`` v1. ``data`` is the validated document; treat it as read-only.
  * fields: `data: dict[str, Any], sha256: str, path: str`
  * `canonical_branch` (property) -> `str`
  * `project` (property) -> `dict[str, Any]`
  * `adapter` (property) -> `dict[str, Any]`
  * `adapter_timeout_seconds` (property) -> `int`
  * `network_hooks` (property) -> `frozenset[str]`
  * `extension_names` (property) -> `frozenset[str]`
  * `targets` (property) -> `dict[str, Any]`
  * `source` (property) -> `dict[str, Any]`
  * `images` (property) -> `dict[str, Any]`
  * `anchor` (property) -> `dict[str, Any]`
  * `admission` (property) -> `dict[str, Any]`
  * `baseline_archive` (property) -> `dict[str, Any]`
  * `families` (property) -> `list[dict[str, Any]]`
  * `labels` (property) -> `dict[str, Any]`
  * `copy` (property) -> `dict[str, Any]`
  * `theme` (property) -> `dict[str, Any]`
  * `template` (property) -> `dict[str, Any]`
  * `family(self, family_id: str) -> dict[str, Any]`: Return the configured family ``family_id`` or raise :class:`MbError`.
  * `image_policy(self) -> dict[str, Any]`: The ``expectation.image_policy`` every expectation must equal (``config.images`` without ``cross_check_runtime_metrics``).
* `def parse_config(data: bytes, *, path: str = 'site/mod-base.json') -> Config`: Strictly decode and validate config bytes; ``path`` is recorded for messages only.
* `def check_repository(config: Config, repo_root: Path) -> None`: Verify the config's repository facts at the checked-out head (see module docstring).
* `def load_config(repo_root: Path | str, config_path: Path | str | None = None, *, check_repository_facts: bool = True) -> Config`: Read, validate and (by default) repository-check the mod's config.

## `mod_base.workflow`

Owner: MB0 (implemented).

THE single source of the Pages and Build/E2E workflow, job and step display names (SPEC §5.9).

Constants:

* `PAGES_WORKFLOW_NAME = 'Project site'`
* `PAGES_WORKFLOW_PATH = '.github/workflows/pages.yml'`
* `PAGES_EVENTS = frozenset({'schedule', 'workflow_dispatch'})`
* `CI_GUARD_WORKFLOW_PATH = '.github/workflows/mod-base-guard.yml'`
* `CI_CALLER_WORKFLOWS`: managed caller id (`build`, `packaged`, `status`) -> its path in the mod
  (`.github/workflows/mod-base-build.yml`, `mod-base-packaged-e2e.yml`, `mod-base-gate-status.yml`);
  `build` and `packaged` are the two producers.
* `CI_CALLEE_WORKFLOWS`: Build/E2E callee id (`build`, `select-build`, `packaged-e2e`) -> its path
  in the kit repository. Its own registry: never `CALLEE_WORKFLOWS`, which stays the Pages table.
* `CI_GUARD_CALL`, `CI_BUILD_CALL`, `CI_SELECT_CALL`, `CI_PACKAGED_CALL`: bare names of the calling
  jobs (`Verify pinned mod-base`, `Shared Build`, `Select exact Build`, `Shared Packaged E2E`), the
  prefix of every job of the workflow each calls.
* `CI_CALLER_JOBS`: managed caller id -> job key -> bare display name of that caller-owned job
  (`build`: `guard`, `deferred`, `shared`; `packaged`: `guard`, `deferred`, `select`, `rebuild`,
  `shared`; `status`: `evaluate`, `publish`).
* `CI_CALLS`: producer id -> calling job key -> the workflow it calls: `guard` (the mod's own
  `CI_GUARD_WORKFLOW_PATH`) or a `CI_CALLEE_WORKFLOWS` id.
* `CI_GUARD_JOBS`, `CI_BUILD_JOBS`, `CI_SELECT_JOBS`, `CI_PACKAGED_JOBS`: job key -> `name:`
  template of the guard workflow and of each kit callee; `{id}` is the target or lane.
* `CI_CALLEE_JOBS`: called workflow id (`guard` or a `CI_CALLEE_WORKFLOWS` id) -> its job table.
* `CI_SEAL_STEP`, `CI_UPLOAD_STEP`: exact step names of the sealing step and of the upload that
  follows it in every target, assemble, lane, aggregate and gate job.
* `CI_JOB_VERBS`: Build/E2E callee id -> job key -> the `ci` verbs its steps issue after the Build
  controller prologue, one verb per step and in step order. A callee enters this table and the two
  below with its workflow file; all three (`build`, `select-build`, `packaged-e2e`) are tabled.
  Every packaged job after `input` issues `select-build` again, naming the run `input`
  authenticated, and the `aggregate` job's sealing step is `ci aggregate` itself.
* `CI_JOB_ARTIFACTS`: Build/E2E callee id -> sealing job key -> callee mode (`full`, `reuse`) -> the
  kind of the one artifact the job uploads in that mode. Exactly these jobs have a `CI_SEAL_STEP`
  directly followed by a `CI_UPLOAD_STEP`; `select-build` has no row and uploads nothing.
* `CI_JOB_PERMISSIONS`: Build/E2E callee id -> job key -> the complete `permissions:` of that job
  (`actions`, `contents` and `pull-requests`, each `read`), which the calling job must grant.
* `PAGES_CRON = '43 * * * *'`
* `OPERATIONS = ('manual', 'deploy', 'family', 'rotate')`
* `PUBLISH_OPERATIONS = ('recovery', 'manual', 'deploy', 'family')`
* `PUBLICATION_LOCK = 'mod-base-pages-publication'`
* `ROTATION_LOCK = 'mod-base-pages-rotation'`
* `CALLEE_WORKFLOWS`: callee workflow id (`publish`, `finalize`, `rotate`) -> its path in the
  kit repository.
* `CALLER`: caller-owned job key -> its bare display name (`caller_job_name`; `template.tool`
  refuses a mod job reusing one of these names).
* `CALLEE`: callee workflow id -> job key -> its `name:` template (`callee_job_name`).
* `STEPS`: step key -> its exact display name (`step_name`).
* `MATRIX_EXPRESSIONS`: placeholder (`key`, `family`, `id`) -> the `${{ matrix.* }}` expression the
  callee YAML uses.

* `def caller_job_name(key: str) -> str`: Return the bare display name of a caller-owned job, e.g. ``"Deploy GitHub Pages"``.
* `def callee_job_name(workflow: str, job: str, **fields: str) -> str`: Return the callee's own ``name:`` value with its placeholders filled (no caller prefix).
* `def api_job_name(workflow: str, job: str, **fields: str) -> str`: Return the name the jobs API reports for a callee job, e.g. ``"Publish / Collect mc1.20.1"``.
* `def workflow_template_name(workflow: str, job: str) -> str`: Return the callee job ``name:`` exactly as written in its YAML (``${{ matrix.* }}`` form).
* `def unexpanded_api_job_name(workflow: str, job: str) -> str`: Return the name the jobs API reports, once, for a matrix callee job that its job-level ``if`` skipped before its matrix expanded (added in the integration round; `build` accepts it only for a publication without family legs, and only `completed/skipped`).
* `def ci_producer(workflow_path: str) -> str`: The producer id (``build`` or ``packaged``) of the managed caller at that path; any other path raises.
* `def ci_caller_job_name(caller: str, job: str) -> str`: The bare display name of a job the managed caller owns, as the jobs API reports a caller job that has steps.
* `def ci_callee_job_name(callee: str, job: str, **fields: str) -> str`: A called workflow's own ``name:`` with its placeholders filled (no caller prefix).
* `def ci_api_job_name(producer: str, call: str, job: str, **fields: str) -> str`: The name the jobs API reports for a job of the workflow the producer's calling job calls: ``"<calling job> / <callee job>"``.
* `def ci_skipped_call_job_name(producer: str, call: str) -> str`: The name the jobs API reports, once, for a calling job whose job-level ``if`` skipped the call: the caller's bare job name.
* `def ci_workflow_template_name(callee: str, job: str) -> str`: A called workflow's job ``name:`` exactly as written in its YAML (``${{ matrix.id }}`` form).
* `def ci_unexpanded_api_job_name(producer: str, call: str, job: str) -> str`: The name the jobs API reports, once, for a matrix job of a called workflow skipped before its matrix expanded.
* `def step_name(key: str) -> str`
* `def find_job(jobs: Sequence[Mapping[str, Any]], name: str, *, run_attempt: int) -> dict[str, Any]`: Return the single job named exactly ``name`` in ``run_attempt``.

## `mod_base.model.grammar`

* `CI_BATCH_BRANCH_PREFIX`: Fixed batch/ namespace; aliases in batch preserve the released value.
* `CI_BATCH_NAME`: The name a new batch is given (`^[a-z0-9][a-z0-9._-]{0,62}$`); its branch `batch/<name>` must also satisfy `is_batch_branch`.
* `def is_batch_branch(value: object) -> bool`: Bounded batch/* Git branch using the existing conservative character grammar, with a nonempty suffix, no dot-started/.lock-ended components and no final dot/slash. This publication-specific check does not change existing BRANCH document grammar.

Owner: MB0 (implemented).

Identifier grammar and the only builders/parsers of kit artifact names (SPEC §3.0).

Constants:

* `MAX_FAMILY_LENGTH = 32`
* `CI_UNIT_ID`: bounded opaque Build target/runtime lane identifier; no slash or command syntax.
* `CI_ENVELOPE_NAME`: reserved outer Build envelope filename.
* `CI_RUNTIME_ENVELOPE_NAME`: reserved outer runtime envelope filename.
* `CI_RUNTIME_INPUT_FORMAT`: fixed local context-hash domain for retained plan/owning Build/runtime lane inputs; not another document kind or native scenario catalog.
* `CI_ARCHIVE_NAME`: fixed local encoded export ZIP leaf, not a nested GitHub artifact format.
* `CI_GATE_NAME`: fixed single root filename of a tested gate-record ZIP.
* `CI_RESULTS_NAME`: fixed single root filename of a results-index ZIP (`mb-ci-results--R--aN`).
* `CI_SELECTION_NAME`: the state record of the Build a packaged job selected (`mod-base.ci.selection`), which `ci select-build` writes and `ci aggregate` reads.
* `CI_VALIDATION_NAME`: reserved verifier output record filename.
* `CI_EXECUTION_NAME`: fixed private local runner-to-root execution record filename.
* `CI_ROOT_REQUEST_NAME`: fixed file name of the one private request inside a root operation's request directory.
* `CI_ROOT_OPERATIONS`: the closed, ordered set of operations the root bootstrap dispatches (`tools/ci_privileged_bootstrap.py` mirrors it without importing the kit). A request never selects code outside this set.
* `CI_PLAN_NAME`: fixed existing-kind plan filename for protected verifier input.
* `CI_ARTIFACT_PREFIXES`: separate attempt-specific CI name prefixes, excluded from the Pages parser.
* `class CIArtifactName`
  * fields: `kind: str, name: str, run_id: int, run_attempt: int, unit_id: str | None = None`
* `def ci_artifact_name(kind: str, run_id: int, run_attempt: int, unit_id: str | None = None) -> str`
* `def parse_ci_artifact_name(name: object) -> CIArtifactName | None`
* `MAX_LANE_ID_LENGTH = 200`
* `MAX_EXTENSION_NAME_LENGTH = 80`
* `ARTIFACT_PREFIX = 'mb-'`
* `PROMOTION_NAME = 'mb-promotion'`
* `PAGES_ARTIFACT_NAME = 'github-pages'`
* `KIT_STAMP_NAME = 'MOD_BASE_KIT.json'`

Compiled full-match patterns (use `is_match`/`require`; the grammar is SCHEMAS.md
"Identifiers"):

* `KEY`, `FAMILY`, `LANE_ID`, `SHA1`, `SHA256`, `DIGEST`, `BRANCH`, `REPOSITORY`, `VERSION`, `IDENT`
* `ARTIFACT_NODE`, `MINECRAFT`, `LOADER`, `SCENARIO`, `ROLE`, `STEP`, `REVIEW_TIER`, `PROFILE`
* `VARIANT_ID`, `NATIVE_KIND`, `CONTRACT_NAME`, `EXTENSION_NAME`, `EVENT`, `WORKFLOW_PATH`
* `RFC3339Z`, `ACTIONS_TIMESTAMP`, `POSITIVE_DECIMAL`, `RUN_URL`, `WORKFLOW_REF`
* `ARTIFACT_PREFIXES`: artifact kind -> name prefix (`mb-handoff`, `mb-anchor`, ...), the kinds
  of `ArtifactName.kind` other than `promotion` and `pages`.

* `def is_match(pattern: re.Pattern[str], value: object) -> bool`: Return True when ``value`` is a ``str`` fully matching ``pattern`` (never raises).
* `def require(pattern: re.Pattern[str], value: object, label: str) -> str`: Return ``value`` when it is a ``str`` fully matching ``pattern``; raise MbError otherwise.
* `def is_key(value: object) -> bool`
* `def is_family(value: object) -> bool`
* `def is_lane_id(value: object) -> bool`
* `def is_extension_name(value: object) -> bool`
* `def require_key(value: object, label: str = 'key') -> str`
* `def require_family(value: object, label: str = 'family') -> str`
* `def require_sha1(value: object, label: str = 'commit') -> str`
* `def require_positive_int(value: object, label: str, *, maximum: int = 9223372036854775807) -> int`: Return a positive ``int`` (never ``bool``) no larger than ``maximum``.
* `def is_bundle_path(value: object) -> bool`: True for a canonical bundle-relative POSIX path (no absolute, ``..``, ``.``, ``\`` or NUL).
* `def is_repo_path(value: object) -> bool`: True for a repository-relative path: a bundle path none of whose components is ``.git`` (compared case-insensitively, for case-insensitive filesystems).
* `def is_export_path(value: object) -> bool`: True for a canonical path inside a sealed CI export: the length and depth bounds of a bundle path (300 characters, 16 components of at most 128), components of ASCII letters, digits, ``._-+`` and single inner spaces, none starting or ending with a space or a dot. Pages bundle paths stay narrower; names that differ only in case are for the inventory holding them to refuse.
* `def is_seed_path(value: object) -> bool`: True for a structurally safe relative path inside a Gradle seed: no NUL, no empty, ``.`` or ``..`` component, at most ``limits.MAX_CI_SEED_PATH_DEPTH`` of them. No name grammar: links, special files, counts and sizes are for the tree walk to refuse.
* `def parse_timestamp(value: object, label: str = 'timestamp') -> datetime`: Parse a GitHub ``YYYY-MM-DDTHH:MM:SSZ`` timestamp into an aware UTC datetime.
* `def normalize_timestamp(value: object, label: str = 'timestamp') -> str`: Return an Actions API time (``...Z``, fractional seconds or a numeric offset) as whole-second UTC ``YYYY-MM-DDTHH:MM:SSZ``, the one form Build/E2E records store; two results compare as text in time order.
* `class WorkflowRef`: A parsed ``GITHUB_WORKFLOW_REF`` (``owner/repo/.github/workflows/f.yml@refs/heads/b``).
  * fields: `repository: str, path: str, branch: str`
* `def parse_workflow_ref(value: object) -> WorkflowRef`: Parse a branch-scoped workflow ref; tags, pull refs and malformed values are rejected.
* `def workflow_ref(repository: str, path: str, branch: str) -> str`: Build the exact ``GITHUB_WORKFLOW_REF`` value for a branch.
* `def run_url(repository: str, run_id: int) -> str`: Build a run URL. Renderers build every run URL with this; none is copied from a bundle.
* `def short_sha(commit: str, length: int = 12) -> str`
* `class ArtifactName`: A parsed kit artifact name. Fields not carried by ``kind`` are ``None``.
  * fields: `kind: str, name: str, key: str | None = None, family: str | None = None, attempt: int | None = None, commit: str | None = None, run_id: int | None = None, coverage_sha: str | None = None`
* `def handoff_name(key: str, attempt: int) -> str`
* `def anchor_name(key: str, commit: str, run_id: int, attempt: int) -> str`
* `def family_handoff_name(family: str, key: str, attempt: int) -> str`
* `def collected_name(key: str) -> str`
* `def collected_family_name(family: str, key: str) -> str`
* `def cache_name(key: str, coverage_sha: str) -> str`
* `def family_cache_name(family: str, key: str, coverage_sha: str) -> str`
* `def baseline_name(key: str, commit: str, tested_run_id: int) -> str`
* `def baseline_name_regex(key_pattern: str) -> str`: Return the full-match regex source for ``mb-baseline`` names whose key matches ``key_pattern``.
* `def parse_artifact_name(name: object) -> ArtifactName | None`: Parse a kit artifact name, or return ``None`` when it is not exactly a kit name.
* `def require_artifact_name(name: object, kind: str) -> ArtifactName`: Parse ``name`` and require its kind; raise MbError for foreign or malformed names.
* `def is_kit_artifact_name(name: object) -> bool`: True only for names this kit may create, list, download or retire.

## `mod_base.model.limits`

Owner: MB0 (implemented).

Every numeric bound the kit enforces (SPEC §3.0 "Global limits" plus the per-field bounds).

Every bound is a module constant and every name below is frozen (values in the source;
`tests/test_model_limits.py` pins them). Code imports a bound from here; a same-named module
constant elsewhere (for example `pages.admission.MAX_CANDIDATES`, frozen in its own section) is only
an alias and must hold the same value.

* `KIB`, `MIB`, `GIB`

Documents (SPEC §3.0 table):

* `MAX_MANIFEST_BYTES`, `MAX_EXPECTATION_BYTES`, `MAX_SELECTION_BYTES`, `MAX_EXTENSIONS_BYTES`
* `MAX_SCOPE_DETAIL_BYTES`, `MAX_PROMOTION_BYTES`, `MAX_ENVELOPE_BYTES`, `MAX_PAIRED_BYTES`
* `MAX_BUILD_RECORD_BYTES`, `MAX_SITE_DATA_BYTES`, `MAX_GALLERY_DATA_BYTES`
* `MAX_TEMPLATE_MANIFEST_BYTES`, `MAX_KIT_STAMP_BYTES`, `MAX_CONFIG_BYTES`
* `MAX_SELECTED_JSON_BYTES`: a `--selected-json` document, also a collected family's `selected.json`

Bundles and images:

* `MAX_FRAMES`, `MAX_LANES`, `MAX_COMPARISONS`, `MAX_SCENARIOS`, `MAX_ROLES_PER_LANE`
* `MAX_RUNTIME_FILES`, `MAX_RUNTIME_JSON_BYTES`, `MAX_SOURCE_PNG_BYTES`, `MAX_DERIVATIVE_BYTES`
* `MAX_ARCHIVE_OVERHEAD_BYTES`: what an archive may add to its files (see the archive-cap amendment)
* `MAX_ARTIFACT_BYTES`: the artifact archive cap, `MAX_RAW_BUNDLE_BYTES` + `MAX_ARCHIVE_OVERHEAD_BYTES`
* `MAX_RAW_BUNDLE_BYTES`, `MAX_COMPACT_BUNDLE_BYTES`, `MAX_ANCHOR_BUNDLE_BYTES`, `MAX_HANDOFF_FILES`
* `MAX_COMPACT_FILES`, `MAX_ANCHOR_FILES`, `MAX_IMAGE_PIXELS`, `MAX_IMAGE_DIMENSION`, `MAX_SITE_BYTES`
* `MAX_SITE_FILES`, `MAX_BUNDLE_PATH_CHARS`, `MAX_BUNDLE_PATH_DEPTH`

Families:

* `MAX_FAMILIES`, `MAX_FAMILY_RETENTION_DAYS`, `MAX_FAMILY_FILES`
* `MAX_COLLECTED_FAMILY_BYTES`, `MAX_FAMILY_PROJECTION_BYTES`, `MAX_FAMILY_HANDOFF_BYTES`, `MAX_FAMILY_BUNDLE_BYTES`: the family byte budget (the collected-family amendment)
* `MAX_FAMILY_LANES`, `MAX_FAMILY_PAIRS_PER_LANE`, `MAX_NOT_APPLICABLE`, `MAX_FAMILY_LINKS`
* `MAX_FAMILY_CONTRACTS`

Text fields:

* `MAX_RUNTIME_EVIDENCE_LENGTH`, `MAX_TITLE_LENGTH`, `MAX_EXPECTATION_TEXT_LENGTH`, `MAX_LABEL_LENGTH`
* `MAX_PROFILE_LENGTH`, `MAX_REASON_LENGTH`, `MAX_LINK_LABEL_LENGTH`, `MAX_DISPLAY_TITLE_LENGTH`
* `MAX_JOB_NAME_LENGTH`, `MAX_EXTENSION_NAMES`

Targets, admission and GitHub (the last seven, and `MAX_SELECTED_JSON_BYTES` above, moved here from
their units in the integration round; `DELETION_BUDGET` is 64, see the rotation-budget amendment):

* `MAX_KEYS`, `MAX_BRANCHES`, `MAX_ARTIFACTS_PER_NAME`, `MAX_PAGES_API_READS`, `MAX_API_RESPONSE_BYTES`
* `MAX_JOBS_PER_ATTEMPT`, `MAX_RUN_ATTEMPT`, `MAX_RUN_ID`, `MAX_ARTIFACT_NAME_BYTES`, `DELETION_BUDGET`
* `RUN_POLL_ATTEMPTS`, `RUN_POLL_INTERVAL_SECONDS`
* `LISTING_READ_ATTEMPTS`, `LISTING_RETRY_DELAY_SECONDS`, `MAX_LISTING_RETRY_DELAY_SECONDS`, `MAX_FILTERED_RUNS_LISTED`: the consistent-listing retry and the 1,000 newest runs GitHub lists for a filtered workflow-run search (see the consistent-listing amendment)
* `MAX_CANDIDATES`, `MAX_SUBJECT_RUNS`, `MAX_CANONICAL_RUNS`, `MAX_FAMILY_LEGS`
* `MAX_WORKFLOW_FILE_BYTES`, `GENERATION_PROBES`, `ANCHOR_PROBES`

ZIP extraction (see the collected-family amendment):

* `MAX_ZIP_RATIO`, `MAX_COLLECTED_FAMILY_FILES`, `MAX_ZIP_ENTRIES`

Adapter host, config and retention:

* `ADAPTER_TIMEOUT_DEFAULT_SECONDS`, `ADAPTER_TIMEOUT_MAX_SECONDS`, `MAX_ADAPTER_REQUEST_BYTES`
* `MAX_ADAPTER_RESPONSE_BYTES`, `MAX_ADAPTER_PYTHON_PATH`
* `MAX_ICON_BYTES`, `MAX_ICON_DIMENSION`, `MAX_PROJECT_LINKS`, `MAX_LABEL_ENTRIES`
* `MAX_COPY_PARAGRAPHS`, `MAX_TEMPLATE_PATHS`
* `RETENTION_DAYS`, `MAX_ANCHOR_RETENTION_DAYS`, `MAX_BASELINE_RETENTION_DAYS`
* `CI_RETENTION_DAYS`: retention days of every `mb-ci-*` artifact kind (target 1; build, runtime and results 7; tested and reuse 90); Pages rotation never sees these names.

Protected Build/runtime (independent ceilings, no change to Pages budgets):

* `MAX_CI_PLAN_BYTES`, `MAX_CI_TARGETS`, `MAX_CI_BUILD_RUNS`, `MAX_CI_LANES`, `MAX_CI_OUTPUTS_PER_TARGET`
* `MAX_CI_PLAN_SOURCE_BYTES`: 4 MiB for each candidate file a plan is derived from (the release inventory, the scenario contract, an extra plan input).
* `MAX_CI_PLAN_INPUTS`: 8 extra candidate files a protected Build config may name under `plan_inputs` and a plan may bind.
* `MAX_CI_IDENTITY_BYTES`: 16 KiB for the private `identity.json` state record.
* `MAX_CI_GATE_REQUESTS`: the 60-request budget of one `ci seal-gate` (a Build gate costs 15, the packaged gate of a pull request 20, whatever the number of targets and lanes).
* `MAX_CI_SUBJECT_REQUESTS`: the 16-request budget of one `ci subject` (a pull request costs 4, a protected subject 5).
* `MAX_CI_STATUS_CONTEXT_CHARS`: 100 characters for one status context of the protected Build config.
* `CI_BUILD_POLL_SECONDS`, `MAX_CI_BUILD_POLLS`: Protected 60-second polling cadence and independent 91-observation ceiling within the existing 5400-second admission budget.
* `MAX_CI_WORKER_RECORD_BYTES`: 256 KiB for the private `worker.json` state record of `ci worker-prepare`.
* `MAX_CI_PLAN_REQUESTS`: the 24-request budget of one `ci plan` without a candidate checkout (the tested tree and one blob per candidate file: 3 without extra plan inputs, at most 11; with a checkout the command spends none).
* `CI_GIT_READ_TIMEOUT_SECONDS`, `MAX_CI_GIT_ANSWER_BYTES`: 60 seconds for one read of the candidate checkout's object store, and 4 KiB for what such a read answers besides a blob (one object id, or one tree entry with its path).
* `MAX_CI_PLAN_INPUT_FILES`, `MAX_CI_PLAN_INPUT_ENTRIES`, `MAX_CI_PLAN_INPUT_BYTES`: The validator's input tree `validation-input/`: at most the plan, the inventory, the scenario contract and `MAX_CI_PLAN_INPUTS` extra plan inputs (11 files, 12 entries with the directory, the plan cap plus ten times the candidate file cap). A check of the tree requires exactly the files of its state, not merely at most these.
* `MAX_CI_SELECT_BUILD_REQUESTS`: 155, the request budget of `ci select-build`: a pull request whose Build is complete costs 17 and each earlier poll of its wait one more (91 polls at most); a protected subject costs 15. The rest is for retries and for the further pages of a run that lists more than 100 jobs.
* `MAX_CI_FETCH_BUILD_REQUESTS`: 48, the request budget of `ci fetch-build` (21 for a pull request, 18 for a selected and 15 for a rebuilt Build of a protected subject).
* `MAX_CI_GATE_STATUS_REQUESTS`: 96, the request budget of `ci gate-status` (45 with both runs complete).
* `MAX_CI_PRIVATE_RECORD_ENTRIES`: Exact entry budget of a fixed single-leaf private record directory. MB1 entry caps count the root, so the directory plus its one leaf; an empty stage stays 1.
* `MAX_CI_POLICY_TESTS`, `MAX_CI_POLICY_WORKERS`: Policy discovery/count and worker ceilings.
* `MAX_CI_ENVELOPE_BYTES`, `MAX_CI_RECORD_BYTES`, `MAX_CI_ARTIFACTS_PER_GATE`
* `MAX_CI_CONFIG_BYTES`, `MAX_CI_ACTIVATION_BYTES`, `MAX_CI_ADAPTER_FILES`, `MAX_CI_WORKER_TIMEOUT_SECONDS`
* `MAX_CI_ADAPTER_FILE_BYTES`, `MAX_CI_ADAPTER_TREE_BYTES`
* `MAX_CI_ENV_VALUE_BYTES`, `MAX_CI_ENV_BYTES`, `MAX_CI_CONTROL_OUTPUT_BYTES`, `MAX_CI_TOOL_PATH_BYTES`
* `MAX_CI_TOOL_ROOTS`, `MAX_CI_TOOL_SYMLINK_HOPS`, `MAX_CI_TOOL_TREE_DEPTH`
* `MAX_CI_KIT_INSTALL_FILES`, `MAX_CI_KIT_INSTALL_BYTES`, `MAX_CI_KIT_INSTALL_ENTRIES`: Bounds of one kit tree (the checkout the root bootstrap hashes and the overlay staged for a candidate); they equal the kit-digest-v1 bounds of `mod_base.pin`.
* `MAX_CI_FILE_ID`
* `MAX_CI_ROOT_REQUEST_BYTES`: Cap of one private root request: twice the source-listing cap (a tested-tree inventory as JSON rows), the plan, Build envelope, runtime envelope and record caps and 2 MiB of framing. No native or artifact cap is enlarged.
* `MIN_CI_WORKER_UID`, `CI_TERMINATION_GRACE_SECONDS`, `CI_TERMINATION_POLL_SECONDS`
* `MAX_CI_UNIX_ID`
* `MAX_CI_COMMAND_ARGUMENTS`, `MAX_CI_COMMAND_BYTES`, `CI_PROCESS_READ_BYTES`
* `CI_ROOT_OPERATION_TIMEOUT_SECONDS`, `MAX_CI_ROOT_DIAGNOSTIC_BYTES`: The 1800-second bound of one root operation process (the bootstrap arms the same alarm on itself) and the 4 KiB of its stderr the launching runner keeps for its own single error line. No worker or hook timeout changes.
* `CI_HOST_FENCE_TIMEOUT_SECONDS`, `MAX_CI_HOST_FENCE_REPORT_BYTES`: The 600-second bound of each of the host fence's two walks, inside the root operation's own bound, and the 64 KiB the fence keeps of each output stream of a walk: the listing of the world-writable entries that remain and the diagnostic of a command that failed. A listing that does not fit is never a clean result.
* `MAX_CI_SOURCE_LIST_BYTES`, `MAX_CI_SOURCE_FILES`, `MAX_CI_SOURCE_ENTRIES`
* `MAX_CI_SOURCE_FILE_BYTES`, `MAX_CI_SOURCE_TREE_BYTES`, `MAX_CI_SOURCE_LINK_BYTES`
* `MAX_CI_SEED_PATH_DEPTH`: components of one Gradle seed path, which has no name grammar and so no `MAX_BUNDLE_PATH_DEPTH`
* `MAX_CI_GIT_METADATA_FILES`, `MAX_CI_GIT_METADATA_ENTRIES`, `MAX_CI_GIT_METADATA_FILE_BYTES`
* `MAX_CI_GIT_METADATA_TREE_BYTES`, `MAX_CI_GIT_REF_BYTES`, `MAX_CI_GIT_REF_LIST_BYTES`
* `MAX_CI_OBLIGATIONS_PER_LANE`, `CI_BUILD_WAIT_SECONDS`, `MAX_CI_BATCH_MEMBERS`
* `MAX_CI_BATCH_DOCUMENT_BYTES`: 64 KiB: GitHub's bound of a pull request body, in which a batch manifest travels. The whole body, the marker line inside it and the decoded document share it.
* `MAX_CI_BATCH_PUSH_RECEIPT_BYTES`: 4 KiB closed single-ref Git porcelain observation cap; separate from worker logs and manifest transport.
* `MAX_CI_BATCH_TITLE_CHARS`: 256, GitHub's bound of a pull request title; a member's title is the subject of its squash commit.
* `MAX_CI_BATCH_ALLOWED_PATHS`: 4096 entries of the allowed-path list a caller hands to the batch constructor.
* `MAX_CI_BATCH_REPORTED_PATHS`: 20 paths named by one batch refusal (a conflict, a rename that was followed).
* `CI_BATCH_GIT_TIMEOUT_SECONDS`, `CI_BATCH_GIT_TRANSFER_TIMEOUT_SECONDS`: 120 seconds for one Git plumbing call of the batch store, 600 for one fetch or push.
* `MAX_CI_BATCH_GIT_OUTPUT_BYTES`: The output of one Git call of the batch store; the source-listing bound.
* `MAX_CI_ASSEMBLE_REQUESTS`: 816, the request budget of `ci assemble`: it sends 15 requests and 2 per target, the two of a download (49 for 17 targets); the rest is for retries and further listing pages.
* `MAX_CI_AGGREGATE_REQUESTS`: 816, the request budget of `ci aggregate`: it sends 13 requests and 2 per lane, the two of a download (81 for 34 lanes); the rest is for retries and further listing pages.
* `MAX_CI_BATCH_PREPARE_REQUESTS`: 216, the request budget of `ci batch-prepare`: it sends 10 requests and 3 per member (160 for 50 members); the rest is for retries.
* `MAX_CI_BATCH_SETTLE_REQUESTS`: 348, the request budget of `ci batch-settle`: it sends 3 requests, the 28 of `transport.download_merged_gate_pair` for both original gates and at most 5 per member (281 for 50 members); the rest is for retries and for the further pages of a run that lists more than 100 jobs or artifacts.
* `MAX_CI_BUNDLE_COMPRESSED_BYTES`: 512 MiB archive cap of one `mb-ci-*` artifact of any kind and profile (`tests/test_ci_limits.py` pins every Build/E2E bound with the native bound it preserves).
* `MAX_CI_EXPORT_FILES`, `MAX_CI_EXPORT_ENTRIES`, `MAX_CI_EXPORT_FILE_BYTES`, `MAX_CI_EXPORT_TREE_BYTES`
* `MAX_CI_TARGET_DOWNLOAD_BYTES`, `MAX_CI_TARGET_INPUT_ENTRIES`: Additional aggregate compressed-download and physical wrapped-input bounds; do not widen native/runtime fan-in or original logical export limits.
* `MAX_CI_ORIGINAL_INPUT_ENTRIES`: original Build/runtime private parent plus the two existing bounded child closures.
* `MAX_CI_RUNTIME_ENTRIES`: complete runtime logical tree closure, derived from existing file/path bounds.
* `MAX_CI_RUNTIME_AGGREGATE_FILES`, `MAX_CI_RUNTIME_AGGREGATE_BYTES`, `MAX_CI_RUNTIME_ENVELOPE_BYTES`: original Block Pops aggregate and closed envelope bounds.
* `MAX_CI_RUNTIME_FILES`, `MAX_CI_RUNTIME_BYTES`, `MAX_CI_REPORT_BYTES`, `MAX_CI_LOG_BYTES`
* `MAX_CI_EXECUTION_LOG_CHARS`, `MAX_CI_EXECUTION_BYTES`: bounded canonical base64 log and whole local execution-handoff JSON; independent of artifact/receipt transport limits.
* `MAX_CI_BUILD_REPORT_BYTES_BY_PROFILE`: closed original Build input report byte caps by profile;
  Block Pops 8 MiB, Quick Skin 4 MiB; independent of validator-output `MAX_CI_REPORT_BYTES`.
* `MAX_CI_SBOM_BYTES`: 16 MiB for one `sbom` output of a Build export (Quick Skin's own SBOM reader cap; an SBOM is optional and Block Pops stages none).
* `MAX_CI_PNG_BYTES`, `MAX_CI_JAR_BYTES`
* `MAX_CI_STATUS_DESCRIPTION_CHARS`: 140 characters for the description of one status intent, GitHub's own bound for a commit status.

## `mod_base.model.canonical`

Owner: MB0 (implemented).

The single strict JSON decoder, canonical encoder and bounded file reader of the kit.

* `class StrictJsonError(MbError)`: JSON bytes or the file holding them are not trustworthy.
* `def has_surrogate(text: str) -> bool`: True when ``text`` holds a UTF-16 surrogate code point (U+D800..U+DFFF), which is never valid Unicode text and cannot be encoded as UTF-8.
* `def strict_loads(data: bytes, *, label: str, max_bytes: int) -> Any`: Decode strict JSON ``data`` (see module docstring) or raise :class:`StrictJsonError`.
* `def canonical_json(value: Any) -> bytes`: Return the canonical document bytes of ``value`` (sorted, compact, UTF-8, trailing newline).
* `def sha256_hex(data: bytes) -> str`
* `def canonical_sha256(value: Any) -> str`: SHA-256 of ``canonical_json(value)``: the identity of an embedded JSON object.
* `def read_regular_file(path: Path | str, *, label: str, max_bytes: int, allow_empty: bool = False) -> bytes`: Read one stable regular file without following a final-component symlink.
* `def read_json_file(path: Path | str, *, label: str, max_bytes: int) -> tuple[Any, bytes]`: Read and strictly decode one JSON document; return ``(value, raw_bytes)``.

## `mod_base.model.validators`

Owner: MB0 (implemented).

Small strict validator combinators shared by every document kind, the config and the adapter.

Constants:

* `DISPLAY_CATEGORIES = frozenset({'Ll', 'Lm', 'Lo', 'Lt', 'Lu', 'Mc', 'Me', 'Mn', 'Nd', 'Nl', 'No', 'Pc', 'Pd', 'Pe', 'Pf', 'Pi', ...`

* `class DocumentError(MbError)`: A document failed structural validation at ``path``.
  * `__init__(self, path: str, message: str) -> None`
* `Validator`: the type of every validator, `Callable[[Any, str], Any]` (`(value, path) -> value`).
* `def fail(path: str, message: str) -> DocumentError`
* `def is_evidence_text(value: Any, max_length: int) -> bool`: The QS runtime-evidence rule (V9) generalized to any bounded text field.
* `def is_display_text(value: Any, max_length: int) -> bool`: Config/site copy: trimmed, non-empty, markup-free text of printable characters.
* `def Any_() -> Validator`
* `def Str(pattern: re.Pattern[str] | None = None, *, max_len: int = 200, min_len: int = 1, text: str | None = None, choices: Iterable[str] | None = None) -> Validator`: A string. ``text="evidence"`` applies :func:`is_evidence_text`, ``text="display"`` :func:`is_display_text`; ``pattern`` must fully match; ``choices`` restricts to a set.
* `def Const(expected: Any) -> Validator`
* `def Int(minimum: int, maximum: int) -> Validator`
* `def Num(minimum: float, maximum: float) -> Validator`
* `def Bool() -> Validator`
* `def Null() -> Validator`
* `def Nullable(inner: Validator) -> Validator`
* `def OneOf(*alternatives: Validator) -> Validator`: The first alternative that accepts the value wins; the last error is reported otherwise.
* `def List(item: Validator, *, min_items: int = 0, max_items: int, unique: bool = False, unique_by: Callable[[Any], Any] | None = None, sorted_values: bool = False) -> Validator`: An array of ``item``. ``unique`` rejects equal items; ``unique_by`` rejects equal projections; ``sorted_values`` requires strictly ascending items (implies uniqueness).
* `def Map(key_pattern: re.Pattern[str], item: Validator, *, max_items: int, min_items: int = 0, max_key_len: int = 80) -> Validator`: An object with arbitrary keys matching ``key_pattern`` and values validated by ``item``.
* `def Obj(required: Mapping[str, Validator], optional: Mapping[str, Validator] | None = None) -> Validator`: An object with exactly the ``required`` keys plus any subset of ``optional`` keys.
* `def Size(minimum: int, maximum: int) -> Validator`: A ``[width, height]`` pair of integers.
* `def Region() -> Validator`: A normalized ``[left, top, right, bottom]`` box with ``0<=l<r<=1`` and ``0<=t<b<=1``.
* `def check(condition: bool, path: str, message: str) -> None`: Raise :class:`DocumentError` at ``path`` unless ``condition`` holds.

## `mod_base.model.documents`

Owner: MB0 (implemented).

Strict validators for every document kind of SPEC §3 (and the §4.1 config envelope).

Constants:

* `SELECTION_COMPLETION_FIELDS = ('manifest_sha256', 'binding', 'composition')`
* `GALLERY_FAMILY_RELEASE_AVAILABLE = ('coverage_sha', 'contracts', 'links', 'image_policy')`

Field validators (each a `Validator`; frozen for every unit):

* `SHA1`, `SHA256`, `DIGEST`, `KEY`, `FAMILY`, `LANE_ID`, `BRANCH`, `REPOSITORY`, `IDENT`
* `ARTIFACT_NODE`, `MINECRAFT`, `LOADER`, `SCENARIO`, `ROLE`, `STEP`, `REVIEW_TIER`, `PROFILE`
* `EVENT`, `EXTENSION_NAME`, `VERSION`, `WORKFLOW_PATH`, `RUN_ID`, `RUN_ATTEMPT`, `JAVA`, `COUNT`
* `RUN_URL`, `TITLE`, `EXPECTATION_TEXT`, `RUNTIME_EVIDENCE`, `LABEL`, `REASON`, `DISPLAY_TITLE`
* `REUSE`, `JSON_OBJECT`, `TIMESTAMP`, `BUNDLE_PATH`, `REPO_PATH`, `PIXEL_METRICS`, `COMPARE_METRICS`
* `RUN_CLAIM`, `KIT_REF`, `SUBJECT`, `FILE_RECORD`, `IMAGE_POLICY`, `FAMILY_IMAGE_POLICY`, `JARS`
* `EXPECTATION_REF`, `EXTENSIONS_REF`, `SELECTION_REF`, `EVIDENCE_LANE`, `VARIANT_ID`, `VERDICT`
* `PAIR`, `PAIRED_LANE`, `NOT_APPLICABLE`, `LINK_LABEL`, `FAMILY_LINK`, `FAMILY_STATUS`
* `IMPLEMENTATION`, `LINK_ID`, `PROJECT_LINK`, `GALLERY_LANE`, `GALLERY_FRAME`, `GALLERY_RELEASE`
* `GALLERY_FAMILY_RELEASE`, `GALLERY_FAMILY`
* `def DISPLAY(max_len: int) -> Validator`
* `def LABEL_MAP(pattern: Any) -> Validator`

Field tables (`{field: Validator}` objects that documents compose with `Obj`):

* `RUN_CLAIM_FIELDS`: the seven `RunClaim` fields (`run_id`, `run_attempt`, `workflow_path`,
  `branch`, `commit`, `controller_branch`, `controller_sha`).
* `RUN_RECORD_REQUIRED`, `RUN_RECORD_OPTIONAL`: the `RunRecord` fields beyond the claim.
* `LANE_FIELDS`, `CAPTURE_FIELDS`, `FRAME_FIELDS`, `COMPARISON_FIELDS`, `PNG_SOURCE_BASE`
* `PAIRED_LANE_FIELDS`, `NOT_APPLICABLE_FIELDS`
* `WORKFLOW_FILE`: the pattern of a workflow file name (`<name>.yml`/`.yaml`).
* `VALIDATORS`: document kind -> its `validate_*` function (`validate_document`).
* `MAX_DOCUMENT_BYTES`: document kind -> its byte bound (`load_document`).

* `def is_https_url(value: Any) -> bool`: https, lowercase ``[a-z0-9.-]`` host with a dot, no userinfo/port, printable ASCII only.
* `def run_record(value: Any, path: str) -> dict[str, Any]`: A RunRecord (SPEC §3.0): a RunClaim plus facts read from the run API. ``head_sha`` is the run's API head. A run that is its own controller validates with :func:`own_run_record`; a tested run of ``none``/``attested`` reuse has ``head_sha == controller_sha`` (:func:`validate_selection`); only a ``delegated`` tested run's head is unconstrained (Quick Skin PR reuse tests a merge commit that is not the PR run's head; the reuse is proven by the adapter's ``authenticate_extensions``).
* `def own_run_record(value: Any, path: str) -> dict[str, Any]`: A RunRecord of a run that is its own controller (a handoff or family producer run): ``head_sha == commit == controller_sha`` and ``branch == controller_branch`` (SPEC §4.8).
* `def thumbnail_size(source: tuple[int, int] | list[int], box: tuple[int, int] | list[int]) -> tuple[int, int]`: Return the size Pillow 12.3 ``Image.thumbnail(box)`` produces for an image of ``source`` size.
* `def inventory_sha256(records: list[Mapping[str, Any]]) -> str`: Identity of a file inventory: SHA-256 of ``canonical_json`` of ``[{path, sha256, size}]`` sorted by path. Used for ``promotion.site.inventory_sha256`` and ``build.site_inventory_sha256`` (the published site inventory excludes ``build.json`` itself).
* `def run_claim_from_environment(environ: Mapping[str, str]) -> dict[str, Any]`: Build the handoff ``RunClaim`` of the executing job from GitHub's environment (no API call).
* `def validate_expectation(document: Any, *, image_policy: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.expectation`` v1 (SPEC §3.1).
* `def validate_handoff(document: Any, *, expectation: Mapping[str, Any] | None = None, allowed_extensions: Collection[str] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.handoff`` v1 (SPEC §3.2).
* `def validate_compact(document: Any, *, expectation: Mapping[str, Any] | None = None, allowed_extensions: Collection[str] | None = None, intermediate: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.compact`` v1 (SPEC §3.3).
* `def validate_anchor(document: Any, *, expectation: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.evidence.anchor`` v1 (SPEC §3.4).
* `def validate_family_envelope(document: Any, *, max_total_bytes: int = 1048576000, path: str = '$') -> dict[str, Any]`: ``mod-base.family.envelope`` v1 (SPEC §3.5).
* `def validate_family_paired(document: Any, *, image_policy: Mapping[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: ``mod-base.family.paired`` v1 (SPEC §3.5), the projection returned by ``family_validate``.
* `def validate_selection(document: Any, *, draft: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.selection`` v1 (SPEC §3.6).
* `def compact_identity_sha256(manifest: Mapping[str, Any]) -> str`: The selection-independent identity of a compact manifest: ``canonical_sha256`` of the manifest without its ``selection`` member and without the ``selection.json`` record of ``files``.
* `def check_compact_selection(compact: Mapping[str, Any], selection: Mapping[str, Any], *, path: str = '$') -> None`: Bind a published compact manifest to the final selection embedded as its ``selection.json``.
* `def validate_promotion(document: Any, *, draft: bool = False, path: str = '$') -> dict[str, Any]`: ``mod-base.promotion`` v1 (SPEC §3.7).
* `def validate_build(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.build`` v1 (SPEC §3.8): ``run_url`` is built from the repository and run id, and ``workflow_ref`` is this repository's ``pages.yml`` on a branch.
* `def validate_site(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.site`` v1 (SPEC §3.9 ``site-data.json``): unique release keys and families, ``loader_names`` parallel to ``loaders``, ``short_sha`` a prefix of ``subject_commit``.
* `def validate_gallery(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.gallery`` v1 (SPEC §3.9 ``e2e/gallery-data.json``, with the per-key family amendment documented in ``docs/SCHEMAS.md``).
* `def validate_template_manifest(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.template-manifest`` v1 (SPEC §8.1): unique paths; ``managed`` sources live under ``managed/`` and carry no markers/lines; ``fragment`` and ``seeded`` sources live under ``seed/``; only ``fragment`` entries may list required ``markers``/``lines``; only the ``.github/dependabot.yml`` fragment may list ``ignore_actions_of``, each a managed workflow of the same manifest (v1.0.1, optional).
* `def validate_kit_stamp(document: Any, *, path: str = '$') -> dict[str, Any]`: ``mod-base.kit-stamp`` v1 (``out/mod-base-kit/MOD_BASE_KIT.json``, SPEC §1.5).
* `def validate_document(document: Any, *, kind: str | None = None, **context: Any) -> dict[str, Any]`: Validate ``document`` as its declared ``kind`` (which must equal ``kind`` when given).
* `def load_document(data: bytes, *, kind: str, label: str | None = None, max_bytes: int | None = None, **context: Any) -> dict[str, Any]`: Strictly decode ``data`` and validate it as ``kind``.

## `mod_base.adapter.protocol`

Owner: MB0 (implemented).

The adapter protocol: hook names, where they may run, and strict request/response schemas.

Constants:

* `ADAPTER_API_WINDOW = frozenset({1})`
* `HOOKS = frozenset({'anchor_selection', 'authenticate_extensions', 'collect', 'compose', 'expectation', 'expected_so...`
* `FIXTURE_HOOKS = frozenset({'synthesize'})`
* `NETWORK_HOOKS = frozenset({'authenticate_extensions', 'compose', 'verify_publication'})`
* `TOKEN_JOBS = frozenset({'admit', 'build', 'collect', 'family'})`
* `PREPARE_EVIDENCE = 'prepare-evidence'`
* `FORBIDDEN_JOBS = frozenset({'deploy', 'finalize', 'notify-pages', 'refresh', 'refresh-family', 'request-rotation', 'rotate',...`
* `CHILD_FLAGS = ('--adapter', '--hook', '--request', '--response')`
* `REQUEST_KIND = 'mod-base.adapter.request'`
* `RESPONSE_KIND = 'mod-base.adapter.response'`
* `JOB_CONCLUSIONS = ('success', 'failure', 'cancelled', 'skipped', 'neutral', 'timed_out', 'action_required', 'stale')`
* `MAX_ERROR_CHARS = 1000`

Frozen for other units (integration round):

* `ImageFactory`: `Callable[[int, int, int], bytes]`, `image_factory(width, height, seed)` (the
  `synthesize` argument; `imaging.png.pattern_png`).
* `HOOK_JOBS`: hook -> the frozenset of SPEC §4.3 places it may run (Pages callee job ids plus
  `PREPARE_EVIDENCE`), enforced at runtime by `adapter.host.check_placement`.
* `TARGET`, `BRANCH_HEAD`: validators of one `targets` result entry and one `branches` argument
  entry.
* `TESTED_RUN_PROJECTION`: the validator of the `expectation` hook's `tested_run` argument
  (`{event, branch}`).
* `EXTENSION_OBJECTS`: the validator of an `extensions` argument (declared name -> object).
* `ARGUMENTS`, `RESULTS`: hook -> the validator of its arguments object / its result.
* `FIXTURE_ARGUMENTS`: fixture hook -> the validator of its JSON arguments.

* `def require_hook(hook: Any) -> str`
* `def require_fixture_hook(hook: Any) -> str`
* `def validate_fixture_arguments(hook: str, arguments: Any) -> dict[str, Any]`: Validate the arguments of a test-only fixture hook (never a Pages hook).
* `def validate_fixture_result(hook: str, result: Any) -> None`: A fixture hook writes files and returns ``None``.
* `def validate_arguments(hook: str, arguments: Any) -> dict[str, Any]`: Validate the per-hook ``arguments`` object of a request.
* `def validate_result(hook: str, result: Any, arguments: Mapping[str, Any]) -> Any`: Validate a hook ``result`` against its strict schema and the request ``arguments``.
* `def validate_request(document: Any) -> dict[str, Any]`: Validate a request envelope (including its per-hook arguments).
* `def validate_response(document: Any, *, hook: str, arguments: Mapping[str, Any]) -> Any`: Validate a response envelope for ``hook`` and return its checked ``result``.
* `class HookUnsupported(MbError)`: The adapter defines no such hook. Callers fail closed wherever the hook is required.
* `class HookFailed(MbError)`: The hook raised; the adapter's refusal is a fail-closed rejection.

## `mod_base.io.secure_json`

Owner: MB1.

Bounded, race-aware, fail-closed JSON readers used at trust boundaries (MB1).

* `class SecureJsonError(MbError)`: JSON bytes or the file containing them are not trustworthy (exit 2).
* `def loads(data: bytes, *, label: str, max_bytes: int) -> Any`: Decode strict JSON: non-empty bytes of at most ``max_bytes``, UTF-8 without BOM, no duplicate object key, no NaN/Infinity. Raises :class:`SecureJsonError` naming ``label``.
* `def read(path: Path, *, label: str, max_bytes: int) -> tuple[Any, bytes]`: Read one stable regular file (``O_NOFOLLOW``, same dev/inode/size before, during and after the read, 1..``max_bytes`` bytes) and return ``(strictly decoded value, raw bytes)``.
* `def read_json(path: Path, *, label: str, max_bytes: int) -> Any`: ``read(...)[0]``: the decoded value only.
* `def require_object(value: Any, *, label: str, required: set[str] | frozenset[str], optional: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]`: Return ``value`` when it is a dict with every ``required`` key and no key outside ``required | optional``; raise :class:`SecureJsonError` listing missing/unknown keys.
* `def canonical_json(value: Any) -> bytes`: Canonical document bytes: sorted keys, ``(",", ":")``, ``ensure_ascii=False``, ``allow_nan=False``, UTF-8, trailing newline (== ``model.canonical.canonical_json``).

## `mod_base.io.atomic_directory`

Owner: MB1.

Descriptor-bound exclusive directory publication (MB1).

* `class AtomicDirectoryError(MbError)`: An owned output cannot be safely written or published (exit 2).
* `def atomic_directory(output: Path, writer: Callable[[Path, int], T]) -> T`: Create ``output`` atomically: call ``writer(stage_path, stage_fd)`` and publish the stage.
* `def write_new(stage: int, relative: str, data: bytes) -> None`: Create ``relative`` (canonical POSIX path, parents created ``0700``) under the stage descriptor with ``O_CREAT|O_EXCL|O_NOFOLLOW``, write ``data``, fsync, and chmod ``0644``.

## `mod_base.io.content_cache`

Owner: MB1.

Bounded in-process memo for work that is a pure function of exact input bytes (MB1).

* `class ContentCache`: An LRU memo bounded by ``entries`` and optionally by ``max_bytes``.
  * `__init__(self, *, entries: int, max_bytes: int | None = None) -> None`
  * `stored_bytes` (property) -> `int`
  * `clear(self) -> None`
  * `get_or_compute(self, data: bytes, parameters: tuple[Hashable, ...], compute: Callable[[], T], *, size: Callable[[T], int] = lambda _value: 0) -> T`: Return ``compute()`` for these exact bytes and parameters, reusing an earlier success.

## `mod_base.io.seal`

Owner: MB1.

Seal a generated output tree against its written bytes (MB1).

* `class SealError(MbError)`: The generated tree differs from what was written, or changed while being sealed (exit 2).
* `def seal_output(stage_fd: int, expected: Mapping[str, bytes], *, max_files: int = 8192, max_bytes: int = 1073741824, rechecks: list[Callable[[], None]] | None = None) -> tuple[int, int]`: Verify the stage behind ``stage_fd`` holds exactly ``expected`` (relative path -> bytes).

## `mod_base.io.tree`

Owner: MB1.

Bounded regular-file walks and descriptor-relative child reads (MB1).

* `class TreeError(MbError)`: A directory tree is not a bounded tree of regular files (exit 2).
* `class PathRule`: The entry paths one tree may hold, relative to its root (never a link or a special file).
  * fields: `name: str, is_safe: Callable[[object], bool], aliases: bool = False`
* `BUNDLE_PATHS`, `REPO_PATHS`, `EXPORT_PATHS`, `SEED_PATHS`: the four rules a walk can be given through `rule`. `BUNDLE_PATHS` (`grammar.is_bundle_path`; Pages bundles and every other kit-named tree) is the default of the nonempty-file functions and `REPO_PATHS` (`grammar.is_repo_path`, no entry differing from another only in case) the default of the `regular_data` functions. `EXPORT_PATHS` (`grammar.is_export_path`, no case alias) is the rule of sealed CI exports, which keep the mod's own file names; `SEED_PATHS` (`grammar.is_seed_path`, case aliases allowed) checks structure only, for Gradle seeds.
* `def regular_files(root: Path, *, max_files: int, max_total_bytes: int, max_file_bytes: int, suffixes: Collection[str] | None = None, rule: PathRule = BUNDLE_PATHS) -> dict[str, int]`: Return ``{relative POSIX path: size}`` for every file under ``root`` (sorted by path). Every entry path must be one ``rule`` admits.
* `def reject_symlinks(root: Path) -> None`: Raise :class:`TreeError` if any entry at or under ``root`` is a symlink or special file.
* `def read_child_file(root: Path, relative: str, *, max_bytes: int) -> bytes`: Read ``root/relative`` walking every component through ``O_NOFOLLOW`` directory descriptors (no symlink anywhere), stat-stable, 1..``max_bytes`` bytes.
* `def stream_child_file(root: Path, relative: str, *, max_bytes: int, consume: Callable[[bytes], None], rule: PathRule = BUNDLE_PATHS) -> int`: Stream bounded chunks from a stat-stable single-link regular child through no-follow directory descriptors to a protected consumer, returning verified size. No whole-file allocation; caller owns root ancestry and multi-file inventory rechecks. ``relative`` must be a path ``rule`` admits (``EXPORT_PATHS`` for a sealed export's own files).
* `def sha256_file(path: Path, *, max_bytes: int) -> str`: SHA-256 hex of one stable regular file of at most ``max_bytes`` (streamed, ``O_NOFOLLOW``).
* `def file_records(root: Path, *, exclude: Collection[str] = (), max_files: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]`: Return the exact inventory ``[{path, sha256, size}]`` of ``root`` sorted by path, leaving out the relative paths in ``exclude`` (for example ``manifest.json``). Every entry and excluded path must be one ``rule`` admits.
* `def privatize_source_copy(root: Path, *, tracked_paths: tuple[str, ...], source_owner_uid: int, owner_uid: int, owner_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, max_link_bytes: int) -> list[dict[str, Any]]`: Protected Linux Root handoff of a fresh independent tracked-source copy. Require protected-owned private root and complete closed declared file/parent closure; reject opaque Git metadata and undeclared/special/hard-linked entries before mutation. Admit exact source bytes/Git modes, remove directory/file ACLs, assign private 0700 directories/executable files and 0600 nonexecutables; preserve literal link bytes and change symlink ownership without following targets. Recheck bounded metadata/ACLs, source records and held/named identity, transfer root last. Caller authenticates copy origin/accounts/ancestors and excluded writers; never apply to candidate originals or treat targets as privileged authority.
* `def source_records(root: Path, *, tracked_paths: Collection[str], generated_roots: Collection[str], max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, max_link_bytes: int) -> list[dict[str, Any]]`: Inspect protected declared source leaves without following links; preserve empty files, Git modes, literal symlink bytes, SHA-256 and Git blob identity. Reject undeclared paths except protected generated roots; root .git remains opaque pending protected replacement.
* `def copy_regular_files(root: Path, stage_fd: int, *, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]`: Count entries before reading content, then stream exact single-link regular-file bytes into an empty caller-owned private stage, rechecking the source inventory under ``rule``. Creates independent files and omits empty directories; caller must exclude source writers and protect output ancestors.
* `def copy_selected_regular_files(root: Path, stage_fd: int, *, paths: tuple[str, ...], max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]`: Append exact bounded sorted unique canonical selected paths (paths ``rule`` admits) to a caller-owned private stage with no-follow exclusive creation and independent streamed files. Inspect and recheck the entire bounded source including unselected files; no replacement or linking. Caller protects both trees, authenticates/bounds the complete destination union and leaves partial failures unpublished.
* `def regular_data_records(root: Path, *, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]`: Return sorted path/sha256/size records for regular data, including zero-byte files. Bound the entire directory closure before hashing; empty directories count toward the entry cap. Reject links, special files and every path ``rule`` refuses: by default case aliases, Git metadata and unsafe repository paths; with ``SEED_PATHS`` only structurally unsafe ones. Does not approve native artifacts or executable code.
* `def selected_regular_data_records(root: Path, *, paths: tuple[str, ...], max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int) -> list[dict[str, Any]]`: Hash only bounded sorted unique declared regular data leaves, including zero-byte files. Bound the entire no-follow source closure before selected content reads; selected paths must be canonical repository paths, files single-link and stable with exact total/file caps. Unselected contents are not read. Caller admits selection/provenance, ancestry and excluded writers.
* `def copy_selected_regular_data_files(root: Path, stage_fd: int, *, paths: tuple[str, ...], max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int) -> list[dict[str, Any]]`: Stream declared regular data leaves into an empty caller-owned private stage, preserving zero-byte files and independently created inodes; never read unselected content. Recheck selected source inventories around copying. Full no-follow source closure is bounded; caller owns protected selection, ancestry and excluded writers. Existing selected export-copy API retains its old nonempty/whole-inventory behavior.
* `def copy_regular_data_files(root: Path, stage_fd: int, *, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]`: Stream regular data into an empty caller-owned private stage; preserve empty files, omit empty directories, and recheck source inventory under ``rule``. Caller excludes writers and independently admits ancestry and any required directory skeleton. Existing export copy APIs retain their nonempty-file contract.
* `def validate_tree_entries(root: Path, *, max_entries: int) -> None`: Bound the complete no-follow regular-file/directory closure including the root before reading content; file size, hard-link and hash admission remain separate.
* `def grant_tree_read_access(root: Path, *, source_owner_uid: int, owner_uid: int, reader_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]`: Protected Linux-root-only permission handoff of a fresh independent private tree. Require one protected original owner, remove inherited POSIX ACLs, bind nonprivileged owner/read group, verify bytes and expose the 0750 root last. Failure restores private root traversal. Every entry path must be one ``rule`` admits. Caller authenticates identities/ancestors and excludes writers; this does not reclaim a candidate original or authorize upload.
* `def grant_regular_data_read_access(root: Path, *, source_owner_uid: int, owner_uid: int, reader_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]`: Additive bounded regular-data permission handoff preserving empty files (0750 directories, 0640 files), through the same protected Linux Root/owner/ancestor admission and ACL/root-last transfer. Independently compare all data records before/after, with supplied file/entry/byte caps; the one ``rule`` admits every entry and bounds the inventory. Preserve existing nonempty/source contracts. Private independent copy and excluded writers remain prerequisites; never apply to worker originals; no candidate reclamation/native validity/upload authority is established.
* `def grant_source_read_access(root: Path, *, tracked_paths: tuple[str, ...], source_owner_uid: int, owner_uid: int, reader_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int) -> list[dict[str, Any]]`: Apply the same protected-root permission transfer to an exact regular-source inventory, allowing empty files and repository paths, excluding Git metadata and links. Strip executable modes and recheck exact path/size/SHA-256/Git-blob records before exposing root traversal. Protected ownership/ancestors and excluded writers remain caller obligations.
* `def authenticate_tree_read_access(root: Path, *, owner_uid: int, reader_gid: int, max_entries: int) -> None`: Linux read-only metadata recheck of exact 0750 directories/0640 single-link regular files, expected owner/group and absent access/default ACLs, through a bounded no-follow walk. Byte and ancestor authentication remain separate caller requirements.
* `def authenticate_tree_private_access(root: Path, *, owner_uid: int, owner_gid: int, max_entries: int) -> None`: Linux no-follow metadata recheck of exact 0700 directories/0600 single-link regular files, owner/group and absent ACLs. Root ownership is allowed for a fresh protected copy; caller separately authenticates role/copy origin, ancestors and bytes.
* `def privatize_tree_copy(root: Path, *, source_owner_uid: int, owner_uid: int, owner_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = BUNDLE_PATHS) -> list[dict[str, Any]]`: Protected Linux-root-only transfer of an independent private copy to a non-root private owner. Require one protected original owner, remove ACLs, assign 0700 dirs/0600 files, verify unchanged content and transfer root last. Every entry path must be one ``rule`` admits and never Git metadata. Caller authenticates provenance/identities/ancestors/excluded writers; never reclaim a worker original through this helper.
* `def privatize_regular_data_copy(root: Path, *, source_owner_uid: int, owner_uid: int, owner_gid: int, max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, rule: PathRule = REPO_PATHS) -> list[dict[str, Any]]`: Additive private independent-copy transfer preserving empty regular data through the existing protected Linux Root/owner/ancestor/ACL/root-last 0700/0600 mechanics. Recheck complete bounded data before/after; the one ``rule`` admits every entry and bounds the inventory. Original copy provenance and excluded writers remain mandatory. Never change a candidate original or infer native/execution/upload authority from this transfer. Existing nonempty/source contracts remain unchanged.
* `def copy_source_files(root: Path, stage_fd: int, *, tracked_paths: Collection[str], max_files: int, max_entries: int, max_total_bytes: int, max_file_bytes: int, max_link_bytes: int) -> list[dict[str, Any]]`: Copy clean declared source into an empty private descriptor-bound stage; stream bounded bytes, preserve tracked link bytes and executable modes, omit Git metadata, and recheck source identity before returning.

## `mod_base.io.bounded_zip`

Owner: MB1.

Bounded ZIP extraction (MB1).

* `class ZipRejected(MbError)`: An archive violates the extraction policy (exit 2).
* `class ExtractionLimits`: Bounds for one archive. ``suffixes`` (when set) restricts every file name's extension.
  * fields: `max_entries: int, max_total_bytes: int, max_entry_bytes: int, max_ratio: int = 200, suffixes: frozenset[str] | None = None`
* `def extract(archive: Path | bytes, destination: Path, limits_: ExtractionLimits) -> list[str]`: Validate and extract ``archive`` into the new directory ``destination``. Every entry name is a canonical bundle path (`tree.BUNDLE_PATHS`): the Pages artifacts, whose file names the kit chooses.
* `def extract_build(archive: Path | bytes, destination: Path) -> list[str]`: Extract with fixed CI export entry/file/expanded bounds through the same hostile ZIP validator and exclusive publication. Entry names are export paths (`tree.EXPORT_PATHS`, the mod's own staged file names); names that differ only in case are refused as on every route. Existing Pages extract/archive_limit entry ceilings remain unchanged.
* `def extract_runtime(archive: Path | bytes, destination: Path, *, scope: str) -> list[str]`: Extract closed lane/complete runtime data under fixed native payload file/expanded/per-entry and compressed bounds, preserving empty logs only on this route. Entry names are export paths (`tree.EXPORT_PATHS`). Shared complete hostile ZIP checks and exclusive publication remain; exact canonical envelope/bytes/native role admission is separately mandatory. Existing Pages/Build nonempty-file contracts and limits are unchanged.

Frozen for other units (integration round):

* `LIMITS_BY_KIND`: artifact kind (`handoff`, `anchor`, `cache`, `collected`, `baseline`,
  `family-handoff`, `family-cache`, `collected-family`, `promotion`) -> its `ExtractionLimits`, built
  only from `mod_base.model.limits`.
* `def archive_limit(limits_: ExtractionLimits) -> int`: The largest archive (compressed bytes) ``limits_`` accept: stored data plus headers and slack.
* `def artifact_limit(kind: str, *, max_total_bytes: int | None = None) -> int`: The largest artifact (archive bytes) of ``kind`` that a kit job admits, selects or downloads (see the archive-cap amendment).

## `mod_base.github.api`

Owner: MB1.

The kit's only GitHub REST client (MB1).

Constants:

* `DEFAULT_BASE_URL = 'https://api.github.com'`
* `API_VERSION = '2022-11-28'`
* `USER_AGENT = 'mod-base'`
* `REQUEST_ATTEMPTS = 4`
* `RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})`
* `MAX_RETRY_DELAY_SECONDS = 30.0`
* `REQUEST_TIMEOUT_SECONDS = 30`
* `PER_PAGE = 100`
* `MAX_RESPONSE_BYTES = 33554432`

* `class ApiError(MbError)`: A GitHub request failed after its retry budget. ``status`` is 0 for transport errors.
  * `__init__(self, message: str, *, status: int, method: str, path: str) -> None`
* `class ApiNotFound(ApiError)`: HTTP 404 (never retried).
* `class ApiRateLimited(ApiError)`: The retry budget ended on a rate-limit response.
* `class InconsistentListing(ApiError)`: A listing that is not one consistent snapshot: its rows disagree with its ``total_count``, the ``total_count`` changed between pages or a row repeats. :func:`read_consistently` reads such a listing again; it escapes only after ``limits.LISTING_READ_ATTEMPTS`` inconsistent reads.
* `class ReadOnlyViolation(MbError)`: A non-GET request was attempted on a read-only client.
* `class RequestBudgetExhausted(MbError)`: The client's ``max_requests`` budget is spent (for example Pages' 160 reads).
* `class GitHubApi`: A repository-scoped client. ``repository`` is ``owner/name``; paths passed to methods are absolute API paths starting with ``/`` (for example ``/repos/o/r/actions/runs/1``).
  * `__init__(self, *, repository: str, token: str | None, base_url: str = 'https://api.github.com', writable: bool = False, max_requests: int | None = None, sleep: Callable[[float], None] = sleep) -> None`
  * `repository` (property) -> `str`
  * `writable` (property) -> `bool`
  * `request_count` (property) -> `int`
  * `get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any`: GET ``path`` and return strictly decoded JSON (``model.canonical.strict_loads``).
  * `paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None, max_items: int) -> list[dict[str, Any]]`: GET every page (``per_page=100``) until a short page (a full page reaching ``total_count`` is confirmed by the next) and return the concatenated ``field`` arrays (or the top-level arrays when ``field`` is None). More than ``max_items`` rows or a non-object row raises :class:`ApiError`; a snapshot whose ``total_count`` disagrees with the rows (or changes between pages), or whose rows repeat an ``id``, is read again from page 1 and raises :class:`InconsistentListing` only after ``limits.LISTING_READ_ATTEMPTS`` such reads.
  * `read_listing(self, read: Callable[[], _T]) -> _T`: ``read()`` (one complete listing through this client) re-run by :func:`read_consistently` with this client's ``sleep`` until it raises no :class:`InconsistentListing`.
  * `post_json(self, path: str, payload: Mapping[str, Any]) -> Any`: POST canonical JSON; requires ``writable``. Returns decoded JSON or None for 204.
  * `patch_json(self, path: str, payload: Mapping[str, Any]) -> Any`: PATCH canonical JSON; requires ``writable``. Returns the decoded JSON of the 200 answer; any other status raises.
  * `delete(self, path: str) -> None`: DELETE ``path``; requires ``writable``. 204 is success; anything else raises.
  * `download(self, path: str, *, max_bytes: int) -> bytes`: GET a binary endpoint (artifact ZIP) that answers with one redirect: the redirect is followed exactly once to an https URL with the ``Authorization`` header stripped; the body is read to at most ``max_bytes``.
  * `rate_limit_snapshot(self) -> dict[str, int]`: ``GET /rate_limit`` projected to numeric ``core`` counters: ``limit``, ``used``, ``remaining``, ``reset`` (the ``budget`` command; never tokens or headers).
* `def listing_retry_delay(attempt: int) -> float`: The wait before re-reading an inconsistent listing after its read ``attempt`` (0-based): ``limits.LISTING_RETRY_DELAY_SECONDS`` doubling per re-read, at most ``limits.MAX_LISTING_RETRY_DELAY_SECONDS``.
* `def read_consistently(read: Callable[[], _T], *, sleep: Callable[[float], None]) -> _T`: ``read()``, one complete listing from its first page, until it returns a consistent snapshot: an :class:`InconsistentListing` is read again after :func:`listing_retry_delay`, at most ``limits.LISTING_READ_ATTEMPTS`` times in all, and the last one fails closed; every read spends the client's request budget.
* `def from_environment(environ: Mapping[str, str], *, writable: bool = False, max_requests: int | None = None) -> GitHubApi`: Build a client from ``GITHUB_REPOSITORY``, ``GH_TOKEN``/``GITHUB_TOKEN`` and ``GITHUB_API_URL`` (default :data:`DEFAULT_BASE_URL`); a missing token or repository raises.

## `mod_base.github.runs`

Owner: MB1.

Workflow-run reads and exact run validation (MB1).

* `def get_run(api: GitHubApi, run_id: int) -> dict[str, Any]`: ``GET /repos/{repo}/actions/runs/{run_id}`` (an object or :class:`ApiError`).
* `def get_run_attempt(api: GitHubApi, run_id: int, run_attempt: int) -> dict[str, Any]`: The historical attempt endpoint; its ``run_attempt`` must equal ``run_attempt``.
* `def validate_run(run: Mapping[str, Any], *, repository: str, workflow_path: str, events: Collection[str], head_branch: str | None = None, head_sha: str | None = None, workflow_id: int | None = None, require_success: bool = True, display_title: str | None = None) -> None`: Require exact provenance: ``path``, ``event`` in ``events``, ``head_repository.full_name``, and (when given) ``head_branch``, ``head_sha``, ``workflow_id``, ``display_title``; with ``require_success`` also ``status == "completed"`` and ``conclusion == "success"``. Raises :class:`mod_base.errors.MbError` on any difference.
* `def run_order(run: Mapping[str, Any]) -> tuple[datetime, int, int]`: ``(created_at, id, run_attempt)`` after strict shape validation (a total dispatch order).
* `def referenced_kit_sha(run: Mapping[str, Any], *, kit_repository: str = 'The-Plum-Team/mod-base') -> str`: The single kit SHA a run resolved: every ``referenced_workflows[]`` entry whose ``path`` starts with ``<kit_repository>/.github/workflows/`` must end in ``@<sha>`` equal to its ``sha``, and exactly one distinct 40-hex SHA must result (SPEC §1.2 step 3).
* `def referenced_workflows(run: Mapping[str, Any]) -> list[tuple[str, str]]`: Every ``referenced_workflows[]`` entry as ``(workflow, sha)`` in listing order: ``workflow`` is ``<owner>/<repo>/.github/workflows/<file>`` and ``sha`` the 40-hex commit the run resolved it at. An entry not pinned to its own SHA, or malformed, raises. GitHub lists a called workflow whether or not its calling job ran, in no fixed order.
* `def workflow_runs(api: GitHubApi, workflow_path: str, *, branch: str | None = None, head_sha: str | None = None, event: str | None = None, status: str | None = None, max_items: int = 1000) -> list[dict[str, Any]]`: List runs of ``workflow_path`` (by file name) newest first with the given filters; the response ``total_count`` must equal the listed rows when it is at most ``max_items``. Beyond ``max_items`` runs, or beyond the ``limits.MAX_FILTERED_RUNS_LISTED`` newest runs GitHub lists for a filtered search, the read must list exactly that many rows; otherwise only a short page ends it. A snapshot whose ``total_count`` changes between pages or disagrees with its rows, or that repeats a run, is read again through ``api.read_listing`` (``limits.LISTING_READ_ATTEMPTS`` reads at most).
* `def wait_for_completion(api: GitHubApi, run_id: int, *, attempts: int = 30, interval: float = 2.0, sleep: Callable[[float], None] = sleep) -> dict[str, Any]`: Poll a run until ``status == "completed"`` (at most ``attempts`` reads); raise otherwise.
* `def run_record(run: Mapping[str, Any], claim: Mapping[str, Any], *, require_controller_head: bool = True) -> dict[str, Any]`: Build the ``RunRecord`` (SPEC §3.0) for an authenticated API ``run`` and its ``RunClaim``: the claim's run id/attempt/workflow path must equal the run's; ``head_sha``, ``event``, ``created_at``, ``conclusion`` (``success``) and ``display_title`` come from the run. The result validates as ``documents.run_record``.

## `mod_base.github.jobs`

Owner: MB1.

Exact job lookup, attempt-scoped listing and generic job-graph hashing (MB1).

* `def attempt_jobs(api: GitHubApi, run_id: int, run_attempt: int) -> list[dict[str, Any]]`: Every job of exactly one attempt (``/runs/{id}/attempts/{n}/jobs``, paginated, bounded by ``limits.MAX_JOBS_PER_ATTEMPT``); job ids must be unique and each job's ``run_attempt`` equal.
* `def job_graph(jobs: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]`: The canonical ``[{name, conclusion}]`` of ``jobs`` sorted by name (names must be unique).
* `def job_graph_sha256(graph: Sequence[Mapping[str, str]]) -> str`: SHA-256 of ``canonical_json(graph)``.
* `def require_job_graph(jobs: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> str`: Require the observed graph to equal ``expected`` exactly (adapter ``expected_source_jobs``) and return its SHA-256; any missing, extra or differently concluded job raises.
* `def step_window(job: Mapping[str, Any], step_name: str) -> tuple[datetime, datetime]`: ``(started_at, completed_at)`` of the single step named exactly ``step_name`` in ``job``.
* `def require_successful_step(job: Mapping[str, Any], step_name: str) -> dict[str, Any]`: The single step named exactly ``step_name``, which must be ``completed``/``success``.

Frozen for other units (integration round):

* `def actions_time(value: Any, label: str) -> datetime`: Parse an Actions job/step timestamp into an aware UTC datetime.

## `mod_base.github.artifacts`

Owner: MB1.

Artifact model, bounded listings, verified download and exact-ID deletion (MB1).

* `class Artifact`: One Actions artifact as reported by the API (``workflow_run`` flattened).
  * fields: `id: int, name: str, size: int, expired: bool, created_at: str, digest: str, run_id: int, head_branch: str, head_sha: str`
  * `order` (property) -> `tuple[datetime, int]`
  * `@classmethod parse(cls, raw: Any) -> 'Artifact'`
* `def get_artifact(api: GitHubApi, artifact_id: int) -> Artifact`
* `def list_named(api: GitHubApi, name: str, *, max_items: int = 512) -> list[Artifact]`: Artifacts with exactly ``name`` (``?name=``), every row re-checked to carry that name.
* `def list_for_run(api: GitHubApi, run_id: int, *, max_items: int = 512) -> list[Artifact]`: Artifacts of one run; every row's ``run_id`` must equal ``run_id``.
* `def list_run_named(api: GitHubApi, run_id: int, name: str, *, max_items: int = 512) -> list[Artifact]`: Artifacts of one run with exactly ``name`` (``/runs/{id}/artifacts?name=``), every row re-checked to carry that name and owner run; the run's other uploads (a sibling job's concurrent cache upload) are never part of the listing.
* `def list_repository(api: GitHubApi, *, max_items: int) -> list[Artifact]`: Every repository artifact, newest first, bounded by ``max_items`` (fail closed beyond).
* `def download(api: GitHubApi, *, artifact_id: int, name: str, digest: str, size: int, run_id: int, output: Path, extraction: ExtractionLimits) -> list[str]`: Download artifact ``artifact_id`` into the new directory ``output`` after re-reading its metadata and requiring exactly ``name``, ``digest``, ``size``, owner ``run_id`` and not expired; the ZIP bytes must hash to ``digest``. Returns the extracted relative paths.
* `def delete(api: GitHubApi, artifact_id: int) -> None`: ``DELETE`` one artifact by id (writable client only; 404 raises, never "already gone").

Frozen for other units (integration round):

* `MAX_ARCHIVE_BYTES`: the largest artifact archive `download` fetches, `limits.MAX_ARTIFACT_BYTES` (1 GiB + 32 MiB, see the archive-cap amendment); a downloader also bounds it by `bounded_zip.archive_limit`.

## `mod_base.github.contents`

Owner: MB1.

Repository contents, Git objects and reachability reads (MB1).

* `def default_branch(api: GitHubApi) -> str`: ``GET /repos/{repo}`` ``.default_branch`` (validated branch grammar).
* `def branch_head(api: GitHubApi, branch: str) -> tuple[str, str]`: ``(commit, tree)`` of the live head of ``branch``.
* `def commit_tree(api: GitHubApi, commit: str) -> str`: The tree SHA of ``commit`` (``/git/commits/{sha}``).
* `def file_at(api: GitHubApi, path: str, ref: str, *, max_bytes: int) -> bytes`: Bytes of ``path`` at commit ``ref`` (contents API, base64 decoded strictly, size and Git blob SHA re-verified).
* `def tree(api: GitHubApi, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]`: Entries of a Git tree; ``truncated`` must be false.
* `def exact_tree(api: GitHubApi, sha: str, *, recursive: bool = True) -> list[dict[str, Any]]`: Require the exact requested tree SHA with all existing bounded complete-tree entry validation; never use the legacy commit-alias fallback.
* `def blob(api: GitHubApi, oid: str, *, max_bytes: int) -> bytes`: A Git blob by id, base64-decoded, with its Git object id recomputed and compared.
* `def compare(api: GitHubApi, base: str, head: str, *, repository: str | None = None) -> dict[str, Any]`: ``GET /repos/{repository or api.repository}/compare/{base}...{head}`` projected to ``{status, ahead_by, behind_by}`` with validated types.

## `mod_base.github.fake`

Owner: MB1.

An in-memory GitHub for tests and ``conformance`` (MB1).

* `class FakeGitHub`: Duck-typed stand-in for ``GitHubApi``; seed it, then pass it where a client is expected.
  * `__init__(self, *, repository: str, default_branch: str = 'master', writable: bool = False, max_requests: int | None = None, sleep: Callable[[float], None] | None = None) -> None`
  * `set_branch(self, name: str, commit: str, tree: str) -> None`
  * `add_run(self, run: Mapping[str, Any], *, attempts: list[Mapping[str, Any]] | None = None) -> None`
  * `add_jobs(self, run_id: int, run_attempt: int, jobs: list[Mapping[str, Any]]) -> None`
  * `add_artifact(self, record: Mapping[str, Any], archive: bytes) -> None`
  * `add_file(self, ref: str, path: str, data: bytes) -> None`
  * `add_compare(self, base: str, head: str, result: Mapping[str, Any], *, repository: str | None = None) -> None`
  * `add_commit(self, sha: str, tree: str, *, parents: Sequence[str] = (), repository: str | None = None) -> None`: Seed ``/git/commits/{sha}`` (``contents.commit_tree``) with its tree and parents.
  * `add_tree(self, sha: str, entries: Sequence[Mapping[str, Any]], *, truncated: bool = False, repository: str | None = None) -> None`: Seed ``/git/trees/{sha}`` (``contents.tree``, ``tools/verify_action_tree.py``): ``entries`` are the API's ``{path, mode, type, sha, size?}`` rows; ``truncated`` seeds a truncated listing, which consumers must refuse.
  * `add_blob(self, data: bytes, *, oid: str | None = None, repository: str | None = None) -> str`: Seed ``/git/blobs/{oid}`` (``contents.blob``) and return its oid: the Git blob id of ``data`` unless ``oid`` forces another one (a corrupt object the reader must reject).
  * `add_ref(self, ref: str, sha: str, *, annotated_tag_sha: str | None = None, repository: str | None = None) -> None`: Seed ``/git/ref/{ref}`` (``ref`` like ``tags/v1.0.0`` or ``heads/main``). With ``annotated_tag_sha`` the ref points at that tag object, which peels to ``sha`` through ``/git/tags/{annotated_tag_sha}`` (``pin.verify``'s tag peel).
  * `add_pull(self, record: Mapping[str, Any]) -> None`: Seed (or replace) ``/repos/{own}/pulls/{number}``: one pull request as the API reports it, kept verbatim. ``POST /pulls`` creates the next number from a seeded head ref and base branch, ``PATCH /pulls/{number}`` changes ``state``, ``title`` or ``body``, ``POST /issues/{number}/comments`` answers for a seeded pull request and ``DELETE /git/refs/{ref}`` removes a seeded ref (with its branch); each needs a writable fake and is recorded in ``mutations``.
  * `add_response(self, path: str, payload: Any, *, params: Mapping[str, str | int] | None = None) -> None`: Seed the exact JSON body of one GET ``path`` (with exactly ``params``) that no typed seeder covers; a request for an unseeded path answers 404 like the API.
  * `skew_listing(self, path: str, *, responses: int, offset: int = 1) -> None`: Serve the next ``responses`` responses of the listing ``path`` (any parameters) with a ``total_count`` ``offset`` rows off the rows it lists, as GitHub's eventually consistent listing does while a sibling job uploads.
  * `during_listing(self, path: str, action: Callable[[], None], *, after_pages: int = 1) -> None`: Run ``action`` once, right after the listing ``path`` served its ``after_pages``-th response from now: an upload or deletion landing between two pages of one read.
  * `repository` (property) -> `str`
  * `writable` (property) -> `bool`
  * `request_count` (property) -> `int`
  * `deleted_artifact_ids` (property) -> `list[int]`
  * `sleeps` (property) -> `list[float]`
  * `get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any`
  * `paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None, max_items: int) -> list[dict[str, Any]]`
  * `read_listing(self, read: Callable[[], _T]) -> _T`
  * `post_json(self, path: str, payload: Mapping[str, Any]) -> Any`
  * `patch_json(self, path: str, payload: Mapping[str, Any]) -> Any`
  * `delete(self, path: str) -> None`
  * `download(self, path: str, *, max_bytes: int) -> bytes`
  * `rate_limit_snapshot(self) -> dict[str, int]`

## `mod_base.github.commands`

Owner: MB1 (register() implemented by MB0; handlers dispatch to the entry points).

``download`` and ``budget`` (MB1). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_download(args: argparse.Namespace) -> int`
* `def run_budget(args: argparse.Namespace) -> int`

## `mod_base.imaging.metrics`

Owner: MB2.

The 8-key PixelMetrics inspection (MB2), ``pixel_metrics_version`` 1.

Constants:

* `SAMPLE_SIZE = (160, 90)`
* `MIN_LUMA_ENTROPY = 0.75`
* `MIN_CHANNEL_STDDEV = 2.0`
* `MIN_MEANINGFUL_COLORS = 4`
* `MAX_DARK_FRACTION = 0.98`
* `MAX_LIGHT_FRACTION = 0.995`
* `QUANTIZE_COLORS = 32`
* `MAX_IMAGE_PIXELS = 20000000`
* `METRIC_KEYS = ('width', 'height', 'file_sha256', 'pixel_sha256', 'luma_entropy', 'meaningful_colors', 'dark_fraction', 'l...`

* `class ImageError(MbError)`: An image is corrupt, of the wrong format or size, blank, or differs from its record.
* `class SizePolicy`: ``exact``: dimensions must equal ``(width, height)``; ``minimum``: at least that size.
  * fields: `mode: str, width: int, height: int`
  * `@classmethod exact(cls, width: int, height: int) -> 'SizePolicy'`
  * `@classmethod minimum(cls, width: int, height: int) -> 'SizePolicy'`
  * `allows(self, width: int, height: int) -> bool`: True when an image of ``width`` x ``height`` satisfies this policy and the pixel bound.
* `def inspect_png(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]`: Decode a PNG (format must be ``PNG``) and return its PixelMetrics; raise :class:`ImageError` when it is not a PNG, violates ``policy`` or is effectively blank.
* `def inspect_webp(source: Path | bytes, policy: SizePolicy) -> dict[str, Any]`: The same inspection for a WebP derivative (format must be ``WEBP``).

## `mod_base.imaging.compare`

Owner: MB2.

Pairwise screenshot comparison (MB2): ``compare_screenshots``, AST-identical in both mods.

Constants:

* `CHANGED_LUMA_THRESHOLD = 8`

* `def compare(first: Path | bytes, second: Path | bytes, *, minimum_changed_fraction: float, region: Sequence[float] | None = None) -> dict[str, Any]`: Return the CompareMetrics of ``first`` -> ``second`` or raise ``ImageError``.

## `mod_base.imaging.png`

Owner: MB2.

Canonical metadata-free RGB PNG re-encoding (MB2).

* `def canonical_png(source: Path | bytes, *, policy: SizePolicy | None = None) -> bytes`: Return the canonical PNG bytes of ``source`` (a PNG); ``policy`` optionally gates its size. Raises :class:`mod_base.imaging.metrics.ImageError` for anything that is not a valid PNG.
* `def pattern_png(width: int, height: int, seed: int = 0) -> bytes`: Deterministic RGB PNG bytes of ``width`` x ``height`` (at least 8x4) that pass the 8-metric blank checks at every size, for synthetic evidence (``protocol.ImageFactory``).

## `mod_base.imaging.webp`

Owner: MB2.

Deterministic WebP derivatives (MB2).

* `def derive_webp(source: Path | bytes, *, box: Sequence[int], quality: int, method: int) -> bytes`: Return the WebP derivative bytes of the PNG ``source`` (see module docstring).

## `mod_base.adapter.api`

Owner: MB3.

The ``ctx`` object every adapter hook receives (MB3), constructed by the child process.

* `class RuntimeTree`: A bounded, regular-file-only, symlink-refusing view of a runtime directory. Paths are canonical relative POSIX paths; every read is bounded and stat-stable.
  * `__init__(self, root: Path, *, max_files: int, max_total_bytes: int) -> None`
  * `root` (property) -> `Path`
  * `files(self) -> list[str]`: Every regular file, sorted (the walk enforces the bounds).
  * `exists(self, relative: str) -> bool`
  * `read_bytes(self, relative: str, *, max_bytes: int) -> bytes`
  * `read_json(self, relative: str, *, max_bytes: int) -> Any`: Strict JSON (duplicate keys and non-finite numbers refused).
  * `path(self, relative: str) -> Path`: The absolute path of an existing regular file (for image inspection).
* `class Context`: Passed as ``ctx`` to every hook.
  * fields: `repo_root: Path, config: Config, tmpdir: Path, implementation_sha: str, api: GitHubApi | None = None`
  * `read_blob(self, commit: str, path: str, max_bytes: int) -> bytes`: Bytes of ``path`` at ``commit`` from the local object store (``git -C repo_root cat-file`` with a sanitized environment); the object must already be present (fetched as an inert object) and be a blob no larger than ``max_bytes``. Never checks anything out.
  * `runtime_tree(self, root: str | Path) -> RuntimeTree`: A :class:`RuntimeTree` over ``root`` with the kit's runtime bounds.
  * `image_metrics(self, path: str | Path, size_policy: SizePolicy | tuple[str, int, int]) -> dict[str, Any]`: The kit's PixelMetrics for a PNG (``imaging.metrics.inspect_png``).

## `mod_base.adapter.host`

Owner: MB3.

Parent side of the adapter call (MB3, SPEC §1.9).

Constants:

* `CHILD_MODULE = 'mod_base.adapter.host_child'`
* `BASE_PATH = '/usr/bin:/bin'`

* `def adapter_pythonpath(invocation: Invocation) -> str`: The child's ``PYTHONPATH``: ``<kit>/src`` then each ``config.adapter.python_path`` entry resolved inside the repository (no ``..``, no symlink component), joined with ``:``.
* `def child_environment(invocation: Invocation, hook: str, *, tmpdir: Path) -> dict[str, str]`: The exact ``env -i`` environment for ``hook`` (see module docstring); the token appears only for a declared network hook in a token job, and ``PYTHONUSERBASE`` replaces ``PYTHONNOUSERSITE`` only as :func:`imaging_user_site` says.
* `def child_argv(invocation: Invocation, hook: str, *, request: Path, response: Path) -> list[str]`: The exact child argv: ``[python3, -P, -m, host_child, --adapter, A, --hook, H, --request, R, --response, S]``.
* `def call(invocation: Invocation, hook: str, arguments: Mapping[str, Any], *, network: bool = False) -> Any`: Run ``hook`` with ``arguments`` in the isolated child and return its validated result.

Frozen for other units (integration round):

* `MAX_CHILD_OUTPUT_BYTES = 4194304`
* `def placement(invocation: Invocation) -> str`: Where this process runs in SPEC §4.3 terms: a Pages callee job id, or ``protocol.PREPARE_EVIDENCE`` for every mod-owned job (and a local run).
* `def check_placement(invocation: Invocation, hook: str, *, network: bool = False) -> None`: Refuse ``hook`` where SPEC §4.3 does not allow it: in a ``protocol.FORBIDDEN_JOBS`` job, in a job outside its ``protocol.HOOK_JOBS`` row (see :func:`placement`), or with ``network`` where the read-only token may not be granted. Runs before any child starts; the in-process test host applies the same check.

Frozen for other units (v1.0.2):

* `def imaging_user_site() -> dict[str, str]`: ``{"PYTHONUSERBASE": <user base>}`` when this process imports Pillow from its own user site, otherwise ``{}``. The hook child and `conformance.run`'s simulation child then omit ``PYTHONNOUSERSITE`` and set ``PYTHONUSERBASE`` instead; see the v1.0.2 amendment.

## `mod_base.adapter.host_child`

Owner: MB3.

Child side of the adapter call (MB3): ``python3 -P -m mod_base.adapter.host_child``.

* `def load_adapter(path: Path) -> ModuleType`: Import the adapter file as an isolated module (never added to ``sys.modules`` under a package name that other code could import).
* `def run_hook(context: Context, adapter: ModuleType, hook: str, arguments: Mapping[str, Any], *, image_factory: ImageFactory | None = None) -> Any`: Dispatch one hook in the current process and return its validated result.
* `def main(argv: Sequence[str] | None = None) -> int`

## `mod_base.evidence.expectation`

Owner: MB3.

Run the adapter's ``targets``/``expectation`` hooks, validate, canonicalize and hash (MB3).

* `def target_for_key(invocation: Invocation, key: str, *, subject: Mapping[str, str]) -> dict[str, Any]`: Call ``targets`` and return the target of ``key`` whose subject equals ``subject`` (``{branch, commit, tree}``). ``default-branch`` mode passes ``branches=None``; ``enrolled-branches`` mode passes exactly ``[{name, commit, tree}]`` of the subject. A missing key or a different subject raises :class:`mod_base.errors.Unavailable`.
* `def read_extensions(invocation: Invocation, path: Path | None) -> dict[str, dict[str, Any]]`: Strictly read an ``extensions.json`` (``{name: object}``, at most 1 MiB, every name declared in ``config.adapter.extensions``); ``None`` gives ``{}``.
* `def derive_expectation(invocation: Invocation, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]`: Call ``expectation`` and return the validated document (``documents.validate_expectation`` with ``image_policy=config.image_policy()``).
* `def expectation_bytes(expectation: Mapping[str, Any]) -> bytes`: ``canonical_json(expectation)``: the exact bytes of ``expectation.json``.
* `def require_rederived(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]`: R2: re-derive the expectation and require its canonical bytes to equal ``embedded``.
* `def run_expect(invocation: Invocation, *, key: str, tested_run_json: Path | None, extensions: Path | None, output: Path) -> dict[str, Any]`: The ``expect`` command: derive the expectation of ``key`` at the checked-out head and write ``output`` (canonical JSON, new file).

Frozen for other units (integration round):

* `def tested_run_projection(tested: Mapping[str, Any], event: str) -> dict[str, str]`: ``{event, branch}`` for the ``expectation`` hook: ``event`` (the handoff run's, see the module docstring) and the tested claim's ``branch``. Validated like a hook argument.
* `def require_rederived_for_runs(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any], tested: Mapping[str, Any], handoff_event: str, tested_event: str, extensions: Mapping[str, Any]) -> dict[str, Any]`: R2 with authenticated run records: re-derive with the handoff run's ``handoff_event`` (the projection the producer used) and, when the tested run was started by another ``tested_event``, also with the tested run's own projection; both must give ``embedded``.

## `mod_base.evidence.prepare`

Owner: MB3.

The producer (MB3): Quick Skin ``evidence.prepare`` + Block Pops ``evidence.curate``.

* `class PrepareResult`
  * fields: `manifest: dict[str, Any], output: Path, anchor_eligible: bool`
* `def prepare_handoff(invocation: Invocation, *, e2e_root: Path, key: str, output: Path, subject: Mapping[str, str], tested: Mapping[str, Any], handoff: Mapping[str, Any], extensions_path: Path | None = None, anchor: str = 'auto', anchor_output: Path | None = None) -> PrepareResult`: Produce the handoff bundle for ``key`` in the new directory ``output``.

## `mod_base.evidence.validate`

Owner: MB3.

Handoff/compact validation in a fresh process (MB3), including BP ``validate_raw``.

Constants:

* `KINDS = ('handoff', 'compact', 'anchor', 'family')`

* `def validate_handoff_dir(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None, rederive: bool = True) -> dict[str, Any]`: Validate a handoff bundle directory and return its manifest (exit 2 on any defect).
* `def validate_compact_dir(invocation: Invocation, root: Path, *, key: str, bind_raw: Path | None = None, expected_subject_commit: str | None = None) -> dict[str, Any]`: Validate a published compact bundle (``complete`` or ``composed``) including its embedded final selection (``documents.validate_selection`` and ``documents.check_compact_selection``); with ``bind_raw`` (the raw handoff directory) re-encode every derivative from the raw PNGs and require byte-identical WebP (BP ``_bind_compact``).
* `def validate_bundle(invocation: Invocation, kind: str, root: Path, *, key: str, bind_raw: Path | None = None, expected_subject_commit: str | None = None) -> dict[str, Any]`: The ``validate`` command: dispatch on ``kind`` (``handoff``, ``compact``, ``anchor`` or ``family``, the latter validating only the envelope via :mod:`mod_base.family.envelope`).

Frozen for other units (integration round):

* `class Bundle`: One bundle read strictly from ``root``: its documents and their exact bytes.
  * fields: `root: Path, manifest: dict[str, Any], manifest_raw: bytes, expectation: dict[str, Any], expectation_raw: bytes, extensions: dict[str, dict[str, Any]], selection: dict[str, Any] | None = None, selection_raw: bytes | None = None`
* `def load_handoff(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None) -> Bundle`: Strict structure, canonical bytes and exact inventory of a handoff (no image decoding, no hook).
* `def load_compact(invocation: Invocation, root: Path, *, key: str, expected_subject_commit: str | None = None, intermediate: bool = False, selection: str = 'final', expectation: Mapping[str, Any] | None = None) -> Bundle`: Strict structure, canonical bytes and exact inventory of a compact bundle (no decoding).
* `def check_handoff(invocation: Invocation, bundle: Bundle, *, handoff_event: str | None = None) -> dict[str, Any]`: Pixels, R1 and (with ``handoff_event``) R2 of a loaded handoff; returns the ``collect`` result of R1.
* `def check_compact_pixels(bundle: Bundle) -> None`: Every derivative re-inspected at exactly ``thumbnail(source, derivative_box)`` and every derivative comparison recomputed on the published WebP bytes.
* `def check_extensions_verified(manifest: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]`: R6 on an ``authenticate_extensions`` result: every extension the manifest carries is verified, and ``delegated`` reuse is proven (``reuse_verified``). Returns the verified names.

## `mod_base.evidence.compact`

Owner: MB3.

Handoff -> compact bundle (MB3), with the re-encode binding (BP ``authenticate_source._bind_compact``).

* `def compact_bundle(invocation: Invocation, *, key: str, input_dir: Path, selection_path: Path, output: Path) -> dict[str, Any]`: Write the compact bundle of ``input_dir`` (a complete handoff or a cache) into the new ``output`` with the completed selection embedded (see module docstring) and return its validated manifest.

Frozen for other units (integration round):

* `def rederive(invocation: Invocation, bundle: Bundle, draft: Mapping[str, Any]) -> dict[str, Any]`: R2 with the authenticated run records of the draft: the handoff run's projection, and the tested run's own when it was started by another event (:func:`mod_base.evidence.expectation.require_rederived_for_runs`).
* `def read_draft(invocation: Invocation, path: Path, *, key: str) -> dict[str, Any]`: The selection draft written by ``authenticate`` for ``key`` in this Pages run.
* `def bind_draft(draft: Mapping[str, Any], bundle: Bundle, *, kind: str) -> None`: The draft describes exactly the selected bundle (see the module docstring).
* `def finalize(manifest: dict[str, Any], draft: Mapping[str, Any], *, mode: str, composition: Mapping[str, Any] | None = None, extensions_verified: list[str] | None = None) -> tuple[dict[str, Any], dict[str, Any], bytes]`: Complete the draft for ``manifest`` (which carries the placeholder selection record) and return ``(manifest, final selection, selection bytes)`` with the real record in place.

## `mod_base.evidence.compose`

Owner: MB3.

Composition of ``selected`` evidence with its authenticated baseline (MB3, rule R3).

* `def compose_selected(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selection_path: Path, output: Path) -> dict[str, Any]`: The ``compose`` command (``--selected DIR --selection F --output DIR``): steps 1-5 of the module docstring; ``selection_path`` is the draft from ``authenticate``. Returns the composed compact manifest written to ``output`` (a new directory).
* `def verify_composition(invocation: Invocation, *, composed_dir: Path, selected_dir: Path, baseline_dir: Path, expectation: dict[str, Any]) -> None`: R3 core re-verification (step 4 of the module docstring); ``selected_dir`` is the intermediate selected compaction. Raises :class:`mod_base.errors.MbError`.

Frozen for other units (integration round):

* `def authenticate_baseline(invocation: Invocation, api: GitHubApi, *, key: str, baseline: Mapping[str, Any]) -> github_artifacts.Artifact`: R3 owner authentication of the ``mb-baseline`` artifact the compose hook named.

## `mod_base.evidence.anchor`

Owner: MB3.

The lossless anchor (MB3): Block Pops ``scripts/pages/visual_anchor.py`` generalized.

* `class AnchorIdentity`
  * fields: `eligible: bool, name: str | None, artifact_nodes: tuple[str, ...]`
* `def artifact_name(key: str, commit: str, run_id: int, run_attempt: int) -> str`: ``mb-anchor--{key}--{commit}--{run_id}--a{attempt}`` (delegates to the grammar).
* `def anchor_identity(invocation: Invocation, handoff_dir: Path, *, key: str) -> AnchorIdentity`: Decide eligibility for a validated handoff directory and name the anchor it would produce.
* `def create_anchor(invocation: Invocation, *, key: str, handoff_dir: Path, raw_artifact_id: int, raw_artifact_name: str, raw_artifact_digest: str, output: Path) -> dict[str, Any]`: Write the anchor bundle into the new ``output`` and return its validated manifest.
* `def validate_anchor_dir(root: Path, *, key: str | None = None, expected_subject_commit: str | None = None, raw_artifact_id: int | None = None, raw_artifact_name: str | None = None, raw_artifact_digest: str | None = None) -> dict[str, Any]`: Validate an anchor directory (inventory, hashes, canonical PNG re-inspection, embedded expectation) and, when all three ``raw_artifact_*`` are given, its source artifact binding. Needs no config: BP curate calls it with the anchor alone.

Frozen for other units (integration round):

* `def eligible_nodes(invocation: Invocation, manifest: Mapping[str, Any], expectation: Mapping[str, Any]) -> tuple[str, ...]`: The anchor nodes of a validated handoff, or ``()``: the anchor is enabled, the handoff was a direct canonical run of the subject that is not an attestation, and ``anchor_selection`` chose nodes (which the protocol requires to equal ``expectation.anchor``).

## `mod_base.evidence.commands`

Owner: MB3 (register() implemented by MB0; handlers dispatch to the entry points).

``expect``, ``prepare``, ``validate``, ``compact``, ``compose`` and ``anchor`` (MB3).

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_expect(args: argparse.Namespace) -> int`
* `def run_prepare(args: argparse.Namespace) -> int`
* `def run_validate(args: argparse.Namespace) -> int`
* `def run_compact(args: argparse.Namespace) -> int`
* `def run_compose(args: argparse.Namespace) -> int`
* `def run_anchor_identity(args: argparse.Namespace) -> int`
* `def run_anchor_create(args: argparse.Namespace) -> int`
* `def run_anchor_validate(args: argparse.Namespace) -> int`

## `mod_base.family.envelope`

Owner: MB4.

``mod-base.family.envelope`` creation and validation (MB4).

Constants:

* `ENVELOPE_NAME = 'envelope.json'`

* `def create_envelope(invocation: Invocation, *, family: str, key: str, bundle_dir: Path, coverage_sha: str, subject: Mapping[str, str], producer: Mapping[str, Any], output: Path) -> dict[str, Any]`: Copy the native bundle into the new ``output`` and write ``envelope.json`` beside it.
* `def validate_envelope_dir(invocation: Invocation, root: Path, *, family: str | None = None, key: str | None = None) -> dict[str, Any]`: Validate ``root/envelope.json`` against the exact directory inventory and the configured family (``handoff_max_bytes``); return the envelope.

Frozen for other units (integration round):

* `MAX_NATIVE_FILE_BYTES = 33554432`: the bound of one native file of a family bundle (the
  `family-handoff`/`family-cache` per-entry extraction bound).

## `mod_base.family.paired`

Owner: MB4.

``mod-base.family.paired`` validation, image re-inspection and ``family collect`` (MB4).

Constants:

* `PROJECTION_NAME = 'paired.json'`
* `IMAGES_DIRECTORY = 'images'`
* `SOURCE_DIRECTORY = 'source'`
* `SELECTED_NAME = 'selected.json'`

* `class FamilyOutcome`: ``status`` is ``available``, ``superseded`` or ``unavailable``; ``projection`` is set only when available.
  * fields: `status: str, reason: str, projection: dict[str, Any] | None = None, carried_from: str | None = None`
* `def validate_projection(invocation: Invocation, projection_path: Path, *, images_root: Path, family: str, key: str, expected_coverage_sha: str) -> dict[str, Any]`: R4 for one written projection; returns the validated projection.
* `def verify_carry_forward(repo_root: Path, carried_from: str, coverage_sha: str) -> None`: R5: both commits are present as inert objects and ``carried_from`` is an ancestor of ``coverage_sha`` (``git merge-base --is-ancestor`` in a sanitized environment).
* `def collect_family(invocation: Invocation, *, family: str, key: str, input_dir: Path, expected_coverage_sha: str, output: Path, selected_json: Path) -> FamilyOutcome`: The ``family collect`` command: validate the envelope, bind the recorded selection ``selected_json`` (``select --family --output F``) to it, run ``family_validate``, apply R4/R5 and, when available, write the collected layout (module docstring) into the new ``output``. ``superseded`` and ``unavailable`` are returned (the command exits 3 without writing an upload).

## `mod_base.family.commands`

Owner: MB4 (register() implemented by MB0; handlers dispatch to the entry points).

``family envelope`` and ``family collect`` (MB4). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_envelope(args: argparse.Namespace) -> int`
* `def run_collect(args: argparse.Namespace) -> int`

## `mod_base.pages.targets`

Owner: MB5.

Adapter ``targets`` orchestration, enrolled-branch listing and inert fetches (MB5).

* `def list_enrolled_branches(api: GitHubApi, *, max_branches: int) -> list[dict[str, str]]`: ``[{name, commit, tree}]`` of the repository's branches (one page; more than ``max_branches`` fails closed), each tree read from the commit API.
* `def fetch_inert(repo_root: Path, commits: Sequence[str], *, depth: int = 1) -> None`: Fetch ``commits`` into ``repo_root``'s object store without checkout, tags or credentials (sanitized ``git`` environment, ``protocol.version=2``, bounded retries).
* `def discover_targets(invocation: Invocation, *, api: GitHubApi | None) -> list[dict[str, Any]]`: Run the adapter ``targets`` hook for the configured mode and return 1..``targets.max`` validated targets (``api`` is required in enrolled-branches mode).

Frozen for other units (integration round):

* `def branch_heads(api: GitHubApi, *, max_branches: int) -> dict[str, str]`: Branch name -> head commit from one page of the branches API. More than ``max_branches`` rows, or a full page, fails closed; a name outside the branch grammar is skipped (it can never be a subject) but counts toward both bounds; a malformed row fails the whole listing.
* `def commit_tree_local(repo_root: Path, commit: str) -> str`: The tree of ``commit`` from the local object store (the object must be present).

## `mod_base.pages.admission`

Owner: MB5.

Publication admission (MB5): QS ``publication_progress.decide`` + wake/recovery/family admission.

Constants:

* `COALESCE_SECONDS = 600`
* `PARTIAL_DEADLINE_SECONDS = 2700`
* `RECOVERY_INTERVAL_SECONDS = 3600`
* `MAX_REQUESTS = 160`
* `MAX_CANDIDATES = 8`
* `MAX_FAILED_PUBLICATIONS = 3`
* `ACTIVE_RUN_STATUSES = frozenset({'in_progress', 'pending', 'queued', 'requested', 'waiting'})`
* `REASONS = frozenset({'always', 'awaiting-complete-v1-evidence', 'coalescing', 'complete', 'current', 'deferred-active...`

* `class ProgressPolicy`
  * fields: `coalesce_seconds: int = 600, partial_deadline_seconds: int = 2700, recovery_interval_seconds: int = 3600, max_failed_publications: int = 3`
  * `@classmethod from_config(cls, admission: Mapping[str, Any]) -> 'ProgressPolicy'`
* `class Decision`
  * fields: `eligible: bool, reason: str, ready: int, published: int, next_check_at: float | None = None`
* `def decide(*, expected: set[str], published: dict[str, float], ready: dict[str, float], ordinary_ready: bool, published_at: float | None, now: float, publisher_available: bool = True, failed_publications: int = 0, ordinary_changed: bool = False, policy: ProgressPolicy = ProgressPolicy(coalesce_seconds=600, partial_deadline_seconds=2700, recovery_interval_seconds=3600, max_failed_publications=3)) -> Decision`: QS ``publication_progress.decide`` (pure, deterministic; same reasons and ordering).
* `class WakeInputs`: The identifier inputs of ``pages.yml`` (all optional; validated per operation).
  * fields: `run_id: int | None = None, sha: str | None = None, family: str | None = None, bundle_key: str | None = None, artifact_id: int | None = None, artifact_digest: str | None = None, coverage_sha: str | None = None`
* `class Admission`: ``admit``'s outputs (each written to ``$GITHUB_OUTPUT`` as one line of canonical JSON).
  * fields: `eligible: bool, reason: str, bundle_keys: list[str] = <factory>, subjects: dict[str, dict[str, str]] = <factory>, families: list[dict[str, str]] = <factory>, nominations: dict[str, int] = <factory>, heads: dict[str, str] = <factory>`
* `def admit(invocation: Invocation, *, api: GitHubApi, operation: str, wake: WakeInputs, now: float | None = None, sleep: Callable[[float], None] = sleep) -> Admission`: Decide whether this Pages run publishes (see module docstring). ``operation`` is one of ``workflow.PUBLISH_OPERATIONS``. An ineligible admission is a normal outcome (exit 0).

## `mod_base.pages.select`

Owner: MB5.

Select the newest authenticated evidence for one key (MB5).

Constants:

* `SELECTED_KEYS = ('kind', 'artifact_id', 'name', 'digest', 'size', 'run_id', 'run_attempt')`
* `SELECTED_KINDS = ('handoff', 'cache', 'family-handoff', 'family-cache')`

* `class Selected`: The ``select`` outputs; also the ``--selected-json`` document of ``authenticate``.
  * fields: `kind: str, artifact_id: int, name: str, digest: str, size: int, run_id: int, run_attempt: int`
  * `@classmethod parse(cls, value: Any) -> 'Selected'`
  * `to_json(self) -> dict[str, Any]`: The JSON object form: exactly :data:`SELECTED_KEYS` mapped to the field values.
* `def select_evidence(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None = None, nomination: int | None = None, expected_subject_commit: str) -> Selected`: Return the selected artifact or raise ``Unavailable`` (see module docstring).

Frozen for other units (integration round):

* `class SourceRuns`: One invocation's bounded, memoized view of ``source.workflow``: its workflow id and pages.yml's (read lazily, at most once each), and the runs naming each subject.
  * `__init__(self, api: GitHubApi, invocation: Invocation) -> None`
  * `workflow_id(self, path: str) -> int`
  * `source_workflow_id` (property) -> `int`
  * `pages_workflow_id` (property) -> `int`
  * `for_subject(self, commit: str, *, subject_canonical: bool) -> list[dict[str, Any]]`: Every source run for subject ``commit`` (any status), newest first by ``(created_at, id, run_attempt)``.
  * `newest(self, commit: str, *, subject_canonical: bool) -> dict[str, Any]`: The newest source run for ``commit``, which must be ``completed/success``.
* `class FamilyGenerations`: The family generation :func:`select_evidence` picks without a nomination (``build`` uses only :meth:`history`; one instance memoizes the reads of every leg of an invocation).
  * `__init__(self, api: GitHubApi, invocation: Invocation, *, sources: SourceRuns | None = None) -> None`
  * `history(self, commit: str) -> list[str]`: ``commit`` and at most ``limits.GENERATION_PROBES`` first-parent ancestors, newest first.
  * `newest(self, family: str, key: str, commit: str) -> Selected | None`: The family generation of ``key`` for the expected ``commit``, or ``None`` when no admissible generation exists.
* `def family_archive_limit(family: Mapping[str, Any]) -> int`: The largest family handoff or cache archive of the configured ``family``: the archive limit of its expanded ``handoff_max_bytes`` plus ``envelope.json`` (:func:`mod_base.io.bounded_zip.artifact_limit`).
* `def display_title(invocation: Invocation, commit: str) -> str | None`: The configured ``source.display_title`` with ``{subject_commit}`` substituted, or ``None``.
* `def subject_events(invocation: Invocation, *, subject_canonical: bool) -> frozenset[str]`: The events a source run may have (``events.canonical``, and also ``events.other`` for a subject that is not the canonical branch).
* `def is_success(run: Mapping[str, Any]) -> bool`
* `def source_run_valid(invocation: Invocation, run: Mapping[str, Any], *, source_workflow_id: int) -> bool`: A ``source.workflow`` run (by path and id) of this repository on the canonical branch with a valid head. Status, event and subject are not considered.
* `def names_commit(invocation: Invocation, run: Mapping[str, Any], commit: str) -> bool`: ``run`` is bound to subject ``commit``: by the configured display title, else by head.
* `def pages_owner_valid(run: Mapping[str, Any], invocation: Invocation, *, default_branch: str, pages_workflow_id: int, head_sha: str | None = None) -> bool`: A successful ``pages.yml`` run (by path and id) of this repository on the default branch (at ``head_sha`` when given), started by a Pages event; never the executing run itself.
* `def producer_run_valid(run: Mapping[str, Any], invocation: Invocation, family: Mapping[str, Any], *, default_branch: str, head_sha: str) -> bool`: A successful ``families[].producer.workflow`` run of this repository on the default branch at ``head_sha`` started by one of ``producer.events``.
* `def upload_in_window(artifact: artifacts.Artifact, job: Mapping[str, Any] | None, step_name: str) -> bool`: The artifact was created inside the successful step ``step_name`` of ``job``.
* `def successful_job(attempt_jobs: list[dict[str, Any]], name: str, run_attempt: int) -> dict[str, Any] | None`: The single job named exactly ``name`` in ``run_attempt`` when it completed successfully.
* `def handoff_job_name(invocation: Invocation, key: str) -> str`: ``source.handoff_job`` with its optional ``{key}`` placeholder filled.

## `mod_base.pages.authenticate`

Owner: MB5.

Source-run authentication producing ``mod-base.selection`` (MB5, SPEC §4.8).

* `def authenticate_selection(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selected: Selected) -> dict[str, Any]`: Authenticate the downloaded ``selected_dir`` and return the validated selection draft.
* `def kit_binding(api: GitHubApi, invocation: Invocation, *, manifest: Mapping[str, Any], owner_run: Mapping[str, Any], selected_kind: str) -> dict[str, str]`: ``{source, sha}``: prove ``manifest.kit.sha`` belongs to the authenticated owner run.
* `def run_authenticate(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selected_json: Path, output: Path) -> dict[str, Any]`: The ``authenticate`` command: read ``selected_json`` (the exact :meth:`Selected.to_json` object written by ``select --output``), authenticate and write the selection draft to ``output`` (canonical JSON, new file).

Frozen for other units (integration round):

* `def write_new_file(path: Path, data: bytes) -> None`: Create ``path`` exclusively (never following or replacing anything) and write ``data``.

## `mod_base.pages.commands_control`

Owner: MB5 (register() implemented by MB0; handlers dispatch to the entry points).

``admit``, ``select`` and ``authenticate`` (MB5).

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_admit(args: argparse.Namespace) -> int`
* `def run_select(args: argparse.Namespace) -> int`
* `def run_authenticate(args: argparse.Namespace) -> int`

## `mod_base.pages.build`

Owner: MB6.

The atomic site renderer (MB6, SPEC §5.3.2): QS ``build_site`` + BP ``_current_pages_inputs``.

Constants:

* `PROMOTION_FILE = 'promotion.json'`
* `SITE_ALLOWLIST = ('index.html', 'e2e/index.html', 'assets/site.js', 'assets/gallery.js', 'assets/styles.css')`

* `class BuildResult`
  * fields: `heads: dict[str, str], site_sha256: str, promotion: dict[str, Any]`
* `def build_site(invocation: Invocation, *, api: GitHubApi, kit_root: Path, collected_dir: Path, families_dir: Path, output: Path, promotion_dir: Path) -> BuildResult`: Render and publish the atomic site (see module docstring); ``output`` must not exist.
* `def render_site(invocation: Invocation, *, kit_root: Path, bundles: list[dict[str, Any]], families: list[dict[str, Any]], stage_fd: int) -> dict[str, bytes]`: Pure rendering of already authenticated inputs into the stage; returns the written ``{relative path: bytes}`` map that :func:`mod_base.io.seal.seal_output` verifies.

Frozen for other units (integration round):

* `class BuildError(MbError)`: The Pages inputs, this run or the rendered site fail a build check (exit 2).
* `def current_implementation(invocation: Invocation, *, jobs_allowed: Sequence[str]) -> dict[str, Any]`: SPEC §5.3.2 step 1: this process runs in this repository's ``pages.yml`` on the canonical branch, in one of ``jobs_allowed``; returns the promotion ``implementation`` of this run.
* `def require_current_run(api: GitHubApi, invocation: Invocation, implementation: Mapping[str, Any]) -> dict[str, Any]`: SPEC §5.3.2 step 2: the API default branch is the canonical branch and this run and its exact attempt are the unfinished (no conclusion yet) ``pages.yml`` run of the canonical head; returns the run.
* `def check_checkouts(invocation: Invocation, *, kit_root: Path, environ: Mapping[str, str]) -> None`: The host facts of SPEC §5.3.2 step 1: no inherited ``GIT_*`` variable in ``environ``, the mod checkout clean at ``GITHUB_SHA`` and the kit checkout ``kit_root`` clean at ``MOD_BASE_KIT_SHA``.
* `FAMILY_SELECTED_NAME = 'selected.json'`: the recorded selection of a collected family artifact (`family.paired.SELECTED_NAME`, see the recorded-selection amendment).
* `def collected_family_selection(root: Path, *, family: str, key: str, reason: str) -> Selected`: The layout of a downloaded collected family artifact and its recorded ``Selected`` generation (``refresh`` checks the same layout).

## `mod_base.pages.templating`

Owner: MB6.

Build-time templating of the kit front end (MB6, SPEC §6.2).

Constants:

* `PLACEHOLDERS = frozenset({'actions_url', 'color_scheme', 'description', 'eyebrow', 'issues_url', 'license', 'meta_description', 'name', 'primary_link_title', 'primary_link_url', 'repository_url', 'tagline', 'theme_color'})`
* `CONDITIONS = frozenset({'families', 'icon', 'primary_link'})`
* `ATTRIBUTES = frozenset({'aria-label', 'content', 'href'})`

* `def render(template: str, values: Mapping[str, str], conditions: Mapping[str, bool]) -> str`: Substitute ``values`` (exactly :data:`PLACEHOLDERS`) and resolve ``conditions`` (exactly :data:`CONDITIONS`); raise :class:`mod_base.errors.MbError` on any template violation.
* `def theme_css(theme: Mapping[str, Any]) -> str`: ``:root{--bg:...}`` from ``theme.dark`` plus, when ``theme.light`` is set, a ``@media (prefers-color-scheme: light){:root{...}}`` block; values are validated colours.

Frozen for other units (integration round):

* `def theme_color(theme: Mapping[str, Any]) -> str`: The ``theme-color`` meta value: the midpoint of ``theme.dark.bg`` and ``theme.dark.surface`` rounded half up.

## `mod_base.pages.refresh`

Owner: MB6.

Roll the promoted bundles forward as caches (MB6): QS refresh-cache + BP ``refresh_cache``.

* `class RefreshResult`
  * fields: `available: bool, cache_name: str | None, baseline_name: str | None = None`
* `def refresh_bundle(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None, input_dir: Path) -> RefreshResult`: Download and revalidate one promoted bundle, write its upload bytes into the new ``input_dir`` and name its cache (see module docstring). For a family leg with nothing collected it returns ``available=False`` (exit 0).

## `mod_base.pages.commands_build`

Owner: MB6 (register() implemented by MB0; handlers dispatch to the entry points).

``build`` and ``refresh`` (MB6). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_build(args: argparse.Namespace) -> int`
* `def run_refresh(args: argparse.Namespace) -> int`

## `mod_base.pages.rotate`

Owner: MB7.

Exact-ID rotation of superseded evidence after an authenticated successful Pages run (MB7).

* `class RotationDeferred(MbError)`: Some candidates were retained after completing these exact deletions (budget spent).
  * `__init__(self, message: str, deleted_artifact_ids: list[int]) -> None`
* `class DeletionBudget`: Bounds authenticated deletion attempts across one rotation invocation.
  * fields: `remaining: int = 64, last_deferred_count: int = 0`
  * `begin_scope(self) -> None`
  * `select(self, artifacts: list[object]) -> list[object]`
  * `consume(self) -> None`
* `def rotate_generation(invocation: Invocation, *, api: GitHubApi, owner_run_id: int, owner_sha: str, delete_delay_seconds: float = 1.0, dry_run: bool = False, now: float | None = None, sleep: Callable[[float], None] = sleep) -> dict[str, object]`: Retire everything R superseded and return the JSON summary (QS keys plus ``remaining_rotation_deletions``). ``dry_run`` plans and re-observes without deleting.

## `mod_base.pages.commands_rotate`

Owner: MB7 (register() implemented by MB0; handlers dispatch to the entry points).

``rotate`` (MB7). Flags are frozen by SPEC §2.2. Loads config data only (no repository checks, no adapter): the rotate job sparse-checks-out ``site/mod-base.json`` alone.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_rotate(args: argparse.Namespace) -> int`

## `mod_base.pin`

Owner: MB9.

The kit pin: parser, verifier, kit resolution and ``kit-digest-v1`` (MB9).

Constants:

* `KIT_TOKEN = 'The-Plum-Team/mod-base'`
* `DIGESTED_DIRS = ('src', 'site', 'requirements')`
* `KIT_PATH_NAME`: Frozen ASCII kit-digest-v1 path regex; privileged copy discovery uses the same path spelling contract.
* `STAMP_NAME = 'MOD_BASE_KIT.json'`
* `OVERLAY_PATH = 'out/mod-base-kit'`

* `class Pin`: The single pin of a mod: ``sha`` (40-hex), ``version`` (``vX.Y.Z``) and every referencing ``path@line`` location, sorted.
  * fields: `sha: str, version: str, references: tuple[str, ...]`
* `def parse_pin_files(files: Mapping[str, bytes]) -> Pin`: Parse the pin from ``{repo-relative path: bytes}`` of the workflow and action files.
* `def parse_pin(repo: Path) -> Pin`: Read (bounded) the mod's workflow and action files and parse its single pin.
* `def verify(repo: Path, *, network: bool, api: GitHubApi | None = None) -> Pin`: Pin consistency; with ``network`` also ``compare/<pin>...main`` is ``ahead|identical`` with ``behind_by == 0`` and ``git/ref/tags/<version>`` peels to the pin (``api`` required).
* `def kit_tree_digest(root: Path) -> str`: ``sha256:<hex>`` kit-digest-v1 of ``root`` (the kit checkout root).
* `def kit_path(repo: Path, environ: Mapping[str, str]) -> Path`: Resolve the kit root for ``repo`` in the bootstrap order (overlay stamp, env, user cache, anonymous fetch), verifying each candidate; raise :class:`mod_base.errors.Unavailable`.
* `def stamp_document(pin: Pin, digest: str) -> dict[str, Any]`: Construct the existing canonical kit-stamp data fields for an independently admitted pin/digest; construction alone grants no checkout, release or execution authority.
* `def read_stamp(directory: Path) -> dict[str, Any]`: Read and validate ``MOD_BASE_KIT.json`` (``mod-base.kit-stamp`` v1) in ``directory``.

Frozen for other units (integration round):

* `LOCKED_DIRS = ('template', 'tools')`
* `STAGED_LOCK = 'src/mod_base/template/staged_files.sha256'`
* `ACTIONS_DIR = 'actions'`
* `ACTIONS_LOCK = 'src/mod_base/template/staged_actions.sha256'`
* `KIT_REPOSITORY_BARE`: the compiled pattern of a bare kit repository name (no following path),
  refused outside comment-only lines of a mod's workflow and action files.
* `def yaml_unescape(text: str) -> str`: ``text`` with every YAML double-quoted escape decoded (unknown escapes are kept).
* `def staged_listing(root: Path) -> bytes`: The listing of ``template/`` and ``tools/`` of the kit root ``root``: the bytes :data:`STAGED_LOCK` must hold.
* `def actions_listing(root: Path) -> bytes`: The listing of ``actions/`` of the kit root ``root``: the bytes :data:`ACTIONS_LOCK` must hold.
* `def verify_staged_files(root: Path) -> None`: Require ``template/`` and ``tools/`` of ``root`` to equal the :data:`STAGED_LOCK` listing inside its digested ``src/`` (verify the digest first), and a present ``actions/`` to equal the :data:`ACTIONS_LOCK` listing there. An absent ``actions/`` binds nothing, so an overlay staged by a bootstrap older than v0.9.2 (which stages none) stays valid; an ``actions/`` without :data:`ACTIONS_LOCK` (a kit older than v0.9.2) is unbound and refused.
* `def require_reachable(sha: str, api: GitHubApi) -> None`: Require ``compare/<sha>...main`` to be ``ahead`` or ``identical`` with ``behind_by == 0``.
* `def verify_released(pin: Pin, api: GitHubApi) -> None`: Require the pin to be reachable from mod-base ``main`` and its tag to peel to the pin.
* `def resolve(repo: Path, environ: Mapping[str, str], *, overlay: bool = True, allow_unpinned: bool = True) -> tuple[Path, Pin, str]`: ``(kit root, pin, source)`` with ``source`` in ``overlay|environment|unpinned|cache|fetch``.
* `def unclean_paths(status: str) -> list[str]`: Paths of ``git status`` output that make a kit checkout unclean.

## `mod_base.pin_commands`

Owner: MB9 (register() implemented by MB0; handlers dispatch to the entry points).

``pin verify`` and ``digest`` (MB9). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_verify(args: argparse.Namespace) -> int`
* `def run_digest(args: argparse.Namespace) -> int`

## `mod_base.template.tool`

Owner: MB9.

``template check|sync|init`` (MB9, SPEC §8.2).

Every workflow the kit renders with the mod's pin is a `RenderedCaller` of `RENDERED_CALLERS`,
which is code: manifest data can neither enrol a caller, remap one nor choose its renderer, and a
template the pin parser would read (a workflow, or an `action.yml` below `.github/actions`) may
hold `{{PIN}}`/`{{VERSION}}` only when it is enrolled. The Pages caller is a manifest entry,
rendered `pages-extension` and always managed. The four Build/E2E callers of
`build_ci.activation.CALLERS` are enrolled in code alone (never in the manifest), rendered `pinned`
(the whole file, no extension region) and managed only in the activation modes that list them.
`check`, `sync` and `init` read the activation state first: a mod with neither an activation
manifest nor a Build configuration is handled exactly as before those callers existed; with a
manifest, a caller its mode manages is checked and written like a managed file and any other must
not exist (`forbidden`); `sync` never deletes one. To add or replace a caller template, put it at
`template/managed/<path>` and enrol its path in `build_ci.activation.CALLERS` and
`MANAGED_CALLERS`; a new way of rendering is a new `RENDERERS` entry with its three functions.

Constants:

* `MANIFEST_PATH = 'template/manifest.json'`
* `RENDERERS = ('pages-extension', 'pinned')`: every renderer a `RenderedCaller` may name; one of another name is an error in every verb, never a byte-identical managed file.
* `RENDERED_CALLERS`: the closed registry, a tuple of `RenderedCaller`: the Pages caller, then one record per `build_ci.activation.CALLERS` path with `modes = build_ci.activation.managing_modes(path)`.

* `class RenderedCaller`: One rendered workflow: its path in the mod, its template source below ``template/``, its renderer and the activation modes that manage it (``None``: a manifest entry, managed whatever the activation).
  * fields: `path: str, source: str, renderer: str, modes: frozenset[str] | None`
* `def load_template_activation(repo: Path) -> dict[str, Any] | None`: The mod's validated activation manifest, or ``None`` for a mod with neither the manifest nor a Build configuration. Read bounded without following symlinks and bound to the repository and profile of ``scripts/ci/mod-base-build.json``; a Build configuration without a manifest is an error, so a deleted manifest is never taken for ``disabled``.
* `def activation_bytes(repo: Path) -> bytes | None`: The bytes of the manifest ``load_template_activation`` accepts (``None`` where it returns ``None``).
* `def expected_callers(kit_root: Path, pin: Pin, activation: dict[str, Any] | None) -> dict[str, bytes | None]`: What every Build/E2E caller path must hold for a validated manifest and a pin: the template of ``kit_root`` rendered with the pin where the mode manages the caller, ``None`` where the file must not exist. ``kit_root`` must be the kit the pin names.
* `def caller_files(repo: Path) -> dict[str, bytes]`: The bytes of every Build/E2E caller path that exists in ``repo``, each a bounded regular file reached without symlinks.

* `class Drift`: One difference: ``kind`` is ``missing``, ``changed``, ``fragment``, ``agents``, ``links``, ``forbidden`` or ``extension``; ``detail`` is a bounded unified diff or message.
  * fields: `path: str, kind: str, detail: str`
* `def load_manifest(kit_root: Path) -> dict[str, Any]`: Read and validate ``template/manifest.json`` (``mod-base.template-manifest`` v1).
* `def check(repo: Path, *, kit_root: Path) -> list[Drift]`
* `def sync(repo: Path, *, kit_root: Path, write: bool) -> list[Drift]`
* `def init(repo: Path, *, kit_root: Path, seed: bool, from_config: Path | None) -> list[str]`: Seed missing files; return the created paths; refuse to overwrite anything.

Frozen for other units (integration round):

* `DEFERRABLE`: the only paths `template.deferred` may name (shared files outside the
  publication control path: `.gitattributes`, `.gitignore`, Dependabot, the PR template and
  `AGENTS.md`).
* `def evaluate(repo: Path, *, kit_root: Path) -> tuple[list[Drift], list[Drift]]`: ``(failing drifts, pending drifts)`` of ``repo``: a present deferred fragment's missing required lines are pending; every other drift fails.
* `def pending(repo: Path, *, kit_root: Path) -> list[Drift]`: The required lines a present fragment in ``template.deferred`` still lacks: reported, never failing, until the adoption completes and ``deferred`` is emptied.
* `def extension_violations(body: tuple[str, ...] | list[str]) -> list[str]`: Every extension-region rule (SPEC §5.2) the extension ``body`` lines break.
* `def link_violations(document: str, text: str, managed_documents: frozenset[str] | set[str]) -> list[str]`: Links of the managed Markdown ``document`` that point anywhere but a managed document or an absolute ``https://`` URL.
* `def pinned_actions(kit_root: Path, manifest: dict[str, Any], entry: dict[str, Any]) -> tuple[tuple[str, str], ...]`: ``(dependency name, workflow)`` of every third-party action pinned in the managed region of each managed workflow the fragment ``entry`` lists in ``ignore_actions_of``, read from the kit's template (v1.0.1).

## `mod_base.template.commands`

Owner: MB9 (register() implemented by MB0; handlers dispatch to the entry points).

``template check|sync|init|activation|transition`` (MB9). Flags are frozen by SPEC §2.2; exit 2 on
drift. `template activation --repo DIR` and `template transition --repo DIR --base DIR` are
additive (v1.1.0): the first validates the mod's activation manifest and prints `state`,
`repository`, `profile`, `rollback_from`, `managed` and `next` lines; the second admits the change
from the protected base checkout to the candidate (`build_ci.transition`) and prints `transition`,
`pin` and `managed` lines. Both exit 2 on a refusal.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run_check(args: argparse.Namespace) -> int`
* `def run_sync(args: argparse.Namespace) -> int`
* `def run_init(args: argparse.Namespace) -> int`
* `def run_activation(args: argparse.Namespace) -> int`
* `def run_transition(args: argparse.Namespace) -> int`

## `mod_base.template.lock`

Owner: MB9.

The staged-file locks ``src/mod_base/template/staged_files.sha256`` and
``src/mod_base/template/staged_actions.sha256`` (MB9): see the staged-file lock amendment and its
v0.9.2 staged-set amendment. `python3 -m mod_base.template.lock [--root DIR] [--write]` checks them
(exit 1 when one is stale, 2 on an error) or rewrites the stale ones.

* `LOCKS`: every lock path with the `pin` listing function it must hold (`STAGED_LOCK` with `staged_listing`, `ACTIONS_LOCK` with `actions_listing`).
* `def recorded(kit_root: Path, lock: str = STAGED_LOCK) -> bytes | None`: The lock ``lock`` (a :data:`LOCKS` path) ``kit_root`` carries, or ``None`` when it has none.
* `def stale(kit_root: Path) -> list[str]`: The :data:`LOCKS` paths whose recorded bytes differ from the listing of ``kit_root``.
* `def write(kit_root: Path) -> bool`: Rewrite every stale lock of ``kit_root``; return whether any changed.
* `def main(argv: Sequence[str] | None = None) -> int`

## `mod_base.conformance.run`

Owner: MB10.

``conformance``: the synthetic producer -> collect -> build -> refresh -> rotate simulation (MB10).

The report's nine keys, the variants and the optional fixture functions of
`config.adapter.fixtures_path` (`family_bundle`, `FAMILY_OUTCOMES`, `delegated_extensions`,
`selected_extensions` and, from v1.0.1, `SELECTED_CHANGE`) are the adapter contract in
`docs/ADAPTER.md` ("Optional conformance fixtures", "The conformance report"), which
`tests/test_adapter_protocol.py` binds to the simulation.

* `def run_conformance(*, repo: Path, keys: Sequence[str] | None, all_keys: bool, kit_root: Path, families: bool) -> dict[str, Any]`: Run the simulation and return its nine-key report (``docs/ADAPTER.md`` "The conformance report"); any failed check raises :class:`mod_base.errors.MbError` (exit 2).
* `def main(argv: Sequence[str] | None = None) -> int`: The simulation child (see module docstring): ``python3 -P -m mod_base.conformance.run`` with the same flags as the ``conformance`` command; writes the canonical JSON report to stdout and returns an exit code through ``errors.run_main``.

## `mod_base.conformance.commands`

Owner: MB10 (register() implemented by MB0; handlers dispatch to the entry points).

``conformance`` (MB10). Flags are frozen by SPEC §2.2.

* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def run(args: argparse.Namespace) -> int`

## `mod_base.build_ci.protocol`

Owner: MB11. The Build adapter API version and pure identity and plan validation; see
BUILD-PROTOCOL.md. The hook contract itself is `mod_base.build_ci.adapter`.

* `BUILD_ADAPTER_API = 1`
* `BUILD_GRAPH_VERSION = 1`
* `PACKAGED_GRAPH_VERSION = 1`
* `PROFILES = ('quick-skin', 'block-pops')`
* `PRODUCERS = ('build', 'packaged')`: the two producers; the managed caller of each is `workflow.CI_CALLER_WORKFLOWS[producer]`.
* `OUTPUT_ROLES`: production, harness, SBOM, native reports and retained build logs.
* `LANE_OUTPUT_ROLES = ('production', 'harness')`: the two JARs every lane has exactly once; never target-scoped.
* `SINGLE_OUTPUT_ROLES = ('production', 'harness', 'sbom')`: roles one lane, or one target as a whole, holds at most once.
* `SHA1`, `SHA256`, `REPO`, `BRANCH`, `ID`, `WORKFLOW`: the field validators of an identity and a plan (commit, digest, repository, branch, unit id, workflow path).
* `def repo_path(value: Any, path: str) -> str`: a canonical repository-relative path (`grammar.is_repo_path`).
* `def export_path(value: Any, path: str) -> str`: a canonical export path (`grammar.is_export_path`); every planned output is one.
* `def validate_subject(value: Any, path: str = '$') -> dict[str, Any]`: An identity before planning: every field of `validate_identity` except `policy_sha256`, `inventory_blob`, `inventory_sha256`, `scenario_sha256` and `runtime_selection_sha256`, with the same cross-field rules.
* `def validate_identity(value: Any, path: str = '$') -> dict[str, Any]`
* `def subject_of(identity: dict[str, Any]) -> dict[str, Any]`: The subject part of a complete identity.
* `def plan_sha256(document: dict[str, Any]) -> str`
* `def check_output_paths(paths: list[str], path: str) -> None`
* `def check_output_scope(output: dict[str, Any], path: str) -> None`: An output (`{lane_id, role, ...}`, of a plan or of a Build envelope) is one lane's or, with a null `lane_id`, its target's as a whole; a production or harness JAR is always one lane's.
* `def validate_plan_units(value: Any, path: str = '$') -> dict[str, Any]`: Exactly `{targets, lanes}` in the plan's shape with every plan rule that needs no identity: what a protected adapter derives. Every lane has exactly one production and one harness output; an SBOM is optional (at most one per lane and one per target as a whole); every target has a native report; an output's `lane_id` is a lane of its own target or null for the target as a whole; paths are unique across all targets.
* `def validate_plan(document: Any, *, path: str = '$') -> dict[str, Any]`

## `mod_base.build_ci.graph`

Owner: MB11. Exact job graphs of the managed Build and packaged-E2E callers: the exact multiset of
the jobs the caller owns and the jobs of every workflow it calls, each with one conclusion, as a
function of the producer, one closed mode and the plan. Never chosen to fit observed jobs. Literal
API listings of every mode are in `tests/fixtures/ci_graphs`; how GitHub names a skipped call and a
skipped unexpanded matrix, and which workflows a run lists as referenced, is confirmed only by a
hosted canary.

* `BUILD_MODES = ('full', 'deferred', 'reuse')`
* `PACKAGED_MODES = ('pull-request', 'deferred', 'selected', 'rebuilt', 'reuse')`
* `class BuildGraphV1`: a run of the managed Build caller in one mode of `BUILD_MODES`. `jobs` is
  the exact sorted `[{name, conclusion}]`; `sealed_jobs` the jobs that seal and upload (gates
  included); `sha256` the digest a producer record carries as `graph_sha256`; `called` every kit
  callee the caller references -> whether its calling job runs in this mode; `prerequisites` the
  successful jobs that must finish before a gate job validates.
  * fields: `mode: str = 'full'`
  * `jobs(self, plan: dict[str, Any]) -> list[dict[str, str]]`
  * `sealed_jobs(self, plan: dict[str, Any]) -> list[str]`
  * `sha256(self, plan: dict[str, Any]) -> str`
  * `called(self) -> dict[str, bool]`
  * `prerequisites(self, plan: dict[str, Any], gate_job: str) -> list[str]`
* `class PackagedGraphV1`: the same for the managed packaged-E2E caller and `PACKAGED_MODES`.
  * fields: `mode: str = 'pull-request'`
  * `jobs(self, plan: dict[str, Any]) -> list[dict[str, str]]`
  * `sealed_jobs(self, plan: dict[str, Any]) -> list[str]`
  * `sha256(self, plan: dict[str, Any]) -> str`
  * `called(self) -> dict[str, bool]`
  * `prerequisites(self, plan: dict[str, Any], gate_job: str) -> list[str]`
* `def run_graph(producer: str, mode: str) -> BuildGraphV1 | PackagedGraphV1`: The graph contract of ``producer`` (``build`` or ``packaged``) in ``mode``.
* `def job_name(producer: str, callee: str, job: str, unit_id: str | None = None) -> str`: The API name of a job of a kit workflow in a run of that producer; ``unit_id`` is the target or lane of a matrix job.
* `def upload_job_name(producer: str, kind: str, unit_id: str | None) -> str`: The API name of the job that uploads that artifact kind in a run of that producer.
* `def gate_mode(producer: str, gate: str, plan: dict[str, Any], digest: str) -> str`: The mode a gate's producer record was sealed in: the one admissible mode of ``records.GATE_MODES`` whose exact graph for the plan has that digest. Settled before any job is read.
* `def authenticate_referenced_workflows(references: Sequence[tuple[str, str]], *, identity: dict[str, Any], producer: str, mode: str) -> None`: Bind a producer run to its controller commit and kit pin: the guard workflow at ``controller_sha``, every kit callee whose calling job runs in that mode at the pinned kit SHA, optionally the caller's skipped callees at the same SHA, no other entry and no repeat.
* `def sealed_upload(job: dict[str, Any]) -> tuple[str, str]`: The upload window of a sealing job as whole-second UTC text: one successful seal step finished before one successful upload step started, both inside the successful job.
* `def require_graph(jobs: list[dict[str, Any]], *, plan: dict[str, Any], producer: str, mode: str, run_attempt: int) -> str`: Require the jobs of a completed attempt to be exactly that graph, every sealing job sealed before it uploaded; return the graph digest.
* `def require_partial_graph(jobs: list[dict[str, Any]], *, plan: dict[str, Any], producer: str, mode: str, run_attempt: int, finished: list[str]) -> None`: Admit the jobs of a still-running attempt for an in-run reader: no unenrolled or repeated job, and every job named in ``finished`` completed as the graph expects. Never proof that the remaining jobs succeed.
* `def authenticate_graph(api: GitHubApi, *, plan: dict[str, Any], producer: str, mode: str, run_id: int, run_attempt: int) -> str`: Read one attempt's jobs and require the exact completed graph; a graph check only, not run, source or artifact admission.
* `def authenticate_gate_timeline(api: GitHubApi, *, document: dict[str, Any], descriptor: dict[str, Any], plan: dict[str, Any]) -> str`: Bind a tested record to the exact graph of its mode and to real execution times: every prerequisite job finished before the gate's seal step started, the gate's upload window is the descriptor's, and every source artifact's window is its producing job's actual upload. A Build consumed from another run needs that run's own exact full graph, finished first. Run/source/pin authority, artifact metadata and bytes remain separate.

## `mod_base.build_ci.authenticate`

Owner: MB11. Inert live and historical source authentication; never publishes a status. Every
function is one observation: a caller that acts on it observes again immediately before its effect
(`mod_base.build_ci.reads.Watch`). Passing a `mod_base.build_ci.reads.CommandReads` as `api` reads
each Git object once per command.

* `class PrGeneration`: Frozen live readiness/source observation, not execution evidence.
  * fields: `repository: str, pr_number: int, base_branch: str, base_sha: str, controller_tree: str, head_branch: str, head_sha: str, draft: bool, merge_sha: str | None`
* `class MergedPr`: Frozen historical PR/tested/final Git observations, not policy, gate, reuse or settlement authority.
  * fields: `repository: str, pr_number: int, identity_sha256: str, merged_sha: str, merged_tree: str, merged_parents: tuple[str, ...], merged_at: str, controller_sha: str`
* `def run_head(identity: dict[str, Any]) -> tuple[str, str, str]`: ``(head_sha, head_branch, head_repository)`` GitHub records on every run and artifact a managed caller produces for the identity: a pull request's head commit, branch and source repository, or the default-branch commit a protected push or dispatch runs from, which must also be its tested commit.
* `def authenticate_merged_pr_identity(api: GitHubApi, identity: dict[str, Any], *, controller_sha: str, merged_sha: str) -> MergedPr`: Bind an independently admitted original PR identity and current controller/final SHA to a closed merged same-repository PR, the original synthetic merge with exact ordered parents, an equal complete final tree and original/current protected history, in one pass. The identity is copied on entry. Live admission remains separate; full historical gates, native policy/pin, source seals and writer/owner approval remain required.
* `def read_pr_generation(api: GitHubApi, *, pr_number: int, controller_sha: str) -> PrGeneration`: One read of the repository, its default head and an open same-repository PR against the executing controller. Draft and unavailable merge states do not authorize workers; ready merge/policy/plan admission remains independent.
* `def authenticate_pr_identity(api: GitHubApi, identity: dict[str, Any]) -> None`: Check the ready PR generation and protected default/base, then the exact ordered merge parents and tree of the tested commit, in one pass. Does not establish native policy, approval, bytes or status authority.
* `def authenticate_source_identity(api: GitHubApi, identity: dict[str, Any]) -> None`: Dispatch to ready-PR authentication or authenticate an exact non-PR Git commit/tree/ordered parents in protected default history against the live controller, in one pass. Request/run/profile authorization and full recovery policy remain caller obligations.

## `mod_base.build_ci.reads`

Owner: MB11. How one Build/E2E command reads GitHub: immutable objects once, mutable state at the
start of the command and again immediately before its effect.

* `class CommandReads`: One command's reads through its one budgeted client. It offers the client's
  read surface, so it is passed wherever a client is read from. A commit, tree or blob named by SHA
  and a comparison of two SHAs are fetched once; so is the job list of an attempt that `run` saw
  completed. Everything else reaches the client every time.
  * `@classmethod of(cls, api: GitHubApi | CommandReads) -> CommandReads`
  * `repository` (property) -> `str`
  * `request_count` (property) -> `int`
  * `get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any`
  * `paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None, max_items: int) -> list[dict[str, Any]]`
  * `read_listing(self, read: Callable[[], _T]) -> _T`
  * `download(self, path: str, *, max_bytes: int) -> bytes`
  * `run(self, run_id: int) -> dict[str, Any]`
  * `attempt_jobs(self, run_id: int, run_attempt: int) -> list[dict[str, Any]]`
* `class Watch`: The mutable state one effect depends on. `read` performs a read the first time its
  key is asked for and answers from that observation afterwards; `recheck`, called immediately
  before the effect, performs every read again and requires the same answers.
  * `read(self, key: tuple[Any, ...], reader: Callable[[], _T]) -> _T`
  * `recheck(self) -> None`

## `mod_base.build_ci.records`

Owner: MB11. Strict evidence records with exact identity/attempt/coverage checks. Structural
validity alone is never status authority. Full admission supplies a protected plan and independently
authenticates the API, graph, native witnesses and actual frozen bytes.

* `GATE_MODES`: gate -> producer (the managed caller whose run sealed it) -> the run modes that end
  in that gate's receipt: `build` from a `full` Build run or a `rebuilt` packaged run; `packaged`
  from a `pull-request`, `selected` or `rebuilt` packaged run. Deferred and reuse runs seal none.
* `def validate_descriptor(value: Any, path: str = '$') -> dict[str, Any]`: A producer is a run of a managed caller, recorded under the pull request's head commit or, for a protected push or dispatch, the commit it both runs from and tests. Runtime, results and packaged-gate artifacts come from the packaged caller; a pull request's Build artifacts from its Build caller. Times are whole-second UTC.
* `def validate_build_envelope(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: Files are export paths with `lane_id` null for a file of the target as a whole (`protocol.check_output_scope`); every output role has its own size bound (JAR, `MAX_CI_SBOM_BYTES`, the profile's native-report bound, `MAX_CI_LOG_BYTES`). With a plan the files equal the exact planned outputs of the target or of the whole Build, scope included.
* `def bind_build_envelope(envelope: dict[str, Any], *, descriptor: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]`: Strictly bind pre-upload producer identity, full plan/profile and artifact scope/target to a selected descriptor retaining actual API window and immutable artifact metadata. Pure structural binding; API authentication and native validity remain independently required.
* `def validate_source_selection(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`
* `def bind_source_selection(document: dict[str, Any], *, plan: dict[str, Any], run_id: int, run_attempt: int, workflow_path: str) -> dict[str, Any]`: Bind a selection record to the plan and to the run attempt that consumes it: only the attempt of the packaged caller that requested it (so a rerun of failed jobs alone never inherits an earlier attempt's Build); a rebuilt Build serves the run attempt that built it, every other Build comes from a separate run of the Build caller. Structural; the consumer authenticates the Build through the API again.
* `def build_source_selection(*, plan: dict[str, Any], request: dict[str, Any], build: dict[str, Any], envelope: dict[str, Any]) -> dict[str, Any]`: The `mod-base.ci.selection` record of the exact Build one packaged run attempt consumes: the plan's binding, the request (`run_id`, `run_attempt`, `nonce`, `workflow_path`, `workflow_ref`), the authenticated descriptor of the complete bundle and the canonical SHA-256 of the envelope read from its verified bytes; bound with `bind_source_selection` before it is returned.
* `def validate_gate_receipt(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`
* `def bind_gate_receipt(document: dict[str, Any], *, descriptor: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]`: Bind a pre-upload full gate receipt to its selected tested-record identity/unit and actual upload window; all source uploads precede record upload and source IDs cannot collide with the record. Full API/native execution proof remains separate.
* `def gate_receipt(*, plan: dict[str, Any], producer: dict[str, Any], gate: str, mode: str, artifacts: list[dict[str, Any]], owning_build: dict[str, Any] | None, native_receipts: list[dict[str, Any]]) -> dict[str, Any]`: The receipt the gate job of `producer` (an attempt's identity, without an upload window) seals for `plan`: a new `mod-base.ci.gate` document, valid for the plan and independent of its arguments. Writing one proves nothing; the gate authenticates what it names first (`gate.seal_gate`).
* `def validate_results_index(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: `mod-base.ci.results` v1, the complete packaged results of one attempt as an index of its lanes: the sealing attempt (always the packaged caller), the owning Build (the one the run rebuilt, or one of a separate Build run) with its envelope's SHA-256, and for every lane its runtime artifact of the same attempt, the SHA-256 of the runtime envelope and of the validation record inside it and of the lane's verification report. With a plan the lanes are exactly the planned ones with their native contracts, in plan order.
* `def results_index(*, plan: dict[str, Any], producer: dict[str, Any], owning_build: dict[str, Any], build_envelope_sha256: str, lanes: list[dict[str, Any]]) -> dict[str, Any]`: The results index the aggregating job of `producer` seals for `plan`: a new `mod-base.ci.results` document, valid for the plan and independent of its arguments. `lanes` holds `descriptor`, `envelope_sha256`, `validation_sha256` and `report_sha256` of every planned lane in plan order; ids and native contracts are the plan's. The job authenticates each lane first (`gate.seal_results`).
* `def bind_results_index(document: dict[str, Any], *, descriptor: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]`: Bind a results index to the results artifact it was read from: same sealing attempt, the artifact kind `results`, no id shared with a lane or the owning Build, and every one of them uploaded before the index.
* `def validate_reuse_reference(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`
* `def bind_reuse_reference(document: dict[str, Any], *, descriptor: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]`: Bind direct reuse identity to its selected reuse-record upload, retaining source-before-record chronology and ID separation. K6 must additionally prove actual source completion before protected verifier start, full original graphs, coherent tree/policy equality and source availability.

## `mod_base.build_ci.config`

* `BUILD_CONFIG_PATH`: Fixed scripts/ci/mod-base-build.json; controller retains the same public alias.

Owner: MB11. Closed data at scripts/ci/mod-base-build.json. Protected policy must authenticate
every source hash/path before import and confirm native timeout parity before activation.

* `def validate_build_config(document: Any, *, path: str = '$') -> dict[str, Any]`: The pure schema (docs/SCHEMAS.md): entry points and their hashed closure, `inventory.path`, `scenario_contract.path`, `plan_inputs` (0..`MAX_CI_PLAN_INPUTS` extra candidate files `{name, path}` sorted by name, each name an `adapter.plan_input_name`), `bundle.path`, the two `contexts` and the timeouts; no named path may alias or contain another.
* `class BuildConfigError(MbError)`: The protected Build configuration or a source it lists cannot be trusted (reason `ci-config`).
* `class AdapterFile`: One source of the protected adapter import closure, as read from the protected checkout.
  * fields: `path: str, sha256: str, data: bytes`
* `class BuildConfig`: A validated protected Build config with the exact bytes of everything it lists; `files` is the closure in the config's order.
  * fields: `data: dict[str, Any], raw: bytes, sha256: str, files: tuple[AdapterFile, ...]`
* `def load_build_config(repo_root: Path, *, repository: str) -> BuildConfig`: Read the config of `repository` from the protected mod checkout and every source it lists, without crossing a symlink; each source must have its configured SHA-256, within the per-file and whole-closure byte caps. The result is the input of `identity.policy_sha256` and of the validator's adapter copy.

## `mod_base.build_ci.adapter`

Owner: MB11. The Build adapter contract (`BUILD_ADAPTER_API = 1`, docs/BUILD-ADAPTER.md): the closed
hooks, their argv and extra environment, the fixed file names and the strict parsers of what a hook
writes. It imports no worker module; `tests/test_ci_adapter.py` pins its directories to theirs.

* `class AdapterError(MbError)`: A hook was requested outside the closed adapter contract (reason `ci-adapter`).
* `class Hook`: One hook of the contract: the worker account that runs it (`validator` hooks are the protected ones), the plan unit a run is for (`target`, `lane` or `None`) and the key of the config's `timeouts`.
  * fields: `name: str, role: str, unit: str | None, timeout: str`
* `HOOKS`: hook name -> `Hook`, all eight: `derive_plan`, `policy`, `build_target`, `verify_target`, `verify_build`, `derive_runtime`, `run_lane`, `verify_runtime`.
* `PROTECTED_HOOKS = ('derive_plan', 'verify_target', 'verify_build', 'derive_runtime', 'verify_runtime')`
* `CANDIDATE_HOOKS = ('policy', 'build_target', 'run_lane')`
* `UNIT_ENVIRONMENT = {'target': 'MB_TARGET_ID', 'lane': 'MB_LANE_ID'}`
* `RUNTIME_VALUES = ('E2E_ROW_JSON', 'E2E_SCENARIOS')`
* `INVENTORY_INPUT = 'inventory'`
* `SCENARIO_INPUT = 'scenario-contract'`
* `PLAN_INPUT = 'ci-plan.json'`
* `RESERVED_INPUT_NAMES = frozenset({'inventory', 'scenario-contract', 'ci-plan.json'})`: the names of `validation-input/` the kit gives out itself.
* `PLAN_OUTPUT = 'plan.json'`
* `RUNTIME_OUTPUT = 'runtime.json'`
* `REPORT_SUFFIX = '.json'`
* `RESERVED_UNIT_IDS = frozenset({'plan', 'runtime', 'ci-validation'})`
* `CHECKOUT_DIRECTORY = {'validator': 'controller', 'candidate': 'repository'}`
* `HOME_DIRECTORY = {'validator': 'validator-home', 'candidate': 'candidate-home'}`
* `INPUT_DIRECTORY = 'validation-input'`
* `SEALED_BUILD_DIRECTORY = 'sealed-build'`
* `SEALED_RUNTIME_DIRECTORY = 'sealed-runtime'`
* `OUTPUT_DIRECTORY = 'validation'`
* `EXPORT_DIRECTORY = 'export'`
* `def hook_command(hook: str, *, python: str, checkout: str, dispatcher: str) -> tuple[str, ...]`: The fixed argv `(python, '-I', '-B', '<checkout>/<dispatcher>', '--hook', hook)`; `checkout` is the role's checkout directory and `dispatcher` the config's `adapter.dispatcher`.
* `def hook_values(hook: str, *, unit_id: str | None = None, runtime: Mapping[str, str] | None = None) -> dict[str, str]`: The extra environment of one hook run beyond `worker.worker_environment`'s fixed names: the unit id under `UNIT_ENVIRONMENT`, and for `run_lane` only the parsed `derive_runtime` values.
* `def hook_timeout_seconds(hook: str, config: Mapping[str, Any]) -> int`: The timeout the validated protected config sets for the hook.
* `def plan_unit(plan: dict[str, Any], hook: str, unit_id: str | None) -> dict[str, Any] | None`: The target or lane of the protected plan a hook run is for; a unit outside the plan is a rejection.
* `def plan_input_name(value: Any, path: str) -> str`: The validator of the name a config gives an extra plan input: a `protocol.ID` token that is not one of `RESERVED_INPUT_NAMES`.
* `def plan_sources(config: Mapping[str, Any]) -> dict[str, str]`: `{staged name: repository path}` of every candidate file a plan is derived from, in staging order: `inventory`, `scenario-contract`, then the config's `plan_inputs` by name. `config` is the validated config document. A job stages the blob of each path at the tested tree under its name in `validation-input/` and passes the same bytes to `planning.build_plan`.
* `def hook_inputs(hook: str, config: Mapping[str, Any]) -> tuple[str, ...]`: The files of `validation-input/` a protected hook may read: every candidate file of `plan_sources(config)`, and for every hook after `derive_plan` the plan first. Empty for a candidate hook.
* `def hook_outputs(hook: str, *, plan: dict[str, Any] | None = None, unit_id: str | None = None) -> tuple[str, ...]`: The exact files a protected hook must leave in `validation/`: `plan.json`, `runtime.json`, or `<unit id>.json` per verified unit (for `verify_build` every target, in plan order).
* `def target_outputs(plan: dict[str, Any], target_id: str) -> tuple[str, ...]`: The exact files `build_target` must leave below `export/` for one target, sorted.
* `def parse_derived_plan(data: bytes) -> dict[str, Any]`: Strictly decode `derive_plan`'s `plan.json` (at most `MAX_CI_PLAN_BYTES`): `protocol.validate_plan_units` and no reserved unit id.
* `def parse_runtime_values(data: bytes) -> dict[str, str]`: Strictly decode `derive_runtime`'s `runtime.json` (at most `MAX_CI_REPORT_BYTES`): `{"values": {...}}` holding exactly `RUNTIME_VALUES`, each non-blank text without control characters of at most `MAX_CI_ENV_VALUE_BYTES` bytes.

## `mod_base.build_ci.identity`

Owner: MB11. What `ci subject` does: authenticate the tested subject through the API and keep it in
the job's private state directory. Mutable state (default branch, pull request) is read at the
start and again before the record is written; the commit object is read once.

* `IDENTITY_NAME = 'identity.json'`
* `PULL_REQUEST_EVENT = 'pull_request_target'`
* `PROTECTED_EVENTS = ('push', 'workflow_dispatch', 'schedule')`
* `POLICY_FORMAT = 'mod-base.build.policy-v1'`
* `class SubjectError(MbError)`: The subject of this job cannot be authenticated (reason `ci-subject`; `draft`, `no-test-merge` and `controller-moved` for those three cases).
* `class StateError(MbError)`: The job's private state directory or one of its records cannot be trusted (reason `ci-state`).
* `def run_workflows(producer: str, *, pull_request: bool) -> tuple[str, ...]`: The managed callers a job of `producer` may run from: its own, and for Build jobs of a protected subject also the packaged caller (its `rebuild` job).
* `def validate_subject_record(document: Any, path: str = '$') -> dict[str, Any]`: The closed identity record `{producer, event, workflow_path, controller_tree, subject}`; `subject` is `protocol.validate_subject` and always names the Build caller as `controller_workflow`.
* `def authenticate_subject(invocation: Invocation, api: GitHubApi, *, producer: str, pr_number: int | None) -> dict[str, Any]`: Authenticate a pull request (`authenticate.read_pr_generation`, not a draft, test merge with parents exactly `[base, head]`) or a protected push/dispatch/schedule (the live default-branch head is the executing commit) and return the identity record. The environment's claims are checked before the first request; 4 requests for a pull request, 5 otherwise; nothing is written.
* `def policy_sha256(config: BuildConfig, subject: dict[str, Any]) -> str`: The closed protected-policy digest: canonical SHA-256 of `{format: POLICY_FORMAT, build_adapter_api, graph_versions: {build, packaged}, kit: subject.kit, config_sha256, adapter_files: [{path, sha256}]}`. It changes with the protected Build config bytes, any source of the adapter closure, the kit pin or tree digest, the adapter API or a graph version, and with nothing else.
* `def create_state(state: Path) -> None`: Create the job's private state directory (mode 0700); an existing path is never adopted.
* `def write_state_record(state: Path, name: str, raw: bytes) -> None`: Create `<state>/<name>` (mode 0600) in an existing private state directory; never replaces a record.
* `def read_state_record(state: Path, name: str, *, max_bytes: int) -> bytes`: Bytes of `<state>/<name>`. The directory is this user's with mode 0700, the record a single-link regular file of this user with mode 0600 and 1..`max_bytes` bytes, unchanged while read.
* `def write_subject(state: Path, record: dict[str, Any]) -> None`: Create the state directory and write the validated record as canonical JSON to `<state>/identity.json`.
* `def read_subject(state: Path) -> dict[str, Any]`: The identity record, strictly decoded, validated and required to be canonical.

## `mod_base.build_ci.planning`

Owner: MB11. Pure functions that turn a subject, the protected config and one `derive_plan` result
into the protected plan. Running the hook belongs to the job (`ci plan`).

* `class PlanError(MbError)`: A plan is not the one this job must work on (reason `ci-plan`; `plan-mismatch` for another hash than the expected one).
* `def runtime_selection_sha256(profile: str, lanes: list[dict[str, Any]]) -> str`: Canonical SHA-256 of `{profile, lanes}`, that is every derived lane with its native contract and ordered obligations.
* `def build_plan(*, subject: dict[str, Any], config: BuildConfig, inventory: bytes, scenario_contract: bytes, plan_inputs: Mapping[str, bytes], derived: bytes) -> dict[str, Any]`: The complete validated `mod-base.build.plan`, built from the subject plus `policy_sha256` (`identity.policy_sha256`), the inventory's Git blob id and SHA-256, the scenario contract's SHA-256 and `runtime_selection_sha256`; the config's profile; `plan_inputs` as `[{name, sha256}]` in the config's order; the parsed `derive_plan` units; `plan_sha256`. `inventory`, `scenario_contract` and the values of `plan_inputs` are the candidate Git blobs at the tested tree (1..`MAX_CI_PLAN_SOURCE_BYTES` bytes each), exactly what the hook was given; `plan_inputs` has one entry for every name of the config's `plan_inputs` and no other (`{}` when the config names none).
* `def require_plan(plan: dict[str, Any], *, subject: dict[str, Any], expected_sha256: str | None = None) -> dict[str, Any]`: Require a valid plan of exactly this subject and, when given, the expected `plan_sha256` (`ci plan --expect-sha256`).
* `def matrices(plan: dict[str, Any]) -> dict[str, list[str]]`: The ids of `{targets, lanes}` in plan order.
* `def plan_outputs(plan: dict[str, Any]) -> dict[str, str]`: The workflow outputs `plan_sha256`, `targets` and `lanes` (single-line JSON arrays of ids).

## `mod_base.build_ci.commands`

Owner: MB11. The top-level `ci` command. Each work area lists its verb module in `VERB_MODULES`.

* `VERB_MODULES`: the modules whose `add_verbs(verbs)` adds verbs to `ci`, one line per work area.
* `def register(subparsers: argparse._SubParsersAction) -> None`
* `def add_job_arguments(parser: argparse.ArgumentParser) -> None`: Add what every verb takes: `--repo`, `--config` and the required `--state DIR`.
* `def api_client(invocation: runtime.Invocation, *, max_requests: int, writable: bool = False) -> github_api.GitHubApi`: The API client of one command, bounded to `max_requests` requests in all; read-only unless the verb writes to GitHub and passes `writable=True`.

## `mod_base.build_ci.commands_subject`

Owner: MB11. `ci subject --repo DIR --config F --state DIR --producer build|packaged --pr N --github-output F`:
authenticates the subject, creates `--state` with `identity.json` and outputs `tested_sha` and
`pr_number` (`--pr` and the output are empty for a protected subject).

* `PULL_REQUEST`: the argparse type of `--pr`, a positive decimal as `int` or the empty string as `None`.
* `def add_verbs(verbs: argparse._SubParsersAction) -> None`
* `def run_subject(args: argparse.Namespace) -> int`

## `mod_base.build_ci.lifecycle`

Owner: MB11. The worker lifecycle of one Build or packaged job, as the `ci worker-*` and `ci plan`
commands compose it. Every step is one command run by the runner; the steps share the job's
private state directory and the fixed worker root. State records are canonical JSON, written once
and read strictly: `worker-host.json` (`{"boundary"}`: the runner home as it was, written before
anything is changed), `worker.json` (`{"boundary", "accounts": {"candidate": {"uid", "gid"} | null,
"validator": {"uid", "gid"}}, "python", "java_homes", "tools": {"roots", "metadata_sha256", "files",
"entries", "total_bytes"}, "config_sha256"}`) and `ci-plan.json` (the plan). Between two hook runs
both accounts are terminated and locked.

* `HOST_NAME = 'worker-host.json'`
* `WORKER_NAME = 'worker.json'`
* `PLAN_NAME = 'ci-plan.json'`
* `ROLE_SETS`: `ci worker-prepare --roles` value -> the roles a job allocates, in allocation order (`validator`, `candidate+validator`).
* `class LifecycleError(MbError)`: A job step was asked for in a state the lifecycle does not allow (exit 2, reason `ci-lifecycle`; a failed read of the candidate checkout has reason `git`).
* `class Job`: What a lifecycle command knows of its job once `ci subject` has run.
  * fields: `state: Path, record: dict[str, Any], config: BuildConfig, sources: ControllerSources, kit_root: Path, kit_digest: str, run_id: int, run_attempt: int`
  * `subject` (property) -> `dict[str, Any]`
* `class Worker`: The prepared worker of a job, as `worker.json` records it; `accounts` holds the allocated roles only.
  * fields: `boundary: HostBoundary, accounts: Mapping[str, WorkerAccount], python: str, java_homes: tuple[str, ...], tools: ToolTreeProof, config_sha256: str`
  * `validator` (property) -> `WorkerAccount`
  * `java_home` (property) -> `str | None`
* `def open_job(invocation: Invocation, state: Path) -> Job`: Bind a command to the job `ci subject` authenticated: the state must belong to the executing repository, controller commit and kit (version and recomputed tree digest); the protected Build config and its adapter closure are read from the verified mod checkout (`config.load_build_config`, `controller.checkout_controller_sources`); the run id and attempt come from the invocation. No API read.
* `def read_worker(state: Path) -> Worker`: The strictly decoded `worker.json`: closed shape, canonical paths, and accounts that differ in user and group from the runner and from each other. The host is not consulted.
* `def open_worker(job: Job) -> Worker`: `read_worker` checked against the host: the protected config digest is the recorded one, the runner home is still fenced, every recorded account is the live one, a role the job did not allocate has no account, and the admitted tool trees are unchanged (`toolchain.authenticate_toolchains`: the interpreter in them is what the command runs as root next). Fails after `ci worker-finish` reopened the home.
* `def read_plan(job: Job) -> dict[str, Any]`: The strictly decoded `ci-plan.json`: canonical, valid and of exactly the job's subject.
* `def rest_workers(accounts: Mapping[str, WorkerAccount]) -> None`: Terminate and lock every account, each one even when another fails; raise the first failure.
* `def resting(accounts: Mapping[str, WorkerAccount]) -> Iterator[None]`: Context manager: whatever the block does, every account of `accounts` (read when the block ends) ends terminated and locked. A failure of the block is the one reported.
* `def tool_roots(python: str, java_homes: tuple[str, ...]) -> tuple[str, ...]`: The tool trees a job's hooks execute from: the prefix of `<prefix>/bin/<interpreter>` as named and as its links resolve, then every JDK home, without repeats. Naming only; `toolchain.inspect_worker_toolchains` admits them.
* `def prepare_worker(invocation: Invocation, job: Job, *, roles: tuple[str, ...], python: str, java_homes: tuple[str, ...]) -> Worker`: `ci worker-prepare`. Admit the hosted layout (`RUNNER_ENVIRONMENT`, `GITHUB_WORKSPACE`, `RUNNER_TEMP`) and write `worker-host.json` before anything is changed (a second prepare of the job stops there); create the worker boundary; close the runner home; run the `host-fence` root operation; admit the tool trees and bind the interpreter and every JDK to them; allocate the accounts; copy the protected adapter closure into `controller/` and run `grant-controller`; leave every account terminated and locked; write `worker.json`. A failure leaves the home closed and the allocated accounts locked.
* `def api_candidate_files(api: GitHubApi, job: Job) -> dict[str, bytes]`: The candidate files a plan is derived from, read from the API at the tested tree: staged name -> bytes, in the order of `adapter.plan_sources` (the inventory, the scenario contract, then the extra plan inputs). One request for the recursive tree and one per file. A path must be a regular file of 1 byte to `MAX_CI_PLAN_SOURCE_BYTES` below real directories.
* `def checkout_candidate_files(checkout: Path, job: Job) -> dict[str, bytes]`: The same mapping from the Git objects of the candidate checkout, never from its working files and without a request: `HEAD` must be the tested commit with the tested tree, a path must be a regular file in that commit's tree, and each blob's id is recomputed. Git runs as `/usr/bin/git` with a fixed environment: no system or global configuration, no replacement objects, no lazy fetch, no hooks.
* `def run_protected_hook(job: Job, worker: Worker, hook: str, *, plan: dict[str, Any] | None, unit_id: str | None, log: Callable[[str], object]) -> WorkerResult`: Run one protected hook as the validator (`controller.execute_controller_validator`; `plan` is `None` only for `derive_plan`) with the job's interpreter, first JDK, tool receipt and run identity, and pass its neutralised log to `log`, also when the hook fails.
* `def derive_plan(job: Job, worker: Worker, sources: Mapping[str, bytes], *, expected_sha256: str | None, log: Callable[[str], object]) -> dict[str, Any]`: `ci plan`. `sources` is what one of the two readers above returned and must hold exactly the candidate files the protected config names. Stage them in `validation-input/`, run `grant-plan-inputs`, run `derive_plan` as the validator, run `take-derived-plan`, build the plan (`planning.build_plan`), require `expected_sha256` when given, stage the complete input root again with the plan and run `grant-validation-inputs`, write `ci-plan.json`. Refused when the job already has a plan. Every account ends terminated and locked whatever happens; no plan record exists after a failure.
* `def finish_worker(state: Path) -> dict[str, Any]`: `ci worker-finish`. Terminate and lock every worker account the host has (an entry that cannot be authenticated is locked by name and fails the command); require that neither owns a process once both are locked; then restore the runner home from `worker-host.json`. A home whose accounts could not all be stopped stays closed. Safe to run twice, after a failed prepare and without one. Returns `{"accounts": {role: "locked" | "absent"}, "home_mode": int | None}`.

## `mod_base.build_ci.commands_worker`

Owner: MB11. `ci worker-prepare --repo DIR --config F --state DIR --roles validator|candidate+validator --python PATH [--java-home PATH]...`,
`ci plan --repo DIR --config F --state DIR [--candidate DIR] [--expect-sha256 HEX] [--github-output F]`
(outputs `plan_sha256`, `targets`, `lanes`) and `ci worker-finish --repo DIR --config F --state DIR`
(`if: always()`; it reads only `--state` and the host). A hook's output is printed neutralised.

* `TOOL_PATH`: the argparse type of `--python` and `--java-home`, a canonical absolute POSIX path.
* `PLAN_SHA256`: the argparse type of `--expect-sha256`, 64 lower-case hex digits.
* `def add_verbs(verbs: argparse._SubParsersAction) -> None`
* `def run_worker_prepare(args: argparse.Namespace) -> int`
* `def run_plan(args: argparse.Namespace) -> int`
* `def run_worker_finish(args: argparse.Namespace) -> int`

## `mod_base.build_ci.controller`

Owner: MB11. The protected adapter copy of a job and the hooks the validator runs from it. The
sources are admitted from the API (`authenticate_controller_sources`) or, inside a job, from the
mod checkout the prologue verified (`checkout_controller_sources`). Native protected-path policy,
owner authorization, import-root closure and installer provenance remain separate requirements.
A candidate-supplied receipt confers no authority. Wherever a function takes an `identity`, it is
the subject `ci subject` authenticated or the complete identity of a plan (`worker.execution_subject`).

* `BUILD_CONFIG_PATH = 'scripts/ci/mod-base-build.json'`
* `CONTROLLER_VALIDATION_ROOT`: Fixed protected source copy under WORKER_ROOT/controller, matching the validator dispatch root.
* `VALIDATOR_HOOKS`: The hooks the validator runs from the protected copy: every protected hook of the adapter contract (`adapter.PROTECTED_HOOKS`), so `derive_plan` and `derive_runtime` as well as the three `verify_*` hooks. No candidate hook runs through this route.
* `class ControllerFile`
  * fields: `path: str, mode: str, git_blob: str, sha256: str, data: bytes`
* `class ControllerSources`
  * fields: `controller_sha: str, controller_tree: str, config: ControllerFile, files: tuple[ControllerFile, ...]`
* `def authenticate_controller_sources(api: GitHubApi, *, identity: dict[str, Any], protected_paths: tuple[str, ...]) -> ControllerSources`: Bracket immutable protected-controller tree/blob reads with live PR authentication; require approved paths, regular Git modes and configured source hashes before returning evidence.
* `def checkout_controller_sources(config: BuildConfig, *, controller_sha: str, controller_tree: str) -> ControllerSources`: The source receipt of the protected adapter as the prologue-verified mod checkout holds it: the bytes `config.load_build_config` read and compared with their configured hashes, with their Git blob ids computed from the same bytes and plain file modes (the validator's copy is run through the interpreter). No API read.
* `class ControllerActivation`: Frozen constructible sources/manifest observation; never transition or execution authority.
  * fields: `sources: ControllerSources, manifest: ControllerFile`
* `def authenticate_controller_activation(api: GitHubApi, *, identity: dict[str, Any], protected_paths: tuple[str, ...]) -> ControllerActivation`: Read fixed activation data only from the API-authenticated original controller tree under independently admitted native policy. Bind normal bounded config/import sources; require regular non-executable activation/real site ancestor with no case aliases, 8 KiB bound, exact tree/blob length and rehashed blob identity, strict kind and matching config repository/profile. Reauthenticate sources, reread manifest and final live source identity before returning bytes. No candidate manifest, constructor/owner approval, protected transition, predecessor/caller-byte or physical import enrollment authority.
* `def verify_controller_source_copy(root: Path, *, sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]`: Recheck minimal source copy bytes/modes/blob hashes against retained protected evidence; refuse undeclared files and Git metadata without importing code.
* `def materialize_controller_sources(output: Path, *, sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]`: Prevalidate retained protected byte evidence, write exclusive descriptor-relative regular files into a private stage, independently verify the exact copy and publish atomically without replacing an existing output. Protected-parent ownership and authentic receipt retention remain caller obligations.
* `def prepare_controller_validation(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, identity: dict[str, Any]) -> dict[str, Any]`: Protected-root-only fixed source handoff; authenticate host/accounts/layout, terminate the candidate when the job has one (`worker.authenticate_peer_account`), verify private bytes/modes, grant only validator-group reads and recheck normalized regular modes, hashes/inode/host. Accepted-copy failure restores private traversal. Import enrollment, provenance and native execution/sealing remain required.
* `def execute_controller_validator(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, tools: ToolTreeProof, plan: dict[str, Any] | None, hook: str, unit_id: str | None, python: str, java_home: str | None, run_id: int, run_attempt: int, subject: dict[str, Any] | None = None) -> WorkerResult`: Run one protected hook of the adapter contract (`VALIDATOR_HOOKS`) as the validator. `derive_plan` takes the `subject` and no plan; every other hook takes the protected `plan`, never a subject, and its unit must be the target or lane the contract gives that hook (`adapter.plan_unit`). The argv, the unit environment and the timeout come from `adapter.hook_command`, `adapter.hook_values` and `adapter.hook_timeout_seconds` with the protected config. Metadata/byte/host checks before and after, existing tool fence and mandatory final UID termination. Caller must authenticate installer/import closure and native inputs; exit zero/logs do not confer receipt/upload/status authority.

## `mod_base.build_ci.inputs`

Owner: MB11. The validator's fixed inputs and what a derivation hands back. `validation-input/`
holds the bytes of the candidate files a plan is derived from, each under its staged name
(`inventory`, `scenario-contract` and the extra plan inputs the protected config names:
`adapter.plan_sources`) and, once the plan exists, the canonical plan (`ci-plan.json`), which
binds every one of them by SHA-256. The runner stages the directory privately, root hands it to the
validator's group read-only, and every check requires exactly the files of its state. Also the
aggregate/target verifier composition over these inputs. Retained protected provenance, native
semantics and complete import/installer closure remain required. These helpers do not authorize
uploads, workflow execution or status publication.

* `VALIDATOR_INPUT_ROOT`: Fixed `validation-input/` directory under the worker boundary.
* `DERIVED_PLAN_ROOT`: Fixed runner-private `derived-plan/` directory: the copy of what `derive_plan` wrote, as root handed it over.
* `class BuildValidationExecution`
  * fields: `execution: WorkerResult, input_sha256: str`
* `def plan_source_digests(plan: dict[str, Any]) -> dict[str, str]`: Staged name -> SHA-256 of every candidate file a valid plan was derived from, in the order of `validation-input/`: the inventory and the scenario contract its identity binds, then the extra plan inputs it lists.
* `def verify_validation_inputs(root: Path, *, digests: Mapping[str, str], plan: dict[str, Any] | None = None) -> None`: Require exactly the validator's input files with these bytes and no other entry: the candidate files `digests` names with their SHA-256 (always the inventory and the scenario contract, and extra plan inputs under their own names) and, when `plan` is given, its canonical bytes (it must bind exactly these digests). Names and bytes only; ownership and modes are separate admissions.
* `def verify_validation_plan(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]`: `verify_validation_inputs` for a hook that runs against the plan: the canonical plan and every candidate file it binds (`plan_source_digests`), nothing else.
* `def materialize_validation_inputs(output: Path, *, sources: Mapping[str, bytes], plan: dict[str, Any] | None = None) -> None`: Write the candidate bytes (`sources`: staged name -> 1 byte to `MAX_CI_PLAN_SOURCE_BYTES`) and, when given, the plan through an exclusive descriptor stage and independently verify before atomic no-replace publication. Caller protects the parent and excludes other writers.
* `def prepare_plan_inputs(*, boundary: HostBoundary, validator: WorkerAccount, digests: Mapping[str, str]) -> None`: Root-only read handoff of the staged candidate files (`digests`: staged name -> SHA-256) before `derive_plan` runs; the same admission as `prepare_validation_plan` for the state without a plan.
* `def prepare_validation_plan(*, boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any]) -> dict[str, Any]`: Root-only read handoff of the complete input root; authenticate host/layout/accounts, terminate the candidate when the job has one, require a private runner-owned directory, check bytes, grant only validator-group reads and recheck metadata/inode/bytes. Failures restore admitted copy traversal to private.
* `def replace_plan_inputs(*, boundary: HostBoundary, validator: WorkerAccount, sources: Mapping[str, bytes], plan: dict[str, Any]) -> None`: Runner-only. Terminate the validator, require the directory it was granted to be exactly the staged candidate files `sources`, remove it and stage the complete private root (with the plan) for `prepare_validation_plan`.
* `def take_derived_plan(*, boundary: HostBoundary, validator: WorkerAccount) -> None`: Root-only. Terminate the validator; require `validator-home/validation/` to be exactly the one file `adapter.hook_outputs("derive_plan")` names, private (0700 directory, 0600 single-link regular file of the validator) and within `MAX_CI_PLAN_SOURCE_BYTES`; copy it into the new runner-private `DERIVED_PLAN_ROOT` (independent inodes, 0700/0600, never a chown of the original) and remove the original directory, so the validator's next hook finds no output of this one. A missing or an extra file, a link or another mode fails and hands nothing over.
* `def execute_frozen_build_validator(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, tools: ToolTreeProof, plan: dict[str, Any], envelope: dict[str, Any], python: str, java_home: str | None, run_id: int, run_attempt: int) -> BuildValidationExecution`: Same-producer aggregate verification only. Require complete retained envelope and exact run/attempt, authenticate fixed plan/Build metadata/bytes and stable directory identities before and after closed protected verify_build execution; always terminate admitted validator. Return retained execution plus canonical envelope input digest for output freezing. Native semantics/provenance and final authority remain separate.
* `def execute_frozen_target_validator(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, tools: ToolTreeProof, plan: dict[str, Any], envelope: dict[str, Any], target_id: str, python: str, java_home: str | None, run_id: int, run_attempt: int) -> BuildValidationExecution`: Require the exact protected enrolled target partition and same producing run/attempt; use the shared fixed read-only plan/Build input lifecycle, closed verify_target/unit execution and mandatory validator termination. Retain actual execution and canonical partition digest for output freezing. Complete or other-target bundles cannot substitute; native semantics/provenance/API authority remain separate.
* `def freeze_frozen_build_validation(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, bound: BuildValidationExecution, plan: dict[str, Any], envelope: dict[str, Any], run_id: int, run_attempt: int) -> dict[str, Any]`: Root-only receipt freeze tied to retained successful execution and the exact canonical input digest/producer attempt. Derive closed hook/unit from the independently retained complete/target envelope, snapshot plan/envelope and inspect read-only input bytes/metadata/inodes before and after independent validation export freeze. Reject disappearance/substitution/drift; always quiesce admitted validator. Genuine protected execution/source provenance remains required across privilege transition; constructible values, private copies or a matching receipt never establish native/API/upload/status authority alone. On late failure a private freeze copy may remain; do not consume it or upload after failure.

## `mod_base.build_ci.policy`

Owner: MB11. Native policy-runner count parity and bounded UTF-8 diagnostics. Discovery and
suite execution belong only in credentialless disposable workers; these in-process counts do
not authenticate candidate reports, protected source provenance, native policy or statuses.

* `POLICY_PROFILES`: Closed block-pops/quick-skin count/discovery profiles; kit defaults stay strict.
* `class PolicyError(MbError)`
* `class PolicyCounts`
  * fields: `tests_run: int, failures: int, errors: int, skipped: int, class_skips: int, expected_failures: int, unexpected_successes: int, successful: bool`
* `def admit_policy_unit(*, profile: str, discovered: int, repeat: int, fixture: bool, counts: PolicyCounts) -> int`: Require retained protected scheduling/discovery metadata and native outcome success/count parity; return tests skipped with an admitted QS whole-class setUpClass skip. BP requires full exact count. Caller still requires complete discovery/worker results and nonzero total testsRun; constructed counts confer no authority.
* `class BoundedPolicyStream`
  * `__init__(self, max_bytes: int = limits.MAX_CI_LOG_BYTES) -> None`
  * `truncated` (property) -> `bool`
  * `writable(self) -> bool`
  * `write(self, value: str) -> int`
  * `flush(self) -> None`
  * `getvalue(self) -> str`

## `mod_base.build_ci.validation`

Owner: MB11. New inactive mod-base.ci.validation v1 protocol and exact verifier-output bytes.
Native closed report schemas/semantics, actual protected execution and quiescent readable source
provenance remain required. A matching record/copy does not authorize upload or successful status.

* `VALIDATOR_OUTPUT_ROOT`, `SEALED_VALIDATION_ROOT`: Fixed private verifier output and independent runner-private frozen copy.

* `def validate_validation_receipt(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`
* `def verify_validation_export(root: Path, *, plan: dict[str, Any], hook: str, unit_id: str | None, run_id: int, run_attempt: int, source_config_sha256: str, input_sha256: str) -> dict[str, Any]`: Require exact protected execution/input context, canonical outer/native JSON, exact report inventory and unchanged record bytes. Native closed-schema semantics and source lifecycle remain separate.
* `def materialize_validation_export(root: Path, output: Path, *, plan: dict[str, Any], hook: str, unit_id: str | None, run_id: int, run_attempt: int, source_config_sha256: str, input_sha256: str) -> dict[str, Any]`: Independently copy admitted verifier files into a private stage, reverify before exclusive atomic publication and never replace an existing output. Caller must stop/reclaim/exclude source writers and protect ancestors.
* `def freeze_validation_export(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, execution: WorkerResult, plan: dict[str, Any], hook: str, unit_id: str | None, run_id: int, run_attempt: int, input_sha256: str) -> dict[str, Any]`: Protected-root-only fixed verifier output freeze. Require retained successful actual execution/source evidence, bind host/accounts/layout, terminate validator, admit private original metadata and independently copy verified bytes, transfer only the protected copy to runner-private ownership and recheck content/inode/metadata/host. Native semantics, input authenticity and final gate/API integration remain required.

## `mod_base.build_ci.exports`

Exact planned target-union and frozen export byte checks. Independent API, worker isolation and
native domain validation remain required; these functions do not confer execution authority.

* `def validate_target_partitions(partitions: Any, *, plan: dict[str, Any]) -> list[dict[str, Any]]`
* `def verify_build_export(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]`
* `def materialize_build_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]`: Verify original admission, stream independent regular-file copies and independently verify the private stage before exclusive atomic publication. Source quiescence/reclamation, output-parent protection and second-UID native validation remain caller obligations.
* `def assemble_build_export(inputs: Path, *, partitions: list[dict[str, Any]], plan: dict[str, Any], run_id: int, run_attempt: int, output: Path) -> dict[str, Any]`: Require the full exact ordered same-attempt target union and fixed target-ordinal input layout. Verify actual target bytes before and after independent selected-file copying, bound the complete logical export/envelope, generate a current-version canonical complete envelope and independently verify the private stage before one exclusive atomic publication. Exclude partition envelopes and root overlap. Caller retains API/source/policy authority and protects ancestors; native aggregate validation and upload/gate admission remain separate.
* `BUILD_VALIDATION_ROOT`: Fixed independent Build copy under the disposable traversal root; callers cannot choose a handoff path.
* `CANDIDATE_SOURCE_ROOT`, `CANDIDATE_OUTPUT_ROOT`: Fixed candidate repository and private Build export roots.
* `def prepare_build_validation(*, boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any]) -> dict[str, Any]`: Protected-root-only fixed Build read handoff. Recheck host/passwd/account/layout identities, terminate the candidate, verify the copy, grant only fixed-validator-group reads and recheck inode/envelope/host. Accepted-copy failures restore private traversal; native second-UID verification and receipt sealing remain required.
* `def freeze_build_export(*, boundary: HostBoundary, candidate: WorkerAccount, execution: WorkerResult, inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...], plan: dict[str, Any]) -> dict[str, Any]`: Protected-root-only candidate Build freeze. Require retained successful execution and genuine tested-tree inventory/protected generated roots, terminate candidate, authenticate layout and tracked-source bytes before/after independent export copy, then transfer only the protected copy to runner-private ownership and recheck inode/content/metadata/host. Native witnesses, source/overlay/cache/Git provenance and second-account validation remain required.

## `mod_base.build_ci.worker`

The disposable accounts of one job: the fixed boundary, the two fixed accounts, their closed
environment and one fenced execution that always ends with the account terminated and locked.
`mod_base.build_ci.lifecycle` composes them into the `ci worker-*` commands. A locked account
still runs what the runner starts for it through `sudo`, so locking is the resting state between
two hooks. Bounded execution and environment construction are not a Linux isolation proof by
themselves. Linux uses lazy passwd imports so the kit remains importable on other hosts, where
account operations explicitly reject.

* `WORKER_ROOT`, `WORKER_ACCOUNTS`: fixed private boundary root and candidate/validator account names.
* `class WorkerError(MbError)`
* `class WorkerAccount`
  * fields: `role: str, uid: int, gid: int, home: str`
* `class WorkerResult`
  * fields: `returncode: int | None, log: bytes, truncated: bool`
* `class WorkerExecutionError(WorkerError)`
  * `__init__(self, message: str, result: WorkerResult) -> None`
* `def execution_subject(identity: Any) -> dict[str, Any]`: The subject a hook runs for, from what its caller holds: a subject as `ci subject` authenticated it (`derive_plan`, before any plan exists) or the complete identity of a plan, which is validated in full and reduced to its subject part. Anything else is rejected.
* `def worker_environment(*, role: str, python: str, java_home: str | None, identity: dict[str, Any], run_id: int, run_attempt: int, values: Mapping[str, str]) -> list[str]`: The closed `env -i` vector of one hook; `identity` is a subject or a complete plan identity (`execution_subject`).
* `def authenticate_worker_account(role: str) -> WorkerAccount`
* `def worker_account_exists(role: str) -> bool`: Whether the fixed account of `role` has a passwd entry, authentic or not.
* `def authenticate_peer_account(account: WorkerAccount, *, runner_uid: int, runner_gid: int) -> WorkerAccount | None`: The other fixed account, authenticated, or `None` in a job that allocated one account (only root adds or removes a passwd entry). Every account that exists must differ in user and group from the other one and from the runner.
* `def lock_worker_account(role: str) -> None`: Lock and expire the fixed account by name without signalling any process: for an entry that cannot be authenticated, whose UID must never be the target of a kill.
* `def worker_processes(account: WorkerAccount) -> bool`: Whether the account still owns a process that can run, by real or effective user. Observation only; a zombie does not count.
* `def allocate_worker_account(role: str) -> WorkerAccount`: Allocate one fresh fixed identity in an already prepared runner-owned traversal boundary; reject account/home reuse and admitted sudo policy, verify private directory ownership, and forbid execution on failure.
* `def prepare_worker_boundary(*, runner_environment: str) -> None`: Exclusively create the fixed runner-owned traversal root on a protected caller-admitted GitHub-hosted Linux runner; reject preexisting identities/paths. This does not restrict the rest of the host.
* `def terminate_worker(account: WorkerAccount) -> None`: Double real/effective UID sweeps, mandatory lock/expiry, then post-lock sweeps and quiescence checks under the original sweep deadline. Any failed phase forbids success.
* `def execute_worker(account: WorkerAccount, *, command: tuple[str, ...], python: str, java_home: str | None, identity: dict[str, Any], run_id: int, run_attempt: int, values: Mapping[str, str], timeout_seconds: int) -> WorkerResult`: Run one dispatcher as the account and always terminate and lock its UID. The dispatcher starts with a umask of 077 (set inside the account, because `sudo`'s session applies the login umask). Only a zero exit of a dispatcher that left no running process behind returns; a failure, a timeout, a signal or a leftover process raises `WorkerExecutionError` with the bounded log.
* `def render_worker_log(result: WorkerResult, *, role: str) -> str`

## `mod_base.build_ci.source`

Owner: MB11. Inactive protected source inventory, post-quiescence inspection and atomic private
tracked-source copy. The caller must authenticate listing provenance and generated roots. This
does not allocate or freeze a worker, stage Git metadata/overlays/caches or assign candidate ownership.

* `class SourceError(MbError)`: Source inventory or immutable copy failed closed.
* `class GitSourceEntry`: A frozen tracked blob identity.
  * fields: `path: str, mode: str, size: int, git_blob: str`
* `def parse_source_inventory(data: bytes) -> tuple[GitSourceEntry, ...]`: Parse bounded full-tree NUL-delimited Git blob records; reject submodules, aliases, malformed paths and oversized trees.
* `def validate_source_inventory(inventory: tuple[GitSourceEntry, ...]) -> None`: Validate complete typed source inventory ordering, paths/closure/aliases, modes/blob identities and file/tree/entry bounds. Shape validation does not authenticate original tree/source provenance.
* `def read_source_tree_inventory(api: GitHubApi, *, tree_sha: str) -> tuple[GitSourceEntry, ...]`: Read complete metadata at exactly this tree SHA with bounded paths, modes, sizes and directory closure. Validate the complete prefix cap before expanding inferred directories. Caller must independently bind source/tree provenance, live identity, policy and blob bytes.
* `def authenticate_source_inventory(api: GitHubApi, *, identity: dict[str, Any]) -> tuple[GitSourceEntry, ...]`: Read the immutable tested tree through bounded GitHub transport, require exact blob/directory closure and sizes, and authenticate the live PR before and after listing. Existing transport caps remain additional limits; no candidate code executes.
* `def verify_source_copy(root: Path, *, inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...] = ()) -> list[dict[str, str | int]]`: Compare all tracked bytes/modes with the protected inventory, retaining SHA-256 and refusing undeclared paths.
* `def materialize_source_copy(root: Path, output: Path, *, inventory: tuple[GitSourceEntry, ...]) -> list[dict[str, str | int]]`: Atomically publish a new private copy of clean tracked source after original, copied and independently inspected staged bytes agree; existing output is never replaced.

## `mod_base.build_ci.host`

Owner: MB11. Host filesystem fences for the initial protected GitHub-hosted Linux
profile, used by `ci worker-prepare` and undone by `ci worker-finish`. The runner hides its fixed home and authenticates workspace/temp containment without
following directory links. Root then closes the image itself before any worker account exists
(D5 of the Build/E2E architecture): a hosted `ubuntu-24.04` image ships `/opt` with the tool
cache, `/usr/share`, `/usr/local` and the JDKs writable by everyone. Neither fence establishes
kernel assumptions or stages authenticated copies.

* `HOST_RUNNER_HOME = '/home/runner'`
* `HOST_FENCE_TREES = ('/opt', '/usr/share', '/usr/local', '/usr/lib/jvm', '/var/lib/gems')`
* `class HostBoundary`: A frozen private runner-home identity, not execution or status authority.
  * fields: `home: str, uid: int, gid: int, device: int, inode: int, original_mode: int`
* `def inspect_worker_host(*, runner_environment: str, runner_home: str, workspace: str, runner_temp: str) -> HostBoundary`: The receipt `protect_worker_host` will return, without changing the home: the same admission of the hosted layout, with the home's current mode as `original_mode`. `ci worker-prepare` records it before it changes anything.
* `def protect_worker_host(*, runner_environment: str, runner_home: str, workspace: str, runner_temp: str) -> HostBoundary`: Authenticate the initial runner-owned hosted layout and close home traversal to 0700. The home's group, the runner's passwd group and the process group must be one (every later receipt check compares them). Failures after chmod keep it private.
* `def restore_worker_host(boundary: HostBoundary) -> None`: Runner-only, the last act of a job, once no worker account can run: give the recorded home (same device, inode, owner and group; mode 0700 or already the original) its original mode back. A second call changes nothing.
* `def authenticate_host_boundary(boundary: HostBoundary) -> None`: Recheck the exact private runner-home inode before admitting either disposable UID.
* `def privileged_runner_identity() -> tuple[int, int]`: Root-only. The `(uid, gid)` of the passwd account that owns the fixed runner home, derived from the filesystem and passwd and never from a request. It names an account; it is not a host fence receipt.
* `def authenticate_privileged_host_boundary(boundary: HostBoundary) -> None`: Recheck a bounded nonprivileged runner receipt against actual passwd/home identity from protected Linux root setup; never admits a worker or selects arbitrary owner identities.
* `def fence_worker_host(*, boundary: HostBoundary) -> None`: Root-only, and only while neither fixed worker account exists. Authenticate the fenced home; in one `find -xdev` walk over the `HOST_FENCE_TREES` this host has, remove group/other write permission from every directory and regular file that carries it (`chmod go-w`) and the default ACL of every directory (`setfacl -k`), never following or changing a link; then, in a second walk over the root filesystem, list every world-writable directory without the sticky bit and every world-writable regular file, without entering the worker boundary or a directory that only its owner can search when that owner is an existing account. Reject when anything is listed (with the count and the first path), when the listing does not fit `MAX_CI_HOST_FENCE_REPORT_BYTES`, when a tree is not a real directory, or when a command fails (with the start of what it wrote to stderr) or outlives `CI_HOST_FENCE_TIMEOUT_SECONDS`. Sticky directories outside the trees stay as they are; other filesystems, special files and group-writable entries outside the trees are not examined. A second run changes nothing.
* `def execute_isolated_worker(account: WorkerAccount, *, boundary: HostBoundary, command: tuple[str, ...], python: str, java_home: str | None, identity: dict[str, Any], run_id: int, run_attempt: int, values: Mapping[str, str], timeout_seconds: int) -> WorkerResult`: Recheck the host fence before dispatching the bounded worker; failed admission terminates/locks the UID without launching.

## `mod_base.build_ci.toolchain`

Owner: MB11. Inactive bounded permission/identity closure of protected-selected host tools.
A root may live anywhere: what admits it is that every entry, ancestor and link target passes
the ownership and mode rules after the host fence, not its location. This does not establish
installer provenance, byte integrity, complete import-root enrollment or compiler semantics.
Protected setup and actual hosted Linux evidence remain required.

* `class ToolTreeProof`: Immutable metadata receipt; not a content digest or build authority.
  * fields: `roots: tuple[str, ...], metadata_sha256: str, files: int, entries: int, total_bytes: int`
* `def inspect_worker_toolchains(*, boundary: HostBoundary, roots: tuple[str, ...]) -> ToolTreeProof`: Inspect every selected root, link target and ancestor under global limits, rejecting foreign owners, group/other-writable directories/files, special entries and any directory that carries a default ACL. No installation or link prefix is special.
* `def authenticate_toolchains(proof: ToolTreeProof, *, boundary: HostBoundary) -> None`: Reinspect the full closure and reject metadata/permission drift.
* `def execute_tool_fenced_worker(account: WorkerAccount, *, boundary: HostBoundary, tools: ToolTreeProof, command: tuple[str, ...], python: str, java_home: str | None, identity: dict[str, Any], run_id: int, run_attempt: int, values: Mapping[str, str], timeout_seconds: int) -> WorkerResult`: Bind Python/JDK paths and their resolved destinations to explicitly admitted roots, require a nonempty worker-executable regular Python file and worker-traversable JAVA_HOME directory, recheck tool and host fences, and terminate/lock without dispatch on failed admission.

## `mod_base.build_ci.root_request_schema`

Owner: MB11. The closed `mod-base.ci.root-request` v1 document: one private request of one root
operation. Its `operation` is a member of `grammar.CI_ROOT_OPERATIONS` and its `arguments` are a
closed object per operation; no field holds a program, a hook or a destination path. Only
`stage-candidate` names directories, and only ones root reads below the fenced runner home.
Validation proves shape and internal consistency only.

* `def validate_root_request(document: Any, *, path: str = "$") -> dict[str, Any]`: Require the kind, version, a closed operation, a 64-hex nonce, the runner's host boundary and exactly that operation's arguments. An account named in the arguments must differ from the runner in uid and gid. `host-fence` carries no arguments; `stage-candidate` carries the candidate, `repository`, `tested_sha`, `tested_tree`, the complete tested-tree `inventory` (`path`, `mode`, `size`, `git_blob` rows, a valid source inventory in ascending path order), the `source` checkout directory, the `overlay` (`path`, pin `sha`, `version`, `tree_digest`) and a `gradle_seed` directory or null, every directory a canonical absolute path strictly below `/home/runner`; `freeze-build-validation` carries the validator, controller source metadata, plan, Build envelope, producing run/attempt and the execution nonce (distinct from the request nonce); `freeze-runtime-validation` carries the same with the complete owning Build, the lane's runtime envelope and the lane id. The lifecycle operations name the accounts of the job, `validator` and `candidate` (`null` in a job that allocated the validator alone; otherwise different from the validator in uid and gid): `grant-controller` adds the subject and the controller source metadata (same controller commit), `grant-plan-inputs` the staged candidate files as `inputs` (name and SHA-256 each: the inventory, the scenario contract, then the extra plan inputs by name), `take-derived-plan` nothing, `grant-validation-inputs` the plan. Source, plan and envelope caps of the existing kinds apply unchanged.

## `mod_base.build_ci.root_request`

Owner: MB11. The private runner-to-root channel (D3 of the Build/E2E architecture). The runner
publishes one canonical request per operation in its own directory below the worker root (0700
directory, 0600 single-link file, never replaced) and starts the kit's root bootstrap for it. Root
derives the runner from the host, re-reads the request through the private record reader and
admits the live host fence before any operation runs. A request is data: it selects no code and
proves no provenance. Root never calls the GitHub API.

* `ROOT_PROGRAM = 'tools/ci_privileged_bootstrap.py'`
* `def root_request_path(operation: str) -> PurePosixPath`: The fixed `WORKER_ROOT / 'root-request-<operation>'` directory of one closed operation; any other name is rejected.
* `def request_host_fence(*, boundary: HostBoundary) -> str`: Runner-only. Publish the `host-fence` request, which carries nothing but the receipt of the fenced home, after the runner closed its home and before any worker account exists. Returns the request nonce.
* `def request_candidate_staging(*, boundary: HostBoundary, candidate: WorkerAccount, repository: str, tested_sha: str, tested_tree: str, inventory: tuple[GitSourceEntry, ...], source: Path, overlay: Path, pin: Pin, expected_digest: str, gradle_seed: Path | None = None) -> str`: Runner-only. Publish the `stage-candidate` request, once, after both worker accounts were allocated and before the candidate ever runs. The caller has authenticated the tested commit and tree, holds the tree's complete inventory and has established that `pin` is a released kit commit; root repeats none of that. `source` is the runner's checkout of the tested commit, detached at it; `overlay` a staged kit of `pin` with its stamp and digest `expected_digest`; `gradle_seed` an optional restored cache holding only `caches/` and `wrapper/`. All three lie below the fenced runner home. Returns the request nonce for `run_root_operation("stage-candidate", ...)`.
* `def request_controller_grant(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, subject: dict[str, Any]) -> str`: Runner-only. Publish the `grant-controller` request while `controller/` is still the runner's private copy of `sources`: the live accounts, the subject and the metadata of every source; the copy is verified before and inside publication. Returns the request nonce.
* `def request_plan_inputs_grant(*, boundary: HostBoundary, validator: WorkerAccount, digests: Mapping[str, str]) -> str`: Runner-only. Publish the `grant-plan-inputs` request for the privately staged candidate files of `validation-input/` (`digests`: staged name -> SHA-256; listed as the inventory, the scenario contract, then the extra plan inputs by name), verified before and inside publication. Returns the request nonce.
* `def request_validation_inputs_grant(*, boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any]) -> str`: Runner-only. Publish the `grant-validation-inputs` request for the privately staged complete `validation-input/` (the plan and the files its identity binds), verified before and inside publication. Returns the request nonce.
* `def request_derived_plan(*, boundary: HostBoundary, validator: WorkerAccount) -> str`: Runner-only. Publish the `take-derived-plan` request, which names only the live accounts: the output's place, name and bound are fixed in root. Returns the request nonce.
* `def request_build_validation_freeze(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, plan: dict[str, Any], envelope: dict[str, Any], run_id: int, run_attempt: int, execution_nonce: str) -> str`: Runner-only. Publish the `freeze-build-validation` request for the retained source receipt, plan, frozen Build and the nonce of the published execution record; the controller copy and both read-only inputs are inspected before and inside publication. Returns the request nonce.
* `def request_runtime_validation_freeze(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int, execution_nonce: str) -> str`: Runner-only. Publish the `freeze-runtime-validation` request for one lane, keeping the complete owning Build's own cross-run identity; the caller's three documents are canonicalised once and compared again inside publication together with all three input roots. Returns the request nonce.
* `def run_root_operation(operation: str, *, python: str, kit_root: Path, kit_digest: str, nonce: str) -> None`: Runner-only. Run `/usr/bin/sudo -n -- <python> -I -B -S <kit_root>/tools/ci_privileged_bootstrap.py --operation <operation> --kit <kit_root> --kit-digest <kit_digest> --nonce <nonce>` with a fixed environment, the worker root as working directory and no inherited descriptors, bounded by `CI_ROOT_OPERATION_TIMEOUT_SECONDS`. `kit_root` and `kit_digest` are the checkout and digest the job prologue verified. A non-zero exit, a timeout or a signal raises with the child's first bounded stderr line.
* `def read_root_request(operation: str, *, nonce: str) -> tuple[HostBoundary, dict[str, Any], bytes]`: Root-only. Derive the runner from the fixed home, read the private canonical request of exactly this operation and nonce, require its boundary to name that runner and to be the live fenced home, and check the worker root layout. Returns the boundary, the closed arguments and the record bytes.

## `mod_base.build_ci.root_request_operations`

Owner: MB11. The closed set of root operations behind the request channel; the only kit entry of
`tools/ci_privileged_bootstrap.py`. Operations rebuild their inputs from the request's closed data
and from protected copies on disk; none calls the GitHub API.

* `def execute_root_operation(operation: str, *, kit_root: str, kit_digest: str, nonce: str) -> None`: Require real Linux root; require the importing package to be `kit_root`'s, its kit-digest-v1 to equal `kit_digest` and its `template/`, `tools/` and `actions/` to match the staged-file locks; admit the request and the live host fence; run the one fixed operation; re-read the request unchanged. `host-fence` runs `host.fence_worker_host` for the request's boundary. `stage-candidate` requires the live candidate (and the validator beside it) to be the accounts the request names and runs `worker_preparation.prepare_privileged_worker_checkout` on the request's directories, identities, inventory and pin, leaving the candidate's `repository/` and Gradle home ready. `freeze-build-validation` and `freeze-runtime-validation` require the plan to name this kit's version and digest, rebuild the source receipt from the validator's protected controller copy, authenticate the read-only inputs before and after, seal the verifier receipt bound to the published execution record and always terminate the validator. The lifecycle operations require the live accounts to be exactly the ones the request names (a candidate the request does not name must not exist): `grant-controller` requires the subject to name this kit, rebuilds the source receipt from the still private `controller/` and runs `controller.prepare_controller_validation`; `grant-plan-inputs` runs `inputs.prepare_plan_inputs`; `take-derived-plan` runs `inputs.take_derived_plan`; `grant-validation-inputs` requires the plan to name this kit and runs `inputs.prepare_validation_plan`.

## `mod_base.build_ci.selection`

Owner: MB11. Newest Build selection before success, and its use by a packaged run. A producer
run is found by the managed Build caller's workflow file and, for a
pull request, the `pull_request_target` event and the pull request's head commit, head branch and
source repository; for a protected subject, a `push` or `workflow_dispatch` event on the default
branch at the tested commit. There is no status filter and no run-title contract. The newest run by
`(created_at, id)` and its latest attempt are chosen before any result is read. No run or an
unfinished one means nothing yet for a pull request, whose wait continues; a protected subject
without a run builds for itself, and an unfinished run is a rejection for it. A completed run with
the exact deferral graph (pull request) or reuse graph (protected subject) is not a producer; a
successful run with the exact full graph is described; anything else is a rejection, never a fall
back to an older run. A standalone packaged run that built for itself reads that Build while the
run is still in progress.

* `SELECTION_NAME = 'ci-selection.json'`: the name of the selection record in the state directory of a job that selects or consumes a Build.
* `SAME_RUN = 'same-run'`: the `--build-run-id` of a standalone packaged run whose `rebuild` job built in that run.
* `def newest_run(api: GitHubApi, producer: str, *, head_sha: str, head_branch: str, head_repository: str, events: tuple[str, ...]) -> dict[str, Any] | None`: The newest run of the managed caller `producer` that GitHub lists under one head, whatever its result: `{id, run_attempt, created_at, status, conclusion, event}` or None. `events` are the events that start such a run; a single one is also the listing's filter. No status filter.
* `def pending_run(run: dict[str, Any]) -> None`: Require the state of a run that has not completed to be a real pending one (a known status and no conclusion).
* `def producer_record(plan: dict[str, Any], *, caller: str, run_id: int, run_attempt: int, event: str, graph_sha256: str) -> dict[str, Any]`: The producer identity a descriptor carries for one run attempt of a managed caller for the plan's subject, without an upload window.
* `def describe_artifact(api: GitHubApi, *, plan: dict[str, Any], producer: dict[str, Any], jobs: list[dict[str, Any]], kind: str, unit_id: str | None = None) -> dict[str, Any]`: Describe the one `kind` artifact of a run attempt from API data alone: the upload window of the job that uploads it (sealed first) and the only artifact of its exact name in the run. Returns a structurally valid descriptor; its consumer authenticates the run and the artifact's metadata, owner and availability.
* `def select_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any]) -> dict[str, Any] | None`: Admit the live pull request, then describe the complete bundle of its newest Build run, or return None while it is absent, pending or deferred. One observation: a consumer repeats it before its effect.
* `def select_latest_merged_pr_build(api: GitHubApi, *, plan: dict[str, Any], controller_sha: str, merged_sha: str) -> dict[str, Any] | None`: The same after historical admission of the merged pull request; the original runs stay recorded under the pull request's head. None is absence, not reuse approval.
* `def revalidate_latest_merged_pr_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any], controller_sha: str, merged_sha: str) -> None`: Repeat the historical newest-run observation and require the same descriptor.
* `def wait_for_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]`: Wait at most the monotonic 5400-second deadline and 91 observations; `wait_seconds` (1 to 5400) may shorten the deadline, never extend it. The pull request is admitted when the wait starts and again when a bundle is returned; in between a poll reads only the run listing. Late API results never admit; API, corruption and failed-producer errors propagate.
* `def revalidate_latest_pr_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any]) -> None`: Repeat the newest-run observation around consumption and require the same descriptor.
* `def download_latest_pr_build(api: GitHubApi, *, plan: dict[str, Any], output: Path, wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]`: Wait/select, download by immutable numeric ID, verify the canonical envelope and bytes, and observe the pull request, the newest run, its latest attempt and the bundle's availability again inside the private atomic copy before publication. Returns the descriptor and envelope.
* `def select_protected_build(api: GitHubApi, *, plan: dict[str, Any]) -> dict[str, Any] | None`: Admit the live protected subject, then describe the complete bundle of the newest Build run of its commit. None when no run exists or the newest one is an admitted reuse (the caller builds for itself); a pending, failed or cancelled newest run is a rejection.
* `def revalidate_protected_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any]) -> None`: Repeat that observation around consumption and require the same descriptor.
* `def download_protected_build(api: GitHubApi, *, plan: dict[str, Any], output: Path, run_id: int | None = None) -> dict[str, Any] | None`: Select the newest Build run of a protected subject (which must be `run_id` when one is named; None only when nothing is selected and no run was named), download its bundle by numeric ID and observe the subject, the newest run, its latest attempt and the bundle's availability again before publication. Returns the descriptor and envelope.
* `def download_rebuilt_build(api: GitHubApi, *, plan: dict[str, Any], run_id: int, run_attempt: int, event: str, output: Path, descriptor: dict[str, Any] | None = None) -> dict[str, Any]`: For a later job of the standalone packaged run that built for itself: authenticate the run's latest attempt (still in progress), its controller and kit pin and the finished guard, selection and Build jobs with their seals, describe the bundle the assembling job uploaded (which must be `descriptor` when one is given), download it and observe the mutable part again before publication. Returns the descriptor and envelope.
* `def select_build(api: GitHubApi, *, plan: dict[str, Any], run_id: int, run_attempt: int, workflow_path: str, event: str, temporary_root: Path, build_run_id: int | str | None = None, wait_seconds: int = limits.CI_BUILD_WAIT_SECONDS, monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any] | None`: The selection record (`records.build_source_selection`) of the exact Build that run attempt of the packaged caller consumes, or None when a protected subject has no Build to select. A pull request names no run and waits (`download_latest_pr_build`); a protected subject takes the newest Build run of its commit, which must be `build_run_id` when that is a run id (`download_protected_build`), or with `SAME_RUN` the Build this run built (`download_rebuilt_build`). The bundle is verified in a private directory below `temporary_root` and removed; the request names the run attempt, `workflow_path` on the default branch and a fresh 256-bit nonce.
* `def fetch_build(api: GitHubApi, *, record: dict[str, Any], plan: dict[str, Any], run_id: int, run_attempt: int, workflow_path: str, event: str, output: Path) -> dict[str, Any]`: Bind a selection record to this run attempt (`records.bind_source_selection`), repeat the newest-run observation and require the selected descriptor (`revalidate_latest_pr_build`, `revalidate_protected_build`), then publish the complete bundle at `output` (`transport.download_completed_build`; a rebuilt Build through `download_rebuilt_build`). The envelope must hash to the record's `envelope_sha256`. Returns the envelope.

## `mod_base.build_ci.transport`

Owner: MB11. Numeric-ID transport for Build/runtime exports and tested gates. A producer run is
authenticated as GitHub records it (a pull request's run under its head commit and branch, a
protected push or dispatch under the commit it runs from); its controller commit and kit pin come
from `referenced_workflows`. The producers are the managed callers of `workflow.CI_CALLER_WORKFLOWS`,
so no route takes a workflow path. Each route reads commits and completed job lists once and the
source, each run's latest attempt and each artifact's availability at its start and again
immediately before it publishes or returns. Plans and descriptors are copied on entry.

* `def download_completed_build(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any], output: Path) -> dict[str, Any]`: Authenticate the live subject, the exact completed latest attempt of a full Build-caller run, its controller and kit pin, exact graph and the assembling job's upload window; bind artifact metadata, expiry, owner and head and the ZIP digest; verify the canonical envelope and inventory and atomically publish a private copy. Newest-run selection, native validity and final authorization remain required.
* `def download_target_set(api: GitHubApi, *, descriptors: list[dict[str, Any]], plan: dict[str, Any], run_id: int, run_attempt: int, output: Path, source_config_sha256: str | None = None) -> list[dict[str, Any]]`: With `source_config_sha256` (the digest of the protected Build config the reader loaded) every artifact is what a target job uploads, the partition with its `verify_target` validation record beside the envelope, which is verified against the partition and kept out of the published inputs; without it every artifact is the bare partition. For the assembling job of a still-running full Build run (or of a standalone packaged run that rebuilds): require the exact ordered complete same-attempt target descriptors and the extra compressed-set budget, finished plan, policy and target jobs with their sealed upload windows, download each checked ZIP into fixed target-ordinal children of one private atomic stage, verify the whole logical export and publish all or nothing. Returns descriptor/envelope pairs.
* `def download_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any], gate: str, temporary_root: Path) -> dict[str, Any]`: Read one tested record of the live subject by numeric ID: the mode of its run is settled from the descriptor's graph digest, the run, exact graph, gate seal and upload and artifact metadata are authenticated, only the fixed canonical root JSON record is extracted, the execution timeline and every source artifact's metadata and availability are bound, and a Build consumed from a separate run is authenticated as its own completed full run.
* `def download_merged_gate_receipt(api: GitHubApi, *, descriptor: dict[str, Any], plan: dict[str, Any], gate: str, controller_sha: str, merged_sha: str, temporary_root: Path) -> dict[str, Any]`: The same for one original PR seal after merge, under historical admission of the merged pull request; original producer identities are preserved. No reuse or settlement effect is approved.
* `def download_merged_gate_pair(api: GitHubApi, *, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any], plan: dict[str, Any], controller_sha: str, merged_sha: str, temporary_root: Path) -> tuple[dict[str, Any], dict[str, Any]]`: Read both original tested seals once and require the packaged gate's owning Build to be the Build gate's bundle. Never a partial result or reuse authority.
* `def download_merged_build(api: GitHubApi, *, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any], plan: dict[str, Any], controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]`: Privately materialize the exact original complete Build bundle of a coherent historical seal pair, observing the pair's mutable state again inside the atomic publication.
* `def download_merged_runtime(api: GitHubApi, *, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any], plan: dict[str, Any], controller_sha: str, merged_sha: str, output: Path) -> dict[str, Any]`: The same for the pair's complete results aggregate, bound to the original owning Build. The extracted source is verified against the bound envelope once more inside the atomic copy, so a source replaced after binding is never published.
* `def download_merged_inputs(api: GitHubApi, *, build_descriptor: dict[str, Any], packaged_descriptor: dict[str, Any], plan: dict[str, Any], controller_sha: str, merged_sha: str, output: Path) -> tuple[dict[str, Any], dict[str, Any]]`: Publish both under fixed private build/runtime children after one final admission. Each child must carry the very envelope that was bound when its archive was extracted; either failure publishes neither.

## `mod_base.build_ci.describe`

Owner: MB11. Descriptors of the artifacts a running attempt has uploaded so far, for a fan-in or
gate job of that same attempt. The attempt's job listing and the run's artifact listing are each
read once: every job that must have finished shows the conclusion its graph expects
(`graph.require_partial_graph`), every expected artifact is listed exactly once, unexpired,
within the size cap of its kind and under this run and the subject's head, and its window is the
upload step of the job that sealed it. A job or an artifact of an earlier attempt is refused as a
failed-jobs-only rerun. The run, the source and the bytes are authenticated by the caller.

* `def attempt_producer(record: dict[str, Any], plan: dict[str, Any], *, mode: str, run_id: int, run_attempt: int) -> dict[str, Any]`: The producer identity (no upload window) that every record and descriptor of this attempt carries: the caller and the event of the job's identity record (`identity.read_subject`), the head GitHub records the run under and the graph digest of `mode` for `plan`. The plan must be of the record's subject.
* `def settled_jobs(producer: str, mode: str, plan: dict[str, Any], kind: str, unit_id: str | None = None) -> list[str]`: The jobs of a run of `producer` in `mode` that have finished when the job that uploads the `kind` artifact seals (`build`: the assembling job; `results`: the aggregating job; `tested` with its gate as `unit_id`): every job that succeeds in its own call or an earlier one, except itself and the gate that follows it, and every job the mode skips.
* `def settled_artifacts(producer: str, mode: str, plan: dict[str, Any], kind: str, unit_id: str | None = None) -> list[tuple[str, str | None]]`: `(kind, unit_id)` of the artifact every sealing job among `settled_jobs` has uploaded, in the order the run produces them.
* `def attempt_jobs(api: GitHubApi | CommandReads, run_id: int, run_attempt: int) -> list[dict[str, Any]]`: Every job of one running attempt, read once (`github.jobs.attempt_jobs`); a job of an earlier attempt in the listing is refused as what a failed-jobs-only rerun leaves behind.
* `def describe_attempt(api: GitHubApi | CommandReads, *, producer: dict[str, Any], plan: dict[str, Any], mode: str, expected: Sequence[tuple[str, str | None]], finished: Sequence[str] = ()) -> list[dict[str, Any]]`: Canonical descriptors (`records.validate_descriptor`) of the distinct `expected` `(kind, unit_id)` artifacts of this attempt, in the order given, from two requests. `producer` is `attempt_producer` for `mode`; `finished` names further jobs that must have finished beside the uploading ones; with nothing expected only the jobs are read and required. A missing, expired, repeated, oversized or foreign artifact, an artifact of an expected kind the plan does not expect, a job that has not finished as the graph expects or did not seal before its upload, and a job or artifact of an earlier attempt are distinct rejections. One observation: a caller that produces an effect describes again before it.

## `mod_base.build_ci.gate`

Owner: MB11. What the sealing jobs of a run prove about their own running attempt before they
seal a record: the gate its tested record, the aggregating job of a packaged run the results
index. The job's identity record fixes the modes that can reach the gate; a pull request has one,
and a protected run shows which of its modes it is by the job names of its own attempt, never by
an input of the gate job. In that mode the gate requires the live source, the run as its latest
attempt in progress (bound to the controller commit and kit pin), every job the graph finishes
before the gate with its expected conclusion (success or skipped) and seal-before-upload, and
every artifact those jobs uploaded. The completed graph and the chronology are the reader's proof
(`graph.authenticate_gate_timeline`).

* `class Attempt`: A running attempt authenticated for the sealing step of its gate job. `reads` and
  `watch` are the command's reads so far (whoever seals rechecks the watch before its effect),
  `producer` is the attempt's identity without a window and `descriptors` are the artifacts of
  `describe.settled_artifacts`, in that order.
  * fields: `reads: CommandReads, watch: Watch, plan: dict[str, Any], producer: dict[str, Any], gate: str, mode: str, descriptors: list[dict[str, Any]]`
  * `descriptor(self, kind: str, unit_id: str | None = None) -> dict[str, Any]`
* `def admissible_modes(record: dict[str, Any], gate: str) -> tuple[str, ...]`: The modes in which a run of the record's caller reaches `gate` (which must be the record's producer) for the record's subject: one for a pull request; for a protected subject never the pull-request mode, and `reuse` too when the event is a push and the gate is the caller's own.
* `def authenticate_attempt(api: GitHubApi | CommandReads, *, record: dict[str, Any], plan: dict[str, Any], gate: str, run_id: int, run_attempt: int) -> Attempt`: Admit the source, settle the mode, authenticate the run and describe the settled jobs' artifacts under one watch. Nothing is sealed.
* `def seal_gate(attempt: Attempt, *, config_sha256: str, temporary_root: Path) -> tuple[str, dict[str, Any]]`: `(grammar.CI_GATE_NAME, receipt)` of an attempt in a mode other than `reuse`. A Build gate downloads the complete Build of the attempt, verifies the export against the plan and its `verify_build` validation record (frozen under `config_sha256` over the canonical envelope) and names the bundle and every target's report. A packaged gate downloads the results index, requires exactly the lane artifacts this attempt uploaded and authenticates the owning Build (the rebuilt one, or a completed full run of the Build caller). The watch is rechecked before return.
* `def seal_reuse(attempt: Attempt, *, temporary_root: Path) -> tuple[str, dict[str, Any]]`: The seam of K6: the reuse reference of an attempt authenticated in `reuse` mode. Not written yet: raises `MbError` with reason `unsupported`.
* `def seal_results(api: GitHubApi | CommandReads, *, record: dict[str, Any], plan: dict[str, Any], selection: dict[str, Any], run_id: int, run_attempt: int, config_sha256: str, temporary_root: Path) -> tuple[str, dict[str, Any]]`: `(grammar.CI_RESULTS_NAME, index)` of the running packaged attempt whose aggregating job calls it. `selection` is the selection record of the job's state and must be this attempt's own request. The attempt is authenticated like a gate's (never in `reuse` mode) with exactly one artifact of this attempt for every planned lane and none for another; each lane is then downloaded by id, one at a time, and its export verified against the plan and bound to the selected Build, and its `verify_runtime` validation record verified against `config_sha256` and the digest of the plan, the selected Build's envelope hash and the lane's envelope. The Build bundle is not read. The watch is rechecked before return.

## `mod_base.build_ci.commands_build`

Owner: MB11. `ci assemble`, `ci aggregate` and `ci seal-gate`, listed in `commands.VERB_MODULES`:
the fan-in and gate steps of a Build or packaged run. All take the job arguments
(`commands.add_job_arguments`: `--repo`, `--config`, `--state DIR`), read `identity.json` and
`ci-plan.json` of the state and the run and attempt from `GITHUB_RUN_ID` and `GITHUB_RUN_ATTEMPT`,
and build their API client through `commands.api_client` with an explicit budget. The state must
belong to the executing repository and controller commit and the plan to the subject of the
state. Downloads go into a temporary directory inside the state and are removed again; a record
is written last, as the one file of a directory the command creates.

* `PARTITIONS_NAME = 'ci-partitions.json'`: the state record `ci assemble` writes last: `{"descriptors", "envelope_sha256"}`, the partition descriptors in plan order and the SHA-256 of the assembled envelope.
* `def add_verbs(verbs: argparse._SubParsersAction) -> None`: Register the three verbs on the `ci` verb group. `assemble` has no flag of its own; `aggregate --output DIR`; `seal-gate --gate build|packaged --output DIR`.
* `def run_assemble(args: argparse.Namespace) -> int`: The `assemble` handler, a step of a Build job in a full run of the Build caller or in a packaged run that rebuilds: describes the partition of every planned target of this attempt, downloads them with the validation record each target job uploaded (`transport.download_target_set` with the digest of the protected Build config) and assembles their exact union into `exports.BUILD_VALIDATION_ROOT`, which must not exist. Budget `MAX_CI_ASSEMBLE_REQUESTS`.
* `def run_aggregate(args: argparse.Namespace) -> int`: The `aggregate` handler, the sealing step of the aggregating job of a packaged run: reads the canonical selection record `grammar.CI_SELECTION_NAME` of the state and writes the index of `gate.seal_results` as `ci-results.json`, the one file of the new directory `--output`. Budget `MAX_CI_AGGREGATE_REQUESTS`.
* `def run_seal_gate(args: argparse.Namespace) -> int`: The `seal-gate` handler: `gate.authenticate_attempt`, then `gate.seal_gate` (or `gate.seal_reuse` in a reuse run) with the digest of the protected Build config of the mod checkout, and the record written last as the one file of the new directory `--output`. Budget `MAX_CI_GATE_REQUESTS`.

## `mod_base.build_ci.handoff`

Owner: MB11. Fixed private runner-origin execution data channel; no native/status authority.

* `EXECUTION_HANDOFF_ROOT`: fixed runner-private execution record directory beneath WORKER_ROOT.
* `def validate_execution_handoff(document: Any, *, path: str = "$") -> dict[str, Any]`: Strict new local v1 kind with bounded canonical binary log, no program/path/hook fields. Structural validation proves no physical origin or actual execution.
* `def record_build_validation_execution(*, boundary: HostBoundary, sources: ControllerSources, bound: BuildValidationExecution, plan: dict[str, Any], envelope: dict[str, Any], run_id: int, run_attempt: int) -> str`: Runner-only exclusive private publication of genuinely retained successful execution bound to protected context; return a fresh random nonce. Caller owns genuine execution provenance and mandatory UID quiescence.
* `def freeze_handed_off_build_validation(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, plan: dict[str, Any], envelope: dict[str, Any], run_id: int, run_attempt: int, nonce: str) -> dict[str, Any]`: Root-only admit fixed runner-owned single-link private canonical record and exact nonce/attempt/source/plan/input, reconstruct data and delegate existing independent input-bound receipt freeze. Fixed root program/import/installer/source provenance and native semantics remain separate requirements.

## `mod_base.build_ci.archive`

Owner: MB11. Local stored ZIP encoding of sealed exports, not upload/native/status authority.

* `def encode_build_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]`: Verify canonical source export, stream sorted exact payload/envelope bytes through no-follow child reads into one exclusive private stored ZIP with fixed metadata and the existing 512 MiB compressed cap applied before every physical write including ZIP metadata. Independently extract/reverify the archive, recheck complete source and hash the bounded ZIP before atomic directory publication. Return local path/size/SHA-256; protect ancestry/quiescence separately. Never upload this file as an extra nested GitHub artifact or infer actual server ZIP size/digest.

## `mod_base.build_ci.gradle_cache`

* `def stage_privileged_gradle_cache(seed: Path, *, boundary: HostBoundary, account: WorkerAccount) -> list[dict[str, Any]]`: Before a fresh candidate UID starts, require actual Root/fenced home and exact `candidate` passwd admission with no processes; the destination is the fixed `candidate-home/gradle-home`. Admit a protected-policy, secret-free, writer-excluded restored seed under runner home, with only caches/wrapper root directories and original BP 250000-entry/200000-file/2 GiB-file/20 GiB-total caps. Require originally empty allocated Gradle home, fence its access to Root while making independent files, recheck source inventories/bytes and original directory bindings, normalize a newly populated independent private copy to worker-owned 0700/0600 and recheck. Any admitted failure locks/terminates the worker and returns no authority; partial private data can remain. Shape/hash checks certify neither arbitrary data's secret absence nor installer approval; restoration provenance, no earlier UID execution and production/Linux integration remain mandatory.

## `mod_base.build_ci.worker_overlay`

* `def stage_privileged_worker_overlay(overlay: Path, *, boundary: HostBoundary, account: WorkerAccount, pin: Pin, expected_digest: str) -> list[dict[str, Any]]`: Old protected Root implementation copies an independently checkout/bootstrap-bound stamped kit into the fixed candidate-only repository/out/mod-base-kit. Require Root/fence/fresh quiescent `candidate`, its private 0700 repository, private protected source, bounded closed kit roots, no bytecode/links/special/hard links/executable source and exact stamp/digest/locks. Root never calls the API: that the pin is a released kit commit is the runner-side caller's prerequisite. `out` is created when the tested tree tracks nothing in it and is the candidate's own 0700 directory either way, so a build can write beside the overlay. Exclusively publish independent files, retain empty files/required empty roots, remove ACLs and hand off to the candidate with plain 0644/0755 bootstrap modes, recheck original directory/pin/source/copy identity. Any admitted failure terminates/locks the candidate; late private output may remain. Candidate upgrade admission and original caller/runtime/source provenance are prerequisites; no copied code is imported or gains protected/native/App authority.


## `mod_base.build_ci.worker_source`

* `def stage_privileged_worker_source(root: Path, *, boundary: HostBoundary, account: WorkerAccount, inventory: tuple[GitSourceEntry, ...]) -> list[dict[str, Any]]`: Require actual Root/fenced home and fresh quiescent `candidate`; admit protected-home source bytes/Git modes against the tested-tree inventory, refusing an undeclared path, a hard link or an entry of another type or mode before anything is published. Bind original source and protected destination parent; exclusively publish independent tracked bytes at the fixed candidate repository, omitting Git metadata. Require new Root-private publication and transfer it to worker-private ownership/permissions while preserving executable Git modes and literal symlink bytes through no-follow operations. Recheck original source/copy/parent/name/inode/role; any admitted failure locks/terminates worker and returns no authority. Late inert output may remain. Original caller/runtime, tested-tree origin, candidate policy and no previous UID activity remain preconditions; no Git, copied-code or native execution and no validation/upload/App authority.


## `mod_base.build_ci.worker_git`

* `def stage_privileged_worker_git(root: Path, *, boundary: HostBoundary, account: WorkerAccount, repository: str, tested_commit: str) -> list[dict[str, Any]]`: Actual protected Root/fenced home and fresh quiescent candidate setup only. Curate independently authenticated original self-contained SHA-1 checkout metadata under private runner home; closed bounded no-follow ownership/shape checks select index, loose/packed object data, heads/tags/remotes/pull refs, packed refs and shallow boundary. Omit source config/HEAD/hooks/logs/other ancillary bytes; reject external object borrowing/grafts, replacement refs, unsupported stores and unsafe types/links/aliases. Validate bounded ref text and require the original HEAD to be detached at exactly `tested_commit` (a moved HEAD, or one on a branch, is refused); the independently supplied commit becomes detached HEAD and grammar-bound repository becomes fixed credential-free configuration with hooks/fsmonitor disabled. Exclusively publish independent files into the worker-private candidate repository .git; Root-private staging, ACL-free worker 0700/0600 handoff and repeated source/parent/copy/name/inode/role checks. Failure locks/terminates worker; late inert output can remain. Never invoke Git or execute copied data as Root. Original caller/runtime and source checkout/commit/tree/index, tracked candidate source, excluded writers and no earlier UID activity remain caller prerequisites; copying observed data hashes does not approve object graphs, native behavior, privileged Git or App/upload authority.


## `mod_base.build_ci.worker_preparation`

* `def prepare_privileged_worker_checkout(source: Path, gradle_seed: Path | None, overlay: Path, *, boundary: HostBoundary, account: WorkerAccount, repository: str, tested_commit: str, tested_tree: str, inventory: tuple[GitSourceEntry, ...], pin: Pin, expected_digest: str) -> dict[str, list[dict[str, Any]]]`: The `stage-candidate` root operation: actual protected Root/fenced-home/fresh-quiescent `candidate` setup only, with no API access. Require separate nonoverlapping original source/overlay/seed roots under private runner home, no existing candidate `repository` and an empty allocated Gradle home (a populated root is never reused); derive Git metadata from the original checkout's `.git`; retain all original no-follow roots plus the allocated private cache. Before the first effect: the inventory must hash to `tested_tree` (the Git tree name is recomputed from its rows), no tracked path may take `out`, the overlay slot or a case alias of either, the checkout must hold exactly the inventory (bytes, Git modes, literal links, no undeclared path or hard link), its HEAD must be detached at `tested_commit`, and the overlay must match its pin, stamp, digest and locks. Then compose exclusive source, Git, private Gradle cache (skipped without a seed, which is admitted when its phase starts) and candidate-only overlay copying in fixed order; retain each published inode/mode/candidate identity, including the candidate's own `out`. Finally recheck the complete tracked source with only the fixed overlay as generated data, Git/cache bytes and private metadata and the original seed/overlay/source bindings. Return only source/git/gradle/overlay copy observations, with no execution/validation/upload authority. Any failure terminates and locks the candidate; after a refusal in a later phase inert outputs may remain. The runner-side caller owns authentication of the tested commit and tree, the released pin, candidate upgrade, safe cache restoration, excluded writers and no earlier UID execution.

## `mod_base.build_ci.activation`

Owner: MB11. The Build/E2E activation manifest of a mod: its closed data, the caller workflows
each mode makes managed files and the allowed transitions. Pure data and tables; it reads no file
and grants nothing. `mod_base.template.tool` gates its rendered callers on these tables and
`mod_base.build_ci.transition` admits a change of state.

* `ACTIVATION_PATH = 'site/mod-base-build-activation.json'`
* `ACTIVATION_KIND = 'mod-base.ci.activation'`
* `ACTIVATION_MODES = ('disabled', 'shadow', 'shared-build', 'shared-build-and-e2e', 'reviewed-rollback')`
* `DISABLED_MODE = 'disabled'`
* `ROLLBACK_MODE = 'reviewed-rollback'`
* `ROLLBACK_SOURCES = ('shadow', 'shared-build', 'shared-build-and-e2e')`: the modes a `reviewed-rollback` can leave (`rollback_from`).
* `ABSENT_STATE = 'absent'`: the state of a mod without a manifest; never a `mode` value.
* `GUARD_CALLER = '.github/workflows/mod-base-guard.yml'`
* `BUILD_CALLER = '.github/workflows/mod-base-build.yml'`
* `PACKAGED_CALLER = '.github/workflows/mod-base-packaged-e2e.yml'`
* `STATUS_CALLER = '.github/workflows/mod-base-gate-status.yml'`
* `CALLERS`: the four caller paths above, in that order.
* `MANAGED_CALLERS`: mode -> the callers the kit manages in it: `disabled` none, `shadow` and `shared-build-and-e2e` all four, `shared-build` the guard, Build and status callers. `reviewed-rollback` has no row: it manages the row of the mode it leaves.
* `TRANSITIONS`: state -> the states it may change to: `absent` to `disabled`; `disabled` to `absent`, `shadow` or `shared-build`; `shadow` to `disabled`, `shared-build`, `shared-build-and-e2e` or `reviewed-rollback`; `shared-build` to `shared-build-and-e2e` or `reviewed-rollback`; `shared-build-and-e2e` to `reviewed-rollback`; `reviewed-rollback` to `disabled`.
* `def validate_activation(document: Any, *, path: str = '$') -> dict[str, Any]`: The closed mod-base.ci.activation v1 data: kind, schema_version, repository, profile, mode and rollback_from, which names the mode a reviewed-rollback leaves and is null in every other mode. No pin, template, job, permission, secret, approval, deferral or scenario key. Returns the same document; a valid one is data, not an admitted transition.
* `def parse_activation(data: bytes, *, label: str = ACTIVATION_PATH) -> dict[str, Any]`: Strictly decode (at most `MAX_CI_ACTIVATION_BYTES`) and validate manifest bytes.
* `def activation_state(document: dict[str, Any] | None) -> str`: The `TRANSITIONS` state of a validated manifest: its mode, or `ABSENT_STATE` for `None`.
* `def managed_mode(document: dict[str, Any] | None) -> str`: The `MANAGED_CALLERS` row a validated manifest selects: the mode a reviewed-rollback leaves, `disabled` for `None`, else its mode.
* `def managed_callers(document: dict[str, Any] | None) -> tuple[str, ...]`: The callers the kit manages for a mod with this validated manifest.
* `def managing_modes(caller: str) -> frozenset[str]`: The `managed_mode` values for which `caller` is a managed file.
* `def next_states(document: dict[str, Any] | None) -> tuple[str, ...]`: The states a mod with this validated manifest may change to in one transition.
* `def transition_refusal(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> str | None`: Why the change between two validated manifests (a mod without one is `None`) is not an allowed transition, or `None` when it is one or nothing changed. A transition changes the mode along `TRANSITIONS` and nothing else, and a reviewed-rollback names exactly the mode it leaves. Pins are not its concern (`transition.admit_transition`).

## `mod_base.build_ci.transition`

Owner: MB11. Admission of an activation change and of a candidate's Build/E2E caller bytes, from
bytes the protected side read itself. `template transition` runs both over two checkouts. Neither
is owner approval.

* `class Transition`: An admitted change: the previous and current `activation_state`, whether the manifest changed at all, and the callers the candidate's state manages.
  * fields: `previous: str, current: str, changed: bool, managed: tuple[str, ...]`
* `def admit_transition(protected: bytes | None, candidate: bytes | None, *, protected_pin: Pin, candidate_pin: Pin) -> Transition`: Admit the change from the protected manifest bytes to the candidate's (a side without a manifest is `None`) or raise `MbError`. Both are decoded strictly. Equal documents are admitted whatever the pins (no transition); any difference must be an allowed transition (`activation.transition_refusal`) and both pins must carry the same SHA and version.
* `def verify_candidate_callers(files: Mapping[str, bytes], *, candidate: bytes | None, pin: Pin, kit_root: Path) -> tuple[str, ...]`: Require the candidate's caller files (`{path: bytes}` of those that exist) to be exactly `template.tool.expected_callers` for its manifest and pin: a managed caller equals its rendered template, every other caller path is absent, and no other path is given. `kit_root` must be the verified kit `pin` names. Returns the managed paths; one `MbError` names every problem.

## `mod_base.build_ci.batch`

Owner: MB11. Batches of pull requests (K5): construction, verification by rebuilding and
settlement, reached through `ci batch-prepare` and `ci batch-settle`. A batch squashes open
same-repository pull requests, in a given order, onto the default branch head and lands them
through one `batch/<name>` pull request. Its commit ids depend only on the base commit and on
each member's number, title and head commit, so the stack can be built again and compared; the
marker in the pull request body is a hint that must equal that rebuild. The token is the
invocation's: a pull request opened with a workflow's default `GITHUB_TOKEN` starts no workflow
runs, so the caller supplies an App or automation token.

* `BATCH_BRANCH_PREFIX = 'batch/'`
* `BATCH_KIND = 'mod-base.ci.batch'`
* `class BatchMember`: One member pull request as the API reports it now.
  * fields: `number: int, title: str, head_branch: str, head_sha: str`
* `class BatchObservation`: The live state a batch is built on: the default branch head and the ordered members.
  * fields: `repository: str, base_branch: str, base_sha: str, base_tree: str, members: tuple[BatchMember, ...]`
* `def observe_batch(api: GitHubApi, *, pr_numbers: tuple[int, ...]) -> BatchObservation`: Check the 1 to 50 distinct positive numbers before any read, then read the default branch, its head and each pull request once (2 requests and 1 per member). A member is open, of this repository on both sides, based on the default branch, not itself a `batch/*` or the base branch, and has a printable title of at most 256 characters. The base commit of the pull request record and its draft flag are not bound.
* `def batch_marker(manifest: dict[str, Any]) -> str`: The one line `<!-- mod-base-batch {manifest} -->` that carries a manifest in a pull request body: its canonical JSON with every character outside printable ASCII and every `<`, `>` and `&` as a JSON escape, so no title can close the comment and the line has one spelling.
* `def read_batch_marker(body: object) -> dict[str, Any]`: The validated manifest of the single marker line of a body of at most 64 KiB. Exactly one marker, alone on its line, in its canonical spelling; anything else is refused. The result is a hint, never proof.
* `def prepare_batch(api: GitHubApi, store: BatchStore, *, name: str, pr_numbers: tuple[int, ...], allowed_paths: tuple[str, ...], dry_run: bool = False) -> dict[str, Any]`: Observe, require the branch `batch/<name>` to be absent, fetch (a ref that is not the commit the API reported has moved), build the stack under the caller's allowed-path list and derive manifest and body. A dry run returns here and needs only a read-only client. Otherwise read the live state again immediately before the push (which can only create the branch), again before opening the pull request, require the pushed ref, and open one ready pull request whose body holds the marker. Returns `{"dry_run", "manifest", "pr_number"}`. 10 requests and 3 per member; a dry run 3 and 1 per member. A change after the push leaves the branch for the operator to delete.
* `def rebuild_batch(store: BatchStore, manifest: dict[str, Any]) -> None`: The verifier. Fetch the manifest's base and member heads by id, build the stack again and require the manifest to equal the result field by field: a commit that holds anything but its member's patch, another title, number, head, base or order give other ids. The repository and branch names and the choice of the last commit are not covered by any id; the caller binds them. No path policy applies.
* `def settle_batch(api: GitHubApi, store: BatchStore, *, pr_number: int, plan: dict[str, Any], build_seal: dict[str, Any], packaged_seal: dict[str, Any], temporary_root: Path, delete_branches: bool = False) -> dict[str, list[int]]`: Needs a writable client. `plan` is the plan the batch pull request's gates ran with; the seals are the descriptors of its Build and packaged tested records, each naming the run that sealed it. Before any write: the pull request is a merged `batch/*` pull request of this repository with one marker for its own branches; the plan's identity names that pull request, its head (the marker's last squash commit), the marker's base and the marker's final tree; `rebuild_batch` holds; and `transport.download_merged_gate_pair` authenticates both original gates, whose runs GitHub records under that head, and the merged commit and its tree against the live default branch head. Then each member still open at its batched head, of this repository and to the same base, gets a comment and is closed (a head that moved between the read and the close reopens it); others are reported as `changed` or `already_closed`. With `delete_branches` a closed member's branch is deleted while its ref still is the batched head. Returns `{"closed", "changed", "already_closed", "deleted"}`. 3 requests, the 28 of the gate pair and 3 per member (5 with deletion).

## `mod_base.build_ci.batch_schema`

Owner: MB11. The closed batch manifest as plain data.

* `def validate_batch_manifest(document: Any, *, path: str = '$') -> dict[str, Any]`: Return the same `mod-base.ci.batch` v1 object after exact keys, types and bounds (1 to 50 members; printable titles of at most 256 characters) and what the shape alone can contradict: `branch` is a batch branch (`grammar.is_batch_branch`) and `base_branch` is not; member numbers and heads are distinct; a member's merge base is not its head; every squash commit is distinct from the base, from every head and merge base and from the other squash commits; each member's result tree differs from the tree before it and the final result is the last member's. Whether the ids are true is `batch.rebuild_batch`'s to decide.

## `mod_base.build_ci.batch_git`

Owner: MB11. The protected Git writer of batch stacks: plumbing in a private bare store that
nothing of the ambient Git state reaches (fixed search path for `git`, an environment built
from nothing, no system or global configuration, no hooks, credential helpers, templates,
replacement objects or attributes, one allowed transport). Requires Git 2.40.

* `BOT_NAME = 'github-actions[bot]'`
* `BOT_EMAIL = '41898282+github-actions[bot]@users.noreply.github.com'`
* `MINIMUM_GIT_VERSION = (2, 40)`
* `GITHUB_ORIGIN = 'https://github.com/'`
* `EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'`
* `TRANSPORTS = ('https', 'file')`
* `PATCH_MODES = ('100644', '100755', '120000')`
* `class BatchGitError(MbError)`: The Git writer refused, or Git itself failed (reason `ci-batch-git`).
* `def empty_batch_branch_lease(branch: str) -> str`: `--force-with-lease=refs/heads/<branch>:` for a valid batch branch: the push option that lets a branch only be created.
* `def validate_batch_push_receipt(data: bytes, *, exit_code: int, remote: str, branch: str, commit_sha: str) -> None`: Require bounded ASCII `git push --porcelain` output of exactly one new branch: the `To <remote>` header, one `*\t<commit>:refs/heads/<branch>\t[new branch]` record and `Done`, with exit status 0. An `up to date` answer for a branch that already was that commit is not a creation and is refused.
* `def supported_git_version(output: bytes) -> tuple[int, int]`: `(major, minor)` of a `git version` line; a version before 2.40 or an unreadable line raises `BatchGitError`.
* `class BatchRemote`: The repository as Git reaches it: one URL through one transport. `https` is `https://github.com/<owner>/<name>.git`; `file` is an absolute path of a local repository and carries no token. Invalid combinations raise `BatchGitError` on construction.
  * fields: `url: str, transport: str, token: str | None = None`
  * `environment(self) -> dict[str, str]`: `GIT_ALLOW_PROTOCOL` for the one transport and, for a token, an `http.https://github.com/.extraheader` authorization header passed through `GIT_CONFIG_COUNT`: never an argument, a URL or a configuration file.
* `def github_remote(repository: str, token: str | None) -> BatchRemote`: The https remote of `repository` on github.com.
* `def allowed_path_roots(allowed_paths: tuple[str, ...]) -> frozenset[str]`: Validate the caller's allowed-path list: a non-empty tuple of at most 4096 canonical repository paths in strictly ascending order. An entry admits itself and, as a directory, everything below it.
* `class PatchEntry`: One path of a member's patch; a side is `(mode, blob id)` or `None` when absent.
  * fields: `path: str, before: tuple[str, str] | None, after: tuple[str, str] | None`
* `def patch_sha256(patch: tuple[PatchEntry, ...]) -> str`: SHA-256 of the canonical JSON `[{path, before, after}]` in path order, sides as `{mode, git_blob}` or null: the manifest's `patch_sha256`.
* `class StackMember`: What one squash commit is made from.
  * fields: `number: int, title: str, head_sha: str`
* `class StackCommit`: One squash commit of a built stack and the member patch it carries.
  * fields: `member: StackMember, head_tree: str, merge_base_sha: str, patch: tuple[PatchEntry, ...], squash_sha: str, result_tree: str`
* `class BatchStack`: A built stack: the base and one commit per member, in order.
  * fields: `base_sha: str, base_tree: str, commits: tuple[StackCommit, ...]`
* `class BatchStore`: One private bare repository; create it with `open_batch_store`.
  * `fetch(self, *, base_sha: str, heads: Mapping[int, str], base_branch: str | None = None) -> None`: Fetch the base and 1 to 50 member heads and require them to be the expected commits. With `base_branch` the live refs `refs/heads/<base_branch>` and `refs/pull/<number>/head` are fetched and a different id is a base or head that moved; without it the commits are fetched by id.
  * `build_stack(self, *, base_sha: str, base_branch: str, members: tuple[StackMember, ...], allowed_paths: tuple[str, ...] | None) -> BatchStack`: One `git commit-tree` commit per member on the fetched base, with the bot identity, the base's committer time and the message `<title> (#<number>)` plus `Batch-Member: <number> <head sha>`; equal inputs give equal ids. A member's patch is the difference between its single merge base with the base and its head, applied with `git merge-tree --write-tree`. Refused, naming the pull request and paths: no or several merge bases, a conflict, a path both sides changed that either deleted, a member that leaves the tree unchanged, a submodule entry, a path outside the repository path grammar or (unless `allowed_paths` is `None`) outside the allowed list, and a result that is not the member's patch applied path by path.
  * `publish(self, *, branch: str, commit_sha: str) -> None`: Push the commit as the new batch branch with the empty lease and require the receipt of exactly one new branch; an existing branch is never moved or adopted.
* `def open_batch_store(parent: Path, remote: BatchRemote) -> Iterator[BatchStore]`: Context manager: a new store in a fresh mode-0700 directory below `parent` (a real directory only its owner can write), removed afterwards. POSIX only; refuses a Git before 2.40.

## `mod_base.build_ci.commands_batch`

Owner: MB11. `ci batch-prepare` and `ci batch-settle`, listed in `commands.VERB_MODULES`. Both
take the job arguments (`commands.add_job_arguments`: `--repo`, `--config` and `--state DIR`, the
private directory below which the Git store lives for the command's lifetime), build their API
client through `commands.api_client` with an explicit request budget, and print one canonical
JSON document.

* `def add_verbs(verbs: argparse._SubParsersAction) -> None`: Register both verbs on the `ci` verb group. `batch-prepare --name NAME --allowed-paths FILE [--dry-run] [--github-output FILE] PR...` (the file is a JSON array of repository paths; outputs `branch`, `head_sha`, `pr_number`). `batch-settle --pr N --plan FILE --build-seal FILE --packaged-seal FILE [--delete-branches] [--github-output FILE]` (outputs the four counts of its report; each seal names its own run, so no workflow is an argument).
* `def run_batch_prepare(args: argparse.Namespace) -> int`: The `batch-prepare` handler; a dry run asks for a read-only client.
* `def run_batch_settle(args: argparse.Namespace) -> int`: The `batch-settle` handler.

## `mod_base.build_ci.status`

Owner: MB11. Read-only status intents for the protected gates of one pull request head. For each
gate the newest run of its managed caller under the head is chosen before any result is read
(`selection.newest_run`). `pending`: a draft, no run yet, a run in progress or a newest run that is
a draft deferral. `success`: the newest run is complete, its exact graph for its mode
authenticates, its tested record downloads and binds to the plan, the packaged gate's owning Build
is the bundle the Build gate sealed, and the live pull request still has the plan's head, base and
test merge and is no draft. `failure`: anything else. An API failure is never a state.

* `SHADOW_SUFFIX = ' (shadow)'`: what `shadow` mode appends to both contexts.
* `class StatusError`: The evaluation does not apply or its own inputs disagree (exit 2); no intent is produced.
* `def gate_contexts(config: BuildConfig, activation: dict[str, Any] | None) -> dict[str, str]`: Gate -> the fixed context string of the protected Build config, for the gates the activation mode puts under the kit's status caller: both, with `SHADOW_SUFFIX` in `shadow`; the Build gate alone in `shared-build`; a rollback as the mode it leaves. A mode that manages no status caller is a `StatusError`.
* `def evaluate_gates(api: GitHubApi, *, pr_number: int, config: BuildConfig, activation: dict[str, Any] | None, controller_sha: str, plan: dict[str, Any] | None, temporary_root: Path) -> dict[str, Any]`: `{repository, pr_number, target_sha, gates}` for the current head of the pull request; `gates` maps each evaluated gate to `{context, state, description, target_url}` (the description is one line of at most `MAX_CI_STATUS_DESCRIPTION_CHARS`, the URL the canonical one of the deciding run or null). `plan` is the plan the job derived, which must be the plan of this pull request, controller and protected policy and of the pull request as it is now (`StatusError` otherwise); without one no gate can succeed. The pull request and both run listings are read again before the document is returned, and a difference raises.

## `mod_base.build_ci.commands_packaged`

Owner: MB11. `ci select-build` and `ci fetch-build`, listed in `commands.VERB_MODULES`. Both take
the job arguments, read the subject and the plan of the job from `--state`, build their API client
through `commands.api_client` with an explicit request budget and write nothing to GitHub.

* `BUILD_RUN`, `WAIT_SECONDS`: the argparse types of `--build-run-id` (a run id as `int`, `same-run`, or the empty string as `None`) and `--wait-seconds` (1 to 5400).
* `def add_verbs(verbs: argparse._SubParsersAction) -> None`: Register both verbs. `select-build [--wait-seconds N] [--build-run-id ID|same-run] --output FILE --github-output FILE` (outputs `found`, `build_run_id` and, when found, `selection`: the record on one line). `fetch-build --selection FILE`.
* `def job_plan(state: Path) -> dict[str, Any]`: The plan `ci plan` left in the job's state directory (`ci-plan.json`), strictly decoded and validated.
* `def run_select_build(args: argparse.Namespace) -> int`: The `select-build` handler: `selection.select_build` for this run attempt; a selection is written to `--output` (a new file of this user) and to `ci-selection.json` in the state directory.
* `def run_fetch_build(args: argparse.Namespace) -> int`: The `fetch-build` handler: `selection.fetch_build` into `exports.BUILD_VALIDATION_ROOT`; the bound record is kept as `ci-selection.json` in the state directory.

## `mod_base.build_ci.commands_status`

Owner: MB11. `ci gate-status --pr N --github-output FILE`, listed in `commands.VERB_MODULES`: the
read-only step of the status caller's `evaluate` job. It reads the protected Build config and the
activation manifest of `--repo`, creates `--state` when no earlier step did, takes the plan from
`ci-plan.json` there when the job derived one, prints the canonical document of
`status.evaluate_gates` and writes it on one line as the output `intents`.

* `def add_verbs(verbs: argparse._SubParsersAction) -> None`
* `def run_gate_status(args: argparse.Namespace) -> int`

## `mod_base.build_ci.runtime_schema`

Owner: MB11. Initial closed runtime inventory data; no native execution or success authority.

* `def validate_runtime_envelope(document: Any, *, plan: dict[str, Any] | None = None, path: str = '$') -> dict[str, Any]`: Validate exact producer/Build/plan bindings, ordered canonical inventory, lane coverage, native role bounds, per-lane and whole-scope budgets. Optional independently admitted plan binds complete ordered lanes/contracts. Native file/role derivation, actual complete frozen bytes and native report/image/log validation remain mandatory.

* `def bind_runtime_envelope(envelope: dict[str, Any], *, descriptor: dict[str, Any], owning_build: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]`: Bind closed runtime data to independently authenticated exact original runtime and owning Build selections, producer and scope. Actual API admission, bytes and native validators remain required.

## `mod_base.build_ci.runtime_exports`

Owner: MB11. Runtime byte admission and private independent copying, never native E2E authority.

* `def verify_runtime_export(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]`: Bound complete no-follow tree closure before content reads, require canonical bounded envelope and exact full size/hash inventory including empty logs, then recheck the original envelope. Requires original private frozen root, independent native mapping/validity and authentic producer/Build provenance.
* `def materialize_runtime_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]`: Atomically create a private independent regular data copy with exact inventory, staged and source revalidation before publication. Caller owns original private output parent and excludes source writers; no candidate execution, upload or native success is authorized.

## `mod_base.build_ci.runtime_freeze`

Owner: MB11. Original candidate lane reclamation through independent copying and private transfer.

* `def freeze_runtime_export(*, boundary: HostBoundary, candidate: WorkerAccount, execution: WorkerResult, inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...], plan: dict[str, Any], build: dict[str, Any], owning_build: dict[str, Any], lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]`: Root-only original runtime lane copy tied to successful retained execution, independently admitted full tested-tree inventory/native generated-root policy and the exact complete selected owning Build descriptor/bytes. Quiesce candidate, authenticate fixed traversal/source/output metadata and original plan/Build inputs, verify complete tracked source and original runtime bytes/producer/lane/whole owning Build, then independently copy under fixed sealed-runtime. Repeat original source/plan/Build/caller/runtime admission inside final private publication. Transfer only the fresh Root-owned copy to runner-private 0700/0600 through bounded regular-data transfer preserving empty logs; recheck FD and named-root identities, all runtime bytes and original source/Build/caller inputs. Reauthenticate Root and always quiesce admitted candidate, including close failure. Genuine original source/execution/API/native/tool/runtime provenance remains caller-owned; constructible values confer no second-validator/native/upload/status authority. Failed closing/cleanup may leave a private copy; never consume it.

## `mod_base.build_ci.runtime_handoff`

Owner: MB11. Original runtime context binding through the existing private execution-v1 channel.

* `def record_runtime_validation_execution(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, bound: RuntimeValidationExecution, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int) -> str`: Runner-only exclusive fixed-channel publication of genuinely retained successful lane execution and original plan/complete owning Build/runtime context, protected source-config identity and fresh random nonce. Preserve existing execution-v1 keys/bounds/canonical binary log with no path/hook/program fields. Quiesce admitted validator, inspect original three inputs before and inside publication, close caller/source snapshots and reread private staged bytes after closing admission. Existing channels never overwrite. Always terminate admitted validator; actual original execution/source/API/tool/runtime and writer-excluded private-root provenance remain caller prerequisites.
* `def freeze_handed_off_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int, nonce: str) -> dict[str, Any]`: Root-only physical private-runner channel/context admission and original runtime receipt freeze. Require exact canonical closed execution-v1 record, original nonce/current attempt/source-config/plan/three-input digest and fixed runner-private single-link layout. Reconstruct bounded RuntimeValidationExecution only after admission and delegate original frozen runtime validation. Reread original channel and all inputs/identities, close original caller/source snapshots and reauthenticate Root before returning; always quiesce admitted validator. Matching constructible records never establish protected execution/source/native provenance. Failed closing may leave a private receipt; never consume/upload it. Independently enrolled fixed Root process/request/installer/runtime and actual native/final API/graph/source authority remain required.


## `mod_base.build_ci.runtime_inputs`

Owner: MB11. Fixed frozen inputs around the enrolled native lane verifier.

* `RUNTIME_VALIDATION_ROOT`: fixed sealed-runtime sibling of the protected plan and complete Build inputs.
* `def prepare_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]`: Root-only validator-group read grant for the independently reclaimed runner-private exact runtime lane. Retain original canonical plan/complete owning Build/lane context, quiesce candidate and validator, authenticate already-prepared plan/Build and private runtime metadata/bytes, then transfer runtime through bounded regular-data handoff with original lane caps and envelope overhead. Verify all three original directory identities/bytes and caller snapshots after transfer, reauthenticate Root and return an independent retained runtime envelope. Failed admission attempts to restore admitted runtime root traversal; cleanup errors remain visible. A late descriptor-close failure may leave a granted copy. Never consume a failed handoff; restage before retry. Always terminate the admitted validator, including descriptor-close failure. Original source/API/native/copy provenance and excluded writers remain independent prerequisites; no candidate original is reclaimed or native execution/upload/status authority granted.
* `class RuntimeValidationExecution`
  * fields: `execution: WorkerResult, input_sha256: str`
* `def freeze_frozen_runtime_validation(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, bound: RuntimeValidationExecution, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, run_id: int, run_attempt: int) -> dict[str, Any]`: Root-only receipt freeze bound to genuinely retained successful lane execution and the exact original canonical plan/complete owning Build/runtime context. Derive fixed verify_runtime/unit from the retained lane, quiesce the admitted validator, inspect all three fixed roots/metadata/byte inventories before and after the existing independent validation export freeze, reject directory or original caller drift and reauthenticate Root before returning. Always terminate the admitted validator; failed closing checks may leave a private copy which must not be consumed or uploaded. Bounded diagnostic truncation alone remains permitted. Constructible bound values confer no protected execution/source/native/API provenance or upload/status authority; complete independent original admission remains mandatory across privilege transition.
* `def execute_frozen_runtime_validator(*, boundary: HostBoundary, validator: WorkerAccount, sources: ControllerSources, tools: ToolTreeProof, plan: dict[str, Any], build: dict[str, Any], runtime: dict[str, Any], lane_id: str, python: str, java_home: str | None, run_id: int, run_attempt: int) -> RuntimeValidationExecution`: Run the protected `verify_runtime` hook for one frozen lane through the same tool-fenced second-account route as Build verification. Requires the exact lane, its complete owning Build and the producing attempt; canonicalises the caller's plan/Build/runtime once and rejects any later change; authenticates the three read-only input roots before and after the hook and always terminates the validator. The result is execution data, not a receipt.
