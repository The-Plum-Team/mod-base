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

The protected Build/packaged-runtime implementation is tracked by
[BUILD-E2E-PROGRESS.md](../BUILD-E2E-PROGRESS.md), against the complete
[BUILD-E2E-DESIGN.md](../BUILD-E2E-DESIGN.md). [BUILD-PROTOCOL.md](../BUILD-PROTOCOL.md) owns the
inactive foundational protocol; `src/mod_base/build_ci/` owns its common mechanisms (MB11).
Pages building stays in `pages.build`. Structural plan/graph validation alone never authorizes
candidate execution, consumer activation, an App success or a merge. The Linux boundary and real
hosted canary remain mandatory before release/adoption.

`build_ci.batch_schema` owns the initial mod-base.ci.batch data format, separately from
batch API/source-byte observations. Its structural parent/result and fingerprint checks never
admit actual Git construction, native protected policy, writer leases or settlement. Keep
the archived predecessor rejection and original 50-member/whole-document bounds in coverage.
`batch.verify_batch_manifest_sources` binds that data to repeated genuine live API and complete
source-byte observations and actual squash parent/tree/result inventory reads. It requires
independently admitted native profile/policy and original private writer-excluded source roots.
API-observed result equality is not safe local patch application or fixed bot construction;
original Git/runtime enrollment, branch leases and merged-gate settlement remain required.
Batch branch absence observations must use the exact ref endpoint and scoped 404 after successful
controller/member reads; a general API failure is never an empty branch. Repeated observation is
not an atomic name reservation. The final protected push must use the explicit empty-expect
lease and independently admitted safe Git/runtime, native policy, source/result and writer proofs.
Exit 0 with an identical-SHA up-to-date Git result is not new-branch creation. Require bounded
exact new-branch porcelain output from the completed protected command, then recheck created
ref/live/native writer admission; supplied receipt bytes and exit codes are not provenance.
`batch.authenticate_batch_publication` binds the exact current commit ref/tree to the original
expected branch/commit and complete manifest sources, with final full member admission. Ref
existence is not proof of who created it or approval to create a PR, publish status or settle.
Actual original safe command/new-branch receipt and native/live writer provenance stay required.
`batch.authenticate_batch_pr` binds a distinct ready batch PR and its exact synthetic merge to
that manifest/publication and current sources. Identity kit/workflow/inventory/scenario/graph
shape is not original native/pin/plan approval or successful full gates. Merge/closure/status
authority and immutable post-merge settlement require their independent complete admission.
`authenticate.authenticate_merged_pr_identity` separately binds original synthetic parents/tree
to the actual merged PR/final tree and original/current protected history. Final merge parents
may differ for merge/squash/rebase; never replace the original tested identity with the final
SHA or relax the existing live open/ready path. Its observation grants no full historical gate,
native policy/pin, reuse or settlement approval. Those readers and their complete admission
remain required before effects. Inactive reuse-v1 data retains separate original PR and current
non-PR bindings even when the actual final merged SHA equals the original synthetic tested SHA.
Do not require unequal SHAs or erase original provenance to make that case fit. Both direct seals,
ordered original parents, tree/policy/pin equality and full K6 admission remain mandatory.
selection.select_latest_merged_pr_build separately admits historical source around original
controller newest-attempt/full-graph/bundle metadata selection. Never substitute the current
default SHA, select only successful runs or fall back after failed/pending/corrupt newer evidence.
Retain original plan bytes in both source routes and original descriptor bytes during historical
revalidation. Public inputs supply no admission callback. Both gates/native payloads/policy/pin/
chronology/consumer/writer admission remain separate; this read grants no reuse or execution.
`transport.download_merged_gate_receipt` separately reads an original full tested PR seal after
merge, retaining original producer head/kit/attempt and repeated historical source observations.
It shares the full live transport's graph, upload, canonical ZIP/JSON and source availability
checks, including independently enrolled owning Build. No public input supplies an admission
callback. Both coherent gates, native payloads, original/current policy/pin, newest-run selection
and actual later consumer chronology remain required; this activates no reuse or settlement.
`transport.download_merged_gate_pair` reads both original full seals with retained historical
source/caller snapshots and requires packaged's exact whole owning Build descriptor to equal
the Build seal's actual complete bundle. Reject independently valid but mixed generations;
repeat both full readers before returning the pair. Native payloads, newest eligible runs,
policy/pin equivalence, later consumer chronology and authority remain mandatory. Observation
never reserves artifacts or activates partial reuse, fallback, statuses or settlement.
`transport.download_merged_build` materializes only that coherent pair's exact original complete
bundle through shared bounded ZIP/envelope/inventory/byte checks and private atomic copying.
Retained source, original caller and the whole pair are rechecked inside final publication;
staged bytes are verified again afterward. Private writer-excluded parent provenance, complete
native Build/runtime validity, newest eligible sources, policy/pin and consumer/writer admission
remain required. Copying authenticated generic bytes is not native or reuse success authority.

