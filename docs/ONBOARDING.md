# Onboarding a mod

This checklist connects a new mod repository to mod-base, and lists what Etherology Revived (ER)
in particular needs (SPEC §4.9; out of scope for the first rollout). Read
[ARCHITECTURE.md](ARCHITECTURE.md) first; [ADAPTER.md](ADAPTER.md) is the adapter contract and
[OPERATIONS.md](OPERATIONS.md) holds the owner commands referenced below.

## Before you start

- The repository is on GitHub under The Plum Team, with a protected default branch (a ruleset with
  required pull requests, required checks and no force pushes), and its packaged end-to-end tests
  already produce screenshots with a machine-readable report.
- Licensing: the kit is published under All Rights Reserved terms, and its managed files
  (`scripts/ci/mod_base_kit.py`, `docs/ai/shared/*.md`, `.gitattributes` and the managed region of
  `pages.yml`) are copied into the mod's repository. Before a mod that is not owned by the kit's
  copyright holder, or that is published under an open-source license (ER is LGPL-2.1-only), adopts
  the kit, settle with the kit owner how those copied files are licensed in that repository and
  record the answer there.
- Every `uses:` in the mod must already be pinned to a full commit SHA with a `# vX.Y.Z` comment.
  ER still pins actions by tag: convert every reference first (for example with the release page
  of each action), and do not enable organization-wide `sha_pinning_required` until it is done.

## 1. Pages and environment

```bash
MOD=The-Plum-Team/<mod-repository>
gh api -X POST "repos/$MOD/pages" -f build_type=workflow
gh api -X PUT "repos/$MOD/environments/github-pages" --input - <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api -X POST "repos/$MOD/environments/github-pages/deployment-branch-policies" -f name=<default-branch> -f type=branch
```

ER's default branch is `main`. If the repository already publishes Pages from a branch (for
example `docs/`), switching `build_type` to `workflow` replaces that site at the first deployment.

## 2. Seed the template

Write the mod's `site/mod-base.json` first (start from the kit's
`template/seed/site/mod-base.json.tmpl`; every field is described in [SCHEMAS.md](SCHEMAS.md)).
ER uses `"license_label": "LGPL-2.1-only"`, which seeds the LGPL-2.1 notice instead of the
All Rights Reserved license. Then, from a clean kit checkout at a release tag:

```bash
git clone https://github.com/The-Plum-Team/mod-base.git ../mod-base
git -C ../mod-base switch --detach vX.Y.Z
PYTHONPATH=../mod-base/src python3 -P -m mod_base template init --repo . --seed --from-config ../config.json
```

`init` creates only missing files and never overwrites one. It pins the new
`.github/workflows/pages.yml` to the kit commit it runs from (labelled with that commit's release
tag), and prints the seed placeholders left for you:

- `LICENSE`: `{{holder}}`, `{{years}}`, the third-party section, and for the LGPL notice the file
  name of the full license text; review every line, since legal text is never managed later;
- `.github/CODEOWNERS`: `{{owner}}`;
- `CONTRIBUTING.md` and `docs/ai/PROJECT.md`: `{{test_command}}` and the project-specific sections;
- existing fragment files (`.gitignore`, `.github/dependabot.yml`, the pull-request template,
  `AGENTS.md`) are left as they are: merge in the lines and markers `template check` reports.

If the adoption must be split across pull requests, list the root files that land later in
`template.deferred` (only `.gitattributes`, `.gitignore`, `.github/dependabot.yml`,
`.github/pull_request_template.md` and `AGENTS.md` can be deferred), and empty it in the last pull
request. `template check` prints the required lines a present deferred fragment still lacks as
`pending`; every other rule of a present deferred file is enforced.

## 3. Write the adapter

`scripts/pages/mod_base_adapter.py` is seeded as a stub whose required hooks raise
`NotImplementedError`. Implement at least:

- `targets(ctx, branches)`: one target per release key (ER: one key per supported Minecraft
  version, or one key for the default branch);
