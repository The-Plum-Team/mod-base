"""Modrinth: create one version per lane and reconcile it by SHA-512 (``https://api.modrinth.com/v2``).

A version is created with one ``POST /v2/version`` multipart request (a ``data`` JSON part and one
``file`` part), never replayed: Modrinth would accept the same bytes twice under two versions if
the first answer was lost. Reconciliation (:func:`reconcile`, Quick Skin ``classify_modrinth``)
looks the file up by its SHA-512 and lists the project's versions: it skips only an exact match of
hash, project and version number, and every collision (the version number held by other bytes, the
bytes held by another version or project, two versions with one number) fails closed.

Modrinth reads are public: a transport without a token reads the same answers for a listed project,
which is what the finalize job (which holds no secret) does. The token is a raw ``Authorization``
value (``mrp_...``), sent only to :data:`MODRINTH_ORIGIN`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.marketplace import limits
from mod_base.marketplace.transport import (
    USER_AGENT,
    HttpsTransport,
    MarketplaceError,
    MarketplaceNotFound,
    MarketplaceOutcomeUnknown,
    Reconciliation,
    file_digests,
    quote_segment,
    require_filename,
    require_match,
    require_text,
)
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json

__all__ = ["MODRINTH_ORIGIN", "VERSION_TYPES", "DEPENDENCY_TYPES", "ModrinthVersion", "Reconciliation", "headers",
           "version_by_sha512", "project_versions", "resolve_project", "create_version", "reconcile"]

MODRINTH_ORIGIN = "https://api.modrinth.com"
VERSION_TYPES = ("release", "beta", "alpha")
DEPENDENCY_TYPES = ("required", "optional", "incompatible", "embedded")
#: A Modrinth id (base62).
_ID = re.compile(r"^[0-9A-Za-z]{1,16}$")
#: A project id or slug as a dependency names it.
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
#: Modrinth's URL-safe version number characters, without quotes and backticks.
_VERSION_NUMBER = re.compile(r"^[A-Za-z0-9!@$().+,_-]+$")
_SHA512 = re.compile(r"^[0-9a-f]{128}$")


def headers(token: str | None) -> dict[str, str]:
    """The construction headers of a Modrinth transport: the raw token as ``Authorization`` (none
    for a public read) and the kit's ``User-Agent``, which Modrinth asks every client to set."""

    values = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token is not None:
        if not isinstance(token, str) or not token:
            raise MarketplaceError("Modrinth token is malformed", reason="environment")
        values["Authorization"] = token
    return values


def _require_origin(transport: HttpsTransport) -> None:
    if not isinstance(transport, HttpsTransport) or transport.origin != MODRINTH_ORIGIN:
        raise MarketplaceError(f"a Modrinth request needs a transport of {MODRINTH_ORIGIN}", reason="usage")


def _sha512(value: Any) -> str:
    if not isinstance(value, str) or _SHA512.fullmatch(value.lower()) is None:
        raise MarketplaceError("expected SHA-512 is not a hex digest", reason="usage")
    return value.lower()


def _names(values: Any, label: str, pattern: re.Pattern[str], maximum: int) -> None:
    if not isinstance(values, tuple) or not 0 < len(values) <= maximum:
        raise MarketplaceError(f"{label} must be a non-empty tuple of at most {maximum} names", reason="usage")
    for value in values:
        require_match(pattern, value, label)
    if len(set(values)) != len(values):
        raise MarketplaceError(f"{label} repeat a name", reason="usage")


