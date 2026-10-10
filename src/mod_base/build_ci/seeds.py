"""Protected seeds of a candidate's private Gradle home: their keys and their export (MB11).

A *seed* is a copy of the ``caches/`` and ``wrapper/`` of a candidate's Gradle user home, kept in
the Actions cache of the mod's default branch so that the next job of the same unit need not
download the Gradle distribution, Minecraft and the loader artifacts again. The protected Build
config enables it per kind (``seeds.gradle`` for target jobs, ``seeds.runtime`` for lane jobs,
which keep what they install for a lane below the same Gradle home) and names the protected files
its key binds. A seed only ever saves time: restoring none, or one that is stale, gives the same
result, because the candidate's build verifies what it uses.

Only protected code keys a seed and only a protected job saves one:

* :func:`seed_key` is derived by the runner from the protected checkout of the default branch the
  prologue verified (never from the candidate), the kind, the planned unit and a format literal;
* the workflow restores the exact key, never a prefix, and passes the restored directory to
  ``ci worker-stage --gradle-seed``, whose root operation admits it as data;
* :func:`export_seed` runs only for a protected subject (a push or a dispatch on the default
  branch, whose candidate is the default branch itself), after its candidate hook succeeded and was
  sealed and its results uploaded: root copies the seed roots of the locked candidate's home into
  ``seed-export/`` (``gradle_cache.export_privileged_gradle_seed``) and the runner copies that into
  the directory the workflow saves. A pull request's job never exports and never saves.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mod_base.build_ci import adapter, lifecycle
from mod_base.build_ci.config import SEED_KINDS, protected_file_state, seed_key_files
from mod_base.build_ci.gradle_cache import _BOUNDS as SEED_BOUNDS, SEED_EXPORT_ROOT
from mod_base.build_ci.host import authenticate_host_boundary
from mod_base.build_ci.inputs import _layout
from mod_base.build_ci.root_request import request_seed_export
from mod_base.errors import MbError, single_line
from mod_base.io.atomic_directory import atomic_directory
from mod_base.io.tree import authenticate_tree_private_access, copy_regular_data_files, regular_data_records
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, sha256_hex
from mod_base.model.validators import check

#: Seed kind -> the candidate hook of the jobs that restore and save it.
SEED_HOOKS = {"gradle": "build_target", "runtime": "run_lane"}
#: The format of a seed and of its key. A change of what a seed holds or how it is staged changes it,
#: so no job restores a seed of another format.
SEED_FORMAT = "mb-seed-v1"
#: The one runner image every Build/E2E job runs on: a seed holds what was built and installed there.
SEED_RUNNER = "ubuntu-24.04-x64"


def seed_key(repo_root: Path, document: dict[str, Any], kind: str, unit_id: str) -> str | None:
    """The cache key of the seed ``kind`` of planned unit ``unit_id``, or ``None`` when the
    validated protected config ``document`` enables no such seed.

    ``repo_root`` is the protected checkout of the default branch. The key is
    ``<format>-<kind>-<unit>-<sha256>``, the SHA-256 of the canonical JSON of the format, the
    runner image, the kind, the unit and the state of every key file (the SHA-256 of its bytes, or
    ``null`` for a file the checkout does not have). Equal protected files give equal keys across
    commits; a changed one gives a new key, so an outdated seed is never restored for it.
    """

    check(kind in SEED_KINDS, "$.kind", "is not a seed kind")
    grammar.require(grammar.CI_UNIT_ID, unit_id, "seed unit")
    paths = seed_key_files(document, kind)
    if paths is None:
        return None
    files = [protected_file_state(repo_root, path, max_bytes=limits.MAX_CI_SEED_KEY_FILE_BYTES) for path in paths]
    material = {"format": SEED_FORMAT, "runner": SEED_RUNNER, "kind": kind, "unit": unit_id,
                "files": [{"path": file.path, "sha256": file.sha256} for file in files]}
    return f"{SEED_FORMAT}-{kind}-{unit_id}-{sha256_hex(canonical_json(material))}"


def derive_seed_key(job: lifecycle.Job, repo_root: Path, kind: str, unit_id: str) -> str | None:
    """``ci seed-key``: the key of this job's seed, after ``ci plan``. The unit must be one the
    job's plan holds for the kind's hook (a target for ``gradle``, a lane for ``runtime``) and the
    job one of the producer that runs that hook. Nothing is read from the API."""

    check(kind in SEED_HOOKS, "$.kind", "is not a seed kind")
    hook = SEED_HOOKS[kind]
    check(job.record["producer"] == lifecycle.HOOK_PRODUCERS[hook], "$.kind",
          f"a {kind} seed belongs to a {lifecycle.HOOK_PRODUCERS[hook]} job")
    adapter.plan_unit(lifecycle.read_plan(job), hook, unit_id)
    return seed_key(repo_root, job.config.data, kind, unit_id)


def export_seed(job: lifecycle.Job, worker: lifecycle.Worker, kind: str, output: Path, *,
                log: Callable[[str], object]) -> list[dict[str, Any]] | None:
    """``ci seed-export``: write the locked candidate's seed roots to the new directory ``output``.

    Only for a protected subject, whose candidate is the default branch itself: a pull request's
    candidate never makes a seed. The job's config must enable the seed ``kind``, and its candidate
    hook must be the kind's hook, have succeeded and been sealed; anything else is a rejection.
    ``output`` must not exist (a restore that failed half-way can leave it). Root then copies
    ``caches/`` and ``wrapper/`` of the candidate's Gradle home into ``seed-export/`` and hands the
    copy to the runner (``export-seed``); the runner checks it is private to it and copies it into
    ``output``, which appears complete or not at all.

    Returns the inventory of ``output``; an empty one, with no ``output``, when the candidate left
    nothing to seed. A seed only saves time, and the job's results are uploaded by now: so an
    existing ``output``, a home that cannot be exported (a link, a special file, a file with
    several links, more than the seed bounds) or any other failure from here on declines the seed
    instead of failing the job. The reason goes to ``log`` and the result is ``None``. Every
    account ends terminated and locked.
    """

    output = Path(os.path.abspath(output))
    check(kind in SEED_HOOKS, "$.kind", "is not a seed kind")
    if job.subject["pr_number"]:
        raise lifecycle.LifecycleError("only a protected subject exports a seed; a pull request never does")
    if seed_key_files(job.config.data, kind) is None:
        raise lifecycle.LifecycleError(f"the protected Build config enables no {kind} seed")
    if os.path.lexists(str(SEED_EXPORT_ROOT)):
        raise lifecycle.LifecycleError("this job exported its seed already; `ci seed-export` runs once")
    run = lifecycle.read_run(job)
    check(run["hook"] == SEED_HOOKS[kind] and run["succeeded"], "$.run",
          f"a {kind} seed follows a successful {SEED_HOOKS[kind]}")
    check(lifecycle.read_seal(job)["hook"] == run["hook"], "$.seal", "the candidate hook was sealed")
    candidate = worker.accounts.get("candidate")
    check(candidate is not None, "$.worker", "this job allocated no candidate account")
    execution = {key: run[key] for key in ("returncode", "truncated", "log_bytes", "log_sha256")}
    if os.path.lexists(output):  # What a restore that failed half-way left: never saved, never replaced.
        log(f"seed-export: no {kind} seed, {output.name} exists already\n")
        return None
    try:
        with lifecycle.resting(worker.accounts):
            lifecycle._root(job, worker.python, "export-seed", request_seed_export(
                boundary=worker.boundary, candidate=candidate, validator=worker.validator, execution=execution))
            authenticate_host_boundary(worker.boundary)
            _layout(worker.boundary)
            exported = Path(str(SEED_EXPORT_ROOT))
            authenticate_tree_private_access(exported, owner_uid=worker.boundary.uid,
                                             owner_gid=worker.boundary.gid, max_entries=SEED_BOUNDS["max_entries"])
            records = regular_data_records(exported, **SEED_BOUNDS)
            if records:
                def writer(stage: Path, stage_fd: int) -> None:
                    check(copy_regular_data_files(exported, stage_fd, **SEED_BOUNDS) == records, "$.seed",
                          "the exported seed changed while it was copied")

                atomic_directory(output, writer)
                check(regular_data_records(output, **SEED_BOUNDS) == records, "$.seed", "the copied seed differs")
    except MbError as error:
        log(f"seed-export: no {kind} seed, {single_line(error)}\n")
        return None
    return records
