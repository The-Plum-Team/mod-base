# Security model

This document states what mod-base protects, from whom, and with which mechanism. It complements
[ARCHITECTURE.md](ARCHITECTURE.md); the owner-side settings it depends on are applied through
[OPERATIONS.md](OPERATIONS.md). Report a vulnerability privately as described in the repository's
`SECURITY.md`, never in a public issue.

## Assets and assumptions

- **Assets:** the public evidence sites of the mods (what they claim was tested), the mods'
  `github-pages` deployments, their Actions artifacts and caches, and the integrity of the mods'
  default branches, which the kit's managed files and pins are part of.
- **Assumptions:** the mods and mod-base are public GitHub repositories run on GitHub-hosted
  runners; one maintainer holds admin rights; GitHub correctly enforces rulesets, environment
  branch policies, `permissions:` and immutable tags; SHA-1 commit identity is not forged.
- **Advisory surface:** Pages publication never replaces a mod's required Build and packaged E2E
  gates. Every failure mode below fails closed and keeps the previously deployed site.

## Trust roots

| Asset | Who may change it | Protection |
|---|---|---|
| mod-base `main` | a pull request that passes `Test`, `Workflow policy` and `Front end` | ruleset "mod-base main": no deletion, no force push, pull request with thread resolution, strict required checks, no bypass actors; CODEOWNERS |
| mod-base `v*` tags | the maintainer, on a `main` commit whose `mod-base CI` is green; mods pin a tag only after the canary, pinned to it, passes | ruleset "mod-base immutable tags" (no deletion, no force push, no update) plus immutable releases |
| The pin in a mod | a protected pull request to the mod's default branch | the mod's default-branch ruleset; in Block Pops every pin change is also a controller upgrade |
| Adapter and config (`scripts/pages/mod_base_adapter.py`, `site/mod-base.json`) | the same | the same; both sit under Block Pops protected roots |
| Managed files | only `template sync` from the pinned kit | `template check` fails on any byte of drift; `template.deferred` can never name the caller, the bootstrap, the shared documents or `CODEOWNERS` |
| The `github-pages` environment | repository admins | deployment branch policy: the default branch only |

Changing privileged publication code therefore always takes two protected merges: one to
mod-base `main` (released as a tag) and one pin bump in the mod.

## Threats and defenses

### Impostor commits from the fork network

GitHub resolves `owner/repo@<sha>` across the whole fork network, so a commit that exists only in
a fork can be named under the kit's repository. Four independent layers stop it:

1. **Pre-merge in Quick Skin:** the Build `policy` job runs
   `python3 scripts/ci/mod_base_kit.py verify --network`: the pin must be reachable from mod-base
   `main` (`compare/<pin>...main` is `ahead` or `identical` with `behind_by == 0`) and its
   `# vX.Y.Z` tag must peel to exactly that commit. This runs as pull-request code, at the same
   trust level as every Quick Skin policy test; review and rulesets remain the defense against a
   malicious pull request.
2. **Pre-merge in Block Pops, controller-side:** the trusted `identity` job runs master's
   bootstrap against the candidate's pin, read as inert bytes. For the adoption pull request,
   whose master has no bootstrap yet, the owner runs the same command locally and records the
   output in the pull request.
3. **Runtime, caller-owned:** `verify-kit` in the mod's own caller takes the kit SHA from the run's
   `referenced_workflows`, requires it to equal every pin in `pages.yml` at `github.sha`, and checks
   reachability from mod-base `main`, before any kit job starts. An impostor callee cannot skip a
   check that lives in the caller.
4. **Monotonic reachability:** mod-base `main` forbids force pushes and deletion and tags are
   immutable, so a pin that was reachable stays reachable, and a fork commit becomes reachable only
   through a protected merge.

Each kit job additionally recomputes the kit tree digest of its checkout and compares it with the
literal compiled into the kit workflow, which catches any mismatch between `referenced_workflows`
and the commit actually checked out. The kit's own tests forbid `uses: The-Plum-Team/mod-base`
references inside the kit outside `canary/`.

### A compromised or faulty kit release

- Kit YAML never holds `pages: write` or `id-token: write`: only the caller's `deploy` job does,
  and it has no checkout and runs only the mod-owned head recheck plus the pinned `deploy-pages`
  action. A bad kit release can at worst produce a bad site artifact, which the mod's recheck still
  gates.
