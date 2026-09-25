"""Composition of ``selected`` evidence with its authenticated baseline (MB3, rule R3).

``compose_selected`` is collect step 4 for a ``selected`` handoff (see ``docs/SCHEMAS.md``, "The
collect flow"); it replaces step 5 (``compact``) for that bundle:

1. validate the selected handoff directory and the selection **draft** written by
   ``authenticate`` (``documents.validate_selection(draft=True)``);
2. write the core's own compaction of the selected handoff into a private directory: a compact
   bundle validated with ``documents.validate_compact(intermediate=True)`` (``scope.kind:
   "selected"``, its selected expectation and the draft embedded as ``selection.json``); its
   :func:`mod_base.model.documents.compact_identity_sha256` is the ``selected_manifest_sha256``;
3. call the adapter's network hook ``compose(ctx, key, selected_compact_dir, output_dir)`` with
   that directory and a private output directory (no ``compose`` hook: fail closed);
4. R3: download the named ``mb-baseline--<key>--...`` artifact by id and authenticate it (owner:
   a successful ``pages.yml`` run on the default branch whose attempt's ``Finalize / Refresh
   evidence cache for {key}`` job succeeded, upload inside its "Retain the complete compact
   generation for feature evidence reuse" step window), validate it as a compact bundle, then
   require every ``epoch: baseline`` frame to equal the baseline bytes and metrics, every ``epoch:
   selected`` frame to equal step 2's compaction, the composed frames to equal the complete
   expectation with no duplicate or missing frame, ``scope.components`` to name that baseline and
   step 2's identity, and every ``tested`` record to equal its source;
5. complete the selection: ``binding`` (``reencode-identical``, frame and distinct-derivative
   counts), ``composition`` (``baseline_artifact`` with the authenticated ``owner_run_id``, and
   ``selected_manifest_sha256``) and ``manifest_sha256``, then re-emit the verified composed bundle
   into the new ``output`` with that final selection embedded as ``selection.json``
   (``documents.check_compact_selection`` holds).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB3"


def compose_selected(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path, selection_path: Path,
                     output: Path) -> dict[str, Any]:
    """The ``compose`` command (``--selected DIR --selection F --output DIR``): steps 1-5 of the
    module docstring; ``selection_path`` is the draft from ``authenticate``. Returns the composed
    compact manifest written to ``output`` (a new directory)."""

    raise NotImplementedError("owned by MB3")


def verify_composition(invocation: Invocation, *, composed_dir: Path, selected_dir: Path, baseline_dir: Path,
                       expectation: dict[str, Any]) -> None:
    """R3 core re-verification (step 4 of the module docstring); ``selected_dir`` is the
    intermediate selected compaction. Raises :class:`mod_base.errors.MbError`."""

    raise NotImplementedError("owned by MB3")
