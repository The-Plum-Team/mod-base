"""The publication state machine of one sealed release plan (docs/RELEASE-PROTOCOL.md).

Every effect follows the same order: read the ledger and the remote state, decide, write the
intent to the ledger, act, write the outcome. The ledger of a tag is the hidden block at the end of
its draft release's body (:mod:`mod_base.release.ledger`); it is the only state, so a stopped job is
resumed by running the same step again. A file is never sent twice: a row is ``uploading`` before
its upload starts, and an ``uploading`` row whose file no marketplace lists stays fenced for the
operator instead of being uploaded again, because a lost answer may hide an accepted upload.

Remote state is reconciled by content: a Modrinth version by the SHA-512 of its file, a CurseForge
file by its listing (under the name it was uploaded with or the one CurseForge stored) and a CDN
download hashed against the plan, a GitHub asset by its normalized name and a download hashed
against the plan. Only ``verified`` rows count; a draft release is published once every row of its
ledger is verified, and the plan's last tag is marked the repository's latest release.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mod_base.errors import MbError
from mod_base.marketplace import curseforge, github_releases, modrinth
from mod_base.marketplace.limits import MAX_MARKETPLACE_REQUESTS
from mod_base.marketplace.transport import (HttpsTransport, MarketplaceError, MarketplaceNotFound,
                                            MarketplaceOutcomeUnknown, MarketplaceUnavailable, PublicationPending,
                                            Reconciliation)
from mod_base.model.canonical import strict_loads
from mod_base.release import ledger as ledger_doc
from mod_base.release.plan import PLAN_NAME, admit_release, plan_digest, released_commit, validate_release_plan

#: The asset of every draft release that holds the plan it publishes, so the scheduled finish job
#: can verify a draft after the plan artifact has expired. The ledger binds its digest.
PLAN_ASSET = "mod-base-release-plan.json"
#: Uploads to one marketplace that run at once. Each is a multi-megabyte POST; four keep a release
#: of 34 files to a few minutes without tripping the marketplaces' rate limits.
UPLOAD_WORKERS = 4
#: How long ``verify --wait-seconds`` sleeps between two passes over the pending rows.
VERIFY_INTERVAL_SECONDS = 60
#: The longest ``--wait-seconds`` a publish job may ask for: the job's own timeout is an hour.
MAX_VERIFY_WAIT_SECONDS = 2400
MARKETPLACES = ("modrinth", "curseforge")
#: Requests one transport may spend, retries included: the marketplace package's own ceiling.
MAX_RELEASE_REQUESTS = MAX_MARKETPLACE_REQUESTS


class FencedUpload(MbError):
    """A row is ``uploading`` and no marketplace lists its file: the upload may or may not have been
    accepted, so only an operator who checked the marketplace may reset it."""


@dataclass(frozen=True)
class Services:
    """The transports of one step. A step receives only the clients its credential allows."""

    github: HttpsTransport
    github_uploads: HttpsTransport | None = None
    modrinth: HttpsTransport | None = None
    curseforge_upload: HttpsTransport | None = None
    curseforge_listing: HttpsTransport | None = None
    curseforge_cdn: HttpsTransport | None = None


@dataclass
class Release:
    """One tag of the plan and its draft release, with the lock that serializes its ledger writes."""

    tag: dict[str, Any]
    release: dict[str, Any]
    ledger: dict[str, Any]
    lock: threading.Lock = field(default_factory=threading.Lock)


def _lanes(tag: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The production asset of each lane of ``tag``. SBOMs (``lane`` null) go to GitHub only."""

    return {asset["lane"]: asset for asset in tag["assets"] if asset["lane"] is not None}


def _asset_path(plan_dir: Path, tag: dict[str, Any], asset: dict[str, Any]) -> Path:
    return plan_dir / "assets" / tag["tag"] / asset["filename"]


def _row(lane: str, marketplace: str) -> str:
    return ledger_doc.row_key(lane, marketplace)


