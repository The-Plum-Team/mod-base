"""Root-only freeze of one lane's native results with the envelope protected code writes (MB11).

A lane's hook leaves its native results below ``candidate-home/export/`` and no kit document. The
plan names the lane and its native contract but not its files, so the envelope is built from what
is found: every file belongs to the lane, and its role follows from its name alone
(:func:`runtime_role`). A role only selects a size bound; what a result means is for the protected
``verify_runtime`` hook to decide.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.exports import (CANDIDATE_OUTPUT_ROOT, CANDIDATE_SOURCE_ROOT, _generated_roots,
                                       _hand_to_runner, _isolated_candidate, inspect_complete_build,
                                       verify_candidate_source)
from mod_base.build_ci.host import HostBoundary, authenticate_privileged_host_boundary
from mod_base.build_ci.protocol import validate_plan
from mod_base.build_ci.records import bind_build_envelope
from mod_base.build_ci.runtime_exports import _bounds, verify_runtime_export
from mod_base.build_ci.runtime_inputs import RUNTIME_VALIDATION_ROOT
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.build_ci.source import GitSourceEntry, verify_source_copy
from mod_base.build_ci.worker import WorkerAccount, WorkerError, terminate_worker
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.io.tree import (EXPORT_PATHS, authenticate_tree_private_access, copy_regular_data_files,
                              privatize_regular_data_copy, regular_data_records, validate_tree_entries)
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check

#: The directory name a game writes its crash reports under.
CRASH_DIRECTORY = "crash-reports"


def runtime_role(path: str) -> str:
    """The role of one runtime result, decided by its export path alone.

    A file below a ``crash-reports`` directory is a ``crash-report``, a ``.png`` a ``screenshot``
    and a ``.json`` a ``native-report``; every other file is a ``runtime-log``. The role bounds
    the file (``runtime_schema.validate_runtime_envelope``): a report and a screenshot may not be
    empty, and a file of no known kind is held to the log bound.
    """

    *parents, name = path.split("/")
    if CRASH_DIRECTORY in parents:
        return "crash-report"
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    return {"png": "screenshot", "json": "native-report"}.get(suffix, "runtime-log")


def lane_envelope(records: list[dict[str, Any]], *, plan: dict[str, Any], lane_id: str,
                  producer: dict[str, Any], owning_build: dict[str, Any]) -> dict[str, Any]:
    """The runtime envelope protected code writes for one lane, from the plan and the bytes found.

    ``records`` is the exact inventory ``[{path, sha256, size}]`` of what the lane's hook left; at
    least one file is required. The lane and its native contract come from the plan, the role of
    each file from its path (:func:`runtime_role`), sizes and hashes from the bytes, ``producer``
    from the executing run attempt and ``owning_build`` from the selection of the Build the lane
    ran against. The result is a valid ``mod-base.ci.runtime-envelope`` of scope ``lane``.
    """

    validate_plan(plan)
    lanes = [lane for lane in plan["lanes"] if lane["id"] == lane_id]
    check(len(lanes) == 1, "$.lane_id", "is not a lane of the protected plan")
    files = [{"path": record["path"], "lane_id": lane_id, "role": runtime_role(record["path"]),
              "size": record["size"], "sha256": record["sha256"]}
             for record in sorted(records, key=lambda record: record["path"])]
    envelope = {"kind": "mod-base.ci.runtime-envelope",
                "schema_version": SCHEMA_VERSIONS["mod-base.ci.runtime-envelope"],
                "identity": copy.deepcopy(plan["identity"]), "plan_sha256": plan["plan_sha256"],
                "profile": plan["profile"], "producer": copy.deepcopy(producer), "scope": "lane",
                "lane_id": lane_id, "owning_build": copy.deepcopy(owning_build),
                "lanes": [{"id": lane_id, "native_contract_sha256": lanes[0]["native_contract_sha256"]}],
                "files": files}
    validate_runtime_envelope(envelope, plan=plan)
    check(len(canonical_json(envelope)) <= limits.MAX_CI_RUNTIME_ENVELOPE_BYTES, "$.envelope",
          "runtime envelope exceeds its byte cap")
    return envelope


def seal_lane_export(root: Path, output: Path, *, plan: dict[str, Any], lane_id: str,
                     producer: dict[str, Any], owning_build: dict[str, Any]) -> dict[str, Any]:
    """Atomically publish an independent copy of one lane's results with its protected envelope.

    ``root`` holds what the lane's hook left and no kit document: directories and single-link
    regular files under export paths, within the caps of one lane. ``output`` is created with
    copies of them and the ``ci-runtime-envelope.json`` this function writes
    (:func:`lane_envelope`); ``runtime_exports.verify_runtime_export`` accepts it. Caller
    terminates the account that wrote ``root`` and owns a private output parent.
    """

    bounds = dict(max_files=limits.MAX_CI_RUNTIME_FILES, max_entries=limits.MAX_CI_RUNTIME_ENTRIES,
                  max_total_bytes=limits.MAX_CI_RUNTIME_BYTES, max_file_bytes=limits.MAX_CI_PNG_BYTES,
                  rule=EXPORT_PATHS)
    validate_tree_entries(root, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
    records = regular_data_records(root, **bounds)
    envelope = lane_envelope(records, plan=plan, lane_id=lane_id, producer=producer, owning_build=owning_build)
    raw = canonical_json(envelope)

    def writer(stage: Path, stage_fd: int) -> dict[str, Any]:
        copied = copy_regular_data_files(root, stage_fd, **bounds)
        check(copied == records, "$.files", "runtime results changed while they were copied")
        write_new(stage_fd, grammar.CI_RUNTIME_ENVELOPE_NAME, raw)
        check(verify_runtime_export(stage, plan=plan) == envelope, "$.envelope",
              "sealed runtime results differ from their protected envelope")
        return envelope

    return atomic_directory(output, writer)


def freeze_runtime_export(*, boundary: HostBoundary, candidate: WorkerAccount,
                          inventory: tuple[GitSourceEntry, ...], generated_roots: tuple[str, ...],
                          plan: dict[str, Any], build: dict[str, Any], owning_build: dict[str, Any],
                          lane_id: str, producer: dict[str, Any]) -> dict[str, Any]:
    """Protected-root freeze of one lane after candidate quiescence and tracked-source rechecking.

    The candidate is terminated; its checkout must still hold exactly the tested tree
    (``exports.verify_candidate_source``) before and after the copy; ``sealed-build/`` must still
    be the runner's private complete Build ``build``, the one the selection ``owning_build``
    names; the candidate's ``export/`` must be private regular files (0700 directories, 0600
    single-link files, no ACL). Root copies them to the fixed ``sealed-runtime/`` with the envelope
    it builds itself (:func:`seal_lane_export`) and hands only that copy to the runner. The
    inventory, ``producer`` and ``owning_build`` come from the runner; generated roots are
    protected policy. Native verification by the second UID remains required before upload. A
    failed closing check may leave a private copy: never consume it.
    """

    authenticate_privileged_host_boundary(boundary)
    validate_plan(plan)
    _generated_roots(generated_roots)
    bind_build_envelope(build, descriptor=owning_build, plan=plan)
    _isolated_candidate(boundary, candidate)
    try:
        original = verify_candidate_source(boundary=boundary, candidate=candidate, inventory=inventory,
                                           generated_roots=generated_roots)
        selected = inspect_complete_build(boundary=boundary, plan=plan)
        check(selected[0] == build, "$.build", "the sealed Build is not the selected Build of this lane")
        authenticate_tree_private_access(CANDIDATE_OUTPUT_ROOT, owner_uid=candidate.uid,
                                         owner_gid=candidate.gid, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
        expected = seal_lane_export(CANDIDATE_OUTPUT_ROOT, RUNTIME_VALIDATION_ROOT, plan=plan,
                                    lane_id=lane_id, producer=producer, owning_build=owning_build)
        if (verify_source_copy(CANDIDATE_SOURCE_ROOT, inventory=inventory, generated_roots=generated_roots) != original
                or inspect_complete_build(boundary=boundary, plan=plan) != selected):
            raise WorkerError("tracked source or the selected Build changed during runtime freeze")
        _hand_to_runner(RUNTIME_VALIDATION_ROOT, boundary, lambda: privatize_regular_data_copy(
            RUNTIME_VALIDATION_ROOT, source_owner_uid=0, owner_uid=boundary.uid, owner_gid=boundary.gid,
            **_bounds(expected, len(canonical_json(expected)))))
        authenticate_tree_private_access(RUNTIME_VALIDATION_ROOT, owner_uid=boundary.uid,
                                         owner_gid=boundary.gid, max_entries=limits.MAX_CI_RUNTIME_ENTRIES)
        if verify_runtime_export(RUNTIME_VALIDATION_ROOT, plan=plan) != expected:
            raise WorkerError("runtime freeze envelope changed during ownership transfer")
        authenticate_privileged_host_boundary(boundary)
        return expected
    except OSError as error:
        raise WorkerError("cannot freeze protected runtime export") from error
    finally:
        terminate_worker(candidate)
