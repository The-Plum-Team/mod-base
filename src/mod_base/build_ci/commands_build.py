"""``ci assemble``, ``ci aggregate`` and ``ci seal-gate``: the fan-in and gate steps of a run.

Each runs after ``ci subject`` and ``ci plan`` of the same job, as the runner, inside the run
whose artifacts it reads:

* ``assemble`` describes the partition of every planned target of this attempt
  (``describe.describe_attempt``), downloads them by numeric id and verifies each with the
  validation record its job uploaded (``transport.download_target_set``), and assembles their
  exact union into the fixed
  ``sealed-build/`` root (``exports.assemble_build_export``), where the next step runs the
  validator. The descriptors it read and the SHA-256 of the assembled envelope are recorded in
  the state directory as ``ci-partitions.json``.
* ``aggregate --output DIR`` authenticates the results of every planned lane of this attempt
  against the Build of the state's selection record (``ci-selection.json``, written by
  ``ci select-build``) and writes the results index as the one file ``ci-results.json`` of the
  new directory ``DIR``, which the next step uploads (``gate.seal_results``).
* ``seal-gate --gate build|packaged --output DIR`` authenticates this attempt in the mode it
  shows (``gate.authenticate_attempt``) and writes the gate receipt as the one file
  ``ci-gate.json`` of the new directory ``DIR``, which the next step uploads as the tested
  record. In a reuse run it seals the reuse reference instead (``gate.seal_reuse``).

A command knows its job from the state directory (``identity.json`` and ``ci-plan.json``) and its
run and attempt from ``GITHUB_RUN_ID`` and ``GITHUB_RUN_ATTEMPT``; the state must belong to the
executing repository and controller commit and the plan to the subject of the state. Downloads
go into a temporary directory inside the state directory, which is private to the runner, and
are removed again. A record is written last, into a directory the command creates.
"""

from __future__ import annotations

import argparse
import functools
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import commands, describe, gate, identity, planning, transport
from mod_base.build_ci.config import load_build_config
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, assemble_build_export
from mod_base.build_ci.reads import CommandReads
from mod_base.build_ci.records import GATE_MODES
from mod_base.errors import MbError
from mod_base.io.atomic_directory import atomic_directory, write_new
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from mod_base.model.validators import check
from mod_base.workflow import ci_producer

#: The state record ``ci assemble`` leaves: the partition descriptors and the assembled envelope's hash.
PARTITIONS_NAME = "ci-partitions.json"


@dataclass(frozen=True)
class _Job:
    """What a fan-in command knows of its job: the identity record and the plan of the state
    directory, and the run attempt that is executing."""

    record: dict[str, Any]
    plan: dict[str, Any]
    run_id: int
    run_attempt: int

    @property
    def caller(self) -> str:
        """The managed caller this run executes: ``build`` or ``packaged``."""

        return ci_producer(self.record["workflow_path"])


def _run_number(invocation: runtime.Invocation, name: str, maximum: int) -> int:
    value = invocation.environ.get(name, "")
    if not grammar.POSITIVE_DECIMAL.fullmatch(value) or int(value) > maximum:
        raise MbError(f"{name} must be a positive decimal", reason="environment")
    return int(value)


def _open_job(invocation: runtime.Invocation, state: Path) -> _Job:
    record = identity.read_subject(state)
    subject = record["subject"]
    check(invocation.repository == subject["repository"] and invocation.implementation_sha == subject["controller_sha"],
          "$.state", "the state directory belongs to another repository or controller commit")
    raw = identity.read_state_record(state, grammar.CI_PLAN_NAME, max_bytes=limits.MAX_CI_PLAN_BYTES)
    plan = load_document(raw, kind="mod-base.build.plan")
    check(raw == canonical_json(plan), "$.state", f"{grammar.CI_PLAN_NAME} is not canonical JSON")
    planning.require_plan(plan, subject=subject)
    return _Job(record, plan, _run_number(invocation, "GITHUB_RUN_ID", limits.MAX_RUN_ID),
                _run_number(invocation, "GITHUB_RUN_ATTEMPT", limits.MAX_RUN_ATTEMPT))


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    assemble = verbs.add_parser("assemble", help="assemble this attempt's target partitions into the sealed Build")
    commands.add_job_arguments(assemble)
    assemble.set_defaults(handler=run_assemble)

    aggregate = verbs.add_parser("aggregate", help="authenticate this attempt's lane results and write their index")
    commands.add_job_arguments(aggregate)
    aggregate.add_argument("--output", type=cli.PATH, required=True, metavar="DIR",
                           help="the new directory that receives the one record to upload")
    aggregate.set_defaults(handler=run_aggregate)

    seal = verbs.add_parser("seal-gate", help="authenticate this attempt and write its gate receipt")
    commands.add_job_arguments(seal)
    seal.add_argument("--gate", choices=tuple(GATE_MODES), required=True, help="the gate this job seals")
    seal.add_argument("--output", type=cli.PATH, required=True, metavar="DIR",
                      help="the new directory that receives the one record to upload")
    seal.set_defaults(handler=run_seal_gate)


