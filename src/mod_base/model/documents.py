"""Strict validators for every document kind of SPEC §3 (and the §4.1 config envelope).

Each ``validate_<kind>(document, ...)`` takes already strictly decoded JSON (see
:func:`mod_base.model.canonical.strict_loads`: no duplicate keys, no non-finite numbers), checks
exact keys, types, grammar and bounds, then every *purely structural* cross-reference the spec
lists for that kind (unique ids, references that resolve, ordering, internal equalities). It
returns the same object on success and raises :class:`~mod_base.model.validators.DocumentError`
(an :class:`~mod_base.errors.MbError`, exit 2) naming the exact failing path otherwise.

Checks that need bytes on disk (hashes of files, image decoding) or live GitHub state belong to
the owning units (``evidence``, ``family``, ``pages``); they call these validators first. Optional
keyword arguments pass in the few external facts a pure validator can compare against (the
embedded expectation, the configured image policy, the configured extension names).

Every document is ``{"kind": "mod-base.<...>", "schema_version": N, ...}``; readers accept the
versions of :func:`mod_base.readable_schema_versions` (N and N-1).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Collection, Mapping
from typing import Any

from mod_base import KIT_REPOSITORY, PIXEL_METRICS_VERSION, readable_schema_versions
from mod_base.errors import MbError
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, canonical_sha256, sha256_hex, strict_loads
from mod_base.model.validators import (
    Bool,
    Const,
    DocumentError,
    Int,
    List,
    Map,
    Null,
    Nullable,
    Num,
    Obj,
    OneOf,
    Region,
    Size,
    Str,
    Validator,
    check,
    fail,
)
from mod_base.workflow import PAGES_WORKFLOW_PATH

__all__ = [
    "DocumentError",
    "VALIDATORS",
    "MAX_DOCUMENT_BYTES",
    "load_document",
    "validate_document",
    "validate_expectation",
    "validate_handoff",
    "validate_compact",
    "validate_anchor",
    "validate_family_envelope",
    "validate_family_paired",
    "validate_selection",
    "validate_promotion",
    "validate_build",
    "validate_site",
    "validate_gallery",
    "validate_template_manifest",
    "validate_kit_stamp",
    "compact_identity_sha256",
    "check_compact_selection",
    "run_record",
    "own_run_record",
    "thumbnail_size",
    "inventory_sha256",
    "is_https_url",
    "run_claim_from_environment",
]

# -- Field validators ------------------------------------------------------------------------------

SHA1 = Str(g.SHA1, max_len=40)
SHA256 = Str(g.SHA256, max_len=64)
DIGEST = Str(g.DIGEST, max_len=71)
KEY = Str(g.KEY, max_len=64)
FAMILY = Str(g.FAMILY, max_len=g.MAX_FAMILY_LENGTH)
LANE_ID = Str(g.LANE_ID, max_len=g.MAX_LANE_ID_LENGTH)
BRANCH = Str(g.BRANCH, max_len=200)
REPOSITORY = Str(g.REPOSITORY, max_len=201)
IDENT = Str(g.IDENT, max_len=200)
ARTIFACT_NODE = Str(g.ARTIFACT_NODE, max_len=80)
MINECRAFT = Str(g.MINECRAFT, max_len=40)
LOADER = Str(g.LOADER, max_len=32)
SCENARIO = Str(g.SCENARIO, max_len=80)
ROLE = Str(g.ROLE, max_len=40)
STEP = Str(g.STEP, max_len=120)
REVIEW_TIER = Str(g.REVIEW_TIER, max_len=40)
PROFILE = Str(g.PROFILE, max_len=lim.MAX_PROFILE_LENGTH)
EVENT = Str(g.EVENT, max_len=40)
EXTENSION_NAME = Str(g.EXTENSION_NAME, max_len=g.MAX_EXTENSION_NAME_LENGTH)
VERSION = Str(g.VERSION, max_len=20)
WORKFLOW_PATH = Str(g.WORKFLOW_PATH, max_len=130)
RUN_ID = Int(1, lim.MAX_RUN_ID)
RUN_ATTEMPT = Int(1, lim.MAX_RUN_ATTEMPT)
JAVA = Int(8, 99)
COUNT = Int(0, 1_000_000)
RUN_URL = Str(g.RUN_URL, max_len=260)
TITLE = Str(max_len=lim.MAX_TITLE_LENGTH, text="evidence")
EXPECTATION_TEXT = Str(max_len=lim.MAX_EXPECTATION_TEXT_LENGTH, text="evidence")
RUNTIME_EVIDENCE = Str(max_len=lim.MAX_RUNTIME_EVIDENCE_LENGTH, text="evidence")
LABEL = Str(max_len=lim.MAX_LABEL_LENGTH, text="display")
REASON = Str(max_len=lim.MAX_REASON_LENGTH, text="evidence")
DISPLAY_TITLE = Str(max_len=lim.MAX_DISPLAY_TITLE_LENGTH, text="evidence")
REUSE = Str(choices=("none", "attested", "delegated"))


def _bundle_path(max_len: int = lim.MAX_BUNDLE_PATH_CHARS) -> Validator:
    def validate(value: Any, path: str) -> str:
        if not g.is_bundle_path(value) or len(value) > max_len:
            raise fail(path, "must be a canonical bundle-relative path")
        return value

    return validate


def _repo_path() -> Validator:
    def validate(value: Any, path: str) -> str:
        if not g.is_repo_path(value):
            raise fail(path, "must be a canonical repository-relative path")
        return value

    return validate


def _timestamp() -> Validator:
    def validate(value: Any, path: str) -> str:
        try:
            g.parse_timestamp(value, path)
        except MbError as exc:
            raise fail(path, "must be an RFC 3339 UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)") from exc
        return value

    return validate


def _https_url(max_len: int = 300) -> Validator:
    def validate(value: Any, path: str) -> str:
        if not is_https_url(value) or len(value) > max_len:
            raise fail(path, "must be an https URL without userinfo, port or whitespace")
        return value

    return validate


def is_https_url(value: Any) -> bool:
    """https, lowercase ``[a-z0-9.-]`` host with a dot, no userinfo/port, printable ASCII only."""

    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 2048:
        return False
    if any(ord(character) <= 32 or ord(character) >= 127 or character in '"<>\\^`{|}' for character in value):
        return False
    rest = value[len("https://"):]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if not host or "@" in host or ":" in host or host.startswith((".", "-")) or host.endswith((".", "-")):
        return False
    if ".." in host or "." not in host:
        return False
    return all(character in "abcdefghijklmnopqrstuvwxyz0123456789.-" for character in host)


def _json_object(value: Any, path: str) -> dict[str, Any]:
    """Any JSON object (adapter-defined content); callers bound its canonical size."""

    if not isinstance(value, dict):
        raise fail(path, "must be an object")
    return value


JSON_OBJECT = _json_object
TIMESTAMP = _timestamp()
BUNDLE_PATH = _bundle_path()
REPO_PATH = _repo_path()


def _schema_version(kind: str) -> Validator:
    accepted = readable_schema_versions(kind)

    def validate(value: Any, path: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value not in accepted:
            raise fail(path, f"must be one of the readable {kind} versions {sorted(accepted)}")
        return value

    return validate


def _header(kind: str) -> dict[str, Validator]:
    return {"kind": Const(kind), "schema_version": _schema_version(kind)}


PIXEL_METRICS = Obj(
    {
        "width": Int(1, lim.MAX_IMAGE_DIMENSION),
        "height": Int(1, lim.MAX_IMAGE_DIMENSION),
        "file_sha256": SHA256,
        "pixel_sha256": SHA256,
        "luma_entropy": Num(0.0, 8.0),
        "meaningful_colors": Int(0, 32),
        "dark_fraction": Num(0.0, 1.0),
        "light_fraction": Num(0.0, 1.0),
    }
)

COMPARE_METRICS = Obj(
    {
        "changed_fraction": Num(0.0, 1.0),
        "rms_difference": Num(0.0, 255.0),
        "required_changed_fraction": Num(0.0, 1.0),
    },
    {"region": Region()},
)

RUN_CLAIM_FIELDS: dict[str, Validator] = {
    "run_id": RUN_ID,
    "run_attempt": RUN_ATTEMPT,
    "workflow_path": WORKFLOW_PATH,
    "branch": BRANCH,
    "commit": SHA1,
    "controller_branch": BRANCH,
    "controller_sha": SHA1,
}
RUN_CLAIM = Obj(RUN_CLAIM_FIELDS)
RUN_RECORD_REQUIRED: dict[str, Validator] = {
    **RUN_CLAIM_FIELDS,
    "event": EVENT,
    "created_at": TIMESTAMP,
    "conclusion": Const("success"),
    "head_sha": SHA1,
}
RUN_RECORD_OPTIONAL: dict[str, Validator] = {"display_title": DISPLAY_TITLE, "job_graph_sha256": SHA256}
_RUN_RECORD = Obj(RUN_RECORD_REQUIRED, RUN_RECORD_OPTIONAL)
KIT_REF = Obj({"repository": Const(KIT_REPOSITORY), "sha": SHA1, "version": VERSION})
SUBJECT = Obj({"branch": BRANCH, "commit": SHA1, "tree": SHA1})
FILE_RECORD = Obj({"path": BUNDLE_PATH, "sha256": SHA256, "size": Int(1, lim.MAX_RAW_BUNDLE_BYTES)})
IMAGE_POLICY = Obj(
    {
        "source_size": Size(1, lim.MAX_IMAGE_DIMENSION),
        "derivative_box": Size(1, lim.MAX_IMAGE_DIMENSION),
        "webp_quality": Int(1, 100),
        "webp_method": Int(0, 6),
        "pixel_metrics_version": Const(PIXEL_METRICS_VERSION),
    }
)
FAMILY_IMAGE_POLICY = Obj(
    {"derivative_box": Size(1, lim.MAX_IMAGE_DIMENSION), "webp_quality": Int(1, 100), "webp_method": Int(0, 6)}
)
JARS = Obj({"production_sha256": SHA256}, {"harness_sha256": SHA256})


def run_record(value: Any, path: str) -> dict[str, Any]:
    """A RunRecord (SPEC §3.0): a RunClaim plus facts read from the run API. ``head_sha`` is the
    run's API head. A run that is its own controller validates with :func:`own_run_record`; a
    tested run of ``none``/``attested`` reuse has ``head_sha == controller_sha``
    (:func:`validate_selection`); only a ``delegated`` tested run's head is unconstrained (Quick
    Skin PR reuse tests a merge commit that is not the PR run's head; the reuse is proven by the
    adapter's ``authenticate_extensions``)."""

    return _RUN_RECORD(value, path)


def own_run_record(value: Any, path: str) -> dict[str, Any]:
    """A RunRecord of a run that is its own controller (a handoff or family producer run):
    ``head_sha == commit == controller_sha`` and ``branch == controller_branch`` (SPEC §4.8)."""

    _RUN_RECORD(value, path)
    check(value["controller_sha"] == value["commit"] and value["controller_branch"] == value["branch"], path,
          "a handoff or producer run is its own controller")
    check(value["head_sha"] == value["commit"], f"{path}.head_sha", "must equal the run's own commit")
    return value


_TESTED_FRAME_RECORD = Obj({**RUN_RECORD_REQUIRED, "jar_sha256": SHA256}, RUN_RECORD_OPTIONAL)


# -- Pure helpers ----------------------------------------------------------------------------------


def thumbnail_size(source: tuple[int, int] | list[int], box: tuple[int, int] | list[int]) -> tuple[int, int]:
    """Return the size Pillow 12.3 ``Image.thumbnail(box)`` produces for an image of ``source`` size.

    This is a verbatim port of ``Image.thumbnail``'s ``preserve_aspect_ratio`` so the model can
    check derivative dimensions without decoding. An image already inside the box is unchanged.
    """

    width, height = int(source[0]), int(source[1])
    x, y = int(box[0]), int(box[1])
    if width <= 0 or height <= 0 or x <= 0 or y <= 0:
        raise MbError("thumbnail dimensions must be positive")
    if x >= width and y >= height:
        return width, height
    aspect = width / height

    def round_aspect(number: float, key: Callable[[int], float]) -> int:
        return max(min(math.floor(number), math.ceil(number), key=key), 1)

    if x / y >= aspect:
        x = round_aspect(y * aspect, key=lambda n: abs(aspect - n / y))
    else:
        y = round_aspect(x / aspect, key=lambda n: 0 if n == 0 else abs(aspect - x / n))
    return x, y


def inventory_sha256(records: list[Mapping[str, Any]]) -> str:
    """Identity of a file inventory: SHA-256 of ``canonical_json`` of ``[{path, sha256, size}]``
    sorted by path. Used for ``promotion.site.inventory_sha256`` and ``build.site_inventory_sha256``
    (the published site inventory excludes ``build.json`` itself)."""

    normalized = sorted(({"path": r["path"], "sha256": r["sha256"], "size": r["size"]} for r in records),
                        key=lambda record: record["path"])
    for index in range(1, len(normalized)):
        if normalized[index - 1]["path"] == normalized[index]["path"]:
            raise MbError(f"inventory lists {normalized[index]['path']!r} twice")
    return canonical_sha256(normalized)


