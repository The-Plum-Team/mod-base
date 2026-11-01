"""GitHub Releases: tags, draft releases, assets and their reconciliation by content.

Two origins carry the token (``Authorization: Bearer``): :data:`API_ORIGIN` for releases and refs,
:data:`UPLOADS_ORIGIN` for asset uploads. An asset download (:func:`asset_bytes`) is answered with
a redirect to a storage host; it is followed exactly once, to an ``https`` URL, as a fresh request
without any credential (the exception ``mod_base.github.api`` makes for artifact downloads).

Requests that GitHub would act on twice are never replayed after an answer that may hide their
acceptance: creating a draft (GitHub allows several drafts with one tag) and uploading an asset
(a lost upload can leave a half-made asset holding the name). Creating a tag is replayed: a
second attempt that finds the ref is reconciled by its commit.

Asset names: GitHub stores an upload whose name holds spaces with every space turned into a period
(``Quick Skin - Fabric.jar`` becomes ``Quick.Skin.-.Fabric.jar``) and strips leading and trailing
periods. :func:`asset_name` applies exactly that and refuses any name for which GitHub might do
more (characters outside ``[A-Za-z0-9 _.-]``, or a result with ``..``), so the mapping from local
file names to remote asset names is exact, and :func:`bind_assets` refuses names that collide once
normalized.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from mod_base.marketplace import limits
from mod_base.marketplace.transport import (
    USER_AGENT,
    HttpsTransport,
    MarketplaceError,
    MarketplaceNotFound,
    MarketplaceOutcomeUnknown,
    MarketplaceUnavailable,
    content_type_for,
    file_digests,
    require_filename,
    require_match,
    require_positive,
    require_sha256,
    require_text,
)
from mod_base.model import grammar

__all__ = ["API_ORIGIN", "UPLOADS_ORIGIN", "API_VERSION", "headers", "asset_name", "draft_releases", "find_release",
           "get_release", "create_ref", "create_draft", "update_body", "release_assets", "bind_assets",
           "upload_asset", "asset_bytes", "publish"]

API_ORIGIN = "https://api.github.com"
UPLOADS_ORIGIN = "https://uploads.github.com"
API_VERSION = "2022-11-28"
#: A release tag: one ref component of safe characters, never ``..`` or a ``.lock`` suffix.
TAG = re.compile(r"^(?!.*\.\.)(?!.*\.lock$)[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_LOCAL_ASSET = re.compile(r"^[A-Za-z0-9 _.-]+$")
_ASSET = re.compile(r"^[A-Za-z0-9_.-]+$")


def headers(token: str) -> dict[str, str]:
    """The construction headers of an API or uploads transport."""

    if not isinstance(token, str) or not token:
        raise MarketplaceError("GitHub token is malformed", reason="environment")
    return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}", "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION}


def _require_origin(transport: Any, origin: str) -> HttpsTransport:
    if not isinstance(transport, HttpsTransport) or transport.origin != origin:
        raise MarketplaceError(f"this GitHub request needs a transport of {origin}", reason="usage")
    return transport


def _repo(repo: Any) -> str:
    return require_match(grammar.REPOSITORY, repo, "repository")


def _tag(tag: Any) -> str:
    return require_match(TAG, tag, "release tag")


def asset_name(filename: str) -> str:
    """The name GitHub stores an upload of ``filename`` under (module docstring): spaces become
    periods, leading and trailing periods go. Names whose normalization is not known exactly raise."""

    require_filename(filename)
    if _LOCAL_ASSET.fullmatch(filename) is None:
        raise MarketplaceError(f"unsupported GitHub release asset name: {filename!r}"[:200], reason="usage")
    name = filename.replace(" ", ".").strip(".")
    if not name or _ASSET.fullmatch(name) is None or ".." in name:
        raise MarketplaceError(f"unsupported GitHub release asset name: {filename!r}"[:200], reason="usage")
    return name


def _release(value: Any, label: str) -> dict[str, Any]:
    if (not isinstance(value, dict) or isinstance(value.get("id"), bool) or not isinstance(value.get("id"), int)
            or value["id"] <= 0 or not isinstance(value.get("tag_name"), str)
            or not isinstance(value.get("draft"), bool)):
        raise MarketplaceError(f"{label} is not a GitHub release")
    return value


def _paginate(transport: HttpsTransport, path: str, maximum: int) -> list[dict[str, Any]]:
    """Every row of an array listing (``per_page=100``, ``page=1..``). A short page ends it; more
    than ``maximum`` rows raise; a row id seen twice means the listing moved while it was read."""

    rows: list[dict[str, Any]] = []
    ids: set[int] = set()
    for page in range(1, maximum // limits.GITHUB_PER_PAGE + 2):
        batch = transport.get_json(path, {"page": page, "per_page": limits.GITHUB_PER_PAGE})
        if not isinstance(batch, list) or len(batch) > limits.GITHUB_PER_PAGE or any(
                not isinstance(row, dict) for row in batch):
            raise MarketplaceError(f"GitHub listing {path} has a malformed page")
        for row in batch:
            identity = row.get("id")
            if isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0:
                raise MarketplaceError(f"GitHub listing {path} holds a row without an id")
            if identity in ids:
                raise MarketplaceUnavailable(f"GitHub listing {path} changed while it was read (row {identity} repeats)")
            ids.add(identity)
        rows.extend(batch)
        if len(rows) > maximum:
            raise MarketplaceError(f"GitHub listing {path} exceeds its bound of {maximum} rows")
        if len(batch) < limits.GITHUB_PER_PAGE:
            return rows
    raise MarketplaceError(f"GitHub listing {path} did not end within its page bound")


def _releases(transport: HttpsTransport, repo: str) -> list[dict[str, Any]]:
    _require_origin(transport, API_ORIGIN)
    rows = _paginate(transport, f"/repos/{_repo(repo)}/releases", limits.MAX_GITHUB_RELEASES)
    return [_release(row, "a listed release") for row in rows]


def draft_releases(transport: HttpsTransport, repo: str) -> list[dict[str, Any]]:
    """Every draft release of ``repo``. GitHub's tag lookup omits drafts, so this lists every
    release (paginated, at most ``limits.MAX_GITHUB_RELEASES``) with a token that sees drafts."""

    return [row for row in _releases(transport, repo) if row["draft"]]


def find_release(transport: HttpsTransport, repo: str, tag: str) -> dict[str, Any] | None:
    """The one release (draft or published) whose ``tag_name`` is ``tag``, or None. Several
    releases with one tag (GitHub allows several drafts) are ambiguous and raise."""

    _tag(tag)
    matches = [row for row in _releases(transport, repo) if row["tag_name"] == tag]
    if len(matches) > 1:
        raise MarketplaceError(f"{len(matches)} GitHub releases carry the tag {tag}")
    return matches[0] if matches else None


def get_release(transport: HttpsTransport, repo: str, release_id: int) -> dict[str, Any]:
    """``GET /repos/{repo}/releases/{id}``: a fresh read of one release (a confirming read)."""

    _require_origin(transport, API_ORIGIN)
    require_positive(release_id, "release id")
    release = _release(transport.get_json(f"/repos/{_repo(repo)}/releases/{release_id}"), "the release answer")
    if release["id"] != release_id:
        raise MarketplaceError(f"GitHub answered another release for {release_id}")
    return release


def create_ref(transport: HttpsTransport, repo: str, tag: str, sha: str) -> None:
    """Create the lightweight tag ``refs/tags/{tag}`` at commit ``sha``. A tag already at exactly
    that commit is accepted (a resumed release); a tag elsewhere, or an annotated tag, raises."""

    _require_origin(transport, API_ORIGIN)
    repo, tag = _repo(repo), _tag(tag)
    require_match(grammar.SHA1, sha, "tag commit")
    try:
        answer = transport.post_json(f"/repos/{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha}, replay=True)
    except MarketplaceError as exc:
        if exc.status != 422:
            raise
        try:
            existing = transport.get_json(f"/repos/{repo}/git/ref/tags/{tag}")
        except MarketplaceNotFound:
            raise MarketplaceError(f"GitHub refused the tag {tag} and holds no such ref: {exc}"[:400],
                                   status=422, method="POST", path=exc.path) from exc
        target = existing.get("object") if isinstance(existing, dict) else None
        if not isinstance(target, dict) or existing.get("ref") != f"refs/tags/{tag}":
            raise MarketplaceError(f"GitHub refused the tag {tag} and holds no such ref") from exc
        if target.get("type") != "commit" or target.get("sha") != sha:
            raise MarketplaceError(f"the tag {tag} already exists at another commit or as an annotated tag") from exc
        return
    target = answer.get("object") if isinstance(answer, dict) else None
    if not isinstance(target, dict) or target.get("sha") != sha or answer.get("ref") != f"refs/tags/{tag}":
        raise MarketplaceError(f"GitHub created the tag {tag} somewhere unexpected")


def _body(body: Any) -> str:
    return require_text(body, "release body", limits.MAX_GITHUB_RELEASE_BODY_CHARS, allow_empty=True,
                        single_line=False)


def create_draft(transport: HttpsTransport, repo: str, *, tag: str, name: str, body: str,
                 target_commitish: str) -> dict[str, Any]:
    """Create the draft release of ``tag`` (never replayed: GitHub would create a second draft).
    The tag should already exist (:func:`create_ref`); ``target_commitish`` names its commit."""

    _require_origin(transport, API_ORIGIN)
    repo, tag = _repo(repo), _tag(tag)
    require_text(name, "release name", limits.MAX_GITHUB_RELEASE_NAME_CHARS)
    require_match(grammar.SHA1, target_commitish, "release target commit")
    route = f"/repos/{repo}/releases"
    answer = transport.post_json(route, {"body": _body(body), "draft": True, "generate_release_notes": False,
                                         "name": name, "prerelease": False, "tag_name": tag,
                                         "target_commitish": target_commitish}, replay=False)
    try:
        release = _release(answer, "the created release")
    except MarketplaceError:
        raise MarketplaceOutcomeUnknown(f"GitHub accepted POST {route} with an answer naming no release",
                                        status=201, method="POST", path=route) from None
    if release["tag_name"] != tag or release["draft"] is not True:
        raise MarketplaceError(f"GitHub created release {release['id']} with another tag or not as a draft")
    return release


def update_body(transport: HttpsTransport, repo: str, release_id: int, body: str, tag: str) -> dict[str, Any]:
    """Replace a release's body. The edit always restates ``tag_name``: a draft edited without it
    loses its tag and becomes ``untagged-...`` (Quick Skin's first ledger recovery)."""

    _require_origin(transport, API_ORIGIN)
    repo, tag = _repo(repo), _tag(tag)
    require_positive(release_id, "release id")
    release = _release(transport.patch_json(f"/repos/{repo}/releases/{release_id}",
                                            {"body": _body(body), "tag_name": tag}), "the edited release")
    if release["id"] != release_id or release["tag_name"] != tag or (release.get("body") or "") != body:
        raise MarketplaceError(f"GitHub did not confirm the body of release {release_id}")
    return release


def release_assets(transport: HttpsTransport, repo: str, release_id: int) -> list[dict[str, Any]]:
    """The assets of one release (``/releases/{id}/assets``, paginated, at most
    ``limits.MAX_GITHUB_RELEASE_ASSETS``)."""

    _require_origin(transport, API_ORIGIN)
    require_positive(release_id, "release id")
    return _paginate(transport, f"/repos/{_repo(repo)}/releases/{release_id}/assets", limits.MAX_GITHUB_RELEASE_ASSETS)


def bind_assets(transport: HttpsTransport, repo: str, release: dict[str, Any],
                local: dict[str, tuple[int, str]]) -> set[str]:
    """The local file names (``{filename: (size, sha256)}``) whose asset is already on ``release``
    with the same bytes.

    Each local name maps to :func:`asset_name`; two that collide raise. The release's assets are
    read fresh: a duplicate name, an asset no local file explains (the draft must hold only the
    plan's files), or one GitHub has not finished storing raises. A matching asset must have the
    same size and, when GitHub reports one, the same ``digest``; it is then downloaded
    (:func:`asset_bytes`, bounded to its size) and its SHA-256 must equal the plan's, else the
    collision raises. A missing asset is simply absent from the result."""

    release = _release(release, "the release to bind")
    if not isinstance(local, dict) or len(local) > limits.MAX_GITHUB_RELEASE_ASSETS:
        raise MarketplaceError("local assets must be a bounded mapping", reason="usage")
    expected: dict[str, tuple[str, int, str]] = {}
    for filename, value in local.items():
        name = asset_name(filename)
        if name in expected:
            raise MarketplaceError(f"GitHub asset names collide after normalization: {expected[name][0]!r} and "
                                   f"{filename!r} are both {name!r}"[:300], reason="usage")
        if not isinstance(value, tuple) or len(value) != 2:
            raise MarketplaceError("a local asset is (size, sha256)", reason="usage")
        size = require_positive(value[0], f"size of {filename}", limits.MAX_MARKETPLACE_FILE_BYTES)
        expected[name] = (filename, size, require_sha256(value[1], f"SHA-256 of {filename}"))
    remote = release_assets(transport, repo, release["id"])
    by_name: dict[str, dict[str, Any]] = {}
    for asset in remote:
        name = asset.get("name")
        if not isinstance(name, str) or name in by_name:
            raise MarketplaceError("the GitHub release holds a duplicate or unnamed asset")
        if name not in expected:
            raise MarketplaceError(f"the GitHub release holds an unexpected asset: {name!r}"[:200])
        by_name[name] = asset
    present: set[str] = set()
    for name, asset in sorted(by_name.items()):
        filename, size, sha256 = expected[name]
        if asset.get("state") != "uploaded":
            raise MarketplaceError(f"GitHub asset {name} is in state {str(asset.get('state'))[:20]!r}, not uploaded")
        if asset.get("size") != size:
            raise MarketplaceError(f"GitHub asset {name} exists with different bytes (another size)")
        digest = asset.get("digest")
        if digest is not None and digest != f"sha256:{sha256}":
            raise MarketplaceError(f"GitHub asset {name} exists with different bytes (another digest)")
        payload = asset_bytes(transport, repo, require_positive(asset.get("id"), f"id of asset {name}"), limit=size)
        if hashlib.sha256(payload).hexdigest() != sha256:
            raise MarketplaceError(f"GitHub asset {name} exists with different bytes")
        present.add(filename)
    return present


def upload_asset(transport_uploads: HttpsTransport, repo: str, release_id: int, path: Path, filename: str, *,
                 sha256: str | None = None) -> dict[str, Any]:
    """Upload ``path`` as the asset ``filename`` of a release (one never-replayed request to
    :data:`UPLOADS_ORIGIN`). ``sha256`` (the plan's digest) must match before anything is sent;
    the answer must name the normalized asset, its size and, when reported, its digest."""

    _require_origin(transport_uploads, UPLOADS_ORIGIN)
    repo = _repo(repo)
    require_positive(release_id, "release id")
    name = asset_name(filename)
    digests = file_digests(path, sha256=sha256)
    route = f"/repos/{repo}/releases/{release_id}/assets"
    answer = transport_uploads.post_bytes(route, path, content_type_for(filename), {"name": filename}, replay=False,
                                          expected_sha256=digests.sha256)
    if not isinstance(answer, dict) or isinstance(answer.get("id"), bool) or not isinstance(answer.get("id"), int):
        raise MarketplaceOutcomeUnknown(f"GitHub accepted POST {route} with an answer naming no asset", status=201,
                                        method="POST", path=route)
    digest = answer.get("digest")
    if (answer.get("name") != name or answer.get("size") != digests.size or answer.get("state") != "uploaded"
            or (digest is not None and digest != f"sha256:{digests.sha256}")):
        raise MarketplaceError(f"GitHub stored asset {answer['id']} with another name, size, state or digest than "
                               f"{name}")
    return answer


def asset_bytes(transport: HttpsTransport, repo: str, asset_id: int, *, limit: int) -> bytes:
    """Exactly ``limit`` bytes of one asset (``Accept: application/octet-stream``): GitHub's answer
    is a redirect to its storage, followed once without credentials."""

    _require_origin(transport, API_ORIGIN)
    require_positive(asset_id, "asset id")
    return transport.download_redirected(f"/repos/{_repo(repo)}/releases/assets/{asset_id}", limit=limit,
                                         accept="application/octet-stream")


def publish(transport: HttpsTransport, repo: str, release_id: int, tag: str, *, make_latest: bool) -> dict[str, Any]:
    """Publish a draft (``draft: false``, restating ``tag_name``; ``make_latest`` "true" or "false")."""

    _require_origin(transport, API_ORIGIN)
    repo, tag = _repo(repo), _tag(tag)
    require_positive(release_id, "release id")
    if not isinstance(make_latest, bool):
        raise MarketplaceError("make_latest must be a boolean", reason="usage")
    release = _release(transport.patch_json(f"/repos/{repo}/releases/{release_id}",
                                            {"draft": False, "make_latest": "true" if make_latest else "false",
                                             "tag_name": tag}), "the published release")
    if release["id"] != release_id or release["tag_name"] != tag or release["draft"] is not False:
        raise MarketplaceError(f"GitHub did not confirm the publication of release {release_id}")
    return release
