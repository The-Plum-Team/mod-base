"""Strict loader and validator for ``site/mod-base.json`` (``mod-base.config`` v1, SPEC §4.1).

:func:`validate_config` is the pure structural check (exact keys, types, grammar, bounds and every
cross-field rule). :func:`load_config` additionally reads the file with a bounded, symlink-refusing
read and, unless ``check_repository_facts=False``, verifies the repository facts at the checked-out
protected head: the adapter, the source and family producer workflows and the declared fixtures
module exist as regular files, every ``python_path`` entry is a real directory inside the
repository, no path crosses a symlink, and the icon is a PNG within 512 KiB and 1024x1024.
Rotation, which only sparse-checks-out the config, loads with ``check_repository_facts=False``.
"""

from __future__ import annotations

import os
import re
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base import PIXEL_METRICS_VERSION
from mod_base.adapter.protocol import ADAPTER_API_WINDOW, NETWORK_HOOKS
from mod_base.errors import MbError
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.canonical import read_regular_file, sha256_hex, strict_loads
from mod_base.model.documents import is_https_url
from mod_base.model.validators import (
    Bool,
    Const,
    DocumentError,
    Int,
    List,
    Map,
    Null,
    Nullable,
    Obj,
    OneOf,
    Size,
    Str,
    Validator,
    check,
    fail,
)

KIND = "mod-base.config"
DEFAULT_CONFIG_PATH = "site/mod-base.json"
THEME_KEYS = (
    "bg",
    "surface",
    "surface_raised",
    "surface_soft",
    "text",
    "muted",
    "line",
    "accent",
    "accent_strong",
    "highlight",
    "danger",
    "image_well",
)
COLOR = re.compile(r"^#[0-9a-f]{6}$")
MATRIX_FIELD = re.compile(r"^[a-z_][a-z0-9_]{0,63}(?:\.[a-z_][a-z0-9_]{0,63}){0,7}$")
LINK_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _display(max_len: int) -> Validator:
    return Str(max_len=max_len, text="display")


def _repo_path(*, prefix: str | None = None, suffix: str | None = None, allow_dot: bool = False) -> Validator:
    def validate(value: Any, path: str) -> str:
        if allow_dot and value == ".":
            return value
        if not g.is_repo_path(value):
            raise fail(path, "must be a repository-relative path without '..', '.' or .git components")
        if prefix is not None and not value.startswith(prefix):
            raise fail(path, f"must start with {prefix}")
        if suffix is not None and not value.endswith(suffix):
            raise fail(path, f"must end with {suffix}")
        return value

    return validate


def _url(value: Any, path: str) -> str:
    if not is_https_url(value) or len(value) > 300:
        raise fail(path, "must be an https URL with a [a-z0-9.-] host, no userinfo, port or whitespace")
    return value


def _template_text(placeholder: str | None) -> Validator:
    """A job/step/title template: evidence text whose only braces form ``{placeholder}`` once."""

    def validate(value: Any, path: str) -> str:
        Str(max_len=lim.MAX_JOB_NAME_LENGTH, text="evidence")(value, path)
        remainder = value.replace("{" + placeholder + "}", "", 1) if placeholder else value
        if "{" in remainder or "}" in remainder or "${{" in value:
            allowed = f"a single {{{placeholder}}}" if placeholder else "no"
            raise fail(path, f"may contain {allowed} placeholder and no other braces")
        return value

    return validate


def _required_template(placeholder: str) -> Validator:
    inner = _template_text(placeholder)

    def validate(value: Any, path: str) -> str:
        inner(value, path)
        if value.count("{" + placeholder + "}") != 1:
            raise fail(path, f"must contain {{{placeholder}}} exactly once")
        return value

    return validate


EVENTS = List(Str(g.EVENT, max_len=40), max_items=8, unique=True)
THEME = Obj({key: Str(COLOR, max_len=7) for key in THEME_KEYS})
IMAGE_POLICY_FAMILY = Obj(
    {"derivative_box": Size(1, lim.MAX_IMAGE_DIMENSION), "webp_quality": Int(1, 100), "webp_method": Int(0, 6)}
)

