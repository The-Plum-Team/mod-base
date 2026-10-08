"""The results index kind and the gate receipt writer: pure structural rules."""

from __future__ import annotations

import copy
import unittest

from mod_base.build_ci.records import bind_results_index, gate_receipt, validate_gate_receipt, validate_results_index
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_json
from mod_base.model.documents import load_document
from tests.helpers import ci_plan, ci_push_plan, ci_run_descriptor, ci_run_gate, ci_run_results, ci_staged_plan

MODES = (("pull-request", ci_plan), ("selected", ci_push_plan), ("rebuilt", ci_push_plan))


def rename(descriptor: dict, **producer) -> None:
    """Move a descriptor to another run or attempt, keeping its artifact name bound to it."""

    descriptor["producer"].update(producer)
    parts = descriptor["artifact"]["name"].split("--")
    parts[1:3] = [str(descriptor["producer"]["run_id"]), f"a{descriptor['producer']['run_attempt']}"]
    descriptor["artifact"]["name"] = "--".join(parts)


class ResultsIndexTests(unittest.TestCase):
    def test_the_index_of_every_packaged_mode_is_valid_for_its_plan(self) -> None:
        for mode, plan in MODES:
            with self.subTest(mode=mode):
                document = ci_run_results(plan(), mode)
                self.assertIs(validate_results_index(document, plan=plan()), document)
                self.assertEqual(load_document(canonical_json(document), kind="mod-base.ci.results", plan=plan()),
                                 document)
                self.assertEqual(document["owning_build"]["producer"]["run_id"], 43 if mode == "rebuilt" else 42)

    def rejected(self, message: str, change, *, mode: str = "pull-request", plan=ci_staged_plan) -> None:
        document = ci_run_results(plan(), mode)
        change(document)
        with self.assertRaisesRegex(MbError, message):
            validate_results_index(document, plan=plan())

    def test_the_closed_shape_and_the_binding_to_the_plan(self) -> None:
        self.rejected("has unknown keys", lambda index: index.update(success=True))
        self.rejected("has unknown keys", lambda index: index["lanes"][0].update(files=[]))
        self.rejected("is missing required keys", lambda index: index["lanes"][0].pop("validation_sha256"))
        self.rejected("does not equal the complete admitted binding", lambda index: index.update(plan_sha256="0" * 64))
        self.rejected("does not equal the complete admitted binding",
                      lambda index: index["lanes"][1]["descriptor"].update(profile="quick-skin"))
        self.rejected(r"\$\.lanes", lambda index: index.update(lanes=[]))

    def test_only_a_packaged_attempt_seals_it_over_its_own_lanes(self) -> None:
        build = {key: value for key, value in ci_run_descriptor(ci_staged_plan(), "build", "full", "build")["producer"]
                 .items() if key != "upload_window"}
        self.rejected("only the packaged caller seals a results index", lambda index: index.update(producer=build))
        self.rejected("mixed producer or attempt", lambda index: rename(index["lanes"][1]["descriptor"], run_attempt=1))
        self.rejected("must name the runtime artifact of this lane", lambda index: index["lanes"].__setitem__(
            slice(0, 2), [{**index["lanes"][0], "descriptor": index["lanes"][1]["descriptor"]},
                          {**index["lanes"][1], "descriptor": index["lanes"][0]["descriptor"]}]))
        self.rejected("repeats an artifact id",
                      lambda index: index["lanes"][2]["descriptor"]["artifact"].update(id=101))
        self.rejected("repeats an artifact id",
                      lambda index: index["lanes"][0]["descriptor"]["artifact"].update(id=100))

    def test_every_planned_lane_and_no_other_in_plan_order(self) -> None:
        coverage = "does not list every planned lane and no other, in plan order"
        self.rejected(coverage, lambda index: index["lanes"].pop())
        self.rejected(coverage, lambda index: index["lanes"].reverse())
        self.rejected(coverage, lambda index: index["lanes"][0].update(native_contract_sha256="0" * 64))
        extra = ci_run_results(ci_staged_plan(), "pull-request")
        extra["lanes"].append({**extra["lanes"][0], "id": "lane-d", "descriptor": ci_run_descriptor(
            ci_staged_plan(), "packaged", "pull-request", "runtime", unit_id="lane-d", artifact_id=150)})
        with self.assertRaisesRegex(MbError, coverage):
            validate_results_index(extra, plan=ci_staged_plan())
        self.assertIs(validate_results_index(extra), extra)  # without a plan only the structure is checked

    def test_the_owning_build_is_the_rebuilt_one_or_the_one_of_a_separate_build_run(self) -> None:
        def lane_as_owner(index) -> None:
            index["owning_build"] = copy.deepcopy(index["lanes"][0]["descriptor"])

        self.rejected("must name a complete Build bundle", lane_as_owner)
        self.rejected("a consumed Build comes from a separate Build run",
                      lambda index: rename(index["owning_build"], run_id=43), mode="selected", plan=ci_push_plan)
        self.rejected("a rebuilt Build is sealed by the packaged run itself",
                      lambda index: rename(index["owning_build"], run_attempt=1), mode="rebuilt", plan=ci_push_plan)
        # A pull request never rebuilds inside its packaged run.
        rebuilt = ci_run_descriptor(ci_staged_plan(), "packaged", "pull-request", "build")
        self.rejected("a pull request's Build artifacts come from its Build caller",
                      lambda index: index.update(owning_build=rebuilt))

    def test_the_index_is_bound_to_the_results_artifact_it_was_read_from(self) -> None:
        plan = ci_staged_plan()
        document = ci_run_results(plan, "pull-request")

        def results(**artifact) -> dict:
            descriptor = ci_run_descriptor(plan, "packaged", "pull-request", "results", artifact_id=150)
            descriptor["producer"]["upload_window"] = {"started_at": "2026-10-07T10:03:00Z",
                                                       "completed_at": "2026-10-07T10:04:00Z"}
            descriptor["artifact"].update(created_at="2026-10-07T10:03:30Z", **artifact)
            return descriptor

        self.assertIs(bind_results_index(document, descriptor=results(), plan=plan), document)
        cases = {
            "wrong selected record kind": ci_run_descriptor(plan, "packaged", "pull-request", "runtime", unit_id="lane-a"),
            "record artifact collides with its source input": results(id=101),
            "source upload cannot postdate the selected record upload":
                ci_run_descriptor(plan, "packaged", "pull-request", "results", artifact_id=150),
        }
        for message, descriptor in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(MbError, message):
                bind_results_index(document, descriptor=descriptor, plan=plan)
        other = results()
        rename(other, run_attempt=3)
        with self.assertRaisesRegex(MbError, "record differs from selected writer identity"):
            bind_results_index(document, descriptor=other, plan=plan)
        with self.assertRaisesRegex(MbError, "does not equal the complete admitted binding"):
            bind_results_index(document, descriptor=results(), plan=ci_plan())


