"""The Build/E2E activation manifest: its closed data, the callers each mode manages and the allowed
transitions (MB11).

A mod states how far it has adopted the shared Build and packaged E2E in
``site/mod-base-build-activation.json`` (``mod-base.ci.activation`` v1): its repository, its native
profile, one of five modes and, for a reviewed rollback, the mode that rollback leaves. The mode is
the only thing that decides which kit caller workflows are managed files of the mod
(:data:`MANAGED_CALLERS`); manifest data never names a template, a job, a permission or a secret.

* ``disabled`` and a mod without a manifest manage no caller: its Build and E2E workflows stay its
  own.
* ``shadow`` manages all four callers beside the mod's own gates, which stay authoritative.
* ``shared-build`` manages the guard, the Build caller and the status caller; packaged E2E stays
  the mod's own.
* ``shared-build-and-e2e`` manages all four.
* ``reviewed-rollback`` keeps managing the callers of the mode it leaves (``rollback_from``) until
  the next transition, which can only be to ``disabled``.

:data:`TRANSITIONS` lists every allowed change of state. A manifest is introduced and removed only
in the ``disabled`` mode, so a mode that manages callers never turns into a missing manifest, and a
transition changes nothing but the mode. Whether a change is also free of a pin change, and whether
the candidate's callers are the rendered templates, is decided by
:mod:`mod_base.build_ci.transition`. Nothing here is owner approval: a mode label is data until the
protected controller admits it.
"""

from __future__ import annotations

from typing import Any

from mod_base import readable_schema_versions
from mod_base.build_ci.protocol import PROFILES
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import strict_loads
from mod_base.model.validators import Const, Int, Nullable, Obj, Str, check

ACTIVATION_PATH = "site/mod-base-build-activation.json"
ACTIVATION_KIND = "mod-base.ci.activation"
ACTIVATION_MODES = ("disabled", "shadow", "shared-build", "shared-build-and-e2e", "reviewed-rollback")
DISABLED_MODE = "disabled"
ROLLBACK_MODE = "reviewed-rollback"
#: The modes a reviewed rollback can leave: every mode that manages callers of its own.
ROLLBACK_SOURCES = ("shadow", "shared-build", "shared-build-and-e2e")
#: The state of a mod that has no activation manifest (never a ``mode`` value).
ABSENT_STATE = "absent"

GUARD_CALLER = ".github/workflows/mod-base-guard.yml"
BUILD_CALLER = ".github/workflows/mod-base-build.yml"
PACKAGED_CALLER = ".github/workflows/mod-base-packaged-e2e.yml"
STATUS_CALLER = ".github/workflows/mod-base-gate-status.yml"
#: Every caller workflow the activation mode can make a managed file of a mod.
CALLERS = (GUARD_CALLER, BUILD_CALLER, PACKAGED_CALLER, STATUS_CALLER)

#: Mode -> the callers the kit manages in it. ``reviewed-rollback`` has no row of its own: it
#: manages the row of the mode it leaves (:func:`managed_mode`).
MANAGED_CALLERS: dict[str, tuple[str, ...]] = {
    "disabled": (),
    "shadow": CALLERS,
    "shared-build": (GUARD_CALLER, BUILD_CALLER, STATUS_CALLER),
    "shared-build-and-e2e": CALLERS,
}

#: State -> the states it may change to (:func:`activation_state` names a state). Entering
#: ``reviewed-rollback`` additionally requires ``rollback_from`` to name the mode being left.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    ABSENT_STATE: ("disabled",),
    "disabled": (ABSENT_STATE, "shadow", "shared-build"),
    "shadow": ("disabled", "shared-build", "shared-build-and-e2e", "reviewed-rollback"),
    "shared-build": ("shared-build-and-e2e", "reviewed-rollback"),
    "shared-build-and-e2e": ("reviewed-rollback",),
    "reviewed-rollback": ("disabled",),
}

_ACTIVATION = Obj({
    "kind": Const(ACTIVATION_KIND),
    "schema_version": Int(min(readable_schema_versions(ACTIVATION_KIND)),
                          max(readable_schema_versions(ACTIVATION_KIND))),
    "repository": Str(grammar.REPOSITORY, max_len=201),
    "profile": Str(choices=PROFILES),
    "mode": Str(choices=ACTIVATION_MODES),
    "rollback_from": Nullable(Str(choices=ROLLBACK_SOURCES)),
})


