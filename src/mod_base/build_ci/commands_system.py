"""``ci system-profile``: install the kit system profile the protected Build config names.

One step of the packaged ``lane`` job, run by the runner after ``ci subject`` and before
``ci worker-prepare``: the image packages a lane runs with (Xvfb and Mesa for a Minecraft client)
are installed as root while no worker account exists, so the host fence and the tool admission that
follow cover them like the rest of the image. Only the lane job runs a client; the policy, target
and validation hooks need no display. The profile comes from the protected config of the verified
mod checkout (bound to the job like every verb, ``lifecycle.open_job``); a config that names none
makes the step a no-op that still refuses a host with worker accounts. No API request.
"""

from __future__ import annotations

import argparse
import sys
import time

from mod_base import cli, runtime
from mod_base.build_ci import commands, lifecycle
from mod_base.build_ci.config import system_profile
from mod_base.build_ci.system_profile import SUDO, install_system_profile


def add_verbs(verbs: argparse._SubParsersAction) -> None:
    parser = verbs.add_parser("system-profile",
                              help="install the system profile of the protected Build config before any worker account exists")
    commands.add_job_arguments(parser)
    parser.set_defaults(handler=run_system_profile)


def run_system_profile(args: argparse.Namespace) -> int:
    invocation = runtime.build_invocation(args.repo, args.config, cli.environ())
    job = lifecycle.open_job(invocation, args.state)
    name = system_profile(job.config.data)
    started = time.monotonic()
    packages = install_system_profile(name, log=sys.stdout.write, sudo=SUDO)
    if name is None:
        sys.stdout.write("system-profile: the protected Build config names no system profile; nothing installed\n")
    else:
        sys.stdout.write(f"system-profile: {name} installed, {packages} packages in "
                         f"{time.monotonic() - started:.0f}s; no worker account exists yet\n")
    return 0
