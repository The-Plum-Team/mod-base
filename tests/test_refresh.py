"""``refresh`` (SPEC §5.4): the promoted bundle revalidated in its own Pages attempt and written as the
exact bytes of its rolling cache (and baseline), or of a family cache.

Ports Block Pops ``test_pages_refresh_cache`` (the exact successful build and deploy are required
before any download; unknown or cross-branch requests never emit a result; post-validation drift of
jobs, source or bytes fails) and the Quick Skin refresh-cache step (the live head must still be the
coverage; the complete generation is retained as the feature baseline) onto the v1 promotion.
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
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.pages import commands_build
from mod_base.pages.build import PROMOTION_FILE, build_site
from mod_base.pages.refresh import RefreshResult, refresh_bundle
from mod_base.workflow import api_job_name, caller_job_name
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

    def test_an_unavailable_family_leg_exits_zero_without_a_cache(self) -> None:
        code, stderr, lines = self.run_command("--key", "mc26.3", "--family", bs.FAMILY)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(lines, ["available=false", "cache_name="])


if __name__ == "__main__":
    unittest.main()
