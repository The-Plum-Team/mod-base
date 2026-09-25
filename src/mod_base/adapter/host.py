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

as one ``subprocess`` without a shell, with the argv allowlist above, ``config.adapter.timeout_seconds``
and a 16 MiB response read through strict JSON; the response is validated by
``protocol.validate_response``. Only :data:`mod_base.adapter.protocol.HOOKS` go through ``call``;
fixture hooks never do (``conformance`` dispatches them in-process through
``host_child.run_hook``). Unsupported hooks raise ``protocol.HookUnsupported``; hook
failures, timeouts, oversize output or malformed responses raise :class:`mod_base.errors.MbError`.

Layout of one call: a fresh ``0700`` call directory holds ``req.json``, ``resp.json``, a copy of
the validated config (``mod-base.json``, canonical bytes, so the child never re-reads a file that
could have changed since the parent validated it) and the hook's private ``<tmp>`` (``TMPDIR``,
``ctx.tmpdir``) with ``<tmp>/home``; request and response stay outside the directory the hook may
scribble in. The child's combined stdout/stderr is read through a pipe: at most
:data:`MAX_CHILD_OUTPUT_BYTES` (more kills the child and fails the call), of which only a short
tail is kept for the error message, so a chatty hook can never fill the runner's disk. The child
runs in its own session: on timeout or overflow the whole process group is killed, and after every
call any process the hook left behind is killed too. The call directory is removed afterwards.

Where hooks may run (SPEC §4.3, :func:`check_placement`) is enforced before any child starts. No
hook ever runs when ``$GITHUB_JOB`` is one of ``protocol.FORBIDDEN_JOBS`` (the write-scoped and
caller-owned jobs). A job is a Pages callee job only when ``$GITHUB_JOB`` is one of
``protocol.TOKEN_JOBS`` *and* ``$GITHUB_WORKFLOW_REF`` names this repository's
``.github/workflows/pages.yml`` (a reusable workflow's jobs carry the caller's workflow ref), so a
mod-owned job that happens to be called ``build`` or ``collect`` is not mistaken for one; every
other job (and a local run without ``$GITHUB_JOB``) is the ``prepare-evidence`` composite. The hook
must be allowed there by ``protocol.HOOK_JOBS``. The read-only token is only ever handed to a hook
in ``config.adapter.network_hooks`` whose ``call`` asked for it and which runs in a Pages callee job
that ``protocol.HOOK_JOBS`` allows for it. The host never reads ``os.environ``: the token,
repository and job come from the :class:`~mod_base.runtime.Invocation` snapshot.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import ADAPTER_API
from mod_base.adapter import protocol
from mod_base.errors import MbError, single_line
from mod_base.github.api import DEFAULT_BASE_URL
from mod_base.model import grammar
from mod_base.model import limits as lim
from mod_base.model.canonical import canonical_json, read_json_file
from mod_base.runtime import Invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH

OWNER = "MB3"
CHILD_MODULE = "mod_base.adapter.host_child"
BASE_PATH = "/usr/bin:/bin"

REQUEST_NAME = "req.json"
RESPONSE_NAME = "resp.json"
CONFIG_NAME = "mod-base.json"
TMP_NAME = "tmp"
#: The child's combined stdout/stderr bound; a hook that prints more is killed and fails.
MAX_CHILD_OUTPUT_BYTES = 4 * 1024 * 1024
#: How much of the child's output tail an error message quotes.
_LOG_TAIL_BYTES = 2048
_READ_CHUNK = 64 * 1024
#: How often the parent re-checks the child while waiting for output.
_POLL_SECONDS = 0.2
#: After the child exits (and its process group is killed), how long the parent keeps draining a
#: pipe that an escaped descendant may still hold open.
_DRAIN_SECONDS = 2.0


def _fail(message: str) -> MbError:
    return MbError(message, reason="adapter-host")


def _python() -> str:
    """The interpreter the child runs: this one, unresolved (a venv's ``python`` symlink must stay
    a symlink so the child keeps the venv's locked site-packages)."""

    executable = sys.executable
    if not executable or not os.path.isabs(executable) or ":" in executable or "\x00" in executable:
        raise _fail("the adapter host needs an absolute python3 path without ':'")
    return executable


