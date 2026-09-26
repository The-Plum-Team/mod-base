"""The in-process adapter host of the conformance simulation (MB10).

A :class:`mod_base.github.fake.FakeGitHub` cannot cross a process boundary, so the simulation child
replaces ``mod_base.adapter.host.call`` by :class:`InProcessHooks`: the production placement rules
first (``host.check_placement``: SPEC §4.3 rows, forbidden jobs, network gating), then the frozen
in-process dispatch ``host_child.run_hook`` over a freshly loaded adapter module and a private
temporary directory, with the fake as ``ctx.api`` for a network hook only. Hook failures surface
exactly as through the isolated host (``run_hook`` maps them).

With ``conformance --keys``, the simulated repository declares only the selected keys:
``targets`` results (validated in full by ``run_hook`` first) are projected onto them, so admission,
build and rotation see exactly the simulated generation.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from mod_base.adapter import host, host_child
from mod_base.adapter.api import Context
from mod_base.adapter.protocol import HookFailed
from mod_base.imaging.png import pattern_png
from mod_base.runtime import Invocation


def adapter_file(invocation: Invocation, field: str) -> Path:
    """The repository file named by ``config.adapter[field]`` (``path`` or ``fixtures_path``)."""

    return invocation.repo_root.joinpath(*invocation.config.adapter[field].split("/"))


def context(invocation: Invocation, tmpdir: Path, api: Any = None) -> Context:
    return Context(repo_root=invocation.repo_root, config=invocation.config, tmpdir=tmpdir,
                   implementation_sha=invocation.implementation_sha, api=api)


class InProcessHooks:
    """A stand-in for ``host.call`` (see the module docstring); ``calls`` records ``(hook, network)``
    of every call the adapter answered (an unsupported hook is not recorded) and ``refusals`` the
    hook of every call the adapter refused (``HookFailed``), in order."""

    def __init__(self, *, keys: frozenset[str] | None, scratch: Path) -> None:
        self.keys = keys
        self.scratch = scratch
        self.api: Any = None
        self.calls: list[tuple[str, bool]] = []
        self.refusals: list[str] = []

    def __call__(self, invocation: Invocation, hook: str, arguments: Mapping[str, Any], *,
                 network: bool = False) -> Any:
        host.check_placement(invocation, hook, network=network)
        adapter = self.module(invocation, "path")
        with tempfile.TemporaryDirectory(prefix="hook-", dir=self.scratch) as directory:
            try:
                result = host_child.run_hook(context(invocation, Path(directory), self.api if network else None),
                                             adapter, hook, arguments)
            except HookFailed:
                self.refusals.append(hook)
                raise
        self.calls.append((hook, network))
        if hook == "targets" and self.keys is not None:
            result = [target for target in result if target["key"] in self.keys]
        return result

    def fixture(self, invocation: Invocation, arguments: Mapping[str, Any]) -> None:
        """The fixtures module's ``synthesize`` (``protocol.FIXTURE_HOOKS``) with ``pattern_png``."""

        with tempfile.TemporaryDirectory(prefix="fixture-", dir=self.scratch) as directory:
            host_child.run_hook(context(invocation, Path(directory)), self.module(invocation, "fixtures_path"),
                                "synthesize", arguments, image_factory=pattern_png)

    @staticmethod
    def module(invocation: Invocation, field: str) -> ModuleType:
        """A freshly loaded adapter (``path``) or fixtures (``fixtures_path``) module."""

        return host_child.load_adapter(adapter_file(invocation, field))

    def optional_fixture(self, invocation: Invocation, name: str) -> Callable[..., Any] | None:
        """An optional conformance fixture function of the fixtures module (see
        ``mod_base.conformance.run``), or ``None`` when the module does not define it."""

        function = getattr(self.module(invocation, "fixtures_path"), name, None)
        return function if callable(function) else None

    def call_fixture(self, invocation: Invocation, name: str, *, fixture_api: Any = None, **arguments: Any) -> Any:
        """Call the optional fixture function ``name`` with a fresh ``ctx`` whose ``api`` is
        ``fixture_api`` (the extension fixtures' :class:`mod_base.conformance._fixture_api.FixtureGitHub`;
        ``None`` for ``family_bundle``)."""

        function = self.optional_fixture(invocation, name)
        if function is None:
            raise ValueError(f"the fixtures module defines no {name}")
        with tempfile.TemporaryDirectory(prefix="fixture-", dir=self.scratch) as directory:
            return function(context(invocation, Path(directory), fixture_api), **arguments)
