# Schemas (v1)

Every document the kit reads or writes, its exact fields and the structural rules
`mod_base.model.documents` (and `mod_base.config` for the config) enforce. The validators are
the normative definition; this page explains them. A valid example of every kind lives in
`tests/fixtures/documents/valid/`, the QS and BP configs in `tests/fixtures/documents/config/`,
and targeted negative cases in `tests/fixtures/documents/invalid/`.

## Common rules (SPEC §3.0)

**Encoding.** Strict UTF-8 JSON, no byte-order mark, no duplicate object keys, no `NaN`,
`Infinity` or `-Infinity` (nor any number that overflows to them), no key or string holding a lone
surrogate (a `\ud800`-style escape is valid ASCII but decodes to a code point that is not Unicode
text and cannot be re-encoded as UTF-8), no unknown keys, exact types.
A JSON boolean is never an integer; integer fields reject `1.0`; number fields accept JSON integers
or floats. Written documents are `canonical_json`: `json.dumps(sort_keys=True,
separators=(",", ":"), ensure_ascii=False, allow_nan=False)` in UTF-8 plus one trailing `\n`. The
SHA-256 of a document is the SHA-256 of exactly those bytes; `canonical_sha256(value)` is the
identity of an embedded object (for example `scope.detail_sha256`). `canonical_json` refuses a
non-`str` object key (instead of silently writing `true`/`1`) and any unencodable value with an
`MbError`, so every document that strict decoding accepts is always encodable and hashable.

**Canonical bytes.** Every JSON file of a kit bundle or artifact must be exactly the
`canonical_json` bytes of its value (one document, one byte sequence, one hash): `manifest.json`,
`expectation.json`, `selection.json` and `extensions.json` of every handoff, compact (including a
`compose` hook's composed bundle) and anchor bundle, `envelope.json`, `promotion.json` and a collected
family artifact's `selected.json`. The kit writes nothing else, and every validating reader
(`validate`, `compact`, `compose`, the `anchor` verbs, `family collect`, `build`, `refresh`, and
`rotate` for `promotion.json`) refuses any other encoding of the same value. `family collect
--selected-json F` requires the canonical bytes `select --output F` wrote, since it re-emits them as
`selected.json`. Documents handed to a command as an argument (a selection draft, `--extensions`,
`--tested-run-json`), an adapter's `family_validate` projection (which the core re-emits canonically
as `paired.json`) and the config are strict JSON but need not be canonical.

**Header.** Every document carries `"kind": "mod-base.<...>"` and `"schema_version"`. This kit writes
the versions in `mod_base.SCHEMA_VERSIONS` (all `1`) and reads N and N-1 (`{1}` today). Within one
`schema_version` only optional fields may ever be added.

**Errors.** A rejection is a `DocumentError` (an `MbError`, exit 2) whose message starts with a
JSONPath-like location, for example `$.frames[3].source.pixel.meaningful_colors: must be between 0
and 32`.

### Identifiers

| Name | Grammar | Notes |
|---|---|---|
| `KEY` | `^[a-z0-9](?:[a-z0-9._]\|-(?!-)){0,62}[a-z0-9]$` | Never contains `--`. QS `mc1.20.1`; BP 24-hex `branch_token`. |
| `FAMILY` | `^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$`, at most 32 characters | QS `mod-compatibility`. |
| `LANE_ID` | `^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+){1,3}$`, at most 200 | Opaque: never parsed. |
| `SHA1` / `SHA256` / `DIGEST` | `^[0-9a-f]{40}$` / `^[0-9a-f]{64}$` / `^sha256:[0-9a-f]{64}$` | Lowercase only. |
| `BRANCH` | `^(?!/)(?!.*(?:\.\.\|//))[A-Za-z0-9._/-]{1,200}$` | BP's rule. |
| `REPOSITORY` | owner `[A-Za-z0-9][A-Za-z0-9_-]{0,38}`, name `[A-Za-z0-9_.-]{1,100}` except `.` and `..` | Never a traversal (`../..`); `RUN_URL` and workflow refs embed the same grammar. |
| `VERSION` | `X.Y.Z` without leading zeros | The kit version (tag without `v`). |
| `IDENT` | `^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$` | `frame_id`, `capture_id`, `comparison_id`, `pair_id`. |
| `ARTIFACT_NODE`, `MINECRAFT` | `^[A-Za-z0-9][A-Za-z0-9._+-]{0,79}$`, `{0,39}` | |
| `LOADER`, `ROLE`, `REVIEW_TIER` | `^[a-z][a-z0-9_-]{0,31}$`, `^[a-z0-9][a-z0-9_-]{0,39}$` (both) | |
| `SCENARIO`, `PROFILE` | `^[a-z0-9][a-z0-9._-]{0,79}$`, `{0,39}` | |
| `STEP` | `^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$` | |
| `EXTENSION_NAME` | `^[a-z0-9-]+\.[a-z0-9_]+$`, at most 80 | `quick-skin.runtime_source`. |
| `EVENT` | `^[a-z_]{1,40}$` | |
| `WORKFLOW_PATH` | `^\.github/workflows/[A-Za-z0-9._-]{1,100}\.ya?ml$` | |
| `TIMESTAMP` | `YYYY-MM-DDTHH:MM:SSZ`, a real calendar time | GitHub's `created_at`. |
| `RUN_URL` | `https://github.com/<repository>/actions/runs/<id>` | Always built by the renderer. |
| bundle path | `/`-separated components `[A-Za-z0-9_][A-Za-z0-9._-]*` (or a dotfile), no `.`, `..`, empty, absolute or backslash component, at most 16 components and 300 characters | |
| repo path | a bundle path with no `.git` component (case-insensitive) | Config and template paths. |

### Text rules

* **Evidence text** (`runtime_evidence`, `title`, `expectation`, reasons): 1..N characters,
  non-empty after `strip()`, no character below U+0020, no DEL (U+007F) and no surrogate code
  point. This is QS's `MAX_RUNTIME_EVIDENCE_LENGTH` rule (V9), defined on code points only.
  `runtime_evidence` is **mandatory** on every frame and pair, N = 4096. Titles: 200.
  Expectations: 4096. Reasons: 200.