def run_assemble(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = _open_job(invocation, args.state)
    check(job.record["producer"] == "build", "$.state.producer", "`ci assemble` is a step of a Build job")
    check(not os.path.lexists(BUILD_VALIDATION_ROOT), "$.output", "the sealed Build root already exists")
    # Build jobs run in a full run of the Build caller, or in a packaged run that rebuilds.
    mode = "full" if job.caller == "build" else "rebuilt"
    config_sha256 = _config_sha256(invocation, job)
    reads = CommandReads.of(commands.api_client(invocation, max_requests=limits.MAX_CI_ASSEMBLE_REQUESTS))
    producer = describe.attempt_producer(job.record, job.plan, mode=mode, run_id=job.run_id,
                                         run_attempt=job.run_attempt)
    descriptors = describe.describe_attempt(
        reads, producer=producer, plan=job.plan, mode=mode,
        expected=[("target", target["id"]) for target in job.plan["targets"]],
        finished=describe.settled_jobs(job.caller, mode, job.plan, "build"))
    with tempfile.TemporaryDirectory(prefix="mb-ci-assemble-", dir=args.state) as temporary:
        inputs = Path(temporary) / "targets"
        partitions = transport.download_target_set(reads, descriptors=descriptors, plan=job.plan, run_id=job.run_id,
                                                   run_attempt=job.run_attempt, output=inputs,
                                                   source_config_sha256=config_sha256)
        envelope = assemble_build_export(inputs, partitions=partitions, plan=job.plan, run_id=job.run_id,
                                         run_attempt=job.run_attempt, output=BUILD_VALIDATION_ROOT)
    identity.write_state_record(args.state, PARTITIONS_NAME, canonical_json(
        {"descriptors": descriptors, "envelope_sha256": canonical_sha256(envelope)}))
    sys.stdout.write(f"assemble: {len(partitions)} target partitions of run {job.run_id} attempt {job.run_attempt} "
                     f"assembled into {len(envelope['files'])} files\n")
    return 0


def _config_sha256(invocation: runtime.Invocation, job: _Job) -> str:
    """The digest of the protected Build config of the mod checkout the prologue verified."""

    return load_build_config(invocation.repo_root, repository=job.record["subject"]["repository"]).sha256


def _write_record(output: Path, name: str, document: dict[str, Any]) -> None:
    """Create the upload directory ``output`` with ``document`` as its one canonical file."""

    atomic_directory(output, lambda stage, stage_fd: write_new(stage_fd, name, canonical_json(document)))


def run_aggregate(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = _open_job(invocation, args.state)
    check(job.record["producer"] == "packaged", "$.state.producer", "`ci aggregate` is a step of a packaged job")
    check(not os.path.lexists(args.output), "$.output", "the upload directory already exists")
    raw = identity.read_state_record(args.state, grammar.CI_SELECTION_NAME, max_bytes=limits.MAX_CI_RECORD_BYTES)
    selection = load_document(raw, kind="mod-base.ci.selection", plan=job.plan)
    check(raw == canonical_json(selection), "$.state", f"{grammar.CI_SELECTION_NAME} is not canonical JSON")
    config_sha256 = _config_sha256(invocation, job)
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_AGGREGATE_REQUESTS)
    with tempfile.TemporaryDirectory(prefix="mb-ci-aggregate-", dir=args.state) as temporary:
        name, document = gate.seal_results(api, record=job.record, plan=job.plan, selection=selection,
                                           run_id=job.run_id, run_attempt=job.run_attempt,
                                           config_sha256=config_sha256, temporary_root=Path(temporary))
    _write_record(args.output, name, document)
    sys.stdout.write(f"aggregate: {len(document['lanes'])} lane results of run {job.run_id} attempt "
                     f"{job.run_attempt} indexed as {name}\n")
    return 0


def run_seal_gate(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = _open_job(invocation, args.state)
    check(not os.path.lexists(args.output), "$.output", "the upload directory already exists")
    seal = functools.partial(gate.seal_gate, config_sha256=_config_sha256(invocation, job))
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_GATE_REQUESTS)
    attempt = gate.authenticate_attempt(api, record=job.record, plan=job.plan, gate=args.gate, run_id=job.run_id,
                                        run_attempt=job.run_attempt)
    with tempfile.TemporaryDirectory(prefix="mb-ci-gate-", dir=args.state) as temporary:
        name, document = (gate.seal_reuse if attempt.mode == "reuse" else seal)(attempt, temporary_root=Path(temporary))
    _write_record(args.output, name, document)
    sys.stdout.write(f"seal-gate: {args.gate} gate of run {job.run_id} attempt {job.run_attempt} sealed in mode "
                     f"{attempt.mode} as {name}\n")
    return 0
