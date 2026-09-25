"""Shared builders for the MB6 build, render and refresh tests; this module holds no test case.

:class:`Publication` materializes a Quick Skin-like fixture mod (``qs_like`` with a pixelated icon,
the family hook of :mod:`tests.test_family_support` and a ``.gitignore``), produces every key's handoff through the real producer, drafts their selections
from the facts a seeded :class:`tests.test_select.World` serves (:func:`selection_draft`, exactly as
``authenticate`` documents them, so these fixtures never depend on the control plane's internal
checks), compacts them with the real ``compact`` and collects its family legs with the real
``family collect``, as the Pages ``collect``/``family`` jobs do: by default both keys and one
family leg, optionally Quick Skin's scale (one key per Minecraft version, a family leg per key),
delegated reuse and a carried family generation. Each collected family leg carries the
``selected.json`` record of the generation its ``select`` step chose (by default the producer's
family handoff; :meth:`Publication.select_family` re-seeds a leg collected from another one).
:meth:`Publication.world` seeds a fresh fake with the same sources plus, on request, this Pages run
in its build or finalize stage. :func:`bp_bundle` produces a Block Pops-like compact bundle for
render-only tests.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest import mock

import mod_base
from mod_base.adapter import host
from mod_base.evidence import compact as compact_module
from mod_base.evidence import prepare
from mod_base.evidence.expectation import tested_run_projection
from mod_base.github import runs
from mod_base.family.envelope import create_envelope
from mod_base.family.paired import collect_family
from mod_base.io.atomic_directory import atomic_directory
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import run_claim_from_environment
from mod_base.pages.build import FAMILY_SELECTED_NAME, render_site
from mod_base.pages.select import Selected
from mod_base.runtime import Invocation, build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, caller_job_name
from tests import test_family_support as fs
from tests.fixtures.mods import support
from tests.test_select import FAMILY_JOB, FAMILY_STEP, QS_HANDOFF_JOB, QS_HANDOFF_STEP, World, at

KIT_ROOT = Path(__file__).resolve().parents[1]
QS_KEYS = ("mc1.20.1", "mc26.3")
FAMILY = fs.FAMILY
FAMILY_KEY = fs.KEY
FAMILY_WORKFLOW = fs.PRODUCER_WORKFLOW
SOURCE = support.SOURCE_WORKFLOW
E2E_RUN = 4242
PRODUCER_RUN = fs.PRODUCER_RUN_ID
#: The producer run's ``created_at`` offset and display title (``World.run``'s default title).
PRODUCER_CREATED = 100
PRODUCER_TITLE = "Run"
#: The run that produced the carried family generation at the head's parent, and its offset.
CARRIED_PRODUCER_RUN = PRODUCER_RUN - 1
CARRIED_PRODUCER_CREATED = 20
#: The family generations of a :class:`Publication`: produced at its head, or at the parent commit
#: and carried forward to the head.
DIRECT_GENERATION = "direct"
CARRIED_GENERATION = "carried"
#: The delegated tested run (Quick Skin's PR reuse) and the extension that proves it.
TESTED_RUN = 4000
RUNTIME_SOURCE = "quick-skin.runtime_source"
#: Quick Skin's 17 supported Minecraft versions (one key each).
SCALE_VERSIONS = ("1.20.1", "1.20.2", "1.20.4", "1.20.6", "1.21", "1.21.1", "1.21.3", "1.21.4", "1.21.5",
                  "1.21.6", "1.21.8", "1.21.10", "1.21.11", "26.1", "26.1.1", "26.2", "26.3")
PAGES_RUN = support.PAGES_RUN_ID
HANDOFF_IDS = {"mc1.20.1": 5101, "mc26.3": 5102}
FAMILY_HANDOFF_ID = 5201
COLLECTED_IDS = {"mc1.20.1": 6101, "mc26.3": 6102}
COLLECTED_FAMILY_ID = 6201
PROMOTION_ID = 6301
DESCRIPTION = "Change, preview and synchronize skins and capes across supported Minecraft loaders."
BUILD_JOB = api_job_name("publish", "build")


def pin_workflow(sha: str = support.KIT_SHA, version: str = mod_base.__version__) -> bytes:
    return (f"name: Packaged E2E\njobs:\n  e2e:\n    steps:\n"
            f"      - uses: The-Plum-Team/mod-base/actions/prepare-evidence@{sha} # v{version}\n").encode()


def icon_png() -> bytes:
    """An RGBA icon with transparent corners and a text chunk (metadata that must be dropped)."""

    from PIL import Image, PngImagePlugin

    image = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    for x in range(8, 40):
        for y in range(8, 40):
            image.putpixel((x, y), (54 + x, 170, 92 + y, 255 if (x + y) % 7 else 128))
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "private note that must not be published")
    stream = io.BytesIO()
    image.save(stream, format="PNG", pnginfo=info)
    return stream.getvalue()


def qs_site_mutation(root: Path) -> None:
    """The Quick Skin-like site: pixelated icon, family hook and policy (the fixture matrix already
    carries the ``project.description`` its config reads, :data:`DESCRIPTION`)."""

    config_path = root / "site" / "mod-base.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["project"]["icon"] = {"path": "icon.png", "rendering": "pixelated"}
    for family in config["families"]:
        family["image_policy"] = dict(fs.IMAGE_POLICY)
    config_path.write_bytes(canonical_json(config))
    (root / "icon.png").write_bytes(icon_png())
    adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
    adapter.write_text(adapter.read_text(encoding="utf-8") + fs.FAMILY_HOOK, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")


def scale_matrix(root: Path, versions: tuple[str, ...]) -> None:
    """Replace the fixture matrix's targets by one ``fabric`` target per Minecraft version."""

    matrix_path = root / "release" / "release-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["targets"] = [{"java": 21, "loaders": ["fabric"], "minecraft": version} for version in versions]
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")


