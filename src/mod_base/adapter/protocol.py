"""The adapter protocol: hook names, where they may run, and strict request/response schemas.

A mod's adapter (``scripts/pages/mod_base_adapter.py``, ``ADAPTER_API = 1``) is called only
through :mod:`mod_base.adapter.host`, which runs it in an ``env -i`` child
(:mod:`mod_base.adapter.host_child`). The parent writes one request envelope, the child writes
one response envelope; both are canonical JSON validated here. Hook *results* are validated
against strict per-hook schemas before the core uses them (SPEC §4.2); the core then applies the
generic re-verification rules R1-R6 (SPEC §4.4) that need bytes, images or the API.

Request envelope (``mod-base.adapter.request`` v1)::

    {kind, schema_version: 1, api: 1, hook,
     context: {repo_root, config_path, kit_src, tmpdir, implementation_sha, network: bool},
     arguments: {...per hook...}}

Response envelope (``mod-base.adapter.response`` v1)::

    {kind, schema_version: 1, hook, status: "ok" | "unsupported" | "error",
     result?: <per hook> (exactly when status == "ok"), error?: str (exactly when "error")}

``unsupported`` means the adapter module defines no such hook; the core fails closed wherever
that hook is required (for example a ``selected`` bundle without ``compose``).

A hook is called as ``hook(ctx, **arguments)``: the argument names are exactly the keys of its
:data:`ARGUMENTS` schema. The same in-process dispatch (``mod_base.adapter.host_child.run_hook``)
serves the isolated child and ``conformance``, which calls hooks in a process whose ``PYTHONPATH``
is the host's (``mod_base.adapter.host.adapter_pythonpath``) with a ``FakeGitHub``-backed ``ctx.api``.
Test-only fixture hooks (:data:`FIXTURE_HOOKS`, from ``config.adapter.fixtures_path``) run only
there: ``synthesize(ctx, target, expectation, out_root, image_factory)`` with the arguments of
:data:`FIXTURE_ARGUMENTS` plus an :data:`ImageFactory`; the Pages host never loads them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from mod_base import ADAPTER_API
from mod_base.errors import MbError
from mod_base.model import grammar as g
from mod_base.model import limits as lim
from mod_base.model.documents import (
    COMPARE_METRICS,
    DIGEST,
    IDENT,
    JARS,
    KEY,
    LANE_ID,
    PIXEL_METRICS,
    PROFILE,
    RUN_ID,
    RUNTIME_EVIDENCE,
    SHA1,
    SHA256,
    SUBJECT,
    run_record,
    validate_expectation,
    validate_promotion,
)
from mod_base.model.validators import (
    Bool,
    Const,
    DocumentError,
    Int,
    List,
    Map,
    Nullable,
    Num,
    Obj,
    Str,
    Validator,
    check,
    fail,
)

#: Adapter API versions this kit can call: N and N-1 (>= 1).
ADAPTER_API_WINDOW = frozenset(version for version in (ADAPTER_API, ADAPTER_API - 1) if version >= 1)

HOOKS = frozenset(
    {
        "targets",
        "expectation",
        "collect",
        "expected_source_jobs",
        "authenticate_extensions",
        "compose",
        "verify_publication",
        "family_validate",
        "anchor_selection",
    }
)
#: Test-only hooks in ``config.adapter.fixtures_path``; never loaded by the Pages host. They run
#: only in-process under ``conformance`` (see :data:`FIXTURE_ARGUMENTS`).
FIXTURE_HOOKS = frozenset({"synthesize"})
#: ``image_factory(width, height, seed) -> bytes``: the deterministic RGB PNG generator passed to
#: ``synthesize``. Conformance passes :func:`mod_base.imaging.png.pattern_png`: the same arguments
#: always give the same bytes, and every image passes the 8-metric blank checks.
ImageFactory = Callable[[int, int, int], bytes]
#: Hooks that may receive a read-only token (only if also listed in ``config.adapter.network_hooks``).
NETWORK_HOOKS = frozenset({"authenticate_extensions", "compose", "verify_publication"})
#: Callee job ids (``$GITHUB_JOB``) whose permissions are exactly ``{actions: read, contents: read}``;
#: the only jobs in which a network hook may receive ``GH_TOKEN``.
TOKEN_JOBS = frozenset({"admit", "collect", "family", "build"})
#: The ``prepare-evidence`` composite, which runs inside a mod-owned producer job.
PREPARE_EVIDENCE = "prepare-evidence"
#: SPEC §4.3: where each hook may run (callee job ids, plus the prepare-evidence composite).
HOOK_JOBS: dict[str, frozenset[str]] = {
    "targets": frozenset({"admit", "collect", "build", PREPARE_EVIDENCE}),
    "expectation": frozenset({PREPARE_EVIDENCE, "admit", "collect", "family", "build"}),
    "collect": frozenset({PREPARE_EVIDENCE, "collect"}),
    "expected_source_jobs": frozenset({"collect", "build"}),
    "authenticate_extensions": frozenset({"collect", "build"}),
    "compose": frozenset({"collect"}),
    "verify_publication": frozenset({"build"}),
    "family_validate": frozenset({"family"}),
    "anchor_selection": frozenset({PREPARE_EVIDENCE}),
}
#: Jobs in which no adapter code may ever run (they hold a write scope or are caller-owned).
FORBIDDEN_JOBS = frozenset({"verify-kit", "deploy", "finalize", "refresh", "refresh-family",
                            "request-rotation", "rotate", "notify-pages"})
#: The fixed argv shape of the child: ``-P -m mod_base.adapter.host_child`` plus these flags.
CHILD_FLAGS = ("--adapter", "--hook", "--request", "--response")
REQUEST_KIND = "mod-base.adapter.request"
RESPONSE_KIND = "mod-base.adapter.response"
JOB_CONCLUSIONS = ("success", "failure", "cancelled", "skipped", "neutral", "timed_out", "action_required", "stale")
MAX_ERROR_CHARS = 1000


def _absolute_path(value: Any, path: str) -> str:
    if (not isinstance(value, str) or not value.startswith("/") or len(value) > 4096 or "\x00" in value
            or any(part in {"", ".", ".."} for part in value.split("/")[1:])):
        raise fail(path, "must be a normalized absolute POSIX path")
    return value


def _any_json_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail(path, "must be an object")
    return value


def _expectation(value: Any, path: str) -> dict[str, Any]:
    return validate_expectation(value, path=path)


TARGET = Obj(
    {
        "key": KEY,
        "label": Str(max_len=lim.MAX_LABEL_LENGTH, text="display"),
        "subject": SUBJECT,
        "matrix_sha256": SHA256,
        "contract_sha256": SHA256,
    }
)
BRANCH_HEAD = Obj({"name": Str(g.BRANCH, max_len=200), "commit": SHA1, "tree": SHA1})
TESTED_RUN_PROJECTION = Obj({"event": Str(g.EVENT, max_len=40), "branch": Str(g.BRANCH, max_len=200)})
EXTENSION_OBJECTS = Map(g.EXTENSION_NAME, _any_json_object, max_items=lim.MAX_EXTENSION_NAMES,
                        max_key_len=g.MAX_EXTENSION_NAME_LENGTH)


ARGUMENTS: dict[str, Validator] = {
    "targets": Obj({"branches": Nullable(List(BRANCH_HEAD, max_items=lim.MAX_BRANCHES,
                                              unique_by=lambda head: head["name"]))}),
    "expectation": Obj({"target": TARGET, "tested_run": Nullable(TESTED_RUN_PROJECTION),
                        "extensions": EXTENSION_OBJECTS}),
    "collect": Obj({"runtime_root": _absolute_path, "target": TARGET, "expectation": _expectation}),
    "expected_source_jobs": Obj({"expectation": _expectation, "tested_run": run_record}),
    "authenticate_extensions": Obj({"manifest": _any_json_object, "extensions": EXTENSION_OBJECTS}),
    "compose": Obj({"key": KEY, "selected_compact_dir": _absolute_path, "output_dir": _absolute_path}),
    "verify_publication": Obj({"promotion_draft": lambda value, path: validate_promotion(value, draft=True, path=path)}),
    "family_validate": Obj(
        {
            "family": Str(g.FAMILY, max_len=g.MAX_FAMILY_LENGTH),
            "key": KEY,
            "bundle_dir": _absolute_path,
            "expected_coverage_sha": SHA1,
            "output_dir": _absolute_path,
        }
    ),
    "anchor_selection": Obj({"expectation": _expectation}),
}

_COLLECT_RESULT = Obj(
    {
        "runtime_files": List(
            Str(max_len=lim.MAX_BUNDLE_PATH_CHARS), min_items=1, max_items=lim.MAX_RUNTIME_FILES, sorted_values=True
        ),
        "lanes": List(
            Obj({"lane_id": LANE_ID, "java": Int(8, 99), "profile": PROFILE, "status": Const("pass"), "jars": JARS},
                {"elapsed_s": Num(0.0, 10_000_000.0)}),
            min_items=1,
            max_items=lim.MAX_LANES,
            unique_by=lambda lane: lane["lane_id"],
        ),
        "frames": List(
            Obj({"frame_id": IDENT, "source_path": Str(max_len=lim.MAX_BUNDLE_PATH_CHARS),
                 "runtime_evidence": RUNTIME_EVIDENCE}, {"reported_pixel": PIXEL_METRICS}),
            min_items=1,
            max_items=lim.MAX_FRAMES,
            unique_by=lambda frame: frame["frame_id"],
        ),
        "comparisons": List(
            Obj({"comparison_id": IDENT}, {"reported": COMPARE_METRICS}),
            max_items=lim.MAX_COMPARISONS,
            unique_by=lambda comparison: comparison["comparison_id"],
        ),
    }
)


def _check_targets(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    List(TARGET, min_items=1, max_items=lim.MAX_KEYS, unique_by=lambda target: target["key"])(result, path)
    branches = arguments.get("branches")
    if branches is None:
        commits = {target["subject"]["commit"] for target in result}
        check(len(commits) == 1, path, "default-branch mode binds every key to the same protected head")
    else:
        heads = {head["name"]: head for head in branches}
        for position, target in enumerate(result):
            head = heads.get(target["subject"]["branch"])
            check(head is not None and head["commit"] == target["subject"]["commit"]
                  and head["tree"] == target["subject"]["tree"], f"{path}[{position}].subject",
                  "must be one of the listed branch heads")
    return result


def _check_expectation_result(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    validate_expectation(result, path=path)
    target = arguments["target"]
    for field in ("key", "subject", "matrix_sha256", "contract_sha256"):
        check(result[field] == target[field], f"{path}.{field}", "must equal the requested target")
    return result


def _check_collect(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    _COLLECT_RESULT(result, path)
    files = set(result["runtime_files"])
    for position, name in enumerate(result["runtime_files"]):
        check(g.is_bundle_path(name) and name.endswith((".json", ".png")), f"{path}.runtime_files[{position}]",
              "must be a relative .json or .png path")
    for position, frame in enumerate(result["frames"]):
        check(frame["source_path"] in files and frame["source_path"].endswith(".png"),
              f"{path}.frames[{position}].source_path", "must be a listed runtime .png file")
    expectation = arguments["expectation"]
    check([lane["lane_id"] for lane in result["lanes"]] == [lane["lane_id"] for lane in expectation["lanes"]],
          f"{path}.lanes", "lane ids must equal the expectation lanes, in order")
    for position, (lane, wanted) in enumerate(zip(result["lanes"], expectation["lanes"])):
        check(lane["java"] == wanted["java"], f"{path}.lanes[{position}].java", "must equal the expectation lane")
    check([frame["frame_id"] for frame in result["frames"]]
          == [capture["frame_id"] for capture in expectation["captures"]], f"{path}.frames",
          "frame ids must equal the expectation captures, in order")
    check([item["comparison_id"] for item in result["comparisons"]]
          == [item["comparison_id"] for item in expectation["comparisons"]], f"{path}.comparisons",
          "comparison ids must equal the expectation comparisons, in order")
    return result


def _check_expected_jobs(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    if result is None:
        return None
    List(
        Obj({"name": Str(max_len=lim.MAX_JOB_NAME_LENGTH, text="evidence"), "conclusion": Str(choices=JOB_CONCLUSIONS)}),
        min_items=1,
        max_items=lim.MAX_JOBS_PER_ATTEMPT,
        unique_by=lambda job: job["name"],
    )(result, path)
    return result


def _check_authenticated(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    Obj({"verified": List(Str(g.EXTENSION_NAME, max_len=g.MAX_EXTENSION_NAME_LENGTH),
                          max_items=lim.MAX_EXTENSION_NAMES, sorted_values=True),
         "reuse_verified": Bool()})(result, path)
    check(set(result["verified"]) == set(arguments["extensions"]), f"{path}.verified",
          "must name exactly the supplied extensions")
    return result


def _check_compose(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    Obj({"baseline_artifact": Obj({"id": RUN_ID, "name": Str(max_len=lim.MAX_ARTIFACT_NAME_BYTES),
                                   "digest": DIGEST})})(result, path)
    parsed = g.parse_artifact_name(result["baseline_artifact"]["name"])
    check(parsed is not None and parsed.kind == "baseline" and parsed.key == arguments["key"],
          f"{path}.baseline_artifact.name", "must be an mb-baseline name of the requested key")
    return result


def _check_verify_publication(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    check(result is None, path, "must be null (raise to veto)")
    return result


def _check_family(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    Obj(
        {"status": Str(choices=("available", "superseded", "unavailable")),
         "reason": Str(max_len=lim.MAX_REASON_LENGTH, text="evidence")},
        {"projection_path": Str(max_len=lim.MAX_BUNDLE_PATH_CHARS), "carried_from": SHA1,
         "impact_paths_sha256": SHA256},
    )(result, path)
    available = result["status"] == "available"
    check(available == ("projection_path" in result), f"{path}.projection_path",
          "is required exactly when status is 'available'")
    if available:
        check(g.is_bundle_path(result["projection_path"]) and result["projection_path"].endswith(".json"),
              f"{path}.projection_path", "must be a relative .json path inside output_dir")
    else:
        check("carried_from" not in result and "impact_paths_sha256" not in result, path,
              "an unavailable family records no carry-forward")
    if "carried_from" in result:
        check(result["carried_from"] != arguments["expected_coverage_sha"], f"{path}.carried_from",
              "must differ from expected_coverage_sha")
    return result


def _check_anchor_selection(result: Any, arguments: Mapping[str, Any], path: str) -> Any:
    """``None`` declines an anchor for this run; a selection must be exactly the expectation's
    declared ``anchor`` (SPEC §3.1), so an anchor can always be validated against its expectation."""

    if result is None:
        return None
    Obj({"artifact_nodes": List(Str(g.ARTIFACT_NODE, max_len=80), min_items=1, max_items=lim.MAX_LANES,
                                sorted_values=True)})(result, path)
    nodes = {lane["artifact_node"] for lane in arguments["expectation"]["lanes"]}
    for position, node in enumerate(result["artifact_nodes"]):
        check(node in nodes, f"{path}.artifact_nodes[{position}]", "is not an artifact node of the expectation")
    check(result == arguments["expectation"]["anchor"], f"{path}.artifact_nodes",
          "must equal the expectation's declared anchor (which is null or other nodes)")
    return result


#: Arguments of the test-only fixture hooks (``synthesize(ctx, target, expectation, out_root,
#: image_factory)``; ``image_factory`` is a Python callable, so it is passed beside these arguments
#: and never crosses a process boundary). A fixture hook returns ``None``.
FIXTURE_ARGUMENTS: dict[str, Validator] = {
    "synthesize": Obj({"target": TARGET, "expectation": _expectation, "out_root": _absolute_path}),
}

RESULTS = {
    "targets": _check_targets,
    "expectation": _check_expectation_result,
    "collect": _check_collect,
    "expected_source_jobs": _check_expected_jobs,
    "authenticate_extensions": _check_authenticated,
    "compose": _check_compose,
    "verify_publication": _check_verify_publication,
    "family_validate": _check_family,
    "anchor_selection": _check_anchor_selection,
}


def require_hook(hook: Any) -> str:
    if not isinstance(hook, str) or hook not in HOOKS:
        raise MbError(f"unknown adapter hook {hook!r}"[:120], reason="adapter-protocol")
    return hook


def require_fixture_hook(hook: Any) -> str:
    if not isinstance(hook, str) or hook not in FIXTURE_HOOKS:
        raise MbError(f"unknown fixture hook {hook!r}"[:120], reason="adapter-protocol")
    return hook


def validate_fixture_arguments(hook: str, arguments: Any) -> dict[str, Any]:
    """Validate the arguments of a test-only fixture hook (never a Pages hook)."""

    return FIXTURE_ARGUMENTS[require_fixture_hook(hook)](arguments, f"$.arguments<{hook}>")


def validate_fixture_result(hook: str, result: Any) -> None:
    """A fixture hook writes files and returns ``None``."""

    require_fixture_hook(hook)
    check(result is None, f"$.result<{hook}>", "must be null")


def validate_arguments(hook: str, arguments: Any) -> dict[str, Any]:
    """Validate the per-hook ``arguments`` object of a request."""

    return ARGUMENTS[require_hook(hook)](arguments, f"$.arguments<{hook}>")


def validate_result(hook: str, result: Any, arguments: Mapping[str, Any]) -> Any:
    """Validate a hook ``result`` against its strict schema and the request ``arguments``.

    Unknown keys, wrong types, bounds and the purely structural cross-checks (for example a
    ``collect`` result listing exactly the expectation's lane, frame and comparison ids in order)
    raise :class:`DocumentError`.
    """

    return RESULTS[require_hook(hook)](result, arguments, f"$.result<{hook}>")


_CONTEXT = Obj(
    {
        "repo_root": _absolute_path,
        "config_path": _absolute_path,
        "kit_src": _absolute_path,
        "tmpdir": _absolute_path,
        "implementation_sha": SHA1,
        "network": Bool(),
    }
)
_REQUEST = Obj(
    {
        "kind": Const(REQUEST_KIND),
        "schema_version": Const(1),
        "api": Int(1, 1000),
        "hook": Str(choices=sorted(HOOKS)),
        "context": _CONTEXT,
        "arguments": _any_json_object,
    }
)


def validate_request(document: Any) -> dict[str, Any]:
    """Validate a request envelope (including its per-hook arguments)."""

    _REQUEST(document, "$")
    check(document["api"] in ADAPTER_API_WINDOW, "$.api", f"must be in {sorted(ADAPTER_API_WINDOW)}")
    check(not document["context"]["network"] or document["hook"] in NETWORK_HOOKS, "$.context.network",
          "only a network hook may run with network access")
    validate_arguments(document["hook"], document["arguments"])
    return document


def validate_response(document: Any, *, hook: str, arguments: Mapping[str, Any]) -> Any:
    """Validate a response envelope for ``hook`` and return its checked ``result``.

    ``status == "unsupported"`` raises :class:`HookUnsupported`; ``"error"`` raises
    :class:`HookFailed` carrying the child's bounded message.
    """

    Obj(
        {"kind": Const(RESPONSE_KIND), "schema_version": Const(1), "hook": Const(require_hook(hook)),
         "status": Str(choices=("ok", "unsupported", "error"))},
        {"result": lambda value, path: value, "error": Str(max_len=MAX_ERROR_CHARS, text="evidence")},
    )(document, "$")
    status = document["status"]
    check((status == "ok") == ("result" in document), "$.result", "is present exactly when status is 'ok'")
    check((status == "error") == ("error" in document), "$.error", "is present exactly when status is 'error'")
    if status == "unsupported":
        raise HookUnsupported(f"adapter does not implement hook {hook!r}")
    if status == "error":
        raise HookFailed(f"adapter hook {hook!r} failed: {document['error']}")
    return validate_result(hook, document["result"], arguments)


class HookUnsupported(MbError):
    """The adapter defines no such hook. Callers fail closed wherever the hook is required."""

    default_reason = "hook-unsupported"


class HookFailed(MbError):
    """The hook raised; the adapter's refusal is a fail-closed rejection."""

    default_reason = "hook-failed"


__all__ = [
    "ADAPTER_API_WINDOW",
    "ARGUMENTS",
    "CHILD_FLAGS",
    "DocumentError",
    "FIXTURE_ARGUMENTS",
    "FIXTURE_HOOKS",
    "FORBIDDEN_JOBS",
    "HOOKS",
    "HOOK_JOBS",
    "HookFailed",
    "HookUnsupported",
    "ImageFactory",
    "NETWORK_HOOKS",
    "PREPARE_EVIDENCE",
    "REQUEST_KIND",
    "RESPONSE_KIND",
    "RESULTS",
    "TOKEN_JOBS",
    "require_fixture_hook",
    "require_hook",
    "validate_arguments",
    "validate_fixture_arguments",
    "validate_fixture_result",
    "validate_request",
    "validate_response",
    "validate_result",
]
