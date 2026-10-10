"""``template check|sync|init|activation|transition`` (MB9). Flags are frozen by SPEC §2.2; exit 2
on drift.

``activation`` and ``transition`` (v1.1.0) are the operator's view of the shared Build/E2E adoption
(``docs/OPERATIONS.md``, "Build/E2E activation and rollback"). ``activation`` validates the mod's
activation manifest against its Build configuration and prints its state, the callers that state
manages and the states it may change to. ``transition`` admits the change from a checkout of the
protected base (``--base``) to the candidate (``--repo``): the manifest changes along an allowed
transition at an unchanged pin, and the candidate's callers are the rendered templates of the
executing kit, which must therefore be the kit the candidate pins.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mod_base import cli, runtime
from mod_base.build_ci.activation import activation_state, managed_callers, next_states
from mod_base.build_ci.transition import admit_transition, verify_candidate_callers
from mod_base.errors import MbError, single_line
from mod_base.model.canonical import read_regular_file
from mod_base.pin import parse_pin
from mod_base.template import tool


def register(subparsers: argparse._SubParsersAction) -> None:
    template = subparsers.add_parser("template", help="repository-configuration template")
    verbs = template.add_subparsers(dest="template_command", metavar="VERB", required=True)
    check = verbs.add_parser("check", help="report drift (exit 2 on drift)")
    check.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    check.set_defaults(handler=run_check)
    sync = verbs.add_parser("sync", help="rewrite managed files (dry run without --write)")
    sync.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    sync.add_argument("--write", action="store_true")
    sync.set_defaults(handler=run_sync)
    init = verbs.add_parser("init", help="seed a new repository (never overwrites)")
    init.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    init.add_argument("--seed", action="store_true")
    init.add_argument("--from-config", type=cli.PATH, metavar="FILE")
    init.set_defaults(handler=run_init)
    activation = verbs.add_parser("activation", help="validate and print the Build/E2E activation state")
    activation.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR")
    activation.set_defaults(handler=run_activation)
    transition = verbs.add_parser("transition", help="admit an activation change against the protected base")
    transition.add_argument("--repo", type=cli.PATH, required=True, metavar="DIR", help="the candidate checkout")
    transition.add_argument("--base", type=cli.PATH, required=True, metavar="DIR", help="the protected base checkout")
    transition.set_defaults(handler=run_transition)


def _report(drifts: list[tool.Drift]) -> int:
    for drift in drifts:
        sys.stdout.write(f"{drift.kind}: {drift.path}\n{drift.detail}\n")
    if drifts:
        raise MbError(f"{len(drifts)} template drift(s): {single_line(', '.join(d.path for d in drifts))}",
                      reason="template-drift")
    return 0


def run_check(args: argparse.Namespace) -> int:
    drifts, pending = tool.evaluate(args.repo, kit_root=runtime.kit_root())
    for drift in pending:
        sys.stdout.write(f"pending (template.deferred): {drift.kind}: {drift.path}\n{drift.detail}\n")
    return _report(drifts)


def run_sync(args: argparse.Namespace) -> int:
    drifts = tool.sync(args.repo, kit_root=runtime.kit_root(), write=args.write)
    if not args.write:
        return _report(drifts)
    for drift in drifts:
        sys.stdout.write(f"wrote {drift.path}\n")
    return 0


def run_init(args: argparse.Namespace) -> int:
    for path in tool.init(args.repo, kit_root=runtime.kit_root(), seed=args.seed, from_config=args.from_config):
        sys.stdout.write(f"created {path}\n")
        data = read_regular_file(Path(args.repo) / path, label=path, max_bytes=tool.MAX_FILE_BYTES, allow_empty=True)
        remaining = tool.unresolved_placeholders(data)
        if remaining:
            sys.stdout.write(f"  fill in by hand: {', '.join('{{' + name + '}}' for name in remaining)}\n")
    return 0


def run_activation(args: argparse.Namespace) -> int:
    """Print ``state``, ``repository``, ``profile``, ``rollback_from`` and one ``managed`` line per
    managed caller and one ``next`` line per state the mod may change to."""

    document = tool.load_template_activation(args.repo)
    lines = [f"state: {activation_state(document)}"]
    if document is not None:
        lines.extend(f"{name}: {document[name]}" for name in ("repository", "profile"))
        if document["rollback_from"] is not None:
            lines.append(f"rollback_from: {document['rollback_from']}")
    lines.extend(f"managed: {path}" for path in managed_callers(document))
    lines.extend(f"next: {state}" for state in next_states(document))
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


def run_transition(args: argparse.Namespace) -> int:
    """Admit the change from ``--base`` to ``--repo`` and print it; exit 2 when it is refused."""

    candidate, candidate_pin = tool.activation_bytes(args.repo), parse_pin(args.repo)
    executing = cli.environ().get("MOD_BASE_KIT_SHA", "")
    if executing and executing != candidate_pin.sha:
        raise MbError("template transition compares the candidate's callers with the templates of the executing "
                      f"kit, so it must run from the kit the candidate pins ({candidate_pin.sha}), not "
                      f"{single_line(executing, limit=60)}", reason="activation")
    admitted = admit_transition(tool.activation_bytes(args.base), candidate, protected_pin=parse_pin(args.base),
                                candidate_pin=candidate_pin)
    managed = verify_candidate_callers(tool.caller_files(args.repo), candidate=candidate, pin=candidate_pin,
                                       kit_root=runtime.kit_root(), branch=tool.canonical_branch(args.repo))
    change = f"{admitted.previous} -> {admitted.current}" if admitted.changed else f"{admitted.current} (unchanged)"
    sys.stdout.write(f"transition: {change}\npin: {candidate_pin.sha} {candidate_pin.version}\n"
                     + "".join(f"managed: {path}\n" for path in managed))
    return 0
