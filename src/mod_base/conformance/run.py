"""``conformance``: the synthetic producer -> collect -> build -> refresh -> rotate simulation (MB10).

For each selected key: adapter ``targets``/``expectation`` on the real matrix and contract; the
fixtures hook ``synthesize`` (from ``config.adapter.fixtures_path``, never loaded by the Pages
host) writes a packaged-output tree in the mod's own format with
``image_factory = mod_base.imaging.png.pattern_png``; then ``prepare`` + ``validate --kind
handoff`` + anchor, ``compact`` + ``validate --bind-raw``, ``admit``/``select``/``download``/
``authenticate`` (and ``compose`` where applicable) against
:class:`mod_base.github.fake.FakeGitHub`, ``build`` into a temporary ``_site``, ``refresh``,
``rotate --dry-run``, optional families, and the front-end/data-gate assertions. Later
generations follow on the same simulated repository: a real rotation, documentation-only pushes
(``docs/mod-base-conformance/head-<n>.md``, which a family's carry-forward policy must treat as
documentation), carried family legs, the anchor successor grace and a same-head publication
interleaved with the previous generation's rotation, every rotation plan checked against an
independent oracle. The stages and every check are listed in
:mod:`mod_base.conformance._simulation` and :mod:`mod_base.conformance._generations`.

Process model (no ``sys.path`` edits anywhere in the kit): :func:`run_conformance` validates the
arguments, copies the mod into a private one-commit snapshot repository on its canonical branch
(:mod:`mod_base.conformance._snapshot`; the mod's own checkout is only read) and re-executes the
simulation as ``python3 -P -m mod_base.conformance.run ...`` in a child whose environment carries
``PYTHONPATH = host.adapter_pythonpath(invocation)`` (the kit ``src`` plus the mod's
``config.adapter.python_path``) and no GitHub credentials: it is built from scratch (``PATH`` limited
to the system directories plus those of this interpreter, ``git`` and ``node``, a private ``HOME``
and ``TMPDIR``, ``PYTHONHASHSEED=0``, ``PYTHONSAFEPATH``, ``PYTHONDONTWRITEBYTECODE`` and
``PYTHONNOUSERSITE``, or instead of the last, from v1.0.2, ``PYTHONUSERBASE`` naming this process's
user base when this process imports Pillow from its own user site: ``host.imaging_user_site``, the
rule the hook child follows too). The child (:func:`main`) loads the adapter and fixtures modules with
``host_child.load_adapter`` and drives every hook, network hooks included, in-process through
``host_child.run_hook`` with a ``Context`` whose ``api`` is the seeded ``FakeGitHub``.

The fixtures module must define ``synthesize``; the simulation also uses these optional
conformance functions of that module (each variant they enable is otherwise reported as skipped):

* ``family_bundle(ctx, family, key, target, expectation, producer, out_root, image_factory,
  outcome) -> None`` writes the mod's native family bundle of ``key`` produced by the ``producer``
  run (its ``RunRecord``) into the existing directory ``out_root``; ``outcome`` is ``available``
  (a clean generation of the subject) or one of the extra outcomes the module lists in
  ``FAMILY_OUTCOMES`` (``superseded``: contract drift; ``unavailable``: a lineage refusal).
  Required with ``--families`` when the configuration declares a family.
* ``delegated_extensions(ctx, target, tested_run) -> extensions`` returns the extension objects
  (among them ``config.source.delegated_reuse_extension``) that prove reuse of the tested run
  ``tested_run`` (``{id, run_attempt, path, event, head_branch, head_sha}``).
* ``selected_extensions(ctx, target, baseline) -> extensions`` returns extension objects that make
  the expectation ``selected``, to be composed with the published ``mb-baseline`` artifact
  ``baseline`` (``{id, name, digest}``).

Either extension function may instead return ``{"extensions": {...}, "responses": [{"path",
"params"?, "payload"}]}``: exact API bodies (paths of this repository only) the simulated GitHub
serves to the adapter's network hooks.

The report is canonical JSON on stdout: ``{keys: [...], families: [...], checks: int}`` plus the
simulated ``repository`` and ``kit``, the ``variants`` outcomes, the ``admission`` reasons observed,
the ``hooks`` exercised and ``site`` facts (among them ``max_job_reads``, the most API reads one
simulated Pages job made, ``listing_rereads``, the inconsistent artifact listings the refresh jobs
read again, and ``generations``: per later generation its head, Pages run, key routes, family legs
and planned rotation size). Any failed check exits 2.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any

from mod_base import cli
from mod_base.adapter import host
from mod_base.config import load_config
from mod_base.conformance._simulation import Settings, simulate
from mod_base.conformance._snapshot import git_executable, make_snapshot
from mod_base.conformance._world import pinned_workflows
from mod_base.errors import MbError, run_main, single_line
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, strict_loads
from mod_base.runtime import build_invocation

PROGRAM = "mod_base.conformance.run"
#: The whole simulation of the largest mod (Quick Skin's 17 keys, each produced at three heads by the
#: later generations) is expected to finish well within this.
CHILD_TIMEOUT_SECONDS = 3 * 3600
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_STDERR_CHARS = 1000


def _fail(message: str, reason: str = "conformance") -> MbError:
    return MbError(message, reason=reason)


def _real_directory(path: Path, label: str) -> Path:
    candidate = Path(os.path.abspath(path))
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise _fail(f"cannot inspect the {label}: {exc.strerror or exc}", reason="usage") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _fail(f"the {label} must be a real directory", reason="usage")
    return candidate


def _check_arguments(keys: Sequence[str] | None, all_keys: bool, families: bool) -> tuple[str, ...] | None:
    if not isinstance(all_keys, bool) or not isinstance(families, bool):
        raise _fail("all_keys and families must be booleans", reason="usage")
    if keys is None:
        return None
    if all_keys:
        raise _fail("--keys and --all are mutually exclusive", reason="usage")
    selected = tuple(grammar.require_key(key) for key in keys)
    if not selected or len(set(selected)) != len(selected):
        raise _fail("--keys needs distinct keys", reason="usage")
    return selected


def _child_environment(pythonpath: str, scratch: Path) -> dict[str, str]:
    directories = ["/usr/bin", "/bin", os.path.dirname(sys.executable), os.path.dirname(git_executable())]
    node = shutil.which("node")
    if node is not None:
        directories.append(os.path.dirname(node))
    path = ":".join(dict.fromkeys(directory for directory in directories if directory))
    home = scratch / "home"
    home.mkdir(mode=0o700)
    return {"PATH": path, "HOME": str(home), "TMPDIR": str(scratch), "LANG": "C.UTF-8", "PYTHONHASHSEED": "0",
            "PYTHONSAFEPATH": "1", "PYTHONDONTWRITEBYTECODE": "1",
            **(host.imaging_user_site() or {"PYTHONNOUSERSITE": "1"}), "PYTHONPATH": pythonpath}


def _remove_tree(path: Path) -> None:
    """Remove ``path`` recursively, making read-only directories and files writable first (git
    object stores, a tool's cache): an error left after that is raised."""

    def writable(directory: str, names: list[str]) -> None:
        for name in [None, *names]:
            target = directory if name is None else os.path.join(directory, name)
            try:
                info = os.lstat(target)
            except FileNotFoundError:
                continue
            if not stat.S_ISLNK(info.st_mode):
                os.chmod(target, stat.S_IMODE(info.st_mode) | stat.S_IRWXU)

    for directory, subdirectories, files in os.walk(path):
        writable(directory, subdirectories + files)
    shutil.rmtree(path)


@contextmanager
def _scratch(prefix: str) -> Iterator[Path]:
    """A private temporary directory that never hides the error of the code using it.

    ``TemporaryDirectory`` raises its own cleanup error from ``__exit__`` and so replaces the
    simulation's real failure (``ignore_cleanup_errors`` only hides it, also after a success, which
    leaks the scratch). Here a cleanup failure after an error is dropped, so the error propagates
    unchanged; after a success it is raised, as an ``MbError`` naming the directory.
    """

    path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    try:
        yield path
    except BaseException:
        try:
            _remove_tree(path)
        except OSError:
            pass
        raise
    try:
        _remove_tree(path)
    except OSError as exc:
        raise _fail(f"cannot remove the conformance scratch directory {path}: {exc.strerror or exc}") from exc


def _check_report(report: Any) -> dict[str, Any]:
    required = {"repository", "kit", "keys", "families", "checks", "variants", "admission", "hooks", "site"}
    if not isinstance(report, dict) or set(report) != required:
        raise _fail("the conformance child wrote a malformed report")
    checks = report["checks"]
    if (isinstance(checks, bool) or not isinstance(checks, int) or checks <= 0 or not isinstance(report["keys"], list)
            or not report["keys"] or not isinstance(report["families"], list)):
        raise _fail("the conformance child wrote a malformed report")
    return report


def run_conformance(*, repo: Path, keys: Sequence[str] | None, all_keys: bool, kit_root: Path,
                    families: bool) -> dict[str, Any]:
    """Run the simulation and return its nine-key report (``docs/ADAPTER.md`` "The conformance
    report"); any failed check raises :class:`mod_base.errors.MbError` (exit 2)."""

    selected = _check_arguments(keys, all_keys, families)
    source = _real_directory(repo, "mod repository")
    kit = _real_directory(kit_root, "kit root")
    if not (kit / "src" / "mod_base" / "__init__.py").is_file() or not (kit / "site").is_dir():
        raise _fail("--kit-root is not a mod-base kit checkout (src/mod_base and site/)", reason="usage")
    config = load_config(source)
    if not config.adapter.get("fixtures_path"):
        raise _fail("conformance needs config.adapter.fixtures_path (the synthesize fixtures module)")
    with _scratch("mod-base-conformance-") as work:
        snapshot = work / "repo"
        make_snapshot(source, snapshot, branch=config.canonical_branch, replace=pinned_workflows(config))
        invocation = build_invocation(snapshot, None, {}, root=kit)
        scratch = work / "tmp"
        scratch.mkdir(mode=0o700)
        argv = [sys.executable, "-P", "-m", PROGRAM, "conformance", "--repo", str(snapshot), "--kit-root", str(kit)]
        argv += ["--keys", ",".join(selected)] if selected is not None else ["--all"]
        if families:
            argv.append("--families")
        try:
            completed = subprocess.run(argv, env=_child_environment(host.adapter_pythonpath(invocation), scratch),
                                       cwd=work, stdin=subprocess.DEVNULL, capture_output=True,
                                       timeout=CHILD_TIMEOUT_SECONDS, check=False)
        except subprocess.TimeoutExpired as exc:
            raise _fail(f"the conformance simulation timed out after {CHILD_TIMEOUT_SECONDS} seconds") from exc
        if completed.returncode != 0:
            lines = completed.stderr.decode("utf-8", "replace").strip().splitlines()
            last = lines[-1] if lines else f"exit {completed.returncode}"
            detail = single_line(last.removeprefix(f"{PROGRAM}: "), limit=MAX_STDERR_CHARS)
            raise _fail(f"the simulation failed: {detail}")
        report = strict_loads(completed.stdout, label="conformance report", max_bytes=MAX_REPORT_BYTES)
    return _check_report(report)


def _simulate(argv: Sequence[str]) -> int:
    namespace = cli.build_parser("conformance").parse_args(list(argv))
    selected = _check_arguments(namespace.keys, namespace.all_keys, namespace.families)
    if namespace.kit_root is None:
        raise _fail("the conformance child needs --kit-root", reason="usage")
    scratch = Path(tempfile.mkdtemp(prefix="simulation-"))
    try:
        # Anything an adapter prints goes to stderr: stdout carries only the report.
        with redirect_stdout(sys.stderr):
            report = simulate(Settings(repo=namespace.repo, kit_root=namespace.kit_root, keys=selected,
                                       families=namespace.families, work=scratch.resolve()))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    sys.stdout.buffer.write(canonical_json(report))
    sys.stdout.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """The simulation child (see module docstring): ``python3 -P -m mod_base.conformance.run``
    with the same flags as the ``conformance`` command; writes the canonical JSON report to
    stdout and returns an exit code through ``errors.run_main``."""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] != ["conformance"]:
        arguments = ["conformance", *arguments]
    return run_main(lambda: _simulate(arguments), program=PROGRAM)


if __name__ == "__main__":
    raise SystemExit(main())
