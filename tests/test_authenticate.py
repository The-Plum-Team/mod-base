"""``authenticate`` (MB5, SPEC §4.8): source-run authentication producing the selection draft.

Ports the Block Pops ``test_pages_publication`` source-authentication cases (direct, attested,
exact job graph, stale head, display title, wrong controller, newest-run rule, compact cache owner)
and ``test_pages_source_scope`` (a scheduled run never vouches for another branch) onto v1
bundles prepared from the qs-like and bp-like fixture mods, plus the Quick Skin delegated-reuse
path (``quick-skin.runtime_source``) and the kit binding of SPEC §1.8. Every draft is handed to
the real ``compact`` step, so the draft is exactly what the collect flow consumes.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import mod_base
from mod_base.adapter import host
from mod_base.errors import MbError, Unavailable
from mod_base.evidence import compact as compact_module
from mod_base.evidence import prepare
from mod_base.model import documents, grammar
from mod_base.model.canonical import canonical_json
from mod_base.pages import authenticate
from mod_base.pages.select import Selected
from mod_base.runtime import build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH
from tests.fixtures.mods import support
from tests.test_select import (
    BP_HANDOFF_JOB,
    BP_HANDOFF_STEP,
    QS_HANDOFF_JOB,
    QS_HANDOFF_STEP,
    World,
    at,
    pin_workflow,
)

SOURCE = support.SOURCE_WORKFLOW
QS_KEY = "mc1.20.1"
ATTESTATION_JOB = "Attest exact tested packaged tree / Verify exact tested tree"
RELEASE = "release/1.21.1"


def branch_token(branch: str) -> str:
    return hashlib.sha256(branch.encode("utf-8")).hexdigest()[:24]


def title(commit: str) -> str:
    return f"Packaged E2E / {commit}"


class Flow(unittest.TestCase):
    """A fixture repository with prepared handoffs (shared, read-only) and a fresh fake per test."""

    fixture = "qs_like"

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = Path(tempfile.mkdtemp(prefix="mb-authenticate-test-")).resolve()
        cls.mod = support.materialize(cls.fixture, cls.directory / "repo")
        cls.key = QS_KEY if cls.fixture == "qs_like" else branch_token("master")
        cls.env = support.environment(cls.mod)
        with mock.patch.object(host, "call", support.InProcessHost()):
            cls.prepare_class()

    @classmethod
    def prepare_class(cls) -> None:
        cls.handoff = cls.produce("direct", cls.mod.subject)

    @classmethod
    def produce(cls, name: str, subject: dict[str, str], *, environ: dict[str, str] | None = None,
                tested: dict[str, Any] | None = None, key: str | None = None,
                extensions: dict[str, Any] | None = None) -> Path:
        environ = environ or cls.env
        key = key or cls.key
        producer = support.invocation(cls.mod, environ, implementation_sha=subject["commit"])
        e2e = cls.directory / f"e2e-{name}"
        support.synthesize(producer, key, subject, e2e, event=environ["GITHUB_EVENT_NAME"])
        handoff, direct = support.claims(environ, subject=subject)
        extensions_path = None
        if extensions is not None:
            extensions_path = cls.directory / f"extensions-{name}.json"
            extensions_path.write_text(json.dumps(extensions))
        output = cls.directory / f"handoff-{name}"
        prepare.prepare_handoff(producer, e2e_root=e2e, key=key, output=output, subject=subject,
                                tested=tested or direct, handoff=handoff, extensions_path=extensions_path)
        return output

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.directory, ignore_errors=True)

    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="mb-authenticate-case-")).resolve()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.world = World(self.mod.repository)
        self.host = support.InProcessHost(api=self.world.api)
        patcher = mock.patch.object(host, "call", self.host)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.world.api.set_branch("master", self.mod.commit, self.mod.tree)

    @staticmethod
    def manifest(bundle: Path) -> dict[str, Any]:
        return json.loads((bundle / "manifest.json").read_bytes())

    def pages(self, config: Path | None = None, **environ: str):
        return build_invocation(self.mod.root, config, {**support.pages_environment(self.mod), **environ})

    def with_source(self, **source: Any) -> Path:
        data = json.loads((self.mod.root / "site/mod-base.json").read_text())
        data["source"].update(source)
        path = self.work / f"config-{len(list(self.work.glob('config-*')))}.json"
        path.write_text(json.dumps(data))
        return path

    def handoff_jobs(self, bundle: Path, *, created: float) -> list[dict[str, Any]]:
        key = self.manifest(bundle)["key"]
        return [self.world.job(QS_HANDOFF_JOB.replace("{key}", key),
                               steps=((QS_HANDOFF_STEP, created - 10, created + 10),))]

    def seed(self, bundle: Path, *, created: float = 300, run: dict[str, Any] | None = None,
             jobs: list[dict[str, Any]] | None = None, **artifact: Any) -> Selected:
        """Seed the handoff run, its attempt jobs, the upload, the live subject and the pinned
        workflow; return the ``Selected`` record ``select`` would have written."""

        manifest = self.manifest(bundle)
        claim = manifest["provenance"]["handoff"]
        subject = manifest["subject"]
        options = {"head_sha": claim["commit"], "head_branch": claim["branch"], "attempt": claim["run_attempt"],
                   "title": title(subject["commit"]), **(run or {})}
        record = self.world.run(claim["run_id"], **options)
        self.world.jobs(record, jobs if jobs is not None else self.handoff_jobs(bundle, created=created),
                        attempt=claim["run_attempt"])
        uploaded = self.world.artifact(grammar.handoff_name(manifest["key"], claim["run_attempt"]), record,
                                       created=created, archive=support.zip_directory(bundle), **artifact)
        self.world.api.set_branch(subject["branch"], subject["commit"], subject["tree"])
        self.world.api.add_file(claim["commit"], SOURCE, pin_workflow())
        return Selected(kind="handoff", artifact_id=uploaded["id"], name=uploaded["name"], digest=uploaded["digest"],
                        size=uploaded["size_in_bytes"], run_id=claim["run_id"], run_attempt=claim["run_attempt"])

    def authenticate(self, selected: Selected, bundle: Path | None = None, invocation: Any = None) -> dict[str, Any]:
        return authenticate.authenticate_selection(invocation or self.pages(), api=self.world.api,
                                                   key=self.manifest(bundle or self.handoff)["key"],
                                                   selected_dir=bundle or self.handoff, selected=selected)

    def compact(self, draft: dict[str, Any], bundle: Path, name: str = "compact") -> dict[str, Any]:
        path = self.work / f"{name}-selection.json"
        path.write_bytes(canonical_json(draft))
        return compact_module.compact_bundle(self.pages(), key=draft["key"], input_dir=bundle, selection_path=path,
                                             output=self.work / name)


class QuickSkinAuthenticationTest(Flow):
    def test_direct_handoff_draft_feeds_the_compact_step(self) -> None:
        selected = self.seed(self.handoff)
        draft = self.authenticate(selected)
        documents.validate_selection(draft, draft=True)
        manifest = self.manifest(self.handoff)
        self.assertEqual(draft["implementation"], {
            "branch": "master", "sha": self.mod.commit, "run_id": support.PAGES_RUN_ID, "run_attempt": 1,
            "workflow_ref": f"{self.mod.repository}/{PAGES_WORKFLOW_PATH}@refs/heads/master"})
        self.assertEqual(draft["kit"]["sha"], support.KIT_SHA)
        self.assertEqual(draft["selected_artifact"],
                         {"kind": "handoff", "id": selected.artifact_id, "name": selected.name,
                          "digest": selected.digest, "size": selected.size, "run_id": selected.run_id,
                          "run_attempt": 1, "workflow_path": SOURCE, "created_at": at(300)})
        source = draft["source"]
        self.assertEqual(source["reuse"], "none")
        self.assertEqual(source["kit_binding"], {"source": "workflow_file", "sha": support.KIT_SHA})
        self.assertNotIn("attestation_job", source)
        self.assertNotIn("job_graph_sha256", source)
        self.assertEqual(source["handoff_run"]["event"], "workflow_dispatch")
        self.assertEqual(draft["source_manifest_sha256"],
                         hashlib.sha256((self.handoff / "manifest.json").read_bytes()).hexdigest())
        self.assertEqual(draft["expectation_sha256"], manifest["expectation"]["sha256"])
        self.assertEqual(draft["extensions_verified"], [])
        self.assertEqual(self.host.calls, [], "no hook is needed for an extension-free direct handoff")
        compacted = self.compact(draft, self.handoff)
        selection = json.loads((self.work / "compact/selection.json").read_bytes())
        self.assertEqual(selection["binding"]["mode"], "reencode-identical")
        self.assertEqual(compacted["source_artifact"]["id"], selected.artifact_id)

    def test_run_authenticate_writes_one_new_canonical_draft(self) -> None:
        selected = self.seed(self.handoff)
        document = self.work / "selected.json"
        document.write_bytes(canonical_json(selected.to_json()))
        output = self.work / "selection.json"
        draft = authenticate.run_authenticate(self.pages(), api=self.world.api, key=QS_KEY, selected_dir=self.handoff,
                                              selected_json=document, output=output)
        self.assertEqual(output.read_bytes(), canonical_json(draft))
        with self.assertRaisesRegex(MbError, "cannot create"):
            authenticate.run_authenticate(self.pages(), api=self.world.api, key=QS_KEY, selected_dir=self.handoff,
                                          selected_json=document, output=output)
        document.write_bytes(canonical_json({**selected.to_json(), "extra": 1}))
        with self.assertRaises(MbError):
            authenticate.run_authenticate(self.pages(), api=self.world.api, key=QS_KEY, selected_dir=self.handoff,
                                          selected_json=document, output=self.work / "other.json")

    def test_handoff_run_provenance_mutations_fail_closed(self) -> None:
        mutations = {"status": {"conclusion": "failure"}, "status ": {"status": "in_progress", "conclusion": None},
                     "event": {"event": "schedule"}, "head repository": {"repository": "fork/qs-like"},
                     "head": {"head_sha": "e" * 40}, "controller": {"head_branch": "topic"},
                     "workflow": {"path": ".github/workflows/other.yml"}, "workflow ": {"workflow_id": 78}}
        upload = {"id": 4242, "head_sha": self.mod.commit, "head_branch": "master"}
        for label, run in mutations.items():
            with self.subTest(label):
                self.setUp()
                selected = self.seed(self.handoff, run=run, workflow_run=upload)
                with self.assertRaisesRegex(MbError, f"failed provenance: {label.strip()}$"):
                    self.authenticate(selected)

    def test_selected_artifact_mismatches_fail_closed(self) -> None:
        selected = self.seed(self.handoff)
        for changes in ({"digest": "sha256:" + "f" * 64}, {"size": selected.size + 1}, {"run_id": 4243}):
            with self.subTest(changes=changes), self.assertRaisesRegex(MbError, "differs"):
                self.authenticate(Selected(**{**selected.to_json(), **changes}))
        cases = {"expired": {"expired": True},
                 "outside the upload step": {"jobs": self.handoff_jobs(self.handoff, created=300), "created": 900},
                 "no jobs": {"jobs": []}}
        for label, options in cases.items():
            with self.subTest(label):
                self.setUp()
                with self.assertRaises(MbError):
                    self.authenticate(self.seed(self.handoff, **options))

    def test_kit_binding_requires_the_pin_at_the_handoff_head(self) -> None:
        for data in (pin_workflow("d" * 40), pin_workflow(version="9.9.9"), b"name: no pin\n"):
            with self.subTest(data=data[:60]):
                self.setUp()
                selected = self.seed(self.handoff)
                self.world.api.add_file(self.mod.commit, SOURCE, data)
                with self.assertRaises(MbError) as caught:
                    self.authenticate(selected)
                self.assertIn(caught.exception.reason, {"kit-binding", "pin"})

    def test_a_moved_subject_head_is_stale(self) -> None:
        selected = self.seed(self.handoff)
        self.world.api.set_branch("master", "e" * 40, self.mod.tree)
        with self.assertRaises(MbError) as caught:
            self.authenticate(selected)
        self.assertEqual(caught.exception.reason, "stale-subject")

    def test_only_this_repositorys_pages_run_may_authenticate(self) -> None:
        selected = self.seed(self.handoff)
        foreign = f"{self.mod.repository}/.github/workflows/on-demand-e2e.yml@refs/heads/master"
        with self.assertRaises(MbError) as caught:
            self.authenticate(selected, invocation=self.pages(GITHUB_WORKFLOW_REF=foreign))
        self.assertEqual(caught.exception.reason, "environment")

    def test_delegated_reuse_is_proven_by_the_adapter(self) -> None:
        tested = {**support.claims(self.env, subject=self.mod.subject)[1], "run_id": 4000,
                  "controller_sha": self.mod.commit}
        reference = {"repository": self.mod.repository, "run_id": 4000, "tested_sha": self.mod.commit}
        with mock.patch.object(host, "call", support.InProcessHost()):
            bundle = self.produce("delegated", self.mod.subject, tested=tested,
                                  extensions={"quick-skin.runtime_source": reference})
        self.addCleanup(shutil.rmtree, bundle, True)
        for label, run in (("verified", {}), ("failed", {"conclusion": "failure"}),
                           ("other workflow", {"path": ".github/workflows/other.yml"}),
                           ("other head", {"head_sha": "d" * 40})):
            with self.subTest(label):
                self.setUp()
                selected = self.seed(bundle)
                self.world.run(4000, **{"head_sha": self.mod.commit, "head_branch": "topic", "event": "pull_request",
                                        **run})
                if label != "verified":
                    # Both the isolated host and the in-process seam report an adapter refusal as
                    # HookFailed (an MbError), never the adapter's own exception.
                    with self.assertRaises(MbError):
                        self.authenticate(selected, bundle)
                    continue
                draft = self.authenticate(selected, bundle)
                self.assertEqual(draft["source"]["reuse"], "delegated")
                self.assertEqual(draft["extensions_verified"], ["quick-skin.runtime_source"])
                self.assertEqual(draft["source"]["tested_run"]["event"], "pull_request")
                self.assertEqual(self.host.calls, [("authenticate_extensions", True)])
                self.compact(draft, bundle)

    def test_a_cache_is_reauthenticated_through_its_pages_owner(self) -> None:
        selected = self.seed(self.handoff)
        self.compact(self.authenticate(selected), self.handoff)
        cache_root = self.work / "compact"

        def seed_cache(owner_id: int = 8800, **run: Any) -> Selected:
            owner = self.world.run(owner_id, **{"path": PAGES_WORKFLOW_PATH, "created": 800,
                                                "kit_sha": support.KIT_SHA, **run})
            uploaded = self.world.artifact(grammar.cache_name(QS_KEY, self.mod.commit), owner, created=900,
                                           archive=support.zip_directory(cache_root))
            return Selected(kind="cache", artifact_id=uploaded["id"], name=uploaded["name"],
                            digest=uploaded["digest"], size=uploaded["size_in_bytes"], run_id=owner_id,
                            run_attempt=owner["run_attempt"])

        cache = seed_cache(head_sha=self.mod.commit)
        draft = self.authenticate(cache, cache_root)
        self.assertEqual(draft["selected_artifact"]["workflow_path"], PAGES_WORKFLOW_PATH)
        self.assertEqual(draft["source"]["kit_binding"], {"source": "referenced_workflows", "sha": support.KIT_SHA})
        self.compact(draft, cache_root, "recompacted")
        self.assertEqual(json.loads((self.work / "recompacted/selection.json").read_bytes())["binding"]["mode"],
                         "cache-revalidated")
        for label, run in (("failed owner", {"conclusion": "failure"}), ("topic owner", {"head_branch": "topic"}),
                           ("another kit", {"kit_sha": "d" * 40}), ("no kit", {"kit_sha": None}),
                           ("owner is this run", {"owner_id": support.PAGES_RUN_ID}),
                           ("another workflow id", {"workflow_id": 77})):
            with self.subTest(label):
                self.setUp()
                self.seed(self.handoff)
                cache = seed_cache(head_sha=self.mod.commit, **run)
                with self.assertRaises(MbError):
                    self.authenticate(cache, cache_root)
        self.setUp()
        self.seed(self.handoff)
        cache = seed_cache(head_sha=self.mod.commit)
        self.world.api.set_branch("master", "e" * 40, self.mod.tree)
        with self.assertRaisesRegex(MbError, "advanced"):
            self.authenticate(cache, cache_root)

    def test_family_generations_bind_their_kit_to_the_envelope_producer_pin(self) -> None:
        workflow = ".github/workflows/mod-compatibility-review.yml"
        producer = self.world.run(2000, path=workflow, event="repository_dispatch")
        envelope = {"kind": "mod-base.family.envelope", "family": "mod-compatibility",
                    "kit": {"repository": "The-Plum-Team/mod-base", "sha": support.KIT_SHA,
                            "version": mod_base.__version__},
                    "producer": {"run_id": 2000, "run_attempt": 1, "workflow_path": workflow, "branch": "master",
                                 "commit": producer["head_sha"], "controller_branch": "master",
                                 "controller_sha": producer["head_sha"]}}
        # A later kit bump: the Pages run that owns (refreshed) the cache executed another kit.
        owner = self.world.run(8800, path=PAGES_WORKFLOW_PATH, kit_sha="e" * 40)

        def bind(kind: str, run: dict[str, Any], manifest: dict[str, Any] = envelope) -> dict[str, str]:
            return authenticate.kit_binding(self.world.api, self.pages(), manifest=manifest, owner_run=run,
                                            selected_kind=kind)

        self.world.api.add_file(producer["head_sha"], workflow, pin_workflow())
        for kind in ("family-handoff", "family-cache"):
            with self.subTest(kind):
                # The cache is the producer's source/ verbatim: its kit is the producer's pin, whatever
                # kit the Pages owner executed.
                self.assertEqual(bind(kind, producer), {"source": "workflow_file", "sha": support.KIT_SHA})
        # A rerun producer: the envelope names its exact attempt, which the caller passes.
        rerun = self.world.run(2004, path=workflow, event="repository_dispatch", attempt=2,
                               earlier=({"run_attempt": 1},))
        second = {**envelope, "producer": {**envelope["producer"], "run_id": 2004, "run_attempt": 2}}
        self.assertEqual(bind("family-cache", rerun, second), {"source": "workflow_file", "sha": support.KIT_SHA})
        first = {**second, "producer": {**second["producer"], "run_attempt": 1}}
        other_kit = {**envelope, "kit": {**envelope["kit"], "sha": "d" * 40}}
        another_producer = self.world.run(2002, path=workflow, event="repository_dispatch")
        self.world.api.add_file(another_producer["head_sha"], workflow, pin_workflow())
        moved = self.world.run(2003, path=workflow, event="repository_dispatch", head_sha="f" * 40)
        self.world.api.add_file(moved["head_sha"], workflow, pin_workflow())
        source_run = self.world.run(2001)
        no_producer = {name: value for name, value in envelope.items() if name != "producer"}
        boolean_id = {**envelope, "producer": {**envelope["producer"], "run_id": True}}
        for label, kind, run, manifest in (
                ("another kit's handoff", "family-handoff", producer, other_kit),
                ("another kit's cache", "family-cache", producer, other_kit),
                ("a source run as producer", "family-handoff", source_run,
                 {**envelope, "producer": {**envelope["producer"], "run_id": 2001}}),
                ("the Pages owner of a cache", "family-cache", owner, envelope),
                ("the Pages owner under the envelope's run id", "family-cache", {**owner, "id": 2000}, envelope),
                ("another producer run of the same pin", "family-cache", another_producer, envelope),
                ("the producer run at another head", "family-handoff", {**moved, "id": 2000}, envelope),
                ("an envelope without a producer", "family-cache", producer, no_producer),
                ("a boolean producer run id", "family-handoff", {**producer, "id": 1}, boolean_id),
                ("another attempt than the envelope's", "family-cache", rerun, first),
                ("an attempt-less owner run", "family-handoff",
                 {name: value for name, value in producer.items() if name != "run_attempt"}, envelope),
                ("a boolean producer attempt", "family-handoff", {**producer, "run_attempt": True},
                 {**envelope, "producer": {**envelope["producer"], "run_attempt": True}}),
                ("an envelope naming another producer workflow", "family-cache", producer,
                 {**envelope, "producer": {**envelope["producer"], "workflow_path": ".github/workflows/other.yml"}})):
            with self.subTest(label), self.assertRaises(MbError) as caught:
                bind(kind, run, manifest)
            self.assertEqual(caught.exception.reason, "kit-binding")
        with self.assertRaises(MbError):
            bind("family-handoff", producer, {**envelope, "family": "unknown-family"})
        with self.assertRaises(MbError) as caught:
            bind("anchor", owner)
        self.assertEqual(caught.exception.reason, "usage")
        self.world.api.add_file(producer["head_sha"], workflow, pin_workflow("d" * 40))
        for kind in ("family-handoff", "family-cache"):
            with self.subTest(f"{kind} after the producer pin changed"), self.assertRaises(MbError) as caught:
                bind(kind, producer)
            self.assertEqual(caught.exception.reason, "kit-binding")

    def test_family_generations_are_not_authenticated_here(self) -> None:
        selected = Selected(kind="family-handoff", artifact_id=1, name=grammar.family_handoff_name(
            "mod-compatibility", QS_KEY, 1), digest="sha256:" + "a" * 64, size=1, run_id=1, run_attempt=1)
        with self.assertRaises(MbError):
            self.authenticate(selected)


class BlockPopsAuthenticationTest(Flow):
    fixture = "bp_like"

    @classmethod
    def prepare_class(cls) -> None:
        cls.handoff = cls.produce("direct", cls.mod.subject)
        handoff, direct = support.claims(cls.env, subject=cls.mod.subject)
        cls.attested = cls.produce("attested", cls.mod.subject, tested={**direct, "run_id": 4000, "run_attempt": 2})
        matrix = json.loads((cls.mod.root / "release/release-matrix.json").read_text())
        matrix["branch"]["name"] = RELEASE
        cls.release = support.commit_on_branch(cls.mod, RELEASE, {
            "release/release-matrix.json": json.dumps(matrix).encode()})
        cls.split = cls.produce("split", cls.release.subject, key=branch_token(RELEASE))
        cls.release_direct = cls.produce("release-direct", cls.release.subject,
                                         environ=support.environment(cls.release), key=branch_token(RELEASE))

    def graph(self, bundle: Path, *, created: float = 300, extra: tuple[dict[str, Any], ...] = (),
              drop: int = 0) -> list[dict[str, Any]]:
        lanes = self.manifest(bundle)["lanes"]
        names = ["Resolve exact source"] + [f"Packaged E2E / {lane['artifact_node']} / {lane['scenario']}"
                                            for lane in lanes]
        jobs = [self.world.job(name) for name in names[drop:]]
        jobs.append(self.world.job(BP_HANDOFF_JOB, steps=((BP_HANDOFF_STEP, created - 10, created + 10),)))
        return [*jobs, *extra]

    def test_direct_run_requires_its_exact_job_graph_and_title(self) -> None:
        selected = self.seed(self.handoff, jobs=self.graph(self.handoff))
        draft = self.authenticate(selected)
        self.assertEqual(draft["source"]["reuse"], "none")
        self.assertIn("job_graph_sha256", draft["source"])
        self.assertEqual(draft["source"]["tested_run"]["job_graph_sha256"], draft["source"]["job_graph_sha256"])
        self.assertEqual(draft["source"]["handoff_run"]["display_title"], title(self.mod.commit))
        self.compact(draft, self.handoff)
        self.assertIn(("expected_source_jobs", False), self.host.calls)
        for label, options in (("missing job", {"jobs": self.graph(self.handoff, drop=1)}),
                               ("extra job", {"jobs": self.graph(self.handoff, extra=(self.world.job("Extra"),))}),
                               ("failed job", {"jobs": self.graph(self.handoff, extra=(
                                   self.world.job("Resolve exact source", conclusion="failure"),))})):
            with self.subTest(label):
                self.setUp()
                with self.assertRaises(MbError) as caught:
                    self.authenticate(self.seed(self.handoff, **options))
                self.assertEqual(caught.exception.reason, "job-graph")
        self.setUp()
        with self.assertRaises(MbError):
            self.authenticate(self.seed(self.handoff, jobs=self.graph(self.handoff), run={"title": title("f" * 40)}))

    def test_newest_run_rule_never_accepts_an_older_attempt(self) -> None:
        for label, run in (("newer success", {}), ("newer in progress", {"status": "in_progress", "conclusion": None}),
                           ("newer failure", {"conclusion": "failure"})):
            with self.subTest(label):
                self.setUp()
                selected = self.seed(self.handoff, jobs=self.graph(self.handoff))
                self.world.run(4300, head_sha=self.mod.commit, title=title(self.mod.commit), created=3600, **run)
                with self.assertRaises(MbError) as caught:
                    self.authenticate(selected)
                self.assertEqual(caught.exception.reason, "newest-run")
        self.setUp()
        selected = self.seed(self.handoff, jobs=self.graph(self.handoff), run={"attempt": 1})
        self.world.run(4242, head_sha=self.mod.commit, title=title(self.mod.commit), attempt=2,
                       earlier=({"run_attempt": 1},))
        self.world.jobs({"id": 4242, "run_attempt": 1}, self.graph(self.handoff))
        with self.assertRaises(MbError):
            self.authenticate(selected)

    def test_attested_reuse_needs_one_exactly_named_successful_attestation(self) -> None:
        manifest = self.manifest(self.attested)
        tested = manifest["provenance"]["tested"]
        self.assertEqual(manifest["provenance"]["reuse"], "attested")

        def seed(attestation: tuple[dict[str, Any], ...]) -> Selected:
            self.world.run(4000, head_sha=tested["controller_sha"], title=title(tested["commit"]), attempt=2,
                           created=-3600, earlier=({"run_attempt": 1},))
            self.world.jobs({"id": 4000, "run_attempt": 2}, self.graph(self.attested))
            selected = self.seed(self.attested, jobs=[*attestation, self.world.job(
                BP_HANDOFF_JOB, steps=((BP_HANDOFF_STEP, 290, 310),))])
            return selected

        draft = self.authenticate(seed((self.world.job(ATTESTATION_JOB),)), self.attested)
        self.assertEqual(draft["source"]["reuse"], "attested")
        self.assertEqual(draft["source"]["attestation_job"]["name"], ATTESTATION_JOB)
        self.assertEqual(draft["source"]["tested_run"]["run_id"], 4000)
        self.assertEqual(draft["source"]["tested_run"]["display_title"], title(tested["commit"]))
        self.compact(draft, self.attested)
        exactly_one = "expected exactly one job named"
        for label, attestation, message in (
                ("duplicate", (self.world.job(ATTESTATION_JOB), self.world.job(ATTESTATION_JOB)), exactly_one),
                ("absent", (), exactly_one),
                ("failed", (self.world.job(ATTESTATION_JOB, conclusion="failure"),), "did not complete"),
                ("suffix only", (self.world.job("Other / Verify exact tested tree"),), exactly_one)):
            with self.subTest(label):
                self.setUp()
                with self.assertRaisesRegex(MbError, message):
                    self.authenticate(seed(attestation), self.attested)
        self.setUp()
        selected = seed((self.world.job(ATTESTATION_JOB),))
        self.world.run(4000, head_sha=tested["controller_sha"], title=title("f" * 40), attempt=2, created=-3600,
                       earlier=({"run_attempt": 1},))
        with self.assertRaisesRegex(MbError, "tested run display title"):
            self.authenticate(selected, self.attested)
        unattested = self.pages(self.with_source(attestation_job=None))
        self.setUp()
        with self.assertRaises(MbError):
            self.authenticate(seed((self.world.job(ATTESTATION_JOB),)), self.attested, invocation=unattested)

    def test_controller_split_is_admitted_only_from_the_canonical_controller_with_a_title(self) -> None:
        manifest = self.manifest(self.split)
        self.assertEqual(manifest["subject"]["branch"], RELEASE)
        self.assertEqual(manifest["provenance"]["handoff"]["commit"], self.mod.commit)
        selected = self.seed(self.split, jobs=self.graph(self.split))
        draft = self.authenticate(selected, self.split)
        self.assertEqual(draft["subject"], self.release.subject)
        self.assertEqual(draft["source"]["handoff_run"]["display_title"], title(self.release.commit))
        self.compact(draft, self.split)
        upload = {"id": 4242, "head_sha": self.mod.commit, "head_branch": "master"}
        for label, run, message in (("scheduled", {"event": "schedule"}, "provenance: event$"),
                                    ("release controller", {"head_branch": RELEASE}, "provenance: controller$"),
                                    ("title of the controller", {"title": title(self.mod.commit)}, "display title")):
            with self.subTest(label):
                self.setUp()
                selected = self.seed(self.split, jobs=self.graph(self.split), run=run, workflow_run=upload)
                with self.assertRaisesRegex(MbError, message):
                    self.authenticate(selected, self.split)
        self.setUp()
        selected = self.seed(self.split, jobs=self.graph(self.split))
        untitled = self.pages(self.with_source(display_title=None, attestation_job=None, require_newest_run=False))
        with self.assertRaisesRegex(MbError, "display_title"):
            self.authenticate(selected, self.split, invocation=untitled)

    def test_a_release_branch_run_never_vouches_even_for_its_own_head(self) -> None:
        # BP rejects any handoff whose controller branch is not the canonical one: a release
        # branch's own copy of the source workflow (dispatchable by anyone who can push that branch)
        # could otherwise fabricate matching job names, title, pin and graph.
        manifest = self.manifest(self.release_direct)
        self.assertEqual((manifest["provenance"]["handoff"]["branch"], manifest["subject"]["branch"]),
                         (RELEASE, RELEASE))
        selected = self.seed(self.release_direct, jobs=self.graph(self.release_direct))
        with self.assertRaisesRegex(MbError, "provenance: controller$"):
            self.authenticate(selected, self.release_direct)

    def test_attested_reuse_never_projects_scheduled_coverage_or_another_tree(self) -> None:
        tested = self.manifest(self.attested)["provenance"]["tested"]

        def seed(handoff_event: str = "workflow_dispatch", tested_event: str = "workflow_dispatch") -> Selected:
            self.world.run(4000, head_sha=tested["controller_sha"], title=title(tested["commit"]), attempt=2,
                           created=-3600, event=tested_event, earlier=({"run_attempt": 1},))
            self.world.jobs({"id": 4000, "run_attempt": 2}, self.graph(self.attested))
            return self.seed(self.attested, run={"event": handoff_event}, jobs=[
                self.world.job(ATTESTATION_JOB), self.world.job(BP_HANDOFF_JOB, steps=((BP_HANDOFF_STEP, 290, 310),))])

        self.assertEqual(self.authenticate(seed(), self.attested)["source"]["reuse"], "attested")
        for label, events in (("scheduled attestation", ("schedule", "workflow_dispatch")),
                              ("scheduled coverage", ("workflow_dispatch", "schedule"))):
            with self.subTest(label):
                self.setUp()
                with self.assertRaisesRegex(MbError, "scheduled packaged coverage") as caught:
                    self.authenticate(seed(*events), self.attested)
                self.assertEqual(caught.exception.reason, "reuse")
        self.setUp()
        selected = seed()
        self.world.api.add_commit(tested["commit"], "e" * 40)
        with self.assertRaisesRegex(MbError, "subject's tree"):
            self.authenticate(selected, self.attested)
        self.setUp()
        selected = seed()
        self.world.run(4000, head_sha=tested["controller_sha"], title=title(tested["commit"]), attempt=2,
                       created=-3600, head_branch=RELEASE, earlier=({"run_attempt": 1},))
        with self.assertRaisesRegex(MbError, "tested run 4000 attempt 2 failed provenance: controller$"):
            self.authenticate(selected, self.attested)

    def test_a_scheduled_canonical_run_is_a_direct_source(self) -> None:
        selected = self.seed(self.handoff, jobs=self.graph(self.handoff), run={"event": "schedule"})
        draft = self.authenticate(selected)
        self.assertEqual(draft["source"]["handoff_run"]["event"], "schedule")
        with self.assertRaises(Unavailable):
            # A newer successful scheduled run elsewhere is still a newer exact-subject source.
            self.world.run(4300, head_sha=self.mod.commit, title=title(self.mod.commit), created=3600,
                           status="queued", conclusion=None)
            self.authenticate(selected)


if __name__ == "__main__":
    unittest.main()
