"""``marketplace.modrinth``: version creation and reconciliation by SHA-512.

The reconciliation cases port Quick Skin ``classify_modrinth``
(``scripts/release/tests/test_reconcile_publication.py``); the client cases run against
``marketplace.fake.FakeModrinth`` through the real transport.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mod_base.marketplace import modrinth
from mod_base.marketplace.fake import FakeModrinth, FakeNetwork, parse_multipart
from mod_base.marketplace.modrinth import ModrinthVersion
from mod_base.marketplace.transport import (
    HttpsTransport,
    MarketplaceError,
    MarketplaceOutcomeUnknown,
    Reconciliation,
)

PROJECT = "AbCd1234"
FILENAME = "Quick Skin - Fabric - 1.20.1-3.0.0.jar"
NUMBER = "mc1.20.1-v3.0.0-fabric-1.20.1"
PAYLOAD = b"quick-skin-published-bytes"
SHA512 = hashlib.sha512(PAYLOAD).hexdigest()


def version(**changes: Any) -> ModrinthVersion:
    values: dict[str, Any] = {
        "project_id": PROJECT, "version_number": NUMBER, "name": "Quick Skin 3.0.0 for Fabric 1.20.1",
        "changelog": "# 3.0.0\n\n- Capes.", "game_versions": ("1.20.1",), "loaders": ("fabric",),
        "version_type": "release", "dependencies": (("fabric-api", "required"), ("P7dR8mSH", "optional")),
        "featured": True,
    }
    values.update(changes)
    return ModrinthVersion(**values)


class ReconcileTest(unittest.TestCase):
    def remote(self, **changes: Any) -> dict[str, Any]:
        remote = {"id": "remote01", "project_id": PROJECT, "version_number": NUMBER,
                  "files": [{"filename": FILENAME, "size": len(PAYLOAD), "hashes": {"sha512": SHA512}}]}
        remote.update(changes)
        return remote

    def reconcile(self, remote: dict[str, Any] | None, versions: list[dict[str, Any]], **changes: Any) -> Reconciliation:
        options: dict[str, Any] = {"expected_sha512": SHA512, "size": len(PAYLOAD), "project_id": PROJECT,
                                   "version_number": NUMBER}
        options.update(changes)
        return modrinth.reconcile(remote, versions, **options)

    def test_skips_only_an_exact_hash_and_identity_match(self) -> None:
        result = self.reconcile(self.remote(), [{"id": "remote01", "version_number": NUMBER}])
        self.assertEqual(result, Reconciliation(False, "remote01"))

    def test_accepts_an_upper_case_expected_digest(self) -> None:
        result = self.reconcile(self.remote(), [{"id": "remote01", "version_number": NUMBER}],
                                expected_sha512=SHA512.upper())
        self.assertFalse(result.publish)

    def test_missing_file_and_version_number_is_publishable(self) -> None:
        self.assertEqual(self.reconcile(None, []), Reconciliation(True, None))
        self.assertTrue(self.reconcile(None, [{"id": "other", "version_number": "another"}]).publish)

    def test_fails_closed_on_a_version_number_held_by_other_bytes(self) -> None:
        with self.assertRaisesRegex(MarketplaceError, "different bytes"):
            self.reconcile(None, [{"id": "other", "version_number": NUMBER}])

    def test_fails_closed_on_a_hash_bound_to_another_identity(self) -> None:
        for remote in (self.remote(project_id="another1", files=[]), self.remote(project_id="another1"),
                       self.remote(version_number="other-number"),
                       self.remote(files=[{"filename": FILENAME, "size": len(PAYLOAD) + 1, "hashes": {"sha512": SHA512}}]),
                       self.remote(files=[{"filename": FILENAME, "size": len(PAYLOAD), "hashes": {"sha512": "0" * 128}}]),
                       self.remote(files=[])):
            with self.subTest(remote=remote), self.assertRaisesRegex(MarketplaceError, "another publication identity"):
                self.reconcile(remote, [{"id": "remote01", "version_number": NUMBER}])

    def test_fails_closed_on_an_ambiguous_identity(self) -> None:
        for versions in ([], [{"id": "remote01", "version_number": NUMBER}, {"id": "dupe0001", "version_number": NUMBER}],
                         [{"id": "dupe0001", "version_number": NUMBER}]):
            with self.subTest(versions=versions), self.assertRaisesRegex(MarketplaceError, "ambiguous"):
                self.reconcile(self.remote(), versions)

    def test_refuses_malformed_input(self) -> None:
        with self.assertRaises(MarketplaceError):
            self.reconcile(None, [], expected_sha512="abc")
        with self.assertRaises(MarketplaceError):
            self.reconcile(None, [], size=0)
        with self.assertRaises(MarketplaceError):
            self.reconcile(None, [None])  # type: ignore[list-item]
        with self.assertRaises(MarketplaceError):
            self.reconcile(self.remote(id="../x"), [{"id": "remote01", "version_number": NUMBER}])
        with self.assertRaisesRegex(MarketplaceError, "another project"):
            self.reconcile(None, [{"id": "x1", "version_number": "a", "project_id": "zzzz9999"}])


class VersionTest(unittest.TestCase):
    def test_a_valid_version(self) -> None:
        self.assertEqual(version().loaders, ("fabric",))

    def test_modrinth_bounds_are_checked_before_any_upload(self) -> None:
        for changes in ({"project_id": "fabric-api"}, {"version_number": "x" * 33}, {"version_number": "a b"},
                        {"version_number": ""}, {"name": ""}, {"name": "x" * 65}, {"name": "a\nb"},
                        {"changelog": "x" * 65_537}, {"game_versions": ()}, {"game_versions": ["1.20.1"]},
                        {"game_versions": ("1.20.1", "1.20.1")}, {"loaders": ("Fabric",)}, {"version_type": "stable"},
                        {"dependencies": (("fabric-api", "needed"),)}, {"dependencies": (("fabric-api",),)},
                        {"dependencies": (("fabric-api", "required"), ("fabric-api", "optional"))},
                        {"dependencies": (("../x", "required"),)}, {"featured": 1}):
            with self.subTest(changes=changes), self.assertRaises(MarketplaceError):
                version(**changes)


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.network = FakeNetwork()
        self.modrinth = FakeModrinth(self.network)
        self.modrinth.add_project(PROJECT, "quick-skin")
        self.modrinth.add_project("FaPi0001", "fabric-api")
        self.modrinth.add_project("P7dR8mSH", "mod-menu")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.jar = Path(directory.name) / FILENAME
        self.jar.write_bytes(PAYLOAD)

    def transport(self, *, token: str | None = None, writable: bool = False, max_requests: int = 20) -> HttpsTransport:
        return HttpsTransport(modrinth.MODRINTH_ORIGIN, headers=modrinth.headers(token), max_requests=max_requests,
                              writable=writable, opener=self.network, sleep=lambda _: None)


class ReadTest(ClientCase):
    def test_version_by_sha512(self) -> None:
        seeded = self.modrinth.add_version(PROJECT, NUMBER, [(FILENAME, PAYLOAD)])
        client = self.transport()
        self.assertEqual(modrinth.version_by_sha512(client, SHA512)["id"], seeded["id"])
        self.assertIsNone(modrinth.version_by_sha512(client, "0" * 128))
        [first, second] = self.network.requests
        self.assertEqual((first.path, first.query), (f"/v2/version_file/{SHA512}", (("algorithm", "sha512"),)))
        self.assertEqual(first.credentials, frozenset())
        with self.assertRaises(MarketplaceError):
            modrinth.version_by_sha512(client, "../../x")
        self.assertEqual(len(self.network.requests), 2)

    def test_project_versions_drop_changelogs_and_stay_bounded(self) -> None:
        self.modrinth.add_version(PROJECT, NUMBER, [(FILENAME, PAYLOAD)])
        rows = modrinth.project_versions(self.transport(), PROJECT)
        self.assertEqual([row["version_number"] for row in rows], [NUMBER])
        self.assertIsNone(rows[0]["changelog"])
        self.assertEqual(self.network.requests[-1].query, (("include_changelog", "false"),))
        self.network.fail("GET", modrinth.MODRINTH_ORIGIN + "/v2/project", status=200, body=b'{"not":"a list"}')
        with self.assertRaises(MarketplaceError):
            modrinth.project_versions(self.transport(), PROJECT)
        many = json.dumps([{"id": f"v{index:07d}"} for index in range(10_001)]).encode()
        self.network.fail("GET", modrinth.MODRINTH_ORIGIN + "/v2/project", status=200, body=many)
        with self.assertRaisesRegex(MarketplaceError, "at most"):
            modrinth.project_versions(self.transport(), PROJECT)

    def test_resolve_project(self) -> None:
        client = self.transport()
        self.assertEqual(modrinth.resolve_project(client, "fabric-api"), "FaPi0001")
        self.assertEqual(modrinth.resolve_project(client, "FaPi0001"), "FaPi0001")
        with self.assertRaises(MarketplaceError):
            modrinth.resolve_project(client, "missing")

    def test_a_token_reaches_only_modrinth(self) -> None:
        client = self.transport(token=self.modrinth.token)
        modrinth.version_by_sha512(client, SHA512)
        self.assertEqual(self.network.requests[-1].header("authorization"), "<secret:modrinth>")
        self.assertEqual(self.network.requests[-1].header("user-agent"), "The-Plum-Team/mod-base")
        self.assertEqual({request.origin for request in self.network.requests}, {modrinth.MODRINTH_ORIGIN})

    def test_requests_need_a_modrinth_transport(self) -> None:
        other = HttpsTransport("https://api.github.com", max_requests=1, opener=self.network)
        for call in (lambda: modrinth.version_by_sha512(other, SHA512), lambda: modrinth.project_versions(other, PROJECT),
                     lambda: modrinth.create_version(other, version(), self.jar, FILENAME)):
            with self.assertRaisesRegex(MarketplaceError, "transport of"):
                call()
        self.assertEqual([], self.network.requests)

    def test_headers(self) -> None:
        self.assertNotIn("Authorization", modrinth.headers(None))
        self.assertEqual(modrinth.headers("mrp_x")["Authorization"], "mrp_x")
        with self.assertRaises(MarketplaceError):
            modrinth.headers("")


class CreateTest(ClientCase):
    def test_create_version_sends_one_multipart_post(self) -> None:
        client = self.transport(token=self.modrinth.token, writable=True)
        created = modrinth.create_version(client, version(), self.jar, FILENAME,
                                          sha256=hashlib.sha256(PAYLOAD).hexdigest())
        posts = self.network.requests_to(modrinth.MODRINTH_ORIGIN, method="POST")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].credentials, frozenset({"modrinth"}))
        parts = parse_multipart(posts[0].header("content-type"), posts[0].body)
        self.assertEqual([(name, filename) for name, filename, _ in parts], [("data", None), ("file", FILENAME)])
        self.assertEqual(parts[1][2], PAYLOAD)
        data = json.loads(parts[0][2])
        self.assertEqual(data["file_parts"], ["file"])
        self.assertEqual(data["primary_file"], "file")
        self.assertEqual(data["dependencies"], [{"dependency_type": "required", "project_id": "FaPi0001"},
                                                {"dependency_type": "optional", "project_id": "P7dR8mSH"}])
        self.assertEqual((data["project_id"], data["version_number"], data["featured"]), (PROJECT, NUMBER, True))
        self.assertEqual(created["version_number"], NUMBER)
        # The created version reconciles as published.
        reconciled = modrinth.reconcile(modrinth.version_by_sha512(client, SHA512),
                                        modrinth.project_versions(client, PROJECT), expected_sha512=SHA512,
                                        size=len(PAYLOAD), project_id=PROJECT, version_number=NUMBER)
        self.assertEqual(reconciled, Reconciliation(False, created["id"]))

    def test_a_5xx_after_acceptance_is_never_retried_and_reconciles(self) -> None:
        self.network.fail("POST", modrinth.MODRINTH_ORIGIN + "/v2/version", status=502, after=True)
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            modrinth.create_version(client, version(dependencies=()), self.jar, FILENAME)
        self.assertEqual(len(self.network.requests_to(method="POST")), 1)
        self.assertEqual(len(self.modrinth.versions), 1)
        result = modrinth.reconcile(modrinth.version_by_sha512(client, SHA512), modrinth.project_versions(client, PROJECT),
                                    expected_sha512=SHA512, size=len(PAYLOAD), project_id=PROJECT,
                                    version_number=NUMBER)
        self.assertFalse(result.publish)

    def test_a_file_that_differs_from_the_plan_is_never_sent(self) -> None:
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaises(MarketplaceError):
            modrinth.create_version(client, version(dependencies=()), self.jar, FILENAME, sha256="0" * 64)
        self.assertEqual([], self.network.requests)

    def test_an_answer_reporting_other_bytes_fails_closed(self) -> None:
        answer = {"id": "zzzz0001", "project_id": PROJECT, "version_number": NUMBER,
                  "files": [{"filename": FILENAME, "size": len(PAYLOAD), "hashes": {"sha512": "0" * 128}}]}
        self.network.fail("POST", modrinth.MODRINTH_ORIGIN + "/v2/version", status=200,
                          body=json.dumps(answer).encode())
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaisesRegex(MarketplaceError, "does not report the uploaded bytes"):
            modrinth.create_version(client, version(dependencies=()), self.jar, FILENAME)
        for changed in ({"project_id": "other001"}, {"version_number": "other"}):
            self.network.fail("POST", modrinth.MODRINTH_ORIGIN + "/v2/version", status=200,
                              body=json.dumps({**answer, **changed}).encode())
            with self.subTest(changed=changed), self.assertRaisesRegex(MarketplaceError, "another project"):
                modrinth.create_version(client, version(dependencies=()), self.jar, FILENAME)

    def test_an_answer_naming_no_version_is_an_unknown_outcome(self) -> None:
        self.network.fail("POST", modrinth.MODRINTH_ORIGIN + "/v2/version", status=200, body=b'{"ok":true}')
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            modrinth.create_version(client, version(dependencies=()), self.jar, FILENAME)

    def test_an_unknown_dependency_stops_before_the_upload(self) -> None:
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaises(MarketplaceError):
            modrinth.create_version(client, version(dependencies=(("sodium", "optional"),)), self.jar, FILENAME)
        self.assertEqual(self.network.requests_to(method="POST"), [])

    def test_two_dependency_names_of_one_project_are_refused(self) -> None:
        client = self.transport(token=self.modrinth.token, writable=True)
        with self.assertRaisesRegex(MarketplaceError, "same project"):
            modrinth.create_version(client, version(dependencies=(("fabric-api", "required"), ("FaPi0001", "optional"))),
                                    self.jar, FILENAME)
        self.assertEqual(self.network.requests_to(method="POST"), [])

    def test_a_refused_token_is_definitive(self) -> None:
        client = HttpsTransport(modrinth.MODRINTH_ORIGIN, headers=modrinth.headers("mrp_wrong_token"), max_requests=5,
                                writable=True, opener=self.network, sleep=lambda _: None)
        with self.assertRaisesRegex(MarketplaceError, "HTTP 401") as caught:
            modrinth.create_version(client, dataclasses.replace(version(), dependencies=()), self.jar, FILENAME)
        self.assertNotIn("mrp_wrong_token", str(caught.exception))
        self.assertEqual(client.request_count, 1)


if __name__ == "__main__":
    unittest.main()
