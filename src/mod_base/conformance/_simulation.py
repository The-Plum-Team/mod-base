"""The conformance simulation itself (MB10, SPEC §9.1): synthetic generations end to end.

It runs in the child process of ``mod_base conformance`` (see :mod:`mod_base.conformance.run`) on a
private snapshot repository, with ``mod_base.adapter.host.call`` replaced by the in-process host of
:mod:`mod_base.conformance._hooks` and every API read served by the seeded fake of
:mod:`mod_base.conformance._world`. Each stage calls the same kit entry points the workflows run,
with the environment of the job that runs them (``GITHUB_JOB``, ``GITHUB_WORKFLOW_REF``, run
identity, the simulated kit SHA), so placement, token gating and every current-attempt check apply
exactly as in production. Every simulated ``publish``/``finalize`` job must stay within its
client's 160-read API budget and change nothing (the fake accepts ``DELETE`` only for a rotation
that is not a dry run). The checkout and host-fact checks that live in the CLI handlers
(``check_checkouts``) are not simulated.

Stages, in order (a failed check raises ``MbError`` with reason ``conformance``):

1. **Discovery.** ``targets`` and ``expectation`` name the repository and every key; ``--keys``
   selects a subset, onto which every later ``targets`` result is projected.
2. **Admission before evidence.** ``recovery`` waits for the v1 cutover
   (``awaiting-complete-v1-evidence``), a moved head is ``stale-implementation`` and, with
   ``defer_on_active_source_runs``, an active source run defers ``recovery`` but not ``manual``.
3. **Producer** (``source.workflow`` run, a ``prepare-evidence`` job): the fixtures module's
   ``synthesize`` writes the mod's own packaged output, then ``prepare`` (R1 and the runtime-metric
   cross-check), ``validate --kind handoff`` in the producing run (R2), and, when eligible, ``anchor
   identity/create/validate`` bound to the uploaded handoff. With ``--families``, every configured
   family gets an ``available`` native bundle per key (the fixtures module's ``family_bundle``) in
   its ``mod-base.family.envelope``, validated as ``validate --kind family``.
4. **Admission after evidence:** ``deploy`` (nominating exactly this run's handoffs), ``family``,
   ``recovery`` and ``manual`` are each eligible for the configured admission mode.
5. **Collect** (Pages ``collect`` jobs): ``select`` by nomination and without one (same artifact),
   ``download`` by id, ``authenticate`` (job graph, display title, kit binding, extensions),
   ``compact``, ``validate --kind compact`` and ``--bind-raw`` (byte-identical WebP). **Family**
   legs: ``select --family``, ``download``, ``family collect`` (``available``).
6. **Build** into a temporary ``_site`` with every current-attempt check, then the site assertions
   of :mod:`mod_base.conformance._site` and the promotion.
7. **Refresh** every key and family leg (cache names, and a baseline name exactly for a new
   complete generation: a key selected as a handoff whose baseline no earlier refresh retained);
   once a sibling job has uploaded its cache, the run's next artifact listing reports a
   ``total_count`` one row off its rows (GitHub's eventually consistent listing during concurrent
   uploads), which each later refresh must read again, exactly once, within its budget;
   **rotate** ``--dry-run`` against a superseded earlier generation seeded at the same head (the
   plan must be exactly what the independent oracle :mod:`mod_base.conformance._rotation` derives,
   cut only by the deletion budget; nothing deleted) and a final ``recovery`` admission that
   reports ``current``.
8. **Variants**, each only when the configuration and fixtures make it applicable (otherwise it is
   reported as skipped with the reason): ``attested`` reuse (``source.attestation_job``),
   ``delegated`` reuse (``source.delegated_reuse_extension`` plus the fixtures module's
   ``delegated_extensions``; the tested claim names the tested pull-request run itself: its run,
   attempt, branch and commit), the ``superseded``/``unavailable`` family outcomes (listed in the
   fixtures module's ``FAMILY_OUTCOMES``) and the newest-run rule (``source.require_newest_run``: a
   newer failed source run refuses the older evidence, nominated or searched).
9. **Later generations** (:mod:`mod_base.conformance._generations`): a real rotation of the first
   generation, a documentation-only push, a second generation at the new head (family legs carried
   forward from the first generation's caches, or absent without ``carry_forward``), dry-run
   rotations across the anchor successor grace, and a same-head publication interleaved with the
   real rotation of the second generation; then the ``carried`` variant, a third head whose family
   legs walk back to the carried cache.
10. **The ``selected`` variant**, last because it moves the protected head once more (``compose``
    plus the fixtures module's ``selected_extensions``): a mod may recompute a selection as the Git
    diff from its baseline's commit to the tested head (Quick Skin does), so the selected
    generation lies on a new head, one commit adding the fixtures module's ``SELECTED_CHANGE``
    (default :data:`SELECTED_DOCUMENT`) on top of the newest published baseline's commit. R3
    refuses the selected handoff compacted without composition; ``compose`` completes it with that
    baseline; and a baseline that is not the retained upload of a successful Pages refresh job (the
    same bytes under its name, uploaded at its commit by a source run or outside a Pages run's
    retention step) is refused, by R3 or by the adapter's own ``compose`` hook, never accepted.
    ``selected_extensions`` may ask ``ctx.api.retained_baseline`` for the retained baseline of any
    declared key at that commit (a stand-in for a key outside ``--keys``:
    :meth:`Simulation.baseline_provider`).
"""

from __future__ import annotations

import hashlib
import io
import os
import tarfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mod_base
from mod_base.adapter import host
from mod_base.adapter.protocol import HookFailed
from mod_base.conformance import _site
from mod_base.conformance._fixture_api import MAX_FIXTURE_RESPONSES, FixtureGitHub
from mod_base.conformance._generations import Generation, Generations
from mod_base.conformance._hooks import InProcessHooks
from mod_base.conformance._rotation import expected_retirements
from mod_base.conformance._snapshot import require_snapshot
from mod_base.conformance._world import KIT_SHA, TOKEN, World, timestamp, zip_directory, zip_files
from mod_base.config import load_config
from mod_base.errors import MbError, Unavailable, single_line
from mod_base.evidence import validate as evidence_validate
from mod_base.evidence.anchor import anchor_identity, create_anchor, validate_anchor_dir
from mod_base.evidence.compact import compact_bundle
from mod_base.evidence.compose import compose_selected
from mod_base.evidence.expectation import derive_expectation, read_extensions, target_for_key, tested_run_projection
from mod_base.evidence.prepare import prepare_handoff
from mod_base.family.envelope import create_envelope
from mod_base.family.paired import SELECTED_NAME, collect_family
from mod_base.github import artifacts, contents, runs
from mod_base.imaging.png import pattern_png
from mod_base.io.bounded_zip import LIMITS_BY_KIND
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file
from mod_base.model.documents import run_claim_from_environment, validate_promotion
from mod_base.pages.admission import Admission, WakeInputs, admit
from mod_base.pages.authenticate import authenticate_selection, write_new_file
from mod_base.pages.build import build_site
from mod_base.pages.refresh import RefreshResult, refresh_bundle
from mod_base.pages.rotate import rotate_generation
from mod_base.pages.select import Selected, display_title, handoff_job_name, select_evidence
from mod_base.runtime import Invocation, build_invocation
from mod_base.workflow import (
    PAGES_WORKFLOW_PATH,
    api_job_name,
    caller_job_name,
    step_name,
    unexpanded_api_job_name,
)

#: Run ids of the simulated generations.
SOURCE_RUN = 4242
FAMILY_RUN = 4300
DELEGATED_TESTED_RUN = 4400
DELEGATED_RUN = 4401
SELECTED_RUN = 4402
FORGED_OWNER_RUN = 4403
FORGED_WINDOW_RUN = 4404
#: The source run of the published baseline's commit that uploads the ``selected`` variant's
#: source-run forgery (``FORGED_OWNER_RUN``, at the selected head, only produces its handoff).
FORGED_SOURCE_RUN = 4405
ATTESTED_TESTED_RUN = 4500
ATTESTED_RUN = 4501
ACTIVE_RUN = 4600
NEWEST_RUN = 4700
PAGES_RUN = 9000
PREVIOUS_PAGES_RUN = 8990
VARIANT_PAGES_RUN = 9010
FORGED_PAGES_RUN = 9020
ROTATION_RUN = 9100
#: The scheduled Pages run whose ``recovery`` admission finds a published generation current (a
#: run never counts its own caches).
RECOVERY_RUN = 9900
#: The (never uploaded) artifact id the ``family-outcomes`` variant's selections record.
OUTCOME_ARTIFACT = 900001
#: The Pages runs that retain the stand-in baselines of keys outside ``--keys`` (one per
#: ``selected_extensions`` call that asks for one; see :meth:`Simulation.baseline_provider`).
STAND_IN_PAGES_RUNS = range(9500, 9600)
#: The new file the ``selected`` head adds when the fixtures module names no ``SELECTED_CHANGE``.
SELECTED_DOCUMENT = "docs/mod-base-conformance/selected.md"
#: Seconds between the last event of the later generations and the ``selected`` variant's head.
SELECTED_DELAY = 1000
#: The bytes of the ``selected`` head's new file.
SELECTED_TEXT = b"mod-base conformance: a synthetic change that the selected generation re-tests.\n"
#: The archive of a stand-in baseline (:meth:`Simulation.baseline_provider`): never a compact bundle.
STAND_IN_ARCHIVE = zip_files({"conformance-stand-in.txt": b"A stand-in baseline of a key outside --keys, not a "
                                                          b"compact bundle.\n"})
