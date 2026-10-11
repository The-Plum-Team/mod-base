"""Pure protocol, exact graph and adversarial read-only API evidence tests."""

from __future__ import annotations

import copy
import hashlib
import unittest

from mod_base.build_ci.authenticate import authenticate_pr_identity, authenticate_source_identity, run_head
from mod_base.build_ci.graph import (BUILD_MODES, PACKAGED_MODES, BuildGraphV1, PackagedGraphV1, authenticate_graph,
                                     authenticate_referenced_workflows, gate_mode, job_name, require_graph,
                                     require_partial_graph, run_graph, sealed_upload, upload_job_name)
from mod_base.build_ci.protocol import plan_sha256, validate_plan
from mod_base.build_ci.reads import RERUN, CommandReads, Watch, require_ran_in_attempt
from mod_base.errors import MbError
from mod_base.github import jobs as github_jobs
from mod_base.github.fake import FakeGitHub
from mod_base.github.runs import referenced_workflows
from mod_base.model.documents import load_document
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_SEAL_STEP, CI_UPLOAD_STEP
from tests.helpers import CI_GRAPH_FIXTURES, ci_api_run, ci_failed_jobs_rerun, ci_graph_jobs, ci_plan, ci_staged_plan


def seeded_pr():
    plan = ci_plan()
    identity = plan["identity"]
    api = FakeGitHub(repository=identity["repository"], default_branch="master")
    api.set_branch("master", identity["base_sha"], "8" * 40)
    api.add_commit(identity["tested_sha"], identity["tested_tree"], parents=identity["tested_parents"])
    pr = {"number": 7, "state": "open", "draft": False, "mergeable": True, "merge_commit_sha": identity["tested_sha"]}
    for side in ("head", "base"):
        pr[side] = {"sha": identity[f"{side}_sha"], "ref": identity[f"{side}_branch"],
                    "repo": {"full_name": api.repository}}
    api.add_response(f"/repos/{api.repository}/pulls/7", pr)
    return plan, api, pr


def protected_subject(plan, api, *, current=False, parents=None):
    identity = plan["identity"]
    identity.update(pr_number=0, head_branch=identity["base_branch"])
    if current:
        identity["tested_sha"] = identity["controller_sha"]
        identity["tested_tree"] = "8" * 40
    identity["head_sha"] = identity["tested_sha"]
    identity["tested_parents"] = ["a" * 40] if parents is None else parents
    api.add_commit(identity["tested_sha"], identity["tested_tree"], parents=identity["tested_parents"])
    api.add_compare(identity["tested_sha"], identity["controller_sha"],
                    {"status": "ahead", "ahead_by": 4, "behind_by": 0})
    plan["plan_sha256"] = plan_sha256(plan)
    return identity


class PlanTests(unittest.TestCase):
    def test_complete_plan_is_strictly_loadable(self):
        plan = ci_plan()
        self.assertIs(validate_plan(plan), plan)
        self.assertEqual(load_document(canonical_json(plan), kind=plan["kind"]), plan)

    def test_hash_covers_identity_and_plan(self):
        for key in ("policy_sha256", "scenario_sha256", "inventory_sha256", "runtime_selection_sha256"):
            plan = ci_plan()
            plan["identity"][key] = "f" * 64
            with self.subTest(key=key), self.assertRaises(MbError):
                validate_plan(plan)

    def test_hostile_shapes_and_partial_plans(self):
        mutations = [
            lambda p: p.update(extra=True),
            lambda p: p["identity"].update(pr_number=True),
            lambda p: p["identity"].update(graph_version=2),
            lambda p: p["identity"].update(tested_parents=list(reversed(p["identity"]["tested_parents"]))),
            lambda p: p["targets"].append(copy.deepcopy(p["targets"][0])),
            lambda p: p["lanes"].append(copy.deepcopy(p["lanes"][0])),
            lambda p: p["targets"][0].update(java=True),
            lambda p: p["targets"][0]["outputs"].pop(),
            lambda p: p["targets"][0]["outputs"][0].update(path="../escape.jar"),
            lambda p: p["lanes"][0].update(target_id="missing"),
            lambda p: p["lanes"][0].update(obligations=[]),
            lambda p: p["lanes"][0]["obligations"].append(p["lanes"][0]["obligations"][0]),
            lambda p: p["targets"][0].update(command="arbitrary code"),
        ]
        for index, mutate in enumerate(mutations):
            plan = ci_plan()
            mutate(plan)
            plan["plan_sha256"] = plan_sha256(plan)
            with self.subTest(index=index), self.assertRaises(MbError):
                validate_plan(plan)

    def test_extra_plan_inputs_are_bound_by_the_plan_hash_in_name_order(self):
        def inputs(*names):
            return [{"name": name, "sha256": hashlib.sha256(name.encode()).hexdigest()} for name in names]

        plan = ci_plan()
        self.assertEqual([item["name"] for item in plan["plan_inputs"]], ["gradle-properties"])
        plan["plan_inputs"][0]["sha256"] = "f" * 64
        with self.assertRaisesRegex(MbError, "does not bind this plan"):
            validate_plan(plan)
        most = [f"input-{index}" for index in range(8)]
        cases = (
            ("none", [], None), ("two", inputs("gradle-properties", "versions.toml"), None), ("eight", inputs(*most), None),
            ("nine", inputs(*most, "input-8"), "must hold between 0 and 8 items"),
            ("unsorted", inputs("versions.toml", "gradle-properties"), "$.plan_inputs: must be sorted by name"),
            ("repeated", inputs("gradle-properties", "gradle-properties"), "duplicates an earlier item"),
            ("a path instead of a name", inputs("gradle/properties"), "does not match the required grammar"),
            ("a name in upper case", inputs("Gradle"), "does not match the required grammar"),
            ("a repository path", [{**inputs("gradle-properties")[0], "path": "gradle.properties"}], "has unknown keys"),
            ("no hash", [{"name": "gradle-properties"}], "missing required keys"),
            ("a short hash", [{"name": "gradle-properties", "sha256": "f" * 63}], "does not match the required grammar"),
            ("a mapping", {"gradle-properties": "f" * 64}, "must be an array"),
        )
        for label, value, message in cases:
            plan = ci_plan()
            plan["plan_inputs"] = value
            plan["plan_sha256"] = plan_sha256(plan)
            with self.subTest(case=label):
                if message is None:
                    validate_plan(plan)
                else:
                    with self.assertRaises(MbError) as caught:
                        validate_plan(plan)
                    self.assertIn(message, str(caught.exception))
        missing = ci_plan()
        del missing["plan_inputs"]
        missing["plan_sha256"] = plan_sha256(missing)
        with self.assertRaisesRegex(MbError, "missing required keys"):
            validate_plan(missing)

    def staged(self, mutate=None):
        plan = ci_staged_plan()
        if mutate is not None:
            mutate({target["id"]: target["outputs"] for target in plan["targets"]}, plan)
        plan["plan_sha256"] = plan_sha256(plan)
        return plan

    def test_a_plan_describes_what_the_mods_stage(self):
        plan = self.staged()
        self.assertIs(validate_plan(plan), plan)
        self.assertEqual(load_document(canonical_json(plan), kind=plan["kind"]), plan)
        outputs = [output for target in plan["targets"] for output in target["outputs"]]
        # Two lanes share one manifest and one SBOM; the other target stages no SBOM at all.
        self.assertEqual([(output["role"], output["lane_id"]) for output in outputs if output["lane_id"] is None],
                         [("native-report", None), ("sbom", None), ("native-report", None)])
        self.assertEqual(sum(output["role"] in ("production", "harness") for output in outputs), 6)

    def test_optional_and_repeatable_outputs_of_either_scope_are_planned(self):
        def output(path, lane, role):
            return {"path": path, "lane_id": lane, "role": role}

        accepted = {
            "no SBOM anywhere": lambda outputs, plan: outputs["target-a"].pop(),
            "an SBOM per lane": lambda outputs, plan: outputs["target-a"].extend(
                output(f"sbom/{lane}.cdx.json", lane, "sbom") for lane in ("lane-a", "lane-b")),
            "reports of a lane and of the target": lambda outputs, plan: outputs["target-a"].extend(
                [output("reports/lane-a/jdk-probe.json", "lane-a", "native-report"),
                 output("reports/lane-a/tasks.json", "lane-a", "native-report"),
                 output("targets/target-a/build-matrix-report.json", None, "native-report")]),
            "logs of a lane and of the target": lambda outputs, plan: outputs["target-c"].extend(
                [output("logs/lane-c.log", "lane-c", "build-log"), output("logs/lane-c.2.log", "lane-c", "build-log"),
                 output("targets/target-c/gradle.log", None, "build-log")]),
            "a lane report instead of a manifest": lambda outputs, plan: outputs["target-c"].__setitem__(
                -1, output("reports/lane-c.json", "lane-c", "native-report")),
        }
        for label, mutate in accepted.items():
            with self.subTest(case=label):
                validate_plan(self.staged(mutate))

    def test_output_rules_that_no_staging_may_break(self):
        def output(path, lane, role):
            return {"path": path, "lane_id": lane, "role": role}

        def scope(outputs, index, lane):
            outputs[index]["lane_id"] = lane

        rejected = {
            "production of the whole target": (lambda outputs, plan: scope(outputs["target-a"], 0, None),
                                               "a production or harness JAR belongs to one lane"),
            "harness of the whole target": (lambda outputs, plan: scope(outputs["target-c"], 1, None),
                                            "a production or harness JAR belongs to one lane"),
            "lane without a harness": (lambda outputs, plan: outputs["target-a"].pop(3),
                                       "lane lane-b requires one production and one harness JAR"),
            "lane without a production": (lambda outputs, plan: outputs["target-c"].pop(0),
                                          "lane lane-c requires one production and one harness JAR"),
            "two productions of one lane": (lambda outputs, plan: outputs["target-a"].append(
                output("files/Example Mod - second.jar", "lane-a", "production")), "repeats the production JAR"),
            "two harnesses of one lane": (lambda outputs, plan: outputs["target-a"].append(
                output("harness/Example Mod E2E - second.jar", "lane-b", "harness")), "repeats the production JAR"),
            "two SBOMs of one target": (lambda outputs, plan: outputs["target-a"].append(
                output("targets/target-a/sbom/second.cdx.json", None, "sbom")), "repeats the production JAR"),
            "two SBOMs of one lane": (lambda outputs, plan: outputs["target-c"].extend(
                output(f"sbom/lane-c.{index}.cdx.json", "lane-c", "sbom") for index in (1, 2)),
                                      "repeats the production JAR"),
            "target without a native report": (lambda outputs, plan: outputs["target-c"].pop(),
                                               "$.targets[1].outputs: every target requires a native report"),
            "report of another target's lane": (lambda outputs, plan: scope(outputs["target-c"], -1, "lane-a"),
                                                "$.targets[1].outputs[2].lane_id: names another target's lane"),
            "jar of another target's lane": (lambda outputs, plan: scope(outputs["target-a"], 0, "lane-c"),
                                             "names another target's lane"),
            "unknown lane": (lambda outputs, plan: scope(outputs["target-a"], 4, "lane-z"), "names another target's lane"),
            "one manifest path for two targets": (lambda outputs, plan: [
                item.update(path="artifacts.json") for item in (outputs["target-a"][4], outputs["target-c"][-1])],
                                                  "case-insensitive alias"),
            "one SBOM path for two targets": (lambda outputs, plan: outputs["target-c"].append(
                output("Targets/target-a/SBOM/example.cdx.json", None, "sbom")), "case-insensitive alias"),
            "lane id of another type": (lambda outputs, plan: scope(outputs["target-a"], 4, 0), "must be a string"),
            "empty lane id": (lambda outputs, plan: scope(outputs["target-a"], 4, ""), "length must be between"),
            "output without a scope": (lambda outputs, plan: outputs["target-a"][4].pop("lane_id"), "missing required keys"),
            "unknown role": (lambda outputs, plan: outputs["target-a"][4].update(role="manifest"), "must be one of"),
        }
        for label, (mutate, message) in rejected.items():
            with self.subTest(case=label), self.assertRaises(MbError) as caught:
                validate_plan(self.staged(mutate))
            self.assertIn(message, str(caught.exception), label)

    def test_duplicate_json_and_size_limits(self):
        plan = canonical_json(ci_plan())
        for data in (plan.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1'),
                     b" " * (4 * 1024 * 1024 + 1)):
            with self.assertRaises(MbError):
                load_document(data, kind="mod-base.build.plan")

    def test_plan_unit_admission_agrees_with_attempt_artifact_transport(self):
        for kind in ("target", "runtime"):
            for unit, success in (("a", True), ("target.v1_loader-2", True), ("x" * 80, True),
                                  ("target--a", False), ("target---a", False)):
                plan = ci_plan()
                if kind == "target":
                    plan["targets"][0]["id"] = unit
                    plan["lanes"][0]["target_id"] = unit
                else:
                    plan["lanes"][0]["id"] = unit
                    for output in plan["targets"][0]["outputs"]:
                        output["lane_id"] = unit
                plan["plan_sha256"] = plan_sha256(plan)
                with self.subTest(kind=kind, unit=unit):
                    if success:
                        validate_plan(plan)
                        name = grammar.ci_artifact_name(kind, 42, 2, unit)
                        self.assertEqual(grammar.parse_ci_artifact_name(name).unit_id, unit)
                    else:
                        with self.assertRaises(MbError):
                            validate_plan(plan)
                        with self.assertRaises(MbError):
                            grammar.ci_artifact_name(kind, 42, 2, unit)


