"""The closed set of root operations behind the private request channel (MB11).

``execute_root_operation`` is the only kit entry of ``tools/ci_privileged_bootstrap.py``. It binds
the importing kit to the checkout and digest the bootstrap verified, admits the runner's request
and the live host fence, runs the one fixed operation the request names and re-admits the request
afterwards. Operations reconstruct their inputs from the request's closed data and from protected
copies on disk; nothing in a request selects code, and no operation calls the GitHub API.
``stage-candidate`` is the one operation that reads directories a request names: the runner's own
checkout, kit overlay and Gradle seed, each below the fenced runner home.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mod_base
from mod_base.build_ci.controller import (CONTROLLER_VALIDATION_ROOT, ControllerSources,
                                          prepare_controller_validation)
from mod_base.build_ci.exports import BUILD_VALIDATION_ROOT, prepare_build_validation
from mod_base.build_ci.handoff import _context, freeze_handed_off_build_validation
from mod_base.build_ci.host import (HostBoundary, _canonical_path, authenticate_privileged_host_boundary,
                                    fence_worker_host)
from mod_base.build_ci.inputs import (_accounts, _inspect_inputs, prepare_plan_inputs, prepare_validation_plan,
                                      take_derived_plan)
from mod_base.build_ci.root_request import (_inspect_sources, _restore_sources, _source_root_identity,
                                          read_root_request)
from mod_base.build_ci.runtime_handoff import _context as _runtime_context, freeze_handed_off_runtime_validation
from mod_base.build_ci.runtime_inputs import _inspect_inputs as _inspect_runtime_inputs, prepare_runtime_validation
from mod_base.build_ci.source import GitSourceEntry
from mod_base.build_ci.worker import WorkerAccount, WorkerError, authenticate_worker_account, terminate_worker
from mod_base.build_ci.worker_preparation import prepare_privileged_worker_checkout
from mod_base.io.tree import authenticate_tree_read_access, read_child_file
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.pin import Pin, kit_tree_digest, verify_staged_files


@dataclass(frozen=True)
class _Kit:
    root: Path
    digest: str


def _authenticate_kit(kit_root: str, kit_digest: str) -> _Kit:
    """Bind the importing package to the verified checkout: same files, same digest, same locks."""
    grammar.require(grammar.DIGEST, kit_digest, "kit digest")
    root = Path(str(_canonical_path(kit_root)))
    check(Path(mod_base.__file__) == root / "src" / "mod_base" / "__init__.py",
          "$.kit", "root operation was not imported from the verified kit checkout")
    check(kit_tree_digest(root) == kit_digest, "$.kit", "kit checkout differs from the verified digest")
    verify_staged_files(root)
    return _Kit(root, kit_digest)


def _account(role: str, boundary: HostBoundary, arguments: dict[str, Any]) -> WorkerAccount:
    """The live fixed account must be the one the runner named; the peer account must exist too."""
    account = authenticate_worker_account(role)
    check({"uid": account.uid, "gid": account.gid} == arguments[role],
          f"$.request.{role}", "root request names another disposable account")
    _accounts(boundary, account if role == "validator" else authenticate_worker_account("validator"))
    return account


def _kit_binding(plan: dict[str, Any], kit: _Kit) -> None:
    bound = plan["identity"]["kit"]
    check(bound["tree_digest"] == kit.digest and bound["version"] == mod_base.__version__,
          "$.kit", "root request plan names another kit than the verified executing checkout")


def _job(boundary: HostBoundary, arguments: dict[str, Any]) -> WorkerAccount:
    """The live accounts must be the ones the runner named: the validator and, if the job has
    one, the candidate. A candidate the request does not name must not exist."""
    validator = authenticate_worker_account("validator")
    candidate = _accounts(boundary, validator)
    named = {"validator": {"uid": validator.uid, "gid": validator.gid},
             "candidate": None if candidate is None else {"uid": candidate.uid, "gid": candidate.gid}}
    check(named == {role: arguments[role] for role in named}, "$.request",
          "root request names other disposable accounts than the host has")
    return validator


def _grant_controller(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = _job(boundary, arguments)
    subject = arguments["subject"]
    _kit_binding({"identity": subject}, kit)
    # The copy is still the runner's private stage: the request carries hashes, the bytes are read here.
    sources = _restore_sources(arguments["sources"], subject)
    prepare_controller_validation(boundary=boundary, validator=validator, sources=sources, identity=subject)


def _grant_plan_inputs(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    prepare_plan_inputs(boundary=boundary, validator=_job(boundary, arguments),
                        digests={item["name"]: item["sha256"] for item in arguments["inputs"]})


def _take_derived_plan(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    take_derived_plan(boundary=boundary, validator=_job(boundary, arguments))


def _grant_validation_inputs(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = _job(boundary, arguments)
    _kit_binding(arguments["plan"], kit)
    prepare_validation_plan(boundary=boundary, validator=validator, plan=arguments["plan"])


def _grant_build_validation(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = _job(boundary, arguments)
    plan, envelope = arguments["plan"], arguments["envelope"]
    _kit_binding(plan, kit)
    # Nothing is granted for another export than the one named: its envelope is compared first,
    # and the handoff then verifies every file against that envelope before and after the grant.
    named = "sealed-build/ is not the export the runner named"
    check(read_child_file(BUILD_VALIDATION_ROOT, grammar.CI_ENVELOPE_NAME, max_bytes=limits.MAX_CI_ENVELOPE_BYTES)
          == canonical_json(envelope), "$.request.envelope", named)
    check(prepare_build_validation(boundary=boundary, validator=validator, plan=plan) == envelope,
          "$.request.envelope", named)


def _grant_runtime_validation(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = _job(boundary, arguments)
    _kit_binding(arguments["plan"], kit)
    prepare_runtime_validation(boundary=boundary, validator=validator, plan=arguments["plan"],
                               build=arguments["build"], runtime=arguments["runtime"],
                               lane_id=arguments["lane_id"], run_id=arguments["run_id"],
                               run_attempt=arguments["run_attempt"])


def _controller_sources(boundary: HostBoundary, validator: WorkerAccount, metadata: dict[str, Any],
                        plan: dict[str, Any]) -> tuple[ControllerSources, tuple[int, int]]:
    initial = _source_root_identity()
    authenticate_tree_read_access(CONTROLLER_VALIDATION_ROOT, owner_uid=boundary.uid,
                                  reader_gid=validator.gid, max_entries=limits.MAX_CI_SOURCE_ENTRIES)
    sources = _restore_sources(metadata, plan["identity"])
    check(_inspect_sources(boundary, validator, sources, plan) == initial,
          "$.sources", "controller copy root replaced during reconstruction")
    return sources, initial


def _host_fence(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    fence_worker_host(boundary=boundary)


def _stage_candidate(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    overlay, seed = arguments["overlay"], arguments["gradle_seed"]
    prepare_privileged_worker_checkout(
        Path(arguments["source"]), None if seed is None else Path(seed), Path(overlay["path"]),
        boundary=boundary, account=_account("candidate", boundary, arguments),
        repository=arguments["repository"], tested_commit=arguments["tested_sha"],
        tested_tree=arguments["tested_tree"],
        inventory=tuple(GitSourceEntry(**entry) for entry in arguments["inventory"]),
        pin=Pin(overlay["sha"], "v" + overlay["version"], ()), expected_digest=overlay["tree_digest"])


def _freeze_build_validation(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = authenticate_worker_account("validator")
    try:
        def admit() -> tuple[Any, ...]:
            check(_account("validator", boundary, arguments) == validator,
                  "$.request.validator", "validator identity changed during the root operation")
            plan, envelope = arguments["plan"], arguments["envelope"]
            _kit_binding(plan, kit)
            sources, initial = _controller_sources(boundary, validator, arguments["sources"], plan)
            inputs = _inspect_inputs(boundary, validator, plan, envelope)
            _context(sources, plan, envelope, arguments["run_id"], arguments["run_attempt"])
            check(_inspect_sources(boundary, validator, sources, plan) == initial
                  and _inspect_inputs(boundary, validator, plan, envelope) == inputs,
                  "$.request", "root context changed during physical admission")
            authenticate_privileged_host_boundary(boundary)
            return sources, initial, inputs

        context = admit()
        freeze_handed_off_build_validation(boundary=boundary, validator=validator, sources=context[0],
            plan=arguments["plan"], envelope=arguments["envelope"], run_id=arguments["run_id"],
            run_attempt=arguments["run_attempt"], nonce=arguments["execution_nonce"])
        check(admit() == context, "$.request", "root context changed during receipt sealing")
    finally:
        terminate_worker(validator)


def _freeze_runtime_validation(boundary: HostBoundary, arguments: dict[str, Any], kit: _Kit) -> None:
    validator = authenticate_worker_account("validator")
    try:
        plan, build, runtime = arguments["plan"], arguments["build"], arguments["runtime"]
        lane = {"lane_id": arguments["lane_id"], "run_id": arguments["run_id"],
                "run_attempt": arguments["run_attempt"]}
        original = tuple(canonical_json(value) for value in (plan, build, runtime))

        def admit() -> tuple[Any, ...]:
            check(_account("validator", boundary, arguments) == validator,
                  "$.request.validator", "validator identity changed during the root operation")
            _kit_binding(plan, kit)
            sources, initial = _controller_sources(boundary, validator, arguments["sources"], plan)
            inputs = _inspect_runtime_inputs(boundary, validator, plan, build, runtime)
            _runtime_context(sources, plan, build, runtime, **lane)
            check(_inspect_sources(boundary, validator, sources, plan) == initial
                  and _inspect_runtime_inputs(boundary, validator, plan, build, runtime) == inputs,
                  "$.request", "runtime root context changed during physical admission")
            authenticate_privileged_host_boundary(boundary)
            return sources, initial, inputs

        context = admit()
        freeze_handed_off_runtime_validation(boundary=boundary, validator=validator, sources=context[0],
            plan=plan, build=build, runtime=runtime, nonce=arguments["execution_nonce"], **lane)
        check(admit() == context
              and tuple(canonical_json(value) for value in (plan, build, runtime)) == original,
              "$.request", "runtime root context changed during receipt sealing")
    finally:
        terminate_worker(validator)


_OPERATIONS: dict[str, Callable[[HostBoundary, dict[str, Any], _Kit], None]] = {
    "host-fence": _host_fence,
    "stage-candidate": _stage_candidate,
    "freeze-build-validation": _freeze_build_validation,
    "freeze-runtime-validation": _freeze_runtime_validation,
    "grant-controller": _grant_controller,
    "grant-plan-inputs": _grant_plan_inputs,
    "take-derived-plan": _take_derived_plan,
    "grant-validation-inputs": _grant_validation_inputs,
    "grant-build-validation": _grant_build_validation,
    "grant-runtime-validation": _grant_runtime_validation,
}


def execute_root_operation(operation: str, *, kit_root: str, kit_digest: str, nonce: str) -> None:
    """Run one closed root operation for the runner's private request (the bootstrap's only entry).

    Requires real root on Linux. The kit must be the checkout the bootstrap verified. The request
    of exactly this operation and nonce is admitted together with the live host fence before the
    operation and re-read unchanged after it. Every rejection is an ``MbError``.
    """
    if sys.platform != "linux" or os.getuid() != 0 or os.geteuid() != 0 or os.getgid() != 0:
        raise WorkerError("root operations require protected Linux root")
    check(type(operation) is str and operation in _OPERATIONS, "$.operation", "unknown root operation")
    kit = _authenticate_kit(kit_root, kit_digest)
    boundary, arguments, raw = read_root_request(operation, nonce=nonce)
    _OPERATIONS[operation](boundary, arguments, kit)
    check(read_root_request(operation, nonce=nonce) == (boundary, arguments, raw),
          "$.request", "root request changed during its operation")
    authenticate_privileged_host_boundary(boundary)
