# 0002. Wake the publisher by explicit dispatch

Date: 2026-09-25

## Status

Accepted.

## Context

Quick Skin woke its Pages workflow with `repository_dispatch`, which needs `contents: write` in the
producer job, and relayed the wake through a second self-dispatch. Block Pops triggered Pages with
`workflow_run`, which does not fire reliably for runs created with the `GITHUB_TOKEN` (Quick Skin's
master E2E is dispatched that way) and carries an attacker-influenced event payload into the
privileged workflow. Block Pops' default branch also has no ruleset yet, so `contents: write` there
is effectively push access.

## Decision

- The managed caller `pages.yml` has exactly two triggers: `workflow_dispatch` with
  `operation ∈ {manual, deploy, family, rotate}` plus seven identifier inputs, and an hourly
  `schedule` (`43 * * * *`) that runs admission as `recovery`.
- Producers wake it through the `notify-pages` composite, a job holding **only**
  `actions: write`, with no checkout and no Python: it validates its inputs by pattern and
  dispatches `pages.yml` on the default branch through a bounded retry.
- A wake is a hint, never evidence. `admit` authenticates the named run or artifact from scratch
  (path, event, conclusion, head, repository, upload window) and exits green without publishing on
  a stale or foreign wake.
- `pages.yml` never uses `workflow_run`, `repository_dispatch`, `pull_request_target` or a
  `workflows:` trigger list. Block Pops keeps `workflow_run` only as a signal in a separate,
  checkout-free `notify-pages.yml` that authenticates the completed run inline.
- Rotation is a separate `operation=rotate` dispatch from the caller's `request-rotation` job,
  under its own lock.

## Consequences

- Token-created producers wake Pages reliably, and no producer needs `contents: write`.
- A lost or duplicated wake costs at most an hour of delay; the schedule recovers it, and a
  complete, current generation costs only a handful of reads per sweep.
- Block Pops moves from a monthly to an hourly schedule, identical in every mod because the
  caller is managed.
- Block Pops' exact E2E job graph is unchanged: no job is added to its gated workflows.

## Alternatives considered

- `repository_dispatch`: needs `contents: write`.
- `workflow_run` on `pages.yml`: unreliable for token-created runs and brings event data into the
  privileged workflow.
- A notify job added to Block Pops' gated E2E workflow: changes its authenticated job graph.
