# Working in mod-base

This file is part of the instruction set imported by the kit's `AGENTS.md`, together with the two
shared documents the kit ships to every mod (`template/managed/docs/ai/shared/REPOSITORY.md` and
`template/managed/docs/ai/shared/PUBLIC-EVIDENCE.md`). Those shared rules apply here as written;
this file adds what is specific to developing the kit itself. The kit's `AGENTS.md` stays
import-only (`@docs/ai/KIT.md` and the two shared documents, one per line), and the kit has no
`CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` either. [ARCHITECTURE.md](../ARCHITECTURE.md)
describes the design, [SECURITY-MODEL.md](../SECURITY-MODEL.md) the trust boundaries,
[OPERATIONS.md](../OPERATIONS.md) the owner and release procedures, and [docs/adr](../adr/) the
decisions behind them.

## The protected Build and packaged-E2E pipeline

`src/mod_base/build_ci/`, the kit workflows `build.yml`, `select-build.yml`, `packaged-e2e.yml` and
`gate-status.yml`, the managed callers `template/managed/.github/workflows/mod-base-*.yml` and the
`ci` command are one unreleased pipeline that no mod runs yet. Before touching any of it, read
[BUILD-PROTOCOL.md](../BUILD-PROTOCOL.md) (the reference: identity, records, job graphs, the steps
of a job, root operations), [BUILD-E2E-PROGRESS.md](../BUILD-E2E-PROGRESS.md) (what exists, what
remains, what is still moving), [BUILD-ADAPTER.md](../BUILD-ADAPTER.md) (what a mod provides) and
[ADR 0007](../adr/0007-protected-build-and-packaged-runtime.md) (the decisions and their reasons).
These rules are specific to this code and add to the sections below:

- Compose. Every function is reachable from a `ci` verb (`build_ci/commands_*.py`, listed in
  `commands.VERB_MODULES`) and every verb from a workflow step, one verb to a step, or from a
  documented operator entry. Do not add a primitive that nothing calls.
- A mod never writes a kit-format document. Its hooks write native files at the paths of the
  adapter contract; protected kit code inventories, hashes and binds them.
- Read immutable objects once and budget every command. A commit, tree or blob named by SHA and
  the job list of a completed attempt are read once per command (`reads.CommandReads`); mutable
  state is read at the start and again immediately before the effect (`reads.Watch`). A command
  builds its client with `commands.api_client(..., max_requests=...)` from a limit of
  `model/limits.py`, and a test pins the request count of a typical case.
- Root work goes through the closed operations only: one entry of `grammar.CI_ROOT_OPERATIONS`,
  requested by a private `mod-base.ci.root-request` and run by `tools/ci_privileged_bootstrap.py`,
  which mirrors the list without importing the kit. The only other `sudo` command lines are the
  fixed ones in `build_ci.worker` that create, lock, kill and enter an account, revoke its
  systemd user manager/linger and remove its cron/at jobs, and the two fixed `apt-get` lines of
  `build_ci.system_profile`, which run before the worker boundary exists (so no request can be
  written yet) and run no kit code as root. Root never calls
  the GitHub API, and a request never names a program, a hook or a destination.
- Account, `sudo` and root behaviour is tested in `tests/ci_linux_worker.py`, deferred execution
  in `tests/ci_linux_deferred.py`, the complete PR generation in `tests/ci_linux_pipeline.py`, and
  the system profile installed before the fence in `tests/ci_linux_system_profile.py`.
  The pipeline executes the workflow command lines, consumes the preceding jobs' real artifacts,
  checks request budgets and exercises rejection controls. The suite collects none of these
  modules. CI runs each module in its own GitHub-hosted job in every Python leg and the `Test`
  gate requires them all. Their account classes
  refuse any other host (`GITHUB_ACTIONS`, `RUNNER_ENVIRONMENT=github-hosted`, passwordless `sudo`,
  `/home/runner`), create real accounts and change the modes of system trees for good: run it in
  CI or on a disposable Linux machine laid out like a hosted runner, never on a workstation. A
  suite test may fake the GitHub API (`mod_base.github.fake.FakeGitHub`) and nothing else this
  code owns.
- Every `...CI_...` constant of `model/limits.py` needs a row in `tests/test_ci_limits.py` saying
  what it bounds, and kit code must use it. No bound is raised because a pipeline fails.
- The workflows are policed through the registry tables of `workflow.py` (the `CI_CALLEE_*` and
  `CI_JOB_*` tables: workflows, callers, jobs, verbs, artifacts, permissions), to which
  `tests/test_workflow_ci_policy.py` holds the YAML: change a table and its workflow together.
  Job names are part of the graph contract (`build_ci/graph.py`, `tests/fixtures/ci_graphs/`).

