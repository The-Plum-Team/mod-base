# Working in mod-base

This file is part of the instruction set imported by the kit's `AGENTS.md`, together with the two
shared documents the kit ships to every mod (`template/managed/docs/ai/shared/REPOSITORY.md` and
`template/managed/docs/ai/shared/PUBLIC-EVIDENCE.md`). Those shared rules apply here as written;
this file adds what is specific to developing the kit itself. The kit's `AGENTS.md` stays
import-only (`@docs/ai/KIT.md` and the two shared documents, one per line), and the kit has no
`CLAUDE.md`, `.claude/CLAUDE.md` or `CLAUDE.local.md` either. [ARCHITECTURE.md](../ARCHITECTURE.md)
describes the design, [SECURITY-MODEL.md](../SECURITY-MODEL.md) the trust boundaries,
[OPERATIONS.md](../OPERATIONS.md) the owner and release procedures, and [docs/adr](../adr/) the
decisions behind them.

## What a kit change reaches

- The kit runs in consumer mods only at the single commit each mod pins, and only after a
  protected merge here, an immutable `vX.Y.Z` tag and a pin bump in the mod. A change to `main`
  alone changes nothing in any mod, so never "fix" a mod by pushing a tag: a tag is immutable and
  every tag must be a complete, tested release.
- `src/`, `site/` and `requirements/` are the digested kit tree (kit-digest-v1). Any change there
  changes the `MB_KIT_TREE_DIGEST` literal of the callee workflows: run
  `python3 tools/update_tree_digest.py --write` and commit the result in the same change;
  `tests/test_tree_digest_literal.py` fails when the literal is stale.
- `template/` and `tools/` are staged into the Block Pops sandbox overlay but are not part of
  kit-digest-v1; the digested `src/mod_base/template/staged_files.sha256` binds them instead. After
  any change below `template/` or `tools/`, run
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock --write` first, then
  refresh the digest literal (the lock lives in `src/`); `tests/test_pin.py` fails when the lock is
  stale.
- `template/managed/` is copied byte-for-byte into every mod and drift-checked there. A change to it
  is a mod-visible change: record it in `CHANGELOG.md` under the next tag, keep the managed
  documents mod-neutral (no mod names, paths or versions) and linked only to each other or to
  absolute `https://` URLs, and keep `template/managed/scripts/ci/mod_base_kit.py` stdlib-only,
  Python 3.11+ and mode 0644.
- `template/seed/` is copied once by `template init`; later edits never reach existing mods.
- The bootstrap and `src/mod_base/pin.py` implement the same pin grammar, kit-digest-v1, staged-file
  lock, stamp and resolution order. Change them together; `tests/test_pin.py` runs both over the
  same inputs.

## Frozen interfaces

These are contracts with mods and with released kit versions. Changing one needs an explicit
version decision, never a silent edit:

- the CLI flags of SPEC §2.2 (`src/mod_base/*/commands*.py`, `src/mod_base/pin_commands.py`);
- the document kinds and fields in [SCHEMAS.md](../SCHEMAS.md): a reader accepts `schema_version`
  N and N-1 and writes N; inside one version only optional fields may be added, and each new field
  is validated whenever present;
- the adapter protocol in [ADAPTER.md](../ADAPTER.md) (`ADAPTER_API`, same N/N-1 rule);
- the job and step display names in `src/mod_base/workflow.py`, which mods match exactly;
- `PIXEL_METRICS_VERSION`: any change to the metric algorithm bumps it and forces regeneration;
- the internal signatures in [INTERNAL-API.md](../INTERNAL-API.md), enforced by
  `tests/test_internal_api.py`.

## Code rules

- Runtime dependencies are the standard library plus hash-locked Pillow (`requirements/pillow.txt`),
  and Pillow is imported only inside `mod_base.imaging`, lazily. Code runs on Python 3.11 through
  3.13: no 3.12-only syntax or APIs without a fallback.
