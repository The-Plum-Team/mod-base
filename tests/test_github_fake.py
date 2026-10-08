"""``github.fake.FakeGitHub``: the in-memory API used by every unit's tests and ``conformance``.

It must behave like :class:`mod_base.github.api.GitHubApi` wherever a consumer can observe it:
the same public surface, request counting and budget, read-only refusals, 404 for anything
unseeded, pagination, and immutable served copies (Quick Skin ``FakeApi`` + Block Pops fakes)."""

from __future__ import annotations

import inspect
import unittest

from mod_base import KIT_REPOSITORY
from mod_base.errors import MbError
from mod_base.github import artifacts, runs
from mod_base.github.api import (
    ApiError,
    ApiNotFound,
    GitHubApi,
    InconsistentListing,
    ReadOnlyViolation,
    RequestBudgetExhausted,
)
from mod_base.github.fake import FakeGitHub
from mod_base.model import limits
from tests.helpers import COMMIT, CREATED_AT, E2E_WORKFLOW, REPOSITORY, TREE, h

RUNS = f"/repos/{REPOSITORY}/actions/runs"


def run(run_id: int, attempt: int = 1, **changes: object) -> dict[str, object]:
    return {"id": run_id, "run_attempt": attempt, "workflow_id": 55, "path": E2E_WORKFLOW, "created_at": CREATED_AT,
            "head_branch": "master", "head_sha": COMMIT, "event": "workflow_dispatch", "status": "completed",
            "conclusion": "success", **changes}


def artifact(artifact_id: int, name: str, run_id: int = 1, **changes: object) -> dict[str, object]:
    return {"id": artifact_id, "name": name, "created_at": CREATED_AT,
            "workflow_run": {"id": run_id, "head_branch": "master", "head_sha": COMMIT}, **changes}