def run_claim_from_environment(environ: Mapping[str, str]) -> dict[str, Any]:
    """Build the handoff ``RunClaim`` of the executing job from GitHub's environment (no API call).

    Uses ``GITHUB_REPOSITORY``, ``GITHUB_RUN_ID``, ``GITHUB_RUN_ATTEMPT``, ``GITHUB_SHA``,
    ``GITHUB_REF_NAME`` and ``GITHUB_WORKFLOW_REF``. The workflow ref must name the same repository
    and branch. A handoff run is its own controller, so ``controller_*`` equal ``branch``/``commit``.
    """

    def need(name: str) -> str:
        value = environ.get(name)
        if not isinstance(value, str) or not value:
            raise MbError(f"{name} is not set", reason="environment")
        return value

    repository = g.require(g.REPOSITORY, need("GITHUB_REPOSITORY"), "GITHUB_REPOSITORY")
    run_id_text, attempt_text = need("GITHUB_RUN_ID"), need("GITHUB_RUN_ATTEMPT")
    if not g.POSITIVE_DECIMAL.fullmatch(run_id_text) or not g.POSITIVE_DECIMAL.fullmatch(attempt_text):
        raise MbError("GITHUB_RUN_ID and GITHUB_RUN_ATTEMPT must be positive decimals", reason="environment")
    commit = g.require_sha1(need("GITHUB_SHA"), "GITHUB_SHA")
    branch = g.require(g.BRANCH, need("GITHUB_REF_NAME"), "GITHUB_REF_NAME")
    ref = g.parse_workflow_ref(need("GITHUB_WORKFLOW_REF"))
    if ref.repository != repository or ref.branch != branch:
        raise MbError("GITHUB_WORKFLOW_REF does not name this repository and branch", reason="environment")
    claim = {
        "run_id": int(run_id_text),
        "run_attempt": int(attempt_text),
        "workflow_path": ref.path,
        "branch": branch,
        "commit": commit,
        "controller_branch": branch,
        "controller_sha": commit,
    }
    RUN_CLAIM(claim, "$env")
    return claim


# -- Shared evidence-body checks -------------------------------------------------------------------

EXPECTATION_REF = Obj({"path": Const("expectation.json"), "sha256": SHA256, "size": Int(1, lim.MAX_EXPECTATION_BYTES)})
EXTENSIONS_REF = Obj(
    {
        "path": Const("extensions.json"),
        "sha256": SHA256,
        "size": Int(1, lim.MAX_EXTENSIONS_BYTES),
        "names": List(EXTENSION_NAME, min_items=1, max_items=lim.MAX_EXTENSION_NAMES, sorted_values=True),
    }
)
SELECTION_REF = Obj({"path": Const("selection.json"), "sha256": SHA256, "size": Int(1, lim.MAX_SELECTION_BYTES)})

LANE_FIELDS: dict[str, Validator] = {
    "lane_id": LANE_ID,
    "artifact_node": ARTIFACT_NODE,
    "minecraft": MINECRAFT,
    "loader": LOADER,
    "java": JAVA,
    "scenario": SCENARIO,
    "roles": List(ROLE, min_items=1, max_items=lim.MAX_ROLES_PER_LANE, unique=True),
}
EVIDENCE_LANE = Obj(
    {**LANE_FIELDS, "profile": PROFILE, "status": Const("pass"), "jars": JARS},
    {"elapsed_s": Num(0.0, 10_000_000.0)},
)
#: The run fields of one lane execution, the lane fields of ``EVIDENCE_LANE`` aside: what a composed
#: lane's optional ``baseline_run`` records for the ``epoch: baseline`` frames of a re-tested lane.
_LANE_RUN = Obj({"profile": PROFILE, "status": Const("pass"), "jars": JARS}, {"elapsed_s": Num(0.0, 10_000_000.0)})
_COMPACT_LANE = Obj(
    {**LANE_FIELDS, "profile": PROFILE, "status": Const("pass"), "jars": JARS},
    {"elapsed_s": Num(0.0, 10_000_000.0), "baseline_run": _LANE_RUN},
)
CAPTURE_FIELDS: dict[str, Validator] = {
    "frame_id": IDENT,
    "capture_id": IDENT,
    "capture_order": Int(0, 1_000_000),
    "lane_id": LANE_ID,
    "role": ROLE,
    "step": STEP,
    "title": TITLE,
    "expectation": EXPECTATION_TEXT,
    "review_tier": REVIEW_TIER,
}
FRAME_FIELDS: dict[str, Validator] = {
    **CAPTURE_FIELDS,
    "artifact_node": ARTIFACT_NODE,
    "minecraft": MINECRAFT,
    "loader": LOADER,
    "scenario": SCENARIO,
    "runtime_evidence": RUNTIME_EVIDENCE,
}
COMPARISON_FIELDS: dict[str, Validator] = {
    "comparison_id": IDENT,
    "lane_id": LANE_ID,
    "role": ROLE,
    "first_frame_id": IDENT,
    "second_frame_id": IDENT,
    "minimum_changed_fraction": Num(0.0, 1.0),
}
PNG_SOURCE_BASE: dict[str, Validator] = {
    "sha256": SHA256,
    "size": Int(1, lim.MAX_SOURCE_PNG_BYTES),
    "width": Int(1, lim.MAX_IMAGE_DIMENSION),
    "height": Int(1, lim.MAX_IMAGE_DIMENSION),
    "format": Const("png"),
    "pixel": PIXEL_METRICS,
}


def _check_pixels(image: Mapping[str, Any], path: str) -> None:
    """``width``/``height`` equal the metrics, ``pixel.file_sha256`` equals the image's ``sha256``
    and the image stays under the pixel bound."""

    pixel = image["pixel"]
    check(pixel["width"] == image["width"] and pixel["height"] == image["height"], f"{path}.pixel",
          "dimensions must equal the recorded width and height")
    check(pixel["width"] * pixel["height"] <= lim.MAX_IMAGE_PIXELS, f"{path}.pixel", "exceeds the pixel bound")
    check(pixel["file_sha256"] == image["sha256"], f"{path}.pixel.file_sha256", "must equal sha256")


def _check_compare(metrics: Mapping[str, Any], comparison: Mapping[str, Any], path: str) -> None:
    check(metrics["required_changed_fraction"] == comparison["minimum_changed_fraction"],
          f"{path}.required_changed_fraction", "must equal the comparison's minimum_changed_fraction")
    check(metrics["changed_fraction"] >= comparison["minimum_changed_fraction"], f"{path}.changed_fraction",
          "is below the required minimum")
    check(metrics.get("region") == comparison.get("region"), f"{path}.region", "must equal the comparison region")


def _index_unique(items: list[Mapping[str, Any]], field: str, path: str) -> dict[Any, Mapping[str, Any]]:
    index: dict[Any, Mapping[str, Any]] = {}
    for position, item in enumerate(items):
        value = item[field]
        check(value not in index, f"{path}[{position}].{field}", f"duplicates {field} {value!r}")
        index[value] = item
    return index


def _check_capture_order(frames: list[Mapping[str, Any]], path: str) -> None:
    last: dict[tuple[str, str], int] = {}
    seen_capture: set[tuple[str, str]] = set()
    for position, frame in enumerate(frames):
        group = (frame["lane_id"], frame["role"])
        order = frame["capture_order"]
        check(group not in last or order > last[group], f"{path}[{position}].capture_order",
              "must strictly increase within its (lane_id, role)")
        last[group] = order
        capture = (frame["lane_id"], frame["capture_id"])
        check(capture not in seen_capture, f"{path}[{position}].capture_id", "duplicates a capture of its lane")
        seen_capture.add(capture)


def _check_frames_against_lanes(frames: list[Mapping[str, Any]], lanes: Mapping[str, Mapping[str, Any]],
                                path: str, *, lane_fields: tuple[str, ...]) -> None:
    for position, frame in enumerate(frames):
        lane = lanes.get(frame["lane_id"])
        check(lane is not None, f"{path}[{position}].lane_id", "references no lane")
        check(frame["role"] in lane["roles"], f"{path}[{position}].role", "is not a role of its lane")
        for field in lane_fields:
            check(frame[field] == lane[field], f"{path}[{position}].{field}", "must equal its lane's value")


def _check_comparisons(comparisons: list[Mapping[str, Any]], frames: Mapping[str, Mapping[str, Any]],
                       lanes: Mapping[str, Mapping[str, Any]], path: str,
                       *, metrics_keys: tuple[str, ...] = ()) -> None:
    _index_unique(comparisons, "comparison_id", path)
    for position, comparison in enumerate(comparisons):
        here = f"{path}[{position}]"
        lane = lanes.get(comparison["lane_id"])
        check(lane is not None, f"{here}.lane_id", "references no lane")
        check(comparison["role"] in lane["roles"], f"{here}.role", "is not a role of its lane")
        check(comparison["first_frame_id"] != comparison["second_frame_id"], here, "compares a frame with itself")
        for field in ("first_frame_id", "second_frame_id"):
            frame = frames.get(comparison[field])
            check(frame is not None, f"{here}.{field}", "references no frame")
            check(frame["lane_id"] == comparison["lane_id"] and frame["role"] == comparison["role"],
                  f"{here}.{field}", "must reference a frame of the same lane and role")
        for key in metrics_keys:
            _check_compare(comparison[key], comparison, f"{here}.{key}")


def _check_file_inventory(files: list[Mapping[str, Any]], path: str, *, max_files: int, max_total: int) -> dict[str, Mapping[str, Any]]:
    check(len(files) <= max_files, path, f"lists more than {max_files} files")
    by_path: dict[str, Mapping[str, Any]] = {}
    previous = ""
    total = 0
    for position, record in enumerate(files):
        name = record["path"]
        check(name > previous, f"{path}[{position}].path", "files must be sorted by path without duplicates")
        check(name != "manifest.json", f"{path}[{position}].path", "must not list manifest.json")
        previous = name
        by_path[name] = record
        total += record["size"]
    check(total <= max_total, path, f"total size exceeds {max_total} bytes")
    return by_path


def _check_file_ref(files: Mapping[str, Mapping[str, Any]], ref: Mapping[str, Any], path: str) -> None:
    record = files.get(ref["path"])
    check(record is not None, path, f"{ref['path']} is missing from files")
    check(record["sha256"] == ref["sha256"] and record["size"] == ref["size"], path,
          f"sha256/size must equal the files record of {ref['path']}")


def _check_same_run_claims(tested: Mapping[str, Any], handoff: Mapping[str, Any], subject: Mapping[str, Any],
                           path: str) -> None:
    """SPEC §3.2 rule 5, ``reuse: "none"``: the tested claim *is* the handoff run, "apart from
    controller fields". Read with the Block Pops controller split (``authenticate_source.py``
    non-attested lineage): the tested claim's ``branch``/``commit`` name what the run tested, which
    is exactly the subject, while its ``controller_*`` name the run's own head, which is the handoff
    claim's ``branch``/``commit`` (a handoff run is its own controller)."""

    check((tested["run_id"], tested["run_attempt"], tested["workflow_path"])
          == (handoff["run_id"], handoff["run_attempt"], handoff["workflow_path"]), path,
          "reuse 'none' requires the tested run to be the handoff run (id, attempt and workflow)")
    check(tested["branch"] == subject["branch"] and tested["commit"] == subject["commit"], path,
          "reuse 'none' requires the tested branch and commit to be the subject's")
    check(tested["controller_branch"] == handoff["branch"] and tested["controller_sha"] == handoff["commit"], path,
          "reuse 'none' requires the tested controller to be the handoff run's own branch and commit")


def _check_provenance(document: Mapping[str, Any], path: str) -> None:
    provenance = document["provenance"]
    handoff, tested = provenance["handoff"], provenance["tested"]
    here = f"{path}.provenance"
    check(provenance["coverage_sha"] == document["subject"]["commit"], f"{here}.coverage_sha",
          "must equal subject.commit (ordinary evidence is never carried forward)")
    check(handoff["controller_sha"] == handoff["commit"] and handoff["controller_branch"] == handoff["branch"],
          f"{here}.handoff", "a handoff run is its own controller (controller_* must equal branch/commit)")
    reuse = provenance["reuse"]
    if reuse == "none":
        _check_same_run_claims(tested, handoff, document["subject"], f"{here}.tested")
    else:
        check(tested["run_id"] != handoff["run_id"], f"{here}.tested", f"reuse {reuse!r} requires a distinct tested run")
    if reuse == "delegated":
        check(document["extensions"] is not None, f"{path}.extensions", "reuse 'delegated' requires extensions")


def _check_extension_names(document: Mapping[str, Any], allowed: Collection[str] | None, path: str) -> None:
    extensions = document["extensions"]
    if extensions is not None and allowed is not None:
        unknown = sorted(set(extensions["names"]) - set(allowed))
        check(not unknown, f"{path}.extensions.names", f"undeclared extensions {unknown}")


def _check_scope_detail(scope: Mapping[str, Any], path: str, *, allowed_kinds: tuple[str, ...]) -> None:
    check(scope["kind"] in allowed_kinds, f"{path}.kind", f"must be one of {list(allowed_kinds)}")
    if scope["kind"] == "selected":
        check("detail_sha256" in scope, path, "a selected scope requires detail_sha256")


def _check_same_header(document: Mapping[str, Any], expectation: Mapping[str, Any] | None, path: str) -> None:
    if expectation is None:
        return
    for field in ("repository", "key", "subject", "matrix_sha256", "contract_sha256"):
        if field in document:
            check(document[field] == expectation[field], f"{path}.{field}", "must equal the embedded expectation")


def _check_frames_equal_captures(frames: list[Mapping[str, Any]], captures: list[Mapping[str, Any]],
                                 lanes: Mapping[str, Mapping[str, Any]], path: str) -> None:
    check(len(frames) == len(captures), path, "must list exactly the expectation's captures")
    for position, (frame, capture) in enumerate(zip(frames, captures)):
        for field in CAPTURE_FIELDS:
            check(frame[field] == capture[field], f"{path}[{position}].{field}",
                  "must equal the expectation capture at the same position")
        lane = lanes[capture["lane_id"]]
        for field in ("artifact_node", "minecraft", "loader", "scenario"):
            check(frame[field] == lane[field], f"{path}[{position}].{field}", "must equal the expectation lane")


def _check_lanes_equal(lanes: list[Mapping[str, Any]], expected: list[Mapping[str, Any]], path: str) -> None:
    check(len(lanes) == len(expected), path, "must list exactly the expectation's lanes")
    for position, (lane, wanted) in enumerate(zip(lanes, expected)):
        for field in LANE_FIELDS:
            check(lane[field] == wanted[field], f"{path}[{position}].{field}",
                  "must equal the expectation lane at the same position")


