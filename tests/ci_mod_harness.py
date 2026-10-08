"""Run the synthetic mod's Build hooks as plain processes: no accounts, no sudo, only files.

``tests/fixtures/ci_mod`` is a tiny fake mod repository: a release inventory and a scenario
contract (two targets, one of them with two loaders, so three lanes), a ``gradle.properties`` that
holds the mod version (the Build config's one extra plan input), the protected Build config, the
adapter with its dispatcher and policy suite, and the minimum a Pages config needs. Its adapter
module documents how a test makes any hook fail on purpose (``faults`` in the inventory).

:class:`Sandbox` lays a temporary worker root out like the real one and runs one hook in it with
the argv of ``adapter.hook_command`` and the environment ``worker.worker_environment`` builds
(only its fixed worker root is replaced by the temporary one). What protected code does around a
hook (staging inputs, freezing an export into a sealed directory) is done here with plain copies,
so the contract is exercised end to end at the level of files.

After editing a script of the fixture, write its hashes into the fixture's Build config again::

    PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -c "from tests.ci_mod_harness import write_config; write_config()"
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import mod_base
from mod_base.build_ci import adapter, planning
from mod_base.build_ci.config import BUILD_CONFIG_PATH, BuildConfig, load_build_config
from mod_base.build_ci.protocol import BUILD_GRAPH_VERSION
from mod_base.build_ci.worker import WORKER_ROOT, worker_environment
from mod_base.github.fake import FakeGitHub
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_CALLER_WORKFLOWS

MOD = Path(__file__).resolve().parent / "fixtures" / "ci_mod"
REPOSITORY = "example/synthetic-mod"
BRANCH = "main"
CONTROLLER_SHA = "c" * 40
HEAD_SHA = "a" * 40
TESTED_SHA = "e" * 40
TESTED_TREE = "f" * 40
CONTROLLER_TREE = "d" * 40
KIT_SHA = "3" * 40
TARGETS = ("1.20.1", "1.21.1")
LANES = ("fabric-1.20.1", "forge-1.20.1", "fabric-1.21.1")
#: The name the fixture's Build config stages ``gradle.properties`` under (its one extra plan input).
PROPERTIES_INPUT = "gradle-properties"
_SCRIPTS = ("scripts/ci/mod_base_build_adapter.py", "scripts/ci/mod_base_build_dispatch.py",
            "scripts/ci/policy_suite.py")


def build_config() -> dict[str, Any]:
    """The fixture's protected Build config, with the hashes of the scripts as they are on disk."""

    return {
        "kind": "mod-base.build.config", "schema_version": 1, "repository": REPOSITORY, "profile": "quick-skin",
        "build_adapter_api": 1,
        "adapter": {"path": _SCRIPTS[0], "dispatcher": _SCRIPTS[1], "policy": _SCRIPTS[2],
                    "files": [{"path": name, "sha256": hashlib.sha256((MOD / name).read_bytes()).hexdigest()}
                              for name in _SCRIPTS]},
        "inventory": {"path": "release/inventory.json"},
        "scenario_contract": {"path": "e2e/scenario-contract.json"},
        "plan_inputs": [{"name": PROPERTIES_INPUT, "path": "gradle.properties"}],
        "bundle": {"path": "build/release"},
        "contexts": {"build": "Synthetic / Build and verify", "packaged": "Synthetic / Packaged E2E gate"},
        "timeouts": {"policy_seconds": 300, "target_seconds": 600, "runtime_seconds": 600, "validator_seconds": 300},
    }


def pretty(document: Any) -> bytes:
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")


def write_config() -> None:
    (MOD / BUILD_CONFIG_PATH).write_bytes(pretty(build_config()))


def materialize(destination: Path, *, faults: list[dict[str, Any]] | None = None) -> Path:
    """Copy the fixture mod to the new directory ``destination``; ``faults`` replaces the
    inventory's (empty) fault list, which is how a test asks a hook to fail."""

    shutil.copytree(MOD, destination, ignore=shutil.ignore_patterns("__pycache__"))
    if faults is not None:
        path = destination / "release" / "inventory.json"
        path.write_bytes(pretty({**json.loads(path.read_bytes()), "faults": faults}))
    return destination


