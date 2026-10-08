"""``ci assemble``: the fan-in step of the job that seals the complete Build.

It runs after ``ci subject`` and ``ci plan`` of the same job, as the runner, inside the run whose
target partitions it reads:

* ``assemble`` describes the partition of every planned target of this attempt
  (``describe.describe_attempt``), downloads them by numeric id
  (``transport.download_target_set``) and assembles their exact union into the fixed
  ``sealed-build/`` root (``exports.assemble_build_export``), where the next step runs the
  validator. The descriptors it read and the SHA-256 of the assembled envelope are recorded in
  the state directory as ``ci-partitions.json``.

A command knows its job from the state directory (``identity.json`` and ``ci-plan.json``) and its
run and attempt from ``GITHUB_RUN_ID`` and ``GITHUB_RUN_ATTEMPT``; the state must belong to the
executing repository and controller commit and the plan to the subject of the state. Target
partitions are downloaded into a temporary directory inside the state directory, which is private
to the runner, and removed again.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import commands, describe, identity, planning, transport
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, assemble_build_export
from mod_base.build_ci.reads import CommandReads
from mod_base.errors import MbError
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


def run_assemble(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = _open_job(invocation, args.state)
    check(job.record["producer"] == "build", "$.state.producer", "`ci assemble` is a step of a Build job")
    check(not os.path.lexists(BUILD_VALIDATION_ROOT), "$.output", "the sealed Build root already exists")
    # Build jobs run in a full run of the Build caller, or in a packaged run that rebuilds.
    mode = "full" if job.caller == "build" else "rebuilt"
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
                                                   run_attempt=job.run_attempt, output=inputs)
        envelope = assemble_build_export(inputs, partitions=partitions, plan=job.plan, run_id=job.run_id,
                                         run_attempt=job.run_attempt, output=BUILD_VALIDATION_ROOT)
    identity.write_state_record(args.state, PARTITIONS_NAME, canonical_json(
        {"descriptors": descriptors, "envelope_sha256": canonical_sha256(envelope)}))
    sys.stdout.write(f"assemble: {len(partitions)} target partitions of run {job.run_id} attempt {job.run_attempt} "
                     f"assembled into {len(envelope['files'])} files\n")
    return 0