class GateReceiptWriterTests(unittest.TestCase):
    def write(self, document: dict, plan: dict, **changes) -> dict:
        arguments = {key: document[key] for key in ("producer", "gate", "mode", "artifacts", "owning_build",
                                                    "native_receipts")}
        arguments.update(changes)
        return gate_receipt(plan=plan, **arguments)

    def test_the_writer_builds_the_receipts_the_reader_accepts(self) -> None:
        for gate, mode, plan in (("build", "full", ci_plan), ("build", "full", ci_push_plan),
                                 ("build", "rebuilt", ci_push_plan), ("packaged", "pull-request", ci_staged_plan),
                                 ("packaged", "selected", ci_push_plan), ("packaged", "rebuilt", ci_push_plan)):
            with self.subTest(gate=gate, mode=mode):
                expected = ci_run_gate(plan(), gate, mode)
                written = self.write(expected, plan())
                self.assertEqual(written, expected)
                self.assertIs(validate_gate_receipt(written, plan=plan()), written)

    def test_the_receipt_is_independent_of_its_arguments(self) -> None:
        plan, source = ci_plan(), ci_run_gate(ci_plan(), "build", "full")
        written = self.write(source, plan)
        source["artifacts"][0]["artifact"]["id"] = 7
        plan["identity"]["pr_number"] = 9
        self.assertEqual(written, ci_run_gate(ci_plan(), "build", "full"))

    def test_it_refuses_what_the_reader_refuses(self) -> None:
        plan, document = ci_staged_plan(), ci_run_gate(ci_staged_plan(), "packaged", "pull-request")
        for label, changes in (("a mode of another subject", dict(mode="selected")),
                               ("no owning Build", dict(owning_build=None)),
                               ("a Build gate over lanes", dict(gate="build")),
                               ("receipts out of plan order", dict(native_receipts=document["native_receipts"][::-1])),
                               ("a lane short", dict(artifacts=document["artifacts"][1:])),
                               ("a producer with a window", dict(producer={**document["producer"], "upload_window": {}}))):
            with self.subTest(case=label), self.assertRaises(MbError):
                self.write(document, plan, **changes)
        with self.assertRaises(MbError):
            self.write(document, ci_plan())


if __name__ == "__main__":
    unittest.main()
