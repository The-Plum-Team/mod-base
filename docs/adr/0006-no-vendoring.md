# 0006. Run the kit at a pinned commit instead of vendoring it

Date: 2026-09-25

## Status

Accepted.

## Context

The kit is roughly twenty thousand lines of Python, front end, workflows and tests. It must run in
three kinds of places: the privileged Pages jobs of each mod, the mods' producer and policy jobs,
and each mod's local unit tests, including Block Pops' sandboxed candidate runs, which see only
files the trusted controller stages into the sandbox (an untracked top-level path outside the
sandbox's generated directories fails its seal, so a kit overlay must live under `out/`).

## Decision

- The kit is never copied into a mod. Reusable workflows and composites run it at the pinned SHA
  (`uses: The-Plum-Team/mod-base/...@<sha>`); CI never pip-installs it, because a VCS URL cannot be
  hash-pinned. `pyproject.toml` exists only for local editable installs.
- Mod code finds the kit only through the managed, stdlib-only bootstrap
  `scripts/ci/mod_base_kit.py`, which resolves and verifies, first match wins:
  1. `out/mod-base-kit/` with a `mod-base.kit-stamp` whose SHA equals the pin and whose
     kit-digest-v1 matches the tree, and whose `template/`, `tools/` and (when staged) `actions/`
     match the staged-file locks inside the digested `src/` (staged by Block Pops' controller);
  2. `MOD_BASE_KIT_PATH` with `MOD_BASE_KIT_SHA` equal to the pin (exported by the `setup`
     composite after verifying its tree against the Git trees API);
  3. a clean git checkout of the pin in the user cache, outside the repository;
  4. an anonymous shallow fetch of the pin into that cache, verified by content address.
- An unavailable kit fails the tests; it never skips them. The unpinned developer override
  (`MOD_BASE_KIT_PATH` without a SHA) needs `MOD_BASE_ALLOW_UNPINNED=1` and is refused whenever
  `CI` or `GITHUB_ACTIONS` is set.
- In Block Pops the controller, never the candidate, stages the kit: the controller's own verified
  kit when the pins are equal, or the candidate's pin after the released checks of
  `verify --network` when a controller upgrade changes it.

## Consequences

- There is one physical copy of the kit, at one commit per mod, and no `.gitignore` or vendored-tree
  maintenance in the mods; only the small managed files are copied, and they are drift-checked.
- Local development and CI jobs without the `setup` composite need anonymous HTTPS access to
  GitHub the first time a pin is used; afterwards the verified cache is reused.
- Block Pops' adoption pull request, whose master stages nothing yet, tests itself through the
  fetch fallback inside its sandbox (which has Git, HTTPS and `CI=true`).
- A kit bump always touches the mods' workflows, so in Quick Skin it pays for the full Build and
  packaged E2E, and in Block Pops it is a controller upgrade; there is deliberately no
  selection-policy exception.

## Alternatives considered

- Vendoring the kit under the mods behind a digest: reintroduces a copy in every mod.
- A hash-pinned release wheel: duplicates the pin in a second artifact.
- A Git submodule: breaks `AGENTS.md` imports and ephemeral worktrees.
- Passing `MOD_BASE_KIT_PATH` into Block Pops' sandbox: the sandbox forwards only allowlisted
  environment names, so the overlay under `out/` is required.