class LiveIdentityTests(unittest.TestCase):
    def test_source_dispatch_preserves_ready_pr_authentication(self):
        plan, api, _ = seeded_pr()
        authenticate_source_identity(api, plan["identity"])
        self.assertEqual(api.mutations, [])

    def test_protected_current_and_historical_subjects_keep_exact_parents(self):
        for current in (False, True):
            for parents in ([], ["a" * 40], ["a" * 40, "b" * 40]):
                plan, api, _ = seeded_pr()
                identity = protected_subject(plan, api, current=current, parents=parents)
                with self.subTest(current=current, parents=parents):
                    authenticate_source_identity(api, identity)
                    self.assertEqual(api.mutations, [])

    def test_non_pr_branch_repository_base_tree_and_parent_substitution_reject(self):
        mutations = [lambda i: i.update(head_branch="unprotected"),
                     lambda i: i.update(base_branch="unprotected"),
                     lambda i: i.update(repository="foreign/mod", source_repository="foreign/mod"),
                     lambda i: i.update(base_sha="f" * 40),
                     lambda i: i.update(tested_tree="f" * 40),
                     lambda i: i.update(tested_parents=["b" * 40]),
                     lambda i: i.update(tested_parents=[i["tested_sha"]])]
        for index, mutate in enumerate(mutations):
            plan, api, _ = seeded_pr()
            identity = protected_subject(plan, api)
            mutate(identity)
            with self.subTest(index=index), self.assertRaises(MbError):
                authenticate_source_identity(api, identity)

    def test_unreachable_inconsistent_or_missing_protected_ancestry_reject(self):
        results = [{"status": "behind", "ahead_by": 0, "behind_by": 1},
                   {"status": "diverged", "ahead_by": 1, "behind_by": 1},
                   {"status": "identical", "ahead_by": 0, "behind_by": 0},
                   {"status": "ahead", "ahead_by": 0, "behind_by": 0},
                   {"status": "ahead", "ahead_by": 1, "behind_by": 1},
                   {"status": "ahead", "ahead_by": True, "behind_by": 0}, {}]
        for result in results:
            plan, api, _ = seeded_pr()
            identity = protected_subject(plan, api)
            api.add_compare(identity["tested_sha"], identity["controller_sha"], result)
            with self.subTest(result=result), self.assertRaises(MbError):
                authenticate_source_identity(api, identity)

    def test_current_subject_requires_matching_live_tree_and_real_git_evidence(self):
        for change in ("tree", "commit", "parents"):
            plan, api, _ = seeded_pr()
            identity = protected_subject(plan, api, current=True)
            if change == "tree":
                api.add_response(f"/repos/{api.repository}/branches/master",
                                 {"name": "master", "commit": {"sha": identity["controller_sha"],
                                  "commit": {"tree": {"sha": "f" * 40}}}})
            else:
                response = {"sha": identity["tested_sha"], "tree": {"sha": identity["tested_tree"]},
                            "parents": [{"sha": "a" * 40}]}
                response["sha" if change == "commit" else "parents"] = "f" * 40
                api.add_response(f"/repos/{api.repository}/git/commits/{identity['tested_sha']}", response)
            with self.subTest(change=change), self.assertRaises(MbError):
                authenticate_source_identity(api, identity)

    def test_authenticates_inert_merge_and_live_source(self):
        plan, api, _ = seeded_pr()
        authenticate_pr_identity(api, plan["identity"])
        self.assertEqual(api.mutations, [])

    def test_same_head_readiness_base_source_and_merge_changes_reject(self):
        mutations = [lambda p: p.update(draft=True), lambda p: p.update(state="closed"),
                     lambda p: p.update(number=True), lambda p: p.update(merge_commit_sha="9" * 40),
                     lambda p: p["head"].update(sha="9" * 40), lambda p: p["base"].update(sha="9" * 40),
                     lambda p: p["head"]["repo"].update(full_name="foreign/mod")]
        for index, mutate in enumerate(mutations):
            plan, api, pr = seeded_pr()
            mutate(pr)
            api.add_response(f"/repos/{api.repository}/pulls/7", pr)
            with self.subTest(index=index), self.assertRaises(MbError):
                authenticate_pr_identity(api, plan["identity"])

    def test_reordered_parents_and_wrong_tree_reject(self):
        for parents, tree in ((["1" * 40, "2" * 40], "6" * 40), (["2" * 40, "1" * 40], "9" * 40)):
            plan, api, _ = seeded_pr()
            api.add_commit(plan["identity"]["tested_sha"], tree, parents=parents)
            with self.assertRaises(MbError):
                authenticate_pr_identity(api, plan["identity"])

    def test_controller_movement_rejects_even_with_old_pr_response(self):
        plan, api, _ = seeded_pr()
        api.set_branch("master", "9" * 40, "8" * 40)
        with self.assertRaises(MbError):
            authenticate_pr_identity(api, plan["identity"])

    def test_missing_api_evidence_is_fatal(self):
        with self.assertRaises(MbError):
            authenticate_pr_identity(FakeGitHub(repository="example/mod"), ci_plan()["identity"])


