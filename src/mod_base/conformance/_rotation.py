"""The rotation oracle of the conformance simulation (MB10): SPEC §5.5 restated over the world.

:func:`expected_retirements` derives from the simulated world alone (every seeded run and artifact
and the fake's deletions so far) what a rotation owned by the Pages run ``owner`` must retire at
``now``. The simulation requires the kit's plan (``rotate --dry-run`` and a real rotation alike) to
be exactly that set or, when the deletion budget ran out, a subset of it of exactly the budget's
size. The retirement table of SPEC §5.5 is restated here independently of
:mod:`mod_base.pages.rotate`, for the owner's promoted keys and available family legs:

* ``mb-cache``/``mb-family-cache``: owned by an earlier completed successful ``pages.yml`` run of
  the default branch, never the owner's own;
* ``mb-handoff`` (attempt 1, or the consumed attempt): the handoff the owner consumed, or one of an
  earlier successful source run of the same branch than the run of the artifact the owner selected
  (the source run of a handoff, the Pages owner of a cache);
* ``mb-family-handoff``: likewise, bounded by the family artifact the owner selected, or by the
  generation's producer run when that artifact is gone;
* ``mb-anchor`` of an anchored key: older than the newest authenticated anchor created at or before
  ``now - anchor.successor_grace_days`` (named from at most ``limits.ANCHOR_PROBES`` source runs of
  the history created at or before that boundary);
* the owner's ``mb-collected*``, ``github-pages`` and ``mb-promotion``.

Every other candidate is unexpired and created before the owner. A baseline, a non-``mb-`` name,
the owner's caches and anything created after the owner (its transients aside) are never retired.
The simulated generations stay within the rotation's discovery bounds (``limits.GENERATION_PROBES``
heads), so every retirable artifact is also discoverable.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from mod_base.config import Config
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.workflow import PAGES_EVENTS, PAGES_WORKFLOW_PATH

#: The owner's own artifacts a rotation retires with its generation.
TRANSIENT_KINDS = frozenset({"collected", "collected-family", "pages", "promotion"})


def _created(record: Mapping[str, Any]) -> datetime:
    return grammar.parse_timestamp(record["created_at"])


def _order(record: Mapping[str, Any]) -> tuple[datetime, int]:
    return _created(record), record["id"]


def _run_id(artifact: Mapping[str, Any]) -> int:
    return artifact["workflow_run"]["id"]


class _Oracle:
    def __init__(self, *, runs: Mapping[int, Mapping[str, Any]], alive: Mapping[int, Mapping[str, Any]],
                 records: Mapping[int, Mapping[str, Any]], config: Config, owner_run_id: int) -> None:
        self.runs = runs
        self.alive = alive
        self.records = records
        self.config = config
        self.owner_run_id = owner_run_id
        self.owner_created = _created(runs[owner_run_id])
        self.canonical = config.canonical_branch
        events = config.source["events"]
        self.source_events = frozenset(events["canonical"]) | frozenset(events["other"])

    def named(self, kind: str, **fields: Any) -> list[tuple[Mapping[str, Any], grammar.ArtifactName]]:
        found = []
        for record in self.alive.values():
            parsed = grammar.parse_artifact_name(record["name"])
            if parsed is not None and parsed.kind == kind and all(getattr(parsed, name) == value
                                                                  for name, value in fields.items()):
                found.append((record, parsed))
        return found

    def older(self, artifact: Mapping[str, Any]) -> bool:
        return (artifact["expired"] is False and _run_id(artifact) != self.owner_run_id
                and _created(artifact) < self.owner_created)

    def superseding(self, run: Mapping[str, Any]) -> bool:
        return (run["status"] == "completed" and run["conclusion"] == "success"
                and _created(run) < self.owner_created)

    def owner(self, artifact: Mapping[str, Any], *, path: str, events: Collection[str],
              branch: str) -> Mapping[str, Any] | None:
        """The run owning ``artifact`` when it is a superseding ``path`` run at the artifact's head."""

        run = self.runs.get(_run_id(artifact))
        if (run is None or run["path"] != path or run["event"] not in events or run["head_branch"] != branch
                or artifact["workflow_run"]["head_branch"] != branch
                or run["head_sha"] != artifact["workflow_run"]["head_sha"] or not self.superseding(run)):
            return None
        return run

    def caches(self, keys: Collection[str], legs: Collection[tuple[str, str]]) -> set[int]:
        found = set()
        for kind in ("cache", "family-cache"):
            for record, parsed in self.named(kind):
                scoped = parsed.key in keys if kind == "cache" else (parsed.family, parsed.key) in legs
                if (scoped and self.older(record)
                        and self.owner(record, path=PAGES_WORKFLOW_PATH, events=PAGES_EVENTS,
                                       branch=self.canonical) is not None):
                    found.add(record["id"])
        return found

    def handoffs(self, key: str, selected_id: int) -> set[int]:
        selected = self.records[selected_id]
        chosen = grammar.parse_artifact_name(selected["name"])
        assert chosen is not None
        consumed_kind = chosen.kind == "handoff"
        reference = _order(self.runs[_run_id(selected)])
        attempts = {1, chosen.attempt} if consumed_kind else {1}
        found = set()
        for record, parsed in self.named("handoff", key=key):
            if parsed.attempt not in attempts or not self.older(record):
                continue
            consumed = consumed_kind and record["id"] == selected_id
            owner = self.owner(record, path=self.config.source["workflow"], events=self.source_events,
                               branch=self.canonical)
            if owner is not None and (consumed or _order(owner) < reference):
                found.add(record["id"])
        return found

    def family_handoffs(self, family: str, key: str, selected_id: int, producer_run_id: int) -> set[int]:
        producer = self.config.family(family)["producer"]
        selected = self.alive.get(selected_id)
        attempts = {1}
        bound = producer_run_id
        if selected is not None:
            bound = _run_id(selected)
            parsed = grammar.parse_artifact_name(selected["name"])
            if parsed is not None and parsed.kind == "family-handoff" and parsed.attempt is not None:
                attempts.add(parsed.attempt)
        reference = _order(self.runs[bound])
        found = set()
        for record, parsed in self.named("family-handoff", family=family, key=key):
            if parsed.attempt not in attempts or not self.older(record):
                continue
            owner = self.owner(record, path=producer["workflow"], events=producer["events"], branch=self.canonical)
            if owner is not None and (record["id"] == selected_id or _order(owner) < reference):
                found.add(record["id"])
        return found

    def anchors(self, key: str, now: datetime) -> set[int]:
        boundary = now - timedelta(days=self.config.anchor["successor_grace_days"])
        template = self.config.source["display_title"]
        canonical_events = frozenset(self.config.source["events"]["canonical"])
        history = sorted((run for run in self.runs.values()
                          if run["path"] == self.config.source["workflow"] and run["head_branch"] == self.canonical
                          and run["event"] in self.source_events and self.superseding(run)),
                         key=_order, reverse=True)
        anchors: list[Mapping[str, Any]] = []
        probes = 0
        for run in history:
            if probes == lim.ANCHOR_PROBES:
                break
            if _created(run) > boundary:
                continue
            title = None if template is None else template.format(subject_commit=run["head_sha"])
            if run["event"] not in canonical_events or (title is not None and run["display_title"] != title):
                continue
            probes += 1
            name = grammar.anchor_name(key, run["head_sha"], run["id"], run["run_attempt"])
            anchors += [record for record in self.alive.values()
                        if record["name"] == name and self.older(record) and _run_id(record) == run["id"]
                        and record["workflow_run"]["head_sha"] == run["head_sha"]
                        and record["workflow_run"]["head_branch"] == self.canonical]
        settled = [record for record in anchors if _created(record) <= boundary]
        if not settled:
            return set()
        bound = max(_order(record) for record in settled)
        return {record["id"] for record in anchors if _order(record) < bound}

    def transients(self, keys: Collection[str], legs: Collection[tuple[str, str]]) -> set[int]:
        found = set()
        for record in self.alive.values():
            parsed = grammar.parse_artifact_name(record["name"])
            if parsed is None or parsed.kind not in TRANSIENT_KINDS or _run_id(record) != self.owner_run_id:
                continue
            if ((parsed.kind == "collected" and parsed.key not in keys)
                    or (parsed.kind == "collected-family" and (parsed.family, parsed.key) not in legs)):
                continue
            found.add(record["id"])
        return found