_CONFIG = Obj(
    {
        "kind": Const(KIND),
        "schema_version": Const(1),
        "project": Obj(
            {
                "name": _display(60),
                "tagline": _display(120),
                "eyebrow": _display(60),
                "description": OneOf(_display(400), Obj({"from_matrix": Str(MATRIX_FIELD, max_len=200)})),
                "license_label": _display(60),
                "icon": Nullable(Obj({"path": _repo_path(suffix=".png"),
                                      "rendering": Str(choices=("pixelated", "auto"))})),
                "links": List(
                    Obj({"id": Str(LINK_ID, max_len=32), "title": _display(40), "description": _display(160),
                         "url": _url}),
                    max_items=lim.MAX_PROJECT_LINKS,
                    unique_by=lambda link: link["id"],
                ),
            }
        ),
        "canonical_branch": Str(g.BRANCH, max_len=200),
        "adapter": Obj(
            {
                "path": _repo_path(prefix="scripts/pages/", suffix=".py"),
                "api": Int(1, 1000),
                "python_path": List(_repo_path(allow_dot=True), min_items=1, max_items=lim.MAX_ADAPTER_PYTHON_PATH,
                                    unique=True),
                "network_hooks": List(Str(choices=sorted(NETWORK_HOOKS)), max_items=len(NETWORK_HOOKS), unique=True),
                "extensions": List(Str(g.EXTENSION_NAME, max_len=g.MAX_EXTENSION_NAME_LENGTH),
                                   max_items=lim.MAX_EXTENSION_NAMES, unique=True),
            },
            {
                "timeout_seconds": Int(1, lim.ADAPTER_TIMEOUT_MAX_SECONDS),
                "fixtures_path": _repo_path(prefix="scripts/pages/", suffix=".py"),
            },
        ),
        "targets": OneOf(
            Obj({"mode": Const("default-branch"), "max": Int(1, lim.MAX_KEYS)}),
            Obj({"mode": Const("enrolled-branches"), "max": Int(1, lim.MAX_KEYS),
                 "max_branches": Int(1, lim.MAX_BRANCHES)}),
        ),
        "source": Obj(
            {
                "workflow": Str(g.WORKFLOW_PATH, max_len=130),
                "events": Obj({"canonical": EVENTS, "other": EVENTS}),
                "display_title": Nullable(_required_template("subject_commit")),
                "attestation_job": Nullable(_template_text(None)),
                "delegated_reuse_extension": Nullable(Str(g.EXTENSION_NAME, max_len=g.MAX_EXTENSION_NAME_LENGTH)),
                "require_job_graph": Bool(),
                "require_newest_run": Bool(),
                "handoff_job": _template_text("key"),
                "handoff_step": _template_text(None),
            }
        ),
        "images": Obj(
            {
                "source_size": Size(1, lim.MAX_IMAGE_DIMENSION),
                "derivative_box": Size(1, lim.MAX_IMAGE_DIMENSION),
                "webp_quality": Int(1, 100),
                "webp_method": Int(0, 6),
                "pixel_metrics_version": Const(PIXEL_METRICS_VERSION),
                "cross_check_runtime_metrics": Bool(),
            }
        ),
        "anchor": Obj({"enabled": Bool(), "retention_days": Int(1, lim.MAX_ANCHOR_RETENTION_DAYS),
                       "successor_grace_days": Int(0, lim.MAX_ANCHOR_RETENTION_DAYS)}),
        "admission": OneOf(
            Obj(
                {
                    "mode": Const("progress"),
                    "coalesce_seconds": Int(0, 86_400),
                    "partial_deadline_seconds": Int(0, 86_400),
                    "recovery_interval_seconds": Int(60, 86_400),
                    "max_failed_publications": Int(1, 10),
                    "defer_on_active_source_runs": Bool(),
                }
            ),
            Obj({"mode": Const("always"), "defer_on_active_source_runs": Bool()}),
        ),
        "baseline_archive": OneOf(
            Obj({"enabled": Const(True), "retention_days": Int(1, lim.MAX_BASELINE_RETENTION_DAYS)}),
            Obj({"enabled": Const(False)}),
        ),
        "families": List(
            Obj(
                {
                    "id": Str(g.FAMILY, max_len=g.MAX_FAMILY_LENGTH),
                    "title": _display(lim.MAX_LABEL_LENGTH),
                    "description": _display(400),
                    "producer": Obj(
                        {
                            "workflow": Str(g.WORKFLOW_PATH, max_len=130),
                            "events": List(Str(g.EVENT, max_len=40), min_items=1, max_items=8, unique=True),
                            "job": _template_text(None),
                            "step": _template_text(None),
                        }
                    ),
                    "handoff_max_bytes": Int(1, lim.MAX_FAMILY_HANDOFF_BYTES),
                    "retention_days": Int(1, lim.MAX_FAMILY_RETENTION_DAYS),
                    "image_policy": IMAGE_POLICY_FAMILY,
                    "carry_forward": Bool(),
                }
            ),
            max_items=lim.MAX_FAMILIES,
            unique_by=lambda family: family["id"],
        ),
        "labels": Obj(
            {
                "scenarios": Map(g.SCENARIO, _display(lim.MAX_LABEL_LENGTH), max_items=lim.MAX_LABEL_ENTRIES),
                "roles": Map(g.ROLE, _display(lim.MAX_LABEL_LENGTH), max_items=lim.MAX_LABEL_ENTRIES),
                "tiers": Map(g.REVIEW_TIER, _display(lim.MAX_LABEL_LENGTH), max_items=lim.MAX_LABEL_ENTRIES),
                "loaders": Map(g.LOADER, _display(lim.MAX_LABEL_LENGTH), max_items=lim.MAX_LABEL_ENTRIES),
                "release_prefix": _display(40),
                "search_placeholder": _display(80),
            }
        ),
        "copy": Obj(
            {
                "gallery_lead": _display(1200),
                "evidence_lead": _display(1200),
                "methodology": List(_display(1500), max_items=lim.MAX_COPY_PARAGRAPHS),
                "principles": List(_display(400), max_items=lim.MAX_COPY_PARAGRAPHS),
                "family_notes": Map(g.FAMILY, _display(500), max_items=lim.MAX_FAMILIES, max_key_len=32),
            }
        ),
        "theme": Obj({"dark": THEME, "light": OneOf(Null(), THEME)}),
        "template": Obj(
            {
                "agents_local": List(_repo_path(suffix=".md"), max_items=lim.MAX_TEMPLATE_PATHS, unique=True),
                "deferred": List(_repo_path(), max_items=lim.MAX_TEMPLATE_PATHS, unique=True),
            }
        ),
    }
)


