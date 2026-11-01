"""In-memory Modrinth, CurseForge and GitHub Releases for the tests (never a network).

:class:`FakeNetwork` is an ``opener`` for :class:`mod_base.marketplace.transport.HttpsTransport`:
it answers ``open(request, timeout)`` the way ``urllib`` does (a response object for a 2xx,
``urllib.error.HTTPError`` for anything else, including every 3xx, since the real opener refuses
redirects) and records every request as a :class:`RecordedRequest`: method, origin, path, query,
headers with every registered secret replaced by ``<secret:label>``, the labels of the secrets it
carried, and a body summary. Tests assert request counts, that each token reaches only its own
origins, that a non-replayable POST is sent once after a 5xx, that redirects are not followed and
that downloads stop at their bound (:attr:`FakeNetwork.delivered` counts body bytes served).

Faults: :meth:`FakeNetwork.fail` answers the next matching requests with a status or an exception,
optionally after the service has acted on the request (``after=True``: an accepted upload whose
answer was lost); :meth:`FakeNetwork.tamper` changes a served body (cut short, padded lazily to any
size, another declared length). An unregistered origin fails like an unresolvable host.

Services (each registers its origins and its token on the network):

* :class:`FakeModrinth`: ``/v2/project/{id|slug}``, ``/v2/project/{id}/version``,
  ``/v2/version_file/{sha512}``, ``POST /v2/version`` (multipart ``data`` + file parts);
* :class:`FakeCurseForge`: the upload API (game versions, ``upload-file``), the public listing
  (pages of at most 50, ``totalCount``) and the CDN, which serves an approved file only under its
  stored name at the unpadded shard path and answers 403 otherwise. ``rename=True`` stores uploads
  the way CurseForge has since late October 2026 (runs of spaces and hyphens holding a space become
  one hyphen);
* :class:`FakeGitHubReleases`: releases (drafts included), refs, assets on the uploads origin with
  GitHub's space-to-period rename, asset downloads redirected to a storage origin that, like S3,
  refuses a request carrying ``Authorization``, and a draft that loses its tag when a PATCH omits
  ``tag_name``.
"""

from __future__ import annotations

import copy
import email.message
import hashlib
import http.client
import io
import itertools
import json
import re
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

APPROVED = 4
_JSON = {"Content-Type": "application/json"}


@dataclass(frozen=True)
class RecordedRequest:
    """One request as the fake network saw it (``headers`` names are lower-case)."""

    method: str
    origin: str
    path: str
    query: tuple[tuple[str, str], ...]
    headers: tuple[tuple[str, str], ...]
    credentials: frozenset[str]
    body_summary: str
    body: bytes = field(repr=False)

    def header(self, name: str) -> str | None:
        return dict(self.headers).get(name.lower())

    @property
    def url(self) -> str:
        return self.origin + self.path


@dataclass
class FakeHttp:
    """A request handed to a service: ``path`` is percent-decoded per segment in ``segments``."""

    method: str
    origin: str
    path: str
    segments: tuple[str, ...]
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes


@dataclass
class Reply:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


def json_reply(status: int, value: Any, headers: dict[str, str] | None = None) -> Reply:
    return Reply(status, json.dumps(value, sort_keys=True).encode("utf-8"), {**_JSON, **(headers or {})})


@dataclass
class _Fault:
    method: str
    prefix: str
    times: int
    status: int = 0
    exception: BaseException | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    after: bool = False
    truncate_to: int | None = None
    extra_bytes: int = 0
    content_length: int | None = None
    omit_length: bool = False
    tamper: bool = False


