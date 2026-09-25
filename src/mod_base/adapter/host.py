"""Parent side of the adapter call (MB3, SPEC §1.9).

``call`` writes the request envelope (:mod:`mod_base.adapter.protocol`) to a private temporary
directory and runs exactly::

    env -i PATH=/usr/bin:/bin:<dirname(python3)> HOME=<tmp>/home TMPDIR=<tmp> LANG=C.UTF-8
           PYTHONHASHSEED=0 PYTHONSAFEPATH=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1
           PYTHONPATH=<kit>/src:<each config.adapter.python_path entry inside the repo>
           [GH_TOKEN GITHUB_API_URL GITHUB_REPOSITORY  <- only if hook in config.adapter.network_hooks
                                                         AND $GITHUB_JOB in protocol.TOKEN_JOBS]
      python3 -P -m mod_base.adapter.host_child --adapter <repo>/<config.adapter.path> --hook <name>
              --request <tmp>/req.json --response <tmp>/resp.json

with ``subprocess.run`` (no shell), the argv allowlist above, ``config.adapter.timeout_seconds``
and a 16 MiB response read through strict JSON; the response is validated by
``protocol.validate_response``. Only :data:`mod_base.adapter.protocol.HOOKS` go through ``call``;
fixture hooks never do (``conformance`` dispatches them in-process through
``host_child.run_hook``). Unsupported hooks raise ``protocol.HookUnsupported``; hook
failures, timeouts, oversize or malformed responses raise :class:`mod_base.errors.MbError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB3"
CHILD_MODULE = "mod_base.adapter.host_child"
BASE_PATH = "/usr/bin:/bin"


def adapter_pythonpath(invocation: Invocation) -> str:
    """The child's ``PYTHONPATH``: ``<kit>/src`` then each ``config.adapter.python_path`` entry
    resolved inside the repository (no ``..``, no symlink component), joined with ``:``.

    ``conformance`` runs its in-process simulation in a child process started with exactly this
    ``PYTHONPATH`` (an environment value, never a ``sys.path`` edit), so the adapter and fixtures
    modules import their mod's code the same way the isolated hook child does."""

    raise NotImplementedError("owned by MB3")


def child_environment(invocation: Invocation, hook: str, *, tmpdir: Path) -> dict[str, str]:
    """The exact ``env -i`` environment for ``hook`` (see module docstring); the token appears only
    for a declared network hook in a token job."""

    raise NotImplementedError("owned by MB3")


def child_argv(invocation: Invocation, hook: str, *, request: Path, response: Path) -> list[str]:
    """The exact child argv: ``[python3, -P, -m, host_child, --adapter, A, --hook, H, --request, R,
    --response, S]``."""

    raise NotImplementedError("owned by MB3")


def call(invocation: Invocation, hook: str, arguments: Mapping[str, Any], *, network: bool = False) -> Any:
    """Run ``hook`` with ``arguments`` in the isolated child and return its validated result.

    ``network=True`` requests the read-only token; it is granted only when the hook is in
    ``config.adapter.network_hooks`` and ``$GITHUB_JOB`` is a token job, otherwise the call raises
    before starting the child.
    """

    raise NotImplementedError("owned by MB3")
