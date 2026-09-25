"""Select the newest authenticated evidence for one key (MB5).

QS ``select_artifact.select_source/resolve_evidence`` + BP ``newest_exact_source``: a nominated
artifact id is re-authenticated and never replaced by a fallback; otherwise the newest
``mb-handoff--<key>--a*`` whose owner run is authenticated for the expected subject commit, else
the newest ``mb-cache--<key>--<subject>`` owned by a successful Pages run. No admissible artifact
raises :class:`mod_base.errors.Unavailable` (exit 3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB5"


#: The exact keys of the ``Selected`` JSON object (``select --output``, ``authenticate --selected-json``).
SELECTED_KEYS = ("kind", "artifact_id", "name", "digest", "size", "run_id", "run_attempt")
#: ``Selected.kind`` values: the ``grammar.parse_artifact_name`` kinds ``select`` may return.
SELECTED_KINDS = ("handoff", "cache", "family-handoff", "family-cache")


@dataclass(frozen=True)
class Selected:
    """The ``select`` outputs; also the ``--selected-json`` document of ``authenticate``.

    JSON form (written by ``select --output F`` as canonical JSON, read by ``authenticate
    --selected-json F``): an object with exactly :data:`SELECTED_KEYS`, the dataclass field names:
    ``kind`` (one of :data:`SELECTED_KINDS`), ``artifact_id``/``size``/``run_id``/``run_attempt``
    (positive integers, never booleans; ``size`` at most ``limits.MAX_RAW_BUNDLE_BYTES``),
    ``name`` (an artifact name that ``grammar.parse_artifact_name`` parses as ``kind``, with the
    same attempt for a handoff) and ``digest`` (``sha256:<64 hex>``)."""

    kind: str
    artifact_id: int
    name: str
    digest: str
    size: int
    run_id: int
    run_attempt: int

    @classmethod
    def parse(cls, value: Any) -> "Selected":
        """Strictly parse the JSON object form (see the class docstring); raises ``MbError``."""

        raise NotImplementedError("owned by MB5")

    def to_json(self) -> dict[str, Any]:
        """The JSON object form: exactly :data:`SELECTED_KEYS` mapped to the field values."""

        raise NotImplementedError("owned by MB5")


def select_evidence(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None = None,
                    nomination: int | None = None, expected_subject_commit: str) -> Selected:
    """Return the selected artifact or raise ``Unavailable`` (see module docstring)."""

    raise NotImplementedError("owned by MB5")
