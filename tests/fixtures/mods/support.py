"""Shared helpers for the synthetic fixture mods ``qs_like`` and ``bp_like`` (MB3).

A fixture mod is materialized as a fresh one-commit Git repository in a temporary directory, so
adapters read their matrix and contract as inert objects (``ctx.read_blob``) exactly as in a real
checkout. Its family producer workflows are materialized pinning the fixture kit
(:func:`pin_workflow`, :data:`KIT_SHA` at the kit's own version), as a real producer's checkout
does: ``family collect`` reads that pin at the producer commit (SPEC §1.8). The shipped fixture files
never reference mod-base themselves (``tests/test_workflow_pins.py``). :func:`environment` builds the GitHub environment of a run, :func:`invocation` the kit
``Invocation``, :func:`synthesize` the mod's own packaged output through its fixtures hook, and
:class:`InProcessHost` replaces ``mod_base.adapter.host.call`` by the in-process dispatch
(``host_child.run_hook``) with a :class:`mod_base.github.fake.FakeGitHub` as ``ctx.api``: the same
seam ``conformance`` uses to run network hooks without credentials. An adapter's own exception
surfaces there as the same ``protocol.HookFailed`` (same message) the isolated host raises, unless
it is ``HookUnsupported`` or an ``MbError`` that already exits 2 (``host_child.run_hook``).

Other units' tests may import this module (``from tests.fixtures.mods import support``).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mod_base
from mod_base.adapter import host, host_child
from mod_base.adapter.api import Context
from mod_base.evidence.expectation import tested_run_projection
from mod_base.imaging.png import pattern_png
from mod_base.model import grammar
from mod_base.model.documents import run_claim_from_environment
from mod_base.runtime import Invocation, build_invocation
from mod_base.workflow import PAGES_WORKFLOW_PATH, api_job_name, step_name

MODS = Path(__file__).resolve().parent
KIT_SHA = hashlib.sha1(b"mod-base fixture kit").hexdigest()  # noqa: S324 - a fixture commit id
REPOSITORIES = {"qs_like": "The-Plum-Team/qs-like", "bp_like": "The-Plum-Team/bp-like"}
SOURCE_WORKFLOW = ".github/workflows/on-demand-e2e.yml"
QS_KEYS = ("mc1.20.1", "mc26.3")
PAGES_RUN_ID = 9000
BASELINE_OWNER_RUN_ID = 8000
_GIT_DATE = "2026-09-01T12:00:00Z"


@dataclass(frozen=True)
class FixtureMod:
    name: str
    root: Path
    commit: str
    tree: str
    branch: str
    repository: str

    @property
    def subject(self) -> dict[str, str]:
        return {"branch": self.branch, "commit": self.commit, "tree": self.tree}


def git(root: Path, *arguments: str) -> str:
    """Run git in a test repository with no user or system configuration."""

    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(root), "LC_ALL": "C",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
                   "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
                   "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
                   "GIT_AUTHOR_DATE": _GIT_DATE, "GIT_COMMITTER_DATE": _GIT_DATE}
    completed = subprocess.run(["git", "-C", str(root), "-c", "commit.gpgsign=false", *arguments], check=True,
                               capture_output=True, env=environment, timeout=60)
    return completed.stdout.decode("utf-8").strip()


def pin_workflow(workflow: str, *, sha: str = KIT_SHA, version: str = mod_base.__version__) -> bytes:
    """A family producer workflow ``workflow`` whose one mod-base reference pins the kit at ``sha``."""

    return (f"# Materialized by tests/fixtures/mods/support.py: {workflow} pins the fixture kit.\n"
            f"name: Family producer\non:\n  workflow_dispatch:\npermissions: {{}}\njobs:\n  publish-evidence:\n"
            f"    runs-on: ubuntu-24.04\n    steps:\n"
            f"      - uses: The-Plum-Team/mod-base/actions/publish-family@{sha} # v{version}\n").encode("utf-8")


def pin_producers(root: Path) -> None:
    """Write every configured family producer workflow of the mod at ``root`` as :func:`pin_workflow`."""

    config = json.loads((root / "site" / "mod-base.json").read_text(encoding="utf-8"))
    for family in config.get("families", []):
        workflow = family["producer"]["workflow"]
        path = root.joinpath(*workflow.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(pin_workflow(workflow))


def materialize(name: str, destination: Path, *, branch: str = "master",
                mutate: Callable[[Path], None] | None = None) -> FixtureMod:
    """Copy fixture mod ``name`` into ``destination`` (a new directory), pin its family producers
    (:func:`pin_producers`, after ``mutate``) and commit it on ``branch``."""

    shutil.copytree(MODS / name, destination, ignore=shutil.ignore_patterns("__pycache__"))
    if mutate is not None:
        mutate(destination)
    pin_producers(destination)
    git(destination, "init", "-q", f"--initial-branch={branch}")
    git(destination, "add", "-A")
    git(destination, "commit", "-q", "-m", f"{name} fixture")
    return FixtureMod(name=name, root=destination.resolve(), commit=git(destination, "rev-parse", "HEAD"),
                      tree=git(destination, "rev-parse", "HEAD^{tree}"), branch=branch,
                      repository=REPOSITORIES[name])


def commit_on_branch(mod: FixtureMod, branch: str, changes: Mapping[str, bytes | None]) -> FixtureMod:
    """Commit ``changes`` (``None`` deletes) on a new ``branch`` forked from ``mod``'s commit and
    return that head; the working tree is restored to ``mod``'s branch afterwards."""

    git(mod.root, "checkout", "-q", "-b", branch, mod.commit)
    for relative, data in changes.items():
        path = mod.root / relative
        if data is None:
            git(mod.root, "rm", "-q", relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            git(mod.root, "add", relative)
    git(mod.root, "commit", "-q", "-m", f"change on {branch}")
    head = FixtureMod(name=mod.name, root=mod.root, commit=git(mod.root, "rev-parse", "HEAD"),
                      tree=git(mod.root, "rev-parse", "HEAD^{tree}"), branch=branch, repository=mod.repository)
    git(mod.root, "checkout", "-q", mod.branch)
    return head


def environment(mod: FixtureMod, *, run_id: int = 4242, attempt: int = 1, event: str = "workflow_dispatch",
                sha: str | None = None, branch: str | None = None, job: str | None = None,
                workflow: str = SOURCE_WORKFLOW, token: str | None = None) -> dict[str, str]:
    """The GitHub environment of one job of ``workflow`` (default: the mod's source workflow)."""

    branch = branch or mod.branch
    environ = {
        "GITHUB_REPOSITORY": mod.repository,
        "GITHUB_SHA": sha or mod.commit,
        "GITHUB_RUN_ID": str(run_id),
        "GITHUB_RUN_ATTEMPT": str(attempt),
        "GITHUB_REF": f"refs/heads/{branch}",
        "GITHUB_REF_NAME": branch,
        "GITHUB_WORKFLOW_REF": f"{mod.repository}/{workflow}@refs/heads/{branch}",
        "GITHUB_EVENT_NAME": event,
        "MOD_BASE_KIT_SHA": KIT_SHA,
    }
    if job is not None:
        environ["GITHUB_JOB"] = job
    if token is not None:
        environ["GH_TOKEN"] = token
    return environ


def invocation(mod: FixtureMod, environ: Mapping[str, str], *, implementation_sha: str | None = None) -> Invocation:
    return build_invocation(mod.root, None, environ, implementation_sha=implementation_sha)


def claims(environ: Mapping[str, str], *, subject: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(handoff, tested)`` claims of a direct run: the handoff run tested ``subject`` itself."""

    handoff = run_claim_from_environment(environ)
    tested = {**handoff, "branch": subject["branch"], "commit": subject["commit"]}
    return handoff, tested


def _context(invocation_: Invocation, tmpdir: Path, api: Any = None) -> Context:
    return Context(repo_root=invocation_.repo_root, config=invocation_.config, tmpdir=tmpdir,
                   implementation_sha=invocation_.implementation_sha, api=api)


def run_in_process(invocation_: Invocation, hook: str, arguments: Mapping[str, Any], *, api: Any = None,
                   module: str = "path") -> Any:
    """Dispatch one hook in this process (``module`` is ``"path"`` or ``"fixtures_path"``)."""

    adapter = host_child.load_adapter(invocation_.repo_root / invocation_.config.adapter[module])
    with tempfile.TemporaryDirectory(prefix="mb-fixture-hook-") as tmpdir:
        return host_child.run_hook(_context(invocation_, Path(tmpdir).resolve(), api), adapter, hook, arguments,
                                   image_factory=pattern_png if module == "fixtures_path" else None)


def synthesize(invocation_: Invocation, key: str, subject: Mapping[str, str], out_root: Path, *,
               tested: Mapping[str, Any] | None = None, event: str = "workflow_dispatch") -> dict[str, Any]:
    """Write the mod's synthetic packaged output of ``key`` into the new directory ``out_root``
    (through the fixtures module's ``synthesize``) and return the expectation it covers."""

    branches = None
    if invocation_.config.targets["mode"] == "enrolled-branches":
        branches = [{"name": subject["branch"], "commit": subject["commit"], "tree": subject["tree"]}]
    targets = run_in_process(invocation_, "targets", {"branches": branches})
    target = next(item for item in targets if item["key"] == key)
    projection = tested_run_projection(tested or {"branch": subject["branch"]}, event)
    expectation = run_in_process(invocation_, "expectation", {"target": target, "tested_run": projection,
                                                              "extensions": {}})
    out_root.mkdir(parents=True)
    run_in_process(invocation_, "synthesize", {"target": target, "expectation": expectation,
                                               "out_root": str(out_root.resolve())}, module="fixtures_path")
    return expectation


@dataclass
class InProcessHost:
    """A stand-in for ``mod_base.adapter.host.call`` that dispatches in-process (the conformance
    seam), handing ``api`` to network hooks. It applies the production placement rules first
    (``host.check_placement``: SPEC §4.3 rows, forbidden jobs, network gating), so a test running a
    hook in the wrong job fails exactly as production would, and a hook's own exception is mapped
    by ``run_hook`` as the module docstring says. ``calls`` records ``(hook, network)``."""

    api: Any = None
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def __call__(self, invocation_: Invocation, hook: str, arguments: Mapping[str, Any], *,
                 network: bool = False) -> Any:
        host.check_placement(invocation_, hook, network=network)
        self.calls.append((hook, network))
        return run_in_process(invocation_, hook, arguments, api=self.api if network else None)


def pages_environment(mod: FixtureMod, *, job: str = "collect", run_id: int = PAGES_RUN_ID,
                      token: str | None = "fixture-read-only-token") -> dict[str, str]:
    """The environment of a Pages callee job (``publish.yml``) at the mod's protected head."""

    return environment(mod, run_id=run_id, event="workflow_dispatch", branch="master", job=job,
                       workflow=PAGES_WORKFLOW_PATH, token=token)


def run_record(claim: Mapping[str, Any], *, event: str, created_at: str = "2026-09-01T12:30:00Z",
               head_sha: str | None = None, display_title: str | None = None) -> dict[str, Any]:
    record = {**claim, "event": event, "created_at": created_at, "conclusion": "success",
              "head_sha": head_sha or claim["controller_sha"]}
    if display_title is not None:
        record["display_title"] = display_title
    return record


def selection_draft(manifest: Mapping[str, Any], manifest_raw: bytes, *, artifact_id: int, digest: str, size: int,
                    event: str = "workflow_dispatch", kind: str = "handoff", owner_run_id: int | None = None,
                    tested_run: Mapping[str, Any] | None = None, attestation_job: Mapping[str, Any] | None = None,
                    display_title: str | None = None, pages_run_id: int = PAGES_RUN_ID) -> dict[str, Any]:
    """A ``mod-base.selection`` draft for ``manifest`` as ``authenticate`` writes it in a Pages
    ``collect`` job of this fixture's protected head (the kit is :data:`KIT_SHA`)."""

    provenance = manifest["provenance"]
    handoff = provenance["handoff"]
    handoff_run = run_record(handoff, event=event, display_title=display_title)
    if tested_run is None:
        tested_run = {**handoff_run, **provenance["tested"]}
    if kind == "handoff":
        artifact = {"kind": "handoff", "name": grammar.handoff_name(manifest["key"], handoff["run_attempt"]),
                    "run_id": handoff["run_id"], "run_attempt": handoff["run_attempt"],
                    "workflow_path": handoff["workflow_path"]}
        binding = {"source": "workflow_file", "sha": manifest["kit"]["sha"]}
    else:
        artifact = {"kind": "cache", "name": grammar.cache_name(manifest["key"], provenance["coverage_sha"]),
                    "run_id": owner_run_id, "run_attempt": 1, "workflow_path": PAGES_WORKFLOW_PATH}
        binding = {"source": "referenced_workflows", "sha": manifest["kit"]["sha"]}
    source: dict[str, Any] = {"handoff_run": handoff_run, "tested_run": dict(tested_run),
                              "reuse": provenance["reuse"], "kit_binding": binding}
    if attestation_job is not None:
        source["attestation_job"] = dict(attestation_job)
    draft = {
        "kind": "mod-base.selection", "schema_version": 1, "repository": manifest["repository"],
        "key": manifest["key"],
        "kit": {"repository": mod_base.KIT_REPOSITORY, "sha": KIT_SHA, "version": mod_base.__version__},
        "implementation": {"branch": "master", "sha": manifest["subject"]["commit"],
                           "workflow_ref": f"{manifest['repository']}/{PAGES_WORKFLOW_PATH}@refs/heads/master",
                           "run_id": pages_run_id, "run_attempt": 1},
        "subject": manifest["subject"], "coverage_sha": provenance["coverage_sha"],
        "selected_artifact": {**artifact, "id": artifact_id, "digest": digest, "size": size,
                              "created_at": "2026-09-01T12:40:00Z"},
        "source": source,
        "expectation_sha256": manifest["expectation"]["sha256"],
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "extensions_verified": sorted(manifest["extensions"]["names"]) if manifest["extensions"] else [],
    }
    return draft


def zip_directory(root: Path) -> bytes:
    """A deterministic deflate ZIP of every file below ``root`` (an uploaded artifact)."""

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), date_time=(2026, 9, 1, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
    return stream.getvalue()


def seed_baseline(fake: Any, mod: FixtureMod, *, key: str, compact_root: Path, artifact_id: int,
                  tested_run_id: int, owner_run_id: int = BASELINE_OWNER_RUN_ID,
                  created_at: str = "2026-09-01T13:05:00Z", job_conclusion: str = "success",
                  step_window: tuple[str, str] = ("2026-09-01T13:04:00Z", "2026-09-01T13:06:00Z"),
                  workflow_path: str = PAGES_WORKFLOW_PATH) -> dict[str, Any]:
    """Seed ``fake`` with ``compact_root`` uploaded as ``mb-baseline--<key>--<commit>--<run>`` by a
    successful Pages run whose ``Finalize / Refresh evidence cache for <key>`` job retained it
    inside its "Retain the complete compact generation..." step; return the artifact reference."""

    name = grammar.baseline_name(key, mod.commit, tested_run_id)
    archive = zip_directory(compact_root)
    fake.add_run({"id": owner_run_id, "run_attempt": 1, "path": workflow_path, "event": "workflow_dispatch",
                  "status": "completed", "conclusion": "success", "head_branch": "master", "head_sha": mod.commit,
                  "head_repository": {"full_name": mod.repository}, "created_at": "2026-09-01T13:00:00Z",
                  "workflow_id": 77, "display_title": "Project site"})
    fake.add_jobs(owner_run_id, 1, [{
        "name": api_job_name("finalize", "refresh", key=key), "status": "completed", "conclusion": job_conclusion,
        "started_at": "2026-09-01T13:01:00Z", "completed_at": "2026-09-01T13:07:00Z",
        "steps": [{"name": step_name("baseline_upload"), "status": "completed", "conclusion": "success",
                   "number": 5, "started_at": step_window[0], "completed_at": step_window[1]}]}])
    fake.add_artifact({"id": artifact_id, "name": name, "created_at": created_at,
                       "workflow_run": {"id": owner_run_id, "head_branch": "master", "head_sha": mod.commit}},
                      archive)
    return {"id": artifact_id, "name": name, "digest": "sha256:" + hashlib.sha256(archive).hexdigest()}
