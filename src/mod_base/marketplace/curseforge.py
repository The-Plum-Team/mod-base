"""CurseForge: upload one file per lane and reconcile it from public data.

Three origins, one transport each:

* :data:`UPLOAD_ORIGIN` (``minecraft.curseforge.com``), the author upload API, which takes the
  ``X-Api-Token``: ``GET /api/game/version-types`` and ``/api/game/versions`` resolve game version
  names to ids, ``POST /api/projects/{id}/upload-file`` uploads (multipart ``metadata`` JSON plus
  ``file``; never replayed, since a CurseForge upload sent twice is two files);
* :data:`LISTING_ORIGIN` (``www.curseforge.com``), the unauthenticated first-party listing
  ``GET /api/v1/mods/{id}/files`` (the hash-bearing Core API needs a separately approved key the
  author token cannot satisfy, so the listing gives identity and the CDN gives the bytes);
* :data:`CDN_ORIGIN` (``mediafilez.forgecdn.net``), the bytes a downloader receives:
  ``/files/{id // 1000}/{id % 1000}/{stored name}``.

Reconciliation (:func:`reconcile`, Quick Skin ``classify_curseforge``) proves byte identity from
the published bytes themselves, which is stronger than trusting a marketplace-asserted digest and
lets SHA-256 take part. Only an approved file (:data:`APPROVED_STATUS`) is served by the CDN and
protected by CurseForge's own duplicate check, so a same-named file that is not approved yet is a
settling upload: :class:`~mod_base.marketplace.transport.PublicationPending`.

No token is ever given to the listing or CDN transports: they are built without headers.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.marketplace import limits
from mod_base.marketplace.transport import (
    USER_AGENT,
    HttpsTransport,
    MarketplaceError,
    MarketplaceOutcomeUnknown,
    MarketplaceUnavailable,
    PublicationPending,
    Reconciliation,
    file_digests,
    quote_segment,
    require_filename,
    require_positive,
    require_sha256,
    require_text,
)
from mod_base.model.canonical import canonical_json

__all__ = ["UPLOAD_ORIGIN", "LISTING_ORIGIN", "CDN_ORIGIN", "APPROVED_STATUS", "LOADER_NAMES", "RELEASE_TYPES",
           "RELATION_TYPES", "CurseForgeFile", "Reconciliation", "headers", "stored_name", "cdn_path", "download",
           "game_version_ids", "upload", "listing", "reconcile"]

UPLOAD_ORIGIN = "https://minecraft.curseforge.com"
LISTING_ORIGIN = "https://www.curseforge.com"
CDN_ORIGIN = "https://mediafilez.forgecdn.net"
APPROVED_STATUS = 4
#: Modrinth-style loader ids to the game version names CurseForge spells them with.
LOADER_NAMES = {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"}
RELEASE_TYPES = ("release", "beta", "alpha")
RELATION_TYPES = ("requiredDependency", "optionalDependency", "embeddedLibrary", "incompatible", "tool")
#: Game version types whose names :func:`game_version_ids` maps: every ``minecraft-*`` type plus these.
NAMED_VERSION_TYPES = frozenset({"modloader", "environment", "java"})
_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,98}[a-z0-9])?$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def headers(token: str) -> dict[str, str]:
    """The construction headers of an upload-API transport: the author token as ``X-Api-Token``."""

    if not isinstance(token, str) or not token:
        raise MarketplaceError("CurseForge token is malformed", reason="environment")
    return {"Accept": "application/json", "User-Agent": USER_AGENT, "X-Api-Token": token}


def _require_origin(transport: Any, origin: str) -> HttpsTransport:
    if not isinstance(transport, HttpsTransport) or transport.origin != origin:
        raise MarketplaceError(f"this CurseForge request needs a transport of {origin}", reason="usage")
    return transport


def stored_name(filename: str) -> str:
    """The name CurseForge stores an upload under.

    Since late October 2026 CurseForge turns every run of spaces and hyphens that holds a space into
    one hyphen, so ``Quick Skin - Fabric - 1.20.1-4.0.0.jar`` is listed and served as
    ``Quick-Skin-Fabric-1.20.1-4.0.0.jar`` (its CDN answers 403 for the original name); a run of
    hyphens alone is kept, and files uploaded earlier kept their original name. Reconciliation
    therefore accepts either name and downloads by the one the listing reports."""

    return re.sub(r"[ -]* [ -]*", "-", require_filename(filename))


def cdn_path(file_id: int, name: str) -> str:
    """The CDN path of a file: its id split into thousands and remainder, the remainder never
    zero-padded (a padded path answers 403), then the percent-encoded stored name."""

    require_positive(file_id, "CurseForge file id")
    return f"/files/{file_id // 1000}/{file_id % 1000}/{quote_segment(require_filename(name))}"


def download(cdn_transport: HttpsTransport, file_id: int, stored_name: str, *, limit: int) -> bytes:
    """Exactly ``limit`` bytes of a published file from the CDN (no credential travels there)."""

    _require_origin(cdn_transport, CDN_ORIGIN)
    return cdn_transport.get_bytes(cdn_path(file_id, stored_name), limit=limit)


def _rows(value: Any, label: str, maximum: int, fields: dict[str, type]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > maximum:
        raise MarketplaceError(f"CurseForge {label} is not an array of at most {maximum} rows")
    for row in value:
        if not isinstance(row, dict) or any(
                not isinstance(row.get(name), kind) or isinstance(row.get(name), bool) for name, kind in fields.items()):
            raise MarketplaceError(f"CurseForge {label} holds a malformed row")
    return value


def game_version_ids(transport: HttpsTransport) -> dict[str, int]:
    """``{game version name: id}`` from ``/api/game/version-types`` and ``/api/game/versions`` (two
    requests), covering the Minecraft versions (every ``minecraft-*`` type) and the loader,
    environment and Java names exactly as CurseForge spells them (``1.20.1``, ``Fabric``,
    ``NeoForge``, ``Client``, ``Java 21``). A name listed twice with different ids is ambiguous and
    raises; a caller resolving a name the mapping lacks must refuse it."""

    _require_origin(transport, UPLOAD_ORIGIN)
    types = _rows(transport.get_json("/api/game/version-types"), "game version types",
                  limits.MAX_CURSEFORGE_VERSION_TYPES, {"id": int, "slug": str})
    versions = _rows(transport.get_json("/api/game/versions"), "game versions", limits.MAX_CURSEFORGE_GAME_VERSIONS,
                     {"id": int, "gameVersionTypeID": int, "name": str})
    named: set[int] = set()
    seen_types: set[int] = set()
    for row in types:
        if row["id"] in seen_types:
            raise MarketplaceError(f"CurseForge lists game version type {row['id']} twice")
        seen_types.add(row["id"])
        if row["slug"].startswith("minecraft-") or row["slug"] in NAMED_VERSION_TYPES:
            named.add(row["id"])
    mapping: dict[str, int] = {}
    for row in versions:
        if row["gameVersionTypeID"] not in named:
            continue
        name, identifier = row["name"], row["id"]
        if identifier <= 0 or not name or len(name) > 64:
            raise MarketplaceError("CurseForge lists a malformed game version")
        if mapping.get(name, identifier) != identifier:
            raise MarketplaceError(f"CurseForge game version name {name[:64]!r} names two ids")
        mapping[name] = identifier
    return mapping


@dataclass(frozen=True)
class CurseForgeFile:
    """The metadata of one CurseForge upload (one lane)."""

    project_id: int
    display_name: str
    changelog: str
    release_type: str
    game_version_ids: tuple[int, ...]
    relations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require_positive(self.project_id, "CurseForge project id")
        require_text(self.display_name, "CurseForge display name", limits.MAX_CURSEFORGE_DISPLAY_NAME_CHARS)
        require_text(self.changelog, "CurseForge changelog", limits.MAX_MARKETPLACE_CHANGELOG_CHARS, allow_empty=True,
                     single_line=False)
        if self.release_type not in RELEASE_TYPES:
            raise MarketplaceError(f"CurseForge release_type must be one of {RELEASE_TYPES}", reason="usage")
        ids = self.game_version_ids
        if not isinstance(ids, tuple) or not 0 < len(ids) <= limits.MAX_MARKETPLACE_GAME_VERSIONS:
            raise MarketplaceError("CurseForge game_version_ids must be a non-empty bounded tuple", reason="usage")
        for identifier in ids:
            require_positive(identifier, "CurseForge game version id")
        if len(set(ids)) != len(ids):
            raise MarketplaceError("CurseForge game_version_ids repeat an id", reason="usage")
        if not isinstance(self.relations, tuple) or len(self.relations) > limits.MAX_MARKETPLACE_DEPENDENCIES:
            raise MarketplaceError("CurseForge relations must be a bounded tuple", reason="usage")
        slugs: set[str] = set()
        for relation in self.relations:
            if (not isinstance(relation, tuple) or len(relation) != 2 or not isinstance(relation[0], str)
                    or _SLUG.fullmatch(relation[0]) is None or relation[1] not in RELATION_TYPES):
                raise MarketplaceError(f"CurseForge relation {relation!r} is not (slug, type)"[:200], reason="usage")
            if relation[0] in slugs:
                raise MarketplaceError(f"CurseForge relation {relation[0]} is declared twice", reason="usage")
            slugs.add(relation[0])


def upload(transport: HttpsTransport, file: CurseForgeFile, path: Path, filename: str, *,
           sha256: str | None = None) -> int:
    """Upload ``path`` as ``filename`` and return the new file id (one never-replayed request).

    ``sha256`` (the plan's digest) must match the file before anything is sent. The answer carries
    only the id, so the bytes are proven later by :func:`reconcile`; an unreadable success answer
    raises :class:`~mod_base.marketplace.transport.MarketplaceOutcomeUnknown`."""

    _require_origin(transport, UPLOAD_ORIGIN)
    if not isinstance(file, CurseForgeFile):
        raise MarketplaceError("upload needs a CurseForgeFile", reason="usage")
    require_filename(filename)
    digests = file_digests(path, sha256=sha256)
    metadata: dict[str, Any] = {
        "changelog": file.changelog,
        "changelogType": "markdown",
        "displayName": file.display_name,
        "gameVersions": list(file.game_version_ids),
        "releaseType": file.release_type,
    }
    if file.relations:
        metadata["relations"] = {"projects": [{"slug": slug, "type": kind} for slug, kind in file.relations]}
    route = f"/api/projects/{file.project_id}/upload-file"
    answer = transport.post_multipart(route, [("metadata", canonical_json(metadata).decode("utf-8"))],
                                      [("file", filename, path)], replay=False,
                                      expected_sha256={"file": digests.sha256})
    identifier = answer.get("id") if isinstance(answer, dict) else None
    if isinstance(identifier, bool) or not isinstance(identifier, int) or identifier <= 0:
        raise MarketplaceOutcomeUnknown(f"CurseForge accepted POST {route} with an answer naming no file id",
                                        status=200, method="POST", path=route)
    return identifier


def listing(transport: HttpsTransport, project_id: int) -> list[dict[str, Any]]:
    """Every file of ``project_id`` from the public listing (``pageIndex`` is a page ordinal, not
    an item offset; ``pageSize=50``). The walk ends on an empty page or once ``totalCount`` rows
    were read, and is bounded by ``limits.MAX_CURSEFORGE_PAGES`` requests. A ``totalCount`` beyond
    ``limits.MAX_CURSEFORGE_FILES`` or a malformed row raises; a listing that changes while it is
    read (its ``totalCount`` moves, a row id repeats) raises
    :class:`~mod_base.marketplace.transport.MarketplaceUnavailable`, to be read again later."""

    _require_origin(transport, LISTING_ORIGIN)
    require_positive(project_id, "CurseForge project id")
    files: list[dict[str, Any]] = []
    ids: set[int] = set()
    total: int | None = None
    route = f"/api/v1/mods/{project_id}/files"
    for page in range(limits.MAX_CURSEFORGE_PAGES):
        answer = transport.get_json(route, {"pageIndex": page, "pageSize": limits.CURSEFORGE_PAGE_SIZE})
        pagination = answer.get("pagination") if isinstance(answer, dict) else None
        count = pagination.get("totalCount") if isinstance(pagination, dict) else None
        if not isinstance(answer, dict) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise MarketplaceError("CurseForge listing has no data and totalCount")
        if count > limits.MAX_CURSEFORGE_FILES:
            raise MarketplaceError(f"CurseForge listing reports {count} files, beyond the bound of "
                                   f"{limits.MAX_CURSEFORGE_FILES}")
        if total is not None and count != total:
            raise MarketplaceUnavailable("CurseForge listing changed while it was read (totalCount moved)")
        total = count
        # The envelope echoes the requested page size rather than the served one, so progress is
        # driven by the rows actually returned.
        rows = _rows(answer.get("data"), "listing page", limits.CURSEFORGE_PAGE_SIZE,
                     {"id": int, "projectId": int, "fileName": str, "fileLength": int, "status": int})
        for row in rows:
            if row["id"] in ids:
                raise MarketplaceUnavailable("CurseForge listing changed while it was read (a file id repeats)")
            ids.add(row["id"])
        files.extend(rows)
        if not rows or len(files) >= total:
            return files
    raise MarketplaceError(f"CurseForge file listing did not end within {limits.MAX_CURSEFORGE_PAGES} pages")


def reconcile(files: list[dict[str, Any]], *, filename: str, size: int, sha1: str, sha256: str, project_id: int,
              fetch_bytes: Callable[[int, str, int], bytes]) -> Reconciliation:
    """Decide one lane against CurseForge (Quick Skin ``classify_curseforge``).

    ``files`` is :func:`listing` of ``project_id``. Rows of this project named ``filename`` or
    :func:`stored_name` of it are the candidates: none publishes; several (both names listed at
    once, or a duplicate) raise; a declared size other than ``size`` raises; a status other than
    :data:`APPROVED_STATUS` raises :class:`~mod_base.marketplace.transport.PublicationPending`.
    Otherwise ``fetch_bytes(file id, stored name, size)`` (:func:`download`) must return exactly
    the plan's bytes (size, SHA-1 and SHA-256), or the collision raises."""

    require_filename(filename)
    require_positive(size, "expected size", limits.MAX_MARKETPLACE_FILE_BYTES)
    require_positive(project_id, "CurseForge project id")
    expected_sha256 = require_sha256(sha256)
    if not isinstance(sha1, str) or _SHA1.fullmatch(sha1.lower()) is None:
        raise MarketplaceError("expected SHA-1 is not a hex digest", reason="usage")
    expected_sha1 = sha1.lower()
    if not isinstance(files, list):
        raise MarketplaceError("CurseForge files must be a list", reason="usage")
    names = {filename, stored_name(filename)}
    named = [row for row in files
             if isinstance(row, dict) and row.get("fileName") in names and row.get("projectId") == project_id]
    if not named:
        return Reconciliation(True, None)
    if len(named) != 1:
        raise MarketplaceError(f"CurseForge lists more than one file named {filename} (or its stored name)")
    remote = named[0]
    if remote.get("fileLength") != size:
        raise MarketplaceError(f"CurseForge file {filename} exists with different bytes (another size)")
    # Before publishing this is a settling upload that must never be republished over; while
    # verifying our own upload it means CurseForge has not finished approving it, so poll.
    if remote.get("status") != APPROVED_STATUS:
        raise PublicationPending(f"CurseForge file {filename} exists but is not approved "
                                 f"(status {str(remote.get('status'))[:20]}); reconcile again once it settles")
    file_id = remote.get("id")
    if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id <= 0:
        raise MarketplaceError("CurseForge file record has no usable id")
    # The CDN serves the file only under the name CurseForge stored it under.
    payload = fetch_bytes(file_id, remote["fileName"], size)
    if (not isinstance(payload, bytes) or len(payload) != size or hashlib.sha1(payload).hexdigest() != expected_sha1
            or hashlib.sha256(payload).hexdigest() != expected_sha256):
        raise MarketplaceError(f"CurseForge file {filename} exists with different bytes")
    return Reconciliation(False, str(file_id))
