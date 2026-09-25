"""Handoff/compact validation in a fresh process (MB3), including BP ``validate_raw``.

Every validation re-reads the bundle from disk: exact directory inventory versus ``files``, every
hash and size, strict JSON, ``documents.validate_*`` with the embedded expectation, every image
re-inspected with the kit (source PNGs against ``image_policy.source_size``, derivatives against
``thumbnail_size``), comparisons recomputed, and for handoffs the adapter ``collect`` re-derivation
(R1) plus, unless disabled, the expectation re-derivation (R2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB3"
KINDS = ("handoff", "compact", "anchor", "family")


def validate_handoff_dir(invocation: Invocation, root: Path, *, key: str,
                         expected_subject_commit: str | None = None, rederive: bool = True) -> dict[str, Any]:
    """Validate a handoff bundle directory and return its manifest (exit 2 on any defect)."""

    raise NotImplementedError("owned by MB3")


def validate_compact_dir(invocation: Invocation, root: Path, *, key: str, bind_raw: Path | None = None,
                         expected_subject_commit: str | None = None) -> dict[str, Any]:
    """Validate a published compact bundle (``complete`` or ``composed``) including its embedded
    final selection (``documents.validate_selection`` and ``documents.check_compact_selection``);
    with ``bind_raw`` (the raw handoff directory) re-encode every derivative from the raw PNGs and
    require byte-identical WebP (BP ``_bind_compact``)."""

    raise NotImplementedError("owned by MB3")


def validate_bundle(invocation: Invocation, kind: str, root: Path, *, key: str, bind_raw: Path | None = None,
                    expected_subject_commit: str | None = None) -> dict[str, Any]:
    """The ``validate`` command: dispatch on ``kind`` (``handoff``, ``compact``, ``anchor`` or
    ``family``, the latter validating only the envelope via :mod:`mod_base.family.envelope`)."""

    raise NotImplementedError("owned by MB3")
