"""The managed batch caller ``mod-base-batch.yml`` (docs/BUILD-E2E-DESIGN.md, "Drafts and batches";
KQ8-2).

Every case reads the template as ``template sync`` renders it for a mod whose Build config names a
``batch`` block. Structure: its two triggers, permissions, the one environment and the one App key
in the preparing and closing jobs alone, the one third-party action at its reviewed pin, the guard
and the kit's settlement workflow at the pin. Jobs: the ``if:`` conditions are evaluated with
GitHub's expression rules for each operation, so that no failed, skipped or empty evaluation ever
reaches the closing job. The ``locate``, ``prepare`` and ``close`` scripts run under the shell
harness against stub ``gh``, ``git`` and ``python3``: which request an event makes, which kit
command a preparation issues, and which members are closed for which evaluated document and which
live pull requests.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mod_base import workflow
from mod_base.build_ci.activation import BATCH_CALLER, BATCH_MODES
from mod_base.pin import Pin
from mod_base.template import tool
from tests.helpers import ci_activation
from tests.test_managed_ci_callers import (BRANCH, HEAD, KIT_SHA, LOCAL_GUARD, OTHER, REPOSITORY, TOKEN, VERSION,
                                           kit_workflow, simulate)
from tests.test_workflow_policy import APP_TOKEN, ROOT, ShellHarness, outputs, parse_yaml, require_tools, step

NAME = "mod-base batch"
ENVIRONMENT = "mod-base-batch"
CLIENT_ID = "MOD_BASE_BATCH_APP_CLIENT_ID"
PRIVATE_KEY = "MOD_BASE_BATCH_APP_PRIVATE_KEY"
LOCATE_STEP = "Find what this event asks of the batch caller"
FETCH_STEP = "Fetch the protected mod head and the pinned kit"
MINT_STEP = "Mint the batch App token"
PREPARE_STEP = "Push the batch branch and open its pull request"
CLOSE_STEP = "Close each member still at its batched head"
APP_TOKEN_VALUE = "fixture-batch-app-token"
MERGED = "c" * 40
BATCHED = {1: "1" * 40, 2: "2" * 40, 3: "3" * 40}
SQUASHED = {1: "4" * 40, 2: "5" * 40, 3: "6" * 40}
BATCH_PR = 9
PULL = f"repos/{REPOSITORY}/pulls"


def text(branch: str = BRANCH) -> str:
    files = tool.expected_callers(ROOT, Pin(KIT_SHA, VERSION, ()), ci_activation("shared-build-and-e2e"), branch,
                                  batching=True)
    return files[BATCH_CALLER].decode("utf-8")  # type: ignore[union-attr]


def document() -> dict[str, Any]:
    return parse_yaml(text(), BATCH_CALLER)


def script(job: str, name: str) -> str:
    return step(document()["jobs"][job]["steps"], name)["run"]


def pull(number: int, **changes: Any) -> dict[str, Any]:
    """A member pull request at its batched head, open, against the default branch."""

    record = {"number": number, "state": "open",
              "head": {"ref": f"fix/n{number}", "sha": BATCHED.get(number, HEAD), "repo": {"full_name": REPOSITORY}},
              "base": {"ref": BRANCH, "sha": OTHER, "repo": {"full_name": REPOSITORY}}}
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(record.get(key), dict):
            record[key] = {**record[key], **value}
        else:
            record[key] = value
    return record


def batch_pull(**changes: Any) -> dict[str, Any]:
    record = pull(BATCH_PR, state="closed", merged=True, merged_at="2026-10-12T20:00:00Z", merge_commit_sha=MERGED,
                  head={"ref": "batch/night", "sha": SQUASHED[2]})
    record.update(changes)
    return record


def intents(numbers: tuple[int, ...] = (1, 2), **changes: Any) -> dict[str, Any]:
    return {"repository": REPOSITORY, "pr_number": BATCH_PR, "base_branch": BRANCH, "merged_sha": MERGED,
            "members": [{"pr_number": number, "head_sha": BATCHED[number], "squash_sha": SQUASHED[number]}
                        for number in numbers], **changes}


class BatchCallerStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = text()
        self.document = document()
        self.jobs = self.document["jobs"]

    def test_header_name_and_top_level_keys(self) -> None:
        self.assertRegex(self.text.splitlines()[0], r"^# mod-base managed: .+, edit only in The-Plum-Team/mod-base "
                                                    r"template/managed/" + re.escape(BATCH_CALLER) + "$")
        self.assertEqual(list(self.document), ["name", "on", "permissions", "jobs"], "no concurrency group")
        self.assertEqual(self.document["name"], NAME)
        self.assertNotIn("{{", self.text.replace("${{", ""), "no placeholder is left")
        self.assertNotIn("\t", self.text)
        self.assertEqual(text("release/1.21"),
                         self.text.replace('    branches: ["master"]', '    branches: ["release/1.21"]'),
                         "only the push filter changes with the canonical branch")

    def test_a_push_to_the_default_branch_and_a_request_there_and_nothing_else(self) -> None:
        triggers = self.document["on"]
        self.assertEqual(list(triggers), ["push", "workflow_dispatch"])
        self.assertEqual(triggers["push"], {"branches": [BRANCH]})
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(list(inputs), ["operation", "pull-requests", "name", "pr-number"])
        self.assertEqual((inputs["operation"]["type"], inputs["operation"]["options"]), ("choice", ["prepare", "settle"]))
        for word in ("pull_request_target", "pull_request:", "workflow_run", "schedule", "issue_comment",
                     "repository_dispatch", "secrets: inherit", "continue-on-error", "github.event.pull_request",
                     "actions/checkout", "always()", "id-token", "actions: write", "statuses:", "permission-statuses",
                     "checks:", "permission-checks", "permission-actions", "permission-administration"):
            with self.subTest(word=word):
                self.assertNotIn(word, self.text)

    def test_no_permission_at_the_top_level_and_only_read_grants_below(self) -> None:
        self.assertEqual(self.document["permissions"], {})
        self.assertEqual({job_id: job.get("permissions") for job_id, job in self.jobs.items()}, {
            "locate": {"contents": "read", "pull-requests": "read"},
            "guard": {"actions": "read", "contents": "read"},
            "prepare": {"contents": "read"},
            "evaluate": {"actions": "read", "contents": "read", "pull-requests": "read"},
            "close": {"pull-requests": "read"}})
        self.assertEqual(self.jobs["evaluate"]["permissions"], workflow.CI_JOB_PERMISSIONS["batch"]["settle"],
                         "the calling job grants exactly what the kit's job holds")

    def test_the_environment_and_the_app_key_are_named_in_the_preparing_and_closing_jobs_alone(self) -> None:
        self.assertEqual({job_id: job.get("environment") for job_id, job in self.jobs.items() if "environment" in job},
                         {"prepare": ENVIRONMENT, "close": ENVIRONMENT})
        self.assertNotEqual(ENVIRONMENT, "mod-base-gate", "the gate status App never prepares or closes a batch")
        self.assertEqual(self.text.count("secrets."), 2)
        self.assertEqual(self.text.count(f"secrets.{PRIVATE_KEY}"), 2)
        self.assertEqual(self.text.count(f"vars.{CLIENT_ID}"), 2)
        self.assertNotIn("MOD_BASE_GATE_APP", self.text)
        mints = {job_id: step(self.jobs[job_id]["steps"], MINT_STEP) for job_id in ("prepare", "close")}
        for job_id, item in mints.items():
            with self.subTest(job=job_id):
                self.assertEqual(item["uses"], APP_TOKEN)
                self.assertIn(f"uses: {APP_TOKEN} # v3.2.0", self.text)
                self.assertEqual(item["with"]["client-id"], f"${{{{ vars.{CLIENT_ID} }}}}")
                self.assertEqual(item["with"]["private-key"], f"${{{{ secrets.{PRIVATE_KEY} }}}}")
                self.assertNotIn("owner", item["with"], "the token is scoped to this repository alone")
                self.assertNotIn("repositories", item["with"])
        # Only what each effect needs: a batch branch and its pull request; closing pull requests.
        self.assertEqual({key: value for key, value in mints["prepare"]["with"].items() if key.startswith("permission")},
                         {"permission-contents": "write", "permission-pull-requests": "write"})
        self.assertEqual({key: value for key, value in mints["close"]["with"].items() if key.startswith("permission")},
                         {"permission-pull-requests": "write"})

    def test_jobs_and_calls(self) -> None:
        self.assertEqual([(job_id, job["name"]) for job_id, job in self.jobs.items()],
                         list(workflow.CI_CALLER_JOBS["batch"].items()))
        self.assertEqual(self.jobs["guard"]["uses"], LOCAL_GUARD)
        self.assertEqual(self.jobs["guard"]["with"], {"callees": "batch"})
        self.assertEqual(self.jobs["evaluate"]["uses"], f"{kit_workflow('batch')}@{KIT_SHA}")
        self.assertIn(f"uses: {kit_workflow('batch')}@{KIT_SHA} # {VERSION}", self.text)
        self.assertEqual(self.jobs["evaluate"]["with"], {"kit-sha": "${{ needs.guard.outputs.kit-sha }}",
                                                        "pr-number": "${{ needs.locate.outputs.pr-number }}"})
        self.assertEqual({job_id: job.get("needs") for job_id, job in self.jobs.items()}, {
            "locate": None, "guard": "locate", "prepare": ["locate", "guard"], "evaluate": ["locate", "guard"],
            "close": ["locate", "evaluate"]})
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", self.text, re.MULTILINE)
        self.assertEqual(sorted(set(uses)), sorted({LOCAL_GUARD, APP_TOKEN, f"The-Plum-Team/mod-base/"
                                                    f".github/workflows/batch.yml@{KIT_SHA}"}))

    def test_event_data_reaches_shell_only_through_env(self) -> None:
        for job_id, job in self.jobs.items():
            for item in job.get("steps", []):
                with self.subTest(job=job_id, step=item["name"]):
                    self.assertNotRegex(item.get("run", ""), r"\$\{\{", "no expression is interpolated into shell")

    def test_each_operation_reaches_exactly_its_jobs(self) -> None:
        github = {"event_name": "workflow_dispatch", "ref": f"refs/heads/{BRANCH}"}
        located = {"prepare": {"operation": "prepare", "pr-number": "", "pull-requests": "1 2", "name": "n"},
                   "settle": {"operation": "settle", "pr-number": "9", "pull-requests": "", "name": ""},
                   "none": {"operation": "none", "pr-number": "", "pull-requests": "", "name": ""}}
        evaluated = {"intents": json.dumps(intents())}
        cases = {
            "prepare": ({"locate": located["prepare"]}, {}, {"locate": "success", "guard": "success",
                                                            "prepare": "success", "evaluate": "skipped",
                                                            "close": "skipped"}),
            "settle": ({"locate": located["settle"], "evaluate": evaluated}, {},
                       {"locate": "success", "guard": "success", "prepare": "skipped", "evaluate": "success",
                        "close": "success"}),
            "settle with nothing to close": ({"locate": located["settle"], "evaluate": {"intents": ""}}, {},
                                             {"locate": "success", "guard": "success", "prepare": "skipped",
                                              "evaluate": "success", "close": "skipped"}),
            "a push that merges no batch": ({"locate": located["none"]}, {},
                                            {"locate": "success", "guard": "skipped", "prepare": "skipped",
                                             "evaluate": "skipped", "close": "skipped"}),
            "a failed guard": ({"locate": located["settle"], "evaluate": evaluated}, {"guard": "failure"},
                               {"locate": "success", "guard": "failure", "prepare": "skipped", "evaluate": "skipped",
                                "close": "skipped"}),
            "a failed evaluation": ({"locate": located["settle"], "evaluate": evaluated}, {"evaluate": "failure"},
                                    {"locate": "success", "guard": "success", "prepare": "skipped",
                                     "evaluate": "failure", "close": "skipped"}),
        }
        for label, (job_outputs, outcomes, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(simulate(self.document, github, job_outputs=job_outputs, outcomes=outcomes), expected)

    def test_the_caller_is_rendered_only_with_a_batch_block_in_its_mode(self) -> None:
        pin = Pin(KIT_SHA, VERSION, ())
        for mode in ("disabled", "shadow", "shared-build", "shared-build-and-e2e"):
            for batching in (False, True):
                with self.subTest(mode=mode, batching=batching):
                    files = tool.expected_callers(ROOT, pin, ci_activation(mode), BRANCH, batching=batching)
                    self.assertEqual(files[BATCH_CALLER] is not None, batching and mode in BATCH_MODES)
        rollback = tool.expected_callers(ROOT, pin, ci_activation("reviewed-rollback", "shared-build-and-e2e"), BRANCH,
                                         batching=True)
        self.assertIsNotNone(rollback[BATCH_CALLER], "a reviewed rollback keeps the caller until the block goes")


class ScriptCase(unittest.TestCase):
    """One ``run:`` body of the batch caller under the shell harness."""

    JOB = ""
    STEP = ""
    STUBS: tuple[str, ...] = ("gh", "sleep")

    def setUp(self) -> None:
        require_tools("bash", "jq", "grep", "sort", "uniq", "tr")
        temporary = tempfile.TemporaryDirectory(prefix="batch caller ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.harness = ShellHarness(self.root / "harness", stubs=self.STUBS)
        self.script = script(self.JOB, self.STEP)
        self.output = self.root / "github_output"
        self.summary = self.root / "summary"
        self.runner_temp = self.root / "runner temp"
        self.runner_temp.mkdir()

    def base(self) -> dict[str, str]:
        return {"GH_TOKEN": TOKEN, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_REF": f"refs/heads/{BRANCH}",
                "GITHUB_SHA": MERGED, "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.summary),
                "RUNNER_TEMP": str(self.runner_temp), "GITHUB_SERVER_URL": "https://github.com",
                "GITHUB_WORKSPACE": str(self.root)}

    def run_script(self, environment: Mapping[str, str | None],
                   fixtures: Mapping[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
        self.output.unlink(missing_ok=True)
        cleaned = {name: value for name, value in environment.items() if value is not None}
        return self.harness.run(self.script, cleaned, fixtures=fixtures, cwd=self.root)

    def calls(self, tool_name: str = "gh") -> list[dict[str, Any]]:
        return [record for record in self.harness.records() if record["tool"] == tool_name]


class LocateTests(ScriptCase):
    JOB, STEP = "locate", LOCATE_STEP
    COMMIT_PULLS = f"GET repos/{REPOSITORY}/commits/{MERGED}/pulls?per_page=100"

    def locate(self, name: str, fixtures: Mapping[str, Any] | None = None, **environment: str | None):
        values: dict[str, str | None] = {**self.base(), "GITHUB_EVENT_NAME": name, "OPERATION": "",
                                         "PULL_REQUESTS": "", "BATCH_NAME": "", "DISPATCH_PR_NUMBER": ""}
        values.update(environment)
        return self.run_script(values, fixtures)

    def assert_located(self, result, expected: dict[str, str], requests: list[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outputs(self.output), expected)
        self.assertEqual([record["route"] for record in self.calls()], requests)

    def assert_refused(self, result, message: str) -> None:
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Locate batch request: " + message, result.stderr)
        self.assertEqual(outputs(self.output), {})

    def test_a_request_names_its_operation_without_a_request_to_github(self) -> None:
        self.assert_located(self.locate("workflow_dispatch", OPERATION="prepare", PULL_REQUESTS="12 7 30",
                                        BATCH_NAME="night-1"),
                            {"operation": "prepare", "pr-number": "", "pull-requests": "12 7 30", "name": "night-1"},
                            [])
        self.assert_located(self.locate("workflow_dispatch", OPERATION="settle", DISPATCH_PR_NUMBER="9",
                                        PULL_REQUESTS="1 2", BATCH_NAME="x"),
                            {"operation": "settle", "pr-number": "9", "pull-requests": "", "name": ""}, [])
        fifty = " ".join(str(number) for number in range(1, 51))
        self.assert_located(self.locate("workflow_dispatch", OPERATION="prepare", PULL_REQUESTS=fifty, BATCH_NAME="a"),
                            {"operation": "prepare", "pr-number": "", "pull-requests": fifty, "name": "a"}, [])

    def test_a_malformed_request_is_refused(self) -> None:
        for pulls in ("", "0", "1  2", " 1", "1 ", "1,2", "1\n2", "01", "1 x", "1 2 1",
                      " ".join(str(number) for number in range(1, 52))):
            with self.subTest(pulls=pulls):
                result = self.locate("workflow_dispatch", OPERATION="prepare", PULL_REQUESTS=pulls, BATCH_NAME="n")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(outputs(self.output), {})
        for name in ("", "../x", "N", "-n", "a b", "a/b", "x" * 64, "n\n"):
            with self.subTest(name=name):
                self.assert_refused(self.locate("workflow_dispatch", OPERATION="prepare", PULL_REQUESTS="1",
                                                BATCH_NAME=name), "malformed batch name")
        for number in ("", "0", "09", "9 ", "-1", "1" * 19):
            with self.subTest(number=number):
                self.assert_refused(self.locate("workflow_dispatch", OPERATION="settle", DISPATCH_PR_NUMBER=number),
                                    "malformed batch pull request number")
        self.assert_refused(self.locate("workflow_dispatch", OPERATION="merge"), "the operation is prepare or settle")
        for name in ("pull_request_target", "workflow_run", "schedule", ""):
            with self.subTest(event=name):
                self.assert_refused(self.locate(name), "the batch caller runs only on push or workflow_dispatch")
        self.assertEqual(self.calls(), [])

    def test_a_push_settles_the_one_batch_pull_request_it_merges(self) -> None:
        self.assert_located(self.locate("push", {self.COMMIT_PULLS: {"body": [batch_pull()]}}),
                            {"operation": "settle", "pr-number": "9", "pull-requests": "", "name": ""},
                            [self.COMMIT_PULLS])
        self.assertTrue(all(record["token"] == TOKEN for record in self.calls()))
        # A merge that is no batch pull request's, or of another repository, base or commit, asks nothing.
        for label, listed in {
                "no pull request": [], "an ordinary pull request": [batch_pull(head={"ref": "fix/n1"})],
                "not merged": [batch_pull(merged_at=None)], "another merge commit": [batch_pull(merge_commit_sha="d" * 40)],
                "another base": [batch_pull(base={"ref": "release"})],
                "a fork's batch branch": [batch_pull(head={"repo": {"full_name": "attacker/mod"}})],
                "another base repository": [batch_pull(base={"repo": {"full_name": "attacker/mod"}})]}.items():
            with self.subTest(case=label):
                self.assert_located(self.locate("push", {self.COMMIT_PULLS: {"body": listed}}),
                                    {"operation": "none", "pr-number": "", "pull-requests": "", "name": ""},
                                    [self.COMMIT_PULLS])
        self.assert_refused(self.locate("push", {self.COMMIT_PULLS: {"body": [batch_pull(), batch_pull(number=10)]}}),
                            "the pushed commit does not merge at most one batch pull request")
        self.assert_refused(self.locate("push", {self.COMMIT_PULLS: {"fail": "gh: Forbidden (HTTP 403)"}}),
                            "cannot list the pull requests of the pushed commit")
        self.assert_refused(self.locate("push", GITHUB_REF="refs/tags/v1"), "this run is not on a branch")


class PrepareTests(ScriptCase):
    JOB, STEP = "prepare", PREPARE_STEP
    STUBS = ("python3",)

    def prepare(self, **environment: str | None):
        values: dict[str, str | None] = {**self.base(), "GH_TOKEN": APP_TOKEN_VALUE, "PULL_REQUESTS": "12 7",
                                         "BATCH_NAME": "night-1", "GITHUB_TOKEN": "leaked", "ACTIONS_RUNTIME_TOKEN": "x"}
        values.update(environment)
        return self.harness.run(self.script, {name: value for name, value in values.items() if value is not None},
                                cwd=self.root, record_env=("GH_TOKEN", "GITHUB_TOKEN", "ACTIONS_RUNTIME_TOKEN",
                                                           "PYTHONPATH"))

    def test_the_kit_prepares_with_the_app_token_and_the_validated_request(self) -> None:
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (call,) = self.calls("python3")
        self.assertEqual(call["argv"], ["-P", "-m", "mod_base", "ci", "batch-prepare", "--repo", "mod", "--config",
                                        "mod/site/mod-base.json", "--state", f"{self.runner_temp}/mb-state",
                                        "--name", "night-1", "12", "7"])
        self.assertEqual(call["env"], {"GH_TOKEN": APP_TOKEN_VALUE, "GITHUB_TOKEN": None, "ACTIONS_RUNTIME_TOKEN": None,
                                       "PYTHONPATH": f"{self.root}/kit/src"})
        self.assertTrue((self.runner_temp / "mb-state").is_dir())
        self.assertNotIn("--allowed-paths", call["argv"], "the list is the protected config's")

    def test_nothing_runs_without_a_token_or_with_a_request_the_locate_job_would_refuse(self) -> None:
        for environment in ({"GH_TOKEN": ""}, {"GH_TOKEN": "two words"}, {"PULL_REQUESTS": "12 --dry-run"},
                            {"PULL_REQUESTS": ""}, {"BATCH_NAME": "../x"}, {"BATCH_NAME": ""}):
            with self.subTest(environment=environment):
                result = self.prepare(**environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls("python3"), [])


class CloseTests(ScriptCase):
    JOB, STEP = "close", CLOSE_STEP

    def close(self, fixtures: Mapping[str, Any], document: Any = None, **environment: str | None):
        values: dict[str, str | None] = {
            **self.base(), "CLOSE_TOKEN": APP_TOKEN_VALUE, "EVALUATION": "success", "PR_NUMBER": str(BATCH_PR),
            "INTENTS": json.dumps(intents() if document is None else document, separators=(",", ":"))}
        values.update(environment)
        return self.run_script(values, fixtures)

    def fixtures(self, *members: dict[str, Any], batch: dict[str, Any] | None = None,
                 closed: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
        found: dict[str, Any] = {f"GET {PULL}/{BATCH_PR}": {"body": batch or batch_pull()}}
        for member in members:
            number = member["number"]
            found[f"GET {PULL}/{number}"] = {"body": member}
            found[f"POST repos/{REPOSITORY}/issues/{number}/comments"] = {"body": {"id": 1}}
            found[f"PATCH {PULL}/{number}"] = {"body": (closed or {}).get(number, {**member, "state": "closed"})}
        return found

    def writes(self) -> list[tuple[str, str | None, Any]]:
        return [(record["route"], record["token"], json.loads(record["input"]) if record["input"] else None)
                for record in self.calls() if not record["route"].startswith("GET ")]

    def test_members_still_at_their_batched_head_are_commented_and_closed_with_the_app(self) -> None:
        result = self.close(self.fixtures(pull(1), pull(2)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [
            (f"POST repos/{REPOSITORY}/issues/1/comments", APP_TOKEN_VALUE,
             {"body": f"Merged through batch #{BATCH_PR} (batch commit `{SQUASHED[1][:12]}`)."}),
            (f"PATCH {PULL}/1", APP_TOKEN_VALUE, {"state": "closed"}),
            (f"POST repos/{REPOSITORY}/issues/2/comments", APP_TOKEN_VALUE,
             {"body": f"Merged through batch #{BATCH_PR} (batch commit `{SQUASHED[2][:12]}`)."}),
            (f"PATCH {PULL}/2", APP_TOKEN_VALUE, {"state": "closed"})])
        reads = [record for record in self.calls() if record["route"].startswith("GET ")]
        self.assertEqual([record["route"] for record in reads], [f"GET {PULL}/{BATCH_PR}", f"GET {PULL}/1",
                                                                 f"GET {PULL}/2"])
        self.assertTrue(all(record["token"] == TOKEN for record in reads), "reads use the read-only job token")
        self.assertIn("Closed 2 member(s) of batch #9; 0 left as they are", self.summary.read_text(encoding="utf-8"))

    def test_a_member_that_moved_closed_or_left_stays_as_it_is(self) -> None:
        members = (pull(1, head={"sha": "f" * 40}), pull(2, state="closed"))
        result = self.close(self.fixtures(*members))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.writes(), [])
        for change in ({"head": {"repo": {"full_name": "attacker/mod"}}}, {"base": {"ref": "release"}},
                       {"base": {"repo": {"full_name": "attacker/mod"}}}):
            with self.subTest(change=change):
                result = self.close(self.fixtures(pull(1, **change), pull(2)))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([route for route, _, _ in self.writes()],
                                 [f"POST repos/{REPOSITORY}/issues/2/comments", f"PATCH {PULL}/2"])

    def test_a_push_that_races_the_close_reopens_the_member(self) -> None:
        raced = {**pull(1), "state": "closed", "head": {**pull(1)["head"], "sha": "f" * 40}}
        result = self.close(self.fixtures(pull(1), pull(2), closed={1: raced}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([(route, body) for route, _, body in self.writes()][:3], [
            (f"POST repos/{REPOSITORY}/issues/1/comments",
             {"body": f"Merged through batch #{BATCH_PR} (batch commit `{SQUASHED[1][:12]}`)."}),
            (f"PATCH {PULL}/1", {"state": "closed"}), (f"PATCH {PULL}/1", {"state": "open"})])
        self.assertIn("Closed 1 member(s) of batch #9; 1 left as they are", self.summary.read_text(encoding="utf-8"))

    def test_nothing_is_closed_without_a_successful_evaluation_a_valid_document_or_the_merged_batch(self) -> None:
        fixtures = self.fixtures(pull(1), pull(2))
        refused = {
            "a failed evaluation": ({}, {"EVALUATION": "failure"}),
            "no App token": ({}, {"CLOSE_TOKEN": ""}),
            "the job token as App token": ({}, {"CLOSE_TOKEN": TOKEN}),
            "another batch number": ({}, {"PR_NUMBER": "10"}),
            "another repository": (intents(repository="attacker/mod"), {}),
            "another base": (intents(base_branch="release"), {}),
            "no members": (intents(()), {}),
            "the batch itself": ({**intents(), "members": [{"pr_number": BATCH_PR, "head_sha": BATCHED[1],
                                                             "squash_sha": SQUASHED[1]}]}, {}),
            "a duplicate member": ({**intents(), "members": intents()["members"] * 2}, {}),
            "an extra key": ({**intents(), "delete": True}, {}),
            "an upper-case head": ({**intents(), "members": [{"pr_number": 1, "head_sha": "A" * 40,
                                                              "squash_sha": SQUASHED[1]}]}, {}),
            "a number that is text": ({**intents(), "members": [{"pr_number": "1", "head_sha": BATCHED[1],
                                                                 "squash_sha": SQUASHED[1]}]}, {}),
            "no JSON": ("{", {}),
        }
        for label, (document, environment) in refused.items():
            with self.subTest(case=label):
                result = self.close(fixtures, document or None, **environment) if document != "{" else \
                    self.close(fixtures, None, INTENTS="{", **environment)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.writes(), [])
        for label, batch in {"not merged": batch_pull(merged=False),
                             "another merge commit": batch_pull(merge_commit_sha="d" * 40),
                             "not a batch branch": batch_pull(head={"ref": "fix/n1"})}.items():
            with self.subTest(case=label):
                result = self.close(self.fixtures(pull(1), pull(2), batch=batch))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("is not the merged batch the evaluation proved", result.stderr)
                self.assertEqual(self.writes(), [])


if __name__ == "__main__":
    unittest.main()
