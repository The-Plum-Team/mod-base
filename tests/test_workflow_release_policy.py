"""The release callee and the managed release caller, held to the ``REL_*`` tables of
``mod_base.workflow`` (ADR 0008, docs/RELEASE-PROTOCOL.md).

The callee only reads: its one job starts with the Build controller prologue bound to the release
caller, consumes the tested generation like any default-branch consumer and seals one artifact. The
caller's credentialed jobs run no action and only the managed bootstrap of the protected head, give each
marketplace token to one step and
reach the release environment once.
"""

from __future__ import annotations

import re
import unittest
from typing import Any

from mod_base import workflow
from mod_base.build_ci.activation import MANAGED_CALLERS, RELEASE_CALLER
from tests.test_workflow_policy import ROOT, SCRUB, SCRUB_API, load_yaml, parse_yaml, step

CALLEE = ROOT / workflow.REL_CALLEE_WORKFLOW
CALLER = ROOT / "template/managed" / workflow.REL_CALLER_WORKFLOW
GUARD = ROOT / "template/managed/.github/workflows/mod-base-guard.yml"
PIN = "0123456789abcdef0123456789abcdef01234567"
KIT_COMMAND = re.compile(r"(?:python3 -P -m mod_base|python3 -I -B mod/scripts/ci/mod_base_kit\.py run --repo mod --) "
                         r"((?:ci|release) [a-z-]+)")


def callee() -> dict[str, Any]:
    return load_yaml(CALLEE)


def caller() -> dict[str, Any]:
    text = CALLER.read_text(encoding="utf-8").replace("{{PIN}}", PIN).replace("{{VERSION}}", "v1.8.0")
    return parse_yaml(text, "mod-base-release.yml")


def commands(steps: list[dict[str, Any]]) -> list[str]:
    found = []
    for item in steps:
        for match in KIT_COMMAND.finditer(str(item.get("run", ""))):
            found.append(match.group(1))
    return found


class ReleaseCalleeTest(unittest.TestCase):
    def test_one_read_only_job_with_the_tabled_name_and_permissions(self) -> None:
        document = callee()
        self.assertEqual(document["permissions"], {})
        self.assertEqual(list(document["jobs"]), list(workflow.REL_CALLEE_JOBS))
        for key, job in document["jobs"].items():
            self.assertEqual(job["name"], workflow.REL_CALLEE_JOBS[key])
            self.assertEqual(job["permissions"], workflow.REL_JOB_PERMISSIONS[key])
            self.assertNotIn("environment", job)
            self.assertNotIn("secrets", str(job))

    def test_its_steps_issue_exactly_the_tabled_commands_in_order(self) -> None:
        for key, job in callee()["jobs"].items():
            self.assertEqual(tuple(commands(job["steps"])), workflow.REL_JOB_VERBS[key])

    def test_the_prologue_binds_the_release_caller_of_the_canonical_branch(self) -> None:
        job = callee()["jobs"]["plan"]
        bind = step(job["steps"], "Bind the two-part implementation identity")["run"]
        self.assertIn("/.github/workflows/mod-base-release.yml@refs/heads/$canonical_branch", bind)
        validate = step(job["steps"], "Validate call inputs before any checkout")["run"]
        self.assertIn('[[ "$GITHUB_EVENT_NAME" == workflow_dispatch ]]', validate)

    def test_it_uploads_one_sealed_artifact_after_the_plan(self) -> None:
        steps = callee()["jobs"]["plan"]["steps"]
        names = [item["name"] for item in steps]
        upload = step(steps, workflow.REL_UPLOAD_STEP)
        self.assertEqual(names.index(workflow.REL_UPLOAD_STEP), names.index("Seal the release plan") + 1)
        self.assertEqual(upload["with"]["name"], "mb-rel-plan--${{ github.run_id }}--a${{ github.run_attempt }}")
        self.assertEqual(sum(1 for item in steps if str(item.get("uses", "")).startswith("actions/upload-artifact@")), 1)

    def test_it_hands_the_caller_the_environment_of_the_release_config(self) -> None:
        document = callee()
        outputs = document["on"]["workflow_call"]["outputs"]
        self.assertEqual(outputs["environment"]["value"], "${{ jobs.plan.outputs.environment }}")
        self.assertEqual(document["jobs"]["plan"]["outputs"]["environment"],
                         "${{ steps.release.outputs.environment }}")

    def test_every_run_step_drops_the_credentials_it_does_not_use(self) -> None:
        for item in callee()["jobs"]["plan"]["steps"]:
            if "run" not in item:
                continue
            uses_api = "GH_TOKEN" in (item.get("env") or {})
            self.assertIn(SCRUB_API if uses_api else SCRUB, item["run"], item["name"])