class ReadRuleTests(unittest.TestCase):
    """Immutable objects are read once per command; mutable state at its start and before its effect."""

    def test_one_admission_is_one_observation(self):
        plan, api, _ = seeded_pr()
        authenticate_pr_identity(api, plan["identity"])
        self.assertEqual(api.request_count, 4)  # repository, default head, pull request, tested commit
        for current, requests in ((True, 3), (False, 4)):  # a historical subject adds its ancestry
            plan, api, _ = seeded_pr()
            identity = protected_subject(plan, api, current=current)
            authenticate_source_identity(api, identity)
            self.assertEqual(api.request_count, requests)

    def test_git_objects_and_sha_comparisons_are_read_once_per_command(self):
        plan, api, _ = seeded_pr()
        identity = protected_subject(plan, api)
        reads = CommandReads.of(api)
        self.assertIs(CommandReads.of(reads), reads)
        self.assertEqual(reads.repository, api.repository)
        for _ in range(3):
            authenticate_source_identity(reads, identity)
        self.assertEqual(reads.request_count, 2 * 3 + 2)  # repository and head three times; commit and ancestry once
        commit = f"/repos/{api.repository}/git/commits/{identity['tested_sha']}"
        first = reads.get_json(commit)
        first["tree"]["sha"] = "f" * 40
        self.assertEqual(reads.get_json(commit)["tree"]["sha"], identity["tested_tree"])
        api.add_tree("8" * 40, [{"path": "a", "mode": "100644", "type": "blob", "sha": "c" * 40, "size": 0},
                                {"path": "d/b", "mode": "100644", "type": "blob", "sha": "c" * 40, "size": 0}])
        before = api.request_count
        tree = f"/repos/{api.repository}/git/trees/{'8' * 40}"
        self.assertEqual(len(reads.get_json(tree, params={"recursive": 1})["tree"]), 2)
        self.assertEqual(len(reads.get_json(tree)["tree"]), 1)
        self.assertEqual(len(reads.get_json(tree, params={"recursive": 1})["tree"]), 2)
        self.assertEqual(api.request_count - before, 2)  # one read per distinct tree request

    def test_branches_pull_requests_runs_and_running_jobs_are_never_remembered(self):
        plan, api, pr = seeded_pr()
        reads = CommandReads.of(api)
        for path in (f"/repos/{api.repository}", f"/repos/{api.repository}/branches/master",
                     f"/repos/{api.repository}/pulls/7"):
            before = api.request_count
            reads.get_json(path)
            reads.get_json(path)
            self.assertEqual(api.request_count - before, 2, path)
        api.add_compare("master", "1" * 40, {"status": "ahead", "ahead_by": 1, "behind_by": 0})
        before = api.request_count
        for _ in range(2):  # a comparison that names a branch can change
            reads.get_json(f"/repos/{api.repository}/compare/master...{'1' * 40}")
        self.assertEqual(api.request_count - before, 2)

        run = ci_api_run(plan, status="in_progress", conclusion=None)
        api.add_run(run)
        api.add_jobs(42, 2, ci_graph_jobs("build-full")[:3])
        self.assertEqual(reads.run(42)["status"], "in_progress")
        self.assertEqual(len(reads.attempt_jobs(42, 2)), 3)
        api.add_jobs(42, 2, ci_graph_jobs("build-full"))
        self.assertEqual(len(reads.attempt_jobs(42, 2)), 7)  # a running attempt is read every time
        api.add_run({**run, "status": "completed", "conclusion": "success"})
        before = api.request_count
        self.assertEqual(reads.run(42)["status"], "completed")
        jobs = reads.attempt_jobs(42, 2)
        jobs[0]["name"] = "changed by the caller"
        self.assertEqual(reads.attempt_jobs(42, 2)[0]["name"], GUARD)
        self.assertEqual(reads.run(42)["conclusion"], "success")
        self.assertEqual(api.request_count - before, 3)  # the run twice, the completed attempt's jobs once
        api.add_run({**run, "run_attempt": 3, "status": "queued", "conclusion": None})
        self.assertEqual(reads.run(42)["run_attempt"], 3)
        with self.assertRaises(MbError):
            reads.attempt_jobs(42, 3)  # the new attempt has no jobs yet and nothing is remembered for it

    def test_watch_reads_each_state_once_and_again_before_the_effect(self):
        plan, api, _ = seeded_pr()
        reads, watch = CommandReads.of(api), Watch()
        calls = []

        def source():
            calls.append(api.request_count)
            return authenticate_source_identity(reads, plan["identity"])

        self.assertIsNone(watch.read(("source",), source))
        self.assertIsNone(watch.read(("source",), source))
        self.assertEqual(len(calls), 1)
        self.assertEqual(api.request_count, 4)
        watch.recheck()
        self.assertEqual((len(calls), api.request_count), (2, 7))  # the tested commit is not read again
        api.set_branch("master", "f" * 40, "e" * 40)
        with self.assertRaisesRegex(MbError, "moved"):
            watch.recheck()

    def test_watch_rejects_any_state_that_differs_at_the_effect(self):
        values = iter([{"expired": False}, {"expired": False}, {"expired": True}])
        watch = Watch()
        observed = watch.read(("artifact availability", 42), lambda: next(values))
        observed["expired"] = True  # a caller cannot alter what was observed
        watch.recheck()
        with self.assertRaisesRegex(MbError, "artifact availability changed between the start of the command"):
            watch.recheck()

    def test_an_observation_made_through_another_request_is_read_again_only_before_the_effect(self):
        answers = iter([{"expired": False}, {"expired": True}])
        calls = []

        def reader():
            calls.append(1)
            return next(answers)

        watch = Watch()
        seen = {"expired": False}
        watch.observe(("artifact availability", 42), reader, seen)
        seen["expired"] = True  # the caller's row is copied, not held
        self.assertEqual(watch.read(("artifact availability", 42), reader), {"expired": False})
        watch.observe(("artifact availability", 42), reader, {"expired": True})  # the first observation stays
        self.assertEqual(calls, [])
        watch.recheck()
        self.assertEqual(calls, [1])
        with self.assertRaisesRegex(MbError, "artifact availability changed between the start of the command"):
            watch.recheck()

    def test_a_listing_row_that_the_read_by_id_contradicts_fails_the_recheck(self):
        watch = Watch()
        watch.observe(("artifact availability", 42), lambda: {"expired": True}, {"expired": False})
        with self.assertRaisesRegex(MbError, "artifact availability changed"):
            watch.recheck()

    def test_a_watch_after_another_starts_only_from_what_that_one_rechecked(self):
        reads = {"source": 0, "run": 0, "late": 0}

        def reader(name):
            def read():
                reads[name] += 1
                return name
            return read

        self.assertEqual(Watch.after(None).read(("source",), reader("source")), "source")
        earlier = Watch()
        earlier.read(("source",), reader("source"))
        earlier.read(("producer run", 42), reader("run"))
        self.assertEqual(reads, {"source": 2, "run": 1, "late": 0})
        unchecked = Watch.after(earlier)  # nothing is lent before the earlier watch rechecked
        unchecked.read(("source",), reader("source"))
        self.assertEqual(reads["source"], 3)
        earlier.recheck()
        earlier.read(("late",), reader("late"))  # read after the recheck: never confirmed, never lent
        self.assertEqual(reads, {"source": 4, "run": 2, "late": 1})
        later = Watch.after(earlier)
        self.assertEqual(later.read(("source",), reader("source")), "source")
        self.assertEqual(later.read(("late",), reader("late")), "late")
        self.assertEqual(reads, {"source": 4, "run": 2, "late": 2})
        later.recheck()  # only what this watch read is read again, the lent source included
        self.assertEqual(reads, {"source": 5, "run": 2, "late": 3})

    def test_a_run_is_recorded_under_the_head_github_gives_it(self):
        plan, api, _ = seeded_pr()
        self.assertEqual(run_head(plan["identity"]), ("1" * 40, "feature/example", "example/mod"))
        self.assertNotEqual(run_head(plan["identity"])[0], plan["identity"]["controller_sha"])
        current = protected_subject(plan, api, current=True)
        self.assertEqual(run_head(current), ("2" * 40, "master", "example/mod"))
        self.assertEqual(current["tested_sha"], current["controller_sha"])
        plan, api, _ = seeded_pr()
        historical = protected_subject(plan, api)
        with self.assertRaisesRegex(MbError, "tests the commit it runs from"):
            run_head(historical)
        with self.assertRaises(MbError):
            run_head({**plan["identity"], "extra": True})


