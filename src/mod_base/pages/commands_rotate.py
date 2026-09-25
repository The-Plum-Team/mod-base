"""``rotate`` (MB7). Flags are frozen by SPEC §2.2. Loads config data only (no repository checks,
no adapter): the rotate job sparse-checks-out ``site/mod-base.json`` alone."""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.github import api as github_api
from mod_base.model.canonical import canonical_json
from mod_base.pages import rotate


def register(subparsers: argparse._SubParsersAction) -> None:
    retire = subparsers.add_parser("rotate", help="retire evidence superseded by an authenticated owner run")
    cli.add_repo_config(retire)
    retire.add_argument("--owner-run-id", type=cli.POSITIVE, required=True)
    retire.add_argument("--owner-sha", type=cli.SHA1, required=True)
    retire.add_argument("--delete-delay-seconds", type=cli.DELAY, default=1.0)
    retire.add_argument("--dry-run", action="store_true")
    retire.set_defaults(handler=run_rotate)


def run_rotate(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ(), check_repository=False)
    client = github_api.from_environment(invocation.environ, writable=not args.dry_run)
    summary = rotate.rotate_generation(invocation, api=client, owner_run_id=args.owner_run_id,
                                       owner_sha=args.owner_sha, delete_delay_seconds=args.delete_delay_seconds,
                                       dry_run=args.dry_run)
    sys.stdout.buffer.write(canonical_json(summary))
    return 0
