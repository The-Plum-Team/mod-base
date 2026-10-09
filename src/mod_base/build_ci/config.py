"""Closed protected Build configuration, distinct from the Pages v1 config.

``scripts/ci/mod-base-build.json`` names the adapter entry points and their hashed import closure,
the candidate files a plan is derived from (the inventory, the scenario contract and up to
``limits.MAX_CI_PLAN_INPUTS`` extra ``plan_inputs``), where a lane's checkout expects the staged
Build, the two required status contexts, the native timeouts and, optionally, the kit system profile
a lane installs before its accounts exist (:func:`system_profile`). :func:`validate_build_config` is the
pure schema; :func:`load_build_config` reads the file from the protected checkout the prologue
verified and requires every listed source there to have its configured hash, so its result is the
complete protected adapter that planning hashes into the policy digest. It also records the mod's
own control files (:data:`CONTROL_PATHS`), which the same digest covers.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.activation import ACTIVATION_PATH, CALLERS
from mod_base.build_ci.adapter import plan_input_name, plan_sources
from mod_base.build_ci.protocol import BUILD_ADAPTER_API, PROFILES, check_output_paths, repo_path
from mod_base.build_ci.system_profile import SYSTEM_PROFILES
from mod_base.errors import MbError
from mod_base.io.secure_json import loads
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import read_regular_file, sha256_hex
from mod_base.model.validators import Const, Int, List, Obj, Str, check, fail


BUILD_CONFIG_PATH = 'scripts/ci/mod-base-build.json'


class BuildConfigError(MbError):
    """The protected Build configuration or a source it lists cannot be trusted (exit 2)."""

    default_reason = "ci-config"


def _context(value: Any, path: str) -> str:
    """A status context: trimmed printable ASCII, so two contexts cannot differ by a lookalike."""

    Str(max_len=lim.MAX_CI_STATUS_CONTEXT_CHARS, text="display")(value, path)
    if not value.isascii():
        raise fail(path, "must be printable ASCII")
    return value


_FILE = Obj({"path": repo_path, "sha256": Str(g.SHA256, max_len=64)})
_PATH = Obj({"path": repo_path})
#: One more candidate file the plan is derived from: the name it is staged under for the protected
#: hooks, next to the inventory and the scenario contract, and its path in the tested tree.
_PLAN_INPUT = Obj({"name": plan_input_name, "path": repo_path})
_CONFIG = Obj({
    "kind": Const("mod-base.build.config"),
    "schema_version": Int(min(readable_schema_versions("mod-base.build.config")),
                          max(readable_schema_versions("mod-base.build.config"))),
    "repository": Str(g.REPOSITORY, max_len=201),
    "profile": Str(choices=PROFILES),
    "build_adapter_api": Const(BUILD_ADAPTER_API),
    "adapter": Obj({"path": repo_path, "dispatcher": repo_path, "policy": repo_path,
                    "files": List(_FILE, min_items=3, max_items=lim.MAX_CI_ADAPTER_FILES,
                                  unique_by=lambda item: item["path"])}),
    "inventory": _PATH,
    "scenario_contract": _PATH,
    "plan_inputs": List(_PLAN_INPUT, max_items=lim.MAX_CI_PLAN_INPUTS, unique_by=lambda item: item["name"]),
    "bundle": _PATH,
    "contexts": Obj({"build": _context, "packaged": _context}),
    "timeouts": Obj({key: Int(1, lim.MAX_CI_WORKER_TIMEOUT_SECONDS)
                     for key in ("policy_seconds", "target_seconds", "runtime_seconds", "validator_seconds")}),
}, {
    # Optional within schema 1: what a lane job installs on the image before its accounts exist,
    # named, never listed. Absent means none.
    "runtime": Obj({"system_profile": Str(choices=tuple(SYSTEM_PROFILES))}),
})


def validate_build_config(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Data only: no matrix/scenario catalog, shell program, runner, permission, package list or
    secret field."""

    _CONFIG(document, path)
    adapter = document["adapter"]
    entries = [adapter[key] for key in ("path", "dispatcher", "policy")]
    check(len(set(entries)) == len(entries), f"{path}.adapter", "adapter, candidate dispatcher and policy are distinct")
    check(all(entry.startswith("scripts/ci/") and entry.endswith(".py") for entry in entries),
          f"{path}.adapter", "entrypoints must be Python files under protected controller roots")
    inventory = [file["path"] for file in adapter["files"]]
    check(inventory == sorted(inventory), f"{path}.adapter.files", "source inventory must be sorted")
    check(set(entries) <= set(inventory), f"{path}.adapter.files", "all fixed entrypoints must be hash-inventoried")
    extra = [item["name"] for item in document["plan_inputs"]]
    check(extra == sorted(extra), f"{path}.plan_inputs", "extra plan inputs must be sorted by name")
    # One tree holds them all: the config, the protected sources, every candidate file a plan is
    # derived from and the directory the Build is staged into. None may alias, contain or replace
    # another.
    check_output_paths([BUILD_CONFIG_PATH, *inventory, *plan_sources(document).values(),
                        document["bundle"]["path"]], path)
    contexts = document["contexts"]
    check(contexts["build"].casefold() != contexts["packaged"].casefold(), f"{path}.contexts",
          "Build and packaged contexts must be distinct")
    return document


