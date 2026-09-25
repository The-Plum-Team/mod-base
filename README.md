# mod-base

mod-base is the shared public-evidence and project-site kit of The Plum Team's Minecraft mods. It
turns a mod's successful packaged end-to-end run into a validated, SHA-bound static site on GitHub
Pages (a landing page and an evidence gallery), and it ships the repository files every mod shares.
Quick Skin and Block Pops use it; [docs/ONBOARDING.md](docs/ONBOARDING.md) connects another mod.

Mods never vendor the kit: every mod runs it **fetched at one pinned commit**, and every mod-base
reference in a mod carries that same commit and its release tag:

```yaml
uses: The-Plum-Team/mod-base/.github/workflows/publish.yml@<40-hex commit> # vX.Y.Z
```

## What a mod gets

| Part | Where it lives | What it does |
|---|---|---|
| Reusable workflows | `.github/workflows/{publish,finalize,rotate}.yml` | admit, collect, build, refresh and rotate the site, called by the mod's managed `pages.yml` |
| Composite actions | `actions/{setup,prepare-evidence,publish-family,notify-pages}` | verify the pinned kit, hand off evidence from the mod's E2E job, publish a family generation, wake the site |
| Python package | `src/mod_base` (stdlib + hash-locked Pillow) | every validator, renderer and API client, run as `python3 -P -m mod_base` |
| Front end | `site/` | the landing page and gallery, templated from the mod's `site/mod-base.json` |
| Repository template | `template/` | managed files (byte-identical in every mod), fragments and seeds (`template check/sync/init`) |
| Canary | `canary/` | the synthetic demo mod copied into `The-Plum-Team/mod-base-canary` to prove every release |

A mod connects through two protected files of its own: `site/mod-base.json` (data) and
`scripts/pages/mod_base_adapter.py` (the hooks that read its contract and packaged output). See
[docs/ADAPTER.md](docs/ADAPTER.md).

## The two-part identity

Every published page is bound to two commits: the mod's protected default-branch head that ran the
`Project site` workflow, and the mod-base commit that head pins. The caller-owned `verify-kit` job
derives the kit commit from GitHub's `referenced_workflows`, requires it to equal every pin in the
executed `pages.yml` and to be reachable from mod-base `main`, and every reusable job re-verifies
the checked-out kit tree against a digest literal compiled into its YAML. The published
`_site/build.json` records both identities. Changing privileged code in a mod therefore always
takes two protected merges: one here, then a pin bump in the mod.

## Pins and bumps

- Pin: `uses: The-Plum-Team/mod-base/<path>@<40-hex> # vX.Y.Z`, one commit and version per mod,
  checked by the mod's `python3 scripts/ci/mod_base_kit.py verify --network`.
- Bump: `python3 scripts/ci/mod_base_kit.py bump --to vX.Y.Z` on a branch; it verifies the tag,
  rewrites every pin and re-synchronizes the managed files. Dependabot ignores mod-base in mods.
- Release: merge to `main`, green `mod-base CI`, an immutable annotated tag on that commit, then
  the canary pinned to the tag; the GitHub Release follows a green canary, and a failing canary
  is fixed forward with the next patch tag
  ([docs/OPERATIONS.md](docs/OPERATIONS.md#releasing-mod-base)).

## Local checks

```bash
python3 -m pip install --only-binary=:all: --require-hashes -r requirements/pillow.txt
PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 -m compileall -q src tests tools canary
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 tools/parallel_unittest.py -v -t . tests
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock
python3 tools/update_tree_digest.py --check
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -P -m mod_base conformance --repo canary --all --kit-root . --families
```

`conformance` runs complete synthetic generations of any mod against an in-memory GitHub, without
touching the mod's checkout: producer, admission, collect, build, refresh and rotation (dry runs
and real ones, each plan checked against an independent oracle), then documentation-only pushes
with carried family legs and a same-head publication interleaved with the previous rotation. Mods
run it through their bootstrap (`python3 scripts/ci/mod_base_kit.py run conformance --repo . --keys
<key>`).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): the design and the publication flow.
- [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md) and [SECURITY.md](SECURITY.md): trust roots,
  threats and reporting.
- [docs/SCHEMAS.md](docs/SCHEMAS.md), [docs/ADAPTER.md](docs/ADAPTER.md) and
  [docs/INTERNAL-API.md](docs/INTERNAL-API.md): the frozen contracts.
- [docs/OPERATIONS.md](docs/OPERATIONS.md): owner activation, releases, the canary, rollout,
  admission reasons, the kill switch and recovery commands.
- [docs/ONBOARDING.md](docs/ONBOARDING.md): connecting a new mod.
- [docs/adr](docs/adr): the decisions behind the design.
- [CHANGELOG.md](CHANGELOG.md): what each release changes for mods.

## License

All Rights Reserved; the source is public for transparency and for use by The Plum Team's
projects. See [LICENSE](LICENSE).