# -- §3.1 expectation --------------------------------------------------------------------------------

_EXPECTATION = Obj(
    {
        **_header("mod-base.evidence.expectation"),
        "repository": REPOSITORY,
        "key": KEY,
        "label": LABEL,
        "subject": SUBJECT,
        "matrix_sha256": SHA256,
        "contract_sha256": SHA256,
        "contract_path": REPO_PATH,
        "profile": PROFILE,
        "scope": Obj({"kind": Str(choices=("complete", "selected"))},
                     {"detail_sha256": SHA256, "detail": JSON_OBJECT}),
        "image_policy": IMAGE_POLICY,
        "scenarios": List(Obj({"id": SCENARIO}, {"title": LABEL}), min_items=1, max_items=lim.MAX_SCENARIOS,
                          unique_by=lambda item: item["id"]),
        "lanes": List(Obj(LANE_FIELDS), min_items=1, max_items=lim.MAX_LANES),
        "captures": List(Obj(CAPTURE_FIELDS), min_items=1, max_items=lim.MAX_FRAMES),
        "comparisons": List(Obj(COMPARISON_FIELDS, {"region": Region()}), max_items=lim.MAX_COMPARISONS),
        "anchor": Nullable(Obj({"artifact_nodes": List(ARTIFACT_NODE, min_items=1, max_items=lim.MAX_LANES,
                                                        sorted_values=True)})),
    }
)


def validate_expectation(document: Any, *, image_policy: Mapping[str, Any] | None = None,
                         path: str = "$") -> dict[str, Any]:
    """``mod-base.evidence.expectation`` v1 (SPEC §3.1).

    Checks: unique scenario/lane/frame/comparison ids; every lane's scenario is declared; every
    capture references an existing lane and one of its roles; ``(lane_id, capture_id)`` unique;
    ``capture_order`` strictly increases within a (lane, role) in list order; each comparison
    references two distinct frames of its own lane and role; ``scope.detail`` hashes to
    ``scope.detail_sha256`` (and a ``selected`` scope carries it); anchor nodes are lane nodes; and,
    when ``image_policy`` (``config.images`` minus ``cross_check_runtime_metrics``) is given,
    ``image_policy`` equals it.
    """

    _EXPECTATION(document, path)
    scope = document["scope"]
    _check_scope_detail(scope, f"{path}.scope", allowed_kinds=("complete", "selected"))
    if "detail" in scope:
        check("detail_sha256" in scope, f"{path}.scope", "detail requires detail_sha256")
        try:
            detail_bytes = canonical_json(scope["detail"])
        except MbError as exc:
            raise fail(f"{path}.scope.detail", "cannot be encoded as canonical JSON") from exc
        check(len(detail_bytes) <= lim.MAX_SCOPE_DETAIL_BYTES, f"{path}.scope.detail", "exceeds 64 KiB")
        check(sha256_hex(detail_bytes) == scope["detail_sha256"], f"{path}.scope.detail_sha256",
              "must equal the SHA-256 of canonical_json(detail)")
    policy = document["image_policy"]
    check(policy["source_size"][0] * policy["source_size"][1] <= lim.MAX_IMAGE_PIXELS,
          f"{path}.image_policy.source_size", "exceeds the pixel bound")
    if image_policy is not None:
        check(policy == dict(image_policy), f"{path}.image_policy", "must equal the configured image policy")
    scenarios = {item["id"] for item in document["scenarios"]}
    lanes = _index_unique(document["lanes"], "lane_id", f"{path}.lanes")
    for position, lane in enumerate(document["lanes"]):
        check(lane["scenario"] in scenarios, f"{path}.lanes[{position}].scenario", "is not a declared scenario")
    captures = document["captures"]
    frames = _index_unique(captures, "frame_id", f"{path}.captures")
    for position, capture in enumerate(captures):
        lane = lanes.get(capture["lane_id"])
        check(lane is not None, f"{path}.captures[{position}].lane_id", "references no lane")
        check(capture["role"] in lane["roles"], f"{path}.captures[{position}].role", "is not a role of its lane")  # type: ignore[index]
    _check_capture_order(captures, f"{path}.captures")
    _check_comparisons(document["comparisons"], frames, lanes, f"{path}.comparisons")
    if document["anchor"] is not None:
        nodes = {lane["artifact_node"] for lane in document["lanes"]}
        for position, node in enumerate(document["anchor"]["artifact_nodes"]):
            check(node in nodes, f"{path}.anchor.artifact_nodes[{position}]", "is not an artifact node of any lane")
    return document


# -- §3.2 handoff ----------------------------------------------------------------------------------

_PROVENANCE = Obj({"handoff": RUN_CLAIM, "tested": RUN_CLAIM, "reuse": REUSE, "coverage_sha": SHA1})

_HANDOFF = Obj(
    {
        **_header("mod-base.evidence.handoff"),
        "repository": REPOSITORY,
        "key": KEY,
        "kit": KIT_REF,
        "subject": SUBJECT,
        "provenance": _PROVENANCE,
        "expectation": EXPECTATION_REF,
        "matrix_sha256": SHA256,
        "contract_sha256": SHA256,
        "scope": Obj({"kind": Str(choices=("complete", "selected"))}, {"detail_sha256": SHA256}),
        "extensions": Nullable(EXTENSIONS_REF),
        "lanes": List(EVIDENCE_LANE, min_items=1, max_items=lim.MAX_LANES),
        "frames": List(Obj({**FRAME_FIELDS, "source": Obj({"path": BUNDLE_PATH, **PNG_SOURCE_BASE})}),
                       min_items=1, max_items=lim.MAX_FRAMES),
        "comparisons": List(Obj({**COMPARISON_FIELDS, "source": COMPARE_METRICS}, {"region": Region()}),
                            max_items=lim.MAX_COMPARISONS),
        "files": List(FILE_RECORD, min_items=1, max_items=lim.MAX_HANDOFF_FILES),
    }
)


def _check_expectation_scope(document: Mapping[str, Any], expectation: Mapping[str, Any] | None, path: str) -> None:
    if expectation is None:
        return
    wanted = expectation["scope"]
    scope = document["scope"]
    check(scope.get("detail_sha256") == wanted.get("detail_sha256"), f"{path}.scope.detail_sha256",
          "must equal the embedded expectation")


def validate_handoff(document: Any, *, expectation: Mapping[str, Any] | None = None,
                     allowed_extensions: Collection[str] | None = None, path: str = "$") -> dict[str, Any]:
    """``mod-base.evidence.handoff`` v1 (SPEC §3.2).

    Structural rules: lane/frame/comparison ids unique; frames reference lanes (same node,
    Minecraft, loader, scenario) and lane roles; capture order; comparisons reference same-lane,
    same-role frames and their ``source`` metrics meet ``minimum_changed_fraction`` (rule 3);
    ``coverage_sha == subject.commit`` (rule 4); the ``reuse`` rules of rule 5 that need no config;
    an exact sorted ``files`` inventory holding ``expectation.json``, ``extensions.json`` iff
    ``extensions`` is set, every frame PNG under ``runtime/`` and nothing but ``runtime/**.json``
    and ``runtime/**.png`` otherwise. With ``expectation``: rule 1 (frames and lanes equal the
    captures and lanes, in order) and header equality. With ``allowed_extensions``: every name is
    declared in ``config.adapter.extensions``.
    """

    _HANDOFF(document, path)
    _check_scope_detail(document["scope"], f"{path}.scope", allowed_kinds=("complete", "selected"))
    _check_provenance(document, path)
    _check_extension_names(document, allowed_extensions, path)
    lanes = _index_unique(document["lanes"], "lane_id", f"{path}.lanes")
    frame_list = document["frames"]
    frames = _index_unique(frame_list, "frame_id", f"{path}.frames")
    _check_frames_against_lanes(frame_list, lanes, f"{path}.frames",
                                lane_fields=("artifact_node", "minecraft", "loader", "scenario"))
    _check_capture_order(frame_list, f"{path}.frames")
    _check_comparisons(document["comparisons"], frames, lanes, f"{path}.comparisons", metrics_keys=("source",))
    files = _check_file_inventory(document["files"], f"{path}.files", max_files=lim.MAX_HANDOFF_FILES,
                                  max_total=lim.MAX_RAW_BUNDLE_BYTES)
    _check_file_ref(files, document["expectation"], f"{path}.expectation")
    if document["extensions"] is not None:
        _check_file_ref(files, document["extensions"], f"{path}.extensions")
    else:
        check("extensions.json" not in files, f"{path}.files", "lists extensions.json without extensions")
    frame_paths: set[str] = set()
    for position, frame in enumerate(frame_list):
        source = frame["source"]
        here = f"{path}.frames[{position}].source"
        check(source["path"].startswith("runtime/") and source["path"].endswith(".png"), f"{here}.path",
              "must be a runtime/**.png path")
        _check_pixels(source, here)
        _check_file_ref(files, source, here)
        frame_paths.add(source["path"])
    runtime_count = 0
    for position, record in enumerate(document["files"]):
        name = record["path"]
        if name in ("expectation.json", "extensions.json"):
            continue
        here = f"{path}.files[{position}]"
        check(name.startswith("runtime/") and name.endswith((".json", ".png")), here,
              "only expectation.json, extensions.json and runtime/**.{json,png} may be shipped")
        if name.endswith(".json"):
            check(record["size"] <= lim.MAX_RUNTIME_JSON_BYTES, f"{here}.size", "runtime JSON exceeds 4 MiB")
        else:
            check(record["size"] <= lim.MAX_SOURCE_PNG_BYTES, f"{here}.size", "runtime PNG exceeds 32 MiB")
        runtime_count += 1
    check(runtime_count <= lim.MAX_RUNTIME_FILES, f"{path}.files", "exceeds the runtime file bound")
    if expectation is not None:
        _check_same_header(document, expectation, path)
        _check_expectation_scope(document, expectation, path)
        check(document["scope"]["kind"] == expectation["scope"]["kind"], f"{path}.scope.kind",
              "must equal the embedded expectation")
        _check_lanes_equal(document["lanes"], expectation["lanes"], f"{path}.lanes")
        _check_frames_equal_captures(frame_list, expectation["captures"], lanes, f"{path}.frames")
        _check_comparisons_equal(document["comparisons"], expectation["comparisons"], f"{path}.comparisons")
    return document


def _check_comparisons_equal(comparisons: list[Mapping[str, Any]], expected: list[Mapping[str, Any]], path: str) -> None:
    check(len(comparisons) == len(expected), path, "must list exactly the expectation's comparisons")
    for position, (comparison, wanted) in enumerate(zip(comparisons, expected)):
        for field in (*COMPARISON_FIELDS, "region"):
            check(comparison.get(field) == wanted.get(field), f"{path}[{position}].{field}",
                  "must equal the expectation comparison at the same position")


# -- §3.3 compact ----------------------------------------------------------------------------------

_WEBP_DERIVATIVE = Obj(
    {
        "path": BUNDLE_PATH,
        "sha256": SHA256,
        "size": Int(1, lim.MAX_DERIVATIVE_BYTES),
        "width": Int(1, lim.MAX_IMAGE_DIMENSION),
        "height": Int(1, lim.MAX_IMAGE_DIMENSION),
        "format": Const("webp"),
        "pixel": PIXEL_METRICS,
    }
)
_COMPACT = Obj(
    {
        **_header("mod-base.evidence.compact"),
        "repository": REPOSITORY,
        "key": KEY,
        "kit": KIT_REF,
        "subject": SUBJECT,
        "provenance": _PROVENANCE,
        "expectation": EXPECTATION_REF,
        "matrix_sha256": SHA256,
        "contract_sha256": SHA256,
        "scope": Obj(
            {"kind": Str(choices=("complete", "selected", "composed"))},
            {
                "detail_sha256": SHA256,
                "components": Obj(
                    {
                        "baseline": Obj({"artifact_id": RUN_ID, "name": Str(max_len=240), "digest": DIGEST,
                                         "manifest_sha256": SHA256}),
                        "selected_manifest_sha256": SHA256,
                    }
                ),
            },
        ),
        "extensions": Nullable(EXTENSIONS_REF),
        "source_artifact": Obj(
            {
                "kind": Str(choices=("handoff", "cache")),
                "id": RUN_ID,
                "name": Str(max_len=240),
                "digest": DIGEST,
                "size": Int(1, lim.MAX_ARTIFACT_BYTES),
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
            }
        ),
        "selection": SELECTION_REF,
        "lanes": List(_COMPACT_LANE, min_items=1, max_items=lim.MAX_LANES),
        "frames": List(
            Obj(
                {**FRAME_FIELDS, "source": Obj(PNG_SOURCE_BASE), "derivative": _WEBP_DERIVATIVE},
                {"epoch": Str(choices=("baseline", "selected")), "tested": _TESTED_FRAME_RECORD},
            ),
            min_items=1,
            max_items=lim.MAX_FRAMES,
        ),
        "comparisons": List(
            Obj({**COMPARISON_FIELDS, "source": COMPARE_METRICS, "derivative": COMPARE_METRICS}, {"region": Region()}),
            max_items=lim.MAX_COMPARISONS,
        ),
        "files": List(FILE_RECORD, min_items=2, max_items=lim.MAX_COMPACT_FILES),
    }
)


