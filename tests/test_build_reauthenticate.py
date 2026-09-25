"""``build`` re-authenticates every kind of embedded selection (SPEC §5.3.2 step 6, §4.8).

Each case prepares handoffs with the real producer, authenticates them with the real MB5
``authenticate_selection`` against a seeded fake, compacts the draft with the real ``compact`` (as
the Pages ``collect`` job does) and runs ``build_site`` over this Pages attempt: Block Pops' direct
and attested sources with their exact job graph, a compact cache owned by an earlier successful
Pages run, and Quick Skin's delegated reuse proven again by the adapter's ``authenticate_extensions``
(R6, the ``quick-skin.runtime_source`` extension). Every positive case is paired with the negative
mutations of the facts ``build`` re-reads (the owner, the attestation, the job graph, the adapter's
extension proof, and the workflow ids and events of the source runs).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base.adapter import host
from mod_base.errors import MbError
from mod_base.evidence import compact as compact_module
from mod_base.evidence import prepare
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.pages import authenticate
from mod_base.pages.build import BuildResult, _Builder, build_site
from mod_base.pages.select import Selected
from mod_base.runtime import Invocation, build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, caller_job_name
from tests import test_build_support as bs
from tests.fixtures.mods import support
from tests.test_select import BP_HANDOFF_JOB, BP_HANDOFF_STEP, QS_HANDOFF_JOB, QS_HANDOFF_STEP, World, at

SOURCE = support.SOURCE_WORKFLOW
E2E_RUN = 4242
TESTED_RUN = 4000
CACHE_OWNER = 8800
CACHE_ID = 5301
ATTESTATION_JOB = "Attest exact tested packaged tree / Verify exact tested tree"
FOREIGN_WORKFLOW_ID = 78


def title(commit: str) -> str:
    return f"Packaged E2E / {commit}"


class Reauthentication(unittest.TestCase):
    """A fixture mod whose handoffs are produced once per class; every test seeds a fresh fake."""

    fixture = "bp_like"

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-build-reauth-")).resolve()
        cls.mod = support.materialize(cls.fixture, cls.directory / "repo",
                                      mutate=bs.qs_site_mutation if cls.fixture == "qs_like" else None)
        cls.kit = bs.copy_kit(cls.directory / "kit")
        cls.env = support.environment(cls.mod, run_id=E2E_RUN)
        cls.collected: dict[str, dict[str, Path]] = {}
        with mock.patch.object(host, "call", support.InProcessHost()):
            cls.prepare_class()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    @classmethod
    def prepare_class(cls) -> None:
        raise NotImplementedError

    @classmethod
    def produce(cls, name: str, key: str, *, tested: Mapping[str, Any] | None = None,
                extensions: Mapping[str, Any] | None = None) -> Path:
        producer = support.invocation(cls.mod, cls.env, implementation_sha=cls.mod.commit)
        e2e = cls.directory / f"e2e-{name}-{key}"
        support.synthesize(producer, key, cls.mod.subject, e2e)
        handoff, direct = support.claims(cls.env, subject=cls.mod.subject)
        extensions_path = None
        if extensions is not None:
            extensions_path = cls.directory / f"extensions-{name}-{key}.json"
            extensions_path.write_text(json.dumps(extensions), encoding="utf-8")
        output = cls.directory / f"handoff-{name}-{key}"
        prepare.prepare_handoff(producer, e2e_root=e2e, key=key, output=output, subject=cls.mod.subject,
                                tested=dict(tested or direct), handoff=handoff, extensions_path=extensions_path)
        return output

    @classmethod
    def collect(cls, world: World, name: str, key: str, bundle: Path, selected: Selected) -> Path:
        """The real ``authenticate`` draft of ``selected`` compacted by the real ``compact``."""

        pages = build_invocation(cls.mod.root, None, support.pages_environment(cls.mod, job="collect"))
        with mock.patch.object(host, "call", support.InProcessHost(api=world.api)):
            draft = authenticate.authenticate_selection(pages, api=world.api, key=key, selected_dir=bundle,
                                                        selected=selected)
            path = cls.directory / f"draft-{name}-{key}.json"
            path.write_bytes(canonical_json(draft))
            output = cls.directory / f"collected-{name}-{key}"
            compact_module.compact_bundle(pages, key=key, input_dir=bundle, selection_path=path, output=output)
        return output

    @classmethod
    def world(cls) -> World:
        world = World(cls.mod.repository)
        world.api.set_branch(cls.mod.branch, cls.mod.commit, cls.mod.tree)
        world.api.add_file(cls.mod.commit, SOURCE, bs.pin_workflow())
        return world

    @staticmethod
    def selected(kind: str, artifact: Mapping[str, Any], run_id: int, run_attempt: int = 1) -> Selected:
        return Selected(kind=kind, artifact_id=artifact["id"], name=artifact["name"], digest=artifact["digest"],
                        size=artifact["size_in_bytes"], run_id=run_id, run_attempt=run_attempt)

    # -- this Pages attempt -----------------------------------------------------------------------

    def keys(self) -> list[str]:
        raise NotImplementedError

    def seed_pages(self, world: World, case: str) -> None:
        """This Pages run in its build stage: every expected job and the collected ``case`` bundles."""

        run = world.run(bs.PAGES_RUN, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, status="in_progress",
                        conclusion=None, created=1000, kit_sha=support.KIT_SHA, title="Project site")
        keys = self.keys()
        families = [family["id"] for family in self.build_invocation().config.families]
        names = [caller_job_name("verify_kit"), api_job_name("publish", "admit")]
        names += [api_job_name("publish", "collect", key=key) for key in keys]
        names += [api_job_name("publish", "family", family=family, key=key) for family in families for key in keys]
        listed = [world.job(name, started=1010, completed=1100) for name in names]
        listed.append({"id": 79999, "name": api_job_name("publish", "build"), "status": "in_progress",
                       "conclusion": None, "started_at": at(1100), "completed_at": None, "steps": []})
        world.jobs(run, listed)
        for index, key in enumerate(keys):
            world.artifact(grammar.collected_name(key), run, created=1050, artifact_id=6101 + index,
                           archive=support.zip_directory(self.collected[case][key]))

    def build_invocation(self) -> Invocation:
        return build_invocation(self.mod.root, None, support.pages_environment(self.mod, job="build"), root=self.kit)

    def build(self, world: World, hooks: support.InProcessHost | None = None) -> BuildResult:
        work = Path(tempfile.mkdtemp(prefix="case-", dir=self.directory))
        self.output = work / "_site"
        hooks = hooks or support.InProcessHost()
        hooks.api = world.api
        with mock.patch.object(host, "call", hooks):
            return build_site(self.build_invocation(), api=world.api, kit_root=self.kit,
                              collected_dir=work / "collected", families_dir=work / "families", output=self.output,
                              promotion_dir=work / "promotion")

    def rejected(self, world: World, *, reason: str | None = None, errors: Any = MbError) -> None:
        with self.assertRaises(errors) as caught:
            self.build(world)
        if reason is not None:
            self.assertEqual(caught.exception.reason, reason, str(caught.exception))
        self.assertFalse(self.output.exists(), "a rejected build publishes nothing")

    def mutations(self, seed: Callable[[], World], cases: Mapping[str, tuple[Callable[[World], Any], str | None]],
                  errors: Any = MbError) -> None:
        for label, (mutate, reason) in cases.items():
            with self.subTest(label):
                world = seed()
                mutate(world)
                self.rejected(world, reason=reason, errors=errors)


class BlockPopsReauthenticationTest(Reauthentication):
    fixture = "bp_like"
    key = hashlib.sha256(b"master").hexdigest()[:24]

    @classmethod
    def prepare_class(cls) -> None:
        cls.direct = cls.produce("direct", cls.key)
        _, direct = support.claims(cls.env, subject=cls.mod.subject)
        cls.attested = cls.produce("attested", cls.key, tested={**direct, "run_id": TESTED_RUN, "run_attempt": 2})
        world = cls.world()
        cls.seed_direct(world)
        cls.collected["direct"] = {cls.key: cls.collect(world, "direct", cls.key, cls.direct, cls.handoff_selected)}
        cls.seed_cache(world)
        cls.collected["cache"] = {cls.key: cls.collect(world, "cache", cls.key, cls.collected["direct"][cls.key],
                                                       cls.cache_selected)}
        world = cls.world()
        cls.seed_attested(world)
        cls.collected["attested"] = {cls.key: cls.collect(world, "attested", cls.key, cls.attested,
                                                          cls.handoff_selected)}

    def keys(self) -> list[str]:
        return [self.key]

    @classmethod
    def graph(cls, bundle: Path, *, drop: int = 0, extra: tuple[dict[str, Any], ...] = (),
              world: World) -> list[dict[str, Any]]:
        """The exact source job graph of ``bundle`` (the adapter's ``expected_source_jobs``)."""

        lanes = json.loads((bundle / "manifest.json").read_bytes())["lanes"]
        names = ["Resolve exact source"] + [f"Packaged E2E / {lane['artifact_node']} / {lane['scenario']}"
                                            for lane in lanes]
        jobs = [world.job(name) for name in names[drop:]]
        jobs.append(world.job(BP_HANDOFF_JOB, steps=((BP_HANDOFF_STEP, 290, 310),)))
        return [*jobs, *extra]

    @classmethod
    def seed_handoff(cls, world: World, bundle: Path, jobs: list[dict[str, Any]], **run: Any) -> None:
        record = world.run(E2E_RUN, **{"head_sha": cls.mod.commit, "title": title(cls.mod.commit), **run})
        world.jobs(record, jobs)
        uploaded = world.artifact(grammar.handoff_name(cls.key, 1), record, created=300, artifact_id=5101,
                                  archive=support.zip_directory(bundle))
        cls.handoff_selected = cls.selected("handoff", uploaded, E2E_RUN)

    @classmethod
    def seed_direct(cls, world: World, **run: Any) -> None:
        cls.seed_handoff(world, cls.direct, cls.graph(cls.direct, world=world), **run)

    @classmethod
    def seed_attested(cls, world: World, *, attestation: tuple[dict[str, Any], ...] | None = None,
                      tested: Mapping[str, Any] | None = None, drop: int = 0) -> None:
        world.run(TESTED_RUN, **{"head_sha": cls.mod.commit, "title": title(cls.mod.commit), "attempt": 2,
                                 "created": -3600, "earlier": ({"run_attempt": 1},), **(tested or {})})
        world.jobs({"id": TESTED_RUN, "run_attempt": 2}, cls.graph(cls.attested, world=world, drop=drop))
        cls.seed_handoff(world, cls.attested, [
            *(attestation if attestation is not None else (world.job(ATTESTATION_JOB),)),
            world.job(BP_HANDOFF_JOB, steps=((BP_HANDOFF_STEP, 290, 310),))])

    @classmethod
    def seed_cache(cls, world: World, **run: Any) -> None:
        owner = world.run(CACHE_OWNER, **{"path": PAGES_WORKFLOW_PATH, "head_sha": cls.mod.commit, "created": 800,
                                          "kit_sha": support.KIT_SHA, **run})
        uploaded = world.artifact(grammar.cache_name(cls.key, cls.mod.commit), owner, created=900,
                                  artifact_id=CACHE_ID, archive=support.zip_directory(cls.collected["direct"][cls.key]))
        cls.cache_selected = cls.selected("cache", uploaded, CACHE_OWNER)

    def seeded(self, case: str, **options: Any) -> World:
        world = self.world()
        if case == "attested":
            self.seed_attested(world, **options)
        else:
            self.seed_direct(world)
            if case == "cache":
                self.seed_cache(world, **options)
        self.seed_pages(world, case)
        return world

    def published(self, case: str, hooks: support.InProcessHost) -> dict[str, Any]:
        result = self.build(self.seeded(case), hooks)
        self.assertEqual(result.heads, {"master": self.mod.commit})
        return json.loads((self.collected[case][self.key] / "selection.json").read_bytes())

    def test_a_direct_source_with_its_exact_job_graph(self) -> None:
        hooks = support.InProcessHost()
        selection = self.published("direct", hooks)
        self.assertEqual(selection["source"]["reuse"], "none")
        self.assertIn("job_graph_sha256", selection["source"])
        self.assertEqual(selection["source"]["tested_run"]["job_graph_sha256"], selection["source"]["job_graph_sha256"])
        self.assertIn(("expected_source_jobs", False), hooks.calls)
        graph = self.graph
        self.mutations(lambda: self.seeded("direct"), {
            "a job added to the graph": (lambda world: world.jobs({"id": E2E_RUN, "run_attempt": 1}, graph(
                self.direct, world=world, extra=(world.job("Extra"),))), "job-graph"),
            "a job removed from the graph": (lambda world: world.jobs({"id": E2E_RUN, "run_attempt": 1}, graph(
                self.direct, world=world, drop=1)), "job-graph"),
            "a failed graph job": (lambda world: world.jobs({"id": E2E_RUN, "run_attempt": 1}, [
                *graph(self.direct, world=world, drop=1), world.job("Resolve exact source", conclusion="failure")]),
                "job-graph"),
            "a handoff run of another workflow": (lambda world: world.run(
                E2E_RUN, head_sha=self.mod.commit, title=title(self.mod.commit), workflow_id=FOREIGN_WORKFLOW_ID),
                "source-authentication"),
        })

    def test_an_attested_source_with_its_attestation_and_tested_graph(self) -> None:
        hooks = support.InProcessHost()
        selection = self.published("attested", hooks)
        self.assertEqual(selection["source"]["reuse"], "attested")
        self.assertEqual(selection["source"]["attestation_job"]["name"], ATTESTATION_JOB)
        self.assertEqual(selection["source"]["tested_run"]["run_id"], TESTED_RUN)
        self.assertIn(("expected_source_jobs", False), hooks.calls)

        def reseed(**options: Any) -> Callable[[World], None]:
            return lambda world: self.seed_attested(world, **options)

        self.mutations(lambda: self.seeded("attested"), {
            "a failed attestation": (lambda world: self.seed_attested(world, attestation=(
                world.job(ATTESTATION_JOB, conclusion="failure"),)), "reuse"),
            "no attestation": (reseed(attestation=()), None),
            "a job removed from the tested graph": (reseed(drop=1), "job-graph"),
            "a tested run of another workflow": (reseed(tested={"workflow_id": FOREIGN_WORKFLOW_ID}),
                                                 "source-authentication"),
            "a failed tested run": (reseed(tested={"conclusion": "failure"}), "source-authentication"),
        })

    def test_a_cache_owned_by_an_earlier_successful_pages_run(self) -> None:
        selection = self.published("cache", support.InProcessHost())
        self.assertEqual(selection["selected_artifact"]["kind"], "cache")
        self.assertEqual(selection["selected_artifact"]["run_id"], CACHE_OWNER)
        self.assertEqual(selection["source"]["kit_binding"], {"source": "referenced_workflows", "sha": support.KIT_SHA})
        cases = {
            "a failed owner": ({"conclusion": "failure"}, "artifact"),
            "an owner of another workflow": ({"workflow_id": 77}, "artifact"),
            "an owner on another branch": ({"head_branch": "topic"}, "artifact"),
            "an owner that is still running": ({"status": "in_progress", "conclusion": None}, "artifact"),
            "an owner of another kit": ({"kit_sha": "d" * 40}, "selection-binding"),
        }
        for label, (run, reason) in cases.items():
            with self.subTest(label):
                self.rejected(self.seeded("cache", **run), reason=reason)

    def test_an_event_is_admitted_only_for_the_branch_its_run_tested(self) -> None:
        """A scheduled canonical run never vouches for a release subject (``authenticate``'s rule)."""

        world = self.world()
        builder = _Builder(self.build_invocation(), world.api, self.kit)
        claim = {"run_id": E2E_RUN, "run_attempt": 1, "workflow_path": SOURCE, "controller_sha": self.mod.commit,
                 "controller_branch": "master"}
        for event, branch, admitted in (("schedule", "master", True), ("workflow_dispatch", "release/1.21.1", True),
                                        ("schedule", "release/1.21.1", False), ("push", "master", False)):
            with self.subTest(event=event, branch=branch):
                run = world.run(E2E_RUN, head_sha=self.mod.commit, event=event)
                if admitted:
                    builder._require_source_run(run, claim, tested_branch=branch, label="handoff run")
                else:
                    with self.assertRaisesRegex(MbError, "failed provenance"):
                        builder._require_source_run(run, claim, tested_branch=branch, label="handoff run")
        for label, changes in (("controller branch", {"head_branch": "release/1.21.1"}),
                               ("workflow id", {"workflow_id": FOREIGN_WORKFLOW_ID}),
                               ("workflow id type", {"workflow_id": True})):
            with self.subTest(label):
                run = {**world.run(E2E_RUN, head_sha=self.mod.commit), **changes}
                with self.assertRaisesRegex(MbError, "failed provenance"):
                    builder._require_source_run(run, {**claim, "controller_branch": run["head_branch"]},
                                                tested_branch="master", label="handoff run")


class QuickSkinDelegatedReauthenticationTest(Reauthentication):
    fixture = "qs_like"

    @classmethod
    def prepare_class(cls) -> None:
        _, direct = support.claims(cls.env, subject=cls.mod.subject)
        tested = {**direct, "run_id": TESTED_RUN, "controller_sha": cls.mod.commit}
        reference = {"repository": cls.mod.repository, "run_id": TESTED_RUN, "tested_sha": cls.mod.commit}
        cls.handoffs = {key: cls.produce("delegated", key, tested=tested,
                                         extensions={"quick-skin.runtime_source": reference})
                        for key in bs.QS_KEYS}
        world = cls.world()
        selected = cls.seed_sources(world)
        cls.collected["delegated"] = {key: cls.collect(world, "delegated", key, cls.handoffs[key], selected[key])
                                      for key in bs.QS_KEYS}

    @classmethod
    def seed_sources(cls, world: World, *, handoff: Mapping[str, Any] | None = None,
                     tested: Mapping[str, Any] | None = None) -> dict[str, Selected]:
        record = world.run(E2E_RUN, **{"head_sha": cls.mod.commit, **(handoff or {})})
        world.jobs(record, [world.job(QS_HANDOFF_JOB.replace("{key}", key), steps=((QS_HANDOFF_STEP, 290, 310),))
                            for key in bs.QS_KEYS])
        selected = {}
        for index, key in enumerate(bs.QS_KEYS):
            uploaded = world.artifact(grammar.handoff_name(key, 1), record, created=300, artifact_id=5101 + index,
                                      archive=support.zip_directory(cls.handoffs[key]))
            selected[key] = cls.selected("handoff", uploaded, E2E_RUN)
        world.run(TESTED_RUN, **{"head_sha": cls.mod.commit, "head_branch": "topic", "event": "pull_request",
                                 **(tested or {})})
        return selected

    def keys(self) -> list[str]:
        return list(bs.QS_KEYS)

    def seeded(self, **options: Any) -> World:
        world = self.world()
        self.seed_sources(world, **options)
        self.seed_pages(world, "delegated")
        return world

    def test_delegated_reuse_is_proven_again_by_the_adapter(self) -> None:
        hooks = support.InProcessHost()
        result = self.build(self.seeded(), hooks)
        self.assertEqual([entry["available"] for entry in result.promotion["families"]], [False, False])
        for key in bs.QS_KEYS:
            selection = json.loads((self.collected["delegated"][key] / "selection.json").read_bytes())
            self.assertEqual(selection["source"]["reuse"], "delegated")
            self.assertEqual(selection["extensions_verified"], ["quick-skin.runtime_source"])
            self.assertEqual(selection["source"]["tested_run"]["run_id"], TESTED_RUN)
        self.assertEqual(hooks.calls.count(("authenticate_extensions", True)), 2)
        self.mutations(self.seeded, {
            "a delegated tested run of another workflow": (
                lambda world: self.seed_sources(world, tested={"workflow_id": FOREIGN_WORKFLOW_ID}), "reuse"),
            "a failed delegated tested run": (
                lambda world: self.seed_sources(world, tested={"conclusion": "failure"}), None),
            "a handoff run of another workflow": (
                lambda world: self.seed_sources(world, handoff={"workflow_id": FOREIGN_WORKFLOW_ID}),
                "source-authentication"),
            "a handoff run from a topic branch": (
                lambda world: self.seed_sources(world, handoff={"head_branch": "topic"}), None),
        })
        # The adapter refuses a reference its tested run no longer matches (in process, the adapter's
        # own exception surfaces; the isolated host reports it as HookFailed).
        self.mutations(self.seeded, {
            "an extension the adapter refuses": (
                lambda world: self.seed_sources(world, tested={"head_sha": "d" * 40}), None),
        }, errors=(MbError, ValueError))


if __name__ == "__main__":
    unittest.main()
