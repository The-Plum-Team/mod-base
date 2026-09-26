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
| `anchor_selection` | `anchor_selection(ctx, expectation)` | `null` (no anchor for this run) or exactly the expectation's declared `anchor` (`{artifact_nodes: [...]}`, sorted, unique) | optional: an adapter without it declines every anchor |

Argument details:

* `targets`: `branches` is `null` in `default-branch` mode (every key's subject is the protected
  head, so every target must name the same commit) and `[{name, commit, tree}]` in
  `enrolled-branches` mode (at most `max_branches`, fetched as inert objects; every target subject
  must be one of them). The adapter decides enrollment by reading each branch's matrix through
  `ctx.read_blob`. To find one key's target (`evidence.expectation.target_for_key`) in
  `enrolled-branches` mode, the core passes exactly the one subject branch; an adapter that does
  not enroll it returns `[]`, which the protocol's `1..targets.max` rule rejects, and the core
  reports that rejection as `Unavailable` (exit 3, no admissible evidence), like a missing key or a
  target naming another subject (in either mode). Every other `targets` failure, including an empty
  result in `default-branch` mode, is a rejection (exit 2).
* `expectation`: `tested_run` is `null` or the projection `{event, branch}`
  (`evidence.expectation.tested_run_projection`; BP derives its `pr-anchors`/`scheduled-anchors`
  profile from it). `branch` is the tested claim's branch. `event` is the **handoff** run's event:
  the producer holds no token, so at `prepare` the only event it knows is its own
  (`GITHUB_EVENT_NAME`), and every later re-derivation uses the authenticated `handoff_run.event`
  the same way. For `reuse: "none"` that is the tested run's own event. When an authenticated
  tested run was started by another event (`attested`/`delegated` reuse), `compact`, `compose` and
  `build` also derive with the tested run's own event and require identical bytes
  (`require_rederived_for_runs`, reason `expectation-drift`), so evidence is never published under
  another run's projection. `extensions` maps each declared extension name to its object from
  `extensions.json`.
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
  into `output_dir`, which the core re-verifies (R3, including the full-use rule) and re-emits with
  the final selection (see `SCHEMAS.md`, "The collect flow"). It composes **per frame**: a frame
  the selection re-captured comes from the selected compaction, every other frame from the
  baseline, each with its `epoch` and `tested` run (that epoch's run record plus its lane's
  production JAR); a lane the selection did not re-test is the baseline's lane; a re-tested lane is
  the selected compaction's lane record, plus `baseline_run` (the baseline lane's `profile`,
  `status`, `elapsed_s` and `jars`) when some of its frames were not re-captured, so a selection must
  run every role of a lane it re-tests (Quick Skin's selections retain every authored role); a
  comparison comes from the generation that holds both its frames (a selection must capture both
  partners). See `SCHEMAS.md`, "Composed lanes and epochs". The composed bundle embeds the
  **complete** expectation, exactly what the adapter re-derives from the composed bundle's own
  extensions (R2 with both run projections, above); it is written as the kit writes bundles
  (every JSON file its `canonical_json` bytes, `images/<sha256>.webp`). An extension object that
  differs from what `authenticate` verified for the selected handoff goes through
  `authenticate_extensions` again (R6).
* `verify_publication`: `promotion_draft` is the promotion without `site`
  (`documents.validate_promotion(draft=True)`): `build` calls the hook before it renders and seals
  the site (SPEC §5.3.2 step 8), so no site inventory exists yet; a draft carrying `site` is
  rejected.
* `anchor_selection`: a non-null result must equal `expectation.anchor`, so every anchor validates
  against its embedded expectation (`validate_anchor(expectation=...)` requires the declared anchor).
  The core asks only when an anchor is possible at all (`evidence.anchor.eligible_nodes`):
  `config.anchor.enabled`, the handoff was a direct canonical run of its subject
  (`handoff.controller_sha == subject.commit` and `handoff.branch == subject.branch`) and its
  pixels were not re-published by attestation (`reuse != "attested"`; `delegated` reuse stays
  eligible and the anchor records the tested run its pixels came from). Without the hook, or with a
  `null` result, `prepare` outputs `anchor_eligible=false`, `anchor identity` reports
  `{"eligible": false, "name": null}` and `anchor create` refuses (reason `anchor-ineligible`);
  `anchor validate` re-checks the direct-run shape (reason `anchor-source`).
* `family_validate`: `projection_path` is a relative `.json` path inside `output_dir` (the
  `mod-base.family.paired` projection, with `images/` beside it), present exactly when
  `status == "available"`; `carried_from` (never equal to `expected_coverage_sha`) and
  `impact_paths_sha256` only when available. `superseded` means contract drift (exit 3, the family
  shows as unavailable); `unavailable` covers lineage or impact refusals. `bundle_dir` is the
  selected family handoff or cache (`envelope.json` plus the native bundle). The hook must:
  * write into the fresh private `output_dir` exactly its projection and the images it references
    in `images/` beside it (`images/<sha256>.webp`), nothing else, with the images together at most
    `limits.MAX_FAMILY_PROJECTION_BYTES` (256 MiB, one compact bundle's derivatives): that is their
    share of the collected family artifact, beside the native bundle's `handoff_max_bytes`;
  * never modify `bundle_dir` (the core re-checks every byte it copies from it);
  * copy `subject` and the producer claim from `bundle_dir/envelope.json`: `projection.subject`
    equals `envelope.subject`, `provenance.producer` carries exactly the envelope's `producer`
    `RunClaim`, and its `event` is one of the family's configured `producer.events`;
  * make `provenance.producer` exactly the producer attempt's `RunRecord` as
    `github.runs.run_record` builds it from the run API (`event`, `created_at`, `conclusion`,
    `head_sha`, `display_title`), plus `job_graph_sha256` only when it is the graph of that
    attempt's jobs. The hook has no network, so it must take these facts from the native bundle
    (the producer can read its own run); `family collect` cannot check them, but `build` re-reads
    the producer attempt and refuses a leg whose record differs (reason `family-provenance`);
  * return `carried_from` equal to the envelope's `coverage_sha` (the producer's own checkout)
    exactly when that differs from `expected_coverage_sha`, and only for a family with
    `carry_forward`; without `carried_from` the envelope must already cover the expected commit;
  * record image pixel metrics type-exact with the kit's `inspect_webp` (the core compares the
    canonical JSON of the records, so `1` and `1.0` differ).

  The hook's `superseded` and `unavailable` are the only exit-3 absences; every R4/R5 failure and
  any tampering with `bundle_dir` is a rejection (exit 2).

