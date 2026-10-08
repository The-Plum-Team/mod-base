# Build/E2E implementation evidence

Scope: every step and invariant in [BUILD-E2E-DESIGN.md](BUILD-E2E-DESIGN.md). The original scope
is not reduced by this progress ledger. Baseline kit: f99ef433b66c729a869a2c1fbf038332c8b2f3e8.
The existing untracked design is preserved. No release or consumer activation has occurred.

### Deterministic template fixture bytes and extension coverage (8 October)

Previous goal turn classification: no progress (separate explanation of the failure counts).
The preceding binary-reader implementation turn made progress. Revalidated the original full
design, current tree and terminal logs before choosing the next action. Existing template cases
expected extension rejection but Windows fixture writes used default cp1252/CRLF, corrupting
the authored UTF-8/LF managed region first. Actual temporary caller reproduction observed
cp1252, 26 CRLF pairs, bytes unequal to the authored UTF-8 and strict UTF-8 rejection. The original
extension method alone produced 46 failures before the fix; retained direct log:
mod-base-template-extension-before.log. This was fixture preparation, not authority to loosen
the production parser or byte checks.

Only tests/test_template_tool.py text I/O now supplies explicit UTF-8 reads and UTF-8/LF writes
(32 read calls / 32 write calls across 57 changed source lines). Existing explicit UTF-8 calls
remain unchanged; intentionally authored CRLF uses the existing exact-byte fixture helper and
write_bytes. AST comparison against the original Git blob, normalizing only text encoding/newline
keywords, confirms all other code, test methods, hostile payloads, assertions and rejection/skip
behavior remain identical. No production, schema, managed-template, lock or digest input changed.
Existing actual caller CRLF and autocrlf checkout regressions run and pass along with extension
scope/reference/permission/drift checks; these old Pages regressions do not implement Build/E2E
activation, new caller ownership or required native conformance.

Initial local module run: 47 executed, 44 passed, two existing WinError 1314 symlink-creation errors
and one existing POSIX 0644 assertion failure (Windows reported 0666). No test was skipped or
relaxed to hide these outcomes. Full module on Python 3.11/3.12/3.13 gives the same three failures
and 44 passes (121.804s / 121.089s / 121.809s under concurrent verification). Logs:
mod-base-template-fixture-utf8-py311.log, -py312.log and -py313.log.

Both full suites are terminal and still failed, each executing 1,889/discovering 2,113 with
86 failures/329 errors/67 skips (387.137s / 387.430s). Exact failed-name sets match between Python
3.11/3.13: 415 versus preceding 469, 54 removed and none added; versus original 544 baseline,
129 removed and none added. Counts alone are not the comparison. Raw logs retained as
mod-base-build-e2e-template-utf8-py311.log and -py313.log; corresponding -comparison.json files
retain complete removed/added names for both comparisons. Remaining failures are not all
diagnosed, and these Windows results establish no full green or hosted/Linux/native success.

External compilation, current template locks/digest, whitespace and no repository bytecode
checks pass. Source-index digest unchanged:
sha256:4de54a41b66feab8051218e4e12233af47327f9ce1b465dc6bd024a357329f58.
All verification processes are terminal. Full K1-K7/Q1-Q10/B1-B7 scope stays active/incomplete;
real Linux/native lifecycle and original runtime/caller/program/tool enrollment, Build/E2E
workflows/composites/profiles/activation and fan-out/fan-in, batch/reuse/settlement/downstream
admission, canary/release and both consumer/governance/dependency/adoption/rollback sequences
remain open. No consumer, status, settings, commit, push, PR, tag or Release effect occurred.

### Exact binary bounded reads and full-suite failure reduction (8 October)

Previous goal turn classification: progress (same-commit final-merge reuse correction, three new
cases, terminal 804-case focus and exact unchanged full-suite baseline). Revalidated actual full
logs before choosing work: the stable reader rejected unchanged icons/config/callers because
Windows os.open defaults to text descriptors. An actual inert temporary 21-byte payload containing
CRLF/0x1A reproduced translated CRLF and early EOF under O_RDONLY, exact bytes under O_BINARY,
and the previous reader's false "changed while it was read" rejection. This was byte handling,
not permission to relax source/size/link/identity admission.

Added getattr(os, "O_BINARY", 0) to model.canonical.read_regular_file's original flags. POSIX flags
and all pre/open/post regular-file, final-symlink, device/inode/size, bounded-read and byte-count
checks remain unchanged. No interpreter/worker/native host requirement changes. The shared JSON
reader now retains original CRLF bytes as it already promises; no document/schema/canonical-writer
format changes. One new cross-platform regression checks CRLF plus embedded 0x1A/trailing bytes,
all 256 byte values, a 64 KiB read-boundary crossing with CRLF/control/NUL bytes and original
strictly decoded CRLF JSON bytes. No existing rejection or platform test was weakened.

Direct canonical/interface run executed 38 tests with one existing Windows symlink-privilege error
and one FIFO skip; it was not green. New byte regression passes. The entire old symlink/type/
empty/oversize method cannot reach its assertions here because os.symlink raises WinError 1314;
do not report that negative method as verified or add an artificial skip. 805 distinct focused
cases pass on Python 3.11/3.12/3.13 (183.024s / 128.201s / 164.992s).

Both full suites are terminal and still failed: each executed 1,889/discovered 2,113; failed-name
sets are identical between 3.11/3.13, 469 names versus original 544, 75 removed and none added.
Each reports 139 failures/330 errors/67 skips (381.204s / 381.500s). Some retained failing cases
now reach later assertions rather than earlier read errors; changed failure/error totals do not
establish green or Linux validity. Existing descriptor-relative publication, POSIX Git/path and
Windows symlink limitations remain. Raw full logs retained as
mod-base-build-e2e-binary-reader-py311.log and -py313.log; corresponding -comparison.json files
retain every removed/added failed name, rather than comparing counts alone.

External compile, template locks, updated source-index digest, whitespace and no repository
bytecode checks pass. Digest:
sha256:4de54a41b66feab8051218e4e12233af47327f9ce1b465dc6bd024a357329f58.
All test/check processes are terminal. The full K1-K7/Q1-Q10/B1-B7 goal remains active and
incomplete: Linux/native fixture and lifecycle outcomes, complete runtime/caller/tool/program
enrollment, workflows/profiles/fan-out/fan-in, batch/reuse/settlement and downstream readers,
real canary/release, both consumer/governance/dependency/adoption/retirement/rollback sequences
remain open. No commit, push, PR, tag, Release, status, settings or consumer effect occurred.

### Same-commit final merge and original reuse provenance (8 October)

Previous goal turn classification: no progress (separate Cursor Profile lookup). Revalidated the
complete design and original tool-byte/SDK admission. Source-derived archive member records do
not independently enroll all host ancestors, interpreter/stdlib/ELF/system closure or original
caller; no locally observed tool digest was promoted into approval and no old metadata-only
validator was substituted. Those runtime lifecycle prerequisites remain open.

Found a K6 contradiction: authenticate_merged_pr_identity already admits a final merged commit
equal to the original synthetic tested SHA with exact original ordered parents/tree and protected
history, while validate_reuse_reference rejected that same SHA unconditionally. Removed only
that inequality in the inactive initial reuse-v1 format. Preserve separate covered non-PR/default
subject and original PR/source/producer bindings, both direct independent tested seals, exact
tree/policy/pin/graph/inventory/scenario/selection equivalence and original source bytes. No
fields/version/common-kind compatibility or Pages shape changes. Initial new-kind ledger and
predecessor rejection remain unchanged; no released schema was relaxed.

Three new ordinary cases cover same-SHA canonical round-trip with an independently supplied
covered plan/selected upload and unchanged original source bytes; actual historical merged-reader
logic against a synthetic FakeGitHub final commit/current default with the original tested
parents, then reuse-reference binding; and rejection of source/current PR role changes, reversed
original parents, changed tree/policy/pin, chained references, mixed source plan, duplicated
artifact ID and wrong producer event. Existing unequal-SHA/merge/squash/rebase cases remain.
These are structural/read-only fake-API fixtures, not real merged provenance, native JAR/report
success, verifier chronology, latest attempts, whole-gate graph, artifact availability or reuse
authorization. Full K6/native/public consumer admission remains mandatory.

86 direct tests pass. 804 distinct focused cases pass on Python 3.11/3.12/3.13 (164.267s,
113.057s, 156.086s). Both full Windows suites are terminal and failed: each executed 1,888 /
discovered 2,112, exactly the same 544 baseline failed names, none added/removed (122 failures,
422 errors, 67 skips; 347.537s / 347.590s). Retained logs:
mod-base-build-e2e-reuse-same-commit-py311.log and -py313.log. External compile, template locks,
updated digest, whitespace and no repository bytecode checks pass. Source index digest:
sha256:54bd27ae799d4777f26681ef42361c84f531ee9fe8438c49ce13459284432a7c.
All test/check processes are terminal. No Linux/native hosted success, commit, push, PR, tag,
Release, consumer activation, status publication or settings change occurred.

The whole K1-K7/Q1-Q10/B1-B7 objective remains active. Next runtime work still requires genuine
original approved tool/runtime/caller/program admission and three-input byte-fenced second-UID
execution, real private handoffs/Root sealing/parent receipt outcomes on fresh hosted Linux.
K6 additionally needs complete original/current native policy, newest attempts, coherent successful
whole graphs, still-available actual payloads, actual later verifier chronology, downstream
readers and final protected authority. Workflows/profiles/batches/settlement/canary/release and
both consumer/governance/dependency/adoption/rollback sequences remain incomplete.

### Required hosted runtime candidate-copy and read-grant fixtures (8 October)

Previous goal turn classification: progress (explicit fixed runtime bootstrap routing and parent
receipt/source/program/tool/SDK re-admission, eleven new cases and terminal exact baseline checks).
Rechecked actual runtime candidate freeze, empty-data transfer/read-grant and existing hosted
Build verifier fixture against the complete original design. The Build fixture still uses the
legacy metadata-only tool route; do not reuse it as runtime's required byte-fenced validator or
assert that it enrolls a complete original interpreter/system/caller. Full scope remains unchanged.

Added two LinuxWorkerTests cases, collected by the existing required explicit hosted CI command:
actual candidate runtime copying/read grant with empty data, and original hardlink rejection before
private copying. Fresh real GitHub-hosted Linux/sudo/account prerequisites are unchanged; no host
flags are simulated and these account tests are never executed on Windows. No physical primitive
is mocked. Candidate runs an authored synthetic dispatcher with actual isolated execution, verifies
its real UID and producing run 43/attempt 2 and absent token/environment capabilities, and writes
known report bytes plus zero-byte log/crash files. Build retains its separate producer run 42.

Prepare the original canonical plan/complete Build through actual Root read-only primitives.
Bind a protected synthetic full tracked-dispatcher Git blob inventory and original whole owning
Build, then invoke actual Root runtime freeze, source/private metadata/byte/copy/ACL/ownership
checks. Compare exact copied envelope/data, every runner-private directory/file UID/GID/0700/0600,
distinct copied/original inodes and unchanged candidate-owned originals. Invoke actual runtime
read preparation and check every runner/validator-group 0750/0640 node, exact validator reads
including empty bytes, candidate read denial, both UID write denial and final quiescence. The
hardlink case creates a real original-file link, requires the specific private metadata rejection,
asserts no sealed-runtime publication and confirms candidate quiescence. Existing dispatcher
fixture run defaults remain 42; only this runtime fixture explicitly passes 43.

Source review caught recursive mkdir creating intermediate Build directories with default rather
than private modes; corrected explicit per-directory 0700 creation before checks. Also corrected
the generic dispatcher fixture's default Build run for the runtime case and added direct identity/
attempt assertions. AST/compilation and actual generic original context validation succeeded.
These corrections were reviewed before launching suites; no initial test failure occurred.

Both new physical cases are authored but unexecuted on this Windows host. Synthetic report/JAR/
descriptor bytes prove no native/API/source-event semantics, complete SDK/program/caller approval,
new runtime validator execution or fixed Root process success. This is additional required physical
coverage, not a substitute for the full original runtime lifecycle or hosted/native outcome.
801 existing focused cases pass on Python 3.11/3.12/3.13. Both full Windows runs are terminal and
failed: each executed 1,885/discovered 2,109, exactly the same 544 baseline failed names, none
added/removed (122 failures/422 errors/67 skips). No extra intermittent failure appeared. New
hosted account cases intentionally do not enter ordinary discovery. Retained temporary logs:
mod-base-build-e2e-runtime-hosted-copy-py311.log and -py313.log. External compile, unchanged locks/
digest, whitespace and no repository bytecode checks pass. Library/source index digest unchanged:
sha256:a9f7ef446246f0b9fe2cfddd75567ee7357a668789ee083f12883380cbf8e53e.
All test/check processes are terminal. No physical hosted success is claimed.

Next lifecycle work remains genuine original approved tool/runtime admission and three-input
byte-fenced second-UID runtime execution, actual private execution/request channels, fixed Root
runtime bootstrap sealing and independent parent receipt admission in required hosted fixtures.
Never replace independent approval with a locally observed hash, constructed execution/proof or
the legacy metadata-only Build route. Actual hosted primitive outcomes, complete original caller/
program/interpreter/stdlib/system/installer enrollment, native QS/BP immutable fixture parity and
report/JDK/package/frame verification, cancellation/orphans/ownership, workflows/fan-out/fan-in/
sealing, activation/reuse/writer/consumer/release and every K1-K7/Q1-Q10/B1-B7 requirement remain
open. No consumer/status/settings/commit/push/PR/tag/Release operation occurred. The goal stays active.

### Explicit runtime bootstrap dispatch and independent parent receipt admission (8 October)

Previous goal turn classification: progress (separate original runtime Root request/schema/channel
and fixed library sealing with terminal three-version focused/two-version exact baseline checks).
Rechecked actual current source/protocol and the full K1-K7/Q1-Q10/B1-B7 objective. Added explicit
runtime process routing and parent receipt admission without declaring native/hosted gates complete.

Legacy bootstrap Build entry keeps its original 12 ordered flag/value pairs and fixed dispatch.
The separate runtime process capability adds only leading --operation runtime-validation-v1,
centrally owned by grammar and independently mirrored in the stdlib guard. Unknown version/value,
pair order, duplicates, malformed/unbounded/inexact arguments reject before kit loading. Request
data never select code, a pathname, callback or native operation. Both fixed targets import only
after original kit byte admission; runtime target imports before account lookup. Code review found
that importing it after composition could strand an admitted account on import failure; moved that
import before lookup and added explicit negative coverage. Existing Build route/signatures stay intact.

execute_privileged_runtime_freeze_request and its installed-Python companion use the original
Root/host/account/Invocation/private program/approved complete tool byte admission and existing
clean bounded -I/-B/-S launcher with fixed private cwd and 20-second entry timeout. Admit the original
typed runtime request, snapshot source/scalars and canonical three-input bytes, derive the existing
runtime input domain digest, and preserve the complete owning Build's distinct producing run.
No arbitrary operation/path/command or observed digest promotes itself into original caller approval.

After silent successful process exit, repeat program/tools/SDK and original request admission,
reject original caller context drift, and independently read exact private sealed runtime receipt/
native report bytes under fixed verify_runtime/lane/current attempt/source-config/input digest.
Reread original request after parent receipt reading, re-admit program/tools/SDK again, then read
and compare original receipt bytes independently a second time. Close caller byte signatures,
private metadata and named sealed-root identity, reauthenticate Root, and always quiesce the
admitted validator. Failed context/process/receipt/closing admission never grants consumption or
upload; role/account denial precedes unadmitted cleanup. Worker/native/domain/file bounds and
legacy Build behavior are unchanged; runtime SDK source admission brackets all four tool checks.

Eleven new cases cover explicit capability/parser/launcher mirror and unchanged legacy parser,
unknown selectors before loading, actual unsupported-host process rejection, fixed runtime-only
dispatch after byte admission, composition/freeze/import failure boundaries, exact command/context/
cross-run producer and two parent receipt reads, missing admission/role denial, original plan/Build/
runtime caller mutations after child and during receipt reading, late request/program/tool/receipt/
root changes, failed/OS/non-silent child, source-derived SDK/path/late SDK drift and unsupported SDK/
context types. Actual closed generic context and validation-receipt schemas run. Host/physical
request/program/tool/SDK/source/receipt/process seams prove no real Linux UID, complete interpreter/
system provenance, native outcomes or actual protected program/caller authority. Actual subprocess
rejection proves failure on this unsupported Windows host, not successful runtime execution.
62 direct new/old/bootstrap/installation/interface cases pass; no initial test failure occurred.

801 distinct focused tests pass on Python 3.11/3.12/3.13. Both full Windows runs are terminal and
failed: each executed 1,885 tests/discovered 2,109, exactly the same 544 baseline failed names,
none added/removed (122 failures/422 errors/67 skips). No extra intermittent failure appeared.
Logs retained in the host temporary directory: mod-base-build-e2e-runtime-root-process-py311.log
and -py313.log. External compilation, updated/check-passing tools staged-file lock, action lock,
staged digest, whitespace and no repository bytecode checks pass. Staged source digest:
sha256:a9f7ef446246f0b9fe2cfddd75567ee7357a668789ee083f12883380cbf8e53e.
All test/check processes are terminal. These results establish no real hosted/native validity.

Next source lifecycle work is required hosted Linux runtime integration: original candidate
reclamation/empty-data private transfer and read grant, three-input byte-fenced second-UID native
validation, actual private execution/request channels and fixed Root runtime bootstrap sealing.
Preserve existing hosted prerequisites and never simulate GITHUB_ACTIONS/RUNNER_ENVIRONMENT or
execute these account tests on Windows. Original full program/interpreter/stdlib/system/installer/
caller enrollment, actual native QS/BP immutable fixture parity and report/JDK/package/frame checks,
Linux cancellation/quiescence/ownership, workflows/fan-out/fan-in/sealing, activation/reuse/writer/
consumer/release and every K1-K7/Q1-Q10/B1-B7 requirement remain open. No consumer/status/settings/
commit/push/PR/tag/Release operation occurred. The original complete goal remains active.

### Separate original runtime Root request and fixed library sealing (8 October)

Previous goal turn classification: no progress (separate Cursor Profile lookup). Revalidated
the actual design K1-K7/Q1-Q10/B1-B7, original Build request/physical channel, runtime three-input
context and completed handoff logs. Independently repeated exact baseline failed-name comparison
for that prior handoff, then added the separate runtime Root request contract and library route.
The original full objective remains unchanged; no gate is declared complete by these fixtures.

New mod-base.ci.runtime-root-request initial v1 has closed boundary/validator/source metadata,
original plan/complete Build/runtime lane, exact lane/current runtime run/attempt and distinct
entry/execution nonces. Preserve legitimate different owning Build producer and bind its whole
descriptor; no operation/program/hook/permission/path field exists. Reuse pure source/account
metadata checks while retaining unchanged Build request-v1 shape, semantics and signatures.
Registry, bounded document dispatch, per-kind ledger, current writer and hostile mutation fixture
advertise new kind/current 1/previous null with explicit predecessor rejection. Individual existing
plan/Build/runtime/native/file/artifact bounds stay intact; only the separate local metadata
whole cap adds the already-existing runtime envelope allowance to the Build request allowance.

runtime_root_request publishes exclusively to fixed runtime-root-request/ci-runtime-root-request.json
with runner-private 0700/0600 metadata and fresh entry nonce. Snapshot/independently parse the three
original inputs; authenticate runner/accounts/Invocation, inspect actual protected source and all
input bytes/inodes before and inside atomic publication, then close original caller/source metadata,
Invocation and host/account admission. Reread staged original bytes after closing admission before
publication. No public callback, executable selection or caller-chosen filesystem channel exists.

Root-only reader delegates the existing fixed bounded no-follow/single-link/private-origin physical
reader, requires canonical/strict JSON and retained nonce/host/validator/Invocation/installed kit,
reconstructs actual protected source bytes including empty inventory entries, then repeats original
request/source/three-input byte and inode admission. Fixed library sealing delegates runtime_handoff
with its original execution nonce, re-admits the whole request afterward and compares original
source/scalar/canonical three-input byte signatures, including the original caller context. Always
quiesce the admitted validator, reject unadmitted Root/accounts before cleanup, and never consume
or upload a private receipt left by closing failure. No domain code is imported by this route.

Four schema cases and eight channel/composition cases cover cross-run ownership, mixed plan/scope/
lane/attempt, exact types/metadata/account separation, unknown privileged selectors, old-kind
separation, preserved caps and strict JSON; private fixed publication/reader, original caller and
staged-byte drift, physical-channel delegation, actual source reconstruction/corruption, kit/root/
input/record/Invocation drift, role denial before reads/cleanup, missing-channel OS rejection and
post-freeze original context changes with termination. Actual generic source/context/schema checks
run; syscall/private metadata/source-copy/physical-reader/freeze seams prove no real Linux UID/
channel, complete source lifetime, native results or independently enrolled process provenance.

Initial import exposed a new cap declared before its dependency; corrected constant order.
Initial route tests used an unchanged profile and corrupted an empty source whose actual whole-copy
inspection was mocked; corrected meaningful mutations and documented the new grammar name.
53 direct new/old/interface/compatibility cases then passed. Initial broad runs detected missing
new-kind coverage in the common hostile mutation catalog, not a new source/context failure.
Added a program-selector rejection without relaxing coverage or regenerating old fixtures.
42 direct mutation/new/interface/compatibility cases passed after that correction.

Final 790 focused cases pass on each Python 3.11/3.12/3.13. Both final full Windows runs are terminal
and failed: each executed 1,874 tests/discovered 2,098, exactly the same 544 baseline failed names,
none added/removed (122 failures/422 errors/67 skips). Logs are retained in the host temporary
directory: mod-base-build-e2e-runtime-root-request-verified-py311.log and -py313.log. Initial logs
without -verified are preserved: each had those baseline names plus the now-corrected common
mutation coverage case (123 failures/422 errors/67 skips). No extra intermittent failure appeared.
External compilation, existing template/tool/action locks, staged digest, whitespace and no
repository bytecode checks pass. Source digest:
sha256:dca704b0ffbcf2cd3ad007f9808572286f94924df659023391c71994951201a8.
All test/check processes are terminal. These Windows results establish no real hosted/native validity.

Next source step is explicit fixed runtime dispatch in the independent pre-import bootstrap and
parent launch/receipt admission. The current program's 12 ordered flag/value pairs and fixed
dispatch remain Build-only; preserve that route rather than sniffing request data to select code.
Add an explicit closed runtime process capability/route with mirrored parser/launcher conformance,
original retained nonces/context, repeated complete program/tool admission and independent parent
runtime receipt reading. Actual original program/interpreter/stdlib/system/caller enrollment,
native mod fixture parity and second-account report/JDK/package/frame validation, real hosted
Linux cancellation/quiescence/ownership, workflows/fan-out/fan-in/sealing, activation/reuse/writer/
consumer/release and every K1-K7/Q1-Q10/B1-B7 requirement remain open. No consumer/status/settings/
commit/push/PR/tag/Release operation occurred. The full goal stays active.

### Original runtime execution handoff and closing private record admission (8 October)

Previous implementation turn classification: progress (original candidate runtime reclamation
and private regular-data transfer). Added runtime_handoff through the unchanged closed generic
execution-v1 fixed private channel. Retain a fresh execution nonce, original source configuration,
current runtime producing attempt and the existing domain-separated digest of plan/complete
cross-run owning Build/runtime lane. Require genuinely retained successful bounded execution;
constructible objects/hashes grant no execution or native authority. Authenticate roles before
account lookup/cleanup, quiesce the admitted validator and close actual three-input bytes/inodes
and original caller snapshots inside publication. Shared execution writer additionally rereads
staged bytes after closing admission, preserving existing Build shape/signature/bounds.

Root-only reader admits the actual existing fixed private physical record, canonical closed JSON,
original nonce and all context fields, reconstructs execution only after admission, and delegates
the existing fixed runtime receipt freeze. Recheck original record, all three inputs and original
caller after freezing; reauthenticate Root and always quiesce the admitted validator. Failed
private receipts are never consumed/uploaded. Original protected caller/source/API/tool/runtime
provenance, excluded writers and independently enrolled Root request/process remain necessary.

Eight tests exercise actual closed context/schema/private publication mechanics through explicit
syscall/input/inventory/physical-reader/freeze seams, including staged byte drift after closing
inspection, wrong nonce/run/attempt/source/plan, substitution of Build-only digest, ambiguous JSON,
late record/input/caller changes and role denial before cleanup. These establish no real UID/Linux
channel, native report semantics, source lifetime or complete executing provenance. The existing
Build handoff cases remain passing. 778 focused tests passed on each Python 3.11/3.12/3.13.
Retained full logs are terminal: mod-base-build-e2e-runtime-execution-handoff-py311.log and
-py313.log, each 1,862 executed/2,086 discovered, failed with exactly the same 544 baseline names,
none added/removed (122 failures/422 errors/67 skips). Exact set comparison was independently
repeated on this continuation. Prior compile/locks/digest/whitespace/no-bytecode checks passed;
its staged digest was sha256:d5f36cd9bd058b3bf724924388fdaee8665920fa17f70587daf9f9e9c0d99309.
No consumer/status/settings/commit/push/PR/tag/Release operation occurred. Full scope stays open.

### Original candidate runtime export copying and private ownership transfer (8 October)

Previous goal turn classification: progress (original lane read handoff preserving empty data,
eleven cases and terminal three-version focused/two-version exact baseline verification).
Rechecked the full design/lifecycle, original Root Build export freeze, complete tracked-source
inspection, runtime byte materializer and existing protected private-transfer mechanics. Added
MB11 runtime_freeze.freeze_runtime_export and MB1 privatize_regular_data_copy; scope is unchanged.

Require original successful bounded candidate execution, complete protected tested-tree inventory/
native generated-root policy and original canonical plan/complete Build/independently selected
whole owning Build descriptor. Authenticate Root/account separation and quiesce candidate; inspect
fixed traversal/home/source, already-prepared plan/Build metadata/bytes, complete actual source
witness and private candidate export. Actual runtime bytes bind exact enrolled lane/current run/
attempt and the whole original selected Build. A candidate cannot replace artifact ID/digest/upload
window just because its producer and the actual Build envelope otherwise match. Independently
parse/snapshot original runtime before private copying; no second authored scenario catalog exists.

Copy only the fixed original candidate export to sealed-runtime through existing bounded regular
data materialization. Inside final private publication, repeat original full source/plan/Build/
caller snapshots and exact original runtime, followed by the materializer's own byte rechecks.
Repeat after copying. Transfer only the fresh Root-owned independent copy to runner-private
0700 directories/0600 files, preserving empty data with original lane caps/envelope overhead and
existing ACL/root-last/record comparison. Originals retain candidate ownership. Check retained
FD/private ownership and independently reopened named-copy inode, private metadata and full bytes;
close source/plan/Build/caller admission again and reauthenticate Root. Always quiesce admitted
candidate, including close failure. Failed closing/cleanup may leave a private copy; never consume it.

Ten new cases cover MB1 empty-data private modes/shared failure privacy; runtime original source/
Build/whole selection/copy order, survivor/pre-source/Build/private-metadata rejection before copy,
source/Build/original-runtime/copy-result changes inside publication before transfer, foreign
owner/transfer/FD inode/named-copy inode/bytes/final-source rejection, candidate alternate owner
artifact ID/scope/producer before copying, original caller drift inside publication, bad execution/
lane/generated policy before account mutation, cleanup/close failure with quiescence, and Root
denial before lookup. Actual generic descriptor/runtime schema/context checks run. Source/copy/
metadata/record/syscall/UID seams prove no physical Linux ownership, actual Git witness/native
JAR/report validity, candidate lifecycle or writer-excluded source lifetime. All direct cases and
interfaces pass without an initial implementation/test failure; actual protected provenance remains
an independent caller prerequisite. Constructible metadata/hashes confer no second-UID authority.

770 distinct focused tests pass on Python 3.11/3.12/3.13. Both full Windows suites are terminal
and failed: each executed 1,854 tests, discovered 2,078, with exactly the same 544 baseline failed
names, none added or removed (122 failures/422 errors/67 skips). No extra intermittent failure
appeared. Logs in the host temporary directory: mod-base-build-e2e-runtime-candidate-freeze-py311.log
and -py313.log. External compile, locks/digest, whitespace and no repository bytecode checks pass.
Staged source digest: sha256:0267ce6f5ca408410782763d6e88327e0a46beca818b3ee2b1530560bae436c3.
All test/check processes are terminal. These results establish no real hosted/native validity.

Next source lifecycle work includes physical-origin/context execution handoff for runtime and
fixed independently enrolled Root process/request admission. Current execution handoff and Root
bootstrap/request route remain Build-only; matching library objects never replace that provenance.
Actual native second-UID report/JDK/package/frame conformance, immutable mod fixture parity, real
Linux/hosted tests/cancellation and producer workflows/fan-out/fan-in/sealing remain open. Activation,
reuse/consumer/writer/release and every K1-K7/Q1-Q10/B1-B7 gate retain the full original requirements.
No consumer/status/settings/commit/push/PR/tag/Release activation occurred. The goal stays active.

### Original runtime lane read handoff with empty regular data (8 October)

Previous goal turn classification: progress (Root-only original runtime receipt/context binding,
seven tests and terminal focused/full verification, with the additional unchanged Windows image
WinError 5 recorded and isolated class reruns passing). Rechecked current full design/lifecycle,
MB1 ownership/ACL/root-last transfer, bounded regular-data inspection, existing plan/Build read
inputs and runtime scope bounds. Added MB1 grant_regular_data_read_access and MB11
prepare_runtime_validation. Preserve the complete K1-K7/Q1-Q10/B1-B7 objective and existing gates.

The additive MB1 handoff uses regular_data_records, retaining empty logs/crash reports and exact
file/entry/byte caps, through the existing protected Linux Root/owner/ancestor admission and
single-link/path checks. Recheck complete data before/after permission transfer, remove inherited
ACLs, set 0640 files/0750 directories and expose the root last. Existing nonempty export and source
contracts remain unchanged. This grants no candidate reclamation, native validity or upload.

Root-only runtime preparation requires the original exact enrolled lane/current producing attempt
and complete cross-run owning Build, retaining bounded canonical independent snapshots. Quiesce
candidate and validator, authenticate already-prepared plan/Build and original runner-private
runtime metadata/bytes, then grant validator-group reads under original lane payload caps plus
canonical envelope overhead. Recheck original FD ownership/mode/identity and reopen/verify all
three input roots and inventories. Close original caller snapshots and reauthenticate Root before
returning an independent envelope. Failed admission attempts to restore only the admitted root's
private traversal; cleanup failure remains visible and does not prove restored privacy. Late
descriptor-close failure can leave a granted copy, but rejects return and still terminates the
validator. Never consume failed handoffs; restage before retry. No unknown/foreign root is chmodded.

Eleven new tests cover MB1 empty-data transfer/ACL/root-last/private-on-failure mechanics and
bound forwarding/nonprivileged rejection; runtime scope caps/original context/order, surviving
candidate/foreign private metadata/changed Build or runtime bytes before grants, transfer/FD inode/
mode/closing bytes/Root drift, replacement of each of three roots, original caller mutation with
retained checks still intact, invalid scope before I/O, unprivileged denial before lookup/cleanup,
descriptor-close termination and visible private-traversal cleanup failure. These use explicit
record/metadata/syscall/worker/copy seams, not actual UID/ACL/source/native execution proof. All
direct source/interface/fixture cases pass; no initial implementation or test failure occurred.

760 distinct focused tests pass on Python 3.11/3.12/3.13 (including the previously unfocused
existing MB1 handoff cases). Both full Windows suites are terminal and failed: each executed
1,844 tests, discovered 2,068, with exactly the same 544 baseline failed names, none added or
removed (122 failures/422 errors/67 skips). No extra intermittent image/cache failure appeared.
Logs in the host temporary directory: mod-base-build-e2e-runtime-handoff-py311.log and -py313.log.
External compile, locks/digest, whitespace and no repository bytecode checks pass. Staged source
digest: sha256:cda878026c39ceec228f648637f0d1d2e58ae468e34f26f5b9d62bd8564f323b.
All test/check processes are terminal. These results establish no real hosted/native validity.

Next lifecycle work remains actual candidate runtime export reclamation with original execution
and complete tracked-source witnesses, preserving empty data through private ownership transfer.
Current MB1 privatize_tree_copy uses nonempty file_records; an additive regular-data private
transfer is needed. The read grant deliberately presupposes original independent reclaimed-copy
provenance and excludes writers. Root runtime process/handoff/program enrollment, native immutable
fixture parity, real Linux/hosted isolation and producer workflows/fan-out/fan-in/sealing remain
open, as do activation/reuse/consumer/writer/release and every migration gate. No consumer/status/
settings/commit/push/PR/tag/Release activation occurred. The full original objective stays active.

### Root-only original runtime validation receipt binding (8 October)