#: SHA-256 of every literal Jobs API listing under ``tests/fixtures/ci_graphs``.
LISTINGS = {
    "build-attest-only": "b5ec2ee50ae7c44887a43b88466d4943ee7d814140af84dfa0ee8eeef666ff5c",
    "build-deferred": "df7def9a1fb7c8d4b1924ffb06fc1a9aa34a6b2783e1ae82816786d3088e435c",
    "build-full": "7dee5b301dc3316f894b90cabe844bf123c5991607064cf7cb5048851c894971",
    "build-full-duplicated-job": "f9a6d6a0e40a00ea90f330d8bcc701dc5b426a103c57225e01dbd399a073065c",
    "build-full-extra-job": "3c43cc8044f6f428206c08109f616f8905872c0f14a6d8a74adfdd1e2cbab416",
    "build-full-missing-job": "ebef5775894d3f55a071c891f2fa37ca71e3d22b2bb034050b677ee9965dcd45",
    "build-full-wrong-conclusion": "36f6db125550ce293bcfe213df0e1dc3403a5c63bddb1e7fedca1ec487219358",
    "build-reuse": "244c58fadfd7faa56055425dae24f1756e62fabfddbf078e881ba4d826f157b2",
    "packaged-deferred": "90df640f1ed838ca9de18aad9c09bebc5770a53fa5c382af3e15e1b0ddf4b324",
    "packaged-pull-request": "40ca12fe70270a59c3ffcb47ef7420830c467dd86b8c1715e05b6d1e93b3881d",
    "packaged-rebuilt": "fd71941186f0ad8ca15edcc37044b65b4b58b88a8f077a272e2a1d2899f90b5a",
    "packaged-reuse": "a2cff652aa712107b90d21e7954bcf7e04e8ee49eb2d66f9204a9c956690effd",
    "packaged-selected": "4a0d02c83479da94d1ab5fd16bd4a831a8ef6434846230b310bf25fc26a86697",
}
#: ``(producer, mode)`` -> the ``graph_sha256`` a producer record carries for the one-target,
#: one-lane fixture plan. The listing ``<producer>-<mode>`` is that graph as the API reports it.
GRAPHS = {
    ("build", "full"): "e774fe528ecc0d05cd2dd854fb41baa1c789c9207320a9d813a74b52bac6e90d",
    ("build", "deferred"): "e4b30d42c69728546aa1e42b7884ce1b902d9c741ce8c03a0822b19608a34cd0",
    ("build", "reuse"): "81570e1ac55cea0832cc0ce00b4afce07ad4de2677a8d902b30071840c90c222",
    ("packaged", "pull-request"): "32769be59551162877c1639c9b0e2f29b71cde76f57fdb2c5cbffd5a9ac56ffd",
    ("packaged", "deferred"): "bf01fb0a8f2a3d6e528e592bd7d4f0cd5009f18e68ac323a86f2d04f92bebfba",
    ("packaged", "selected"): "6f8dec36c641e95c76bd7f515429159c7127dae98d100718a90b56a5da5ff0c1",
    ("packaged", "rebuilt"): "3ba2c136386786829a24f6b6e75ee4a1640942b47be2d8c937a8f0d26a8f320f",
    ("packaged", "reuse"): "b83d405546e2e9f0f383b6a0a5b18a58987dd3521c7e7fe3271106bc2f32c493",
}
GUARD = "Verify pinned mod-base / Authenticate the pinned kit"
BUILD_FULL = ["Shared Build / Plan protected Build", "Shared Build / Verify protected policy",
              "Shared Build / Compile target target-a", "Shared Build / Seal complete Build bundle",
              "Shared Build / Verify complete Build"]
PACKAGED_FULL = ["Shared Packaged E2E / Authenticate exact Build", "Shared Packaged E2E / Run packaged lane lane-a",
                 "Shared Packaged E2E / Seal complete packaged results",
                 "Shared Packaged E2E / Verify complete packaged E2E"]
SELECT = "Select exact Build / Select exact Build source"
#: ``(producer, mode)`` -> the exact jobs of the fixture plan, spelled out: ``(succeeded, skipped)``.
EXPECTED = {
    ("build", "full"): ([GUARD, *BUILD_FULL], ["Build deferred for draft"]),
    ("build", "deferred"): ([GUARD, "Build deferred for draft"], ["Shared Build"]),
    ("build", "reuse"): ([GUARD, "Shared Build / Plan protected Build", "Shared Build / Verify complete Build"],
                         ["Build deferred for draft", "Shared Build / Verify protected policy",
                          "Shared Build / Compile target ${{ matrix.id }}",
                          "Shared Build / Seal complete Build bundle"]),
    ("packaged", "pull-request"): ([GUARD, *PACKAGED_FULL],
                                   ["Packaged E2E deferred for draft", "Select exact Build", "Shared Build"]),
    ("packaged", "deferred"): ([GUARD, "Packaged E2E deferred for draft"],
                               ["Select exact Build", "Shared Build", "Shared Packaged E2E"]),
    ("packaged", "selected"): ([GUARD, SELECT, *PACKAGED_FULL], ["Packaged E2E deferred for draft", "Shared Build"]),
    ("packaged", "rebuilt"): ([GUARD, SELECT, *BUILD_FULL, *PACKAGED_FULL], ["Packaged E2E deferred for draft"]),
    ("packaged", "reuse"): ([GUARD, SELECT, "Shared Packaged E2E / Verify complete packaged E2E"],
                            ["Packaged E2E deferred for draft", "Shared Build",
                             "Shared Packaged E2E / Authenticate exact Build",
                             "Shared Packaged E2E / Run packaged lane ${{ matrix.id }}",
                             "Shared Packaged E2E / Seal complete packaged results"]),
}
SEALED = {
    ("build", "full"): BUILD_FULL[2:], ("build", "deferred"): [], ("build", "reuse"): BUILD_FULL[4:],
    ("packaged", "pull-request"): PACKAGED_FULL[1:], ("packaged", "deferred"): [],
    ("packaged", "selected"): PACKAGED_FULL[1:], ("packaged", "rebuilt"): BUILD_FULL[2:] + PACKAGED_FULL[1:],
    ("packaged", "reuse"): PACKAGED_FULL[3:],
}


def listing(producer, mode):
    return ci_graph_jobs(f"{producer}-{mode}")