Test-only hook, in `config.adapter.fixtures_path` and **never** loaded by the Pages host:
`synthesize(ctx, target, expectation, out_root, image_factory) -> None` writes a synthetic
packaged-output tree in the mod's own format under the absolute `out_root`. Its arguments are
validated by `protocol.validate_fixture_arguments` (`target` and `expectation` as for
`expectation`/`collect`) and it must return `None`. `image_factory` is a
`protocol.ImageFactory`, `image_factory(width, height, seed) -> bytes`: deterministic RGB PNG bytes
(the same arguments always give the same bytes, at least 8x4) that pass the 8-metric blank checks;
conformance passes `mod_base.imaging.png.pattern_png`. Fixture modules need no `ADAPTER_API`. The
same module may define the optional conformance fixtures `family_bundle`, `FAMILY_OUTCOMES`,
`delegated_extensions` and `selected_extensions` ([below](#optional-conformance-fixtures)); a mod
that configures a family and runs `conformance --families` must define `family_bundle`.

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
  read-only `GitHubApi`). `run_hook` validates arguments and results exactly as in production and
  reports an adapter's own exception as the same `HookFailed` (same message) the isolated host
  raises; `HookUnsupported` and an `MbError` that exits 2 pass unchanged.

`mod_base conformance --repo DIR [--keys a,b | --all] [--kit-root DIR] [--families]` (every key
without `--keys`) needs `config.adapter.fixtures_path`. It never runs in the mod's checkout: it
commits a copy of the mod into a private snapshot repository (with the kit's synthetic pinned
`source.workflow` and family `producer.workflow` files in place of the mod's own, exactly what the
simulated GitHub serves at every head) and simulates, for the selected keys, a first generation
(producer, admission, collect, build, refresh, a dry-run rotation) and then **later generations**
on the same simulated repository: a real rotation of the first generation, a push of a
documentation-only commit, a second generation at that new head (family legs carried forward from
the first generation's caches), dry-run rotations across the anchor successor grace and a
same-head publication interleaved with the second generation's real rotation. Every Pages job of
every generation must stay within its 160-read budget and change nothing, and every rotation plan
must equal an independent oracle's (cut only by the deletion budget). The stages are listed in
`mod_base.conformance._simulation` and `_generations`. Then come the variants below, each run only
when the configuration and the fixtures module make it applicable and otherwise reported as
skipped with its reason. A skipped variant never fails the run; every failed check exits 2 (reason
`conformance`).

**The documentation-commit contract.** Each later head adds exactly one new file,
`docs/mod-base-conformance/head-<n>.md`, on top of the previous head (first-parent). A family with
`carry_forward` must treat that path as documentation: its `family_validate` must carry an earlier
generation forward across such a commit (the Quick Skin-like fixture and the canary carry across
any commit that leaves their release matrix and scenario contract unchanged), or the second
generation's legs, and the `carried` variant, fail.

| Variant | Runs when | Proves |
|---|---|---|
| `attested` | `source.attestation_job` is set | `authenticate` binds the attestation job of a re-published tested run; no anchor is cut |
| `delegated` | `source.delegated_reuse_extension` is set and the fixtures define `delegated_extensions` | a `delegated` handoff whose extensions `authenticate_extensions` verifies with `reuse_verified` |
| `selected` | `baseline_archive.enabled` and the fixtures define `selected_extensions` | `compact` refuses a `selected` handoff (R3); `compose` completes it with the published `mb-baseline` |
| `family-outcomes` | `--families`, at least one family, `family_bundle`, and `FAMILY_OUTCOMES` lists `superseded` or `unavailable` | `family collect` reports exactly each listed absence and writes no output |
| `newest-run` | `source.require_newest_run` | a newer failed source run refuses the older evidence, nominated or searched |
| `carried` | `--families` and at least one family with `carry_forward` | a third head: every family leg walks the first-parent history back to the second head's generations (a family cache whose envelope still covers the first head, or the woken leg's own), `family collect` carries it (R5), `build` checks the recorded selection by id, refresh, `current` and a dry-run rotation |

### Optional conformance fixtures

Besides `synthesize`, the simulation looks these names up in the fixtures module (loaded afresh for
every call). They are called directly as `function(ctx, **arguments)`, by keyword, with a fresh
`Context` (a private `tmpdir`), not through `run_hook`'s hook schemas; anything they raise, or a
result of the wrong shape, fails the run. `family_bundle` gets `api` `None`; the two extension
fixtures get the seeding API below as `ctx.api`.

| Name | Signature or value | Result | Required |
|---|---|---|---|
| `family_bundle` | `family_bundle(ctx, family, key, target, expectation, producer, out_root, image_factory, outcome)` | `None` | with `--families` when the configuration declares a family: without it the run fails ("--families needs the fixtures module's family_bundle"); never asked for otherwise |
| `FAMILY_OUTCOMES` | a non-empty tuple or list of outcomes among `available`, `superseded`, `unavailable` (absent means `("available",)`) | | optional: enables the `family-outcomes` variant |
| `delegated_extensions` | `delegated_extensions(ctx, target, tested_run)` | extensions | optional: enables the `delegated` variant |
| `selected_extensions` | `selected_extensions(ctx, target, baseline)` | extensions | optional: enables the `selected` variant |

* `family_bundle` writes the mod's **native** family bundle of `key` (what the family's producer
  workflow would hand to the `publish-family` composite) into the existing empty directory `out_root`
  (an absolute path string). `family` is the configured family id; `target` is the adapter's
  `targets` result for `key`; `expectation` is the adapter's expectation of it, derived with the
  family producer run's projection and no extensions; `producer` is that producer attempt's
  `RunRecord` (a run of `producer.workflow` on the canonical branch at the subject, started by
  `producer.events[0]`); `image_factory` is `pattern_png`, as for `synthesize`. `outcome` is
  `available` (a clean generation of the subject that `family_validate` accepts) or one of the other
  outcomes the module lists in `FAMILY_OUTCOMES`: `superseded` (contract drift, which
  `family_validate` must report as `superseded`) or `unavailable` (a lineage or impact refusal,
  reported as `unavailable`). The core then wraps the bundle in its `mod-base.family.envelope`,
  validates it (`validate --kind family`) and collects it (`family collect` must report exactly
  `outcome`). With `--families`, every configured family gets an `available` bundle per selected key.
