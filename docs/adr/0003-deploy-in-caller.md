# 0003. Keep `verify-kit`, `deploy` and `request-rotation` in the mod's caller

Date: 2026-09-25

## Status

Accepted. This deliberately deviates from "the whole Pages workflow is reusable".

## Context

The publication pipeline could live entirely in one reusable workflow. That would put
`pages: write` and `id-token: write` (the OIDC token that authorizes a Pages deployment) inside
kit YAML, run `deploy-pages` from a cross-repository callee whose OIDC `job_workflow_ref` names the
kit, and leave the impostor check to the callee itself. Neither mod has ever deployed that way,
and both mods' tests assert that only their own deploy job holds those scopes and that it checks
nothing out.

## Decision

- The mod's managed caller keeps three jobs:
  - `verify-kit` (`actions: read, contents: read`): the runtime identity binding of
    [ADR 0001](0001-two-part-implementation-identity.md);
  - `deploy` (`contents: read, pages: write, id-token: write`, environment `github-pages`): no
    checkout, one inline step that rechecks every published branch head, then the pinned
    `actions/deploy-pages`;
  - `request-rotation` (`actions: write`): no checkout, one dispatch of `operation=rotate`.
- The kit provides `publish.yml` (admit, collect, family, build), `finalize.yml` (cache refresh)
  and `rotate.yml`. The callee-uploaded `github-pages` artifact belongs to the run, so the caller's
  `deploy` finds it.
- Each caller job grants exactly its callee's permissions; a kit test requires every callee job's
  permissions to be a subset of the calling job's grant.
- The caller's managed region is byte-identical in every mod apart from the pin, and only jobs
  named `ext-*` without mod-base references, `pages`/`id-token` permissions, `actions: write` or a
  dependency on `rotate` may be added below it.

## Consequences

- Kit YAML can never mint an OIDC token or deploy; a faulty kit release can at worst produce a bad
  site artifact, which the mod's own head recheck still gates.
- The mods' existing deploy assertions stay literally checkable against their own files.
- The pipeline is split into two callees around the caller's `deploy` (`publish.yml` and
  `finalize.yml`), and the caller is slightly longer.
- The canary must prove that a callee-uploaded artifact deploys from the caller (item G3).

## Alternatives considered

- One reusable workflow including deploy: puts the OIDC scope in kit code and relies on an
  unverified cross-repository deployment path.
- Composites only, with a generated caller: the caller would still be large and harder to
  drift-check than one managed file.
