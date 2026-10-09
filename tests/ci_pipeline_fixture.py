"""Execute callee jobs against one fake GitHub; every kit command and worker is real.

Bash resolves each workflow command's arguments before the CLI runs in this process. The
recording executable replaces only the CLI invocation, so shell guards, arrays, Git queries
and the selection hand-over run unchanged. Checkouts are detached real repositories.
"""

from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import __version__, cli, runtime, workflow
from mod_base.build_ci import commands
from tests import ci_lifecycle_fixture as fixture
from tests import ci_mod_harness as h
from tests.ci_request_budget import command_route, generation_breakdown, request_cost
from tests.test_workflow_ci_policy import CI_COMMAND, CI_INPUTS, ci_callee, step_verb
from tests.test_workflow_policy import ShellHarness, load_yaml, outputs

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r"\$\{\{ (.*?) \}\}")
ATOM = re.compile(r"(?:inputs\.[a-z-]+|needs\.[a-z]+\.outputs\.[a-z0-9-]+|steps\.[a-z]+\.(?:outputs\.[a-z0-9_]+|outcome)|matrix\.id|github\.(?:token|sha|run_id|run_attempt|event_name)|runner\.temp)")
FENCE_LAUNCHES: list[tuple[str, ...]] = []


def observe_root_launch(event: str, arguments: tuple[Any, ...]) -> None:
    """An audit observer counts real subprocess starts without replacing any system operation."""
    if event != "subprocess.Popen":
        return
    argv = arguments[1]
    if not isinstance(argv, (list, tuple)) or "--operation" not in argv:
        return
    index = argv.index("--operation")
    if index + 1 < len(argv) and argv[index + 1] == "host-fence" and any(
            str(value).endswith("/tools/ci_privileged_bootstrap.py") for value in argv):
        FENCE_LAUNCHES.append(tuple(str(value) for value in argv))


sys.addaudithook(observe_root_launch)


@dataclass
class Job:
    root: Path
    outputs: dict[str, str]
    listing: dict[str, Any]


@dataclass
class Captured:
    argv: list[str]
    environment: dict[str, str]
    workspace: Path
    api: Any
    jobs: list[dict[str, Any]]