* `delegated_extensions` returns the extension objects that prove the handoff reuses the tested run
  `tested_run` = `{id, run_attempt, path, event, head_branch, head_sha}` (a `pull_request` run of
  `source.workflow` on another branch). They must include `config.source.delegated_reuse_extension`,
  and `authenticate_extensions` must answer `reuse_verified: true` for them.
* `selected_extensions` returns the extension objects that make the adapter's expectation
  `scope.kind: "selected"`, to be completed by the `compose` hook with the `mb-baseline` artifact the
  simulated generation published for `key`: `baseline` = `{id, name, digest}`.
* Each extension function returns either the extension objects themselves (a non-empty object, each
  name a declared extension, as in `extensions.json`) or `{"extensions": {...}, "responses": [...]}`.
  `responses` (at most 64) are exact API bodies `{path, params?, payload}` the simulated GitHub serves
  to the adapter's network hooks (`authenticate_extensions`, `compose`); each is seeded exactly as
  `ctx.api.add_response` does, so every `path` must be one of this repository's
  (`/repos/<repository>/...`) under its rules below, and anything else fails the run.
* **The seeding API (v0.9.2).** A real adapter's network hooks read more than JSON bodies (Quick
  Skin's `runtime_source` downloads the tested run's `tested-source` seal and the handoff run's
  `reused-source` descriptor, its feature selection a coverage certificate: ZIP artifacts bound to
  their runs and jobs). So `delegated_extensions` and `selected_extensions` receive as `ctx.api` a
  `mod_base.conformance._fixture_api.FixtureGitHub` over the simulated GitHub. It reads like a
  read-only `GitHubApi` (`repository`, `get_json`, `paginate`, `read_listing`, `download`; at most
  160 requests per call; `post_json`/`delete` raise `ReadOnlyViolation`) and names
  `handoff_run`, `{id, run_attempt, path, event, head_branch, head_sha}` of the run whose handoff
  the extensions will prove (already seeded; its jobs are seeded after the call). Its typed,
  bounded seeders add exactly the evidence the simulation does not hold (per call; a seeding
  mistake raises `ValueError` and fails the run):

  | Seeder | Rules |
  |---|---|
  | `add_artifact(run_id, name, archive, *, created_at=None) -> record` | an artifact of any seeded run (the handoff run, the delegated tested run, or a run from `add_run`) with its ZIP bytes (a readable ZIP of at most 16 MiB, 64 MiB and 32 artifacts per call); `name` matches `[A-Za-z0-9][A-Za-z0-9._+-]{0,127}` and is never a kit (`mb-...`), `github-pages` or simulation (`conformance-...`) name; `created_at` defaults to the run's `updated_at`. It returns the API record (`id`, `name`, `digest`, `size_in_bytes`, `created_at`, `expired`, `workflow_run`), so an extension object can name the artifact's id and digest. |
  | `add_run(run) -> record` | a run of another workflow of this repository: `run` gives `path` (never `pages.yml`, `source.workflow` or a family producer, whose runs the simulation owns), `event`, `head_branch`, `head_sha` and optionally `created_at` (default: the handoff run's), `status`/`conclusion` (default `completed`/`success`), `display_title` or any other JSON field; `id`, attempt 1, `workflow_id` and `head_repository` are assigned (and the workflow's `/actions/workflows/<file>` record). At most 16 runs. |
  | `add_jobs(run_id, run_attempt, jobs)` | jobs (JSON objects with a `name`; `status`/`conclusion` default to `completed`/`success`; `id`, `run_id` and `run_attempt` assigned) appended to an attempt of any seeded run, after the jobs the simulation seeds for it before or after this call; a name never repeats one of that attempt. At most 128 jobs. They count in the attempt's job graph (`source.require_job_graph`). |
  | `add_response(path, payload, *, params=None)` | the exact JSON body of one GET of this repository that nothing answers yet (a pull request, a synthetic commit): never a kit-owned route (`/actions/...`, `/branches...`, `/contents/...`, the repository itself: use the typed seeders) and never a path the simulated GitHub already answers. At most 64. |