def subject(*, pull_request: bool = True) -> dict[str, Any]:
    """A subject of the fixture repository as ``ci subject`` would authenticate it."""

    head, tested = (HEAD_SHA, TESTED_SHA) if pull_request else (CONTROLLER_SHA, CONTROLLER_SHA)
    return {
        "repository": REPOSITORY, "source_repository": REPOSITORY, "pr_number": 7 if pull_request else 0,
        "head_sha": head, "head_branch": "feature/synthetic" if pull_request else BRANCH,
        "base_sha": CONTROLLER_SHA, "base_branch": BRANCH,
        "controller_sha": CONTROLLER_SHA, "controller_workflow": CI_CALLER_WORKFLOWS["build"],
        "controller_ref": grammar.workflow_ref(REPOSITORY, CI_CALLER_WORKFLOWS["build"], BRANCH),
        "kit": {"repository": mod_base.KIT_REPOSITORY, "sha": KIT_SHA, "version": mod_base.__version__,
                "tree_digest": "sha256:" + "4" * 64},
        "tested_sha": tested, "tested_tree": TESTED_TREE if pull_request else CONTROLLER_TREE,
        "tested_parents": [CONTROLLER_SHA, HEAD_SHA] if pull_request else ["b" * 40],
        "graph_version": BUILD_GRAPH_VERSION,
    }


def environment(*, event: str = "pull_request_target", caller: str = "build") -> dict[str, str]:
    """The GitHub environment of a job of the fixture repository, run from a managed caller."""

    return {
        "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": CONTROLLER_SHA, "GITHUB_EVENT_NAME": event,
        "GITHUB_REF": f"refs/heads/{BRANCH}", "GITHUB_REF_NAME": BRANCH,
        "GITHUB_WORKFLOW_REF": grammar.workflow_ref(REPOSITORY, CI_CALLER_WORKFLOWS[caller], BRANCH),
        "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "1", "MOD_BASE_KIT_SHA": KIT_SHA, "GH_TOKEN": "fixture-token",
    }


def github(*, max_requests: int | None = None) -> tuple[FakeGitHub, dict[str, Any]]:
    """A fake GitHub holding the fixture repository with one ready pull request (number 7) whose
    test merge has the parents ``[base, head]``; returns it with the seeded pull request record,
    which a test changes and seeds again (:func:`seed_pull_request`)."""

    api = FakeGitHub(repository=REPOSITORY, default_branch=BRANCH, max_requests=max_requests)
    api.set_branch(BRANCH, CONTROLLER_SHA, CONTROLLER_TREE)
    api.add_commit(CONTROLLER_SHA, CONTROLLER_TREE, parents=["b" * 40])
    api.add_commit(TESTED_SHA, TESTED_TREE, parents=[CONTROLLER_SHA, HEAD_SHA])
    pull = {"number": 7, "state": "open", "draft": False, "mergeable": True, "merge_commit_sha": TESTED_SHA,
            "head": {"sha": HEAD_SHA, "ref": "feature/synthetic", "repo": {"full_name": REPOSITORY}},
            "base": {"sha": CONTROLLER_SHA, "ref": BRANCH, "repo": {"full_name": REPOSITORY}}}
    seed_pull_request(api, pull)
    return api, pull


def seed_pull_request(api: FakeGitHub, pull: dict[str, Any]) -> None:
    api.add_response(f"/repos/{REPOSITORY}/pulls/{pull['number']}", pull)


def files(root: Path) -> set[str]:
    """Every regular file below ``root`` by its relative POSIX path (nothing when it is absent)."""

    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()} if root.is_dir() else set()


