"""The one HTTPS client of the marketplace package (ADR 0008 "The network").

It follows :mod:`mod_base.github.api` (``ProxyHandler({})``, redirects refused, the default TLS
context, bounded bodies, strict JSON, a ``max_requests`` budget counting every attempt, read-only
unless ``writable``) with three additions the uploads need:

* **Closed origins.** A transport serves exactly one origin, ``https://host``. Every path is an
  absolute path of unreserved or percent-encoded characters that cannot traverse, and the built URL
  is checked to stay on that origin. The headers given at construction (a token) travel only to
  it: no redirect is ever followed, except the one storage redirect of
  :meth:`HttpsTransport.download_redirected`, which is sent with no credential at all.
* **No replay of a request whose acceptance an answer may hide.** A marketplace upload is not
  idempotent: a CurseForge or Modrinth upload sent twice is two files, and a GitHub draft created
  twice is two drafts. A request sent with ``replay=False`` is retried only after HTTP 429, on
  which the service acted not at all. After a 5xx, a transport error, or a 2xx whose answer cannot
  be read, it raises :class:`MarketplaceOutcomeUnknown` at once; the caller reconciles by content.
* **Exact downloads.** :meth:`HttpsTransport.get_bytes` reads exactly the expected length: a
  longer (or declared shorter) body is a definitive mismatch, never retried; a body cut short in
  transfer is transient, since hashing it would report a published file as a byte conflict.

Retries: at most ``limits.MARKETPLACE_REQUEST_ATTEMPTS`` attempts, on transport errors and
:data:`RETRYABLE_HTTP_STATUSES`, with exponential backoff and path-derived jitter capped at
``limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS``. ``Retry-After`` is honoured while it fits that cap
and the transport's total wait (``limits.MAX_MARKETPLACE_RETRY_WAIT_SECONDS``); a longer wait ends
the request at once. Exhausted retries raise :class:`MarketplaceUnavailable` (exit 3: re-dispatch
later); every other refusal is a :class:`MarketplaceError`. Error messages hold the method, the
path (never the query) and one bounded line of the answer with every credential redacted.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import secrets
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.errors import MbError, Unavailable
from mod_base.marketplace import limits
from mod_base.model.canonical import StrictJsonError, canonical_json, read_regular_file, strict_loads

USER_AGENT = "The-Plum-Team/mod-base"
#: Statuses a replayable request retries (429 is retried by every request).
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
#: Headers whose value is never a secret; every other construction header is redacted from errors.
PUBLIC_HEADERS = frozenset({"accept", "user-agent", "x-github-api-version"})
#: Headers a transport owns per request; construction headers may not set them.
RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection",
                              "cookie", "proxy-authorization", "te", "upgrade", "expect"})

_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ORIGIN = re.compile(rf"^https://({_HOST_LABEL}(?:\.{_HOST_LABEL})+)(?::443)?$")
_SEGMENT = r"(?:[A-Za-z0-9._~@:+,=!$()*;'-]|%[0-9A-Fa-f]{2})+"
_PATH = re.compile(rf"^(?:/{_SEGMENT})+$")
_PARAMETER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_PART_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
#: An uploaded file name: printable ASCII without quotes, backslashes, slashes or control characters.
_FILENAME = re.compile(r"^[A-Za-z0-9_.+()\[\]-](?:[A-Za-z0-9 _.+()\[\],=-]{0,253}[A-Za-z0-9_.+()\[\]-])?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_TYPE = re.compile(r"^[a-z]+/[a-z0-9.+-]+$")
_READ_CHUNK = 1 << 16


# -- errors and the shared result -----------------------------------------------------------------

class MarketplaceError(MbError):
    """A definitive refusal (exit 2). ``status`` is the HTTP status, 0 before or outside HTTP."""

    default_reason = "marketplace"

    def __init__(self, message: str, *, status: int = 0, method: str = "", path: str = "",
                 reason: str | None = None) -> None:
        super().__init__(message, reason=reason)
        self.status = status
        self.method = method
        self.path = path


class MarketplaceNotFound(MarketplaceError):
    """HTTP 404 (never retried)."""

    default_reason = "marketplace-not-found"


class MarketplaceUnavailable(Unavailable):
    """The service could not be reached or kept failing within the retry budget (exit 3)."""

    default_reason = "marketplace-unavailable"

    def __init__(self, message: str, *, status: int = 0, method: str = "", path: str = "",
                 reason: str | None = None) -> None:
        super().__init__(message, reason=reason)
        self.status = status
        self.method = method
        self.path = path


class MarketplaceOutcomeUnknown(MarketplaceUnavailable):
    """A non-replayable request got an answer that may hide its acceptance (a 5xx, a transport error
    or an unreadable 2xx). It was not sent again: reconcile by content before any retry."""

    default_reason = "marketplace-outcome-unknown"


class PublicationPending(Unavailable):
    """The marketplace holds the expected file but has not finished accepting it (exit 3). Before an
    upload it fences a settling upload; after one it means "poll again"."""

    default_reason = "publication-pending"


@dataclass(frozen=True)
class Reconciliation:
    """One file against one marketplace: ``publish`` when no remote copy exists, otherwise the
    ``remote_id`` of the copy whose bytes were proven equal. Collisions raise instead."""

    publish: bool
    remote_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.publish, bool):
            raise MarketplaceError("Reconciliation.publish must be a boolean", reason="usage")
        if self.publish != (self.remote_id is None):
            raise MarketplaceError("a reconciliation names a remote id exactly when nothing is published",
                                   reason="usage")
        if self.remote_id is not None and (not isinstance(self.remote_id, str) or not 0 < len(self.remote_id) <= 64):
            raise MarketplaceError("Reconciliation.remote_id must be a short string", reason="usage")


# -- validation helpers shared by the service modules --------------------------------------------

def require_filename(filename: Any, label: str = "file name") -> str:
    """``filename`` when it is a plain printable-ASCII name (no slash, quote, backslash or control
    character, no leading or trailing space, not ``.``/``..``) of at most
    ``limits.MAX_MARKETPLACE_FILENAME_CHARS`` characters."""

    if (not isinstance(filename, str) or len(filename) > limits.MAX_MARKETPLACE_FILENAME_CHARS
            or _FILENAME.fullmatch(filename) is None or filename in {".", ".."}):
        raise MarketplaceError(f"{label} is not a safe file name: {filename!r}"[:200], reason="usage")
    return filename


def require_text(value: Any, label: str, maximum: int, *, allow_empty: bool = False,
                 single_line: bool = True) -> str:
    """``value`` when it is a string of at most ``maximum`` characters, encodable as UTF-8, without
    control characters (newlines and tabs allowed unless ``single_line``)."""

    if not isinstance(value, str) or len(value) > maximum or (not value and not allow_empty):
        raise MarketplaceError(f"{label} must be a string of {'0' if allow_empty else '1'} to {maximum} characters",
                               reason="usage")
    allowed = "" if single_line else "\n\t\r"
    if any((ord(character) < 32 and character not in allowed) or ord(character) == 127 for character in value):
        raise MarketplaceError(f"{label} holds a control character", reason="usage")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise MarketplaceError(f"{label} is not valid Unicode text", reason="usage") from None
    if single_line and value != value.strip():
        raise MarketplaceError(f"{label} must not start or end with whitespace", reason="usage")
    return value


def require_match(pattern: re.Pattern[str], value: Any, label: str) -> str:
    """``value`` when it is a string fully matching ``pattern`` (a bounded preview names it otherwise)."""

    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise MarketplaceError(f"{label} is not valid: {value!r}"[:200], reason="usage")
    return value


def require_positive(value: Any, label: str, maximum: int = 2**53) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise MarketplaceError(f"{label} must be a positive integer no larger than {maximum}", reason="usage")
    return value


def require_sha256(value: Any, label: str = "SHA-256") -> str:
    """A SHA-256 hex digest, lowercased (upper-case manifests are accepted)."""

    if not isinstance(value, str) or _SHA256.fullmatch(value.lower()) is None:
        raise MarketplaceError(f"{label} is not a SHA-256 hex digest", reason="usage")
    return value.lower()


def quote_segment(value: str) -> str:
    """One path segment percent-encoded completely (a slash in ``value`` stays inside the segment)."""

    return urllib.parse.quote(value, safe="")


def content_type_for(filename: str) -> str:
    """The upload ``Content-Type`` of a release file: JARs are ``application/java-archive``."""

    return "application/java-archive" if filename.lower().endswith(".jar") else "application/octet-stream"


@dataclass(frozen=True)
class FileDigests:
    """Size and digests of one stable regular file, read once (``model.canonical.read_regular_file``)."""

    size: int
    sha1: str
    sha256: str
    sha512: str


def _read_upload(path: Any, label: str) -> bytes:
    if not isinstance(path, Path):
        raise MarketplaceError(f"{label} must be a pathlib.Path", reason="usage")
    try:
        return read_regular_file(path, label=label, max_bytes=limits.MAX_MARKETPLACE_FILE_BYTES)
    except StrictJsonError as exc:
        raise MarketplaceError(str(exc), reason="usage") from None


def _digests(data: bytes) -> FileDigests:
    return FileDigests(size=len(data), sha1=hashlib.sha1(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest(),
                       sha512=hashlib.sha512(data).hexdigest())


def file_digests(path: Path, *, sha256: str | None = None) -> FileDigests:
    """Digests of ``path`` (a stable regular file, no symlink, 1 byte to
    ``limits.MAX_MARKETPLACE_FILE_BYTES``). With ``sha256``, the bytes must have that digest: the
    upload functions bind what they send to the plan's hash before the first request."""

    digests = _digests(_read_upload(path, "upload file"))
    if sha256 is not None and digests.sha256 != require_sha256(sha256):
        raise MarketplaceError(f"upload file {path.name!r} does not have the expected SHA-256"[:200])
    return digests


