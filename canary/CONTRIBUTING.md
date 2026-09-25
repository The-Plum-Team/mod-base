# Contributing to the mod-base canary

Synthetic demonstration evidence — not a product. The canary accepts no feature work of its own:
it is a copy of the `canary/` directory of The-Plum-Team/mod-base, refreshed by the canary
procedure in that repository's `docs/OPERATIONS.md`. Propose every change there, as a pull request
to mod-base, and run its complete checks:

```bash
python3 scripts/ci/mod_base_kit.py verify --network
python3 scripts/ci/mod_base_kit.py run template check --repo .
python3 scripts/ci/mod_base_kit.py run conformance --repo . --all --families
git diff --check
```

Agents follow `AGENTS.md` and the documents it imports.
