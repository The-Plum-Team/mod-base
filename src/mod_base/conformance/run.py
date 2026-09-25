"""``conformance``: the synthetic producer -> collect -> build -> refresh -> rotate simulation (MB10).

For each selected key: adapter ``targets``/``expectation`` on the real matrix and contract; the
fixtures hook ``synthesize`` (from ``config.adapter.fixtures_path``, never loaded by the Pages
host) writes a packaged-output tree in the mod's own format with
``image_factory = mod_base.imaging.png.pattern_png``; then ``prepare`` + ``validate --kind
handoff`` + anchor, ``compact`` + ``validate --bind-raw``, ``admit``/``select``/``download``/
``authenticate`` (and ``compose`` where applicable) against
:class:`mod_base.github.fake.FakeGitHub`, ``build`` into a temporary ``_site``, ``refresh``,
``rotate --dry-run``, optional families, and the front-end/data-gate assertions.

Process model (no ``sys.path`` edits anywhere in the kit): :func:`run_conformance` validates the
arguments and re-executes the simulation as ``python3 -P -m mod_base.conformance.run ...`` in a
child whose environment carries ``PYTHONPATH = host.adapter_pythonpath(invocation)`` (the kit
``src`` plus the mod's ``config.adapter.python_path``) and no GitHub credentials. The child
(:func:`main`) loads the adapter and fixtures modules with ``host_child.load_adapter`` and drives
every hook, network hooks included, in-process through ``host_child.run_hook`` with a
``Context`` whose ``api`` is the seeded ``FakeGitHub``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

OWNER = "MB10"


def run_conformance(*, repo: Path, keys: Sequence[str] | None, all_keys: bool, kit_root: Path,
                    families: bool) -> dict[str, Any]:
    """Run the simulation and return a report ``{keys: [...], families: [...], checks: int}``;
    any failed check raises :class:`mod_base.errors.MbError` (exit 2)."""

    raise NotImplementedError("owned by MB10")


def main(argv: Sequence[str] | None = None) -> int:
    """The simulation child (see module docstring): ``python3 -P -m mod_base.conformance.run``
    with the same flags as the ``conformance`` command; writes the canonical JSON report to
    stdout and returns an exit code through ``errors.run_main``."""

    raise NotImplementedError("owned by MB10")


if __name__ == "__main__":
    raise SystemExit(main())