Previous goal turn classification: progress (original three-input byte-fenced runtime execution
binding, seven cases and terminal three-version focused/two-version baseline checks). Rechecked
the current full design, existing Root Build binding, validation export freeze, runtime inputs
and progress evidence. Added MB11 freeze_frozen_runtime_validation, retaining original scope.

Authenticate Root before account lookup/cleanup, require exact RuntimeValidationExecution with
its original context digest and genuinely successful bounded execution. Independently parse
canonical retained plan/complete owning Build/runtime lane snapshots. Preserve exact current
runtime run/attempt and legitimate different owning Build producer. Quiesce the admitted validator,
inspect all three fixed read-only roots and complete inventories, then call the existing Root-only
independent validation export freeze with fixed verify_runtime and the retained enrolled lane.
Recheck all root identities/bytes and original caller context before returning, reauthenticate
Root and always terminate the admitted validator. Diagnostic truncation alone remains permitted.
Shared private inspection separates runner/Root identity admission without weakening either;
existing byte-fenced execution still authenticates its runner role before input inspection.

Seven new tests cover exact cross-run original context/native receipt contract/termination order,
wrong digest or missing/nonzero/boolean/unbounded/wrong-type execution before copying, wrong
scope/lane/attempt/changed context, substitution of each of three roots, pre/post/copy/OS failure,
original caller mutation during freeze with retained byte checks intact, and unprivileged rejection
before account lookup/cleanup. Actual closed generic validation receipt schema runs; protected
host/account/tree/copy/worker seams establish no real Linux UID/no-follow/quiescence, native report
bytes, Root ownership transfer or execution/source provenance. No initial test failure occurred.
Genuine protected provenance across privilege transition remains caller-owned; constructible
objects/matching hashes are not approval. A private copy left after closing failure must not be
consumed or uploaded. Final actual native/API/graph/artifact/policy/source/writer admission remains.

737 distinct focused cases pass on Python 3.11/3.12/3.13. Both full Windows suites are terminal
and failed: each executed 1,833 tests, discovered 2,057. Python 3.11 has exactly the same 544 baseline
failed names, none added/removed (122 failures/422 errors/67 skips). Python 3.13 has those same
544 plus test_a_file_swapped_before_opening_is_refused in unchanged imaging StableReadTest
(123 failures/422 errors/67 skips); actual fixture os.replace failed with WinError 5 before the
expected image-substitution assertion. The full log is preserved, not reported baseline-identical.
All four unchanged StableReadTest cases pass independently afterward on both Python 3.11/3.13;
no imaging source/test change was made. Logs in the host temporary directory:
mod-base-build-e2e-runtime-freeze-py311.log and -py313.log. External compile, locks/digest,
whitespace and no repository bytecode checks pass. Staged source digest:
sha256:2902eb32fa0b98d745c39fc1e0e17d8f11e09acf1465d761ada2c837bd74b5f3.
All test/check processes are terminal. No real hosted/native validity is established.

Runtime read-grant preparation remains the next lifecycle step. Actual MB1 grant_tree_read_access
uses nonempty file_records, so it cannot preserve permitted empty runtime logs/crash reports;
add an explicitly bounded regular-data handoff using the existing protected transfer primitive
rather than changing Pages/Build/source contracts. Runtime candidate freeze/witness/provenance,
Root execution handoff/program enrollment, actual native parity, real Linux/hosted tests and
producer workflows/fan-out/fan-in/sealing remain open. Activation/reuse/writer/consumer/release
and every K1-K7/Q1-Q10/B1-B7 gate retain the complete original requirements. No consumer/status/
settings/commit/push/PR/tag/Release activation occurred. The full original objective stays active.

### Frozen plan/owning Build/runtime lane execution binding (8 October)

Previous goal turn classification: no progress (separate Cursor Profile lookup). Revalidated
the current design, K1-K7/Q1-Q10/B1-B7 scope and the pending runtime_inputs source against
existing byte-fenced controller hooks, plan/Build input lifecycle and runtime byte admission.
Completed the additive MB11 execute_byte_fenced_frozen_runtime_validator interface and tests.
It admits only an exact plan-enrolled lane, its producing runtime attempt and a complete owning
Build bound to the whole descriptor. Preserve legitimate different Build/runtime producer runs.
Independent approved tool byte digest is mandatory, with no metadata-only fallback.

Retain bounded canonical independently parsed original plan/Build/runtime snapshots and a fixed
domain-separated context hash containing plan identity and both envelope hashes. Before and after
existing protected verify_runtime execution, inspect all three fixed roots for runner owner,
validator-only group reads, original entry caps, safe metadata and exact complete inventories,
including empty runtime logs. Reject changes to any directory identity or original caller bytes;
always terminate the admitted validator. Execution/context values are data, not a frozen receipt
or success/upload/status authority. Native report/capture/JDK/package semantics remain mod-owned.

Seven new cases cover cross-run owning Build with exact lane selection, invalid scope/owner/
plan/lane/run/attempt/tool approval before input reads, substitution of each of the three roots,
caller mutation with retained snapshots still intact, pre/post/native/OS errors with termination,
foreign/writable/ACL metadata or either envelope mismatch, and whole owner-descriptor context
binding without claiming API authentication. Account/syscall/tree/executor substitutions prove
no actual Linux UID/no-follow/quiescence or native execution. Initial test-only upload-window
mutation used the wrong field nesting; corrected it to producer.upload_window. All seven pass.
Clarified comments/documentation that bounded diagnostic log truncation alone is not native
failure; genuinely successful native execution remains required before receipt freezing.

730 distinct focused cases pass on Python 3.11/3.12/3.13. Both full Windows suites are terminal
and failed: each executed 1,826 tests, discovered 2,050, with exactly the same 544 baseline failed
names, none added or removed (122 failures/422 errors/67 skips). Logs in the host temporary
directory: mod-base-build-e2e-runtime-inputs-py311.log and -py313.log. No additional intermittent
failure occurred. Final documentation/comment clarification changes no code/test behavior;
29 runtime-input/interface tests pass afterward. External compile, locks/digest, whitespace and
no repository bytecode checks pass. Staged source digest:
sha256:5bf0296f6f94f0a6cda118fec701e8bc6e6e0b4119425fde2dd24f8026560389.
All test/check processes are terminal. No hosted/native validity is established by these results.

Runtime read-grant preparation and Root receipt binding remain the next source lifecycle work;
original private reclaimed roots, candidate quiescence/writer exclusion and genuine protected
source/API/plan/program/runtime provenance remain independent prerequisites. Native immutable
fixture parity, real Linux/hosted isolation, producer workflows/fan-out/fan-in/sealing, activation,
reuse/policy/pin/current consumer/writer admission and every migration/release gate remain open.
No consumer/status/settings/commit/push/PR/tag/Release activation occurred. The full original
objective remains active; this binding does not replace the remaining requirements.

### Combined original complete Build/runtime private inputs (8 October)

Previous goal turn classification: progress (complete original runtime numeric transport, eight
actual API/ZIP/physical-data cases and terminal three-version focused/two-version baseline
verification). Rechecked complete design and both actual byte readers, original pair/source
admission, MB1 atomic cleanup/copy and logical depth/entry bounds. New download_merged_inputs
retains bounded original caller/source snapshots and the actual initial coherent pair, then
runs both complete byte readers under one outer private atomic stage. Fixed children build and
runtime remain unpublished caller inputs until both downloads and final whole admission succeed.

Retain both readers' exact original run/attempt/kit/graph/upload/artifact, ZIP/inventory/producer/
plan/scope/owning Build and byte proofs; no payload role or native bound is expanded. Bind both
actual envelopes to the initial original complete descriptors, repeat the full pair and compare
canonical fingerprints, close source/original caller snapshots, bound complete outer closure,
require exactly the two fixed names, and rehash both children after runtime and final API reads.
Return both original envelopes in Build/runtime order. Combined logical entry cap derives from
the two existing bounded child closures plus one parent; each reader keeps its native payload
file/byte limits. MB1 traversal depth 64 preserves existing legal depth-16 child paths here.
Source availability is observed, never reserved; fresh consumer/effect admission stays mandatory.

Seven new cases run actual original pair/source/run/graph/timeline/metadata/digest readers over
FakeGitHubApi data, actual shared Build/runtime ZIP/CRC/size streaming, actual canonical envelope
and binding logic and complete byte comparisons. Test-only Windows traversal/read/inventory/
copy/open/seal/publication substitutions physically copy/hash authored opaque payloads, including
an empty runtime log. They prove no native JAR/report/frame semantics, Linux UID/no-follow/atomic
or writer-excluded source lifetime. Original seal ZIP extraction/read keeps its known-data seam.
Cover both complete original byte sets and fixed children/26 numeric reads/two payloads/no API
mutations, bad output/binding before API, runtime digest failure after Build really copied with
neither output, Build tampering during runtime copy, original caller drift after inner retained
snapshots stay valid, either child's/extra-root-name/aggregate-expiry corruption during final
pair reads, and combined pre-content entry-cap rejection. All direct cases pass without an
initial source or fixture failure; actual native validation remains separate.

723 distinct focused cases pass independently on Python 3.11/3.12/3.13. External compile,
locks, digest, whitespace and no repository bytecode checks pass. Staged source digest:
sha256:b816fd49dd036326b6e00c8cc2b773061b8db31a2ae55b80868cfceeb9d4c244.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1,819 tests,
2,043 discovered, with exactly the same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). No extra intermittent image/cache/fixture failure appeared.
Logs in the host temporary directory: mod-base-build-e2e-merged-inputs-py311.log and -py313.log.
All test/check processes are terminal. These results establish no Linux/hosted/native validity.
Read-only immutable Git source inventory again located BP scripts/ci/tests/test_e2e_fanin*.py,
matrix_fixtures.py and tests/fixtures/release-matrix-schema1.json, plus QS packaged runtime/
build matrix/provenance/visual/install fixtures; none were executed, checked out or changed.
Native fixture conformance still requires actual independent schema/domain parity.

K6 still requires native Build/runtime/JDK/package/frame validity, newest eligible sources,
original/current protected policy/pin equivalence, actual later consumer chronology and reuse/
writer integration. Live ready-PR runtime transport, producer fan-out/fan-in/sealing and native
immutable fixture parity, Linux/hosted/workflow/canary and all other migration gates remain open.
No consumer/status/settings/commit/push/PR/tag/Release activation occurred. The full original
K1?K7/Q1?Q10/B1?B7 objective remains active; generic byte composition does not authorize success.

### Exact original complete runtime download after merge (8 October)

Previous goal turn classification: progress (fixed runtime ZIP primitive, eight actual parser/
CRC/physical-data cases and terminal focused/full baseline verification). Rechecked current
transport, original pair/timeline/records, fixed runtime extractor and byte materializer before
implementation. New download_merged_runtime retains original merged source and bounded original
caller snapshots, reads both original full tested seals through download_merged_gate_pair, and
selects only its unique complete results aggregate. No public replacement descriptor or source
approval callback exists. Actual pair admission retains original run/attempt/kit/graph/upload
and every lane/source artifact availability, plus exact whole owning Build bundle coherence.

Recheck numeric selected metadata, download only by its original ID, and reject wrong actual
ZIP length/SHA-256 before extraction. Fixed complete runtime ZIP/role/scope/plan/inventory byte
validation precedes exact original runtime producer/owning Build binding. Recheck selected
metadata and retained historical source. Inside final byte publication, reread both complete
original seals and compare canonical fingerprints, close source and original caller snapshots,
and compare source export to the exact originally extracted envelope. A self-consistent source
replacement after binding cannot become the materializer's new approved input. The existing
materializer then rechecks both source/stage bytes again after callback and before publication.
Original private root/output ancestry and writer exclusion remain independent prerequisites.

Eight new cases run actual original pair/API/timeline/graph/metadata/digest logic over authored
FakeGitHubApi responses, actual bounded
runtime ZIP admission and CRC/size streaming, actual canonical runtime schema/inventory/binding
and materializer comparisons. Newly authored report/log payloads are opaque fixtures, including
an empty log. Explicit Windows file-opening/sealing/publication/read/inventory/copy substitutions
physically write/read/copy/hash these bytes, but establish no Linux no-follow/UID/atomic/source
lifetime or native report/JAR/runtime validity. Only original seal ZIP extraction/read is the
existing known-record seam; runtime ZIP parsing is real. Cover original complete bytes/nine
numeric reads/no API mutations, output preflight before API, corrupt ZIP before parsing, source/
stage/final-API byte tampering without output, either seal/runtime/lane expiry during copy,
historical/caller/latest-attempt drift, independently structurally valid different owning Build/producer/
scope, and an actual self-consistent source replacement after binding that verifies locally but
cannot publish. Corrected an initially weaker tampering assertion to inspect output before
TemporaryDirectory cleanup; no test or source failure remained in final direct/focused runs.

716 distinct focused cases pass independently on Python 3.11/3.12/3.13. External compile,
locks, digest, whitespace and no repository bytecode checks pass. Staged source digest:
sha256:22d4606e73bb9c67546202aa04ca6993056529135a6d50541a4dfe5650789efe.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1,812 tests,
2,036 discovered, with exactly the same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). No extra intermittent image/cache/fixture failure appeared.
Logs in the host temporary directory: mod-base-build-e2e-merged-runtime-py311.log and -py313.log.
All test/check processes are terminal. These results establish no Linux/hosted/native validity.

K6 still needs actual native Build/runtime/JDK/package/frame validity, actual owning Build
bytes/composed original source admission, newest eligible sources and original/current native
policy/pin equivalence, actual later consumer chronology and reuse/writer integration. Native
report identities cannot be inferred from generic file names or roles. Live ready-PR runtime
transport/producer fan-out/fan-in/lifecycle and native immutable fixtures remain open, as do
Linux/hosted/workflow/canary and every previously recorded migration requirement. No status,
consumer, settings, commit/push/PR/tag/Release activation occurred. The full objective stays active.

### Fixed runtime-only bounded ZIP transport primitive (8 October)

Previous goal turn classification: progress (runtime byte/copy admission, eight physical-data
cases and terminal focused/full verification). Rechecked complete design and actual shared MB1
ZIP/transport/reader sources before continuing. Existing extraction deliberately refuses empty
files, so routing runtime logs through Build would violate the preserved native contract.
MB1 bounded_zip.extract_runtime now accepts only exact lane/complete scopes with central fixed
native file, total, per-entry and compressed bounds. Only this fixed route permits empty files;
existing Pages/Build signatures, ceilings and nonempty-file semantics stay unchanged.

Runtime compressed cap is 512 MiB for bytes and Path sources before ZipFile allocation. Bound
central-directory bytes/entries first and non-directory file count before any inflation. Retain
all shared unsafe name/case/prefix/type/encryption/method/overlap/ratio/directory-parent checks
and actual streamed CRC/declared/total-size checks. Scope budgets add exactly one bounded outer
envelope, never widen native role or payload caps. Every runtime caller must subsequently bind
exact canonical envelope/complete bytes, original actual API/seals and independent native roles.
This primitive alone never proves native E2E validity or artifact provenance.

Eight new tests run actual hostile ZIP admission, stored/deflated reads, streamed CRC/size
verification and physical temporary writes. Windows opening/sealing/publication substitutions
establish no Linux no-follow/UID/atomic guarantee. Cases cover empty log preservation, old
Pages/Build empty-file rejection, invalid scope/compressed bytes/Path refusal before parser
allocation, central directory bounds before allocation, file cap before inflation separate from
legal directory parents, unsafe names/aliases/links/specials/duplicate/encrypted/method cases,
entry/whole/ratio caps before inflation, and CRC failure with unpublished-stage cleanup.
Windows ZipInfo normalized a hostile backslash fixture; patch actual stored name bytes instead.
Initial temporary stage rename hit WinError 5, then an older newly-authored runtime-copy fixture
hit the same host race in Python 3.12. Both explicit test-only publication seams now copy their
completed known stage into a fresh output; production exclusive atomic primitives are unchanged.
Do not interpret that substitute as real atomic proof or hide the initial failed observations.

Final 708 distinct focused cases pass independently on Python 3.11/3.12/3.13. External compile,
locks, digest, whitespace and no repository bytecode checks pass. Staged source digest:
sha256:de74f16f74076ea32ed162256f31475ca859be190aa8f030cfab70d22254d68f.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1,804 tests,
2,028 discovered, with exactly the same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). No extra intermittent image/cache/runtime-fixture failure
appeared in these final full runs. Logs in the host temporary directory:
mod-base-build-e2e-runtime-zip-py311.log and -py313.log. All test/check processes are terminal.
These results do not establish Linux/hosted/native proof.

Actual runtime numeric-ID API downloading, original full coherent pair/source/attempt/upload
admission and final revalidation inside byte publication remain open. Native QS/BP fixtures,
complete mapping/validators, Linux/hosted/workflow/canary and all original migration requirements
are not completed. No consumer, status/settings, commit/push/PR/tag/Release activation occurred.
The original whole K1?K7/Q1?Q10/B1?B7 objective remains active.

### Exact runtime frozen bytes and private independent copy (8 October)

Previous goal turn classification: progress (initial runtime schema, nine cases, three-version
focused verification and terminal full suites with exact baseline failed-name equality).
Rechecked current schema, MB1 regular-data readers/copying, Build materializer and design.
New MB11 runtime_exports uses existing MB1 no-follow regular-data inspection so empty runtime
logs survive; Build's nonempty-file contract is unchanged. Bound complete logical tree closure
before any content reads. Require canonical ci-runtime-envelope.json, complete exact actual
size/SHA-256 records including its own envelope, and reread the original envelope. The new
entry bound is derived from existing 4,096-file/path-depth caps, expanding no native file/byte
limit. Per-scope tree budget adds only outer-envelope overhead to native payload limits.

bind_runtime_envelope separately requires independently authenticated original runtime and
whole owning Build descriptors, exact producer/attempt/plan/profile and runtime-lane/results
scope. Metadata is not actual API provenance. materialize_runtime_export makes an independent
regular-data copy via MB1 exclusive atomic publication, verifies copied inventory, and repeatedly
reads stage and original source. A private closing transport callback is followed by complete
stage/source revalidation; no public callback grants admission. Original private ancestry/output
parent and writer exclusion remain independent caller requirements. No native code executes.

Eight new tests use physically written/read/copied/hashed authored opaque report/log bytes with
explicit Windows MB1/atomic syscall substitutions. Actual canonical/schema/binding/byte inventory
comparisons and materializer logic run. Cover empty logs, original source/copy bytes, missing/
extra/same-size hash/size tampering, noncanonical/duplicate/unknown/plan rejection before payload
inspection, pre-content entry-bound rejection, envelope reread, scope budgets including only
outer overhead, late source/stage mutation rejecting without publication, preserved existing
output, and exact original descriptor/owning Build provenance comparisons. These seams prove no
Linux UID, no-follow, atomic filesystem, source lifetime, native report/JAR or runtime validity.

700 distinct focused cases pass independently on Python 3.11/3.12/3.13. External compilation,
locks, digest, whitespace and no repository bytecode checks pass. Source digest after staging
only this turn's changed source inputs:
sha256:3135e7bf7367d27a1f935dcbc9612ed44dd1845e8d5184ef9f783b748d433900.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1,796 tests,
2,020 discovered, with exactly the same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). No additional intermittent image/cache failure appeared.
Logs in the host temporary directory: mod-base-build-e2e-runtime-exports-py311.log and -py313.log.
All test/check processes are terminal; Windows results establish no real Linux/hosted proof.

Actual runtime API numeric-ID/ZIP transport and coherent original pair integration remain open.
Source inspection confirms existing _download_export is Build-specific and uses Build ZIP/output
budgets; do not route runtime bytes through that reader or accept widened native payload limits.
Original/current native policy/pin, native mapping/validators, newest eligible source chronology,
Linux/hosted/workflows/reuse/authority and all complete migration gates remain open. No consumer
activation, setting, commit, push, tag, Release or PR operation occurred. The original full
K1?K7/Q1?Q10/B1?B7 objective remains active.

### Closed runtime inventory and preserved scope bounds (8 October)

Previous goal turn classification: no progress (separate Cursor Profile lookup, no Build/E2E
implementation). Revalidated the unfinished runtime_schema source and complete design before
continuing. The initial mod-base.ci.runtime-envelope v1 kind is now wired to strict document
loading, central grammar/bounds, compatibility ledger and a current writer fixture. The immutable
predecessor rejects this genuinely new kind; all existing unchanged-kind writers remain covered.
No released schema/interface is changed. runtime_schema owns the closed inventory in MB11.

Rechecked original immutable QS/BP e2e/packaged_runtime.py and BP scripts/ci/e2e_fanin.py without
executing consumer code. Each lane retains 512 files/256 MiB, including inside a complete export.
Whole complete export preserves BP's 4,096 files/512 MiB; envelope 4 MiB; reports 4 MiB,
logs/crash 16 MiB and screenshots 32 MiB. Empty log/crash files remain permissible inventory
records; reports/images are nonempty. Role labels never independently establish native mapping.
The common complete envelope is proposed composition, not original QS aggregate conformance.

Closed identity/producer/plan/profile/owning complete Build bindings, ordered canonical paths,
case/prefix collisions, reserved envelopes/Git internals, lane coverage, scope selection and
complete ordered plan contract checks are covered by nine new structural/decoding tests.
Oversized collections reject before nested file validation. Aggregate-only null-lane files do
not substitute for actual lane coverage. Tests explicitly do not prove native outputs or bytes.
Initial fixtures lacked the second lane's required Build outputs and incorrectly expected
structural API head authentication; corrected the fixtures rather than relaxing the existing
plan or provenance boundary. A first broader run found pretty fixture encoding mismatch;
corrected only the new fixture to the repository writer format. All 692 distinct focused cases
then pass independently on Python 3.11/3.12/3.13, including every old/common predecessor case.

