"""Roll the promoted bundles forward as caches (MB6): QS refresh-cache + BP ``refresh_cache``.

``refresh_bundle`` requires that this attempt's caller job ``Deploy GitHub Pages`` and
``Publish / Build atomic static site`` succeeded, then downloads its own inputs by exact id: this
run's single ``mb-promotion`` (uploaded inside the build job's window; validated as a final
promotion), then the collected artifact the promotion names for ``key`` (and ``family``), using the
promotion's ``collected_artifact_id`` and ``collected_digest``. It revalidates that artifact,
requires the live head to equal its ``coverage_sha`` and writes **exactly the bytes to upload**
into ``input_dir``, a new directory (the frozen ``--input`` flag names this upload directory):

* ordinary key: the compact bundle (``mb-collected--<key>`` verbatim), rolled forward as
  ``mb-cache--<key>--<coverage_sha>`` and, when ``baseline_archive.enabled`` and the scope is
  ``complete``, also retained as ``mb-baseline--<key>--<commit>--<tested_run_id>``;
* family leg: the collected family artifact's ``source/`` directory (the envelope and native
  bundle, re-collectable by ``family collect``), rolled forward as
  ``mb-family-cache--<family>--<key>--<coverage_sha>``. A leg the promotion records as not
  available ("nothing collected") returns ``available=False`` with no cache name, writes nothing
  and exits 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mod_base.github.api import GitHubApi
from mod_base.runtime import Invocation

OWNER = "MB6"


@dataclass(frozen=True)
class RefreshResult:
    available: bool
    cache_name: str | None
    baseline_name: str | None = None


def refresh_bundle(invocation: Invocation, *, api: GitHubApi, key: str, family: str | None, input_dir: Path) -> RefreshResult:
    """Download and revalidate one promoted bundle, write its upload bytes into the new
    ``input_dir`` and name its cache (see module docstring). For a family leg with nothing
    collected it returns ``available=False`` (exit 0)."""

    raise NotImplementedError("owned by MB6")
