"""Closed private request of one root operation; data only, never execution authority (MB11).

One document kind carries every operation of ``grammar.CI_ROOT_OPERATIONS``. Its ``operation``
names fixed protected code and its ``arguments`` are a closed object per operation: no field holds
a program, a hook or a destination path. Validation proves shape and internal consistency only;
physical admission of the host, the accounts, the sources and the frozen inputs happens in root.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import check_output_paths, repo_path, validate_plan
from mod_base.build_ci.records import bind_build_envelope, validate_build_envelope
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Const, Int, List, Obj, Str, check, fail


_SHA = Str(grammar.SHA256, max_len=64)
_OID = Str(grammar.SHA1, max_len=40)
_UNIT = Str(grammar.CI_UNIT_ID, max_len=80)
_RUN = Int(1, limits.MAX_RUN_ID)
_ATTEMPT = Int(1, limits.MAX_RUN_ATTEMPT)
_FILE_ID = Int(0, limits.MAX_CI_FILE_ID)
_UNIX_ID = Int(1, limits.MAX_CI_UNIX_ID)
_WORKER_ID = Int(limits.MIN_CI_WORKER_UID, limits.MAX_CI_UNIX_ID)
_VERSIONS = readable_schema_versions("mod-base.ci.root-request")
_FILE = Obj({"path": repo_path, "mode": Str(choices=("100644", "100755")),
             "git_blob": _OID, "sha256": _SHA, "size": Int(0, limits.MAX_CI_ADAPTER_FILE_BYTES)})
_BOUNDARY = Obj({"home": Const("/home/runner"), "uid": _UNIX_ID, "gid": _UNIX_ID,
                 "device": _FILE_ID, "inode": Int(1, limits.MAX_CI_FILE_ID),
                 "original_mode": Int(0, 0o7777)})
_ACCOUNT = Obj({"uid": _WORKER_ID, "gid": _WORKER_ID})
_SOURCES = Obj({"controller_sha": _OID, "controller_tree": _OID, "config": _FILE,
                "files": List(_FILE, min_items=3, max_items=limits.MAX_CI_ADAPTER_FILES,
                              unique_by=lambda file: file["path"])})


def _plan(value: Any, path: str) -> dict[str, Any]:
    validate_plan(value, path=path)
    check(len(canonical_json(value)) <= limits.MAX_CI_PLAN_BYTES, path, "root plan exceeds existing cap")
    return value


def _envelope(value: Any, path: str) -> dict[str, Any]:
    validate_build_envelope(value, path=path)
    check(len(canonical_json(value)) <= limits.MAX_CI_ENVELOPE_BYTES, path, "root envelope exceeds existing cap")
    return value


def _runtime(value: Any, path: str) -> dict[str, Any]:
    validate_runtime_envelope(value, path=path)
    check(len(canonical_json(value)) <= limits.MAX_CI_RUNTIME_ENVELOPE_BYTES,
          path, "root runtime envelope exceeds existing cap")
    return value


def _sources(arguments: dict[str, Any], path: str) -> None:
    """Closed controller-source metadata; the bytes are re-read from the protected copy in root."""
    plan, sources = arguments["plan"], arguments["sources"]
    check(sources["controller_sha"] == plan["identity"]["controller_sha"], path, "root controller differs")
    config = sources["config"]
    check(config["path"] == "scripts/ci/mod-base-build.json" and config["size"] <= limits.MAX_CI_CONFIG_BYTES,
          f"{path}.sources.config", "root source config differs or exceeds its existing cap")
    files = [config, *sources["files"]]
    check_output_paths([file["path"] for file in files], f"{path}.sources")
    check(all(file["path"].split("/")[0].casefold() != ".git" for file in files), path,
          "root source metadata cannot include Git internals")
    check(sum(file["size"] for file in files) <= limits.MAX_CI_ADAPTER_TREE_BYTES,
          f"{path}.sources", "root source set exceeds its existing whole-byte cap")


def _build_validation(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    envelope = arguments["envelope"]
    validate_build_envelope(envelope, plan=arguments["plan"], path=f"{path}.envelope")
    check((arguments["run_id"], arguments["run_attempt"]) ==
          (envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]), path, "root producer differs")
    _sources(arguments, path)
    check(document["nonce"] != arguments["execution_nonce"], path, "root request needs a separate entry nonce")


def _runtime_validation(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    plan, build, runtime = arguments["plan"], arguments["build"], arguments["runtime"]
    validate_runtime_envelope(runtime, plan=plan, path=f"{path}.runtime")
    check(build["scope"] == "complete", f"{path}.build", "root runtime needs its complete owning Build")
    bind_build_envelope(build, descriptor=runtime["owning_build"], plan=plan)
    check(runtime["scope"] == "lane" and runtime["lane_id"] == arguments["lane_id"],
          f"{path}.lane_id", "root runtime requires the exact enrolled lane")
    check((arguments["run_id"], arguments["run_attempt"]) ==
          (runtime["producer"]["run_id"], runtime["producer"]["run_attempt"]),
          path, "root runtime producing attempt differs")
    _sources(arguments, path)
    check(document["nonce"] != arguments["execution_nonce"], path, "root request needs a separate entry nonce")


#: Operation -> (closed argument object, cross-field checks over the whole request).
_OPERATIONS = {
    "freeze-build-validation": (
        Obj({"validator": _ACCOUNT, "sources": _SOURCES, "plan": _plan, "envelope": _envelope,
             "run_id": _RUN, "run_attempt": _ATTEMPT, "execution_nonce": _SHA}), _build_validation),
    "freeze-runtime-validation": (
        Obj({"validator": _ACCOUNT, "sources": _SOURCES, "plan": _plan, "build": _envelope,
             "runtime": _runtime, "lane_id": _UNIT, "run_id": _RUN, "run_attempt": _ATTEMPT,
             "execution_nonce": _SHA}), _runtime_validation),
}


def _arguments(value: Any, path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise fail(path, "must be an object")
    return value


_REQUEST = Obj({"kind": Const("mod-base.ci.root-request"),
                "schema_version": Int(min(_VERSIONS), max(_VERSIONS)),
                "operation": Str(choices=grammar.CI_ROOT_OPERATIONS, max_len=40), "nonce": _SHA,
                "boundary": _BOUNDARY, "arguments": _arguments})


def validate_root_request(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Validate data only; the host, accounts, sources and inputs still need physical admission."""
    _REQUEST(document, path)
    arguments, consistent = _OPERATIONS[document["operation"]]
    arguments(document["arguments"], f"{path}.arguments")
    runner = document["boundary"]
    for role in ("candidate", "validator"):
        account = document["arguments"].get(role)
        check(account is None or (runner["uid"] != account["uid"] and runner["gid"] != account["gid"]),
              f"{path}.arguments.{role}", "root request account is not isolated from the runner")
    consistent(document, f"{path}.arguments")
    check(len(canonical_json(document)) <= limits.MAX_CI_ROOT_REQUEST_BYTES, path, "root request exceeds local cap")
    return document
