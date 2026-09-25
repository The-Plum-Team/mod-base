"""The producer (MB3): Quick Skin ``evidence.prepare`` + Block Pops ``evidence.curate``.

Runs in the ``prepare-evidence`` composite with no API token. It derives the target and the
expectation, calls ``collect`` on the E2E output, copies the declared runtime subset verbatim into
``runtime/``, recomputes every PixelMetrics and CompareMetrics with the kit (cross-checking the
adapter's ``reported_*`` values when ``config.images.cross_check_runtime_metrics``), calls
``collect`` again on the copied ``runtime/`` and requires an equal result (R1), writes
``manifest.json``, ``expectation.json`` and ``extensions.json`` through ``atomic_directory`` and
validates the result as ``mod-base.evidence.handoff``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB3"


@dataclass(frozen=True)
class PrepareResult:
    manifest: dict[str, Any]
    output: Path
    anchor_eligible: bool


def prepare_handoff(invocation: Invocation, *, e2e_root: Path, key: str, output: Path,
                    subject: Mapping[str, str], tested: Mapping[str, Any], handoff: Mapping[str, Any],
                    extensions_path: Path | None = None, anchor: str = "auto",
                    anchor_output: Path | None = None) -> PrepareResult:
    """Produce the handoff bundle for ``key`` in the new directory ``output``.

    ``subject`` is ``{branch, commit, tree}``; ``tested`` and ``handoff`` are ``RunClaim`` dicts
    (the handoff claim comes from ``documents.run_claim_from_environment``). ``anchor`` is
    ``"auto"`` or ``"off"``: with ``"auto"`` the result reports whether the adapter's
    ``anchor_selection`` and the direct-run rule make an anchor eligible (the anchor itself is cut
    by ``anchor create`` after the handoff upload, because it needs the artifact id).
    """

    raise NotImplementedError("owned by MB3")