def system_profile(document: dict[str, Any]) -> str | None:
    """The kit system profile a validated config names (``runtime.system_profile``), or ``None``."""

    return document["runtime"]["system_profile"] if "runtime" in document else None


@dataclass(frozen=True)
class AdapterFile:
    """One source of the protected adapter import closure, as read from the protected checkout."""

    path: str
    sha256: str
    data: bytes


#: What decides, beside the Build config and the adapter closure, how the Build and the packaged E2E
#: of a mod execute: its activation manifest and the caller workflows the kit can manage. A mode
#: that leaves one of them to the mod still makes it a file that runs the mod's gates.
CONTROL_PATHS = (ACTIVATION_PATH, *CALLERS)


@dataclass(frozen=True)
class ControlFile:
    """One control file of the protected checkout: its path and the SHA-256 of its bytes, or
    ``None`` for a file the checkout does not have."""

    path: str
    sha256: str | None


@dataclass(frozen=True)
class BuildConfig:
    """A validated protected Build config with the exact bytes of everything it lists.

    ``data`` is the validated document (treat it as read-only), ``raw`` the file's bytes,
    ``files`` the import closure in the config's order, each with its configured hash, and
    ``control`` the state of every path of :data:`CONTROL_PATHS` in the same checkout."""

    data: dict[str, Any]
    raw: bytes
    sha256: str
    files: tuple[AdapterFile, ...]
    control: tuple[ControlFile, ...]


def _protected_file(root: Path, relative: str, *, max_bytes: int) -> bytes:
    """Bytes of one regular file of the protected checkout, reached without crossing a symlink."""

    current = root
    try:
        for part in relative.split("/"):
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise BuildConfigError(f"protected source crosses a symlink: {relative}")
    except OSError as exc:
        raise BuildConfigError(f"protected source is missing: {relative} ({exc.strerror or exc})") from exc
    return read_regular_file(current, label=relative, max_bytes=max_bytes, allow_empty=True)


def _control_file(root: Path, relative: str) -> ControlFile:
    """The state of one control file of the protected checkout. A missing file is a state of its
    own (a mode manages only some callers); what exists must be a regular file, reached without
    crossing a symlink, within the size of a workflow."""

    current = root
    try:
        for part in relative.split("/"):
            current = current / part
            if stat.S_ISLNK(current.lstat().st_mode):
                raise BuildConfigError(f"protected source crosses a symlink: {relative}")
    except (FileNotFoundError, NotADirectoryError):
        return ControlFile(relative, None)
    except OSError as exc:
        raise BuildConfigError(f"protected source cannot be read: {relative} ({exc.strerror or exc})") from exc
    data = read_regular_file(current, label=relative, max_bytes=lim.MAX_WORKFLOW_FILE_BYTES, allow_empty=True)
    return ControlFile(relative, sha256_hex(data))


def load_build_config(repo_root: Path, *, repository: str) -> BuildConfig:
    """Read the protected Build config of ``repository`` and its complete adapter closure.

    ``repo_root`` is the protected mod checkout. Every listed source must be a regular file whose
    SHA-256 is the configured one; sizes are bounded per file and for the whole closure. The
    control files of the same checkout are recorded as they are, present or not."""

    root = Path(repo_root)
    raw = _protected_file(root, BUILD_CONFIG_PATH, max_bytes=lim.MAX_CI_CONFIG_BYTES)
    document = validate_build_config(loads(raw, label=BUILD_CONFIG_PATH, max_bytes=lim.MAX_CI_CONFIG_BYTES))
    if document["repository"] != repository:
        raise BuildConfigError("protected Build config names another repository")
    total = len(raw)
    files = []
    for entry in document["adapter"]["files"]:
        data = _protected_file(root, entry["path"], max_bytes=lim.MAX_CI_ADAPTER_FILE_BYTES)
        total += len(data)
        if total > lim.MAX_CI_ADAPTER_TREE_BYTES:
            raise BuildConfigError("protected adapter closure exceeds its whole-byte cap")
        if sha256_hex(data) != entry["sha256"]:
            raise BuildConfigError(f"protected source differs from its configured hash: {entry['path']}")
        files.append(AdapterFile(entry["path"], entry["sha256"], data))
    control = tuple(_control_file(root, path) for path in CONTROL_PATHS)
    return BuildConfig(document, raw, sha256_hex(raw), tuple(files), control)
