"""``mod-base.family.paired`` validation, image re-inspection and ``family collect`` (MB4).

R4: the envelope inventory is validated first, then the projection returned by the adapter's
``family_validate`` against ``documents.validate_family_paired`` (with the configured family image
policy); every image is re-inspected (WebP decode, dimensions equal to
``thumbnail(recorded source, derivative_box)``, recorded pixel metrics equal to the kit's); every
unclean pair is rejected; ``projection.coverage_sha`` must equal ``expected_coverage_sha``.
R5: when ``carried_from`` is set the core fetches both commits as inert objects and requires
``git merge-base --is-ancestor carried_from expected_coverage_sha``.

Layout of an available ``family collect`` output, uploaded as ``mb-collected-family--<family>--<key>``:

* ``paired.json`` (:data:`PROJECTION_NAME`): the validated ``mod-base.family.paired`` projection;
* ``images/<sha256>.webp`` (:data:`IMAGES_DIRECTORY`): its images;
* ``source/`` (:data:`SOURCE_DIRECTORY`): the selected family handoff or cache copied verbatim
  (``envelope.json`` plus the native bundle, exactly its envelope inventory).

``build`` renders ``paired.json`` and ``images/``; finalize's ``refresh-family`` re-validates the
whole artifact and rolls exactly ``source/`` forward as ``mb-family-cache--<family>--<key>--<coverage_sha>``,
so every family cache stays re-collectable by ``family collect`` (carry-forward is re-proven, R5,
at every collection).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB4"
PROJECTION_NAME = "paired.json"
IMAGES_DIRECTORY = "images"
SOURCE_DIRECTORY = "source"


@dataclass(frozen=True)
class FamilyOutcome:
    """``status`` is ``available``, ``superseded`` or ``unavailable``; ``projection`` is set only
    when available."""

    status: str
    reason: str
    projection: dict[str, Any] | None = None
    carried_from: str | None = None


def validate_projection(invocation: Invocation, projection_path: Path, *, images_root: Path, family: str, key: str,
                        expected_coverage_sha: str) -> dict[str, Any]:
    """R4 for one written projection; returns the validated projection."""

    raise NotImplementedError("owned by MB4")


def verify_carry_forward(repo_root: Path, carried_from: str, coverage_sha: str) -> None:
    """R5: both commits are present as inert objects and ``carried_from`` is an ancestor of
    ``coverage_sha`` (``git merge-base --is-ancestor`` in a sanitized environment)."""

    raise NotImplementedError("owned by MB4")


def collect_family(invocation: Invocation, *, family: str, key: str, input_dir: Path, expected_coverage_sha: str,
                   output: Path) -> FamilyOutcome:
    """The ``family collect`` command: validate the envelope, run ``family_validate``, apply R4/R5
    and, when available, write the collected layout (module docstring) into the new ``output``.
    ``superseded`` and ``unavailable`` are returned (the command exits 3 without writing an
    upload)."""

    raise NotImplementedError("owned by MB4")
