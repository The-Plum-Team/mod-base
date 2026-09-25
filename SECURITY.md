# Security policy

mod-base runs with privileges in The Plum Team's mods: its reusable workflows build and publish
each mod's GitHub Pages site, and its composites run inside the mods' end-to-end jobs. A flaw here
can reach every mod that pins an affected release.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting:
<https://github.com/The-Plum-Team/mod-base/security/advisories/new>. Do not open a public issue or
pull request for a suspected vulnerability. Include the affected release tag or commit, the
component (workflow, composite, Python module or front end) and a reproduction. The maintainer
acknowledges reports within a week; a fix is released as a new immutable tag and every mod is
bumped through its own protected pull request.

## Supported releases

Only the newest `vX.Y.Z` tag receives fixes. Tags are immutable: a faulty release is fixed forward
by a new tag, or a mod pins back to an earlier one through an ordinary pull request.

## Trust model in brief

The complete model is [docs/SECURITY-MODEL.md](docs/SECURITY-MODEL.md).

- **Two protected merges.** Privileged code reaches a mod only through a protected merge to
  mod-base `main` (required checks, no force pushes, immutable `v*` tags) followed by a pin bump in
  the mod. A push here alone changes nothing in any mod.
- **One pinned commit per mod.** Every mod-base reference in a mod is `@<40-hex> # vX.Y.Z` with a
  single commit. The caller-owned `verify-kit` job proves at run time that the executing reusable
  workflows are that commit (`referenced_workflows`) and that it is reachable from `main`, which
  defeats impostor commits from the fork network; every reusable job re-verifies the checked-out
  kit tree against a digest literal compiled into its YAML.
- **Least privilege.** Only the mod-owned `deploy` job holds `pages: write` and `id-token: write`;
  the kit's workflows never do. Wakes need only `actions: write`, rotation runs apart with
  `actions: write` and no adapter code, and no workflow receives secrets.
- **Hostile inputs.** Every artifact, JSON document, image and API response is validated strictly
  and fail-closed before use: bounded reads, no symlinks or traversal, canonical JSON, exact
  inventories, re-derived expectations and re-encoded images. A mod's adapter runs only in
  read-only jobs, in an `env -i` child process, and its every result is re-verified.
- **Atomic publication.** Any failure before deployment keeps the previously deployed site; the
  published `build.json` names the exact mod and kit commits that produced it.
