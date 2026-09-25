# Adapter interface (`ADAPTER_API = 1`)

A mod connects to the kit with two protected files (both under Block Pops protected roots):

* **data:** `site/mod-base.json` (`mod-base.config` v1, see [SCHEMAS.md](SCHEMAS.md#mod-baseconfig-sitemod-basejson-spec-41));
* **code:** `scripts/pages/mod_base_adapter.py` (`config.adapter.path`), which defines
  `ADAPTER_API = 1` and the hooks below.

The adapter is trusted code at the protected mod head, like every Pages script today. The
isolation below is defence in depth against accidental token use, leaks and non-determinism, not a
sandbox against a malicious adapter. The kit re-verifies every hook result (R1-R6) before any byte
is published. The normative protocol is `mod_base.adapter.protocol`.

## Hooks

Every argument and result is JSON-compatible and validated against a strict schema: unknown keys,
wrong types and out-of-bounds values fail closed. A hook that the module does not define is
**unsupported**: the host raises `HookUnsupported`, and the core fails closed wherever the hook is
required. A hook that raises becomes `HookFailed` (exit 2) with a bounded one-line message. A hook
is called as `hook(ctx, **arguments)`: its parameter names are exactly the argument names below.

| Hook | Signature | Result | Required |
|---|---|---|---|
| `targets` | `targets(ctx, branches)` | 1..`targets.max` `{key, label, subject: {branch, commit, tree}, matrix_sha256, contract_sha256}`, unique keys | always |
| `expectation` | `expectation(ctx, target, tested_run, extensions)` | `mod-base.evidence.expectation` whose key, subject and hashes equal `target` | always |
| `collect` | `collect(ctx, runtime_root, target, expectation)` | `{runtime_files, lanes, frames, comparisons}` (below) | always |
| `expected_source_jobs` | `expected_source_jobs(ctx, expectation, tested_run)` | `null` or `[{name, conclusion}]` (unique names) | when `source.require_job_graph` |
| `authenticate_extensions` | `authenticate_extensions(ctx, manifest, extensions)` | `{verified: [names] (sorted), reuse_verified: bool}`; `verified` names exactly the supplied extensions | when a manifest carries extensions |
| `compose` | `compose(ctx, key, selected_compact_dir, output_dir)` | `{baseline_artifact: {id, name: mb-baseline--<key>--..., digest}}` | for every `selected` bundle |
| `verify_publication` | `verify_publication(ctx, promotion_draft)` | `null` (raise to veto) | optional |
| `family_validate` | `family_validate(ctx, family, key, bundle_dir, expected_coverage_sha, output_dir)` | `{status: available\|superseded\|unavailable, reason (<= 200), projection_path?, carried_from?, impact_paths_sha256?}` | for every configured family |
| `anchor_selection` | `anchor_selection(ctx, expectation)` | `null` (no anchor for this run) or exactly the expectation's declared `anchor` (`{artifact_nodes: [...]}`, sorted, unique) | when `anchor.enabled` (absent means no anchor) |

Argument details:

* `targets`: `branches` is `null` in `default-branch` mode (every key's subject is the protected
  head, so every target must name the same commit) and `[{name, commit, tree}]` in
  `enrolled-branches` mode (at most `max_branches`, fetched as inert objects; every target subject
  must be one of them). The adapter decides enrollment by reading each branch's matrix through
  `ctx.read_blob`.
* `expectation`: `tested_run` is `null` or `{event, branch}` of the tested run (BP derives its
  `pr-anchors`/`scheduled-anchors` projection from it); `extensions` maps each declared extension
  name to its object from `extensions.json`.
* `collect`: `runtime_root` is an absolute path; the result is a pure function of its bytes and is
  computed twice (on the E2E output at prepare, on the handoff `runtime/` at collect) and must be
  equal (R1). It returns `runtime_files` (sorted relative `.json`/`.png` paths to copy, 1..4096),
  `lanes` (`[{lane_id, java, profile, status: "pass", elapsed_s?, jars: {production_sha256,
  harness_sha256?}}]`, exactly the expectation's lanes in order, same `java`), `frames`
  (`[{frame_id, source_path, runtime_evidence, reported_pixel?: PixelMetrics}]`, exactly the
  captures in order, `source_path` a listed `.png`) and `comparisons` (`[{comparison_id,
  reported?: CompareMetrics}]`, exactly the expectation's comparisons in order).
* `expected_source_jobs`: `tested_run` is the tested run's `RunRecord`.
* `compose`: `selected_compact_dir` is the core's own compaction of the selected handoff, a
  `mod-base.evidence.compact` with `scope.kind: "selected"` (`validate_compact(intermediate=True)`),
  its selected expectation and the selection draft embedded; the hook writes a `composed` bundle
  into `output_dir`, which the core re-verifies (R3) and re-emits with the final selection (see
  `SCHEMAS.md`, "The collect flow"). The composed bundle embeds the **complete** expectation.
* `verify_publication`: `promotion_draft` is the promotion without `site`
  (`documents.validate_promotion(draft=True)`): `build` calls the hook before it renders and seals
  the site (SPEC §5.3.2 step 8), so no site inventory exists yet; a draft carrying `site` is
  rejected.
* `anchor_selection`: a non-null result must equal `expectation.anchor`, so every anchor validates
  against its embedded expectation (`validate_anchor(expectation=...)` requires the declared anchor).
* `family_validate`: `projection_path` is a relative `.json` path inside `output_dir` (the
  `mod-base.family.paired` projection, with `images/` beside it), present exactly when
  `status == "available"`; `carried_from` (never equal to `expected_coverage_sha`) and
  `impact_paths_sha256` only when available. `superseded` means contract drift (exit 3, the family
  shows as unavailable); `unavailable` covers lineage or impact refusals.

Test-only hook, in `config.adapter.fixtures_path` and **never** loaded by the Pages host:
`synthesize(ctx, target, expectation, out_root, image_factory) -> None` writes a synthetic
packaged-output tree in the mod's own format under the absolute `out_root`. Its arguments are
validated by `protocol.validate_fixture_arguments` (`target` and `expectation` as for
`expectation`/`collect`) and it must return `None`. `image_factory` is a
`protocol.ImageFactory`, `image_factory(width, height, seed) -> bytes`: deterministic RGB PNG bytes
(the same arguments always give the same bytes, at least 8x4) that pass the 8-metric blank checks;
conformance passes `mod_base.imaging.png.pattern_png`. Fixture modules need no `ADAPTER_API`.

## Conformance (in-process, no `sys.path` edits)

`conformance` must drive every hook, network hooks included, against a `FakeGitHub`, which cannot
cross a process boundary, so it runs hooks in-process through the same dispatch the isolated child
uses:

* `mod_base conformance` re-executes its simulation as `python3 -P -m mod_base.conformance.run` in a
  child whose `PYTHONPATH` is `host.adapter_pythonpath(invocation)` (the kit `src`, then each
  `config.adapter.python_path` entry inside the repository) and which holds no GitHub credentials.
  That is an environment value, exactly like the hook child's; nothing in the kit edits `sys.path`.
* The child loads the adapter and the fixtures module with `host_child.load_adapter` and calls
  `host_child.run_hook(context, module, hook, arguments, image_factory=...)`, where `context` is an
  `adapter.api.Context` whose `api` is the seeded `github.fake.FakeGitHub` (duck-typed as a
  read-only `GitHubApi`). `run_hook` validates arguments and results exactly as in production.

## The `ctx` object (`mod_base.adapter.api.Context`)

| Member | Meaning |
|---|---|
| `repo_root` | the mod checkout (at `GITHUB_SHA` in Pages jobs) |
| `config` | the validated `mod_base.config.Config` |
| `tmpdir` | a private per-call temporary directory |
| `implementation_sha` | `GITHUB_SHA` in Pages jobs; the `--subject-commit` in `prepare` |
| `read_blob(commit, path, max_bytes)` | bounded `git cat-file` of objects already fetched as inert objects; never a checkout |
| `runtime_tree(root)` | a bounded, regular-file-only, symlink-refusing `RuntimeTree` view |
| `image_metrics(path, size_policy)` | the kit's PixelMetrics (`imaging.metrics.inspect_png`) |
| `api` | a read-only `GitHubApi`, present only for a declared network hook in a token job; otherwise `None` |

## Where hooks run (SPEC §4.3)

| Hook | Jobs |
|---|---|
| `targets` | `publish/admit`, `publish/collect`, `publish/build`, `prepare-evidence` |
| `expectation` | `prepare-evidence`, `admit`, `collect`, `family`, `build` |
| `collect` | `prepare-evidence`, `collect` |
| `expected_source_jobs` | `collect`, `build` (the core does the API reads) |
| `authenticate_extensions` | `collect`, `build` (network) |
| `compose` | `collect` (network) |
| `verify_publication` | `build` (network) |
| `family_validate` | `family` |
| `anchor_selection` | `prepare-evidence` |

No hook ever runs in `verify-kit`, `deploy`, `finalize`/`refresh`/`refresh-family`,
`request-rotation`, `rotate` or `notify-pages`, nor in any job holding `actions: write`,
`pages: write` or `id-token: write` (`protocol.FORBIDDEN_JOBS`). Rotation reads config data only.

## Isolation (`mod_base.adapter.host`)

`host.call(invocation, hook, arguments, network=...)` writes a request envelope and runs exactly:

```
env -i PATH=/usr/bin:/bin:<dirname(python3)> HOME=<tmp>/home TMPDIR=<tmp> LANG=C.UTF-8 PYTHONHASHSEED=0 \
       PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
       PYTHONPATH=<kit>/src:<each config.adapter.python_path entry, inside the repo, no symlink components> \
       [GH_TOKEN GITHUB_API_URL GITHUB_REPOSITORY   <- only if hook in config.adapter.network_hooks
                                                       AND $GITHUB_JOB in {admit, collect, family, build}]
  python3 -P -m mod_base.adapter.host_child --adapter <repo>/<config.adapter.path> --hook <name> \
          --request <tmp>/req.json --response <tmp>/resp.json
```

* The argv is fixed (`protocol.CHILD_FLAGS`); hook names come only from `protocol.HOOKS`.
* The child loads the adapter with `importlib.util.spec_from_file_location`, requires
  `ADAPTER_API` in `protocol.ADAPTER_API_WINDOW` (`{1}`; N and N-1 from v2 on), calls the hook and
  writes a canonical response.
* Timeout: `config.adapter.timeout_seconds` (default 600, at most 1800). The response is read to at
  most 16 MiB through strict JSON and validated.

Request envelope (`mod-base.adapter.request` v1):
`{kind, schema_version: 1, api: 1, hook, context: {repo_root, config_path, kit_src, tmpdir,
implementation_sha, network}, arguments}` — absolute normalized paths; `network` may be true only
for a network hook.

Response envelope (`mod-base.adapter.response` v1):
`{kind, schema_version: 1, hook, status: "ok"|"unsupported"|"error", result?, error?}` — `result`
exactly when `ok`, `error` (one line, <= 1000 characters) exactly when `error`.

## Generic re-verification (SPEC §4.4)

* **R1 `collect`:** byte-equal at prepare and collect; every `source_path` inside
  `runtime_files`; kit-computed pixels equal `reported_pixel` whenever
  `images.cross_check_runtime_metrics`; lane, frame and comparison ids equal the expectation.
* **R2 `expectation`:** byte-equal when re-derived at collect and at build and equal to the embedded
  `expectation.json`.
* **R3 `compose`:** the core downloads the named baseline itself by id and authenticates its owner
  (a successful `pages.yml` run on the default branch whose attempt's `Finalize / Refresh evidence
  cache for {key}` job succeeded and uploaded it inside the "Retain the complete compact generation
  for feature evidence reuse" step window), validates it as a compact bundle, and requires every
  `baseline` frame to equal the baseline, every `selected` frame to equal the core's own
  compaction, the composed frames to equal the complete expectation, and every `tested` record to
  equal its source.
* **R4 `family_validate`:** envelope inventory, then the projection schema, then every image
  re-inspected (dimensions equal `thumbnail(recorded source, derivative_box)`, pixels match), no
  unclean pair, `coverage_sha == expected_coverage_sha`.
* **R5 carry-forward:** with `carried_from`, the core fetches both commits as inert objects and
  requires `git merge-base --is-ancestor carried_from expected_coverage_sha`.
* **R6 `authenticate_extensions`:** every extension named in the manifest is verified; for
  `reuse: "delegated"`, `reuse_verified` is true.
