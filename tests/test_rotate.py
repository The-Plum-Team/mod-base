"""Rotation (MB7): Quick Skin ``test_pages_artifact_rotation`` cases and Block Pops
``test_pages_rotation_{actions,inputs}`` pinned-read cases, rewritten against the v1 ``mb-`` names
and an in-memory :class:`mod_base.github.fake.FakeGitHub`.

The default world is one Quick Skin-like generation: the owner Pages run ``R`` (500) published key
``mc1.20.1`` and its ``mod-compatibility`` leg at ``COMMIT``; the previous generation (Pages run
400) published both at ``OLD``; E2E runs 90 (``OLD``), 101 (``COMMIT``, consumed by R) and 120
(``NEWER``, created after R) produced handoffs and anchors; family producer runs 190 and 202
produced family handoffs.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from mod_base import cli, runtime
from mod_base.errors import MbError, Unavailable
from mod_base.github import api as github_api
from mod_base.github.api import ApiError, ApiNotFound
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256, strict_loads
from mod_base.model.documents import compact_identity_sha256
from mod_base.pages import rotate as rotation
from tests import helpers

REPOSITORY = helpers.REPOSITORY
KEY = helpers.KEY
SECOND_KEY = "mc1.21.1"
FAMILY = helpers.FAMILY_ID
SCOPE = f"{FAMILY}--{KEY}"
COMMIT = helpers.COMMIT
OLD = helpers.h("previous-commit", 40)
OLDER = helpers.h("older-commit", 40)
OLDEST = helpers.h("oldest-commit", 40)
NEWER = helpers.h("newer-commit", 40)
KIT_SHA = helpers.KIT_SHA
OWNER = helpers.PAGES_RUN_ID
PREVIOUS = 400
ROTATION_RUN = 600
E2E = helpers.E2E_WORKFLOW
FAMILY_WORKFLOW = helpers.FAMILY_WORKFLOW
PAGES = ".github/workflows/pages.yml"
WORKFLOW_ID = 77
SELECTED = helpers.HANDOFF_ARTIFACT_ID
FAMILY_SELECTED = 9101
OWNER_CREATED = "2026-09-25T10:30:00Z"
NOW = datetime(2026, 9, 25, 11, 0, tzinfo=timezone.utc).timestamp()

#: What the default world retires, per summary section, in deletion order.
KEY_DELETIONS = [6001, 5090, 8801, SELECTED]
FAMILY_DELETIONS = [6002, 9100, FAMILY_SELECTED]
TRANSIENT_DELETIONS = [7001, 7002, 7300, 7100]
#: The global deletion order, longest-lived first: the key's cache, its family leg's cache and its
#: anchor (90 days), the family handoffs (7 days), the handoffs, then R's transients (1 day).
ALL_DELETIONS = [6001, 6002, 5090, 9100, FAMILY_SELECTED, 8801, SELECTED, *TRANSIENT_DELETIONS]
#: Never retired by the default world: replacements, baselines, a non-kit name, the previous
#: generation's own transient, artifacts newer than R and the newest anchor.
RETAINED = [7200, 7400, 7500, 7600, 6003, 6004, 8802, 5101, 5120]
#: A Quick Skin-sized generation: seventeen keys, each with a family leg.
QS_KEYS = (KEY, *(f"mc1.21.{minor}" for minor in range(16)))
#: Offset of the artifact ids of a generation published after R (``World.publish_later``).
LATER = 1_000_000


def long_lived_order(keys: tuple[str, ...]) -> list[int]:
    """The 90-day artifacts a world of ``keys`` (each with a family leg) supersedes, in retirement
    order: one key at a time, its cache then its family cache, the first key's anchor after them."""

    order = [6001, 6002, 5090]
    for key in sorted(keys[1:]):
        base = 10000 * keys.index(key)
        order += [6001 + base, 6002 + base]
    return order


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_record(path: str, data: bytes) -> dict[str, Any]:
    return {"path": path, "sha256": sha(data), "size": len(data)}


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        for name in sorted(files):
            package.writestr(name, files[name])
    return buffer.getvalue()


def rekey(value: Any, key: str, commit: str = COMMIT) -> Any:
    return json.loads(json.dumps(value).replace(KEY, key).replace(COMMIT, commit))


def compact_files(key: str = KEY, *, selected_id: int = SELECTED, anchored: bool = True, collector: int = OWNER,
                  commit: str = COMMIT,
                  adjust: Callable[[dict[str, Any], dict[str, Any]], None] | None = None) -> dict[str, bytes]:
    """A structurally valid compact bundle (``helpers.compact`` with real file bytes) for ``key`` at
    ``commit``, collected by Pages run ``collector``; ``adjust(manifest, selection)`` may change
    both documents before they are hashed."""

    expectation = rekey(helpers.expectation(), key, commit)
    if not anchored:
        expectation["anchor"] = None
    manifest = rekey(helpers.compact(), key, commit)
    selection = rekey(helpers.embedded_selection(), key, commit)
    manifest["source_artifact"]["id"] = selected_id
    selection["selected_artifact"]["id"] = selected_id
    selection["implementation"]["run_id"] = collector
    if adjust is not None:
        adjust(manifest, selection)
    files = {"expectation.json": canonical_json(expectation)}
    manifest["expectation"] = file_record("expectation.json", files["expectation.json"])
    for frame in manifest["frames"]:
        data = f"derivative {frame['frame_id']}".encode()
        derivative = frame["derivative"]
        derivative.update(path=f"images/{sha(data)}.webp", sha256=sha(data), size=len(data))
        derivative["pixel"]["file_sha256"] = sha(data)
        files[derivative["path"]] = data
    manifest.pop("selection")
    manifest["files"] = sorted((file_record(path, data) for path, data in files.items()),
                               key=lambda record: record["path"])
    selection["expectation_sha256"] = canonical_sha256(expectation)
    selection["manifest_sha256"] = compact_identity_sha256({**manifest, "selection": {}})
    files["selection.json"] = canonical_json(selection)
    manifest["selection"] = file_record("selection.json", files["selection.json"])
    manifest["files"] = sorted(manifest["files"] + [manifest["selection"]], key=lambda record: record["path"])
    files["manifest.json"] = canonical_json(manifest)
    return files


def family_files(key: str = KEY, *, commit: str = COMMIT, **envelope_fields: Any) -> dict[str, bytes]:
    """A structurally valid family cache: ``envelope.json`` produced at ``commit`` (with
    ``envelope_fields`` overriding) beside a small native bundle."""

    native = {"manifest.json": b'{"kind":"quick-skin-public-mod-compatibility","schema_version":6}\n',
              "images/pair.webp": b"native pair image"}
    envelope = {**rekey(helpers.family_envelope(), key, commit), **envelope_fields}
    envelope["native"]["manifest_sha256"] = sha(native["manifest.json"])
    envelope["files"] = sorted((file_record(path, data) for path, data in native.items()),
                               key=lambda record: record["path"])
    return {**native, "envelope.json": canonical_json(envelope)}


def run(run_id: int, *, path: str, sha: str, created: str, event: str = "workflow_dispatch",
        conclusion: str | None = "success", status: str = "completed", attempt: int = 1, branch: str = "master",
        workflow_id: int = 1, title: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": run_id, "run_attempt": attempt, "workflow_id": workflow_id, "path": path, "head_branch": branch,
            "head_sha": sha, "event": event, "display_title": title or "run", "created_at": created,
            "status": status, "conclusion": conclusion, "head_repository": {"full_name": REPOSITORY}, **extra}


def pages_run(run_id: int, sha: str, created: str, **overrides: Any) -> dict[str, Any]:
    return {**run(run_id, path=PAGES, sha=sha, created=created, workflow_id=WORKFLOW_ID, title="Project site"),
            **overrides}


def owner_run(**overrides: Any) -> dict[str, Any]:
    referenced = [{"path": f"The-Plum-Team/mod-base/.github/workflows/publish.yml@{KIT_SHA}", "ref": "refs/tags/v0.9.0",
                   "sha": KIT_SHA}]
    return pages_run(OWNER, COMMIT, OWNER_CREATED, **{"referenced_workflows": referenced, **overrides})


def remove_families(config: dict[str, Any]) -> None:
    config["families"] = []
    config["copy"]["family_notes"] = {}


def make_unavailable(promotion: dict[str, Any]) -> None:
    """The promotion of a family leg the owner found superseded (no collected fields)."""

    entry = promotion["families"][0]
    for name in ("collected_artifact_id", "collected_digest", "coverage_sha", "selected_artifact_id"):
        entry.pop(name)
    entry.update(available=False, status="superseded")


