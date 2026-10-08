"""``ci seal-gate`` end to end: the gate of a running attempt writes a receipt that the existing
readers accept once the run has finished. Real state, real ZIPs, fake GitHub API."""

from __future__ import annotations

import copy
import json
import unittest

from mod_base.build_ci import gate, graph, identity, transport
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.records import validate_gate_receipt
from mod_base.errors import MbError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from tests.ci_attempt import ATTEMPT, AttemptCase, expand, record_zip, sealed, validation, zip_tree
from tests.helpers import ci_api_run, ci_graph_jobs, h
from tests.test_ci_describe import ASSEMBLE, GATE, PLAN, POLICY, RERUN, TARGET
from tests.test_ci_transport import after_download, build_archive

PACKAGED_GATE = "Shared Packaged E2E / Verify complete packaged E2E"
AGGREGATE = "Shared Packaged E2E / Seal complete packaged results"
LANE = "Shared Packaged E2E / Run packaged lane lane-a"
LISTINGS = {"full": "build-full", "pull-request": "packaged-pull-request", "selected": "packaged-selected",
            "rebuilt": "packaged-rebuilt"}


def seed_build(attempt, mode: str, *, change=None) -> None:
    """Every target partition and the complete Build of this attempt, as their jobs upload them:
    the Build with the validation record of ``verify_build`` beside its envelope. ``change``
    edits that record, its report files and the export's archive before they are sealed."""

    producer = attempt.producer(mode)
    for index, target in enumerate(attempt.plan["targets"]):
        data, _ = build_archive(attempt.plan, producer, target_id=target["id"])
        attempt.publish("target", target["id"], data, artifact_id=110 + index)
    data, envelope = build_archive(attempt.plan, producer)
    document, files = validation(attempt.plan, hook="verify_build", unit_id=None, run_id=attempt.run_id,
                                 input_sha256=canonical_sha256(envelope))
    if change is not None:
        data = change(document, files, data) or data
    attempt.validation = document
    attempt.publish("build", None, sealed(data, document, files) if document else data, artifact_id=100)


def results_index(attempt, mode: str, owning: dict, lanes: list[dict]) -> dict:
    plan = attempt.plan
    return {"kind": "mod-base.ci.results", "schema_version": 1, "identity": copy.deepcopy(plan["identity"]),
            "plan_sha256": plan["plan_sha256"], "profile": plan["profile"], "producer": attempt.producer(mode),
            "owning_build": owning, "build_envelope_sha256": h("build-envelope"),
            "lanes": [{"id": lane["id"], "native_contract_sha256": lane["native_contract_sha256"],
                       "descriptor": descriptor, "envelope_sha256": h(lane["id"] + "-envelope"),
                       "validation_sha256": h(lane["id"] + "-validation"), "report_sha256": h(lane["id"] + "-report")}
                      for lane, descriptor in zip(plan["lanes"], lanes)]}


