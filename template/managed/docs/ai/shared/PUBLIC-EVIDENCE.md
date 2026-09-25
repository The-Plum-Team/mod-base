# Shared public-evidence contract

This file is managed by mod-base (https://github.com/The-Plum-Team/mod-base) and byte-identical in
every mod that pins the kit; `template check` fails on any local edit (see
[REPOSITORY.md](REPOSITORY.md)). It holds the invariants of the mod-base v1 public-evidence
pipeline and GitHub Pages publication. The mod's local documents own its scenarios, captures,
review semantics and adapter; they may add stricter rules, never weaker ones. The normative
schemas, adapter protocol and operations are in the kit's documentation:
https://github.com/The-Plum-Team/mod-base/tree/main/docs

## Flow and ownership

- The mod's packaged E2E produces the evidence. Its `prepare-evidence` step (a kit composite)
  runs the adapter's `collect` over the packaged output and uploads the private handoff
  `mb-handoff--<key>--a<attempt>` (and, when eligible, the lossless anchor). A separate job
  holding only `actions: write` then wakes the publisher with `notify-pages`.
- The managed caller `.github/workflows/pages.yml` publishes: `verify-kit` binds the executing kit,
  the kit's `publish` workflow admits, collects, validates families and builds the site, the
  caller-owned `deploy` job deploys it, `finalize` rolls the caches forward, and
  `request-rotation` starts a separately locked rotation run. The only wakes are
  `workflow_dispatch` (`deploy`, `family`, `rotate`, `manual`) and the hourly schedule; `pages.yml`
  never uses `workflow_run`, `repository_dispatch` or `pull_request_target`.
- A wake is a hint, not evidence: admission re-authenticates everything from scratch and a stale
  or foreign wake exits without publishing.
- Publication runs share the caller-owned concurrency group `mod-base-pages-publication` (one
  running, one pending, never cancelled); rotation runs use `mod-base-pages-rotation`. Every job of
  one Pages run checks out the same `github.sha`, and that commit must still be the live default
  head; a default branch that advances affects only a later run.
- The mod's scenario contract is the only authored source of scenarios, lanes, captures,
  expectations, review tiers and comparisons. The kit sees it only through the adapter's
  `expectation` hook; every consumer view is derived from that expectation. Never create a second
  catalog or a workflow-side scenario list.

## Two-part implementation identity

- Only code fixed by the protected mod commit may validate or render public evidence: the adapter
  and `site/mod-base.json` at `github.sha`, plus the mod-base commit that `github.sha` pins and that
  is reachable from mod-base `main`.
- `verify-kit` (caller-owned shell, no kit code) takes the kit SHA from the run's
  `referenced_workflows`, requires it to equal every pin in `pages.yml` at `github.sha`, and
  requires it to be reachable from mod-base `main`. Every kit job then checks out both commits
  cleanly, compares the kit tree digest with the literal compiled into the kit workflow, and
  refuses any `GIT_*` environment variable.
- Every manifest records the implementation (repository, commit, run, attempt, workflow ref) and
  the kit (repository, SHA, version). Consumers bind a recorded kit SHA to the authenticated owner
  of the artifact (the owning Pages run's `referenced_workflows`, or the pin read from the
  producer workflow at the producer commit), never to the current pin, so a kit bump does not
  invalidate existing evidence. `_site/build.json` publishes both identities.

## Frame validity and provenance

- A public screenshot is valid only when an authenticated, successful packaged result references
  it and its recorded SHA-256, byte size and dimensions match the PNG exactly. Never infer a
  scenario, role, step or capture from a filename or an ordinal, and never let sets or duplicate
  labels collapse two frames into false coverage.
- A bundle's frames, lanes and comparisons equal its expectation exactly: a missing, extra,
  duplicated or reordered capture rejects it, and so does a screenshot that no capture declares.
  Every bundle carries the exact contract and release-inventory SHA-256 of its expectation, every
  lane passed, and each lane records exactly one production JAR digest.
- Every published frame carries its contract identity (frame, capture, capture order, lane, role,
  step) and expectation; the mandatory `runtime_evidence`, the bounded message its passed assertion
  emitted (1 to 4096 characters, non-empty after trimming, no character below 32 and no DEL); the
  decoded pixel metrics of both the source PNG and the served derivative; its required
  comparisons; its lane with the exact production JAR digest; and the provenance of the handoff and
  tested runs, including the reuse mode.
- Pixel metrics and comparisons are recomputed by the kit's single implementation from the decoded
  bytes. A comparison below its minimum changed fraction rejects the bundle. When the configuration
  asks for it, the kit's metrics must also equal those the mod's runtime reported.
- The adapter's `collect` runs at the producer and again at the collector over the same bytes; the
  two results must be identical.
- Ordinary evidence proves exactly the commit and run that produced it: its coverage equals its
  subject commit and it is never carried forward. A newer head waits for newer evidence.

## Schemas and evolution

- Every document is strict UTF-8 JSON of a `mod-base.*` kind with a `schema_version`: no duplicate
  keys, no NaN or Infinity, no unknown keys, exact types. Written documents are canonical JSON.
- A kit release N reads schema versions N and N-1 and writes N. Within one schema version only
  optional fields may be added; a new field is validated whenever it is present. `ADAPTER_API`
  follows the same N/N-1 rule.
- Generations use only `mb-*` artifact names built by the kit's grammar. There are no converters:
  legacy artifacts are never read and never deleted, and they expire under their own retention.
- `pixel_metrics_version` must equal the kit's current version. A metric algorithm change bumps it
  and fails closed until the evidence is regenerated.

| Artifact | Retention |
|---|---|
| `mb-handoff--<key>--a<attempt>` | 1 day |
| `mb-anchor--<key>--<commit>--<run_id>--a<attempt>` | `anchor.retention_days` (at most 90) |
| `mb-family-handoff--<family>--<key>--a<attempt>` | the family's `retention_days` (at most 7) |
| `mb-collected--<key>`, `mb-collected-family--<family>--<key>`, `mb-promotion`, `github-pages` | 1 day |
| `mb-cache--<key>--<coverage_sha>`, `mb-family-cache--<family>--<key>--<coverage_sha>` | 90 days |
| `mb-baseline--<key>--<commit>--<tested_run_id>` | `baseline_archive.retention_days` (at most 90) |

## Untrusted artifacts

- Every artifact, JSON document, image and API response is hostile until the kit has validated
  it. Artifacts are downloaded only by immutable numeric id, with their name grammar, digest, size,
  owner run and attempt, expiry and upload window checked first.
- Archives are extracted with bounded entry counts, sizes and compression ratio; stored or deflated
  entries only; no symlink, special, encrypted, absolute or traversing entry. The extracted tree
  must equal the manifest's exact file inventory.
- Every image is fully decoded within the pixel limit and must have the exact configured size
  before any metric is trusted. Derivatives are re-encoded deterministically and must be
  byte-identical to the collector's own encoding.
- Source runs are authenticated through their exact attempt: workflow path, event set, head
  commit, repository, conclusion, and, when configured, display title, attestation job (exact name
  match), exact job graph and the newest-run rule. Live head rechecks bracket admission,
  collection, rendering and deployment.
- Other branches and historical commits are fetched only as inert Git objects and read through
  bounded blob reads; they are never checked out or executed in a privileged job.
- API failures are never treated as absent evidence. Retryable failures get bounded backoff;
  anything else fails the job visibly without publishing.

## Adapter isolation

- The adapter is trusted code at the protected head; its isolation is defense in depth. It runs
  only in the read-only jobs `admit`, `collect`, `family` and `build` and in `prepare-evidence`,
  never in `verify-kit`, `deploy`, `finalize`, `request-rotation`, `rotate`, `notify-pages` or any
  job holding `actions: write`, `pages: write` or `id-token: write`. Rotation reads the config as
  data only.
- Each hook runs in an `env -i` child process with a fixed argument list, a fixed hook name, a
  timeout and a bounded response validated against a strict schema. Only hooks declared as network
  hooks receive a read-only token, and only in those read-only jobs.
- The kit re-verifies every hook result before a byte is published: equal `collect` results,
  byte-equal re-derived expectations, composed bundles checked against their authenticated
  baseline and the complete expectation, family projections and images re-inspected, carry-forward
  ancestry proven by the kit itself, and every declared extension verified. A hook the adapter does
  not define is unsupported, and the kit fails closed wherever it would be required.

## Publication, deployment and rotation

- Admission is advisory cost control (`site/mod-base.json` `admission`): it may coalesce, defer or
  decline a run, but it never admits bytes that collection has not authenticated. An ineligible
  admission ends the run green without deploying; `operation=manual` is the operator's recovery.
- Publication is atomic and advisory. The site build runs only when every collection leg
  succeeded; any failure skips the build and the deployment and leaves the deployed site
  unchanged. Before rendering, the build re-derives every expectation, re-authenticates every
  selected artifact by id and re-inspects every derivative. The rendered output is sealed and copies
  only an allowlisted static inventory.
- Only the caller's `deploy` job holds `pages: write` and `id-token: write`. It checks out nothing
  and rechecks every published branch head immediately before deploying; a moved head keeps the
  previous site.
- Caches are rolled forward only from bundles the successful run actually promoted. A baseline
  archive is kept only for complete-scope evidence.
- Rotation is a separate run that starts only after the owning Pages run is authenticated as
  `completed/success`, under its own lock. It deletes by exact artifact id after re-observing each
  artifact, within a bounded budget, and defers the rest. Besides `mb-*` artifacts it deletes only
  the owning run's own `github-pages` artifact; it never deletes any other name, anything newer
  than its owner, the newest anchor of a key, or a baseline archive.
- The lossless anchor stores canonical metadata-free PNGs. Only a direct canonical run is
  eligible; the newest authenticated anchor per key is kept and older ones retire after the
  configured successor grace.
- Disabling `pages.yml` is the kill switch: the deployed site stays online and producers keep
  working. Pages never weakens or replaces the repository's required build and E2E gates.

## Families

- A family handoff wraps the mod's native bundle in a `mod-base.family.envelope` with an exact file
  inventory. Only the adapter understands the native bundle; the kit validates the envelope, the
  generic paired projection and every image, and publishes only pairs whose verdict is
  runtime-passed, semantically valid and defect-free.
- `superseded` (contract drift) and `unavailable` omit the family for that key; they are not
  failures. Any malformed bundle under the current contract remains fatal. Carry-forward to a newer
  commit requires the kit's own proof that the covered commit is an ancestor.

## Derivatives and the public site

- Optimized WebP derivatives are not the proof. Source and derivative hashes, dimensions and
  metrics are published separately, and every public image path is content-addressed by the bytes
  actually served. Original PNGs exist only in the one-day handoffs and in the lossless anchor.
- Every run URL on the site is built by the renderer from the repository and a numeric run id,
  never copied from a bundle.
- Pages carry the meta Content Security Policy `default-src 'none'; script-src 'self'; style-src
  'self'; img-src 'self'; connect-src 'self'; font-src 'none'; object-src 'none'; base-uri 'none';
  form-action 'none'` and `referrer` `no-referrer`. Scripts render with `textContent` only: no
  `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, inline
  handlers or inline scripts and styles, and only local assets. `frame-ancestors` cannot be set
  from a meta tag and GitHub Pages sets no headers; that limitation is accepted.

## Secret-bearing visual review

When a mod runs AI visual review over its evidence:

- The boundary is `authenticate -> curate without secrets -> durable queue -> secretless
  preparation -> review in a fresh capsule -> retention-safe cleanup`. Raw artifacts never enter a
  credential-bearing job.
- Curation authenticates every source artifact by numeric id, size, digest, run, release-inventory
  row, complete scenario product and single JAR digest, imports source commits only as inert Git
  objects, fully decodes every image, requires the exact contracted dimensions and re-encodes
  canonical metadata-free PNGs without resizing.
- A reference comes only from authenticated lossless evidence: the kit's `mb-anchor--` artifact of
  the declared reference lanes, or the same run's authenticated raw packaged captures, as the
  mod's local documents define. It is paired by semantic capture id, never taken from a lossy
  derivative, a filename, an ordinal or a latest-version baseline.
- The model sees only bounded manifests and curated images through read-only tools. Its verdicts
  are validated against a protected schema and normalized before upload; raw provider text is
  never uploaded, the first confirmed defect cancels outstanding work and fails closed, and the
  live default-branch head is rechecked immediately before model admission.
