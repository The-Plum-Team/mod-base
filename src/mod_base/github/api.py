"""The kit's only GitHub REST client (MB1).

Block Pops ``select_artifact.GitHubApi`` (``ProxyHandler({})``, no redirects, default TLS context,
32 MiB response cap, strict JSON) plus Quick Skin ``rotate_artifacts`` retry: at most
:data:`REQUEST_ATTEMPTS` attempts with exponential backoff and path-derived jitter capped at
:data:`MAX_RETRY_DELAY_SECONDS`, retrying only transport errors, :data:`RETRYABLE_HTTP_STATUSES`
and rate-limited 403s (``X-RateLimit-Remaining: 0``, ``Retry-After`` or a "rate limit" body),
honouring ``Retry-After``. A client is read-only unless constructed ``writable=True``: any
non-GET request on a read-only client raises :class:`ReadOnlyViolation` before touching the
network. ``max_requests`` bounds every request (retries included) of one client.

Honouring ``Retry-After`` (and, for an exhausted primary budget, ``X-RateLimit-Reset``) means the
next attempt waits at least that long; a wait beyond :data:`MAX_RETRY_DELAY_SECONDS` ends the
retry budget at once (:class:`ApiRateLimited`) instead of stalling a job. Paths are absolute API
paths of unreserved characters (callers percent-encode free text); query parameters travel
separately. The bearer token is sent only to the configured API origin: redirects are refused,
except the single artifact-download redirect, which is followed once without any credential.
"""

from __future__ import annotations

import http.client
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import StrictJsonError, canonical_json, strict_loads

OWNER = "MB1"

DEFAULT_BASE_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "mod-base"
REQUEST_ATTEMPTS = 4
RETRYABLE_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 30.0
REQUEST_TIMEOUT_SECONDS = 30
PER_PAGE = 100
MAX_RESPONSE_BYTES = limits.MAX_API_RESPONSE_BYTES

#: Redirect statuses the artifact download accepts (GitHub answers ``302 Found``).
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_TOKEN_CHARS = 4096
MAX_ERROR_BODY_BYTES = 64 * 1024
MAX_REDIRECT_URL_CHARS = 8192
MAX_DOWNLOAD_BYTES = limits.MAX_RAW_BUNDLE_BYTES
_PATH = re.compile(r"^/(?:[A-Za-z0-9._~@:+,=-]|%[0-9A-Fa-f]{2})+(?:/(?:[A-Za-z0-9._~@:+,=-]|%[0-9A-Fa-f]{2})+)*$")
_PARAMETER = re.compile(r"^[a-z][a-z_]{0,39}$")
_READ_CHUNK = 1 << 20


class ApiError(MbError):
    """A GitHub request failed after its retry budget. ``status`` is 0 for transport errors."""

    default_reason = "github-api"

    def __init__(self, message: str, *, status: int, method: str, path: str) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path


class ApiNotFound(ApiError):
    """HTTP 404 (never retried)."""

    default_reason = "github-not-found"


class ApiRateLimited(ApiError):
    """The retry budget ended on a rate-limit response."""

    default_reason = "github-rate-limited"


class ReadOnlyViolation(MbError):
    """A non-GET request was attempted on a read-only client."""

    default_reason = "read-only"


class RequestBudgetExhausted(MbError):
    """The client's ``max_requests`` budget is spent (for example Pages' 160 reads)."""

    default_reason = "request-budget"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep the GitHub bearer credential pinned to the configured API origin: a 3xx surfaces as an
    ``HTTPError`` instead of being followed."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def _is_visible_ascii(value: str) -> bool:
    return all(33 <= ord(character) <= 126 for character in value)


def _validate_base_url(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048 or not _is_visible_ascii(value):
        raise MbError("GitHub API URL must be an absolute HTTPS origin", reason="usage")
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        # A malformed port or bracketed host ("https://[x]", "https://[::1").
        raise MbError("GitHub API URL is malformed", reason="usage") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or "@" in parsed.netloc or parsed.query or parsed.fragment):
        raise MbError("GitHub API URL must be an absolute HTTPS origin without credentials", reason="usage")
    return value.rstrip("/")


def _validate_path(path: Any) -> str:
    if not isinstance(path, str) or len(path) > 2048 or _PATH.fullmatch(path) is None:
        raise MbError(f"GitHub API path is unsafe: {path!r}"[:200], reason="usage")
    if any(urllib.parse.unquote(segment) in {".", ".."} for segment in path.split("/")[1:]):
        raise MbError("GitHub API path may not traverse", reason="usage")
    return path


