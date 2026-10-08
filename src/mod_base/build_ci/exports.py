"""Independent Build export and complete target-union verification (MB11).

Call only over frozen exports after the disposable processes have been terminated and locked.
These checks establish bytes/coverage, not native compiler validity or API provenance.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_worker_account, terminate_worker)
from mod_base.build_ci.source import GitSourceEntry, verify_source_copy
from mod_base.build_ci.records import bind_build_envelope, validate_build_envelope, validate_descriptor
from mod_base.io.secure_json import loads
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import (EXPORT_PATHS, authenticate_tree_private_access, copy_regular_files, copy_selected_regular_files,
                              file_records, grant_tree_read_access, privatize_tree_copy, read_child_file, validate_tree_entries)
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Int, List, Obj, check


BUILD_VALIDATION_ROOT = WORKER_ROOT / "sealed-build"
CANDIDATE_SOURCE_ROOT = WORKER_ROOT / "repository"
CANDIDATE_OUTPUT_ROOT = WORKER_ROOT / "candidate-home" / "export"


def validate_target_partitions(partitions: Any, *, plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sorted complete file union of exact ordered plan-target receipts.

    Descriptors must already have been independently authenticated against the owning API
    attempt. This function never infers coverage from artifact names or accepts a partial set.
    """

    validate_plan(plan)
    List(Obj({"descriptor": validate_descriptor,
              "envelope": lambda value, path: validate_build_envelope(value, plan=plan, path=path)}),
         min_items=1, max_items=lim.MAX_CI_TARGETS)(partitions, "$.partitions")
    check(len(partitions) == len(plan["targets"]), "$.partitions", "target partition set is incomplete")
    artifact_ids: set[int] = set()
    producer = None
    files = []
    for index, (partition, target) in enumerate(zip(partitions, plan["targets"])):
        path = f"$.partitions[{index}]"
        descriptor, envelope = partition["descriptor"], partition["envelope"]
        validate_build_envelope(envelope, plan=plan, path=f"{path}.envelope")
        check(envelope["scope"] == "target" and envelope["target_id"] == target["id"], path,
              "partition does not match the exact ordered protected target")
        bind_build_envelope(envelope, descriptor=descriptor, plan=plan)
        for key in ("identity", "plan_sha256", "profile"):
            check(descriptor[key] == envelope[key], f"{path}.descriptor.{key}",
                  "descriptor does not bind the export receipt")
        name = g.parse_ci_artifact_name(descriptor["artifact"]["name"])
        check(name.kind == "target" and name.unit_id == target["id"], f"{path}.descriptor.artifact.name",
              "must name this target partition")
        artifact_id = descriptor["artifact"]["id"]
        check(artifact_id not in artifact_ids, path, "duplicate target artifact id")
        artifact_ids.add(artifact_id)
        current = envelope["producer"]
        if producer is None:
            producer = current
        check(current == producer, f"{path}.envelope.producer", "mixed target producer or attempt")
        files.extend(envelope["files"])
    check(len(files) <= lim.MAX_CI_EXPORT_FILES, "$.partitions", "complete export exceeds file-count cap")
    check(sum(file["size"] for file in files) <= lim.MAX_CI_EXPORT_TREE_BYTES,
          "$.partitions", "complete export exceeds whole-tree byte cap")
    directories = {"/".join(file["path"].split("/")[:index]) for file in files
                   for index in range(1, len(file["path"].split("/")))}
    check(len(files) + len(directories) + 2 <= lim.MAX_CI_EXPORT_ENTRIES,
          "$.partitions", "complete logical export exceeds entry cap")
    return sorted(files, key=lambda file: file["path"])


