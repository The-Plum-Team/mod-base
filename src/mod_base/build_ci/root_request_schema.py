"""Closed private request of one root operation; data only, never execution authority (MB11).

One document kind carries every operation of ``grammar.CI_ROOT_OPERATIONS``. Its ``operation``
names fixed protected code and its ``arguments`` are a closed object per operation: no field holds
a program, a hook or a destination path. ``stage-candidate`` alone names directories, and only ones
root reads: the runner's own checkout, kit overlay and Gradle seed below its fenced home. The
operations that place or freeze candidate data take their paths from the protected controller copy
root re-reads, and state the successful execution they follow: a request after a failed hook cannot
be written.
Validation proves shape and internal consistency only; physical admission of the host, the
accounts, the sources and the frozen inputs happens in root.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci import adapter
from mod_base.build_ci.host import HOST_RUNNER_HOME, _canonical_path
from mod_base.build_ci.protocol import REPO, check_output_paths, repo_path, validate_plan, validate_subject
from mod_base.build_ci.records import (_PRODUCER_IDENTITY, _producer_binding, bind_build_envelope,
                                       validate_build_envelope, validate_descriptor)
from mod_base.build_ci.runtime_schema import validate_runtime_envelope
from mod_base.build_ci.source import GitSourceEntry, validate_source_inventory
from mod_base.build_ci.worker import WorkerError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Bool, Const, Int, List, Nullable, Obj, Str, check, fail


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
_TRACKED = Obj({"path": repo_path, "mode": Str(choices=("100644", "100755", "120000")),
                "size": Int(0, limits.MAX_CI_SOURCE_FILE_BYTES), "git_blob": _OID})


def _home_path(value: Any, path: str) -> str:
    """A directory root will read: canonical, absolute and strictly below the fenced runner home."""
    try:
        _canonical_path(value)
    except WorkerError:
        raise fail(path, "must be a canonical absolute POSIX path") from None
    check(value.startswith(HOST_RUNNER_HOME + "/"), path, "must be below the fenced runner home")
    return value


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
    sources = arguments["sources"]
    identity = arguments["plan"]["identity"] if "plan" in arguments else arguments["subject"]
    check(sources["controller_sha"] == identity["controller_sha"], path, "root controller differs")
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


def _runtime_lane(arguments: dict[str, Any], path: str) -> None:
    """One lane of this run attempt with the complete Build its envelope names as its owner."""
    plan, build, runtime = arguments["plan"], arguments["build"], arguments["runtime"]
    validate_runtime_envelope(runtime, plan=plan, path=f"{path}.runtime")
    check(build["scope"] == "complete", f"{path}.build", "root runtime needs its complete owning Build")
    bind_build_envelope(build, descriptor=runtime["owning_build"], plan=plan)
    check(runtime["scope"] == "lane" and runtime["lane_id"] == arguments["lane_id"],
          f"{path}.lane_id", "root runtime requires the exact enrolled lane")
    check((arguments["run_id"], arguments["run_attempt"]) ==
          (runtime["producer"]["run_id"], runtime["producer"]["run_attempt"]),
          path, "root runtime producing attempt differs")


def _runtime_validation(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    _runtime_lane(arguments, path)
    _sources(arguments, path)
    check(document["nonce"] != arguments["execution_nonce"], path, "root request needs a separate entry nonce")


def _candidate_staging(document: dict[str, Any], path: str) -> None:
    """The inventory is a complete one of the existing kind; root binds it to the tree and the bytes."""
    validate_source_inventory(tuple(GitSourceEntry(**entry) for entry in document["arguments"]["inventory"]))


#: The accounts of a job, as its lifecycle operations name them: the validator and the candidate,
#: which is ``null`` in a job that allocated the validator alone.
_JOB = {"validator": _ACCOUNT, "candidate": Nullable(_ACCOUNT)}
#: One staged candidate file of ``validation-input/``: its name there and the SHA-256 of its bytes.
_INPUT = Obj({"name": Str(grammar.CI_UNIT_ID, max_len=80), "sha256": _SHA})


def _job(document: dict[str, Any], path: str) -> None:
    validator, candidate = document["arguments"]["validator"], document["arguments"]["candidate"]
    check(candidate is None or (candidate["uid"] != validator["uid"] and candidate["gid"] != validator["gid"]),
          f"{path}.candidate", "root request accounts are not isolated from each other")


def _controller_grant(document: dict[str, Any], path: str) -> None:
    _job(document, path)
    _sources(document["arguments"], path)


def _plan_inputs(document: dict[str, Any], path: str) -> None:
    """The candidate files in the order a job stages them: the inventory, the scenario contract,
    then the extra plan inputs of the protected config by name."""
    _job(document, path)
    names = [item["name"] for item in document["arguments"]["inputs"]]
    extra = names[2:]
    check(names[:2] == [adapter.INVENTORY_INPUT, adapter.SCENARIO_INPUT] and extra == sorted(set(extra))
          and not set(extra) & adapter.RESERVED_INPUT_NAMES, f"{path}.inputs",
          "must be the inventory, the scenario contract and the extra plan inputs sorted by name")


#: The candidate operations of a job that allocated both accounts: neither may be absent.
_CANDIDATE_JOB = {"validator": _ACCOUNT, "candidate": _ACCOUNT}
#: What the runner retained of the candidate hook a freeze follows: only a zero exit is expressible.
_EXECUTION = Obj({"returncode": Const(0), "truncated": Bool(), "log_bytes": Int(0, limits.MAX_CI_LOG_BYTES),
                  "log_sha256": _SHA})
#: What every operation after a candidate hook names: the accounts, the protected sources root
#: re-reads its policy from, the plan, the tested tree's inventory and the execution it follows.
_CANDIDATE_SOURCE = {**_CANDIDATE_JOB, "sources": _SOURCES, "plan": _plan,
                     "inventory": List(_TRACKED, min_items=1, max_items=limits.MAX_CI_SOURCE_FILES),
                     "execution": _EXECUTION}


def _bundle_staging(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    _job(document, path)
    _sources(arguments, path)
    validate_build_envelope(arguments["envelope"], plan=arguments["plan"], path=f"{path}.envelope")
    check(arguments["envelope"]["scope"] == "complete", f"{path}.envelope", "a lane is staged the complete Build")


def _candidate_source(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    _job(document, path)
    _sources(arguments, path)
    _candidate_staging(document, path)


def _unit(arguments: dict[str, Any], kind: str, path: str) -> None:
    """The target or lane a freeze names is one of the plan, and its producer runs for the plan's subject."""
    key = f"{kind}_id"
    check(arguments[key] in [unit["id"] for unit in arguments["plan"][f"{kind}s"]], f"{path}.{key}",
          f"is not a {kind} of the protected plan")
    _producer_binding(arguments["producer"], arguments["plan"]["identity"], f"{path}.producer")


