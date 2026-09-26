# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

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
