"""The Build adapter contract (``BUILD_ADAPTER_API = 1``): hooks, argv, environment, files, parsers.

A mod's protected Build config names one dispatcher. Every hook is that dispatcher run as
``<python> -I -B <checkout>/<dispatcher> --hook <name>`` inside a disposable account, with the
fixed environment of ``worker.worker_environment`` plus the values of :func:`hook_values`:

* a *protected* hook runs as the validator from the protected adapter copy (``controller/``). It
  reads the files :func:`hook_inputs` names in ``validation-input/`` (and the sealed exports) and
  must leave exactly the files :func:`hook_outputs` names in ``validation/`` of its home;
* a *candidate* hook runs as the candidate from its own checkout (``repository/``) and writes
  below ``export/`` of its home: for ``build_target`` exactly :func:`target_outputs`.

The worker root is the parent directory of a hook's ``HOME``; the directories below it are the
``*_DIRECTORY`` constants of this module. Mod code writes native files only: protected kit code
turns ``plan.json`` into the plan (:mod:`mod_base.build_ci.planning`) and inventories every export.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from mod_base.build_ci.protocol import ID, repo_path, validate_plan, validate_plan_units
from mod_base.errors import MbError
from mod_base.io.secure_json import loads
from mod_base.model import grammar, limits
from mod_base.model.validators import Obj, Str, check


class AdapterError(MbError):
    """A hook was requested outside the closed adapter contract (exit 2)."""

    default_reason = "ci-adapter"


@dataclass(frozen=True)
class Hook:
    """One hook of the contract: who runs it, for which unit and under which configured timeout."""

    name: str
    role: str
    unit: str | None
    timeout: str


#: Every hook, in the order a generation first needs it. ``role`` is the worker account
#: (``validator`` hooks are the protected ones), ``unit`` the plan unit a run is for (``target``,
#: ``lane`` or none) and ``timeout`` the key of the protected config's ``timeouts``.
HOOKS = {hook.name: hook for hook in (
    Hook("derive_plan", "validator", None, "validator_seconds"),
    Hook("policy", "candidate", None, "policy_seconds"),
    Hook("build_target", "candidate", "target", "target_seconds"),
    Hook("verify_target", "validator", "target", "validator_seconds"),
    Hook("verify_build", "validator", None, "validator_seconds"),
    Hook("derive_runtime", "validator", "lane", "validator_seconds"),
    Hook("run_lane", "candidate", "lane", "runtime_seconds"),
    Hook("verify_runtime", "validator", "lane", "validator_seconds"),
)}
PROTECTED_HOOKS = tuple(name for name, hook in HOOKS.items() if hook.role == "validator")
CANDIDATE_HOOKS = tuple(name for name, hook in HOOKS.items() if hook.role == "candidate")
#: The environment name that carries a run's unit id.
UNIT_ENVIRONMENT = {"target": "MB_TARGET_ID", "lane": "MB_LANE_ID"}
#: The names ``derive_runtime`` returns for its lane; ``run_lane`` receives exactly these.
RUNTIME_VALUES = ("E2E_ROW_JSON", "E2E_SCENARIOS")

#: Files of ``validation-input/``: the bytes of the two candidate files the config names, then
#: the plan those bytes produced. Every later protected hook may read all three (the plan's
#: identity binds both candidate files by SHA-256).
INVENTORY_INPUT = "inventory"
SCENARIO_INPUT = "scenario-contract"
PLAN_INPUT = grammar.CI_PLAN_NAME
#: Files a derivation hook leaves in ``validation/``; a verification hook leaves one report per unit.
PLAN_OUTPUT = "plan.json"
RUNTIME_OUTPUT = "runtime.json"
REPORT_SUFFIX = ".json"
#: Unit ids whose report would take the name of another file of ``validation/`` or of its sealed copy.
RESERVED_UNIT_IDS = frozenset(name[:-len(REPORT_SUFFIX)]
                              for name in (PLAN_OUTPUT, RUNTIME_OUTPUT, grammar.CI_VALIDATION_NAME))

#: Directories below the worker root, as a hook sees them.
CHECKOUT_DIRECTORY = {"validator": "controller", "candidate": "repository"}
HOME_DIRECTORY = {"validator": "validator-home", "candidate": "candidate-home"}
INPUT_DIRECTORY = "validation-input"
SEALED_BUILD_DIRECTORY = "sealed-build"
SEALED_RUNTIME_DIRECTORY = "sealed-runtime"
#: Directories below a hook's home.
OUTPUT_DIRECTORY = "validation"
EXPORT_DIRECTORY = "export"


def _hook(name: object) -> Hook:
    if not isinstance(name, str) or name not in HOOKS:
        raise AdapterError("unknown Build adapter hook")
    return HOOKS[name]


def hook_command(hook: str, *, python: str, checkout: str, dispatcher: str) -> tuple[str, ...]:
    """The fixed argv of ``hook``: the interpreter, isolated and without bytecode, the dispatcher
    of the role's checkout and the hook name. ``dispatcher`` is the config's ``adapter.dispatcher``."""

    name = _hook(hook).name
    repo_path(dispatcher, "$.adapter.dispatcher")
    for label, value in (("python", python), ("checkout", checkout)):
        check(isinstance(value, str) and value.startswith("/") and not value.endswith("/") and "\0" not in value,
              f"$.{label}", "must be an absolute path")
    return (python, "-I", "-B", f"{checkout}/{dispatcher}", "--hook", name)


_VALUE = Str(max_len=limits.MAX_CI_ENV_VALUE_BYTES, text="evidence")


def _runtime_value(value: Any, path: str) -> str:
    _VALUE(value, path)
    check(len(value.encode("utf-8")) <= limits.MAX_CI_ENV_VALUE_BYTES, path, "exceeds the environment value cap")
    return value


_RUNTIME = Obj({"values": Obj({name: _runtime_value for name in RUNTIME_VALUES})})


def hook_values(hook: str, *, unit_id: str | None = None,
                runtime: Mapping[str, str] | None = None) -> dict[str, str]:
    """The extra environment of one hook run: its unit id and, for ``run_lane`` only, the values
    ``derive_runtime`` returned for that lane (:func:`parse_runtime_values`)."""

    contract = _hook(hook)
    values: dict[str, str] = {}
    if contract.unit is None:
        check(unit_id is None, "$.unit_id", f"{contract.name} runs for no unit")
    else:
        values[UNIT_ENVIRONMENT[contract.unit]] = ID(unit_id, "$.unit_id")
    if contract.name == "run_lane":
        check(isinstance(runtime, Mapping), "$.runtime", "run_lane requires its derived runtime values")
        values.update(_RUNTIME({"values": dict(runtime)}, "$.runtime")["values"])
    else:
        check(runtime is None, "$.runtime", f"{contract.name} receives no runtime values")
    return values


def hook_timeout_seconds(hook: str, config: Mapping[str, Any]) -> int:
    """The native timeout the protected config (a validated document) sets for ``hook``."""

    return config["timeouts"][_hook(hook).timeout]


def plan_unit(plan: dict[str, Any], hook: str, unit_id: str | None) -> dict[str, Any] | None:
    """The target or lane of the protected plan a hook run is for (``None`` for a hook without
    a unit). A unit the plan does not hold is a rejection, never a default."""

    contract = _hook(hook)
    validate_plan(plan)
    if contract.unit is None:
        check(unit_id is None, "$.unit_id", f"{contract.name} runs for no unit")
        return None
    for unit in plan[f"{contract.unit}s"]:
        if unit["id"] == unit_id:
            return unit
    raise AdapterError(f"{contract.name} was requested for a {contract.unit} outside the protected plan")


def hook_inputs(hook: str) -> tuple[str, ...]:
    """The files of ``validation-input/`` a protected hook may read (none for a candidate hook)."""

    contract = _hook(hook)
    if contract.role != "validator":
        return ()
    candidate = (INVENTORY_INPUT, SCENARIO_INPUT)
    return candidate if contract.name == "derive_plan" else (PLAN_INPUT, *candidate)


def hook_outputs(hook: str, *, plan: dict[str, Any] | None = None, unit_id: str | None = None) -> tuple[str, ...]:
    """The exact files a protected hook must leave in ``validation/``.

    A derivation leaves its one document; a verification leaves ``<unit id>.json`` for its unit, and
    ``verify_build`` one for every target, in plan order. Anything else there fails the step."""

    contract = _hook(hook)
    if contract.role != "validator":
        raise AdapterError(f"{contract.name} writes its export, not validation files")
    if contract.name == "derive_plan":
        check(unit_id is None, "$.unit_id", "derive_plan runs for no unit")
        return (PLAN_OUTPUT,)
    if plan is None:
        raise AdapterError(f"{contract.name} runs against the protected plan")
    unit = plan_unit(plan, contract.name, unit_id)
    if contract.name == "derive_runtime":
        return (RUNTIME_OUTPUT,)
    units = plan["targets"] if unit is None else [unit]
    return tuple(item["id"] + REPORT_SUFFIX for item in units)


def target_outputs(plan: dict[str, Any], target_id: str) -> tuple[str, ...]:
    """The exact files ``build_target`` must leave below ``export/`` for one target, sorted."""

    target = plan_unit(plan, "build_target", target_id)
    return tuple(sorted(output["path"] for output in target["outputs"]))


def parse_derived_plan(data: bytes) -> dict[str, Any]:
    """Strictly decode what ``derive_plan`` wrote: ``{"targets": [...], "lanes": [...]}`` in the
    plan's shape, with no identity, hash, command or path outside the export grammar."""

    document = loads(data, label="derive_plan output", max_bytes=limits.MAX_CI_PLAN_BYTES)
    validate_plan_units(document, "$.plan")
    for kind in ("targets", "lanes"):
        for index, unit in enumerate(document[kind]):
            check(unit["id"] not in RESERVED_UNIT_IDS, f"$.plan.{kind}[{index}].id",
                  "would name a reserved validation file")
    return document


def parse_runtime_values(data: bytes) -> dict[str, str]:
    """Strictly decode what ``derive_runtime`` wrote: ``{"values": {...}}`` holding exactly
    :data:`RUNTIME_VALUES`, each non-blank text without control characters within the worker's
    per-value cap."""

    document = loads(data, label="derive_runtime output", max_bytes=limits.MAX_CI_REPORT_BYTES)
    return dict(_RUNTIME(document, "$.runtime")["values"])
