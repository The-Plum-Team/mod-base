# Project and release contract

This file is part of the repository-wide instruction set imported by `AGENTS.md`. Together, the
documents listed there are the source of truth for coding agents working anywhere below the
repository root. The two shared documents imported first (`docs/ai/shared/REPOSITORY.md` and
`docs/ai/shared/PUBLIC-EVIDENCE.md`) are managed by mod-base and hold the rules every mod shares;
this file holds only what is specific to the canary.

## What the canary is

Synthetic demonstration evidence — not a product. The mod-base canary is a stand-in mod that
proves the kit's cross-repository behaviour before each kit release: its "packaged E2E" is a
generator (`scripts/pages/mod_base_fixtures.py`, run by `scripts/canary/produce.py`), its release
inventory is `release/release-matrix.json` and its scenario contract is
`e2e/scenario-contract.json`. Nothing here describes a real mod.

## Documentation map

- `README.md` explains the canary and links to the procedure in the mod-base repository
  (`docs/OPERATIONS.md`, "Canary procedure").
- `site/mod-base.json` and `scripts/pages/mod_base_adapter.py` connect the canary to mod-base;
  `scripts/ci/mod_base_kit.py` (managed) finds the pinned kit.
- `.github/workflows/pages.yml` is the managed caller; `canary-producer.yml` and
  `canary-family.yml` are the synthetic producers; `canary-probe.yml` records platform
  observations only.

## Task routing

Every change starts in the mod-base repository: the canary's files are copied from its `canary/`
directory, and a canary cycle moves to a newer kit only with
`python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z`.

## Verification

```bash
git diff --check
python3 scripts/ci/mod_base_kit.py verify --network
python3 scripts/ci/mod_base_kit.py run template check --repo .
python3 scripts/ci/mod_base_kit.py run conformance --repo . --all --families
```
