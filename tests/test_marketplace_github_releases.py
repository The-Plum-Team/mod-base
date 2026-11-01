"""``marketplace.github_releases``: tags, drafts, assets and their reconciliation by content.

Runs against ``marketplace.fake.FakeGitHubReleases`` through the real transport; the asset-name
rule and its collision check port Quick Skin ``github_release.expected_remote_assets``.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mod_base.marketplace import github_releases as gh
from mod_base.marketplace import limits
from mod_base.marketplace.fake import FakeGitHubReleases, FakeNetwork
from mod_base.marketplace.transport import (
    HttpsTransport,
    MarketplaceError,
    MarketplaceOutcomeUnknown,
    MarketplaceUnavailable,
)

REPO = "The-Plum-Team/Example-Mod"
TAG = "mc1.20.1-v4.0.0"
SHA = "a" * 40
FILENAME = "Quick Skin - Fabric - 1.20.1-4.0.0.jar"
ASSET = "Quick.Skin.-.Fabric.-.1.20.1-4.0.0.jar"
PAYLOAD = b"published jar bytes"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class NameTest(unittest.TestCase):
    def test_spaces_become_periods_and_outer_periods_go(self) -> None:
        for filename, name in ((FILENAME, ASSET), ("block-pops-2.0.0.jar", "block-pops-2.0.0.jar"),
                               (" lead.jar", None), ("trail .jar", "trail..jar"), ("a_b-c.jar", "a_b-c.jar"),
                               (".hidden.jar", "hidden.jar")):
            with self.subTest(filename=filename):
                if name is None or ".." in name:
                    with self.assertRaises(MarketplaceError):
                        gh.asset_name(filename)
                else:
                    self.assertEqual(gh.asset_name(filename), name)

    def test_names_whose_normalization_is_not_known_are_refused(self) -> None:
        for filename in ("a+b.jar", "a(1).jar", "a  b.jar", "a/b.jar", "é.jar", "a,b.jar", "...", "a\tb.jar"):
            with self.subTest(filename=filename), self.assertRaises(MarketplaceError):
                gh.asset_name(filename)

    def test_headers(self) -> None:
        self.assertEqual(gh.headers("t"), {"Accept": "application/vnd.github+json", "Authorization": "Bearer t",
                                           "User-Agent": "The-Plum-Team/mod-base",
                                           "X-GitHub-Api-Version": "2022-11-28"})
        with self.assertRaises(MarketplaceError):
            gh.headers("")


class ClientCase(unittest.TestCase):
    def setUp(self) -> None:
        self.network = FakeNetwork()
        self.github = FakeGitHubReleases(self.network, repo=REPO)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.jar = self.root / FILENAME
        self.jar.write_bytes(PAYLOAD)

    def api(self, *, writable: bool = True, max_requests: int = 50) -> HttpsTransport:
        return HttpsTransport(gh.API_ORIGIN, headers=gh.headers(self.github.token), max_requests=max_requests,
                              writable=writable, opener=self.network, sleep=lambda _: None)

    def uploads(self) -> HttpsTransport:
        return HttpsTransport(gh.UPLOADS_ORIGIN, headers=gh.headers(self.github.token), max_requests=10, writable=True,
                              opener=self.network, sleep=lambda _: None)

    def assert_tokens_stay_home(self) -> None:
        for request in self.network.requests:
            expected = frozenset({"github"}) if request.origin in {gh.API_ORIGIN, gh.UPLOADS_ORIGIN} else frozenset()
            self.assertEqual(request.credentials, expected, request.url)


class ReleaseListingTest(ClientCase):
    def test_find_release_includes_drafts_across_pages(self) -> None:
        wanted = self.github.add_release(TAG, draft=True)
        for index in range(150):
            self.github.add_release(f"v0.{index}.0", draft=False)
        client = self.api(writable=False)
        self.assertEqual(gh.find_release(client, REPO, TAG)["id"], wanted["id"])
        self.assertEqual([dict(request.query)["page"] for request in self.network.requests], ["1", "2"])
        self.assertIsNone(gh.find_release(client, REPO, "v9.9.9"))
        self.assert_tokens_stay_home()

    def test_two_releases_with_one_tag_are_ambiguous(self) -> None:
        self.github.add_release(TAG)
        self.github.add_release(TAG)
        with self.assertRaisesRegex(MarketplaceError, "2 GitHub releases"):
            gh.find_release(self.api(), REPO, TAG)

    def test_draft_releases(self) -> None:
        first = self.github.add_release("v1.0.0", draft=True)
        self.github.add_release("v0.9.0", draft=False)
        self.assertEqual([row["id"] for row in gh.draft_releases(self.api(), REPO)], [first["id"]])

    def test_the_listing_is_bounded(self) -> None:
        pages = limits.MAX_GITHUB_RELEASES // limits.GITHUB_PER_PAGE + 1
        for index in range(pages):
            rows = [{"id": index * 100 + row + 1, "tag_name": f"t{row}", "draft": False} for row in range(100)]
            self.network.fail("GET", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=200,
                              body=json.dumps(rows).encode())
        client = self.api(writable=False, max_requests=100)
        with self.assertRaisesRegex(MarketplaceError, "exceeds its bound"):
            gh.draft_releases(client, REPO)
        self.assertEqual(client.request_count, pages)

    def test_a_repeated_row_means_the_listing_moved(self) -> None:
        rows = [{"id": index + 1, "tag_name": f"t{index}", "draft": False} for index in range(100)]
        for _ in range(2):
            self.network.fail("GET", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=200,
                              body=json.dumps(rows).encode())
        with self.assertRaises(MarketplaceUnavailable):
            gh.draft_releases(self.api(writable=False), REPO)

    def test_malformed_releases_are_refused(self) -> None:
        for rows in ([{"id": 1, "tag_name": TAG}], [{"id": "1", "tag_name": TAG, "draft": True}], [None], {"a": 1}):
            self.network.fail("GET", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=200, body=json.dumps(rows).encode())
            with self.subTest(rows=rows), self.assertRaises(MarketplaceError):
                gh.find_release(self.api(), REPO, TAG)

    def test_inputs_are_validated_before_the_network(self) -> None:
        client = self.api()
        for call in (lambda: gh.find_release(client, "../x", TAG), lambda: gh.find_release(client, REPO, "a..b"),
                     lambda: gh.find_release(client, REPO, "x.lock"), lambda: gh.find_release(client, REPO, "a/b"),
                     lambda: gh.create_ref(client, REPO, TAG, "abc"), lambda: gh.get_release(client, REPO, 0)):
            with self.assertRaises(MarketplaceError):
                call()
        self.assertEqual(self.network.requests, [])

    def test_requests_need_their_own_origin(self) -> None:
        with self.assertRaisesRegex(MarketplaceError, "transport of"):
            gh.find_release(self.uploads(), REPO, TAG)
        with self.assertRaisesRegex(MarketplaceError, "transport of"):
            gh.upload_asset(self.api(), REPO, 1, self.jar, FILENAME)
        self.assertEqual(self.network.requests, [])


class RefTest(ClientCase):
    def test_create_ref(self) -> None:
        gh.create_ref(self.api(), REPO, TAG, SHA)
        self.assertEqual(self.github.refs[TAG], {"sha": SHA, "type": "commit"})
        [post] = self.network.requests
        self.assertEqual(json.loads(post.body), {"ref": f"refs/tags/{TAG}", "sha": SHA})

    def test_an_existing_ref_at_the_same_commit_is_accepted(self) -> None:
        self.github.add_ref(TAG, SHA)
        client = self.api()
        gh.create_ref(client, REPO, TAG, SHA)
        self.assertEqual([(request.method, request.path) for request in self.network.requests],
                         [("POST", f"/repos/{REPO}/git/refs"), ("GET", f"/repos/{REPO}/git/ref/tags/{TAG}")])

    def test_a_ref_at_another_commit_or_an_annotated_tag_fails_closed(self) -> None:
        self.github.add_ref(TAG, "b" * 40)
        with self.assertRaisesRegex(MarketplaceError, "another commit"):
            gh.create_ref(self.api(), REPO, TAG, SHA)
        self.github.add_ref(TAG, SHA, annotated=True)
        with self.assertRaisesRegex(MarketplaceError, "annotated"):
            gh.create_ref(self.api(), REPO, TAG, SHA)
        self.assertEqual(self.github.refs[TAG]["type"], "tag")

    def test_a_5xx_is_retried_and_the_accepted_first_attempt_is_reconciled(self) -> None:
        self.network.fail("POST", gh.API_ORIGIN + f"/repos/{REPO}/git/refs", status=502, after=True)
        client = self.api()
        gh.create_ref(client, REPO, TAG, SHA)
        self.assertEqual(client.request_count, 3)

    def test_a_refusal_without_a_ref_fails_closed(self) -> None:
        self.network.fail("POST", gh.API_ORIGIN + f"/repos/{REPO}/git/refs", status=422,
                          body=b'{"message":"Object does not exist"}')
        with self.assertRaisesRegex(MarketplaceError, "holds no such ref"):
            gh.create_ref(self.api(), REPO, TAG, SHA)


class DraftTest(ClientCase):
    def test_create_draft(self) -> None:
        release = gh.create_draft(self.api(), REPO, tag=TAG, name="Example 4.0.0", body="notes", target_commitish=SHA)
        self.assertTrue(release["draft"])
        [post] = self.network.requests
        self.assertEqual(json.loads(post.body), {"body": "notes", "draft": True, "generate_release_notes": False,
                                                 "name": "Example 4.0.0", "prerelease": False, "tag_name": TAG,
                                                 "target_commitish": SHA})

    def test_a_5xx_after_creating_a_draft_is_never_retried(self) -> None:
        self.network.fail("POST", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=502, after=True)
        client = self.api()
        with self.assertRaises(MarketplaceOutcomeUnknown):
            gh.create_draft(client, REPO, tag=TAG, name="n", body="", target_commitish=SHA)
        self.assertEqual((client.request_count, len(self.github.releases)), (1, 1))
        # The draft that was created anyway is found again by its tag.
        self.assertIsNotNone(gh.find_release(client, REPO, TAG))

    def test_an_answer_naming_no_release_is_an_unknown_outcome(self) -> None:
        self.network.fail("POST", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=201, body=b'{"ok":1}')
        with self.assertRaises(MarketplaceOutcomeUnknown):
            gh.create_draft(self.api(), REPO, tag=TAG, name="n", body="", target_commitish=SHA)

    def test_draft_inputs_are_bounded(self) -> None:
        for options in ({"name": ""}, {"name": "x" * 257}, {"body": "x" * 125_001}, {"target_commitish": "main"}):
            values: dict[str, Any] = {"tag": TAG, "name": "n", "body": "", "target_commitish": SHA, **options}
            with self.subTest(options={key: str(value)[:20] for key, value in options.items()}):
                with self.assertRaises(MarketplaceError):
                    gh.create_draft(self.api(), REPO, **values)
        self.assertEqual(self.network.requests, [])

    def test_update_body_restates_the_tag(self) -> None:
        release = self.github.add_release(TAG)
        updated = gh.update_body(self.api(), REPO, release["id"], "ledger", TAG)
        self.assertEqual((updated["body"], updated["tag_name"]), ("ledger", TAG))
        self.assertEqual(json.loads(self.network.requests[-1].body), {"body": "ledger", "tag_name": TAG})
        # The fake detaches a draft edited without its tag, as GitHub does.
        self.api().patch_json(f"/repos/{REPO}/releases/{release['id']}", {"body": "x"})
        self.assertTrue(self.github.release(release["id"])["tag_name"].startswith("untagged-"))

    def test_an_unconfirmed_body_fails_closed(self) -> None:
        release = self.github.add_release(TAG)
        stale = dict(release, body="other")
        self.network.fail("PATCH", gh.API_ORIGIN + f"/repos/{REPO}/releases", status=200, body=json.dumps(stale).encode())
        with self.assertRaisesRegex(MarketplaceError, "did not confirm"):
            gh.update_body(self.api(), REPO, release["id"], "ledger", TAG)

    def test_publish(self) -> None:
        release = self.github.add_release(TAG)
        published = gh.publish(self.api(), REPO, release["id"], TAG, make_latest=True)
        self.assertFalse(published["draft"])
        self.assertEqual(self.github.latest, release["id"])
        self.assertEqual(json.loads(self.network.requests[-1].body),
                         {"draft": False, "make_latest": "true", "tag_name": TAG})
        other = self.github.add_release("v3.0.0")
        gh.publish(self.api(), REPO, other["id"], "v3.0.0", make_latest=False)
        self.assertEqual(json.loads(self.network.requests[-1].body)["make_latest"], "false")
        self.assertEqual(self.github.latest, release["id"])
        with self.assertRaises(MarketplaceError):
            gh.publish(self.api(), REPO, other["id"], "v3.0.0", make_latest="true")  # type: ignore[arg-type]

    def test_get_release(self) -> None:
        release = self.github.add_release(TAG)
        self.assertEqual(gh.get_release(self.api(), REPO, release["id"])["tag_name"], TAG)


class AssetTest(ClientCase):
    def test_upload_asset_goes_to_the_uploads_origin_once(self) -> None:
        release = self.github.add_release(TAG)
        asset = gh.upload_asset(self.uploads(), REPO, release["id"], self.jar, FILENAME, sha256=digest(PAYLOAD))
        self.assertEqual((asset["name"], asset["size"]), (ASSET, len(PAYLOAD)))
        [post] = self.network.requests
        self.assertEqual((post.origin, post.query, post.body), (gh.UPLOADS_ORIGIN, (("name", FILENAME),), PAYLOAD))
        self.assertEqual(post.header("content-type"), "application/java-archive")
        self.assert_tokens_stay_home()

    def test_a_5xx_after_an_upload_is_never_retried(self) -> None:
        release = self.github.add_release(TAG)
        self.network.fail("POST", gh.UPLOADS_ORIGIN, status=502, after=True)
        with self.assertRaises(MarketplaceOutcomeUnknown):
            gh.upload_asset(self.uploads(), REPO, release["id"], self.jar, FILENAME)
        self.assertEqual(len(self.network.requests), 1)
        self.assertEqual(gh.bind_assets(self.api(), REPO, release, {FILENAME: (len(PAYLOAD), digest(PAYLOAD))}),
                         {FILENAME})

    def test_an_upload_answer_with_other_bytes_fails_closed(self) -> None:
        release = self.github.add_release(TAG)
        for changes in ({"size": 1}, {"name": "other.jar"}, {"state": "starter"}, {"digest": "sha256:" + "0" * 64}):
            answer = {"id": 9, "name": ASSET, "size": len(PAYLOAD), "state": "uploaded", **changes}
            self.network.fail("POST", gh.UPLOADS_ORIGIN, status=201, body=json.dumps(answer).encode())
            with self.subTest(changes=changes), self.assertRaises(MarketplaceError):
                gh.upload_asset(self.uploads(), REPO, release["id"], self.jar, FILENAME)

    def test_an_upload_differing_from_the_plan_is_never_sent(self) -> None:
        with self.assertRaises(MarketplaceError):
            gh.upload_asset(self.uploads(), REPO, 1, self.jar, FILENAME, sha256="0" * 64)
        with self.assertRaises(MarketplaceError):
            gh.upload_asset(self.uploads(), REPO, 1, self.jar, "a+b.jar")
        self.assertEqual(self.network.requests, [])

    def test_asset_bytes_follow_one_redirect_without_credentials(self) -> None:
        release = self.github.add_release(TAG)
        asset = self.github.add_asset(release["id"], ASSET, PAYLOAD)
        client = self.api(writable=False)
        self.assertEqual(gh.asset_bytes(client, REPO, asset["id"], limit=len(PAYLOAD)), PAYLOAD)
        first, second = self.network.requests
        self.assertEqual((first.origin, first.header("accept")), (gh.API_ORIGIN, "application/octet-stream"))
        self.assertEqual((second.origin, second.credentials), (FakeGitHubReleases.STORAGE_ORIGIN, frozenset()))
        self.assertIsNone(second.header("authorization"))
        self.assert_tokens_stay_home()

    def test_asset_bytes_are_bounded(self) -> None:
        release = self.github.add_release(TAG)
        asset = self.github.add_asset(release["id"], ASSET, PAYLOAD)
        with self.assertRaises(MarketplaceError):
            gh.asset_bytes(self.api(), REPO, asset["id"], limit=len(PAYLOAD) - 1)
        self.network.tamper("GET", FakeGitHubReleases.STORAGE_ORIGIN, omit_length=True, extra_bytes=10_000_000)
        with self.assertRaisesRegex(MarketplaceError, "bound"):
            gh.asset_bytes(self.api(), REPO, asset["id"], limit=len(PAYLOAD))
        self.assertLess(self.network.delivered, 3 * len(PAYLOAD) + 2)

    def test_a_redirect_to_plain_http_is_refused(self) -> None:
        release = self.github.add_release(TAG)
        asset = self.github.add_asset(release["id"], ASSET, PAYLOAD)
        self.network.fail("GET", gh.API_ORIGIN + f"/repos/{REPO}/releases/assets", status=302,
                          headers={"Location": "http://release-assets.githubusercontent.com/x"})
        with self.assertRaisesRegex(MarketplaceError, "unsafe location"):
            gh.asset_bytes(self.api(), REPO, asset["id"], limit=len(PAYLOAD))
        self.assertEqual(len(self.network.requests), 1)


class BindTest(ClientCase):
    def setUp(self) -> None:
        super().setUp()
        self.release = self.github.add_release(TAG)
        self.local = {FILENAME: (len(PAYLOAD), digest(PAYLOAD)), "sbom.json": (3, digest(b"{}\n"))}

    def test_present_assets_with_the_same_bytes_are_bound(self) -> None:
        self.github.add_asset(self.release["id"], ASSET, PAYLOAD)
        self.assertEqual(gh.bind_assets(self.api(), REPO, self.release, self.local), {FILENAME})
        self.assert_tokens_stay_home()

    def test_nothing_present_binds_nothing(self) -> None:
        self.assertEqual(gh.bind_assets(self.api(), REPO, self.release, self.local), set())
        self.assertEqual(len(self.network.requests), 1)

    def test_an_asset_with_other_bytes_fails_closed(self) -> None:
        self.github.add_asset(self.release["id"], ASSET, b"x" * len(PAYLOAD), digest=False)
        with self.assertRaisesRegex(MarketplaceError, "different bytes"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)

    def test_a_reported_size_or_digest_mismatch_fails_before_downloading(self) -> None:
        self.github.add_asset(self.release["id"], ASSET, b"short")
        with self.assertRaisesRegex(MarketplaceError, "another size"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)
        self.github.release(self.release["id"])["assets"] = []
        self.github.add_asset(self.release["id"], ASSET, PAYLOAD)
        self.github.release(self.release["id"])["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(MarketplaceError, "another digest"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)
        self.assertEqual([request for request in self.network.requests if "assets/" in request.path], [])

    def test_names_colliding_after_normalization_are_refused(self) -> None:
        local = {"a b.jar": (1, "0" * 64), "a.b.jar": (1, "1" * 64)}
        with self.assertRaisesRegex(MarketplaceError, "collide"):
            gh.bind_assets(self.api(), REPO, self.release, local)
        with self.assertRaisesRegex(MarketplaceError, "collide"):
            gh.bind_assets(self.api(), REPO, self.release, {"x.jar": (1, "0" * 64), ".x.jar": (1, "0" * 64)})
        self.assertEqual(self.network.requests, [])

    def test_an_unexpected_or_duplicate_or_unfinished_asset_is_refused(self) -> None:
        self.github.add_asset(self.release["id"], "extra.jar", b"x")
        with self.assertRaisesRegex(MarketplaceError, "unexpected asset"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)
        self.github.release(self.release["id"])["assets"] = []
        self.github.add_asset(self.release["id"], ASSET, PAYLOAD)
        self.github.add_asset(self.release["id"], ASSET, PAYLOAD)
        with self.assertRaisesRegex(MarketplaceError, "duplicate"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)
        self.github.release(self.release["id"])["assets"] = []
        self.github.add_asset(self.release["id"], ASSET, PAYLOAD, state="starter")
        with self.assertRaisesRegex(MarketplaceError, "not uploaded"):
            gh.bind_assets(self.api(), REPO, self.release, self.local)

    def test_local_entries_are_validated(self) -> None:
        for local in ({FILENAME: (0, digest(PAYLOAD))}, {FILENAME: (1, "xyz")}, {FILENAME: [1, digest(PAYLOAD)]},
                      {"a+b.jar": (1, digest(PAYLOAD))}):
            with self.subTest(local=local), self.assertRaises(MarketplaceError):
                gh.bind_assets(self.api(), REPO, self.release, local)  # type: ignore[arg-type]
        with self.assertRaises(MarketplaceError):
            gh.bind_assets(self.api(), REPO, {"id": 1}, self.local)


if __name__ == "__main__":
    unittest.main()