- Imports are absolute (`from mod_base.io.secure_json import ...`); nothing inside `src/` edits
  `sys.path`. Entry points receive a `mod_base.runtime.Invocation` and never read `os.environ`
  themselves.
- Every rejection raises `mod_base.errors.MbError` or a subclass; only `errors.run_main` turns it
  into an exit code (2, 3 for unavailable/superseded evidence, 78 for controller skew) and one
  bounded stderr line.
- Treat every file, artifact, image and API response as hostile: validate strictly before
  allocating or mutating state, bound every read, refuse symlinks, special files and traversal,
  and write documents with `model.canonical.canonical_json`. Bounds come only from
  `model/limits.py`, artifact names only from `model/grammar.py`, job names only from
  `workflow.py`. No `shell=True`, no `eval`, no network access outside `mod_base.github` and the
  bootstrap's documented fetch.
- Match the style of the ported lineage code: plain typed functions and dataclasses, explicit
  errors, docstrings where the reason is not obvious, no dead code.

## Workflows and composites

- Every `uses:` is pinned to a full commit SHA with its `# vX.Y.Z` comment. The kit never
  references itself (`uses: The-Plum-Team/mod-base...`) outside `canary/`, which is copied into the
  separate canary repository.
- Every callee job starts with the identical prologue (validate inputs, check out the mod at
  `github.sha`, check out the kit at `inputs.kit-sha`, bind the two-part identity and the tree
  digest) before any kit code runs; its permissions must be a subset of the calling job's grant in
  the managed caller. Callees declare no concurrency and no secrets.
- Adapter hooks never run in a job holding `actions: write`, `pages: write` or `id-token: write`.
  Inputs and event data reach `run:` scripts only through `env:`.

## Tests

```bash
PYTHONPYCACHEPREFIX="$(mktemp -d)" python3 -m compileall -q src tests tools
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 tools/parallel_unittest.py -v -t . tests
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock
python3 tools/update_tree_digest.py --check
```

Never let bytecode land in the kit tree: kit-digest-v1 and the staged-file lock refuse any
`__pycache__`, so compile into a separate prefix and run Python with `PYTHONDONTWRITEBYTECODE=1`.

Run the suite on Python 3.11 and 3.13 before handing work off. Tests are stdlib `unittest`, import
the package (`tests/__init__.py` exists), use fakes instead of the network (`mod_base.github.fake`,
local bare Git repositories for fetches) and write only inside temporary directories. A test that
needs a missing kit file fails; it skips only for a missing platform tool such as `bash` or
`sha256sum`.

## Parallel work

While several agents implement the kit, each owns a disjoint set of files (SPEC §10). Edit only the
files you own; when another owner's file must change, say so in your report instead of editing it.
Run your own test modules while iterating and the whole suite once at the end, reporting failures
outside your files as notes.

## Releases

The tag comes first and the canary then proves it
([OPERATIONS.md](../OPERATIONS.md#releasing-mod-base) has the commands):

1. Merge the release change to `main` through a pull request with the required checks green. It
   sets `__version__` in `src/mod_base/__init__.py` to the version being released, adds its
   `CHANGELOG.md` section and refreshes the staged-file lock and the digest literal.
2. Wait for `mod-base CI` to be green on that exact merge commit.
3. Create the annotated, immutable tag `vX.Y.Z` on that commit and push it.
4. Run the canary pinned to the tag ([OPERATIONS.md](../OPERATIONS.md#canary-procedure)); its
   `verify --network` requires the tag to exist and peel to the pinned commit.
5. Once the canary is green, publish a GitHub Release listing schema changes, managed-file changes,
   adapter-protocol changes and the `pixel_metrics_version`; only then may a mod bump to the tag.

Tags are never moved or deleted. A failing canary is fixed forward with the next patch tag through
the same steps; the failed tag gets no Release and no mod pins it. Every tag names its own commit,
because `__version__` must equal the tag: `v1.0.0` repeats unchanged `v0.9.0` code in a new
release commit instead of tagging the `v0.9.0` commit again.
