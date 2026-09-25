"""Handoff -> compact bundle (MB3), with the re-encode binding (BP ``authenticate_source._bind_compact``).

``compact_bundle`` is collect step 5 (see ``docs/SCHEMAS.md``, "The collect flow"). Deterministic:
``images/<sha>.webp`` derivatives via ``imaging.webp.derive_webp`` at the expectation's image
policy, derivative PixelMetrics and CompareMetrics measured by the kit, ``files`` listing exactly
the shipped files. The input is a ``complete`` handoff or a cache (re-validated and re-emitted); a
``selected`` handoff is refused here (it must go through ``compose``, R3). ``selection_path`` is
the selection **draft** written by ``authenticate``; this step completes it (``binding``:
``reencode-identical`` for a handoff, ``cache-revalidated`` for a cache, with the frame and
distinct-derivative counts; ``manifest_sha256`` = ``documents.compact_identity_sha256`` of the
written manifest; a composed cache keeps its ``composition``) and embeds the final selection as
``selection.json``, so ``documents.check_compact_selection`` holds. The written manifest's ``kit``
is the executing kit (the same ``KitRef`` as the selection).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB3"


def compact_bundle(invocation: Invocation, *, key: str, input_dir: Path, selection_path: Path,
                   output: Path) -> dict[str, Any]:
    """Write the compact bundle of ``input_dir`` (a complete handoff or a cache) into the new
    ``output`` with the completed selection embedded (see module docstring) and return its
    validated manifest."""

    raise NotImplementedError("owned by MB3")