class Sandbox:
    """A temporary worker root holding the protected adapter copy and a candidate checkout."""

    def __init__(self, directory: Path, *, protected: Path = MOD, candidate: Path | None = None) -> None:
        self.root = directory / "worker"
        self.config: BuildConfig = load_build_config(protected, repository=REPOSITORY)
        self.subject = subject()
        self.plan: dict[str, Any] | None = None
        self.checkout = {role: self.root / name for role, name in adapter.CHECKOUT_DIRECTORY.items()}
        self.home = {role: self.root / name for role, name in adapter.HOME_DIRECTORY.items()}
        self.inputs = self.root / adapter.INPUT_DIRECTORY
        self.validation = self.home["validator"] / adapter.OUTPUT_DIRECTORY
        self.export = self.home["candidate"] / adapter.EXPORT_DIRECTORY
        self.sealed_build = self.root / adapter.SEALED_BUILD_DIRECTORY
        self.sealed_runtime = self.root / adapter.SEALED_RUNTIME_DIRECTORY
        temporary = [home / "tmp" for home in self.home.values()]
        for path in (self.inputs, self.sealed_build, self.sealed_runtime, *temporary):
            path.mkdir(parents=True)
        # The validator sees the protected adapter and nothing else: the config and the closure it lists.
        closure = [(file.path, file.data) for file in self.config.files]
        for name, data in ((BUILD_CONFIG_PATH, self.config.raw), *closure):
            target = self.checkout["validator"] / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        shutil.copytree(protected if candidate is None else candidate, self.checkout["candidate"],
                        ignore=shutil.ignore_patterns("__pycache__"))

    def candidate_sources(self) -> dict[str, bytes]:
        """Bytes of every candidate file the plan is derived from, by its name in
        ``validation-input/``: what the protected config names, read from the tested checkout."""

        return {name: (self.checkout["candidate"] / path).read_bytes()
                for name, path in adapter.plan_sources(self.config.data).items()}

    def identity(self) -> dict[str, Any]:
        """The identity of the hook environment: the subject until the plan exists (``derive_plan``
        runs for it), the plan's complete identity afterwards."""

        return self.subject if self.plan is None else self.plan["identity"]

    def run(self, hook: str, *, unit_id: str | None = None, runtime: dict[str, str] | None = None,
            timeout: float = 60.0) -> subprocess.CompletedProcess[bytes]:
        """Run one hook exactly as the contract says, as this user, and return the finished process."""

        role = adapter.HOOKS[hook].role
        command = adapter.hook_command(hook, python=sys.executable, checkout=str(self.checkout[role]),
                                       dispatcher=self.config.data["adapter"]["dispatcher"])
        pairs = worker_environment(role=role, python=sys.executable, java_home=None, identity=self.identity(),
                                   run_id=42, run_attempt=1,
                                   values=adapter.hook_values(hook, unit_id=unit_id, runtime=runtime))
        environment = {name: value.replace(str(WORKER_ROOT), str(self.root))
                       for name, value in (pair.split("=", 1) for pair in pairs)}
        return subprocess.run(command, cwd=self.checkout[role], env=environment, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, check=False)

    def stage_inputs(self) -> None:
        """What ``ci plan`` stages first: the bytes of every candidate file, each under its name."""

        for name, data in self.candidate_sources().items():
            (self.inputs / name).write_bytes(data)

    def derive_plan(self) -> dict[str, Any]:
        """Stage the inputs, run ``derive_plan``, build the plan and stage it for the later hooks."""

        self.stage_inputs()
        process = self.run("derive_plan")
        if process.returncode != 0 or files(self.validation) != set(adapter.hook_outputs("derive_plan")):
            raise AssertionError(f"derive_plan failed: {process.stdout!r}")
        sources = self.candidate_sources()
        self.plan = planning.build_plan(subject=self.subject, config=self.config,
                                        inventory=sources.pop(adapter.INVENTORY_INPUT),
                                        scenario_contract=sources.pop(adapter.SCENARIO_INPUT), plan_inputs=sources,
                                        derived=(self.validation / adapter.PLAN_OUTPUT).read_bytes())
        (self.inputs / adapter.PLAN_INPUT).write_bytes(canonical_json(self.plan))
        shutil.rmtree(self.validation)
        return copy.deepcopy(self.plan)

    def seal(self, source: Path, sealed: Path) -> None:
        """Freeze an export: move its files into a sealed directory (a union when called again)."""

        shutil.copytree(source, sealed, dirs_exist_ok=True)
        shutil.rmtree(source)

    def build(self, target_id: str) -> None:
        """Build one target and seal its export; the export must be exactly the planned files."""

        process = self.run("build_target", unit_id=target_id)
        if process.returncode != 0 or files(self.export) != set(adapter.target_outputs(self.plan, target_id)):
            raise AssertionError(f"build_target {target_id} failed: {process.stdout!r}")
        self.seal(self.export, self.sealed_build)

    def run_lane(self, lane_id: str) -> None:
        """Derive a lane's runtime values, stage the sealed Build where its checkout expects it,
        run the lane and seal its results."""

        process = self.run("derive_runtime", unit_id=lane_id)
        if process.returncode != 0 or files(self.validation) != {adapter.RUNTIME_OUTPUT}:
            raise AssertionError(f"derive_runtime {lane_id} failed: {process.stdout!r}")
        runtime = adapter.parse_runtime_values((self.validation / adapter.RUNTIME_OUTPUT).read_bytes())
        shutil.rmtree(self.validation)
        bundle = self.checkout["candidate"] / self.config.data["bundle"]["path"]
        if not bundle.exists():
            shutil.copytree(self.sealed_build, bundle)
        process = self.run("run_lane", unit_id=lane_id, runtime=runtime)
        if process.returncode != 0:
            raise AssertionError(f"run_lane {lane_id} failed: {process.stdout!r}")
        self.seal(self.export, self.sealed_runtime)