def selection_draft(invocation: Invocation, handoff: Path, *, run: dict[str, Any], artifact: dict[str, Any],
                    workflow_path: str = SOURCE, tested_run: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The draft ``authenticate`` writes for the handoff ``handoff`` uploaded as ``artifact`` by
    ``run`` (:meth:`World.run` and :meth:`World.artifact` records), in the Pages job of
    ``invocation``: a direct one, or with ``tested_run`` a delegated one (its tested record read
    from that run, its reuse proven by the ``quick-skin.runtime_source`` extension)."""

    raw = (handoff / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    provenance = manifest["provenance"]
    environ = invocation.environ
    return {
        "kind": "mod-base.selection", "schema_version": 1, "repository": manifest["repository"],
        "key": manifest["key"], "kit": invocation.kit,
        "implementation": {"branch": "master", "sha": invocation.implementation_sha,
                           "workflow_ref": environ["GITHUB_WORKFLOW_REF"], "run_id": int(environ["GITHUB_RUN_ID"]),
                           "run_attempt": int(environ["GITHUB_RUN_ATTEMPT"])},
        "subject": manifest["subject"], "coverage_sha": provenance["coverage_sha"],
        "selected_artifact": {"kind": "handoff", "id": artifact["id"], "name": artifact["name"],
                              "digest": artifact["digest"], "size": artifact["size_in_bytes"], "run_id": run["id"],
                              "run_attempt": run["run_attempt"], "workflow_path": workflow_path,
                              "created_at": artifact["created_at"]},
        "source": {"handoff_run": runs.run_record(run, provenance["handoff"]),
                   "tested_run": runs.run_record(run, provenance["tested"]) if tested_run is None else
                   runs.run_record(tested_run, provenance["tested"], require_controller_head=False),
                   "reuse": provenance["reuse"],
                   "kit_binding": {"source": "workflow_file", "sha": manifest["kit"]["sha"]}},
        "expectation_sha256": manifest["expectation"]["sha256"],
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "extensions_verified": [] if tested_run is None else [RUNTIME_SOURCE],
    }


#: The hooks :class:`CachingHost` memoizes: pure functions of the subject's inert Git objects.
PURE_HOOKS = frozenset({"targets", "expectation"})


@dataclass
class CachingHost(support.InProcessHost):
    """:class:`support.InProcessHost` that memoizes ``targets`` and ``expectation`` (network-free
    functions of the subject's inert Git objects) by their canonical arguments, so a large fixture
    does not repeat their Git reads. Placement is still checked and every call recorded; every
    other hook, each network hook included, always runs."""

    results: dict[tuple[str, str, str, bytes], Any] = field(default_factory=dict)

    def pure(self, invocation_: Invocation, hook: str, arguments: Mapping[str, Any]) -> Any:
        memo = (str(invocation_.repo_root), invocation_.implementation_sha, hook, canonical_json(dict(arguments)))
        if memo not in self.results:
            self.results[memo] = support.run_in_process(invocation_, hook, arguments)
        return copy.deepcopy(self.results[memo])

    def __call__(self, invocation_: Invocation, hook: str, arguments: Mapping[str, Any], *,
                 network: bool = False) -> Any:
        if network or hook not in PURE_HOOKS:
            return super().__call__(invocation_, hook, arguments, network=network)
        host.check_placement(invocation_, hook, network=network)
        self.calls.append((hook, network))
        return self.pure(invocation_, hook, arguments)


def synthesize(hooks: CachingHost, invocation: Invocation, key: str, subject: Mapping[str, str],
               out_root: Path) -> None:
    """``support.synthesize`` of a direct ``default-branch`` run whose ``targets`` and
    ``expectation`` come from ``hooks``' memo."""

    target = next(item for item in hooks.pure(invocation, "targets", {"branches": None}) if item["key"] == key)
    projection = tested_run_projection({"branch": subject["branch"]}, "workflow_dispatch")
    expectation = hooks.pure(invocation, "expectation", {"target": target, "tested_run": projection, "extensions": {}})
    out_root.mkdir(parents=True)
    support.run_in_process(invocation, "synthesize", {"target": target, "expectation": expectation,
                                                      "out_root": str(out_root.resolve())}, module="fixtures_path")


def copy_kit(destination: Path) -> Path:
    """A kit root holding exactly the kit's ``site/`` (``render_site`` reads nothing else)."""

    shutil.copytree(KIT_ROOT / "site", destination / "site")
    return destination.resolve()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render(invocation: Invocation, output: Path, *, kit_root: Path, bundles: list[dict[str, Any]],
           families: list[dict[str, Any]]) -> dict[str, bytes]:
    """``render_site`` into the new ``output`` through the production atomic directory."""

    return atomic_directory(output, lambda stage, descriptor: render_site(
        invocation, kit_root=kit_root, bundles=bundles, families=families, stage_fd=descriptor))


def selected_record(record: Mapping[str, Any], *, kind: str, run_attempt: int = 1) -> dict[str, Any]:
    """The ``Selected`` JSON object ``select --output`` writes for the artifact ``record`` (a
    :meth:`World.artifact` record) of ``kind`` uploaded by attempt ``run_attempt`` of its run."""

    return Selected(kind=kind, artifact_id=record["id"], name=record["name"], digest=record["digest"],
                    size=record["size_in_bytes"], run_id=record["workflow_run"]["id"], run_attempt=run_attempt).to_json()


def collect_family_leg(invocation: Invocation, *, key: str, input_dir: Path, coverage_sha: str, output: Path,
                       selected: Mapping[str, Any]) -> None:
    """The ``family`` job's collection: the real ``family collect --selected-json`` of ``input_dir``
    into ``output``, recording ``selected`` (the ``Selected`` JSON object ``select --output`` wrote,
    canonical) as its ``selected.json``."""

    record = output.parent / f"{output.name}.selected.json"
    record.write_bytes(canonical_json(dict(selected)))
    collect_family(invocation, family=FAMILY, key=key, input_dir=input_dir, expected_coverage_sha=coverage_sha,
                   output=output, selected_json=record)
    assert (output / FAMILY_SELECTED_NAME).read_bytes() == record.read_bytes(), "family collect recorded another selection"


def load_bundle(root: Path) -> dict[str, Any]:
    """The render input of a written compact bundle."""

    return {"root": root, "manifest": json.loads((root / "manifest.json").read_bytes()),
            "expectation": json.loads((root / "expectation.json").read_bytes()),
            "selection": json.loads((root / "selection.json").read_bytes())}


def load_family(root: Path | None, *, key: str, status: str = "unavailable") -> dict[str, Any]:
    """The render input of one family leg: collected at ``root``, or absent with ``status``."""

    if root is None:
        return {"family": FAMILY, "key": key, "status": status, "root": None, "projection": None}
    return {"family": FAMILY, "key": key, "status": "available", "root": root,
            "projection": json.loads((root / "paired.json").read_bytes())}


class Publication:
    """One synthetic Quick Skin-like publication (see module docstring); read-only once built.

    ``versions`` replaces the fixture matrix with one ``fabric`` target per Minecraft version
    (Quick Skin's scale); ``family_keys`` are the keys with a family generation; ``delegated``
    produces every handoff with Quick Skin's delegated reuse (tested run :data:`TESTED_RUN`,
    proven by the ``quick-skin.runtime_source`` extension). With ``carried``, a second family
    generation of every family key (:data:`CARRIED_GENERATION`) is produced by run
    :data:`CARRIED_PRODUCER_RUN` at the fixture's first commit (:attr:`parent`) before the head
    advances by ``advance`` commits (:attr:`chain`, parent first), and collected at the head through
    the adapter's carry-forward (R5)."""

    def __init__(self, directory: Path, *, versions: tuple[str, ...] | None = None,
                 family_keys: tuple[str, ...] = (FAMILY_KEY,), carried: bool = False,
                 delegated: bool = False, advance: int = 1) -> None:
        self.directory = directory
        self.keys = QS_KEYS if versions is None else tuple(f"mc{version}" for version in versions)
        self.family_keys = family_keys
        self.delegated = delegated
        mutation = qs_site_mutation if versions is None else (
            lambda root: (qs_site_mutation(root), scale_matrix(root, versions)))
        self.mod = support.materialize("qs_like", directory / "mod", mutate=mutation)
        self.parent: support.FixtureMod | None = None
        #: The first-parent commits from :attr:`parent` to the head, oldest first (carried only).
        self.chain: list[support.FixtureMod] = []
        self.kit = copy_kit(directory / "kit")
        self.archives: dict[str, bytes] = {}
        self.family_handoffs: dict[str, dict[str, Path]] = {DIRECT_GENERATION: {}}
        #: The fixture's hooks (``targets``/``expectation`` memoized); tests may reuse them.
        self.hooks = CachingHost()
        with mock.patch.object(host, "call", self.hooks):
            if carried:
                self.parent = self.mod
                self.family_handoffs[CARRIED_GENERATION] = {
                    key: self._family_handoff(CARRIED_GENERATION, key) for key in family_keys}
                self.chain = [self.mod]
                for index in range(advance):
                    self.chain.append(fs.advance(self.chain[-1], f"later-{index}.txt"))
                self.mod = self.chain[-1]
            environ = support.environment(self.mod, run_id=E2E_RUN)
            producer = support.invocation(self.mod, environ, implementation_sha=self.mod.commit)
            handoff_claim, tested_claim = support.claims(environ, subject=self.mod.subject)
            extensions_path = None
            if delegated:
                tested_claim = {**tested_claim, "run_id": TESTED_RUN}
                extensions_path = directory / "extensions.json"
                extensions_path.write_text(json.dumps({RUNTIME_SOURCE: {
                    "repository": self.mod.repository, "run_id": TESTED_RUN, "tested_sha": self.mod.commit}}),
                    encoding="utf-8")
            self.handoffs: dict[str, Path] = {}
            for key in self.keys:
                e2e = directory / f"e2e-{key}"
                synthesize(self.hooks, producer, key, self.mod.subject, e2e)
                self.handoffs[key] = directory / f"handoff-{key}"
                prepare.prepare_handoff(producer, e2e_root=e2e, key=key, output=self.handoffs[key],
                                        subject=self.mod.subject, tested=tested_claim, handoff=handoff_claim,
                                        extensions_path=extensions_path)
            self.family_handoffs[DIRECT_GENERATION] = {
                key: self._family_handoff(DIRECT_GENERATION, key) for key in family_keys}
        for key, path in self.handoffs.items():
            self.archives[key] = support.zip_directory(path)
        for generation, handoffs in self.family_handoffs.items():
            for key, path in handoffs.items():
                self.archives[f"family:{generation}:{key}"] = support.zip_directory(path)
        self.seeds: dict[str, Any] = {}
        self.world()
        self.collected: dict[str, Path] = {}
        self.collected_families: dict[str, dict[str, Path]] = {}
        self._variants: dict[tuple[str, str, bytes], bytes] = {}
        with mock.patch.object(host, "call", self.hooks):
            collect = self.invocation("collect")
            for key in self.keys:
                draft = selection_draft(collect, self.handoffs[key], run=self.seeds["e2e"], artifact=self.seeds[key],
                                        tested_run=self.seeds.get("tested"))
                draft_path = directory / f"draft-{key}.json"
                draft_path.write_bytes(canonical_json(draft))
                self.collected[key] = directory / "collected" / key
                compact_module.compact_bundle(collect, key=key, input_dir=self.handoffs[key],
                                              selection_path=draft_path, output=self.collected[key])
            for generation, handoffs in self.family_handoffs.items():
                self.collected_families[generation] = {}
                for key, path in handoffs.items():
                    output = directory / f"collected-family-{generation}-{key}"
                    collect_family_leg(self.invocation("family"), key=key, input_dir=path,
                                       coverage_sha=self.mod.commit, output=output,
                                       selected=self.handoff_selected(key, generation))
                    self.collected_families[generation][key] = output
        for key in self.keys:
            self.archives[f"collected:{key}"] = support.zip_directory(self.collected[key])
        for generation, collected in self.collected_families.items():
            for key, path in collected.items():
                self.archives[f"collected-family:{generation}:{key}"] = support.zip_directory(path)
        if FAMILY_KEY in family_keys:
            self.family_handoff = self.family_handoffs[DIRECT_GENERATION][FAMILY_KEY]
            self.collected_family = self.collected_families[DIRECT_GENERATION][FAMILY_KEY]
            self.archives[FAMILY] = self.archives[f"family:{DIRECT_GENERATION}:{FAMILY_KEY}"]
            self.archives["collected-family"] = self.archives[f"collected-family:{DIRECT_GENERATION}:{FAMILY_KEY}"]

    # -- identities ---------------------------------------------------------------------------------

    def handoff_id(self, key: str) -> int:
        return 5101 + self.keys.index(key)

    def collected_id(self, key: str) -> int:
        return 6101 + self.keys.index(key)

    def family_handoff_id(self, key: str, generation: str = DIRECT_GENERATION) -> int:
        return (5201 if generation == DIRECT_GENERATION else 5251) + self.family_keys.index(key)

    def collected_family_id(self, key: str) -> int:
        return 6201 + self.family_keys.index(key)

    def handoff_selected(self, key: str, generation: str = DIRECT_GENERATION) -> dict[str, Any]:
        """The ``Selected`` object of ``generation``'s family handoff of ``key`` (attempt 1)."""

        archive = self.archives[f"family:{generation}:{key}"]
        return Selected(kind="family-handoff", artifact_id=self.family_handoff_id(key, generation),
                        name=grammar.family_handoff_name(FAMILY, key, 1), digest="sha256:" + sha256(archive),
                        size=len(archive), run_id=self.producer(generation)[1], run_attempt=1).to_json()

    def producer(self, generation: str = DIRECT_GENERATION) -> tuple[support.FixtureMod, int, int]:
        """``(checkout, run id, created offset)`` of ``generation``'s producer run."""

        if generation == DIRECT_GENERATION:
            return self.mod, PRODUCER_RUN, PRODUCER_CREATED
        assert self.parent is not None
        return self.parent, CARRIED_PRODUCER_RUN, CARRIED_PRODUCER_CREATED

    def _family_handoff(self, generation: str, key: str, *, directory: Path | None = None,
                        forge: Callable[[dict[str, Any]], None] | None = None) -> Path:
        """The family handoff of ``key`` that ``generation``'s producer run writes at its checkout
        (a carried generation's native bundle asks the fixture hook to carry it forward) into
        ``directory`` (default: the publication's); ``forge`` edits the projection's producer
        record the adapter will return (a claim ``build`` must refuse or prove)."""

        mod, run_id, created = self.producer(generation)
        claim = run_claim_from_environment(support.environment(
            mod, run_id=run_id, event="repository_dispatch", job="publish-evidence", workflow=FAMILY_WORKFLOW))

        def projection_adjust(projection: dict[str, Any]) -> None:
            projection["key"] = key
            projection["provenance"]["producer"] = support.run_record(
                claim, event="repository_dispatch", created_at=at(created), display_title=PRODUCER_TITLE)
            if forge is not None:
                forge(projection["provenance"]["producer"])

        root = directory or self.directory
        native = fs.native_bundle(root / f"family-native-{generation}-{key}", mod, projection_adjust=projection_adjust,
                                  adjust=(lambda manifest: manifest.update(carry=True))
                                  if generation == CARRIED_GENERATION else None)
        output = root / f"family-handoff-{generation}-{key}"
        create_envelope(fs.producer_invocation(mod), family=FAMILY, key=key, bundle_dir=native, coverage_sha=mod.commit,
                        subject={"branch": mod.branch, "commit": mod.commit}, producer=claim, output=output)
        return output

    def forged_family(self, directory: Path, forge: Callable[[dict[str, Any]], None]) -> tuple[bytes, bytes]:
        """``(family handoff, collected family)`` archives of :data:`FAMILY_KEY`'s direct generation
        whose projection carries the producer record ``forge`` edits, collected by the real
        ``family collect`` into the new ``directory``."""

        directory.mkdir()
        with mock.patch.object(host, "call", self.hooks):
            handoff = self._family_handoff(DIRECT_GENERATION, FAMILY_KEY, directory=directory, forge=forge)
            archive = support.zip_directory(handoff)
            selected = {**self.handoff_selected(FAMILY_KEY), "digest": "sha256:" + sha256(archive),
                        "size": len(archive)}
            collect_family_leg(self.invocation("family"), key=FAMILY_KEY, input_dir=handoff,
                               coverage_sha=self.mod.commit, output=directory / "collected", selected=selected)
        return archive, support.zip_directory(directory / "collected")

    # -- invocations and render inputs ----------------------------------------------------------------

    def environment(self, job: str = "build", **changes: str) -> dict[str, str]:
        return {**support.pages_environment(self.mod, job=job), **changes}

    def invocation(self, job: str = "build", *, config: Path | None = None, **changes: str) -> Invocation:
        return build_invocation(self.mod.root, config, self.environment(job, **changes), root=self.kit)

    def bundles(self) -> list[dict[str, Any]]:
        return [load_bundle(self.collected[key]) for key in self.keys]

    def families(self) -> list[dict[str, Any]]:
        return [load_family(self.collected_family, key=FAMILY_KEY), load_family(None, key="mc26.3")]

    # -- the fake API -------------------------------------------------------------------------------

    def world(self, *, stage: str | None = None, family: bool = True, generation: str = DIRECT_GENERATION,
              max_requests: int | None = None) -> World:
        """A fresh fake holding the sources and ``generation``'s producer run with its family
        handoffs (and, with ``stage`` ``"build"`` or ``"finalize"``, this Pages run: its jobs and
        collected artifacts, the family legs collected from ``generation`` when ``family``, then
        the finalize prerequisites). ``max_requests`` is the fake's hard request stop."""

        world = World(self.mod.repository, max_requests=max_requests)
        world.api.set_branch("master", self.mod.commit, self.mod.tree)
        e2e = world.run(E2E_RUN, head_sha=self.mod.commit, created=0)
        world.jobs(e2e, [world.job(QS_HANDOFF_JOB.replace("{key}", key), steps=((QS_HANDOFF_STEP, 290, 310),))
                         for key in self.keys])
        self.seeds["e2e"] = e2e
        for key in self.keys:
            self.seeds[key] = world.artifact(grammar.handoff_name(key, 1), e2e, created=300,
                                             archive=self.archives[key], artifact_id=self.handoff_id(key))
        if self.delegated:
            self.seeds["tested"] = world.run(TESTED_RUN, head_sha=self.mod.commit, head_branch="topic",
                                             event="pull_request", created=-100)
        world.api.add_file(self.mod.commit, SOURCE, pin_workflow())
        mod, run_id, created = self.producer(generation)
        world.api.add_file(mod.commit, FAMILY_WORKFLOW, pin_workflow())
        producer = world.run(run_id, path=FAMILY_WORKFLOW, event="repository_dispatch", head_sha=mod.commit,
                             created=created, title=PRODUCER_TITLE)
        world.jobs(producer, [world.job(FAMILY_JOB, steps=((FAMILY_STEP, created + 290, created + 310),))])
        for key in self.family_keys:
            world.artifact(grammar.family_handoff_name(FAMILY, key, 1), producer, created=created + 300,
                           archive=self.archives[f"family:{generation}:{key}"],
                           artifact_id=self.family_handoff_id(key, generation))
        if stage is not None:
            self.seed_pages(world, stage=stage, family=family, generation=generation)
        return world

    def pages_jobs(self, world: World, *, stage: str) -> list[dict[str, Any]]:
        listed = [world.job(caller_job_name("verify_kit"), started=1000, completed=1010),
                  world.job(api_job_name("publish", "admit"), started=1010, completed=1020)]
        listed += [world.job(api_job_name("publish", "collect", key=key), started=1020, completed=1100)
                   for key in self.keys]
        listed += [world.job(api_job_name("publish", "family", family=FAMILY, key=key), started=1020, completed=1100)
                   for key in self.keys]
        if stage == "build":
            listed.append({"id": 79999, "name": BUILD_JOB, "status": "in_progress", "conclusion": None,
                           "started_at": at(1100), "completed_at": None, "steps": []})
        else:
            listed += [world.job(BUILD_JOB, started=1100, completed=1200),
                       world.job(caller_job_name("deploy"), started=1200, completed=1300)]
        return listed

    def seed_pages(self, world: World, *, stage: str, family: bool = True, promotion: bytes | None = None,
                   generation: str = DIRECT_GENERATION) -> dict[str, Any]:
        run = world.run(PAGES_RUN, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, status="in_progress",
                        conclusion=None, created=1000, kit_sha=support.KIT_SHA, title="Project site")
        world.jobs(run, self.pages_jobs(world, stage=stage))
        for key in self.keys:
            world.artifact(grammar.collected_name(key), run, created=1050, archive=self.archives[f"collected:{key}"],
                           artifact_id=self.collected_id(key))
        if family:
            for key in self.family_keys:
                world.artifact(grammar.collected_family_name(FAMILY, key), run, created=1060,
                               archive=self.archives[f"collected-family:{generation}:{key}"],
                               artifact_id=self.collected_family_id(key))
        if promotion is not None:
            world.artifact(grammar.PROMOTION_NAME, run, created=1150, archive=promotion, artifact_id=PROMOTION_ID)
        return run

    def collected_family_archive(self, key: str, selected: Mapping[str, Any], *,
                                 generation: str = DIRECT_GENERATION) -> bytes:
        """``generation``'s collected family artifact of ``key`` recording ``selected`` (the JSON
        object, written as is) as the generation its ``select`` step chose."""

        data = canonical_json(dict(selected))
        memo = (generation, key, data)
        if memo not in self._variants:
            variant = self.directory / "variants" / f"{generation}-{key}-{len(self._variants)}"
            shutil.copytree(self.collected_families[generation][key], variant)
            (variant / FAMILY_SELECTED_NAME).write_bytes(data)
            self._variants[memo] = support.zip_directory(variant)
        return self._variants[memo]

    def select_family(self, world: World, key: str, selected: Mapping[str, Any], *,
                      generation: str = DIRECT_GENERATION) -> bytes:
        """Re-seed this Pages run's collected family artifact of ``key`` as collected from the
        generation ``selected`` names (same id and upload time); returns its archive."""

        archive = self.collected_family_archive(key, selected, generation=generation)
        world.artifact(grammar.collected_family_name(FAMILY, key), {"id": PAGES_RUN, "head_sha": self.mod.commit,
                                                                   "head_branch": self.mod.branch},
                       created=1060, archive=archive, artifact_id=self.collected_family_id(key))
        return archive

    def pages_owner(self, world: World, run_id: int, *, head: support.FixtureMod | None = None,
                    created: float = 800) -> dict[str, Any]:
        """An earlier successful Pages run at ``head`` (default: the publication's head)."""

        return world.run(run_id, path=PAGES_WORKFLOW_PATH, head_sha=(head or self.mod).commit, created=created,
                         kit_sha=support.KIT_SHA, title="Project site")

    def family_cache(self, world: World, owner: Mapping[str, Any], key: str, coverage_sha: str, *,
                     generation: str = DIRECT_GENERATION, artifact_id: int, created: float = 900) -> dict[str, Any]:
        """``generation``'s family handoff of ``key`` rolled forward by ``owner`` as the family cache
        named with ``coverage_sha`` (``refresh`` copies the producer's bundle verbatim)."""

        return world.artifact(grammar.family_cache_name(FAMILY, key, coverage_sha), owner, created=created,
                              archive=self.archives[f"family:{generation}:{key}"], artifact_id=artifact_id)

    def expire_family_handoffs(self, world: World, generation: str = DIRECT_GENERATION) -> None:
        """Replace ``generation``'s family handoffs by their expired records (retention passed)."""

        mod, run_id, created = self.producer(generation)
        producer = {"id": run_id, "head_sha": mod.commit, "head_branch": mod.branch}
        for key in self.family_keys:
            world.artifact(grammar.family_handoff_name(FAMILY, key, 1), producer, created=created + 300, expired=True,
                           archive=self.archives[f"family:{generation}:{key}"],
                           artifact_id=self.family_handoff_id(key, generation))


def bp_bundle(directory: Path) -> tuple[support.FixtureMod, dict[str, Any], Path]:
    """A Block Pops-like mod, the render input of its single compacted key and a kit root."""

    mod = support.materialize("bp_like", directory / "bp-mod")
    key = hashlib.sha256(b"master").hexdigest()[:24]
    environ = support.environment(mod)
    producer = support.invocation(mod, environ, implementation_sha=mod.commit)
    handoff_claim, tested_claim = support.claims(environ, subject=mod.subject)
    with mock.patch.object(host, "call", support.InProcessHost()):
        support.synthesize(producer, key, mod.subject, directory / "bp-e2e")
        prepare.prepare_handoff(producer, e2e_root=directory / "bp-e2e", key=key, output=directory / "bp-handoff",
                                subject=mod.subject, tested=tested_claim, handoff=handoff_claim)
        raw = (directory / "bp-handoff" / "manifest.json").read_bytes()
        draft = support.selection_draft(json.loads(raw), raw, artifact_id=11, digest="sha256:" + "a" * 64,
                                        size=4096)
        draft_path = directory / "bp-draft.json"
        draft_path.write_bytes(canonical_json(draft))
        pages = build_invocation(mod.root, None, support.pages_environment(mod, job="collect"))
        compact_module.compact_bundle(pages, key=key, input_dir=directory / "bp-handoff", selection_path=draft_path,
                                      output=directory / "bp-collected")
    return mod, load_bundle(directory / "bp-collected"), copy_kit(directory / "bp-kit")
