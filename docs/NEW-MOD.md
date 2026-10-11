# A new mod, end to end

The ordered steps that take a new Plum Team mod from an empty repository to the shared, protected
Build and packaged E2E gates. Each step names its command and what to check before going on.
[ONBOARDING.md](ONBOARDING.md) details the Pages part (steps 2 and 3),
[BUILD-ADAPTER.md](BUILD-ADAPTER.md) the Build adapter (step 6) and
[OPERATIONS.md](OPERATIONS.md#builde2e-activation-and-rollback) activation and rollback (steps 7
and 8). Quick Skin and Block Pops went this way in October 2026: compare the adapter pull requests
([Quick Skin #2078](https://github.com/The-Plum-Team/Quick-Skin-Mod/pull/2078),
[Block Pops #58](https://github.com/The-Plum-Team/Block-Pops-Minecraft-Mod/pull/58)) and the
shadow pull requests ([Quick Skin #2079](https://github.com/The-Plum-Team/Quick-Skin-Mod/pull/2079),
[Block Pops #59](https://github.com/The-Plum-Team/Block-Pops-Minecraft-Mod/pull/59)).

Already in place, nothing to do per mod:

- The gate status App `plum-mod-base-gate` (App id 5256389, "Commit statuses: Read and write"
  only) is installed on every repository of the organization, so a new repository has it from
  the start. What a mod needs is its credentials in a protected environment (step 5).
- The [Plum Maven mirror](https://github.com/The-Plum-Team/maven-mirror) serves the openly
  licensed Fabric, Architectury, Stonecutter, NeoForge and Forge artifacts (step 4).

Run the commands from Git Bash or a Linux shell with `gh` logged in as an organization owner. On
Windows use `python` for `python3`, and set `MSYS_NO_PATHCONV=1` before a `gh` command whose
arguments start with `/`.

```bash
MOD=The-Plum-Team/<mod-repository>
BRANCH=main                     # the default branch; it is site/mod-base.json "canonical_branch"
KIT_TAG="$(gh release list -R The-Plum-Team/mod-base --exclude-drafts --exclude-pre-releases \
  --limit 1 --json tagName --jq '.[0].tagName')"   # the newest kit tag with a GitHub Release
```

Pin only a tag that has a GitHub Release: a tag without one has not passed its canary
([OPERATIONS.md](OPERATIONS.md#releasing-mod-base)).

## 1. Create the repository and protect its default branch

```bash
gh repo create "$MOD" --public --description "<one line>" --clone
cd "${MOD#*/}" && git symbolic-ref HEAD "refs/heads/$BRANCH"
# ... first commit (README, build), then:
git push --set-upstream origin "$BRANCH"
gh api -X PUT "repos/$MOD/actions/permissions" --input - <<'JSON'
{"enabled": true, "allowed_actions": "all", "sha_pinning_required": true}
JSON
gh api -X PUT "repos/$MOD/actions/permissions/workflow" --input - <<'JSON'
{"default_workflow_permissions": "read", "can_approve_pull_request_reviews": false}
JSON
gh api -X POST "repos/$MOD/rulesets" --input - <<'JSON'
{"name": "default branch", "target": "branch", "enforcement": "active", "bypass_actors": [],
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
   {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
     "dismiss_stale_reviews_on_push": false, "require_code_owner_review": false,
     "require_last_push_approval": false, "required_review_thread_resolution": true}}]}
JSON
```

Check: `gh api "repos/$MOD/rules/branches/$BRANCH" --jq '.[].type'` lists `deletion`,
`non_fast_forward` and `pull_request`. From here on every change lands through a pull request.
The protected default branch is what keeps the gate App key safe (see
[Security](#security-the-app-key-lives-only-in-default-branch-environments)). Add the mod's own
CI checks to the ruleset's `required_status_checks` once they have reported on a pull request.

## 2. Add the kit files

Write `site/mod-base.json` from the kit's `template/seed/site/mod-base.json.tmpl` (fields in
[SCHEMAS.md](SCHEMAS.md)), with `"canonical_branch"` set to `$BRANCH`. Then seed every managed,
fragment and seeded file from a clean kit checkout at `$KIT_TAG`:

```bash
git clone https://github.com/The-Plum-Team/mod-base.git ../mod-base-$KIT_TAG
git -C ../mod-base-$KIT_TAG switch --detach "$KIT_TAG"
git switch --create chore/adopt-mod-base
PYTHONPATH=../mod-base-$KIT_TAG/src python3 -P -m mod_base template init --repo . --seed
python3 scripts/ci/mod_base_kit.py verify --network
python3 scripts/ci/mod_base_kit.py run template check --repo .
```

`init` never overwrites a file; it pins `.github/workflows/pages.yml` to the kit commit and prints
the placeholders left to fill (`LICENSE`, `.github/CODEOWNERS`, `CONTRIBUTING.md`,
`docs/ai/PROJECT.md`). Check: `verify --network` and `template check` exit 0. A later kit release
is adopted with `bump` ([OPERATIONS.md](OPERATIONS.md#bumping-the-kit-in-a-mod)), never by hand.

## 3. Pages and the evidence adapter

Follow [ONBOARDING.md](ONBOARDING.md) sections 1 and 3 to 7: the `github-pages` environment
(limited to `$BRANCH`), the Pages adapter `scripts/pages/mod_base_adapter.py`, the producer and
wake jobs, and the first publication. Check: `run conformance --repo . --keys <key>` passes
locally and the first `Project site` run deploys `/build.json` with both identities.

## 4. Maven mirror and dependency verification

Put the mirror first in `settings.gradle.kts` (`pluginManagement` and dependency repositories),
limited to the mirrored groups, with the upstream repositories after it. It answers 404 for
everything else, so Gradle falls back unchanged:

```kotlin
val plumMirrorSwitch = providers.gradleProperty("plum.mavenMirror").orNull
require(plumMirrorSwitch == null || plumMirrorSwitch == "true" || plumMirrorSwitch == "false") {
    "plum.mavenMirror must be true or false, not '$plumMirrorSwitch'"
}
if (plumMirrorSwitch != "false") {
    maven("https://the-plum-team.github.io/maven-mirror/") {
        name = "PlumMavenMirror"
        metadataSources { gradleMetadata(); mavenPom() }   // the mirror holds .module files, not stub .poms
        mavenContent { releasesOnly() }
        content { listOf("net.fabricmc", "net.fabricmc.fabric-api", "dev.architectury", /* ... */)
            .forEach { includeGroup(it) } }
    }
}
// ... the upstream repositories, unchanged
```

Copy the complete group list and the project-level script from Quick Skin's
`settings.gradle.kts` and `gradle/maven-mirror.gradle.kts`; the mirror's
[README](https://github.com/The-Plum-Team/maven-mirror#using-it-from-gradle) explains the per-version
fallback. `-Pplum.mavenMirror=false` (or `plum.mavenMirror=false` in `gradle.properties` through a
pull request, since shared Build hooks get no runner variables) is the emergency switch.

Then:

1. Commit strict Gradle dependency verification (`gradle/verification-metadata.xml`, never
   `org.gradle.dependency.verification=off`). The shared gates and any seed rely on it, and it is
   the final check of every file the mirror serves.
2. Open a pull request in `The-Plum-Team/maven-mirror` that adds
   `{"repository": "$MOD", "ref": "$BRANCH", "path": "gradle/verification-metadata.xml"}` to
   `sources.json` and the result of `python3 scripts/mirror.py update-list`. Review every new hash
   in `artifacts.lock`; a new group or license is a `policy.json` change that cites the license.
3. Check the size in the `Publish mirror` summary: the site must stay below 90 % of the 1 GB Pages
   limit. Past that, drop versions no mod builds before adding more.

Check: a clean build with `--refresh-dependencies` passes with the mirror and with
`-Pplum.mavenMirror=false`.

## 5. The `mod-base-gate` environment (owner, once per mod)

The status caller mints its token from the variable `MOD_BASE_GATE_APP_CLIENT_ID` and the secret
`MOD_BASE_GATE_APP_PRIVATE_KEY` of the environment `mod-base-gate`, which must admit the default
branch alone. `tools/new_mod_env.py` creates the environment, limits it to the default branch,
reads that restriction back, and only then stores both values. The key goes to
`gh secret set` on standard input and is never printed:

```bash
python3 ../mod-base-$KIT_TAG/tools/new_mod_env.py --repo "$MOD" --branch "$BRANCH" \
  --app-json ~/.mod-base-secrets/mod-base-gate-app.json --dry-run    # reads only, prints the plan
python3 ../mod-base-$KIT_TAG/tools/new_mod_env.py --repo "$MOD" --branch "$BRANCH" \
  --app-json ~/.mod-base-secrets/mod-base-gate-app.json
```

(`--client-id <id> --key-file app.pem` replaces `--app-json` for a key downloaded from the App's
settings; the tool needs a kit checkout that has it, or run it from a clone of `main`.) It refuses,
before writing anything, an environment that admits another branch or has required reviewers or a
wait timer, a `--branch` that is not the default branch, and a repository or organization secret
of the key's name. The manual equivalent is in
[OPERATIONS.md](OPERATIONS.md#the-gate-status-app). Check:

```bash
gh api "repos/$MOD/environments/mod-base-gate/deployment-branch-policies" --jq '.branch_policies[].name'  # exactly $BRANCH
gh variable list --repo "$MOD" --env mod-base-gate     # MOD_BASE_GATE_APP_CLIENT_ID
gh secret list --repo "$MOD" --env mod-base-gate       # MOD_BASE_GATE_APP_PRIVATE_KEY
gh secret list --repo "$MOD"                           # must NOT list the key
```

Do this before the shadow pull request (step 7) merges: until then every status run fails in
`publish` and publishes nothing.

## 6. The Build adapter, `disabled`

One pull request adds the protected Build config, the adapter and the activation manifest in
`disabled` mode ([BUILD-ADAPTER.md](BUILD-ADAPTER.md); the synthetic mod in the kit's
`tests/fixtures/ci_mod/` is a complete working example):

- `scripts/ci/mod_base_build_dispatch.py`, the adapter module and the policy entry point, with the
  eight hooks `derive_plan`, `policy`, `build_target`, `verify_target`, `verify_build`,
  `derive_runtime`, `run_lane` and `verify_runtime`, plus the mod's tests of them;
- `scripts/ci/mod-base-build.json`: `repository` `$MOD`, `profile` (only `quick-skin` and
  `block-pops` exist; take `block-pops`, whose policy run counts tests exactly, unless the policy
  suite needs Quick Skin's whole-class skips), `adapter.files` with the SHA-256 of every file the
  dispatcher imports (`sha256sum scripts/ci/*.py`), `contexts.build` and `contexts.packaged`
  (names no other check uses, for example `<Mod> / Build and verify`), the four `timeouts` and,
  for Minecraft client lanes, `"runtime": {"system_profile": "xvfb-mesa"}`. Leave `seeds` out
  until the owner has checked the conditions in
  [OPERATIONS.md](OPERATIONS.md#protected-seeds);
- `site/mod-base-build-activation.json`:

  ```json
  {"kind": "mod-base.ci.activation", "schema_version": 1, "repository": "The-Plum-Team/<mod-repository>",
   "profile": "block-pops", "mode": "disabled", "rollback_from": null}
  ```

Check, with `../base` a detached worktree of `origin/$BRANCH`:

```bash
python3 scripts/ci/mod_base_kit.py run template activation --repo .                  # state: disabled
python3 scripts/ci/mod_base_kit.py run template check --repo .                       # exit 0
python3 scripts/ci/mod_base_kit.py run template transition --repo . --base ../base   # exit 0
```

and the mod's adapter tests pass. `disabled` manages no caller: nothing runs on GitHub yet.

## 7. `disabled` to `shadow`

A pull request of its own, at the same pin: set `"mode": "shadow"`, render the four callers, and
add `actions/create-github-app-token` to the `ignore` list of every `github-actions` update in
`.github/dependabot.yml` (a seeded mod has it):

```bash
git worktree add --detach ../base "origin/$BRANCH"
python3 scripts/ci/mod_base_kit.py run template sync --repo . --write   # mod-base-{guard,build,packaged-e2e,gate-status}.yml
python3 scripts/ci/mod_base_kit.py run template check --repo .
python3 scripts/ci/mod_base_kit.py run template transition --repo . --base ../base   # disabled -> shadow
```

The callers run from the default branch's copy (`pull_request_target`, `workflow_run`), so the
first shadow generation is the next ready pull request after this one merges; a draft gets only
the deferral jobs. On it, check:

```bash
gh run list -R "$MOD" --commit "$HEAD_SHA" --json workflowName,conclusion   # guard, Build, Packaged E2E, gate status
gh api "repos/$MOD/commits/$HEAD_SHA/statuses" \
  --jq '.[] | select(.context | endswith(" (shadow)")) | [.context, .state, .creator.login] | @tsv'
gh api rate_limit --jq .resources.core                                       # request budget
```

Both `<context> (shadow)` statuses come from `plum-mod-base-gate[bot]`. No ruleset may require a
shadow name. Leave shadow when the shared gates are trusted: at least five ready pull requests
with both statuses `success`, a deliberately broken test pull request that gets `failure`, a green
push generation on `$BRANCH` after a merge, and no unexplained infrastructure failure.

## 8. `shadow` to `shared-build-and-e2e`

Another pull request at the same pin: set `"mode": "shared-build-and-e2e"` (or `shared-build`
first, then `git rm .github/workflows/mod-base-packaged-e2e.yml`, if the packaged lanes are not
ready), `template sync --write`, `template check`, `template transition --base ../base`.

Right after it merges, the owner makes the two contexts required from the App, so that no other
writer of a status with the same name satisfies them (before the merge, the pull request itself is
still gated by shadow, which publishes only the ` (shadow)` names). With the two names of
`scripts/ci/mod-base-build.json`:

```bash
RULESET="$(gh api "repos/$MOD/rulesets" --jq '.[] | select(.name == "default branch") | .id')"
gh api -X PUT "repos/$MOD/rulesets/$RULESET" --input - <<'JSON'
{"name": "default branch", "target": "branch", "enforcement": "active", "bypass_actors": [],
 "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
 "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
   {"type": "pull_request", "parameters": {"required_approving_review_count": 0,
     "dismiss_stale_reviews_on_push": false, "require_code_owner_review": false,
     "require_last_push_approval": false, "required_review_thread_resolution": true}},
   {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": true,
     "do_not_enforce_on_create": false, "required_status_checks": [
       {"context": "<contexts.build>", "integration_id": 5256389},
       {"context": "<contexts.packaged>", "integration_id": 5256389}]}}]}
JSON
gh api "repos/$MOD/rules/branches/$BRANCH" --jq '.[] | select(.type == "required_status_checks") | .parameters'
```

Keep any check of the mod's own CI that step 1 made required in that list.
Check: the next pull request shows both contexts, without the suffix, from `plum-mod-base-gate[bot]`,
and cannot merge until they are `success`. A rollback is two pull requests through
`reviewed-rollback` ([OPERATIONS.md](OPERATIONS.md#builde2e-activation-and-rollback)).

## Security: the App key lives only in default-branch environments

- The App is installed on every repository of the organization, so its key can mint a token that
  writes commit statuses, required gate contexts included, on any of them. The managed caller
  scopes its token to the mod's own repository, but anyone holding the key is not bound by that.
- The key lives only in the `mod-base-gate` environment of a mod, limited to that mod's default
  branch, with no reviewers and no wait timer. Never store it as a repository or organization
  secret, never in a canary or test repository, never in a workflow log, and never print it.
- The branch restriction is the only thing that keeps the key from a workflow of another branch:
  anyone who can push a branch can add a job that names the environment. It is safe only because
  the default branch takes changes through reviewed pull requests alone (step 1's ruleset, no
  bypass actors).
- Keep the owner's key file private (`~/.mod-base-secrets/`, owner-only permissions). If the key
  may have leaked, generate a new one in the App's settings, run `tools/new_mod_env.py` for every
  mod, then delete the old key in the App's settings.
