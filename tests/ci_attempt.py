"""One running attempt of the synthetic mod, as a job inside it sees it.

The fixture of the fan-in and gate tests (``describe``, ``gate`` and the ``ci`` verbs of
``commands_build``): a fake GitHub that serves the run, its literal job listing and real artifact
ZIPs, and a real job state directory holding the identity record ``ci subject`` authenticates and
a plan of that subject. Commands run through the real entry point; only the API client and the
fixed worker root are replaced.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest import mock

from mod_base import cli, runtime
from mod_base.build_ci import commands, commands_build, describe, identity
from mod_base.build_ci.authenticate import run_head
from mod_base.build_ci.graph import sealed_upload, upload_job_name
from mod_base.build_ci.protocol import plan_sha256
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json, strict_loads
from tests import ci_mod_harness as h
from tests.helpers import ci_api_artifact, ci_api_run, ci_graph_jobs, ci_plan

RUN = {"build": 42, "packaged": 43}
ATTEMPT = 2
#: The matrix jobs of the literal listings, which hold one job for the fixture's single unit.
_MATRIX = (("Compile target ", "target-a", "targets"), ("Run packaged lane ", "lane-a", "lanes"))


def environment(*, caller: str = "build", push: bool = False) -> dict[str, str]:
    """The environment of a job of attempt 2 of the fixture run of ``caller``."""

    return {**h.environment(event="push" if push else "pull_request_target", caller=caller),
            "GITHUB_RUN_ID": str(RUN[caller]), "GITHUB_RUN_ATTEMPT": str(ATTEMPT)}


@functools.lru_cache(maxsize=None)
def _record(producer: str, caller: str, push: bool) -> bytes:
    invocation = runtime.build_invocation(h.MOD, h.MOD / "site" / "mod-base.json", environment(caller=caller, push=push))
    return canonical_json(identity.authenticate_subject(invocation, h.github()[0], producer=producer,
                                                        pr_number=None if push else 7))


def unit_ids(prefix: str, count: int) -> list[str]:
    """``count`` unit ids, the first being the one the literal listings name."""

    return [f"{prefix}-a", *(f"{prefix}-{index:02d}" for index in range(2, count + 1))]


def plan_of(subject: dict[str, Any], *, targets: int = 1, lanes: int = 1) -> dict[str, Any]:
    """A plan of ``subject`` with ``targets`` targets of ``lanes`` lanes each: two JARs for every
    lane and one native report for every target."""

    plan = ci_plan()
    derived = {key: plan["identity"][key] for key in ("policy_sha256", "inventory_blob", "inventory_sha256",
                                                      "scenario_sha256", "runtime_selection_sha256")}
    plan["identity"] = {**copy.deepcopy(subject), **derived}
    target, lane = plan["targets"][0], plan["lanes"][0]
    lane_ids = unit_ids("lane", targets * lanes)
    plan["targets"], plan["lanes"] = [], []
    for index, target_id in enumerate(unit_ids("target", targets)):
        own = lane_ids[index * lanes:(index + 1) * lanes]
        outputs = [{"path": f"staged/{lane_id}/{role}.jar", "lane_id": lane_id, "role": role}
                   for lane_id in own for role in ("production", "harness")]
        outputs.append({"path": f"staged/{target_id}/native-report.json", "lane_id": None, "role": "native-report"})
        plan["targets"].append({**target, "id": target_id, "outputs": outputs})
        plan["lanes"] += [{**copy.deepcopy(lane), "id": lane_id, "target_id": target_id} for lane_id in own]
    plan["plan_sha256"] = plan_sha256(plan)
    return plan


def expand(jobs: list[dict[str, Any]], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The literal listing with one matrix job for every unit of ``plan``."""

    expanded = []
    for job in jobs:
        units = next((plan[key] for prefix, unit, key in _MATRIX if job["name"].endswith(prefix + unit)), None)
        if units is None:
            expanded.append(job)
            continue
        stem = job["name"][:job["name"].rindex(" ") + 1]
        expanded += [{**copy.deepcopy(job), "name": stem + unit["id"], "id": job["id"] * 1000 + index}
                     for index, unit in enumerate(units)]
    return expanded


