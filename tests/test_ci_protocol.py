"""Pure protocol, exact graph and adversarial read-only API evidence tests."""

from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from mod_base.build_ci.authenticate import authenticate_pr_identity, authenticate_source_identity
from mod_base.build_ci.graph import BuildGraphV1, PackagedGraphV1, authenticate_graph
from mod_base.build_ci.protocol import plan_sha256, validate_plan
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.model.documents import load_document
from mod_base.model import grammar
from mod_base.model.canonical import canonical_json
from mod_base.workflow import CI_SEAL_STEP, CI_UPLOAD_STEP
from tests.helpers import ci_plan


def seeded_pr():
    plan = ci_plan()
    identity = plan["identity"]
    api = FakeGitHub(repository=identity["repository"], default_branch="master")
    api.set_branch("master", identity["base_sha"], "8" * 40)
    api.add_commit(identity["tested_sha"], identity["tested_tree"], parents=identity["tested_parents"])
    pr = {"number": 7, "state": "open", "draft": False, "merge_commit_sha": identity["tested_sha"]}
    for side in ("head", "base"):
        pr[side] = {"sha": identity[f"{side}_sha"], "ref": identity[f"{side}_branch"],
                    "repo": {"full_name": api.repository}}
    api.add_response(f"/repos/{api.repository}/pulls/7", pr)
    return plan, api, pr


def seeded_graph(producer="build"):
    plan = ci_plan()
    graph = BuildGraphV1() if producer == "build" else PackagedGraphV1()
    api = FakeGitHub(repository="example/mod")
    jobs = [{**entry, "status": "completed", "steps": []} for entry in graph.jobs(plan)]
    for job in jobs:
        if job["name"] in graph.sealed_jobs(plan):
            job["steps"] = [
                {"name": CI_SEAL_STEP, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-07T10:00:00Z", "completed_at": "2026-10-07T10:01:00Z"},
                {"name": CI_UPLOAD_STEP, "status": "completed", "conclusion": "success",
                 "started_at": "2026-10-07T10:01:00Z", "completed_at": "2026-10-07T10:02:00Z"},
            ]
    api.add_jobs(42, 2, jobs)
    return plan, api, jobs


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

    def test_protected_controller_movement_during_ancestry_read_rejects(self):
        plan, api, _ = seeded_pr()
        identity = protected_subject(plan, api)
        original = api.get_json
        def reading(path, **kwargs):
            value = original(path, **kwargs)
            if "/compare/" in path:
                api.set_branch("master", "f" * 40, "e" * 40)
            return value
        with patch.object(api, "get_json", side_effect=reading), self.assertRaises(MbError):
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


class GraphTests(unittest.TestCase):
    def test_exact_full_graphs_and_order_independence(self):
        for producer in ("build", "packaged"):
            plan, api, jobs = seeded_graph(producer)
            before = authenticate_graph(api, plan=plan, producer=producer, run_id=42, run_attempt=2)
            api.add_jobs(42, 2, list(reversed(jobs)))
            self.assertEqual(before, authenticate_graph(api, plan=plan, producer=producer, run_id=42, run_attempt=2))

    def test_missing_extra_duplicate_skipped_failed_and_mixed_attempt_jobs(self):
        mutations = [lambda j: j.pop(), lambda j: j.append(copy.deepcopy(j[0])),
                     lambda j: j[0].update(name="Lookalike / " + j[0]["name"]),
                     lambda j: j[0].update(conclusion="failure"), lambda j: j[0].update(conclusion="skipped"),
                     lambda j: j[0].update(status="in_progress"), lambda j: j[0].update(run_attempt=1),
                     lambda j: j[0].update(run_id=43)]
        for producer in ("build", "packaged"):
            for index, mutate in enumerate(mutations):
                plan, api, jobs = seeded_graph(producer)
                mutate(jobs)
                api.add_jobs(42, 2, jobs)
                with self.subTest(producer=producer, index=index), self.assertRaises(MbError):
                    authenticate_graph(api, plan=plan, producer=producer, run_id=42, run_attempt=2)

    def test_upload_requires_completed_successful_unique_prior_seal(self):
        mutations = [lambda s: s.pop(0), lambda s: s.append(copy.deepcopy(s[0])),
                     lambda s: s[0].update(conclusion="skipped"),
                     lambda s: s[1].update(started_at="2026-10-07T10:00:30Z")]
        for index, mutate in enumerate(mutations):
            plan, api, jobs = seeded_graph()
            job = next(job for job in jobs if job["steps"])
            mutate(job["steps"])
            api.add_jobs(42, 2, jobs)
            with self.subTest(index=index), self.assertRaises(MbError):
                authenticate_graph(api, plan=plan, producer="build", run_id=42, run_attempt=2)


if __name__ == "__main__":
    unittest.main()
