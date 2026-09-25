"""``download`` and ``budget`` (MB1). Flags are frozen by SPEC §2.2.

``download`` fetches one kit artifact by immutable id through a read-only client and extracts it
with the bounded-ZIP limits of its artifact kind. ``budget`` is Quick Skin's
``github_api_budget_snapshot``: one advisory ``/rate_limit`` read printed as canonical JSON of the
numeric ``core`` counters only; an unavailable snapshot is reported on stderr and never fails the
step, because telemetry can neither authorize nor reject evidence.
"""

from __future__ import annotations

import argparse
import sys

from mod_base import cli
from mod_base.errors import MbError, single_line
from mod_base.github import api as github_api
from mod_base.github import artifacts
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json


def register(subparsers: argparse._SubParsersAction) -> None:
    download = subparsers.add_parser("download", help="download one artifact by immutable id and extract it")
    download.add_argument("--artifact-id", type=cli.POSITIVE, required=True)
    download.add_argument("--name", required=True)
    download.add_argument("--digest", type=cli.DIGEST, required=True)
    download.add_argument("--size", type=cli.POSITIVE, required=True)
    download.add_argument("--run-id", type=cli.POSITIVE, required=True)
    download.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    download.set_defaults(handler=run_download)

    budget = subparsers.add_parser("budget", help="print the Actions token's numeric REST budget counters")
    budget.set_defaults(handler=run_budget)


def run_download(args: argparse.Namespace) -> int:
    parsed = grammar.parse_artifact_name(args.name)
    if parsed is None or parsed.kind not in LIMITS_BY_KIND:
        raise MbError("download accepts only kit evidence artifact names", reason="usage")
    client = github_api.from_environment(cli.environ())
    artifacts.download(client, artifact_id=args.artifact_id, name=args.name, digest=args.digest, size=args.size,
                       run_id=args.run_id, output=args.output, extraction=LIMITS_BY_KIND[parsed.kind])
    return 0


def run_budget(args: argparse.Namespace) -> int:
    try:
        counters = github_api.from_environment(cli.environ()).rate_limit_snapshot()
    except MbError as exc:
        print(f"GitHub REST core budget telemetry unavailable ({single_line(exc.reason, limit=60)}).",
              file=sys.stderr)
        return 0
    sys.stdout.write(canonical_json(counters).decode("ascii"))
    sys.stdout.flush()
    return 0
