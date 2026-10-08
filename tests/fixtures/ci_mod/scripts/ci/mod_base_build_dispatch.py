"""Synthetic mod: the Build dispatcher, run as ``<python> -I -B <this file> --hook <name>``.

The kit runs it in a disposable account whose ``HOME`` lies directly below the worker root:

* a protected hook (``derive_plan``, ``derive_runtime``, ``verify_*``) runs from the protected copy
  of this directory. It reads ``<root>/validation-input/`` and the sealed exports
  (``<root>/sealed-build/``, ``<root>/sealed-runtime/``) and writes ``$HOME/validation/``;
* a candidate hook (``policy``, ``build_target``, ``run_lane``) runs from the tested checkout,
  its working directory, and writes ``$HOME/export/``.

``MB_TARGET_ID`` or ``MB_LANE_ID`` names the unit; ``MB_TESTED_SHA`` and ``MB_TESTED_TREE`` name what
is tested. Every output is a new private file: nothing is overwritten and nothing else is written.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ``-I`` keeps the script's directory off ``sys.path``; the adapter modules live next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import mod_base_build_adapter as adapter  # noqa: E402
import policy_suite  # noqa: E402

UNIT_NAMES = {"build_target": "MB_TARGET_ID", "verify_target": "MB_TARGET_ID", "derive_runtime": "MB_LANE_ID",
              "run_lane": "MB_LANE_ID", "verify_runtime": "MB_LANE_ID"}
CANDIDATE_HOOKS = ("policy", "build_target", "run_lane")
KIT_FILES = ("ci-envelope.json", "ci-runtime-envelope.json")


class Output:
    """The hook's output directory: files are created, never replaced."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.written: list[Path] = []

    def write(self, relative: str, data: bytes) -> None:
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "xb") as stream:
            stream.write(data)
        self.written.append(path)


def _read(root: Path, relative: str) -> bytes:
    path = root.joinpath(*relative.split("/"))
    if path.is_symlink() or not path.is_file():
        raise adapter.AdapterError(f"{relative} is not a regular file")
    return path.read_bytes()


def _files(root: Path) -> set[str]:
    """Every file below ``root`` by relative path; a link or special file is a rejection."""

    found = set()
    for path in sorted(root.rglob("*")) if root.is_dir() else ():
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise adapter.AdapterError(f"{path.name} is not a regular file or directory")
        if path.is_file():
            found.add(path.relative_to(root).as_posix())
    return found


