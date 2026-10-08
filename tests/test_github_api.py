"""``github.api``: the kit's only REST client.

Transport rules from Block Pops ``select_artifact.GitHubApi`` (no redirects, ``ProxyHandler({})``,
default TLS, 32 MiB bound, strict JSON), the retry policy of Quick Skin
``rotate_artifacts.GitHubApi`` (ported ``test_pages_api_retries_installation_rate_limit_then_returns_clean_json``),
the credential-free single redirect of artifact downloads, and the read-only mode. Only HTTP is
mocked: every request goes through the client's real request construction and response checks.
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import ssl
import unittest
import urllib.error
import urllib.request
from typing import Any
from unittest.mock import patch

from mod_base.errors import MbError
from mod_base.github import api
from mod_base.github.api import (
    ApiError,
    ApiNotFound,
    ApiRateLimited,
    GitHubApi,
    InconsistentListing,
    ReadOnlyViolation,
    RequestBudgetExhausted,
)
from mod_base.model import limits

REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
BASE = "https://api.github.test"
RUN_PATH = f"/repos/{REPOSITORY}/actions/runs/1"


def headers(values: dict[str, str] | None = None) -> email.message.Message:
    message = email.message.Message()
    for name, value in (values or {}).items():
        message[name] = value
    return message


class Response:
    """A minimal ``http.client.HTTPResponse`` stand-in (context manager, ``status``, ``read``)."""

    def __init__(self, status: int = 200, body: bytes = b"{}", header_values: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers(header_values)
        self._body = io.BytesIO(body)

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def http_error(code: int, body: bytes = b"{}", header_values: dict[str, str] | None = None,
               url: str = BASE) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", headers(header_values), io.BytesIO(body))


class Opener:
    """Replays ``outcomes`` (a response or an exception per request) and records each request."""

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list[float] = []

    def open(self, request: urllib.request.Request, timeout: float) -> Response:
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self.outcomes:
            raise AssertionError(f"unexpected request {request.full_url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class ClientTestCase(unittest.TestCase):
    def client(self, outcomes: list[Any], **options: Any) -> tuple[GitHubApi, Opener, list[float]]:
        opener = Opener(outcomes)
        sleeps: list[float] = []
        options.setdefault("repository", REPOSITORY)
        options.setdefault("token", "fixture-token")
        options.setdefault("base_url", BASE)
        with patch.object(api, "_opener", return_value=opener):
            client = GitHubApi(sleep=sleeps.append, **options)
        return client, opener, sleeps


class TransportTests(ClientTestCase):
    def test_opener_ignores_proxies_refuses_redirects_and_verifies_tls(self) -> None:
        with patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy.invalid:3128", "https_proxy": "http://p:1"}):
            opener = api._opener()
            default = urllib.request.build_opener()
        # The stock opener would pick the environment proxy up; the kit's has no proxy route at all.
        self.assertTrue(any(isinstance(handler, urllib.request.ProxyHandler) and handler.proxies
                            for handler in default.handlers))
        self.assertEqual([], [handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)
                              and handler.proxies])
        self.assertEqual([urllib.request.HTTPSHandler],
                         [type(handler) for handler in opener.handle_open["https"]])
        redirects = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([api._NoRedirect], [type(handler) for handler in redirects])
        request = urllib.request.Request(BASE + "/x", headers={"Authorization": "Bearer secret"})
        self.assertIsNone(redirects[0].redirect_request(request, None, 302, "Found", headers(), "https://evil.test/"))
        https = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]
        self.assertEqual(1, len(https))
        context = https[0]._context
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        self.assertTrue(context.check_hostname)

    def test_get_sends_exact_headers_and_decodes_strict_json(self) -> None:
        client, opener, sleeps = self.client([Response(body=b'{"id":1,"name":"x"}')])
        self.assertEqual({"id": 1, "name": "x"}, client.get_json(RUN_PATH, params={"page": 2, "name": "a b/c"}))
        request = opener.requests[0]
        self.assertEqual(f"{BASE}{RUN_PATH}?name=a%20b%2Fc&page=2", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual("Bearer fixture-token", request.get_header("Authorization"))
        self.assertEqual("application/vnd.github+json", request.get_header("Accept"))
        self.assertEqual(api.API_VERSION, request.get_header("X-github-api-version"))
        self.assertEqual(api.USER_AGENT, request.get_header("User-agent"))
        self.assertEqual([api.REQUEST_TIMEOUT_SECONDS], opener.timeouts)
        self.assertEqual(([], 1), (sleeps, client.request_count))
        self.assertNotIn("fixture-token", repr(client))

    def test_an_anonymous_client_sends_no_authorization(self) -> None:
        client, opener, _ = self.client([Response(body=b"[]")], token=None)
        self.assertEqual([], client.get_json(RUN_PATH))
        self.assertIsNone(opener.requests[0].get_header("Authorization"))

    def test_hostile_json_and_unexpected_statuses_are_rejected_without_retry(self) -> None:
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b"\xef\xbb\xbf{}", b"", b"{", b'"\\ud800"'):
            with self.subTest(body=body):
                client, _, sleeps = self.client([Response(body=body)])
                with self.assertRaisesRegex(ApiError, "invalid JSON"):
                    client.get_json(RUN_PATH)
                self.assertEqual([], sleeps)
        client, _, _ = self.client([Response(status=202, body=b"{}")])
        with self.assertRaisesRegex(ApiError, "HTTP 202"):
            client.get_json(RUN_PATH)

    def test_responses_are_bounded_before_and_while_reading(self) -> None:
        declared = Response(body=b"{}", header_values={"Content-Length": str(api.MAX_RESPONSE_BYTES + 1)})
        client, _, sleeps = self.client([declared])
        with self.assertRaisesRegex(ApiError, "exceeds"):
            client.get_json(RUN_PATH)
        self.assertEqual([], sleeps)
        with patch.object(api, "MAX_RESPONSE_BYTES", 8):
            client, _, _ = self.client([Response(body=b'{"key":"too long"}')])
            with self.assertRaisesRegex(ApiError, "exceeds 8 bytes"):
                client.get_json(RUN_PATH)

    def test_a_redirect_is_never_followed_by_an_api_request(self) -> None:
        client, opener, sleeps = self.client([http_error(302, header_values={"Location": "https://evil.test/"})])
        with self.assertRaises(ApiError) as caught:
            client.get_json(RUN_PATH)
        self.assertEqual((302, 1, []), (caught.exception.status, len(opener.requests), sleeps))

    def test_constructor_and_path_validation_fail_before_any_request(self) -> None:
        for options in ({"repository": "no-slash"}, {"repository": "../x"}, {"repository": "o/.."},
                        {"token": ""}, {"token": "bad token"}, {"token": "x" * 5000},
                        {"base_url": "http://api.github.com"}, {"base_url": "https://user:pw@api.github.com"},
                        {"base_url": "https://api.github.com?x=1"}, {"base_url": "https://api.github.com#f"},
                        {"base_url": "https://api.github.com:bad"}, {"base_url": "ftp://api.github.com"},
                        {"base_url": "https://[evil]"}, {"base_url": "https://[::1"},
                        {"writable": 1}, {"max_requests": 0}, {"max_requests": True}):
            with self.subTest(options=options), self.assertRaises(MbError):
                self.client([], **options)
        client, opener, _ = self.client([])
        for path in ("repos/x", "/repos/../x", "/repos/%2e%2E/x", "/repos/./x", "/repos//x", "/repos/x?y=1",
                     "/repos/a b", "/repos/x#f", "/repos/é", "", "/", "/repos/x/"):
            with self.subTest(path=path), self.assertRaisesRegex(MbError, "path"):
                client.get_json(path)
        for params in ({"Name": "x"}, {"name": True}, {"name": 1.5}, {"name": -1}, {"name": ""}, {"name": "a\nb"},
                       {"name": "x" * 1025}, ["name"]):
            with self.subTest(params=params), self.assertRaises(MbError):
                client.get_json(RUN_PATH, params=params)  # type: ignore[arg-type]
        self.assertEqual(([], 0), (opener.requests, client.request_count))


class RetryTests(ClientTestCase):
    def test_pages_api_retries_installation_rate_limit_then_returns_clean_json(self) -> None:
        rate_limit = http_error(403, b'{"message":"API rate limit exceeded for installation"}',
                                {"X-RateLimit-Remaining": "0"})
        client, opener, sleeps = self.client([rate_limit, Response(body=b'{"ok":true}')])
        self.assertEqual({"ok": True}, client.get_json("/repos/example/x"))
        self.assertEqual(1, len(sleeps))
        self.assertEqual(2, len(opener.requests))

    def test_retry_matrix(self) -> None:
        retried = {
            **{f"HTTP {code}": http_error(code) for code in sorted(api.RETRYABLE_HTTP_STATUSES)},
            "403 remaining 0": http_error(403, b'{"message":"x"}', {"X-RateLimit-Remaining": "0"}),
            "403 retry-after": http_error(403, b'{"message":"x"}', {"Retry-After": "1"}),
            "403 secondary body": http_error(403, b'{"message":"You have exceeded a secondary Rate Limit"}'),
            "URLError": urllib.error.URLError("temporary failure in name resolution"),
            "timeout": TimeoutError("timed out"),
            "reset": ConnectionResetError("reset by peer"),
            "incomplete": http.client.IncompleteRead(b"partial"),
            "disconnected": http.client.RemoteDisconnected("closed"),
            "tls": ssl.SSLError("record layer failure"),
        }
        for label, failure in retried.items():
            with self.subTest(retried=label):
                client, opener, sleeps = self.client([failure, Response(body=b"{}")])
                self.assertEqual({}, client.get_json(RUN_PATH))
                self.assertEqual((2, 1, 2), (len(opener.requests), len(sleeps), client.request_count))
        not_retried = {
            "400": (http_error(400), ApiError),
            "401": (http_error(401, b'{"message":"Bad credentials"}'), ApiError),
            "403 permission": (http_error(403, b'{"message":"Resource not accessible by integration"}'), ApiError),
            "404": (http_error(404, b'{"message":"Not Found"}'), ApiNotFound),
            "409": (http_error(409), ApiError),
            "410": (http_error(410), ApiError),
            "422": (http_error(422), ApiError),
            "501": (http_error(501), ApiError),
        }
        for label, (failure, error) in not_retried.items():
            with self.subTest(not_retried=label):
                client, opener, sleeps = self.client([failure])
                with self.assertRaises(error) as caught:
                    client.get_json(RUN_PATH)
                self.assertNotIsInstance(caught.exception, ApiRateLimited)
                self.assertEqual((1, []), (len(opener.requests), sleeps))
                self.assertEqual(int(label[:3]), caught.exception.status)
                self.assertEqual(("GET", RUN_PATH), (caught.exception.method, caught.exception.path))

    def test_four_attempts_with_exponential_backoff_and_path_jitter(self) -> None:
        client, opener, sleeps = self.client([http_error(503) for _ in range(4)])
        with self.assertRaises(ApiError) as caught:
            client.get_json(RUN_PATH)
        self.assertNotIsInstance(caught.exception, ApiRateLimited)
        self.assertEqual((503, 4, 4), (caught.exception.status, len(opener.requests), client.request_count))
        jitter = (sum(RUN_PATH.encode()) % 997) / 997
        self.assertEqual([1 + jitter, 2 + jitter, 4 + jitter], sleeps)
        other = "/repos/o/r/actions/runs/2"
        client, _, other_sleeps = self.client([http_error(503), http_error(503), Response()])
        client.get_json(other)
        self.assertEqual(2, len(other_sleeps))
        self.assertTrue(all(0 <= delay - 2 ** index < 1 for index, delay in enumerate(other_sleeps)))

    def test_transport_exhaustion_reports_status_zero(self) -> None:
        client, _, sleeps = self.client([urllib.error.URLError("down") for _ in range(4)])
        with self.assertRaises(ApiError) as caught:
            client.get_json(RUN_PATH)
        self.assertEqual((0, 3), (caught.exception.status, len(sleeps)))
        self.assertIn("transport", str(caught.exception))

    def test_rate_limit_exhaustion_is_reported_as_rate_limited(self) -> None:
        for code, header_values in ((429, {}), (403, {"X-RateLimit-Remaining": "0"})):
            with self.subTest(status=code):
                client, _, sleeps = self.client([http_error(code, b"{}", header_values) for _ in range(4)])
                with self.assertRaises(ApiRateLimited) as caught:
                    client.get_json(RUN_PATH)
                self.assertEqual((code, 3), (caught.exception.status, len(sleeps)))
                self.assertEqual("github-rate-limited", caught.exception.reason)

    def test_retry_after_is_honoured_and_bounded(self) -> None:
        client, _, sleeps = self.client([http_error(429, header_values={"Retry-After": "7"}), Response()])
        client.get_json(RUN_PATH)
        self.assertEqual([7.0], sleeps)
        client, opener, sleeps = self.client([http_error(429, header_values={"Retry-After": "31"})])
        with self.assertRaises(ApiRateLimited):
            client.get_json(RUN_PATH)
        self.assertEqual(([], 1), (sleeps, len(opener.requests)))
        client, _, sleeps = self.client([http_error(503, header_values={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
                                          Response()])
        client.get_json(RUN_PATH)
        self.assertLess(sleeps[0], 2)  # an HTTP-date is not a delay the client can trust; normal backoff

    def test_primary_rate_limit_reset_is_honoured_or_ends_the_budget(self) -> None:
        now = 1_900_000_000.0
        with patch.object(api.time, "time", return_value=now):
            soon = http_error(403, b"{}", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now) + 10)})
            client, _, sleeps = self.client([soon, Response()])
            client.get_json(RUN_PATH)
            self.assertEqual([11.0], sleeps)
            late = http_error(403, b"{}", {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now) + 3600)})
            client, opener, sleeps = self.client([late])
            with self.assertRaises(ApiRateLimited):
                client.get_json(RUN_PATH)
            self.assertEqual(([], 1), (sleeps, len(opener.requests)))

    def test_every_attempt_spends_the_request_budget(self) -> None:
        client, opener, _ = self.client([http_error(503), http_error(503), Response()], max_requests=2)
        with self.assertRaises(RequestBudgetExhausted):
            client.get_json(RUN_PATH)
        self.assertEqual((2, 2), (len(opener.requests), client.request_count))
        with self.assertRaises(RequestBudgetExhausted):
            client.get_json(RUN_PATH)
        self.assertEqual(2, len(opener.requests))

    def test_error_messages_are_bounded_single_lines(self) -> None:
        body = b'{"message":"line one\\nline two ' + b"x" * 5000 + b'"}'
        client, _, _ = self.client([http_error(400, body)])
        with self.assertRaises(ApiError) as caught:
            client.get_json(RUN_PATH)
        self.assertNotIn("\n", str(caught.exception))
        self.assertLess(len(str(caught.exception)), 400)


class PaginationTests(ClientTestCase):
    PATH = f"/repos/{REPOSITORY}/actions/artifacts"

    @staticmethod
    def page(count: int, total: int | None, start: int = 0, field: str | None = "artifacts") -> Response:
        rows = [{"id": start + index} for index in range(count)]
        body: Any = rows if field is None else {field: rows, **({} if total is None else {"total_count": total})}
        return Response(body=json.dumps(body).encode())

    def test_every_page_is_read_until_the_listing_ends(self) -> None:
        client, opener, _ = self.client([self.page(100, 230), self.page(100, 230, 100), self.page(30, 230, 200)])
        rows = client.paginate(self.PATH, field="artifacts", params={"name": "mb-promotion"}, max_items=512)
        self.assertEqual(list(range(230)), [row["id"] for row in rows])
        self.assertEqual([f"{BASE}{self.PATH}?name=mb-promotion&page={page}&per_page=100" for page in (1, 2, 3)],
                         [request.full_url for request in opener.requests])
        client, opener, _ = self.client([self.page(100, 100), self.page(0, 100, 100)])
        self.assertEqual(100, len(client.paginate(self.PATH, field="artifacts", max_items=512)))
        # Only a short page ends a listing: a full page completing total_count is confirmed by the next.
        self.assertEqual(2, len(opener.requests))
        client, _, _ = self.client([self.page(3, None, field=None)])
        self.assertEqual(3, len(client.paginate(self.PATH, field=None, max_items=5)))
        client, _, _ = self.client([self.page(0, 0)])
        self.assertEqual([], client.paginate(self.PATH, field="artifacts", max_items=5))

    def test_malformed_or_unbounded_listings_are_rejected_without_a_second_read(self) -> None:
        cases = {
            "total beyond bound": [self.page(10, 600)],
            "total grows beyond bound": [self.page(100, 150), self.page(50, 151, 100)],
            "rows beyond bound": [self.page(100, None), self.page(100, None, 100)],
            "total invalid": [Response(body=b'{"artifacts":[],"total_count":true}')],
            "field missing": [Response(body=b'{"total_count":0}')],
            "not an array": [Response(body=b'{"artifacts":{},"total_count":0}')],
            "row not object": [Response(body=b'{"artifacts":[1],"total_count":1}')],
            "page too long": [self.page(101, 101)],
        }
        for label, outcomes in cases.items():
            with self.subTest(label=label):
                client, opener, sleeps = self.client(outcomes)
                with self.assertRaisesRegex(ApiError, "listing") as caught:
                    client.paginate(self.PATH, field="artifacts", max_items=150)
                self.assertNotIsInstance(caught.exception, InconsistentListing)
                self.assertEqual((len(outcomes), []), (len(opener.requests), sleeps), "never read again")
        client, _, _ = self.client([self.page(3, None)])
        with self.assertRaisesRegex(ApiError, "is not an array"):
            client.paginate(self.PATH, field=None, max_items=5)
        for options in ({"params": {"page": 2}}, {"params": {"per_page": 10}}, {"max_items": 0},
                        {"field": "Bad-Field"}):
            with self.subTest(options=options), self.assertRaises(MbError):
                client.paginate(self.PATH, **{"field": "artifacts", "max_items": 5, **options})


class ConsistentListingTests(ClientTestCase):
    """GitHub's listings are eventually consistent while the listed run uploads artifacts (the
    canary's ``Finalize / Refresh evidence cache`` failed on ``total_count 6 disagrees with 5 listed
    rows`` while its sibling jobs uploaded caches). An inconsistent snapshot is discarded and read
    again from page 1 within the bounded attempts and the request budget; the last one fails closed,
    and an incomplete listing is never returned."""

    PATH = PaginationTests.PATH
    page = staticmethod(PaginationTests.page)

    def urls(self, opener: Opener) -> list[str]:
        return [request.full_url.rsplit("?", 1)[1] for request in opener.requests]

    def test_a_disagreeing_total_is_read_again_until_consistent(self) -> None:
        # The canary's failure: an upload in flight is counted before it is listed.
        client, opener, sleeps = self.client([self.page(5, 6), self.page(5, 6), self.page(6, 6)])
        rows = client.paginate(self.PATH, field="artifacts", max_items=512)
        self.assertEqual(list(range(6)), [row["id"] for row in rows])
        self.assertEqual(["page=1&per_page=100"] * 3, self.urls(opener))
        self.assertEqual(([2.0, 4.0], 3), (sleeps, client.request_count))

    def test_a_total_that_changes_between_pages_restarts_from_page_one(self) -> None:
        client, opener, sleeps = self.client([self.page(100, 150), self.page(50, 151, 100),
                                              self.page(100, 151), self.page(51, 151, 100)])
        rows = client.paginate(self.PATH, field="artifacts", params={"name": "mb-promotion"}, max_items=512)
        self.assertEqual(list(range(151)), [row["id"] for row in rows])
        self.assertEqual([f"name=mb-promotion&page={page}&per_page=100" for page in (1, 2, 1, 2)], self.urls(opener))
        self.assertEqual([2.0], sleeps)

    def test_a_row_repeated_across_pages_restarts_from_page_one(self) -> None:
        # An upload and a deletion between two pages keep the total but shift the rows.
        client, _, sleeps = self.client([self.page(100, 150), self.page(50, 150, 99),
                                         self.page(100, 150), self.page(50, 150, 100)])
        self.assertEqual(list(range(150)), [row["id"] for row in client.paginate(self.PATH, field="artifacts",
                                                                                  max_items=512)])
        self.assertEqual([2.0], sleeps)
        body = b'{"artifacts":[{"id":7},{"id":7}],"total_count":2}'
        client, _, _ = self.client([Response(body=body) for _ in range(limits.LISTING_READ_ATTEMPTS)])
        with self.assertRaisesRegex(InconsistentListing, "repeats the row id 7"):
            client.paginate(self.PATH, field="artifacts", max_items=5)

    def test_a_total_lagging_at_a_full_page_is_read_again(self) -> None:
        # 101 rows under a total_count of 100: the full first page reaches the total, and only the
        # confirming second page shows the row the lagging count hides.
        client, opener, sleeps = self.client([self.page(100, 100), self.page(1, 100, 100),
                                              self.page(100, 101), self.page(1, 101, 100)])
        rows = client.paginate(self.PATH, field="artifacts", max_items=512)
        self.assertEqual(list(range(101)), [row["id"] for row in rows])
        self.assertEqual(["page=1&per_page=100", "page=2&per_page=100"] * 2, self.urls(opener))
        self.assertEqual([2.0], sleeps)
        # More rows than a total within max_items is read again before the row bound rejects it.
        client, _, sleeps = self.client([page for _ in range(limits.LISTING_READ_ATTEMPTS)
                                         for page in (self.page(100, 100), self.page(1, 100, 100))])
        with self.assertRaisesRegex(InconsistentListing, "total_count 100 disagrees with 101 listed rows"):
            client.paginate(self.PATH, field="artifacts", max_items=100)
        self.assertEqual([2.0, 4.0, 8.0], sleeps)

    def test_the_last_of_the_bounded_reads_fails_closed(self) -> None:
        attempts = limits.LISTING_READ_ATTEMPTS
        client, opener, sleeps = self.client([self.page(5, 6) for _ in range(attempts)])
        with self.assertRaises(InconsistentListing) as caught:
            client.paginate(self.PATH, field="artifacts", max_items=512)
        self.assertEqual(f"GitHub API GET {self.PATH} listing total_count 6 disagrees with 5 listed rows "
                         f"(the last of {attempts} inconsistent reads)", str(caught.exception))
        self.assertEqual(("github-api", 200, "GET", self.PATH), (caught.exception.reason, caught.exception.status,
                                                                 caught.exception.method, caught.exception.path))
        self.assertEqual(attempts, len(opener.requests))
        self.assertEqual([api.listing_retry_delay(attempt) for attempt in range(attempts - 1)], sleeps)
        self.assertEqual([2.0, 4.0, 8.0], sleeps)

    def test_every_read_spends_the_request_budget(self) -> None:
        client, opener, sleeps = self.client([self.page(5, 6), self.page(5, 6)], max_requests=2)
        with self.assertRaises(RequestBudgetExhausted):
            client.paginate(self.PATH, field="artifacts", max_items=512)
        self.assertEqual((2, 2, [2.0, 4.0]), (client.request_count, len(opener.requests), sleeps))
        # A transport retry inside one read counts too, and does not count as a listing re-read.
        client, opener, sleeps = self.client([http_error(502), self.page(5, 6), self.page(6, 6)], max_requests=3)
        self.assertEqual(6, len(client.paginate(self.PATH, field="artifacts", max_items=512)))
        self.assertEqual(3, client.request_count)
        self.assertEqual(2, len(sleeps), "one transport backoff and one listing backoff")

    def test_the_backoff_is_bounded(self) -> None:
        delays = [api.listing_retry_delay(attempt) for attempt in range(6)]
        self.assertEqual([2.0, 4.0, 8.0, 8.0, 8.0, 8.0], delays)
        self.assertEqual(limits.MAX_LISTING_RETRY_DELAY_SECONDS, max(delays))
        self.assertLessEqual(limits.MAX_LISTING_RETRY_DELAY_SECONDS, api.MAX_RETRY_DELAY_SECONDS)
        self.assertEqual((4, 2.0, 8.0), (limits.LISTING_READ_ATTEMPTS, limits.LISTING_RETRY_DELAY_SECONDS,
                                         limits.MAX_LISTING_RETRY_DELAY_SECONDS))

    def test_read_listing_retries_only_inconsistency(self) -> None:
        client, _, sleeps = self.client([])
        outcomes: list[Any] = [InconsistentListing("skewed", status=200, method="GET", path=self.PATH), ["rows"]]

        def read() -> Any:
            outcome = outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.assertEqual(["rows"], client.read_listing(read))
        self.assertEqual([2.0], sleeps)
        for error in (ApiError("malformed", status=200, method="GET", path=self.PATH),
                      MbError("outside its filters"), RequestBudgetExhausted("spent")):
            with self.subTest(error=type(error).__name__):
                client, _, sleeps = self.client([])
                calls: list[int] = []

                def failing() -> Any:
                    calls.append(1)
                    raise error

                with self.assertRaises(type(error)):
                    client.read_listing(failing)
                self.assertEqual(([1], []), (calls, sleeps))


class ReadOnlyTests(ClientTestCase):
    def test_read_only_client_refuses_every_non_get_before_the_network(self) -> None:
        client, opener, _ = self.client([])
        self.assertFalse(client.writable)
        with self.assertRaises(ReadOnlyViolation):
            client.post_json(f"/repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches", {"ref": "master"})
        with self.assertRaises(ReadOnlyViolation):
            client.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        for method in ("POST", "DELETE", "PUT", "PATCH", "HEAD"):
            with self.subTest(method=method), self.assertRaises(ReadOnlyViolation):
                client._request(method, RUN_PATH, statuses=frozenset({200}))
        self.assertEqual(([], 0), (opener.requests, client.request_count))
        self.assertEqual("read-only", ReadOnlyViolation("x").reason)

    def test_writable_client_posts_canonical_json_and_deletes_exactly(self) -> None:
        client, opener, _ = self.client([Response(status=204, body=b""), Response(status=201, body=b'{"id":5}'),
                                         Response(status=204, body=b"")], writable=True)
        dispatch = f"/repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches"
        self.assertIsNone(client.post_json(dispatch, {"ref": "master", "inputs": {"b": "2", "a": "1"}}))
        self.assertEqual(b'{"inputs":{"a":"1","b":"2"},"ref":"master"}\n', opener.requests[0].data)
        self.assertEqual("POST", opener.requests[0].get_method())
        self.assertEqual("application/json", opener.requests[0].get_header("Content-type"))
        self.assertEqual({"id": 5}, client.post_json(dispatch, {}))
        self.assertIsNone(client.delete(f"/repos/{REPOSITORY}/actions/artifacts/1"))
        self.assertEqual("DELETE", opener.requests[2].get_method())
        client, _, _ = self.client([Response(status=200, body=b"{}")], writable=True)
        with self.assertRaisesRegex(ApiError, "HTTP 200"):
            client.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        client, _, sleeps = self.client([http_error(404)], writable=True)
        with self.assertRaises(ApiNotFound):
            client.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        self.assertEqual([], sleeps)
        with self.assertRaises(MbError):
            client.post_json(dispatch, ["not", "an", "object"])  # type: ignore[arg-type]


class DownloadTests(ClientTestCase):
    ZIP = f"/repos/{REPOSITORY}/actions/artifacts/7/zip"
    STORAGE = "https://pipelines.actions.githubusercontent.test/blob/7?sig=abc"

    def redirect(self, location: str | None = STORAGE, code: int = 302) -> urllib.error.HTTPError:
        return http_error(code, b"", {"Location": location} if location is not None else {})

    def test_follows_exactly_one_redirect_without_any_credential(self) -> None:
        client, opener, _ = self.client([self.redirect(), Response(body=b"PK-bytes")])
        self.assertEqual(b"PK-bytes", client.download(self.ZIP, max_bytes=100))
        api_request, storage_request = opener.requests
        self.assertEqual(BASE + self.ZIP, api_request.full_url)
        self.assertEqual("Bearer fixture-token", api_request.get_header("Authorization"))
        self.assertEqual(self.STORAGE, storage_request.full_url)
        self.assertEqual({"User-agent": api.USER_AGENT}, dict(storage_request.header_items()))
        self.assertEqual(2, client.request_count)

    def test_credentials_are_stripped_even_for_a_same_origin_redirect(self) -> None:
        client, opener, _ = self.client([self.redirect(BASE + "/storage/7"), Response(body=b"zip")])
        client.download(self.ZIP, max_bytes=10)
        self.assertIsNone(opener.requests[1].get_header("Authorization"))
        self.assertIsNone(opener.requests[1].get_header("X-github-api-version"))

    def test_unsafe_or_missing_redirects_are_refused(self) -> None:
        for location in ("http://storage.test/blob", "https://user:secret@storage.test/blob",
                         "https://token@storage.test/blob", "https://storage.test/blob#fragment", "/relative/blob",
                         "https://storage.test:bad/", "https://", "", None, "https://storage.test/é",
                         "https://[evil]/blob", "https://[::1/blob", "https://[::1]:bad/blob",
                         "https://storage.test/" + "x" * 9000):
            with self.subTest(location=location):
                client, opener, sleeps = self.client([self.redirect(location)])
                with self.assertRaisesRegex(ApiError, "unsafe location"):
                    client.download(self.ZIP, max_bytes=10)
                self.assertEqual((1, []), (len(opener.requests), sleeps))

    def test_a_second_redirect_or_a_direct_body_is_refused(self) -> None:
        client, opener, _ = self.client([self.redirect(), self.redirect("https://third.test/blob")])
        with self.assertRaises(ApiError) as caught:
            client.download(self.ZIP, max_bytes=10)
        self.assertEqual((302, 2), (caught.exception.status, len(opener.requests)))
        client, _, _ = self.client([Response(body=b"zip bytes")])
        with self.assertRaisesRegex(ApiError, "instead of a redirect"):
            client.download(self.ZIP, max_bytes=10)
        client, _, _ = self.client([Response(body=b"")])
        with self.assertRaisesRegex(ApiError, "instead of a redirect"):
            client.download(self.ZIP, max_bytes=10)

    def test_download_is_bounded_and_retried_as_one_exchange(self) -> None:
        client, _, sleeps = self.client([self.redirect(), Response(body=b"x" * 11)])
        with self.assertRaisesRegex(ApiError, "10-byte bound"):
            client.download(self.ZIP, max_bytes=10)
        self.assertEqual([], sleeps)
        declared = Response(body=b"x", header_values={"Content-Length": "11"})
        client, _, _ = self.client([self.redirect(), declared])
        with self.assertRaisesRegex(ApiError, "10-byte bound"):
            client.download(self.ZIP, max_bytes=10)
        client, opener, sleeps = self.client([self.redirect(), http_error(503, url=self.STORAGE), self.redirect(),
                                              Response(body=b"zip")])
        self.assertEqual(b"zip", client.download(self.ZIP, max_bytes=10))
        self.assertEqual((4, 1), (len(opener.requests), len(sleeps)))
        self.assertIsNone(opener.requests[3].get_header("Authorization"))
        client, _, _ = self.client([http_error(404)])
        with self.assertRaises(ApiNotFound):
            client.download(self.ZIP, max_bytes=10)
        for bound in (0, -1, True, api.MAX_DOWNLOAD_BYTES + 1):
            with self.subTest(bound=bound), self.assertRaises(MbError):
                client.download(self.ZIP, max_bytes=bound)


class ReleaseDownloadTests(ClientTestCase):
    STORAGE = DownloadTests.STORAGE
    redirect = DownloadTests.redirect

    def test_release_direct_and_redirected_bytes(self) -> None:
        for responses in ([Response(body=b"archive")], [self.redirect(), Response(body=b"archive")]):
            with self.subTest(redirected=len(responses) == 2):
                client, opener, _ = self.client(responses)
                self.assertEqual(b"archive", client.download_release_asset(REPOSITORY, 7, max_bytes=7))
                self.assertEqual(BASE + f"/repos/{REPOSITORY}/releases/assets/7", opener.requests[0].full_url)
                self.assertEqual("application/octet-stream", opener.requests[0].get_header("Accept"))
                self.assertEqual("Bearer fixture-token", opener.requests[0].get_header("Authorization"))
                self.assertEqual(len(responses), client.request_count)
                if len(responses) == 2:
                    self.assertEqual({"User-agent": api.USER_AGENT}, dict(opener.requests[1].header_items()))

    def test_release_rejects_oversize_and_redirect_chains(self) -> None:
        for responses in ([Response(body=b"12345678")],
                          [Response(body=b"x", header_values={"Content-Length": "8"})],
                          [self.redirect(), Response(body=b"12345678")],
                          [self.redirect(), self.redirect()], [self.redirect("http://unsafe.test")],
                          [self.redirect(code=301)]):
            with self.subTest(responses=responses):
                client, _, sleeps = self.client(responses)
                with self.assertRaises(ApiError):
                    client.download_release_asset(REPOSITORY, 7, max_bytes=7)
                self.assertEqual([], sleeps)

    def test_release_validation_precedes_network(self) -> None:
        client, opener, _ = self.client([])
        for asset_id in (0, -1, True, "7", 2**63):
            with self.subTest(asset_id=asset_id), self.assertRaises(MbError):
                client.download_release_asset(REPOSITORY, asset_id, max_bytes=7)
        for repository in ("bad", "owner/repo/extra", "../repo"):
            with self.subTest(repository=repository), self.assertRaises(MbError):
                client.download_release_asset(repository, 7, max_bytes=7)
        for bound in (0, True, api.MAX_DOWNLOAD_BYTES + 1):
            with self.subTest(bound=bound), self.assertRaises(MbError):
                client.download_release_asset(REPOSITORY, 7, max_bytes=bound)
        self.assertEqual([], opener.requests)

    def test_release_retries_the_whole_exchange_and_spends_budget(self) -> None:
        client, opener, sleeps = self.client([self.redirect(), http_error(503), Response(body=b"ok")])
        self.assertEqual(b"ok", client.download_release_asset(REPOSITORY, 7, max_bytes=7))
        self.assertEqual((3, 1), (len(opener.requests), len(sleeps)))


class RateLimitAndEnvironmentTests(ClientTestCase):
    CORE = {"limit": 5000, "used": 12, "remaining": 4988, "reset": 1_900_000_000}

    def test_rate_limit_snapshot_projects_numeric_core_counters_only(self) -> None:
        body = ('{"resources":{"core":{"limit":5000,"used":12,"remaining":4988,"reset":1900000000,"resource":"core"},'
                '"search":{"limit":30}},"rate":{"limit":5000}}').encode()
        client, opener, _ = self.client([Response(body=body)])
        self.assertEqual(self.CORE, client.rate_limit_snapshot())
        self.assertEqual(BASE + "/rate_limit", opener.requests[0].full_url)

    def test_invalid_counters_are_rejected(self) -> None:
        for core in ({**self.CORE, "limit": 0}, {**self.CORE, "reset": 0}, {**self.CORE, "remaining": 5001},
                     {**self.CORE, "used": -1}, {**self.CORE, "used": True}, {**self.CORE, "used": 1.5},
                     {key: value for key, value in self.CORE.items() if key != "reset"}, None):
            with self.subTest(core=core):
                self.assertRaises(ApiError, api.rate_limit_counters, {"resources": {"core": core}})
        for payload in (None, [], {"resources": []}, {"resources": {}}):
            with self.subTest(payload=payload):
                self.assertRaises(ApiError, api.rate_limit_counters, payload)

    def test_from_environment_prefers_gh_token_and_requires_repository_and_token(self) -> None:
        environ = {"GITHUB_REPOSITORY": REPOSITORY, "GH_TOKEN": "gh", "GITHUB_TOKEN": "github",
                   "GITHUB_API_URL": "https://ghe.example.test/api/v3"}
        client = api.from_environment(environ, writable=True, max_requests=160)
        self.assertEqual((REPOSITORY, True), (client.repository, client.writable))
        self.assertEqual("Bearer gh", client._api_headers()["Authorization"])
        self.assertEqual("https://ghe.example.test/api/v3", client._base_url)
        self.assertEqual(160, client._max_requests)
        fallback = api.from_environment({"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_TOKEN": "github"})
        self.assertEqual(("Bearer github", api.DEFAULT_BASE_URL, False),
                         (fallback._api_headers()["Authorization"], fallback._base_url, fallback.writable))
        for missing in ({"GITHUB_TOKEN": "x"}, {"GITHUB_REPOSITORY": REPOSITORY}, {"GITHUB_REPOSITORY": REPOSITORY,
                                                                                   "GH_TOKEN": ""}):
            with self.subTest(environ=missing), self.assertRaisesRegex(MbError, "required"):
                api.from_environment(missing)
        with self.assertRaises(MbError):
            api.from_environment({**environ, "GITHUB_API_URL": "http://insecure.test"})

    def test_error_classes_are_kit_rejections_with_reasons(self) -> None:
        self.assertEqual("github-api", ApiError("x", status=500, method="GET", path="/").reason)
        self.assertEqual("github-not-found", ApiNotFound("x", status=404, method="GET", path="/").reason)
        self.assertTrue(issubclass(ApiRateLimited, ApiError))
        self.assertEqual("request-budget", RequestBudgetExhausted("x").reason)
        self.assertTrue(all(issubclass(error, MbError) for error in (ApiError, ReadOnlyViolation,
                                                                       RequestBudgetExhausted)))


if __name__ == "__main__":
    unittest.main()
