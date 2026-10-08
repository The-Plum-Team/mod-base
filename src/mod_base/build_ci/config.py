"""Closed protected Build configuration, distinct from the Pages v1 config.

File hashes bind the protected adapter/dispatcher/policy import closure. This pure schema cannot
establish that a path is protected: native admission must authenticate every inventoried source
at the executing controller before any import, and preserve the native timeout contract.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import BUILD_ADAPTER_API, check_output_paths, repo_path
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.validators import Const, Int, List, Obj, Str, check


BUILD_CONFIG_PATH = 'scripts/ci/mod-base-build.json'

_FILE = Obj({"path": repo_path, "sha256": Str(g.SHA256, max_len=64)})
_CONFIG = Obj({
    "kind": Const("mod-base.build.config"),
    "schema_version": Int(min(readable_schema_versions("mod-base.build.config")),
                          max(readable_schema_versions("mod-base.build.config"))),
    "repository": Str(g.REPOSITORY, max_len=201),
    "profile": Str(choices=("quick-skin", "block-pops")),
    "build_adapter_api": Const(BUILD_ADAPTER_API),
    "adapter": Obj({"path": repo_path, "dispatcher": repo_path, "policy": repo_path,
                    "files": List(_FILE, min_items=3, max_items=lim.MAX_CI_ADAPTER_FILES,
                                  unique_by=lambda item: item["path"])}),
    "timeouts": Obj({key: Int(1, lim.MAX_CI_WORKER_TIMEOUT_SECONDS)
                     for key in ("policy_seconds", "target_seconds", "runtime_seconds", "validator_seconds")}),
})


def validate_build_config(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Data only: no matrix/scenario catalog, shell program, runner, permission or secret field."""

    _CONFIG(document, path)
    adapter = document["adapter"]
    entries = [adapter[key] for key in ("path", "dispatcher", "policy")]
    check(len(set(entries)) == len(entries), f"{path}.adapter", "adapter, candidate dispatcher and policy are distinct")
    check(all(entry.startswith("scripts/ci/") and entry.endswith(".py") for entry in entries),
          f"{path}.adapter", "entrypoints must be Python files under protected controller roots")
    inventory = [file["path"] for file in adapter["files"]]
    check_output_paths(inventory, f"{path}.adapter.files")
    check(inventory == sorted(inventory) and len({name.casefold() for name in inventory}) == len(inventory),
          f"{path}.adapter.files", "source inventory must be sorted without case aliases")
    check(set(entries) <= set(inventory), f"{path}.adapter.files", "all fixed entrypoints must be hash-inventoried")
    return document