class World:
    """The default generation (see the module docstring), adjustable before :meth:`rotate`."""

    def __init__(self, test: unittest.TestCase, *, keys: tuple[str, ...] = (KEY,), family: bool = True,
                 family_keys: tuple[str, ...] = (KEY,), grace: int = 0, display_title: str | None = None,
                 writable: bool = True) -> None:
        self.api = FakeGitHub(repository=REPOSITORY, writable=writable)
        temporary = tempfile.TemporaryDirectory(prefix="rotate test ")
        test.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        config = helpers.load_fixture("config/qs.json")
        config["anchor"]["successor_grace_days"] = grace
        config["source"]["display_title"] = display_title
        (self.repo / "site").mkdir()
        self.write_config(config)
        if not family:
            self.configure(remove_families)
        self.seeded: dict[int, tuple[dict[str, Any], bytes]] = {}
        self.sleeps: list[float] = []
        self.keys = keys
        self.family = family
        self._seed_runs()
        bundles = [self._seed_key(position, key) for position, key in enumerate(keys)]
        legs = [self._seed_leg(keys.index(key), key) for key in family_keys] if family else []
        self._seed_owner(bundles, legs)

    # -- seeding ---------------------------------------------------------------------------------

    def write_config(self, config: dict[str, Any]) -> None:
        self.config = config
        (self.repo / "site" / "mod-base.json").write_bytes(canonical_json(config))

    def configure(self, change: Callable[[dict[str, Any]], None]) -> None:
        """Change the config the next rotation reads (the rotation run's head, not R's)."""

        config = copy.deepcopy(self.config)
        change(config)
        self.write_config(config)

    def replace_cache(self, files: dict[str, bytes]) -> None:
        """Replace R's key cache (7200) by ``files`` and record their manifest in the promotion."""

        record, _ = self.seeded[7200]
        self.api.add_artifact(record, zipped(files))
        self.replace_promotion(lambda promotion: promotion["bundles"][0].update(
            manifest_sha256=sha(files["manifest.json"])))

    def replace_family_cache(self, files: dict[str, bytes], position: int = 0) -> None:
        """Replace the bytes of R's family cache of the leg seeded at ``position`` by ``files``."""

        record, _ = self.seeded[7400 + 10000 * position]
        self.api.add_artifact(record, zipped(files))

    def add(self, artifact_id: int, name: str, created: str, *, run_id: int, head_sha: str, branch: str = "master",
            files: dict[str, bytes] | None = None, expired: bool = False, **explicit: Any) -> dict[str, Any]:
        record = {"id": artifact_id, "name": name, "created_at": created, "expired": expired,
                  "workflow_run": {"id": run_id, "head_branch": branch, "head_sha": head_sha}, **explicit}
        archive = zipped(files) if files is not None else f"artifact {artifact_id}".encode()
        self.api.add_artifact(record, archive)
        self.seeded[artifact_id] = (record, archive)
        return record

    def change(self, artifact_id: int, **fields: Any) -> None:
        """Re-seed a seeded artifact with changed API metadata (its bytes unchanged)."""

        record, archive = self.seeded[artifact_id]
        self.api.add_artifact({**record, **fields}, archive)

    def _seed_runs(self) -> None:
        self.api.add_run(owner_run())
        self.api.add_run(pages_run(PREVIOUS, OLD, "2026-09-24T10:30:00Z"))
        for run_id, commit, created in ((90, OLD, "2026-09-24T09:00:00Z"), (101, COMMIT, "2026-09-25T09:00:00Z"),
                                        (120, NEWER, "2026-09-25T10:40:00Z")):
            self.api.add_run(run(run_id, path=E2E, sha=commit, created=created))
        for run_id, commit, created in ((190, OLD, "2026-09-24T09:40:00Z"), (202, COMMIT, "2026-09-25T09:40:00Z")):
            self.api.add_run(run(run_id, path=FAMILY_WORKFLOW, sha=commit, created=created,
                                 event="repository_dispatch"))

    def _seed_key(self, position: int, key: str) -> dict[str, Any]:
        base = 10000 * position
        selected = SELECTED + base
        anchored = position == 0
        files = compact_files(key, selected_id=selected, anchored=anchored)
        self.add(7200 + base, grammar.cache_name(key, COMMIT), "2026-09-25T10:55:00Z", run_id=OWNER, head_sha=COMMIT,
                 files=files)
        collected = self.add(7001 + base, grammar.collected_name(key), "2026-09-25T10:40:00Z", run_id=OWNER,
                             head_sha=COMMIT)
        self.add(6001 + base, grammar.cache_name(key, OLD), "2026-09-24T10:55:00Z", run_id=PREVIOUS, head_sha=OLD,
                 files=compact_files(key))
        handoff = grammar.handoff_name(key, 1)
        self.add(selected, handoff, helpers.CREATED_AT, run_id=101, head_sha=COMMIT,
                 digest=f"sha256:{helpers.h('handoff-zip')}", size_in_bytes=4_567_890)
        self.add(8801 + base, handoff, "2026-09-24T09:50:00Z", run_id=90, head_sha=OLD)
        self.add(8802 + base, handoff, "2026-09-25T10:45:00Z", run_id=120, head_sha=NEWER)
        if anchored:
            for artifact_id, commit, run_id, created in ((5090, OLD, 90, "2026-09-24T09:30:00Z"),
                                                         (5101, COMMIT, 101, "2026-09-25T09:30:00Z"),
                                                         (5120, NEWER, 120, "2026-09-25T10:46:00Z")):
                self.add(artifact_id, grammar.anchor_name(key, commit, run_id, 1), created, run_id=run_id,
                         head_sha=commit)
        return {"key": key, "collected_artifact_id": collected["id"],
                "collected_digest": "sha256:" + sha(self.seeded[collected["id"]][1]),
                "manifest_sha256": sha(files["manifest.json"]), "coverage_sha": COMMIT,
                "selected_artifact_id": selected}

    def _seed_leg(self, position: int, key: str) -> dict[str, Any]:
        """The ``FAMILY`` leg of ``key``: R's family cache (7400) and collected artifact (7002), the
        previous generation's family cache (6002) and the family handoffs of producer runs 202 (the
        one R consumed) and 190, each id offset by ``10000 * position``."""

        base = 10000 * position
        files = family_files(key)
        self.add(7400 + base, grammar.family_cache_name(FAMILY, key, COMMIT), "2026-09-25T10:56:00Z", run_id=OWNER,
                 head_sha=COMMIT, files=files)
        collected = self.add(7002 + base, grammar.collected_family_name(FAMILY, key), "2026-09-25T10:41:00Z",
                             run_id=OWNER, head_sha=COMMIT)
        self.add(6002 + base, grammar.family_cache_name(FAMILY, key, OLD), "2026-09-24T10:56:00Z", run_id=PREVIOUS,
                 head_sha=OLD, files=files)
        name = grammar.family_handoff_name(FAMILY, key, 1)
        self.add(FAMILY_SELECTED + base, name, "2026-09-25T09:45:00Z", run_id=202, head_sha=COMMIT)
        self.add(9100 + base, name, "2026-09-24T09:45:00Z", run_id=190, head_sha=OLD)
        return {"family": FAMILY, "key": key, "available": True, "status": "available",
                "collected_artifact_id": collected["id"],
                "collected_digest": "sha256:" + sha(self.seeded[collected["id"]][1]),
                "coverage_sha": COMMIT, "selected_artifact_id": FAMILY_SELECTED + base}

    def _seed_owner(self, bundles: list[dict[str, Any]], legs: list[dict[str, Any]]) -> None:
        promotion = helpers.promotion()
        promotion["bundles"] = bundles
        promotion["families"] = legs
        self.promotion = promotion
        self.add(7100, grammar.PROMOTION_NAME, "2026-09-25T10:50:00Z", run_id=OWNER, head_sha=COMMIT,
                 files={"promotion.json": canonical_json(promotion)})
        self.add(7300, grammar.PAGES_ARTIFACT_NAME, "2026-09-25T10:50:00Z", run_id=OWNER, head_sha=COMMIT)
        self.add(7500, grammar.baseline_name(KEY, COMMIT, 101), "2026-09-25T10:57:00Z", run_id=OWNER, head_sha=COMMIT)
        self.add(7600, f"pages-cache-{KEY}--{COMMIT}", "2026-09-25T10:57:00Z", run_id=OWNER, head_sha=COMMIT)
        self.add(6003, grammar.baseline_name(KEY, OLD, 90), "2026-09-24T10:57:00Z", run_id=PREVIOUS, head_sha=OLD)
        self.add(6004, grammar.collected_name(KEY), "2026-09-24T10:40:00Z", run_id=PREVIOUS, head_sha=OLD)

    def replace_promotion(self, change: Callable[[dict[str, Any]], None]) -> None:
        promotion = copy.deepcopy(self.promotion)
        change(promotion)
        record, _ = self.seeded[7100]
        self.api.add_artifact({key: value for key, value in record.items() if key not in {"size_in_bytes", "digest"}},
                              zipped({"promotion.json": canonical_json(promotion)}))
        self.promotion = promotion

    # -- running ---------------------------------------------------------------------------------

    def invocation(self, **environ: str) -> runtime.Invocation:
        values = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(ROTATION_RUN), **environ}
        return runtime.build_invocation(self.repo, None, values, check_repository=False)

    def publish_later(self, run_id: int, created: str, *, legs: tuple[str, ...]) -> None:
        """Seed a later successful Pages generation ``run_id`` at R's head (a later publication of
        the same head, such as a compatibility milestone) whose run starts at ``created`` (an hour
        of ``2026-09-25``, ``HH:00``). It promotes every key, consuming the handoffs R consumed, and
        the ``FAMILY`` legs of ``legs`` only; its artifact ids are R's plus ``LATER``."""

        stamp = created[:14]
        self.api.add_run(owner_run(id=run_id, created_at=created))
        bundles, families = [], []
        for position, key in enumerate(self.keys):
            base = LATER + 10000 * position
            files = compact_files(key, selected_id=SELECTED + base - LATER, anchored=position == 0, collector=run_id)
            self.add(7200 + base, grammar.cache_name(key, COMMIT), f"{stamp}25:00Z", run_id=run_id, head_sha=COMMIT,
                     files=files)
            collected = self.add(7001 + base, grammar.collected_name(key), f"{stamp}10:00Z", run_id=run_id,
                                 head_sha=COMMIT)
            bundles.append({"key": key, "collected_artifact_id": collected["id"],
                            "collected_digest": "sha256:" + sha(self.seeded[collected["id"]][1]),
                            "manifest_sha256": sha(files["manifest.json"]), "coverage_sha": COMMIT,
                            "selected_artifact_id": SELECTED + base - LATER})
        for key in legs:
            base = LATER + 10000 * self.keys.index(key)
            self.add(7400 + base, grammar.family_cache_name(FAMILY, key, COMMIT), f"{stamp}26:00Z", run_id=run_id,
                     head_sha=COMMIT, files=family_files(key))
            collected = self.add(7002 + base, grammar.collected_family_name(FAMILY, key), f"{stamp}11:00Z",
                                 run_id=run_id, head_sha=COMMIT)
            families.append({"family": FAMILY, "key": key, "available": True, "status": "available",
                             "collected_artifact_id": collected["id"],
                             "collected_digest": "sha256:" + sha(self.seeded[collected["id"]][1]),
                             "coverage_sha": COMMIT, "selected_artifact_id": FAMILY_SELECTED + base - LATER})
        promotion = helpers.promotion()
        promotion["implementation"]["run_id"] = run_id
        promotion.update(bundles=bundles, families=families)
        self.add(7100 + LATER, grammar.PROMOTION_NAME, f"{stamp}20:00Z", run_id=run_id, head_sha=COMMIT,
                 files={"promotion.json": canonical_json(promotion)})
        self.add(7300 + LATER, grammar.PAGES_ARTIFACT_NAME, f"{stamp}20:00Z", run_id=run_id, head_sha=COMMIT)

    def rotate(self, *, dry_run: bool = False, now: float = NOW, delay: float = 0.0, owner: int = OWNER,
               **environ: str) -> dict[str, Any]:
        return rotation.rotate_generation(self.invocation(**environ), api=self.api, owner_run_id=owner,
                                          owner_sha=COMMIT, delete_delay_seconds=delay, dry_run=dry_run, now=now,
                                          sleep=self.sleeps.append)

    def remaining(self) -> set[int]:
        return set(self.seeded) - set(self.api.deleted_artifact_ids)


def budget_of(remaining: int) -> Any:
    original = rotation.DeletionBudget
    return mock.patch.object(rotation, "DeletionBudget", lambda: original(remaining))


