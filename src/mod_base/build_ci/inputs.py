"""The validator's fixed inputs and what a derivation hands back (MB11).

``validation-input/`` holds what a protected hook may read besides the sealed exports: the bytes of
the candidate files a plan is derived from (the inventory, the scenario contract and the extra
plan inputs the protected config names, each under its staged name) and, once the plan exists,
the canonical plan, which binds every one of them by SHA-256. The runner stages the directory
privately, root hands it to the validator read-only, and every later check requires exactly
those files. ``derive_plan`` answers in the validator's own home; root hands an independent
private copy to the runner and removes the original, so the next protected hook of the job
starts clean.

A Build verification reads ``sealed-build/`` beside it. :func:`read_sealed_build` is how the
runner reads that directory before and after root hands it over, and
:func:`execute_frozen_build_validator` binds one ``verify_build`` or ``verify_target`` run to both
read-only roots.

No schema or success authority is introduced: plan and envelope retain their existing kinds.
Protected admission must retain genuine source/plan/export provenance and exclude other writers.
"""

from __future__ import annotations

import hashlib
import copy
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from mod_base.build_ci import adapter
from mod_base.build_ci.controller import ControllerFile, ControllerSources, _write_controller_files
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, verify_build_export
from mod_base.build_ci.host import (HostBoundary, _open_directory, authenticate_host_boundary,
                                    authenticate_privileged_host_boundary)
from mod_base.build_ci.protocol import SHA256, validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.build_ci.validation import VALIDATOR_OUTPUT_ROOT, freeze_validation_export
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_peer_account, authenticate_worker_account, terminate_worker)
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import (authenticate_tree_private_access, authenticate_tree_read_access,
                              copy_regular_files, file_records, grant_tree_read_access, privatize_tree_copy,
                              read_child_file, validate_tree_entries)
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Int, check


VALIDATOR_INPUT_ROOT = WORKER_ROOT / adapter.INPUT_DIRECTORY
#: The runner's private copy of what ``derive_plan`` wrote, as root handed it over.
DERIVED_PLAN_ROOT = WORKER_ROOT / "derived-plan"


@dataclass(frozen=True)
class BuildValidationExecution:
    execution: WorkerResult
    input_sha256: str


def _plan_bytes(plan: dict[str, Any]) -> bytes:
    validate_plan(plan)
    raw = canonical_json(plan)
    check(len(raw) <= limits.MAX_CI_PLAN_BYTES, "$.plan", "protected plan exceeds byte cap")
    return raw


def plan_source_digests(plan: dict[str, Any]) -> dict[str, str]:
    """Staged name -> SHA-256 of every candidate file a valid plan was derived from, in the order
    of ``validation-input/``: the inventory and the scenario contract its identity binds, then the
    extra plan inputs it lists."""

    identity = plan["identity"]
    return {adapter.INVENTORY_INPUT: identity["inventory_sha256"], adapter.SCENARIO_INPUT: identity["scenario_sha256"],
            **{item["name"]: item["sha256"] for item in plan["plan_inputs"]}}


def _expected_inputs(digests: Mapping[str, str], plan: dict[str, Any] | None) -> tuple[dict[str, str], bytes | None]:
    """File name -> SHA-256 of the input root, and the plan's bytes once there is a plan.

    ``digests`` names the candidate files: always the inventory and the scenario contract, and
    the extra plan inputs of the protected config under their own names.
    """

    check(isinstance(digests, Mapping) and all(type(name) is str for name in digests)
          and {adapter.INVENTORY_INPUT, adapter.SCENARIO_INPUT} <= set(digests)
          and len(digests) <= 2 + limits.MAX_CI_PLAN_INPUTS, "$.inputs",
          "must name the inventory, the scenario contract and at most the extra plan inputs")
    for name, digest in digests.items():
        if name not in (adapter.INVENTORY_INPUT, adapter.SCENARIO_INPUT):
            adapter.plan_input_name(name, f"$.inputs.{name}")
        SHA256(digest, f"$.inputs.{name}")
    if plan is None:
        return dict(digests), None
    raw = _plan_bytes(plan)
    check(plan_source_digests(plan) == dict(digests), "$.plan", "the plan was derived from other candidate files")
    return {**digests, adapter.PLAN_INPUT: hashlib.sha256(raw).hexdigest()}, raw


