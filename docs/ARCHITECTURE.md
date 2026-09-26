# Architecture

mod-base is the shared public-evidence and GitHub Pages publication kit of The Plum Team's
Minecraft mods (Quick Skin and Block Pops today; any further mod through
[ONBOARDING.md](ONBOARDING.md)). It turns a mod's successful packaged end-to-end run into a
validated, SHA-bound, static evidence site, and it ships the repository files every mod shares.
Both mods run the kit **fetched at one pinned commit**; nothing is vendored
([ADR 0006](adr/0006-no-vendoring.md)).

The normative contracts are [SCHEMAS.md](SCHEMAS.md) (documents and artifacts),
[ADAPTER.md](ADAPTER.md) (the mod adapter) and [INTERNAL-API.md](INTERNAL-API.md) (module
signatures). [SECURITY-MODEL.md](SECURITY-MODEL.md) explains the trust boundaries and
[OPERATIONS.md](OPERATIONS.md) the owner procedures.

## Repository layout

```
src/mod_base/        the Python package (stdlib + hash-locked Pillow), run as python3 -P -m mod_base
site/                the static front end (landing page, gallery), templated at build time
requirements/        pillow.txt: the 86-hash Pillow 12.3.0 lock shared with the mods
tools/               stdlib helpers: test runner, digest literal updater, composite tree check, API retry
.github/workflows/   publish.yml, finalize.yml, rotate.yml (reusable, called by each mod) and ci.yml
actions/             setup, prepare-evidence, publish-family, notify-pages (composites used by mod jobs)
template/            manifest.json, managed/ (byte-identical in mods) and seed/ (copied once)
canary/              the synthetic demo mod published by the separate canary repository
docs/                this documentation; docs/adr holds the decisions
tests/               unittest suites, fixtures and the porting ledger
```

## Delivery model

A mod reaches the kit in four places, all pinned to the same commit
(`The-Plum-Team/mod-base/<path>@<40-hex> # vX.Y.Z`):

| Where | What runs | Identity check |
|---|---|---|
| The managed caller `.github/workflows/pages.yml` | the reusable `publish.yml`, `finalize.yml` and `rotate.yml` | caller-owned `verify-kit` ([ADR 0001](adr/0001-two-part-implementation-identity.md)) |
| Mod producer jobs | the `prepare-evidence` and `publish-family` composites | the composite verifies its own tree against the mod checkout's pin (exit 78 on skew) |
| Mod policy jobs and tests | the `setup` composite, then kit code through `scripts/ci/mod_base_kit.py` | tree verification; the bootstrap verifies every candidate kit root |
| Mod wake jobs | the `notify-pages` composite (pure bash, `actions: write` only) | none needed: a wake is only a hint |

Three jobs stay in each mod's caller instead of the reusable workflows: `verify-kit`, `deploy` (the
only holder of `pages: write` and `id-token: write`) and `request-rotation`
([ADR 0003](adr/0003-deploy-in-caller.md)). The caller's managed region is byte-identical in every
mod apart from the pin, and `template check` enforces it
([ADR 0005](adr/0005-managed-repository-template.md)).

## Publication flow

```
mod packaged E2E (producer)
  └─ prepare-evidence composite: adapter collect -> mb-handoff--<key>--a<n> (1 day) [+ mb-anchor--...]
  └─ notify-pages composite: workflow_dispatch pages.yml operation=deploy        (actions: write only)
mod family producer (optional): publish-family -> mb-family-handoff--<family>--<key>--a<n>; notify operation=family

pages.yml (caller, permissions {}; locks mod-base-pages-publication / -rotation; hourly schedule + manual)
  verify-kit            caller shell: kit SHA from referenced_workflows == every pin, reachable from main
  publish.yml           admit -> collect <key> (matrix) -> collect <family> <key> (matrix) -> build
                        (select, download by id, authenticate, compact, validate, recheck heads, render, seal)
  deploy                caller: recheck every head, deploy-pages               (pages/id-token write)
  finalize.yml          refresh caches: mb-cache--, mb-family-cache--, mb-baseline-- (90 days)
  request-rotation      caller: workflow_dispatch pages.yml operation=rotate   (actions: write)
  ext-* jobs            mod-local extensions after finalize (QS: feature coverage request)

pages.yml operation=rotate (separate run, own lock)
  rotate.yml            owner authenticated completed/success -> delete superseded mb-* by exact id
```

The wake model is explicit `workflow_dispatch` plus an hourly recovery schedule
([ADR 0002](adr/0002-explicit-dispatch-wake.md)). Admission re-authenticates everything from
scratch, so a lost, repeated or stale wake never publishes wrong evidence: at worst the site waits
for the next hour.

`build` runs only when every collection leg succeeded; any failure skips `build` and `deploy`, so
the previously deployed site stays online. `deploy` rechecks every published branch head right
before deploying.

## Two-part implementation identity

Every published byte is fixed by two commits: the protected mod commit `github.sha` (its adapter
and `site/mod-base.json`) and the mod-base commit that `github.sha` pins and that is reachable from
mod-base `main`. Manifests record both, consumers bind a recorded kit SHA to the authenticated
owner of the artifact rather than to the current pin, and `_site/build.json` publishes both
([ADR 0001](adr/0001-two-part-implementation-identity.md)).

## Evidence model

All documents are strict, canonical JSON of `mod-base.*` kinds at `schema_version: 1`, and all
artifacts are named `mb-*` by `mod_base.model.grammar`
([ADR 0004](adr/0004-unified-evidence-schema-v1.md), [SCHEMAS.md](SCHEMAS.md)).

