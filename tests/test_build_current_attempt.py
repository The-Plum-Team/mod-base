"""``build`` (SPEC §5.3.2): the current-attempt checks against exact job names, the collected
artifacts of this run, the live heads, the re-authenticated selections, families, the
``verify_publication`` veto, the promotion and the ``build`` command.

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

from mod_base import cli
from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.model import documents, grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.pages import build, commands_build
from mod_base.pages.build import PROMOTION_FILE, build_site, check_checkouts
from mod_base.runtime import build_invocation
from mod_base.workflow import api_job_name, caller_job_name
from tests import test_build_support as bs
from tests.fixtures.mods import support
from tests.test_select import World, at

COLLECT = {key: api_job_name("publish", "collect", key=key) for key in bs.QS_KEYS}
FAMILY_JOBS = {key: api_job_name("publish", "family", family=bs.FAMILY, key=key) for key in bs.QS_KEYS}


class BuildFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-flow-")).resolve()
        cls.pub = bs.Publication(cls.directory / "publication")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.world = self.pub.world(stage="build")

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
    def test_the_family_cache_identifies_a_generation_whose_handoff_is_gone(self) -> None:
        producer = {"id": bs.PRODUCER_RUN, "head_sha": self.pub.mod.commit, "head_branch": "master"}
        self.world.artifact(grammar.family_handoff_name(bs.FAMILY, "mc1.20.1", 1), producer, created=400, expired=True,
                            archive=self.pub.archives[bs.FAMILY], artifact_id=bs.FAMILY_HANDOFF_ID)
        self.rejected("cannot identify", reason="family-projection")
        self.setUp()
        self.world.artifact(grammar.family_handoff_name(bs.FAMILY, "mc1.20.1", 1), producer, created=400, expired=True,
                            archive=self.pub.archives[bs.FAMILY], artifact_id=bs.FAMILY_HANDOFF_ID)
        owner = self.world.run(8800, path=".github/workflows/pages.yml", head_sha=self.pub.mod.commit, created=800)
        cache = self.world.artifact(grammar.family_cache_name(bs.FAMILY, "mc1.20.1", self.pub.mod.commit), owner,
                                    created=900, archive=self.pub.archives[bs.FAMILY], artifact_id=5301)
        result = self.build()
        self.assertEqual(result.promotion["families"][0]["selected_artifact_id"], cache["id"])


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