def validate_activation(document: Any, *, path: str = "$") -> dict[str, Any]:
    """Validate the closed ``mod-base.ci.activation`` v1 data and return it.

    ``rollback_from`` names the mode a ``reviewed-rollback`` leaves and is ``null`` in every other
    mode, so a rollback always says which callers it still manages. No pin, template, job,
    permission, secret, approval, deferral or scenario key exists. A valid document is data only:
    the protected controller binds it to the Build configuration and admits the transition.
    """

    _ACTIVATION(document, path)
    check((document["mode"] == ROLLBACK_MODE) == (document["rollback_from"] is not None), f"{path}.rollback_from",
          f"must name the mode a {ROLLBACK_MODE} leaves and be null in every other mode")
    return document


def parse_activation(data: bytes, *, label: str = ACTIVATION_PATH) -> dict[str, Any]:
    """Strictly decode and validate the bytes of an activation manifest (at most 8 KiB)."""

    return validate_activation(strict_loads(data, label=label, max_bytes=lim.MAX_CI_ACTIVATION_BYTES))


def activation_state(document: dict[str, Any] | None) -> str:
    """The :data:`TRANSITIONS` state of a validated manifest: its mode, or :data:`ABSENT_STATE`
    for a mod without one (``None``)."""

    return ABSENT_STATE if document is None else document["mode"]


def managed_mode(document: dict[str, Any] | None) -> str:
    """The :data:`MANAGED_CALLERS` row a validated manifest selects: the mode a
    ``reviewed-rollback`` leaves, ``disabled`` for a mod without a manifest, else its mode."""

    if document is None:
        return DISABLED_MODE
    return document["rollback_from"] if document["mode"] == ROLLBACK_MODE else document["mode"]


def managed_callers(document: dict[str, Any] | None) -> tuple[str, ...]:
    """The caller workflows the kit manages for a mod with this validated manifest (``None``: no
    manifest), in :data:`CALLERS` order."""

    return MANAGED_CALLERS[managed_mode(document)]


def managing_modes(caller: str) -> frozenset[str]:
    """The :data:`MANAGED_CALLERS` rows that hold ``caller``: the values of :func:`managed_mode`
    for which it is a managed file."""

    return frozenset(mode for mode, callers in MANAGED_CALLERS.items() if caller in callers)


def next_states(document: dict[str, Any] | None) -> tuple[str, ...]:
    """The states a mod with this validated manifest may change to in one transition."""

    return TRANSITIONS[activation_state(document)]


def transition_refusal(previous: dict[str, Any] | None, current: dict[str, Any] | None) -> str | None:
    """Why the change from the validated manifest ``previous`` to ``current`` is not an allowed
    transition, or ``None`` when it is one (or nothing changed). ``None`` stands for no manifest.

    A transition changes the mode along :data:`TRANSITIONS` and nothing else: the repository and
    the profile stay, a manifest appears and disappears only in the ``disabled`` mode, and a
    ``reviewed-rollback`` names exactly the mode it leaves.
    """

    if previous == current:
        return None
    before, after = activation_state(previous), activation_state(current)
    if previous is not None and current is not None and (
            (previous["repository"], previous["profile"]) != (current["repository"], current["profile"])):
        return "an activation transition changes the mode only, never the repository or the profile"
    if before == after:
        return f"the activation manifest changed while its mode stayed {before}; a transition changes the mode"
    if after not in TRANSITIONS[before]:
        if after == ABSENT_STATE:
            return (f"the activation manifest cannot be removed in the {before} mode: only a {DISABLED_MODE} "
                    "manifest may be removed")
        return (f"the activation cannot change from {before} to {after}; from {before} it may change to "
                f"{', '.join(TRANSITIONS[before])}")
    if current is not None and after == ROLLBACK_MODE and current["rollback_from"] != before:
        return (f"a {ROLLBACK_MODE} from the {before} mode must name it: rollback_from is "
                f"{current['rollback_from']}")
    return None