def verify_build_export(root: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Read the bounded canonical outer envelope and match every frozen file's size/hash.

    Uses MB1 descriptor-relative no-follow traversal and single-link regular-file streaming.
    Extra files, missing files, links, empty files and unsafe path components fail closed: every
    entry must be an export path (``grammar.is_export_path``, the mod's own file names) and no two
    may differ only in case.
    Native domain validation and authenticated transport must additionally pass before upload.
    """

    validate_plan(plan)
    validate_tree_entries(root, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
    raw = read_child_file(root, g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES)
    envelope = loads(raw, label=g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES)
    validate_build_envelope(envelope, plan=plan)
    check(raw == canonical_json(envelope), "$.envelope", "outer envelope must be canonical JSON")
    observed = file_records(root, exclude=(g.CI_ENVELOPE_NAME,),
                            max_files=lim.MAX_CI_EXPORT_FILES + 1,
                            max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                            max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
    expected = [{key: file[key] for key in ("path", "sha256", "size")} for file in envelope["files"]]
    check(observed == expected, "$.files", "frozen export differs from its exact file inventory")
    check(read_child_file(root, g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES) == raw,
          "$.envelope", "outer envelope changed during verification")
    return envelope


def assemble_build_export(inputs: Path, *, partitions: list[dict[str, Any]], plan: dict[str, Any],
                           run_id: int, run_attempt: int, output: Path) -> dict[str, Any]:
    """Independently assemble the exact ordered target inputs into one atomic complete export.

    Inputs are the fixed target-ordinal children from authenticated target-set transport. Caller
    protects source/output ancestors and excludes writers; descriptors must retain real API
    admission. Native aggregate validation, protected policy success and upload/gate authority
    remain mandatory after byte assembly. This does not mint native observations or gate receipts.
    """

    files = validate_target_partitions(partitions, plan=plan)
    Int(1, lim.MAX_RUN_ID)(run_id, "$.run_id")
    Int(1, lim.MAX_RUN_ATTEMPT)(run_attempt, "$.run_attempt")
    producer = partitions[0]["envelope"]["producer"]
    check((producer["run_id"], producer["run_attempt"]) == (run_id, run_attempt),
          "$.producer", "assembly differs from protected same-attempt targets")
    expected = {**partitions[0]["envelope"], "schema_version": SCHEMA_VERSIONS["mod-base.build.envelope"],
                "scope": "complete", "target_id": None, "files": files,
                "native_reports": [file["path"] for file in files if file["role"] == "native-report"]}
    validate_build_envelope(expected, plan=plan)
    raw = canonical_json(expected)
    check(len(raw) <= lim.MAX_CI_ENVELOPE_BYTES, "$.envelope", "complete envelope exceeds its byte cap")
    expected = loads(raw, label=g.CI_ENVELOPE_NAME, max_bytes=lim.MAX_CI_ENVELOPE_BYTES)
    roots = [inputs / f"target-{index}" for index in range(len(partitions))]

    def verify_inputs() -> None:
        validate_tree_entries(inputs, max_entries=lim.MAX_CI_TARGET_INPUT_ENTRIES)
        with os.scandir(inputs) as entries:
            names = sorted(entry.name for entry in itertools.islice(entries, lim.MAX_CI_TARGETS + 1))
        check(names == sorted(root.name for root in roots), "$.inputs", "target input children differ from fixed ordinals")
        for root, partition in zip(roots, partitions):
            check(verify_build_export(root, plan=plan) == partition["envelope"],
                  "$.inputs", "target bytes differ from admitted partition")

    try:
        try:
            resolved_inputs, resolved_output = inputs.resolve(), output.resolve()
        except (OSError, RuntimeError) as error:
            raise WorkerError("cannot resolve protected assembly roots") from error
        check(not resolved_output.is_relative_to(resolved_inputs)
              and not resolved_inputs.is_relative_to(resolved_output), "$.output", "assembly inputs/output overlap")
        verify_inputs()

        def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
            copied = []
            for root, partition in zip(roots, partitions):
                envelope = partition["envelope"]
                records = copy_selected_regular_files(root, stage_fd,
                    paths=tuple(file["path"] for file in envelope["files"]),
                    max_files=lim.MAX_CI_EXPORT_FILES + 1, max_entries=lim.MAX_CI_EXPORT_ENTRIES,
                    max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + lim.MAX_CI_ENVELOPE_BYTES,
                    max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
                wanted = [{key: file[key] for key in ("path", "size", "sha256")} for file in envelope["files"]]
                check(records == wanted, "$.files", "copied target differs from exact admitted payload")
                copied.extend(records)
            check(sorted(copied, key=lambda file: file["path"]) ==
                  [{key: file[key] for key in ("path", "size", "sha256")} for file in files],
                  "$.files", "copied union differs from complete protected plan")
            write_new(stage_fd, g.CI_ENVELOPE_NAME, raw)
            check(verify_build_export(stage, plan=plan) == expected,
                  "$.envelope", "assembled export differs from complete admission")
            verify_inputs()
            return expected

        return atomic_directory(output, writer)
    except OSError as error:
        raise WorkerError("cannot assemble private complete Build export") from error


def materialize_build_export(root: Path, output: Path, *, plan: dict[str, Any]) -> dict[str, Any]:
    """Atomically create an independent protected copy of an exactly verified export.

    Caller must terminate/lock the source UID, establish readable reclaimed source and own a
    private output parent inaccessible to both disposable UIDs. This does not reclaim ownership
    or perform native second-account validation. No upload is authorized by this copy alone.
    """

    return _materialize_build_export(root, output, plan=plan, before_publish=None)


def _materialize_build_export(root: Path, output: Path, *, plan: dict[str, Any],
                               before_publish: Callable[[], None] | None) -> dict[str, Any]:
    """MB11-only final admission inside the existing private atomic copy transaction."""

    expected = verify_build_export(root, plan=plan)
    inventory = [{key: file[key] for key in ("path", "size", "sha256")} for file in expected["files"]]
    raw = canonical_json(expected)
    inventory.append({"path": g.CI_ENVELOPE_NAME, "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    inventory.sort(key=lambda file: file["path"])

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_files(root, stage_fd, max_files=lim.MAX_CI_EXPORT_FILES + 1,
                                    max_entries=lim.MAX_CI_EXPORT_ENTRIES,
                                    max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                                    max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
        check(copied == inventory, "$.files", "copied export differs from protected admission")
        observed = verify_build_export(stage, plan=plan)
        check(observed == expected, "$.envelope", "staged export differs from protected admission")
        if before_publish is not None:
            before_publish()
            observed = verify_build_export(stage, plan=plan)
            check(observed == expected, "$.envelope", "staged export changed during final admission")
        return observed

    return atomic_directory(output, writer)


def prepare_build_validation(*, boundary: HostBoundary, validator: WorkerAccount,
                             plan: dict[str, Any]) -> dict[str, Any]:
    """Protected-root-only handoff of the fixed independent Build copy to the fixed validator.

    Authenticates actual host/account/layout identities, terminates the candidate, verifies the
    copy and grants only validator-group reads. Protected source immutability, provenance and
    native second-account execution/receipt sealing remain separate required phases.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("Build read handoff requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("Build validator identity changed")
    candidate = authenticate_worker_account("candidate")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("Build handoff identities are not isolated from runner and peer")
    descriptor = None
    admitted = False
    try:
        for path in (WORKER_ROOT.parent, WORKER_ROOT):
            parent = _open_directory(tuple(path.parts[1:]))
            try:
                info = os.fstat(parent)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o711):
                    raise WorkerError("Build handoff traversal root is not protected runner-owned")
            finally:
                os.close(parent)
        descriptor = _open_directory(tuple(BUILD_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (boundary.uid, boundary.gid, 0o700):
            raise WorkerError("Build handoff copy must be fresh private runner-owned bytes")
        admitted = True
        terminate_worker(candidate)
        expected = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
        raw = canonical_json(expected)
        grant_tree_read_access(BUILD_VALIDATION_ROOT, source_owner_uid=boundary.uid,
                               owner_uid=boundary.uid, reader_gid=validator.gid,
                               max_files=lim.MAX_CI_EXPORT_FILES + 1, max_entries=lim.MAX_CI_EXPORT_ENTRIES,
                               max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                               max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, validator.gid, 0o750)):
            raise WorkerError("Build read handoff copy identity or permissions changed")
        observed = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
        if observed != expected:
            raise WorkerError("Build read handoff envelope changed")
        authenticate_privileged_host_boundary(boundary)
        return observed
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("Build handoff could not restore private copy traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot prepare protected Build read handoff") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def freeze_build_export(*, boundary: HostBoundary, candidate: WorkerAccount, execution: WorkerResult,
                         inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...],
                         plan: dict[str, Any]) -> dict[str, Any]:
    """Protected-root freeze after candidate quiescence and complete tracked-source rechecking.

    Retain genuine tested-tree inventory and execution evidence; constructed objects do not
    establish provenance. Generated roots are protected native policy, never candidate claims.
    Copy independently and transfer only the new protected copy; originals keep candidate ownership.
    Native compiler/report witnesses, overlays/cache/Git provenance and second-UID validation
    remain required before upload/gate admission.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    if type(generated_roots) is not tuple or any(not g.is_repo_path(path) for path in generated_roots):
        raise WorkerError("Build freeze generated roots require exact protected policy paths")
    if (type(execution) is not WorkerResult or type(execution.returncode) is not int or execution.returncode != 0
            or type(execution.log) is not bytes or len(execution.log) > lim.MAX_CI_LOG_BYTES
            or type(execution.truncated) is not bool):
        raise WorkerError("Build freeze requires retained successful candidate execution")
    if type(candidate) is not WorkerAccount or candidate.role != "candidate":
        raise WorkerError("Build freeze requires the fixed candidate identity")
    if authenticate_worker_account("candidate") != candidate:
        raise WorkerError("Build freeze candidate identity changed")
    validator = authenticate_worker_account("validator")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("Build freeze identities are not isolated")
    descriptor = None
    admitted = False
    try:
        terminate_worker(candidate)
        for path, owner, group, mode in ((WORKER_ROOT.parent, boundary.uid, boundary.gid, 0o711),
                                        (WORKER_ROOT, boundary.uid, boundary.gid, 0o711),
                                        (CANDIDATE_OUTPUT_ROOT.parent, candidate.uid, candidate.gid, 0o700),
                                        (CANDIDATE_SOURCE_ROOT, candidate.uid, candidate.gid, 0o700)):
            parent = _open_directory(tuple(path.parts[1:]))
            try:
                info = os.fstat(parent)
                if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (owner, group, mode):
                    raise WorkerError("Build freeze traversal/home/source identity changed")
            finally:
                os.close(parent)
        original = verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots)
        authenticate_tree_private_access(CANDIDATE_OUTPUT_ROOT, owner_uid=candidate.uid,
                                         owner_gid=candidate.gid, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
        expected = materialize_build_export(CANDIDATE_OUTPUT_ROOT, BUILD_VALIDATION_ROOT, plan=plan)
        if verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots) != original:
            raise WorkerError("tracked source changed during Build freeze")
        descriptor = _open_directory(tuple(BUILD_VALIDATION_ROOT.parts[1:]))
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (0, 0, 0o700):
            raise WorkerError("Build freeze copy is not fresh private protected-root-owned")
        admitted = True
        raw = canonical_json(expected)
        privatize_tree_copy(BUILD_VALIDATION_ROOT, source_owner_uid=0, owner_uid=boundary.uid,
                             owner_gid=boundary.gid, max_files=lim.MAX_CI_EXPORT_FILES + 1,
                             max_entries=lim.MAX_CI_EXPORT_ENTRIES,
                             max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
                             max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, boundary.gid, 0o700)):
            raise WorkerError("Build freeze copy identity or private ownership changed")
        authenticate_tree_private_access(BUILD_VALIDATION_ROOT, owner_uid=boundary.uid,
                                         owner_gid=boundary.gid, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
        observed = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
        if observed != expected:
            raise WorkerError("Build freeze envelope changed during ownership transfer")
        authenticate_privileged_host_boundary(boundary)
        return observed
    except BaseException as error:
        try:
            if descriptor is not None and admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("Build freeze could not restore private traversal") from cleanup
        if isinstance(error, OSError):
            raise WorkerError("cannot freeze protected Build export") from error
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
