"""``ci select-build``: the exact Build a packaged run consumes.

``select-build`` runs in the packaged ``input`` job and in the one job of ``select-build.yml``.
The subject ``ci subject`` authenticated and ``--build-run-id`` choose the route
(:func:`mod_base.build_ci.selection.select_build`):

* a pull request (``--build-run-id`` empty or absent) waits at most ``--wait-seconds`` (5400, the
  default and the maximum) for the newest Build run of its head;
* a protected push or dispatch without ``--build-run-id`` takes the newest Build run of its
  commit, or finds none and says so, on which its caller builds in the same run;
* ``--build-run-id ID`` requires exactly that run to be the newest Build run, and
  ``--build-run-id same-run`` authenticates the Build this run built for itself.

A selection writes the canonical ``mod-base.ci.selection`` record to ``--output`` (a new file) and
to ``ci-selection.json`` in the state directory, and outputs ``found=true``, ``build_run_id`` and
``selection`` (the record on one line, for the jobs that consume it). Nothing to select writes no
record and outputs ``found=false`` with an empty ``build_run_id``.

The command reads the job's subject and plan from the state directory and writes nothing to GitHub.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import commands, identity, planning, selection
from mod_base.build_ci.protocol import validate_plan
from mod_base.errors import MbError
from mod_base.io.secure_json import loads
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json


def _build_run(value: str) -> int | str | None:
    if value in ("", selection.SAME_RUN):
        return value or None
    if not grammar.POSITIVE_DECIMAL.fullmatch(value):
        raise ValueError(value)
    return grammar.require_positive_int(int(value), "Build run id")


def _wait_seconds(value: str) -> int:
    if not grammar.POSITIVE_DECIMAL.fullmatch(value) or int(value) > limits.CI_BUILD_WAIT_SECONDS:
        raise ValueError(value)
    return int(value)


BUILD_RUN = cli.typed(_build_run, "Build run id")
WAIT_SECONDS = cli.typed(_wait_seconds, f"wait of 1 to {limits.CI_BUILD_WAIT_SECONDS} seconds")


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``select-build`` on the ``ci`` verb group ``verbs``."""

    select = verbs.add_parser("select-build", help="find and authenticate the exact Build of this packaged run")
    commands.add_job_arguments(select)
    select.add_argument("--wait-seconds", type=WAIT_SECONDS, default=limits.CI_BUILD_WAIT_SECONDS, metavar="N",
                        help="how long a pull request waits for its Build run (default and maximum: 5400)")
    select.add_argument("--build-run-id", type=BUILD_RUN, default=None, metavar="ID",
                        help="a protected subject's selected Build run, or same-run for the Build this run built; "
                             "empty for a pull request")
    select.add_argument("--output", type=cli.PATH, required=True, metavar="FILE",
                        help="where the selection record is written (a new file)")
    select.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    select.set_defaults(handler=run_select_build)


def job_plan(state: Path) -> dict[str, Any]:
    """The plan ``ci plan`` left in the job's state directory, strictly decoded and validated."""

    raw = identity.read_state_record(state, grammar.CI_PLAN_NAME, max_bytes=limits.MAX_CI_PLAN_BYTES)
    return validate_plan(loads(raw, label=grammar.CI_PLAN_NAME, max_bytes=limits.MAX_CI_PLAN_BYTES))


def _job(args: argparse.Namespace) -> tuple[runtime.Invocation, dict[str, Any], dict[str, Any], int, int]:
    """The invocation, the identity record and the plan of this job, with the run and attempt
    GitHub says it belongs to."""

    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    record = identity.read_subject(args.state)
    plan = planning.require_plan(job_plan(args.state), subject=record["subject"])
    run = []
    for name, maximum in (("GITHUB_RUN_ID", limits.MAX_RUN_ID), ("GITHUB_RUN_ATTEMPT", limits.MAX_RUN_ATTEMPT)):
        text = invocation.environ.get(name, "")
        if not grammar.POSITIVE_DECIMAL.fullmatch(text) or int(text) > maximum:
            raise MbError(f"{name} must be a positive decimal for this command", reason="environment")
        run.append(int(text))
    return invocation, record, plan, run[0], run[1]


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` for this user alone and write ``data``; nothing is followed or replaced."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(descriptor, view):]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise MbError(f"cannot write the selection record: {exc.strerror or exc}", reason="output") from exc


def run_select_build(args: argparse.Namespace) -> int:
    invocation, record, plan, run_id, run_attempt = _job(args)
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_SELECT_BUILD_REQUESTS)
    document = selection.select_build(api, plan=plan, run_id=run_id, run_attempt=run_attempt,
                                      workflow_path=record["workflow_path"], event=record["event"],
                                      temporary_root=args.state, build_run_id=args.build_run_id,
                                      wait_seconds=args.wait_seconds)
    if document is None:
        cli.write_github_output(args.github_output, {"found": False, "build_run_id": ""})
        return 0
    raw = canonical_json(document)
    _write_new(args.output, raw)
    identity.write_state_record(args.state, selection.SELECTION_NAME, raw)
    cli.write_github_output(args.github_output, {"found": True, "build_run_id": document["build"]["producer"]["run_id"],
                                                 "selection": raw.decode("utf-8").rstrip("\n")})
    return 0
