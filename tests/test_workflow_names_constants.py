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