class DeletionBudgetTests(unittest.TestCase):
    def test_select_charges_nothing_and_counts_the_deferred_tail(self) -> None:
        budget = rotation.DeletionBudget(2)
        self.assertEqual(limits.DELETION_BUDGET, rotation.DeletionBudget().remaining)
        budget.begin_scope()
        self.assertEqual([1, 2], budget.select([1, 2, 3]))
        self.assertEqual([1, 2], budget.select([1, 2]))
        self.assertEqual((2, 1), (budget.remaining, budget.last_deferred_count))
        budget.consume()
        budget.consume()
        self.assertEqual([], budget.select([4]))
        self.assertEqual(2, budget.last_deferred_count)
        with self.assertRaises(MbError):
            budget.consume()
        budget.begin_scope()
        self.assertEqual(0, budget.last_deferred_count)

    def test_counters_must_be_non_negative_integers(self) -> None:
        for value in (-1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(MbError):
                rotation.DeletionBudget(value)  # type: ignore[arg-type]


class GenerationTests(unittest.TestCase):
    def test_every_superseded_family_is_retired_by_exact_id_in_order(self) -> None:
        world = World(self)
        summary = world.rotate(delay=1.0)
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)
        self.assertEqual({
            "compatibility_deferred_branches": [],
            "deferral_reasons": {},
            "deferred_branches": [],
            "deleted_artifact_ids": {KEY: KEY_DELETIONS},
            "deleted_compatibility_artifact_ids": {SCOPE: FAMILY_DELETIONS},
            "deleted_pages_run_artifact_ids": TRANSIENT_DELETIONS,
            "dry_run": False,
            "owner_run_id": OWNER,
            "pages_run_deferral_reason": None,
            "pages_run_deferred": False,
            "planned_artifact_ids": ALL_DELETIONS,
            "remaining_rotation_deletions": limits.DELETION_BUDGET - len(ALL_DELETIONS),
            "rotation_deletion_limit": limits.DELETION_BUDGET,
        }, summary)
        self.assertTrue(set(RETAINED) <= world.remaining())
        self.assertEqual([1.0] * len(ALL_DELETIONS), world.sleeps)
        canonical_json(summary)

    def test_dry_run_plans_and_reobserves_without_deleting(self) -> None:
        world = World(self, writable=False)
        summary = world.rotate(dry_run=True, delay=1.0)
        self.assertEqual([], world.api.deleted_artifact_ids)
        self.assertEqual([], world.sleeps)
        self.assertEqual(ALL_DELETIONS, summary["planned_artifact_ids"])
        self.assertEqual({KEY: []}, summary["deleted_artifact_ids"])
        self.assertEqual([], summary["deleted_pages_run_artifact_ids"])
        self.assertTrue(summary["dry_run"])
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS), summary["remaining_rotation_deletions"])

    def test_a_deleting_rotation_needs_a_writable_client(self) -> None:
        world = World(self, writable=False)
        with self.assertRaisesRegex(MbError, "writable"):
            world.rotate()

    def test_discovery_reads_exact_names_and_never_lists_the_repository(self) -> None:
        world = World(self)
        requests: list[tuple[str, dict[str, Any]]] = []
        get_json = world.api.get_json

        def recording(path: str, *, params: Any = None) -> Any:
            requests.append((path, dict(params or {})))
            return get_json(path, params=params)

        with mock.patch.object(world.api, "get_json", side_effect=recording):
            world.rotate()
        listings = [params for path, params in requests if path == f"/repos/{REPOSITORY}/actions/artifacts"]
        self.assertTrue(listings)
        self.assertTrue(all(grammar.is_kit_artifact_name(params.get("name")) for params in listings), listings)
        named = {params["name"] for params in listings}
        self.assertIn(grammar.handoff_name(KEY, 1), named)
        self.assertIn(grammar.cache_name(KEY, OLD), named)
        self.assertFalse(any(name.startswith("mb-baseline") for name in named))
        self.assertLessEqual(world.api.request_count, 120)

    def test_a_second_rotation_of_the_generation_is_unavailable_once_its_promotion_is_gone(self) -> None:
        world = World(self)
        world.rotate()
        with self.assertRaises(Unavailable):
            world.rotate()

    def test_a_retry_after_a_partial_rotation_finishes_the_generation(self) -> None:
        world = World(self)
        with budget_of(5):
            first = world.rotate()
        self.assertEqual(ALL_DELETIONS[:5], world.api.deleted_artifact_ids)
        self.assertTrue(first["pages_run_deferred"])
        world.rotate()
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)

    def test_a_history_row_without_a_head_is_skipped(self) -> None:
        world = World(self)
        headless = run(105, path=E2E, sha=COMMIT, created="2026-09-25T09:10:00Z")
        del headless["head_sha"]
        world.api.add_run(headless)
        summary = world.rotate()
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)
        self.assertEqual({}, summary["deferral_reasons"])


