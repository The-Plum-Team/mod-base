"""``ci worker-prepare``, ``ci plan``, ``ci worker-stage``, ``ci worker-run``, ``ci worker-seal``,
``ci worker-validate`` and ``ci worker-finish``: the worker lifecycle of a job.

Each is one job step run by the runner, after ``ci subject`` created ``--state``:

* ``worker-prepare --roles validator|candidate+validator --python PATH [--java-home PATH]...``
  fences the host and allocates the job's disposable accounts. ``--python`` is the interpreter
  hooks and root operations run with; every ``--java-home`` is a JDK the job installed (the first
  one is a hook's ``JAVA_HOME``).
* ``plan [--candidate DIR] [--expect-sha256 HEX] [--github-output FILE]`` runs ``derive_plan`` and
  writes ``<state>/ci-plan.json``. The candidate files the protected config names come from the
  Git objects of the candidate checkout ``DIR`` when the job has one, otherwise from the API. ``--expect-sha256`` is
  the plan hash another job of the generation derived: a different plan fails. Outputs
  ``plan_sha256`` and the JSON arrays ``targets`` and ``lanes``.
* ``worker-stage --candidate DIR [--gradle-seed DIR] [--bundle]`` gives the candidate its own copy
  of the tested commit from the runner's checkout ``DIR`` (which must be detached at that commit
  and hold nothing else), with the verified kit as ``out/mod-base-kit`` and, when given, a Gradle
  cache seed. ``--bundle`` (lane jobs) also places the complete Build of ``sealed-build/`` where
  the protected config says the checkout expects it. Nothing of the candidate runs.
* ``worker-run --hook policy|build_target|run_lane [--unit ID]`` runs the one candidate hook of the
  job as the candidate: for a target or a lane ``--unit`` names it and ``policy`` takes none. A
  lane first gets its runtime values from the protected ``derive_runtime`` hook. The command exits
  non-zero, with one error line, when the hook failed, timed out or left a process behind; the
  result is recorded in the state either way.
* ``worker-seal`` (``if: always()`` once the accounts exist) terminates and locks the candidate
  again. After a successful hook it has root prove the tracked sources unchanged and freeze the
  export with its envelope: a target into ``sealed-build/``, a lane into ``sealed-runtime/``,
  ``policy`` nothing. After a hook that the state records as failed it seals nothing and exits
  zero: the step that ran the hook has already failed the job. Without a record of the run it
  exits non-zero, because nothing shows that an earlier step failed.
* ``worker-validate --hook verify_target|verify_build|verify_runtime [--unit ID] --output DIR``
  runs the verification of the job's sealed export as the validator and writes ``DIR``, the
  directory the job uploads next: the export with its envelope, the validation record and the
  reports. ``--unit`` is the target or the lane; ``verify_build`` takes none. ``DIR`` must not
  exist. It reads nothing from the API.
* ``worker-finish`` (``if: always()``) leaves both accounts terminated and locked and gives the
  runner home its mode back. It reads nothing but ``--state`` and the host, so it also works when
  the mod checkout or an earlier step failed.

A hook's output is printed with every line prefixed and neutralised (``worker.render_worker_log``).
"""

from __future__ import annotations

import argparse
import sys

from mod_base import cli, runtime
from mod_base.build_ci import adapter, commands, lifecycle, planning
from mod_base.build_ci.host import _canonical_path
from mod_base.errors import MbError
from mod_base.model import grammar, limits


def _tool_path(value: str) -> str:
    return str(_canonical_path(value))


def _sha256(value: str) -> str:
    return grammar.require(grammar.SHA256, value, "SHA-256")


def _unit_id(value: str) -> str:
    return grammar.require(grammar.CI_UNIT_ID, value, "unit id")