`transport.download_merged_runtime` copies only the original coherent pair's complete results.
Retain original producer/attempt/kit/ZIP/owning Build identity and recheck pair/source/caller
inside final runtime publication. Original extracted inventory must still match after final API
admission; a self-consistent replacement is not original payload proof. Actual owning Build
bytes, native domain validation, newest source/policy/pin/consumer/writer admission remain open.

`transport.download_merged_inputs` stages original complete Build/runtime readers beneath one
private parent and publishes only the fixed build/runtime pair together. Repeat whole original
seal/source/caller admission and both byte inventories after both children complete. No partial
caller output or native/effect authority is produced. Later native/current-policy/consumer and
writer rechecks remain mandatory; observations never reserve source artifact availability.

`build_ci.runtime_schema` owns initial runtime inventory data with original lane limits retained
inside the aggregate. Its labels/hashes are not native output mapping or complete byte proof.
Independently admitted plan, producer, exact owning Build and actual native validators remain
required before runtime transport or success. No second authored scenario catalog is permitted.
`runtime_exports` reads complete actual frozen data, including empty logs, and independently
copies it through existing MB1 no-follow/atomic primitives. Recheck source and stage after final
internal transport admission. Generic byte equality remains separate from native E2E authority.

MB11 runtime_inputs binds the tool-fenced verify_runtime hook (`execute_frozen_runtime_validator`,
the same second-account route as Build verification) to three fixed frozen
roots and retained canonical plan/complete owning Build/exact lane bytes. Recheck metadata,
inventories, directory identities and original caller snapshots; always terminate the admitted
validator. Context/execution data grant no native validity, Root receipt or status authority.
Private reclaimed read-only input preparation and actual source/API/tool/runtime/native admission
remain independent prerequisites. Preserve cross-run owning Build identity rather than replacing
it with the runtime producer. Root-only freeze_frozen_runtime_validation binds genuinely retained
successful execution/context to the existing independent frozen validation export, preserving
three-root pre/post checks and rejecting original caller drift. Constructible values grant no
provenance. Root-only prepare_runtime_validation grants exact runtime reads through MB1 bounded
regular-data handoff, preserving empty logs, original private copy and all three input identities
and snapshots. Failed admission restores admitted runtime root traversal when cleanup succeeds;
late close failure may leave a granted copy. Never consume a failed handoff; restage before retry.
Actual candidate reclamation/privilege handoff and real hosted lifecycle remain open; never
consume a private frozen copy left by a failed closing check.

Required hosted Linux runtime candidate-copy/read-grant cases preserve actual empty log/crash
data, source/owning Build binding, distinct inode/private transfer, validator-only reads and
candidate original ownership. Real hardlinks reject before publication. Keep fresh hosted/sudo/
account prerequisites; never execute these account tests on Windows or fake hosted environment
flags. These synthetic physical fixtures do not enroll complete tools or a native runtime.

MB11 runtime_freeze binds Root-only candidate runtime copying to original successful execution,
complete tracked-source witnesses, independently selected whole owning Build and actual lane bytes.
Close original source/plan/Build/caller admission inside publication and after private transfer;
recheck named-copy identity as well as the retained FD. MB1 privatize_regular_data_copy preserves
empty data through the existing protected ACL/owner/root-last 0700/0600 transfer. Only an independent
copy changes owner. Native second-UID validation/receipt and actual provenance/hosted/workflow
admission remain mandatory; no failed private copy may be consumed or uploaded.

MB11 runtime_handoff uses the existing closed execution-v1 private channel for original lane
execution/context with a fresh retained nonce, preserving field/bound compatibility. Shared
publication rereads staged bytes after closing admission. Runtime writer/Root reader bind exact
three-input snapshots and source-config identity before and after publication/freezing; Root
rereads the private original record afterward. No record chooses a program/path/hook. Actual
protected provenance and independently enrolled runtime Root process/request remain required;
constructible channel data grant no native/status authority and failed receipts are never consumed.

MB1 bounded_zip.extract_runtime is the fixed runtime-only empty-data ZIP route. Closed lane/
complete scope derives native payload caps centrally, with bounded central directory before
allocation, file count before inflation, compressed cap and shared hostile ZIP validation.
Pages/Build stay nonempty. Exact runtime envelope/native mapping/API admission is still required.