## What a kit change reaches

- The kit runs in consumer mods only at the single commit each mod pins, and only after a
  protected merge here, an immutable `vX.Y.Z` tag and a pin bump in the mod. A change to `main`
  alone changes nothing in any mod, so never "fix" a mod by pushing a tag: a tag is immutable and
  every tag must be a complete, tested release.
- `src/`, `site/` and `requirements/` are the digested kit tree (kit-digest-v1). Any change there
  changes the `MB_KIT_TREE_DIGEST` literal of the callee workflows: run
  `python3 tools/update_tree_digest.py --write` and commit the result in the same change;
  `tests/test_tree_digest_literal.py` fails when the literal is stale.
- `template/`, `tools/` and `actions/` are staged into the Block Pops sandbox overlay but are not
  part of kit-digest-v1; the digested `src/mod_base/template/staged_files.sha256` (`template/` and
  `tools/`, the exact bytes a pre-v0.9.2 bootstrap checks, so never add a directory to it) and
  `staged_actions.sha256` (`actions/`) bind them instead. After any change below `actions/`,
  `template/` or `tools/`, run
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock --write` first, then
  refresh the digest literal (the locks live in `src/`); `tests/test_pin.py` fails when a lock is
  stale.
- `template/managed/` is copied byte-for-byte into every mod and drift-checked there. A change to it
  is a mod-visible change: record it in `CHANGELOG.md` under the next tag, keep the managed
  documents mod-neutral (no mod names, paths or versions) and linked only to each other or to
  absolute `https://` URLs, and keep `template/managed/scripts/ci/mod_base_kit.py` stdlib-only,
  Python 3.11+ and mode 0644.
- `template/seed/` is copied once by `template init`; later edits never reach existing mods.
- The bootstrap and `src/mod_base/pin.py` implement the same pin grammar, kit-digest-v1, staged-file
  lock, stamp and resolution order. Change them together; `tests/test_pin.py` runs both over the
  same inputs.

## Frozen interfaces

These are contracts with mods and with released kit versions. Changing one needs an explicit
version decision, never a silent edit:

- the CLI flags of SPEC §2.2 (`src/mod_base/*/commands*.py`, `src/mod_base/pin_commands.py`);
- the document kinds and fields in [SCHEMAS.md](../SCHEMAS.md): a reader accepts `schema_version`
  N and N-1 and writes N; inside one version only optional fields may be added, and each new field
  is validated whenever present;
- the adapter protocol in [ADAPTER.md](../ADAPTER.md) (`ADAPTER_API`, same N/N-1 rule);
- the job and step display names in `src/mod_base/workflow.py`, which mods match exactly;
- `PIXEL_METRICS_VERSION`: any change to the metric algorithm bumps it and forces regeneration;
- the internal signatures in [INTERNAL-API.md](../INTERNAL-API.md), enforced by
  `tests/test_internal_api.py`.

## Code rules

- Runtime dependencies are the standard library plus hash-locked Pillow (`requirements/pillow.txt`),
  and Pillow is imported only inside `mod_base.imaging`, lazily. Code runs on Python 3.11 through
  3.13: no 3.12-only syntax or APIs without a fallback.
- Imports are absolute (`from mod_base.io.secure_json import ...`); nothing inside `src/` edits
  `sys.path`. Entry points receive a `mod_base.runtime.Invocation` and never read `os.environ`
  themselves.
- Every rejection raises `mod_base.errors.MbError` or a subclass; only `errors.run_main` turns it
  into an exit code (2, 3 for unavailable/superseded evidence, 78 for controller skew) and one
  bounded stderr line.
- Treat every file, artifact, image and API response as hostile: validate strictly before
  allocating or mutating state, bound every read, refuse symlinks, special files and traversal,
  and write documents with `model.canonical.canonical_json`. Bounds come only from
  `model/limits.py`, artifact names only from `model/grammar.py`, job names only from
  `workflow.py`. No `shell=True`, no `eval`, no network access outside `mod_base.github` and the
  bootstrap's documented fetch.
  Bounded byte readers must use binary descriptors on platforms that translate text reads;
  CRLF and 0x1A are payload bytes, never implicit normalization or end-of-file markers.
- Match the style of the ported lineage code: plain typed functions and dataclasses, explicit
  errors, docstrings where the reason is not obvious, no dead code.

## Workflows and composites