TOOL_PATH = cli.typed(_tool_path, "absolute tool path")
PLAN_SHA256 = cli.typed(_sha256, "plan SHA-256")
UNIT_ID = cli.typed(_unit_id, "target or lane id")


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    prepare = verbs.add_parser("worker-prepare", help="fence the host and allocate the job's disposable accounts")
    commands.add_job_arguments(prepare)
    prepare.add_argument("--roles", choices=tuple(lifecycle.ROLE_SETS), required=True,
                         help="the accounts this job needs")
    prepare.add_argument("--python", type=TOOL_PATH, required=True, metavar="PATH",
                         help="the interpreter hooks run with, as <prefix>/bin/<name>")
    prepare.add_argument("--java-home", type=TOOL_PATH, action="append", default=[], metavar="PATH",
                         help="a JDK the job installed (repeatable; the first is a hook's JAVA_HOME)")
    prepare.set_defaults(handler=run_worker_prepare)

    plan = verbs.add_parser("plan", help="derive the plan as the validator and write it to the state")
    commands.add_job_arguments(plan)
    plan.add_argument("--candidate", type=cli.PATH, default=None, metavar="DIR",
                      help="the candidate checkout; without it the candidate files are read from the API")
    plan.add_argument("--expect-sha256", type=PLAN_SHA256, default=None, metavar="HEX",
                      help="fail unless the derived plan has this hash")
    plan.add_argument("--github-output", type=cli.PATH, default=None, metavar="F")
    plan.set_defaults(handler=run_plan)

    stage = verbs.add_parser("worker-stage", help="stage the tested commit, the kit and a lane's Build for the candidate")
    commands.add_job_arguments(stage)
    stage.add_argument("--candidate", type=cli.PATH, required=True, metavar="DIR",
                       help="the runner's checkout of the tested commit")
    stage.add_argument("--gradle-seed", type=cli.PATH, default=None, metavar="DIR",
                       help="a restored Gradle cache (caches/ and wrapper/) the candidate's own is seeded from")
    stage.add_argument("--bundle", action="store_true",
                       help="also place the complete Build of sealed-build/ in the checkout (lane jobs)")
    stage.set_defaults(handler=run_worker_stage)

    run = verbs.add_parser("worker-run", help="run the one candidate hook of the job and record how it ended")
    commands.add_job_arguments(run)
    run.add_argument("--hook", choices=tuple(lifecycle.HOOK_PRODUCERS), required=True, help="the candidate hook")
    run.add_argument("--unit", type=UNIT_ID, default=None, metavar="ID",
                     help="the target of build_target or the lane of run_lane")
    run.set_defaults(handler=run_worker_run)

    seal = verbs.add_parser("worker-seal", help="lock the candidate and freeze its export (if: always())")
    commands.add_job_arguments(seal)
    seal.set_defaults(handler=run_worker_seal)

    validate = verbs.add_parser("worker-validate",
                                help="verify the sealed export as the validator and write the upload directory")
    commands.add_job_arguments(validate)
    validate.add_argument("--hook", choices=tuple(lifecycle.VALIDATION_HOOKS), required=True,
                          help="the verification this job ends with")
    validate.add_argument("--unit", type=UNIT_ID, default=None, metavar="ID",
                          help="the target or lane to verify (none for verify_build)")
    validate.add_argument("--output", type=cli.PATH, required=True, metavar="DIR",
                          help="the new directory the job uploads; it must not exist")
    validate.set_defaults(handler=run_worker_validate)

    finish = verbs.add_parser("worker-finish", help="lock both accounts and restore the runner home (if: always())")
    commands.add_job_arguments(finish)
    finish.set_defaults(handler=run_worker_finish)


