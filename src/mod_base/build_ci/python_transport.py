"""Inert, pinned Python release admission and numeric-ID transport (MB11).

This authenticates publisher API identity and independently pinned archive bytes. It neither
publishes a cache nor executes the installer, and is not runtime/system-library enrollment.
"""

from __future__ import annotations

import io
from typing import Any

from mod_base.build_ci.python_archive import inspect_python_installer
from mod_base.build_ci.python_installation import _PROFILES
from mod_base.build_ci.worker import WorkerError
from mod_base.github.api import GitHubApi
from mod_base.model import limits


_REPOSITORY = "actions/python-versions"
_PREFIX = f"/repos/{_REPOSITORY}"
_PUBLISHERS = {
    "3.11.17": (401059204, 603458734, 36876195594, "eb7756b60e2add788a3f5c9dba83102f67e076bf"),
    "3.12.15": (400606086, 602286467, 36805895057, "96cf261124d1e3fbc49339879ac37f935c25653f"),
    "3.13.16": (400609198, 602296178, 36805956071, "96cf261124d1e3fbc49339879ac37f935c25653f"),
}


def _matches(value: Any, fields: dict[str, Any], label: str) -> dict[str, Any]:
    if type(value) is not dict or any(type(value.get(key)) is not type(expected)
                                     or value[key] != expected for key, expected in fields.items()):
        raise WorkerError(f"Python publisher {label} differs from the pinned profile")
    return value


def _authenticate(api: GitHubApi, version: str) -> None:
    release_id, asset_id, run_id, commit = _PUBLISHERS[version]
    size, digest = _PROFILES[version]
    tag = f"{version}-{run_id}"
    expected_asset = {"id": asset_id, "name": f"python-{version}-linux-24.04-x64.tar.gz",
                      "state": "uploaded", "size": size, "digest": f"sha256:{digest}"}
    release = _matches(api.get_json(f"{_PREFIX}/releases/{release_id}"),
                       {"id": release_id, "tag_name": tag, "draft": False, "prerelease": False}, "release")
    assets = release.get("assets")
    if type(assets) is not list or not 1 <= len(assets) <= limits.MAX_CI_PYTHON_RELEASE_ASSETS:
        raise WorkerError("Python publisher release assets are missing or exceed their bound")
    ids, names = set(), set()
    selected = None
    for asset in assets:
        if (type(asset) is not dict or type(asset.get("id")) is not int
                or not 1 <= asset["id"] <= limits.MAX_RUN_ID or type(asset.get("name")) is not str
                or not 1 <= len(asset["name"]) <= limits.MAX_ARTIFACT_NAME_BYTES
                or asset["id"] in ids or asset["name"] in names):
            raise WorkerError("Python publisher release assets have invalid or duplicate identities")
        ids.add(asset["id"])
        names.add(asset["name"])
        if asset["id"] == asset_id:
            selected = asset
    _matches(selected, expected_asset, "release asset membership")
    _matches(api.get_json(f"{_PREFIX}/releases/assets/{asset_id}"), expected_asset, "asset")
    ref = _matches(api.get_json(f"{_PREFIX}/git/ref/tags/{tag}"), {"ref": f"refs/tags/{tag}"}, "tag")
    _matches(ref.get("object"), {"type": "commit", "sha": commit}, "tag target")
    _matches(api.get_json(f"{_PREFIX}/git/commits/{commit}"), {"sha": commit}, "commit")
    run_fields = {"id": run_id, "run_attempt": 1, "event": "workflow_dispatch", "status": "completed",
                  "conclusion": "success", "path": ".github/workflows/build-python-packages.yml",
                  "head_branch": "main", "head_sha": commit}
    for path in (f"{_PREFIX}/actions/runs/{run_id}", f"{_PREFIX}/actions/runs/{run_id}/attempts/1"):
        run = _matches(api.get_json(path), run_fields, "producer attempt")
        for key in ("repository", "head_repository"):
            _matches(run.get(key), {"full_name": _REPOSITORY}, "producer repository")


def download_python_installer(api: GitHubApi, *, version: str) -> bytes:
    """Bracket inert archive admission with fixed release/tag/commit/producer checks.

    The protected caller must already admit this kit and its reviewed profile selection. The
    installation operation independently admits its private kit lock and stable local source.
    Returned bytes grant no interpreter execution or cache/installation authority.
    """

    if type(version) is not str or version not in _PUBLISHERS:
        raise WorkerError("Python installer version is outside the pinned publisher profiles")
    _authenticate(api, version)
    _, asset_id, _, _ = _PUBLISHERS[version]
    size, digest = _PROFILES[version]
    data = api.download_release_asset(_REPOSITORY, asset_id, max_bytes=size)
    if type(data) is not bytes:
        raise WorkerError("Python installer transport returned non-binary data")
    inspect_python_installer(io.BytesIO(data), expected_size=size, expected_digest=f"sha256:{digest}")
    _authenticate(api, version)
    return data
