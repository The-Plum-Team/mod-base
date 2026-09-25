"""``publish.yml`` (SPEC §5.3): job graph, step order and the executed shell of its steps.

The shell of every non-trivial step runs under stub ``gh``/``git``/``python3``: call-input
validation, the exact kit argv of ``admit``/``select``/``authenticate``/``compact``/``validate``/
``family collect``/``build``, the collect route decision, the family exit-3 semantics (no evidence
or superseded/unavailable means "no upload", never a failed publication), inert ancestry fetches
and the live head recheck.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mod_base import workflow
from tests.test_workflow_policy import (PROLOGUE, SCRUB, SCRUB_API, TOKEN_VALUE, UPLOAD, UPLOAD_PAGES, ShellHarness,
                                        callee, outputs, parse_kit_argv, require_tools, step)

REPOSITORY = "The-Plum-Team/Quick-Skin-Mod"
HEAD = "a" * 40
OTHER = "b" * 40
RECORDED_ENV = ("PYTHONPATH", "PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE", "GH_TOKEN",
                "GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_RUNTIME_TOKEN", "GIT_TERMINAL_PROMPT")
PYTHON_STUB = """
outputs_json = os.environ.get("STUB_OUTPUTS")
if outputs_json and os.environ.get("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        for name, value in json.loads(outputs_json).items():
            stream.write(name + "=" + value + "\\n")
code = int(os.environ.get("STUB_EXIT", "0"))
if code:
    raise SystemExit(code)
"""
KIT_PREFIX = ["-P", "-m", "mod_base"]
AFTER_PROLOGUE = {
    "admit": ["Admit an authenticated publication", "Report the admission decision"],
    "collect": [workflow.STEPS["select"], "Download the selected artifact by immutable ID",
                "Authenticate the selected source", "Compose selected evidence with its authenticated baseline",
                "Compact and bind the public derivatives", "Validate the compact bundle in a fresh process",
                "Recheck the source head", "Upload the collected bundle for the atomic build"],
    "family": [workflow.STEPS["family_select"], "Download the selected family generation by immutable ID",
               "Fetch coverage ancestry as inert objects", "Validate the family through the mod adapter",
               "Recheck the source head", "Upload the collected family bundle for the atomic build"],
    "build": ["Recheck and render the atomic site", "Upload the atomic Pages artifact", "Upload the promotion record"],
}


class PublishStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = callee("publish")
        self.jobs = self.document["jobs"]

    def test_job_graph(self) -> None:
        self.assertEqual(list(self.jobs), ["admit", "collect", "family", "build"])
        self.assertNotIn("needs", self.jobs["admit"])
        self.assertNotIn("if", self.jobs["admit"])
        self.assertEqual(self.jobs["collect"]["needs"], "admit")
        self.assertEqual(self.jobs["collect"]["if"], "needs.admit.outputs.eligible == 'true'")
        self.assertEqual(self.jobs["family"]["needs"], "admit")
        self.assertEqual(self.jobs["family"]["if"],
                         "needs.admit.outputs.eligible == 'true' && needs.admit.outputs.families != '[]'")
        self.assertEqual(self.jobs["build"]["needs"], ["admit", "collect", "family"])
        self.assertEqual(self.jobs["build"]["if"],
                         "!cancelled() && needs.admit.outputs.eligible == 'true' && needs.collect.result == 'success' "
                         "&& (needs.family.result == 'success' || (needs.family.result == 'skipped' && "
                         "needs.admit.outputs.families == '[]'))")
        self.assertEqual(self.jobs["collect"]["strategy"], {
            "fail-fast": "false", "max-parallel": "3",
            "matrix": {"key": "${{ fromJSON(needs.admit.outputs.bundle_keys) }}"}})
        self.assertEqual(self.jobs["family"]["strategy"], {
            "fail-fast": "false", "max-parallel": "3",
            "matrix": {"include": "${{ fromJSON(needs.admit.outputs.families) }}"}})
        self.assertEqual({job_id: job["timeout-minutes"] for job_id, job in self.jobs.items()},
                         {"admit": "10", "collect": "20", "family": "20", "build": "20"})

    def test_outputs(self) -> None:
        self.assertEqual(self.jobs["admit"]["outputs"], {
            name: f"${{{{ steps.admit.outputs.{name} }}}}"
            for name in ("eligible", "reason", "bundle_keys", "subjects", "families", "nominations", "heads")})
        self.assertEqual(self.jobs["build"]["outputs"], {"heads": "${{ steps.build.outputs.heads }}"})
        declared = self.document["on"]["workflow_call"]["outputs"]
        self.assertEqual({name: spec["value"] for name, spec in declared.items()}, {
            "eligible": "${{ jobs.admit.outputs.eligible }}", "bundle_keys": "${{ jobs.admit.outputs.bundle_keys }}",
            "families": "${{ jobs.admit.outputs.families }}", "heads": "${{ jobs.build.outputs.heads }}"})

    def test_steps_after_the_prologue(self) -> None:
        for job_id, names in AFTER_PROLOGUE.items():
            with self.subTest(job=job_id):
                self.assertEqual([item["name"] for item in self.jobs[job_id]["steps"]], [*PROLOGUE, *names])

    def test_step_conditions_and_wiring(self) -> None:
        collect = self.jobs["collect"]["steps"]
        self.assertEqual(step(collect, "Compose selected evidence with its authenticated baseline")["if"],
                         "steps.authenticate.outputs.route == 'compose'")
        self.assertEqual(step(collect, "Compact and bind the public derivatives")["if"],
                         "steps.authenticate.outputs.route == 'reencode' || "
                         "steps.authenticate.outputs.route == 'cache'")
        self.assertEqual(step(collect, workflow.STEPS["select"])["env"], {
            "GH_TOKEN": TOKEN_VALUE, "KEY": "${{ matrix.key }}", "SUBJECTS": "${{ needs.admit.outputs.subjects }}",
            "NOMINATIONS": "${{ needs.admit.outputs.nominations }}"})
        self.assertEqual(step(collect, "Validate the compact bundle in a fresh process")["env"], {
            "KEY": "${{ matrix.key }}", "ROUTE": "${{ steps.authenticate.outputs.route }}",
            "SUBJECT": "${{ steps.select.outputs.subject }}"})
        for item in collect:
            if "if" in item:
                self.assertIn(item["name"], ("Compose selected evidence with its authenticated baseline",
                                             "Compact and bind the public derivatives"))
        family = self.jobs["family"]["steps"]
        selected = "steps.select.outputs.selected == 'true'"
        collected = "steps.collect.outputs.collected == 'true'"
        self.assertEqual([item.get("if") for item in family[len(PROLOGUE):]],
                         [None, selected, selected, selected, collected, collected])
        self.assertEqual(step(family, workflow.STEPS["family_select"])["env"]["COVERAGE_SHA"],
                         "${{ matrix.coverage_sha }}")
        upload = step(self.jobs["build"]["steps"], "Upload the atomic Pages artifact")
        self.assertEqual(upload["uses"], UPLOAD_PAGES)
        promotion = step(self.jobs["build"]["steps"], "Upload the promotion record")
        self.assertEqual((promotion["uses"], promotion["with"]["path"]), (UPLOAD, "${{ runner.temp }}/mb/promotion"))
        for job_id in ("collect", "family"):
            upload = self.jobs[job_id]["steps"][-1]
            self.assertEqual(upload["with"]["path"], "${{ runner.temp }}/mb/collected")

    def test_input_validation_is_identical_and_token_free_in_every_job(self) -> None:
        bodies = {json.dumps(step(job["steps"], PROLOGUE[0]), sort_keys=True) for job in self.jobs.values()}
        self.assertEqual(len(bodies), 1)
        validation = step(self.jobs["admit"]["steps"], PROLOGUE[0])
        self.assertNotIn("GH_TOKEN", validation["env"])
        self.assertEqual(validation["run"].splitlines()[1], SCRUB)


class PublishShellTests(unittest.TestCase):
    def setUp(self) -> None:
        require_tools("bash", "jq")
        temporary = tempfile.TemporaryDirectory(prefix="publish shell ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.harness = ShellHarness(self.root / "harness")
        self.runner_temp = self.root / "runner temp"
        self.runner_temp.mkdir()
        self.output = self.root / "github_output"
        self.jobs = callee("publish")["jobs"]

    def invoke(self, job: str, name: str, env: dict[str, str], *, fixtures: dict | None = None):
        if self.output.exists():
            self.output.unlink()
        base = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": HEAD, "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_OUTPUT": str(self.output), "GITHUB_STEP_SUMMARY": str(self.root / "summary"),
                "RUNNER_TEMP": str(self.runner_temp), "GITHUB_WORKSPACE": str(self.root / "workspace"),
                "STUB_PYTHON3_SCRIPT": PYTHON_STUB, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc",
                "ACTIONS_RUNTIME_TOKEN": "runtime", "GITHUB_TOKEN": "ambient"}
        script = step(self.jobs[job]["steps"], name)["run"]
        result = self.harness.run(script, {**base, **env}, fixtures=fixtures, record_env=RECORDED_ENV)
        return result, outputs(self.output), self.harness.records()

    def kit_calls(self, records: list[dict]) -> list[list[str]]:
        calls = []
        for record in records:
            if record["tool"] == "python3":
                self.assertEqual(record["argv"][:3], KIT_PREFIX)
                self.assertEqual(record["env"]["PYTHONPATH"], str(self.root / "workspace" / "kit/src"))
                self.assertEqual([record["env"][name] for name in
                                  ("PYTHONSAFEPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONNOUSERSITE")], ["1", "1", "1"])
                for name in ("GITHUB_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_RUNTIME_TOKEN"):
                    self.assertIsNone(record["env"][name], name)
                calls.append(record["argv"][3:])
                parse_kit_argv(record["argv"][3:])
        return calls

    # -- call inputs -----------------------------------------------------------------------------

    def test_call_input_validation(self) -> None:
        family = {"FAMILY": "mod-compatibility", "BUNDLE_KEY": "mc1.20.1", "ARTIFACT_ID": "812345",
                  "ARTIFACT_DIGEST": "sha256:" + "c" * 64, "COVERAGE_SHA": HEAD}
        empty = {name: "" for name in ("RUN_ID", "SHA", "FAMILY", "BUNDLE_KEY", "ARTIFACT_ID", "ARTIFACT_DIGEST",
                                       "COVERAGE_SHA")}
        accepted = [
            {"GITHUB_EVENT_NAME": "schedule", "OPERATION": "recovery"},
            {"OPERATION": "manual"},
            {"OPERATION": "deploy", "RUN_ID": "36042781699", "SHA": HEAD},
            {"OPERATION": "family", "RUN_ID": "36042781699", "SHA": HEAD, **family},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "BUNDLE_KEY": "0123456789abcdef01234567"},
        ]
        rejected = [
            {"GITHUB_EVENT_NAME": "schedule", "OPERATION": "manual"},
            {"GITHUB_EVENT_NAME": "push", "OPERATION": "deploy", "RUN_ID": "1", "SHA": HEAD},
            {"GITHUB_EVENT_NAME": "pull_request_target", "OPERATION": "manual"},
            {"OPERATION": "recovery"},
            {"OPERATION": "rotate", "RUN_ID": "1", "SHA": HEAD},
            {"OPERATION": ""},
            {"OPERATION": "manual", "RUN_ID": "1"},
            {"OPERATION": "manual", "FAMILY": "mod-compatibility"},
            {"OPERATION": "deploy", "RUN_ID": "1"},
            {"OPERATION": "deploy", "SHA": HEAD},
            {"OPERATION": "deploy", "RUN_ID": "0", "SHA": HEAD},
            {"OPERATION": "deploy", "RUN_ID": "1234567890123456789", "SHA": HEAD},
            {"OPERATION": "deploy", "RUN_ID": "1\n2", "SHA": HEAD},
            {"OPERATION": "deploy", "RUN_ID": "1", "SHA": HEAD.upper()},
            {"OPERATION": "deploy", "RUN_ID": "1", "SHA": HEAD, "FAMILY": "mod-compatibility"},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "FAMILY": "Mod"},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "FAMILY": "a" * 33},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "BUNDLE_KEY": "mc--1"},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "BUNDLE_KEY": "x"},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "ARTIFACT_DIGEST": "c" * 64},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "ARTIFACT_ID": "-1"},
            {"OPERATION": "family", "RUN_ID": "1", "SHA": HEAD, **family, "COVERAGE_SHA": ""},
            {"OPERATION": "manual", "KIT_SHA": "main"},
            {"OPERATION": "manual", "GITHUB_SHA": "HEAD"},
        ]
        for case in accepted:
            with self.subTest(accepted=case):
                result, _, records = self.invoke("admit", PROLOGUE[0], {"KIT_SHA": OTHER, **empty, **case})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(records, [])
        for case in rejected:
            with self.subTest(rejected=case):
                result, _, records = self.invoke("admit", PROLOGUE[0], {"KIT_SHA": OTHER, **empty, **case})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid call input", result.stderr)
                self.assertEqual(records, [])

    # -- admit -----------------------------------------------------------------------------------

    def test_admit_passes_only_the_given_wake_identifiers(self) -> None:
        empty = {name: "" for name in ("RUN_ID", "SHA", "FAMILY", "BUNDLE_KEY", "ARTIFACT_ID", "ARTIFACT_DIGEST",
                                       "COVERAGE_SHA")}
        result, _, records = self.invoke("admit", "Admit an authenticated publication",
                                         {"GH_TOKEN": "t", "OPERATION": "recovery", **empty})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_calls(records), [["admit", "--repo", "mod", "--config", "mod/site/mod-base.json",
                                                    "--operation", "recovery", "--github-output", str(self.output)]])
        self.assertEqual(records[0]["env"]["GH_TOKEN"], "t")
        wake = {"RUN_ID": "7", "SHA": HEAD, "FAMILY": "mod-compatibility", "BUNDLE_KEY": "mc26.3",
                "ARTIFACT_ID": "9", "ARTIFACT_DIGEST": "sha256:" + "d" * 64, "COVERAGE_SHA": HEAD}
        result, _, records = self.invoke("admit", "Admit an authenticated publication",
                                         {"GH_TOKEN": "t", "OPERATION": "family", **wake})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_calls(records)[0][7:], [
            "--github-output", str(self.output), "--run-id", "7", "--sha", HEAD, "--family", "mod-compatibility",
            "--bundle-key", "mc26.3", "--artifact-id", "9", "--artifact-digest", "sha256:" + "d" * 64,
            "--coverage-sha", HEAD])

    def test_admission_report_accepts_only_kit_shaped_values(self) -> None:
        result, _, _ = self.invoke("admit", "Report the admission decision",
                                   {"ELIGIBLE": "false", "REASON": "awaiting-complete-v1-evidence"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("reason: awaiting-complete-v1-evidence", (self.root / "summary").read_text())
        for env in ({"ELIGIBLE": "yes", "REASON": "current"}, {"ELIGIBLE": "true", "REASON": "::set-output x"}):
            result, _, _ = self.invoke("admit", "Report the admission decision", env)
            self.assertNotEqual(result.returncode, 0)

    # -- collect ---------------------------------------------------------------------------------

    def collect_select(self, key: str, subjects: object, nominations: object, **extra: str):
        return self.invoke("collect", workflow.STEPS["select"], {
            "GH_TOKEN": "t", "KEY": key, "SUBJECTS": json.dumps(subjects), "NOMINATIONS": json.dumps(nominations),
            **extra})

    def test_select_binds_the_admitted_subject_and_nomination(self) -> None:
        subjects = {"mc1.20.1": {"branch": "master", "commit": HEAD, "tree": OTHER}}
        result, values, records = self.collect_select("mc1.20.1", subjects, {"mc1.20.1": 812345})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"subject": HEAD})
        self.assertEqual(self.kit_calls(records), [[
            "select", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1",
            "--expected-subject-commit", HEAD, "--github-output", str(self.output),
            "--output", str(self.runner_temp / "mb/selected.json"), "--nomination", "812345"]])
        (self.runner_temp / "mb").rmdir()
        result, _, records = self.collect_select("mc1.20.1", subjects, {})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--nomination", self.kit_calls(records)[0])

    def test_select_rejects_malformed_admission_outputs(self) -> None:
        good = {"mc1.20.1": {"branch": "master", "commit": HEAD, "tree": OTHER}}
        cases = [("MC1", good, {}), ("mc1.20.1", {}, {}), ("mc1.20.1", {"mc1.20.1": {"commit": "HEAD"}}, {}),
                 ("mc1.20.1", good, {"mc1.20.1": "12; rm -rf /"}), ("mc1.20.1", good, {"mc1.20.1": 0}),
                 ("mc1.20.1", good, {"mc1.20.1": {"id": 1}}), ("mc1.20.1", ["x"], {})]
        for key, subjects, nominations in cases:
            with self.subTest(key=key, subjects=subjects, nominations=nominations):
                result, values, records = self.collect_select(key, subjects, nominations)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.kit_calls(records), [])
                self.assertEqual(values, {})

    def test_select_failure_including_no_evidence_fails_the_leg(self) -> None:
        subjects = {"mc1.20.1": {"branch": "master", "commit": HEAD, "tree": OTHER}}
        for code in ("2", "3"):
            result, values, _ = self.collect_select("mc1.20.1", subjects, {}, STUB_EXIT=code)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("subject", values)
            (self.runner_temp / "mb").rmdir()

    def test_downloads_are_exactly_the_selected_artifact(self) -> None:
        selected = {"ARTIFACT_ID": "812345", "ARTIFACT_NAME": "mb-handoff--mc1.20.1--a2",
                    "ARTIFACT_DIGEST": "sha256:" + "e" * 64, "ARTIFACT_SIZE": "4096", "ARTIFACT_RUN_ID": "36042781699"}
        for job, name in (("collect", "Download the selected artifact by immutable ID"),
                          ("family", "Download the selected family generation by immutable ID")):
            with self.subTest(job=job):
                result, _, records = self.invoke(job, name, {"GH_TOKEN": "t", **selected})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.kit_calls(records), [[
                    "download", "--artifact-id", "812345", "--name", "mb-handoff--mc1.20.1--a2", "--digest",
                    "sha256:" + "e" * 64, "--size", "4096", "--run-id", "36042781699", "--output",
                    str(self.runner_temp / "mb/selected")]])
                self.assertEqual(records[0]["env"]["GH_TOKEN"], "t")
        family = step(self.jobs["family"]["steps"], "Download the selected family generation by immutable ID")
        collect = step(self.jobs["collect"]["steps"], "Download the selected artifact by immutable ID")
        self.assertEqual(family["run"], collect["run"])
        self.assertEqual(family["env"], collect["env"])

    def authenticate(self, kind: str, scope: str | None, **extra: str):
        selected = self.runner_temp / "mb/selected"
        selected.mkdir(parents=True, exist_ok=True)
        manifest = selected / "manifest.json"
        manifest.write_text(json.dumps({"scope": {"kind": scope}} if scope is not None else {"x": 1}))
        return self.invoke("collect", "Authenticate the selected source",
                           {"GH_TOKEN": "t", "KEY": "mc1.20.1", "SELECTED_KIND": kind, **extra})

    def test_authenticate_then_route_by_selected_kind_and_scope(self) -> None:
        routes = {("handoff", "selected"): "compose", ("handoff", "complete"): "reencode",
                  ("cache", "complete"): "cache", ("cache", "composed"): "cache"}
        for (kind, scope), route in routes.items():
            with self.subTest(kind=kind, scope=scope):
                result, values, records = self.authenticate(kind, scope)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(values, {"route": route})
                self.assertEqual(self.kit_calls(records), [[
                    "authenticate", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1",
                    "--selected", str(self.runner_temp / "mb/selected"), "--selected-json",
                    str(self.runner_temp / "mb/selected.json"), "--output",
                    str(self.runner_temp / "mb/selection.json")]])
        for kind, scope in (("cache", "selected"), ("handoff", "composed"), ("family-handoff", "complete"),
                            ("handoff", None), ("handoff", "complete\nroute=compose")):
            with self.subTest(kind=kind, scope=scope):
                result, values, _ = self.authenticate(kind, scope)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(values, {})
        result, values, _ = self.authenticate("handoff", "complete", STUB_EXIT="2")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(values, {})

    def test_compose_compact_and_fresh_validation_arguments(self) -> None:
        mb = str(self.runner_temp / "mb")
        result, _, records = self.invoke("collect", "Compose selected evidence with its authenticated baseline",
                                         {"GH_TOKEN": "t", "KEY": "mc1.20.1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_calls(records), [[
            "compose", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--selected",
            f"{mb}/selected", "--selection", f"{mb}/selection.json", "--output", f"{mb}/collected"]])
        self.assertEqual(records[0]["env"]["GH_TOKEN"], "t")
        result, _, records = self.invoke("collect", "Compact and bind the public derivatives",
                                         {"GH_TOKEN": "leak", "KEY": "mc1.20.1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_calls(records), [[
            "compact", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--input",
            f"{mb}/selected", "--selection", f"{mb}/selection.json", "--output", f"{mb}/collected"]])
        self.assertIsNone(records[0]["env"]["GH_TOKEN"], "compaction never holds a token")
        base = ["validate", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--kind",
                "compact", "--input", f"{mb}/collected", "--expected-subject-commit", HEAD]
        for route, extra in (("reencode", ["--bind-raw", f"{mb}/selected"]), ("compose", []), ("cache", [])):
            with self.subTest(route=route):
                result, _, records = self.invoke("collect", "Validate the compact bundle in a fresh process",
                                                 {"KEY": "mc1.20.1", "ROUTE": route, "SUBJECT": HEAD})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.kit_calls(records), [base + extra])
        for env in ({"ROUTE": "other", "SUBJECT": HEAD}, {"ROUTE": "", "SUBJECT": HEAD},
                    {"ROUTE": "cache", "SUBJECT": "HEAD"}):
            result, _, records = self.invoke("collect", "Validate the compact bundle in a fresh process",
                                             {"KEY": "mc1.20.1", **env})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.kit_calls(records), [])

    def recheck(self, job: str, subjects: object, branch_response: object):
        fixtures = {f"GET repos/{REPOSITORY}/branches/release%2F1.20": branch_response,
                    f"GET repos/{REPOSITORY}/branches/master": branch_response}
        return self.invoke(job, "Recheck the source head",
                           {"GH_TOKEN": "t", "KEY": "k1", "SUBJECTS": json.dumps(subjects)}, fixtures=fixtures)

    def test_head_recheck_fails_a_stale_collection(self) -> None:
        for job in ("collect", "family"):
            with self.subTest(job=job):
                subjects = {"k1": {"branch": "release/1.20", "commit": HEAD, "tree": OTHER}}
                result, _, records = self.recheck(job, subjects, {"body": {"commit": {"sha": HEAD}}})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([record["route"] for record in records if record["tool"] == "gh"],
                                 [f"GET repos/{REPOSITORY}/branches/release%2F1.20"])
                result, _, _ = self.recheck(job, subjects, {"body": {"commit": {"sha": OTHER}}})
                self.assertEqual(result.returncode, 1)
                self.assertIn("this collection is stale", result.stderr)
                result, _, _ = self.recheck(job, subjects, [{"fail": "gh: Bad Gateway (HTTP 502)"},
                                                            {"body": {"commit": {"sha": HEAD}}}])
                self.assertEqual(result.returncode, 0, result.stderr)
                for bad in ({"k1": {"branch": "../x", "commit": HEAD}}, {"k1": {"branch": "master", "commit": "x"}},
                            {}):
                    result, _, records = self.recheck(job, bad, {"body": {"commit": {"sha": HEAD}}})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse([record for record in records if record["tool"] == "gh"])

    # -- family ----------------------------------------------------------------------------------

    def family_select(self, code: str = "0", **overrides: str):
        env = {"GH_TOKEN": "t", "FAMILY": "mod-compatibility", "KEY": "mc1.20.1", "COVERAGE_SHA": HEAD,
               "SUBJECTS": json.dumps({"mc1.20.1": {"branch": "master", "commit": HEAD, "tree": OTHER}}),
               "NOMINATIONS": json.dumps({"mod-compatibility/mc1.20.1": 99, "mc1.20.1": 12}), "STUB_EXIT": code,
               **overrides}
        mb = self.runner_temp / "mb"
        if mb.exists():
            mb.rmdir()
        return self.invoke("family", workflow.STEPS["family_select"], env)

    def test_family_select_treats_absent_evidence_as_unavailable(self) -> None:
        result, values, records = self.family_select()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"selected": "true"})
        self.assertEqual(self.kit_calls(records), [[
            "select", "--repo", "mod", "--config", "mod/site/mod-base.json", "--key", "mc1.20.1", "--family",
            "mod-compatibility", "--expected-subject-commit", HEAD, "--github-output", str(self.output), "--output",
            str(self.runner_temp / "mb/selected.json"), "--nomination", "99"]])
        result, values, _ = self.family_select("3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"selected": "false"})
        for code in ("1", "2", "78"):
            result, values, _ = self.family_select(code)
            self.assertEqual(result.returncode, int(code))
            self.assertEqual(values, {})

    def test_family_select_rejects_a_leg_that_disagrees_with_admission(self) -> None:
        for overrides in ({"COVERAGE_SHA": OTHER}, {"FAMILY": "Bad"}, {"KEY": "mc--1"}, {"COVERAGE_SHA": "x"},
                          {"NOMINATIONS": json.dumps({"mod-compatibility/mc1.20.1": "x"})}):
            with self.subTest(overrides=overrides):
                result, values, records = self.family_select(**overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.kit_calls(records), [])

    def test_family_collect_uploads_only_available_generations(self) -> None:
        env = {"FAMILY": "mod-compatibility", "KEY": "mc1.20.1", "COVERAGE_SHA": HEAD}
        mb = str(self.runner_temp / "mb")
        result, values, records = self.invoke("family", "Validate the family through the mod adapter", env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(values, {"collected": "true"})
        self.assertEqual(self.kit_calls(records), [[
            "family", "collect", "--repo", "mod", "--config", "mod/site/mod-base.json", "--family",
            "mod-compatibility", "--key", "mc1.20.1", "--input", f"{mb}/selected", "--expected-coverage-sha", HEAD,
            "--output", f"{mb}/collected"]])
        self.assertIsNone(records[0]["env"]["GH_TOKEN"], "family_validate is never a network hook")
        result, values, _ = self.invoke("family", "Validate the family through the mod adapter",
                                        {**env, "STUB_EXIT": "3"})
        self.assertEqual((result.returncode, values), (0, {"collected": "false"}), result.stderr)
        result, values, _ = self.invoke("family", "Validate the family through the mod adapter",
                                        {**env, "STUB_EXIT": "2"})
        self.assertEqual((result.returncode, values), (2, {}))

    def fetch(self, envelope: object, present: set[str], **env: str):
        selected = self.runner_temp / "mb/selected"
        selected.mkdir(parents=True, exist_ok=True)
        (selected / "envelope.json").write_text(json.dumps(envelope) if not isinstance(envelope, str) else envelope)
        script = """
state = pathlib.Path(os.environ["STUB_CALLS"] + ".git")
fetched = set(state.read_text().split()) if state.exists() else set()
present = set(os.environ["STUB_PRESENT"].split(",")) | fetched
if arguments[:2] == ["-C", "mod"] and arguments[2:4] == ["cat-file", "-e"]:
    raise SystemExit(0 if arguments[4].removesuffix("^{commit}") in present else 1)
if "fetch" in arguments:
    shas = arguments[arguments.index("origin") + 1:]
    state.write_text(" ".join(sorted(fetched | set(shas))))
    raise SystemExit(0)
raise SystemExit("unexpected git " + " ".join(arguments))
"""
        return self.invoke("family", "Fetch coverage ancestry as inert objects",
                           {"COVERAGE_SHA": HEAD, "STUB_GIT_SCRIPT": script, "STUB_PRESENT": ",".join(present),
                            **env})

    def test_ancestry_fetch_is_anonymous_inert_and_only_for_missing_commits(self) -> None:
        carried = "c" * 40
        result, _, records = self.fetch({"coverage_sha": OTHER, "carried_from": carried}, {HEAD})
        self.assertEqual(result.returncode, 0, result.stderr)
        fetches = [record for record in records if record["tool"] == "git" and "fetch" in record["argv"]]
        self.assertEqual([record["argv"] for record in fetches], [[
            "-C", "mod", "-c", "protocol.version=2", "-c", "credential.helper=", "fetch", "--no-tags", "--quiet",
            "origin", OTHER, carried]])
        self.assertEqual(fetches[0]["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertFalse(any("checkout" in record["argv"] for record in records))
        result, _, records = self.fetch({"coverage_sha": HEAD}, {HEAD})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse([record for record in records if "fetch" in record["argv"]])
        for envelope in ({"coverage_sha": "HEAD"}, {}, {"coverage_sha": HEAD, "carried_from": None},
                         {"coverage_sha": HEAD, "carried_from": "--upload-pack=x"}, "not json"):
            with self.subTest(envelope=envelope):
                result, _, records = self.fetch(envelope, {HEAD})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse([record for record in records if "fetch" in record["argv"]])

    def test_ancestry_that_cannot_be_fetched_fails(self) -> None:
        script_env = {"STUB_GIT_SCRIPT": "raise SystemExit(1 if 'cat-file' in arguments else 128)"}
        selected = self.runner_temp / "mb/selected"
        selected.mkdir(parents=True, exist_ok=True)
        (selected / "envelope.json").write_text(json.dumps({"coverage_sha": HEAD}))
        result, _, _ = self.invoke("family", "Fetch coverage ancestry as inert objects",
                                   {"COVERAGE_SHA": HEAD, **script_env})
        self.assertNotEqual(result.returncode, 0)

    # -- build -----------------------------------------------------------------------------------

    def test_build_renders_with_the_token_into_new_directories(self) -> None:
        mb = str(self.runner_temp / "mb")
        result, _, records = self.invoke("build", "Recheck and render the atomic site", {"GH_TOKEN": "t"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kit_calls(records), [[
            "build", "--repo", "mod", "--config", "mod/site/mod-base.json", "--kit-root", "kit", "--collected",
            f"{mb}/collected", "--families", f"{mb}/families", "--output", "_site", "--promotion", f"{mb}/promotion",
            "--github-output", str(self.output)]])
        self.assertEqual(records[0]["env"]["GH_TOKEN"], "t")
        build = step(callee("publish")["jobs"]["build"]["steps"], "Recheck and render the atomic site")
        self.assertEqual(build["run"].splitlines()[1], SCRUB_API)


if __name__ == "__main__":
    unittest.main()