def validate_config(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Validate a decoded ``mod-base.config`` v1 document and return it.

    Cross-field rules: ``adapter.api`` is in the kit's adapter window; ``fixtures_path`` differs
    from the adapter; ``delegated_reuse_extension`` is a declared extension; the source workflow has
    at least one canonical event; a family producer is not the source workflow; family notes name
    configured families; the derivative box and grace period fit their parents; the source image
    fits the pixel bound.
    """

    _CONFIG(document, path)
    adapter = document["adapter"]
    check(adapter["api"] in ADAPTER_API_WINDOW, f"{path}.adapter.api",
          f"must be in the kit's adapter window {sorted(ADAPTER_API_WINDOW)}")
    check(adapter.get("fixtures_path") != adapter["path"], f"{path}.adapter.fixtures_path",
          "must not be the privileged adapter module")
    source = document["source"]
    check(bool(source["events"]["canonical"]), f"{path}.source.events.canonical", "must name at least one event")
    extension = source["delegated_reuse_extension"]
    check(extension is None or extension in adapter["extensions"], f"{path}.source.delegated_reuse_extension",
          "must be one of adapter.extensions")
    images = document["images"]
    width, height = images["source_size"]
    check(width * height <= lim.MAX_IMAGE_PIXELS, f"{path}.images.source_size", "exceeds the pixel bound")
    anchor = document["anchor"]
    check(anchor["successor_grace_days"] <= anchor["retention_days"], f"{path}.anchor.successor_grace_days",
          "cannot exceed retention_days")
    admission = document["admission"]
    if admission["mode"] == "progress":
        check(admission["coalesce_seconds"] <= admission["partial_deadline_seconds"],
              f"{path}.admission.coalesce_seconds", "cannot exceed partial_deadline_seconds")
    family_ids = set()
    for position, family in enumerate(document["families"]):
        family_ids.add(family["id"])
        check(family["producer"]["workflow"] != source["workflow"], f"{path}.families[{position}].producer.workflow",
              "must differ from source.workflow")
    for note in document["copy"]["family_notes"]:
        check(note in family_ids, f"{path}.copy.family_notes.{note}", "names no configured family")
    return document


@dataclass(frozen=True)
class Config:
    """A validated ``mod-base.config`` v1. ``data`` is the validated document; treat it as read-only."""

    data: dict[str, Any]
    sha256: str
    path: str

    @property
    def canonical_branch(self) -> str:
        return self.data["canonical_branch"]

    @property
    def project(self) -> dict[str, Any]:
        return self.data["project"]

    @property
    def adapter(self) -> dict[str, Any]:
        return self.data["adapter"]

    @property
    def adapter_timeout_seconds(self) -> int:
        return self.data["adapter"].get("timeout_seconds", lim.ADAPTER_TIMEOUT_DEFAULT_SECONDS)

    @property
    def network_hooks(self) -> frozenset[str]:
        return frozenset(self.data["adapter"]["network_hooks"])

    @property
    def extension_names(self) -> frozenset[str]:
        return frozenset(self.data["adapter"]["extensions"])

    @property
    def targets(self) -> dict[str, Any]:
        return self.data["targets"]

    @property
    def source(self) -> dict[str, Any]:
        return self.data["source"]

    @property
    def images(self) -> dict[str, Any]:
        return self.data["images"]

    @property
    def anchor(self) -> dict[str, Any]:
        return self.data["anchor"]

    @property
    def admission(self) -> dict[str, Any]:
        return self.data["admission"]

    @property
    def baseline_archive(self) -> dict[str, Any]:
        return self.data["baseline_archive"]

    @property
    def families(self) -> list[dict[str, Any]]:
        return self.data["families"]

    @property
    def labels(self) -> dict[str, Any]:
        return self.data["labels"]

    @property
    def copy(self) -> dict[str, Any]:
        return self.data["copy"]

    @property
    def theme(self) -> dict[str, Any]:
        return self.data["theme"]

    @property
    def template(self) -> dict[str, Any]:
        return self.data["template"]

    def family(self, family_id: str) -> dict[str, Any]:
        """Return the configured family ``family_id`` or raise :class:`MbError`."""

        for family in self.data["families"]:
            if family["id"] == family_id:
                return family
        raise MbError(f"family {family_id!r} is not configured", reason="unknown-family")

    def image_policy(self) -> dict[str, Any]:
        """The ``expectation.image_policy`` every expectation must equal (``config.images`` without
        ``cross_check_runtime_metrics``)."""

        images = self.data["images"]
        return {
            "source_size": list(images["source_size"]),
            "derivative_box": list(images["derivative_box"]),
            "webp_quality": images["webp_quality"],
            "webp_method": images["webp_method"],
            "pixel_metrics_version": images["pixel_metrics_version"],
        }


def parse_config(data: bytes, *, path: str = DEFAULT_CONFIG_PATH) -> Config:
    """Strictly decode and validate config bytes; ``path`` is recorded for messages only."""

    document = strict_loads(data, label=path, max_bytes=lim.MAX_CONFIG_BYTES)
    try:
        validate_config(document)
    except DocumentError as exc:
        raise DocumentError(f"{path}:{exc.path}", str(exc).split(": ", 1)[-1]) from exc
    return Config(data=document, sha256=sha256_hex(data), path=path)


def _inside(repo_root: Path, relative: str, label: str) -> tuple[Path, int]:
    """Resolve ``relative`` under ``repo_root`` refusing any symlink component; return (path, mode)."""

    root = Path(repo_root)
    try:
        root_info = root.lstat()
    except OSError as exc:
        raise MbError(f"repository root is not accessible: {exc.strerror or exc}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise MbError("repository root must be a real directory")
    current = root
    mode = root_info.st_mode
    if relative != ".":
        for part in relative.split("/"):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                raise MbError(f"{label} does not exist: {relative}", reason="missing-path") from None
            except OSError as exc:
                raise MbError(f"cannot inspect {label} {relative}: {exc.strerror or exc}") from exc
            if stat.S_ISLNK(info.st_mode):
                raise MbError(f"{label} crosses a symlink: {relative}")
            mode = info.st_mode
    return current, mode


def _require_file(repo_root: Path, relative: str, label: str) -> Path:
    path, mode = _inside(repo_root, relative, label)
    if not stat.S_ISREG(mode):
        raise MbError(f"{label} must be a regular file: {relative}")
    return path


def _check_icon(repo_root: Path, relative: str) -> None:
    path = _require_file(repo_root, relative, "project.icon.path")
    data = read_regular_file(path, label="project icon", max_bytes=lim.MAX_ICON_BYTES)
    if len(data) < 33 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        raise MbError("project icon must be a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if not (0 < width <= lim.MAX_ICON_DIMENSION and 0 < height <= lim.MAX_ICON_DIMENSION):
        raise MbError(f"project icon must be at most {lim.MAX_ICON_DIMENSION}x{lim.MAX_ICON_DIMENSION}")


def check_repository(config: Config, repo_root: Path) -> None:
    """Verify the config's repository facts at the checked-out head (see module docstring)."""

    data = config.data
    _require_file(repo_root, data["adapter"]["path"], "adapter.path")
    if "fixtures_path" in data["adapter"]:
        _require_file(repo_root, data["adapter"]["fixtures_path"], "adapter.fixtures_path")
    for entry in data["adapter"]["python_path"]:
        _, mode = _inside(repo_root, entry, "adapter.python_path entry")
        if not stat.S_ISDIR(mode):
            raise MbError(f"adapter.python_path entry must be a directory: {entry}")
    _require_file(repo_root, data["source"]["workflow"], "source.workflow")
    for family in data["families"]:
        _require_file(repo_root, family["producer"]["workflow"], f"families[{family['id']}].producer.workflow")
    if data["project"]["icon"] is not None:
        _check_icon(repo_root, data["project"]["icon"]["path"])


def load_config(repo_root: Path | str, config_path: Path | str | None = None, *,
                check_repository_facts: bool = True) -> Config:
    """Read, validate and (by default) repository-check the mod's config.

    ``config_path`` defaults to ``<repo_root>/site/mod-base.json``; an explicit path is taken as
    given (relative to the working directory, like ``--repo``). A config that lies lexically inside
    the repository is reached component by component from the root, refusing any symlink; one
    outside it (local development only) is read without following its final component.
    """

    root = Path(os.path.abspath(repo_root))
    if config_path is None:
        relative = DEFAULT_CONFIG_PATH
        target = _require_file(root, relative, "config")
    else:
        candidate = Path(os.path.abspath(config_path))
        try:
            relative = candidate.relative_to(root).as_posix()
        except ValueError:
            relative, target = candidate.name, candidate
        else:
            target = _require_file(root, relative, "config")
    raw = read_regular_file(target, label="mod-base config", max_bytes=lim.MAX_CONFIG_BYTES)
    config = parse_config(raw, path=relative)
    if check_repository_facts:
        check_repository(config, root)
    return config
