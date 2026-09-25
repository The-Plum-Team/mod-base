"""Every Pages job and step display name equals ``mod_base.workflow`` (SPEC §5.9).

The jobs API reports a callee job as ``"<caller job name> / <callee job name>"`` and consumers
match it with exact equality, so a renamed job or step in the YAML would silently break
admission, build, refresh and feature coverage. Matrix names are compared in their
``${{ matrix.* }}`` template form and, rendered, with :func:`mod_base.workflow.api_job_name`.
"""

from __future__ import annotations

import unittest

from mod_base import workflow
from tests.test_workflow_policy import COMPOSITES, callee, caller, composite

#: YAML job id -> ``workflow.CALLEE`` job key.
CALLEE_JOBS = {
    "publish": {"admit": "admit", "collect": "collect", "family": "family", "build": "build"},
    "finalize": {"refresh": "refresh", "refresh-family": "refresh_family"},
    "rotate": {"rotate": "rotate"},
}
#: ``workflow.STEPS`` key -> the (callee, job) that must hold exactly one step of that name.
STEP_OWNERS = {
    "select": ("publish", "collect"),
    "family_select": ("publish", "family"),
    "cache_upload": ("finalize", "refresh"),
    "family_cache_upload": ("finalize", "refresh-family"),
    "baseline_upload": ("finalize", "refresh"),
}
CALLER_JOBS = {"verify-kit": "verify_kit", "publish": "publish", "deploy": "deploy", "finalize": "finalize",
               "request-rotation": "request_rotation", "rotate": "rotate"}


class JobNameTests(unittest.TestCase):
    def test_callee_job_names_are_the_workflow_templates(self) -> None:
        for name, jobs in CALLEE_JOBS.items():
            document = callee(name)
            self.assertEqual(list(document["jobs"]), list(jobs), name)
            for job_id, key in jobs.items():
                with self.subTest(callee=name, job=job_id):
                    self.assertEqual(document["jobs"][job_id]["name"], workflow.workflow_template_name(name, key))

    def test_rendered_matrix_names_are_the_api_names(self) -> None:
        fields = {"key": "mc1.20.1", "family": "mod-compatibility"}
        for name, jobs in CALLEE_JOBS.items():
            for job_id, key in jobs.items():
                template = callee(name)["jobs"][job_id]["name"]
                rendered = template
                used = {}
                for field, expression in workflow.MATRIX_EXPRESSIONS.items():
                    if expression in rendered:
                        rendered = rendered.replace(expression, fields[field])
                        used[field] = fields[field]
                with self.subTest(callee=name, job=job_id):
                    self.assertNotIn("${{", rendered)
                    self.assertEqual(f"{workflow.CALLER[name]} / {rendered}", workflow.api_job_name(name, key, **used))
                    if used:
                        matrix = callee(name)["jobs"][job_id]["strategy"]["matrix"]
                        self.assertTrue(set(used) <= {"key", "family"})
                        self.assertTrue("key" in matrix or "include" in matrix)

    def test_caller_names(self) -> None:
        document = caller()
        self.assertEqual(document["name"], workflow.PAGES_WORKFLOW_NAME)
        self.assertEqual({job_id: job["name"] for job_id, job in document["jobs"].items()},
                         {job_id: workflow.CALLER[key] for job_id, key in CALLER_JOBS.items()})
        for job_id, name in (("publish", "publish"), ("finalize", "finalize"), ("rotate", "rotate")):
            self.assertTrue(document["jobs"][job_id]["uses"].startswith(
                f"The-Plum-Team/mod-base/{workflow.CALLEE_WORKFLOWS[name]}@"))


class StepNameTests(unittest.TestCase):
    def test_consumed_step_names_exist_exactly_where_consumers_look(self) -> None:
        for key, (name, job_id) in STEP_OWNERS.items():
            expected = workflow.step_name(key)
            with self.subTest(step=key):
                owners = [(workflow_name, other_job)
                          for workflow_name in CALLEE_JOBS
                          for other_job, job in callee(workflow_name)["jobs"].items()
                          for item in job["steps"] if item["name"] == expected]
                self.assertEqual(owners, [(name, job_id)])

    def test_step_names_are_unique_within_every_job_and_composite(self) -> None:
        for name in CALLEE_JOBS:
            for job_id, job in callee(name)["jobs"].items():
                names = [item["name"] for item in job["steps"]]
                self.assertEqual(len(names), len(set(names)), f"{name}/{job_id}")
                self.assertTrue(all(isinstance(item, str) and item for item in names))
        for job_id, job in caller()["jobs"].items():
            names = [item["name"] for item in job.get("steps", [])]
            self.assertEqual(len(names), len(set(names)), job_id)
        for name in COMPOSITES:
            names = [item["name"] for item in composite(name)["runs"]["steps"]]
            self.assertEqual(len(names), len(set(names)), name)

    def test_the_upload_steps_consumers_bind_are_named_as_the_spec_says(self) -> None:
        refresh = [item["name"] for item in callee("finalize")["jobs"]["refresh"]["steps"]]
        self.assertLess(refresh.index("Revalidate the promoted bundle"), refresh.index(workflow.STEPS["cache_upload"]))
        self.assertLess(refresh.index(workflow.STEPS["cache_upload"]), refresh.index(workflow.STEPS["baseline_upload"]))
        collect = [item["name"] for item in callee("publish")["jobs"]["collect"]["steps"]]
        self.assertEqual(collect[6], workflow.STEPS["select"])
        family = [item["name"] for item in callee("publish")["jobs"]["family"]["steps"]]
        self.assertEqual(family[6], workflow.STEPS["family_select"])


if __name__ == "__main__":
    unittest.main()
