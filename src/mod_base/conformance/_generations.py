"""The later generations of the conformance simulation (MB10, SPEC §9.1 step 7, §3.5, §4.4 R5, §5.5).

:class:`Generations` is mixed into :class:`mod_base.conformance._simulation.Simulation` and continues
its world after the first generation, its dry-run rotation and the variants (stage 9):

a. **The first generation's rotation, for real.** The fake deletes exactly the plan, which must be
   the oracle's (:mod:`mod_base.conformance._rotation`); unless a variant handed off newer evidence
   at the first head, ``recovery`` then still reports ``current`` (a rotation never retires the
   caches a publication is current by).
b. **A push.** A documentation-only commit (:data:`CONFORMANCE_DOCUMENT`) moves the protected head;
   a family's carry-forward policy (its adapter's impact decision) must treat that path as
   documentation.
c. **The second generation** at the new head: a new source run hands off every key and its
   ``deploy`` wake nominates exactly those handoffs. Every family leg is selected without a
   nomination. With ``carry_forward`` the first-parent walk finds the first generation at the
   parent commit (its family cache once the rotation retired the family handoff) and ``family
   collect`` carries it forward (R5); without it ``select`` finds no generation at the new head
   (``Unavailable``) and the leg is published as unavailable. Build (re-authenticating each leg's
   recorded selection, ``selected.json``, by id with no walk, and re-proving R5), refresh (a carried
   family cache is named by the new head and keeps its producer's envelope), then ``recovery`` is
   ``current``.
d. **The anchor successor grace.** ``rotate --dry-run`` of the second generation once the first
   anchor has a successor and, when ``anchor.successor_grace_days`` is positive, again once the
   grace has passed: the older anchor is planned only then. The superseded generation lies at the
   parent commit, so both plans also prove the older-commit generation probe.
e. **A same-head publication interleaved with the previous rotation** (with ``--families``; a mod
   without a family never publishes a current head again, so a ``manual`` admission there must be
   ``current``). A family's producer hands off a new generation of the first key at the same head
   and wakes the site (``family``). That publication collects every key without a nomination (the
   second generation's cache, or its handoff), the woken leg by its nomination and every other leg
   without one (the second generation's family cache, carried again). The second generation's
   rotation then runs for real before this publication builds: its build, refresh and the
   ``current`` admission must still succeed, and ``rotate --dry-run`` of this generation retires
   the second one at the same head.

The ``carried`` variant (``--families`` and a family with ``carry_forward``) pushes a third head:
its generation's family legs walk the first-parent history back to the second head. There the woken
leg finds its own second-head generation (the family cache the same-head publication refreshed
from the family handoff, which supersedes it; carried from the second head) and every other leg the
family cache that publication refreshed under the second head, whose envelope still covers the
first head (carried from the first head). R5 again, build re-authenticates each leg's recorded
selection (``selected.json``) by id with no walk, refresh, ``current`` and a dry-run rotation.

Every publication job keeps the 160-read budget and changes nothing (``Simulation.budget``) at the
mod's configured scale: all its keys and family legs, carried legs included.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mod_base.conformance._snapshot import commit_file, git
from mod_base.conformance._world import timestamp
from mod_base.errors import MbError, Unavailable
from mod_base.model import grammar
from mod_base.pages.admission import WakeInputs
from mod_base.pages.select import Selected, select_evidence

#: The documentation-only file each later head adds (``{number}`` is the head's number).
CONFORMANCE_DOCUMENT = "docs/mod-base-conformance/head-{number}.md"
#: Run ids of the later generations.
SECOND_SOURCE_RUN = 5242
SECOND_FAMILY_RUN = 5300
THIRD_SOURCE_RUN = 6242
SECOND_PAGES_RUN = 9200
SAME_HEAD_PAGES_RUN = 9300
THIRD_PAGES_RUN = 9400
#: Simulation offsets: the first generation's real rotation, then the second head's source run.
FIRST_ROTATION_AT = 2700
SECOND_HEAD_AT = 3000
DAY_SECONDS = 86400


def _fail(message: str) -> MbError:
    return MbError(message, reason="conformance")


@dataclass
class Generation:
    """One simulated Pages publication (``pages.yml`` run ``run_id`` created at offset ``start``
    at the protected head ``head``) and what its jobs collected and published."""

    number: int
    run_id: int
    start: float
    head: str
    tree: str
    #: Per key: the selected artifact's id and kind (``handoff``/``cache``), the raw handoff its
    #: derivatives are bound to, the collected directory and its manifest.
    selected: dict[str, int] = field(default_factory=dict)
    routes: dict[str, str] = field(default_factory=dict)
    raws: dict[str, Path] = field(default_factory=dict)
    collected: dict[str, Path] = field(default_factory=dict)
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Per available family leg: the collected directory and the selected family artifact.
    families: dict[tuple[str, str], Path] = field(default_factory=dict)
    family_selected: dict[tuple[str, str], Selected] = field(default_factory=dict)
    collected_ids: dict[str, int] = field(default_factory=dict)
    promotion: dict[str, Any] | None = None
    site: Path | None = None

    def at(self, offset: float) -> float:
        return self.start + offset


@dataclass(frozen=True)
class LegSource:
    """The generation a family leg must select: of ``commit``, owned by one of ``owners`` (a family
    cache's Pages run or a family handoff's producer run), carried forward from ``carried_from``
    (``None``: it covers the expected commit itself)."""

    commit: str
    owners: frozenset[int]
    carried_from: str | None


class Generations:
    """Stage 9 and the ``carried`` variant (module docstring). Every name used here that is not
    defined here is :class:`mod_base.conformance._simulation.Simulation`'s."""

    # Provided by Simulation (declared for readers; assigned there).
    settings: Any
    config: Any
    repo: Path
    branch: str
    head: str
    tree: str
    subject: dict[str, str]
    keys: list[str]
    world: Any
    first: Generation
    produced: dict[str, Any]
    family_producers: dict[tuple[str, str], int]
    report: Any
    same_head: Generation | None = None

    # -- helpers --------------------------------------------------------------------------------------

    def push(self, number: int) -> None:
        """Move the protected head by one documentation-only commit on top of it."""

        previous = self.head
        relative = CONFORMANCE_DOCUMENT.format(number=number)
        text = (f"# mod-base conformance head {number}\n\nA documentation-only commit of a synthetic "
                "conformance run.\n")
        head, tree = commit_file(self.repo, relative, text.encode("utf-8"), branch=self.branch)
        parent = git(self.repo, "rev-parse", f"{head}^").decode("ascii").strip()
        self.check(parent == previous, f"head {number} is not a child of the previous head")
        self.head, self.tree = head, tree
        self.subject = {"branch": self.branch, "commit": head, "tree": tree}
        self.world.advance(head, tree)

    def generation(self, number: int, run_id: int, start: float) -> Generation:
        return Generation(number=number, run_id=run_id, start=start, head=self.head, tree=self.tree)

    def produce_head(self, run_id: int, *, created: float, number: int, pages_run: int) -> tuple[dict[str, Any], Any]:
        """A source run at the protected head hands off every key; its ``deploy`` wake is admitted
        by the Pages run ``pages_run``."""

        run = self.source_run(self.world, run_id, created=created)
        produced = self.produce_keys(self.world, run, suffix=f"-head-{number}")
        deploy = self.admit_deploy(run_id, produced, now=created + 900, pages_run=pages_run)
        return produced, deploy

    def legs(self) -> list[tuple[str, str]]:
        return [(family["id"], key) for family in self.config.families for key in self.keys]

    def carried_legs(self, source: LegSource) -> dict[tuple[str, str], LegSource | None]:
        """Every leg of a later generation without its own family generation: ``source`` for a
        family with ``carry_forward``, no generation at all (``None``) otherwise."""

        return {(family["id"], key): source if family["carry_forward"] else None
                for family in self.config.families for key in self.keys}

    def collect_later_families(self, generation: Generation, nominations: Mapping[str, int],
                               sources: Mapping[tuple[str, str], LegSource | None]) -> dict[str, int]:
        """Every family leg of a later generation (``--families``): it must select exactly its
        ``sources`` entry (``family collect`` available, carried as the entry says), or, for
        ``None``, find no generation at all (``Unavailable``). Returns the count of legs collected
        as ``carried``, collected ``fresh`` and ``unavailable``."""

        counts = {"carried": 0, "fresh": 0, "unavailable": 0}
        if not self.settings.families:
            counts["unavailable"] = len(self.legs())
            return counts
        for leg in self.legs():
            family, key = leg
            source = sources[leg]
            label = f"family {family}/{key} of generation {generation.number}"
            nomination = nominations.get(f"{family}/{key}")
            with self.budget(label):
                if source is None:
                    try:
                        select_evidence(self.pages("family", generation.run_id), api=self.api, key=key,
                                        family=family, nomination=nomination, expected_subject_commit=self.head)
                    except Unavailable:
                        counts["unavailable"] += 1
                        continue
                    raise _fail(f"{label}: select found a generation although {family} never ran at this head "
                                "and does not carry forward")
                selected, output = self.collect_family_leg(generation, family, key, nomination,
                                                           carried_from=source.carried_from)
            parsed = grammar.parse_artifact_name(selected.name)
            owner = self.world.runs.get(selected.run_id, {})
            covers = (parsed is not None and parsed.coverage_sha == source.commit if selected.kind == "family-cache"
                      else owner.get("head_sha") == source.commit)
            self.check(covers and selected.run_id in source.owners,
                       f"{label}: select chose {selected.kind} {selected.name} of run {selected.run_id}, not the "
                       f"generation of {source.commit}")
            generation.families[leg], generation.family_selected[leg] = output, selected
            counts["carried" if source.carried_from is not None else "fresh"] += 1
        return counts

    def publish_later(self, generation: Generation, *, started: bool = False) -> None:
        """Build (its collect jobs' uploads already seeded when ``started``), refresh and the
        ``current`` admission of a later generation."""

        self.build(generation, full_site_checks=False, started=started)
        self.refresh(generation)
        self.admit_current(now=generation.at(600), label=f"generation {generation.number}")

    def record(self, generation: Generation, head: int, families: Mapping[str, int], planned: set[int]) -> None:
        routes: dict[str, int] = {}
        for route in generation.routes.values():
            routes[route] = routes.get(route, 0) + 1
        self.report.site.setdefault("generations", []).append({
            "generation": generation.number, "head": head, "pages_run": generation.run_id, "key_routes": routes,
            "family_legs": {name: count for name, count in families.items() if count},
            "rotation_planned": len(planned)})

    @staticmethod
    def _offset_of(artifact: Mapping[str, Any]) -> float:
        return grammar.parse_timestamp(artifact["created_at"]).timestamp() - timestamp(0)

    # -- stage 9 --------------------------------------------------------------------------------------

    def later_generations(self) -> None:
        first = self.first
        self.rotate(first, now=FIRST_ROTATION_AT, dry_run=False, label="the first generation's rotation")
        newer = [run for run in self.world.runs.values()
                 if run["path"] == self.config.source["workflow"] and run["head_sha"] == first.head
                 and run["conclusion"] == "success" and self._offset_of(run) > first.start]
        if not newer:  # a variant's newer handoff at the first head leaves it not current, rightly
            self.admit_current(now=FIRST_ROTATION_AT + 50, label="the first generation's rotation")
        first_anchors = {item.anchor["id"] for item in self.produced.values() if item.anchor is not None}
        first_producers = frozenset(self.family_producers.values())

        # c. the second generation, one documentation-only commit later.
        self.push(2)
        produced, deploy = self.produce_head(SECOND_SOURCE_RUN, created=SECOND_HEAD_AT, number=2,
                                             pages_run=SECOND_PAGES_RUN)
        second = self.generation(2, SECOND_PAGES_RUN, SECOND_HEAD_AT + 1000)
        raws = {key: item.handoff_dir for key, item in produced.items()}
        self.collect_keys(second, nominations=deploy.nominations, raws=raws)
        second_families = self.collect_later_families(second, deploy.nominations, self.carried_legs(
            LegSource(commit=first.head, owners=frozenset({first.run_id}) | first_producers, carried_from=first.head)))
        self.publish_later(second)

        # d. the anchor successor grace, across the parent commit's superseded generation.
        grace = self.config.anchor["successor_grace_days"] * DAY_SECONDS
        successors = [self._offset_of(item.anchor) for item in produced.values() if item.anchor is not None]
        settled = max([second.at(600), *successors]) + 60
        planned, exhausted = self.rotate(second, now=max(settled, second.at(700)), dry_run=True,
                                         label="the second generation's dry-run rotation")
        if grace and successors:
            self.check(not planned & first_anchors, "rotation retires an anchor before its successor's grace passed")
            settled += grace
            planned, exhausted = self.rotate(second, now=settled, dry_run=True,
                                             label="the second generation's dry-run rotation after the grace")
        if successors and not exhausted:
            self.check(first_anchors <= planned, "rotation keeps an anchor whose successor's grace has passed")

        # e. a same-head publication, interleaved with the second generation's real rotation.
        start = max(second.at(1000), settled + 100)
        if not (self.settings.families and self.config.families):
            manual = self.admit("manual", now=start, run_id=SAME_HEAD_PAGES_RUN)
            self.check(not manual.eligible and manual.reason == "current",
                       f"a manual publication of a current head admitted {manual.reason!r}")
            self.record(second, 2, second_families, planned)
            return
        family = self.config.families[0]
        woken_leg = (family["id"], self.keys[0])
        handoff = self.family_generation(family, SECOND_FAMILY_RUN, [self.keys[0]], created=start)[woken_leg]
        policy = self.config.admission
        woken_at = self._offset_of(handoff) + max(policy.get("coalesce_seconds", 0),
                                                  policy.get("partial_deadline_seconds", 0)) + 60
        wake = WakeInputs(run_id=SECOND_FAMILY_RUN, sha=self.head, family=family["id"], bundle_key=self.keys[0],
                          artifact_id=handoff["id"], artifact_digest=handoff["digest"], coverage_sha=self.head)
        woken = self.admit("family", now=woken_at, wake=wake, run_id=SAME_HEAD_PAGES_RUN)
        self.check(woken.eligible and woken.nominations.get("/".join(woken_leg)) == handoff["id"],
                   f"the same-head family wake admitted {woken.reason!r} without nominating its family handoff")
        same = self.generation(3, SAME_HEAD_PAGES_RUN, woken_at + 50)
        self.collect_keys(same, nominations=woken.nominations, raws=raws, routes=("cache", "handoff"))
        sources = self.carried_legs(LegSource(commit=self.head, owners=frozenset({second.run_id}),
                                              carried_from=first.head))
        sources[woken_leg] = LegSource(commit=self.head, owners=frozenset({SECOND_FAMILY_RUN}), carried_from=None)
        same_families = self.collect_later_families(same, woken.nominations, sources)
        # Its collect jobs have uploaded their bundles; the previous generation's rotation runs now.
        self.start_build(same)
        second_planned, _ = self.rotate(second, now=same.at(110), dry_run=False,
                                        label="the second generation's rotation, during a same-head publication")
        self.publish_later(same, started=True)
        same_planned, _ = self.rotate(same, now=same.at(700), dry_run=True,
                                      label="the same-head generation's dry-run rotation")
        self.record(second, 2, second_families, second_planned)
        self.record(same, 2, same_families, same_planned)
        self.same_head = same

    # -- the carried variant ------------------------------------------------------------------------------

    def carried(self) -> str | None:
        if not self.settings.families:
            return "--families was not given"
        if not any(family["carry_forward"] for family in self.config.families):
            return "no configured family has carry_forward"
        same = self.same_head
        assert same is not None
        second_head, first_head = self.head, self.first.head
        woken_leg = (self.config.families[0]["id"], self.keys[0])
        self.push(3)
        start = same.at(1000)
        produced, deploy = self.produce_head(THIRD_SOURCE_RUN, created=start, number=3, pages_run=THIRD_PAGES_RUN)
        third = self.generation(4, THIRD_PAGES_RUN, start + 1000)
        self.collect_keys(third, nominations=deploy.nominations,
                          raws={key: item.handoff_dir for key, item in produced.items()})
        sources = self.carried_legs(LegSource(commit=second_head, owners=frozenset({same.run_id}),
                                              carried_from=first_head))
        if self.config.families[0]["carry_forward"]:
            # The second head's own generation of the woken leg: its family handoff, or the family
            # cache the same-head publication refreshed from it (which supersedes the handoff).
            sources[woken_leg] = LegSource(commit=second_head, owners=frozenset({SECOND_FAMILY_RUN, same.run_id}),
                                           carried_from=second_head)
        families = self.collect_later_families(third, deploy.nominations, sources)
        self.publish_later(third)
        planned, _ = self.rotate(third, now=third.at(700), dry_run=True, label="the third head's dry-run rotation")
        self.record(third, 3, families, planned)
        return None