#: Optional conformance functions of the fixtures module (``config.adapter.fixtures_path``).
FAMILY_BUNDLE = "family_bundle"
DELEGATED_EXTENSIONS = "delegated_extensions"
SELECTED_EXTENSIONS = "selected_extensions"
SELECTED_CHANGE = "SELECTED_CHANGE"
FAMILY_OUTCOMES = "FAMILY_OUTCOMES"
FAMILY_OUTCOME_VALUES = ("available", "superseded", "unavailable")
#: The producer and Pages job ids of the simulation (placement: a mod job is ``prepare-evidence``).
PRODUCER_JOB = "conformance-producer"
FAMILY_PRODUCER_JOB = "conformance-family-producer"
#: Admission reasons that publish, per admission mode.
PUBLISHING = {"always": {"always"}, "progress": {"initial-ordinary", "final-complete"}}


def _fail(message: str) -> MbError:
    return MbError(message, reason="conformance")


def sha256_digest(data: bytes) -> str:
    """The ``sha256:<hex>`` artifact digest of ``data``."""

    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Produced:
    """One key's evidence produced by one source run."""

    key: str
    handoff_dir: Path
    handoff: dict[str, Any]
    manifest: dict[str, Any]
    expectation: dict[str, Any]
    anchor: dict[str, Any] | None = None


@dataclass
class Settings:
    repo: Path
    kit_root: Path
    keys: tuple[str, ...] | None
    families: bool
    work: Path


@dataclass
class Report:
    checks: int = 0
    keys: list[dict[str, Any]] = field(default_factory=list)
    families: list[dict[str, Any]] = field(default_factory=list)
    variants: dict[str, str] = field(default_factory=dict)
    admission: list[str] = field(default_factory=list)
    site: dict[str, Any] = field(default_factory=dict)