class GraphContractTests(unittest.TestCase):
    """The graph of every producer and mode against literal API job listings, never against itself."""

    def test_literal_listings_are_pinned_byte_for_byte(self):
        files = {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in CI_GRAPH_FIXTURES.glob("*.json")}
        self.assertEqual(files, LISTINGS)

    def test_modes_are_a_closed_set_with_one_literal_listing_each(self):
        self.assertEqual(BUILD_MODES, ("full", "deferred", "reuse"))
        self.assertEqual(PACKAGED_MODES, ("pull-request", "deferred", "selected", "rebuilt", "reuse"))
        self.assertEqual(set(GRAPHS), {("build", mode) for mode in BUILD_MODES}
                         | {("packaged", mode) for mode in PACKAGED_MODES})
        self.assertEqual(len(set(GRAPHS.values())), len(GRAPHS))
        for producer, mode in (("build", "pull-request"), ("packaged", "full"), ("build", "attest-only"),
                               ("status", "full"), ("build", None), (None, "full")):
            with self.subTest(producer=producer, mode=mode), self.assertRaises(MbError):
                run_graph(producer, mode)
        self.assertEqual((BuildGraphV1().mode, PackagedGraphV1().mode), ("full", "pull-request"))

    def test_expected_jobs_and_sealing_jobs_are_spelled_out(self):
        plan = ci_plan()
        for (producer, mode), (succeeded, skipped) in EXPECTED.items():
            graph = run_graph(producer, mode)
            expected = sorted([{"name": name, "conclusion": "success"} for name in succeeded]
                              + [{"name": name, "conclusion": "skipped"} for name in skipped],
                              key=lambda entry: entry["name"])
            with self.subTest(producer=producer, mode=mode):
                self.assertEqual(graph.jobs(plan), expected)
                self.assertEqual(graph.sealed_jobs(plan), SEALED[producer, mode])
                self.assertEqual(graph.sha256(plan), GRAPHS[producer, mode])

    def test_every_mode_admits_its_literal_listing_and_no_other(self):
        plan = ci_plan()
        for producer, mode in GRAPHS:
            jobs = listing(producer, mode)
            self.assertEqual(require_graph(jobs, plan=plan, producer=producer, mode=mode, run_attempt=2),
                             GRAPHS[producer, mode])
            self.assertEqual(require_graph(list(reversed(jobs)), plan=plan, producer=producer, mode=mode,
                                           run_attempt=2), GRAPHS[producer, mode])
            for other_producer, other_mode in GRAPHS:
                if (other_producer, other_mode) != (producer, mode):
                    with self.subTest(listing=(producer, mode), graph=(other_producer, other_mode)), \
                            self.assertRaisesRegex(MbError, "exact job graph mismatch"):
                        require_graph(jobs, plan=plan, producer=other_producer, mode=other_mode, run_attempt=2)

    def test_listings_use_every_time_shape_the_api_writes(self):
        shapes = set()
        for name in LISTINGS:
            for job in ci_graph_jobs(name):
                for step in job["steps"]:
                    value = step["started_at"]
                    shapes.add("offset" if value.endswith("-08:00") else "fraction" if "." in value else "z")
        self.assertEqual(shapes, {"fraction", "offset"})
        self.assertEqual(sealed_upload(ci_graph_jobs("build-full")[4]),
                         ("2026-10-07T10:01:00Z", "2026-10-07T10:02:00Z"))
        self.assertEqual(sealed_upload(ci_graph_jobs("packaged-pull-request")[5]),
                         ("2026-10-07T10:08:00Z", "2026-10-07T10:09:00Z"))

    def test_attest_only_extra_missing_duplicated_and_wrongly_concluded_listings_fit_no_mode(self):
        plan = ci_plan()
        for name, message in (("build-attest-only", "exact job graph mismatch"),
                              ("build-full-extra-job", "unexpected=\\['Publish advisory summary'\\]"),
                              ("build-full-missing-job", "missing=\\['Shared Build / Verify protected policy'\\]"),
                              ("build-full-duplicated-job", "repeats a job name"),
                              ("build-full-wrong-conclusion",
                               "different conclusion=\\['Shared Build / Verify protected policy'\\]")):
            jobs = ci_graph_jobs(name)
            for producer, mode in GRAPHS:
                with self.subTest(listing=name, producer=producer, mode=mode), self.assertRaises(MbError):
                    require_graph(jobs, plan=plan, producer=producer, mode=mode, run_attempt=2)
            with self.subTest(listing=name), self.assertRaisesRegex(MbError, message):
                require_graph(jobs, plan=plan, producer="build", mode="full", run_attempt=2)
        # The attest-only shape without its mod-owned job: a green run in which nothing was built.
        workers_skipped = [job for job in ci_graph_jobs("build-attest-only")
                           if job["name"] != "Attest exact tested build tree"]
        self.assertEqual({job["name"]: job["conclusion"] for job in workers_skipped},
                         {GUARD: "success", "Build deferred for draft": "skipped", "Shared Build": "skipped"})
        for mode in BUILD_MODES:
            with self.subTest(mode=mode), self.assertRaisesRegex(MbError, "exact job graph mismatch"):
                require_graph(workers_skipped, plan=plan, producer="build", mode=mode, run_attempt=2)

    def test_lookalike_pending_and_failed_jobs_reject(self):
        mutations = [lambda j: j[2].update(name="Lookalike / " + j[2]["name"]),
                     lambda j: j[2].update(name=j[2]["name"].split(" / ")[1]),
                     lambda j: j[2].update(conclusion="failure"), lambda j: j[2].update(conclusion="cancelled"),
                     lambda j: j[2].update(status="in_progress", conclusion=None),
                     lambda j: j[1].update(conclusion="success"), lambda j: j[1].update(name="Shared Build")]
        for index, mutate in enumerate(mutations):
            jobs = listing("build", "full")
            mutate(jobs)
            with self.subTest(index=index), self.assertRaises(MbError):
                require_graph(jobs, plan=ci_plan(), producer="build", mode="full", run_attempt=2)

    def test_matrix_jobs_follow_the_plan_and_collapse_only_when_skipped(self):
        plan = ci_plan()
        target, lane = copy.deepcopy(plan["targets"][0]), copy.deepcopy(plan["lanes"][0])
        target["id"] = "target-b"
        for output in target["outputs"]:
            output.update(path=output["path"].replace("lane-a", "lane-b"), lane_id="lane-b")
        lane.update(id="lane-b", target_id="target-b")
        plan["targets"].append(target)
        plan["lanes"].append(lane)
        plan["plan_sha256"] = plan_sha256(plan)
        names = [entry["name"] for entry in BuildGraphV1().jobs(plan)]
        self.assertEqual([name for name in names if "Compile target" in name],
                         ["Shared Build / Compile target target-a", "Shared Build / Compile target target-b"])
        names = [entry["name"] for entry in PackagedGraphV1("rebuilt").jobs(plan)]
        self.assertEqual(sum("Compile target" in name for name in names), 2)
        self.assertEqual(sum("Run packaged lane" in name for name in names), 2)
        for graph in (BuildGraphV1("reuse"), PackagedGraphV1("reuse"), BuildGraphV1("deferred")):
            self.assertEqual(graph.sha256(plan), graph.sha256(ci_plan()))
        self.assertNotEqual(BuildGraphV1().sha256(plan), GRAPHS["build", "full"])
        with self.assertRaisesRegex(MbError, "missing=\\['Shared Build / Compile target target-b'\\]"):
            require_graph(listing("build", "full"), plan=plan, producer="build", mode="full", run_attempt=2)

    def test_upload_requires_one_completed_successful_prior_seal_inside_its_job(self):
        mutations = [lambda s: s.pop(2), lambda s: s.append(copy.deepcopy(s[2])),
                     lambda s: s[2].update(conclusion="skipped"), lambda s: s[3].update(conclusion="failure"),
                     lambda s: s[3].update(started_at="2026-10-07T10:00:59.999Z"),
                     lambda s: s[2].update(started_at="2026-10-07T10:00:54Z"),
                     lambda s: s[3].update(completed_at="2026-10-07T10:02:06Z"),
                     lambda s: s[3].update(started_at="2026-10-07 10:01:00"),
                     lambda s: s[2].update(completed_at="2026-10-07T10:00:57Z")]
        for index, mutate in enumerate(mutations):
            jobs = listing("build", "full")
            target = next(job for job in jobs if job["name"] == "Shared Build / Compile target target-a")
            self.assertEqual([step["name"] for step in target["steps"]][2:4], [CI_SEAL_STEP, CI_UPLOAD_STEP])
            mutate(target["steps"])
            with self.subTest(index=index), self.assertRaises(MbError):
                require_graph(jobs, plan=ci_plan(), producer="build", mode="full", run_attempt=2)

    def test_attempt_listing_is_read_once_and_must_belong_to_the_exact_attempt(self):
        plan = ci_plan()
        api = FakeGitHub(repository="example/mod")
        api.add_run(ci_api_run(plan))
        api.add_jobs(42, 2, listing("build", "full"))
        self.assertEqual(authenticate_graph(api, plan=plan, producer="build", mode="full", run_id=42,
                                            run_attempt=2), GRAPHS["build", "full"])
        # The listing, and the attempt's record: when the attempt started.
        self.assertEqual(api.request_count, 2)
        for run_id, attempt, changes in ((43, 2, {}), (42, 3, {}), (42, 2, {"run_attempt": 1}),
                                         (42, 2, {"run_id": 43})):
            api = FakeGitHub(repository="example/mod")
            jobs = listing("build", "full")
            jobs[0].update(changes)
            api.add_jobs(run_id, attempt, jobs)
            with self.subTest(run_id=run_id, attempt=attempt, changes=changes), self.assertRaises(MbError):
                authenticate_graph(api, plan=plan, producer="build", mode="full", run_id=run_id, run_attempt=attempt)
        for arguments in ({"run_id": True}, {"run_attempt": 0}, {"mode": "pull-request"}, {"producer": "pages"}):
            with self.subTest(arguments=arguments), self.assertRaises(MbError):
                authenticate_graph(api, **{"plan": plan, "producer": "build", "mode": "full", "run_id": 42,
                                           "run_attempt": 2, **arguments})


