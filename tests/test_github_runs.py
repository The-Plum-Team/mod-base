"""``github.runs``: exact run provenance (Block Pops ``_validate_run``/``_historical_run``/
``_run_order`` plus Quick Skin owner checks), the ``referenced_workflows`` kit-SHA parser of the
caller's ``verify-kit`` binding (SPEC §1.2 step 3), bounded run listings and ``RunRecord``s."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Any

from mod_base import KIT_REPOSITORY
from mod_base.errors import MbError
from mod_base.github import runs
from mod_base.github.api import ApiNotFound, RequestBudgetExhausted
from mod_base.github.fake import FakeGitHub
from mod_base.model import documents
from tests.helpers import COMMIT, CREATED_AT, E2E_WORKFLOW, REPOSITORY, h, run_claim

KIT_SHA = h("kit", 40)
OTHER_SHA = h("other-kit", 40)
PAGES = ".github/workflows/pages.yml"


def run(run_id: int = 101, **changes: Any) -> dict[str, Any]:
    value = {
        "id": run_id,
        "run_attempt": 1,
        "workflow_id": 55,
        "name": "Packaged E2E",
        "display_title": f"Packaged E2E / {COMMIT}",
        "path": E2E_WORKFLOW,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_branch": "master",
        "head_sha": COMMIT,
        "created_at": CREATED_AT,
        "head_repository": {"full_name": REPOSITORY},
        "repository": {"full_name": REPOSITORY},
    }
    value.update(changes)
    return value


def referenced(*paths_and_shas: tuple[str, str], ref: Any = "refs/heads/main") -> dict[str, Any]:
    return {"id": 900, "path": PAGES, "referenced_workflows": [{"path": path, "sha": sha, "ref": ref}
                                                              for path, sha in paths_and_shas]}


def kit_entry(workflow: str = "publish.yml", sha: str = KIT_SHA, *, pinned: str | None = None) -> tuple[str, str]:
    return f"{KIT_REPOSITORY}/.github/workflows/{workflow}@{pinned or sha}", sha


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY)

    def test_get_run_requires_the_exact_run(self) -> None:
        self.api.add_run(run(101))
        self.assertEqual(101, runs.get_run(self.api, 101)["id"])
        self.api.add_response(f"/repos/{REPOSITORY}/actions/runs/102", run(103))
        with self.assertRaisesRegex(MbError, "names another run"):
            runs.get_run(self.api, 102)
        self.api.add_response(f"/repos/{REPOSITORY}/actions/runs/104", [run(104)])
        with self.assertRaisesRegex(MbError, "malformed"):
            runs.get_run(self.api, 104)
        with self.assertRaises(ApiNotFound):
            runs.get_run(self.api, 105)
        for bad in (0, -1, True, "101", 2**63):
            with self.subTest(run_id=bad), self.assertRaises(MbError):
                runs.get_run(self.api, bad)  # type: ignore[arg-type]

    def test_historical_attempt_endpoint_must_answer_that_attempt(self) -> None:
        self.api.add_run(run(101, run_attempt=3, conclusion="failure"),
                         attempts=[run(101, run_attempt=1), run(101, run_attempt=2, head_sha=h("x", 40))])
        self.assertEqual("success", runs.get_run_attempt(self.api, 101, 1)["conclusion"])
        self.assertEqual("failure", runs.get_run_attempt(self.api, 101, 3)["conclusion"])
        self.api.add_response(f"/repos/{REPOSITORY}/actions/runs/101/attempts/4", run(101, run_attempt=3))
        with self.assertRaisesRegex(MbError, "stale"):
            runs.get_run_attempt(self.api, 101, 4)
        self.api.add_response(f"/repos/{REPOSITORY}/actions/runs/101/attempts/5", run(101, run_attempt=5,
                                                                                       workflow_id=True))
        with self.assertRaisesRegex(MbError, "workflow_id"):
            runs.get_run_attempt(self.api, 101, 5)
        with self.assertRaises(ApiNotFound):
            runs.get_run_attempt(self.api, 101, 6)
        with self.assertRaises(MbError):
            runs.get_run_attempt(self.api, 101, 0)

    def test_wait_for_completion_polls_a_bounded_number_of_times(self) -> None:
        sleeps: list[float] = []
        self.api.add_run(run(101, status="in_progress", conclusion=None))
        with self.assertRaisesRegex(MbError, "did not complete within 3"):
            runs.wait_for_completion(self.api, 101, attempts=3, interval=0.5, sleep=sleeps.append)
        self.assertEqual(([0.5, 0.5], 3), (sleeps, self.api.request_count))

        class Completing(FakeGitHub):
            def get_json(inner, path: str, *, params: Any = None) -> Any:  # noqa: N805
                value = FakeGitHub.get_json(inner, path, params=params)
                if inner.request_count == 2:
                    inner.add_run(run(101))
                return value

        completing = Completing(repository=REPOSITORY)
        completing.add_run(run(101, status="queued", conclusion=None))
        sleeps.clear()
        self.assertEqual("completed", runs.wait_for_completion(completing, 101, attempts=5, interval=0,
                                                               sleep=sleeps.append)["status"])
        self.assertEqual(3, completing.request_count)
        for bad in ({"attempts": 0}, {"interval": -1}, {"interval": 61}, {"interval": True}):
            with self.subTest(bad=bad), self.assertRaises(MbError):
                runs.wait_for_completion(self.api, 101, sleep=sleeps.append, **bad)


class ValidateRunTests(unittest.TestCase):
    OPTIONS = {"repository": REPOSITORY, "workflow_path": E2E_WORKFLOW, "events": {"workflow_dispatch", "schedule"}}

    def test_exact_provenance_passes(self) -> None:
        runs.validate_run(run(), **self.OPTIONS, head_branch="master", head_sha=COMMIT, workflow_id=55,
                          display_title=f"Packaged E2E / {COMMIT}")
        runs.validate_run(run(status="in_progress", conclusion=None), **self.OPTIONS, require_success=False)

    def test_every_difference_is_rejected(self) -> None:
        cases = {
            "path": (run(path=".github/workflows/other.yml"), {}),
            "event": (run(event="pull_request_target"), {}),
            "event type": (run(event=["workflow_dispatch"]), {}),
            "fork": (run(head_repository={"full_name": "attacker/Quick-Skin-Mod"}), {}),
            "no head repository": (run(head_repository=None), {}),
            "branch": (run(head_branch="feature"), {"head_branch": "master"}),
            "sha": (run(head_sha=h("other", 40)), {"head_sha": COMMIT}),
            "workflow id": (run(workflow_id=56), {"workflow_id": 55}),
            "workflow id bool": (run(workflow_id=True), {"workflow_id": 1}),
            "display title": (run(display_title="Packaged E2E / other"), {"display_title": f"Packaged E2E / {COMMIT}"}),
            "status": (run(status="in_progress"), {}),
            "conclusion": (run(conclusion="failure"), {}),
            "cancelled": (run(conclusion="cancelled"), {}),
        }
        for label, (value, options) in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(MbError, "provenance"):
                runs.validate_run(value, **self.OPTIONS, **options)

    def test_invalid_expectations_are_usage_errors(self) -> None:
        for options in ({"events": "workflow_dispatch"}, {"events": set()}, {"repository": "bad"},
                        {"workflow_path": "pages.yml"}, {"head_sha": "ABC"}):
            with self.subTest(options=options), self.assertRaises(MbError):
                runs.validate_run(run(), **{**self.OPTIONS, **options})
        with self.assertRaises(MbError):
            runs.validate_run([run()], **self.OPTIONS)  # type: ignore[arg-type]


class RunOrderTests(unittest.TestCase):
    def test_order_is_created_at_then_id_then_attempt(self) -> None:
        values = [run(3, created_at="2026-09-25T10:00:00Z"), run(1, created_at="2026-09-25T11:00:00Z"),
                  run(2, created_at="2026-09-25T10:00:00Z", run_attempt=2), run(2, created_at="2026-09-25T10:00:00Z")]
        ordered = sorted(values, key=runs.run_order)
        self.assertEqual([(2, 1), (2, 2), (3, 1), (1, 1)], [(item["id"], item["run_attempt"]) for item in ordered])
        self.assertEqual((datetime(2026, 9, 25, 10, tzinfo=timezone.utc), 3, 1), runs.run_order(values[0]))

    def test_malformed_shapes_are_rejected(self) -> None:
        for changes in ({"id": 0}, {"id": True}, {"id": "1"}, {"run_attempt": 0}, {"run_attempt": None},
                        {"created_at": "2026-09-25T10:00:00"}, {"created_at": "2026-09-25T10:00:00+00:00"},
                        {"created_at": "2026-02-30T10:00:00Z"}, {"created_at": None}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                runs.run_order(run(**changes))


class ReferencedKitShaTests(unittest.TestCase):
    def test_the_single_pinned_kit_sha_is_returned(self) -> None:
        value = referenced(kit_entry("publish.yml"), kit_entry("finalize.yml"),
                           (f"{REPOSITORY}/.github/workflows/local.yml@{h('local', 40)}", h("local", 40)))
        self.assertEqual(KIT_SHA, runs.referenced_kit_sha(value))

    def test_verified_same_repository_shape_needs_a_kit_entry(self) -> None:
        # V1: a run that only made same-repo calls lists them, but names no kit workflow.
        same_repo = referenced((f"{REPOSITORY}/.github/workflows/build-matrix.yml@{COMMIT}", COMMIT),
                               ref="refs/heads/master")
        with self.assertRaisesRegex(MbError, "0 distinct kit SHAs"):
            runs.referenced_kit_sha(same_repo)
        with self.assertRaisesRegex(MbError, "0 distinct kit SHAs"):
            runs.referenced_kit_sha(referenced())

    def test_two_kit_shas_or_an_unpinned_entry_are_rejected(self) -> None:
        cases = {
            "two shas": referenced(kit_entry("publish.yml"), kit_entry("rotate.yml", OTHER_SHA)),
            "path pins another sha": referenced(kit_entry(pinned=OTHER_SHA)),
            "path pins a branch": referenced((f"{KIT_REPOSITORY}/.github/workflows/publish.yml@refs/heads/main",
                                              KIT_SHA)),
            "path pins a tag": referenced((f"{KIT_REPOSITORY}/.github/workflows/publish.yml@v1.0.0", KIT_SHA)),
            "no ref": referenced((f"{KIT_REPOSITORY}/.github/workflows/publish.yml", KIT_SHA)),
            "uppercase sha": referenced(kit_entry(sha=KIT_SHA.upper())),
            "short sha": referenced(kit_entry(sha=KIT_SHA[:12])),
            "sha not text": referenced((f"{KIT_REPOSITORY}/.github/workflows/publish.yml@{KIT_SHA}",
                                        None)),  # type: ignore[arg-type]
            "nested workflow": referenced((f"{KIT_REPOSITORY}/.github/workflows/sub/publish.yml@{KIT_SHA}", KIT_SHA)),
            "double at": referenced((f"{KIT_REPOSITORY}/.github/workflows/p.yml@x@{KIT_SHA}", KIT_SHA)),
            "other case": referenced((f"the-plum-team/MOD-BASE/.github/workflows/publish.yml@{KIT_SHA}", KIT_SHA)),
            "ref not text": referenced(kit_entry(), ref=5),
        }
        for label, value in cases.items():
            with self.subTest(label=label), self.assertRaisesRegex(MbError, "kit|referenced"):
                runs.referenced_kit_sha(value)

    def test_malformed_lists_are_rejected(self) -> None:
        for value in ({"id": 1}, {"referenced_workflows": None}, {"referenced_workflows": {}},
                      {"referenced_workflows": ["path"]}, {"referenced_workflows": [{"sha": KIT_SHA}]},
                      {"referenced_workflows": [dict(zip(("path", "sha"), kit_entry()))] * 101}, []):
            with self.subTest(value=str(value)[:60]), self.assertRaises(MbError) as caught:
                runs.referenced_kit_sha(value)  # type: ignore[arg-type]
            self.assertEqual("kit-binding", caught.exception.reason)

    def test_a_similarly_named_repository_is_not_the_kit(self) -> None:
        value = referenced((f"{KIT_REPOSITORY}-fork/.github/workflows/publish.yml@{OTHER_SHA}", OTHER_SHA),
                           (f"The-Plum-Team/mod-base2/.github/workflows/publish.yml@{OTHER_SHA}", OTHER_SHA),
                           kit_entry())
        self.assertEqual(KIT_SHA, runs.referenced_kit_sha(value))
        canary = referenced((f"The-Plum-Team/mod-base-canary/.github/workflows/x.yml@{OTHER_SHA}", OTHER_SHA))
        self.assertEqual(OTHER_SHA, runs.referenced_kit_sha(canary, kit_repository="The-Plum-Team/mod-base-canary"))
        with self.assertRaises(MbError):
            runs.referenced_kit_sha(canary, kit_repository="not a repository")


class WorkflowRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY)

    def seed(self, count: int, api: FakeGitHub | None = None) -> list[dict[str, Any]]:
        seeded = [run(1000 + index, created_at=f"2026-09-{1 + index // 100:02d}T{index % 24:02d}:{index % 60:02d}:00Z")
                  for index in range(count)]
        for value in seeded:
            (api or self.api).add_run(value)
        return seeded

    def test_lists_every_page_newest_first_with_filters(self) -> None:
        self.seed(150)
        self.api.add_run(run(5000, head_branch="feature", created_at="2026-09-30T00:00:00Z"))
        self.api.add_run(run(5001, path=".github/workflows/other.yml", created_at="2026-09-30T00:00:00Z"))
        listed = runs.workflow_runs(self.api, E2E_WORKFLOW, branch="master", event="workflow_dispatch",
                                    status="success", head_sha=COMMIT)
        self.assertEqual(150, len(listed))
        self.assertEqual(sorted(listed, key=runs.run_order, reverse=True), listed)
        self.assertEqual(2, self.api.request_count)
        self.assertEqual(151, len(runs.workflow_runs(self.api, E2E_WORKFLOW)))

    def test_beyond_max_items_the_newest_are_returned(self) -> None:
        seeded = self.seed(250)
        listed = runs.workflow_runs(self.api, E2E_WORKFLOW, max_items=150)
        self.assertEqual(150, len(listed))
        newest = sorted(seeded, key=runs.run_order, reverse=True)[:150]
        self.assertEqual([item["id"] for item in newest], [item["id"] for item in listed])

    def test_inconsistent_or_foreign_listings_are_rejected(self) -> None:
        path = f"/repos/{REPOSITORY}/actions/workflows/on-demand-e2e.yml/runs"
        cases = {
            "total mismatch": {"total_count": 2, "workflow_runs": [run(1)]},
            "negative total": {"total_count": -1, "workflow_runs": []},
            "rows not objects": {"total_count": 1, "workflow_runs": ["run"]},
            "foreign workflow": {"total_count": 1, "workflow_runs": [run(1, path=".github/workflows/x.yml")]},
            "outside branch": {"total_count": 1, "workflow_runs": [run(1, head_branch="feature")]},
            "duplicate attempt": {"total_count": 2, "workflow_runs": [run(1), run(1)]},
            "one run twice": {"total_count": 2, "workflow_runs": [run(5, run_attempt=2), run(5, run_attempt=1)]},
            "malformed order": {"total_count": 1, "workflow_runs": [run(1, created_at="yesterday")]},
            "not a listing": [run(1)],
        }
        for label, payload in cases.items():
            with self.subTest(label=label):
                api = FakeGitHub(repository=REPOSITORY)
                api.add_response(path, payload, params={"branch": "master", "per_page": 100, "page": 1})
                with self.assertRaises(MbError):
                    runs.workflow_runs(api, E2E_WORKFLOW, branch="master")

    def test_unhashable_row_values_are_rejections_not_type_errors(self) -> None:
        path = f"/repos/{REPOSITORY}/actions/workflows/on-demand-e2e.yml/runs"
        for status, changes in (("completed", {"status": ["completed"]}),
                                ("success", {"conclusion": {"success": True}}),
                                ("success", {"status": ["completed"], "conclusion": ["success"]})):
            with self.subTest(changes=changes):
                api = FakeGitHub(repository=REPOSITORY)
                api.add_response(path, {"total_count": 1, "workflow_runs": [run(1, **changes)]},
                                 params={"status": status, "per_page": 100, "page": 1})
                with self.assertRaisesRegex(MbError, "outside its filters"):
                    runs.workflow_runs(api, E2E_WORKFLOW, status=status)

    def test_invalid_filters_fail_before_any_request(self) -> None:
        for options in ({"status": "done"}, {"branch": "../x"}, {"head_sha": "abc"}, {"event": "Push"},
                        {"max_items": 0}):
            with self.subTest(options=options), self.assertRaises(MbError):
                runs.workflow_runs(self.api, E2E_WORKFLOW, **options)
        with self.assertRaises(MbError):
            runs.workflow_runs(self.api, "on-demand-e2e.yml")
        self.assertEqual(0, self.api.request_count)

    def test_listing_respects_the_request_budget(self) -> None:
        api = FakeGitHub(repository=REPOSITORY, max_requests=2)
        self.seed(250, api)
        with self.assertRaises(RequestBudgetExhausted):
            runs.workflow_runs(api, E2E_WORKFLOW)


class RunRecordTests(unittest.TestCase):
    def test_builds_a_validated_record_from_the_run_and_its_claim(self) -> None:
        record = runs.run_record(run(101), run_claim(101))
        self.assertEqual({**run_claim(101), "event": "workflow_dispatch", "created_at": CREATED_AT,
                          "conclusion": "success", "head_sha": COMMIT,
                          "display_title": f"Packaged E2E / {COMMIT}"}, record)
        documents.own_run_record(record, "$")
        without_title = {key: value for key, value in run(101).items() if key != "display_title"}
        self.assertNotIn("display_title", runs.run_record(without_title, run_claim(101)))

    def test_claim_must_name_this_successful_run(self) -> None:
        cases = {
            "other run": (run(102), run_claim(101)),
            "other attempt": (run(101, run_attempt=2), run_claim(101)),
            "other workflow": (run(101, path=PAGES), run_claim(101)),
            "failed": (run(101, conclusion="failure"), run_claim(101)),
            "incomplete": (run(101, status="in_progress"), run_claim(101)),
            "other controller head": (run(101, head_sha=h("merge", 40)), run_claim(101)),
            "other controller branch": (run(101, head_branch="feature"), run_claim(101)),
            "invalid claim": (run(101), {**run_claim(101), "commit": "short"}),
            "claim extra key": (run(101), {**run_claim(101), "event": "push"}),
            "invalid event": (run(101, event="Push!"), run_claim(101)),
            "invalid title": (run(101, display_title="line\nbreak"), run_claim(101)),
        }
        for label, (value, claim) in cases.items():
            with self.subTest(label=label), self.assertRaises(MbError):
                runs.run_record(value, claim)

    def test_only_a_delegated_tested_run_may_have_another_head(self) -> None:
        merge = h("tested-merge", 40)
        tested = run(300, head_sha=h("pr-head", 40), head_branch="feature/x")
        claim = {**run_claim(300, commit=merge), "branch": "master", "controller_branch": "master"}
        with self.assertRaisesRegex(MbError, "claimed controller"):
            runs.run_record(tested, claim)
        record = runs.run_record(tested, claim, require_controller_head=False)
        self.assertEqual(h("pr-head", 40), record["head_sha"])
        documents.run_record(record, "$")


if __name__ == "__main__":
    unittest.main()