class Pipeline:
    """One PR generation, retaining exactly the artifacts and outputs its jobs produced."""

    def __init__(self, test: Any, root: Path, api: Any, pull: dict[str, Any]) -> None:
        self.test, self.root, self.api = test, root, api
        self.clock = datetime(2026, 10, 8, tzinfo=timezone.utc)
        self.requests: list[tuple[str, str, int]] = []
        self.first_fence = len(FENCE_LAUNCHES)
        self.runs: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, list[dict[str, Any]]] = {}
        self.artifacts: dict[str, tuple[dict[str, Any], bytes]] = {}
        self.jobs: dict[str, Job] = {}
        self.captured: dict[tuple[str, str], Captured] = {}
        self.controls: list[tuple[str, int]] = []
        self.plan: dict[str, Any] | None = None
        self.next_job_id = 9000000
        self.shell = ShellHarness(root / "shell", stubs=("pipeline-command",))
        self.kit = root / "kit-source"
        shutil.copytree(ROOT, self.kit, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        fixture.git(self.kit, "init", "-q")
        fixture.git(self.kit, "add", "-A")
        fixture.git(self.kit, "commit", "-q", "-m", "kit under test")
        self.pin = fixture.git(self.kit, "rev-parse", "HEAD")
        self.mod = h.materialize(root / "mod-source")
        activation = {"kind": "mod-base.ci.activation", "schema_version": 1, "repository": h.REPOSITORY,
                      "profile": h.build_config()["profile"], "mode": "shared-build-and-e2e", "rollback_from": None}
        (self.mod / "site/mod-base-build-activation.json").write_bytes(h.pretty(activation))
        managed = [*workflow.CI_CALLER_WORKFLOWS.values(), ".github/workflows/mod-base-guard.yml"]
        for relative in managed:
            text = (ROOT / "template/managed" / relative).read_text(encoding="utf-8")
            text = text.replace("{{PIN}}", self.pin).replace("{{VERSION}}", "v" + __version__).replace("{{BRANCH}}", h.BRANCH)
            (self.mod / relative).write_text(text, encoding="utf-8", newline="\n")
        fixture.git(self.mod, "init", "-q")
        fixture.git(self.mod, "add", "-A")
        fixture.git(self.mod, "commit", "-q", "-m", "protected controller")
        self.controller = fixture.git(self.mod, "rev-parse", "HEAD")
        fixture.git(self.mod, "commit", "-q", "--allow-empty", "-m", "candidate head")
        self.head = fixture.git(self.mod, "rev-parse", "HEAD")
        self.tree = fixture.git(self.mod, "rev-parse", "HEAD^{tree}")
        self.tested = fixture.git(self.mod, "commit-tree", self.tree, "-p", self.controller,
                                  "-p", self.head, "-m", "synthetic test merge")
        self.pull = {**pull, "merge_commit_sha": self.tested,
                     "head": {**pull["head"], "sha": self.head},
                     "base": {**pull["base"], "sha": self.controller}}
        h.seed_pull_request(api, self.pull)
        api.set_branch(h.BRANCH, self.controller, self.tree)
        api.add_commit(self.controller, self.tree, parents=[])
        api.add_commit(self.head, self.tree, parents=[self.controller])
        api.add_commit(self.tested, self.tree, parents=[self.controller, self.head])
        rows = []
        for line in fixture.git(self.mod, "ls-tree", "-r", "-t", self.tree).splitlines():
            metadata, path = line.split("\t")
            mode, kind, sha = metadata.split()
            row = {"path": path, "mode": mode, "type": kind, "sha": sha}
            if kind == "blob":
                data = (self.mod / path).read_bytes()
                self.test.assertEqual(api.add_blob(data), sha)
                row["size"] = len(data)
            rows.append(row)
        api.add_tree(self.tree, rows)

    def tick(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def fences(self) -> int:
        return len(FENCE_LAUNCHES) - self.first_fence

    def job_id(self) -> int:
        self.next_job_id += 1
        return self.next_job_id

    def expression(self, expression: str, context: dict[str, str]) -> str:
        if ATOM.fullmatch(expression):
            if expression in context:
                return context[expression]
            if expression.startswith("steps."):
                return ""
            raise AssertionError(f"no executed job answers {expression!r}")
        choice = re.fullmatch(r"(.+?) == '([^']*)' && '([^']*)' \|\| '([^']*)'", expression)
        if choice:
            return choice[3] if self.expression(choice[1], context) == choice[2] else choice[4]
        alternative = re.fullmatch(r"(steps\.[a-z]+\.outputs\.[a-z_]+) \|\| (steps\.[a-z]+\.outputs\.[a-z_]+)", expression)
        if alternative:
            return self.expression(alternative[1], context) or self.expression(alternative[2], context)
        formatted = re.fullmatch(r"format\((.+), github.run_id, github.run_attempt\)", expression)
        if formatted:
            return self.expression(formatted[1], context).format(context["github.run_id"], context["github.run_attempt"])
        raise AssertionError(f"unsupported workflow expression: {expression!r}")

    def resolve(self, text: str, context: dict[str, str]) -> str:
        return REFERENCE.sub(lambda match: self.expression(match[1], context), text)

    def condition(self, condition: str | None, context: dict[str, str]) -> bool:
        if condition is None:
            return True
        expression = condition.removeprefix("${{ ").removesuffix(" }}")
        expression = expression.removeprefix("always() && ")
        match = re.fullmatch(r"(.+?) (==|!=) '([^']*)'", expression)
        if not match:
            raise AssertionError(f"unsupported workflow condition: {condition!r}")
        equal = self.expression(match[1], context) == match[3]
        return equal if match[2] == "==" else not equal

    def checkout(self, source: Path, destination: Path, sha: str) -> None:
        destination.mkdir()
        fixture.git(destination, "init", "-q")
        fixture.git(destination, "fetch", "-q", "--no-tags", source.as_uri(), sha)
        fixture.git(destination, "checkout", "-q", "--detach", "FETCH_HEAD")
        self.test.assertEqual(fixture.git(destination, "rev-parse", "HEAD"), sha)
        self.test.assertEqual(fixture.git(destination, "status", "--porcelain"), "")

    def begin(self, producer: str) -> None:
        callee = {"build": "build", "packaged": "packaged-e2e", "status": "gate-status"}[producer]
        run_id = {"build": 42, "packaged": 43, "status": 44}[producer]
        started = self.tick()
        run = {"id": run_id, "run_attempt": 1, "path": workflow.CI_CALLER_WORKFLOWS[producer],
               "name": producer, "event": "workflow_run" if producer == "status" else "pull_request_target",
               "status": "in_progress", "conclusion": None,
               "head_branch": self.pull["head"]["ref"], "head_sha": self.head,
               "head_repository": {"full_name": h.REPOSITORY}, "repository": {"full_name": h.REPOSITORY},
               "created_at": started, "run_started_at": started, "updated_at": started,
               "referenced_workflows": [
                   {"path": f"{h.REPOSITORY}/.github/workflows/mod-base-guard.yml@{self.controller}",
                    "sha": self.controller, "ref": f"refs/heads/{h.BRANCH}"},
                   {"path": f"The-Plum-Team/mod-base/.github/workflows/{callee}.yml@{self.pin}", "sha": self.pin}]}
        self.runs[producer], self.listings[producer] = run, []
        self.api.add_run(run)
        # Caller jobs are orchestration facts; callee jobs below are executed, never seeded.
        if producer != "status":
            caller = load_yaml(ROOT / "template/managed" / workflow.CI_CALLER_WORKFLOWS[producer])["jobs"]
            guard = load_yaml(ROOT / "template/managed/.github/workflows/mod-base-guard.yml")["jobs"]["verify"]
            for job_id, item in caller.items():
                if job_id == "shared":
                    continue
                name = item["name"]
                conclusion = "skipped"
                if job_id == "guard":
                    name += " / " + guard["name"]
                    conclusion = "success"
                self.listings[producer].append({"id": self.job_id(), "name": name, "conclusion": conclusion, "status": "completed",
                                                "started_at": self.tick(), "completed_at": self.tick(), "steps": []})
        self.api.add_jobs(run_id, 1, self.listings[producer])

    def invoke(self, argv: list[str], environment: dict[str, str], cwd: Path) -> tuple[int, str, str]:
        def client(environ: Any, *, writable: bool = False, max_requests: int | None = None) -> Any:
            self.test.assertFalse(writable)
            self.test.assertIsNotNone(max_requests)
            self.test.assertEqual(environ.get("GH_TOKEN"), "fixture-token", "the API token must come from this step's env")
            return self.api

        raw_stdout, stderr = io.BytesIO(), io.StringIO()
        stdout = io.TextIOWrapper(raw_stdout, encoding="utf-8", newline="\n", write_through=True)
        previous = Path.cwd()
        try:
            os.chdir(cwd)
            with mock.patch.object(cli, "environ", return_value=environment), \
                    mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(argv)
        finally:
            os.chdir(previous)
        stdout.flush()
        return code, raw_stdout.getvalue().decode("utf-8"), stderr.getvalue()

    def job(self, producer: str, job_id: str, *, needs: dict[str, Job] | None = None,
            unit: str | None = None) -> Job:
        callee = {"build": "build", "packaged": "packaged-e2e", "status": "gate-status"}[producer]
        definition = ci_callee(callee)["jobs"][job_id]
        label = f"{callee}/{job_id}" + (f"/{unit}" if unit else "")
        root = self.root / label
        workspace, temporary = root / "workspace", root / "temp"
        workspace.mkdir(parents=True)
        temporary.mkdir()
        run = self.runs[producer]
        inputs = {**dict.fromkeys(CI_INPUTS[callee], ""), "kit-sha": self.pin, "pr-number": "7"}
        context = {**{f"inputs.{key}": value for key, value in inputs.items()}, "matrix.id": unit or "",
                   "github.token": "fixture-token", "github.sha": self.controller, "github.event_name": run["event"],
                   "github.run_id": str(run["id"]), "github.run_attempt": "1", "runner.temp": str(temporary),
                   **{f"needs.{key}.outputs.{name}": value for key, job in (needs or {}).items() for name, value in job.outputs.items()}}
        environment = {**h.environment(caller=producer, event=run["event"]), "GITHUB_SHA": self.controller,
                       "GITHUB_RUN_ID": str(run["id"]),
                       "RUNNER_ENVIRONMENT": "github-hosted", "GITHUB_WORKSPACE": str(workspace),
                       "RUNNER_TEMP": str(temporary), **ci_callee(callee).get("env", {}),
                       **{key: value for key, value in os.environ.items() if key.startswith("JAVA_HOME_")}}
        environment.pop("MOD_BASE_KIT_SHA", None)
        environment.pop("GH_TOKEN", None)
        # Disposable rigs may provision these JDK roots; the hosted image's own paths win.
        for version in (17, 21, 25):
            fallback = Path(f"/opt/modbase-jdk-{version}")
            if fallback.is_dir():
                environment.setdefault(f"JAVA_HOME_{version}_X64", str(fallback))
        caller = load_yaml(ROOT / "template/managed" / workflow.CI_CALLER_WORKFLOWS[producer])["jobs"]
        prefix = caller["evaluate" if producer == "status" else "shared"]["name"]
        name = prefix + " / " + self.resolve(definition["name"], context)
        listing = {"id": self.job_id(), "name": name, "status": "completed", "conclusion": "success", "started_at": self.tick(), "steps": []}
        try:
            for position, item in enumerate(definition["steps"], 1):
                started, conclusion = self.tick(), "success"
                step_environment = {**environment, "GITHUB_OUTPUT": str(root / f"output-{position}"),
                                    "GITHUB_ENV": str(root / "github-env"),
                                    **{key: self.resolve(value, context) for key, value in item.get("env", {}).items()}}
                if not self.condition(item.get("if"), context):
                    conclusion = "skipped"
                elif item.get("uses", "").startswith("actions/checkout@"):
                    options = item["with"]
                    pin = self.resolve(options["ref"], context)
                    source = self.kit if options["path"] == "kit" else self.mod
                    self.checkout(source, workspace / options["path"], pin)
                elif item.get("uses", "").startswith("actions/upload-artifact@"):
                    self.upload(producer, item["with"], context)
                elif "run" in item:
                    verb = step_verb(item)
                    if verb:
                        command_match = CI_COMMAND.search(item["run"])
                        self.test.assertIsNotNone(command_match)
                        command_lines = item["run"][command_match.start():].rstrip().splitlines()
                        self.test.assertTrue(all(line.rstrip().endswith("\\") for line in command_lines[:-1]),
                                             f"{label}: ci must be the step's final shell command")
                    # Dependency installation belongs to the image/CI preparation, outside the command chain.
                    if "python3 -m pip install" not in item["run"]:
                        script = item["run"].replace("python3 -P -m mod_base", "pipeline-command")
                        result = self.shell.run(script, step_environment, cwd=workspace,
                                                record_env=tuple(dict.fromkeys((*runtime.ENVIRONMENT_NAMES, *step_environment))))
                        self.test.assertEqual(result.returncode, 0, f"{label}: {item['name']}: {result.stderr}")
                        recorded_commands = self.shell.records()
                        self.test.assertEqual(len(recorded_commands), int(verb is not None), label)
                        for recorded in recorded_commands:
                            self.test.assertEqual(recorded["tool"], "pipeline-command")
                            argv = recorded["argv"]
                            self.test.assertEqual(argv[:2], ["ci", verb])
                            before = self.api.request_count
                            command_environment = {key: value for key, value in recorded["env"].items() if value is not None}
                            self.test.assertEqual(command_environment.get("MOD_BASE_KIT_SHA"), self.pin,
                                                  f"{label}: binding must hand the verified pin through GITHUB_ENV")
                            if verb == "assemble" or (verb == "seal-gate" and producer == "packaged") or (
                                    verb == "subject" and producer == "build" and job_id == "plan"):
                                self.captured[label, verb] = Captured(argv, command_environment, workspace,
                                                                      copy.deepcopy(self.api), copy.deepcopy(self.listings[producer]))
                            code, stdout, stderr = self.invoke(argv, command_environment, workspace)
                            count = self.api.request_count - before
                            self.requests.append((label, verb, count))
                            self.test.assertEqual((code, stderr), (0, ""), f"{label} / {verb}: {stdout}\n{stderr}")
                            if verb == "plan" and self.plan is None:
                                self.plan = json.loads((temporary / "mb-state/ci-plan.json").read_bytes())
                            if self.plan:
                                cost = request_cost(command_route(verb, item["run"], callee),
                                                    targets=len(self.plan["targets"]), lanes=len(self.plan["lanes"]),
                                                    extra_inputs=len(self.plan.get("plan_inputs", [])))
                                self.test.assertEqual(count, cost, f"{label}/{verb} request count")
                if "id" in item:
                    context[f"steps.{item['id']}.outcome"] = conclusion
                    context.update({f"steps.{item['id']}.outputs.{key}": value
                                    for key, value in outputs(Path(step_environment["GITHUB_OUTPUT"])).items()})
                environment.update(outputs(Path(step_environment["GITHUB_ENV"])))
                listing["steps"].append({"name": item["name"], "number": position, "status": "completed",
                                         "conclusion": conclusion, "started_at": started, "completed_at": self.tick()})
            listing["completed_at"] = self.tick()
            self.listings[producer].append(listing)
            self.api.add_jobs(run["id"], 1, self.listings[producer])
            result = Job(root, {key: self.resolve(value, context) for key, value in definition.get("outputs", {}).items()}, listing)
            self.jobs[label] = result
            return result
        finally:
            self.test.cleanup()

    def upload(self, producer: str, options: dict[str, Any], context: dict[str, str]) -> None:
        directory = Path(self.resolve(options["path"], context))
        name = self.resolve(options["name"], context)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED, compresslevel=int(options["compression-level"])) as archive:
            for path in sorted(directory.rglob("*")):
                relative = path.relative_to(directory)
                if path.is_file() and (options.get("include-hidden-files") == "true" or not any(part.startswith(".") for part in relative.parts)):
                    archive.write(path, relative.as_posix())
        self.test.assertTrue(zipfile.ZipFile(io.BytesIO(stream.getvalue())).namelist(), name)
        created = self.tick()
        run = self.runs[producer]
        record = {"id": 1000 + len(self.artifacts), "name": name, "created_at": created, "updated_at": created,
                  "expires_at": (self.clock + timedelta(days=int(options["retention-days"]))).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "workflow_run": {"id": run["id"], "head_branch": run["head_branch"], "head_sha": self.head}}
        self.artifacts[name] = record, stream.getvalue()
        self.api.add_artifact(record, stream.getvalue())

    def complete(self, producer: str) -> None:
        if producer in ("build", "packaged"):
            fixture_name = "build-full" if producer == "build" else "packaged-pull-request"
            literal = json.loads((ROOT / f"tests/fixtures/ci_graphs/{fixture_name}.json").read_bytes())
            expected = []
            for item in literal["jobs"]:
                placeholder, units = ("target-a", self.plan["targets"]) if producer == "build" else ("lane-a", self.plan["lanes"])
                names = [item["name"].replace(placeholder, unit["id"]) for unit in units] if placeholder in item["name"] else [item["name"]]
                expected.extend((name, item["conclusion"]) for name in names)
            self.test.assertCountEqual([(item["name"], item["conclusion"]) for item in self.listings[producer]], expected)
        run = self.runs[producer]
        run.update(status="completed", conclusion="success", updated_at=self.tick())
        self.api.add_run(run)

    def report(self) -> None:
        grouped: dict[str, list[tuple[str, int]]] = {}
        totals: dict[str, int] = {}
        for job, verb, count in self.requests:
            grouped.setdefault(job, []).append((verb, count))
            key = "/".join(job.split("/")[:2])
            totals[key] = totals.get(key, 0) + count
        expected = generation_breakdown(targets=len(self.plan["targets"]), lanes=len(self.plan["lanes"]),
                                        extra_inputs=len(self.plan["plan_inputs"]))
        self.test.assertEqual(totals, {key: expected[key] for key in totals})
        self.test.assertEqual(self.fences, sum(verb == "worker-prepare" for _, verb, _ in self.requests))
        print("job | requests | command counts")
        for job, costs in grouped.items():
            print(f"{job} | {sum(count for _, count in costs)} | " + ", ".join(f"{verb}={count}" for verb, count in costs))
        for name, count in self.controls:
            print(f"control/{name} | {count} | rejection replay (outside generation budget)")

    def reject(self, name: str, job: str, verb: str, alter: Callable[[Any, Captured], None], message: str) -> None:
        """Replay the exact terminal CLI command against its real pre-command API state.

        The successful generation's private subject, plan and selection remain on disk. The
        API is copied, so the only altered facts are those the control names. Earlier success
        output is moved aside: a rejection must reach its intended check and write no new seal.
        No worker job or prepare operation is cached or re-executed by these command replays.
        """
        captured = self.captured[job, verb]
        saved_api, self.api = self.api, copy.deepcopy(captured.api)
        output = Path(captured.argv[captured.argv.index("--output") + 1]) if "--output" in captured.argv else None
        previous = output.with_name(output.name + "-successful") if output is not None else None
        github_output = Path(captured.argv[captured.argv.index("--github-output") + 1]) if "--github-output" in captured.argv else None
        github_bytes = github_output.read_bytes() if github_output is not None else None
        if output is not None:
            output.rename(previous)
        before = self.api.request_count
        try:
            alter(self.api, captured)
            code, stdout, stderr = self.invoke(captured.argv, captured.environment, captured.workspace)
            self.test.assertIn(code, (2, 3), (name, stdout, stderr))
            self.test.assertIn(message, stderr, name)
            self.test.assertEqual(stdout, "", name)
            if output is not None:
                self.test.assertFalse(output.exists(), name)
            if verb == "assemble":
                self.test.assertFalse((self.test.root / "sealed-build").exists(), name)
            if github_output is not None:
                self.test.assertEqual(github_output.read_bytes(), github_bytes, name)
            self.controls.append((name, self.api.request_count - before))
        finally:
            self.api = saved_api
            if previous is not None:
                if output.exists():
                    shutil.rmtree(output)
                previous.rename(output)

    def altered_lane(self, api: Any, captured: Captured) -> None:
        name = next(name for name in self.artifacts if name.startswith("mb-ci-runtime--"))
        record, original = self.artifacts[name]
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(stream, "w") as changed:
            victim = next(path for path in source.namelist() if not path.startswith("ci-"))
            for info in source.infolist():
                data = source.read(info.filename)
                changed.writestr(info, data + b"\n" if info.filename == victim else data)
        self.test.assertNotEqual(stream.getvalue(), original)
        # GitHub's digest and size now describe the altered real ZIP, while the sealed results
        # index still binds what the lane actually uploaded before aggregation.
        api.add_artifact(record, stream.getvalue())

    def missing_target(self, api: Any, captured: Captured) -> None:
        name = self.jobs[f"build/target/{self.plan['targets'][0]['id']}"].listing["name"]
        jobs = [job for job in captured.jobs if job["name"] != name]
        self.test.assertEqual(len(jobs), len(captured.jobs) - 1)
        api.add_jobs(self.runs["build"]["id"], 1, jobs)

    def later_attempt(self, api: Any, captured: Captured) -> None:
        selection = json.loads(self.jobs["packaged-e2e/input"].outputs["selection"])
        self.test.assertEqual(selection["build"]["producer"]["run_attempt"], 1)
        original = self.runs["build"]
        later = {**original, "run_attempt": 2, "status": "in_progress", "conclusion": None,
                 "run_started_at": self.tick(), "updated_at": self.tick()}
        api.add_run(later, attempts=[original])

    def draft(self, api: Any, captured: Captured) -> None:
        h.seed_pull_request(api, {**self.pull, "draft": True})
