"""Child side of the adapter call (MB3): ``python3 -P -m mod_base.adapter.host_child``.

Accepts exactly ``--adapter PATH --hook NAME --request FILE --response FILE``; reads and validates
the request envelope, loads the adapter with :func:`load_adapter`, builds
:class:`mod_base.adapter.api.Context` (``api`` from ``GH_TOKEN``/``GITHUB_API_URL`` only when the
request grants network: read-only and capped at ``limits.MAX_PAGES_API_READS`` requests) and
dispatches through the same code as :func:`run_hook`, then writes one canonical response envelope
(``unsupported`` for :class:`~mod_base.adapter.protocol.HookUnsupported`; any other exception
becomes ``status: "error"`` with a bounded one-line message, which the parent raises as
:class:`~mod_base.adapter.protocol.HookFailed`).

:func:`run_hook` is also the frozen in-process seam of ``conformance``: it loads the adapter (and
the ``config.adapter.fixtures_path`` module) with :func:`load_adapter` in a process whose
``PYTHONPATH`` is ``host.adapter_pythonpath`` and calls :func:`run_hook` with a ``Context`` whose
``api`` is a :class:`mod_base.github.fake.FakeGitHub`, so network hooks run against the fake. So
that a simulation observes what production observes, :func:`run_hook` raises the adapter's own
exception (anything but ``HookUnsupported`` or an :class:`~mod_base.errors.MbError` that already
exits 2) as the same ``HookFailed`` (same message) that the isolated host raises for it.

Only the argv shape, the request and the child's own environment are trusted inputs here. A
malformed argv or request writes no response (the parent then fails closed on the missing
response); everything from loading the adapter onwards is reported in the response envelope. The
response is created exclusively (``O_EXCL|O_NOFOLLOW``), so a hook cannot plant it in advance.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

import mod_base
from mod_base.adapter import protocol
from mod_base.adapter.api import Context
from mod_base.adapter.protocol import HookUnsupported, ImageFactory
from mod_base.config import parse_config
from mod_base.errors import EXIT_REJECTED, MbError, exit_code_for, run_main, single_line
from mod_base.github.api import from_environment
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file, read_regular_file

OWNER = "MB3"
PROGRAM = "mod_base.adapter.host_child"
#: The private ``sys.modules`` namespace of loaded adapter files (never an importable package).
_MODULE_PREFIX = "_mod_base_hook_module_"


def _fail(message: str) -> MbError:
    return MbError(message, reason="adapter-protocol")


def load_adapter(path: Path) -> ModuleType:
    """Import the adapter file as an isolated module (never added to ``sys.modules`` under a
    package name that other code could import)."""

    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise _fail("the adapter path must be absolute and normalized")
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise _fail(f"cannot inspect the adapter module: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or candidate.suffix != ".py":
        raise _fail("the adapter module must be a regular .py file")
    name = _MODULE_PREFIX + hashlib.sha256(os.fsencode(candidate)).hexdigest()[:24]
    spec = importlib.util.spec_from_file_location(name, candidate)
    if spec is None or spec.loader is None:
        raise _fail("the adapter module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    # Dataclasses and typing resolve a class's module through sys.modules while the file executes;
    # the private name keeps it out of every importable namespace.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _adapter_api(adapter: ModuleType) -> None:
    api = getattr(adapter, "ADAPTER_API", None)
    if isinstance(api, bool) or not isinstance(api, int) or api not in protocol.ADAPTER_API_WINDOW:
        raise _fail(f"the adapter must declare ADAPTER_API in {sorted(protocol.ADAPTER_API_WINDOW)}")


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
    name raises before the module is touched.

    Failures surface as the isolated host reports them: ``HookUnsupported`` and every
    :class:`~mod_base.errors.MbError` that already exits 2 (a protocol violation, or a kit rejection
    the adapter raised) pass unchanged; any other exception from the adapter's code becomes
    :class:`~mod_base.adapter.protocol.HookFailed` whose bounded one-line message equals the one
    ``protocol.validate_response`` raises for the child's error (exit 2, never an internal error).
    That includes an ``MbError`` with another exit code (an adapter's ``Unavailable``,
    ``Superseded`` or ``ControllerSkew``): across the host it is a ``HookFailed`` too, so a
    simulation can never report a clean absence (exit 3) where production rejects the hook.
    ``KeyboardInterrupt`` and ``SystemExit`` are never wrapped."""

    try:
        return _dispatch(context, adapter, hook, arguments, image_factory=image_factory)
    except HookUnsupported:
        raise
    except MbError as exc:
        if exit_code_for(exc) == EXIT_REJECTED:
            raise
        raise protocol.HookFailed(f"adapter hook {hook!r} failed: {_error_text(exc)}") from exc
    except Exception as exc:  # noqa: BLE001 - the adapter's own exception is its fail-closed refusal
        raise protocol.HookFailed(f"adapter hook {hook!r} failed: {_error_text(exc)}") from exc


