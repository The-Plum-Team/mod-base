"""``expect``, ``prepare``, ``validate``, ``compact``, ``compose`` and ``anchor`` (MB3).

Flags are frozen by SPEC §2.2, plus the announced MB0 amendment ``compose --selection F`` (the
selection draft that ``compose`` completes, see ``docs/SCHEMAS.md`` "The collect flow").
Handlers only translate flags and the environment into entry-point arguments and write
``$GITHUB_OUTPUT``; all validation lives in the entry points.
"""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.errors import MbError
from mod_base.evidence import anchor, compact, compose, expectation, prepare, validate
from mod_base.github import api as github_api
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import run_claim_from_environment


def register(subparsers: argparse._SubParsersAction) -> None:
    expect = subparsers.add_parser("expect", help="derive the mod-base.evidence.expectation of one key")
    cli.add_repo_config(expect)
    expect.add_argument("--key", type=cli.KEY, required=True)
    expect.add_argument("--tested-run-json", type=cli.PATH, metavar="F")
    expect.add_argument("--extensions", type=cli.PATH, metavar="F")
    expect.add_argument("--output", type=cli.PATH, required=True, metavar="F")
    expect.set_defaults(handler=run_expect)

    produce = subparsers.add_parser("prepare", help="produce the raw handoff bundle (no API call)")
    cli.add_repo_config(produce)
    produce.add_argument("--e2e-root", type=cli.PATH, required=True, metavar="DIR")
    produce.add_argument("--key", type=cli.KEY, required=True)
    produce.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    produce.add_argument("--subject-branch", type=cli.BRANCH, required=True)
    produce.add_argument("--subject-commit", type=cli.SHA1, required=True)
    produce.add_argument("--subject-tree", type=cli.SHA1, required=True)
    produce.add_argument("--tested-run-id", type=cli.POSITIVE, required=True)
    produce.add_argument("--tested-run-attempt", type=cli.POSITIVE, required=True)
    produce.add_argument("--tested-branch", type=cli.BRANCH, required=True)
    produce.add_argument("--tested-commit", type=cli.SHA1, required=True)
    produce.add_argument("--tested-controller-branch", type=cli.BRANCH, required=True)
    produce.add_argument("--tested-controller-sha", type=cli.SHA1, required=True)
    produce.add_argument("--extensions", type=cli.PATH, metavar="F")
    produce.add_argument("--anchor", choices=("auto", "off"), default="off")
    produce.add_argument("--anchor-output", type=cli.PATH, metavar="DIR")
    produce.set_defaults(handler=run_prepare)

    check = subparsers.add_parser("validate", help="validate a bundle in a fresh process (0 / 2 / 3)")
    cli.add_repo_config(check)
    check.add_argument("--key", type=cli.KEY, required=True)
    check.add_argument("--kind", choices=validate.KINDS, required=True)
    check.add_argument("--input", type=cli.PATH, required=True, metavar="DIR")
    check.add_argument("--bind-raw", type=cli.PATH, metavar="DIR")
    check.add_argument("--expected-subject-commit", type=cli.SHA1)
    check.set_defaults(handler=run_validate)

    shrink = subparsers.add_parser("compact", help="compact a handoff or cache with its selection embedded")
    cli.add_repo_config(shrink)
    shrink.add_argument("--key", type=cli.KEY, required=True)
    shrink.add_argument("--input", type=cli.PATH, required=True, metavar="DIR")
    shrink.add_argument("--selection", type=cli.PATH, required=True, metavar="F")
    shrink.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    shrink.set_defaults(handler=run_compact)

    combine = subparsers.add_parser("compose", help="compose selected evidence with its authenticated baseline")
    cli.add_repo_config(combine)
    combine.add_argument("--key", type=cli.KEY, required=True)
    combine.add_argument("--selected", type=cli.PATH, required=True, metavar="DIR")
    combine.add_argument("--selection", type=cli.PATH, required=True, metavar="F")
    combine.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    combine.set_defaults(handler=run_compose)

    anchors = subparsers.add_parser("anchor", help="lossless anchor identity, creation and validation")
    verbs = anchors.add_subparsers(dest="anchor_command", metavar="VERB", required=True)
    identity = verbs.add_parser("identity")
    cli.add_repo_config(identity)
    identity.add_argument("--key", type=cli.KEY, required=True)
    identity.add_argument("--handoff", type=cli.PATH, required=True, metavar="DIR")
    identity.set_defaults(handler=run_anchor_identity)
    create = verbs.add_parser("create")
    cli.add_repo_config(create)
    create.add_argument("--key", type=cli.KEY, required=True)
    create.add_argument("--handoff", type=cli.PATH, required=True, metavar="DIR")
    create.add_argument("--raw-artifact-id", type=cli.POSITIVE, required=True)
    create.add_argument("--raw-artifact-name", required=True)
    create.add_argument("--raw-artifact-digest", type=cli.DIGEST, required=True)
    create.add_argument("--output", type=cli.PATH, required=True, metavar="DIR")
    create.set_defaults(handler=run_anchor_create)
    verify = verbs.add_parser("validate")
    verify.add_argument("--key", type=cli.KEY, required=True)
    verify.add_argument("--input", type=cli.PATH, required=True, metavar="DIR")
    verify.add_argument("--expected-subject-commit", type=cli.SHA1)
    verify.add_argument("--raw-artifact-id", type=cli.POSITIVE)
    verify.add_argument("--raw-artifact-name")
    verify.add_argument("--raw-artifact-digest", type=cli.DIGEST)
    verify.set_defaults(handler=run_anchor_validate)