class Attempt:
    """A run of ``caller`` in progress, with the jobs of the literal ``listing`` and no artifact
    yet. ``producer`` is the gate the job under test belongs to (a Build job may run inside a
    packaged run); ``push`` makes the subject a protected push instead of pull request 7."""

    def __init__(self, directory: Path, *, listing: str, caller: str = "build", producer: str | None = None,
                 push: bool = False, targets: int = 1, lanes: int = 1, max_requests: int | None = None) -> None:
        self.directory, self.caller, self.run_id = directory, caller, RUN[caller]
        self.environment = environment(caller=caller, push=push)
        self.record = strict_loads(_record(producer or caller, caller, push), label="identity", max_bytes=1 << 20)
        self.plan = plan_of(self.record["subject"], targets=targets, lanes=lanes)
        self.api, self.pull = h.github(max_requests=max_requests)
        self.state = directory / "state"
        identity.write_subject(self.state, self.record)
        identity.write_state_record(self.state, grammar.CI_PLAN_NAME, canonical_json(self.plan))
        self.run = ci_api_run(self.plan, caller, status="in_progress", conclusion=None)
        self.api.add_run(self.run)
        self.jobs = expand(ci_graph_jobs(listing), self.plan)
        self.records: dict[int, dict[str, Any]] = {}
        self.archives: dict[int, bytes] = {}
        self.budgets: list[int | None] = []
        self.sealed_build = directory / "worker" / "sealed-build"
        self.sealed_build.parent.mkdir(mode=0o700)
        self.seed_jobs()

    # -- the run -----------------------------------------------------------------------------------

    def seed_jobs(self) -> None:
        self.api.add_jobs(self.run_id, ATTEMPT, self.jobs)

    def set_run(self, **changes: Any) -> None:
        self.run.update(changes)
        self.api.add_run(self.run)

    def job(self, name: str) -> dict[str, Any]:
        return next(job for job in self.jobs if job["name"] == name)

    def sealing(self, name: str, *, steps: int = 2) -> None:
        """The job ``name`` is running its sealing step and every later job has not started."""

        position = self.jobs.index(self.job(name))
        del self.jobs[position + 1:]
        self.jobs[position].update(status="in_progress", conclusion=None, completed_at=None)
        self.jobs[position]["steps"] = self.jobs[position]["steps"][:steps]
        self.seed_jobs()

    def change_job(self, name: str, **changes: Any) -> None:
        self.job(name).update(changes)
        self.seed_jobs()

    def producer(self, mode: str) -> dict[str, Any]:
        return describe.attempt_producer(self.record, self.plan, mode=mode, run_id=self.run_id, run_attempt=ATTEMPT)

    def upload_job(self, kind: str, unit_id: str | None = None) -> str:
        return upload_job_name(self.caller, kind, unit_id)

    # -- its artifacts -----------------------------------------------------------------------------

    def publish(self, kind: str, unit_id: str | None, data: bytes, *, artifact_id: int, attempt: int = ATTEMPT,
                job: str | None = None, **changes: Any) -> dict[str, Any]:
        """Seed the ``kind`` artifact of this attempt, created inside the upload step of its job
        (or of ``job``, for an artifact no job of the plan uploads)."""

        created = sealed_upload(self.job(job or self.upload_job(kind, unit_id)))[0]
        expires = (grammar.parse_timestamp(created) + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        identity_ = self.plan["identity"]
        record = ci_api_artifact({
            "identity": identity_, "producer": {"run_id": self.run_id, "api_head_sha": run_head(identity_)[0]},
            "artifact": {"id": artifact_id, "name": grammar.ci_artifact_name(kind, self.run_id, attempt, unit_id),
                         "size": len(data), "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                         "created_at": created, "expires_at": expires}}, **changes)
        self.records[artifact_id], self.archives[artifact_id] = record, data
        self.api.add_artifact(record, data)
        return record

    def set_artifact(self, artifact_id: int, **changes: Any) -> None:
        workflow_run = changes.pop("workflow_run", {})
        self.records[artifact_id].update(changes)
        self.records[artifact_id]["workflow_run"].update(workflow_run)
        self.api.add_artifact(self.records[artifact_id], self.archives[artifact_id])

    # -- a command of one of its jobs --------------------------------------------------------------

    def command(self, verb: str, *flags: str, environment: dict[str, str] | None = None) -> tuple[int, str, str]:
        """Run ``ci <verb>`` of this job through the real entry point: exit code, stdout, stderr."""

        def client(environ: Any, *, writable: bool = False, max_requests: int | None = None) -> Any:
            if writable:
                raise AssertionError("a fan-in or gate command writes nothing to GitHub")
            self.budgets.append(max_requests)
            return self.api

        argv = ["ci", verb, "--repo", str(h.MOD), "--config", str(h.MOD / "site" / "mod-base.json"),
                "--state", str(self.state), *flags]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "environ", return_value=self.environment if environment is None else environment), \
                mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                mock.patch.object(commands_build, "BUILD_VALIDATION_ROOT", self.sealed_build), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()


class AttemptCase(unittest.TestCase):
    """A test case with a private temporary directory for the state and the worker root."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.temporary = Path(directory.name)

    def attempt(self, **options: Any) -> Attempt:
        """A new attempt with a job directory of its own."""

        return Attempt(Path(tempfile.mkdtemp(prefix="job-", dir=self.temporary)), **options)
