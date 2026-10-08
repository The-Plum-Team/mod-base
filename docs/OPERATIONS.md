# Operations

Owner activation, releases, the canary, rollout, publication admission, the kill switch and
recovery. Every command is a `gh` or `git` invocation that the rollout operator, acting for the
maintainer, runs at the rollout stage named (the rulesets included); the kit's workflows never
change repository settings themselves. [SECURITY-MODEL.md](SECURITY-MODEL.md) explains why each
setting matters.

Repositories used below:

```bash
KIT=The-Plum-Team/mod-base
CANARY=The-Plum-Team/mod-base-canary
QS=The-Plum-Team/Quick-Skin-Mod
BP=The-Plum-Team/Block-Pops-Minecraft-Mod
```

## Owner activation

| Precondition | What | When |
|---|---|---|
| P2 | mod-base repository settings and its two rulesets | rollout stage S0, after the first green `mod-base CI` run on `main` and before tagging `v0.9.0` |
| P3 | the canary repository (authorized) with Pages and a `github-pages` environment | stage S1 |
| P1 | the Block Pops `master` ruleset with the App-authored gate contexts | before Block Pops PR A is marked ready (S4) |
| P4 | `sha_pinning_required` on the Quick Skin and Block Pops repositories (recommended) | any time before their cutover |
| P5 | both mods' `github-pages` environments restricted to `master` | verify before each mod's cutover |

### P2: mod-base settings

```bash
gh api -X PATCH "repos/$KIT" --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                           "secret_scanning_push_protection": {"status": "enabled"}}}
JSON
gh api -X PUT "repos/$KIT/actions/permissions/workflow" --input - <<'JSON'
{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false}
JSON
gh api -X PUT "repos/$KIT/actions/permissions" --input - <<'JSON'
{"enabled": true, "allowed_actions": "selected", "sha_pinning_required": true}
JSON
gh api -X PUT "repos/$KIT/actions/permissions/selected-actions" --input - <<'JSON'
{"github_owned_allowed": true, "verified_allowed": false, "patterns_allowed": []}
JSON
gh api -X PUT "repos/$KIT/immutable-releases"
```

If the immutable-releases endpoint is not available to the account, enable **Settings → General →
Releases → Enable release immutability** instead. `CODEOWNERS` (`* @AkaNebur`) is part of the kit
tree.

### P2: ruleset "mod-base main"

The required contexts are the check runs of the kit CI workflow (`mod-base CI`), created by the
GitHub Actions app (`gh api apps/github-actions --jq .id` prints its id, 15368). The ruleset can
only select a context after it has been reported once, so apply it after the first green CI run on
`main`.

```bash
gh api -X POST "repos/$KIT/rulesets" --input - <<'JSON'
{
  "name": "mod-base main",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request",
     "parameters": {"required_approving_review_count": 0, "dismiss_stale_reviews_on_push": false,
                    "require_code_owner_review": false, "require_last_push_approval": false,
                    "required_review_thread_resolution": true}},
    {"type": "required_status_checks",
     "parameters": {"strict_required_status_checks_policy": true, "do_not_enforce_on_create": false,
                    "required_status_checks": [
                      {"context": "Test", "integration_id": 15368},
                      {"context": "Workflow policy", "integration_id": 15368},
                      {"context": "Front end", "integration_id": 15368}]}}
  ]
}
JSON
```

### P2: ruleset "mod-base immutable tags"

Tag creation stays allowed; a created `v*` tag can never move or disappear.

```bash
gh api -X POST "repos/$KIT/rulesets" --input - <<'JSON'
{
  "name": "mod-base immutable tags",
  "target": "tag",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}]
}
JSON
```

Verify P2:

```bash
gh api "repos/$KIT" --jq '.security_and_analysis'
gh api "repos/$KIT/actions/permissions"
gh api "repos/$KIT/actions/permissions/workflow"
gh api "repos/$KIT/rulesets" --jq '.[] | [.id, .name, .target, .enforcement] | @tsv'
gh api "repos/$KIT/rules/branches/main" --jq '.[].type'
```

### P1: ruleset "Block Pops master"

The two contexts are commit statuses written by Block Pops' PR-gate GitHub App
(`handle-pr-gate-result.yml`), not by GitHub Actions, so the rule must name that App. Read its id
from the installation list, or from the creator of an existing status on a recently gated pull
request head:

```bash
gh api "orgs/The-Plum-Team/installations" --jq '.installations[] | [.app_id, .app_slug] | @tsv'
# or, with HEAD_SHA a pull-request head the gate has evaluated:
slug="$(gh api "repos/$BP/commits/$HEAD_SHA/statuses" \
  --jq '[.[] | select(.context == "Trusted PR / Build and verify")][0].creator.login' | sed 's/\[bot\]$//')"
APP_ID="$(gh api "apps/$slug" --jq .id)"
```

