"""``conformance`` (MB10). Flags are frozen by SPEC §2.2."""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.conformance.run import run_conformance
from mod_base.model.canonical import canonical_json


def register(subparsers: argparse._SubParsersAction) -> None:
    conformance = subparsers.add_parser("conformance", help="synthetic end-to-end adapter conformance (0 / 2)")
    conformance.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    scope = conformance.add_mutually_exclusive_group()
    scope.add_argument("--keys", type=cli.KEYS, metavar="a,b")
    scope.add_argument("--all", action="store_true", dest="all_keys")
    conformance.add_argument("--kit-root", type=cli.PATH, metavar="DIR")
    conformance.add_argument("--families", action="store_true")
    conformance.set_defaults(handler=run)


def run(args: argparse.Namespace) -> int:
    report = run_conformance(repo=args.repo, keys=args.keys, all_keys=args.all_keys,
                             kit_root=args.kit_root or runtime.kit_root(), families=args.families)
    sys.stdout.buffer.write(canonical_json(report))
    return 0
