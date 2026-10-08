"""``ci gate-status``: the states the protected gates may show on a pull request's current head.

The one kit step of the status caller's ``evaluate`` job. It is read-only: it prints one canonical
JSON document of status intents (:func:`mod_base.build_ci.status.evaluate_gates`) and writes the
same document, on one line, as the output ``intents``. The caller-owned ``publish`` job posts them
with the mod's App credentials.

The context strings come from the protected Build config of the checkout that is executing and the
gates from its activation manifest; ``--pr`` is the only thing the command is told about the
pull request, and everything else about it is read from the API.

``--state`` is this job's private directory. The command creates it when no earlier step did, and
keeps the tested records it downloads in a private temporary directory below it. When the
directory holds the plan this job derived (``ci-plan.json``, written by ``ci plan``), the gates
are verified against it; without that record no gate can be ``success``.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from mod_base import cli, runtime
from mod_base.build_ci import commands, identity, status
from mod_base.build_ci.commands_packaged import job_plan
from mod_base.build_ci.config import load_build_config
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.template import tool


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``gate-status`` on the ``ci`` verb group ``verbs``."""

    parser = verbs.add_parser("gate-status", help="evaluate the protected gates of a pull request head (read-only)")
    commands.add_job_arguments(parser)
    parser.add_argument("--pr", type=cli.POSITIVE, required=True, metavar="N", help="the pull request number")
    parser.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    parser.set_defaults(handler=run_gate_status)


def run_gate_status(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    config = load_build_config(invocation.repo_root, repository=invocation.repository)
    activation = tool.load_template_activation(invocation.repo_root)
    if not os.path.lexists(args.state):
        identity.create_state(args.state)
    plan = job_plan(args.state) if os.path.lexists(args.state / grammar.CI_PLAN_NAME) else None
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_GATE_STATUS_REQUESTS)
    try:
        with tempfile.TemporaryDirectory(prefix="mb-ci-status-", dir=args.state) as temporary:
            document = status.evaluate_gates(api, pr_number=args.pr, config=config, activation=activation,
                                             controller_sha=invocation.implementation_sha, plan=plan,
                                             temporary_root=Path(temporary))
    except OSError as error:
        raise MbError("cannot hold the tested records of this evaluation privately", reason="ci-status") from error
    raw = canonical_json(document)
    sys.stdout.buffer.write(raw)
    cli.write_github_output(args.github_output, {"intents": raw.decode("utf-8").rstrip("\n")})
    return 0