Then apply the ruleset with that id:

```bash
jq -n --argjson app "$APP_ID" '{
  name: "Block Pops master",
  target: "branch",
  enforcement: "active",
  bypass_actors: [],
  conditions: {ref_name: {include: ["~DEFAULT_BRANCH"], exclude: []}},
  rules: [
    {type: "deletion"},
    {type: "non_fast_forward"},
    {type: "pull_request",
     parameters: {required_approving_review_count: 0, dismiss_stale_reviews_on_push: false,
                  require_code_owner_review: false, require_last_push_approval: false,
                  required_review_thread_resolution: true}},
    {type: "required_status_checks",
     parameters: {strict_required_status_checks_policy: true, do_not_enforce_on_create: false,
                  required_status_checks: [
                    {context: "Trusted PR / Build and verify", integration_id: $app},
                    {context: "Trusted PR / Packaged E2E gate", integration_id: $app}]}}
  ]}' | gh api -X POST "repos/$BP/rulesets" --input -
gh api "repos/$BP/rules/branches/master" --jq '.[] | [.type, (.parameters.required_status_checks // [] | tostring)] | @tsv'
```

Do not select the similarly named candidate-workflow checks; only the App-authored contexts are
authoritative. Quick Skin's existing rulesets (`20241788`, `20241790`) already protect its default
branch.

### P3: the canary repository

