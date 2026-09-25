# Changelog

Every release lists what changes for mods: document kinds and schema versions, the
`pixel_metrics_version`, the adapter protocol (`ADAPTER_API`) and the managed files `template sync`
rewrites. A reader of release N accepts `schema_version` N and N-1 of every kind; within one
`schema_version` only optional fields are ever added.

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