class OwnerAndReplacementTests(unittest.TestCase):
    def assert_nothing_deleted(self, world: World, error: type[MbError] = MbError, pattern: str = "") -> None:
        with self.assertRaisesRegex(error, pattern):
            world.rotate()
        self.assertEqual([], world.api.deleted_artifact_ids)

    def test_owner_must_be_a_completed_successful_default_branch_pages_run_at_its_sha(self) -> None:
        for overrides in ({"conclusion": "failure"}, {"status": "in_progress", "conclusion": None},
                          {"path": ".github/workflows/other.yml"}, {"head_branch": "feature"},
                          {"head_sha": NEWER}, {"event": "push"},
                          {"head_repository": {"full_name": "attacker/fork"}}):
            with self.subTest(overrides=overrides):
                world = World(self)
                world.api.add_run(owner_run(**overrides))
                self.assert_nothing_deleted(world, pattern="owner")

    def test_rotation_run_and_repository_are_bound(self) -> None:
        world = World(self)
        with self.assertRaisesRegex(MbError, "its own generation"):
            world.rotate(GITHUB_RUN_ID=str(OWNER))
        with self.assertRaisesRegex(MbError, "another repository"):
            world.rotate(GITHUB_REPOSITORY="The-Plum-Team/Other")
        self.assertEqual([], world.api.deleted_artifact_ids)

    def test_canonical_branch_must_be_the_default_branch(self) -> None:
        world = World(self)
        world.api = FakeGitHub(repository=REPOSITORY, default_branch="main", writable=True)
        self.assert_nothing_deleted(world, pattern="default branch")

    def test_owner_kit_must_be_resolved_from_its_referenced_workflows(self) -> None:
        world = World(self)
        world.api.add_run(owner_run(referenced_workflows=[]))
        self.assert_nothing_deleted(world)

    def test_missing_or_ambiguous_promotion(self) -> None:
        world = World(self)
        world.change(7100, expired=True)
        self.assert_nothing_deleted(world, Unavailable)
        world = World(self)
        world.add(7101, grammar.PROMOTION_NAME, "2026-09-25T10:51:00Z", run_id=OWNER, head_sha=COMMIT,
                  files={"promotion.json": canonical_json(world.promotion)})
        self.assert_nothing_deleted(world, pattern="several mb-promotion")

    def test_promotion_must_describe_the_owner_and_its_kit(self) -> None:
        changes: list[Callable[[dict[str, Any]], None]] = [
            lambda promotion: promotion["implementation"].update(run_id=OWNER + 1),
            lambda promotion: promotion["implementation"].update(run_attempt=2),
            lambda promotion: promotion["kit"].update(sha=helpers.h("other-kit", 40)),
            lambda promotion: promotion.update(repository="The-Plum-Team/Other"),
            lambda promotion: promotion.pop("site"),
        ]
        for change in changes:
            with self.subTest(change=change):
                world = World(self)
                world.replace_promotion(change)
                self.assert_nothing_deleted(world)

    def test_the_promotion_must_be_canonical_json(self) -> None:
        # The same document written by another serializer (as build never writes it) is refused
        # before any deletion, like every other validating reader of the promotion.
        world = World(self)
        record, _ = world.seeded[7100]
        pretty = json.dumps(world.promotion, indent=2, sort_keys=True).encode("utf-8")
        world.api.add_artifact({key: value for key, value in record.items() if key not in {"size_in_bytes", "digest"}},
                               zipped({"promotion.json": pretty}))
        self.assert_nothing_deleted(world, pattern="canonical")

    def test_a_fallback_is_retained_until_every_replacement_is_verified(self) -> None:
        other_kit = helpers.h("other-kit", 40)

        def kit(manifest: dict[str, Any], selection: dict[str, Any]) -> None:
            manifest["kit"]["sha"] = selection["kit"]["sha"] = other_kit

        def rerun_selection(manifest: dict[str, Any], selection: dict[str, Any]) -> None:
            selection["implementation"]["run_attempt"] = 2

        def owner_implementation(manifest: dict[str, Any], selection: dict[str, Any]) -> None:
            # Evidence of another subject commit, still collected by R at R's head.
            selection["implementation"] = helpers.embedded_selection()["implementation"]

        cases: dict[str, Callable[[World], None]] = {
            "missing cache": lambda world: world.change(7200, expired=True),
            "duplicate cache": lambda world: world.add(7201, grammar.cache_name(KEY, COMMIT), "2026-09-25T10:58:00Z",
                                                       run_id=OWNER, head_sha=COMMIT, files=compact_files()),
            "unrecorded manifest": lambda world: world.replace_promotion(
                lambda promotion: promotion["bundles"][0].update(manifest_sha256=helpers.h("other"))),
            "other selection": lambda world: world.replace_promotion(
                lambda promotion: promotion["bundles"][0].update(selected_artifact_id=SELECTED + 7)),
            "other kit": lambda world: world.replace_cache(compact_files(adjust=kit)),
            "other subject commit and coverage": lambda world: world.replace_cache(
                compact_files(commit=OLD, adjust=owner_implementation)),
            "other key": lambda world: world.replace_cache(compact_files(SECOND_KEY)),
            "selection of a later attempt": lambda world: world.replace_cache(compact_files(adjust=rerun_selection)),
        }
        for label, damage in cases.items():
            with self.subTest(label=label):
                world = World(self)
                damage(world)
                self.assert_nothing_deleted(world)

    def test_the_owner_inventory_must_be_exactly_its_promoted_generation(self) -> None:
        def unavailable_leg(world: World) -> None:
            world.change(7400, expired=True)
            world.replace_promotion(make_unavailable)

        created = "2026-09-25T10:58:00Z"
        cases: dict[str, Callable[[World], None]] = {
            "unpromoted cache": lambda world: world.add(
                7800, grammar.cache_name("mc9.9.9", COMMIT), created, run_id=OWNER, head_sha=COMMIT),
            "unpromoted family cache": lambda world: world.add(
                7801, grammar.family_cache_name(FAMILY, "mc9.9.9", COMMIT), created, run_id=OWNER, head_sha=COMMIT),
            "unpromoted collected": lambda world: world.add(
                7700, grammar.collected_name("mc9.9.9"), created, run_id=OWNER, head_sha=COMMIT),
            "second collected": lambda world: world.add(
                7003, grammar.collected_name(KEY), created, run_id=OWNER, head_sha=COMMIT),
            "collected of another digest": lambda world: world.change(7001, digest=f"sha256:{helpers.h('other')}"),
            "collected family of an unavailable leg": unavailable_leg,
            "second github-pages": lambda world: world.add(
                7301, grammar.PAGES_ARTIFACT_NAME, created, run_id=OWNER, head_sha=COMMIT),
            "artifact at another head": lambda world: world.add(
                7702, grammar.baseline_name(KEY, NEWER, 120), created, run_id=OWNER, head_sha=NEWER),
        }
        for label, damage in cases.items():
            with self.subTest(label=label):
                world = World(self)
                damage(world)
                self.assert_nothing_deleted(world, pattern="owner run")

    def test_replacements_are_bound_to_the_owner_not_to_the_live_config(self) -> None:
        # The rotation run reads the config at its own (possibly newer) head; R's evidence was
        # validated against R's config, so a later policy change must not block its rotation.
        world = World(self)
        world.configure(lambda config: config["images"].update(webp_quality=81))
        world.rotate()
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)
        world = World(self)
        world.configure(remove_families)
        summary = world.rotate()
        # Superseded family caches still yield to R's verified replacement; only the family handoffs,
        # whose producer can no longer be authenticated, are retained and reported.
        self.assertEqual([6002], summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertIn("not configured", summary["deferral_reasons"][SCOPE])
        self.assertTrue({9100, FAMILY_SELECTED} <= world.remaining())
        self.assertEqual(KEY_DELETIONS, summary["deleted_artifact_ids"][KEY])
        self.assertEqual(TRANSIENT_DELETIONS, summary["deleted_pages_run_artifact_ids"])

    def test_replacement_bytes_must_match_their_inventory(self) -> None:
        compact = compact_files()
        extra = {**compact, "images/extra.webp": b"unlisted image"}
        tampered = dict(compact)
        image = next(path for path in compact if path.startswith("images/"))
        tampered[image] = b"tampered image"
        for label, files in (("extra", extra), ("tampered", tampered)):
            with self.subTest(label=label):
                world = World(self)
                world.replace_cache(files)
                self.assert_nothing_deleted(world)

    def test_replacement_must_be_collected_by_the_owner(self) -> None:
        world = World(self)
        files = compact_files(collector=OWNER + 1)
        record, _ = world.seeded[7200]
        world.api.add_artifact(record, zipped(files))
        world.replace_promotion(lambda promotion: promotion["bundles"][0].update(
            manifest_sha256=sha(files["manifest.json"])))
        self.assert_nothing_deleted(world, pattern="collected by the owner")

    def test_a_rerun_owner_keeps_the_promotion_of_its_earlier_attempt(self) -> None:
        world = World(self)
        world.api.add_run(owner_run(run_attempt=2))
        summary = world.rotate()
        self.assertEqual(ALL_DELETIONS, summary["planned_artifact_ids"])


class FamilyLegTests(unittest.TestCase):
    """A carried family leg is a verified replacement; a family replacement that cannot be verified
    rejects only its own leg (with its collected artifact, github-pages and the promotion)."""

    #: What a carried world retires, in order: run 202's family handoff is newer than the bound.
    CARRIED = [6001, 6002, 5090, 9100, 8801, SELECTED, *TRANSIENT_DELETIONS]

    @staticmethod
    def carry(world: World, **envelope_fields: Any) -> None:
        """R found no family evidence at ``COMMIT`` and carried the previous generation's family cache
        (6002, produced at ``OLD`` by run 190) forward: refresh named R's copy by R's coverage but the
        envelope it copied verbatim still covers ``OLD``."""

        producer = helpers.run_claim(190, workflow_path=FAMILY_WORKFLOW, commit=OLD)
        world.replace_family_cache(family_files(commit=OLD, producer=producer, **envelope_fields))
        world.replace_promotion(lambda promotion: promotion["families"][0].update(selected_artifact_id=6002))

    def test_a_carried_leg_retires_the_generation_it_was_carried_from(self) -> None:
        for envelope_fields in ({}, {"carried_from": OLDER}):
            with self.subTest(envelope_fields=envelope_fields):
                world = World(self)
                self.carry(world, **envelope_fields)
                summary = world.rotate()
                self.assertEqual({}, summary["deferral_reasons"])
                self.assertEqual(self.CARRIED, world.api.deleted_artifact_ids)
                self.assertEqual({SCOPE: [6002, 9100]}, summary["deleted_compatibility_artifact_ids"])
                self.assertEqual({KEY: KEY_DELETIONS}, summary["deleted_artifact_ids"])
                self.assertEqual(TRANSIENT_DELETIONS, summary["deleted_pages_run_artifact_ids"])
                # The selected cache's owner (400) bounds the family handoffs even though the 90-day phase
                # retired that cache first: run 202's newer handoff is kept.
                self.assertTrue({7400, FAMILY_SELECTED} <= world.remaining())

    def test_a_dry_run_plans_a_carried_leg_like_the_deleting_run(self) -> None:
        world = World(self, writable=False)
        self.carry(world)
        summary = world.rotate(dry_run=True)
        self.assertEqual(self.CARRIED, summary["planned_artifact_ids"])
        self.assertEqual([], world.api.deleted_artifact_ids)

    def test_a_family_leg_mismatch_rejects_only_that_leg(self) -> None:
        tampered = family_files()
        tampered["images/pair.webp"] = b"tampered pair image"
        cases: dict[str, Callable[[World], None]] = {
            "missing family cache": lambda world: world.change(7400, expired=True),
            "duplicate family cache": lambda world: world.add(
                7401, grammar.family_cache_name(FAMILY, KEY, COMMIT), "2026-09-25T10:58:00Z", run_id=OWNER,
                head_sha=COMMIT, files=family_files()),
            "other envelope family": lambda world: world.replace_family_cache(family_files(family="other-family")),
            "other envelope key": lambda world: world.replace_family_cache(family_files(key=SECOND_KEY)),
            "other envelope repository": lambda world: world.replace_family_cache(
                family_files(repository="The-Plum-Team/Other")),
            "unlisted file": lambda world: world.replace_family_cache(
                {**family_files(), "images/unlisted.webp": b"unlisted"}),
            # A carried envelope is accepted for its coverage only: its inventory still binds.
            "unlisted file of a carried leg": lambda world: world.replace_family_cache(
                {**family_files(commit=OLD), "images/unlisted.webp": b"unlisted"}),
            "tampered file": lambda world: world.replace_family_cache(tampered),
            "invalid envelope": lambda world: world.replace_family_cache(family_files(schema_version=2)),
        }
        for label, damage in cases.items():
            with self.subTest(label=label):
                world = World(self)
                damage(world)
                summary = world.rotate()
                self.assertEqual({KEY: KEY_DELETIONS}, summary["deleted_artifact_ids"])
                self.assertEqual([], summary["deferred_branches"])
                self.assertEqual({SCOPE: []}, summary["deleted_compatibility_artifact_ids"])
                self.assertEqual([SCOPE], summary["compatibility_deferred_branches"])
                self.assertIn("replacement cannot be verified", summary["deferral_reasons"][SCOPE])
                # Only the key's collected artifact retires: the leg's collected artifact, github-pages and
                # the promotion yield to the unverified replacement, so a retry can re-plan.
                self.assertEqual([7001], summary["deleted_pages_run_artifact_ids"])
                self.assertTrue(summary["pages_run_deferred"])
                self.assertIn(f"{SCOPE}, github-pages and mb-promotion", summary["pages_run_deferral_reason"])
                self.assertTrue({6002, 9100, FAMILY_SELECTED, 7002, 7300, 7100} <= world.remaining())
                self.assertEqual([*KEY_DELETIONS, 7001], world.api.deleted_artifact_ids)

    def test_one_rejected_leg_leaves_the_other_legs_to_rotate(self) -> None:
        world = World(self, keys=(KEY, SECOND_KEY), family_keys=(KEY, SECOND_KEY))
        world.replace_family_cache(family_files(SECOND_KEY, family="other-family"), position=1)
        summary = world.rotate()
        second = f"{FAMILY}--{SECOND_KEY}"
        self.assertEqual({SCOPE: FAMILY_DELETIONS, second: []}, summary["deleted_compatibility_artifact_ids"])
        self.assertEqual([second], summary["compatibility_deferred_branches"])
        self.assertEqual({KEY: KEY_DELETIONS, SECOND_KEY: [16001, 18801, SELECTED + 10000]},
                         summary["deleted_artifact_ids"])
        self.assertEqual([7001, 17001, 7002], summary["deleted_pages_run_artifact_ids"])
        self.assertTrue({16002, 19100, FAMILY_SELECTED + 10000, 17002, 17400, 7300, 7100} <= world.remaining())

    def test_an_unverified_leg_keeps_its_caches_protected(self) -> None:
        world = World(self)
        world.add(7401, grammar.family_cache_name(FAMILY, KEY, COMMIT), "2026-09-25T10:58:00Z", run_id=OWNER,
                  head_sha=COMMIT, files=family_files())
        with tempfile.TemporaryDirectory() as scratch:
            plan = rotation._Rotation(world.invocation(), world.api, owner_run_id=OWNER, owner_sha=COMMIT,
                                      delete_delay_seconds=0.0, dry_run=True, now=datetime.now(timezone.utc),
                                      sleep=world.sleeps.append, workdir=Path(scratch))
            plan.authenticate_owner()
            plan.load_promotion()
            plan.load_replacements()
        self.assertEqual([(FAMILY, KEY)], list(plan.rejected_legs))
        self.assertEqual([], plan.family_keeps)
        self.assertTrue({7400, 7401} <= plan.protected)

    def test_an_unverifiable_key_replacement_still_stops_everything(self) -> None:
        world = World(self)
        world.replace_family_cache(family_files(key=SECOND_KEY))
        world.change(7200, expired=True)
        with self.assertRaisesRegex(MbError, "exactly one unexpired mb-cache"):
            world.rotate()
        self.assertEqual([], world.api.deleted_artifact_ids)


class RetirementRuleTests(unittest.TestCase):
    def test_newer_foreign_expired_failed_and_unpromoted_caches_are_never_candidates(self) -> None:
        world = World(self)
        world.add(6010, grammar.cache_name(KEY, OLD), "2026-09-24T10:57:00Z", run_id=PREVIOUS, head_sha=OLD,
                  expired=True)
        world.add(6011, grammar.cache_name("mc9.9.9", OLD), "2026-09-24T10:57:00Z", run_id=PREVIOUS, head_sha=OLD)
        world.add(6012, "pages-cache-mc1.20.1", "2026-09-24T10:57:00Z", run_id=PREVIOUS, head_sha=OLD)
        world.api.add_run(pages_run(410, OLD, "2026-09-24T11:30:00Z", conclusion="failure"))
        world.add(6013, grammar.cache_name(KEY, OLD), "2026-09-24T11:55:00Z", run_id=410, head_sha=OLD)
        world.api.add_run(pages_run(700, OLD, "2026-09-25T10:50:00Z"))
        world.add(6014, grammar.cache_name(KEY, OLD), "2026-09-25T10:58:00Z", run_id=700, head_sha=OLD)
        world.add(6015, grammar.cache_name(KEY, COMMIT), "2026-09-25T10:59:00Z", run_id=700, head_sha=OLD)
        summary = world.rotate()
        self.assertEqual(KEY_DELETIONS, summary["deleted_artifact_ids"][KEY])
        self.assertTrue({6010, 6011, 6012, 6013, 6014, 6015} <= world.remaining())

    def test_same_coverage_duplicates_of_earlier_owners_are_retired(self) -> None:
        world = World(self)
        world.api.add_run(pages_run(450, COMMIT, "2026-09-25T09:55:00Z"))
        world.add(6020, grammar.cache_name(KEY, COMMIT), "2026-09-25T09:58:00Z", run_id=450, head_sha=COMMIT)
        summary = world.rotate()
        self.assertEqual([6001, 6020, 5090, 8801, SELECTED], summary["deleted_artifact_ids"][KEY])

    def test_another_owner_workflow_rejects_its_family_and_keeps_the_others(self) -> None:
        world = World(self)
        world.api.add_run(run(420, path=".github/workflows/build-gate.yml", sha=OLD, created="2026-09-24T08:00:00Z"))
        world.add(6030, grammar.cache_name(KEY, OLD), "2026-09-24T08:10:00Z", run_id=420, head_sha=OLD)
        summary = world.rotate()
        self.assertEqual([5090, 8801, SELECTED], summary["deleted_artifact_ids"][KEY])
        self.assertEqual([KEY], summary["deferred_branches"])
        self.assertIn("cache retirement", summary["deferral_reasons"][KEY])
        self.assertTrue({6001, 6030} <= world.remaining())
        self.assertEqual(FAMILY_DELETIONS, summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS) + 1, summary["remaining_rotation_deletions"])

    def test_fork_runs_planting_kit_names_on_a_master_branch_are_ignored(self) -> None:
        world = World(self)
        world.api.add_run(run(430, path=".github/workflows/evil.yml", sha=OLD, created="2026-09-24T08:00:00Z",
                              event="pull_request", head_repository={"full_name": "attacker/Quick-Skin-Mod"}))
        world.add(6040, grammar.cache_name(KEY, OLD), "2026-09-24T08:10:00Z", run_id=430, head_sha=OLD)
        world.add(8806, grammar.handoff_name(KEY, 1), "2026-09-24T08:11:00Z", run_id=430, head_sha=OLD)
        world.add(9102, grammar.family_handoff_name(FAMILY, KEY, 1), "2026-09-24T08:12:00Z", run_id=430,
                  head_sha=OLD)
        summary = world.rotate()
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)
        self.assertEqual({}, summary["deferral_reasons"])
        self.assertTrue({6040, 8806, 9102} <= world.remaining())

    def test_handoffs_of_failed_newer_or_other_branch_runs_are_kept(self) -> None:
        world = World(self)
        world.api.add_run(run(95, path=E2E, sha=OLD, created="2026-09-24T12:00:00Z", conclusion="failure"))
        world.add(8803, grammar.handoff_name(KEY, 1), "2026-09-24T12:10:00Z", run_id=95, head_sha=OLD)
        world.api.add_run(run(96, path=E2E, sha=OLDER, created="2026-09-24T13:00:00Z", branch="feature"))
        world.add(8804, grammar.handoff_name(KEY, 1), "2026-09-24T13:10:00Z", run_id=96, head_sha=OLDER,
                  branch="feature")
        world.api.add_run(run(110, path=E2E, sha=COMMIT, created="2026-09-25T09:10:00Z"))
        world.add(8805, grammar.handoff_name(KEY, 1), "2026-09-25T09:20:00Z", run_id=110, head_sha=COMMIT)
        summary = world.rotate()
        self.assertEqual(KEY_DELETIONS, summary["deleted_artifact_ids"][KEY])
        self.assertEqual([], summary["deferred_branches"])
        self.assertTrue({8802, 8803, 8804, 8805} <= world.remaining())

    def test_the_consumed_handoff_must_equal_the_authenticated_selection(self) -> None:
        world = World(self)
        world.change(SELECTED, digest=f"sha256:{helpers.h('another-zip')}")
        summary = world.rotate()
        self.assertEqual([6001, 5090], summary["deleted_artifact_ids"][KEY])
        self.assertIn("handoff retirement", summary["deferral_reasons"][KEY])
        self.assertTrue({8801, SELECTED} <= world.remaining())

    def test_a_handoff_owner_of_another_workflow_rejects_the_handoff_family(self) -> None:
        world = World(self)
        world.api.add_run(run(90, path=".github/workflows/build-gate.yml", sha=OLD, created="2026-09-24T09:00:00Z"))
        summary = world.rotate()
        # Run 90 is no successful source run any more, so neither its head (the previous generation's
        # coverage) nor its anchor is named; its handoff's owner rejects the handoff family.
        self.assertEqual([], summary["deleted_artifact_ids"][KEY])
        self.assertIn("handoff retirement", summary["deferral_reasons"][KEY])
        self.assertTrue({6001, 8801, SELECTED, 5090} <= world.remaining())

    def test_family_handoffs_follow_the_selected_family_artifact(self) -> None:
        for gone in (True, False):
            with self.subTest(gone=gone):
                world = World(self)
                if gone:
                    # Without the selected family artifact its envelope's producer run (202) bounds "older".
                    world.api.delete(f"/repos/{REPOSITORY}/actions/artifacts/{FAMILY_SELECTED}")
                else:
                    world.change(FAMILY_SELECTED, expired=True)
                summary = world.rotate()
                self.assertEqual([6002, 9100], summary["deleted_compatibility_artifact_ids"][SCOPE])
        world = World(self)
        world.change(FAMILY_SELECTED, name=grammar.family_handoff_name(FAMILY, SECOND_KEY, 1))
        summary = world.rotate()
        self.assertEqual([6002], summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertIn("family handoff retirement", summary["deferral_reasons"][SCOPE])

    def test_unavailable_family_legs_keep_their_caches(self) -> None:
        # A superseded or unavailable leg has neither a family cache nor a collected artifact in R.
        world = World(self)
        world.change(7400, expired=True)
        world.change(7002, expired=True)
        world.replace_promotion(make_unavailable)
        summary = world.rotate()
        self.assertEqual({}, summary["deleted_compatibility_artifact_ids"])
        self.assertTrue({6002, 9100, FAMILY_SELECTED} <= world.remaining())
        self.assertEqual([7001, 7300, 7100], summary["deleted_pages_run_artifact_ids"])

    def test_transients_are_exactly_the_owner_generation_with_the_promotion_last(self) -> None:
        world = World(self)
        world.api.add_run(pages_run(710, OLD, "2026-09-25T08:00:00Z"))
        world.add(7701, grammar.PAGES_ARTIFACT_NAME, "2026-09-25T08:10:00Z", run_id=710, head_sha=OLD)
        summary = world.rotate()
        self.assertEqual(TRANSIENT_DELETIONS, summary["deleted_pages_run_artifact_ids"])
        self.assertEqual(7100, world.api.deleted_artifact_ids[-1])
        self.assertIn(7701, world.remaining())

    def test_newer_family_handoffs_created_before_the_owner_are_kept(self) -> None:
        world = World(self)
        world.api.add_run(run(205, path=FAMILY_WORKFLOW, sha=COMMIT, created="2026-09-25T09:50:00Z",
                              event="repository_dispatch"))
        world.add(9105, grammar.family_handoff_name(FAMILY, KEY, 1), "2026-09-25T10:00:00Z", run_id=205,
                  head_sha=COMMIT)
        summary = world.rotate()
        self.assertEqual(FAMILY_DELETIONS, summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertIn(9105, world.remaining())

    def test_a_handoff_naming_an_attempt_its_run_never_had_rejects_its_family(self) -> None:
        world = World(self)
        # R consumed the second attempt's family handoff, so rotation also lists the "--a2" name.
        world.api.add_run(run(202, path=FAMILY_WORKFLOW, sha=COMMIT, created="2026-09-25T09:40:00Z",
                              event="repository_dispatch", attempt=2))
        world.change(FAMILY_SELECTED, name=grammar.family_handoff_name(FAMILY, KEY, 2))
        world.add(9103, grammar.family_handoff_name(FAMILY, KEY, 2), "2026-09-24T09:46:00Z", run_id=190,
                  head_sha=OLD)
        summary = world.rotate()
        self.assertEqual([6002], summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertIn("names an attempt its run never had", summary["deferral_reasons"][SCOPE])
        self.assertTrue({9100, 9103, FAMILY_SELECTED} <= world.remaining())


class AnchorTests(unittest.TestCase):
    def seed_history(self, world: World) -> None:
        for run_id, commit, day in ((70, OLDEST, "01"), (80, OLDER, "10")):
            world.api.add_run(run(run_id, path=E2E, sha=commit, created=f"2026-09-{day}T09:00:00Z"))
            world.add(5000 + run_id, grammar.anchor_name(KEY, commit, run_id, 1), f"2026-09-{day}T09:30:00Z",
                      run_id=run_id, head_sha=commit)

    def test_the_newest_anchor_is_kept_and_older_ones_retire_after_the_successor_grace(self) -> None:
        for grace, retired in ((0, [5070, 5080, 5090]), (8, [5070]), (30, [])):
            with self.subTest(grace=grace):
                world = World(self, grace=grace)
                self.seed_history(world)
                summary = world.rotate()
                anchors = [identifier for identifier in summary["deleted_artifact_ids"][KEY]
                           if identifier in {5070, 5080, 5090, 5101, 5120}]
                self.assertEqual(retired, anchors)
                self.assertTrue({5101, 5120} <= world.remaining())

    def test_the_grace_boundary_names_anchors_behind_many_newer_runs(self) -> None:
        # Block Pops runs about ten successful source runs a day against an eight-day grace: the
        # anchors whose successor grace has passed sit far behind the newest runs.
        world = World(self, grace=8)
        self.seed_history(world)
        for offset in range(24):
            run_id = 300 + offset
            commit = helpers.h(f"recent-commit-{offset}", 40)
            created = f"2026-09-{18 + offset // 5:02d}T{(offset % 5) * 2 + 8:02d}:00:00Z"
            world.api.add_run(run(run_id, path=E2E, sha=commit, created=created))
            world.add(5300 + offset, grammar.anchor_name(KEY, commit, run_id, 1), created.replace(":00:00Z", ":30:00Z"),
                      run_id=run_id, head_sha=commit)
        named: list[str] = []
        get_json = world.api.get_json

        def recording(path: str, *, params: Any = None) -> Any:
            name = (params or {}).get("name")
            if isinstance(name, str) and name.startswith("mb-anchor--"):
                named.append(name)
            return get_json(path, params=params)

        with mock.patch.object(world.api, "get_json", side_effect=recording):
            summary = world.rotate()
        self.assertEqual([6001, 5070, 8801, SELECTED], summary["deleted_artifact_ids"][KEY])
        self.assertTrue({5080, 5090, 5101, *range(5300, 5324)} <= world.remaining())
        # Only runs created at or before the boundary (2026-09-17T11:00) are named.
        self.assertEqual({grammar.anchor_name(KEY, OLDER, 80, 1), grammar.anchor_name(KEY, OLDEST, 70, 1)}, set(named))

    def test_an_anchor_uploaded_after_the_boundary_is_a_successor_within_its_grace(self) -> None:
        # Run 85 started before the boundary (2026-09-17T11:00) but uploaded its anchor after it: 5085
        # is 5080's first successor and still inside the grace, so only 5070 retires.
        world = World(self, grace=8)
        self.seed_history(world)
        world.api.add_run(run(85, path=E2E, sha=NEWER, created="2026-09-17T10:00:00Z"))
        world.add(5085, grammar.anchor_name(KEY, NEWER, 85, 1), "2026-09-17T12:00:00Z", run_id=85, head_sha=NEWER)
        summary = world.rotate()
        self.assertEqual([6001, 5070, 8801, SELECTED], summary["deleted_artifact_ids"][KEY])
        self.assertTrue({5080, 5085, 5090, 5101} <= world.remaining())

    def test_anchor_reads_are_bounded_per_key(self) -> None:
        world = World(self)
        for offset in range(limits.ANCHOR_PROBES + 4):
            run_id = 300 + offset
            commit = helpers.h(f"older-commit-{offset}", 40)
            created = f"2026-09-{2 + offset:02d}T09:00:00Z"
            world.api.add_run(run(run_id, path=E2E, sha=commit, created=created))
            world.add(5300 + offset, grammar.anchor_name(KEY, commit, run_id, 1), created.replace(":00:00Z", ":30:00Z"),
                      run_id=run_id, head_sha=commit)
        summary = world.rotate()
        # Runs 101 and 90 plus the newest 14 older runs are named; the oldest six are left to retention.
        anchors = {5090, 5101, *range(5300, 5320)}
        retired = [identifier for identifier in summary["deleted_artifact_ids"][KEY] if identifier in anchors]
        self.assertEqual([*range(5306, 5320), 5090], retired)
        self.assertTrue({5101, *range(5300, 5306)} <= world.remaining())

    def test_planted_anchor_names_never_become_the_protected_anchor(self) -> None:
        world = World(self)
        world.api.add_run(run(431, path=".github/workflows/evil.yml", sha=COMMIT, created="2026-09-25T09:55:00Z",
                              event="pull_request", head_repository={"full_name": "attacker/Quick-Skin-Mod"}))
        world.add(5999, grammar.anchor_name(KEY, COMMIT, 101, 1), "2026-09-25T10:00:00Z", run_id=431,
                  head_sha=COMMIT)
        world.api.add_run(run(133, path=E2E, sha=NEWER, created="2026-09-25T09:58:00Z", conclusion="failure"))
        world.add(5998, grammar.anchor_name(KEY, OLD, 90, 1), "2026-09-25T10:05:00Z", run_id=133, head_sha=NEWER)
        summary = world.rotate()
        self.assertEqual(KEY_DELETIONS, summary["deleted_artifact_ids"][KEY])
        self.assertTrue({5101, 5998, 5999} <= world.remaining())

    def test_unauthenticated_anchors_are_neither_candidates_nor_successors(self) -> None:
        world = World(self, grace=8)
        self.seed_history(world)
        # Run 80 is not a successful source run: its anchor cannot be the successor that makes 5070
        # retirable, and it is never retired itself.
        world.api.add_run(run(80, path=E2E, sha=OLDER, created="2026-09-10T09:00:00Z", conclusion="failure"))
        summary = world.rotate()
        self.assertNotIn(5070, summary["deleted_artifact_ids"][KEY])
        self.assertTrue({5070, 5080} <= world.remaining())

    def test_anchor_owners_must_carry_the_configured_display_title(self) -> None:
        world = World(self, display_title="Packaged E2E / {subject_commit}")
        for run_id, commit, created in ((90, OLD, "2026-09-24T09:00:00Z"), (101, COMMIT, "2026-09-25T09:00:00Z")):
            world.api.add_run(run(run_id, path=E2E, sha=commit, created=created, title=f"Packaged E2E / {commit}"))
        summary = world.rotate()
        self.assertIn(5090, summary["deleted_artifact_ids"][KEY])
        world = World(self, display_title="Packaged E2E / {subject_commit}")
        world.api.add_run(run(101, path=E2E, sha=COMMIT, created="2026-09-25T09:00:00Z",
                              title=f"Packaged E2E / {COMMIT}"))
        summary = world.rotate()
        # Run 90's title does not bind its head: 5090 is not authenticated, so 5101 is the only anchor.
        self.assertNotIn(5090, summary["deleted_artifact_ids"][KEY])
        self.assertIn(5090, world.remaining())

    def test_keys_without_a_declared_anchor_never_retire_anchors(self) -> None:
        world = World(self, keys=(KEY, SECOND_KEY))
        world.add(5190, grammar.anchor_name(SECOND_KEY, OLD, 90, 1), "2026-09-24T09:30:00Z", run_id=90, head_sha=OLD)
        world.add(5191, grammar.anchor_name(SECOND_KEY, COMMIT, 101, 1), "2026-09-25T09:30:00Z", run_id=101,
                  head_sha=COMMIT)
        summary = world.rotate()
        self.assertEqual([16001, 18801, SELECTED + 10000], summary["deleted_artifact_ids"][SECOND_KEY])
        self.assertTrue({5190, 5191} <= world.remaining())


class BudgetAndDeferralTests(unittest.TestCase):
    def test_the_global_budget_bounds_every_family_and_defers_the_rest(self) -> None:
        world = World(self)
        with budget_of(2):
            summary = world.rotate()
        # The key's cache and its family leg's cache (90 days) come before everything else.
        self.assertEqual([6001, 6002], world.api.deleted_artifact_ids)
        self.assertEqual({KEY: [6001]}, summary["deleted_artifact_ids"])
        self.assertEqual({SCOPE: [6002]}, summary["deleted_compatibility_artifact_ids"])
        self.assertEqual([KEY], summary["deferred_branches"])
        self.assertEqual([SCOPE], summary["compatibility_deferred_branches"])
        self.assertTrue(summary["pages_run_deferred"])
        self.assertIn("budget", summary["pages_run_deferral_reason"])
        self.assertEqual(0, summary["remaining_rotation_deletions"])
        self.assertIn("exhausted", summary["deferral_reasons"][KEY])
        self.assertIn("exhausted", summary["deferral_reasons"][SCOPE])
        self.assertTrue({5090, 8801, SELECTED, 9100, FAMILY_SELECTED, 7100} <= world.remaining())

    def test_caches_take_priority_within_one_shared_budget(self) -> None:
        budget = limits.DELETION_BUDGET
        for extra, family_retired in ((budget - 2, True), (budget, False)):
            with self.subTest(extra=extra):
                world = World(self)
                for offset in range(extra):
                    world.add(6100 + offset, grammar.cache_name(KEY, OLD),
                              f"2026-09-24T10:{offset // 2:02d}:{15 + 30 * (offset % 2):02d}Z", run_id=PREVIOUS,
                              head_sha=OLD)
                summary = world.rotate()
                caches = [*range(6100, 6100 + extra), 6001][:budget]
                self.assertEqual(caches, summary["deleted_artifact_ids"][KEY])
                self.assertEqual({SCOPE: [6002] if family_retired else []},
                                 summary["deleted_compatibility_artifact_ids"])
                self.assertEqual([KEY], summary["deferred_branches"])
                reason = summary["deferral_reasons"][KEY]
                self.assertIn("exhausted", reason)
                # budget + 1 superseded caches: the group is cut to the budget and its oldest are retired first.
                self.assertEqual(not family_retired, "left 1 artifact(s)" in reason)
                self.assertTrue({5090, 8801, SELECTED} <= world.remaining())

    def test_the_budget_retires_a_whole_quick_skin_generation_long_lived_first(self) -> None:
        # Seventeen keys, each with a family leg, a superseded cache and family cache, two handoffs and
        # two family handoffs: 35 superseded 90-day artifacts, which the SPEC's original budget of 32
        # could never retire. They all go first, one key at a time (its cache, its family cache, its
        # anchor); the family handoffs (7 days) take the rest of the budget, and the last family
        # handoffs, every 1-day handoff and R's transients (the promotion among them) are deferred.
        keys = QS_KEYS
        world = World(self, keys=keys, family_keys=keys)
        summary = world.rotate()
        order = long_lived_order(keys)
        self.assertEqual(35, len(order))
        self.assertLess(len(order), limits.DELETION_BUDGET)
        family_handoffs = [identifier + 10000 * keys.index(key) for key in sorted(keys)
                           for identifier in (9100, FAMILY_SELECTED)]
        expected = [*order, *family_handoffs][:limits.DELETION_BUDGET]
        self.assertEqual(expected, world.api.deleted_artifact_ids)
        self.assertEqual(0, summary["remaining_rotation_deletions"])
        self.assertFalse(set(order) & world.remaining(), "no superseded 90-day artifact is left to retention")
        self.assertEqual(sorted(keys), summary["deferred_branches"])
        legs = [f"{FAMILY}--{key}" for key in sorted(keys)]
        self.assertEqual(legs, list(summary["deleted_compatibility_artifact_ids"]))
        self.assertTrue(summary["pages_run_deferred"])
        self.assertEqual([], summary["deleted_pages_run_artifact_ids"])
        short_lived = {7300, 7100, *family_handoffs[limits.DELETION_BUDGET - len(order):]}
        for position in range(len(keys)):
            base = 10000 * position
            short_lived |= {8801 + base, SELECTED + base, 7001 + base, 7002 + base}
        self.assertEqual(short_lived, short_lived & world.remaining())
        last = sorted(keys)[-1]
        self.assertEqual([6001 + 10000 * keys.index(last)], summary["deleted_artifact_ids"][last])
        self.assertIn("exhausted", summary["deferral_reasons"][f"{FAMILY}--{last}"])

    def test_a_rejected_group_reports_its_rejection_not_a_budget_cut(self) -> None:
        # Four superseded caches meet a budget of two, then their owner changes before the first
        # deletion: the group is rejected whole, so its reason must not claim the budget cut its tail.
        world = World(self)
        for offset in range(3):
            world.add(6100 + offset, grammar.cache_name(KEY, OLD), f"2026-09-24T10:{offset:02d}:30Z",
                      run_id=PREVIOUS, head_sha=OLD)
        get_json = world.api.get_json
        observed = {"count": 0}

        def reads(path: str, *, params: Any = None) -> Any:
            value = get_json(path, params=params)
            if path == f"/repos/{REPOSITORY}/actions/runs/{PREVIOUS}":
                observed["count"] += 1
                if observed["count"] > 1:
                    return {**value, "conclusion": "failure"}
            return value

        with budget_of(2), mock.patch.object(world.api, "get_json", side_effect=reads):
            summary = world.rotate()
        self.assertEqual([5090, 9100], world.api.deleted_artifact_ids)
        reason = summary["deferral_reasons"][KEY]
        self.assertIn("cache retirement", reason)
        self.assertIn("changed before its retirement", reason)
        self.assertIn("exhausted", reason)
        self.assertNotIn("left", reason)
        # The leg's family cache group is rejected the same way; its family handoff group completed
        # with its tail cut by the budget, and says so.
        self.assertIn("family cache retirement", summary["deferral_reasons"][SCOPE])
        self.assertIn("left 1 artifact(s)", summary["deferral_reasons"][SCOPE])

    def test_404_and_failed_deletions_still_spend_their_attempt(self) -> None:
        for status in (404, 500):
            with self.subTest(status=status):
                world = World(self)
                delete = world.api.delete

                def failing(path: str) -> None:
                    if path.endswith("/6001"):
                        error = ApiNotFound if status == 404 else ApiError
                        raise error(f"HTTP {status}", status=status, method="DELETE", path=path)
                    delete(path)

                with mock.patch.object(world.api, "delete", side_effect=failing):
                    summary = world.rotate()
                self.assertNotIn(6001, world.api.deleted_artifact_ids)
                self.assertEqual(limits.DELETION_BUDGET - len(summary["planned_artifact_ids"]),
                                 summary["remaining_rotation_deletions"])
                if status == 404:
                    self.assertEqual(KEY_DELETIONS[1:], summary["deleted_artifact_ids"][KEY])
                    self.assertEqual([], summary["deferred_branches"])
                else:
                    self.assertEqual([], summary["deleted_artifact_ids"][KEY])
                    self.assertEqual([KEY], summary["deferred_branches"])
                    self.assertEqual(FAMILY_DELETIONS, summary["deleted_compatibility_artifact_ids"][SCOPE])
                expected = ALL_DELETIONS if status == 404 else [6001, *FAMILY_DELETIONS, *TRANSIENT_DELETIONS]
                self.assertEqual(expected, summary["planned_artifact_ids"])

    def test_a_rejected_family_spends_no_budget(self) -> None:
        world = World(self)
        world.api.add_run(run(190, path=".github/workflows/build-gate.yml", sha=OLD, created="2026-09-24T09:40:00Z",
                              event="repository_dispatch"))
        summary = world.rotate()
        self.assertEqual([SCOPE], summary["compatibility_deferred_branches"])
        self.assertIn("family handoff retirement", summary["deferral_reasons"][SCOPE])
        self.assertEqual([6002], summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertTrue({9100, FAMILY_SELECTED} <= world.remaining())
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS) + 2, summary["remaining_rotation_deletions"])


class LeftoverTests(unittest.TestCase):
    """The long-lived artifacts a spent budget deferred stay discoverable through the last promoted
    key's names in the window, and a later rotation with budget left retires them after its own
    previous generation's long-lived artifacts and before every shorter-lived kind. The deferral
    mechanics are shown at SPEC §5.5's original budget of 32 (:data:`SMALL_BUDGET`), which a Quick
    Skin generation's 35 long-lived artifacts exceed; any budget smaller than a generation behaves
    alike."""

    SMALL_BUDGET = 32

    @staticmethod
    def base(key: str) -> int:
        return 10000 * QS_KEYS.index(key)

    def test_a_spent_budget_reads_no_leftover_name(self) -> None:
        world = World(self, keys=QS_KEYS, family_keys=QS_KEYS)
        names: list[str] = []
        get_json = world.api.get_json

        def recording(path: str, *, params: Any = None) -> Any:
            name = (params or {}).get("name")
            if isinstance(name, str) and name.startswith(("mb-cache--", "mb-family-cache--")):
                names.append(name)
            return get_json(path, params=params)

        with mock.patch.object(world.api, "get_json", side_effect=recording), \
                budget_of(len(long_lived_order(QS_KEYS))):
            summary = world.rotate()
        self.assertEqual(0, summary["remaining_rotation_deletions"])
        # The previous generation spent the budget exactly: the leftover phase was never planned, so
        # only the probe key's window was read.
        self.assertEqual({grammar.cache_name(KEY, COMMIT), grammar.cache_name(KEY, OLD)}, set(names))

    def test_retries_of_the_same_owner_retire_its_leftovers_and_finish_the_generation(self) -> None:
        # The operator recovery (the rotation dispatched again for the same owner) continues where
        # the budget stopped, although the first rotation already retired the probe key's
        # predecessor through which the previous generation was found.
        budget = self.SMALL_BUDGET
        world = World(self, keys=QS_KEYS, family_keys=QS_KEYS)
        order = long_lived_order(QS_KEYS)
        leftovers = order[budget:]
        *_, penultimate, last = sorted(QS_KEYS)
        self.assertEqual([6002 + self.base(penultimate), 6001 + self.base(last), 6002 + self.base(last)], leftovers)
        with budget_of(budget):
            world.rotate()
            self.assertTrue(set(leftovers) <= world.remaining())
            summary = world.rotate()
        self.assertEqual(order, world.api.deleted_artifact_ids[:len(order)])
        second = world.api.deleted_artifact_ids[budget:]
        self.assertEqual(budget, len(second))
        family_handoffs = {identifier + 10000 * position for position in range(len(QS_KEYS))
                           for identifier in (9100, FAMILY_SELECTED)}
        self.assertTrue(set(second[len(leftovers):]) <= family_handoffs)
        self.assertEqual([6001 + self.base(last)], summary["deleted_artifact_ids"][last])
        self.assertEqual(6002 + self.base(last), summary["deleted_compatibility_artifact_ids"][f"{FAMILY}--{last}"][0])
        rotations = 2
        while True:
            try:
                with budget_of(budget):
                    world.rotate()
            except Unavailable:
                break
            rotations += 1
            self.assertLess(rotations, 10)
        # 139 superseded artifacts: five rotations of 32 attempts, the promotion retired last.
        self.assertEqual(5, rotations)
        self.assertEqual(7100, world.api.deleted_artifact_ids[-1])
        retained = {7500, 7600, 6003, 6004, 5101, 5120}
        for position in range(len(QS_KEYS)):
            retained |= {identifier + 10000 * position for identifier in (7200, 7400, 8802)}
        self.assertEqual(retained, world.remaining())

    def test_the_following_rotation_retires_the_leftovers_its_budget_reaches(self) -> None:
        # Two generations of seventeen keys. R's rotation defers the previous generation's last three
        # long-lived artifacts. A later publication of the same head (run 800) promotes every key but
        # only the last two keys' family legs, so it supersedes 20 long-lived artifacts of R: its
        # rotation retires those, then the leftovers, before any shorter-lived artifact.
        budget = self.SMALL_BUDGET
        world = World(self, keys=QS_KEYS, family_keys=QS_KEYS)
        order = long_lived_order(QS_KEYS)
        leftovers = order[budget:]
        *_, penultimate, last = sorted(QS_KEYS)
        with budget_of(budget):
            world.rotate()
        self.assertTrue(set(leftovers) <= world.remaining())
        world.publish_later(800, "2026-09-25T12:00:00Z", legs=(penultimate, last))
        before = len(world.api.deleted_artifact_ids)
        with budget_of(budget):
            summary = world.rotate(owner=800, now=datetime(2026, 9, 25, 13, 0, tzinfo=timezone.utc).timestamp())
        deleted = world.api.deleted_artifact_ids[before:]
        previous = []
        for key in sorted(QS_KEYS):
            previous.append(7200 + self.base(key))
            if key in (penultimate, last):
                previous.append(7400 + self.base(key))
            if key == KEY:
                previous.append(5101)
        self.assertEqual(20, len(previous))
        self.assertEqual([*previous, *leftovers], deleted[:len(previous) + len(leftovers)])
        # Then the promoted legs' family handoffs (7 days) and the first 1-day handoffs.
        legs = {identifier + self.base(key) for key in (penultimate, last) for identifier in (9100, FAMILY_SELECTED)}
        self.assertEqual(legs, set(deleted[23:27]))
        self.assertEqual([8801, SELECTED], deleted[27:29])
        self.assertEqual(budget, len(deleted))
        self.assertEqual([7200 + self.base(last), 6001 + self.base(last)], summary["deleted_artifact_ids"][last])
        self.assertEqual([7400 + self.base(penultimate), 6002 + self.base(penultimate)],
                         summary["deleted_compatibility_artifact_ids"][f"{FAMILY}--{penultimate}"][:2])
        self.assertFalse(set(leftovers) & world.remaining())
        # R's family caches of the legs run 800 did not promote are not superseded by it.
        unpromoted = {7400 + self.base(key) for key in QS_KEYS if key not in (penultimate, last)}
        self.assertTrue(unpromoted <= world.remaining())

    def test_earlier_generations_follow_the_previous_one_and_precede_the_handoffs(self) -> None:
        world = World(self)
        world.api.add_run(run(80, path=E2E, sha=OLDER, created="2026-09-23T09:00:00Z"))
        world.api.add_run(pages_run(300, OLDER, "2026-09-23T10:30:00Z"))
        world.add(6201, grammar.cache_name(KEY, OLDER), "2026-09-23T10:55:00Z", run_id=300, head_sha=OLDER)
        world.add(6202, grammar.family_cache_name(FAMILY, KEY, OLDER), "2026-09-23T10:56:00Z", run_id=300,
                  head_sha=OLDER)
        # Never candidates: a failed Pages run's cache, a name a fork planted and a generation whose
        # head is outside the window (no source run of the history names it).
        world.api.add_run(pages_run(310, OLDER, "2026-09-23T11:30:00Z", conclusion="failure"))
        world.add(6203, grammar.cache_name(KEY, OLDER), "2026-09-23T11:55:00Z", run_id=310, head_sha=OLDER)
        world.api.add_run(run(320, path=".github/workflows/evil.yml", sha=OLDER, created="2026-09-23T12:00:00Z",
                              event="pull_request", head_repository={"full_name": "attacker/Quick-Skin-Mod"}))
        world.add(6204, grammar.family_cache_name(FAMILY, KEY, OLDER), "2026-09-23T12:10:00Z", run_id=320,
                  head_sha=OLDER)
        world.api.add_run(pages_run(290, OLDEST, "2026-09-22T10:30:00Z"))
        world.add(6205, grammar.cache_name(KEY, OLDEST), "2026-09-22T10:55:00Z", run_id=290, head_sha=OLDEST)
        summary = world.rotate()
        self.assertEqual([6001, 6002, 5090, 6201, 6202, 9100, FAMILY_SELECTED, 8801, SELECTED, *TRANSIENT_DELETIONS],
                         world.api.deleted_artifact_ids)
        self.assertEqual([6001, 5090, 6201, 8801, SELECTED], summary["deleted_artifact_ids"][KEY])
        self.assertEqual([6002, 6202, 9100, FAMILY_SELECTED], summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertEqual({}, summary["deferral_reasons"])
        self.assertTrue({6203, 6204, 6205} <= world.remaining())

    def test_a_new_first_key_leaves_the_previous_generation_to_the_leftover_phase(self) -> None:
        # R added mc1.19.4, which sorts first and so becomes the probe key, but the previous generation
        # never published it: the probe finds nothing and the last key's names find that generation.
        new = "mc1.19.4"
        world = World(self, keys=(KEY, new))
        world.change(16001, expired=True)
        summary = world.rotate()
        self.assertEqual([5090, 6001, 6002, 9100, FAMILY_SELECTED, 18801, SELECTED + 10000, 8801, SELECTED,
                          17001, 7001, 7002, 7300, 7100], world.api.deleted_artifact_ids)
        self.assertEqual({KEY: [5090, 6001, 8801, SELECTED], new: [18801, SELECTED + 10000]},
                         summary["deleted_artifact_ids"])
        self.assertEqual({}, summary["deferral_reasons"])

    def test_a_foreign_owner_of_a_leftover_name_rejects_only_the_leftover_group(self) -> None:
        world = World(self)
        world.api.add_run(run(80, path=E2E, sha=OLDER, created="2026-09-23T09:00:00Z"))
        world.api.add_run(run(420, path=".github/workflows/build-gate.yml", sha=OLDER, created="2026-09-23T08:00:00Z"))
        world.add(6030, grammar.cache_name(KEY, OLDER), "2026-09-23T08:10:00Z", run_id=420, head_sha=OLDER)
        summary = world.rotate()
        self.assertEqual(ALL_DELETIONS, world.api.deleted_artifact_ids)
        self.assertEqual([KEY], summary["deferred_branches"])
        self.assertIn("leftover cache retirement", summary["deferral_reasons"][KEY])
        self.assertIn(6030, world.remaining())
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS), summary["remaining_rotation_deletions"])


class ReobservationTests(unittest.TestCase):
    """BP leases: pinned reads and a fresh observation of R, the replacement and the candidate before
    every DELETE; QS: a changed replacement or head stops that scope with its partial ids."""

    def rotate_changing_after_first_delete(self, world: World, change: Callable[[], None],
                                           after: int | None = None) -> dict[str, Any]:
        """Rotate, calling ``change`` once right after the first DELETE (of ``after`` if given)."""

        delete = world.api.delete
        state = {"changed": False}

        def deleting(path: str) -> None:
            delete(path)
            if not state["changed"] and (after is None or path.endswith(f"/{after}")):
                state["changed"] = True
                change()

        with mock.patch.object(world.api, "delete", side_effect=deleting):
            return world.rotate()

    def test_a_replacement_gone_after_a_deletion_stops_every_scope_it_authorizes(self) -> None:
        world = World(self)
        delete = world.api.delete
        summary = self.rotate_changing_after_first_delete(
            world, lambda: delete(f"/repos/{REPOSITORY}/actions/artifacts/7200"))
        self.assertEqual({KEY: [6001]}, summary["deleted_artifact_ids"])
        self.assertIn("replacement mb-cache", summary["deferral_reasons"][KEY])
        self.assertIn("disappeared", summary["deferral_reasons"][KEY])
        self.assertEqual(FAMILY_DELETIONS, summary["deleted_compatibility_artifact_ids"][SCOPE])
        self.assertEqual([], summary["deleted_pages_run_artifact_ids"])
        self.assertTrue({8801, SELECTED, 5090, 7001, 7300, 7100} <= world.remaining())

    def test_the_promotion_survives_a_replacement_that_changes_during_the_transients(self) -> None:
        # The family leg's replacement changes after its collected artifact was retired: github-pages
        # and the promotion yield to every replacement, so both are kept for a retry.
        world = World(self)
        summary = self.rotate_changing_after_first_delete(world, lambda: world.change(7400, size_in_bytes=99),
                                                          after=7002)
        self.assertEqual([7001, 7002], summary["deleted_pages_run_artifact_ids"])
        self.assertTrue(summary["pages_run_deferred"])
        self.assertIn("changed before its retirement", summary["pages_run_deferral_reason"])
        self.assertTrue({7300, 7100} <= world.remaining())

    def test_a_changed_replacement_stops_its_scope_before_charging_the_budget(self) -> None:
        world = World(self)
        summary = self.rotate_changing_after_first_delete(world, lambda: world.change(7200, size_in_bytes=99))
        self.assertEqual({KEY: [6001]}, summary["deleted_artifact_ids"])
        self.assertIn("changed before its retirement", summary["deferral_reasons"][KEY])
        self.assertEqual(FAMILY_DELETIONS, summary["deleted_compatibility_artifact_ids"][SCOPE])
        # The key's collected transient is authorized by the same replacement, so the transients stop
        # before their first deletion (and before charging it).
        self.assertEqual([], summary["deleted_pages_run_artifact_ids"])
        self.assertTrue(summary["pages_run_deferred"])
        self.assertIn("pages-run transient retirement", summary["pages_run_deferral_reason"])
        self.assertEqual(limits.DELETION_BUDGET - 1 - 3, summary["remaining_rotation_deletions"])

    def test_a_changed_candidate_is_charged_and_never_deleted(self) -> None:
        world = World(self)
        # The handoff group is planned (8801 and SELECTED pinned) before 8801's deletion changes SELECTED.
        summary = self.rotate_changing_after_first_delete(
            world, lambda: world.change(SELECTED, size_in_bytes=99), after=8801)
        self.assertEqual({KEY: [6001, 5090, 8801]}, summary["deleted_artifact_ids"])
        self.assertIn(SELECTED, world.remaining())
        self.assertIn("handoff retirement", summary["deferral_reasons"][KEY])
        self.assertIn("changed before its retirement", summary["deferral_reasons"][KEY])
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS), summary["remaining_rotation_deletions"])

    def test_an_owner_rerun_stops_every_later_retirement(self) -> None:
        world = World(self)
        summary = self.rotate_changing_after_first_delete(
            world, lambda: world.api.add_run(owner_run(run_attempt=2, status="in_progress", conclusion=None)))
        self.assertEqual([6001], world.api.deleted_artifact_ids)
        self.assertEqual([KEY], summary["deferred_branches"])
        self.assertEqual([SCOPE], summary["compatibility_deferred_branches"])
        self.assertTrue(summary["pages_run_deferred"])

    def test_a_changed_candidate_owner_defers_its_family_before_any_deletion(self) -> None:
        world = World(self)
        runs_read = world.api.get_json
        observed = {"count": 0}

        def reads(path: str, *, params: Any = None) -> Any:
            value = runs_read(path, params=params)
            if path == f"/repos/{REPOSITORY}/actions/runs/{PREVIOUS}":
                observed["count"] += 1
                if observed["count"] > 1:
                    return {**value, "conclusion": "failure"}
            return value

        with mock.patch.object(world.api, "get_json", side_effect=reads):
            summary = world.rotate()
        self.assertNotIn(6001, world.api.deleted_artifact_ids)
        self.assertIn("cache retirement", summary["deferral_reasons"][KEY])
        self.assertIn(8801, world.api.deleted_artifact_ids)

    def test_a_candidate_gone_before_its_delete_is_charged_but_not_reported(self) -> None:
        world = World(self)
        original = world.api.get_json

        def reads(path: str, *, params: Any = None) -> Any:
            if path == f"/repos/{REPOSITORY}/actions/artifacts/8801" and world.api.deleted_artifact_ids:
                raise ApiNotFound("HTTP 404", status=404, method="GET", path=path)
            return original(path, params=params)

        with mock.patch.object(world.api, "get_json", side_effect=reads):
            summary = world.rotate()
        self.assertEqual([6001, 5090, SELECTED], summary["deleted_artifact_ids"][KEY])
        self.assertNotIn(8801, summary["planned_artifact_ids"])
        self.assertEqual(limits.DELETION_BUDGET - len(ALL_DELETIONS), summary["remaining_rotation_deletions"])

    def test_listings_that_disagree_about_one_artifact_reject_the_plan(self) -> None:
        world = World(self)
        record, archive = world.seeded[6001]
        listed = {**record, "size_in_bytes": len(archive) + 1, "digest": "sha256:" + sha(archive)}
        world.api.add_response(f"/repos/{REPOSITORY}/actions/artifacts", {"total_count": 1, "artifacts": [listed]},
                               params={"name": grammar.cache_name(KEY, OLD), "per_page": 100, "page": 1})
        summary = world.rotate()
        self.assertIn("changed while rotation was planning", summary["deferral_reasons"][KEY])
        self.assertIn(6001, world.remaining())
        self.assertIn(8801, world.api.deleted_artifact_ids)

    def test_one_deferred_key_does_not_abort_the_remaining_keys(self) -> None:
        world = World(self, keys=(KEY, SECOND_KEY))
        summary = self.rotate_changing_after_first_delete(world, lambda: world.change(7200, size_in_bytes=99))
        self.assertEqual([6001], summary["deleted_artifact_ids"][KEY])
        self.assertEqual([16001, 18801, SELECTED + 10000], summary["deleted_artifact_ids"][SECOND_KEY])
        self.assertEqual([KEY], summary["deferred_branches"])

    def test_the_guard_refuses_each_forbidden_artifact_for_exactly_one_reason(self) -> None:
        world = World(self)
        world.add(6050, "pages-cache-mc1.20.1", "2026-09-24T10:57:00Z", run_id=PREVIOUS, head_sha=OLD)
        with tempfile.TemporaryDirectory() as scratch:
            plan = rotation._Rotation(world.invocation(), world.api, owner_run_id=OWNER, owner_sha=COMMIT,
                                      delete_delay_seconds=0.0, dry_run=True, now=datetime.now(timezone.utc),
                                      sleep=world.sleeps.append, workdir=Path(scratch))
            plan.authenticate_owner()
            plan.load_promotion()
            plan.load_replacements()
            reads = plan.reads

            def guard(artifact: Any, transient: bool = False) -> None:
                plan._guard(artifact, rotation._Candidate(artifact, transient=transient))

            # Allowed: an older superseded cache, an older anchor, R's own transient.
            for artifact_id, transient in ((6001, False), (5090, False), (7300, True)):
                guard(reads.artifact(artifact_id), transient)
            # Created before T_R, so only the owner run condition is left.
            early_collected = dataclasses.replace(reads.artifact(7001), created_at="2026-09-25T10:00:00Z")
            cases = {
                "replacement (protected)": (reads.artifact(7200), False),
                "baseline created before R": (reads.artifact(6003), False),
                "non-kit name created before R": (reads.artifact(6050), False),
                "newer than R": (reads.artifact(8802), False),
                "R's own artifact as a predecessor": (early_collected, False),
                "another run's transient": (reads.artifact(6004), True),
                "R's own non-transient kind": (dataclasses.replace(reads.artifact(6001), run_id=OWNER), True),
            }
            for label, (artifact, transient) in cases.items():
                with self.subTest(label=label), self.assertRaisesRegex(MbError, "refusing"):
                    guard(artifact, transient)
            plan.protected.add(5090)
            with self.assertRaisesRegex(MbError, "refusing to retire protected"):
                guard(reads.artifact(5090))