### The conformance report

`mod_base conformance` prints one canonical JSON object on stdout (exit 0) with exactly these keys;
the parent refuses a report of any other shape:

| Key | Value |
|---|---|
| `repository` | the simulated repository (the one the adapter's expectation names) |
| `kit` | `{repository, sha, version}` of the simulated kit |
| `keys` | non-empty, one entry per simulated key: `{key, lanes, frames, comparisons, scope, anchor}` (`scope` is `complete`; `anchor` whether an anchor was cut) |
| `families` | one entry per promoted family leg: `{family, key, status}` |
| `checks` | the number of checks performed (> 0) |
| `variants` | `{attested, delegated, selected, family-outcomes, newest-run, carried}`, each `"passed"` or `"skipped: <reason>"` |
| `admission` | the admission results observed, in order, as `"<operation>:<reason>"` |
| `hooks` | the sorted names of every adapter hook that answered |
| `site` | facts about the first generation's site (`files`, `frames`, `node_check`, `rotation_planned`), `composed_lanes` (when the `selected` variant ran: how many lanes of its composed bundle hold only `baseline`, only `selected` or `mixed` frames; a partially re-captured lane is `mixed`), `max_job_reads` (the most API reads one simulated Pages job made, at most 160), `listing_rereads` (the inconsistent artifact listings the refresh jobs read again: every refresh after a sibling's upload sees one `total_count` one row off and must re-read it exactly once) and `generations`: one entry per later generation, `{generation, head, pages_run, key_routes, family_legs, rotation_planned}` (`key_routes` counts the keys selected as `handoff` or `cache`; `family_legs` counts the legs collected `carried`, `fresh` or `unavailable`) |

