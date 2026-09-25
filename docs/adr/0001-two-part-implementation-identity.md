# 0001. Bind every publication to a two-part implementation identity

Date: 2026-09-25

## Status

Accepted.

## Context

Before mod-base, each mod's evidence validator and renderer lived in the mod itself, so "the
protected default-branch commit" fully described the code that may validate or render public
evidence. With a shared kit, privileged Pages jobs run code from a second repository. GitHub
resolves `owner/repo@<sha>` across the whole fork network, so a SHA that exists only in a fork of
mod-base resolves under mod-base's name, and a reusable workflow cannot defend itself against an
impostor copy of itself: the impostor would simply omit the check.

`referenced_workflows` on a workflow run is GitHub's server-side record of the reusable workflows a
run resolved, with their commits. `job.workflow_sha` also exists but is new and undocumented for
this use, and an unknown context property risks a workflow parse failure.

## Decision

- Only code fixed by the protected mod commit may validate or render: the adapter and
  `site/mod-base.json` at `github.sha`, plus the mod-base commit that `github.sha` pins **and** that
  is reachable from mod-base `main`.
- Every mod-base reference in a mod is `@<40-hex> # vX.Y.Z`, and all of them carry one SHA.
- The caller-owned `verify-kit` job (inline shell in the mod's managed `pages.yml`, no kit code)
  takes the kit SHA from `referenced_workflows`, requires it to equal every pin in `pages.yml` at
  `github.sha`, and requires `compare/<sha>...main` to be `ahead` or `identical` with
  `behind_by == 0`. Every callee depends on it.
- Every callee job checks out the mod at `github.sha` and the kit at the verified SHA, requires
  both clean, and compares kit-digest-v1 of `src/`, `site/` and `requirements/` with a literal
  compiled into the callee YAML.
- Every manifest records `implementation` and `kit`; consumers bind a recorded `kit.sha` to the
  artifact's authenticated owner (the owning Pages run's `referenced_workflows`, or the pin read
  from the producer workflow at the producer commit), never to the current pin.
- Pre-merge `verify --network` adds the same reachability check plus "the tag peels to the pin"
  before a pin can merge (as pull-request code in Quick Skin, controller-side in Block Pops).
- `job.workflow_sha` is only recorded by the canary; `uses: $/...` is never used.

## Consequences

- An impostor kit commit cannot run in a privileged job: the defense lives in code the mod owns.
- A kit bump does not invalidate existing evidence, because consumers check owner consistency,
  not equality with the current pin; a `pixel_metrics_version` change still forces regeneration.
- `_site/build.json` publishes both identities, and the gallery shows the kit version and commit.
- Changing privileged code always needs two protected merges: mod-base `main` and the mod's pin.
- The cross-repository shape of `referenced_workflows` must be proven by the canary before
  `v1.0.0`; until then an unexpected shape fails closed and keeps the previous site.

## Alternatives considered

- A check inside the reusable workflow: useless against an impostor, which writes its own check.
- Pre-merge checks only: they do not protect a run whose referenced commit differs from review.
- `job.workflow_sha` as the primary source: too new; kept as a future redundant cross-check.
