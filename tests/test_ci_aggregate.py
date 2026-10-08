"""``ci aggregate`` end to end: the aggregating job of a running packaged attempt indexes the real
lane results of that attempt, and the gate and the readers accept what it wrote."""

from __future__ import annotations

import copy
import json
import unittest

from mod_base.build_ci import identity
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from mod_base.model.documents import load_document
from tests.ci_attempt import expand, lane_archive, record_zip, runtime_input_sha256, sealed, validation
from tests.helpers import ci_graph_jobs, h
from tests.test_ci_describe import RERUN
from tests.test_ci_gate import AGGREGATE, LANE, LISTINGS, PACKAGED_GATE, GateCase, seed_build
from tests.test_ci_transport import after_download

BUILD_ENVELOPE = h("the selected Build's envelope")


class AggregateCase(GateCase):
    def world(self, *, mode: str = "pull-request", change=None, select: bool = True, upload=None, **options):
        """A packaged run whose aggregating job is sealing: every lane has uploaded its sealed
        results with their validation record, and the state holds the selection record.

        ``change(lane_id, document, files, envelope)`` edits a lane's validation record before it
        is sealed; ``upload(lane_id)`` names the lane whose results are uploaded in its place."""

        attempt = self.attempt(listing=LISTINGS[mode], caller="packaged", push=mode != "pull-request", **options)
        attempt.sealing(AGGREGATE)
        if mode == "rebuilt":
            seed_build(attempt, mode)
            attempt.publish("tested", "build", record_zip(grammar.CI_GATE_NAME, b"{}\n"), artifact_id=200)
            attempt.owning, attempt.build_sha256 = attempt.descriptor(mode, 100), attempt.validation["input_sha256"]
        else:
            attempt.owning = attempt.add_build_run(b"the complete Build of the other run")
            attempt.build_sha256 = BUILD_ENVELOPE
        attempt.mode, attempt.expected = mode, []
        for index, lane in enumerate(attempt.plan["lanes"]):
            source = lane["id"] if upload is None else upload(lane["id"])
            data, envelope = lane_archive(attempt.plan, attempt.producer(mode), attempt.owning, source)
            document, files = validation(attempt.plan, hook="verify_runtime", unit_id=source, run_id=attempt.run_id,
                                         input_sha256=runtime_input_sha256(attempt.plan, attempt.build_sha256, envelope))
            if change is not None:
                data = change(lane["id"], document, files, data) or data
            attempt.publish("runtime", lane["id"], sealed(data, document, files) if document else data,
                            artifact_id=300 + index)
            attempt.expected.append({
                "id": lane["id"], "native_contract_sha256": lane["native_contract_sha256"],
                "descriptor": attempt.descriptor(mode, 300 + index), "envelope_sha256": canonical_sha256(envelope),
                "validation_sha256": canonical_sha256(document), "report_sha256": document["reports"][0]["sha256"]
                if document else None})
        if select:
            attempt.select(mode, attempt.owning, attempt.build_sha256)
        return attempt

    def aggregate(self, attempt):
        """Run ``ci aggregate``; return ``(code, stdout or stderr, index bytes or None)``."""

        output = attempt.directory / "index"
        code, stdout, stderr = attempt.command("aggregate", "--output", str(output))
        if code:
            self.assertEqual(stdout, "")
            self.assertFalse(output.exists())
            self.assertEqual([path for path in attempt.state.iterdir() if path.is_dir()], [])
            return code, stderr, None
        self.assertEqual(stderr, "")
        self.assertEqual([path.name for path in output.iterdir()], [grammar.CI_RESULTS_NAME])
        return code, stdout, (output / grammar.CI_RESULTS_NAME).read_bytes()

    def assert_refused(self, attempt, message: str, reason: str = "invalid-document") -> None:
        code, stderr, _ = self.aggregate(attempt)
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith(f"mod_base: {reason}: "), stderr)
        self.assertIn(message, stderr)


