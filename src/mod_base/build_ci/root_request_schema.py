"""Closed local metadata request for fixed Build validation sealing; no execution authority."""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import check_output_paths, repo_path, validate_plan
from mod_base.build_ci.records import validate_build_envelope
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from mod_base.model.validators import Const, Int, List, Obj, Str, check


_SHA = Str(grammar.SHA256, max_len=64)
_OID = Str(grammar.SHA1, max_len=40)
_FILE_ID = Int(0, limits.MAX_CI_FILE_ID)
_UNIX_ID = Int(1, limits.MAX_CI_UNIX_ID)
_WORKER_ID = Int(limits.MIN_CI_WORKER_UID, limits.MAX_CI_UNIX_ID)
_VERSIONS = readable_schema_versions("mod-base.ci.root-request")
_FILE = Obj({"path": repo_path, "mode": Str(choices=("100644", "100755")),
             "git_blob": _OID, "sha256": _SHA, "size": Int(0, limits.MAX_CI_ADAPTER_FILE_BYTES)})
_BOUNDARY = Obj({"home": Const("/home/runner"), "uid": _UNIX_ID, "gid": _UNIX_ID,
                 "device": _FILE_ID, "inode": Int(1, limits.MAX_CI_FILE_ID),
                 "original_mode": Int(0, 0o7777)})
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


_REQUEST = Obj({"kind": Const("mod-base.ci.root-request"),
    "schema_version": Int(min(_VERSIONS), max(_VERSIONS)), "nonce": _SHA, "execution_nonce": _SHA,
    "boundary": _BOUNDARY, "validator": Obj({"uid": _WORKER_ID, "gid": _WORKER_ID}),
    "sources": _SOURCES, "plan": _plan, "envelope": _envelope,
    "run_id": Int(1, limits.MAX_RUN_ID), "run_attempt": Int(1, limits.MAX_RUN_ATTEMPT)})


def validate_root_request(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Validate data only; original source, UID, pin and successful execution need physical admission."""
    _REQUEST(document, path)
    plan, envelope = document["plan"], document["envelope"]
    validate_build_envelope(envelope, plan=plan, path=f"{path}.envelope")
    check((document["run_id"], document["run_attempt"]) ==
          (envelope["producer"]["run_id"], envelope["producer"]["run_attempt"]), path, "root producer differs")
    _metadata(document, path)
    check(len(canonical_json(document)) <= limits.MAX_CI_ROOT_REQUEST_BYTES, path, "root request exceeds local cap")
    return document


def _metadata(document: dict[str, Any], path: str) -> None:
    """Shared closed source/account data checks, after each request's own shape validation."""
    plan, sources = document["plan"], document["sources"]
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
    runner, validator = document["boundary"], document["validator"]
    check(runner["uid"] != validator["uid"] and runner["gid"] != validator["gid"],
          f"{path}.validator", "root request validator is not isolated from runner")
    check(document["nonce"] != document["execution_nonce"], path, "root request needs a separate entry nonce")
