from __future__ import annotations

import unittest

from mod_base import workflow
from mod_base.errors import MbError


class JobNameTableTest(unittest.TestCase):
    def test_api_job_names(self) -> None:
        table = [
            (("publish", "admit"), {}, "Publish / Admit publication"),
            (("publish", "collect"), {"key": "mc1.20.1"}, "Publish / Collect mc1.20.1"),
            (("publish", "family"), {"family": "mod-compatibility", "key": "mc26.3"},
             "Publish / Collect mod-compatibility mc26.3"),
            (("publish", "build"), {}, "Publish / Build atomic static site"),
            (("finalize", "refresh"), {"key": "mc1.20.1"}, "Finalize / Refresh evidence cache for mc1.20.1"),
            (("finalize", "refresh_family"), {"family": "mod-compatibility", "key": "mc1.20.1"},
             "Finalize / Refresh mod-compatibility cache for mc1.20.1"),
            (("rotate", "rotate"), {}, "Rotate / Rotate the authenticated successful generation"),
            (("publish", "collect"), {"key": "0123456789abcdef01234567"}, "Publish / Collect 0123456789abcdef01234567"),
        ]
        for (name, job), fields, expected in table:
            with self.subTest(expected=expected):
                self.assertEqual(workflow.api_job_name(name, job, **fields), expected)

    def test_caller_job_names_are_never_prefixed(self) -> None:
        self.assertEqual(workflow.caller_job_name("verify_kit"), "Verify pinned mod-base")
        self.assertEqual(workflow.caller_job_name("deploy"), "Deploy GitHub Pages")
        self.assertEqual(workflow.caller_job_name("request_rotation"), "Request post-success evidence rotation")
        self.assertEqual(workflow.CALLER, {
            "verify_kit": "Verify pinned mod-base", "publish": "Publish", "deploy": "Deploy GitHub Pages",
            "finalize": "Finalize", "request_rotation": "Request post-success evidence rotation", "rotate": "Rotate",
        })
        with self.assertRaises(MbError):
            workflow.caller_job_name("admit")

    def test_step_names(self) -> None:
        self.assertEqual(workflow.STEPS, {
            "select": "Select the newest authenticated evidence",
            "family_select": "Select the newest authenticated family generation",
            "cache_upload": "Roll the protected evidence cache forward",
            "family_cache_upload": "Roll the protected family cache forward",
            "baseline_upload": "Retain the complete compact generation for feature evidence reuse",
        })
        self.assertEqual(workflow.step_name("baseline_upload"),
                         "Retain the complete compact generation for feature evidence reuse")
        with self.assertRaises(MbError):
            workflow.step_name("upload")

    def test_yaml_template_names(self) -> None:
        self.assertEqual(workflow.workflow_template_name("publish", "collect"), "Collect ${{ matrix.key }}")
        self.assertEqual(workflow.workflow_template_name("publish", "family"),
                         "Collect ${{ matrix.family }} ${{ matrix.key }}")
        self.assertEqual(workflow.workflow_template_name("finalize", "refresh_family"),
                         "Refresh ${{ matrix.family }} cache for ${{ matrix.key }}")
        self.assertEqual(workflow.workflow_template_name("publish", "admit"), "Admit publication")

    def test_fields_are_exact_and_validated(self) -> None:
        for call in (
            lambda: workflow.api_job_name("publish", "collect"),
            lambda: workflow.api_job_name("publish", "collect", key="mc1.20.1", family="x"),
            lambda: workflow.api_job_name("publish", "admit", key="mc1.20.1"),
            lambda: workflow.api_job_name("publish", "collect", key="MC 1"),
            lambda: workflow.api_job_name("publish", "family", family="Bad", key="k1"),
            lambda: workflow.api_job_name("publish", "deploy"),
            lambda: workflow.api_job_name("deploy", "deploy"),
        ):
            with self.assertRaises(MbError):
                call()

    def test_workflow_constants(self) -> None:
        self.assertEqual(workflow.PAGES_WORKFLOW_NAME, "Project site")
        self.assertEqual(workflow.PAGES_WORKFLOW_PATH, ".github/workflows/pages.yml")
        self.assertEqual(workflow.PAGES_CRON, "43 * * * *")
        self.assertEqual((workflow.PUBLICATION_LOCK, workflow.ROTATION_LOCK),
                         ("mod-base-pages-publication", "mod-base-pages-rotation"))
        self.assertEqual(workflow.OPERATIONS, ("manual", "deploy", "family", "rotate"))
        self.assertEqual(workflow.PUBLISH_OPERATIONS, ("recovery", "manual", "deploy", "family"))
        self.assertEqual(set(workflow.CALLEE_WORKFLOWS), set(workflow.CALLEE))


