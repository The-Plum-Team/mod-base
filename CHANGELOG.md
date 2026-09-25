# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

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
