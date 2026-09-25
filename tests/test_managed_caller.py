"""The managed caller ``template/managed/.github/workflows/pages.yml`` (SPEC §1.2, §1.3, §5.1, §5.2).

Structure (regions, pin placeholders, triggers, locks, per-job permissions, wiring, forbid-list) and
the shell of the three caller-owned jobs, executed with stub ``gh``: ``verify-kit`` (accepted and
rejected bindings, bounded retry), the deploy head recheck (moved heads fail, malformed heads fail)
and ``request-rotation`` (exact dispatch request through the same bounded retry).
"""

from __future__ import annotations

import base64
import json
import re
import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from mod_base.pin import parse_pin_files
from tests.test_workflow_policy import (CALLER_PATH, CHECKOUT, DEPLOY_PAGES, ShellHarness, callee, caller,
                                        render_caller, require_tools, step)

REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
KIT_SHA = "0123456789abcdef0123456789abcdef01234567"
HEAD = "a" * 40
RUN_ID = "36042781699"
JOB_IDS = ["verify-kit", "publish", "deploy", "finalize", "request-rotation", "rotate"]
JOB_NAMES = {"verify-kit": "verify_kit", "publish": "publish", "deploy": "deploy", "finalize": "finalize",
             "request-rotation": "request_rotation", "rotate": "rotate"}
PERMISSIONS = {
    "verify-kit": {"actions": "read", "contents": "read"},
    "publish": {"actions": "read", "contents": "read"},
    "deploy": {"contents": "read", "pages": "write", "id-token": "write"},
    "finalize": {"actions": "read", "contents": "read"},
    "request-rotation": {"actions": "write"},
    "rotate": {"actions": "write", "contents": "read"},
}
FORBIDDEN = ("github.event.workflow_run", "implementation_sha", "pull_request_target", "repository_dispatch",
             "quick-skin-pages-wake", "workflows:", "job.workflow_sha", "$/", "secrets", "workflow_run")


class CallerStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = CALLER_PATH.read_text(encoding="utf-8")
        self.document = caller()

    def test_regions_and_pin_placeholders(self) -> None:
        lines = self.text.splitlines(keepends=True)
        self.assertTrue(lines[0].startswith("# >>> mod-base managed: pages caller v1"))
        self.assertEqual(lines[-3:], ["# <<< mod-base managed\n",
                                      '# >>> mod-local extensions: only jobs whose id starts with "ext-"; no mod-base '
                                      "uses, no pages/id-token/actions:write\n",
                                      "# <<< mod-local extensions\n"])
        uses = [line.strip() for line in lines if "{{PIN}}" in line or "{{VERSION}}" in line]
        self.assertEqual(uses, ["uses: The-Plum-Team/mod-base/.github/workflows/" + name
                                + ".yml@{{PIN}} # {{VERSION}}" for name in ("publish", "finalize", "rotate")])
        self.assertNotRegex(self.text, r"(?i)the-plum-team/mod-base/[A-Za-z0-9._/-]+@(?!\{\{PIN\}\} # \{\{VERSION\}\})")

    def test_rendered_caller_carries_exactly_one_pin(self) -> None:
        pin = parse_pin_files({".github/workflows/pages.yml": render_caller(KIT_SHA, "v1.2.3").encode()})
        self.assertEqual((pin.sha, pin.version), (KIT_SHA, "v1.2.3"))
        self.assertEqual(len(pin.references), 3)

    def test_triggers_permissions_and_locks(self) -> None:
        document = self.document
        self.assertEqual(document["name"], workflow.PAGES_WORKFLOW_NAME)
        self.assertEqual(list(document["on"]), ["schedule", "workflow_dispatch"])
        self.assertEqual(document["on"]["schedule"], [{"cron": workflow.PAGES_CRON}])
        inputs = document["on"]["workflow_dispatch"]["inputs"]
        self.assertEqual(list(inputs), ["operation", "run_id", "sha", "family", "bundle_key", "artifact_id",
                                        "artifact_digest", "coverage_sha"])
        self.assertEqual((inputs["operation"]["type"], inputs["operation"]["default"], inputs["operation"]["required"]),
                         ("choice", "manual", "true"))
        self.assertEqual(tuple(inputs["operation"]["options"]), workflow.OPERATIONS)
        for name in list(inputs)[1:]:
            self.assertEqual((inputs[name]["type"], inputs[name]["required"]), ("string", "false"), name)
        self.assertEqual(document["permissions"], {})
        group = document["concurrency"]["group"]
        self.assertEqual(group, "${{ github.event_name == 'workflow_dispatch' && inputs.operation == 'rotate' && "
                                f"'{workflow.ROTATION_LOCK}' || '{workflow.PUBLICATION_LOCK}' }}}}")
        self.assertEqual(document["concurrency"]["cancel-in-progress"], "false")
        for word in FORBIDDEN:
            self.assertNotIn(word, self.text, word)

    def test_jobs_names_and_exact_permissions(self) -> None:
        jobs = self.document["jobs"]
        self.assertEqual(list(jobs), JOB_IDS)
        for job_id, job in jobs.items():
            with self.subTest(job=job_id):
                self.assertEqual(job["name"], workflow.CALLER[JOB_NAMES[job_id]])
                self.assertEqual(job["permissions"], PERMISSIONS[job_id])
                if "steps" in job:
                    self.assertEqual(job["runs-on"], "ubuntu-24.04")
                    self.assertIn("timeout-minutes", job)
                    for item in job["steps"]:
                        self.assertNotIn("python", item.get("run", ""))
        for job_id, job in jobs.items():
            has_pages = any(scope in job["permissions"] for scope in ("pages", "id-token"))
            self.assertEqual(has_pages, job_id == "deploy", job_id)

    def test_deploy_holds_no_checkout_and_only_rechecks_then_deploys(self) -> None:
        deploy = self.document["jobs"]["deploy"]
        self.assertEqual([item["name"] for item in deploy["steps"]],
                         ["Recheck every published source head immediately before deployment",
                          "Deploy the immutable site artifact"])
        self.assertEqual(deploy["steps"][1], {"name": "Deploy the immutable site artifact", "id": "deployment",
                                              "uses": DEPLOY_PAGES})
        self.assertEqual(deploy["environment"], {"name": "github-pages",
                                                 "url": "${{ steps.deployment.outputs.page_url }}"})
        self.assertNotIn(CHECKOUT, json.dumps(self.document["jobs"]))
        self.assertEqual(deploy["steps"][0]["env"], {"GH_TOKEN": "${{ github.token }}",
                                                     "HEADS": "${{ needs.publish.outputs.heads }}"})

    def test_wiring_and_callee_inputs(self) -> None:
        jobs = self.document["jobs"]
        self.assertNotIn("needs", jobs["verify-kit"])
        wiring = {
            "publish": ("verify-kit", "inputs.operation != 'rotate'"),
            "deploy": ("publish", "needs.publish.outputs.eligible == 'true'"),
            "finalize": (["verify-kit", "publish", "deploy"], "needs.deploy.result == 'success'"),
            "request-rotation": (["publish", "finalize"], "always() && needs.publish.outputs.eligible == 'true' && "
                                                          "needs.finalize.result == 'success'"),
            "rotate": ("verify-kit", "github.event_name == 'workflow_dispatch' && inputs.operation == 'rotate'"),
        }
        for job_id, (needs, condition) in wiring.items():
            with self.subTest(job=job_id):
                self.assertEqual(jobs[job_id]["needs"], needs)
                self.assertEqual(jobs[job_id]["if"], condition)
        self.assertNotIn("continue-on-error", jobs["request-rotation"],
                         "a failed rotation request fails the generation, as today (SPEC §5.2)")
        for job_id, name in (("publish", "publish"), ("finalize", "finalize"), ("rotate", "rotate")):
            with self.subTest(callee=name):
                self.assertEqual(jobs[job_id]["uses"], f"The-Plum-Team/mod-base/.github/workflows/{name}.yml@{KIT_SHA}")
                self.assertNotIn("secrets", jobs[job_id])
                self.assertEqual(list(jobs[job_id]["with"]), list(callee(name)["on"]["workflow_call"]["inputs"]))
                self.assertEqual(jobs[job_id]["with"]["kit-sha"], "${{ needs.verify-kit.outputs.kit_sha }}")
        self.assertEqual(jobs["publish"]["with"]["operation"],
                         "${{ github.event_name == 'schedule' && 'recovery' || inputs.operation }}")
        self.assertEqual(jobs["rotate"]["with"]["pages-run-id"], "${{ inputs.run_id }}")
        self.assertEqual(jobs["rotate"]["with"]["pages-run-sha"], "${{ inputs.sha }}")
        publish_outputs = callee("publish")["on"]["workflow_call"]["outputs"]
        self.assertEqual(list(publish_outputs), ["eligible", "bundle_keys", "families", "heads"])
        self.assertEqual(jobs["verify-kit"]["outputs"], {"kit_sha": "${{ steps.kit.outputs.kit_sha }}"})

    def test_retry_function_is_shared_with_the_callees(self) -> None:
        pattern = re.compile(r"(?ms)^protected_gh_api_retry\(\) \{.*?^\}\n")
        functions = set()
        for job in self.document["jobs"].values():
            for item in job.get("steps", []):
                functions.update(pattern.findall(item.get("run", "")))
        for name in workflow.CALLEE_WORKFLOWS:
            for job in callee(name)["jobs"].values():
                for item in job["steps"]:
                    functions.update(pattern.findall(item.get("run", "")))
        self.assertEqual(len(functions), 1)
        self.assertIn("else\n      status=$?", functions.pop(), "the failure status is captured, not the if's")