class _Response:
    """What ``urllib`` returns for a 2xx: ``status``, ``headers`` and ``read(n)``."""

    def __init__(self, network: FakeNetwork, status: int, headers: email.message.Message, body: bytes, extra: int,
                 truncate_to: int | None) -> None:
        self.status = status
        self.headers = headers
        self._network = network
        self._body = io.BytesIO(body if truncate_to is None else body[:truncate_to])
        self._extra = extra
        self._truncated = truncate_to is not None and truncate_to < len(body) + extra

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        chunk = self._body.read(size if size >= 0 else -1)
        if not chunk and self._extra and not self._truncated:
            count = self._extra if size < 0 else min(size, self._extra)
            self._extra -= count
            chunk = b"\0" * count
        if not chunk and self._truncated:
            self._truncated = False
            raise http.client.IncompleteRead(b"")
        self._network.delivered += len(chunk)
        return chunk


def _message(headers: dict[str, str]) -> email.message.Message:
    message = email.message.Message()
    for name, value in headers.items():
        message[name] = value
    return message


class FakeNetwork:
    """The opener the fakes serve through (see the module docstring)."""

    def __init__(self) -> None:
        self._services: dict[str, Callable[[FakeHttp], Reply]] = {}
        self._secrets: dict[str, str] = {}
        self._faults: list[_Fault] = []
        self.requests: list[RecordedRequest] = []
        self.timeouts: list[float] = []
        #: Body bytes handed to the client by every response so far.
        self.delivered = 0

    def add_service(self, origin: str, handler: Callable[[FakeHttp], Reply]) -> None:
        if origin in self._services:
            raise ValueError(f"{origin} is already served")
        self._services[origin] = handler

    def add_secret(self, label: str, value: str) -> None:
        self._secrets[value] = label

    def fail(self, method: str, url: str, *, status: int = 0, exception: BaseException | None = None,
             headers: dict[str, str] | None = None, body: bytes = b"", after: bool = False, times: int = 1) -> None:
        """Answer the next ``times`` requests whose ``origin + path`` starts with ``url`` with
        ``status`` (or raise ``exception``); with ``after`` the service acts on the request first."""

        if (status == 0) == (exception is None):
            raise ValueError("a fault is a status or an exception")
        self._faults.append(_Fault(method, url, times, status=status, exception=exception, headers=dict(headers or {}),
                                   body=body, after=after))

    def tamper(self, method: str, url: str, *, truncate_to: int | None = None, extra_bytes: int = 0,
               content_length: int | None = None, omit_length: bool = False, times: int = 1) -> None:
        """Change the next ``times`` successful bodies: cut them to ``truncate_to`` bytes (then the
        read raises ``IncompleteRead``), append ``extra_bytes`` zero bytes (served lazily), declare
        ``content_length`` or no length at all."""

        self._faults.append(_Fault(method, url, times, truncate_to=truncate_to, extra_bytes=extra_bytes,
                                   content_length=content_length, omit_length=omit_length, tamper=True))

    def _take(self, method: str, url: str, *, tamper: bool) -> _Fault | None:
        for fault in self._faults:
            if fault.tamper == tamper and fault.method == method and url.startswith(fault.prefix) and fault.times:
                fault.times -= 1
                return fault
        return None

    def requests_to(self, origin: str | None = None, *, method: str | None = None) -> list[RecordedRequest]:
        return [request for request in self.requests
                if (origin is None or request.origin == origin) and (method is None or request.method == method)]

    def _record(self, method: str, origin: str, path: str, query: list[tuple[str, str]], headers: dict[str, str],
                body: bytes) -> None:
        credentials: set[str] = set()
        shown: list[tuple[str, str]] = []
        for name, value in sorted(headers.items()):
            for secret, label in self._secrets.items():
                if secret in value:
                    credentials.add(label)
                    value = value.replace(secret, f"<secret:{label}>")
            shown.append((name, value))
        for secret, label in self._secrets.items():
            if secret.encode("utf-8") in body or any(secret in value for _, value in query):
                credentials.add(label)
        content_type = headers.get("content-type", "")
        if not body:
            summary = ""
        elif content_type.startswith("multipart/form-data"):
            summary = "multipart:" + ",".join(name for name, _, _ in parse_multipart(content_type, body))
        else:
            summary = f"{content_type or 'bytes'}:{len(body)}:{hashlib.sha256(body).hexdigest()[:16]}"
        self.requests.append(RecordedRequest(method, origin, path, tuple(query), tuple(shown), frozenset(credentials),
                                             summary, body))

    def open(self, request: Any, timeout: float | None = None) -> _Response:
        url = request.full_url
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https":
            raise AssertionError(f"the transport sent a non-https request: {url}")
        origin = f"https://{parsed.netloc}"
        method = request.get_method()
        headers = {name.lower(): value for name, value in request.header_items()}
        body = request.data if isinstance(request.data, bytes) else b""
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        self._record(method, origin, parsed.path, query, headers, body)
        self.timeouts.append(timeout if timeout is not None else -1.0)
        handler = self._services.get(origin)
        if handler is None:
            raise urllib.error.URLError(OSError(f"cannot resolve {parsed.hostname}"))
        target = origin + parsed.path
        fault = self._take(method, target, tamper=False)
        if fault is not None and not fault.after:
            if fault.exception is not None:
                raise fault.exception
            return self._answer(url, Reply(fault.status, fault.body, fault.headers), None)
        segments = tuple(urllib.parse.unquote(segment) for segment in parsed.path.split("/")[1:])
        reply = handler(FakeHttp(method, origin, parsed.path, segments, dict(query), headers, body))
        if fault is not None:
            if fault.exception is not None:
                raise fault.exception
            reply = Reply(fault.status, fault.body, fault.headers)
        return self._answer(url, reply, self._take(method, target, tamper=True) if reply.status < 300 else None)

    def _answer(self, url: str, reply: Reply, tamper: _Fault | None) -> _Response:
        headers = dict(reply.headers)
        extra = tamper.extra_bytes if tamper is not None else 0
        if tamper is not None and tamper.omit_length:
            pass
        elif tamper is not None and tamper.content_length is not None:
            headers["Content-Length"] = str(tamper.content_length)
        else:
            headers["Content-Length"] = str(len(reply.body) + extra)
        if reply.status >= 300:
            raise urllib.error.HTTPError(url, reply.status, http.client.responses.get(reply.status, "error"),
                                         _message(headers), io.BytesIO(reply.body))
        return _Response(self, reply.status, _message(headers), reply.body, extra,
                         tamper.truncate_to if tamper is not None else None)


def parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str | None, bytes]]:
    """``[(part name, file name or None, content)]`` of a ``multipart/form-data`` body (strict
    enough for the fakes: every part has a ``Content-Disposition`` with a quoted name)."""

    match = re.fullmatch(r"multipart/form-data; boundary=([A-Za-z0-9-]{1,70})", content_type)
    if match is None:
        raise ValueError("not a multipart content type")
    marker = b"--" + match.group(1).encode("ascii")
    if not body.startswith(marker) or not body.endswith(marker + b"--\r\n"):
        raise ValueError("multipart body is not framed by its boundary")
    inner = body[len(marker):-len(marker) - 4]
    if not inner.endswith(b"\r\n"):
        raise ValueError("multipart body does not end its last part")
    parts: list[tuple[str, str | None, bytes]] = []
    for chunk in inner[:-2].split(b"\r\n" + marker):
        if not chunk.startswith(b"\r\n"):
            raise ValueError("multipart part does not start a line")
        head, separator, content = chunk[2:].partition(b"\r\n\r\n")
        if not separator:
            raise ValueError("multipart part has no header block")
        disposition = re.search(rb'Content-Disposition: form-data; name="([^"]+)"(?:; filename="([^"]*)")?', head)
        if disposition is None:
            raise ValueError("multipart part has no disposition")
        filename = disposition.group(2).decode("ascii") if disposition.group(2) is not None else None
        parts.append((disposition.group(1).decode("ascii"), filename, content))
    return parts