@dataclass(frozen=True)
class ModrinthVersion:
    """The metadata of one Modrinth version (one lane), validated against Modrinth's own bounds so a
    refusal happens before the upload rather than as an HTTP 400 after it."""

    project_id: str
    version_number: str
    name: str
    changelog: str
    game_versions: tuple[str, ...]
    loaders: tuple[str, ...]
    version_type: str
    dependencies: tuple[tuple[str, str], ...]
    featured: bool

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or _ID.fullmatch(self.project_id) is None:
            raise MarketplaceError("Modrinth project_id must be the project's id", reason="usage")
        if (not isinstance(self.version_number, str)
                or not 0 < len(self.version_number) <= limits.MAX_MODRINTH_VERSION_NUMBER_CHARS
                or _VERSION_NUMBER.fullmatch(self.version_number) is None):
            raise MarketplaceError(f"Modrinth version_number must be 1 to {limits.MAX_MODRINTH_VERSION_NUMBER_CHARS} "
                                   "URL-safe characters", reason="usage")
        require_text(self.name, "Modrinth version name", limits.MAX_MODRINTH_NAME_CHARS)
        require_text(self.changelog, "Modrinth changelog", limits.MAX_MARKETPLACE_CHANGELOG_CHARS, allow_empty=True,
                     single_line=False)
        _names(self.game_versions, "Modrinth game versions", grammar.MINECRAFT, limits.MAX_MARKETPLACE_GAME_VERSIONS)
        _names(self.loaders, "Modrinth loaders", grammar.LOADER, limits.MAX_MARKETPLACE_LOADERS)
        if self.version_type not in VERSION_TYPES:
            raise MarketplaceError(f"Modrinth version_type must be one of {VERSION_TYPES}", reason="usage")
        if not isinstance(self.dependencies, tuple) or len(self.dependencies) > limits.MAX_MARKETPLACE_DEPENDENCIES:
            raise MarketplaceError("Modrinth dependencies must be a bounded tuple", reason="usage")
        projects: set[str] = set()
        for dependency in self.dependencies:
            if (not isinstance(dependency, tuple) or len(dependency) != 2 or not isinstance(dependency[0], str)
                    or _PROJECT.fullmatch(dependency[0]) is None or dependency[1] not in DEPENDENCY_TYPES):
                raise MarketplaceError(f"Modrinth dependency {dependency!r} is not (project, type)"[:200], reason="usage")
            if dependency[0] in projects:
                raise MarketplaceError(f"Modrinth dependency {dependency[0]} is declared twice", reason="usage")
            projects.add(dependency[0])
        if not isinstance(self.featured, bool):
            raise MarketplaceError("Modrinth featured must be a boolean", reason="usage")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MarketplaceError(f"{label} is not a JSON object")
    return value


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise MarketplaceError(f"{label} has no valid Modrinth id")
    return value


def version_by_sha512(transport: HttpsTransport, sha512: str) -> dict[str, Any] | None:
    """The version holding the file whose SHA-512 is ``sha512`` (``GET /v2/version_file/{hash}
    ?algorithm=sha512``), or None when Modrinth knows no such file (HTTP 404)."""

    _require_origin(transport)
    digest = _sha512(sha512)
    try:
        answer = transport.get_json(f"/v2/version_file/{digest}", {"algorithm": "sha512"})
    except MarketplaceNotFound:
        return None
    version = _object(answer, "Modrinth version_file answer")
    _id(version.get("id"), "Modrinth version_file answer")
    return version


def project_versions(transport: HttpsTransport, project: str) -> list[dict[str, Any]]:
    """Every version of ``project`` (``GET /v2/project/{id}/version``, one unpaginated array of at
    most ``limits.MAX_MODRINTH_VERSIONS``). ``include_changelog=false`` drops the changelog bodies,
    which reconciliation never reads; the byte bound holds either way."""

    _require_origin(transport)
    require_match(_PROJECT, project, "Modrinth project")
    answer = transport.get_json(f"/v2/project/{quote_segment(project)}/version", {"include_changelog": "false"})
    if not isinstance(answer, list) or len(answer) > limits.MAX_MODRINTH_VERSIONS:
        raise MarketplaceError(f"Modrinth versions of {project} are not an array of at most "
                               f"{limits.MAX_MODRINTH_VERSIONS} versions")
    for version in answer:
        _id(_object(version, "Modrinth version").get("id"), "Modrinth version")
    return answer


def resolve_project(transport: HttpsTransport, project: str) -> str:
    """The id of the project whose id or slug is ``project`` (``GET /v2/project/{id|slug}``):
    Modrinth's version data names a dependency by project id only."""

    _require_origin(transport)
    require_match(_PROJECT, project, "Modrinth project")
    answer = _object(transport.get_json(f"/v2/project/{quote_segment(project)}"), "Modrinth project")
    identifier = _id(answer.get("id"), f"Modrinth project {project}")
    slug = answer.get("slug")
    if project != identifier and not (isinstance(slug, str) and slug.lower() == project.lower()):
        raise MarketplaceError(f"Modrinth answered another project for {project}")
    return identifier


