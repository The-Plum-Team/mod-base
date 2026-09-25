"""Run the adapter's ``targets``/``expectation`` hooks, validate, canonicalize and hash (MB3).

The expectation is the only view of a mod's contract and matrix the kit sees (SPEC §3.1). It is
derived at ``prepare`` and re-derived at ``collect`` and ``build`` from the authenticated subject;
every derivation must be byte-equal to the embedded ``expectation.json`` (rule R2).

The ``tested_run`` projection handed to the ``expectation`` hook is :func:`tested_run_projection`:
``{event, branch}`` with the tested claim's branch. The producer has no API token, so at
``prepare`` the only event it knows is its own run's (``GITHUB_EVENT_NAME``): the embedded
expectation is derived with the **handoff run's** event, and every re-derivation uses the
authenticated ``handoff_run.event`` the same way. That is exactly the tested run's event for
``reuse: "none"`` (one run). For a distinct tested run, :func:`require_rederived_for_runs` also
derives with the authenticated **tested run's** own event (SPEC §4.2) and requires the same bytes,
so a tested run can never be published under another run's projection (Block Pops: scheduled
packaged coverage never becomes an attested ``pr-anchors`` projection).

In ``enrolled-branches`` mode the adapter decides enrollment: for a lone subject branch it does not
enroll, it can only return no target, which the protocol's ``1..targets.max`` rule rejects;
:func:`target_for_key` reports exactly that rejection as :class:`~mod_base.errors.Unavailable`
(the documented "missing key"), every other ``targets`` failure stays a rejection.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base.adapter import host, protocol
from mod_base.adapter.api import _run_git
from mod_base.adapter.protocol import EXTENSION_OBJECTS, TESTED_RUN_PROJECTION, HookFailed
from mod_base.errors import MbError, Unavailable
from mod_base.evidence._common import fail, read_json_argument, write_new_file
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import SUBJECT, validate_expectation
from mod_base.model.validators import DocumentError
from mod_base.runtime import Invocation

OWNER = "MB3"


def tested_run_projection(tested: Mapping[str, Any], event: str) -> dict[str, str]:
    """``{event, branch}`` for the ``expectation`` hook: ``event`` (the handoff run's, see the
    module docstring) and the tested claim's ``branch``. Validated like a hook argument."""

    projection = {"event": event, "branch": tested["branch"]}
    TESTED_RUN_PROJECTION(projection, "$.tested_run")
    return projection


def _no_target_text() -> str:
    """The protocol's own rejection of a ``targets`` result naming no target."""

    try:
        protocol.validate_result("targets", [], {"branches": []})
    except DocumentError as exc:
        return str(exc)
    raise AssertionError("the protocol accepts an empty targets result")  # pragma: no cover


def target_for_key(invocation: Invocation, key: str, *, subject: Mapping[str, str]) -> dict[str, Any]:
    """Call ``targets`` and return the target of ``key`` whose subject equals ``subject``
    (``{branch, commit, tree}``). ``default-branch`` mode passes ``branches=None``;
    ``enrolled-branches`` mode passes exactly ``[{name, commit, tree}]`` of the subject. A missing
    key or a different subject raises :class:`mod_base.errors.Unavailable`."""

    grammar.require_key(key)
    wanted = SUBJECT(dict(subject), "$.subject")
    targets_config = invocation.config.targets
    if targets_config["mode"] == "default-branch":
        branches = None
    else:
        branches = [{"name": wanted["branch"], "commit": wanted["commit"], "tree": wanted["tree"]}]
    try:
        targets = host.call(invocation, "targets", {"branches": branches})
    except (HookFailed, DocumentError) as exc:
        # In-process the protocol raises its DocumentError, across the host it arrives as the
        # child's HookFailed carrying the same text; either way the adapter enrolled nothing.
        if branches is not None and str(exc).endswith(_no_target_text()):
            raise Unavailable(f"the adapter enrolls no target for branch {wanted['branch']}") from exc
        raise
    if len(targets) > targets_config["max"]:
        raise fail(f"the adapter returned {len(targets)} targets, more than targets.max {targets_config['max']}")
    for target in targets:
        if target["key"] == key:
            if target["subject"] != wanted:
                raise Unavailable(f"the adapter's target {key} covers another subject than the evidence")
            return target
    raise Unavailable(f"the adapter declares no target {key} for this subject")


def read_extensions(invocation: Invocation, path: Path | None) -> dict[str, dict[str, Any]]:
    """Strictly read an ``extensions.json`` (``{name: object}``, at most 1 MiB, every name declared
    in ``config.adapter.extensions``); ``None`` gives ``{}``."""

    if path is None:
        return {}
    value, _ = read_json_argument(path, max_bytes=lim.MAX_EXTENSIONS_BYTES, label="extensions.json")
    return _check_extensions(invocation, value, label="extensions.json")


