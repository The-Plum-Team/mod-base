"""The publication state machine against the marketplace fakes (``mod_base.release.publish``).

A sealed plan of two tags and two lanes is published to GitHub, Modrinth and CurseForge: the whole
path, a resumed run that sends nothing twice, moderation that only the scheduled finish completes,
a fenced upload and a tag that already names another commit.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mod_base.errors import MbError
from mod_base.marketplace import curseforge, github_releases, modrinth
from mod_base.marketplace.fake import FakeCurseForge, FakeGitHubReleases, FakeModrinth, FakeNetwork
from mod_base.marketplace.transport import HttpsTransport
from mod_base.release import ledger as ledger_doc
from mod_base.release import publish
from mod_base.release.plan import plan_digest, released_commit, seal_release
from tests.test_release_plan import World

REPO = "The-Plum-Team/Example-Mod"
PENDING = 1


class Release(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        world = World(root, "config-two-marketplaces.json")
        self.plan = world.derive()
        self.plan_dir = root / "sealed"
        seal_release(self.plan_dir, plan=self.plan, bundle=world.bundle)
        self.network = FakeNetwork()
        self.github = FakeGitHubReleases(self.network, repo=REPO)
        self.modrinth = FakeModrinth(self.network)
        self.modrinth.add_project("ExAmPl01", "examplemod")
        self.modrinth.add_project("P7dR8mSH", "fabric-api")
        self.curseforge = FakeCurseForge(self.network)
        self.curseforge.seed_game_versions()

    def transport(self, origin: str, headers: dict[str, str] | None = None, *, writable: bool = False) -> HttpsTransport:
        return HttpsTransport(origin, headers=headers, max_requests=publish.MAX_RELEASE_REQUESTS, writable=writable,
                              opener=self.network, sleep=lambda _seconds: None)

    def services(self, *, modrinth_token: bool = False, curseforge_token: bool = False) -> publish.Services:
        headers = github_releases.headers(self.github.token)
        return publish.Services(
            github=self.transport(github_releases.API_ORIGIN, headers, writable=True),
            github_uploads=self.transport(github_releases.UPLOADS_ORIGIN, headers, writable=True),
            modrinth=self.transport(modrinth.MODRINTH_ORIGIN,
                                    modrinth.headers(self.modrinth.token if modrinth_token else None),
                                    writable=modrinth_token),
            curseforge_upload=self.transport(curseforge.UPLOAD_ORIGIN, curseforge.headers(self.curseforge.token),
                                             writable=True) if curseforge_token else None,
            curseforge_listing=self.transport(curseforge.LISTING_ORIGIN),
            curseforge_cdn=self.transport(curseforge.CDN_ORIGIN))

    def publish_all(self) -> bool:
        publish.create_releases(self.services(), repository=REPO, plan_dir=self.plan_dir)
        publish.upload(self.services(modrinth_token=True), repository=REPO, plan_dir=self.plan_dir,
                       marketplace="modrinth")
        publish.upload(self.services(curseforge_token=True), repository=REPO, plan_dir=self.plan_dir,
                       marketplace="curseforge")
        return publish.verify(self.services(), repository=REPO, plan_dir=self.plan_dir, wait_seconds=0)

    def ledger(self, tag: str) -> dict:
        release = next(item for item in self.github.releases if item["tag_name"] == tag)
        return ledger_doc.parse_body(release["body"])

    def uploads(self, origin: str) -> int:
        return len(self.network.requests_to(origin, method="POST"))

    def test_the_whole_release_is_published_and_verified_by_content(self) -> None:
        self.assertTrue(self.publish_all())
        sha = released_commit(self.plan)
        for tag in self.plan["tags"]:
            self.assertEqual(self.github.refs[tag["tag"]]["sha"], sha)
            release = next(item for item in self.github.releases if item["tag_name"] == tag["tag"])
            self.assertFalse(release["draft"])
            names = sorted(asset["name"] for asset in release["assets"])
            self.assertEqual(names, sorted([github_releases.asset_name(asset["filename"]) for asset in tag["assets"]]
                                           + [publish.PLAN_ASSET]))
            ledger = self.ledger(tag["tag"])
            self.assertEqual(ledger["plan_sha256"], plan_digest(self.plan))
            self.assertTrue(ledger_doc.complete(ledger))
        # The last tag of the plan is the repository's latest release.
        latest = next(item for item in self.github.releases if item["tag_name"] == self.plan["tags"][-1]["tag"])
        self.assertEqual(self.github.latest, latest["id"])
        # CurseForge stored the files under its own names; they were still found and verified.
        stored = sorted(item["fileName"] for item in self.curseforge.files)
        self.assertTrue(all(" " not in name for name in stored), stored)
        self.assertEqual(len(self.modrinth.versions), 2)
        self.assertEqual(len(self.curseforge.files), 2)

    def test_files_are_uploaded_oldest_minecraft_first_so_the_newest_is_listed_on_top(self) -> None:
        self.publish_all()
        for origin in (modrinth.MODRINTH_ORIGIN, curseforge.UPLOAD_ORIGIN):
            bodies = [request.body for request in self.network.requests_to(origin, method="POST")]
            fabric = next(i for i, body in enumerate(bodies) if b"1.20.1" in body)
            neoforge = next(i for i, body in enumerate(bodies) if b"1.21.1" in body)
            self.assertLess(fabric, neoforge, origin)

    def test_the_upload_order_is_version_then_loader(self) -> None:
        def lane(loader: str) -> dict:
            return {"curseforge": {"loaders": [loader]}}

        plan = {"lanes": {"neoforge-26.3": lane("neoforge"), "fabric-26.3": lane("fabric"),
                          "neoforge-26.2": lane("neoforge"), "fabric-26.1.2": lane("fabric"),
                          "forge-1.20.1": lane("forge"), "fabric-1.20.1": lane("fabric"),
                          "fabric-1.21.10": lane("fabric"), "fabric-1.21.9": lane("fabric")}}
        minecraft = {"neoforge-26.3": "26.3", "fabric-26.3": "26.3", "neoforge-26.2": "26.2",
                     "fabric-26.1.2": "26.1.2", "forge-1.20.1": "1.20.1", "fabric-1.20.1": "1.20.1",
                     "fabric-1.21.10": "1.21.10", "fabric-1.21.9": "1.21.9"}
        ordered = sorted(plan["lanes"], key=lambda name: publish.upload_order(
            plan, "curseforge", {"minecraft": minecraft[name]}, name))
        self.assertEqual(ordered, ["forge-1.20.1", "fabric-1.20.1", "fabric-1.21.9", "fabric-1.21.10",
                                   "fabric-26.1.2", "neoforge-26.2", "fabric-26.3", "neoforge-26.3"])

    def test_a_second_run_sends_nothing_twice(self) -> None:
        self.assertTrue(self.publish_all())
        before = (self.uploads(FakeModrinth.ORIGIN), self.uploads(FakeCurseForge.UPLOAD_ORIGIN),
                  self.uploads(FakeGitHubReleases.UPLOADS_ORIGIN))
        self.assertTrue(self.publish_all())
        after = (self.uploads(FakeModrinth.ORIGIN), self.uploads(FakeCurseForge.UPLOAD_ORIGIN),
                 self.uploads(FakeGitHubReleases.UPLOADS_ORIGIN))
        self.assertEqual(before, after)
        self.assertEqual(len(self.modrinth.versions), 2)
        self.assertEqual(len(self.curseforge.files), 2)

    def test_moderation_leaves_drafts_that_the_scheduled_finish_publishes(self) -> None:
        self.curseforge.upload_status = PENDING
        self.assertFalse(self.publish_all())
        for tag in self.plan["tags"]:
            states = {row: value["state"] for row, value in self.ledger(tag["tag"])["rows"].items()}
            self.assertEqual(sorted(states.values()), ["pending", "verified"], states)
        self.assertTrue(all(release["draft"] for release in self.github.releases))
        # Nothing changes while CurseForge still holds the files.
        self.assertEqual(publish.finish_drafts(self.services(), repository=REPO),
                         {tag["tag"]: False for tag in self.plan["tags"]})
        for item in self.curseforge.files:
            self.curseforge.set_status(item["id"], curseforge.APPROVED_STATUS)
        self.assertEqual(publish.finish_drafts(self.services(), repository=REPO),
                         {tag["tag"]: True for tag in self.plan["tags"]})
        self.assertFalse(any(release["draft"] for release in self.github.releases))

    def test_an_upload_whose_file_no_marketplace_lists_stays_fenced(self) -> None:
        publish.create_releases(self.services(), repository=REPO, plan_dir=self.plan_dir)
        tag = self.plan["tags"][0]
        release = next(item for item in self.github.releases if item["tag_name"] == tag["tag"])
        ledger = ledger_doc.advance(ledger_doc.parse_body(release["body"]), f"{tag['lanes'][0]}--modrinth",
                                    "uploading")
        release["body"] = ledger_doc.render_body(self.plan, tag["tag"], ledger)
        with self.assertRaises(publish.FencedUpload):
            publish.upload(self.services(modrinth_token=True), repository=REPO, plan_dir=self.plan_dir,
                           marketplace="modrinth")
        # Every other row was still attempted.
        self.assertEqual(len(self.modrinth.versions), 1)

    def test_a_tag_at_another_commit_fails_closed(self) -> None:
        self.github.add_ref(self.plan["tags"][0]["tag"], "1" * 40)
        with self.assertRaises(MbError):
            publish.create_releases(self.services(), repository=REPO, plan_dir=self.plan_dir)
        self.assertEqual(self.github.releases, [])

    def test_no_tag_exists_until_its_release_is_published(self) -> None:
        self.curseforge.upload_status = PENDING
        self.assertFalse(self.publish_all())
        self.assertEqual(self.github.refs, {}, "a release held in moderation has no tag yet")

    def test_a_definitive_refusal_puts_the_row_back_and_the_next_run_sends_it(self) -> None:
        publish.create_releases(self.services(), repository=REPO, plan_dir=self.plan_dir)
        self.network.fail("POST", f"{FakeModrinth.ORIGIN}/v2/version", status=400, times=2)
        with self.assertRaises(MbError):
            publish.upload(self.services(modrinth_token=True), repository=REPO, plan_dir=self.plan_dir,
                           marketplace="modrinth")
        for tag in self.plan["tags"]:
            states = {row: value["state"] for row, value in self.ledger(tag["tag"])["rows"].items()
                      if row.endswith("--modrinth")}
            self.assertEqual(set(states.values()), {"unstarted"}, states)
        self.assertTrue(self.publish_all())
        self.assertEqual(len(self.modrinth.versions), 2)

    def test_a_request_that_cannot_be_built_writes_no_intent(self) -> None:
        self.curseforge.game_versions = [row for row in self.curseforge.game_versions if row["name"] != "Fabric"]
        publish.create_releases(self.services(), repository=REPO, plan_dir=self.plan_dir)
        with self.assertRaises(MbError):
            publish.upload(self.services(curseforge_token=True), repository=REPO, plan_dir=self.plan_dir,
                           marketplace="curseforge")
        self.assertEqual(self.uploads(FakeCurseForge.UPLOAD_ORIGIN), 1, "only the lane CurseForge can name")
        row = f"{self.plan['tags'][0]['lanes'][0]}--curseforge"
        self.assertEqual(self.ledger(self.plan["tags"][0]["tag"])["rows"][row]["state"], "unstarted")

    def test_a_newer_latest_release_keeps_its_mark(self) -> None:
        newer = self.github.add_release("mc1.21.1-v9.0.0", draft=False)
        self.github.latest = newer["id"]
        self.assertTrue(self.publish_all())
        self.assertEqual(self.github.latest, newer["id"])

    def test_verify_refuses_an_unbounded_wait(self) -> None:
        with self.assertRaises(MbError):
            publish.verify(self.services(), repository=REPO, plan_dir=self.plan_dir,
                           wait_seconds=publish.MAX_VERIFY_WAIT_SECONDS + 1)


if __name__ == "__main__":
    unittest.main()