class ReleaseCallerTest(unittest.TestCase):
    def test_events_concurrency_and_jobs(self) -> None:
        document = caller()
        self.assertEqual(set(document["on"]), set(workflow.REL_EVENTS))
        self.assertEqual(document["on"]["schedule"], [{"cron": workflow.REL_CRON}])
        self.assertEqual(document["permissions"], {})
        self.assertEqual(document["concurrency"], {"group": workflow.REL_LOCK, "cancel-in-progress": "false"})
        self.assertEqual(list(document["jobs"]), list(workflow.REL_CALLER_JOBS))
        for key, job in document["jobs"].items():
            self.assertEqual(job["name"], workflow.REL_CALLER_JOBS[key])
            self.assertEqual(job["permissions"], workflow.REL_CALLER_PERMISSIONS[key])

    def test_the_guard_and_the_plan_call_the_tabled_workflows(self) -> None:
        jobs = caller()["jobs"]
        self.assertEqual(jobs["guard"]["uses"], "./.github/workflows/mod-base-guard.yml")
        self.assertEqual(jobs["guard"]["with"], {"callees": workflow.REL_GUARD_CALLEES})
        self.assertEqual(jobs["plan"]["uses"], f"The-Plum-Team/mod-base/{workflow.REL_CALLEE_WORKFLOW}@{PIN}")
        self.assertEqual(jobs["plan"]["permissions"], workflow.REL_JOB_PERMISSIONS["plan"])

    def test_the_guard_admits_the_release_caller_on_its_two_events_only(self) -> None:
        text = GUARD.read_text(encoding="utf-8")
        self.assertIn("release) caller=.github/workflows/mod-base-release.yml ;;", text)
        self.assertIn('elif [[ "$CALLEES" == release ]]; then', text)
        self.assertIn("workflow_dispatch | schedule) ;;", text)

    def test_credentialed_jobs_run_only_the_managed_bootstrap_of_the_protected_head(self) -> None:
        jobs = caller()["jobs"]
        for key in ("publish", "finish"):
            steps = jobs[key]["steps"]
            # No action at all: nothing third-party, no checkout naming the kit repository.
            self.assertEqual([item["name"] for item in steps if "uses" in item], [], key)
            fetch = step(steps, "Check out the protected mod head")["run"]
            self.assertIn('fetch --quiet --depth=1 --no-tags', fetch)
            self.assertIn('[[ "${pin%% *}" == "$KIT_SHA" ]]', fetch)
            for item in steps:
                run = str(item.get("run", ""))
                self.assertNotIn("The-Plum-Team/mod-base", run, item["name"])
                self.assertNotIn("python3 -P -m mod_base", run, item["name"])
        text = CALLER.read_text(encoding="utf-8")
        self.assertEqual(text.count("The-Plum-Team/mod-base/"), 1, "the one pinned call")

    def test_one_environment_and_one_secret_per_credentialed_step(self) -> None:
        jobs = caller()["jobs"]
        self.assertEqual(jobs["publish"]["environment"], workflow.REL_ENVIRONMENT)
        for key in ("guard", "plan", "finish"):
            self.assertNotIn("environment", jobs[key])
        self.assertNotIn("secrets.", str(jobs["finish"]))
        runs = [item for item in jobs["publish"]["steps"] if "run" in item]
        self.assertEqual(tuple(command for item in runs for command in commands([item])),
                         workflow.REL_CALLER_VERBS["publish"])
        kit_steps = [item for item in runs if commands([item])]
        for index, item in enumerate(kit_steps):
            secrets = sorted(re.findall(r"secrets\.([A-Z_]+)", str(item.get("env", {}))))
            expected = [workflow.REL_STEP_SECRETS[index]] if index in workflow.REL_STEP_SECRETS else []
            self.assertEqual(secrets, expected, item["name"])
        self.assertEqual(tuple(commands(jobs["finish"]["steps"])), workflow.REL_CALLER_VERBS["finish"])
        # Once the drafts exist, each marketplace and the verification run even if another failed.
        publish_steps = jobs["publish"]["steps"]
        self.assertEqual(step(publish_steps, "Create the draft releases")["id"], "github")
        for name in ("Upload to Modrinth", "Upload to CurseForge", "Verify and finish the releases"):
            self.assertEqual(step(publish_steps, name)["if"], "${{ !cancelled() && steps.github.outcome == 'success' }}")
        admit = step(publish_steps, "Admit the sealed release plan")
        self.assertIn('--plan-attempt "$PLAN_ATTEMPT"', admit["run"])
        self.assertEqual(admit["env"]["PLAN_ATTEMPT"], "${{ needs.plan.outputs.run-attempt }}")
        # The job runs in the environment the plan read from the protected config, and admission
        # refuses to go on unless that config, the plan's own, names this environment.
        self.assertEqual(admit["env"]["RELEASE_ENVIRONMENT"], "${{ needs.plan.outputs.environment }}")
        self.assertIn('--environment "$RELEASE_ENVIRONMENT"', admit["run"])
        self.assertIn('--release-config "$GITHUB_WORKSPACE/mod/scripts/ci/mod-base-release.json"', admit["run"])
        validate = step(publish_steps, "Validate the plan outputs before any checkout")["run"]
        self.assertIn('[[ "$RELEASE_ENVIRONMENT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]', validate)

    def test_only_the_authoritative_mode_manages_the_release_caller(self) -> None:
        self.assertEqual([mode for mode, callers in MANAGED_CALLERS.items() if RELEASE_CALLER in callers],
                         ["shared-build-and-e2e"])


if __name__ == "__main__":
    unittest.main()
