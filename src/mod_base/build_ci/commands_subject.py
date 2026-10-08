"""``ci subject``: authenticate what this job tests and write its identity record.

The first step of every Build and packaged job. ``--pr`` is the pull request number of a
``pull_request_target`` run and empty for a protected push, dispatch or schedule; ``--producer``
says which gate the job belongs to. The command creates ``--state`` and writes
``identity.json`` there (:mod:`mod_base.build_ci.identity`), then outputs ``tested_sha`` (the commit
to check out as the candidate) and ``pr_number`` (empty for a protected subject).
"""

from __future__ import annotations

import argparse

from mod_base import cli, runtime
from mod_base.build_ci import commands, identity
from mod_base.build_ci.protocol import PRODUCERS
from mod_base.model import grammar, limits


def _pull_request(value: str) -> int | None:
    if value == "":
        return None
    if not grammar.POSITIVE_DECIMAL.fullmatch(value):
        raise ValueError(value)
    return grammar.require_positive_int(int(value), "pull request number")


PULL_REQUEST = cli.typed(_pull_request, "pull request number")


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    subject = verbs.add_parser("subject", help="authenticate the tested subject and write its identity record")
    commands.add_job_arguments(subject)
    subject.add_argument("--producer", choices=PRODUCERS, required=True)
    subject.add_argument("--pr", type=PULL_REQUEST, required=True, metavar="N",
                         help="the pull request number; empty for a protected push, dispatch or schedule")
    subject.add_argument("--github-output", type=cli.PATH, required=True, metavar="F")
    subject.set_defaults(handler=run_subject)


def run_subject(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_SUBJECT_REQUESTS)
    record = identity.authenticate_subject(invocation, api, producer=args.producer, pr_number=args.pr)
    identity.write_subject(args.state, record)
    subject = identity.read_subject(args.state)["subject"]
    cli.write_github_output(args.github_output, {"tested_sha": subject["tested_sha"],
                                                 "pr_number": subject["pr_number"] or ""})
    return 0