def _no_separator(path: Path, label: str) -> str:
    text = str(path)
    if ":" in text or "\x00" in text or "\n" in text:
        raise _fail(f"{label} cannot be placed on PYTHONPATH (it contains ':' or a control character)")
    return text


def _inside_repo(repo_root: Path, relative: str, *, kind: str, label: str) -> Path:
    """Resolve ``relative`` under ``repo_root`` component by component, refusing ``..``, ``.git``
    and any symlink component; the final entry must be of ``kind`` (``"dir"`` or ``"file"``)."""

    root = Path(os.path.abspath(repo_root))
    try:
        info = root.lstat()
    except OSError as exc:
        raise _fail(f"cannot inspect the mod repository: {exc.strerror or exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail("the mod repository root must be a real directory")
    if relative == "." and kind == "dir":
        return root
    if not grammar.is_repo_path(relative):
        raise _fail(f"{label} is not a repository-relative path without '..' or .git: {relative!r}"[:300])
    current = root
    for part in relative.split("/"):
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise _fail(f"{label} does not exist inside the repository: {relative!r}"[:300]) from exc
        if stat.S_ISLNK(info.st_mode):
            raise _fail(f"{label} crosses a symlink: {relative!r}"[:300])
    wanted = stat.S_ISDIR(info.st_mode) if kind == "dir" else stat.S_ISREG(info.st_mode)
    if not wanted:
        raise _fail(f"{label} must be a {'directory' if kind == 'dir' else 'regular file'}: {relative!r}"[:300])
    return current


def adapter_pythonpath(invocation: Invocation) -> str:
    """The child's ``PYTHONPATH``: ``<kit>/src`` then each ``config.adapter.python_path`` entry
    resolved inside the repository (no ``..``, no symlink component), joined with ``:``.

    ``conformance`` runs its in-process simulation in a child process started with exactly this
    ``PYTHONPATH`` (an environment value, never a ``sys.path`` edit), so the adapter and fixtures
    modules import their mod's code the same way the isolated hook child does."""

    kit_src = Path(os.path.abspath(invocation.kit_src))
    if not kit_src.is_dir() or not (kit_src / "mod_base" / "__init__.py").is_file():
        raise _fail("the kit source directory does not hold the mod_base package")
    entries = [_no_separator(kit_src, "the kit source directory")]
    for entry in invocation.config.adapter["python_path"]:
        path = _inside_repo(invocation.repo_root, entry, kind="dir", label="adapter.python_path entry")
        entries.append(_no_separator(path, "an adapter.python_path entry"))
    return ":".join(entries)


def _pages_job(invocation: Invocation) -> str | None:
    """``$GITHUB_JOB`` when this process is a ``publish.yml`` callee job: a ``protocol.TOKEN_JOBS``
    id under this repository's ``pages.yml`` workflow ref; otherwise ``None``."""

    job = invocation.github_job
    if job not in protocol.TOKEN_JOBS:
        return None
    try:
        ref = grammar.parse_workflow_ref(invocation.environ.get("GITHUB_WORKFLOW_REF"))
    except MbError:
        return None
    if ref.path != PAGES_WORKFLOW_PATH or ref.repository != invocation.environ.get("GITHUB_REPOSITORY"):
        return None
    return job


def placement(invocation: Invocation) -> str:
    """Where this process runs in SPEC §4.3 terms: a Pages callee job id, or
    ``protocol.PREPARE_EVIDENCE`` for every mod-owned job (and a local run)."""

    return _pages_job(invocation) or protocol.PREPARE_EVIDENCE


def _token_allowed(invocation: Invocation, hook: str) -> bool:
    """A declared network hook in a read-only Pages callee job that SPEC §4.3 allows it in (SPEC
    §1.9 token gating)."""

    job = _pages_job(invocation)
    return (hook in protocol.NETWORK_HOOKS and hook in invocation.config.network_hooks
            and job is not None and job in protocol.HOOK_JOBS[hook])


def check_placement(invocation: Invocation, hook: str, *, network: bool = False) -> None:
    """Refuse ``hook`` where SPEC §4.3 does not allow it: in a ``protocol.FORBIDDEN_JOBS`` job, in
    a job outside its ``protocol.HOOK_JOBS`` row (see :func:`placement`), or with ``network`` where
    the read-only token may not be granted. Runs before any child starts; the in-process test host
    applies the same check."""

    protocol.require_hook(hook)
    if not isinstance(network, bool):
        raise _fail("network must be a boolean")
    job = invocation.github_job
    if job in protocol.FORBIDDEN_JOBS:
        raise _fail(f"adapter hook {hook!r} may never run in job {job!r}")
    where = placement(invocation)
    if where not in protocol.HOOK_JOBS[hook]:
        raise _fail(f"adapter hook {hook!r} may not run in {where!r} (SPEC §4.3 allows "
                    f"{sorted(protocol.HOOK_JOBS[hook])})")
    if network and not _token_allowed(invocation, hook):
        raise _fail(f"hook {hook!r} may not run with network access in job {job!r} "
                    "(it must be a declared network hook in a read-only Pages job that allows it)")


def _environment(invocation: Invocation, hook: str, tmpdir: Path, *, network: bool) -> dict[str, str]:
    tmp = Path(os.path.abspath(tmpdir))
    environment = {
        "PATH": f"{BASE_PATH}:{os.path.dirname(_python())}",
        "HOME": _no_separator(tmp / "home", "the hook home directory"),
        "TMPDIR": _no_separator(tmp, "the hook temporary directory"),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONSAFEPATH": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": adapter_pythonpath(invocation),
    }
    if network:
        if not _token_allowed(invocation, hook):
            raise _fail(f"hook {hook!r} may not receive a token in job {invocation.github_job!r}")
        token = invocation.token
        if not token or any(ord(character) <= 32 or ord(character) >= 127 for character in token):
            raise _fail(f"network hook {hook!r} needs the step's read-only GH_TOKEN")
        environment.update({"GH_TOKEN": token, "GITHUB_API_URL": DEFAULT_BASE_URL,
                            "GITHUB_REPOSITORY": invocation.repository})
    return environment


def child_environment(invocation: Invocation, hook: str, *, tmpdir: Path) -> dict[str, str]:
    """The exact ``env -i`` environment for ``hook`` (see module docstring); the token appears only
    for a declared network hook in a token job (a Pages callee job whose SPEC §4.3 row allows it)."""

    protocol.require_hook(hook)
    return _environment(invocation, hook, tmpdir, network=_token_allowed(invocation, hook) and bool(invocation.token))


def child_argv(invocation: Invocation, hook: str, *, request: Path, response: Path) -> list[str]:
    """The exact child argv: ``[python3, -P, -m, host_child, --adapter, A, --hook, H, --request, R,
    --response, S]``."""

    protocol.require_hook(hook)
    adapter = _inside_repo(invocation.repo_root, invocation.config.adapter["path"], kind="file", label="adapter.path")
    values = (str(adapter), hook, str(Path(os.path.abspath(request))), str(Path(os.path.abspath(response))))
    argv = [_python(), "-P", "-m", CHILD_MODULE]
    for flag, value in zip(protocol.CHILD_FLAGS, values):
        if "\x00" in value:
            raise _fail("a child argument holds a NUL character")
        argv.extend((flag, value))
    return argv


def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _remove(directory: Path) -> None:
    def make_writable(function: Any, path: str, _info: Any) -> None:
        try:
            os.chmod(path, 0o700)
            function(path)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(directory, onexc=make_writable)
    else:  # pragma: no cover - Python 3.11
        shutil.rmtree(directory, onerror=make_writable)


def _supervise(process: subprocess.Popen[bytes], timeout: int) -> tuple[int, bytes]:
    """Read the child's combined output (bounded, keeping only the tail) until it closes, then
    wait for its exit status, all within ``timeout`` seconds."""

    stream = process.stdout
    if stream is None:
        raise _fail("the adapter child has no output pipe")
    descriptor = stream.fileno()
    os.set_blocking(descriptor, False)
    deadline = time.monotonic() + timeout
    tail = b""
    total = 0
    exited_at: float | None = None
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        open_pipe = True
        while open_pipe:
            now = time.monotonic()
            if exited_at is None and process.poll() is not None:
                exited_at = now
                _kill_group(process.pid)  # descendants may still hold the pipe open
            if exited_at is not None and now - exited_at >= _DRAIN_SECONDS:
                break
            if exited_at is None and now >= deadline:
                raise _fail(f"adapter hook timed out after {timeout} seconds")
            wait = _POLL_SECONDS if exited_at is not None else min(_POLL_SECONDS, deadline - now)
            for _key, _events in selector.select(max(wait, 0.0)):
                try:
                    chunk = os.read(descriptor, _READ_CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    open_pipe = False
                    break
                total += len(chunk)
                if total > MAX_CHILD_OUTPUT_BYTES:
                    raise _fail(f"adapter hook wrote more than {MAX_CHILD_OUTPUT_BYTES} bytes of output")
                tail = (tail + chunk)[-_LOG_TAIL_BYTES:]
    try:
        status = process.wait(timeout=max(deadline - time.monotonic(), 0.0) if exited_at is None else None)
    except subprocess.TimeoutExpired:
        raise _fail(f"adapter hook timed out after {timeout} seconds") from None
    return status, tail


def _run_child(argv: list[str], environment: dict[str, str], *, cwd: Path, timeout: int) -> tuple[int, str]:
    """Run the child; return its exit status and a one-line tail of its output."""

    try:
        process = subprocess.Popen(argv, env=environment, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    except OSError as exc:
        raise _fail(f"cannot start the adapter child: {exc.strerror or exc}") from exc
    try:
        status, tail = _supervise(process, timeout)
    finally:
        _kill_group(process.pid)  # nothing the hook started may outlive the call
        process.wait()
        if process.stdout is not None:
            process.stdout.close()
    return status, single_line(tail.decode("utf-8", "replace"), limit=400) if tail else ""


def call(invocation: Invocation, hook: str, arguments: Mapping[str, Any], *, network: bool = False) -> Any:
    """Run ``hook`` with ``arguments`` in the isolated child and return its validated result.

    The hook must be allowed where this process runs (:func:`check_placement`). ``network=True``
    requests the read-only token; it is granted only when the hook is in
    ``config.adapter.network_hooks`` and runs in a Pages callee job that SPEC §4.3 allows it in,
    otherwise the call raises before starting the child.
    """

    check_placement(invocation, hook, network=network)
    checked = protocol.validate_arguments(hook, dict(arguments))
    timeout = invocation.config.adapter_timeout_seconds
    call_directory = Path(tempfile.mkdtemp(prefix="mb-hook-"))
    try:
        call_directory = call_directory.resolve()
        tmp = call_directory / TMP_NAME
        os.mkdir(tmp, 0o700)
        os.mkdir(tmp / "home", 0o700)
        config_path = call_directory / CONFIG_NAME
        request_path = call_directory / REQUEST_NAME
        response_path = call_directory / RESPONSE_NAME
        _write_private(config_path, canonical_json(invocation.config.data))
        request = {
            "kind": protocol.REQUEST_KIND,
            "schema_version": 1,
            "api": ADAPTER_API,
            "hook": hook,
            "context": {
                "repo_root": str(Path(os.path.abspath(invocation.repo_root))),
                "config_path": str(config_path),
                "kit_src": str(Path(os.path.abspath(invocation.kit_src))),
                "tmpdir": str(tmp),
                "implementation_sha": invocation.implementation_sha,
                "network": network,
            },
            "arguments": checked,
        }
        protocol.validate_request(request)
        encoded = canonical_json(request)
        if len(encoded) > lim.MAX_ADAPTER_REQUEST_BYTES:
            raise _fail(f"the {hook} request exceeds {lim.MAX_ADAPTER_REQUEST_BYTES} bytes")
        _write_private(request_path, encoded)
        argv = child_argv(invocation, hook, request=request_path, response=response_path)
        environment = _environment(invocation, hook, tmp, network=network)
        status, tail = _run_child(argv, environment, cwd=tmp, timeout=timeout)
        if status != 0 or not os.path.lexists(response_path):
            raise _fail(f"adapter child for hook {hook!r} exited with status {status} without a valid response"
                        + (f": {tail}" if tail else ""))
        document, _ = read_json_file(response_path, label=f"{hook} response", max_bytes=lim.MAX_ADAPTER_RESPONSE_BYTES)
        return protocol.validate_response(document, hook=hook, arguments=checked)
    finally:
        _remove(call_directory)


def _write_private(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
    finally:
        os.close(descriptor)
