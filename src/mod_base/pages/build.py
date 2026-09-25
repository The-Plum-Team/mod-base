"""The atomic site renderer (MB6, SPEC §5.3.2): QS ``build_site`` + BP ``_current_pages_inputs``.

``build_site`` downloads its own inputs: SPEC §5.3 build steps 1 ("Download every collected bundle
by immutable ID") and 2 ("Recheck and render the atomic site") are this one kit invocation,
because the download is part of the authentication (§5.3.2 step 4) and must happen in the process
that renders. ``collected_dir`` and ``families_dir`` are therefore **new** work directories that
``build_site`` creates: it lists this run's artifacts (``GITHUB_RUN_ID``, exact attempt), requires
each ``mb-collected--<key>`` exactly once inside its ``Publish / Collect <key>`` job window and
downloads it by id with its digest, size and owner run into ``collected_dir/<key>/``; each
``mb-collected-family--<family>--<key>`` (present exactly for an available leg) likewise into
``families_dir/<family>/<key>/`` (layout: ``mod_base.family.paired``).

Steps: invocation checks (``GITHUB_*`` set, workflow ref is ``pages.yml`` on the default branch, no
``GIT_*``, clean checkouts); this run and exact attempt are ``in_progress``; the attempt's jobs by
exact names from :mod:`mod_base.workflow`; the downloads above; live heads (and, for enrolled
branches, tree and matrix blob) equal every bundle subject; each bundle validated against a
re-derived expectation, its embedded selection re-authenticated and bound
(``documents.check_compact_selection``), every derivative re-inspected; families re-run R4;
adapter ``verify_publication`` with the promotion draft (``documents.validate_promotion(draft=
True)``: no ``site`` yet); render into an ``atomic_directory`` stage from the allowlisted kit
``site/`` inventory plus generated files (``site-data.json``, ``e2e/gallery-data.json``,
``assets/theme.css``, images, ``.nojekyll``, optional icon), seal it; recheck steps 2-5; publish to
``output`` (``_site``), write ``_site/build.json`` and the final promotion (with ``site``) as
``promotion_dir/`` :data:`PROMOTION_FILE`, output ``heads`` and ``site_sha256``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB6"
#: The promotion's file name inside ``--promotion DIR`` and inside the ``mb-promotion`` artifact.
PROMOTION_FILE = "promotion.json"
#: The exact kit ``site/`` files copied into every published site (never a directory walk).
SITE_ALLOWLIST = ("index.html", "e2e/index.html", "assets/site.js", "assets/gallery.js", "assets/styles.css")


@dataclass(frozen=True)
class BuildResult:
    heads: dict[str, str]
    site_sha256: str
    promotion: dict[str, Any]


def build_site(invocation: Invocation, *, api: GitHubApi, kit_root: Path, collected_dir: Path, families_dir: Path,
               output: Path, promotion_dir: Path) -> BuildResult:
    """Render and publish the atomic site (see module docstring); ``output`` must not exist."""

    raise NotImplementedError("owned by MB6")


def render_site(invocation: Invocation, *, kit_root: Path, bundles: list[dict[str, Any]],
                families: list[dict[str, Any]], stage_fd: int) -> dict[str, bytes]:
    """Pure rendering of already authenticated inputs into the stage; returns the written
    ``{relative path: bytes}`` map that :func:`mod_base.io.seal.seal_output` verifies."""

    raise NotImplementedError("owned by MB6")
