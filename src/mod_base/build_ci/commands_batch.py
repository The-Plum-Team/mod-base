"""``ci batch-prepare`` and ``ci batch-settle`` (K5, KQ8-1).

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

The allowed-path list of ``batch-prepare`` is a JSON array of repository paths written by the
mod's protected policy; an entry admits itself and everything below it. Without
``--allowed-paths`` the list is the one the protected Build config's ``batch`` block names, read
from the protected checkout ``--repo``, the batch is capped at that block's ``max_members`` and
the actor and triggering actor of the run must both be among its ``operators``: the step of the
managed batch caller. Whenever the protected checkout has a Build config, the list may admit no
path that config keeps out of every batch (``config.never_batched``), nor a managed file of the
kit's template or the kit config ``site/mod-base.json``.

``batch-settle`` takes the plan of the batch pull request's gates and the descriptors of its two
tested records as files (``--plan``, ``--build-seal``, ``--packaged-seal``), or none of the three:
it then derives them from the protected plan of the default-branch commit that ``ci subject`` and
``ci plan`` left in ``--state`` (``batch.settle_derived_batch``). There, ``--intents`` writes
nothing and outputs the closing intents for the job of the managed caller that holds the batch App.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import batch, batch_git, commands, identity, planning
from mod_base.build_ci import config as build_config
from mod_base.build_ci.commands_packaged import job_plan
from mod_base.config import DEFAULT_CONFIG_PATH
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, read_json_file
from mod_base.model.validators import check
from mod_base.template.tool import load_manifest

#: The seal files of an operator's ``batch-settle``: given all together or not at all.
_SEALS = ("plan", "build_seal", "packaged_seal")


def _batch_name(value: str) -> str:
    return grammar.require(grammar.CI_BATCH_NAME, value, "batch name")


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``batch-prepare`` and ``batch-settle`` on the ``ci`` verb group ``verbs``."""

    prepare = verbs.add_parser("batch-prepare", help="squash open pull requests into one new batch pull request")
    commands.add_job_arguments(prepare)
    prepare.add_argument("--name", type=cli.typed(_batch_name, "batch name"), required=True,
                         help="the batch branch is batch/<name>; it must not exist")
    prepare.add_argument("--allowed-paths", type=cli.PATH, default=None, metavar="FILE",
                         help="JSON array of the repository paths the protected policy lets a batch change; "
                              "default: the list of the protected Build config's batch block")
    prepare.add_argument("--dry-run", action="store_true", help="build the stack only; push and open nothing")
    prepare.add_argument("--github-output", type=cli.PATH, metavar="FILE")
    prepare.add_argument("pulls", nargs="+", type=cli.POSITIVE, metavar="PR", help="pull request numbers, in order")
    prepare.set_defaults(handler=run_batch_prepare)

    settle = verbs.add_parser("batch-settle", help="close the pull requests of a merged batch pull request")
    commands.add_job_arguments(settle)
    settle.add_argument("--pr", type=cli.POSITIVE, required=True, help="the merged batch pull request")
    settle.add_argument("--plan", type=cli.PATH, default=None, metavar="FILE",
                        help="the plan the batch pull request's gates ran with; omit with both seals to derive "
                             "them from the protected plan in --state")
    settle.add_argument("--build-seal", type=cli.PATH, default=None, metavar="FILE",
                        help="descriptor of its Build tested record")
    settle.add_argument("--packaged-seal", type=cli.PATH, default=None, metavar="FILE",
                        help="descriptor of its packaged tested record")
    settle.add_argument("--intents", action="store_true",
                        help="derived settlement only: write nothing and output the closing intents")
    settle.add_argument("--delete-branches", action="store_true",
                        help="also delete a closed member's branch while it still points at the batched head")
    settle.add_argument("--github-output", type=cli.PATH, metavar="FILE")
    settle.set_defaults(handler=run_batch_settle)


def _document(path: Path, label: str, max_bytes: int) -> Any:
    return read_json_file(path, label=label, max_bytes=max_bytes)[0]


def _configured_batch(invocation: runtime.Invocation) -> build_config.BuildConfig | None:
    """The protected Build config of the checkout ``--repo`` names, or ``None`` when it has none."""

    state = build_config.protected_file_state(invocation.repo_root, build_config.BUILD_CONFIG_PATH,
                                              max_bytes=limits.MAX_CI_CONFIG_BYTES)
    if state.sha256 is None:
        return None
    return build_config.load_build_config(invocation.repo_root, repository=invocation.repository)


def _kit_paths() -> tuple[str, ...]:
    """The kit-owned files of a mod, which no batch changes: every managed file of the executing
    kit's template (its bootstrap, its Pages caller, its shared documents) and the kit config."""

    manifest = load_manifest(runtime.kit_root())
    return tuple(sorted({entry["path"] for entry in manifest["files"] if entry["class"] == "managed"}
                        | {DEFAULT_CONFIG_PATH}))


