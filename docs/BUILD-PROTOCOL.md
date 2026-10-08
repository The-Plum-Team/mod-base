# Protected Build protocol foundation

This is the inactive K1 foundation for [BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md), not an enabled
Build service. `mod_base.build_ci` is separate from `pages.build`, which builds the public site.
The planned release is v1.1.0. BUILD_ADAPTER_API, BuildGraphV1 and PackagedGraphV1 start at 1;
Pages adapter/schema/metric versions are unchanged.

K5 verify_batch_patch_bytes verifies both complete quiescent source copies through the existing
no-follow source inspector against authenticated merge-base/head inventories. It repeats the
copies and complete inventories, reauthenticates the patch, and closes with live member/patch
admission after the final byte pass. Unknown/modified source, observation drift, changed API
inventories and source/readiness changes reject. The internal mod-base.batch-source-bytes-v1
SHA-256 stream starts with canonical {format, tree_sha}, followed by each canonical source row
in path order with exactly {path, mode, size, git_blob, sha256}. The copy digest binds unchanged
source too; it is not installer, writer or source-root lifetime approval.
Caller must independently retain original private writer-excluded roots. Opaque .git metadata
remains outside source byte inspection; safe matching local Git graph/application, result trees,
strict manifests, leases, settlement and Linux/native evidence remain mandatory.

K5 authenticate_batch_patch now binds a live member and API-selected merge-base to their exact
commit/tree objects and common ancestry, deriving the patch from complete source inventories.
It ignores compare files/patch text and repeats trees, object/ancestry observations and member
admission before returning frozen BatchPatch observations. GitHub's compare response exposes
merge_base_commit but its file list is limited to 300: see the
[official compare documentation](https://docs.github.com/en/rest/commits/commits#compare-two-commits).
The new exact_tree reader retains existing tree transport bounds while denying commit aliases.
Source and original-controller tree inventories use it; legacy tree keeps its documented alias
behavior. Source directory inference runs after complete typed prefix-cap validation.
Independent original native policy, mode/link/restricted-transition admission, blob/patch bytes,
matching safe local Git graph/application, result trees, manifests, leases and settlement remain
mandatory. These API observations never authorize a batch writer, status or consumer activation.

K5 patch inventories now derive canonical changed paths from complete typed before/after source
inventories under independently admitted native exact-path policy. Each changed entry retains
the exact absent/present sides, mode, size and Git blob; renames are explicit deletion/addition,
both requiring permission. No-ops, unknown changed paths, malformed inventories/policy, case
aliases and inconsistent same-blob sizes reject. Combined policy file/prefix/byte caps derive
from two bounded source inventories; existing source/API/transport limits are unchanged.
The caller must bind genuine protected merge-base/head inventories and independently admit
native mode/link/restricted transitions. Controller-vs-head differences are not a substitute
for a member's merge-base patch. This pure derivation reads no bytes and constructs no Git
objects. Patch byte verification, safe application/result trees, manifests and writer leases
remain required; no new schema/CLI/consumer route is introduced here.
The shared source validator now enforces its existing root-inclusive prefix entry cap before
inserting another distinct prefix, rather than accumulating the entire rejected closure first.
The cap and accepted inventory shapes are unchanged; copied/source readers share this guard.

K5 batch membership now has a read-only collection API. It admits a distinct ordered tuple of
1..50 open same-repository PRs against the original caller's protected executing controller.
Draft state is retained and rechecked, preserving the original Quick Skin constructor's ability
to select drafts without granting their own deferred gates success. Nested batch/base branches
are excluded. The resulting batch's own native readiness/admission remains a separate gate.
Each complete head tree is authenticated from the exact head commit between generation reads;
the entire collection is read again before returning frozen observations. Native ordinary-path
policy, protected/restricted/kit/matrix exclusions, patch inventory, resulting tree, safe Git
construction, lease/write admission and settlement remain separate mandatory work. No manifest
kind, CLI, batch writer, consumer route or status grant is introduced by these observations.

The managed bootstrap plans synchronization with the verified target kit before rewriting pin
references. It calls the existing sync library with write=False, accepting ordinary drift while
rejecting template/config/activation errors before pin edits. The target's normal error boundary
distinguishes rejection from the expected drift list, without new CLI flags or data-selected code.
Actual writing still revalidates and can fail; this is not atomic rollback, protected activation
transition admission or support for active caller profiles. The copied bootstrap remains stdlib-only.

## Root operations

Each workflow step runs one `ci` command as the runner. Work that needs root runs in one child
process started by `build_ci.root_request.run_root_operation`:

```
/usr/bin/sudo -n -- <python> -I -B -S <kit>/tools/ci_privileged_bootstrap.py \
    --operation <name> --kit <kit> --kit-digest sha256:<hex> --nonce <hex>
```

`<kit>` is the kit checkout the job prologue verified against the pinned digest literal, and
`<python>` the interpreter the job runs on. The four flag/value pairs are exact and ordered; an
unknown operation, a malformed or oversized value, a checkout outside the runner home or any
extra argument is rejected with one fixed line before a kit byte is read. The bootstrap then
requires real root and an isolated, site-less, bytecode-free interpreter, derives the runner from
the owner of `/home/runner`, opens the checkout through directories that only root or the runner
own and nobody else can write, requires the running program to be that checkout's own
`tools/ci_privileged_bootstrap.py`, and re-computes kit-digest-v1 over `src/`, `site/` and
`requirements/` (regular single-link non-executable files only; no bytecode, `.pth`, link or
special entry). Only when that digest equals `--kit-digest` does it load `mod_base` from the
checkout's exact files, without editing `sys.path`; it then re-computes the digest once more.
Every failure up to here prints `mod-base: root bootstrap rejected` and exits 2. The bootstrap
sets umask 077 and a 1800-second alarm on itself and calls
`build_ci.root_request_operations.execute_root_operation`, which again checks the kit digest with
the kit's own implementation and the staged-file locks that bind `tools/`, `template/` and
`actions/`. There is no private copy of the kit and no private interpreter: the prologue
establishes kit integrity, and the host fence plus the tool-root scan protect the tools.

Parent and child exchange data only through `mod-base.ci.root-request` v1, a local kind that is
never uploaded. The runner publishes one canonical request per operation, exclusively, at
`mod-base-worker/root-request-<operation>/ci-root-request.json` (runner-owned 0700 directory,
0600 single-link file). It has exactly `kind`, `schema_version`, `operation`, `nonce`, `boundary`
(the fenced home: fixed path, uid, gid, device, inode, original mode) and `arguments`, a closed
object per operation. Nothing in it names a program, a hook, a command or a destination path;
only `stage-candidate` names directories, and only ones root reads below the fenced runner home.
Root reads it with the private record reader (no-follow directories, exact owner, group and mode,
single link, stable identity and timestamps, canonical bytes), requires the nonce and operation
it was started with, requires the boundary to name the derived runner and to be the live 0700
home, and re-reads the request unchanged after the operation. A request is data from the
sudo-capable runner: it proves no provenance, so every operation re-admits what it touches. Root
never calls the GitHub API; whatever needs it happens in the runner before the request exists.

The operations are the closed tuple `grammar.CI_ROOT_OPERATIONS`, mirrored by the bootstrap:

| Operation | Arguments | Effect |
|---|---|---|
| `host-fence` | none | Closes the hosted image's world-writable trees and proves that none is left; runs before any worker account exists |
| `stage-candidate` | `candidate`, `repository`, `tested_sha`, `tested_tree`, `inventory`, `source`, `overlay`, `gradle_seed` | Publishes the candidate's `repository/` (tracked source, curated `.git`, kit overlay) and seeds its Gradle home; runs once, before the candidate ever runs |
| `freeze-build-validation` | `validator`, `sources`, `plan`, `envelope`, `run_id`, `run_attempt`, `execution_nonce` | Seals the Build or target verifier's receipt into `sealed-validation/` |
| `freeze-runtime-validation` | `validator`, `sources`, `plan`, `build`, `runtime`, `lane_id`, `run_id`, `run_attempt`, `execution_nonce` | Seals one runtime lane verifier's receipt into `sealed-validation/` |
| `grant-controller` | `validator`, `candidate`, `subject`, `sources` | Hands the runner's private `controller/` (the protected adapter closure) to the validator read-only |
| `grant-plan-inputs` | `validator`, `candidate`, `inputs` | Hands the staged candidate files in `validation-input/` (each named with its SHA-256: the inventory, the scenario contract, then the extra plan inputs by name) to the validator read-only, before `derive_plan` |
| `take-derived-plan` | `validator`, `candidate` | Copies the one file `derive_plan` wrote into the runner-private `derived-plan/` and removes the original |
| `grant-validation-inputs` | `validator`, `candidate`, `plan` | Hands the complete `validation-input/` (the plan and every candidate file it binds) to the validator read-only |

The last four are the operations of the worker lifecycle below. `candidate` is the uid and gid of
the job's candidate account, or `null` in a job that allocated the validator alone; root requires
the live accounts to be exactly the ones named and a candidate that is not named to be absent.

`candidate` and `validator` are the uid and gid of the live fixed account and must differ from the
runner's. For `stage-candidate`, `inventory` is the complete tested-tree inventory (one row of
`path`, `mode`, `size` and `git_blob` per tracked blob or link, in ascending path order, under the
existing source caps), `source` is the runner's checkout of `tested_sha`, `overlay` holds the
staged kit's `path` with its pin (`sha`, `version`) and `tree_digest`, and `gradle_seed` is a
restored cache directory or null; "Candidate checkout staging" below says what root requires of
them. `sources` is controller source metadata only (controller SHA and tree, the config and each
import file with path, mode, Git blob, SHA-256 and size); root rebuilds the bytes from the
validator's protected controller copy and verifies the whole copy. `plan`, `envelope`, `build`
and `runtime` are the existing kinds under their existing caps; the plan must name the executing
kit's version and digest. `execution_nonce` names the `mod-base.ci.execution` record of the
verifier run and must differ from the request nonce. Both freeze operations authenticate the
read-only inputs before and after sealing and always terminate the validator. `host-fence` is
described with the host fences below.

## Worker lifecycle of a job

Every step of a job is one `ci` command run by the runner (`build_ci.lifecycle`). The steps share
the private state directory `ci subject` creates (`--state`, in `$RUNNER_TEMP`) and the fixed
worker root. State records are canonical JSON, written once, read strictly, never replaced.

