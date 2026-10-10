"""Admission of an activation change and of the candidate's Build/E2E callers (MB11).

Two questions decide whether a change to a mod's shared Build/E2E adoption may merge, and both are
answered from bytes the protected side read itself, never from what a candidate reports:

* :func:`admit_transition` compares the protected (base) activation manifest with the candidate's.
  Nothing may change but the mode, along
  :data:`mod_base.build_ci.activation.TRANSITIONS`, and a change of the manifest never comes with
  a pin change: the kit that renders and verifies the new callers is the kit the protected
  controller already runs. An unchanged manifest is no transition, so an ordinary pin bump stays
  possible in every mode.
* :func:`verify_candidate_callers` compares the candidate's caller files with the kit templates
  rendered for its pin and its canonical branch. A caller its mode manages equals the rendered
  template byte for byte and every other one is absent, so no mode leaves a kit caller outside
  the check.

``template transition`` runs both over two checkouts; a protected job runs them over the blobs it
authenticated. Neither is owner approval: they state what the bytes are, and the protected
controller decides who may merge them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mod_base.build_ci.activation import (activation_state, managed_callers, parse_activation, transition_refusal)
from mod_base.errors import MbError, single_line
from mod_base.pin import Pin
from mod_base.template.tool import expected_callers

MAX_REPORTED_PROBLEMS = 8


@dataclass(frozen=True)
class Transition:
    """An admitted change of activation: the ``previous`` and ``current``
    :func:`mod_base.build_ci.activation.activation_state`, whether the manifest ``changed`` at all,
    and the callers the candidate's state manages."""

    previous: str
    current: str
    changed: bool
    managed: tuple[str, ...]


def _pin(value: Pin, label: str) -> tuple[str, str]:
    if not isinstance(value, Pin):
        raise MbError(f"the {label} pin must be a parsed pin", reason="activation")
    return value.sha, value.version


def admit_transition(protected: bytes | None, candidate: bytes | None, *, protected_pin: Pin,
                     candidate_pin: Pin) -> Transition:
    """Admit the change from the ``protected`` manifest bytes to the ``candidate`` ones (``None``:
    that side has no manifest) or raise :class:`MbError`.

    Both manifests are decoded strictly. A candidate that states the same as the protected side is
    admitted whatever the pins, because nothing transitions. Any difference must be an allowed
    transition and both sides must carry the same pin SHA and version.
    """

    before, after = _pin(protected_pin, "protected"), _pin(candidate_pin, "candidate")
    previous = None if protected is None else parse_activation(protected, label="the protected activation manifest")
    current = None if candidate is None else parse_activation(candidate, label="the candidate activation manifest")
    changed = previous != current
    if changed:
        refusal = transition_refusal(previous, current)
        if refusal is not None:
            raise MbError(refusal, reason="activation")
        if before != after:
            raise MbError(f"an activation transition never comes with a pin change: the protected pin is "
                          f"{before[0]} {before[1]} and the candidate's is {after[0]} {after[1]}; change the mode "
                          "and the pin in separate pull requests", reason="activation")
    return Transition(activation_state(previous), activation_state(current), changed, managed_callers(current))


def verify_candidate_callers(files: Mapping[str, bytes], *, candidate: bytes | None, pin: Pin,
                             kit_root: Path, branch: str | None = None) -> tuple[str, ...]:
    """Require the candidate's Build/E2E caller ``files`` (``{path: bytes}`` of those that exist)
    to be exactly what its activation manifest ``candidate``, ``pin`` and canonical ``branch`` call
    for; return the managed paths.

    ``kit_root`` is the verified kit that ``pin`` names: its templates are the reviewed bytes.
    ``branch`` is ``canonical_branch`` of the candidate's ``site/mod-base.json`` as the protected
    side read it; without one, a managed caller whose template names the branch is an error.
    Every problem is reported in one :class:`MbError`: a managed caller that is missing or differs
    from its rendered template, a caller present outside its mode, or a path that is no caller.
    """

    _pin(pin, "candidate")
    document = None if candidate is None else parse_activation(candidate, label="the candidate activation manifest")
    expected = expected_callers(kit_root, pin, document, branch)
    problems = [f"{single_line(path, limit=120)} is not a mod-base Build/E2E caller" for path in sorted(files)
                if path not in expected]
    for path, wanted in expected.items():
        actual = files.get(path)
        if actual is not None and not isinstance(actual, bytes):
            raise MbError(f"the candidate's {path} must be read as bytes", reason="activation")
        if wanted is None:
            if actual is not None:
                problems.append(f"{path} must not exist in the {activation_state(document)} state")
        elif actual is None:
            problems.append(f"{path} is missing")
        elif actual != wanted:
            problems.append(f"{path} differs from the kit template rendered for {pin.sha} {pin.version}"
                            + ("" if branch is None else f" and the canonical branch {branch}"))
    if problems:
        shown = "; ".join(problems[:MAX_REPORTED_PROBLEMS])
        more = len(problems) - MAX_REPORTED_PROBLEMS
        raise MbError(f"the candidate's Build/E2E callers are not the managed ones: {shown}"
                      + (f"; and {more} more" if more > 0 else ""), reason="activation")
    return tuple(path for path, wanted in expected.items() if wanted is not None)
