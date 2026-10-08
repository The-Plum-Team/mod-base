# Protected Python installer candidates

This inactive profile addresses the installer provenance prerequisite in
[BUILD-PROTOCOL.md](BUILD-PROTOCOL.md). It does not activate a worker, install Python or approve
an installed interpreter/import/system closure. The committed archive lock is
[python-ubuntu24-x64.sha256](../requirements/python-ubuntu24-x64.sha256). Updating these bytes
requires protected review and a kit digest refresh; setup must never select a new minor/patch
version from a mutable manifest or accept a locally observed hash as approval.

## Read-only installed SDK authentication and fixed launch binding

`python_setup.authenticate_privileged_python_installation` reopens the fixed private archive
cache with Root host/kit-lock admission, verifies exact size/hash and stable metadata, then
reconstructs the complete selected source inventory, normalized install manifest and counts.
The retained installation receipt must match that independently derived expectation and the
original SDK directory inode. The actual installed tree must match every expected directory,
file byte/mode and internal file-resolving link. The cache, archive descriptor/named identity,
prefix and SDK bindings are checked around inspection. No files or permissions are changed.
The installer composition now performs this independent revalidation before returning its proof.

The additive `privileged_launch.execute_installed_python_freeze_request` chooses only the fixed
versioned `bin/python3.<minor>` path. SDK authentication is repeated at the three existing tool
admission points around the sealing subprocess, in addition to the original independently
approved complete tool-byte closure, bootstrap program/context/receipt validation and validator
cleanup. SDK proof cannot replace system-library/import closure approval. The existing string-path
launcher signature and behavior remain available unchanged; neither route enrolls its own caller.
No workflow calls the new route and no copied Python/archive program has run in this session.

A new required Linux fixture assembles known synthetic source bytes into a fresh temporary cache
and SDK, exercises actual read-only authentication, and rejects a changed installed payload.
Its outer host/kit/profile/location seams are explicit, so it establishes neither the fixed
production layout nor original caller/runtime enrollment. Its body was captured and compiled
locally without invoking sudo or executing it. Actual hosted execution remains pending.

## Inactive root-private archive handoff

`build_ci.python_setup.cache_privileged_python_installer` requires the actual protected Root
role, the retained private runner-home fence and the genuine installed kit archive lock before
network access or file creation. It downloads through the pinned publisher transport and
publishes exclusively at `/home/runner/.mod-base-python/<version>/installer.tar.gz`. Existing
version directories are refused; this is a private handoff, with no shared cache reuse. The
parent and version directory must be Root-owned mode 0700 and the sole file Root-owned mode
0600 with one link, exact size/hash and stable opened/named metadata. Source bytes are written
in bounded chunks, handling short writes and rejecting zero progress. Inventory scanning stops
at a second entry. Host/kit/parent, original published root inode, archive bytes and publisher
metadata are rechecked around publication and before returning the path.

`install_privileged_python_from_publisher` connects that admitted path directly to the existing
exclusive source-only installation at the archive's compiled prefix. Installation repeats the
private kit lock and stable local-source admission; publisher/kit/host checks bracket return of
the installation proof. No copied interpreter or installer program runs. A failure after
publication or installation can leave a private output but returns no execution authority;
new empty parent directories can also remain after an unsuccessful operation. Nothing adopts,
overwrites or executes an earlier cached/global installation.

Original protected caller/program/interpreter/import/system provenance remains an independent
prerequisite. Unit OS seams do not establish physical Root or runner admission. Two mandatory
hosted Linux component fixtures now exercise actual Root-private temporary archive writes,
exclusive publication, permissions, hard-link/symlink/FIFO rejection and failure cleanup. Their
embedded bodies compile locally; they have not run on this Windows host. Those fixtures touch
only fresh temporary directories, not the fixed production cache or global SDK. The full
fixed-path download/cache/install workflow and complete runtime closure still need Linux and
hosted evidence before production startup wiring or execution.

## Inactive publisher transport