def _query(params: Mapping[str, str | int] | None) -> str:
    if params is None:
        return ""
    if not isinstance(params, Mapping):
        raise MbError("GitHub API parameters must be a mapping", reason="usage")
    pairs: list[tuple[str, str]] = []
    for name in sorted(params):
        value = params[name]
        if not isinstance(name, str) or _PARAMETER.fullmatch(name) is None:
            raise MbError(f"GitHub API parameter name is unsafe: {name!r}"[:120], reason="usage")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise MbError(f"GitHub API parameter {name} must be text or an integer", reason="usage")
        if isinstance(value, int):
            if value < 0:
                raise MbError(f"GitHub API parameter {name} must not be negative", reason="usage")
            text = str(value)
        else:
            text = value
            if not text or len(text) > 1024 or any(ord(character) < 32 or ord(character) == 127 for character in text):
                raise MbError(f"GitHub API parameter {name} must be a short printable value", reason="usage")
        pairs.append((name, text))
    return urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote) if pairs else ""


def _positive(value: Any, label: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise MbError(f"{label} must be a positive integer no larger than {maximum}", reason="usage")
    return value


def _detail(data: bytes) -> str:
    """A bounded, single-line description of an error body (GitHub's ``message`` when present)."""

    try:
        value = strict_loads(data, label="GitHub error body", max_bytes=MAX_ERROR_BODY_BYTES)
    except StrictJsonError:
        value = None
    if isinstance(value, dict) and isinstance(value.get("message"), str):
        text = value["message"]
    else:
        text = data[:200].decode("utf-8", errors="replace")
    return " ".join(text.split())[:200]


def _header(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    value = headers.get(name)
    return value.strip() if isinstance(value, str) else ""


def _decimal(value: str) -> int | None:
    """A header's non-negative decimal value (``Content-Length``, ``Retry-After``, a reset time)."""

    return int(value) if re.fullmatch(r"[0-9]{1,10}", value) else None


@dataclass
class _Failure:
    """One failed attempt: the error to raise if retries end and whether a retry may help."""

    error: ApiError
    retryable: bool
    headers: Any = None


def paginate_with(get_json: Callable[..., Any], path: str, *, field: str | None,
                  params: Mapping[str, str | int] | None, max_items: int) -> list[dict[str, Any]]:
    """The one pagination loop of :class:`GitHubApi` and the fake (``per_page=100``, ``page=1..``).

    Stops at the first short page or once ``total_count`` rows are listed; more than ``max_items``
    rows, a non-object row, a page longer than ``per_page`` or a ``total_count`` that changes or
    disagrees with the rows raises."""

    _positive(max_items, "max_items", limits.MAX_RUN_ID)
    if params is not None and ("per_page" in params or "page" in params):
        raise MbError("pagination owns per_page and page", reason="usage")
    if field is not None and (not isinstance(field, str) or _PARAMETER.fullmatch(field) is None):
        raise MbError("pagination field is invalid", reason="usage")

    def malformed(message: str) -> ApiError:
        return ApiError(f"GitHub API GET {path} listing {message}", status=200, method="GET", path=path)

    rows: list[dict[str, Any]] = []
    total: int | None = None
    for page in range(1, max_items // PER_PAGE + 3):
        payload = get_json(path, params={**(params or {}), "per_page": PER_PAGE, "page": page})
        if field is None:
            if not isinstance(payload, list):
                raise malformed("is not an array")
            batch = payload
        else:
            if not isinstance(payload, dict) or not isinstance(payload.get(field), list):
                raise malformed(f"has no {field!r} array")
            batch = payload[field]
            if "total_count" in payload:
                count = payload["total_count"]
                if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                    raise malformed("has an invalid total_count")
                if total is not None and count != total:
                    raise malformed("total_count changed between pages")
                total = count
                if total > max_items:
                    raise malformed(f"reports {total} rows, beyond its bound of {max_items}")
        if len(batch) > PER_PAGE or any(not isinstance(row, dict) for row in batch):
            raise malformed("has a malformed page")
        rows.extend(batch)
        if len(rows) > max_items:
            raise malformed(f"exceeds its bound of {max_items} rows")
        # A short page ends the listing; so does a full page completing ``total_count`` (Quick Skin
        # ``feature_coverage_github.jobs``), which saves one read of the bounded budget.
        if len(batch) < PER_PAGE or (total is not None and len(rows) >= total):
            if total is not None and total != len(rows):
                raise malformed(f"total_count {total} disagrees with {len(rows)} listed rows")
            return rows
    raise malformed("did not end within its page bound")


class GitHubApi:
    """A repository-scoped client. ``repository`` is ``owner/name``; paths passed to methods are
    absolute API paths starting with ``/`` (for example ``/repos/o/r/actions/runs/1``)."""

    def __init__(self, *, repository: str, token: str | None, base_url: str = DEFAULT_BASE_URL,
                 writable: bool = False, max_requests: int | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._repository = grammar.require(grammar.REPOSITORY, repository, "repository")
        if token is not None and (not isinstance(token, str) or not token or len(token) > MAX_TOKEN_CHARS
                                  or not _is_visible_ascii(token)):
            raise MbError("GitHub token is malformed", reason="environment")
        if not isinstance(writable, bool):
            raise MbError("writable must be a boolean", reason="usage")
        if max_requests is not None:
            _positive(max_requests, "max_requests", 1_000_000)
        self._token = token
        self._base_url = _validate_base_url(base_url)
        self._writable = writable
        self._max_requests = max_requests
        self._sleep = sleep
        self._request_count = 0
        self._opener = _opener()

    def __repr__(self) -> str:  # never the token
        return f"GitHubApi(repository={self._repository!r}, writable={self._writable})"

    @property
    def repository(self) -> str:
        return self._repository

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def request_count(self) -> int:
        """Requests sent so far (every attempt counts)."""

        return self._request_count

    # -- transport --------------------------------------------------------------------------------

    def _spend(self) -> None:
        if self._max_requests is not None and self._request_count >= self._max_requests:
            raise RequestBudgetExhausted(f"the client's budget of {self._max_requests} GitHub requests is spent")
        self._request_count += 1

    def _api_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                   "User-Agent": USER_AGENT}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _open(self, method: str, url: str, headers: Mapping[str, str], body: bytes | None,
              limit: int) -> tuple[int, bytes]:
        """One HTTP exchange: ``(status, body)`` of a 2xx response read to at most ``limit`` bytes."""

        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        with self._opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = response.status
            declared = _decimal(_header(response.headers, "Content-Length"))
            if declared is not None and declared > limit:
                raise _TooLarge(status)
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
            raise _TooLarge(status)
        return status, data

    def _http_failure(self, method: str, path: str, exc: urllib.error.HTTPError) -> _Failure:
        body = b""
        if getattr(exc, "fp", None) is not None:
            try:
                body = exc.read(MAX_ERROR_BODY_BYTES)
            except (OSError, ValueError, http.client.HTTPException):  # ValueError: an already closed body
                body = b""
            finally:
                exc.close()
        if not isinstance(body, bytes):
            body = b""
        message = f"GitHub API {method} {path} failed: HTTP {exc.code}: {_detail(body)}"
        if exc.code == 404:
            return _Failure(ApiNotFound(message, status=404, method=method, path=path), False)
        rate_limited = exc.code == 429 or exc.code == 403 and bool(
            _header(exc.headers, "X-RateLimit-Remaining") == "0" or _header(exc.headers, "Retry-After")
            or b"rate limit" in body.lower())
        if rate_limited:
            return _Failure(ApiRateLimited(message, status=exc.code, method=method, path=path), True, exc.headers)
        return _Failure(ApiError(message, status=exc.code, method=method, path=path),
                        exc.code in RETRYABLE_HTTP_STATUSES, exc.headers)

    @staticmethod
    def _transport_failure(method: str, path: str, exc: BaseException) -> _Failure:
        reason = getattr(exc, "reason", None) or exc
        message = f"GitHub API {method} {path} failed in transport: {type(exc).__name__}: {reason}"
        return _Failure(ApiError(" ".join(message.split())[:300], status=0, method=method, path=path), True)

    @staticmethod
    def _retry_delay(path: str, attempt: int, headers: Any) -> float | None:
        """Backoff for ``attempt`` (0-based), at least ``Retry-After``/the primary reset; ``None``
        when honouring the server would exceed :data:`MAX_RETRY_DELAY_SECONDS`."""

        jitter = (sum(path.encode("utf-8")) % 997) / 997
        delay = min(float(2 ** attempt) + jitter, MAX_RETRY_DELAY_SECONDS)
        retry_after = _decimal(_header(headers, "Retry-After"))
        if retry_after is not None:
            delay = max(delay, float(retry_after))
        if _header(headers, "X-RateLimit-Remaining") == "0":
            reset = _decimal(_header(headers, "X-RateLimit-Reset"))
            if reset is not None:
                delay = max(delay, reset - time.time() + 1.0)
        return delay if delay <= MAX_RETRY_DELAY_SECONDS else None

    def _retrying(self, path: str, attempt_once: Callable[[], Any]) -> Any:
        for attempt in range(REQUEST_ATTEMPTS):
            outcome = attempt_once()
            if not isinstance(outcome, _Failure):
                return outcome
            if not outcome.retryable or attempt + 1 == REQUEST_ATTEMPTS:
                raise outcome.error
            delay = self._retry_delay(path, attempt, outcome.headers)
            if delay is None:
                raise outcome.error
            self._sleep(delay)
        raise AssertionError("unreachable: the retry loop returns or raises")  # pragma: no cover

    def _request(self, method: str, path: str, *, params: Mapping[str, str | int] | None = None,
                 body: bytes | None = None, statuses: frozenset[int]) -> tuple[int, bytes]:
        if method != "GET" and not self._writable:
            raise ReadOnlyViolation(f"refusing {method} {path!r} on a read-only GitHub client"[:200])
        path = _validate_path(path)
        query = _query(params)
        url = self._base_url + path + (f"?{query}" if query else "")
        headers = self._api_headers()
        if body is not None:
            headers["Content-Type"] = "application/json"

        def attempt_once() -> tuple[int, bytes] | _Failure:
            self._spend()
            try:
                status, data = self._open(method, url, headers, body, MAX_RESPONSE_BYTES)
            except urllib.error.HTTPError as exc:
                return self._http_failure(method, path, exc)
            except _TooLarge as exc:
                raise ApiError(f"GitHub API {method} {path} response exceeds {MAX_RESPONSE_BYTES} bytes",
                               status=exc.status, method=method, path=path) from None
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure(method, path, exc)
            if status not in statuses:
                raise ApiError(f"GitHub API {method} {path} answered HTTP {status}", status=status, method=method,
                               path=path)
            return status, data

        return self._retrying(path, attempt_once)

    # -- public surface ---------------------------------------------------------------------------

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        """GET ``path`` and return strictly decoded JSON (``model.canonical.strict_loads``)."""

        _, data = self._request("GET", path, params=params, statuses=frozenset({200}))
        try:
            return strict_loads(data, label=f"GitHub API {path}", max_bytes=MAX_RESPONSE_BYTES)
        except StrictJsonError as exc:
            raise ApiError(f"GitHub API GET {path} returned invalid JSON: {exc}"[:300], status=200, method="GET",
                           path=path) from exc

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        """GET every page (``per_page=100``) and return the concatenated ``field`` arrays (or the
        top-level arrays when ``field`` is None). More than ``max_items`` rows, a non-object row or
        a ``total_count`` that disagrees with the rows raises :class:`ApiError`."""

        return paginate_with(self.get_json, path, field=field, params=params, max_items=max_items)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        """POST canonical JSON; requires ``writable``. Returns decoded JSON or None for 204."""

        if not self._writable:
            raise ReadOnlyViolation(f"refusing POST {path!r} on a read-only GitHub client"[:200])
        if not isinstance(payload, Mapping):
            raise MbError("POST payload must be a JSON object", reason="usage")
        status, data = self._request("POST", path, body=canonical_json(dict(payload)),
                                     statuses=frozenset({200, 201, 202, 204}))
        if status == 204 or not data:
            return None
        try:
            return strict_loads(data, label=f"GitHub API {path}", max_bytes=MAX_RESPONSE_BYTES)
        except StrictJsonError as exc:
            raise ApiError(f"GitHub API POST {path} returned invalid JSON", status=status, method="POST",
                           path=path) from exc

    def delete(self, path: str) -> None:
        """DELETE ``path``; requires ``writable``. 204 is success; anything else raises."""

        if not self._writable:
            raise ReadOnlyViolation(f"refusing DELETE {path!r} on a read-only GitHub client"[:200])
        self._request("DELETE", path, statuses=frozenset({204}))

    def download(self, path: str, *, max_bytes: int) -> bytes:
        """GET a binary endpoint (artifact ZIP) that answers with one redirect: the redirect is
        followed exactly once to an https URL with the ``Authorization`` header stripped; the
        body is read to at most ``max_bytes``."""

        path = _validate_path(path)
        _positive(max_bytes, "max_bytes", MAX_DOWNLOAD_BYTES)
        url = self._base_url + path
        api_headers = self._api_headers()

        def attempt_once() -> bytes | _Failure:
            self._spend()
            try:
                status, _ = self._open("GET", url, api_headers, None, 0)
            except urllib.error.HTTPError as exc:
                if exc.code not in REDIRECT_STATUSES:
                    return self._http_failure("GET", path, exc)
                location = _header(exc.headers, "Location")
                if getattr(exc, "fp", None) is not None:
                    exc.close()
                target = _redirect_target(location, path)
            except _TooLarge as exc:
                raise ApiError(f"GitHub API GET {path} answered HTTP {exc.status} instead of a redirect",
                               status=exc.status, method="GET", path=path) from None
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure("GET", path, exc)
            else:
                raise ApiError(f"GitHub API GET {path} answered HTTP {status} instead of a redirect",
                               status=status, method="GET", path=path)
            # The one redirect: a fresh request carrying no credential and no API headers.
            self._spend()
            try:
                status, data = self._open("GET", target, {"User-Agent": USER_AGENT}, None, max_bytes)
            except urllib.error.HTTPError as exc:
                return self._http_failure("GET", path, exc)
            except _TooLarge as exc:
                raise ApiError(f"artifact download for {path} exceeds its {max_bytes}-byte bound",
                               status=exc.status, method="GET", path=path) from None
            except (OSError, http.client.HTTPException) as exc:
                return self._transport_failure("GET", path, exc)
            if status != 200:
                raise ApiError(f"artifact download for {path} answered HTTP {status}", status=status, method="GET",
                               path=path)
            return data

        return self._retrying(path, attempt_once)

    def rate_limit_snapshot(self) -> dict[str, int]:
        """``GET /rate_limit`` projected to numeric ``core`` counters: ``limit``, ``used``,
        ``remaining``, ``reset`` (the ``budget`` command; never tokens or headers)."""

        return rate_limit_counters(self.get_json("/rate_limit"))


class _TooLarge(Exception):
    """A response body (or declared length) exceeded the caller's bound; never retried."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


def _redirect_target(location: str, path: str) -> str:
    """Validate the artifact-download redirect: an https URL without credentials or fragment."""

    def refuse(reason: str) -> ApiError:
        return ApiError(f"GitHub API GET {path} redirected to an unsafe location: {reason}", status=302,
                        method="GET", path=path)

    if not location or len(location) > MAX_REDIRECT_URL_CHARS or not _is_visible_ascii(location):
        raise refuse("missing, oversized or not printable ASCII")
    try:
        parsed = urllib.parse.urlsplit(location)
        parsed.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError:
        # A malformed port or bracketed host ("https://[x]/", "https://[::1/").
        raise refuse("malformed URL") from None
    if parsed.scheme != "https" or not parsed.hostname:
        raise refuse("not an absolute https URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise refuse("the URL carries credentials")
    if parsed.fragment:
        raise refuse("the URL carries a fragment")
    return location


def rate_limit_counters(payload: Any) -> dict[str, int]:
    """Project a ``/rate_limit`` body to ``{limit, used, remaining, reset}`` (QS
    ``github_api_budget_snapshot`` rules: non-negative integers, ``limit > 0``, ``reset > 0``,
    ``remaining <= limit``)."""

    core = payload.get("resources", {}).get("core") if isinstance(payload, dict) and isinstance(
        payload.get("resources"), dict) else None
    if not isinstance(core, dict):
        raise ApiError("GitHub rate-limit response has no core resource", status=200, method="GET", path="/rate_limit")
    counters: dict[str, int] = {}
    for name in ("limit", "used", "remaining", "reset"):
        value = core.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ApiError("GitHub rate-limit counters are invalid", status=200, method="GET", path="/rate_limit")
        counters[name] = value
    if counters["limit"] <= 0 or counters["reset"] <= 0 or counters["remaining"] > counters["limit"]:
        raise ApiError("GitHub rate-limit counters are inconsistent", status=200, method="GET", path="/rate_limit")
    return counters


def from_environment(environ: Mapping[str, str], *, writable: bool = False,
                     max_requests: int | None = None) -> GitHubApi:
    """Build a client from ``GITHUB_REPOSITORY``, ``GH_TOKEN``/``GITHUB_TOKEN`` and
    ``GITHUB_API_URL`` (default :data:`DEFAULT_BASE_URL`); a missing token or repository raises."""

    repository = environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise MbError("GITHUB_REPOSITORY is required for GitHub API access", reason="environment")
    token = environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN")
    if not token:
        raise MbError("GH_TOKEN or GITHUB_TOKEN is required for GitHub API access", reason="environment")
    return GitHubApi(repository=repository, token=token, base_url=environ.get("GITHUB_API_URL") or DEFAULT_BASE_URL,
                     writable=writable, max_requests=max_requests)
