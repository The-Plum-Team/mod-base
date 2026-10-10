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

from mod_base import cli, runtime
from mod_base.build_ci import commands, identity, planning, reuse
from mod_base.build_ci.commands_packaged import job_plan
from mod_base.errors import single_line
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


def run_reuse_admit(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    record = identity.read_subject(args.state)
    subject = record["subject"]
    check(invocation.repository == subject["repository"] and invocation.implementation_sha == subject["controller_sha"],
          "$.state", "the state directory belongs to another repository or controller commit")
    plan = planning.require_plan(job_plan(args.state), subject=subject)
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