class BuildE2eNameTableTest(unittest.TestCase):
    """Every Build/E2E name, spelled out: a rename must change this file, the literal job listings
    under ``tests/fixtures/ci_graphs`` and every graph digest pinned in ``tests/test_ci_protocol.py``."""

    def test_managed_workflow_paths(self) -> None:
        self.assertEqual(workflow.CI_GUARD_WORKFLOW_PATH, ".github/workflows/mod-base-guard.yml")
        self.assertEqual(workflow.CI_CALLER_WORKFLOWS, {
            "build": ".github/workflows/mod-base-build.yml",
            "packaged": ".github/workflows/mod-base-packaged-e2e.yml",
            "status": ".github/workflows/mod-base-gate-status.yml",
        })
        self.assertEqual(workflow.CI_CALLEE_WORKFLOWS, {
            "build": ".github/workflows/build.yml",
            "select-build": ".github/workflows/select-build.yml",
            "packaged-e2e": ".github/workflows/packaged-e2e.yml",
            "gate-status": ".github/workflows/gate-status.yml",
        })
        # The Build/E2E callees are their own registry: the Pages table and its tests stay as they are.
        self.assertEqual(set(workflow.CI_CALLEE_WORKFLOWS.values()) & set(workflow.CALLEE_WORKFLOWS.values()), set())

    def test_caller_and_callee_job_tables(self) -> None:
        self.assertEqual(workflow.CI_CALLER_JOBS, {
            "build": {"guard": "Verify pinned mod-base", "deferred": "Build deferred for draft",
                      "shared": "Shared Build"},
            "packaged": {"guard": "Verify pinned mod-base", "deferred": "Packaged E2E deferred for draft",
                         "select": "Select exact Build", "rebuild": "Shared Build",
                         "shared": "Shared Packaged E2E"},
            "status": {"guard": "Verify pinned mod-base", "locate": "Locate the pull request",
                       "evaluate": "Evaluate protected gates", "publish": "Publish protected gate statuses"},
        })
        self.assertEqual(workflow.CI_CALLS, {
            "build": {"guard": "guard", "shared": "build"},
            "packaged": {"guard": "guard", "select": "select-build", "rebuild": "build", "shared": "packaged-e2e"},
        })
        # The status caller is no producer: its calls are tabled apart and `ci_producer` refuses it.
        self.assertEqual(workflow.CI_STATUS_CALLS, {"guard": "guard", "evaluate": "gate-status"})
        self.assertLessEqual(set(workflow.CI_STATUS_CALLS), set(workflow.CI_CALLER_JOBS["status"]))
        self.assertNotIn("status", workflow.CI_CALLS)
        with self.assertRaises(MbError):
            workflow.ci_producer(workflow.CI_CALLER_WORKFLOWS["status"])
        self.assertEqual(workflow.CI_CALLEE_CALLERS, {
            "build": ("build", "packaged"), "select-build": ("build", "packaged"),
            "packaged-e2e": ("build", "packaged"), "gate-status": ("status",)})
        self.assertEqual(list(workflow.CI_CALLEE_CALLERS), list(workflow.CI_CALLEE_WORKFLOWS))
        for callee, callers in workflow.CI_CALLEE_CALLERS.items():
            self.assertLessEqual(set(callers), set(workflow.CI_CALLER_WORKFLOWS), callee)
            called = {caller for caller, calls in {**workflow.CI_CALLS, "status": workflow.CI_STATUS_CALLS}.items()
                      if callee in calls.values()}
            self.assertLessEqual(called, set(callers), f"{callee}: a caller that calls it is admitted by it")
        self.assertEqual(workflow.CI_CALLEE_JOBS, {
            "guard": {"verify": "Authenticate the pinned kit"},
            "build": {"plan": "Plan protected Build", "policy": "Verify protected policy",
                      "target": "Compile target {id}", "assemble": "Seal complete Build bundle",
                      "gate": "Verify complete Build"},
            "select-build": {"select": "Select exact Build source"},
            "packaged-e2e": {"input": "Authenticate exact Build", "lane": "Run packaged lane {id}",
                             "aggregate": "Seal complete packaged results",
                             "gate": "Verify complete packaged E2E"},
            "gate-status": {"evaluate": "Evaluate gate states"},
        })
        self.assertIs(workflow.CI_CALLEE_JOBS["build"], workflow.CI_BUILD_JOBS)
        self.assertIs(workflow.CI_CALLEE_JOBS["packaged-e2e"], workflow.CI_PACKAGED_JOBS)
        self.assertIs(workflow.CI_CALLEE_JOBS["gate-status"], workflow.CI_STATUS_JOBS)
        self.assertEqual((workflow.CI_GUARD_CALL, workflow.CI_BUILD_CALL, workflow.CI_SELECT_CALL,
                          workflow.CI_PACKAGED_CALL, workflow.CI_STATUS_CALL),
                         ("Verify pinned mod-base", "Shared Build", "Select exact Build", "Shared Packaged E2E",
                          "Evaluate protected gates"))
        self.assertEqual((workflow.CI_SEAL_STEP, workflow.CI_UPLOAD_STEP),
                         ("Validate frozen native exports", "Upload sealed outputs"))
        self.assertEqual(set(workflow.CI_CALLEE_JOBS), set(workflow.CI_CALLEE_WORKFLOWS) | {"guard"})
        for producer, calls in workflow.CI_CALLS.items():
            self.assertLessEqual(set(calls), set(workflow.CI_CALLER_JOBS[producer]))
            self.assertLessEqual(set(calls.values()), set(workflow.CI_CALLEE_JOBS))

    def test_api_job_names(self) -> None:
        table = [
            (("build", "guard", "verify"), {}, "Verify pinned mod-base / Authenticate the pinned kit"),
            (("build", "shared", "plan"), {}, "Shared Build / Plan protected Build"),
            (("build", "shared", "policy"), {}, "Shared Build / Verify protected policy"),
            (("build", "shared", "target"), {"id": "mc1.20.1"}, "Shared Build / Compile target mc1.20.1"),
            (("build", "shared", "assemble"), {}, "Shared Build / Seal complete Build bundle"),
            (("build", "shared", "gate"), {}, "Shared Build / Verify complete Build"),
            (("packaged", "guard", "verify"), {}, "Verify pinned mod-base / Authenticate the pinned kit"),
            (("packaged", "select", "select"), {}, "Select exact Build / Select exact Build source"),
            (("packaged", "rebuild", "target"), {"id": "target-a"}, "Shared Build / Compile target target-a"),
            (("packaged", "rebuild", "gate"), {}, "Shared Build / Verify complete Build"),
            (("packaged", "shared", "input"), {}, "Shared Packaged E2E / Authenticate exact Build"),
            (("packaged", "shared", "lane"), {"id": "1.20.1-fabric.full"},
             "Shared Packaged E2E / Run packaged lane 1.20.1-fabric.full"),
            (("packaged", "shared", "aggregate"), {}, "Shared Packaged E2E / Seal complete packaged results"),
            (("packaged", "shared", "gate"), {}, "Shared Packaged E2E / Verify complete packaged E2E"),
        ]
        for arguments, fields, expected in table:
            with self.subTest(expected=expected):
                self.assertEqual(workflow.ci_api_job_name(*arguments, **fields), expected)

    def test_caller_owned_and_skipped_call_names_are_bare(self) -> None:
        self.assertEqual(workflow.ci_caller_job_name("build", "deferred"), "Build deferred for draft")
        self.assertEqual(workflow.ci_caller_job_name("packaged", "deferred"), "Packaged E2E deferred for draft")
        self.assertEqual(workflow.ci_caller_job_name("status", "evaluate"), "Evaluate protected gates")
        self.assertEqual(workflow.ci_caller_job_name("status", "publish"), "Publish protected gate statuses")
        # A calling job that is skipped never expands its callee: the API lists the caller's job once.
        self.assertEqual(workflow.ci_skipped_call_job_name("build", "shared"), "Shared Build")
        self.assertEqual(workflow.ci_skipped_call_job_name("packaged", "select"), "Select exact Build")
        self.assertEqual(workflow.ci_skipped_call_job_name("packaged", "rebuild"), "Shared Build")
        self.assertEqual(workflow.ci_skipped_call_job_name("packaged", "shared"), "Shared Packaged E2E")
        for call in (lambda: workflow.ci_skipped_call_job_name("build", "deferred"),
                     lambda: workflow.ci_skipped_call_job_name("status", "publish"),
                     lambda: workflow.ci_caller_job_name("build", "select"),
                     lambda: workflow.ci_caller_job_name("pages", "guard")):
            with self.assertRaises(MbError):
                call()

    def test_yaml_templates_and_skipped_unexpanded_matrices(self) -> None:
        self.assertEqual(workflow.MATRIX_EXPRESSIONS["id"], "${{ matrix.id }}")
        self.assertEqual(workflow.ci_workflow_template_name("build", "target"), "Compile target ${{ matrix.id }}")
        self.assertEqual(workflow.ci_workflow_template_name("packaged-e2e", "lane"),
                         "Run packaged lane ${{ matrix.id }}")
        self.assertEqual(workflow.ci_workflow_template_name("build", "gate"), "Verify complete Build")
        self.assertEqual(workflow.ci_workflow_template_name("guard", "verify"), "Authenticate the pinned kit")
        self.assertEqual(workflow.ci_unexpanded_api_job_name("build", "shared", "target"),
                         "Shared Build / Compile target ${{ matrix.id }}")
        self.assertEqual(workflow.ci_unexpanded_api_job_name("packaged", "shared", "lane"),
                         "Shared Packaged E2E / Run packaged lane ${{ matrix.id }}")
        with self.assertRaises(MbError):
            workflow.ci_unexpanded_api_job_name("build", "shared", "gate")

    def test_fields_and_keys_are_exact_and_validated(self) -> None:
        for call in (
            lambda: workflow.ci_api_job_name("build", "shared", "target"),
            lambda: workflow.ci_api_job_name("build", "shared", "target", id="a", key="mc1.20.1"),
            lambda: workflow.ci_api_job_name("build", "shared", "gate", id="a"),
            lambda: workflow.ci_api_job_name("build", "shared", "target", id="a--b"),
            lambda: workflow.ci_api_job_name("build", "shared", "target", id="A"),
            lambda: workflow.ci_api_job_name("build", "shared", "lane", id="a"),
            lambda: workflow.ci_api_job_name("build", "deferred", "plan"),
            lambda: workflow.ci_api_job_name("build", "rebuild", "plan"),
            lambda: workflow.ci_api_job_name("status", "evaluate", "plan"),
            lambda: workflow.ci_callee_job_name("publish", "admit"),
            lambda: workflow.ci_workflow_template_name("build", "lane"),
        ):
            with self.assertRaises(MbError):
                call()

    def test_producer_of_a_workflow_path(self) -> None:
        self.assertEqual(workflow.ci_producer(".github/workflows/mod-base-build.yml"), "build")
        self.assertEqual(workflow.ci_producer(".github/workflows/mod-base-packaged-e2e.yml"), "packaged")
        for path in (".github/workflows/mod-base-gate-status.yml", ".github/workflows/mod-base-guard.yml",
                     ".github/workflows/build.yml", ".github/workflows/pages.yml", "", None):
            with self.subTest(path=path), self.assertRaises(MbError):
                workflow.ci_producer(path)


