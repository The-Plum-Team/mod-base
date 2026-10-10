"""``ci assemble`` end to end: real state, real ZIPs and a real sealed root, fake GitHub API."""

from __future__ import annotations

import copy
import json
import unittest

from mod_base.build_ci import commands_build, identity
from mod_base.build_ci.exports import verify_build_export
from mod_base.build_ci.protocol import plan_sha256
from mod_base.model import grammar, limits
from mod_base.model.canonical import canonical_json, canonical_sha256
from tests.ci_attempt import AttemptCase, sealed, validation
from tests.test_ci_describe import ASSEMBLE, GUARD, PLAN, RERUN, TARGET
from tests.test_ci_transport import build_archive


def records(state) -> list[str]:
    return sorted(path.name for path in state.iterdir())


class AssembleCommandTests(AttemptCase):
    def world(self, *, targets: int = 2, mode: str = "full", change=None, **options):
        """A Build whose assembling job runs, with what every target job uploaded: the real
        partition and the validation record of its ``verify_target`` run beside the envelope.
        ``change(target_id, document, files)`` edits a record before it is sealed."""

        attempt = self.attempt(listing="build-full" if mode == "full" else "packaged-rebuilt", targets=targets,
                               **options)
        attempt.sealing(ASSEMBLE)
        self.envelopes = []
        for index, target in enumerate(attempt.plan["targets"]):
            data, envelope = build_archive(attempt.plan, attempt.producer(mode), target_id=target["id"])
            document, files = validation(attempt.plan, hook="verify_target", unit_id=target["id"],
                                         run_id=attempt.run_id, input_sha256=canonical_sha256(envelope))
            if change is not None:
                change(target["id"], document, files)
            attempt.publish("target", target["id"], sealed(data, document, files) if document else data,
                            artifact_id=110 + index)
            self.envelopes.append(envelope)
        return attempt

    def assert_rejected(self, attempt, reason: str, message: str, **options) -> None:
        code, stdout, stderr = attempt.command("assemble", **options)
        self.assertEqual((code, stdout), (2, ""))
        self.assertTrue(stderr.startswith(f"mod_base: {reason}: "), stderr)
        self.assertIn(message, stderr)
        self.assertFalse(attempt.sealed_build.exists())
        self.assertNotIn(commands_build.PARTITIONS_NAME, records(attempt.state))
        self.assertEqual([path for path in attempt.state.iterdir() if path.is_dir()], [])

    def test_the_partitions_of_every_target_become_the_exact_sealed_union(self) -> None:
        attempt = self.world()
        code, stdout, stderr = attempt.command("assemble")
        files = sorted((file for envelope in self.envelopes for file in envelope["files"]), key=lambda file: file["path"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(stdout, f"assemble: 2 target partitions of run 42 attempt 2 assembled into {len(files)} files\n")
        envelope = verify_build_export(attempt.sealed_build, plan=attempt.plan)
        self.assertEqual((envelope["scope"], envelope["target_id"], envelope["files"]), ("complete", None, files))
        self.assertEqual(envelope["producer"], attempt.producer("full"))
        for file in files:
            self.assertEqual((attempt.sealed_build / file["path"]).read_bytes(), (file["path"] + "\n").encode())
        self.assertEqual(records(attempt.state), [commands_build.PARTITIONS_NAME, grammar.CI_PLAN_NAME,
                                                  identity.IDENTITY_NAME])
        raw = identity.read_state_record(attempt.state, commands_build.PARTITIONS_NAME, max_bytes=limits.MIB)
        recorded = json.loads(raw)
        self.assertEqual(raw, canonical_json(recorded))
        self.assertEqual(recorded["envelope_sha256"], canonical_sha256(envelope))
        self.assertEqual([descriptor["artifact"]["id"] for descriptor in recorded["descriptors"]], [110, 111])
        self.assertEqual(attempt.budgets, [limits.MAX_CI_ASSEMBLE_REQUESTS])
        self.assertEqual(attempt.api.request_count, 13 + 2 * 2)
        self.assertEqual(attempt.api.mutations, [])

    def test_seventeen_targets_cost_forty_seven_requests(self) -> None:
        attempt = self.world(targets=17)
        self.assertEqual(attempt.command("assemble")[0], 0)
        # The source (4), the run, its jobs and its artifacts (3), then two per target, then the
        # same once more before the sealed root appears (6): the one description of the attempt
        # is also what the downloads are checked against.
        self.assertEqual(attempt.api.request_count, 13 + 2 * 17)
        self.assertLess(attempt.api.request_count, 60)
        self.assertEqual(len(verify_build_export(attempt.sealed_build, plan=attempt.plan)["native_reports"]), 17)

    def test_a_packaged_run_that_rebuilds_assembles_its_own_targets(self) -> None:
        attempt = self.world(mode="rebuilt", caller="packaged", producer="build", push=True)
        self.assertEqual(attempt.command("assemble")[0], 0)
        envelope = verify_build_export(attempt.sealed_build, plan=attempt.plan)
        self.assertEqual(envelope["producer"], attempt.producer("rebuilt"))
        self.assertEqual(envelope["producer"]["run_id"], 43)

    def test_a_missing_partition_a_failed_job_and_a_mixed_attempt_reject_before_any_download(self) -> None:
        cases = {
            "failed target": (lambda attempt: attempt.change_job(TARGET, conclusion="failure"),
                              f"required job '{TARGET}' has not finished as the graph expects"),
            "earlier plan job": (lambda attempt: attempt.change_job(PLAN, run_attempt=1),
                                 f"job '{PLAN}' ran in attempt 1, not in attempt 2: {RERUN}"),
            "carried-over plan job": (lambda attempt: attempt.rerun_failed_jobs(), f"job '{GUARD}' started before attempt 2 did, in an earlier attempt: {RERUN}"),
            "expired partition": (lambda attempt: attempt.set_artifact(111, expired=True),
                                  "artifact 'mb-ci-target--42--a2--target-02' has expired"),
            "extra partition": (lambda attempt: attempt.publish("target", "target-zz", b"x", artifact_id=150, job=TARGET),
                                "artifact 'mb-ci-target--42--a2--target-zz' is not one the plan expects"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.world()
                change(attempt)
                self.assert_rejected(attempt, "invalid-document", message)
                # At most the source, the run, its jobs and its artifacts; nothing is downloaded.
                self.assertLessEqual(attempt.api.request_count, 7)

    def test_a_run_that_moved_and_bytes_that_differ_publish_nothing(self) -> None:
        attempt = self.world()
        # GitHub still serves the record of attempt 2 once attempt 3 exists.
        attempt.api.add_run({**attempt.run, "run_attempt": 3, "run_started_at": "2026-10-07T10:30:00Z"},
                            attempts=[attempt.run])
        self.assert_rejected(attempt, "invalid-document", "a newer producer attempt exists")
        attempt = self.world()
        attempt.set_run(status="completed", conclusion="failure")
        self.assert_rejected(attempt, "invalid-document", "producer run is queued, failed or cancelled")
        attempt = self.world()
        attempt.archives[111] = b"X" + attempt.archives[111][1:]
        attempt.set_artifact(111)
        self.assert_rejected(attempt, "invalid-document", "download length or SHA-256 differs")
        attempt = self.world()
        attempt.api.add_response(f"/repos/{attempt.api.repository}/pulls/7", {**attempt.pull, "draft": True})
        self.assert_rejected(attempt, "invalid-document", "PR must be open and ready")

    def test_a_partition_must_carry_the_validation_record_of_its_own_target_run(self) -> None:
        def second(edit):
            return lambda target, document, files: edit(document, files) if target == "target-02" else None

        context = "validation record differs from protected execution/input context"
        cases = {
            "no record": (second(lambda document, files: document.clear()), "cannot read"),
            "the Build's hook": (second(lambda document, files: document.update(hook="verify_build", unit_id=None)),
                                 "reports do not cover the exact ordered protected native contracts"),
            "another attempt": (second(lambda document, files: document.update(run_attempt=1)), context),
            "another config": (second(lambda document, files: document.update(source_config_sha256="0" * 64)), context),
            "another input": (second(lambda document, files: document.update(input_sha256="0" * 64)), context),
            "report changed": (second(lambda document, files: files.update({"target-02.json": b'{"x":1}\n'})),
                               "native verification reports differ from exact byte inventory"),
            "stray file": (second(lambda document, files: files.update({"stray.json": b"{}\n"})),
                           "frozen export differs from its exact file inventory"),
        }
        for label, (change, message) in cases.items():
            with self.subTest(case=label):
                attempt = self.world(change=change)
                code, stdout, stderr = attempt.command("assemble")
                self.assertEqual((code, stdout), (2, ""))
                self.assertIn(message, stderr)
                self.assertFalse(attempt.sealed_build.exists())
                self.assertEqual([path for path in attempt.state.iterdir() if path.is_dir()], [])

    def test_the_sealed_root_is_never_replaced(self) -> None:
        attempt = self.world()
        attempt.sealed_build.mkdir()
        (attempt.sealed_build / "kept").write_bytes(b"earlier")
        code, _, stderr = attempt.command("assemble")
        self.assertEqual(code, 2, stderr)
        self.assertIn("the sealed Build root already exists", stderr)
        self.assertEqual([path.name for path in attempt.sealed_build.iterdir()], ["kept"])
        self.assertNotIn(commands_build.PARTITIONS_NAME, records(attempt.state))
        self.assertEqual(attempt.api.request_count, 0)

    def test_the_state_must_be_this_jobs_build_state(self) -> None:
        attempt = self.world(mode="rebuilt", caller="packaged", producer="packaged", push=True)
        self.assert_rejected(attempt, "invalid-document", "`ci assemble` is a step of a Build job")
        self.assertEqual(attempt.api.request_count, 0)
        for name, value, message in (("GITHUB_SHA", "9" * 40, "belongs to another repository or controller commit"),
                                     ("GITHUB_RUN_ATTEMPT", "0", "GITHUB_RUN_ATTEMPT must be a positive decimal"),
                                     ("GITHUB_RUN_ID", "", "GITHUB_RUN_ID must be a positive decimal")):
            with self.subTest(variable=name):
                attempt = self.world()
                code, _, stderr = attempt.command("assemble", environment={**attempt.environment, name: value})
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)
                self.assertEqual(attempt.api.request_count, 0)

    def test_the_plan_of_the_state_must_be_canonical_and_of_its_subject(self) -> None:
        other = copy.deepcopy(self.world().plan)
        other["identity"]["tested_tree"] = "9" * 40
        for label, raw, message in (
                ("another subject", lambda plan: canonical_json({**other, "plan_sha256": plan_sha256(other)}),
                 "the plan belongs to another subject"),
                ("not canonical", lambda plan: json.dumps(plan).encode(), "is not canonical JSON"),
                ("not a plan", lambda plan: canonical_json({"kind": "mod-base.build.plan"}), "missing required keys")):
            with self.subTest(case=label):
                attempt = self.world()
                state = attempt.directory / "other-state"
                identity.write_subject(state, attempt.record)
                identity.write_state_record(state, grammar.CI_PLAN_NAME, raw(attempt.plan))
                attempt.state = state
                code, _, stderr = attempt.command("assemble")
                self.assertEqual(code, 2)
                self.assertIn(message, stderr)
                self.assertEqual(attempt.api.request_count, 0)
        attempt = self.world()
        state = attempt.directory / "no-plan"
        identity.write_subject(state, attempt.record)
        attempt.state = state
        code, _, stderr = attempt.command("assemble")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("mod_base: ci-state: "), stderr)

    def test_the_request_budget_is_a_hard_bound(self) -> None:
        attempt = self.world(max_requests=12)
        code, _, stderr = attempt.command("assemble")
        self.assertEqual(code, 2)
        self.assertIn("request-budget", stderr)
        self.assertEqual(attempt.api.request_count, 12)
        self.assertFalse(attempt.sealed_build.exists())

    def test_unknown_flags_are_usage_errors(self) -> None:
        attempt = self.world()
        for flags in (("--output", "x"), ("--gate", "build")):
            with self.subTest(flags=flags):
                self.assertEqual(attempt.command("assemble", *flags)[0], 2)
                self.assertEqual(attempt.api.request_count, 0)


if __name__ == "__main__":
    unittest.main()