class Simulation(Generations):
    """One conformance run over the snapshot repository ``settings.repo`` (see the module docstring)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.repo = Path(os.path.abspath(settings.repo))
        self.kit_root = Path(os.path.abspath(settings.kit_root))
        self.work = settings.work
        self.config = load_config(self.repo)
        self.branch = self.config.canonical_branch
        self.source = self.config.source
        if not self.config.adapter.get("fixtures_path"):
            raise _fail("conformance needs config.adapter.fixtures_path (the synthesize fixtures module)")
        self.head, self.tree = require_snapshot(self.repo, branch=self.branch)
        self.subject = {"branch": self.branch, "commit": self.head, "tree": self.tree}
        self.hooks = InProcessHooks(keys=None, scratch=self._directory("hooks"))
        self.report = Report()
        self.repository = ""
        self.keys: list[str] = []
        self.world: World | None = None
        self.produced: dict[str, Produced] = {}
        self.family_handoffs: dict[tuple[str, str], dict[str, Any]] = {}
        #: The producer run of every family leg's newest generation (carried legs keep theirs).
        self.family_producers: dict[tuple[str, str], int] = {}
        self.anchored: set[str] = set()
        #: Every ``mb-baseline`` name a successful simulated refresh retained, and the newest such
        #: artifact record of each key.
        self.retained_baselines: set[str] = set()
        self.latest_baselines: dict[str, dict[str, Any]] = {}
        #: Every key the adapter declares at the first head (``--keys`` may simulate fewer).
        self.declared_keys: frozenset[str] = frozenset()
        self._stand_in_runs = iter(STAND_IN_PAGES_RUNS)
        self.first = Generation(number=1, run_id=PAGES_RUN, start=1000, head=self.head, tree=self.tree)
        self._invocations: dict[tuple[tuple[tuple[str, str], ...], str | None, bool], Invocation] = {}

    # -- helpers --------------------------------------------------------------------------------------

    def check(self, condition: bool, message: str) -> None:
        if not condition:
            raise _fail(message)
        self.report.checks += 1

    @contextmanager
    def budget(self, label: str, api: Any = None) -> Iterator[None]:
        """One simulated Pages publication job (``publish``/``finalize``) stays within its client's read
        budget (SPEC §3.0: 160, the ``admit``/``select``/``build``/``refresh`` commands' cap) and
        changes nothing. Rotation is bounded by its deletion budget instead."""

        client = api if api is not None else self.api
        before, mutations = client.request_count, len(client.mutations)
        yield
        used = client.request_count - before
        self.report.site["max_job_reads"] = max(used, self.report.site.get("max_job_reads", 0))
        self.check(used <= lim.MAX_PAGES_API_READS,
                   f"{label} made {used} API requests, more than the {lim.MAX_PAGES_API_READS}-read budget of a job")
        self.check(len(client.mutations) == mutations, f"{label} changed the repository's artifacts or workflows")

    def _directory(self, name: str) -> Path:
        path = self.work / name
        path.mkdir(parents=True, exist_ok=False)
        return path

    def environ(self, *, workflow: str, run_id: int, job: str, event: str = "workflow_dispatch",
                token: bool = False, sha: str | None = None) -> dict[str, str]:
        values = {
            "GITHUB_REPOSITORY": self.repository, "GITHUB_SHA": sha or self.head, "GITHUB_RUN_ID": str(run_id),
            "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REF": f"refs/heads/{self.branch}", "GITHUB_REF_NAME": self.branch,
            "GITHUB_WORKFLOW_REF": grammar.workflow_ref(self.repository, workflow, self.branch),
            "GITHUB_EVENT_NAME": event, "GITHUB_JOB": job, "MOD_BASE_KIT_SHA": KIT_SHA,
        }
        if token:
            values["GH_TOKEN"] = TOKEN
        return values

    def invocation(self, environ: Mapping[str, str], *, implementation_sha: str | None = None,
                   check_repository: bool = True) -> Invocation:
        """The (memoized, immutable) invocation of one simulated job environment."""

        memo = (tuple(sorted(environ.items())), implementation_sha, check_repository)
        if memo not in self._invocations:
            self._invocations[memo] = build_invocation(self.repo, None, environ, check_repository=check_repository,
                                                       implementation_sha=implementation_sha, root=self.kit_root)
        return self._invocations[memo]

    def pages(self, job: str, run_id: int = PAGES_RUN) -> Invocation:
        return self.invocation(self.environ(workflow=PAGES_WORKFLOW_PATH, run_id=run_id, job=job, token=True))

    def producer(self, run: Mapping[str, Any], *, job: str = PRODUCER_JOB) -> tuple[Invocation, dict[str, str]]:
        environ = self.environ(workflow=run["path"], run_id=run["id"], job=job, event=run["event"])
        return self.invocation(environ, implementation_sha=self.head), environ

    @property
    def api(self) -> Any:
        assert self.world is not None
        return self.world.api

    def title(self, commit: str) -> str:
        """A source run's display title: the configured template for ``commit`` or a fixed title."""

        pages = self.invocation({"GITHUB_REPOSITORY": self.repository, "GITHUB_SHA": self.head},
                                check_repository=False)
        return display_title(pages, commit) or "Conformance packaged E2E"

    def fixture_call(self, invocation: Invocation, name: str, *, fixture_api: Any = None, **arguments: Any) -> Any:
        try:
            return self.hooks.call_fixture(invocation, name, fixture_api=fixture_api, **arguments)
        except MbError:
            raise
        except Exception as exc:  # noqa: BLE001 - the fixture's own failure is a conformance failure
            raise _fail(f"fixtures {name} failed: {single_line(exc, limit=300)}") from exc

    # -- 1. discovery -------------------------------------------------------------------------------------

    def discover(self) -> None:
        local = self.invocation({"GITHUB_SHA": self.head}, check_repository=True)
        branches = None
        if self.config.targets["mode"] == "enrolled-branches":
            branches = [{"name": self.branch, "commit": self.head, "tree": self.tree}]
        targets = self.hooks(local, "targets", {"branches": branches})
        keys = [target["key"] for target in targets]
        self.check(bool(keys), "the adapter declares no target for the protected head")
        self.declared_keys = frozenset(keys)
        projection = tested_run_projection({"branch": self.branch}, self.source["events"]["canonical"][0])
        expectation = self.hooks(local, "expectation", {"target": targets[0], "tested_run": projection,
                                                        "extensions": {}})
        self.repository = grammar.require(grammar.REPOSITORY, expectation.get("repository"), "expectation repository")
        wanted = list(self.settings.keys) if self.settings.keys is not None else keys
        unknown = sorted(set(wanted) - set(keys))
        if unknown:
            raise _fail(f"the adapter declares no key {unknown[0]} (it declares {', '.join(sorted(keys))})"[:400])
        self.keys = sorted(wanted)
        self.hooks.keys = frozenset(self.keys)

    # -- worlds -------------------------------------------------------------------------------------------

    def new_world(self) -> World:
        return World(repository=self.repository, config=self.config, head=self.head, tree=self.tree)

    def source_run(self, world: World, run_id: int, *, created: float, event: str | None = None,
                   status: str = "completed", conclusion: str | None = "success") -> dict[str, Any]:
        return world.run(run_id, path=self.source["workflow"], event=event or self.source["events"]["canonical"][0],
                         created=created, updated=created + 400, status=status, conclusion=conclusion,
                         title=self.title(self.head))

    # -- 2 and 4. admission ---------------------------------------------------------------------------------

    def admit(self, operation: str, *, now: float, world: World | None = None, wake: WakeInputs | None = None,
              invocation: Invocation | None = None, run_id: int = PAGES_RUN) -> Admission:
        """The ``admit`` job of the Pages run ``run_id``."""

        api = (world or self.world).api  # type: ignore[union-attr]
        with self.budget(f"admit {operation}", api):
            result = admit(invocation or self.pages("admit", run_id), api=api, operation=operation,
                           wake=wake or WakeInputs(), now=timestamp(now), sleep=lambda _seconds: None)
        self.report.admission.append(f"{operation}:{result.reason}")
        return result

    def admission_before_evidence(self) -> None:
        admitted = self.admit("recovery", now=100)
        self.check(not admitted.eligible and admitted.reason == "awaiting-complete-v1-evidence",
                   f"recovery before any evidence admitted {admitted.reason!r}, not awaiting-complete-v1-evidence")
        moved = self.invocation(self.environ(workflow=PAGES_WORKFLOW_PATH, run_id=PAGES_RUN, job="admit", token=True,
                                             sha="0" * 40), check_repository=True)
        stale = self.admit("recovery", now=100, invocation=moved)
        self.check(not stale.eligible and stale.reason == "stale-implementation",
                   f"a moved head admitted {stale.reason!r}, not stale-implementation")
        if not self.config.admission["defer_on_active_source_runs"]:
            return
        busy = self.new_world()
        self.source_run(busy, ACTIVE_RUN, created=50, status="in_progress", conclusion=None)
        deferred = self.admit("recovery", now=100, world=busy)
        self.check(not deferred.eligible and deferred.reason == "deferred-active-source",
                   f"an active source run did not defer recovery ({deferred.reason!r})")
        manual = self.admit("manual", now=100, world=busy)
        self.check(manual.reason != "deferred-active-source", "manual admission was deferred for an active source run")

    def admit_deploy(self, run_id: int, produced: Mapping[str, Produced], *, now: float,
                     pages_run: int = PAGES_RUN) -> Admission:
        """A ``deploy`` wake of source run ``run_id`` (admitted by the Pages run ``pages_run``)
        publishes and nominates exactly its handoffs."""

        mode = self.config.admission["mode"]
        deploy = self.admit("deploy", now=now, wake=WakeInputs(run_id=run_id, sha=self.head), run_id=pages_run)
        self.check(deploy.eligible and deploy.reason in PUBLISHING[mode],
                   f"the deploy wake admitted {deploy.reason!r} (eligible={deploy.eligible})")
        self.check(deploy.bundle_keys == self.keys, "the deploy admission does not publish exactly the simulated keys")
        self.check(deploy.heads == {self.branch: self.head}, "the deploy admission names other heads")
        wanted = {key: item.handoff["id"] for key, item in produced.items()}
        self.check({key: deploy.nominations.get(key) for key in self.keys} == wanted,
                   "the deploy admission does not nominate exactly this run's handoffs")
        families = [(family["id"], key) for family in self.config.families for key in self.keys]
        self.check([(entry["family"], entry["key"]) for entry in deploy.families] == sorted(families),
                   "the deploy admission does not list every family leg")
        return deploy

    def admission_after_evidence(self, produced: Mapping[str, Produced], family_wake: WakeInputs | None) -> dict[str, int]:
        mode = self.config.admission["mode"]
        deploy = self.admit_deploy(SOURCE_RUN, produced, now=900)
        for operation in ("recovery", "manual"):
            admitted = self.admit(operation, now=900)
            expected = {"manual"} if operation == "manual" else PUBLISHING[mode]
            self.check(admitted.eligible and admitted.reason in expected,
                       f"{operation} after the evidence admitted {admitted.reason!r}")
        if family_wake is not None:
            family = self.admit("family", now=900, wake=family_wake)
            self.check(family.eligible and family.reason in ({"family-wake"} if mode == "always" else PUBLISHING[mode]),
                       f"the family wake admitted {family.reason!r}")
            leg = f"{family_wake.family}/{family_wake.bundle_key}"
            self.check(family.nominations.get(leg) == family_wake.artifact_id,
                       "the family wake does not nominate its family handoff")
        return dict(deploy.nominations)

    def admit_current(self, *, now: float, label: str) -> None:
        """After a publication, recovery at the published head has nothing left to publish."""

        final = self.admit("recovery", now=now, run_id=RECOVERY_RUN)
        self.check(not final.eligible and final.reason == "current",
                   f"recovery after {label} admitted {final.reason!r}, not current")

    # -- 3. producer ----------------------------------------------------------------------------------

    def produce(self, world: World, run: dict[str, Any], key: str, *, tested: Mapping[str, Any] | None = None,
                extensions: Mapping[str, Any] | None = None, label: str) -> Produced:
        invocation, environ = self.producer(run)
        claim = run_claim_from_environment(environ)
        tested_claim = dict(tested) if tested is not None else claim
        extensions_path = None
        if extensions:
            extensions_path = self.work / f"extensions-{label}.json"
            write_new_file(extensions_path, canonical_json(dict(extensions)))
        target = target_for_key(invocation, key, subject=self.subject)
        expectation = derive_expectation(invocation, target=target,
                                         tested_run=tested_run_projection(tested_claim, run["event"]),
                                         extensions=read_extensions(invocation, extensions_path))
        e2e = self._directory(f"e2e-{label}")
        self.hooks.fixture(invocation, {"target": target, "expectation": expectation, "out_root": str(e2e)})
        handoff_dir = self.work / f"handoff-{label}"
        result = prepare_handoff(invocation, e2e_root=e2e, key=key, output=handoff_dir, subject=self.subject,
                                 tested=tested_claim, handoff=claim, extensions_path=extensions_path, anchor="auto")
        manifest = evidence_validate.validate_handoff_dir(invocation, handoff_dir, key=key,
                                                          expected_subject_commit=self.head)
        self.check(manifest == result.manifest, f"{key}: the fresh-process handoff validation differs from prepare")
        created = self._offset(run)
        handoff = world.artifact(grammar.handoff_name(key, 1), run, created=created + 300,
                                 archive=zip_directory(handoff_dir))
        produced = Produced(key=key, handoff_dir=handoff_dir, handoff=handoff, manifest=manifest,
                            expectation=expectation)
        identity = anchor_identity(invocation, handoff_dir, key=key)
        self.check(identity.eligible == result.anchor_eligible, f"{key}: anchor identity disagrees with prepare")
        if result.anchor_eligible:
            self.check(identity.name == grammar.anchor_name(key, self.head, run["id"], 1),
                       f"{key}: the anchor name is not the handoff run's")
            anchor_dir = self.work / f"anchor-{label}"
            create_anchor(invocation, key=key, handoff_dir=handoff_dir, raw_artifact_id=handoff["id"],
                          raw_artifact_name=handoff["name"], raw_artifact_digest=handoff["digest"], output=anchor_dir)
            validate_anchor_dir(anchor_dir, key=key, expected_subject_commit=self.head, raw_artifact_id=handoff["id"],
                                raw_artifact_name=handoff["name"], raw_artifact_digest=handoff["digest"])
            produced.anchor = world.artifact(identity.name, run, created=created + 310,
                                             archive=zip_directory(anchor_dir))
        return produced

    @staticmethod
    def _offset(run: Mapping[str, Any]) -> float:
        """The simulation offset (seconds after ``BASE``) at which ``run`` was created."""

        return grammar.parse_timestamp(run["created_at"]).timestamp() - timestamp(0)

    def handoff_jobs(self, world: World, run: Mapping[str, Any], invocation: Invocation) -> list[dict[str, Any]]:
        created = self._offset(run)
        return [world.job(handoff_job_name(invocation, key), started=created + 250, completed=created + 350,
                          steps=((self.source["handoff_step"], created + 280, created + 320),)) for key in self.keys]

    def source_jobs(self, world: World, run: Mapping[str, Any], produced: Mapping[str, Produced]) -> None:
        """Seed the attempt jobs of a handoff run: its handoff jobs or, with
        ``source.require_job_graph``, exactly the adapter's expected graph of that run."""

        jobs = self.handoff_jobs(world, run, self.pages("collect"))
        if self.source["require_job_graph"]:
            jobs = self.graph_jobs(world, run, produced, jobs)
        world.jobs(dict(run), jobs)

    def graph_jobs(self, world: World, run: Mapping[str, Any], produced: Mapping[str, Produced],
                   named: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The jobs of ``run`` when the adapter asserts the exact job graph: every key's
        ``expected_source_jobs`` graph must be the same, and must hold every job the producer needs."""

        collect = self.pages("collect")
        graphs = []
        for item in produced.values():
            tested = runs.run_record(run, item.manifest["provenance"]["tested"])
            graph = self.hooks(collect, "expected_source_jobs", {"expectation": item.expectation, "tested_run": tested})
            if graph is None:
                raise _fail("source.require_job_graph is set but expected_source_jobs asserts no graph")
            graphs.append(sorted((entry["name"], entry["conclusion"]) for entry in graph))
        self.check(all(graph == graphs[0] for graph in graphs), "expected_source_jobs differs between keys of one run")
        by_name = {job["name"]: job for job in named}
        missing = sorted(set(by_name) - {name for name, _ in graphs[0]})
        if missing:
            raise _fail(f"the adapter's expected source job graph lacks the producer job {missing[0]!r}")
        created = self._offset(run)
        jobs = []
        for name, conclusion in graphs[0]:
            job = by_name.get(name) or world.job(name, started=created + 10, completed=created + 240,
                                                 conclusion=conclusion)
            if name in by_name and conclusion != "success":
                raise _fail(f"the adapter's expected graph gives the producer job {name!r} conclusion {conclusion!r}")
            jobs.append(job)
        return jobs

    def produce_keys(self, world: World, run: dict[str, Any], *, suffix: str) -> dict[str, Produced]:
        """Every key's evidence of the source run ``run`` at the protected head, with its jobs."""

        produced = {key: self.produce(world, run, key, label=f"{key}{suffix}") for key in self.keys}
        for item in produced.values():
            self.check(item.manifest["scope"]["kind"] == "complete",
                       f"{item.key}: evidence without extensions must be complete")
            if item.expectation["anchor"] is not None:
                self.anchored.add(item.key)
        self.source_jobs(world, run, produced)
        return produced

    def produce_generation(self) -> dict[str, Produced]:
        world = self.world
        assert world is not None
        run = self.source_run(world, SOURCE_RUN, created=0)
        produced = self.produce_keys(world, run, suffix="")
        if self.settings.families:
            for position, family in enumerate(self.config.families):
                self.family_handoffs.update(self.family_generation(family, FAMILY_RUN + position, self.keys,
                                                                   created=-400.0 + 10 * position))
        for item in produced.values():
            self.report.keys.append({"key": item.key, "lanes": len(item.manifest["lanes"]),
                                     "frames": len(item.manifest["frames"]),
                                     "comparisons": len(item.manifest["comparisons"]),
                                     "scope": item.manifest["scope"]["kind"], "anchor": item.anchor is not None})
        self.produced = produced
        return produced

    # -- families ------------------------------------------------------------------------------------------

    def family_native(self, family: Mapping[str, Any], family_run: Mapping[str, Any], key: str, *, outcome: str,
                      label: str) -> tuple[Path, Invocation, dict[str, Any]]:
        invocation, environ = self.producer(family_run, job=FAMILY_PRODUCER_JOB)
        claim = run_claim_from_environment(environ)
        record = runs.run_record(family_run, claim)
        target = target_for_key(invocation, key, subject=self.subject)
        expectation = derive_expectation(invocation, target=target,
                                         tested_run=tested_run_projection(claim, family_run["event"]), extensions={})
        native = self._directory(f"family-native-{label}")
        result = self.fixture_call(invocation, FAMILY_BUNDLE, family=family["id"], key=key, target=target,
                                   expectation=expectation, producer=record, out_root=str(native),
                                   image_factory=pattern_png, outcome=outcome)
        if result is not None:
            raise _fail(f"fixtures {FAMILY_BUNDLE} must return None")
        return native, invocation, claim

    def family_generation(self, family: Mapping[str, Any], run_id: int, keys: list[str], *,
                          created: float) -> dict[tuple[str, str], dict[str, Any]]:
        """A successful ``family``'s producer run ``run_id`` at the protected head hands off an
        ``available`` generation of every key of ``keys``; returns its family handoffs by leg."""

        world = self.world
        assert world is not None
        producer = family["producer"]
        family_run = world.run(run_id, path=producer["workflow"], event=producer["events"][0], created=created,
                               updated=created + 400)
        handoffs = {}
        for key in keys:
            handoffs[(family["id"], key)] = self.produce_family(world, family, family_run, key, created=created)
            self.family_producers[(family["id"], key)] = run_id
        world.jobs(family_run, [world.job(producer["job"], started=created + 360, completed=created + 395,
                                          steps=((producer["step"], created + 380, created + 392),))])
        return handoffs

    def produce_family(self, world: World, family: Mapping[str, Any], family_run: dict[str, Any], key: str, *,
                       created: float) -> dict[str, Any]:
        if self.hooks.optional_fixture(self.pages("family"), FAMILY_BUNDLE) is None:  # any job loads it
            raise _fail(f"--families needs the fixtures module's {FAMILY_BUNDLE} (family {family['id']})")
        label = f"{family['id']}-{key}-{family_run['id']}"
        native, invocation, claim = self.family_native(family, family_run, key, outcome="available", label=label)
        envelope_dir = self.work / f"family-handoff-{label}"
        create_envelope(invocation, family=family["id"], key=key, bundle_dir=native, coverage_sha=self.head,
                        subject={"branch": self.branch, "commit": self.head}, producer=claim, output=envelope_dir)
        evidence_validate.validate_bundle(invocation, "family", envelope_dir, key=key)
        self.report.checks += 1
        return world.artifact(grammar.family_handoff_name(family["id"], key, 1), family_run, created=created + 390,
                              archive=zip_directory(envelope_dir))

    # -- 5. collect ------------------------------------------------------------------------------------------

    def download(self, selected: Selected, output: Path, kind: str) -> Path:
        artifacts.download(self.api, artifact_id=selected.artifact_id, name=selected.name, digest=selected.digest,
                           size=selected.size, run_id=selected.run_id, output=output,
                           extraction=LIMITS_BY_KIND[kind])
        return output

    def collect_key(self, key: str, *, nomination: int | None, raw: Path, run_id: int = PAGES_RUN,
                    compose: bool = False, routes: tuple[str, ...] = ("handoff",)) -> tuple[Path, dict[str, Any]]:
        """One ``Publish / Collect <key>`` job: select (one of ``routes``), download, authenticate,
        compact (or compose), then validate in a fresh process and bind the derivatives to the raw
        handoff ``raw``."""

        with self.budget(f"collect {key}"):
            return self._collect_key(key, nomination=nomination, raw=raw, run_id=run_id, compose=compose,
                                     routes=routes)

    def _collect_key(self, key: str, *, nomination: int | None, raw: Path, run_id: int, compose: bool,
                     routes: tuple[str, ...]) -> tuple[Path, dict[str, Any]]:
        invocation = self.pages("collect", run_id)
        label = f"{key}-{run_id}"
        selected = select_evidence(invocation, api=self.api, key=key, nomination=nomination,
                                   expected_subject_commit=self.head)
        self.check(selected.kind in routes and (nomination is None or selected.artifact_id == nomination),
                   f"{key}: select chose the {selected.kind} {selected.name}, not "
                   f"{'the nominated handoff' if nomination is not None else ' or '.join(routes)}")
        selected_dir = self.download(selected, self.work / f"selected-{label}", selected.kind)
        draft = authenticate_selection(invocation, api=self.api, key=key, selected_dir=selected_dir, selected=selected)
        draft_path = self.work / f"draft-{label}.json"
        write_new_file(draft_path, canonical_json(draft))
        output = self.work / f"collected-{label}"
        if compose:
            compose_selected(invocation, api=self.api, key=key, selected_dir=selected_dir, selection_path=draft_path,
                             output=output)
        else:
            compact_bundle(invocation, key=key, input_dir=selected_dir, selection_path=draft_path, output=output)
        manifest = evidence_validate.validate_compact_dir(invocation, output, key=key,
                                                          expected_subject_commit=self.head)
        if selected.kind == "handoff":
            evidence_validate.validate_compact_dir(invocation, output, key=key, bind_raw=raw)
        else:
            # A cache is revalidated, never re-encoded: the derivatives stay the bytes the raw
            # handoff was bound to when the cache's own generation collected it.
            cached = read_json_file(selected_dir / "manifest.json", label="cache manifest",
                                    max_bytes=lim.MAX_MANIFEST_BYTES)[0]
            selection = read_json_file(output / "selection.json", label="selection",
                                       max_bytes=lim.MAX_SELECTION_BYTES)[0]
            self.check(selection["binding"]["mode"] == "cache-revalidated"
                       and _derivatives(manifest) == _derivatives(cached),
                       f"{key}: the collected cache does not keep the cached derivatives")
        self.check(contents.branch_head(self.api, self.branch) == (self.head, self.tree),
                   f"{key}: the source head moved during collection")
        return output, {"draft": draft, "manifest": manifest, "selected": selected}

    def collect_keys(self, generation: Generation, *, nominations: Mapping[str, int],
                     raws: Mapping[str, Path], routes: tuple[str, ...] = ("handoff",)) -> None:
        """Every ``Publish / Collect <key>`` job of ``generation``, each with its admission's
        nomination (``nominations[key]``, when there is one)."""

        for key in self.keys:
            nomination = nominations.get(key)
            output, collected = self.collect_key(key, nomination=nomination, raw=raws[key], run_id=generation.run_id,
                                                 routes=routes)
            selected: Selected = collected["selected"]
            generation.collected[key], generation.manifests[key] = output, collected["manifest"]
            generation.selected[key], generation.routes[key] = selected.artifact_id, selected.kind
            generation.raws[key] = raws[key]

    def collect_generation(self, nominations: Mapping[str, int]) -> None:
        for key in self.keys:
            again = select_evidence(self.pages("collect"), api=self.api, key=key, nomination=None,
                                    expected_subject_commit=self.head)
            self.check(again.artifact_id == self.produced[key].handoff["id"],
                       f"{key}: select without a nomination chose another artifact")
        self.collect_keys(self.first, nominations=nominations,
                          raws={key: item.handoff_dir for key, item in self.produced.items()})

    def collect_families(self, generation: Generation, nominations: Mapping[str, int]) -> None:
        for (family, key), handoff in sorted(self.family_handoffs.items()):
            with self.budget(f"family {family}/{key}"):
                selected, output = self.collect_family_leg(generation, family, key, nominations.get(f"{family}/{key}"))
            self.check(selected.artifact_id == handoff["id"], f"{family}/{key}: select chose another family generation")
            generation.families[(family, key)], generation.family_selected[(family, key)] = output, selected

    def collect_family_leg(self, generation: Generation, family: str, key: str, nomination: int | None, *,
                           carried_from: str | None = None) -> tuple[Selected, Path]:
        """One ``Publish / Collect <family> <key>`` job: select, download and ``family collect``,
        which must report ``available`` (carried forward from ``carried_from`` when given)."""

        invocation = self.pages("family", generation.run_id)
        selected = select_evidence(invocation, api=self.api, key=key, family=family, nomination=nomination,
                                   expected_subject_commit=self.head)
        label = f"{family}-{key}-{generation.run_id}"
        # select --family --output F writes the Selected object; family collect --selected-json F binds it.
        selected_json = self.work / f"family-selected-{label}.json"
        write_new_file(selected_json, canonical_json(selected.to_json()))
        source = self.download(selected, self.work / f"family-selected-{label}", selected.kind)
        output = self.work / f"collected-family-{label}"
        outcome = collect_family(invocation, family=family, key=key, input_dir=source, expected_coverage_sha=self.head,
                                 output=output, selected_json=selected_json)
        self.check(outcome.status == "available",
                   f"{family}/{key}: family collect reported {outcome.status} ({outcome.reason})"[:400])
        self.check(outcome.carried_from == carried_from,
                   f"{family}/{key}: family collect carried the generation from {outcome.carried_from}, "
                   f"not {carried_from}")
        # The collected artifact records the generation this job's select chose (build re-authenticates it).
        self.check((output / SELECTED_NAME).read_bytes() == selected_json.read_bytes(),
                   f"{family}/{key}: family collect recorded another selection than select wrote")
        return selected, output

    # -- 6. build ---------------------------------------------------------------------------------------------

    def pages_run(self, world: World, generation: Generation, *, status: str, conclusion: str | None,
                  updated: float) -> dict[str, Any]:
        return world.run(generation.run_id, path=PAGES_WORKFLOW_PATH, event="workflow_dispatch", created=generation.start,
                         updated=generation.at(updated), status=status, conclusion=conclusion, head_sha=generation.head,
                         title="Project site", pages=True)

    def pages_jobs(self, world: World, generation: Generation, stage: str) -> list[dict[str, Any]]:
        at = generation.at
        legs = [(family["id"], key) for family in self.config.families for key in self.keys]
        jobs = [world.job(caller_job_name("verify_kit"), started=at(0), completed=at(10)),
                world.job(api_job_name("publish", "admit"), started=at(10), completed=at(20))]
        jobs += [world.job(api_job_name("publish", "collect", key=key), started=at(20), completed=at(100),
                           steps=((step_name("select"), at(25), at(30)),)) for key in self.keys]
        jobs += [world.job(api_job_name("publish", "family", family=family, key=key), started=at(20), completed=at(100),
                           steps=((step_name("family_select"), at(25), at(30)),)) for family, key in legs]
        if not legs:
            # A mod without families: the family matrix job is skipped before its matrix expands and
            # the jobs API reports it once, under its unexpanded name (build must accept exactly this).
            jobs.append(world.job(unexpanded_api_job_name("publish", "family"), started=at(20), completed=at(20),
                                  conclusion="skipped"))
        if stage == "build":
            return jobs + [world.job(api_job_name("publish", "build"), started=at(100), completed=None)]
        jobs += [world.job(api_job_name("publish", "build"), started=at(100), completed=at(200)),
                 world.job(caller_job_name("deploy"), started=at(200), completed=at(300))]
        jobs += [world.job(api_job_name("finalize", "refresh", key=key), started=at(300), completed=at(400),
                           steps=((step_name("cache_upload"), at(350), at(360)),
                                  (step_name("baseline_upload"), at(360), at(370))))
                 for key in self.keys]
        jobs += [world.job(api_job_name("finalize", "refresh_family", family=family, key=key), started=at(300),
                           completed=at(400), steps=((step_name("family_cache_upload"), at(350), at(360)),))
                 for family, key in legs]
        if not legs:
            jobs.append(world.job(unexpanded_api_job_name("finalize", "refresh_family"), started=at(300),
                                  completed=at(300), conclusion="skipped"))
        return jobs

    def start_build(self, generation: Generation) -> None:
        """The Pages run of ``generation`` is in its build job; its collect jobs uploaded their bundles."""

        world = self.world
        assert world is not None
        pages = self.pages_run(world, generation, status="in_progress", conclusion=None, updated=100)
        world.jobs(pages, self.pages_jobs(world, generation, "build"))
        generation.collected_ids = {key: world.artifact(grammar.collected_name(key), pages, created=generation.at(50),
                                                        archive=zip_directory(path))["id"]
                                    for key, path in generation.collected.items()}
        for (family, key), path in generation.families.items():
            world.artifact(grammar.collected_family_name(family, key), pages, created=generation.at(60),
                           archive=zip_directory(path))

    def build(self, generation: Generation, *, full_site_checks: bool, started: bool = False) -> None:
        """The build job of ``generation`` (after :meth:`start_build`, unless ``started``) and the
        checks of its promotion and site."""

        if not started:
            self.start_build(generation)
        label = f"build-{generation.run_id}"
        invocation = self.pages("build", generation.run_id)
        site = self.work / f"_site-{generation.run_id}"
        with self.budget(f"build of generation {generation.number}"):
            result = build_site(invocation, api=self.api, kit_root=self.kit_root,
                                collected_dir=self.work / f"{label}-collected", families_dir=self.work / f"{label}-families",
                                output=site, promotion_dir=self.work / f"{label}-promotion")
        promotion = validate_promotion(result.promotion)
        self.check(result.heads == {self.branch: generation.head}, "build reports other heads than the simulated subject")
        self.check(sorted(bundle["key"] for bundle in promotion["bundles"]) == self.keys,
                   "the promotion does not publish exactly the simulated keys")
        self.check(all(bundle["selected_artifact_id"] == generation.selected[bundle["key"]]
                       and bundle["coverage_sha"] == generation.head for bundle in promotion["bundles"]),
                   "the promotion names another selected artifact or coverage")
        for entry in promotion["families"]:
            leg = (entry["family"], entry["key"])
            wanted = "available" if leg in generation.families else "unavailable"
            self.check(entry["status"] == wanted, f"family leg {'/'.join(leg)} is {entry['status']}, not {wanted}")
            if leg in generation.families:
                self.check(entry["selected_artifact_id"] == generation.family_selected[leg].artifact_id
                           and entry["coverage_sha"] == generation.head,
                           f"family leg {'/'.join(leg)} names another selected generation or coverage")
            if generation.number == 1:
                self.report.families.append({"family": leg[0], "key": leg[1], "status": entry["status"]})
        generation.promotion, generation.site = promotion, site
        self.site_checks(site, invocation, result, full=full_site_checks)

    def site_checks(self, site: Path, invocation: Invocation, result: Any, *, full: bool) -> None:
        if full:
            self.report.checks += _site.check_pages(site)
            scripts, node = _site.check_scripts(site)
            self.report.checks += scripts
            self.report.site.update(node_check=node)
        gallery, data_checks = _site.check_data(site)
        record_checks = _site.check_build_record(site, repository=self.repository,
                                                 implementation=result.promotion["implementation"],
                                                 kit=invocation.kit, site_sha256=result.site_sha256)
        self.report.checks += data_checks + record_checks
        self.check(sorted(release["key"] for release in gallery["releases"]) == self.keys,
                   "the gallery does not list exactly the simulated releases")
        frames = sum(item["frames"] for item in self.report.keys)
        self.check(len(gallery["frames"]) == frames, "the gallery does not publish every produced frame")
        if full:
            self.report.site.update(files=result.promotion["site"]["files"], frames=len(gallery["frames"]))

    # -- 7. refresh, rotation and the final admission -------------------------------------------------------

    def refresh(self, generation: Generation) -> dict[str, Any]:
        """Every ``Finalize`` refresh job of ``generation``, uploading its caches and baselines; the
        run then completes successfully. Returns the seeded ``promotion``/``pages``/``caches``/
        ``baselines`` artifacts."""

        world = self.world
        assert world is not None and generation.site is not None
        pages = self.pages_run(world, generation, status="in_progress", conclusion=None, updated=300)
        world.jobs(pages, self.pages_jobs(world, generation, "finalize"))
        promotion_root = self.work / f"build-{generation.run_id}-promotion"
        seeded = {"promotion": world.artifact(grammar.PROMOTION_NAME, pages, created=generation.at(150),
                                              archive=zip_directory(promotion_root))["id"],
                  "pages": world.artifact(grammar.PAGES_ARTIFACT_NAME, pages, created=generation.at(150),
                                          archive=zip_files({"artifact.tar": _tar(generation.site)}))["id"],
                  "caches": [], "baselines": []}
        for key in self.keys:
            output = self.work / f"refresh-{generation.run_id}-{key}"
            refreshed = self.refresh_job(generation, key, None, output, concurrent=bool(seeded["caches"]))
            name = grammar.cache_name(key, generation.head)
            self.check(refreshed.available and refreshed.cache_name == name, f"{key}: refresh names another cache than {name}")
            archive = zip_directory(output)
            seeded["caches"].append(world.artifact(refreshed.cache_name, pages, created=generation.at(355),
                                                   archive=archive)["id"])
            # A baseline is retained once per new complete generation: only for evidence selected as a
            # handoff, and never again under a name a successful earlier refresh already retained.
            wanted = None
            if self.config.baseline_archive["enabled"] and generation.routes[key] == "handoff":
                tested = generation.manifests[key]["provenance"]["tested"]["run_id"]
                name = grammar.baseline_name(key, generation.head, tested)
                wanted = None if name in self.retained_baselines else name
            self.check(refreshed.baseline_name == wanted,
                       f"{key}: refresh retains baseline {refreshed.baseline_name!r}, not {wanted!r}")
            if wanted is not None:
                seeded["baselines"].append(world.artifact(wanted, pages, created=generation.at(365), archive=archive))
                self.retained_baselines.add(wanted)
                self.latest_baselines[key] = seeded["baselines"][-1]
        for family in self.config.families:
            for key in self.keys:
                output = self.work / f"refresh-family-{generation.run_id}-{family['id']}-{key}"
                refreshed = self.refresh_job(generation, key, family["id"], output, concurrent=bool(seeded["caches"]))
                available = (family["id"], key) in generation.families
                self.check(refreshed.available == available,
                           f"{family['id']}/{key}: refresh-family reported available={refreshed.available}")
                if available:
                    name = grammar.family_cache_name(family["id"], key, generation.head)
                    self.check(refreshed.cache_name == name, f"{family['id']}/{key}: refresh names {refreshed.cache_name}")
                    seeded["caches"].append(world.artifact(name, pages, created=generation.at(356),
                                                           archive=zip_directory(output))["id"])
                else:
                    self.check(refreshed.cache_name is None and not os.path.lexists(output),
                               f"{family['id']}/{key}: an unavailable leg wrote a cache")
        self.pages_run(world, generation, status="completed", conclusion="success", updated=500)
        return seeded

    def refresh_job(self, generation: Generation, key: str, family: str | None, output: Path, *,
                    concurrent: bool) -> RefreshResult:
        """One ``Finalize`` refresh job of ``generation`` for ``key`` (and ``family``). With
        ``concurrent`` a sibling job's upload is still settling: the run's next artifact listing
        reports a ``total_count`` one row off its rows (the canary's ``total_count 6 disagrees with 5
        listed rows``), and the job must read it again exactly once, within its read budget."""

        label = f"refresh {key if family is None else f'{family}/{key}'} of generation {generation.number}"
        if concurrent:
            self.api.skew_listing(f"/repos/{self.repository}/actions/runs/{generation.run_id}/artifacts", responses=1)
        waits = len(self.api.sleeps)
        with self.budget(label):
            refreshed = refresh_bundle(self.pages("refresh" if family is None else "refresh-family", generation.run_id),
                                       api=self.api, key=key, family=family, input_dir=output)
        rereads = len(self.api.sleeps) - waits
        self.check(rereads == int(concurrent), f"{label} read its artifact listing again {rereads} times, "
                                               f"not {int(concurrent)}")
        self.report.site["listing_rereads"] = self.report.site.get("listing_rereads", 0) + rereads
        return refreshed

    def rotate(self, generation: Generation, *, now: float, dry_run: bool, label: str) -> tuple[set[int], bool]:
        """``rotate`` owned by ``generation``'s Pages run: its plan must be exactly the oracle's
        (:mod:`mod_base.conformance._rotation`), cut only by the deletion budget; a dry run deletes
        nothing and a real rotation deletes exactly its plan. Returns the planned ids and whether
        the deletion budget cut the plan."""

        world = self.world
        assert world is not None and generation.promotion is not None
        expected = expected_retirements(runs=world.runs, alive=world.alive(), records=world.records, config=self.config,
                                        owner_run_id=generation.run_id, promotion=generation.promotion,
                                        anchored=self.anchored, family_producers=self.family_producers,
                                        now=timestamp(now))
        before, mutations = self.api.deleted_artifact_ids, len(self.api.mutations)
        invocation = self.invocation({"GITHUB_REPOSITORY": self.repository,
                                      "GITHUB_RUN_ID": str(ROTATION_RUN + generation.number), "GITHUB_JOB": "rotate"},
                                     check_repository=False)
        summary = rotate_generation(invocation, api=self.api, owner_run_id=generation.run_id, owner_sha=generation.head,
                                    delete_delay_seconds=0.0, dry_run=dry_run, now=timestamp(now),
                                    sleep=lambda _seconds: None)
        planned_ids: list[int] = list(summary["planned_artifact_ids"])  # type: ignore[call-overload]
        planned = set(planned_ids)
        self.check(summary["dry_run"] is dry_run and len(planned) == len(planned_ids),
                   f"{label}: the rotation summary is not the requested run")
        unexpected = sorted(planned - expected)
        self.check(not unexpected, f"{label}: rotation plans to retire {unexpected[:3]}, which SPEC §5.5 keeps "
                                   f"(names {[world.records[i]['name'] for i in unexpected[:3]]})"[:500])
        exhausted = summary["remaining_rotation_deletions"] == 0 and len(expected) > len(planned)
        if exhausted:
            # A generation larger than the deletion budget defers its tail to its own retention.
            self.check(len(planned) == summary["rotation_deletion_limit"],
                       f"{label}: rotation stopped before spending its deletion budget")
        else:
            missing = sorted(expected - planned)
            self.check(not missing, f"{label}: rotation does not retire {missing[:3]} "
                                    f"(names {[world.records[i]['name'] for i in missing[:3]]})"[:500])
        deleted = self.api.deleted_artifact_ids[len(before):]
        if dry_run:
            self.check(not deleted and len(self.api.mutations) == mutations, f"{label}: a dry-run rotation deleted artifacts")
        else:
            self.check(deleted == planned_ids, f"{label}: the rotation deleted {sorted(set(deleted) ^ planned)[:3]} "
                                               "other than its plan")
        return planned, exhausted

    def rotate_first(self, seeded: Mapping[str, Any]) -> None:
        """Stage 7: ``rotate --dry-run`` of the first generation against a superseded generation
        seeded at the same head (every cache copied into an earlier successful Pages run) and a
        foreign artifact of the owner, then a final ``recovery`` that reports ``current``."""

        world = self.world
        assert world is not None
        owner = world.runs[PAGES_RUN]
        foreign = world.artifact("conformance-foreign-artifact", owner, created=1370, archive=zip_files({"x.txt": b"x"}))
        previous = world.run(PREVIOUS_PAGES_RUN, path=PAGES_WORKFLOW_PATH, event="schedule", created=600, updated=700,
                             title="Project site", pages=True)
        superseded = {world.artifact(world.records[artifact_id]["name"], previous, created=650,
                                     archive=world.archives[artifact_id])["id"] for artifact_id in seeded["caches"]}
        planned, _ = self.rotate(self.first, now=1600, dry_run=True, label="the first generation's dry-run rotation")
        kept = set(seeded["caches"]) | {baseline["id"] for baseline in seeded["baselines"]} | {foreign["id"]}
        kept |= {item.anchor["id"] for item in self.produced.values() if item.anchor is not None}
        self.check(not planned & kept, f"rotation plans to retire {sorted(planned & kept)[:3]}, which it must keep")
        self.check(bool(planned & superseded) or not superseded, "rotation retires none of the superseded generation")
        self.report.site["rotation_planned"] = len(planned)
        self.admit_current(now=1700, label="the first generation")

    # -- 8. variants -----------------------------------------------------------------------------------------

    def variant(self, name: str, action: Callable[[], str | None]) -> None:
        skipped = action()
        self.report.variants[name] = "passed" if skipped is None else f"skipped: {skipped}"

    def fixture_extensions(self, invocation: Invocation, name: str, handoff_run: Mapping[str, Any],
                           baselines: Callable[[str], dict[str, Any]] | None, **arguments: Any) -> dict[str, Any]:
        """The optional fixture function ``name``, called with a :class:`FixtureGitHub` for the handoff
        run ``handoff_run`` as ``ctx.api`` (serving ``retained_baseline`` through ``baselines`` for
        ``selected_extensions`` only): extension objects, or ``{"extensions": ..., "responses": [...]}``
        whose exact API bodies are seeded like ``ctx.api.add_response``."""

        world = self.world
        assert world is not None
        protected = frozenset({self.source["workflow"], *(family["producer"]["workflow"]
                                                          for family in self.config.families)})
        fixture_api = FixtureGitHub(world, handoff_run=handoff_run, protected_workflows=protected, baselines=baselines)
        value = self.fixture_call(invocation, name, fixture_api=fixture_api, **arguments)
        if isinstance(value, dict) and set(value) <= {"extensions", "responses"} and "extensions" in value:
            responses = value.get("responses", [])
            if not isinstance(responses, list) or len(responses) > MAX_FIXTURE_RESPONSES:
                raise _fail(f"fixtures {name} returned malformed responses")
            for response in responses:
                if not (isinstance(response, dict) and set(response) <= {"path", "params", "payload"}
                        and isinstance(response.get("path"), str)
                        and response["path"].startswith(f"/repos/{self.repository}/")):
                    raise _fail(f"fixtures {name} returned a response outside this repository")
                try:
                    fixture_api.add_response(response["path"], response.get("payload"), params=response.get("params"))
                except ValueError as exc:
                    raise _fail(f"fixtures {name} returned an invalid response: {single_line(exc, limit=300)}") from exc
            value = value["extensions"]
        if not isinstance(value, dict) or not value:
            raise _fail(f"fixtures {name} must return extension objects")
        return value

    def delegated(self) -> str | None:
        name = self.source["delegated_reuse_extension"]
        if name is None:
            return "config.source.delegated_reuse_extension is null"
        key = self.keys[0]
        probe = self.pages("collect")
        if self.hooks.optional_fixture(probe, DELEGATED_EXTENSIONS) is None:
            return f"the fixtures module defines no {DELEGATED_EXTENSIONS}"
        world = self.world
        assert world is not None
        tested_run = world.run(DELEGATED_TESTED_RUN, path=self.source["workflow"], event="pull_request", created=1800,
                               updated=1850, head_branch="conformance/reused-pull-request",
                               title=self.title(self.head))
        run = self.source_run(world, DELEGATED_RUN, created=1900)
        invocation, environ = self.producer(run)
        # The tested claim names the tested run itself: its run, attempt, branch and commit (the
        # reused pull request's branch and tested commit); the controller is the handoff's.
        tested = {**run_claim_from_environment(environ), "run_id": tested_run["id"],
                  "run_attempt": tested_run["run_attempt"], "branch": tested_run["head_branch"],
                  "commit": tested_run["head_sha"]}
        target = target_for_key(invocation, key, subject=self.subject)
        facts = {field: tested_run[field] for field in ("id", "run_attempt", "path", "event", "head_branch", "head_sha")}
        extensions = self.fixture_extensions(invocation, DELEGATED_EXTENSIONS, run, None, target=target,
                                             tested_run=facts)
        self.check(name in extensions, f"fixtures {DELEGATED_EXTENSIONS} returned no {name}")
        produced = self.produce(world, run, key, tested=tested, extensions=extensions, label=f"{key}-delegated")
        self.check(produced.manifest["provenance"]["reuse"] == "delegated", "the delegated handoff is not delegated")
        self.source_jobs(world, run, {key: produced})
        _, collected = self.collect_key(key, nomination=produced.handoff["id"], raw=produced.handoff_dir,
                                        run_id=VARIANT_PAGES_RUN)
        self.check(collected["draft"]["source"]["reuse"] == "delegated"
                   and name in collected["draft"]["extensions_verified"],
                   "authenticate did not verify the delegated reuse")
        return None

    def selected_handoff(self, key: str, run_id: int, created: float, baseline: artifacts.Artifact | Mapping[str, Any],
                         label: str, *, composed: Mapping[str, Any]) -> Produced:
        """A handoff of ``key`` at the protected head whose extensions (the fixtures module's
        ``selected_extensions``) make it ``selected`` evidence to be composed with ``baseline``;
        ``composed`` is the baseline the simulation retained, whose commit and tested run the stand-in
        baselines of :meth:`baseline_provider` share."""

        world = self.world
        assert world is not None
        run = self.source_run(world, run_id, created=created)
        invocation, _ = self.producer(run)
        target = target_for_key(invocation, key, subject=self.subject)
        reference = ({"id": baseline.id, "name": baseline.name, "digest": baseline.digest}
                     if isinstance(baseline, artifacts.Artifact)
                     else {"id": baseline["id"], "name": baseline["name"], "digest": baseline["digest"]})
        extensions = self.fixture_extensions(invocation, SELECTED_EXTENSIONS, run, self.baseline_provider(composed),
                                             target=target, baseline=reference)
        produced = self.produce(world, run, key, extensions=extensions, label=f"{key}-{label}")
        self.check(produced.manifest["scope"]["kind"] == "selected", "the selected extensions did not select a scope")
        self.source_jobs(world, run, {key: produced})
        return produced

    def baseline_provider(self, composed: Mapping[str, Any]) -> Callable[[str], dict[str, Any]]:
        """``ctx.api.retained_baseline`` of one ``selected_extensions`` call. ``composed`` is the newest
        ``mb-baseline`` the simulation retained for the composed key. A simulated key gets its own
        newest retained baseline (which must share ``composed``'s commit). Any other key the adapter
        declares (outside ``--keys``) gets a stand-in: an ``mb-baseline`` of that key at the same
        commit and tested run, uploaded inside the retention step of a successful ``pages.yml`` run
        whose build, deploy and refresh jobs succeeded, but whose archive is only a placeholder (no
        simulated generation holds that key's evidence; R3 composes a key only with its own
        baseline, never a stand-in). One such Pages run per call holds every stand-in it asks for."""

        world = self.world
        assert world is not None
        parsed = grammar.require_artifact_name(composed["name"], "baseline")
        base = grammar.parse_timestamp(composed["created_at"]).timestamp() - timestamp(0) - 365
        stand_ins: dict[str, dict[str, Any]] = {}
        owner: list[dict[str, Any]] = []

        def provide(key: str) -> dict[str, Any]:
            if key in self.keys:
                record = self.latest_baselines.get(key)
                if record is None or grammar.require_artifact_name(record["name"], "baseline").commit != parsed.commit:
                    raise ValueError(f"the simulation retained no baseline of {key} at {parsed.commit}")
                return dict(record)
            if key not in self.declared_keys:
                raise ValueError(f"the adapter declares no key {key!r}"[:200])
            if key not in stand_ins:
                if not owner:
                    run_id = next(self._stand_in_runs, None)
                    if run_id is None:
                        raise ValueError("the simulation has no Pages run left for stand-in baselines")
                    owner.append(world.run(run_id, path=PAGES_WORKFLOW_PATH, event="workflow_dispatch", created=base,
                                           updated=base + 500, head_sha=parsed.commit, title="Project site",
                                           pages=True))
                keys = [*stand_ins, key]
                jobs = [world.job(caller_job_name("verify_kit"), started=base, completed=base + 10),
                        world.job(api_job_name("publish", "admit"), started=base + 10, completed=base + 20)]
                jobs += [world.job(api_job_name("publish", "collect", key=item), started=base + 20, completed=base + 100)
                         for item in keys]
                jobs += [world.job(api_job_name("publish", "build"), started=base + 100, completed=base + 200),
                         world.job(caller_job_name("deploy"), started=base + 200, completed=base + 300)]
                jobs += [world.job(api_job_name("finalize", "refresh", key=item), started=base + 300, completed=base + 400,
                                   steps=((step_name("cache_upload"), base + 350, base + 360),
                                          (step_name("baseline_upload"), base + 360, base + 370)))
                         for item in keys]
                world.jobs(owner[0], jobs)
                stand_ins[key] = world.artifact(grammar.baseline_name(key, parsed.commit, parsed.run_id), owner[0],
                                                created=base + 365, archive=STAND_IN_ARCHIVE)
            return dict(stand_ins[key])

        return provide

    def latest_offset(self) -> float:
        """The simulation offset of the newest run or artifact event so far."""

        world = self.world
        assert world is not None
        moments = [run[field] for run in world.runs.values() for field in ("created_at", "updated_at") if run.get(field)]
        moments += [record["created_at"] for record in world.records.values()]
        return max(grammar.parse_timestamp(moment).timestamp() for moment in moments) - timestamp(0)

    def selected(self) -> str | None:
        """The last variant: selected evidence one commit after the newest published baseline,
        composed with it; then R3 (or the adapter's own ``compose``) refuses forged baselines."""

        key = self.keys[0]
        probe = self.pages("collect")
        if self.hooks.optional_fixture(probe, SELECTED_EXTENSIONS) is None:
            return f"the fixtures module defines no {SELECTED_EXTENSIONS}"
        if not self.config.baseline_archive["enabled"]:
            return "config.baseline_archive is disabled, so no baseline can complete selected evidence"
        world = self.world
        assert world is not None
        newest = self.latest_baselines.get(key)
        self.check(newest is not None and grammar.require_artifact_name(newest["name"], "baseline").commit == self.head,
                   f"{key}: the published generations retained no baseline of the protected head")
        assert newest is not None
        baselines = artifacts.list_named(self.api, newest["name"])
        self.check(len(baselines) == 1 and baselines[0].id == newest["id"],
                   f"{key}: the protected head's baseline is not retained exactly once")
        baseline = baselines[0]
        # A selection re-tests what changed since its baseline, and a mod may recompute it as the Git
        # diff between the two commits (Quick Skin does): so the selected generation lies on a new
        # protected head, one commit (SELECTED_CHANGE) after the baseline's.
        self.commit_on_head(_selected_change(self.hooks, probe), SELECTED_TEXT, label="the selected head")
        start = self.latest_offset() + SELECTED_DELAY
        produced = self.selected_handoff(key, SELECTED_RUN, start, baseline, "selected", composed=newest)
        try:
            self.collect_key_refused(key, produced, self.work / "selected-compact", VARIANT_PAGES_RUN + 2)
        except MbError:
            self.report.checks += 1
        else:
            raise _fail("compact accepted a selected handoff without composition (R3)")
        _, collected = self.collect_key(key, nomination=produced.handoff["id"], raw=produced.handoff_dir,
                                        run_id=VARIANT_PAGES_RUN + 1, compose=True)
        manifest = collected["manifest"]
        self.check(manifest["scope"]["kind"] == "composed", "compose did not write composed evidence")
        self.check(manifest["scope"]["components"]["baseline"]["artifact_id"] == baseline.id,
                   "compose did not complete the selection with the published baseline")
        epochs = {frame.get("epoch") for frame in manifest["frames"]}
        self.check("selected" in epochs, "the composed bundle holds no selected frame")
        self.report.site["composed_lanes"] = _composed_lanes(manifest)
        # The same bytes under the baseline's name are refused when a successful source run of the
        # baseline's commit uploaded them, or when a successful Pages run of that commit uploaded them
        # outside its refresh job's retention step: each forgery differs from the genuine baseline in
        # one property of its owner only (its workflow, its upload window). R3 authenticates the owner
        # itself; the adapter's own compose hook may refuse first (Quick Skin's authenticates the
        # owner its coverage certificate names).
        archive = world.archives[baseline.id]
        forged_run = world.run(FORGED_SOURCE_RUN, path=self.source["workflow"],
                               event=self.source["events"]["canonical"][0], created=start + 5, updated=start + 405,
                               head_sha=baseline.head_sha, title=self.title(baseline.head_sha))
        window_run = world.run(FORGED_PAGES_RUN, path=PAGES_WORKFLOW_PATH, event="workflow_dispatch", created=start + 40,
                               updated=start + 150, head_sha=baseline.head_sha, title="Project site", pages=True)
        world.jobs(window_run, [world.job(api_job_name("publish", "build"), started=start + 42, completed=start + 45),
                                world.job(caller_job_name("deploy"), started=start + 45, completed=start + 48),
                                world.job(api_job_name("finalize", "refresh", key=key), started=start + 50,
                                          completed=start + 140,
                                          steps=((step_name("baseline_upload"), start + 100, start + 110),))])
        forgeries = (("a source run's upload", FORGED_OWNER_RUN, start + 10,
                      world.artifact(baseline.name, forged_run, created=start + 300, archive=archive)),
                     ("an upload outside the retention step", FORGED_WINDOW_RUN, start + 30,
                      world.artifact(baseline.name, window_run, created=start + 120, archive=archive)))
        for position, (what, run_id, created, forged) in enumerate(forgeries):
            forged_handoff = self.selected_handoff(key, run_id, created, forged, f"forged-baseline-{position}",
                                                   composed=newest)
            refused = len(self.hooks.refusals)
            try:
                self.collect_key(key, nomination=forged_handoff.handoff["id"], raw=forged_handoff.handoff_dir,
                                 run_id=VARIANT_PAGES_RUN + 4 + position, compose=True)
            except HookFailed as exc:
                hooks = self.hooks.refusals[refused:]
                if hooks != ["compose"]:
                    raise _fail(f"the adapter's {', '.join(hooks) or 'unknown'} hook, not compose or R3, refused "
                                f"{what}: {single_line(exc, limit=200)}") from exc
                self.report.checks += 1
            except MbError:
                self.report.checks += 1
            else:
                raise _fail(f"compose accepted {what} as the baseline (R3)")
        return None

    def collect_key_refused(self, key: str, produced: Produced, output: Path, run_id: int) -> None:
        invocation = self.pages("collect", run_id)
        selected = select_evidence(invocation, api=self.api, key=key, nomination=produced.handoff["id"],
                                   expected_subject_commit=self.head)
        selected_dir = self.download(selected, self.work / f"selected-refused-{run_id}", "handoff")
        draft = authenticate_selection(invocation, api=self.api, key=key, selected_dir=selected_dir, selected=selected)
        draft_path = self.work / f"draft-refused-{run_id}.json"
        write_new_file(draft_path, canonical_json(draft))
        compact_bundle(invocation, key=key, input_dir=selected_dir, selection_path=draft_path, output=output)

    def attested(self) -> str | None:
        name = self.source["attestation_job"]
        if name is None:
            return "config.source.attestation_job is null"
        key = self.keys[0]
        world = self.world
        assert world is not None
        tested_run = self.source_run(world, ATTESTED_TESTED_RUN, created=2200, event="workflow_dispatch")
        run = self.source_run(world, ATTESTED_RUN, created=2300, event="workflow_dispatch")
        _, tested_environ = self.producer(tested_run)
        tested = run_claim_from_environment(tested_environ)
        produced = self.produce(world, run, key, tested=tested, label=f"{key}-attested")
        self.check(produced.manifest["provenance"]["reuse"] == "attested", "the attested handoff is not attested")
        self.check(produced.anchor is None, "an attested handoff was cut into an anchor")
        attestation = world.job(name, started=2310, completed=2340)
        world.jobs(run, self.handoff_jobs(world, run, self.pages("collect")) + [attestation])
        if self.source["require_job_graph"]:
            world.jobs(tested_run, self.graph_jobs(world, tested_run, {key: produced}, []))
        else:
            world.jobs(tested_run, [])
        _, collected = self.collect_key(key, nomination=produced.handoff["id"], raw=produced.handoff_dir,
                                        run_id=VARIANT_PAGES_RUN + 3)
        source = collected["draft"]["source"]
        self.check(source["reuse"] == "attested" and source["attestation_job"]["name"] == name,
                   "authenticate did not bind the attestation job")
        return None

    def family_outcomes(self) -> str | None:
        if not self.settings.families or not self.config.families:
            return "no family is configured or --families was not given"
        probe = self.pages("family")
        if self.hooks.optional_fixture(probe, FAMILY_BUNDLE) is None:
            return f"the fixtures module defines no {FAMILY_BUNDLE}"
        declared = _declared_outcomes(self.hooks, probe)
        wanted = [outcome for outcome in ("superseded", "unavailable") if outcome in declared]
        if not wanted:
            return f"the fixtures module lists no superseded or unavailable outcome in {FAMILY_OUTCOMES}"
        key = self.keys[0]
        for family in self.config.families:
            run = runs.get_run(self.api, self.family_handoffs[(family["id"], key)]["workflow_run"]["id"])
            for outcome in wanted:
                label = f"{family['id']}-{key}-{outcome}"
                native, invocation, claim = self.family_native(family, run, key, outcome=outcome, label=label)
                envelope_dir = self.work / f"family-handoff-{label}"
                create_envelope(invocation, family=family["id"], key=key, bundle_dir=native, coverage_sha=self.head,
                                subject={"branch": self.branch, "commit": self.head}, producer=claim,
                                output=envelope_dir)
                output = self.work / f"collected-family-{label}"
                # As select would record it: the family handoff of that producer attempt (not uploaded).
                archive = zip_directory(envelope_dir)
                selected = Selected(kind="family-handoff", artifact_id=OUTCOME_ARTIFACT, digest=sha256_digest(archive),
                                    name=grammar.family_handoff_name(family["id"], key, claim["run_attempt"]),
                                    size=len(archive), run_id=claim["run_id"], run_attempt=claim["run_attempt"])
                selected_json = self.work / f"family-selected-{label}.json"
                write_new_file(selected_json, canonical_json(selected.to_json()))
                result = collect_family(probe, family=family["id"], key=key, input_dir=envelope_dir,
                                        expected_coverage_sha=self.head, output=output, selected_json=selected_json)
                self.check(result.status == outcome and result.projection is None and not os.path.lexists(output),
                           f"{family['id']}/{key}: the {outcome} bundle collected as {result.status}")
        return None

    def newest_run(self) -> str | None:
        """``require_newest_run``: a newer source run that did not succeed makes the subject's older
        evidence inadmissible, whether nominated or searched (never a fallback)."""

        if not self.source["require_newest_run"]:
            return "config.source.require_newest_run is false"
        world = self.world
        assert world is not None
        self.source_run(world, NEWEST_RUN, created=2500, conclusion="failure")
        key = self.keys[0]
        for nomination in (None, self.produced[key].handoff["id"]):
            try:
                select_evidence(self.pages("collect"), api=self.api, key=key, nomination=nomination,
                                expected_subject_commit=self.head)
            except Unavailable as exc:
                self.check(exc.reason == "newest-run", f"{key}: a failed newest run refused with {exc.reason!r}")
            else:
                raise _fail(f"{key}: evidence of an older run was selected while the newest run failed")
        return None

    # -- the whole run -----------------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        previous = host.call
        host.call = self.hooks  # type: ignore[assignment]
        try:
            self.discover()
            self.world = self.new_world()
            self.hooks.api = self.world.api
            self.admission_before_evidence()
            produced = self.produce_generation()
            family_wake = None
            if self.family_handoffs:
                (family, key), handoff = sorted(self.family_handoffs.items())[0]
                family_wake = WakeInputs(run_id=handoff["workflow_run"]["id"], sha=self.head, family=family,
                                         bundle_key=key, artifact_id=handoff["id"], artifact_digest=handoff["digest"],
                                         coverage_sha=self.head)
            nominations = self.admission_after_evidence(produced, family_wake)
            self.collect_generation(nominations)
            self.collect_families(self.first, nominations)
            self.build(self.first, full_site_checks=True)
            seeded = self.refresh(self.first)
            self.rotate_first(seeded)
            self.variant("attested", self.attested)
            self.variant("delegated", self.delegated)
            self.variant("family-outcomes", self.family_outcomes)
            self.variant("newest-run", self.newest_run)
            self.later_generations()
            self.variant("carried", self.carried)
            # Last: it moves the protected head to a descendant of the newest baseline's commit.
            self.variant("selected", self.selected)
        finally:
            host.call = previous  # type: ignore[assignment]
        hooks = sorted({hook for hook, _network in self.hooks.calls})
        return {"repository": self.repository, "kit": {"repository": mod_base.KIT_REPOSITORY, "sha": KIT_SHA,
                                                        "version": mod_base.__version__},
                "keys": self.report.keys, "families": self.report.families, "checks": self.report.checks,
                "variants": dict(sorted(self.report.variants.items())), "admission": self.report.admission,
                "hooks": hooks, "site": self.report.site}


def _selected_change(hooks: InProcessHooks, invocation: Invocation) -> str:
    """The new file the ``selected`` head adds: the fixtures module's ``SELECTED_CHANGE``, a
    repository path absent at the protected head (for example a path a mod's selection owns), or
    :data:`SELECTED_DOCUMENT`."""

    change = getattr(hooks.module(invocation, "fixtures_path"), SELECTED_CHANGE, SELECTED_DOCUMENT)
    if not grammar.is_repo_path(change) or change.split("/", 1)[0].lower() == ".github":
        raise _fail(f"the fixtures module's {SELECTED_CHANGE} must be a repository path outside .github")
    return change


def _declared_outcomes(hooks: InProcessHooks, invocation: Invocation) -> tuple[str, ...]:
    """The family outcomes the fixtures module declares it can produce (``FAMILY_OUTCOMES``)."""

    declared = getattr(hooks.module(invocation, "fixtures_path"), FAMILY_OUTCOMES, ("available",))
    if (not isinstance(declared, (tuple, list)) or not declared
            or any(outcome not in FAMILY_OUTCOME_VALUES for outcome in declared)):
        raise _fail(f"the fixtures module's {FAMILY_OUTCOMES} must list outcomes of {FAMILY_OUTCOME_VALUES}")
    return tuple(declared)


def _composed_lanes(manifest: Mapping[str, Any]) -> dict[str, int]:
    """How many lanes of a composed bundle hold only baseline, only selected, or both epochs' frames
    (``mixed``: a partially re-captured lane, whose record then carries ``baseline_run``)."""

    epochs: dict[str, set[str]] = {lane["lane_id"]: set() for lane in manifest["lanes"]}
    for frame in manifest["frames"]:
        epochs[frame["lane_id"]].add(frame["epoch"])
    counts = {"baseline": 0, "mixed": 0, "selected": 0}
    for held in epochs.values():
        if held:
            counts["mixed" if len(held) == 2 else next(iter(held))] += 1
    return counts


def _derivatives(manifest: Mapping[str, Any]) -> list[tuple[str, str]]:
    return sorted((frame["frame_id"], frame["derivative"]["sha256"]) for frame in manifest["frames"])


def _tar(site: Path) -> bytes:
    """The ``artifact.tar`` a ``github-pages`` upload holds (deterministic; rotation only names it)."""

    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(site.rglob("*")):
            if path.is_file():
                info = tarfile.TarInfo(path.relative_to(site).as_posix())
                data = path.read_bytes()
                info.size = len(data)
                info.mtime = int(timestamp(0))
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
    return stream.getvalue()


def simulate(settings: Settings) -> dict[str, Any]:
    """Run the whole simulation (see the module docstring) and return its report."""

    return Simulation(settings).run()
