"""``ci reuse-admit``: decide whether a push reuses the gates of its pull request.

The step runs after ``ci subject`` and ``ci plan`` in the plan job of a Build run and in the one
job of ``select-build.yml``, on a push to the default branch. It reads the job's subject and plan
from the state directory, decides (:func:`mod_base.build_ci.reuse.admit_post_merge_reuse`) and
says so three times:

* the outputs ``mode`` (``reuse`` or ``full``) and ``reason`` (``identical-tested-tree`` or a key
  of ``reuse.FULL_RUN_REASONS``), which the workflow turns into skipped or running workers;
* the state record ``ci-reuse-admission.json``: for a reuse the covered binding and the ``source``
  the gate's reference will name, for a full run the reason and its one-line explanation;
* one line on standard output.

A mode of ``reuse`` is written only after everything mutable was observed a second time. Corrupt
evidence, an API failure and an original run that has not finished write nothing and fail the
step. Any event but a push answers ``full`` without a request: a dispatch, a schedule or a release
is a request to build and run in full.

Both managed callers run this same decision over the same evidence, so the Build run and the
packaged run of one push reach the same answer. Neither takes it from the other: each gate
decides once more before it seals (``gate.seal_reuse``). A gate that finds the evidence gone by
then fails instead of sealing, and rerunning all jobs of its run decides again and tests in full.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

from mod_base import cli, runtime
from mod_base.build_ci import commands, generation, identity, planning, reuse
from mod_base.build_ci.commands_packaged import job_plan
from mod_base.build_ci.config import load_build_config
from mod_base.errors import MbError, single_line
from mod_base.model import limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check

#: The state record ``ci reuse-admit`` leaves: its answer and what the answer rests on.
ADMISSION_NAME = "ci-reuse-admission.json"


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    """Register ``reuse-admit`` on the ``ci`` verb group ``verbs``."""

    admit = verbs.add_parser("reuse-admit", help="decide whether this push reuses the gates of its pull request")
    commands.add_job_arguments(admit)
    admit.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    admit.set_defaults(handler=run_reuse_admit)
    select = verbs.add_parser("select-generation",
                              help="select and authenticate the tested generation of this default-branch commit")
    commands.add_job_arguments(select)
    select.add_argument("--output", type=cli.PATH, required=True, metavar="DIR",
                        help="a new private directory for generation.json (and build/ with --with-build)")
    select.add_argument("--with-build", action="store_true",
                        help="also copy the complete Build bundle the packaged gate tested")
    lanes = select.add_mutually_exclusive_group()
    lanes.add_argument("--lane", action="append", default=[], metavar="ID",
                       help="also keep the verified export of this planned lane (repeatable)")
    lanes.add_argument("--all-lanes", action="store_true", help="also keep the verified export of every lane")
    select.add_argument("--github-output", type=cli.PATH, required=True, metavar="FILE")
    select.set_defaults(handler=run_select_generation)


def _job(args: argparse.Namespace) -> tuple[runtime.Invocation, dict[str, Any], dict[str, Any]]:
    """The invocation, the identity record and the plan of the job whose state ``args`` names."""

    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    record = identity.read_subject(args.state)
    subject = record["subject"]
    check(invocation.repository == subject["repository"] and invocation.implementation_sha == subject["controller_sha"],
          "$.state", "the state directory belongs to another repository or controller commit")
    return invocation, record, planning.require_plan(job_plan(args.state), subject=subject)


def run_reuse_admit(args: argparse.Namespace) -> int:
    invocation, record, plan = _job(args)
    api = commands.api_client(invocation, max_requests=limits.MAX_CI_REUSE_ADMIT_REQUESTS)
    with tempfile.TemporaryDirectory(prefix="mb-ci-reuse-", dir=args.state) as temporary:
        outcome = reuse.admit_post_merge_reuse(api, plan=plan, event=record["event"], temporary_root=Path(temporary))
    if isinstance(outcome, reuse.ReuseAdmitted):
        outcome.recheck()
        admission = {"mode": "reuse", "reason": reuse.ADMITTED_REASON, "pr_number": outcome.pr_number,
                     "identity": plan["identity"], "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                     "source": outcome.source}
        build, packaged = (outcome.source[key]["producer"] for key in ("build_seal", "packaged_seal"))
        line = (f"reuse of pull request #{outcome.pr_number}: Build run {build['run_id']} attempt "
                f"{build['run_attempt']}, packaged run {packaged['run_id']} attempt {packaged['run_attempt']}")
    else:
        admission = {"mode": "full", "reason": outcome.reason, "detail": outcome.detail}
        line = f"full run ({outcome.reason}): {outcome.detail}"
    identity.write_state_record(args.state, ADMISSION_NAME, canonical_json(admission))
    cli.write_github_output(args.github_output, {"mode": admission["mode"], "reason": admission["reason"]})
    sys.stdout.write(f"reuse-admit: {single_line(line)}\n")
    return 0


def run_select_generation(args: argparse.Namespace) -> int:
    """``ci select-generation``: publish the tested generation of the job's protected subject, with
    the Build bundle (``--with-build``) and the verified exports of lanes (``--lane ID`` or
    ``--all-lanes``) when asked for.

    Outputs ``generation`` (``full``, ``reuse`` or ``none``) and ``reason`` (``selected`` or a key
    of ``generation.ABSENT_REASONS``) before it returns or raises, so a step that tolerates exit 3
    can still branch on them; a rejection writes ``none`` and the error's own reason."""

    try:
        invocation, _, plan = _job(args)
        check(plan["identity"]["pr_number"] == 0, "$.state", "a generation covers a commit of the default branch")
        config = load_build_config(invocation.repo_root, repository=invocation.repository)
        api = commands.api_client(invocation, max_requests=limits.MAX_CI_SELECT_GENERATION_REQUESTS)
        lanes = tuple(lane["id"] for lane in plan["lanes"]) if args.all_lanes else tuple(args.lane)
        document = generation.select_generation(api, plan=plan, output=args.output, with_build=args.with_build,
                                                lanes=lanes, source_config_sha256=config.sha256)
    except MbError as error:
        cli.write_github_output(args.github_output, {"generation": "none", "reason": error.reason})
        raise
    source = document["source"]
    seals = (source["build_seal"]["producer"], source["packaged_seal"]["producer"])
    line = (f"{document['mode']} generation of pull request #{source['identity']['pr_number']}"
            if document["mode"] == "reuse" else "full generation")
    line += "".join(f", {gate} run {seal['run_id']} attempt {seal['run_attempt']}"
                    for gate, seal in zip(("Build", "packaged"), seals))
    cli.write_github_output(args.github_output, {"generation": document["mode"], "reason": "selected"})
    sys.stdout.write(f"select-generation: {single_line(line)}\n")
    return 0
