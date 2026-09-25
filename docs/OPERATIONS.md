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

1. Merge the release change to `main` through a pull request; it sets `__version__` in
   `src/mod_base/__init__.py` to `X.Y.Z` and refreshes the staged-file lock, then the digest literal
   (`PYTHONPATH=src python3 -m mod_base.template.lock --write`, then
   `python3 tools/update_tree_digest.py --write`).
2. Wait for `mod-base CI` on that commit, then run the canary against it (below).
3. Tag the exact commit and push the tag:

   ```bash
   git fetch origin main && git switch --detach origin/main
   git tag -a vX.Y.Z -m "mod-base vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. Publish the release notes with `gh release create vX.Y.Z -R "$KIT" --verify-tag --notes-file notes.md`,
   listing schema and kind changes, managed-file changes, adapter-protocol changes and the
   `pixel_metrics_version`.

A tag is never moved or deleted. A faulty release is fixed forward by `vX.Y.Z+1`, or mods pin back
to the previous tag with an ordinary pull request.

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
`/controller-upgrade approve <sha>`. Every bump runs the mod's complete gates.

## Canary procedure

The canary is a separate public caller repository, so it exercises exactly the cross-repository
resolution real mods use. It publishes only synthetic evidence under a "Synthetic demonstration
evidence — not a product" banner.

1. Check out the kit at the tag under test and create the canary working tree:

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

   A later canary cycle moves to a newer tag with `python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z`.

4. Commit, add the remote, push `main`, then finish P3 (Pages and environment).
5. Run the cycles required by stage S1, observing each item G1–G7 below:
   - three full generations: `gh workflow run canary-producer.yml -R "$CANARY" --ref main`, each
     followed by its Pages run (deploy, finalize) and its separate rotation run;
   - one forced head move (G7), one producer re-run attempt (`gh run rerun <producer-run> -R "$CANARY"`),
     one family-less generation and one synthetic family generation;
   - `canary-probe.yml` once, to record `job.workflow_sha`.

What to observe for each item:

| Item | Claim | How to observe |
|---|---|---|
| G1 | `referenced_workflows` lists `The-Plum-Team/mod-base/.github/workflows/<file>.yml@<sha>` with a matching `sha` for every callee, while the run is in progress and even for skipped callees | `gh api "repos/$CANARY/actions/runs/$RUN" --jq '[.status, (.referenced_workflows[] \| .path + " " + .sha)]'` during and after the run |
| G2 | Job names are `Publish / Admit publication`, `Publish / Collect <key>`, `Publish / Build atomic static site`, `Finalize / Refresh evidence cache for <key>`, `Rotate / Rotate the authenticated successful generation` | `gh api "repos/$CANARY/actions/runs/$RUN/attempts/1/jobs?per_page=100" --jq '.jobs[].name'` |
| G3 | The `github-pages` artifact uploaded by the callee `build` job deploys from the caller's `deploy` job under the `main` branch policy | `Deploy GitHub Pages` succeeds; `curl -fsS https://the-plum-team.github.io/mod-base-canary/build.json` shows the kit SHA |
| G4 | `GITHUB_WORKFLOW_REF` inside a callee is the caller's `.../pages.yml@refs/heads/main` | every callee's "Bind the two-part implementation identity" step succeeds (it asserts the value) |
| G5 | The composite tree check passes against the live trees API; admission sees the composite's upload window | the producer's "Verify the executing kit against the checked-out pin" succeeds; the admit summary nominates the handoff |
| G6 | `notify-pages` dispatches `pages.yml` holding only `actions: write`; rotation self-dispatches; `job.workflow_sha` value | the dispatched run appears in `gh run list -R "$CANARY" --workflow pages.yml --event workflow_dispatch`; the rotation run follows; `canary-probe.yml` log |
| G7 | A head that moves between build and deploy keeps the previous site | during a Pages run, `git commit --allow-empty -m "canary: move head" && git push`; `deploy` fails ("advanced before deployment") and `build.json` is unchanged |

Record every observation in [Canary evidence](#canary-evidence) with its run URL. The tag is
promoted to `v1.0.0` only when G1–G7 are all observed.

## Canary evidence

Fill one row per observation (run URLs are `https://github.com/The-Plum-Team/mod-base-canary/actions/runs/<id>`).

| Item | Kit tag / SHA | Run | Observed | Date |
|---|---|---|---|---|
| G1 | pending | | | |
| G2 | pending | | | |
| G3 | pending | | | |
| G4 | pending | | | |
| G5 | pending | | | |
| G6 | pending | | | |
| G7 | pending | | | |

`job.workflow_sha` observations (v1.1 may add it as a redundant cross-check only after three
observations equal `referenced_workflows`):

| Run | `job.workflow_sha` | `referenced_workflows[].sha` | Equal |
|---|---|---|---|
| pending | | | |

## Rollout

| Stage | Action | Exit criterion |
|---|---|---|
| S0 | create and push mod-base; apply P2; CI green | `Test`, `Workflow policy`, `Front end` required and green on `main` |
| S1 | tag `v0.9.0`; create and run the canary (P3) | G1–G7 recorded above |
| S2 | open the mod drafts pinned to `v0.9.0` (Quick Skin `feat/mod-base`, Block Pops PR A and PR B) | every local verification green |
| S3 | tag `v1.0.0` (same commit when unchanged) and re-pin the drafts | canary green at `v1.0.0` |
| S4 | Block Pops first: apply P1, mark PR A ready, record `verify --network`, owner approval, merge, dispatch E2E | the `master` key is live at `/e2e/`, a rotation run is green, visual review finds `mb-anchor--` |
| S5 | Quick Skin: ready or batch PR; full Build and E2E; merge | all keys live as `mb-cache--`, two rotations, one family publication, one baseline consumed |
| S6 | Block Pops PR B, then PR C clearing `template.deferred` | `template check` clean with an empty `deferred` |

Between PR A and PR C, Block Pops lists `.gitattributes`, `.gitignore`, `.github/dependabot.yml`,
`.github/pull_request_template.md` and `AGENTS.md` in `template.deferred`: the root files a
controller upgrade cannot change. `template check` then accepts their absence, still requires a
present deferred `.gitattributes` to be byte-identical, and prints only the required lines Block
Pops' existing `.gitignore` lacks (`_site/`, `public-evidence/`, `/.architectury-transformer/`) as
`pending` without failing; every other rule of a present deferred file (the `AGENTS.md` grammar,
the pull-request template markers, the Dependabot ignore) fails, so PR B must add those files
complete. PR A must still bring its own `.github/CODEOWNERS`
(a protected path) up to the fragment rules (`/AGENTS.md` and `/docs/ai/` owned), because
`CODEOWNERS` can never be deferred.

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

# Retry the rotation of a successful Pages run
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
