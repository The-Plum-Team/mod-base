# 0007. Share protected Build and packaged runtime mechanisms

Date: 2026-10-07

## Status

Accepted for implementation by the request to execute BUILD-E2E-DESIGN.md. Consumer activation,
hosted canary results, release completion and separately authorized governance/App operations
are not established by this ADR.

## Decision

The profile activation manifest is a new mod-base.ci.activation v1 kind, current 1/previous null,
with explicit predecessor rejection. Its repository/native profile and five closed modes are
pure data under an 8 KiB reader cap. No pin/job/template/permission/secret or owner-approval
selection is added. Physical original configuration binding, exact protected transitions,
predecessors, rendered callers and reviewed rollback remain separate required admission.

The local metadata-only root-freeze request is a new `mod-base.ci.root-request` v1 kind
(current 1/previous null), with explicit predecessor rejection in the exhaustive ledger.
It preserves all native/source/plan/envelope bounds and adds only a separate local request cap.
It carries no program or permissions grant. Physical protected-origin context transfer and the
fixed production operation remain prerequisites; a valid request is not execution authority.

Implement the complete staged design in [BUILD-E2E-DESIGN.md](../BUILD-E2E-DESIGN.md): kit first,
Quick Skin second, Block Pops third. Preserve all native obligations, strict dependencies and
protected authority. Disposable candidate execution and second-account validation precede
credential-bearing upload. A current live source/approval check precedes App success and merge.

Keep Pages protocols and existing workflow names unchanged. New Build adapter API 1 is
independent of Pages adapter API 1. New document kinds start at schema 1. Shared Build and
packaged full-execution graph contracts start at 1; hosted job-name behavior must be demonstrated
before release. Current execution graph selection comes only from protected caller/pin/profile.

Target v1.1.0 for the additive release, subject to checking that it remains unused at release.
Keep the runtime version unchanged during foundational work. A release still requires protected
merge, exact-commit CI, immutable annotated tag, hosted canary and published release in that order.

Amend ADR 0004 with the exhaustive per-kind compatibility ledger: every current fixture of an
unchanged common kind/schema passes the immutable predecessor reader, every supported old
fixture passes the current reader, and every new kind explicitly fails in the old reader. No
fixture is hidden from compatibility testing. Strict unknown-key handling remains; an optional
new field needs a compatible writer omission policy or an explicit version/capability decision.

The new `build_ci` package owns generic identity/plan/graph mechanisms (MB11 in INTERNAL-API).
Mod adapters own inventory/scenario parsing, fixed dispatch, native compiler/runtime witnesses,
release routing and protected approval policy. Plans cannot return commands, permissions,
runner labels or arbitrary uploads.

## Consequences

The first inactive Build envelope format is finalized at schema 1 with a pre-upload producer
identity (run/attempt, workflow path/ref, API head/event and protected graph contract). Its local
draft included its own future upload window, which cannot be authenticated while writing the
ZIP. Discard that unreleased draft shape and reject it explicitly; selected descriptors retain
mandatory real API windows and immutable ID/digest/size/expiry. Independently bind every identity
field and scope/target without rewriting export bytes. Released v1.0.3 and the committed
predecessor registry have no Build envelope kind; the exhaustive compatibility ledger therefore
continues to advertise a new kind, current 1, previous null, with old-reader rejection. This is
an explicit initial-format decision before release/activation, not an exception to required-field
versioning for released schemas. The same initial-format decision applies to the new inactive
gate and reuse kinds: their record producer contains the seven pre-upload fields; source/input
descriptors retain actual already-completed uploads, and final selected record descriptors
authenticate the eventual upload. Mandatory binding preserves writer identity/kind/unit,
source-before-record chronology and distinct artifact IDs. Reuse additionally requires real
source completion before protected verifier start through independent API execution evidence;
that K6 proof and final record transport remain incomplete. Their current-1/previous-null ledger
and old-reader rejection remain explicit; this does not change any released schema.

The inactive local verifier-output protocol adds `mod-base.ci.validation` at schema 1 for the
planned v1.1.0 release. It binds native verification reports to protected plan/hook/unit,
run/attempt, source-config hash and independently authenticated input hash. This is a new kind,
not an optional addition to existing gate/envelope records; v1.0.3 rejects it explicitly in the
exhaustive compatibility ledger. Canonical strict native JSON and exact byte inventory still
do not establish mod-owned closed-schema semantics or real protected verifier execution.

The inactive local runner-to-root data channel adds `mod-base.ci.execution` schema 1 before the
planned first v1.1.0 release. It preserves a bounded binary execution log and exact nonce/attempt/
source/plan/input context in a private runner-owned file. This is a new local kind with explicit
v1.0.3 rejection in the exhaustive ledger; it is not an artifact descriptor, native validation
record, proof of actual execution or a substitute for enrolled privileged-program/import setup.

The inactive root-owned installation record adds `mod-base.ci.kit-installation` schema 1,
current 1/previous null with explicit v1.0.3 rejection. Its fixed private local bytes retain
kit ref/digest, bounded counts and unsigned 64-bit root device/inode. It selects no program,
path, permission or status and does not alter any released kind. Protected-caller admission
of the actual copy surrounds reading/writing; pre-import bootstrap, interpreter/program
enrollment and independent pin provenance remain separate required work.

Validation primitives do not authorize execution or status publication. Missing evidence and
corruption remain distinct; API failures cannot select an older successful generation. Complete
lane/target coverage, bounds, native provenance and direct-only coherent reuse must be proven
independently. Windows tests cannot establish the Linux UID or hosted API boundaries.

The inactive batch manifest adds new mod-base.ci.batch schema 1, current 1/previous null,
with explicit archived v1.0.3 rejection. It preserves the 50-member limit and requires ordered
source/patch/parent/result identities with policy and complete-source byte fingerprints.
This shape decision changes no released kind or Pages protocol. Shape and fingerprint labels
cannot establish live API provenance, native approval, safe Git construction or settlement.

Track unverified requirements in [BUILD-E2E-PROGRESS.md](../BUILD-E2E-PROGRESS.md); a step remains
incomplete until all of its required evidence exists. Consumer/App/governance operations retain
the staged owner authorization and legacy gates specified by the design.
