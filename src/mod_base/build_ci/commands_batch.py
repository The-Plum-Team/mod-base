"""``ci batch-prepare`` and ``ci batch-settle`` (K5).

``batch-prepare`` squashes the given open pull requests, in order, onto the default branch head,
pushes the stack as the new branch ``batch/<name>`` and opens one ready pull request for it.
``batch-settle`` closes the members of a merged batch pull request after proving what merged.
Both print one canonical JSON document; ``batch.prepare_batch`` and ``batch.settle_batch`` say
what they check.

Neither command mints a credential: the API client and the Git transport use the token the
invocation carries (``GH_TOKEN``, else ``GITHUB_TOKEN``). A pull request opened with the default
``GITHUB_TOKEN`` of a workflow run starts no workflows, so a batch opened with it would never be
gated: supply the token of a GitHub App or of an automation account. ``--state`` is the job's
private state directory, which only the invoking user can write; the private Git store lives below
it for the command's lifetime.

``batch-settle`` takes the plan of the batch pull request's gates and the descriptors of its two
tested records as files. Each descriptor names the run that sealed it, so no workflow is named on
the command line.

The allowed-path list of ``batch-prepare`` is a JSON array of repository paths written by the
mod's protected policy; an entry admits itself and everything below it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import batch, batch_git, commands
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, read_json_file
from mod_base.model.validators import check


def _batch_name(value: str) -> str:
    return grammar.require(grammar.CI_BATCH_NAME, value, "batch name")


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``batch-prepare`` and ``batch-settle`` on the ``ci`` verb group ``verbs``."""

    prepare = verbs.add_parser("batch-prepare", help="squash open pull requests into one new batch pull request")
    commands.add_job_arguments(prepare)
    prepare.add_argument("--name", type=cli.typed(_batch_name, "batch name"), required=True,
                         help="the batch branch is batch/<name>; it must not exist")
    prepare.add_argument("--allowed-paths", type=cli.PATH, required=True, metavar="FILE",
                         help="JSON array of the repository paths the protected policy lets a batch change")
    prepare.add_argument("--dry-run", action="store_true", help="build the stack only; push and open nothing")
    prepare.add_argument("--github-output", type=cli.PATH, metavar="FILE")
    prepare.add_argument("pulls", nargs="+", type=cli.POSITIVE, metavar="PR", help="pull request numbers, in order")
    prepare.set_defaults(handler=run_batch_prepare)

    settle = verbs.add_parser("batch-settle", help="close the pull requests of a merged batch pull request")
    commands.add_job_arguments(settle)
    settle.add_argument("--pr", type=cli.POSITIVE, required=True, help="the merged batch pull request")
    settle.add_argument("--plan", type=cli.PATH, required=True, metavar="FILE",
                        help="the plan the batch pull request's gates ran with")
    settle.add_argument("--build-seal", type=cli.PATH, required=True, metavar="FILE",
                        help="descriptor of its Build tested record")
    settle.add_argument("--packaged-seal", type=cli.PATH, required=True, metavar="FILE",
                        help="descriptor of its packaged tested record")
    settle.add_argument("--delete-branches", action="store_true",
                        help="also delete a closed member's branch while it still points at the batched head")
    settle.add_argument("--github-output", type=cli.PATH, metavar="FILE")
    settle.set_defaults(handler=run_batch_settle)


def _document(path: Path, label: str, max_bytes: int) -> Any:
    return read_json_file(path, label=label, max_bytes=max_bytes)[0]


def run_batch_prepare(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ(), check_repository=False)
    allowed = _document(args.allowed_paths, "allowed-path list", limits.MAX_CI_CONFIG_BYTES)
    check(type(allowed) is list, "$.allowed_paths", "must be a JSON array of repository paths")
    client = commands.api_client(invocation, max_requests=limits.MAX_CI_BATCH_PREPARE_REQUESTS,
                                 writable=not args.dry_run)
    remote = batch_git.github_remote(invocation.repository, invocation.token)
    with batch_git.open_batch_store(args.state, remote) as store:
        result = batch.prepare_batch(client, store, name=args.name, pr_numbers=tuple(args.pulls),
                                     allowed_paths=tuple(allowed), dry_run=args.dry_run)
    sys.stdout.buffer.write(canonical_json(result))
    manifest = result["manifest"]
    cli.write_github_output(invocation.github_output(args.github_output), {
        "branch": manifest["branch"], "head_sha": manifest["members"][-1]["squash_sha"],
        "pr_number": "" if result["pr_number"] is None else result["pr_number"]})
    return 0


def run_batch_settle(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ(), check_repository=False)
    plan = _document(args.plan, "batch plan", limits.MAX_CI_PLAN_BYTES)
    build_seal = _document(args.build_seal, "Build tested record descriptor", limits.MAX_CI_RECORD_BYTES)
    packaged_seal = _document(args.packaged_seal, "packaged tested record descriptor", limits.MAX_CI_RECORD_BYTES)
    client = commands.api_client(invocation, max_requests=limits.MAX_CI_BATCH_SETTLE_REQUESTS, writable=True)
    remote = batch_git.github_remote(invocation.repository, invocation.token)
    with batch_git.open_batch_store(args.state, remote) as store:
        report = batch.settle_batch(client, store, pr_number=args.pr, plan=plan, build_seal=build_seal,
                                    packaged_seal=packaged_seal, temporary_root=args.state,
                                    delete_branches=args.delete_branches)
    sys.stdout.buffer.write(canonical_json(report))
    cli.write_github_output(invocation.github_output(args.github_output),
                            {name: len(numbers) for name, numbers in report.items()})
    return 0
