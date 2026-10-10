"""Synthetic mod: the repository policy the ``policy`` hook checks in the candidate checkout.

Every check reads the checkout as data and reports what it found; the hook fails when any check
does. The real mods run their unit suites here instead.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import mod_base_build_adapter as adapter

CONFIG = "scripts/ci/mod-base-build.json"
SOURCE = "src/payload.txt"


def _read(root: Path, relative: str) -> bytes:
    path = root.joinpath(*relative.split("/"))
    if path.is_symlink() or not path.is_file():
        raise adapter.AdapterError(f"{relative} is not a regular file of the checkout")
    return path.read_bytes()


def documents(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The inventory and the scenario contract of the checkout ``root``, read from the paths its
    own Build config names: the two fixed candidate files and ``gradle.properties``, the extra one."""

    config = adapter.decode(_read(root, CONFIG), CONFIG)
    extra = {entry["name"]: entry["path"] for entry in config["plan_inputs"]}
    properties = adapter.parse_properties(_read(root, extra[adapter.PROPERTIES_INPUT]))
    return (adapter.parse_inventory(_read(root, config["inventory"]["path"]), properties),
            adapter.parse_contract(_read(root, config["scenario_contract"]["path"])))


def _inventory_and_contract_are_well_formed(root: Path) -> None:
    documents(root)


def _every_lane_has_a_scenario(root: Path) -> None:
    adapter.derive_plan(*documents(root))


def _every_scenario_is_used(root: Path) -> None:
    inventory, contract = documents(root)
    loaders = {loader for target in inventory["targets"] for loader in target["loaders"]}
    for scenario in contract["scenarios"]:
        if not loaders & set(scenario["loaders"]):
            raise adapter.AdapterError(f"scenario {scenario['id']} applies to no released loader")


def _the_source_is_present(root: Path) -> None:
    if not _read(root, SOURCE):
        raise adapter.AdapterError(f"{SOURCE} is empty")


CHECKS: tuple[Callable[[Path], None], ...] = (
    _inventory_and_contract_are_well_formed,
    _every_lane_has_a_scenario,
    _every_scenario_is_used,
    _the_source_is_present,
)


def run(root: Path) -> list[str]:
    """Run every check against the checkout ``root``; return one line per failure."""

    failures = []
    for check in CHECKS:
        try:
            check(root)
        except (adapter.AdapterError, KeyError, TypeError, OSError) as error:
            failures.append(f"{check.__name__.strip('_')}: {error}")
    return failures
