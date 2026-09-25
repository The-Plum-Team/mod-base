"""``family envelope`` and ``family collect`` (MB4). Flags are frozen by SPEC §2.2.

``family collect --selected-json F`` is an MB0 amendment (``docs/INTERNAL-API.md``).
"""

from __future__ import annotations

import argparse

from mod_base import cli, runtime
from mod_base.errors import Superseded, Unavailable
from mod_base.family import envelope, paired
from mod_base.model.documents import run_claim_from_environment


def register(subparsers: argparse._SubParsersAction) -> None:
    family = subparsers.add_parser("family", help="family envelope creation and collection")
    verbs = family.add_subparsers(dest="family_command", metavar="VERB", required=True)

    create = verbs.add_parser("envelope", help="wrap a mod-native family bundle in its envelope")
    cli.add_repo_config(create)
    create.add_argument("--family", type=cli.FAMILY, required=True)
    create.add_argument("--key", type=cli.KEY, required=True)
    create.add_argument("--bundle", type=cli.PATH, required=True, metavar="DIR")
    create.add_argument("--coverage-sha", type=cli.SHA1, required=True)
    create.add_argument("--subject-branch", type=cli.BRANCH, required=True)
    create.add_argument("--subject-commit", type=cli.SHA1, required=True)
    create.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    create.set_defaults(handler=run_envelope)

    collect = verbs.add_parser("collect", help="validate a family generation through the adapter (exit 3 = absent)")
    cli.add_repo_config(collect)
    collect.add_argument("--family", type=cli.FAMILY, required=True)
    collect.add_argument("--key", type=cli.KEY, required=True)
    collect.add_argument("--input", type=cli.PATH, required=True, metavar="DIR")
    collect.add_argument("--expected-coverage-sha", type=cli.SHA1, required=True)
    collect.add_argument("--selected-json", type=cli.PATH, required=True, metavar="F",
                         help="the Selected JSON object select --family --output wrote for --input")
    collect.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    collect.set_defaults(handler=run_collect)


def run_envelope(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ(),
                                          implementation_sha=args.subject_commit)
    producer = run_claim_from_environment(invocation.environ)
    envelope.create_envelope(invocation, family=args.family, key=args.key, bundle_dir=args.bundle,
                             coverage_sha=args.coverage_sha,
                             subject={"branch": args.subject_branch, "commit": args.subject_commit},
                             producer=producer, output=args.output)
    return 0


def run_collect(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    outcome = paired.collect_family(invocation, family=args.family, key=args.key, input_dir=args.input,
                                    expected_coverage_sha=args.expected_coverage_sha, output=args.output,
                                    selected_json=args.selected_json)
    cli.write_github_output(invocation.github_output(), {"status": outcome.status,
                                                         "available": outcome.status == "available"})
    if outcome.status == "superseded":
        raise Superseded(outcome.reason)
    if outcome.status != "available":
        raise Unavailable(outcome.reason)
    return 0