- The kit's only write scope is `actions: write` in `rotate.yml`, used to delete `mb-*` artifacts
  (and the owning run's own `github-pages` artifact) by exact id after the owning run is
  authenticated `completed/success`; it never touches any other artifact.
- Every kit change reaches a mod only through a reviewed pin bump, which runs the mod's complete
  gates, and a bump is revertible by an ordinary pull request.

### Hostile artifacts and API responses

Every artifact, JSON document, image and API response is treated as hostile:

- artifacts are downloaded only by immutable numeric id with name grammar, digest, size, owner run,
  attempt, expiry and upload window checked; redirects never carry credentials;
- archives are extracted with bounded entries, sizes and compression ratio, refusing symlinks,
  special files, encryption and traversal, and the result must equal the manifest's exact
  inventory;
- JSON is strict (no duplicate keys, NaN, Infinity or unknown keys) and size-bounded before
  parsing;
- images are fully decoded within a pixel bound and must have the exact configured size; pixel
  metrics and comparisons are recomputed, derivatives are re-encoded deterministically and must be
  byte-identical;
- source runs are authenticated through the exact attempt (path, event, head, repository,
  conclusion, display title, attestation job, job graph, newest run as configured), and live heads
  are rechecked at admission, collection, rendering and deployment.

### The mod adapter

The adapter is trusted code at the protected mod head, like every Pages script before it. Its
isolation is defense in depth against accidental token use, leaks and non-determinism, **not** a
sandbox against a malicious adapter:

- hooks run only in the read-only jobs `admit`, `collect`, `family`, `build` and in
  `prepare-evidence`; never in `verify-kit`, `deploy`, `finalize`, `request-rotation`, `rotate`,
  `notify-pages` or any job with a write scope;
- each hook runs in an `env -i` child with a fixed argument list, a timeout and a bounded,
  schema-validated response; only declared network hooks receive a read-only token, and only in
  those jobs;
- the kit re-verifies every hook result (R1–R6) before publishing, and a missing hook fails closed
  wherever it is required;
- other branches are fetched only as inert Git objects and read through bounded blob reads; they
  are never checked out or executed.

### Tokens and event data

- The managed caller and every kit workflow declare top-level `permissions: {}`, and each caller
  job grants exactly what its callee needs; a kit test requires every callee job's permissions to
  be a subset of the calling job's grant in the managed caller.
- Composite steps that execute Python, or parse producer output, first unset the Actions runtime
  and GitHub tokens; only the tree-verification and dispatch steps receive `GH_TOKEN`.
- Workflow inputs and event data reach scripts only through `env:` and are validated by exact
  pattern before any checkout; `pages.yml` accepts only `workflow_dispatch` and `schedule`, never
  `workflow_run`, `repository_dispatch` or `pull_request_target`.
- Wakes need only `actions: write` (a `workflow_dispatch`), never `contents: write`, and carry no
  authority: admission re-authenticates the named run and artifact and exits cleanly on a stale or
  foreign wake.

### Concurrency and races

- Publication and rotation use two separate locks (`mod-base-pages-publication`,
  `mod-base-pages-rotation`) owned by the caller; callees declare no concurrency.
- Rotation starts only after the owning Pages run is authenticated `completed/success`, re-observes
  each artifact immediately before deleting it by exact id, never deletes anything newer than its
  owner, keeps the newest anchor per key, and stops at a bounded deletion budget (64 exact-ID
  deletions per run, `limits.DELETION_BUDGET`). A collector that loses a race with rotation fails
  closed and the next wake or hourly sweep recovers.
- Build output is written into a private atomic directory, sealed, and rechecked before upload;
  `deploy` rechecks every source head, and a head that moved keeps the previous site.

### The bootstrap and local kit resolution

- The pin parser accepts only full-SHA references in one exact line form and rejects any other
  reference to the kit (tags, branches, other SHAs, quoted, escaped, folded or flow-style values,
  a `uses:` value on another line, and the bare kit repository outside a comment, as a checkout
  `repository:` would name it), so a workflow cannot quietly run a second kit version. Each line
  is also read with its YAML escapes decoded and, after an escaped line break, joined to the next
  line; YAML folds every other line break into a space or newline, so no reference can be spelled
  across lines unseen. Values assembled at run time by an expression or a shell command are outside
  any static parser; like every workflow change they are governed by review (layer 1 of the
  impostor defense).
- The caller's extension region is checked over a small YAML subset (no escapes, explicit keys,
  anchors or multi-line quoted scalars outside block scalars; plain job-level keys; balanced flow
  collections), and every job-level key is checked again with all its deeper lines joined, so a
  permission or `needs` cannot be split across lines to evade the rules.