class AggregateCommandTests(AggregateCase):
    def test_the_index_lists_every_lane_with_the_hashes_of_what_it_read(self) -> None:
        attempt = self.world(targets=2, lanes=2)
        code, stdout, raw = self.aggregate(attempt)
        self.assertEqual((code, stdout), (0, "aggregate: 4 lane results of run 43 attempt 2 indexed as "
                                             "ci-results.json\n"))
        index = load_document(raw, kind="mod-base.ci.results", plan=attempt.plan)
        self.assertEqual(raw, canonical_json(index))
        self.assertEqual(index["lanes"], attempt.expected)
        self.assertEqual([lane["id"] for lane in index["lanes"]], [lane["id"] for lane in attempt.plan["lanes"]])
        self.assertEqual((index["producer"], index["owning_build"], index["build_envelope_sha256"]),
                         (attempt.producer("pull-request"), attempt.owning, BUILD_ENVELOPE))
        self.assertEqual(attempt.budgets, [limits.MAX_CI_AGGREGATE_REQUESTS])
        self.assertEqual(attempt.api.request_count, 13 + 2 * 4)
        self.assertEqual(attempt.api.mutations, [])

    def test_thirty_four_lanes_cost_two_requests_each_and_thirteen(self) -> None:
        attempt = self.world(targets=17, lanes=2)
        self.assertEqual(self.aggregate(attempt)[0], 0)
        # A download is two requests, so reading every lane's records cannot stay below 60.
        self.assertEqual(attempt.api.request_count, 13 + 2 * 34)

    def test_the_gate_and_the_readers_accept_the_index_of_every_mode(self) -> None:
        for mode in ("pull-request", "selected", "rebuilt"):
            with self.subTest(mode=mode):
                attempt = self.world(mode=mode, lanes=2)
                code, _, raw = self.aggregate(attempt)
                self.assertEqual(code, 0)
                attempt.jobs = expand(ci_graph_jobs(LISTINGS[mode]), attempt.plan)
                attempt.sealing(PACKAGED_GATE)
                attempt.publish("results", None, record_zip(grammar.CI_RESULTS_NAME, raw), artifact_id=400)
                code, _, receipt = self.seal(attempt, "packaged")
                self.assertEqual(code, 0)
                document = self.assert_readers_accept(attempt, "packaged", mode, receipt, artifact_id=201)
                self.assertEqual(document["owning_build"], attempt.owning)
                self.assertEqual(document["native_receipts"], [
                    {key: lane[key] if key != "unit_id" else lane["id"]
                     for key in ("unit_id", "native_contract_sha256", "report_sha256")} for lane in attempt.expected])

    def test_every_planned_lane_and_no_other_must_have_uploaded_in_this_attempt(self) -> None:
        cases = {
            "expired lane": (lambda attempt: attempt.set_artifact(301, expired=True),
                             "artifact 'mb-ci-runtime--43--a2--lane-02' has expired"),
            "lane of another attempt": (lambda attempt: attempt.set_artifact(301, name="mb-ci-runtime--43--a1--lane-02"),
                                        f"artifact 'mb-ci-runtime--43--a1--lane-02' is not of attempt 2: {RERUN}"),
            "lane never uploaded": (lambda attempt: attempt.set_artifact(301, workflow_run={"id": 77}),
                                    "this attempt has no artifact 'mb-ci-runtime--43--a2--lane-02'"),
            "extra lane": (lambda attempt: attempt.publish("runtime", "lane-zz", b"x", artifact_id=390, job=LANE),
                           "artifact 'mb-ci-runtime--43--a2--lane-zz' is not one the plan expects"),
            "failed lane job": (lambda attempt: attempt.change_job(LANE, conclusion="failure"),
                                f"required job '{LANE}' has not finished as the graph expects"),
            "earlier lane job": (lambda attempt: attempt.change_job(LANE, run_attempt=1),
                                 f"job '{LANE}' ran in attempt 1, not in attempt 2: {RERUN}"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.world(lanes=2)
                change(attempt)
                self.assert_refused(attempt, message)
                self.assertLessEqual(attempt.api.request_count, 7)

    def test_a_lanes_results_must_be_its_own_validated_against_the_selected_build(self) -> None:
        def only(lane_id: str, edit):
            return lambda lane, document, files, data: edit(document, files) if lane == lane_id else None

        def stray(document, files) -> None:
            files["stray.json"] = b"{}\n"

        cases = {
            "a target's hook": (only("lane-a", lambda document, files: document.update(hook="verify_target")),
                                "unit is outside the protected plan"),
            "another attempt": (only("lane-02", lambda document, files: document.update(run_attempt=1)),
                                "validation record differs from protected execution/input context"),
            "another config": (only("lane-a", lambda document, files: document.update(source_config_sha256="0" * 64)),
                               "validation record differs from protected execution/input context"),
            "another input": (only("lane-a", lambda document, files: document.update(input_sha256="0" * 64)),
                              "validation record differs from protected execution/input context"),
            "changed report": (only("lane-a", lambda document, files: files.update({"lane-a.json": b'{"x":1}\n'})),
                               "native verification reports differ from exact byte inventory"),
            "no validation record": (only("lane-a", lambda document, files: document.clear()), "cannot read"),
            "stray file": (only("lane-a", stray), "frozen runtime differs from its complete file inventory"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                code, stderr, _ = self.aggregate(self.world(lanes=2, change=change))
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)
        swapped = self.world(lanes=2, upload=lambda lane_id: {"lane-a": "lane-02", "lane-02": "lane-a"}[lane_id])
        self.assert_refused(swapped, "runtime descriptor scope differs")
        # Lanes that ran another Build than the one this job selected.
        attempt = self.world(select=False)
        attempt.select("pull-request", attempt.owning, h("another envelope"))
        self.assert_refused(attempt, "validation record differs from protected execution/input context")
        attempt = self.world(select=False)
        other = copy.deepcopy(attempt.owning)
        other["artifact"]["digest"] = "sha256:" + "0" * 64
        attempt.select("pull-request", other, BUILD_ENVELOPE)
        self.assert_refused(attempt, "runtime owning Build selection differs")

    def test_the_selection_record_must_be_this_attempts_own(self) -> None:
        attempt = self.world(select=False)
        self.assert_refused(attempt, "cannot read the state record ci-selection.json", "ci-state")
        attempt = self.world(select=False)
        attempt.select("pull-request", attempt.owning, BUILD_ENVELOPE, plan_sha256="0" * 64)
        self.assert_refused(attempt, "does not equal the complete admitted binding")
        cases = {
            "another attempt": (lambda document: canonical_json({**document, "request": {**document["request"],
                                                                                          "run_attempt": 1}}),
                                "the selection record belongs to another run or attempt"),
            "another run": (lambda document: canonical_json({**document, "request": {**document["request"],
                                                                                      "run_id": 77}}),
                            "the selection record belongs to another run or attempt"),
            "not canonical": (lambda document: json.dumps(document, indent=1).encode(),
                              "ci-selection.json is not canonical JSON"),
            "a target partition": (lambda document: canonical_json({**document, "build": partition}),
                                   "runtime input must be a complete Build bundle"),
        }
        for label, (encode, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.world(select=False)
                document = attempt.select("pull-request", attempt.owning, BUILD_ENVELOPE)
                partition = copy.deepcopy(attempt.owning)
                partition["artifact"]["name"] = "mb-ci-target--42--a2--target-a"
                state = attempt.directory / "other-state"
                identity.write_subject(state, attempt.record)
                identity.write_state_record(state, grammar.CI_PLAN_NAME, canonical_json(attempt.plan))
                identity.write_state_record(state, grammar.CI_SELECTION_NAME, encode(document))
                attempt.state = state
                self.assert_refused(attempt, message)
                self.assertEqual(attempt.api.request_count, 0)

    def test_only_the_aggregating_job_of_a_packaged_run_that_ran_its_lanes(self) -> None:
        attempt = self.attempt(listing="packaged-rebuilt", caller="packaged", producer="build", push=True)
        attempt.select("rebuilt", attempt.add_build_run(b"x"), BUILD_ENVELOPE)
        self.assert_refused(attempt, "`ci aggregate` is a step of a packaged job")
        self.assertEqual(attempt.api.request_count, 0)
        attempt = self.attempt(listing="packaged-reuse", caller="packaged", push=True)
        attempt.select("selected", attempt.add_build_run(b"x"), BUILD_ENVELOPE)
        self.assert_refused(attempt, "this attempt lists the jobs of no single mode of selected, rebuilt")
        attempt = self.world()
        (attempt.directory / "index").mkdir()
        code, _, stderr = attempt.command("aggregate", "--output", str(attempt.directory / "index"))
        self.assertEqual(code, 2)
        self.assertIn("the upload directory already exists", stderr)
        self.assertEqual(attempt.api.request_count, 0)
        self.assertEqual(attempt.command("aggregate")[0], 2)

    def test_a_source_a_run_or_a_lane_that_moves_forbids_the_index(self) -> None:
        attempt = self.world()
        attempt.set_run(run_attempt=3)
        self.assert_refused(attempt, "a newer producer attempt exists")
        for label, moved, message in (
                ("expiry", lambda attempt: attempt.set_artifact(300, expired=True), "has expired"),
                ("rerun", lambda attempt: attempt.set_run(run_attempt=3), "producer run changed between"),
                ("draft", lambda attempt: attempt.api.add_response(
                    f"/repos/{attempt.api.repository}/pulls/7", {**attempt.pull, "draft": True}), "PR must be open")):
            with self.subTest(after_download=label):
                attempt = self.world(lanes=2)
                with after_download(attempt.api, lambda: moved(attempt), count=2):
                    self.assert_refused(attempt, message)

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        attempt = self.world(lanes=2, max_requests=10)
        code, stderr, _ = self.aggregate(attempt)
        self.assertEqual((code, attempt.api.request_count), (2, 10))
        self.assertIn("request-budget", stderr)


if __name__ == "__main__":
    unittest.main()
