# 0004. Adopt one unified evidence schema, version 1, without converters

Date: 2026-09-25

## Status

Accepted.

## Context

Quick Skin and Block Pops grew separate evidence formats: Quick Skin shared-source schemas 3 to 7
plus compatibility schemas 1 to 6, Block Pops raw and compact schemas 1 and 2 with an embedded
release matrix and a companion selection artifact. Both computed the same eight pixel metrics with
the same algorithm, but differed in naming, provenance layout, optional `runtime_evidence`, and
rolling-cache compatibility rules that kept old readers alive until every cache regenerated.

## Decision

- New document kinds `mod-base.*`, all at `schema_version: 1`: expectation, handoff, compact,
  anchor, family envelope, family paired projection, selection, promotion, build, site and gallery.
  Every document is strict UTF-8 JSON with no unknown keys, written as canonical JSON.
- New artifact names `mb-*`, built and parsed only by `mod_base.model.grammar`.
- No converters and forced regeneration: legacy `pages-*`, `visual-anchor-v1-*` and related
  artifacts are never read and never deleted by the kit; they expire under their own retention,
  which keeps rollback a pure revert.
- Readers accept `schema_version` N and N-1 of every kind and write N; within one version only
  optional fields may be added. `ADAPTER_API` follows the same rule.
- `runtime_evidence` is mandatory on every frame: 1 to 4096 characters, non-empty after trimming,
  no character below 32 and no DEL, which accepts every current Quick Skin and Block Pops message.
- `pixel_metrics_version` is part of every image policy; the kit's single implementation computes
  the metrics, and a change to the algorithm bumps the version and fails closed until the evidence
  is regenerated.
- The kit sees a mod's contract and matrix only through the adapter's expectation, which is
  re-derived and required byte-equal at collection and at build.

## Consequences

- One validator, renderer and rotation engine serve every mod.
- At cutover each mod's old site stays deployed until every expected key has v1 evidence
  (admission reason `awaiting-complete-v1-evidence`); Quick Skin's family view returns after its
  next compatibility wave.
- Quick Skin's historical carry-forward, `--allow-continuation`, the historical reference
  comparison and the legacy readers retire (auditable at each mod's `pre-mod-base-gallery` tag).
- `tests/test_schema_evolution.py` checks the current writer's fixtures against the previous
  release's validator once a previous release exists.

## Alternatives considered

- Converters from the old formats: privileged code parsing two untrusted legacy formats for a
  one-off event.
- Keeping all historical readers: indefinite complexity for caches that regenerate within days.
- A shorter `runtime_evidence` bound: would reject existing Quick Skin messages.