# -- transport internals --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx surfaces as an ``HTTPError`` instead of being followed, so construction headers (a
    token) never leave their origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def build_opener() -> urllib.request.OpenerDirector:
    """No proxy route (``ProxyHandler({})`` ignores ``HTTPS_PROXY``), no redirects, default TLS."""

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def _visible_ascii(value: str) -> bool:
    return all(33 <= ord(character) <= 126 for character in value)


def _decimal(value: Any) -> int | None:
    return int(value) if isinstance(value, str) and re.fullmatch(r"[0-9]{1,19}", value.strip()) else None


def _header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    value = headers.get(name)
    return value.strip() if isinstance(value, str) else ""


class _TooLarge(Exception):
    """The body (or its declared length) exceeds the read bound: definitive, never retried."""


class _WrongLength(Exception):
    """The declared length differs from the exact length expected: another file, never retried."""


class _Truncated(Exception):
    """The body ended before the exact length expected: a transfer cut short, retried."""


@dataclass
class _Failure:
    """One failed attempt: ``kind`` is ``definitive``, ``transient`` or ``rate-limited``."""

    kind: str
    message: str
    status: int
    headers: Any = None
    not_found: bool = False


class HttpsTransport:
    """A client of exactly one ``https://host`` origin. Paths passed to methods are absolute
    (``/v2/version``); query parameters travel separately as a mapping. One transport may be shared
    by several threads: its budget and total wait are counted under a lock."""

    def __init__(self, origin: str, *, headers: Mapping[str, str] | None = None, max_requests: int,
                 writable: bool = False, opener: Any = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if not isinstance(origin, str) or len(origin) > 300 or _ORIGIN.fullmatch(origin) is None:
            raise MarketplaceError(f"marketplace origin must be exactly https://host: {origin!r}"[:200], reason="usage")
        if not isinstance(writable, bool):
            raise MarketplaceError("writable must be a boolean", reason="usage")
        require_positive(max_requests, "max_requests", limits.MAX_MARKETPLACE_REQUESTS)
        if opener is not None and not callable(getattr(opener, "open", None)):
            raise MarketplaceError("opener must provide open(request, timeout=...)", reason="usage")
        if not callable(sleep):
            raise MarketplaceError("sleep must be callable", reason="usage")
        self._origin = origin
        self._host = _ORIGIN.fullmatch(origin).group(1)  # type: ignore[union-attr]
        self._headers, self._secrets = self._validate_headers(headers)
        self._writable = writable
        self._max_requests = max_requests
        self._opener = opener if opener is not None else build_opener()
        self._sleep = sleep
        self._request_count = 0
        self._waited = 0.0
        # Uploads to one marketplace run in a few threads over one transport: the budget and the
        # total wait are shared, so they are counted under one lock.
        self._lock = threading.Lock()

    @staticmethod
    def _validate_headers(headers: Mapping[str, str] | None) -> tuple[dict[str, str], tuple[str, ...]]:
        if headers is None:
            headers = {}
        if not isinstance(headers, Mapping) or len(headers) > limits.MAX_MARKETPLACE_HEADERS:
            raise MarketplaceError("transport headers must be a small mapping", reason="usage")
        validated: dict[str, str] = {}
        found: list[str] = []
        for name, value in headers.items():
            if not isinstance(name, str) or _HEADER_NAME.fullmatch(name) is None:
                raise MarketplaceError(f"header name is unsafe: {name!r}"[:120], reason="usage")
            lowered = name.lower()
            if lowered in RESERVED_HEADERS or any(key.lower() == lowered for key in validated):
                raise MarketplaceError(f"header {name} is reserved or repeated", reason="usage")
            if (not isinstance(value, str) or not value or len(value) > limits.MAX_MARKETPLACE_HEADER_CHARS
                    or value != value.strip() or any(not 32 <= ord(character) <= 126 for character in value)):
                # The value is never echoed: it may be a token.
                raise MarketplaceError(f"header {name} has a malformed value", reason="usage")
            validated[name] = value
            if lowered not in PUBLIC_HEADERS:
                found.append(value)
                credential = value.split(" ", 1)[-1]
                if len(credential) >= 4:
                    found.append(credential)
        if not any(key.lower() == "user-agent" for key in validated):
            validated["User-Agent"] = USER_AGENT
        return validated, tuple(sorted(set(found), key=len, reverse=True))

    def __repr__(self) -> str:  # never the headers
        return f"HttpsTransport(origin={self._origin!r}, writable={self._writable})"

    @property
    def origin(self) -> str:
        return self._origin

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def request_count(self) -> int:
        """Requests sent so far: every attempt and every followed redirect counts."""

        return self._request_count

    # -- building a request -----------------------------------------------------------------------

    def _url(self, path: Any, query: Mapping[str, str | int] | None) -> str:
        if not isinstance(path, str) or len(path) > limits.MAX_MARKETPLACE_PATH_CHARS or _PATH.fullmatch(path) is None:
            raise MarketplaceError(f"marketplace path is unsafe: {path!r}"[:200], reason="usage")
        for segment in path.split("/")[1:]:
            decoded = urllib.parse.unquote(segment)
            if decoded in {".", ".."} or any(character in decoded for character in "/\\") or any(
                    ord(character) < 32 or ord(character) == 127 for character in decoded):
                raise MarketplaceError(f"marketplace path may not traverse: {path!r}"[:200], reason="usage")
        url = self._origin + path
        text = self._query(query)
        if text:
            url += "?" + text
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != self._host or parsed.port not in (None, 443) or (
                parsed.username is not None or parsed.fragment):
            raise MarketplaceError(f"marketplace URL leaves its origin {self._origin}", reason="usage")
        return url

    @staticmethod
    def _query(query: Mapping[str, str | int] | None) -> str:
        if query is None:
            return ""
        if not isinstance(query, Mapping) or len(query) > limits.MAX_MARKETPLACE_QUERY_PARAMETERS:
            raise MarketplaceError("query parameters must be a small mapping", reason="usage")
        pairs: list[tuple[str, str]] = []
        for name in sorted(query):
            value = query[name]
            if not isinstance(name, str) or _PARAMETER.fullmatch(name) is None:
                raise MarketplaceError(f"query parameter name is unsafe: {name!r}"[:120], reason="usage")
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise MarketplaceError(f"query parameter {name} must be text or an integer", reason="usage")
            if isinstance(value, int):
                if value < 0:
                    raise MarketplaceError(f"query parameter {name} must not be negative", reason="usage")
                text = str(value)
            else:
                text = require_text(value, f"query parameter {name}", limits.MAX_MARKETPLACE_QUERY_VALUE_CHARS)
            pairs.append((name, text))
        return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)

    def _request_headers(self, *, content_type: str | None = None, accept: str | None = None) -> dict[str, str]:
        headers = dict(self._headers)
        if accept is not None:
            for name in [name for name in headers if name.lower() == "accept"]:
                del headers[name]
            headers["Accept"] = accept
        if content_type is not None:
            headers["Content-Type"] = content_type
        return headers

    def _require_writable(self, method: str, path: str) -> None:
        if not self._writable:
            raise MarketplaceError(f"refusing {method} {path!r} on a read-only marketplace transport"[:200],
                                   method=method, path=path if isinstance(path, str) else "", reason="read-only")

    # -- one exchange -----------------------------------------------------------------------------

    def _spend(self) -> None:
        with self._lock:
            if self._request_count >= self._max_requests:
                raise MarketplaceError(f"the transport's budget of {self._max_requests} requests to {self._origin} "
                                       "is spent", reason="request-budget")
            self._request_count += 1

    def _open(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, limit: int,
              exact: int | None) -> tuple[int, bytes]:
        """``(status, body)`` of a 2xx answer read to at most ``limit`` bytes (exactly ``exact``)."""

        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        with self._opener.open(request, timeout=limits.MARKETPLACE_REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            declared = _decimal(_header(response.headers, "Content-Length"))
            if declared is not None and declared > limit:
                raise _TooLarge()
            if exact is not None and declared is not None and declared != exact:
                raise _WrongLength()
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = response.read(min(_READ_CHUNK, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
        if len(data) > limit:
            raise _TooLarge()
        if exact is not None and len(data) != exact:
            raise _Truncated()
        return status, data

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "<redacted>")
        return text

    def _detail(self, data: bytes) -> str:
        """One bounded, credential-free line describing an error body (its message when JSON)."""

        text = ""
        try:
            value = strict_loads(data, label="marketplace error body", max_bytes=limits.MAX_MARKETPLACE_ERROR_BODY_BYTES)
        except StrictJsonError:
            value = None
        if isinstance(value, dict):
            for key in ("description", "errorMessage", "message", "error"):
                if isinstance(value.get(key), str):
                    text = value[key]
                    break
        if not text:
            text = data[:1024].decode("utf-8", errors="replace")
        line = self._redact(" ".join(self._redact(text).split()))
        return line[:limits.MAX_MARKETPLACE_ERROR_DETAIL_CHARS]

    def _http_failure(self, method: str, path: str, exc: urllib.error.HTTPError) -> _Failure:
        body = b""
        if getattr(exc, "fp", None) is not None:
            try:
                body = exc.read(limits.MAX_MARKETPLACE_ERROR_BODY_BYTES)
            except (OSError, ValueError, http.client.HTTPException):
                body = b""
            finally:
                exc.close()
        if not isinstance(body, bytes):
            body = b""
        code = exc.code
        if code in REDIRECT_STATUSES:
            # Never followed and never described: a Location may carry a signed query.
            return _Failure("definitive", f"{method} {self._origin}{path} refused a redirect (HTTP {code})", code)
        message = f"{method} {self._origin}{path} failed: HTTP {code}: {self._detail(body)}"
        if code == 404:
            return _Failure("definitive", message, code, not_found=True)
        # GitHub answers an exhausted or secondary rate limit with 403 (``github.api`` reads it the
        # same way). A rate-limited request was not acted on, so even a non-replayable one may retry.
        if code == 429 or code == 403 and bool(
                _header(exc.headers, "X-RateLimit-Remaining") == "0" or _header(exc.headers, "Retry-After")
                or b"rate limit" in body.lower()):
            return _Failure("rate-limited", message, code, exc.headers)
        if code in RETRYABLE_HTTP_STATUSES:
            return _Failure("transient", message, code, exc.headers)
        return _Failure("definitive", message, code)

    def _transport_failure(self, method: str, path: str, exc: BaseException) -> _Failure:
        reason = getattr(exc, "reason", None) or exc
        message = self._redact(" ".join(f"{method} {self._origin}{path} failed in transport: "
                                        f"{type(exc).__name__}: {reason}".split()))
        return _Failure("transient", message[:400], 0)

    def _retry_delay(self, path: str, attempt: int, headers: Any) -> float | None:
        jitter = (sum(path.encode("utf-8")) % 997) / 997
        delay = min(float(2 ** attempt) + jitter, limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS)
        retry_after = _decimal(_header(headers, "Retry-After"))
        if retry_after is not None:
            delay = max(delay, float(retry_after))
        if delay > limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS:
            return None
        with self._lock:
            if self._waited + delay > limits.MAX_MARKETPLACE_RETRY_WAIT_SECONDS:
                return None
            self._waited += delay
        return delay

    def _retrying(self, method: str, path: str, attempt_once: Callable[[], Any], *, replay: bool) -> Any:
        for attempt in range(limits.MARKETPLACE_REQUEST_ATTEMPTS):
            outcome = attempt_once()
            if not isinstance(outcome, _Failure):
                return outcome
            if outcome.kind == "definitive":
                kind = MarketplaceNotFound if outcome.not_found else MarketplaceError
                raise kind(outcome.message, status=outcome.status, method=method, path=path)
            if outcome.kind == "transient" and not replay:
                raise MarketplaceOutcomeUnknown(f"{outcome.message}; not sent again, since the request may have "
                                                "been accepted", status=outcome.status, method=method, path=path)
            if attempt + 1 == limits.MARKETPLACE_REQUEST_ATTEMPTS:
                raise MarketplaceUnavailable(f"{outcome.message} (after {limits.MARKETPLACE_REQUEST_ATTEMPTS} attempts)",
                                             status=outcome.status, method=method, path=path)
            delay = self._retry_delay(path, attempt, outcome.headers)
            if delay is None:
                raise MarketplaceUnavailable(f"{outcome.message} (the service asks for a wait beyond the bound)",
                                             status=outcome.status, method=method, path=path)
            self._sleep(delay)
        raise AssertionError("unreachable: the retry loop returns or raises")  # pragma: no cover

    def _send(self, method: str, path: str, *, query: Mapping[str, str | int] | None = None,
              body: bytes | None = None, content_type: str | None = None, accept: str | None = None,
              statuses: frozenset[int], limit: int, exact: int | None = None, replay: bool) -> tuple[int, bytes]:
        if method != "GET":
            self._require_writable(method, path)
        url = self._url(path, query)
        headers = self._request_headers(content_type=content_type, accept=accept)

        def attempt_once() -> tuple[int, bytes] | _Failure:
            self._spend()
            try:
                status, data = self._open(method, url, headers, body, limit, exact)
            except urllib.error.HTTPError as exc:
                return self._http_failure(method, path, exc)
            except _TooLarge:
                return self._unreadable(method, path, f"answer exceeds its {limit}-byte bound", replay)
            except _WrongLength:
                return _Failure("definitive", f"{method} {self._origin}{path} declares another length than the "
                                f"expected {exact} bytes", 200)
            except _Truncated:
                return _Failure("transient", f"{method} {self._origin}{path} transfer ended short of {exact} bytes", 200)
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure(method, path, exc)
            if status not in statuses:
                return self._unreadable(method, path, f"answered HTTP {status}", replay, status=status)
            return status, data

        return self._retrying(method, path, attempt_once, replay=replay)

    def _unreadable(self, method: str, path: str, problem: str, replay: bool, *, status: int = 200) -> _Failure:
        """A success answer that cannot be used. For a request that may not be replayed the service
        has very likely acted on it, so the caller must reconcile (:class:`MarketplaceOutcomeUnknown`)."""

        message = f"{method} {self._origin}{path} {problem}"
        if not replay and method != "GET":
            raise MarketplaceOutcomeUnknown(message, status=status, method=method, path=path)
        return _Failure("definitive", message, status)

    def _decode(self, method: str, path: str, data: bytes, *, replay: bool) -> Any:
        try:
            return strict_loads(data, label=f"{self._origin}{path}", max_bytes=limits.MAX_MARKETPLACE_JSON_BYTES)
        except StrictJsonError as exc:
            message = f"{method} {self._origin}{path} returned invalid JSON: {exc}"[:400]
            if not replay and method != "GET":
                raise MarketplaceOutcomeUnknown(message, status=200, method=method, path=path) from None
            raise MarketplaceError(message, status=200, method=method, path=path) from None

    # -- public surface ---------------------------------------------------------------------------

    def get_json(self, path: str, query: Mapping[str, str | int] | None = None) -> Any:
        """GET ``path`` and return its strictly decoded JSON (HTTP 200 only; 404 raises
        :class:`MarketplaceNotFound`)."""

        _, data = self._send("GET", path, query=query, accept=None, statuses=frozenset({200}),
                             limit=limits.MAX_MARKETPLACE_JSON_BYTES, replay=True)
        return self._decode("GET", path, data, replay=True)

    def get_bytes(self, path: str, *, limit: int) -> bytes:
        """GET exactly ``limit`` bytes (a CDN copy of a file whose size is known). A longer body, or
        a declared other length, is refused without retry; a transfer cut short is retried."""

        require_positive(limit, "limit", limits.MAX_MARKETPLACE_FILE_BYTES)
        _, data = self._send("GET", path, accept="*/*", statuses=frozenset({200}), limit=limit, exact=limit,
                             replay=True)
        return data

    def download_redirected(self, path: str, *, limit: int, accept: str = "application/octet-stream") -> bytes:
        """GET exactly ``limit`` bytes from an endpoint that may answer with one redirect to its
        storage (GitHub release assets). The redirect is followed once, to an ``https`` URL without
        credentials or fragment, as a fresh request carrying only ``User-Agent`` and ``Accept``:
        no construction header (the token) goes there. A second redirect is refused."""

        require_positive(limit, "limit", limits.MAX_MARKETPLACE_FILE_BYTES)
        if not isinstance(accept, str) or _CONTENT_TYPE.fullmatch(accept) is None:
            raise MarketplaceError(f"accept type is unsafe: {accept!r}"[:120], reason="usage")
        url = self._url(path, None)
        headers = self._request_headers(accept=accept)
        agent = next(value for name, value in self._headers.items() if name.lower() == "user-agent")
        bare = {"User-Agent": agent, "Accept": accept}

        def attempt_once() -> bytes | _Failure:
            self._spend()
            try:
                _, data = self._open("GET", url, headers, None, limit, limit)
                return data
            except urllib.error.HTTPError as exc:
                if exc.code not in REDIRECT_STATUSES:
                    return self._http_failure("GET", path, exc)
                location = _header(exc.headers, "Location")
                if getattr(exc, "fp", None) is not None:
                    exc.close()
            except _TooLarge:
                return _Failure("definitive", f"GET {self._origin}{path} exceeds its {limit}-byte bound", 200)
            except _WrongLength:
                return _Failure("definitive", f"GET {self._origin}{path} declares another length than {limit}", 200)
            except _Truncated:
                return _Failure("transient", f"GET {self._origin}{path} transfer ended short of {limit} bytes", 200)
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure("GET", path, exc)
            target = self._redirect_target(location, path)
            self._spend()
            try:
                _, data = self._open("GET", target, bare, None, limit, limit)
            except urllib.error.HTTPError as exc:
                failure = self._http_failure("GET", path, exc)
                failure.message = f"{failure.message} (at the storage redirect)"
                return failure
            except _TooLarge:
                return _Failure("definitive", f"GET {self._origin}{path} storage copy exceeds its {limit}-byte bound", 200)
            except _WrongLength:
                return _Failure("definitive", f"GET {self._origin}{path} storage copy declares another length than "
                                f"{limit}", 200)
            except _Truncated:
                return _Failure("transient", f"GET {self._origin}{path} storage transfer ended short of {limit} bytes",
                                200)
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure("GET", path, exc)
            return data

        return self._retrying("GET", path, attempt_once, replay=True)

    def _redirect_target(self, location: str, path: str) -> str:
        def refuse(reason: str) -> MarketplaceError:
            return MarketplaceError(f"GET {self._origin}{path} redirected to an unsafe location: {reason}",
                                    status=302, method="GET", path=path)

        if not location or len(location) > limits.MAX_MARKETPLACE_REDIRECT_URL_CHARS or not _visible_ascii(location):
            raise refuse("missing, oversized or not printable ASCII")
        try:
            parsed = urllib.parse.urlsplit(location)
            port = parsed.port
        except ValueError:
            raise refuse("malformed URL") from None
        if parsed.scheme != "https" or not parsed.hostname or port not in (None, 443):
            raise refuse("not an absolute https URL on port 443")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise refuse("the URL carries credentials")
        if parsed.fragment:
            raise refuse("the URL carries a fragment")
        return location

    def post_json(self, path: str, body: Mapping[str, Any], *, replay: bool) -> Any:
        """POST canonical JSON (requires ``writable``) and return the decoded 200/201 answer.
        ``replay=False`` for a request the service would act on twice (module docstring)."""

        self._require_writable("POST", path)
        payload = self._json_body(body)
        _, data = self._send("POST", path, body=payload, content_type="application/json",
                             statuses=frozenset({200, 201}), limit=limits.MAX_MARKETPLACE_JSON_BYTES,
                             replay=self._replay(replay))
        return self._decode("POST", path, data, replay=replay)

    def patch_json(self, path: str, body: Mapping[str, Any]) -> Any:
        """PATCH canonical JSON (requires ``writable``); a PATCH restates state, so it is replayed."""

        self._require_writable("PATCH", path)
        payload = self._json_body(body)
        _, data = self._send("PATCH", path, body=payload, content_type="application/json",
                             statuses=frozenset({200}), limit=limits.MAX_MARKETPLACE_JSON_BYTES, replay=True)
        return self._decode("PATCH", path, data, replay=True)

    def post_multipart(self, path: str, fields: Sequence[tuple[str, str]], files: Sequence[tuple[str, str, Path]], *,
                       replay: bool, expected_sha256: Mapping[str, str] | None = None) -> Any:
        """POST ``multipart/form-data`` (requires ``writable``): ``fields`` are ``(name, text)``,
        ``files`` are ``(part name, file name, path)``. Each file is read once as a stable regular
        file; ``expected_sha256`` (part name -> hex) binds the bytes sent to a digest computed
        earlier, so a file that changed in between is refused before the request."""

        self._require_writable("POST", path)
        body, content_type = encode_multipart(fields, files, expected_sha256=expected_sha256)
        _, data = self._send("POST", path, body=body, content_type=content_type, statuses=frozenset({200, 201}),
                             limit=limits.MAX_MARKETPLACE_JSON_BYTES, replay=self._replay(replay))
        return self._decode("POST", path, data, replay=replay)

    def post_bytes(self, path: str, data_path: Path, content_type: str, query: Mapping[str, str | int] | None, *,
                   replay: bool, expected_sha256: str | None = None) -> Any:
        """POST one file's bytes as the body (requires ``writable``; GitHub's asset upload)."""

        self._require_writable("POST", path)
        if not isinstance(content_type, str) or _CONTENT_TYPE.fullmatch(content_type) is None:
            raise MarketplaceError(f"content type is unsafe: {content_type!r}"[:120], reason="usage")
        data = _read_upload(data_path, "upload file")
        if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != require_sha256(expected_sha256):
            raise MarketplaceError("upload file changed since it was hashed")
        _, answer = self._send("POST", path, query=query, body=data, content_type=content_type,
                               statuses=frozenset({200, 201}), limit=limits.MAX_MARKETPLACE_JSON_BYTES,
                               replay=self._replay(replay))
        return self._decode("POST", path, answer, replay=replay)

    @staticmethod
    def _replay(replay: Any) -> bool:
        if not isinstance(replay, bool):
            raise MarketplaceError("replay must be a boolean", reason="usage")
        return replay

    @staticmethod
    def _json_body(body: Any) -> bytes:
        if not isinstance(body, Mapping):
            raise MarketplaceError("a JSON request body must be an object", reason="usage")
        try:
            payload = canonical_json(dict(body))
        except MbError as exc:
            raise MarketplaceError(f"request body is not canonical JSON: {exc}"[:200], reason="usage") from None
        if len(payload) > limits.MAX_MARKETPLACE_REQUEST_JSON_BYTES:
            raise MarketplaceError("request body exceeds its bound", reason="usage")
        return payload


def encode_multipart(fields: Sequence[tuple[str, str]], files: Sequence[tuple[str, str, Path]], *,
                     expected_sha256: Mapping[str, str] | None = None) -> tuple[bytes, str]:
    """``(body, content type)`` of a ``multipart/form-data`` request. Part names are unique plain
    tokens and file names pass :func:`require_filename`, so no quoting is ever needed; the boundary
    is random and checked to occur in no part."""

    if not isinstance(fields, Sequence) or not isinstance(files, Sequence) or isinstance(fields, (str, bytes)):
        raise MarketplaceError("multipart fields and files must be sequences", reason="usage")
    if not 0 < len(fields) + len(files) <= limits.MAX_MULTIPART_PARTS:
        raise MarketplaceError(f"a multipart body holds 1 to {limits.MAX_MULTIPART_PARTS} parts", reason="usage")
    expected = dict(expected_sha256 or {})
    names: list[str] = []
    parts: list[tuple[bytes, bytes]] = []
    for field in fields:
        if not isinstance(field, tuple) or len(field) != 2:
            raise MarketplaceError("a multipart field is (name, text)", reason="usage")
        name, value = field
        names.append(_part_name(name))
        text = require_text(value, f"multipart field {name}", limits.MAX_MARKETPLACE_REQUEST_JSON_BYTES,
                            allow_empty=True, single_line=False)
        parts.append((f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"), text.encode("utf-8")))
    for item in files:
        if not isinstance(item, tuple) or len(item) != 3:
            raise MarketplaceError("a multipart file is (name, file name, path)", reason="usage")
        name, filename, path = item
        names.append(_part_name(name))
        require_filename(filename)
        data = _read_upload(path, f"multipart file {name}")
        if name in expected and hashlib.sha256(data).hexdigest() != require_sha256(expected.pop(name)):
            raise MarketplaceError(f"multipart file {name} changed since it was hashed")
        header = (f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                  f"Content-Type: {content_type_for(filename)}\r\n\r\n")
        parts.append((header.encode("ascii"), data))
    if len(set(names)) != len(names):
        raise MarketplaceError("multipart part names must be unique", reason="usage")
    if expected:
        raise MarketplaceError(f"expected digests name parts that are not files: {sorted(expected)}"[:200],
                               reason="usage")
    for _ in range(4):
        boundary = "mod-base-" + secrets.token_hex(24)
        marker = boundary.encode("ascii")
        if not any(marker in content for _, content in parts):
            break
    else:  # pragma: no cover - a 192-bit random boundary inside a payload four times running
        raise MarketplaceError("no multipart boundary is absent from the payload")
    chunks: list[bytes] = []
    for header, content in parts:
        chunks += [b"--", marker, b"\r\n", header, content, b"\r\n"]
    chunks += [b"--", marker, b"--\r\n"]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _part_name(name: Any) -> str:
    if not isinstance(name, str) or _PART_NAME.fullmatch(name) is None:
        raise MarketplaceError(f"multipart part name is unsafe: {name!r}"[:120], reason="usage")
    return name
