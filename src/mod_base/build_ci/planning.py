"""Turn a subject, the protected policy and one ``derive_plan`` result into the protected plan.

Pure functions: nothing here runs a hook, reads a file or calls the API. The job that plans runs
``derive_plan`` inside the validator account and passes its bytes here, together with the bytes
of the two candidate files the hook read and the protected Build config. Every job of a
generation plans again and compares the hash (``ci plan --expect-sha256``), so a plan is authority
only where protected code derived it.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from mod_base import SCHEMA_VERSIONS
from mod_base.build_ci.adapter import parse_derived_plan
from mod_base.build_ci.config import BuildConfig
from mod_base.build_ci.identity import policy_sha256
from mod_base.build_ci.protocol import (BUILD_ADAPTER_API, SHA256, plan_sha256, subject_of, validate_plan,
                                        validate_subject)
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json, canonical_sha256, sha256_hex
from mod_base.model.validators import check


class PlanError(MbError):
    """A plan is not the one this job must work on (exit 2)."""

    default_reason = "ci-plan"


def _candidate_bytes(data: Any, path: str) -> bytes:
    check(type(data) is bytes and 1 <= len(data) <= limits.MAX_CI_PLAN_SOURCE_BYTES, path,
          f"must be 1..{limits.MAX_CI_PLAN_SOURCE_BYTES} bytes of the candidate file")
    return data


def runtime_selection_sha256(profile: str, lanes: list[dict[str, Any]]) -> str:
    """The identity of what the runtime gate must run: the profile and every derived lane with
    its native contract and its ordered obligations."""

    return canonical_sha256({"profile": profile, "lanes": lanes})


def build_plan(*, subject: dict[str, Any], config: BuildConfig, inventory: bytes, scenario_contract: bytes,
               derived: bytes) -> dict[str, Any]:
    """The complete, validated ``mod-base.build.plan`` of ``subject``.

    ``inventory`` and ``scenario_contract`` are the bytes of the candidate Git blobs at the tested
    tree, at the paths the protected config names: exactly what ``derive_plan`` was given.
    ``derived`` is what that hook wrote. The identity gains the policy digest
    (:func:`mod_base.build_ci.identity.policy_sha256`), the inventory's Git blob id and SHA-256, the
    scenario contract's SHA-256 and the runtime selection digest; ``plan_sha256`` binds all of it."""

    policy = policy_sha256(config, subject)
    _candidate_bytes(inventory, "$.inventory")
    _candidate_bytes(scenario_contract, "$.scenario_contract")
    units = parse_derived_plan(derived)
    profile = config.data["profile"]
    identity = {
        **copy.deepcopy(subject),
        "policy_sha256": policy,
        "inventory_blob": hashlib.sha1(b"blob %d\0" % len(inventory) + inventory).hexdigest(),  # noqa: S324 - Git id
        "inventory_sha256": sha256_hex(inventory),
        "scenario_sha256": sha256_hex(scenario_contract),
        "runtime_selection_sha256": runtime_selection_sha256(profile, units["lanes"]),
    }
    plan = {"kind": "mod-base.build.plan", "schema_version": SCHEMA_VERSIONS["mod-base.build.plan"],
            "build_adapter_api": BUILD_ADAPTER_API, "identity": identity, "profile": profile,
            "targets": units["targets"], "lanes": units["lanes"]}
    plan["plan_sha256"] = plan_sha256(plan)
    validate_plan(plan)
    check(len(canonical_json(plan)) <= limits.MAX_CI_PLAN_BYTES, "$.plan", "exceeds the plan byte cap")
    return plan


def require_plan(plan: dict[str, Any], *, subject: dict[str, Any],
                 expected_sha256: str | None = None) -> dict[str, Any]:
    """Require a valid plan of exactly ``subject`` and, when given, with the expected hash: the one
    another job of the same generation derived. A difference is a rejection, never a reason to
    prefer either plan."""

    validate_plan(plan)
    check(subject_of(plan["identity"]) == validate_subject(subject), "$.plan.identity",
          "the plan belongs to another subject")
    if expected_sha256 is not None:
        SHA256(expected_sha256, "$.expected_sha256")
        if plan["plan_sha256"] != expected_sha256:
            raise PlanError("this job derived another plan than the one the generation agreed on",
                            reason="plan-mismatch")
    return plan


def matrices(plan: dict[str, Any]) -> dict[str, list[str]]:
    """The job matrices of a plan: its target ids and its lane ids, in plan order."""

    validate_plan(plan)
    return {"targets": [target["id"] for target in plan["targets"]], "lanes": [lane["id"] for lane in plan["lanes"]]}


def plan_outputs(plan: dict[str, Any]) -> dict[str, str]:
    """The workflow outputs of ``ci plan``: ``plan_sha256`` and the two matrices as JSON arrays."""

    return {"plan_sha256": plan["plan_sha256"],
            **{name: canonical_json(ids).decode("utf-8").rstrip("\n") for name, ids in matrices(plan).items()}}