1. `ci worker-prepare --roles validator|candidate+validator --python PATH [--java-home PATH]...`
   admits the hosted layout and writes `worker-host.json` (the home's identity and original mode)
   before it changes anything, so a second prepare of the job stops there and the last step can
   always restore the home. It then creates the worker boundary, closes the runner home, runs
   `host-fence`, admits the tool trees (the interpreter's prefix as named and as resolved, and
   every JDK home), allocates the accounts, copies the protected adapter closure from the mod
   checkout the prologue verified into `controller/` (no API read) and runs `grant-controller`.
   It ends with every account terminated and locked and writes `worker.json`: the boundary, the
   accounts, the interpreter, the JDK homes, the tool receipt and the SHA-256 of the protected
   config.
2. `ci plan [--candidate DIR] [--expect-sha256 HEX]` first opens the prepared worker, as every later
   step does: the protected config is the recorded one, the home is still fenced, the accounts
   are the recorded ones and the tool trees are unchanged. It reads the candidate files the
   protected config names: from the Git objects of the candidate checkout when the job has one (its `HEAD`
   must be the tested commit; the working files are never read), otherwise from the API at the
   tested tree (one request for the tree and one per file). It stages them in `validation-input/`
   under their names (`adapter.plan_sources`), runs `grant-plan-inputs`,
   runs `derive_plan` as the validator, runs `take-derived-plan`, builds the plan around the one
   file the hook wrote and compares its hash with `--expect-sha256` when given. It then stages the
   input root again with the plan, runs `grant-validation-inputs` and writes `ci-plan.json` to
   the state. Afterwards every protected hook of the job finds `validation-input/` complete.
3. `ci worker-finish` (`if: always()`) terminates and locks every worker account the host has,
   requires that neither owns a process once both are locked, and only then gives the runner home
   its original mode back. It reads nothing but the state and the host, and it is safe to run
   twice, after a prepare that failed anywhere and without one. An account entry it cannot
   authenticate is locked by name, never signalled, and keeps the home closed.

Between two hook runs both accounts are terminated and locked; `sudo` still starts the next hook
for a locked account, so a job runs several hooks as the same account. A hook starts with a umask
of 077, set inside the account because `sudo`'s session applies the login umask. A hook that exits
non-zero, outlives its timeout or leaves a running process behind fails the step, and the account
is swept in every case.

## Plan v1

`mod-base.build.plan` has exactly `kind`, `schema_version`, `build_adapter_api`, `identity`,
`profile`, `plan_inputs`, `targets`, `lanes`, `plan_sha256`. The profile is `quick-skin` or `block-pops`, not an
execution activation flag. Strict bounded JSON decoding and exact key/type validators apply.
The plan hash covers the canonical document excluding only `plan_sha256`, including identity.
Protected adapters derive it from bounded inert candidate blobs (the inventory, the scenario
contract and the extra files the protected config names) and fan-in independently
re-derives it. A structurally valid candidate plan is never authority.

`plan_inputs` lists the extra candidate files the plan was derived from, as `{name, sha256}` in
name order (at most 8; `[]` for a config that names none): `name` is the staged file name of the
protected config's `plan_inputs` entry and `sha256` the hash of the candidate blob at the tested
tree. Quick Skin's plan binds `gradle.properties` this way, the file that holds the version in
its JAR names. The identity keeps binding the inventory and the scenario contract, so no other
record kind changes shape; every record binds the extra files through `plan_sha256`, and a reuse
compares them through the tested tree and the policy digest, which fix the files and their paths.

Identity has exactly these fields:

- `repository`, `source_repository`, `pr_number` (zero only for non-PR protected subjects).
- `head_sha`, `head_branch`, `base_sha`, `base_branch`.
- `controller_sha`, `controller_workflow`, `controller_ref` (protected repository/workflow/base ref).
- `kit`: exact `repository`, `sha`, `version`, `tree_digest`.
- `tested_sha`, `tested_tree`, `tested_parents` (PR merge parents are ordered base/head).
- `policy_sha256`, `inventory_blob`, `inventory_sha256`, `scenario_sha256`,
  `runtime_selection_sha256`, `graph_version`.

The producer run/attempt, artifact descriptor and Build-owner tuple belong in the records below.
The API head is not the tested merge. `authenticate_pr_identity` independently checks a
ready open same-repository PR, live default/controller/base, exact current head/ref, merge commit,
ordered parents and complete tree. It does not verify owner approval, policy/pin reachability,
artifact availability or future records; those are still required for complete admission.

`authenticate_source_identity` retains that PR route and admits non-PR source identity only
from the protected default history. Both branch fields name the current default, and base/controller
equal its live head. The tested/head commit may be that head or an independently API-proven ancestor;
its complete tree and ordered parents must match the immutable Git object. Current-head subjects
also match the branch API tree. Recheck the default name and controller commit/tree after reading.
Source inventory and protected controller-source admission bracket their reads with this check,
so historical subject bytes never substitute for the current controller's import sources.
The existing v1 parent bound remains two; unsupported octopus objects reject. This primitive does
not authenticate a requesting workflow/run/attempt/nonce, protected dispatch ref, pin or approved
profile. Those requirements, explicit full release/rebuild policy, staging and production integration
remain open; protected ancestry alone does not authorize execution, reuse or status publication.

Every target has `id`, `java`, `native_contract_sha256`, `outputs`; each output has exactly
`path`, `lane_id`, `role`. Roles are production, harness, SBOM, native report and build log. Each lane has
`id`, `target_id`, `native_contract_sha256`, ordered unique `obligations`. IDs are opaque bounded
ASCII tokens without the transport delimiter `--`; plan and artifact unit admission use the
same CI_UNIT_ID grammar. The mods' runtime row ids hold `--`, so a lane is named by its artifact
node. Every target has a lane and every lane names a target.

An output's `lane_id` names a lane of its own target, or is null for an output that belongs to
the target as a whole: a staged manifest, a report, a log or the SBOM of the whole target. The
rules follow what the two mods stage:

- every lane has exactly one production and one harness output, and neither is ever target-scoped;
- an SBOM is optional (Block Pops stages none): at most one per lane and one per target as a whole
  (Quick Skin stages one per target);
- every target has at least one native report of either scope (both mods stage a manifest per
  target partition); further native reports and build logs are optional and may repeat, retaining
  independent compiler/JDK/task observations.

Paths are canonical export paths
and globally unique even under case folding, including parent-directory spelling, so a file a mod
writes once per target carries the target in its path (`targets/<target id>/artifacts.json`). A file cannot
also be a directory, and the outer `ci-envelope.json` name is reserved. The whole logical plan
keeps the export file-count ceiling across every partition. No commands or permission fields are accepted.
Native validators still prove actual JAR/report/task/JDK/scenario observations from frozen bytes.

The limits live in `model.limits`, separately from Pages. The plan is at most 4 MiB, with at most
256 targets and 256 lanes (hosted matrix limit). Export ceilings retain the design's per-export
file/entry/byte bounds. A partition does not multiply the original complete-tree limit. Constants
alone do not establish that future worker/transport code enforces them; those gates remain open.

## Configuration and records v1

Complete Build bundle transport is separately owned by `build_ci.transport`, with immutable
numeric IDs and no changes to Pages artifact grammar/download/rotation. Its caller must enroll
the producer workflow path from protected policy and admit the plan independently. The transport
requires a completed successful latest attempt, exact protected API head/event/ref/repository,
executing kit SHA, full Build graph digest and exact aggregate upload window. It compares selected
ID/name/size/digest/creation/expiry with live artifact metadata, verifies owner/head and non-expiry,
then checks ZIP length and SHA-256. A private temporary extraction uses `bounded_zip.extract_build`
with fixed 20,000-entry CI export limits, preserving the existing Pages extractor ceiling.
Canonical envelope, complete scope, producer and every file hash/size are verified before a
second provenance check and independent atomic private copy. Temporary extraction is removed.
Newest producer-run selection, protected pin/policy and request authorization, native compiler
validation, target fan-in during a running producer and production workflow integration remain
unfinished; this function alone cannot authorize a gate or reuse. Real Linux download/copy and
corrupt-inventory cleanup fixtures are wired into the required Linux suite but not executed here.

`download_target_partition` additionally binds the admitted assembler's run/attempt and an exact
plan target. It accepts an in-progress producer with null conclusion or a completed successful
producer, never queued/failed/cancelled execution. Latest and exact-attempt workflow IDs agree.
The observed partial job list has no duplicates or unenrolled jobs; the exact target job must
have succeeded with unique successful seal/upload steps, ordered seal-before-upload and the
selected exact upload window. Its graph digest names the protected full contract, not an observed
successful full graph. Artifact name and envelope scope/target must match that target. Byte,
metadata and source/attempt freshness checks bracket private independent copying as for complete
bundles. Missing later aggregate/gate jobs cannot prevent legitimate same-run fan-in. This does
not permit cross-run target mixing or authorize partial coverage: complete ordered target union,
successful policy, native aggregate validation and final full graph remain mandatory and their
production integration remains incomplete. The complete bundle route still requires full success.
Both routes bind the producer API head to the executing controller. A push additionally requires
tested subject equal to that controller; it cannot attribute an old push to a new live controller.
A protected dispatch may instead name a historical tested subject while executing the current
controller. Request/run/nonce and protected dispatch approval remain independent obligations.

`download_target_set` prepares the exact ordered complete target inputs of an independently
authenticated assembler run/attempt in one private atomic directory. Before any ZIP download it
rejects missing/extra/reordered targets, duplicate numeric IDs, foreign plan/producer identities
and unenrolled workflows. Protected plan/policy jobs and every exact target seal/upload must
succeed. Two shared producer/job/source snapshots bracket the full set, with per-artifact metadata
checks, instead of repeating the whole API context for each target. ZIPs are processed one at a
time into fixed protected ordinal children `target-0`, `target-1`, etc.; manifests cannot choose
destinations. Each canonical envelope/byte inventory is reverified and the complete union passes
the original 10,000-file, 20,000-logical-entry and 2 GiB expanded payload limits. Logical entries
include inferred directories, root and the future aggregate envelope. Failed or stale preparation
does not publish partial input sets; atomic cleanup removes the private stage.

An explicit additional compressed-set budget is 4 GiB, independently of the 512 MiB per-archive
cap. This bounds total transfer/storage across up to 256 target archives while allowing the
2 GiB logical payload, separate per-target envelopes and ZIP metadata. The physical wrapped-input
entry ceiling derives from the original file count, target count and path-depth bounds; repeated
wrapper directories do not multiply the original logical export budget. These are new aggregate
protections, not claims about native limits. Native/runtime 512 MiB fan-in limits remain unchanged
and must still be enforced in their eventual native/domain integration. The prepared set is an
input to native aggregate verification, not a minted aggregate envelope or successful gate.
Native complete-bundle construction, second-UID aggregate receipt and production integration
remain incomplete. Required Linux complete-set and second-ZIP-failure cleanup cases are unexecuted.

The generic policy runner remains `tools/parallel_unittest.py`, bound by the staged tools lock.
Its default explicit `block-pops` profile keeps exact per-unit discovery/run counts and the
root-package import layout. `--policy-profile quick-skin` requires one start directory, uses
that directory as the top-level import root and mirrors the parent import path into spawned
workers. It admits only a whole fixture-class `setUpClass` skip with zero testsRun and exactly
one skipped holder per repeat. The global suite must still execute at least one test. Partial
counts, fixture cleanup errors, failures, unexpected successes, failed imports or dead workers
fail. Native method and tearDownClass skips retain unittest's existing counts; expected failures
retain native success semantics. Reexported classes retain their multiplicity. Each repeat now
uses a fresh TextTestResult, fixing the original runner's reuse of stale previous-class state
after tearDownClass. Synthetic repeat fixtures agree with serial lifecycle/count behavior.

Common policy ceilings are 100000 discovered tests and 256 worker processes. The discovery
ceiling is checked while enumerating before scheduling/class reload. In-process diagnostic
streams retain at most the existing 16 MiB log cap per scheduling unit, preserving a valid UTF-8
prefix and reporting truncation; worker pipe draining/quiescence remain separate requirements.
The helper library is stdlib-only and reads no ambient environment. The tool's verified kit src
must be importable before discovery. These are new common bounded mechanisms, not proof of full
native-suite parity or complete interpreter/import/overlay provenance.

Discovery itself imports test modules: run the entire tool only inside the disposable
credentialless policy worker, never a token-bearing protected controller. Configured profile,
source closure and argv are protected native policy, not candidate choices. Count objects and
log strings cannot authorize uploads/statuses or establish native policy validity. Required
Linux synthetic cases exercise real candidate/subworker UIDs, private temporary directories,
allowed class skips and zero-executed failure through bounded account dispatch. Those cases are
unexecuted on this Windows host. QS/BP complete native policy suites and protected integration
remain required before activation.

Every following document has the strict `kind` and `schema_version: 1` header. Validators in
`build_ci.config` and `build_ci.records` are normative. Configuration is bounded to 1 MiB;
envelopes, selection/gate/reuse records and tested-seal artifacts to 4 MiB. These new kinds
explicitly reject in the predecessor reader.

`mod-base.ci.validation` is a new inactive schema-1 local verifier output kind, supported only
by the planned v1.1.0 protocol. It has exactly `kind`, `schema_version`, `identity`, `plan_sha256`,
`profile`, `hook`, nullable `unit_id`, `run_id`, `run_attempt`, `source_config_sha256`, `input_sha256`
and ordered `reports`. Hooks are verify_target/verify_build/verify_runtime; aggregate Build has
no unit and requires all protected targets in plan order, while a target/runtime hook requires
exactly its enrolled target/lane. Each report has `unit_id`, `native_contract_sha256`, canonical
repository `path` ending .json, positive `size` and `sha256`. Units/contracts equal the protected
plan. Paths are unique without case/parent aliases and cannot overwrite ci-validation.json or
the Build envelope. There is no success boolean. Existing gate/envelope schemas are unchanged;
the exhaustive ledger requires v1.0.3 to reject this new kind.

`verify_validation_export` compares the record with retained protected execution/input context,
not its own claims. The canonical outer record is at most 4 MiB; there are at most 256 reports,
each at most the new 4 MiB validator-output report ceiling. Existing entry and complete-export byte
caps remain additional limits. Exact file size/hash inventory is independently recomputed,
each report is reread/rehashed and strictly decoded as a canonical JSON object, and outer bytes
are rechecked afterward. Duplicate keys, nonfinite values, noncanonical JSON and extra/missing
files reject even with matching declared hashes. Mod-owned closed report schemas and native
semantics must separately be verified; generic JSON/byte admission does not confer validity.

`build_ci.inputs` writes the validator's input root, the fixed `validation-input` directory: the
bytes of the candidate files a plan is derived from (`inventory`, `scenario-contract` and the
extra plan inputs the protected config names, each under its staged name) and,
once the plan exists, the existing `mod-base.build.plan` document as `ci-plan.json`, which binds
every one of them by SHA-256. This adds no document kind/version. The independent atomic stage
has a 4 MiB cap per file and holds exactly the files of its state (the candidate files before
the plan, the plan as well with it) and no other entry; undeclared import files, links, changed candidate bytes and
alternate canonical bytes reject. Protected-root handoff grants only the fixed validator group
reads (0750 directories/0640 files), rechecking ownership, inode, absent ACLs and bytes. Complete
import enrollment remains a protected caller obligation.

`execute_frozen_build_validator` supports same-producer complete aggregate Build verification.
It requires the retained plan and complete envelope, exact producing run/attempt, both fixed
read-only input trees and protected account/layout identities. It independently verifies input
metadata and all plan/Build bytes before and after the closed `verify_build` execution, retaining
directory identities across execution. It returns the actual execution result and SHA-256 of
the canonical Build envelope for `freeze_validation_export`'s input binding; the envelope's
protected plan hash and file hashes transitively bind the complete input. Native hooks read the
fixed plan and sealed Build paths and emit the existing verifier-output record. Cross-run runtime
selection and runtime verification are separate unfinished lifecycle compositions. Zero exit and
matching objects still cannot substitute for native closed schemas or API/status authority.

`execute_frozen_target_validator` uses the same fixed read-only input lifecycle for an exact
enrolled target partition. It requires `scope: target`, that exact `target_id`, its complete
planned output inventory and the same producing run/attempt. Complete bundles and other target
partitions cannot substitute. Only the existing protected `verify_target` hook and MB_TARGET_ID
are forwarded; paths, environment and commands remain closed. The returned canonical partition
digest binds subsequent verifier-output freezing. Native compiler/JDK/task/packaging witness
validation and protected admission remain separate requirements.

Required hosted aggregate/target fixtures independently check actual second-UID hook/unit
arguments, read the fixed plan/Build bytes, confirm input paths are not writable, produce the
existing closed receipts and freeze them with retained execution/input digests. Both fixtures
are synthetic and unexecuted on this Windows host; they do not establish QS/BP native parity.

The required hosted controller fixture now reads/hashes real fixed plan/Build inputs from the
second UID, returns that digest and freezes its output using the retained execution/digest.
Additional Linux plan tests check existing-output preservation, changed bytes, links/specials,
undeclared .pth files and stage cleanup. These cases remain unexecuted on the Windows host.

`materialize_validation_export` creates new independent single-link files in a private stage,
verifies the copied inventory and independently rechecks the stage before exclusive atomic
publication. Existing outputs are never replaced and writer failure leaves no published output.
Actual protected verification, validator UID termination/locking, readable ownership reclamation,
excluded source writers, destination-parent protection and final receipt/status/API admission
remain caller requirements. These inactive readers/copy functions do not seal a gate by themselves.

`freeze_validation_export` composes the protected-root fixed output lifecycle. Verifiers emit
their local packet at `WORKER_ROOT/validator-home/validation`, with 0700 directories/0600 files,
one validator UID/GID, single-link regular files and no ACLs. Protected setup authenticates
host/passwd, retained successful actual execution/source evidence, profile and fixed/disjoint
accounts; terminates/locks the validator; authenticates traversal roots, its private home and
the complete private output metadata; then copies verified context/bytes independently into
`WORKER_ROOT/sealed-validation`. Existing frozen copies are never replaced.

Only the newly protected-root-created copy is transferred to runner UID/GID using
`privatize_tree_copy`, with ACL removal, 0700 dirs/0600 files and root-last ownership transfer.
The validator original is never chowned. Final inode, private metadata, exact receipt/reports
and host fence are rechecked. Accepted-copy failures retain 0700 traversal; rejected foreign
copies are not chmodded. `authenticate_tree_private_access` performs the corresponding bounded
no-follow owner/group/mode/ACL checks and allows root ownership only as metadata for a fresh
protected copy. Role/copy origin and byte authenticity still require the composition above.
Constructed in-memory receipts are not provenance; native schema/semantic/input validation,
complete import/installer provenance, production root dispatch, gate/API sealing and upload
integration remain required and inactive.

`mod-base.build.config` lives at `scripts/ci/mod-base-build.json`. Its remaining fields are
`repository`, `profile`, `build_adapter_api`, `adapter`, `timeouts`. Adapter has distinct fixed
`path`, `dispatcher`, `policy` Python entrypoints under `scripts/ci/`, plus sorted `files` with
exact `path`/`sha256` pairs, including all three entrypoints and their protected import closure.
At most 256 files are accepted. Timeouts explicitly specify `policy_seconds`, `target_seconds`,
`runtime_seconds`, `validator_seconds`, each positive and at most six hours. No matrix/scenario
catalog, arbitrary commands, runner/permission/secret or activation fields exist. Native
admission must authenticate the protected sources and preserve native timeout semantics.

Records bind `identity`, `plan_sha256`, `profile`. A selected descriptor's producer has exact `run_id`, `run_attempt`,
`workflow_path`, `workflow_ref`, `api_head_sha`, `event`, `graph_sha256`, `upload_window`.
The workflow ref names the same protected repository/path/default branch. A PR requires
`pull_request_target`; protected non-PR events are push, dispatch or schedule. Window has exact
UTC `started_at`/`completed_at`. API head remains independent of tested merge SHA.

An embedded descriptor adds `producer` and `artifact` to that binding. Artifact has exact
numeric `id`, attempt-specific `name`, `digest`, compressed `size`, UTC `created_at`, `expires_at`.
Creation must fall in the owner's upload window and expiry after creation. The name binds the
owner run/attempt, never the tested identity. `mb-ci-target` and `mb-ci-runtime` also bind an
opaque target/lane; `mb-ci-tested` binds build or packaged. Complete build/results and reuse
names have no unit. Every CI name is excluded from the Pages parser and rotation.

`mod-base.build.envelope` has producer, `scope` (target or complete) and nullable `target_id`.
Its producer has only `run_id`, `run_attempt`, `workflow_path`, `workflow_ref`,
`api_head_sha`, `event`, `graph_sha256`. It rejects `upload_window` and all artifact transport
metadata: the export is written before upload, so its own eventual step completion/digest/ID
cannot be known then. `bind_build_envelope` independently validates envelope and selected
descriptor, compares every producer identity field and full plan/profile, and binds exact
artifact scope/target. Actual upload window, immutable numeric ID/digest/size/expiry remain
mandatory in the descriptor and are reauthenticated against the API by transport. Binding never
rewrites the export or infers the window from its self-report.

The envelope also has sorted `files` and sorted `native_reports`. Each file has `path`, `size`, `sha256`, `lane_id`,
`role`; a path is an export path, as in the plan, and the frozen tree, the archive encoder and
`extract_build` apply that same grammar. `lane_id` is null for a file of the target as a whole, exactly
as planned; a production or harness file always names its lane. With an independently derived plan it must equal the exact target partition or complete
union. Native reports equal the report-role subset. Actual size/hash equality is separately
verified by `verify_build_export` over canonical `ci-envelope.json` and descriptor-relative MB1
regular-file inventory. No missing/extra files, links, duplicate names or envelope mutation
are accepted. The caller must first terminate/lock disposable workers and freeze their output;
this reader does not establish that lifecycle or prove native report semantics.

Build native-report payload limits are profile-specific: Block Pops retains the 8 MiB original
compiler-report limit from `scripts/release/build_evidence.py:MAX_REPORT_BYTES` and
`build_matrix.py:_observation_report` at reviewed commit
`47a890ae46a2878fb08d29a932803ab91bccdcd9`; Quick Skin retains the initial inactive 4 MiB
transport limit, a kit choice below the 16 MiB its own manifest reader admits
(`scripts/release/artifact_manifest.py:15` at `c0cdc01ab20f1eac663c628011520c76fc7e3d7a`). The
measured files leave both bounds as they are: Block Pops' build report is 1.28 MiB and its
manifest 28.7 KiB, Quick Skin's manifest 30.1 KiB (`tests/fixtures/ci_native/*/measured.json`).
An `sbom` file is limited to 16 MiB, the cap of Quick Skin's own SBOM reader
(`scripts/release/generate_sbom.py:31`; its measured SBOM is 46.5 KiB); before, that role fell
back to the 1 GiB ceiling of any export file. Every output role now has a bound of its own.
This is an initial-format correction
before release, not an increase caused by a failing pipeline. New validator-output reports and
records remain limited independently to 4 MiB. Whole-export, JAR, log and compressed ZIP caps
remain unchanged. Accepting an inventoried compiler report does not validate its native semantics.

Initial-format decision: finalize this new inactive kind at schema 1 before its first release,
discarding the local draft envelope shape that embedded the future upload window. This kind is
absent from the committed predecessor/released v1.0.3 registry; the exhaustive ledger retains
`new-kind`, previous version null, writer/reader 1 and explicit predecessor rejection. There is
no released envelope v1 predecessor to migrate and no fictional schema 0 or 2. The old local
draft shape now rejects rather than being silently interpreted. This decision does not permit
required-field changes within any released schema. Existing Pages formats, adapter API and
runtime version remain unchanged. Gate/reuse apply the corresponding initial-format decision
below; actual record API transport and execution-timeline admission remain required before activation.

`mod-base.ci.gate` and `mod-base.ci.reuse` likewise store only the seven pre-upload producer
identity fields and reject the old local draft's own `upload_window`. Nested input/source
descriptors still retain their already-observed API windows and immutable artifact metadata.
`bind_gate_receipt` checks the complete native plan, record identity/profile/producer and exact
tested artifact unit (build or packaged). Every evidence upload, including packaged's owning
Build, must end before the selected gate-record upload starts. `bind_reuse_reference` checks
the original direct-seal constraints, covered plan when supplied, exact reuse descriptor and
both source seal uploads before its selected record upload. Source IDs cannot collide with
the selected record ID. These times come from authenticated descriptors, never the pre-upload
record, and binding does not rewrite record bytes. Pure validate_* functions validate structure;
their success alone cannot authorize a reuse/gate. Selected-descriptor binding and independent
API/native admission are mandatory.

For reuse, source-before-record chronology is necessary but not sufficient: K6 additionally
requires source completion before the actual protected verifier starts, using independent API
step/job execution evidence and the closed protected graph. Removing a self-reported future
window cannot remove that requirement. This timeline authentication, original whole graphs,
coherent available source artifacts, actual tree/policy equality and final record transport are
still incomplete. The new gate/reuse kinds also remain initial schema 1 before first release;
the exhaustive new-kind ledger and predecessor rejection are unchanged, and old local draft
record shapes reject. This is not a migration exception for released schemas. The synthetic
reuse fixture now models a push with tested subject equal to its actual controller, retaining
the distinct original PR identity; this does not claim any live merge or native runtime proof.

`validate_target_partitions` requires descriptors/envelopes for every protected target in exact
plan order, one distinct artifact ID each, matching target names and receipt bindings, and one
producer/attempt/graph. Each worker may have its own upload window. Whole-tree byte/file limits
apply to the union rather than independently granting that budget to every partition. API
authentication, immutable-ID download and frozen-byte/native verification must precede assembly.

`mod-base.ci.selection` adds `request` (run/attempt, nonce SHA-256, workflow path/ref), `build`
(complete Build descriptor) and `envelope_sha256`. Request references its protected workflow;
the selected Build retains exactly the admitted binding. It cannot select a target partition.

`mod-base.ci.gate` adds producer, `gate` (build or packaged), `mode` (currently full only),
`artifacts`, nullable `owning_build`, ordered `native_receipts`. Build requires one complete
bundle and every target receipt; packaged requires the exact owning Build, each lane's runtime
artifact, one results aggregate and every lane receipt. A receipt has `unit_id`,
`native_contract_sha256`, `report_sha256`; contracts/order equal the protected plan. Owned
descriptors retain the same attempt/graph with independent upload windows. Owning Build IDs
cannot collide with runtime IDs. Self-reported native hashes alone never prove validity.

A gate writes its receipt from inside the run it judges (`ci seal-gate`, `build_ci.gate`). The run
is still in progress, so the gate authenticates what exists: the live source, the run as its
latest attempt, every job the graph finishes before the gate with its expected conclusion and
seal-before-upload, and exactly one unexpired artifact for every sealing job among them
(`build_ci.describe`). A pull request has one admissible mode; a protected run shows its mode by
the job names of its own attempt and must then be exactly that graph. A Build gate downloads the
complete Build of its attempt, verifies the export and the `verify_build` validation record that
lies beside its envelope (`ci-validation.json` and its reports), and takes each target's
`report_sha256` from that record. A packaged gate downloads `mod-base.ci.results`, the index its
aggregating job sealed (docs/SCHEMAS.md), requires it to list exactly this attempt's lane
artifacts and authenticates the owning Build. The reader of a receipt requires the completed
graph and the real chronology. A job or an artifact of an earlier attempt is a rejection that
says so: after a failed-jobs-only rerun the recovery is to rerun all jobs.

`mod-base.ci.reuse` adds current producer and `source`: original PR binding plus independent
direct `build_seal`/`packaged_seal` tested descriptors. Covered default-branch commit and original
tested commit retain separate current/non-PR and original/PR bindings even when the final merged
SHA equals the original synthetic tested SHA. SHA inequality is not an admission rule. Repository,
policy, pin, graph, tree, inventory,
scenario and selection semantics must agree; source seals must precede actual protected reuse
verification, independently authenticated through API execution evidence. Reuse
references cannot chain. This structural check does not yet authenticate the actual merged PR,
latest attempts, original complete graphs or artifact availability: full K6 admission is pending.
This corrects the inactive initial schema-1 semantics before first release; no fields, versions,
common-kind predecessor support or Pages schemas change. An equal SHA alone supplies no merged
PR, historical gate, protected-policy, chronology or native admission.

## Initial v1 PR Build run selection

The live selector and separate select_latest_merged_pr_build route share only the fixed private
producer-selection engine. Their source admission is selected by protected library code, never
configuration or a public callback. Both retain independent bounded canonical plan bytes and
reject original caller or retained-plan changes before returning success, absence or pending.
Historical selection first admits the original ordered test parents/tree and actual final merged
PR/current protected history, then retains that observation around every source recheck. Run
queries and producer/kit/graph/artifact bindings continue to use the original controller API head;
the current default SHA never replaces original provenance. A final merged SHA may equal the
original test merge. No source field chooses execution or a query program.

Historical selection keeps the same unfiltered-success newest-first contract, pending/absence
semantics, complete latest-attempt Build graph and aggregate seal/upload/immutable artifact
admission. Failed/cancelled newest or malformed/corrupt/moved evidence never falls back to an
older successful Build. revalidate_latest_merged_pr_build additionally retains the original
descriptor bytes and rejects mutations/supersession around independent consumption. Caller
must independently admit original plan/workflow and current controller/final merged identity.
This adds no historical packaged producer selection, coherent pair download, payload/native
validation, original/current policy equivalence, verifier chronology or reuse/status authority.
Those complete K6 prerequisites and actual hosted API/consumer conformance remain mandatory.

The new inactive producer profile uses a closed versioned selection marker:
`mb-ci-build-v1 profile=<profile> pr=<number> head=<sha> base=<sha> tested=<sha>`.
`model.grammar` owns its builder/parser. This is an explicit initial contract before first release,
not a change to an already released graph/job name or an implicit legacy-title reader. A protected
caller must emit the marker from authenticated event/subject fields and its fixed profile; PR
titles, candidate flags and observed jobs never choose it. [GitHub's run-name documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#run-name)
permits expressions using github/inputs contexts; actual title/API/deferral behavior remains a
required hosted canary and managed-caller integration gate. No caller has been activated here.

`select_latest_pr_build` brackets a source-authenticated read of at most 1000 newest runs under
the protected workflow path, default branch, controller API head and pull_request_target event.
It supplies no status/conclusion filter. Every listed run must use the enrolled initial title
grammar/profile; unsupported or ambiguous metadata fails closed rather than hiding a new failed
producer behind an older success. From exact PR/head/base/tested matches, select the latest by
created_at, run ID and latest attempt before checking result. Other PRs and other exact tuples
cannot supply coverage. Absent or pending means no bundle and no permission to compile/succeed;
completed failed/cancelled/neutral/skipped attempts reject with no older fallback.

A completed successful selection then authenticates the exact latest attempt, protected run/
workflow identity, executing kit and complete Build graph, derives the aggregate's successful
actual upload window, and requires one exact current-attempt complete artifact by name. The
[workflow-run artifact API](https://docs.github.com/en/rest/actions/artifacts#list-workflow-run-artifacts)
supports that exact name filter; immutable ID/name/digest/size/created/expiry/owner/head are retained
and reauthenticated. Relist the unfiltered runs and recheck run/attempt/workflow/source before
returning to reject a newer producer or rerun arriving during selection. None is never a success
or an API-failure fallback. A missing/duplicate/expired/corrupt newest bundle requires Build recovery.

The descriptor is bound to the independently expected plan, not proof of the payload's embedded
whole identity. The transport must still download/hash/read and compare that complete tuple and
native bytes/reports; the marker cannot establish tree/policy/plan/pin semantics. This route is
PR-only and does not dispatch or compile.

`wait_for_latest_pr_build` retains an independent validated plan and repeats that entire source/
newest selection while absent or pending. A monotonic deadline includes observation time within
the existing 5400-second admission budget. Fixed 60-second sleeps are clipped to remaining time;
an additional 91-observation cap prevents unlimited reads if an injected runtime clock/sleeper
does not advance. Invalid/backwards clocks reject. API errors, corrupt metadata and newest failed
producers propagate immediately, never becoming polling misses. No observation begins at/after
the deadline and even a successful observation finishing then rejects. An already in-flight API
read can overrun the deadline under the REST client's existing bounded timeout/retry policy;
this helper does not interrupt network calls or extend their bounds and never admits late data.
Timeout requires complete Build/E2E recovery for current source, not a PR compile fallback.

`revalidate_latest_pr_build` repeats complete selection and requires exact descriptor equality
immediately before and after independent download/byte/native verification. A newer pending,
failed or successful producer, changed attempt or immutable metadata cannot reuse the retained
selection. This inactive helper must be wired into materialization and final gate evaluation;
it establishes no payload proof or atomic guarantee against a future event after its return.
Production waiting/consumption integration, independent caller graph/pin/approval/status authority,
draft/readiness writer reconciliation and non-PR request/nonce selection remain unimplemented.

`download_latest_pr_build` now composes the wait and selection with actual numeric-ID transport.
Retain an independent plan, revalidate the selected descriptor before download, then enforce
existing immutable API/ZIP digest, canonical complete embedded tuple and payload inventory checks.
Copy independently into the existing private atomic stage. Repeat complete newest/source/metadata
admission inside that transaction, and reinspect the staged export after API reads before exclusive
publication. A newer producer arriving during fetch or copying prevents publication and the owned
temporary trees are cleaned. Existing descriptor-based complete/target routes retain their public
signatures and behavior. Only this PR composition installs a fixed protected final-admission hook;
candidate config cannot choose a callback. Return retained descriptor/envelope data, with no extra
document written or native success inferred. Caller must protect output ancestry and revalidate
again around later native/final gate consumption. The waiting budget does not substitute for the
separate bounded transport/native timeouts. Workflow/CLI integration and native verifier/receipt
sealing remain pending; this additive composition activates no profile.

## Build execution-to-receipt binding

The inactive root-only `freeze_frozen_build_validation` connects retained BuildValidationExecution
to the exact validated complete/target envelope and protected producer run/attempt. Require the
actual successful bounded WorkerResult and canonical input SHA from protected execution. Derive
verify_build/verify_target and its unit from the retained envelope instead of accepting a separate
free-form freeze context. Snapshot plan/envelope and inspect the fixed read-only input roots'
ownership, modes, access, canonical bytes and inode identities before and after the independent
validation output freeze. Missing/changed inputs or mismatched execution/run/digest reject.
Quiesce the admitted validator even on rejection. Existing runner-side execution and root-side
generic freeze signatures stay unchanged and preserve their role-specific host admission.

Protected code must preserve genuine source/execution evidence across that privilege transition;
serializing/reconstructing constructible dataclasses does not by itself authenticate a sender.
The synthetic required Linux fixture passes values retained from its actual protected dispatcher
to the trusted root test helper; that fixture is not a production IPC/CLI design and remains
unexecuted. No candidate may provide root helper code/context or obtain sudo. Real authenticated
privilege bridging, complete import/installer provenance and native domain schemas/semantics
remain required. A late failure can leave a private freeze copy; the failed operation returns no
receipt and cannot authorize its consumption/upload. Final API graph/source/approval/status
admission and workflows remain separate requirements.

## Local sealed-export archive encoding

The inactive `encode_build_export` produces a local `ci-export.zip` inside one private atomic
output directory from a canonical verified complete/target export. It streams sorted exact
payload/envelope files through MB1 no-follow single-link child reads, checking every streamed
size/hash. ZIP entries use stored compression, fixed timestamps and regular-file metadata;
the reviewed immutable Quick Skin snapshot's upload uses compression-level 0. Before each
physical write, enforce the existing 512 MiB compressed bound, including headers and central
directory. No whole expanded file or whole ZIP is allocated in memory. Independently extract
through the strict CI reader, reverify the decoded canonical envelope/payload, recheck the entire
original source and hash the bounded ZIP before exclusive publication. Input/output overlap,
source drift, output collision, unsafe source links and oversize encoding fail unpublished.

This local ZIP is a codec/preflight artifact, not a selected GitHub artifact or a native validator
receipt. Never pass it as a single file to upload-artifact: that adds a nested wrapper inconsistent
with the current root-envelope transport. A workflow must upload the admitted sealed tree with
the enrolled action/settings, or separately establish an approved direct ZIP-stream uploader;
neither is implemented here. Always authenticate the actual GitHub artifact size/digest/owner/
window after upload; local bytes/metadata do not predict the service's archive encoding. Actual
compressed admission, native receipt sealing and producer/gate success remain separate proofs.

## Complete Build byte assembly

`assemble_build_export` consumes the exact retained descriptor/envelope pairs from authenticated
target-set download and its fixed `target-0`, `target-1`, etc. input children. Protected callers
supply their own exact run/attempt and admitted plan, exclude all source writers and own private
input/output ancestors. Missing/extra/reordered/mixed target sets, substituted run context,
undeclared physical input children and overlapping input/output trees reject before publication.
The complete logical file/byte/entry limits apply across every partition, with a separate 4 MiB
cap for the assembled canonical envelope. Physical input wrappers keep their existing derived cap.

Actual target bytes must match the retained canonical envelopes before copying. MB1's new
`copy_selected_regular_files` shares the original streaming copy engine while appending only
declared payload paths with exclusive no-follow file creation. It inventories/rechecks the entire
bounded source, including its unselected envelope, and preserves the old `copy_regular_files`
empty-stage behavior and signature. Every file gets independent bytes/inodes; source files and
existing destination files are never linked, replaced or reowned. The protected assembler checks
each copied target inventory and the whole sorted union, writes one current-version complete
`ci-envelope.json`, independently verifies the staged export and rechecks all original inputs
before one exclusive atomic publication. Partial failures leave no published bundle.

This is inactive byte assembly, not native aggregate validity or successful Build authority.
Protected plan/policy/descriptor admission, original target/compiler/JDK witnesses, separate
credentialless aggregate verification and sealed validator receipts remain required before upload.
ZIP encoding/compressed-cap enforcement, full workflow/CLI wiring and the native aggregate hook
remain separate work. The Block Pops immutable snapshot's 512 MiB `e2e_fanin.py` budget applies
to its packaged fan-in; it is not substituted for Build's 2 GiB logical export bound. Existing
runtime/fan-in limits remain mandatory in the later packaged implementation. Required real Linux
download/assembly, inode independence, late mutation/cleanup, collision and link tests are added,
but none has executed on this Windows host.

## Private execution handoff v1

`mod-base.ci.execution` is a new local-only kind, not an artifact or a tested receipt. It has
exactly kind/schema_version, run_id/run_attempt, nonce, plan_sha256, source_config_sha256,
input_sha256, exact integer returncode 0, boolean truncated and log_base64. Base64 is canonical
and decodes to at most the original 16 MiB log cap; the local record cap is that maximum encoded
log plus 64 KiB metadata allowance. Artifact/validation-record limits remain unchanged.
The exhaustive compatibility ledger advertises current 1/previous null and requires v1.0.3
rejection; Pages schemas and adapter versions are unchanged.

`record_build_validation_execution` authenticates the protected runner host role, validates
retained source/plan/envelope/producer/execution context, and exclusively publishes the fixed
execution-handoff/ci-execution.json directory/file as runner-owned 0700/0600 bytes. It returns
a fresh random 256-bit nonce. An existing channel is never reused or replaced. The caller must
pass the genuine successful returned execution after UID quiescence; a matching object is not
execution provenance. Candidate/validator UIDs must remain excluded by the host/layout fence.

`freeze_handed_off_build_validation` authenticates the root role and admitted validator before
filesystem access, then requires the fixed private runner-owned directory and sole bounded
single-link regular leaf. It walks no-follow directories, reads through an independently checked
no-follow/nonblocking descriptor, rechecks named file/root identity, permissions and timestamps,
strictly decodes canonical JSON and binds nonce plus exact attempt/source-config/plan/input.
Only reconstructed data reaches existing input-bracketed independent receipt freeze. The record
cannot choose a program, path, hook or status. Admitted validator cleanup runs on errors too.

This establishes a data-channel contract, not a fully enrolled production privileged program.
The invoking root program, interpreter/import/installer closure, genuine protected runner source
and execution provenance, native closed-schema semantics and final API/status authority remain
separate mandatory requirements. Protected context is still supplied by the caller. The Linux
target/aggregate fixture now transports the actual returned result through this channel instead
of interpolating its log/result into fixture Python; its protected context/helper is a harness,
not a production CLI. No Linux outcome is established on this Windows host.

## Full graphs v1

Before deriving a plan or allocating a disposable worker, `read_pr_generation` reads an open
same-repository PR, binds its base/default to the caller's protected executing controller SHA,
and independently rechecks default/controller/tree plus PR number, source branches/SHAs,
draft state and nullable API merge SHA. The frozen observation retains scalar values without
aliasing the API response. Exact types, closed/fork/foreign/changed sources and API errors reject.
Drafts and ready PRs without an available synthetic merge may be observed without requesting
any candidate Git object. An API merge SHA is only a hint here; no ordered parents/tree or
protected policy are inferred. Ready execution still requires `authenticate_pr_identity`,
independent protected plan/pin/approval admission and later source rechecks. Future API events
are not atomic with this read. Deferred workflows/graphs, no-allocation enforcement and status
reconciliation remain required integration work; no draft observation can mint a tested record.
Existing `authenticate_pr_identity` also uses the immutable generation observation and rechecks
it plus default/controller after exact ordered merge parents/tree admission. A draft/head/base/
merge/controller change during the inert Git API read rejects; its public signature is unchanged.

`workflow.py` owns the exact display names. Build requires planning, protected policy, each
planned target, complete bundle sealing and the full Build gate. Packaged requires exact Build
input, each planned lane, result sealing and its full gate. `graph.jobs(plan)` derives the complete
expected graph before observing any jobs. `authenticate_graph` rejects missing/extra/duplicate,
skipped/failed/pending and wrong-run/attempt jobs, and requires successful unique seal/upload
steps with validation complete before upload begins. It returns the canonical graph digest.

This helper verifies the shared producer graph only. Caller-owned shell guards, identity anchors,
writers and advisory jobs require an additional closed caller graph before workflow admission.
Deferred, reuse, attest-only and transitional native graphs are not admitted by this helper.
Their explicit fixtures and hosted prefix/unexpanded-matrix behavior remain K1/K3/K6/K7 work.
No substring matching or observed-jobs-driven topology choice is allowed.

`authenticate_gate_timeline` additionally binds a full tested-record descriptor and receipt to
actual attempt-scoped API execution times. It requires the gate's successful unique native
validation and upload steps, validation finishing before upload, the selected record window
equal to that actual upload, and each step contained within its completed job. Every other
job in the full protected graph must finish before gate validation starts. Each selected bundle,
runtime lane and results aggregate must match its exact producing job's actual upload window;
a self-reported earlier source time cannot satisfy this check. Packaged's owning Build gets
an independently complete exact graph/digest, completed prerequisite jobs and sealed upload
steps before the packaged gate verifier starts. Equal timestamps are accepted at the boundary.

This is an inactive chronology primitive, not complete gate admission. It does not authenticate
run heads/events/protected pins, artifact API metadata/availability or downloaded bytes, native
reports, the caller graph or status authority. It does not prove the owning Build was admitted
before runtime execution; runtime input admission remains a separate required proof. It supports
completed full graphs only: same-run writers before workflow completion and historical reuse
need independently defined and authenticated routes. No new workflow has been activated.

`download_gate_receipt` connects that full-gate chronology to immutable numeric-ID record
transport. Protected callers supply the independently admitted plan, expected gate and exact
producer/owning-Build workflow paths, plus a private temporary parent inaccessible to workers.
Before downloading, the reader authenticates the live source, latest successful exact attempt,
protected API head/event/ref, executing kit, full graph, gate seal/upload and artifact API metadata.
ZIP length and digest must match the selected artifact. The existing hostile ZIP engine extracts
exactly one root file named `ci-gate.json`, with the existing 4 MiB record compressed/expanded
caps and compression-ratio policy. Strict kind/schema/JSON/canonical-byte validation and selected
record binding precede all receipt use. No caller-supplied archive filename or upload path is accepted.

Every referenced source artifact's immutable API metadata, owning run/head and availability are
checked. Packaged's owning Build additionally uses the separately enrolled Build workflow, latest
exact successful attempt, executing kit, full Build graph and aggregate upload binding. Source
and selected-record API admission are repeated before returning the retained document; temporary
extraction is cleaned on success or rejection. Corruption/API failure does not choose reuse or
another producer. Source ZIP payload bytes and native report semantics still need independent
download/domain verification. This inactive reader does not select the newest producer run,
authorize a caller graph/status, authenticate historical merged PRs or admit in-progress writers.
Real Linux extraction/cleanup cases are required CI tests and remain unexecuted on this Windows host.

## Compatibility

`tests/fixtures/documents/compatibility.json` is exhaustive over the registered kinds. The
predecessor is immutable v1.0.3 commit `f99ef433b66c729a869a2c1fbf038332c8b2f3e8`. Its Python
reader is archived as a digest-bound ZIP import source, with all old valid/config fixtures and
their hashes, under `tests/fixtures/previous_release/v1.0.3`. No network fetch occurs in tests.
All unchanged current writer fixtures pass it; all new CI kinds explicitly reject. Every old fixture
is checked by both readers. Adding a kind without a ledger decision or fixture fails coverage.

Strict unknown keys remain errors. A newly optional field cannot be emitted to an older strict
reader merely because it is optional. Either keep writes in the known common subset until reader
rollout, or make an explicit version/capability decision with unsupported-reader negatives.

## Inactive worker primitives

`materialize_build_export` independently verifies the canonical envelope and exact byte
inventory, copies every admitted regular file into new single-link inodes in an empty
caller-owned private stage, then repeats envelope/file verification before exclusive atomic
publication. It includes the envelope in copied hash coverage. `validate_tree_entries` applies
the native 20,000-entry whole-tree budget, including the root, before even envelope content
reads. Copying repeats this entry check before and after streaming; file/expanded-byte budgets
remain unchanged. Extra/changed bytes, links, hard links, special entries or an existing output
reject. Empty directories are omitted; no manifest file is omitted or aliased to source inodes.

This implements independent export materialization, not the complete ownership lifecycle.
Protected orchestration must terminate/lock the source UID, reclaim readable source ownership
without trusting candidate metadata and establish a private output parent inaccessible to both
disposable accounts. Native verification must still run under the second UID and seal its
receipt before upload. Four required Linux cases exercise independent inodes and mutation
isolation, hostile source entries, failed-stage nonpublication and entry caps before content;
they remain unexecuted on this Windows host. No CLI/workflow or consumer uses these copies yet.

`io.tree.grant_tree_read_access` implements the privileged permission transition for a fresh
independent private copy. It requires protected Linux root setup, an exact protected source
owner on every entry and a 0700 root. It applies all caller-supplied entry/file/byte caps before
mutation, removes access/default POSIX ACLs from every inode, assigns the authenticated runner
owner and validator read group, and verifies 0640 files/0750 directories through descriptors.
It compares the exact content inventory again and opens root traversal last. Any failure keeps
the root at 0700. This primitive never operates on the candidate-owned original and does not
authenticate the supplied runner/group identities itself: protected orchestration must do that,
protect all ancestors, exclude writers, and keep candidate UID/group distinct from the reader.
There is no production root dispatcher or complete lifecycle wiring yet. A required hosted Linux fixture
uses an inert runner-owned copy and actual candidate/validator UIDs to check validator-only read
access and denial of writes from both workers; its execution remains pending.
The fixture plants named-candidate access ACLs behind zero group masks plus a directory default
ACL, then requires both ACL removal and actual candidate access denial after the handoff.
Its byte layout and tags follow the Linux UAPI
[ACL xattr header](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/posix_acl_xattr.h)
and [ACL tags](https://github.com/torvalds/linux/blob/v6.8/include/uapi/linux/posix_acl.h).

`prepare_build_validation` binds that handoff to the fixed sealed-build copy under WORKER_ROOT.
Protected root setup rechecks the original private runner-home receipt against actual passwd
and inode/ownership/mode, authenticates both fixed worker accounts and refuses runner/peer
UID/GID collisions. Traversal parents must remain runner-owned 0711; the independent copy must
start runner-owned 0700. The helper terminates/locks the candidate, verifies the complete plan-
bound envelope and bytes, transfers only fixed-validator-group read access and rechecks the
copy inode, envelope and host fence. Accepted-copy failures restore private 0700 traversal;
invalid ancestors/foreign copies are rejected without changing their permissions. No caller-
selected path, owner or reader group reaches this operation. The separately invoked Linux
fixture now calls this composition and checks quiescence before probing the locked candidate
UID through protected root setpriv; a positive id check prevents PAM expiry from disguising
a non-executed negative access probe. Protected source immutability/reclamation and native
second-account execution/receipt sealing remain incomplete, with no workflow activation.

`build_ci.worker` starts the K2 port from Block Pops' protected `untrusted_runner.py` at
47a890ae46a2878fb08d29a932803ab91bccdcd9. It provides an explicit env-i vector, passwd/group/home
binding and real/effective UID termination with repeated SIGKILL sweeps followed by account
lock/expiry. Environment construction reads no ambient variables, excludes credential and
command-file names, binds the admitted tested identity and keeps candidate/validator homes,
temporary directories, Gradle caches and Python cache paths separate. Native protected
dispatchers map fixed MB_* names into their own domain inputs; native adapter parity is pending.
Only the validator receives a fixed Git safe.directory entry for the runner-owned sealed
repository. No passed value may override it or any other Git/Python/credential environment field.

Control commands have fixed argument lists, an explicit host environment, bounded streamed PID
output and timeouts. Timeout, surviving processes, changed account identity or failed lock all
reject. Termination grace is 15 seconds, with 0.25-second polls; the final locking command and
control-process reap each have their own 15-second bound. No arbitrary account names or ambient
sudo configuration are selected by plan/profile data.

`execute_worker` accepts only a bounded fixed Python `-I -B` dispatcher argv below the role's
scripts/ci root; protected code selects it, never a plan. It uses sudo/setpriv/no-new-privileges
and env-i, merging stdout/stderr into a 16 MiB capture. It continues draining after truncation.
Python bytecode is explicitly disabled with -B; isolated mode ignores bytecode environment settings.
Workload timeout is explicit (at most six hours), including launch and capture. A late observed
exit cannot reset an expired deadline. Once the launcher exits, the UID is terminated immediately
so orphaned descendants retaining stdout cannot keep the pipe alive until the workload timeout;
post-termination drainage has a separate 15-second deadline. Failure, launch/read errors,
timeout and SIGINT/SIGTERM all terminate/lock the UID and reap the launcher. Repeated cancellation
signals are ignored during final cleanup and prior handlers are restored afterward. Only zero
exit plus successful quiescence returns a `WorkerResult`; execution errors carry bounded raw
diagnostics on `WorkerExecutionError`, and failed cleanup remains fatal.

Account termination repeats real/effective UID kill sweeps until quiescent, always locks/expires
the identity, then repeats the sweeps and observations after locking. A process appearing during
the lock interval must be killed before return. The post-lock phase retains the original sweep
deadline; locking does not reset the termination budget. Failed observation or any surviving
process forbids success/sealing. The Linux child-process case records actual commands and
requires both final observations after lock/expiry; its hosted execution remains pending.

Raw logs must never be printed. `render_worker_log` strips C0/C1/DEL terminal controls, prefixes
every line and neutralizes both modern `::` and legacy `##[` Actions-command markers. Whole
colon runs are spaced so escaping cannot recreate a marker. Raw capture bytes remain unchanged
for native diagnostic hashes. GitHub's [runner parser](https://github.com/actions/runner/blob/main/src/Runner.Common/ActionCommand.cs)
recognizes legacy commands after arbitrary prefix text; a prefix alone is insufficient.
Structural error messages quoting hostile values are kept out of the execution error line.

`build_ci.source` parses bounded NUL-delimited full-tree Git inventories from protected objects.
It retains blob identity, size and modes 100644/100755/120000; submodules, aliases, traversal,
duplicate paths and oversized inventories reject. The read-only post-quiescence inspector uses
directory descriptors, hashes literal tracked link bytes without following them, preserves empty
files and compares every tracked mode/size/blob. All tracked leaves are inspected even within
protected generated roots. Undeclared paths outside those roots, hard links and source changes
reject. The root .git entry is opaque and must be replaced with protected metadata before Git
can ever run on the copy. Protected listing provenance and generated roots cannot be supplied
by candidate self-report. Source bounds preserve the native 200,000 files/250,000 entries,
2 GiB per file and 20 GiB per tree ceiling; inventory listing is capped at 64 MiB, links at 4 KiB.

`authenticate_source_inventory` obtains the immutable tested tree through the existing bounded
GitHub tree transport and authenticates the complete live PR identity before and after that read.
It requires exact blob sizes and the complete inferred directory closure; extra/empty directory
objects, submodules, malformed entries and truncation reject. It applies all source inventory
budgets and the transport's independent 100,000-entry cap; no existing bound is enlarged.
The returned inventory still needs byte/blob/mode comparison during materialization, and does
not authorize controller policy, pin staging or execution. It never runs Git in a candidate
copy or imports candidate modules. Protected non-PR source admission remains unfinished.

`build_ci.controller.authenticate_controller_sources` separately reads the protected controller
commit's tree and immutable blobs, rather than resolving candidate paths or following Contents
API links. Native authenticated policy supplies a canonical sorted approved-path tuple including
`scripts/ci/mod-base-build.json`; every configured adapter source must be approved, regular Git
mode 100644/100755 and match its configured SHA-256. Live PR checks bracket the whole read.
Code-source admission adds a 4 MiB per-file and 64 MiB whole-closure cap; the existing 1 MiB
config and GitHub response/entry caps also apply. Code blobs are not fetched until sizes fit.

The frozen `ControllerSources` receipt is protected in-memory evidence, never candidate authority.
`verify_controller_source_copy` rechecks its config, Git blob IDs, hashes, exact closure and local
bytes/modes, refusing undeclared files and `.git`. Neither function imports or executes code.
Native protected-path authorization, complete Python/system import closure, protected ancestors,
excluded writers, installer provenance and second-UID dispatcher integration remain required.

`materialize_controller_sources` validates that retained protected receipt before creating an
output. It writes only its config and declared source bytes through exclusive no-follow
descriptor-relative files into an empty caller-owned 0700 stage, preserving regular Git modes.
Writes and directory entries are fsynced, then the independent copy inspector rechecks the full
closure before atomic exclusive publication. Existing outputs are never replaced; failed writers
leave no published copy or stage. It does not reuse candidate inodes, run Git, import code or
grant validator access. Protected destination ancestors, authentic receipt retention, complete
import closure and ownership/read-access composition remain required before execution.

`prepare_controller_validation` admits only the fixed `WORKER_ROOT/controller` copy matching
the validator dispatch root. Protected root setup rechecks the runner home/passwd fence,
actual fixed validator/candidate identities and disjoint UID/GID ownership, the runner-owned
0711 traversal layout and the private runner-owned 0700 copy. It terminates the candidate,
verifies the original bytes/Git modes, then uses `grant_source_read_access` to remove ACLs,
assign validator-group read access and expose the 0750 root last. Source files become 0640;
their executable bits are intentionally stripped because the fixed Python interpreter reads
them. The final check requires normalized regular Git modes, unchanged blob/SHA-256 hashes,
the same copy inode/owner/group and the host fence again. Empty regular files remain supported.
Failures after copy admission restore 0700 traversal; foreign copies are never chmodded.
The generic source permission helper rejects undeclared entries, Git metadata, links and
special files. Existing export permission transfer continues to reject empty artifact files.
Complete import enrollment/provenance, protected root-dispatch integration, second-account
native hook execution and receipt sealing remain incomplete and required before activation.

`execute_controller_validator` executes only `verify_target`, `verify_build` or `verify_runtime`.
It requires the fixed actual validator identity, a valid protected plan and matching source
profile. Target/lane hooks require an exact enrolled unit; aggregate Build rejects a unit.
The fixed argv is `(python, -I, -B, protected_dispatcher, --hook, hook)` and the timeout comes
from protected `validator_seconds`. Only the applicable fixed MB_TARGET_ID/MB_LANE_ID is added
to the existing closed env-i environment. No command string or arbitrary environment is accepted.

Before and after execution the runner rechecks the host/traversal fence, exact read-only source
metadata and original source byte/blob hashes. `authenticate_tree_read_access` performs a bounded
no-follow metadata walk requiring 0750 directories, 0640 single-link regular files, expected
owner/read group and absent access/default ACLs; hashes alone cannot detect writable code.
The existing tool fence reauthenticates selected tool trees and Python/JDK path binding before
launch. The wrapper always performs a final validator kill/lock/quiescence sweep, including
pre-dispatch failure and post-dispatch source drift. Forged/non-validator inputs never launch.

This remains inactive library composition. Installer provenance, complete interpreter/system
import enrollment, frozen native inputs, native hook conformance and protected root-dispatch
integration are caller prerequisites not established by this function. A zero exit/bounded log
is execution evidence only; native receipt validation, freezing, upload and status evaluation
remain separate required phases. The Linux fixture uses a benign protected synthetic dispatcher,
not a claim that either consumer's compiler/runtime assertions passed.

`materialize_source_copy` stages a new 0700 tracked-source copy with descriptor-relative parent
creation and exclusive no-follow file creation. Source bytes are streamed within the existing
budgets; literal links and executable bits are retained. Original source, copied records and
the independently re-inspected stage must agree before atomic exclusive publication. Existing
outputs are never replaced and a failed stage cannot become a published copy. Only tracked
leaves are copied; root .git is omitted. The source must be a clean protected checkout without
concurrent writers. Overlays, Git metadata and caches require separate protected staging.

`allocate_worker_account` creates each fixed role only after both dedicated traversal directories
are verified as runner-owned 0711 directories. An existing account/home rejects; each home/tmp/
Gradle directory starts as 0700. Allocation uses fixed useradd arguments, authenticates the fresh
UID/GID and exact primary group, rejects a successful sudo policy listing, and changes ownership
of only the three fresh directories without recursive chown. Identity/inode/mode checks bracket
ownership changes. Failure stops admission and locks/terminates an authenticated new identity;
an unexpected privileged identity is locked by its newly allocated name without a UID kill.
This does not protect the host runner/workspace or stage source/overlays/caches. Those are still
mandatory before execute_worker may be called.

`prepare_worker_boundary` creates those dedicated directories exclusively under a no-follow /tmp
descriptor, binds their runner UID/GID and inode identities and establishes 0711 traversal modes.
It requires Linux plus the protected caller's authenticated github-hosted runner classification.
Preexisting identities or paths reject; an interrupted/failed allocation cannot be silently reused.
The rest of the host is not protected by this root-creation primitive.

`build_ci.host` adds the initial hosted-Linux filesystem fence. The protected caller supplies
the runner classification and canonical layout; only /home/runner is supported, with workspace
and runner-temp directories strictly below it. Descriptor walks refuse links/identity changes,
bind the passwd home and runner ownership, then close home traversal to mode 0700. A frozen
host receipt records its device/inode/ownership. Failed fencing keeps the home private.
`execute_isolated_worker` rechecks that receipt before either UID dispatch; failure terminates/
locks the account without launching. Dispatch uses a private cwd and explicit close_fds=True.
The fence hides host workspace/action/temp trees under that home; it does not prove facts about
outside-layout files, immutable toolchains, kernel vulnerabilities or authenticated copies.
The launcher starts in the runner-owned traversal root, then GNU env --chdir switches to the
private role directory after sudo has dropped UID. This supports a candidate-owned 0700 source
root without weakening its permissions or retaining an inherited protected host cwd.
Only the dedicated Linux
fixture restores its prior home mode after every allocated identity is deleted and every probe
is reaped. Production never relaxes the fence while disposable identities/processes remain.

The home fence says nothing about the image. Measured on a hosted `ubuntu-24.04` runner, `/opt`
with all of `/opt/hostedtoolcache`, `/usr/share`, `/usr/local` (including `/usr/local/bin`, the
head of sudo's PATH) and `/usr/lib/jvm` are mode 0777, the `/opt` trees carry default ACLs that
give `other::rwx` to every new entry, and ten world-writable regular files sit under
`/var/lib/gems`. Any local account could replace the interpreter, a JDK or a command that root
runs. The root operation `host-fence` (`host.fence_worker_host`) closes this before any worker
account exists and refuses to run once either fixed account is present. One `find -xdev` walk
over the members of `HOST_FENCE_TREES` the host has (`/opt`, `/usr/share`, `/usr/local`,
`/usr/lib/jvm`, `/var/lib/gems`) runs `chmod go-w` on every directory and regular file that has
a group or other write bit and `setfacl -k` on every directory. Links are neither followed nor
changed, and a second run changes nothing. A second walk over the root filesystem then lists
every world-writable directory without the sticky bit and every world-writable regular file. It
does not enter the worker boundary, nor a directory that only its owner can search when that
owner is an existing account, because an account created later can never be that owner.
Anything listed fails the operation with the count and the first path. So do a listing larger
than 64 KiB, a tree that is not a real directory and a command that fails (reported with the
start of what it wrote to stderr) or runs longer than 600 seconds. Sticky directories outside the fenced
trees, such as `/tmp` and `/var/tmp`, stay world-writable; a sticky directory inside a fenced
tree loses its write bits like any other entry. Other filesystems, special files and
group-writable entries outside the fenced trees are not examined: a worker account has a fresh
primary group and no other. On the measured runner `chmod -R go-w` over those trees took about
7.5 seconds. The required Linux fixture runs the real operation over a runner-owned stand-in
below `/opt` with the measured modes and default ACLs, and requires a stray world-writable entry
elsewhere and a default ACL that cannot be removed to fail it.

Required Linux probes use deliberately inert marker files and a separately launched benign
runner-UID process. They check host directory/file denial, /proc environment/memory and ptrace
denial, and closing even an explicitly inheritable host descriptor. Fixture UID probes use a
private cwd so they cannot bypass home traversal through an inherited protected working directory.
These tests have not run on the Windows development host.

`build_ci.toolchain` inspects the complete permission/identity closure of protected-selected
Python/JDK roots after the host fence. A root may live anywhere and no prefix is special: what
admits a root is that it, every entry below it, every ancestor and every link target with its
own ancestors pass the same rules. Every ancestor and link target is checked through no-follow
directory descriptors. Only root/runner-owned entries are admitted; group/other writable
files/directories, special entries and directories that carry a default ACL reject.
Global source file/entry/byte/metadata caps apply, alongside 16 roots, 40 link hops and depth 64.
Directory identities bound alias cycles. Mutable installations fail closed; this primitive
does not chmod system tools or install dependencies. The host fence must have run first.

The immutable ToolTreeProof fingerprints metadata, permissions and identities, not file contents
or installer provenance. `execute_tool_fenced_worker` reinspects that closure, binds the selected
Python/JDK paths and their resolved destinations to explicitly admitted roots, then rechecks
the host fence before dispatch. A link target outside the enrolled roots does not enroll its
surrounding interpreter/import tree. Internal aliases and aliases between explicitly enrolled
roots remain supported, including an enrolled root that resolves to another installation path.
Python must resolve to a nonempty regular file executable by the fresh worker UID; JAVA_HOME
must resolve to a worker-traversable directory. Resolution I/O failures reject before launch.
Failed
admission terminates/locks the account without launch. Complete import-root enrollment, trusted
installer hashes and native compiler observations remain separate protected requirements.
The Linux fixture now admits the real complete Python prefix before a fenced synthetic dispatch;
that case remains unexecuted and its hosted cost has not been measured.

The inactive protected-root `freeze_build_export` composes candidate termination/locking,
fixed account/layout admission, and tracked-source verification both before and after an
independent export copy. Original exports require exact candidate-owned private metadata and
absent ACLs. Only the newly created root-owned copy transfers to runner ownership, with
0700 directories and 0600 files, followed by identity, metadata and content rechecks. Original
candidate files keep their ownership. Successful execution objects and source inventories must
be retained from protected admission/execution; matching constructed objects prove no provenance.
Generated roots come from protected native policy and never exempt tracked leaves.

Required hosted fixtures now exercise a synthetic candidate export through this composition,
check independent inodes and denial of both worker UIDs, and reject a zero-exit dispatcher that
changes its tracked source. They are unexecuted on this Windows host. Native compiler/report
semantics, complete import enrollment and production workflow integration remain pending.

Overlay/Git/cache staging, protected inventory transport and complete native
freeze/second-account verifier integration are not yet implemented. Passwd/group
checks and env-i alone do not prove no-sudo, ptrace/process or filesystem isolation. These
primitives have no CLI/workflow execution entry point and enable no consumer.

Each required Python CI matrix leg explicitly runs `tests.ci_linux_worker` after the complete
suite. That fixture requires Linux, sudo and a fresh GitHub-hosted runner; missing prerequisites
fail rather than skip. Worker cases create two fresh dedicated accounts, check private-file/home access and
no-sudo execution, starts a real shell with two child processes, then checks UID quiescence and
shadow lock/expiry after termination. Cleanup verifies original UID/GID before removing its own
accounts and boundary. Additional real dispatcher cases cover success with an orphan retaining
stdout, nonzero exit, timeout, SIGTERM cancellation, second-account controller execution and
capture overflow/draining. It refuses pre-existing accounts/directories. Ordinary local discovery
does not allocate OS accounts. GitHub defines the fixture's hosted-runner guard variables in its
[variables reference](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
No run of this fixture has yet passed on this Windows development host or been submitted to CI;
it is a required future result, not completed boundary evidence.

## Candidate checkout staging

The root operation `stage-candidate` gives the fresh candidate account everything it may see,
before it has ever run: its own copy of the tested tree at `repository/`, a curated
`repository/.git`, the kit overlay at `repository/out/mod-base-kit` and, when the runner restored
one, a Gradle cache seed in `candidate-home/gradle-home`. The runner publishes the request with
`root_request.request_candidate_staging` after both accounts were allocated; root runs
`worker_preparation.prepare_privileged_worker_checkout` from it and calls no API. Authenticating
the tested commit and tree, reading the tree's inventory and establishing that the overlay's pin
is a released kit commit are the runner's work before the request exists.

The three directories a request names (`source`, `overlay.path`, `gradle_seed`) are read, never
written. Each must be a canonical path strictly below `/home/runner`, owned by root or the runner
and closed to group and others, and none may contain another. Before the first effect root
requires:

- no `repository/` below the worker root and an empty allocated Gradle home: a second staging,
  or one for a candidate that already ran, never reuses a populated root;
- the inventory to hash to `tested_tree`. The Git tree name is recomputed from the rows (a tree
  lists blobs, links and subtrees by name and object name, a directory sorting as `name/`), so a
  request cannot pair a tree with another tree's inventory, and root needs no object store;
- no tracked path at `out`, at or below `out/mod-base-kit`, or differing from either only in case;
- the checkout to hold exactly the inventory: every tracked byte and Git mode, tracked links as
  literal bytes, and no undeclared path, hard link, special file or linked directory;
- the checkout's `.git/HEAD` to be detached at exactly `tested_sha`, and its metadata to fit the
  profile below;
- the overlay to be a closed stamped kit that matches its pin, digest and staged-file locks.

Anything else is refused with nothing published. The phases then run in a fixed order. Each
publishes exclusively (an existing destination is never adopted or replaced) and re-checks the
held and named originals, every published root, the host fence, the passwd identity of the
candidate and that no process runs under its uid. At the end the complete tracked source is
verified again with only the overlay as generated data, together with the Git and cache bytes,
their private ownership and the unchanged originals. Any failure terminates and locks the
candidate, and later phases do not run; after a failure in a later phase inert output of earlier
ones can remain. The returned inventories confer no execution authority.

**Source.** Tracked files are copied into a root-private stage, published at `repository/` and
then handed to the candidate with ACLs removed: directories and executable files 0700, other files
0600, the root last. Links keep their literal bytes and change owner without being followed; no
link target is read, chmodded or chowned, and a target outside the copy gains nothing: the runner
home stays closed to the candidate. The original `.git` is not copied here.

**Git metadata.** Root never runs Git and never parses object or index bytes. From the original
self-contained SHA-1 files-backend `.git` it selects the index, loose objects, conventional
pack/idx/rev/bitmap/keep/mtimes files, heads/tags/remotes/pull refs, packed refs and an optional
shallow boundary. The original HEAD, config, hooks, logs, description, FETCH_HEAD/ORIG_HEAD, info
and branches are never copied, so no credential, include, filter or hook is inherited. The whole
bounded closure is inspected for type, owner, mode and case aliases without reading unselected
contents; links, hard links, special entries, alternates, grafts, replacement refs, worktree,
commondir, reftable, split-index, partial-clone, submodule and LFS stores are refused. Loose and
packed refs and the shallow list have bounded visible-ASCII shape and duplicate checks (4 KiB per
line, LF counted before lines are split). The copy gets a detached HEAD at the tested commit and a
fixed non-bare configuration: file mode and symlink tracking, no autocrlf, hooks and fsmonitor
disabled, and a credential-free `origin` for the repository. It is published at `repository/.git`
as the candidate's private 0700/0600 data. The index is the original's: the candidate's first
`git status` refreshes it against its own files.

**Gradle seed.** Optional. A seed root holds only the conventional `caches` and `wrapper`
directories; Gradle properties, init scripts and every other root entry are refused by name.
Below them only structure is checked (no link, no special or hard-linked file, the original Block
Pops caps of 250000 entries, 200000 files, 2 GiB per file and 20 GiB in all), because a cache
names its entries freely. The allocated cache must be empty; it is held, made root-private for the
copy and handed back as the candidate's 0700/0600 data. The seed is admitted when this phase
starts, so a refused seed leaves the source and Git copies behind for a candidate that is locked.
Names, sizes and hashes cannot prove that arbitrary cache bytes hold no secret: a protected
restore policy must supply secret-free data and exclude concurrent writers.

**Kit overlay.** The overlay is a kit as a mod's bootstrap stages it: `src/`, `site/` and
`requirements/`, optionally `template/`, `tools/` and `actions/`, and `MOD_BASE_KIT.json`. No
bytecode, `.pth`, executable, link or name outside the kit path grammar is admitted; the stamp
must equal the pin and digest of the request, kit-digest-v1 must equal that digest and the staged
directories must match the locks inside it. `out` is created when the tested tree tracks nothing
in it and is the candidate's own 0700 directory either way, so a build writes beside the overlay.
The copy is published at `out/mod-base-kit` with plain 0644 files and 0755 directories, the modes
a bootstrap expects. No copied code is imported by root, and a pin other than the executing kit's
gains no protected-code, native or App authority by being copied.

`tests/ci_linux_worker.py` (`LinuxCandidateStagingTests`) runs the operation through the installed
bootstrap against really allocated accounts: a real shallow detached Git checkout is staged; the
candidate reads it byte for byte, builds, writes generated roots and beside the overlay, and
reaches neither the original nor what a tracked link names; a linked, hard-linked, undeclared or
changed file, a moved HEAD, another tree's inventory, another overlay digest and overlapping roots
are refused with nothing published; a second staging refuses the populated root, as does a first
one for a candidate whose cache already holds bytes.

### Closed caller rendering foundation

MB9 template.tool now selects caller rendering through a protected-code fixed
(destination, source, renderer) tuple. Pages is the only enrolled caller and retains its existing
managed/extension behavior. A manifest cannot remap an enrolled path/source/class or select a
renderer; case aliases reject. Any unregistered workflow source with PIN/VERSION placeholders
rejects before check/sync/init writes, including fragment/seeded entries. Ordinary token-free
workflow entries retain their manifest behavior. This neither enrolls a privileged Build/E2E
caller nor activates a consumer. Profiles, their exact transition/manifest contracts, reviewed
rollback, actual Build/E2E templates and bump/bootstrap parity remain required K4 work.

### Inactive profile activation manifest

mod-base.ci.activation v1 at the fixed prospective site/mod-base-build-activation.json path
contains only kind/schema_version/repository/native profile/mode. The two native profiles and
five modes are closed; its generic strict reader has an 8 KiB central cap. Initial v1 explicitly
has no predecessor; the exhaustive compatibility ledger proves v1.0.3 reader rejection. No
Pages shape changes or duplicate pin/scenario inventory are introduced. Arbitrary execution,
template/job/permission/secret, approval, extension/deferral and path selectors reject.

Parsed activation state is not a protected transition or authority grant. Repository/profile
binding to original protected configuration, current-head native upgrade/owner prerequisites,
fixed caller bytes, reviewed rollback and profile-aware template/pin/bootstrap integration are
still mandatory incomplete work. Existing legacy consumers neither require nor adopt the new
manifest merely because this validator exists. No mode chooses arbitrary jobs or templates.

### Original controller binding for activation

controller.authenticate_controller_activation derives genuine config/import evidence through
existing API source admission, under a bounded canonical native-policy file inventory including
the fixed activation/config paths. Read activation only from that retained original controller
commit/tree; require regular non-executable bounded bytes, real site ancestor, no case aliases,
exact tree/blob length and rehashed blob identity. Strict activation repository/profile must
match the original config. Reauthenticate original sources, reread activation and recheck live
source before returning frozen sources/manifest observations. Candidate manifest bytes are not
consulted. No constructor or mode label establishes owner approval, protected transition,
native predecessor, rendered caller bytes, physical import enrollment or execution/status
permission. Those admissions and template/pin/bootstrap integration remain mandatory work.

### Closed batch manifest foundation

The initial mod-base.ci.batch v1 data binds repository/profile, protected base SHA/tree,
batch branch, native policy digest, ordered member source/head/tree/draft and merge-base
identities, complete-source byte fingerprints, before/after patch identities and per-member
parent/squash/result identities. Single ordered parent linkage and final result equality are
checked structurally, alongside the original 50-member and combined inventory caps.
The generic reader strictly rejects duplicate/unknown keys, unsupported versions and oversized
JSON. New-kind ledger coverage proves v1.0.3 rejection; existing kind versions are unchanged.

The byte fingerprints are the mod-base.batch-source-bytes-v1 stream already produced by
verify_batch_patch_bytes: canonical {format, tree_sha}, then fixed canonical
{path, mode, size, git_blob, sha256} rows for the complete source in path order. A constructed
manifest can assert arbitrary valid fingerprints; acceptance never proves those bytes.
Protected original caller/native ordinary-path approval, actual API/source-byte admission,
safe local graph and patch application, real single-parent squash commits, result-tree
verification, live membership/empty branch leases and exact merged-gate settlement remain
required before any effects. The automation writer and consumer integration remain open.

### Batch manifest source and graph observation binding

verify_batch_manifest_sources takes independently admitted protected native profile/policy,
exact ordinary permitted paths and original private writer-excluded source root pairs in member
order. It snapshots the strictly bounded closed manifest, binds its repository/base to the
executing controller, and repeatedly authenticates the complete live ordered member collection.
Every source branch/head/tree/draft and protected base ref/tree must match. Actual complete
source verification derives merge-base/head byte fingerprints and patch identities; those must
match every declared field. Constructing a BatchPatchBytes receipt cannot bypass this path.

Actual API squash objects must name the exact commit, tree and one declared predecessor parent.
Complete result inventories use exact-tree admission and are streamed into a domain-separated
comparison fingerprint; no accumulation of all members' full result inventories is needed.
The whole source/graph pass runs twice, with complete live membership rechecked after each
result-graph read. Caller document mutation rejects; repeat closed-shape/cap validation before
re-encoding that caller document so a malformed oversized mutation cannot reach the encoder.
BatchManifestSources retains only the
canonical manifest SHA-256 and ordered byte/source observations; it grants no native/writer
approval. All reads must retain genuine API and original root provenance independently.

API-observed result identities do not prove local three-way patch application or fixed-bot
commit-tree construction. Those checks, independently enrolled original Git program/runtime,
empty branch leases, live writer rechecks, immutable merged full-gate provenance and explicit
settlement remain required. No Git command, code import or account/ref/API mutation occurs.
Source root lifecycle/ownership and exclusion of writers are prerequisites, not inferred from
root names or repeated reads; physical Linux and hosted canary proof remain unverified.

### Empty branch publication observations

observe_batch_branch_lease validates the publication-specific batch Git branch grammar and
bounded ordered member tuple before API IO. It authenticates the entire current member/controller
collection, reads the exact git/ref/heads/<batch-branch> endpoint, repeats complete membership,
reads absence again and closes with another full member/controller admission. A source change
inside the final absence read therefore cannot return its earlier observations. Only ApiNotFound with status 404, GET and the exact requested path counts
as absence. A missing controller/member, other API error or existing/malformed 200 response
cannot become an empty branch. See [GitHub Get a reference](https://docs.github.com/en/rest/git/refs#get-a-reference).
The returned BatchBranchLease is only retained current-state observations. A fresh recheck uses
the original protected caller's independent repository/branch/controller/member order and
rejects substitutions or drift. Constructors do not supply provenance or native approval.

These API reads never reserve a name. The final independently admitted protected writer must
use empty_batch_branch_lease's explicit --force-with-lease=refs/heads/<branch>: argument; the
empty expectation requires nonexistence at the actual Git ref update, including an intervening
actor's creation. See [Git push](https://git-scm.com/docs/git-push). Partial leases based on a
remote-tracking ref or nonempty old SHA are not substitutes for the original empty-name rule.
An important actual Git observation: if the name already points at the identical SHA, a
--porcelain push returns exit 0 and an up-to-date (=) record even with empty expectation because
there is no update. That is not creation by this writer. validate_batch_push_receipt therefore
requires integer exit 0, bounded 4 KiB ASCII stdout, the exact original destination To header,
one * source-SHA:refs/heads/<branch> [new branch] record and Done trailer, with complete LF/CRLF
framing. No-op/extra/unknown/malformed records reject. The actual independently enrolled Git
command must use sanitized C locale; constructor-authored receipt bytes/exit codes are not
programme, destination, execution or writer provenance. Fresh created-ref API and native/live
writer rechecks remain mandatory, as do safe actual construction and publication integration.
Native ordinary policy, safe original Git/runtime, fixed bot commit-tree construction, complete
source and result/patch proof, live writer rechecks and immutable full-gate settlement remain
mandatory. The library neither pushes nor changes refs/accounts/API. A known local Git fixture
checks this argument's actual creation/refusal behavior as the local test user; it proves no
hosted transport, privileged writer, native Linux account boundary or production integration.

### Published batch ref/source observation binding

authenticate_batch_publication performs shared bounded manifest/native/root argument preflight
before any API read. Branch and final squash SHA must match independent original caller values,
not merely fields chosen by the manifest. The exact git/ref/heads/<branch> response must name
that full ref and a commit object with that SHA; tags, partial aliases, malformed or absent refs
and permission/network/rate errors fail closed. An independent commit read must name the final
manifest result tree. API failures never become publication or select another branch.

Between ref reads the existing complete manifest source verifier performs repeated live source,
merge-base/patch/full-byte and actual squash single-parent/result-inventory admission. Closing
complete member/controller admission after the final ref read rejects source/readiness drift
inside that read. Shared preflight leaves verify_batch_manifest_sources' frozen API unchanged.
The original caller document is shape/cap revalidated before comparison against its snapshot;
mutation cannot select another reference or unbounded encoding. BatchPublication retains the
source digest/observations and current branch/commit/tree only; it conveys no approval.

This is current API state, not proof of who created the name. Original safe Git/runtime/native
policy, private writer-excluded roots, local application/result construction, empty-before
lease and completed protected new-branch porcelain receipt remain independent prerequisites.
Actual protected writer integration and repeated live/native/owner checks around effects,
PR creation/gate provenance and immutable settlement remain required. No API/ref/account/PR
mutation, candidate/Git execution, status authority or atomic future guarantee is added.

### Ready batch PR and synthetic merge observation binding

authenticate_batch_pr takes a closed identity from the independently admitted original protected
caller/native plan. Shared bounded manifest/native/root preflight and identity validation happen
before API IO. The subject is a distinct positive batch PR, never an original source member.
Source repository, protected controller/base/ref, branch/head commit, policy and tested tree
must match the manifest and independent caller. Snapshot both inputs rather than allowing
callback-driven substitutions. The batch's synthetic tested tree equals the combined result
tree under that exact protected base; its ordered parents remain base/head, not squash parents.

Existing authenticate_pr_identity proves the live same-repository open ready PR, current exact
synthetic merge SHA, complete tree and ordered two parents around complete publication/ref and
member source/byte/squash graph admission. Close source membership and batch generation after
those reads. Current draft/closed/fork/missing or moved merge/base/head states and final caller
input mutation reject. Original source-member drafts are observations; they never convert a
draft batch PR into a ready producer or permit revival of deferred evidence.

BatchPr retains publication/source and current batch generation plus the canonical identity
digest. Identity's kit/workflow/inventory/scenario/graph values are structurally checked and
retained; their actual original caller/pin/native-plan derivation needs independent admission.
These checks grant no actual construction, source-root lifecycle, writer/owner approval, full
Build/runtime gate success, PR creation, merge, member closing, status or immutable settlement.
Actual protected Git/program/runtime, empty-before/new-branch publication provenance, native
policy, complete graph/seals/artifacts/current authority and post-merge proof remain required.
Repeat admission around future effects; API observations are not an atomic lease.

### Historical merged PR identity observations

authenticate_merged_pr_identity admits an original protected PR identity separately from the
independently expected current controller and final merged SHA. It requires the exact closed,
merged, ready same-repository PR, original head/branch and protected base branch, with a real
bounded UTC merged_at. Its API base SHA is validated and retained across reads but is not treated
as the original tested parent: post-merge base observations can advance. The original identity
still requires controller equal to base and exact synthetic base/head parents.

Read the original synthetic Git commit by its immutable ID, enforcing the original complete
tested tree and ordered parents. Read the independently expected final commit separately;
merge/squash/rebase may retain different one/two-parent identities, but its complete tree must
equal the originally tested tree. Duplicate/self/malformed parents and mismatched objects reject.
Both original-base-to-final and final-to-current-default ancestry must hold. Repeat PR/Git/history
reads, close protected default/controller and PR state, and revalidate the original caller input
before comparing its canonical snapshot. API failures never select a success/full-run fallback.

GitHub documents that merge_commit_sha changes after merge to the final merge/squash/rebase
commit, rather than continuing to identify the synthetic test merge:
https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request.
This source/API observation does not authenticate either gate, native outputs, protected policy
equivalence, original/current pin, latest attempts, artifact bytes, source seals, selection or
owner/writer authority. MergedPr retains the original identity digest, final Git identities,
timestamp and current controller only. Existing live authentication remains unchanged; no PR,
status, reuse or settlement operation is activated. Historical gate/manifest membership readers
and their full native/Linux/hosted conformance remain required before reuse or member closure.

### Original full gate transport after merge

download_merged_gate_receipt reads one original full PR tested seal using independently admitted
original plan/workflows and expected current controller/final merged SHA. Bounded plan/descriptor
snapshots reject caller substitution; shared full-record preflight rejects wrong kind/unit,
workflow enrollment and plan binding before API IO. No data selects arbitrary source admission.
An internal fixed historical admission closure retains the first MergedPr and compares every
later observation, including around packaged's independently authenticated owning Build.

Share the existing exact completed run/latest-attempt, original API controller head, executing
kit, full graph, seal/upload and numeric artifact metadata checks. The current controller never
replaces the original producer's controller SHA. Download only the selected tested ZIP under
its exact size/digest, extract the one fixed canonical gate JSON record under unchanged limits,
bind actual source/gate timeline, require all source artifact metadata/availability and owning
Build workflow/complete graph/latest attempt, then repeat record/input/source admission. Temporary
extraction is cleaned on success and rejection. The live public reader retains its signature
and open/ready requirement; the historical reader does not admit deferred/reuse/partial graphs.

This reads a seal and source metadata, not all native Build/runtime payloads. Both coherent
original gates, independently selected newest eligible runs, original/current native policy
and executing pin equivalence, complete source bytes/native validation, actual later protected
consumer chronology and caller/writer/owner authority remain required. No API error, corrupt
ZIP/JSON/hash/graph or changed observation becomes an optimization miss or success. Availability
recovery and actual reuse/settlement integration remain separate; no status or PR effect occurs.

### Coherent original Build and packaged seal pair

download_merged_gate_pair preflights the independently admitted original plan, two exact tested
gate/workflow descriptors and current controller/final merged SHA before API IO. Original Build
and packaged seals require independent run/artifact IDs and enrolled workflows. Snapshot all
three bounded caller objects and retain the first actual historical MergedPr across the whole
operation. The existing actual historical reader independently admits each complete seal.

Require the packaged record's entire immutable owning Build descriptor to equal the Build
record's actual sole complete bundle, including producer/run/attempt, upload window and numeric
artifact metadata. Identical PR/plan/tree values alone do not bind consumed Build generation.
Two individually valid seals that consumed different Build generations reject. Read both
seals again through full historical transport and compare bounded canonical record fingerprints,
closing historical source and caller snapshots. Four record ZIP downloads remain under their
original individual size/hash/record caps; native source bundles are not downloaded by this
pair reader. Each private record temporary is cleaned before proceeding. Return both original
records together only after every read succeeds; no partial result, fallback or effect occurs.

Repeated observation is not an atomic artifact reservation. Actual native source bytes and
semantics, newest eligible run selection, original/current policy and executing pin equivalence,
actual later protected consumer chronology, caller graph and owner/writer authority remain
required. Reauthenticate around later consumption and effects. Genuine unavailable source proof
needs a fresh coherent full generation through the shared pipeline; API failures or authenticated
corruption never become an optimization miss. Actual recovery/reuse/settlement integration and
Linux/hosted/native conformance remain open.

### Original complete Build payload after merge

download_merged_build uses the shared bounded pair preflight before any API read, retaining
original caller plan/descriptors and an actual historical source observation. Require the
original coherent full seal pair, then select its exact complete Build bundle; no caller-supplied
replacement payload descriptor is accepted. Preserve original producer/controller/kit identities.
Share complete bundle numeric API metadata/latest attempt/full graph/upload, exact ZIP size/hash,
bounded extraction, actual canonical envelope/inventory/scope and stable byte verification.

Materialize through the existing private atomic export copy. Fixed internal source admission
repeats before/after extraction; inside the copy's final before-publish callback, reread the
entire original coherent seal pair and compare canonical fingerprints, close retained historical
source and bounded caller snapshots. Existing materialization verifies the staged complete
bytes again after that callback and before atomic publication. A moved source/attempt, lost
seal or corrupt/mismatched bytes reject without publishing. Caller owns an original private
parent inaccessible to disposable writers; this route cannot create that lifetime/provenance.
No public callback, command, execution selector or new document/bound is added.

These copied bytes retain original JAR metadata and identity. Generic envelope/hash validity
does not prove native compiler/JDK/package semantics. Complete runtime payloads, native second
validation, newest eligible run selection, original/current protected policy and pin equivalence,
actual later consumer chronology and writer/owner authority remain required before reuse or
settlement. Repeated API observations reserve no artifact; reauthenticate around future effects.
Linux physical no-follow/UID/private-copy and real hosted/native conformance remain mandatory.

### Local template activation preflight foundation


Before manifest selection/writes, check/evaluate, sync and init call the bounded no-follow
local activation preflight. Legacy absence remains valid with the current Pages-only registry.
Present disabled data requires strict 8 KiB activation and separate regular 1 MiB native config
with matching repository/profile. Shadow/active/rollback modes fail until their fixed callers
and native admission are implemented; this guard does not substitute for implementing them.
Original-controller API admission remains a separate protected verifier obligation. No local
parsed data grants owner approval, execution/status authority or complete removed-marker
protection. Actual active rendering, transitions, pin/bump/bootstrap parity and hosted Linux
conformance remain incomplete. Config owns the fixed Build config path with its old controller
alias preserved; no data field chooses another path.

## Initial runtime envelope (inactive v1)

`mod-base.ci.runtime-envelope` starts at schema 1 with no predecessor; the archived v1.0.3
reader rejects it. `build_ci.runtime_schema` (MB11) validates closed identity, plan_sha256,
profile, producer, scope, lane_id, owning_build, lanes and files. Each lane declares exactly id
and native_contract_sha256; each file declares path, lane_id, role, size and sha256. This is a
transport inventory derived from native outputs, never another scenario/capture catalog.
The owning_build must be the bound complete Build descriptor. Scope lane selects one lane;
complete retains the complete ordered plan lanes/contracts when a plan is supplied. Files are
canonical export paths (`grammar.is_export_path`, the rule the sealed tree and its archive are
walked and extracted with, so a file keeps the name the mod gave it), path ordered, case/prefix
consistent, exclude Git internals and reserved envelopes,
and cover every declared lane. Aggregate-only files have null lane_id only in complete scope.
Every lane retains the original QS/BP 512-file/256-MiB caps, including inside a complete export.
Whole complete inventory retains BP's 4,096-file/512-MiB fan-in cap. The envelope is at most
4 MiB; role bounds retain report 4 MiB, log/crash 16 MiB and screenshot 32 MiB. Reports/images
cannot be empty. Role labels do not confer native validity or permit native bound bypass.

Bounds were read from immutable QS/BP e2e/packaged_runtime.py and BP scripts/ci/e2e_fanin.py
at the design snapshots. The common complete shape is new composition, not reproduced QS
aggregate conformance. Actual native mapping, frozen complete bytes, authenticated original
producer/Build seals, private source/root provenance and independent native report/image/log
validation are still mandatory. This parser does not implement runtime byte transport, reuse,
status publication or consumer activation.

### Runtime frozen bytes and independent copies

bind_runtime_envelope requires exact original producer/scope/plan/profile bindings and the whole
independently admitted owning Build descriptor. Runtime lane artifacts bind scope lane; results
artifacts bind complete. Supplied metadata is not actual API authentication.
runtime_exports.verify_runtime_export bounds complete logical closure before reading content,
requires canonical ci-runtime-envelope.json and matches every file's actual size/SHA-256 through
MB1 regular-data inspection. Empty logs are preserved, every extra/missing/changed file rejects,
and the original envelope is reread. The logical entry cap derives from the existing maximum
file count and legal path depth; it expands no native payload file or byte cap. Bounds include
only the outer envelope overhead in addition to the selected native payload scope.
materialize_runtime_export uses MB1 regular-data copying and exclusive atomic publication,
checks the copied inventory and repeatedly verifies both stage and source. Its private internal
closing transport callback is followed by complete stage/source byte revalidation. Public
callers cannot supply admission callbacks. Original private writer-excluded root/output parent
and real lifecycle provenance remain required. Windows fixture filesystem substitutions verify
known physical bytes and reject tampering, but establish no Linux no-follow/UID/atomic guarantees.
This adds no API runtime download route, native success, status, reuse or consumer activation.

### Frozen inputs around native lane verification

MB11 runtime_inputs adds the frozen runtime lane execution binding
(`execute_frozen_runtime_validator`), tool-fenced like Build verification. Require the
independently admitted plan, exact lane scope/identity and runtime producing run/attempt. Bind
the complete retained Build envelope to the runtime's whole owning Build descriptor; that Build
may legitimately be from a different run. Snapshot bounded canonical plan/Build/runtime inputs
and derive a fixed domain-separated context digest from plan identity and both envelopes.
Before and after the existing protected verify_runtime hook, authenticate the three fixed roots
(validation-input, sealed-build, sealed-runtime), runner owner/validator reader permissions,
no ACL/writable metadata, complete actual plan/Build/runtime inventories and directory identities.
Recheck original caller bytes after execution, so mutable inputs cannot replace initial approval.
Always quiesce the admitted validator. Native closed report/capture/JDK/package validation stays
in the original protected mod dispatcher; this introduces no second scenario catalog.

Independently prepared reclaimed private roots, original candidate quiescence and writer
exclusion, genuine protected source/API/plan and complete program/interpreter/tool provenance
remain caller prerequisites. Execution/context objects are data, not authority. They can retain
failed execution or truncated diagnostic output; subsequent Root freezing requires genuinely
successful native execution, while bounded diagnostic truncation alone is not native failure.
Real hosted Linux/native lifecycle conformance remains open.
New Windows tests use account/metadata/executor seams and prove no actual UID or native execution.

freeze_frozen_runtime_validation supplies the separate Root-only receipt binding. Authenticate
Root before account lookup or cleanup, require genuinely retained successful bounded execution
and its exact original context digest, then independently parse canonical plan/complete owning
Build/lane snapshots. Quiesce the admitted validator and inspect all three read-only roots and
complete byte inventories before and after the existing validation.freeze_validation_export.
Derive only verify_runtime and the retained enrolled lane; no caller-selectable hook or receipt
context exists. Original runtime run/attempt and cross-run owning Build bindings remain exact.
Reject changed root identities and original caller inputs, then reauthenticate Root before
returning the independently frozen receipt. Always terminate the admitted validator.

Retain genuine protected execution/source provenance across the privilege transition; matching
constructible objects or private copies do not establish it. Failed closing admission can leave
a private frozen copy, which must not be consumed or uploaded. Native validity and final actual
API/graph/artifact/source/current-policy/upload/status admission remain independently required.
Root launch/handoff enrollment for runtime and actual hosted integration remain open; this adds
no independent process, production workflow or success authority. Seven additional seam-based
tests validate closed receipt data and context/termination/pre-post binding, without actual UID,
native report bytes, private ownership transfer or Linux execution proof.

prepare_runtime_validation implements the separate Root-only read grant for a genuinely
independent already-reclaimed runner-private lane copy. Original canonical plan/complete owning
Build/lane context and producing runtime attempt remain exact. Quiesce candidate and validator,
authenticate the already-prepared plan/Build and the private runtime root (0700 directories,
0600 files, exact runner owner/group and no ACLs), and verify original complete runtime bytes.
MB1 grant_regular_data_read_access preserves empty logs/crash reports through the existing
protected Root permission transfer: original bounded lane file/entry/byte caps plus canonical
envelope overhead, all original records rechecked, ACL removal, 0640 files/0750 directories and
root exposure last. Existing nonempty export/source contracts remain unchanged.

After transfer, authenticate all three original directory identities, metadata and complete byte
inventories and close original caller snapshots; reauthenticate Root before returning the retained
independent envelope. Failed admission restores only the admitted runtime root's private traversal;
restage before retry, never consume a partial handoff. Validator termination is mandatory even
if descriptor closing fails. A late close failure may leave a granted copy, while cleanup failure
cannot prove restored privacy; both reject return and forbid consumption. Original copy/reclamation/
source/API/native provenance and excluded
writers remain independent requirements; this operation never grants reads to a candidate-owned
original. Actual candidate runtime freeze/witnesses, privilege handoff/program enrollment and
real Linux/native/hosted workflow conformance are still required. Authored transfer tests use
syscall/metadata/record/worker seams and establish no physical UID/ACL/source lifetime guarantee.

### Original runtime execution channel and context

MB11 runtime_handoff adds runtime producer/consumer bindings to the existing fixed private
execution-v1 channel. JSON kind/version/keys/bounds remain unchanged; no program/path/hook field
is added. Runtime input_sha256 binds the previously defined runtime-validation-input-v1 domain,
exact original plan and complete owning Build/runtime envelopes. Original current runtime attempt,
protected source-config hash, plan identity and fresh retained random nonce remain explicit.
Build writer/reader public signatures and envelope-hash binding remain unchanged. Shared private
publication additionally rereads original staged canonical bytes after closing admission.

The runtime writer authenticates runner/account context, requires genuinely retained successful
bounded execution, quiesces validator and inspects original three read-only inputs. Independently
parse snapshots and close input directory identities/bytes and original caller/source context
inside exclusive fixed-channel publication. Publish only canonical closed data in runner-private
0700/0600 single-link layout, never overwriting an existing channel. Always quiesce validator.

The Root consumer authenticates Root/accounts and original input context, quiesces validator,
inspects input roots and uses the existing bounded no-follow private physical record reader.
Require exact canonical record/nonce/run/attempt/source/plan/three-input hash before reconstructing
RuntimeValidationExecution. Delegate the existing original runtime receipt freeze, then reread
the original private record, all input identities/bytes and original caller/source snapshots;
reauthenticate Root before returning and always quiesce validator. Closing failure can leave a
private receipt, which must never be consumed or uploaded.

Private physical runner origin alone proves no genuine protected execution/source/native or
upload/status authority. Independent caller/program/interpreter/kit/tool/root/request enrollment,
original actual API/native provenance and excluded writers remain prerequisites. The fixed
Root bootstrap now has an explicit fixed runtime route; original runtime process enrollment and
real hosted Linux/native conformance remain open. New authored channel/context tests use explicit
input/metadata/syscall/publication/read/receipt seams and assert no actual UID/physical channel,
native execution or Root process provenance. Existing execution-v1 shape/strict rejection stays.

### Original candidate runtime export reclamation

The required hosted LinuxWorkerTests now include actual candidate runtime private-copy/read-grant
cases. A credentialless candidate writes a protected synthetic tracked dispatcher/export with a
nonempty report and empty log/crash data. Root preparation uses original plan/complete cross-run
owning Build, freezes through actual source inventory/metadata/byte/copy/ownership primitives,
then grants only validator-group reads. Assert complete private 0700/0600 and final 0750/0640
ownership, exact zero/nonzero bytes, independent copied inodes, unchanged candidate originals,
candidate read denial, both workers' write denial and quiescence. A real original-file hardlink
must fail private metadata admission before sealed-runtime appears. No primitive is mocked.

These cases are authored and unexecuted on this Windows host. Their existing fresh GitHub-hosted
Linux/sudo/account prerequisites remain mandatory and are never simulated. They prove no native
report/JAR/API semantics. Runtime verification uses the same tool-fenced second-account route
as Build verification.

MB11 runtime_freeze.freeze_runtime_export requires retained successful candidate execution,
original protected tested-tree inventory/native generated-root policy, exact enrolled lane/current
runtime attempt and independently selected whole complete owning Build descriptor/bytes. Snapshot
bounded canonical original plan/Build/selection data. After authenticating Root/accounts, quiesce
the candidate and inspect fixed traversal/home/source metadata, already-prepared plan/Build inputs,
complete actual tracked-source witness and private candidate export. Validate actual runtime bytes
and exact lane/producer/plan/Build binding; candidate metadata cannot select another Build artifact
ID/digest/upload window even when its producer and Build envelope would otherwise bind.

Independently copy the fixed candidate export to sealed-runtime through existing bounded data
materialization. Inside final private publication, repeat complete source, original plan/Build root
identities/bytes, caller snapshots and exact originally admitted runtime envelope. Recheck after
copying. MB1 privatize_regular_data_copy transfers only the new private Root-owned copy to the
runner, using original lane caps plus envelope overhead, preserving empty logs/crash reports and
removing inherited ACLs through existing 0700-directory/0600-file/root-last transfer. Existing
nonempty/source APIs remain unchanged. Candidate originals retain candidate ownership.

Recheck original copy FD/ownership and independently reopen the fixed named copy to reject inode
substitution; authenticate all private metadata and full runtime bytes. Close complete tracked
source and original plan/Build/caller admission again and reauthenticate Root before returning.
Always terminate the admitted candidate, including descriptor-close failure. Failed closing or
cleanup may leave a private copy; never consume it. Genuine original execution/source/API/native
policy/tool/runtime provenance is an independent prerequisite, not established by constructible
metadata or matching hashes. Second-UID native verification/receipt freezing and final actual
graph/source/artifact/policy/upload/status admission remain required. Current tests use explicit
source/record/copy/syscall/UID seams and prove no native JAR/report validity or physical Linux
ownership/source-lifetime/cancellation guarantees; real hosted workflow conformance remains open.

### Fixed runtime ZIP extraction

MB1 bounded_zip.extract_runtime accepts only exact string scope lane or complete. It preserves
zero-byte files for logs, without changing Pages/Build extraction limits or nonempty contracts.
Payload file cap is 512/4,096 plus exactly one outer envelope; expanded payload cap is 256/512
MiB plus at most 4 MiB outer-envelope overhead. Entry size cap is 32 MiB, including the outer
envelope before its own 4-MiB content check. The complete logical-entry bound is central and
compressed archives remain at most 512 MiB, for bytes and Path inputs before ZipFile allocation.
Central-directory allocation is bounded first, file count and declared native payload budgets
before inflation. All existing name/case/prefix/type/encryption/method/overlap/ratio/directory
checks and streamed CRC/size checks are shared. Empty deflated/stored logs both survive.

Transport extraction alone cannot distinguish role-specific report/log/crash/screenshot limits
or native validity: verify_runtime_export must additionally validate the exact canonical envelope,
complete actual bytes and native file-role mapping. Numeric API provenance/coherent original
seals and native semantics remain mandatory. New authored ZIP tests use actual shared parser,
ZipFile payload/CRC streaming and real temporary file writes. Windows file-opening/sealing/
publication substitutions assert no Linux no-follow/UID/exclusive atomic guarantee. The test
publication copy avoids observed host scanner rename races; it is explicitly not atomic proof.

### Original complete runtime payload after merge

transport.download_merged_runtime shares bounded original pair argument/snapshot preflight,
retains the actual merged source across the entire read, and uses download_merged_gate_pair for
both full original tested seals. Select exactly that packaged seal's unique results descriptor;
no public replacement descriptor or admission callback exists. Original producer/attempt/kit,
complete graph/timeline/upload/artifact availability are admitted by the full pair readers.
Recheck numeric selected metadata and download by its immutable ID under its byte bound; require
actual ZIP length/SHA-256 before extraction. Fixed complete runtime ZIP extraction, canonical
runtime envelope and full actual inventory validation precede binding to the exact results and
whole original owning Build selections. Recheck selected metadata and historical source.

Inside the existing runtime materializer's private before-publication hook, reread both full
original seals, compare their canonical fingerprints, close source and bounded original caller
snapshots, and compare the source export to the exact envelope admitted immediately after ZIP
extraction. This catches a self-consistent source replacement between binding and copying.
The materializer verifies both stage/source again after that hook before atomic publication.
The runtime aggregate includes all declared plan lanes, but domain/native correctness and
actual owning Build bytes still require their independent complete validation. Native report/
frame identities are not inferred from generic filenames or roles. Newest eligible sources,
original/current protected policy/pin, actual later consumer chronology and writer/owner
admission remain mandatory. This reader activates no consumer, native success or reuse authority.

### Combined original Build/runtime private inputs

transport.download_merged_inputs retains the original bounded caller and merged source, reads
the coherent full pair, and invokes both existing complete byte readers inside one outer private
atomic stage. Fixed children build and runtime are independently copied through their original
ZIP/inventory/role/scope and byte bounds. If runtime fails after Build completes, neither child
is a published caller output. The outer entry cap derives from the two existing bounded logical
closures plus their one parent; no native payload cap is widened.

After both children complete, bind their actual envelopes to the original complete bundle and
results descriptors, including the runtime's exact owning Build. Repeat the actual full coherent
pair, compare canonical fingerprints, close retained historical source and original caller
snapshots, bound the complete outer closure and require exactly the two fixed child names.
Then rehash both complete children before exclusive outer publication: Build mutation during
runtime copying or either child's corruption during final API reads cannot publish. Return the
two original envelopes in Build/runtime order. No public path selector or callback exists.

This establishes composition of generic original bytes, not native Build/runtime/JDK/package/
frame validity, newest eligible source selection, protected policy/pin equivalence, later consumer
chronology, App/reuse/owner/writer authority or reservation of external artifact availability.
The caller must independently retain original private output ancestry and exclude both workers.
Native domain validation and fresh complete admission remain required before subsequent effects.
