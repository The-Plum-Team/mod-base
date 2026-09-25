"""Run the adapter's ``targets``/``expectation`` hooks, validate, canonicalize and hash (MB3).

The expectation is the only view of a mod's contract and matrix the kit sees (SPEC §3.1). It is
derived at ``prepare`` and re-derived at ``collect`` and ``build`` from the authenticated subject;
every derivation must be byte-equal to the embedded ``expectation.json`` (rule R2).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.runtime import Invocation

OWNER = "MB3"


def target_for_key(invocation: Invocation, key: str, *, subject: Mapping[str, str]) -> dict[str, Any]:
    """Call ``targets`` and return the target of ``key`` whose subject equals ``subject``
    (``{branch, commit, tree}``). ``default-branch`` mode passes ``branches=None``;
    ``enrolled-branches`` mode passes exactly ``[{name, commit, tree}]`` of the subject. A missing
    key or a different subject raises :class:`mod_base.errors.Unavailable`."""

    raise NotImplementedError("owned by MB3")


def read_extensions(invocation: Invocation, path: Path | None) -> dict[str, dict[str, Any]]:
    """Strictly read an ``extensions.json`` (``{name: object}``, at most 1 MiB, every name declared
    in ``config.adapter.extensions``); ``None`` gives ``{}``."""

    raise NotImplementedError("owned by MB3")


def derive_expectation(invocation: Invocation, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None,
                       extensions: Mapping[str, Any]) -> dict[str, Any]:
    """Call ``expectation`` and return the validated document (``documents.validate_expectation``
    with ``image_policy=config.image_policy()``)."""

    raise NotImplementedError("owned by MB3")


def expectation_bytes(expectation: Mapping[str, Any]) -> bytes:
    """``canonical_json(expectation)``: the exact bytes of ``expectation.json``."""

    raise NotImplementedError("owned by MB3")


def require_rederived(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any],
                      tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]:
    """R2: re-derive the expectation and require its canonical bytes to equal ``embedded``."""

    raise NotImplementedError("owned by MB3")


def run_expect(invocation: Invocation, *, key: str, tested_run_json: Path | None, extensions: Path | None,
               output: Path) -> dict[str, Any]:
    """The ``expect`` command: derive the expectation of ``key`` at the checked-out head and write
    ``output`` (canonical JSON, new file)."""

    raise NotImplementedError("owned by MB3")