A mod's own conformance test (Quick Skin's `test_mod_base_conformance.py` runs `conformance --keys
mc1.20.1,mc26.3 --families`) should assert the variants it expects to pass, since a skipped variant
does not fail the run.

## The `ctx` object (`mod_base.adapter.api.Context`)

| Member | Meaning |
|---|---|
| `repo_root` | the mod checkout (at `GITHUB_SHA` in Pages jobs) |
| `config` | the validated `mod_base.config.Config` |
| `tmpdir` | a private per-call temporary directory |
| `implementation_sha` | `GITHUB_SHA` in Pages jobs; the subject commit in the `prepare-evidence` composite (`prepare`, `validate --kind handoff`, the `anchor` verbs) |
| `read_blob(commit, path, max_bytes)` | bounded `git cat-file` of objects already fetched as inert objects; never a checkout |
| `runtime_tree(root)` | a bounded, regular-file-only, symlink-refusing `RuntimeTree` view |
| `image_metrics(path, size_policy)` | the kit's PixelMetrics (`imaging.metrics.inspect_png`) |
| `api` | a read-only `GitHubApi`, present only for a declared network hook in a token job (below); otherwise `None` (the conformance extension fixtures get the seeding API instead, above). It is capped at `limits.MAX_PAGES_API_READS` (160) requests per call, retries included (also the re-reads of a listing GitHub serves inconsistently: `paginate` and `read_listing` read one again from page 1, at most `limits.LISTING_READ_ATTEMPTS` times); a further request raises `RequestBudgetExhausted` in the hook |

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