The workflows the kit renders with a mod's pin are enrolled in `template.tool.RENDERED_CALLERS`, a
closed registry in code: neither `template/manifest.json` nor a mod's data can enrol a caller, move
one or choose its renderer. The Pages caller is a manifest entry, always managed, and keeps its
region of mod-local `ext-` jobs. The four Build/E2E callers (`mod-base-guard.yml`,
`mod-base-build.yml`, `mod-base-packaged-e2e.yml`, `mod-base-gate-status.yml`) are enrolled in code
alone and rendered whole from `{{PIN}}`, `{{VERSION}}` and `{{BRANCH}}` (the mod's
`canonical_branch`): they have no extension region and `template.deferred` cannot name them. Which
of them a mod has is decided by the mode of its activation manifest alone
(`site/mod-base-build-activation.json`, `build_ci.activation.MANAGED_CALLERS`). `template check`,
`sync` and `init` read it first, check a managed caller like any managed file and report a caller
outside its mode as `forbidden`; a Build configuration without a manifest is an error, and a mod
with neither is not checked for these callers at all. A mode changes only along
`activation.TRANSITIONS`, in a pull request of its own at an unchanged pin (`build_ci.transition`,
`template transition`), and a mode is data, not owner approval. While a mode manages callers,
`.github/dependabot.yml` must ignore the third-party actions they pin. The bootstrap's `bump`
refuses a target kit that cannot read the manifest while a mode other than `disabled` is active,
requires that kit's `template sync` plan to raise no error before it rewrites any pin, and
restores every workflow and action file when its write phase fails.
[OPERATIONS.md](../OPERATIONS.md#builde2e-activation-and-rollback) has the procedure.

- Every `uses:` is pinned to a full commit SHA with its `# vX.Y.Z` comment. The kit never
  references itself (`uses: The-Plum-Team/mod-base...`) outside `canary/`, which is copied into the
  separate canary repository.
- Every callee job starts with the identical prologue (validate inputs, check out the mod at
  `github.sha`, check out the kit at `inputs.kit-sha`, bind the two-part identity and the tree
  digest) before any kit code runs; its permissions must be a subset of the calling job's grant in
  the managed caller. Callees declare no concurrency and no secrets.
- Adapter hooks never run in a job holding `actions: write`, `pages: write` or `id-token: write`.
  Inputs and event data reach `run:` scripts only through `env:`.

## Tests

```bash
PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 -m compileall -q src tests tools
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 tools/parallel_unittest.py -v -t . tests
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock
python3 tools/update_tree_digest.py --check
```

Never let bytecode land in the kit tree: kit-digest-v1 and the staged-file lock refuse any
`__pycache__`, so compile into a separate prefix and run Python with `PYTHONDONTWRITEBYTECODE=1`.

Run the suite on Python 3.11 and 3.13 before handing work off. Tests are stdlib `unittest`, import
the package (`tests/__init__.py` exists), use fakes instead of the network (`mod_base.github.fake`,
local bare Git repositories for fetches) and write only inside temporary directories. A test that
needs a missing kit file fails; it skips only for a missing platform tool such as `bash` or
`sha256sum`.
Prepare byte-sensitive template/caller fixtures with explicit UTF-8 and authored LF writes,
preserving intentionally hostile CRLF/encoding cases. Locale/newline conversion must not prevent
the intended extension/drift/security assertion from executing. Keep POSIX mode/symlink tests
intact when a Windows host cannot run them; report the actual failure instead of weakening it.

## Parallel work

While several agents implement the kit, each owns a disjoint set of files (SPEC §10). Edit only the
files you own; when another owner's file must change, say so in your report instead of editing it.
Run your own test modules while iterating and the whole suite once at the end, reporting failures
outside your files as notes.

## Releases

The tag comes first and the canary then proves it
([OPERATIONS.md](../OPERATIONS.md#releasing-mod-base) has the commands):

1. Merge the release change to `main` through a pull request with the required checks green. It
   sets `__version__` in `src/mod_base/__init__.py` to the version being released, adds its
   `CHANGELOG.md` section and refreshes the staged-file lock and the digest literal.
2. Wait for `mod-base CI` to be green on that exact merge commit.
3. Create the annotated, immutable tag `vX.Y.Z` on that commit and push it.
4. Run the canary pinned to the tag ([OPERATIONS.md](../OPERATIONS.md#canary-procedure)); its
   `verify --network` requires the tag to exist and peel to the pinned commit.
5. Once the canary is green, publish a GitHub Release listing schema changes, managed-file changes,
   adapter-protocol changes and the `pixel_metrics_version`; only then may a mod bump to the tag.

Tags are never moved or deleted. A failing canary is fixed forward with the next patch tag through
the same steps; the failed tag gets no Release and no mod pins it. Every tag names its own commit,
because `__version__` must equal the tag: `v1.0.0` repeats unchanged `v0.9.0` code in a new
release commit instead of tagging the `v0.9.0` commit again.