class FailedJobsRerunTests(unittest.TestCase):
    """What GitHub lists for an attempt after a rerun of failed jobs only (K7 canary, run
    38032224931 attempt 2): the jobs it did not run again appear under the new attempt, with new
    job ids and ``run_attempt`` of that attempt, but with the times and runner of attempt 1. Only
    the attempt's own ``run_started_at`` tells them apart."""

    PLAN = BUILD_FULL[0]

    def world(self, jobs=None):
        plan = ci_plan()
        api = FakeGitHub(repository="example/mod")
        jobs = listing("build", "full") if jobs is None else jobs
        return plan, api, ci_failed_jobs_rerun(api, ci_api_run(plan), jobs)

    def test_the_listing_of_the_canary_is_refused_by_the_times_of_its_carried_jobs(self):
        plan, api, run = self.world()
        listed = github_jobs.attempt_jobs(api, 42, 2)
        # Nothing in the listing names attempt 1: the check of v1.1.1 let this attempt through.
        self.assertEqual({job["run_attempt"] for job in listed}, {2})
        earlier = {job["name"]: job for job in github_jobs.attempt_jobs(api, 42, 1)}
        self.assertEqual(set(earlier), {GUARD, "Build deferred for draft", self.PLAN})
        for job in listed:
            if job["name"] in earlier:
                self.assertNotEqual(job["id"], earlier[job["name"]]["id"])
                self.assertEqual((job["started_at"], job["completed_at"], job["runner_id"]),
                                 tuple(earlier[job["name"]][key] for key in ("started_at", "completed_at", "runner_id")))
        self.assertEqual(require_graph(listed, plan=plan, producer="build", mode="full", run_attempt=2),
                         GRAPHS["build", "full"])
        refused = rf"job '{GUARD}' started before attempt 2 did, in an earlier attempt: {RERUN}"
        reads = CommandReads.of(api)
        self.assertEqual(reads.run(42)["run_started_at"], run["run_started_at"])
        before = api.request_count
        with self.assertRaisesRegex(MbError, refused):
            reads.attempt_jobs(42, 2)
        self.assertEqual(api.request_count - before, 1)  # the run's record said when attempt 2 started
        before = api.request_count
        with self.assertRaisesRegex(MbError, refused):
            authenticate_graph(api, plan=plan, producer="build", mode="full", run_id=42, run_attempt=2)
        self.assertEqual(api.request_count - before, 2)  # alone, it reads the attempt's own record

    def test_a_carried_plan_job_alone_is_refused(self):
        jobs = listing("build", "full")
        guard = next(job for job in jobs if job["name"] == GUARD)
        guard.update(started_at="2026-10-07T10:00:52Z", completed_at="2026-10-07T10:00:54Z")
        for step in guard["steps"]:
            step.update(started_at="2026-10-07T10:00:53.000Z", completed_at="2026-10-07T10:00:53.000Z")
        _, api, _ = self.world(jobs)
        with self.assertRaisesRegex(MbError, rf"job '{self.PLAN}' started before attempt 2 did, in an earlier "
                                             rf"attempt: {RERUN}"):
            CommandReads.of(api).attempt_jobs(42, 2)

    def test_a_rerun_of_all_jobs_is_the_attempts_own_work(self):
        plan, api = ci_plan(), FakeGitHub(repository="example/mod")
        first = ci_api_run(plan, run_attempt=1, run_started_at="2026-10-07T09:00:00Z", conclusion="failure",
                           previous_attempt_url=None)
        second = ci_api_run(plan)  # every job of the fixture started after 10:00:00Z
        third = ci_api_run(plan, run_attempt=3, run_started_at="2026-10-07T11:00:00Z")
        api.add_run(third, attempts=[first, second])
        api.add_jobs(42, 2, listing("build", "full"))
        rerun = listing("build", "full")
        for job in rerun:
            job.update(id=job["id"] + 1_000_000, run_attempt=3, started_at="2026-10-07T11:00:00Z",
                       completed_at="2026-10-07T11:00:30Z")
        api.add_jobs(42, 3, rerun)
        reads = CommandReads.of(api)
        self.assertEqual(reads.run(42)["run_attempt"], 3)
        before = api.request_count
        self.assertEqual(len(reads.attempt_jobs(42, 3)), 7)  # started in the second attempt 3 did
        self.assertEqual(api.request_count - before, 1)
        self.assertEqual(len(reads.attempt_jobs(42, 2)), 7)  # an earlier attempt's own record says when it began
        self.assertEqual(api.request_count - before, 3)
        self.assertEqual(len(reads.attempt_jobs(42, 2)), 7)  # nothing is read twice but the listing
        self.assertEqual(api.request_count - before, 4)

    #: The canary's own listings (K7, The-Plum-Team/mod-base-canary, evidence rows "R1-C | C2",
    #: "R1-C | C3" and "R1-C | C4"), read after every attempt had completed: ``run_started_at`` as
    #: ``/runs/{id}/attempts/{n}`` served it for each attempt, earlier ones included, and every job's
    #: name, conclusion and ``started_at`` in the order of ``/attempts/{n}/jobs``. Attempts 2 are the
    #: reruns of failed jobs only, attempts 3 the reruns of all jobs; all of them ran v1.1.1.
    CANARY_BUILD = {  # Build run 38032224931
        1: ("2026-10-10T06:48:44Z", [
            (GUARD, "success", "2026-10-10T06:48:47Z"),
            ("Shared Build / Plan protected Build", "success", "2026-10-10T06:48:55Z"),
            ("Build deferred for draft", "skipped", "2026-10-10T06:48:53Z"),
            ("Shared Build / Compile target 1.21.1", "cancelled", "2026-10-10T06:51:02Z"),
            ("Shared Build / Compile target 1.20.1", "cancelled", "2026-10-10T06:51:01Z"),
            ("Shared Build / Verify protected policy", "cancelled", "2026-10-10T06:51:01Z"),
            ("Shared Build / Seal complete Build bundle", "cancelled", "2026-10-10T06:51:31Z"),
            ("Shared Build / Verify complete Build", "cancelled", "2026-10-10T06:51:32Z")]),
        2: ("2026-10-10T06:55:06Z", [
            ("Shared Build / Compile target 1.21.1", "success", "2026-10-10T06:55:10Z"),
            ("Shared Build / Compile target 1.20.1", "success", "2026-10-10T06:55:10Z"),
            ("Shared Build / Verify protected policy", "success", "2026-10-10T06:55:10Z"),
            (GUARD, "success", "2026-10-10T06:48:47Z"),
            ("Shared Build / Plan protected Build", "success", "2026-10-10T06:48:55Z"),
            ("Build deferred for draft", "skipped", "2026-10-10T06:55:17Z"),
            ("Shared Build / Seal complete Build bundle", "success", "2026-10-10T06:58:31Z"),
            ("Shared Build / Verify complete Build", "success", "2026-10-10T07:00:59Z")]),
        3: ("2026-10-10T07:07:36Z", [
            (GUARD, "success", "2026-10-10T07:07:40Z"),
            ("Shared Build / Plan protected Build", "success", "2026-10-10T07:07:50Z"),
            ("Build deferred for draft", "skipped", "2026-10-10T07:07:48Z"),
            ("Shared Build / Compile target 1.21.1", "success", "2026-10-10T07:10:37Z"),
            ("Shared Build / Verify protected policy", "success", "2026-10-10T07:10:37Z"),
            ("Shared Build / Compile target 1.20.1", "success", "2026-10-10T07:10:37Z"),
            ("Shared Build / Seal complete Build bundle", "success", "2026-10-10T07:12:38Z"),
            ("Shared Build / Verify complete Build", "success", "2026-10-10T07:14:45Z")]),
    }
    CANARY_PACKAGED = {  # packaged run 38032224927
        2: ("2026-10-10T07:18:00Z", [
            ("Shared Packaged E2E / Authenticate exact Build", "success", "2026-10-10T07:18:05Z"),
            ("Packaged E2E deferred for draft", "skipped", "2026-10-10T07:18:03Z"),
            ("Select exact Build", "skipped", "2026-10-10T07:18:03Z"),
            (GUARD, "success", "2026-10-10T06:48:47Z"),
            ("Shared Build", "skipped", "2026-10-10T07:18:11Z"),
            ("Shared Packaged E2E / Run packaged lane forge-1.20.1", "success", "2026-10-10T07:19:54Z"),
            ("Shared Packaged E2E / Run packaged lane fabric-1.21.1", "success", "2026-10-10T07:19:54Z"),
            ("Shared Packaged E2E / Run packaged lane fabric-1.20.1", "success", "2026-10-10T07:19:55Z"),
            ("Shared Packaged E2E / Seal complete packaged results", "success", "2026-10-10T07:23:08Z"),
            ("Shared Packaged E2E / Verify complete packaged E2E", "success", "2026-10-10T07:25:51Z")]),
        3: ("2026-10-10T07:29:41Z", [
            (GUARD, "success", "2026-10-10T07:29:44Z"),
            ("Packaged E2E deferred for draft", "skipped", "2026-10-10T07:29:49Z"),
            ("Shared Packaged E2E / Authenticate exact Build", "success", "2026-10-10T07:29:51Z"),
            ("Shared Build", "skipped", "2026-10-10T07:29:49Z"),
            ("Select exact Build", "skipped", "2026-10-10T07:29:50Z"),
            ("Shared Packaged E2E / Run packaged lane fabric-1.20.1", "success", "2026-10-10T07:32:19Z"),
            ("Shared Packaged E2E / Run packaged lane fabric-1.21.1", "success", "2026-10-10T07:32:19Z"),
            ("Shared Packaged E2E / Run packaged lane forge-1.20.1", "success", "2026-10-10T07:32:19Z"),
            ("Shared Packaged E2E / Seal complete packaged results", "success", "2026-10-10T07:35:47Z"),
            ("Shared Packaged E2E / Verify complete packaged E2E", "success", "2026-10-10T07:37:49Z")]),
    }

    @staticmethod
    def canary_jobs(run_attempt, rows):
        return [{"id": 114_150_000_000 + 100 * run_attempt + index, "run_id": 42, "run_attempt": run_attempt,
                 "name": name, "status": "completed", "conclusion": conclusion, "started_at": started}
                for index, (name, conclusion, started) in enumerate(rows)]

    def test_the_canary_reruns_of_all_jobs_pass_and_its_failed_jobs_reruns_do_not(self):
        refused = rf"job '{GUARD}' started before attempt 2 did, in an earlier attempt: {RERUN}"
        for name, attempts in (("packaged", self.CANARY_PACKAGED), ("build", self.CANARY_BUILD)):
            for attempt, (started, rows) in attempts.items():
                with self.subTest(run=name, attempt=attempt):
                    jobs = self.canary_jobs(attempt, rows)
                    if attempt == 2:
                        with self.assertRaisesRegex(MbError, refused):
                            require_ran_in_attempt(jobs, run_attempt=attempt, run_started_at=started)
                    else:
                        require_ran_in_attempt(jobs, run_attempt=attempt, run_started_at=started)
        # The Build run as a command reads it once attempt 3 is its latest: attempt 3 from the run's
        # record, earlier attempts from their own records, each with its own start.
        plan, api = ci_plan(), FakeGitHub(repository="example/mod")
        records = {attempt: ci_api_run(plan, run_attempt=attempt, run_started_at=started,
                                       created_at="2026-10-10T06:48:44Z", updated_at="2026-10-10T07:17:13Z",
                                       conclusion="cancelled" if attempt == 1 else "success")
                   for attempt, (started, _) in self.CANARY_BUILD.items()}
        records[1]["previous_attempt_url"] = None
        api.add_run(records[3], attempts=[records[1], records[2]])
        for attempt, (_, rows) in self.CANARY_BUILD.items():
            api.add_jobs(42, attempt, self.canary_jobs(attempt, rows))
        reads = CommandReads.of(api)
        self.assertEqual(reads.run(42)["run_attempt"], 3)
        self.assertEqual(len(reads.attempt_jobs(42, 3)), 8)
        with self.assertRaisesRegex(MbError, refused):
            reads.attempt_jobs(42, 2)
        self.assertEqual(reads.attempt_started(42, 2), "2026-10-10T06:55:06Z")
        self.assertEqual(len(reads.attempt_jobs(42, 1)), 8)

    def test_times_are_whole_seconds_and_skipped_or_unstarted_jobs_are_not_judged_by_time(self):
        def job(started, conclusion="success"):
            return {"name": "a job", "status": "completed", "conclusion": conclusion, "started_at": started}

        attempt = "2026-10-07T10:00:05Z"
        for started in ("2026-10-07T10:00:05Z", "2026-10-07T10:00:05.000Z", "2026-10-07T10:00:05.999Z",
                        "2026-10-07T12:00:05+02:00", "2026-10-07T10:00:06Z", None):
            with self.subTest(started=started):
                require_ran_in_attempt([job(started)], run_attempt=1, run_started_at=attempt)
        for started in ("2026-10-07T10:00:04Z", "2026-10-07T10:00:04.999Z", "2026-10-07T12:00:04+02:00",
                        "2026-10-07T09:59:59.999999999Z"):
            with self.subTest(started=started), self.assertRaisesRegex(MbError, "started before attempt 1 did"):
                require_ran_in_attempt([job(started)], run_attempt=1, run_started_at=attempt)
        # GitHub's placeholder times of a skipped job and a queued job without a start say nothing.
        require_ran_in_attempt([job("2026-10-07T09:00:00Z", "skipped"),
                                {"name": "queued", "status": "queued", "conclusion": None, "started_at": None}],
                               run_attempt=2, run_started_at=attempt)
        for started in ("2026-10-07 10:00:06", 0, ""):
            with self.subTest(started=started), self.assertRaisesRegex(MbError, "job started_at"):
                require_ran_in_attempt([job(started)], run_attempt=2, run_started_at=attempt)
        for value in (None, "", "2026-10-07", 1_700_000_000):
            with self.subTest(run_started_at=value), self.assertRaisesRegex(MbError, "run_started_at of attempt 2"):
                require_ran_in_attempt([], run_attempt=2, run_started_at=value)

    def test_an_attempt_without_a_start_is_refused(self):
        plan, api = ci_plan(), FakeGitHub(repository="example/mod")
        run = ci_api_run(plan)
        del run["run_started_at"]
        api.add_run(run)
        api.add_jobs(42, 2, listing("build", "full"))
        with self.assertRaisesRegex(MbError, "run_started_at of attempt 2"):
            CommandReads.of(api).attempt_jobs(42, 2)