def verify_validation_inputs(root: Path, *, digests: Mapping[str, str],
                             plan: dict[str, Any] | None = None) -> None:
    """Require exactly the validator's input files, with these bytes, and nothing undeclared.

    Before there is a plan (``derive_plan``) these are the candidate files ``digests`` names with
    their SHA-256. Afterwards the canonical plan joins them and must bind every one of them. This
    checks names and bytes; ownership and modes of the directory are separate admissions.
    """

    expected, raw = _expected_inputs(digests, plan)
    validate_tree_entries(root, max_entries=len(expected) + 1)  # The files and their directory: no other entry.
    observed = file_records(root, max_files=limits.MAX_CI_PLAN_INPUT_FILES,
                            max_total_bytes=limits.MAX_CI_PLAN_INPUT_BYTES,
                            max_file_bytes=max(limits.MAX_CI_PLAN_BYTES, limits.MAX_CI_PLAN_SOURCE_BYTES))
    check(len(observed) == len(expected)
          and {record["path"]: record["sha256"] for record in observed} == expected,
          "$.input", "validator input inventory differs from protected input")
    check(all(record["size"] <= limits.MAX_CI_PLAN_SOURCE_BYTES for record in observed
              if record["path"] != adapter.PLAN_INPUT), "$.input", "candidate file exceeds its byte cap")
    if raw is not None:
        check(read_child_file(root, adapter.PLAN_INPUT, max_bytes=limits.MAX_CI_PLAN_BYTES) == raw,
              "$.plan", "validator plan bytes differ from retained protected plan")