The host enforces this table (`protocol.HOOK_JOBS`) at runtime, before any child starts
(`adapter.host.check_placement`; the in-process test host applies the same check):

* a hook in a `protocol.FORBIDDEN_JOBS` job (`$GITHUB_JOB`) is refused;
* the job is a Pages callee job only when `$GITHUB_JOB` is one of `protocol.TOKEN_JOBS`
  (`admit`, `collect`, `family`, `build`) **and** `$GITHUB_WORKFLOW_REF` names this repository's
  `.github/workflows/pages.yml` (a reusable workflow's jobs carry their caller's workflow ref).
  Every other job, including a mod job that happens to be called `build` or `collect`, and a local
  run without `$GITHUB_JOB`, counts as `prepare-evidence` (`adapter.host.placement`);
* the hook must be allowed there by its row, so for example the `family` job can never call
  `targets`, and `prepare-evidence` never runs a network hook;
* the token is handed only to a hook in `config.adapter.network_hooks` that asked for network, in a
  Pages callee job its row allows. Tests that drive the real host in a Pages job therefore set
  `GITHUB_JOB` and `GITHUB_WORKFLOW_REF=<repository>/.github/workflows/pages.yml@refs/heads/<branch>`.

## Isolation (`mod_base.adapter.host`)

`host.call(invocation, hook, arguments, network=...)` writes a request envelope and runs exactly:

```
env -i PATH=/usr/bin:/bin:<dirname(python3)> HOME=<tmp>/home TMPDIR=<tmp> LANG=C.UTF-8 PYTHONHASHSEED=0 \
       PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
       PYTHONPATH=<kit>/src:<each config.adapter.python_path entry, inside the repo, no symlink components> \
       [GH_TOKEN GITHUB_API_URL=https://api.github.com GITHUB_REPOSITORY
                                  <- only for a declared network hook, called with network,
                                     in a Pages callee job its HOOK_JOBS row allows (above)]
  python3 -P -m mod_base.adapter.host_child --adapter <repo>/<config.adapter.path> --hook <name> \
          --request <tmp>/req.json --response <tmp>/resp.json
```

* The argv is fixed (`protocol.CHILD_FLAGS`); hook names come only from `protocol.HOOKS`.
* The child loads the adapter with `importlib.util.spec_from_file_location`, requires
  `ADAPTER_API` in `protocol.ADAPTER_API_WINDOW` (`{1}`; N and N-1 from v2 on), calls the hook and
  writes a canonical response.
* Timeout: `config.adapter.timeout_seconds` (default 600, at most 1800). The response is read to at
  most 16 MiB through strict JSON and validated.
* `python3` is the host's own interpreter (`sys.executable`, unresolved, so a virtual environment
  keeps its locked site-packages); `PATH` adds only its directory.
* Call layout: a fresh `0700` call directory holds `req.json`, `resp.json`, `mod-base.json` (the
  canonical bytes of the config the parent already validated, so the child never re-reads a file
  that may have changed) and the hook's private `<tmp>` (`TMPDIR`, `ctx.tmpdir`, with
  `<tmp>/home`); request and response stay outside the directory the hook may write in. The whole
  call directory is removed afterwards.
