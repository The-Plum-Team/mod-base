"""``ci seed-key`` and ``ci seed-export``: the protected seed of a target or lane job.

Both are steps of the ``target`` job of ``build.yml`` and of the ``lane`` job of
``packaged-e2e.yml`` (``build_ci.seeds``):

* ``seed-key --kind gradle|runtime --unit ID --github-output FILE``, after ``ci plan``, writes the
  output ``key``: the cache key of the job's seed, derived from protected files of the default
  branch, or the empty string when the protected Build config enables no seed of that kind. The
  workflow restores only that exact key and hands what it restored to ``ci worker-stage
  --gradle-seed``.
* ``seed-export --kind gradle|runtime --output DIR --github-output FILE`` runs only in a protected
  job (a push or a dispatch on the default branch) whose restore missed, after its results were
  uploaded. It writes ``DIR`` from the locked candidate's Gradle home and the output ``saved``
  (``true`` when ``DIR`` holds a seed to save). A home that cannot be exported declines the seed
  with one line and ``saved=false``; the job's result does not depend on it.

Neither reads the API or receives a token.
"""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.build_ci import commands, lifecycle, seeds
from mod_base.build_ci.commands_worker import UNIT_ID
from mod_base.build_ci.config import SEED_KINDS


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    key = verbs.add_parser("seed-key", help="derive the cache key of the job's protected seed")
    commands.add_job_arguments(key)
    key.add_argument("--kind", choices=SEED_KINDS, required=True, help="gradle (a target) or runtime (a lane)")
    key.add_argument("--unit", type=UNIT_ID, required=True, metavar="ID", help="the planned target or lane")
    key.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    key.set_defaults(handler=run_seed_key)

    export = verbs.add_parser("seed-export",
                              help="copy the locked candidate's Gradle home out as the next seed (protected jobs)")
    commands.add_job_arguments(export)
    export.add_argument("--kind", choices=SEED_KINDS, required=True, help="gradle (a target) or runtime (a lane)")
    export.add_argument("--output", type=cli.PATH, required=True, metavar="DIR",
                        help="the new directory the job saves; it must not exist")
    export.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    export.set_defaults(handler=run_seed_export)


def run_seed_key(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    key = seeds.derive_seed_key(job, invocation.repo_root, args.kind, args.unit)
    cli.write_github_output(args.github_output, {"key": key or ""})
    sys.stdout.write(f"seed-key: {args.kind} seed {key} for {args.unit}\n" if key else
                     f"seed-key: the protected Build config enables no {args.kind} seed\n")
    return 0


def run_seed_export(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.open_worker(job)
    records = seeds.export_seed(job, worker, args.kind, args.output, log=sys.stdout.write)
    cli.write_github_output(args.github_output, {"saved": bool(records)})
    if records:
        sys.stdout.write(f"seed-export: {args.kind} seed of {len(records)} files, "
                         f"{sum(record['size'] for record in records)} bytes, ready to save\n")
    elif records is not None:
        sys.stdout.write(f"seed-export: the candidate left no {args.kind} seed to save\n")
    return 0