class RunningGraphTests(unittest.TestCase):
    """What an in-run reader may rely on while the rest of its attempt has not happened yet."""

    FINISHED = BUILD_FULL[:3]

    def running(self):
        jobs = [job for job in listing("build", "full") if job["name"] != "Shared Build / Verify complete Build"]
        assemble = next(job for job in jobs if job["name"] == "Shared Build / Seal complete Build bundle")
        assemble.update(status="in_progress", conclusion=None, completed_at=None)
        assemble["steps"] = assemble["steps"][:1]
        return jobs

    def check(self, jobs, **changes):
        arguments = {"plan": ci_plan(), "producer": "build", "mode": "full", "run_attempt": 2,
                     "finished": self.FINISHED, **changes}
        return require_partial_graph(jobs, **arguments)

    def test_finished_prerequisites_are_enough_while_the_reader_and_gate_are_open(self):
        self.assertIsNone(self.check(self.running()))
        self.assertIsNone(self.check(listing("build", "full")))
        rebuilt = [job for job in listing("packaged", "rebuilt") if "Shared Packaged E2E" not in job["name"]]
        self.assertIsNone(self.check(rebuilt, producer="packaged", mode="rebuilt"))

    def test_unenrolled_duplicate_unfinished_failed_and_unsealed_jobs_reject(self):
        mutations = [lambda j: j.append({**copy.deepcopy(j[0]), "name": "foreign job"}),
                     lambda j: j.append(copy.deepcopy(j[4])),
                     lambda j: j[4].update(status="in_progress", conclusion=None),
                     lambda j: j[4].update(conclusion="skipped"), lambda j: j[3].update(conclusion="failure"),
                     lambda j: j[2].update(conclusion="cancelled"), lambda j: j.pop(3),
                     lambda j: j[4]["steps"].pop(3),
                     lambda j: j[4]["steps"][2].update(completed_at="2026-10-07T10:01:30Z"),
                     lambda j: j[4].update(run_attempt=1)]
        for index, mutate in enumerate(mutations):
            jobs = self.running()
            self.assertEqual(jobs[4]["name"], "Shared Build / Compile target target-a")
            mutate(jobs)
            with self.subTest(index=index), self.assertRaises(MbError):
                self.check(jobs)
        for changes in ({"finished": ["Shared Build / Compile target target-z"]}, {"mode": "deferred"},
                        {"producer": "packaged", "mode": "pull-request"}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                self.check(self.running(), **changes)


class ProducerNameTests(unittest.TestCase):
    def test_every_artifact_kind_names_the_job_that_uploads_it(self):
        table = [(("build", "target", "target-a"), "Shared Build / Compile target target-a"),
                 (("build", "build", None), "Shared Build / Seal complete Build bundle"),
                 (("build", "tested", "build"), "Shared Build / Verify complete Build"),
                 (("build", "reuse", None), "Shared Build / Verify complete Build"),
                 (("packaged", "target", "target-a"), "Shared Build / Compile target target-a"),
                 (("packaged", "build", None), "Shared Build / Seal complete Build bundle"),
                 (("packaged", "tested", "build"), "Shared Build / Verify complete Build"),
                 (("packaged", "runtime", "lane-a"), "Shared Packaged E2E / Run packaged lane lane-a"),
                 (("packaged", "results", None), "Shared Packaged E2E / Seal complete packaged results"),
                 (("packaged", "tested", "packaged"), "Shared Packaged E2E / Verify complete packaged E2E"),
                 (("packaged", "reuse", None), "Shared Packaged E2E / Verify complete packaged E2E")]
        for arguments, expected in table:
            with self.subTest(arguments=arguments):
                self.assertEqual(upload_job_name(*arguments), expected)
        for arguments in (("build", "runtime", "lane-a"), ("build", "results", None), ("build", "tested", "packaged"),
                          ("build", "tested", "other"), ("build", "handoff", None), ("pages", "build", None),
                          ("build", "target", None), ("build", "target", "A")):
            with self.subTest(arguments=arguments), self.assertRaises(MbError):
                upload_job_name(*arguments)
        self.assertEqual(job_name("build", "build", "plan"), "Shared Build / Plan protected Build")
        self.assertEqual(job_name("packaged", "build", "policy"), "Shared Build / Verify protected policy")
        self.assertEqual(job_name("packaged", "select-build", "select"), SELECT)
        self.assertEqual(job_name("packaged", "packaged-e2e", "lane", "lane-a"), PACKAGED_FULL[1])
        for arguments in (("build", "packaged-e2e", "gate"), ("build", "build", "plan", "target-a"),
                          ("packaged", "packaged-e2e", "lane")):
            with self.subTest(arguments=arguments), self.assertRaises(MbError):
                job_name(*arguments)

    def test_a_gate_record_settles_its_mode_from_its_digest_and_subject(self):
        plan = ci_plan()
        self.assertEqual(gate_mode("build", "build", plan, GRAPHS["build", "full"]), "full")
        self.assertEqual(gate_mode("packaged", "packaged", plan, GRAPHS["packaged", "pull-request"]), "pull-request")
        standalone, api, _ = seeded_pr()
        protected_subject(standalone, api, current=True)
        self.assertEqual(gate_mode("build", "build", standalone, GRAPHS["build", "full"]), "full")
        for mode in ("selected", "rebuilt"):
            self.assertEqual(gate_mode("packaged", "packaged", standalone, GRAPHS["packaged", mode]), mode)
        self.assertEqual(gate_mode("packaged", "build", standalone, GRAPHS["packaged", "rebuilt"]), "rebuilt")
        refused = [("build", "build", plan, GRAPHS["build", "deferred"]),
                   ("build", "build", plan, GRAPHS["build", "reuse"]),
                   ("build", "packaged", plan, GRAPHS["build", "full"]),
                   ("packaged", "packaged", plan, GRAPHS["packaged", "selected"]),
                   ("packaged", "packaged", plan, GRAPHS["packaged", "rebuilt"]),
                   ("packaged", "build", plan, GRAPHS["packaged", "rebuilt"]),
                   ("packaged", "packaged", standalone, GRAPHS["packaged", "pull-request"]),
                   ("packaged", "packaged", standalone, GRAPHS["packaged", "reuse"]),
                   ("packaged", "build", standalone, GRAPHS["packaged", "selected"]),
                   ("packaged", "packaged", standalone, "f" * 64)]
        for index, arguments in enumerate(refused):
            with self.subTest(index=index), self.assertRaisesRegex(MbError, "admissible gate mode"):
                gate_mode(*arguments)

    def test_gate_prerequisites_stop_at_the_gate_own_call(self):
        plan = ci_plan()
        self.assertEqual(BuildGraphV1().prerequisites(plan, BUILD_FULL[4]), [GUARD, *BUILD_FULL[:4]])
        self.assertEqual(PackagedGraphV1().prerequisites(plan, PACKAGED_FULL[3]), [GUARD, *PACKAGED_FULL[:3]])
        rebuilt = PackagedGraphV1("rebuilt")
        self.assertEqual(rebuilt.prerequisites(plan, BUILD_FULL[4]), [GUARD, SELECT, *BUILD_FULL[:4]])
        self.assertEqual(rebuilt.prerequisites(plan, PACKAGED_FULL[3]),
                         [GUARD, SELECT, *BUILD_FULL, *PACKAGED_FULL[:3]])
        for graph, name in ((BuildGraphV1(), BUILD_FULL[3]), (BuildGraphV1("deferred"), BUILD_FULL[4]),
                            (PackagedGraphV1(), BUILD_FULL[4])):
            with self.assertRaises(MbError):
                graph.prerequisites(plan, name)


class ReferencedWorkflowTests(unittest.TestCase):
    """A producer run names its controller commit and kit pin only in ``referenced_workflows``."""

    def check(self, run, producer="build", mode="full", plan=None):
        identity = (plan or ci_plan())["identity"]
        return authenticate_referenced_workflows(referenced_workflows(run), identity=identity,
                                                 producer=producer, mode=mode)

    def test_real_pull_request_target_shape_binds_controller_and_pin(self):
        plan = ci_plan()
        run = ci_api_run(plan)
        self.assertEqual((run["head_sha"], run["head_branch"]), ("1" * 40, "feature/example"))
        self.assertNotIn(plan["identity"]["controller_sha"], (run["head_sha"], run["display_title"]))
        self.assertEqual(run["referenced_workflows"], [
            {"path": "example/mod/.github/workflows/mod-base-guard.yml@" + "2" * 40, "sha": "2" * 40,
             "ref": "refs/heads/master"},
            {"path": "The-Plum-Team/mod-base/.github/workflows/build.yml@" + "3" * 40, "sha": "3" * 40}])
        self.assertIsNone(self.check(run))
        packaged = ci_api_run(plan, "packaged")
        self.assertEqual(len(packaged["referenced_workflows"]), 4)
        for mode in PACKAGED_MODES:
            with self.subTest(mode=mode):
                self.assertIsNone(self.check(packaged, "packaged", mode))

    def test_listing_order_is_free_and_a_skipped_call_may_be_listed_or_not(self):
        plan = ci_plan()
        run = ci_api_run(plan, "packaged")
        run["referenced_workflows"].reverse()
        self.assertIsNone(self.check(run, "packaged", "pull-request"))
        guard, select, build, packaged = ci_api_run(plan, "packaged")["referenced_workflows"]
        # Whether GitHub lists a kit workflow whose calling job is skipped is settled by the canary.
        for mode, entries in (("pull-request", [guard, packaged]), ("deferred", [guard]),
                              ("selected", [guard, select, packaged]), ("reuse", [guard, select, packaged]),
                              ("rebuilt", [guard, select, build, packaged])):
            with self.subTest(mode=mode):
                self.assertIsNone(self.check({"referenced_workflows": entries}, "packaged", mode))
        for mode, entries in (("pull-request", [guard, select, build]), ("selected", [guard, packaged]),
                              ("rebuilt", [guard, select, packaged]), ("reuse", [guard, packaged]),
                              ("deferred", [select, build, packaged])):
            with self.subTest(mode=mode), self.assertRaisesRegex(MbError, "does not list"):
                self.check({"referenced_workflows": entries}, "packaged", mode)
        deferred = ci_api_run(plan)
        deferred["referenced_workflows"].pop()
        self.assertIsNone(self.check(deferred, mode="deferred"))
        with self.assertRaisesRegex(MbError, "does not list"):
            self.check(deferred, mode="full")

    def test_another_controller_commit_or_kit_pin_rejects(self):
        for index, sha, message in ((0, "9" * 40, "admitted controller commit"), (1, "9" * 40, "pinned kit")):
            run = ci_api_run(ci_plan())
            entry = run["referenced_workflows"][index]
            entry.update(path=entry["path"].rsplit("@", 1)[0] + "@" + sha, sha=sha)
            with self.subTest(index=index), self.assertRaisesRegex(MbError, message):
                self.check(run)

    def test_foreign_repeated_unpinned_and_malformed_entries_reject(self):
        kit = "The-Plum-Team/mod-base/.github/workflows/"
        mutations = [
            lambda r: r.append({"path": "example/mod/.github/workflows/other.yml@" + "2" * 40, "sha": "2" * 40}),
            lambda r: r.append({"path": kit + "publish.yml@" + "3" * 40, "sha": "3" * 40}),
            lambda r: r.append({"path": kit + "packaged-e2e.yml@" + "3" * 40, "sha": "3" * 40}),
            lambda r: r.append({"path": "the-plum-team/mod-base/.github/workflows/build.yml@" + "3" * 40,
                                "sha": "3" * 40}),
            lambda r: r.append({"path": "fork/mod/.github/workflows/mod-base-guard.yml@" + "2" * 40,
                                "sha": "2" * 40}),
            lambda r: r.append(copy.deepcopy(r[1])),
            lambda r: r[0].update(path="example/mod/.github/workflows/mod-base-guard.yml@refs/heads/master"),
            lambda r: r[1].update(path=kit + "build.yml@v1.1.0"),
            lambda r: r[1].update(sha="4" * 40), lambda r: r[1].update(sha=None), lambda r: r[1].pop("path"),
            lambda r: r[0].update(ref=7), lambda r: r.append("build.yml"), lambda r: r.pop(0), lambda r: r.clear(),
        ]
        for index, mutate in enumerate(mutations):
            run = ci_api_run(ci_plan())
            mutate(run["referenced_workflows"])
            with self.subTest(index=index), self.assertRaises(MbError):
                self.check(run)
        for value in (None, {}, "guard", [{}] * 101):
            with self.subTest(value=value), self.assertRaises(MbError):
                self.check({**ci_api_run(ci_plan()), "referenced_workflows": value})
        with self.assertRaises(MbError):
            referenced_workflows(None)

if __name__ == "__main__":
    unittest.main()