def _marketplaces(plan: dict[str, Any]) -> tuple[str, ...]:
    """The marketplaces the plan publishes to, in :data:`MARKETPLACES` order."""

    return tuple(name for name in MARKETPLACES if any(name in lane for lane in plan["lanes"].values()))


# -- The ledger --------------------------------------------------------------------------------------


def _write_ledger(services: Services, repository: str, plan: dict[str, Any], entry: Release,
                  updated: dict[str, Any]) -> None:
    """Persist ``updated`` as ``entry``'s ledger. The body is read again first: a body that no
    longer holds the ledger this job last wrote means another writer, and the job stops."""

    current = github_releases.get_release(services.github, repository, entry.release["id"])
    if ledger_doc.parse_body(current.get("body") or "", plan=plan, tag=entry.tag["tag"]) != entry.ledger:
        raise MbError(f"the ledger of {entry.tag['tag']} changed under this job", reason="release")
    body = ledger_doc.render_body(plan, entry.tag["tag"], updated)
    entry.release = github_releases.update_body(services.github, repository, entry.release["id"], body,
                                                entry.tag["tag"])
    entry.ledger = updated


def _advance(services: Services, repository: str, plan: dict[str, Any], entry: Release, row: str,
             state: str, remote_id: str | None = None) -> None:
    with entry.lock:
        current = entry.ledger["rows"][row]
        if current["state"] == state and current["remote_id"] == remote_id:
            return
        _write_ledger(services, repository, plan, entry, ledger_doc.advance(entry.ledger, row, state, remote_id))


def _entry(plan: dict[str, Any], tag: dict[str, Any], release: dict[str, Any]) -> Release:
    recorded = ledger_doc.parse_body(release.get("body") or "", plan=plan, tag=tag["tag"])
    return Release(tag, release, recorded)


# -- Reconciliation ----------------------------------------------------------------------------------