Source digest after staging only this turn's changed source files:
sha256:f00c8f5f955b193ddcc7c129d7388afa8318905be06b094d776efab585064b24.
External compilation, staged locks, digest, both whitespace and no repository bytecode checks
pass. Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed
1,788 tests, 2,012 discovered, with exactly the same 544 baseline failed names, none added or
removed (122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-runtime-envelope-py311.log and -py313.log. No additional intermittent failure
occurred. All check/test processes are terminal. Windows data/fixture checks establish no real
Linux UID/no-follow/writer-exclusion or hosted workflow/native runtime success.
No consumer files, GitHub settings, commit, push, tag, release or activation were performed.

Runtime byte transport/materialization, actual native mapping/validators and immutable native
fixture parity remain open, as do all previously recorded Linux/hosted/workflow/authority and
migration gates. Plan/descriptor labels and structural success confer no execution or native
success authority. The complete K1?K7/Q1?Q10/B1?B7 objective remains active and uncompleted.

### Exact original complete Build bytes after merge (8 October)

Previous goal turn classification: progress (coherent original pair reader, eight API cases and
terminal three-version focused/two-version full baseline verification). Current complete payload
transport, export byte verification and atomic materializer were rechecked before implementation.
download_merged_build shares bounded pair argument/snapshot preflight and retains an actual
historical source before the initial coherent pair. Select only that pair's exact complete bundle.
The existing complete export transport now accepts only an internal source admission callback;
live public routes retain their signatures and live requirements. Keep original numeric artifact,
producer/controller/kit/attempt/graph/upload, ZIP size/hash, canonical envelope/inventory/scope
and actual full byte verification. Inside the existing atomic copy's final before-publish hook,
reread both coherent full seals, compare canonical fingerprints, close source and caller snapshots.
Reverify staged bytes after the callback, then publish. Missing/moved/corrupt evidence rejects
without an output. No public callback, execution selector, new schema/bound or candidate code is
introduced. Original private writer-excluded parent and lifetime require independent admission.

Seven tests execute actual historical/pair/transport/envelope/inventory/materializer logic with
newly authored opaque payloads and explicit Windows filesystem seams. The fixtures physically
write/read/copy/hash their known bytes; no native compiler/JAR correctness is claimed. Cases cover
original complete bytes/envelope, nine numeric reads (eight seals plus one bundle), preexisting/
non-path output before API, corrupt ZIP before extraction, source/staged hash tampering, either
seal's expiry inside copying, historical/caller movement inside copying, and staged corruption
during the final pair read caught by post-callback actual byte verification. The syscall seams
replace no-follow extraction/traversal/copy/atomic FD operations with known regular test-file IO;
they establish no Linux UID, no-follow, writer exclusion, Root provenance or hostile ZIP safety.
Twenty-nine direct new/API cases pass; previous transport/latest/pair cases passed during refactor.

Source digest after staging only this turn's changed transport.py input:
sha256:9bdd92bc3fa78f8bea191d3427ff6274a5a060120208a634700a550155b27b5f.
683 distinct focused cases pass separately on Python 3.11/3.12/3.13. External compilation,
staged locks, generated digest, both whitespace checks and no repository bytecode checks pass.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1779 tests,
2003 discovered, with the exact same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-merged-build-py311.log and -py313.log. No extra intermittent image/cache failure
appeared in these runs; prior observations remain recorded. All test/check processes are
terminal. These results do not establish Linux/hosted proof.

K6 still requires complete runtime native payload transport, native Build/runtime/JDK/package
validation, newest eligible sources, independently derived original/current protected policy and
executing pin equivalence, actual later consumer chronology and reuse graph/writer integration.
Generic authenticated/copied bytes never authorize native success, statuses or settlement.
Source review confirms runtime lane plans retain lane/target/native-contract/obligation identity
but have no generic runtime file envelope/materializer yet. Complete runtime transport needs
bounded native-profile file interpretation and independent byte validation without authoring a
second scenario/action inventory; existing gate descriptor metadata is insufficient.
Inert reads of the immutable original workflows identify concrete downstream bindings: Quick
Skin exports per-lane packaged evidence and tested-source-e2e; Block Pops' fan-in compares
aggregate.artifact_manifest.sha256 with actual build/release/artifacts.json before its aggregate
handoff. The runtime/native reader must preserve that real bundle-manifest coupling, not infer
native success from this generic byte envelope. No consumer checkout, script or workflow ran.
K5 safe construction/immutable manifest/settlement, all K/Q/B gates and real Linux/hosted/native
conformance remain open. No commit, push, PR, release, consumer/settings/activation, candidate/Git
execution or account/API effect occurred; existing user/staged work was preserved.

### Coherent original Build/packaged seal pair (8 October)

Previous goal turn classification: progress (original full historical gate reader with ten API
cases and terminal three-version focused/two-version full baseline verification). Current pair
requirements, closed gate model, historical transport and source observations were rechecked.
download_merged_gate_pair preflights original native plan/workflows and independent complete
tested descriptors, distinct run/artifact IDs, current controller/final SHA before API reads.
Bounded original plan and both descriptor snapshots plus a retained actual MergedPr bracket the
whole pair. Both existing historical readers execute actual complete admission. Require packaged's
entire immutable owning Build descriptor equal the Build seal's sole actual complete bundle,
not just matching PR/plan/tree labels. Read both records again through complete transport,
compare bounded canonical fingerprints and close historical source/original caller bindings.
Return both original records only after success; any missing/error/corrupt/moved input rejects.

Eight new tests execute actual historical readers/API/record/timeline admission with explicit
Windows extract/read seams. The authored pair fixture sequences the Build gate before packaged
verification. Cases cover four bounded numeric ZIP reads, original records and no residue,
pre-API malformed/mixed/workflow/scalar rejection, independently valid packaged proof consuming
another actual fake-API Build run, earlier Build seal expiry during packaged and later packaged
expiry during second Build read, whole-pair historical/caller movement and later download error
without returning partial success. The historical inter-reader timing case wraps the actual
individual reader before changing timestamp; it is explicitly a timing seam, not a fake success.
An initial later-error test bypassed its filesystem download seam, leaving no raw test record;
inject the error through that seam instead. Forty direct pair/single/API cases now pass.
No native payload byte validity, physical private-root/Linux or hosted behavior is established.

Source digest after staging only this turn's changed transport.py input:
sha256:5c2278556e0092ddf8d60eb5d30dd6f1b3c25c3488ffa0f20fb73fc143d02fc4.
676 distinct focused cases pass separately on Python 3.11/3.12/3.13. External compilation,
staged locks, generated digest, both whitespace checks and no repository bytecode checks pass.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1772 tests,
1996 discovered, with the exact same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-merged-pair-py311.log and -py313.log. No extra intermittent image/cache failure
appeared in these runs; prior observations remain recorded. All test/check processes are
terminal. These results do not establish Linux/hosted proof.

The current complete Build payload reader still calls the live _authenticate path before and
after extraction. It cannot directly materialize this historical pair's bundle after merge;
that route needs the same retained historical source and final before-publish admission while
preserving actual byte/envelope/scope/atomic-copy checks. Runtime native payload transport also
remains required. No source byte validity is inferred from this pair's metadata/record success.

Remaining K6 admission includes complete native Build/runtime source bytes and semantics,
newest eligible runs, original/current protected policy and executing pin equivalence, actual
later protected consumer chronology and complete reuse graph/writer. Observation reserves no
artifact; future consumers/effects must reauthenticate. Genuine unavailable proof requires a
fresh coherent full generation, while corruption/API errors remain fatal; no recovery/status
or reuse effect is implemented by this reader. K5 construction/immutable manifest/settlement,
all K/Q/B gates and real Linux/hosted/native conformance remain open. No commit, push, PR,
release, consumer/settings/activation, candidate/Git execution or account/API effect occurred.

### Original full gate transport after merge (8 October)

Previous goal turn classification: progress (separate historical merged identity admission,
nine actual API cases and completed three-version focused/two-version full baseline checks).
Current live transport, original run/graph/timeline and owning Build paths were rechecked.
download_merged_gate_receipt adds a separate original PR reader with independently expected
current controller/final SHA and bounded original plan/descriptor snapshots. The existing live
public signature remains unchanged and still rejects a merged PR. A shared private full-record
path uses only fixed internal live/historical source admission; no public callback/data selector
exists. Retain the first MergedPr and compare every later source admission, including owning
Build and closing reads, so a stable but substituted merged timestamp between reads rejects.

Keep actual original producer controller/head, kit, completed run and latest attempt, exact
full graph, seal/upload and numeric selected artifact metadata. Download the exact bounded ZIP
and digest, extract only the original fixed canonical gate JSON, bind actual API chronology and
every source artifact's availability, independently authenticate packaged's owning Build with
its enrolled workflow/full graph/latest attempt, then repeat all record/input/source admission.
Caller original plan/descriptor mutation rejects after bounded closing validation; original
producer identity is not rewritten to the later controller. No partial/reuse/deferred route,
effect, code/Git execution or new schema/bound is introduced.

Ten new cases execute actual historical and shared full API/read logic with explicit Windows
extract/read filesystem seams. They cover both full original gates and original producer/numeric
identity, live-path closed refusal, pre-API scalar/kind/workflow rejection, wrong historical
source/tree/controller, latest attempt/head/success/kit/full graph, seal and all source expiry/
digest/owner, owning Build's independent attempt/enrollment, actual timeline and canonical/
duplicate/unknown JSON, ZIP corruption before extraction, post-download historical/controller/
attempt/source/caller drift and API/extraction rejection. These tests do not validate native
payload bytes or physical Linux/private-root/hosted behavior. Initial test iteration corrected
cleanup assertions placed after deleting the test temporary parent, and changed the imported
TestCase to a module alias to avoid duplicated discovery. 44 direct transport/API cases pass.

Source digest after staging only this turn's changed transport.py input:
sha256:424d9a3e32e29aa9af1234d1c229ffa7270927b818f4a9335060018d391206a7.
668 distinct focused cases pass separately on Python 3.11/3.12/3.13. External compilation,
staged locks, generated digest, both whitespace checks and no repository bytecode checks pass.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1764 tests,
1988 discovered, with the exact same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-merged-gate-py311.log and -py313.log. No extra intermittent image/cache failure
appeared in these runs; earlier observations remain recorded. All test/check processes are
terminal. These results do not establish Linux/hosted proof.

Both coherent original Build/runtime seals and source bundles, newest eligible run selection,
all native payload bytes/semantics, original/current policy/pin and actual later protected
consumer chronology remain independently required.

The closed gate model already requires Build to retain exactly one complete bundle and packaged
to retain a separate owning Build. The next paired reader must compare that whole immutable
owning descriptor with the Build seal's actual bundle, rather than accepting two independently
valid seals that happen to share a source identity but consumed different Build generations.
Missing-source coherent full recovery versus
authenticated corruption must retain the design's distinct behavior; this reader itself never
converts either to success or silently falls back. Batch immutable manifest/member admission,
safe construction/writer integration and settlement are still open. All K/Q/B gates remain open;
no commit, push, PR, release, consumer/settings/activation or account/API effect occurred.

### Historical merged PR identity observations (8 October)

Previous goal turn classification: progress (terminal three-version focused verification and
two full-suite baseline comparisons, plus the discovered live-only historical-gate dependency).
Current worktree, original live admission and gate reader were rechecked before implementation.
authenticate_merged_pr_identity is a separate read-only historical path: original independently
admitted PR identity, expected final merged SHA and current protected controller are distinct.
Require the exact closed/merged/ready same-repository PR and original source head/ref/base ref,
bounded real UTC merged_at, exact original synthetic commit/tree/ordered base-head parents,
equal complete final tree and original-base-to-final plus final-to-current protected ancestry.
Final merge/squash/rebase parents may differ, while duplicate/self/malformed parent objects reject.
Post-merge API base SHA may advance and never replaces the original tested parent. If final SHA
is current, branch and final Git object trees must agree. Repeat original/final object, ancestry
and PR reads; close default/controller/PR state and reject original caller identity substitution
after bounded revalidation. Return a frozen MergedPr observation, never gate or effect approval.

Nine tests execute actual inert fake-API logic without mocking the historical verifier. Cases
cover final one/two-parent shapes, a final SHA equal to the original synthetic merge, current or
later protected heads, advanced API base SHA, frozen identity digest, live-path closed refusal,
independent argument rejection before API, unmerged/draft/fork/malformed/moved PR data, wrong
original/final tree/ordered parents/objects, both ancestry requirements, branch tree inconsistency,
late original/final object and PR/controller/default movement, API failure propagation and
caller mutation. An encoder guard proves oversized closing caller data rejects before encoding.
An initial direct import exposed a jobs/adapter/model/controller cycle; use the already-central
grammar.parse_timestamp instead, with no new timestamp grammar or lazy import workaround.
The default-movement test's shared event counter was corrected before validation. Initial
658-case focused runs on all three versions failed one new fixture setup: FakeGitHub correctly
refused reseeding the same commit with a different tree. The hostile branch-tree case now uses
an explicit malformed branch response seam instead of violating the fake's object invariant.
The nine-case direct rerun passes; all three complete focused reruns pass 658 tests. No physical
Linux, full-gate, native policy, original root or hosted API outcome is established by these tests.

Primary GitHub pull-request documentation was checked: merge_commit_sha changes after merge to
the final merge, squash or rebased default-head commit. Preserve the original test identity:
https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request.
Source digest after staging only this turn's changed authenticate.py input:
sha256:704cdd26dd73840aa488548aa21cefdea02d295830ee107947356a0b8d957237.
658 distinct focused cases pass separately on Python 3.11/3.12/3.13. External compilation,
staged locks, generated digest, both whitespace checks and no repository bytecode checks pass.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1754 tests,
1978 discovered, with the exact same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-merged-pr-py311.log and -py313.log. No additional intermittent image/cache
failure appeared in these runs; prior observations remain recorded. All test/check processes
are terminal. These results do not establish Linux/hosted proof.

Historical full gate transport/selection, immutable batch manifest/member source-byte admission,
original/current policy and executing pin equivalence, source seals and latest attempts are still
required. The existing live gate reader stays unchanged and cannot simply admit closed PRs.
Source review confirms authenticate_gate_timeline constrains internal prerequisite/verifier/
upload chronology, but does not bind a later historical consumer's execution time. The reuse
route must independently prove complete source seals before the actual protected reuse verifier,
with separate latest-attempt and availability/corruption admission. Pre-merge live authorization
is a separate writer obligation; this source observation does not retroactively establish it.
No blanket pre-merged_at constraint is added to reuse beyond the design's original source and
actual consumer chronology requirements.
No reuse, status, settlement, PR/branch creation, member closure, candidate/Git execution or
account/API mutation is added. Safe actual construction/writer integration, Linux/hosted/native
conformance and all K/Q/B gates remain open. No commit, push, release, consumer activation or
settings effect occurred; existing user/staged work was preserved.

### Ready batch PR and synthetic merge binding (8 October)

Previous goal implementation turn classification: progress (ready batch PR/source/publication
binding, eight focused cases and API/protocol documentation). Its three retained focused-test
handles were missing on this continuation, so no success was inferred from the lost output.
Fresh executions are terminal: 649 distinct focused cases pass on each Python 3.11/3.12/3.13.
The intervening Cursor Profile lookup made no progress on this Build/E2E objective.

authenticate_batch_pr requires an independently supplied closed identity and bounded manifest/
native/root preflight before API IO. A batch PR must be distinct from all source members, open,
ready and same-repository. Its protected base/controller, branch/head, policy and tested tree
must match the manifest. Existing genuine PR admission verifies the actual synthetic merge SHA,
complete tree and exact ordered base/head parents before and after the complete publication/
source verifier. Final whole-source membership and batch-generation reads detect changes during
the closing observations. Original caller identity/manifest substitution rejects after bounded
revalidation. BatchPr retains observations and the canonical identity digest only.

Eight tests cover ordered ready+draft source members with a ready batch, identity mismatches
before IO, draft/closed/fork/wrong head/base/missing merge, actual synthetic tree/parent mismatch,
batch movement during source reads, source movement during the closing merge read, batch movement
during closing membership and original caller substitution. Actual inert fake-API and source
verification run with explicit authored source_records filesystem seams. One final-generation
case also wraps the actual membership function to place a mutation after its return; this is an
explicit timing seam, not physical transport/Linux evidence. Complete roots remain four reads
each. No candidate/Git execution, API/ref/PR/account mutation or status authority is added.

External compilation, staged locks, generated digest, both whitespace checks and no repository
bytecode checks pass. Current source digest:
sha256:59cc4d3ef3888a63b8b67b1a9074ac135d5f53698e8c3f9a391de073ecec9b90.
Both full Windows Python 3.11/3.13 suites are terminal and failed: each executed 1745 tests,
1969 discovered, with exactly the same 544 baseline failed names, none added or removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-batch-pr-py311.log and -py313.log. No additional intermittent image/cache
failure appeared in these runs; earlier observations remain recorded. All test/check processes
are terminal. These results do not establish Linux/hosted proof.

K5 still requires original safe Git/runtime enrollment, native ordinary-path admission, actual
local patch application and fixed-bot construction, completed new-branch push provenance,
protected writer integration, full batch gates and immutable merged settlement with explicit
member-closing authorization. Identity kit/workflow/inventory/scenario/graph shape does not
establish independent original pin/native-plan approval. All K/Q/B gates remain open. No commit,
push, PR, release, consumer activation or settings effect occurred.

The immutable Quick Skin constructor/settle source was read again without checkout/execution:
its legacy settlement requires merged/merged_at and compares each current member head before
closing. That is weaker than the new complete merged-gate requirement and is not adopted here.
The current download_gate_receipt path calls _authenticate_run, which admits a live source via
authenticate_source_identity; its PR path requires open/ready PR admission. Consequently it
cannot simply be reused after batch merge. Immutable merged provenance needs a separate exact
historical source/gate path preserving the original synthetic parents and both complete gate
proofs, rather than weakening current live admission to accept closed PRs.

### Published batch ref/source observations (8 October)

Previous goal turn classification: progress (exact empty-branch observations, genuine local Git
same-SHA no-op discovery, strict new-branch receipt and completed focused/full verification).
Current source/API/docs were rechecked before continuing K5. authenticate_batch_publication
adds shared bounded native/manifest/root argument preflight, keeping verify_batch_manifest_sources'
public signature and behavior. The caller's independent expected branch/final squash SHA must
match the snapshotted manifest before API IO. Exact ref responses must name that full ref and
a commit object with that SHA; partial aliases/tags/malformed data and missing/permission/
transport/rate failures reject. Independent commit-tree reads bind the final manifest tree.

Ref reads bracket the actual existing complete source-byte and squash graph verifier, including
its repeated complete inventories/patch/member admission. Full closing member/controller reads
after the last ref read reject readiness/source movement during that read. Caller document
changes reject after bounded shape/cap revalidation. BatchPublication retains source digest/
observations plus current branch/commit/tree only. No additional complete-root byte pass or
graph accumulation is introduced; each complete source root is read four times by the existing
verifier. Native policy, original private writer-excluded root lifetimes and genuine API/caller
provenance remain independently mandatory. Constructors and current ref existence are not proof
of who created the name or permission to create a PR, emit statuses or settle members.

Eight cases execute actual inert fake-API/source-verification logic with explicit authored
source_records filesystem seams. They cover single/ordered ready+draft members, exact ref and
final commit/tree with repeated complete bytes, independent branch/commit/policy/root preflight
before IO, partial/tag/wrong/malformed refs, missing/permission/network/rate errors, wrong actual
tree/parents, ref movement after bytes, member movement inside the last ref read and caller
manifest substitution. These seams prove no physical Linux/no-follow/Root lifetime or hosted
publication. 641 distinct focused cases pass separately on Python 3.11/3.12/3.13. Source digest:
sha256:0cf53f88b04871998964d58391cd6d98db3e80f3e3ba647101250853f66dd214.
Both full Windows suites are terminal and failed: Python 3.11/3.13 each executed 1737 tests,
1961 discovered, with the same exact 544 baseline failed names, none added/removed
(122 failures/422 errors/67 skips). Logs in the host temporary directory:
mod-base-build-e2e-batch-publication-py311.log and -py313.log. No extra intermittent image/cache
failure appeared; earlier observations remain recorded in their own stages. External compilation,
staged locks, generated digest, both whitespace checks and no repository bytecode pass. Only
this turn's modified src batch input was staged for index-based digest generation; existing
user/staged work was preserved. No commit, push, release or consumer/settings effect occurred.
All test/check processes are terminal. These observations do not establish Linux/hosted proof.

Remaining K5 requirements include independently enrolled original safe Git/runtime and native
approval, local patch/application and fixed-bot commit-tree construction, actual completed
empty-before/new-branch publication provenance and protected writer integration, live owner/
writer rechecks around effects, batch PR/full-gate provenance and immutable settlement.
No Git/candidate execution or ref/API/account/PR mutation is added by this library. No commit,
push, release, consumer activation or settings action occurred. K5 and all K/Q/B gates remain
open; real Linux/hosted/native pipeline evidence is still unverified.

### Exact empty batch branch observations (8 October)

Previous goal turn classification: progress (manifest source/byte/actual API squash graph binding,
bounded closing mutation guard and completed focused/full verification). Current API/error,
source, design and immutable original Quick Skin batch code were rechecked before continuing K5.
observe_batch_branch_lease admits current ordered members/controller, observes exact branch
absence twice around complete collection reads and closes with another full member/controller
admission after the final ref read. A readiness/head/controller change during either absence
read rejects. Only matching GET/path ApiNotFound 404 is absence after successful source/controller
access; generic/mismatched 404, permission/network/rate errors and existing/malformed 200 reject.
recheck_batch_branch_lease uses independent original repository/branch/controller/member order,
rejects substituted or malformed receipt binding before API IO, and observes current state again.
Constructors and observed absence grant no native approval or atomic name reservation.

The publication-specific is_batch_branch check preserves generic BRANCH document grammar and
requires nonempty batch/* within its original 200-character bound, rejecting dot-started and
.lock-ended components and final dot/slash. empty_batch_branch_lease emits only the explicit
--force-with-lease=refs/heads/<branch>: argument. Actual protected publication must use the empty
expectation at its final update. API observations cannot prevent an intervening actor creating
the name, and a later source move still requires independent privileged writer admission.
The original immutable Quick Skin script uses that same explicit empty lease; its old git commit
route is not the new design's fixed bot/protected commit-tree construction and was not executed.

Eleven tests use actual inert fake-API membership admission with an explicit exact-ref transport
seam, covering ordered absence/recheck, invalid branches/members before API, exact error scope,
existing/malformed responses, branch appearance, early/late source/controller/readiness drift
and substituted/stale receipts. A real locally installed Git test uses only newly authored
temporary bare repos as the local test user: accepted/refused names match check-ref-format,
first empty-expect creation succeeds, a fast-forward to an existing name is refused, and a
conflicting parent-ref namespace also refuses without changing the original ref. An actual
identical-SHA repeat returns exit 0 with = / [up to date] even under empty expectation; it does
not update/create the ref. This was observed independently in a newly authored temporary Git
experiment and then retained in the real Git test, rather than inferred from an exit-code mock.
validate_batch_push_receipt therefore requires a bounded 4 KiB ASCII machine receipt with
exact original destination/source/ref new-branch (*) record, successful integer exit and Done
trailer. LF/CRLF are accepted; invalid controls/encoding, missing/extra/unknown rows, nonzero/
noninteger exit and up-to-date success reject. A supplied byte string/exit code proves neither
the completed protected original command nor its safe remote/program/runtime provenance.
These tests
do not prove protected Git/runtime enrollment, Root/UID lifecycle, hosted refs or production
writer integration. 633 focused tests pass separately on Python 3.11/3.12/3.13. Source digest:
sha256:049c1b327936c47a6ed797a92280835ac43104efd44f37f6f4d7a917e915eb3e.
Before the new-branch receipt guard, both terminal full Windows suites (3.11/3.13) executed
1728 tests/1952 discovered with exactly 544 baseline failed names, none added/removed
(122 failures/422 errors/67 skips). Initial logs: mod-base-build-e2e-batch-lease-py311.log
and -py313.log. Earlier source digest was
sha256:778070ddb6c9e12b053af5c321c0e54af7bc0f71b59eb7dc09d419f4a651ab39.
Final source verification is terminal: both Windows 3.11/3.13 suites executed 1729 tests,
1953 discovered, with exactly the same 544 baseline failed names, none added/removed
(122 failures/422 errors/67 skips). Final logs: mod-base-build-e2e-batch-lease-receipt-py311.log
and -py313.log. These are failed Windows gates, not Linux/hosted or native writer evidence.
No extra intermittent image/cache failure appeared in either full stage; previous observations
remain recorded in their own stages. External compileall, staged locks, generated digest,
both whitespace checks and no repository bytecode pass. Only this turn's modified src batch,
grammar and limits inputs were staged for index-based digest generation; existing staged/user
work was preserved. No commit/push/release/consumer/settings mutation occurred. All test and
independent Git-observation processes are terminal.

Primary semantics were checked against [Git push](https://git-scm.com/docs/git-push),
[Git ref format](https://git-scm.com/docs/git-check-ref-format), and
[GitHub Get a reference](https://docs.github.com/en/rest/git/refs#get-a-reference).
Safe independently enrolled original Git/runtime, local patch/application/result proof,
fixed-bot commit-tree writer, actual empty-expect publication, native policy/current-head
approval, merged full-gate provenance and immutable settlement remain required. K5 and all
K/Q/B gates stay open. No library Git/candidate execution or ref/API/account effect was added;
only the controlled test fixture executes local Git. No production push/commit/release/consumer
or settings action was performed.

### Batch manifest source and graph observations (8 October)

Previous goal turn classification: progress (new closed manifest source/fixtures/docs and
completed three-version focused/full-suite verification). Current files and tests were read
before continuing K5. verify_batch_manifest_sources now snapshots the bounded closed document,
binds repository/controller/base/ref/native profile/policy, and authenticates complete ordered
live membership. Every member source/head/tree/draft, merge-base, patch and complete-source
byte fingerprint must equal actual repeated API/source observations. Caller manifest changes
reject. Native profile/policy and original private writer-excluded root provenance must be
independently admitted; data or constructed receipts do not grant that approval.

Each actual API squash object must have the declared commit/tree and exact single predecessor
parent. Complete result trees pass exact inventory admission; comparison hashes stream one
inventory at a time instead of retaining all members' whole trees. The full source/graph pass
runs twice, with complete live membership rechecked after graph inspection. Retained receipt
contains canonical manifest SHA-256 and ordered BatchPatchBytes observations only.

Nine tests use actual inert fake-API/source-verifier logic with explicit authored source_records
filesystem seams. Cases cover ordered draft/ready members, four reads per complete root,
preflight before API IO, valid-shaped forged source/base/patch/byte claims, actual wrong commit/
tree/zero-or-multiple-or-wrong parent, result inventory drift, earlier member change during later
source inspection, caller mutation and unchanged source tampering in the second pass. The final
guard repeats bounded closed-shape validation before re-encoding the caller document; a new
oversized-mutation test denies reaching the encoder rather than allocating mutated huge data.
622 distinct focused tests pass separately on Python 3.11/3.12/3.13.
Current staged source digest is
sha256:ec1ce48fbad2d861f171789bdac70a21b2645ec0d4acca0993fa7862a7f3a026.
Before that final guard, 621 focused tests passed on all three versions and full Windows
3.11/3.13 suites each executed 1717 tests/1941 discovered, with exactly 544 baseline failed
names (122 failures/422 errors/67 skips), none added/removed. Those terminal logs remain:
mod-base-build-e2e-batch-manifest-sources-py311.log and -py313.log. Final source verification
is terminal: both full Windows 3.11/3.13 suites executed 1718 tests/1942 discovered with the
same exact 544 baseline failed names, none added/removed (122 failures/422 errors/67 skips).
Logs: mod-base-build-e2e-batch-manifest-sources-final-py311.log and -py313.log. These are failed
Windows gates, not Linux or hosted evidence. No extra intermittent image/cache failure appeared
in either stage; previous observations remain recorded in their own stages. Final external
compilation, staged locks, digest, both whitespace checks and no repository bytecode pass.
Only this turn's changed src batch module was staged to bind the generated digest. No commit,
push, release, consumer or GitHub settings mutation occurred. All processes are terminal.

This does not verify local three-way patch application, independently enrolled original safe
Git/runtime, fixed-bot commit-tree construction, empty branch leases, live privileged writer
rechecks, immutable merged complete-gate provenance or settlement. Those implementations and
real Linux/hosted/native/consumer integration remain required. No Git command, candidate code,
ref/API/account mutation, consumer activation, release or completed K5 is claimed. Every K/Q/B
gate remains open. Read-only Get-Command inspection found no docker/podman executable on this
PATH; ssh/wsl exist. This proves neither absence of installations elsewhere nor an available
remote Linux runner. The mandatory Linux integration module explicitly requires a fresh
GitHub-hosted runner; its environment requirement was preserved and not simulated locally.

### Closed batch manifest (8 October)

Previous turn classification for this goal: no progress; it answered the separate Cursor Profile
request and removed an unverified draft patch. Current authoritative source was rechecked before
continuing K5. New mod-base.ci.batch v1 now retains base SHA/tree, repository/profile, batch branch,
native policy fingerprint, ordered member source/head/tree/draft and merge-base identities,
complete-source byte fingerprints, patch before/after identities, parent/squash/result chain and
final result. Parents must match the ordered predecessor, squash commits are distinct from the
base/each other, results cannot be no-ops and the final tree must agree. No live/Git authority is
inferred from those structural equalities or caller-authored hashes.

Closed schema/64 MiB decoder and progressive whole-patch/path caps reject unsupported selectors,
malformed modes/link sizes, inconsistent blob sizes, duplicate/fork/nested members and invalid
paths/order/case aliases. Initial current 1/previous null is an explicit new-kind decision;
the archived v1.0.3 reader rejects it while existing kinds remain unchanged. Valid and invalid
fixtures, schema/ADR/API/protocol docs and eight focused manifest tests cover these data checks.
613 distinct focused tests pass separately on Python 3.11, 3.12 and 3.13.

Remaining K5 requirements include an independently approved original native-policy/caller,
genuine retained private source/API/byte observations, sanitized protected Git construction and
application, actual single-parent/result-tree verification, complete live membership and empty
branch leases, immutable merged full-gate settlement and production integration. K5 and every
K/Q/B gate remain open. Real Linux/hosted execution, consumers and status authority are unproven.
Current staged source digest is
sha256:e38ce19c9bdd4f610842f9d43794dc85ebfb9fdd2f78c1aeebc5b7059bafdb10.
Both full Windows suites are terminal and failed: Python 3.11/3.13 each executed 1709 tests,
1933 discovered, 122 failures/422 errors/67 skips. Exact failed-name sets equal the 544-name
untouched baseline with no added/removed cases. Logs are in the host temporary directory:
mod-base-build-e2e-batch-manifest-py311.log and -py313.log. No extra intermittent image/cache
failure appeared in these runs; previous observations remain recorded in their own stages.
External compileall over src/tests/tools/canary, staged locks, digest, both whitespace checks
and absence of repository bytecode pass. Only the changed/new src inputs were staged for
index-based digest generation; no commit, push, release or consumer mutation was performed.




### Complete batch source-byte inspection (8 October)

K5 verify_batch_patch_bytes now composes actual source.verify_source_copy with authenticated
complete merge-base/head inventories and derived patch identity. Both complete source copies
are inspected twice and their streamed canonical source-byte digests must agree. The complete
inventories and API patch/member admission are repeated, including after the final byte pass
so a member readiness/head change during that read cannot return stale observations.
BatchPatchBytes retains the patch and two internal mod-base.batch-source-bytes-v1 fingerprints:
canonical {format, tree_sha} then fixed {path, mode, size, git_blob, sha256} rows in path order.
Digest includes all unchanged source; constructors/hashes do not grant writer/root authority.

Six cases run actual fake-API admission and actual source verification logic with explicit
authored source_records syscall seams. They cover full source/tree digest and repeated copies,
changed/unchanged file tampering, retained-Git-metadata SHA-256 observation drift, member change
in the final read, inventory change during a read and read/malformed-byte-hash rejection. These
seams do not prove physical Linux/no-follow/root lifecycle or a real same-SHA1 byte collision.

Original private roots/excluded writers must be retained independently. Opaque Git metadata,
matching safe local Git graph/application, resulting trees, strict manifests, leases, settlement
and production integration remain required. No Git/candidate code, ref/account/API mutation,
consumer activation or completed K5 is claimed. 605 focused cases pass separately on Python
3.11/3.12/3.13. Current staged digest is
sha256:97ee9446282bd66ab0cff1e49df443bdb3d17a525b61ad2d0ae42e96e4519283.
Full Windows 3.11/3.13 suites each executed 1701 tests / 1925 discovered and retain exactly
the 544 baseline failed names, none added/removed (122 failures/422 errors/67 skips). External
logs: mod-base-build-e2e-batch-bytes-py311.log and -py313.log. External compilation, locks,
digest, both whitespace checks and no repository bytecode pass. No extra image/cache failure
occurred in these full runs; earlier intermittent observations remain recorded in their stages.
These results do not establish physical Linux/native writer evidence; all K/Q/B gates stay open.
Read-only local environment inspection also queried wsl --list --verbose; Windows reports no
installed WSL distributions for this user. No distribution/OS component was installed. This
does not establish availability of remote Linux runners or replace the required hosted native
boundary/canary evidence.

### API-bound batch merge-base and exact tree inventories (8 October)

K5 authenticate_batch_patch preflights native policy tuple shape, authenticates the live member,
reads the API-selected merge-base against the exact protected controller/head comparison,
independently binds its Git commit/tree and reachability in both histories, and derives changes
from complete exact source trees. Tree inventories, object/merge-base/ancestry metadata and the
live member are read again before BatchPatch observations return. Compare files and patch text
are ignored; GitHub documents their 300-file comparison limit. Protected native policy, actual
blob/patch bytes, matching safe local Git graph/application, result trees, manifests, writer
leases and settlement remain separate required work. No writer/status authority is added.

MB1 exact_tree exposes strict SHA tree transport without modifying the frozen legacy tree
signature/alias behavior. Source tree inventories and original-controller source/activation
readers use strict exact trees. MB11 read_source_tree_inventory factors existing bounded shape
and closure validation; live source authentication still brackets authenticate_source_inventory.
Directory prefixes are inferred only after the complete source cap passes, avoiding the prior
pre-validation inferred-set expansion. No source/copy signature, source/API cap or schema changed.

Nine inert API cases cover real complete-tree vs partial compare derivation, retained drafts,
malformed/wrong/unreachable objects, live member/tree/merge-base drift, unknown/native policy
rejection, preserved legacy alias with exact/source refusal, original-controller alias refusal,
incomplete/truncated closure and early source cap. Tests use authored fake API metadata and do
not establish blob bytes, physical Linux/protected caller/native admission or local Git results.
The controller activation fixture now reads its authored tree through the owning contents API,
rather than relying on the controller's incidental imported tree symbol. 599 focused cases pass
separately on Python 3.11/3.12/3.13. External compilation, locks, digest and whitespace checks
pass; no repository bytecode was emitted. Current staged digest is
sha256:43ecaca00d28b2083d9e378d4005d7918970ec472cb1be4285202907164381af.
Full Windows 3.11/3.13 suites each completed 1695 tests / 1919 discovered and retain exactly
the 544 baseline failed names, none added/removed (122 failures/422 errors/67 skips). External
logs: mod-base-build-e2e-batch-binding-py311.log and -py313.log. No extra image/cache publication
failure occurred in these runs; earlier intermittent results remain recorded in their stages.
These Windows results do not establish the mandatory Linux/native writer or local Git evidence.
All K/Q/B gates remain open; no consumer, release, Git/API mutation or candidate execution occurred.

### Complete-tree batch patch identities (8 October)

K5 derive_batch_patch_inventory validates both complete typed source inventories before deriving
canonical additions/deletions/content/mode changes under independently admitted native exact-path
policy. BatchPatchEntry retains both GitSourceEntry identities, with None for absent sides.
Renames remain explicit deletion/addition and both paths require admission; unchanged protected
paths are not patches. No-op, unknown path, malformed source/policy and same-blob conflicting
sizes reject. Policy ordering/uniqueness/case aliases, file count, prefix entry count and total
UTF-8 path bytes are bounded before accumulating additional policy state. Combined caps derive
from two existing source inventory caps without widening source/API/transport limits.

New public source.validate_source_inventory exposes complete typed shape checks without
changing source parsing/copy/authentication signatures or granting provenance. The shared
validator now rejects the existing root-inclusive prefix entry cap before another distinct prefix
is inserted; previously it gathered the entire closure before checking that cap. Eight pure cases
use authored known blob bytes for add/delete/modify/rename/mode/link identities, both rename
permissions, no-ops/unknown paths, hostile policy/caps, complete source shapes and cross-path
same-blob size contradictions. A small source-cap test proves rejection before validating later
paths, without creating a huge inventory. No physical API/native/merge-base/writer proof is inferred.

Genuine protected merge-base/head inventory binding, native mode/link/restricted-transition
policy, exact patch bytes, safe application/resulting trees, strict manifests, commit-tree
construction, leases and settlement remain incomplete. Controller-vs-head comparison alone
cannot stand in for the member's merge-base patch. This is not completed K5 or a batch writer.
Initial pre-source-cap verification had 589 focused cases passing on each supported Python.
Initial full Windows suites completed 1685 tests / 1909 discovered. 3.11 retained exactly the
544 baseline names; 3.13 retained them plus the previously observed image swap diagnostic failure
(123 failures/422 errors/67 skips). Injected os.replace failed with Windows access denied,
and the reader rejected it with cannot-open rather than changed-while-opening. The isolated
swap rerun passed on 3.13 but failed on 3.11 with that same access-denied diagnostic; this is
stronger evidence of a recurring Windows behavior, not a passing rerun on both versions.
Cause remains unproven and no imaging code or test was edited. This initial full result remains
recorded; final cap-adjusted verification
is running. Initial logs: mod-base-build-e2e-batch-patch-py311.log and -py313.log externally.
Final focused verification has 590 cases passing on each of Python 3.11/3.12/3.13.
Current staged digest is sha256:6082b21fe09a60935b314a5584de1e4151c45a875092b0a9bf396af89a265105.
Final full Windows suites each executed 1686 tests / 1910 discovered and retain exactly the
544 baseline failed names, with none added/removed (122 failures/422 errors/67 skips). The early
source entry-cap regression passes within each full run. The image swap extra failure did not
recur in these final runs; its prior full and isolated failures remain recorded above and its
cause remains unproven. External final logs: mod-base-build-e2e-patch-cap-py311.log and -py313.log.
External compilation, lock/digest checks, both whitespace checks and no repository bytecode pass.
This does not establish Linux/native source, merge-base, byte or writer admission. No consumer,
release or GitHub state changed; all K/Q/B gates remain open.
All K/Q/B gates remain open and consumers remain inactive.

### Ordered batch source collection (8 October)

K5 build_ci.batch.authenticate_batch_members now validates a distinct ordered 1..50 member
tuple before API reads. Existing read_pr_generation binds every open same-repository
member to the live original protected default/controller. Exact head commit/tree reads are
bracketed by generation admission, and the complete ordered collection is repeated to reject
movement during later members. All members must retain the same protected base-tree observation.
Frozen BatchMember records are observations only; constructing them cannot authorize a writer.

Seven inert FakeGitHub cases cover requested order, the actual 50-member boundary, malformed and
duplicate input before API reads, closed/forks/nested batch/base branch/wrong base/bad head object,
earlier member
movement during later collection, same-head tree drift and readiness movement during tree reads.
This helper performs no candidate import/Git/API mutation. Native ordinary-path policy and
protected/restricted/kit/matrix exclusion, patch inventory/manifests/result trees, sanitized
commit-tree construction, branch leases, settlement and production integration remain open.
No batch/consumer activation or completed K5 is claimed. Source inspection of the immutable
Quick Skin constructor showed it accepts draft members, including explicit --drafts selection;
the initial ready-only check was corrected before handoff. Draft state is retained and must
remain unchanged; members on batch/* or the base branch reject, preserving original exclusions.
This does not authorize a draft member's own required gate or bypass the batch's native admission.

The initial ready-only full suites executed 1677 tests / 1901 discovered. 3.11 retained exactly
the 544 baseline names. 3.13 retained those names plus the bump rewrite test: cached_kit failed
to publish its fetched temporary kit before the new planning code ran (122 failures/423 errors/
67 skips). Cause is not established; this resembles previously recorded Windows cache publication
failure but is not erased by later passing reruns. Both isolated three-case BumpTest suites
subsequently pass, without source changes to cache publication. Final corrected-source verification
has 582 focused cases passing on each of Python 3.11/3.12/3.13. The staged digest is
sha256:c10389b3a36fad4d08f3cadf2a705410badac93dd5b94b9879736e2faf2bcb08.
External compilation, locks, digest, both whitespace checks and absence of repository bytecode
pass. Final full Windows 3.11/3.13 suites each executed 1678 tests / 1902 discovered and retain
exactly the 544 baseline failed names, with none added or removed (122 failures/422 errors/67
skips). The earlier cache publication failure did not recur; its cause remains unproven and
the original extra-failure run is retained above. Final external logs are
mod-base-build-e2e-batch-final-py311.log and -py313.log. These results do not prove the required
Linux/native physical boundaries. No consumer/release/GitHub state changed; all K/Q/B gates
remain open.

### Target-kit bump planning before pin edits (8 October)

K4 managed bootstrap bump now runs the verified target kit's existing sync library with
write=False before rewriting any references. Ordinary drift is expected and accepted; rejection
uses the target kit's normal error boundary and stops before pin edits or writing sync. Arguments
remain separate process arguments, and no new CLI flag or data-selected program is added.
The local v1.0.3 tag retains the two library interfaces used by this fixed planning program.

Three tests run that actual planning subprocess against authored miniature target modules,
with explicit pin/cache/API/write seams. They prove rejection-before-rewrite, drift acceptance,
literal path arguments and continued rejection of later write failure. The existing local-Git
target fixture now supplies these miniature library APIs. These are not physical no-follow,
native activation, complete bootstrap parity or transactional rollback evidence; a write/race
failure after successful planning can still leave partial edits and needs further work.

575 focused tests pass separately on Python 3.11/3.12/3.13; the module list now deduplicates a
repeated tests.test_ci_inputs entry from the previous focused list. External compilation, locks,
digest and both whitespace checks pass; no repository bytecode was emitted. Current staged kit
digest is sha256:f4d33c972c55f23489d6c42c472e280b9272559a9770ecb7cc01e9f7bdd42e4a.

Both full Windows suites completed 1671 tests / 1895 discovered. Python 3.11 retains exactly
the 544 baseline failed names (122 failures/422 errors/67 skips). Python 3.13 has those same
544 names plus test_a_file_swapped_before_opening_is_refused in imaging StableReadTest
(123 failures/422 errors/67 skips). Its injected os.replace failed with Windows access denied;
the image reader rejected the file, but the diagnostic differed from the expected swap error.
That previously observed extra failure now recurred in 3.13. The isolated swap case passes on
both versions; this does not establish the cause or turn the original full suite green. No
imaging source or tests were edited. Both isolated 28-case imaging modules retain only the
baseline test_only_regular_files_are_read error, and the swap case passes there as well.
Full logs are mod-base-build-e2e-bump-planning-py311.log
and -py313.log in the external Windows temp directory. No consumer, release or GitHub state changed.
All K/Q/B gates remain open, including actual active profile/caller/transition implementation.

### Local template activation preflight (8 October)

K4 template.tool.load_template_activation now precedes manifest selection and all writes in
check/evaluate, sync and init. Legacy absence stays valid while current registry enrolls Pages
only. Present activation must be a regular no-follow bounded 8 KiB document, with a separate
regular strict 1 MiB native Build config and identical repository/profile. Disabled data passes
that preflight; active/shadow/rollback modes reject because no fixed Build/E2E templates/native
transition admission exist yet. This fail-closed foundation is not the completed active-profile
implementation, automatic activation, owner approval or removal-marker/rollback protection.

Build config path now has one MB11 config constant; the controller public alias is preserved.
Five cases check legacy absence, cap/config binding, malformed/foreign/missing/linked state,
unsupported modes and actual check/sync/init rejection before registry selection or writes,
with explicit filesystem seams. Previous rendered-registry operation tests now also mock their
repository directory and require the precise unregistered-workflow diagnostic, so they reach
the intended guard instead of passing on a missing fixture directory or activation-read error.
Physical no-follow/Linux behavior remains mandatory; these seams are not that evidence.

Exact protected transitions, actual active caller/profile rendering, managed marker removal
protection, pin/bump/bootstrap parity, native writer/approval/rollback and Linux conformance
remain incomplete. All K/Q/B gates remain open; no consumer/release/GitHub mutation occurred.

584 focused tests pass separately on Python 3.11, 3.12 and 3.13, including the strengthened
rendered-registry operation cases and five local preflight cases. External compileall, staged
locks, digest, working/index whitespace and no-repository-bytecode checks pass. Current callee
digest: sha256:d1f8983062716d10621d169bc8573f28d07ae0bd3445b4027bf70a45f837edb0.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1668 tests (1892 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline with none added/removed. This does not prove green Linux/native
profile/transition or physical template/bump/bootstrap conformance. Terminal logs in the host
temporary directory: mod-base-build-e2e-template-activation-py311.log and -py313.log.
No test process remains running. No commit/release/consumer/GitHub mutation occurred;
all original K/Q/B gates remain open.

### Original-controller activation/config binding (8 October)

Inactive K1/K4 controller.authenticate_controller_activation requires the fixed manifest path
and Build config in a bounded canonical independently admitted native protected-file policy.
It authenticates the original controller config/import closure, retains its commit/tree, reads
only the fixed activation from that controller tree, rejects missing/linked/executable/oversized
or case-aliased data/ancestors, and binds strict repository/profile to that config. Tree/blob
length and cryptographic blob identity bind bytes. Sources are reauthenticated, manifest bytes
reread and live source identity checked again before returning frozen sources/manifest evidence.
Candidate manifest data is never the source. Constructors/mode labels prove no owner approval,
protected transition, predecessor, fixed rendered caller or execution/status authority.

Five cases use actual inert fake-API commit/tree/blob/source admission plus explicit drift/API
failure seams. They cover all modes, controller-versus-tested identity, native policy/path
rejection, source metadata, foreign configuration, final manifest drift and final live-source
failure. Fixture activation now follows the same native profile as the coherent Build config;
there is still no second profile/version/scenario inventory. No code is imported/executed or
API mutated during admission. Physical protected origin/import/runtime, exact native transition
and owner/rollback admission, profile-aware template/pin/bootstrap wiring and hosted Linux
conformance remain incomplete. All K/Q/B gates remain open.

579 focused tests pass separately on Python 3.11, 3.12 and 3.13. Actual fake-API protected
source/tree/blob admission and final brackets pass, alongside kind/schema/compatibility and
previous controller/input checks. External compileall, staged locks, digest, working/index
whitespace and no-repository-bytecode checks pass. Current callee digest:
sha256:f115b3738c4d886c708645fadc466600a904774d828ee7e85cf650bb925b2be2.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1663 tests (1887 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline with none added/removed. No green Linux/native/transition or
actual full-lifecycle result is established. Terminal logs in the host temporary directory:
mod-base-build-e2e-controller-activation-py311.log and -py313.log. No test process remains running.
No commit/release/consumer/GitHub mutation occurred; all original K/Q/B gates remain open.

### Strict activation manifest data (8 October)

Inactive K1/K4 mod-base.ci.activation v1 now has a strict pure validator, central 8 KiB reader
cap, generic bounded document dispatch, coherent disabled-profile fixture/builder, hostile
mutation coverage and exhaustive compatibility ledger entry (new-kind/current 1/previous null).
The predecessor reader rejects this kind; no optional fields or new semantics were injected
into Pages v1. Fixed prospective path: site/mod-base-build-activation.json. Exact fields are
kind/schema_version/repository/native profile/mode; profiles are quick-skin/block-pops and modes
are disabled/shadow/shared-build/shared-build-and-e2e/reviewed-rollback. Arbitrary pin, job,
template, permission, secret, approval, extension/deferral, matrix/scenario and path selectors
reject. The mode label never proves owner approval or grants execution/status authority.

Protected binding to original repository/configuration, exact current-head transition and
native predecessors, fixed rendered caller bytes, reviewed rollback and profile-aware
init/check/sync/bump/bootstrap integration remain incomplete. Legacy consumers do not gain or
require this manifest here. Native transition admission is not replaced by a parsed document.
All K/Q/B gates remain open; no release/consumer/GitHub mutation occurred.

574 focused tests pass separately on Python 3.11, 3.12 and 3.13, including actual archived
predecessor-reader rejection, exhaustive kind/fixture/negative-case coverage and hostile activation
data/reader cases. External compileall, staged locks, digest, working/index whitespace and
no-repository-bytecode checks pass. Current callee digest:
sha256:4a322cf45520fd818520ff0ec93b9c3121fbe54833ffe52241a79ea8a282a1a8.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1658 tests (1882 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline with none added/removed. The previous rendered-registry turn's
isolated full-run image swap access-denial did not recur here; its cause remains unproven and its
original result stays recorded. This does not replace mandatory green Linux/native/profile
integration evidence. Terminal logs in the host temporary directory:
mod-base-build-e2e-activation-py311.log and -py313.log. No test process remains running.
All original K/Q/B gates remain open; no commit, release, consumer or GitHub mutation occurred.

### Closed rendered-caller registry foundation (8 October)

K4 now has a closed protected-code destination/source/renderer tuple in template.tool. Its sole
current member is Pages with its existing managed-region/extension policy. Shared check/sync/init
routing derives from that record; known path/source/class remapping and case aliases reject.
Manifest admission rejects unregistered workflow PIN/VERSION placeholders in managed, fragment
and seeded entries before repository writes. Templates are read once per unregistered workflow
under existing source bounds. Plain workflows remain ordinary manifest entries and gain no
renderer or authority. Existing signatures, Pages markers/extensions/pin rendering and schema
versions are unchanged. No Build/E2E caller is enrolled or activated by this foundation.

Five focused tests cover genuine manifest schema admission, all three manifest classes, fixed
binding/case aliases, ordinary workflows and actual check/sync/init pre-write failure with
explicit file seams. The existing 47-test template suite was attempted locally and FAILED
(3 failures, 83 errors), including unsupported no-follow reads and existing Windows differences;
full-suite baseline comparison remains necessary. Closed activation profiles, their manifest,
protected transitions, actual Build/E2E templates/registry members, bump/bootstrap parity and
Linux template evidence remain incomplete. Every K/Q/B gate remains open.

570 focused tests pass separately on Python 3.11, 3.12 and 3.13. External compileall, staged
locks, digest, working/index whitespace and no-repository-bytecode checks pass. Current callee
digest: sha256:21ebf953876721c6adb92df24314e2b46e852477ad15644e2511bd70be60ded6.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1654 tests (1878 discovered).
3.13 has 122 failures, 422 errors and 67 skips: exactly the 544 untouched baseline failing/error
names, none added/removed. 3.11 has 123 failures, 422 errors and 67 skips: the same baseline plus
StableReadTest.test_a_file_swapped_before_opening_is_refused. Its injected os.replace failed with
Windows access-denied WinError 5; the image reader rejected opening, while the test expected
"changed while opening". Imaging source/tests were not changed. Repeating the entire 28-test
imaging module separately on 3.11/3.13 shows only the existing baseline regular-file error; the
swapped-file case passes there and separately on 3.11. Cause of the full-run access denial is
unproven; the first full failure remains recorded. Both full suites are FAILED and do not prove
green Linux/template/activation conformance. Terminal logs in the host temporary directory:
mod-base-build-e2e-rendered-registry-py311.log and -py313.log; isolated module logs:
mod-base-rendered-registry-imaging-py311.log and -py313.log. No test process remains running.
No commit/release/consumer/GitHub mutation occurred. All K/Q/B gates remain open.

### Byte-fenced protected controller hooks (8 October)

Inactive K2 `build_ci.controller.execute_byte_fenced_controller_validator` routes closed
verify_build/verify_target/verify_runtime hooks through independently approved selected tool
bytes. Shared private implementation retains the original plan, retained source/config/profile,
exact enrolled unit, distinct candidate/validator/runner identities, controller read-copy checks,
fixed dispatcher argv/timeout/environment and mandatory validator cleanup. Its public byte
entry selects byte mode explicitly; an absent/malformed digest or metadata-only receipt cannot
fall back to metadata execution. Source reinspection still follows successful byte-fenced
execution. Original metadata interface remains unchanged and retains its caller provenance
prerequisites. Failed admission, byte execution or post-source drift never returns success.

Tests use explicit syscall/execution seams, inert fake-API protected source evidence and the
actual shared closed-hook logic; none establishes Linux execution or native semantic success.
Original caller/runtime, complete system/import enrollment and immutable native inputs remain
caller prerequisites. Additive `inputs.execute_byte_fenced_build_validator` and `execute_byte_fenced_target_validator`
now share the original frozen-input lifecycle: exact complete/target scope and enrolled unit,
same producing run/attempt, canonical plan/envelope, read-only input bytes/metadata and retained
directory identities before/after the byte-fenced controller. Return original execution and
canonical input digest only after final input recheck. Missing digest cannot select legacy
execution. Original public metadata interfaces and receipt-freeze contract stay unchanged.
Production workflow selection of the stronger route remains pending, along with the complete
hosted Linux/native lifecycle. All K/Q/B
gates remain open; no release, consumer activation or GitHub settings mutation occurred.

565 focused tests pass separately on Python 3.11, 3.12 and 3.13, including the existing
controller/frozen-input suites and seven new byte-route cases. External compileall, locks,
digest, working/index whitespace and no-repository-bytecode checks pass. Current callee digest:
sha256:fae3627384cf20437915bee9d7d83c767faeec75eb3d4633d662c4a0f21c1995.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1649 tests (1873 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline with none added/removed. Green Linux/native conformance and
actual full lifecycle evidence remain missing. Terminal logs in the host temporary directory:
mod-base-build-e2e-byte-controller-py311.log and -py313.log. No test process remains running.
No commit, release, consumer activation or GitHub settings mutation occurred.

### Byte-admitted disposable worker dispatch (8 October)

Inactive K2 `build_ci.toolchain.execute_byte_fenced_worker` adds a separately approved digest to
the selected-closure byte receipt. Actual runner fence and exact disposable account identity
precede admitted cleanup or tool reads; foreign identities cannot authorize termination. A
malformed receipt/digest or mismatch rejects before dispatch. Complete selected bytes are
reauthenticated against the independent digest, then the existing metadata/path/host fence
executes the bounded dispatcher. Its whole-UID termination precedes the final full-byte
reinspection. Byte/metadata drift or execution failure rejects and locks/terminates the admitted
account. A zero exit cannot bypass final byte checks. Receipt constructors and locally observed
hashes are not approval; neither returned logs nor the result authorize export or upload.

The original protected caller/runtime, full system/import enrollment, source/Git/cache/overlay
preparation, native request/policy and excluded writers remain prerequisites. Existing metadata
entry signatures are unchanged; production callers are not yet wired to the additive route.
The new required Linux component uses authored temporary file hashes, actual selected-tree
byte/metadata readers and descriptor accounting with explicit outer host/account/dispatch seams;
it does not execute a tool, allocate an account or establish the full lifecycle. Physical hosted
Linux execution and native workflow integration remain pending. All K/Q/B gates remain open.

528 focused tests pass separately on Python 3.11, 3.12 and 3.13. External compileall, staged
locks, digest, working/index whitespace and no-repository-bytecode checks pass. Current callee
digest: sha256:b7f9a68383a5fdd44fbb56f3d3083979c93ac475f6f05ab075011300870ac34f.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1642 tests (1866 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline with none added/removed; required green Linux/native evidence
is still missing. Terminal logs in the host temporary directory:
mod-base-build-e2e-byte-dispatch-py311.log and -py313.log. No test process remains running.
No commit, release, consumer activation or GitHub settings mutation occurred.

### Composed candidate checkout preparation (8 October)

Inactive K2 `build_ci.worker_preparation.prepare_privileged_worker_checkout` composes the four
exclusive source/Git/cache/overlay handoffs before candidate execution. Actual Root/fenced home,
exact fresh worker and current quiescence precede pin/digest/path admission. Original source,
curated Gradle seed and proposed kit overlay must be distinct nonoverlapping protected roots;
Git comes only from that retained original source's .git. The original allocated cache inode is
retained before effects, followed by original source/Git/cache/overlay descriptors and each
published worker root. API-authenticated tested-tree inventory precedes source copying; tracked
collisions with the reserved overlay reject. Each stage rechecks held/named roots, roles and
quiescence. Final tracked source bytes/modes, selected Git/cache data, private Git/cache metadata,
overlay pin/digest/locks, original data and live API/released-pin identity are rechecked together.
Malformed pin/digest inputs reject before source access; any admitted failure terminates/locks
worker and prevents later stages. Observations remain data-copy evidence, not execution authority.

Original caller/runtime and Git graph/index provenance, native request/policy/upgrade admission,
secret-free cache restoration, excluded writers and no earlier UID execution remain caller
prerequisites. Source, cache and overlay staging origins must be arranged by the protected
controller; an overlay inside the original candidate source is deliberately rejected. Production
workflow wiring, candidate startup and the complete execute/freeze/second-account lifecycle,
Linux adversarial evidence and native conformance remain incomplete. The hosted component body
is captured and compiled without executing privileged commands locally; it composes real data
helpers in fresh temporary Linux directories with explicit outer API/host/account/checkout seams.
All K/Q/B gates remain open; no release, consumer or GitHub settings change occurred.

524 focused tests pass separately on Python 3.11, 3.12 and 3.13. External compileall,
staged locks, digest, working/index whitespace and no-repository-bytecode checks pass. Current
callee digest: sha256:18c8323e9ef1a969b9a5980dadd3ceb7793ce1c0ccd952cf30476425b48ab4e8.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1638 tests (1862 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. The exact 544 failing/error names
match the untouched Windows baseline, with none added or removed. This is not green Linux
boundary or native-conformance evidence. Terminal logs in the host temporary directory:
mod-base-build-e2e-worker-preparation-py311.log and -py313.log. No test process remains running.

### Candidate-only Git metadata curation (8 October)

Inactive K2 `build_ci.worker_git.stage_privileged_worker_git` now curates independently
admitted original self-contained SHA-1 files-backend checkout data for the fresh quiescent
candidate. Actual Root/fenced home and exact worker admission precede protected metadata
inspection; original caller/runtime, checkout/commit/tree/index and tracked worker source,
writer exclusion and no earlier UID activity remain mandatory caller prerequisites. Complete
bounded no-follow metadata/ownership/alias/type closure selects index, conventional loose/pack
object files, heads/tags/remotes/pull refs, packed refs and optional shallow boundary. Source
HEAD/config/hooks/logs/other ancillary content is not read/copied. Unsupported shared/reftable/
split-index/partial-clone stores, external object borrowing/grafts, replacement refs and unsafe
entries reject. Bound ref ASCII/shape/duplicates and count lines before splitting lists.

The admitted tested commit becomes detached HEAD; grammar-bound repository supplies fixed
credential-free origin/configuration, with hooks/fsmonitor disabled. Exclusively publish new
independent files at fixed worker repository .git; ACL-free private worker 0700/0600 handoff and
repeated original source/parent/role/copy/name/inode/content checks surround publication. Never
invoke Git or execute copied data as Root; observed copy hashes do not approve object graphs,
privileged Git/runtime or native behavior. Existing .git is never adopted/removed/overwritten;
failed admission locks/terminates worker and late inert output may remain. Protected post-run
Git restoration/index refresh, worker Git startup, complete environment/credential/runtime
boundary, native profile parity and workflow composition remain incomplete.

Two additive MB1 selected-data inventory/copy APIs preserve empty selected files without reading
other contents, with bounded full metadata closure and path/cap preflight before opening.
Selected data copy requires an empty private stage; existing selected export copy retains its
nonempty whole-inventory and incremental-stage behavior. Dedicated central Git caps alias source
file/entry/tree limits, with 4 KiB loose refs and 64 MiB packed/shallow lists.

518 focused tests pass independently on Python 3.11, 3.12 and 3.13. Known authored SHA-1
blob/tree/commit/index fixture bytes plus generated config pass actual local test-user Git
HEAD/tree/clean status/tag reads. This format check uses authored data and original local Git;
it is not a privileged runtime, copied consumer, Linux or full native-conformance result.
The new required Linux component body compiles after capture, without executing sudo/Git or
copied programs: it will check actual temporary independent metadata copying, worker-private
ownership/modes, omission of source token/hook fixtures, known HEAD/config/data and overwrite
refusal. Outer original-checkout/role/account/location/quiescence seams are explicit. Actual
Linux component execution remains pending. External compileall, locks/digest, whitespace and
no-repository-bytecode checks pass. Current callee digest:
sha256:b522a96475250bf852607656dc11ba45c0a4a20e5d11e557a7ea090a3a4962b4.
The first full Windows 3.11/3.13 suites are terminal: each executes 1632 tests (1856 discovered),
with 122 failures, 422 errors and 67 skips, exactly the 544 untouched baseline failing/error
names with none added/removed. Both are FAILED, not green Linux evidence. Final review then
identified allocation risks from str.splitlines control-byte separators and large header flag
lists. Ref lists now accept only visible ASCII/LF, bound LF count before splitting and bound
4 KiB line width before tokenizing fields/flags; hostile header/control cases pass focused checks.
Final revised-tree full suites are also terminal: each Python 3.11/3.13 run executes 1632 tests
(1856 discovered), with 122 failures, 422 errors and 67 skips. Exact 544 failing/error names
still equal the untouched Windows baseline, with none added/removed. Both suites remain FAILED;
these results do not replace required green Linux CI. Final logs in the host temporary directory:
mod-base-build-e2e-worker-git-final-py311.log and mod-base-build-e2e-worker-git-final-py313.log.
Earlier pre-hardening logs: mod-base-build-e2e-worker-git-py311.log and -py313.log.
Final 518 focused tests passed separately on Python 3.11/3.12/3.13 after the line-parser change.
No test process remains running.
All original K/Q/B gates remain open. No commit/release/consumer/GitHub mutation occurred.

### Private candidate tracked-source publication (8 October)

K2 now has inactive `build_ci.worker_source.stage_privileged_worker_source`: actual protected
Root/fenced home and exact fresh quiescent worker admission precede private-home source inspection.
It verifies independently authenticated tested-tree inventory/bytes/Git modes, retains original
source and protected worker-parent bindings, and exclusively publishes a new independent tracked
copy at the fixed candidate repository. Existing output rejects; original opaque Git metadata is
omitted. The new MB1 `privatize_source_copy` transfers only a fresh protected independent copy,
rejects undeclared/Git/special/hard-linked/foreign-owned closure, removes file/directory ACLs,
sets private worker-owned directories/nonexecutables/executables to 0700/0600/0700, retains exact
Git modes/link bytes and transfers root last. Symlink ownership is descriptor-relative no-follow;
literal external/dangling targets remain data and receive no privileged path/execution authority.
Complete metadata/ACL/content/name/inode checks precede final return. Admitted failures lock/
terminate worker and return no authority; late inert output can remain.

Original caller/runtime, tested-tree origin/native policy, excluded writers and no previous UID
execution remain caller preconditions. No Git/candidate code is executed. Protected credential-free
Git metadata staging, composition with cache/overlay/import setup, execution/freeze/validator/native
parity and workflow integration remain incomplete. Read-only inert inspection of the fixed Block Pops
47a890ae46a2878fb08d29a932803ab91bccdcd9 untrusted_runner.py confirms its separate restoration:
remove candidate .git, copy authenticated source .git, then check HEAD/tree and refresh index
with fsmonitor disabled and diff-index --no-ext-diff. This is source evidence only, not permission
to execute that consumer or proof that wholesale metadata/config copying satisfies the kit's
credential/runtime boundary. Kit metadata admission/transport remains to be implemented. No activation gate is closed by this helper.

508 focused tests pass separately on Python 3.11, 3.12 and 3.13. Descriptor-seam tests exercise
Root-last ownership, executable/empty/literal-link preservation, foreign ownership, hard links,
Git metadata, ACL failure, content drift and symlink replacement rejection; orchestration tests
cover source/parent/copy/handoff/role/late-metadata failures. These are not physical host proofs.
The new mandatory Linux source component probe uses known authored Git expectations, real
fresh temporary copying/handoff, Git omission/private modes/overwrite refusal and an outside
Root-owned sentinel whose inode/owner/group/mode/ctime/bytes must not change through link handoff.
Its outer role/account/location/quiescence seams are explicit. Body captured/compiled on Windows;
not run as Linux/Root here. External compileall, locks/digest, whitespace and no-bytecode checks
pass. Current callee digest:
sha256:2bf5689196bae735f057705f29bda970996bff50b82998acc93b8def0331e9a0.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1622 tests (1846 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Exact 544 failing/error names match
the untouched Windows baseline, with none added or removed. No new source/handoff test failed;
this host comparison does not satisfy green Linux CI. Logs in the host temporary directory:
mod-base-build-e2e-worker-source-py311.log and mod-base-build-e2e-worker-source-py313.log.
No suite remains running.
All original K/Q/B gates remain open; no commit/release/consumer/GitHub operation occurred.

### Candidate overlay and empty-data correction (8 October)

Inactive K2 source work now includes `build_ci.worker_overlay`: the original protected Root
implementation authenticates a supplied checkout/bootstrap-bound pin/digest and stamped kit,
rechecks released tag/main ancestry and staged locks, and exclusively copies it as candidate-only
data at the fixed repository overlay path. Independent files retain zero-byte Python sources;
required empty src/site/requirements roots are recreated. ACL removal and worker ownership are
followed by bootstrap 0644/0755 modes, with bounded permission walks, held/named output-parent
identity and per-entry name/descriptor checks. Failed admission locks/terminates the worker;
a late published inert overlay may remain. No future code is imported, no workflow calls this
helper, and original caller/runtime enrollment, candidate upgrade admission, Git integration,
Linux boundary proof and native parity remain incomplete.

Review found that the previous Gradle helper used export APIs that refuse zero-byte files,
contradicting its authored Linux fixture. Four additive MB1 regular-data inventory/copy/handoff
APIs now preserve empty files and admit bounded empty directory closure while rejecting unsafe
paths, aliases, links, hard links and special files. Export readers and old copy signatures keep
their nonempty-file contract. Gradle staging uses the data APIs and retains independent copying,
original BP bounds and worker-private 0700/0600 handoff; protected secret-free restoration and
writer exclusion remain caller prerequisites. Generic data copying omits empty directories;
overlay setup recreates the mandatory bootstrap roots.

501 focused tests pass independently on Python 3.11, 3.12 and 3.13. The new required Linux overlay
probe uses independently authored fixture expectations, real temporary copying/publication and
ownership/mode checks, plus explicit outer account/host/location/quiescence/release seams.
The repaired Gradle and new overlay probe bodies have been captured and compiled locally;
neither has run as a physical Linux/Root probe here. Compileall passed with external bytecode;
staged locks, generated digest and working/index whitespace checks pass. Current callee digest:
sha256:1d0f40e8ef7eb5805116bddbbee1385a25e64032db3953413b1e0084d1c3cfe1.
Full Python 3.11/3.13 Windows suites are terminal: each executes 1615 tests (1839 discovered),
with 122 failures, 422 errors and 67 skips. Both are FAILED. Their exact 544 failing/error names
match the untouched Windows baseline, with none added or removed; this comparison does not
satisfy green Linux CI. Logs in the host temporary directory:
mod-base-build-e2e-worker-overlay-py311.log and mod-base-build-e2e-worker-overlay-py313.log.
No test process remains running. No commit, release, consumer activation or GitHub mutation occurred.
Every K/Q/B completion gate remains open.

| Step | Current evidence | Remaining completion gate |
| --- | --- | --- |
| K1 | ADR 0007; strict config/plan/identity/envelope/selection/gate/reuse schemas; full graph primitives; adversarial cases; digest-bound predecessor reader and exhaustive ledger | Activation manifest; full/deferred/reuse/attest/transitional caller graphs; native QS/BP immutable parity; Python 3.11/3.13 full suite and Linux CI |
| K2 | Inactive fresh traversal-root/account allocation, hosted runner-home fence/recheck, bounded host tool-tree permission/identity closure and pre-dispatch reinspection, environment/termination and bounded execution/cancellation port; source-bracketed GitHub tested-tree inventory for ready PRs and exact non-PR subjects in protected default history, tracked-byte/mode comparison and atomic private tracked-source copy; candidate/verifier independent private freeze, protected code/read handoff and fixed read-only plan/Build aggregate verifier input binding; root-side retained execution/envelope/producer context binding with input reinspection around independent receipt freeze; additive selected-byte-fenced worker/controller hooks and complete/exact-target frozen-input execution preserving source/input rechecks; required Linux account/source/copy/input/dispatcher/tool fixtures wired into every Python CI leg (not run yet) | Real hosted boundary evidence and outside-layout/toolchain/import/cache closure, trusted installer provenance and complete import enrollment; production source/Git/cache/overlay staging integration; non-PR request/run/nonce/ref authorization and materialization integration; complete native target/runtime/aggregate lifecycle and production integration; all Linux adversarial results and policy-suite parity |
| K3 | Exact target-union receipt validation, canonical export size/hash inventory, logical entry-cap checks and independently verified atomic export materialization; inactive complete Build and same-attempt target transport; complete ordered target-input set with shared API context, successful plan/policy, additional compressed/physical bounds and one private atomic publication; complete same-attempt byte assembly with fixed target input layout, independent selected-file copying, current canonical aggregate envelope, source/stage revalidation and one atomic publication; pre-upload envelope/gate/reuse identity separated from actual descriptor API windows with explicit initial-format decisions and source-before-record binding; full-gate API chronology binds successful source/gate steps and prerequisite completion before verifier start, including independently graphed owning Build; full tested-record numeric-ID transport with strict fixed canonical JSON, latest successful protected producer, immutable ZIP/API metadata, bracketed source availability/timeline and independent owning Build enrollment; fixed separate CI ZIP extraction bounds; newest exact PR Build selection before success with strict initial v1 title hints, full protected producer/artifact admission and final newest/source rechecks; inactive monotonic 5400-second/91-observation waiting and exact-descriptor consumption revalidation; integrated latest-PR complete download with final newest/source admission inside private atomic byte publication; bounded local stored ZIP encoding with no-follow byte streaming, strict independent extraction/source rechecks and atomic local archive publication | Protected/native workflow integration and reuse-record transport; workflows/composites; production waiting/consumption integration, native aggregate construction/validation/receipts, approved upload encoding/actual artifact cap proof and packaged transport/assembly; native byte/receipt validation; real Linux bundle/target/set/record/assembly transport and all failure cases; actionlint/shellcheck |
| K4 | Strict initial activation manifest kind/cap/fixture/compatibility; closed rendered-caller path/source/format registry with Pages-only enrollment and unknown-workflow placeholder rejection before writes | Protected activation transitions/config binding and profile-aware integration; actual Build/E2E caller registration; init/check/sync/bump/bootstrap/pin parity and hostile cases |
| K5 | Not implemented | Protected ordinary-only batch construction, membership, safe writer and settlement proof |
| K6 | Not implemented | Direct coherent reuse/source seals and downstream conformance; no chains or corruption fallback |
| K7 | Not started | Protected release sequence and expanded real hosted synthetic canary before Release |
| Q1 | Not started | Protected native adapter/readers and bootstrap-only inactive pin bump under legacy gates |
| Q2 | Not started | Strict external dependency verification and reviewed hashes; full target builds |
| Q3 | Not started | Approval/reconciliation/shadow preparation; isolated canary and concrete owner App/event proposal |
| Q4 | Not started | Separately authorized provisioning; bounded shadow period and all migration comparisons |
| Q5 | Not started | Protected bridge publishing original names while native authority remains |
| Q6 | Not started | Separately authorized source-only governance settings operation with recent App contexts |
| Q7 | Not started | App-gated activation and fresh post-merge full execution/downstream checks |
| Q8 | Not started | Ordinary batch activation and exact membership/settlement proof |
| Q9 | Not started | Direct reuse activation, full downstream proof and measured latency/cost |
| Q10 | Not started | Remove generic local copies; full docs/governance/rollback conformance |
| B1 | Not started | Fresh origin/master worktree only after predecessors; approved compatibility/pin foundation |
| B2 | Not started | Shared Build activation; unchanged complete App/20-lane evidence; concurrency/corruption tests |
| B3 | Not started | Shared E2E and exact Build handoff; no duplicate compilation/ceiling; complete native/public/release checks |
| B4 | Not started | Protected draft deferral/reconciliation and same-head readiness/outage checks |
| B5 | Not started | Ordinary-only batches under current admission, exact complete tree/gate provenance |
| B6 | Not started | Direct identical-tree reuse across every downstream reader; full recovery |
| B7 | Not started | Retire generic copies/current transition window; historical readers and tested rollback |

Local environment observed: Windows PowerShell, Python 3.12.10 and uv-managed Python 3.11.15/3.13.14,
WSL executable with no installed distribution. Python 3.13 uses an external temporary virtual
environment with Pillow installed from requirements/pillow.txt using hashes and binary wheels.
Python 3.11 now has a separate external environment with hash-locked Pillow; Linux privilege-boundary evidence remains missing. Existing kit CI is Ubuntu
24.04 with Python 3.11/3.12/3.13; no run of the modified tree has been submitted.

## Verification recorded 2026-10-07 and 2026-10-08

Private Gradle cache handoff stage: inspected immutable Block Pops untrusted_runner.py at
47a890ae46a2878fb08d29a932803ab91bccdcd9 as inert Git data. Its restored seed validation uses
250000 entries, 200000 files, 2 GiB/file and 20 GiB total before independent candidate cache
copying. The new inactive MB11 helper preserves those bounds, requires a protected-home seed with
only caches/wrapper root directories, exact fresh worker/passwd/Root/fence admission and a
quiescent UID, and refuses a nonempty allocated cache. It holds that original inode and fences
access to Root during independent copying, rechecks source inventories/bytes and directory
bindings, then normalizes newly copied private ownership/modes/ACLs and checks final access.
Any admitted failure locks/terminates the worker and grants no execution authority. Protected
secret-free restore/save provenance, excluded writers/no earlier UID execution, original caller
runtime/helper enrollment and native wrapper/cache parity remain independent prerequisites.

All 493 focused tests pass separately on Python 3.11, 3.12 and 3.13. A required Linux component
fixture adds actual temporary independent files, empty-file preservation, private UID/modes and
configuration-root rejection with explicit outer setup/account/location/quiescence seams. Its
captured body compiles without invoking sudo or executing it. Actual Linux, protected credential
restoration and production lifecycle integration remain unverified; no workflow calls the helper.
Compileall uses an external bytecode prefix; locks, internal API, digest and whitespace checks
pass. Current staged digest is
sha256:26d6f26b5513314b359db55394e4ca73856068f3ad42cef8e5a046f42b0f45ea.


Terminal full-suite evidence for private Gradle handoff: Python 3.11 and 3.13 each executed
1607 tests / 1831 discovered, with 122 failures, 422 errors and 67 skips. Both gates **failed**.
Their 544 distinct failing/error names exactly match the untouched Windows baseline; none were
added or removed. The previous authentication stage's extra temporary-bootstrap-cache failure
did not recur in either complete run; its earlier failure remains recorded, not erased or
explained by this observation. Source/tests stayed unchanged throughout both complete suites.
Terminal logs are `mod-base-build-e2e-gradle-cache-py311.log` and
`mod-base-build-e2e-gradle-cache-py313.log` in the host temporary directory. Mandatory green Linux,
credential-boundary, original runtime, native cache parity and full lifecycle/hosted evidence
remain unverified. No K/Q/B completion, activation, release or remote mutation occurred.

Source-derived installed Python authentication stage: added read-only private cache/archive/SDK
reinspection, re-deriving all manifest/count/byte/mode/link expectations from the independently
locked source and enforcing original SDK root identity plus stable parent/source bindings.
The publisher installation composition now reauthenticates before returning. An additive fixed
copied-SDK sealing route repeats source admission at all three existing tool-check points while
retaining independently approved complete tool bytes, program/context/receipt checks and validator
cleanup. Existing public launcher parameters/behavior are preserved. Unsupported/forged receipts,
wrong executable paths, missing complete closure approval and source/SDK drift reject; a drift
after the subprocess rejects the receipt and terminates the admitted validator.

All 488 focused tests pass independently on Python 3.11, 3.12 and 3.13. A required Linux fixture
uses actual temporary Root cache/SDK files and source-derived expectations, with explicit outer
host/kit/profile/location seams; its captured embedded body compiles locally without invoking
sudo or executing a probe. No copied interpreter, archive program or production workflow was
launched. Full original caller/interpreter/import/system enrollment and real fixed-path/hosted
proof remain open. Compileall uses an external bytecode prefix; locks, internal API, digest and
whitespace checks pass. Current staged digest is
sha256:8ca315d71c8dea02642f8fe442afeeaee67de9ca6fdbd0def09beb567f995a28.


Terminal full-suite evidence for installed SDK authentication: both Python 3.11 and 3.13
executed 1602 tests / 1826 discovered. Both gates **failed**. Python 3.13 reports 122 failures,
422 errors and 67 skips, with the same 544 failing/error names as the untouched Windows baseline.
Python 3.11 reports 122 failures, 423 errors and 67 skips: the baseline names plus
`tests.test_pin.StageTest.test_a_different_released_pin_is_fetched_verified_and_staged`.
Its traceback is the managed bootstrap refusing publication of a fetched kit into the temporary
Windows cache. The unchanged case passes separately in both Python 3.11 and 3.13 (one test each,
about three seconds), without source/test changes; the cause of the full-run-only error remains
unproven. Do not treat the failed 3.11 full run as exact baseline parity or a green gate.
Source/tests stayed unchanged throughout both full runs and the diagnostic reruns. Terminal logs
are `mod-base-build-e2e-python-authentication-py311.log` and
`mod-base-build-e2e-python-authentication-py313.log` in the host temporary directory. Mandatory
Linux/full-runtime/hosted evidence and every K/Q/B completion gate remain open.

Root-private Python archive handoff stage: `build_ci.python_setup` requires actual Root host
role and a genuine private kit lock before any download/files, publishes the admitted numeric-ID
archive under a fixed exclusive private home cache, and connects its path to the existing
exclusive source-only Python installation. Root-only 0700 directory/0600 single-link file,
original parent/root identities, stable named/opened metadata and exact archive size/hash are
rechecked; bounded write progress and a two-entry inventory scan reject hostile input. Publisher,
kit and host rechecks bracket publication, handoff and installation return. Existing outputs
reject. A late failure grants no execution authority even if a private output already exists.
Original caller/runtime provenance, complete system closure and workflow startup remain separate.

All 480 focused tests pass independently on Python 3.11, 3.12 and 3.13. The tests expose their OS,
host, kit and publication seams and exercise the real byte writer/hash reader; they prove no
physical Linux boundary. Two required hosted Linux probes add actual Root temporary publication,
permission/hard-link/symlink/FIFO rejection and cleanup after changed bytes. Their captured bodies
compile locally without invoking sudo or executing a probe. No local fixed cache/global Python
installation or archive program was executed. Compileall uses an external bytecode prefix; locks,
internal API, digest and whitespace checks pass. Current staged digest is
sha256:eca20ed347218c3541430123ea8dc5d29fa51dd22c383dab85ce5a6ac05ee1b0.


Terminal full-suite evidence for the Root-private handoff stage: Python 3.11 and 3.13 each
executed 1594 tests / 1818 discovered, with 122 failures, 422 errors and 67 skips. Both gates
**failed**. Their 544 distinct failing/error names exactly equal the untouched Windows baseline;
none were added or removed. Source/tests remained unchanged throughout both complete runs.
Terminal logs are `mod-base-build-e2e-python-setup-py311.log` and
`mod-base-build-e2e-python-setup-py313.log` in the host temporary directory. The mandatory green
Linux suites, fixed-path setup boundary, complete runtime enrollment and hosted canary remain
unverified. No K/Q/B completion gate or consumer/release authority is established.

Python publisher transport stage: added bounded numeric release-asset download with direct 200
or one credential-free 302, preserving the existing artifact-only redirect behavior. The inactive
MB11 transport admits the three fixed publisher release/asset/tag/commit/latest and exact producer
attempt identities, unique membership and locked bytes, with full metadata brackets around actual
GNU archive inspection. Fake metadata adversarial tests cover every relied field, nested tag/repo
fields, missing/duplicate/oversized membership, tampered bytes, post-download drift and API/budget
errors. Binary fake seeding is separate from metadata and preserves public client conformance.

All 472 focused tests pass independently on Python 3.11, 3.12 and 3.13. Compileall uses an external
bytecode prefix and passes; locks, internal API, whitespace and repository bytecode checks pass.
Current staged tree digest is sha256:f8cbf45639909438df706e69648c0b06a4f878b2b4b8075f96a954e5ccfb8966.
The real production client freshly downloaded all three pinned assets without a token: 14 requests
each, 42 total, exact committed sizes/hashes and all pre/post API/reader checks passed. This was
inert in-memory admission only: no extraction, program execution or cache publication. Cache/source
handoff, full runtime enrollment, actual Linux installation/boundary and production wiring remain
open. No release, consumer activation or remote mutation occurred.


Terminal full-suite evidence for the publisher transport stage: Python 3.11 and 3.13 each
executed 1586 tests / 1810 discovered, with 122 failures, 422 errors and 67 skips. Both gates
**failed**. The 544 distinct failing/error names exactly match the untouched Windows baseline,
with none added or removed. Source/tests stayed unchanged throughout both complete runs;
metadata checks and real network admission ran read-only alongside them. Terminal logs are
`mod-base-build-e2e-python-transport-py311.log` and
`mod-base-build-e2e-python-transport-py313.log` in the host temporary directory. These results
establish no green Linux or hosted worker/canary evidence. All K/Q/B completion gates remain open.

Fixed Python installation stage: `build_ci.python_installation` requires actual root host role,
the genuine authenticated private kit/archive lock and a fixed supported exact version before
stable no-follow protected-home archive reading. A complete compressed-lock-first inventory
precedes protected parent provisioning and exclusive publication at the compiled hosted-toolcache
prefix. Existing destinations are refused; setup/bytecode/.pth/cache paths are omitted, removed
link targets reject, and root-owned read-only tool modes replace archive modes/ownership.
The archive reader now has a private same-unit streaming sink for the second approved pass;
its public signature and hash-first inspection behavior remain unchanged.

Regular/empty files stream into exclusive no-follow leaves, partial writes are handled and
zero progress rejects. Only after the complete second inventory agrees are internal links
created. Complete actual inode/owner/mode/size/hash/link/inventory verification, source/kit/
parent rechecks and original named-root checks bracket publication. The manifest digest derives
from approved archive members and a fixed transformation; it approves no system/runtime closure.
No archive program executes, no Python is relocalized and no existing tool slot is replaced.
Separate archive-lock read cap: 4 KiB; all prior bounds remain unchanged.

Nine installation tests cover publisher-lock parity, source-only normalization, deterministic
manifest identity, fixed root/role/version admission, source/copy/hash/existing-output/published
drift, real streaming/empty/partial/zero-write behavior, stable closed lock reading and read-only
parent rechecks. OS/account/publication seams remain explicit. Initial internal-API checks
correctly rejected a cross-unit private MB1 file helper; the installer now owns its exclusive
leaf creation using the existing same-unit no-follow directory primitive. Current installer/
reader/internal-API/model-limit checks pass (58 tests).

Two new required hosted Linux component probes copy inert synthetic bytes under root ownership,
verify actual modes/empty files/links, reject hard-link/symlink substitutions and clean failed
unpublished stages. They touch only fresh temporary trees and never execute copied bytes or
install at the global prefix. Both generated bodies compile without invoking sudo or their
bodies on this Windows host. Actual Linux results, authenticated publisher/download integration,
original caller and complete interpreter/import/system enrollment, privileged-launch/native
lifecycle and production wiring remain open; all K/Q/B completion gates remain incomplete.
All three real locked archives pass the fixed source-only transformation and contained-link/
versioned-executable checks without writing an installation. Expected member/file/payload
counts and archive-derived manifest digests are recorded in PYTHON-INSTALLER.md. Official
runner image source at `db776964592d0362a6bed85f90bc4e2980250e49` documents earlier cached
patches in image 20260927.320.1, suggesting unused selected slots; actual VM absence is not
established and exclusive publication remains mandatory. Setup must precede any action that
installs the selected version; future collisions fail rather than adopting unknown bytes.
Current external compileall, staged locks, digest and whitespace checks pass (existing CRLF
warnings); no repository bytecode remains. Staged digest:
`sha256:cb0a2572cad1ba1769af1c243590e84d03ef77cddd805ad9035c9cb9554e3a70`.
All 406 focused tests pass separately on Python 3.11/3.12/3.13. Full installation-stage
Windows suites are terminal: Python 3.11 handle 9433 and Python 3.13 handle 93060 each executed
1,575 tests (1,799 discovered), failing with 122 failures/422 errors/67 skips. Both retain
exactly the 544 untouched Windows baseline failure/error names, none added or removed.
Logs: `mod-base-build-e2e-python-installation-py311.log` and
`mod-base-build-e2e-python-installation-py313.log`. Source/tests remained unchanged throughout
both full runs. These remain failed gates and prove no actual Linux installation/runtime
approval. Fresh availability checks still find WSL without a listed distribution and no
QEMU/Docker/Podman executable, so no local Linux environment or hosted run is claimed.

Bounded Python installer reader stage: `build_ci.python_archive` authenticates the independently
supplied compressed size/hash before decoding and again after complete inert GNU TAR/GZIP
inventory. Its own fixed-size parser avoids general-purpose extraction and rejects checksum/
CRC failures, unsafe/duplicate paths, unsupported hard-link/special/PAX/sparse records, hidden
text, invalid/redundant/disagreeing/orphan long names, absent or non-directory parents,
external/dangling/cyclic/non-file-resolving links and nonzero/excessive padding. Empty regular
files are preserved with their real content hash. Returned member tuples/dataclasses are
immutable; source modes are retained as data and never applied. Stable no-follow input identity,
publisher/profile authorization, actual deterministic installation and complete runtime/system
enrollment remain separate prerequisites. No archive script/library/binary executes.

New separate central caps: 128 MiB compressed, 512 MiB expanded including headers/padding,
200:1 whole compression ratio, 20,000 nonzero raw GNU headers including long-name metadata,
10 KiB terminal zero padding. Existing path/depth/link-hop limits are reused, with exact-bound
acceptance/rejection. Native, kit, worker and artifact bounds remain unchanged. Eleven real
in-memory adversarial TAR/GZIP tests and internal-API/limit checks pass (49 tests). The new
reader also passes all three real downloaded locked archives, with exactly the member/payload/
link counts recorded in PYTHON-INSTALLER.md. An independent streaming tarfile read also matches
every path/kind/mode/size/content SHA-256/link target in all three complete inventories; it
performs no filesystem extraction or archive-code execution. Raw-header inspection confirms only GNU regular,
directory, symlink and long-name records: respectively 10,740/9,449/9,448 nonzero headers,
including 195/109/110 long-name metadata records. Real decompressed totals are
313,845,760/314,654,720/338,022,400 bytes, within the new independent caps.

Current external compileall, staged locks, digest and whitespace checks pass (existing CRLF
warnings); no repository bytecode remains. Staged digest:
`sha256:38831f9462edf70bf7eb1f41af4f7dc4d59b988efcf81e3af8f9da551974b15d`.
The profile is inactive and all K/Q/B completion gates remain open.
All 397 focused tests pass independently on Python 3.11/3.12/3.13. Full reader-stage Windows
suites are terminal: Python 3.11 handle 80435 and Python 3.13 handle 52270 each executed
1,566 tests (1,790 discovered), with 122 failures/422 errors/67 skips. Both retain exactly the
544 untouched Windows baseline failure/error names, none added or removed. Logs are
`mod-base-build-e2e-python-archive-py311.log` and `mod-base-build-e2e-python-archive-py313.log`.
Source/tests remained unchanged through both full runs. A separate real nonzero member-padding
probe rejects for the intended padding error rather than an earlier checksum failure. These
remain failed full gates and do not substitute for the mandatory Linux installation,
UID/process/closure/privileged-launch or workflow/native/canary results.

Installer candidate stage: [PYTHON-INSTALLER.md](PYTHON-INSTALLER.md) records exact publisher
release/asset IDs, tag commits, successful producer run/attempt metadata and the inactive
Ubuntu 24.04 x64 Python 3.11.17/3.12.15/3.13.16 archive hash lock. Hashes came from publisher
release-asset metadata before any archive download; no first-download digest was promoted to
approval. All releases remain mutable, so a tag URL alone is insufficient. The older 3.12.10
asset has no publisher digest and was not selected. All three bounded numeric-ID downloads
matched exact published size/hash; full asset metadata remained identical on reread.
Release/tag/commit and exact producer attempt identity/status also remained unchanged on
post-download API reinspection. Staged locks and refreshed kit digest checks pass:
`sha256:645aee2bdf4401ff7451a90bf7111eaa789609582236d90421fe6304e247cfe6`.
Installer-lock full Windows suites are terminal: Python 3.11 handle 15284 and Python 3.13
handle 45803 each executed 1,555 tests (1,779 discovered), failing with 122 failures/422 errors/
67 skips. Both retain exactly the 544 untouched Windows baseline failure/error names, none
added or removed. Logs: `mod-base-build-e2e-installer-lock-py311.log` and
`mod-base-build-e2e-installer-lock-py313.log` in the host temporary directory. Source/tests
remained unchanged until both suites and the separate check completed. These use the existing
local interpreters, not execution of the new Linux installer archives. The separate 128-test
digest/pin/internal-API/limits run also failed on Windows (2 failures, 12 errors, 9 skips);
external compileall completed and no repository bytecode was found. These are failed gates.
Temporary archives and inert review JSON are in
`C:/Users/nebur/AppData/Local/Temp/mod-base-python-installer-review-oey3vt6v`.
No archive was extracted and no downloaded setup script, binary or library executed.

Streaming member and bounded static ELF inspection changes the next implementation requirement:
the archives contain thousands of bytecode members, writable metadata and (3.11) an ambient
importing .pth file. Their installer scripts choose ambient destinations, delete prior installs
and upgrade pip from the network. Their compiled library search paths are exact hostedtoolcache
version roots; direct ELF dependency names do not establish transitive or dynamic system closure.
The exact counts/script digests/source comparison and their limitations are recorded in the
profile document. Initial ELF parsing rejected the legitimate relocatable python.o member with
no program headers; the corrected inspection permits that exact ELF type/header case. Template
comparison also exposes one additional archive-script final newline, rather than claiming an
exact template reproduction. This candidate lock does not authorize installer execution or
complete runtime enrollment. Closed deterministic install and actual Linux results remain open.

Fixed privileged launch stage: actual root/account admission, matching genuine Invocation and
configuration, installed lock-bound program, retained approved selected tool bytes and enrolled
Python destination precede fixed -I/-B/-S execution of the installed standalone guard. Exact
ordered flags conform to that guard; no command/operation/argument passthrough exists. The
existing bounded administrative controller now accepts an optional fixed cwd; existing callers
keep their original default behavior. A separate central 20-second process bound preserves the
Linux fixture's original deadline, without changing native hook/worker timeouts or source caps.

Program/tool/request reinspection brackets launch. A zero exit must be silent and cannot replace
the sealed receipt: the parent independently checks private sealed ownership, original root
identity, canonical report/receipt inventory and retained source/plan/attempt/input context.
Every authenticated operation exit terminates the admitted validator. Constructor values alone
approve no program, installer or complete runtime closure. The original protected caller must
already be independently admitted; actual approved installer/profile, complete interpreter/
import/system roots, real Linux cancellation/timeout/descendant lifecycle and workflow integration
remain open. The required Linux fixture does not invent an approved current-interpreter digest
to adopt this launcher. No native/App success, release or consumer activation is claimed.

Six new launcher tests cover exact guard argv/cwd/deadline conformance; role-before-account/
launch; admission-before-execution and validator cleanup; config/kit/tool/context drift;
successful-exit/output/receipt/post-launch drift negatives; process/I/O failure normalization.
An additional worker control test verifies fixed cwd with the same clean environment and reaping,
while old callers still omit cwd. Physical filesystem/account/tool/process seams are explicit.
The initial negative test reused a changed argument dictionary across cases; cases now use fresh
arguments so each rejection exercises its intended cause. Initial API checks correctly found
the newly added deadline missing from the bound registry; the exact bound is now documented.
Current launch/worker/internal-API/model-limit checks pass (78 tests). All 386 focused tests
pass independently on Python 3.11/3.12/3.13, including the changed worker module. External
compileall, staged locks, refreshed digest and whitespace pass (existing CRLF warnings); no
repository bytecode remains. Staged digest:
`sha256:9cebd34a164c8c6f130d76e2c898ddc2218eb32dcb0f28b2dd9e5d8f5fdd0bb1`.
Privileged-launch full Windows suites are terminal: Python 3.11 handle 20453 and Python 3.13
handle 48070 each executed 1,555 tests (1,779 discovered), with 122 failures/422 errors/67 skips.
Both retain exactly the 544 untouched Windows baseline failure/error names, none added or
removed. These remain failed gates. Source/tests stayed unchanged through both full runs.
Host temporary logs: `mod-base-build-e2e-privileged-launch-py311.log` and
`mod-base-build-e2e-privileged-launch-py313.log`. These local results do not prove Linux lifecycle,
actual installer/runtime enrollment or production integration.

Read-only primary installer source at the exact setup-python action SHA confirms the remaining
provenance gap: [install-python.ts](https://raw.githubusercontent.com/actions/setup-python/5fda3b95a4ea91299a34e894583c3862153e4b97/src/install-python.ts)
fetches the versions manifest from actions/python-versions main, resolves a release URL,
downloads/extracts its archive and executes its setup script. Pinning that action code does
not provide this launcher's independently approved exact archive/installed-closure digest.
No installer archive was downloaded/executed or newly accepted. A reviewed immutable installer
profile/byte lock and complete runtime root admission remain the next integration prerequisite.

Selected-tool byte admission stage: existing ToolTreeProof metadata/signatures/limits stay
unchanged. New ToolBytesProof and authenticate_toolchain_bytes require independently approved
full selected-closure bytes and explicit actual runner/root host role. The mod-base.tool-bytes-v1
hash binds ordered roots plus sorted absolute ancestor/directory/link/file paths, permission
modes, exact link targets and file sizes/content hashes. Stable no-follow/nonblocking bounded
single-link reads preserve empty files and compare retained/named/open metadata around EOF;
whole metadata/role reinspection brackets admission. No source/tool executes, no digest is
observed-and-approved, and no installer or complete interpreter/import/ELF provenance is implied.
Actual installer profile/digest authority, complete runtime root selection and process-launch
integration remain open; this is a necessary byte check, not complete interpreter enrollment.

Six synthetic syscall tests cover empty/alias/role preservation; same-metadata changed bytes;
mode/link/path binding; hard links, truncation/growth/stamp/I/O failures and descriptor cleanup;
role/proof/type-before-byte-I/O and post-read metadata drift. An initial Windows fixture omitted
O_NONBLOCK; the explicit syscall seam now supplies it without weakening production flags.
Three required Linux tests add real empty/multichunk digest/leak checks, symlink/hardlink/FIFO
rejection and actual named replacement/open-file mutation during real reads. They are unexecuted
locally and use inert temporary bytes, not an approved interpreter or installer. Current local
tool bytes/tool metadata/internal-API checks pass (40 tests). All 346 focused tests pass
independently on Python 3.11/3.12/3.13. External compileall, staged locks, refreshed digest and
whitespace pass (existing CRLF warnings); no repository bytecode remains. Current staged digest:
`sha256:b171a3b47c393a854219a7b95e5a82d408032c511ba7d78377b3feb744273ee3`.
Tool-byte full Windows suites are terminal: Python 3.11 handle 78474 and Python 3.13 handle
38574 each executed 1,548 tests (1,772 discovered), with 122 failures/422 errors/67 skips.
Both retain exactly the 544 untouched Windows baseline failure/error names, none added or
removed. These remain failed gates. Source/tests stayed unchanged through both full runs.
Host temporary logs: `mod-base-build-e2e-tool-bytes-py311.log` and
`mod-base-build-e2e-tool-bytes-py313.log`. No Linux or complete runtime enrollment is claimed.

Read-only installer inventory confirms requirements/ has the Pillow wheel lock and tools/ has
the actionlint archive lock, with no Python interpreter installer/installed-tree approval lock.
The immutable BP Build workflow at the reviewed SHA also pins setup-python action code while
selecting minor version 3.13. These observations do not supply an approved interpreter-byte
digest. The next integration must obtain an independently admitted installer/profile and exact
runtime/import/ELF roots; neither current action selection nor this new observed-tree mechanism
can substitute for that provenance. No consumer executes or changes in this inspection.

Closed privileged entry stage: the installed independent program accepts exactly twelve
ordered scalar/data flag pairs, bounds their bytes/types and numeric host observations, rejects
unknown/duplicate/missing/extra/control arguments before loading, and then byte-admits/loads
the fixed private kit before any kit import. Pre-dispatch failure has fixed bounded nonzero
reporting, including SystemExit(0). Its only operation is fixed request receipt sealing.
The composition root authenticates host fence and runner-owned canonical configuration checkout
below the home fence, reads bounded no-follow default Pages configuration as data, invokes the
existing genuine Invocation factory with only explicit repository/controller/kit identity and
copied kit root, and rechecks config bytes/checkout identity/installed kit SHA/version. It never
imports that checkout's adapter. Composition failure terminates the admitted fixed validator;
the sealing API owns termination after dispatch. Independent original checkout/pin/controller
approval, executing program/interpreter/stdlib enrollment and workflow integration stay open.

The target and aggregate required Linux fixtures now invoke the actual installed program with
-I/-B/-S and closed arguments rather than generated sealing code. Physical Linux execution and
the unchanged 20-second command cost remain unverified. Three new composition tests cover
explicit runtime identity, role/path/owner-before-read, config/root/kit drift and OS normalization.
Four new entry tests cover closed hostile arguments, actual unsupported-host process rejection,
pre-dispatch errors/interrupt/SystemExit and load-before-fixed-dispatch/composition cleanup.
The Windows physical/loading seams are explicit. An initial test factory parameter collided
with the root keyword and an overly broad OS fstat seam affected the real config reader; both
fixture errors were corrected. Subprocess line-ending comparison now normalizes Windows CRLF.
All 340 focused tests pass separately on Python 3.11/3.12/3.13. Both generated setup bodies
compile and both installed-program argument vectors satisfy the exact closed grammar under
external inert capture seams; no Linux/sudo/account/worker/body executes in that capture.
External compileall, staged locks, refreshed digest and whitespace checks pass (existing CRLF
warnings); no repository bytecode remains. Staged digest:
`sha256:ac4d336accdd2021fc0827848f9982400391ccffd264824cc92ed9ab2e9e7814`.
Closed-entry full Windows suites are terminal: Python 3.11 handle 92908 and Python 3.13 handle
79868 each executed 1,542 tests (1,766 discovered), with 122 failures/422 errors/67 skips.
Both have exactly the 544 untouched Windows baseline failure/error names, none added or removed.
These remain failed full gates, not Linux success. Logs in the host temporary directory:
`mod-base-build-e2e-closed-entry-py311.log` and `mod-base-build-e2e-closed-entry-py313.log`.
Source/tests stayed unchanged through both runs. No K/Q/B row is complete from these tests.

Next coupled requirement: actual program/interpreter/stdlib/installer enrollment and protected
workflow admission. Current toolchain proof hashes permission/identity metadata, not executable
bytes or installer provenance. Kit CI pins setup-python action code but selects Python minor
versions; no new interpreter enrollment record or approved installer-byte closure was produced.
Immutable BP source inspection confirms its old sys.path insertion and ambient sys.executable
selection; that is not evidence for the new isolated privileged entry. Consumers remain untouched.

Physical root-request stage: private runner-only exclusive publication and root-only reading
now bind genuine Invocation, original host/validator observations, actual installed kit,
protected controller copy and read-only plan/Build inputs. Source bytes are reconstructed only
from fixed protected files and validated against retained metadata/native configuration;
no source/program is transferred in the request. The fixed sealing library operation retains
the original execution nonce, rechecks physical context after sealing and terminates the
validator on every authenticated operation exit. Production entry/CLI and interpreter/stdlib
enrollment remain open; no workflow/consumer activation or native success is implied.

Seven tests cover publication, reconstruction including empty modules, nonce/canonicality,
source byte/root/request drift, retained host/account/Invocation/installed-kit mismatch,
role-before-I/O and post-seal reinspection/termination. OS/ownership/account seams are explicit
Windows fixtures. The existing aggregate and target Linux lifecycle fixtures now install/load
the fixed private guard/kit and pass a private request nonce into the sealing operation;
their final sealing bodies no longer transfer source objects or mutate sys.path. All four
generated setup/sealing bodies compile using inert external capture seams; no Linux command,
account, worker or generated body executed. Original 20-second command bounds are unchanged
and unmeasured. All 333 focused tests pass independently on Python 3.11/3.12/3.13.
External compileall, staged locks, refreshed digest and whitespace checks pass (existing CRLF
warnings); no repository bytecode remains. Current staged digest:
`sha256:a675b27e7b253018936cf9f975231dd53899b273a3c9ccb8ea6c821bdbab7914`.
Physical-request full Windows suites are terminal: Python 3.11 handle 30872 and Python 3.13
handle 65147 each executed 1,535 tests (1,759 discovered), with 122 failures/422 errors/67 skips.
Both have exactly the 544 untouched Windows baseline failure/error names, none added or removed.
These are failed full gates; they do not substitute for mandatory green Linux results.
Host temporary logs: `mod-base-build-e2e-root-request-physical-py311.log` and
`mod-base-build-e2e-root-request-physical-py313.log`. Current source/tests stayed unchanged
through both full runs. Production closed entry and interpreter/stdlib enrollment are the
next coupled prerequisites; all K/Q/B completion gates remain open.

External isolated/no-site/no-bytecode import probes load the trusted checkout package explicitly
and import root_request on Python 3.11/3.12/3.13. Each loads 40 kit modules from that package,
with 115/115/119 file-backed standard-library modules inside the actual base interpreter root,
no Pillow/third-party import and no sys.path change. The initial probe mistakenly classified
the synthetic __main__ `<stdin>` marker as a filesystem origin; reporting that exact marker
identified the harness error, then a specific main-marker exception corrected the probe.
These Windows observations characterize current imports; they do not enroll interpreter,
stdlib, native extensions, dynamic future imports or Linux installation provenance.

Closed root-request metadata stage: new local-only `mod-base.ci.root-request` v1 (current
1/previous null, explicit predecessor rejection) preserves distinct entry/execution nonces,
retained fixed host boundary and validator UID/GID observations, protected controller source
metadata, existing plan/envelope and exact producer attempt. No program/command/permissions
grant, source bytes, selected import/upload root or operation selector exists. File paths and
modes are inert retained source metadata. The separate local JSON cap is existing plan plus
envelope caps, 2 MiB metadata and 64 KiB framing; native source/config/file/tree/plan/envelope
bounds and disposable UID/GID minimums remain unchanged. Controller/producer/plan binding,
closed exact types, config path, no Git internals, unique/collision-free sources, UID separation
and separate nonces are enforced. Structural metadata establishes no physical origin.

Four new tests cover closed/inexact/unknown fields, typed/ranged boundary/worker observations,
mixed controller/producer/plan/nonces and source paths, existing native/local caps and strict
JSON ambiguity. Current helper/valid fixture, exhaustive kind ledger and a negative mutation
were added without excluding any common writer fixture or old-kind regression. 326 focused
tests pass on Python 3.11/3.12/3.13. External compileall, staged locks, digest and whitespace
pass (existing CRLF warnings). Staged digest:
`sha256:733132c91b78dad817fd5de1cf61d398f798d5967171d38d2d63601b67ad1da2`.
Two test bytecode leaves generated by a fixture command missing the explicit environment were
removed after verifying their exact paths/timestamps; no repository bytecode remains. The
initial computed-path cleanup was automatically rejected; exact verified leaf cleanup passed.

These schema-only results precede the physical request operation above. Production entry,
Linux/hosted proof and all remaining K/Q/B obligations remain open. Schema-stage full Windows
suites are terminal: Python 3.11 handle 24127, Python 3.13 handle 9952 each executed 1,528 tests
(1,752 discovered), with 122 failures/422 errors/67 skips. Their exact 544 failure/error names
equal the untouched Windows baseline, none added or removed. These remain failed gates.
Host temporary logs:
`mod-base-build-e2e-root-request-schema-py311.log` / `mod-base-build-e2e-root-request-schema-py313.log`.

Fixed package loading stage: independent `load_fixed_kit` rejects preloaded mod_base modules,
admits fixed copied bytes before explicit importlib package/submodule-root loading, requires
the approved package version/repository and fixed origins of all loaded kit modules, and
reinspects the bytes after import. It never edits sys.path or dispatches a native operation.
Failed initialization/reinspection removes partial kit modules; a rejected already-loaded
kit is preserved rather than mixed with another copy. The independent program's own trusted
execution and interpreter/stdlib enrollment remain prerequisites.

One new test method runs five actual -I/-B/-S fixture subprocesses: successful package and
submodule import against a rejecting cwd shadow, failed pre/post byte admission, wrong package
version and initialization failure. It verifies exact approved scalar admission, unchanged
sys.path, no bytecode, partial-module cleanup and rejection of a repeated preloaded kit. Only
physical byte admission is an explicit seam; this is Windows import behavior, not Linux proof.
322 focused tests pass separately on Python 3.11/3.12/3.13. External compileall, locks, digest
and whitespace pass, with existing CRLF warnings. Staged digest:
`sha256:f5fabed2806c186e0d64f1b1407ff57476c197f4d296754584fa07f5b8999b57`.

The required Linux fixture now uses this loader from the installed private program in place
of manual copied-package loading. Generated body compiles without executing root/Linux.
Physical outcomes, original 20-second cost, production closed operation/context channel,
interpreter/stdlib/installer/import enrollment and all remaining K/Q/B gates are open.
Fixed-loader Windows full suites are terminal: Python 3.11 handle 38057, Python 3.13 handle
86110 each ran 1,524 tests/discovered 1,748, failed with 122 failures/422 errors/67 skips and
the exact 544 Windows baseline failure/error names, none added or removed. These are failed
gates. The extra managed-bootstrap temporary-cache publication error from the preceding
3.11 run did not recur; its cause remains unproven. This does not erase that historical error.
host temporary logs `mod-base-build-e2e-fixed-load-py311.log` and
`mod-base-build-e2e-fixed-load-py313.log`. These results precede the root-request schema changes.

Lock-bound private program installation stage: `build_ci.bootstrap_installation` uses the
genuinely retained admitted kit and matching validated invocation. Root/kit authentication
precedes source reads. The fixed guard hash comes only from the copied digest-bound staged
tools lock (ASCII canonical lines, sorted unique template/tools paths and exact fixed program
entry), not a candidate-selected path/hash. Source remains behind the runner home fence.
Separate new caps are 256 KiB program bytes and 1 MiB lock bytes; the latter matches the
existing pin lock cap. Exclusive fixed private publication writes one root-owned 0600 leaf
inside 0700 `privileged-bootstrap`, with source/stage/kit/lock/original-root checks. Retained
hash/size/root-identity data permits fixed no-follow reauthentication, with independent record
bytes and metadata/ACL/root checks around repeated admitted-kit/lock verification. No copied
program executes in either library API, and no document kind or CLI changed.

Six new local tests cover lock grammar/fixed enrollment/uniqueness, exact private exclusive
publication, wrong source or invocation before publication, source/stage mutation, hash/count/
original-inode/record drift, role-before-I/O and normalized filesystem errors. Windows seams
remain explicit. 321 focused tests pass on Python 3.11/3.12/3.13. External compileall, staged
locks, digest and whitespace pass (CRLF warnings in INTERNAL-API and schema-evolution files).
New staged digest: `sha256:58daef9fd1b4dcfb2bf85c4290a74b8cf31342131063a24b07f3a5481ae953b9`.

Required Linux fixture now actually installs/reauthenticates the lock-bound private program,
rejects a repeated install, loads those installed program bytes after unloading original kit
modules and requires independent kit admission before importing the private kit. Both worker
UIDs must be denied reading the program directory/leaf. Generated body compiles without
Linux/root execution; actual physical outcomes and unchanged 20-second timing remain pending.
Independent old executing-caller pin/source provenance, interpreter/stdlib installation and
complete import closure, fixed production dispatch/context integration and every remaining
K/Q/B gate remain required. Program-installation full suites are terminal: handles 99072
(Python 3.11) and 73871 (Python 3.13), 1,523 executed/1,747 discovered each. Python 3.13 has
122 failures/422 errors/67 skips and the exact 544 baseline failure/error names. Python 3.11
has 122 failures/423 errors/67 skips and 545 names: the baseline plus the managed-bootstrap
subtest of `test_global_git_configuration_is_ignored`, which failed publishing a freshly
fetched temporary kit cache. Its original traceback reports KitError at fetch publication;
the underlying filesystem cause is not established. The unchanged test passes on two separate
serial Python 3.11 reruns with verified Git Bash PATH. Those passes do not erase the extra
full-suite error or prove its cause. Logs `mod-base-build-e2e-program-install-py311.log` and
`mod-base-build-e2e-program-install-py313.log` remain in the host temporary directory.
All full suites remain failed gates; the later loader source changes require their own run.

Independent pre-import guard stage: new `tools/ci_privileged_bootstrap.py` imports only stdlib
and receives explicit independently approved executing SHA/version/digest. Fixed installation
and record paths cannot be selected by document data. Linux root and -I/-B/-S are required;
strict closed canonical record identity, 4 KiB cap, exact root-owned private metadata without
ACLs, bounded no-follow file discovery/reads, bytecode/.pth rejection, kit-digest-v1/counts and
retained original root identity precede admission. Record bytes and named roots are rechecked.
BootstrapError intentionally cannot import kit errors before admission. Mirrored constants
are tested against the central grammar/limits; staged tools lock binds this file's bytes.

Eight new tests cover contract parity, approved record identity/canonical/closed types,
actual -I/-B/-S stdlib-only loading with unchanged sys.path and no mod_base imports,
role-before-filesystem rejection, private metadata/ACL checks, fixed path/read/hash/root
brackets and descriptor cleanup (including failure opening the second root), original digest
listing including empty source, whole-tree caps/poison paths and bounded file read/drift.
Windows descriptor/metadata seams remain explicit. 315 focused tests pass separately on
Python 3.11/3.12/3.13. External compileall, staged locks, digest and whitespace checks pass
with the same schema-evolution CRLF warning. New staged digest:
`sha256:f7708f511c1426c5165cc68a528033b821d3699abeeea3bb85fed58296a5313e`.

The required Linux fixture now loads the independent guard before any mod_base module,
requires its rejection of each physical record adversary, unloads original kit modules and
requires independent byte admission before importing the private copy. Generated body
compiles without root/Linux execution; physical behavior and unchanged 20-second timing
remain unverified. Fixture loading from the trusted checkout is not production enrollment.
This guard does not enroll/authenticate its own program/interpreter/stdlib provenance,
authorize a pin, establish the host fence or dispatch native work. Independent protected
program installation, complete import/installer closure and production entry integration
remain mandatory. No K/Q/B row is complete. The pre-import-stage Windows full suites are terminal:
Python 3.11 handle 29083, Python 3.13 handle 70481 each ran 1,517 tests and discovered 1,741.
Both failed with 122 failures, 422 errors and 67 skips. Their exact 544 failure/error names
match the Windows baseline, with no added/removed names. These remain failed gates. Logs
`mod-base-build-e2e-preimport-py311.log` / `mod-base-build-e2e-preimport-py313.log` in the host
temporary directory. Those results precede the later program-installation source changes.

Private root installation-record stage: new local-only mod-base.ci.kit-installation v1 has a
fixed kit ref/digest, bounded file/byte counts and unsigned 64-bit original root device/inode.
Its separate local JSON cap is 4 KiB. No program/path/hook/permission/status field exists.
The exhaustive ledger advertises current 1/previous null and explicitly rejects the new kind
under the immutable v1.0.3 reader; unchanged common writer compatibility remains tested.
Root-only fixed private publication authenticates the actual retained copy before/inside an
exclusive atomic 0700 directory/0600 single-leaf write, with independent bytes and metadata/ACL
rechecks. Reading authenticates root role/layout/private metadata, bounded stable no-follow
bytes, strict canonical JSON/fields, the actual installed copy's digest/counts/original inode,
and record bytes/metadata around that admission. The shared descriptor reader now examines
at most two directory entries and retains the runner execution channel's original cap/checks.

Six new local tests cover closed types/widths/fields/versions/bounds, exact fixed private
publication, reconstruction/canonical/duplicate/nonfinite/malformed data, copy/record drift,
role-before-I/O rejection, normalization and descriptor ownership/mode/link/size/entry/identity
checks. Windows OS/copy/atomic seams are explicit. 307 focused tests pass separately on
Python 3.11/3.12/3.13 (the preceding 301 plus six methods). External compileall, staged locks,
digest and whitespace pass, with the existing schema-evolution CRLF warning. Current digest:
`sha256:758da1b6cd172d96cfc583f2c4134f378465f1f19785676c340858c1ebddf31d`.

The required Linux private-copy fixture also publishes/reads the actual root record, rejects
reuse and denies both worker UIDs access to its directory and leaf before explicitly importing
the admitted copy under -I -B -S. Its generated body compiles without executing Linux/root.
All physical Linux cases and hosted cost remain unverified. This protected-caller API already
runs from an admitted kit; it is not the independent pre-import bootstrap or production root
entry. Genuine retained installation/pin/source provenance, interpreter/stdlib/installer/
program enrollment and native/final workflow/App authority remain separate mandatory work.
Neither root file ownership nor constructed data proves those claims. All K/Q/B rows stay open.
Final installation-record Windows full suites are terminal with verified Git Bash PATH:
Python 3.11 handle 60905 and Python 3.13 handle 15862 each executed 1,509 tests, discovered
1,733, and failed with 122 failures, 422 errors and 67 skips. Their 544 failure/error names
equal the Windows Python 3.12 baseline exactly, with none added or removed. These are failed
full-suite gates, not green checks. Logs `mod-base-build-e2e-kit-record-py311.log`
and `mod-base-build-e2e-kit-record-py313.log` are in the host temporary directory.

After those suites terminated, the required Linux private-kit fixture gained physical record
adversaries: incorrect leaf/directory modes, runner-owned leaf, extra/missing entries, symbolic
and hard links, FIFO, noncanonical bytes, duplicate/nonfinite JSON, malformed UTF-8, over-cap
bytes and wrong retained root inode. Each must raise MbError; restoration is followed by a
successful original-record read before importing the installed copy. The unchanged 20-second
fixture command bound still applies. Its generated body was captured without root execution,
compiled and parsed on Windows (`mod-base-kit-record-linux-body.py` in the host temporary
directory); this proves syntax only. Physical rejection and hosted timing remain unverified.
The 44 installation/record/handoff/internal-API tests pass on Python 3.11/3.12/3.13 after this
test-only change. External compileall, digest and both whitespace checks pass (same existing
CRLF warning). No digested source changed; the digest above remains current. No second full
Windows run is claimed for this Linux-only fixture expansion.

Private privileged-kit installation stage: new inactive install/recheck APIs receive a validated
Invocation and independently approved digest, admit protected root role before source I/O,
require runner-home-fenced source and copy only the three kit-digest-v1 roots into one fresh
fixed root-owned private directory. No linked/executable/special/bytecode/.pth entry is admitted.
Empty source leaves are preserved. Bounded descriptor discovery precedes source hashing; the
global 20,000-file/512 MiB limits match existing kit digest bounds, with an additional 40,000-entry
cap and existing depth cap. Independent stage hashes/counts, retained-source reinspection,
exact three-root shape, original root device/inode, private metadata checks before/after reads,
stable named roots and final role/layout checks precede exclusive publication/reauthentication.
The executing pin/digest must already be independently admitted by the protected caller.
These APIs do not import copied Python, install tools, enroll interpreter/stdlib or authorize
release, native execution, status authority or a constructed receipt's SHA/version claims.

Eight new Windows tests exercise listing parity with the existing digest (including empty
source), global caps, mode/link/bytecode/.pth/special rejection, bounded discovery/root replacement,
copy/source hash drift, private fixed publication, role/path/digest/receipt type/count/metadata/
inode/I/O failures and cleanup. OS/atomic/copy seams are explicit. 301 focused tests pass on
Python 3.11/3.12/3.13: the preceding 277 plus eight installation tests and 16 existing model-limit
tests, not 24 new methods. Internal API ownership/signatures/cross-unit uses and all new bounds
are documented; no released signature, document kind or Pages format changed.

The new required Linux fixture performs actual root-owned copying from the trusted test checkout,
rejects installation reuse and identical-byte replacement of the original root inode, denies
both worker UIDs read access, then explicitly loads the independently admitted copied package
under -I -B -S with unchanged sys.path and no Pillow. Its synthetic kit SHA is metadata only,
not a released pin; generated -c code is still a test harness, not the production privileged
program. The generated body has been compiled without executing it. Actual Linux execution
and hosted installation cost remain unverified. Interpreter/stdlib/installer enrollment,
production launcher/context integration and genuine source/execution/native/App authority remain
mandatory. No K/Q/B step is closed by these primitives.

Current staged source digest:
`sha256:f7f76e38f1062eee97cedf4fd4b032e98d140b43af7bb3bf01acf4d55ec3650f`.
External compileall, staged locks, digest and whitespace checks pass (existing CRLF warning).
Final current-tree Windows full suites finished with verified Git Bash PATH: Python 3.11
handle 77702 and Python 3.13 handle 44543 both report suite exit 1. Each executes 1503 of
1727 discovered tests with 122 failures, 422 errors and 67 skips. All 544 failure/error names
exactly equal the saved Windows baseline, with no additions or removals. Logs
`mod-base-build-e2e-private-kit-py311.log` and `mod-base-build-e2e-private-kit-py313.log` are in
the host temporary directory. These remain failed full-suite gates, not Linux passes.

Execution-tool destination binding stage: dispatch now resolves the selected Python/JDK paths
through the bounded no-follow scanner after full tool-proof reinspection. Both textual paths
and resolved destinations must belong to explicitly enrolled roots (resolved root aliases
are supported). Permitted system-link prefixes alone never enroll an execution/import tree.
Python must be a nonempty regular file with other-execute permission; JAVA_HOME must be a
directory with other-traverse permission, matching the separate fresh UIDs with no trusted
supplementary groups. Resolution I/O errors become WorkerError; failed admission terminates
the account without invoking isolated dispatch. This does not prove installer hashes,
interpreter semantics, complete import enrollment or privileged launcher provenance.

Four additional toolchain test methods cover internal/root/cross-root aliases, unenrolled
Python/JDK destinations, empty/non-executable/wrong-type paths, normalized I/O rejection and
the full fence's refusal/cleanup before launch. Filesystem and launch seams are explicit on
Windows. 277 focused tests pass separately on Python 3.11/3.12/3.13 (the preceding 265 plus
all 12 toolchain methods, of which eight existed already). After strengthening the integrated
negative/positive launch case, all 12 toolchain tests pass again on all three versions.
The required real Linux complete-Python-prefix fixture additionally checks actual destination
binding and directory-as-Python/file-as-JAVA_HOME rejection before its genuine fenced dispatch;
it remains unexecuted, and local alias simulations are not physical Linux evidence.
External compileall, staged locks, digest and whitespace pass, with the existing CRLF warning.
Current staged source digest:
`sha256:88d7735e914903954c73640cc8ea4173ebcd54df4ca37ad64605d8487495196b`.
Final current-tree Windows full suites finished at 19:59 CEST: Python 3.11 handle 23304,
Python 3.13 handle 35826, both with verified Git Bash PATH, both reporting suite exit 1.
Each executes 1495 of 1719 discovered tests with 122 failures, 422 errors and 67 skips.
All 544 failure/error names exactly equal the saved Windows baseline, with no additions or
removals. Logs `mod-base-build-e2e-tool-destinations-py311.log` and
`mod-base-build-e2e-tool-destinations-py313.log` live in the host temporary directory.
These remain failed full-suite gates, not Linux passes. All K/Q/B rows remain open.

Independent fixed-import feasibility probe during those suites: fresh Python 3.11/3.12/3.13
processes with -I -B -S explicitly load the trusted checkout package using an importlib package
spec and its explicit submodule search location, without editing sys.path. All three import
the freeze entry and its 28 kit modules from that package root, leave sys.path unchanged and
do not load Pillow. A temporary cwd holds rejecting mod_base/sitecustomize/usercustomize
shadows; hostile PYTHONPATH/PYTHONHOME/PYTHONUSERBASE point there and are ignored by isolated
startup. The recorded module lists are in host-temp `mod-base-fixed-import-probe-results.json`.
This is an executed Windows feasibility check only: it does not enroll the interpreter,
stdlib, source installation, privileged program or context, nor prove Linux role/permission
isolation. Production must still authenticate those physical inputs before the explicit import
and use a fixed validated invocation/data channel, never fixture-generated -c code.

Private execution-data handoff stage: new local-only mod-base.ci.execution v1 carries exactly
protected run/attempt, fresh 256-bit nonce, plan/source-config/input hashes, exact successful
exit code, truncation flag and canonical base64 binary log under the original 16 MiB log bound.
Its separate local JSON cap is the maximum encoded log plus 64 KiB metadata; artifact/receipt
caps and Pages schemas stay unchanged. Exhaustive current-writer/predecessor testing advertises
current 1/previous null and explicitly rejects the new kind in v1.0.3.
The runner-only writer publishes a new fixed private 0700 directory/0600 single-leaf record,
never overwriting an existing channel. Root-side freeze admits the fixed runner-owned no-follow
directory/file, exact permissions/single link/type/size, stable descriptor/named-file/root
identity and timestamps, canonical strict JSON and exact nonce/attempt/source/plan/input.
Only reconstructed data reaches existing input-bracketed independent receipt freeze; admitted
validator cleanup also runs on rejection/I/O failure. No program/path/hook/status is a field.

This replaces the actual execution-result repr transfer in the two required synthetic Linux
target/aggregate fixtures with a protected data channel. Their context/root helper remains a
test harness, not an enrolled production CLI. A new required Linux metadata-only adversarial
case rejects channel reuse, wrong nonce, directory/file mode changes, hard/symbolic links,
oversize/noncanonical records, foreign ownership and identical-byte inode replacement during
the real root descriptor read. Both worker UIDs are denied access in the actual-execution
fixtures. All these Linux cases remain unexecuted on this Windows host.
Genuine protected runner source/execution provenance, privileged-program/interpreter/import/
installer enrollment, native closed schemas/semantics and final workflow/API/App authority
remain independent mandatory requirements. Constructible successful objects are not execution
provenance; physical channel origin is not native validity or approval authority.

265 focused source/readiness/handoff/binding/validation/codec/stream/selection/assembly/transport/
record/input/protocol/internal-API/schema/valid-and-mutation fixture tests pass separately on
Python 3.11/3.12/3.13. This adds eight handoff tests and includes three existing mutation-fixture
tests in the focused set, not eleven new methods. Windows filesystem/role/freeze seams are
explicit. Compileall uses external bytecode; staged locks, digest and whitespace pass, with
the existing tests/test_schema_evolution.py CRLF warning. Current digest:
`sha256:f5ac566998804e9ba266037fcabc5978151d39bcac095627996807cb886bdc96`.
Full current-tree Windows suites with verified Git Bash finished: Python 3.11 handle 87797 and
Python 3.13 handle 81638 both report suite exit 1. Each executes 1491 of 1715 discovered tests
with 122 failures, 422 errors and 67 skips. All 544 failure/error names equal the saved Windows
baseline exactly, with no additions or removals. Logs
`mod-base-build-e2e-execution-handoff-py311.log` and
`mod-base-build-e2e-execution-handoff-py313.log` are in the host temporary directory.
These are failed full-suite gates, not Linux passes. No K/Q/B completion row is closed by these
local checks.

Rechecked at 19:48 CEST on 2026-10-07: both original suite handles still return live sessions;
their logs continue advancing and neither has a terminal summary. No suite was restarted and
no source/test edits were made during this observation. Independent staged-lock, digest and
working/index whitespace checks completed successfully against the same handoff digest above
(the existing schema-evolution CRLF warning remains). This is a verified wait, not a passing
full-suite gate or Linux boundary evidence.
Both handles subsequently returned terminal results at 19:49 CEST; the completed comparison
above supersedes that live observation.

Pre-plan PR readiness stage: read_pr_generation binds an open same-repository PR/default base
to the caller's protected executing controller SHA and independently rechecks default/controller/
tree and number/head/base branches/SHAs/draft/nullable API merge hint. Frozen scalar fields do not
alias raw API dictionaries. Reject malformed exact types, foreign/fork/closed/moved sources and
API errors. Drafts and unavailable merges consume no candidate Git objects or worker actions.
Existing authenticate_pr_identity shares the same observed generation, independently verifies
the ordered merge parents/tree, then rechecks PR generation and default/controller before return.
Public existing signatures remain unchanged. Ready execution still needs protected policy/pin/
plan/approval admission and future effects need repeated source checks; API events are not atomic
with observation. This does not implement deferred graphs/workflows or status reconciliation.
Ten new cases cover ready/draft/unavailable merge observations, input/response rejection before
effects, same-head readiness/head/base/merge changes, controller/tree/default movement, API
failure, immutable retention and refusal to use observations as full-ready admission. Late
generation/default changes during full merge Git reads reject through the existing public API.
254 expanded focused source/readiness/binding/validation/codec/stream/selection/assembly/
transport/record/input/protocol/internal-API/compatibility tests pass on Python 3.11/3.12/3.13.
This adds ten new readiness cases and includes 14 existing source tests in the focused set;
it is not 24 new tests. Compileall with external bytecode, locks, digest and whitespace pass
(the existing CRLF warning remains). Current digest:
`sha256:0eec8177633e075af22b4526c9b7a106cebb57ef19c6053d6e3c635e0a042579`.
Full Windows suites with verified Git Bash are terminal: Python 3.11 handle 20558 and Python 3.13
handle 34761, each reporting suite exit 1. Logs `mod-base-build-e2e-pr-generation-py311.log` and
`mod-base-build-e2e-pr-generation-py313.log` are in the host temporary directory.
Each executed 1483 of 1707 discovered tests, with 122 failures, 422 errors and 67 skips.
BOM-aware parsing proves both exact 544 failure/error-name sets equal baseline, none added or
removed. These remain failed full gates. Linux/hosted/native/workflow/adoption/release gates
and all K/Q/B completion rows remain open.

Native Build conformance inspection found and corrected a concrete bound mismatch. At immutable
Block Pops `47a890ae46a2878fb08d29a932803ab91bccdcd9`,
`scripts/release/build_evidence.py:MAX_REPORT_BYTES` and
`scripts/release/build_matrix.py:_observation_report` admit the original compiler report up to
8 MiB. The producer exports `build/build-matrix-report.json` in `build-gate.yml`.
The prior shared `records.validate_build_envelope` instead applied the 4 MiB
`MAX_CI_REPORT_BYTES` limit to every native-report payload. This contradicts preserving the
native report contract; it is not evidence that all source reports fit the smaller bound.
The shared Build envelope now uses a closed profile-specific native-report payload bound:
Block Pops 8 MiB; Quick Skin retains its initial inactive 4 MiB transport limit, still requiring
native conformance proof. Validator-output receipt/report limits remain independent at 4 MiB;
whole-export/JAR/log/archive limits are unchanged. Boundary regressions cover exact/one-byte-over
limits in both complete and target envelopes with protected plans, and refusal to expand validator
outputs. This unreleased initial-format correction started only after both full runs ended.
230 focused binding/validation/codec/stream/selection/assembly/transport/records/input/protocol/
internal-API/compatibility tests pass separately on Python 3.11/3.12/3.13, including two new
boundary cases. The first expanded runs exposed two missing INTERNAL-API bound declarations;
those were documented and all three full focused sets rerun successfully. Compileall with
external bytecode, staged locks, digest and whitespace checks pass (the existing CRLF warning
for tests/test_schema_evolution.py remains). Digest:
`sha256:8086b6590d700da4b266a9784450567ef977d4f675e69ca9b302307994ea04f4`.
Full Windows suites with the verified Git Bash PATH are terminal: Python 3.11 handle 38739,
Python 3.13 handle 30136, each reporting suite exit 1. Logs `mod-base-build-e2e-native-report-py311.log` and
`mod-base-build-e2e-native-report-py313.log` are in the host temporary directory.
Each executed 1473 of 1697 discovered tests, with 122 failures, 422 errors and 67 skips.
BOM-aware parsing proves both exact 544 failure/error-name sets equal baseline, none added or
removed. These remain failed full gates; no full success or Linux/native proof is claimed.

The same inert inspection distinguishes Block Pops' durable
`read_lane_build_evidence` from live `prepare_observation`/`validate_observation`: historical
report parsing rederives commands, toolchain bindings, compiler/class scope and production/harness
digests, but explicitly does not reproduce the old process or establish freshness. Retained
lease-bound JVM/compiler/JDK observations therefore remain an independent native prerequisite.
Quick Skin's `build_matrix.execute_build` records serial target commands/output hashes and
partial scope; `assemble_build.assemble` revalidates the complete target partition and every
production/harness pair before rebuilding the complete manifest. An adapter must preserve both
layers; neither a zero exit code nor shared byte assembly replaces the packaging gate.
These are source findings only, not native conformance test results or consumer activation.

Build-execution/receipt-binding stage: freeze_frozen_build_validation authenticates the root-side
host/account role and retains an independent validated plan/envelope. Require the genuine retained
successful bounded BuildValidationExecution and exact canonical input SHA/producing run/attempt.
Derive the closed aggregate/target hook and unit from that envelope. Inspect the fixed read-only
plan/Build ownership, access, canonical bytes and root inode identities before and after existing
independent validation output freezing; reject missing/substituted/drifted inputs and always
quiesce the admitted validator. Existing runner-side execution and generic root freeze signatures
are unchanged, sharing only role-neutral metadata/byte inspection after their own host admission.

This connects currently retained execution context to receipt sealing; it is not a native domain
verifier implementation or proof that a constructible successful object came from execution.
Genuine protected source/execution provenance must survive the privileged transition. Production
authenticated IPC/CLI/installer/import enrollment remains missing. A late error may leave a private
freeze copy; no receipt is returned and that failure cannot authorize consumption/upload. Native
compiler/JDK/report contract semantics, API graph/source/policy/approval/status authority and all
workflow/consumer/release completion gates remain required and unimplemented/unverified.

228 focused execution-binding/validation/codec/stream/latest-download/selection/assembly/selected-
copy/gate/record/export/transport/input/protocol/internal-API/predecessor/exact-builder tests pass
on Python 3.11/3.12/3.13. This expands the previous 210-module set by 12 existing validation tests
plus six new context cases, not 18 new tests. New cases cover aggregate/target-derived context,
wrong execution/digest/nonzero/run/attempt/envelope, unavailable/drifted input identities/bytes,
freeze failure, retained caller data and role rejection before account effects. Account/filesystem/
freeze operations are explicit unit seams; these cases establish no native or Linux boundary.
The existing real aggregate/target Linux fixture now passes its actual returned bound execution
to the trusted root test helper and exercises this input-bracketed freeze. It is still unexecuted;
its protected repr transfer is a test harness, not an approved generic privilege bridge.
Compileall with external bytecode, locks and digest pass. Digest:
`sha256:b1449fee2e033505abe7de7963ea55b3e7bab7a3498e164494cd9688c0f12411`.
The earlier full Windows handles 47350/17431 are missing; current process inspection finds no
matching suite processes, and both validationbinding-py311/py313 logs end without final summary.
Their termination cause is unknown; these preserved partial logs prove no completed full result.
The unchanged tree completed fresh full Windows runs with verified Git Bash: Python 3.11
session 69204 and Python 3.13 session 15351 are terminal, each reporting suite exit 1. Logs:
`mod-base-build-e2e-validationbinding-final-py311.log` and
`mod-base-build-e2e-validationbinding-final-py313.log` in the host temporary directory.
Each executed 1471 of 1695 discovered tests: 122 failures, 422 errors and 67 skips.
BOM-aware parsing proves both exact 544 failure/error-name sets equal the preserved baseline,
with none added or removed. These remain failed full gates; all Linux/native/hosted and K/Q/B
gates remain open. No observation timeout was treated as terminal or used alone to restart a
live run. The native bound correction below begins only after these terminal results.

Local-Build-archive stage: encode_build_export verifies the canonical complete/target export,
streams its sorted exact payload/envelope inventory with independent byte hashes into one private
stored ZIP, and enforces the existing 512 MiB compressed ceiling before every physical write,
including headers and central directory. Fixed timestamps/regular-file metadata and stored
compression preserve the inspected Quick Skin c0cdc01ab20f1eac663c628011520c76fc7e3d7a Build
workflow's compression-level 0 choice. This immutable consumer source was read only; no consumer
code was executed or changed. Independently decode with the strict CI extractor, reverify the
actual canonical envelope/payload, recheck original source and hash the bounded ZIP before
exclusive atomic directory publication. Output contains only the fixed local ci-export.zip.
No whole expanded file/ZIP is allocated. Root overlap, changed bytes/source, unsafe links,
partial/non-progress writes and oversize encoding reject without publishing.

New MB1 stream_child_file uses bounded existing no-follow descriptor streaming, and additionally
rechecks the named child's identity/size/type/single-link status after the protected consumer.
Existing whole-file/hash/inventory readers and signatures are unchanged. Protected source
quiescence and ancestry exclusion remain caller obligations, not guarantees from a hash.

The local ZIP/metadata is neither a GitHub artifact descriptor nor native/sealing/status authority.
Uploading it as one file would add a nested wrapper that the current root-envelope transport
rejects. Actual approved uploader/action integration must preserve root encoding and separately
authenticate the service artifact's real compressed size/digest/owner/window; local ZIP size/hash
cannot stand in for those API fields. No upload workflow, direct SDK uploader or consumer profile
is activated. Native compiler/report/receipt validity and every K/Q/B completion gate stay open.

210 focused codec/stream/latest-download/selection/assembly/selected-copy/gate/record/export/
transport/input/protocol/internal-API/predecessor/exact-builder tests pass separately on Python
3.11/3.12/3.13. Six new focused tests exercise actual ZIP byte equality/entry contents/order/
metadata, complete archive cap including central directory, streamed/late source drift, overlap,
short/zero-progress sink writes and pre-I/O path/bound/consumer rejection. Windows filesystem/
hash/extraction admission seams are explicit; they prove no POSIX boundary. Three new required
Linux cases cover real encoding/strict round-trip, compressed cap/late source cleanup and bounded
child chunks with pre-existing and mid-stream links. None has run. Compileall uses external
bytecode storage; locks and digest pass. Digest:
`sha256:6a1b7674003b108504b451f4e13e5956ef15346ab7898777baf93d3bd9ce0604`.
Full Windows suites with the previously verified Git Bash completed: Python 3.11 session 28465
and Python 3.13 session 79565 are terminal (suite exit 1). Logs:
`mod-base-build-e2e-archive-py311.log` and `mod-base-build-e2e-archive-py313.log` in the host
temporary directory. Each executed 1465 of 1689 discovered tests, with 122 failures, 422 errors
and 67 skips. BOM-aware parsing proves both exact 544 failure/error-name sets equal baseline,
none added or removed. These remain failed full gates. Final whitespace/digest/lock checks pass;
Linux/native/hosted outcomes and all remaining K/Q/B completion gates remain missing/open.

Latest-PR-Build-download composition: download_latest_pr_build retains an independent protected
plan, waits/selects the exact newest successful complete producer and revalidates before immutable
numeric-ID download. Existing full tuple/canonical envelope, API/ZIP digest, graph/kit/upload and
payload inventory checks apply. A fixed protected callback repeats exact newest/source/descriptor
admission inside the independent private atomic copy transaction, followed by staged-byte
reinspection before exclusive publication. A newer producer during fetch or copy rejects and
owned temporary trees are cleaned. Existing descriptor-based complete/target transport and public
materialize_build_export signatures/behavior remain unchanged. No arbitrary candidate callback,
compiler fallback, native receipt, status authority or activated workflow is introduced.

The returned descriptor/envelope data must be retained for later native validation and rechecked
around final consumption; this composition cannot make future API events atomic with publication.
Caller still protects output ancestors and both worker UIDs' exclusion. Transport/native bounds
remain independent from the selection wait budget. CLI/caller/native second-validator/receipt
integration, non-PR authorization and all remaining K/Q/B gates remain incomplete.

204 focused latest-download/selection/assembly/selected-copy/gate-transport/timeline/records/export/
transport/input/protocol/internal-API/predecessor/exact-builder tests pass on Python 3.11/3.12/3.13.
Five new Windows tests use explicit filesystem seams with actual fake API authentication and
numeric ZIP data: correct descriptor/envelope publication, supersession before fetch, new producer
during download/copy with no output, source/digest/final-stage rejection and retained-plan mutation.
Two required Linux CI cases exercise real extraction/copy/atomic publication and a new-run race
after actual copying with no stage residue. Neither Linux case has executed; these are additional
required cases, not POSIX/native completion evidence. Compileall with external bytecode storage,
locks and digest checks pass. Digest:
`sha256:3ab49cab53e495b8f8a8fce0e8077648d84d0a4499667c988937d46e75ba3317`.

Read-only host inspection identifies the earlier hung Bash as the WindowsApps WSL alias and
confirms C:/Program Files/Git/bin/bash.exe is installed and reports Bash 5.3.9. Only each test
command's PATH now prepends that existing Git bin directory; no installation, service, global
environment or runtime source PATH policy changes. Full Windows suites using this verified Bash
completed: Python 3.11 session 38048 and Python 3.13 session 36210 are terminal (suite exit 1). Logs:
`mod-base-build-e2e-latestdownload-py311.log` and
`mod-base-build-e2e-latestdownload-py313.log` in the host temporary directory.
Both executed 1459 of 1683 discovered tests: 122 failures, 422 errors and 67 skips. BOM-aware
log parsing proves both exact 544 failure/error-name sets equal the original Windows baseline,
with none added or removed. These are failed full gates; Git Bash supplies no real Linux/hosted
boundary evidence. The final 204 focused runs, compileall, locks, digest and whitespace checks
pass; every remaining K/Q/B completion gate stays open. No activation, consumer change or release.

Bounded-PR-Build-wait stage: wait_for_latest_pr_build retains an independent validated plan,
repeats complete exact newest/source selection while absent/pending, and enforces the protected
5400-second monotonic admission deadline. Fixed 60-second sleeps are clipped to remaining time;
an independent 91-observation cap prevents unlimited reads with a broken runtime clock/sleeper.
Invalid/backwards clocks reject. API errors, corrupt metadata and newest failed producers remain
fatal; source changes do not switch to another tuple. Cancellation propagates, and sleep OS errors
fail visibly. Exhaustion requests complete Build/E2E recovery with no independent PR compiler.
No observation starts at/after the deadline and successful observations finishing then cannot
admit. An already in-flight API read may finish later under the client's existing bounded timeout/
retry policy; the helper neither interrupts it nor accepts its late result. This is a bounded
admission budget, not proof that all network calls terminate precisely at 5400 seconds.

revalidate_latest_pr_build repeats complete producer/source/newest/metadata authentication and
requires exact descriptor equality around consumption. New pending/failed/successful producers,
changed attempts or immutable metadata invalidate a retained selection. Production download/
native verification and final gate callers still must invoke it before/after consumption; no
workflow has been wired or enabled. Payload/native validity, status authority and non-PR request/
nonce routing remain separate requirements. Future events after the recheck are not atomic with it.

199 focused selection/assembly/selected-copy/gate-transport/timeline/record/export/transport/input/
protocol/internal-API/predecessor/exact-builder tests pass separately on Python 3.11/3.12/3.13.
Nine new tests cover immediate success, pending completion, absent deadline/exact read count,
slow API late success, clipped last sleep, next-poll failed producer/moved source, API/sleep errors,
invalid clocks/observation caps, cancellation/retained plan and changed consumption descriptors.
These clock/fake-API tests establish no hosted or native authority. Compileall with external
bytecode storage, staged locks, digest and whitespace checks pass. Current digest:
`sha256:c645b6f61e431eaf07adca25d879bd1d58a71a7641b30f6ffba5933a4575fe96`.
The final focused runs include finite integer clocks without overflowing a float conversion;
all three 199-test runs completed after that correction. Full Windows suites were interrupted:
Python 3.11 session 71293 and Python 3.13 session 89897 are terminal (suite exit 1).
Logs: `mod-base-build-e2e-buildwait-py311.log` and `mod-base-build-e2e-buildwait-py313.log` in the
host temporary directory. Their workers stalled in the existing Bash version probe despite its
30-second subprocess timeout: 32 owned WSL processes remained live. Read-only current-state
inspection found WslService running and no registered user distribution. Exact parent/creation
identity checks scoped cleanup to the two owned suite roots and 66 descendants; no service or
unrelated process was changed. Final partial counts were 567/703 executed respectively, of 1678
discovered, with BrokenProcessPool failures after explicit interruption. These are incomplete,
failed gates, not baseline-equivalent full results. Root PIDs 63840/60196/22620/41048 were then
absent. The previous selection-stage completed baseline comparison remains historical evidence;
it does not verify this stage's full suite. Full Windows/Linux validation remains pending.
All K/Q/B completion gates, Linux/hosted/native validation, activation and release remain open.

Newest-PR-Build-selection stage: select_latest_pr_build lists the independently enrolled
protected workflow without status/conclusion filtering, chooses the newest exact PR generation
by creation/run ID/latest attempt, and only then checks success. The closed initial v1 run-title
marker binds profile/PR/head/base/tested SHA as a selection hint, never as embedded tuple proof.
API controller head and tested SHA remain separate. Unknown/legacy titles fail closed;
other valid PR tuples do not qualify. Failed/cancelled/neutral/skipped newest runs cannot fall
back to older successes. Absent or valid pending runs return no admission and no compile route.

For a successful producer, authenticate exact workflow ID/path/event/ref/controller/latest
attempt, executing kit, complete graph and actual aggregate upload step. Require one exact-name
immutable artifact, bind its metadata/expiry/owner to the expected plan, then repeat newest-run,
attempt and live-source authentication before returning the descriptor. New runs/attempts,
workflow-ID drift, missing/duplicate/expired bundles and deferred graphs reject. Native payload
and embedded full-tuple validity still require actual download and independent verification.
The protected caller's run-name emission and hosted API head/title semantics remain unverified;
no caller is activated. Bounded 5400-second polling, consumption-time newest revalidation,
non-PR request/nonce selection and status authority remain required work.

190 focused selection/assembly/selected-copy/gate-transport/timeline/records/export/transport/
input/protocol/internal-API/predecessor/exact-builder tests passed on Python 3.11/3.12/3.13.
The 16 selection cases cover newest failures and pending states, exact tuple/title parsing,
workflow/API query enrollment, immutable bundle metadata, graph/source/kit failures and races.
These fake API tests establish no real hosted/native/Linux authority. A subsequent Python 3.12
selection/internal-API recheck passes 38 tests; staged locks and digest checks pass.
Digest: `sha256:e4c800f4595d6484f0f2fac5c33abb99713bb90341fe49f1b8f3b5af00763ee0`.
Full Windows Python 3.11/3.13 each executed 1445 of 1669 discovered tests and failed with
122 failures, 422 errors and 67 skips. The logs were re-read with BOM-aware decoding:
their exact 544 failure/error-name sets equal baseline, with none added or removed.
Logs: `mod-base-build-e2e-buildselection-py311.log` and
`mod-base-build-e2e-buildselection-py313.log` in the host temporary directory. Both suites
are terminal; the full gates remain failed. The auxiliary read-only GitHub query for legacy
run 37501309423 returned no data and was explicitly cancelled (session 4690, exit 1);
it contributes no evidence about API head semantics. All real Linux/hosted/native outcomes
and K/Q/B completion gates remain open. No release, consumer change or activation occurred.

Complete-Build-byte-assembly stage: assemble_build_export consumes the exact ordered retained
target partitions and fixed target-ordinal physical inputs, checking the protected assembler's
same run/attempt and complete plan. Enforce original complete logical file/byte/entry bounds,
existing physical-input cap and assembled canonical-envelope byte cap. Reject missing/extra/
reordered/mixed partitions, altered physical child layout and overlapping input/output roots.
Verify every actual source export against its retained canonical envelope, copy only declared
payload paths into one independent private atomic stage, check each copied inventory and the
whole sorted union, write one current-version complete envelope, independently verify the stage
and recheck every original input before publication. Returned envelope has independent data;
partition envelopes, target ordinal wrappers and future upload windows are not copied into it.

MB1 copy_selected_regular_files shares the original bounded stream engine, with sorted unique
bounded canonical selectors and exclusive no-follow destination creation. It allows appending
selected payloads while retaining whole-source inventory/entry checks before and after, including
unselected envelope bytes. Files are independent inodes; collisions/links never overwrite another
file. Existing copy_regular_files keeps its original signature and empty-stage behavior. Partial
failures remain unpublished under atomic_directory. Source quiescence/API admission and protected
input/output ancestors remain explicit caller obligations, not properties of constructed receipts.

This is inactive byte assembly, not native aggregate validity or successful gate authority.
Protected plan/policy/API provenance, native compiler/JDK/report witnesses, credentialless native
aggregate verification and validator receipt sealing remain mandatory before upload. ZIP encoding/
compressed-cap enforcement and real workflow/CLI integration are still missing. Immutable Block
Pops 47a890ae46a2878fb08d29a932803ab91bccdcd9 source inspection locates the 512 MiB native fan-in
budget in scripts/ci/e2e_fanin.py; it is not incorrectly substituted for Build's original 2 GiB
logical export cap. Native packaged fan-in remains separately unimplemented and retains that cap.

174 focused assembly/selected-copy/gate-transport/timeline/records/export/transport/input/protocol/
internal-API/predecessor/exact-builder tests pass on Python 3.11/3.12/3.13. Twelve new focused tests
cover canonical complete ordered union, payload-only copy/independent retained data, missing/
reordered/mixed/context substitution, physical child layout, source drift, copy loss/late rejection/
wrong stage bytes, envelope and whole-union caps, root overlap and bounded filesystem/loop errors;
selected-copy tests cover short writes, unselected source drift, missing paths/private stage/zero
write progress and rejected selectors before I/O. Windows filesystem seams are explicit.
Four new required Linux CI tests use real target-set download and actual assembly/inode checks,
early corrupt target rejection, late error/source mutation with no stage residue, and destination
collision/source symlink/unselected hard link/parent symlink refusal without outside writes. None
has executed here; no fake result claims real POSIX/UID/native validation.
Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:9d1533008d7511d912a3ab3efabba8ff54ae40a260a77a1ecfb497284467e2a3`.
Full Windows Python 3.11/3.13 each execute 1429 of 1653 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. External logs: `mod-base-build-e2e-buildassembly-py311.log` and
`mod-base-build-e2e-buildassembly-py313.log`. Both suite handles are terminal. Real Linux/hosted/
native outcomes and every remaining K/Q/B completion gate remain open; no activation or release.

Full-tested-record-transport stage: download_gate_receipt connects full-gate API chronology to
actual numeric-ID record transport. Caller supplies protected plan, expected gate, exact gate/
owning-Build workflow enrollment and a private temporary parent. Before ZIP fetch, authenticate
live source, latest completed successful exact producer attempt, protected API head/event/ref,
executing kit, full graph/digest and successful gate sealing/upload with actual selected window;
then bind immutable artifact metadata/owner/expiry and downloaded length/SHA-256. The common
hostile ZIP engine uses one-entry JSON-only limits at the existing 4 MiB record cap, accepts
only root ci-gate.json, and strict kind/schema/JSON/canonical bytes. No released schema changes.

Read every referenced source artifact's immutable metadata and current availability. Packaged's
owning Build additionally requires separate protected workflow enrollment, current exact successful
attempt, executing kit, full graph and aggregate upload proof. Full-gate actual execution timeline
and input provenance are rechecked alongside selected record admission before returning retained
data, with a final live source recheck. Temporary extraction is cleaned on success/rejection.
Private run/upload checks are factored without changing existing Build/target public signatures.

This remains inactive transport/chronology, not native/domain or status authority. Source payload
bytes/reports, protected plan/policy/pin and non-PR request/nonce authority, newest producer-run
selection, caller graph/writer integration, runtime input-before-execution admission, historical
original merged PR/reuse readers and reuse-record transport remain required separate work.
Two new explicit Linux CI tests exercise real Build/packaged extraction/API binding and real
wrong-filename/noncanonical rejection with no temporary residue; they have not executed here.

162 focused gate-transport/timeline/records/export/transport/input/protocol/internal-API/
predecessor/exact-builder tests pass on Python 3.11/3.12/3.13. Twelve new transport tests cover
both gates and exact numeric download/bounds, kind/unit/plan/workflow enrollment, latest attempts,
wrong head/failed producer/kit, immutable metadata/expiry/owner, ZIP size/hash, extra/nested filenames,
noncanonical/duplicate/unknown/wrong-kind JSON, all source availability/metadata, independent owning
Build enrollment/attempt, source/head/attempt changes after fetch, actual timeline rejection,
extraction failures and temporary cleanup. Windows filesystem seams are explicit; no simulated
filesystem result claims Linux extraction or account isolation success.
Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:6f5c6179e07029d6feb028d1b937ed674f380d30f0f1df1ace48df355e391531`.
Full Windows Python 3.11/3.13 each execute 1417 of 1641 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. External logs: `mod-base-build-e2e-gatetransport-py311.log` and
`mod-base-build-e2e-gatetransport-py313.log`. Both suite handles are terminal. Real Linux/hosted/
native outcomes and every remaining K/Q/B completion gate remain open; no activation or release.

Full-gate-API-timeline stage: authenticate_gate_timeline binds a full tested-record receipt and
selection to the exact attempt-scoped full producer graph/digest. Actual gate validation and
upload steps must be unique, successful, ordered and contained within their completed job;
the selected record window must equal the API upload. Every other full-graph job must finish
before protected gate validation starts. Exact source bundle/runtime/result jobs must have
actual successful upload windows equal to their selected descriptors. Packaged's owning Build
requires its own independent full graph/digest, completed jobs and successful bounded sealing.
Equal-time boundaries are supported; records are not mutated and API access is read-only.

This is an inactive chronology primitive, not complete admission: run/source/pin authority,
artifact API metadata/availability and byte authentication, native reports and caller graph are
independent requirements. Owning Build admission before runtime execution, in-progress writer
graphs, historical original PR/reuse admission and final record transport remain unimplemented.
Existing authenticate_graph, graph hashes, schemas, Pages and public status authority are unchanged.

150 focused timeline/records/export/transport/input/protocol/internal-API/predecessor/exact-builder
tests pass on Python 3.11/3.12/3.13. Ten new tests exercise full Build/packaged chronology,
source completion after verifier start despite later record upload, false source/record windows,
missing/duplicate/failed/overlapping steps, missing/reversed/outside job times, policy/target
prerequisites, independent owning Build graph/sealing/timing and full graph failures.
Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:a413318e1d2c7a7dc8c9827fb37b8a8771b50f925105f3a67695350d800fe0e2`.
Full Windows Python 3.11/3.13 each execute 1405 of 1629 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. External logs: `mod-base-build-e2e-gatetimeline-py311.log` and
`mod-base-build-e2e-gatetimeline-py313.log`. Both suite handles are terminal. Real Linux/hosted/
native outcomes and every remaining K/Q/B completion gate remain open; no activation or release.

Gate/reuse-record-timing stage: extend the explicit initial-format decision to the two new
inactive record kinds at schema 1 before first release. Their own producer contains only seven
pre-upload identity fields, rejecting the old local draft future-window shape; nested input
descriptors retain mandatory actual windows. bind_gate_receipt and bind_reuse_reference validate
selected writer identity, complete binding/kind/tested unit, source-ID separation and every
source upload completing before the selected record upload starts. The optional covered plan
path for reuse is exercised; pure structure/binding still cannot authorize actual reuse.
Real source completion before protected verifier start remains a separate mandatory K6 proof,
using independent API execution/closed graph evidence. Historical original PR producer admission,
whole original successful graphs/tree/policy/source availability and final record transport are
still incomplete. Current ready-PR authentication cannot substitute for a merged source reader.
Synthetic reuse now models a push with subject/controller equal, while retaining original PR
provenance. Fixtures use exact pretty_json byte output; no live merge or native execution is claimed.

140 focused records/export/transport/input/protocol/internal-API/predecessor/exact-builder tests
pass on Python 3.11/3.12/3.13. Ten new tests cover pre-upload serialization, rejected draft shape,
mandatory actual selected window, source chronology including packaged's owning Build and both
reuse seals, equal-time boundary, ID collisions, writer/kind/gate-unit/plan substitution and
independently supplied covered-plan binding. Existing direct-only/no-chain/source semantics remain.
Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:68ec877a685571d0390d7f7ff11c616d747f5739bd86ad3c503a0d0767d3e28d`.
Full Windows Python 3.11/3.13 each execute 1395 of 1619 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. External logs: `mod-base-build-e2e-recordtiming-py311.log` and
`mod-base-build-e2e-recordtiming-py313.log`. No consumer activation/settings/release occurred;
all remaining K/Q/B completion gates and real Linux/hosted/native outcomes stay open.

Pre-upload-envelope stage: explicit initial-format decision in ADR 0007 and BUILD-PROTOCOL
finalizes the new inactive envelope at schema 1 before first release. The committed predecessor
has no build_ci/records.py; released v1.0.3's digest-bound registry has no envelope kind. Keep
the exhaustive new-kind/current-1/previous-null ledger and reject the old local draft shape,
rather than invent predecessor support or change a released schema. Envelope producer now
contains the seven pre-upload identity fields only. Selected descriptors still require actual
API upload window and immutable ID/name/digest/size/creation/expiry. Strict bind_build_envelope
checks complete identity/plan/profile/producer/scope/target; partition and transport readers use it.
Canonical writer fixtures and selection's envelope hash were regenerated from their builders.

130 focused records/export/transport/input/protocol/internal-API/predecessor and exact fixture
builder tests pass on Python 3.11/3.12/3.13. Six new tests cover pre-upload serialization before
real window observation, unknown transport fields/old draft rejection, required descriptor
window, every producer field and artifact scope/target; a transport case shifts actual API
upload times after ZIP serialization while preserving its original digest/bytes. Native reports
and real GitHub workflow outcomes remain independently required. The first full Windows run
found two new fixture byte-equality failures: write_text introduced CRLF. Regeneration with
the exact pretty_json byte writer fixed both; no assertions or validators were weakened.

Final full Windows Python 3.11/3.13 each execute 1385 of 1609 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. Compileall with external bytecode storage, locks, digest and whitespace
checks pass. Digest: `sha256:af09a144544e58713292ae365e6c17f24f8c092c9b34b563edd2cf4d79a4f79b`.
Final external logs: `mod-base-build-e2e-preupload-final-py311.log` and
`mod-base-build-e2e-preupload-final-py313.log`. Earlier preupload logs retain the corrected
fixture failures for diagnosis. Gate/reuse receipt producer windows still need the corresponding
pre-upload identity/selected-descriptor audit; reuse source-before-writer chronology must remain
authenticated against actual API metadata, not disappear. Workflows, native aggregation, Linux
and real hosted canary evidence plus all remaining K/Q/B gates remain open and inactive.

Complete-target-input stage: 101 focused transport/protocol/export/internal-API/model-limit
and existing pure Pages ZIP-bound tests pass on Python 3.11/3.12/3.13. Eight additional tests
exercise complete ordered descriptor preflight, duplicate/missing/extra/reordered/mixed producers,
compressed-set budget before fetch, successful protected plan/policy, late second-target failure,
source drift after the last download and original whole-logical byte/entry bounds. Shared producer
authentication keeps the two-target synthetic transfer below 30 API reads. Each ZIP is released
before fetching the next; the original complete logical export retains 10,000 files/20,000 entries/
2 GiB payload, independently of per-target budgets. New 4 GiB total compressed-set and derived
physical wrapped-input entry bounds are explicit additional protections, not native fan-in limits.
Native/runtime 512 MiB limits remain unchanged and their domain integration is still required.
Windows positive publication uses explicit filesystem seams. Two required Linux tests now cover
real complete-set private publication and second-ZIP failure without partial output/stage residue;
neither was run locally.

Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:ee18547ea15d436d44fbeb5b6b8ecd045038c159de2923f7f668c8ad5ce3e4a6`.
Full Windows Python 3.11/3.13 each execute 1379 of 1603 discovered tests: 122 failures,
422 errors and 67 skips. Exact 544 fail/error-name sets match baseline, none added/removed.
Full gates remain failed. External logs: `mod-base-build-e2e-targetset-py311.log` and
`mod-base-build-e2e-targetset-py313.log` in the temporary directory.

Integration review found a protocol timing gap: the current envelope producer includes
`upload_window`, and transport compares it to the actual completed upload step, but that exact
future completion time cannot be authenticated when writing the pre-upload export. Preseeded
fake-API/ZIP fixtures do not prove a real workflow can produce that shape. Correct the initial
inactive contract with an explicit schema/compatibility decision before activating workflows;
retain exact actual API windows in immutable selected descriptors and independently bind the
pre-upload producer identity. Native aggregate receipt/bundle creation, workflows, hosted canary
and all remaining K/Q/B gates remain incomplete. No consumer activation/settings/release occurred.

Target-partition/controller stage: 93 focused transport/protocol/export/internal-API/model-limit
and existing pure Pages ZIP-bound tests pass on Python 3.11/3.12/3.13. Nine additional tests cover
same-assembler run/attempt/target binding, an admitted sealed target before aggregate/gate visibility,
duplicate/unenrolled jobs, failed/skipped/unsealed/overlapping-upload target jobs, queued/cancelled
or incoherent producer states, newer attempts after download, unchanged complete-Build requirements,
and latest versus exact-attempt workflow-ID agreement. A reproduced regression initially accepted
a historical push attributed to a different live controller; its new test failed before the fix
and passes afterwards. Producer API head now always equals the executing controller, and push
tested subject additionally equals that controller. Current push and historical protected dispatch
positive cases pass. Request/nonce/ref approval remains independently required and unimplemented.
One additional required Linux test exercises real same-run target extraction/private independent
copy before aggregate completion; it is unexecuted here. Windows positive-copy tests use explicit
filesystem seams; partial graph digest is the protected full contract, not full success evidence.

Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Final digest: `sha256:4fb370e3d84ed81b7668ce9bf23b930d4c25fde3575bd6c4027081a72cef1b25`.
Final Windows Python 3.11/3.13 full suites each execute 1371 of 1595 discovered tests: 122 failures,
422 errors, 67 skips; their exact 544 fail/error-name sets equal baseline, none added/removed.
The pre-controller-fix full suites executed 1369/1593 with the same 544 baseline names; they did
not contain the later reproducer and do not prove that identity constraint. Final logs are external
temporary `mod-base-build-e2e-targetcontroller-py311.log` and `mod-base-build-e2e-targetcontroller-py313.log`.
Full gates remain failed; real Linux/native/workflow/canary evidence, whole-target assembly and
all remaining K/Q/B completion gates stay open. No consumer activation/settings/release occurred.

Completed-Build-transport stage: 84 focused transport/protocol/export/internal-API/model-limit
and existing pure Pages ZIP-bound tests pass on Python 3.11/3.12/3.13. Nine new tests exercise
numeric-ID orchestration, producer attempt/event/head/path/kit/conclusion, artifact immutable
metadata/expiry/owner, exact full graph/upload window, length/digest, source drift, wrong envelope,
existing output and workflow enrollment. Real ZIP traversal/entry-count preflight rejects before
publication on Windows; successful extraction/copy uses explicit mocked filesystem seams here.
Fixed CI ZIP extraction retains the 20,000-entry export bound without widening Pages' 16,387-entry
ceiling. Two real Linux tests cover successful checked independent private copying and corrupt
inventory rejection with no output/temporary residue; they are required but unexecuted locally.
An attempted entire existing KindLimitsTests class hits its known Windows-only extraction error;
its two pure limit tests pass and the full-suite fail/error name set is unchanged.

Compileall with external bytecode storage, staged locks, digest and whitespace checks pass.
Digest: `sha256:9e19aad768ea9ae6c7e487bf3f8cfb3e08f0458391fbf4ac69f63843660d01d4`.
Full Windows Python 3.11/3.13 each execute 1362 of 1586 discovered tests, with 122 failures,
422 errors and 67 skips. Exact 544 fail/error name sets equal the baseline, none added/removed.
These are failed full gates, not Linux evidence. Logs are external temporary
`mod-base-build-e2e-transport-py311.log` and `mod-base-build-e2e-transport-py313.log`.
Newest-run selection, running target fan-in, packaged transport, native validators, protected
pin/request authority, workflows and all remaining K/Q/B gates remain incomplete and inactive.

Non-PR-source stage: 101 focused source/protocol/controller/input/export/internal-API tests pass
on Python 3.11/3.12/3.13. Nine new tests cover ready-PR routing, current and historical protected
subjects with zero/one/two parents, distinct controller sources, exact trees/parents/repository/ref,
unreachable or inconsistent ancestry, malformed/missing Git evidence and controller movement
during ancestry or inventory reads. These are fake-API identity tests, not live dispatch authority
or hosted execution evidence. Source and controller reads now bracket both supported routes.
Requesting workflow/run/attempt/nonce and protected dispatch authorization, full recovery policy,
staging and integration remain incomplete. No consumer activation or settings operation occurred.

Compileall with external bytecode storage, staged locks, digest check and whitespace checks pass.
Digest: `sha256:9f441856659c066d37ac1012ddd0921b1114f608a42442a7fccb287ba3d15906`.
Full Windows Python 3.11/3.13 suites each execute 1353 of 1577 discovered tests: 122 failures,
422 errors and 67 skips. Their exact 544 fail/error test-name sets match the recorded baseline,
with none added or removed. These are failed full gates; Linux/native/workflow/canary evidence
and all remaining K/Q/B completion gates stay open. Logs: external temporary
`mod-base-build-e2e-nonpr-py311.log` and `mod-base-build-e2e-nonpr-py313.log`.

Policy-runner-parity stage: 251 focused policy/input/validation/document/predecessor/controller/
handoff/export/source/protocol/worker/toolchain/host/internal-API/model-limit/grammar tests pass
independently on Python 3.11/3.12/3.13. Twelve new tests exercise count metadata, explicit profile
rules, expected failures/native method and teardown skips, bounded UTF-8 diagnostics, discovery
caps and real spawned benign suites. Subprocess suites preserve start-root sibling imports,
private worker/child temporary directories, class fixtures and reexport multiplicity; reject
empty/all-class-skipped suites, failed cleanup, unexpected successes, import failures and dead
workers. Serial comparison requires successful serial execution for comparable fixtures.

Rules were read as inert Git objects from QS c0cdc01ab20f1eac663c628011520c76fc7e3d7a and BP
47a890ae46a2878fb08d29a932803ab91bccdcd9, specifically scripts/ci/parallel_unittest.py and QS
scripts/ci/tests/test_parallel_unittest.py. No consumer source was imported or executed here.
The new explicit QS profile preserves whole-class setUpClass skip semantics and start-root
imports; BP/kit defaults retain strict per-unit counts. Both require complete discovery/worker
results and a nonzero global testsRun. Native cleanup/method skip and expected-failure success
semantics remain; candidate count/log claims never become status/upload authority.

A reviewed reexport fixture exposed stale TestResult previous-class state after tearDownClass
in the original repeat loop. The common runner now uses a fresh result per repeat, preserving
complete class/module lifecycle and actual serial counts. Common discovery/worker ceilings are
100000 tests and 256 processes; count fields enforce exact bounded types. Diagnostic retention
is capped at the existing 16 MiB per unit, with valid UTF-8 prefix/truncation indication. The
new MB11 helper has no environment reads; tool discovery/imports must run entirely inside the
credentialless account with verified kit imports. Complete source/installer/import provenance,
native policy suites and protected production integration remain open.

Two required hosted Linux cases now run the actual shared tool with reviewed minimal readonly
code under candidate/subworker UIDs: a passing UID/environment/temp check beside a class skip;
and refusal of an all-class-skipped suite. Both use bounded dispatch and require UID quiescence.
The generated root helpers, dispatchers and suite bodies compile. These are synthetic fixtures,
not production overlay provenance, and neither case has executed on this Windows host.

The changed tools lock was regenerated before the digest. Compileall with external bytecode,
both locks, digest and whitespace checks pass. Callee digest:
sha256:fd736ae0ae7417d4b198ef78b8087dca79d333473ff4427c8ad1c8bb20275f66.
Full Windows suites on Python 3.11/3.13 each execute 1344 tests (1568 discovered), with 122
failures, 422 errors and 67 skips. Both fail; exact 544 failing/error name sets equal the
untouched baseline, with no added or removed names. Terminal logs are
mod-base-build-e2e-policyparity-py311.log and mod-base-build-e2e-policyparity-py313.log in the host
temporary directory. No test process remains live. No schema, consumer activation or release
was added by this stage. Mandatory Linux/native/hosted evidence and all K/Q/B gates remain open.

Fixed-target-input stage: 239 focused input/validation/document/predecessor/controller/handoff/
export/source/protocol/worker/toolchain/host/internal-API/model-limit/grammar tests pass
independently on Python 3.11/3.12/3.13. Four new target tests exercise two independent enrolled
partitions, exact hook/unit/input-digest binding, complete/other-target/unknown/malformed-unit
refusal, mixed producer attempts, missing/extra outputs, pre/post byte/inode failures and native
execution rejection. A fifth new plan test proves target/lane unit admission agrees with
attempt-artifact transport, including 80-character IDs and forbidden double delimiters.
Portable composition tests mock lifecycle primitives; they are not Linux boundary evidence.

Inactive execute_frozen_target_validator shares the aggregate fixed read-only plan/Build
lifecycle without changing existing aggregate signatures or JSON schemas. It requires exactly
the enrolled target's partition, complete planned output coverage and producing run/attempt,
uses only protected verify_target and MB_TARGET_ID, and retains actual execution plus canonical
partition input digest for verifier-output freezing. Input metadata/bytes and directory identity
are rechecked before and after; admitted validator termination remains mandatory.

The unreleased Build CI_UNIT_ID grammar now rejects the artifact delimiter -- at plan/record/
environment admission, matching the formatter's existing rejection rather than admitting a
plan whose unit cannot be transported. Pages identifiers/formats and accepted artifact names
are unchanged. No consumer or runtime release version is activated by this correction.

Required hosted aggregate/target fixtures share reviewed setup while each test allocates fresh
accounts. Both inspect actual second-UID hook/unit arguments, hash fixed plan/Build files,
require nonwritable input paths and freeze existing closed outputs using retained execution/
input digest. Their synthetic protected sources compile; the new target case and modified
aggregate case have not executed on this Windows host. Native QS/BP compiler/JDK/task/packaging
witnesses, runtime/cross-run composition, full staging/import provenance and workflows remain open.

Compileall with external bytecode, staged locks, digest and whitespace checks pass. Callee digest:
sha256:9df90e3482883b282d1bd460fdf37a36bc3c73be04f24e76a9cc417bce2b6f55.
Full Windows suites on Python 3.11/3.13 each execute 1332 tests (1556 discovered), with 122
failures, 422 errors and 67 skips. Both fail; their exact 544 failing/error names equal the
untouched baseline, without added or removed names. Terminal logs are
mod-base-build-e2e-targetbind-py311.log and mod-base-build-e2e-targetbind-py313.log in the host
temporary directory. No suite process remains live. Mandatory green Linux/hosted/native
evidence and every original K/Q/B completion gate remain required.

Fixed-Build-input stage: 210 focused input/validation/document/predecessor/controller/handoff/
export/source/protocol/worker/toolchain/host/internal-API/model-limit tests pass independently
on Python 3.11/3.12/3.13. Eight new portable tests cover exact bounded plan inventory/bytes,
entry preflight, invalid-plan rejection before allocation, independent stage verification,
private-copy group read transfer, wrong producer/partial input and pre/post content/identity
failures, and unsafe/foreign input metadata. Mocked composition tests are not Linux evidence.

The new inactive MB11 inputs module uses the existing plan/envelope schema versions, with no
new JSON fields or kinds. A canonical protected plan is independently written as the sole
ci-plan.json in a private atomic stage. Fixed-root handoff authenticates host/accounts/layout,
terminates candidate, grants only validator reads and rechecks ownership/ACLs/inode/bytes.
Plan budgets are 4 MiB, one file and two entries including root, all defined in model.limits.
Same-producer complete aggregate verification checks fixed read-only plan/Build metadata,
full bytes and stable root identities before and after the existing closed protected hook.
Actual execution and canonical envelope input digest are retained together for output freezing.
The envelope's protected plan hash/file hashes bind those inputs transitively; matching objects
alone cannot supply provenance, native compiler semantics or API/status authority.

The required hosted protected-controller case now reads/hashes actual fixed plan/Build files
from the second UID, emits their digest and freezes its output with retained execution/digest.
Its protected synthetic verifier sources compile. Three additional Linux plan cases exercise
existing-output preservation, changed bytes, links/hardlinks/FIFOs/undeclared .pth files and
independent-stage failure cleanup. No new Linux case has been executed on this Windows host.
Target/runtime and cross-run selection composition, complete native validation/import closure,
overlay/Git/cache staging and workflow integration remain unfinished.

Compileall with external bytecode, staged locks, digest and both whitespace checks pass. Callee
digest: sha256:8be98d478ff3079045a88a7b835a072c91a2f3d706f5e3e32424d7057cebfbbf.
Full Windows suites on Python 3.11/3.13 each execute 1327 tests (1551 discovered), with 122
failures, 422 errors and 67 skips. Both fail; exact sets of 544 failing/error names equal the
untouched baseline, with no added/removed names. Terminal logs are
mod-base-build-e2e-inputbind-py311.log and mod-base-build-e2e-inputbind-py313.log in the host
temporary directory. No test process remains live. Mandatory green Linux and hosted canary
evidence are still missing. Every original K/Q/B completion gate remains required.

Candidate-Build-freeze stage: 186 focused validation/document/predecessor/controller/handoff/
export/source/protocol/worker/toolchain/host/internal-API tests pass independently on Python
3.11/3.12/3.13. Three added portable tests exercise actual composition order and rejection of
tracked-source drift before/after copying, surviving processes, unsafe original metadata,
foreign copy ownership, transfer failures, changed copy inode/envelope and malformed or
unsuccessful execution. These use mocked primitives and are not Linux boundary evidence.

The inactive fixed-root freezer terminates/locks the candidate, verifies protected tracked
inventory before and after independent export materialization, requires private original
metadata and transfers only a fresh root-owned copy into runner-private 0700/0600 ownership.
It rechecks copy identity, metadata, bytes and host fence. Original candidate files are never
chowned. Protected inventory/execution provenance and generated-root native policy remain
caller requirements; constructed receipts cannot authorize execution or uploads.

Two required hosted Linux cases now use actual candidate dispatch: an independent private
copy with unchanged original ownership/inodes and denial of both worker UIDs; and rejection
specifically by SourceError when a zero-exit dispatcher modifies its tracked source. Their
embedded candidate and root helper programs compile. They have not run on this Windows host.
Complete overlay/Git/cache staging, import/installer provenance, native compiler/report
validation and production verifier/workflow integration remain unfinished.

Compileall passes with external bytecode; staged locks, digest and both whitespace checks
pass. The callee digest is
sha256:57a6c20408be44a8e59404e5c8f80d8662260b43e9a586b9dfaae460f5c7d00d.
Full Windows suites on Python 3.11/3.13 each execute 1319 tests (1543 discovered), with
122 failures, 422 errors and 67 skips. Both fail. Their exact 544 distinct failure/error
names equal the untouched baseline, with none added or removed. Terminal logs are
mod-base-build-e2e-buildfreeze-py311.log and mod-base-build-e2e-buildfreeze-py313.log in the
host temporary directory. No suite process remains live. Mandatory Linux green gates and
the complete K/Q/B rollout remain unproven; every original completion gate stays required.

Fixed-verifier-freeze stage: 183 focused validation/document/predecessor/controller/handoff/
export/source/protocol/worker/toolchain/host/internal-API tests pass independently on Python
3.11/3.12/3.13. Five added portable tests cover private-copy 0700/0600 ACL/ownership transfer
and unchanged-content checks; exact private metadata admission; fixed output/account/layout/
quiescence/context transfer order; survivor/foreign-copy/transfer/inode/record failures; and
rejection of missing/nonzero/boolean execution results before copy or termination.

The protected-root freezer authenticates host, retained source/execution evidence, profile and
actual disjoint fixed accounts. It terminates/locks the validator, requires its original output
to remain private with exact validator ownership, verifies/copies context-bound canonical
reports and transfers only the new protected-root-created independent copy to runner UID/GID.
Root-last transfer preserves 0700 dirs/0600 files and removes ACLs; final private metadata,
inode, exact bytes/record and host are rechecked. Accepted-copy failure restores private
traversal; rejected foreign copies are not chmodded. The original verifier tree is never chowned.
Constructed source/execution objects do not establish provenance or native semantics.

The required Linux protected-dispatch fixture now emits a canonical private local receipt under
the actual verifier UID, derives its config hash from protected bytes and invokes actual root
freezing after quiescence. It checks runner-private ownership/modes, exact record/report bytes,
independent original/copy inodes and unchanged original validator ownership, plus read/write
denial to both expired workers with positively confirmed UIDs. This remains unexecuted on this
host and is synthetic, not native consumer compiler/runtime conformance. Complete import/input
provenance, native closed schemas/semantics, production root dispatch and final gate/API/upload
integration remain required. No profile/consumer/workflow activation or release occurred.

Compileall with external bytecode, locks and whitespace checks pass. Current digest:
sha256:5185c6263ffe3dc304cc4affaac74f4a3c593f287660636cde7c143216895c33.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1316 tests (1540 discovered),
with 122 failures, 422 errors and 67 skips. Their exact failure/error sets equal all 544 untouched
v1.0.3 baseline names, with none added or removed. These remain failed gates and do not establish
real Linux ownership/UID/freezing evidence. Logs in the host temporary directory:
mod-base-build-e2e-validationfreeze-py311.log and mod-base-build-e2e-validationfreeze-py313.log.
No suite remains running. All migration rows remain incomplete until their full design gates pass.

Verifier-output-record stage: 178 focused validation/document/predecessor/controller/handoff/
export/source/protocol/worker/toolchain/host/internal-API tests pass independently on Python
3.11/3.12/3.13. The new mod-base.ci.validation v1 kind is registered in the exhaustive fixture,
mutation and compatibility catalogs; the immutable v1.0.3 reader explicitly rejects it, while
all existing common-kind formats retain their unchanged compatibility decisions. Runtime
version stays v1.0.3; this is inactive planned-v1.1.0 work, not a released capability.

Nine added validation tests cover exact hook/plan/unit/native-contract coverage, closed fields
and versions/types, forbidden self-reported success, protected run/attempt/config/input context,
actual inventory/hash/record stability, hash-matching duplicate/nonfinite/noncanonical native
JSON, pre-read context/entry budgets, independent staged copy checks and no allocation after
rejected original admission. The reader requires canonical outer bytes and canonical strict
native JSON objects, exact file inventory, rehashed report bytes and a final record recheck.
The copy creates new independent regular files and independently verifies a private stage
before exclusive atomic publication. Existing output is never replaced.

Four required Linux verifier-output cases cover real independent inodes/private root/existing
output preservation, source mutation after copying, planted symlink/hardlink/FIFO refusal,
hash-matching duplicate native JSON rejection and changed-stage cleanup. They remain unexecuted
on this Windows host. Actual protected execution, UID termination and readable ownership
reclamation, excluded writers, native mod-owned closed report schemas/semantics and final
receipt/API/gate integration remain required. A matching packet/copy is not an upload or
successful-status authorization. All original migration rows remain incomplete.

Compileall with external bytecode, locks and whitespace checks pass. Current digest:
sha256:acc5cab99f0bdee5dacc97c259a4cccde2e03fd607c57cd811886de13a5b3ebb.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1311 tests (1535 discovered),
with 122 failures, 422 errors and 67 skips. Both exact failure/error sets equal all 544 untouched
v1.0.3 baseline names, with none added or removed. These remain failed gates, not required Linux
or native conformance evidence. Logs in the host temporary directory: mod-base-build-e2e-validation-py311.log
and mod-base-build-e2e-validation-py313.log. No suite remains running; no consumer/profile activation occurred.

Protected-controller-execution stage: 135 focused controller/handoff/export/source/protocol/
worker/toolchain/host/internal-API tests pass independently on Python 3.11/3.12/3.13. Six added
portable tests cover closed verifier hooks and exact plan-unit/dispatcher/timeout/environment
binding; planning/unknown/invalid-unit rejection; admission, native execution, postcheck and
final kill failures; exact read-access modes with empty sources; writable/foreign-group/linked/
inode-drift/ACL rejection; and bad platform/identity/cap rejection before metadata opening.

The inactive execution wrapper accepts only the actual fixed validator, validates protected
plan/profile/source evidence, admits target/lane IDs solely from that plan and derives fixed
Python -I -B dispatcher argv plus --hook. Native timeout is protected validator_seconds.
Before/after dispatch it rechecks host/traversal layout, full source byte/blob identity and
read-only metadata/ACL closure; existing tool admission binds the selected Python/JDK trees.
It always performs a final validator kill/lock/quiescence sweep, including pre-dispatch and
post-dispatch failure. No arbitrary command or environment is accepted. Zero exit/logs are
execution evidence only, not receipt validity, an upload authorization or a successful status.

A required Linux case stages benign protected synthetic source, performs the actual root read
handoff and invokes the new execution composition. It asserts real validator UID/cwd/isolated
Python/fixed hook/environment, no bytecode, both accounts' quiescence and validator lock/expiry.
It remains unexecuted on Windows and does not establish native consumer compiler/runtime
assertions, installer provenance or complete interpreter/system import enrollment. Frozen native
inputs, protected root-dispatch integration, native hook conformance, receipt validation/freezing
and real hosted evidence remain incomplete. No production workflow/consumer is activated.

Compileall with external bytecode, locks and whitespace checks pass. Current digest:
sha256:68d779684bc15f091aee5018c531a2636d5dfb8fef8228444680187c9b16fea7.
Full Windows Python 3.11/3.13 suites are terminal: each executes 1302 tests (1526 discovered),
with 122 failures, 422 errors and 67 skips. Both exact failure/error sets equal all 544 untouched
v1.0.3 baseline names, with none added or removed. These remain failed gates, not Linux or native
validator evidence. Logs in the host temporary directory: mod-base-build-e2e-controllerexec-py311.log
and mod-base-build-e2e-controllerexec-py313.log. No suite remains running; all migration rows stay
incomplete until their full design requirements are established.

Protected-controller-read-handoff stage: 129 focused controller/handoff/export/source/
protocol/worker/toolchain/host/internal-API tests pass independently on Python 3.11/3.12/3.13.
Five added portable tests cover fixed source/account/layout/order binding; rejected foreign
copy, changed final inode/config and failed permissions; forged validator rejection before
directory opening; empty source files with executable-bit removal and blob-byte rechecking;
and malformed/duplicate/Git-metadata source path rejection before content reads.

The protected root handoff checks runner host/passwd identity, actual fixed accounts, disjoint
UID/GID ownership and runner-owned traversal/copy layout. It terminates the candidate, verifies
original Git modes/bytes, grants only fixed-validator-group reads and independently rechecks
normalized regular modes and original blob/SHA-256 identities, copy inode and host fence.
Accepted-copy failures restore private root traversal; foreign copies are not chmodded.
The additive MB1 source permission helper supports empty regular files and repository paths;
both source and unchanged export permission helpers share ACL removal, exact content transfer,
metadata admission and root-last exposure. Existing artifact reads still reject empty files.

The required Linux fixture gains a real fixed-account/controller-copy access test, including
an empty Python module and executable tracked source. It checks validator reads, both workers'
write denial, candidate read denial under an independently confirmed expired UID and exact
0640/0750 metadata. This fixture remains unexecuted on Windows. Complete import enrollment,
installer provenance, protected root dispatch, native second-UID hook execution and receipt
sealing remain incomplete; no profile/workflow/consumer activation occurred.

Compileall with external bytecode, locks and whitespace checks pass. Current digest:
sha256:404acc08378f16b31f409139e9253cec4fdffc6a0a856682ee0f6924e0575567.
Full Windows Python 3.11/3.13 suites have completed: each executes 1296 tests (1520 discovered),
with 122 failures, 422 errors and 67 skips. Their exact failure/error sets match all 544
untouched v1.0.3 baseline names, with none added or removed. These are failed gates, not Linux
isolation or native verification evidence. Logs in the host temporary directory:
mod-base-build-e2e-controlleraccess-py311.log and mod-base-build-e2e-controlleraccess-py313.log.
Both handles are terminal; no suite remains running. Every migration row remains incomplete.

Protected-controller-materialization stage: 124 focused controller/handoff/export/source/
protocol/worker/toolchain/host/internal-API tests pass independently on Python 3.11/3.12/3.13.
Two new portable cases require malformed receipts to fail before output allocation and require
independent stage verification before publication. The materializer writes retained immutable
API byte evidence through exclusive descriptor-relative no-follow regular files, preserving
Git executable modes, in an empty caller-owned 0700 stage. All receipt hashes/config/closure
checks precede allocation; stage verification precedes atomic exclusive publication. It never
copies candidate inodes, imports code, runs Git or grants account access.

The required Linux fixture gains two physical materialization cases: exact bytes, modes and
single-link files with existing-output preservation, and injected undeclared-stage mutation
with no output or leaked stage. Its source fixture now also includes an executable Git file.
These physical cases remain unexecuted on this host. Protected destination ancestors, authentic
receipt retention, complete native import-root enrollment/provenance and fixed second-account
read-access/execution integration remain required. Every migration row remains incomplete.

Compileall with external bytecode, locks and whitespace checks pass. Current digest:
sha256:2b28ac077cf62ecff80a9f6f5f67a99cae88f63b3968ab6611e0551fae3c0f22.
Full Windows suites on Python 3.11/3.13 are terminal: each executes 1291 tests (1515 discovered),
with 122 failures, 422 errors and 67 skips. Both exact failure/error name sets equal all 544
untouched v1.0.3 baseline names; none added or removed. These remain failed gates and do not
establish real Linux materialization or native-validator isolation. Logs in the host temporary
directory: mod-base-build-e2e-controllercopy-py311.log and
mod-base-build-e2e-controllercopy-py313.log. No suite remains running.

Protected-controller-source stage: 122 focused controller/handoff/export/source/protocol/
worker/toolchain/host/internal-API tests pass independently on Python 3.11, 3.12 and 3.13.
Nine controller tests cover approved-path closure, protected immutable Git blobs and configured
hashes, regular modes and directory ancestors, missing sizes, file/whole-code byte budgets,
foreign config, head/default movement during reads, minimal copy binding, Git metadata exclusion
and malformed receipt rejection. Source code is read as inert bytes, never imported during
admission. Evidence receipts must remain in protected memory; constructing a matching dataclass
does not establish API or native-policy authority. New import-source limits add 4 MiB per file
and 64 MiB total without expanding existing source/config/transport caps.

Two physical Linux controller-copy cases are included in the existing required CI fixture:
exact bytes/modes followed by mutation, and undeclared Python/Git metadata/symlink/hardlink
rejection. They remain unexecuted on this Windows host. Complete native import enrollment,
installer provenance, protected source materialization and second-UID execution/receipt sealing
remain incomplete; no production workflow or consumer is activated.

Compileall with external bytecode, locks, digest and both whitespace checks pass. Current digest:
sha256:9b70928c9ff36bf83806c06ea0d9269a5fda2a903b4b2543176d491c631436ce.
Full Windows suites on Python 3.11/3.13 both complete: 1289 executed tests, 1513 discovered,
122 failures, 422 errors and 67 skips. Exact failure/error name sets equal the untouched v1.0.3
baseline's 544 names, with none added or removed. These are failed full-suite gates, not Linux
or native execution evidence. Logs: mod-base-build-e2e-controller-py311.log and
mod-base-build-e2e-controller-py313.log in the host temporary directory. Both handles are terminal.

Bound-Build-handoff stage: 113 focused handoff/export/source/protocol/worker/toolchain/host/
internal-API tests pass independently on Python 3.11, 3.12 and 3.13. Four new cases cover
privileged runner passwd/home rechecking, fixed account/path/order binding, rejected runner/
peer/forged-validator identities, foreign layouts/copies, failed termination/permissions and
changed final inode/mode/envelope. Only the admitted copy is restored to private traversal
on failure; rejected foreign ancestors/copies are never chmodded. The operation terminates
the authenticated candidate before verifying/granting the independent copy, then rechecks its
identity, envelope and the private runner host fence. Reader group and owner are derived from
the actual fixed validator and authenticated runner receipt, never plan-selected fields.

The required Linux ACL/access fixture now calls this composition over a complete inert
plan-bound envelope, checks candidate quiescence and uses protected root setpriv for probes
under the expired candidate UID. A positive id probe ensures the negative access checks really
execute rather than failing in PAM. The fixture remains unexecuted on this host. No production
root dispatcher/workflow is activated; original source reclamation/immutability and native
second-account execution/receipt sealing remain incomplete. Compileall, locks, digest and
whitespace checks pass. Current digest:
sha256:00adb063fafdec8f3d2c21bb058f654f6db1ef886a1add9ca891408a3cecb9a0.
Full Windows bound-handoff suites have completed on Python 3.11 and 3.13: each executes
1280 tests (1504 discovered), with 122 failures, 422 errors and 67 skips. Both have exactly
the untouched v1.0.3 baseline's 544 failure/error names, with none added or removed. The previous
stage's intermittent bash-launch errors do not recur in these runs. These remain failed gates
and do not establish Linux account/ACL/native-validator isolation. Logs in the host temporary
directory: mod-base-build-e2e-boundhandoff-py311.log and mod-base-build-e2e-boundhandoff-py313.log.
Both process handles are terminal; no suite remains running.

Read-group-handoff stage: 109 focused handoff/export/source/protocol/worker/toolchain/host/
internal-API tests pass independently on Python 3.11, 3.12 and 3.13. Five new handoff tests cover
root-last exposure, access/default ACL removal, absent ACLs, foreign owners, byte drift,
ACL/root-permission failures, Linux/root prerequisites and exact bounded UID/GID arguments.
The privileged helper accepts only a fresh private independent copy with one protected original
owner; it transfers files to 0640 and directories to 0750 with the protected owner/read group.
The exact byte inventory must remain unchanged. Failure restores 0700 root traversal.
UID/GID bounds reject Linux's all-ones no-change sentinel and oversized integers before I/O.
Caller authentication of runner/validator identities, ancestor protection, exclusion of writers
and separation of candidate/read groups are mandatory, not established by this generic MB1 helper.

The required hosted Linux fixture now invokes the protected helper over an inert runner-owned
copy and checks actual validator read access, candidate read denial and write denial for both
workers. It also plants named-candidate ACL grants masked off by initial private modes, plus a
default directory ACL, and requires their removal before actual post-handoff access checks.
The fixture's ACL layout/tags were checked against Linux v6.8 UAPI headers, linked in
BUILD-PROTOCOL.md. It remains unexecuted. No production privileged dispatcher or lifecycle wiring exists;
candidate-original ownership reclamation and native second-UID receipt sealing remain incomplete.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:0be4d510088d00c277e55b22adede0f9324f4fdc4b2485c48ad3dd8b259d27cf.
Full Windows read-handoff suites have completed on Python 3.11 and 3.13: each executes
1276 tests (1500 discovered). Python 3.11 reports 122 failures, 428 errors and 61 skips;
Python 3.13 reports 122 failures, 427 errors and 62 skips. Both retain every one of the untouched
v1.0.3 baseline's 544 failure/error names, with no removed name. Python 3.11 adds six errors
in the unchanged BindStepExecutionTests/CompositeShellTests; Python 3.13 adds five in the
unchanged DigestParityTests. Every added error is Windows WinError 26 from starting bash
inside the existing platform-tool prerequisite check, before the relevant test body executes.
No read-handoff test fails. Isolated reruns of the complete affected classes finish with the
existing unavailable-tool skips: 11 tests/11 skips on 3.11, five tests/five skips on 3.13.
Those reruns are not passing execution evidence and do not turn either failed full suite green.
Logs in the host temporary directory: mod-base-build-e2e-readhandoff-py311.log and
mod-base-build-e2e-readhandoff-py313.log. Both process handles are terminal; no suite remains running.
The subsequently strengthened, separately invoked Linux ACL fixture compiles, but has not run.

Independent-export-copy stage: 104 focused export/source/protocol/worker/toolchain/host/
internal-API tests pass independently on Python 3.11, 3.12 and 3.13. Three new export cases
cover admission-before-allocation, exact copied/staged inventories and entry-budget rejection
before envelope reads. MB1 counts the full no-follow entry closure before content, streams
hash-checked bytes into new exclusive files and rechecks source entries/inventory afterwards.
The MB11 orchestrator verifies original and independent staged envelopes before exclusive
atomic publication. The output root is private and caller-owned; files never alias source inodes.
The native 20,000-entry whole-tree cap includes the root and is not multiplied by partitions.
Empty directories are omitted; all admitted files, including the envelope, remain hash-covered.

Four required real Linux export-copy cases now cover independent inodes, post-copy source
mutation isolation, existing-output refusal, links/hard links/FIFO rejection, failed-stage
nonpublication and pre-content entry caps. They remain unexecuted. This is export
materialization only; source UID termination/ownership reclamation, protected output-parent
layout, native second-account verification/receipt sealing and uploads remain incomplete.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:9731cf7587e122dd8de094170a97eec9c7b5bf7c82a9294bdac69741c926e02e.
Full Windows export-copy suites have completed on Python 3.11 and 3.13: each executes
1271 tests (1495 discovered), with 122 failures, 422 errors and 67 skips. Both have exactly
the untouched v1.0.3 baseline's 544 failure/error names, with none added or removed. These
remain failed full-suite gates and do not prove Linux copy or account isolation. Logs in the
host temporary directory: mod-base-build-e2e-exportcopy-py311.log and
mod-base-build-e2e-exportcopy-py313.log. Both process handles are terminal; no suite remains running.

Authenticated-inventory stage: 94 focused source/protocol/worker/toolchain/host/internal-API
tests pass independently on Python 3.11, 3.12 and 3.13. Five new cases cover complete immutable
tree admission, truncation/wrong/missing evidence, unsupported entries/sizes/directory closure,
source budgets and live head movement during listing. Live PR authentication brackets the tree
read; returned blob identities still require independent byte/mode verification during copying.
The existing GitHub transport's 100,000-entry cap remains an additional restriction, unchanged.
No candidate code or Git command runs in this source reader. Non-PR admission remains pending.

Read-only immutable Git-object inspection converted the complete recursive directory/blob
listings into fake API tree responses and passed the new admission reader. Quick Skin's design
snapshot has tree 0dcc51f8f3648364eae58d95a947058dcdf41702: 1702 API entries, 977 files,
200006 listing bytes and 15350612 source bytes. Block Pops' design snapshot has tree
1e959a94f46ef8f2d660cdcb7bc299e8a139baf9: 1004 API entries, 806 files, 119326 listing
bytes and 11797522 source bytes. Neither consumer checkout was changed. The PR/controller
responses in this check are synthetic, so this proves inventory-shape compatibility only,
not live consumer authentication, native compiler/runtime parity or protected materialization.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:e6b5d6497bc570bdbe803510a0c6be76523d6debeab9f5e2e2ecdf9c517d03b2.
Full Windows inventory-stage suites have completed on Python 3.11 and 3.13: each executes
1268 tests (1492 discovered), with 122 failures, 422 errors and 67 skips. Both have exactly
the untouched v1.0.3 baseline's 544 failure/error names, with none added or removed. These
remain failed full-suite gates and do not prove Linux isolation. Logs in the host temporary
directory: mod-base-build-e2e-inventory-py311.log and mod-base-build-e2e-inventory-py313.log.
Both process handles are terminal; no suite remains running.

Post-lock termination stage: 77 focused worker/toolchain/host/source/internal-API tests pass
independently on Python 3.11, 3.12 and 3.13. Termination now repeats real/effective UID kill
sweeps and observations after mandatory lock/expiry, retaining the original sweep deadline.
Two regressions cover a process appearing during locking and a failed post-lock observation;
neither permits successful return. The real Linux child-process fixture records actual control
calls and requires final sweeps/observations after locking; it remains unexecuted on this host.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:f9115ba3b9fd8fd2b75a51d41e78385479e10af820c88b73def96f58dc45640d.
Full Windows post-lock suites have completed on Python 3.11 and 3.13: each executes
1263 tests (1487 discovered), with 122 failures, 422 errors and 67 skips. Both retain exactly
the untouched v1.0.3 baseline's 544 failure/error names, with none added or removed. These
remain failed gates, not Linux isolation evidence. Logs in the host temporary directory:
mod-base-build-e2e-postlock-py311.log and mod-base-build-e2e-postlock-py313.log. Both process
handles are terminal. The separately invoked Linux fixture accepts repeated post-lock sweeps
for asynchronous teardown and still requires complete kill/query ordering; it remains unexecuted.
This is not complete source freezing, export sealing or second-account native validation.

Tool-closure stage: 75 focused toolchain/host/worker/source/internal-API tests pass independently
on Python 3.11, 3.12 and 3.13. Eight toolchain tests cover stable complete metadata closure,
foreign owners, writable entries/ancestors, special entries, escaping/invalid/looping links,
global budgets, hostile root inventories, receipt drift, directory alias cycles and failed
pre-dispatch admission. These syscall fakes do not prove actual host permissions or provenance.
The required Linux fixture now inspects the complete real Python prefix and reinspects it before
a fenced synthetic dispatch; it remains unexecuted. Mutable tool trees reject without repair.
Installer provenance, all actual import roots, native observations and protected installation
preparation remain incomplete. The receipt hashes metadata rather than file contents.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:3f4965e0c7d435fe2c816fe7597b6441117862d8c494f83cb127449dd92a52a7.
Full Windows tool-closure suites have completed on Python 3.11 and 3.13: each executes
1261 tests (1485 discovered), with 122 failures, 422 errors and 67 skips. Both have exactly
the untouched v1.0.3 baseline's 544 failure/error names, with none added or removed.
These remain failed full-suite gates and do not establish hosted Linux isolation. Logs in
the host temporary directory: mod-base-build-e2e-toolchain-py311.log and
mod-base-build-e2e-toolchain-py313.log. Both process handles are terminal; no suite remains running.

Host-fence stage: 67 focused host/worker/source/internal-API tests pass independently on
Python 3.11, 3.12 and 3.13. Seven new host protocol tests reject foreign/noncanonical layouts,
unsupported hosts, links, swapped inodes, foreign owners, changed private modes and malformed
receipts; failed pre-dispatch fencing never launches and locks/terminates the identity.
The required Linux fixture now closes /home/runner, rechecks before actual dispatcher execution,
and probes inert host files, a benign runner-UID process's environment/memory/ptrace access and
an inheritable host descriptor. It also checks Python/import-root ancestor writability under
both disposable UIDs; those checks remain unexecuted. No existing credential file or GitHub
service process is probed. Fixture home-mode restoration waits for deleted accounts and reaped
probes; any failure leaves it private. The launcher starts at the runner-owned traversal root
and changes to a genuinely candidate-owned 0700 cwd via fixed GNU env --chdir after UID drop,
with explicit inherited-FD closure. Validator controller roots are runner-owned and group-readable
only to that validator. Full authenticated copies, tool/cache/import closure, source freezing,
second-account domain validation and actual Linux/hosted evidence remain incomplete.
Compileall, locks, digest and whitespace checks pass. Current digest:
sha256:e9a4e83c50add6ad7bcf88681305642a8a6cd87d12be8f602039b2fefedd21c2.
Full host-fence Windows suites have completed on Python 3.11 and 3.13: each executes
1253 tests (1477 discovered), with 122 failures, 422 errors and 67 skips. Both have exactly
the untouched v1.0.3 baseline's 544 failure/error names, with no added or removed name.
These remain failed full-suite gates and do not prove hosted Linux isolation. Logs in the
host temporary directory: mod-base-build-e2e-host-py311.log and
mod-base-build-e2e-host-py313.log. Both process handles are terminal; no suite remains running.

Earlier fresh-allocation stage: 60 focused worker/source/internal-API tests pass separately on Python
3.11, 3.12 and 3.13. Ten new boundary/account tests cover hosted/Linux prerequisites,
exclusive fresh paths, preexisting identity rejection, descriptor cleanup, sudo-policy failure,
partial useradd failure without an unverified UID kill, and changed ownership/inode rejection.
The runner must have a non-root UID/GID; only disposable workers use the minimum-1000 policy.
A test preserves this distinction with a non-root runner primary GID below that minimum.
The required Linux fixture now calls production boundary/account allocation rather than
constructing identities itself, and checks that existing accounts/boundaries cannot be reused.
It remains unexecuted here. Creating a dedicated root is not evidence that host credentials,
runner/action directories or process memory are inaccessible; those boundary checks and the
complete lifecycle remain mandatory and incomplete. Compileall, locks, digest and whitespace
checks pass. Current digest:
sha256:5e804f00749d8512c8fa70eec15fd30ce6bb2a3112e6dd3762c5c92270d9e229.
Initial allocation Windows suites completed on Python 3.11/3.13: each executed 1245 tests
(1469 discovered), with 122 failures, 422 errors and 67 skips, and exactly the 544 failure/error
names of the untouched v1.0.3 baseline. These runs preceded the runner-GID correction and
do not establish the final tree's full-suite result. Initial logs: mod-base-build-e2e-allocate-py311.log
and mod-base-build-e2e-allocate-py313.log in the host temporary directory.
Final allocation suites after the runner-GID correction have also completed on Python
3.11/3.13: each executes 1246 tests (1470 discovered), with 122 failures, 422 errors and
67 skips. Both retain exactly the baseline's 544 failure/error names, with none added or
removed. These remain failed Windows gates and do not establish Linux or release readiness.
Final logs: mod-base-build-e2e-allocate-final-py311.log and
mod-base-build-e2e-allocate-final-py313.log in the host temporary directory. Both handles
are terminal; no test suite remains running.

Earlier tracked-copy stage: 50 focused source/worker/internal-API tests pass separately on Python
3.11, 3.12 and 3.13. The new copy orchestration case rejects differences between original,
copied and independently inspected staged records. Two additional required Linux tests
exercise real atomic copying (empty/executable/literal-link preservation, 0700 root,
Git metadata omission and no replacement) and source mutation during staging with no output.
These Linux tests remain unexecuted. This implements tracked-source materialization only;
fresh account allocation, protected Git/overlay/cache staging, ownership transitions, source
freeze and second-account validation remain unfinished. Compileall, staged locks and both
whitespace checks pass. Current callee digest:
sha256:e8b9cd05161b4f66e1e8d294ecb84fb52eab8b4fcb89044980efe67d9f4d8c9a.
Full tracked-copy Windows suites have completed on Python 3.11 and 3.13: each executes
1236 tests (1460 discovered), with 122 failures, 422 errors and 67 skips. Both have the exact
544 failure/error names of the untouched v1.0.3 Windows baseline, with no added or removed
failure name. These are failed full-suite gates, not Linux isolation or release evidence.
Logs in the host temporary directory: mod-base-build-e2e-copy-py311.log and
mod-base-build-e2e-copy-py313.log. Both process handles are terminal; no suite remains running.

Earlier source-inspection stage: 49 focused source/worker/internal-API tests pass independently on
Python 3.11, 3.12 and 3.13. Six new source tests cover hostile listings, count/size caps,
case/path conflicts, direct-inventory bypass and exact mode/blob matching. Four required Linux
source tests were added for literal links, empty/executable files, generated-root parents,
tracked leaves within generated roots, source changes, hard links and byte budgets; they have
not run on this Windows host. These pure/mock checks do not prove the physical boundary.
Dispatchers now explicitly use -I -B because isolated Python ignores bytecode environment
settings. Compileall, staged locks, digest check and both whitespace checks pass. Current callee
digest is sha256:7059d76f926884bd7a050d064a13e778aec84e60d60491beae294fcfc34f93cd.
The source inspector is read-only; protected Git listing transport, private copies, source
freezing and second-account native validation remain incomplete.
Read-only Git-object inspection at the design's immutable consumer snapshots also confirms
the new parser accepts Quick Skin's complete listing (977 files; 123,520 listing bytes;
15,350,612 source bytes) and Block Pops' listing (806 files; 99,820 listing bytes;
11,797,522 source bytes). Both include modes 100644 and 100755. Existing consumer checkouts
were not switched or edited. This is source-inventory compatibility evidence only, not native
compiler/report/runtime conformance or authenticated candidate-copy evidence.
Full source-stage suites have completed: Python 3.11/3.13 each execute 1235 tests (1459
discovered). Python 3.13 reports 122 failures, 422 errors and 67 skips, with the exact 544
failure/error names of the untouched v1.0.3 Windows baseline. Python 3.11 reports 123 failures,
422 errors and 67 skips: the same baseline plus the unchanged imaging stable-read test
test_a_file_swapped_before_opening_is_refused. Its os.replace failed with Windows access denied
instead of reaching the expected inode-change check. That exact test passes when rerun alone;
this observation does not turn the failed full suite into a pass. Logs in the host temporary
directory: mod-base-build-e2e-source-py311.log and mod-base-build-e2e-source-py313.log.

Earlier bounded-execution stage: 137 focused protocol/model/reader/internal-API tests pass separately on
Python 3.11, 3.12 and 3.13, including 21 worker tests. New cases cover nonzero/timeout/read/launch/
cleanup failure, orphan-pipe handling, log truncation/draining, fixed isolated dispatcher roots,
signal cleanup/restoration and modern/legacy log-marker escaping. These mock account/syscall
tests do not establish actual isolation. The explicitly required Linux fixture now has eight
real account/dispatcher tests; no hosted execution has been submitted. Compileall and staged
locks pass; current callee digest is
sha256:e8bfdfb158fe1ddb90fcbf9b7a0c3d6a1f2b2cee7f5d1642fe52c38b3647559e.
Workflow policy/name-constant checks pass (35 tests; 3 unavailable-platform-tool skips).
Full bounded-execution Windows suites on Python 3.11/3.13 each execute 1229 tests (1453
discovered), with 122 failures, 422 errors and 67 skips. The exact 544 failing/error names
match the untouched v1.0.3 Windows baseline; no new or removed failing name appears. Both
processes have completed. Logs: mod-base-build-e2e-execution-py311.log and
mod-base-build-e2e-execution-py313.log in the host temporary directory. These are failed gates,
not green Linux results or native sandbox/canary evidence.

Earlier initial-worker evidence:

- Worker-stage focused suites now pass 129 tests separately on Python 3.11, 3.12 and 3.13.
  Thirteen new tests cover explicit identity/environment restrictions, separate homes/caches,
  account/group binding, double real/effective UID sweeps, surviving-process/control failures,
  mandatory lock/expiry and bounded/reaped control output. Those syscall/account tests use
  mocks and do not substitute for Linux results. The Python 3.11 Windows missing-set_blocking
  mock was corrected without changing the Linux implementation.
- Required real account tests are in tests.ci_linux_worker and explicitly invoked by each
  required Ubuntu Python CI leg. They intentionally are not ordinary test_* discovery and
  refuse arbitrary/non-hosted hosts rather than allocating system accounts there. They remain
  unexecuted locally and unsubmitted to hosted CI. Full copy/execute/freeze/second-validator
  and policy-suite parity requirements are still incomplete.
- Worker-stage compileall and staged locks pass. Current callee digest is
  sha256:802c6a3c907547e0561141a4924f1bce1154350665eeee31f208e0483d92e2c7.
  Workflow policy/name-constant focused checks pass on Python 3.12 (70 tests including the worker
  and internal-API checks; 3 platform-tool skips). Pinned hosted actionlint/shellcheck remain open.
- Worker-stage full Windows suites on Python 3.11/3.13 each execute 1221 tests (1445 discovered),
  failing with 122 failures, 422 errors and 67 skips. All 544 failing/error names exactly match
  the untouched v1.0.3 Windows baseline; neither suite has an added/removed failing name. The
  corrected worker tests pass within both runs. Logs are mod-base-build-e2e-worker-py311.log
  and mod-base-build-e2e-worker-py313.log in the host temporary directory. Both processes are
  terminal. These are failed full-suite gates; mandatory green Linux CI/UID results remain open.

Earlier export-stage evidence:

- Current focused config/record/export/protocol/model/predecessor/internal-API suites: 116 tests
  pass separately on Python 3.11.15, 3.12.10 and 3.13.14. The seven new export tests prove exact
  complete target receipt coverage, attempt/producer coherence, whole-tree budget preservation,
  and rejection of size/hash/inventory/envelope mismatches. Byte admission tests supply MB1
  inventory results; they do not establish Linux traversal or worker lifecycle safety.
- Added canonical frozen-export reader uses existing MB1 descriptor-relative single-link
  regular-file inventory and rechecks envelope bytes after hashing. Production integration,
  immutable-ID/API/domain validation and actual Linux frozen-tree adversarial tests remain open.
- Current compileall passes with bytecode outside the checkout. Staged locks pass; refreshed
  callee digest is sha256:c25b1da12b95d5ee7776ad931c26e898bd640807c78972fc8e2865a9fdad7ef7.
- Current full Windows suites on Python 3.11 and 3.13 each execute 1208 tests (1432 discovered).
  Both fail. Their 544 distinct failing/error test names exactly equal the untouched v1.0.3
  Python 3.12 Windows baseline: no new or removed failing names. Focused tests and this host
  comparison do not satisfy the mandatory green Linux suites. Current logs are
  mod-base-build-e2e-current-py311.log and mod-base-build-e2e-current-py313.log in the host
  temporary directory; both test processes have completed.

Earlier full-suite comparison, before the additional config/records/export implementation:

- The 68 focused protocol, model-document, predecessor-reader and internal-API tests pass on
  Python 3.13. The same modules pass separately on Python 3.12.
- Compileall over src/tests/tools passes with bytecode directed outside the checkout.
- The staged-file lock and all three generated callee digest literals pass their check commands.
  Required input staging is limited to modified src/ and the managed public-evidence document;
  no commit was made. git diff --check and git diff --cached --check pass.
- Full modified-tree suites on Python 3.12 and 3.13 each execute 1184 tests (1408 discovered),
  with 122 failures, 422 errors and 67 skips. Both are failed gates, not qualified success.
- An independent untouched v1.0.3 detached worktree, after rereading its instructions, executes
  1170 tests (1394 discovered) on Python 3.12 with the same 122 failures, 422 errors and 67 skips.
  Exact failing test-name sets are identical across all three runs; no changed-only failing test
  was found. The extra 14 tests are the new protocol tests and expanded compatibility coverage.
- Representative host failures require descriptor-relative POSIX filesystem primitives, Git in
  the fixed /usr/bin:/bin:/usr/local/bin search path, normalized absolute POSIX paths, and symlink
  creation privilege. Preserve those checks; this comparison does not replace Linux evidence.

Full local logs are in the host temporary directory: mod-base-build-e2e-tests.log,
mod-base-build-e2e-tests-py313.log and mod-base-build-e2e-baseline-py312.log. These local observations
do not establish required hosted CI, actionlint/shellcheck, worker boundary or canary completion.

Native fixtures and hosted API semantics remain unverified. Synthetic graph/plan tests cannot
establish sandbox isolation, actual compiler/runtime behavior, App authority, release completion
or governance provisioning. Every row remains incomplete until its design-required evidence is
recorded here from authoritative files/results/runs.
