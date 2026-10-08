"""Planning: from a subject, the protected config and hook bytes to the complete protected plan."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mod_base import cli
from mod_base.build_ci import identity, planning
from mod_base.build_ci.config import load_build_config
from mod_base.build_ci.protocol import plan_sha256, subject_of, validate_plan
from mod_base.errors import MbError
from mod_base.model import limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from tests import ci_mod_harness as h
from tests.test_ci_adapter import derived, encoded

INVENTORY = b"hello\n"
SCENARIOS = b'{"scenarios": ["smoke"]}\n'


class BuildPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_build_config(h.MOD, repository=h.REPOSITORY)
        self.subject = h.subject()

    def build(self, **changes) -> dict:
        arguments = {"subject": self.subject, "config": self.config, "inventory": INVENTORY,
                     "scenario_contract": SCENARIOS, "derived": encoded(derived()), **changes}
        return planning.build_plan(**arguments)

    def test_the_plan_is_complete_valid_and_bound_to_the_actual_bytes(self) -> None:
        plan = self.build()
        self.assertIs(validate_plan(plan), plan)
        self.assertEqual(load_document(canonical_json(plan), kind="mod-base.build.plan"), plan)
        self.assertEqual((plan["kind"], plan["schema_version"], plan["build_adapter_api"], plan["profile"]),
                         ("mod-base.build.plan", 1, 1, "quick-skin"))
        self.assertEqual({"targets": plan["targets"], "lanes": plan["lanes"]}, derived())
        self.assertEqual(subject_of(plan["identity"]), self.subject)
        self.assertEqual({key: value for key, value in plan["identity"].items() if key not in self.subject}, {
            "policy_sha256": identity.policy_sha256(self.config, self.subject),
            # `printf 'hello\n' | git hash-object --stdin`
            "inventory_blob": "ce013625030ba8dba906f756967f9e9ca394464a",
            "inventory_sha256": hashlib.sha256(INVENTORY).hexdigest(),
            "scenario_sha256": hashlib.sha256(SCENARIOS).hexdigest(),
            "runtime_selection_sha256": canonical_sha256({"profile": "quick-skin", "lanes": derived()["lanes"]}),
        })
        self.assertEqual(plan["plan_sha256"], plan_sha256(plan))

    def test_the_same_inputs_give_the_same_plan_and_stay_untouched(self) -> None:
        subject, document = copy.deepcopy(self.subject), encoded(derived())
        first = self.build(derived=document)
        first["identity"]["kit"]["sha"] = "9" * 40
        first["targets"][0]["id"] = "changed"
        self.assertEqual(self.subject, subject)
        second = self.build(derived=json.dumps(derived(), indent=3).encode("utf-8"))
        self.assertEqual(second, self.build())
        self.assertNotEqual(second, first)

    def test_every_input_is_bound_by_the_plan_hash(self) -> None:
        baseline = self.build()
        other_lanes = derived()
        other_lanes["lanes"][0]["obligations"].append("scenario/example/server/second")
        moved = copy.deepcopy(self.subject)
        moved.update(head_sha="9" * 40, tested_parents=[h.CONTROLLER_SHA, "9" * 40])
        with tempfile.TemporaryDirectory() as directory:
            root = h.materialize(Path(directory) / "mod")
            path = root / "scripts/ci/mod-base-build.json"
            path.write_bytes(h.pretty({**json.loads(path.read_bytes()), "profile": "block-pops"}))
            changed = [
                self.build(inventory=INVENTORY + b" "),
                self.build(scenario_contract=SCENARIOS + b" "),
                self.build(derived=encoded(other_lanes)),
                self.build(subject=moved),
                self.build(config=load_build_config(root, repository=h.REPOSITORY)),
            ]
        hashes = {plan["plan_sha256"] for plan in (baseline, *changed)}
        self.assertEqual(len(hashes), 6)
        self.assertEqual(changed[0]["identity"]["scenario_sha256"], baseline["identity"]["scenario_sha256"])
        self.assertNotEqual(changed[0]["identity"]["inventory_blob"], baseline["identity"]["inventory_blob"])
        self.assertNotEqual(changed[2]["identity"]["runtime_selection_sha256"],
                            baseline["identity"]["runtime_selection_sha256"])
        self.assertNotEqual(changed[4]["identity"]["policy_sha256"], baseline["identity"]["policy_sha256"])
        self.assertNotEqual(changed[4]["identity"]["runtime_selection_sha256"],
                            baseline["identity"]["runtime_selection_sha256"])
        self.assertEqual(changed[3]["identity"]["policy_sha256"], baseline["identity"]["policy_sha256"])

    def test_inputs_outside_the_contract_are_rejected(self) -> None:
        complete = self.build()["identity"]
        cases = {
            "text inventory": {"inventory": "hello\n"},
            "empty inventory": {"inventory": b""},
            "oversized inventory": {"inventory": bytes(limits.MAX_CI_PLAN_SOURCE_BYTES + 1)},
            "missing scenario contract": {"scenario_contract": None},
            "oversized scenario contract": {"scenario_contract": bytes(limits.MAX_CI_PLAN_SOURCE_BYTES + 1)},
            "complete identity as subject": {"subject": complete},
            "foreign subject": {"subject": {**self.subject, "repository": "example/other",
                                            "source_repository": "example/other"}},
            "config document": {"config": self.config.data},
            "derived with identity": {"derived": encoded({**derived(), "identity": complete})},
            "derived text": {"derived": json.dumps(derived())},
            "derived not json": {"derived": b"plan"},
        }
        for label, change in cases.items():
            with self.subTest(case=label), self.assertRaises(MbError):
                self.build(**change)
        self.build(inventory=bytes(limits.MAX_CI_PLAN_SOURCE_BYTES))

    def test_a_plan_beyond_the_plan_byte_cap_is_rejected(self) -> None:
        # One target of many lanes whose obligations fill the derived document up to its own cap:
        # the hook output still parses, but the plan it would become no longer fits.
        def obligation(lane: int, step: int) -> str:
            return f"lane-{lane:02d}/{step:04d}/" + "x" * 187  # 200 characters, 203 bytes as a list item

        document = derived()
        lane, outputs = document["lanes"][0], document["targets"][0]["outputs"]
        document["lanes"] = [{**lane, "id": f"lane-{index:02d}", "obligations": [obligation(index, 0)]}
                             for index in range(24)]
        document["targets"][0]["outputs"] = [
            {**output, "lane_id": entry["id"], "path": f"{entry['id']}/{output['role']}.bin"}
            for entry in document["lanes"] for output in outputs]
        missing = (limits.MAX_CI_PLAN_BYTES - 512 - len(canonical_json(document))) // 203
        for index, entry in enumerate(document["lanes"]):
            count = min(999, missing)
            entry["obligations"] += [obligation(index, step) for step in range(1, count + 1)]
            missing -= count
        self.assertEqual(missing, 0)
        data = canonical_json(document)
        self.assertLessEqual(len(data), limits.MAX_CI_PLAN_BYTES)
        self.assertGreater(len(data), limits.MAX_CI_PLAN_BYTES - 1024)
        with self.assertRaises(MbError) as caught:
            self.build(derived=data)
        self.assertIn("plan byte cap", str(caught.exception))


class RequirePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.subject = h.subject()
        self.plan = planning.build_plan(subject=self.subject, config=load_build_config(h.MOD, repository=h.REPOSITORY),
                                        inventory=INVENTORY, scenario_contract=SCENARIOS, derived=encoded(derived()))

    def test_the_plan_of_this_subject_with_the_agreed_hash_is_returned(self) -> None:
        self.assertIs(planning.require_plan(self.plan, subject=self.subject), self.plan)
        self.assertIs(planning.require_plan(self.plan, subject=self.subject, expected_sha256=self.plan["plan_sha256"]),
                      self.plan)

    def test_another_hash_is_a_mismatch_never_a_choice(self) -> None:
        with self.assertRaises(planning.PlanError) as caught:
            planning.require_plan(self.plan, subject=self.subject, expected_sha256="0" * 64)
        self.assertEqual((caught.exception.reason, caught.exception.exit_code), ("plan-mismatch", 2))
        for expected in ("", "0" * 63, self.plan["plan_sha256"].upper(), 7, ["0" * 64]):
            with self.subTest(expected=expected), self.assertRaises(MbError):
                planning.require_plan(self.plan, subject=self.subject, expected_sha256=expected)

    def test_another_subject_or_a_tampered_plan_is_rejected(self) -> None:
        other = copy.deepcopy(self.subject)
        other["head_branch"] = "feature/other"
        with self.assertRaises(MbError):
            planning.require_plan(self.plan, subject=other)
        with self.assertRaises(MbError):
            planning.require_plan(self.plan, subject=self.plan["identity"])
        tampered = copy.deepcopy(self.plan)
        tampered["lanes"][0]["obligations"].append("scenario/extra")
        with self.assertRaises(MbError):
            planning.require_plan(tampered, subject=self.subject, expected_sha256=self.plan["plan_sha256"])


class MatrixTests(unittest.TestCase):
    def test_matrices_and_outputs_follow_plan_order(self) -> None:
        document = derived()
        target, lane = document["targets"][0], document["lanes"][0]
        document["targets"].insert(0, {**copy.deepcopy(target), "id": "target-z", "outputs": [
            {**output, "lane_id": "lane-z", "path": "z/" + output["path"]} for output in target["outputs"]]})
        document["lanes"].append({**copy.deepcopy(lane), "id": "lane-z", "target_id": "target-z"})
        plan = planning.build_plan(subject=h.subject(), config=load_build_config(h.MOD, repository=h.REPOSITORY),
                                   inventory=INVENTORY, scenario_contract=SCENARIOS, derived=encoded(document))
        self.assertEqual(planning.matrices(plan), {"targets": ["target-z", "target-a"], "lanes": ["lane-a", "lane-z"]})
        outputs = planning.plan_outputs(plan)
        self.assertEqual(outputs, {"plan_sha256": plan["plan_sha256"], "targets": '["target-z","target-a"]',
                                   "lanes": '["lane-a","lane-z"]'})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output"
            cli.write_github_output(path, outputs)
            lines = dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())
        self.assertEqual({name: json.loads(lines[name]) for name in ("targets", "lanes")}, planning.matrices(plan))

    def test_an_invalid_plan_has_no_matrices(self) -> None:
        with self.assertRaises(MbError):
            planning.matrices({"targets": [{"id": "a"}], "lanes": []})
        with self.assertRaises(MbError):
            planning.plan_outputs({"plan_sha256": "0" * 64, "targets": [], "lanes": []})


if __name__ == "__main__":
    unittest.main()