def _multipart(request: FakeHttp) -> dict[str, tuple[str | None, bytes]] | None:
    try:
        parts = parse_multipart(request.headers.get("content-type", ""), request.body)
    except ValueError:
        return None
    names = [name for name, _, _ in parts]
    if len(set(names)) != len(names):
        return None
    return {name: (filename, content) for name, filename, content in parts}


def _not_found() -> Reply:
    return json_reply(404, {"message": "Not Found"})


# -- Modrinth -------------------------------------------------------------------------------------

class FakeModrinth:
    ORIGIN = "https://api.modrinth.com"

    def __init__(self, network: FakeNetwork, *, token: str = "mrp_fake_modrinth_token") -> None:
        self.token = token
        self.projects: dict[str, dict[str, Any]] = {}
        self.versions: list[dict[str, Any]] = []
        self._ids = itertools.count(1)
        network.add_service(self.ORIGIN, self.handle)
        network.add_secret("modrinth", token)

    def add_project(self, project_id: str, slug: str) -> None:
        self.projects[project_id] = {"id": project_id, "slug": slug, "title": slug}

    def add_version(self, project_id: str, version_number: str, files: list[tuple[str, bytes]], *,
                    version_id: str | None = None) -> dict[str, Any]:
        version = {
            "changelog": "seeded", "dependencies": [], "featured": False, "game_versions": ["1.20.1"],
            "id": version_id or f"ver{next(self._ids):05d}", "loaders": ["fabric"], "name": version_number,
            "project_id": project_id, "version_number": version_number, "version_type": "release",
            "files": [self._file(name, data, index == 0) for index, (name, data) in enumerate(files)],
        }
        self.versions.append(version)
        return copy.deepcopy(version)

    @staticmethod
    def _file(filename: str, data: bytes, primary: bool) -> dict[str, Any]:
        return {"filename": filename, "primary": primary, "size": len(data),
                "hashes": {"sha1": hashlib.sha1(data).hexdigest(), "sha512": hashlib.sha512(data).hexdigest()},
                "url": f"https://cdn.modrinth.com/data/{urllib.parse.quote(filename)}"}

    def _project(self, key: str) -> dict[str, Any] | None:
        return self.projects.get(key) or next(
            (project for project in self.projects.values() if project["slug"].lower() == key.lower()), None)

    def handle(self, request: FakeHttp) -> Reply:
        given = request.headers.get("authorization")
        if given is not None and given != self.token:
            return json_reply(401, {"error": "unauthorized", "description": f"token {given} is invalid"})
        segments = request.segments
        if request.method == "GET" and len(segments) == 3 and segments[:2] == ("v2", "project"):
            project = self._project(segments[2])
            return json_reply(200, project) if project else _not_found()
        if request.method == "GET" and len(segments) == 4 and segments[:2] == ("v2", "project") and segments[3] == "version":
            project = self._project(segments[2])
            if project is None:
                return _not_found()
            rows = [copy.deepcopy(row) for row in reversed(self.versions) if row["project_id"] == project["id"]]
            if request.query.get("include_changelog") == "false":
                for row in rows:
                    row["changelog"] = None
            return json_reply(200, rows)
        if request.method == "GET" and len(segments) == 3 and segments[:2] == ("v2", "version_file"):
            if request.query.get("algorithm") != "sha512":
                return json_reply(400, {"error": "invalid_input", "description": "algorithm"})
            for version in self.versions:
                if any(file["hashes"]["sha512"] == segments[2] for file in version["files"]):
                    return json_reply(200, version)
            return _not_found()
        if request.method == "POST" and segments == ("v2", "version"):
            if given is None:
                return json_reply(401, {"error": "unauthorized", "description": "no token"})
            return self._create(request)
        return _not_found()

    def _create(self, request: FakeHttp) -> Reply:
        parts = _multipart(request)
        if parts is None or "data" not in parts:
            return json_reply(400, {"error": "invalid_input", "description": "malformed multipart"})
        data = json.loads(parts["data"][1])
        if data.get("project_id") not in self.projects or data.get("file_parts") != [
                name for name in parts if name != "data"] or data.get("primary_file") not in data["file_parts"]:
            return json_reply(400, {"error": "invalid_input", "description": "bad version data"})
        for dependency in data.get("dependencies", []):
            if dependency.get("project_id") not in self.projects:
                return json_reply(400, {"error": "invalid_input", "description": "unknown dependency"})
        files = [(parts[name][0] or name, parts[name][1]) for name in data["file_parts"]]
        self.add_version(data["project_id"], data["version_number"], files)
        stored = self.versions[-1]
        for key in ("changelog", "dependencies", "featured", "game_versions", "loaders", "name", "version_type"):
            stored[key] = data[key]
        return json_reply(200, stored)


