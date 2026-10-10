"""Loader of the immutable native parity fixtures (``tests/fixtures/ci_native``).

The fixtures hold data of the two consumer commits ``docs/BUILD-E2E-DESIGN.md`` reviewed, one folder
per Build profile:

* ``release-matrix.json`` and ``scenario-contract.json`` (and Quick Skin's ``gradle.properties``,
  which holds its mod version): the mod's own bytes at that commit;
* ``targets.json``: the targets, lanes, output paths, runtime row ids and pull-request scenarios the
  mod's own code derives from those bytes today;
* ``outputs.json``: the file names its staging writes for one target;
* ``jobs.json``: the Jobs API listings of the Build and Packaged E2E runs of the pull request that
  became the reviewed commit;
* ``measured.json``: sizes and file counts of real Build bundles and runtime artifacts.

``manifest.json`` names the repository, commit and tree of each profile, the path and Git blob id of
every native file, how every other file was derived, and the SHA-256 and size of all of them.
:func:`read` checks that record before it returns bytes, so a test that loads a fixture through this
module never sees edited data. ``tests/test_ci_native.py`` proves the set self-consistent.

Other units' tests may import this module (``from tests import ci_native``).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from mod_base.model import limits
from mod_base.model.canonical import strict_loads

ROOT = Path(__file__).resolve().parent / "fixtures" / "ci_native"
MANIFEST = "manifest.json"
#: Every fixture file is far smaller; the bound only keeps a read from following a wrong path.
MAX_FILE_BYTES = limits.MIB


def manifest(root: Path = ROOT) -> dict[str, Any]:
    """The decoded ``manifest.json``: ``{"profiles": {profile: {repository, commit, tree, native,
    derived}}}``."""

    return strict_loads((root / MANIFEST).read_bytes(), label=MANIFEST, max_bytes=MAX_FILE_BYTES)


def profiles(root: Path = ROOT) -> tuple[str, ...]:
    """The profiles that have fixtures, sorted."""

    return tuple(sorted(manifest(root)["profiles"]))


def records(profile: str, root: Path = ROOT) -> dict[str, dict[str, Any]]:
    """``{file name: manifest record}`` of one profile, native and derived files together."""

    entry = manifest(root)["profiles"][profile]
    return {**entry["native"], **entry["derived"]}


def read(profile: str, name: str, root: Path = ROOT) -> bytes:
    """The bytes of one fixture file, after checking them against the manifest."""

    record = records(profile, root)[name]
    data = (root / profile / name).read_bytes()
    if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        raise AssertionError(f"native fixture {profile}/{name} differs from its manifest record")
    return data


def load(profile: str, name: str, root: Path = ROOT) -> Any:
    """One JSON fixture file, strictly decoded from its checked bytes."""

    return strict_loads(read(profile, name, root), label=f"{profile}/{name}", max_bytes=MAX_FILE_BYTES)


def staged_names(lane: dict[str, Any]) -> list[str]:
    """Where both mods stage a lane's two JARs: ``files/`` and ``harness/`` plus the JAR's own name."""

    return ["files/" + lane["production_jar"].rsplit("/", 1)[1], "harness/" + lane["harness_jar"].rsplit("/", 1)[1]]


def output_paths(profile: str, root: Path = ROOT) -> list[str]:
    """Every real Build output path of a profile, sorted: the repository-relative Gradle outputs and
    the staged names of every lane, and the manifest (and SBOM) names of its staging."""

    paths = set()
    for lane in load(profile, "targets.json", root)["lanes"]:
        paths.update((lane["production_jar"], lane["harness_jar"], *staged_names(lane)))
    for unit in load(profile, "outputs.json", root)["units"]:
        paths.update(name for name in unit["staged"] if not name.startswith(("files/", "harness/")))
    return sorted(paths)