class GateCase(AttemptCase):
    def build_world(self, *, mode: str = "full", change=None, **options):
        """A run whose Build gate job is sealing: every earlier job has finished and uploaded."""

        if mode == "rebuilt":
            options = {"caller": "packaged", "producer": "build", "push": True, **options}
        attempt = self.attempt(listing=LISTINGS[mode], **options)
        attempt.sealing(GATE)
        seed_build(attempt, mode, change=change)
        return attempt

    def packaged_world(self, *, mode: str = "pull-request", edit=None, **options):
        """A packaged run whose gate job is sealing, with the results index its aggregate job
        sealed over the lane artifacts of this attempt. ``edit`` changes the index first."""

        attempt = self.attempt(listing=LISTINGS[mode], caller="packaged", push=mode != "pull-request", **options)
        attempt.sealing(PACKAGED_GATE)
        if mode == "rebuilt":
            seed_build(attempt, mode)
            attempt.publish("tested", "build", record_zip(grammar.CI_GATE_NAME, b"{}\n"), artifact_id=200)
            attempt.owning = attempt.descriptor(mode, 100)
        else:
            attempt.owning = attempt.add_build_run(b"the complete Build of the other run")
        attempt.lanes = []
        for index, lane in enumerate(attempt.plan["lanes"]):
            attempt.publish("runtime", lane["id"], b"results of " + lane["id"].encode(), artifact_id=300 + index)
            attempt.lanes.append(attempt.descriptor(mode, 300 + index))
        attempt.index = results_index(attempt, mode, attempt.owning, attempt.lanes)
        if edit is not None:
            edit(attempt.index)
        self.publish_index(attempt)
        return attempt

    @staticmethod
    def publish_index(attempt, raw: bytes | None = None, name: str = grammar.CI_RESULTS_NAME) -> None:
        attempt.publish("results", None, record_zip(name, canonical_json(attempt.index) if raw is None else raw),
                        artifact_id=400)

    def seal(self, attempt, gate_: str):
        """Run ``ci seal-gate``; return ``(code, stderr, receipt bytes or None)``."""

        output = attempt.directory / "upload"
        code, stdout, stderr = attempt.command("seal-gate", "--gate", gate_, "--output", str(output))
        if code:
            self.assertEqual(stdout, "")
            self.assertFalse(output.exists())
            self.assertEqual([path for path in attempt.state.iterdir() if path.is_dir()], [])
            return code, stderr, None
        self.assertEqual(stderr, "")
        self.assertEqual([path.name for path in output.iterdir()], [grammar.CI_GATE_NAME])
        return code, stdout, (output / grammar.CI_GATE_NAME).read_bytes()

    def assert_rejected(self, attempt, gate_: str, message: str, reason: str = "invalid-document") -> None:
        code, stderr, _ = self.seal(attempt, gate_)
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith(f"mod_base: {reason}: "), stderr)
        self.assertIn(message, stderr)

    def assert_readers_accept(self, attempt, gate_: str, mode: str, raw: bytes, *, artifact_id: int) -> dict:
        """Finish the run, upload ``raw`` as its tested record and read it back the way a
        consumer does: the proof that writer and reader agree."""

        document = json.loads(raw)
        self.assertEqual(raw, canonical_json(document))
        attempt.complete(LISTINGS[mode])
        attempt.publish("tested", gate_, record_zip(grammar.CI_GATE_NAME, raw), artifact_id=artifact_id)
        descriptor = attempt.descriptor(mode, artifact_id)
        self.assertEqual(transport.download_gate_receipt(attempt.api, descriptor=descriptor, plan=attempt.plan,
                                                         gate=gate_, temporary_root=self.temporary), document)
        self.assertEqual(graph.authenticate_gate_timeline(attempt.api, document=document, descriptor=descriptor,
                                                          plan=attempt.plan), document["producer"]["graph_sha256"])
        return document


