# 0005. Manage shared repository files with a checked template

Date: 2026-09-25

## Status

Accepted.

## Context

Both mods carry near-identical repository conventions: agent instructions imported by `AGENTS.md`,
a `.gitattributes` rule for Gradle's batch wrapper, ignore rules for evidence output, CODEOWNERS
for control-plane paths, Dependabot configuration and a pull-request template. After adopting the
kit they also need the same Pages caller and the same kit bootstrap. Copies drift. A GitHub
template repository copies once and then drifts too, and a submodule breaks `AGENTS.md` imports and
worktrees. Block Pops can change only admissible paths in a controller-upgrade pull request, so its
adoption has to be split across pull requests.

## Decision

`template/manifest.json` (`mod-base.template-manifest` v1) classifies every shared file:

- **managed** files are byte-identical to `template/managed/<path>` at the pinned kit:
  `.gitattributes` (Quick Skin's exact bytes), `.github/workflows/pages.yml` (managed region with
  `{{PIN}}`/`{{VERSION}}` plus an `ext-*` extension region), `scripts/ci/mod_base_kit.py` and the
  shared agent documents `docs/ai/shared/REPOSITORY.md` and `docs/ai/shared/PUBLIC-EVIDENCE.md`;
- **fragment** files are seeded once and must keep listed lines, markers or structure:
  `.gitignore`, `.github/CODEOWNERS`, `.github/dependabot.yml` (every `github-actions` update ignores
  `The-Plum-Team/mod-base*` and, from v1.0.1, every third-party action the caller's managed region
  pins, `actions/deploy-pages`: both move only with a kit bump), `.github/pull_request_template.md`
  and `AGENTS.md` (the two shared imports first, then `template.agents_local`);
- **seeded** files are copied once by `template init --seed` and never checked: `CONTRIBUTING.md`,
  `LICENSE` (All Rights Reserved or the LGPL-2.1 notice, chosen by `license_label`),
  `site/mod-base.json`, the adapter stub, `docs/ai/PROJECT.md` and the decisions index;
- a mod's `template.deferred` stages an adoption across pull requests. It may name only the root
  files outside the publication control path (`.gitattributes`, `.gitignore`,
  `.github/dependabot.yml`, the pull-request template and `AGENTS.md`), never the caller, the
  bootstrap, the shared documents or `CODEOWNERS`. A deferred file may be absent; a present deferred
  managed file must be byte-identical, and a present deferred fragment is checked strictly except
  that its missing required lines are reported as `pending` without failing, because Block Pops'
  PR A cannot touch its existing `.gitignore`.

`template check` (run by each mod's tests and policy job) reports every drift as a unified diff,
enforces the caller's region rules (over a small YAML subset, so that a line-based check sees what
GitHub parses), the `AGENTS.md` grammar, the managed-documents link rule (links
only to other managed documents or absolute `https://` URLs) and the absence of `CLAUDE.md`,
`.claude/CLAUDE.md` and `CLAUDE.local.md`. `template sync --write` restores managed files while
preserving the pin and the extension region; the bootstrap's `bump` runs it from the newly pinned
kit. Legal text is never managed, and the verification sections of each mod's documentation stay
mod-local.

## Consequences

- One edit in mod-base plus one bump updates every mod, and local edits to managed files cannot
  merge unnoticed.
- The pull-request template stays a fragment, so Quick Skin keeps its richer, CONTRIBUTING-aligned
  template.
- Block Pops adopts in stages: PR A (controller upgrade) defers `.gitattributes`, `.gitignore`,
  `.github/dependabot.yml`, the pull-request template and `AGENTS.md`, PR B adds or completes them,
  PR C clears `deferred`. Deferral can never hide drift of a privileged file.
- Managed documents must stay mod-neutral; mod-specific rules live in each mod's local imports.

## Alternatives considered

- A GitHub template repository: copies once, then drifts silently.
- A managed, byte-identical pull-request template: would erase Quick Skin's project-specific
  checklist.
- Vendoring whole directories: see [ADR 0006](0006-no-vendoring.md).
