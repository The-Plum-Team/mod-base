"""Synthetic mod: everything the Build hooks know about this mod (``BUILD_ADAPTER_API = 1``).

The mod is a stand-in. Its "release inventory" lists Minecraft targets and their loaders, its
"scenario contract" lists scenarios and checkpoints, a "JAR" is a tiny stored ZIP that embeds the
tested commit, and a "screenshot" is a 16x9 one-colour PNG. Nothing here launches Gradle or
Minecraft; every byte is derived from the inputs, so the same inputs always give the same plan and
the same Build outputs.

Standard library only, Python 3.11 or newer. The dispatcher next to this file is the only caller.
Every JSON input is decoded strictly (no duplicate key, no non-finite number) against a closed
schema of this mod's own; every JSON output is canonical (sorted keys, compact, one final newline).

Failing on purpose: ``faults`` in the release inventory lists ``{"hook", "unit", "mode"}`` objects
(``unit`` is ``null`` for a hook without one). A listed hook run fails the way ``mode`` says:

* ``fail``: exits 1 before it writes anything;
* ``hang``: never finishes (a timeout test);
* ``missing``: finishes, then removes the first file it wrote;
* ``extra``: finishes, then adds ``unplanned.txt`` to its output directory;
* ``orphan``: finishes and leaves a sleeping child process behind (its pid is printed).

A protected hook reads the inventory it was given as input, a candidate hook the one in its own
checkout, so a test selects a failure by changing the candidate's inventory and nothing else.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
import zlib
from collections.abc import Callable
from typing import Any

BUILD_ADAPTER_API = 1
HOOKS = ("derive_plan", "policy", "build_target", "verify_target", "verify_build", "derive_runtime", "run_lane",
         "verify_runtime")
FAULT_MODES = ("fail", "hang", "missing", "extra", "orphan")
LOADERS = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge"}
MAX_INPUT_BYTES = 1 << 20
BUILD_IDENTITY = "META-INF/synthetic-build.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SCREENSHOT_SIZE = (16, 9)


class AdapterError(Exception):
    """An input or an export does not satisfy this mod's native contract."""


# -- Strict JSON ---------------------------------------------------------------------------------------


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _constant(name: str) -> Any:
    raise AdapterError(f"non-finite JSON number {name}")