def _check_composed_epochs(document: Mapping[str, Any], lanes: Mapping[str, Mapping[str, Any]],
                           frames: Mapping[str, Mapping[str, Any]], path: str) -> None:
    """The epoch rules of a composed bundle that need no source (R3 proves the rest against them).

    A lane's own run fields describe the execution of its newest epoch; a lane re-tested by the
    selection whose frames still include ``epoch: baseline`` ones (a partially re-captured lane,
    Quick Skin schema 7's per-epoch lanes) carries the baseline execution of those frames as
    ``baseline_run``. So: ``baseline_run`` exists only on a lane holding a baseline frame, and a
    lane holding frames of both epochs must carry it; every frame's ``tested.jar_sha256`` is the
    production JAR of the run its epoch used (``baseline_run`` for a baseline frame of a lane that
    carries one, else the lane's own); all frames of one epoch record one tested run (one source
    generation each), the selected one being ``provenance.tested``; and a comparison never spans
    the two epochs (its frames are one generation's pixels)."""

    lane_epochs: dict[str, set[str]] = {lane_id: set() for lane_id in lanes}
    tested_runs: dict[str, dict[str, Any]] = {}
    claim = document["provenance"]["tested"]
    for position, frame in enumerate(document["frames"]):
        here = f"{path}.frames[{position}]"
        epoch, lane = frame["epoch"], lanes[frame["lane_id"]]
        lane_epochs[frame["lane_id"]].add(epoch)
        run = lane["baseline_run"] if epoch == "baseline" and "baseline_run" in lane else lane
        check(frame["tested"]["jar_sha256"] == run["jars"]["production_sha256"], f"{here}.tested.jar_sha256",
              "must be the production JAR of the lane run its epoch used")
        record = {name: value for name, value in frame["tested"].items() if name != "jar_sha256"}
        check(tested_runs.setdefault(epoch, record) == record, f"{here}.tested",
              f"every {epoch} frame must record the same tested run")
        if epoch == "selected":
            check(all(record[name] == value for name, value in claim.items()), f"{here}.tested",
                  "the selected epoch's tested run must be provenance.tested")
    for position, lane in enumerate(document["lanes"]):
        epochs = lane_epochs[lane["lane_id"]]
        if "baseline_run" in lane:
            check("baseline" in epochs, f"{path}.lanes[{position}].baseline_run",
                  "exists only for a lane holding epoch: baseline frames")
        else:
            check(epochs != {"baseline", "selected"}, f"{path}.lanes[{position}]",
                  "a lane holding frames of both epochs requires baseline_run")
    for position, comparison in enumerate(document["comparisons"]):
        check(frames[comparison["first_frame_id"]]["epoch"] == frames[comparison["second_frame_id"]]["epoch"],
              f"{path}.comparisons[{position}]", "must compare two frames of one epoch")


def validate_compact(document: Any, *, expectation: Mapping[str, Any] | None = None,
                     allowed_extensions: Collection[str] | None = None, intermediate: bool = False,
                     path: str = "$") -> dict[str, Any]:
    """``mod-base.evidence.compact`` v1 (SPEC §3.3).

    Adds to the handoff rules: ``source_artifact.name`` parses as a handoff (``key`` and attempt
    bound) or a cache (``key`` and ``coverage_sha`` bound) name; ``composed`` scopes carry
    ``components`` (a baseline name for this key) and every frame an ``epoch`` plus ``tested``
    record, while other scopes carry neither (nor any lane a ``baseline_run``), and a composed
    bundle's epochs are consistent (:func:`_check_composed_epochs`); each derivative lives at
    ``images/<sha256>.webp``;
    derivative comparisons meet the minimum too; ``files`` is exactly ``expectation.json``,
    ``selection.json``, ``extensions.json`` (iff extensions) and the set of derivative images.
    With ``expectation``: header equality, frames equal the expectation's captures, a ``complete``
    or ``composed`` bundle embeds the **complete** expectation (R3: composed frames equal the
    complete expectation) and every derivative size equals ``thumbnail_size``.

    A published bundle (collected artifact, cache) is ``complete`` or ``composed``. ``intermediate=
    True`` instead validates the core's own compaction of a ``selected`` handoff, which ``compose``
    hands to the adapter hook as ``selected_compact_dir`` (SPEC §4.2): ``scope.kind == "selected"``
    with its ``detail_sha256``, the selected expectation embedded, no epochs, and the selection
    **draft** embedded as ``selection.json``. It is never uploaded, cached or published.
    """

    _COMPACT(document, path)
    scope = document["scope"]
    _check_scope_detail(scope, f"{path}.scope",
                        allowed_kinds=("selected",) if intermediate else ("complete", "composed"))
    composed = scope["kind"] == "composed"
    check(composed == ("components" in scope), f"{path}.scope", "components are required exactly for a composed scope")
    if composed:
        baseline = g.parse_artifact_name(scope["components"]["baseline"]["name"])
        check(baseline is not None and baseline.kind == "baseline" and baseline.key == document["key"],
              f"{path}.scope.components.baseline.name", "must be an mb-baseline name for this key")
    _check_provenance(document, path)
    _check_extension_names(document, allowed_extensions, path)
    source_artifact = document["source_artifact"]
    parsed = g.parse_artifact_name(source_artifact["name"])
    if source_artifact["kind"] == "handoff":
        check(parsed is not None and parsed.kind == "handoff" and parsed.key == document["key"]
              and parsed.attempt == source_artifact["run_attempt"], f"{path}.source_artifact.name",
              "must be the handoff name of this key and attempt")
    else:
        check(parsed is not None and parsed.kind == "cache" and parsed.key == document["key"]
              and parsed.coverage_sha == document["provenance"]["coverage_sha"], f"{path}.source_artifact.name",
              "must be the cache name of this key and coverage")
    lanes = _index_unique(document["lanes"], "lane_id", f"{path}.lanes")
    frame_list = document["frames"]
    frames = _index_unique(frame_list, "frame_id", f"{path}.frames")
    _check_frames_against_lanes(frame_list, lanes, f"{path}.frames",
                                lane_fields=("artifact_node", "minecraft", "loader", "scenario"))
    _check_capture_order(frame_list, f"{path}.frames")
    _check_comparisons(document["comparisons"], frames, lanes, f"{path}.comparisons",
                       metrics_keys=("source", "derivative"))
    files = _check_file_inventory(document["files"], f"{path}.files", max_files=lim.MAX_COMPACT_FILES,
                                  max_total=lim.MAX_COMPACT_BUNDLE_BYTES)
    _check_file_ref(files, document["expectation"], f"{path}.expectation")
    _check_file_ref(files, document["selection"], f"{path}.selection")
    if document["extensions"] is not None:
        _check_file_ref(files, document["extensions"], f"{path}.extensions")
    else:
        check("extensions.json" not in files, f"{path}.files", "lists extensions.json without extensions")
    image_paths: set[str] = set()
    box = expectation["image_policy"]["derivative_box"] if expectation is not None else None
    for position, frame in enumerate(frame_list):
        here = f"{path}.frames[{position}]"
        check(composed == ("epoch" in frame) and composed == ("tested" in frame), here,
              "epoch and tested are required exactly for composed frames")
        _check_pixels(frame["source"], f"{here}.source")
        derivative = frame["derivative"]
        check(derivative["path"] == f"images/{derivative['sha256']}.webp", f"{here}.derivative.path",
              "must be images/<sha256>.webp")
        _check_pixels(derivative, f"{here}.derivative")
        _check_file_ref(files, derivative, f"{here}.derivative")
        if box is not None:
            wanted = thumbnail_size((frame["source"]["width"], frame["source"]["height"]), box)
            check((derivative["width"], derivative["height"]) == wanted, f"{here}.derivative",
                  f"dimensions must equal thumbnail(source, derivative_box) = {wanted[0]}x{wanted[1]}")
        image_paths.add(derivative["path"])
    expected_files = {"expectation.json", "selection.json", *image_paths}
    if document["extensions"] is not None:
        expected_files.add("extensions.json")
    check(set(files) == expected_files, f"{path}.files",
          "must be exactly expectation.json, selection.json, extensions.json (iff set) and the derivatives")
    if composed:
        _check_composed_epochs(document, lanes, frames, path)
    else:
        for position, lane in enumerate(document["lanes"]):
            check("baseline_run" not in lane, f"{path}.lanes[{position}].baseline_run",
                  "exists only in a composed bundle")
    if expectation is not None:
        _check_same_header(document, expectation, path)
        _check_expectation_scope(document, expectation, path)
        _check_lanes_equal(document["lanes"], expectation["lanes"], f"{path}.lanes")
        _check_frames_equal_captures(frame_list, expectation["captures"], lanes, f"{path}.frames")
        _check_comparisons_equal(document["comparisons"], expectation["comparisons"], f"{path}.comparisons")
        if intermediate:
            check(expectation["scope"]["kind"] == "selected", f"{path}.scope.kind",
                  "the intermediate compaction embeds its selected expectation")
        else:
            check(expectation["scope"]["kind"] == "complete", f"{path}.scope.kind",
                  "a published compact bundle embeds the complete expectation (a selected one must be composed)")
    return document


# -- §3.4 anchor -----------------------------------------------------------------------------------