def run_worker_prepare(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.prepare_worker(invocation, job, roles=lifecycle.ROLE_SETS[args.roles], python=args.python,
                                      java_homes=tuple(args.java_home))
    sys.stdout.write(f"worker-prepare: {' and '.join(worker.accounts)} allocated and locked; "
                     f"{worker.tools.entries} tool entries admitted under {len(worker.tools.roots)} roots\n")
    return 0


def run_plan(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.open_worker(job)
    if args.candidate is None:
        sources = lifecycle.api_candidate_files(
            commands.api_client(invocation, max_requests=limits.MAX_CI_PLAN_REQUESTS), job)
    else:
        sources = lifecycle.checkout_candidate_files(args.candidate, job)
    plan = lifecycle.derive_plan(job, worker, sources, expected_sha256=args.expect_sha256, log=sys.stdout.write)
    cli.write_github_output(args.github_output, planning.plan_outputs(plan))
    sys.stdout.write(f"plan: {plan['plan_sha256']} with {len(plan['targets'])} targets and "
                     f"{len(plan['lanes'])} lanes\n")
    return 0


def run_worker_stage(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.open_worker(job)
    stage = lifecycle.stage_candidate(job, worker, lifecycle.read_plan(job), args.candidate,
                                      gradle_seed=args.gradle_seed, bundle=args.bundle)
    extras = (["a Gradle seed"] if stage["gradle_seed"] else []) + (
        [] if stage["bundle"] is None else [f"the Build of {stage['bundle']['files']} files at {stage['bundle']['path']}"])
    sys.stdout.write(f"worker-stage: {stage['source']['files']} tracked files of {stage['tested_sha']} and kit "
                     f"{stage['kit']['version']} staged for the candidate"
                     + "".join(f", with {extra}" for extra in extras) + "\n")
    return 0


def _hook_label(run: dict[str, object]) -> str:
    return str(run["hook"]) if run["unit_id"] is None else f"{run['hook']} {run['unit_id']}"


def run_worker_run(args: argparse.Namespace) -> int:
    unit = adapter.HOOKS[args.hook].unit
    if (unit is None) != (args.unit is None):
        raise MbError(f"mod_base ci worker-run: --hook {args.hook} "
                      + ("takes no --unit" if unit is None else f"requires --unit, the {unit} it runs for"),
                      reason="usage")
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.open_worker(job)
    run = lifecycle.run_candidate_hook(job, worker, lifecycle.read_plan(job), args.hook, unit_id=args.unit,
                                       log=sys.stdout.write)
    sys.stdout.write(f"worker-run: {_hook_label(run)} succeeded; candidate terminated and locked\n")
    return 0


def run_worker_seal(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    run, seal = lifecycle.seal_worker(invocation, args.state)
    if seal is None:
        sys.stdout.write(f"worker-seal: candidate terminated and locked; nothing sealed, because {_hook_label(run)} "
                         f"did not succeed ({run['error']})\n")
    elif seal["export"] is None:
        sys.stdout.write(f"worker-seal: candidate terminated and locked; tracked sources unchanged after "
                         f"{_hook_label(run)}; nothing to export\n")
    else:
        sys.stdout.write(f"worker-seal: candidate terminated and locked; tracked sources unchanged; "
                         f"{seal['files']} files of {_hook_label(run)} sealed in sealed-{seal['export']}/ with "
                         f"envelope {seal['envelope_sha256']}\n")
    return 0


def run_worker_validate(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    worker = lifecycle.open_worker(job)
    sealed = lifecycle.validate_export(job, worker, hook=args.hook, unit_id=args.unit, output=args.output,
                                       log=sys.stdout.write)
    reports = len(sealed["validation"]["reports"])
    sys.stdout.write(f"worker-validate: {args.hook}{'' if args.unit is None else ' ' + args.unit} verified "
                     f"{len(sealed['envelope']['files'])} sealed files; {reports} "
                     f"{'report' if reports == 1 else 'reports'} sealed; upload directory written\n")
    return 0


def run_worker_finish(args: argparse.Namespace) -> int:
    report = lifecycle.finish_worker(args.state)
    accounts = ", ".join(f"{role} {state}" for role, state in report["accounts"].items())
    home = ("runner home untouched by this job" if report["home_mode"] is None
            else f"runner home mode {report['home_mode']:04o} restored")
    sys.stdout.write(f"worker-finish: {accounts}; no process left; {home}\n")
    return 0