* **Display text** (config copy, labels, site and gallery copy): trimmed, non-empty, bounded, free
  of `<`, `>`, `{{` and `}}` (so no text can open a tag, a template placeholder or an `<!-- mb:`
  block), and every character is U+0020 or a letter, mark, number, punctuation or symbol **in the
  frozen Unicode 3.2 database** (`unicodedata.ucd_3_2_0`, identical in every CPython). The
  interpreter's own `str.isprintable()` is not used: it follows the running Python's Unicode
  version (a Unicode 15 character is printable on 3.13 and unassigned on 3.11), and a config valid
  in CI must be valid on every supported interpreter. Characters assigned after Unicode 3.2 (most
  emoji, U+20B9) are refused everywhere; controls, bidi and zero-width format characters,
  private use, surrogates and separators other than the space always are.

### Shared objects

| Object | Fields |
|---|---|
| `PixelMetrics` | exactly `{width, height, file_sha256, pixel_sha256, luma_entropy: 0..8, meaningful_colors: 0..32, dark_fraction: 0..1, light_fraction: 0..1}`; `width x height <= 20,000,000`. Wherever an image records `width`/`height` they equal the metrics, and `pixel.file_sha256` equals the image's `sha256`. |
| `CompareMetrics` | `{changed_fraction: 0..1, rms_difference: 0..255, required_changed_fraction: 0..1, region?: [l,t,r,b]}` with `0<=l<r<=1`, `0<=t<b<=1`. In a comparison, `required_changed_fraction == minimum_changed_fraction`, `changed_fraction >= minimum_changed_fraction` and `region` equals the comparison's. |
| `RunClaim` | `{run_id: 1.., run_attempt: 1..1000, workflow_path, branch, commit, controller_branch, controller_sha}`: written by producers from the environment, no API data. A handoff or producer run is its own controller (`controller_* == branch/commit`); `run_claim_from_environment` builds it from `GITHUB_RUN_ID/ATTEMPT/SHA/REF_NAME/WORKFLOW_REF`. |
| `RunRecord` | `RunClaim` + `{event, created_at, conclusion: "success", head_sha, display_title?, job_graph_sha256?}` from the API. A handoff or family producer run is its own controller, so its record has `head_sha == commit == controller_sha` (`own_run_record`). A `none`/`attested` tested run has `head_sha == controller_sha`; only a `delegated` tested run's API head is unconstrained (QS PR reuse tests a merge commit; the reuse is proven by `authenticate_extensions`). `github.runs.run_record(..., require_controller_head=False)` builds exactly that one case. |
| `KitRef` | `{repository: "The-Plum-Team/mod-base", sha: SHA1, version: VERSION}` |
| `Subject` | `{branch, commit: SHA1, tree: SHA1}` |
| `FileRecord` | `{path: bundle path, sha256, size: 1..}`; every `files` array is sorted by `path` without duplicates and never lists `manifest.json`. |

### Artifacts (names built and parsed only by `mod_base.model.grammar`)

