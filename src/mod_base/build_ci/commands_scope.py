"""``ci scope``: the narrowed runtime scope of a packaged run attempt (``build_ci.scope``).

``scope`` runs in the packaged ``input`` job, after ``select-build``, with the job's read-only
token. Without a ``runtime_selection`` in the protected config, for any subject but a pull request
and for a changed kit pin it answers a full scope at once, without a request. Otherwise it reads
the changed paths and the base commit's full packaged evidence, runs the protected ``derive_scope``
hook as the validator with them (``lifecycle.derive_scope``) and checks what the hook selected.
A narrowed scope is written as the canonical ``mod-base.ci.scope`` record to ``ci-scope.json`` in
the state directory and output as ``scope`` (the record on one line), with ``reason=narrowed``; a
full scope outputs an empty ``scope`` and the token of ``scope.FULL_SCOPE_REASONS`` as ``reason``.

Every later job of the run receives the ``scope`` output and writes it to a file, empty for a full
scope, which ``fetch-build``, ``aggregate`` and the packaged ``seal-gate`` read with
:func:`received_scope` (``--scope FILE``): only the canonical record of the job's own plan that this
very run attempt requested is admitted. ``fetch-build`` keeps it in the lane's state, where the
lane's ``derive_runtime`` and ``verify_runtime`` find it (``lifecycle.read_scope``).
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import commands, identity, lifecycle, scope
from mod_base.model import limits
from mod_base.model.canonical import canonical_json, canonical_sha256, read_regular_file
from mod_base.model.validators import check


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``scope`` on the ``ci`` verb group ``verbs``."""

    derive = verbs.add_parser("scope", help="derive the narrowed runtime scope of this packaged run attempt")
    commands.add_job_arguments(derive)
    derive.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    derive.set_defaults(handler=run_scope)


def add_scope_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--scope FILE``: where the job wrote the scope line it received from its ``input`` job,
    an empty file for a run in full."""

    parser.add_argument("--scope", type=cli.PATH, default=None, metavar="FILE",
                        help="the scope record this run attempt's input job handed to this job (empty: full)")


def received_scope(path: Path | None, *, plan: dict[str, Any], run_id: int, run_attempt: int,
                   workflow_path: str) -> dict[str, Any] | None:
    """The scope record of ``--scope FILE``, bound to ``plan`` and this run attempt, or ``None`` for
    no file or an empty one (``scope.received_scope``). Whatever carried the bytes here is not
    trusted."""

    if path is None:
        return None
    raw = read_regular_file(path, label="scope record", max_bytes=limits.MAX_CI_SCOPE_BYTES + 1, allow_empty=True)
    return scope.received_scope(raw, plan=plan, run_id=run_id, run_attempt=run_attempt, workflow_path=workflow_path)


def run_scope(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    check(job.record["producer"] == "packaged", "$.state.producer", "`ci scope` is a step of a packaged job")
    plan = lifecycle.read_plan(job)

    def derive(value: str) -> bytes:
        return lifecycle.derive_scope(job, lifecycle.open_worker(job), plan, value, log=sys.stdout.write)

    api = commands.api_client(invocation, max_requests=limits.MAX_CI_SCOPE_REQUESTS)
    with tempfile.TemporaryDirectory(prefix="mb-ci-scope-", dir=args.state) as temporary:
        outcome = scope.derive_scope(api, plan=plan, config=job.config.data, event=job.record["event"],
                                     run_id=job.run_id, run_attempt=job.run_attempt,
                                     workflow_path=job.record["workflow_path"], temporary_root=Path(temporary),
                                     derive=derive)
    if isinstance(outcome, scope.FullScope):
        cli.write_github_output(args.github_output, {"scope": "", "reason": outcome.reason})
        sys.stdout.write(f"scope: every lane runs every obligation: {outcome.detail}\n")
        return 0
    raw = canonical_json(outcome)
    identity.write_state_record(args.state, scope.SCOPE_NAME, raw)
    cli.write_github_output(args.github_output, {"scope": raw.decode("utf-8").rstrip("\n"), "reason": "narrowed"})
    selected = sum(len(lane["scenarios"]) for lane in outcome["lanes"])
    planned = sum(len(lane["obligations"]) for lane in plan["lanes"])
    sys.stdout.write(f"scope: {selected} of {planned} obligations in {len(outcome['lanes'])} lanes, "
                     f"{outcome['changes']['paths']} changed paths against {outcome['changes']['base_sha']}; "
                     f"scope {canonical_sha256(outcome)}\n")
    return 0
