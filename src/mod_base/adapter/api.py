"""The ``ctx`` object every adapter hook receives (MB3), constructed by the child process.

Members (SPEC §4.2): ``repo_root``, ``config`` (the validated :class:`mod_base.config.Config`),
``tmpdir`` (a private per-call directory), ``implementation_sha`` (``GITHUB_SHA`` in Pages jobs, the
``--subject-commit`` in ``prepare``), ``read_blob`` (bounded ``git cat-file`` of inert objects),
``runtime_tree`` (a bounded regular-file view), ``image_metrics`` (the kit's PixelMetrics) and
``api`` (a read-only :class:`mod_base.github.api.GitHubApi`, present only for a declared network
hook running in a token job; ``None`` otherwise).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mod_base.config import Config
from mod_base.github.api import GitHubApi
from mod_base.imaging.metrics import SizePolicy

OWNER = "MB3"


class RuntimeTree:
    """A bounded, regular-file-only, symlink-refusing view of a runtime directory. Paths are
    canonical relative POSIX paths; every read is bounded and stat-stable."""

    def __init__(self, root: Path, *, max_files: int, max_total_bytes: int) -> None:
        raise NotImplementedError("owned by MB3")

    @property
    def root(self) -> Path:
        raise NotImplementedError("owned by MB3")

    def files(self) -> list[str]:
        """Every regular file, sorted (the walk enforces the bounds)."""

        raise NotImplementedError("owned by MB3")

    def exists(self, relative: str) -> bool:
        raise NotImplementedError("owned by MB3")

    def read_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        raise NotImplementedError("owned by MB3")

    def read_json(self, relative: str, *, max_bytes: int) -> Any:
        """Strict JSON (duplicate keys and non-finite numbers refused)."""

        raise NotImplementedError("owned by MB3")

    def path(self, relative: str) -> Path:
        """The absolute path of an existing regular file (for image inspection)."""

        raise NotImplementedError("owned by MB3")


@dataclass(frozen=True)
class Context:
    """Passed as ``ctx`` to every hook."""

    repo_root: Path
    config: Config
    tmpdir: Path
    implementation_sha: str
    api: GitHubApi | None = None

    def read_blob(self, commit: str, path: str, max_bytes: int) -> bytes:
        """Bytes of ``path`` at ``commit`` from the local object store (``git -C repo_root cat-file``
        with a sanitized environment); the object must already be present (fetched as an inert
        object) and be a blob no larger than ``max_bytes``. Never checks anything out."""

        raise NotImplementedError("owned by MB3")

    def runtime_tree(self, root: str | Path) -> RuntimeTree:
        """A :class:`RuntimeTree` over ``root`` with the kit's runtime bounds."""

        raise NotImplementedError("owned by MB3")

    def image_metrics(self, path: str | Path, size_policy: SizePolicy | tuple[str, int, int]) -> dict[str, Any]:
        """The kit's PixelMetrics for a PNG (``imaging.metrics.inspect_png``)."""

        raise NotImplementedError("owned by MB3")
