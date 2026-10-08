"""The top-level ``ci`` command: the steps of the protected Build and packaged-runtime jobs.

``ci`` has one verb per job step. Each work area keeps its verbs in a module of its own and lists
that module in :data:`VERB_MODULES`; a verb module exposes ``add_verbs(verbs)``, which adds its
parsers with ``set_defaults(handler=...)`` like every other command module. Every verb takes
``--repo``, ``--config`` and ``--state`` (:func:`add_job_arguments`), reads ``GITHUB_*`` only
through the invocation and reaches the API only through a client with an explicit request budget
(:func:`api_client`).
"""

from __future__ import annotations

import argparse
import importlib

from mod_base import cli, runtime
from mod_base.github import api as github_api

#: The modules that add verbs to ``ci``, in help order. One line per work area.
VERB_MODULES = (
    "mod_base.build_ci.commands_subject",
    "mod_base.build_ci.commands_worker",
    "mod_base.build_ci.commands_batch",
)


def register(subparsers: argparse._SubParsersAction) -> None:
    ci = subparsers.add_parser("ci", help="steps of the protected Build and packaged-runtime jobs")
    verbs = ci.add_subparsers(dest="ci_command", metavar="VERB", required=True)
    for name in VERB_MODULES:
        importlib.import_module(name).add_verbs(verbs)


def add_job_arguments(parser: argparse.ArgumentParser) -> None:
    """Add what every verb takes: ``--repo``, ``--config`` and ``--state DIR``, the job's private
    state directory below ``$RUNNER_TEMP`` (``ci subject`` creates it; every later verb reads it)."""

    cli.add_repo_config(parser)
    parser.add_argument("--state", type=cli.PATH, required=True, metavar="DIR",
                        help="the job's private state directory (created by `ci subject`)")


def api_client(invocation: runtime.Invocation, *, max_requests: int,
               writable: bool = False) -> github_api.GitHubApi:
    """The API client of one command, bounded to ``max_requests`` requests in all. It is read-only
    unless the verb is one that writes to GitHub and says so (``writable``)."""

    return github_api.from_environment(invocation.environ, writable=writable, max_requests=max_requests)
