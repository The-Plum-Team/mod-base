"""Child side of the adapter call (MB3): ``python3 -P -m mod_base.adapter.host_child``.

Accepts exactly ``--adapter PATH --hook NAME --request FILE --response FILE``; reads and validates
the request envelope, loads the adapter with :func:`load_adapter`, builds
:class:`mod_base.adapter.api.Context` (``api`` from ``GH_TOKEN``/``GITHUB_API_URL`` only when the
request grants network) and dispatches through :func:`run_hook`, then writes one canonical
response envelope (``unsupported`` for :class:`~mod_base.adapter.protocol.HookUnsupported`; any
other exception becomes ``status: "error"`` with a bounded one-line message).

:func:`run_hook` is also the frozen in-process seam of ``conformance``: it loads the adapter (and
the ``config.adapter.fixtures_path`` module) with :func:`load_adapter` in a process whose
``PYTHONPATH`` is ``host.adapter_pythonpath`` and calls :func:`run_hook` with a ``Context`` whose
``api`` is a :class:`mod_base.github.fake.FakeGitHub`, so network hooks run against the fake.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

from mod_base.adapter.api import Context
from mod_base.adapter.protocol import ImageFactory

OWNER = "MB3"


def load_adapter(path: Path) -> ModuleType:
    """Import the adapter file as an isolated module (never added to ``sys.modules`` under a
    package name that other code could import)."""

    raise NotImplementedError("owned by MB3")


def run_hook(context: Context, adapter: ModuleType, hook: str, arguments: Mapping[str, Any], *,
             image_factory: ImageFactory | None = None) -> Any:
    """Dispatch one hook in the current process and return its validated result.

    For a Pages hook (``protocol.HOOKS``): require ``adapter.ADAPTER_API`` in
    ``protocol.ADAPTER_API_WINDOW``, validate ``arguments`` (``protocol.validate_arguments``), call
    ``getattr(adapter, hook)(context, **arguments)`` (raise ``HookUnsupported`` when the module has
    no such callable) and validate the result (``protocol.validate_result``). For a fixture hook
    (``protocol.FIXTURE_HOOKS``, only ever from ``conformance``): ``image_factory`` is required,
    arguments and result go through ``protocol.validate_fixture_arguments``/``_result`` and the call
    is ``adapter.synthesize(context, **arguments, image_factory=image_factory)``. Any other hook
    name raises before the module is touched."""

    raise NotImplementedError("owned by MB3")


def main(argv: Sequence[str] | None = None) -> int:
    raise NotImplementedError("owned by MB3")


if __name__ == "__main__":
    raise SystemExit(main())