def _check_extensions(invocation: Invocation, value: Any, *, label: str) -> dict[str, dict[str, Any]]:
    """``value`` as validated extension objects whose names are all declared by the config."""

    EXTENSION_OBJECTS(value, f"${label}")
    undeclared = sorted(set(value) - invocation.config.extension_names)
    if undeclared:
        raise fail(f"{label} names undeclared extensions {undeclared[:5]}")
    return value


def derive_expectation(invocation: Invocation, *, target: Mapping[str, Any], tested_run: Mapping[str, Any] | None,
                       extensions: Mapping[str, Any]) -> dict[str, Any]:
    """Call ``expectation`` and return the validated document (``documents.validate_expectation``
    with ``image_policy=config.image_policy()``)."""

    document = host.call(invocation, "expectation", {"target": dict(target),
                                                     "tested_run": None if tested_run is None else dict(tested_run),
                                                     "extensions": dict(extensions)})
    validate_expectation(document, image_policy=invocation.config.image_policy())
    if document["repository"] != invocation.repository:
        raise fail("the expectation names another repository than this run")
    encoded = canonical_json(document)
    if len(encoded) > lim.MAX_EXPECTATION_BYTES:
        raise fail(f"the expectation exceeds {lim.MAX_EXPECTATION_BYTES} bytes")
    return document


def expectation_bytes(expectation: Mapping[str, Any]) -> bytes:
    """``canonical_json(expectation)``: the exact bytes of ``expectation.json``."""

    return canonical_json(expectation)


def require_rederived(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any],
                      tested_run: Mapping[str, Any] | None, extensions: Mapping[str, Any]) -> dict[str, Any]:
    """R2: re-derive the expectation and require its canonical bytes to equal ``embedded``."""

    derived = derive_expectation(invocation, target=target, tested_run=tested_run, extensions=extensions)
    if expectation_bytes(derived) != bytes(embedded):
        raise MbError("R2: the re-derived expectation differs from the embedded expectation.json",
                      reason="expectation-drift")
    return derived


def require_rederived_for_runs(invocation: Invocation, embedded: bytes, *, target: Mapping[str, Any],
                               tested: Mapping[str, Any], handoff_event: str, tested_event: str,
                               extensions: Mapping[str, Any]) -> dict[str, Any]:
    """R2 with authenticated run records: re-derive with the handoff run's ``handoff_event`` (the
    projection the producer used) and, when the tested run was started by another
    ``tested_event``, also with the tested run's own projection; both must give ``embedded``."""

    derived = require_rederived(invocation, embedded, target=target,
                                tested_run=tested_run_projection(tested, handoff_event), extensions=extensions)
    if tested_event != handoff_event:
        own = derive_expectation(invocation, target=target, tested_run=tested_run_projection(tested, tested_event),
                                 extensions=extensions)
        if expectation_bytes(own) != bytes(embedded):
            raise MbError(f"R2: the tested run's own {tested_event!r} projection differs from the expectation its "
                          f"{handoff_event!r} handoff run published", reason="expectation-drift")
    return derived


def _head_subject(invocation: Invocation) -> dict[str, str]:
    """``{branch, commit, tree}`` of the checked-out head (``GITHUB_SHA``)."""

    commit = invocation.implementation_sha
    with tempfile.TemporaryDirectory(prefix="mb-git-") as home:
        tree = _run_git(invocation.repo_root, ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{tree}}"],
                        home=Path(home), max_output_bytes=256).decode("ascii", "replace").strip()
    grammar.require_sha1(tree, "checked-out tree")
    if invocation.config.targets["mode"] == "default-branch":
        branch = invocation.config.canonical_branch
    else:
        branch = grammar.require(grammar.BRANCH, invocation.environ.get("GITHUB_REF_NAME"), "GITHUB_REF_NAME")
    return {"branch": branch, "commit": commit, "tree": tree}


def run_expect(invocation: Invocation, *, key: str, tested_run_json: Path | None, extensions: Path | None,
               output: Path) -> dict[str, Any]:
    """The ``expect`` command: derive the expectation of ``key`` at the checked-out head and write
    ``output`` (canonical JSON, new file)."""

    grammar.require_key(key)
    tested_run = None
    if tested_run_json is not None:
        value, _ = read_json_argument(tested_run_json, max_bytes=lim.MAX_SELECTION_BYTES, label="tested run")
        tested_run = TESTED_RUN_PROJECTION(value, "$tested_run")
    objects = read_extensions(invocation, extensions)
    target = target_for_key(invocation, key, subject=_head_subject(invocation))
    document = derive_expectation(invocation, target=target, tested_run=tested_run, extensions=objects)
    write_new_file(output, expectation_bytes(document))
    return document
