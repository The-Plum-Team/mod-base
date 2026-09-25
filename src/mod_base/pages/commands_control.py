"""``admit``, ``select`` and ``authenticate`` (MB5).

Flags are frozen by SPEC §2.2, plus the announced MB0 amendments: ``admit`` also outputs
``subjects`` (and ``coverage_sha`` in every ``families`` entry), and ``select --output F`` writes
the ``Selected`` JSON object that ``authenticate --selected-json`` reads.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from mod_base import cli, runtime
from mod_base.errors import MbError
from mod_base.github import api as github_api
from mod_base.model.canonical import canonical_json
from mod_base.model.limits import MAX_PAGES_API_READS
from mod_base.pages import admission, authenticate, select
from mod_base.workflow import PUBLISH_OPERATIONS


def _json(value: object) -> str:
    """A single-line canonical JSON output value."""

    return canonical_json(value).decode("utf-8").rstrip("\n")


def register(subparsers: argparse._SubParsersAction) -> None:
    admit = subparsers.add_parser("admit", help="decide whether this Pages run publishes")
    cli.add_repo_config(admit)
    admit.add_argument("--operation", choices=PUBLISH_OPERATIONS, required=True)
    admit.add_argument("--run-id", type=cli.POSITIVE)
    admit.add_argument("--sha", type=cli.SHA1)
    admit.add_argument("--family", type=cli.FAMILY)
    admit.add_argument("--bundle-key", type=cli.KEY)
    admit.add_argument("--artifact-id", type=cli.POSITIVE)
    admit.add_argument("--artifact-digest", type=cli.DIGEST)
    admit.add_argument("--coverage-sha", type=cli.SHA1)
    admit.add_argument("--github-output", type=cli.PATH, required=True, metavar="F")
    admit.set_defaults(handler=run_admit)

    choose = subparsers.add_parser("select", help="select the newest authenticated evidence (exit 3 = none)")
    cli.add_repo_config(choose)
    choose.add_argument("--key", type=cli.KEY, required=True)
    choose.add_argument("--family", type=cli.FAMILY)
    choose.add_argument("--nomination", type=cli.POSITIVE, metavar="ID")
    choose.add_argument("--expected-subject-commit", type=cli.SHA1, required=True)
    choose.add_argument("--github-output", type=cli.PATH, required=True, metavar="F")
    choose.add_argument("--output", type=cli.PATH, metavar="F",
                        help="also write the Selected JSON object (authenticate --selected-json) to this new file")
    choose.set_defaults(handler=run_select)

    auth = subparsers.add_parser("authenticate", help="authenticate a downloaded selection")
    cli.add_repo_config(auth)
    auth.add_argument("--key", type=cli.KEY, required=True)
    auth.add_argument("--selected", type=cli.PATH, required=True, metavar="DIR")
    auth.add_argument("--selected-json", type=cli.PATH, required=True, metavar="F")
    auth.add_argument("--output", type=cli.PATH, required=True, metavar="F")
    auth.set_defaults(handler=run_authenticate)


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively (never follow or replace an existing file) and write ``data``."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as exc:
        raise MbError(f"cannot create {path}: {exc.strerror or exc}", reason="output") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def _client(invocation: runtime.Invocation) -> github_api.GitHubApi:
    return github_api.from_environment(invocation.environ, max_requests=MAX_PAGES_API_READS)


def run_admit(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    wake = admission.WakeInputs(run_id=args.run_id, sha=args.sha, family=args.family, bundle_key=args.bundle_key,
                                artifact_id=args.artifact_id, artifact_digest=args.artifact_digest,
                                coverage_sha=args.coverage_sha)
    result = admission.admit(invocation, api=_client(invocation), operation=args.operation, wake=wake)
    cli.write_github_output(args.github_output, {
        "eligible": result.eligible,
        "reason": result.reason,
        "bundle_keys": _json(result.bundle_keys),
        "subjects": _json(result.subjects),
        "families": _json(result.families),
        "nominations": _json(result.nominations),
        "heads": _json(result.heads),
    })
    return 0


def run_select(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    chosen = select.select_evidence(invocation, api=_client(invocation), key=args.key, family=args.family,
                                    nomination=args.nomination, expected_subject_commit=args.expected_subject_commit)
    cli.write_github_output(args.github_output, {
        "kind": chosen.kind,
        "artifact_id": chosen.artifact_id,
        "name": chosen.name,
        "digest": chosen.digest,
        "size": chosen.size,
        "run_id": chosen.run_id,
        "run_attempt": chosen.run_attempt,
    })
    if args.output is not None:
        _write_new(args.output, canonical_json(chosen.to_json()))
    return 0


def run_authenticate(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    authenticate.run_authenticate(invocation, api=_client(invocation), key=args.key, selected_dir=args.selected,
                                  selected_json=args.selected_json, output=args.output)
    return 0