def _protected_inputs(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    """The inventory and the contract a protected hook was given, and the bytes of every candidate
    file by its name in ``validation-input/``."""

    inputs = root / "validation-input"
    raw = {name: _read(inputs, name) for name in ("inventory", "scenario-contract", adapter.PROPERTIES_INPUT)}
    properties = adapter.parse_properties(raw[adapter.PROPERTIES_INPUT])
    return adapter.parse_inventory(raw["inventory"], properties), adapter.parse_contract(raw["scenario-contract"]), raw


def _plan(root: Path, raw: dict[str, bytes]) -> dict[str, Any]:
    """The protected plan, which must bind every candidate file this hook was given: the
    inventory and the scenario contract in its identity, ``gradle.properties`` in ``plan_inputs``."""

    plan = adapter.decode(_read(root / "validation-input", "ci-plan.json"), "plan")
    identity = plan["identity"]
    if (identity["inventory_sha256"], identity["scenario_sha256"]) != (adapter.sha256(raw["inventory"]),
                                                                      adapter.sha256(raw["scenario-contract"])):
        raise adapter.AdapterError("the plan was not derived from this inventory and scenario contract")
    extra = [{"name": adapter.PROPERTIES_INPUT, "sha256": adapter.sha256(raw[adapter.PROPERTIES_INPUT])}]
    if plan["plan_inputs"] != extra:
        raise adapter.AdapterError("the plan was not derived from this gradle.properties")
    return plan


def _unit(plan: dict[str, Any], kind: str, unit: str) -> dict[str, Any]:
    for entry in plan[kind]:
        if entry["id"] == unit:
            return entry
    raise adapter.AdapterError(f"the plan has no {kind[:-1]} {unit!r}")


def _verify_targets(root: Path, output: Output, hook: str, unit: str | None) -> None:
    inventory, contract, raw = _protected_inputs(root)
    plan = _plan(root, raw)
    sealed = root / "sealed-build"
    planned = {entry["path"] for target in plan["targets"] for entry in target["outputs"]}
    present = _files(sealed) - set(KIT_FILES)
    if not present <= planned or (unit is None and present != planned):
        raise adapter.AdapterError("the sealed Build does not hold exactly the planned outputs")
    for entry in plan["targets"] if unit is None else [_unit(plan, "targets", unit)]:
        target = adapter.target_of(inventory, entry["id"])
        staged = [item for loader in target["loaders"] for item in adapter.lane_outputs(inventory, target, loader)]
        if entry["native_contract_sha256"] != adapter.target_contract(inventory, target) or entry["outputs"] != staged:
            raise adapter.AdapterError(f"target {entry['id']} of the plan is not this inventory's target")
        files = [record for loader in target["loaders"] for record in adapter.verify_lane(
            inventory, target, loader, tested_sha=plan["identity"]["tested_sha"],
            tested_tree=plan["identity"]["tested_tree"], read=lambda path: _read(sealed, path))]
        output.write(f"{entry['id']}.json", adapter.encode({
            "schema_version": 1, "hook": hook, "unit": entry["id"],
            "native_contract_sha256": entry["native_contract_sha256"],
            "files": sorted(files, key=lambda record: record["path"])}))


def _verify_runtime(root: Path, output: Output, lane: str) -> None:
    inventory, contract, raw = _protected_inputs(root)
    plan = _plan(root, raw)
    entry = _unit(plan, "lanes", lane)
    target, loader = adapter.lane_of(inventory, lane)
    if entry["native_contract_sha256"] != adapter.lane_contract(contract, target, loader):
        raise adapter.AdapterError(f"lane {lane} of the plan is not this contract's lane")
    production = [item["path"] for item in _unit(plan, "targets", entry["target_id"])["outputs"]
                  if item["lane_id"] == lane and item["role"] == "production"]
    results = root / "sealed-runtime" / "lanes" / lane
    summary = adapter.verify_run(lane, entry["obligations"], adapter.runtime_row(inventory, lane),
                                 tested_sha=plan["identity"]["tested_sha"],
                                 production=_read(root / "sealed-build", production[0]),
                                 read=lambda path: _read(results, path))
    if _files(results) != set(summary["files"]):
        raise adapter.AdapterError(f"lane {lane} left files its result report does not account for")
    output.write(f"{lane}.json", adapter.encode({"schema_version": 1, "hook": "verify_runtime", "unit": lane,
                                               "native_contract_sha256": entry["native_contract_sha256"], **summary}))


def _candidate_inputs(checkout: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    config = adapter.decode(_read(checkout, policy_suite.CONFIG), policy_suite.CONFIG)
    return (config, *policy_suite.documents(checkout))


def _run_lane(checkout: Path, output: Output, lane: str) -> None:
    config, inventory, contract = _candidate_inputs(checkout)
    row = adapter.decode(os.environ["E2E_ROW_JSON"].encode("utf-8"), "E2E_ROW_JSON")
    wanted = os.environ["E2E_SCENARIOS"].split(",")
    scenarios = [scenario for scenario in contract["scenarios"] if scenario["id"] in wanted]
    if type(row) is not dict or row.get("id") != lane or [scenario["id"] for scenario in scenarios] != wanted:
        raise adapter.AdapterError(f"the runtime values do not describe lane {lane}")
    production = _read(checkout / config["bundle"]["path"], row["production"])
    if adapter.jar_identity(production, row["production"])["tested_sha"] != os.environ["MB_TESTED_SHA"]:
        raise adapter.AdapterError("the staged Build was not built from the tested commit")
    for path, data in adapter.run_lane(lane, row, scenarios, tested_sha=os.environ["MB_TESTED_SHA"],
                                       production=production).items():
        output.write(path, data)


def _hook(hook: str, unit: str | None, root: Path, output: Output) -> None:
    if hook == "derive_plan":
        inventory, contract, _ = _protected_inputs(root)
        output.write("plan.json", adapter.encode(adapter.derive_plan(inventory, contract)))
    elif hook == "derive_runtime":
        inventory, contract, raw = _protected_inputs(root)
        _unit(_plan(root, raw), "lanes", unit)
        output.write("runtime.json", adapter.encode({"values": adapter.runtime_values(inventory, contract, unit)}))
    elif hook in ("verify_target", "verify_build"):
        _verify_targets(root, output, hook, unit)
    elif hook == "verify_runtime":
        _verify_runtime(root, output, unit)
    elif hook == "policy":
        failures = policy_suite.run(Path.cwd())
        print(f"synthetic policy: {len(policy_suite.CHECKS)} checks, {len(failures)} failures", flush=True)
        if failures:
            raise adapter.AdapterError("; ".join(failures))
    elif hook == "build_target":
        _, inventory, _ = _candidate_inputs(Path.cwd())
        target = adapter.target_of(inventory, unit)
        for loader in target["loaders"]:
            for path, data in adapter.build_lane(inventory, target, loader, tested_sha=os.environ["MB_TESTED_SHA"],
                                                 tested_tree=os.environ["MB_TESTED_TREE"],
                                                 source=_read(Path.cwd(), policy_suite.SOURCE)).items():
                output.write(path, data)
    else:
        _run_lane(Path.cwd(), output, unit)


def main(arguments: list[str]) -> int:
    if len(arguments) != 2 or arguments[0] != "--hook" or arguments[1] not in adapter.HOOKS:
        print("usage: mod_base_build_dispatch.py --hook <name>", flush=True)
        return 2
    hook = arguments[1]
    os.umask(0o077)
    home = Path(os.environ["HOME"])
    unit = os.environ.get(UNIT_NAMES[hook]) if hook in UNIT_NAMES else None
    label = hook if unit is None else f"{hook} {unit}"
    candidate = hook in CANDIDATE_HOOKS
    output = Output(home / ("export" if candidate else "validation"))
    try:
        if hook in UNIT_NAMES and not unit:
            raise adapter.AdapterError(f"{UNIT_NAMES[hook]} is required")
        inventory = _candidate_inputs(Path.cwd())[1] if candidate else _protected_inputs(home.parent)[0]
        mode = adapter.fault(inventory, hook, unit)
        if mode == "fail":
            raise adapter.AdapterError("the release inventory requests this failure")
        while mode == "hang":
            time.sleep(60)
        _hook(hook, unit, home.parent, output)
        if mode == "missing" and output.written:
            output.written[0].unlink()
        elif mode == "extra":
            output.write("unplanned.txt", b"not a planned output\n")
        elif mode == "orphan":
            child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(3600)"],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
            print(f"synthetic {label}: orphan pid={child.pid}", flush=True)
    except (adapter.AdapterError, KeyError, TypeError, AttributeError, ValueError, OSError) as error:
        print(f"synthetic {label} rejected: {error}", flush=True)
        return 1
    print(f"synthetic {label}: ok, {len(output.written)} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