class Reconciler:
    """One pass of reconciliation: each project's marketplace listing is read once per pass, not
    once per row (a CurseForge listing can take hundreds of requests)."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self._lock = threading.Lock()
        self._modrinth: dict[str, list[dict[str, Any]]] = {}
        self._curseforge: dict[int, list[dict[str, Any]]] = {}
        self._resolved: dict[str, str] = {}

    def modrinth_project(self, project: str) -> str:
        """The Modrinth id of a dependency named by id or slug, resolved once per pass."""

        with self._lock:
            if project not in self._resolved:
                self._resolved[project] = modrinth.resolve_project(self.services.modrinth, project)
            return self._resolved[project]

    def _modrinth_versions(self, project: str) -> list[dict[str, Any]]:
        with self._lock:
            if project not in self._modrinth:
                self._modrinth[project] = modrinth.project_versions(self.services.modrinth, project)
            return self._modrinth[project]

    def _curseforge_files(self, project: int) -> list[dict[str, Any]]:
        with self._lock:
            if project not in self._curseforge:
                self._curseforge[project] = curseforge.listing(self.services.curseforge_listing, project)
            return self._curseforge[project]

    def __call__(self, plan: dict[str, Any], lane: str, marketplace: str, asset: dict[str, Any]) -> Reconciliation:
        """Whether ``asset`` of ``lane`` is on ``marketplace``: ``publish`` true when it is absent.
        Raises :class:`PublicationPending` while the marketplace holds it in moderation."""

        metadata = plan["lanes"][lane][marketplace]
        if marketplace == "modrinth":
            remote = modrinth.version_by_sha512(self.services.modrinth, asset["sha512"])
            return modrinth.reconcile(remote, self._modrinth_versions(metadata["project_id"]),
                                      expected_sha512=asset["sha512"], size=asset["size"],
                                      project_id=metadata["project_id"], version_number=metadata["version_number"])

        def fetch(file_id: int, stored: str, limit: int) -> bytes:
            try:
                return curseforge.download(self.services.curseforge_cdn, file_id, stored, limit=limit)
            except MarketplaceError as error:
                # The CDN answers 403 or 404 for a while after CurseForge approves a file.
                if isinstance(error, MarketplaceNotFound) or error.status == 403:
                    raise PublicationPending(f"the CDN does not serve CurseForge file {file_id} yet",
                                             reason="release") from error
                raise

        return curseforge.reconcile(self._curseforge_files(metadata["project_id"]), filename=asset["filename"],
                                    size=asset["size"], sha1=asset["sha1"], sha256=asset["sha256"],
                                    project_id=metadata["project_id"], fetch_bytes=fetch)


# -- GitHub ------------------------------------------------------------------------------------------


def create_releases(services: Services, *, repository: str, plan_dir: Path) -> list[Release]:
    """Create every tag at the released commit and one draft release per tag holding its assets, the
    plan asset and a fresh ledger; resume what an earlier attempt created. A tag that exists at
    another commit, or an asset of the right name with other bytes, fails closed."""

    plan = admit_release(plan_dir)
    sha = released_commit(plan)
    plan_path = plan_dir / PLAN_NAME
    plan_bytes = plan_path.read_bytes()
    plan_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    entries: list[Release] = []
    for tag in plan["tags"]:
        # The tag itself is created only when its release is published (_publish_release), so a
        # release that fails before then leaves no tag behind; one that names another commit
        # already is refused before anything is created.
        existing = github_releases.tag_commit(services.github, repository, tag["tag"])
        if existing not in (None, sha):
            raise MbError(f"the tag {tag['tag']} already names commit {existing}, not {sha}", reason="release")
    for tag in plan["tags"]:
        release = github_releases.find_release(services.github, repository, tag["tag"])
        if release is None:
            body = ledger_doc.render_body(plan, tag["tag"], ledger_doc.new_ledger(plan, tag["tag"]))
            release = github_releases.create_draft(services.github, repository, tag=tag["tag"], name=tag["name"],
                                                   body=body, target_commitish=sha)
        entry = _entry(plan, tag, release)
        entries.append(entry)
        if not release.get("draft"):
            continue  # finished by an earlier attempt; a published release's assets are immutable
        local = {asset["filename"]: (asset["size"], asset["sha256"]) for asset in tag["assets"]}
        local[PLAN_ASSET] = (len(plan_bytes), plan_sha256)
        present = github_releases.bind_assets(services.github, repository, release, local)
        for asset in tag["assets"]:
            if asset["filename"] not in present:
                github_releases.upload_asset(services.github_uploads, repository, release["id"],
                                             _asset_path(plan_dir, tag, asset), asset["filename"],
                                             sha256=asset["sha256"])
        if PLAN_ASSET not in present:
            github_releases.upload_asset(services.github_uploads, repository, release["id"], plan_path, PLAN_ASSET,
                                         sha256=plan_sha256)
    return entries


def _open_releases(services: Services, repository: str, plan: dict[str, Any]) -> list[Release]:
    """The draft (or already published) release of every tag of ``plan`` with its ledger."""

    entries = []
    for tag in plan["tags"]:
        release = github_releases.find_release(services.github, repository, tag["tag"])
        if release is None:
            raise MbError(f"{tag['tag']} has no release yet; run `release github` first", reason="release")
        entries.append(_entry(plan, tag, release))
    return entries


# -- Uploads -----------------------------------------------------------------------------------------


def _curseforge_ids(names: dict[str, int], metadata: dict[str, Any]) -> tuple[int, ...]:
    wanted = (*metadata["game_versions"], *(curseforge.LOADER_NAMES[loader] for loader in metadata["loaders"]))
    missing = [name for name in wanted if name not in names]
    if missing:
        raise MbError(f"CurseForge knows no game version named {', '.join(missing)}", reason="release")
    return tuple(names[name] for name in wanted)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _upload_one(services: Services, plan: dict[str, Any], plan_dir: Path, entry: Release, lane: str,
                marketplace: str, repository: str, reconcile: Reconciler,
                game_versions: Callable[[], dict[str, int]]) -> str:
    """Bring one row as far as an upload can: ``verified`` or ``pending``. Returns the new state.

    Everything the upload needs (the file's digest, CurseForge's game version ids, Modrinth's
    dependency ids) is read and checked before the row's intent is written, so a refusal at that
    point leaves the row ``unstarted``. Once the intent is written, a definitive refusal of the
    upload itself puts the row back too; only an outcome that may hide an accepted upload, or a
    row sent before that no marketplace lists, stays fenced."""

    row = _row(lane, marketplace)
    asset = _lanes(entry.tag)[lane]
    recorded = entry.ledger["rows"][row]
    if recorded["state"] == "verified":
        return "verified"
    try:
        found = reconcile(plan, lane, marketplace, asset)
    except PublicationPending:
        # The marketplace holds this very file in moderation: it was sent, by this or an earlier job.
        if recorded["state"] == "unstarted":
            _advance(services, repository, plan, entry, row, "uploading")
        if entry.ledger["rows"][row]["state"] == "uploading":
            _advance(services, repository, plan, entry, row, "pending")
        return "pending"
    if not found.publish:
        _advance(services, repository, plan, entry, row, "verified", found.remote_id)
        return "verified"
    if recorded["state"] == "pending" and recorded["remote_id"] is not None:
        # Accepted with this id, not listed yet: a marketplace may hide a file until it is approved.
        return "pending"
    if recorded["state"] != "unstarted":
        raise FencedUpload(f"{row} was sent before and {marketplace} lists no such file; check the marketplace's "
                           "author dashboard, then reset the row only if it never accepted the file",
                           reason="release")
    path = _asset_path(plan_dir, entry.tag, asset)
    if _sha256_of(path) != asset["sha256"]:
        raise MbError(f"{path.name} is not the planned file", reason="release")
    metadata = plan["lanes"][lane][marketplace]
    if marketplace == "modrinth":
        request = modrinth.ModrinthVersion(
            project_id=metadata["project_id"], version_number=metadata["version_number"], name=metadata["name"],
            changelog=plan["changelog"]["text"], game_versions=tuple(metadata["game_versions"]),
            loaders=tuple(metadata["loaders"]), version_type=metadata["version_type"],
            dependencies=tuple((reconcile.modrinth_project(item["project_id"]), item["dependency_type"])
                               for item in metadata["dependencies"]),
            featured=False)

        def send() -> str:
            return str(modrinth.create_version(services.modrinth, request, path, asset["filename"],
                                               sha256=asset["sha256"])["id"])
    else:
        request = curseforge.CurseForgeFile(
            project_id=metadata["project_id"], display_name=metadata["display_name"],
            changelog=plan["changelog"]["text"], release_type=metadata["release_type"],
            game_version_ids=_curseforge_ids(game_versions(), metadata),
            relations=tuple((item["slug"], item["type"]) for item in metadata["relations"]))

        def send() -> str:
            return str(curseforge.upload(services.curseforge_upload, request, path, asset["filename"],
                                         sha256=asset["sha256"]))
    _advance(services, repository, plan, entry, row, "uploading")
    try:
        remote_id = send()
    except MarketplaceOutcomeUnknown:
        raise  # the row stays fenced: the marketplace may have accepted the file
    except (MarketplaceError, MarketplaceUnavailable):
        # A definitive refusal, or a read that failed before the upload was sent: nothing was accepted.
        _advance(services, repository, plan, entry, row, "unstarted")
        raise
    _advance(services, repository, plan, entry, row, "pending", remote_id)
    return "pending"


def upload(services: Services, *, repository: str, plan_dir: Path, marketplace: str) -> dict[str, str]:
    """Upload every lane of the plan to ``marketplace``, at most :data:`UPLOAD_WORKERS` at once, and
    return each row's state. Every row is attempted; the first failure is raised after the others
    finished, so one refused file does not hold back the rest of the release."""

    if marketplace not in MARKETPLACES:
        raise MbError(f"unknown marketplace {marketplace!r}", reason="usage")
    plan = admit_release(plan_dir)
    if marketplace not in _marketplaces(plan):
        return {}
    entries = _open_releases(services, repository, plan)
    reconcile = Reconciler(services)
    names: dict[str, int] = {}
    names_lock = threading.Lock()

    def game_versions() -> dict[str, int]:
        with names_lock:
            if not names:
                names.update(curseforge.game_version_ids(services.curseforge_upload))
            return names

    jobs = [(entry, lane) for entry in entries if entry.release.get("draft") for lane in entry.tag["lanes"]
            if marketplace in plan["lanes"][lane]]
    states: dict[str, str] = {}
    errors: list[BaseException] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=UPLOAD_WORKERS) as pool:
        futures = {pool.submit(_upload_one, services, plan, plan_dir, entry, lane, marketplace, repository,
                               reconcile, game_versions): _row(lane, marketplace) for entry, lane in jobs}
        for future in concurrent.futures.as_completed(futures):
            try:
                states[futures[future]] = future.result()
            except BaseException as error:  # every row is attempted before the first failure is raised
                states[futures[future]] = "failed"
                errors.append(error)
    if errors:
        raise errors[0]
    return dict(sorted(states.items()))


# -- Verification ------------------------------------------------------------------------------------


_TRAILING_VERSION = re.compile(r"(?:^|[-_])v?(\d{1,6}(?:\.\d{1,6}){1,3})$")


def _version_key(text: str) -> tuple[int, ...] | None:
    """The numeric version a tag ends with (``mc26.3-v4.0.0`` -> ``(4, 0, 0)``), or ``None``."""

    match = _TRAILING_VERSION.search(text)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _minecraft_key(tag: dict[str, Any]) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", tag.get("minecraft") or ""))


def _latest_tag(services: Services, repository: str, plan: dict[str, Any]) -> str | None:
    """The tag of the plan to mark as the repository's latest release: the one of the highest
    Minecraft version, and only when no release marked latest has a higher version than the plan's
    (a release finished late by the schedule must not hide a newer one)."""

    preferred = max(plan["tags"], key=_minecraft_key)["tag"]
    current = github_releases.latest_release(services.github, repository)
    if current is None:
        return preferred
    ours, theirs = _version_key(plan["version"]), _version_key(str(current.get("tag_name") or ""))
    if ours is None or theirs is None:
        return preferred if current.get("tag_name") in {tag["tag"] for tag in plan["tags"]} else None
    return preferred if ours >= theirs else None


def _publish_release(services: Services, repository: str, plan: dict[str, Any], entry: Release) -> None:
    """Create ``entry``'s tag at the released commit and publish its draft."""

    github_releases.create_ref(services.github, repository, entry.tag["tag"], released_commit(plan))
    latest = _latest_tag(services, repository, plan) == entry.tag["tag"]
    entry.release = github_releases.publish(services.github, repository, entry.release["id"], entry.tag["tag"],
                                            make_latest=latest)


