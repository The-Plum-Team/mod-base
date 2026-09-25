"""The per-process invocation context every command builds once and passes to entry points.

An :class:`Invocation` binds the mod checkout (``--repo``), its validated config (``--config``),
the executing kit tree and the GitHub environment. Entry points never read ``os.environ``
themselves; they read the frozen copy held here, which keeps them testable and makes the set of
environment variables the kit consumes explicit:

* ``GITHUB_REPOSITORY``, ``GITHUB_SHA``, ``GITHUB_JOB``, ``GITHUB_RUN_ID``, ``GITHUB_RUN_ATTEMPT``,
  ``GITHUB_REF_NAME``, ``GITHUB_WORKFLOW_REF``, ``GITHUB_OUTPUT``: GitHub's own run facts;
* ``MOD_BASE_KIT_SHA``: the pinned kit commit (exported by the ``setup`` composite and by every
  callee prologue); the kit version is always this checkout's ``mod_base.__version__``;
* ``GH_TOKEN`` / ``GITHUB_TOKEN``: present only in steps that call the API (never in hooks
  unless the host grants it to a declared network hook).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import mod_base
from mod_base.config import Config, load_config
from mod_base.errors import MbError
from mod_base.model import grammar

ENVIRONMENT_NAMES = (
    "GITHUB_REPOSITORY",
    "GITHUB_SHA",
    "GITHUB_JOB",
    "GITHUB_RUN_ID",
    "GITHUB_RUN_ATTEMPT",
    "GITHUB_REF",
    "GITHUB_REF_NAME",
    "GITHUB_WORKFLOW_REF",
    "GITHUB_EVENT_NAME",
    "GITHUB_OUTPUT",
    "GITHUB_API_URL",
    "MOD_BASE_KIT_SHA",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


def kit_root() -> Path:
    """The root of the executing kit checkout (the directory holding ``src/``, ``site/``...)."""

    return Path(mod_base.__file__).resolve().parents[2]


def kit_ref(sha: str) -> dict[str, str]:
    """Return the ``KitRef`` of the executing kit at ``sha`` (SPEC §3.0)."""

    return {"repository": mod_base.KIT_REPOSITORY, "sha": grammar.require_sha1(sha, "kit SHA"),
            "version": mod_base.__version__}


@dataclass(frozen=True)
class Invocation:
    """Everything an entry point may know about its process. Accessors fail closed when a fact
    the caller needs is absent instead of inventing a default."""

    repo_root: Path
    config: Config
    kit_root: Path
    environ: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    implementation_sha_override: str | None = None

    @property
    def kit_src(self) -> Path:
        return self.kit_root / "src"

    def _required(self, name: str) -> str:
        value = self.environ.get(name)
        if not value:
            raise MbError(f"{name} is required for this command", reason="environment")
        return value

    @property
    def repository(self) -> str:
        return grammar.require(grammar.REPOSITORY, self._required("GITHUB_REPOSITORY"), "GITHUB_REPOSITORY")

    @property
    def implementation_sha(self) -> str:
        """The protected mod head executing: ``GITHUB_SHA``, or the explicit override (the
        ``prepare`` subject commit)."""

        if self.implementation_sha_override is not None:
            return grammar.require_sha1(self.implementation_sha_override, "implementation SHA")
        return grammar.require_sha1(self._required("GITHUB_SHA"), "GITHUB_SHA")

    @property
    def kit(self) -> dict[str, str]:
        return kit_ref(self._required("MOD_BASE_KIT_SHA"))

    @property
    def github_job(self) -> str | None:
        return self.environ.get("GITHUB_JOB") or None

    @property
    def token(self) -> str | None:
        return self.environ.get("GH_TOKEN") or self.environ.get("GITHUB_TOKEN") or None

    @property
    def api_url(self) -> str:
        return self.environ.get("GITHUB_API_URL") or "https://api.github.com"

    def github_output(self, explicit: Path | None = None) -> Path | None:
        """The ``$GITHUB_OUTPUT`` file (an explicit ``--github-output`` wins)."""

        if explicit is not None:
            return explicit
        value = self.environ.get("GITHUB_OUTPUT")
        return Path(value) if value else None


def snapshot_environment(environ: Mapping[str, str]) -> Mapping[str, str]:
    """A read-only copy of exactly the environment names the kit consumes."""

    return MappingProxyType({name: environ[name] for name in ENVIRONMENT_NAMES if name in environ})


def build_invocation(repo: Path | str, config_path: Path | str | None, environ: Mapping[str, str], *,
                     check_repository: bool = True, implementation_sha: str | None = None,
                     root: Path | None = None) -> Invocation:
    """Load the config of ``repo`` and bind the environment snapshot into an :class:`Invocation`."""

    repo_root = Path(os.path.abspath(repo))
    config = load_config(repo_root, config_path, check_repository_facts=check_repository)
    return Invocation(repo_root=repo_root, config=config, kit_root=root or kit_root(),
                      environ=snapshot_environment(environ), implementation_sha_override=implementation_sha)

