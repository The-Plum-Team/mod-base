"""Fixed privileged sealing launch after independent caller/interpreter approval (MB11).

Proof constructors are data, not approval. The old protected caller must admit its own code,
the installer and complete interpreter/import/system closure before calling this module.
It never installs a runtime, executes a domain hook or authorizes a native/App success.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci.bootstrap_installation import (BootstrapInstallation, PRIVILEGED_BOOTSTRAP_ROOT,
                                                       authenticate_privileged_bootstrap)
from mod_base.build_ci.host import HostBoundary, _open_directory, authenticate_privileged_host_boundary
from mod_base.build_ci.inputs import _accounts
from mod_base.build_ci.installation import KitInstallation, PRIVILEGED_KIT_ROOT
from mod_base.build_ci.python_installation import PYTHON_INSTALL_ROOT, PythonInstallation, _PROFILES
from mod_base.build_ci.python_setup import authenticate_privileged_python_installation
from mod_base.build_ci.root_request import build_root_freeze_invocation, read_root_freeze_request
from mod_base.build_ci.runtime_inputs import _context as _runtime_context
from mod_base.build_ci.runtime_root_request import (RuntimeRootFreezeContext, _signature as _runtime_signature,
                                                  read_runtime_root_freeze_request)
from mod_base.build_ci.toolchain import (ToolBytesProof, _execution_tool_paths, authenticate_toolchain_bytes)
from mod_base.build_ci.validation import SEALED_VALIDATION_ROOT, verify_validation_export
from mod_base.build_ci.worker import WorkerError, _control, authenticate_worker_account, terminate_worker
from mod_base.io.tree import authenticate_tree_private_access
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import check
from mod_base.runtime import Invocation


PRIVILEGED_FREEZE_FLAGS = ("--kit-sha", "--kit-version", "--kit-digest", "--repository", "--controller-sha",
    "--controller-root", "--runner-uid", "--runner-gid", "--home-device", "--home-inode", "--home-original-mode", "--nonce")
PRIVILEGED_RUNTIME_FREEZE_FLAGS = ("--operation", *PRIVILEGED_FREEZE_FLAGS)


def _sealed_identity() -> tuple[int, int]:
    descriptor = _open_directory(tuple(SEALED_VALIDATION_ROOT.parts[1:]))
    try:
        info = os.fstat(descriptor)
        return info.st_dev, info.st_ino
    finally:
        os.close(descriptor)


def _runtime_matches(context: RuntimeRootFreezeContext, original: tuple[Any, ...]) -> bool:
    return type(context) is RuntimeRootFreezeContext and _runtime_signature(context) == original


def execute_privileged_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                                     installation: KitInstallation, program: BootstrapInstallation,
                                     tools: ToolBytesProof, python: str, nonce: str) -> dict[str, Any]:
    """Root-only fixed process launch with retained admitted context and independent receipt read.

    Caller retains genuine installation/program/tool approval and complete runtime closure.
    A successful process exit alone never admits the sealed receipt or native semantics.
    """
    return _execute_freeze(invocation, boundary=boundary, installation=installation, program=program,
                           tools=tools, python=python, nonce=nonce, python_admission=None)


def execute_installed_python_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                                          installation: KitInstallation, program: BootstrapInstallation,
                                          tools: ToolBytesProof, python: PythonInstallation, nonce: str) -> dict[str, Any]:
    """Use the fixed copied SDK only with both source-derived and complete tool byte admission.

    Independent original caller/runtime enrollment and reviewed complete system closure remain
    prerequisites. No constructed receipt or observed tool hash authorizes execution.
    """

    if type(python) is not PythonInstallation or type(python.version) is not str or python.version not in _PROFILES:
        raise WorkerError("installed Python launch requires a supported installation receipt")
    executable = str(PYTHON_INSTALL_ROOT / python.version / "x64" / "bin" / f"python{python.version.rsplit('.', 1)[0]}")
    return _execute_freeze(invocation, boundary=boundary, installation=installation, program=program,
        tools=tools, python=executable, nonce=nonce,
        python_admission=lambda: authenticate_privileged_python_installation(python, boundary=boundary,
                                                                            installation=installation))


def execute_privileged_runtime_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                                             installation: KitInstallation, program: BootstrapInstallation,
                                             tools: ToolBytesProof, python: str, nonce: str) -> dict[str, Any]:
    """Root-only explicitly versioned runtime process, with independent original receipt admission.

    Caller retains genuinely admitted original program/tools and complete interpreter/system/
    source/execution provenance. Constructible proof objects or successful exit confer no authority.
    """
    return _execute_freeze(invocation, boundary=boundary, installation=installation, program=program,
                          tools=tools, python=python, nonce=nonce, python_admission=None, runtime=True)


def execute_installed_python_runtime_freeze_request(invocation: Invocation, *, boundary: HostBoundary,
                                                  installation: KitInstallation, program: BootstrapInstallation,
                                                  tools: ToolBytesProof, python: PythonInstallation,
                                                  nonce: str) -> dict[str, Any]:
    """Use the original source-admitted fixed SDK and independently approved complete tool closure."""
    if type(python) is not PythonInstallation or type(python.version) is not str or python.version not in _PROFILES:
        raise WorkerError("installed runtime Python launch requires a supported installation receipt")
    executable = str(PYTHON_INSTALL_ROOT / python.version / "x64" / "bin" / f"python{python.version.rsplit('.', 1)[0]}")
    return _execute_freeze(invocation, boundary=boundary, installation=installation, program=program,
        tools=tools, python=executable, nonce=nonce, runtime=True,
        python_admission=lambda: authenticate_privileged_python_installation(python, boundary=boundary,
                                                                            installation=installation))


def _execute_freeze(invocation: Invocation, *, boundary: HostBoundary,
                    installation: KitInstallation, program: BootstrapInstallation,
                    tools: ToolBytesProof, python: str, nonce: str,
                    python_admission: Callable[[], str] | None, runtime: bool = False) -> dict[str, Any]:
    authenticate_privileged_host_boundary(boundary)
    validator = authenticate_worker_account("validator")
    _accounts(boundary, validator)
    try:
        check(type(invocation) is Invocation and type(tools) is ToolBytesProof,
              "$.launch", "requires genuine invocation and retained approved tool byte proof")
        grammar.require(grammar.SHA256, nonce, "root request nonce")
        authenticate_privileged_bootstrap(program, boundary=boundary, installation=installation)
        check(invocation.kit["sha"] == installation.kit_sha and invocation.kit["version"] == installation.kit_version,
              "$.launch", "executing kit identity differs")

        def tools_match() -> None:
            if python_admission is not None:
                check(python_admission() == python, "$.launch", "source-admitted Python executable path differs")
            check(authenticate_toolchain_bytes(tools.tools, boundary=boundary,
                    expected_digest=tools.digest, privileged=True) == tools,
                  "$.launch", "approved tool byte proof changed")
            _execution_tool_paths(tools.tools, boundary, python, None)

        tools_match()
        composed = build_root_freeze_invocation(boundary=boundary, controller_root=invocation.repo_root.as_posix(),
            repository=invocation.repository, controller_sha=invocation.implementation_sha, kit_sha=installation.kit_sha)
        check(composed.config.sha256 == invocation.config.sha256, "$.launch", "controller configuration changed")
        read_request = read_runtime_root_freeze_request if runtime else read_root_freeze_request
        context = read_request(composed, boundary=boundary, validator=validator, nonce=nonce)
        if runtime:
            check(type(context) is RuntimeRootFreezeContext, "$.launch", "runtime launch requires its original typed context")
            original = _runtime_signature(context)
            input_digest, _ = _runtime_context(context.plan, context.build, context.runtime,
                lane_id=context.lane_id, run_id=context.run_id, run_attempt=context.run_attempt)
        check(context.plan["identity"]["kit"]["tree_digest"] == installation.digest,
              "$.launch", "request names another installed kit digest")
        values = (installation.kit_sha, installation.kit_version, installation.digest, composed.repository,
            composed.implementation_sha, composed.repo_root.as_posix(), str(boundary.uid), str(boundary.gid),
            str(boundary.device), str(boundary.inode), str(boundary.original_mode), nonce)
        flags = PRIVILEGED_FREEZE_FLAGS
        if runtime:
            flags = PRIVILEGED_RUNTIME_FREEZE_FLAGS
            values = (grammar.CI_RUNTIME_FREEZE_OPERATION, *values)
        arguments = tuple(item for pair in zip(flags, values) for item in pair)
        command = (python, "-I", "-B", "-S",
                   str(PRIVILEGED_BOOTSTRAP_ROOT / grammar.CI_BOOTSTRAP_PROGRAM_NAME), *arguments)
        authenticate_privileged_bootstrap(program, boundary=boundary, installation=installation)
        tools_match()
        check(_control(command, timeout=limits.CI_PRIVILEGED_ENTRY_TIMEOUT_SECONDS,
              accepted=frozenset({0}), cwd=Path(str(PRIVILEGED_KIT_ROOT))) == b"",
              "$.launch", "fixed successful entry must have no stdout")
        authenticate_privileged_bootstrap(program, boundary=boundary, installation=installation)
        tools_match()
        closing = read_request(composed, boundary=boundary, validator=validator, nonce=nonce)
        check((_runtime_matches(closing, original) and _runtime_matches(context, original)) if runtime
              else closing == context, "$.launch", "root request changed during process sealing")
        root = Path(str(SEALED_VALIDATION_ROOT))
        initial = _sealed_identity()
        authenticate_tree_private_access(root, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                          max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        receipt = verify_validation_export(root, plan=context.plan,
            hook="verify_runtime" if runtime else ("verify_target" if context.envelope["scope"] == "target" else "verify_build"),
            unit_id=context.lane_id if runtime else context.envelope["target_id"], run_id=context.run_id, run_attempt=context.run_attempt,
            source_config_sha256=context.sources.config.sha256,
            input_sha256=input_digest if runtime else hashlib.sha256(canonical_json(context.envelope)).hexdigest())
        if runtime:
            closing = read_request(composed, boundary=boundary, validator=validator, nonce=nonce)
            check(_runtime_matches(closing, original) and _runtime_matches(context, original),
                  "$.launch", "original runtime request changed during parent receipt admission")
            authenticate_privileged_bootstrap(program, boundary=boundary, installation=installation)
            tools_match()
            check(verify_validation_export(root, plan=context.plan, hook="verify_runtime", unit_id=context.lane_id,
                run_id=context.run_id, run_attempt=context.run_attempt, source_config_sha256=context.sources.config.sha256,
                input_sha256=input_digest) == receipt, "$.launch", "original runtime receipt changed during closing admission")
            check(_runtime_signature(context) == original, "$.launch", "original runtime context changed during closing reads")
        authenticate_tree_private_access(root, owner_uid=boundary.uid, owner_gid=boundary.gid,
                                          max_entries=limits.MAX_CI_EXPORT_ENTRIES)
        check(_sealed_identity() == initial, "$.launch", "sealed receipt root changed during parent admission")
        authenticate_privileged_host_boundary(boundary)
        return receipt
    except OSError as error:
        raise WorkerError("cannot execute fixed privileged sealing entry") from error
    finally:
        terminate_worker(validator)
