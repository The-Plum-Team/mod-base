"""``build`` (SPEC §5.3.2): the current-attempt checks against exact job names, the collected
artifacts of this run, the live heads, the re-authenticated selections, families (their producer
run, run record and kit, SPEC §1.8, and the generation each leg was collected from, carried ones
included), the ``verify_publication`` veto, the promotion, the ``build`` command and the Pages API
budget at Quick Skin's scale.

Ports Block Pops ``test_pages_site_companions`` (the owner invocation, jobs and companion identity
cannot be substituted; final API and source changes fail before publication) and the Quick Skin
``--expected-bundles-json`` exact key set onto the v1 flow with a seeded ``FakeGitHub``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli, workflow
from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.github import jobs as github_jobs
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.pages import build, commands_build
from mod_base.pages.build import PROMOTION_FILE, build_site, check_checkouts
from mod_base.pages.select import family_archive_limit
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, caller_job_name
from tests import test_build_support as bs
from tests.fixtures.mods import support
from tests.test_select import FAMILY_JOB, World, at

COLLECT = {key: api_job_name("publish", "collect", key=key) for key in bs.QS_KEYS}
FAMILY_JOBS = {key: api_job_name("publish", "family", family=bs.FAMILY, key=key) for key in bs.QS_KEYS}


class BuildFlow(unittest.TestCase):
    #: The ``bs.Publication`` options of this class's fixture and the family generation it publishes.
    fixture_options: dict[str, Any] = {}
    generation = bs.DIRECT_GENERATION

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-flow-")).resolve()
        cls.pub = bs.Publication(cls.directory / "publication", **cls.fixture_options)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.world = self.pub.world(stage="build", generation=self.generation)

    def build(self, *, world: World | None = None, invocation: Any = None, hooks: Any = None) -> build.BuildResult:
        world = world or self.world
        with mock.patch.object(host, "call", hooks or support.InProcessHost(api=world.api)):
            return build_site(invocation or self.pub.invocation(), api=world.api, kit_root=self.pub.kit,
                              collected_dir=self.work / "collected", families_dir=self.work / "families",
                              output=self.work / "_site", promotion_dir=self.work / "promotion")

    def rejected(self, fragment: str = "", *, reason: str | None = None, **options: Any) -> MbError:
        with self.assertRaises(MbError) as caught:
            self.build(**options)
        self.assertIn(fragment, str(caught.exception))
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason)
        self.assertFalse((self.work / "_site").exists(), "a rejected build publishes nothing")
        self.assertFalse((self.work / "promotion").exists())
        return caught.exception

    def pages_run(self, world: World | None = None, **changes: Any) -> dict[str, Any]:
        world = world or self.world
        options = {"path": ".github/workflows/pages.yml", "head_sha": self.pub.mod.commit, "status": "in_progress",
                   "conclusion": None, "created": 1000, "kit_sha": support.KIT_SHA, "title": "Project site", **changes}
        return world.run(bs.PAGES_RUN, **options)

    def jobs(self, change: Any) -> None:
        listed = self.pub.pages_jobs(self.world, stage="build")
        change(listed)
        self.world.jobs({"id": bs.PAGES_RUN, "run_attempt": 1}, listed)

    def fork(self, addition: str) -> Any:
        """A copy of the fixture repository whose adapter gains ``addition``."""

        root = self.work / "fork"
        shutil.copytree(self.pub.mod.root, root, symlinks=True)
        adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
        adapter.write_text(adapter.read_text(encoding="utf-8") + addition, encoding="utf-8")
        return build_invocation(root, None, self.pub.environment("build"), root=self.pub.kit)


class PublishTest(BuildFlow):
    def test_the_site_and_the_final_promotion_are_published(self) -> None:
        hooks = support.InProcessHost(api=self.world.api)
        result = self.build(hooks=hooks)
        promotion = result.promotion
        documents.validate_promotion(promotion)
        written = (self.work / "promotion" / PROMOTION_FILE).read_bytes()
        self.assertEqual(written, canonical_json(promotion))
        self.assertEqual(os.listdir(self.work / "promotion"), [PROMOTION_FILE])
        self.assertEqual(result.heads, {"master": self.pub.mod.commit})
        self.assertEqual(promotion["heads"], result.heads)
        self.assertEqual(promotion["implementation"]["run_id"], bs.PAGES_RUN)
        record = json.loads((self.work / "_site" / "build.json").read_bytes())
        self.assertEqual(result.site_sha256, record["site_inventory_sha256"])
        self.assertEqual(result.site_sha256, promotion["site"]["inventory_sha256"])
        files = [path for path in (self.work / "_site").rglob("*") if path.is_file()]
        self.assertEqual(promotion["site"]["files"], len(files) - 1)
        self.assertEqual(promotion["site"]["bytes"], sum(path.stat().st_size for path in files
                                                         if path.name != "build.json" or path.parent.name != "_site"))
        self.assertEqual([(bundle["key"], bundle["collected_artifact_id"], bundle["selected_artifact_id"])
                          for bundle in promotion["bundles"]],
                         [(key, bs.COLLECTED_IDS[key], bs.HANDOFF_IDS[key]) for key in bs.QS_KEYS])
        self.assertEqual(promotion["families"], [
            {"family": bs.FAMILY, "key": "mc1.20.1", "available": True, "status": "available",
             "collected_artifact_id": bs.COLLECTED_FAMILY_ID,
             "collected_digest": "sha256:" + bs.sha256(self.pub.archives["collected-family"]),
             "coverage_sha": self.pub.mod.commit, "selected_artifact_id": bs.FAMILY_HANDOFF_ID},
            {"family": bs.FAMILY, "key": "mc26.3", "available": False, "status": "unavailable"}])
        for bundle in promotion["bundles"]:
            manifest = (self.work / "collected" / bundle["key"] / "manifest.json").read_bytes()
            self.assertEqual(bundle["manifest_sha256"], bs.sha256(manifest))
        self.assertTrue((self.work / "families" / bs.FAMILY / "mc1.20.1" / "paired.json").is_file())
        self.assertEqual({hook for hook, _ in hooks.calls}, {"targets", "expectation", "verify_publication"})
        self.assertIn(("verify_publication", True), hooks.calls)

    def test_the_api_budget_is_bounded_and_reads_are_memoized(self) -> None:
        self.build()
        api = self.world.api
        self.assertLessEqual(api.request_count, lim.MAX_PAGES_API_READS)
        self.assertLessEqual(api.request_count, 40)
        self.assertEqual(len(api.requests(f"/contents/{bs.SOURCE}")), 1, "the handoff pin is read once")
        self.assertEqual(len([call for call in api.calls if call[0].endswith(f"/actions/runs/{bs.E2E_RUN}/attempts/1")]),
                         1, "the handoff run attempt is read once for both keys")
        self.assertEqual(len(api.requests(f"/actions/runs/{bs.PAGES_RUN}/attempts/1/jobs")), 2, "observe + recheck")
        self.assertEqual(len(api.requests(f"/contents/{bs.FAMILY_WORKFLOW}")), 1, "the family kit pin is read once")
        self.assertEqual(len(api.requests(f"/actions/runs/{bs.PRODUCER_RUN}/attempts/1")), 1,
                         "the producer attempt is read once")

    def test_a_missing_family_leg_is_published_as_unavailable(self) -> None:
        world = self.pub.world(stage="build", family=False)
        result = self.build(world=world)
        self.assertEqual([entry["available"] for entry in result.promotion["families"]], [False, False])
        gallery = json.loads((self.work / "_site" / "e2e" / "gallery-data.json").read_bytes())
        self.assertEqual(gallery["families"][0]["status"], "unavailable")

    def test_output_directories_must_be_new(self) -> None:
        (self.work / "_site").mkdir()
        with self.assertRaisesRegex(MbError, "must not exist"):
            self.build()
        self.assertEqual(self.world.api.request_count, 0)
        os.rmdir(self.work / "_site")
        (self.work / "promotion").mkdir()
        with self.assertRaisesRegex(MbError, "must not exist"):
            self.build()


class InvocationTest(BuildFlow):
    def test_only_the_canonical_pages_build_job_may_build(self) -> None:
        cases = {
            "workflow ref": {"GITHUB_WORKFLOW_REF": f"{self.pub.mod.repository}/.github/workflows/other.yml@refs/heads/master"},
            "tag ref": {"GITHUB_REF": "refs/tags/v1.0.0"},
            "branch": {"GITHUB_REF": "refs/heads/feature"},
            "job": {"GITHUB_JOB": "collect"},
            "run id": {"GITHUB_RUN_ID": "0"},
            "attempt": {"GITHUB_RUN_ATTEMPT": "x"},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                self.rejected(invocation=self.pub.invocation(**changes))
        self.assertEqual(self.world.api.request_count, 0)

    def test_the_kit_root_is_the_executing_kit(self) -> None:
        other = bs.copy_kit(self.work / "other-kit")
        with self.assertRaisesRegex(MbError, "--kit-root"):
            with mock.patch.object(host, "call", support.InProcessHost(api=self.world.api)):
                build_site(self.pub.invocation(), api=self.world.api, kit_root=other,
                           collected_dir=self.work / "collected", families_dir=self.work / "families",
                           output=self.work / "_site", promotion_dir=self.work / "promotion")

    def test_the_run_must_be_the_in_progress_attempt_of_the_protected_head(self) -> None:
        cases = {
            "completed": {"status": "completed", "conclusion": "success"},
            "cancelled": {"conclusion": "cancelled"},
            "event": {"event": "push"},
            "head": {"head_sha": "0" * 40},
            "branch": {"head_branch": "release"},
            "workflow": {"path": ".github/workflows/other.yml"},
            "fork": {"repository": "Someone/fork"},
            "kit": {"kit_sha": "1" * 40},
            "newer attempt": {"attempt": 2, "earlier": ({"run_attempt": 1},)},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                self.world = self.pub.world(stage="build")
                self.pages_run(**changes)
                self.rejected()

    def test_the_default_branch_must_be_the_canonical_branch(self) -> None:
        repository = self.pub.mod.repository
        self.world.api.add_response(f"/repos/{repository}", {"full_name": repository, "default_branch": "main"})
        self.rejected("default branch", reason="canonical-branch")


class JobGraphTest(BuildFlow):
    def test_every_publication_job_is_required_exactly_once_and_successful(self) -> None:
        names = [caller_job_name("verify_kit"), api_job_name("publish", "admit"), *COLLECT.values(),
                 *FAMILY_JOBS.values()]
        for name in names:
            for label, change in (("missing", lambda jobs, n=name: jobs.remove(next(j for j in jobs if j["name"] == n))),
                                  ("failed", lambda jobs, n=name: next(j for j in jobs if j["name"] == n).update(
                                      conclusion="failure")),
                                  ("duplicated", lambda jobs, n=name: jobs.append(
                                      {**next(j for j in jobs if j["name"] == n), "id": 88888}))):
                with self.subTest(job=name, case=label):
                    self.setUp()
                    self.jobs(change)
                    self.rejected(reason="job-graph")

    def test_the_build_job_is_in_progress_and_no_stray_collect_job_ran(self) -> None:
        self.jobs(lambda jobs: jobs[-1].update(status="completed", conclusion="success", completed_at=at(1200)))
        self.rejected("not in progress", reason="job-graph")
        self.setUp()
        self.jobs(lambda jobs: jobs.append(self.world.job(api_job_name("publish", "collect", key="mc9.9"),
                                                          started=1020, completed=1100)))
        self.rejected("outside the publication", reason="job-graph")


class NoFamilyJobGraphTest(BuildFlow):
    """A mod without families (Block Pops): ``admit`` outputs ``families == []``, so the ``family``
    matrix job is skipped by its job-level ``if`` before its matrix expands, and the jobs API reports
    it once under its unexpanded template name (Quick Skin ``e2e_job_graph.UNEXPANDED_SCENARIO_JOB``).
    ``build`` accepts exactly that job, only ``completed/skipped`` and only without family legs."""

    SKIPPED = "Publish / Collect ${{ matrix.family }} ${{ matrix.key }}"

    def setUp(self) -> None:
        super().setUp()
        config = json.loads((self.pub.mod.root / "site" / "mod-base.json").read_text(encoding="utf-8"))
        config["families"] = []
        config["copy"]["family_notes"] = {}
        path = self.work / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.invocation = self.pub.invocation(config=path)
        self.world = self.pub.world(stage="build", family=False)

    def skipped(self, **changes: Any) -> dict[str, Any]:
        return {"id": 79998, "name": self.SKIPPED, "status": "completed", "conclusion": "skipped",
                "started_at": at(1020), "completed_at": at(1020), "steps": [], "run_attempt": 1, **changes}

    def family_jobs(self, *extra: dict[str, Any]) -> None:
        """This attempt's jobs without any expanded family leg, plus ``extra``."""

        listed = [job for job in self.pub.pages_jobs(self.world, stage="build")
                  if job["name"] not in FAMILY_JOBS.values()]
        self.world.jobs({"id": bs.PAGES_RUN, "run_attempt": 1}, [*listed, *extra])

    def test_the_name_is_the_unexpanded_family_template(self) -> None:
        self.assertEqual(workflow.unexpanded_api_job_name("publish", "family"), self.SKIPPED)
        publish = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "publish.yml").read_text(
            encoding="utf-8")
        self.assertIn("    name: Collect ${{ matrix.family }} ${{ matrix.key }}\n", publish)

    def test_the_skipped_family_job_of_a_publication_without_families_is_accepted(self) -> None:
        self.family_jobs(self.skipped())
        result = self.build(invocation=self.invocation)
        self.assertEqual(result.promotion["families"], [])
        self.assertEqual([bundle["key"] for bundle in result.promotion["bundles"]], list(bs.QS_KEYS))

    def test_a_listing_without_it_is_accepted(self) -> None:
        self.family_jobs()
        self.assertEqual(self.build(invocation=self.invocation).promotion["families"], [])

    def test_only_one_completed_skipped_job_of_this_attempt_is_accepted(self) -> None:
        for label, extra, fragment in (
                ("ran", (self.skipped(conclusion="success"),), "was not skipped"),
                ("running", (self.skipped(status="in_progress", conclusion=None),), "was not skipped"),
                ("failed", (self.skipped(conclusion="failure"),), "was not skipped"),
                ("duplicated", (self.skipped(), self.skipped(id=79997)), "expected exactly one job"),
                ("other attempt", (self.skipped(run_attempt=2),), "does not belong to attempt")):
            with self.subTest(case=label):
                self.setUp()
                self.family_jobs(*extra)
                self.rejected(fragment, reason="job-graph", invocation=self.invocation)

    def test_with_family_legs_the_unexpanded_name_is_a_stray(self) -> None:
        self.world = self.pub.world(stage="build")
        self.jobs(lambda jobs: jobs.append(self.skipped()))
        self.rejected("outside the publication", reason="job-graph")


