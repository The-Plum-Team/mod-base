"""Future kit admission reads real candidate/kit commits and only fakes GitHub."""

from __future__ import annotations

import copy
import re
from pathlib import Path

from mod_base import runtime
from mod_base.build_ci import candidate_kit, planning
from mod_base.errors import MbError
from mod_base.model.canonical import canonical_sha256
from mod_base.pin import Pin, STAGED_LOCK, kit_tree_digest
from tests import ci_future_kit, ci_lifecycle_fixture as fixture, ci_mod_harness as h
from tests.test_ci_lifecycle_candidate import CandidateCase


class CandidateKitAdmissionTests(CandidateCase):
    def candidate(self, pin: Pin):
        tested = h.materialize(self.temporary / "tested")
        (tested / ".github/workflows/kit.yml").write_text(
            f"jobs:\n  kit:\n    uses: The-Plum-Team/mod-base/.github/workflows/build.yml@{pin.sha} # {pin.version}\n",
            encoding="utf-8", newline="\n")
        return self.begin(tested)

    def test_unchanged_pin_costs_no_admission_requests(self) -> None:
        job = self.candidate(Pin(h.KIT_SHA, "v" + h.mod_base.__version__, ()))
        before = self.api.request_count
        self.assertIsNone(candidate_kit.planned_pin(self.checkout, job.subject, api=self.api))
        self.assertEqual(self.api.request_count, before)

    def test_changed_pin_is_admitted_in_three_requests_and_bound_into_the_plan_hash(self) -> None:
        pin = Pin("4" * 40, "v1.0.4", ())
        job = self.candidate(pin)
        ci_future_kit.release(self.api, pin)
        before = self.api.request_count
        admitted = candidate_kit.planned_pin(self.checkout, job.subject, api=self.api)
        self.assertEqual(self.api.request_count - before, 3)
        plan = copy.deepcopy(self.plan(job))
        old_hash = plan["plan_sha256"]
        plan["candidate_kit"] = admitted
        plan["plan_sha256"] = canonical_sha256({key: value for key, value in plan.items() if key != "plan_sha256"})
        self.assertNotEqual(old_hash, plan["plan_sha256"])
        planning.require_plan(plan, subject=job.subject, expected_sha256=plan["plan_sha256"])
        admitted_hash = plan["plan_sha256"]
        plan["candidate_kit"]["sha"] = "6" * 40
        with self.assertRaisesRegex(MbError, "does not bind this plan"):
            planning.require_plan(plan, subject=job.subject, expected_sha256=admitted_hash)
        plan["plan_sha256"] = canonical_sha256({key: value for key, value in plan.items() if key != "plan_sha256"})
        with self.assertRaisesRegex(MbError, "another plan"):
            planning.require_plan(plan, subject=job.subject, expected_sha256=admitted_hash)

    def test_wrong_tag_peel_rejects(self) -> None:
        pin = Pin("4" * 40, "v1.0.4", ())
        job = self.candidate(pin)
        ci_future_kit.release(self.api, pin)
        self.api.add_ref("tags/v1.0.4", "6" * 40, repository="The-Plum-Team/mod-base")
        with self.assertRaisesRegex(MbError, "tag v1.0.4"):
            candidate_kit.planned_pin(self.checkout, job.subject, api=self.api)

    def test_commit_outside_main_rejects(self) -> None:
        pin = Pin("4" * 40, "v1.0.4", ())
        job = self.candidate(pin)
        ci_future_kit.release(self.api, pin)
        self.api.add_compare(pin.sha, "main", {"status": "diverged", "behind_by": 1, "ahead_by": 1},
                             repository="The-Plum-Team/mod-base")
        with self.assertRaisesRegex(MbError, "not reachable"):
            candidate_kit.planned_pin(self.checkout, job.subject, api=self.api)

    def test_older_released_pin_is_allowed_only_as_candidate_data(self) -> None:
        pin = Pin("4" * 40, "v0.9.2", ())
        job = self.candidate(pin)
        ci_future_kit.release(self.api, pin)
        self.assertEqual(candidate_kit.planned_pin(self.checkout, job.subject, api=self.api),
                         {"sha": pin.sha, "version": "0.9.2"})
        self.assertEqual(job.subject["kit"]["sha"], h.KIT_SHA)

    def test_future_checkout_must_match_its_commit_and_own_digest_literal(self) -> None:
        future = self.temporary / "future"
        pin, digest = ci_future_kit.future_kit(runtime.kit_root(), future)
        self.assertEqual(candidate_kit.verify_future_checkout(future, pin), digest)
        with self.assertRaisesRegex(MbError, "checkout commit differs"):
            candidate_kit.verify_future_checkout(future, Pin("4" * 40, pin.version, ()))
        (future / "src/mod_base/__init__.py").write_bytes(b"raise RuntimeError('never execute future kit')\n")
        fixture.git(future, "add", "-A")
        fixture.git(future, "commit", "-q", "-m", "bad digest")
        bad = Pin(fixture.git(future, "rev-parse", "HEAD"), pin.version, ())
        with self.assertRaisesRegex(MbError, "future kit tree differs from its own digest literal"):
            candidate_kit.verify_future_checkout(future, bad)

    def test_incompatible_digest_version_fails_with_compatibility_first_message(self) -> None:
        future = self.temporary / "future"
        pin, _ = ci_future_kit.future_kit(runtime.kit_root(), future)
        workflow = future / ".github/workflows/build.yml"
        workflow.write_text(workflow.read_text(encoding="utf-8").replace("kit-digest-v1", "kit-digest-v2"),
                            encoding="utf-8", newline="\n")
        fixture.git(future, "add", "-A")
        fixture.git(future, "commit", "-q", "-m", "new digest format")
        bad = Pin(fixture.git(future, "rev-parse", "HEAD"), pin.version, ())
        with self.assertRaisesRegex(MbError, "compatibility-first release"):
            candidate_kit.verify_future_checkout(future, bad)

    def test_unknown_staged_lock_format_is_not_guessed(self) -> None:
        future = self.temporary / "future"
        pin, _ = ci_future_kit.future_kit(runtime.kit_root(), future)
        (future / STAGED_LOCK).write_bytes(b'{"lock_version":2}\n')
        workflow = future / ".github/workflows/build.yml"
        workflow.write_text(re.sub(r'sha256:[0-9a-f]{64}', kit_tree_digest(future),
                                   workflow.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")
        fixture.git(future, "add", "-A")
        fixture.git(future, "commit", "-q", "-m", "new lock format")
        bad = Pin(fixture.git(future, "rev-parse", "HEAD"), pin.version, ())
        with self.assertRaisesRegex(MbError, "compatibility-first release"):
            candidate_kit.verify_future_checkout(future, bad)