class SurfaceTests(unittest.TestCase):
    def test_release_download_is_direct_bounded_and_independent_of_metadata(self) -> None:
        api = FakeGitHub(repository=REPOSITORY, max_requests=3)
        api.add_release_asset(REPOSITORY, 7, b"known")
        self.assertEqual(b"known", api.download_release_asset(REPOSITORY, 7, max_bytes=5))
        with self.assertRaises(ApiError):
            api.download_release_asset(REPOSITORY, 7, max_bytes=4)
        with self.assertRaises(ApiNotFound):
            api.download_release_asset("foreign/repo", 7, max_bytes=5)
        self.assertEqual(3, api.request_count)
        with self.assertRaises(RequestBudgetExhausted):
            api.download_release_asset(REPOSITORY, 7, max_bytes=5)

    def test_public_surface_matches_the_real_client(self) -> None:
        for name, member in inspect.getmembers(GitHubApi):
            if name.startswith("_"):
                continue
            with self.subTest(name=name):
                fake_member = getattr(FakeGitHub, name)
                if isinstance(member, property):
                    self.assertIsInstance(fake_member, property)
                else:
                    self.assertEqual(inspect.signature(member), inspect.signature(fake_member))

    def test_requests_are_counted_and_budgeted_like_the_client(self) -> None:
        api = FakeGitHub(repository=REPOSITORY, max_requests=2)
        api.add_run(run(1))
        api.get_json(f"{RUNS}/1")
        with self.assertRaises(ApiNotFound):
            api.get_json(f"{RUNS}/2")
        self.assertEqual(2, api.request_count)
        with self.assertRaises(RequestBudgetExhausted):
            api.get_json(f"{RUNS}/1")
        self.assertEqual(2, api.request_count)

    def test_invalid_paths_and_params_are_rejected_like_the_client(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        for path in ("repos/x", "/repos/../x", "/repos/x?y=1"):
            with self.subTest(path=path), self.assertRaises(MbError):
                api.get_json(path)
        with self.assertRaises(MbError):
            api.get_json(f"{RUNS}/1", params={"Bad": 1})
        api.add_run(run(1))
        with self.assertRaises(ApiError) as caught:
            api.get_json(f"{RUNS}/1", params={"exclude_pull_requests": "true"})
        self.assertEqual(422, caught.exception.status)
        self.assertEqual(1, api.request_count)

    def test_seeding_mistakes_are_programming_errors(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        for seed in (lambda: api.add_run({"id": 1}), lambda: api.add_run(run(1, 2), attempts=[run(1, 2)]),
                     lambda: api.add_run(run(1, 1), attempts=[run(2, 1)]), lambda: api.add_jobs(0, 1, []),
                     lambda: api.add_artifact({"id": 1}, b"x"), lambda: api.add_artifact(artifact(1, "a"), "x"),
                     lambda: api.add_file(COMMIT, "../x", b"x"), lambda: api.add_ref("v1.0.0", COMMIT)):
            with self.assertRaises((ValueError, MbError)):
                seed()
        with self.assertRaises(MbError):
            FakeGitHub(repository="not-a-repository")
        with self.assertRaises(MbError):
            api.add_response("/x", float("nan"))


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY, default_branch="main")

    def test_served_bodies_are_fresh_copies(self) -> None:
        self.api.add_run(run(1))
        served = self.api.get_json(f"{RUNS}/1")
        served["conclusion"] = "failure"
        self.assertEqual("success", self.api.get_json(f"{RUNS}/1")["conclusion"])

    def test_exact_responses_win_and_bind_their_params(self) -> None:
        self.api.add_run(run(1))
        self.api.add_response(f"{RUNS}/1", {"id": 1, "seeded": True})
        self.api.add_response(f"{RUNS}/1", {"id": 1, "paged": True}, params={"page": 2})
        self.assertTrue(self.api.get_json(f"{RUNS}/1")["seeded"])
        self.assertTrue(self.api.get_json(f"{RUNS}/1", params={"page": "2"})["paged"])
        with self.assertRaises(ApiNotFound):
            self.api.get_json("/repos/other/repo/anything")

    def test_repository_branches_runs_attempts_and_jobs(self) -> None:
        self.api.set_branch("main", COMMIT, TREE)
        self.assertEqual({"full_name": REPOSITORY, "default_branch": "main", "private": False, "visibility": "public"},
                         self.api.get_json(f"/repos/{REPOSITORY}"))
        self.assertEqual(["main"], [row["name"] for row in self.api.get_json(f"/repos/{REPOSITORY}/branches")])
        self.assertEqual(TREE, self.api.get_json(f"/repos/{REPOSITORY}/git/commits/{COMMIT}")["tree"]["sha"])
        self.api.add_run(run(9, 2, conclusion="failure"), attempts=[run(9, 1)])
        self.assertEqual("failure", self.api.get_json(f"{RUNS}/9")["conclusion"])
        self.assertEqual("success", runs.get_run_attempt(self.api, 9, 1)["conclusion"])
        self.api.add_jobs(9, 1, [{"name": "first"}])
        self.api.add_jobs(9, 2, [{"name": "second"}])
        listed = self.api.get_json(f"{RUNS}/9/jobs")
        self.assertEqual((1, ["second"]), (listed["total_count"], [job["name"] for job in listed["jobs"]]))
        every = self.api.get_json(f"{RUNS}/9/jobs", params={"filter": "all"})
        self.assertEqual(["first", "second"], [job["name"] for job in every["jobs"]])
        self.assertEqual({9}, {job["run_id"] for job in every["jobs"]})
        self.assertEqual(2, len({job["id"] for job in every["jobs"]}))
        with self.assertRaises(ApiNotFound):
            self.api.get_json(f"{RUNS}/9/attempts/3")

    def test_listings_page_like_the_api(self) -> None:
        for index in range(45):
            self.api.add_artifact(artifact(100 + index, "mb-promotion"), b"x")
        first = self.api.get_json(f"/repos/{REPOSITORY}/actions/artifacts")
        self.assertEqual((45, 30), (first["total_count"], len(first["artifacts"])))
        second = self.api.get_json(f"/repos/{REPOSITORY}/actions/artifacts", params={"per_page": 30, "page": 2})
        self.assertEqual(15, len(second["artifacts"]))
        self.assertEqual(45, len(self.api.paginate(f"/repos/{REPOSITORY}/actions/artifacts", field="artifacts",
                                                   max_items=100)))
        with self.assertRaises(ApiError):
            self.api.get_json(f"/repos/{REPOSITORY}/actions/artifacts", params={"per_page": 101})

    def test_contents_blobs_refs_tags_and_foreign_repositories(self) -> None:
        self.api.add_file(COMMIT, "site/mod-base.json", b"{}")
        record = self.api.get_json(f"/repos/{REPOSITORY}/contents/site/mod-base.json", params={"ref": COMMIT})
        self.assertEqual(("base64", 2), (record["encoding"], record["size"]))
        kit_commit, tag_object = h("kit", 40), h("tag", 40)
        self.api.add_ref("tags/v1.0.0", kit_commit, annotated_tag_sha=tag_object, repository=KIT_REPOSITORY)
        self.api.add_ref("heads/main", kit_commit, repository=KIT_REPOSITORY)
        ref = self.api.get_json(f"/repos/{KIT_REPOSITORY}/git/ref/tags/v1.0.0")
        self.assertEqual({"ref": "refs/tags/v1.0.0", "object": {"sha": tag_object, "type": "tag"}}, ref)
        self.assertEqual({"sha": tag_object, "tag": "v1.0.0", "object": {"sha": kit_commit, "type": "commit"}},
                         self.api.get_json(f"/repos/{KIT_REPOSITORY}/git/tags/{tag_object}"))
        self.assertEqual("commit", self.api.get_json(f"/repos/{KIT_REPOSITORY}/git/ref/heads/main")["object"]["type"])
        with self.assertRaises(ApiNotFound):
            self.api.get_json(f"/repos/{REPOSITORY}/git/ref/tags/v1.0.0")
        self.api.add_compare(kit_commit, "main", {"status": "ahead", "ahead_by": 1, "behind_by": 0},
                             repository=KIT_REPOSITORY)
        self.assertEqual("ahead", self.api.get_json(f"/repos/{KIT_REPOSITORY}/compare/{kit_commit}...main")["status"])
        self.assertEqual(0, self.api.get_json(f"/repos/{KIT_REPOSITORY}/compare/{kit_commit}...main",
                                              params={"per_page": 1})["behind_by"])  # pin.verify_released's read

    def test_rate_limit_counts_requests(self) -> None:
        self.api.get_json(f"/repos/{REPOSITORY}")
        snapshot = self.api.rate_limit_snapshot()
        self.assertEqual({"limit", "used", "remaining", "reset"}, set(snapshot))
        self.assertEqual(snapshot["limit"] - snapshot["used"], snapshot["remaining"])


class ListingSeamTests(unittest.TestCase):
    """The seams that reproduce GitHub's eventually consistent listings (the canary's refresh saw
    ``total_count 6 disagrees with 5 listed rows`` while sibling jobs uploaded caches)."""

    RUN_ARTIFACTS = f"{RUNS}/1/artifacts"

    def setUp(self) -> None:
        self.api = FakeGitHub(repository=REPOSITORY)
        for index in range(5):
            self.api.add_artifact(artifact(100 + index, f"mb-collected--k{index}"), b"x")

    def test_a_skewed_listing_reports_a_total_off_its_rows_for_the_next_responses(self) -> None:
        self.api.skew_listing(self.RUN_ARTIFACTS, responses=2)
        totals = [(listed["total_count"], len(listed["artifacts"]))
                  for listed in (self.api.get_json(self.RUN_ARTIFACTS, params={"name": "mb-collected--k1"}),
                                 self.api.get_json(self.RUN_ARTIFACTS), self.api.get_json(self.RUN_ARTIFACTS))]
        self.assertEqual([(2, 1), (6, 5), (5, 5)], totals, "any parameters; two responses, then consistent")
        self.api.skew_listing(self.RUN_ARTIFACTS, responses=1, offset=-9)
        self.assertEqual(0, self.api.get_json(self.RUN_ARTIFACTS)["total_count"], "never below zero")
        self.api.add_run(run(1))
        self.api.skew_listing(f"{RUNS}/1", responses=1)
        self.assertNotIn("total_count", self.api.get_json(f"{RUNS}/1"), "only a listing body is skewed")
        for options in ({"responses": 0}, {"responses": True}, {"responses": 1, "offset": 0},
                        {"responses": 1, "offset": True}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.api.skew_listing(self.RUN_ARTIFACTS, **options)  # type: ignore[arg-type]
        with self.assertRaises(MbError):
            self.api.skew_listing("../x", responses=1)

    def test_during_listing_changes_the_state_between_two_pages(self) -> None:
        for index in range(5, 150):
            self.api.add_artifact(artifact(100 + index, f"mb-collected--k{index}"), b"x")
        upload = artifact(999, "mb-cache--k0--" + COMMIT, created_at="2026-09-02T00:00:00Z")
        self.api.during_listing(self.RUN_ARTIFACTS, lambda: self.api.add_artifact(upload, b"x"))
        first = self.api.get_json(self.RUN_ARTIFACTS, params={"per_page": 100, "page": 1})
        second = self.api.get_json(self.RUN_ARTIFACTS, params={"per_page": 100, "page": 2})
        self.assertEqual((150, 151), (first["total_count"], second["total_count"]))
        self.assertEqual(151, self.api.get_json(self.RUN_ARTIFACTS)["total_count"], "the action ran once")
        for options in ({"after_pages": 0}, {"action": "not callable"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.api.during_listing(self.RUN_ARTIFACTS, **{"action": lambda: None, **options})  # type: ignore[arg-type]

    def test_the_fake_reads_an_inconsistent_listing_again_like_the_client(self) -> None:
        waited: list[float] = []
        api = FakeGitHub(repository=REPOSITORY, sleep=waited.append)
        for index in range(5):
            api.add_artifact(artifact(100 + index, f"mb-collected--k{index}"), b"x")
        api.skew_listing(self.RUN_ARTIFACTS, responses=2)
        self.assertEqual(5, len(artifacts.list_for_run(api, 1)))
        self.assertEqual(([2.0, 4.0], [2.0, 4.0], 3), (api.sleeps, waited, api.request_count))
        self.api.skew_listing(self.RUN_ARTIFACTS, responses=limits.LISTING_READ_ATTEMPTS)
        with self.assertRaisesRegex(InconsistentListing, "total_count 6 disagrees with 5 listed rows"):
            artifacts.list_for_run(self.api, 1)
        self.assertEqual(([2.0, 4.0, 8.0], limits.LISTING_READ_ATTEMPTS), (self.api.sleeps, self.api.request_count))

    def test_a_row_added_between_pages_restarts_the_whole_listing(self) -> None:
        for index in range(5, 150):
            self.api.add_artifact(artifact(100 + index, f"mb-collected--k{index}"), b"x")
        upload = artifact(999, "mb-cache--k0--" + COMMIT, created_at="2026-09-02T00:00:00Z")
        self.api.during_listing(self.RUN_ARTIFACTS, lambda: self.api.add_artifact(upload, b"x"))
        self.assertEqual(151, len(artifacts.list_for_run(self.api, 1)))
        self.assertEqual(([2.0], 4), (self.api.sleeps, self.api.request_count))

    def test_read_listing_matches_the_client(self) -> None:
        skewed = [True, False]

        def read() -> str:
            if skewed.pop(0):
                raise InconsistentListing("skewed", status=200, method="GET", path=self.RUN_ARTIFACTS)
            return "consistent"

        self.assertEqual("consistent", self.api.read_listing(read))
        self.assertEqual([2.0], self.api.sleeps)


class MutationTests(unittest.TestCase):
    def test_read_only_fake_refuses_mutations_without_counting(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        api.add_artifact(artifact(1, "mb-promotion"), b"x")
        with self.assertRaises(ReadOnlyViolation):
            api.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        with self.assertRaises(ReadOnlyViolation):
            api.post_json(f"/repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches", {"ref": "master"})
        self.assertEqual((0, [], []), (api.request_count, api.deleted_artifact_ids, api.mutations))

    def test_writable_fake_records_dispatches_and_exact_deletions(self) -> None:
        api = FakeGitHub(repository=REPOSITORY, writable=True)
        api.add_artifact(artifact(1, "mb-promotion"), b"x")
        dispatch = f"/repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches"
        self.assertIsNone(api.post_json(dispatch, {"ref": "master", "inputs": {"operation": "deploy"}}))
        api.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        with self.assertRaises(ApiNotFound):
            api.delete(f"/repos/{REPOSITORY}/actions/artifacts/1")
        with self.assertRaises(ApiNotFound):
            api.get_json(f"/repos/{REPOSITORY}/actions/artifacts/1")
        with self.assertRaises(ApiNotFound):
            api.post_json(f"/repos/{REPOSITORY}/issues", {"title": "x"})
        self.assertEqual([1], api.deleted_artifact_ids)
        self.assertEqual([("POST", dispatch, {"inputs": {"operation": "deploy"}, "ref": "master"}),
                          ("DELETE", f"/repos/{REPOSITORY}/actions/artifacts/1", None)], api.mutations)

    def test_downloads_cost_two_requests_and_honour_expiry_and_bounds(self) -> None:
        api = FakeGitHub(repository=REPOSITORY)
        api.add_artifact(artifact(1, "mb-promotion"), b"zip-bytes")
        self.assertEqual(b"zip-bytes", api.download(f"/repos/{REPOSITORY}/actions/artifacts/1/zip", max_bytes=9))
        self.assertEqual(2, api.request_count)
        with self.assertRaisesRegex(ApiError, "8-byte bound"):
            api.download(f"/repos/{REPOSITORY}/actions/artifacts/1/zip", max_bytes=8)
        api.add_artifact(artifact(2, "mb-promotion", expired=True), b"zip-bytes")
        with self.assertRaises(ApiError) as caught:
            api.download(f"/repos/{REPOSITORY}/actions/artifacts/2/zip", max_bytes=9)
        self.assertEqual(410, caught.exception.status)
        with self.assertRaises(ApiNotFound):
            api.download(f"/repos/{REPOSITORY}/actions/artifacts/3/zip", max_bytes=9)


if __name__ == "__main__":
    unittest.main()