def _require_operators(invocation: runtime.Invocation, settings: dict[str, Any]) -> None:
    """Both the actor and the triggering actor of the run are operators of the batch block: a run
    started by anyone else, or re-run by anyone else, requests no batch."""

    operators = {login.casefold() for login in settings["operators"]}
    for name in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR"):
        login = invocation.environ.get(name, "")
        check(type(login) is str and login.casefold() in operators, "$.batch.operators",
              f"{name} is not an operator of the Build config's batch block")


def run_batch_prepare(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ(), check_repository=False)
    config = _configured_batch(invocation)
    if args.allowed_paths is None:
        settings = None if config is None else build_config.batch(config.data)
        check(settings is not None, "$.batch", "without --allowed-paths the protected Build config must name a batch "
                                               "block: batching is not enabled in this repository")
        _require_operators(invocation, settings)
        maximum = settings.get("max_members", limits.MAX_CI_BATCH_MEMBERS)
        check(len(args.pulls) <= maximum, "$.batch.members",
              f"the Build config's batch block admits at most {maximum} pull requests")
        # The list is a file of the protected checkout, reached without crossing a symlink.
        listed = build_config.protected_file_state(invocation.repo_root, settings["allowed_paths"],
                                                   max_bytes=limits.MAX_CI_CONFIG_BYTES)
        check(listed.sha256 is not None, "$.batch.allowed_paths",
              f"the protected checkout has no allowed-path list at {settings['allowed_paths']}")
        allowed = _document(invocation.repo_root / settings["allowed_paths"], "allowed-path list",
                            limits.MAX_CI_CONFIG_BYTES)
    else:
        allowed = _document(args.allowed_paths, "allowed-path list", limits.MAX_CI_CONFIG_BYTES)
    check(type(allowed) is list, "$.allowed_paths", "must be a JSON array of repository paths")
    if config is not None:
        batch_git.allowed_path_roots(tuple(allowed))
        build_config.check_batch_paths(config, tuple(allowed), _kit_paths())
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


def _derived_plan(args: argparse.Namespace) -> tuple[runtime.Invocation, dict[str, Any]]:
    """The invocation and the protected plan of the default-branch commit whose job state ``args`` names."""

    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    subject = identity.read_subject(args.state)["subject"]
    check(invocation.repository == subject["repository"] and invocation.implementation_sha == subject["controller_sha"],
          "$.state", "the state directory belongs to another repository or controller commit")
    plan = planning.require_plan(job_plan(args.state), subject=subject)
    check(plan["identity"]["pr_number"] == 0, "$.state",
          "a batch is settled from the protected plan of a default-branch commit")
    return invocation, plan


def run_batch_settle(args: argparse.Namespace) -> int:
    given = [name for name in _SEALS if getattr(args, name) is not None]
    check(len(given) in (0, len(_SEALS)), "$.batch", "--plan, --build-seal and --packaged-seal go together")
    if not given:
        return _run_derived_settle(args)
    check(not args.intents, "$.batch", "--intents derives the gates: give no plan or seal with it")
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
    _write_report(invocation, args, report)
    return 0


def _run_derived_settle(args: argparse.Namespace) -> int:
    """``batch-settle`` with the gates derived from the protected plan of the job's state."""

    check(not (args.intents and args.delete_branches), "$.batch", "--intents deletes no branch")
    invocation, plan = _derived_plan(args)
    client = commands.api_client(invocation, max_requests=limits.MAX_CI_BATCH_SETTLE_DERIVED_REQUESTS,
                                 writable=not args.intents)
    remote = batch_git.github_remote(invocation.repository, invocation.token)
    with batch_git.open_batch_store(args.state, remote) as store:
        result = batch.settle_derived_batch(client, store, pr_number=args.pr, plan=plan, temporary_root=args.state,
                                            intents=args.intents, delete_branches=args.delete_branches)
    raw = canonical_json(result)
    sys.stdout.buffer.write(raw)
    if args.intents:
        cli.write_github_output(invocation.github_output(args.github_output),
                                {"intents": raw.decode("utf-8").rstrip("\n")})
    else:
        _write_report(invocation, args, result)
    return 0


def _write_report(invocation: runtime.Invocation, args: argparse.Namespace, report: dict[str, list[int]]) -> None:
    """Output how many members a settlement closed, left changed, found closed and deleted."""

    cli.write_github_output(invocation.github_output(args.github_output), {
        "closed": len(report["closed"]), "changed": len(report["changed"]),
        "already_closed": len(report["already_closed"]), "deleted": len(report["deleted"])})

