"""Independent Build export and complete target-union verification (MB11).

Call only over frozen exports after the disposable processes have been terminated and locked.
These checks establish bytes/coverage, not native compiler validity or API provenance.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import os
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.worker import (WORKER_ROOT, WorkerAccount, WorkerError, WorkerResult,
                                      authenticate_peer_account, authenticate_worker_account, terminate_worker)
from mod_base.build_ci.source import GitSourceEntry, verify_source_copy
from mod_base.build_ci.records import bind_build_envelope, validate_build_envelope, validate_descriptor
from mod_base.io.secure_json import loads
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import (EXPORT_PATHS, authenticate_tree_private_access, copy_regular_files, copy_selected_regular_files,
                              file_records, grant_tree_read_access, privatize_tree_copy, read_child_file, validate_tree_entries)
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Int, List, Obj, check, fail


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

    Authenticates actual host/account/layout identities, terminates the candidate when the job
    has one (the assembling job allocates the validator alone), verifies the copy and grants only
    validator-group reads. Protected source immutability, provenance and native second-account
    execution/receipt sealing remain separate required phases.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    if type(validator) is not WorkerAccount or validator.role != "validator":
        raise WorkerError("Build read handoff requires the fixed validator identity")
    if authenticate_worker_account("validator") != validator:
        raise WorkerError("Build validator identity changed")
    candidate = authenticate_peer_account(validator, runner_uid=boundary.uid, runner_gid=boundary.gid)
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
        if candidate is not None:
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


def target_envelope(records: list[dict[str, Any]], *, plan: dict[str, Any], target_id: str,
                    producer: dict[str, Any]) -> dict[str, Any]:
    """The envelope protected code writes for one target partition, from the plan and the bytes found.

    ``records`` is the exact inventory ``[{path, sha256, size}]`` of what the target's hook left.
    It must hold exactly the outputs the plan lists for the target: a missing and an extra file
    are rejections. Every file takes its lane and role from the plan and its size and hash from
    the bytes; ``producer`` is the identity of the run attempt that is executing. No byte of a
    mod decides a field. The result is a valid ``mod-base.build.envelope`` of scope ``target``.
    """

    validate_plan(plan)
    targets = [target for target in plan["targets"] if target["id"] == target_id]
    check(len(targets) == 1, "$.target_id", "is not a target of the protected plan")
    planned = {output["path"]: output for output in targets[0]["outputs"]}
    found = {record["path"]: record for record in records}
    missing, extra = sorted(set(planned) - set(found)), sorted(set(found) - set(planned))
    if missing or extra:
        detail = [f"{len(paths)} {label} (first {paths[0]!r})"
                  for paths, label in ((missing, "planned and missing"), (extra, "not planned")) if paths]
        raise fail("$.export", f"is not the planned output set of target {target_id}: {', '.join(detail)}"[:600])
    files = [{**planned[path], "size": found[path]["size"], "sha256": found[path]["sha256"]} for path in sorted(planned)]
    envelope = {"kind": "mod-base.build.envelope", "schema_version": SCHEMA_VERSIONS["mod-base.build.envelope"],
                "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                "profile": plan["profile"], "producer": copy.deepcopy(producer), "scope": "target",
                "target_id": target_id, "files": files,
                "native_reports": [file["path"] for file in files if file["role"] == "native-report"]}
    validate_build_envelope(envelope, plan=plan)
    check(len(canonical_json(envelope)) <= lim.MAX_CI_ENVELOPE_BYTES, "$.envelope", "target envelope exceeds its byte cap")
    return envelope


def seal_target_export(root: Path, output: Path, *, plan: dict[str, Any], target_id: str,
                       producer: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish an independent copy of one target's export with its protected envelope.

    ``root`` holds what the target's hook left and no kit document: every entry must be a
    directory or a non-empty single-link regular file under an export path, and the files must be
    exactly the planned outputs (:func:`target_envelope`). ``output`` is created with copies of
    them and the ``ci-envelope.json`` this function writes; :func:`verify_build_export` accepts it.
    Caller terminates the account that wrote ``root`` and owns a private output parent.
    """

    bounds = dict(max_files=lim.MAX_CI_EXPORT_FILES, max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES,
                  max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
    validate_tree_entries(root, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
    records = file_records(root, **bounds)
    envelope = target_envelope(records, plan=plan, target_id=target_id, producer=producer)
    raw = canonical_json(envelope)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_files(root, stage_fd, max_entries=lim.MAX_CI_EXPORT_ENTRIES, **bounds)
        check(copied == records, "$.files", "export changed while it was copied")
        write_new(stage_fd, g.CI_ENVELOPE_NAME, raw)
        check(verify_build_export(stage, plan=plan) == envelope, "$.envelope",
              "sealed export differs from its protected envelope")
        return envelope

    return atomic_directory(output, writer)


def _isolated_candidate(boundary: HostBoundary, candidate: WorkerAccount) -> WorkerAccount:
    """Bind the fixed candidate to the live account, apart from the validator and the runner;
    return the validator."""

    if type(candidate) is not WorkerAccount or candidate.role != "candidate":
        raise WorkerError("candidate freeze requires the fixed candidate identity")
    if authenticate_worker_account("candidate") != candidate:
        raise WorkerError("candidate freeze identity changed")
    validator = authenticate_worker_account("validator")
    if (candidate.uid == validator.uid or candidate.gid == validator.gid
            or any(account.uid == boundary.uid or account.gid == boundary.gid for account in (candidate, validator))):
        raise WorkerError("candidate freeze identities are not isolated")
    return validator


def _candidate_layout(boundary: HostBoundary, candidate: WorkerAccount) -> None:
    """The traversal roots are the runner's, the home and the checkout the candidate's own."""

    for path, owner, group, mode in ((WORKER_ROOT.parent, boundary.uid, boundary.gid, 0o711),
                                    (WORKER_ROOT, boundary.uid, boundary.gid, 0o711),
                                    (CANDIDATE_OUTPUT_ROOT.parent, candidate.uid, candidate.gid, 0o700),
                                    (CANDIDATE_SOURCE_ROOT, candidate.uid, candidate.gid, 0o700)):
        descriptor = _open_directory(tuple(path.parts[1:]))
        try:
            info = os.fstat(descriptor)
            if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (owner, group, mode):
                raise WorkerError("candidate traversal, home or checkout identity changed")
        finally:
            os.close(descriptor)


def _generated_roots(generated_roots: Any) -> None:
    if type(generated_roots) is not tuple or any(not g.is_repo_path(path) for path in generated_roots):
        raise WorkerError("candidate source check requires exact protected generated-root paths")


def verify_candidate_source(*, boundary: HostBoundary, candidate: WorkerAccount,
                            inventory: tuple[GitSourceEntry, ...],
                            generated_roots: tuple[str, ...]) -> list[dict[str, str | int]]:
    """Root-only: prove that the quiescent candidate's checkout still holds exactly the tested tree.

    The candidate is terminated first. Every tracked path must have the bytes and the mode of
    ``inventory`` and nothing undeclared may exist outside ``generated_roots``, which are protected
    policy (``root_request_operations.candidate_generated_roots``). Nothing is copied or changed.
    Returns the records.
    """

    authenticate_privileged_host_boundary(boundary)
    _generated_roots(generated_roots)
    _isolated_candidate(boundary, candidate)
    try:
        terminate_worker(candidate)
        _candidate_layout(boundary, candidate)
        records = verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots)
        authenticate_privileged_host_boundary(boundary)
        return records
    except OSError as error:
        raise WorkerError("cannot verify the candidate's tracked sources") from error


def inspect_complete_build(*, boundary: HostBoundary, plan: dict[str, Any]) -> tuple[dict[str, Any], tuple[int, int]]:
    """The complete Build a lane job holds in ``sealed-build/``: its envelope and its root's identity.

    The root must be the runner's own private directory, which is how ``ci fetch-build`` publishes
    it: no worker account can enter it yet. Every byte is checked against the envelope. For the
    runner and for root alike; each authenticates its own role first.
    """

    descriptor = _open_directory(tuple(BUILD_VALIDATION_ROOT.parts[1:]))
    try:
        info = os.fstat(descriptor)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (boundary.uid, boundary.gid, 0o700):
            raise WorkerError("the Build of this lane is not the runner's private copy")
    finally:
        os.close(descriptor)
    envelope = verify_build_export(BUILD_VALIDATION_ROOT, plan=plan)
    check(envelope["scope"] == "complete", "$.build", "a lane runs against the complete Build")
    return envelope, (info.st_dev, info.st_ino)


def _candidate_directory(parent: int, name: str, candidate: WorkerAccount) -> int:
    """Open ``name`` below a directory of the quiescent candidate's checkout, creating it for the
    candidate when the tested tree has none."""

    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        created = True
    except FileExistsError:
        created = False  # The tested tree tracks files in it: the source phase published it.
    child = _open_directory((name,), root=parent)
    try:
        if created:
            os.fchown(child, candidate.uid, candidate.gid)
            os.fchmod(child, 0o700)
            os.fsync(child)
        info = os.fstat(child)
        if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (candidate.uid, candidate.gid, 0o700):
            raise WorkerError("a parent of the bundle directory is not the candidate's private directory")
        return child
    except BaseException:
        os.close(child)
        raise


def stage_build_bundle(*, boundary: HostBoundary, candidate: WorkerAccount, plan: dict[str, Any],
                       envelope: dict[str, Any], path: str) -> None:
    """Root-only: give the candidate its own copy of the complete Build at ``repository/<path>``.

    ``path`` is the protected config's ``bundle.path``; ``envelope`` is what the runner read in
    ``sealed-build/``, which must still hold exactly that complete Build. The candidate is
    terminated first and has never run. The new directory holds the files the envelope lists,
    byte for byte under the mod's own names, and no kit document; it and every parent created for
    it belong to the candidate (0700 directories, 0600 files, their own inodes). An existing
    ``repository/<path>`` is never replaced: a tested tree that tracks the directory is refused.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_build_envelope(envelope, plan=plan)
    if not g.is_repo_path(path):
        raise WorkerError("bundle staging requires the protected bundle directory")
    _isolated_candidate(boundary, candidate)
    bounds = dict(max_files=lim.MAX_CI_EXPORT_FILES + 1, max_entries=lim.MAX_CI_EXPORT_ENTRIES,
                  max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + lim.MAX_CI_ENVELOPE_BYTES,
                  max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS)
    expected = [{key: file[key] for key in ("path", "sha256", "size")} for file in envelope["files"]]
    parts = path.split("/")
    try:
        terminate_worker(candidate)
        _candidate_layout(boundary, candidate)
        build = inspect_complete_build(boundary=boundary, plan=plan)
        check(build[0] == envelope, "$.envelope", "the sealed Build is not the one the runner read")
        parent = _open_directory(tuple(CANDIDATE_SOURCE_ROOT.parts[1:]))
        try:
            for part in parts[:-1]:
                child = _candidate_directory(parent, part, candidate)
                os.close(parent)
                parent = child
        finally:
            os.close(parent)
        destination = Path(str(CANDIDATE_SOURCE_ROOT)).joinpath(*parts)

        def writer(stage: Path, stage_fd: int) -> None:
            copied = copy_selected_regular_files(BUILD_VALIDATION_ROOT, stage_fd,
                                                 paths=tuple(file["path"] for file in expected), **bounds)
            check(copied == expected, "$.files", "the Build changed while it was copied")
            check(privatize_tree_copy(stage, source_owner_uid=0, owner_uid=candidate.uid, owner_gid=candidate.gid,
                                      **bounds) == expected, "$.files", "the staged Build changed during its handover")

        atomic_directory(destination, writer)
        authenticate_tree_private_access(destination, owner_uid=candidate.uid, owner_gid=candidate.gid,
                                         max_entries=lim.MAX_CI_EXPORT_ENTRIES)
        check(inspect_complete_build(boundary=boundary, plan=plan) == build, "$.build",
              "the sealed Build changed while it was staged")
        authenticate_privileged_host_boundary(boundary)
    except OSError as error:
        raise WorkerError("cannot stage the Build for the candidate") from error
    finally:
        terminate_worker(candidate)


def _hand_to_runner(root: PurePosixPath, boundary: HostBoundary, transfer: Callable[[], object]) -> None:
    """Give root's fresh private copy at ``root`` to the runner (``transfer``), bound to one inode.

    A failure after the copy was admitted leaves its root closed to everyone but its owner.
    """

    descriptor = _open_directory(tuple(root.parts[1:]))
    admitted = False
    try:
        initial = os.fstat(descriptor)
        if (initial.st_uid, initial.st_gid, stat.S_IMODE(initial.st_mode)) != (0, 0, 0o700):
            raise WorkerError("frozen copy is not fresh private protected-root-owned")
        admitted = True
        transfer()
        final = os.fstat(descriptor)
        if ((final.st_dev, final.st_ino) != (initial.st_dev, initial.st_ino)
                or (final.st_uid, final.st_gid, stat.S_IMODE(final.st_mode)) != (boundary.uid, boundary.gid, 0o700)):
            raise WorkerError("frozen copy identity or private ownership changed")
        named = _open_directory(tuple(root.parts[1:]))
        try:
            info = os.fstat(named)
            if (info.st_dev, info.st_ino) != (initial.st_dev, initial.st_ino):
                raise WorkerError("frozen copy was replaced during its ownership transfer")
        finally:
            os.close(named)
    except BaseException:
        try:
            if admitted:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
        except OSError as cleanup:
            raise WorkerError("frozen copy could not keep its private traversal") from cleanup
        raise
    finally:
        os.close(descriptor)


def freeze_build_export(*, boundary: HostBoundary, candidate: WorkerAccount,
                         inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...],
                         plan: dict[str, Any], target_id: str, producer: dict[str, Any]) -> dict[str, Any]:
    """Protected-root freeze of one target after candidate quiescence and tracked-source rechecking.

    The candidate is terminated; its checkout must still hold exactly the tested tree
    (:func:`verify_candidate_source`) before and after the copy; its ``export/`` must be private
    regular files (0700 directories, 0600 single-link files, no ACL) and exactly the planned
    outputs of ``target_id``. Root copies them to the fixed ``sealed-build/`` with the envelope it
    builds itself (:func:`seal_target_export`) and hands only that copy to the runner; the
    originals keep the candidate's ownership. The inventory and ``producer`` come from the runner,
    which authenticated the tested tree and knows the executing run; generated roots are protected
    policy. Native validity and second-UID validation remain required before upload.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    _generated_roots(generated_roots)
    _isolated_candidate(boundary, candidate)
    try:
        original = verify_candidate_source(boundary=boundary, candidate=candidate, inventory=inventory,
                                           generated_roots=generated_roots)
        authenticate_tree_private_access(CANDIDATE_OUTPUT_ROOT, owner_uid=candidate.uid,
                                         owner_gid=candidate.gid, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
        expected = seal_target_export(CANDIDATE_OUTPUT_ROOT, BUILD_VALIDATION_ROOT, plan=plan,
                                      target_id=target_id, producer=producer)
        if verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots) != original:
            raise WorkerError("tracked source changed during Build freeze")
        raw = canonical_json(expected)
        _hand_to_runner(BUILD_VALIDATION_ROOT, boundary, lambda: privatize_tree_copy(
            BUILD_VALIDATION_ROOT, source_owner_uid=0, owner_uid=boundary.uid, owner_gid=boundary.gid,
            max_files=lim.MAX_CI_EXPORT_FILES + 1, max_entries=lim.MAX_CI_EXPORT_ENTRIES,
            max_total_bytes=lim.MAX_CI_EXPORT_TREE_BYTES + len(raw),
            max_file_bytes=lim.MAX_CI_EXPORT_FILE_BYTES, rule=EXPORT_PATHS))
        authenticate_tree_private_access(BUILD_VALIDATION_ROOT, owner_uid=boundary.uid,
                                         owner_gid=boundary.gid, max_entries=lim.MAX_CI_EXPORT_ENTRIES)
        if verify_build_export(BUILD_VALIDATION_ROOT, plan=plan) != expected:
            raise WorkerError("Build freeze envelope changed during ownership transfer")
        authenticate_privileged_host_boundary(boundary)
        return expected
    except OSError as error:
        raise WorkerError("cannot freeze protected Build export") from error
    finally:
        terminate_worker(candidate)