Root work has one process model. Each workflow step runs one `ci` command as the runner; work
that needs root runs in a child started as `/usr/bin/sudo -n -- <python> -I -B -S
<kit>/tools/ci_privileged_bootstrap.py --operation <name> --kit <kit> --kit-digest <digest>
--nonce <nonce>` (`build_ci.root_request.run_root_operation`). The bootstrap is stdlib-only until
it has re-computed kit-digest-v1 of that prologue-verified checkout and compared it with the
digest it was given; it then loads `mod_base` from the checkout's exact files, without editing
`sys.path`, and calls `build_ci.root_request_operations.execute_root_operation`. Never add a kit
import above that point, and keep its mirrored constants equal to `model/grammar.py` and
`model/limits.py` (`tests/test_ci_privileged_bootstrap.py`). There is no private kit copy, no
private interpreter and no byte digest of the tool trees: the prologue establishes kit integrity
and the host fence plus the metadata scan protect the tools.

Parent and child exchange data only through `mod-base.ci.root-request` records: one canonical
request per operation in its own runner-owned 0700 directory below the worker root (0600
single-link file, never replaced), read with the private record reader. The operation is one of
`grammar.CI_ROOT_OPERATIONS`; the request's closed `arguments` never select code, a hook or a
destination. Root derives the runner from the fixed home and passwd, admits the live host fence,
rebuilds controller sources from the protected copy on disk and re-reads the request after the
operation. Root never calls the GitHub API: anything that needs it happens in the runner before
the request is written. Adding an operation means adding it to `CI_ROOT_OPERATIONS`, the
bootstrap's mirror, `root_request_schema` (closed arguments), `root_request` (the runner's typed
request function) and `root_request_operations` (the handler), with a hosted test in
`tests/ci_linux_worker.py` that runs it through the real bootstrap.

## What a kit change reaches

- The kit runs in consumer mods only at the single commit each mod pins, and only after a
  protected merge here, an immutable `vX.Y.Z` tag and a pin bump in the mod. A change to `main`
  alone changes nothing in any mod, so never "fix" a mod by pushing a tag: a tag is immutable and
  every tag must be a complete, tested release.
- `src/`, `site/` and `requirements/` are the digested kit tree (kit-digest-v1). Any change there
  changes the `MB_KIT_TREE_DIGEST` literal of the callee workflows: run
  `python3 tools/update_tree_digest.py --write` and commit the result in the same change;
  `tests/test_tree_digest_literal.py` fails when the literal is stale.
- `template/`, `tools/` and `actions/` are staged into the Block Pops sandbox overlay but are not
  part of kit-digest-v1; the digested `src/mod_base/template/staged_files.sha256` (`template/` and
  `tools/`, the exact bytes a pre-v0.9.2 bootstrap checks, so never add a directory to it) and
  `staged_actions.sha256` (`actions/`) bind them instead. After any change below `actions/`,
  `template/` or `tools/`, run
  `PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m mod_base.template.lock --write` first, then
  refresh the digest literal (the locks live in `src/`); `tests/test_pin.py` fails when a lock is
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
  Bounded byte readers must use binary descriptors on platforms that translate text reads;
  CRLF and 0x1A are payload bytes, never implicit normalization or end-of-file markers.
- Match the style of the ported lineage code: plain typed functions and dataclasses, explicit
  errors, docstrings where the reason is not obvious, no dead code.

## Workflows and composites

`template.tool.RENDERED_CALLERS` is a closed protected-code destination/source/renderer registry.
Manifest/profile data cannot choose a renderer, remap an enrolled caller or confer an extension
policy. Pages is currently the sole entry. Unregistered workflow PIN/VERSION placeholders reject
before template writes in every manifest class. New Build/E2E entries require actual protected
templates, closed profile/transition admission and their native prerequisites; never route them
through Pages extensions or template deferral.
The new mod-base.ci.activation v1 parser validates closed data only at the prospective fixed
site/mod-base-build-activation.json path. Bind it to original native configuration and exact
protected transitions before wiring profiles. A mode label, including reviewed-rollback, is not
owner approval or execution/status authority; legacy consumers are not activated by parsing it.
controller.authenticate_controller_activation reads the fixed manifest from the original
API-authenticated controller tree under native protected-path policy, binds repository/profile
to its genuine config and repeats source/manifest/live identity admission. Retain that genuine
provenance; a constructed sources/manifest receipt is not approval or a protected transition.
Template check/sync/init now preflight any present activation against bounded regular native
config before writes. Legacy absence and bound disabled state remain supported; other modes
reject until fixed active templates/native admission exist. Do not call that guard complete
profile activation or removed-marker/rollback protection; pin/bootstrap parity is still required.
Bootstrap bump now runs the verified target kit's non-writing sync plan before rewriting pins.
Do not treat this error preflight as transactional rollback or complete profile/pin parity.

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
Prepare byte-sensitive template/caller fixtures with explicit UTF-8 and authored LF writes,
preserving intentionally hostile CRLF/encoding cases. Locale/newline conversion must not prevent
the intended extension/drift/security assertion from executing. Keep POSIX mode/symlink tests
intact when a Windows host cannot run them; report the actual failure instead of weakening it.

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