- `expectation(ctx, target, tested_run, extensions)`: the lanes, captures and comparisons of the
  key. The adapter is contract-agnostic: ER derives them from its Java-defined scenarios
  (`ScenarioDefinitions.java`), read as source text or from a generated manifest, not from a JSON
  contract;
- `collect(ctx, runtime_root, target, expectation)`: read the packaged output (ER:
  `reports/report.json` and its screenshots) and return the runtime files, lanes (with the
  production JAR SHA-256), frames (with each capture's assertion message as `runtime_evidence`) and
  comparisons.

Add `anchor_selection` only when `anchor.enabled` is set, `expected_source_jobs` only with
`source.require_job_graph`, and the network hooks only when the configuration declares them. Put
a `synthesize` fixture hook in `adapter.fixtures_path` so `conformance` can exercise the adapter
without Minecraft.

## 4. Producer and wake jobs

Replace any step that commits evidence to the repository (ER: `docs/evidence`) with the kit's
composites in the packaged-E2E workflow, pinned to the same SHA as `pages.yml`:

```yaml
  public-evidence:
    name: Prepare public evidence for <key>          # must equal source.handoff_job in site/mod-base.json
    needs: [packaged-e2e]
    permissions:
      actions: read
      contents: read
    steps:
      - uses: actions/checkout@<sha> # vX.Y.Z
        with:
          persist-credentials: false
      # ... download the packaged output into e2e-out ...
      - name: Prepare and hand off public evidence    # must equal source.handoff_step
        uses: The-Plum-Team/mod-base/actions/prepare-evidence@<pin> # vX.Y.Z
        with:
          e2e-root: e2e-out
          key: <key>
          subject-branch: ${{ github.ref_name }}
          subject-commit: ${{ github.sha }}
          subject-tree: <tree of github.sha>
          tested-run-id: ${{ github.run_id }}
          tested-run-attempt: ${{ github.run_attempt }}
          tested-branch: ${{ github.ref_name }}
          tested-commit: ${{ github.sha }}
          tested-controller-branch: ${{ github.ref_name }}
          tested-controller-sha: ${{ github.sha }}
          anchor: "off"
  notify-pages:
    needs: [public-evidence]
    permissions:
      actions: write
    steps:
      - uses: The-Plum-Team/mod-base/actions/notify-pages@<pin> # vX.Y.Z
        with:
          operation: deploy
          run-id: ${{ github.run_id }}
          sha: ${{ github.sha }}
```

The producer workflow is `source.workflow`; its triggering events must be listed in
`source.events`. Keep the wake job free of any checkout, and never give the producer
`contents: write`.

## 5. Mod tests and local checks

Mod tests reach the kit only through the bootstrap: a small helper calls
`scripts/ci/mod_base_kit.py`'s `kit_path()`, puts `<kit>/src` on `sys.path`, and lets a missing kit
fail the run. Add these to the repository's verification commands and CI policy job:

```bash
python3 scripts/ci/mod_base_kit.py verify --network
python3 scripts/ci/mod_base_kit.py run template check --repo .
python3 scripts/ci/mod_base_kit.py run conformance --repo . --keys <key>
```

To iterate on the adapter against a local kit clone, set
`MOD_BASE_KIT_PATH=../mod-base MOD_BASE_ALLOW_UNPINNED=1` (refused whenever `CI` is set).

## 6. Governance

- Add the mod's `.github/CODEOWNERS` owners for `/.github/`, `/site/`, `/scripts/pages/`,
  `/scripts/ci/`, `/AGENTS.md` and `/docs/ai/`.
- Make the new CI checks required in the default-branch ruleset, and enable
  `sha_pinning_required` on the repository (see P4 in [OPERATIONS.md](OPERATIONS.md#owner-activation)).
- Keep the `github-pages` environment restricted to the default branch.

## 7. First publication

Merge the adoption pull request, dispatch the producer on the default branch, and watch the
`Project site` run: `admit` reports `awaiting-complete-v1-evidence` until every key has evidence,
then publishes. Check `/build.json` for the two-part identity and follow the
[admission reasons](OPERATIONS.md#publication-admission) if nothing is deployed.