class BuildGateTests(GateCase):
    def test_the_receipt_of_a_full_build_is_what_the_readers_accept(self) -> None:
        attempt = self.build_world(targets=2)
        code, stdout, raw = self.seal(attempt, "build")
        self.assertEqual((code, stdout), (0, "seal-gate: build gate of run 42 attempt 2 sealed in mode full as "
                                             "ci-gate.json\n"))
        self.assertEqual(attempt.budgets, [limits.MAX_CI_GATE_REQUESTS])
        self.assertEqual(attempt.api.request_count, 15)
        document = self.assert_readers_accept(attempt, "build", "full", raw, artifact_id=200)
        self.assertIs(validate_gate_receipt(document, plan=attempt.plan), document)
        self.assertEqual((document["gate"], document["mode"], document["owning_build"]), ("build", "full", None))
        self.assertEqual(document["producer"], attempt.producer("full"))
        self.assertEqual(document["artifacts"], [attempt.descriptor("full", 100)])
        self.assertEqual(document["native_receipts"], [
            {"unit_id": report["unit_id"], "native_contract_sha256": report["native_contract_sha256"],
             "report_sha256": report["sha256"]} for report in attempt.validation["reports"]])
        self.assertEqual([receipt["unit_id"] for receipt in document["native_receipts"]], ["target-a", "target-02"])

    def test_seventeen_targets_cost_the_same_fifteen_requests(self) -> None:
        attempt = self.build_world(targets=17, lanes=2)
        self.assertEqual(self.seal(attempt, "build")[0], 0)
        self.assertEqual(attempt.api.request_count, 15)

    def test_a_protected_push_and_a_rebuilding_packaged_run_seal_their_build_gate(self) -> None:
        attempt = self.build_world(push=True)
        code, _, raw = self.seal(attempt, "build")
        # A protected source costs two requests less than a pull request; reading the mode costs one.
        self.assertEqual((code, attempt.api.request_count), (0, 14))
        document = self.assert_readers_accept(attempt, "build", "full", raw, artifact_id=200)
        self.assertEqual(document["producer"]["event"], "push")
        attempt = self.build_world(mode="rebuilt")
        code, _, raw = self.seal(attempt, "build")
        self.assertEqual(code, 0)
        document = json.loads(raw)
        self.assertEqual((document["mode"], document["producer"]["run_id"]), ("rebuilt", 43))
        self.assertIs(validate_gate_receipt(document, plan=attempt.plan), document)

    def test_every_job_before_the_gate_must_have_finished_as_the_graph_says(self) -> None:
        def unsealed(attempt) -> None:
            attempt.job(ASSEMBLE)["steps"][2]["completed_at"] = "2026-10-07T10:03:00.000Z"
            attempt.seed_jobs()

        def without(name):
            def change(attempt) -> None:
                attempt.jobs.remove(attempt.job(name))
                attempt.seed_jobs()
            return change

        cases = {
            "failed target": (lambda attempt: attempt.change_job(TARGET, conclusion="failure"),
                              f"required job '{TARGET}' has not finished as the graph expects"),
            "skipped policy": (lambda attempt: attempt.change_job(POLICY, conclusion="skipped"),
                               f"required job '{POLICY}' has not finished as the graph expects"),
            "running assemble": (lambda attempt: attempt.change_job(ASSEMBLE, status="in_progress", conclusion=None),
                                 f"required job '{ASSEMBLE}' has not finished as the graph expects"),
            "deferral that ran": (lambda attempt: attempt.change_job("Build deferred for draft", conclusion="success"),
                                  "required job 'Build deferred for draft' has not finished as the graph expects"),
            "no guard job": (without("Verify pinned mod-base / Authenticate the pinned kit"), "expected exactly one job"),
            "unsealed upload": (unsealed, "upload started before sealing finished"),
            "earlier attempt": (lambda attempt: attempt.change_job(PLAN, run_attempt=1),
                                f"job '{PLAN}' ran in attempt 1, not in attempt 2: {RERUN}"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.build_world()
                change(attempt)
                code, stderr, _ = self.seal(attempt, "build")
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_every_artifact_of_the_attempt_must_be_present(self) -> None:
        cases = {
            "expired partition": (lambda attempt: attempt.set_artifact(110, expired=True),
                                  "artifact 'mb-ci-target--42--a2--target-a' has expired"),
            "expired build": (lambda attempt: attempt.set_artifact(100, expired=True),
                              "artifact 'mb-ci-build--42--a2' has expired"),
            "build of another attempt": (lambda attempt: attempt.set_artifact(100, name="mb-ci-build--42--a1"),
                                         f"artifact 'mb-ci-build--42--a1' is not of attempt 2: {RERUN}"),
            "second build": (lambda attempt: attempt.api.add_artifact({**attempt.records[100], "id": 190},
                                                                      attempt.archives[100]),
                             "artifact 'mb-ci-build--42--a2' is listed more than once"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.build_world()
                change(attempt)
                self.assert_rejected(attempt, "build", message)

    def test_the_complete_build_and_its_validation_record_must_be_this_attempts(self) -> None:
        def partition(document, files, data):
            return build_archive(self.plan, self.producer, target_id="target-a")[0]

        def other_plan(document, files, data):
            plan = copy.deepcopy(self.plan)
            plan["identity"]["tested_tree"] = "9" * 40
            return build_archive(plan, self.producer)[0]

        def drop_report(document, files, data):
            files.clear()

        def extra_file(document, files, data):
            files["stray.json"] = b"{}\n"

        cases = {
            "target partition": (partition, "selected artifact does not bind export scope/target"),
            "another plan": (other_plan, "does not equal the complete admitted binding"),
            "no validation record": (lambda document, files, data: document.clear(), "cannot read"),
            "report missing": (drop_report, "cannot read the sealed Build of this attempt"),
            "stray file": (extra_file, "frozen export differs from its exact file inventory"),
            "target hook": (lambda document, files, data: document.update(hook="verify_target", unit_id="target-a"),
                            "validation record differs from protected execution/input context"),
            "another attempt": (lambda document, files, data: document.update(run_attempt=1),
                                "validation record differs from protected execution/input context"),
            "another config": (lambda document, files, data: document.update(source_config_sha256="0" * 64),
                               "validation record differs from protected execution/input context"),
            "another input": (lambda document, files, data: document.update(input_sha256="0" * 64),
                              "validation record differs from protected execution/input context"),
            "another contract": (lambda document, files, data: document["reports"][0].update(
                native_contract_sha256="0" * 64), "reports do not cover the exact ordered protected native contracts"),
            "changed report": (lambda document, files, data: files.update({"target-a.json": b'{"unit":"x"}\n'}),
                               "native verification reports differ from exact byte inventory"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.attempt(listing="build-full")
                attempt.sealing(GATE)
                self.plan, self.producer = attempt.plan, attempt.producer("full")
                seed_build(attempt, "full", change=change)
                code, stderr, _ = self.seal(attempt, "build")
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)

    def test_a_source_a_run_or_an_artifact_that_moves_forbids_the_receipt(self) -> None:
        attempt = self.build_world()
        attempt.api.add_response(f"/repos/{attempt.api.repository}/pulls/7", {**attempt.pull, "draft": True})
        self.assert_rejected(attempt, "build", "PR must be open and ready")
        attempt = self.build_world()
        attempt.set_run(run_attempt=3)
        self.assert_rejected(attempt, "build", "a newer producer attempt exists")
        attempt = self.build_world()
        attempt.set_run(referenced_workflows=attempt.run["referenced_workflows"][:1])
        self.assert_rejected(attempt, "build", "does not list the guard and every called kit workflow")
        for label, moved, message in (
                ("expiry", lambda attempt: attempt.set_artifact(100, expired=True), "has expired"),
                ("rerun", lambda attempt: attempt.set_run(run_attempt=3), "producer run changed between"),
                ("draft", lambda attempt: attempt.api.add_response(
                    f"/repos/{attempt.api.repository}/pulls/7", {**attempt.pull, "draft": True}), "PR must be open")):
            with self.subTest(after_download=label):
                attempt = self.build_world()
                with after_download(attempt.api, lambda: moved(attempt)):
                    self.assert_rejected(attempt, "build", message)

    def test_the_gate_must_be_this_jobs_and_the_upload_directory_new(self) -> None:
        attempt = self.build_world()
        self.assert_rejected(attempt, "packaged", "is not the gate of this job")
        self.assertEqual(attempt.api.request_count, 0)
        (attempt.directory / "upload").mkdir()
        code, _, stderr = attempt.command("seal-gate", "--gate", "build", "--output", str(attempt.directory / "upload"))
        self.assertEqual(code, 2)
        self.assertIn("the upload directory already exists", stderr)
        self.assertEqual((attempt.api.request_count, list((attempt.directory / "upload").iterdir())), (0, []))
        for flags in (("--gate", "build"), ("--output", "x"), ("--gate", "status", "--output", "x")):
            with self.subTest(flags=flags):
                self.assertEqual(attempt.command("seal-gate", *flags)[0], 2)

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        attempt = self.build_world(max_requests=9)
        code, stderr, _ = self.seal(attempt, "build")
        self.assertEqual((code, attempt.api.request_count), (2, 9))
        self.assertIn("request-budget", stderr)


class BuildFanInTests(GateCase):
    def test_target_partitions_become_a_tested_build_that_the_readers_accept(self) -> None:
        """The whole Build fan-in with real bytes: ``ci assemble`` in the assembling job, the
        validator's record and the upload of the sealed root, ``ci seal-gate`` in the gate job
        with a state of its own, and the readers of the tested record once the run has finished."""

        attempt = self.attempt(listing="build-full", targets=2, lanes=2)
        attempt.sealing(ASSEMBLE)
        for index, target in enumerate(attempt.plan["targets"]):
            data, partition = build_archive(attempt.plan, attempt.producer("full"), target_id=target["id"])
            record = validation(attempt.plan, hook="verify_target", unit_id=target["id"], run_id=attempt.run_id,
                                input_sha256=canonical_sha256(partition))
            attempt.publish("target", target["id"], sealed(data, *record), artifact_id=110 + index)
        self.assertEqual(attempt.command("assemble")[0], 0)
        envelope = verify_build_export(attempt.sealed_build, plan=attempt.plan)
        document, files = validation(attempt.plan, hook="verify_build", unit_id=None, run_id=attempt.run_id,
                                     input_sha256=canonical_sha256(envelope))
        attempt.jobs = expand(ci_graph_jobs("build-full"), attempt.plan)
        attempt.sealing(GATE)
        attempt.publish("build", None, sealed(zip_tree(attempt.sealed_build), document, files), artifact_id=100)
        attempt.state = attempt.directory / "gate-state"
        identity.write_subject(attempt.state, attempt.record)
        identity.write_state_record(attempt.state, grammar.CI_PLAN_NAME, canonical_json(attempt.plan))
        code, _, raw = self.seal(attempt, "build")
        self.assertEqual(code, 0)
        receipt = self.assert_readers_accept(attempt, "build", "full", raw, artifact_id=200)
        self.assertEqual(receipt["artifacts"], [attempt.descriptor("full", 100)])
        self.assertEqual([entry["report_sha256"] for entry in receipt["native_receipts"]],
                         [report["sha256"] for report in document["reports"]])


class PackagedGateTests(GateCase):
    def test_a_pull_request_receipt_names_every_lane_the_index_and_the_owning_build(self) -> None:
        attempt = self.packaged_world(targets=2, lanes=2)
        code, stdout, raw = self.seal(attempt, "packaged")
        self.assertEqual((code, stdout), (0, "seal-gate: packaged gate of run 43 attempt 2 sealed in mode "
                                             "pull-request as ci-gate.json\n"))
        self.assertEqual(attempt.api.request_count, 20)
        document = self.assert_readers_accept(attempt, "packaged", "pull-request", raw, artifact_id=201)
        self.assertEqual((document["gate"], document["mode"]), ("packaged", "pull-request"))
        self.assertEqual(document["artifacts"], [*attempt.lanes, attempt.descriptor("pull-request", 400)])
        self.assertEqual(document["owning_build"], attempt.owning)
        self.assertEqual(document["native_receipts"], [
            {"unit_id": lane["id"], "native_contract_sha256": lane["native_contract_sha256"],
             "report_sha256": h(lane["id"] + "-report")} for lane in attempt.plan["lanes"]])
        self.assertEqual(len(document["native_receipts"]), 4)

    def test_thirty_four_lanes_cost_the_same_twenty_requests(self) -> None:
        attempt = self.packaged_world(targets=17, lanes=2)
        self.assertEqual(self.seal(attempt, "packaged")[0], 0)
        self.assertEqual(attempt.api.request_count, 20)
        self.assertLess(attempt.api.request_count, 60)

    def test_a_standalone_run_seals_in_the_mode_its_jobs_show(self) -> None:
        for mode in ("selected", "rebuilt"):
            with self.subTest(mode=mode):
                attempt = self.packaged_world(mode=mode)
                code, _, raw = self.seal(attempt, "packaged")
                self.assertEqual(code, 0)
                document = self.assert_readers_accept(attempt, "packaged", mode, raw, artifact_id=201)
                self.assertEqual((document["mode"], document["owning_build"]), (mode, attempt.owning))
                self.assertEqual(document["owning_build"]["producer"]["run_id"], 43 if mode == "rebuilt" else 42)

    def test_the_index_must_list_exactly_the_lanes_this_attempt_uploaded(self) -> None:
        def other_artifact(index) -> None:
            index["lanes"][1]["descriptor"]["artifact"]["digest"] = "sha256:" + "0" * 64

        cases = {
            "another lane artifact": (other_artifact, "lists other lane artifacts than this attempt uploaded"),
            "missing lane": (lambda index: index["lanes"].pop(), "does not list every planned lane"),
            "reordered lanes": (lambda index: index["lanes"].reverse(), "does not list every planned lane"),
            "another contract": (lambda index: index["lanes"][0].update(native_contract_sha256="0" * 64),
                                 "does not list every planned lane"),
            "another attempt": (lambda index: index["producer"].update(run_attempt=1), "mixed producer or attempt"),
            "another plan": (lambda index: index.update(plan_sha256="0" * 64),
                             "does not equal the complete admitted binding"),
            "unknown key": (lambda index: index.update(success=True), "has unknown keys"),
        }
        for label, (edit, message) in cases.items():
            with self.subTest(case=label):
                self.assert_rejected(self.packaged_world(lanes=2, edit=edit), "packaged", message)
        attempt = self.packaged_world()
        self.publish_index(attempt, raw=json.dumps(attempt.index).encode())
        self.assert_rejected(attempt, "packaged", "results index must be canonical JSON")
        attempt = self.packaged_world()
        self.publish_index(attempt, name="results.json")
        self.assert_rejected(attempt, "packaged", "results index requires exactly its fixed root filename")

    def test_an_extra_or_a_missing_lane_artifact_rejects(self) -> None:
        attempt = self.packaged_world()
        attempt.publish("runtime", "lane-zz", b"extra", artifact_id=390, job=LANE)
        self.assert_rejected(attempt, "packaged", "artifact 'mb-ci-runtime--43--a2--lane-zz' is not one the plan")
        attempt = self.packaged_world()
        attempt.set_artifact(300, expired=True)
        self.assert_rejected(attempt, "packaged", "artifact 'mb-ci-runtime--43--a2--lane-a' has expired")
        attempt = self.packaged_world()
        attempt.change_job(AGGREGATE, conclusion="failure")
        self.assert_rejected(attempt, "packaged", f"required job '{AGGREGATE}' has not finished")

    def test_the_owning_build_must_be_the_authenticated_build_of_its_mode(self) -> None:
        attempt = self.packaged_world()
        attempt.api.add_run(ci_api_run(attempt.plan, "build", conclusion="failure"))
        self.assert_rejected(attempt, "packaged", "producer run did not complete successfully")
        attempt = self.packaged_world()
        attempt.set_artifact(100, expired=True)
        self.assert_rejected(attempt, "packaged", "expired artifact or wrong protected producer")
        # A pull request never names a Build of its own packaged run; a rebuilding run names its own.
        attempt = self.packaged_world(mode="rebuilt")
        other = attempt.add_build_run(b"a Build of another run", artifact_id=101)
        attempt.index["owning_build"] = other
        self.publish_index(attempt)
        self.assert_rejected(attempt, "packaged", "does not own the Build this run rebuilt")
        attempt = self.packaged_world(mode="selected")
        with after_download(attempt.api, lambda: attempt.set_artifact(300, expired=True)):
            self.assert_rejected(attempt, "packaged", "artifact 'mb-ci-runtime--43--a2--lane-a' has expired")


class ModeTests(GateCase):
    def test_the_modes_a_gate_can_be_reached_in(self) -> None:
        cases = [
            (dict(listing="build-full"), "build", ("full",)),
            (dict(listing="build-full", push=True), "build", ("full", "reuse")),
            (dict(listing="packaged-pull-request", caller="packaged"), "packaged", ("pull-request",)),
            (dict(listing="packaged-selected", caller="packaged", push=True), "packaged",
             ("selected", "rebuilt", "reuse")),
            (dict(listing="packaged-rebuilt", caller="packaged", producer="build", push=True), "build", ("rebuilt",)),
        ]
        for options, gate_, modes in cases:
            with self.subTest(options=options):
                attempt = self.attempt(**options)
                self.assertEqual(gate.admissible_modes(attempt.record, gate_), modes)
                with self.assertRaises(MbError):
                    gate.admissible_modes(attempt.record, "packaged" if gate_ == "build" else "build")
        dispatched = self.attempt(listing="build-full", push=True)
        dispatched.record["event"] = "workflow_dispatch"
        self.assertEqual(gate.admissible_modes(dispatched.record, "build"), ("full",))

    def authenticate(self, attempt, gate_: str = "build"):
        return gate.authenticate_attempt(attempt.api, record=attempt.record, plan=attempt.plan, gate=gate_,
                                         run_id=attempt.run_id, run_attempt=ATTEMPT)

    def test_a_reuse_run_is_authenticated_as_one_and_cannot_seal_yet(self) -> None:
        for caller, listing, name in (("build", "build-reuse", GATE), ("packaged", "packaged-reuse", PACKAGED_GATE)):
            with self.subTest(caller=caller):
                attempt = self.attempt(listing=listing, caller=caller, push=True)
                attempt.sealing(name)
                authenticated = self.authenticate(attempt, caller)
                self.assertEqual((authenticated.mode, authenticated.descriptors), ("reuse", []))
                self.assertEqual(authenticated.producer, attempt.producer("reuse"))
                with self.assertRaisesRegex(MbError, "a reuse run seals a reuse reference"):
                    gate.seal_gate(authenticated, config_sha256="0" * 64, temporary_root=self.temporary)
                with self.assertRaises(MbError) as caught:
                    gate.seal_reuse(authenticated, temporary_root=self.temporary)
                self.assertEqual(caught.exception.reason, "unsupported")
                before = attempt.api.request_count
                self.assert_rejected(attempt, caller, "sealing a reuse reference is not implemented", "unsupported")
                self.assertEqual(attempt.api.request_count - before, 6)

    def test_a_reuse_shaped_run_must_be_exactly_the_reuse_graph(self) -> None:
        attempt = self.attempt(listing="build-reuse", push=True)
        attempt.sealing(GATE)
        attempt.change_job(PLAN, conclusion="failure")
        with self.assertRaisesRegex(MbError, f"required job '{PLAN}' has not finished as the graph expects"):
            self.authenticate(attempt)
        attempt = self.attempt(listing="build-reuse", push=True)
        attempt.sealing(GATE)
        attempt.jobs.insert(3, {**copy.deepcopy(attempt.job(POLICY)), "name": TARGET, "id": 5})
        attempt.seed_jobs()
        with self.assertRaisesRegex(MbError, "this attempt lists the jobs of no single mode of full, reuse"):
            self.authenticate(attempt)
        # A pull request is never a reuse run, whatever its jobs look like.
        attempt = self.attempt(listing="build-reuse")
        attempt.sealing(GATE)
        with self.assertRaisesRegex(MbError, "duplicate or unenrolled job"):
            self.authenticate(attempt)
        # And a full run is not authenticated by a seal_reuse call.
        authenticated = self.authenticate(self.build_world(push=True))
        self.assertEqual(authenticated.mode, "full")
        with self.assertRaisesRegex(MbError, "only a reuse run seals a reuse reference"):
            gate.seal_reuse(authenticated, temporary_root=self.temporary)


if __name__ == "__main__":
    unittest.main()