def expected_retirements(*, runs: Mapping[int, Mapping[str, Any]], alive: Mapping[int, Mapping[str, Any]],
                         records: Mapping[int, Mapping[str, Any]], config: Config, owner_run_id: int,
                         promotion: Mapping[str, Any], anchored: Collection[str],
                         family_producers: Mapping[tuple[str, str], int], now: float) -> set[int]:
    """Every artifact id a rotation owned by ``owner_run_id`` (whose final promotion is
    ``promotion``) must retire at ``now`` (see the module docstring). ``runs`` and ``records`` hold
    every seeded run and artifact, ``alive`` the artifacts the fake still holds; ``anchored`` are
    the keys whose expectation declares an anchor and ``family_producers`` the producer run of
    every available leg's generation."""

    oracle = _Oracle(runs=runs, alive=alive, records=records, config=config, owner_run_id=owner_run_id)
    bundles = {bundle["key"]: bundle for bundle in promotion["bundles"]}
    legs = {(entry["family"], entry["key"]): entry for entry in promotion["families"] if entry["available"]}
    expected = oracle.caches(bundles, legs) | oracle.transients(bundles, legs)
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    for key, bundle in bundles.items():
        expected |= oracle.handoffs(key, bundle["selected_artifact_id"])
        if key in anchored:
            expected |= oracle.anchors(key, moment)
    for (family, key), entry in legs.items():
        expected |= oracle.family_handoffs(family, key, entry["selected_artifact_id"], family_producers[(family, key)])
    return expected
