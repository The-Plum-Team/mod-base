"""``marketplace.curseforge``: uploads, the public listing, the CDN and reconciliation.

Every CurseForge case of Quick Skin's ``scripts/release/tests/test_reconcile_publication.py``
(``master`` and ``fix/curseforge-renamed-files``) is kept; the listing and client cases run
against ``marketplace.fake.FakeCurseForge`` through the real transport.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mod_base.marketplace import curseforge, limits
from mod_base.marketplace.curseforge import CurseForgeFile
from mod_base.marketplace.fake import FakeCurseForge, FakeNetwork, parse_multipart
from mod_base.marketplace.transport import (
    HttpsTransport,
    MarketplaceError,
    MarketplaceOutcomeUnknown,
    MarketplaceUnavailable,
    PublicationPending,
    Reconciliation,
)

FILENAME = "Quick Skin - Fabric - 1.20.1-3.0.0.jar"
STORED = "Quick-Skin-Fabric-1.20.1-3.0.0.jar"
PROJECT = 1323980
PAYLOAD = b"quick-skin-published-bytes"


@dataclasses.dataclass(frozen=True)
class Expected:
    filename: str
    size: int
    sha1: str
    sha256: str


def expected_for(payload: bytes, filename: str = FILENAME) -> Expected:
    return Expected(filename, len(payload), hashlib.sha1(payload).hexdigest(), hashlib.sha256(payload).hexdigest())


def approved(expected: Expected) -> dict[str, Any]:
    return {"id": 42, "projectId": PROJECT, "fileName": expected.filename, "fileLength": expected.size,
            "status": curseforge.APPROVED_STATUS}


def refuse_download(file_id: int, name: str, limit: int) -> bytes:
    raise AssertionError(f"unexpected download of {file_id}/{name}")


def classify(files: list[dict[str, Any]], expected: Expected, fetch: Any = refuse_download,
             project_id: int = PROJECT) -> Reconciliation:
    return curseforge.reconcile(files, filename=expected.filename, size=expected.size, sha1=expected.sha1,
                                sha256=expected.sha256, project_id=project_id, fetch_bytes=fetch)


class NameTest(unittest.TestCase):
    def test_stored_name_joins_spaced_separators_into_one_hyphen(self) -> None:
        for original, stored in (
            ("Quick Skin - Fabric - 1.20.1-4.0.0.jar", "Quick-Skin-Fabric-1.20.1-4.0.0.jar"),
            ("Quick Skin - NeoForge - 26.3-4.0.0.jar", "Quick-Skin-NeoForge-26.3-4.0.0.jar"),
            ("block-pops-2.0.0.jar", "block-pops-2.0.0.jar"),
            ("a b.jar", "a-b.jar"),
            ("a  -  - b.jar", "a-b.jar"),
            ("a--b.jar", "a--b.jar"),
            ("a -- b-c.jar", "a-b-c.jar"),
        ):
            with self.subTest(original=original):
                self.assertEqual(curseforge.stored_name(original), stored)
        with self.assertRaises(MarketplaceError):
            curseforge.stored_name("../a.jar")

    def test_cdn_path_never_pads_the_shard_remainder(self) -> None:
        # A zero-padded remainder is rejected by the CDN with HTTP 403.
        self.assertEqual(curseforge.cdn_path(7214079, "Quick Skin.jar"), "/files/7214/79/Quick%20Skin.jar")
        self.assertEqual(curseforge.cdn_path(42, STORED), f"/files/0/42/{STORED}")
        self.assertEqual(curseforge.cdn_path(7214000, "a+b.jar"), "/files/7214/0/a%2Bb.jar")
        for file_id in (0, -1, True):
            with self.assertRaises(MarketplaceError):
                curseforge.cdn_path(file_id, "a.jar")  # type: ignore[arg-type]

    def test_loader_names(self) -> None:
        self.assertEqual(curseforge.LOADER_NAMES,
                         {"fabric": "Fabric", "forge": "Forge", "neoforge": "NeoForge", "quilt": "Quilt"})


class ReconcileTest(unittest.TestCase):
    def test_reconciles_by_downloading_the_published_bytes(self) -> None:
        expected = expected_for(PAYLOAD)
        requested: list[tuple[int, str, int]] = []

        def fetch(file_id: int, name: str, limit: int) -> bytes:
            requested.append((file_id, name, limit))
            return PAYLOAD

        self.assertEqual(classify([approved(expected)], expected, fetch), Reconciliation(False, "42"))
        # The download is bounded by the staged size and addresses the file by its listed name.
        self.assertEqual(requested, [(42, FILENAME, expected.size)])

    def test_finds_a_file_under_the_name_curseforge_gave_it(self) -> None:
        expected = expected_for(PAYLOAD)
        renamed = dict(approved(expected), fileName=STORED)
        requested: list[str] = []

        def fetch(file_id: int, name: str, limit: int) -> bytes:
            requested.append(curseforge.cdn_path(file_id, name))
            return PAYLOAD

        self.assertEqual(classify([renamed], expected, fetch), Reconciliation(False, "42"))
        # The CDN answers 403 for the original name, so the bytes come from the stored one.
        self.assertEqual(requested, [f"/files/0/42/{STORED}"])

    def test_renamed_file_still_needs_the_staged_bytes(self) -> None:
        expected = expected_for(PAYLOAD)
        renamed = dict(approved(expected), fileName=curseforge.stored_name(expected.filename))
        with self.assertRaisesRegex(MarketplaceError, "different bytes"):
            classify([renamed], expected, lambda file_id, name, limit: b"x" * len(PAYLOAD))

    def test_refuses_both_names_listed_at_once(self) -> None:
        expected = expected_for(PAYLOAD)
        original = approved(expected)
        renamed = dict(original, id=43, fileName=curseforge.stored_name(expected.filename))
        with self.assertRaisesRegex(MarketplaceError, "more than one"):
            classify([original, renamed], expected)

    def test_refuses_a_duplicate_of_one_name(self) -> None:
        expected = expected_for(PAYLOAD)
        with self.assertRaisesRegex(MarketplaceError, "more than one"):
            classify([approved(expected), dict(approved(expected), id=43)], expected)

    def test_fails_closed_when_published_bytes_differ(self) -> None:
        expected = expected_for(PAYLOAD)
        # Same declared length, different content: only hashing the real bytes catches this.
        with self.assertRaisesRegex(MarketplaceError, "different bytes"):
            classify([approved(expected)], expected, lambda file_id, name, limit: b"x" * len(PAYLOAD))

    def test_fails_closed_when_only_the_sha256_diverges(self) -> None:
        # Size and SHA-1 still agree, so only the strong digest can reject this.
        expected = dataclasses.replace(expected_for(PAYLOAD), sha256="a" * 64)
        with self.assertRaises(MarketplaceError):
            classify([approved(expected)], expected, lambda file_id, name, limit: PAYLOAD)

    def test_fails_closed_when_only_the_sha1_diverges(self) -> None:
        expected = dataclasses.replace(expected_for(PAYLOAD), sha1="a" * 40)
        with self.assertRaises(MarketplaceError):
            classify([approved(expected)], expected, lambda file_id, name, limit: PAYLOAD)

    def test_fails_closed_when_the_download_has_another_length(self) -> None:
        expected = expected_for(PAYLOAD)
        with self.assertRaises(MarketplaceError):
            classify([approved(expected)], expected, lambda file_id, name, limit: PAYLOAD + b"x")

    def test_accepts_uppercase_manifest_digests(self) -> None:
        base = expected_for(PAYLOAD)
        expected = dataclasses.replace(base, sha1=base.sha1.upper(), sha256=base.sha256.upper())
        self.assertFalse(classify([approved(expected)], expected, lambda file_id, name, limit: PAYLOAD).publish)

    def test_reports_an_unapproved_file_as_pending(self) -> None:
        expected = expected_for(PAYLOAD)
        # The distinct type is what lets verification poll while pre-publication still fails closed.
        with self.assertRaises(PublicationPending):
            classify([dict(approved(expected), status=1)], expected)

    def test_fails_closed_on_a_declared_size_mismatch(self) -> None:
        expected = expected_for(PAYLOAD)
        with self.assertRaisesRegex(MarketplaceError, "another size"):
            classify([dict(approved(expected), fileLength=expected.size + 1)], expected)

    def test_a_size_mismatch_wins_over_a_pending_status(self) -> None:
        expected = expected_for(PAYLOAD)
        with self.assertRaisesRegex(MarketplaceError, "another size"):
            classify([dict(approved(expected), fileLength=expected.size + 1, status=1)], expected)

    def test_ignores_a_file_owned_by_another_project(self) -> None:
        expected = expected_for(PAYLOAD)
        self.assertTrue(classify([dict(approved(expected), projectId=999999)], expected).publish)

    def test_missing_file_is_publishable(self) -> None:
        self.assertEqual(classify([], expected_for(PAYLOAD)), Reconciliation(True, None))
        other = dict(approved(expected_for(PAYLOAD)), fileName="other.jar")
        self.assertTrue(classify([other], expected_for(PAYLOAD)).publish)

    def test_refuses_a_record_without_a_usable_id(self) -> None:
        expected = expected_for(PAYLOAD)
        for identifier in (None, -1, 0, True, "42"):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(MarketplaceError, "usable id"):
                classify([dict(approved(expected), id=identifier)], expected)

    def test_refuses_malformed_expectations(self) -> None:
        expected = expected_for(PAYLOAD)
        for changes in ({"sha1": "xyz"}, {"sha256": "1" * 63}, {"size": 0}, {"filename": "a/b.jar"}):
            with self.subTest(changes=changes), self.assertRaises(MarketplaceError):
                classify([], dataclasses.replace(expected, **changes))
        with self.assertRaises(MarketplaceError):
            classify([], expected, project_id=0)


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.network = FakeNetwork()
        self.curseforge = FakeCurseForge(self.network)
        self.curseforge.seed_game_versions()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.jar = Path(directory.name) / FILENAME
        self.jar.write_bytes(PAYLOAD)

    def uploads(self, *, writable: bool = True, token: str | None = None) -> HttpsTransport:
        return HttpsTransport(curseforge.UPLOAD_ORIGIN, headers=curseforge.headers(token or self.curseforge.token),
                              max_requests=20, writable=writable, opener=self.network, sleep=lambda _: None)

    def listing_transport(self, max_requests: int = 300) -> HttpsTransport:
        return HttpsTransport(curseforge.LISTING_ORIGIN, max_requests=max_requests, opener=self.network,
                              sleep=lambda _: None)

    def cdn(self) -> HttpsTransport:
        return HttpsTransport(curseforge.CDN_ORIGIN, max_requests=20, opener=self.network, sleep=lambda _: None)

    def file(self, **changes: Any) -> CurseForgeFile:
        values: dict[str, Any] = {"project_id": PROJECT, "display_name": "Quick Skin 3.0.0 for Fabric 1.20.1",
                                  "changelog": "# 3.0.0\n\n- Capes.", "release_type": "release",
                                  "game_version_ids": (9990, 7499, 9153, 9150),
                                  "relations": (("fabric-api", "requiredDependency"),)}
        values.update(changes)
        return CurseForgeFile(**values)


class GameVersionTest(ClientCase):
    def test_names_map_to_ids_for_minecraft_loaders_environments_and_java(self) -> None:
        mapping = curseforge.game_version_ids(self.uploads(writable=False))
        self.assertEqual(mapping, {"1.20.1": 9990, "1.20-Snapshot": 9991, "1.21.1": 11779, "Fabric": 7499,
                                   "Forge": 7498, "NeoForge": 10150, "Client": 9153, "Server": 9150, "Java 21": 11135})
        # The addon row also named "1.20.1" belongs to another type and does not collide.
        self.assertEqual([request.path for request in self.network.requests],
                         ["/api/game/version-types", "/api/game/versions"])
        self.assertEqual({request.credentials for request in self.network.requests}, {frozenset({"curseforge"})})

    def test_a_name_with_two_ids_is_ambiguous(self) -> None:
        self.curseforge.game_versions.append({"id": 9999, "gameVersionTypeID": 77784, "name": "1.20.1", "slug": "x"})
        with self.assertRaisesRegex(MarketplaceError, "names two ids"):
            curseforge.game_version_ids(self.uploads(writable=False))

    def test_the_same_id_listed_twice_is_not_ambiguous(self) -> None:
        self.curseforge.game_versions.append(dict(self.curseforge.game_versions[0]))
        self.assertEqual(curseforge.game_version_ids(self.uploads(writable=False))["1.20.1"], 9990)

    def test_malformed_listings_are_refused(self) -> None:
        for rows in ([{"id": "9990", "gameVersionTypeID": 73250, "name": "1.20.1"}],
                     [{"id": True, "gameVersionTypeID": 73250, "name": "1.20.1"}], [None], {"data": []}):
            self.network.fail("GET", curseforge.UPLOAD_ORIGIN + "/api/game/versions", status=200,
                              body=json.dumps(rows).encode())
            with self.subTest(rows=rows), self.assertRaises(MarketplaceError):
                curseforge.game_version_ids(self.uploads(writable=False))

    def test_the_mapping_needs_the_upload_api_transport(self) -> None:
        with self.assertRaisesRegex(MarketplaceError, "transport of"):
            curseforge.game_version_ids(self.listing_transport())


class ListingTest(ClientCase):
    def seed(self, count: int) -> None:
        for index in range(count):
            self.curseforge.add_file(7, f"other-{index}.jar", b"x", file_id=100 + index)

    def test_listing_advances_by_page_ordinal_and_terminates(self) -> None:
        self.seed(60)
        files = curseforge.listing(self.listing_transport(), 7)
        self.assertEqual(len(files), 60)
        # Two pages, addressed by ordinal. An item offset would re-read the first page forever.
        self.assertEqual([dict(request.query) for request in self.network.requests],
                         [{"pageIndex": "0", "pageSize": "50"}, {"pageIndex": "1", "pageSize": "50"}])
        self.assertEqual({request.credentials for request in self.network.requests}, {frozenset()})

    def test_listing_ends_on_an_empty_page(self) -> None:
        self.assertEqual(curseforge.listing(self.listing_transport(), 7), [])
        self.assertEqual(len(self.network.requests), 1)

    def test_listing_is_bounded_when_the_remote_never_terminates(self) -> None:
        # One new row per page and a totalCount within the bound: only the page bound ends the walk.
        def page(index: int) -> bytes:
            return json.dumps({"data": [{"id": index + 1, "projectId": 7, "fileName": f"f{index}.jar", "fileLength": 1,
                                         "status": 4}],
                               "pagination": {"index": index, "pageSize": 50,
                                              "totalCount": limits.MAX_CURSEFORGE_FILES}}).encode()

        for index in range(limits.MAX_CURSEFORGE_PAGES + 5):
            self.network.fail("GET", curseforge.LISTING_ORIGIN + "/api/v1/mods/7/files", status=200, body=page(index))
        client = self.listing_transport(max_requests=limits.MAX_CURSEFORGE_PAGES + 5)
        with self.assertRaisesRegex(MarketplaceError, "did not end"):
            curseforge.listing(client, 7)
        self.assertEqual(client.request_count, limits.MAX_CURSEFORGE_PAGES)

    def test_a_total_beyond_the_bound_is_refused_at_once(self) -> None:
        endless = {"data": [], "pagination": {"index": 0, "pageSize": 100, "totalCount": 10_000_000}}
        self.network.fail("GET", curseforge.LISTING_ORIGIN + "/api", status=200, body=json.dumps(endless).encode())
        client = self.listing_transport()
        with self.assertRaisesRegex(MarketplaceError, "beyond the bound"):
            curseforge.listing(client, 7)
        self.assertEqual(client.request_count, 1)

    def test_unusable_rows_are_refused_rather_than_skipped(self) -> None:
        # A page of unusable rows must not keep the walk going: it fails on the first page.
        junk = {"data": [None] * 50, "pagination": {"index": 0, "pageSize": 50, "totalCount": 98}}
        self.network.fail("GET", curseforge.LISTING_ORIGIN + "/api", status=200, body=json.dumps(junk).encode())
        client = self.listing_transport()
        with self.assertRaisesRegex(MarketplaceError, "malformed row"):
            curseforge.listing(client, 7)
        self.assertEqual(client.request_count, 1)

    def test_malformed_envelopes_are_refused(self) -> None:
        for answer in ([], {"data": []}, {"data": [], "pagination": {"totalCount": "1"}},
                       {"data": [{"id": 1}], "pagination": {"totalCount": 1}},
                       {"data": [{"id": 1, "projectId": 7, "fileName": "a", "fileLength": 1, "status": 4}] * 51,
                        "pagination": {"totalCount": 51}}):
            self.network.fail("GET", curseforge.LISTING_ORIGIN + "/api", status=200, body=json.dumps(answer).encode())
            with self.subTest(answer=str(answer)[:60]), self.assertRaises(MarketplaceError):
                curseforge.listing(self.listing_transport(), 7)

    def test_a_listing_that_moves_while_read_is_unavailable(self) -> None:
        self.seed(60)
        client = self.listing_transport()
        # A file uploaded between the two pages shifts every row by one: the boundary row repeats.
        original = self.curseforge.handle_listing

        def shifting(request: Any) -> Any:
            if request.query.get("pageIndex") == "1":
                self.curseforge.add_file(7, "new.jar", b"y", file_id=999)
                self.curseforge.files.sort(key=lambda row: -row["id"])
            return original(request)

        self.network._services[curseforge.LISTING_ORIGIN] = shifting
        with self.assertRaises(MarketplaceUnavailable):
            curseforge.listing(client, 7)


class UploadTest(ClientCase):
    def test_upload_sends_metadata_and_file_once_to_the_upload_api(self) -> None:
        identifier = curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME,
                                       sha256=hashlib.sha256(PAYLOAD).hexdigest())
        [post] = self.network.requests_to(curseforge.UPLOAD_ORIGIN, method="POST")
        self.assertEqual(post.path, f"/api/projects/{PROJECT}/upload-file")
        self.assertEqual(post.credentials, frozenset({"curseforge"}))
        self.assertEqual(post.header("x-api-token"), "<secret:curseforge>")
        parts = parse_multipart(post.header("content-type"), post.body)
        self.assertEqual([(name, filename) for name, filename, _ in parts], [("metadata", None), ("file", FILENAME)])
        self.assertEqual(json.loads(parts[0][2]), {
            "changelog": "# 3.0.0\n\n- Capes.", "changelogType": "markdown",
            "displayName": "Quick Skin 3.0.0 for Fabric 1.20.1", "gameVersions": [9990, 7499, 9153, 9150],
            "relations": {"projects": [{"slug": "fabric-api", "type": "requiredDependency"}]}, "releaseType": "release"})
        self.assertEqual(self.curseforge.files[0]["id"], identifier)

    def test_no_relations_are_sent_when_none_are_declared(self) -> None:
        curseforge.upload(self.uploads(), self.file(relations=()), self.jar, FILENAME)
        [post] = self.network.requests_to(method="POST")
        metadata = json.loads(parse_multipart(post.header("content-type"), post.body)[0][2])
        self.assertNotIn("relations", metadata)

    def test_end_to_end_the_renamed_upload_reconciles_through_the_cdn(self) -> None:
        identifier = curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)
        self.assertEqual(self.curseforge.files[0]["fileName"], STORED)
        expected = expected_for(PAYLOAD)
        cdn = self.cdn()
        result = classify(curseforge.listing(self.listing_transport(), PROJECT), expected,
                          lambda file_id, name, limit: curseforge.download(cdn, file_id, name, limit=limit))
        self.assertEqual(result, Reconciliation(False, str(identifier)))
        [download] = self.network.requests_to(curseforge.CDN_ORIGIN)
        self.assertEqual(download.path, curseforge.cdn_path(identifier, STORED))
        # The token went to the upload API only.
        for request in self.network.requests:
            self.assertEqual(request.credentials, frozenset({"curseforge"}) if request.origin == curseforge.UPLOAD_ORIGIN
                             else frozenset(), request.url)

    def test_the_cdn_refuses_the_original_name_of_a_renamed_file(self) -> None:
        identifier = curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)
        with self.assertRaisesRegex(MarketplaceError, "HTTP 403"):
            curseforge.download(self.cdn(), identifier, FILENAME, limit=len(PAYLOAD))

    def test_an_earlier_upload_keeps_its_original_name(self) -> None:
        seeded = self.curseforge.add_file(PROJECT, FILENAME, PAYLOAD)
        cdn = self.cdn()
        result = classify(curseforge.listing(self.listing_transport(), PROJECT), expected_for(PAYLOAD),
                          lambda file_id, name, limit: curseforge.download(cdn, file_id, name, limit=limit))
        self.assertEqual(result, Reconciliation(False, str(seeded["id"])))

    def test_a_settling_upload_is_pending_and_never_downloaded(self) -> None:
        self.curseforge.upload_status = 1
        curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)
        with self.assertRaises(PublicationPending):
            classify(curseforge.listing(self.listing_transport(), PROJECT), expected_for(PAYLOAD))
        self.assertEqual(self.network.requests_to(curseforge.CDN_ORIGIN), [])

    def test_a_5xx_after_acceptance_is_never_retried_and_reconciles(self) -> None:
        self.network.fail("POST", curseforge.UPLOAD_ORIGIN + "/api/projects", status=502, after=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)
        self.assertEqual(len(self.network.requests_to(method="POST")), 1)
        self.assertEqual(len(self.curseforge.files), 1)
        cdn = self.cdn()
        result = classify(curseforge.listing(self.listing_transport(), PROJECT), expected_for(PAYLOAD),
                          lambda file_id, name, limit: curseforge.download(cdn, file_id, name, limit=limit))
        self.assertFalse(result.publish)

    def test_a_timeout_after_acceptance_is_never_retried(self) -> None:
        self.network.fail("POST", curseforge.UPLOAD_ORIGIN + "/api/projects", exception=TimeoutError(), after=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)
        self.assertEqual(len(self.network.requests_to(method="POST")), 1)

    def test_an_answer_without_an_id_is_an_unknown_outcome(self) -> None:
        for body in (b'{"id":0}', b'{"id":"12"}', b"[]", b'{"id":true}'):
            self.network.fail("POST", curseforge.UPLOAD_ORIGIN + "/api/projects", status=200, body=body)
            with self.subTest(body=body), self.assertRaises(MarketplaceOutcomeUnknown):
                curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME)

    def test_an_invalid_game_version_is_a_definitive_refusal(self) -> None:
        with self.assertRaisesRegex(MarketplaceError, "Invalid game version ID"):
            curseforge.upload(self.uploads(), self.file(game_version_ids=(123,)), self.jar, FILENAME)

    def test_a_file_that_differs_from_the_plan_is_never_sent(self) -> None:
        with self.assertRaises(MarketplaceError):
            curseforge.upload(self.uploads(), self.file(), self.jar, FILENAME, sha256="0" * 64)
        self.assertEqual(self.network.requests, [])

    def test_uploads_need_the_upload_api_transport_and_write_access(self) -> None:
        with self.assertRaisesRegex(MarketplaceError, "transport of"):
            curseforge.upload(self.listing_transport(), self.file(), self.jar, FILENAME)
        with self.assertRaises(MarketplaceError) as caught:
            curseforge.upload(self.uploads(writable=False), self.file(), self.jar, FILENAME)
        self.assertEqual(caught.exception.reason, "read-only")
        with self.assertRaisesRegex(MarketplaceError, "transport of"):
            curseforge.download(self.listing_transport(), 42, STORED, limit=1)
        self.assertEqual(self.network.requests, [])

    def test_file_metadata_is_validated(self) -> None:
        for changes in ({"project_id": 0}, {"display_name": ""}, {"display_name": "x" * 257}, {"release_type": "final"},
                        {"game_version_ids": ()}, {"game_version_ids": (1, 1)}, {"game_version_ids": (0,)},
                        {"game_version_ids": [1]}, {"relations": (("Fabric API", "requiredDependency"),)},
                        {"relations": (("fabric-api", "required"),)},
                        {"relations": (("fabric-api", "tool"), ("fabric-api", "incompatible"))},
                        {"changelog": "x" * (limits.MAX_MARKETPLACE_CHANGELOG_CHARS + 1)}):
            with self.subTest(changes=changes), self.assertRaises(MarketplaceError):
                self.file(**changes)

    def test_headers(self) -> None:
        self.assertEqual(curseforge.headers("t")["X-Api-Token"], "t")
        with self.assertRaises(MarketplaceError):
            curseforge.headers("")


if __name__ == "__main__":
    unittest.main()
