"""Shared builders for the MB6 build, render and refresh tests; this module holds no test case.

:class:`Publication` materializes a Quick Skin-like fixture mod (``qs_like`` with a pixelated icon,
the family hook of :mod:`tests.test_family_support`, a matrix ``project.description`` and a
``.gitignore``), produces both keys' handoffs through the real producer, drafts their selections
from the facts a seeded :class:`tests.test_select.World` serves (:func:`selection_draft`, exactly as
``authenticate`` documents them, so these fixtures never depend on the control plane's internal
checks), compacts them with the real ``compact`` and collects one family leg with the real
``family collect``, as the Pages ``collect``/``family`` jobs do. :meth:`Publication.world`
seeds a fresh fake with the same sources plus, on request, this Pages run in its build or finalize
stage. :func:`bp_bundle` produces a Block Pops-like compact bundle for render-only tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
from pathlib import Path
from typing import Any
from unittest import mock

import mod_base
from mod_base.adapter import host
from mod_base.evidence import compact as compact_module
from mod_base.evidence import prepare
from mod_base.github import runs
from mod_base.family.envelope import create_envelope
from mod_base.family.paired import collect_family
from mod_base.io.atomic_directory import atomic_directory
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.pages.build import render_site
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
    """The Quick Skin-like site: pixelated icon, family hook and policy, matrix description."""

    config_path = root / "site" / "mod-base.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["project"]["icon"] = {"path": "icon.png", "rendering": "pixelated"}
    for family in config["families"]:
        family["image_policy"] = dict(fs.IMAGE_POLICY)
    config_path.write_bytes(canonical_json(config))
    (root / "icon.png").write_bytes(icon_png())
    adapter = root / "scripts" / "pages" / "mod_base_adapter.py"
    adapter.write_text(adapter.read_text(encoding="utf-8") + fs.FAMILY_HOOK, encoding="utf-8")
    matrix_path = root / "release" / "release-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["project"] = {"description": DESCRIPTION}
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")


def selection_draft(invocation: Invocation, handoff: Path, *, run: dict[str, Any], artifact: dict[str, Any],
                    workflow_path: str = SOURCE) -> dict[str, Any]:
    """The draft ``authenticate`` writes for the direct handoff ``handoff`` uploaded as ``artifact``
    by ``run`` (:meth:`World.run` and :meth:`World.artifact` records), in the Pages job of
    ``invocation``."""

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
                   "tested_run": runs.run_record(run, provenance["tested"]), "reuse": provenance["reuse"],
                   "kit_binding": {"source": "workflow_file", "sha": manifest["kit"]["sha"]}},
        "expectation_sha256": manifest["expectation"]["sha256"],
        "source_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "extensions_verified": [],
    }


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
    """One synthetic Quick Skin-like publication (see module docstring); read-only once built."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.mod = support.materialize("qs_like", directory / "mod", mutate=qs_site_mutation)
        self.kit = copy_kit(directory / "kit")
        environ = support.environment(self.mod, run_id=E2E_RUN)
        producer = support.invocation(self.mod, environ, implementation_sha=self.mod.commit)
        handoff_claim, tested_claim = support.claims(environ, subject=self.mod.subject)
        self.handoffs: dict[str, Path] = {}
        with mock.patch.object(host, "call", support.InProcessHost()):
            for key in QS_KEYS:
                e2e = directory / f"e2e-{key}"
                support.synthesize(producer, key, self.mod.subject, e2e)
                self.handoffs[key] = directory / f"handoff-{key}"
                prepare.prepare_handoff(producer, e2e_root=e2e, key=key, output=self.handoffs[key],
                                        subject=self.mod.subject, tested=tested_claim, handoff=handoff_claim)
            native = fs.native_bundle(directory / "family-native", self.mod)
            self.family_handoff = directory / "family-handoff"
            create_envelope(fs.producer_invocation(self.mod), family=FAMILY, key=FAMILY_KEY, bundle_dir=native,
                            coverage_sha=self.mod.commit,
                            subject={"branch": self.mod.branch, "commit": self.mod.commit},
                            producer=fs.producer_claim(self.mod), output=self.family_handoff)
        self.archives = {key: support.zip_directory(path) for key, path in self.handoffs.items()}
        self.archives[FAMILY] = support.zip_directory(self.family_handoff)
        self.seeds: dict[str, Any] = {}
        self.world()
        self.collected: dict[str, Path] = {}
        with mock.patch.object(host, "call", support.InProcessHost()):
            collect = self.invocation("collect")
            for key in QS_KEYS:
                draft = selection_draft(collect, self.handoffs[key], run=self.seeds["e2e"],
                                        artifact=self.seeds[key])
                draft_path = directory / f"draft-{key}.json"
                draft_path.write_bytes(canonical_json(draft))
                self.collected[key] = directory / "collected" / key
                compact_module.compact_bundle(collect, key=key, input_dir=self.handoffs[key],
                                              selection_path=draft_path, output=self.collected[key])
            self.collected_family = directory / "collected-family"
            collect_family(self.invocation("family"), family=FAMILY, key=FAMILY_KEY, input_dir=self.family_handoff,
                           expected_coverage_sha=self.mod.commit, output=self.collected_family)
        for key in QS_KEYS:
            self.archives[f"collected:{key}"] = support.zip_directory(self.collected[key])
        self.archives["collected-family"] = support.zip_directory(self.collected_family)

    def environment(self, job: str = "build", **changes: str) -> dict[str, str]:
        return {**support.pages_environment(self.mod, job=job), **changes}

    def invocation(self, job: str = "build", *, config: Path | None = None, **changes: str) -> Invocation:
        return build_invocation(self.mod.root, config, self.environment(job, **changes), root=self.kit)

    def bundles(self) -> list[dict[str, Any]]:
        return [load_bundle(self.collected[key]) for key in QS_KEYS]

    def families(self) -> list[dict[str, Any]]:
        return [load_family(self.collected_family, key=FAMILY_KEY), load_family(None, key="mc26.3")]

    def world(self, *, stage: str | None = None, family: bool = True) -> World:
        """A fresh fake holding the sources (and, with ``stage`` ``"build"`` or ``"finalize"``, this
        Pages run: its jobs and collected artifacts, then the finalize prerequisites)."""

        world = World(self.mod.repository)
        world.api.set_branch("master", self.mod.commit, self.mod.tree)
        e2e = world.run(E2E_RUN, head_sha=self.mod.commit, created=0)
        world.jobs(e2e, [world.job(QS_HANDOFF_JOB.replace("{key}", key), steps=((QS_HANDOFF_STEP, 290, 310),))
                         for key in QS_KEYS])
        self.seeds["e2e"] = e2e
        for key in QS_KEYS:
            self.seeds[key] = world.artifact(grammar.handoff_name(key, 1), e2e, created=300,
                                             archive=self.archives[key], artifact_id=HANDOFF_IDS[key])
        world.api.add_file(self.mod.commit, SOURCE, pin_workflow())
        producer = world.run(PRODUCER_RUN, path=FAMILY_WORKFLOW, event="repository_dispatch", head_sha=self.mod.commit,
                             created=100)
        world.jobs(producer, [world.job(FAMILY_JOB, steps=((FAMILY_STEP, 390, 410),))])
        world.artifact(grammar.family_handoff_name(FAMILY, FAMILY_KEY, 1), producer, created=400,
                       archive=self.archives[FAMILY], artifact_id=FAMILY_HANDOFF_ID)
        if stage is not None:
            self.seed_pages(world, stage=stage, family=family)
        return world

    def pages_jobs(self, world: World, *, stage: str) -> list[dict[str, Any]]:
        listed = [world.job(caller_job_name("verify_kit"), started=1000, completed=1010),
                  world.job(api_job_name("publish", "admit"), started=1010, completed=1020)]
        listed += [world.job(api_job_name("publish", "collect", key=key), started=1020, completed=1100)
                   for key in QS_KEYS]
        listed += [world.job(api_job_name("publish", "family", family=FAMILY, key=key), started=1020, completed=1100)
                   for key in QS_KEYS]
        if stage == "build":
            listed.append({"id": 79999, "name": BUILD_JOB, "status": "in_progress", "conclusion": None,
                           "started_at": at(1100), "completed_at": None, "steps": []})
        else:
            listed += [world.job(BUILD_JOB, started=1100, completed=1200),
                       world.job(caller_job_name("deploy"), started=1200, completed=1300)]
        return listed

    def seed_pages(self, world: World, *, stage: str, family: bool = True,
                   promotion: bytes | None = None) -> dict[str, Any]:
        run = world.run(PAGES_RUN, path=PAGES_WORKFLOW_PATH, head_sha=self.mod.commit, status="in_progress",
                        conclusion=None, created=1000, kit_sha=support.KIT_SHA, title="Project site")
        world.jobs(run, self.pages_jobs(world, stage=stage))
        for key in QS_KEYS:
            world.artifact(grammar.collected_name(key), run, created=1050, archive=self.archives[f"collected:{key}"],
                           artifact_id=COLLECTED_IDS[key])
        if family:
            world.artifact(grammar.collected_family_name(FAMILY, FAMILY_KEY), run, created=1060,
                           archive=self.archives["collected-family"], artifact_id=COLLECTED_FAMILY_ID)
        if promotion is not None:
            world.artifact(grammar.PROMOTION_NAME, run, created=1150, archive=promotion, artifact_id=PROMOTION_ID)
        return run


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
