"""``template check|sync|init`` (MB9). Flags are frozen by SPEC §2.2; exit 2 on drift."""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.errors import MbError, single_line
from mod_base.template import tool


def register(subparsers: argparse._SubParsersAction) -> None:
    template = subparsers.add_parser("template", help="repository-configuration template")
    verbs = template.add_subparsers(dest="template_command", metavar="VERB", required=True)
    check = verbs.add_parser("check", help="report drift (exit 2 on drift)")
    check.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    check.set_defaults(handler=run_check)
    sync = verbs.add_parser("sync", help="rewrite managed files (dry run without --write)")
    sync.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    sync.add_argument("--write", action="store_true")
    sync.set_defaults(handler=run_sync)
    init = verbs.add_parser("init", help="seed a new repository (never overwrites)")
    init.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    init.add_argument("--seed", action="store_true")
    init.add_argument("--from-config", type=cli.PATH, metavar="FILE")
    init.set_defaults(handler=run_init)


def _report(drifts: list[tool.Drift]) -> int:
    for drift in drifts:
        sys.stdout.write(f"{drift.kind}: {drift.path}\n{drift.detail}\n")
    if drifts:
        raise MbError(f"{len(drifts)} template drift(s): {single_line(', '.join(d.path for d in drifts))}",
                      reason="template-drift")
    return 0


def run_check(args: argparse.Namespace) -> int:
    return _report(tool.check(args.repo, kit_root=runtime.kit_root()))


def run_sync(args: argparse.Namespace) -> int:
    drifts = tool.sync(args.repo, kit_root=runtime.kit_root(), write=args.write)
    return 0 if args.write else _report(drifts)


def run_init(args: argparse.Namespace) -> int:
    for path in tool.init(args.repo, kit_root=runtime.kit_root(), seed=args.seed, from_config=args.from_config):
        sys.stdout.write(f"created {path}\n")
    return 0