`build_ci.python_transport.download_python_installer` accepts only the three reviewed profiles
below. Before and after downloading it requires the fixed numeric release/asset, unique exact
asset membership, published non-prerelease state, direct tag commit, existing Git commit and
successful `workflow_dispatch` producer on `main`, including both latest and exact attempt 1.
Every relied field has an exact type; API failures, missing hashes, duplicate assets and a newer
attempt reject. Unrelated upstream response fields are permitted within the REST body bound;
release membership has a separate 128-entry cap.

The MB1 client uses the numeric release-asset endpoint with `application/octet-stream`: direct
HTTP 200 or one HTTP 302 to credential-free HTTPS storage. Size, retries and total requests remain
bounded. The existing artifact ZIP redirect contract is unchanged. This matches the
[GitHub release-asset API contract](https://docs.github.com/en/rest/releases/assets?apiVersion=2022-11-28).
The returned compressed bytes pass the independently pinned hash/size and inert GNU reader before
the final publisher recheck. The protected caller must already admit the kit/profile; installation
separately authenticates its private kit lock and local archive source. This API grants no execution
authority and does not establish publisher attestation or reproducible-build provenance.

On 2026-10-08 the actual production client, without a token, freshly downloaded all three
numeric assets and passed this entire metadata/byte admission path. Each used 14 HTTP requests
(two six-record metadata brackets plus the API/storage download), 42 total. Returned lengths and
SHA-256 values exactly matched the committed table/lock. No archive program, extraction or runtime
execution occurred and no local cache was published. Root-private atomic archive publication and source-only installation are now connected by
`python_setup`; physical fixed-path Linux admission and production workflow integration remain
to be verified/completed.

## Publisher identities observed 2026-10-07

Repository: `actions/python-versions`. Selected platform: Ubuntu 24.04, x64, standard GIL build.
The three release-asset SHA-256 values were obtained from the publisher's GitHub API before
downloading any archive. Release names locate records; immutable numeric asset IDs and the
pre-existing byte lock identify the intended downloads. Every selected release is non-draft
and non-prerelease, but **mutable** (`immutable: false`). A tag or release URL alone is insufficient.

| Python | Release ID | Asset ID | Compressed bytes | Producer run, attempt 1 | Tag commit |
| --- | --- | --- | --- | --- | --- |
| 3.11.17 | 401059204 | 603458734 | 92586271 | [36876195594](https://github.com/actions/python-versions/actions/runs/36876195594) | `eb7756b60e2add788a3f5c9dba83102f67e076bf` |
| 3.12.15 | 400606086 | 602286467 | 95218570 | [36805895057](https://github.com/actions/python-versions/actions/runs/36805895057) | `96cf261124d1e3fbc49339879ac37f935c25653f` |
| 3.13.16 | 400609198 | 602296178 | 102730838 | [36805956071](https://github.com/actions/python-versions/actions/runs/36805956071) | `96cf261124d1e3fbc49339879ac37f935c25653f` |

All three observed producer runs are successful, completed `workflow_dispatch` runs on `main`
of `.github/workflows/build-python-packages.yml`; their API `head_sha` equals the corresponding
tag's direct commit. This is producer metadata evidence, not an independent reproducible build
or attestation verification. The API's `target_commitish: main` does not replace the exact tag
commit above.

Release tags are `3.11.17-36876195594`, `3.12.15-36805895057` and
`3.13.16-36805956071`. Publisher metadata is available from the corresponding
`https://api.github.com/repos/actions/python-versions/releases/<release-id>` and
`https://api.github.com/repos/actions/python-versions/releases/assets/<asset-id>` endpoints.
Downloads must use the asset endpoint, not resolve a mutable release filename at execution time.
Recheck exact owner, release/tag/commit, asset ID/name/size/digest and producer attempt before
and after bounded download; require actual bytes to equal the protected hash lock. API failures
or missing digests are admission failures, never first-download trust. For comparison, the
older 3.12.10 asset 244701306 has `digest: null` and is deliberately not locked here.

## Inert archive inspection

All three numeric-ID downloads completed and matched the pre-existing published hash and
exact compressed size. Asset metadata was reread after each download and remained identical.
Release ID/tag, direct tag commit and exact producer attempt identity/status were also rechecked
after the downloads and matched the retained identities above.
Only temporary files outside the checkout were written. No extraction, setup script, Python
binary or library from these archives was executed.

| Python | Archive members | Sum of member sizes | Symlinks | `.pyc`/`.pyo` members | ELF members |
| --- | --- | --- | --- | --- | --- |
| 3.11.17 | 10545 | 305635289 | 9 | 6237 | 81 |
| 3.12.15 | 9340 | 307444571 | 9 | 5733 | 82 |
| 3.13.16 | 9338 | 330812544 | 8 | 5696 | 82 |

Streaming inspection found no hard links, special members, normalized duplicate paths,
absolute paths or `..` path components. This inspection is evidence about these locked
archives, not a production archive reader or permission to extract them. Metadata includes
group/other-writable entries, including symlinks and bytecode/cache directories; blindly
preserving archive modes would not establish the protected tree required by tool admission.
The 3.11 archive also contains `lib/python3.11/site-packages/distutils-precedence.pth`, which
imports `_distutils_hack` using ambient `SETUPTOOLS_USE_DISTUTILS`. The other two have no `.pth`
members. A reviewed source-only protected installation must explicitly handle these files.

The three `setup.sh` SHA-256 values are respectively
`97f38bfa3d1d1745801bfc8b2b36c9044f01bb90c2931d2746643202584d90c1`,
`e38f16b42c6e5c7247abeaf915269fce1f560c19725b039692fec2b494315a4c` and
`e10c36ef78f9cc193102192a15005458b3af07960d607c440ef77ef2cccb042d`.
Their text matches the corresponding exact-commit
[installer template](https://github.com/actions/python-versions/blob/96cf261124d1e3fbc49339879ac37f935c25653f/installers/nix-setup-template.sh)
with version/architecture substitution and an additional final newline; the Git blob is
`16389f60a075099478cda1d35083ea85d90c773b`. This is byte comparison, not reproducible-build
verification. The script derives its destination from ambient tool-cache variables, deletes
an existing version directory and upgrades pip over the network. It cannot be adopted as the
closed protected installer unchanged.

Bounded static ELF64 program-header/dynamic-table inspection found search paths pointing at
`/opt/hostedtoolcache/Python/<exact-version>/x64/lib`. These are fixed compiled paths, not evidence
that arbitrary relocation is safe. Across all archive ELF members, direct `DT_NEEDED` names are
the version-specific `libpython3.<minor>.so.1.0`, `libbz2.so.1.0`, `libc.so.6`, `libcrypto.so.3`,
`libffi.so.8`, `libgdbm.so.6`, `libgdbm_compat.so.4`, `liblzma.so.5`, `libm.so.6`,
`libncursesw.so.6`, `libpanelw.so.6`, `libreadline.so.8`, `libsqlite3.so.0`, `libssl.so.3`,
`libtcl8.6.so`, `libtinfo.so.6`, `libtk8.6.so`, `libuuid.so.1` and `libz.so.1`.
3.11/3.12 additionally name `libcrypt.so.1`; 3.12/3.13 name `ld-linux-x86-64.so.2` in
`DT_NEEDED`. This does not enumerate `PT_INTERP`, resolve real host libraries, prove their bytes,
cover transitive dependencies or bound runtime `dlopen`; complete system closure remains open.

## Remaining execution admission

`build_ci.python_installation.install_privileged_python_archive` now implements the inactive
fixed-profile filesystem operation. The original caller/interpreter must already be
independently admitted. It authenticates actual root host role, the retained private kit and
its bounded archive lock, then reads the selected exact archive through stable no-follow
single-link descriptors beneath the protected home. A full hash-first inventory precedes
destination provisioning. Unknown versions and existing `x64` destinations are refused.

The operation installs at `/opt/hostedtoolcache/Python/<exact-version>/x64`, preserving the
provider's compiled prefix. It removes `setup.sh`, `.pyc`/`.pyo`, `.pth` and `__pycache__` paths,
requires the enrolled versioned executable and rejects any link whose target was removed.
Directories/files are root-owned, with directories/executable files `0755` and other regular
files `0644`; these public dependency bytes are readable but not writable by disposable UIDs.
Archive ownership and write bits are never applied. Regular files stream into exclusive
no-follow leaves, including empty files, and internal links are created only after the second
complete inventory agrees. The expected installation manifest derives from the approved
archive plus this fixed transformation; it is not a hash of an unknown installed tree promoted
to approval.

Actual inode/owner/mode/link/size/content and exact inventory checks bracket exclusive atomic
publication, with source/kit/parent rechecks and original named-root verification afterward.
A failed writer cleans its unpublished stage; newly provisioned empty ancestors can remain.
A late rejection after publication grants no execution authority. This operation downloads
nothing and executes no archive program. Authenticated producer metadata/download integration,
complete old/new interpreter/import/system closure and actual hosted installation remain open.
The two required hosted Linux probes exercise root-owned temporary synthetic copying and
failure cleanup without installing at a global prefix or executing copied bytes. Their bodies
compile locally; they have not been run on Linux here.

The transformation has also been derived from all three real locked archives without writing
an installation. These expected manifests are source-derived expectations, not observed host
tree approvals:

| Python | Selected entries | Regular files | Regular payload bytes | Expected installation manifest SHA-256 |
| --- | --- | --- | --- | --- |
| 3.11.17 | 4071 | 3731 | 143031161 | `37eebe8117eadf75e37617c7a19cc9313f46cfa642bf966688d660f521af9a3a` |
| 3.12.15 | 3419 | 3149 | 165665732 | `5c8b81a110821b8c6dc8cfaae03f4ad9044bad9933a57e3c8016939da9f2259b` |
| 3.13.16 | 3455 | 3167 | 178159094 | `4bc12f34397d808c03a1e38741f96f759546f436c9debc33b576c7ee17033243` |

Read-only compatibility evidence: the official
[Ubuntu 24.04 image inventory](https://github.com/actions/runner-images/blob/db776964592d0362a6bed85f90bc4e2980250e49/images/ubuntu/Ubuntu2404-Readme.md)
for image `20260927.320.1` documents cached Python 3.11.16/3.12.14/3.13.15 rather than the
selected patches. This suggests the selected slots are initially unused in that documented
image; it does not prove actual directory absence or native compatibility on a scheduled VM.
Actual exclusive publication still checks absence. Production setup must call this operation
before any action installs the selected patch. Future image changes or an occupied slot must
fail visibly and require a reviewed profile/setup update, never an unapproved tree adoption.

`build_ci.python_archive.inspect_python_installer` now performs compressed-lock-first inert
inventory with a separate 128 MiB compressed cap, 512 MiB expanded cap, 200:1 compression
ratio cap (including headers/padding), 20,000 nonzero raw
header cap and 10 KiB terminal padding cap. It uses fixed-size GNU TAR parsing rather than
general-purpose extraction; bounded long-name metadata counts toward the header budget.
Checks include checksum/CRC, UTF-8 paths, explicit directory parents, duplicates, nonzero
padding, unsupported types, contained acyclic file-resolving links and a final complete
compressed-byte hash recheck. The immutable sorted inventory retains exact source modes and
content hashes, including empty files. It applies no modes and writes no installation.
Caller descriptor identity, independent profile approval and physical source ownership remain
separate requirements. All three real locked archives pass this reader with the counts above.

Review the exact archive layout and installer program as inert bytes before execution. Retain
all existing filesystem, archive, source and process bounds; do not use general-purpose tar
extraction as an admission boundary. Establish a deterministic private install with no
network, no ambient installer settings, no mutable caches and independently authorized paths.
Do not invoke an archive's setup script merely because its compressed bytes match this lock.

Admit the complete installed interpreter, stdlib, extension/import roots and dynamically loaded
system-library closure from this reviewed profile and independently approved provenance.
`authenticate_toolchain_bytes` only proves the explicitly selected closure; hashing the current
host Python and approving that result does not satisfy this requirement. Actual Ubuntu Linux
installation, UID/access/ptrace, cancellation/timeout/descendant cleanup and privileged-launch
tests remain mandatory before production workflow wiring. Existing setup-python CI selection
is unchanged by this inactive lock.
