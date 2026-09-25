"""``pin verify`` and ``digest`` (MB9). Flags are frozen by SPEC §2.2."""

from __future__ import annotations

import argparse
import sys

from mod_base import cli
from mod_base.errors import MbError
from mod_base.github import api as github_api
from mod_base.model import grammar
from mod_base.pin import kit_tree_digest, verify


def _literal(value: str) -> str:
    return grammar.require(grammar.DIGEST, value, "digest literal")


def register(subparsers: argparse._SubParsersAction) -> None:
    pin = subparsers.add_parser("pin", help="pin checks")
    verbs = pin.add_subparsers(dest="pin_command", metavar="VERB", required=True)
    check = verbs.add_parser("verify", help="verify the mod's single kit pin (0 / 2)")
    check.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    check.add_argument("--network", action="store_true")
    check.set_defaults(handler=run_verify)

    digest = subparsers.add_parser("digest", help="print the kit-digest-v1 of a kit root")
    digest.add_argument("--root", type=cli.PATH, required=True, metavar="DIR")
    digest.add_argument("--check", type=cli.typed(_literal, "digest literal"), metavar="LITERAL")
    digest.set_defaults(handler=run_digest)


def run_verify(args: argparse.Namespace) -> int:
    client = github_api.from_environment(cli.environ()) if args.network else None
    pin = verify(args.repo, network=args.network, api=client)
    sys.stdout.write(f"{pin.sha} {pin.version}\n")
    return 0


def run_digest(args: argparse.Namespace) -> int:
    value = kit_tree_digest(args.root)
    sys.stdout.write(f"{value}\n")
    if args.check is not None and value != args.check:
        raise MbError(f"kit tree digest {value} does not equal the literal {args.check}", reason="digest-mismatch")
    return 0