- A staged overlay must carry a stamp whose SHA equals the pin and whose kit-digest-v1 matches the
  recomputed tree, and its `template/` and `tools/` (which `template check` reads but the digest
  does not cover) must equal the listing `src/mod_base/template/staged_files.sha256` inside the
  digested tree, and a staged `actions/` (which a mod's gate may check) the listing
  `src/mod_base/template/staged_actions.sha256` there; an `actions/` no digested lock binds is
  refused. No verified kit may hold bytecode: Python loads a planted `__pycache__` file in
  place of the verified source, so `kit_path()` turns bytecode writing off and an overlay or cache
  holding any is refused. The user cache must be a clean git checkout at the pin, outside the
  repository, where no ignore rule can hide an added file; a fetch is anonymous, runs no hooks or
  filesystem monitors, ignores system and global git configuration, and is verified by content
  address before it is published into the cache. A candidate that exists but fails verification
  is an error, never a fall-through. The cache is as
  trustworthy as the account that owns it: a local attacker who can rewrite it can equally rewrite
  the checkout that uses it.
- The unpinned developer override (`MOD_BASE_KIT_PATH` without `MOD_BASE_KIT_SHA`) requires
  `MOD_BASE_ALLOW_UNPINNED=1` and is refused whenever `CI` or `GITHUB_ACTIONS` is set.
- In Block Pops the trusted controller, never the candidate, chooses and verifies the kit it stages
  into the sandbox; a candidate pin different from the controller's must first pass the released
  checks of `verify --network`.

### Supply chain

- Every action is pinned to a full commit SHA; `sha_pinning_required` is recommended on the mod
  repositories and required on mod-base, whose Actions policy allows GitHub-owned actions only.
- Pillow is installed only from the 86-hash lock (`--require-hashes --only-binary=:all:`), kept in
  lockstep with the mods' own locks by tests.
- Dependabot ignores `The-Plum-Team/mod-base*`; the only bump path is the bootstrap's `bump`, which
  verifies the release before editing and re-synchronizes the managed files.

### The published site

Pages carry the meta policy `default-src 'none'; script-src 'self'; style-src 'self'; img-src
'self'; connect-src 'self'; font-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'`
and `referrer: no-referrer`. Scripts render with `textContent` only and load only local assets;
every run URL is built by the renderer from the repository and a numeric run id; configuration
text is validated and HTML-escaped at build time.

## Residual risks and limits

- **Malicious adapter or configuration at the protected head:** trusted by design; the defense is
  the mod's review and rulesets. Block Pops `master` has no ruleset until precondition P1 is
  applied ([OPERATIONS.md](OPERATIONS.md#p1-ruleset-block-pops-master)), and its adoption pull
  request is not marked ready before that.
- **Single maintainer:** one compromised account can merge to both repositories. Immutable tags,
  reachability checks, two protected merges, secret scanning and push protection limit, but do not
  remove, that risk.
- **Unverified platform behavior:** the cross-repository shape of `referenced_workflows`, the
  `Publish / ...` job names of matrix legs (and, for a mod without families, the unexpanded name of
  its skipped `family` matrix job, which only a Block Pops run can show), deploying a
  callee-uploaded artifact from the caller, and `GITHUB_WORKFLOW_REF` in a callee are proven only by
  the canary ([OPERATIONS.md](OPERATIONS.md#canary-evidence)). Each fails closed while unproven.
- **`job.workflow_sha`** is not used in v1; it becomes a redundant cross-check only after three
  values observed inside a kit callee job equal `referenced_workflows` (the canary probe sees only
  its own caller-side value, the canary head, so that observation is deferred to v1.1).
  `uses: $/...` is never used.
- **Dependabot's `ignore` wildcard** for reusable-workflow references is unverified; a stray pull
  request fails `template check` and is closed.
- **`frame-ancestors`** cannot be set from a meta tag and GitHub Pages sets no response headers, so
  the site can be framed.
- **Pre-merge verification in Quick Skin** runs as pull-request code; the runtime `verify-kit` is
  the defense that does not depend on it.