_ANCHOR = Obj(
    {
        **_header("mod-base.evidence.anchor"),
        "repository": REPOSITORY,
        "key": KEY,
        "kit": KIT_REF,
        "subject": SUBJECT,
        "provenance": Obj({"handoff": RUN_CLAIM, "tested": RUN_CLAIM}),
        "reference": Obj(
            {
                "artifact_nodes": List(ARTIFACT_NODE, min_items=1, max_items=lim.MAX_LANES, sorted_values=True),
                "minecraft": List(MINECRAFT, min_items=1, max_items=lim.MAX_LANES, sorted_values=True),
                "loaders": List(LOADER, min_items=1, max_items=lim.MAX_LANES, sorted_values=True),
            }
        ),
        "source_artifact": Obj(
            {"id": RUN_ID, "name": Str(max_len=240), "digest": DIGEST, "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT}
        ),
        "expectation": EXPECTATION_REF,
        "lanes": List(EVIDENCE_LANE, min_items=1, max_items=lim.MAX_LANES),
        "frames": List(
            Obj({**FRAME_FIELDS, "source": Obj({"path": BUNDLE_PATH, "pixel_sha256": SHA256, **PNG_SOURCE_BASE})}),
            min_items=1,
            max_items=lim.MAX_FRAMES,
        ),
        "files": List(FILE_RECORD, min_items=2, max_items=lim.MAX_ANCHOR_FILES),
    }
)


def validate_anchor(document: Any, *, expectation: Mapping[str, Any] | None = None, path: str = "$") -> dict[str, Any]:
    """``mod-base.evidence.anchor`` v1 (SPEC §3.4).

    Checks: the source artifact is this key's handoff of the same run and attempt as
    ``provenance.handoff``; the handoff was a direct canonical run
    (``handoff.controller_sha == subject.commit``); ``reference`` equals the sorted node, Minecraft
    and loader sets of ``lanes``; frames belong to those lanes; each canonical PNG lives at
    ``images/<sha256>.png`` with ``pixel_sha256`` equal to its metrics; ``files`` is exactly
    ``expectation.json`` plus those images. With ``expectation``: the expectation declares an
    anchor (``anchor`` is not null, SPEC §3.4 eligibility) whose ``artifact_nodes`` equal
    ``reference.artifact_nodes``, the lanes are exactly the expectation lanes of those nodes and the
    frames exactly their captures, in order.
    """

    _ANCHOR(document, path)
    provenance = document["provenance"]
    handoff = provenance["handoff"]
    check(handoff["controller_sha"] == handoff["commit"] and handoff["controller_branch"] == handoff["branch"],
          f"{path}.provenance.handoff", "a handoff run is its own controller")
    check(handoff["controller_sha"] == document["subject"]["commit"], f"{path}.provenance.handoff.controller_sha",
          "an anchor requires a direct canonical run (controller_sha == subject.commit)")
    source = document["source_artifact"]
    parsed = g.parse_artifact_name(source["name"])
    check(parsed is not None and parsed.kind == "handoff" and parsed.key == document["key"]
          and parsed.attempt == source["run_attempt"], f"{path}.source_artifact.name",
          "must be the handoff name of this key and attempt")
    check((source["run_id"], source["run_attempt"]) == (handoff["run_id"], handoff["run_attempt"]),
          f"{path}.source_artifact", "must be owned by the handoff run and attempt")
    lanes = _index_unique(document["lanes"], "lane_id", f"{path}.lanes")
    reference = document["reference"]
    for field, lane_field in (("artifact_nodes", "artifact_node"), ("minecraft", "minecraft"), ("loaders", "loader")):
        check(reference[field] == sorted({lane[lane_field] for lane in document["lanes"]}),
              f"{path}.reference.{field}", "must equal the sorted distinct values of the anchor lanes")
    frame_list = document["frames"]
    _index_unique(frame_list, "frame_id", f"{path}.frames")
    _check_frames_against_lanes(frame_list, lanes, f"{path}.frames",
                                lane_fields=("artifact_node", "minecraft", "loader", "scenario"))
    _check_capture_order(frame_list, f"{path}.frames")
    files = _check_file_inventory(document["files"], f"{path}.files", max_files=lim.MAX_ANCHOR_FILES,
                                  max_total=lim.MAX_ANCHOR_BUNDLE_BYTES)
    _check_file_ref(files, document["expectation"], f"{path}.expectation")
    image_paths: set[str] = set()
    for position, frame in enumerate(frame_list):
        source_image = frame["source"]
        here = f"{path}.frames[{position}].source"
        check(source_image["path"] == f"images/{source_image['sha256']}.png", f"{here}.path",
              "must be images/<sha256>.png")
        check(source_image["pixel_sha256"] == source_image["pixel"]["pixel_sha256"], f"{here}.pixel_sha256",
              "must equal pixel.pixel_sha256")
        _check_pixels(source_image, here)
        _check_file_ref(files, source_image, here)
        image_paths.add(source_image["path"])
    check(set(files) == {"expectation.json", *image_paths}, f"{path}.files",
          "must be exactly expectation.json and the canonical PNGs")
    if expectation is not None:
        _check_same_header(document, expectation, path)
        declared = expectation["anchor"]
        check(declared is not None, f"{path}.reference", "the embedded expectation declares no anchor (anchor: null)")
        check(reference["artifact_nodes"] == declared["artifact_nodes"], f"{path}.reference.artifact_nodes",
              "must equal the expectation's declared anchor.artifact_nodes")
        nodes = set(reference["artifact_nodes"])
        wanted_lanes = [lane for lane in expectation["lanes"] if lane["artifact_node"] in nodes]
        _check_lanes_equal(document["lanes"], wanted_lanes, f"{path}.lanes")
        lane_ids = {lane["lane_id"] for lane in wanted_lanes}
        wanted = [capture for capture in expectation["captures"] if capture["lane_id"] in lane_ids]
        _check_frames_equal_captures(frame_list, wanted, lanes, f"{path}.frames")
    return document


# -- §3.5 families ---------------------------------------------------------------------------------

_ENVELOPE = Obj(
    {
        **_header("mod-base.family.envelope"),
        "repository": REPOSITORY,
        "family": FAMILY,
        "key": KEY,
        "kit": KIT_REF,
        "subject": SUBJECT,
        "coverage_sha": SHA1,
        "producer": RUN_CLAIM,
        "native": Obj(
            {
                "manifest_path": Const("manifest.json"),
                "manifest_sha256": SHA256,
                "kind": Str(g.NATIVE_KIND, max_len=80),
                "schema_version": Int(1, 1000),
            }
        ),
        "files": List(FILE_RECORD, min_items=1, max_items=lim.MAX_FAMILY_FILES),
    },
    {"carried_from": SHA1},
)


def validate_family_envelope(document: Any, *, max_total_bytes: int = lim.MAX_FAMILY_HANDOFF_BYTES,
                             path: str = "$") -> dict[str, Any]:
    """``mod-base.family.envelope`` v1 (SPEC §3.5).

    ``files`` is the exact sorted inventory of the native bundle (``envelope.json`` excluded, which
    is the envelope's own file name), it includes ``manifest.json`` whose hash is
    ``native.manifest_sha256``, its total stays within ``max_total_bytes``
    (``families[].handoff_max_bytes``), a producer run is its own controller, and ``carried_from``
    differs from ``coverage_sha``.
    """

    _ENVELOPE(document, path)
    producer = document["producer"]
    check(producer["controller_sha"] == producer["commit"] and producer["controller_branch"] == producer["branch"],
          f"{path}.producer", "a producer run is its own controller")
    if "carried_from" in document:
        check(document["carried_from"] != document["coverage_sha"], f"{path}.carried_from",
              "must differ from coverage_sha")
    by_path: dict[str, Mapping[str, Any]] = {}
    previous = ""
    total = 0
    for position, record in enumerate(document["files"]):
        name = record["path"]
        check(name > previous, f"{path}.files[{position}].path", "files must be sorted by path without duplicates")
        check(name != "envelope.json", f"{path}.files[{position}].path", "must not list envelope.json")
        previous = name
        by_path[name] = record
        total += record["size"]
    check(total <= max_total_bytes, f"{path}.files", f"total size exceeds {max_total_bytes} bytes")
    manifest = by_path.get("manifest.json")
    check(manifest is not None and manifest["sha256"] == document["native"]["manifest_sha256"],
          f"{path}.native.manifest_sha256", "must equal the files record of manifest.json")
    return document


def _paired_image(value: Any, path: str) -> dict[str, Any]:
    Obj(
        {
            "image": Obj(
                {
                    "path": BUNDLE_PATH,
                    "sha256": SHA256,
                    "size": Int(1, lim.MAX_DERIVATIVE_BYTES),
                    "width": Int(1, lim.MAX_IMAGE_DIMENSION),
                    "height": Int(1, lim.MAX_IMAGE_DIMENSION),
                    "format": Const("webp"),
                    "pixel": PIXEL_METRICS,
                }
            ),
            "source": Obj(
                {
                    "sha256": SHA256,
                    "width": Int(1, lim.MAX_IMAGE_DIMENSION),
                    "height": Int(1, lim.MAX_IMAGE_DIMENSION),
                    "pixel": PIXEL_METRICS,
                }
            ),
        }
    )(value, path)
    _check_pixels(value["image"], f"{path}.image")
    _check_pixels(value["source"], f"{path}.source")
    return value


VARIANT_ID = Str(g.VARIANT_ID, max_len=64)
VERDICT = Obj(
    {"runtime_passed": Bool(), "semantic_valid": Bool(), "matches_reference": Nullable(Bool()), "defect": Bool()}
)
PAIR = Obj(
    {
        "pair_id": IDENT,
        "capture_id": IDENT,
        "reference_capture_id": IDENT,
        "title": TITLE,
        "expectation": EXPECTATION_TEXT,
        "runtime_evidence": RUNTIME_EVIDENCE,
        "verdict": VERDICT,
        "metrics": Obj(
            {
                "semantic_changed_fraction": Num(0.0, 1.0),
                "perceptual_delta": Num(0.0, 1_000_000.0),
                "candidate_semantic_sha256": SHA256,
                "reference_semantic_sha256": SHA256,
            }
        ),
        "reference": _paired_image,
        "candidate": _paired_image,
    }
)
PAIRED_LANE_FIELDS: dict[str, Validator] = {
    "lane_id": LANE_ID,
    "artifact_node": ARTIFACT_NODE,
    "minecraft": MINECRAFT,
    "loader": LOADER,
    "variant": Obj({"id": VARIANT_ID, "name": LABEL, "version": Str(max_len=80, text="evidence"),
                    "version_id": Str(max_len=80, text="evidence")}),
    "review": Obj({"reviewed_frame_count": Int(0, 1_000_000), "manifest_sha256": SHA256, "proof_sha256": SHA256,
                   "report_sha256": SHA256}),
    "pairs": List(PAIR, min_items=1, max_items=lim.MAX_FAMILY_PAIRS_PER_LANE),
}
PAIRED_LANE = Obj(PAIRED_LANE_FIELDS)
NOT_APPLICABLE_FIELDS: dict[str, Validator] = {
    "artifact_node": ARTIFACT_NODE,
    "minecraft": MINECRAFT,
    "loader": LOADER,
    "variant_id": VARIANT_ID,
    "variant_name": LABEL,
    "reason": REASON,
}
NOT_APPLICABLE = Obj(NOT_APPLICABLE_FIELDS)
LINK_LABEL = Str(max_len=lim.MAX_LINK_LABEL_LENGTH, text="display")
FAMILY_LINK = Obj({"label": LINK_LABEL, "run_id": RUN_ID})
FAMILY_STATUS = Str(choices=("available", "superseded", "unavailable"))

_PAIRED = Obj(
    {
        **_header("mod-base.family.paired"),
        "family": FAMILY,
        "key": KEY,
        "coverage_sha": SHA1,
        "subject": SUBJECT,
        "status": Const("available"),
        "provenance": Obj({"producer": own_run_record,
                           "links": List(FAMILY_LINK, max_items=lim.MAX_FAMILY_LINKS,
                                         unique_by=lambda link: link["label"])}),
        "contracts": Map(g.CONTRACT_NAME, SHA256, max_items=lim.MAX_FAMILY_CONTRACTS, max_key_len=64),
        "image_policy": FAMILY_IMAGE_POLICY,
        "lanes": List(PAIRED_LANE, max_items=lim.MAX_FAMILY_LANES),
        "not_applicable": List(NOT_APPLICABLE, max_items=lim.MAX_NOT_APPLICABLE),
    }
)


def _check_paired_lanes(lanes: list[Mapping[str, Any]], not_applicable: list[Mapping[str, Any]],
                        box_for: Callable[[str | None], list[int]], path: str, *, image_prefix: str) -> set[str]:
    """Shared by the paired projection and the gallery family view; returns the image paths.

    The projection covers one key and its entries carry no ``key``; the gallery view unions every
    key of a family, so there each lane and not-applicable entry carries ``key`` and every
    uniqueness rule is scoped by it. ``box_for(key)`` is that key's ``derivative_box``.
    """

    lane_ids: set[tuple[str | None, str]] = set()
    lane_variants: set[tuple[str | None, str, str]] = set()
    images: set[str] = set()
    for position, lane in enumerate(lanes):
        here = f"{path}.lanes[{position}]"
        key = lane.get("key")
        check((key, lane["lane_id"]) not in lane_ids, f"{here}.lane_id", f"duplicates lane_id {lane['lane_id']!r}")
        lane_ids.add((key, lane["lane_id"]))
        variant = (key, lane["artifact_node"], lane["variant"]["id"])
        check(variant not in lane_variants, here, "duplicates an (artifact_node, variant) lane")
        lane_variants.add(variant)
        box = box_for(key)
        _index_unique(lane["pairs"], "pair_id", f"{here}.pairs")
        for pair_position, pair in enumerate(lane["pairs"]):
            pair_path = f"{here}.pairs[{pair_position}]"
            verdict = pair["verdict"]
            check(verdict["runtime_passed"] and verdict["semantic_valid"] and not verdict["defect"],
                  f"{pair_path}.verdict", "is not publishable (requires runtime_passed, semantic_valid, no defect)")
            for side in ("reference", "candidate"):
                image = pair[side]["image"]
                source = pair[side]["source"]
                check(image["path"] == f"{image_prefix}{image['sha256']}.webp", f"{pair_path}.{side}.image.path",
                      f"must be {image_prefix}<sha256>.webp")
                wanted = thumbnail_size((source["width"], source["height"]), box)
                check((image["width"], image["height"]) == wanted, f"{pair_path}.{side}.image",
                      f"dimensions must equal thumbnail(source, derivative_box) = {wanted[0]}x{wanted[1]}")
                check(source["pixel"]["file_sha256"] == source["sha256"], f"{pair_path}.{side}.source.pixel",
                      "file_sha256 must equal source.sha256")
                images.add(image["path"])
    seen: set[tuple[str | None, str, str]] = set()
    for position, entry in enumerate(not_applicable):
        marker = (entry.get("key"), entry["artifact_node"], entry["variant_id"])
        check(marker not in seen and marker not in lane_variants, f"{path}.not_applicable[{position}]",
              "duplicates a lane or another not-applicable entry")
        seen.add(marker)
    return images


def validate_family_paired(document: Any, *, image_policy: Mapping[str, Any] | None = None,
                           path: str = "$") -> dict[str, Any]:
    """``mod-base.family.paired`` v1 (SPEC §3.5), the projection returned by ``family_validate``.

    Checks: ``status == "available"``; every pair is publishable (``runtime_passed`` and
    ``semantic_valid`` and not ``defect``); lane ids and ``(artifact_node, variant.id)`` unique;
    pair ids unique per lane; each image lives at ``images/<sha256>.webp`` with metrics naming it
    and dimensions equal to ``thumbnail(source, image_policy.derivative_box)``; not-applicable
    entries neither repeat nor overlap a lane; link labels unique. With ``image_policy`` (the
    configured ``families[].image_policy``) the recorded policy must equal it.
    """

    _PAIRED(document, path)
    if image_policy is not None:
        check(document["image_policy"] == dict(image_policy), f"{path}.image_policy",
              "must equal the configured family image policy")
    box = document["image_policy"]["derivative_box"]
    _check_paired_lanes(document["lanes"], document["not_applicable"], lambda _key: box, path, image_prefix="images/")
    return document


# -- §3.6 selection --------------------------------------------------------------------------------

IMPLEMENTATION = Obj({"branch": BRANCH, "sha": SHA1, "workflow_ref": Str(max_len=500), "run_id": RUN_ID,
                      "run_attempt": RUN_ATTEMPT})


def _check_implementation(implementation: Mapping[str, Any], repository: str, path: str) -> None:
    try:
        ref = g.parse_workflow_ref(implementation["workflow_ref"])
    except MbError as exc:
        raise fail(f"{path}.workflow_ref", "must be a branch-scoped workflow ref") from exc
    check(ref.repository == repository and ref.path == PAGES_WORKFLOW_PATH
          and ref.branch == implementation["branch"], f"{path}.workflow_ref",
          "must be <repository>/.github/workflows/pages.yml@refs/heads/<implementation.branch>")


_SELECTED_ARTIFACT = Obj(
    {
        "kind": Str(choices=("handoff", "cache")),
        "id": RUN_ID,
        "name": Str(max_len=240),
        "digest": DIGEST,
        "size": Int(1, lim.MAX_ARTIFACT_BYTES),
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "workflow_path": WORKFLOW_PATH,
        "created_at": TIMESTAMP,
    }
)
_SELECTION_SOURCE = Obj(
    {
        "handoff_run": own_run_record,
        "tested_run": run_record,
        "reuse": REUSE,
        "kit_binding": Obj({"source": Str(choices=("referenced_workflows", "workflow_file")), "sha": SHA1}),
    },
    {
        "attestation_job": Obj({"name": Str(max_len=lim.MAX_JOB_NAME_LENGTH, text="evidence"), "id": RUN_ID,
                                "conclusion": Const("success")}),
        "job_graph_sha256": SHA256,
    },
)
_SELECTION_DRAFT_FIELDS: dict[str, Validator] = {
    **_header("mod-base.selection"),
    "repository": REPOSITORY,
    "key": KEY,
    "kit": KIT_REF,
    "implementation": IMPLEMENTATION,
    "subject": SUBJECT,
    "coverage_sha": SHA1,
    "selected_artifact": _SELECTED_ARTIFACT,
    "source": _SELECTION_SOURCE,
    "expectation_sha256": SHA256,
    "source_manifest_sha256": SHA256,
    "extensions_verified": List(EXTENSION_NAME, max_items=lim.MAX_EXTENSION_NAMES, sorted_values=True),
}
#: The fields only the step that writes the compact bundle can know (see :func:`validate_selection`).
SELECTION_COMPLETION_FIELDS = ("manifest_sha256", "binding", "composition")
_COMPOSITION = Obj(
    {
        "baseline_artifact": Obj({"id": RUN_ID, "name": Str(max_len=240), "digest": DIGEST, "owner_run_id": RUN_ID}),
        "selected_manifest_sha256": SHA256,
    }
)
_SELECTION = Obj(
    {
        **_SELECTION_DRAFT_FIELDS,
        "manifest_sha256": SHA256,
        "binding": Obj({"mode": Str(choices=("reencode-identical", "cache-revalidated")),
                        "frames": Int(1, lim.MAX_FRAMES), "derivatives": Int(1, lim.MAX_FRAMES)}),
    },
    {"composition": _COMPOSITION},
)
_SELECTION_DRAFT = Obj(_SELECTION_DRAFT_FIELDS)


def _check_selection_runs(document: Mapping[str, Any], path: str) -> None:
    """The run rules of SPEC §3.2 rule 5 and §4.8 steps 2-4 that need no config."""

    selected = document["selected_artifact"]
    source = document["source"]
    handoff_run, tested_run = source["handoff_run"], source["tested_run"]
    reuse = source["reuse"]
    here = f"{path}.source"
    if selected["kind"] == "handoff":
        check((handoff_run["run_id"], handoff_run["run_attempt"]) == (selected["run_id"], selected["run_attempt"]),
              f"{here}.handoff_run", "must be the selected artifact's owner run and attempt")
        check(selected["workflow_path"] == handoff_run["workflow_path"], f"{path}.selected_artifact.workflow_path",
              "must be the handoff run's workflow")
    else:
        check(selected["workflow_path"] == PAGES_WORKFLOW_PATH, f"{path}.selected_artifact.workflow_path",
              f"a cache is owned by a {PAGES_WORKFLOW_PATH} run")
        check(selected["run_id"] != document["implementation"]["run_id"], f"{path}.selected_artifact.run_id",
              "a selected cache is owned by an earlier Pages run, never by this one")
    if reuse == "none":
        _check_same_run_claims(tested_run, handoff_run, document["subject"], f"{here}.tested_run")
        for field in ("event", "created_at", "head_sha"):
            check(tested_run[field] == handoff_run[field], f"{here}.tested_run.{field}",
                  "reuse 'none' records the handoff run itself")
        check(tested_run.get("display_title") == handoff_run.get("display_title"), f"{here}.tested_run.display_title",
              "reuse 'none' records the handoff run itself")
    else:
        check(tested_run["run_id"] != handoff_run["run_id"], f"{here}.tested_run",
              "reused evidence requires a distinct tested run")
    if reuse != "delegated":
        check(tested_run["head_sha"] == tested_run["controller_sha"], f"{here}.tested_run.head_sha",
              "must equal the tested run's controller_sha (only delegated reuse tests a foreign head)")
    check((reuse == "attested") == ("attestation_job" in source), f"{here}.attestation_job",
          "is required exactly when reuse is 'attested'")
    if reuse == "attested":
        for field in ("handoff_run", "tested_run"):
            check("display_title" in source[field], f"{here}.{field}", "attested reuse requires a verified display_title")
    if handoff_run["controller_sha"] != document["subject"]["commit"]:
        check("display_title" in handoff_run, f"{here}.handoff_run",
              "a verified display_title is mandatory when the handoff controller differs from the subject")
    wanted_binding = "workflow_file" if selected["kind"] == "handoff" else "referenced_workflows"
    check(source["kit_binding"]["source"] == wanted_binding, f"{here}.kit_binding.source",
          f"a {selected['kind']} binds its kit through {wanted_binding}")


def validate_selection(document: Any, *, draft: bool = False, path: str = "$") -> dict[str, Any]:
    """``mod-base.selection`` v1 (SPEC §3.6).

    A selection is written in two phases (see ``docs/SCHEMAS.md``, "The collect flow"):
    ``authenticate`` writes the **draft** (``draft=True``): every field except
    :data:`SELECTION_COMPLETION_FIELDS`, which a draft must not carry. The step that writes the
    compact bundle (``compact``, or ``compose`` for selected evidence) completes it with
    ``manifest_sha256`` (:func:`compact_identity_sha256` of that bundle), ``binding`` and, for a
    composed bundle, ``composition``, and embeds it as ``selection.json``. A final selection
    (``draft=False``) requires ``manifest_sha256`` and ``binding``.

    Checks: ``coverage_sha == subject.commit``; the implementation workflow ref is this repository's
    ``pages.yml`` on the implementation branch; the selected artifact name is this key's handoff
    (attempt bound, owned by ``handoff_run`` and its workflow) or cache (coverage bound, owned by an
    earlier ``pages.yml`` run); the kit binding source follows the kind (SPEC §1.8: a handoff binds
    through ``workflow_file``, a cache through ``referenced_workflows``); ``kit_binding.sha`` is the
    selected artifact's recorded kit and deliberately **not** compared with ``kit`` (the executing
    kit of this run: a bump must not force regeneration); ``reuse: "none"`` means the tested run is
    the handoff run (same id, attempt, workflow, event, creation time, head and display title) that
    tested the subject's branch and commit under the handoff run's own head; ``attested`` and
    ``delegated`` name a distinct tested run; unless ``delegated``, the tested run's head equals its
    ``controller_sha``; ``attestation_job`` exists exactly for ``attested``, which also requires
    ``display_title`` on both runs; a handoff controller other than the subject commit requires the
    handoff run's ``display_title`` (SPEC §4.8 step 3). Final only: ``binding.mode`` matches the
    kind (``reencode-identical`` for a handoff, ``cache-revalidated`` for a cache),
    ``derivatives <= frames`` and a composition names an ``mb-baseline`` of this key.
    """

    (_SELECTION_DRAFT if draft else _SELECTION)(document, path)
    check(document["coverage_sha"] == document["subject"]["commit"], f"{path}.coverage_sha",
          "must equal subject.commit")
    _check_implementation(document["implementation"], document["repository"], f"{path}.implementation")
    selected = document["selected_artifact"]
    parsed = g.parse_artifact_name(selected["name"])
    if selected["kind"] == "handoff":
        check(parsed is not None and parsed.kind == "handoff" and parsed.key == document["key"]
              and parsed.attempt == selected["run_attempt"], f"{path}.selected_artifact.name",
              "must be the handoff name of this key and attempt")
    else:
        check(parsed is not None and parsed.kind == "cache" and parsed.key == document["key"]
              and parsed.coverage_sha == document["coverage_sha"], f"{path}.selected_artifact.name",
              "must be the cache name of this key and coverage")
    _check_selection_runs(document, path)
    if draft:
        return document
    wanted_mode = "reencode-identical" if selected["kind"] == "handoff" else "cache-revalidated"
    check(document["binding"]["mode"] == wanted_mode, f"{path}.binding.mode",
          f"a {selected['kind']} is bound by {'re-encoding' if selected['kind'] == 'handoff' else 'revalidation'}")
    check(document["binding"]["derivatives"] <= document["binding"]["frames"], f"{path}.binding.derivatives",
          "cannot exceed frames")
    if "composition" in document:
        baseline = g.parse_artifact_name(document["composition"]["baseline_artifact"]["name"])
        check(baseline is not None and baseline.kind == "baseline" and baseline.key == document["key"],
              f"{path}.composition.baseline_artifact.name", "must be an mb-baseline name for this key")
    return document


def compact_identity_sha256(manifest: Mapping[str, Any]) -> str:
    """The selection-independent identity of a compact manifest: ``canonical_sha256`` of the
    manifest without its ``selection`` member and without the ``selection.json`` record of
    ``files``.

    A compact bundle embeds its ``selection.json`` and the manifest records that file's hash (in
    ``selection`` and in ``files``), so the selection can never name the manifest's own file hash;
    it records this identity as ``manifest_sha256`` instead. ``scope.components.
    selected_manifest_sha256`` and ``selection.composition.selected_manifest_sha256`` name the
    identity of the intermediate selected compaction the same way. (``promotion.bundles[].
    manifest_sha256`` and ``components.baseline.manifest_sha256`` are plain SHA-256 of a published
    ``manifest.json``.)
    """

    if (not isinstance(manifest, Mapping) or "selection" not in manifest
            or not isinstance(manifest.get("files"), list)):
        raise MbError("a compact manifest identity needs a compact manifest with selection and files")
    identity = {key: value for key, value in manifest.items() if key != "selection"}
    identity["files"] = [record for record in manifest["files"]
                         if not (isinstance(record, Mapping) and record.get("path") == "selection.json")]
    return canonical_sha256(identity)


_SELECTED_ARTIFACT_FIELDS = ("kind", "id", "name", "digest", "size", "run_id", "run_attempt")


def check_compact_selection(compact: Mapping[str, Any], selection: Mapping[str, Any], *, path: str = "$") -> None:
    """Bind a published compact manifest to the final selection embedded as its ``selection.json``.

    Both documents must already be valid (:func:`validate_compact` without ``intermediate`` and
    :func:`validate_selection` with ``draft=False``); this checks the purely structural agreement
    every writer (``compact``, ``compose``) and reader (``validate --kind compact``, ``build``)
    relies on: repository, key, kit (both are written by the executing run), subject and coverage;
    ``expectation_sha256`` is the embedded ``expectation.json`` hash; ``manifest_sha256`` is
    :func:`compact_identity_sha256` of the manifest; the selected artifact is the manifest's
    ``source_artifact``; the handoff and tested run records carry exactly the manifest's
    ``provenance`` claims and ``reuse``; ``extensions_verified`` lists exactly the manifest's
    extension names; ``binding`` counts the frames and the distinct derivative files; and a
    ``composition`` exists exactly for a ``composed`` scope, naming the same baseline artifact and
    selected identity as ``scope.components``.
    """

    here = f"{path}.selection"
    for field in ("repository", "key", "kit", "subject"):
        check(selection[field] == compact[field], f"{here}.{field}", "must equal the compact manifest's")
    provenance = compact["provenance"]
    check(selection["coverage_sha"] == provenance["coverage_sha"], f"{here}.coverage_sha",
          "must equal the compact provenance coverage")
    check(selection["expectation_sha256"] == compact["expectation"]["sha256"], f"{here}.expectation_sha256",
          "must equal the embedded expectation.json hash")
    check(selection["manifest_sha256"] == compact_identity_sha256(compact), f"{here}.manifest_sha256",
          "must equal the compact manifest identity (canonical manifest without its selection member)")
    selected = selection["selected_artifact"]
    check({field: selected[field] for field in _SELECTED_ARTIFACT_FIELDS} == dict(compact["source_artifact"]),
          f"{here}.selected_artifact", "must be the compact manifest's source_artifact")
    source = selection["source"]
    check(source["reuse"] == provenance["reuse"], f"{here}.source.reuse", "must equal the compact provenance")
    for record, claim in (("handoff_run", "handoff"), ("tested_run", "tested")):
        check({field: source[record][field] for field in RUN_CLAIM_FIELDS} == dict(provenance[claim]),
              f"{here}.source.{record}", f"must carry exactly the compact provenance.{claim} claim")
    names = compact["extensions"]["names"] if compact["extensions"] is not None else []
    check(selection["extensions_verified"] == names, f"{here}.extensions_verified",
          "must list exactly the compact manifest's extension names")
    binding = selection["binding"]
    check(binding["frames"] == len(compact["frames"]), f"{here}.binding.frames", "must count the compact frames")
    check(binding["derivatives"] == len({frame["derivative"]["path"] for frame in compact["frames"]}),
          f"{here}.binding.derivatives", "must count the distinct derivative files")
    composed = compact["scope"]["kind"] == "composed"
    check(composed == ("composition" in selection), f"{here}.composition",
          "is required exactly for a composed compact bundle")
    if composed:
        components = compact["scope"]["components"]
        composition = selection["composition"]
        baseline, artifact = components["baseline"], composition["baseline_artifact"]
        check((artifact["id"], artifact["name"], artifact["digest"])
              == (baseline["artifact_id"], baseline["name"], baseline["digest"]),
              f"{here}.composition.baseline_artifact", "must name the compact's scope.components.baseline")
        check(composition["selected_manifest_sha256"] == components["selected_manifest_sha256"],
              f"{here}.composition.selected_manifest_sha256", "must equal scope.components.selected_manifest_sha256")


# -- §3.7 promotion --------------------------------------------------------------------------------

_PROMOTION_DRAFT_FIELDS: dict[str, Validator] = {
    **_header("mod-base.promotion"),
    "repository": REPOSITORY,
    "implementation": IMPLEMENTATION,
    "kit": KIT_REF,
    "heads": Map(g.BRANCH, SHA1, min_items=1, max_items=lim.MAX_KEYS, max_key_len=200),
    "bundles": List(
        Obj({"key": KEY, "collected_artifact_id": RUN_ID, "collected_digest": DIGEST, "manifest_sha256": SHA256,
             "coverage_sha": SHA1, "selected_artifact_id": RUN_ID}),
        min_items=1,
        max_items=lim.MAX_KEYS,
    ),
    "families": List(
        Obj(
            {"family": FAMILY, "key": KEY, "available": Bool(),
             "status": Str(choices=("available", "superseded", "unavailable"))},
            {"collected_artifact_id": RUN_ID, "collected_digest": DIGEST, "coverage_sha": SHA1,
             "selected_artifact_id": RUN_ID},
        ),
        max_items=lim.MAX_KEYS * lim.MAX_FAMILIES,
    ),
}
_PROMOTION = Obj(
    {**_PROMOTION_DRAFT_FIELDS,
     "site": Obj({"files": Int(1, lim.MAX_SITE_FILES), "bytes": Int(1, lim.MAX_SITE_BYTES), "inventory_sha256": SHA256})}
)
_PROMOTION_DRAFT = Obj(_PROMOTION_DRAFT_FIELDS)
_FAMILY_COLLECTED_FIELDS = ("collected_artifact_id", "collected_digest", "coverage_sha", "selected_artifact_id")


def validate_promotion(document: Any, *, draft: bool = False, path: str = "$") -> dict[str, Any]:
    """``mod-base.promotion`` v1 (SPEC §3.7).

    Checks: bundle keys unique; ``(family, key)`` unique and every family key is an ordinary key;
    ``available`` iff ``status == "available"``, and the collected fields are present exactly for
    available families with ``coverage_sha`` equal to the ordinary bundle's coverage; ``heads`` maps
    the implementation branch to the implementation SHA.

    ``draft=True`` validates the promotion **draft** that ``build`` hands to the adapter's
    ``verify_publication`` hook (SPEC §5.3.2 step 8): the same document without ``site``, which
    only exists after rendering and sealing (step 9); a draft carrying ``site`` is rejected, so no
    step can pass fabricated inventory numbers. The final promotion (step 10) requires ``site``.
    """

    (_PROMOTION_DRAFT if draft else _PROMOTION)(document, path)
    implementation = document["implementation"]
    _check_implementation(implementation, document["repository"], f"{path}.implementation")
    check(document["heads"].get(implementation["branch"]) == implementation["sha"], f"{path}.heads",
          "must map the implementation branch to the implementation SHA")
    bundles = _index_unique(document["bundles"], "key", f"{path}.bundles")
    seen: set[tuple[str, str]] = set()
    for position, family in enumerate(document["families"]):
        here = f"{path}.families[{position}]"
        marker = (family["family"], family["key"])
        check(marker not in seen, here, "duplicates a (family, key) entry")
        seen.add(marker)
        check(family["key"] in bundles, f"{here}.key", "is not an ordinary bundle key")
        available = family["available"]
        check(available == (family["status"] == "available"), f"{here}.available", "must match status")
        present = [field for field in _FAMILY_COLLECTED_FIELDS if field in family]
        check(present == list(_FAMILY_COLLECTED_FIELDS) if available else not present, here,
              "collected fields are required exactly for an available family")
        if available:
            check(family["coverage_sha"] == bundles[family["key"]]["coverage_sha"], f"{here}.coverage_sha",
                  "must equal the ordinary bundle coverage")
    return document


# -- §3.8 build ------------------------------------------------------------------------------------

_BUILD = Obj(
    {
        **_header("mod-base.build"),
        "repository": REPOSITORY,
        "implementation": Obj({"sha": SHA1, "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT, "run_url": RUN_URL,
                               "workflow_ref": Str(max_len=500)}),
        "kit": KIT_REF,
        "pixel_metrics_version": Const(PIXEL_METRICS_VERSION),
        "site_inventory_sha256": SHA256,
    }
)


def validate_build(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.build`` v1 (SPEC §3.8): ``run_url`` is built from the repository and run id, and
    ``workflow_ref`` is this repository's ``pages.yml`` on a branch."""

    _BUILD(document, path)
    implementation = document["implementation"]
    check(implementation["run_url"] == g.run_url(document["repository"], implementation["run_id"]),
          f"{path}.implementation.run_url", "must be the run URL of implementation.run_id")
    try:
        ref = g.parse_workflow_ref(implementation["workflow_ref"])
    except MbError as exc:
        raise fail(f"{path}.implementation.workflow_ref", "must be a branch-scoped workflow ref") from exc
    check(ref.repository == document["repository"] and ref.path == PAGES_WORKFLOW_PATH,
          f"{path}.implementation.workflow_ref", "must name this repository's .github/workflows/pages.yml")
    return document


# -- §3.9 front-end data ---------------------------------------------------------------------------

def DISPLAY(max_len: int) -> Validator:  # noqa: N802 - reads like the other field validators
    return Str(max_len=max_len, text="display")


LINK_ID = Str(g.SCENARIO, max_len=32)
PROJECT_LINK = Obj({"id": LINK_ID, "title": DISPLAY(40), "description": DISPLAY(160), "url": _https_url()})

_SITE = Obj(
    {
        **_header("mod-base.site"),
        "project": Obj(
            {
                "name": DISPLAY(60),
                "tagline": DISPLAY(120),
                "eyebrow": DISPLAY(60),
                "description": DISPLAY(400),
                "license_label": DISPLAY(60),
                "repository_url": _https_url(),
                "issues_url": _https_url(),
                "icon": OneOf(Null(), Const("assets/icon.png")),
                "links": List(PROJECT_LINK, max_items=lim.MAX_PROJECT_LINKS, unique_by=lambda link: link["id"]),
            }
        ),
        "gallery_url": Const("e2e/"),
        "releases": List(
            Obj(
                {
                    "key": KEY,
                    "label": LABEL,
                    "minecraft": List(MINECRAFT, min_items=1, max_items=lim.MAX_LANES, unique=True),
                    "loaders": List(LOADER, min_items=1, max_items=32, unique=True),
                    "loader_names": List(LABEL, min_items=1, max_items=32),
                    "frame_count": Int(1, lim.MAX_FRAMES),
                    "lane_count": Int(1, lim.MAX_LANES),
                    "short_sha": Str(max_len=12, min_len=7),
                    "subject_commit": SHA1,
                    "tested_run_url": RUN_URL,
                }
            ),
            max_items=lim.MAX_KEYS,
            unique_by=lambda release: release["key"],
        ),
        "families": List(Obj({"family": FAMILY, "title": LABEL, "lane_count": Int(0, lim.MAX_FAMILY_LANES),
                              "available": Bool()}),
                         max_items=lim.MAX_FAMILIES, unique_by=lambda family: family["family"]),
        "copy": Obj({"principles": List(DISPLAY(400), max_items=lim.MAX_COPY_PARAGRAPHS),
                     "evidence_lead": DISPLAY(1200)}),
        "generated": Obj({"implementation_sha": SHA1, "kit_sha": SHA1, "kit_version": VERSION, "pages_run_url": RUN_URL}),
    }
)


def validate_site(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.site`` v1 (SPEC §3.9 ``site-data.json``): unique release keys and families,
    ``loader_names`` parallel to ``loaders``, ``short_sha`` a prefix of ``subject_commit``."""

    _SITE(document, path)
    for position, release in enumerate(document["releases"]):
        here = f"{path}.releases[{position}]"
        check(len(release["loader_names"]) == len(release["loaders"]), f"{here}.loader_names",
              "must be parallel to loaders")
        check(release["subject_commit"].startswith(release["short_sha"]), f"{here}.short_sha",
              "must be a prefix of subject_commit")
    return document


def LABEL_MAP(pattern: Any) -> Validator:  # noqa: N802
    return Map(pattern, LABEL, max_items=lim.MAX_LABEL_ENTRIES)


GALLERY_LANE = Obj(
    {
        "lane_id": LANE_ID,
        "key": KEY,
        "artifact_node": ARTIFACT_NODE,
        "minecraft": MINECRAFT,
        "loader": LOADER,
        "loader_name": LABEL,
        "scenario": SCENARIO,
        "roles": List(ROLE, min_items=1, max_items=lim.MAX_ROLES_PER_LANE, unique=True),
        "status": Const("pass"),
        "jars": JARS,
    },
    {"elapsed_s": Num(0.0, 10_000_000.0), "epoch": Str(choices=("baseline", "selected")),
     "baseline_run": Obj({"status": Const("pass"), "jars": JARS}, {"elapsed_s": Num(0.0, 10_000_000.0)})},
)
GALLERY_FRAME = Obj(
    {
        **{field: validator for field, validator in FRAME_FIELDS.items()},
        "key": KEY,
        "loader_name": LABEL,
        "image": BUNDLE_PATH,
        "width": Int(1, lim.MAX_IMAGE_DIMENSION),
        "height": Int(1, lim.MAX_IMAGE_DIMENSION),
        "alt": Str(max_len=400, text="evidence"),
        "source": Obj({"width": Int(1, lim.MAX_IMAGE_DIMENSION), "height": Int(1, lim.MAX_IMAGE_DIMENSION),
                       "file_sha256": SHA256, "pixel": PIXEL_METRICS}),
        "published": Obj({"file_sha256": SHA256, "format": Const("webp"), "pixel": PIXEL_METRICS}),
        "provenance": Obj({"handoff_run_url": RUN_URL, "handoff_commit": SHA1, "tested_run_url": RUN_URL,
                           "tested_commit": SHA1, "tested_created_at": TIMESTAMP, "coverage_sha": SHA1}),
    },
    {"epoch": Str(choices=("baseline", "selected"))},
)
GALLERY_RELEASE = Obj(
    {
        "key": KEY,
        "label": LABEL,
        "minecraft": List(MINECRAFT, min_items=1, max_items=lim.MAX_LANES, unique=True),
        "loaders": List(LOADER, min_items=1, max_items=32, unique=True),
        "loader_names": List(LABEL, min_items=1, max_items=32),
        "frame_count": Int(1, lim.MAX_FRAMES),
        "lane_count": Int(1, lim.MAX_LANES),
        "scenarios": List(SCENARIO, min_items=1, max_items=lim.MAX_SCENARIOS, unique=True),
        "contract_sha256": SHA256,
        "contract_url": _https_url(500),
        "matrix_sha256": SHA256,
        "subject": SUBJECT,
        "coverage_sha": SHA1,
        "short_sha": Str(max_len=12, min_len=7),
        "handoff": Obj({"run_id": RUN_ID, "run_url": RUN_URL, "created_at": TIMESTAMP}),
        "tested": Obj({"run_id": RUN_ID, "run_url": RUN_URL, "created_at": TIMESTAMP, "commit": SHA1}),
        "scope": Str(choices=("complete", "composed")),
        "reuse": REUSE,
    }
)
GALLERY_FAMILY_RELEASE = Obj(
    {"key": KEY, "available": Bool(), "status": FAMILY_STATUS},
    {
        "coverage_sha": SHA1,
        "contracts": Map(g.CONTRACT_NAME, SHA256, max_items=lim.MAX_FAMILY_CONTRACTS, max_key_len=64),
        "links": List(Obj({"label": LINK_LABEL, "run_url": RUN_URL}), max_items=lim.MAX_FAMILY_LINKS,
                      unique_by=lambda link: link["label"]),
        "image_policy": FAMILY_IMAGE_POLICY,
    },
)
#: The per-key fields a family release carries exactly when that key's family evidence is available.
GALLERY_FAMILY_RELEASE_AVAILABLE = ("coverage_sha", "contracts", "links", "image_policy")
GALLERY_FAMILY = Obj(
    {
        "family": FAMILY,
        "title": LABEL,
        "description": DISPLAY(400),
        "available": Bool(),
        "status": FAMILY_STATUS,
        "releases": List(GALLERY_FAMILY_RELEASE, max_items=lim.MAX_KEYS, unique_by=lambda release: release["key"]),
        "lanes": List(Obj({**PAIRED_LANE_FIELDS, "key": KEY}), max_items=lim.MAX_KEYS * lim.MAX_FAMILY_LANES),
        "not_applicable": List(Obj({**NOT_APPLICABLE_FIELDS, "key": KEY}),
                               max_items=lim.MAX_KEYS * lim.MAX_NOT_APPLICABLE),
    }
)

_GALLERY = Obj(
    {
        **_header("mod-base.gallery"),
        "project": Obj({"name": DISPLAY(60), "repository_url": _https_url(), "actions_url": _https_url()}),
        "labels": Obj(
            {
                "scenarios": LABEL_MAP(g.SCENARIO),
                "roles": LABEL_MAP(g.ROLE),
                "tiers": LABEL_MAP(g.REVIEW_TIER),
                "loaders": LABEL_MAP(g.LOADER),
                "release_prefix": DISPLAY(40),
                "search_placeholder": DISPLAY(80),
            }
        ),
        "copy": Obj(
            {
                "gallery_lead": DISPLAY(1200),
                "methodology": List(DISPLAY(1500), max_items=lim.MAX_COPY_PARAGRAPHS),
                "family_notes": Map(g.FAMILY, DISPLAY(500), max_items=lim.MAX_FAMILIES, max_key_len=32),
            }
        ),
        "releases": List(GALLERY_RELEASE, min_items=1, max_items=lim.MAX_KEYS),
        "lanes": List(GALLERY_LANE, min_items=1, max_items=lim.MAX_KEYS * lim.MAX_LANES),
        "frames": List(GALLERY_FRAME, min_items=1, max_items=lim.MAX_KEYS * lim.MAX_FRAMES),
        "comparisons": List(
            Obj({"comparison_id": IDENT, "key": KEY, "lane_id": LANE_ID, "role": ROLE, "first_frame_id": IDENT,
                 "second_frame_id": IDENT, "source": COMPARE_METRICS, "published": COMPARE_METRICS}),
            max_items=lim.MAX_KEYS * lim.MAX_COMPARISONS,
        ),
        "families": List(GALLERY_FAMILY, max_items=lim.MAX_FAMILIES, unique_by=lambda family: family["family"]),
        "build": Obj({"implementation_sha": SHA1, "kit_sha": SHA1, "kit_version": VERSION,
                      "pixel_metrics_version": Const(PIXEL_METRICS_VERSION)}),
    }
)


#: The file name of a workflow (``actions_url`` names ``<repository>/actions/workflows/<file>``).
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.ya?ml$")


def _gallery_repository(document: Mapping[str, Any], path: str) -> str:
    """The repository every gallery URL must belong to: ``project.repository_url`` is exactly
    ``https://github.com/<repository>``; ``actions_url`` is one of its workflow pages."""

    project = document["project"]
    prefix = "https://github.com/"
    repository = project["repository_url"][len(prefix):] if project["repository_url"].startswith(prefix) else ""
    check(g.is_match(g.REPOSITORY, repository), f"{path}.project.repository_url",
          "must be https://github.com/<owner>/<name>")
    actions = project["actions_url"]
    workflows = f"{project['repository_url']}/actions/workflows/"
    check(actions.startswith(workflows) and g.is_match(WORKFLOW_FILE, actions[len(workflows):]),
          f"{path}.project.actions_url", "must be a workflow page of project.repository_url")
    return repository


def _family_status(releases: list[Mapping[str, Any]]) -> tuple[bool, str]:
    """A family is available when any key is; otherwise ``superseded`` when every listed key is
    superseded (contract drift), else ``unavailable`` (including a family with no key yet)."""

    if any(release["available"] for release in releases):
        return True, "available"
    if releases and all(release["status"] == "superseded" for release in releases):
        return False, "superseded"
    return False, "unavailable"


def _check_gallery_families(document: Mapping[str, Any], repository: str,
                            releases: Mapping[str, Mapping[str, Any]], path: str) -> None:
    run_prefix = f"https://github.com/{repository}/actions/runs/"
    families = {family["family"] for family in document["families"]}
    for note in document["copy"]["family_notes"]:
        check(note in families, f"{path}.copy.family_notes.{note}", "names no listed family")
    for position, family in enumerate(document["families"]):
        here = f"{path}.families[{position}]"
        boxes: dict[str, list[int]] = {}
        for release_position, release in enumerate(family["releases"]):
            release_path = f"{here}.releases[{release_position}]"
            key = release["key"]
            check(key in releases, f"{release_path}.key", "is not a release key of this gallery")
            check(release["available"] == (release["status"] == "available"), f"{release_path}.available",
                  "must match status")
            present = [field for field in GALLERY_FAMILY_RELEASE_AVAILABLE if field in release]
            check(present == list(GALLERY_FAMILY_RELEASE_AVAILABLE) if release["available"] else not present,
                  release_path, "coverage_sha, contracts, links and image_policy exist exactly for an available key")
            if not release["available"]:
                continue
            check(release["coverage_sha"] == releases[key]["coverage_sha"], f"{release_path}.coverage_sha",
                  "must equal the ordinary release coverage of its key")
            for link_position, link in enumerate(release["links"]):
                check(link["run_url"].startswith(run_prefix), f"{release_path}.links[{link_position}].run_url",
                      "must be a run of this repository")
            boxes[key] = release["image_policy"]["derivative_box"]
        available, status = _family_status(family["releases"])
        check(family["available"] == available and family["status"] == status, here,
              f"available/status must summarize its releases ({str(available).lower()}, {status!r})")
        for field in ("lanes", "not_applicable"):
            for item_position, item in enumerate(family[field]):
                check(item["key"] in boxes, f"{here}.{field}[{item_position}].key",
                      "must be an available release key of this family")
        _check_paired_lanes(family["lanes"], family["not_applicable"], lambda key: boxes[key], here,
                            image_prefix=f"families/{family['family']}/images/")


def validate_gallery(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.gallery`` v1 (SPEC §3.9 ``e2e/gallery-data.json``, with the per-key family
    amendment documented in ``docs/SCHEMAS.md``).

    Checks: ``project.repository_url`` is ``https://github.com/<repository>`` and every run URL is a
    run of that repository (release handoff/tested URLs are exactly ``run_url(repository, run_id)``);
    release keys unique; lanes unique per ``(key, lane_id)`` and owned by a release; frames unique
    per ``(key, frame_id)``, owned by their lane (same node, Minecraft, loader, loader name,
    scenario, role) with ``image == images/<key>/<published.file_sha256>.webp`` and
    ``width``/``height`` equal to the published metrics; release ``frame_count``/``lane_count`` and
    ``loader_names`` agree with the data; comparisons reference same-key, same-lane, same-role
    frames; a lane's ``baseline_run`` (the baseline execution of a re-tested lane's ``epoch:
    baseline`` frames) exists exactly when a lane with ``epoch: selected`` holds a baseline frame;
    family notes name listed families. Families: one entry per family whose ``releases``
    list its keys (each a release key; ``coverage_sha``, ``contracts``, ``links`` and
    ``image_policy`` exactly for an available key, whose coverage equals the ordinary release's);
    ``available``/``status`` summarize the releases; every lane and not-applicable entry names an
    available key; family images live at ``families/<family>/images/<sha256>.webp`` sized by their
    key's policy, and every pair is publishable. ``families: []`` is valid.
    """

    _GALLERY(document, path)
    repository = _gallery_repository(document, path)
    run_prefix = f"https://github.com/{repository}/actions/runs/"
    releases = _index_unique(document["releases"], "key", f"{path}.releases")
    lanes: dict[tuple[str, str], Mapping[str, Any]] = {}
    lane_counts: dict[str, int] = {key: 0 for key in releases}
    for position, lane in enumerate(document["lanes"]):
        here = f"{path}.lanes[{position}]"
        marker = (lane["key"], lane["lane_id"])
        check(lane["key"] in releases, f"{here}.key", "is not a release key")
        check(marker not in lanes, here, "duplicates a (key, lane_id)")
        lanes[marker] = lane
        lane_counts[lane["key"]] += 1
    frames: dict[tuple[str, str], Mapping[str, Any]] = {}
    frame_counts: dict[str, int] = {key: 0 for key in releases}
    baseline_lanes: set[tuple[str, str]] = set()
    for position, frame in enumerate(document["frames"]):
        here = f"{path}.frames[{position}]"
        marker = (frame["key"], frame["frame_id"])
        check(marker not in frames, here, "duplicates a (key, frame_id)")
        frames[marker] = frame
        lane = lanes.get((frame["key"], frame["lane_id"]))
        check(lane is not None, f"{here}.lane_id", "references no lane of its key")
        for field in ("artifact_node", "minecraft", "loader", "loader_name", "scenario"):
            check(frame[field] == lane[field], f"{here}.{field}", "must equal its lane's value")
        check(frame["role"] in lane["roles"], f"{here}.role", "is not a role of its lane")
        published = frame["published"]
        check(frame["image"] == f"images/{frame['key']}/{published['file_sha256']}.webp", f"{here}.image",
              "must be images/<key>/<published.file_sha256>.webp")
        check(published["pixel"]["file_sha256"] == published["file_sha256"], f"{here}.published.pixel",
              "file_sha256 must equal published.file_sha256")
        check((frame["width"], frame["height"]) == (published["pixel"]["width"], published["pixel"]["height"]),
              here, "width/height must equal the published metrics")
        check((frame["source"]["width"], frame["source"]["height"])
              == (frame["source"]["pixel"]["width"], frame["source"]["pixel"]["height"])
              and frame["source"]["pixel"]["file_sha256"] == frame["source"]["file_sha256"],
              f"{here}.source", "must agree with its metrics")
        for field in ("handoff_run_url", "tested_run_url"):
            check(frame["provenance"][field].startswith(run_prefix), f"{here}.provenance.{field}",
                  "must be a run of this repository")
        frame_counts[frame["key"]] += 1
        if frame.get("epoch") == "baseline" and lane.get("epoch") == "selected":
            check("baseline_run" in lane, f"{here}.epoch",
                  "a baseline frame of a re-tested lane needs its lane's baseline_run")
            baseline_lanes.add((frame["key"], frame["lane_id"]))
    for position, lane in enumerate(document["lanes"]):
        if "baseline_run" in lane:
            check(lane.get("epoch") == "selected" and (lane["key"], lane["lane_id"]) in baseline_lanes,
                  f"{path}.lanes[{position}].baseline_run", "exists only for a re-tested lane with baseline frames")
    for position, release in enumerate(document["releases"]):
        here = f"{path}.releases[{position}]"
        check(release["frame_count"] == frame_counts[release["key"]], f"{here}.frame_count", "must equal its frames")
        check(release["lane_count"] == lane_counts[release["key"]], f"{here}.lane_count", "must equal its lanes")
        check(len(release["loader_names"]) == len(release["loaders"]), f"{here}.loader_names",
              "must be parallel to loaders")
        check(release["subject"]["commit"].startswith(release["short_sha"]), f"{here}.short_sha",
              "must be a prefix of subject.commit")
        for field in ("handoff", "tested"):
            check(release[field]["run_url"] == g.run_url(repository, release[field]["run_id"]),
                  f"{here}.{field}.run_url", "must be the run URL of its run_id in this repository")
    seen: set[tuple[str, str]] = set()
    for position, comparison in enumerate(document["comparisons"]):
        here = f"{path}.comparisons[{position}]"
        marker = (comparison["key"], comparison["comparison_id"])
        check(marker not in seen, here, "duplicates a (key, comparison_id)")
        seen.add(marker)
        for field in ("first_frame_id", "second_frame_id"):
            frame = frames.get((comparison["key"], comparison[field]))
            check(frame is not None and frame["lane_id"] == comparison["lane_id"] and frame["role"] == comparison["role"],
                  f"{here}.{field}", "must reference a frame of the same key, lane and role")
    _check_gallery_families(document, repository, releases, path)
    return document


# -- §8.1 template manifest and §1.5 kit stamp -----------------------------------------------------

_TEMPLATE_MANIFEST = Obj(
    {
        **_header("mod-base.template-manifest"),
        "files": List(
            Obj(
                {"path": REPO_PATH, "class": Str(choices=("managed", "fragment", "seeded")), "source": BUNDLE_PATH},
                {"markers": List(Str(max_len=200, text="evidence"), min_items=1, max_items=64, unique=True),
                 "lines": List(Str(max_len=200, text="evidence"), min_items=1, max_items=64, unique=True)},
            ),
            min_items=1,
            max_items=128,
            unique_by=lambda entry: entry["path"],
        ),
    }
)


def validate_template_manifest(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.template-manifest`` v1 (SPEC §8.1): unique paths; ``managed`` sources live under
    ``managed/`` and carry no markers/lines; ``fragment`` and ``seeded`` sources live under
    ``seed/``; only ``fragment`` entries may list required ``markers``/``lines``."""

    _TEMPLATE_MANIFEST(document, path)
    for position, entry in enumerate(document["files"]):
        here = f"{path}.files[{position}]"
        klass = entry["class"]
        prefix = "managed/" if klass == "managed" else "seed/"
        check(entry["source"].startswith(prefix), f"{here}.source", f"a {klass} source must live under {prefix}")
        if klass != "fragment":
            check("markers" not in entry and "lines" not in entry, here,
                  "only fragment files may list required markers or lines")
    return document


_KIT_STAMP = Obj({**_header("mod-base.kit-stamp"), "sha": SHA1, "version": VERSION, "tree_digest": DIGEST})


def validate_kit_stamp(document: Any, *, path: str = "$") -> dict[str, Any]:
    """``mod-base.kit-stamp`` v1 (``out/mod-base-kit/MOD_BASE_KIT.json``, SPEC §1.5)."""

    return _KIT_STAMP(document, path)


# -- Dispatch ----------------------------------------------------------------------------------------

VALIDATORS: dict[str, Callable[..., dict[str, Any]]] = {
    "mod-base.evidence.expectation": validate_expectation,
    "mod-base.evidence.handoff": validate_handoff,
    "mod-base.evidence.compact": validate_compact,
    "mod-base.evidence.anchor": validate_anchor,
    "mod-base.family.envelope": validate_family_envelope,
    "mod-base.family.paired": validate_family_paired,
    "mod-base.selection": validate_selection,
    "mod-base.promotion": validate_promotion,
    "mod-base.build": validate_build,
    "mod-base.site": validate_site,
    "mod-base.gallery": validate_gallery,
    "mod-base.template-manifest": validate_template_manifest,
    "mod-base.kit-stamp": validate_kit_stamp,
}

#: Default size bound for each kind's file.
MAX_DOCUMENT_BYTES: dict[str, int] = {
    "mod-base.evidence.expectation": lim.MAX_EXPECTATION_BYTES,
    "mod-base.evidence.handoff": lim.MAX_MANIFEST_BYTES,
    "mod-base.evidence.compact": lim.MAX_MANIFEST_BYTES,
    "mod-base.evidence.anchor": lim.MAX_MANIFEST_BYTES,
    "mod-base.family.envelope": lim.MAX_ENVELOPE_BYTES,
    "mod-base.family.paired": lim.MAX_PAIRED_BYTES,
    "mod-base.selection": lim.MAX_SELECTION_BYTES,
    "mod-base.promotion": lim.MAX_PROMOTION_BYTES,
    "mod-base.build": lim.MAX_BUILD_RECORD_BYTES,
    "mod-base.site": lim.MAX_SITE_DATA_BYTES,
    "mod-base.gallery": lim.MAX_GALLERY_DATA_BYTES,
    "mod-base.template-manifest": lim.MAX_TEMPLATE_MANIFEST_BYTES,
    "mod-base.kit-stamp": lim.MAX_KIT_STAMP_BYTES,
}

def validate_document(document: Any, *, kind: str | None = None, **context: Any) -> dict[str, Any]:
    """Validate ``document`` as its declared ``kind`` (which must equal ``kind`` when given).

    ``context`` is passed to the kind's validator (for example ``expectation=...``).
    ``mod-base.config`` documents are validated by :func:`mod_base.config.validate_config`.
    """

    if not isinstance(document, dict):
        raise fail("$", "document must be a JSON object")
    declared = document.get("kind")
    if not isinstance(declared, str) or declared not in VALIDATORS:
        raise fail("$.kind", f"is not a known document kind: {declared!r}"[:120])
    if kind is not None and declared != kind:
        raise fail("$.kind", f"must be {kind!r}")
    return VALIDATORS[declared](document, **context)


def load_document(data: bytes, *, kind: str, label: str | None = None, max_bytes: int | None = None,
                  **context: Any) -> dict[str, Any]:
    """Strictly decode ``data`` and validate it as ``kind``."""

    if kind not in VALIDATORS:
        raise MbError(f"unknown document kind {kind!r}")
    bound = max_bytes if max_bytes is not None else MAX_DOCUMENT_BYTES[kind]
    value = strict_loads(data, label=label or kind, max_bytes=bound)
    return validate_document(value, kind=kind, **context)
