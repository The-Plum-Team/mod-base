"""A consumer that selected no generation requests a full one, once, for the live commit (KQ9-4).

D-Q9-b: a late consumer gets a full shared generation on request, never a native fallback. The
fake GitHub holds the default branch, the managed callers' runs for the commit and records every
dispatch; a ``sleep`` of the test lets the dispatched Build run appear. ``generation.request_generation``
is called directly and ``ci request-generation`` through the real ``ci`` parser on a real state
directory.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from mod_base import cli
from mod_base.build_ci import commands, generation, identity, reuse, scope
from mod_base.errors import MbError
from mod_base.github.fake import FakeGitHub
from mod_base.model import limits
from mod_base.workflow import CI_CALLER_WORKFLOWS

REPOSITORY = "example/mod"
BRANCH = "master"
COMMIT = "a" * 40
NEWER = "b" * 40
TREE = "c" * 40
BUILD_DISPATCH = f"/repos/{REPOSITORY}/actions/workflows/mod-base-build.yml/dispatches"
PACKAGED_DISPATCH = f"/repos/{REPOSITORY}/actions/workflows/mod-base-packaged-e2e.yml/dispatches"


def subject(**changes) -> dict:
    """The subject ``ci subject --producer generation`` writes for the live default-branch commit."""

    return {"repository": REPOSITORY, "source_repository": REPOSITORY, "pr_number": None, "head_sha": COMMIT,
            "head_branch": BRANCH, "base_sha": COMMIT, "base_branch": BRANCH, "controller_sha": COMMIT,
            "tested_sha": COMMIT, **changes}


def run(run_id: int, producer: str = "build", *, event: str = "push", status: str = "completed",
        conclusion: str | None = "success", created_at: str = "2026-10-05T10:00:00Z") -> dict:
    return {"id": run_id, "name": producer, "path": CI_CALLER_WORKFLOWS[producer], "event": event, "status": status,
            "conclusion": conclusion, "head_branch": BRANCH, "head_sha": COMMIT,
            "head_repository": {"full_name": REPOSITORY}, "repository": {"full_name": REPOSITORY},
            "run_attempt": 1, "created_at": created_at, "run_started_at": created_at, "updated_at": created_at,
            "referenced_workflows": []}


class World:
    def __init__(self, *runs: dict, writable: bool = True) -> None:
        self.hub = FakeGitHub(repository=REPOSITORY, writable=writable)
        self.hub.set_branch(BRANCH, COMMIT, TREE)
        for item in runs:
            self.hub.add_run(item)
        self.sleeps: list[float] = []
        self.appear: dict | None = run(900, event="workflow_dispatch", status="queued", conclusion=None,
                                       created_at="2026-10-12T22:00:00Z")
        self.appear_after = 2

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.appear is not None and len(self.sleeps) == self.appear_after:
            self.hub.add_run(self.appear)

    def request(self, reason: str = "ci-generation-evidence-unavailable", **changes) -> str:
        return generation.request_generation(self.hub, subject=subject(**changes), reason=reason, sleep=self.sleep)

    def dispatches(self) -> list[tuple[str, dict]]:
        return [(path, payload) for method, path, payload in self.hub.mutations if method == "POST"]

    def artifact(self, artifact_id: int, run_id: int, *, expired: bool = False) -> None:
        self.hub.add_artifact({"id": artifact_id, "name": f"mb-ci-{artifact_id}", "expired": expired,
                               "created_at": "2026-10-05T10:00:00Z", "expires_at": "2026-10-12T10:00:00Z",
                               "workflow_run": {"id": run_id, "head_sha": COMMIT, "head_branch": BRANCH}}, b"zip")


class RequestTests(unittest.TestCase):
    def test_an_expired_or_absent_generation_of_the_live_head_is_requested_build_first(self) -> None:
        for reason, runs in (("ci-generation-evidence-unavailable", (run(10), run(11, "packaged"))),
                             ("ci-generation-absent", ())):
            world = World(*runs)
            with self.subTest(reason=reason), mock.patch.object(world.hub, "post_json",
                                                                wraps=world.hub.post_json) as post:
                self.assertEqual(world.request(reason), "requested")
                # The Build caller, then (once its new run is listed) the packaged caller, on the branch.
                self.assertEqual(world.dispatches(), [(BUILD_DISPATCH, {"ref": BRANCH}),
                                                      (PACKAGED_DISPATCH, {"ref": BRANCH})])
                # Each dispatch is sent once: an answer that may hide an accepted dispatch is not retried.
                self.assertEqual([call.kwargs for call in post.call_args_list], [{"replay": False}] * 2)
                self.assertEqual(world.sleeps, [limits.CI_REQUEST_GENERATION_POLL_SECONDS] * 2)
                self.assertLessEqual(world.hub.request_count, limits.MAX_CI_REQUEST_GENERATION_REQUESTS)
                # Two listings until the run appeared, and the branch read again before the packaged dispatch.
                self.assertEqual(world.hub.request_count, 10 + 2)

    def test_a_failed_generation_is_never_requested(self) -> None:
        # A failure is the commit's result (a rate limit, an operator's cancellation): an operator
        # re-runs it, and the commit's one request is not spent on it within the same hour.
        world = World(run(10, conclusion="failure"), run(11, "packaged"))
        for reason in ("ci-generation-failed", "ci-generation-absent", "ci-generation-evidence-unavailable"):
            with self.subTest(reason=reason), self.assertRaises(MbError):
                world.request(reason)
        self.assertNotIn("ci-generation-failed", generation.REQUESTABLE_REASONS)
        cancelled = World(run(10, conclusion="cancelled"))
        with self.assertRaisesRegex(MbError, "do not show"):
            cancelled.request("ci-generation-absent")
        self.assertEqual((world.dispatches(), cancelled.dispatches()), ([], []))

    def test_a_commit_the_default_branch_moved_past_is_never_requested(self) -> None:
        world = World(run(10), run(11, "packaged"))
        world.hub.set_branch(BRANCH, NEWER, TREE)
        self.assertEqual(world.request(), "superseded")
        self.assertEqual((world.dispatches(), world.sleeps), ([], []))

    def test_a_branch_that_moves_once_the_build_run_is_listed_gets_no_packaged_dispatch(self) -> None:
        # The packaged dispatch would run at the newer head, rebuild beside that head's own
        # (reused) Build run and leave the live head's generation mixed: nothing more is dispatched.
        world = World(run(10), run(11, "packaged"))
        original = world.sleep

        def sleep(seconds: float) -> None:
            original(seconds)
            if len(world.sleeps) == world.appear_after:
                world.hub.set_branch(BRANCH, NEWER, TREE)

        self.assertEqual(generation.request_generation(world.hub, subject=subject(), sleep=sleep,
                                                       reason="ci-generation-evidence-unavailable"), "superseded")
        self.assertEqual(world.dispatches(), [(BUILD_DISPATCH, {"ref": BRANCH})])
        self.assertEqual(world.hub.request_count, 7 + 2 + 2)

    def test_a_run_in_progress_or_a_failed_request_is_not_requested_again(self) -> None:
        cases = {
            "Build running": ((run(10, status="in_progress", conclusion=None), run(11, "packaged")), "running"),
            "packaged queued": ((run(10), run(11, "packaged", status="queued", conclusion=None)), "running"),
            "requested Build failed": ((run(12, event="workflow_dispatch", conclusion="failure"), run(11, "packaged")),
                                       "already-requested"),
            "requested packaged run cancelled": (
                (run(12, event="workflow_dispatch"), run(13, "packaged", event="workflow_dispatch",
                                                         conclusion="cancelled")), "already-requested"),
        }
        for label, (runs, outcome) in cases.items():
            world = World(*runs)
            with self.subTest(case=label):
                self.assertEqual(world.request(), outcome)
                self.assertEqual(world.dispatches(), [])

    def test_a_requested_generation_is_requested_again_only_once_its_evidence_is_gone(self) -> None:
        requested = (run(12, event="workflow_dispatch"), run(13, "packaged", event="workflow_dispatch"))
        for label, artifacts in (("expired", ((1, 12, True), (2, 13, False))), ("none listed", ()),
                                 ("packaged expired", ((1, 12, False), (2, 13, True)))):
            world = World(*requested)
            for artifact_id, run_id, expired in artifacts:
                world.artifact(artifact_id, run_id, expired=expired)
            with self.subTest(evidence=label):
                self.assertEqual(world.request(), "requested")
                # The new Build run must be newer than the requested one before the packaged
                # dispatch: the old successful dispatch run is not it.
                self.assertEqual(world.sleeps, [limits.CI_REQUEST_GENERATION_POLL_SECONDS] * 2)
                self.assertEqual(world.dispatches(), [(BUILD_DISPATCH, {"ref": BRANCH}),
                                                      (PACKAGED_DISPATCH, {"ref": BRANCH})])
                self.assertLessEqual(world.hub.request_count, limits.MAX_CI_REQUEST_GENERATION_REQUESTS)
        # A reason read before the requested generation completed (a re-run of a request job, a
        # job queued behind a runner wait) finds its evidence still there: nothing is requested.
        stale = World(*requested)
        stale.artifact(1, 12)
        stale.artifact(2, 13)
        with self.assertRaisesRegex(MbError, "still has its evidence"):
            stale.request()
        # A requested Build run whose packaged caller was never dispatched (listed too late): a new
        # request would dispatch a second Build; an operator dispatches only the packaged caller.
        orphan = World(run(12, event="workflow_dispatch"))
        orphan.artifact(1, 12)
        with self.assertRaisesRegex(MbError, "still has its evidence"):
            orphan.request("ci-generation-absent")
        self.assertEqual((stale.dispatches(), orphan.dispatches(), stale.sleeps), ([], [], []))

    def test_a_build_run_that_never_appears_stops_before_the_packaged_dispatch(self) -> None:
        world = World(run(10), run(11, "packaged"))
        world.appear = None
        with self.assertRaisesRegex(generation.GenerationRequestError, "dispatch only the packaged caller"):
            world.request()
        self.assertEqual(world.dispatches(), [(BUILD_DISPATCH, {"ref": BRANCH})])
        self.assertEqual(len(world.sleeps), limits.CI_REQUEST_GENERATION_POLLS)
        # One dispatch, every listing, and the branch read once more before the error.
        self.assertEqual(world.hub.request_count, 7 + limits.CI_REQUEST_GENERATION_POLLS + 2)
        self.assertLessEqual(world.hub.request_count, limits.MAX_CI_REQUEST_GENERATION_REQUESTS)
        # An older push run, or a run of another event, is not the dispatched one.
        world = World(run(10), run(11, "packaged"))
        world.appear = run(5, event="workflow_dispatch", created_at="2026-10-01T10:00:00Z")
        with self.assertRaises(generation.GenerationRequestError):
            world.request()

    def test_a_branch_that_moves_before_the_build_run_is_listed_supersedes_the_request(self) -> None:
        # The dispatch ran at the newer head, so no run of this commit appears: nothing more is
        # dispatched, and the consumer of this commit fails closed.
        world = World(run(10), run(11, "packaged"))
        world.appear = None
        original = world.sleep

        def sleep(seconds: float) -> None:
            original(seconds)
            if len(world.sleeps) == 1:
                world.hub.set_branch(BRANCH, NEWER, TREE)

        self.assertEqual(generation.request_generation(world.hub, subject=subject(), sleep=sleep,
                                                       reason="ci-generation-evidence-unavailable"), "superseded")
        self.assertEqual(world.dispatches(), [(BUILD_DISPATCH, {"ref": BRANCH})])
        self.assertEqual(world.hub.request_count, 7 + limits.CI_REQUEST_GENERATION_POLLS + 2)

    def test_what_a_request_starts_is_always_a_full_generation(self) -> None:
        # The managed callers take no dispatch inputs: a request cannot choose a mode, a scope or
        # lanes. Their dispatched runs of a default-branch commit then neither reuse nor narrow,
        # decided without a request.
        managed = Path(__file__).resolve().parents[1] / "template" / "managed" / ".github" / "workflows"
        for producer in ("build", "packaged"):
            with self.subTest(producer=producer):
                text = (managed / Path(CI_CALLER_WORKFLOWS[producer]).name).read_text(encoding="utf-8")
                self.assertEqual(text.count("workflow_dispatch:"), 1)
                self.assertIn("\n  workflow_dispatch: {}\n", text)
        from tests import test_ci_scope

        world = test_ci_scope.World()
        with tempfile.TemporaryDirectory() as directory:
            admitted = reuse.admit_post_merge_reuse(world.api, plan=world.base, event="workflow_dispatch",
                                                    temporary_root=Path(directory))
            scoped = scope.derive_scope(world.api, plan=world.base, config=world.config, event="workflow_dispatch",
                                        run_id=test_ci_scope.RUN_ID, run_attempt=test_ci_scope.RUN_ATTEMPT,
                                        workflow_path=test_ci_scope.PACKAGED, temporary_root=Path(directory),
                                        derive=world.derive)
        self.assertEqual(admitted.reason, "not-a-push")
        self.assertIsInstance(scoped, scope.FullScope)
        self.assertEqual((scoped.reason, world.inputs, world.api.request_count), ("not-a-pull-request", [], 0))

    def test_only_a_requestable_reason_and_a_default_branch_subject_are_admitted(self) -> None:
        world = World(run(10), run(11, "packaged"))
        for reason in ("ci-generation-pending", "ci-generation-mixed", "ci-generation-failed", "rejected", "", None):
            with self.subTest(reason=reason), self.assertRaises(MbError):
                world.request(reason)  # type: ignore[arg-type]
        # A reason the runs do not show starts nothing: a consumer that misread its selection.
        for reason, runs in (("ci-generation-absent", (run(10), run(11, "packaged"))),
                             ("ci-generation-absent", (run(10, conclusion="failure"),)),
                             ("ci-generation-evidence-unavailable", (run(10, conclusion="failure"),
                                                                     run(11, "packaged"))),
                             ("ci-generation-evidence-unavailable", (run(10),))):
            misread = World(*runs)
            with self.subTest(reason=reason, runs=len(runs)), self.assertRaisesRegex(MbError, "do not show"):
                misread.request(reason)
            self.assertEqual(misread.dispatches(), [])
        for changes in ({"pr_number": 7}, {"tested_sha": NEWER}, {"head_branch": "feature/x"},
                        {"repository": "other/mod"}):
            with self.subTest(changes=changes), self.assertRaises(MbError):
                world.request(**changes)
        reader = World(run(10), run(11, "packaged"), writable=False)
        with self.assertRaisesRegex(MbError, "start workflows"):
            reader.request()
        self.assertEqual((world.hub.request_count, reader.hub.request_count, world.dispatches()), (0, 0, []))


class CommandTests(unittest.TestCase):
    def test_the_command_requests_for_the_job_s_subject_and_outputs_the_outcome(self) -> None:
        world = World(run(10), run(11, "packaged"))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, output = root / "state", root / "output"
            fixtures = Path(__file__).resolve().parent / "fixtures" / "mods" / "qs_like"
            args = cli.build_parser("ci").parse_args([
                "ci", "request-generation", "--repo", str(fixtures), "--state", str(state),
                "--reason", "ci-generation-evidence-unavailable", "--github-output", str(output)])
            self.assertIs(args.handler.__name__, "run_request_generation")
            record = {"subject": subject(repository=REPOSITORY)}
            clients = []

            def client(environment, *, writable=False, max_requests=None):
                clients.append((writable, max_requests))
                return world.hub

            environ = {"GITHUB_REPOSITORY": REPOSITORY, "GITHUB_SHA": COMMIT, "GH_TOKEN": "t"}
            stdout = io.StringIO()
            with mock.patch.object(identity, "read_subject", return_value=record), \
                    mock.patch.object(commands.github_api, "from_environment", side_effect=client), \
                    mock.patch.object(cli, "environ", return_value=environ), \
                    mock.patch.object(generation.time, "sleep", side_effect=world.sleep), \
                    mock.patch("mod_base.runtime.build_invocation", wraps=None) as build:
                build.return_value = mock.Mock(repository=REPOSITORY, implementation_sha=COMMIT)
                with redirect_stdout(stdout):
                    self.assertEqual(args.handler(args), 0)
            self.assertEqual(clients, [(True, limits.MAX_CI_REQUEST_GENERATION_REQUESTS)])
            self.assertEqual(dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines()),
                             {"requested": "true", "outcome": "requested"})
            self.assertIn("request-generation: requested", stdout.getvalue())
        for reason in ("ci-generation-pending", "ci-generation-mixed", "ci-generation-failed"):
            with self.subTest(reason=reason), self.assertRaises(MbError):
                cli.build_parser("ci").parse_args(["ci", "request-generation", "--repo", "mod", "--state", "s",
                                                   "--reason", reason, "--github-output", "o"])


if __name__ == "__main__":
    unittest.main()