def create_version(transport: HttpsTransport, version: ModrinthVersion, file: Path, filename: str, *,
                   sha256: str | None = None) -> dict[str, Any]:
    """Create ``version`` holding ``file`` uploaded as ``filename`` and return Modrinth's version.

    Requests: one ``GET /v2/project/{dependency}`` per dependency (its id), then one never-replayed
    ``POST /v2/version``. ``sha256`` (the plan's digest) must match the file before anything is
    sent, and the answer must report the same project, version number and a file of this name,
    size and SHA-512; an unreadable success answer raises
    :class:`~mod_base.marketplace.transport.MarketplaceOutcomeUnknown`."""

    _require_origin(transport)
    if not isinstance(version, ModrinthVersion):
        raise MarketplaceError("create_version needs a ModrinthVersion", reason="usage")
    require_filename(filename)
    digests = file_digests(file, sha256=sha256)
    dependencies = [{"dependency_type": kind, "project_id": resolve_project(transport, project)}
                    for project, kind in version.dependencies]
    if len({row["project_id"] for row in dependencies}) != len(dependencies):
        raise MarketplaceError("two Modrinth dependencies name the same project", reason="usage")
    data = {
        "changelog": version.changelog,
        "dependencies": dependencies,
        "featured": version.featured,
        "file_parts": ["file"],
        "game_versions": list(version.game_versions),
        "loaders": list(version.loaders),
        "name": version.name,
        "primary_file": "file",
        "project_id": version.project_id,
        "version_number": version.version_number,
        "version_type": version.version_type,
    }
    answer = transport.post_multipart("/v2/version", [("data", canonical_json(data).decode("utf-8"))],
                                      [("file", filename, file)], replay=False,
                                      expected_sha256={"file": digests.sha256})
    if not isinstance(answer, dict) or not isinstance(answer.get("id"), str) or _ID.fullmatch(answer["id"]) is None:
        raise MarketplaceOutcomeUnknown("Modrinth accepted POST /v2/version with an answer naming no version",
                                        status=200, method="POST", path="/v2/version")
    if answer.get("project_id") != version.project_id or answer.get("version_number") != version.version_number:
        raise MarketplaceError(f"Modrinth created version {answer['id']} under another project or version number")
    files = answer.get("files")
    if not isinstance(files, list) or not any(
            isinstance(row, dict) and row.get("filename") == filename and row.get("size") == digests.size
            and isinstance(row.get("hashes"), dict) and row["hashes"].get("sha512") == digests.sha512
            for row in files):
        raise MarketplaceError(f"Modrinth version {answer['id']} does not report the uploaded bytes of {filename}")
    return answer


def reconcile(remote_by_hash: dict[str, Any] | None, versions: list[dict[str, Any]], *, expected_sha512: str,
              size: int, project_id: str, version_number: str) -> Reconciliation:
    """Decide one lane against Modrinth (Quick Skin ``classify_modrinth``).

    ``remote_by_hash`` is :func:`version_by_sha512` of the plan's SHA-512 and ``versions`` is
    :func:`project_versions` of ``project_id``. Skip only when the hash belongs to a version of this
    project with this version number holding a file of this size and SHA-512, and that version is
    the only one carrying the number. No hash and no such version number publishes. Anything else is
    a collision: :class:`~mod_base.marketplace.transport.MarketplaceError`."""

    digest = _sha512(expected_sha512)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise MarketplaceError("expected size must be a positive integer", reason="usage")
    if not isinstance(project_id, str) or _ID.fullmatch(project_id) is None:
        raise MarketplaceError("Modrinth project_id must be the project's id", reason="usage")
    if not isinstance(version_number, str) or not version_number:
        raise MarketplaceError("Modrinth version_number is empty", reason="usage")
    if not isinstance(versions, list) or any(not isinstance(row, dict) for row in versions):
        raise MarketplaceError("Modrinth versions must be a list of objects", reason="usage")
    for row in versions:
        if "project_id" in row and row["project_id"] != project_id:
            raise MarketplaceError("the Modrinth version listing holds a version of another project")
    conflicting = [row for row in versions if row.get("version_number") == version_number]
    if remote_by_hash is None:
        if conflicting:
            raise MarketplaceError(f"Modrinth version {version_number} exists with different bytes")
        return Reconciliation(True, None)
    remote = _object(remote_by_hash, "Modrinth version_file answer")
    remote_id = _id(remote.get("id"), "Modrinth version_file answer")
    files = remote.get("files")
    if not isinstance(files, list) or len(files) > limits.MAX_MODRINTH_VERSION_FILES:
        raise MarketplaceError("Modrinth version_file answer has no bounded file list")
    exact_file = any(
        isinstance(row, dict) and row.get("size") == size and isinstance(row.get("hashes"), dict)
        and row["hashes"].get("sha512") == digest
        for row in files)
    if remote.get("project_id") != project_id or remote.get("version_number") != version_number or not exact_file:
        raise MarketplaceError("the expected Modrinth file hash is already bound to another publication identity")
    if len(conflicting) != 1 or conflicting[0].get("id") != remote_id:
        raise MarketplaceError(f"Modrinth publication identity {version_number} is ambiguous")
    return Reconciliation(False, remote_id)
