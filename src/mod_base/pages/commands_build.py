"""``build`` and ``refresh`` (MB6). Flags are frozen by SPEC §2.2.

``build --collected DIR --families DIR`` name new work directories that ``build`` downloads this
run's collected artifacts into; ``refresh --input DIR`` names the new directory ``refresh`` writes
the exact upload bytes into (see :mod:`mod_base.pages.build` and :mod:`mod_base.pages.refresh`).
Both handlers first prove the host facts an entry point cannot see in its invocation
(:func:`mod_base.pages.build.check_checkouts`: no inherited ``GIT_*`` variable, the mod and kit
checkouts clean at their commits).
"""

from __future__ import annotations

import argparse

from mod_base import cli, runtime
from mod_base.github import api as github_api
from mod_base.model.canonical import canonical_json
from mod_base.model.limits import MAX_PAGES_API_READS
from mod_base.pages import build, refresh


def register(subparsers: argparse._SubParsersAction) -> None:
    render = subparsers.add_parser("build", help="recheck and render the atomic static site")
    cli.add_repo_config(render)
    render.add_argument("--kit-root", type=cli.PATH, required=True, metavar="DIR")
    render.add_argument("--collected", type=cli.PATH, required=True, metavar="DIR")
    render.add_argument("--families", type=cli.PATH, required=True, metavar="DIR")
    render.add_argument("--output", type=cli.PATH, required=True, metavar="_site")
    render.add_argument("--promotion", type=cli.PATH, required=True, metavar="DIR")
    render.add_argument("--github-output", type=cli.PATH, required=True, metavar="F")
    render.set_defaults(handler=run_build)

    roll = subparsers.add_parser("refresh", help="revalidate a promoted bundle and name its cache")
    cli.add_repo_config(roll)
    roll.add_argument("--key", type=cli.KEY, required=True)
    roll.add_argument("--family", type=cli.FAMILY)
    roll.add_argument("--input", type=cli.PATH, required=True, metavar="DIR")
    roll.add_argument("--github-output", type=cli.PATH, required=True, metavar="F")
    roll.set_defaults(handler=run_refresh)


def run_build(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    build.check_checkouts(invocation, kit_root=args.kit_root, environ=cli.environ())
    client = github_api.from_environment(invocation.environ, max_requests=MAX_PAGES_API_READS)
    result = build.build_site(invocation, api=client, kit_root=args.kit_root, collected_dir=args.collected,
                              families_dir=args.families, output=args.output, promotion_dir=args.promotion)
    cli.write_github_output(args.github_output, {
        "heads": canonical_json(result.heads).decode("utf-8").rstrip("\n"),
        "site_sha256": result.site_sha256,
    })
    return 0


def run_refresh(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    build.check_checkouts(invocation, kit_root=invocation.kit_root, environ=cli.environ())
    client = github_api.from_environment(invocation.environ, max_requests=MAX_PAGES_API_READS)
    result = refresh.refresh_bundle(invocation, api=client, key=args.key, family=args.family, input_dir=args.input)
    outputs: dict[str, str | bool] = {"available": result.available, "cache_name": result.cache_name or ""}
    if result.baseline_name is not None:
        outputs["baseline_name"] = result.baseline_name
    cli.write_github_output(args.github_output, outputs)
    return 0