* Output: the child's stdout and stderr are piped (never a log file) and capped together at
  `adapter.host.MAX_CHILD_OUTPUT_BYTES` (4 MiB); beyond it the child is killed and the call fails.
  Only a 2 KiB tail is kept, for the one-line error message.
* Processes: the child runs in its own session. On timeout or output overflow its whole process
  group is killed, and after every call any process the hook left behind is killed too.

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
  `expectation.json`, with the handoff run's projection and, for a tested run started by another
  event, also with the tested run's own (reason `expectation-drift`). `validate --kind handoff`
  re-derives only inside the producing run: it needs the handoff run's event, which only that run
  knows without the API, so `GITHUB_RUN_ID`/`GITHUB_RUN_ATTEMPT` must name the handoff claim
  (otherwise exit 2, reason `usage`). Pages jobs re-derive with the authenticated run records
  instead (`compact`, `compose`, `build`), and `validate --kind compact` calls no hook at all.
* **R3 `compose`:** the core downloads the named baseline itself by id and authenticates its owner
  (a successful `pages.yml` run on the default branch whose attempt's `Finalize / Refresh evidence
  cache for {key}` job succeeded and uploaded it inside the "Retain the complete compact generation
  for feature evidence reuse" step window), validates it as a compact bundle, and requires every
  `baseline` frame to equal the baseline, every `selected` frame to equal the core's own
  compaction, the composed frames to equal the complete expectation, and every `tested` record to
  equal its source. **Full use:** the `epoch: selected` frames are exactly the selected
  compaction's frames, every selected lane is present and is exactly the selected lane record (its
  roles included, so they are the complete lane's), with exactly `baseline_run` = the baseline
  lane's execution when it still holds baseline frames, every selected comparison is present, no comparison spans the two
  epochs, and every other lane equals its baseline lane, so a hook can never publish older baseline
  evidence for what the selection re-tested.
* **R4 `family_validate`:** envelope inventory, then the projection schema, then every image
  re-inspected (dimensions equal `thumbnail(recorded source, derivative_box)`, pixels match), no
  unclean pair, `coverage_sha == expected_coverage_sha`, and the projection bound to the envelope
  (subject, producer claim, `carried_from`; see `family_validate` above).
* **R5 carry-forward:** with `carried_from`, the core requires `git merge-base --is-ancestor
  carried_from expected_coverage_sha` over inert objects: the `family` job fetches both commits
  anonymously before `family collect` runs, and the core only reads objects already present (it
  never fetches); an envelope's own `carried_from` is re-proven up to its `coverage_sha`. Both hold
  at every collection, including a family cache collected again by a later Pages run, and `build`
  re-proves them for every collected leg.
* **Recorded selection (`family collect`):** before the hook runs, the `Selected` record of the
  family job's `select` (`--selected-json`) must name this family and key and the generation being
  collected (a family handoff of the envelope's producer attempt, or a family cache named between
  the envelope's coverage and the leg coverage), and `envelope.kit` must be the pin of
  `producer.workflow` at `producer.commit` (read from inert objects). The record is written verbatim
  as the collected artifact's `selected.json`, which `build` re-authenticates by id.
* **Family provenance (`build`):** every collected family leg's producer attempt must be a
  successful `families[].producer.workflow` run (path and workflow id) of this repository on the
  default branch at `producer.commit`, started by one of `producer.events`; the projection's
  `provenance.producer` must equal its run record, and the envelope's `kit` must be the pin of
  `producer.workflow` at that run's head (SPEC §1.8), for a family cache as for a family handoff
  (a cache is the producer's bundle verbatim).
* **R6 `authenticate_extensions`:** every extension named in the manifest is verified; for
  `reuse: "delegated"`, `reuse_verified` is true.