def _invocation(args: argparse.Namespace, **options: object) -> runtime.Invocation:
    return runtime.build_invocation(args.repo, args.config, cli.environ(), **options)  # type: ignore[arg-type]


def run_expect(args: argparse.Namespace) -> int:
    expectation.run_expect(_invocation(args), key=args.key, tested_run_json=args.tested_run_json,
                           extensions=args.extensions, output=args.output)
    return 0


def run_prepare(args: argparse.Namespace) -> int:
    invocation = _invocation(args, implementation_sha=args.subject_commit)
    if args.anchor_output is not None and args.anchor == "off":
        raise MbError("--anchor-output requires --anchor auto", reason="usage")
    tested = {
        "run_id": args.tested_run_id,
        "run_attempt": args.tested_run_attempt,
        "workflow_path": invocation.config.source["workflow"],
        "branch": args.tested_branch,
        "commit": args.tested_commit,
        "controller_branch": args.tested_controller_branch,
        "controller_sha": args.tested_controller_sha,
    }
    result = prepare.prepare_handoff(
        invocation,
        e2e_root=args.e2e_root,
        key=args.key,
        output=args.output,
        subject={"branch": args.subject_branch, "commit": args.subject_commit, "tree": args.subject_tree},
        tested=tested,
        handoff=run_claim_from_environment(invocation.environ),
        extensions_path=args.extensions,
        anchor=args.anchor,
        anchor_output=args.anchor_output,
    )
    cli.write_github_output(invocation.github_output(), {"anchor_eligible": result.anchor_eligible})
    return 0


def run_validate(args: argparse.Namespace) -> int:
    validate.validate_bundle(_invocation(args), args.kind, args.input, key=args.key, bind_raw=args.bind_raw,
                             expected_subject_commit=args.expected_subject_commit)
    return 0


def run_compact(args: argparse.Namespace) -> int:
    compact.compact_bundle(_invocation(args), key=args.key, input_dir=args.input, selection_path=args.selection,
                           output=args.output)
    return 0


def run_compose(args: argparse.Namespace) -> int:
    invocation = _invocation(args)
    client = github_api.from_environment(invocation.environ)
    compose.compose_selected(invocation, api=client, key=args.key, selected_dir=args.selected,
                             selection_path=args.selection, output=args.output)
    return 0


def run_anchor_identity(args: argparse.Namespace) -> int:
    invocation = _invocation(args)
    identity = anchor.anchor_identity(invocation, args.handoff, key=args.key)
    cli.write_github_output(invocation.github_output(),
                            {"anchor_eligible": identity.eligible, "anchor_name": identity.name or ""})
    sys.stdout.buffer.write(canonical_json({"eligible": identity.eligible, "name": identity.name}))
    return 0


def run_anchor_create(args: argparse.Namespace) -> int:
    anchor.create_anchor(_invocation(args), key=args.key, handoff_dir=args.handoff,
                         raw_artifact_id=args.raw_artifact_id, raw_artifact_name=args.raw_artifact_name,
                         raw_artifact_digest=args.raw_artifact_digest, output=args.output)
    return 0


def run_anchor_validate(args: argparse.Namespace) -> int:
    raw = (args.raw_artifact_id, args.raw_artifact_name, args.raw_artifact_digest)
    if any(value is not None for value in raw) and not all(value is not None for value in raw):
        raise MbError("--raw-artifact-id, --raw-artifact-name and --raw-artifact-digest go together", reason="usage")
    anchor.validate_anchor_dir(args.input, key=args.key, expected_subject_commit=args.expected_subject_commit,
                               raw_artifact_id=args.raw_artifact_id, raw_artifact_name=args.raw_artifact_name,
                               raw_artifact_digest=args.raw_artifact_digest)
    return 0