def _dispatch(context: Context, adapter: ModuleType, hook: str, arguments: Mapping[str, Any], *,
              image_factory: ImageFactory | None) -> Any:
    """:func:`run_hook` without the ``HookFailed`` mapping: the child reports the raw exception
    text, and the parent's ``protocol.validate_response`` adds the same ``HookFailed`` prefix."""

    if isinstance(hook, str) and hook in protocol.HOOKS:
        _adapter_api(adapter)
        checked = protocol.validate_arguments(hook, copy.deepcopy(dict(arguments)))
        function = getattr(adapter, hook, None)
        if not callable(function):
            raise HookUnsupported(f"adapter does not implement hook {hook!r}")
        result = function(context, **copy.deepcopy(checked))
        return protocol.validate_result(hook, result, checked)
    if isinstance(hook, str) and hook in protocol.FIXTURE_HOOKS:
        if not callable(image_factory):
            raise _fail(f"fixture hook {hook!r} needs an image_factory")
        checked = protocol.validate_fixture_arguments(hook, copy.deepcopy(dict(arguments)))
        function = getattr(adapter, hook, None)
        if not callable(function):
            raise HookUnsupported(f"fixtures module does not implement hook {hook!r}")
        result = function(context, **checked, image_factory=image_factory)
        protocol.validate_fixture_result(hook, result)
        return None
    raise _fail(f"unknown adapter hook {single_line(repr(hook), limit=80)}")


def _parse_argv(argv: Sequence[str]) -> dict[str, str]:
    """The fixed argv shape: exactly the four :data:`protocol.CHILD_FLAGS`, in order, once each."""

    if len(argv) != 2 * len(protocol.CHILD_FLAGS):
        raise _fail("the adapter child takes exactly --adapter, --hook, --request and --response")
    values: dict[str, str] = {}
    for position, flag in enumerate(protocol.CHILD_FLAGS):
        if argv[2 * position] != flag:
            raise _fail(f"argument {2 * position + 1} must be {flag}")
        value = argv[2 * position + 1]
        if not value or value.startswith("-") or "\x00" in value:
            raise _fail(f"{flag} needs a value")
        values[flag[2:]] = value
    protocol.require_hook(values["hook"])
    for name in ("adapter", "request", "response"):
        if not os.path.isabs(values[name]):
            raise _fail(f"--{name} must be an absolute path")
    return values


def _error_text(error: BaseException) -> str:
    """``error`` as one bounded printable line: an ``MbError``'s message, otherwise ``Type:
    message``. An adapter-defined exception whose text cannot be rendered (its ``__str__`` raises)
    is still reported, generically, instead of escaping the error path."""

    try:
        text = single_line(str(error) if isinstance(error, MbError) else f"{type(error).__name__}: {error}",
                           limit=protocol.MAX_ERROR_CHARS)
    except Exception:  # noqa: BLE001 - rendering runs the adapter's own __str__
        text = "an exception whose message cannot be rendered"
    return "".join("?" if "\ud800" <= character <= "\udfff" else character for character in text)


def _write_response(path: Path, envelope: dict[str, Any]) -> None:
    data = canonical_json(envelope)
    if len(data) > lim.MAX_ADAPTER_RESPONSE_BYTES:
        data = canonical_json({**{key: envelope[key] for key in ("kind", "schema_version", "hook")},
                               "status": "error",
                               "error": f"the hook result exceeds {lim.MAX_ADAPTER_RESPONSE_BYTES} bytes"})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _context(request: Mapping[str, Any], adapter_path: str) -> Context:
    context = request["context"]
    kit_src = Path(mod_base.__file__).resolve().parents[1]
    if Path(context["kit_src"]).resolve() != kit_src:
        raise _fail("the child does not run the kit named by the request")
    repo_root = Path(context["repo_root"])
    config_path = Path(context["config_path"])
    config = parse_config(read_regular_file(config_path, label="adapter config", max_bytes=lim.MAX_CONFIG_BYTES),
                          path=config_path.name)
    if Path(adapter_path) != repo_root.joinpath(*config.adapter["path"].split("/")):
        raise _fail("--adapter is not the configured adapter of the repository")
    api = None
    if context["network"]:
        # A network hook's reads count against the same per-process Pages budget as every kit
        # command's (SPEC §3.0: 160 reads); the client refuses the next request once it is spent.
        api = from_environment(os.environ, writable=False, max_requests=lim.MAX_PAGES_API_READS)
    return Context(repo_root=repo_root, config=config, tmpdir=Path(context["tmpdir"]),
                   implementation_sha=context["implementation_sha"], api=api)


def _serve(argv: Sequence[str]) -> int:
    values = _parse_argv(argv)
    hook = values["hook"]
    request, _ = read_json_file(Path(values["request"]), label="adapter request",
                                max_bytes=lim.MAX_ADAPTER_REQUEST_BYTES)
    protocol.validate_request(request)
    if request["hook"] != hook:
        raise _fail("--hook differs from the request's hook")
    envelope: dict[str, Any] = {"kind": protocol.RESPONSE_KIND, "schema_version": 1, "hook": hook}
    try:
        context = _context(request, values["adapter"])
        adapter = load_adapter(Path(values["adapter"]))
        result = _dispatch(context, adapter, hook, request["arguments"], image_factory=None)
        canonical_json(result)  # an unencodable result is the hook's error, reported below
        envelope.update(status="ok", result=result)
    except HookUnsupported:
        envelope.update(status="unsupported")
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - every hook failure becomes one bounded line
        envelope.update(status="error", error=_error_text(exc))
    _write_response(Path(values["response"]), envelope)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    return run_main(lambda: _serve(arguments), program=PROGRAM)


if __name__ == "__main__":
    raise SystemExit(main())
