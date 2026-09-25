"""The kit's only GitHub REST client (MB1).

Block Pops ``select_artifact.GitHubApi`` (``ProxyHandler({})``, no redirects, default TLS context,
32 MiB response cap, strict JSON) plus Quick Skin ``rotate_artifacts`` retry: at most
:data:`REQUEST_ATTEMPTS` attempts with exponential backoff and path-derived jitter capped at
:data:`MAX_RETRY_DELAY_SECONDS`, retrying only transport errors, :data:`RETRYABLE_HTTP_STATUSES`
and rate-limited 403s (``X-RateLimit-Remaining: 0``, ``Retry-After`` or a "rate limit" body),
honouring ``Retry-After``. A client is read-only unless constructed ``writable=True``: any
non-GET request on a read-only client raises :class:`ReadOnlyViolation` before touching the
network. ``max_requests`` bounds every request (retries included) of one client.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from mod_base.errors import MbError
from mod_base.model import limits

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


class GitHubApi:
    """A repository-scoped client. ``repository`` is ``owner/name``; paths passed to methods are
    absolute API paths starting with ``/`` (for example ``/repos/o/r/actions/runs/1``)."""

    def __init__(self, *, repository: str, token: str | None, base_url: str = DEFAULT_BASE_URL,
                 writable: bool = False, max_requests: int | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        raise NotImplementedError("owned by MB1")

    @property
    def repository(self) -> str:
        raise NotImplementedError("owned by MB1")

    @property
    def writable(self) -> bool:
        raise NotImplementedError("owned by MB1")

    @property
    def request_count(self) -> int:
        """Requests sent so far (every attempt counts)."""

        raise NotImplementedError("owned by MB1")

    def get_json(self, path: str, *, params: Mapping[str, str | int] | None = None) -> Any:
        """GET ``path`` and return strictly decoded JSON (``model.canonical.strict_loads``)."""

        raise NotImplementedError("owned by MB1")

    def paginate(self, path: str, *, field: str | None, params: Mapping[str, str | int] | None = None,
                 max_items: int) -> list[dict[str, Any]]:
        """GET every page (``per_page=100``) and return the concatenated ``field`` arrays (or the
        top-level arrays when ``field`` is None). More than ``max_items`` rows, a non-object row or
        a ``total_count`` that disagrees with the rows raises :class:`ApiError`."""

        raise NotImplementedError("owned by MB1")

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        """POST canonical JSON; requires ``writable``. Returns decoded JSON or None for 204."""

        raise NotImplementedError("owned by MB1")

    def delete(self, path: str) -> None:
        """DELETE ``path``; requires ``writable``. 204 is success; anything else raises."""

        raise NotImplementedError("owned by MB1")

    def download(self, path: str, *, max_bytes: int) -> bytes:
        """GET a binary endpoint (artifact ZIP) that answers with one redirect: the redirect is
        followed exactly once to an https URL with the ``Authorization`` header stripped; the
        body is read to at most ``max_bytes``."""

        raise NotImplementedError("owned by MB1")

    def rate_limit_snapshot(self) -> dict[str, int]:
        """``GET /rate_limit`` projected to numeric ``core`` counters: ``limit``, ``used``,
        ``remaining``, ``reset`` (the ``budget`` command; never tokens or headers)."""

        raise NotImplementedError("owned by MB1")


def from_environment(environ: Mapping[str, str], *, writable: bool = False,
                     max_requests: int | None = None) -> GitHubApi:
    """Build a client from ``GITHUB_REPOSITORY``, ``GH_TOKEN``/``GITHUB_TOKEN`` and
    ``GITHUB_API_URL`` (default :data:`DEFAULT_BASE_URL`); a missing token or repository raises."""

    raise NotImplementedError("owned by MB1")