Authorized. Create it public, seed it (see [Canary procedure](#canary-procedure)), push `main`,
then enable Pages from Actions and restrict the `github-pages` environment to `main`:

```bash
gh repo create "$CANARY" --public --description "Synthetic mod-base canary: demonstration evidence, not a product"
# ... seed and push main (Canary procedure, steps 1-4) ...
gh api -X POST "repos/$CANARY/pages" -f build_type=workflow
gh api -X PUT "repos/$CANARY/environments/github-pages" --input - <<'JSON'
{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}
JSON
gh api -X POST "repos/$CANARY/environments/github-pages/deployment-branch-policies" -f name=main -f type=branch
gh api -X PUT "repos/$CANARY/actions/permissions/workflow" --input - <<'JSON'
{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false}
JSON
```

### P4 and P5: the mods

Set `sha_pinning_required` without changing the rest of each mod's Actions policy, and confirm the
Pages environment policy (never at organization level while other organization repositories still
pin actions by tag):

```bash
for R in "$QS" "$BP"; do
  gh api "repos/$R/actions/permissions" \
    | jq '{enabled, allowed_actions, sha_pinning_required: true}' \
    | gh api -X PUT "repos/$R/actions/permissions" --input -
  gh api "repos/$R/environments/github-pages" --jq '.deployment_branch_policy'
  gh api "repos/$R/environments/github-pages/deployment-branch-policies" --jq '.branch_policies[].name'
done
```

The branch policy list must be exactly `master` for both mods.

## Releasing mod-base

The tag comes first and the canary then proves it (rollout stages S1 and S3). The canary pins the
tag under test, and `verify --network`, `bump` and Block Pops' `stage` of a new pin all refuse a
`# vX.Y.Z` pin whose tag does not exist or does not peel to the pinned commit. A green canary
decides whether mods may pin a tag, never whether it is created.

1. Merge the release change to `main` through a pull request. It sets `__version__` in
   `src/mod_base/__init__.py` to `X.Y.Z`, adds the `## vX.Y.Z` section to `CHANGELOG.md`, and
   refreshes the staged-file locks, then the digest literal
   (`PYTHONPATH=src python3 -m mod_base.template.lock --write`, then
   `python3 tools/update_tree_digest.py --write`).
2. Wait for `mod-base CI` to be green on that exact merge commit:

   ```bash
   COMMIT=<the release merge commit>
   gh run list -R "$KIT" --workflow ci.yml --commit "$COMMIT" --json event,status,conclusion
   ```

3. Tag that commit and push the tag. Its `__version__` must already equal the tag: every handoff
   records the executing kit's `__version__`, and authentication requires it to equal the pin's
   `# vX.Y.Z` comment.

   ```bash
   git fetch origin main && git merge-base --is-ancestor "$COMMIT" origin/main
   git show "$COMMIT:src/mod_base/__init__.py" | grep -Fx '__version__ = "X.Y.Z"'
   git tag -a vX.Y.Z -m "mod-base vX.Y.Z" "$COMMIT"
   git push origin vX.Y.Z
   ```

4. Run the canary pinned to the tag ([Canary procedure](#canary-procedure)): the first release seeds
   it, a later one moves it with `bump --to vX.Y.Z`.
5. Only once the canary is green, publish the release notes with
   `gh release create vX.Y.Z -R "$KIT" --verify-tag --notes-file notes.md`, listing schema and kind
   changes, managed-file changes, adapter-protocol changes and the `pixel_metrics_version`. Mods
   bump to the tag only after that.

A tag is never moved or deleted. A tag whose canary fails gets no GitHub Release and no mod pins
it: the fix merges to `main` and is released as the next patch tag `vX.Y.(Z+1)` through the same
five steps, canary included. A faulty release that mods already pin is fixed forward the same way,
or mods pin back to the previous tag with an ordinary pull request.

Every tag names its own release commit. When nothing changed after the last `v0.9.x` patch tag
(`v0.9.3`), `v1.0.0` (stage S3) reuses its code unchanged, but its release change still sets
`__version__` to `1.0.0`, adds the `## v1.0.0` changelog section and regenerates the digest
literal. Tagging the `v0.9.3` commit a second time would make every handoff record kit version
`0.9.3` under a `# v1.0.0` pin, which authentication refuses.

## Bumping the kit in a mod

```bash
git switch --create chore/mod-base-vX.Y.Z origin/master          # Block Pops: controller-upgrade/mod-base-vX.Y.Z
python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z
python3 scripts/ci/mod_base_kit.py verify --network
python3 scripts/ci/mod_base_kit.py run template check --repo .
git diff --check && git diff
```

`bump` refuses a tag that does not peel to a commit reachable from mod-base `main` before it edits
anything, then rewrites every pin and `# v` comment and re-synchronizes the managed files from the
new kit. Quick Skin opens the bump as a draft and lands it through a batch pull request; Block Pops
opens a `controller-upgrade/*` pull request with the `controller-upgrade` label and the owner's
`/controller-upgrade approve <sha>`. Every bump runs the mod's complete gates. The release notes
list every managed-file change; a mod test that pins the bytes of a managed file moves to the new
kit's bytes in the same pull request (Quick Skin's `gradlew.bat` test checks the `cr-at-eol`
attribute, not the file's bytes, so it needs no change). A release can also tighten a fragment
rule, which `bump` does not rewrite (fragments are the mod's): `v1.0.1` requires every
`github-actions` update of `.github/dependabot.yml` to ignore `actions/deploy-pages`, pinned in the
caller's managed region. Add what `template check` reports in the same pull request. A Block Pops
bump is staged by the controller's bootstrap, the base branch's: one older than `v0.9.2` stages the
new kit without its `actions/`, so a gate step that reads the staged `actions/` can only follow
once the controller's own pin is `v0.9.2` or later.

## Build/E2E activation and rollback

A mod states how far it uses the shared Build and packaged E2E in
`site/mod-base-build-activation.json` ([SCHEMAS.md](SCHEMAS.md#profile-activation-data-v1)), next to
its Build configuration `scripts/ci/mod-base-build.json`. The mode decides which of the four kit
callers (`mod-base-guard.yml`, `mod-base-build.yml`, `mod-base-packaged-e2e.yml`,
`mod-base-gate-status.yml` in `.github/workflows/`) are managed files: none in `disabled`, all in
`shadow` and `shared-build-and-e2e`, all but the packaged E2E caller in `shared-build`, and in
`reviewed-rollback` those of the mode it leaves. A managed caller is the kit template rendered with
the mod's pin and, in the branch filter of the Build and packaged E2E callers' `push` trigger, the
`canonical_branch` of `site/mod-base.json`, byte for byte; it has no extension region and cannot
be named in `template.deferred`. A change of `canonical_branch` is therefore followed by
`template sync --write`. A caller outside its mode must not exist.

Every change of mode is its own pull request, never combined with a kit bump:

```bash
git worktree add --detach ../base origin/master        # the protected state the change starts from
python3 scripts/ci/mod_base_kit.py run template activation --repo .       # state, managed callers, allowed next states
# edit "mode" (and "rollback_from") in site/mod-base-build-activation.json, then:
python3 scripts/ci/mod_base_kit.py run template sync --repo . --write     # writes the callers the new mode manages
git rm <every caller the new mode no longer manages>                      # sync never deletes; check names them as forbidden
python3 scripts/ci/mod_base_kit.py run template check --repo .
python3 scripts/ci/mod_base_kit.py run template transition --repo . --base ../base
```

`template transition` exits 2 unless the candidate's callers are exactly the rendered templates
and its manifest is either unchanged (an ordinary bump, at any pin) or changed along an allowed
transition with both checkouts at the same pin. It compares the callers with the templates of the
kit that runs it, so run it through the candidate's bootstrap as above. The
allowed transitions are: no manifest to `disabled` (with the Build configuration, in a preparatory
pull request) and back; `disabled` to `shadow` or `shared-build`; `shadow` to `disabled`,
`shared-build` or `shared-build-and-e2e`; `shared-build` to `shared-build-and-e2e`; any of
`shadow`, `shared-build` and `shared-build-and-e2e` to `reviewed-rollback`; and `reviewed-rollback`
to `disabled`.

Rollback is two pull requests. The first sets `"mode": "reviewed-rollback"` and
`"rollback_from"` to the mode being left: the callers stay managed and unchanged while the mod's
previous gates are restored and reviewed. The second sets `disabled` (and `"rollback_from": null`)
and removes the callers. Deleting the manifest is not a rollback: with the Build configuration
still present `template check` fails, and a manifest is removed only from `disabled`.

A kit bump in an active mode is an ordinary bump: `bump` rewrites the pin lines and the new kit's
`template sync --write` renders every managed caller again. `bump` refuses a kit that does not read
the activation manifest (v1.0.3 and older) while a mode other than `disabled` is active, so a pin
rollback that far follows the two rollback pull requests. When `bump` fails after it started
writing, it restores every workflow and action file; run `template sync --repo . --write` to
restore any other managed file. On a `core.autocrlf=true` clone the callers check out with CRLF
until the managed `.gitattributes` lists them: add `/.github/workflows/mod-base-*.yml text eol=lf`
to `.git/info/attributes`.

## Canary procedure

The canary is a separate public caller repository, so it exercises exactly the cross-repository
resolution real mods use. It publishes only synthetic evidence under a "Synthetic demonstration
evidence — not a product" banner.

1. Check out the kit at the tag under test, already pushed by
   [Releasing mod-base](#releasing-mod-base) step 3, and create the canary working tree:

   ```bash
   TAG=v0.9.0
   git clone "https://github.com/$KIT.git" mod-base && git -C mod-base switch --detach "$TAG"
   PIN="$(git -C mod-base rev-parse "$TAG^{commit}")"
   git init -b main mod-base-canary && cp -R mod-base/canary/. mod-base-canary/
   ```

2. Pin every mod-base reference the canary carries to the tag under test. The kit cannot contain
   its own future commit, so `canary/` writes its references as `@{{PIN}} # {{VERSION}}`, like the
   managed caller template:

   ```bash
   cd mod-base-canary
   grep -rlF '{{PIN}}' .github | while IFS= read -r file; do
     sed -e "s/{{PIN}}/$PIN/g" -e "s/{{VERSION}}/$TAG/g" "$file" > "$file.new" && mv "$file.new" "$file"
   done
   ! grep -rF -e '{{PIN}}' -e '{{VERSION}}' .github
   ```

3. Seed every template file the canary does not carry itself (it never overwrites one), then check
   the repository:

   ```bash
   PYTHONPATH=../mod-base/src python3 -P -m mod_base template init --repo . --seed --from-config site/mod-base.json
   python3 scripts/ci/mod_base_kit.py pin            # must print "$PIN $TAG"
   python3 scripts/ci/mod_base_kit.py verify --network
   python3 scripts/ci/mod_base_kit.py run template check --repo .
   ```

   The canary carries these template files itself, so `init` leaves them exactly as copied:
   `.github/workflows/pages.yml` (pinned by step 2), `.github/CODEOWNERS`, `CONTRIBUTING.md`,
   `LICENSE` (the canary's own All Rights Reserved notice), `docs/ai/PROJECT.md`,
   `site/mod-base.json` and `scripts/pages/mod_base_adapter.py`.

   `init` creates every other template file and prints `created <path>` for each: `.gitattributes`,
   `.gitignore`, `.github/dependabot.yml`, `.github/pull_request_template.md`, `AGENTS.md`,
   `scripts/ci/mod_base_kit.py`, `docs/ai/shared/REPOSITORY.md`, `docs/ai/shared/PUBLIC-EVIDENCE.md`
   and `docs/architecture/decisions/README.md`. Any other `created` line means the working tree
   lacks a file of `canary/`: start again from step 1.

   A later canary cycle moves to a newer tag with `python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z`.

4. Commit, add the remote, push `main`, then finish P3 (Pages and environment).
5. Run the cycles required by stage S1, observing each item G1–G7 below. A producer's wake (or the
   hourly schedule) starts each Pages run, and one that publishes dispatches its own rotation run;
   `gh run list -R "$CANARY" --workflow pages.yml` lists both with their ids.
   - three full generations, each followed by its Pages run (deploy, finalize) and its separate
     rotation run:

     ```bash
     gh workflow run canary-producer.yml -R "$CANARY" --ref main
     ```

   - one forced head move (G7) and one producer re-run attempt
     (`gh run rerun <producer-run> -R "$CANARY"`);
   - one family-less generation: a producer generation at a head without a family generation (the
     first full generation is one), whose gallery reports that no "Synthetic pairs" evidence has
     been published;
   - one synthetic family generation, dispatched after a producer generation has published at the
     same head. `canary-family.yml` generates the `demo-pairs` generation of one key (the first key
     unless `key` names another) and wakes `pages.yml` with `operation=family`:

     ```bash
     gh workflow run canary-family.yml -R "$CANARY" --ref main
     gh workflow run canary-family.yml -R "$CANARY" --ref main -f key=<key>   # a key other than the first
     ```

   - one carried family leg: after a family generation has published, push a documentation-only
     commit and run a producer generation at the new head; its Pages run publishes the
     `demo-pairs` legs carried forward from the earlier head (the canary's `carry_forward` family
     carries across any commit that leaves its release matrix and scenario contract unchanged);
   - `canary-probe.yml` at least once, after a Pages run: it writes that run's
     `referenced_workflows` and job names, and its own (caller-side) `job.workflow_sha`, to its job
     summary. Its only input, the optional `pages-run-id`, names the Pages run to observe (empty:
     the newest):

     ```bash
     gh workflow run canary-probe.yml -R "$CANARY" --ref main -f pages-run-id=<pages-run-id>
     ```

What to observe for each item:

| Item | Claim | How to observe |
|---|---|---|
| G1 | `referenced_workflows` lists `The-Plum-Team/mod-base/.github/workflows/<file>.yml@<sha>` with a matching `sha` for every callee, while the run is in progress and even for skipped callees | `gh api "repos/$CANARY/actions/runs/$RUN" --jq '[.status, (.referenced_workflows[] \| .path + " " + .sha)]'` during and after the run |
| G2 | Job names are `Publish / Admit publication`, `Publish / Collect <key>`, `Publish / Build atomic static site`, `Finalize / Refresh evidence cache for <key>`, `Rotate / Rotate the authenticated successful generation`. A mod without families additionally reports its skipped `family` matrix job once, unexpanded, as `Publish / Collect ${{ matrix.family }} ${{ matrix.key }}` with conclusion `skipped` (Quick Skin's E2E job graph relies on the same platform behavior; `build` accepts that job only in this form, or its absence, and refuses any other stray). The canary configures `demo-pairs`, so it cannot show that row: record it from Block Pops' first Pages run | `gh api "repos/$CANARY/actions/runs/$RUN/attempts/1/jobs?per_page=100" --jq '.jobs[].name'` (for the unexpanded row: the same query against the Block Pops run, with `.jobs[] \| [.name, .conclusion]`) |
| G3 | The `github-pages` artifact uploaded by the callee `build` job deploys from the caller's `deploy` job under the `main` branch policy | `Deploy GitHub Pages` succeeds; `curl -fsS https://the-plum-team.github.io/mod-base-canary/build.json` shows the kit SHA |
| G4 | `GITHUB_WORKFLOW_REF` inside a callee is the caller's `.../pages.yml@refs/heads/main` | every callee's "Bind the two-part implementation identity" step succeeds (it asserts the value) |
| G5 | The composite tree check passes against the live trees API; admission sees the composite's upload window | the producer's "Verify the executing kit against the checked-out pin" succeeds; the admit summary nominates the handoff |
| G6 | `notify-pages` dispatches `pages.yml` holding only `actions: write`; rotation self-dispatches; the caller-side `job.workflow_sha` is the canary head (the kit-side value, visible only inside a kit callee job, is not observed in v1.0: deferred) | the dispatched run appears in `gh run list -R "$CANARY" --workflow pages.yml --event workflow_dispatch`; the rotation run follows; the `canary-probe.yml` job summary shows `workflow_sha` equal to the canary head it ran at |
| G7 | A head that moves between build and deploy keeps the previous site | during a Pages run, `git commit --allow-empty -m "canary: move head" && git push`; `deploy` fails ("advanced before deployment") and `build.json` is unchanged |

Record every observation in [Canary evidence](#canary-evidence) with its run URL. Stage S3 tags
`v1.0.0` only after G1–G7 are all observed in a green canary cycle at one `v0.9.x` patch tag, and
then runs the canary again pinned to `v1.0.0`. The `v0.9.0` and `v0.9.2` cycles each failed (see
the Defect rows) and `v0.9.1` preceded the mod-migration fixes of `v0.9.2`, so the patch tag that
satisfied S3 is `v0.9.3`: its single green cycle observed G1–G7, and the earlier rows stay as
history.

## Canary evidence

Fill one row per observation (run URLs are `https://github.com/The-Plum-Team/mod-base-canary/actions/runs/<id>`).
The first cycle ran at `v0.9.0` (`b6918cc`) with the canary head at `4ec994b`, then at `edfeb7f` (a
documentation-only commit for the carried family leg).

| Item | Kit tag / SHA | Run | Observed | Date |
|---|---|---|---|---|
| G1 | v0.9.0 / `b6918cc` | 36189680701 (rotation) | observed while the run was `in_progress`: `referenced_workflows` lists `rotate.yml`, `finalize.yml` and `publish.yml` of `The-Plum-Team/mod-base` `@b6918cc`, each with a matching `sha` | 2026-09-25 |
| G2 | v0.9.0 / `b6918cc` | 36189495635, 36189846471 | observed: `Publish / Admit publication`, `Publish / Collect mc1.20.1`, `Publish / Collect demo-pairs mc1.20.1`, `Publish / Build atomic static site`, `Finalize / Refresh evidence cache for mc1.20.1`, `Finalize / Refresh demo-pairs cache for mc1.20.1` (36189495635). A skipped matrix job appears once, unexpanded: the deferred run 36189846471, where `build` was skipped too, lists `Publish / Collect ${{ matrix.key }}` and `Publish / Collect ${{ matrix.family }} ${{ matrix.key }}`, each once with conclusion `skipped`. Still to be recorded from Block Pops' first Pages run: `build` accepting that unexpanded family row in a family-less run that publishes | 2026-09-25 |
| G3 | v0.9.0 / `b6918cc` | 36189495635 | observed: `Deploy GitHub Pages` succeeded; `build.json` names implementation `4ec994b`, run 36189495635 and kit `b6918cc` `0.9.0` | 2026-09-25 |
| G4 | v0.9.0 / `b6918cc` | the cycle's Pages runs | observed: every callee's "Bind the two-part implementation identity" step succeeded | 2026-09-25 |
| G5 | v0.9.0 / `b6918cc` | 36189435580 (producer) | observed: "Verify the executing kit against the checked-out pin" (the composite tree check) succeeded; admission nominated its handoffs with reason `initial-ordinary` | 2026-09-25 |
| G6 | v0.9.0 / `b6918cc` | 36189680701, 36189915606 (probe) | partly observed: `notify-pages` dispatched `pages.yml` holding `actions: write`; the rotation self-dispatched as 36189680701; the probe recorded the caller-side `job.workflow_sha` `4ec994b`, the canary head | 2026-09-25 |
| Family wake | v0.9.0 / `b6918cc` | 36189846471 | observed: a family wake with 1 of 2 keys' `demo-pairs` generations ready was deferred with reason `coalescing` | 2026-09-25 |
| Carried family leg | v0.9.0 / `b6918cc` | 36190041285 | observed: the Pages run at `edfeb7f` published the `demo-pairs` leg of `mc1.20.1` generated at `4ec994b`, with coverage `edfeb7f` | 2026-09-25 |
| Defect | v0.9.0 / `b6918cc` | 36190041285 | `Finalize / Refresh evidence cache for mc1.20.1` failed closed with `github-api: GitHub API GET /repos/The-Plum-Team/mod-base-canary/actions/runs/36190041285/artifacts listing total_count 6 disagrees with 5 listed rows` while its sibling finalize jobs uploaded their caches: the site deployed, but the generation lost its cache refresh and rotation. Fixed in v0.9.1 (consistent listings, exact-name run inventories in `refresh`); re-run the canary at v0.9.1 | 2026-09-25 |
| Re-run attempt | v0.9.1 / `06a939b` | 36199867668 (attempt 2), 36200564323 | observed: re-running the producer created attempt 2; its Pages run admitted it with reason `ordinary-replacement` and published | 2026-09-25 |
| G7 | v0.9.1 / `06a939b` | 36201086333 | observed: the head moved to `74eb271` while `Publish / Build atomic static site` ran; `build` itself failed its live-head recheck, so `deploy` never started and `build.json` stayed byte-identical (the previous site was kept) | 2026-09-25 |
| Defect | v0.9.2 / `4053de3` | 36210848548 | three sibling `Finalize / Refresh …` jobs failed closed with `current-run: this Pages run 36210848548 attempt 1 is not the in-progress run of the protected head`, though the head never moved: GitHub reports a running run as `queued`/`waiting` while matrix jobs wait for a runner. Fixed in v0.9.3 (any unfinished, conclusionless status identifies the current run) | 2026-09-26 |
| G1–G7 cycle | v0.9.3 / `c352cc4` | 36212202333 (producer), 36212234587 → 36212402875, 36212669394 → 36212796187, 36212878850 (probe), 36212917073 → 36213014518, 36213118248 → 36213242441, 36213348645 (G7), 36213492093 → 36213599915 | observed in one green cycle (Pages run → its rotation run): initial publication (`initial-ordinary`) with every `Finalize` job green; `demo-pairs` generations for both keys (`final-complete`); the probe (caller-side `job.workflow_sha` = canary head); a producer re-run attempt (`ordinary-replacement`); a documentation-only head whose Pages run carried both `demo-pairs` legs to coverage `f5c226b`; G7 (head moved to `d1f0303` during `build`, which failed its recheck, `build.json` unchanged); a final generation at `d1f0303`. G1 and G2 job names as in the rows above; G3 `build.json` names kit `c352cc4` `0.9.3` | 2026-09-26 |

`job.workflow_sha` observations. v1.1 may add it as a redundant cross-check of the kit SHA only
after three values **observed inside a kit callee job** (a step of a kit reusable workflow, which
v1.0 does not have) equal that run's `referenced_workflows[].sha`. `canary-probe.yml` cannot supply
such a value: it is a caller-side job, so its `job.workflow_sha` is the canary head. Record its
value only as the caller-side row of G6; it never counts toward the three.

| Run | Observed in | `job.workflow_sha` | `referenced_workflows[].sha` | Equal |
|---|---|---|---|---|
| pending | a kit callee job (deferred to v1.1) | | | |

## Rollout

| Stage | Action | Exit criterion |
|---|---|---|
| S0 | create and push mod-base; apply P2; CI green | `Test`, `Workflow policy`, `Front end` required and green on `main` |
| S1 | tag `v0.9.0` ([Releasing mod-base](#releasing-mod-base)); then create the canary pinned to that tag (P3) and run it; a failing canary is fixed forward as the next `v0.9.x` patch tag (it took `v0.9.3`), to which the canary moves with `bump` | G1–G7 recorded above in one green cycle at a `v0.9.x` tag |
| S2 | open the mod drafts pinned to the current `v0.9.x` tag (the drafts were prepared at `v0.9.0` and move straight to `v1.0.0`) (Quick Skin `feat/mod-base`, Block Pops PR A and PR B) | every local verification green |
| S3 | tag `v1.0.0` on its own release commit (the last `v0.9.x` code when unchanged, with `__version__` `1.0.0`); then move the canary to that tag with `bump --to v1.0.0`, run it and re-pin the drafts | canary green at `v1.0.0` |
| S4 | Block Pops first: apply P1, mark PR A ready, record `verify --network`, owner approval, merge, dispatch E2E | the `master` key is live at `/e2e/`, a rotation run is green, visual review finds `mb-anchor--` |
| S5 | Quick Skin: ready or batch PR; full Build and E2E; merge | all keys live as `mb-cache--`, two rotations, one family publication, one baseline consumed |
| S6 | Block Pops PR B, then PR C clearing `template.deferred` | `template check` clean with an empty `deferred` |

Between PR A and PR C, Block Pops lists `.gitattributes`, `.gitignore`, `.github/dependabot.yml`,
`.github/pull_request_template.md` and `AGENTS.md` in `template.deferred`: the root files a
controller upgrade cannot change. `template check` then accepts their absence, still requires a
present deferred `.gitattributes` to be byte-identical, and prints only the required lines Block
Pops' existing `.gitignore` lacks (`_site/`, `public-evidence/`, `/.architectury-transformer/`) as
`pending` without failing; every other rule of a present deferred file (the `AGENTS.md` grammar,
the pull-request template markers, the Dependabot ignores of the kit and, from `v1.0.1`, of
`actions/deploy-pages`) fails, so PR B must add those files complete. PR A must still bring its own `.github/CODEOWNERS`
(a protected path) up to the fragment rules (`/AGENTS.md` and `/docs/ai/` owned), because
`CODEOWNERS` can never be deferred.

The managed `.gitattributes` also pins `text eol=lf` for every managed and fragment path, so a
Windows clone with `core.autocrlf=true` checks them out byte-identical to the kit. Until Block Pops'
PR B adds it, such a clone gets CRLF copies, which `template check` reports as line-ending drift
(with its fix) rather than a whole-file diff: set `git config core.autocrlf input` in that clone and
check the managed files out again (delete them, then `git checkout -- <path>`), or run
`template sync --write`, which rewrites them (the caller included) with LF. The same applies to any
clone made before the rules landed, since Git applies new attributes only at the next checkout.

If P1 is not applied within 7 days of S3, S5 may run before S4 with the same exit criteria.

## Publication admission

`admit` (`Publish / Admit publication`) decides whether a Pages run publishes. Its reason appears
in the job's step summary; an ineligible admission is a normal outcome and the run ends green
without deploying.

| Operation | Source | Additional checks |
|---|---|---|
| `recovery` | the hourly schedule | none beyond the common checks |
| `manual` | an operator | never deferred for active source runs |
| `deploy` | a producer's `notify-pages` | the named run is a successful `source.workflow` run at the live head, polled up to 60 s; its `mb-handoff--` artifacts become the nominations |
| `family` | a family producer's `notify-pages` | the named artifact is the exact authenticated family handoff at the live head |
| `rotate` | `request-rotation` | not an admission: the separately locked rotation run |

Common checks: the run's `github.sha` is the live default head, `canonical_branch` equals the
API's default branch, and the adapter lists the targets. The time windows below are the
`admission` values of `site/mod-base.json` (`coalesce_seconds` 600, `partial_deadline_seconds`
2700, `recovery_interval_seconds` 3600 and `max_failed_publications` 3 in Quick Skin); the
`progress` reasons apply only in `admission.mode: "progress"`. Reasons:

| Reason | Publishes | Meaning and operator action |
|---|---|---|
| `stale-implementation` | no | a newer default head exists; its own runs publish. None. |
| `stale-wake` | no | the wake names a commit that is no longer the live head. None. |
| `current` | no | every key is already cached at its subject and every family is current. None. |
| `deferred-active-source` | no | a producer run for a subject head is still active; its wake or the hourly sweep follows. Wait. |
| `awaiting-complete-v1-evidence` | no | cutover: some key has neither a v1 handoff nor a v1 cache; the old site stays. Dispatch the producer E2E. |
| `ordinary-handoffs-pending` | no | progress mode: the current generation's handoffs are not complete yet. Wait. |
| `complete` / `unchanged` | no | nothing new since the last successful publication. None. |
| `publisher-active` | no | another publication is running; the next check follows it. None. |
| `publication-recovery-budget-exhausted` | no | `max_failed_publications` failed publications without newer readiness stopped automatic fan-out. Fix the cause, then dispatch `operation=manual`. |
| `initial-ordinary` | yes | the first publication of this head. |
| `ordinary-replacement` | yes | a complete newer attempt of the same head replaces the published one. |
| `final-complete` | yes | every expected key is ready. |
| `partial-deadline` | yes | the oldest unpublished ready key waited `partial_deadline_seconds`; a partial site is published. |
| `half-coverage` | yes | fewer than half of the keys are published, at least half are ready, and the oldest unpublished one waited `coalesce_seconds`. |
| `coalescing` | no | progress mode is waiting for more keys within its window. None. |
| `family-wake` | yes | an authenticated family wake. |
| `always` / `manual` | yes | admission mode `always` with an authenticated wake or stale key; an operator run. |

## Kill switch

Disable the caller in the affected mod. The deployed site stays online, producers keep running,
and their dispatches find the workflow disabled; enabling it resumes with the next wake or hour.

```bash
gh workflow disable pages.yml -R "$QS"      # or "$BP"
gh workflow enable pages.yml -R "$QS"
```

## Recovery commands

```bash
# Publish now (operator recovery; ignores active-source deferral)
gh workflow run pages.yml -R "$QS" --ref master -f operation=manual

# Regenerate ordinary evidence
gh workflow run on-demand-e2e.yml -R "$QS" --ref master -f capture_coverage=full
gh workflow run on-demand-e2e.yml -R "$BP" --ref master

# Retry the rotation of a successful Pages run (at most 64 deletions per run, longest-lived first;
# a retry continues where the budget stopped, keeping the promotion until the end, and also drains
# older generations' deferred caches; an already rotated or expired generation is a green no-op)
gh workflow run pages.yml -R "$QS" --ref master -f operation=rotate -f run_id=<pages-run-id> -f sha=<its-head-sha>

# Inspect a run's jobs, the admission reason and the kit identities
gh run view <run-id> -R "$QS"
gh api "repos/$QS/actions/runs/<run-id>" --jq '[.head_sha, (.referenced_workflows[] | .path)]'
curl -fsS https://the-plum-team.github.io/Quick-Skin-Mod/build.json

# List the kit's artifacts (apart from a Pages run's own github-pages artifact, the kit never reads
# or deletes any other name)
gh api --paginate "repos/$QS/actions/artifacts?per_page=100" \
  --jq '.artifacts[] | select(.name | startswith("mb-")) | [.id, .name, .created_at, .expired] | @tsv'

# Pin back to the previous kit release (on a branch, as an ordinary pull request)
python3 scripts/ci/mod_base_kit.py bump --to vPREVIOUS

# Discard a corrupted local kit cache entry (macOS path shown; see MOD_BASE_CACHE_DIR)
rm -rf ~/Library/Caches/mod-base/<sha>
```

Reverting a mod's cutover is `git revert` of its adoption merge (in Block Pops, through a
controller-upgrade pull request). The kit never read or deleted the old `pages-*` artifacts, so the
old pipeline needs only one fresh E2E dispatch; the `mb-*` artifacts expire on their own. Tag
`pre-mod-base-gallery` in each mod keeps the retired historical paths auditable.
