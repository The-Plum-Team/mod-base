"""``refresh`` (SPEC §5.4): the promoted bundle revalidated in its own Pages attempt and written as the
exact bytes of its rolling cache (and baseline), or of a family cache.

Ports Block Pops ``test_pages_refresh_cache`` (the exact successful build and deploy are required
before any download; unknown or cross-branch requests never emit a result; post-validation drift of
jobs, source or bytes fails) and the Quick Skin refresh-cache step (the live head must still be the
coverage; the complete generation is retained as the feature baseline, once: a republication from
its cache or from the same handoff retains no second copy) onto the v1 promotion.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.evidence import compact as compact_module
from mod_base.github import artifacts as github_artifacts
from mod_base.github.api import ApiError, ApiNotFound
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.model.limits import LISTING_READ_ATTEMPTS, MAX_PAGES_API_READS
from mod_base.pages import authenticate, commands_build
from mod_base.pages.build import PROMOTION_FILE, build_site
from mod_base.pages.refresh import RefreshResult, refresh_bundle
from mod_base.pages.select import Selected
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, caller_job_name, step_name
from tests import test_build_support as bs
from tests.fixtures.mods import support
from tests.test_select import World


def listing(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file()}


class RefreshFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-refresh-")).resolve()
        cls.pub = bs.Publication(cls.directory / "publication")
        world = cls.pub.world(stage="build")
        with mock.patch.object(host, "call", support.InProcessHost(api=world.api)):
            build_site(cls.pub.invocation(), api=world.api, kit_root=cls.pub.kit,
                       collected_dir=cls.directory / "collected", families_dir=cls.directory / "families",
                       output=cls.directory / "_site", promotion_dir=cls.directory / "promotion")
        cls.promotion = json.loads((cls.directory / "promotion" / PROMOTION_FILE).read_bytes())
        cls.archive = support.zip_directory(cls.directory / "promotion")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.world = self.finalize_world()

    def finalize_world(self, *, promotion: bytes | None = None) -> World:
        world = self.pub.world()
        self.pub.seed_pages(world, stage="finalize", promotion=self.archive if promotion is None else promotion)
        return world

    def refresh(self, key: str = "mc1.20.1", family: str | None = None, *, invocation: Any = None,
                world: World | None = None) -> RefreshResult:
        world = world or self.world
        job = "refresh" if family is None else "refresh-family"

        def forbidden(*arguments: Any, **options: Any) -> None:
            raise AssertionError("refresh must never run an adapter hook")

        with mock.patch.object(host, "call", side_effect=forbidden):
            return refresh_bundle(invocation or self.pub.invocation(job), api=world.api, key=key, family=family,
                                  input_dir=self.work / "cache")

    def rejected(self, fragment: str = "", *, reason: str | None = None, **options: Any) -> None:
        with self.assertRaises(MbError) as caught:
            self.refresh(**options)
        self.assertIn(fragment, str(caught.exception))
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertFalse((self.work / "cache").exists(), "a rejected refresh writes nothing")

    def jobs(self, change: Any) -> None:
        listed = self.pub.pages_jobs(self.world, stage="finalize")
        change(listed)
        self.world.jobs({"id": bs.PAGES_RUN, "run_attempt": 1}, listed)


class RefreshTest(RefreshFlow):
    def test_an_ordinary_key_rolls_its_exact_collected_bundle_forward(self) -> None:
        for key in bs.QS_KEYS:
            with self.subTest(key=key):
                self.setUp()
                result = self.refresh(key)
                commit = self.pub.mod.commit
                self.assertEqual(result, RefreshResult(available=True, cache_name=grammar.cache_name(key, commit),
                                                       baseline_name=grammar.baseline_name(key, commit, bs.E2E_RUN)))
                self.assertEqual(listing(self.work / "cache"), listing(self.pub.collected[key]))

    def test_a_family_leg_rolls_its_source_forward(self) -> None:
        result = self.refresh(bs.FAMILY_KEY, bs.FAMILY)
        self.assertEqual(result, RefreshResult(available=True, cache_name=grammar.family_cache_name(
            bs.FAMILY, bs.FAMILY_KEY, self.pub.mod.commit)))
        self.assertEqual(listing(self.work / "cache"), listing(self.pub.family_handoff))

    def test_a_family_leg_with_nothing_collected_writes_nothing(self) -> None:
        self.assertEqual(self.refresh("mc26.3", bs.FAMILY), RefreshResult(available=False, cache_name=None))
        self.assertFalse((self.work / "cache").exists())

    def test_no_baseline_is_named_when_the_archive_is_disabled(self) -> None:
        config = json.loads((self.pub.mod.root / "site" / "mod-base.json").read_text(encoding="utf-8"))
        config["baseline_archive"] = {"enabled": False}
        path = self.work / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        result = self.refresh(invocation=self.pub.invocation("refresh", config=path))
        self.assertIsNone(result.baseline_name)
        self.assertTrue(result.available)


class BaselineTest(RefreshFlow):
    """A complete generation is retained as its feature baseline once: when it is first published
    from its handoff. A republication, from its cache or from the same handoff before rotation
    retires it (a family wake), rolls the cache forward but names no second 90-day baseline."""

    KEY = "mc1.20.1"
    OWNER = 8800
    BASELINE_ID = 7401

    def baseline(self) -> str:
        return grammar.baseline_name(self.KEY, self.pub.mod.commit, bs.E2E_RUN)

    def retained(self, *, created: float = 565, expired: bool = False, job_conclusion: str = "success",
                 **run_changes: Any) -> dict[str, Any]:
        """The baseline an earlier Pages run retained in its refresh job's retention step (560-570)."""

        options = {"path": PAGES_WORKFLOW_PATH, "head_sha": self.pub.mod.commit, "created": 500,
                   "kit_sha": support.KIT_SHA, "title": "Project site", **run_changes}
        owner = self.world.run(self.OWNER, **options)
        self.world.jobs(owner, [self.world.job(api_job_name("finalize", "refresh", key=self.KEY), started=540,
                                               completed=600, conclusion=job_conclusion,
                                               steps=((step_name("baseline_upload"), 560, 570),))])
        return self.world.artifact(self.baseline(), owner, created=created, expired=expired,
                                   archive=self.pub.archives[f"collected:{self.KEY}"], artifact_id=self.BASELINE_ID)

    def baseline_listings(self) -> int:
        return len([call for call in self.world.api.calls if call[1].get("name") == self.baseline()])

    def test_the_first_publication_from_its_handoff_names_the_baseline(self) -> None:
        self.assertEqual(self.refresh(self.KEY).baseline_name, self.baseline())
        self.assertEqual(self.baseline_listings(), 1, "one exact-name listing looks for a retained baseline")

    def test_a_baseline_an_earlier_pages_run_retained_is_not_retained_again(self) -> None:
        self.retained()
        result = self.refresh(self.KEY)
        self.assertEqual(result, RefreshResult(available=True, baseline_name=None,
                                               cache_name=grammar.cache_name(self.KEY, self.pub.mod.commit)))
        self.assertEqual(listing(self.work / "cache"), listing(self.pub.collected[self.KEY]), "the cache still rolls")

    def test_an_upload_the_consumers_would_not_authenticate_is_no_retained_baseline(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "expired": {"expired": True},
            "fork": {"repository": "Someone/fork"},
            "failed owner": {"conclusion": "failure"},
            "running owner": {"status": "in_progress", "conclusion": None},
            "another workflow": {"path": ".github/workflows/other.yml"},
            "another branch": {"head_branch": "topic"},
            "outside the retention step": {"created": 580},
            "failed refresh job": {"job_conclusion": "failure"},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                self.setUp()
                self.retained(**changes)
                self.assertEqual(self.refresh(self.KEY).baseline_name, self.baseline())
        with self.subTest("this run's own upload"):
            self.setUp()
            pages = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
            self.world.artifact(self.baseline(), pages, created=1150, archive=self.pub.archives[f"collected:{self.KEY}"],
                                artifact_id=self.BASELINE_ID)
            self.assertEqual(self.refresh(self.KEY).baseline_name, self.baseline())

    def test_a_vanished_upload_is_skipped_and_other_api_failures_propagate(self) -> None:
        self.retained()
        path = f"/repos/{self.pub.mod.repository}/actions/artifacts/{self.BASELINE_ID}"
        original = github_artifacts.get_artifact

        def failing(error: ApiError) -> Any:
            def lookup(api: Any, artifact_id: int) -> Any:
                if artifact_id == self.BASELINE_ID:
                    raise error
                return original(api, artifact_id)
            return lookup

        with mock.patch.object(github_artifacts, "get_artifact",
                               failing(ApiNotFound("HTTP 404", status=404, method="GET", path=path))):
            self.assertEqual(self.refresh(self.KEY).baseline_name, self.baseline())
        self.setUp()
        self.retained()
        with mock.patch.object(github_artifacts, "get_artifact",
                               failing(ApiError("HTTP 502", status=502, method="GET", path=path))):
            self.rejected("HTTP 502", reason="github-api", key=self.KEY)

    def test_a_republication_from_its_cache_names_no_baseline(self) -> None:
        # The collect job re-authenticated the cache an earlier Pages run rolled forward; no
        # baseline of this generation exists any more (expired), and none is retained again.
        source = self.pub.world()
        owner = source.run(self.OWNER, path=PAGES_WORKFLOW_PATH, head_sha=self.pub.mod.commit, created=800,
                           kit_sha=support.KIT_SHA, title="Project site")
        cache = source.artifact(grammar.cache_name(self.KEY, self.pub.mod.commit), owner, created=900,
                                archive=self.pub.archives[f"collected:{self.KEY}"], artifact_id=7301)
        selected = Selected(kind="cache", artifact_id=cache["id"], name=cache["name"], digest=cache["digest"],
                            size=cache["size_in_bytes"], run_id=self.OWNER, run_attempt=1)
        collect = self.pub.invocation("collect")
        draft_path, collected = self.work / "draft.json", self.work / "collected-cache"
        with mock.patch.object(host, "call", support.InProcessHost(api=source.api)):
            draft = authenticate.authenticate_selection(collect, api=source.api, key=self.KEY,
                                                        selected_dir=self.pub.collected[self.KEY], selected=selected)
            draft_path.write_bytes(canonical_json(draft))
            compact_module.compact_bundle(collect, key=self.KEY, input_dir=self.pub.collected[self.KEY],
                                          selection_path=draft_path, output=collected)
        self.assertEqual(json.loads((collected / "selection.json").read_bytes())["selected_artifact"]["kind"], "cache")
        archive = support.zip_directory(collected)
        document = json.loads(json.dumps(self.promotion))
        entry = next(bundle for bundle in document["bundles"] if bundle["key"] == self.KEY)
        entry.update(collected_digest="sha256:" + bs.sha256(archive), selected_artifact_id=cache["id"],
                     manifest_sha256=bs.sha256((collected / "manifest.json").read_bytes()))
        promotion = self.work / "promotion"
        promotion.mkdir()
        (promotion / PROMOTION_FILE).write_bytes(canonical_json(document))
        self.world = self.finalize_world(promotion=support.zip_directory(promotion))
        self.world.artifact(grammar.collected_name(self.KEY),
                            {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"},
                            created=1050, archive=archive, artifact_id=entry["collected_artifact_id"])
        result = self.refresh(self.KEY)
        self.assertEqual(result, RefreshResult(available=True, baseline_name=None,
                                               cache_name=grammar.cache_name(self.KEY, self.pub.mod.commit)))
        self.assertEqual(listing(self.work / "cache"), listing(collected))
        self.assertEqual(self.baseline_listings(), 0, "a cache route never looks for a baseline")


class PrerequisiteTest(RefreshFlow):
    def test_the_exact_successful_build_and_deploy_are_required_before_any_download(self) -> None:
        for name in (api_job_name("publish", "build"), caller_job_name("deploy")):
            for label, change in (("missing", lambda jobs, n=name: jobs.remove(next(j for j in jobs if j["name"] == n))),
                                  ("failed", lambda jobs, n=name: next(j for j in jobs if j["name"] == n).update(
                                      conclusion="failure")),
                                  ("running", lambda jobs, n=name: next(j for j in jobs if j["name"] == n).update(
                                      status="in_progress", conclusion=None)),
                                  ("duplicated", lambda jobs, n=name: jobs.append(
                                      {**next(j for j in jobs if j["name"] == n), "id": 77777}))):
                with self.subTest(job=name, case=label):
                    self.setUp()
                    self.jobs(change)
                    self.rejected(reason="job-graph")
                    self.assertFalse(self.world.api.requests("/zip"))

    def test_only_this_attempts_refresh_job_may_refresh(self) -> None:
        for job, family in (("build", None), ("refresh", bs.FAMILY), ("refresh-family", None)):
            with self.subTest(job=job, family=family):
                self.rejected(family=family, invocation=self.pub.invocation(job))
        self.rejected(invocation=self.pub.invocation("refresh", GITHUB_WORKFLOW_REF="x/y/.github/workflows/pages.yml@refs/heads/master"))
        self.world.run(bs.PAGES_RUN, path=".github/workflows/pages.yml", head_sha=self.pub.mod.commit,
                       status="completed", conclusion="success", created=1000, kit_sha=bs.support.KIT_SHA)
        self.rejected(reason="current-run")

    def test_this_run_may_report_any_unfinished_status_while_siblings_wait_for_runners(self) -> None:
        # Canary run 36210848548: three sibling refresh jobs saw their own run as not in_progress
        # while other matrix jobs were queued; an unfinished, conclusionless run is still current.
        for status in ("queued", "waiting", "pending"):
            with self.subTest(status):
                self.setUp()
                self.world.run(bs.PAGES_RUN, path=".github/workflows/pages.yml", head_sha=self.pub.mod.commit,
                               status=status, conclusion=None, created=1000, kit_sha=bs.support.KIT_SHA)
                self.assertTrue(self.refresh().available)

    def test_the_upload_directory_must_be_new_and_the_key_published(self) -> None:
        (self.work / "cache").mkdir()
        with self.assertRaisesRegex(MbError, "must not exist"):
            self.refresh()
        (self.work / "cache").rmdir()
        self.rejected("publishes no bundle", key="mc9.9")
        self.rejected("no mod-compatibility leg", key="mc9.9", family=bs.FAMILY)


class PromotionTest(RefreshFlow):
    def promoted(self, change: Any) -> bytes:
        document = json.loads(json.dumps(self.promotion))
        change(document)
        root = self.work / "promotion"
        root.mkdir()
        (root / PROMOTION_FILE).write_bytes(canonical_json(document))
        return support.zip_directory(root)

    def test_the_promotion_is_this_runs_single_build_upload(self) -> None:
        run = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        cases = {
            "outside the build job": lambda: self.world.artifact(grammar.PROMOTION_NAME, run, created=1250,
                                                                 archive=self.archive, artifact_id=bs.PROMOTION_ID),
            "duplicated": lambda: self.world.artifact(grammar.PROMOTION_NAME, run, created=1150, archive=self.archive,
                                                      artifact_id=6399),
            "expired": lambda: self.world.artifact(grammar.PROMOTION_NAME, run, created=1150, archive=self.archive,
                                                   artifact_id=bs.PROMOTION_ID, expired=True),
            "another head": lambda: self.world.artifact(grammar.PROMOTION_NAME, {**run, "head_sha": "4" * 40},
                                                        created=1150, archive=self.archive, artifact_id=bs.PROMOTION_ID),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                change()
                self.rejected()

    def test_the_promotion_must_name_this_attempt_and_kit(self) -> None:
        cases = {
            "another run": lambda document: document["implementation"].update(run_id=9001),
            "another kit": lambda document: document["kit"].update(sha="5" * 40),
            "another collected artifact": lambda document: document["bundles"][0].update(collected_artifact_id=6100),
            "another manifest": lambda document: document["bundles"][0].update(manifest_sha256="6" * 64),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                self.world = self.finalize_world(promotion=self.promoted(change))
                self.rejected()

    def test_a_non_canonical_or_padded_promotion_is_refused(self) -> None:
        root = self.work / "promotion"
        root.mkdir()
        (root / PROMOTION_FILE).write_text(json.dumps(self.promotion, indent=2), encoding="utf-8")
        self.world = self.finalize_world(promotion=support.zip_directory(root))
        self.rejected("canonical", reason="promotion")
        self.setUp()
        (self.work / "extra").mkdir()
        (self.work / "extra" / PROMOTION_FILE).write_bytes(canonical_json(self.promotion))
        (self.work / "extra" / "other.json").write_bytes(b"{}")
        self.world = self.finalize_world(promotion=support.zip_directory(self.work / "extra"))
        self.rejected("exactly", reason="promotion")


class CollectedTest(RefreshFlow):
    def test_the_collected_artifact_must_be_the_promoted_bytes(self) -> None:
        run = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.collected_name("mc1.20.1"), run, created=1050,
                            archive=self.pub.archives["collected:mc26.3"], artifact_id=bs.COLLECTED_IDS["mc1.20.1"])
        self.rejected("not the collected artifact the promotion recorded", reason="artifact")
        self.setUp()
        self.world.artifact(grammar.collected_family_name(bs.FAMILY, bs.FAMILY_KEY), run, created=1060,
                            archive=self.pub.archives["collected:mc26.3"], artifact_id=bs.COLLECTED_FAMILY_ID)
        self.rejected(key=bs.FAMILY_KEY, family=bs.FAMILY)

    def test_the_collected_family_selection_is_the_promoted_generation(self) -> None:
        """A collected family artifact whose ``selected.json`` is missing or names another generation
        than the promotion's ``selected_artifact_id`` is refused, even under the promoted digest."""

        recorded = json.loads((self.pub.collected_family / "selected.json").read_bytes())
        self.assertEqual(recorded["artifact_id"], self.promotion["families"][0]["selected_artifact_id"])
        cases = {
            "another generation": ("selected another generation", lambda directory: (
                directory / "selected.json").write_bytes(canonical_json({**recorded, "artifact_id": 5301}))),
            "missing": ("holds", lambda directory: (directory / "selected.json").unlink()),
        }
        for label, (fragment, change) in cases.items():
            with self.subTest(label):
                self.setUp()
                variant = self.work / "variant"
                shutil.copytree(self.pub.collected_family, variant)
                change(variant)
                archive = support.zip_directory(variant)
                document = json.loads(json.dumps(self.promotion))
                document["families"][0]["collected_digest"] = "sha256:" + bs.sha256(archive)
                promotion = self.work / "promotion"
                promotion.mkdir()
                (promotion / PROMOTION_FILE).write_bytes(canonical_json(document))
                self.world = self.finalize_world(promotion=support.zip_directory(promotion))
                self.world.artifact(grammar.collected_family_name(bs.FAMILY, bs.FAMILY_KEY),
                                    {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"},
                                    created=1060, archive=archive, artifact_id=bs.COLLECTED_FAMILY_ID)
                self.rejected(fragment, reason="artifact", key=bs.FAMILY_KEY, family=bs.FAMILY)

    def test_a_moved_head_keeps_the_cache_from_rolling_forward(self) -> None:
        self.world.api.set_branch("master", "7" * 40, "8" * 40)
        with self.assertRaises(MbError):
            self.refresh()
        self.assertFalse((self.work / "cache").exists())


class DriftTest(RefreshFlow):
    """BP ``test_post_validation_job_source_bytes_inventory_and_directory_drift_fail``: whatever
    changes between the validation and the publication of the upload directory fails closed."""

    def stage(self) -> Path:
        (stage,) = self.work.glob(".cache.building-*")
        return stage

    def mutate(self, mutation: str) -> None:
        run = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        if mutation == "job":
            self.jobs(lambda jobs: next(job for job in jobs if job["name"] == caller_job_name("deploy")).update(
                conclusion="failure"))
        elif mutation == "job id":
            self.jobs(lambda jobs: next(job for job in jobs if job["name"] == api_job_name("publish", "build")).update(
                id=77777))
        elif mutation == "artifact":
            self.world.artifact(grammar.collected_name("mc1.20.1"), run, created=1050, expired=True,
                                archive=self.pub.archives["collected:mc1.20.1"], artifact_id=bs.COLLECTED_IDS["mc1.20.1"])
        elif mutation == "promotion":
            self.world.artifact(grammar.PROMOTION_NAME, run, created=1160, archive=self.archive, artifact_id=6398)
        elif mutation == "head":
            self.world.api.set_branch("master", "7" * 40, self.pub.mod.tree)
        elif mutation == "bytes":
            image = next((self.stage() / "images").iterdir())
            with image.open("r+b") as stream:
                stream.write(b"x")
        elif mutation == "extra":
            (self.stage() / "extra").write_bytes(b"x")
        elif mutation == "link":
            (self.stage() / "extra").symlink_to(next((self.stage() / "images").iterdir()))
        elif mutation == "directory":
            (self.stage() / "empty").mkdir()
        else:
            held = self.stage().with_name("held-cache")
            self.stage().rename(held)
            shutil.copytree(held, self.work / held.name.replace("held-cache", self.stage_name))

    def test_post_validation_run_job_artifact_head_bytes_inventory_and_directory_drift_fail(self) -> None:
        from mod_base.pages import refresh as refresh_module

        original = refresh_module.jobs.attempt_jobs
        for mutation in ("job", "job id", "artifact", "promotion", "head", "bytes", "extra", "link",
                         "directory", "rename"):
            with self.subTest(mutation):
                self.setUp()
                observed = 0

                def attempt_jobs(api: Any, run_id: int, attempt: int, mutation: str = mutation) -> Any:
                    nonlocal observed
                    observed += 1
                    if observed == 2:
                        if mutation == "rename":
                            self.stage_name = self.stage().name
                        self.mutate(mutation)
                    return original(api, run_id, attempt)

                with mock.patch.object(refresh_module.jobs, "attempt_jobs", side_effect=attempt_jobs):
                    self.rejected()
                self.assertEqual(observed, 2, "the recheck observes the attempt again after validation")

    def test_the_recheck_runs_after_the_bytes_are_sealed(self) -> None:
        from mod_base.pages import refresh as refresh_module

        original = refresh_module.jobs.attempt_jobs
        staged: list[dict[str, bytes]] = []

        def attempt_jobs(api: Any, run_id: int, attempt: int) -> Any:
            stages = list(self.work.glob(".cache.building-*"))
            staged.append(listing(stages[0]) if stages else {})
            return original(api, run_id, attempt)

        with mock.patch.object(refresh_module.jobs, "attempt_jobs", side_effect=attempt_jobs):
            self.refresh()
        self.assertEqual(staged[0], {}, "nothing is staged before the first observation")
        self.assertEqual(staged[1], listing(self.work / "cache"), "the recheck sees the complete sealed stage")
        self.assertEqual(listing(self.work / "cache"), listing(self.pub.collected["mc1.20.1"]))


class ConcurrentUploadTest(RefreshFlow):
    """The canary's defect (run 36190041285): ``Finalize / Refresh evidence cache for mc1.20.1`` failed
    closed on ``listing total_count 6 disagrees with 5 listed rows`` while the sibling finalize jobs
    of the same run uploaded their caches, and the generation lost its cache refresh and rotation.
    Refresh now reads only its own names, each by exact name, and reads a listing GitHub serves
    inconsistently again within its budget; one that stays inconsistent still fails closed."""

    KEY = "mc1.20.1"

    def run_artifacts(self) -> str:
        return f"/repos/{self.pub.mod.repository}/actions/runs/{bs.PAGES_RUN}/artifacts"

    def sibling_upload(self, key: str, artifact_id: int) -> None:
        """A sibling ``Finalize / Refresh`` job's cache, uploaded into this run while it runs."""

        run = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.cache_name(key, self.pub.mod.commit), run, created=1300,
                            archive=self.pub.archives[f"collected:{key}"], artifact_id=artifact_id)

    def expected(self) -> RefreshResult:
        commit = self.pub.mod.commit
        return RefreshResult(available=True, cache_name=grammar.cache_name(self.KEY, commit),
                             baseline_name=grammar.baseline_name(self.KEY, commit, bs.E2E_RUN))

    def test_the_canary_inconsistency_is_read_again_and_the_cache_rolls_forward(self) -> None:
        baseline = self.refresh(self.KEY)
        reads = self.world.api.request_count
        self.setUp()
        self.sibling_upload("mc26.3", 7501)
        # An upload in flight is counted before it is listed, for the first listing the job reads.
        self.world.api.skew_listing(self.run_artifacts(), responses=1)
        self.assertEqual(self.refresh(self.KEY), baseline)
        self.assertEqual(listing(self.work / "cache"), listing(self.pub.collected[self.KEY]))
        self.assertEqual(self.world.api.sleeps, [2.0])
        self.assertEqual(self.world.api.request_count, reads + 1, "one re-read within the budget")
        self.assertLessEqual(self.world.api.request_count, MAX_PAGES_API_READS)

    def test_every_listing_of_this_run_is_by_exact_name(self) -> None:
        self.sibling_upload("mc26.3", 7501)
        self.assertEqual(self.refresh(self.KEY), self.expected())
        listed = self.world.api.requests(f"/actions/runs/{bs.PAGES_RUN}/artifacts")
        self.assertEqual(len(listed), 4, "the promotion and the collected artifact, observed and rechecked")
        self.assertEqual({call[1].get("name") for call in listed},
                         {grammar.PROMOTION_NAME, grammar.collected_name(self.KEY)})

    def test_a_sibling_upload_landing_mid_refresh_changes_nothing(self) -> None:
        for after in (1, 2, 3):
            with self.subTest(after_pages=after):
                self.setUp()
                self.world.api.during_listing(self.run_artifacts(), lambda: self.sibling_upload("mc26.3", 7501),
                                              after_pages=after)
                self.assertEqual(self.refresh(self.KEY), self.expected())
                self.assertEqual(self.world.api.sleeps, [], "an exact-name listing never saw the sibling upload")
        self.setUp()
        self.world.api.during_listing(self.run_artifacts(), lambda: self.sibling_upload(bs.FAMILY_KEY, 7502))
        self.world.api.skew_listing(self.run_artifacts(), responses=2)
        self.assertEqual(self.refresh(bs.FAMILY_KEY, bs.FAMILY).cache_name,
                         grammar.family_cache_name(bs.FAMILY, bs.FAMILY_KEY, self.pub.mod.commit))
        self.assertEqual(self.world.api.sleeps, [2.0, 4.0])

    def test_a_listing_that_stays_inconsistent_fails_closed_and_writes_nothing(self) -> None:
        self.world.api.skew_listing(self.run_artifacts(), responses=LISTING_READ_ATTEMPTS)
        self.rejected("total_count 2 disagrees with 1 listed rows (the last of 4 inconsistent reads)",
                      reason="github-api", key=self.KEY)
        self.assertFalse(self.world.api.requests("/zip"), "nothing is downloaded from an inconsistent inventory")
        self.assertEqual(self.world.api.sleeps, [2.0, 4.0, 8.0])
        # The recheck reads with the same rule: a listing inconsistent at the post-validation recheck
        # is read again, and one that stays so leaves no upload directory.
        from mod_base.pages import refresh as refresh_module

        original = refresh_module.jobs.attempt_jobs
        for responses, succeeds in ((1, True), (LISTING_READ_ATTEMPTS, False)):
            with self.subTest(recheck_responses=responses):
                self.setUp()
                observed = 0

                def attempt_jobs(api: Any, run_id: int, attempt: int, responses: int = responses) -> Any:
                    nonlocal observed
                    observed += 1
                    if observed == 2:
                        self.world.api.skew_listing(self.run_artifacts(), responses=responses)
                    return original(api, run_id, attempt)

                with mock.patch.object(refresh_module.jobs, "attempt_jobs", side_effect=attempt_jobs):
                    if succeeds:
                        self.assertEqual(self.refresh(self.KEY), self.expected())
                    else:
                        self.rejected("disagrees", reason="github-api", key=self.KEY)


class CommandTest(RefreshFlow):
    def run_command(self, *extra: str) -> tuple[int, str, list[str]]:
        output = self.work / "github-output"
        job = "refresh-family" if "--family" in extra else "refresh"
        with mock.patch.object(cli, "environ", return_value=self.pub.environment(job)), \
                mock.patch.object(commands_build.github_api, "from_environment", return_value=self.world.api), \
                mock.patch.object(commands_build.build, "check_checkouts"), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cli.main(["refresh", "--repo", str(self.pub.mod.root), "--input", str(self.work / "cache"),
                             "--github-output", str(output), *extra])
        lines = output.read_text(encoding="utf-8").splitlines() if output.exists() else []
        return code, stderr.getvalue(), lines

    def test_the_refresh_command_names_the_cache_and_baseline(self) -> None:
        code, stderr, lines = self.run_command("--key", "mc1.20.1")
        self.assertEqual(code, 0, stderr)
        commit = self.pub.mod.commit
        self.assertEqual(lines, ["available=true", f"cache_name={grammar.cache_name('mc1.20.1', commit)}",
                                 f"baseline_name={grammar.baseline_name('mc1.20.1', commit, bs.E2E_RUN)}"])

    def test_the_refresh_command_omits_a_baseline_an_earlier_run_retained(self) -> None:
        commit = self.pub.mod.commit
        owner = self.world.run(8800, path=PAGES_WORKFLOW_PATH, head_sha=commit, created=500, kit_sha=support.KIT_SHA,
                               title="Project site")
        self.world.jobs(owner, [self.world.job(api_job_name("finalize", "refresh", key="mc1.20.1"), started=540,
                                               completed=600, steps=((step_name("baseline_upload"), 560, 570),))])
        self.world.artifact(grammar.baseline_name("mc1.20.1", commit, bs.E2E_RUN), owner, created=565,
                            archive=self.pub.archives["collected:mc1.20.1"])
        code, stderr, lines = self.run_command("--key", "mc1.20.1")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines, ["available=true", f"cache_name={grammar.cache_name('mc1.20.1', commit)}"])

    def test_an_unavailable_family_leg_exits_zero_without_a_cache(self) -> None:
        code, stderr, lines = self.run_command("--key", "mc26.3", "--family", bs.FAMILY)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines, ["available=false", "cache_name="])


if __name__ == "__main__":
    unittest.main()