| Artifact | Retention (days) | Builder |
|---|---|---|
| `mb-handoff--{key}--a{attempt}` | 1 | `handoff_name` |
| `mb-anchor--{key}--{commit}--{run_id}--a{attempt}` | `config.anchor.retention_days` (<= 90) | `anchor_name` |
| `mb-family-handoff--{family}--{key}--a{attempt}` | `families[].retention_days` (<= 7) | `family_handoff_name` |
| `mb-collected--{key}` | 1 | `collected_name` |
| `mb-collected-family--{family}--{key}` | 1 | `collected_family_name` |
| `mb-promotion` | 1 | `PROMOTION_NAME` |
| `github-pages` | 1 | `PAGES_ARTIFACT_NAME` |
| `mb-cache--{key}--{coverage_sha}` | 90 | `cache_name` |
| `mb-family-cache--{family}--{key}--{coverage_sha}` | 90 | `family_cache_name` |
| `mb-baseline--{key}--{commit}--{tested_run_id}` | 90 | `baseline_name` (`baseline_name_regex(key_pattern)` for QS's literal) |

`parse_artifact_name` returns `None` for every name that is not exactly one of these (including
every legacy `pages-*` name); a `None` name is never listed, downloaded or deleted by the kit.

**Baselines (amendment to the SPEC §3.0 `mb-baseline` row).** `refresh` retains
`mb-baseline--<key>--<commit>--<tested_run_id>` once per new complete generation, never again: only
when `baseline_archive.enabled`, the promoted bundle's scope is `complete`, its embedded selection is
a `handoff` (a `cache` republishes a generation an earlier publication already promoted, and
re-retaining it would stretch the baseline's 90 days) and no upload of that exact name already passes
the consumers' owner check (R3, `compose.authenticate_baseline`: a successful earlier `pages.yml` run
whose `Finalize / Refresh evidence cache for <key>` job uploaded it in its retention step; at most
`limits.MAX_CANDIDATES` unexpired default-branch uploads by another run are tried, newest first; one
that is gone (404) or fails the check is no retained baseline, every other API error stops `refresh`).
Rotation never retires a baseline.

**Rotation budget (amendment to SPEC §5.5).** One rotation run deletes at most
`limits.DELETION_BUDGET` = 64 artifacts (the SPEC's 32 could not retire even the ~35 long-lived
artifacts a Quick Skin generation supersedes). It retires the longest-lived first (the previous
generation's caches, family caches and anchors, then the leftovers an earlier rotation deferred, then
family handoffs, handoffs and the owner's transients, the promotion last); every deletion is still an
exact-ID deletion after the owner and replacement checks and the configured delete delay.
Every bound in the SPEC §3.0 limit table is a constant in `mod_base.model.limits`.

**Archive cap and expanded totals (amendment to SPEC §3.0).** Every SPEC §3.0 expanded bound keeps its
value: a raw bundle (`MAX_RAW_BUNDLE_BYTES`), an anchor (`MAX_ANCHOR_BUNDLE_BYTES`) and a collected
family (`MAX_COLLECTED_FAMILY_BYTES`) are each at most 1 GiB of files. A family handoff or cache is
smaller (the family byte budget below). An archive is larger than the files it holds (per-entry
headers, deflate framing; PNG and WebP payloads do not compress), so the one cap on the ZIP bytes a
consumer admits, selects and downloads into memory is that bound plus a documented overhead margin:
`MAX_ARTIFACT_BYTES` = `MAX_RAW_BUNDLE_BYTES` + `MAX_ARCHIVE_OVERHEAD_BYTES` (1 GiB + 32 MiB =
1,107,296,256 bytes). The largest archive each kind accepts, `bounded_zip.archive_limit(kind)`
(expanded total + 1/512 of it + 1 KiB per entry + 1 MiB), exceeds its expanded total by at most
`MAX_ARCHIVE_OVERHEAD_BYTES` for every kind (at most 1,093,667,840 bytes, the collected family;
`tests/test_model_limits.py`). A producer that stays within its expanded bound (`prepare`, `anchor
create`, `family envelope`, `family collect`) therefore uploads an archive every consumer accepts,
instead of one refused only at admission or at download. Every consumer that admits, selects or
authenticates an artifact bounds its size (an archive size) by `bounded_zip.artifact_limit(kind)`,
the kind's `archive_limit`; a family handoff or cache is narrowed to its family's `handoff_max_bytes`
plus `envelope.json` (`select.family_archive_limit`) by `admit`, by `select` (an oversized family
handoff is skipped as unusable, an oversized family cache fails selection as an ordinary cache does)
and by `build` (either kind in a leg's recorded selection). Rotation, which only reads a family cache
to plan deletions, downloads it within the `family-cache` kind's limit. This replaces SPEC §5.3.1's
`0 < size ≤ 1 GiB` (`deploy`) and `size ≤ handoff_max_bytes` (`family`) admission checks, which
compared an archive with an expanded bound. Recorded artifact sizes (`Selected.size`,
`selected_artifact.size`, a compact bundle's `source_artifact.size`) are archive sizes, bounded by
`MAX_ARTIFACT_BYTES`; a `FileRecord` size is bounded by the expanded `MAX_RAW_BUNDLE_BYTES`.

Extraction bounds per downloaded kind (`io.bounded_zip.LIMITS_BY_KIND`, all built from `limits`;
every archive is also at most `archive_limit(kind)` <= `MAX_ARTIFACT_BYTES` compressed, and at most
200:1):

| Kind | Entries | Expanded total | Per entry | Suffixes |
|---|---|---|---|---|
| handoff | `MAX_HANDOFF_FILES` + 1 (4099) | `MAX_RAW_BUNDLE_BYTES` (1 GiB) | 32 MiB | `.json`, `.png` |
| anchor | `MAX_ANCHOR_FILES` + 1 (1002) | `MAX_ANCHOR_BUNDLE_BYTES` (1 GiB) | 32 MiB | `.json`, `.png` |
| cache, collected, baseline | `MAX_COMPACT_FILES` + 1 (1004) | `MAX_COMPACT_BUNDLE_BYTES` (256 MiB) | 16 MiB | `.json`, `.webp` |
| family-handoff, family-cache | `MAX_FAMILY_FILES` + 1 (8193) | `MAX_FAMILY_BUNDLE_BYTES` (788,463,616) | 32 MiB | any |
| collected-family | `MAX_COLLECTED_FAMILY_FILES` (16387) | `MAX_COLLECTED_FAMILY_BYTES` (1 GiB) | 32 MiB | any |
| promotion | 2 | 4 MiB | 4 MiB | `.json` |

**Collected-family limits and the family byte budget (amendment; the SPEC §3.0 table has no row for
them).** A collected family artifact holds `paired.json`, `selected.json`, at most `MAX_FAMILY_FILES`
projection images and `source/`, the selected family handoff or cache verbatim (`envelope.json` plus
at most `MAX_FAMILY_FILES` native files of any suffix): `MAX_COLLECTED_FAMILY_FILES` = 2 x
`MAX_FAMILY_FILES` + 3 entries. Its expanded bytes are `MAX_COLLECTED_FAMILY_BYTES` =
`MAX_RAW_BUNDLE_BYTES` (1 GiB), because `build` downloads it as one archive within the cap above. The
family bounds partition that budget exactly, so every generation a producer's `family envelope`
accepts is collectable and nothing is refused only at collection (where exit 2 would fail the
`family` job, skip `build` and block every publication until the generation is superseded):

| Part | Bound | Bytes |
|---|---|---|
| projection images (`family_validate`'s `images/`) | `MAX_FAMILY_PROJECTION_BYTES` = `MAX_COMPACT_BUNDLE_BYTES` | 268,435,456 |
| `paired.json` | `MAX_PAIRED_BYTES` | 16,777,216 |
| `selected.json` | `MAX_SELECTED_JSON_BYTES` | 65,536 |
| `source/envelope.json` | `MAX_ENVELOPE_BYTES` | 4,194,304 |
| `source/` native bundle (`families[].handoff_max_bytes` at most) | `MAX_FAMILY_HANDOFF_BYTES` (the rest) | 784,269,312 |

A family handoff or cache is the native bundle plus its envelope, `MAX_FAMILY_BUNDLE_BYTES` =
`MAX_FAMILY_HANDOFF_BYTES` + `MAX_ENVELOPE_BYTES` (the `family-handoff`/`family-cache` extraction
total). Config refuses a `handoff_max_bytes` above `MAX_FAMILY_HANDOFF_BYTES` (before this amendment
the ceiling was 1 GiB, which left the envelope and the projection no room); `family collect` still
refuses a larger output before it is uploaded, which the partition makes unreachable.
`MAX_ZIP_ENTRIES` is the largest entry bound of any kind (16387); `bounded_zip` refuses any
extraction limits above it.

## `mod-base.evidence.expectation` (`expectation.json`, <= 16 MiB)

The only view of a mod's contract and matrix the kit sees; produced by the adapter hook
`expectation`, re-derived at collect and build and required byte-equal (R2).

| Field | Type |
|---|---|
| `repository`, `key`, `label` (display, <= 80) | |
| `subject` | `Subject` |
| `matrix_sha256`, `contract_sha256` | `SHA256` |
| `contract_path` | repo path (`e2e/scenario-contract.json`) |
| `profile` | `PROFILE` (QS `pr`; BP `pr-anchors`/`scheduled-anchors`) |
| `scope` | `{kind: "complete"\|"selected", detail_sha256?, detail?: object}`; `detail` is at most 64 KiB canonical and requires `detail_sha256 == canonical_sha256(detail)`; `selected` requires `detail_sha256` |
| `image_policy` | `{source_size: [w,h], derivative_box: [w,h], webp_quality: 1..100, webp_method: 0..6, pixel_metrics_version: 1}` and equals `Config.image_policy()` |
| `scenarios` | `[{id: SCENARIO, title?: display <= 80}]`, 1..256, unique ids |
| `lanes` | `[{lane_id, artifact_node, minecraft, loader, java: 8..99, scenario, roles: [ROLE] (unique, 1..16)}]`, 1..256 |
| `captures` | `[{frame_id, capture_id, capture_order: 0.., lane_id, role, step, title, expectation, review_tier}]`, 1..1000 |
| `comparisons` | `[{comparison_id, lane_id, role, first_frame_id, second_frame_id, minimum_changed_fraction: 0..1, region?}]`, <= 1000 |
| `anchor` | `{artifact_nodes: [ARTIFACT_NODE] sorted unique}` or `null` |

Rules: unique scenario, lane, frame and comparison ids; every lane's scenario is declared; every
capture names an existing lane and one of its roles; `(lane_id, capture_id)` unique;
`capture_order` strictly increases within each `(lane_id, role)` in list order; a comparison names
two distinct frames of its own lane and role; anchor nodes are lane nodes.

## `mod-base.evidence.handoff` (`manifest.json`, raw, private, 1 day)

Bundle layout: `manifest.json`, `expectation.json`, `extensions.json` (optional), `runtime/**`
(only `.json` <= 4 MiB and `.png` <= 32 MiB, at most 4096 files; the whole bundle at most
`MAX_RAW_BUNDLE_BYTES`, 1 GiB).

| Field | Type |
|---|---|
| `repository`, `key`, `kit: KitRef`, `subject` | |
| `provenance` | `{handoff: RunClaim, tested: RunClaim, reuse: "none"\|"attested"\|"delegated", coverage_sha}` |
| `expectation` | `{path: "expectation.json", sha256, size}` |
| `matrix_sha256`, `contract_sha256` | |
| `scope` | `{kind: "complete"\|"selected", detail_sha256?}` |
| `extensions` | `{path: "extensions.json", sha256, size <= 1 MiB, names: [EXTENSION_NAME] sorted, 1..16}` or `null` |
| `lanes` | expectation lane fields + `{profile, status: "pass", elapsed_s?: >= 0, jars: {production_sha256, harness_sha256?}}` |
| `frames` | capture fields + `{artifact_node, minecraft, loader, scenario, runtime_evidence, source: {path: "runtime/...png", sha256, size, width, height, format: "png", pixel: PixelMetrics}}` |
| `comparisons` | expectation comparison fields + `{source: CompareMetrics}` |
| `files` | `[FileRecord]`: every file except `manifest.json` |

Rules: frames agree with their lane (node, Minecraft, loader, scenario, role) and follow the capture
order rule; `coverage_sha == subject.commit` (ordinary evidence is never carried forward); the
handoff claim is its own controller; `reuse: "none"` means the tested run is the handoff run, read
as "`tested == handoff` apart from controller fields" with the Block Pops controller split: the
same id, attempt and workflow, the tested `branch`/`commit` equal to `subject.branch`/`subject.commit`
(what the run tested) and the tested `controller_branch`/`controller_sha` equal to the handoff's
`branch`/`commit` (the run's own head); `attested` and `delegated` name a distinct tested run; `delegated` requires `extensions`; `files` holds `expectation.json`,
`extensions.json` exactly when `extensions` is set, every frame PNG with matching hash and size, and
otherwise only `runtime/**.json` and `runtime/**.png`. With the embedded expectation: frames equal
its captures in order (copied fields verbatim), lanes equal its lanes, comparisons equal its
comparisons, and repository, key, subject, hashes and scope agree. With `config.adapter.extensions`,
every extension name is declared. The config-dependent rules (`attested` job, delegated extension
name, `display_title`) are applied by `pages.authenticate`.

## `mod-base.evidence.compact` (`manifest.json`, public cache, 90 days)

Bundle layout: `manifest.json`, `expectation.json`, `selection.json`, `extensions.json?`,
`images/<webp_sha256>.webp` (each <= 4 MiB, 256 MiB in total).

The handoff fields, plus:

| Field | Type |
|---|---|
| `source_artifact` | `{kind: "handoff"\|"cache", id, name, digest, size, run_id, run_attempt}`; `name` is this key's handoff name of `run_attempt` or this key's cache name of `coverage_sha` |
| `selection` | `{path: "selection.json", sha256, size <= 1 MiB}` |
| `scope` | `{kind: "complete"\|"composed", detail_sha256?, components?}`; `composed` requires `components: {baseline: {artifact_id, name (mb-baseline of this key), digest, manifest_sha256}, selected_manifest_sha256}` and `complete` forbids it. `"selected"` exists only for the intermediate compaction `compose` hands to the adapter (`validate_compact(intermediate=True)`), never for an uploaded bundle. |
| `frames[].source` | `{sha256, size, width, height, format: "png", pixel}` (no path: the PNG is not shipped) |
| `frames[].derivative` | `{path: "images/<sha256>.webp", sha256, size, width, height, format: "webp", pixel}` |
| `frames[].epoch`, `frames[].tested` | only in `composed` bundles, then mandatory: `"baseline"\|"selected"` and `RunRecord + {jar_sha256}` |
| `comparisons[].derivative` | `CompareMetrics` re-measured on the derivatives (same minimum rule) |

Rules: `files` is exactly `expectation.json`, `selection.json`, `extensions.json` (iff set) and the
set of derivative images; with the embedded expectation every derivative size equals
`thumbnail_size(source, image_policy.derivative_box)` (Pillow 12.3 `Image.thumbnail`), and a
published (`complete` or `composed`) bundle embeds the **complete** expectation (R3: composed
frames equal the complete expectation; a compose hook therefore emits the extensions from which
the adapter re-derives the complete expectation, R2), while the intermediate embeds its selected
expectation. `manifest.json`'s selection-independent identity is `compact_identity_sha256`: the
canonical manifest without its `selection` member and without the `selection.json` record of
`files`.

## `mod-base.evidence.anchor` (`manifest.json`, lossless, durable)

Bundle layout: `manifest.json`, `expectation.json`, `images/<png_sha256>.png` (canonical metadata-free RGB).

| Field | Type |
|---|---|
| `repository`, `key`, `kit`, `subject` | |
| `provenance` | `{handoff: RunClaim, tested: RunClaim}` |
| `reference` | `{artifact_nodes, minecraft, loaders}`: each the sorted distinct values of `lanes` |
| `source_artifact` | `{id, name, digest, run_id, run_attempt}`: the handoff it was cut from (this key's handoff name, owned by `provenance.handoff`) |
| `expectation`, `lanes`, `files` | as in the handoff (lanes: the reference subset) |
| `frames[].source` | `{path: "images/<sha256>.png", sha256, pixel_sha256, size, width, height, format: "png", pixel}` with `pixel_sha256 == pixel.pixel_sha256` |

Rules: an anchor exists only for a direct canonical run (`handoff.controller_sha ==
subject.commit`); with the expectation, the expectation declares an anchor (`anchor` is not null)
whose `artifact_nodes` equal `reference.artifact_nodes`, and the lanes and frames are exactly the
expectation's lanes of those nodes and their captures.

**Eligibility** (`evidence.anchor.eligible_nodes`, decided from the validated handoff by `prepare`,
`anchor identity` and `anchor create`) adds: `config.anchor.enabled`; the handoff run ran on the
subject branch (`handoff.branch == subject.branch`); its pixels were not re-published by
attestation (`reuse != "attested"`; `delegated` reuse stays eligible and the anchor records the
tested run its pixels came from); and the adapter's `anchor_selection` chose nodes (an adapter
without the hook declines every anchor). `anchor create` refuses an ineligible handoff with reason
`anchor-ineligible`. **Validation** (`anchor validate`, which reads the anchor alone; its
provenance records no `reuse`) re-checks the direct-run shape with reason `anchor-source`:
`handoff.branch == subject.branch`, and a tested run that is the handoff run tested exactly the
subject; given the raw artifact id, name and digest, the anchor must also name exactly that
handoff artifact (`anchor-source`). Every image is its own canonical re-encoding (`canonical_png`,
unchanged pixels) and the anchor records the handoff's `kit`.

## `mod-base.family.envelope` (`envelope.json`)

`{repository, family, key, kit, subject, coverage_sha, carried_from?, producer: RunClaim,
native: {manifest_path: "manifest.json", manifest_sha256, kind (<= 80), schema_version: 1..1000},
files: [FileRecord]}` — `files` is the exact inventory of the native bundle (never `envelope.json`),
at most `families[].handoff_max_bytes`, and includes `manifest.json` hashed as
`native.manifest_sha256`; the producer is its own controller; `carried_from != coverage_sha`.

`family.envelope` also requires, at creation and at every validation: the family is configured and
the envelope names this repository; the producer ran the family's `producer.workflow` at exactly
the subject (`producer.branch`/`commit` equal `subject.branch`/`commit`); the envelope covers
exactly that checkout (`coverage_sha == subject.commit`); `carried_from` only for a family with
`carry_forward` (re-proven, R5, up to `coverage_sha` at every collection; `family envelope` never
writes it); at most `MAX_FAMILY_FILES` non-empty native files of at most 32 MiB each, a root
`manifest.json` whose `kind`/`schema_version` equal `native`, paths that stay canonical below the
collected `source/` directory and never collide under case folding; and canonical bytes.

## `mod-base.family.paired` (the projection `family_validate` writes)

| Field | Type |
|---|---|
| `family`, `key`, `coverage_sha`, `subject` | |
| `status` | always `"available"` (superseded/unavailable are hook statuses, never projected) |
| `provenance` | `{producer: RunRecord (its own controller), links: [{label (display <= 40, unique), run_id}]}` |
| `contracts` | `{name: SHA256}`, <= 16 |
| `image_policy` | `{derivative_box, webp_quality, webp_method}`; equals the configured family policy |
| `lanes` | `[{lane_id, artifact_node, minecraft, loader, variant: {id, name, version, version_id}, review: {reviewed_frame_count, manifest_sha256, proof_sha256, report_sha256}, pairs}]` |
| `pairs` | `[{pair_id, capture_id, reference_capture_id, title, expectation, runtime_evidence, verdict: {runtime_passed, semantic_valid, matches_reference: bool\|null, defect}, metrics: {semantic_changed_fraction, perceptual_delta, candidate_semantic_sha256, reference_semantic_sha256}, reference: Side, candidate: Side}]` |
| `Side` | `{image: {path: "images/<sha256>.webp", sha256, size, width, height, format: "webp", pixel}, source: {sha256, width, height, pixel}}` |
| `not_applicable` | `[{artifact_node, minecraft, loader, variant_id, variant_name, reason}]` |

Rules: every pair is publishable (`runtime_passed and semantic_valid and not defect`); lane ids and
`(artifact_node, variant.id)` unique; pair ids unique per lane; image dimensions equal
`thumbnail_size(source, derivative_box)`; not-applicable entries repeat neither each other nor a lane.

`family collect` binds the projection to the authenticated envelope (R4): equal `subject`,
`provenance.producer` carrying exactly the envelope's producer `RunClaim` with an `event` among the
family's `producer.events`, `coverage_sha == expected_coverage_sha`, image records type-exact with
the kit's `inspect_webp` metrics, and one identical record per image path. `build` then
authenticates the producer attempt (a successful `families[].producer.workflow` run of this
repository on the default branch at `producer.commit`, with an event among `producer.events`),
requires `provenance.producer` to equal its `github.runs.run_record` (and a `job_graph_sha256`, when
present, to be that attempt's job graph) and binds the envelope's `kit` to the pin of
`producer.workflow` at the producer head (SPEC §1.8), for a family handoff and a family cache alike.
The adapter's obligations are listed under `family_validate` in [ADAPTER.md](ADAPTER.md#hooks).

## `mod-base.selection` (`selection.json`, embedded in every collected artifact and cache)

`{repository, key, kit, implementation: {branch, sha, workflow_ref, run_id, run_attempt}, subject,
coverage_sha, selected_artifact: {kind: "handoff"|"cache", id, name, digest, size, run_id,
run_attempt, workflow_path, created_at}, source: {handoff_run: RunRecord, tested_run: RunRecord,
reuse, attestation_job?: {name, id, conclusion: "success"}, job_graph_sha256?, kit_binding:
{source: "referenced_workflows"|"workflow_file", sha}}, expectation_sha256, source_manifest_sha256,
manifest_sha256, binding: {mode: "reencode-identical"|"cache-revalidated", frames, derivatives},
extensions_verified: [sorted names], composition?: {baseline_artifact: {id, name, digest,
owner_run_id}, selected_manifest_sha256}}`

**Two phases.** `authenticate` writes the **draft** (`validate_selection(draft=True)`): every field
except `manifest_sha256`, `binding` and `composition`, which a draft must not carry. The step that
writes the compact bundle completes it and embeds the **final** selection (`draft=False`, which
requires `manifest_sha256` and `binding`); see "The collect flow" below.

Field meanings: `kit` is the executing kit of the collecting Pages run (the same `KitRef` as the
compact manifest it is embedded in). `source.kit_binding.sha` is the selected artifact's recorded
`kit.sha`, proven against its owner (SPEC §1.8: a handoff's pin in `source.workflow` at the handoff
run's head, `workflow_file`; a cache's `referenced_workflows` of its Pages run). It is **not**
compared with `kit`: after a kit bump an older, still valid cache or handoff keeps its older kit,
and requiring equality would force a regeneration of every key on every bump.
`source_manifest_sha256` is the SHA-256 of the selected artifact's `manifest.json` bytes;
`manifest_sha256` is `compact_identity_sha256` of the compact manifest the selection is embedded
in; `binding.frames` and `binding.derivatives` count its frames and distinct derivative files.

Rules: `coverage_sha == subject.commit`; `implementation.workflow_ref` is
`<repository>/.github/workflows/pages.yml@refs/heads/<implementation.branch>`; a handoff selection
names this key's handoff of its attempt, owned by `handoff_run` and its workflow, bound
`reencode-identical`, with `kit_binding.source == "workflow_file"`; a cache selection names this
key's cache of `coverage_sha`, owned by an earlier `.github/workflows/pages.yml` run (never this
one), bound `cache-revalidated`, with `kit_binding.source == "referenced_workflows"`; `reuse:
"none"` means the tested run is the handoff run (the handoff rule above, plus equal `event`,
`created_at`, `head_sha` and `display_title`); `attested`/`delegated` name a distinct tested run;
unless `delegated`, `tested_run.head_sha == tested_run.controller_sha`; `attestation_job` exists
exactly for `attested`, which also requires `display_title` on both run records (SPEC §3.2 rule 5);
a handoff run whose `controller_sha` differs from `subject.commit` carries `display_title` (SPEC
§4.8 step 3); `derivatives <= frames`; a composition names an `mb-baseline` of this key.

**Binding to the compact bundle** (`check_compact_selection(compact, selection)`, applied by every
writer and reader of a published compact): repository, key, `kit`, subject and coverage agree;
`expectation_sha256` is the embedded `expectation.json` hash; `manifest_sha256` is the manifest's
identity; `selected_artifact` is the manifest's `source_artifact`; the two run records carry exactly
`provenance.handoff`/`provenance.tested` and the same `reuse`; `extensions_verified` equals the
manifest's extension names; `binding` counts match; `composition` exists exactly for a `composed`
scope and names the same baseline artifact and selected identity as `scope.components`.

So the final selection of a **composed** bundle differs from its draft in two more fields than the
three completion fields: `expectation_sha256` becomes the hash of the composed bundle's complete
expectation (the draft named the selected handoff's), and `extensions_verified` becomes the composed
bundle's extension names (verified again through `authenticate_extensions` when any object differs
from the selected handoff's, R6). `compact`'s finalization accepts these two changes only when a
`composition` is recorded; every other field is the draft's, and a reader re-authenticating a
composed selection (build) must expect exactly these differences.

## The collect flow (SPEC §5.3 `collect`, frozen by MB0)

1. `admit` outputs `bundle_keys`, `subjects: {key: {branch, commit, tree}}` (the only key to
   subject mapping, needed because enrolled-branch keys are opaque) and `families: [{family, key,
   coverage_sha}]` with `coverage_sha == subjects[key].commit`.
2. `select --expected-subject-commit <subjects[key].commit> --output selected.json` writes the
   `Selected` object (`pages.select.SELECTED_KEYS`); `download` fetches that artifact by id.
3. `authenticate --selected-json selected.json --output selection.json` writes the **draft**.
4. A `selected` handoff only: `compose --selected DIR --selection selection.json --output DIR`
   compacts the handoff itself into an intermediate (`scope.kind: "selected"`, the draft
   embedded), runs the adapter's `compose` hook on it, applies R3, completes the selection
   (`binding`, `composition` with the authenticated baseline `owner_run_id`, `manifest_sha256`) and
   writes the final composed bundle. Step 5 is skipped for it. R3 includes the **full-use rule**:
   the `epoch: selected` frames are exactly the intermediate's frames, every selected lane is
   present and composed only from selected frames, every selected comparison is present and every
   other lane equals its baseline lane, so older baseline evidence can never stand in for what the
   selection re-tested. The hook's output must be canonical bytes and its complete expectation
   must pass R2 (both run projections) and its extensions R6.
5. Otherwise `compact --input DIR --selection selection.json --output DIR` re-encodes a `complete`
   handoff (or revalidates and re-emits a cache), completes the selection (`binding`,
   `manifest_sha256`; a composed cache keeps its `composition`) and embeds it. A `selected`
   handoff is refused here.
6. `validate --kind compact` in a fresh process (including `check_compact_selection`), the live
   head recheck, then the upload as `mb-collected--<key>`.

Families: `select --family --output selected.json` then `family collect --expected-coverage-sha
<families[].coverage_sha> --selected-json selected.json` writes, when available, `paired.json`,
`images/`, `source/` (the selected family handoff or cache verbatim) and `selected.json` (the
recorded selection, the exact canonical bytes `select` wrote) and is uploaded as
`mb-collected-family--<family>--<key>`. `build` downloads every collected artifact of its run itself
(into its new `--collected`/`--families` directories) and passes the adapter's `verify_publication` a
promotion **draft** (no `site`). Finalize's `refresh` downloads the run's `mb-promotion`
(`promotion.json`) and the promoted collected artifact itself and writes the exact upload bytes into
its new `--input` directory: the compact bundle for a key, the `source/` directory for a family leg
(its `selected.json` is checked, never uploaded).

**The family generation chain.** A family leg is `(family, key)` at the leg coverage `C`
(`admit`'s `families[].coverage_sha`, the key's subject commit):

1. `select --family` takes an `mb-family-handoff--<f>--<key>--a<attempt>` upload of a successful
   `producer.workflow` run on the default branch at `C`, unless an `mb-family-cache--<f>--<key>--<C>`
   owned by a successful Pages run **supersedes** it (that owner run was created after the handoff
   was uploaded, so that publication consumed the handoff and its rotation may retire it), else the
   newest such family cache. When neither exists and the family sets `carry_forward`, the same
   probes walk a bounded first-parent history below `C`, newest first (at most
   `limits.GENERATION_PROBES` commits and cache owners), and the first earlier generation found is
   selected; a nomination is re-authenticated exactly and never walks. A selected family handoff is
   then bound to its kit (SPEC §1.8): downloaded by id and digest, its envelope must name exactly the
   producer attempt that uploaded it and `envelope.kit` must be the pin of `producer.workflow` at that
   run's head, or the leg fails (reason `kit-binding`, never a fallback to another generation).
2. `family collect` binds the recorded selection to its input before anything else runs: canonical
   `Selected` bytes of a `family-handoff` or `family-cache` of this family and key; a handoff is its
   envelope producer attempt's upload (`run_id`, `run_attempt`); a cache is named by a commit its
   envelope's coverage precedes and that precedes `C`; and `envelope.kit` is the pin of
   `producer.workflow` at `producer.commit`, read from the inert object store (reason
   `family-selection`, `kit-binding` or `git`). Then it decides: the adapter's `family_validate`
   accepts (returning `carried_from` equal to the envelope's `coverage_sha` when that is not `C`) or
   refuses (`superseded`/`unavailable`, exit 3, no upload), and the core re-proves R5 (`carried_from`
   is an ancestor of `C`) plus the envelope's own `carried_from`, if any. Selection never decides
   that a carry is safe.
3. `refresh` (`refresh-family`) rolls exactly `source/` forward as
   `mb-family-cache--<f>--<key>--<C>`: the cache is **named by the leg coverage**, while its
   `envelope.json` stays byte-identical and keeps the **producer's** `coverage_sha` (an envelope
   always covers its producer's checkout). Rotation keeps such a carried cache as the leg's
   replacement even though its envelope coverage differs from its name.
4. A later Pages run that selects that cache for a newer `C'` collects it again: `family_validate`
   returns the envelope coverage as `carried_from`, and R5 is proven again up to `C'`. The chain
   never trusts an earlier collection: R4 and R5 hold at **every** collection.
5. `build` re-proves R4/R5 for every collected leg and checks the recorded selection by id, with no
   walk: the promotion's `selected_artifact_id` is the `selected.json` generation, which must be
   listed unexpired by its recorded owner run with its recorded name, digest and size; a family
   handoff must be the envelope's producer attempt's own upload, and a family cache must be owned by
   a successful earlier Pages run and named by a commit of `C`'s bounded first-parent history
   (`select.FamilyGenerations.history`) at or above the envelope's coverage. The cost is one lookup
   per leg however far below `C` the generation was produced, and, as for an ordinary bundle, a
   newer generation appearing after collection never invalidates the one this publication
   collected (the newest rule is not re-applied). `refresh-family` requires the recorded selection to
   be the promotion's `selected_artifact_id`.

**Command outputs** (`$GITHUB_OUTPUT` unless noted): `prepare` writes `anchor_eligible`;
`anchor identity` writes `anchor_eligible` and `anchor_name` (empty when not eligible) and prints
`canonical_json({"eligible": bool, "name": str | null})` on stdout; `select` writes the
`SELECTED_KEYS`; `family collect` writes `status` (`available`, `superseded` or `unavailable`) and
`available` (`true`/`false`) and, for an absence, exits 3 without an output directory; `build` writes
`heads` (one line of canonical JSON) and `site_sha256` (the published inventory hash); `refresh`
writes `available`, `cache_name` (empty when not available) and `baseline_name` only when a
baseline is retained: exactly for a new complete generation whose baseline no earlier successful
refresh retained ("Baselines" above). `conformance` prints its report on stdout (the nine keys
`repository`, `kit`, `keys`, `families`, `checks`, `variants`, `admission`, `hooks`, `site`); the
report, the variants and the optional fixture functions it calls are specified in
[ADAPTER.md](ADAPTER.md#the-conformance-report).

## `mod-base.promotion` (`mb-promotion`)

`{repository, implementation, kit, heads: {branch: SHA1} (1..64), bundles: [{key, collected_artifact_id,
collected_digest, manifest_sha256, coverage_sha, selected_artifact_id}], families: [{family, key,
available, status, collected_artifact_id?, collected_digest?, coverage_sha?, selected_artifact_id?}],
site: {files, bytes, inventory_sha256}}`

Rules: `heads[implementation.branch] == implementation.sha`; bundle keys unique; `(family, key)`
unique and every family key is a bundle key; `available == (status == "available")`; the four
collected fields are present exactly for available families, whose `coverage_sha` equals the
bundle's. `inventory_sha256` is `inventory_sha256([{path, sha256, size}])` of every published file
except `build.json`. `bundles[].manifest_sha256` is the SHA-256 of the collected `manifest.json`.

**Draft** (`validate_promotion(draft=True)`): the same document without `site`, which only exists
after rendering and sealing. `build` hands the draft to the adapter's `verify_publication` (SPEC
§5.3.2 step 8, before render) and writes the final promotion, with `site`, after sealing (step 10).
A draft carrying `site` is rejected, so no step can pass fabricated inventory numbers.

## `mod-base.build` (`_site/build.json`)

`{repository, implementation: {sha, run_id, run_attempt, run_url, workflow_ref}, kit: KitRef,
pixel_metrics_version: 1, site_inventory_sha256}` — `run_url` equals `run_url(repository, run_id)`
and `workflow_ref` names this repository's `.github/workflows/pages.yml`.

## `mod-base.site` (`_site/site-data.json`)

`{project: {name, tagline, eyebrow, description, license_label, repository_url, issues_url, icon:
"assets/icon.png"|null, links: [{id, title, description, url}]}, gallery_url: "e2e/", releases:
[{key, label, minecraft, loaders, loader_names, frame_count, lane_count, short_sha, subject_commit,
tested_run_url}], families: [{family, title, lane_count, available}], copy: {principles: [display],
evidence_lead}, generated: {implementation_sha, kit_sha, kit_version, pages_run_url}}` — URLs are
https without userinfo or port; `loader_names` is parallel to `loaders`; `short_sha` (7..12) is a
prefix of `subject_commit`; release keys and family ids are unique.

## `mod-base.gallery` (`_site/e2e/gallery-data.json`)

| Field | Type |
|---|---|
| `project` | `{name, repository_url, actions_url}` |
| `labels` | `{scenarios, roles, tiers, loaders: {id: display}, release_prefix, search_placeholder}` |
| `copy` | `{gallery_lead, methodology: [display], family_notes: {family: display}}` |
| `releases` | `[{key, label, minecraft, loaders, loader_names, frame_count, lane_count, scenarios, contract_sha256, contract_url, matrix_sha256, subject, coverage_sha, short_sha, handoff: {run_id, run_url, created_at}, tested: {run_id, run_url, created_at, commit}, scope: "complete"\|"composed", reuse}]`, 1..64 |
| `lanes` | `[{lane_id, key, artifact_node, minecraft, loader, loader_name, scenario, roles, status: "pass", elapsed_s?, jars, epoch?}]` |
| `frames` | `[{frame_id, key, capture_id, capture_order, title, expectation, runtime_evidence, review_tier, lane_id, artifact_node, minecraft, loader, loader_name, scenario, role, step, image, width, height, alt, source: {width, height, file_sha256, pixel}, published: {file_sha256, format: "webp", pixel}, provenance: {handoff_run_url, handoff_commit, tested_run_url, tested_commit, tested_created_at, coverage_sha}, epoch?}]`, at least one |
| `comparisons` | `[{comparison_id, key, lane_id, role, first_frame_id, second_frame_id, source: CompareMetrics, published: CompareMetrics}]` |
| `families` | `[{family, title, description, available, status, releases: [FamilyRelease], lanes: [paired lane + key], not_applicable: [entry + key]}]`, one entry per family; `[]` is valid |
| `FamilyRelease` | `{key, available, status: "available"\|"superseded"\|"unavailable", coverage_sha?, contracts?: {name: SHA256}, links?: [{label (display <= 40, unique), run_url}] (<= 16), image_policy?: {derivative_box, webp_quality, webp_method}}`, one per key, the four optional fields exactly when `available` |
| `build` | `{implementation_sha, kit_sha, kit_version, pixel_metrics_version: 1}` |

Rules (the front-end data gate plus consistency): `project.repository_url` is exactly
`https://github.com/<repository>` and `actions_url` one of its workflow pages; every run URL is a
run of that repository (release `handoff`/`tested` URLs are exactly `run_url(repository, run_id)`);
lanes unique per `(key, lane_id)` and owned by a release; frames unique per `(key, frame_id)`,
consistent with their lane, `image == images/<key>/<published.file_sha256>.webp` and
`width/height` equal to the published metrics; `frame_count`/`lane_count` match; comparisons
reference same-key, same-lane, same-role frames; `family_notes` name listed families.

**Families (announced amendment of SPEC §3.9).** The SPEC's family entry `{family, title,
description, available, status, lanes, not_applicable}` has one status for all keys, but
promotions and family artifacts are per `(family, key)` and Quick Skin publishes 17 keys, each with
its own coverage, contract hashes and provenance runs (its current gallery keeps them per release,
SPEC §6.3). So a family entry stays one view per family (one `family-view-<id>` button) and adds
`releases`, one `FamilyRelease` per key of `promotion.families` for that family: every release key
is a gallery release key; an available key carries `coverage_sha` (equal to that release's
`coverage_sha`, SPEC §5.3.2 step 7), `contracts`, its provenance `links` and `image_policy`; an
unavailable or superseded key carries none of them. The entry's `available` is true when any key
is available and its `status` is `available`, else `superseded` when every listed key is
superseded, else `unavailable` (including a family with no key yet). Every family lane and
not-applicable entry carries the `key` it belongs to, which must be an available release key;
lane ids, `(artifact_node, variant)` pairs and not-applicable entries are unique per key; family
images live at `families/<family>/images/<sha256>.webp`, sized by their key's `image_policy`, and
every pair is publishable.

## `mod-base.template-manifest` (`template/manifest.json`)

`{files: [{path: repo path, class: "managed"|"fragment"|"seeded", source: bundle path, markers?:
[text <= 200], lines?: [text <= 200]}]}` — unique paths; managed sources live under `managed/`;
fragment and seeded sources under `seed/`; only fragments may list required `markers`/`lines`.

## `mod-base.kit-stamp` (`out/mod-base-kit/MOD_BASE_KIT.json`)

`{sha: SHA1, version: VERSION, tree_digest: DIGEST}` — the staged kit's pin and its kit-digest-v1.

## `mod-base.config` (`site/mod-base.json`, SPEC §4.1)

Validated by `mod_base.config.validate_config` (structure) and `load_config` (repository facts).

| Field | Rule |
|---|---|
| `project.name` / `tagline` / `eyebrow` / `license_label` | display text, <= 60 / 120 / 60 / 60 |
| `project.description` | display text <= 400, or `{"from_matrix": "a.b"}` (dotted lowercase path) |
| `project.icon` | `null` or `{path: repo path ending .png, rendering: "pixelated"\|"auto"}`; the file is a PNG of at most 512 KiB and 1024x1024 |
| `project.links` | <= 8 `{id: [a-z0-9][a-z0-9-]{0,31}, title <= 40, description <= 160, url}`, unique ids, display order |
| `canonical_branch` | `BRANCH` (must equal the API default branch at runtime) |
| `adapter` | `{path: scripts/pages/*.py, api: in ADAPTER_API_WINDOW, python_path: 1..8 unique repo directories (or "."), network_hooks: subset of {authenticate_extensions, compose, verify_publication}, extensions: unique names, timeout_seconds?: 1..1800 (default 600), fixtures_path?: scripts/pages/*.py != path}` |
| `targets` | `{mode: "default-branch", max: 1..64}` or `{mode: "enrolled-branches", max: 1..64, max_branches: 1..100}` |
| `source` | `{workflow: WORKFLOW_PATH, events: {canonical: [EVENT] (1..8), other: [EVENT]}, display_title: null or text with exactly one {subject_commit}, attestation_job: null or text without placeholders, delegated_reuse_extension: null or a declared extension, require_job_graph, require_newest_run, handoff_job: text with at most one {key}, handoff_step: text}`; job texts never contain other braces or `${{` |
| `images` | `{source_size, derivative_box, webp_quality: 1..100, webp_method: 0..6, pixel_metrics_version: 1, cross_check_runtime_metrics}`; `source_size` within 20 M pixels |
| `anchor` | `{enabled, retention_days: 1..90, successor_grace_days: 0..retention_days}` |
| `admission` | `{mode: "progress", coalesce_seconds <= partial_deadline_seconds, recovery_interval_seconds: 60.., max_failed_publications: 1..10, defer_on_active_source_runs}` or `{mode: "always", defer_on_active_source_runs}` |
| `baseline_archive` | `{enabled: true, retention_days: 1..90}` or `{enabled: false}` |
| `families` | <= 8 unique `{id: FAMILY, title, description, producer: {workflow != source.workflow, events (1..8), job, step}, handoff_max_bytes: 1..MAX_FAMILY_HANDOFF_BYTES (784,269,312), retention_days: 1..7, image_policy, carry_forward}` |
| `labels` | `{scenarios, roles, tiers, loaders: {id: display <= 80}, release_prefix <= 40, search_placeholder <= 80}` |
| `copy` | `{gallery_lead <= 1200, evidence_lead <= 1200, methodology: <= 16 x 1500, principles: <= 16 x 400, family_notes: {configured family: <= 500}}` |
| `theme` | `{dark: Theme, light: Theme\|null}`; a Theme has exactly `bg, surface, surface_raised, surface_soft, text, muted, line, accent, accent_strong, highlight, danger, image_well`, each `^#[0-9a-f]{6}$` |
| `template` | `{agents_local: [repo paths ending .md], deferred: [repo paths]}`, unique, <= 32 |

Repository facts (skipped only by rotation's data-only load): the adapter, fixtures module, source
and family producer workflows exist as regular files, every `python_path` entry is a real
directory, no checked path crosses a symlink, and the icon rules hold.