class ArtifactTest(BuildFlow):
    def collected(self, key: str = "mc1.20.1", **changes: Any) -> None:
        run = {"id": bs.PAGES_RUN, "head_sha": changes.pop("head_sha", self.pub.mod.commit), "head_branch": "master"}
        self.world.artifact(changes.pop("name", grammar.collected_name(key)), run,
                            created=changes.pop("created", 1050), archive=self.pub.archives[f"collected:{key}"],
                            artifact_id=changes.pop("artifact_id", bs.COLLECTED_IDS[key]), **changes)

    def test_each_collected_bundle_exists_exactly_once_inside_its_collect_job(self) -> None:
        self.collected(created=1101)
        self.rejected("inside its collect job", reason="artifact")
        self.setUp()
        self.collected(artifact_id=6199)
        self.rejected("exactly one", reason="artifact")
        self.setUp()
        self.collected(expired=True)
        self.rejected("exactly one", reason="artifact")
        self.setUp()
        self.collected(head_sha="0" * 40)
        self.rejected("another head", reason="artifact")
        self.setUp()
        self.collected(name=grammar.collected_name("mc9.9"), artifact_id=6198)
        self.rejected("outside the publication", reason="artifact")

    def test_a_collected_family_artifact_is_bound_to_its_family_job(self) -> None:
        run = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.collected_family_name(bs.FAMILY, "mc1.20.1"), run, created=1000,
                            archive=self.pub.archives["collected-family"], artifact_id=bs.COLLECTED_FAMILY_ID)
        self.rejected("inside its collect job", reason="artifact")

    def test_a_tampered_collected_bundle_is_refused(self) -> None:
        tampered = self.work / "tampered"
        shutil.copytree(self.pub.collected["mc26.3"], tampered)
        manifest = json.loads((tampered / "manifest.json").read_bytes())
        manifest["frames"][0]["title"] = "Another title"
        (tampered / "manifest.json").write_bytes(canonical_json(manifest))
        self.world.artifact(grammar.collected_name("mc26.3"), {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit,
                                                               "head_branch": "master"},
                            created=1050, archive=support.zip_directory(tampered), artifact_id=bs.COLLECTED_IDS["mc26.3"])
        self.rejected()

    def test_an_unclean_family_projection_is_refused(self) -> None:
        tampered = self.work / "family"
        shutil.copytree(self.pub.collected_family, tampered)
        projection = json.loads((tampered / "paired.json").read_bytes())
        projection["lanes"][0]["pairs"][0]["verdict"]["defect"] = True
        (tampered / "paired.json").write_bytes(canonical_json(projection))
        self.world.artifact(grammar.collected_family_name(bs.FAMILY, "mc1.20.1"),
                            {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"},
                            created=1060, archive=support.zip_directory(tampered), artifact_id=bs.COLLECTED_FAMILY_ID)
        self.rejected("publishable")


class HeadTest(BuildFlow):
    def test_a_moved_head_fails_before_any_download(self) -> None:
        self.world.api.set_branch("master", "0" * 40, "1" * 40)
        self.rejected(reason="stale-subject")
        self.assertFalse((self.work / "collected").exists())

    def test_a_head_that_moves_while_rendering_keeps_the_previous_site(self) -> None:
        original = build.render_site

        def moved(*arguments: Any, **options: Any) -> dict[str, bytes]:
            written = original(*arguments, **options)
            self.world.api.set_branch("master", "0" * 40, "1" * 40)
            return written

        with mock.patch.object(build, "render_site", side_effect=moved):
            self.rejected("advanced past the published head", reason="stale-subject")

    def test_a_job_or_artifact_change_while_rendering_keeps_the_previous_site(self) -> None:
        original = build.render_site

        def changed(*arguments: Any, **options: Any) -> dict[str, bytes]:
            written = original(*arguments, **options)
            self.world.artifact(grammar.collected_name("mc26.3"),
                                {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"},
                                created=1051, archive=self.pub.archives["collected:mc26.3"],
                                artifact_id=bs.COLLECTED_IDS["mc26.3"])
            return written

        with mock.patch.object(build, "render_site", side_effect=changed):
            self.rejected("changed while the site was rendered")


class SelectionTest(BuildFlow):
    def e2e(self, **changes: Any) -> dict[str, Any]:
        return self.world.run(bs.E2E_RUN, **{"head_sha": self.pub.mod.commit, "created": 0, **changes})

    def test_the_selected_handoff_is_re_authenticated_by_id(self) -> None:
        e2e = {"id": bs.E2E_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        cases = {
            "expired": lambda: self.world.artifact(grammar.handoff_name("mc1.20.1", 1), e2e, created=300, expired=True,
                                                   archive=self.pub.archives["mc1.20.1"],
                                                   artifact_id=bs.HANDOFF_IDS["mc1.20.1"]),
            "digest": lambda: self.world.artifact(grammar.handoff_name("mc1.20.1", 1), e2e, created=300,
                                                  archive=b"PK\x05\x06" + bytes(18),
                                                  artifact_id=bs.HANDOFF_IDS["mc1.20.1"]),
            "created": lambda: self.world.artifact(grammar.handoff_name("mc1.20.1", 1), e2e, created=305,
                                                   archive=self.pub.archives["mc1.20.1"],
                                                   artifact_id=bs.HANDOFF_IDS["mc1.20.1"]),
            "upload window": lambda: self.world.jobs(e2e | {"run_attempt": 1}, [
                self.world.job(bs.QS_HANDOFF_JOB.replace("{key}", key), steps=((bs.QS_HANDOFF_STEP, 500, 510),))
                for key in bs.QS_KEYS]),
            "run title": lambda: self.e2e(title="Another title"),
            "run event": lambda: self.e2e(event="schedule"),
            "run created": lambda: self.e2e(created=5),
            "run failed": lambda: self.e2e(conclusion="failure"),
            "kit pin": lambda: self.world.api.add_file(self.pub.mod.commit, bs.SOURCE, bs.pin_workflow("2" * 40)),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                change()
                self.rejected()

    def test_the_recomputed_selection_equals_the_control_planes_draft(self) -> None:
        """The draft the MB5 ``authenticate`` writes for the same facts is what ``build`` recomputes."""

        from mod_base.pages import authenticate
        from mod_base.pages.select import Selected

        world = self.pub.world()
        world.api.add_response(f"/repos/{self.pub.mod.repository}/actions/workflows/on-demand-e2e.yml",
                               {"id": 77, "path": bs.SOURCE, "state": "active"})
        artifact = self.pub.seeds["mc1.20.1"]
        selected = Selected(kind="handoff", artifact_id=artifact["id"], name=artifact["name"],
                            digest=artifact["digest"], size=artifact["size_in_bytes"], run_id=bs.E2E_RUN, run_attempt=1)
        with mock.patch.object(host, "call", support.InProcessHost(api=world.api)):
            draft = authenticate.authenticate_selection(self.pub.invocation("collect"), api=world.api, key="mc1.20.1",
                                                        selected_dir=self.pub.handoffs["mc1.20.1"], selected=selected)
        embedded = json.loads((self.pub.collected["mc1.20.1"] / "selection.json").read_bytes())
        self.assertEqual(draft, {key: value for key, value in embedded.items()
                                 if key not in documents.SELECTION_COMPLETION_FIELDS})


class FamilySelectionTest(BuildFlow):
    """The collected family artifact records, as ``selected.json``, the generation its ``select``
    step chose; ``build`` re-authenticates that artifact by id and never walks again (MB6-1, MB6-4)."""

    def producer_handoff(self, **changes: Any) -> dict[str, Any]:
        """Re-seed the producer attempt's family handoff with ``changes``."""

        producer = {"id": bs.PRODUCER_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        archive = changes.pop("archive", self.pub.archives[bs.FAMILY])
        return self.world.artifact(grammar.family_handoff_name(bs.FAMILY, bs.FAMILY_KEY, 1), producer, created=400,
                                   archive=archive, artifact_id=bs.FAMILY_HANDOFF_ID, **changes)

    def owned_cache(self, *, commit: str | None = None, **run_changes: Any) -> dict[str, Any]:
        """The direct generation rolled forward as the family cache named with ``commit`` (default:
        the coverage) by the earlier Pages run 8800. Its owner ran another kit: a family cache is the
        producer's bundle verbatim, so its kit is bound to the producer workflow's pin, never to the
        owner's referenced kit."""

        options = {"path": PAGES_WORKFLOW_PATH, "head_sha": self.pub.mod.commit, "created": 800, "kit_sha": "d" * 40,
                   **run_changes}
        owner = self.world.run(8800, **options)
        return self.pub.family_cache(self.world, owner, bs.FAMILY_KEY, commit or self.pub.mod.commit, artifact_id=5301)

    def collected_from(self, record: dict[str, Any], *, kind: str = "family-cache", **changes: Any) -> None:
        """This leg was collected from the artifact ``record`` (its ``Selected`` object, edited by ``changes``)."""

        self.pub.select_family(self.world, bs.FAMILY_KEY, {**bs.selected_record(record, kind=kind), **changes})

    def collected_files(self, change: Any) -> None:
        """This leg's collected artifact with its files edited by ``change(directory)``."""

        directory = self.work / "collected-variant"
        shutil.copytree(self.pub.collected_family, directory)
        change(directory)
        self.world.artifact(grammar.collected_family_name(bs.FAMILY, bs.FAMILY_KEY),
                            {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"},
                            created=1060, archive=support.zip_directory(directory), artifact_id=bs.COLLECTED_FAMILY_ID)

    def name_listings(self) -> list[str]:
        return [params["name"] for path, params in self.world.api.calls
                if path.endswith("/actions/artifacts") and "name" in params]

    def test_the_recorded_family_cache_is_re_authenticated_by_id(self) -> None:
        self.pub.expire_family_handoffs(self.world)
        cache = self.owned_cache()
        self.collected_from(cache)
        result = self.build()
        self.assertEqual(result.promotion["families"][0]["selected_artifact_id"], cache["id"])
        calls = [path for path, _ in self.world.api.calls]
        self.assertEqual(calls.count(f"/repos/{self.pub.mod.repository}/actions/runs/8800"), 1, "the owner is read once")
        self.assertEqual(calls.count(f"/repos/{self.pub.mod.repository}/actions/runs/8800/artifacts"), 1,
                         "the cache is looked up in its owner's inventory")
        self.assertEqual(self.name_listings(), [], "no exact-name listing: the generation is never searched for")

    def test_the_recorded_generation_must_be_listed_unexpired_as_recorded(self) -> None:
        cases = {
            "expired": lambda: self.producer_handoff(expired=True),
            "digest": lambda: self.producer_handoff(archive=b"PK\x05\x06" + bytes(18)),
            "unknown id": lambda: self.collected_from(self.producer_handoff(), kind="family-handoff", artifact_id=5999),
            "another owner run": lambda: self.collected_from(self.producer_handoff(), kind="family-handoff",
                                                             run_id=bs.E2E_RUN),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                change()
                self.rejected("recorded name, digest and size", reason="family-selection")

    def test_an_oversized_family_generation_is_not_an_admissible_selection(self) -> None:
        # ``select`` skips a family handoff, and refuses a family cache, whose archive exceeds the
        # archive limit of the family's handoff_max_bytes: a record naming either is refused.
        maximum = family_archive_limit(self.pub.invocation().config.family(bs.FAMILY))
        self.collected_from(self.producer_handoff(size_in_bytes=maximum + 1), kind="family-handoff")
        self.rejected("exceeds the family's archive bound", reason="family-selection")
        self.setUp()
        self.pub.expire_family_handoffs(self.world)
        cache = self.owned_cache()
        owner = {"id": 8800, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.collected_from(self.world.artifact(cache["name"], owner, created=900,
                                                archive=self.pub.archives[bs.FAMILY], artifact_id=cache["id"],
                                                size_in_bytes=maximum + 1))
        self.rejected("exceeds the family's archive bound", reason="family-selection")

    def test_a_handoff_of_another_producer_run_is_refused(self) -> None:
        other = self.world.run(bs.PRODUCER_RUN + 1, path=bs.FAMILY_WORKFLOW, event="repository_dispatch",
                               head_sha=self.pub.mod.commit, created=150, title=bs.PRODUCER_TITLE)
        handoff = self.world.artifact(grammar.family_handoff_name(bs.FAMILY, bs.FAMILY_KEY, 1), other, created=450,
                                      archive=self.pub.archives[bs.FAMILY], artifact_id=5202)
        self.collected_from(handoff, kind="family-handoff")
        self.rejected("envelope's producer attempt", reason="family-selection")

    def test_a_producer_run_owning_its_handoff_name_twice_or_at_another_head_fails_closed(self) -> None:
        producer = {"id": bs.PRODUCER_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.family_handoff_name(bs.FAMILY, bs.FAMILY_KEY, 1), producer, created=401,
                            archive=self.pub.archives[bs.FAMILY], artifact_id=5299)
        self.rejected("exactly one", reason="family-projection")
        self.setUp()
        self.producer_handoff(workflow_run={"id": bs.PRODUCER_RUN, "head_sha": "0" * 40, "head_branch": "master"})
        self.rejected("exactly one", reason="family-projection")

    def test_a_cache_is_owned_by_the_recorded_attempt_of_a_successful_earlier_pages_run(self) -> None:
        cases: dict[str, Any] = {
            "fork": {"repository": "Someone/fork"},
            "failed": {"conclusion": "failure"},
            "running": {"status": "in_progress", "conclusion": None},
            "workflow": {"path": ".github/workflows/other.yml"},
            "workflow id": {"workflow_id": 78},
            "branch": {"head_branch": "topic"},
            "event": {"event": "pull_request"},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                self.setUp()
                self.pub.expire_family_handoffs(self.world)
                self.collected_from(self.owned_cache(**changes))
                self.rejected("owned by a successful earlier Pages run", reason="family-selection")
        with self.subTest("attempt"):
            self.setUp()
            self.pub.expire_family_handoffs(self.world)
            self.collected_from(self.owned_cache(), run_attempt=2)
            self.rejected("owned by a successful earlier Pages run", reason="family-selection")
        with self.subTest("this run"):
            self.setUp()
            pages = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
            self.collected_from(self.pub.family_cache(self.world, pages, bs.FAMILY_KEY, self.pub.mod.commit,
                                                      artifact_id=5301))
            self.rejected("owned by a successful earlier Pages run", reason="family-selection")

    def test_a_cache_named_outside_the_coverages_first_parent_history_is_refused(self) -> None:
        self.pub.expire_family_handoffs(self.world)
        self.collected_from(self.owned_cache(commit="1" * 40))
        self.rejected("outside the bounded first-parent history", reason="family-selection")

    def test_a_missing_malformed_or_foreign_selection_record_is_refused(self) -> None:
        handoff = bs.selected_record(self.producer_handoff(), kind="family-handoff")
        other_key = {**handoff, "name": grammar.family_handoff_name(bs.FAMILY, "mc26.3", 1)}
        ordinary = {**handoff, "kind": "handoff", "name": grammar.handoff_name(bs.FAMILY_KEY, 1)}
        record = self.pub.collected_family / "selected.json"
        cases = {
            "missing": lambda directory: (directory / "selected.json").unlink(),
            "not canonical": lambda directory: (directory / "selected.json").write_text(
                json.dumps(handoff, indent=1), encoding="utf-8"),
            "not json": lambda directory: (directory / "selected.json").write_bytes(b"{"),
            "extra key": lambda directory: (directory / "selected.json").write_bytes(canonical_json(
                {**handoff, "coverage_sha": self.pub.mod.commit})),
            "another leg": lambda directory: (directory / "selected.json").write_bytes(canonical_json(other_key)),
            "not a family generation": lambda directory: (directory / "selected.json").write_bytes(
                canonical_json(ordinary)),
            "a stray entry": lambda directory: (directory / "selected-2.json").write_bytes(record.read_bytes()),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                self.collected_files(change)
                self.rejected(reason="family-projection")


class FamilyProvenanceTest(BuildFlow):
    """SPEC §1.8 and §5.3.2 step 6: a family generation's producer run, run record and kit come from
    the API (``family_validate`` has no network, so the projection's record is only a claim)."""

    def producer(self, **changes: Any) -> dict[str, Any]:
        options = {"path": bs.FAMILY_WORKFLOW, "event": "repository_dispatch", "head_sha": self.pub.mod.commit,
                   "created": bs.PRODUCER_CREATED, "title": bs.PRODUCER_TITLE, **changes}
        return self.world.run(bs.PRODUCER_RUN, **options)

    def forged(self, name: str, forge: Any) -> None:
        """This leg's family handoff and collected artifact become a generation whose projection's
        producer record ``forge`` edits (``family collect`` admits it: the fields are claims)."""

        handoff, collected = self.pub.forged_family(self.work / name, forge)
        producer = {"id": bs.PRODUCER_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.family_handoff_name(bs.FAMILY, bs.FAMILY_KEY, 1), producer, created=400,
                            archive=handoff, artifact_id=bs.FAMILY_HANDOFF_ID)
        pages = {"id": bs.PAGES_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.collected_family_name(bs.FAMILY, bs.FAMILY_KEY), pages, created=1060,
                            archive=collected, artifact_id=bs.COLLECTED_FAMILY_ID)

    def test_a_forged_producer_record_is_refused(self) -> None:
        cases = {
            "created_at": lambda record: record.update(created_at=at(bs.PRODUCER_CREATED + 1)),
            "display_title": lambda record: record.update(display_title="Compatibility review (forged)"),
            "no display_title": lambda record: record.pop("display_title"),
            "job graph": lambda record: record.update(job_graph_sha256="0" * 64),
        }
        for label, forge in cases.items():
            with self.subTest(label):
                self.setUp()
                self.forged(label.replace(" ", "-"), forge)
                self.rejected("producer run's record", reason="family-provenance")

    def test_the_api_record_of_the_producer_run_is_required(self) -> None:
        for label, changes in (("created_at", {"created": bs.PRODUCER_CREATED + 1}),
                               ("display_title", {"title": "Another title"})):
            with self.subTest(label):
                self.setUp()
                self.producer(**changes)
                self.rejected("producer run's record", reason="family-provenance")

    def test_a_recorded_job_graph_is_the_producer_attempts_graph(self) -> None:
        graph = github_jobs.job_graph_sha256([{"name": FAMILY_JOB, "conclusion": "success"}])
        self.forged("graph", lambda record: record.update(job_graph_sha256=graph))
        result = self.build()
        self.assertTrue(result.promotion["families"][0]["available"])
        self.assertEqual(len(self.world.api.requests(f"/actions/runs/{bs.PRODUCER_RUN}/attempts/1/jobs")), 1)
        self.setUp()
        self.forged("graph-changed", lambda record: record.update(job_graph_sha256=graph))
        self.world.jobs({"id": bs.PRODUCER_RUN, "run_attempt": 1}, [
            self.world.job(FAMILY_JOB, steps=((bs.FAMILY_STEP, 390, 410),)), self.world.job("Late job")])
        self.rejected("producer run's record", reason="family-provenance")

    def test_a_foreign_producer_run_is_refused(self) -> None:
        cases = {
            "fork": {"repository": "Someone/fork"},
            "workflow id": {"workflow_id": 78},
            "workflow": {"path": ".github/workflows/other.yml"},
            "event": {"event": "push"},
            "branch": {"head_branch": "topic"},
            "head": {"head_sha": "0" * 40},
            "failed": {"conclusion": "failure"},
            "running": {"status": "in_progress", "conclusion": None},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                self.setUp()
                self.producer(**changes)
                self.rejected("failed provenance", reason="family-provenance")

    def test_the_family_kit_is_the_producer_workflows_pin_at_its_head(self) -> None:
        for label, pin in (("kit", bs.pin_workflow("2" * 40)), ("version", bs.pin_workflow(version="9.9.9"))):
            with self.subTest(label):
                self.setUp()
                self.world.api.add_file(self.pub.mod.commit, bs.FAMILY_WORKFLOW, pin)
                self.rejected(reason="kit-binding")


class CarriedFamilyTest(BuildFlow):
    """A family generation carried forward from the head's parent (SPEC §3.5, R5): ``build``
    re-authenticates the artifact the family job recorded (MB6-1) without repeating ``select``'s walk."""

    fixture_options = {"carried": True}
    generation = bs.CARRIED_GENERATION

    def selected(self, result: build.BuildResult) -> int:
        entry = result.promotion["families"][0]
        self.assertEqual((entry["key"], entry["available"], entry["coverage_sha"]),
                         (bs.FAMILY_KEY, True, self.pub.mod.commit))
        return entry["selected_artifact_id"]

    def cache(self, run_id: int, head: Any, *, artifact_id: int, created: float, **run_changes: Any) -> dict[str, Any]:
        options = {"path": PAGES_WORKFLOW_PATH, "head_sha": head.commit, "created": created - 100,
                   "kit_sha": support.KIT_SHA, "title": "Project site", **run_changes}
        owner = self.world.run(run_id, **options)
        return self.pub.family_cache(self.world, owner, bs.FAMILY_KEY, head.commit, generation=bs.CARRIED_GENERATION,
                                     artifact_id=artifact_id, created=created)

    def collected_from(self, record: dict[str, Any], *, kind: str = "family-cache") -> None:
        self.pub.select_family(self.world, bs.FAMILY_KEY, bs.selected_record(record, kind=kind),
                               generation=bs.CARRIED_GENERATION)

    def name_listings(self) -> list[str]:
        return [params["name"] for path, params in self.world.api.calls
                if path.endswith("/actions/artifacts") and "name" in params]

    def test_the_collected_envelope_keeps_the_producers_coverage(self) -> None:
        envelope = json.loads((self.pub.collected_families[bs.CARRIED_GENERATION][bs.FAMILY_KEY] / "source" /
                               "envelope.json").read_bytes())
        self.assertEqual(envelope["coverage_sha"], self.pub.parent.commit)
        self.assertNotEqual(self.pub.parent.commit, self.pub.mod.commit)

    def test_the_producers_handoff_below_the_head_is_the_selected_generation(self) -> None:
        self.assertEqual(self.selected(self.build()), self.pub.family_handoff_id(bs.FAMILY_KEY, bs.CARRIED_GENERATION))
        self.assertEqual(self.name_listings(), [])

    def test_a_cache_named_with_the_envelopes_coverage_is_the_selected_generation(self) -> None:
        self.pub.expire_family_handoffs(self.world, bs.CARRIED_GENERATION)
        cache = self.cache(8800, self.pub.parent, artifact_id=5301, created=900)
        self.collected_from(cache)
        self.assertEqual(self.selected(self.build()), cache["id"])
        calls = [path for path, _ in self.world.api.calls]
        self.assertEqual(calls.count(f"/repos/{self.pub.mod.repository}/actions/runs/8800"), 1, "the owner is read once")
        self.assertEqual(self.name_listings(), [], "no walk below the head")

    def test_a_cache_named_with_the_head_holding_the_parents_envelope_is_the_selected_generation(self) -> None:
        # A publication at the head already carried the parent's generation and rolled it forward
        # under the head's name; its envelope still names the parent (refresh copies it verbatim).
        self.pub.expire_family_handoffs(self.world, bs.CARRIED_GENERATION)
        self.cache(8800, self.pub.parent, artifact_id=5301, created=900)
        newer = self.cache(8801, self.pub.mod, artifact_id=5302, created=950)
        self.collected_from(newer)
        self.assertEqual(self.selected(self.build()), newer["id"])
        self.assertEqual(self.name_listings(), [])

    def test_a_newer_generation_after_collection_does_not_invalidate_the_collected_one(self) -> None:
        # A newer producer run at the parent owns another handoff: select would choose it now, but
        # the newest rule is not re-applied (as for ordinary bundles), so the build does not fail.
        other = self.world.run(bs.CARRIED_PRODUCER_RUN + 10, path=bs.FAMILY_WORKFLOW, event="repository_dispatch",
                               head_sha=self.pub.parent.commit, created=60, title=bs.PRODUCER_TITLE)
        self.world.artifact(grammar.family_handoff_name(bs.FAMILY, bs.FAMILY_KEY, 1), other, created=360,
                            archive=self.pub.archives[f"family:{bs.CARRIED_GENERATION}:{bs.FAMILY_KEY}"])
        self.assertEqual(self.selected(self.build()), self.pub.family_handoff_id(bs.FAMILY_KEY, bs.CARRIED_GENERATION))

    def test_a_carried_record_that_is_not_an_admissible_generation_is_refused(self) -> None:
        self.pub.expire_family_handoffs(self.world, bs.CARRIED_GENERATION)
        self.rejected("recorded name, digest and size", reason="family-selection")
        self.setUp()
        self.pub.expire_family_handoffs(self.world, bs.CARRIED_GENERATION)
        self.collected_from(self.cache(8800, self.pub.parent, artifact_id=5301, created=900, repository="Someone/fork"))
        self.rejected("owned by a successful earlier Pages run", reason="family-selection")

    def test_a_family_without_carry_forward_refuses_a_carried_generation(self) -> None:
        config = json.loads((self.pub.mod.root / "site" / "mod-base.json").read_text(encoding="utf-8"))
        for family in config["families"]:
            family["carry_forward"] = False
        path = self.work / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.rejected(reason="carry-forward", invocation=self.pub.invocation(config=path))


class ScaleBudgetTest(BuildFlow):
    """The Pages API budget at Quick Skin's scale (MB6-4): 17 keys, each with delegated reuse, and a
    family leg per key collected from its family cache, named with the coverage or carried up to
    three commits below it, under the fake's hard ``MAX_PAGES_API_READS`` stop.

    What grows with the keys is only each leg's collected artifact's download (two requests: the API
    and the storage redirect). Everything else is read once per invocation (:data:`FIXED_READS`, the
    final recheck included), however far below the coverage the generation was produced: ``build``
    re-authenticates each leg's recorded generation in its owner's inventory instead of repeating
    ``select``'s walk (one exact-name listing per leg and probed commit: 162 reads, beyond the budget,
    for caches three commits below the coverage). The adapter's own ``authenticate_extensions``
    reads (one per key here) are counted apart: in production the hook runs in its own process with
    its own budget."""

    fixture_options = {"versions": bs.SCALE_VERSIONS, "family_keys": tuple(f"mc{v}" for v in bs.SCALE_VERSIONS),
                       "carried": True, "delegated": True, "advance": 3}
    #: Reads that do not grow with the keys: this run's observation twice (run, attempt, jobs,
    #: artifacts, default branch, head), three workflow ids, the handoff run's attempt, jobs,
    #: inventory and pin, the tested attempt, the producer attempt and pin, the cache owner and its
    #: inventory. The whole build reads 92 times at any carried distance (repeating the walk, it read
    #: 128 times one commit behind and 162 three commits behind).
    FIXED_READS = 24

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))

    def published(self, generation: str, head: Any) -> tuple[World, dict[str, int]]:
        """A build of this attempt whose family handoffs expired and whose legs were collected from
        the family caches an earlier Pages run at ``head`` rolled forward (named with ``head``)."""

        world = self.pub.world(stage="build", generation=generation, max_requests=lim.MAX_PAGES_API_READS)
        self.pub.expire_family_handoffs(world, generation)
        owner = self.pub.pages_owner(world, 8800, head=head)
        caches = {}
        for index, key in enumerate(self.pub.family_keys):
            cache = self.pub.family_cache(world, owner, key, head.commit, generation=generation, artifact_id=7101 + index)
            self.pub.select_family(world, key, bs.selected_record(cache, kind="family-cache"), generation=generation)
            caches[key] = cache["id"]
        hooks = self.pub.hooks
        hooks.api = world.api
        result = self.build(world=world, hooks=hooks)
        self.assertEqual({entry["key"]: entry["selected_artifact_id"] for entry in result.promotion["families"]},
                         caches)
        for bundle in result.promotion["bundles"]:
            selection = json.loads((self.work / "collected" / bundle["key"] / "selection.json").read_bytes())
            self.assertEqual(selection["source"]["reuse"], "delegated")
        return world, self.reads(world)

    def reads(self, world: World) -> dict[str, int]:
        api, repository = world.api, self.pub.mod.repository
        hook = [call for call in api.calls if call[0] == f"/repos/{repository}/actions/runs/{bs.TESTED_RUN}"]
        self.assertEqual(len(hook), len(self.pub.keys), "authenticate_extensions reads the tested run per key")
        listings = [call for call in api.calls if call[0].endswith("/actions/artifacts") and "name" in call[1]]
        self.assertEqual(listings, [], "no exact-name listing: no leg searches for its generation")
        reads = {"build": api.request_count - len(hook), "downloads": api.request_count - len(api.calls),
                 "fixed": len(api.calls) - len(hook)}
        self.assertEqual(reads["downloads"], 2 * (len(self.pub.keys) + len(self.pub.family_keys)),
                         "each collected artifact is downloaded once")
        self.assertLessEqual(reads["fixed"], self.FIXED_READS, reads)
        self.assertLessEqual(reads["build"], lim.MAX_PAGES_API_READS * 7 // 10, reads)
        return reads

    def assert_read_once(self, world: World, *fragments: str) -> None:
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                self.assertEqual(len([call for call in world.api.calls if call[0].endswith(fragment)]), 1)

    def test_family_caches_at_the_coverage_with_delegated_reuse(self) -> None:
        world, _ = self.published(bs.DIRECT_GENERATION, self.pub.mod)
        self.assert_read_once(world, "/actions/runs/8800", "/actions/runs/8800/artifacts",
                              f"/actions/runs/{bs.PRODUCER_RUN}/attempts/1", f"/actions/runs/{bs.TESTED_RUN}/attempts/1",
                              f"/actions/runs/{bs.E2E_RUN}/attempts/1", f"/actions/runs/{bs.E2E_RUN}/artifacts",
                              f"/contents/{bs.FAMILY_WORKFLOW}", f"/contents/{bs.SOURCE}")

    def test_the_reads_do_not_grow_with_the_carried_distance(self) -> None:
        chain = self.pub.chain
        self.assertEqual(len(chain), 4, "the producer's commit is three commits below the head")
        observed = {}
        for distance in (1, 3):
            with self.subTest(distance=distance):
                self.setUp()
                world, reads = self.published(bs.CARRIED_GENERATION, chain[-1 - distance])
                self.assert_read_once(world, "/actions/runs/8800", "/actions/runs/8800/artifacts",
                                      f"/actions/runs/{bs.CARRIED_PRODUCER_RUN}/attempts/1",
                                      f"/contents/{bs.FAMILY_WORKFLOW}")
                observed[distance] = reads
        self.assertEqual(observed[1], observed[3], "a generation three commits behind costs no more reads")


class HookTest(BuildFlow):
    def test_verify_publication_receives_the_draft_and_may_veto(self) -> None:
        drafts = self.work / "drafts.json"
        invocation = self.fork(f'''

def verify_publication(ctx, promotion_draft):
    import json as _json
    with open({str(drafts)!r}, "w", encoding="utf-8") as stream:
        _json.dump(promotion_draft, stream)
''')
        self.build(invocation=invocation)
        draft = json.loads(drafts.read_text(encoding="utf-8"))
        self.assertNotIn("site", draft)
        documents.validate_promotion(draft, draft=True)
        shutil.rmtree(self.work / "fork")
        for name in ("_site", "promotion", "collected", "families"):
            shutil.rmtree(self.work / name)
        vetoing = self.fork('''

def verify_publication(ctx, promotion_draft):
    raise ValueError("runtime tree not verified")
''')
        # In-process the adapter's own exception surfaces; the isolated host reports it as HookFailed.
        with self.assertRaisesRegex((MbError, ValueError), "runtime tree not verified"):
            self.build(invocation=vetoing)
        self.assertFalse((self.work / "_site").exists())
        self.assertFalse((self.work / "promotion").exists())

    def test_a_drifting_expectation_is_refused(self) -> None:
        invocation = self.fork('''

_original_expectation = expectation


def expectation(ctx, target, tested_run, extensions):
    document = _original_expectation(ctx, target, tested_run, extensions)
    document["label"] = document["label"] + " (drift)"
    return document
''')
        self.rejected(reason="expectation-drift", invocation=invocation)


class CheckoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-build-checkout-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.mod = support.materialize("qs_like", self.work / "mod", mutate=lambda root: (
            root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8"))
        self.kit = bs.copy_kit(self.work / "kit")
        for arguments in (("init", "-q"), ("add", "-A"), ("commit", "-q", "-m", "kit")):
            support.git(self.kit, *arguments)
        self.kit_sha = support.git(self.kit, "rev-parse", "HEAD")

    def invocation(self, **changes: str) -> Any:
        environ = {**support.pages_environment(self.mod, job="build"), "MOD_BASE_KIT_SHA": self.kit_sha, **changes}
        return build_invocation(self.mod.root, None, environ, root=self.kit)

    def test_clean_checkouts_at_their_commits_pass(self) -> None:
        check_checkouts(self.invocation(), kit_root=self.kit, environ={"PATH": "/usr/bin"})

    def test_inherited_git_controls_dirty_or_moved_checkouts_fail(self) -> None:
        with self.assertRaisesRegex(MbError, "inherited Git controls"):
            check_checkouts(self.invocation(), kit_root=self.kit, environ={"GIT_DIR": "/tmp/x"})
        with self.assertRaisesRegex(MbError, "kit checkout is not at"):
            check_checkouts(self.invocation(MOD_BASE_KIT_SHA="3" * 40), kit_root=self.kit, environ={})
        with self.assertRaisesRegex(MbError, "mod checkout is not at"):
            check_checkouts(self.invocation(GITHUB_SHA="3" * 40), kit_root=self.kit, environ={})
        (self.mod.root / "untracked.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(MbError, "mod checkout is not clean"):
            check_checkouts(self.invocation(), kit_root=self.kit, environ={})
        os.remove(self.mod.root / "untracked.txt")
        (self.kit / "site" / "index.html").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(MbError, "kit checkout is not clean"):
            check_checkouts(self.invocation(), kit_root=self.kit, environ={})
        with self.assertRaises(MbError):
            check_checkouts(self.invocation(), kit_root=self.work, environ={})


class CommandTest(BuildFlow):
    def test_the_build_command_writes_heads_and_site_sha256(self) -> None:
        output = self.work / "github-output"
        environ = self.pub.environment("build")
        arguments = ["build", "--repo", str(self.pub.mod.root), "--kit-root", str(bs.KIT_ROOT),
                     "--collected", str(self.work / "collected"), "--families", str(self.work / "families"),
                     "--output", str(self.work / "_site"), "--promotion", str(self.work / "promotion"),
                     "--github-output", str(output)]
        checked: list[Any] = []
        with mock.patch.object(cli, "environ", return_value=environ), \
                mock.patch.object(commands_build.github_api, "from_environment", return_value=self.world.api), \
                mock.patch.object(commands_build.build, "check_checkouts",
                                  side_effect=lambda *args, **kwargs: checked.append(kwargs)), \
                mock.patch.object(host, "call", support.InProcessHost(api=self.world.api)), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cli.main(arguments)
        self.assertEqual(code, 0, stderr.getvalue())
        self.assertEqual(checked[0]["kit_root"], bs.KIT_ROOT)
        lines = output.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "heads=" + json.dumps({"master": self.pub.mod.commit}, separators=(",", ":")))
        record = json.loads((self.work / "_site" / "build.json").read_bytes())
        self.assertEqual(lines[1], f"site_sha256={record['site_inventory_sha256']}")

    def test_the_build_command_refuses_inherited_git_controls(self) -> None:
        environ = {**self.pub.environment("build"), "GIT_DIR": "/tmp/elsewhere"}
        with mock.patch.object(cli, "environ", return_value=environ), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = cli.main(["build", "--repo", str(self.pub.mod.root), "--kit-root", str(bs.KIT_ROOT),
                             "--collected", str(self.work / "c"), "--families", str(self.work / "f"),
                             "--output", str(self.work / "_site"), "--promotion", str(self.work / "p"),
                             "--github-output", str(self.work / "out")])
        self.assertEqual(code, 2)
        self.assertIn("inherited Git controls", stderr.getvalue())
        self.assertFalse((self.work / "_site").exists())


if __name__ == "__main__":
    unittest.main()
