"""What the worker-lifecycle tests share: a job's commands, a tested tree and a candidate checkout.

The unit tests (``tests/test_ci_lifecycle.py``) and the hosted account tests
(``tests/ci_linux_worker.py``) run the same ``ci`` commands on the synthetic mod of
``tests/ci_mod_harness.py``. Only the GitHub API is a fake; files, Git and processes are real.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands
from mod_base.build_ci.config import BUILD_CONFIG_PATH
from mod_base.github.fake import FakeGitHub
from tests import ci_mod_harness as h

GIT = "/usr/bin/git"
_GIT_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "HOME": "/nonexistent",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}


def git(repository: Path, *arguments: str) -> str:
    """Run one Git command in ``repository`` with a closed environment; return its output."""

    completed = subprocess.run((GIT, "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *arguments),
                               cwd=repository, env=_GIT_ENV, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, timeout=60, check=False)
    if completed.returncode != 0:
        raise AssertionError(f"git {arguments[0]} failed: {completed.stderr.decode(errors='replace')}")
    return completed.stdout.decode("utf-8").strip()


def commit_candidate(mod: Path, checkout: Path) -> tuple[str, str]:
    """Make ``checkout`` a Git checkout whose one commit holds a copy of ``mod``; return
    ``(commit, tree)``, the tested commit of a job that has this candidate checkout."""

    shutil.copytree(mod, checkout, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    git(checkout, "init", "-q")
    git(checkout, "add", "-A")
    git(checkout, "commit", "-q", "-m", "candidate")
    return git(checkout, "rev-parse", "HEAD"), git(checkout, "rev-parse", "HEAD^{tree}")


def retarget(api: FakeGitHub, pull: dict[str, Any], commit: str, tree: str) -> None:
    """Make ``commit`` (with ``tree``) the test merge of the fixture pull request."""

    api.add_commit(commit, tree, parents=[h.CONTROLLER_SHA, h.HEAD_SHA])
    h.seed_pull_request(api, {**pull, "merge_commit_sha": commit})


def seed_tested_tree(api: FakeGitHub, mod: Path, *, tree: str = h.TESTED_TREE,
                     replace: dict[str, dict[str, Any]] | None = None) -> None:
    """Seed the fake API with ``mod`` as the tested tree: one blob per file and one tree row per
    directory. ``replace`` overrides whole rows by path (a link, a submodule, another size)."""

    rows: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in mod.rglob("*") if item.is_file() and "__pycache__" not in item.parts):
        name = path.relative_to(mod).as_posix()
        data = path.read_bytes()
        rows[name] = {"path": name, "mode": "100644", "type": "blob", "sha": api.add_blob(data), "size": len(data)}
        parts = name.split("/")
        for count in range(1, len(parts)):
            parent = "/".join(parts[:count])
            rows[parent] = {"path": parent, "mode": "040000", "type": "tree", "sha": "9" * 40}
    rows.update(replace or {})
    api.add_tree(tree, [rows[name] for name in sorted(rows)])


def rewrite_config(mod: Path, **timeouts: int) -> None:
    """Write the protected Build config of the copy ``mod`` again: the hashes of its scripts as
    they are now, and other ``timeouts`` (a test keeps a hanging hook short)."""

    path = mod / BUILD_CONFIG_PATH
    document = json.loads(path.read_bytes())
    for entry in document["adapter"]["files"]:
        entry["sha256"] = hashlib.sha256((mod / entry["path"]).read_bytes()).hexdigest()
    document["timeouts"].update(timeouts)
    path.write_bytes(h.pretty(document))


class Commands:
    """Runs ``ci`` verbs of one job through the real entry point, with the fake API as the client."""

    def __init__(self, mod: Path, state: Path, api: FakeGitHub, environment: dict[str, str]) -> None:
        self.mod, self.state, self.api, self.environment = mod, state, api, environment
        #: The request budget of every API client a command built, in order.
        self.budgets: list[int | None] = []

    def run(self, verb: str, *arguments: str, state: Path | None = None) -> tuple[int, str, str]:
        """Run ``ci <verb>`` with the job's ``--repo``, ``--config`` and ``--state``; return the
        exit code, stdout and stderr."""

        def client(environ: Any, *, writable: bool = False, max_requests: int | None = None) -> FakeGitHub:
            if writable:
                raise AssertionError("a job step of the worker lifecycle asked for a writable API client")
            self.budgets.append(max_requests)
            return self.api

        argv = ["ci", verb, "--repo", str(self.mod), "--config", str(self.mod / "site" / "mod-base.json"),
                "--state", str(self.state if state is None else state), *arguments]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "environ", return_value=self.environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def subject(self, output: Path) -> None:
        """``ci subject`` of the fixture pull request; it must succeed."""

        result = self.run("subject", "--producer", "build", "--pr", "7", "--github-output", str(output))
        if result != (0, "", ""):
            raise AssertionError(f"ci subject failed: {result}")