| Stage | Document | Artifact |
|---|---|---|
| Expectation (adapter view of the contract and matrix) | `mod-base.evidence.expectation` | embedded in every bundle |
| Producer handoff (raw PNGs, runtime tree) | `mod-base.evidence.handoff` | `mb-handoff--<key>--a<n>`, 1 day |
| Lossless anchor (canonical PNGs of the reference lanes) | `mod-base.evidence.anchor` | `mb-anchor--<key>--<commit>--<run>--a<n>` |
| Authentication record | `mod-base.selection` | embedded in collected bundles and caches |
| Compact public bundle (WebP derivatives) | `mod-base.evidence.compact` | `mb-collected--<key>` (1 day), `mb-cache--<key>--<sha>` (90 days) |
| Family envelope and paired projection | `mod-base.family.envelope`, `mod-base.family.paired` | `mb-family-handoff--`, `mb-collected-family--`, `mb-family-cache--` |
| What one run published | `mod-base.promotion` | `mb-promotion`, 1 day |
| Public site data | `mod-base.site`, `mod-base.gallery`, `mod-base.build` | the `github-pages` artifact |

The adapter is the only component that understands a mod's contract, matrix and packaged output.
The kit re-verifies every adapter result before publishing (R1–R6 in [ADAPTER.md](ADAPTER.md)).

## Package structure

| Package | Responsibility |
|---|---|
| `mod_base.io` | secure JSON, atomic directories, content cache, sealing, bounded trees, bounded ZIP |
| `mod_base.github` | read-only-by-default REST client with bounded retry, runs, jobs, artifacts, contents, in-memory fake |
| `mod_base.model` | grammar, limits, canonical JSON, strict document validators |
| `mod_base.config`, `mod_base.workflow` | `site/mod-base.json` validation; the single source of job and step names |
| `mod_base.adapter` | the hook protocol, the isolated `env -i` host and child, the hook context |
| `mod_base.imaging` | the 8-key pixel metrics, comparisons, canonical PNG, deterministic WebP (the only Pillow user) |
| `mod_base.evidence` | expectation, prepare, validate, compact, compose, anchor |
| `mod_base.family` | family envelopes and paired projections |
| `mod_base.pages` | targets, admission, selection, source authentication, build, refresh, rotation |
| `mod_base.pin`, `mod_base.template` | the pin, kit-digest-v1 and kit resolution; `template check/sync/init` |
| `mod_base.conformance` | the synthetic producer-to-rotation simulation used against real mod contracts |

`python3 -P -m mod_base <command>` dispatches through a static registry in `mod_base.cli`; each
command's flags are registered by its owning `commands*.py` module and are frozen (SPEC §2.2).

## Repository template and bootstrap

`template/manifest.json` classifies every shared repository file
([ADR 0005](adr/0005-managed-repository-template.md)):

- **managed** files are byte-identical in every mod: `.gitattributes`, the caller
  `.github/workflows/pages.yml` (managed region plus an `ext-*` extension region),
  `scripts/ci/mod_base_kit.py` and the two shared agent documents under `docs/ai/shared/`;
- **fragment** files are seeded once and must keep listed lines or structure: `.gitignore`,
  `.github/CODEOWNERS`, `.github/dependabot.yml`, `.github/pull_request_template.md`, `AGENTS.md`;
- **seeded** files are copied once by `template init --seed`;
- a mod may list its non-control root files (`.gitattributes`, `.gitignore`, Dependabot, the
  pull-request template, `AGENTS.md`) in `template.deferred` while an adoption is staged across
  pull requests; they may be absent, and a present one only reports its missing required lines as
  pending.

The managed bootstrap `scripts/ci/mod_base_kit.py` is the only way mod code finds the kit. It
parses the single pin and resolves the kit root in a fixed order, verifying every candidate: a
stamped `out/mod-base-kit/` overlay staged by a trusted controller, `MOD_BASE_KIT_PATH` with a
matching `MOD_BASE_KIT_SHA` (exported by the `setup` composite), a user cache outside the
repository, and an anonymous shallow fetch of the pin into that cache. It also implements
`verify --network`, `stage` (Block Pops' controller-to-sandbox handoff) and `bump`. An overlay's
`template/` and `tools/`, outside kit-digest-v1, must match the listing
`src/mod_base/template/staged_files.sha256` inside the digested tree, and its `actions/` (staged
from v0.9.2 when the kit carries it) the listing `src/mod_base/template/staged_actions.sha256`
there, and a verified kit never holds bytecode.

## Front end

The site is the Quick Skin UI made configuration-driven: build-time escaped placeholders from a
closed set, `<!-- mb:if ... -->` blocks, a generated `theme.css`, and all labels and copy delivered
through data. The published tree uses only relative URLs and local assets, a strict meta Content
Security Policy, and `textContent`-only rendering. A mod without families gets no family UI at all.

## Versions and compatibility

- `mod_base.__version__` equals the tag without `v`.
- Readers accept `schema_version` N and N-1 of every kind and write N; `ADAPTER_API` follows the
  same rule; `pixel_metrics_version` must equal the current one, so a metric change forces
  regeneration.
- Consumers never require a recorded kit SHA to equal the current pin, so a bump does not discard
  valid evidence; they require it to match the artifact's authenticated owner.

## Decisions

1. [Two-part implementation identity](adr/0001-two-part-implementation-identity.md)
2. [Explicit dispatch wake](adr/0002-explicit-dispatch-wake.md)
3. [Deploy in the caller](adr/0003-deploy-in-caller.md)
4. [Unified evidence schema v1](adr/0004-unified-evidence-schema-v1.md)
5. [Managed repository template](adr/0005-managed-repository-template.md)
6. [No vendoring](adr/0006-no-vendoring.md)
