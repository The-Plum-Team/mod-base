# Shared repository contract

This file is managed by mod-base (https://github.com/The-Plum-Team/mod-base). It is byte-identical
in every mod that pins the kit, it is the first import of that mod's `AGENTS.md`, and
`template check` fails on any local edit. Change it in mod-base, release a kit tag, then bump the
mod's pin (see "The mod-base kit" below). Together with [PUBLIC-EVIDENCE.md](PUBLIC-EVIDENCE.md)
and the mod's own imported documents, it is the source of truth for coding agents working anywhere
below the repository root. A mod-local document may narrow these rules; it never relaxes them.

## Instruction documents

- `AGENTS.md` is an import-only manifest: exactly one `@path.md` per line and nothing else. Its
  first two lines are `@docs/ai/shared/REPOSITORY.md` and `@docs/ai/shared/PUBLIC-EVIDENCE.md`;
  the remaining lines are the mod's `site/mod-base.json` `template.agents_local`, in order, and
  every imported file exists. `template check` enforces this grammar.
- There is deliberately no `CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md`: Claude Code reads
  `AGENTS.md` and expands its imports only while none of them exists. `template check` fails when
  one appears.
- Do not put operational rules directly in `AGENTS.md`, and do not create another root instruction
  file that restates this contract. Add a nested `AGENTS.md` only when a directory genuinely needs
  narrower rules, and keep it limited to imports of those local deltas.
- `CONTRIBUTING.md` is the human path from a fresh fork to a reviewed pull request, kept aligned
  with `.github/pull_request_template.md`; `README.md` is for users and builders; focused documents
  own their subjects; `docs/architecture/decisions/` records evidence-backed decisions that must
  survive individual worktrees.
- Update the appropriate imported document whenever source routing, ownership, lifecycle
  composition roots, security boundaries or mandatory verification commands change.

## Starting a task

Choose the target before editing:

1. Inspect `git status --short --branch` and preserve existing work.
2. Read the mod's authoritative release inventory (its local project document names it); never
   infer supported versions or loaders from directory or branch names.
3. Read the focused document and the build file of the module you will change.
4. Search the owning sources and every active copy or version overlay of the affected path or
   symbol; changing one copy does not fix a target that another copy replaces.
5. State the intended scope and run the smallest check that can disprove the change while
   iterating.

New work targets the default branch unless the mod's local documents name an explicit historical
recovery path. Generated output and staged artifacts are never edited: fix the tracked input
instead. Never develop directly on automation-owned branches; they are disposable workflow heads.
If intended behavior excludes a target, make the exception explicit in its owning module or
adapter and document the decision, instead of spreading broad version conditions across unrelated
code or creating a second version inventory.

## Editing workflow

- Read `CONTRIBUTING.md` when preparing a human-facing branch, commit or pull request.
- Read the release inventory and the owning module's build file before changing versions, loaders,
  source roots, resources, artifact tasks or end-to-end coverage.
- Preserve unrelated working-tree changes. Do not rewrite or delete user work to simplify a patch.
- Never switch or repurpose a user's existing checkout merely to inspect or edit a different
  branch. Fetch that branch and create a separate ephemeral Git worktree; inside it, reread
  `AGENTS.md`, every imported document and that branch's release inventory before acting.
- Remove an ephemeral worktree only after `git status --short` is empty and every valuable change
  belongs to a named branch that is committed, pushed or otherwise exported. A detached-HEAD commit
  alone is not preserved. Let `git worktree remove` refuse dirty trees; never use `--force` to
  erase a dirty or user-owned worktree, and discard work only with the user's explicit
  authorization.
- Do not commit generated JARs, staged release files, Minecraft runtime directories, screenshots,
  caches, public-evidence builds or IDE output.
- Keep production and test-harness JARs physically separate. A harness may compile against the
  production output but never packages production classes.
- Do not run several Gradle invocations concurrently on one machine or checkout: Architectury keeps
  JVM-global transform state. Separate targets may build concurrently only on isolated CI runners,
  each with its own checkout and a serial JVM.
- Keep each commit to one reviewable concern with an imperative conventional subject: `feat:`,
  `fix:`, `refactor:`, `test:`, `build:`, `docs:`, `ci:` or `chore:`.
- Before committing, inspect the staged diff, run `git diff --check` and `git diff --cached --check`,
  and confirm that no generated or unrelated file is staged. Commit, amend, rebase, push,
  force-push, open a pull request or merge only when explicitly asked.
- Never rewrite commits that may belong to the user or another contributor. Updating an unshared
  topic branch may use a rebase when asked; a shared branch takes a non-destructive merge or a
  fresh topic branch.
- A pull request title uses the same conventional form. Its body records scope, validation, risks,
  generated-output status and material AI assistance.

## Workflows, dependencies and credentials

- Every remote `uses:` (`owner/repo[/path]@ref`) is pinned to a full 40-hex commit SHA followed by
  `# vX.Y.Z`; never a tag, branch or abbreviated SHA. Local `./` references need no pin. Every
  mod-base reference carries the one kit pin, written on one plain line (see below).
- A new workflow declares top-level `permissions: {}`, and every job grants exactly the scopes it
  uses; write scopes live in the fewest, smallest jobs. Never widen an existing workflow's
  top-level grant. Checkouts use `persist-credentials: false` unless a later step of that same job
  must authenticate Git itself.
- Event data and workflow inputs reach shell steps only through `env:`; never interpolate
  `${{ github.event.* }}` or `${{ inputs.* }}` into a `run:` script. Validate inputs by exact
  pattern before use.
- Install dependencies only from committed hash-locked files (`pip install --require-hashes
  --only-binary=:all:` for Python). A dependency bump is one coordinated change of every lock that
  pins it; first-download trust is forbidden.
- Scripts treat every file, artifact, image and API response as untrusted: bounded reads, strict
  JSON, no symlink following, no `shell=True`, no `eval`, and one bounded error line on failure.
- No credential or write-scoped token ever reaches a step that executes code under review: pull
  request heads, candidate trees and downloaded artifacts are checked out and run with credentials
  disabled. Publication credentials live only in protected deployment environments.
- A protected writer that commits on behalf of automation runs on a fresh runner, recomputes and
  compares the exact tree it is about to write using only protected policy, creates commits with an
  explicit bot identity through `git commit-tree` (never hooks), and never executes candidate
  scripts.

## AI-credential steps

- A workflow step that receives an AI credential runs a pinned CLI with safe mode, no session
  persistence or prompt history, `dontAsk`, an explicit shell-free `--tools` set, and scoped
  `Read`/`Edit`/`Write` permission rules.
- Install that CLI only from package and lock files materialized from the protected workflow
  commit, with lifecycle scripts disabled and only the reviewed pinned installer invoked
  explicitly. Never load project hooks, MCP servers, agent configuration or package metadata from
  the checkout that supplies logs or sources for analysis.
- Treat AI failure evidence as an adversarial payload: authenticate the source run, cap its log,
  select only named artifacts by immutable numeric id, bound their count and compressed size, and
  extract them with a protected traversal/link/entry/expanded-size validator. The model gets
  read-only access to that evidence; repair writes are positively limited to production sources and
  can never persist agent configuration.
- Model output is untrusted data. Protected code validates it against a schema derived from the
  exact input and normalizes it before anything is uploaded; raw provider text is never uploaded.

## The mod-base kit

mod-base supplies the public-evidence pipeline, the GitHub Pages publisher and the managed
repository files. Its code runs in this repository only at the single pinned commit, so changing
privileged publication code always takes two protected merges: one to mod-base `main` (released as
an immutable `vX.Y.Z` tag) and one pin bump here.

**The pin.** Every mod-base reference in `.github/workflows/*.yml` and `.github/actions/*/action.yml`
has the form `uses: The-Plum-Team/mod-base/<path>@<40-hex> # vX.Y.Z`, and all of them carry the same
SHA and version. Any other reference to the kit repository in those files is an error, however it
is spelled: another ref, a checkout `repository:` naming the kit, a quoted, escaped, folded or
flow-style value, or a `uses:` value that is not on its own line. Name the kit repository only in
a pin line, a comment, or followed by a path (as in an API URL).

```bash
python3 scripts/ci/mod_base_kit.py pin                # print "<sha> <version>"
python3 scripts/ci/mod_base_kit.py verify --network   # released tag == pin, reachable from mod-base main
python3 scripts/ci/mod_base_kit.py run template check --repo .
```

**Managed files** are byte-identical to the pinned kit and never edited here: `.gitattributes`,
`scripts/ci/mod_base_kit.py`, `docs/ai/shared/REPOSITORY.md`, `docs/ai/shared/PUBLIC-EVIDENCE.md`
and the managed region of `.github/workflows/pages.yml`. Only the pin and the caller's marked
extension region (jobs named `ext-*`, with no mod-base reference, no `pages`/`id-token`
permission, no `actions: write` and never needing `rotate`) are local. Write extension jobs in
plain YAML: block-mapping jobs with plain job-level keys, no escapes, explicit keys, anchors or
multi-line quoted scalars outside `run: |`-style block scalars, and flow collections that close on
their own key. Managed documents link only to each other or to absolute `https://` URLs.

**Fragment files** (`.gitignore`, `.github/CODEOWNERS`, `.github/dependabot.yml`,
`.github/pull_request_template.md` and `AGENTS.md`) were seeded once and are owned here, but must
keep the lines, markers and structure `template check` lists.

**Deferral.** While an adoption is staged across pull requests, `site/mod-base.json`
`template.deferred` may name `.gitattributes`, `.gitignore`, `.github/dependabot.yml`,
`.github/pull_request_template.md` and `AGENTS.md`, never the caller, the bootstrap, the shared
documents or `CODEOWNERS`. A deferred file may be absent. A present deferred file is checked under
its class with one allowance: the required lines a deferred fragment still lacks are printed as
`pending` without failing. A deferred managed file must still be byte-identical, and every other
fragment rule (markers, the `AGENTS.md` grammar, the Dependabot ignore) still fails. The adoption
is complete only when `deferred` is empty again.

**Drift.** When `template check` reports a managed file, restore it with
`python3 scripts/ci/mod_base_kit.py run template sync --write --repo .` at the current pin. If the
managed content itself must change, change it in mod-base first and bump; never patch it locally.

**Bump.** On a fresh branch run `python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z`. Before it
edits anything it resolves the tag, requires it to peel to a commit reachable from mod-base `main`
and fetches that commit; it then rewrites every pin and `# v` comment and re-synchronizes the
managed files from the new kit. Review the complete diff, commit it as one change
(`ci: bump mod-base to vX.Y.Z`) and open it through the route the mod's local documents name for
control-plane changes. The complete gates run on it: a bump changes the privileged publication path
and gets no selection-policy exception. Dependabot ignores `The-Plum-Team/mod-base*`; close any
Dependabot pull request that touches a kit reference. The kit's release notes list every schema,
managed-file and adapter-protocol change of the tag.

**Kit availability.** Mod code and tests find the kit only through `scripts/ci/mod_base_kit.py`
(`kit_path()`), in this order: a stamped `out/mod-base-kit/` overlay staged by a trusted
controller; `MOD_BASE_KIT_PATH` exported together with a matching `MOD_BASE_KIT_SHA`; the user
cache outside the repository; an anonymous shallow fetch of the pin into that cache. Every
candidate is verified, and an unavailable kit fails the tests, never skips them. A verified kit
must never gain bytecode: `kit_path()` turns bytecode writing off in the process that imports the
kit, and any child process that imports it needs `PYTHONDONTWRITEBYTECODE=1`. To iterate on a
local kit clone, set `MOD_BASE_KIT_PATH=../mod-base MOD_BASE_ALLOW_UNPINNED=1`; that override is
refused whenever `CI` or `GITHUB_ACTIONS` is set. Delete `<cache>/mod-base/<sha>/` to discard a
cache entry that fails verification.

## Documentation maintenance

- Generated documentation blocks are regenerated by their generator, never hand-edited.
- Never hand-maintain version, loader or scenario lists in consumers: derive them from the
  authoritative release inventory and scenario contract.
- When a packaged scenario adds, renames or removes a step, edit the scenario contract and its
  executable action together, update independent probe canaries where that is intentional, and let
  every derived consumer (gallery, reviewer, generated documentation) follow the contract.
- Keep the user-facing build and support instructions in `README.md` synchronized with the release
  inventory when behavior, build commands, source layout or compatibility facts change; verify
  every affected target and document intentional exclusions and any outstanding runtime or
  publication evidence.
- Keep the newcomer and AI-assisted contribution path in `CONTRIBUTING.md` and keep the pull-request
  template aligned with it.
