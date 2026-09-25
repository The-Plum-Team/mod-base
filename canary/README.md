# mod-base canary

**Synthetic demonstration evidence — not a product.**

This repository is the canary of [mod-base](https://github.com/The-Plum-Team/mod-base), the shared
public-evidence and project-site kit of The Plum Team. It is a stand-in mod: its "packaged E2E"
generates deterministic images instead of launching Minecraft, and it publishes them through the
pinned kit exactly as a real mod does (a producer hands off evidence, the protected
`Project site` workflow authenticates, renders and deploys it, then rotates the superseded
generation). A mod pins a kit tag only after this canary, pinned to that tag, has proven, from a
separate repository, every behaviour that cannot be verified locally.

| Workflow | Purpose |
|---|---|
| `.github/workflows/pages.yml` | the managed `Project site` caller (identical in every mod) |
| `.github/workflows/canary-producer.yml` | generates every key's evidence and wakes the site (`operation=deploy`) |
| `.github/workflows/canary-family.yml` | generates one synthetic `demo-pairs` family generation (`operation=family`); a later head carries it forward while the release matrix and scenario contract are unchanged |
| `.github/workflows/canary-probe.yml` | records platform observations for the canary evidence table (changes nothing) |

The procedure, the items to observe (G1–G7) and where the observations are recorded live in the
mod-base repository: `docs/OPERATIONS.md`, sections "Canary procedure" and "Canary evidence".
Nothing on the published site describes a real mod, release or download.