# -- CurseForge -----------------------------------------------------------------------------------

def curseforge_rename(filename: str) -> str:
    """CurseForge's stored-name rule, written independently of ``curseforge.stored_name``."""

    return re.sub(r"[ -]* [ -]*", "-", filename)


class FakeCurseForge:
    UPLOAD_ORIGIN = "https://minecraft.curseforge.com"
    LISTING_ORIGIN = "https://www.curseforge.com"
    CDN_ORIGIN = "https://mediafilez.forgecdn.net"

    def __init__(self, network: FakeNetwork, *, token: str = "cf-fake-curseforge-token", rename: bool = True,
                 upload_status: int = APPROVED) -> None:
        self.token = token
        self.rename = rename
        self.upload_status = upload_status
        self.version_types: list[dict[str, Any]] = []
        self.game_versions: list[dict[str, Any]] = []
        self.files: list[dict[str, Any]] = []
        self.data: dict[int, bytes] = {}
        self.metadata: dict[int, dict[str, Any]] = {}
        self._ids = itertools.count(7_214_079)
        network.add_service(self.UPLOAD_ORIGIN, self.handle_upload)
        network.add_service(self.LISTING_ORIGIN, self.handle_listing)
        network.add_service(self.CDN_ORIGIN, self.handle_cdn)
        network.add_secret("curseforge", token)

    def seed_game_versions(self) -> None:
        """A small slice of CurseForge's real listings (ids are made up)."""

        self.version_types += [
            {"id": 73250, "name": "Minecraft 1.20", "slug": "minecraft-1-20"},
            {"id": 77784, "name": "Minecraft 1.21", "slug": "minecraft-1-21"},
            {"id": 68441, "name": "Modloader", "slug": "modloader"},
            {"id": 75208, "name": "Environment", "slug": "environment"},
            {"id": 2, "name": "Java", "slug": "java"},
            {"id": 1, "name": "Addons", "slug": "addons"},
        ]
        self.game_versions += [
            {"id": 9990, "gameVersionTypeID": 73250, "name": "1.20.1", "slug": "1-20-1"},
            {"id": 9991, "gameVersionTypeID": 73250, "name": "1.20-Snapshot", "slug": "1-20-snapshot"},
            {"id": 11779, "gameVersionTypeID": 77784, "name": "1.21.1", "slug": "1-21-1"},
            {"id": 7499, "gameVersionTypeID": 68441, "name": "Fabric", "slug": "fabric"},
            {"id": 7498, "gameVersionTypeID": 68441, "name": "Forge", "slug": "forge"},
            {"id": 10150, "gameVersionTypeID": 68441, "name": "NeoForge", "slug": "neoforge"},
            {"id": 9153, "gameVersionTypeID": 75208, "name": "Client", "slug": "client"},
            {"id": 9150, "gameVersionTypeID": 75208, "name": "Server", "slug": "server"},
            {"id": 11135, "gameVersionTypeID": 2, "name": "Java 21", "slug": "java-21"},
            {"id": 1, "gameVersionTypeID": 1, "name": "1.20.1", "slug": "addon"},
        ]

    def add_file(self, project_id: int, file_name: str, data: bytes, *, status: int = APPROVED,
                 file_id: int | None = None, length: int | None = None) -> dict[str, Any]:
        """Seed a listed file under exactly ``file_name`` (files uploaded before the rename kept it)."""

        identifier = file_id if file_id is not None else next(self._ids)
        row = {"id": identifier, "projectId": project_id, "fileName": file_name,
               "fileLength": len(data) if length is None else length, "status": status, "displayName": file_name}
        self.files.insert(0, row)
        self.data[identifier] = data
        return dict(row)

    def set_status(self, file_id: int, status: int) -> None:
        next(row for row in self.files if row["id"] == file_id)["status"] = status

    def handle_upload(self, request: FakeHttp) -> Reply:
        if request.headers.get("x-api-token") != self.token:
            return json_reply(401, {"errorCode": 401, "errorMessage": "You must provide an API token"})
        segments = request.segments
        if request.method == "GET" and segments == ("api", "game", "version-types"):
            return json_reply(200, self.version_types)
        if request.method == "GET" and segments == ("api", "game", "versions"):
            return json_reply(200, self.game_versions)
        if (request.method == "POST" and len(segments) == 4 and segments[0:2] == ("api", "projects")
                and segments[3] == "upload-file" and segments[2].isdigit()):
            parts = _multipart(request)
            if parts is None or set(parts) != {"metadata", "file"} or not parts["file"][0]:
                return json_reply(400, {"errorCode": 1000, "errorMessage": "malformed upload"})
            metadata = json.loads(parts["metadata"][1])
            known = {row["id"] for row in self.game_versions}
            if not metadata.get("gameVersions") or any(identifier not in known for identifier in metadata["gameVersions"]):
                return json_reply(400, {"errorCode": 1018, "errorMessage": "Invalid game version ID"})
            name = parts["file"][0]
            row = self.add_file(int(segments[2]), curseforge_rename(name) if self.rename else name, parts["file"][1],
                                status=self.upload_status)
            self.metadata[row["id"]] = metadata
            return json_reply(200, {"id": row["id"]})
        return _not_found()

    def handle_listing(self, request: FakeHttp) -> Reply:
        segments = request.segments
        if (request.method != "GET" or len(segments) != 5 or segments[:3] != ("api", "v1", "mods")
                or segments[4] != "files" or not segments[3].isdigit()):
            return _not_found()
        rows = [dict(row) for row in self.files if row["projectId"] == int(segments[3])]
        index = int(request.query.get("pageIndex", "0"))
        size = min(int(request.query.get("pageSize", "50")), 50)
        page = rows[index * size:(index + 1) * size]
        return json_reply(200, {"data": page, "pagination": {
            "index": index, "pageSize": int(request.query.get("pageSize", "50")), "resultCount": len(page),
            "totalCount": len(rows)}})

    def handle_cdn(self, request: FakeHttp) -> Reply:
        segments = request.segments
        forbidden = Reply(403, b"<Error><Code>AccessDenied</Code></Error>", {"Content-Type": "application/xml"})
        if request.method != "GET" or len(segments) != 4 or segments[0] != "files":
            return forbidden
        thousands, remainder, name = segments[1:]
        if not thousands.isdigit() or not remainder.isdigit() or str(int(remainder)) != remainder:
            return forbidden  # a zero-padded remainder is refused like the real CDN does
        identifier = int(thousands) * 1000 + int(remainder)
        row = next((row for row in self.files if row["id"] == identifier), None)
        if row is None or row["fileName"] != name or row["status"] != APPROVED:
            return forbidden
        return Reply(200, self.data[identifier], {"Content-Type": "application/java-archive"})