class CommandTests(unittest.TestCase):
    def run_command(self, world: World, *extra: str) -> tuple[int, bytes, list[bool]]:
        clients: list[bool] = []

        def client(environ: Any, *, writable: bool = False, max_requests: Any = None) -> FakeGitHub:
            clients.append(writable)
            return world.api

        stdout = SimpleNamespace(buffer=io.BytesIO())
        environment = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": str(ROTATION_RUN), "GH_TOKEN": "t"}
        with mock.patch.object(github_api, "from_environment", side_effect=client), \
                mock.patch.object(cli, "environ", return_value=environment), \
                mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", io.StringIO()):
            code = cli.main(["rotate", "--repo", str(world.repo), "--owner-run-id", str(OWNER), "--owner-sha", COMMIT,
                             "--delete-delay-seconds", "0", *extra])
        return code, stdout.buffer.getvalue(), clients

    def test_the_command_prints_the_canonical_summary(self) -> None:
        world = World(self)
        code, output, clients = self.run_command(world)
        self.assertEqual((0, [True]), (code, clients))
        summary = strict_loads(output, label="summary", max_bytes=1 << 20)
        self.assertEqual(canonical_json(summary), output)
        # The command runs at the real clock: whether anchor 5090's successor is already past the
        # (zero-day) grace depends on the time of day, everything else does not.
        self.assertEqual([item for item in ALL_DELETIONS if item != 5090],
                         [item for item in summary["planned_artifact_ids"] if item != 5090])
        self.assertEqual(summary["planned_artifact_ids"], world.api.deleted_artifact_ids)

    def test_a_dry_run_uses_a_read_only_client(self) -> None:
        world = World(self, writable=False)
        code, output, clients = self.run_command(world, "--dry-run")
        self.assertEqual((0, [False]), (code, clients))
        self.assertTrue(strict_loads(output, label="summary", max_bytes=1 << 20)["dry_run"])
        self.assertEqual([], world.api.deleted_artifact_ids)

    def test_flags_are_validated(self) -> None:
        world = World(self)
        for extra in (["--owner-sha", "main"], ["--delete-delay-seconds", "-1"]):
            with self.subTest(extra=extra):
                code, _, clients = self.run_command(world, *extra)
                self.assertEqual((2, []), (code, clients))


if __name__ == "__main__":
    unittest.main()