def run_fixture(**overrides: object) -> dict:
    run = {
        "id": int(RUN_ID), "path": ".github/workflows/pages.yml", "head_sha": HEAD, "head_branch": "master",
        "event": "workflow_dispatch", "head_repository": {"full_name": REPOSITORY},
        "referenced_workflows": [
            {"path": f"The-Plum-Team/mod-base/.github/workflows/{name}.yml@{KIT_SHA}", "sha": KIT_SHA,
             "ref": KIT_SHA} for name in ("publish", "finalize", "rotate")],
    }
    run.update(overrides)
    return run


def contents(text: str) -> dict:
    encoded = base64.b64encode(text.encode()).decode()
    return {"content": "\n".join(encoded[index:index + 60] for index in range(0, len(encoded), 60)) + "\n",
            "encoding": "base64"}


class VerifyKitExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq", "base64", "grep", "sed", "sort")
        temporary = tempfile.TemporaryDirectory(prefix="verify kit ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.script = step(caller()["jobs"]["verify-kit"]["steps"],
                           "Bind the executing callee to the protected pin")["run"]

    def fixtures(self, **overrides: object) -> dict:
        fixtures = {
            f"GET repos/{REPOSITORY}": {"body": {"default_branch": "master"}},
            f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}": {"body": run_fixture()},
            f"GET repos/{REPOSITORY}/contents/.github/workflows/pages.yml?ref={HEAD}":
                {"body": contents(render_caller(KIT_SHA, "v1.0.0"))},
            f"GET repos/The-Plum-Team/mod-base/compare/{KIT_SHA}...main":
                {"body": {"status": "ahead", "ahead_by": 3, "behind_by": 0}},
        }
        fixtures.update(overrides)
        return fixtures

    def invoke(self, fixtures: dict, **env: str):
        output = self.root / "github_output"
        if output.exists():
            output.unlink()
        environment = {"GH_TOKEN": "fixture-token", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": RUN_ID,
                       "GITHUB_SHA": HEAD, "GITHUB_REF": "refs/heads/master", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_OUTPUT": str(output), **env}
        result = self.harness.run(self.script, environment, fixtures=fixtures)
        return result, output.read_text() if output.exists() else ""

    def test_a_bound_pin_is_output(self) -> None:
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event):
                result, output = self.invoke(self.fixtures(**{
                    f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}": {"body": run_fixture(event=event)}}),
                    GITHUB_EVENT_NAME=event)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output, f"kit_sha={KIT_SHA}\n")
                records = self.harness.records()
                self.assertEqual([record["route"] for record in records], [
                    f"GET repos/{REPOSITORY}", f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}",
                    f"GET repos/{REPOSITORY}/contents/.github/workflows/pages.yml?ref={HEAD}",
                    f"GET repos/The-Plum-Team/mod-base/compare/{KIT_SHA}...main"])
                self.assertTrue(all(record["token"] == "fixture-token" for record in records))
        result, output = self.invoke(self.fixtures(**{
            f"GET repos/The-Plum-Team/mod-base/compare/{KIT_SHA}...main":
                {"body": {"status": "identical", "ahead_by": 0, "behind_by": 0}}}))
        self.assertEqual((result.returncode, output), (0, f"kit_sha={KIT_SHA}\n"), result.stderr)

    def test_rejected_bindings_output_nothing(self) -> None:
        other = "f" * 40
        runs = f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"
        file = f"GET repos/{REPOSITORY}/contents/.github/workflows/pages.yml?ref={HEAD}"
        compare = f"GET repos/The-Plum-Team/mod-base/compare/{KIT_SHA}...main"
        references = run_fixture()["referenced_workflows"]
        cases = {
            "push event": ({}, {"GITHUB_EVENT_NAME": "push"}),
            "pull_request_target event": ({}, {"GITHUB_EVENT_NAME": "pull_request_target"}),
            "non-default ref": ({}, {"GITHUB_REF": "refs/heads/feature"}),
            "tag ref": ({}, {"GITHUB_REF": "refs/tags/master"}),
            "other workflow": ({runs: {"body": run_fixture(path=".github/workflows/other.yml")}}, {}),
            "other head": ({runs: {"body": run_fixture(head_sha=other)}}, {}),
            "other branch": ({runs: {"body": run_fixture(head_branch="feature")}}, {}),
            "other event": ({runs: {"body": run_fixture(event="schedule")}}, {}),
            "fork head": ({runs: {"body": run_fixture(head_repository={"full_name": "attacker/fork"})}}, {}),
            "no referenced workflows": ({runs: {"body": run_fixture(referenced_workflows=[])}}, {}),
            "missing referenced workflows": ({runs: {"body": {key: value for key, value in run_fixture().items()
                                                              if key != "referenced_workflows"}}}, {}),
            "two kit commits": ({runs: {"body": run_fixture(referenced_workflows=[
                *references[:2], {"path": f"The-Plum-Team/mod-base/.github/workflows/rotate.yml@{other}",
                                  "sha": other}])}}, {}),
            "path and sha disagree": ({runs: {"body": run_fixture(referenced_workflows=[
                {"path": f"The-Plum-Team/mod-base/.github/workflows/publish.yml@{other}", "sha": KIT_SHA}])}}, {}),
            "lowercase impostor entry": ({runs: {"body": run_fixture(referenced_workflows=[
                *references, {"path": f"the-plum-team/MOD-BASE/.github/workflows/publish.yml@{other}",
                              "sha": other}])}}, {}),
            "tag reference": ({runs: {"body": run_fixture(referenced_workflows=[
                {"path": "The-Plum-Team/mod-base/.github/workflows/publish.yml@v1.0.0", "sha": "v1.0.0"}])}}, {}),
            "file pins another commit": ({file: {"body": contents(render_caller(other, "v1.0.0"))}}, {}),
            "file mixes commits": ({file: {"body": contents(render_caller(KIT_SHA, "v1.0.0") +
                                                            f"# The-Plum-Team/mod-base/x@{other}\n")}}, {}),
            "file declares no pin": ({file: {"body": contents("name: Project site\n")}}, {}),
            "diverged kit commit": ({compare: {"body": {"status": "diverged", "ahead_by": 1, "behind_by": 1}}}, {}),
            "kit commit behind main": ({compare: {"body": {"status": "behind", "ahead_by": 0, "behind_by": 2}}}, {}),
            "unknown kit commit": ({compare: {"fail": "gh: Not Found (HTTP 404)"}}, {}),
        }
        for label, (fixture_overrides, env) in cases.items():
            with self.subTest(case=label):
                result, output = self.invoke(self.fixtures(**fixture_overrides), **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(output, "")

    def test_only_transient_failures_are_retried_with_a_bounded_budget(self) -> None:
        runs = f"GET repos/{REPOSITORY}/actions/runs/{RUN_ID}"
        transient = {"fail": "gh: Server Error (HTTP 502)"}
        result, output = self.invoke(self.fixtures(**{runs: [transient, transient, {"body": run_fixture()}]}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, f"kit_sha={KIT_SHA}\n")
        sleeps = [record["argv"] for record in self.harness.records() if record["tool"] == "sleep"]
        self.assertEqual(sleeps, [["5"], ["10"]])
        result, output = self.invoke(self.fixtures(**{runs: [transient] * 10}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(record.get("route") == runs for record in self.harness.records()), 4)
        self.assertEqual([record["argv"] for record in self.harness.records() if record["tool"] == "sleep"],
                         [["5"], ["10"], ["20"]])
        forbidden = [{"fail": "gh: Forbidden (HTTP 403)"}, {"body": run_fixture()}]
        result, _ = self.invoke(self.fixtures(**{runs: forbidden}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(sum(record.get("route") == runs for record in self.harness.records()), 1)


class DeployRecheckExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="deploy recheck ")
        self.addCleanup(temporary.cleanup)
        self.harness = ShellHarness(Path(temporary.name) / "harness")
        self.script = step(caller()["jobs"]["deploy"]["steps"],
                           "Recheck every published source head immediately before deployment")["run"]

    def invoke(self, heads: object, branches: dict[str, object] | None = None, **env: str):
        fixtures: dict[str, object] = {f"GET repos/{REPOSITORY}": {"body": {"default_branch": "master"}}}
        for branch, response in (branches or {"master": HEAD}).items():
            encoded = branch.replace("/", "%2F")
            fixtures[f"GET repos/{REPOSITORY}/branches/{encoded}"] = (
                response if isinstance(response, (dict, list)) else {"body": {"commit": {"sha": response}}})
        text = heads if isinstance(heads, str) else json.dumps(heads)
        environment = {"GH_TOKEN": "fixture-token", "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": HEAD,
                       "GITHUB_REF": "refs/heads/master", "HEADS": text, **env}
        return self.harness.run(self.script, environment, fixtures=fixtures)

    def test_unchanged_heads_deploy(self) -> None:
        result = self.invoke({"master": HEAD})
        self.assertEqual(result.returncode, 0, result.stderr)
        second = "b" * 40
        result = self.invoke({"master": HEAD, "release/1.20.1": second}, {"master": HEAD, "release/1.20.1": second})
        self.assertEqual(result.returncode, 0, result.stderr)
        routes = [record["route"] for record in self.harness.records()]
        self.assertIn(f"GET repos/{REPOSITORY}/branches/release%2F1.20.1", routes)

    def test_a_moved_head_retains_the_deployed_site(self) -> None:
        result = self.invoke({"master": HEAD}, {"master": "c" * 40})
        self.assertEqual(result.returncode, 1)
        self.assertIn("advanced before deployment; retaining the deployed site", result.stderr)
        second = "b" * 40
        result = self.invoke({"master": HEAD, "release/1.20.1": second}, {"master": HEAD, "release/1.20.1": "d" * 40})
        self.assertEqual(result.returncode, 1)

    def test_malformed_or_foreign_heads_fail_before_any_branch_read(self) -> None:
        cases = {
            "not json": "{not json",
            "array": ["master"],
            "empty": {},
            "traversal branch": {"master": HEAD, "../x": HEAD},
            "absolute branch": {"master": HEAD, "/x": HEAD},
            "double slash": {"master": HEAD, "a//b": HEAD},
            "short sha": {"master": HEAD[:39]},
            "numeric sha": {"master": 1},
            "too many": {f"b{index}": HEAD for index in range(65)},
            "missing default": {"release": HEAD},
            "default is not github.sha": {"master": "b" * 40},
        }
        for label, heads in cases.items():
            with self.subTest(case=label):
                result = self.invoke(heads)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("/branches/" in str(record.get("route")) for record in self.harness.records()))
        result = self.invoke({"master": HEAD}, GITHUB_REF="refs/heads/feature")
        self.assertNotEqual(result.returncode, 0)

    def test_transient_branch_reads_are_retried(self) -> None:
        result = self.invoke({"master": HEAD}, {"master": [{"fail": "gh: Service Unavailable (HTTP 503)"},
                                                           {"body": {"commit": {"sha": HEAD}}}]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([record["argv"] for record in self.harness.records() if record["tool"] == "sleep"], [["5"]])


class RequestRotationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="request rotation ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.runner_temp = self.root / "runner temp"
        self.runner_temp.mkdir()
        self.item = step(caller()["jobs"]["request-rotation"]["steps"], "Dispatch the separately locked exact-ID rotation")
        self.dispatch = f"POST repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches"

    def invoke(self, dispatch: object = None, **env: str):
        fixtures = {f"GET repos/{REPOSITORY}": {"body": {"default_branch": "master"}},
                    self.dispatch: dispatch or {"body": ""}}
        environment = {"GH_TOKEN": "fixture-token", "GH_REPO": REPOSITORY, "GITHUB_REPOSITORY": REPOSITORY,
                       "GITHUB_RUN_ID": RUN_ID, "GITHUB_SHA": HEAD, "GITHUB_REF": "refs/heads/master",
                       "RUNNER_TEMP": str(self.runner_temp), **env}
        return self.harness.run(self.item["run"], environment, fixtures=fixtures)

    def dispatches(self) -> list[dict]:
        return [record for record in self.harness.records() if record.get("route") == self.dispatch]

    def sleeps(self) -> list[list[str]]:
        return [record["argv"] for record in self.harness.records() if record["tool"] == "sleep"]

    def test_step_environment(self) -> None:
        self.assertEqual(self.item["env"], {"GH_TOKEN": "${{ github.token }}", "GH_REPO": "${{ github.repository }}"})
        self.assertNotIn("gh workflow run", self.item["run"], "every caller call goes through the protected retry")

    def test_dispatches_exactly_one_rotation_of_this_run(self) -> None:
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        posts = self.dispatches()
        self.assertEqual(len(posts), 1)
        argv = posts[0]["argv"]
        self.assertEqual(argv[:5], ["api", "--method", "POST", f"repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches",
                                    "--input"])
        self.assertEqual(len(argv), 6)
        self.assertEqual(Path(argv[5]).parent, self.runner_temp)
        self.assertEqual(json.loads(posts[0]["input"]),
                         {"ref": "master", "inputs": {"operation": "rotate", "run_id": RUN_ID, "sha": HEAD}})
        self.assertEqual((posts[0]["token"], posts[0]["repo"]), ("fixture-token", REPOSITORY))
        self.assertEqual(self.sleeps(), [])

    def test_the_dispatch_names_the_live_default_branch(self) -> None:
        fixtures = {f"GET repos/{REPOSITORY}": {"body": {"default_branch": "main"}}, self.dispatch: {"body": ""}}
        result = self.harness.run(self.item["run"], {
            "GH_TOKEN": "fixture-token", "GH_REPO": REPOSITORY, "GITHUB_REPOSITORY": REPOSITORY, "GITHUB_RUN_ID": RUN_ID,
            "GITHUB_SHA": HEAD, "GITHUB_REF": "refs/heads/main", "RUNNER_TEMP": str(self.runner_temp)}, fixtures=fixtures)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.dispatches()[0]["input"])["ref"], "main")

    def test_refuses_foreign_refs_and_malformed_identity(self) -> None:
        for env in ({"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_RUN_ID": "0"}, {"GITHUB_SHA": "HEAD"},
                    {"GITHUB_RUN_ID": "1\ninputs[operation]=manual"}, {"GH_REPO": "attacker/fork"}):
            with self.subTest(env=env):
                result = self.invoke(**env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.dispatches(), [])

    def test_a_transient_dispatch_failure_is_retried_with_a_bounded_budget(self) -> None:
        transient = {"fail": "gh: Server Error (HTTP 502)"}
        result = self.invoke([transient, {"fail": "gh: API rate limit exceeded (HTTP 403)"}, {"body": ""}])
        self.assertEqual(result.returncode, 0, result.stderr)
        posts = self.dispatches()
        self.assertEqual(len(posts), 3)
        self.assertEqual({record["input"] for record in posts}, {posts[0]["input"]}, "every attempt sends one request")
        self.assertEqual(self.sleeps(), [["5"], ["10"]])
        result = self.invoke([{"fail": "gh: Service Unavailable (HTTP 503)"}] * 10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.dispatches()), 4)
        self.assertEqual(self.sleeps(), [["5"], ["10"], ["20"]])

    def test_a_rejected_dispatch_fails_the_request_without_a_retry(self) -> None:
        for failure in ("gh: Unexpected inputs provided (HTTP 422)", "gh: Resource not accessible by integration (HTTP 403)",
                        "gh: Not Found (HTTP 404)"):
            with self.subTest(failure=failure):
                result = self.invoke({"fail": failure, "status": 1})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.dispatches()), 1)
                self.assertEqual(self.sleeps(), [])
                self.assertIn(failure, result.stderr)


if __name__ == "__main__":
    unittest.main()