def _build_export(document: dict[str, Any], path: str) -> None:
    _candidate_source(document, path)
    _unit(document["arguments"], "target", path)


def _runtime_export(document: dict[str, Any], path: str) -> None:
    arguments = document["arguments"]
    _candidate_source(document, path)
    _unit(arguments, "lane", path)
    check(arguments["build"]["scope"] == "complete", f"{path}.build", "a lane ran against the complete Build")
    bind_build_envelope(arguments["build"], descriptor=arguments["owning_build"], plan=arguments["plan"])


def _build_grant(document: dict[str, Any], path: str) -> None:
    """The sealed Build a verification reads: a partition or the complete Build of the plan. A
    lane's owning Build may come from another run, so no producing attempt is named."""
    _job(document, path)
    arguments = document["arguments"]
    validate_build_envelope(arguments["envelope"], plan=arguments["plan"], path=f"{path}.envelope")


def _runtime_grant(document: dict[str, Any], path: str) -> None:
    _job(document, path)
    _runtime_lane(document["arguments"], path)


#: Operation -> (closed argument object, cross-field checks over the whole request).
_OPERATIONS = {
    "host-fence": (Obj({}), lambda document, path: None),
    "stage-candidate": (
        Obj({"candidate": _ACCOUNT, "repository": REPO,
             "tested_sha": _OID, "tested_tree": _OID,
             "inventory": List(_TRACKED, min_items=1, max_items=limits.MAX_CI_SOURCE_FILES),
             "source": _home_path, "gradle_seed": Nullable(_home_path),
             "overlay": Obj({"path": _home_path, "sha": _OID, "version": Str(grammar.VERSION, max_len=20),
                             "tree_digest": Str(grammar.DIGEST, max_len=71)})}),
        _candidate_staging),
    "freeze-build-validation": (
        Obj({"validator": _ACCOUNT, "sources": _SOURCES, "plan": _plan, "envelope": _envelope,
             "run_id": _RUN, "run_attempt": _ATTEMPT, "execution_nonce": _SHA}), _build_validation),
    "freeze-runtime-validation": (
        Obj({"validator": _ACCOUNT, "sources": _SOURCES, "plan": _plan, "build": _envelope,
             "runtime": _runtime, "lane_id": _UNIT, "run_id": _RUN, "run_attempt": _ATTEMPT,
             "execution_nonce": _SHA}), _runtime_validation),
    "grant-controller": (Obj({**_JOB, "subject": validate_subject, "sources": _SOURCES}), _controller_grant),
    "grant-plan-inputs": (
        Obj({**_JOB, "inputs": List(_INPUT, min_items=2, max_items=2 + limits.MAX_CI_PLAN_INPUTS)}), _plan_inputs),
    "take-derived-plan": (Obj(_JOB), _job),
    "grant-validation-inputs": (Obj({**_JOB, "plan": _plan}), _job),
    "stage-bundle": (
        Obj({**_CANDIDATE_JOB, "sources": _SOURCES, "plan": _plan, "envelope": _envelope}), _bundle_staging),
    "take-derived-runtime": (Obj(_JOB), _job),
    "verify-candidate-source": (Obj(_CANDIDATE_SOURCE), _candidate_source),
    "freeze-build-export": (
        Obj({**_CANDIDATE_SOURCE, "target_id": _UNIT, "producer": _PRODUCER_IDENTITY}), _build_export),
    "freeze-runtime-export": (
        Obj({**_CANDIDATE_SOURCE, "lane_id": _UNIT, "producer": _PRODUCER_IDENTITY, "build": _envelope,
             "owning_build": validate_descriptor}), _runtime_export),
    "grant-build-validation": (Obj({**_JOB, "plan": _plan, "envelope": _envelope}), _build_grant),
    "grant-runtime-validation": (
        Obj({**_JOB, "plan": _plan, "build": _envelope, "runtime": _runtime, "lane_id": _UNIT,
             "run_id": _RUN, "run_attempt": _ATTEMPT}), _runtime_grant),
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