def _verify_entries(services: Services, repository: str, plan: dict[str, Any], entries: Iterable[Release],
                    errors: list[BaseException]) -> bool:
    """One pass: reconcile every row that is not verified yet and publish each complete draft.
    A row or a draft that fails is recorded in ``errors`` and the others go on. Returns whether
    every release of ``entries`` is published."""

    reconcile = Reconciler(services)
    published = True
    for entry in entries:
        if not entry.release.get("draft"):
            continue
        for row, value in sorted(entry.ledger["rows"].items()):
            if value["state"] == "verified":
                continue
            lane, marketplace = row.rsplit("--", 1)
            try:
                found = reconcile(plan, lane, marketplace, _lanes(entry.tag)[lane])
                if not found.publish:
                    _advance(services, repository, plan, entry, row, "verified", found.remote_id)
            except PublicationPending:
                continue
            except MbError as error:
                errors.append(error)
                print(f"{row}: {error}", file=sys.stderr)
        try:
            if ledger_doc.complete(entry.ledger):
                _publish_release(services, repository, plan, entry)
            else:
                published = False
        except MbError as error:
            published = False
            errors.append(error)
            print(f"{entry.tag['tag']}: {error}", file=sys.stderr)
    return published


def verify(services: Services, *, repository: str, plan_dir: Path, wait_seconds: int,
           sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """Reconcile and publish the plan's releases, waiting at most ``wait_seconds`` for moderation.
    Returns whether every release is published; what is still pending is left to the schedule."""

    if type(wait_seconds) is not int or not 0 <= wait_seconds <= MAX_VERIFY_WAIT_SECONDS:
        raise MbError(f"--wait-seconds must be 0..{MAX_VERIFY_WAIT_SECONDS}", reason="usage")
    plan = admit_release(plan_dir)
    deadline = clock() + wait_seconds
    while True:
        errors: list[BaseException] = []
        done = _verify_entries(services, repository, plan, _open_releases(services, repository, plan), errors)
        if errors:
            raise errors[0]
        if done:
            return True
        if clock() + VERIFY_INTERVAL_SECONDS > deadline:
            return False
        sleep(VERIFY_INTERVAL_SECONDS)


def finish_drafts(services: Services, *, repository: str) -> dict[str, bool]:
    """The scheduled finish: for every draft release whose body carries a ledger, read its plan from
    the draft's :data:`PLAN_ASSET`, require the ledger to bind that plan's digest, reconcile and
    publish what is complete. Returns tag -> whether it is published now. A draft that cannot be
    read or finished does not hold back the others; the first such error is raised at the end."""

    outcome: dict[str, bool] = {}
    errors: list[BaseException] = []
    for release in github_releases.draft_releases(services.github, repository):
        body = release.get("body") or ""
        if ledger_doc.BLOCK_START not in body:
            continue
        tag_name = str(release.get("tag_name"))
        try:
            outcome[tag_name] = _finish_draft(services, repository, release, body, errors)
        except MbError as error:
            outcome[tag_name] = False
            errors.append(error)
            print(f"{tag_name}: {error}", file=sys.stderr)
    if errors:
        raise errors[0]
    return outcome


def _finish_draft(services: Services, repository: str, release: dict[str, Any], body: str,
                  errors: list[BaseException]) -> bool:
    tag_name = release.get("tag_name")
    recorded = ledger_doc.parse_body(body)
    asset = next((item for item in release.get("assets") or ()
                  if item.get("name") == github_releases.asset_name(PLAN_ASSET)), None)
    if asset is None:
        raise MbError(f"the draft of {tag_name!r} has a ledger but no plan asset yet; dispatch the release "
                      "again with publish to finish creating it", reason="release")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= ledger_doc.MAX_PLAN_ASSET_BYTES:
        raise MbError(f"the plan asset of {tag_name!r} has an impossible size", reason="release")
    data = github_releases.asset_bytes(services.github, repository, asset["id"], limit=size)
    plan = validate_release_plan(strict_loads(data, label=PLAN_ASSET, max_bytes=ledger_doc.MAX_PLAN_ASSET_BYTES))
    if plan_digest(plan) != recorded["plan_sha256"]:
        raise MbError(f"the plan asset of {tag_name!r} is not the plan its ledger names", reason="release")
    tag = next((item for item in plan["tags"] if item["tag"] == tag_name), None)
    if tag is None:
        raise MbError(f"the plan asset of {tag_name!r} names no such tag", reason="release")
    return _verify_entries(services, repository, plan, [_entry(plan, tag, release)], errors)