class FindJobTest(unittest.TestCase):
    JOBS = [
        {"id": 1, "name": "Verify pinned mod-base", "run_attempt": 2, "conclusion": "success"},
        {"id": 2, "name": "Publish / Collect mc1.20.1", "run_attempt": 2, "conclusion": "success"},
        {"id": 3, "name": "Publish / Collect mc1.20.10", "run_attempt": 2, "conclusion": "success"},
    ]

    def test_exact_match_in_the_attempt(self) -> None:
        self.assertEqual(workflow.find_job(self.JOBS, "Publish / Collect mc1.20.1", run_attempt=2)["id"], 2)

    def test_never_matches_suffixes_or_prefixes(self) -> None:
        for name in ("Collect mc1.20.1", "Publish / Collect mc1.20", "publish / collect mc1.20.1", " Publish / Collect mc1.20.1"):
            with self.subTest(name=name), self.assertRaises(MbError):
                workflow.find_job(self.JOBS, name, run_attempt=2)

    def test_duplicates_and_other_attempts_fail_closed(self) -> None:
        duplicated = self.JOBS + [{"id": 4, "name": "Verify pinned mod-base", "run_attempt": 1}]
        with self.assertRaisesRegex(MbError, "found 2"):
            workflow.find_job(duplicated, "Verify pinned mod-base", run_attempt=2)
        with self.assertRaisesRegex(MbError, "does not belong to attempt 1"):
            workflow.find_job(self.JOBS, "Verify pinned mod-base", run_attempt=1)
        with self.assertRaises(MbError):
            workflow.find_job([{"id": 1, "name": "x", "run_attempt": True}], "x", run_attempt=1)

    def test_malformed_input(self) -> None:
        for jobs, name, attempt in (
            ([{"name": 5}], "x", 1), (["x"], "x", 1), ("x", "x", 1), (self.JOBS, "", 1), (self.JOBS, "x", 0),
            (self.JOBS, "x", True),
        ):
            with self.assertRaises(MbError):
                workflow.find_job(jobs, name, run_attempt=attempt)

    def test_returns_a_copy(self) -> None:
        job = workflow.find_job(self.JOBS, "Verify pinned mod-base", run_attempt=2)
        job["name"] = "changed"
        self.assertEqual(self.JOBS[0]["name"], "Verify pinned mod-base")


if __name__ == "__main__":
    unittest.main()
