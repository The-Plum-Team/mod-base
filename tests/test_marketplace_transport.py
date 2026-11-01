"""``marketplace.transport``: the one HTTPS client of the release uploads.

Every request goes through the real request construction, retry policy and body checks; only the
opener is replaced (``marketplace.fake.FakeNetwork``). Ports Quick Skin's ``request_json`` /
``request_bytes`` retry cases (``scripts/release/tests/test_reconcile_publication.py``).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from mod_base.errors import EXIT_REJECTED, EXIT_UNAVAILABLE, MbError
from mod_base.marketplace import limits, transport
from mod_base.marketplace.fake import FakeHttp, FakeNetwork, Reply, json_reply, parse_multipart
from mod_base.marketplace.transport import (
    HttpsTransport,
    MarketplaceError,
    MarketplaceNotFound,
    MarketplaceOutcomeUnknown,
    MarketplaceUnavailable,
    PublicationPending,
    Reconciliation,
)

ORIGIN = "https://api.market.test"
STORAGE = "https://storage.market.test"
TOKEN = "secret-token-0123456789"


class Echo:
    """A service answering ``{"ok": true, ...}`` for every request it is given, counting them."""

    def __init__(self) -> None:
        self.seen: list[FakeHttp] = []
        self.reply: Reply | None = None

    def __call__(self, request: FakeHttp) -> Reply:
        self.seen.append(request)
        if self.reply is not None:
            return self.reply
        return json_reply(200, {"ok": True, "method": request.method, "path": request.path})


class TransportCase(unittest.TestCase):
    def setUp(self) -> None:
        self.network = FakeNetwork()
        self.service = Echo()
        self.network.add_service(ORIGIN, self.service)
        self.network.add_secret("market", TOKEN)
        self.sleeps: list[float] = []
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def transport(self, **options: Any) -> HttpsTransport:
        options.setdefault("headers", {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"})
        options.setdefault("max_requests", 50)
        return HttpsTransport(ORIGIN, opener=self.network, sleep=self.sleeps.append, **options)

    def file(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path


class ConstructionTest(TransportCase):
    def test_origin_must_be_exactly_an_https_host(self) -> None:
        for origin in ("http://api.market.test", "https://api.market.test/", "https://api.market.test/v2",
                       "https://api.market.test:8443", "https://user@api.market.test", "https://API.market.test",
                       "https://api.market.test?x=1", "https://localhost", "https://api.market.test#f", "", None):
            with self.subTest(origin=origin), self.assertRaises(MarketplaceError):
                HttpsTransport(origin, max_requests=1, opener=self.network)  # type: ignore[arg-type]
        self.assertEqual(HttpsTransport("https://api.market.test:443", max_requests=1).origin,
                         "https://api.market.test:443")

    def test_headers_are_validated_and_reserved_names_refused(self) -> None:
        for headers in ({"Host": "evil.test"}, {"Content-Type": "text/plain"}, {"X-Token": "a\r\nInjected: 1"},
                        {"X-Token": ""}, {"Bad Name": "x"}, {"X-Token": "é"}, {"a": "1", "A": "2"}):
            with self.subTest(headers=headers), self.assertRaises(MarketplaceError):
                HttpsTransport(ORIGIN, headers=headers, max_requests=1, opener=self.network)

    def test_budget_and_flags_are_validated(self) -> None:
        for value in (0, -1, True, limits.MAX_MARKETPLACE_REQUESTS + 1, None):
            with self.subTest(max_requests=value), self.assertRaises(MarketplaceError):
                HttpsTransport(ORIGIN, max_requests=value, opener=self.network)  # type: ignore[arg-type]
        with self.assertRaises(MarketplaceError):
            HttpsTransport(ORIGIN, max_requests=1, writable="yes", opener=self.network)  # type: ignore[arg-type]

    def test_repr_never_shows_a_token(self) -> None:
        client = self.transport()
        self.assertNotIn(TOKEN, repr(client))
        self.assertNotIn(TOKEN, str(vars(client).get("_origin")))

    def test_default_opener_ignores_proxies_refuses_redirects_and_verifies_tls(self) -> None:
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:3128", "https_proxy": "http://p:1"}):
            opener = transport.build_opener()
        self.assertEqual([], [handler for handler in opener.handlers
                              if isinstance(handler, urllib.request.ProxyHandler) and handler.proxies])
        redirects = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPRedirectHandler)]
        self.assertEqual([transport._NoRedirect], [type(handler) for handler in redirects])
        request = urllib.request.Request(ORIGIN + "/x", headers={"Authorization": "Bearer secret"})
        self.assertIsNone(redirects[0].redirect_request(request, None, 302, "Found", {}, "https://evil.test/"))
        https = [handler for handler in opener.handlers if isinstance(handler, urllib.request.HTTPSHandler)]
        self.assertEqual(1, len(https))
        context = https[0]._context
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_errors_map_to_their_exit_codes(self) -> None:
        self.assertEqual(MarketplaceError("x").exit_code, EXIT_REJECTED)
        self.assertTrue(issubclass(MarketplaceNotFound, MarketplaceError))
        self.assertEqual(MarketplaceUnavailable("x").exit_code, EXIT_UNAVAILABLE)
        self.assertEqual(MarketplaceOutcomeUnknown("x").exit_code, EXIT_UNAVAILABLE)
        self.assertEqual(PublicationPending("x").exit_code, EXIT_UNAVAILABLE)
        for error in (MarketplaceError, MarketplaceUnavailable, MarketplaceOutcomeUnknown, PublicationPending):
            self.assertTrue(issubclass(error, MbError))

    def test_reconciliation_names_a_remote_exactly_when_nothing_is_published(self) -> None:
        self.assertEqual(Reconciliation(True, None).remote_id, None)
        self.assertEqual(Reconciliation(False, "42").remote_id, "42")
        for publish, remote in ((True, "42"), (False, None), ("yes", None), (False, "")):
            with self.subTest(publish=publish, remote=remote), self.assertRaises(MarketplaceError):
                Reconciliation(publish, remote)  # type: ignore[arg-type]


class OriginTest(TransportCase):
    def test_paths_cannot_leave_the_origin_or_traverse(self) -> None:
        client = self.transport()
        for path in ("relative", "//evil.test/x", "/a/../b", "/a/%2e%2e/b", "/a/%2Fb", "/a/%5Cb", "/a?x=1",
                     "/a#f", "/a b", "/a/%00", "/", "", "/a//b", "https://evil.test/a", "/" + "a" * 3000):
            with self.subTest(path=path), self.assertRaises(MarketplaceError):
                client.get_json(path)
        self.assertEqual([], self.network.requests)

    def test_query_parameters_are_validated_and_encoded(self) -> None:
        client = self.transport()
        client.get_json("/v1/files", {"pageIndex": 2, "name": "Quick Skin & co/1"})
        self.assertEqual(self.network.requests[-1].query, (("name", "Quick Skin & co/1"), ("pageIndex", "2")))
        for query in ({"bad name": "x"}, {"flag": True}, {"n": -1}, {"v": "a\nb"}, {"v": ""}, {"v": 1.5}):
            with self.subTest(query=query), self.assertRaises(MarketplaceError):
                client.get_json("/v1/files", query)  # type: ignore[arg-type]

    def test_the_token_travels_only_to_the_origin(self) -> None:
        client = self.transport()
        self.assertEqual(client.get_json("/v1/x"), {"method": "GET", "ok": True, "path": "/v1/x"})
        [request] = self.network.requests
        self.assertEqual(request.origin, ORIGIN)
        self.assertEqual(request.credentials, frozenset({"market"}))
        self.assertEqual(request.header("authorization"), "Bearer <secret:market>")
        self.assertEqual(request.header("user-agent"), transport.USER_AGENT)
        self.assertEqual(self.network.timeouts, [limits.MARKETPLACE_REQUEST_TIMEOUT_SECONDS])

    def test_a_redirect_is_refused_and_never_followed(self) -> None:
        other = Echo()
        self.network.add_service("https://evil.test", other)
        client = self.transport()
        for status in sorted(transport.REDIRECT_STATUSES):
            self.network.fail("GET", ORIGIN + "/v1/x", status=status,
                              headers={"Location": "https://evil.test/steal?token=" + TOKEN})
            with self.subTest(status=status), self.assertRaisesRegex(MarketplaceError, "refused a redirect") as caught:
                client.get_json("/v1/x")
            self.assertNotIn("evil", str(caught.exception))
        self.assertEqual([], other.seen)
        self.assertEqual(len(self.network.requests), len(transport.REDIRECT_STATUSES))

    def test_read_only_transport_refuses_writes_before_the_network(self) -> None:
        client = self.transport()
        path = self.file("a.jar", b"jar")
        calls = [lambda: client.post_json("/v1/x", {}, replay=True), lambda: client.patch_json("/v1/x", {}),
                 lambda: client.post_multipart("/v1/x", [("a", "b")], [], replay=False),
                 lambda: client.post_bytes("/v1/x", path, "application/java-archive", None, replay=False)]
        for call in calls:
            with self.assertRaises(MarketplaceError) as caught:
                call()
            self.assertEqual(caught.exception.reason, "read-only")
        self.assertEqual([], self.network.requests)


class RetryTest(TransportCase):
    def test_transient_failures_retry_until_success(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=502)
        self.network.fail("GET", ORIGIN + "/v1/x", exception=TimeoutError("timed out"))
        self.network.fail("GET", ORIGIN + "/v1/x", exception=ConnectionResetError("reset"))
        client = self.transport()
        self.assertTrue(client.get_json("/v1/x")["ok"])
        self.assertEqual(client.request_count, 4)
        self.assertEqual(len(self.sleeps), 3)
        self.assertEqual([int(delay) for delay in self.sleeps], [1, 2, 4])

    def test_every_retryable_status_is_retried(self) -> None:
        for status in sorted(transport.RETRYABLE_HTTP_STATUSES):
            self.network.fail("GET", ORIGIN + "/v1/x", status=status)
            with self.subTest(status=status):
                self.assertTrue(self.transport().get_json("/v1/x")["ok"])

    def test_persistent_transient_failure_stays_bounded(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=503, times=99)
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceUnavailable, f"after {limits.MARKETPLACE_REQUEST_ATTEMPTS} attempts"):
            client.get_json("/v1/x")
        self.assertEqual(client.request_count, limits.MARKETPLACE_REQUEST_ATTEMPTS)
        self.assertEqual(len(self.sleeps), limits.MARKETPLACE_REQUEST_ATTEMPTS - 1)
        self.assertTrue(all(delay <= limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS for delay in self.sleeps))

    def test_client_rejections_never_retry(self) -> None:
        for status in (400, 401, 403, 409, 410, 422, 501):
            self.network.fail("GET", ORIGIN + "/v1/x", status=status)
            client = self.transport()
            with self.subTest(status=status), self.assertRaisesRegex(MarketplaceError, f"HTTP {status}") as caught:
                client.get_json("/v1/x")
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(client.request_count, 1)
        self.assertEqual(self.sleeps, [])

    def test_not_found_is_its_own_error_and_never_retried(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=404)
        client = self.transport()
        with self.assertRaises(MarketplaceNotFound):
            client.get_json("/v1/x")
        self.assertEqual(client.request_count, 1)

    def test_retry_after_is_honoured_within_the_bound(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=429, headers={"Retry-After": "7"})
        self.assertTrue(self.transport().get_json("/v1/x")["ok"])
        self.assertEqual(self.sleeps, [7.0])

    def test_a_retry_after_beyond_the_bound_ends_the_request(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=429,
                          headers={"Retry-After": str(int(limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS) + 1)})
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceUnavailable, "beyond the bound"):
            client.get_json("/v1/x")
        self.assertEqual((client.request_count, self.sleeps), (1, []))

    def test_the_total_wait_of_one_transport_is_bounded(self) -> None:
        delay = int(limits.MAX_MARKETPLACE_RETRY_DELAY_SECONDS)
        allowed = int(limits.MAX_MARKETPLACE_RETRY_WAIT_SECONDS // delay)
        self.network.fail("GET", ORIGIN + "/v1/x", status=429, headers={"Retry-After": str(delay)}, times=999)
        client = self.transport()
        for _ in range(allowed // (limits.MARKETPLACE_REQUEST_ATTEMPTS - 1)):
            with self.assertRaises(MarketplaceUnavailable):
                client.get_json("/v1/x")
        with self.assertRaisesRegex(MarketplaceUnavailable, "beyond the bound"):
            client.get_json("/v1/x")
        self.assertLessEqual(sum(self.sleeps), limits.MAX_MARKETPLACE_RETRY_WAIT_SECONDS)

    def test_threads_sharing_a_transport_never_exceed_its_budget(self) -> None:
        import threading

        client = self.transport(max_requests=40)
        refused: list[str] = []

        def worker() -> None:
            for _ in range(10):
                try:
                    client.get_json("/v1/x")
                except MarketplaceError as exc:
                    refused.append(exc.reason)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((client.request_count, len(self.network.requests)), (40, 40))
        self.assertEqual(refused, ["request-budget"] * 40)

    def test_every_attempt_counts_against_the_budget(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", status=502, times=2)
        client = self.transport(max_requests=2)
        with self.assertRaises(MarketplaceError) as caught:
            client.get_json("/v1/x")
        self.assertEqual(caught.exception.reason, "request-budget")
        self.assertEqual(len(self.network.requests), 2)


class ReplayTest(TransportCase):
    def test_a_non_replayable_post_is_never_sent_again_after_a_5xx(self) -> None:
        for status in (500, 502, 503, 504):
            self.network.fail("POST", ORIGIN + "/v1/upload", status=status, after=True)
            client = self.transport(writable=True)
            with self.subTest(status=status), self.assertRaises(MarketplaceOutcomeUnknown):
                client.post_json("/v1/upload", {"a": 1}, replay=False)
            self.assertEqual(client.request_count, 1)
        # The service did act on each of them: exactly why the answer must not be retried.
        self.assertEqual(len([seen for seen in self.service.seen if seen.method == "POST"]), 4)
        self.assertEqual(self.sleeps, [])

    def test_a_non_replayable_post_is_never_sent_again_after_a_transport_error(self) -> None:
        self.network.fail("POST", ORIGIN + "/v1/upload", exception=ConnectionResetError("reset"), after=True)
        client = self.transport(writable=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            client.post_multipart("/v1/upload", [("data", "{}")], [("file", "a.jar", self.file("a.jar", b"x"))],
                                  replay=False)
        self.assertEqual(client.request_count, 1)

    def test_a_non_replayable_post_retries_a_rate_limit(self) -> None:
        self.network.fail("POST", ORIGIN + "/v1/upload", status=429, headers={"Retry-After": "2"})
        client = self.transport(writable=True)
        self.assertTrue(client.post_json("/v1/upload", {"a": 1}, replay=False)["ok"])
        self.assertEqual((client.request_count, self.sleeps), (2, [2.0]))
        self.assertEqual(len(self.service.seen), 1)

    def test_a_rate_limited_403_is_retried_even_for_a_non_replayable_post(self) -> None:
        for headers, body in (({"X-RateLimit-Remaining": "0"}, b"{}"), ({"Retry-After": "3"}, b"{}"),
                              ({}, b'{"message":"You have exceeded a secondary rate limit"}')):
            self.network.fail("POST", ORIGIN + "/v1/upload", status=403, headers=headers, body=body)
            client = self.transport(writable=True)
            with self.subTest(headers=headers):
                self.assertTrue(client.post_json("/v1/upload", {"a": 1}, replay=False)["ok"])
            self.assertEqual(client.request_count, 2)
        self.network.fail("POST", ORIGIN + "/v1/upload", status=403, body=b'{"message":"Resource not accessible"}')
        client = self.transport(writable=True)
        with self.assertRaisesRegex(MarketplaceError, "HTTP 403"):
            client.post_json("/v1/upload", {"a": 1}, replay=False)
        self.assertEqual(client.request_count, 1)

    def test_an_unreadable_success_of_a_non_replayable_post_is_an_unknown_outcome(self) -> None:
        self.service.reply = Reply(200, b"<html>gateway</html>")
        client = self.transport(writable=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            client.post_json("/v1/upload", {"a": 1}, replay=False)
        self.service.reply = Reply(202, b"{}")
        with self.assertRaises(MarketplaceOutcomeUnknown):
            client.post_json("/v1/upload", {"a": 1}, replay=False)
        self.assertEqual(client.request_count, 2)

    def test_a_replayable_post_and_a_patch_retry_a_5xx(self) -> None:
        self.network.fail("POST", ORIGIN + "/v1/refs", status=502)
        self.network.fail("PATCH", ORIGIN + "/v1/release", status=503)
        client = self.transport(writable=True)
        self.assertTrue(client.post_json("/v1/refs", {"a": 1}, replay=True)["ok"])
        self.assertTrue(client.patch_json("/v1/release", {"a": 1})["ok"])
        self.assertEqual(client.request_count, 4)

    def test_a_definitive_refusal_of_a_post_is_a_marketplace_error(self) -> None:
        self.network.fail("POST", ORIGIN + "/v1/upload", status=400, body=b'{"description":"bad loader"}')
        with self.assertRaisesRegex(MarketplaceError, "HTTP 400: bad loader"):
            self.transport(writable=True).post_json("/v1/upload", {"a": 1}, replay=False)

    def test_json_bodies_are_canonical_and_bounded(self) -> None:
        client = self.transport(writable=True)
        client.post_json("/v1/x", {"b": 1, "a": "é"}, replay=True)
        self.assertEqual(self.service.seen[-1].body, '{"a":"é","b":1}\n'.encode("utf-8"))
        self.assertEqual(self.service.seen[-1].headers["content-type"], "application/json")
        for body in ([1], {"x": float("nan")}, {"x": "a" * limits.MAX_MARKETPLACE_REQUEST_JSON_BYTES}):
            with self.subTest(body=str(body)[:30]), self.assertRaises(MarketplaceError):
                client.post_json("/v1/x", body, replay=True)  # type: ignore[arg-type]
        with self.assertRaises(MarketplaceError):
            client.post_json("/v1/x", {}, replay="no")  # type: ignore[arg-type]


class BodyTest(TransportCase):
    def test_a_declared_oversized_json_answer_is_refused_unread(self) -> None:
        self.network.tamper("GET", ORIGIN + "/v1/x", content_length=limits.MAX_MARKETPLACE_JSON_BYTES + 1)
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceError, "bound"):
            client.get_json("/v1/x")
        self.assertEqual((self.network.delivered, client.request_count), (0, 1))

    def test_an_undeclared_oversized_json_answer_stops_at_the_bound(self) -> None:
        self.network.tamper("GET", ORIGIN + "/v1/x", omit_length=True, extra_bytes=10_000_000)
        with patch.object(limits, "MAX_MARKETPLACE_JSON_BYTES", 4096):
            with self.assertRaisesRegex(MarketplaceError, "4096-byte bound"):
                self.transport().get_json("/v1/x")
        self.assertLessEqual(self.network.delivered, 4097)

    def test_invalid_or_duplicate_key_json_is_refused(self) -> None:
        for body in (b"not json", b'{"a":1,"a":2}', b'{"a":NaN}', b"\xef\xbb\xbf{}", b""):
            self.service.reply = Reply(200, body)
            with self.subTest(body=body), self.assertRaises(MarketplaceError):
                self.transport().get_json("/v1/x")

    def test_error_bodies_become_one_bounded_line_without_the_token(self) -> None:
        echo = json.dumps({"description": f"token Bearer {TOKEN} invalid\n::error::forged\n" + "x" * 5000}).encode()
        self.network.fail("GET", ORIGIN + "/v1/x", status=401, body=echo)
        with self.assertRaises(MarketplaceError) as caught:
            self.transport().get_json("/v1/x")
        message = str(caught.exception)
        self.assertNotIn(TOKEN, message)
        self.assertNotIn("\n", message)
        self.assertIn("<redacted>", message)
        self.assertLess(len(message), limits.MAX_MARKETPLACE_ERROR_DETAIL_CHARS + 120)

    def test_transport_errors_never_echo_the_token(self) -> None:
        self.network.fail("GET", ORIGIN + "/v1/x", exception=OSError(f"failed with {TOKEN}"), times=9)
        with self.assertRaises(MarketplaceUnavailable) as caught:
            self.transport().get_json("/v1/x")
        self.assertNotIn(TOKEN, str(caught.exception))


class DownloadTest(TransportCase):
    PAYLOAD = b"a" * 64

    def setUp(self) -> None:
        super().setUp()
        self.service.reply = Reply(200, self.PAYLOAD, {"Content-Type": "application/java-archive"})

    def test_an_exact_download(self) -> None:
        self.assertEqual(self.transport().get_bytes("/files/0/42/a.jar", limit=64), self.PAYLOAD)
        self.assertEqual(self.network.requests[0].header("accept"), "*/*")

    def test_truncated_transfer_retries_instead_of_reporting_divergence(self) -> None:
        self.network.tamper("GET", ORIGIN + "/files", truncate_to=10)
        client = self.transport()
        self.assertEqual(client.get_bytes("/files/0/42/a.jar", limit=64), self.PAYLOAD)
        self.assertEqual((client.request_count, len(self.sleeps)), (2, 1))

    def test_a_short_body_without_an_error_is_retried(self) -> None:
        self.network.tamper("GET", ORIGIN + "/files", omit_length=True, times=99)
        self.service.reply = Reply(200, self.PAYLOAD[:10])
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceUnavailable, "ended short"):
            client.get_bytes("/files/0/42/a.jar", limit=64)
        self.assertEqual(client.request_count, limits.MARKETPLACE_REQUEST_ATTEMPTS)

    def test_mid_body_connection_reset_is_transient(self) -> None:
        self.network.fail("GET", ORIGIN + "/files", exception=ConnectionResetError("peer reset the connection"))
        self.network.fail("GET", ORIGIN + "/files", exception=http.client.IncompleteRead(b"x", 63))
        client = self.transport()
        self.assertEqual(client.get_bytes("/files/0/42/a.jar", limit=64), self.PAYLOAD)
        self.assertEqual(client.request_count, 3)

    def test_an_oversized_body_is_definitive_and_never_retried(self) -> None:
        self.network.tamper("GET", ORIGIN + "/files", omit_length=True, extra_bytes=50_000_000)
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceError, "bound"):
            client.get_bytes("/files/0/42/a.jar", limit=10)
        self.assertEqual(client.request_count, 1)
        self.assertLessEqual(self.network.delivered, 11)

    def test_a_declared_other_length_is_definitive(self) -> None:
        for declared in (63, 65, 10_000_000_000):
            self.network.tamper("GET", ORIGIN + "/files", content_length=declared)
            client = self.transport()
            with self.subTest(declared=declared), self.assertRaises(MarketplaceError):
                client.get_bytes("/files/0/42/a.jar", limit=64)
            self.assertEqual(client.request_count, 1)
        self.assertEqual(self.network.delivered, 0)

    def test_cdn_client_rejection_never_retries(self) -> None:
        self.network.fail("GET", ORIGIN + "/files", status=403)
        client = self.transport()
        with self.assertRaisesRegex(MarketplaceError, "HTTP 403"):
            client.get_bytes("/files/0/42/a.jar", limit=10)
        self.assertEqual((client.request_count, self.sleeps), (1, []))

    def test_limits_are_validated(self) -> None:
        for limit in (0, -1, True, limits.MAX_MARKETPLACE_FILE_BYTES + 1):
            with self.subTest(limit=limit), self.assertRaises(MarketplaceError):
                self.transport().get_bytes("/files/a", limit=limit)  # type: ignore[arg-type]


class RedirectedDownloadTest(TransportCase):
    PAYLOAD = b"asset-bytes" * 10

    def setUp(self) -> None:
        super().setUp()
        self.storage = Echo()
        self.storage.reply = Reply(200, self.PAYLOAD)
        self.network.add_service(STORAGE, self.storage)
        self.service.reply = Reply(302, b"", {"Location": STORAGE + "/blob/1?sig=abc"})

    def test_one_redirect_is_followed_without_credentials(self) -> None:
        client = self.transport()
        self.assertEqual(client.download_redirected("/assets/1", limit=len(self.PAYLOAD)), self.PAYLOAD)
        first, second = self.network.requests
        self.assertEqual((first.origin, first.credentials), (ORIGIN, frozenset({"market"})))
        self.assertEqual(first.header("accept"), "application/octet-stream")
        self.assertEqual((second.origin, second.credentials), (STORAGE, frozenset()))
        self.assertEqual(set(dict(second.headers)), {"accept", "user-agent"})
        self.assertEqual(second.query, (("sig", "abc"),))
        self.assertEqual(client.request_count, 2)

    def test_a_direct_answer_is_accepted(self) -> None:
        self.service.reply = Reply(200, self.PAYLOAD)
        self.assertEqual(self.transport().download_redirected("/assets/1", limit=len(self.PAYLOAD)), self.PAYLOAD)
        self.assertEqual(len(self.network.requests), 1)

    def test_unsafe_redirect_targets_are_refused(self) -> None:
        for location in ("http://storage.market.test/blob", "https://user:pw@storage.market.test/blob",
                         "https://storage.market.test/blob#frag", "/relative", "", "https://storage.market.test:8443/b",
                         "https://storage.market.test/" + "a" * limits.MAX_MARKETPLACE_REDIRECT_URL_CHARS,
                         "https://[::1/x"):
            self.service.reply = Reply(302, b"", {"Location": location} if location else {})
            with self.subTest(location=location[:60]), self.assertRaisesRegex(MarketplaceError, "unsafe location"):
                self.transport().download_redirected("/assets/1", limit=len(self.PAYLOAD))
        self.assertEqual([], self.storage.seen)

    def test_a_second_redirect_is_refused(self) -> None:
        self.storage.reply = Reply(302, b"", {"Location": "https://elsewhere.test/x"})
        with self.assertRaisesRegex(MarketplaceError, "refused a redirect"):
            self.transport().download_redirected("/assets/1", limit=len(self.PAYLOAD))
        self.assertEqual(len(self.network.requests), 2)

    def test_the_storage_copy_is_bounded_and_retried_when_cut_short(self) -> None:
        self.network.tamper("GET", STORAGE + "/blob", truncate_to=5)
        client = self.transport()
        self.assertEqual(client.download_redirected("/assets/1", limit=len(self.PAYLOAD)), self.PAYLOAD)
        self.assertEqual(client.request_count, 4)
        self.network.tamper("GET", STORAGE + "/blob", omit_length=True, extra_bytes=1_000_000)
        with self.assertRaisesRegex(MarketplaceError, "bound"):
            self.transport().download_redirected("/assets/1", limit=len(self.PAYLOAD))


class UploadBodyTest(TransportCase):
    def test_multipart_parts_round_trip(self) -> None:
        jar = self.file("Quick Skin - Fabric.jar", b"PK\x03\x04--mod-base-boundary-lookalike\r\n")
        client = self.transport(writable=True)
        client.post_multipart("/v2/version", [("data", '{"name":"é"}')], [("file", "Quick Skin - Fabric.jar", jar)],
                              replay=False, expected_sha256={"file": hashlib.sha256(jar.read_bytes()).hexdigest()})
        request = self.service.seen[-1]
        parts = parse_multipart(request.headers["content-type"], request.body)
        self.assertEqual(parts, [("data", None, '{"name":"é"}'.encode()),
                                 ("file", "Quick Skin - Fabric.jar", jar.read_bytes())])
        self.assertIn(b"Content-Type: application/java-archive", request.body)
        self.assertEqual(self.network.requests[-1].body_summary, "multipart:data,file")

    def test_a_file_changed_since_it_was_hashed_is_refused_before_the_request(self) -> None:
        jar = self.file("a.jar", b"new bytes")
        client = self.transport(writable=True)
        with self.assertRaisesRegex(MarketplaceError, "changed since it was hashed"):
            client.post_multipart("/v2/version", [], [("file", "a.jar", jar)], replay=False,
                                  expected_sha256={"file": hashlib.sha256(b"old bytes").hexdigest()})
        with self.assertRaisesRegex(MarketplaceError, "changed since it was hashed"):
            client.post_bytes("/assets", jar, "application/java-archive", None, replay=False,
                              expected_sha256=hashlib.sha256(b"old bytes").hexdigest())
        self.assertEqual([], self.network.requests)

    def test_unsafe_parts_and_files_are_refused(self) -> None:
        jar = self.file("a.jar", b"x")
        link = self.root / "link.jar"
        link.symlink_to(jar)
        client = self.transport(writable=True)
        cases = [
            ([("na\"me", "x")], []), ([("a", "x"), ("a", "y")], []), ([], [("file", 'a".jar', jar)]),
            ([], [("file", "../a.jar", jar)]), ([], [("file", "a\r\n.jar", jar)]), ([], [("file", " a.jar", jar)]),
            ([], [("file", "a.jar", link)]), ([], [("file", "a.jar", self.root)]), ([], [("file", "a.jar", "a.jar")]),
            ([], [("file", "a.jar", self.root / "missing.jar")]), ([], []), ([("x", "\x00")], []),
            ([("a", "1")] * (limits.MAX_MULTIPART_PARTS + 1), []),
        ]
        for fields, files in cases:
            with self.subTest(fields=fields, files=files), self.assertRaises(MarketplaceError):
                client.post_multipart("/v2/version", fields, files, replay=False)  # type: ignore[arg-type]
        with self.assertRaises(MarketplaceError):
            client.post_multipart("/v2/version", [], [("file", "a.jar", jar)], replay=False,
                                  expected_sha256={"other": "0" * 64})
        self.assertEqual([], self.network.requests)

    def test_an_empty_or_oversized_file_is_refused(self) -> None:
        empty = self.file("empty.jar", b"")
        client = self.transport(writable=True)
        with self.assertRaises(MarketplaceError):
            client.post_bytes("/assets", empty, "application/java-archive", None, replay=False)
        big = self.file("big.jar", b"x" * 2048)
        with patch.object(limits, "MAX_MARKETPLACE_FILE_BYTES", 1024), self.assertRaises(MarketplaceError):
            client.post_bytes("/assets", big, "application/java-archive", None, replay=False)
        self.assertEqual([], self.network.requests)

    def test_post_bytes_sends_the_file_with_its_type_and_query(self) -> None:
        jar = self.file("a.jar", b"jar-bytes")
        client = self.transport(writable=True)
        client.post_bytes("/assets", jar, "application/java-archive", {"name": "a b.jar"}, replay=False)
        request = self.network.requests[-1]
        self.assertEqual((request.body, request.header("content-type"), request.query),
                         (b"jar-bytes", "application/java-archive", (("name", "a b.jar"),)))
        with self.assertRaises(MarketplaceError):
            client.post_bytes("/assets", jar, "text/plain\r\nX: y", None, replay=False)

    def test_file_digests(self) -> None:
        data = b"digest me"
        digests = transport.file_digests(self.file("a.jar", data), sha256=hashlib.sha256(data).hexdigest().upper())
        self.assertEqual(digests, transport.FileDigests(len(data), hashlib.sha1(data).hexdigest(),
                                                        hashlib.sha256(data).hexdigest(),
                                                        hashlib.sha512(data).hexdigest()))
        with self.assertRaises(MarketplaceError):
            transport.file_digests(self.file("b.jar", data), sha256="0" * 64)
        with self.assertRaises(MarketplaceError):
            transport.file_digests(self.file("c.jar", data), sha256="not-a-digest")


if __name__ == "__main__":
    unittest.main()