def verify_validation_plan(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Require the input root of a hook that runs against the plan: the canonical plan, every
    candidate file it binds, and no undeclared file."""

    verify_validation_inputs(root, digests=plan_source_digests(validate_plan(plan)), plan=plan)
    return plan


def _source_digests(sources: Mapping[str, bytes]) -> dict[str, str]:
    check(isinstance(sources, Mapping), "$.sources", "must map each staged name to the bytes of its candidate file")
    for name, data in sources.items():
        check(type(data) is bytes and 1 <= len(data) <= limits.MAX_CI_PLAN_SOURCE_BYTES, f"$.sources.{name}",
              f"must be 1..{limits.MAX_CI_PLAN_SOURCE_BYTES} bytes of the candidate file")
    return {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}


def materialize_validation_inputs(output: Path, *, sources: Mapping[str, bytes],
                                  plan: dict[str, Any] | None = None) -> None:
    """Independently write the validator's input files into an exclusive private protected stage.

    ``sources`` holds the bytes of every candidate file a plan is derived from under its staged
    name (``adapter.plan_sources``); ``plan`` joins them once it exists. An existing ``output`` is
    never replaced.
    """

    digests = _source_digests(sources)
    _, raw = _expected_inputs(digests, plan)
    contents = {**sources, **({} if raw is None else {adapter.PLAN_INPUT: raw})}
    files = tuple(ControllerFile(
        name, "100644", hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest(),
        hashlib.sha256(data).hexdigest(), data) for name, data in sorted(contents.items()))

    def writer(stage: Path, descriptor: int) -> None:
        _write_controller_files(descriptor, files)
        verify_validation_inputs(stage, digests=digests, plan=plan)

    atomic_directory(output, writer)


def _accounts(boundary: HostBoundary, validator: WorkerAccount) -> WorkerAccount | None:
    """The job's candidate, when it has one, after binding the validator and the isolation of both."""

    if type(boundary) is not HostBoundary:
        raise WorkerError("validation input requires retained host boundary evidence")
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("validation input requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("validation input validator identity changed")
    return authenticate_peer_account(validator, runner_uid=boundary.uid, runner_gid=boundary.gid)


def _layout(boundary: HostBoundary) -> None:
    for path in (WORKER_ROOT.parent, WORKER_ROOT):
        descriptor = _open_directory(tuple(path.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o711):
                raise WorkerError("validation input traversal layout changed")
        finally:
            os.close(descriptor)


def prepare_plan_inputs(*, boundary: HostBoundary, validator: WorkerAccount, digests: Mapping[str, str]) -> None:
    """Root-only read grant of the staged candidate files, before ``derive_plan`` runs.

    ``digests`` is the SHA-256 of each one by staged name."""

    _prepare_validation_inputs(boundary, validator, digests, None)


def prepare_validation_plan(*, boundary: HostBoundary, validator: WorkerAccount,
                             plan: dict[str, Any]) -> dict[str, Any]:
    """Root-only read grant of the complete input root: the plan and the candidate files it binds."""

    _prepare_validation_inputs(boundary, validator, plan_source_digests(validate_plan(plan)), plan)
    return plan


def _prepare_validation_inputs(boundary: HostBoundary, validator: WorkerAccount, digests: Mapping[str, str],
                               plan: dict[str, Any] | None) -> None:
    """Hand the fixed runner-owned private input root to the validator's group, bytes rechecked."""

    authenticate_privileged_host_boundary(boundary)
    _expected_inputs(digests, plan)
    expected = dict(digests=dict(digests), plan=plan)
    candidate = _accounts(boundary, validator)
    descriptor = None
    admitted = False
    try:
        if candidate is not None:
            terminate_worker(candidate)
        _layout(boundary)
        descriptor = _open_directory(tuple(VALIDATOR_INPUT_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (boundary.uid, boundary.gid, 0o700):
            raise WorkerError("validation input must be a private runner-owned independent copy")
        admitted = True
        verify_validation_inputs(VALIDATOR_INPUT_ROOT, **expected)
        grant_tree_read_access(VALIDATOR_INPUT_ROOT, source_owner_uid=boundary.uid,
                               owner_uid=boundary.uid, reader_gid=validator.gid,
                               max_files=limits.MAX_CI_PLAN_INPUT_FILES, max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES,
                               max_total_bytes=limits.MAX_CI_PLAN_INPUT_BYTES,
                               max_file_bytes=max(limits.MAX_CI_PLAN_BYTES, limits.MAX_CI_PLAN_SOURCE_BYTES))
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, validator.gid, 0o750)):
            raise WorkerError("validation input handoff identity or permissions changed")
        authenticate_tree_read_access(VALIDATOR_INPUT_ROOT, owner_uid=boundary.uid,
                                      reader_gid=validator.gid, max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES)
        verify_validation_inputs(VALIDATOR_INPUT_ROOT, **expected)
        authenticate_privileged_host_boundary(boundary)
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("validation input could not restore private traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot prepare protected validation input") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _discard(root: PurePosixPath, names: tuple[str, ...]) -> None:
    """Remove the flat directory ``root`` that holds exactly the files ``names``.

    For a directory the caller has just authenticated and that no live account can write. A
    link is never followed, and a directory that holds anything else is left exactly as it is.
    """

    parent = _open_directory(tuple(root.parent.parts[1:]))
    try:
        directory = _open_directory((root.name,), root=parent)
        try:
            if sorted(os.listdir(directory)) != sorted(names):
                raise WorkerError("a directory to discard holds something else than its expected files")
            for name in names:
                os.unlink(name, dir_fd=directory)
        finally:
            os.close(directory)
        os.rmdir(root.name, dir_fd=parent)
        os.fsync(parent)
    finally:
        os.close(parent)


def replace_plan_inputs(*, boundary: HostBoundary, validator: WorkerAccount, sources: Mapping[str, bytes],
                        plan: dict[str, Any]) -> None:
    """Runner-only: replace the input root ``derive_plan`` read with the complete private one.

    The validator is terminated first. The directory it was granted must still be exactly the
    staged candidate files ``sources``; it is removed and written again with the plan, private
    to the runner, for root to hand over (:func:`prepare_validation_plan`).
    """

    authenticate_host_boundary(boundary)
    _accounts(boundary, validator)
    digests = _source_digests(sources)
    names, _ = _expected_inputs(digests, None)
    try:
        terminate_worker(validator)
        _layout(boundary)
        authenticate_tree_read_access(VALIDATOR_INPUT_ROOT, owner_uid=boundary.uid, reader_gid=validator.gid,
                                      max_entries=limits.MAX_CI_PLAN_INPUT_ENTRIES)
        verify_validation_inputs(VALIDATOR_INPUT_ROOT, digests=digests)
        _discard(VALIDATOR_INPUT_ROOT, tuple(names))
        materialize_validation_inputs(Path(str(VALIDATOR_INPUT_ROOT)), sources=sources, plan=plan)
    except OSError as error:
        raise WorkerError("cannot replace the validator's plan inputs") from error


def take_derived_plan(*, boundary: HostBoundary, validator: WorkerAccount) -> None:
    """Root-only: give the runner a private copy of what ``derive_plan`` wrote and remove the original.

    The validator is terminated first. Its output directory must be exactly the one file the
    contract names: private, regular, single-linked, within the derivation cap. Anything else
    (a missing or an extra file, a link, another mode) fails and hands nothing over. The copy has
    its own inodes, in the fixed runner-private :data:`DERIVED_PLAN_ROOT`, which must not exist
    yet. The original is never chowned; it is removed, so that the validator's next hook finds no
    output of this one.
    """

    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    names = adapter.hook_outputs("derive_plan")
    bounds = dict(max_files=len(names), max_entries=len(names) + 1,
                  max_total_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES, max_file_bytes=limits.MAX_CI_PLAN_SOURCE_BYTES)
    output = Path(str(DERIVED_PLAN_ROOT))
    try:
        terminate_worker(validator)
        _layout(boundary)
        home = _open_directory(tuple(VALIDATOR_OUTPUT_ROOT.parent.parts[1:]))
        try:
            info = os.fstat(home)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (validator.uid, validator.gid, 0o700):
                raise WorkerError("validator home identity changed before the derivation was taken")
        finally:
            os.close(home)
        authenticate_tree_private_access(VALIDATOR_OUTPUT_ROOT, owner_uid=validator.uid,
                                         owner_gid=validator.gid, max_entries=bounds["max_entries"])

        def writer(stage: Path, stage_fd: int) -> list[dict[str, Any]]:
            copied = copy_regular_files(VALIDATOR_OUTPUT_ROOT, stage_fd, **bounds)
            check([record["path"] for record in copied] == list(names), "$.derivation",
                  "derive_plan must leave exactly its one output file")
            return copied

        copied = atomic_directory(output, writer)
        privatize_tree_copy(output, source_owner_uid=0, owner_uid=boundary.uid, owner_gid=boundary.gid, **bounds)
        authenticate_tree_private_access(output, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                         max_entries=bounds["max_entries"])
        check(file_records(output, max_files=bounds["max_files"], max_total_bytes=bounds["max_total_bytes"],
                           max_file_bytes=bounds["max_file_bytes"]) == copied,
              "$.derivation", "derived plan changed during the ownership transfer")
        _discard(VALIDATOR_OUTPUT_ROOT, names)
        authenticate_privileged_host_boundary(boundary)
    except OSError as error:
        raise WorkerError("cannot take the derived plan from the validator") from error
    finally:
        terminate_worker(validator)


def _read_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                  envelope: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    authenticate_host_boundary(boundary)
    return _inspect_inputs(boundary, validator, plan, envelope)


def _inspect_inputs(boundary: HostBoundary, validator: WorkerAccount, plan: dict[str, Any],
                     envelope: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    """Shared metadata/byte checks after the caller authenticates its runner or root role."""

    _layout(boundary)
    identities = []
    for root, cap in ((VALIDATOR_INPUT_ROOT, limits.MAX_CI_PLAN_INPUT_ENTRIES),
                      (BUILD_VALIDATION_ROOT, limits.MAX_CI_EXPORT_ENTRIES)):
        descriptor = _open_directory(tuple(root.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, validator.gid, 0o750):
                raise WorkerError("validation input root ownership or permissions changed")
            identities.append((info.st_dev, info.st_ino))
        finally:
            os.close(descriptor)
        authenticate_tree_read_access(root, owner_uid=boundary.uid, reader_gid=validator.gid, max_entries=cap)
    verify_validation_plan(VALIDATOR_INPUT_ROOT, plan=plan)
    check(verify_build_export(BUILD_VALIDATION_ROOT, plan=plan) == envelope, "$.input",
          "frozen Build differs from retained protected input")
    return tuple(identities)


def build_input_sha256(*, plan: dict[str, Any], envelope: dict[str, Any], hook: str, unit_id: str | None,
                       run_id: int, run_attempt: int) -> str:
    """Require the sealed Build one verification is for and return the digest of that input.

    ``envelope`` must be a Build export of ``plan`` sealed by this run attempt: the complete Build
    for ``verify_build`` (which takes no unit), exactly the partition of target ``unit_id`` for
    ``verify_target``. The digest is the SHA-256 of the canonical envelope, which names every
    file the hook reads by size and hash; a validation record carries it as ``input_sha256``.
    """

    _plan_bytes(plan)
    validate_build_envelope(envelope, plan=plan)
    Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    if hook == "verify_build":
        check(unit_id is None and envelope["scope"] == "complete" and envelope["target_id"] is None,
              "$.input", "aggregate Build verification requires the complete bundle")
    else:
        check(hook == "verify_target" and type(unit_id) is str
              and unit_id in {target["id"] for target in plan["targets"]},
              "$.target_id", "target verification requires its exact protected enrolled target")
        check(envelope["scope"] == "target" and envelope["target_id"] == unit_id, "$.input",
              "target verification requires exactly its own frozen partition")
    check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
          "$.input", "Build verifier differs from its producing run/attempt")
    raw = canonical_json(envelope)
    check(len(raw) <= limits.MAX_CI_ENVELOPE_BYTES, "$.input", "retained envelope exceeds byte cap")
    return hashlib.sha256(raw).hexdigest()


def read_sealed_build(*, boundary: HostBoundary, validator: WorkerAccount,
                      plan: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Runner-only: the envelope of ``sealed-build/`` and whether the validator reads it already.

    The directory is the runner's private export (the partition ``ci worker-seal`` froze, the
    Build ``ci assemble`` put together or the one ``ci fetch-build`` materialised: the runner's,
    mode 0700) or that export after root handed it to this job's validator (the validator's group,
    0750, every entry read-only for it). Either way its files are exactly its canonical envelope,
    a Build export of ``plan``, and the directory is the same one before and after it was read.
    A missing directory, another owner or mode and any other file set are rejections.
    """

    authenticate_host_boundary(boundary)
    _accounts(boundary, validator)
    _layout(boundary)

    def state() -> tuple[int, ...]:
        descriptor = _open_directory(tuple(BUILD_VALIDATION_ROOT.parts[1:]))
        try:
            info = os.fstat(descriptor)
            return info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)
        finally:
            os.close(descriptor)

    try:
        initial = state()
        granted = initial[2:] == (boundary.uid, validator.gid, 0o750)
        check(granted or initial[2:] == (boundary.uid, boundary.gid, 0o700), "$.input",
              "sealed-build/ is neither the runner's private export nor granted to this job's validator")
        if granted:
            authenticate_tree_read_access(BUILD_VALIDATION_ROOT, owner_uid=boundary.uid, reader_gid=validator.gid,
                                          max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        envelope = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
        check(state() == initial, "$.input", "sealed-build/ changed while it was read")
    except OSError as error:
        raise WorkerError("cannot read the sealed Build export") from error
    return envelope, granted


def execute_frozen_build_validator(*, boundary: HostBoundary, validator: WorkerAccount,
                                   plan: dict[str, Any], envelope: dict[str, Any], hook: str,
                                   unit_id: str | None, run_id: int, run_attempt: int,
                                   execute: Callable[[], WorkerResult]) -> BuildValidationExecution:
    """Bind one Build verification to the read-only plan and Build it is given.

    ``hook`` is ``verify_build`` over the complete Build of this run attempt, or ``verify_target``
    over exactly the frozen partition of target ``unit_id``; a complete bundle never stands in
    for a partition. ``execute`` runs that hook as the validator and returns its result
    (``lifecycle.run_protected_hook``). Both input roots (``validation-input/`` and
    ``sealed-build/``) are authenticated in metadata and bytes before and after it and must be
    the same directories. Returns the execution with the digest of the canonical envelope, for
    the receipt freeze. The validator is terminated whatever happens. Native semantics, source
    provenance and final API authority remain separate.
    """

    _accounts(boundary, validator)
    try:
        digest = build_input_sha256(plan=plan, envelope=envelope, hook=hook, unit_id=unit_id, run_id=run_id,
                                    run_attempt=run_attempt)
        initial = _read_inputs(boundary, validator, plan, envelope)
        result = execute()
        if _read_inputs(boundary, validator, plan, envelope) != initial:
            raise WorkerError("validation input directory identities changed during execution")
        return BuildValidationExecution(result, digest)
    except OSError as error:
        raise WorkerError("cannot execute frozen Build verification") from error
    finally:
        terminate_worker(validator)


def freeze_frozen_build_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                                    sources: ControllerSources, bound: BuildValidationExecution,
                                    plan: dict[str, Any], envelope: dict[str, Any],
                                    run_id: int, run_attempt: int) -> dict[str, Any]:
    """Root-only bind retained Build execution to exact live inputs and independent receipt freeze.

    Hook/unit/input digest come from the retained validated envelope, never a caller's separate
    free-form freeze context. Caller retains genuine execution/source provenance across the
    protected privilege transition. Matching constructible objects confer no authority. Native
    domain verification and final graph/API/upload/status admission remain additional obligations.
    """

    authenticate_privileged_host_boundary(boundary)
    _accounts(boundary, validator)
    try:
        _plan_bytes(plan)
        validate_build_envelope(envelope, plan=plan)
        Int(1, limits.MAX_RUN_ID)(run_id, "$.run_id")
        Int(1, limits.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
        check((envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]) == (run_id, run_attempt),
              "$.input", "validation freeze differs from its producing run/attempt")
        raw = canonical_json(envelope)
        check(len(raw) <= limits.MAX_CI_ENVELOPE_BYTES, "$.input", "retained envelope exceeds byte cap")
        check(type(bound) is BuildValidationExecution
              and bound.input_sha256 == hashlib.sha256(raw).hexdigest(),
              "$.execution.input_sha256", "retained execution differs from exact frozen input")
        execution = bound.execution
        check(type(execution) is WorkerResult and type(execution.returncode) is int and execution.returncode == 0
              and type(execution.log) is bytes and len(execution.log) <= limits.MAX_CI_LOG_BYTES
              and type(execution.truncated) is bool,
              "$.execution", "validation freeze requires retained successful bounded execution")
        plan, envelope = copy.deepcopy(plan), copy.deepcopy(envelope)
        hook, unit_id = (("verify_build", None) if envelope["scope"] == "complete"
                         else ("verify_target", envelope["target_id"]))
        terminate_worker(validator)
        initial = _inspect_inputs(boundary, validator, plan, envelope)
        receipt = freeze_validation_export(boundary=boundary, validator=validator, sources=sources,
                    execution=execution, plan=plan, hook=hook, unit_id=unit_id,
                    run_id=run_id, run_attempt=run_attempt, input_sha256=bound.input_sha256)
        if _inspect_inputs(boundary, validator, plan, envelope) != initial:
            raise WorkerError("validation input identities changed during receipt freeze")
        authenticate_privileged_host_boundary(boundary)
        return receipt
    except OSError as exc:
        raise WorkerError("cannot bind frozen Build validation receipt") from exc
    finally:
        terminate_worker(validator)