def decode(data: bytes, label: str) -> Any:
    if not data or len(data) > MAX_INPUT_BYTES:
        raise AdapterError(f"{label} must hold 1..{MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise AdapterError(f"{label} is not strict JSON") from error


def encode(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
            + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or tuple(sorted(value)) != tuple(sorted(keys)):
        raise AdapterError(f"{label} must be an object with exactly {sorted(keys)}")
    return value


def _text(value: Any, label: str, alphabet: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 40 or any(character not in alphabet for character in value):
        raise AdapterError(f"{label} is not a short identifier")
    return value


_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGITS = "0123456789"


def _strings(value: Any, label: str, alphabet: str) -> list[str]:
    if type(value) is not list or not 1 <= len(value) <= 16 or len(set(map(str, value))) != len(value):
        raise AdapterError(f"{label} must list 1..16 distinct values")
    return [_text(item, label, alphabet) for item in value]


# -- Native inputs -------------------------------------------------------------------------------------


def parse_inventory(data: bytes) -> dict[str, Any]:
    """The release inventory: the mod, its targets (one per Minecraft version) and the faults."""

    document = _object(decode(data, "release inventory"), ("schema_version", "mod", "targets", "faults"),
                       "release inventory")
    if document["schema_version"] != 1 or type(document["schema_version"]) is not int:
        raise AdapterError("release inventory schema_version must be 1")
    mod = _object(document["mod"], ("name", "version", "harness_version"), "mod")
    _text(mod["name"], "mod.name", _LOWER + _LOWER.upper() + _DIGITS + " ")
    for key in ("version", "harness_version"):
        _text(mod[key], f"mod.{key}", _DIGITS + ".")
    targets = document["targets"]
    if type(targets) is not list or not 1 <= len(targets) <= 16:
        raise AdapterError("targets must list 1..16 targets")
    for target in targets:
        _object(target, ("minecraft", "java", "loaders"), "target")
        _text(target["minecraft"], "target.minecraft", _DIGITS + ".")
        if type(target["java"]) is not int or not 8 <= target["java"] <= 99:
            raise AdapterError("target.java must be a Java feature version")
        if any(loader not in LOADERS for loader in _strings(target["loaders"], "target.loaders", _LOWER)):
            raise AdapterError("target.loaders names an unknown loader")
    if len({target["minecraft"] for target in targets}) != len(targets):
        raise AdapterError("targets must have distinct Minecraft versions")
    faults = document["faults"]
    if type(faults) is not list or len(faults) > 16:
        raise AdapterError("faults must be a short list")
    for fault in faults:
        _object(fault, ("hook", "unit", "mode"), "fault")
        if fault["hook"] not in HOOKS or fault["mode"] not in FAULT_MODES:
            raise AdapterError("fault names an unknown hook or mode")
        if fault["unit"] is not None:
            _text(fault["unit"], "fault.unit", _LOWER + _DIGITS + ".-")
    return document


def parse_contract(data: bytes) -> dict[str, Any]:
    """The scenario contract: every scenario, the loaders it applies to and its checkpoints."""

    document = _object(decode(data, "scenario contract"), ("schema_version", "scenarios"), "scenario contract")
    if document["schema_version"] != 1 or type(document["schema_version"]) is not int:
        raise AdapterError("scenario contract schema_version must be 1")
    scenarios = document["scenarios"]
    if type(scenarios) is not list or not 1 <= len(scenarios) <= 16:
        raise AdapterError("scenarios must list 1..16 scenarios")
    for scenario in scenarios:
        _object(scenario, ("id", "loaders", "checkpoints"), "scenario")
        _text(scenario["id"], "scenario.id", _LOWER + _DIGITS + "-")
        if any(loader not in LOADERS for loader in _strings(scenario["loaders"], "scenario.loaders", _LOWER)):
            raise AdapterError("scenario.loaders names an unknown loader")
        _strings(scenario["checkpoints"], "scenario.checkpoints", _LOWER + _DIGITS + "-")
    if len({scenario["id"] for scenario in scenarios}) != len(scenarios):
        raise AdapterError("scenarios must have distinct ids")
    return document


def fault(inventory: dict[str, Any], hook: str, unit: str | None) -> str | None:
    """The failure mode the inventory requests for this hook run, if any."""

    for entry in inventory["faults"]:
        if entry["hook"] == hook and entry["unit"] == unit:
            return entry["mode"]
    return None


# -- The plan ------------------------------------------------------------------------------------------


def lane_id(loader: str, minecraft: str) -> str:
    return f"{loader}-{minecraft}"


def target_of(inventory: dict[str, Any], target_id: str) -> dict[str, Any]:
    for target in inventory["targets"]:
        if target["minecraft"] == target_id:
            return target
    raise AdapterError(f"the release inventory has no target {target_id!r}")


def lane_of(inventory: dict[str, Any], lane: str) -> tuple[dict[str, Any], str]:
    """``(target, loader)`` of a lane id."""

    for target in inventory["targets"]:
        for loader in target["loaders"]:
            if lane_id(loader, target["minecraft"]) == lane:
                return target, loader
    raise AdapterError(f"the release inventory has no lane {lane!r}")


def lane_outputs(inventory: dict[str, Any], target: dict[str, Any], loader: str) -> list[dict[str, str]]:
    """The files one lane's Build must stage, named like the real mods' staged release."""

    mod, minecraft, lane = inventory["mod"], target["minecraft"], lane_id(loader, target["minecraft"])
    names = {
        "production": f"files/{mod['name']} - {LOADERS[loader]} - {minecraft}-{mod['version']}.jar",
        "harness": f"harness/{mod['name']} E2E - {LOADERS[loader]} - {minecraft}-{mod['harness_version']}.jar",
        "sbom": f"sbom/{lane}.cdx.json",
        "native-report": f"reports/{lane}.json",
        "build-log": f"logs/{lane}.log",
    }
    return [{"path": path, "lane_id": lane, "role": role} for role, path in names.items()]


def scenarios_of(contract: dict[str, Any], loader: str) -> list[dict[str, Any]]:
    return [scenario for scenario in contract["scenarios"] if loader in scenario["loaders"]]


def obligation(scenario: str, checkpoint: str) -> str:
    return f"scenario/{scenario}/{checkpoint}"


def target_contract(inventory: dict[str, Any], target: dict[str, Any]) -> str:
    return sha256(encode({"contract": "synthetic-target-v1", "mod": inventory["mod"], "target": target}))


def lane_contract(contract: dict[str, Any], target: dict[str, Any], loader: str) -> str:
    return sha256(encode({"contract": "synthetic-lane-v1", "lane": lane_id(loader, target["minecraft"]),
                          "java": target["java"], "scenarios": scenarios_of(contract, loader)}))


def derive_plan(inventory: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    """Targets and lanes in the kit's plan shape: one target per Minecraft version, one lane per
    loader of it, and one obligation per checkpoint of every scenario the loader runs."""

    targets, lanes = [], []
    for target in inventory["targets"]:
        outputs = []
        for loader in target["loaders"]:
            scenarios = scenarios_of(contract, loader)
            if not scenarios:
                raise AdapterError(f"no scenario covers loader {loader!r}")
            outputs.extend(lane_outputs(inventory, target, loader))
            lanes.append({"id": lane_id(loader, target["minecraft"]), "target_id": target["minecraft"],
                          "native_contract_sha256": lane_contract(contract, target, loader),
                          "obligations": [obligation(scenario["id"], checkpoint) for scenario in scenarios
                                          for checkpoint in scenario["checkpoints"]]})
        targets.append({"id": target["minecraft"], "java": target["java"],
                        "native_contract_sha256": target_contract(inventory, target), "outputs": outputs})
    return {"targets": targets, "lanes": lanes}


def runtime_row(inventory: dict[str, Any], lane: str) -> dict[str, Any]:
    """The row of one lane: what its runtime needs to know about the staged Build."""

    target, loader = lane_of(inventory, lane)
    outputs = {output["role"]: output["path"] for output in lane_outputs(inventory, target, loader)}
    return {"id": lane, "loader": loader, "minecraft": target["minecraft"], "java": target["java"],
            "production": outputs["production"], "harness": outputs["harness"]}


def runtime_values(inventory: dict[str, Any], contract: dict[str, Any], lane: str) -> dict[str, str]:
    """What ``run_lane`` receives: the lane's row and the scenarios it must run."""

    loader = lane_of(inventory, lane)[1]
    return {"E2E_ROW_JSON": encode(runtime_row(inventory, lane)).decode("utf-8").rstrip("\n"),
            "E2E_SCENARIOS": ",".join(scenario["id"] for scenario in scenarios_of(contract, loader))}


# -- Build outputs -------------------------------------------------------------------------------------


def jar(identity: dict[str, Any], payload: bytes) -> bytes:
    """A deterministic stored ZIP: the embedded build identity and one payload entry."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        for name, data in ((BUILD_IDENTITY, encode(identity)), ("synthetic/payload.txt", payload)):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data)
    return stream.getvalue()


def jar_identity(data: bytes, label: str) -> dict[str, Any]:
    """The build identity a JAR embeds; a damaged archive is a rejection."""

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            intact = archive.testzip() is None and archive.namelist() == [BUILD_IDENTITY, "synthetic/payload.txt"]
            embedded = archive.read(BUILD_IDENTITY) if intact else b""
    except (zipfile.BadZipFile, zlib.error, struct.error, OSError, KeyError, ValueError, EOFError,
            NotImplementedError, RuntimeError) as error:
        raise AdapterError(f"{label} is not an intact synthetic JAR") from error
    if not intact:
        raise AdapterError(f"{label} is not an intact synthetic JAR")
    return decode(embedded, f"{label} build identity")


def jar_versions(inventory: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """The two JARs of a lane: its role and the version its name and identity carry."""

    return (("production", inventory["mod"]["version"]), ("harness", inventory["mod"]["harness_version"]))


def build_lane(inventory: dict[str, Any], target: dict[str, Any], loader: str, *, tested_sha: str,
               tested_tree: str, source: bytes) -> dict[str, bytes]:
    """Every staged file of one lane, by export path."""

    lane = lane_id(loader, target["minecraft"])
    outputs = {output["role"]: output["path"] for output in lane_outputs(inventory, target, loader)}
    files: dict[str, bytes] = {}
    for role, version in jar_versions(inventory):
        identity = {"mod": inventory["mod"]["name"], "version": version, "role": role, "lane": lane,
                    "tested_sha": tested_sha, "tested_tree": tested_tree}
        files[outputs[role]] = jar(identity, source + f"{role} for {lane}\n".encode("utf-8"))
    files[outputs["sbom"]] = encode({"bomFormat": "CycloneDX", "specVersion": "1.5", "components": [
        {"type": "library", "name": inventory["mod"]["name"], "version": inventory["mod"]["version"]}]})
    files[outputs["build-log"]] = f"synthetic build of {lane} with Java {target['java']}\n".encode("utf-8")
    staged = [{"path": path, "sha256": sha256(data), "size": len(data)} for path, data in sorted(files.items())]
    files[outputs["native-report"]] = encode({"schema_version": 1, "lane": lane, "tested_sha": tested_sha,
                                             "tested_tree": tested_tree, "files": staged})
    return files


def verify_lane(inventory: dict[str, Any], target: dict[str, Any], loader: str, *, tested_sha: str,
                tested_tree: str, read: Callable[[str], bytes]) -> list[dict[str, Any]]:
    """Check one lane's staged files (``read(path) -> bytes``) and return their inventory."""

    lane = lane_id(loader, target["minecraft"])
    outputs = {output["role"]: output["path"] for output in lane_outputs(inventory, target, loader)}
    raw = read(outputs["native-report"])
    report = _object(decode(raw, "native report"), ("schema_version", "lane", "tested_sha", "tested_tree", "files"),
                     "native report")
    if encode(report) != raw or (report["schema_version"], report["lane"], report["tested_sha"],
                                 report["tested_tree"]) != (1, lane, tested_sha, tested_tree):
        raise AdapterError(f"the native report of {lane} is not canonical or names another build")
    expected = sorted(path for role, path in outputs.items() if role != "native-report")
    if type(report["files"]) is not list or [entry.get("path") if type(entry) is dict else None
                                              for entry in report["files"]] != expected:
        raise AdapterError(f"the native report of {lane} does not list exactly its staged files")
    for entry in report["files"]:
        data = read(_object(entry, ("path", "sha256", "size"), "native report file")["path"])
        if (sha256(data), len(data)) != (entry["sha256"], entry["size"]):
            raise AdapterError(f"{entry['path']} differs from the native report of {lane}")
    for role, version in jar_versions(inventory):
        identity = jar_identity(read(outputs[role]), outputs[role])
        if identity != {"mod": inventory["mod"]["name"], "version": version, "role": role, "lane": lane,
                        "tested_sha": tested_sha, "tested_tree": tested_tree}:
            raise AdapterError(f"{outputs[role]} embeds another build identity")
    sbom = decode(read(outputs["sbom"]), "SBOM")
    if type(sbom) is not dict or sbom.get("bomFormat") != "CycloneDX":
        raise AdapterError(f"the SBOM of {lane} is not a CycloneDX document")
    return report["files"] + [{"path": outputs["native-report"], "sha256": sha256(raw), "size": len(raw)}]


# -- Runtime results -----------------------------------------------------------------------------------


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def screenshot(text: str) -> bytes:
    """A 16x9 PNG whose colour is derived from ``text``."""

    width, height = SCREENSHOT_SIZE
    colour = hashlib.sha256(text.encode("utf-8")).digest()[:3]
    rows = b"".join(b"\x00" + colour * width for _ in range(height))
    return (PNG_SIGNATURE + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(rows, 9)) + _chunk(b"IEND", b""))


def check_screenshot(data: bytes, label: str) -> None:
    """Decode a screenshot completely: signature, chunk checksums, header and pixel data."""

    width, height = SCREENSHOT_SIZE
    if data[:8] != PNG_SIGNATURE:
        raise AdapterError(f"{label} is not a PNG")
    position, chunks = 8, []
    while position < len(data):
        if position + 12 > len(data):
            raise AdapterError(f"{label} has a truncated chunk")
        (length,) = struct.unpack(">I", data[position:position + 4])
        tag, body = data[position + 4:position + 8], data[position + 8:position + 8 + length]
        checksum = data[position + 8 + length:position + 12 + length]
        if len(body) != length or checksum != struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF):
            raise AdapterError(f"{label} has a damaged chunk")
        chunks.append((tag, body))
        position += 12 + length
    if [tag for tag, _ in chunks] != [b"IHDR", b"IDAT", b"IEND"]:
        raise AdapterError(f"{label} is not a synthetic screenshot")
    if chunks[0][1] != struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0):
        raise AdapterError(f"{label} has another size or pixel format")
    try:
        pixels = zlib.decompress(chunks[1][1])
    except zlib.error as error:
        raise AdapterError(f"{label} has damaged pixel data") from error
    if len(pixels) != height * (1 + 3 * width):
        raise AdapterError(f"{label} has the wrong amount of pixel data")


def run_lane(lane: str, row: dict[str, Any], scenarios: list[dict[str, Any]], *, tested_sha: str,
             production: bytes) -> dict[str, bytes]:
    """The native results of one lane, by export path: a result report, a log and one screenshot
    per checkpoint."""

    root = f"lanes/{lane}"
    files: dict[str, bytes] = {}
    checks = []
    for scenario in scenarios:
        for checkpoint in scenario["checkpoints"]:
            name = f"screenshots/{scenario['id']}-{checkpoint}.png"
            image = screenshot(f"{lane}/{scenario['id']}/{checkpoint}")
            files[f"{root}/{name}"] = image
            checks.append({"obligation": obligation(scenario["id"], checkpoint), "status": "pass",
                           "screenshot": name, "sha256": sha256(image), "size": len(image)})
    log = f"synthetic client of {lane} ran {len(checks)} checkpoints\n".encode("utf-8")
    files[f"{root}/logs/client.log"] = log
    files[f"{root}/result.json"] = encode({
        "schema_version": 1, "lane": lane, "row": row, "tested_sha": tested_sha,
        "production_sha256": sha256(production), "checks": checks,
        "log": {"path": "logs/client.log", "sha256": sha256(log), "size": len(log)}})
    return files


def verify_run(lane: str, obligations: list[str], row: dict[str, Any], *, tested_sha: str, production: bytes,
               read: Callable[[str], bytes]) -> dict[str, Any]:
    """Check one lane's native results (``read(path below lanes/<lane>) -> bytes``) against the
    plan's obligations, the lane's row and the sealed production JAR; return what the report records."""

    raw = read("result.json")
    result = _object(decode(raw, "result report"),
                     ("schema_version", "lane", "row", "tested_sha", "production_sha256", "checks", "log"),
                     "result report")
    if encode(result) != raw or (result["schema_version"], result["lane"], result["row"], result["tested_sha"]) != (
            1, lane, row, tested_sha):
        raise AdapterError(f"the result report of {lane} is not canonical or names another run")
    if result["production_sha256"] != sha256(production):
        raise AdapterError(f"{lane} did not run the sealed production JAR")
    checks = result["checks"]
    if type(checks) is not list or [check.get("obligation") if type(check) is dict else None
                                     for check in checks] != obligations:
        raise AdapterError(f"{lane} did not run exactly its planned obligations")
    for check in checks:
        _object(check, ("obligation", "status", "screenshot", "sha256", "size"), "check")
        image = read(check["screenshot"])
        if check["status"] != "pass" or (sha256(image), len(image)) != (check["sha256"], check["size"]):
            raise AdapterError(f"{check['obligation']} of {lane} did not pass with its recorded screenshot")
        check_screenshot(image, check["screenshot"])
    log = _object(result["log"], ("path", "sha256", "size"), "log")
    if log["path"] != "logs/client.log" or (sha256(read(log["path"])), len(read(log["path"]))) != (
            log["sha256"], log["size"]):
        raise AdapterError(f"the client log of {lane} differs from its result report")
    return {"obligations": obligations, "production_sha256": result["production_sha256"],
            "result_sha256": sha256(raw),
            "files": sorted(["result.json", log["path"], *(check["screenshot"] for check in checks)])}
