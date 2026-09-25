"""Source-run authentication producing ``mod-base.selection`` (MB5, SPEC §4.8).

Generic checks in order: the selected artifact by id (name grammar, digest, size, not expired,
owner run and attempt); the handoff run through its attempt endpoint (path, completed/success,
event set by branch, head repository, head SHA); ``display_title`` (mandatory whenever
``handoff.controller_sha != subject.commit``); the tested run by ``reuse`` (``none``: same run and
attempt; ``attested``: exactly one completed/success job named exactly
``config.source.attestation_job``; ``delegated``: the adapter's ``authenticate_extensions`` proved
the reference and the tested run is a successful ``source.workflow`` run); the exact job graph
when ``require_job_graph``; the newest-run rule when ``require_newest_run``; the kit binding
(SPEC §1.8: ``referenced_workflows`` for caches, the pin in ``source.workflow`` at the handoff
run's head for handoffs); and a live subject-head recheck.

``authenticate`` is collect step 3 and writes the selection **draft**
(``documents.validate_selection(draft=True)``): every field except ``manifest_sha256``,
``binding`` and ``composition``, which only the step writing the compact bundle can know
(``compact`` or, for a ``selected`` handoff, ``compose``). ``kit`` is the executing kit of this
Pages run; ``source.kit_binding.sha`` is the selected artifact's recorded ``kit.sha`` proven
against its owner, which may be an older kit. Run records come from ``runs.run_record`` with
``require_controller_head=False`` only for a ``delegated`` tested run.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.pages.select import Selected
from mod_base.runtime import Invocation

OWNER = "MB5"


def authenticate_selection(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path,
                           selected: Selected) -> dict[str, Any]:
    """Authenticate the downloaded ``selected_dir`` and return the validated selection draft."""

    raise NotImplementedError("owned by MB5")


def kit_binding(api: GitHubApi, invocation: Invocation, *, manifest: Mapping[str, Any],
                owner_run: Mapping[str, Any], selected_kind: str) -> dict[str, str]:
    """``{source, sha}``: prove ``manifest.kit.sha`` belongs to the authenticated owner run."""

    raise NotImplementedError("owned by MB5")


def run_authenticate(invocation: Invocation, *, api: GitHubApi, key: str, selected_dir: Path,
                     selected_json: Path, output: Path) -> dict[str, Any]:
    """The ``authenticate`` command: read ``selected_json`` (the exact :meth:`Selected.to_json`
    object written by ``select --output``), authenticate and write the selection draft to
    ``output`` (canonical JSON, new file)."""

    raise NotImplementedError("owned by MB5")