# -- GitHub Releases ------------------------------------------------------------------------------

def github_asset_rename(filename: str) -> str:
    """GitHub's asset-name rule, written independently of ``github_releases.asset_name``."""

    return filename.replace(" ", ".").strip(".")


class FakeGitHubReleases:
    API_ORIGIN = "https://api.github.com"
    UPLOADS_ORIGIN = "https://uploads.github.com"
    STORAGE_ORIGIN = "https://release-assets.githubusercontent.com"

    def __init__(self, network: FakeNetwork, *, repo: str = "The-Plum-Team/Example-Mod",
                 token: str = "ghs_fake_github_token") -> None:
        self.repo = repo
        self.token = token
        self.refs: dict[str, dict[str, str]] = {}
        self.releases: list[dict[str, Any]] = []
        self.data: dict[int, bytes] = {}
        self.latest: int | None = None
        self._release_ids = itertools.count(1001)
        self._asset_ids = itertools.count(5001)
        self._signatures: dict[int, str] = {}
        network.add_service(self.API_ORIGIN, self.handle_api)
        network.add_service(self.UPLOADS_ORIGIN, self.handle_uploads)
        network.add_service(self.STORAGE_ORIGIN, self.handle_storage)
        network.add_secret("github", token)

    # seeding
    def add_ref(self, tag: str, sha: str, *, annotated: bool = False) -> None:
        self.refs[tag] = {"sha": sha, "type": "tag" if annotated else "commit"}

    def add_release(self, tag: str, *, draft: bool = True, body: str = "", name: str | None = None,
                    target: str = "0" * 40) -> dict[str, Any]:
        release = {"id": next(self._release_ids), "tag_name": tag, "name": name or tag, "body": body, "draft": draft,
                   "prerelease": False, "target_commitish": target, "assets": []}
        self.releases.insert(0, release)
        return copy.deepcopy(release)

    def add_asset(self, release_id: int, name: str, data: bytes, *, state: str = "uploaded",
                  digest: bool = True) -> dict[str, Any]:
        asset = {"id": next(self._asset_ids), "name": name, "size": len(data), "state": state,
                 "content_type": "application/java-archive",
                 "browser_download_url": f"https://github.com/{self.repo}/releases/download/x/{name}"}
        if digest:
            asset["digest"] = "sha256:" + hashlib.sha256(data).hexdigest()
        self.release(release_id)["assets"].append(asset)
        self.data[asset["id"]] = data
        return dict(asset)

    def release(self, release_id: int) -> dict[str, Any]:
        return next(release for release in self.releases if release["id"] == release_id)

    # handlers
    def _authorized(self, request: FakeHttp) -> bool:
        return request.headers.get("authorization") == f"Bearer {self.token}"

    def _page(self, request: FakeHttp, rows: list[dict[str, Any]]) -> Reply:
        per_page = min(int(request.query.get("per_page", "30")), 100)
        page = int(request.query.get("page", "1"))
        return json_reply(200, rows[(page - 1) * per_page:page * per_page])

    def handle_api(self, request: FakeHttp) -> Reply:
        if not self._authorized(request):
            return json_reply(401, {"message": "Bad credentials"})
        prefix = tuple(["repos", *self.repo.split("/")])
        segments = request.segments
        if segments[:3] != prefix:
            return _not_found()
        rest = segments[3:]
        if rest == ("releases",) and request.method == "GET":
            return self._page(request, [copy.deepcopy(release) for release in self.releases])
        if rest == ("releases", "latest") and request.method == "GET":
            release = next((row for row in self.releases if row["id"] == self.latest and not row["draft"]), None)
            return json_reply(200, copy.deepcopy(release)) if release else _not_found()
        if rest == ("releases",) and request.method == "POST":
            data = json.loads(request.body)
            release = self.add_release(data["tag_name"], draft=data.get("draft", False), body=data.get("body", ""),
                                       name=data.get("name"), target=data.get("target_commitish", ""))
            return json_reply(201, release)
        if len(rest) == 2 and rest[0] == "releases" and rest[1].isdigit():
            release = next((row for row in self.releases if row["id"] == int(rest[1])), None)
            if release is None:
                return _not_found()
            if request.method == "GET":
                return json_reply(200, release)
            if request.method == "PATCH":
                return self._patch(release, json.loads(request.body))
        if len(rest) == 3 and rest[0] == "releases" and rest[1].isdigit() and rest[2] == "assets":
            release = next((row for row in self.releases if row["id"] == int(rest[1])), None)
            return self._page(request, copy.deepcopy(release["assets"])) if release else _not_found()
        if len(rest) == 3 and rest[:2] == ("releases", "assets") and rest[2].isdigit():
            return self._asset_download(request, int(rest[2]))
        if rest == ("git", "refs") and request.method == "POST":
            data = json.loads(request.body)
            tag = data["ref"].removeprefix("refs/tags/")
            if tag in self.refs:
                return json_reply(422, {"message": "Reference already exists"})
            self.add_ref(tag, data["sha"])
            return json_reply(201, {"ref": data["ref"], "object": {"sha": data["sha"], "type": "commit"}})
        if len(rest) == 4 and rest[:3] == ("git", "ref", "tags") and request.method == "GET":
            ref = self.refs.get(rest[3])
            return json_reply(200, {"ref": f"refs/tags/{rest[3]}", "object": dict(ref)}) if ref else _not_found()
        return _not_found()

    def _patch(self, release: dict[str, Any], data: dict[str, Any]) -> Reply:
        if "tag_name" in data:
            release["tag_name"] = data["tag_name"]
        elif release["draft"]:
            release["tag_name"] = "untagged-" + hashlib.sha1(str(release["id"]).encode()).hexdigest()[:20]
        if "body" in data:
            release["body"] = data["body"]
        if "draft" in data:
            release["draft"] = data["draft"]
        if data.get("make_latest") == "true":
            self.latest = release["id"]
        return json_reply(200, copy.deepcopy(release))

    def _asset_download(self, request: FakeHttp, asset_id: int) -> Reply:
        if asset_id not in self.data:
            return _not_found()
        if request.headers.get("accept") != "application/octet-stream":
            asset = next(asset for release in self.releases for asset in release["assets"] if asset["id"] == asset_id)
            return json_reply(200, asset)
        signature = hashlib.sha256(f"{asset_id}:{len(self._signatures)}".encode()).hexdigest()[:24]
        self._signatures[asset_id] = signature
        location = f"{self.STORAGE_ORIGIN}/{asset_id}/blob?sig={signature}&response-content-disposition=attachment"
        return Reply(302, b"", {"Location": location})

    def handle_storage(self, request: FakeHttp) -> Reply:
        if "authorization" in request.headers:
            # Like S3: a signed URL plus an Authorization header is an invalid request.
            return Reply(400, b"<Error><Code>InvalidArgument</Code></Error>", {"Content-Type": "application/xml"})
        segments = request.segments
        if (request.method != "GET" or len(segments) != 2 or not segments[0].isdigit()
                or self._signatures.get(int(segments[0])) != request.query.get("sig")):
            return Reply(403, b"<Error><Code>AccessDenied</Code></Error>", {"Content-Type": "application/xml"})
        return Reply(200, self.data[int(segments[0])], {"Content-Type": "application/octet-stream"})

    def handle_uploads(self, request: FakeHttp) -> Reply:
        if not self._authorized(request):
            return json_reply(401, {"message": "Bad credentials"})
        segments = request.segments
        prefix = tuple(["repos", *self.repo.split("/")])
        if (request.method != "POST" or segments[:3] != prefix or len(segments) != 6 or segments[3] != "releases"
                or not segments[4].isdigit() or segments[5] != "assets"):
            return _not_found()
        release = next((row for row in self.releases if row["id"] == int(segments[4])), None)
        if release is None:
            return _not_found()
        name = github_asset_rename(request.query.get("name", ""))
        if any(asset["name"] == name for asset in release["assets"]):
            return json_reply(422, {"message": "Validation Failed", "errors": [{"code": "already_exists"}]})
        asset = self.add_asset(release["id"], name, request.body)
        asset["content_type"] = request.headers.get("content-type", "")
        return json_reply(201, asset)
