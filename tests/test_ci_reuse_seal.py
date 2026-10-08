"""The gate of a reuse run seals a reference, and a consumer reads it back as a reused generation.

``ci seal-gate`` runs through the real ``ci`` parser on a real state directory; the reference it
writes is uploaded to the fake GitHub as the run's ``mb-ci-reuse`` artifact and read back by
``reuse.download_reuse_reference``, which is the proof that writer and reader agree. See
``tests/ci_reuse.py`` for the world.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import unittest
from unittest.mock import patch

from mod_base.build_ci import gate, reuse, transport
from mod_base.build_ci.graph import run_graph
from mod_base.build_ci.records import reuse_reference, validate_reuse_reference
from mod_base.errors import MbError, Unavailable
from mod_base.github.api import ApiError
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json
from tests import ci_mod_harness as h
from tests.ci_attempt import ATTEMPT, RUN
from tests.ci_reuse import (BUILD_SEAL, BUNDLE, GATE, LANE, ORIGINAL_RUN, PACKAGED_SEAL, REFERENCE, RESULTS, TEST_MERGE,
                            Merged, ReuseCase, downloaded, publish_reference, reuse_attempt)
from tests.test_ci_transport import after_download

PLAN_JOB = "Shared Build / Plan protected Build"
POLICY = "Shared Build / Verify protected policy"
#: ``ci seal-gate`` in a reuse run of either caller: the attempt (6), the admission (25: the pushed
#: commit is already read) and everything mutable once more (4 of the attempt, 12 of the admission).
SEAL_REQUESTS = 47
#: ``reuse.download_reuse_reference``: the live subject, the reuse run, its jobs, the reference by
#: id, the run's artifacts and the download (8); the original pair (19); 4 and 9 to observe again.
READ_REQUESTS = 40


class SealCase(ReuseCase):
    def sealing(self, caller: str = "build", *, targets: int = 1, lanes: int = 1, **merged) -> Merged:
        """A reuse run of ``caller`` whose gate job is sealing, and the pull request it covers."""

        return Merged(reuse_attempt(self, caller, targets=targets, lanes=lanes), **merged)

    def seal(self, world: Merged):
        """Run ``ci seal-gate``; return ``(code, stdout, stderr, reference bytes or None)``."""

        attempt = world.attempt
        output = attempt.directory / "upload"
        code, stdout, stderr = attempt.command("seal-gate", "--gate", attempt.caller, "--output", str(output))
        self.assertEqual([path for path in attempt.state.iterdir() if path.is_dir()], [])
        if code:
            self.assertEqual(stdout, "")
            self.assertFalse(output.exists())
            return code, stdout, stderr, None
        self.assertEqual([path.name for path in output.iterdir()], [grammar.CI_REUSE_NAME])
        return code, stdout, stderr, (output / grammar.CI_REUSE_NAME).read_bytes()

    def refused(self, world: Merged, start: str, message: str = "") -> str:
        code, _, stderr, raw = self.seal(world)
        self.assertEqual((code, raw), (2, None))
        self.assertTrue(stderr.startswith(f"mod_base: {start}: "), stderr)
        self.assertIn(message, stderr)
        self.assertEqual(world.api.mutations, [])
        return stderr

    def sealed(self, caller: str = "build", **options):
        """``(world, reference, descriptor)`` of a reuse run that sealed and finished."""

        world = self.sealing(caller, **options)
        attempt = world.attempt
        authenticated = gate.authenticate_attempt(attempt.api, record=attempt.record, plan=attempt.plan, gate=caller,
                                                  run_id=attempt.run_id, run_attempt=ATTEMPT)
        name, document = gate.seal_reuse(authenticated, temporary_root=self.scratch)
        self.assertEqual((name, list(self.scratch.iterdir())), (grammar.CI_REUSE_NAME, []))
        return world, document, publish_reference(attempt, canonical_json(document))

    def read(self, world: Merged, descriptor: dict, **changes):
        arguments = {"descriptor": descriptor, "plan": world.covered, "temporary_root": self.scratch, **changes}
        try:
            return reuse.download_reuse_reference(world.api, **arguments)
        finally:
            self.assertEqual(list(self.scratch.iterdir()), [], "a record download was left behind")
            self.assertEqual(world.api.mutations, [])

    def unreadable(self, world: Merged, descriptor: dict, message: str = "", **changes) -> MbError:
        with self.assertRaisesRegex(MbError, message) as caught:
            self.read(world, descriptor, **changes)
        self.assertNotIsInstance(caught.exception, Unavailable)
        return caught.exception


class SealTests(SealCase):
    def test_the_gate_seals_a_reference_that_the_reader_accepts(self) -> None:
        for caller, targets, lanes in (("build", 1, 1), ("packaged", 1, 1), ("build", 17, 2)):
            world = self.sealing(caller, targets=targets, lanes=lanes)
            attempt, plan = world.attempt, world.covered
            with self.subTest(caller=caller, targets=targets):
                code, stdout, stderr, raw = self.seal(world)
                self.assertEqual((code, stderr), (0, ""))
                self.assertEqual(stdout, f"seal-gate: {caller} gate of run {RUN[caller]} attempt 2 sealed in mode "
                                         "reuse as ci-reuse.json\n")
                self.assertEqual(attempt.budgets, [limits.MAX_CI_GATE_REQUESTS])
                self.assertEqual(world.api.request_count, SEAL_REQUESTS)
                self.assertLess(SEAL_REQUESTS, 60)
                document = json.loads(raw)
                self.assertEqual(raw, canonical_json(document))
                self.assertEqual(document, {
                    "kind": "mod-base.ci.reuse", "schema_version": 1, "identity": plan["identity"],
                    "plan_sha256": plan["plan_sha256"], "profile": plan["profile"],
                    "producer": attempt.producer("reuse"), "source": world.source})
                self.assertIs(validate_reuse_reference(document, plan=plan), document)

                # The covered merge and the tested commit, run, attempt and artifact stay apart.
                source = document["source"]
                self.assertEqual((document["identity"]["tested_sha"], document["identity"]["pr_number"]),
                                 (h.CONTROLLER_SHA, 0))
                self.assertEqual((source["identity"]["tested_sha"], source["identity"]["pr_number"]), (TEST_MERGE, 7))
                self.assertEqual(document["producer"]["run_id"], RUN[caller])
                for key, producer, artifact_id in (("build_seal", "build", BUILD_SEAL),
                                                   ("packaged_seal", "packaged", PACKAGED_SEAL)):
                    seal, record = source[key], world.records[artifact_id]
                    self.assertEqual((seal["producer"]["run_id"], seal["producer"]["run_attempt"]),
                                     (ORIGINAL_RUN[producer], ATTEMPT))
                    self.assertEqual((seal["artifact"]["id"], seal["artifact"]["digest"]),
                                     (artifact_id, record["digest"]))
                    # A reference renews nothing: the record expires when it always would have.
                    self.assertEqual(seal["artifact"]["expires_at"], record["expires_at"])

                descriptor = publish_reference(attempt, raw)
                before = world.api.request_count
                with patch.object(world.api, "download", wraps=world.api.download) as downloads:
                    reference, build, packaged = self.read(world, descriptor)
                self.assertEqual((reference, build, packaged),
                                 (document, world.documents["build"], world.documents["packaged"]))
                self.assertEqual(world.api.request_count - before, READ_REQUESTS)
                self.assertLess(READ_REQUESTS, 60)
                # The reused generation is the original one: its bundle, its lanes, its runs.
                self.assertEqual(packaged["owning_build"], build["artifacts"][0])
                self.assertEqual({item["producer"]["run_id"] for item in (*build["artifacts"], *packaged["artifacts"])},
                                 set(ORIGINAL_RUN.values()))
                self.assertEqual(downloaded(downloads), [REFERENCE, BUILD_SEAL, PACKAGED_SEAL])

    def test_a_reuse_that_is_no_longer_admitted_fails_the_gate(self) -> None:
        def pushed_directly(world):
            world.associated = []
            world.seed_pull()

        cases = {"original-evidence-unavailable": lambda world: world.set_artifact(BUNDLE, expired=True),
                 "no-merged-pull-request": pushed_directly,
                 "original-run-failed": lambda world: world.set_run("packaged", conclusion="cancelled"),
                 "kit-pin-differs": lambda world: world.set_run("build", referenced_workflows=[
                     entry for entry in world.runs["build"]["referenced_workflows"]
                     if entry["path"].startswith(h.REPOSITORY)])}
        for reason, change in cases.items():
            world = self.sealing()
            change(world)
            with self.subTest(reason=reason):
                stderr = self.refused(world, "ci-reuse-refused", f"which is no longer admitted ({reason}): ")
                self.assertTrue(stderr.endswith("; rerun all jobs\n"), stderr)
        world = self.sealing("packaged", tested_tree="7" * 40)
        self.refused(world, "ci-reuse-refused", "(merged-tree-differs)")

    def test_what_stops_an_admission_stops_the_gate(self) -> None:
        world = self.sealing()
        world.set_run("build", status="in_progress", conclusion=None)
        self.refused(world, "ci-original-pending", "build run 142 of pull request #7 has not finished")

        world = self.sealing()
        world.seal("packaged", raw=canonical_json(world.documents["packaged"]) + b"\n")
        self.refused(world, "invalid-document", "tested receipt must be canonical JSON")

        world = self.sealing()
        failure = ApiError("GitHub is unavailable", status=502, method="GET", path="/rate_limit")
        with patch.object(world.api, "download", side_effect=failure):
            self.refused(world, "github-api", "GitHub is unavailable")

    def test_everything_mutable_is_observed_again_before_the_reference_is_written(self) -> None:
        changed = "changed between the start of the command and its effect"
        moves = {"original record": (lambda world: world.set_artifact(PACKAGED_SEAL, expired=True), changed),
                 "original bundle": (lambda world: world.set_artifact(BUNDLE, expired=True), changed),
                 "original rerun": (lambda world: world.set_run("build", run_attempt=3), changed),
                 "this run rerun": (lambda world: world.attempt.set_run(run_attempt=3), changed),
                 "default branch": (lambda world: world.api.set_branch(h.BRANCH, "f" * 40, "e" * 40),
                                    "protected controller has moved")}
        for name, (move, message) in moves.items():
            world = self.sealing()
            # After the last record was read: the admission stands, and then the world moves.
            with self.subTest(move=name), after_download(world.api, lambda: move(world), count=3):
                self.refused(world, "invalid-document", message)
        # A lane that expires before its listing is read was never admitted: the gate refuses the reuse.
        world = self.sealing()
        with after_download(world.api, lambda: world.set_artifact(LANE, expired=True), count=3):
            self.refused(world, "ci-reuse-refused", "(original-evidence-unavailable)")

    def test_only_a_reuse_run_seals_a_reference(self) -> None:
        world = self.sealing()
        attempt = world.attempt
        authenticated = gate.authenticate_attempt(attempt.api, record=attempt.record, plan=attempt.plan, gate="build",
                                                  run_id=attempt.run_id, run_attempt=ATTEMPT)
        with self.assertRaisesRegex(MbError, "a reuse run seals a reuse reference"):
            gate.seal_gate(authenticated, config_sha256="0" * 64, temporary_root=self.scratch)
        before = world.api.request_count
        with self.assertRaisesRegex(MbError, "only a reuse run seals a reuse reference"):
            gate.seal_reuse(dataclasses.replace(authenticated, mode="full"), temporary_root=self.scratch)
        self.assertEqual(world.api.request_count, before)


class ReaderTests(SealCase):
    def test_a_run_that_also_holds_evidence_of_its_own_is_a_mixed_generation(self) -> None:
        for caller in ("build", "packaged"):
            # A worker that ran is not the reuse graph, whatever the reference says.
            world, _, descriptor = self.sealed(caller)
            worker = POLICY if caller == "build" else "Shared Packaged E2E / Seal complete packaged results"
            world.attempt.change_job(worker, conclusion="success")
            with self.subTest(caller=caller, mixed="worker job"):
                self.unreadable(world, descriptor, "exact job graph mismatch")
            for kind, unit_id in (("build", None), ("tested", caller), ("results", None), ("runtime", "lane-a")):
                world, _, descriptor = self.sealed(caller)
                world.attempt.publish(kind, unit_id, b"evidence of this attempt", artifact_id=710, job=GATE[caller])
                with self.subTest(caller=caller, mixed=kind):
                    self.unreadable(world, descriptor, "is a mixed generation")
        # What an earlier attempt of the same run uploaded is history, not a mix.
        world, document, descriptor = self.sealed()
        world.attempt.publish("build", None, b"the Build of attempt 1", artifact_id=710, attempt=1, job=GATE["build"])
        self.assertEqual(self.read(world, descriptor)[0], document)

    def test_the_gate_must_have_sealed_after_everything_it_covers_had_finished(self) -> None:
        world, _, descriptor = self.sealed()
        world.job("packaged", GATE["packaged"]).update(completed_at="2026-10-07T10:01:10Z")
        world.set_jobs("packaged", world.jobs["packaged"])
        self.unreadable(world, descriptor, "an original job completed after the reuse gate started")
        world, _, descriptor = self.sealed()
        world.attempt.change_job(PLAN_JOB, completed_at="2026-10-07T10:01:10Z")
        self.unreadable(world, descriptor, "the reuse gate started before its prerequisites completed")

    def test_a_reference_cannot_substitute_the_tested_commit_or_an_original_artifact(self) -> None:
        def swap(document):
            source = document["source"]
            source["build_seal"], source["packaged_seal"] = source["packaged_seal"], source["build_seal"]

        substitutions = {
            "tested commit": lambda d: d["source"]["identity"].update(tested_sha=d["identity"]["tested_sha"]),
            "record digest": lambda d: d["source"]["build_seal"]["artifact"].update(digest="sha256:" + "f" * 64),
            "record id": lambda d: d["source"]["packaged_seal"]["artifact"].update(id=RESULTS),
            "run": lambda d: d["source"]["build_seal"]["producer"].update(run_id=141),
            "attempt": lambda d: d["source"]["build_seal"]["producer"].update(run_attempt=1),
            "original plan": lambda d: d["source"].update(plan_sha256="f" * 64),
            "head": lambda d: d["source"]["identity"].update(head_sha="f" * 40),
            "swapped records": swap,
            "covered tree": lambda d: d["identity"].update(tested_tree="7" * 40),
            "covered plan": lambda d: d.update(plan_sha256="f" * 64),
            "sealing run": lambda d: d["producer"].update(run_id=99),
        }
        for name, substitute in substitutions.items():
            world, document, _ = self.sealed()
            substitute(document)
            with self.subTest(substitution=name):
                self.unreadable(world, publish_reference(world.attempt, canonical_json(document)))
        world, document, _ = self.sealed()
        for raw, name in ((canonical_json(document) + b"\n", grammar.CI_REUSE_NAME),
                          (canonical_json({**document, "extra": True}), grammar.CI_REUSE_NAME),
                          (canonical_json(document), grammar.CI_GATE_NAME)):
            with self.subTest(raw=raw[-12:], name=name):
                self.unreadable(world, publish_reference(world.attempt, raw, name=name))

    def test_the_covering_run_and_both_originals_are_authenticated_again(self) -> None:
        newer, unfinished = "a newer producer attempt exists", "did not complete successfully"
        changes = {
            "this run rerun": (lambda world: world.attempt.set_run(run_attempt=3), newer),
            "this run unfinished": (lambda world: world.attempt.set_run(status="in_progress", conclusion=None),
                                    unfinished),
            "this run failed": (lambda world: world.attempt.set_run(conclusion="failure"), unfinished),
            "reference expired": (lambda world: world.attempt.set_artifact(REFERENCE, expired=True),
                                  "expired artifact"),
            "default branch moved": (lambda world: world.api.set_branch(h.BRANCH, "f" * 40, "e" * 40),
                                     "protected controller has moved"),
            "original rerun": (lambda world: world.set_run("packaged", run_attempt=3), newer),
            "original failed": (lambda world: world.set_run("build", conclusion="failure"), unfinished),
            "merge undone": (lambda world: world.change_pull(merged=False), r"\$\.merged\.pr"),
            "test merge parents": (lambda world: world.api.add_commit(
                TEST_MERGE, world.plan["identity"]["tested_tree"],
                parents=world.plan["identity"]["tested_parents"][::-1]), r"\$\.merged\.tested_parents"),
            "record corrupt": (lambda world: world.api.add_artifact(world.records[BUILD_SEAL], b"other bytes"),
                               "download length or SHA-256 differs"),
            "record foreign": (lambda world: world.set_artifact(PACKAGED_SEAL, workflow_run={"head_sha": "f" * 40}),
                               "expired artifact or wrong protected producer"),
        }
        for name, (change, message) in changes.items():
            world, _, descriptor = self.sealed()
            change(world)
            with self.subTest(change=name):
                self.unreadable(world, descriptor, message)

    def test_originals_that_have_expired_since_are_unavailable_not_corrupt(self) -> None:
        for artifact_id in (BUILD_SEAL, PACKAGED_SEAL, BUNDLE, LANE, RESULTS):
            world, _, descriptor = self.sealed()
            world.set_artifact(artifact_id, expired=True)
            with self.subTest(artifact_id=artifact_id), self.assertRaises(transport.OriginalUnavailable) as caught:
                self.read(world, descriptor)
            self.assertEqual((caught.exception.reason, caught.exception.exit_code), ("ci-original-unavailable", 3))

    def test_everything_mutable_is_observed_again_before_the_generation_is_returned(self) -> None:
        moves = {"this run rerun": lambda world: world.attempt.set_run(run_attempt=3),
                 "reference expired": lambda world: world.attempt.set_artifact(REFERENCE, expired=True),
                 "original record expired": lambda world: world.set_artifact(BUILD_SEAL, expired=True),
                 "original bundle expired": lambda world: world.set_artifact(BUNDLE, expired=True),
                 "original rerun": lambda world: world.set_run("packaged", run_attempt=3)}
        for name, move in moves.items():
            world, _, descriptor = self.sealed()
            # After the last of the three records was read: everything held, and then the world moves.
            with self.subTest(move=name), after_download(world.api, lambda: move(world), count=3):
                self.unreadable(world, descriptor, "changed between the start of the command and its effect")

    def test_a_descriptor_that_is_no_reference_of_this_plan_is_refused_before_any_read(self) -> None:
        world, _, descriptor = self.sealed()
        full = copy.deepcopy(descriptor)
        full["producer"]["graph_sha256"] = run_graph("build", "full").sha256(world.covered)
        cases = [{"descriptor": {**descriptor, "plan_sha256": "f" * 64}}, {"descriptor": world.seals["build"]},
                 {"descriptor": full}, {"plan": world.plan}, {"descriptor": {**descriptor, "extra": True}}]
        for index, changes in enumerate(cases):
            with self.subTest(index=index), patch.object(world.api, "get_json") as reads:
                self.unreadable(world, changes.pop("descriptor", descriptor), **changes)
                reads.assert_not_called()


class RecordTests(SealCase):
    def test_a_reference_binds_the_work_of_the_covered_plan_and_a_push(self) -> None:
        world = self.sealing()
        plan, producer = world.covered, world.attempt.producer("reuse")
        document = reuse_reference(plan=plan, producer=producer, source=world.source)
        self.assertIs(validate_reuse_reference(document, plan=plan), document)
        self.assertEqual(document["source"], world.source)
        self.assertIsNot(document["source"]["build_seal"], world.seals["build"])

        # Gates sealed for another plan, consistently: the structure holds, the covered plan tells.
        other = copy.deepcopy(document)
        for holder in (other["source"], other["source"]["build_seal"], other["source"]["packaged_seal"]):
            holder["plan_sha256"] = "f" * 64
        validate_reuse_reference(copy.deepcopy(other))
        for refuse in (lambda: validate_reuse_reference(other, plan=plan),
                       lambda: reuse_reference(plan=plan, producer=producer, source=other["source"])):
            with self.assertRaisesRegex(MbError, "sealed for another plan than the covered one"):
                refuse()
        # Only a push reuses: a dispatch or a schedule on the same commit asks for a full run.
        for event in ("workflow_dispatch", "schedule"):
            with self.subTest(event=event), \
                    self.assertRaisesRegex(MbError, "only a push to the default branch reuses"):
                reuse_reference(plan=plan, producer={**producer, "event": event}, source=world.source)
        self.assertEqual(world.api.request_count, 0)


if __name__ == "__main__":
    unittest.main()
